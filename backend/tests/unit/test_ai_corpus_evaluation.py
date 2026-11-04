from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from scripts import evaluate_ai_corpus as evaluator

from app.providers.azure_document_intelligence import (
    AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
    AZURE_DOCUMENT_INTELLIGENCE_MODEL,
    AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION,
    AzureDocumentIntelligenceProvider,
)
from app.providers.errors import ProviderFailure
from app.providers.protocol import ProviderCapabilities


def _install_single_synthetic_sample(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict, bytes]:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    content = stream.getvalue()
    sample_path = tmp_path / "generated-routing-test-only.pdf"
    sample_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    row = {
        "sample_id": "synthetic-routing-check-001",
        "file": sample_path.name,
        "sha256": digest,
        "split": "validation",
        "layout_family": "test-only-family",
        "modality": "scanned_pdf",
        "language": "vi-VN",
        "page_count": 1,
        "synthetic": True,
        "labels": {},
        "po_reference": {"po_number": "PO-TEST-001", "currency": "VND", "lines": []},
        "expected_findings": [],
    }
    manifest = {
        "dataset_id": "offline-azure-route-test",
        "dataset_type": "ai_extraction_evaluation",
        "fixtures_included": False,
        "synthetic_only": True,
        "label_status": "programmatic_reference_not_human_annotated",
        "samples": [row],
        "split_policy": {"validation": {"sample_count": 1, "layout_family_count": 1}},
    }
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setattr(evaluator, "validate_manifest", lambda _path: (manifest, b"test-only-manifest"))
    monkeypatch.setattr(evaluator, "_reconcile_labels", lambda _invoice, _po: {"issues": []})
    return manifest, content


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


def test_azure_evaluation_offline_does_not_construct_client_or_send_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_single_synthetic_sample(monkeypatch, tmp_path)
    monkeypatch.setattr(evaluator.settings, "extraction_provider", "azure_document_intelligence")
    monkeypatch.setattr(evaluator.settings, "allow_live_model_calls", False)
    monkeypatch.setattr(evaluator.settings, "document_intelligence_endpoint", "")
    created: list[AzureDocumentIntelligenceProvider] = []

    def configured_azure_provider() -> AzureDocumentIntelligenceProvider:
        provider = AzureDocumentIntelligenceProvider()
        created.append(provider)
        return provider

    def forbidden_client_creation(_provider: AzureDocumentIntelligenceProvider):
        pytest.fail("offline evaluation must not construct an Azure SDK client")

    def forbidden_request(*_args, **_kwargs):
        pytest.fail("offline evaluation must not call the Azure provider")

    monkeypatch.setattr(evaluator, "configured_provider", configured_azure_provider)
    monkeypatch.setattr(AzureDocumentIntelligenceProvider, "_make_client", forbidden_client_creation)
    monkeypatch.setattr(AzureDocumentIntelligenceProvider, "extract_invoice", forbidden_request)

    report_path = tmp_path / "reports" / "offline-azure.json"
    report = evaluator.evaluate(
        manifest_path=tmp_path / "manifest.json",
        split="validation",
        report_path=report_path,
        live=False,
        confirm_held_out=False,
    )

    assert created and created[0].client is None
    assert report["configuration"]["requested_provider"] == "azure_document_intelligence"
    assert report["configuration"]["live_preflight_block_code"] == "LIVE_RUN_NOT_REQUESTED"
    assert report["sample_count"] == report["blocked_sample_count"] == 1
    assert report["model_input_attempted_sample_count"] == 0
    assert report["live_model_requests_started"] == 0
    assert report["accuracy_status"] == "NOT_MEASURED"
    assert report["samples"][0]["latency_ms"] is None
    assert report["runtime"]["input_tokens"] is None
    assert report["runtime"]["output_tokens"] is None
    assert report["runtime"]["cost_usd"] is None
    assert report["runtime"]["provider_usage_status"] == "NOT_REPORTED"
    assert report["runtime"]["model_calls"] is None
    assert report["runtime"]["provider_invocations"] == 0


