from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    ApprovalRecord,
    AuditEvent,
    Document,
    DocumentPage,
    ExtractionEvidence,
    ExtractionRun,
    Invoice,
    InvoiceVersion,
    Job,
    OutboxEvent,
    PurchaseOrder,
    PurchaseOrderLine,
    ReconciliationRun,
    ReviewDecision,
    Supplier,
)
from app.domain.errors import DomainError
from app.domain.reconciliation import POLICY_VERSION, reconcile
from app.domain.snapshots import canonical_hash
from app.schemas import DecisionCreate, ReconciliationCreate, VersionCreate
from app.security import Principal
from app.services.common import IdempotentReplay, audit, begin_idempotent, finish_idempotent
from app.services.purchase_orders import po_response


def _invoice_for_org(db: Session, org_id: uuid.UUID, invoice_id: uuid.UUID, *, lock: bool = False) -> Invoice:
    stmt = select(Invoice).where(Invoice.org_id == org_id, Invoice.id == invoice_id)
    if lock:
        stmt = stmt.with_for_update()
    invoice = db.scalar(stmt)
    if invoice is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy chứng từ.", 404)
    return invoice


def _invoice(db: Session, actor: Principal, invoice_id: uuid.UUID, *, lock: bool = False) -> Invoice:
    return _invoice_for_org(db, actor.org_id, invoice_id, lock=lock)


def _version(db: Session, invoice: Invoice) -> InvoiceVersion:
    if invoice.current_version_id is None:
        raise DomainError("VERSION_NOT_READY", "Chứng từ chưa có dữ liệu trích xuất.", 409)
    version = db.scalar(
        select(InvoiceVersion).where(
            InvoiceVersion.org_id == invoice.org_id,
            InvoiceVersion.invoice_id == invoice.id,
            InvoiceVersion.id == invoice.current_version_id,
        )
    )
    if version is None:
        raise DomainError("VERSION_NOT_FOUND", "Không tìm thấy phiên bản hiện hành.", 409)
    return version


def _check_expected(invoice: Invoice, expected: uuid.UUID) -> InvoiceVersion:
    current = invoice.current_version_id
    if current != expected:
        raise DomainError(
            "STALE_REVIEW_VERSION",
            "Dữ liệu đã thay đổi. Hãy tải lại trước khi tiếp tục.",
            409,
            {
                "current_version_no": invoice.version_counter,
                "current_version_id": str(current) if current else None,
            },
        )
    return None  # type: ignore[return-value]


def _leaf_fields(data: dict[str, Any]) -> dict[str, Any]:
    values = {
        k: v
        for k, v in data.items()
        if k not in {"schema_version", "document_id", "lines"}
        and (k != "invoice_series_absent_confirmed" or v is True)
    }
    for index, line in enumerate(data.get("lines", [])):
        for key, value in line.items():
            if key != "line_id":
                values[f"lines[{index}].{key}"] = value
    return values


