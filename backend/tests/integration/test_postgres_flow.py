from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from threading import Barrier, local

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

import app.services.extraction as extraction_service
from app import main as api_main
from app.db.models import (
    ApprovalRecord,
    AuditEvent,
    Document,
    ExtractionEvidence,
    ExtractionRun,
    IdempotencyRequest,
    Invoice,
    InvoiceVersion,
    Job,
    Membership,
    Organization,
    OutboxEvent,
    ProviderCall,
    PurchaseOrder,
    PurchaseOrderLine,
    ReviewDecision,
    Supplier,
    User,
)
from app.db.session import SessionLocal, engine
from app.jobs import dispatcher as dispatcher_module
from app.jobs.celery_app import celery_app
from app.jobs.dispatcher import dispatch_once, recover_expired_leases
from app.login_rate_limit import LOGIN_LIMIT, clear_login_attempts
from app.main import app
from app.providers.errors import ProviderFailure
from app.provisioning_cli import provision_organization, provision_user
from app.security import hash_password, verify_password
from app.services.extraction import (
    process_invoice_job,
    record_provider_invocation,
    reserve_provider_invocation,
)
from app.workflows.checkpoint import postgres_checkpointer
from app.workflows.invoice_graph import build_invoice_graph, run_invoice_graph

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="requires a migrated PostgreSQL test database"
)
client = TestClient(app)
PASSWORD = "Integration-test-only-9v2!"
_TEST_FILE = Path(__file__).resolve()


def _find_root() -> Path:
    for parent in _TEST_FILE.parents:
        if (parent / "data" / "demo" / "fixture_manifest.json").exists():
            return parent
    return _TEST_FILE.parents[2]


ROOT = _find_root()


def setup_tenant(role: str = "reviewer") -> tuple[str, str, uuid.UUID]:
    suffix = uuid.uuid4().hex[:10]
    slug = f"test-{suffix}"
    email = f"{role}-{suffix}@test.local"
    with SessionLocal.begin() as db:
        org = Organization(slug=slug, name=f"Test {suffix}")
        user = User(email=email, password_hash=hash_password(PASSWORD), active=True)
        db.add_all([org, user])
        db.flush()
        db.add(Membership(org_id=org.id, user_id=user.id, role=role))
        supplier = Supplier(org_id=org.id, tax_id_normalized="DEMO-SUP-001", name="Demo Supplier")
        db.add(supplier)
        db.flush()
        for po_number, sku, quantity, price in [
            ("PO-DEMO-001", "KB-001", "10", "200000"),
            ("PO-DEMO-002", "KB-001", "10", "200000"),
            ("PO-DEMO-003", "MOUSE-001", "5", "120000"),
        ]:
            po = PurchaseOrder(
                org_id=org.id, supplier_id=supplier.id, po_number=po_number, currency="VND", state="OPEN"
            )
            db.add(po)
            db.flush()
            db.add(
                PurchaseOrderLine(
                    org_id=org.id,
                    po_id=po.id,
                    line_no=1,
                    sku=sku,
                    description={"KB-001": "Keyboard", "MOUSE-001": "Mouse"}[sku],
                    unit="piece",
                    quantity=Decimal(quantity),
                    unit_price=Decimal(price),
                )
            )
    return email, slug, org.id


def test_operator_provisioning_is_audited_idempotent_and_organization_scoped() -> None:
    suffix = uuid.uuid4().hex[:12]
    slug_a = f"provision-{suffix}-a"
    slug_b = f"provision-{suffix}-b"
    email = f"provision-{suffix}@test.local"
    password = "Ops-Initial-Password-123"

    with SessionLocal.begin() as db:
        org_a, created = provision_organization(db, slug_a, f"Provisioned {suffix} A")
        assert created
    with SessionLocal.begin() as db:
        org_a_replay, created = provision_organization(db, slug_a, f"Provisioned {suffix} A")
        assert not created and org_a_replay == org_a
        user_id, created = provision_user(
            db,
            email=email,
            org_slug=slug_a,
            role="reviewer",
            password=password,
        )
        assert created

    with SessionLocal.begin() as db:
        replay_id, created = provision_user(
            db,
            email=email.upper(),
            org_slug=slug_a,
            role="reviewer",
        )
        assert not created and replay_id == user_id
        with pytest.raises(ValueError, match="different role"):
            provision_user(db, email=email, org_slug=slug_a, role="operator")
        with pytest.raises(ValueError, match="operator or reviewer"):
            provision_user(db, email=email, org_slug=slug_a, role="admin")
        org_b, created = provision_organization(db, slug_b, f"Provisioned {suffix} B")
        assert created
        second_id, created = provision_user(
            db,
            email=email,
            org_slug=slug_b,
            role="operator",
        )
        assert created and second_id == user_id

    with SessionLocal() as db:
        user = db.get(User, user_id)
        memberships = db.scalars(select(Membership).where(Membership.user_id == user_id)).all()
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.org_id.in_([org_a, org_b]))
        ).all()
        assert user is not None and verify_password(password, user.password_hash)
        assert sorted((membership.org_id, membership.role) for membership in memberships) == sorted(
            [(org_a, "reviewer"), (org_b, "operator")]
        )
        assert sorted(event.action for event in events) == [
            "ORGANIZATION_PROVISIONED",
            "ORGANIZATION_PROVISIONED",
            "USER_MEMBERSHIP_PROVISIONED",
            "USER_MEMBERSHIP_PROVISIONED",
        ]
        assert all(event.actor_id is None for event in events)
        assert all("password" not in event.metadata_json for event in events)


def login(email: str, slug: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD, "org_slug": slug}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def sample(filename: str) -> bytes:
    return (ROOT / "data" / "demo" / filename).read_bytes()


