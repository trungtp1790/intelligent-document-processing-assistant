from __future__ import annotations

import io
import os
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

import jwt
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings, validate_runtime_settings
from app.copilot.service import copilot_status, run_copilot
from app.db.models import Membership, Organization, User
from app.db.session import engine, get_db
from app.domain.errors import DomainError
from app.login_rate_limit import (
    LOGIN_LIMIT,
    check_login_rate_limiter,
    clear_login_attempts,
    register_login_attempt,
)
from app.mcp.auth import McpBearerAuthMiddleware
from app.mcp.server import mcp_asgi_app, mcp_server
from app.rag.service import list_policy_sources, query_policy
from app.schemas import (
    CopilotRequest,
    DecisionCreate,
    LoginRequest,
    POCreate,
    PolicyQueryRequest,
    ReconciliationCreate,
    SupplierCreate,
    VersionCreate,
)
from app.security import Principal, create_access_token, decode_access_token, verify_password
from app.services.purchase_orders import (
    create_purchase_order,
    create_supplier,
    get_purchase_order,
    get_purchase_orders,
    get_suppliers,
    import_purchase_orders_csv,
)
from app.services.review import (
    create_decision,
    create_invoice_version,
    create_reconciliation,
    export_invoice,
    export_invoice_csv,
    get_audit,
    get_file,
    get_invoice_detail,
    get_invoice_list,
    get_job,
    get_versions,
    retry_job,
)
from app.services.uploads import upload_invoice


@asynccontextmanager
async def lifespan(_: FastAPI):
    # MCPServer's Streamable HTTP manager is intentionally one-shot per process.
    # PostgreSQL integration tests enter their own manager context around MCP calls.
    if os.getenv("TEST_DATABASE_URL"):
        yield
        return
    async with mcp_server.session_manager.run():
        yield


validate_runtime_settings()
app = FastAPI(title="Intelligent Document Processing Assistant", version="0.1.0", lifespan=lifespan)
app.add_middleware(McpBearerAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)
bearer = HTTPBearer(auto_error=False)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(_: Request, exc: IntegrityError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "DATABASE_CONFLICT",
                "message": "Dữ liệu xung đột với một thao tác đồng thời. Hãy tải lại.",
                "details": {},
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND"}.get(exc.status_code, "HTTP_ERROR")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    headers = dict(exc.headers or {})
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": message, "details": {}}},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "INVALID_REQUEST",
                "message": "Dữ liệu gửi lên không hợp lệ.",
                "details": {
                    "fields": [
                        {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
                    ]
                },
            }
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, __: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "Đã xảy ra lỗi nội bộ.", "details": {}}},
    )


def current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> Principal:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Cần đăng nhập.", headers={"WWW-Authenticate": "Bearer"})
    try:
        token_principal = decode_access_token(credentials.credentials)
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise HTTPException(
            status_code=401,
            detail="Phiên đăng nhập không hợp lệ hoặc đã hết hạn.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    row = db.execute(
        select(Membership.role, User.active, Organization.slug, Organization.name)
        .join(User, User.id == Membership.user_id)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == token_principal.user_id, Membership.org_id == token_principal.org_id)
    ).first()
    if row is None or not row.active or row.role != token_principal.role:
        raise HTTPException(
            status_code=401,
            detail="Quyền thành viên không còn hiệu lực.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(token_principal.user_id, token_principal.org_id, row.slug, row.name, row.role)


def require_role(*roles: str):
    def dependency(actor: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if actor.role not in roles:
            raise HTTPException(status_code=403, detail="Bạn không có quyền thực hiện thao tác này.")
        return actor

    return dependency


def _api_error(code: str, message: str, status: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "details": details or {}}}
    )


@app.get("/health/live")
def health_live() -> dict:
    return {"status": "alive"}


@app.get("/health/ready")
def health_ready() -> dict:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    try:
        check_login_rate_limiter()
    except RedisError:
        raise HTTPException(status_code=503, detail="Required service unavailable") from None
    return {"status": "ready", "database": "ok", "rate_limit_store": "ok"}


