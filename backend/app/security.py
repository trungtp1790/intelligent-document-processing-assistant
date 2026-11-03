from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash

from app.config import settings

password_hash = PasswordHash.recommended()


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    org_id: UUID
    org_slug: str
    org_name: str
    role: str


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return password_hash.verify(password, encoded)
    except Exception:
        return False


def create_access_token(principal: Principal) -> tuple[str, int]:
    if len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be configured with at least 32 characters")
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_minutes)
    claims = {
        "sub": str(principal.user_id),
        "org_id": str(principal.org_id),
        "org_slug": principal.org_slug,
        "org_name": principal.org_name,
        "role": principal.role,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": expires,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256"), int((expires - now).total_seconds())


def decode_access_token(token: str) -> Principal:
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        options={"require": ["sub", "org_id", "role", "exp", "iat"]},
    )
    if payload.get("role") not in {"operator", "reviewer"}:
        raise jwt.InvalidTokenError("Invalid role")
    return Principal(
        UUID(payload["sub"]),
        UUID(payload["org_id"]),
        payload["org_slug"],
        payload["org_name"],
        payload["role"],
    )
