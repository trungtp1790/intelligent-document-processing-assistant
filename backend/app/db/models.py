from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv

from app.db.base import Base

JSON_VALUE = JSON().with_variant(JSONB, "postgresql")


def now_utc() -> datetime:
    return datetime.now(UTC)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class McpServiceToken(Base):
    __tablename__ = "mcp_service_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_mcp_service_token_hash"),
        CheckConstraint("length(token_hash) = 64", name="mcp_service_token_hash_length"),
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="mcp_service_token_name_length"),
        Index("ix_mcp_service_tokens_org_id", "org_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyDocument(Base):
    __tablename__ = "policy_documents"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_policy_documents_org_id"),
        UniqueConstraint("org_id", "slug", "version_no", name="uq_policy_document_org_slug_version"),
        CheckConstraint("length(slug) BETWEEN 1 AND 100", name="policy_document_slug_length"),
        CheckConstraint("length(title) BETWEEN 1 AND 240", name="policy_document_title_length"),
        CheckConstraint("version_no >= 1", name="policy_document_version_positive"),
        CheckConstraint("length(version) BETWEEN 1 AND 32", name="policy_document_version_length"),
        CheckConstraint("length(source_sha256) = 64", name="policy_document_sha256_length"),
        Index("ix_policy_documents_org_id", "org_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_policy_chunks_org_id"),
        UniqueConstraint("org_id", "policy_document_id", "ordinal", name="uq_policy_chunk_document_ordinal"),
        ForeignKeyConstraint(
            ["org_id", "policy_document_id"],
            ["policy_documents.org_id", "policy_documents.id"],
            name="fk_policy_chunk_org_document",
            ondelete="CASCADE",
        ),
        CheckConstraint("ordinal >= 0", name="policy_chunk_ordinal_nonnegative"),
        CheckConstraint("page_no >= 1", name="policy_chunk_page_positive"),
        CheckConstraint("length(section) BETWEEN 1 AND 160", name="policy_chunk_section_length"),
        CheckConstraint("length(content) BETWEEN 1 AND 4000", name="policy_chunk_content_length"),
        CheckConstraint("length(content_sha256) = 64", name="policy_chunk_sha256_length"),
        Index("ix_policy_chunks_org_id", "org_id"),
        Index("ix_policy_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    policy_document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str] = mapped_column(String(160), nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', search_text)", persisted=True), nullable=False
    )


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "org_id", name="uq_membership_user_org"),
        UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),
        UniqueConstraint("org_id", "user_id", "role", name="uq_membership_org_user_role"),
        CheckConstraint("role IN ('operator', 'reviewer')", name="membership_role"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_supplier_org_id"),
        UniqueConstraint("org_id", "tax_id_normalized", name="uq_supplier_org_tax"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    tax_id_normalized: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_po_org_id"),
        UniqueConstraint("org_id", "po_number", name="uq_po_org_number"),
        ForeignKeyConstraint(
            ["org_id", "supplier_id"],
            ["suppliers.org_id", "suppliers.id"],
            name="fk_po_org_supplier",
            ondelete="RESTRICT",
        ),
        CheckConstraint("state IN ('OPEN', 'CLOSED')", name="po_state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    supplier_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    po_number: Mapped[str] = mapped_column(String(80), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="VND")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_po_line_org_id"),
        UniqueConstraint("org_id", "po_id", "line_no", name="uq_po_line_number"),
        ForeignKeyConstraint(
            ["org_id", "po_id"],
            ["purchase_orders.org_id", "purchase_orders.id"],
            name="fk_po_line_org_po",
            ondelete="CASCADE",
        ),
        CheckConstraint("quantity > 0", name="po_line_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="po_line_unit_price_nonnegative"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    po_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_document_org_id"),
        UniqueConstraint("org_id", "sha256", name="uq_document_org_hash"),
        CheckConstraint("bytes > 0", name="document_bytes_positive"),
        CheckConstraint("pages > 0", name="document_pages_positive"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(String(80), nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    pages: Mapped[int] = mapped_column(Integer, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    uploader_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_no", name="uq_document_page_number"),
        ForeignKeyConstraint(
            ["org_id", "document_id"],
            ["documents.org_id", "documents.id"],
            name="fk_document_page_org_document",
            ondelete="CASCADE",
        ),
        CheckConstraint("page_no > 0", name="document_page_positive"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text_ref: Mapped[str | None] = mapped_column(String(255))
    preview_ref: Mapped[str | None] = mapped_column(String(255))
    page_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_invoice_org_id"),
        UniqueConstraint("org_id", "document_id", name="uq_invoice_org_document"),
        ForeignKeyConstraint(
            ["org_id", "document_id"],
            ["documents.org_id", "documents.id"],
            name="fk_invoice_org_document",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "id", "current_version_id"],
            ["invoice_versions.org_id", "invoice_versions.invoice_id", "invoice_versions.id"],
            name="fk_invoice_current_version_scope",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["org_id", "id", "current_reconciliation_id"],
            ["reconciliation_runs.org_id", "reconciliation_runs.invoice_id", "reconciliation_runs.id"],
            name="fk_invoice_current_reconciliation_scope",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "workflow_status IN ('UPLOADED','PROCESSING','PROCESSING_FAILED','REVIEW_REQUIRED',"
            "'READY_FOR_APPROVAL','APPROVED','REJECTED')",
            name="invoice_workflow_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    workflow_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UPLOADED")
    version_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_reconciliation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class InvoiceVersion(Base):
    __tablename__ = "invoice_versions"
    __table_args__ = (
        UniqueConstraint("invoice_id", "version_no", name="uq_invoice_version_number"),
        UniqueConstraint("org_id", "invoice_id", "id", name="uq_invoice_version_scope"),
        ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["invoices.org_id", "invoices.id"],
            name="fk_invoice_version_org_invoice",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "invoice_id", "parent_id"],
            ["invoice_versions.org_id", "invoice_versions.invoice_id", "invoice_versions.id"],
            name="fk_invoice_version_parent_scope",
        ),
        CheckConstraint("version_no > 0", name="invoice_version_positive"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    creator_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_extraction_run_org_id"),
        ForeignKeyConstraint(
            ["org_id", "document_id"], ["documents.org_id", "documents.id"], name="fk_extraction_org_document"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    raw_output_ref: Mapped[str | None] = mapped_column(String(255))
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    failure_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    __table_args__ = (
        UniqueConstraint("org_id", "extraction_run_id", "attempt_no", name="uq_provider_call_attempt"),
        ForeignKeyConstraint(
            ["org_id", "extraction_run_id"],
            ["extraction_runs.org_id", "extraction_runs.id"],
            name="fk_provider_call_org_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("attempt_no BETWEEN 1 AND 3", name="provider_call_attempt_limit"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="provider_call_latency_nonnegative"),
        CheckConstraint("status IN ('STARTED','SUCCEEDED','FAILED')", name="provider_call_status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(200))
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="STARTED")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExtractionEvidence(Base):
    __tablename__ = "extraction_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "invoice_id", "version_id"],
            ["invoice_versions.org_id", "invoice_versions.invoice_id", "invoice_versions.id"],
            name="fk_evidence_version_scope",
        ),
        CheckConstraint("page_no IS NULL OR page_no > 0", name="evidence_page_positive"),
        CheckConstraint(
            "page_no IS NOT NULL OR verification_state = 'MISSING_PROVENANCE'",
            name="evidence_page_provenance_state",
        ),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1", name="evidence_confidence_range"),
        CheckConstraint(
            "normalization_issue IS NULL OR normalization_issue IN "
            "('AMBIGUOUS_NUMBER','INVALID_NUMBER','NUMERIC_OVERFLOW','AMBIGUOUS_DATE','INVALID_DATE','UNSUPPORTED_LOCALE')",
            name="evidence_normalization_issue",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    page_no: Mapped[int | None] = mapped_column(Integer)
    raw_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    confidence: Mapped[float | None] = mapped_column(Float)
    normalization_issue: Mapped[str | None] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    verification_state: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance_state: Mapped[str] = mapped_column(
        String(32),
        Computed(
            "CASE WHEN page_no IS NULL THEN 'MISSING_PROVENANCE' ELSE verification_state END",
            persisted=True,
        ),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        UniqueConstraint("org_id", "invoice_id", "id", name="uq_reconciliation_scope"),
        ForeignKeyConstraint(
            ["org_id", "invoice_id", "invoice_version_id"],
            ["invoice_versions.org_id", "invoice_versions.invoice_id", "invoice_versions.id"],
            name="fk_reconciliation_invoice_version",
        ),
        ForeignKeyConstraint(
            ["org_id", "po_id"],
            ["purchase_orders.org_id", "purchase_orders.id"],
            name="fk_reconciliation_org_po",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    invoice_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    po_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    mapping_json: Mapped[dict[str, str]] = mapped_column(JSON_VALUE, nullable=False)
    mapping_provenance: Mapped[dict[str, dict[str, str]]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="reconciliation-v1")
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_review_decision_org_id"),
        UniqueConstraint("invoice_id", "version_id", name="uq_review_decision_invoice_version"),
        ForeignKeyConstraint(
            ["org_id", "invoice_id", "version_id"],
            ["invoice_versions.org_id", "invoice_versions.invoice_id", "invoice_versions.id"],
            name="fk_review_decision_version_scope",
        ),
        ForeignKeyConstraint(
            ["org_id", "invoice_id", "reconciliation_id"],
            ["reconciliation_runs.org_id", "reconciliation_runs.invoice_id", "reconciliation_runs.id"],
            name="fk_review_decision_reconciliation_scope",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    note: Mapped[str | None] = mapped_column(String(1000))
    acknowledged_warnings: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False, default=list)
    expected_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ApprovalRecord(Base):
    __tablename__ = "approval_records"
    __table_args__ = (
        UniqueConstraint("org_id", "invoice_id", name="uq_approval_org_invoice"),
        UniqueConstraint("org_id", "business_key", name="uq_approval_org_business_key"),
        UniqueConstraint("org_id", "po_id", name="uq_approval_org_po"),
        ForeignKeyConstraint(
            ["org_id", "invoice_id", "version_id"],
            ["invoice_versions.org_id", "invoice_versions.invoice_id", "invoice_versions.id"],
            name="fk_approval_version_scope",
        ),
        ForeignKeyConstraint(
            ["org_id", "po_id"], ["purchase_orders.org_id", "purchase_orders.id"], name="fk_approval_po_scope"
        ),
        ForeignKeyConstraint(
            ["org_id", "supplier_id"], ["suppliers.org_id", "suppliers.id"], name="fk_approval_supplier_scope"
        ),
        ForeignKeyConstraint(
            ["org_id", "decision_id"],
            ["review_decisions.org_id", "review_decisions.id"],
            name="fk_approval_decision_scope",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    po_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    supplier_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    business_key: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_VALUE, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_job_org_id"),
        UniqueConstraint("workflow_thread_id", name="uq_job_workflow_thread_id"),
        ForeignKeyConstraint(
            ["org_id", "entity_id"], ["invoices.org_id", "invoices.id"], name="fk_job_org_invoice"
        ),
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','WAITING_RETRY','WAITING_REVIEW','DONE','FAILED')",
            name=conv("job_status"),
        ),
        CheckConstraint("attempts >= 0", name="job_attempts_nonnegative"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_thread_id: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4, nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    current_step: Mapped[str] = mapped_column(String(40), nullable=False, default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        ForeignKeyConstraint(["org_id", "job_id"], ["jobs.org_id", "jobs.id"], name="fk_outbox_org_job"),
        Index("ix_outbox_dispatch", "status", "next_attempt_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payload_ref: Mapped[dict[str, str]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class IdempotencyRequest(Base):
    __tablename__ = "idempotency_requests"
    __table_args__ = (
        UniqueConstraint("org_id", "actor_id", "route_key", "key", name="uq_idempotency_scope"),
        ForeignKeyConstraint(
            ["org_id", "actor_id"],
            ["memberships.org_id", "memberships.user_id"],
            name="fk_idempotency_membership_scope",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    route_key: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    response_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