def test_missing_source_page_blocks_approval_until_reviewer_attests_a_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "register_login_attempt", lambda *_args: 1)
    original_candidate_and_evidence = extraction_service._candidate_and_evidence

    def omit_invoice_number_page(*args, **kwargs):
        canonical, evidence, result, raw_output_ref = original_candidate_and_evidence(*args, **kwargs)
        evidence["invoice_number"] = replace(
            evidence["invoice_number"], page_no=None, verification_state="MISSING_PROVENANCE"
        )
        return canonical, evidence, result, raw_output_ref

    monkeypatch.setattr(extraction_service, "_candidate_and_evidence", omit_invoice_number_page)
    email, slug, org_id = setup_tenant("reviewer")
    token = login(email, slug)
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("valid.pdf", sample("invoice_valid_native.pdf"), "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    item = upload.json()
    run_invoice_graph(item["job_id"])

    detail = client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    old_version_id = uuid.UUID(detail["version"]["id"])
    missing_page = next(row for row in detail["evidence"] if row["field_path"] == "invoice_number")
    assert missing_page["page_no"] is None
    assert missing_page["verification_state"] == "MISSING_PROVENANCE"
    assert missing_page["bbox"] is None
    assert missing_page["confidence"] is None
    invalid_evidence = ExtractionEvidence(
        org_id=org_id,
        invoice_id=uuid.UUID(item["invoice_id"]),
        version_id=old_version_id,
        field_path="constraint_probe",
        page_no=None,
        raw_value="synthetic",
        normalized_value="synthetic",
        origin="provider",
        verification_state="UNVERIFIED",
    )
    with SessionLocal.begin() as db:
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                db.add(invalid_evidence)
                db.flush()

    po = next(
        row
        for row in client.get(
            "/api/v1/purchase-orders", headers={"Authorization": f"Bearer {token}"}
        ).json()
        if row["po_number"] == "PO-DEMO-001"
    )
    reconciliation = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/reconciliations",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_version_id": detail["version"]["id"], "po_id": po["id"], "line_mapping": {}},
    )
    assert reconciliation.status_code == 201, reconciliation.text
    blocked = reconciliation.json()
    assert blocked["status"] == "BLOCKED"
    assert {
        "code": "SOURCE_PAGE_MISSING",
        "severity": "BLOCK",
        "field_path": "invoice_number",
    } in blocked["issues"]

    denied = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/decisions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "action": "APPROVE",
            "expected_version_id": detail["version"]["id"],
            "reconciliation_id": blocked["reconciliation_id"],
            "expected_snapshot_hash": blocked["snapshot_hash"],
            "acknowledged_warning_codes": [],
        },
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "APPROVAL_BLOCKED"
    assert "SOURCE_PAGE_MISSING" in denied.json()["error"]["details"]["issue_codes"]

    draft = dict(detail["version"]["data"])
    draft.pop("document_id", None)
    added_evidence = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/versions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "expected_version_id": detail["version"]["id"],
            "reason": "Đã đối chiếu số hóa đơn trên chứng từ gốc",
            "manual_source_page": 1,
            "manual_evidence_pages": {"invoice_number": 1},
            "draft": draft,
        },
    )
    assert added_evidence.status_code == 201, added_evidence.text
    revised = client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    revised_page = next(row for row in revised["evidence"] if row["field_path"] == "invoice_number")
    assert revised_page["page_no"] == 1
    assert revised_page["origin"] == "manual"
    assert revised_page["verification_state"] == "MANUAL"
    assert revised_page["raw_value"] == missing_page["raw_value"]
    assert revised_page["normalized_value"] == missing_page["normalized_value"]
    assert revised_page["bbox"] is None
    assert revised_page["confidence"] is None
    with SessionLocal() as db:
        prior_evidence = db.scalar(
            select(ExtractionEvidence).where(
                ExtractionEvidence.version_id == old_version_id,
                ExtractionEvidence.field_path == "invoice_number",
            )
        )
        assert prior_evidence is not None and prior_evidence.page_no is None
        assert prior_evidence.verification_state == "MISSING_PROVENANCE"

    second_reconciliation = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/reconciliations",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_version_id": revised["version"]["id"], "po_id": po["id"], "line_mapping": {}},
    )
    assert second_reconciliation.status_code == 201, second_reconciliation.text
    second = second_reconciliation.json()
    assert second["status"] == "WARNING"
    assert "SOURCE_PAGE_MISSING" not in {issue["code"] for issue in second["issues"]}
    assert "SOURCE_NOT_TEXT_VERIFIED" in {issue["code"] for issue in second["issues"]}
    approved = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/decisions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "action": "APPROVE",
            "expected_version_id": revised["version"]["id"],
            "reconciliation_id": second["reconciliation_id"],
            "expected_snapshot_hash": second["snapshot_hash"],
            "acknowledged_warning_codes": ["SOURCE_NOT_TEXT_VERIFIED"],
            "note": "Đã kiểm tra trang nguồn.",
        },
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["status"] == "APPROVED"


