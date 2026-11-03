from __future__ import annotations

import csv
import hashlib
import io
import uuid
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PurchaseOrder, PurchaseOrderLine, Supplier
from app.domain.errors import DomainError
from app.schemas import POCreate, SupplierCreate
from app.security import Principal
from app.services.common import IdempotentReplay, audit, begin_idempotent, finish_idempotent


def supplier_response(supplier: Supplier) -> dict[str, Any]:
    return {"id": str(supplier.id), "tax_id": supplier.tax_id_normalized, "name": supplier.name}


def create_supplier(db: Session, actor: Principal, body: SupplierCreate, key: str | None) -> dict[str, Any]:
    command = body.model_dump(mode="json")
    result = begin_idempotent(db, actor, "POST /suppliers", key, command)
    if isinstance(result, IdempotentReplay):
        return result.response
    tax_id = body.tax_id.strip().upper()
    found = db.scalar(
        select(Supplier).where(Supplier.org_id == actor.org_id, Supplier.tax_id_normalized == tax_id)
    )
    if found:
        raise DomainError("SUPPLIER_EXISTS", "Mã số thuế này đã tồn tại trong tổ chức.", 409)
    supplier = Supplier(org_id=actor.org_id, tax_id_normalized=tax_id, name=body.name.strip())
    db.add(supplier)
    db.flush()
    response = supplier_response(supplier)
    finish_idempotent(db, result, supplier.id, response)
    audit(db, actor, actor.org_id, "supplier", supplier.id, "SUPPLIER_CREATED")
    db.commit()
    return response


def get_suppliers(db: Session, org_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        supplier_response(row)
        for row in db.scalars(select(Supplier).where(Supplier.org_id == org_id).order_by(Supplier.name))
    ]


def po_response(db: Session, po: PurchaseOrder) -> dict[str, Any]:
    supplier = db.scalar(select(Supplier).where(Supplier.org_id == po.org_id, Supplier.id == po.supplier_id))
    lines = db.scalars(
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.org_id == po.org_id, PurchaseOrderLine.po_id == po.id)
        .order_by(PurchaseOrderLine.line_no)
    ).all()
    return {
        "id": str(po.id),
        "po_number": po.po_number,
        "supplier_id": str(po.supplier_id),
        "supplier_tax_id": supplier.tax_id_normalized if supplier else "",
        "supplier_name": supplier.name if supplier else "",
        "currency": po.currency,
        "state": po.state,
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
            for line in lines
        ],
    }


def create_purchase_order(db: Session, actor: Principal, body: POCreate, key: str | None) -> dict[str, Any]:
    command = body.model_dump(mode="json")
    result = begin_idempotent(db, actor, "POST /purchase-orders", key, command)
    if isinstance(result, IdempotentReplay):
        return result.response
    supplier = db.scalar(
        select(Supplier).where(
            Supplier.org_id == actor.org_id,
            Supplier.tax_id_normalized == body.supplier_tax_id.strip().upper(),
        )
    )
    if supplier is None:
        raise DomainError("SUPPLIER_NOT_FOUND", "Hãy tạo nhà cung cấp trước khi tạo PO.", 404)
    if db.scalar(
        select(PurchaseOrder.id).where(
            PurchaseOrder.org_id == actor.org_id, PurchaseOrder.po_number == body.po_number.strip()
        )
    ):
        raise DomainError("PO_NUMBER_EXISTS", "Mã PO đã tồn tại trong tổ chức.", 409)
    po = PurchaseOrder(
        org_id=actor.org_id,
        supplier_id=supplier.id,
        po_number=body.po_number.strip(),
        currency=body.currency.upper(),
        state=body.state,
    )
    db.add(po)
    db.flush()
    for index, line in enumerate(body.lines, start=1):
        quantity, unit_price = Decimal(line.quantity), Decimal(line.unit_price)
        if quantity <= 0 or unit_price < 0:
            raise DomainError("INVALID_PO_LINE", "Số lượng phải dương và đơn giá không âm.", 422)
        db.add(
            PurchaseOrderLine(
                org_id=actor.org_id,
                po_id=po.id,
                line_no=index,
                sku=line.sku,
                description=line.description,
                unit=line.unit,
                quantity=quantity,
                unit_price=unit_price,
            )
        )
    db.flush()
    response = po_response(db, po)
    finish_idempotent(db, result, po.id, response)
    audit(db, actor, actor.org_id, "purchase_order", po.id, "PURCHASE_ORDER_CREATED")
    db.commit()
    return response


def get_purchase_orders(db: Session, org_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(PurchaseOrder).where(PurchaseOrder.org_id == org_id).order_by(PurchaseOrder.created_at.desc())
    ).all()
    return [po_response(db, row) for row in rows]


def get_purchase_order(db: Session, org_id: uuid.UUID, po_id: uuid.UUID) -> dict[str, Any]:
    po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.org_id == org_id, PurchaseOrder.id == po_id))
    if po is None:
        raise DomainError("NOT_FOUND", "Không tìm thấy PO.", 404)
    return po_response(db, po)


