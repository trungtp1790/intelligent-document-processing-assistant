from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from scripts import evaluate_ai_corpus as evaluator

from app.providers.protocol import ProviderCapabilities


def test_ai_corpus_is_grouped_by_layout_family_and_has_frozen_splits() -> None:
    manifest, _ = evaluator.validate_manifest(evaluator.DEFAULT_MANIFEST)
    samples = manifest["samples"]
    sample_counts = Counter(row["split"] for row in samples)
    families_by_split = {
        split: {row["layout_family"] for row in samples if row["split"] == split}
        for split in ("dev", "validation", "test")
    }

    assert sample_counts == {"dev": 30, "validation": 20, "test": 40}
    assert {split: len(families) for split, families in families_by_split.items()} == {
        "dev": 3,
        "validation": 2,
        "test": 4,
    }
    assert len({row["layout_family"] for row in samples}) == 9
    assert {row["modality"] for row in samples} == {"native_pdf_text", "scanned_pdf"}
    assert {row["language"] for row in samples} == {"vi-VN", "en-US"}
    assert {row["page_count"] for row in samples} == {1, 2}
    assert all(Path(evaluator.DEFAULT_MANIFEST.parent, row["label_path"]).is_file() for row in samples)
    assert all(row["source_document_id"] for row in samples)
    assert all(row["synthetic"] is True for row in samples)
    assert all(
        row["label_status"] == "programmatic_reference_not_human_annotated" for row in samples
    )


def test_eval_report_keeps_runtime_blocked_samples_out_of_ai_accuracy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setattr(evaluator.settings, "allow_live_model_calls", True)
    monkeypatch.setattr(evaluator.settings, "extraction_provider", "fixture")

    class NoModelCalls:
        name = "bedrock"

        @staticmethod
        def capabilities() -> ProviderCapabilities:
            return ProviderCapabilities(True, True, 10, 15 * 1024 * 1024, "tool_schema", "test-model")

        def __init__(self) -> None:
            pass

        def extract_invoice(self, *_args, **_kwargs):
            pytest.fail("A fixture-selected evaluation must never make a model call")

    monkeypatch.setattr(evaluator, "configured_provider", NoModelCalls)
    report_path = tmp_path / "report.json"
    report = evaluator.evaluate(
        manifest_path=evaluator.DEFAULT_MANIFEST,
        split="validation",
        report_path=report_path,
        live=True,
        confirm_held_out=False,
    )

    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert report["sample_count"] == report["processed_sample_count"] == 20
    assert report["blocked_sample_count"] == 20
    assert report["failure_count"] == 0
    assert report["model_input_attempted_sample_count"] == 0
    assert report["schema_valid_denominator"] == 0
    assert report["accuracy_status"] == "NOT_MEASURED"
    assert report["accuracy"]["header_exact_match"] is None
    assert report["dataset"]["fixture_accuracy_included"] is False
    assert report["canonical_label_rule_regression"]["ai_accuracy_included"] is False
    assert report["error_analysis"]["blocked_counts"] == {"EXTRACTION_PROVIDER_NOT_LIVE_MODEL": 20}


def test_held_out_evaluation_requires_explicit_confirmation(tmp_path: Path) -> None:
    report_path = tmp_path / "held-out.json"
    with pytest.raises(ValueError, match="--confirm-held-out"):
        evaluator.evaluate(
            manifest_path=tmp_path / "manifest-is-not-read.json",
            split="test",
            report_path=report_path,
            live=True,
            confirm_held_out=False,
        )
    assert not report_path.exists()


def test_held_out_provider_failures_still_mark_the_split_consumed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setattr(evaluator.settings, "allow_live_model_calls", True)
    monkeypatch.setattr(evaluator.settings, "extraction_provider", "bedrock")
    monkeypatch.setattr(evaluator.settings, "aws_region", "us-east-1")
    monkeypatch.setattr(evaluator.settings, "bedrock_model_id", "test-model")

    class FailingProvider:
        name = "bedrock"

        @staticmethod
        def capabilities() -> ProviderCapabilities:
            return ProviderCapabilities(True, True, 10, 15 * 1024 * 1024, "tool_schema", "test-model")

        def __init__(self) -> None:
            pass

        def extract_invoice(self, *_args, **_kwargs):
            raise evaluator.ProviderFailure(
                "PROVIDER_TIMEOUT", "simulated contract failure", retryable=True
            )

    monkeypatch.setattr(evaluator, "configured_provider", FailingProvider)
    report = evaluator.evaluate(
        manifest_path=evaluator.DEFAULT_MANIFEST,
        split="test",
        report_path=tmp_path / "held-out-failures.json",
        live=True,
        confirm_held_out=True,
    )

    assert report["sample_count"] == report["failure_count"] == 40
    assert report["model_input_attempted_sample_count"] == 40
    assert report["selection"]["held_out_test_consumed"] is True
    assert report["error_analysis"]["failure_counts"] == {"PROVIDER_TIMEOUT": 40}
    assert report["accuracy_status"] == "NOT_MEASURED"


def test_foundry_uses_shared_eval_contract_without_fabricating_model_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setattr(evaluator.settings, "allow_live_model_calls", True)
    monkeypatch.setattr(evaluator.settings, "extraction_provider", "foundry")
    monkeypatch.setattr(
        evaluator.settings, "foundry_model_endpoint", "https://test.services.ai.azure.com/openai/v1/"
    )
    monkeypatch.setattr(evaluator.settings, "foundry_deployment", "foundry-eval-deployment")

    class ProviderFailureOnly:
        name = "foundry"

        @staticmethod
        def capabilities() -> ProviderCapabilities:
            return ProviderCapabilities(
                True, True, 10, 15 * 1024 * 1024, "native_schema", "foundry-eval-deployment"
            )

        def extract_invoice(self, *_args, **_kwargs):
            raise evaluator.ProviderFailure("PROVIDER_TIMEOUT", "contract timeout", retryable=True)

    monkeypatch.setattr(evaluator, "configured_provider", ProviderFailureOnly)
    report = evaluator.evaluate(
        manifest_path=evaluator.DEFAULT_MANIFEST,
        split="validation",
        report_path=tmp_path / "foundry-validation.json",
        live=True,
        confirm_held_out=False,
    )

    assert report["configuration"]["requested_provider"] == "foundry"
    assert report["configuration"]["model_id"] == "foundry-eval-deployment"
    assert report["configuration"]["live_preflight_block_code"] is None
    assert report["model_input_attempted_sample_count"] == 20
    assert report["failure_count"] == 20
    assert report["completed_sample_count"] == 0
    assert report["accuracy_status"] == "NOT_MEASURED"
    assert report["error_analysis"]["failure_counts"] == {"PROVIDER_TIMEOUT": 20}