def create_invoice_version(
    db: Session, actor: Principal, invoice_id: uuid.UUID, body: VersionCreate, key: str | None
) -> dict[str, Any]:
    command = body.model_dump(mode="json")
    claim = begin_idempotent(db, actor, f"POST /invoices/{invoice_id}/versions", key, command)
    if isinstance(claim, IdempotentReplay):
        return claim.response
    invoice = _invoice(db, actor, invoice_id, lock=True)
    current = _version(db, invoice)
    if current.id != body.expected_version_id:
        raise DomainError(
            "STALE_REVIEW_VERSION",
            "Dữ liệu đã thay đổi. Hãy tải lại trước khi sửa.",
            409,
            {"current_version_no": invoice.version_counter},
        )
    if invoice.workflow_status in {"APPROVED", "REJECTED"}:
        raise DomainError("INVOICE_TERMINAL", "Hồ sơ đã kết thúc và không thể sửa.", 409)
    document = db.scalar(
        select(Document).where(Document.org_id == actor.org_id, Document.id == invoice.document_id)
    )
    if document is None or body.manual_source_page > document.pages:
        raise DomainError("INVALID_SOURCE_PAGE", "Trang nguồn không nằm trong tài liệu.", 422)

    draft = body.draft.model_dump(mode="json")
    old_data = current.canonical_json
    old_lines = old_data.get("lines", [])
    seen_ids: set[str] = set()
    for index, line in enumerate(draft.get("lines", [])):
        supplied = line.get("line_id")
        if supplied is None and index < len(old_lines):
            supplied = old_lines[index].get("line_id")
        if supplied is None:
            supplied = str(uuid.uuid4())
        if index < len(old_lines) and supplied != old_lines[index].get("line_id"):
            raise DomainError("LINE_ID_IMMUTABLE", "Không thể thay line ID hiện có.", 422)
        if supplied in seen_ids:
            raise DomainError("DUPLICATE_LINE_ID", "Line ID bị trùng.", 422)
        seen_ids.add(supplied)
        line["line_id"] = supplied
    draft["document_id"] = str(invoice.document_id)
    draft["schema_version"] = "1.0"
    snapshot_hash = canonical_hash(draft)
    version = InvoiceVersion(
        org_id=actor.org_id,
        invoice_id=invoice.id,
        version_no=invoice.version_counter + 1,
        canonical_json=draft,
        schema_version="1.0",
        parent_id=current.id,
        snapshot_hash=snapshot_hash,
        origin="manual",
        creator_id=actor.user_id,
        reason=body.reason,
    )
    db.add(version)
    db.flush()

    old_evidence = db.scalars(
        select(ExtractionEvidence).where(
            ExtractionEvidence.org_id == actor.org_id,
            ExtractionEvidence.invoice_id == invoice.id,
            ExtractionEvidence.version_id == current.id,
        )
    ).all()
    evidence_by_path = {item.field_path: item for item in old_evidence}
    old_fields, new_fields = _leaf_fields(old_data), _leaf_fields(draft)
    for field_path, value in new_fields.items():
        if value is None and old_fields.get(field_path) is None:
            continue
        previous = evidence_by_path.get(field_path)
        if field_path in old_fields and old_fields[field_path] == value and previous:
            db.add(
                ExtractionEvidence(
                    org_id=actor.org_id,
                    invoice_id=invoice.id,
                    version_id=version.id,
                    field_path=field_path,
                    page_no=previous.page_no,
                    raw_value=previous.raw_value,
                    normalized_value=previous.normalized_value,
                    raw_text=previous.raw_text,
                    bbox=previous.bbox,
                    origin=previous.origin,
                    verification_state=previous.verification_state,
                    actor_id=previous.actor_id,
                    note=previous.note,
                )
            )
        else:
            db.add(
                ExtractionEvidence(
                    org_id=actor.org_id,
                    invoice_id=invoice.id,
                    version_id=version.id,
                    field_path=field_path,
                    page_no=body.manual_source_page,
                    raw_value=str(value) if value is not None else None,
                    normalized_value=str(value) if value is not None else None,
                    raw_text=str(value) if value is not None else None,
                    origin="manual",
                    verification_state="MANUAL",
                    actor_id=actor.user_id,
                    note=body.reason[:500],
                )
            )

    invoice.current_version_id = version.id
    invoice.version_counter = version.version_no
    invoice.current_reconciliation_id = None
    invoice.workflow_status = "REVIEW_REQUIRED"
    invoice.updated_at = datetime_now()
    response = {
        "invoice_id": str(invoice.id),
        "version_id": str(version.id),
        "version_no": version.version_no,
        "snapshot_hash": snapshot_hash,
    }
    finish_idempotent(db, claim, version.id, response)
    audit(
        db,
        actor,
        actor.org_id,
        "invoice",
        invoice.id,
        "INVOICE_VERSION_CREATED",
        before_hash=current.snapshot_hash,
        after_hash=snapshot_hash,
        metadata={
            "version_no": version.version_no,
            "reason": body.reason,
            "manual_source_page": body.manual_source_page,
        },
    )
    db.commit()
    return response


def datetime_now():
    from datetime import datetime

    return datetime.now(UTC)


def _business_key(data: dict[str, Any], supplier_id: uuid.UUID) -> str | None:
    number = data.get("invoice_number")
    value_date = data.get("invoice_date")
    series = data.get("invoice_series")
    if series is None and not data.get("invoice_series_absent_confirmed", False):
        return None
    if not number or not value_date:
        return None
    try:
        year = date.fromisoformat(value_date).year
    except (ValueError, TypeError):
        return None
    canonical = {
        "supplier_id": str(supplier_id),
        "invoice_series": series or "",
        "invoice_number": number,
        "invoice_year": year,
    }
    return canonical_hash(canonical)


def _pending_duplicate(
    db: Session,
    actor: Principal,
    invoice_id: uuid.UUID,
    data: dict[str, Any],
    supplier_id: uuid.UUID,
    business_key: str | None,
) -> bool:
    if not business_key:
        return False
    rows = db.execute(
        select(Invoice, InvoiceVersion)
        .join(
            InvoiceVersion,
            (InvoiceVersion.org_id == Invoice.org_id)
            & (InvoiceVersion.invoice_id == Invoice.id)
            & (InvoiceVersion.id == Invoice.current_version_id),
        )
        .where(
            Invoice.org_id == actor.org_id,
            Invoice.id != invoice_id,
            Invoice.workflow_status.in_(["REVIEW_REQUIRED", "READY_FOR_APPROVAL", "PROCESSING"]),
        )
    ).all()
    for _, version in rows:
        if _business_key(version.canonical_json, supplier_id) == business_key:
            return True
    return False


