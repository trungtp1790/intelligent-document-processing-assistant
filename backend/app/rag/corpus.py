from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PolicyChunk, PolicyDocument


def policy_manifest_path() -> Path:
    repository_path = Path(__file__).resolve().parents[3] / "data" / "policies" / "policy_manifest.json"
    container_path = Path("/app/data/policies/policy_manifest.json")
    for candidate in (repository_path, container_path):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Synthetic policy manifest is missing from the application image.")


def normalize_search_text(value: str) -> str:
    unaccented = unicodedata.normalize("NFKD", value.translate(str.maketrans({"đ": "d", "Đ": "D"})))
    return "".join(character for character in unaccented if not unicodedata.combining(character)).casefold()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_policy_manifest() -> dict[str, Any]:
    manifest = json.loads(policy_manifest_path().read_text(encoding="utf-8"))
    documents = manifest.get("documents")
    if manifest.get("manifest_version") != "1" or not isinstance(documents, list):
        raise ValueError("Unsupported synthetic policy manifest.")
    slugs = {str(document.get("slug", "")) for document in documents}
    if not 5 <= len(slugs) <= 10:
        raise ValueError("The demo corpus must contain between five and ten policy sources.")
    if len(
        {(str(document.get("slug")), int(document.get("version_no", 0))) for document in documents}
    ) != len(documents):
        raise ValueError("Each policy source version must be unique in the manifest.")
    for document in documents:
        if document.get("synthetic") is not True:
            raise ValueError("Every bundled policy source must be explicitly marked synthetic.")
        if not document.get("pages") or any(
            not str(page.get("content", "")).strip() for page in document.get("pages", [])
        ):
            raise ValueError("Every policy source must contain non-empty, page-referenced text.")
    return manifest


def _source_hash(source: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            key: source[key]
            for key in ("slug", "title", "version", "version_no", "synthetic", "pages")
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(canonical)


def seed_policy_corpus(db: Session, org_id: UUID) -> tuple[int, int]:
    """Append verified manifest versions; never update or delete an ingested source."""
    manifest = load_policy_manifest()
    new_documents = 0
    new_chunks = 0
    for source in manifest["documents"]:
        slug = str(source["slug"])
        version_no = int(source["version_no"])
        source_hash = _source_hash(source)
        existing = db.scalar(
            select(PolicyDocument).where(
                PolicyDocument.org_id == org_id,
                PolicyDocument.slug == slug,
                PolicyDocument.version_no == version_no,
            )
        )
        expected = [
            (
                ordinal,
                str(page["section"]),
                int(page["page_no"]),
                str(page["content"]),
                _sha256(str(page["content"])),
                normalize_search_text(str(page["content"])),
            )
            for ordinal, page in enumerate(source["pages"])
        ]
        if existing is not None:
            if (
                existing.source_sha256 != source_hash
                or existing.title != source["title"]
                or existing.version != source["version"]
                or existing.synthetic is not True
            ):
                raise ValueError(f"Policy source version is immutable: {slug} v{version_no}.")
            stored = db.execute(
                select(
                    PolicyChunk.ordinal,
                    PolicyChunk.section,
                    PolicyChunk.page_no,
                    PolicyChunk.content,
                    PolicyChunk.content_sha256,
                    PolicyChunk.search_text,
                )
                .where(
                    PolicyChunk.org_id == org_id,
                    PolicyChunk.policy_document_id == existing.id,
                )
                .order_by(PolicyChunk.ordinal)
            ).all()
            if [tuple(row) for row in stored] != expected:
                raise ValueError(f"Policy source chunks are inconsistent: {slug} v{version_no}.")
            continue

        latest_version = db.scalar(
            select(func.max(PolicyDocument.version_no)).where(
                PolicyDocument.org_id == org_id,
                PolicyDocument.slug == slug,
            )
        )
        expected_version = 1 if latest_version is None else latest_version + 1
        if version_no != expected_version:
            raise ValueError(
                "Policy source versions must be appended sequentially: "
                f"{slug} v{expected_version} expected."
            )
        document = PolicyDocument(
            org_id=org_id,
            slug=slug,
            title=str(source["title"]),
            version=str(source["version"]),
            version_no=version_no,
            source_sha256=source_hash,
            synthetic=True,
        )
        db.add(document)
        db.flush()
        db.add_all(
            [
                PolicyChunk(
                    org_id=org_id,
                    policy_document_id=document.id,
                    ordinal=ordinal,
                    section=section,
                    page_no=page_no,
                    content=content,
                    content_sha256=content_hash,
                    search_text=search_text,
                )
                for ordinal, section, page_no, content, content_hash, search_text in expected
            ]
        )
        db.flush()
        new_documents += 1
        new_chunks += len(expected)
    return new_documents, new_chunks
