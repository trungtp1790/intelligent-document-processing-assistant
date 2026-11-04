from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.domain.money import decimal_text, fits_numeric_20_6
from app.schemas import InvoiceData, ProviderInvoiceCandidate

MONEY_FIELDS = ("subtotal", "tax_total", "grand_total", "discount_total", "shipping_total")
LINE_MONEY_FIELDS = ("quantity", "unit_price", "net_amount")


@dataclass(frozen=True)
class NormalizedValue:
    value: str | None
    issue: str | None = None


@dataclass(frozen=True)
class NormalizedInvoiceCandidate:
    data: dict[str, Any]
    raw_values: dict[str, str]
    normalized_values: dict[str, str]
    issues: dict[str, str]


def _split_grouped_integer(value: str, separator: str) -> str | None:
    groups = value.split(separator)
    if (
        len(groups) < 2
        or not 1 <= len(groups[0]) <= 3
        or not groups[0].isascii()
        or not groups[0].isdigit()
        or any(not group.isascii() or not group.isdigit() or len(group) != 3 for group in groups[1:])
    ):
        return None
    return "".join(groups)


def normalize_money(value: str | None, locale: str) -> NormalizedValue:
    """Parse an amount only when its notation is unambiguous for the declared locale."""
    if value is None or not value.strip():
        return NormalizedValue(None)
    if locale != "vi-VN":
        return NormalizedValue(None, "UNSUPPORTED_LOCALE")

    text = value.strip().replace("\u00a0", " ").replace("\u202f", " ")
    sign = ""
    if text[:1] in {"+", "-"}:
        sign, text = text[0], text[1:]
    if not text:
        return NormalizedValue(None, "INVALID_NUMBER")

    integer_part: str
    fraction_part: str | None = None
    if "," in text:
        if text.count(",") != 1:
            return NormalizedValue(None, "INVALID_NUMBER")
        integer_part, fraction_part = text.split(",", 1)
        if not fraction_part.isascii() or not fraction_part.isdigit():
            return NormalizedValue(None, "INVALID_NUMBER")
        if "." in integer_part and " " in integer_part:
            return NormalizedValue(None, "INVALID_NUMBER")
        if "." in integer_part:
            integer_digits = _split_grouped_integer(integer_part, ".")
        elif " " in integer_part:
            integer_digits = _split_grouped_integer(integer_part, " ")
        elif integer_part.isascii() and integer_part.isdigit():
            integer_digits = integer_part
        else:
            integer_digits = None
        if integer_digits is None:
            return NormalizedValue(None, "INVALID_NUMBER")
    elif "." in text:
        if text.count(".") == 1:
            integer_digits = _split_grouped_integer(text, ".")
            if integer_digits is None:
                left, right = text.split(".", 1)
                if left.isascii() and left.isdigit() and right.isascii() and right.isdigit():
                    return NormalizedValue(None, "AMBIGUOUS_NUMBER")
                return NormalizedValue(None, "INVALID_NUMBER")
        else:
            integer_digits = _split_grouped_integer(text, ".")
            if integer_digits is None:
                return NormalizedValue(None, "INVALID_NUMBER")
    elif " " in text:
        integer_digits = _split_grouped_integer(text, " ")
        if integer_digits is None:
            return NormalizedValue(None, "INVALID_NUMBER")
    elif text.isascii() and text.isdigit():
        integer_digits = text
    else:
        return NormalizedValue(None, "INVALID_NUMBER")

    assert integer_digits is not None
    decimal_text_value = f"{sign}{integer_digits}"
    if fraction_part is not None:
        decimal_text_value += f".{fraction_part}"
    try:
        parsed = Decimal(decimal_text_value)
    except Exception:
        return NormalizedValue(None, "INVALID_NUMBER")
    if not fits_numeric_20_6(parsed):
        return NormalizedValue(None, "NUMERIC_OVERFLOW")
    return NormalizedValue(decimal_text(parsed))


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VI_DATE = re.compile(r"^(\d{1,2})([/.-])(\d{1,2})\2(\d{4})$")


def normalize_date(value: str | None, locale: str) -> NormalizedValue:
    """Return an ISO date; reject day/month strings that have two plausible readings."""
    if value is None or not value.strip():
        return NormalizedValue(None)
    text = value.strip()
    if _ISO_DATE.fullmatch(text):
        try:
            return NormalizedValue(date.fromisoformat(text).isoformat())
        except ValueError:
            return NormalizedValue(None, "INVALID_DATE")
    if locale != "vi-VN":
        return NormalizedValue(None, "UNSUPPORTED_LOCALE")

    match = _VI_DATE.fullmatch(text)
    if match is None:
        issue = (
            "AMBIGUOUS_DATE"
            if re.fullmatch(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}", text)
            else "INVALID_DATE"
        )
        return NormalizedValue(None, issue)
    day, month, year = int(match.group(1)), int(match.group(3)), int(match.group(4))
    if day < 1 or day > 31 or month < 1 or month > 12:
        return NormalizedValue(None, "INVALID_DATE")
    if day <= 12:
        return NormalizedValue(None, "AMBIGUOUS_DATE")
    try:
        return NormalizedValue(date(year, month, day).isoformat())
    except ValueError:
        return NormalizedValue(None, "INVALID_DATE")


def _leaf_values(candidate: dict[str, Any]) -> dict[str, str]:
    values = {
        key: value
        for key, value in candidate.items()
        if key not in {"schema_version", "document_id", "lines", "invoice_series_absent_confirmed"}
        and value is not None
    }
    for index, line in enumerate(candidate.get("lines", [])):
        values.update(
            {
                f"lines[{index}].{key}": value
                for key, value in line.items()
                if key != "line_id" and value is not None
            }
        )
    return values


def normalize_invoice_candidate(candidate: dict[str, Any], locale: str) -> NormalizedInvoiceCandidate:
    """Validate provider structure, retain raw leaf strings, then normalize date and money fields."""
    raw = dict(candidate)
    raw.pop("schema_version", None)
    try:
        provider_candidate = ProviderInvoiceCandidate.model_validate(raw)
    except ValidationError:
        raise
    raw_data = provider_candidate.model_dump(mode="json")
    normalized_data = {
        **raw_data,
        "lines": [dict(line) for line in raw_data.get("lines", [])],
    }
    raw_values = _leaf_values(raw_data)
    issues: dict[str, str] = {}

    for field in MONEY_FIELDS:
        source_value = raw_data.get(field)
        if source_value is not None:
            parsed = normalize_money(source_value, locale)
            normalized_data[field] = parsed.value
            if parsed.issue:
                issues[field] = parsed.issue

    source_date = raw_data.get("invoice_date")
    if source_date is not None:
        parsed_date = normalize_date(source_date, locale)
        normalized_data["invoice_date"] = parsed_date.value
        if parsed_date.issue:
            issues["invoice_date"] = parsed_date.issue

    for index, line in enumerate(raw_data.get("lines", [])):
        for field in LINE_MONEY_FIELDS:
            source_value = line.get(field)
            if source_value is None:
                continue
            parsed = normalize_money(source_value, locale)
            normalized_data["lines"][index][field] = parsed.value
            if parsed.issue:
                issues[f"lines[{index}].{field}"] = parsed.issue

    try:
        canonical = InvoiceData.model_validate(normalized_data).model_dump(mode="json")
    except ValidationError:
        raise
    normalized_values = _leaf_values(canonical)
    return NormalizedInvoiceCandidate(
        data=canonical,
        raw_values=raw_values,
        normalized_values=normalized_values,
        issues=issues,
    )