def test_fixture_mode_rejects_unlisted_upload_before_persisting_it() -> None:
    email, slug, org_id = setup_tenant("operator")
    token = login(email, slug)
    image = Image.open(BytesIO(sample("invoice_scan_valid.png")))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("fixture-test", uuid.uuid4().hex)
    stream = BytesIO()
    image.save(stream, format="PNG", pnginfo=metadata)
    content = stream.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    key = str(uuid.uuid4())

    response = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        files={"file": ("private-invoice.png", content, "image/png")},
        data={"locale": "vi-VN"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FIXTURE_NOT_FOUND"
    with SessionLocal() as db:
        assert db.scalar(
            select(Document.id).where(Document.org_id == org_id, Document.sha256 == digest)
        ) is None
        assert db.scalar(
            select(IdempotencyRequest.id).where(
                IdempotencyRequest.org_id == org_id,
                IdempotencyRequest.route_key == "POST /documents",
                IdempotencyRequest.key == key,
            )
        ) is None


def test_migrated_postgres_upload_fixture_mismatch_and_reviewer_gate() -> None:
    reviewer_email, reviewer_slug, reviewer_org_id = setup_tenant("reviewer")
    reviewer_token = login(reviewer_email, reviewer_slug)
    file_content = sample("invoice_quantity_mismatch.pdf")
    first_key = str(uuid.uuid4())
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {reviewer_token}", "Idempotency-Key": first_key},
        files={"file": ("mismatch.pdf", file_content, "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    item = upload.json()
    replay = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {reviewer_token}", "Idempotency-Key": first_key},
        files={"file": ("mismatch.pdf", file_content, "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert replay.status_code == 202
    assert replay.json() == item
    duplicate = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {reviewer_token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("same.pdf", file_content, "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["invoice_id"] == item["invoice_id"]
    assert duplicate.json()["job_id"] is None

    run_invoice_graph(item["job_id"])
    with SessionLocal() as db:
        active_job = db.scalar(select(Job).where(Job.id == uuid.UUID(item["job_id"])))
        assert active_job is not None and active_job.status == "WAITING_REVIEW"
        assert active_job.lease_until is None
        graph_thread_id = str(active_job.workflow_thread_id)
    with postgres_checkpointer() as checkpointer:
        graph = build_invoice_graph(checkpointer)
        paused = graph.get_state({"configurable": {"thread_id": graph_thread_id}})
        assert paused.next == ("await_review",)
        assert "document_bytes" not in paused.values
        assert "raw_output" not in paused.values
    detail = client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers={"Authorization": f"Bearer {reviewer_token}"}
    )
    assert detail.status_code == 200, detail.text
    detail_payload = detail.json()
    assert detail_payload["fixture_mode"] is True
    assert detail_payload["version"]["data"]["invoice_number"] == "DEMO-0002"
    assert isinstance(detail_payload["pages"], int)
    assert detail_payload["pages"] == detail_payload["document"]["pages"]
    assert len(detail_payload["document_pages"]) == detail_payload["pages"]
    assert detail_payload["content_url"] == f"/documents/{item['document_id']}/content"
    assert detail_payload["document_pages"][0]["preview_url"] == (
        f"/documents/{item['document_id']}/pages/1"
    )
    content_response = client.get(
        f"/api/v1{detail_payload['content_url']}",
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert content_response.status_code == 200
    assert content_response.content == file_content
    page_response = client.get(
        f"/api/v1{detail_payload['document_pages'][0]['preview_url']}",
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert page_response.status_code == 200
    assert page_response.content == file_content
    po = next(
        po
        for po in client.get(
            "/api/v1/purchase-orders", headers={"Authorization": f"Bearer {reviewer_token}"}
        ).json()
        if po["po_number"] == "PO-DEMO-002"
    )
    reconciliation = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/reconciliations",
        headers={"Authorization": f"Bearer {reviewer_token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_version_id": detail.json()["version"]["id"], "po_id": po["id"], "line_mapping": {}},
    )
    assert reconciliation.status_code == 201, reconciliation.text
    assert reconciliation.json()["status"] == "BLOCKED"
    decision = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/decisions",
        headers={"Authorization": f"Bearer {reviewer_token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "action": "APPROVE",
            "expected_version_id": detail.json()["version"]["id"],
            "reconciliation_id": reconciliation.json()["reconciliation_id"],
            "expected_snapshot_hash": reconciliation.json()["snapshot_hash"],
            "acknowledged_warning_codes": [],
            "note": "Checked",
        },
    )
    assert decision.status_code == 409
    assert decision.json()["error"]["code"] == "APPROVAL_BLOCKED"
    with SessionLocal() as db:
        assert db.scalar(select(ApprovalRecord.id).where(ApprovalRecord.org_id == reviewer_org_id)) is None

    other_email, other_slug, other_org_id = setup_tenant("reviewer")
    other_token = login(other_email, other_slug)
    other_headers = {"Authorization": f"Bearer {other_token}"}
    assert client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers=other_headers
    ).status_code == 404
    assert client.get(
        f"/api/v1/invoices/{item['invoice_id']}/versions", headers=other_headers
    ).status_code == 404
    assert client.get(
        f"/api/v1/invoices/{item['invoice_id']}/audit", headers=other_headers
    ).status_code == 404
    assert client.get(f"/api/v1/jobs/{item['job_id']}", headers=other_headers).status_code == 404
    forbidden_file = client.get(
        f"/api/v1/documents/{item['document_id']}/content", headers=other_headers
    )
    assert forbidden_file.status_code == 404
    assert client.get(
        f"/api/v1/documents/{item['document_id']}/pages/1", headers=other_headers
    ).status_code == 404
    assert client.get(
        f"/api/v1/exports/invoices/{item['invoice_id']}.json", headers=other_headers
    ).status_code == 404
    assert client.get(
        f"/api/v1/exports/invoices/{item['invoice_id']}.csv", headers=other_headers
    ).status_code == 404
    other_purchase_orders = client.get("/api/v1/purchase-orders", headers=other_headers).json()
    assert all(row["id"] != po["id"] for row in other_purchase_orders)

    foreign_version = dict(detail.json()["version"]["data"])
    foreign_version.pop("document_id", None)
    assert client.post(
        f"/api/v1/invoices/{item['invoice_id']}/versions",
        headers={**other_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "expected_version_id": detail.json()["version"]["id"],
            "reason": "Cross-tenant mutation attempt",
            "manual_source_page": 1,
            "draft": foreign_version,
        },
    ).status_code == 404
    assert client.post(
        f"/api/v1/invoices/{item['invoice_id']}/reconciliations",
        headers={**other_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "expected_version_id": detail.json()["version"]["id"],
            "po_id": po["id"],
            "line_mapping": {},
        },
    ).status_code == 404
    assert client.post(
        f"/api/v1/invoices/{item['invoice_id']}/decisions",
        headers={**other_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "action": "APPROVE",
            "expected_version_id": detail.json()["version"]["id"],
            "reconciliation_id": reconciliation.json()["reconciliation_id"],
            "expected_snapshot_hash": reconciliation.json()["snapshot_hash"],
            "acknowledged_warning_codes": [],
            "note": "Cross-tenant mutation attempt",
        },
    ).status_code == 404

    same_bytes_other_tenant = client.post(
        "/api/v1/documents",
        headers={**other_headers, "Idempotency-Key": first_key},
        files={"file": ("mismatch.pdf", file_content, "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert same_bytes_other_tenant.status_code == 202, same_bytes_other_tenant.text
    assert same_bytes_other_tenant.json()["duplicate"] is False
    assert same_bytes_other_tenant.json()["invoice_id"] != item["invoice_id"]
    assert same_bytes_other_tenant.json()["document_id"] != item["document_id"]
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count(Document.id)).where(Document.org_id == reviewer_org_id)
        ) >= 1
        assert db.scalar(
            select(func.count(Document.id)).where(Document.org_id == other_org_id)
        ) == 1


def test_valid_fixture_revision_approval_export_and_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "register_login_attempt", lambda *_args: 1)
    email, slug, _ = setup_tenant("reviewer")
    token = login(email, slug)
    file_content = sample("invoice_valid_native.pdf")
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("valid.pdf", file_content, "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    item = upload.json()
    run_invoice_graph(item["job_id"])
    with SessionLocal() as db:
        initial_job = db.scalar(select(Job).where(Job.id == uuid.UUID(item["job_id"])))
        assert initial_job is not None and initial_job.status == "WAITING_REVIEW"
        assert initial_job.lease_until is None
        initial_thread_id = str(initial_job.workflow_thread_id)
    detail = client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert detail["evidence"]
    assert all(evidence["verification_state"] == "TEXT_VERIFIED" for evidence in detail["evidence"])
    assert all(1 <= evidence["page_no"] <= detail["pages"] for evidence in detail["evidence"])
    original_evidence = {row["field_path"]: row for row in detail["evidence"]}
    po = next(
        row
        for row in client.get("/api/v1/purchase-orders", headers={"Authorization": f"Bearer {token}"}).json()
        if row["po_number"] == "PO-DEMO-001"
    )
    recon_key = str(uuid.uuid4())
    recon_body = {"expected_version_id": detail["version"]["id"], "po_id": po["id"], "line_mapping": {}}
    first_recon = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/reconciliations",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": recon_key},
        json=recon_body,
    )
    assert first_recon.status_code == 201, first_recon.text
    assert first_recon.json()["status"] == "PASS"
    provenance = list(first_recon.json()["mapping_provenance"].values())
    assert provenance and all(row["origin"] == "sku_auto" for row in provenance)
    assert all(len(row["actor_id"]) == 36 for row in provenance)
    stale_recon_id = first_recon.json()["reconciliation_id"]

    draft = dict(detail["version"]["data"])
    draft.pop("document_id", None)
    draft["supplier_name"] = "Demo Supplier manually confirmed"
    missing_manual_page = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/versions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "expected_version_id": detail["version"]["id"],
            "reason": "Confirm supplier name",
            "draft": draft,
        },
    )
    assert missing_manual_page.status_code == 422
    assert missing_manual_page.json()["error"]["code"] == "MANUAL_SOURCE_PAGE_REQUIRED"
    revision = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/versions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "expected_version_id": detail["version"]["id"],
            "reason": "Confirm supplier name",
            "manual_source_page": 1,
            "draft": draft,
        },
    )
    assert revision.status_code == 201, revision.text
    stale_decision = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/decisions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "action": "APPROVE",
            "expected_version_id": detail["version"]["id"],
            "reconciliation_id": stale_recon_id,
            "expected_snapshot_hash": first_recon.json()["snapshot_hash"],
            "acknowledged_warning_codes": [],
            "note": "stale",
        },
    )
    assert stale_decision.status_code == 409
    assert stale_decision.json()["error"]["code"] == "STALE_REVIEW_VERSION"

    revised_detail = client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    revised_evidence = {row["field_path"]: row for row in revised_detail["evidence"]}
    for field_path in ("invoice_number", "subtotal"):
        assert revised_evidence[field_path]["page_no"] == original_evidence[field_path]["page_no"]
        assert revised_evidence[field_path]["raw_value"] == original_evidence[field_path]["raw_value"]
        assert (
            revised_evidence[field_path]["normalized_value"]
            == original_evidence[field_path]["normalized_value"]
        )
        assert revised_evidence[field_path]["bbox"] == original_evidence[field_path]["bbox"]
        assert revised_evidence[field_path]["confidence"] == original_evidence[field_path]["confidence"]
    assert revised_evidence["supplier_name"]["page_no"] == 1
    assert revised_evidence["supplier_name"]["verification_state"] == "MANUAL"
    assert revised_evidence["supplier_name"]["raw_value"] is None
    assert revised_evidence["supplier_name"]["raw_text"] is None
    assert revised_evidence["supplier_name"]["normalized_value"] == "Demo Supplier manually confirmed"
    assert revised_evidence["supplier_name"]["bbox"] is None
    assert revised_evidence["supplier_name"]["confidence"] is None
    second_recon = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/reconciliations",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_version_id": revised_detail["version"]["id"], "po_id": po["id"], "line_mapping": {}},
    )
    assert second_recon.status_code == 201, second_recon.text
    second_recon_result = second_recon.json()
    assert second_recon_result["status"] == "WARNING"
    warning_codes = [issue["code"] for issue in second_recon_result["issues"] if issue["severity"] == "WARN"]
    assert "SOURCE_NOT_TEXT_VERIFIED" in warning_codes
    run_invoice_graph(item["job_id"])
    with SessionLocal() as db:
        refreshed_job = db.scalar(select(Job).where(Job.id == uuid.UUID(item["job_id"])))
        assert refreshed_job is not None and refreshed_job.status == "WAITING_REVIEW"
        assert str(refreshed_job.workflow_thread_id) != initial_thread_id
        refreshed_thread_id = str(refreshed_job.workflow_thread_id)
    with postgres_checkpointer() as checkpointer:
        graph = build_invoice_graph(checkpointer)
        refreshed_checkpoint = graph.get_state({"configurable": {"thread_id": refreshed_thread_id}})
        assert refreshed_checkpoint.next == ("await_review",)
        assert refreshed_checkpoint.values["version_id"] == revised_detail["version"]["id"]
        assert refreshed_checkpoint.values["reconciliation_id"] == second_recon_result["reconciliation_id"]
    decision_key = str(uuid.uuid4())
    decision_body = {
        "action": "APPROVE",
        "expected_version_id": revised_detail["version"]["id"],
        "reconciliation_id": second_recon_result["reconciliation_id"],
        "expected_snapshot_hash": second_recon_result["snapshot_hash"],
        "acknowledged_warning_codes": warning_codes,
        "note": "Checked the source and match.",
    }
    approved = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/decisions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": decision_key},
        json=decision_body,
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["status"] == "APPROVED"
    decision_id = approved.json()["decision_id"]
    assert celery_app.conf.task_routes["app.jobs.tasks.resume_invoice_graph"]["queue"] == "invoice"
    sent_tasks: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        dispatcher_module.process_invoice_task,
        "delay",
        lambda job_id: sent_tasks.append(("process", job_id)),
    )
    monkeypatch.setattr(
        dispatcher_module.resume_invoice_graph_task,
        "delay",
        lambda job_id, saved_decision_id: sent_tasks.append(
            ("resume", job_id, saved_decision_id)
        ),
    )
    assert dispatch_once(limit=500) >= 1
    assert ("resume", item["job_id"], decision_id) in sent_tasks
    with SessionLocal() as db:
        resume_event = db.scalar(
            select(OutboxEvent).where(
                OutboxEvent.job_id == uuid.UUID(item["job_id"]),
                OutboxEvent.kind == "RESUME_INVOICE_GRAPH",
            )
        )
        assert resume_event is not None and resume_event.status == "SENT"
        assert resume_event.payload_ref["decision_id"] == decision_id

    # Simulate a worker dying while it owns the per-job PostgreSQL lock, after the
    # reviewer transaction and durable checkpoint have committed.
    crash_worker = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys; from app.workflows.checkpoint import workflow_run_lock; "
            "lock=workflow_run_lock(sys.argv[1]); acquired=lock.__enter__(); "
            "assert acquired; os._exit(73)",
            item["job_id"],
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert crash_worker.returncode == 73, crash_worker.stderr

    # A fresh worker process resumes from the saved thread. Delay graph creation
    # after acquiring the lock so this test can deliver the same resume concurrently.
    with tempfile.TemporaryDirectory(prefix="idp-m4-") as temp_dir:
        marker = str(Path(temp_dir) / "lock-acquired")
        resume_worker_code = """
import sys, time
import app.workflows.invoice_graph as workflow
original = workflow.build_invoice_graph
def delayed_build(checkpointer):
    with open(sys.argv[3], "w", encoding="utf-8") as marker_file:
        marker_file.write("locked")
    time.sleep(2)
    return original(checkpointer)
workflow.build_invoice_graph = delayed_build
workflow.run_invoice_graph(sys.argv[1], sys.argv[2])
"""
        resume_worker = subprocess.Popen(
            [sys.executable, "-c", resume_worker_code, item["job_id"], decision_id, marker],
            cwd=Path(__file__).resolve().parents[2],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 20
        while not Path(marker).exists() and resume_worker.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Path(marker).exists(), "Restarted worker did not acquire the workflow lock"
        duplicate = run_invoice_graph(item["job_id"], decision_id)
        assert duplicate == {"status": "DUPLICATE_DELIVERY", "job_id": item["job_id"]}
        worker_stdout, worker_stderr = resume_worker.communicate(timeout=30)
        assert resume_worker.returncode == 0, f"{worker_stdout}\n{worker_stderr}"

    with SessionLocal() as db:
        finalized_job = db.scalar(select(Job).where(Job.id == uuid.UUID(item["job_id"])))
        assert finalized_job is not None and finalized_job.status == "DONE"
        assert finalized_job.lease_until is None
        assert db.scalar(
            select(func.count(ApprovalRecord.id)).where(
                ApprovalRecord.org_id == finalized_job.org_id,
                ApprovalRecord.invoice_id == uuid.UUID(item["invoice_id"]),
            )
        ) == 1
        assert db.scalar(
            select(func.count(ReviewDecision.id)).where(
                ReviewDecision.org_id == finalized_job.org_id,
                ReviewDecision.invoice_id == uuid.UUID(item["invoice_id"]),
            )
        ) == 1
    assert (
        client.post(
            f"/api/v1/invoices/{item['invoice_id']}/decisions",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": decision_key},
            json=decision_body,
        ).json()
        == approved.json()
    )
    exported = client.get(
        f"/api/v1/exports/invoices/{item['invoice_id']}.json", headers={"Authorization": f"Bearer {token}"}
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["fixture_mode"] is True
    assert exported.json()["approved_version_id"] == revision.json()["version_id"]
    csv_response = client.get(
        f"/api/v1/exports/invoices/{item['invoice_id']}.csv", headers={"Authorization": f"Bearer {token}"}
    )
    assert csv_response.status_code == 200
    assert "fixture_mode" in csv_response.text


def test_expired_lease_recovery_requeues_and_rejects_stale_provider_worker() -> None:
    email, slug, org_id = setup_tenant("operator")
    token = login(email, slug)
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("valid.pdf", sample("invoice_valid_native.pdf"), "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    item = upload.json()
    job_id = uuid.UUID(item["job_id"])
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.org_id == org_id, Job.id == job_id).with_for_update())
        invoice = db.scalar(select(Invoice).where(Invoice.org_id == org_id, Invoice.id == job.entity_id))
        assert job is not None and invoice is not None
        job.status = "PROCESSING"
        job.attempts = 1
        job.fencing_token = 7
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        invoice.workflow_status = "PROCESSING"
        run = db.scalar(
            select(ExtractionRun).where(
                ExtractionRun.org_id == org_id, ExtractionRun.document_id == invoice.document_id
            )
        )
        assert run is not None
        run_id = run.id
        events = db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.org_id == org_id,
                OutboxEvent.job_id == job_id,
                OutboxEvent.status == "PENDING",
            )
        ).all()
        for event in events:
            event.status = "SENT"

    assert recover_expired_leases() == 1
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.org_id == org_id, Job.id == job_id))
        pending = db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.org_id == org_id,
                OutboxEvent.job_id == job_id,
                OutboxEvent.status == "PENDING",
            )
        ).all()
        assert job is not None and job.status == "WAITING_RETRY" and job.lease_until is None
        assert len(pending) == 1
    with pytest.raises(ProviderFailure) as captured:
        reserve_provider_invocation(job_id, 7, run_id, "unused-model", "tool_schema")
    assert captured.value.code == "STALE_WORKER"


