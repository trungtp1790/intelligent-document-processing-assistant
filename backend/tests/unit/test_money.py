from decimal import Decimal

from app.domain.money import parse_decimal, round_vnd


def test_decimal_keeps_leading_zero_as_text_and_rejects_non_finite() -> None:
    assert parse_decimal("00123.450000") == Decimal("123.450000")
    assert parse_decimal("NaN") is None
    assert parse_decimal("not a number") is None
    assert parse_decimal(None) is None


def test_vnd_rounds_half_up_without_binary_float() -> None:
    assert round_vnd(Decimal("1.5")) == Decimal("2")
    assert round_vnd(Decimal("1.49")) == Decimal("1")
    assert round_vnd(Decimal("-1.5")) == Decimal("-2")
