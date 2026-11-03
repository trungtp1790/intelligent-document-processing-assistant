from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from app.db.models import (
    Document,
    ExtractionRun,
    Invoice,
    InvoiceVersion,
    McpServiceToken,
    Membership,
    Organization,
    PurchaseOrder,
    PurchaseOrderLine,
    ReconciliationRun,
    Supplier,
    User,
)
from app.db.session import SessionLocal
from app.main import app
from app.mcp.server import mcp_server
from app.mcp.tokens import MCP_TOKEN_SCOPES, hash_mcp_token, issue_mcp_token
from app.security import hash_password

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="requires a migrated PostgreSQL test database"
)


def _tenant_with_records(label: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str]:
    suffix = uuid.uuid4().hex
    password_hash = hash_password("MCP-integration-only-9v2!")
    payload = {
        "schema_version": "1.0",
        "invoice_number": f"MCP-{label}-{suffix[:6]}",
        "invoice_series": "DEMO26",
        "invoice_date": "2026-10-01",
        "supplier_tax_id": "DEMO-SUP-001",
        "supplier_name": "Synthetic MCP supplier",
        "po_number": f"PO-MCP-{suffix[:8]}",
        "currency": "VND",
        "subtotal": "100.000000",
        "tax_total": "10.000000",
        "grand_total": "110.000000",
        "discount_total": "0.000000",
        "shipping_total": "0.000000",
        "lines": [
            {
                "line_id": str(uuid.uuid4()),
                "sku": "SAMPLE-001",
                "description": "Synthetic line",
                "unit": "piece",
                "quantity": "1.000000",
                "unit_price": "100.000000",
                "net_amount": "100.000000",
            }
        ],
    }
    org_slug = f"mcp-{label}-{suffix[:8]}"
    with SessionLocal.begin() as db:
        organization = Organization(slug=org_slug, name=f"MCP {label}")
        user = User(email=f"mcp-{label}-{suffix}@test.local", password_hash=password_hash, active=True)
        db.add_all([organization, user])
        db.flush()
        db.add(Membership(org_id=organization.id, user_id=user.id, role="reviewer"))
        supplier = Supplier(
            org_id=organization.id,
            tax_id_normalized="DEMO-SUP-001",
            name="Synthetic supplier",
        )
        db.add(supplier)
        db.flush()
        po = PurchaseOrder(
            org_id=organization.id,
            supplier_id=supplier.id,
            po_number=payload["po_number"],
            currency="VND",
            state="OPEN",
        )
        db.add(po)
        db.flush()
        db.add(
            PurchaseOrderLine(
                org_id=organization.id,
                po_id=po.id,
                line_no=1,
                sku="SAMPLE-001",
                description="Synthetic line",
                unit="piece",
                quantity=Decimal("1.000000"),
                unit_price=Decimal("100.000000"),
            )
        )
        document = Document(
            org_id=organization.id,
            storage_key=f"mcp-integration/{suffix}.pdf",
            sha256=hashlib.sha256(suffix.encode()).hexdigest(),
            mime="application/pdf",
            bytes=1,
            pages=1,
            original_name="synthetic-mcp-test.pdf",
            uploader_id=user.id,
        )
        db.add(document)
        db.flush()
        invoice = Invoice(
            org_id=organization.id,
            document_id=document.id,
            workflow_status="REVIEW_REQUIRED",
            version_counter=1,
        )
        db.add(invoice)
        db.flush()
        version = InvoiceVersion(
            org_id=organization.id,
            invoice_id=invoice.id,
            version_no=1,
            canonical_json=payload,
            schema_version="1.0",
            snapshot_hash=hashlib.sha256(f"{suffix}-snapshot".encode()).hexdigest(),
            origin="fixture",
            creator_id=user.id,
        )
        db.add(version)
        db.flush()
        invoice.current_version_id = version.id
        reconciliation = ReconciliationRun(
            org_id=organization.id,
            invoice_id=invoice.id,
            invoice_version_id=version.id,
            po_id=po.id,
            mapping_json={},
            mapping_provenance={},
            policy_version="reconciliation-v1",
            result_json={
                "status": "BLOCKED",
                "issues": [
                    {
                        "code": "SAMPLE_REVIEW",
                        "severity": "WARN",
                        "field_path": "grand_total",
                        "detail": "raw evidence sentinel must not be exposed",
                    }
                ],
            },
            status="BLOCKED",
            snapshot_hash=hashlib.sha256(f"{suffix}-reconciliation".encode()).hexdigest(),
        )
        db.add(reconciliation)
        db.flush()
        invoice.current_reconciliation_id = reconciliation.id
        now = datetime.now(UTC)
        db.add(
            ExtractionRun(
                org_id=organization.id,
                document_id=document.id,
                provider="fixture",
                schema_version="1.0",
                status="SUCCEEDED",
                started_at=now,
                completed_at=now,
            )
        )
        return organization.id, user.id, invoice.id, po.id, org_slug


