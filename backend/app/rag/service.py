from __future__ import annotations

import math
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.rag.corpus import normalize_search_text

MAX_CITATIONS = 5
MAX_CANDIDATES = 40
MAX_QUERY_TERMS = 12
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = {
    "a", "ai", "and", "are", "cac", "can", "co", "cua", "do", "duoc",
    "for", "gi", "how", "is", "khi", "la", "lam", "nay", "nhung",
    "of", "the", "thi", "toi", "trong", "va", "ve", "voi",
    "what", "when", "where", "which", "who", "why",
}


def _query_terms(question: str) -> list[str]:
    normalized = normalize_search_text(question)
    unique = dict.fromkeys(_TOKEN.findall(normalized))
    return [term for term in unique if term not in _STOPWORDS][:MAX_QUERY_TERMS]


def _minimum_term_matches(term_count: int) -> int:
    if term_count <= 1:
        return 1
    return max(2, math.ceil(term_count * 0.4))


def query_policy(db: Session, org_id: UUID, question: str) -> dict[str, Any]:
    """Return extractive, tenant-scoped passages; no model or policy action is invoked."""
    terms = _query_terms(question)
    if not terms:
        return _insufficient_answer()
    ts_query = " | ".join(terms)
    rows = db.execute(
        text(
            """
            SELECT d.slug, d.title, d.version, d.source_sha256, d.synthetic,
                   c.section, c.page_no, c.ordinal, c.content, c.content_sha256,
                   tsvector_to_array(c.search_vector) AS search_terms,
                   ts_rank_cd(c.search_vector, to_tsquery('simple', :ts_query)) AS rank
              FROM policy_chunks AS c
              JOIN policy_documents AS d
                ON d.org_id = c.org_id AND d.id = c.policy_document_id
             WHERE c.org_id = :org_id
               AND d.version_no = (
                    SELECT max(latest.version_no)
                      FROM policy_documents AS latest
                     WHERE latest.org_id = :org_id
                       AND latest.slug = d.slug
               )
               AND c.search_vector @@ to_tsquery('simple', :ts_query)
             ORDER BY rank DESC, d.slug, c.page_no, c.ordinal
             LIMIT :candidate_limit
            """
        ),
        {"org_id": org_id, "ts_query": ts_query, "candidate_limit": MAX_CANDIDATES},
    ).mappings()

    min_matches = _minimum_term_matches(len(terms))
    citations: list[dict[str, Any]] = []
    for row in rows:
        matched = set(terms).intersection(str(term).casefold() for term in row["search_terms"])
        if len(matched) < min_matches:
            continue
        citations.append(
            {
                "citation_id": f"S{len(citations) + 1}",
                "source_slug": row["slug"],
                "source_title": row["title"],
                "source_version": row["version"],
                "source_sha256": row["source_sha256"],
                "section": row["section"],
                "page_no": row["page_no"],
                "quote": row["content"],
                "content_sha256": row["content_sha256"],
                "synthetic": row["synthetic"],
            }
        )
        if len(citations) >= MAX_CITATIONS:
            break

    if not citations:
        return _insufficient_answer()
    return {
        "status": "answered",
        "answer": (
            f"Tìm thấy {len(citations)} đoạn hướng dẫn tổng hợp có liên quan. "
            "Các trích dẫn bên dưới là nội dung nguồn; chúng không thay đổi quy tắc đối soát "
            "hoặc quyết định phê duyệt."
        ),
        "citations": citations,
        "synthetic": True,
        "generation": "extractive",
    }


def _insufficient_answer() -> dict[str, Any]:
    return {
        "status": "insufficient_evidence",
        "answer": (
            "Chưa tìm thấy đủ nguồn trong bộ hướng dẫn demo để trả lời. "
            "Không có kết luận chính sách nào được đưa ra."
        ),
        "citations": [],
        "synthetic": True,
        "generation": "extractive",
    }


def list_policy_sources(db: Session, org_id: UUID) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT d.slug, d.title, d.version, d.source_sha256, d.synthetic,
                   count(c.id) AS chunk_count
              FROM policy_documents AS d
              JOIN policy_chunks AS c
                ON c.org_id = d.org_id AND c.policy_document_id = d.id
             WHERE d.org_id = :org_id
               AND d.version_no = (
                    SELECT max(latest.version_no)
                      FROM policy_documents AS latest
                     WHERE latest.org_id = :org_id
                       AND latest.slug = d.slug
               )
             GROUP BY d.id
             ORDER BY d.slug
            """
        ),
        {"org_id": org_id},
    ).mappings()
    return [
        {
            "slug": row["slug"],
            "title": row["title"],
            "version": row["version"],
            "source_sha256": row["source_sha256"],
            "chunk_count": row["chunk_count"],
            "synthetic": row["synthetic"],
        }
        for row in rows
    ]
