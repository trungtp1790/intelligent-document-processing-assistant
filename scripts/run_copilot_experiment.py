from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" if (ROOT / "backend").is_dir() else ROOT))

from fastapi import HTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402

from app.config import settings  # noqa: E402
from app.copilot.experiment import (  # noqa: E402
    BASELINE_PROMPT_VERSION,
    M6_PROMPT_VERSION,
    run_copilot_experiment,
)
from app.copilot.service import MAX_ROUNDS, MAX_TOOL_CALLS, create_provider  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.domain.errors import DomainError  # noqa: E402
from app.main import current_principal  # noqa: E402
from app.providers.errors import ProviderFailure  # noqa: E402

DEFAULT_REPORT = ROOT / "evals" / "reports" / "copilot_m6_latest.json"
TOKEN_ENV = "IDP_COPILOT_EXPERIMENT_TOKEN"
SAMPLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
LOCK_PATH = ROOT / "backend" / "requirements.lock"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvaluationCase(_StrictModel):
    sample_id: str = Field(min_length=1, max_length=64)
    invoice_id: UUID
    question: str = Field(min_length=3, max_length=2_000)


def _price(value: str) -> Decimal:
    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Price must be a decimal number.") from exc
    if not price.is_finite() or price < 0:
        raise argparse.ArgumentTypeError("Price must be finite and non-negative.")
    return price


def _configuration_blockers(args: argparse.Namespace) -> list[str]:
    blockers: list[str] = []
    if not args.live:
        blockers.append("LIVE_FLAG_REQUIRED")
    if not args.confirm_cloud_data:
        blockers.append("CLOUD_DATA_CONFIRMATION_REQUIRED")
    if args.persist_private_review_bundle and (not args.live or not args.confirm_cloud_data):
        blockers.append("PRIVATE_REVIEW_BUNDLE_REQUIRES_LIVE")
    if settings.copilot_provider != "foundry":
        blockers.append("COPILOT_PROVIDER_DISABLED")
    if not settings.allow_live_model_calls:
        blockers.append("LIVE_CALLS_DISABLED")
    if not settings.foundry_model_endpoint or not settings.foundry_deployment:
        blockers.append("PROVIDER_NOT_CONFIGURED")
    if args.live and not args.model_family:
        blockers.append("MODEL_FAMILY_REQUIRED")
    if not (args.preflight or args.case_file or args.invoice_id):
        blockers.append("CASE_INPUT_REQUIRED")
    if not args.preflight and not args.case_file and not os_environ(TOKEN_ENV):
        blockers.append("AUTH_TOKEN_MISSING")
    if args.preflight:
        if args.case_file or args.invoice_id or args.sample_id or args.question:
            blockers.append("PREFLIGHT_CASE_INPUT_CONFLICT")
        return blockers
    if args.case_file and (args.invoice_id or args.sample_id or args.question):
        blockers.append("CASE_INPUT_CONFLICT")
    if not args.case_file and args.invoice_id and not args.sample_id:
        blockers.append("SAMPLE_ID_REQUIRED")
    if not args.case_file and args.invoice_id and not args.question:
        blockers.append("QUESTION_REQUIRED")
    return blockers


def os_environ(name: str) -> str | None:
    # Kept as a function so tests can replace environment lookup without exposing values.
    return os.environ.get(name)


def _load_cases(args: argparse.Namespace) -> list[EvaluationCase]:
    if args.preflight:
        return []
    if args.case_file:
        payload = json.loads(args.case_file.read_text(encoding="utf-8"))
        rows = payload.get("cases") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
            raise ValueError("Case file must contain between 1 and 20 cases.")
        cases = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("invoice_id"), str):
                raise ValueError("Each case must include an invoice_id UUID string.")
            try:
                parsed_invoice_id = UUID(row["invoice_id"])
            except ValueError as exc:
                raise ValueError("Each case must include a valid invoice_id UUID.") from exc
            cases.append(
                EvaluationCase.model_validate({**row, "invoice_id": parsed_invoice_id}, strict=True)
            )
    else:
        cases = [EvaluationCase(sample_id=args.sample_id, invoice_id=args.invoice_id, question=args.question)]
    sample_ids = [case.sample_id for case in cases]
    if any(not SAMPLE_ID_PATTERN.fullmatch(sample_id) for sample_id in sample_ids):
        raise ValueError("sample_id may contain only letters, numbers, '.', '_' and '-'.")
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("sample_id values must be unique.")
    return cases


