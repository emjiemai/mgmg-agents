"""Agent — old stock, every Monday 08:00 (2026-10-17, the owner).

Goods a year or more in the warehouse, and those within a month of it, from
SAP's push (``integrations/org_bot/old_stock.py``): the Director gets every
warehouse, the Garmin manager the Garmin warehouses. Once a week.

Run:
    python agents/old-stock/agent.py            # Mondays only (any other day: nothing)
    python agents/old-stock/agent.py --force    # today, whatever the day
    python agents/old-stock/agent.py --dry-run  # print the counts, send nothing
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
from integrations.common.db import close_pool, fetch_one, log_action  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402
from integrations.common.timeutil import today_local  # noqa: E402
from integrations.org_bot import old_stock, store  # noqa: E402
from integrations.org_bot.roles import DIRECTOR_ROLE  # noqa: E402

AGENT = "old-stock"
log = setup_logging(AGENT)


async def _sent_this_week() -> bool:
    row = await fetch_one(
        """SELECT 1 AS x FROM agent_actions WHERE agent = %s AND action = 'old_stock_weekly' AND status = 'success'
           AND date_trunc('week', occurred_at AT TIME ZONE 'Asia/Tashkent') = date_trunc('week', now() AT TIME ZONE 'Asia/Tashkent')
           LIMIT 1""",
        (AGENT,),
    )
    return row is not None


async def run(force: bool = False, dry_run: bool = False) -> int:
    try:
        if dry_run:
            old, soon, _ = await old_stock.load()
            g_old, g_soon, _ = await old_stock.load(old_stock.garmin_codes())
            print(f"all: {len(old)} old, {len(soon)} soon | Garmin: {len(g_old)} old, {len(g_soon)} soon")
            return 0
        if not force and today_local().weekday() != 0:
            return 0
        if settings.bots_frozen or (not force and await _sent_this_week()):
            return 0
        run_id = uuid.uuid4()
        sent = 0
        for role, codes in ((DIRECTOR_ROLE, None), ("garmin_manager", old_stock.garmin_codes())):
            for person in await store.active_employees_by_role(role):
                try:
                    await old_stock.send(person["telegram_user_id"], codes, run_id)
                    sent += 1
                except Exception as exc:  # noqa: BLE001 — the next person still gets theirs
                    log.error("Old stock list not sent: {}", type(exc).__name__)
        await log_action(agent=AGENT, action="old_stock_weekly", target_system="telegram", status="success",
                         run_id=run_id, mode="notify", payload={"people": sent})
        return 0
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Old stock — a year or more in the warehouse.")
    parser.add_argument("--force", action="store_true", help="send today whatever the day")
    parser.add_argument("--dry-run", action="store_true", help="print the counts, send nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.force, args.dry_run)))


if __name__ == "__main__":
    main()
