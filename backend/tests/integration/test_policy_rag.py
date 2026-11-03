from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.db.models import Membership, Organization, PolicyChunk, PolicyDocument, User
from app.db.session import SessionLocal
from app.main import app
from app.rag.corpus import normalize_search_text, seed_policy_corpus
from app.security import Principal, create_access_token, hash_password

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="requires a migrated PostgreSQL test database"
)


def _tenant(label: str) -> tuple[uuid.UUID, str]:
    suffix = uuid.uuid4().hex[:10]
    with SessionLocal.begin() as db:
        organization = Organization(slug=f"policy-{label}-{suffix}", name=f"Policy {label}")
        user = User(
            email=f"policy-{label}-{suffix}@test.local",
            password_hash=hash_password("Policy-integration-only-9v2!"),
            active=True,
        )
        db.add_all([organization, user])
        db.flush()
        db.add(Membership(org_id=organization.id, user_id=user.id, role="operator"))
        principal = Principal(
            user_id=user.id,
            org_id=organization.id,
            org_slug=organization.slug,
            org_name=organization.name,
            role="operator",
        )
        token, _ = create_access_token(principal)
        return organization.id, token


def _add_policy_version(
    org_id: uuid.UUID,
    *,
    slug: str,
    version: str,
    version_no: int,
    text_content: str,
) -> None:
    source_hash = hashlib.sha256(f"{slug}:{version}:{text_content}".encode()).hexdigest()
    content_hash = hashlib.sha256(text_content.encode("utf-8")).hexdigest()
    with SessionLocal.begin() as db:
        document = PolicyDocument(
            org_id=org_id,
            slug=slug,
            title=f"[DEMO] {slug} {version}",
            version=version,
            version_no=version_no,
            source_sha256=source_hash,
            synthetic=True,
        )
        db.add(document)
        db.flush()
        db.add(
            PolicyChunk(
                org_id=org_id,
                policy_document_id=document.id,
                ordinal=0,
                section="Synthetic update",
                page_no=1,
                content=text_content,
                content_sha256=content_hash,
                search_text=normalize_search_text(text_content),
            )
        )


def test_policy_retrieval_cites_current_source_and_is_tenant_scoped() -> None:
    org_a, token_a = _tenant("a")
    _, token_b = _tenant("b")
    with SessionLocal.begin() as db:
        assert seed_policy_corpus(db, org_a) == (7, 14)
        assert seed_policy_corpus(db, org_a) == (0, 0)
        assert db.scalar(
            select(func.count()).select_from(PolicyDocument).where(PolicyDocument.org_id == org_a)
        ) == 7

    latest_text = (
        "B\u1ea3n c\u1eadp nh\u1eadt demo: khi ch\u00eanh l\u1ec7ch s\u1ed1 l\u01b0\u1ee3ng PO "
        "ch\u01b0a r\u00f5, t\u1ea1o b\u1ea3n ki\u1ec3m tra m\u1edbi "
        "v\u00e0 chuy\u1ec3n reviewer xem x\u00e9t."
    )
    _add_policy_version(
        org_a,
        slug="discrepancy-handling",
        version="2.0",
        version_no=2,
        text_content=latest_text,
    )
    _add_policy_version(
        org_a,
        slug="versioning-regression",
        version="1.0",
        version_no=1,
        text_content="Legacy-only-term archiveonly.",
    )
    _add_policy_version(
        org_a,
        slug="versioning-regression",
        version="2.0",
        version_no=2,
        text_content="Current-version-only term currentonly.",
    )

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/policies/query",
            json={"question": "s\u1ed1 l\u01b0\u1ee3ng tr\u00ean h\u00f3a \u0111\u01a1n kh\u00e1c PO"},
        ).status_code == 401

        result = client.post(
            "/api/v1/policies/query",
            headers={"Authorization": f"Bearer {token_a}"},
            json={
                "question": (
                    "ch\u00eanh l\u1ec7ch s\u1ed1 l\u01b0\u1ee3ng "
                    "phi\u00ean b\u1ea3n c\u1eadp nh\u1eadt"
                )
            },
        )
        assert result.status_code == 200
        body = result.json()
        assert body["status"] == "answered"
        assert body["generation"] == "extractive"
        assert body["synthetic"] is True
        assert body["citations"]
        assert body["citations"][0]["source_slug"] == "discrepancy-handling"
        assert body["citations"][0]["source_version"] == "2.0"
        assert body["citations"][0]["page_no"] == 1
        assert body["citations"][0]["content_sha256"] == hashlib.sha256(
            latest_text.encode("utf-8")
        ).hexdigest()
        assert body["citations"][0]["synthetic"] is True

        old_version_only = client.post(
            "/api/v1/policies/query",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"question": "archiveonly"},
        )
        assert old_version_only.status_code == 200
        assert old_version_only.json()["status"] == "insufficient_evidence"

        sources = client.get(
            "/api/v1/policies/sources",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert sources.status_code == 200
        assert sources.json()["synthetic"] is True
        current_discrepancy = next(
            source for source in sources.json()["sources"] if source["slug"] == "discrepancy-handling"
        )
        assert current_discrepancy["version"] == "2.0"

        other_tenant = client.post(
            "/api/v1/policies/query",
            headers={"Authorization": f"Bearer {token_b}"},
            json={
                "question": (
                    "ch\u00eanh l\u1ec7ch s\u1ed1 l\u01b0\u1ee3ng "
                    "phi\u00ean b\u1ea3n c\u1eadp nh\u1eadt"
                )
            },
        )
        assert other_tenant.status_code == 200
        assert other_tenant.json()["status"] == "insufficient_evidence"
        assert other_tenant.json()["citations"] == []
        assert client.get(
            "/api/v1/policies/sources",
            headers={"Authorization": f"Bearer {token_b}"},
        ).json()["sources"] == []

        extra_org = client.post(
            "/api/v1/policies/query",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"question": "so luong khac hoa don", "org_id": str(org_a)},
        )
        assert extra_org.status_code == 422


def test_policy_query_abstains_and_database_enforces_immutability() -> None:
    org_id, token = _tenant("immutable")
    with SessionLocal.begin() as db:
        assert seed_policy_corpus(db, org_id) == (7, 14)
        document = db.scalar(
            select(PolicyDocument).where(
                PolicyDocument.org_id == org_id,
                PolicyDocument.slug == "guidance-boundaries",
            )
        )
        assert document is not None
        document_id = document.id

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/policies/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "quantum blockchain entropy carbon"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "insufficient_evidence"
        assert response.json()["citations"] == []
        assert "ch\u01b0a t\u00ecm th\u1ea5y \u0111\u1ee7 ngu\u1ed3n" in response.json()[
            "answer"
        ].casefold()

    with pytest.raises(DBAPIError):
        with SessionLocal.begin() as db:
            db.execute(
                update(PolicyDocument)
                .where(PolicyDocument.org_id == org_id, PolicyDocument.id == document_id)
                .values(title="tampered")
            )
