from __future__ import annotations

import hashlib
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import McpServiceToken, Organization
from app.db.session import SessionLocal

MCP_TOKEN_PREFIX = "idpmcp_"
MCP_TOKEN_SCOPES = frozenset(
    {
        "invoices:list",
        "invoices:summary",
        "invoices:reconciliation",
        "purchase_orders:read",
    }
)


@dataclass(frozen=True)
class McpPrincipal:
    token_id: UUID
    org_id: UUID
    org_slug: str
    org_name: str
    scopes: frozenset[str]


mcp_principal: ContextVar[McpPrincipal | None] = ContextVar("mcp_principal", default=None)


def hash_mcp_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_mcp_scopes(scopes: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    normalized = sorted(set(scopes))
    if not normalized:
        raise ValueError("At least one MCP scope is required.")
    unknown = set(normalized) - MCP_TOKEN_SCOPES
    if unknown:
        raise ValueError(f"Unsupported MCP scope: {', '.join(sorted(unknown))}.")
    return normalized


def issue_mcp_token(
    db: Session,
    *,
    org_id: UUID,
    name: str,
    scopes: list[str] | tuple[str, ...] | set[str],
    expires_in_days: int = 30,
) -> tuple[str, McpServiceToken]:
    normalized_name = name.strip()
    if not 1 <= len(normalized_name) <= 120:
        raise ValueError("Token name must be between 1 and 120 characters.")
    if not 1 <= expires_in_days <= 365:
        raise ValueError("Token expiry must be between 1 and 365 days.")
    if db.scalar(select(Organization.id).where(Organization.id == org_id)) is None:
        raise ValueError("Organization does not exist.")

    plaintext = f"{MCP_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    row = McpServiceToken(
        id=uuid4(),
        org_id=org_id,
        name=normalized_name,
        token_hash=hash_mcp_token(plaintext),
        scopes=validate_mcp_scopes(scopes),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return plaintext, row


def revoke_mcp_token(db: Session, token_id: UUID) -> bool:
    row = db.scalar(select(McpServiceToken).where(McpServiceToken.id == token_id).with_for_update())
    if row is None or row.revoked_at is not None:
        db.rollback()
        return False
    row.revoked_at = datetime.now(UTC)
    db.commit()
    return True


def authenticate_mcp_token(plaintext: str) -> McpPrincipal | None:
    if not plaintext.startswith(MCP_TOKEN_PREFIX) or len(plaintext) > 128:
        return None
    now = datetime.now(UTC)
    with SessionLocal() as db:
        result = db.execute(
            select(McpServiceToken, Organization.slug, Organization.name)
            .join(Organization, Organization.id == McpServiceToken.org_id)
            .where(McpServiceToken.token_hash == hash_mcp_token(plaintext))
        ).one_or_none()
        if result is None:
            return None
        row, org_slug, org_name = result
        if row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
            return None
        try:
            scopes = frozenset(validate_mcp_scopes(row.scopes))
        except (TypeError, ValueError):
            return None
        return McpPrincipal(row.id, row.org_id, org_slug, org_name, scopes)
