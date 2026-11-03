from app.domain.reconciliation import reconcile


def fixture_inputs(quantity: str = "10") -> tuple[dict, dict, dict]:
    invoice = {
        "invoice_number": "000123",
        "invoice_date": "2026-10-01",
        "po_number": "PO-1",
        "supplier_tax_id": "SUP-1",
        "currency": "VND",
        "subtotal": "2000000",
        "tax_total": "200000",
        "grand_total": "2200000",
        "discount_total": "0",
        "shipping_total": "0",
        "lines": [
            {
                "line_id": "i1",
                "sku": "KB-1",
                "unit": "piece",
                "quantity": quantity,
                "unit_price": "200000",
                "net_amount": str(int(quantity) * 200000),
            }
        ],
    }
    po = {
        "po_number": "PO-1",
        "currency": "VND",
        "state": "OPEN",
        "supplier_tax_id": "SUP-1",
        "lines": [
            {
                "id": "p1",
                "sku": "KB-1",
                "unit": "piece",
                "quantity": "10",
                "unit_price": "200000",
            }
        ],
    }
    return invoice, po, {"i1": "p1"}


def test_exact_full_match_passes() -> None:
    invoice, po, mapping = fixture_inputs()
    result = reconcile(invoice, po, mapping)
    assert result["status"] == "PASS"
    assert result["issues"] == []


def test_quantity_mismatch_is_blocking() -> None:
    invoice, po, mapping = fixture_inputs("12")
    result = reconcile(invoice, po, mapping)
    assert result["status"] == "BLOCKED"
    assert "QUANTITY_MISMATCH" in {i["code"] for i in result["issues"]}


def test_missing_total_is_not_coerced_to_zero() -> None:
    invoice, po, mapping = fixture_inputs()
    invoice["tax_total"] = None
    result = reconcile(invoice, po, mapping)
    assert result["status"] == "BLOCKED"
    assert {"code": "REQUIRED_FIELD_MISSING", "severity": "BLOCK", "field_path": "tax_total"} in result[
        "issues"
    ]


def test_scan_evidence_is_warning_and_requires_human_ack() -> None:
    invoice, po, mapping = fixture_inputs()
    result = reconcile(invoice, po, mapping, has_unverified_source=True)
    assert result["status"] == "WARNING"
    assert result["issues"] == [{"code": "SOURCE_NOT_TEXT_VERIFIED", "severity": "WARN"}]
