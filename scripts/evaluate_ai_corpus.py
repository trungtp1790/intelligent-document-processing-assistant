from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" if (ROOT / "backend").is_dir() else ROOT))

from app.config import settings  # noqa: E402
from app.domain.errors import DomainError  # noqa: E402
from app.documents.validation import validate_document  # noqa: E402
from app.domain.reconciliation import POLICY_VERSION, reconcile  # noqa: E402
from app.providers.azure_document_intelligence import (  # noqa: E402
    AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
    AZURE_DOCUMENT_INTELLIGENCE_MODEL,
    AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION,
    AzureDocumentIntelligenceProvider,
)
from app.providers.errors import ProviderFailure  # noqa: E402
from app.providers.fixture import FixtureProvider  # noqa: E402
from app.providers.factory import configured_provider  # noqa: E402
from app.services.extraction import _make_request, _normalized_text, _pdf_text_pages  # noqa: E402
from app.schemas import InvoiceData, POCreate  # noqa: E402

DEFAULT_MANIFEST = ROOT / "evals" / "datasets" / "synthetic_invoice_corpus_v1" / "manifest.json"
HEADER_FIELDS = (
    "invoice_number",
    "invoice_series",
    "invoice_series_absent_confirmed",
    "invoice_date",
    "supplier_tax_id",
    "supplier_name",
    "po_number",
    "currency",
    "subtotal",
    "tax_total",
    "grand_total",
    "discount_total",
    "shipping_total",
)
LINE_FIELDS = ("sku", "description", "unit", "quantity", "unit_price", "net_amount")
MONEY_FIELDS = {
    "subtotal",
    "tax_total",
    "grand_total",
    "discount_total",
    "shipping_total",
    "quantity",
    "unit_price",
    "net_amount",
}
EXPECTED_SPLITS = {"dev": (30, 3), "validation": (20, 2), "test": (40, 4)}


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _installed_azure_sdk_version() -> str | None:
    """Read local package metadata only; this must never initialize an Azure client."""
    try:
        return package_version("azure-ai-documentintelligence")
    except PackageNotFoundError:
        return None


def _provider_versions(provider: Any) -> dict[str, str | None]:
    if settings.extraction_provider != "azure_document_intelligence":
        return {
            "sdk_name": None,
            "sdk_version": None,
            "sdk_required_version": None,
            "api_version": None,
            "model_id": provider.capabilities().model_id,
            "model_version": None,
            "model_version_reason": None,
        }
    return {
        "sdk_name": "azure-ai-documentintelligence",
        "sdk_version": _installed_azure_sdk_version(),
        "sdk_required_version": AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION,
        "api_version": AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
        "model_id": provider.capabilities().model_id or AZURE_DOCUMENT_INTELLIGENCE_MODEL,
        # Azure's prebuilt response does not identify an immutable service build.
        "model_version": None,
        "model_version_reason": "The provider response does not expose an immutable prebuilt model build.",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_dataset_path(manifest_path: Path, relative_path: Any, sample_id: str, role: str) -> Path:
    root = manifest_path.parent.resolve()
    path = (root / str(relative_path or "")).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"{role} path escapes the dataset directory: {sample_id}")
    return path


def _resolved_sample_path(manifest_path: Path, row: dict[str, Any]) -> Path:
    return _resolved_dataset_path(manifest_path, row.get("file"), row["sample_id"], "sample")