@app.post("/api/v1/auth/login")
def login(body: LoginRequest, request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    remote = request.client.host if request.client else "unknown"
    try:
        attempts = register_login_attempt(remote, body.email)
    except RedisError:
        return _api_error("AUTH_RATE_LIMIT_UNAVAILABLE", "Không thể xác minh giới hạn đăng nhập.", 503)
    if attempts > LOGIN_LIMIT:
        return _api_error("RATE_LIMITED", "Quá nhiều lần đăng nhập. Thử lại sau ít phút.", 429)
    user = db.scalar(select(User).where(User.email == body.email.strip().casefold()))
    org = db.scalar(select(Organization).where(Organization.slug == body.org_slug.strip().casefold()))
    membership = (
        db.scalar(
            select(Membership).where(
                Membership.user_id == user.id if user else False,
                Membership.org_id == org.id if org else False,
            )
        )
        if user and org
        else None
    )
    if (
        not user
        or not user.active
        or not org
        or not membership
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=401,
            detail="Email, mật khẩu hoặc tổ chức không đúng.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    actor = Principal(user.id, org.id, org.slug, org.name, membership.role)
    token, expires = create_access_token(actor)
    try:
        clear_login_attempts(remote, body.email)
    except RedisError:
        return _api_error("AUTH_RATE_LIMIT_UNAVAILABLE", "Không thể xác minh giới hạn đăng nhập.", 503)
    return JSONResponse(
        {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires,
            "user_id": str(user.id),
            "org_id": str(org.id),
            "org_slug": org.slug,
            "org_name": org.name,
            "role": membership.role,
        }
    )


@app.get("/api/v1/auth/me")
def auth_me(actor: Annotated[Principal, Depends(current_principal)]) -> dict:
    return {
        "user_id": str(actor.user_id),
        "org_id": str(actor.org_id),
        "org_slug": actor.org_slug,
        "org_name": actor.org_name,
        "role": actor.role,
    }


@app.get("/api/v1/suppliers")
def suppliers(
    actor: Annotated[Principal, Depends(current_principal)], db: Annotated[Session, Depends(get_db)]
) -> list[dict]:
    return get_suppliers(db, actor.org_id)


@app.get("/api/v1/demo/fixtures")
def demo_fixtures(actor: Annotated[Principal, Depends(current_principal)]) -> list[dict]:
    if settings.app_env == "production":
        raise HTTPException(status_code=404, detail="Not found")
    import json

    from app.providers.fixture import MANIFEST_PATH

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return [
        {
            "file": item["file"],
            "display_name": item.get("display_name", item["label"]),
            "label": item["label"],
            "synthetic": True,
            "sha256": item["sha256"],
            "expected_findings": item["expected_findings"],
            "content_type": "image/png" if item["file"].lower().endswith(".png") else "application/pdf",
        }
        for item in manifest["fixtures"]
        if (MANIFEST_PATH.parent / item["file"]).is_file()
    ]


@app.get("/api/v1/policies/sources")
def policy_sources(
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    sources = list_policy_sources(db, actor.org_id)
    return {"sources": sources, "synthetic": True}


@app.post("/api/v1/policies/query")
def policy_query(
    body: PolicyQueryRequest,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    return query_policy(db, actor.org_id, body.question)


@app.get("/api/v1/copilot/status")
def invoice_copilot_status(
    actor: Annotated[Principal, Depends(current_principal)],
) -> dict:
    return copilot_status()


@app.post("/api/v1/invoices/{invoice_id}/copilot")
def invoice_copilot(
    invoice_id: UUID,
    body: CopilotRequest,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    # The authorization dependency has already verified this membership. Release its
    # connection before bounded model latency; each tool opens a short tenant-scoped read.
    db.close()
    return run_copilot(
        actor=actor,
        invoice_id=invoice_id,
        question=body.question,
        history=[item.model_dump() for item in body.history],
    )


@app.post("/api/v1/demo/fixtures/{filename}/upload", status_code=202)
def demo_fixture_upload(
    filename: str,
    actor: Annotated[Principal, Depends(require_role("operator", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    if settings.app_env == "production":
        raise HTTPException(status_code=404, detail="Not found")
    import json

    from app.providers.fixture import MANIFEST_PATH

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    item = next((row for row in manifest["fixtures"] if row["file"] == filename), None)
    if item is None:
        raise DomainError("FIXTURE_NOT_FOUND", "Không tìm thấy fixture demo.", 404)
    content = (MANIFEST_PATH.parent / item["file"]).read_bytes()
    return upload_invoice(db, actor, filename=item["file"], content=content, locale="vi-VN", key=key)


@app.post("/api/v1/suppliers", status_code=201)
def supplier_create(
    body: SupplierCreate,
    actor: Annotated[Principal, Depends(require_role("operator", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    return create_supplier(db, actor, body, key)


@app.get("/api/v1/purchase-orders")
def purchase_orders(
    actor: Annotated[Principal, Depends(current_principal)], db: Annotated[Session, Depends(get_db)]
) -> list[dict]:
    return get_purchase_orders(db, actor.org_id)


@app.get("/api/v1/purchase-orders/{po_id}")
def purchase_order(
    po_id: UUID,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    return get_purchase_order(db, actor.org_id, po_id)


@app.post("/api/v1/purchase-orders", status_code=201)
def purchase_order_create(
    body: POCreate,
    actor: Annotated[Principal, Depends(require_role("operator", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    return create_purchase_order(db, actor, body, key)


@app.post("/api/v1/purchase-orders/import", status_code=201)
async def purchase_orders_import(
    file: UploadFile = File(...),
    actor: Principal = Depends(require_role("operator", "reviewer")),
    db: Session = Depends(get_db),
    key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise DomainError("CSV_TOO_LARGE", "CSV nhập PO tối đa 5 MiB.", 413)
    return import_purchase_orders_csv(db, actor, content, file.filename, key)


@app.post("/api/v1/documents", status_code=202)
async def document_upload(
    file: UploadFile = File(...),
    locale: str = Form("vi-VN"),
    actor: Principal = Depends(require_role("operator", "reviewer")),
    db: Session = Depends(get_db),
    key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    content = await file.read(settings.max_file_mib * 1024 * 1024 + 1)
    return upload_invoice(db, actor, filename=file.filename, content=content, locale=locale, key=key)


@app.get("/api/v1/documents/{document_id}/content")
def document_content(
    document_id: UUID,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    content, mime, filename = get_file(db, actor, document_id)
    safe_filename = quote(filename.replace("\r", "").replace("\n", ""))
    return StreamingResponse(
        io.BytesIO(content),
        media_type=mime,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{safe_filename}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/v1/documents/{document_id}/pages/{page_no}")
def document_page(
    document_id: UUID,
    page_no: int,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    content, mime, _ = get_file(db, actor, document_id)
    from app.db.models import Document

    doc = db.scalar(select(Document).where(Document.org_id == actor.org_id, Document.id == document_id))
    if doc is None or page_no < 1 or page_no > doc.pages:
        raise DomainError("NOT_FOUND", "Không tìm thấy trang tài liệu.", 404)
    return StreamingResponse(
        io.BytesIO(content),
        media_type=mime,
        headers={
            "X-Document-Page": str(page_no),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/v1/jobs/{job_id}")
def job_detail(
    job_id: UUID,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    return get_job(db, actor, job_id)


@app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
def job_retry(
    job_id: UUID,
    actor: Annotated[Principal, Depends(require_role("operator", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    return retry_job(db, actor, job_id, key)


@app.get("/api/v1/invoices")
def invoices(
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = 100,
) -> list[dict]:
    if limit < 1 or limit > 100:
        raise DomainError("INVALID_LIMIT", "limit phải nằm trong khoảng 1–100.", 422)
    return get_invoice_list(db, actor.org_id, limit)


@app.get("/api/v1/invoices/{invoice_id}")
def invoice_detail(
    invoice_id: UUID,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    return get_invoice_detail(db, actor, invoice_id)


@app.get("/api/v1/invoices/{invoice_id}/versions")
def invoice_versions(
    invoice_id: UUID,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    return get_versions(db, actor, invoice_id)


@app.post("/api/v1/invoices/{invoice_id}/versions", status_code=201)
def invoice_version_create(
    invoice_id: UUID,
    body: VersionCreate,
    actor: Annotated[Principal, Depends(require_role("operator", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    return create_invoice_version(db, actor, invoice_id, body, key)


@app.post("/api/v1/invoices/{invoice_id}/reconciliations", status_code=201)
def invoice_reconcile(
    invoice_id: UUID,
    body: ReconciliationCreate,
    actor: Annotated[Principal, Depends(require_role("operator", "reviewer"))],
    db: Annotated[Session, Depends(get_db)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    return create_reconciliation(db, actor, invoice_id, body, key)


@app.post("/api/v1/invoices/{invoice_id}/decisions", status_code=201)
def invoice_decide(
    invoice_id: UUID,
    body: DecisionCreate,
    actor: Annotated[Principal, Depends(require_role("reviewer"))],
    db: Annotated[Session, Depends(get_db)],
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    return create_decision(db, actor, invoice_id, body, key)


@app.get("/api/v1/invoices/{invoice_id}/audit")
def invoice_audit(
    invoice_id: UUID,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    return get_audit(db, actor, invoice_id)


@app.get("/api/v1/exports/invoices/{invoice_id}.json")
def invoice_export_json(
    invoice_id: UUID,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    return export_invoice(db, actor, invoice_id)


@app.get("/api/v1/exports/invoices/{invoice_id}.csv")
def invoice_export_csv(
    invoice_id: UUID,
    actor: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    content = export_invoice_csv(db, actor, invoice_id)
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=invoice-{invoice_id}.csv"},
    )


app.mount("/api/v1", mcp_asgi_app, name="mcp")
