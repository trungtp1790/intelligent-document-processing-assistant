from __future__ import annotations

import io
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter

from app.providers.azure_document_intelligence import (
    AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
    AZURE_DOCUMENT_INTELLIGENCE_MODEL,
    AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION,
    AzureDocumentIntelligenceProvider,
)
from app.providers.errors import ProviderFailure
from app.providers.factory import configured_provider
from app.providers.protocol import ExtractionPage, ExtractionRequest
from app.services.extraction import _candidate_and_evidence, _make_request

_MISSING = object()
_REGION = {
    "pageNumber": 1,
    "polygon": [1.0, 2.0, 3.0, 2.0, 3.0, 4.0, 1.0, 4.0],
    "unit": "pixel",
}


def _field(
    content: Any,
    *,
    page_no: Any = _MISSING,
    bbox: bool = True,
    confidence: Any = 0.94,
) -> dict[str, Any]:
    field: dict[str, Any] = {"content": content}
    if confidence is not _MISSING:
        field["confidence"] = confidence
    if bbox:
        region = dict(_REGION)
        if page_no is _MISSING:
            region.pop("pageNumber")
        else:
            region["pageNumber"] = page_no
        field["boundingRegions"] = [region]
    return field


def _result(fields: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"documents": [{"fields": fields or {"InvoiceId": _field("INV-00001", page_no=1)} }]}


class FakePoller:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.timeout: float | None = None

    def result(self, timeout: float | None = None) -> Any:
        self.timeout = timeout
        if self._error:
            raise self._error
        return self._result

    def done(self) -> bool:
        return True


class FakeAzureClient:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.poller = FakePoller(result=result, error=error)
        self.calls: list[dict[str, Any]] = []

    def begin_analyze_document(self, model_id: str, *, body: io.BytesIO, **kwargs: Any) -> FakePoller:
        self.calls.append({"model_id": model_id, "body": body.read(), **kwargs})
        return self.poller


def _request(
    *,
    mime: str = "application/pdf",
    document_bytes: bytes = b"%PDF-test-document",
    pages: tuple[ExtractionPage, ...] | None = None,
    deadline: float = 30,
) -> ExtractionRequest:
    return ExtractionRequest(
        document_sha256="a" * 64,
        document_id="doc-1",
        mime=mime,
        pages=pages or (ExtractionPage(page_no=1),),
        locale="vi-VN",
        routing_reason="azure_prebuilt_invoice",
        deadline_seconds_remaining=deadline,
        document_bytes=document_bytes,
    )


@pytest.fixture(autouse=True)
def configure_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.providers.azure_document_intelligence.settings.allow_live_model_calls", True)
    monkeypatch.setattr(
        "app.providers.azure_document_intelligence.settings.document_intelligence_endpoint",
        "https://idp-resource.cognitiveservices.azure.com/",
    )