def validate_manifest(manifest_path: Path) -> tuple[dict[str, Any], bytes]:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("dataset_type") != "ai_extraction_evaluation":
        raise ValueError("manifest is not an AI extraction evaluation dataset")
    if manifest.get("fixtures_included") is not False:
        raise ValueError("fixture/demo data cannot be included in AI accuracy evaluation")
    rows = manifest.get("samples")
    if not isinstance(rows, list) or not rows:
        raise ValueError("manifest has no samples")

    ids: set[str] = set()
    hashes: dict[str, str] = {}
    family_splits: dict[str, str] = {}
    source_splits: dict[str, str] = {}
    split_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        sample_id = row.get("sample_id")
        file_hash = row.get("sha256")
        family = row.get("layout_family")
        source_document_id = row.get("source_document_id")
        split = row.get("split")
        if not isinstance(sample_id, str) or not sample_id or sample_id in ids:
            raise ValueError(f"sample_id is missing or duplicated: {sample_id}")
        if not isinstance(file_hash, str) or len(file_hash) != 64:
            raise ValueError(f"sample has invalid SHA-256: {sample_id}")
        if file_hash in hashes:
            raise ValueError(f"duplicate sample hash: {sample_id} and {hashes[file_hash]}")
        if not isinstance(family, str) or not family:
            raise ValueError(f"sample has no layout family: {sample_id}")
        if not isinstance(source_document_id, str) or not source_document_id:
            raise ValueError(f"sample has no source_document_id: {sample_id}")
        if split not in EXPECTED_SPLITS:
            raise ValueError(f"sample has unsupported split {split}: {sample_id}")
        if family in family_splits and family_splits[family] != split:
            raise ValueError(f"layout family leakage across splits: {family}")
        family_splits[family] = split
        if source_document_id in source_splits and source_splits[source_document_id] != split:
            raise ValueError(f"source document variants leak across splits: {source_document_id}")
        source_splits[source_document_id] = split
        if not row.get("license_or_consent") or not row.get("source"):
            raise ValueError(f"sample lacks source/license-or-consent metadata: {sample_id}")
        if any(
            marker in str(row["source"]).casefold() for marker in ("fixture", "demo")
        ):
            raise ValueError(f"fixture/demo source cannot be included in AI accuracy: {sample_id}")
        if type(row.get("synthetic")) is not bool:
            raise ValueError(f"sample is missing an explicit synthetic flag: {sample_id}")
        if row.get("fixture_sample") is True or row.get("fixture_result") is not None:
            raise ValueError(f"fixture data cannot be included in AI accuracy: {sample_id}")
        if "manual_correction" in row or "post_manual_extraction" in row:
            raise ValueError(f"manual-correction output cannot be part of AI accuracy: {sample_id}")
        if not row.get("synthetic"):
            if row.get("label_status") != "human_verified_pre_manual_correction":
                raise ValueError(f"real AI labels must be verified before any manual correction: {sample_id}")
        elif row.get("label_status") != "programmatic_reference_not_human_annotated":
            raise ValueError(f"synthetic reference label provenance is missing: {sample_id}")

        if row.get("modality") not in {"native_pdf_text", "scanned_pdf"}:
            raise ValueError(f"sample has unsupported modality: {sample_id}")
        if row.get("language") not in {"vi-VN", "en-US"}:
            raise ValueError(f"sample has unsupported language: {sample_id}")
        if type(row.get("page_count")) is not int or row["page_count"] < 1:
            raise ValueError(f"sample has invalid page_count: {sample_id}")

        sample_path = _resolved_sample_path(manifest_path, row)
        if not sample_path.is_file():
            raise ValueError(f"sample document does not exist: {sample_id}")
        actual_hash = _sha256(sample_path)
        if actual_hash != file_hash:
            raise ValueError(f"document SHA-256 mismatch: {sample_id}")
        if sample_path.suffix.casefold() != ".pdf":
            raise ValueError(f"AI corpus supports PDF documents only: {sample_id}")
        content = sample_path.read_bytes()
        validated = validate_document(content)
        if validated.mime != "application/pdf" or validated.page_count != row.get("page_count"):
            raise ValueError(f"PDF validation disagrees with manifest metadata: {sample_id}")
        searchable = any(_normalized_text(page) for page in _pdf_text_pages(content))
        if row.get("modality") == "native_pdf_text" and not searchable:
            raise ValueError(f"native-text sample contains no searchable PDF text: {sample_id}")
        if row.get("modality") == "scanned_pdf" and searchable:
            raise ValueError(f"scanned sample unexpectedly contains searchable PDF text: {sample_id}")
        label_path = _resolved_dataset_path(
            manifest_path, row.get("label_path"), sample_id, "label"
        )
        label_hash = row.get("label_sha256")
        if not label_path.is_file() or not isinstance(label_hash, str) or len(label_hash) != 64:
            raise ValueError(f"sample label file or hash is missing: {sample_id}")
        if _sha256(label_path) != label_hash:
            raise ValueError(f"label SHA-256 mismatch: {sample_id}")
        labels = InvoiceData.model_validate(json.loads(label_path.read_text(encoding="utf-8"))).model_dump(
            mode="json"
        )
        row["labels"] = labels
        evidence_pages = row.get("evidence_pages")
        if (
            not isinstance(row.get("po_reference"), dict)
            or not isinstance(row.get("po_reference", {}).get("lines"), list)
            or not isinstance(row.get("expected_findings"), list)
            or any(not isinstance(code, str) for code in row.get("expected_findings", []))
            or not isinstance(evidence_pages, dict)
            or any(
                not isinstance(field, str)
                or type(page) is not int
                or not 1 <= page <= row["page_count"]
                for field, page in evidence_pages.items()
            )
        ):
            raise ValueError(f"sample is missing PO reference or expected findings: {sample_id}")
        ids.add(sample_id)
        hashes[file_hash] = sample_id
        split_rows[split].append(row)

    policy = manifest.get("split_policy", {})
    for split, (expected_count, expected_families) in EXPECTED_SPLITS.items():
        rows_for_split = split_rows.get(split, [])
        families = {row["layout_family"] for row in rows_for_split}
        declared = policy.get(split, {})
        if len(rows_for_split) != expected_count or declared.get("sample_count") != expected_count:
            raise ValueError(f"{split} split must have exactly {expected_count} samples")
        if len(families) != expected_families or declared.get("layout_family_count") != expected_families:
            raise ValueError(f"{split} split must have exactly {expected_families} layout families")
        if any(sum(row["layout_family"] == family for row in rows_for_split) != 10 for family in families):
            raise ValueError(f"every layout family in {split} must contain exactly 10 samples")

    return manifest, manifest_bytes


def _normal_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if field in MONEY_FIELDS:
        try:
            return Decimal(str(value)).normalize()
        except InvalidOperation:
            return str(value)
    if field == "invoice_date":
        try:
            from datetime import date

            return date.fromisoformat(str(value))
        except ValueError:
            return str(value)
    normalized = unicodedata.normalize("NFKC", str(value))
    return " ".join(normalized.split()).casefold()


def _line_matches(expected: list[dict[str, Any]], predicted: list[dict[str, Any]]) -> list[tuple[int | None, int | None]]:
    expected_skus = [line.get("sku") for line in expected]
    predicted_skus = [line.get("sku") for line in predicted]
    can_match_by_sku = (
        all(expected_skus)
        and all(predicted_skus)
        and len({str(value).casefold() for value in expected_skus}) == len(expected_skus)
        and len({str(value).casefold() for value in predicted_skus}) == len(predicted_skus)
    )
    matches: list[tuple[int | None, int | None]] = []
    if can_match_by_sku:
        predicted_by_sku = {str(line["sku"]).casefold(): index for index, line in enumerate(predicted)}
        paired: set[int] = set()
        for index, line in enumerate(expected):
            predicted_index = predicted_by_sku.get(str(line["sku"]).casefold())
            matches.append((index, predicted_index))
            if predicted_index is not None:
                paired.add(predicted_index)
        matches.extend((None, index) for index in range(len(predicted)) if index not in paired)
    else:
        common = min(len(expected), len(predicted))
        matches.extend((index, index) for index in range(common))
        matches.extend((index, None) for index in range(common, len(expected)))
        matches.extend((None, index) for index in range(common, len(predicted)))
    return matches