def test_provider_invocation_budget_is_persisted_and_limited_to_three() -> None:
    email, slug, org_id = setup_tenant("operator")
    token = login(email, slug)
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("valid.pdf", sample("invoice_valid_native.pdf"), "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    job_id = uuid.UUID(upload.json()["job_id"])
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.org_id == org_id, Job.id == job_id).with_for_update())
        assert job is not None
        job.status = "PROCESSING"
        job.fencing_token = 1
        job.lease_until = datetime.now(UTC) + timedelta(seconds=60)
        run = db.scalar(
            select(ExtractionRun).where(
                ExtractionRun.org_id == org_id,
                ExtractionRun.document_id == uuid.UUID(upload.json()["document_id"]),
            )
        )
        assert run is not None
        run_id = run.id
        db.add_all(
            [
                ProviderCall(
                    org_id=org_id,
                    extraction_run_id=run_id,
                    attempt_no=attempt,
                    provider="bedrock",
                    model_id="test-model",
                    strategy="tool_schema",
                    status="FAILED",
                    failure_code="PROVIDER_TIMEOUT",
                )
                for attempt in (1, 2, 3)
            ]
        )
    with pytest.raises(ProviderFailure) as captured:
        reserve_provider_invocation(job_id, 1, run_id, "test-model", "tool_schema")
    assert captured.value.code == "PROVIDER_CALL_BUDGET_EXCEEDED"


