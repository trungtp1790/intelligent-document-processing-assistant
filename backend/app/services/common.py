from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, IdempotencyRequest
from app.domain.errors import DomainError
from app.domain.snapshots import canonical_hash
from app.security import Principal


@dataclass(frozen=True)
class IdempotencyClaim:
    id: uuid.UUID


@dataclass(frozen=True)
class IdempotentReplay:
    response: dict[str, Any]


def begin_idempotent(
    db: Session, actor: Principal, route_key: str, key: str | None, request: Any
) -> IdempotencyClaim | IdempotentReplay:
    if not key or len(key) < 8 or len(key) > 200:
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Gửi Idempotency-Key hợp lệ cho thao tác này.", 400)
    request_hash = canonical_hash(request)
    identifier = uuid.uuid4()
    statement = (
        pg_insert(IdempotencyRequest)
        .values(
            id=identifier,
            org_id=actor.org_id,
            actor_id=actor.user_id,
            route_key=route_key,
            key=key,
            request_hash=request_hash,
            resource_id=uuid.uuid4(),
            response_snapshot={},
        )
        .on_conflict_do_nothing(index_elements=["org_id", "actor_id", "route_key", "key"])
    )
    inserted_id = db.execute(statement.returning(IdempotencyRequest.id)).scalar_one_or_none()
    if inserted_id is not None:
        return IdempotencyClaim(inserted_id)
    existing = db.scalar(
        select(IdempotencyRequest).where(
            IdempotencyRequest.org_id == actor.org_id,
            IdempotencyRequest.actor_id == actor.user_id,
            IdempotencyRequest.route_key == route_key,
            IdempotencyRequest.key == key,
        )
    )
    if existing is None:
        raise DomainError("IDEMPOTENCY_CONFLICT", "Không thể xác nhận yêu cầu lặp. Hãy thử lại.", 409)
    if existing.request_hash != request_hash:
        raise DomainError("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key đã dùng với nội dung khác.", 409)
    return IdempotentReplay(existing.response_snapshot)


def finish_idempotent(
    db: Session, claim: IdempotencyClaim, resource_id: uuid.UUID, response: dict[str, Any]
) -> None:
    entry = db.get(IdempotencyRequest, claim.id)
    if entry is None:
        raise RuntimeError("Idempotency reservation disappeared")
    entry.resource_id = resource_id
    entry.response_snapshot = response


def audit(
    db: Session,
    actor: Principal | None,
    org_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    *,
    before_hash: str | None = None,
    after_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditEvent(
            org_id=org_id,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor.user_id if actor else None,
            action=action,
            before_hash=before_hash,
            after_hash=after_hash,
            metadata_json=metadata or {},
        )
    )
