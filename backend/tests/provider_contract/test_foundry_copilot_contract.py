from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.copilot.provider import FoundryCopilotProvider
from app.providers.errors import ProviderFailure


def _response(*, finish_reason="stop", content="Giải thích demo.", calls=None, usage=None, refusal=None):
    message = NS(content=content, tool_calls=calls or [], refusal=refusal)
    return NS(
        choices=[NS(finish_reason=finish_reason, message=message)],
        usage=usage or NS(prompt_tokens=17, completion_tokens=9),
    )


def _call(call_id="call-read-1", name="get_reconciliation", arguments="{}"):
    return NS(
        id=call_id,
        function=NS(name=name, arguments=arguments),
    )


class FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.chat = NS(completions=FakeCompletions(response, error))


def test_foundry_copilot_returns_real_sdk_tool_call_shape_and_usage() -> None:
    client = FakeClient(
        _response(
            finish_reason="tool_calls",
            content=None,
            calls=[_call(arguments='{"ignored":false}')],
        )
    )
    result = FoundryCopilotProvider(client=client).complete(
        messages=[{"role": "user", "content": "Vì sao hồ sơ bị chặn?"}],
        tools=({"type": "function", "function": {"name": "get_reconciliation"}},),
        timeout_seconds=20,
    )

    assert result.tool_calls[0].call_id == "call-read-1"
    assert result.tool_calls[0].name == "get_reconciliation"
    assert result.tool_calls[0].arguments == {"ignored": False}
    assert result.input_tokens == 17 and result.output_tokens == 9
    assert client.chat.completions.kwargs["parallel_tool_calls"] is False
    assert client.chat.completions.kwargs["tool_choice"] == "auto"
    assert client.chat.completions.kwargs["timeout"] == 20


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_response(finish_reason="length"), "COPILOT_TRUNCATED"),
        (_response(finish_reason="content_filter"), "COPILOT_REFUSAL"),
        (_response(finish_reason="stop", content="", refusal="blocked"), "COPILOT_REFUSAL"),
        (_response(finish_reason="tool_calls", content=None, calls=[]), "COPILOT_SCHEMA_INVALID"),
        (
            _response(finish_reason="tool_calls", content=None, calls=[_call(arguments="not json")]),
            "COPILOT_SCHEMA_INVALID",
        ),
        (
            _response(finish_reason="tool_calls", content=None, calls=[_call(call_id="bad id")]),
            "COPILOT_SCHEMA_INVALID",
        ),
        (_response(finish_reason="stop", content=None), "COPILOT_SCHEMA_INVALID"),
    ],
)
def test_foundry_copilot_rejects_refusal_truncation_and_invalid_envelopes(response, code) -> None:
    provider = FoundryCopilotProvider(client=FakeClient(response))
    with pytest.raises(ProviderFailure) as caught:
        provider.complete(messages=[], tools=(), timeout_seconds=10)
    assert caught.value.code == code


def test_foundry_copilot_maps_rate_limit_without_retry() -> None:
    error = RuntimeError("rate limited")
    error.status_code = 429
    client = FakeClient(error=error)
    with pytest.raises(ProviderFailure) as caught:
        FoundryCopilotProvider(client=client).complete(messages=[], tools=(), timeout_seconds=10)
    assert caught.value.code == "PROVIDER_THROTTLED"
    assert client.chat.completions.kwargs is not None