def _safe_actor(token: str):
    with SessionLocal() as db:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        return current_principal(credentials=credentials, db=db)


def _lock_hash() -> str | None:
    for path in (LOCK_PATH, ROOT / "requirements.lock"):
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return None


def _empty_arm(code: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "error_code": code,
        "completion_attempts": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
        "elapsed_ms": 0,
        "model_latency_ms": 0,
        "answer_chars": None,
        "citation_ids_valid": None,
        "citation_count": None,
        "roles": [],
    }


def _cost(arm: dict[str, Any], input_price: Decimal | None, output_price: Decimal | None) -> str | None:
    if input_price is None or output_price is None:
        return None
    input_tokens, output_tokens = arm.get("input_tokens"), arm.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    amount = (input_price * input_tokens + output_price * output_tokens) / Decimal(1_000_000)
    return str(amount.quantize(Decimal("0.00000001")))


def _report(
    *,
    args: argparse.Namespace,
    cases: list[EvaluationCase],
    blockers: list[str],
    rows: list[dict[str, Any]],
    input_price: Decimal | None,
    output_price: Decimal | None,
    provider_completion_attempts: int,
    run_id: str,
    private_review_bundle: str | None,
    model_family: str | None,
) -> dict[str, Any]:
    statuses = Counter(row["status"] for row in rows)
    errors = Counter(
        f"{arm}:{metrics['error_code']}"
        for row in rows
        for arm, metrics in (
            ("single_agent", row.get("single_agent") or {}),
            ("multi_agent", row.get("multi_agent") or {}),
        )
        if metrics.get("error_code")
    )
    for row in rows:
        for arm in ("single_agent", "multi_agent"):
            if row.get(arm):
                row[arm]["estimated_cost_usd"] = _cost(row[arm], input_price, output_price)
    if provider_completion_attempts:
        execution_status = "COMPLETED" if statuses.get("complete") == len(rows) else "PARTIAL"
    else:
        execution_status = "LIVE_PENDING"
    return {
        "report_version": "1.0",
        "evaluation_type": "m6_single_vs_multi_agent_experiment",
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "execution_status": execution_status,
        "input_classification": "eligible_nonfixture_required; real-vs-synthetic is not inferred",
        "fixture_samples_included": 0,
        "fixture_accuracy_included": False,
        "answer_quality_status": "NOT_MEASURED",
        "human_review_status": "NOT_COLLECTED",
        "dataset": {
            "dataset_id": "operator_supplied_nonfixture_invoices",
            "sample_count": len(cases),
            "held_out_test_claimed": False,
            "sample_content_persisted": False,
        },
        "sample_count": len(cases),
        "complete_count": statuses.get("complete", 0),
        "partial_count": statuses.get("partial", 0),
        "blocked_count": statuses.get("blocked", 0),
        "failure_count": statuses.get("failed", 0),
        "provider_completion_attempt_count": provider_completion_attempts,
        "model_response_count": sum(
            (row.get(arm) or {}).get("model_calls", 0) or 0
            for row in rows
            for arm in ("single_agent", "multi_agent")
        ),
        "preflight_blockers": blockers,
        "configuration": {
            "provider": settings.copilot_provider,
            "deployment_alias": settings.foundry_deployment or None,
            "model_family": model_family,
            "model_family_note": "Operator supplied; never inferred from the deployment alias.",
            "temperature": settings.foundry_temperature,
            "max_output_tokens_per_completion": settings.copilot_max_output_tokens,
            "arm_deadline_seconds": settings.copilot_timeout_seconds,
            "single_agent_prompt_version": BASELINE_PROMPT_VERSION,
            "multi_agent_prompt_version": M6_PROMPT_VERSION,
            "single_agent_budget": {"max_model_rounds": MAX_ROUNDS, "max_read_tool_calls": MAX_TOOL_CALLS},
            "multi_agent_budget": {
                "max_model_calls": MAX_ROUNDS,
                "planned_model_calls": 3,
                "max_read_tool_calls": MAX_TOOL_CALLS,
                "planned_read_tool_calls": 4,
                "roles": ["document_reader", "reconciliation_analyst", "explainer_citation_checker"],
            },
            "retry_policy": "no experiment retry; Foundry SDK retries disabled",
            "pricing_usd_per_million_tokens": (
                {"input": str(input_price), "output": str(output_price)}
                if input_price is not None and output_price is not None
                else None
            ),
            "pricing_note": "Cost is omitted unless both explicit rates are supplied; no rate is guessed.",
            "dependency_lock_sha256": _lock_hash(),
        },
        "aggregate_metrics": {
            "single_agent": _aggregate(rows, "single_agent"),
            "multi_agent": _aggregate(rows, "multi_agent"),
        },
        "error_analysis": {"counts": dict(sorted(errors.items()))},
        "private_review_bundle": private_review_bundle,
        "private_review_bundle_policy": (
            "Local-only under ignored var/; includes question, model answers, and evidence quotes. "
            "Never commit or share without authorization."
            if private_review_bundle
            else "No question, answer, document, or tool content was persisted."
        ),
        "samples": rows,
        "limitations": [
            "Automated source-ID validation does not measure answer correctness or citation entailment.",
            "Human review is required before making a quality comparison.",
            "Fixture records are blocked before any provider call and excluded from the experiment.",
            "A blocked or preflight run has no AI accuracy result.",
        ],
    }


