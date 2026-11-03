from __future__ import annotations

import hashlib
import re
from pathlib import PurePath
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Document, DocumentPage, ExtractionRun, Invoice, Job, OutboxEvent
from app.documents.storage import get_private_storage
from app.documents.validation import ValidatedDocument, validate_document
from app.domain.errors import DomainError
from app.providers.fixture import FixtureProvider
from app.security import Principal
from app.services.common import IdempotentReplay, audit, begin_idempotent, finish_idempotent


def _safe_filename(name: str | None) -> str:
    value = PurePath((name or "invoice").replace("\\", "/")).name
    value = re.sub(r"[^\w.() -]", "_", value, flags=re.UNICODE).strip(" .")
    return (value or "invoice")[:255]


def upload_invoice(
    db: Session, actor: Principal, *, filename: str | None, content: bytes, locale: str, key: str | None
) -> dict[str, Any]:
    validated: ValidatedDocument = validate_document(content)
    digest = hashlib.sha256(content).hexdigest()
    if settings.extraction_provider == "fixture" and FixtureProvider().find_fixture(digest) is None:
        raise DomainError(
            "FIXTURE_NOT_FOUND",
            "Fixture demo only accepts files from the labeled sample library. "
            "Choose a bundled sample or configure a live extraction provider.",
            422,
        )
    request = {"sha256": digest, "filename": _safe_filename(filename), "locale": locale}
    claim = begin_idempotent(db, actor, "POST /documents", key, request)
    if isinstance(claim, IdempotentReplay):
        return claim.response

    existing = db.scalar(select(Document).where(Document.org_id == actor.org_id, Document.sha256 == digest))
    if existing:
        invoice = db.scalar(
            select(Invoice).where(Invoice.org_id == actor.org_id, Invoice.document_id == existing.id)
        )
        if invoice is None:
            raise DomainError("DOCUMENT_INCOMPLETE", "Hồ sơ trùng đang được khôi phục; hãy thử lại.", 409)
        response = {
            "document_id": str(existing.id),
            "invoice_id": str(invoice.id),
            "job_id": None,
            "status": invoice.workflow_status,
            "duplicate": True,
            "fixture_mode": _invoice_uses_fixture(db, actor.org_id, existing.id),
        }
        finish_idempotent(db, claim, invoice.id, response)
        db.commit()
        return response

    storage = get_private_storage()
    storage_key = storage.put(content)
    try:
        document = Document(
            org_id=actor.org_id,
            storage_key=storage_key,
            sha256=digest,
            mime=validated.mime,
            bytes=len(content),
            pages=validated.page_count,
            original_name=_safe_filename(filename),
            uploader_id=actor.user_id,
        )
        db.add(document)
        db.flush()
        db.add_all(
            [
                DocumentPage(
                    org_id=actor.org_id, document_id=document.id, page_no=page_no, page_hash=page_hash
                )
                for page_no, page_hash in enumerate(validated.page_hashes, start=1)
            ]
        )
        invoice = Invoice(
            org_id=actor.org_id, document_id=document.id, workflow_status="UPLOADED", version_counter=0
        )
        db.add(invoice)
        db.flush()
        extraction = ExtractionRun(
            org_id=actor.org_id,
            document_id=document.id,
            provider=settings.extraction_provider,
            prompt_version=settings.extraction_prompt_version,
            schema_version="1.0",
            usage={"locale": locale},
            status="PENDING",
        )
        job = Job(
            org_id=actor.org_id,
            kind="EXTRACT_INVOICE",
            entity_id=invoice.id,
            status="PENDING",
            current_step="QUEUED",
            requested_by=actor.user_id,
        )
        db.add_all([extraction, job])
        db.flush()
        db.add(
            OutboxEvent(
                org_id=actor.org_id,
                kind="PROCESS_INVOICE",
                job_id=job.id,
                payload_ref={"job_id": str(job.id)},
                status="PENDING",
            )
        )
        response = {
            "document_id": str(document.id),
            "invoice_id": str(invoice.id),
            "job_id": str(job.id),
            "status": "UPLOADED",
            "duplicate": False,
            "fixture_mode": settings.extraction_provider == "fixture",
        }
        finish_idempotent(db, claim, invoice.id, response)
        audit(
            db,
            actor,
            actor.org_id,
            "document",
            document.id,
            "DOCUMENT_UPLOADED",
            metadata={
                "sha256": digest,
                "mime": validated.mime,
                "bytes": len(content),
                "pages": validated.page_count,
            },
        )
        db.commit()
        return response
    except IntegrityError:
        db.rollback()
        storage.delete(storage_key)
        duplicate_document = db.scalar(
            select(Document).where(Document.org_id == actor.org_id, Document.sha256 == digest)
        )
        duplicate_invoice = (
            db.scalar(
                select(Invoice).where(
                    Invoice.org_id == actor.org_id,
                    Invoice.document_id == duplicate_document.id,
                )
            )
            if duplicate_document
            else None
        )
        if duplicate_document and duplicate_invoice:
            replay = begin_idempotent(db, actor, "POST /documents", key, request)
            if isinstance(replay, IdempotentReplay):
                db.commit()
                return replay.response
            response = {
                "document_id": str(duplicate_document.id),
                "invoice_id": str(duplicate_invoice.id),
                "job_id": None,
                "status": duplicate_invoice.workflow_status,
                "duplicate": True,
                "fixture_mode": _invoice_uses_fixture(db, actor.org_id, duplicate_document.id),
            }
            finish_idempotent(db, replay, duplicate_invoice.id, response)
            db.commit()
            return response
        raise
    except Exception:
        db.rollback()
        storage.delete(storage_key)
        raise


def _invoice_uses_fixture(db: Session, org_id: Any, document_id: Any) -> bool:
    run = db.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.org_id == org_id, ExtractionRun.document_id == document_id)
        .order_by(ExtractionRun.started_at.desc().nullslast())
    )
    return bool(run and run.provider == "fixture")
