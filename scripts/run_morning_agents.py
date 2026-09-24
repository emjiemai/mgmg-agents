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
    "agents/lead-agent/agent.py",
    "agents/receivables/agent.py",
    "agents/task-tracker/agent.py --morning",
]

# 17:00 Mon-Fri. The scorecard checks for Friday itself, so this list can run
# every weekday.
EVENING_AGENTS = [
    "agents/daily-reports/agent.py --remind",
    "agents/task-tracker/agent.py --weekly",
]


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


def main() -> None:
    commands = EVENING_AGENTS if "--evening" in sys.argv[1:] else AGENTS
    results: dict[str, int] = {}
    for command in commands:
        results[command] = run_agent(command)

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
