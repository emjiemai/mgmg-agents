"""One run per scheduled job per day (2026-11-11), however many places start it.

The 08:00 / 16:00 / 17:00 jobs moved into mgmg-api's loop (``integrations/api/scheduler.py``); the job's own command
calls ``claim`` first and stops if the day is already taken — so the old Render cron service (until it is deleted),
a restart inside the start window, or a second instance can never send the brief or the report ask twice.
"""

from __future__ import annotations

from datetime import date

from integrations.common.db import fetch_one


async def claim(job: str, day: date) -> bool:
    """True for the first caller of (job, day); False for every later one."""
    row = await fetch_one(
        "INSERT INTO job_runs (job, day) VALUES (%s, %s) ON CONFLICT (job, day) DO NOTHING RETURNING job", (job, day))
    return row is not None


async def claimed(job: str, day: date) -> bool:
    return await fetch_one("SELECT 1 AS x FROM job_runs WHERE job = %s AND day = %s", (job, day)) is not None
