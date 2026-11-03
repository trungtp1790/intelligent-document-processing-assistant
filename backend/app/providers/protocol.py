from __future__ import annotations

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
    evidence_pages: dict[str, int] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    strategy: str = "fixture_manifest"
    raw_output: str | None = None


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
