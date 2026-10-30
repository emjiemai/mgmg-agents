"""A local copy of the bot's database on the IT computer, refreshed with changes only (2026-10-30, the owner).

«Build a copy of the database on this PC; whenever it is awake it copies every 30 minutes — only the changes,
not the whole database.» Render's plan allows no real replication (``wal_level=replica``, no replication role)
and only one table has an ``updated_at`` column, so changes are found by **checksums**:

  1. per table: row count + a checksum of every row (both sides, the same SQL) — equal → nothing to do (most
     tables, most runs);
  2. a table that changed: rows are split into ``BUCKETS`` slices by their primary key's hash; only the slices
     whose checksum differs are deleted locally and copied again (``COPY``) — new, changed and deleted rows
     alike, without timestamps;
  3. each table in one local transaction, so the copy is never half-written.

**What is copied:** the tables IT may open on ``/db`` for good (``db_viewer.TECHNICAL_TABLES`` — the bot's
own records, the Director's order of 07.10 and the Boss's approval of 17.10). SAP, 1C, Billz, Verifix and what
is built from their figures (``PLATFORM_TABLES``) keep their **structure only** here — never their rows, also
not while ``/db`` opens them for a while. The encrypted backup to the owner's key (``scripts/backup``) stays
the copy of everything.

Production is read in a READ ONLY session with a statement timeout; nothing is ever written there. The local
schema is ``database/schema.sql`` (applied when it changes), with foreign keys dropped — a read copy filled
slice by slice. Runs and counts: local table ``_mirror_runs``; never row contents in the log.

    .venv/bin/python scripts/db-mirror/mirror.py            # one sync now
    .venv/bin/python scripts/db-mirror/mirror.py --check    # what would change, nothing written
Set up: scripts/db-mirror/README.md (install.sh: PostgreSQL, the local database, the 30-minute timer).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integrations.api.db_viewer import TECHNICAL_TABLES  # noqa: E402 — the one list of what IT may see
from integrations.common.config import settings  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402

AGENT = "db-mirror"
log = setup_logging(AGENT)

SCHEMA_PATH = ROOT / "database" / "schema.sql"
BUCKETS = 64
FULL_COPY_SHARE = 0.5  # more than half the slices changed → the whole table is copied again
REMOTE_TIMEOUT = "60s"
# Production is a 0.1-CPU database: hashing every table each run took ~40 s. Postgres counts each table's
# inserts / updates / deletes (pg_stat_user_tables) — a table whose count didn't move since the last sync is
# skipped without reading it; every table is checksummed anyway once a day (FULL_CHECK_HOURS), which also
# covers a stats reset or a TRUNCATE (not counted).
FULL_CHECK_HOURS = 24
# Only ever inserted (the audit log): new rows are copied by id, no checksum needed; a count mismatch falls
# back to the slices.
APPEND_ONLY = {"agent_actions": "id"}
# Both sides print values the same way, so the same row has the same checksum on both.
SESSION = ("SET TimeZone = 'UTC'; SET DateStyle = 'ISO, YMD'; SET IntervalStyle = 'postgres'; "
           "SET extra_float_digits = 1; SET bytea_output = 'hex'")
LOCAL_DSN_DEFAULT = "postgresql:///mgmg_mirror"


def local_dsn() -> str:
    return os.environ.get("MIRROR_DATABASE_URL") or LOCAL_DSN_DEFAULT


# ---------------------------------------------------------------------------------------------------- SQL


def row_hash(columns: list[str]) -> sql.Composable:
    """md5 of the row's text, over these columns in this order."""
    return sql.SQL("md5(ROW({})::text)").format(sql.SQL(", ").join(sql.Identifier(c) for c in columns))


def bucket_of(key: list[str]) -> sql.Composable:
    """0 … BUCKETS-1 from the primary key's md5 — the same on both sides."""
    return sql.SQL("get_byte(decode(md5(ROW({})::text), 'hex'), 0) % {}").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in key), sql.Literal(BUCKETS))


def table_check(table: str, columns: list[str]) -> sql.Composable:
    """(rows, checksum) of a whole table."""
    return sql.SQL("SELECT count(*) AS n, coalesce(md5(string_agg(h, '' ORDER BY h)), '') AS h "
                   "FROM (SELECT {} AS h FROM {}) x").format(row_hash(columns), sql.Identifier(table))


def bucket_check(table: str, columns: list[str], key: list[str]) -> sql.Composable:
    """(slice, rows, checksum) for every slice that has rows."""
    return sql.SQL("SELECT b, count(*) AS n, md5(string_agg(h, '' ORDER BY h)) AS h "
                   "FROM (SELECT {} AS b, {} AS h FROM {}) x GROUP BY b").format(
        bucket_of(key), row_hash(columns), sql.Identifier(table))


