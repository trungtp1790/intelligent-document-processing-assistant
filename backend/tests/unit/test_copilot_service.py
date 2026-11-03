from __future__ import annotations

from uuid import uuid4

import pytest

from app.copilot.provider import CopilotCompletion, ToolCall
from app.copilot.service import MAX_ROUNDS, MAX_TOOL_CALLS, run_copilot
from app.domain.errors import DomainError
from app.security import Principal


def _principal() -> Principal:
    return Principal(uuid4(), uuid4(), "copilot-test", "Copilot test", "operator")


def _detail(*, version_hash="a" * 64, reconciliation_hash=None):
    return {
        "id": str(uuid4()),
        "workflow_status": "REVIEW_REQUIRED",
        "fixture_mode": True,
        "version": {"id": str(uuid4()), "snapshot_hash": version_hash, "version_no": 1, "data": {}},
        "reconciliation": (
            {"id": str(uuid4()), "snapshot_hash": reconciliation_hash} if reconciliation_hash else None
        ),
    }


class SequenceProvider:
    name = "test-double"

    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.completions.pop(0)

    def close(self):
        self.closed = getattr(self, "closed", 0) + 1


def _tool(round_no: int, name="get_invoice_summary"):
    return ToolCall(f"call-{round_no}", name, {})


def _tool_completion(*calls):
    return CopilotCompletion(None, tuple(calls), "tool_calls", 10, 3, 5)


def _answer_completion(text="Có một cảnh báo cần kiểm tra."):
    return CopilotCompletion(text, (), "stop", 12, 8, 4)


def test_tool_results_are_bound_to_exact_call_id_and_trace_keeps_metadata_only(monkeypatch) -> None:
    detail = _detail()
    monkeypatch.setattr("app.copilot.service._read_invoice", lambda _org_id, _invoice_id: detail)
    monkeypatch.setattr("app.copilot.service.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        "app.copilot.service.execute_read_tool",
        lambda *_args, **_kwargs: {"workflow_status": "REVIEW_REQUIRED", "untrusted": True},
    )
    provider = SequenceProvider([_tool_completion(_tool(1)), _answer_completion()])

    result = run_copilot(
        actor=_principal(), invoice_id=uuid4(), question="Vì sao cần kiểm tra?", history=[], provider=provider
    )

    tool_message = next(item for item in provider.calls[1]["messages"] if item.get("role") == "tool")
    assert tool_message["tool_call_id"] == "call-1"
    assert '"workflow_status":"REVIEW_REQUIRED"' in tool_message["content"]
    assistant_call = next(item for item in provider.calls[1]["messages"] if item.get("tool_calls"))
    assert assistant_call["tool_calls"][0]["id"] == tool_message["tool_call_id"]
    assert result["status"] == "answered"
    assert result["synthetic"] is True
    assert result["trace"]["tool_calls"] == [
        {
            "round": 1,
            "call_id": "call-1",
            "tool": "get_invoice_summary",
            "status": "success",
            "duration_ms": 0,
        }
    ]
    assert result["trace"]["input_tokens"] == 22
    assert result["trace"]["output_tokens"] == 11
    assert "content" not in result["trace"]


def test_copilot_enforces_four_rounds_and_six_backend_tool_attempts(monkeypatch) -> None:
    detail = _detail()
    monkeypatch.setattr("app.copilot.service._read_invoice", lambda _org_id, _invoice_id: detail)
    monkeypatch.setattr("app.copilot.service.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        "app.copilot.service.execute_read_tool",
        lambda *_args, **_kwargs: {"ok": True},
    )
    rounds = [
        _tool_completion(_tool(index * 2 + 1), _tool(index * 2 + 2)) for index in range(1, MAX_ROUNDS + 1)
    ]
    provider = SequenceProvider(rounds)

    result = run_copilot(
        actor=_principal(), invoice_id=uuid4(), question="Tóm tắt hồ sơ này", history=[], provider=provider
    )

    assert len(provider.calls) == MAX_ROUNDS
    assert result["status"] == "budget_exhausted"
    assert result["trace"]["tool_executions"] == MAX_TOOL_CALLS
    assert result["trace"]["tool_call_count"] == MAX_TOOL_CALLS
    assert result["trace"]["tool_calls_requested"] == MAX_ROUNDS * 2
    assert sum(row["status"] == "budget_denied" for row in result["trace"]["tool_calls"]) == 2
    assert result["trace"]["stop_reason"] == "round_limit"


def test_context_change_stops_before_using_mixed_versions(monkeypatch) -> None:
    initial = _detail(version_hash="a" * 64)
    changed = _detail(version_hash="b" * 64)
    monkeypatch.setattr("app.copilot.service._read_invoice", lambda _org_id, _invoice_id: initial)
    monkeypatch.setattr("app.copilot.service.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        "app.copilot.service.execute_read_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DomainError("COPILOT_CONTEXT_CHANGED", "Hồ sơ đã thay đổi.", 409)
        ),
    )
    provider = SequenceProvider([_tool_completion(_tool(1))])

    with pytest.raises(DomainError) as caught:
        run_copilot(
            actor=_principal(), invoice_id=uuid4(), question="Vì sao bị chặn?", history=[], provider=provider
        )
    assert caught.value.code == "COPILOT_CONTEXT_CHANGED"
    assert changed["version"]["snapshot_hash"] != initial["version"]["snapshot_hash"]


def test_server_owned_provider_is_closed_after_the_turn(monkeypatch) -> None:
    detail = _detail()
    monkeypatch.setattr("app.copilot.service._read_invoice", lambda _org_id, _invoice_id: detail)
    provider = SequenceProvider([_answer_completion()])
    monkeypatch.setattr("app.copilot.service.create_provider", lambda: provider)

    result = run_copilot(actor=_principal(), invoice_id=uuid4(), question="Tóm tắt hồ sơ", history=[])

    assert result["status"] == "answered"
    assert provider.closed == 1


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
