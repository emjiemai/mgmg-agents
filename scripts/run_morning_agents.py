"""Runs the three 08:00 Asia/Tashkent agents back to back in one cron service.

CEO Daily Brief, Lead Agent and Receivables all used to be separate Render
cron services scheduled at the same moment ("0 3 * * *" UTC) purely because
Render has no free tier for cron at all — each service is billed a ~$1/month
minimum regardless of how little it actually runs. Folding them into one
service keeps that to a single ~$1/month minimum.

Each agent runs as its own subprocess, not an in-process import, so one
agent crashing (an unhandled exception, even a segfault) can't take the
others down with it — the same isolation separate cron services gave for
free. Every agent runs regardless of whether an earlier one failed; the
wrapper's own exit code is non-zero if any agent failed, so Render's cron run
history still shows a real failure rather than a false "success" if one of
the three had a problem.

The 17:00 job reuses this runner (``--evening``) for the same reason: the
report reminder and the Friday task scorecard share one cron service.

Run:
    python scripts/run_morning_agents.py            # 08:00
    python scripts/run_morning_agents.py --evening  # 17:00
    python scripts/run_morning_agents.py --daytime  # nothing since 2026-10-17 (team cheer removed)
    python scripts/run_morning_agents.py --force    # again, though today's run already happened

Since 2026-11-11 mgmg-api's own loop starts these (integrations/api/scheduler.py); each run first claims its day
(common/job_runs.py), so a second start the same day does nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Relative to PROJECT_ROOT, matching how each was already invoked as its own
# cron service's dockerCommand in render.yaml.
AGENTS = [
    "agents/ceo-daily-brief/agent.py",
    "agents/report-bonus/agent.py",  # Garmin сотув: yesterday's report → +1 KPI, or politely none (a note)
    "agents/explanations/agent.py",  # absent yesterday / report not sent: asked why (a note); yesterday's answers summed up
    "agents/task-tracker/agent.py --morning",  # tasks due today / tomorrow, just overdue (notes; the Director's list)
    "agents/morning-notes/agent.py",  # 2026-10-24: each employee ONE message from the notes above, one question at most
    "agents/billz-sap-check/agent.py",  # did every shop cheque reach SAP? (needs the full SAP push)
    "agents/garmin-brief/agent.py",  # the Garmin manager's brief (after the check: it shows its result)
    "agents/lead-agent/agent.py",
    "agents/lead-handout/agent.py --morning",  # 28.10: one numbered lead per B2B seller (when on — /lidlar), the Boss's list
    "agents/receivables/agent.py",
    "agents/data-quality/agent.py",  # sends only on Mondays
    "agents/cash-calendar/agent.py",  # sends only on Mondays
    "agents/old-stock/agent.py",  # Mondays: goods a year or more in stock — the Director, the Garmin manager
    "agents/garmin-competitors/agent.py",  # weekly (Thursday): who else sells Garmin, for how much — the Garmin manager
    "agents/task-tracker/agent.py --monthly",  # sends only on the 1st
    "agents/db-review/agent.py",  # the bot's own tables: stale, test, duplicate rows (counts to IT)
    "agents/tech-report/agent.py",  # last: IT's report on how the runs above went (no figures)
]

# 17:00 every day (the reminder only goes to people asked today, so on a
# weekend just the weekend workers). The scorecard checks for Friday itself.
EVENING_AGENTS = [
    "agents/daily-reports/agent.py --remind",
    "agents/task-tracker/agent.py --weekly",
]


# The daytime job is empty: team cheer was removed (2026-10-17, the owner: "not effective")
# and the 15:00 lead check-in with the lead hand-out (2026-10-07). A leftover
# mgmg-team-cheer cron on Render runs nothing — delete that service. The lead tracker's 15:00
# follow-up (back 2026-10-28) runs inside the API's 5-minute round, not here.
DAYTIME_AGENTS: list[str] = []


def run_agent(relative_command: str) -> int:
    """Run one agent as a subprocess and return its exit code.

    Args:
        relative_command: Script path, optionally followed by CLI args, e.g.
            "agents/receivables/agent.py --dry-run".

    Returns:
        The subprocess's exit code (0 = success).
    """
    parts = relative_command.split()
    script = str(PROJECT_ROOT / parts[0])
    args = parts[1:]

    print(f"\n=== Running {relative_command} ===", flush=True)
    result = subprocess.run([sys.executable, script, *args], cwd=PROJECT_ROOT)
    return result.returncode


def _lease():
    """The job_runs helpers (imported late: the project root on sys.path first)."""
    sys.path.insert(0, str(PROJECT_ROOT))
    from integrations.common import job_runs
    from integrations.common.timeutil import today_local

    return job_runs, today_local()


def main() -> None:
    import threading

    args = sys.argv[1:]
    commands = EVENING_AGENTS if "--evening" in args else DAYTIME_AGENTS if "--daytime" in args else AGENTS
    job = "evening" if "--evening" in args else "daytime" if "--daytime" in args else "morning"
    # One run a day, whoever starts it — mgmg-api's loop or the old Render cron (2026-11-11) — kept alive by a
    # heartbeat; a run killed by a deploy is taken over by the next start, which skips the agents already started.
    # --force / --dry-run: run everything anyway.
    guarded = "--force" not in args and "--dry-run" not in args
    stop = threading.Event()
    if guarded:
        job_runs, day = _lease()
        if not job_runs.claim_sync(job, day):
            print(f"{job}: already run today, or running elsewhere — nothing to do.")
            return

        def heartbeat() -> None:
            while not stop.wait(60):
                job_runs.beat_sync(job, day)

        threading.Thread(target=heartbeat, daemon=True).start()
    results: dict[str, int] = {}
    try:
        for command in commands:
            if guarded and not job_runs.once_sync(f"{job}:{command}", day):
                print(f"\n=== Skipping {command}: already started today (a resumed run) ===", flush=True)
                continue
            results[command] = run_agent(command)
    finally:
        stop.set()
    if guarded:
        job_runs.finish_sync(job, day)

    print("\n=== Morning agents summary ===")
    failed = [name for name, code in results.items() if code != 0]
    for name, code in results.items():
        status = "ok" if code == 0 else f"FAILED (exit {code})"
        print(f"  {status:20} {name}")

    if failed:
        print(f"\n{len(failed)}/{len(results)} agent(s) failed: {', '.join(failed)}")
        sys.exit(1)

    print(f"\nAll {len(results)} agent(s) completed successfully.")


if __name__ == "__main__":
    main()
