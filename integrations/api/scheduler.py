"""The 08:00 / 16:00 / 17:00 jobs, started from mgmg-api's own 5-minute loop (2026-11-11, the owner: drop the Render
cron services, ≈ $5/month).

Each job is the same command the cron ran, started as its own low-priority process: a crash or heavy work (the
brief's picture, the Lead Agent's search) can't block the bots' webhooks or take the API down. The command claims
(job, day) first (``common/job_runs.py``) — whoever starts first runs, any other start that day does nothing.
A job starts only inside its window (a restart at 08:20 still sends the 08:00 brief; at 11:00 it doesn't).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, time

from integrations.common.config import PROJECT_ROOT, settings
from integrations.common.job_runs import claimed
from integrations.common.logging_setup import setup_logging

log = setup_logging("scheduler")

# job name (as the command claims it) → (window start, window end, command)
JOBS: dict[str, tuple[time, time, tuple[str, ...]]] = {
    "morning": (time(8, 0), time(9, 0), ("scripts/run_morning_agents.py",)),
    "reports-ask": (time(16, 0), time(16, 30), ("agents/daily-reports/agent.py", "--ask")),
    "evening": (time(17, 0), time(17, 30), ("scripts/run_morning_agents.py", "--evening")),
}
_started: set[tuple[str, str]] = set()
_running: set[asyncio.Task] = set()


def due(now: datetime) -> list[str]:
    """The jobs whose window is open now and that this process hasn't started today."""
    return [job for job, (start, end, _cmd) in JOBS.items()
            if start <= now.time() < end and (job, now.date().isoformat()) not in _started]


def _lower_priority() -> None:  # pragma: no cover — runs in the child process
    try:
        os.nice(10)
    except OSError:
        pass


async def _run(job: str, command: tuple[str, ...]) -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, *command, cwd=str(PROJECT_ROOT),
                                                preexec_fn=_lower_priority)
    code = await proc.wait()
    (log.info if code == 0 else log.error)("Scheduled job {} finished (exit {})", job, code)


async def tick(now: datetime) -> list[str]:
    """Start every due job not yet claimed today; returns the jobs started."""
    if not settings.internal_jobs_enabled:
        return []
    started = []
    for job in due(now):
        _started.add((job, now.date().isoformat()))
        if await claimed(job, now.date()):
            continue  # the Render cron (or an earlier start) already ran it
        task = asyncio.create_task(_run(job, JOBS[job][2]))
        _running.add(task)
        task.add_done_callback(_running.discard)
        started.append(job)
        log.info("Scheduled job {} started", job)
    return started