def _aggregate(rows: list[dict[str, Any]], arm_name: str) -> dict[str, Any]:
    measured = [
        row[arm_name]
        for row in rows
        if row.get(arm_name) and row[arm_name].get("status") == "complete"
    ]
    if not measured:
        return {
            "measured_sample_count": 0,
            "model_calls": 0,
            "read_tool_calls": 0,
            "input_tokens": None,
            "output_tokens": None,
            "mean_elapsed_ms": None,
            "mean_model_latency_ms": None,
            "answer_quality": "NOT_MEASURED",
        }
    return {
        "measured_sample_count": len(measured),
        "model_calls": sum(item["model_calls"] for item in measured),
        "read_tool_calls": sum(item["tool_calls"] for item in measured),
        "input_tokens": sum(item["input_tokens"] for item in measured)
        if all(item["input_tokens"] is not None for item in measured)
        else None,
        "output_tokens": sum(item["output_tokens"] for item in measured)
        if all(item["output_tokens"] is not None for item in measured)
        else None,
        "mean_elapsed_ms": round(sum(item["elapsed_ms"] for item in measured) / len(measured), 2),
        "mean_model_latency_ms": round(
            sum(item["model_latency_ms"] for item in measured) / len(measured), 2
        ),
        "answer_quality": "NOT_MEASURED",
    }


def _blocked_rows(cases: list[EvaluationCase], code: str) -> list[dict[str, Any]]:
    return [
        {
            "sample_id": case.sample_id,
            "status": "blocked",
            "error_code": code,
            "single_agent": _empty_arm(code),
            "multi_agent": _empty_arm(code),
        }
        for case in cases
    ]


def _write_private_bundle(run_id: str, entries: list[dict[str, Any]]) -> str:
    relative = Path("var") / "copilot_m6_reviews" / f"{run_id}.json"
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"run_id": run_id, "samples": entries}, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return relative.as_posix()


