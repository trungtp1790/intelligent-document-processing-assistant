from __future__ import annotations

import json
from uuid import uuid4

import pytest
from scripts import run_copilot_experiment as m6_runner

from app.copilot.experiment import run_copilot_experiment
from app.copilot.provider import CopilotCompletion
from app.domain.errors import DomainError
from app.security import Principal


def _principal() -> Principal:
    return Principal(uuid4(), uuid4(), "m6-test", "M6 test", "operator")


def _detail(*, fixture_mode: bool = False, version_hash: str = "a" * 64) -> dict:
    return {
        "id": str(uuid4()),
        "workflow_status": "REVIEW_REQUIRED",
        "fixture_mode": fixture_mode,
        "version": {
            "id": str(uuid4()),
            "snapshot_hash": version_hash,
            "version_no": 1,
            "data": {},
        },
        "reconciliation": {"id": str(uuid4()), "snapshot_hash": "b" * 64},
    }


class SequenceProvider:
    name = "test-double"

    def __init__(self, completions: list[CopilotCompletion]) -> None:
        self.completions = list(completions)
        self.calls: list[dict] = []

    def complete(self, **kwargs) -> CopilotCompletion:
        self.calls.append(kwargs)
        return self.completions.pop(0)


def _completion(content: str) -> CopilotCompletion:
    return CopilotCompletion(content, (), "stop", 10, 8, 2)


def _agent_result(*, source_id: str) -> str:
    return json.dumps(
        {
            "notes": [{"statement": "Thông tin được hỗ trợ bởi nguồn.", "citations": [source_id]}],
            "uncertainties": [],
        },
        ensure_ascii=False,
    )


def _source_data(name: str) -> dict:
    return {
        "get_invoice_summary": {"fields": {"invoice_number": "DEMO-001"}, "data_is_untrusted": True},
        "get_invoice_evidence": {
            "evidence": [{"field_path": "invoice_number", "page_no": 1, "quote": "DEMO-001"}],
            "document_content_is_untrusted": True,
        },
        "get_reconciliation": {
            "status": "BLOCKED",
            "issues": [{"code": "TOTAL_MISMATCH", "severity": "error"}],
        },
        "get_purchase_order": {"status": "NOT_LINKED", "purchase_order": None},
    }[name]


def _provider(*, reader_source: str = "invoice_evidence:invoice_number:1") -> SequenceProvider:
    return SequenceProvider(
        [
            _completion("Baseline answer."),
            _completion(_agent_result(source_id=reader_source)),
            _completion(_agent_result(source_id="reconciliation:issue:TOTAL_MISMATCH")),
            _completion(
                json.dumps(
                    {
                        "answer": "The coded reconciliation reports a total mismatch.",
                        "citations": [
                            "invoice_evidence:invoice_number:1",
                            "reconciliation:issue:TOTAL_MISMATCH",
                        ],
                    }
                )
            ),
        ]
    )


def test_experiment_runs_single_and_three_role_arms_with_snapshot_bound_reads(monkeypatch) -> None:
    actor = _principal()
    invoice_id = uuid4()
    detail = _detail()
    provider = _provider()
    read_org_ids: list = []
    monkeypatch.setattr("app.copilot.experiment._read_invoice", lambda _org_id, _invoice_id: detail)
    monkeypatch.setattr("app.copilot.service._read_invoice", lambda _org_id, _invoice_id: detail)

    def read_tool(*, actor, invoice_id, snapshot, name):
        read_org_ids.append(actor.org_id)
        return _source_data(name)

    monkeypatch.setattr("app.copilot.experiment._read_tool", read_tool)

    result = run_copilot_experiment(
        actor=actor,
        invoice_id=invoice_id,
        question="Vì sao hồ sơ bị chặn?",
        provider=provider,
        include_private_review=True,
    )

    assert result["status"] == "complete"
    assert result["synthetic"] is False
    assert result["quality"] == {
        "answer_quality": "NOT_MEASURED",
        "human_review": "NOT_COLLECTED",
        "citation_check": "source_ids_validated",
    }
    assert result["single_agent"]["model_calls"] == 1
    assert result["multi_agent"]["model_calls"] == 3
    assert result["multi_agent"]["tool_calls"] == 4
    assert result["multi_agent"]["citation_ids_valid"] is True
    assert result["private_review"]["single_agent_answer"] == "Baseline answer."
    assert result["private_review"]["multi_agent_citations"] == [
        "invoice_evidence:invoice_number:1",
        "reconciliation:issue:TOTAL_MISMATCH",
    ]
    assert [call["messages"][0]["role"] for call in provider.calls] == ["system"] * 4
    assert all(call["tools"] == () for call in provider.calls[1:])
    assert read_org_ids == [actor.org_id] * 4


