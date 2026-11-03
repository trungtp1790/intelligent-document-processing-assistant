from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" if (ROOT / "backend").is_dir() else ROOT))

from app.domain.reconciliation import reconcile  # noqa: E402
from app.schemas import InvoiceData  # noqa: E402


MANIFEST_PATH = ROOT / "data" / "demo" / "fixture_manifest.json"


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def evaluate_item(item: dict[str, Any], seen_business_keys: set[str]) -> dict[str, Any]:
    parsed = InvoiceData.model_validate(item["extraction"]).model_dump(mode="json")
    invoice_number = parsed.get("invoice_number")
    invoice_date = parsed.get("invoice_date")
    supplier_tax_id = (parsed.get("supplier_tax_id") or "").strip().casefold()
    business_key = ""
    if invoice_number and invoice_date and supplier_tax_id:
        business_key = "|".join(
            [
                supplier_tax_id,
                (parsed.get("invoice_series") or "").casefold(),
                invoice_number,
                invoice_date[:4],
            ]
        )
    po = item["po_reference"]
    po_line = dict(po["line"], id="po-line-1", line_no=1)
    invoice_lines = parsed["lines"]
    if invoice_lines:
        invoice_lines[0]["line_id"] = "invoice-line-1"
    mapping = (
        {"invoice-line-1": "po-line-1"}
        if invoice_lines and invoice_lines[0].get("sku")
        else {}
    )
    duplicate_pending = bool(business_key and business_key in seen_business_keys)
    result = reconcile(
        parsed,
        {
            "po_number": po["po_number"],
            "currency": po["currency"],
            "state": "OPEN",
            "supplier_tax_id": "DEMO-SUP-001",
            "lines": [po_line],
        },
        mapping,
        duplicate_pending=duplicate_pending,
        has_unverified_source=item["modality"] == "image",
    )
    if business_key:
        seen_business_keys.add(business_key)
    observed = sorted({issue["code"] for issue in result["issues"]})
    expected = sorted(set(item["expected_findings"]))
    return {
        "sample_id": item["sample_id"],
        "file": item["file"],
        "status": result["status"],
        "expected_findings": expected,
        "observed_findings": observed,
        "passed": observed == expected,
    }


def run(output_path: Path) -> dict[str, Any]:
    manifest_bytes = MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_bytes)
    rows = manifest.get("fixtures", [])
    seen_hashes: set[str] = set()
    seen_names: set[str] = set()
    seen_business_keys: set[str] = set()
    samples: list[dict[str, Any]] = []
    for item in rows:
        path = MANIFEST_PATH.parent / item["file"]
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha != item["sha256"]:
            raise ValueError(f"fixture hash mismatch: {item['file']}")
        if actual_sha in seen_hashes or item["file"] in seen_names:
            raise ValueError(f"duplicate fixture hash or filename: {item['file']}")
        if (
            item.get("synthetic") is not True
            or item.get("source") != "project-generated-demo"
        ):
            raise ValueError(f"fixture missing synthetic source labels: {item['file']}")
        if not item.get("layout_family") or item.get("split") != "demo_only":
            raise ValueError(
                f"fixture must be marked as demo-only and have a layout family: {item['file']}"
            )
        seen_hashes.add(actual_sha)
        seen_names.add(item["file"])
        samples.append(evaluate_item(item, seen_business_keys))

    if len(rows) != 12:
        raise ValueError(
            f"expected exactly 12 synthetic demo fixtures, found {len(rows)}"
        )
    counts = Counter(code for sample in samples for code in sample["observed_findings"])
    report = {
        "report_version": "1.0",
        "evaluation_type": "synthetic_rule_regression",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "provider": None,
        "model_id": None,
        "synthetic_only": True,
        "ai_extraction_accuracy_measured": False,
        "sample_count": len(samples),
        "passed_count": sum(sample["passed"] for sample in samples),
        "failed_count": sum(not sample["passed"] for sample in samples),
        "observed_issue_counts": dict(sorted(counts.items())),
        "samples": samples,
        "limitations": [
            "Fixtures are synthetic plumbing examples, not evidence of AI accuracy.",
            "The separate 90-document synthetic evaluation corpus is not used by this fixture report.",
            "No live provider calls were made by this report.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run synthetic fixture rule regression checks."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evals" / "reports" / "fixture_rule_regression.json",
    )
    args = parser.parse_args()
    report = run(args.output.resolve())
    print(
        f"synthetic_rule_regression: {report['passed_count']}/{report['sample_count']} passed; "
        f"AI accuracy=NOT_MEASURED; report={args.output}"
    )
    if report["failed_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
