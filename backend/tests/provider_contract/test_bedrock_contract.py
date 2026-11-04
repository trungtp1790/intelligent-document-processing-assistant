from __future__ import annotations

import io
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from app.providers.bedrock import BedrockProvider
from app.providers.errors import ProviderFailure
from app.providers.protocol import (
    ExtractionPage,
    ExtractionRequest,
    ExtractionResult,
    ProviderCapabilities,
)
from app.services.extraction import _candidate_and_evidence, _make_request


class FakeBedrockClient:
    def __init__(self, response: dict[str, Any] | Exception):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def tool_response(invoice: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "invoice_number": "000123",
        "invoice_date": "2026-10-01",
        "currency": "VND",
        "lines": [],
    }
    if invoice:
        payload.update(invoice)
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "submit_invoice",
                            "input": {
                                "invoice": payload,
                                "evidence": [
                                    {
                                        "field_path": "invoice_number",
                                        "page_no": 1,
                                        "raw_text": "Invoice number 000123",
                                    },
                                    {
                                        "field_path": "invoice_date",
                                        "page_no": 1,
                                        "raw_text": "Invoice date 2026-10-01",
                                    },
                                    {"field_path": "currency", "page_no": 1, "raw_text": "Currency VND"},
                                ],
                            },
                        }
                    }
                ]
            }
        },
        "usage": {"inputTokens": 71, "outputTokens": 18},
    }


def request() -> ExtractionRequest:
    return ExtractionRequest(
        document_sha256="a" * 64,
        document_id="doc-id",
        mime="application/pdf",
        pages=(
            ExtractionPage(
                page_no=1,
                text="Invoice number 000123; invoice date 2026-10-01; currency VND",
            ),
        ),
        routing_reason="native_text",
    )


class EvidencePipelineProvider:
    name = "contract-test"

    def __init__(self, quote: str):
        self.quote = quote
        self.request: ExtractionRequest | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            text_input=True,
            image_input=True,
            max_pages=10,
            max_bytes=15 * 1024 * 1024,
            output_strategy="tool_schema",
            model_id="contract-test-model",
        )

    def extract_invoice(self, extraction_request: ExtractionRequest, **_: Any) -> ExtractionResult:
        self.request = extraction_request
        return ExtractionResult(
            provider="bedrock",
            model_id="contract-test-model",
            candidate={"invoice_number": "INV-000123"},
            evidence_texts={"invoice_number": self.quote},
            evidence_pages={"invoice_number": 1},
            raw_output_ref=None,
            strategy="tool_schema",
        )


@pytest.fixture(autouse=True)
def configure_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.providers.bedrock.settings.allow_live_model_calls", True)
    monkeypatch.setattr("app.providers.bedrock.settings.aws_region", "us-east-1")


def test_bedrock_contract_uses_schema_tool_and_keeps_document_as_untrusted_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeBedrockClient(tool_response())
    provider = BedrockProvider(client=client, model_id="demo-model")
    reserved: list[tuple[str, str]] = []
    recorded: list[dict[str, Any]] = []

    result = provider.extract_invoice(
        request(),
        reserve_invocation=lambda model, strategy: reserved.append((model, strategy)) or "call-id",
        record_invocation=lambda call_id, **kwargs: recorded.append({"id": call_id, **kwargs}),
    )

    assert result.provider == "bedrock"
    assert result.strategy == "tool_schema"
    assert result.candidate["invoice_number"] == "000123"
    assert result.evidence_pages == {"invoice_number": 1, "invoice_date": 1, "currency": 1}
    assert result.usage["input_tokens"] == 71
    assert reserved == [("demo-model", "tool_schema")]
    assert recorded[0]["id"] == "call-id"
    assert recorded[0]["failure_code"] is None
    request_body = client.calls[0]
    assert request_body["toolConfig"]["tools"][0]["toolSpec"]["name"] == "submit_invoice"
    assert "untrusted data" in request_body["system"][0]["text"]
    assert request_body["messages"][0]["content"][0]["text"].startswith("Extract this invoice.")


