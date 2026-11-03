from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.domain.money import parse_decimal, round_vnd

POLICY_VERSION = "reconciliation-v1"
AMOUNT_TOLERANCE = Decimal("1")
QUANTITY_TOLERANCE = Decimal("0.000001")
PRICE_TOLERANCE = Decimal("0.000001")


def reconcile(
    invoice: dict[str, Any],
    purchase_order: dict[str, Any],
    mapping: dict[str, str],
    *,
    duplicate_approved: bool = False,
    duplicate_pending: bool = False,
    has_unverified_source: bool = False,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []

    def issue(code: str, severity: str = "BLOCK", field_path: str | None = None) -> None:
        item = {"code": code, "severity": severity}
        if field_path:
            item["field_path"] = field_path
        issues.append(item)

    required = (
        "invoice_number",
        "invoice_date",
        "supplier_tax_id",
        "currency",
        "subtotal",
        "tax_total",
        "grand_total",
    )
    for field in required:
        if invoice.get(field) is None or invoice.get(field) == "":
            issue("REQUIRED_FIELD_MISSING", field_path=field)

    if invoice.get("invoice_date"):
        try:
            date.fromisoformat(invoice["invoice_date"])
        except (TypeError, ValueError):
            issue("AMBIGUOUS_DATE", field_path="invoice_date")

    currency = invoice.get("currency")
    if currency and currency != "VND":
        issue("UNSUPPORTED_CASE", field_path="currency")
    if purchase_order.get("currency") != "VND":
        issue("UNSUPPORTED_CASE", field_path="po_currency")
    if invoice.get("po_number") and invoice["po_number"].strip() != purchase_order["po_number"].strip():
        issue("PO_NOT_FOUND", field_path="po_number")
    for field in ("discount_total", "shipping_total"):
        value = parse_decimal(invoice.get(field))
        if value is None:
            issue("REQUIRED_FIELD_MISSING", field_path=field)
        elif value != 0:
            issue("UNSUPPORTED_CASE", field_path=field)

    if purchase_order["state"] != "OPEN":
        issue("PO_CLOSED", field_path="po_id")
    if (invoice.get("supplier_tax_id") or "").strip().casefold() != purchase_order[
        "supplier_tax_id"
    ].strip().casefold():
        issue("SUPPLIER_MISMATCH", field_path="supplier_tax_id")

    invoice_lines = invoice.get("lines", [])
    invoice_ids = {str(line.get("line_id")) for line in invoice_lines if line.get("line_id")}
    po_lines = purchase_order["lines"]
    po_ids = {str(line["id"]) for line in po_lines}
    valid_mapping = {str(k): str(v) for k, v in mapping.items() if str(k) in invoice_ids and str(v) in po_ids}
    if len(valid_mapping) != len(mapping) or len(set(valid_mapping.values())) != len(valid_mapping):
        issue("LINE_MAPPING_REQUIRED", field_path="lines")
    if len(valid_mapping) != len(invoice_lines) or len(valid_mapping) != len(po_lines):
        issue("LINE_MAPPING_REQUIRED", field_path="lines")

    po_by_id = {str(line["id"]): line for line in po_lines}
    total_line_net = Decimal("0")
    for idx, line in enumerate(invoice_lines):
        path = f"lines[{idx}]"
        qty, price, net = (parse_decimal(line.get(k)) for k in ("quantity", "unit_price", "net_amount"))
        if qty is None or price is None or net is None or not line.get("unit"):
            issue("REQUIRED_FIELD_MISSING", field_path=path)
            continue
        if qty <= 0 or price < 0 or net < 0:
            issue("UNSUPPORTED_CASE", field_path=path)
            continue
        total_line_net += net
        if round_vnd(qty * price) != round_vnd(net):
            issue("LINE_AMOUNT_MISMATCH", field_path=path)
        line_id = str(line.get("line_id"))
        po_line = po_by_id.get(valid_mapping.get(line_id, ""))
        if po_line is None:
            continue
        po_qty, po_price = Decimal(str(po_line["quantity"])), Decimal(str(po_line["unit_price"]))
        if line.get("sku") and po_line.get("sku") and line["sku"].casefold() != po_line["sku"].casefold():
            issue("DESCRIPTION_DIFFERENCE", "WARN", path)
        elif (
            line.get("description")
            and po_line.get("description")
            and line["description"].casefold() != po_line["description"].casefold()
        ):
            issue("DESCRIPTION_DIFFERENCE", "WARN", path)
        if line.get("unit", "").casefold() != po_line["unit"].casefold():
            issue("UNIT_MISMATCH", field_path=path)
        if abs(qty - po_qty) > QUANTITY_TOLERANCE:
            issue("QUANTITY_MISMATCH", field_path=path)
        if abs(price - po_price) > PRICE_TOLERANCE:
            issue("UNIT_PRICE_MISMATCH", field_path=path)

    subtotal = parse_decimal(invoice.get("subtotal"))
    tax = parse_decimal(invoice.get("tax_total"))
    grand = parse_decimal(invoice.get("grand_total"))
    if subtotal is not None and abs(total_line_net - subtotal) > AMOUNT_TOLERANCE:
        issue("SUBTOTAL_MISMATCH", field_path="subtotal")
    for field, amount in (("subtotal", subtotal), ("tax_total", tax), ("grand_total", grand)):
        if amount is not None and amount < 0:
            issue("UNSUPPORTED_CASE", field_path=field)
    if (
        subtotal is not None
        and tax is not None
        and grand is not None
        and abs(subtotal + tax - grand) > AMOUNT_TOLERANCE
    ):
        issue("GRAND_TOTAL_MISMATCH", field_path="grand_total")
    if duplicate_approved:
        issue("DUPLICATE_APPROVED_INVOICE", field_path="invoice_number")
    if duplicate_pending:
        issue("DUPLICATE_PENDING_INVOICE", "WARN", "invoice_number")
    if has_unverified_source:
        issue("SOURCE_NOT_TEXT_VERIFIED", "WARN")

    status = "BLOCKED" if any(x["severity"] == "BLOCK" for x in issues) else "WARNING" if issues else "PASS"
    return {"status": status, "issues": issues, "policy_version": POLICY_VERSION}
