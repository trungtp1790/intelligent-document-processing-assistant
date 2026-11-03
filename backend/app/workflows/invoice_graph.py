from __future__ import annotations

import uuid
from typing import Any, TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select

from app.db.models import (
    Document,
    ExtractionRun,
    Invoice,
    InvoiceVersion,
    Job,
    ReconciliationRun,
    ReviewDecision,
)
from app.db.session import SessionLocal
from app.documents.storage import get_private_storage
from app.domain.errors import DomainError
from app.domain.snapshots import canonical_hash
from app.schemas import InvoiceData
from app.services.extraction import process_invoice_job
from app.workflows.checkpoint import postgres_checkpointer, workflow_run_lock


class InvoiceWorkflowState(TypedDict, total=False):
    job_id: str
    org_id: str
    invoice_id: str
    document_id: str
    extraction_run_id: str
    version_id: str
    reconciliation_id: str | None
    decision_id: str | None
    job_status: str
    mime: str
    page_count: int
    phase: str


def _uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("INVALID_WORKFLOW_ID", f"Invalid internal {label}.", 422) from exc


def _load_snapshot(state: InvoiceWorkflowState) -> dict[str, Any]:
    job_id = _uuid(state["job_id"], "job id")
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.id == job_id))
        if job is None:
            raise DomainError("NOT_FOUND", "Workflow job does not exist.", 404)
        invoice = db.scalar(select(Invoice).where(Invoice.org_id == job.org_id, Invoice.id == job.entity_id))
        document = (
            db.scalar(
                select(Document).where(Document.org_id == job.org_id, Document.id == invoice.document_id)
            )
            if invoice
            else None
        )
        run = (
            db.scalar(
                select(ExtractionRun)
                .where(ExtractionRun.org_id == job.org_id, ExtractionRun.document_id == invoice.document_id)
                .order_by(ExtractionRun.started_at.desc().nullslast())
            )
            if invoice
            else None
        )
        if invoice is None or document is None:
            raise DomainError("WORKFLOW_SNAPSHOT_MISSING", "Invoice or document snapshot is missing.", 404)
        return {
            "org_id": str(job.org_id),
            "invoice_id": str(invoice.id),
            "document_id": str(document.id),
            "extraction_run_id": str(run.id) if run else "",
            "job_status": job.status,
            "mime": document.mime,
            "page_count": document.pages,
            "phase": "LOAD_SNAPSHOT",
        }


def _prepare_pages(state: InvoiceWorkflowState) -> dict[str, Any]:
    with SessionLocal() as db:
        document = db.scalar(
            select(Document).where(
                Document.org_id == _uuid(state["org_id"], "organization id"),
                Document.id == _uuid(state["document_id"], "document id"),
            )
        )
        if document is None:
            raise DomainError("NOT_FOUND", "Document does not exist.", 404)
        # Read the private object only to validate its reference. File bytes never enter graph state.
        get_private_storage().read(document.storage_key)
        return {"page_count": document.pages, "mime": document.mime, "phase": "PREPARE_PAGES"}


def _extract(state: InvoiceWorkflowState) -> dict[str, Any]:
    process_invoice_job(state["job_id"])
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.id == _uuid(state["job_id"], "job id")))
        if job is None:
            raise DomainError("NOT_FOUND", "Workflow job does not exist.", 404)
        return {"job_status": job.status, "phase": job.current_step}


def _validate_snapshot(state: InvoiceWorkflowState) -> dict[str, Any]:
    if state.get("job_status") != "WAITING_REVIEW":
        return {"phase": "VALIDATION_SKIPPED"}
    with SessionLocal() as db:
        invoice = db.scalar(
            select(Invoice).where(
                Invoice.org_id == _uuid(state["org_id"], "organization id"),
                Invoice.id == _uuid(state["invoice_id"], "invoice id"),
            )
        )
        version = (
            db.scalar(
                select(InvoiceVersion).where(
                    InvoiceVersion.org_id == invoice.org_id,
                    InvoiceVersion.invoice_id == invoice.id,
                    InvoiceVersion.id == invoice.current_version_id,
                )
            )
            if invoice and invoice.current_version_id
            else None
        )
        if invoice is None or version is None:
            raise DomainError("WORKFLOW_VERSION_MISSING", "Extracted invoice version is missing.", 409)
        try:
            candidate = dict(version.canonical_json)
            candidate.pop("document_id", None)
            InvoiceData.model_validate(candidate)
        except Exception as exc:
            raise DomainError(
                "WORKFLOW_VERSION_INVALID", "Stored invoice version failed schema validation.", 409
            ) from exc
        if canonical_hash(version.canonical_json) != version.snapshot_hash:
            raise DomainError(
                "WORKFLOW_SNAPSHOT_HASH_INVALID", "Stored invoice version hash is invalid.", 409
            )
        return {"version_id": str(version.id), "phase": "VALIDATE_INVOICE"}