def create_reconciliation(
    db: Session, actor: Principal, invoice_id: uuid.UUID, body: ReconciliationCreate, key: str | None
) -> dict[str, Any]:
    command = body.model_dump(mode="json")
    claim = begin_idempotent(db, actor, f"POST /invoices/{invoice_id}/reconciliations", key, command)
    if isinstance(claim, IdempotentReplay):
        return claim.response
    invoice = _invoice(db, actor, invoice_id, lock=True)
    version = _version(db, invoice)
    if version.id != body.expected_version_id:
        raise DomainError(
            "STALE_REVIEW_VERSION",
            "Dữ liệu đã thay đổi. Hãy tải lại trước khi đối soát.",
            409,
            {"current_version_no": invoice.version_counter},
        )
    if invoice.workflow_status in {"APPROVED", "REJECTED", "PROCESSING", "PROCESSING_FAILED"}:
        raise DomainError("INVALID_INVOICE_STATE", "Hồ sơ chưa thể đối soát ở trạng thái hiện tại.", 409)
    po = db.scalar(
        select(PurchaseOrder).where(PurchaseOrder.org_id == actor.org_id, PurchaseOrder.id == body.po_id)
    )
    if po is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy PO.", 404)
    supplier = db.scalar(
        select(Supplier).where(Supplier.org_id == actor.org_id, Supplier.id == po.supplier_id)
    )
    if supplier is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy nhà cung cấp.", 404)
    po_lines = db.scalars(
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.org_id == actor.org_id, PurchaseOrderLine.po_id == po.id)
        .order_by(PurchaseOrderLine.line_no)
    ).all()
    invoice_data = version.canonical_json
    invoice_lines = invoice_data.get("lines", [])
    manual_mapping = {str(k): str(v) for k, v in body.line_mapping.items() if v}
    mapping = dict(manual_mapping)
    invoice_counts: dict[str, int] = {}
    po_counts: dict[str, int] = {}
    for line in invoice_lines:
        if line.get("sku"):
            sku = line["sku"].casefold()
            invoice_counts[sku] = invoice_counts.get(sku, 0) + 1
    for line in po_lines:
        if line.sku:
            sku = line.sku.casefold()
            po_counts[sku] = po_counts.get(sku, 0) + 1
    po_by_sku = {
        line.sku.casefold(): line for line in po_lines if line.sku and po_counts.get(line.sku.casefold()) == 1
    }
    for line in invoice_lines:
        if not line.get("line_id") or line["line_id"] in mapping or not line.get("sku"):
            continue
        sku = line["sku"].casefold()
        candidate_po_line = po_by_sku.get(sku)
        if (
            candidate_po_line
            and invoice_counts.get(sku) == 1
            and (line.get("unit") or "").casefold() == candidate_po_line.unit.casefold()
        ):
            mapping[line["line_id"]] = str(candidate_po_line.id)
    po_view = {
        **po_response(db, po),
        "lines": [
            {
                "id": str(line.id),
                "line_no": line.line_no,
                "sku": line.sku,
                "description": line.description,
                "unit": line.unit,
                "quantity": format(line.quantity, "f"),
                "unit_price": format(line.unit_price, "f"),
            }
            for line in po_lines
        ],
    }
    mapping_provenance = {
        invoice_line_id: {
            "origin": "manual" if invoice_line_id in manual_mapping else "sku_auto",
            "actor_id": str(actor.user_id),
        }
        for invoice_line_id in mapping
    }
    business_key = _business_key(invoice_data, supplier.id)
    duplicate_approved = bool(
        business_key
        and db.scalar(
            select(ApprovalRecord.id).where(
                ApprovalRecord.org_id == actor.org_id, ApprovalRecord.business_key == business_key
            )
        )
    )
    duplicate_pending = _pending_duplicate(db, actor, invoice.id, invoice_data, supplier.id, business_key)
    evidence = db.scalars(
        select(ExtractionEvidence).where(
            ExtractionEvidence.org_id == actor.org_id,
            ExtractionEvidence.invoice_id == invoice.id,
            ExtractionEvidence.version_id == version.id,
        )
    ).all()
    unverified = any(item.verification_state != "TEXT_VERIFIED" for item in evidence)
    result = reconcile(
        invoice_data,
        po_view,
        mapping,
        duplicate_approved=duplicate_approved,
        duplicate_pending=duplicate_pending,
        has_unverified_source=unverified,
    )
    snapshot = {
        "invoice_version_id": str(version.id),
        "invoice_snapshot_hash": version.snapshot_hash,
        "po_id": str(po.id),
        "po_snapshot": po_view,
        "mapping": mapping,
        "mapping_provenance": mapping_provenance,
        "policy_version": POLICY_VERSION,
    }
    snapshot_hash = canonical_hash(snapshot)
    run = ReconciliationRun(
        org_id=actor.org_id,
        invoice_id=invoice.id,
        invoice_version_id=version.id,
        po_id=po.id,
        mapping_json=mapping,
        mapping_provenance=mapping_provenance,
        policy_version=POLICY_VERSION,
        result_json=result,
        status=result["status"],
        snapshot_hash=snapshot_hash,
    )
    db.add(run)
    db.flush()
    invoice.current_reconciliation_id = run.id
    invoice.workflow_status = (
        "READY_FOR_APPROVAL" if result["status"] in {"PASS", "WARNING"} else "REVIEW_REQUIRED"
    )
    invoice.updated_at = datetime_now()
    response = {
        "reconciliation_id": str(run.id),
        "status": result["status"],
        "issues": result["issues"],
        "mapping": mapping,
        "mapping_provenance": mapping_provenance,
        "snapshot_hash": snapshot_hash,
        "po": po_view,
    }
    finish_idempotent(db, claim, run.id, response)
    audit(
        db,
        actor,
        actor.org_id,
        "invoice",
        invoice.id,
        "RECONCILIATION_CREATED",
        after_hash=snapshot_hash,
        metadata={"reconciliation_id": str(run.id), "status": result["status"], "po_id": str(po.id)},
    )
    db.commit()
    return response


