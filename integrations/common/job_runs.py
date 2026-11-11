"""One run per scheduled job per day (2026-11-11), however many places start it.

The 08:00 / 16:00 / 17:00 jobs moved into mgmg-api's loop (``integrations/api/scheduler.py``); the job's own command
calls ``claim`` first and stops if the day is already taken — so the old Render cron service (until it is deleted),
a restart inside the start window, or a second instance can never send the brief or the report ask twice.

Never in the way (2026-11-11: one deploy's schema didn't apply, ``job_runs`` was missing and the 16:00 ask failed
in both the loop and the cron): the table is created here when missing, and a claim that can't be made at all lets
the job run — a missed brief is worse than a rare double.
"""

from __future__ import annotations

from datetime import date

from integrations.common.db import execute, fetch_one
from integrations.common.logging_setup import setup_logging

log = setup_logging("scheduler")

TABLE = """CREATE TABLE IF NOT EXISTS job_runs (
    job         TEXT         NOT NULL,
    day         DATE         NOT NULL,
    started_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (job, day)
)"""
_ready = {"table": False}


async def _table() -> None:
    if _ready["table"]:
        return
    try:
        await execute(TABLE)
        _ready["table"] = True
    except Exception as exc:  # noqa: BLE001 — two processes creating it at once; the insert below tells
        log.warning("job_runs not created here: {}", type(exc).__name__)


async def claim(job: str, day: date) -> bool:
    """True for the first caller of (job, day); False for every later one — and True when it can't be checked."""
    try:
        await _table()
        row = await fetch_one(
            "INSERT INTO job_runs (job, day) VALUES (%s, %s) ON CONFLICT (job, day) DO NOTHING RETURNING job",
            (job, day))
        return row is not None
    except Exception as exc:  # noqa: BLE001
        log.error("Claim for {} not checked ({}) — running anyway", job, type(exc).__name__)
        return True


async def claimed(job: str, day: date) -> bool:
    """Whether (job, day) already ran; False when it can't be checked (the job's own claim decides then)."""
    try:
        await _table()
        return await fetch_one("SELECT 1 AS x FROM job_runs WHERE job = %s AND day = %s", (job, day)) is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("Claim for {} not read ({})", job, type(exc).__name__)
        return False