def test_experiment_rejects_a_model_citation_outside_the_snapshot(monkeypatch) -> None:
    actor = _principal()
    detail = _detail()
    provider = _provider(reader_source="invoice_evidence:grand_total:99")
    monkeypatch.setattr("app.copilot.experiment._read_invoice", lambda _org_id, _invoice_id: detail)
    monkeypatch.setattr("app.copilot.service._read_invoice", lambda _org_id, _invoice_id: detail)
    monkeypatch.setattr("app.copilot.experiment._read_tool", lambda *, name, **_kwargs: _source_data(name))

    result = run_copilot_experiment(
        actor=actor,
        invoice_id=uuid4(),
        question="Vì sao hồ sơ bị chặn?",
        provider=provider,
    )

    assert result["status"] == "partial"
    assert result["multi_agent"]["status"] == "failed"
    assert result["multi_agent"]["error_code"] == "M6_CITATION_INVALID"
    assert result["multi_agent"]["model_calls"] == 1


def test_experiment_never_uses_fixture_rows_as_ai_evidence(monkeypatch) -> None:
    provider = _provider()
    fixture_detail = _detail(fixture_mode=True)
    monkeypatch.setattr("app.copilot.experiment._read_invoice", lambda _org_id, _invoice_id: fixture_detail)

    result = run_copilot_experiment(
        actor=_principal(), invoice_id=uuid4(), question="Giải thích hồ sơ.", provider=provider
    )

    assert result["status"] == "blocked"
    assert result["error_code"] == "M6_FIXTURE_INPUT_EXCLUDED"
    assert result["synthetic"] is True
    assert provider.calls == []


def test_experiment_aborts_if_invoice_snapshot_changes_between_arms(monkeypatch) -> None:
    actor = _principal()
    invoice_id = uuid4()
    initial = _detail()
    changed = _detail(version_hash="c" * 64)
    reads = 0
    provider = _provider()

    def current_detail(_org_id, _invoice_id):
        nonlocal reads
        reads += 1
        return changed if reads == 3 else initial

    monkeypatch.setattr("app.copilot.experiment._read_invoice", current_detail)
    monkeypatch.setattr("app.copilot.service._read_invoice", lambda _org_id, _invoice_id: initial)

    with pytest.raises(DomainError) as caught:
        run_copilot_experiment(
            actor=actor,
            invoice_id=invoice_id,
            question="Vì sao hồ sơ bị chặn?",
            provider=provider,
        )

    assert caught.value.code == "COPILOT_CONTEXT_CHANGED"
    assert len(provider.calls) == 1


def test_case_file_accepts_uuid_strings_and_rejects_invalid_rows(tmp_path) -> None:
    invoice_id = uuid4()
    case_path = tmp_path / "cases.json"
    case_path.write_text(
        json.dumps(
            [
                {
                    "sample_id": "heldout-001",
                    "invoice_id": str(invoice_id),
                    "question": "Explain the reconciliation status.",
                }
            ]
        ),
        encoding="utf-8",
    )
    args = m6_runner._parser().parse_args(["--case-file", str(case_path)])

    cases = m6_runner._load_cases(args)

    assert len(cases) == 1
    assert cases[0].sample_id == "heldout-001"
    assert cases[0].invoice_id == invoice_id

    case_path.write_text(
        json.dumps(
            [
                {
                    "sample_id": 1,
                    "invoice_id": str(invoice_id),
                    "question": "Explain the reconciliation status.",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(m6_runner.ValidationError):
        m6_runner._load_cases(args)
