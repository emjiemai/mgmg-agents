"""PostgreSQL access and the agent audit trail.

Every external call and every side effect an agent performs is written to
``agent_actions`` (security rule #2). Audit writes are best-effort: a failure to
audit is logged loudly but never aborts the agent, because a half-finished
agent run is worse than a missing audit line — the failure itself is visible in
the log file and in the run summary.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from integrations.common.config import PROJECT_ROOT, settings
from integrations.common.logging_setup import setup_logging

log = setup_logging("db")

Mode = Literal["read", "write", "notify"]
Status = Literal["success", "failure", "skipped", "dry_run"]

SCHEMA_PATH = PROJECT_ROOT / "database" / "schema.sql"

_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    """Return the process-wide connection pool, opening it on first use.

    Every schema statement is ``CREATE ... IF NOT EXISTS`` / ``CREATE OR
    REPLACE``, so applying it on every process start is safe and cheap — this
    is what makes the schema self-provisioning on any host (VPS, Render, a
    fresh CI run) without a separate migration step that's easy to forget, as
    it was on the first Render deploy.

    Returns:
        An open ``AsyncConnectionPool`` (min 1, max 5 connections).

    Raises:
        psycopg.OperationalError: if PostgreSQL is unreachable.
        psycopg.Error: if the schema fails to apply (e.g. insufficient
            privileges for ``CREATE EXTENSION``).
    """
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=settings.postgres_dsn,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await _pool.open(wait=True, timeout=10)
        log.debug("PostgreSQL pool opened ({}:{})", settings.postgres_host, settings.postgres_port)
        await _ensure_schema(_pool)
    return _pool


async def _ensure_schema(pool: AsyncConnectionPool) -> None:
    """Apply ``database/schema.sql`` against the given pool.

    Args:
        pool: An already-open connection pool.

    Raises:
        psycopg.Error: if the schema fails to apply.
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # No parameters -> psycopg uses the simple query protocol, which
            # runs every ';'-separated statement in the file as one script.
            await cur.execute(sql)
    log.info("Schema ensured ({})", SCHEMA_PATH.name)


async def close_pool() -> None:
    """Close the pool. Call once at process shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection() -> AsyncIterator[psycopg.AsyncConnection]:
    """Yield a pooled connection inside a transaction.

    Commits on clean exit, rolls back on exception.

    Yields:
        An ``AsyncConnection`` with ``dict_row`` row factory.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        yield conn


async def fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
    """Run a SELECT and return every row as a dict.

    Args:
        query: SQL with ``%s`` placeholders.
        params: Sequence or mapping of bind values.

    Returns:
        List of row dicts (empty if no rows).

    Raises:
        psycopg.Error: on SQL or connection failure.
    """
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchall()


async def fetch_one(query: str, params: Any = None) -> dict[str, Any] | None:
    """Run a SELECT and return the first row, or None.

    Args:
        query: SQL with ``%s`` placeholders.
        params: Sequence or mapping of bind values.

    Returns:
        The first row as a dict, or ``None`` if the query matched nothing.

    Raises:
        psycopg.Error: on SQL or connection failure.
    """
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchone()


async def execute(query: str, params: Any = None) -> int:
    """Run an INSERT/UPDATE/DELETE.

    Args:
        query: SQL with ``%s`` placeholders.
        params: Sequence or mapping of bind values.

    Returns:
        Number of rows affected.

    Raises:
        psycopg.Error: on SQL or connection failure.
    """
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return cur.rowcount


async def execute_many(query: str, rows: list[tuple[Any, ...]]) -> int:
    """Run one statement against many parameter tuples in a single transaction.

    Args:
        query: SQL with ``%s`` placeholders.
        rows: Parameter tuples, one per statement execution.

    Returns:
        Number of rows submitted (0 if ``rows`` is empty).

    Raises:
        psycopg.Error: on SQL or connection failure; the whole batch rolls back.
    """
    if not rows:
        return 0
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(query, rows)
    return len(rows)


async def log_action(
    *,
    agent: str,
    action: str,
    target_system: str,
    status: Status,
    run_id: uuid.UUID | str | None = None,
    target_ref: str | None = None,
    mode: Mode = "read",
    duration_ms: int | None = None,
    http_status: int | None = None,
    error_message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one row to the ``agent_actions`` audit table.

    Never raises — an audit failure is logged and swallowed so it cannot break
    an agent run.

    Args:
        agent: Agent name, e.g. 'ceo-daily-brief'.
        action: What happened, e.g. 'api_call', 'telegram_send'.
        target_system: 'sap' | 'amocrm' | 'verifix' | 'msgraph' | 'telegram' | 'postgres'.
        status: Outcome of the action.
        run_id: UUID grouping all actions of one run.
        target_ref: Endpoint, entity id, or chat id touched.
        mode: 'read' for reads, 'write' for mutations, 'notify' for messages.
        duration_ms: Wall time of the action.
        http_status: HTTP status code, when applicable.
        error_message: Truncated error text on failure.
        payload: Non-secret summary of request/response.
    """
    try:
        await execute(
            """
            INSERT INTO agent_actions
                (agent, run_id, action, target_system, target_ref, mode,
                 status, duration_ms, http_status, error_message, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                agent,
                str(run_id) if run_id else None,
                action,
                target_system,
                target_ref,
                mode,
                status,
                duration_ms,
                http_status,
                (error_message or "")[:2000] or None,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — audit must never break the caller
        log.error("Audit write failed ({} / {}): {}", agent, action, exc)


@asynccontextmanager
async def audited(
    *,
    agent: str,
    action: str,
    target_system: str,
    run_id: uuid.UUID | str | None = None,
    target_ref: str | None = None,
    mode: Mode = "read",
    payload: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Time a block of work and audit it, whether it succeeds or raises.

    The yielded dict is a mutable context: set ``ctx['http_status']`` or add
    keys to ``ctx['payload']`` inside the block and they land in the audit row.

    Args:
        agent: Agent name.
        action: What is being attempted.
        target_system: System being touched.
        run_id: UUID grouping this run's actions.
        target_ref: Endpoint or entity reference.
        mode: 'read' | 'write' | 'notify'.
        payload: Initial non-secret context.

    Yields:
        A dict with keys ``http_status`` and ``payload``.

    Raises:
        Exception: re-raises whatever the wrapped block raised, after auditing.
    """
    ctx: dict[str, Any] = {"http_status": None, "payload": dict(payload or {})}
    started = time.perf_counter()
    try:
        yield ctx
    except Exception as exc:
        await log_action(
            agent=agent,
            action=action,
            target_system=target_system,
            status="failure",
            run_id=run_id,
            target_ref=target_ref,
            mode=mode,
            duration_ms=int((time.perf_counter() - started) * 1000),
            http_status=ctx.get("http_status"),
            error_message=f"{type(exc).__name__}: {exc}",
            payload=ctx.get("payload"),
        )
        raise
    else:
        await log_action(
            agent=agent,
            action=action,
            target_system=target_system,
            status="dry_run" if settings.dry_run and mode != "read" else "success",
            run_id=run_id,
            target_ref=target_ref,
            mode=mode,
            duration_ms=int((time.perf_counter() - started) * 1000),
            http_status=ctx.get("http_status"),
            payload=ctx.get("payload"),
        )