def test_foundry_provider_call_and_usage_are_persisted_without_schema_changes() -> None:
    email, slug, org_id = setup_tenant("operator")
    token = login(email, slug)
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("valid.pdf", sample("invoice_valid_native.pdf"), "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    job_id = uuid.UUID(upload.json()["job_id"])
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.org_id == org_id, Job.id == job_id).with_for_update())
        assert job is not None
        job.status = "PROCESSING"
        job.fencing_token = 1
        job.lease_until = datetime.now(UTC) + timedelta(seconds=60)
        run = db.scalar(
            select(ExtractionRun).where(
                ExtractionRun.org_id == org_id,
                ExtractionRun.document_id == uuid.UUID(upload.json()["document_id"]),
            )
        )
        assert run is not None
        run_id = run.id

    call_id = reserve_provider_invocation(
        job_id,
        1,
        run_id,
        "foundry-invoice-deployment",
        "native_schema",
        provider_name="foundry",
    )
    record_provider_invocation(
        call_id,
        latency_ms=27,
        usage={"inputTokens": 57, "outputTokens": 31},
        failure_code=None,
    )

    with SessionLocal() as db:
        call = db.scalar(select(ProviderCall).where(ProviderCall.id == uuid.UUID(call_id)))
        assert call is not None
        assert call.org_id == org_id
        assert call.extraction_run_id == run_id
        assert call.provider == "foundry"
        assert call.model_id == "foundry-invoice-deployment"
        assert call.strategy == "native_schema"
        assert call.status == "SUCCEEDED"
        assert call.input_tokens == 57
        assert call.output_tokens == 31


