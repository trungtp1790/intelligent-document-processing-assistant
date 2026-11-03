from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.providers.errors import ProviderFailure
from app.providers.foundry import FoundryProvider, _get

_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_]{1,80}$")
MAX_TOOL_ARGUMENT_BYTES = 8_000


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class CopilotCompletion:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class FoundryCopilotProvider:
    """Foundry Chat Completions tool-calling adapter using the pinned OpenAI SDK."""

    name = "foundry"

    def __init__(self, client: Any | None = None) -> None:
        self.client = client

    def close(self) -> None:
        client, self.client = self.client, None
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: tuple[dict[str, Any], ...],
        timeout_seconds: float,
    ) -> CopilotCompletion:
        if timeout_seconds < 1:
            raise ProviderFailure(
                "COPILOT_DEADLINE_EXCEEDED",
                "Đã hết thời gian cho lượt hỏi này.",
                retryable=False,
                status_code=504,
            )
        client = self.client
        try:
            if client is None:
                client = FoundryProvider().make_client(
                    min(float(settings.foundry_read_timeout_seconds), timeout_seconds)
                )
                self.client = client
            started = time.monotonic()
            response = client.chat.completions.create(
                model=settings.foundry_deployment,
                messages=messages,
                tools=list(tools),
                tool_choice="auto",
                parallel_tool_calls=False,
                temperature=settings.foundry_temperature,
                max_completion_tokens=settings.copilot_max_output_tokens,
                n=1,
                timeout=timeout_seconds,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
        except ProviderFailure:
            raise
        except Exception as exc:
            failure = FoundryProvider._map_exception(exc)
            if failure.code == "PROVIDER_TIMEOUT":
                failure = ProviderFailure(
                    "COPILOT_TIMEOUT", "Foundry không phản hồi trong thời hạn của lượt hỏi.", retryable=True
                )
            raise failure from exc

        choices = _get(response, "choices")
        if not isinstance(choices, list | tuple) or len(choices) != 1:
            self._invalid("Foundry returned an invalid copilot completion envelope.")
        choice = choices[0]
        message = _get(choice, "message")
        finish_reason = _get(choice, "finish_reason")
        if finish_reason == "length":
            raise ProviderFailure(
                "COPILOT_TRUNCATED", "Câu trả lời vượt giới hạn đầu ra.", retryable=False, status_code=502
            )
        if finish_reason == "content_filter" or _get(message, "refusal"):
            raise ProviderFailure(
                "COPILOT_REFUSAL", "Mô hình từ chối trả lời yêu cầu này.", retryable=False, status_code=422
            )
        if finish_reason not in {"stop", "tool_calls"}:
            self._invalid("Foundry returned an unsupported copilot completion state.")

        content = _get(message, "content")
        if content is not None and not isinstance(content, str):
            self._invalid("Foundry returned invalid copilot text.")
        raw_calls = _get(message, "tool_calls") or []
        if not isinstance(raw_calls, list | tuple):
            self._invalid("Foundry returned invalid tool calls.")
        parsed_calls: list[ToolCall] = []
        seen_ids: set[str] = set()
        for raw in raw_calls:
            call_id = _get(raw, "id")
            function = _get(raw, "function")
            name = _get(function, "name")
            encoded_arguments = _get(function, "arguments")
            if (
                not isinstance(call_id, str)
                or not _CALL_ID.fullmatch(call_id)
                or call_id in seen_ids
                or not isinstance(name, str)
                or not _TOOL_NAME.fullmatch(name)
                or not isinstance(encoded_arguments, str)
                or len(encoded_arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES
            ):
                self._invalid("Foundry returned an invalid tool-call envelope.")
            seen_ids.add(call_id)
            try:
                arguments = json.loads(encoded_arguments)
            except (json.JSONDecodeError, TypeError):
                self._invalid("Foundry returned malformed tool arguments.")
            if not isinstance(arguments, dict):
                self._invalid("Foundry tool arguments must be a JSON object.")
            parsed_calls.append(ToolCall(call_id, name, arguments))

        if finish_reason == "tool_calls" and not parsed_calls:
            self._invalid("Foundry indicated tool calls but returned no calls.")
        if finish_reason == "stop" and parsed_calls:
            self._invalid("Foundry returned tool calls with an incompatible finish reason.")
        if finish_reason == "stop" and (not isinstance(content, str) or not content.strip()):
            self._invalid("Foundry returned an empty copilot answer.")

        usage = _get(response, "usage")
        return CopilotCompletion(
            content=content,
            tool_calls=tuple(parsed_calls),
            finish_reason=finish_reason,
            input_tokens=self._token_count(_get(usage, "prompt_tokens")),
            output_tokens=self._token_count(_get(usage, "completion_tokens")),
            latency_ms=latency_ms,
        )

    @staticmethod
    def _invalid(message: str) -> None:
        raise ProviderFailure("COPILOT_SCHEMA_INVALID", message, retryable=False, status_code=502)

    @staticmethod
    def _token_count(value: Any) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None
