"""Add provider call accounting and durable workflow thread identity.

Revision ID: 0002_m2_m4_provider_calls_graph
Revises: 0001_m0_m1_base
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_m2_m4_provider_calls_graph"
down_revision = "0001_m0_m1_base"
branch_labels = None
depends_on = None


def _job_status_constraint_name() -> str:
    """Return the physical legacy check name across baseline naming-convention variants."""
    checks = sa.inspect(op.get_bind()).get_check_constraints("jobs")
    expected_values = ("'pending'", "'processing'", "'done'", "'failed'")
    matches = [
        check.get("name")
        for check in checks
        if check.get("name")
        and "status" in str(check.get("sqltext", "")).casefold()
        and all(value in str(check.get("sqltext", "")).casefold() for value in expected_values)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Expected exactly one legacy jobs status CHECK constraint; "
            f"found {matches or 'none'} among {[check.get('name') for check in checks]}"
        )
    return matches[0]


def _replace_job_status_constraint(expression: str) -> None:
    name = _job_status_constraint_name()
    op.drop_constraint(op.f(name), "jobs", type_="check")
    op.create_check_constraint(op.f(name), "jobs", expression)


def upgrade() -> None:
    op.add_column("jobs", sa.Column("workflow_thread_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE jobs SET workflow_thread_id = id WHERE workflow_thread_id IS NULL")
    op.alter_column("jobs", "workflow_thread_id", nullable=False)
    op.create_unique_constraint("uq_job_workflow_thread_id", "jobs", ["workflow_thread_id"])
    _replace_job_status_constraint(
        "status IN ('PENDING','PROCESSING','WAITING_RETRY','WAITING_REVIEW','DONE','FAILED')"
    )
    op.create_table(
        "provider_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=True),
        sa.Column("strategy", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_no BETWEEN 1 AND 3", name="provider_call_attempt_limit"),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="provider_call_latency_nonnegative"),
        sa.CheckConstraint("status IN ('STARTED','SUCCEEDED','FAILED')", name="provider_call_status"),
        sa.ForeignKeyConstraint(
            ["org_id", "extraction_run_id"],
            ["extraction_runs.org_id", "extraction_runs.id"],
            name="fk_provider_call_org_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "extraction_run_id", "attempt_no", name="uq_provider_call_attempt"),
    )


def downgrade() -> None:
    op.drop_table("provider_calls")
    _replace_job_status_constraint(
        "status IN ('PENDING','PROCESSING','WAITING_RETRY','DONE','FAILED')"
    )
    op.drop_constraint("uq_job_workflow_thread_id", "jobs", type_="unique")
    op.drop_column("jobs", "workflow_thread_id")
