"""One run per scheduled job per day — a lease that a dead run gives up (2026-11-11).

The 08:00 / 16:00 / 17:00 jobs start from mgmg-api's loop (``integrations/api/scheduler.py``) and, until they are
deleted, from the old Render cron services; the job's own command calls ``claim`` first:

  * the first caller gets the day and keeps it alive with ``beat`` (heartbeat) while it runs, ``finish`` at the end;
  * a second caller while it runs, or after it finished, gets nothing — no double brief or ask;
  * a run that died (a deploy or an out-of-memory kill inside the container — 2026-11-11 review: the day was lost for
    good) stops beating; after ``LEASE_SECONDS`` the next start takes over and goes on.

Inside the morning job each agent is claimed once (``once``): a resumed run skips the agents already started, so the
brief or the morning notes never go twice — an agent killed half-way is not repeated.

The table is created here when missing (2026-11-11: a deploy's schema didn't apply, ``job_runs`` was missing and the
16:00 ask failed); a claim that can't be checked at all lets the job run — a missed brief is worse than a rare double.
Two flavours: async (the API, daily-reports) and sync (``scripts/run_morning_agents.py``, which must not apply the
whole schema just to claim — plain psycopg, one short connection per call).
"""

from __future__ import annotations

from datetime import date

from integrations.common.logging_setup import setup_logging

log = setup_logging("scheduler")

LEASE_SECONDS = 180  # no heartbeat for this long = the run died; the next start takes over
TABLE = """CREATE TABLE IF NOT EXISTS job_runs (
    job          TEXT         NOT NULL,
    day          DATE         NOT NULL,
    started_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (job, day)
);
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ"""
CLAIM = f"""INSERT INTO job_runs (job, day) VALUES (%s, %s)
ON CONFLICT (job, day) DO UPDATE SET started_at = now(), heartbeat_at = now()
    WHERE job_runs.finished_at IS NULL AND job_runs.heartbeat_at < now() - interval '{LEASE_SECONDS} seconds'
RETURNING job"""
ONCE = "INSERT INTO job_runs (job, day) VALUES (%s, %s) ON CONFLICT (job, day) DO NOTHING RETURNING job"
BEAT = "UPDATE job_runs SET heartbeat_at = now() WHERE job = %s AND day = %s AND finished_at IS NULL"
FINISH = "UPDATE job_runs SET finished_at = now(), heartbeat_at = now() WHERE job = %s AND day = %s"
STATE = f"""SELECT finished_at IS NOT NULL AS finished, heartbeat_at > now() - interval '{LEASE_SECONDS} seconds' AS live
FROM job_runs WHERE job = %s AND day = %s"""
_ready = {"table": False}


# ---------------------------------------------------------------- async (the API, daily-reports)

async def _table() -> None:
    from integrations.common.db import execute

    if _ready["table"]:
        return
    try:
        await execute(TABLE)
        _ready["table"] = True
    except Exception as exc:  # noqa: BLE001 — two processes creating it at once; the next statement tells
        log.warning("job_runs not prepared here: {}", type(exc).__name__)


async def claim(job: str, day: date) -> bool:
    """The day's lease: True for the first caller, or one taking over a dead run; False while another run is alive or
    after it finished — and True when it can't be checked."""
    from integrations.common.db import fetch_one

    try:
        await _table()
        return await fetch_one(CLAIM, (job, day)) is not None
    except Exception as exc:  # noqa: BLE001
        log.error("Claim for {} not checked ({}) — running anyway", job, type(exc).__name__)
        return True


async def finish(job: str, day: date) -> None:
    from integrations.common.db import execute

    try:
        await execute(FINISH, (job, day))
    except Exception as exc:  # noqa: BLE001 — the lease simply expires; a rerun is idempotent
        log.warning("Finish of {} not recorded ({})", job, type(exc).__name__)


async def state(job: str, day: date) -> str:
    """'finished', 'running' (a live lease), 'dead' (claimed, no heartbeat for LEASE_SECONDS) or 'none'."""
    from integrations.common.db import fetch_one

    try:
        await _table()
        row = await fetch_one(STATE, (job, day))
    except Exception as exc:  # noqa: BLE001 — unknown: let the job's own claim decide
        log.warning("State of {} not read ({})", job, type(exc).__name__)
        return "none"
    if row is None:
        return "none"
    return "finished" if row["finished"] else "running" if row["live"] else "dead"


async def claimed(job: str, day: date) -> bool:
    """Finished today (kept for callers that only need «done?»)."""
    return await state(job, day) == "finished"


# ---------------------------------------------------------------- sync (scripts/run_morning_agents.py)

def _sync(sql: str, params: tuple, *, prepare: bool = False) -> bool | None:
    """One statement on a short connection of its own; True if it returned a row, None on any error."""
    import psycopg

    from integrations.common.config import settings

    try:
        with psycopg.connect(settings.postgres_dsn, autocommit=True, connect_timeout=10) as conn:
            if prepare and not _ready["table"]:
                conn.execute(TABLE)
                _ready["table"] = True
            cur = conn.execute(sql, params)
            return cur.description is not None and cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("job_runs: {} ({})", sql.split()[0], type(exc).__name__)
        return None


def claim_sync(job: str, day: date) -> bool:
    got = _sync(CLAIM, (job, day), prepare=True)
    if got is None:
        log.error("Claim for {} not checked — running anyway", job)
        return True
    return got


def once_sync(key: str, day: date) -> bool:
    """One start of one agent of a job a day (never repeated by a resumed run); True when it can't be checked."""
    got = _sync(ONCE, (key, day), prepare=True)
    return True if got is None else got


def beat_sync(job: str, day: date) -> None:
    _sync(BEAT, (job, day))


def finish_sync(job: str, day: date) -> None:
    _sync(FINISH, (job, day))
