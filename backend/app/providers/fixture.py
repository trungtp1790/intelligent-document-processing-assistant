from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.domain.errors import DomainError
from app.providers.protocol import (
    ExtractionRequest,
    ExtractionResult,
    ProviderCapabilities,
)

_HERE = Path(__file__).resolve()


def _find_root() -> Path:
    for parent in _HERE.parents:
        if (parent / "data" / "demo" / "fixture_manifest.json").exists():
            return parent
    return _HERE.parents[2]


_REPO_ROOT = _find_root()
MANIFEST_PATH = _REPO_ROOT / "data" / "demo" / "fixture_manifest.json"


class FixtureProvider:
    name = "fixture"

    def __init__(self, manifest_path: Path = MANIFEST_PATH):
        self.manifest_path = manifest_path

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, 10, 15 * 1024 * 1024, "fixture_manifest", None)

    def find_fixture(self, document_sha256: str) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            raise DomainError("FIXTURE_MANIFEST_MISSING", "Demo fixtures are not installed.", 503)
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainError(
                "FIXTURE_MANIFEST_INVALID", "Demo fixture manifest cannot be loaded.", 503
            ) from exc
        fixtures = manifest.get("fixtures") if isinstance(manifest, dict) else None
        if not isinstance(fixtures, list):
            raise DomainError("FIXTURE_MANIFEST_INVALID", "Demo fixture manifest is invalid.", 503)
        return next(
            (
                row
                for row in fixtures
                if isinstance(row, dict) and row.get("sha256") == document_sha256
            ),
            None,
        )

    def extract_invoice(self, request: ExtractionRequest, **_: Any) -> ExtractionResult:
        return self.extract(document_sha256=request.document_sha256, document_id=request.document_id)

    def extract(self, *, document_sha256: str, document_id: str) -> ExtractionResult:
        item = self.find_fixture(document_sha256)
        if item is None:
            raise DomainError(
                "FIXTURE_NOT_FOUND",
                "This file is not in the fixture set. Choose a labeled demo sample "
                "or configure a live provider.",
                422,
            )
        candidate: dict[str, Any] = json.loads(json.dumps(item["extraction"]))
        candidate["schema_version"] = "1.0"
        evidence_texts = item.get("evidence_texts", {})
        return ExtractionResult(
            provider="fixture",
            model_id=None,
            candidate=candidate,
            evidence_texts=evidence_texts,
            raw_output_ref=f"fixture://{item['sha256']}",
            evidence_pages=item.get("evidence_pages", {}),
            usage={"model_calls": 0, "note": "synthetic fixture; no model invocation"},
            strategy="fixture_manifest",
        )