def _reconcile(state: InvoiceWorkflowState) -> dict[str, Any]:
    """Read a persisted reconciliation snapshot if one exists; matching rules stay in the domain service."""
    with SessionLocal() as db:
        invoice = db.scalar(
            select(Invoice).where(
                Invoice.org_id == _uuid(state["org_id"], "organization id"),
                Invoice.id == _uuid(state["invoice_id"], "invoice id"),
            )
        )
        run = (
            db.scalar(
                select(ReconciliationRun).where(
                    ReconciliationRun.org_id == invoice.org_id,
                    ReconciliationRun.invoice_id == invoice.id,
                    ReconciliationRun.id == invoice.current_reconciliation_id,
                )
            )
            if invoice and invoice.current_reconciliation_id
            else None
        )
        return {
            "reconciliation_id": str(run.id) if run else None,
            "phase": "RECONCILE" if run else "AWAIT_PO_MAPPING",
        }


def _await_review(state: InvoiceWorkflowState) -> dict[str, Any]:
    org_id = _uuid(state["org_id"], "organization id")
    invoice_id = _uuid(state["invoice_id"], "invoice id")
    with SessionLocal() as db:
        invoice = db.scalar(select(Invoice).where(Invoice.org_id == org_id, Invoice.id == invoice_id))
        if invoice is None:
            raise DomainError("NOT_FOUND", "Invoice does not exist.", 404)
        decision = (
            db.scalar(
                select(ReviewDecision).where(
                    ReviewDecision.org_id == org_id,
                    ReviewDecision.invoice_id == invoice.id,
                    ReviewDecision.version_id == invoice.current_version_id,
                    ReviewDecision.reconciliation_id == invoice.current_reconciliation_id,
                )
            )
            if invoice.current_version_id
            else None
        )
        if decision:
            return {"decision_id": str(decision.id), "phase": "REVIEW_DECISION_RECORDED"}
        if invoice.workflow_status in {"APPROVED", "REJECTED"}:
            raise DomainError(
                "WORKFLOW_DECISION_MISSING", "Terminal invoice has no persisted review decision.", 409
            )
    if state.get("job_status") != "WAITING_REVIEW":
        return {"phase": "WORKFLOW_FINISHED"}

    # Interrupt payload contains only opaque business IDs, never document bytes or model output.
    resumed = interrupt(
        {
            "job_id": state["job_id"],
            "invoice_id": state["invoice_id"],
            "version_id": state.get("version_id"),
            "reconciliation_id": state.get("reconciliation_id"),
        }
    )
    if not isinstance(resumed, dict) or not isinstance(resumed.get("decision_id"), str):
        raise DomainError(
            "INVALID_WORKFLOW_RESUME", "Workflow can only resume from a persisted review decision.", 422
        )
    return {"decision_id": resumed["decision_id"], "phase": "REVIEW_RESUMED"}


def _finalize_decision(state: InvoiceWorkflowState) -> dict[str, Any]:
    if not state.get("decision_id"):
        return {"phase": "NO_DECISION"}
    decision_id = _uuid(state["decision_id"], "decision id")
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.id == _uuid(state["job_id"], "job id")).with_for_update())
        if job is None:
            raise DomainError("NOT_FOUND", "Workflow job does not exist.", 404)
        if job.status == "DONE":
            return {"job_status": "DONE", "phase": "FINALIZED"}
        decision = db.scalar(
            select(ReviewDecision).where(
                ReviewDecision.org_id == job.org_id,
                ReviewDecision.invoice_id == job.entity_id,
                ReviewDecision.id == decision_id,
            )
        )
        invoice = db.scalar(select(Invoice).where(Invoice.org_id == job.org_id, Invoice.id == job.entity_id))
        if decision is None or invoice is None:
            raise DomainError("WORKFLOW_DECISION_MISSING", "Persisted review decision is missing.", 409)
        reconciliation = db.scalar(
            select(ReconciliationRun).where(
                ReconciliationRun.org_id == job.org_id,
                ReconciliationRun.invoice_id == invoice.id,
                ReconciliationRun.id == decision.reconciliation_id,
                ReconciliationRun.invoice_version_id == decision.version_id,
            )
        )
        if (
            decision.version_id != invoice.current_version_id
            or decision.reconciliation_id != invoice.current_reconciliation_id
            or reconciliation is None
            or decision.expected_snapshot_hash != reconciliation.snapshot_hash
        ):
            raise DomainError(
                "STALE_WORKFLOW_DECISION", "Review decision no longer matches the current snapshot.", 409
            )
        if state.get("version_id") != str(decision.version_id):
            raise DomainError(
                "STALE_WORKFLOW_VERSION", "Graph run does not match the approved invoice version.", 409
            )
        if state.get("reconciliation_id") != str(decision.reconciliation_id):
            raise DomainError(
                "STALE_WORKFLOW_RECONCILIATION", "Graph run does not match the reviewed reconciliation.", 409
            )
        expected_status = "APPROVED" if decision.action == "APPROVE" else "REJECTED"
        if invoice.workflow_status != expected_status:
            raise DomainError(
                "WORKFLOW_DECISION_MISMATCH", "Invoice state does not match its review decision.", 409
            )
        job.status = "DONE"
        job.current_step = "FINALIZED"
        job.lease_until = None
        return {"job_status": "DONE", "phase": "FINALIZED"}