def test_operator_cannot_call_decision_route() -> None:
    email, slug, _ = setup_tenant("operator")
    token = login(email, slug)
    response = client.post(
        f"/api/v1/invoices/{uuid.uuid4()}/decisions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={
            "action": "REJECT",
            "expected_version_id": str(uuid.uuid4()),
            "reconciliation_id": str(uuid.uuid4()),
            "expected_snapshot_hash": "0" * 64,
            "acknowledged_warning_codes": [],
            "note": "reason",
        },
    )
    assert response.status_code == 403


def test_concurrent_upload_with_same_idempotency_key_creates_one_record_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email, slug, org_id = setup_tenant("operator")
    token = login(email, slug)
    content = sample("invoice_valid_native.pdf")
    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    barrier = Barrier(2)
    original_upload = api_main.upload_invoice

    def synchronized_upload(*args, **kwargs):
        barrier.wait(timeout=15)
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(api_main, "upload_invoice", synchronized_upload)

    def send_upload():
        with TestClient(app) as request_client:
            return request_client.post(
                "/api/v1/documents",
                headers=headers,
                files={"file": ("valid.pdf", content, "application/pdf")},
                data={"locale": "vi-VN"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: send_upload(), range(2)))

    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()
    item = responses[0].json()
    assert item["duplicate"] is False
    monkeypatch.undo()
    reused_key = client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("renamed.pdf", content, "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert reused_key.status_code == 409
    assert reused_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count(Document.id)).where(Document.org_id == org_id)
        ) == 1
        assert db.scalar(
            select(func.count(Invoice.id)).where(Invoice.org_id == org_id)
        ) == 1
        assert db.scalar(
            select(func.count(Job.id)).where(Job.org_id == org_id, Job.id == uuid.UUID(item["job_id"]))
        ) == 1
        assert db.scalar(
            select(func.count(IdempotencyRequest.id)).where(
                IdempotencyRequest.org_id == org_id,
                IdempotencyRequest.route_key == "POST /documents",
                IdempotencyRequest.key == key,
            )
        ) == 1