def test_azure_pdf_contract_preserves_raw_values_and_actual_provenance() -> None:
    fields = {
        "InvoiceId": _field("  INV-00001  ", page_no=2),
        "InvoiceDate": _field("08/02/2026", page_no=1),
        "SubTotal": _field("2.132.000,00", page_no=2),
        "TotalTax": _field("173.360,00", page_no=2),
        "InvoiceTotal": _field("2.305.360,00", page_no=2),
        "Items": {
            "valueArray": [
                {
                    "valueObject": {
                        "ProductCode": _field("SKU-7", page_no=2),
                        "Description": _field("Coffee", page_no=2),
                        "Quantity": _field("2,00", page_no=2),
                        "UnitPrice": _field("1.000,00", page_no=2),
                        "Amount": _field("2.000,00", page_no=2),
                    }
                }
            ]
        },
    }
    source = b"%PDF-original-two-page-invoice"
    client = FakeAzureClient(_result(fields))
    recorded: list[dict[str, Any]] = []

    result = AzureDocumentIntelligenceProvider(client=client).extract_invoice(
        _request(
            document_bytes=source,
            pages=(ExtractionPage(page_no=1), ExtractionPage(page_no=2)),
        ),
        reserve_invocation=lambda model, strategy: f"{model}:{strategy}",
        record_invocation=lambda call_id, **kwargs: recorded.append({"call_id": call_id, **kwargs}),
    )

    call = client.calls[0]
    assert call["model_id"] == AZURE_DOCUMENT_INTELLIGENCE_MODEL == "prebuilt-invoice"
    assert call["body"] == source
    assert call["locale"] == "vi"
    assert call["logging_enable"] is False
    assert client.poller.timeout is not None and client.poller.timeout > 0
    assert result.candidate["invoice_number"] == "  INV-00001  "
    assert result.candidate["invoice_date"] == "08/02/2026"
    assert result.candidate["subtotal"] == "2.132.000,00"
    assert result.candidate["tax_total"] == "173.360,00"
    assert result.candidate["grand_total"] == "2.305.360,00"
    assert result.candidate["lines"][0]["unit_price"] == "1.000,00"
    assert result.evidence_texts["invoice_number"] == "  INV-00001  "
    assert result.evidence_pages["invoice_number"] == 2
    assert result.evidence_bboxes["invoice_number"] == {**_REGION, "pageNumber": 2}
    assert result.evidence_confidences["invoice_number"] == 0.94
    assert result.usage == {"routing_reason": "azure_prebuilt_invoice"}
    assert recorded[0]["usage"] is None
    assert recorded[0]["failure_code"] is None
    assert recorded[0]["latency_ms"] >= 0


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg"])
def test_azure_sends_original_scanned_image_without_reencoding(mime: str) -> None:
    source = b"original-image-bytes"
    client = FakeAzureClient(_result({"InvoiceId": _field("IMG-001", page_no=1)}))

    result = AzureDocumentIntelligenceProvider(client=client).extract_invoice(
        _request(mime=mime, document_bytes=source)
    )

    assert client.calls[0]["body"] == source
    assert result.candidate["invoice_number"] == "IMG-001"
    assert result.evidence_pages == {"invoice_number": 1}


def test_azure_multi_page_scanned_pdf_keeps_provider_page_number() -> None:
    client = FakeAzureClient(_result({"InvoiceId": _field("PAGE-3", page_no=3)}))
    request = _request(
        pages=(ExtractionPage(page_no=1), ExtractionPage(page_no=2), ExtractionPage(page_no=3)),
    )

    result = AzureDocumentIntelligenceProvider(client=client).extract_invoice(request)

    assert result.evidence_pages == {"invoice_number": 3}
    assert result.evidence_bboxes["invoice_number"]["pageNumber"] == 3


def test_azure_keeps_missing_page_bbox_and_confidence_optional() -> None:
    client = FakeAzureClient(
        _result({"InvoiceId": _field("NO-PAGE", confidence=0.82)})
    )

    result = AzureDocumentIntelligenceProvider(client=client).extract_invoice(_request())

    assert result.evidence_pages == {}
    assert result.evidence_bboxes["invoice_number"] == {
        "polygon": _REGION["polygon"],
        "unit": "pixel",
    }
    assert result.evidence_confidences == {"invoice_number": 0.82}


def test_azure_does_not_invent_bounding_region_or_confidence() -> None:
    client = FakeAzureClient(_result({"InvoiceId": _field("NO-METADATA", bbox=False, confidence=_MISSING)}))

    result = AzureDocumentIntelligenceProvider(client=client).extract_invoice(_request())

    assert result.evidence_texts == {"invoice_number": "NO-METADATA"}
    assert result.evidence_pages == {}
    assert result.evidence_bboxes == {}
    assert result.evidence_confidences == {}