def test_bedrock_throttling_is_a_typed_retryable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "busy"},
            "ResponseMetadata": {"HTTPStatusCode": 429, "HTTPHeaders": {"retry-after": "4"}},
        },
        "Converse",
    )
    client = FakeBedrockClient(error)
    recorded: list[dict[str, Any]] = []
    with pytest.raises(ProviderFailure) as captured:
        BedrockProvider(client=client, model_id="demo-model").extract_invoice(
            request(),
            reserve_invocation=lambda *_: "call-id",
            record_invocation=lambda call_id, **kwargs: recorded.append({"id": call_id, **kwargs}),
        )
    assert captured.value.code == "PROVIDER_THROTTLED"
    assert captured.value.retryable is True
    assert captured.value.retry_after_seconds == 4
    assert recorded[0]["failure_code"] == "PROVIDER_THROTTLED"


def test_bedrock_rejects_invalid_evidence_page(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tool_response()
    bad["output"]["message"]["content"][0]["toolUse"]["input"]["evidence"][0]["page_no"] = 2
    with pytest.raises(ProviderFailure) as captured:
        BedrockProvider(client=FakeBedrockClient(bad), model_id="demo-model").extract_invoice(request())
    assert captured.value.code == "PROVIDER_EVIDENCE_INVALID"
    assert captured.value.retryable is False


def test_bedrock_keeps_fields_without_provider_evidence_unverified() -> None:
    result = BedrockProvider(
        client=FakeBedrockClient(tool_response(invoice={"supplier_name": "ACME Co."})),
        model_id="demo-model",
    ).extract_invoice(request())

    assert result.candidate["supplier_name"] == "ACME Co."
    assert "supplier_name" not in result.evidence_pages
    assert "supplier_name" not in result.evidence_texts


@pytest.mark.parametrize("stop_reason", ["guardrail_intervened", "content_filtered"])
def test_bedrock_refusal_is_a_typed_failure(stop_reason: str) -> None:
    response = {"stopReason": stop_reason, "output": {"message": {"content": []}}}
    with pytest.raises(ProviderFailure) as captured:
        BedrockProvider(client=FakeBedrockClient(response), model_id="demo-model").extract_invoice(request())
    assert captured.value.code == "PROVIDER_REFUSAL"
    assert captured.value.retryable is False


def test_bedrock_truncation_is_not_retried_blindly() -> None:
    response = {"stopReason": "max_tokens", "output": {"message": {"content": []}}}
    with pytest.raises(ProviderFailure) as captured:
        BedrockProvider(client=FakeBedrockClient(response), model_id="demo-model").extract_invoice(request())
    assert captured.value.code == "PROVIDER_TRUNCATED"
    assert captured.value.retryable is False


@pytest.mark.parametrize(
    ("stop_reason", "expected_code"),
    [
        ("malformed_model_output", "PROVIDER_SCHEMA_INVALID"),
        ("malformed_tool_use", "PROVIDER_SCHEMA_INVALID"),
        ("model_context_window_exceeded", "INPUT_LIMIT_EXCEEDED"),
    ],
)
def test_bedrock_malformed_output_and_context_limit_are_typed(
    stop_reason: str, expected_code: str
) -> None:
    response = {"stopReason": stop_reason, "output": {"message": {"content": []}}}
    with pytest.raises(ProviderFailure) as captured:
        BedrockProvider(client=FakeBedrockClient(response), model_id="demo-model").extract_invoice(request())
    assert captured.value.code == expected_code
    assert captured.value.retryable is False


def test_bedrock_preserves_raw_money_for_application_normalization() -> None:
    client = FakeBedrockClient(tool_response(invoice={"grand_total": "not-a-decimal"}))
    recorded: list[dict[str, Any]] = []
    result = BedrockProvider(client=client, model_id="demo-model").extract_invoice(
        request(),
        reserve_invocation=lambda *_: "call-id",
        record_invocation=lambda call_id, **kwargs: recorded.append({"id": call_id, **kwargs}),
    )

    assert result.candidate["grand_total"] == "not-a-decimal"
    assert recorded[0]["failure_code"] is None


def test_bedrock_read_deadline_is_typed_retryable_and_sdk_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []
    timeout = ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com", error="slow")
    with pytest.raises(ProviderFailure) as captured:
        BedrockProvider(client=FakeBedrockClient(timeout), model_id="demo-model").extract_invoice(
            request(),
            reserve_invocation=lambda *_: "call-id",
            record_invocation=lambda call_id, **kwargs: recorded.append({"id": call_id, **kwargs}),
        )
    assert captured.value.code == "PROVIDER_TIMEOUT"
    assert captured.value.retryable is True
    assert recorded[0]["failure_code"] == "PROVIDER_TIMEOUT"

    created: dict[str, Any] = {}

    def fake_client(service_name: str, **kwargs: Any) -> object:
        created.update(service_name=service_name, **kwargs)
        return object()

    monkeypatch.setattr("boto3.client", fake_client)
    BedrockProvider._make_client(deadline_remaining_seconds=25)
    config = created["config"]
    assert created["service_name"] == "bedrock-runtime"
    assert config.connect_timeout == 10
    assert config.read_timeout == 14
    assert config.retries["total_max_attempts"] == 1

    reserve_calls: list[bool] = []
    with pytest.raises(ProviderFailure) as expired:
        BedrockProvider(client=FakeBedrockClient(tool_response()), model_id="demo-model").extract_invoice(
            replace(request(), deadline_seconds_remaining=1.0),
            reserve_invocation=lambda *_: reserve_calls.append(True) or "call-id",
        )
    assert expired.value.code == "JOB_DEADLINE_EXCEEDED"
    assert reserve_calls == []


def test_provider_failure_from_client_setup_is_preserved_without_self_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ProviderFailure("JOB_DEADLINE_EXCEEDED", "deadline", retryable=False)
    recorded: list[dict[str, Any]] = []

    def fail_client(_: float | None) -> Any:
        raise expected

    monkeypatch.setattr(BedrockProvider, "_make_client", staticmethod(fail_client))
    with pytest.raises(ProviderFailure) as captured:
        BedrockProvider(model_id="demo-model").extract_invoice(
            replace(request(), deadline_seconds_remaining=30.0),
            reserve_invocation=lambda *_: "call-id",
            record_invocation=lambda call_id, **kwargs: recorded.append({"id": call_id, **kwargs}),
        )
    assert captured.value is expected
    assert captured.value.__cause__ is None
    assert recorded[0]["failure_code"] == "JOB_DEADLINE_EXCEEDED"


def test_native_pdf_routes_to_text_with_page_markers() -> None:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(30, 750, "Invoice number INV-000123 supplier name and tax information " * 3)
    pdf.save()
    content = output.getvalue()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        sha256="b" * 64,
        mime="application/pdf",
        bytes=len(content),
        pages=1,
    )
    extraction_request, page_text = _make_request(
        document,
        content,
        uuid.uuid4(),
        "vi-VN",
        BedrockProvider(client=FakeBedrockClient(tool_response()), model_id="demo-model"),
        datetime.now(UTC) + timedelta(minutes=5),
    )
    assert extraction_request.routing_reason == "native_text"
    assert extraction_request.pages[0].text
    assert "INV-000123" in page_text[1]