def changed_buckets(remote: dict[int, tuple[int, str]], local: dict[int, tuple[int, str]]) -> list[int]:
    """The slices to copy again: different checksum, or rows on one side only."""
    return sorted(b for b in set(remote) | set(local) if remote.get(b) != local.get(b))


# ---------------------------------------------------------------------------------------------------- catalogue


async def columns_of(conn: psycopg.AsyncConnection, table: str) -> list[str]:
    """Stored (not generated) columns, in table order."""
    rows = await (await conn.execute(
        """SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = ''
           ORDER BY a.attnum""", (table,))).fetchall()
    return [r["attname"] for r in rows]


async def primary_key(conn: psycopg.AsyncConnection, table: str) -> list[str]:
    rows = await (await conn.execute(
        """SELECT a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
           JOIN pg_namespace n ON n.oid = c.relnamespace
           JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
           WHERE n.nspname = 'public' AND c.relname = %s AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)""",
        (table,))).fetchall()
    return [r["attname"] for r in rows]


async def tables_of(conn: psycopg.AsyncConnection) -> list[str]:
    rows = await (await conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname NOT LIKE '\\_mirror%%' ORDER BY 1")).fetchall()
    return [r["relname"] for r in rows]


# ---------------------------------------------------------------------------------------------------- the local side


async def prepare_local(local: psycopg.AsyncConnection) -> bool:
    """The local schema from schema.sql when the file changed (foreign keys dropped); True if applied now."""
    await local.execute("CREATE TABLE IF NOT EXISTS _mirror_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    await local.execute(
        """CREATE TABLE IF NOT EXISTS _mirror_runs (
               id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT now(), seconds NUMERIC,
               tables INTEGER, changed INTEGER, slices INTEGER, rows_copied INTEGER, error TEXT)""")
    await local.execute(
        """CREATE TABLE IF NOT EXISTS _mirror_tables (
               name TEXT PRIMARY KEY, churn BIGINT NOT NULL, checked_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    digest = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()
    row = await (await local.execute("SELECT value FROM _mirror_meta WHERE key = 'schema_sha256'")).fetchone()
    if row and row["value"] == digest:
        return False
    await local.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    fks = await (await local.execute(
        """SELECT c.conrelid::regclass::text AS t, c.conname FROM pg_constraint c
           JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' AND c.contype = 'f'""")).fetchall()
    for fk in fks:  # a read copy filled slice by slice: no order between tables to keep
        await local.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
            sql.SQL(fk["t"]), sql.Identifier(fk["conname"])))
    await local.execute("INSERT INTO _mirror_meta VALUES ('schema_sha256', %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (digest,))
    log.info("Local schema applied ({} foreign keys dropped)", len(fks))
    return True


async def copy_rows(remote: psycopg.AsyncConnection, local: psycopg.AsyncConnection, table: str,
                    columns: list[str], where: sql.Composable | None) -> int:
    """COPY the chosen rows from production into the local table (text format, streamed)."""
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    select = sql.SQL("SELECT {} FROM {}").format(cols, sql.Identifier(table))
    if where is not None:
        select = sql.SQL("{} WHERE {}").format(select, where)
    rows = 0
    async with remote.cursor() as rcur, local.cursor() as lcur:
        async with rcur.copy(sql.SQL("COPY ({}) TO STDOUT").format(select)) as source, \
                lcur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(table), cols)) as target:
            async for chunk in source:
                await target.write(chunk)
                rows += bytes(chunk).count(b"\n")
    return rows


async def append_new(remote: psycopg.AsyncConnection, local: psycopg.AsyncConnection, table: str,
                     columns: list[str], key: str) -> int | None:
    """An insert-only table: copy the rows past the local copy's last id. None if the counts then disagree."""
    top = await (await local.execute(sql.SQL("SELECT coalesce(max({}), 0) AS m FROM {}").format(
        sql.Identifier(key), sql.Identifier(table)))).fetchone()
    async with local.transaction():
        rows = await copy_rows(remote, local, table, columns,
                               sql.SQL("{} > {}").format(sql.Identifier(key), sql.Literal(top["m"])))
    count = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
    if (await (await remote.execute(count)).fetchone())["n"] != (await (await local.execute(count)).fetchone())["n"]:
        return None
    return rows


async def churn_now(remote: psycopg.AsyncConnection) -> dict[str, int]:
    """Each table's insert + update + delete count since the server's stats began."""
    rows = await (await remote.execute(
        "SELECT relname, n_tup_ins + n_tup_upd + n_tup_del AS churn FROM pg_stat_user_tables WHERE schemaname = 'public'"
    )).fetchall()
    return {r["relname"]: int(r["churn"]) for r in rows}


def quiet(table: str, churn: dict[str, int], seen: dict[str, tuple[int, float]], now: float) -> bool:
    """Unchanged since the last sync (same count) and checked within FULL_CHECK_HOURS."""
    last = seen.get(table)
    return bool(last and table in churn and churn[table] == last[0] and now - last[1] < FULL_CHECK_HOURS * 3600)