def import_purchase_orders_csv(
    db: Session, actor: Principal, content: bytes, filename: str | None, key: str | None
) -> dict[str, Any]:
    from app.schemas import POLineCreate
    from app.services.common import IdempotentReplay, audit, begin_idempotent, finish_idempotent

    digest = hashlib.sha256(content).hexdigest()
    claim = begin_idempotent(
        db,
        actor,
        "POST /purchase-orders/import",
        key,
        {"sha256": digest, "filename": (filename or "purchase-orders.csv")[:255]},
    )
    if isinstance(claim, IdempotentReplay):
        return claim.response
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainError("CSV_ENCODING_INVALID", "CSV phải được mã hóa UTF-8.", 422) from exc
    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    expected = [
        "po_number",
        "supplier_tax_id",
        "currency",
        "line_no",
        "sku",
        "description",
        "unit",
        "quantity",
        "unit_price",
    ]
    if reader.fieldnames != expected:
        raise DomainError(
            "CSV_SCHEMA_INVALID",
            "Header CSV không khớp schema PO.",
            422,
            {"expected_headers": expected, "actual_headers": reader.fieldnames},
        )
    groups: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    rows_seen = 0
    for row_number, raw in enumerate(reader, start=2):
        rows_seen += 1
        row = {str(key): (value or "").strip() for key, value in raw.items() if key is not None}
        try:
            po_number = row["po_number"]
            supplier_tax_id = row["supplier_tax_id"].upper()
            currency = row["currency"].upper()
            if not po_number or not supplier_tax_id or not currency:
                raise ValueError("PO number, supplier tax ID and currency are required")
            line_no = int(row["line_no"])
            if line_no <= 0:
                raise ValueError("line_no must be positive")
            line = POLineCreate.model_validate(
                {
                    "sku": row["sku"] or None,
                    "description": row["description"],
                    "unit": row["unit"],
                    "quantity": row["quantity"],
                    "unit_price": row["unit_price"],
                }
            )
            group = groups.setdefault(
                po_number,
                {
                    "supplier_tax_id": supplier_tax_id,
                    "currency": currency,
                    "lines": [],
                },
            )
            if group["supplier_tax_id"] != supplier_tax_id or group["currency"] != currency:
                raise ValueError("Repeated PO header values must be identical")
            if any(existing_no == line_no for existing_no, _ in group["lines"]):
                raise ValueError("line_no is duplicated for this PO")
            group["lines"].append((line_no, line))
        except (KeyError, ValueError, ValidationError) as exc:
            errors.append({"row": row_number, "message": str(exc)})
    if not rows_seen:
        errors.append({"row": 2, "message": "CSV contains no PO lines"})
    if errors:
        raise DomainError(
            "CSV_IMPORT_INVALID",
            "Không có dữ liệu nào được nhập; hãy sửa các dòng lỗi.",
            422,
            {"rows": errors},
        )
    existing = set(
        db.scalars(
            select(PurchaseOrder.po_number).where(
                PurchaseOrder.org_id == actor.org_id,
                PurchaseOrder.po_number.in_(list(groups)),
            )
        )
    )
    if existing:
        raise DomainError(
            "PO_NUMBER_EXISTS",
            "Không có dữ liệu nào được nhập; mã PO đã tồn tại.",
            409,
            {"po_numbers": sorted(existing)},
        )
    tax_ids = {group["supplier_tax_id"] for group in groups.values()}
    suppliers = {
        supplier.tax_id_normalized: supplier
        for supplier in db.scalars(
            select(Supplier).where(Supplier.org_id == actor.org_id, Supplier.tax_id_normalized.in_(tax_ids))
        )
    }
    missing_suppliers = sorted(tax_id for tax_id in tax_ids if tax_id not in suppliers)
    if missing_suppliers:
        raise DomainError(
            "SUPPLIER_NOT_FOUND",
            "Không có dữ liệu nào được nhập; hãy tạo nhà cung cấp trước.",
            422,
            {"supplier_tax_ids": missing_suppliers},
        )
    created: list[PurchaseOrder] = []
    for po_number, group in groups.items():
        po = PurchaseOrder(
            org_id=actor.org_id,
            supplier_id=suppliers[group["supplier_tax_id"]].id,
            po_number=po_number,
            currency=group["currency"],
            state="OPEN",
        )
        db.add(po)
        db.flush()
        for line_no, line in sorted(group["lines"], key=lambda item: item[0]):
            quantity, unit_price = Decimal(line.quantity), Decimal(line.unit_price)
            if quantity <= 0 or unit_price < 0:
                raise DomainError("INVALID_PO_LINE", "Số lượng phải dương và đơn giá không âm.", 422)
            db.add(
                PurchaseOrderLine(
                    org_id=actor.org_id,
                    po_id=po.id,
                    line_no=line_no,
                    sku=line.sku,
                    description=line.description,
                    unit=line.unit,
                    quantity=quantity,
                    unit_price=unit_price,
                )
            )
        created.append(po)
    db.flush()
    responses = [po_response(db, po) for po in created]
    response = {"imported_count": len(responses), "purchase_orders": responses}
    finish_idempotent(db, claim, created[0].id, response)
    for po in created:
        audit(
            db,
            actor,
            actor.org_id,
            "purchase_order",
            po.id,
            "PURCHASE_ORDER_IMPORTED",
            metadata={"source": "csv", "line_count": len(groups[po.po_number]["lines"])},
        )
    db.commit()
    return response