def test_multi_page_scanned_pdf_becomes_two_image_inputs() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_blank_page(width=300, height=400)
    output = io.BytesIO()
    writer.write(output)
    content = output.getvalue()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        sha256="c" * 64,
        mime="application/pdf",
        bytes=len(content),
        pages=2,
    )
    client = FakeBedrockClient(tool_response())
    extraction_request, page_text = _make_request(
        document,
        content,
        uuid.uuid4(),
        "vi-VN",
        BedrockProvider(client=client, model_id="demo-model"),
        datetime.now(UTC) + timedelta(minutes=5),
    )
    BedrockProvider(client=client, model_id="demo-model").extract_invoice(extraction_request)

    assert page_text == {1: "", 2: ""}
    assert extraction_request.routing_reason == "vision_low_or_unusable_native_text"
    assert [page.page_no for page in extraction_request.pages] == [1, 2]
    request_content = client.calls[0]["messages"][0]["content"]
    assert sum("image" in block for block in request_content) == 2
    page_markers = [block["text"] for block in request_content if "text" in block]
    assert "[SOURCE PAGE 1 IMAGE]" in page_markers
    assert "[SOURCE PAGE 2 IMAGE]" in page_markers
    tool_schema = client.calls[0]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert set(tool_schema) == {"type", "properties", "required"}