async def sync_table(remote: psycopg.AsyncConnection, local: psycopg.AsyncConnection, table: str,
                     check_only: bool) -> dict[str, int]:
    """One table: nothing, the changed slices, or all of it. Returns counts."""
    remote_cols, local_cols = await columns_of(remote, table), set(await columns_of(local, table))
    columns = [c for c in remote_cols if c in local_cols]
    key = [c for c in await primary_key(remote, table) if c in local_cols]
    if not columns or not key:
        return {"changed": 0, "slices": 0, "rows": 0, "skipped": 1}
    if table in APPEND_ONLY and not check_only:
        appended = await append_new(remote, local, table, columns, APPEND_ONLY[table])
        if appended is not None:
            return {"changed": int(appended > 0), "slices": 0, "rows": appended, "skipped": 0}
    r = await (await remote.execute(table_check(table, columns))).fetchone()
    lc = await (await local.execute(table_check(table, columns))).fetchone()
    if (r["n"], r["h"]) == (lc["n"], lc["h"]):
        return {"changed": 0, "slices": 0, "rows": 0, "skipped": 0}
    rb = {x["b"]: (x["n"], x["h"]) for x in await (await remote.execute(bucket_check(table, columns, key))).fetchall()}
    lb = {x["b"]: (x["n"], x["h"]) for x in await (await local.execute(bucket_check(table, columns, key))).fetchall()}
    slices = changed_buckets(rb, lb)
    if check_only:
        return {"changed": 1, "slices": len(slices), "rows": 0, "skipped": 0}
    full = len(slices) > FULL_COPY_SHARE * BUCKETS
    where = None if full else sql.SQL("{} = ANY({})").format(bucket_of(key), sql.Literal(slices))
    async with local.transaction():
        if full:
            await local.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(table)))
        else:
            await local.execute(sql.SQL("DELETE FROM {} WHERE {}").format(sql.Identifier(table), where))
        rows = await copy_rows(remote, local, table, columns, where)
    return {"changed": 1, "slices": BUCKETS if full else len(slices), "rows": rows, "skipped": 0}


async def run(check_only: bool = False) -> int:
    started = time.monotonic()
    totals = {"tables": 0, "changed": 0, "slices": 0, "rows": 0}
    # autocommit on both: production reads never sit in one long transaction; each local table is its own
    # BEGIN … COMMIT (local.transaction() below).
    async with await psycopg.AsyncConnection.connect(settings.postgres_dsn, row_factory=dict_row, autocommit=True) as remote, \
            await psycopg.AsyncConnection.connect(local_dsn(), row_factory=dict_row, autocommit=True) as local:
        await remote.execute(f"{SESSION}; SET default_transaction_read_only = on; "
                             f"SET statement_timeout = '{REMOTE_TIMEOUT}'")
        await local.execute(SESSION)
        if not check_only:
            await prepare_local(local)
        here = set(await tables_of(local))
        churn = await churn_now(remote)
        seen = {} if check_only else {  # _mirror_tables is made by prepare_local above
            r["name"]: (int(r["churn"]), r["checked_at"].timestamp())
            for r in await (await local.execute("SELECT name, churn, checked_at FROM _mirror_tables")).fetchall()}
        for table in await tables_of(remote):
            if table not in here:
                continue  # not in schema.sql yet — added when the file brings it
            if table not in TECHNICAL_TABLES:
                if not check_only:  # structure only: rows of the platforms' data never stay here
                    await local.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(table)))
                continue
            if quiet(table, churn, seen, time.time()):
                continue
            counts = await sync_table(remote, local, table, check_only)
            if not check_only and table in churn:
                await local.execute(
                    "INSERT INTO _mirror_tables (name, churn) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET churn = EXCLUDED.churn, checked_at = now()", (table, churn[table]))
            totals["tables"] += 1
            for k in ("changed", "slices", "rows"):
                totals[k] += counts[k]
            if counts["changed"]:
                log.info("{}: {} slice(s), {} row(s)", table, counts["slices"], counts["rows"])
        seconds = round(time.monotonic() - started, 1)
        if not check_only:
            await local.execute("INSERT INTO _mirror_runs (seconds, tables, changed, slices, rows_copied) VALUES (%s, %s, %s, %s, %s)",
                                (seconds, totals["tables"], totals["changed"], totals["slices"], totals["rows"]))
    log.info("{}: {} tables checked, {} changed, {} slices, {} rows, {} s", "Check" if check_only else "Mirror",
             totals["tables"], totals["changed"], totals["slices"], totals["rows"], seconds)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy the bot's own tables to the local database, changes only.")
    parser.add_argument("--check", action="store_true", help="say what would change; write nothing")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args.check)))
    except psycopg.OperationalError as exc:  # asleep / offline / local server down: the next run tries again
        log.warning("Mirror skipped — a database is unreachable: {}", type(exc).__name__)
        sys.exit(0)


if __name__ == "__main__":
    main()