def test_concurrent_same_bytes_with_distinct_keys_deduplicate_to_one_document() -> None:
    email, slug, org_id = setup_tenant("operator")
    token = login(email, slug)
    content = sample("invoice_valid_native.pdf")
    keys = [str(uuid.uuid4()), str(uuid.uuid4())]
    headers = [
        {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
        for key in keys
    ]
    lookup_barrier = Barrier(2)
    lookup_sync_state = local()

    def synchronize_empty_hash_lookup(_connection, _cursor, statement, _parameters, _context, _many):
        query = statement.casefold()
        if (
            "from documents" in query
            and "documents.sha256" in query
            and not getattr(lookup_sync_state, "passed", False)
        ):
            lookup_sync_state.passed = True
            lookup_barrier.wait(timeout=15)

    event.listen(engine, "before_cursor_execute", synchronize_empty_hash_lookup)
    try:
        def send_upload(index: int):
            with TestClient(app) as request_client:
                return request_client.post(
                    "/api/v1/documents",
                    headers=headers[index],
                    files={"file": (f"valid-{index}.pdf", content, "application/pdf")},
                    data={"locale": "vi-VN"},
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(send_upload, range(2)))
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_empty_hash_lookup)

    assert [response.status_code for response in responses] == [202, 202]
    assert len({response.json()["document_id"] for response in responses}) == 1
    assert len({response.json()["invoice_id"] for response in responses}) == 1
    assert {response.json()["duplicate"] for response in responses} == {False, True}
    assert sum(response.json()["job_id"] is not None for response in responses) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Document.id)).where(Document.org_id == org_id)) == 1
        assert db.scalar(select(func.count(Invoice.id)).where(Invoice.org_id == org_id)) == 1
        assert db.scalar(select(func.count(Job.id)).where(Job.org_id == org_id)) == 1
        assert db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.org_id == org_id, OutboxEvent.kind == "PROCESS_INVOICE"
            )
        ) == 1
        assert db.scalar(
            select(func.count(IdempotencyRequest.id)).where(
                IdempotencyRequest.org_id == org_id,
                IdempotencyRequest.route_key == "POST /documents",
                IdempotencyRequest.key.in_(keys),
            )
        ) == 2


def test_concurrent_version_edits_serialize_against_expected_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email, slug, org_id = setup_tenant("reviewer")
    token = login(email, slug)
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("valid.pdf", sample("invoice_valid_native.pdf"), "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    item = upload.json()
    process_invoice_job(item["job_id"])
    invoice_id = uuid.UUID(item["invoice_id"])
    detail = client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    base_version_id = uuid.UUID(detail["version"]["id"])
    barrier = Barrier(2)
    original_edit = api_main.create_invoice_version

    def synchronized_edit(*args, **kwargs):
        barrier.wait(timeout=15)
        return original_edit(*args, **kwargs)

    monkeypatch.setattr(api_main, "create_invoice_version", synchronized_edit)
    requests = []
    for suffix in ("A", "B"):
        draft = dict(detail["version"]["data"])
        draft.pop("document_id", None)
        draft["supplier_name"] = f"Manually confirmed supplier {suffix}"
        requests.append(
            {
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
                "json": {
                    "expected_version_id": str(base_version_id),
                    "reason": f"Concurrent edit {suffix}",
                    "manual_source_page": 1,
                    "draft": draft,
                },
            }
        )

    def send_edit(payload):
        with TestClient(app) as request_client:
            return request_client.post(
                f"/api/v1/invoices/{item['invoice_id']}/versions", **payload
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send_edit, requests))

    assert sorted(response.status_code for response in responses) == [201, 409]
    stale = next(response for response in responses if response.status_code == 409)
    assert stale.json()["error"]["code"] == "STALE_REVIEW_VERSION"
    winner = next(response.json() for response in responses if response.status_code == 201)
    with SessionLocal() as db:
        invoice = db.scalar(select(Invoice).where(Invoice.org_id == org_id, Invoice.id == invoice_id))
        versions = db.scalars(
            select(InvoiceVersion)
            .where(InvoiceVersion.org_id == org_id, InvoiceVersion.invoice_id == invoice_id)
            .order_by(InvoiceVersion.version_no)
        ).all()
        assert invoice is not None
        assert invoice.version_counter == 2
        assert len(versions) == 2
        assert invoice.current_version_id == uuid.UUID(winner["version_id"])
        assert versions[1].parent_id == base_version_id
        immutable_version_id = versions[0].id

    with pytest.raises(IntegrityError):
        with SessionLocal.begin() as db:
            immutable_version = db.get(InvoiceVersion, immutable_version_id)
            assert immutable_version is not None
            immutable_version.reason = "attempted in-place mutation"


def test_concurrent_approval_race_creates_one_decision_and_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email, slug, org_id = setup_tenant("reviewer")
    token = login(email, slug)
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("valid.pdf", sample("invoice_valid_native.pdf"), "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    item = upload.json()
    process_invoice_job(item["job_id"])
    invoice_id = uuid.UUID(item["invoice_id"])
    detail = client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    po = next(
        row
        for row in client.get("/api/v1/purchase-orders", headers={"Authorization": f"Bearer {token}"}).json()
        if row["po_number"] == "PO-DEMO-001"
    )
    reconciliation = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/reconciliations",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_version_id": detail["version"]["id"], "po_id": po["id"], "line_mapping": {}},
    )
    assert reconciliation.status_code == 201, reconciliation.text
    assert reconciliation.json()["status"] == "PASS"
    decision = {
        "action": "APPROVE",
        "expected_version_id": detail["version"]["id"],
        "reconciliation_id": reconciliation.json()["reconciliation_id"],
        "expected_snapshot_hash": reconciliation.json()["snapshot_hash"],
        "acknowledged_warning_codes": [],
        "note": "Concurrent approval test",
    }
    barrier = Barrier(2)
    original_decision = api_main.create_decision

    def synchronized_decision(*args, **kwargs):
        barrier.wait(timeout=15)
        return original_decision(*args, **kwargs)

    monkeypatch.setattr(api_main, "create_decision", synchronized_decision)

    def send_decision(_: int):
        with TestClient(app) as request_client:
            return request_client.post(
                f"/api/v1/invoices/{item['invoice_id']}/decisions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
                json=decision,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send_decision, range(2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count(ReviewDecision.id)).where(
                ReviewDecision.org_id == org_id,
                ReviewDecision.invoice_id == invoice_id,
                ReviewDecision.action == "APPROVE",
            )
        ) == 1
        assert db.scalar(
            select(func.count(ApprovalRecord.id)).where(
                ApprovalRecord.org_id == org_id,
                ApprovalRecord.invoice_id == invoice_id,
            )
        ) == 1
        assert db.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.org_id == org_id,
                OutboxEvent.job_id == uuid.UUID(item["job_id"]),
                OutboxEvent.kind == "RESUME_INVOICE_GRAPH",
            )
        ) == 1