@pytest.mark.parametrize("scanned", [False, True], ids=["native-text", "scanned-image"])
def test_candidate_evidence_provenance_distinguishes_text_from_page_reference(
    scanned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = io.BytesIO()
    if scanned:
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        writer.write(output)
    else:
        pdf = canvas.Canvas(output)
        pdf.drawString(30, 750, "Invoice number INV-000123 supplier details and invoice data " * 3)
        pdf.save()
    content = output.getvalue()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        sha256="d" * 64,
        mime="application/pdf",
        bytes=len(content),
        pages=1,
    )
    provider = EvidencePipelineProvider("INV-000123")
    monkeypatch.setattr("app.services.extraction.configured_provider", lambda: provider)

    _, evidence, _, _ = _candidate_and_evidence(
        document,
        content,
        uuid.uuid4(),
        uuid.uuid4(),
        1,
        "vi-VN",
        datetime.now(UTC) + timedelta(minutes=2),
    )

    assert provider.request is not None
    assert provider.request.routing_reason == (
        "vision_low_or_unusable_native_text" if scanned else "native_text"
    )
    expected_verification = "PAGE_REFERENCED" if scanned else "TEXT_VERIFIED"
    assert evidence["invoice_number"].verification_state == expected_verification


def test_pipeline_keeps_raw_normalized_and_only_provider_supplied_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(30, 750, "Invoice INV-000123 subtotal 1.234.567,89 " * 4)
    pdf.save()
    content = output.getvalue()
    document = SimpleNamespace(
        id=uuid.uuid4(), sha256="e" * 64, mime="application/pdf", bytes=len(content), pages=1
    )

    class PipelineProvider(EvidencePipelineProvider):
        def extract_invoice(self, extraction_request: ExtractionRequest, **_: Any) -> ExtractionResult:
            self.request = extraction_request
            return ExtractionResult(
                provider="test-provider",
                model_id="offline-contract",
                candidate={
                    "invoice_number": "INV-000123",
                    "invoice_date": "25/12/2026",
                    "subtotal": "1.234.567,89",
                    "grand_total": "1234.56",
                    "tax_total": "0",
                },
                evidence_texts={"invoice_number": "INV-000123", "subtotal": "1.234.567,89"},
                evidence_pages={"invoice_number": 1},
                evidence_bboxes={"subtotal": {"polygon": [[0.1, 0.2], [0.4, 0.3]]}},
                evidence_confidences={"subtotal": 0.87},
                raw_output_ref=None,
                strategy="test",
            )

    provider = PipelineProvider("unused")
    monkeypatch.setattr("app.services.extraction.configured_provider", lambda: provider)
    canonical, evidence, _, _ = _candidate_and_evidence(
        document,
        content,
        uuid.uuid4(),
        uuid.uuid4(),
        1,
        "vi-VN",
        datetime.now(UTC) + timedelta(minutes=2),
    )

    assert canonical["invoice_date"] == "2026-12-25"
    assert canonical["subtotal"] == "1234567.89"
    assert canonical["grand_total"] is None
    assert evidence["subtotal"].raw_value == "1.234.567,89"
    assert evidence["subtotal"].normalized_value == "1234567.89"
    assert evidence["subtotal"].page_no is None
    assert evidence["subtotal"].verification_state == "MISSING_PROVENANCE"
    assert evidence["subtotal"].bbox == {"polygon": [[0.1, 0.2], [0.4, 0.3]]}
    assert evidence["subtotal"].confidence == 0.87
    assert evidence["grand_total"].normalization_issue == "AMBIGUOUS_NUMBER"
    assert evidence["tax_total"].raw_value == "0"
    assert evidence["tax_total"].normalized_value == "0"


def test_pipeline_does_not_fill_missing_provider_page_bbox_or_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(30, 750, "Invoice INV-000123 " * 6)
    pdf.save()
    content = output.getvalue()
    document = SimpleNamespace(
        id=uuid.uuid4(), sha256="f" * 64, mime="application/pdf", bytes=len(content), pages=1
    )
    provider = EvidencePipelineProvider("INV-000123")
    monkeypatch.setattr("app.services.extraction.configured_provider", lambda: provider)

    _, evidence, _, _ = _candidate_and_evidence(
        document,
        content,
        uuid.uuid4(),
        uuid.uuid4(),
        1,
        "vi-VN",
        datetime.now(UTC) + timedelta(minutes=2),
    )

    assert evidence["invoice_number"].page_no == 1
    assert evidence["invoice_number"].bbox is None
    assert evidence["invoice_number"].confidence is None