def create_decision(
    db: Session, actor: Principal, invoice_id: uuid.UUID, body: DecisionCreate, key: str | None
) -> dict[str, Any]:
    command = body.model_dump(mode="json")
    claim = begin_idempotent(db, actor, f"POST /invoices/{invoice_id}/decisions", key, command)
    if isinstance(claim, IdempotentReplay):
        return claim.response
    invoice = _invoice(db, actor, invoice_id, lock=True)
    version = _version(db, invoice)
    if version.id != body.expected_version_id:
        raise DomainError(
            "STALE_REVIEW_VERSION",
            "Phiên bản đã thay đổi. Tải lại trước khi quyết định.",
            409,
            {"current_version_no": invoice.version_counter},
        )
    if invoice.current_reconciliation_id != body.reconciliation_id:
        raise DomainError("STALE_RECONCILIATION", "Kết quả đối soát đã cũ. Hãy chạy lại đối soát.", 409)
    reconciliation = db.scalar(
        select(ReconciliationRun).where(
            ReconciliationRun.org_id == actor.org_id,
            ReconciliationRun.invoice_id == invoice.id,
            ReconciliationRun.id == body.reconciliation_id,
            ReconciliationRun.invoice_version_id == version.id,
        )
    )
    if reconciliation is None or reconciliation.snapshot_hash != body.expected_snapshot_hash:
        raise DomainError("STALE_RECONCILIATION", "Snapshot đối soát không còn hiện hành.", 409)
    if invoice.workflow_status in {"APPROVED", "REJECTED"}:
        raise DomainError("INVOICE_TERMINAL", "Hồ sơ đã có quyết định cuối cùng.", 409)
    result = reconciliation.result_json
    if body.action == "REJECT":
        if not body.note or len(body.note.strip()) < 3:
            raise DomainError("REJECTION_REASON_REQUIRED", "Nhập lý do từ chối (ít nhất 3 ký tự).", 422)
    else:
        if result.get("status") == "BLOCKED":
            raise DomainError(
                "APPROVAL_BLOCKED",
                "Đối soát còn lỗi BLOCK; không thể phê duyệt.",
                409,
                {"issue_codes": [i["code"] for i in result.get("issues", []) if i["severity"] == "BLOCK"]},
            )
        warning_codes = {i["code"] for i in result.get("issues", []) if i["severity"] == "WARN"}
        acknowledged = set(body.acknowledged_warning_codes)
        if warning_codes - acknowledged:
            raise DomainError(
                "WARNINGS_NOT_ACKNOWLEDGED",
                "Reviewer cần xác nhận tất cả cảnh báo.",
                409,
                {"missing_warning_codes": sorted(warning_codes - acknowledged)},
            )
        if acknowledged - warning_codes:
            raise DomainError(
                "INVALID_WARNING_ACKNOWLEDGEMENT", "Có mã cảnh báo không thuộc snapshot này.", 422
            )
        if result.get("status") not in {"PASS", "WARNING"}:
            raise DomainError(
                "APPROVAL_BLOCKED", "Chỉ PASS hoặc WARNING đã xác nhận mới có thể phê duyệt.", 409
            )

    po = db.scalar(
        select(PurchaseOrder)
        .where(PurchaseOrder.org_id == actor.org_id, PurchaseOrder.id == reconciliation.po_id)
        .with_for_update()
    )
    if po is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy PO.", 404)
    if body.action == "APPROVE":
        if po.state != "OPEN":
            raise DomainError("PO_CLOSED", "PO đã được đóng hoặc phê duyệt.", 409)
        supplier = db.scalar(
            select(Supplier).where(Supplier.org_id == actor.org_id, Supplier.id == po.supplier_id)
        )
        if supplier is None:
            raise DomainError("NOT_FOUND", "Không tìm thấy nhà cung cấp.", 404)
        business_key = _business_key(version.canonical_json, supplier.id)
        if not business_key:
            raise DomainError(
                "APPROVAL_KEY_INCOMPLETE", "Cần xác nhận số hóa đơn, ngày và series trước khi duyệt.", 409
            )
        duplicate = db.scalar(
            select(ApprovalRecord.id).where(
                ApprovalRecord.org_id == actor.org_id,
                (ApprovalRecord.business_key == business_key) | (ApprovalRecord.po_id == po.id),
            )
        )
        if duplicate:
            raise DomainError(
                "DUPLICATE_APPROVED_INVOICE", "Hóa đơn hoặc PO đã được phê duyệt trước đó.", 409
            )

    decision = ReviewDecision(
        org_id=actor.org_id,
        invoice_id=invoice.id,
        version_id=version.id,
        reconciliation_id=reconciliation.id,
        action=body.action,
        actor_id=actor.user_id,
        note=body.note.strip() if body.note else None,
        acknowledged_warnings=sorted(body.acknowledged_warning_codes),
        expected_snapshot_hash=body.expected_snapshot_hash,
    )
    db.add(decision)
    db.flush()
    if body.action == "APPROVE":
        supplier = db.scalar(
            select(Supplier).where(Supplier.org_id == actor.org_id, Supplier.id == po.supplier_id)
        )
        db.add(
            ApprovalRecord(
                org_id=actor.org_id,
                invoice_id=invoice.id,
                version_id=version.id,
                po_id=po.id,
                supplier_id=supplier.id,
                business_key=_business_key(version.canonical_json, supplier.id),
                decision_id=decision.id,
            )
        )
        po.state = "CLOSED"
        invoice.workflow_status = "APPROVED"
    else:
        invoice.workflow_status = "REJECTED"
    invoice.updated_at = datetime_now()
    if settings.workflow_engine == "langgraph":
        active_job = db.scalar(
            select(Job)
            .where(
                Job.org_id == actor.org_id,
                Job.entity_id == invoice.id,
                Job.status == "WAITING_REVIEW",
            )
            .order_by(Job.created_at.desc())
            .with_for_update()
        )
        if active_job is not None:
            db.add(
                OutboxEvent(
                    org_id=actor.org_id,
                    kind="RESUME_INVOICE_GRAPH",
                    job_id=active_job.id,
                    payload_ref={
                        "job_id": str(active_job.id),
                        "decision_id": str(decision.id),
                    },
                    status="PENDING",
                )
            )
    response = {
        "decision_id": str(decision.id),
        "invoice_id": str(invoice.id),
        "action": body.action,
        "status": invoice.workflow_status,
        "version_id": str(version.id),
        "reconciliation_id": str(reconciliation.id),
    }
    finish_idempotent(db, claim, decision.id, response)
    audit(
        db,
        actor,
        actor.org_id,
        "invoice",
        invoice.id,
        "INVOICE_APPROVED" if body.action == "APPROVE" else "INVOICE_REJECTED",
        before_hash=version.snapshot_hash,
        after_hash=reconciliation.snapshot_hash,
        metadata={
            "decision_id": str(decision.id),
            "action": body.action,
            "note": body.note.strip() if body.note else None,
        },
    )
    db.commit()
    return response