@pytest.mark.parametrize(
    ("live", "allow_live", "endpoint", "sdk_version", "expected"),
    [
        (False, False, "", None, "LIVE_RUN_NOT_REQUESTED"),
        (True, False, "", None, "LIVE_CALLS_DISABLED"),
        (True, True, "", AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION, "PROVIDER_NOT_CONFIGURED"),
        (
            True,
            True,
            "http://idp-resource.cognitiveservices.azure.com",
            AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION,
            "PROVIDER_NOT_CONFIGURED",
        ),
        (
            True,
            True,
            "https://idp-resource.cognitiveservices.azure.com",
            None,
            "PROVIDER_SDK_UNAVAILABLE",
        ),
        (
            True,
            True,
            "https://idp-resource.cognitiveservices.azure.com",
            "0.0.0",
            "PROVIDER_SDK_VERSION_MISMATCH",
        ),
        (
            True,
            True,
            "https://idp-resource.cognitiveservices.azure.com",
            AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION,
            None,
        ),
    ],
)
def test_azure_live_preflight_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    live: bool,
    allow_live: bool,
    endpoint: str,
    sdk_version: str | None,
    expected: str | None,
) -> None:
    monkeypatch.setattr(evaluator.settings, "extraction_provider", "azure_document_intelligence")
    monkeypatch.setattr(evaluator.settings, "allow_live_model_calls", allow_live)
    monkeypatch.setattr(evaluator.settings, "document_intelligence_endpoint", endpoint)
    monkeypatch.setattr(evaluator, "_installed_azure_sdk_version", lambda: sdk_version)

    assert evaluator._live_block_code(evaluator.argparse.Namespace(live=live)) == expected


def test_azure_evaluator_routes_through_fake_client_without_accuracy_or_fake_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _manifest, content = _install_single_synthetic_sample(monkeypatch, tmp_path)
    monkeypatch.setattr(evaluator.settings, "extraction_provider", "azure_document_intelligence")
    monkeypatch.setattr(evaluator.settings, "allow_live_model_calls", True)
    monkeypatch.setattr(
        evaluator.settings,
        "document_intelligence_endpoint",
        "https://idp-resource.cognitiveservices.azure.com",
    )
    monkeypatch.setattr(
        evaluator, "_installed_azure_sdk_version", lambda: AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION
    )

    class FakeAzureClient:
        def __init__(self) -> None:
            self.models: list[str] = []
            self.documents: list[bytes] = []
            self.locales: list[str] = []

        def begin_analyze_document(self, model_id: str, **kwargs):
            self.models.append(model_id)
            self.documents.append(kwargs["body"].read())
            self.locales.append(kwargs["locale"])
            raise ProviderFailure(
                "PROVIDER_THROTTLED", "offline fake 429", retryable=True, status_code=429
            )

    fake_client = FakeAzureClient()
    azure_provider = AzureDocumentIntelligenceProvider(client=fake_client)
    monkeypatch.setattr(evaluator, "configured_provider", lambda: azure_provider)

    report_path = tmp_path / "reports" / "fake-azure-contract.json"
    report = evaluator.evaluate(
        manifest_path=tmp_path / "manifest.json",
        split="validation",
        report_path=report_path,
        live=True,
        confirm_held_out=False,
    )

    assert fake_client.models == [AZURE_DOCUMENT_INTELLIGENCE_MODEL]
    assert fake_client.documents == [content]
    assert fake_client.locales == ["vi"]
    assert report["configuration"]["live_preflight_block_code"] is None
    assert report["configuration"]["provider_versions"] == {
        "sdk_name": "azure-ai-documentintelligence",
        "sdk_version": AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION,
        "sdk_required_version": AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION,
        "api_version": AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
        "model_id": AZURE_DOCUMENT_INTELLIGENCE_MODEL,
        "model_version": None,
        "model_version_reason": "The provider response does not expose an immutable prebuilt model build.",
    }
    assert report["sample_count"] == report["model_input_attempted_sample_count"] == 1
    assert report["live_model_requests_started"] == 1
    assert report["failure_count"] == 1
    assert report["model_output_count"] == 0
    assert report["completed_sample_count"] == 0
    assert report["accuracy_status"] == "NOT_MEASURED"
    assert report["runtime"]["model_latency_median_ms"] is None
    assert report["runtime"]["input_tokens"] is None
    assert report["runtime"]["output_tokens"] is None
    assert report["runtime"]["cost_usd"] is None
    assert report["runtime"]["provider_usage_status"] == "NOT_REPORTED"
    assert report["runtime"]["model_calls"] is None
    assert report["runtime"]["provider_invocations"] == 1
    prediction_path = tmp_path / report["private_predictions_ref"]
    assert prediction_path.stat().st_size == 0


