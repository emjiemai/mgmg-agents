"""Agent — Garmin competitors in Uzbekistan, once a week (2026-10-19, the owner).

Who else sells Garmin here and for how much — OLX listings and Uzbek web shops
against our prices on garmin.com.uz (``integrations/garmin/competitors.py``) —
to everyone holding the ``garmin_manager`` role, in the 08:00 job on
``GARMIN_COMPETITORS_WEEKDAY`` (Thursday). ``/raqobat`` any time.

Run:
    python agents/garmin-competitors/agent.py            # on its weekday only (any other day: nothing)
    python agents/garmin-competitors/agent.py --force    # today, whatever the day
    python agents/garmin-competitors/agent.py --dry-run  # read the sources, print the counts, send nothing
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
from integrations.garmin import competitors  # noqa: E402
from integrations.org_bot import store  # noqa: E402

AGENT = competitors.AGENT
ROLE = "garmin_manager"
log = setup_logging(AGENT)


async def _sent_this_week() -> bool:
    row = await fetch_one(
        """SELECT 1 AS x FROM agent_actions WHERE agent = %s AND action = 'competitors_weekly' AND status = 'success'
           AND date_trunc('week', occurred_at AT TIME ZONE 'Asia/Tashkent') = date_trunc('week', now() AT TIME ZONE 'Asia/Tashkent')
           LIMIT 1""",
        (AGENT,),
    )
    return row is not None


async def run(force: bool = False, dry_run: bool = False) -> int:
    try:
        if dry_run:
            report = await competitors.collect(uuid.uuid4())
            print(f"OLX: {len(report.olx)} listings ({sum(1 for o in report.olx if o.state == 'new')} new), "
                  f"{len(report.cheaper)} cheaper than ours, {len(report.shops())} shops | web: {len(report.web)} "
                  f"({report.web_state}) | our prices: {report.ours.count if report.ours else 'not read'}")
            return 0
        if not settings.garmin_competitors_enabled:
            return 0
        if not force and today_local().weekday() != settings.garmin_competitors_weekday:
            return 0
        if settings.bots_frozen or (not force and await _sent_this_week()):
            return 0
        people = await store.active_employees_by_role(ROLE)
        if not people:
            return 0
        run_id = uuid.uuid4()
        sent = 0
        for person in people:
            try:
                # The first gets a fresh search; the rest the same results (no second search the same morning).
                await competitors.send(person["telegram_user_id"], run_id, fresh_web=sent == 0)
                sent += 1
            except Exception as exc:  # noqa: BLE001 — the next person still gets theirs
                log.error("Competitors not sent: {}", type(exc).__name__)
        await log_action(agent=AGENT, action="competitors_weekly", target_system="telegram", status="success",
                         run_id=run_id, mode="notify", payload={"people": sent})
        return 0
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Garmin competitors in Uzbekistan — weekly, to the Garmin manager.")
    parser.add_argument("--force", action="store_true", help="send today whatever the day")
    parser.add_argument("--dry-run", action="store_true", help="read the sources, print the counts, send nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.force, args.dry_run)))


if __name__ == "__main__":
    main()
