from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.providers.errors import ProviderFailure
from app.providers.foundry import FoundryProvider, extraction_json_schema
from app.providers.protocol import ExtractionPage, ExtractionRequest


def _invoice(**updates: Any) -> dict[str, Any]:
    invoice: dict[str, Any] = {
        "invoice_number": "INV-000123",
        "invoice_series": None,
        "invoice_series_absent_confirmed": False,
        "invoice_date": None,
        "supplier_tax_id": None,
        "supplier_name": None,
        "po_number": None,
        "currency": None,
        "subtotal": None,
        "tax_total": None,
        "grand_total": None,
        "discount_total": None,
        "shipping_total": None,
        "lines": [],
    }
    invoice.update(updates)
    return invoice


def _response(
    *,
    candidate: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    content: str | None = None,
    refusal: str | None = None,
) -> dict[str, Any]:
    output = content or json.dumps(
        {
            "invoice": candidate or _invoice(),
            "evidence": evidence
            if evidence is not None
            else [{"field_path": "invoice_number", "page_no": 1, "raw_text": "INV-000123"}],
        }
    )
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": output, "refusal": refusal},
            }
        ],
        "usage": {"prompt_tokens": 57, "completion_tokens": 31},
    }


class FakeCompletions:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.completions = FakeCompletions(response, error)
        self.chat = SimpleNamespace(completions=self.completions)


def _request(*, pages: tuple[ExtractionPage, ...] | None = None, deadline: float = 30) -> ExtractionRequest:
    return ExtractionRequest(
        document_sha256="a" * 64,
        document_id="doc-1",
        mime="application/pdf",
        pages=pages or (ExtractionPage(page_no=1, text="Invoice INV-000123"),),
        routing_reason="native_text",
        deadline_seconds_remaining=deadline,
    )


@pytest.fixture(autouse=True)
def configure_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.providers.foundry.settings.allow_live_model_calls", True)
    monkeypatch.setattr(
        "app.providers.foundry.settings.foundry_model_endpoint",
        "https://example.services.ai.azure.com/openai/v1/",
    )
    monkeypatch.setattr("app.providers.foundry.settings.foundry_deployment", "invoice-vision-deployment")


def test_foundry_uses_structured_output_and_shared_invoice_contract() -> None:
    client = FakeClient(_response())
    reserved: list[tuple[str, str]] = []
    recorded: list[dict[str, Any]] = []

    result = FoundryProvider(client=client).extract_invoice(
        _request(),
        reserve_invocation=lambda model, strategy: reserved.append((model, strategy)) or "call-1",
        record_invocation=lambda call_id, **kwargs: recorded.append({"id": call_id, **kwargs}),
    )

    request_body = client.completions.calls[0]
    assert request_body["model"] == "invoice-vision-deployment"
    assert request_body["response_format"]["json_schema"]["strict"] is True
    assert request_body["temperature"] == 0
    assert request_body["n"] == 1
    assert "untrusted data" in request_body["messages"][0]["content"]
    assert "[SOURCE PAGE 1 TEXT]" in request_body["messages"][1]["content"][1]["text"]
    assert result.provider == "foundry"
    assert result.strategy == "native_schema"
    assert result.model_id == "invoice-vision-deployment"
    assert result.candidate["invoice_number"] == "INV-000123"
    assert result.evidence_pages == {"invoice_number": 1}
    assert result.usage["input_tokens"] == 57
    assert result.usage["output_tokens"] == 31
    assert reserved == [("invoice-vision-deployment", "native_schema")]
    assert len(recorded) == 1
    assert recorded[0]["id"] == "call-1"
    assert recorded[0]["latency_ms"] >= 0
    assert recorded[0]["usage"] == {"inputTokens": 57, "outputTokens": 31}
    assert recorded[0]["failure_code"] is None


def test_foundry_schema_uses_required_nullable_properties_at_every_object_level() -> None:
    schema = extraction_json_schema()

    def assert_strict(node: dict[str, Any]) -> None:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                assert_strict(child)
        if node.get("type") == "array":
            assert_strict(node["items"])

    assert_strict(schema)


