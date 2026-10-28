"""Agent — the B2B lead tracker's morning (2026-10-28, the Boss; integrations/org_bot/lead_tracker.py, spec 14).

08:00 (``--morning``, after the Lead Agent in the morning job): copies the leads sheet in, gives each B2B
sales person at work today one numbered lead, and tells the Boss who got which. Mondays: the Boss's
statistics and the PDF table. The 15:00 follow-up runs inside the API (its 5-minute round); ``--checkin``
asks now, by hand.

Nothing goes out while the tracker is stopped (/lidlar ⏸ — the Director's or the admin's), on a day off
(/dam), or with BOTS_FROZEN.

Run:
    python agents/lead-handout/agent.py --morning --dry-run    # who would get what
    python agents/lead-handout/agent.py --checkin              # the 15:00 questions, now
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
from integrations.common.timeutil import now_local
from integrations.org_bot import lead_tracker, leads, store

AGENT = leads.AGENT
log = setup_logging(AGENT)

REQUIRED_SETTINGS = {"ops_manager_bot_telegram_bot_token"}


async def run(mode: str, dry_run: bool = False) -> int:
    """Run ``mode`` ('morning' or 'checkin'). Returns the process exit code."""
    if dry_run:
        settings.dry_run = True
    run_id = uuid.uuid4()
    log.info("Lead tracker run {} starting (mode={}, dry_run={})", run_id, mode, settings.dry_run)
    unfilled = REQUIRED_SETTINGS & set(settings.missing_placeholders())
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(sorted(unfilled)))
        return 2
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0
    today = now_local().date()
    if await store.is_day_off(today):
        log.info("Today is a day off (Admin Bot /dam) — no leads and no 15:00 question")
        return 0
    if await lead_tracker.is_on() or settings.dry_run:
        await (lead_tracker.morning(run_id) if mode == "morning" else lead_tracker.checkin(run_id))
    else:
        log.info("The lead tracker is stopped (/lidlar) — no leads handed out")
    if mode == "morning" and today.weekday() == 0:  # Mondays: the Boss's statistics, on or off
        await lead_tracker.weekly(run_id)
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="The B2B lead tracker: hand out leads, ask how they went.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--morning", action="store_true", help="08:00 — one lead to each sales person")
    group.add_argument("--checkin", action="store_true", help="the 15:00 questions, now")
    parser.add_argument("--dry-run", action="store_true", help="print, send nothing")
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(_main("morning" if args.morning else "checkin", args.dry_run))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


async def _main(mode: str, dry_run: bool) -> int:
    try:
        return await run(mode, dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
