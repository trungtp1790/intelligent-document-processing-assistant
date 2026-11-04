from __future__ import annotations

import io
import random
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from PIL import Image
from pydantic import ValidationError
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    Document,
    ExtractionEvidence,
    ExtractionRun,
    Invoice,
    InvoiceVersion,
    Job,
    OutboxEvent,
    ProviderCall,
)
from app.db.session import SessionLocal
from app.documents.storage import get_private_storage
from app.domain.errors import DomainError
from app.domain.normalization import normalize_invoice_candidate
from app.domain.snapshots import canonical_hash
from app.providers.errors import ProviderFailure
from app.providers.factory import configured_provider
from app.providers.protocol import (
    ExtractionPage,
    ExtractionRequest,
    ExtractionResult,
    validate_bounding_region,
    validate_confidence,
)
from app.services.common import audit

MAX_RENDERED_PAGE_BYTES = 3_750_000
MAX_RENDERED_PAGE_PIXELS = 25_000_000
MAX_RENDERED_DOCUMENT_BYTES = 15 * 1024 * 1024
MAX_RENDERED_PAGE_EDGE = 8000


@dataclass(frozen=True)
class FieldProvenance:
    raw_value: str | None
    normalized_value: str | None
    page_no: int | None
    raw_text: str | None
    bbox: dict[str, Any] | None
    confidence: float | None
    normalization_issue: str | None
    verification_state: str
JOB_LEASE_SECONDS = 60
JOB_HEARTBEAT_SECONDS = 15


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _pdf_text_pages(content: bytes) -> list[str]:
    try:
        return [(page.extract_text() or "") for page in PdfReader(io.BytesIO(content), strict=True).pages]
    except Exception as exc:
        raise DomainError("INVALID_DOCUMENT", "PDF hỏng hoặc không đọc được.", 422) from exc


def _render_pdf_pages(content: bytes) -> list[ExtractionPage]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise DomainError("VISION_RENDERER_NOT_CONFIGURED", "PDF page renderer is unavailable.", 503) from exc
    pages: list[ExtractionPage] = []
    try:
        pdf = pdfium.PdfDocument(content)
        if len(pdf) > settings.max_document_pages:
            raise DomainError("TOO_MANY_PAGES", "PDF exceeds the configured page limit.", 422)
        for page_no, page in enumerate(pdf, start=1):
            bitmap = page.render(scale=1.5, rev_byteorder=True)
            image = bitmap.to_pil().convert("RGB")
            width, height = image.size
            if width > MAX_RENDERED_PAGE_EDGE or height > MAX_RENDERED_PAGE_EDGE:
                raise DomainError(
                    "RENDERED_PAGE_TOO_LARGE", "Rendered page exceeds the image edge limit.", 422
                )
            if width * height > MAX_RENDERED_PAGE_PIXELS:
                raise DomainError(
                    "RENDERED_PAGE_TOO_LARGE", "Rendered page exceeds the image pixel budget.", 422
                )
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
            rendered = output.getvalue()
            if len(rendered) > MAX_RENDERED_PAGE_BYTES:
                raise DomainError(
                    "RENDERED_PAGE_TOO_LARGE", "Rendered page exceeds the provider image limit.", 422
                )
            pages.append(ExtractionPage(page_no=page_no, image_bytes=rendered, image_format="jpeg"))
            page.close()
        pdf.close()
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError(
            "PDF_RENDER_FAILED", "PDF pages could not be prepared for vision extraction.", 422
        ) from exc
    return pages


def _image_page(content: bytes) -> ExtractionPage:
    try:
        with Image.open(io.BytesIO(content)) as image:
            rgb = image.convert("RGB")
            if rgb.width > MAX_RENDERED_PAGE_EDGE or rgb.height > MAX_RENDERED_PAGE_EDGE:
                raise DomainError("IMAGE_TOO_LARGE", "Image exceeds the provider edge limit.", 422)
            if rgb.width * rgb.height > MAX_RENDERED_PAGE_PIXELS:
                raise DomainError("IMAGE_TOO_LARGE", "Image exceeds the 25 megapixel provider limit.", 422)
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=85, optimize=True)
            rendered = output.getvalue()
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("INVALID_DOCUMENT", "Image could not be prepared for extraction.", 422) from exc
    if len(rendered) > MAX_RENDERED_PAGE_BYTES:
        raise DomainError("RENDERED_PAGE_TOO_LARGE", "Image exceeds the provider image byte limit.", 422)
    return ExtractionPage(page_no=1, image_bytes=rendered, image_format="jpeg")


