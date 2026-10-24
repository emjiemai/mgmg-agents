"""Agent — each employee's one morning message (2026-10-24, the owner: "it might ask multiple questions at once").

Runs right after the 08:00 agents that leave notes (report-bonus, explanations, task-tracker --morning) and sends
every person their notes as ONE message: the facts as short lines, at most one question last, with its button
(``integrations/org_bot/morning_notes.py``). Counts-only logs. If this run dies, the API sends what is left
half an hour later.

Run:
    python agents/morning-notes/agent.py            # send today's notes
    python agents/morning-notes/agent.py --dry-run  # print the counts, send nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# Agents are run as scripts (see run_morning_agents.py): put the project root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common.db import close_pool, fetch_all  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402
from integrations.common.timeutil import today_local  # noqa: E402
from integrations.org_bot import morning_notes  # noqa: E402

AGENT = "morning-notes"
log = setup_logging(AGENT)


async def run(dry_run: bool = False) -> int:
    try:
        today = today_local()
        if dry_run:
            rows = await fetch_all(
                "SELECT employee_id, kind FROM morning_notes WHERE day = %s AND sent_at IS NULL", (today,))
            people = {str(r["employee_id"]) for r in rows}
            print(f"{today}: {len(rows)} note(s) for {len(people)} person(s), "
                  f"{sum(1 for r in rows if r['kind'] == 'ask')} question(s)")
            return 0
        counts = await morning_notes.send_day(today, uuid.uuid4())
        log.info("Morning messages: {} told, {} failed, {} skipped", counts["told"], counts["failed"], counts["skipped"])
        return 0
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Each employee's one morning message.")
    parser.add_argument("--dry-run", action="store_true", help="print the counts, send nothing")
    sys.exit(asyncio.run(run(parser.parse_args().dry_run)))


if __name__ == "__main__":
    main()