def build_invoice_graph(checkpointer: PostgresSaver):
    graph = StateGraph(InvoiceWorkflowState)
    graph.add_node("load_snapshot", _load_snapshot)
    graph.add_node("prepare_pages", _prepare_pages)
    graph.add_node("extract", _extract)
    graph.add_node("validate", _validate_snapshot)
    graph.add_node("reconcile", _reconcile)
    graph.add_node("await_review", _await_review)
    graph.add_node("finalize_decision", _finalize_decision)
    graph.add_edge(START, "load_snapshot")
    graph.add_edge("load_snapshot", "prepare_pages")
    graph.add_edge("prepare_pages", "extract")
    graph.add_conditional_edges(
        "extract",
        lambda state: "validate" if state.get("job_status") == "WAITING_REVIEW" else END,
        {"validate": "validate", END: END},
    )
    graph.add_edge("validate", "reconcile")
    graph.add_edge("reconcile", "await_review")
    graph.add_edge("await_review", "finalize_decision")
    graph.add_edge("finalize_decision", END)
    return graph.compile(checkpointer=checkpointer)


def _validate_checkpoint_binding(
    values: dict[str, Any],
    *,
    job_id: uuid.UUID,
    org_id: uuid.UUID,
    invoice_id: uuid.UUID,
    require_snapshot_identity: bool,
) -> None:
    expected = {
        "job_id": str(job_id),
        "org_id": str(org_id),
        "invoice_id": str(invoice_id),
    }
    mismatch = values.get("job_id") != expected["job_id"] or any(
        key in values and values[key] != value for key, value in expected.items() if key != "job_id"
    )
    missing_snapshot_identity = require_snapshot_identity and any(
        values.get(key) != expected[key] for key in ("org_id", "invoice_id")
    )
    if mismatch or missing_snapshot_identity:
        raise DomainError(
            "WORKFLOW_CHECKPOINT_MISMATCH",
            "Saved workflow state does not belong to this job and tenant.",
            409,
        )


def _load_resume_snapshot(
    *, org_id: uuid.UUID, invoice_id: uuid.UUID, decision_id: uuid.UUID
) -> tuple[str, str]:
    with SessionLocal() as db:
        invoice = db.scalar(select(Invoice).where(Invoice.org_id == org_id, Invoice.id == invoice_id))
        if invoice is None:
            raise DomainError("WORKFLOW_SNAPSHOT_MISSING", "Invoice snapshot is missing.", 404)
        decision = db.scalar(
            select(ReviewDecision).where(
                ReviewDecision.org_id == org_id,
                ReviewDecision.invoice_id == invoice_id,
                ReviewDecision.id == decision_id,
            )
        )
        if decision is None:
            raise DomainError(
                "WORKFLOW_DECISION_MISSING", "Persisted review decision is missing.", 409
            )
        reconciliation = db.scalar(
            select(ReconciliationRun).where(
                ReconciliationRun.org_id == org_id,
                ReconciliationRun.invoice_id == invoice_id,
                ReconciliationRun.id == decision.reconciliation_id,
                ReconciliationRun.invoice_version_id == decision.version_id,
            )
        )
        if (
            decision.version_id != invoice.current_version_id
            or decision.reconciliation_id != invoice.current_reconciliation_id
            or reconciliation is None
            or decision.expected_snapshot_hash != reconciliation.snapshot_hash
        ):
            raise DomainError(
                "STALE_WORKFLOW_DECISION",
                "Review decision does not match the current immutable invoice snapshot.",
                409,
            )
        expected_status = "APPROVED" if decision.action == "APPROVE" else "REJECTED"
        if invoice.workflow_status != expected_status:
            raise DomainError(
                "WORKFLOW_DECISION_MISMATCH", "Invoice state does not match its review decision.", 409
            )
        return str(invoice.current_version_id), str(invoice.current_reconciliation_id)