def invoice_summary(db: Session, invoice: Invoice) -> dict[str, Any]:
    document = db.scalar(
        select(Document).where(Document.org_id == invoice.org_id, Document.id == invoice.document_id)
    )
    version = (
        db.scalar(
            select(InvoiceVersion).where(
                InvoiceVersion.org_id == invoice.org_id,
                InvoiceVersion.invoice_id == invoice.id,
                InvoiceVersion.id == invoice.current_version_id,
            )
        )
        if invoice.current_version_id
        else None
    )
    run = db.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.org_id == invoice.org_id, ExtractionRun.document_id == invoice.document_id)
        .order_by(ExtractionRun.started_at.desc().nullslast())
        .limit(1)
    )
    data = version.canonical_json if version else {}
    fixture_mode = bool(run and run.provider == "fixture")
    return {
        "id": str(invoice.id),
        "document_id": str(invoice.document_id),
        "filename": document.original_name if document else "",
        "mime": document.mime if document else "application/octet-stream",
        "pages": document.pages if document else 0,
        "invoice_number": data.get("invoice_number"),
        "invoice_date": data.get("invoice_date"),
        "supplier_name": data.get("supplier_name"),
        "supplier_tax_id": data.get("supplier_tax_id"),
        "grand_total": data.get("grand_total"),
        "currency": data.get("currency"),
        "workflow_status": invoice.workflow_status,
        "version_id": str(version.id) if version else None,
        "version_no": version.version_no if version else 0,
        "snapshot_hash": version.snapshot_hash if version else None,
        "fixture_mode": fixture_mode,
        "provider": run.provider if run else None,
        "job_id": None,
        "created_at": invoice.created_at.isoformat(),
    }


