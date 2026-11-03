"""Create the M0/M1 tenant, invoice, review, and job schema.

Revision ID: 0001_m0_m1_base
Revises:
"""

import sqlalchemy as sa

from alembic import op
from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0001_m0_m1_base"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the original M0/M1 baseline stable as ORM metadata evolves.
    metadata = Base.metadata
    jobs = metadata.tables["jobs"]
    thread_column = jobs.c.workflow_thread_id
    thread_unique = next(
        constraint
        for constraint in jobs.constraints
        if isinstance(constraint, sa.UniqueConstraint)
        and [column.name for column in constraint.columns] == ["workflow_thread_id"]
    )
    job_status = next(constraint for constraint in jobs.constraints if constraint.name == "job_status")
    jobs._columns.remove(thread_column)
    jobs.constraints.remove(thread_unique)
    jobs.constraints.remove(job_status)
    legacy_job_status = sa.CheckConstraint(
        "status IN ('PENDING','PROCESSING','WAITING_RETRY','DONE','FAILED')",
        name=sa.schema.conv("job_status"),
    )
    jobs.append_constraint(legacy_job_status)
    try:
        legacy_tables = [
            table
            for table in metadata.sorted_tables
            if table.name not in {
                "provider_calls",
                "mcp_service_tokens",
                "policy_documents",
                "policy_chunks",
            }
        ]
        metadata.create_all(bind=op.get_bind(), tables=legacy_tables)
    finally:
        jobs.constraints.remove(legacy_job_status)
        jobs.append_column(thread_column)
        jobs.append_constraint(thread_unique)
        jobs.append_constraint(job_status)
    op.execute(
        """
        CREATE FUNCTION idp_reject_immutable_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "invoice_versions",
        "extraction_evidence",
        "reconciliation_runs",
        "review_decisions",
        "approval_records",
        "audit_events",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION idp_reject_immutable_mutation()"
        )


def downgrade() -> None:
    for table in (
        "invoice_versions",
        "extraction_evidence",
        "reconciliation_runs",
        "review_decisions",
        "approval_records",
        "audit_events",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS idp_reject_immutable_mutation()")
    Base.metadata.drop_all(bind=op.get_bind())
