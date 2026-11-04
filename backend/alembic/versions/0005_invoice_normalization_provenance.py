"""Persist normalization issues and optional provider provenance.

Revision ID: 0005_invoice_normalization_provenance
Revises: 0004_m5b_policy_rag
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_provenance"
down_revision = "0004_m5b_policy_rag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("extraction_evidence")}
    constraints = {
        constraint.get("name") for constraint in inspector.get_check_constraints("extraction_evidence")
    }
    page_constraint = "ck_extraction_evidence_evidence_page_positive"
    if page_constraint in constraints:
        op.drop_constraint(op.f(page_constraint), "extraction_evidence", type_="check")
    op.alter_column("extraction_evidence", "page_no", existing_type=sa.Integer(), nullable=True)
    op.create_check_constraint(
        "evidence_page_positive",
        "extraction_evidence",
        "page_no IS NULL OR page_no > 0",
    )
    if "confidence" not in columns:
        op.add_column("extraction_evidence", sa.Column("confidence", sa.Float(), nullable=True))
    if "normalization_issue" not in columns:
        op.add_column(
            "extraction_evidence",
            sa.Column("normalization_issue", sa.String(length=32), nullable=True),
        )

    existing_constraints = {
        constraint.get("name")
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(
            "extraction_evidence"
        )
    }
    if "ck_extraction_evidence_evidence_confidence_range" not in existing_constraints:
        op.create_check_constraint(
            "evidence_confidence_range",
            "extraction_evidence",
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
        )
    if "ck_extraction_evidence_evidence_normalization_issue" not in existing_constraints:
        op.create_check_constraint(
            "evidence_normalization_issue",
            "extraction_evidence",
            "normalization_issue IS NULL OR normalization_issue IN "
            "('AMBIGUOUS_NUMBER','INVALID_NUMBER','NUMERIC_OVERFLOW','AMBIGUOUS_DATE','INVALID_DATE','UNSUPPORTED_LOCALE')",
        )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_extraction_evidence_evidence_normalization_issue"),
        "extraction_evidence",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_extraction_evidence_evidence_confidence_range"),
        "extraction_evidence",
        type_="check",
    )
    op.alter_column("extraction_evidence", "page_no", existing_type=sa.Integer(), nullable=False)
    op.drop_column("extraction_evidence", "normalization_issue")
    op.drop_column("extraction_evidence", "confidence")
    op.drop_constraint(
        op.f("ck_extraction_evidence_evidence_page_positive"),
        "extraction_evidence",
        type_="check",
    )
    op.create_check_constraint(
        "evidence_page_positive", "extraction_evidence", "page_no > 0"
    )