def get_invoice_list(db: Session, org_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Invoice).where(Invoice.org_id == org_id).order_by(Invoice.created_at.desc()).limit(limit)
    ).all()
    return [invoice_summary(db, row) for row in rows]


def get_invoice_detail_for_org(
    db: Session, org_id: uuid.UUID, invoice_id: uuid.UUID, *, role: str = "read_only"
) -> dict[str, Any]:
    """Shared organization-scoped invoice read used by REST and read-only adapters."""
    invoice = _invoice_for_org(db, org_id, invoice_id)
    summary = invoice_summary(db, invoice)
    document = db.scalar(
        select(Document).where(Document.org_id == org_id, Document.id == invoice.document_id)
    )
    version = (
        db.scalar(
            select(InvoiceVersion).where(
                InvoiceVersion.org_id == org_id,
                InvoiceVersion.invoice_id == invoice.id,
                InvoiceVersion.id == invoice.current_version_id,
            )
        )
        if invoice.current_version_id
        else None
    )
    evidence = (
        db.scalars(
            select(ExtractionEvidence)
            .where(
                ExtractionEvidence.org_id == org_id,
                ExtractionEvidence.invoice_id == invoice.id,
                ExtractionEvidence.version_id == invoice.current_version_id,
            )
            .order_by(ExtractionEvidence.field_path)
        ).all()
        if version
        else []
    )
    reconciliation = (
        db.scalar(
            select(ReconciliationRun).where(
                ReconciliationRun.org_id == org_id,
                ReconciliationRun.invoice_id == invoice.id,
                ReconciliationRun.id == invoice.current_reconciliation_id,
            )
        )
        if invoice.current_reconciliation_id
        else None
    )
    job = db.scalar(
        select(Job)
        .where(Job.org_id == org_id, Job.entity_id == invoice.id)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    summary["job_id"] = str(job.id) if job else None
    summary["job"] = (
        {
            "id": str(job.id),
            "status": job.status,
            "step": job.current_step,
            "attempts": job.attempts,
            "error_code": job.error_code,
        }
        if job
        else None
    )
    # Frontend API helpers prepend the /api/v1 base path; return a relative API path here.
    summary["content_url"] = f"/documents/{invoice.document_id}/content"
    summary["document"] = (
        {"mime": document.mime, "pages": document.pages, "sha256": document.sha256} if document else None
    )
    summary["document_pages"] = [
        {
            "page_no": page.page_no,
            "page_hash": page.page_hash,
            "preview_url": f"/documents/{invoice.document_id}/pages/{page.page_no}",
        }
        for page in db.scalars(
            select(DocumentPage)
            .where(
                DocumentPage.org_id == org_id,
                DocumentPage.document_id == invoice.document_id,
            )
            .order_by(DocumentPage.page_no)
        )
    ]
    summary["version"] = (
        {
            "id": str(version.id),
            "version_no": version.version_no,
            "snapshot_hash": version.snapshot_hash,
            "origin": version.origin,
            "reason": version.reason,
            "data": version.canonical_json,
        }
        if version
        else None
    )
    summary["evidence"] = [
        {
            "field_path": e.field_path,
            "page_no": e.page_no,
            "raw_value": e.raw_value,
            "normalized_value": e.normalized_value,
            "raw_text": e.raw_text,
            "origin": e.origin,
            "verification_state": e.verification_state,
            "actor_id": str(e.actor_id) if e.actor_id else None,
            "note": e.note,
        }
        for e in evidence
    ]
    summary["reconciliation"] = (
        {
            "id": str(reconciliation.id),
            "status": reconciliation.status,
            "issues": reconciliation.result_json.get("issues", []),
            "mapping": reconciliation.mapping_json,
            "mapping_provenance": reconciliation.mapping_provenance,
            "po_id": str(reconciliation.po_id),
            "snapshot_hash": reconciliation.snapshot_hash,
            "policy_version": reconciliation.policy_version,
        }
        if reconciliation
        else None
    )
    summary["allowed_actions"] = {
        "edit": invoice.workflow_status in {"REVIEW_REQUIRED", "READY_FOR_APPROVAL"},
        "reconcile": invoice.workflow_status in {"REVIEW_REQUIRED", "READY_FOR_APPROVAL"},
        "approve": role == "reviewer" and invoice.workflow_status == "READY_FOR_APPROVAL",
        "reject": role == "reviewer"
        and invoice.workflow_status in {"REVIEW_REQUIRED", "READY_FOR_APPROVAL"},
        "export": invoice.workflow_status == "APPROVED",
    }
    return summary


def get_invoice_detail(db: Session, actor: Principal, invoice_id: uuid.UUID) -> dict[str, Any]:
    return get_invoice_detail_for_org(db, actor.org_id, invoice_id, role=actor.role)


def get_versions(db: Session, actor: Principal, invoice_id: uuid.UUID) -> list[dict[str, Any]]:
    invoice = _invoice(db, actor, invoice_id)
    return [
        {
            "id": str(v.id),
            "version_no": v.version_no,
            "origin": v.origin,
            "reason": v.reason,
            "snapshot_hash": v.snapshot_hash,
            "parent_id": str(v.parent_id) if v.parent_id else None,
            "created_at": v.created_at.isoformat(),
        }
        for v in db.scalars(
            select(InvoiceVersion)
            .where(InvoiceVersion.org_id == actor.org_id, InvoiceVersion.invoice_id == invoice.id)
            .order_by(InvoiceVersion.version_no)
        )
    ]


def get_job(db: Session, actor: Principal, job_id: uuid.UUID) -> dict[str, Any]:
    job = db.scalar(select(Job).where(Job.org_id == actor.org_id, Job.id == job_id))
    if job is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy job.", 404)
    return {
        "id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "step": job.current_step,
        "attempts": job.attempts,
        "error_code": job.error_code,
        "updated_at": job.updated_at.isoformat(),
    }


def retry_job(db: Session, actor: Principal, job_id: uuid.UUID, key: str | None) -> dict[str, Any]:
    command = {"job_id": str(job_id)}
    claim = begin_idempotent(db, actor, f"POST /jobs/{job_id}/retry", key, command)
    if isinstance(claim, IdempotentReplay):
        return claim.response
    old_job = db.scalar(select(Job).where(Job.org_id == actor.org_id, Job.id == job_id).with_for_update())
    if old_job is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy job.", 404)
    invoice = db.scalar(
        select(Invoice)
        .where(Invoice.org_id == actor.org_id, Invoice.id == old_job.entity_id)
        .with_for_update()
    )
    if invoice is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy chứng từ.", 404)
    if old_job.status != "FAILED" or invoice.workflow_status != "PROCESSING_FAILED":
        raise DomainError("INVALID_RETRY_STATE", "Chỉ có thể thử lại hồ sơ xử lý đã thất bại.", 409)
    if invoice.workflow_status in {"APPROVED", "REJECTED"}:
        raise DomainError("INVOICE_TERMINAL", "Hồ sơ đã kết thúc.", 409)
    document = db.scalar(
        select(Document).where(Document.org_id == actor.org_id, Document.id == invoice.document_id)
    )
    previous_run = db.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.org_id == actor.org_id, ExtractionRun.document_id == invoice.document_id)
        .order_by(ExtractionRun.started_at.desc().nullslast())
    )
    run = ExtractionRun(
        org_id=actor.org_id,
        document_id=document.id if document else invoice.document_id,
        provider=settings.extraction_provider,
        prompt_version=settings.extraction_prompt_version,
        schema_version="1.0",
        usage={"locale": (previous_run.usage or {}).get("locale", "vi-VN") if previous_run else "vi-VN"},
        status="PENDING",
    )
    new_job = Job(
        org_id=actor.org_id,
        kind="EXTRACT_INVOICE",
        entity_id=invoice.id,
        status="PENDING",
        current_step="QUEUED",
        requested_by=actor.user_id,
    )
    db.add_all([run, new_job])
    db.flush()
    db.add(
        OutboxEvent(
            org_id=actor.org_id,
            kind="PROCESS_INVOICE",
            job_id=new_job.id,
            payload_ref={"job_id": str(new_job.id)},
            status="PENDING",
        )
    )
    invoice.workflow_status = "PROCESSING"
    response = {"job_id": str(new_job.id), "invoice_id": str(invoice.id), "status": "PENDING"}
    finish_idempotent(db, claim, new_job.id, response)
    audit(
        db,
        actor,
        actor.org_id,
        "invoice",
        invoice.id,
        "EXTRACTION_RETRY_REQUESTED",
        metadata={"previous_job_id": str(old_job.id), "new_job_id": str(new_job.id)},
    )
    db.commit()
    return response


