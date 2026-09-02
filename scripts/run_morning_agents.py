"""Runs the four 08:00 Asia/Tashkent agents back to back in one cron service.

CEO Daily Brief, Lead Agent, Receivables, and the CRM follow-up sweep all
used to be separate Render cron services scheduled at the same moment
("0 3 * * *" UTC) purely because Render has no free tier for cron at all —
each service is billed a ~$1/month minimum regardless of how little it
actually runs, so five services cost ~$5/month for a few minutes of total
work. Folding these four into one service (keeping the fifth, the hourly
CRM webhook drain, on its own schedule — collapsing that one to daily would
mean a missed webhook event waits up to 24h instead of 1h to get caught)
cuts that to two services.

Each agent runs as its own subprocess, not an in-process import, so one
agent crashing (an unhandled exception, even a segfault) can't take the
others down with it — the same isolation four separate cron services gave
for free. Every agent runs regardless of whether an earlier one failed;
the wrapper's own exit code is non-zero if any agent failed, so Render's
cron run history still shows a real failure rather than a false "success"
if one of the four had a problem.

Run:
    python scripts/run_morning_agents.py
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
    "agents/amocrm-followup/agent.py --sweep",
]


def run_agent(relative_command: str) -> int:
    """Run one agent as a subprocess and return its exit code.

    Args:
        relative_command: Script path, optionally followed by CLI args, e.g.
            "agents/amocrm-followup/agent.py --sweep".

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
    results: dict[str, int] = {}
    for command in AGENTS:
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
