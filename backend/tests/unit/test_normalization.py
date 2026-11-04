from app.domain.normalization import (
    NormalizedValue,
    normalize_date,
    normalize_invoice_candidate,
    normalize_money,
)


def test_vi_vn_money_grouping_decimal_comma_and_whitespace() -> None:
    assert normalize_money("1.234.567,89", "vi-VN") == NormalizedValue("1234567.89")
    assert normalize_money(" 001.234,50 ", "vi-VN") == NormalizedValue("1234.50")
    assert normalize_money("1 234 567,89", "vi-VN") == NormalizedValue("1234567.89")
    assert normalize_money("1.234", "vi-VN") == NormalizedValue("1234")


def test_ambiguous_invalid_and_overflow_money_values_are_not_guessed() -> None:
    assert normalize_money("1234.56", "vi-VN") == NormalizedValue(None, "AMBIGUOUS_NUMBER")
    assert normalize_money("1,234.56", "vi-VN").issue in {"AMBIGUOUS_NUMBER", "INVALID_NUMBER"}
    assert normalize_money("12,34,56", "vi-VN") == NormalizedValue(None, "INVALID_NUMBER")
    assert normalize_money("100.000.000.000.000", "vi-VN") == NormalizedValue(None, "NUMERIC_OVERFLOW")
    assert normalize_money("1.2345678", "vi-VN").issue == "AMBIGUOUS_NUMBER"
    assert normalize_money("1.234,5678901", "vi-VN") == NormalizedValue(None, "NUMERIC_OVERFLOW")


def test_date_normalization_accepts_iso_and_unambiguous_vietnamese_day_first() -> None:
    assert normalize_date("2026-12-25", "vi-VN") == NormalizedValue("2026-12-25")
    assert normalize_date("25/12/2026", "vi-VN") == NormalizedValue("2026-12-25")
    assert normalize_date("13.01.2026", "vi-VN") == NormalizedValue("2026-01-13")
    assert normalize_date("01/02/2026", "vi-VN") == NormalizedValue(None, "AMBIGUOUS_DATE")
    assert normalize_date("31/02/2026", "vi-VN") == NormalizedValue(None, "INVALID_DATE")
    assert normalize_date("2026-02-30", "vi-VN") == NormalizedValue(None, "INVALID_DATE")


def test_invoice_candidate_keeps_raw_separate_and_missing_distinct_from_zero() -> None:
    normalized = normalize_invoice_candidate(
        {
            "invoice_date": " 01/02/2026 ",
            "subtotal": " 1.234.567,89 ",
            "tax_total": None,
            "grand_total": "0",
            "lines": [{"quantity": "2", "unit_price": "1,234", "net_amount": "2,468"}],
        },
        "vi-VN",
    )

    assert normalized.raw_values["subtotal"] == " 1.234.567,89 "
    assert normalized.normalized_values["subtotal"] == "1234567.89"
    assert normalized.data["invoice_date"] is None
    assert normalized.issues == {"invoice_date": "AMBIGUOUS_DATE"}
    assert "tax_total" not in normalized.raw_values
    assert "tax_total" not in normalized.normalized_values
    assert normalized.raw_values["grand_total"] == "0"
    assert normalized.normalized_values["grand_total"] == "0"
    assert normalized.issues["invoice_date"] == "AMBIGUOUS_DATE"


def test_invalid_money_keeps_raw_and_sets_normalized_to_null_with_issue() -> None:
    normalized = normalize_invoice_candidate(
        {"subtotal": "1234.56", "grand_total": "0"}, "vi-VN"
    )

    assert normalized.data["subtotal"] is None
    assert normalized.raw_values["subtotal"] == "1234.56"
    assert "subtotal" not in normalized.normalized_values
    assert normalized.issues["subtotal"] == "AMBIGUOUS_NUMBER"


def test_unknown_locale_does_not_silently_use_vietnamese_rules() -> None:
    assert normalize_money("1.234.567,89", "en-US") == NormalizedValue(None, "UNSUPPORTED_LOCALE")
    assert normalize_date("25/12/2026", "en-US") == NormalizedValue(None, "UNSUPPORTED_LOCALE")