def test_foundry_sends_all_scanned_pdf_pages_as_page_marked_images() -> None:
    pages = (
        ExtractionPage(page_no=1, image_bytes=b"first-page", image_format="jpeg"),
        ExtractionPage(page_no=2, image_bytes=b"second-page", image_format="jpeg"),
    )
    client = FakeClient(
        _response(
            evidence=[{"field_path": "invoice_number", "page_no": 2, "raw_text": "INV-000123"}]
        )
    )

    result = FoundryProvider(client=client).extract_invoice(_request(pages=pages))

    content = client.completions.calls[0]["messages"][1]["content"]
    assert [part["text"] for part in content if part["type"] == "text"] == [
        content[0]["text"],
        "[SOURCE PAGE 1 IMAGE]",
        "[SOURCE PAGE 2 IMAGE]",
    ]
    image_parts = [part for part in content if part["type"] == "image_url"]
    assert len(image_parts) == 2
    assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert result.evidence_pages == {"invoice_number": 2}


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (_response(finish_reason="content_filter"), "PROVIDER_REFUSAL"),
        (_response(refusal="I cannot process this request"), "PROVIDER_REFUSAL"),
        (_response(finish_reason="length"), "PROVIDER_TRUNCATED"),
        (_response(content="{"), "PROVIDER_SCHEMA_INVALID"),
    ],
)
def test_foundry_refusal_truncation_and_schema_errors_are_typed_and_recorded(
    response: dict[str, Any], expected_code: str
) -> None:
    recorded: list[dict[str, Any]] = []

    with pytest.raises(ProviderFailure) as captured:
        FoundryProvider(client=FakeClient(response)).extract_invoice(
            _request(),
            reserve_invocation=lambda *_: "call-1",
            record_invocation=lambda call_id, **kwargs: recorded.append({"id": call_id, **kwargs}),
        )

    assert captured.value.code == expected_code
    assert recorded[0]["failure_code"] == expected_code


def test_foundry_rejects_missing_evidence_and_out_of_range_pages() -> None:
    for evidence in ([{"field_path": "invoice_number", "page_no": 2, "raw_text": "INV-000123"}],):
        with pytest.raises(ProviderFailure) as captured:
            FoundryProvider(client=FakeClient(_response(evidence=evidence))).extract_invoice(_request())
        assert captured.value.code == "PROVIDER_EVIDENCE_INVALID"


def test_foundry_preserves_raw_ambiguous_money_and_does_not_invent_evidence() -> None:
    result = FoundryProvider(
        client=FakeClient(_response(candidate=_invoice(grand_total="1234.56"), evidence=[]))
    ).extract_invoice(_request())

    assert result.candidate["grand_total"] == "1234.56"
    assert result.evidence_pages == {}
    assert result.evidence_texts == {}


def test_foundry_throttling_reads_retry_after_ms_and_does_not_retry_sdk_call() -> None:
    error = RuntimeError("rate limited")
    error.status_code = 429  # type: ignore[attr-defined]
    error.response = SimpleNamespace(headers={"retry-after-ms": "2200"})  # type: ignore[attr-defined]
    client = FakeClient(error=error)
    recorded: list[dict[str, Any]] = []

    with pytest.raises(ProviderFailure) as captured:
        FoundryProvider(client=client).extract_invoice(
            _request(),
            reserve_invocation=lambda *_: "call-1",
            record_invocation=lambda call_id, **kwargs: recorded.append({"id": call_id, **kwargs}),
        )

    assert captured.value.code == "PROVIDER_THROTTLED"
    assert captured.value.retryable is True
    assert captured.value.retry_after_seconds == 3
    assert len(client.completions.calls) == 1
    assert recorded[0]["failure_code"] == "PROVIDER_THROTTLED"


def test_foundry_deadline_blocks_before_reserving_a_call() -> None:
    reserved: list[bool] = []

    with pytest.raises(ProviderFailure) as captured:
        FoundryProvider(client=FakeClient(_response())).extract_invoice(
            _request(deadline=0.1), reserve_invocation=lambda *_: reserved.append(True) or "call-1"
        )

    assert captured.value.code == "JOB_DEADLINE_EXCEEDED"
    assert reserved == []


def test_foundry_endpoint_must_be_https_inference_endpoint() -> None:
    for endpoint in (
        "http://example.services.ai.azure.com/openai/v1",
        "https://example.services.ai.azure.com/api/projects/project",
        "https://user:secret@example.services.ai.azure.com/openai/v1",
        "https://example.services.ai.azure.com/openai/v1?api-version=preview",
    ):
        with pytest.raises(ProviderFailure) as captured:
            FoundryProvider._validate_endpoint(endpoint)
        assert captured.value.code == "PROVIDER_NOT_CONFIGURED"


def test_foundry_entra_scope_matches_endpoint_family() -> None:
    assert FoundryProvider._token_scope("https://x.openai.azure.com/openai/v1") == "https://ai.azure.com/.default"
    assert (
        FoundryProvider._token_scope("https://x.services.ai.azure.com/openai/v1")
        == "https://cognitiveservices.azure.com/.default"
    )


def test_foundry_builds_pinned_sdk_client_without_implicit_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.providers.foundry.settings.foundry_api_key", "test-key-not-a-credential")

    client = FoundryProvider(deployment="test-deployment")._make_client(24.0)

    assert str(client.base_url) == "https://example.services.ai.azure.com/openai/v1/"
    assert client.max_retries == 0


def test_foundry_timeout_errors_map_to_retryable_timeout() -> None:
    timeout_error = type("APITimeoutError", (Exception,), {})("timed out")

    mapped = FoundryProvider._map_exception(timeout_error)

    assert mapped.code == "PROVIDER_TIMEOUT"
    assert mapped.retryable is True
