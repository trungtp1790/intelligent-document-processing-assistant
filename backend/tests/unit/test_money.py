from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.money import fits_numeric_20_6, parse_decimal, round_vnd
from app.schemas import InvoiceData, POCreate


def test_decimal_keeps_leading_zero_as_text_and_rejects_non_finite() -> None:
    assert parse_decimal("00123.450000") == Decimal("123.450000")
    assert parse_decimal("NaN") is None
    assert parse_decimal("not a number") is None
    assert parse_decimal(None) is None


def test_vnd_rounds_half_up_without_binary_float() -> None:
    assert round_vnd(Decimal("1.5")) == Decimal("2")
    assert round_vnd(Decimal("1.49")) == Decimal("1")
    assert round_vnd(Decimal("-1.5")) == Decimal("-2")


def test_numeric_20_6_uses_fourteen_integer_and_six_fraction_digits() -> None:
    assert fits_numeric_20_6(Decimal("99999999999999.999999"))
    assert fits_numeric_20_6(Decimal("0.000001"))
    assert fits_numeric_20_6(Decimal("0"))
    assert not fits_numeric_20_6(Decimal("100000000000000"))
    assert not fits_numeric_20_6(Decimal("0.0000001"))
    assert not fits_numeric_20_6(Decimal("NaN"))


def test_invoice_and_po_schema_reject_numeric_overflow_without_rounding() -> None:
    assert InvoiceData(subtotal="99999999999999.999999").subtotal == "99999999999999.999999"
    with pytest.raises(ValidationError):
        InvoiceData(subtotal="100000000000000")
    with pytest.raises(ValidationError):
        InvoiceData(lines=[{"quantity": "1.0000001"}])
    with pytest.raises(ValidationError):
        POCreate(
            po_number="PO-1",
            supplier_tax_id="SUP-1",
            lines=[{"description": "item", "unit": "ea", "quantity": "100000000000000", "unit_price": "1"}],
        )