@pytest.mark.parametrize(
    ("error", "expected", "retryable", "retry_after"),
    [
        (
            type("HttpResponseError", (Exception,), {
                "status_code": 429,
                "response": SimpleNamespace(headers={"retry-after-ms": "2200"}),
            })("limited"),
            "PROVIDER_THROTTLED",
            True,
            3,
        ),
        (TimeoutError("timeout"), "PROVIDER_TIMEOUT", True, None),
        (
            type("ClientAuthenticationError", (Exception,), {})("auth"),
            "PROVIDER_AUTH_FAILED",
            False,
            None,
        ),
        (
            type("HttpResponseError", (Exception,), {
                "status_code": 415,
                "response": SimpleNamespace(headers={}),
            })("unsupported"),
            "PROVIDER_DOCUMENT_UNSUPPORTED",
            False,
            None,
        ),
    ],
)
def test_azure_provider_errors_are_typed_and_recorded(
    error: Exception, expected: str, retryable: bool, retry_after: int | None
) -> None:
    client = FakeAzureClient(error=error)
    recorded: list[dict[str, Any]] = []

    with pytest.raises(ProviderFailure) as captured:
        AzureDocumentIntelligenceProvider(client=client).extract_invoice(
            _request(),
            reserve_invocation=lambda *_: "call-1",
            record_invocation=lambda call_id, **kwargs: recorded.append({"call_id": call_id, **kwargs}),
        )

    assert captured.value.code == expected
    assert captured.value.retryable is retryable
    assert captured.value.retry_after_seconds == retry_after
    assert len(client.calls) == 1
    assert recorded[0]["failure_code"] == expected
    assert recorded[0]["usage"] is None


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"documents": "malformed"}, "PROVIDER_INVALID_OUTPUT"),
        ({"documents": []}, "PROVIDER_DOCUMENT_UNSUPPORTED"),
        ({"documents": [{"fields": []}]}, "PROVIDER_INVALID_OUTPUT"),
        (
            {"documents": [{"fields": {"InvoiceId": _field(42, page_no=1)}}]},
            "PROVIDER_SCHEMA_INVALID",
        ),
        (
            {"documents": [{"fields": {"InvoiceId": _field("INV", page_no=12)}}]},
            "PROVIDER_EVIDENCE_INVALID",
        ),
    ],
)
def test_azure_rejects_invalid_output_and_out_of_document_evidence(
    response: Any, expected: str
) -> None:
    with pytest.raises(ProviderFailure) as captured:
        AzureDocumentIntelligenceProvider(client=FakeAzureClient(response)).extract_invoice(_request())
    assert captured.value.code == expected


def test_azure_deadline_is_bounded_and_rejected_before_reserving_call() -> None:
    client = FakeAzureClient(_result())
    reserved: list[bool] = []

    with pytest.raises(ProviderFailure) as captured:
        AzureDocumentIntelligenceProvider(client=client).extract_invoice(
            _request(deadline=0.1), reserve_invocation=lambda *_: reserved.append(True) or "call-1"
        )

    assert captured.value.code == "JOB_DEADLINE_EXCEEDED"
    assert reserved == []
    assert client.calls == []


def test_azure_requires_explicit_live_flag_and_supported_input() -> None:
    provider = AzureDocumentIntelligenceProvider(client=FakeAzureClient(_result()))
    settings = __import__("app.providers.azure_document_intelligence", fromlist=["settings"]).settings
    original_live = settings.allow_live_model_calls
    settings.allow_live_model_calls = False
    try:
        with pytest.raises(ProviderFailure) as captured:
            provider.extract_invoice(_request())
        assert captured.value.code == "LIVE_CALLS_DISABLED"
    finally:
        settings.allow_live_model_calls = original_live

    with pytest.raises(ProviderFailure) as captured:
        provider.extract_invoice(_request(mime="application/zip"))
    assert captured.value.code == "PROVIDER_DOCUMENT_UNSUPPORTED"


def test_azure_sdk_and_model_versions_are_pinned_in_adapter() -> None:
    assert AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION == "1.0.2"
    assert AZURE_DOCUMENT_INTELLIGENCE_API_VERSION == "2024-11-30"
    assert AZURE_DOCUMENT_INTELLIGENCE_MODEL == "prebuilt-invoice"
    requirements_lock = Path(__file__).resolve().parents[2] / "requirements.lock"
    assert f"azure-ai-documentintelligence=={AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION}" in (
        requirements_lock.read_text(encoding="utf-8")
    )


def test_azure_sdk_client_uses_official_api_version_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    azure_module = ModuleType("azure")
    azure_module.__path__ = []  # type: ignore[attr-defined]
    azure_ai_module = ModuleType("azure.ai")
    azure_ai_module.__path__ = []  # type: ignore[attr-defined]
    sdk_module = ModuleType("azure.ai.documentintelligence")
    core_module = ModuleType("azure.core")
    core_module.__path__ = []  # type: ignore[attr-defined]
    credentials_module = ModuleType("azure.core.credentials")
    created: dict[str, Any] = {}

    class StubKeyCredential:
        def __init__(self, key: str) -> None:
            self.key = key

    class StubDocumentIntelligenceClient:
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)

    credentials_module.AzureKeyCredential = StubKeyCredential  # type: ignore[attr-defined]
    sdk_module.DocumentIntelligenceClient = StubDocumentIntelligenceClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.ai", azure_ai_module)
    monkeypatch.setitem(sys.modules, "azure.ai.documentintelligence", sdk_module)
    monkeypatch.setitem(sys.modules, "azure.core", core_module)
    monkeypatch.setitem(sys.modules, "azure.core.credentials", credentials_module)
    monkeypatch.setattr(
        "app.providers.azure_document_intelligence.settings.document_intelligence_api_key",
        "offline-test-key",
    )

    client = AzureDocumentIntelligenceProvider()._make_client()

    assert isinstance(client, StubDocumentIntelligenceClient)
    assert created["api_version"] == "2024-11-30"
    assert created["polling_interval"] == 1
    assert created["retry_total"] == 0
    assert created["endpoint"] == "https://idp-resource.cognitiveservices.azure.com"


