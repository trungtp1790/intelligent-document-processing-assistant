"""Require a missing-provenance marker for new evidence without a source page.

Revision ID: 0006_page_provenance
Revises: 0005_provenance
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_page_provenance"
down_revision = "0005_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("extraction_evidence")}
    if "provenance_state" not in columns:
        op.add_column(
            "extraction_evidence",
            sa.Column(
                "provenance_state",
                sa.String(length=32),
                sa.Computed(
                    "CASE WHEN page_no IS NULL THEN 'MISSING_PROVENANCE' ELSE verification_state END",
                    persisted=True,
                ),
                nullable=False,
            ),
        )
    constraints = {
        constraint.get("name"): constraint.get("sqltext", "")
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("extraction_evidence")
    }
    name = "ck_extraction_evidence_evidence_page_provenance_state"
    definition = constraints.get(name, "").upper()
    strict_constraint_exists = (
        "PAGE_NO IS NOT NULL" in definition
        and "VERIFICATION_STATE" in definition
        and "MISSING_PROVENANCE" in definition
        and "UNVERIFIED" not in definition
    )
    if not strict_constraint_exists:
        if name in constraints:
            op.drop_constraint(op.f(name), "extraction_evidence", type_="check")
        # NOT VALID preserves existing append-only rows. PostgreSQL still
        # enforces the check for every new evidence row.
        op.execute(
            sa.text(
                "ALTER TABLE extraction_evidence "
                f"ADD CONSTRAINT {name} "
                "CHECK (page_no IS NOT NULL OR verification_state = 'MISSING_PROVENANCE') NOT VALID"
            )
        )


def downgrade() -> None:
    constraints = {
        constraint.get("name")
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("extraction_evidence")
    }
    name = "ck_extraction_evidence_evidence_page_provenance_state"
    if name in constraints:
        op.drop_constraint(op.f(name), "extraction_evidence", type_="check")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("extraction_evidence")}
    if "provenance_state" in columns:
        op.drop_column("extraction_evidence", "provenance_state")