def get_file(db: Session, actor: Principal, document_id: uuid.UUID) -> tuple[bytes, str, str]:
    document = db.scalar(select(Document).where(Document.org_id == actor.org_id, Document.id == document_id))
    if document is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy tài liệu.", 404)
    from app.documents.storage import get_private_storage

    return get_private_storage().read(document.storage_key), document.mime, document.original_name


def get_audit(db: Session, actor: Principal, invoice_id: uuid.UUID) -> list[dict[str, Any]]:
    _invoice(db, actor, invoice_id)
    rows = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.org_id == actor.org_id,
            AuditEvent.entity_type == "invoice",
            AuditEvent.entity_id == invoice_id,
        )
        .order_by(AuditEvent.created_at)
    ).all()
    return [
        {
            "action": row.action,
            "actor_id": str(row.actor_id) if row.actor_id else None,
            "before_hash": row.before_hash,
            "after_hash": row.after_hash,
            "metadata": row.metadata_json,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def export_invoice(db: Session, actor: Principal, invoice_id: uuid.UUID) -> dict[str, Any]:
    invoice = _invoice(db, actor, invoice_id)
    if invoice.workflow_status != "APPROVED":
        raise DomainError("EXPORT_NOT_APPROVED", "Chỉ hồ sơ đã được reviewer phê duyệt mới được xuất.", 409)
    approval = db.scalar(
        select(ApprovalRecord).where(
            ApprovalRecord.org_id == actor.org_id, ApprovalRecord.invoice_id == invoice.id
        )
    )
    if approval is None:
        raise DomainError("APPROVAL_RECORD_MISSING", "Thiếu bản ghi phê duyệt.", 409)
    version = db.scalar(
        select(InvoiceVersion).where(
            InvoiceVersion.org_id == actor.org_id,
            InvoiceVersion.invoice_id == invoice.id,
            InvoiceVersion.id == approval.version_id,
        )
    )
    reconciliation = db.scalar(
        select(ReconciliationRun).where(
            ReconciliationRun.org_id == actor.org_id,
            ReconciliationRun.invoice_id == invoice.id,
            ReconciliationRun.id == invoice.current_reconciliation_id,
            ReconciliationRun.invoice_version_id == approval.version_id,
        )
    )
    if version is None or reconciliation is None:
        raise DomainError("APPROVAL_SNAPSHOT_MISSING", "Snapshot đã duyệt không còn đầy đủ.", 409)
    extraction = db.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.org_id == actor.org_id, ExtractionRun.document_id == invoice.document_id)
        .order_by(ExtractionRun.started_at.desc().nullslast())
        .limit(1)
    )
    return {
        "invoice_id": str(invoice.id),
        "approved_version_id": str(version.id),
        "version_no": version.version_no,
        "invoice_snapshot_hash": version.snapshot_hash,
        "reconciliation_id": str(reconciliation.id),
        "reconciliation_snapshot_hash": reconciliation.snapshot_hash,
        "policy_version": reconciliation.policy_version,
        "fixture_mode": bool(extraction and extraction.provider == "fixture"),
        "approved_at": approval.created_at.isoformat(),
        "data": version.canonical_json,
    }