def _generated_fixture_inputs(tmp_path: Path) -> tuple[Path, Path, dict]:
    stream = io.BytesIO()
    image = Image.new("RGB", (850, 1100), "white")
    draw = ImageDraw.Draw(image)
    for line_number, text in enumerate(
        (
            "TEST GENERATED SYNTHETIC INVOICE",
            "Invoice: SMOKE-001    Date: 2026-11-04",
            "Supplier: Synthetic Supplier  Tax ID: 0100000000",
            "Purchase order: PO-SMOKE-001",
            "SYN-001  Synthetic widget  2 piece x 50000 = 100000 VND",
            "Subtotal: 100000 VND    Tax: 10000 VND    Total: 110000 VND",
        ),
        start=1,
    ):
        draw.text((45, 55 * line_number), text, fill="black")
    image.save(stream, format="PDF", resolution=150)
    content = stream.getvalue()
    sample_path = tmp_path / "generated-synthetic-invoice.pdf"
    sample_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    invoice = {
        "schema_version": "1.0",
        "invoice_number": "SMOKE-001",
        "invoice_series": "SYN",
        "invoice_series_absent_confirmed": False,
        "invoice_date": "2026-11-04",
        "supplier_tax_id": "0100000000",
        "supplier_name": "Synthetic Supplier",
        "po_number": "PO-SMOKE-001",
        "currency": "VND",
        "subtotal": "100000",
        "tax_total": "10000",
        "grand_total": "110000",
        "discount_total": "0",
        "shipping_total": "0",
        "lines": [
            {
                "sku": "SYN-001",
                "description": "Synthetic widget",
                "unit": "piece",
                "quantity": "2",
                "unit_price": "50000",
                "net_amount": "100000",
            }
        ],
    }
    purchase_order = {
        "po_number": "PO-SMOKE-001",
        "supplier_tax_id": "0100000000",
        "currency": "VND",
        "state": "OPEN",
        "lines": [
            {
                "sku": "SYN-001",
                "description": "Synthetic widget",
                "unit": "piece",
                "quantity": "2",
                "unit_price": "50000",
            }
        ],
    }
    manifest = {
        "dataset_id": "test-generated-offline-smoke",
        "dataset_type": "fixture_offline_pipeline_smoke",
        "synthetic_only": True,
        "accuracy_eligible": False,
        "fixtures": [
            {
                "sha256": digest,
                "synthetic": True,
                "extraction": invoice,
                "evidence_texts": {},
                "evidence_pages": {},
            }
        ],
        "samples": [
            {
                "sample_id": "test-generated-sample-001",
                "file": sample_path.name,
                "sha256": digest,
                "page_count": 1,
                "language": "vi-VN",
                "modality": "scanned_pdf",
                "layout_family": "test-generated-layout",
                "synthetic": True,
                "label_status": "test_generated",
                "labels": invoice,
                "po_reference": purchase_order,
                "expected_findings": [],
            }
        ],
    }
    manifest_path = tmp_path / "synthetic-fixture-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = tmp_path / "reports" / "offline-fixture-smoke.json"
    return manifest_path, report_path, manifest


