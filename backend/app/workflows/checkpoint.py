from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row

from app.config import settings


def _psycopg_url() -> str:
    url = settings.database_url
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    return url


@contextmanager
def postgres_checkpointer() -> Iterator[PostgresSaver]:
    connection = psycopg.connect(_psycopg_url(), autocommit=True, row_factory=dict_row)
    try:
        serializer = JsonPlusSerializer(allowed_msgpack_modules=[])
        yield PostgresSaver(connection, serde=serializer)
    finally:
        connection.close()


@contextmanager
def workflow_run_lock(job_id: str) -> Iterator[bool]:
    """Serialize graph deliveries for one job; PostgreSQL releases it if a worker dies."""
    connection = psycopg.connect(_psycopg_url(), autocommit=True, row_factory=dict_row)
    acquired = False
    try:
        row = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('idp-invoice-graph:' || %s, 0)) AS acquired",
            (job_id,),
        ).fetchone()
        acquired = bool(row["acquired"])
        yield acquired
    finally:
        if acquired:
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended('idp-invoice-graph:' || %s, 0))",
                (job_id,),
            )
        connection.close()


def setup_checkpoint_tables() -> None:
    with postgres_checkpointer() as checkpointer:
        checkpointer.setup()
