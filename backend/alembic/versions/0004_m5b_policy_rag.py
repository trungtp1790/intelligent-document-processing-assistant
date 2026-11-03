"""Add immutable, organization-scoped policy sources and searchable chunks.

Revision ID: 0004_m5b_policy_rag
Revises: 0003_m5a_mcp_service_tokens
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_m5b_policy_rag"
down_revision = "0003_m5a_mcp_service_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(slug) BETWEEN 1 AND 100", name="policy_document_slug_length"),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 240", name="policy_document_title_length"),
        sa.CheckConstraint("version_no >= 1", name="policy_document_version_positive"),
        sa.CheckConstraint("length(version) BETWEEN 1 AND 32", name="policy_document_version_length"),
        sa.CheckConstraint("length(source_sha256) = 64", name="policy_document_sha256_length"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_policy_documents"),
        sa.UniqueConstraint("org_id", "id", name="uq_policy_documents_org_id"),
        sa.UniqueConstraint(
            "org_id", "slug", "version_no", name="uq_policy_document_org_slug_version"
        ),
    )
    op.create_index("ix_policy_documents_org_id", "policy_documents", ["org_id"])
    op.create_table(
        "policy_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("policy_document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=160), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', search_text)", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name="policy_chunk_ordinal_nonnegative"),
        sa.CheckConstraint("page_no >= 1", name="policy_chunk_page_positive"),
        sa.CheckConstraint("length(section) BETWEEN 1 AND 160", name="policy_chunk_section_length"),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 4000", name="policy_chunk_content_length"),
        sa.CheckConstraint("length(content_sha256) = 64", name="policy_chunk_sha256_length"),
        sa.CheckConstraint("length(search_text) BETWEEN 1 AND 4000", name="policy_chunk_search_text_length"),
        sa.ForeignKeyConstraint(
            ["org_id", "policy_document_id"],
            ["policy_documents.org_id", "policy_documents.id"],
            name="fk_policy_chunk_org_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_policy_chunks"),
        sa.UniqueConstraint("org_id", "id", name="uq_policy_chunks_org_id"),
        sa.UniqueConstraint(
            "org_id", "policy_document_id", "ordinal", name="uq_policy_chunk_document_ordinal"
        ),
    )
    op.create_index("ix_policy_chunks_org_id", "policy_chunks", ["org_id"])
    op.create_index(
        "ix_policy_chunks_search_vector",
        "policy_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.execute(
        "CREATE TRIGGER trg_policy_documents_immutable "
        "BEFORE UPDATE OR DELETE ON policy_documents "
        "FOR EACH ROW EXECUTE FUNCTION idp_reject_immutable_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_policy_chunks_immutable "
        "BEFORE UPDATE OR DELETE ON policy_chunks "
        "FOR EACH ROW EXECUTE FUNCTION idp_reject_immutable_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_policy_chunks_immutable ON policy_chunks")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_documents_immutable ON policy_documents")
    op.drop_index("ix_policy_chunks_search_vector", table_name="policy_chunks")
    op.drop_index("ix_policy_chunks_org_id", table_name="policy_chunks")
    op.drop_table("policy_chunks")
    op.drop_index("ix_policy_documents_org_id", table_name="policy_documents")
    op.drop_table("policy_documents")