def test_edit_and_approval_race_cannot_commit_against_different_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email, slug, org_id = setup_tenant("reviewer")
    token = login(email, slug)
    upload = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("valid.pdf", sample("invoice_valid_native.pdf"), "application/pdf")},
        data={"locale": "vi-VN"},
    )
    assert upload.status_code == 202, upload.text
    item = upload.json()
    process_invoice_job(item["job_id"])
    invoice_id = uuid.UUID(item["invoice_id"])
    detail = client.get(
        f"/api/v1/invoices/{item['invoice_id']}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    base_version_id = uuid.UUID(detail["version"]["id"])
    po = next(
        row
        for row in client.get("/api/v1/purchase-orders", headers={"Authorization": f"Bearer {token}"}).json()
        if row["po_number"] == "PO-DEMO-001"
    )
    reconciliation = client.post(
        f"/api/v1/invoices/{item['invoice_id']}/reconciliations",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_version_id": str(base_version_id), "po_id": po["id"], "line_mapping": {}},
    )
    assert reconciliation.status_code == 201, reconciliation.text
    assert reconciliation.json()["status"] == "PASS"

    draft = dict(detail["version"]["data"])
    draft.pop("document_id", None)
    draft["supplier_name"] = "Concurrent approval race supplier edit"
    edit_request = {
        "headers": {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
        "json": {
            "expected_version_id": str(base_version_id),
            "reason": "Edit racing a final approval",
            "manual_source_page": 1,
            "draft": draft,
        },
    }
    decision_request = {
        "headers": {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
        "json": {
            "action": "APPROVE",
            "expected_version_id": str(base_version_id),
            "reconciliation_id": reconciliation.json()["reconciliation_id"],
            "expected_snapshot_hash": reconciliation.json()["snapshot_hash"],
            "acknowledged_warning_codes": [],
            "note": "Approval racing a version edit",
        },
    }
    barrier = Barrier(2)
    original_edit = api_main.create_invoice_version
    original_decision = api_main.create_decision

    def synchronized_edit(*args, **kwargs):
        barrier.wait(timeout=15)
        return original_edit(*args, **kwargs)

    def synchronized_decision(*args, **kwargs):
        barrier.wait(timeout=15)
        return original_decision(*args, **kwargs)

    monkeypatch.setattr(api_main, "create_invoice_version", synchronized_edit)
    monkeypatch.setattr(api_main, "create_decision", synchronized_decision)

    def send_edit():
        with TestClient(app) as request_client:
            return request_client.post(
                f"/api/v1/invoices/{item['invoice_id']}/versions", **edit_request
            )

    def send_decision():
        with TestClient(app) as request_client:
            return request_client.post(
                f"/api/v1/invoices/{item['invoice_id']}/decisions", **decision_request
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        edit_future = pool.submit(send_edit)
        decision_future = pool.submit(send_decision)
        edit_response = edit_future.result()
        decision_response = decision_future.result()

    assert sorted((edit_response.status_code, decision_response.status_code)) == [201, 409]
    with SessionLocal() as db:
        invoice = db.scalar(select(Invoice).where(Invoice.org_id == org_id, Invoice.id == invoice_id))
        assert invoice is not None
        approval_count = db.scalar(
            select(func.count(ApprovalRecord.id)).where(
                ApprovalRecord.org_id == org_id, ApprovalRecord.invoice_id == invoice_id
            )
        )
        decision_count = db.scalar(
            select(func.count(ReviewDecision.id)).where(
                ReviewDecision.org_id == org_id,
                ReviewDecision.invoice_id == invoice_id,
                ReviewDecision.action == "APPROVE",
            )
        )
        if edit_response.status_code == 201:
            assert decision_response.json()["error"]["code"] == "STALE_REVIEW_VERSION"
            assert invoice.version_counter == 2
            assert invoice.workflow_status != "APPROVED"
            assert approval_count == decision_count == 0
        else:
            assert edit_response.json()["error"]["code"] == "INVOICE_TERMINAL"
            assert invoice.version_counter == 1
            assert invoice.workflow_status == "APPROVED"
            assert approval_count == decision_count == 1


def test_failed_login_attempts_are_rate_limited_by_shared_redis() -> None:
    email = f"rate-limit-{uuid.uuid4().hex}@example.test"
    payload = {"email": email, "password": "wrong-password", "org_slug": "missing-org"}
    try:
        for _ in range(LOGIN_LIMIT):
            response = client.post("/api/v1/auth/login", json=payload)
            assert response.status_code == 401
        blocked = client.post("/api/v1/auth/login", json=payload)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    finally:
        clear_login_attempts("testclient", email)


def test_demo_fixture_endpoints_are_hidden_in_production(monkeypatch) -> None:
    email, slug, _org_id = setup_tenant("operator")
    token = login(email, slug)
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(api_main.settings, "app_env", "production")

    assert client.get("/api/v1/demo/fixtures", headers=headers).status_code == 404
    assert (
        client.post(
            "/api/v1/demo/fixtures/invoice_valid_native.pdf/upload",
            headers=headers,
        ).status_code
        == 404
    )