def _issue_token(
    org_id: uuid.UUID, scopes: list[str] | None = None
) -> tuple[str, uuid.UUID]:
    with SessionLocal() as db:
        token, record = issue_mcp_token(
            db,
            org_id=org_id,
            name="integration read only",
            scopes=scopes or sorted(MCP_TOKEN_SCOPES),
        )
        return token, record.id


def test_official_mcp_client_read_tools_auth_and_tenant_isolation() -> None:
    org_a, _, invoice_a, po_a, _ = _tenant_with_records("a")
    org_b, _, invoice_b, po_b, _ = _tenant_with_records("b")
    token_a, token_id = _issue_token(org_a)

    with SessionLocal() as db:
        record = db.scalar(select(McpServiceToken).where(McpServiceToken.id == token_id))
        assert record is not None
        assert record.token_hash == hash_mcp_token(token_a)
        assert token_a not in record.token_hash

    async def exercise_official_client() -> None:
        async with mcp_server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"Authorization": f"Bearer {token_a}"},
            ) as http_client:
                transport = streamable_http_client("http://testserver/api/v1/mcp", http_client=http_client)
                async with Client(transport) as client:
                    listed_tools = await client.list_tools()
                    tool_names = {tool.name for tool in listed_tools.tools}
                    assert tool_names == {
                        "list_invoices",
                        "get_invoice_summary",
                        "get_reconciliation",
                        "get_purchase_order",
                    }
                    assert not any(name in tool_names for name in ("approve", "reject", "payment"))

                    listing = await client.call_tool("list_invoices", {"limit": 10})
                    assert not listing.is_error
                    listed_invoices = listing.structured_content["result"]
                    assert len(listed_invoices) == 1
                    assert listed_invoices[0]["id"] == str(invoice_a)
                    assert listed_invoices[0]["fixture_mode"] is True

                    summary = await client.call_tool(
                        "get_invoice_summary", {"invoice_id": str(invoice_a)}
                    )
                    assert not summary.is_error
                    summary_data = summary.structured_content
                    assert summary_data["invoice_number"].startswith("MCP-a-")
                    assert summary_data["data_is_untrusted"] is True
                    assert "raw_text" not in str(summary_data)
                    assert "content_url" not in summary_data
                    assert len(summary_data["lines"]) == 1

                    reconciliation = await client.call_tool(
                        "get_reconciliation", {"invoice_id": str(invoice_a)}
                    )
                    assert not reconciliation.is_error
                    reconciliation_data = reconciliation.structured_content
                    assert reconciliation_data["status"] == "BLOCKED"
                    assert reconciliation_data["issues"] == [
                        {
                            "code": "SAMPLE_REVIEW",
                            "severity": "WARN",
                            "field_path": "grand_total",
                        }
                    ]
                    assert "raw evidence sentinel" not in str(reconciliation_data)

                    po_result = await client.call_tool(
                        "get_purchase_order", {"purchase_order_id": str(po_a)}
                    )
                    assert not po_result.is_error
                    assert po_result.structured_content["po_number"].startswith("PO-MCP-")

                    foreign_invoice = await client.call_tool(
                        "get_invoice_summary", {"invoice_id": str(invoice_b)}
                    )
                    assert foreign_invoice.is_error
                    assert str(invoice_b) not in str(foreign_invoice)

                    foreign_po = await client.call_tool(
                        "get_purchase_order", {"purchase_order_id": str(po_b)}
                    )
                    assert foreign_po.is_error
                    assert str(po_b) not in str(foreign_po)

            limited_token, _ = _issue_token(org_a, ["invoices:list"])
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"Authorization": f"Bearer {limited_token}"},
            ) as limited_http_client:
                limited_transport = streamable_http_client(
                    "http://testserver/api/v1/mcp", http_client=limited_http_client
                )
                async with Client(limited_transport) as limited_client:
                    denied = await limited_client.call_tool(
                        "get_purchase_order", {"purchase_order_id": str(po_a)}
                    )
                    assert denied.is_error

    asyncio.run(exercise_official_client())

    async def invalid_token_request() -> int:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
        ) as http_client:
            response = await http_client.post(
                "/api/v1/mcp", headers={"Authorization": "Bearer idpmcp_invalid"}, json={}
            )
            return response.status_code

    assert asyncio.run(invalid_token_request()) == 401
