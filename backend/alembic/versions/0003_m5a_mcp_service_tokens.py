"""Add hashed, tenant-scoped read-only MCP service tokens.

Revision ID: 0003_m5a_mcp_service_tokens
Revises: 0002_m2_m4_provider_calls_graph
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_m5a_mcp_service_tokens"
down_revision = "0002_m2_m4_provider_calls_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_service_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_hash) = 64", name="mcp_service_token_hash_length"),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 120", name="mcp_service_token_name_length"
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_mcp_service_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_mcp_service_token_hash"),
    )
    op.create_index("ix_mcp_service_tokens_org_id", "mcp_service_tokens", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_service_tokens_org_id", table_name="mcp_service_tokens")
    op.drop_table("mcp_service_tokens")