def _csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def export_invoice_csv(db: Session, actor: Principal, invoice_id: uuid.UUID) -> str:
    exported = export_invoice(db, actor, invoice_id)
    data = exported["data"]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "invoice_id",
            "invoice_number",
            "invoice_series",
            "invoice_date",
            "supplier_tax_id",
            "currency",
            "line_id",
            "sku",
            "description",
            "unit",
            "quantity",
            "unit_price",
            "net_amount",
            "subtotal",
            "tax_total",
            "grand_total",
            "version_id",
            "reconciliation_id",
            "fixture_mode",
        ]
    )
    for line in data.get("lines", []):
        writer.writerow(
            [
                exported["invoice_id"],
                _csv_safe(data.get("invoice_number")),
                _csv_safe(data.get("invoice_series")),
                _csv_safe(data.get("invoice_date")),
                _csv_safe(data.get("supplier_tax_id")),
                _csv_safe(data.get("currency")),
                line.get("line_id"),
                _csv_safe(line.get("sku")),
                _csv_safe(line.get("description")),
                _csv_safe(line.get("unit")),
                line.get("quantity"),
                line.get("unit_price"),
                line.get("net_amount"),
                data.get("subtotal"),
                data.get("tax_total"),
                data.get("grand_total"),
                exported["approved_version_id"],
                exported["reconciliation_id"],
                str(exported["fixture_mode"]).lower(),
            ]
        )
    return buffer.getvalue()
