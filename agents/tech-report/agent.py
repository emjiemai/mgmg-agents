"""Agent — IT's technical report, every morning after the 08:00 agents.

The Director's order of 07.10.2026: IT no longer gets the CEO brief or any
figures; it gets this instead — connections, SAP push, report delivery,
errors, database and security — counts, times and ok/failed only. The
content is built in ``integrations/org_bot/tech_report.py``; Admin Bot
``/texnik`` sends the same thing on demand.

Run:
    python agents/tech-report/agent.py            # send to the admin (Admin Bot)
    python agents/tech-report/agent.py --dry-run  # print, send nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# The folder name contains a hyphen, so this file cannot be imported as a
# package. Put the project root on sys.path and run it as a script instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common.config import settings
from integrations.common.db import close_pool
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import tech_report

AGENT = "tech-report"
log = setup_logging(AGENT)


async def run(dry_run: bool = False) -> int:
    """Send the report; 0 even when the admin chat isn't set (nothing to fix in the run itself)."""
    if dry_run:
        settings.dry_run = True
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0
    outcome = await tech_report.send(uuid.uuid4())
    log.info("Technical report: {}", outcome)
    return 1 if outcome == "failed" else 0


async def _main(dry_run: bool) -> int:
    try:
        return await run(dry_run)
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="IT's technical report (no figures)")
    parser.add_argument("--dry-run", action="store_true", help="print, send nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.dry_run)))


if __name__ == "__main__":
    main()
