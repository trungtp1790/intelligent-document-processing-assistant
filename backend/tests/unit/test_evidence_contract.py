from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.providers.protocol import ExtractionResult
from app.schemas import EvidenceResponse
from app.services.review import (
    _evidence_response,
    _include_normalization_issues,
    _include_page_provenance_issues,
)


def _evidence(**changes):
    values = {
        "field_path": "subtotal",
        "page_no": None,
        "raw_value": "1.234.567,89",
        "normalized_value": "1234567.89",
        "raw_text": None,
        "bbox": None,
        "confidence": None,
        "normalization_issue": None,
        "origin": "bedrock",
        "verification_state": "UNVERIFIED",
        "actor_id": None,
        "note": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_provider_result_defaults_optional_bbox_and_confidence_to_absent() -> None:
    result = ExtractionResult(
        provider="fixture",
        model_id=None,
        candidate={"subtotal": "1234"},
        evidence_texts={},
        raw_output_ref=None,
    )

    assert result.evidence_bboxes == {}
    assert result.evidence_confidences == {}
    assert result.evidence_pages == {}


def test_api_evidence_contract_preserves_raw_normalized_and_nullable_metadata() -> None:
    payload = _evidence_response(_evidence())

    assert payload["raw_value"] == "1.234.567,89"
    assert payload["normalized_value"] == "1234567.89"
    assert payload["page_no"] is None
    assert payload["verification_state"] == "MISSING_PROVENANCE"
    assert payload["bbox"] is None
    assert payload["confidence"] is None


def test_api_evidence_contract_passes_through_provider_bbox_and_confidence() -> None:
    bbox = {"polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.3], [0.1, 0.3]], "unit": "normalized"}
    payload = _evidence_response(_evidence(page_no=2, bbox=bbox, confidence=0.87))

    assert payload["page_no"] == 2
    assert payload["bbox"] == bbox
    assert payload["confidence"] == 0.87


def test_normalization_issue_is_bound_to_review_and_blocks_ambiguous_snapshot() -> None:
    result = {"status": "PASS", "issues": []}
    evidence = [_evidence(normalization_issue="AMBIGUOUS_NUMBER")]

    merged = _include_normalization_issues(result, evidence)

    assert merged["status"] == "BLOCKED"
    assert merged["issues"] == [
        {"code": "AMBIGUOUS_NUMBER", "severity": "BLOCK", "field_path": "subtotal"}
    ]


def test_missing_source_page_is_a_field_level_blocking_review_issue() -> None:
    result = {"status": "WARNING", "issues": []}

    merged = _include_page_provenance_issues(result, {"invoice_number"}, set())

    assert merged["status"] == "BLOCKED"
    assert merged["issues"] == [
        {"code": "SOURCE_PAGE_MISSING", "severity": "BLOCK", "field_path": "invoice_number"}
    ]


def test_out_of_range_source_page_is_a_field_level_blocking_review_issue() -> None:
    result = {"status": "PASS", "issues": []}

    merged = _include_page_provenance_issues(result, set(), {"subtotal"})

    assert merged["status"] == "BLOCKED"
    assert merged["issues"] == [
        {"code": "SOURCE_PAGE_INVALID", "severity": "BLOCK", "field_path": "subtotal"}
    ]


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf")])
def test_evidence_api_rejects_invalid_provider_confidence(confidence: float) -> None:
    with pytest.raises(ValidationError):
        EvidenceResponse(
            field_path="subtotal",
            origin="provider",
            verification_state="PAGE_REFERENCED",
            confidence=confidence,
        )
