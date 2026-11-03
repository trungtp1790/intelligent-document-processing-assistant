from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.db.models import Invoice, Job, OutboxEvent
from app.db.session import SessionLocal
from app.jobs.tasks import process_invoice_task, resume_invoice_graph_task

HEARTBEAT_PATH = Path(os.getenv("DISPATCHER_HEARTBEAT_PATH", "/tmp/idp-dispatcher-heartbeat"))


def dispatch_once(limit: int = 50) -> int:
    sent = 0
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.status == "PENDING", OutboxEvent.next_attempt_at <= datetime.now(UTC))
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for event in rows:
            try:
                if event.kind == "PROCESS_INVOICE":
                    process_invoice_task.delay(str(event.job_id))
                elif event.kind == "RESUME_INVOICE_GRAPH":
                    decision_id = event.payload_ref.get("decision_id")
                    if not decision_id:
                        event.status = "FAILED"
                        event.attempts += 1
                        continue
                    resume_invoice_graph_task.delay(str(event.job_id), decision_id)
                else:
                    event.status = "FAILED"
                    event.attempts += 1
                    continue
                event.status = "SENT"
                event.attempts += 1
                sent += 1
            except Exception:
                event.attempts += 1
                event.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 2 ** min(event.attempts, 8))
                )
        db.commit()
        return sent
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def recover_expired_leases() -> int:
    now = datetime.now(UTC)
    recovered = 0
    with SessionLocal.begin() as db:
        jobs = db.scalars(
            select(Job)
            .where(Job.status == "PROCESSING", Job.lease_until < now)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            if job.attempts >= 3:
                invoice = db.scalar(
                    select(Invoice)
                    .where(Invoice.org_id == job.org_id, Invoice.id == job.entity_id)
                    .with_for_update()
                )
                job.status = "FAILED"
                job.current_step = "FAILED"
                job.error_code = "LEASE_RECOVERY_RETRY_LIMIT"
                job.lease_until = None
                if invoice and invoice.workflow_status == "PROCESSING":
                    invoice.workflow_status = "PROCESSING_FAILED"
                continue
            pending = db.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.org_id == job.org_id,
                    OutboxEvent.job_id == job.id,
                    OutboxEvent.status == "PENDING",
                )
            )
            if pending is None:
                job.status = "WAITING_RETRY"
                job.current_step = "RECOVERING"
                job.lease_until = None
                db.add(
                    OutboxEvent(
                        org_id=job.org_id,
                        kind="PROCESS_INVOICE",
                        job_id=job.id,
                        payload_ref={"job_id": str(job.id)},
                        status="PENDING",
                    )
                )
                recovered += 1
    return recovered


def main() -> None:
    while True:
        try:
            dispatch_once()
            recover_expired_leases()
            HEARTBEAT_PATH.touch()
        except Exception:
            # The next bounded poll retries the durable outbox/recovery scan.
            pass
        time.sleep(1)


if __name__ == "__main__":
    main()