def test_factory_selects_azure_only_when_explicitly_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.providers.factory.settings.extraction_provider", "azure_document_intelligence")

    assert isinstance(configured_provider(), AzureDocumentIntelligenceProvider)


def test_extraction_request_passes_original_pdf_only_for_azure_provider() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    source = stream.getvalue()
    document = SimpleNamespace(
        id=uuid.uuid4(), sha256="b" * 64, mime="application/pdf", pages=2, bytes=len(source)
    )
    provider = AzureDocumentIntelligenceProvider(client=FakeAzureClient(_result()))

    request, _ = _make_request(
        document,
        source,
        uuid.uuid4(),
        "vi-VN",
        provider,
        datetime.now(UTC) + timedelta(seconds=30),
    )

    assert request.document_bytes == source
    assert [page.page_no for page in request.pages] == [1, 2]
    assert all(page.text is None and page.image_bytes is None for page in request.pages)
    assert request.routing_reason == "azure_prebuilt_invoice"


def test_missing_azure_page_flows_to_existing_missing_provenance_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AzureDocumentIntelligenceProvider(
        client=FakeAzureClient(_result({"InvoiceId": _field("INV-NO-PAGE", bbox=False, confidence=_MISSING)}))
    )
    document = SimpleNamespace(
        id=uuid.uuid4(), sha256="c" * 64, mime="image/png", pages=1, bytes=12
    )
    monkeypatch.setattr("app.services.extraction.configured_provider", lambda: provider)
    monkeypatch.setattr(
        "app.services.extraction.reserve_provider_invocation",
        lambda *args, **kwargs: "call-1",
    )
    monkeypatch.setattr("app.services.extraction.record_provider_invocation", lambda *args, **kwargs: None)

    _, evidence, result, _ = _candidate_and_evidence(
        document,
        b"synthetic-png-source",
        uuid.uuid4(),
        uuid.uuid4(),
        1,
        "vi-VN",
        datetime.now(UTC) + timedelta(seconds=30),
    )

    assert result.provider == "azure_document_intelligence"
    assert evidence["invoice_number"].verification_state == "MISSING_PROVENANCE"
    assert evidence["invoice_number"].page_no is None
    assert evidence["invoice_number"].bbox is None


def test_azure_raw_values_get_separate_application_normalized_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AzureDocumentIntelligenceProvider(
        client=FakeAzureClient(
            _result(
                {
                    "InvoiceId": _field("INV-NORM-1", page_no=1),
                    "InvoiceDate": _field("24/02/2026", page_no=1),
                    "SubTotal": _field("2.132.000,00", page_no=1),
                }
            )
        )
    )
    document = SimpleNamespace(
        id=uuid.uuid4(), sha256="d" * 64, mime="image/png", pages=1, bytes=18
    )
    monkeypatch.setattr("app.services.extraction.configured_provider", lambda: provider)
    monkeypatch.setattr(
        "app.services.extraction.reserve_provider_invocation",
        lambda *args, **kwargs: "call-1",
    )
    monkeypatch.setattr("app.services.extraction.record_provider_invocation", lambda *args, **kwargs: None)

    _, evidence, _, _ = _candidate_and_evidence(
        document,
        b"synthetic-png-source",
        uuid.uuid4(),
        uuid.uuid4(),
        1,
        "vi-VN",
        datetime.now(UTC) + timedelta(seconds=30),
    )

    assert evidence["invoice_date"].raw_value == "24/02/2026"
    assert evidence["invoice_date"].normalized_value == "2026-02-24"
    assert evidence["subtotal"].raw_value == "2.132.000,00"
    assert evidence["subtotal"].normalized_value == "2132000.00"
    assert evidence["subtotal"].page_no == 1
    assert evidence["subtotal"].bbox == _REGION
    assert evidence["subtotal"].confidence == 0.94