def _po_view(po_reference: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    po_lines = []
    mapping: dict[str, str] = {}
    seen: set[str] = set()
    for index, line in enumerate(po_reference.get("lines", []), start=1):
        po_id = f"po-line-{index}"
        po_lines.append({**line, "id": po_id, "line_no": index})
        sku = line.get("sku")
        if sku:
            normalized_sku = str(sku).casefold()
            if normalized_sku not in seen:
                mapping[normalized_sku] = po_id
            else:
                mapping.pop(normalized_sku, None)
            seen.add(normalized_sku)
    return (
        {
            "po_number": po_reference["po_number"],
            "currency": po_reference["currency"],
            "state": po_reference.get("state", "OPEN"),
            "supplier_tax_id": po_reference["supplier_tax_id"],
            "lines": po_lines,
        },
        mapping,
    )


def _reconcile_labels(invoice: dict[str, Any], po_reference: dict[str, Any]) -> dict[str, Any]:
    po, po_by_sku = _po_view(po_reference)
    labelled_invoice = dict(invoice)
    lines = []
    mapping: dict[str, str] = {}
    for index, line in enumerate(invoice.get("lines", []), start=1):
        line_id = f"invoice-line-{index}"
        lines.append({**line, "line_id": line_id})
        if line.get("sku"):
            po_line_id = po_by_sku.get(str(line["sku"]).casefold())
            if po_line_id:
                mapping[line_id] = po_line_id
    labelled_invoice["lines"] = lines
    return reconcile(labelled_invoice, po, mapping)


def _score_prediction(
    row: dict[str, Any], result: Any, source_text: dict[int, str]
) -> dict[str, Any]:
    expected = row["labels"]
    predicted = InvoiceData.model_validate(result.candidate).model_dump(mode="json")
    mismatches: list[str] = []
    header = {"correct": 0, "total": len(HEADER_FIELDS), "missing": 0, "hallucinated": 0}
    by_field: dict[str, dict[str, int]] = {}
    for field in HEADER_FIELDS:
        expected_value, predicted_value = expected.get(field), predicted.get(field)
        matches = _normal_value(field, expected_value) == _normal_value(field, predicted_value)
        by_field[field] = {"correct": int(matches), "total": 1, "missing": 0, "hallucinated": 0}
        if matches:
            header["correct"] += 1
        else:
            mismatches.append(field)
            if expected_value is None and predicted_value is not None:
                header["hallucinated"] += 1
                by_field[field]["hallucinated"] = 1
            elif expected_value is not None and predicted_value is None:
                header["missing"] += 1
                by_field[field]["missing"] = 1

    expected_lines = expected.get("lines", [])
    predicted_lines = predicted.get("lines", [])
    pairs = _line_matches(expected_lines, predicted_lines)
    line_counts = {field: {"correct": 0, "total": 0, "missing": 0, "hallucinated": 0} for field in LINE_FIELDS}
    line_true_positive = line_false_positive = line_false_negative = 0
    for expected_index, predicted_index in pairs:
        if expected_index is not None and predicted_index is not None:
            line_true_positive += 1
        elif expected_index is None:
            line_false_positive += 1
        else:
            line_false_negative += 1
        for field in LINE_FIELDS:
            expected_value = expected_lines[expected_index].get(field) if expected_index is not None else None
            predicted_value = predicted_lines[predicted_index].get(field) if predicted_index is not None else None
            counts = line_counts[field]
            counts["total"] += 1
            if _normal_value(field, expected_value) == _normal_value(field, predicted_value):
                counts["correct"] += 1
            else:
                path = f"lines[{expected_index if expected_index is not None else predicted_index}].{field}"
                mismatches.append(path)
                if expected_value is None and predicted_value is not None:
                    counts["hallucinated"] += 1
                elif expected_value is not None and predicted_value is None:
                    counts["missing"] += 1

    labels_rule = _reconcile_labels(expected, row["po_reference"])
    expected_findings = sorted(set(row["expected_findings"]))
    label_findings = sorted({issue["code"] for issue in labels_rule["issues"]})
    po, po_by_sku = _po_view(row["po_reference"])
    candidate_for_rules = dict(predicted)
    candidate_lines = []
    mapping: dict[str, str] = {}
    for index, line in enumerate(predicted_lines, start=1):
        line_id = f"pred-line-{index}"
        candidate_lines.append({**line, "line_id": line_id})
        if line.get("sku"):
            po_line_id = po_by_sku.get(str(line["sku"]).casefold())
            if po_line_id:
                mapping[line_id] = po_line_id
    candidate_for_rules["lines"] = candidate_lines
    prediction_reconciliation = reconcile(
        candidate_for_rules,
        po,
        mapping,
        has_unverified_source=row["modality"] == "scanned_pdf",
    )
    predicted_findings = sorted({issue["code"] for issue in prediction_reconciliation["issues"]})

    evidence_expected = row.get("evidence_pages", {})
    page_correct = 0
    page_total = len(evidence_expected)
    evidence_missing = 0
    quote_verified = 0
    quote_total = 0
    for field_path, expected_page in evidence_expected.items():
        page_no = result.evidence_pages.get(field_path)
        quote = result.evidence_texts.get(field_path)
        if page_no is None or not 1 <= page_no <= row["page_count"]:
            evidence_missing += 1
            continue
        page_correct += int(page_no == expected_page)
        page_content = source_text.get(page_no, "")
        if page_content:
            quote_total += 1
            quote_verified += int(bool(quote and _normalized_text(quote) in _normalized_text(page_content)))

    result_precision = (
        line_true_positive / (line_true_positive + line_false_positive)
        if line_true_positive + line_false_positive
        else None
    )
    result_recall = (
        line_true_positive / (line_true_positive + line_false_negative)
        if line_true_positive + line_false_negative
        else None
    )
    result_f1 = (
        2 * result_precision * result_recall / (result_precision + result_recall)
        if result_precision is not None and result_recall is not None and result_precision + result_recall
        else 0.0 if result_precision == 0 or result_recall == 0 else None
    )
    return {
        "predicted": predicted,
        "header": header,
        "header_by_field": by_field,
        "line_cells": line_counts,
        "line_detection": {
            "true_positive": line_true_positive,
            "false_positive": line_false_positive,
            "false_negative": line_false_negative,
            "precision": result_precision,
            "recall": result_recall,
            "f1": result_f1,
        },
        "mismatch_fields": sorted(set(mismatches)),
        "rule_labels_match_manifest": label_findings == expected_findings,
        "expected_findings": expected_findings,
        "label_rule_findings": label_findings,
        "predicted_findings": predicted_findings,
        "predicted_reconciliation_status": prediction_reconciliation["status"],
        "predicted_false_pass": bool(expected_findings and prediction_reconciliation["status"] == "PASS"),
        "evidence": {
            "page_correct": page_correct,
            "page_total": page_total,
            "missing_or_invalid_page": evidence_missing,
            "native_quote_verified": quote_verified,
            "native_quote_total": quote_total,
        },
    }


def _live_block_code(args: argparse.Namespace) -> str | None:
    if not args.live:
        return "LIVE_RUN_NOT_REQUESTED"
    if not settings.allow_live_model_calls:
        return "LIVE_CALLS_DISABLED"
    if settings.extraction_provider == "bedrock" and (not settings.aws_region or not settings.bedrock_model_id):
        return "PROVIDER_NOT_CONFIGURED"
    if settings.extraction_provider == "foundry" and (
        not settings.foundry_model_endpoint or not settings.foundry_deployment
    ):
        return "PROVIDER_NOT_CONFIGURED"
    if settings.extraction_provider == "azure_document_intelligence":
        try:
            AzureDocumentIntelligenceProvider._validate_endpoint(
                settings.document_intelligence_endpoint
            )
        except ProviderFailure:
            return "PROVIDER_NOT_CONFIGURED"
        installed_version = _installed_azure_sdk_version()
        if installed_version is None:
            return "PROVIDER_SDK_UNAVAILABLE"
        if installed_version != AZURE_DOCUMENT_INTELLIGENCE_SDK_VERSION:
            return "PROVIDER_SDK_VERSION_MISMATCH"
        return None
    if settings.extraction_provider not in {"bedrock", "foundry"}:
        return "EXTRACTION_PROVIDER_NOT_LIVE_MODEL"
    return None


def _validate_offline_fixture_manifest(
    manifest_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], bytes]:
    """Validate a small, explicitly synthetic manifest without touching the AI corpus."""
    manifest_path = manifest_path.resolve(strict=True)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict):
        raise ValueError("offline fixture manifest must be an object")
    if (
        manifest.get("dataset_type") != "fixture_offline_pipeline_smoke"
        or manifest.get("synthetic_only") is not True
        or manifest.get("accuracy_eligible") is not False
    ):
        raise ValueError("offline smoke accepts only synthetic, non-accuracy fixture manifests")
    dataset_id = manifest.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("offline fixture manifest requires a dataset_id")
    rows = manifest.get("samples")
    fixtures = manifest.get("fixtures")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("offline fixture manifest must contain between 1 and 20 samples")
    if not isinstance(fixtures, list):
        raise ValueError("offline fixture manifest requires a fixtures list")

    fixture_by_hash: dict[str, dict[str, Any]] = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise ValueError("offline fixture entries must be objects")
        digest = fixture.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest.casefold())
            or digest in fixture_by_hash
            or fixture.get("synthetic") is not True
        ):
            raise ValueError("offline fixture entry has an invalid or duplicate synthetic SHA-256")
        InvoiceData.model_validate(fixture.get("extraction"))
        fixture_by_hash[digest] = fixture

    sample_ids: set[str] = set()
    sample_hashes: set[str] = set()
    validated_rows: list[dict[str, Any]] = []
    for original in rows:
        if not isinstance(original, dict):
            raise ValueError("offline fixture samples must be objects")
        row = dict(original)
        sample_id = row.get("sample_id")
        digest = row.get("sha256")
        if (
            not isinstance(sample_id, str)
            or not sample_id.strip()
            or sample_id in sample_ids
            or not isinstance(digest, str)
            or digest not in fixture_by_hash
            or digest in sample_hashes
        ):
            raise ValueError("offline fixture sample has a missing or duplicate id/hash")
        if (
            row.get("synthetic") is not True
            or row.get("label_status") != "test_generated"
            or "manual_correction" in row
            or "post_manual_extraction" in row
        ):
            raise ValueError(f"offline fixture sample is not explicitly test-generated: {sample_id}")
        if row.get("language") not in {"vi-VN", "en-US"}:
            raise ValueError(f"offline fixture sample has an unsupported language: {sample_id}")
        if row.get("modality") not in {"native_pdf_text", "scanned_pdf"}:
            raise ValueError(f"offline fixture sample has an unsupported modality: {sample_id}")
        page_count = row.get("page_count")
        if type(page_count) is not int or page_count < 1:
            raise ValueError(f"offline fixture sample has an invalid page count: {sample_id}")

        sample_path = _resolved_sample_path(manifest_path, row)
        if not sample_path.is_file() or sample_path.suffix.casefold() != ".pdf":
            raise ValueError(f"offline fixture sample must reference a generated PDF: {sample_id}")
        content = sample_path.read_bytes()
        if _sha256(sample_path) != digest:
            raise ValueError(f"offline fixture PDF hash mismatch: {sample_id}")
        validated = validate_document(content)
        if validated.mime != "application/pdf" or validated.page_count != page_count:
            raise ValueError(f"offline fixture PDF metadata mismatch: {sample_id}")

        labels = InvoiceData.model_validate(row.get("labels")).model_dump(mode="json")
        purchase_order = POCreate.model_validate(row.get("po_reference")).model_dump(mode="json")
        expected_findings = row.get("expected_findings")
        if not isinstance(expected_findings, list) or any(
            not isinstance(code, str) for code in expected_findings
        ):
            raise ValueError(f"offline fixture sample has invalid expected findings: {sample_id}")
        row["labels"] = labels
        row["po_reference"] = purchase_order
        row["_validated_path"] = sample_path
        row["_validated_content"] = content
        sample_ids.add(sample_id)
        sample_hashes.add(digest)
        validated_rows.append(row)

    if set(fixture_by_hash) != sample_hashes:
        raise ValueError("offline fixture manifest must bind exactly one fixture to every sample")
    return manifest, validated_rows, manifest_bytes


