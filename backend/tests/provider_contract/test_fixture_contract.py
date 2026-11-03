from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.domain.errors import DomainError
from app.providers.fixture import MANIFEST_PATH, FixtureProvider
from app.providers.protocol import ExtractionPage, ExtractionRequest


def test_all_demo_fixtures_are_hash_bound_and_synthetic() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert len(manifest["fixtures"]) == 12
    provider = FixtureProvider()
    for row in manifest["fixtures"]:
        data = (MANIFEST_PATH.parent / row["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"]
        assert row["synthetic"] is True
        assert b"DEMO" in data or row["file"].endswith(".png")
        result = provider.extract_invoice(
            ExtractionRequest(
                document_sha256=row["sha256"],
                document_id="fixture-test",
                mime="image/png" if row["file"].endswith(".png") else "application/pdf",
                pages=(ExtractionPage(page_no=1),),
            )
        )
        assert result.candidate["invoice_number"] == row["extraction"]["invoice_number"]
        assert result.usage["model_calls"] == 0


def test_fixture_unknown_hash_fails_closed(tmp_path: Path) -> None:
    manifest = tmp_path / "fixture_manifest.json"
    manifest.write_text('{"schema_version":"1.0","fixtures":[]}', encoding="utf-8")
    with pytest.raises(DomainError) as captured:
        FixtureProvider(manifest).extract(document_sha256="0" * 64, document_id="nope")
    assert captured.value.code == "FIXTURE_NOT_FOUND"