def _offline_cli_environment(*, provider: str, allow_live: bool = False) -> dict[str, str]:
    allowed_environment = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC")
    environment = {key: os.environ[key] for key in allowed_environment if key in os.environ}
    environment.update(
        {
            "EXTRACTION_PROVIDER": provider,
            "ALLOW_LIVE_MODEL_CALLS": "true" if allow_live else "false",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def test_evaluator_cli_runs_fixture_pipeline_on_generated_synthetic_invoice_and_po(
    tmp_path: Path,
) -> None:
    manifest_path, report_path, _manifest = _generated_fixture_inputs(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(evaluator.__file__).resolve()),
            "--offline-fixture-smoke",
            "--manifest",
            str(manifest_path),
            "--report",
            str(report_path),
        ],
        cwd=tmp_path,
        env=_offline_cli_environment(provider="fixture"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "provider=fixture" in completed.stdout
    assert "accuracy=NOT_MEASURED" in completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["report_schema_version"] == "1.0"
    assert report["evaluation_type"] == "fixture_offline_pipeline_smoke"
    assert report["classification"] == "SYNTHETIC_FIXTURE_PLUMBING_ONLY"
    assert report["accuracy_status"] == "NOT_MEASURED"
    assert report["sample_count"] == report["processed_sample_count"] == 1
    assert report["completed_sample_count"] == 1
    assert report["failure_count"] == 0
    assert report["provider"] == {
        "name": "fixture",
        "mode": "offline",
        "sdk_name": None,
        "sdk_version": None,
        "api_version": None,
        "model_id": None,
        "model_version": None,
    }
    assert report["dataset"]["synthetic_only"] is True
    assert report["dataset"]["accuracy_eligible"] is False
    assert report["metrics"]["ai_accuracy"] == {
        "status": "NOT_MEASURED",
        "value": None,
        "numerator": None,
        "denominator": 0,
    }
    assert report["metrics"]["fixture_result_schema_validity"] == {
        "valid_count": 1,
        "denominator": 1,
        "rate": 1.0,
    }
    assert report["metrics"]["synthetic_rule_regression"] == {
        "passed_count": 1,
        "denominator": 1,
        "pass_rate": 1.0,
        "ai_accuracy_included": False,
    }
    runtime = report["runtime"]
    assert runtime["fixture_pipeline_latency_median_ms"] >= 0
    assert runtime["model_latency_median_ms"] is None
    assert runtime["input_tokens"] is None
    assert runtime["output_tokens"] is None
    assert runtime["cost_usd"] is None
    assert runtime["cost_usd_source"] is None
    assert runtime["model_calls"] == 0
    assert runtime["provider_invocations"] == 1
    assert report["configuration"]["execution_mode"] == "offline"
    assert report["configuration"]["fixture_accuracy_included"] is False
    assert report["configuration"]["azure_live_status"] == "LIVE_PENDING"
    assert report["samples"][0]["routing_reason"] == "fixture_manifest"
    assert report["private_predictions_ref"] is None
    assert any("not Azure accuracy" in item for item in report["limitations"])


@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        (("--preflight-only",), "LIVE_RUN_NOT_REQUESTED"),
        (("--preflight-only", "--live"), "LIVE_CALLS_DISABLED"),
    ],
)
def test_cli_preflight_stops_before_provider_or_client_creation(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected_code: str,
) -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(evaluator.__file__).resolve()), *arguments],
        cwd=tmp_path,
        env=_offline_cli_environment(provider="azure_document_intelligence"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 2
    assert f"BLOCKED:{expected_code}" in completed.stdout
    assert "provider=azure_document_intelligence" in completed.stdout


@pytest.mark.parametrize(
    ("live", "allow_live", "expected_code"),
    [
        (False, True, "LIVE_RUN_NOT_REQUESTED"),
        (True, False, "LIVE_CALLS_DISABLED"),
    ],
)
def test_preflight_main_never_resolves_provider(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    live: bool,
    allow_live: bool,
    expected_code: str,
) -> None:
    monkeypatch.setattr(evaluator.settings, "extraction_provider", "azure_document_intelligence")
    monkeypatch.setattr(evaluator.settings, "allow_live_model_calls", allow_live)
    monkeypatch.setattr(
        evaluator,
        "configured_provider",
        lambda: pytest.fail("preflight must exit before provider or client construction"),
    )
    argv = ["evaluate_ai_corpus.py", "--preflight-only"]
    if live:
        argv.append("--live")
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as captured:
        evaluator.main()

    assert captured.value.code == 2
    assert f"BLOCKED:{expected_code}" in capsys.readouterr().out