def evaluate_offline_fixture_smoke(*, manifest_path: Path, report_path: Path) -> dict[str, Any]:
    """Run fixture extraction and deterministic reconciliation on test-generated documents only."""
    if settings.extraction_provider != "fixture":
        raise ValueError("offline fixture smoke requires EXTRACTION_PROVIDER=fixture")
    manifest, selected, manifest_bytes = _validate_offline_fixture_manifest(manifest_path)
    report_path = report_path.resolve()
    protected_paths = {
        manifest_path.resolve(),
        *(row["_validated_path"].resolve() for row in selected),
    }
    if report_path in protected_paths or report_path.exists():
        raise ValueError("offline fixture report path must be new and separate from its inputs")

    provider = FixtureProvider(manifest_path.resolve())
    outcomes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    schema_valid_count = 0
    rule_regression_passed = 0
    fixture_invocations = 0
    fixture_model_calls = 0
    fixture_model_calls_known = True
    fixture_latencies: list[float] = []
    started = time.perf_counter()

    for row in selected:
        sample_id = row["sample_id"]
        sample_started = time.perf_counter()
        fixture_invocations += 1
        try:
            document = SimpleNamespace(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"{manifest['dataset_id']}:{sample_id}"),
                sha256=row["sha256"],
                mime="application/pdf",
                bytes=len(row["_validated_content"]),
                pages=row["page_count"],
            )
            request, _page_text = _make_request(
                document,
                row["_validated_content"],
                uuid.uuid5(uuid.NAMESPACE_URL, f"fixture-smoke:{sample_id}"),
                row["language"],
                provider,
                datetime.now(UTC) + timedelta(seconds=settings.extraction_job_deadline_seconds),
            )
            result = provider.extract_invoice(request)
            candidate = InvoiceData.model_validate(result.candidate).model_dump(mode="json")
            schema_valid_count += 1
            usage = result.usage if isinstance(result.usage, dict) else {}
            reported_model_calls = usage.get("model_calls")
            if type(reported_model_calls) is int and reported_model_calls >= 0:
                fixture_model_calls += reported_model_calls
            else:
                fixture_model_calls_known = False

            expected_findings = sorted(set(row["expected_findings"]))
            label_rule = _reconcile_labels(row["labels"], row["po_reference"])
            candidate_rule = _reconcile_labels(candidate, row["po_reference"])
            label_findings = sorted({issue["code"] for issue in label_rule["issues"]})
            candidate_findings = sorted({issue["code"] for issue in candidate_rule["issues"]})
            rule_passed = label_findings == expected_findings and candidate_findings == expected_findings
            rule_regression_passed += int(rule_passed)
            latency_ms = round((time.perf_counter() - sample_started) * 1000, 2)
            fixture_latencies.append(latency_ms)
            outcomes.append(
                {
                    "sample_id": sample_id,
                    "status": "SUCCEEDED",
                    "routing_reason": request.routing_reason,
                    "fixture_pipeline_latency_ms": latency_ms,
                    "fixture_result_schema_valid": True,
                    "synthetic_rule_regression_passed": rule_passed,
                    "expected_findings": expected_findings,
                    "candidate_findings": candidate_findings,
                }
            )
        except (ProviderFailure, DomainError) as exc:
            latency_ms = round((time.perf_counter() - sample_started) * 1000, 2)
            fixture_latencies.append(latency_ms)
            failures.append({"sample_id": sample_id, "code": exc.code})
            outcomes.append(
                {
                    "sample_id": sample_id,
                    "status": "FAILED",
                    "fixture_pipeline_latency_ms": latency_ms,
                    "failure_code": exc.code,
                }
            )
        except (ValueError, TypeError):
            latency_ms = round((time.perf_counter() - sample_started) * 1000, 2)
            fixture_latencies.append(latency_ms)
            failures.append({"sample_id": sample_id, "code": "FIXTURE_OUTPUT_INVALID"})
            outcomes.append(
                {
                    "sample_id": sample_id,
                    "status": "FAILED",
                    "fixture_pipeline_latency_ms": latency_ms,
                    "failure_code": "FIXTURE_OUTPUT_INVALID",
                }
            )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    completed = sum(outcome["status"] == "SUCCEEDED" for outcome in outcomes)
    report = {
        "report_schema_version": "1.0",
        "evaluation_type": "fixture_offline_pipeline_smoke",
        "classification": "SYNTHETIC_FIXTURE_PLUMBING_ONLY",
        "accuracy_status": "NOT_MEASURED",
        "sample_count": len(selected),
        "processed_sample_count": len(outcomes),
        "completed_sample_count": completed,
        "failure_count": len(failures),
        "provider": {
            "name": "fixture",
            "mode": "offline",
            "sdk_name": None,
            "sdk_version": None,
            "api_version": None,
            "model_id": None,
            "model_version": None,
        },
        "dataset": {
            "dataset_id": manifest["dataset_id"],
            "synthetic_only": True,
            "accuracy_eligible": False,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "metrics": {
            "ai_accuracy": {"status": "NOT_MEASURED", "value": None, "numerator": None, "denominator": 0},
            "fixture_result_schema_validity": {
                "valid_count": schema_valid_count,
                "denominator": len(selected),
                "rate": _ratio(schema_valid_count, len(selected)),
            },
            "synthetic_rule_regression": {
                "passed_count": rule_regression_passed,
                "denominator": len(selected),
                "pass_rate": _ratio(rule_regression_passed, len(selected)),
                "ai_accuracy_included": False,
            },
        },
        "configuration": {
            "requested_provider": "fixture",
            "execution_mode": "offline",
            "live_model_calls_requested": False,
            "fixture_accuracy_included": False,
            "azure_live_status": "LIVE_PENDING",
        },
        "runtime": {
            "runner_elapsed_ms": elapsed_ms,
            "fixture_pipeline_latency_median_ms": median(fixture_latencies) if fixture_latencies else None,
            "model_latency_median_ms": None,
            "input_tokens": None,
            "output_tokens": None,
            "provider_usage_status": "MODEL_NOT_USED",
            "model_calls": fixture_model_calls if fixture_model_calls_known else None,
            "provider_invocations": fixture_invocations,
            "cost_usd": None,
            "cost_usd_source": None,
        },
        "error_analysis": {
            "failure_counts": dict(sorted(Counter(item["code"] for item in failures).items())),
            "samples": failures,
        },
        "samples": outcomes,
        "private_predictions_ref": None,
        "limitations": [
            "This report verifies offline fixture plumbing using test-generated synthetic documents only.",
            "Fixture outputs and fixture metrics are not Azure accuracy or provider performance evidence.",
            "No Azure client, external API, database, or model was used in this run.",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percentile95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def evaluate(
    *, manifest_path: Path, split: str, report_path: Path, live: bool, confirm_held_out: bool
) -> dict[str, Any]:
    if split == "test" and not confirm_held_out:
        raise ValueError("held-out test execution requires --confirm-held-out")
    manifest, manifest_bytes = validate_manifest(manifest_path)
    selected = [row for row in manifest["samples"] if row["split"] == split]
    if not selected:
        raise ValueError(f"no samples found for split {split}")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    private_dir = ROOT / "var" / "eval_predictions"
    private_dir.mkdir(parents=True, exist_ok=True)
    private_path = private_dir / f"{run_id}.jsonl"
    blocker = _live_block_code(argparse.Namespace(live=live))
    provider = configured_provider()

    outcomes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    private_predictions = private_path.open("w", encoding="utf-8")
    start_total = time.perf_counter()
    response_latencies: list[float] = []
    routing_counts: Counter[str] = Counter()
    input_tokens = output_tokens = 0
    token_input_known = token_output_known = False
    provider_cost_usd = Decimal("0")
    provider_cost_known = False
    provider_call_usage_known = False
    model_calls = 0
    provider_invocations = 0
    model_input_attempts = 0
    output_count = 0
    schema_valid_count = 0
    header_correct = header_total = 0
    header_missing = header_hallucinated = 0
    header_field_counts: dict[str, Counter[str]] = defaultdict(Counter)
    line_field_counts: dict[str, Counter[str]] = defaultdict(Counter)
    line_detect_tp = line_detect_fp = line_detect_fn = 0
    line_f1_values: list[float] = []
    reconciliation_exact = reconciliation_total = 0
    false_passes = invoice_issue_count = 0
    rule_passed = rule_total = 0
    evidence_page_correct = evidence_page_total = evidence_missing = 0
    native_quote_verified = native_quote_total = 0

    try:
        for row in selected:
            sample_path = _resolved_sample_path(manifest_path, row)
            content = sample_path.read_bytes()
            document = SimpleNamespace(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"{manifest['dataset_id']}:{row['sample_id']}"),
                sha256=row["sha256"],
                mime="application/pdf",
                bytes=len(content),
                pages=row["page_count"],
            )
            extraction_request, page_text = _make_request(
                document,
                content,
                uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{row['sample_id']}"),
                row["language"],
                provider,
                datetime.now(UTC) + timedelta(seconds=settings.extraction_job_deadline_seconds),
            )
            routing_counts[extraction_request.routing_reason] += 1
            labels_rule = _reconcile_labels(row["labels"], row["po_reference"])
            expected_findings = sorted(set(row["expected_findings"]))
            rule_total += 1
            rule_passed += int(sorted({issue["code"] for issue in labels_rule["issues"]}) == expected_findings)

            sample_start = time.perf_counter()
            if blocker:
                failure = ProviderFailure(
                    blocker,
                    "Live evaluation is blocked by explicit runtime configuration.",
                    retryable=False,
                    status_code=503,
                )
                errors.append(
                    {
                        "status": "BLOCKED",
                        "sample_id": row["sample_id"],
                        "layout_family": row["layout_family"],
                        "modality": row["modality"],
                        "code": failure.code,
                    }
                )
                outcomes.append(
                    {
                        "sample_id": row["sample_id"],
                        "layout_family": row["layout_family"],
                        "split": row["split"],
                        "synthetic": row["synthetic"],
                        "modality": row["modality"],
                        "routing_reason": extraction_request.routing_reason,
                        "status": "BLOCKED",
                        "failure_code": failure.code,
                        "retryable": failure.retryable,
                        "latency_ms": None,
                    }
                )
                continue

            provider_invocations += 1
            model_input_attempts += 1
            model_calls += 1
            try:
                result = provider.extract_invoice(extraction_request)
                latency_ms = round((time.perf_counter() - sample_start) * 1000, 2)
                response_latencies.append(latency_ms)
                output_count += 1
                schema_valid_count += 1
                usage = result.usage
                if type(usage.get("input_tokens")) is int and usage["input_tokens"] >= 0:
                    input_tokens += usage["input_tokens"]
                    token_input_known = True
                if type(usage.get("output_tokens")) is int and usage["output_tokens"] >= 0:
                    output_tokens += usage["output_tokens"]
                    token_output_known = True
                raw_cost = usage.get("cost_usd")
                if raw_cost is not None and not isinstance(raw_cost, bool):
                    try:
                        parsed_cost = Decimal(str(raw_cost))
                    except InvalidOperation:
                        parsed_cost = Decimal("NaN")
                    if parsed_cost.is_finite() and parsed_cost >= 0:
                        provider_cost_usd += parsed_cost
                        provider_cost_known = True
                reported_model_calls = usage.get("model_calls")
                if type(reported_model_calls) is int and reported_model_calls >= 0:
                    model_calls += reported_model_calls - 1
                    provider_call_usage_known = True
                elif settings.extraction_provider != "azure_document_intelligence":
                    model_calls -= 1
                scores = _score_prediction(row, result, page_text)
                header_correct += scores["header"]["correct"]
                header_total += scores["header"]["total"]
                header_missing += scores["header"]["missing"]
                header_hallucinated += scores["header"]["hallucinated"]
                for field, counts in scores["header_by_field"].items():
                    header_field_counts[field].update(counts)
                for field, counts in scores["line_cells"].items():
                    line_field_counts[field].update(counts)
                line_detect_tp += scores["line_detection"]["true_positive"]
                line_detect_fp += scores["line_detection"]["false_positive"]
                line_detect_fn += scores["line_detection"]["false_negative"]
                if scores["line_detection"]["f1"] is not None:
                    line_f1_values.append(scores["line_detection"]["f1"])
                reconciliation_total += 1
                reconciliation_exact += int(scores["predicted_findings"] == scores["expected_findings"])
                invoice_issue_count += int(bool(scores["expected_findings"]))
                false_passes += int(scores["predicted_false_pass"])
                evidence_page_correct += scores["evidence"]["page_correct"]
                evidence_page_total += scores["evidence"]["page_total"]
                evidence_missing += scores["evidence"]["missing_or_invalid_page"]
                native_quote_verified += scores["evidence"]["native_quote_verified"]
                native_quote_total += scores["evidence"]["native_quote_total"]
                outcomes.append(
                    {
                        "sample_id": row["sample_id"],
                        "layout_family": row["layout_family"],
                        "split": row["split"],
                        "synthetic": row["synthetic"],
                        "modality": row["modality"],
                        "routing_reason": extraction_request.routing_reason,
                        "status": "SUCCEEDED",
                        "latency_ms": latency_ms,
                        "mismatch_fields": scores["mismatch_fields"],
                        "predicted_findings": scores["predicted_findings"],
                        "expected_findings": scores["expected_findings"],
                        "evidence": scores["evidence"],
                    }
                )
                private_predictions.write(
                    json.dumps(
                        {
                            "sample_id": row["sample_id"],
                            "candidate": scores["predicted"],
                            "evidence_texts": result.evidence_texts,
                            "evidence_pages": result.evidence_pages,
                            "usage": result.usage,
                            "model_id": result.model_id,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            except ProviderFailure as failure:
                latency_ms = round((time.perf_counter() - sample_start) * 1000, 2)
                errors.append(
                    {
                        "status": "FAILED",
                        "sample_id": row["sample_id"],
                        "layout_family": row["layout_family"],
                        "modality": row["modality"],
                        "code": failure.code,
                    }
                )
                outcomes.append(
                    {
                        "sample_id": row["sample_id"],
                        "layout_family": row["layout_family"],
                        "split": row["split"],
                        "synthetic": row["synthetic"],
                        "modality": row["modality"],
                        "routing_reason": extraction_request.routing_reason,
                        "status": "FAILED",
                        "failure_code": failure.code,
                        "retryable": failure.retryable,
                        "latency_ms": latency_ms,
                    }
                )
    finally:
        private_predictions.close()

    elapsed_ms = round((time.perf_counter() - start_total) * 1000, 2)
    failure_counts = Counter(error["code"] for error in errors if error["status"] == "FAILED")
    blocked_counts = Counter(error["code"] for error in errors if error["status"] == "BLOCKED")
    error_by_family: dict[str, Counter[str]] = defaultdict(Counter)
    error_by_modality: dict[str, Counter[str]] = defaultdict(Counter)
    blocked_by_family: dict[str, Counter[str]] = defaultdict(Counter)
    blocked_by_modality: dict[str, Counter[str]] = defaultdict(Counter)
    for error in errors:
        if error["status"] == "BLOCKED":
            blocked_by_family[error["layout_family"]][error["code"]] += 1
            blocked_by_modality[error["modality"]][error["code"]] += 1
        else:
            error_by_family[error["layout_family"]][error["code"]] += 1
            error_by_modality[error["modality"]][error["code"]] += 1

    header_by_field = {
        field: {
            **dict(counts),
            "exact_match_rate": _ratio(counts["correct"], counts["total"]),
        }
        for field, counts in sorted(header_field_counts.items())
    }
    line_by_field = {
        field: {
            **dict(counts),
            "cell_exact_match_rate": _ratio(counts["correct"], counts["total"]),
        }
        for field, counts in sorted(line_field_counts.items())
    }
    real_count = sum(not row["synthetic"] for row in selected)
    synthetic_count = sum(row["synthetic"] for row in selected)
    completed_count = sum(row["status"] == "SUCCEEDED" for row in outcomes)
    blocked_count = sum(row["status"] == "BLOCKED" for row in outcomes)
    failed_count = sum(row["status"] == "FAILED" for row in outcomes)
    split_policy = manifest["split_policy"]
    family_counts = Counter(row["layout_family"] for row in selected)
    lock_path = ROOT / "backend" / "requirements.lock"
    selected_provider_versions = _provider_versions(provider)
    if settings.extraction_provider == "azure_document_intelligence":
        configured_connect_timeout = settings.document_intelligence_connect_timeout_seconds
        configured_read_timeout = settings.document_intelligence_read_timeout_seconds
        configured_max_output_tokens = None
        configured_temperature = None
        configured_prompt_version = None
    elif settings.extraction_provider == "foundry":
        configured_connect_timeout = settings.foundry_connect_timeout_seconds
        configured_read_timeout = settings.foundry_read_timeout_seconds
        configured_max_output_tokens = settings.foundry_max_output_tokens
        configured_temperature = settings.foundry_temperature
        configured_prompt_version = settings.extraction_prompt_version
    elif settings.extraction_provider == "bedrock":
        configured_connect_timeout = settings.bedrock_connect_timeout_seconds
        configured_read_timeout = settings.bedrock_read_timeout_seconds
        configured_max_output_tokens = settings.bedrock_max_output_tokens
        configured_temperature = 0
        configured_prompt_version = settings.extraction_prompt_version
    else:
        configured_connect_timeout = None
        configured_read_timeout = None
        configured_max_output_tokens = None
        configured_temperature = None
        configured_prompt_version = None
    report = {
        "report_version": "1.0",
        "evaluation_type": "ai_extraction_evaluation",
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "execution_status": (
            "LIVE_PENDING"
            if blocker and live
            else "DRY_RUN"
            if blocker
            else "COMPLETED_WITH_FAILURES"
            if failed_count
            else "COMPLETED"
        ),
        "git_sha": _git_sha(),
        "dependency_lock_sha256": _sha256(lock_path) if lock_path.is_file() else None,
        "dataset": {
            "dataset_id": manifest["dataset_id"],
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "dataset_type": manifest["dataset_type"],
            "synthetic_only": manifest.get("synthetic_only") is True,
            "label_status": manifest.get("label_status"),
            "sample_count": len(manifest["samples"]),
            "split_policy": split_policy,
            "fixture_samples_included": 0,
            "fixture_accuracy_included": False,
        },
        "selection": {
            "split": split,
            "sample_count": len(selected),
            "layout_family_count": len(family_counts),
            "layout_families": dict(sorted(family_counts.items())),
            "held_out_test_selected": split == "test",
            # Sending a held-out document to the model consumes the split even if
            # the provider refuses, times out, or returns an invalid response.
            "held_out_test_consumed": split == "test" and model_input_attempts > 0,
        },
        "sample_count": len(selected),
        "synthetic_sample_count": synthetic_count,
        "real_sample_count": real_count,
        "processed_sample_count": len(outcomes),
        "completed_sample_count": completed_count,
        "blocked_sample_count": blocked_count,
        "failure_count": failed_count,
        "non_success_sample_count": failed_count + blocked_count,
        "provider_failure_count": failed_count,
        "preflight_blocked_count": blocked_count,
        "model_input_attempted_sample_count": model_input_attempts,
        "live_model_requests_started": provider_invocations
        if settings.extraction_provider == "azure_document_intelligence"
        else model_calls,
        "model_output_count": output_count,
        "schema_valid_count": schema_valid_count,
        "schema_valid_denominator": model_input_attempts,
        "accuracy_status": "MEASURED" if completed_count else "NOT_MEASURED",
        "accuracy": {
            "header_exact_match": _ratio(header_correct, header_total),
            "header_exact_match_correct": header_correct,
            "header_exact_match_denominator": header_total,
            "header_missing_values": header_missing,
            "header_hallucinated_values": header_hallucinated,
            "header_by_field": header_by_field,
            "line_cell_by_field": line_by_field,
            "line_detection": {
                "true_positive": line_detect_tp,
                "false_positive": line_detect_fp,
                "false_negative": line_detect_fn,
                "precision": _ratio(line_detect_tp, line_detect_tp + line_detect_fp),
                "recall": _ratio(line_detect_tp, line_detect_tp + line_detect_fn),
                "mean_per_invoice_f1": sum(line_f1_values) / len(line_f1_values) if line_f1_values else None,
            },
            "reconciliation_issue_exact_match": _ratio(reconciliation_exact, reconciliation_total),
            "reconciliation_issue_exact_match_denominator": reconciliation_total,
            "false_pass_rate_on_labelled_issue_samples": _ratio(false_passes, invoice_issue_count),
            "provenance_page_accuracy": _ratio(evidence_page_correct, evidence_page_total),
            "provenance_page_correct": evidence_page_correct,
            "provenance_page_denominator": evidence_page_total,
            "provenance_missing_or_invalid_page": evidence_missing,
            "native_quote_verification_rate": _ratio(native_quote_verified, native_quote_total),
            "native_quote_verified": native_quote_verified,
            "native_quote_denominator": native_quote_total,
        },
        "canonical_label_rule_regression": {
            "passed": rule_passed,
            "denominator": rule_total,
            "passed_rate": _ratio(rule_passed, rule_total),
            "ai_accuracy_included": False,
        },
        "configuration": {
            "requested_provider": settings.extraction_provider,
            "application_extraction_provider": settings.extraction_provider,
            "model_id": provider.capabilities().model_id,
            "provider_versions": selected_provider_versions,
            "region": settings.aws_region or None if settings.extraction_provider == "bedrock" else None,
            "foundry_endpoint": settings.foundry_model_endpoint or None
            if settings.extraction_provider == "foundry"
            else None,
            "live_flag_requested": live,
            "allow_live_model_calls": settings.allow_live_model_calls,
            "live_preflight_block_code": blocker,
            "prompt_version": configured_prompt_version,
            "schema_version": "1.0",
            "reconciliation_policy_version": POLICY_VERSION,
            "temperature": configured_temperature,
            "max_output_tokens": configured_max_output_tokens,
            "connect_timeout_seconds": configured_connect_timeout,
            "read_timeout_seconds": configured_read_timeout,
            "job_deadline_seconds": settings.extraction_job_deadline_seconds,
            "retry_policy": "one attempt per sample; no automatic eval reruns",
            "pricing": None,
            "pricing_unknown_reason": (
                "No versioned pricing table is available; cost is provider-reported."
                if provider_cost_known
                else "Provider did not report cost and no versioned pricing configuration is available."
            ),
        },
        "runtime": {
            "runner_elapsed_ms": elapsed_ms,
            "model_latency_median_ms": median(response_latencies) if response_latencies else None,
            "model_latency_p95_ms": _percentile95(response_latencies),
            "input_tokens": input_tokens if token_input_known else None,
            "output_tokens": output_tokens if token_output_known else None,
            "provider_usage_status": "REPORTED"
            if token_input_known or token_output_known or provider_cost_known or provider_call_usage_known
            else "NOT_REPORTED",
            "cost_usd": float(provider_cost_usd) if provider_cost_known else None,
            "cost_usd_source": "provider_response" if provider_cost_known else None,
            "model_calls": None
            if settings.extraction_provider == "azure_document_intelligence" and not provider_call_usage_known
            else model_calls,
            "provider_invocations": provider_invocations,
            "routing_counts": dict(sorted(routing_counts.items())),
        },
        "error_analysis": {
            "failure_counts": dict(sorted(failure_counts.items())),
            "blocked_counts": dict(sorted(blocked_counts.items())),
            "by_layout_family": {
                family: dict(sorted(counts.items())) for family, counts in sorted(error_by_family.items())
            },
            "by_modality": {
                modality: dict(sorted(counts.items())) for modality, counts in sorted(error_by_modality.items())
            },
            "blocked_by_layout_family": {
                family: dict(sorted(counts.items())) for family, counts in sorted(blocked_by_family.items())
            },
            "blocked_by_modality": {
                modality: dict(sorted(counts.items())) for modality, counts in sorted(blocked_by_modality.items())
            },
            "samples": errors,
            "interpretation": (
                "Every selected sample was blocked by runtime configuration before a model request started."
                if blocker and live and blocked_count == len(selected)
                else "Some samples were blocked by runtime configuration before model requests started."
                if blocked_count
                else "Failures are provider/extraction errors observed in this run."
                if failed_count
                else "No model outputs were generated in this run."
                if not completed_count
                else "Error groups describe only the selected labeled split."
            ),
        },
        "samples": outcomes,
        "private_predictions_ref": str(private_path.relative_to(ROOT).as_posix()),
        "limitations": [
            "This AI corpus is synthetic and its programmatic reference labels are not human annotated.",
            "Synthetic-corpus accuracy is not a claim about real invoices or production performance.",
            "Fixture/demo regression results are excluded from all AI accuracy metrics.",
            "A live accuracy run requires explicit CLI opt-in, live-call opt-in, and complete selected-provider configuration.",
            "Azure prebuilt invoice does not report an immutable service model build or token/cost usage in this contract; those fields remain null unless returned by the provider.",
        ],
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_report.replace(report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate extraction providers or run an offline fixture smoke.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--offline-fixture-smoke",
        action="store_true",
        help="Run the fixture pipeline on an explicitly synthetic temporary manifest; never measures accuracy.",
    )
    modes.add_argument(
        "--preflight-only",
        action="store_true",
        help="Check live-run gates and exit without loading a manifest or constructing a provider/client.",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--split", choices=("dev", "validation", "test"), default="validation")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--live", action="store_true", help="Request live evaluation; still requires env opt-in.")
    parser.add_argument(
        "--confirm-held-out",
        action="store_true",
        help="Required to run the frozen test split; any returned prediction consumes the held-out set.",
    )
    args = parser.parse_args()
    if args.preflight_only:
        blocker = _live_block_code(args)
        status = f"BLOCKED:{blocker}" if blocker else "PREFLIGHT_OK_NO_CLIENT_CREATED"
        print(f"provider_preflight: provider={settings.extraction_provider} status={status}")
        if blocker:
            raise SystemExit(2)
        return

    if args.offline_fixture_smoke:
        if args.live or args.confirm_held_out:
            parser.error("offline fixture smoke cannot be combined with --live or --confirm-held-out")
        if args.manifest is None or args.report is None:
            parser.error("offline fixture smoke requires explicit --manifest and --report paths")
        try:
            report = evaluate_offline_fixture_smoke(
                manifest_path=args.manifest,
                report_path=args.report,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"offline fixture smoke failed before report generation: {exc}") from exc
        print(
            f"fixture_offline_pipeline_smoke: provider=fixture n={report['sample_count']} "
            f"completed={report['completed_sample_count']} failures={report['failure_count']} "
            f"accuracy=NOT_MEASURED report={args.report}"
        )
        if report["failure_count"]:
            raise SystemExit(1)
        return

    manifest_path = args.manifest or DEFAULT_MANIFEST
    report_path = args.report or ROOT / "evals" / "reports" / f"ai_corpus_{args.split}_latest.json"
    try:
        report = evaluate(
            manifest_path=manifest_path.resolve(),
            split=args.split,
            report_path=report_path.resolve(),
            live=args.live,
            confirm_held_out=args.confirm_held_out,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"evaluation failed before report generation: {exc}") from exc
    print(
        f"ai_extraction_evaluation: split={report['selection']['split']} n={report['sample_count']} "
        f"completed={report['completed_sample_count']} failures={report['failure_count']} "
        f"blocked={report['blocked_sample_count']} "
        f"accuracy={report['accuracy_status']} state={report['execution_status']} report={report_path}"
    )
    if report["failure_count"] or report["blocked_sample_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
