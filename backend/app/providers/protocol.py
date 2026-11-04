from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ExtractionPage:
    page_no: int
    text: str | None = None
    image_bytes: bytes | None = None
    image_format: str | None = None


@dataclass(frozen=True)
class ExtractionRequest:
    document_sha256: str
    document_id: str
    mime: str
    pages: tuple[ExtractionPage, ...]
    schema_version: str = "1.0"
    locale: str = "vi-VN"
    prompt_version: str = "invoice-v1"
    run_id: str | None = None
    routing_reason: str = ""
    deadline_seconds_remaining: float | None = None
    # Only the explicitly selected native-document provider receives the original upload.
    document_bytes: bytes | None = None


@dataclass(frozen=True)
class ProviderCapabilities:
    text_input: bool
    image_input: bool
    max_pages: int
    max_bytes: int
    output_strategy: str
    model_id: str | None


@dataclass(frozen=True)
class ExtractionResult:
    provider: str
    model_id: str | None
    candidate: dict[str, Any]
    evidence_texts: dict[str, str]
    raw_output_ref: str | None
    evidence_pages: dict[str, int | None] = field(default_factory=dict)
    evidence_bboxes: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_confidences: dict[str, float] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    strategy: str = "fixture_manifest"
    raw_output: str | None = None


def validate_confidence(value: Any) -> float | None:
    """Validate an actual provider score; this function never derives or estimates one."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Provider confidence must be a number")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("Provider confidence must be finite and between zero and one")
    return score


def validate_bounding_region(value: Any) -> dict[str, Any] | None:
    """Accept only a provider-supplied JSON object; coordinates are not inferred here."""
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError("Provider bounding region must be a non-empty object")
    try:
        json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Provider bounding region must contain finite JSON values") from exc
    return value


ReserveInvocation = Callable[[str, str], str]
RecordInvocation = Callable[..., None]


class ExtractionProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def extract_invoice(
        self,
        request: ExtractionRequest,
        *,
        reserve_invocation: ReserveInvocation | None = None,
        record_invocation: RecordInvocation | None = None,
    ) -> ExtractionResult: ...
