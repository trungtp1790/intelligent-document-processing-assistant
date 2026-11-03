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
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" if (ROOT / "backend").is_dir() else ROOT))

from app.config import settings  # noqa: E402
from app.documents.validation import validate_document  # noqa: E402
from app.domain.reconciliation import POLICY_VERSION, reconcile  # noqa: E402
from app.providers.errors import ProviderFailure  # noqa: E402
from app.providers.factory import configured_provider  # noqa: E402
from app.services.extraction import _make_request, _normalized_text, _pdf_text_pages  # noqa: E402
from app.schemas import InvoiceData  # noqa: E402

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
    if settings.extraction_provider not in {"bedrock", "foundry"}:
        return "EXTRACTION_PROVIDER_NOT_LIVE_MODEL"
    return None


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
    provider = configured_provider()
    blocker = _live_block_code(argparse.Namespace(live=live))

    outcomes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    private_predictions = private_path.open("w", encoding="utf-8")
    start_total = time.perf_counter()
    response_latencies: list[float] = []
    routing_counts: Counter[str] = Counter()
    input_tokens = output_tokens = 0
    token_input_known = token_output_known = False
    model_calls = 0
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
                        "latency_ms": round((time.perf_counter() - sample_start) * 1000, 2),
                    }
                )
                continue

            model_calls += 1
            model_input_attempts += 1
            try:
                result = provider.extract_invoice(extraction_request)
                latency_ms = round((time.perf_counter() - sample_start) * 1000, 2)
                response_latencies.append(latency_ms)
                output_count += 1
                schema_valid_count += 1
                usage = result.usage
                if isinstance(usage.get("input_tokens"), int):
                    input_tokens += usage["input_tokens"]
                    token_input_known = True
                if isinstance(usage.get("output_tokens"), int):
                    output_tokens += usage["output_tokens"]
                    token_output_known = True
                model_calls += int(usage.get("model_calls", 0)) - 1
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
        "live_model_requests_started": model_calls,
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
            "region": settings.aws_region or None if settings.extraction_provider == "bedrock" else None,
            "foundry_endpoint": settings.foundry_model_endpoint or None
            if settings.extraction_provider == "foundry"
            else None,
            "live_flag_requested": live,
            "allow_live_model_calls": settings.allow_live_model_calls,
            "live_preflight_block_code": blocker,
            "prompt_version": settings.extraction_prompt_version,
            "schema_version": "1.0",
            "reconciliation_policy_version": POLICY_VERSION,
            "temperature": settings.foundry_temperature if settings.extraction_provider == "foundry" else 0,
            "max_output_tokens": settings.foundry_max_output_tokens
            if settings.extraction_provider == "foundry"
            else settings.bedrock_max_output_tokens,
            "connect_timeout_seconds": settings.foundry_connect_timeout_seconds
            if settings.extraction_provider == "foundry"
            else settings.bedrock_connect_timeout_seconds,
            "read_timeout_seconds": settings.foundry_read_timeout_seconds
            if settings.extraction_provider == "foundry"
            else settings.bedrock_read_timeout_seconds,
            "job_deadline_seconds": settings.extraction_job_deadline_seconds,
            "retry_policy": "one attempt per sample; no automatic eval reruns",
            "pricing": None,
            "pricing_unknown_reason": "No versioned pricing configuration is present.",
        },
        "runtime": {
            "runner_elapsed_ms": elapsed_ms,
            "model_latency_median_ms": median(response_latencies) if response_latencies else None,
            "model_latency_p95_ms": _percentile95(response_latencies),
            "input_tokens": input_tokens if token_input_known else None,
            "output_tokens": output_tokens if token_output_known else None,
            "model_calls": model_calls,
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
            "A live accuracy run requires explicit CLI opt-in and Bedrock or Foundry runtime configuration.",
        ],
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_report.replace(report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a live extraction provider on the labeled AI corpus.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--split", choices=("dev", "validation", "test"), default="validation")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--live", action="store_true", help="Request live evaluation; still requires env opt-in.")
    parser.add_argument(
        "--confirm-held-out",
        action="store_true",
        help="Required to run the frozen test split; any returned prediction consumes the held-out set.",
    )
    args = parser.parse_args()
    report_path = args.report or ROOT / "evals" / "reports" / f"ai_corpus_{args.split}_latest.json"
    try:
        report = evaluate(
            manifest_path=args.manifest.resolve(),
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
