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
from integrations.common.job_runs import state
from integrations.common.logging_setup import setup_logging

log = setup_logging("scheduler")

# job name (as the command claims it) → (window start, window end, command). The report ask may still go until 19:00
# (2026-11-11: a failed 16:00 start had no second chance; reports are taken until 22:00); the 17:00 reminder waits for
# the ask to FINISH until 18:00 (AFTER), then goes anyway; the ask claims and finishes even on days nobody is asked.
JOBS: dict[str, tuple[time, time, tuple[str, ...]]] = {
    "morning": (time(8, 0), time(9, 0), ("scripts/run_morning_agents.py",)),
    "reports-ask": (time(16, 0), time(19, 0), ("agents/daily-reports/agent.py", "--ask")),
    "evening": (time(17, 0), time(20, 0), ("scripts/run_morning_agents.py", "--evening")),
}
AFTER: dict[str, tuple[str, time]] = {"evening": ("reports-ask", time(18, 0))}
MAX_STARTS = 6  # starts per job a day from this process (a command that keeps failing doesn't loop all window)
_starts: dict[tuple[str, str], int] = {}
_running: dict[str, asyncio.Task] = {}


def due(now: datetime) -> list[str]:
    """The jobs whose window is open now."""
    return [job for job, (start, end, _cmd) in JOBS.items() if start <= now.time() < end]


def _lower_priority() -> None:  # pragma: no cover — runs in the child process
    try:
        os.nice(10)
    except OSError:
        pass
    try:  # if the container runs out of memory, a job dies first — never uvicorn and the bots (2026-11-11 review)
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass


async def _run(job: str, command: tuple[str, ...]) -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, *command, cwd=str(PROJECT_ROOT),
                                                preexec_fn=_lower_priority)
    code = await proc.wait()
    (log.info if code == 0 else log.error)("Scheduled job {} finished (exit {})", job, code)


async def tick(now: datetime) -> list[str]:
    """Start every job whose window is open and that isn't finished or running anywhere; returns the jobs started.

    Tried every round inside the window (2026-11-11 review: one failed start had no second chance), at most
    ``MAX_STARTS`` times a day from this process. A job running in this process, or with a live lease anywhere (the
    old Render cron), is left alone; a dead lease (a run killed by a deploy) is taken over by the command itself.
    """
    if not settings.internal_jobs_enabled:
        return []
    started = []
    day = now.date()
    for job in due(now):
        task = _running.get(job)
        if task is not None and not task.done():
            continue
        if _starts.get((job, day.isoformat()), 0) >= MAX_STARTS:
            continue
        if await state(job, day) in ("finished", "running"):
            continue
        first, until = AFTER.get(job, ("", time(0, 0)))
        if first and now.time() < until and await state(first, day) != "finished":
            continue  # the reminder after the ask (tried again next round)
        _starts[(job, day.isoformat())] = _starts.get((job, day.isoformat()), 0) + 1
        _running[job] = asyncio.create_task(_run(job, JOBS[job][2]))
        started.append(job)
        log.info("Scheduled job {} started", job)
    return started
