"""PostgreSQL access and the agent audit trail.

Every external call and every side effect an agent performs is written to
``agent_actions`` (security rule #2). Audit writes are best-effort: a failure to
audit is logged loudly but never aborts the agent, because a half-finished
agent run is worse than a missing audit line — the failure itself is visible in
the log file and in the run summary.
"""

from __future__ import annotations

import asyncio
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
SCHEMA_ATTEMPTS = 4  # a lock clash with the instance being replaced is retried (2026-11-11)
SCHEMA_RETRY_EVERY = 600  # seconds between later tries when every attempt failed
SCHEMA_LOCK_TIMEOUT = "15s"
_schema = {"ok": False, "retry": None}


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
        await _apply_schema(_pool)
    return _pool


async def _apply_schema(pool: AsyncConnectionPool) -> bool:
    """The schema with retries (2026-11-11: one deploy's apply failed — most likely a lock clash with the instance
    being replaced — and, the pool being kept, was never tried again: job_runs was missing and the 16:00 ask didn't
    go). A lock or deadlock error is retried a few times; if every attempt fails, the failure is logged loudly, put
    in the audit log (IT's /texnik shows it) and tried again every ``SCHEMA_RETRY_EVERY`` seconds in the background.
    The app keeps serving with the schema it already has."""
    for attempt in range(1, SCHEMA_ATTEMPTS + 1):
        try:
            await _ensure_schema(pool)
            _schema["ok"] = True
            return True
        except psycopg.Error as exc:
            log.error("Schema not applied (attempt {}/{}): {}: {}", attempt, SCHEMA_ATTEMPTS, type(exc).__name__,
                      str(exc).splitlines()[0][:200] if str(exc) else "")
            if attempt < SCHEMA_ATTEMPTS:
                await asyncio.sleep(3 * attempt)
                continue
            try:
                await log_action(agent="db", action="schema_apply", target_system="postgres", status="failure",
                                 error_message=f"{type(exc).__name__}: {str(exc)[:300]}", mode="write")
            except Exception:  # noqa: BLE001
                pass
            if _schema["retry"] is None:
                _schema["retry"] = asyncio.create_task(_retry_schema_later(pool))
            return False
    return False


async def _retry_schema_later(pool: AsyncConnectionPool) -> None:
    while not _schema["ok"]:
        await asyncio.sleep(SCHEMA_RETRY_EVERY)
        try:
            await _ensure_schema(pool)
            _schema["ok"] = True
            log.info("Schema applied on a later try")
        except psycopg.Error as exc:
            log.error("Schema still not applied: {}", type(exc).__name__)


async def _ensure_schema(pool: AsyncConnectionPool) -> None:
    """Apply ``database/schema.sql`` against the given pool.

    Args:
        pool: An already-open connection pool.

    Raises:
        psycopg.Error: if the schema fails to apply.
    """
    # A lock wait ends with an error (retried) instead of holding the start-up; LOCAL — this transaction only.
    sql = f"SET LOCAL lock_timeout = '{SCHEMA_LOCK_TIMEOUT}';\n" + SCHEMA_PATH.read_text(encoding="utf-8")
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


async def fetch_read_only(query: str, params: Any = None, timeout: str = "10s") -> list[dict[str, Any]]:
    """Like ``fetch_all``, but inside a READ ONLY transaction with a statement timeout.

    For code that answers questions (the Director's analyst): even a bug there
    cannot change or lock anything in the database.
    """
    if not (timeout[:-1].isdigit() and timeout.endswith("s")):
        raise ValueError(f"timeout must look like '10s', got {timeout!r}")
    async with connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute(f"SET LOCAL statement_timeout = '{timeout}'")
                await cur.execute(query, params)
                return await cur.fetchall()


ANALYST_ROLES = frozenset({"mgmg_analyst_director", "mgmg_analyst_sap"})  # = org_bot.sql_catalog.ROLES
_analyst_ready: dict[str, Any] = {"value": None, "at": 0.0}


async def fetch_analyst(
    sql: str, role: str, timeout: str = "4s", max_rows: int = 200
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """Run the analyst's one validated SELECT (org_bot/sql_guard.py) as a role that may read only analyst.* views.

    READ ONLY, SET LOCAL ROLE to a NOLOGIN role granted SELECT on the analyst views alone, search_path = analyst, a
    short statement / lock timeout, the row limit applied BY THE SERVER (09.11 review: a client cursor loaded a whole
    runaway join into the 512 MB app before fetchmany), and the transaction is always rolled back. Sent as a prepared
    statement so the server itself refuses a second statement; '%' is doubled so LIKE patterns aren't read as
    placeholders. The role switch rests on sql_guard's function allow-list (see sql_guard's docstring).

    Returns:
        ``(column names, at most max_rows rows as tuples, True if there were more)``.

    Raises:
        psycopg.Error: the server's error (sqlstate, message, hint) — the caller hands it back to the model.
    """
    if role not in ANALYST_ROLES:
        raise ValueError(f"not an analyst role: {role!r}")
    if not (timeout[:-1].isdigit() and timeout.endswith("s")):
        raise ValueError(f"timeout must look like '4s', got {timeout!r}")
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            async with conn.cursor(row_factory=psycopg.rows.tuple_row) as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute(f"SET LOCAL ROLE {role}")
                await cur.execute("SET LOCAL search_path = analyst, pg_catalog")
                await cur.execute(f"SET LOCAL statement_timeout = '{timeout}'")
                await cur.execute("SET LOCAL lock_timeout = '1s'")
                await cur.execute("SET LOCAL work_mem = '8MB'")
                await cur.execute("SET LOCAL jit = off")
                # prepare=True: a prepared statement — the server refuses "SELECT …; DELETE …" (checked 09.11:
                # bound-but-empty parameters alone still ran both).
                limited = f"SELECT * FROM ({sql}) AS q LIMIT {int(max_rows) + 1}"
                await cur.execute(limited.replace("%", "%%"), (), prepare=True)
                names = [d.name for d in (cur.description or [])]
                rows = await cur.fetchmany(max_rows + 1)
    return names, rows[:max_rows], len(rows) > max_rows


async def analyst_sql_ready() -> bool:
    """True when the analyst schema and the roles query_db runs as exist and the app may switch to them.

    Created by database/schema.sql at start-up; on a database where they couldn't be (no CREATEROLE), query_db is
    simply not offered. A «no» is checked again after 10 minutes; a «yes» is kept.
    """
    now = time.monotonic()
    if _analyst_ready["value"] is True or (_analyst_ready["value"] is False and now - _analyst_ready["at"] < 600):
        return bool(_analyst_ready["value"])
    try:
        row = await fetch_one(
            """SELECT to_regnamespace('analyst') IS NOT NULL
                      AND bool_and(pg_has_role(current_user, r.oid, 'SET')) AND count(*) = 2 AS ready
               FROM pg_roles r WHERE r.rolname IN ('mgmg_analyst_director', 'mgmg_analyst_sap')""")
        ready = bool(row and row.get("ready"))
    except Exception as exc:  # noqa: BLE001 — not ready is the safe answer
        log.warning("Analyst SQL readiness not checked: {}", type(exc).__name__)
        ready = False
    _analyst_ready.update(value=ready, at=now)
    return ready


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