def _load_current_snapshot(*, org_id: uuid.UUID, invoice_id: uuid.UUID) -> tuple[str | None, str | None]:
    with SessionLocal() as db:
        invoice = db.scalar(select(Invoice).where(Invoice.org_id == org_id, Invoice.id == invoice_id))
        if invoice is None:
            raise DomainError("WORKFLOW_SNAPSHOT_MISSING", "Invoice snapshot is missing.", 404)
        version_id = str(invoice.current_version_id) if invoice.current_version_id else None
        reconciliation_id = (
            str(invoice.current_reconciliation_id) if invoice.current_reconciliation_id else None
        )
        return version_id, reconciliation_id


def run_invoice_graph(job_id: str, decision_id: str | None = None) -> dict[str, Any] | None:
    job_uuid = _uuid(job_id, "job id")
    with workflow_run_lock(str(job_uuid)) as acquired:
        if not acquired:
            return {"status": "DUPLICATE_DELIVERY", "job_id": str(job_uuid)}
        with SessionLocal() as db:
            job = db.scalar(select(Job).where(Job.id == job_uuid))
            if job is None:
                raise DomainError("NOT_FOUND", "Workflow job does not exist.", 404)
            if job.status in {"DONE", "FAILED"}:
                return None
            org_id = job.org_id
            invoice_id = job.entity_id
            job_status = job.status
            thread_id = str(job.workflow_thread_id)

        config = {"configurable": {"thread_id": thread_id}}
        with postgres_checkpointer() as checkpointer:
            graph = build_invoice_graph(checkpointer)
            current = graph.get_state(config)
            current_values = dict(current.values) if current and current.values else {}
            if current_values:
                _validate_checkpoint_binding(
                    current_values,
                    job_id=job_uuid,
                    org_id=org_id,
                    invoice_id=invoice_id,
                    require_snapshot_identity=not current or current.next != ("load_snapshot",),
                )

            if decision_id:
                current_version_id, current_reconciliation_id = _load_resume_snapshot(
                    org_id=org_id,
                    invoice_id=invoice_id,
                    decision_id=_uuid(decision_id, "decision id"),
                )
            else:
                current_version_id, current_reconciliation_id = _load_current_snapshot(
                    org_id=org_id, invoice_id=invoice_id
                )

            checkpoint_version_id = current_values.get("version_id")
            checkpoint_reconciliation_id = current_values.get("reconciliation_id")
            snapshot_changed = current and current.next and job_status == "WAITING_REVIEW" and (
                checkpoint_version_id != current_version_id
                or checkpoint_reconciliation_id != current_reconciliation_id
            )
            if snapshot_changed:
                # Immutable version/reconciliation changes use a fresh thread and re-read DB state.
                thread_id = str(uuid.uuid4())
                with SessionLocal.begin() as db:
                    locked_job = db.scalar(select(Job).where(Job.id == job_uuid).with_for_update())
                    if locked_job is None or locked_job.status != "WAITING_REVIEW":
                        return None
                    if str(locked_job.workflow_thread_id) != config["configurable"]["thread_id"]:
                        raise DomainError(
                            "WORKFLOW_THREAD_MISMATCH", "Workflow thread changed during resume.", 409
                        )
                    locked_job.workflow_thread_id = uuid.UUID(thread_id)
                config = {"configurable": {"thread_id": thread_id}}
                current = graph.get_state(config)
                current_values = {}

            if decision_id and current and current.next and current.next == ("await_review",):
                result = graph.invoke(
                    Command(
                        update={"reconciliation_id": current_reconciliation_id},
                        resume={"decision_id": decision_id},
                    ),
                    config=config,
                )
            elif current and current.next:
                if not decision_id and current.next == ("await_review",):
                    return None
                # Continue a durable checkpoint after a process restart. The review node reads only
                # the stored domain decision; extraction output never supplies a decision.
                result = graph.invoke(None, config=config)
            else:
                result = graph.invoke({"job_id": str(job_uuid)}, config=config)
        return result
