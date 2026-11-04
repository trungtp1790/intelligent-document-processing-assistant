from __future__ import annotations

import math
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    org_slug: str = Field(min_length=1, max_length=64)


class PolicyQueryRequest(StrictModel):
    question: str = Field(min_length=3, max_length=500)

    @field_validator("question")
    @classmethod
    def non_blank_question(cls, value: str) -> str:
        question = value.strip()
        if len(question) < 3:
            raise ValueError("Question must contain at least three non-whitespace characters")
        if any(ord(character) < 32 and character not in "\t\n\r" for character in question):
            raise ValueError("Question cannot contain control characters")
        return question


class CopilotMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def non_blank_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("Message cannot be blank")
        if any(ord(character) < 32 and character not in "\t\n\r" for character in content):
            raise ValueError("Message cannot contain control characters")
        return content


class CopilotRequest(StrictModel):
    question: str = Field(min_length=3, max_length=1000)
    history: list[CopilotMessage] = Field(default_factory=list, max_length=8)

    @field_validator("question")
    @classmethod
    def non_blank_question(cls, value: str) -> str:
        question = value.strip()
        if len(question) < 3:
            raise ValueError("Question must contain at least three non-whitespace characters")
        if any(ord(character) < 32 and character not in "\t\n\r" for character in question):
            raise ValueError("Question cannot contain control characters")
        return question

    @model_validator(mode="after")
    def bounded_history(self) -> CopilotRequest:
        if sum(len(item.content) for item in self.history) > 6000:
            raise ValueError("Conversation history is too large")
        return self


class TokenResponse(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user_id: UUID
    org_id: UUID
    org_name: str
    org_slug: str
    role: Literal["operator", "reviewer"]


class SupplierCreate(StrictModel):
    tax_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)