class _CountingProvider:
    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped
        self.name = wrapped.name
        self.completion_attempts = 0

    def complete(self, **kwargs: Any):
        self.completion_attempts += 1
        return self.wrapped.complete(**kwargs)

    def close(self) -> None:
        close = getattr(self.wrapped, "close", None)
        if callable(close):
            close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare the M5c single-agent copilot with the isolated M6 three-role experiment."
    )
    parser.add_argument("--preflight", action="store_true", help="Write a no-network configuration report.")
    parser.add_argument(
        "--live", action="store_true", help="Allow live Foundry requests for eligible invoices."
    )
    parser.add_argument(
        "--confirm-cloud-data",
        action="store_true",
        help="Confirm the selected nonfixture content may be sent to Foundry.",
    )
    parser.add_argument(
        "--persist-private-review-bundle",
        action="store_true",
        help="Write answers and bounded evidence quotes under ignored var/ for manual scoring.",
    )
    parser.add_argument(
        "--case-file",
        type=Path,
        help="Private JSON file of 1–20 {sample_id, invoice_id, question} cases.",
    )
    parser.add_argument(
        "--invoice-id", type=UUID, help="One invoice UUID, scoped to the bearer token's organization."
    )
    parser.add_argument("--sample-id", help="Non-sensitive label stored in the aggregate report.")
    parser.add_argument("--question", help="Question used identically by both experiment arms.")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT, help="Aggregate JSON report path.")
    parser.add_argument("--input-price-usd-per-million", type=_price)
    parser.add_argument("--output-price-usd-per-million", type=_price)
    parser.add_argument(
        "--model-family", help="Operator-supplied model family deployed behind the Foundry alias."
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if (args.input_price_usd_per_million is None) != (args.output_price_usd_per_million is None):
        print("Both input and output token prices are required together.", file=sys.stderr)
        return 2
    try:
        cases = _load_cases(args)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        print(f"Invalid experiment case input ({type(exc).__name__}).", file=sys.stderr)
        return 2

    blockers = _configuration_blockers(args)
    rows: list[dict[str, Any]] = []
    private_review_entries: list[dict[str, Any]] = []
    provider_completion_attempts = 0
    if blockers:
        if cases:
            first = blockers[0]
            rows = _blocked_rows(cases, first)
    elif cases:
        token = os_environ(TOKEN_ENV)
        if not token:
            blockers.append("AUTH_TOKEN_MISSING")
            rows = _blocked_rows(cases, "AUTH_TOKEN_MISSING")
        else:
            try:
                actor = _safe_actor(token)
            except HTTPException:
                blockers.append("AUTH_TOKEN_INVALID_OR_EXPIRED")
                rows = _blocked_rows(cases, "AUTH_TOKEN_INVALID_OR_EXPIRED")
            else:
                provider = _CountingProvider(create_provider())
                try:
                    for case in cases:
                        attempts_before = provider.completion_attempts
                        try:
                            metrics = run_copilot_experiment(
                                actor=actor,
                                invoice_id=case.invoice_id,
                                question=case.question,
                                provider=provider,
                                include_private_review=args.persist_private_review_bundle,
                            )
                            private_review = metrics.pop("private_review", None)
                            row = {"sample_id": case.sample_id, **metrics}
                            if args.persist_private_review_bundle and private_review is not None:
                                private_review_entries.append(
                                    {"sample_id": case.sample_id, **private_review}
                                )
                        except (DomainError, ProviderFailure) as exc:
                            row = {
                                "sample_id": case.sample_id,
                                "status": "failed",
                                "error_code": exc.code,
                                "single_agent": None,
                                "multi_agent": None,
                            }
                        except Exception:
                            row = {
                                "sample_id": case.sample_id,
                                "status": "failed",
                                "error_code": "EXPERIMENT_INTERNAL_ERROR",
                                "single_agent": None,
                                "multi_agent": None,
                            }
                        call_attempts = provider.completion_attempts - attempts_before
                        provider_completion_attempts += call_attempts
                        row["provider_completion_attempts"] = call_attempts
                        rows.append(row)
                finally:
                    close = getattr(provider, "close", None)
                    if callable(close):
                        close()

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    private_review_bundle = (
        _write_private_bundle(run_id, private_review_entries)
        if args.persist_private_review_bundle and private_review_entries
        else None
    )
    report = _report(
        args=args,
        cases=cases,
        blockers=blockers,
        rows=rows,
        input_price=args.input_price_usd_per_million,
        output_price=args.output_price_usd_per_million,
        provider_completion_attempts=provider_completion_attempts,
        run_id=run_id,
        private_review_bundle=private_review_bundle,
        model_family=args.model_family,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"M6 report: {args.output}; status={report['execution_status']}; "
        f"n={report['sample_count']}; model responses={report['model_response_count']}; "
        f"answer quality={report['answer_quality_status']}"
    )
    return 0 if args.preflight or report["execution_status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
