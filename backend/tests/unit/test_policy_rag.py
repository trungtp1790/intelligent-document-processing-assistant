from __future__ import annotations

from app.rag.corpus import load_policy_manifest, normalize_search_text
from app.rag.service import _minimum_term_matches, _query_terms


def test_demo_manifest_has_seven_explicitly_synthetic_sources() -> None:
    manifest = load_policy_manifest()

    assert len(manifest["documents"]) == 7
    assert all(document["synthetic"] is True for document in manifest["documents"])
    assert all(
        page["page_no"] >= 1 and page["section"] and page["content"]
        for document in manifest["documents"]
        for page in document["pages"]
    )


def test_vietnamese_query_normalization_is_accent_insensitive_and_preserves_negation() -> None:
    assert normalize_search_text("\u0110\u1ed1i chi\u1ebfu h\u00f3a \u0111\u01a1n") == "doi chieu hoa don"
    terms = _query_terms(
        "Operator kh\u00f4ng c\u00f3 quy\u1ec1n ph\u00ea duy\u1ec7t h\u00f3a \u0111\u01a1n"
    )
    assert terms == [
        "operator",
        "khong",
        "quyen",
        "phe",
        "duyet",
        "hoa",
        "don",
    ]


def test_retrieval_requires_multiple_query_terms_for_a_citation() -> None:
    assert _minimum_term_matches(0) == 1
    assert _minimum_term_matches(1) == 1
    assert _minimum_term_matches(5) == 2
    assert _minimum_term_matches(10) == 4