def _make_request(
    document: Document,
    content: bytes,
    run_id: uuid.UUID,
    locale: str,
    provider: Any,
    deadline_at: datetime,
) -> tuple[ExtractionRequest, dict[int, str]]:
    if document.mime == "application/pdf":
        text_pages = _pdf_text_pages(content)
        normalized_lengths = [len(_normalized_text(page)) for page in text_pages]
        native_text_is_usable = sum(normalized_lengths) >= 100 and sum(
            length >= 20 for length in normalized_lengths
        ) >= max(1, (len(text_pages) + 1) // 2)
        if provider.name == "azure_document_intelligence":
            pages = tuple(ExtractionPage(page_no=index) for index in range(1, len(text_pages) + 1))
            route = "azure_prebuilt_invoice"
        elif provider.name == "fixture" or native_text_is_usable:
            pages = tuple(
                ExtractionPage(page_no=index, text=page or None)
                for index, page in enumerate(text_pages, start=1)
            )
            route = "fixture_manifest" if provider.name == "fixture" else "native_text"
        else:
            pages = tuple(_render_pdf_pages(content))
            if sum(len(page.image_bytes or b"") for page in pages) > MAX_RENDERED_DOCUMENT_BYTES:
                raise DomainError(
                    "RENDERED_DOCUMENT_TOO_LARGE", "Rendered pages exceed the provider document budget.", 422
                )
            route = "vision_low_or_unusable_native_text"
        source_text = {index: page for index, page in enumerate(text_pages, start=1)}
    else:
        image_page = (
            ExtractionPage(page_no=1)
            if provider.name in {"fixture", "azure_document_intelligence"}
            else _image_page(content)
        )
        pages = (image_page,)
        route = (
            "fixture_manifest"
            if provider.name == "fixture"
            else "azure_prebuilt_invoice"
            if provider.name == "azure_document_intelligence"
            else "vision_image_input"
        )
        source_text = {}

    capabilities = provider.capabilities()
    if len(pages) > capabilities.max_pages:
        raise DomainError("PROVIDER_PAGE_LIMIT", "Document exceeds the selected provider page limit.", 422)
    if document.bytes > capabilities.max_bytes:
        raise DomainError("PROVIDER_BYTE_LIMIT", "Document exceeds the selected provider byte limit.", 413)
    if any(page.text for page in pages) and not capabilities.text_input:
        raise DomainError("PROVIDER_TEXT_UNSUPPORTED", "Selected provider cannot accept document text.", 422)
    if any(page.image_bytes for page in pages) and not capabilities.image_input:
        raise DomainError(
            "PROVIDER_IMAGE_UNSUPPORTED", "Selected provider cannot accept document images.", 422
        )
    return (
        ExtractionRequest(
            document_sha256=document.sha256,
            document_id=str(document.id),
            mime=document.mime,
            pages=pages,
            schema_version="1.0",
            locale=locale,
            prompt_version=settings.extraction_prompt_version,
            run_id=str(run_id),
            routing_reason=route,
            deadline_seconds_remaining=max(0.0, (deadline_at - datetime.now(UTC)).total_seconds()),
            document_bytes=content if provider.name == "azure_document_intelligence" else None,
        ),
        source_text,
    )


def _evidence_fields(invoice: dict[str, Any]) -> dict[str, Any]:
    fields = {
        key: value
        for key, value in invoice.items()
        if key not in {"schema_version", "document_id", "lines", "invoice_series_absent_confirmed"}
        and value is not None
        and not isinstance(value, bool)
    }
    for index, line in enumerate(invoice.get("lines", [])):
        fields.update(
            {
                f"lines[{index}].{key}": value
                for key, value in line.items()
                if key != "line_id" and value is not None
            }
        )
    return fields


def reserve_provider_invocation(
    job_id: uuid.UUID,
    fence: int,
    extraction_id: uuid.UUID,
    model_id: str,
    strategy: str,
    provider_name: str = "bedrock",
) -> str:
    if provider_name not in {"bedrock", "foundry", "azure_document_intelligence"}:
        raise ProviderFailure("PROVIDER_NOT_CONFIGURED", "Live provider is not supported.", retryable=False)
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.fencing_token != fence or job.status != "PROCESSING":
            raise ProviderFailure("STALE_WORKER", "Worker lease is no longer current.", retryable=False)
        deadline_at = job.created_at + timedelta(seconds=settings.extraction_job_deadline_seconds)
        if datetime.now(UTC) >= deadline_at:
            raise ProviderFailure(
                "JOB_DEADLINE_EXCEEDED", "Extraction exceeded its configured job deadline.", retryable=False
            )
        run = db.scalar(
            select(ExtractionRun)
            .where(ExtractionRun.org_id == job.org_id, ExtractionRun.id == extraction_id)
            .with_for_update()
        )
        if run is None:
            raise ProviderFailure("EXTRACTION_RUN_NOT_FOUND", "Extraction run is missing.", retryable=False)
        attempts = (
            db.scalar(
                select(func.count(ProviderCall.id)).where(ProviderCall.extraction_run_id == extraction_id)
            )
            or 0
        )
        if attempts >= 3:
            raise ProviderFailure(
                "PROVIDER_CALL_BUDGET_EXCEEDED", "Provider call budget exhausted.", retryable=False
            )
        call = ProviderCall(
            org_id=job.org_id,
            extraction_run_id=extraction_id,
            attempt_no=attempts + 1,
            provider=provider_name,
            model_id=model_id,
            strategy=strategy,
            status="STARTED",
        )
        db.add(call)
        db.flush()
        return str(call.id)


def record_provider_invocation(
    call_id: str,
    *,
    latency_ms: int,
    usage: dict[str, Any] | None,
    failure_code: str | None,
) -> None:
    try:
        call_uuid = uuid.UUID(call_id)
    except ValueError as exc:
        raise ProviderFailure(
            "PROVIDER_CALL_RECORD_INVALID", "Provider call record is invalid.", retryable=False
        ) from exc
    with SessionLocal.begin() as db:
        call = db.scalar(select(ProviderCall).where(ProviderCall.id == call_uuid).with_for_update())
        if call is None:
            raise ProviderFailure(
                "PROVIDER_CALL_RECORD_MISSING", "Provider call record is missing.", retryable=False
            )
        call.status = "FAILED" if failure_code else "SUCCEEDED"
        call.failure_code = failure_code
        call.latency_ms = max(0, latency_ms)
        call.input_tokens = _safe_token_count((usage or {}).get("inputTokens"))
        call.output_tokens = _safe_token_count((usage or {}).get("outputTokens"))
        call.completed_at = datetime.now(UTC)


def _candidate_and_evidence(
    document: Document,
    content: bytes,
    extraction_id: uuid.UUID,
    job_id: uuid.UUID,
    fence: int,
    locale: str,
    deadline_at: datetime,
) -> tuple[dict[str, Any], dict[str, FieldProvenance], ExtractionResult, str]:
    provider = configured_provider()
    request, source_text = _make_request(document, content, extraction_id, locale, provider, deadline_at)

    def reserve_live_invocation(model_id: str, strategy: str) -> str:
        return reserve_provider_invocation(
            job_id, fence, extraction_id, model_id, strategy, provider_name=provider.name
        )

    result = provider.extract_invoice(
        request,
        reserve_invocation=reserve_live_invocation
        if provider.name in {"bedrock", "foundry", "azure_document_intelligence"}
        else None,
        record_invocation=record_provider_invocation
        if provider.name in {"bedrock", "foundry", "azure_document_intelligence"}
        else None,
    )
    try:
        normalized = normalize_invoice_candidate(result.candidate, locale)
    except ValidationError as exc:
        raise DomainError(
            "EXTRACTION_SCHEMA_INVALID", "Provider output does not match the invoice schema.", 422
        ) from exc
    canonical = normalized.data
    # Line identifiers are backend-owned; provider values never become domain IDs.
    for line in canonical["lines"]:
        line["line_id"] = str(uuid.uuid4())
    canonical["schema_version"] = "1.0"
    canonical["document_id"] = str(document.id)

    valid_fields = set(normalized.raw_values) | set(normalized.normalized_values)
    supplied_provenance = (
        set(result.evidence_texts)
        | set(result.evidence_pages)
        | set(result.evidence_bboxes)
        | set(result.evidence_confidences)
    )
    unknown_evidence = supplied_provenance - valid_fields
    if unknown_evidence:
        raise DomainError(
            "EXTRACTION_EVIDENCE_INVALID", "Provider returned evidence for an unknown field.", 422
        )
    evidence: dict[str, FieldProvenance] = {}
    for field_path in sorted(valid_fields):
        quote = result.evidence_texts.get(field_path)
        page_no = result.evidence_pages.get(field_path)
        if page_no is not None and not 1 <= page_no <= document.pages:
            raise DomainError(
                "EXTRACTION_EVIDENCE_INVALID", "Evidence references a page outside the document.", 422
            )
        if quote is not None and (not isinstance(quote, str) or not quote.strip()):
            raise DomainError("EXTRACTION_EVIDENCE_INVALID", "Provider evidence text is invalid.", 422)
        bbox = result.evidence_bboxes.get(field_path)
        confidence = result.evidence_confidences.get(field_path)
        try:
            bbox = validate_bounding_region(bbox)
            confidence = validate_confidence(confidence)
        except ValueError as exc:
            raise DomainError(
                "EXTRACTION_EVIDENCE_INVALID",
                "Provider provenance metadata is invalid.",
                422,
            ) from exc
        page_text = source_text.get(page_no, "") if page_no is not None else ""
        verified = bool(
            quote
            and page_no is not None
            and page_text
            and _normalized_text(quote) in _normalized_text(page_text)
        )
        verification_state = (
            "TEXT_VERIFIED"
            if verified
            else "PAGE_REFERENCED"
            if page_no is not None
            else "MISSING_PROVENANCE"
        )
        evidence[field_path] = FieldProvenance(
            raw_value=normalized.raw_values.get(field_path),
            normalized_value=normalized.normalized_values.get(field_path),
            page_no=page_no,
            raw_text=quote,
            bbox=bbox,
            confidence=confidence,
            normalization_issue=normalized.issues.get(field_path),
            verification_state=verification_state,
        )

    raw_output_ref = result.raw_output_ref or ""
    if result.raw_output is not None:
        raw_key = get_private_storage().put(result.raw_output.encode("utf-8"))
        raw_output_ref = f"private://{raw_key}"
    return canonical, evidence, result, raw_output_ref


def _safe_token_count(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _provider_usage_summary(calls: list[ProviderCall]) -> dict[str, int | None]:
    summary: dict[str, int | None] = {"model_calls": len(calls)}
    for field in ("input_tokens", "output_tokens"):
        values = [getattr(call, field) for call in calls if getattr(call, field) is not None]
        summary[field] = sum(values) if values else None
    return summary


def _set_failure(
    db: Session, job: Job, invoice: Invoice, extraction: ExtractionRun, code: str, *, retryable: bool
) -> bool:
    job.lease_until = None
    job.error_code = code
    extraction.status = "FAILED"
    extraction.failure_code = code
    if retryable and job.attempts < 3:
        job.status = "WAITING_RETRY"
        job.current_step = "RETRY_WAIT"
        invoice.workflow_status = "PROCESSING"
        return True
    job.status = "FAILED"
    job.current_step = "FAILED"
    invoice.workflow_status = "PROCESSING_FAILED"
    return False


def _record_failure(job_id: uuid.UUID, fence: int, exc: DomainError, extraction_id: uuid.UUID) -> None:
    with SessionLocal.begin() as db:
        locked = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if locked is None or locked.status != "PROCESSING" or locked.fencing_token != fence:
            return
        invoice = db.scalar(
            select(Invoice)
            .where(Invoice.org_id == locked.org_id, Invoice.id == locked.entity_id)
            .with_for_update()
        )
        extraction = db.scalar(
            select(ExtractionRun)
            .where(ExtractionRun.org_id == locked.org_id, ExtractionRun.id == extraction_id)
            .with_for_update()
        )
        if invoice is None or extraction is None:
            return
        provider_calls = db.scalars(
            select(ProviderCall)
            .where(
                ProviderCall.org_id == locked.org_id,
                ProviderCall.extraction_run_id == extraction.id,
            )
            .order_by(ProviderCall.attempt_no)
        ).all()
        if provider_calls:
            extraction.usage = {
                **(extraction.usage or {}),
                **_provider_usage_summary(provider_calls),
            }
        retryable = exc.retryable if isinstance(exc, ProviderFailure) else exc.status_code >= 500
        now = datetime.now(UTC)
        deadline_at = locked.created_at + timedelta(seconds=settings.extraction_job_deadline_seconds)
        retry_after = exc.retry_after_seconds if isinstance(exc, ProviderFailure) else None
        base_backoff = (2**locked.attempts) * random.uniform(0.5, 1.5)
        backoff_seconds = max(base_backoff, retry_after or 0)
        retry_at = now + timedelta(seconds=backoff_seconds)
        can_retry = retryable and locked.attempts < 3 and retry_at < deadline_at
        deadline_exhausted = now >= deadline_at or (
            retryable and locked.attempts < 3 and retry_at >= deadline_at
        )
        failure_code = "JOB_DEADLINE_EXCEEDED" if deadline_exhausted else exc.code
        retry = _set_failure(db, locked, invoice, extraction, failure_code, retryable=can_retry)
        if retry:
            db.add(
                OutboxEvent(
                    org_id=locked.org_id,
                    kind="PROCESS_INVOICE",
                    job_id=locked.id,
                    payload_ref={"job_id": str(locked.id)},
                    status="PENDING",
                    next_attempt_at=retry_at,
                )
            )
        audit(
            db,
            None,
            locked.org_id,
            "invoice",
            locked.entity_id,
            "EXTRACTION_FAILED",
            metadata={"error_code": exc.code},
        )


def _heartbeat(job_id: uuid.UUID, fence: int, stop: threading.Event) -> None:
    while not stop.wait(JOB_HEARTBEAT_SECONDS):
        try:
            with SessionLocal.begin() as db:
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                if job is None or job.status != "PROCESSING" or job.fencing_token != fence:
                    return
                job.lease_until = datetime.now(UTC) + timedelta(seconds=JOB_LEASE_SECONDS)
        except Exception:
            # Recovery will retry the job if the database stays unavailable.
            continue


def process_invoice_job(job_id: str) -> None:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        return
    claim = SessionLocal()
    extraction_id: uuid.UUID | None = None
    fence: int | None = None
    try:
        job = claim.scalar(select(Job).where(Job.id == job_uuid).with_for_update())
        if job is None or job.status in {"DONE", "FAILED", "WAITING_REVIEW"}:
            claim.rollback()
            return
        now = datetime.now(UTC)
        if job.status == "PROCESSING" and job.lease_until and job.lease_until > now:
            claim.rollback()
            return
        invoice = claim.scalar(
            select(Invoice).where(Invoice.org_id == job.org_id, Invoice.id == job.entity_id).with_for_update()
        )
        if invoice is None or invoice.workflow_status in {"APPROVED", "REJECTED"}:
            job.status = "FAILED"
            job.error_code = "TERMINAL_INVOICE"
            claim.commit()
            return
        deadline_at = job.created_at + timedelta(seconds=settings.extraction_job_deadline_seconds)
        if now >= deadline_at:
            job.status = "FAILED"
            job.current_step = "FAILED"
            job.error_code = "JOB_DEADLINE_EXCEEDED"
            invoice.workflow_status = "PROCESSING_FAILED"
            audit(
                claim,
                None,
                job.org_id,
                "invoice",
                job.entity_id,
                "EXTRACTION_FAILED",
                metadata={"error_code": job.error_code},
            )
            claim.commit()
            return
        job.status = "PROCESSING"
        job.current_step = "PREPARING_PAGES"
        job.attempts += 1
        job.fencing_token += 1
        fence = job.fencing_token
        job.lease_until = now + timedelta(seconds=JOB_LEASE_SECONDS)
        invoice.workflow_status = "PROCESSING"
        document = claim.scalar(
            select(Document).where(Document.org_id == job.org_id, Document.id == invoice.document_id)
        )
        extraction = claim.scalar(
            select(ExtractionRun)
            .where(
                ExtractionRun.org_id == job.org_id,
                ExtractionRun.document_id == invoice.document_id,
                ExtractionRun.status.in_(["PENDING", "FAILED", "PROCESSING"]),
            )
            .order_by(ExtractionRun.started_at.nullsfirst())
            .with_for_update()
        )
        if extraction is None:
            extraction = ExtractionRun(
                org_id=job.org_id,
                document_id=invoice.document_id,
                provider=settings.extraction_provider,
                status="PENDING",
                prompt_version=settings.extraction_prompt_version,
            )
            claim.add(extraction)
            claim.flush()
        extraction_id = extraction.id
        locale = (extraction.usage or {}).get("locale", "vi-VN")
        extraction.provider = settings.extraction_provider
        extraction.prompt_version = settings.extraction_prompt_version
        extraction.status = "PROCESSING"
        extraction.failure_code = None
        extraction.started_at = now
        claim.commit()

        if document is None:
            raise DomainError("DOCUMENT_NOT_FOUND", "Uploaded file is missing.", 404)
        raw = get_private_storage().read(document.storage_key)
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat, args=(job_uuid, fence, stop_heartbeat), daemon=True, name="idp-job-heartbeat"
        )
        heartbeat.start()
        try:
            canonical, evidence, result, raw_output_ref = _candidate_and_evidence(
                document, raw, extraction_id, job_uuid, fence, locale, deadline_at
            )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=JOB_HEARTBEAT_SECONDS + 1)
        with SessionLocal.begin() as db:
            fresh_job = db.scalar(select(Job).where(Job.id == job_uuid).with_for_update())
            fresh_invoice = db.scalar(
                select(Invoice)
                .where(Invoice.org_id == job.org_id, Invoice.id == job.entity_id)
                .with_for_update()
            )
            if fresh_job is None or fresh_invoice is None or fresh_job.fencing_token != fence:
                return
            if datetime.now(UTC) >= deadline_at:
                raise ProviderFailure(
                    "JOB_DEADLINE_EXCEEDED",
                    "Extraction exceeded its configured job deadline.",
                    retryable=False,
                )
            if fresh_invoice.workflow_status in {"APPROVED", "REJECTED"}:
                fresh_job.status = "FAILED"
                fresh_job.error_code = "TERMINAL_INVOICE"
                return
            version = InvoiceVersion(
                org_id=job.org_id,
                invoice_id=job.entity_id,
                version_no=1,
                canonical_json=canonical,
                schema_version="1.0",
                parent_id=None,
                snapshot_hash=canonical_hash(canonical),
                origin=result.provider,
                creator_id=None,
                reason=f"{result.provider} extraction",
            )
            db.add(version)
            db.flush()
            fresh_invoice.current_version_id = version.id
            fresh_invoice.version_counter = 1
            fresh_invoice.current_reconciliation_id = None
            fresh_invoice.workflow_status = "REVIEW_REQUIRED"
            for field_path, provenance in evidence.items():
                db.add(
                    ExtractionEvidence(
                        org_id=job.org_id,
                        invoice_id=job.entity_id,
                        version_id=version.id,
                        field_path=field_path,
                        page_no=provenance.page_no,
                        raw_value=provenance.raw_value,
                        normalized_value=provenance.normalized_value,
                        raw_text=provenance.raw_text,
                        bbox=provenance.bbox,
                        confidence=provenance.confidence,
                        normalization_issue=provenance.normalization_issue,
                        origin="fixture" if result.provider == "fixture" else "model",
                        verification_state=provenance.verification_state,
                    )
                )
            run = db.scalar(select(ExtractionRun).where(ExtractionRun.id == extraction_id).with_for_update())
            if run:
                run.provider = result.provider
                run.model_id = result.model_id
                run.prompt_version = settings.extraction_prompt_version
                run.schema_version = "1.0"
                run.status = "SUCCEEDED"
                run.completed_at = datetime.now(UTC)
                run.raw_output_ref = raw_output_ref or None
                persisted_usage = {
                    **(run.usage or {}),
                    **result.usage,
                    "strategy": result.strategy,
                    "routing_reason": result.usage.get(
                        "routing_reason",
                        "fixture_manifest" if result.provider == "fixture" else "native_text_or_vision",
                    ),
                }
                if result.provider in {"bedrock", "foundry", "azure_document_intelligence"}:
                    provider_calls = db.scalars(
                        select(ProviderCall)
                        .where(
                            ProviderCall.org_id == job.org_id,
                            ProviderCall.extraction_run_id == extraction_id,
                        )
                        .order_by(ProviderCall.attempt_no)
                    ).all()
                    persisted_usage.update(_provider_usage_summary(provider_calls))
                run.usage = persisted_usage
            fresh_job.status = "WAITING_REVIEW"
            fresh_job.current_step = "REVIEW_REQUIRED"
            fresh_job.lease_until = None
            fresh_job.error_code = None
            audit(
                db,
                None,
                job.org_id,
                "invoice",
                job.entity_id,
                "EXTRACTION_COMPLETED",
                after_hash=version.snapshot_hash,
                metadata={"provider": result.provider, "version_no": 1, "strategy": result.strategy},
            )
    except DomainError as exc:
        claim.rollback()
        if extraction_id is not None and fence is not None:
            _record_failure(job_uuid, fence, exc, extraction_id)
    except Exception:
        claim.rollback()
        if extraction_id is not None and fence is not None:
            _record_failure(
                job_uuid,
                fence,
                DomainError("EXTRACTION_FAILED", "Extraction job failed.", 503),
                extraction_id,
            )
    finally:
        claim.close()
