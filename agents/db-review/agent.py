"""Agent — the bot's own database, reviewed every morning (2026-10-17).

Checks only the bot's own tables (never SAP, 1C, Billz, Verifix): stale held
messages, removed people's open work, duplicate or test rows, questions saved
as reports… Safe ones are settled with an UPDATE (never a DELETE); the rest
go to IT in Admin Bot, counts only. The checks: ``integrations/org_bot/db_review.py``.

Run:
    python agents/db-review/agent.py            # check, fix the safe ones, tell IT
    python agents/db-review/agent.py --dry-run  # check only, print, change and send nothing
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

from integrations.common.config import settings  # noqa: E402
from integrations.common.db import close_pool  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402
from integrations.org_bot import db_review  # noqa: E402

AGENT = "db-review"
log = setup_logging(AGENT)


async def run(dry_run: bool = False) -> int:
    try:
        if dry_run:
            print(db_review.text(await db_review.review(fix=False)))
            return 0
        if settings.bots_frozen:
            log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
            return 0
        await db_review.send(uuid.uuid4())
        return 0
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Review the bot's own database tables.")
    parser.add_argument("--dry-run", action="store_true", help="check only: change and send nothing")
    sys.exit(asyncio.run(run(parser.parse_args().dry_run)))


if __name__ == "__main__":
    main()