class POLineCreate(StrictModel):
    sku: str | None = Field(default=None, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    unit: str = Field(min_length=1, max_length=32)
    quantity: str
    unit_price: str

    @field_validator("quantity", "unit_price")
    @classmethod
    def decimal_string(cls, value: str) -> str:
        from app.domain.money import fits_numeric_20_6, parse_decimal

        parsed = parse_decimal(value)
        if parsed is None:
            raise ValueError("Must be a decimal string")
        if not fits_numeric_20_6(parsed):
            raise ValueError("Maximum precision is NUMERIC(20,6)")
        return format(parsed, "f")


class POCreate(StrictModel):
    po_number: str = Field(min_length=1, max_length=80)
    supplier_tax_id: str = Field(min_length=1, max_length=64)
    currency: str = Field(default="VND", min_length=3, max_length=3)
    state: Literal["OPEN", "CLOSED"] = "OPEN"
    lines: list[POLineCreate] = Field(min_length=1, max_length=200)


class InvoiceLineData(StrictModel):
    line_id: str | None = None
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    unit: str | None = Field(default=None, max_length=32)
    quantity: str | None = None
    unit_price: str | None = None
    net_amount: str | None = None

    @field_validator("quantity", "unit_price", "net_amount")
    @classmethod
    def optional_decimal_string(cls, value: str | None) -> str | None:
        if value is None:
            return value
        from app.domain.money import fits_numeric_20_6, parse_decimal

        parsed = parse_decimal(value)
        if parsed is None:
            raise ValueError("Must be a decimal string")
        if not fits_numeric_20_6(parsed):
            raise ValueError("Maximum precision is NUMERIC(20,6)")
        return format(parsed, "f")


class InvoiceData(StrictModel):
    schema_version: str = "1.0"
    invoice_number: str | None = Field(default=None, max_length=100)
    invoice_series: str | None = Field(default=None, max_length=100)
    invoice_series_absent_confirmed: bool = False
    invoice_date: str | None = Field(default=None, max_length=10)
    supplier_tax_id: str | None = Field(default=None, max_length=64)
    supplier_name: str | None = Field(default=None, max_length=200)
    po_number: str | None = Field(default=None, max_length=80)
    currency: str | None = Field(default=None, max_length=3)
    subtotal: str | None = None
    tax_total: str | None = None
    grand_total: str | None = None
    discount_total: str | None = None
    shipping_total: str | None = None
    lines: list[InvoiceLineData] = Field(default_factory=list, max_length=200)

    @field_validator("invoice_date")
    @classmethod
    def iso_date_only(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        if len(value) != 10 or value[4] != "-" or value[7] != "-":
            raise ValueError("Date must use ISO YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Date must be a valid ISO YYYY-MM-DD value") from exc
        return value

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @field_validator("subtotal", "tax_total", "grand_total", "discount_total", "shipping_total")
    @classmethod
    def optional_money_string(cls, value: str | None) -> str | None:
        if value is None:
            return value
        from app.domain.money import fits_numeric_20_6, parse_decimal

        parsed = parse_decimal(value)
        if parsed is None:
            raise ValueError("Must be a decimal string")
        if not fits_numeric_20_6(parsed):
            raise ValueError("Maximum precision is NUMERIC(20,6)")
        return format(parsed, "f")


class _ProviderCandidateModel(BaseModel):
    """Strict provider wire shape that preserves source strings before normalization."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class ProviderInvoiceLineCandidate(_ProviderCandidateModel):
    line_id: StrictStr | None = None
    sku: StrictStr | None = Field(default=None, max_length=100)
    description: StrictStr | None = Field(default=None, max_length=500)
    unit: StrictStr | None = Field(default=None, max_length=32)
    quantity: StrictStr | None = Field(default=None, max_length=64)
    unit_price: StrictStr | None = Field(default=None, max_length=64)
    net_amount: StrictStr | None = Field(default=None, max_length=64)


class ProviderInvoiceCandidate(_ProviderCandidateModel):
    """Untrusted extraction output; amounts and dates remain raw until locale normalization."""

    invoice_number: StrictStr | None = Field(default=None, max_length=100)
    invoice_series: StrictStr | None = Field(default=None, max_length=100)
    invoice_series_absent_confirmed: StrictBool = False
    invoice_date: StrictStr | None = Field(default=None, max_length=64)
    supplier_tax_id: StrictStr | None = Field(default=None, max_length=64)
    supplier_name: StrictStr | None = Field(default=None, max_length=200)
    po_number: StrictStr | None = Field(default=None, max_length=80)
    currency: StrictStr | None = Field(default=None, max_length=3)
    subtotal: StrictStr | None = Field(default=None, max_length=64)
    tax_total: StrictStr | None = Field(default=None, max_length=64)
    grand_total: StrictStr | None = Field(default=None, max_length=64)
    discount_total: StrictStr | None = Field(default=None, max_length=64)
    shipping_total: StrictStr | None = Field(default=None, max_length=64)
    lines: list[ProviderInvoiceLineCandidate] = Field(default_factory=list, max_length=200)


class EvidenceResponse(StrictModel):
    field_path: str
    page_no: int | None = None
    raw_value: str | None = None
    normalized_value: str | None = None
    raw_text: str | None = None
    bbox: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    normalization_issue: str | None = None
    origin: str
    verification_state: str
    actor_id: UUID | None = None
    note: str | None = None

    @field_validator("confidence")
    @classmethod
    def finite_confidence(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Confidence must be finite")
        return value


class VersionCreate(StrictModel):
    expected_version_id: UUID
    reason: str = Field(min_length=3, max_length=500)
    manual_source_page: int | None = Field(default=None, ge=1, le=10)
    manual_evidence_pages: dict[str, int] = Field(default_factory=dict, max_length=200)
    draft: InvoiceData


class ReconciliationCreate(StrictModel):
    expected_version_id: UUID
    po_id: UUID
    line_mapping: dict[str, str] = Field(default_factory=dict)


class DecisionCreate(StrictModel):
    action: Literal["APPROVE", "REJECT"]
    expected_version_id: UUID
    reconciliation_id: UUID
    expected_snapshot_hash: str = Field(min_length=64, max_length=64)
    acknowledged_warning_codes: list[str] = Field(default_factory=list, max_length=30)
    note: str | None = Field(default=None, max_length=1000)


class SupplierResponse(StrictModel):
    id: UUID
    tax_id: str
    name: str


class POResponse(StrictModel):
    id: UUID
    po_number: str
    supplier_id: UUID
    supplier_tax_id: str
    supplier_name: str
    currency: str
    state: str
    lines: list[dict]
