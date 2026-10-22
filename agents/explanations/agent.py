"""Agent — explanations, every morning after the brief (2026-10-22, the Boss).

Asks whoever Verifix shows absent yesterday (with the working days in a row) and whoever's report
of yesterday didn't come why — politely, once (``integrations/org_bot/explanations.py``). Their
answer reaches the Director as a card (ops_manager). First it tells the Director who answered the
asks of the day before and who didn't.

Run:
    python agents/explanations/agent.py            # ask and summarise
    python agents/explanations/agent.py --dry-run  # print the counts, send nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

# Agents are run as scripts (see run_morning_agents.py): put the project root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common.config import settings  # noqa: E402
from integrations.common.db import close_pool, log_action  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402
from integrations.common.timeutil import today_local  # noqa: E402
from integrations.org_bot import explanations, names, store  # noqa: E402
from integrations.org_bot.report_calendar import TEAM_OF  # noqa: E402
from integrations.org_bot.roles import DIRECTOR_ROLE  # noqa: E402
from integrations.telegram.bot import TelegramBot  # noqa: E402

AGENT = "explanations"
log = setup_logging(AGENT)


async def collect(yesterday, employees, run_id) -> tuple[list[tuple[dict, str, list]], int]:
    """(person, kind, days) to ask about, and how many absent Verifix people have no bot account."""
    asks: list[tuple[dict, str, list]] = []
    absent: set = set()
    unmatched = 0
    if settings.verifix_configured:
        from integrations.verifix import attendance

        recs = await attendance.load(yesterday - timedelta(days=explanations.STREAK_DAYS), yesterday, run_id, AGENT)
        for _pid, (name, days) in explanations.absent_streaks(recs, yesterday).items():
            person = explanations.match_employee(name, employees)
            if person is None:
                unmatched += 1
                continue
            asks.append((person, "absent", days))
            absent.add(str(person["id"]))
    asked_ids = {str(e["id"]) for e in employees}
    for row in await store.missed_reports_of(yesterday):
        if str(row["id"]) in absent or str(row["id"]) not in asked_ids:
            continue
        asks.append((row, "report", [yesterday]))
    return asks, unmatched


async def _summaries(bot: TelegramBot, day) -> int:
    """Who answered the asks about ``day`` — to the Director (everyone) and the Garmin manager (his team)."""
    rows = await store.explanations_for(day)
    sent = 0
    for role, roles in ((DIRECTOR_ROLE, None), *((m, team) for m, team in TEAM_OF.items())):
        text = explanations.summary(day, [r for r in rows if roles is None or r["role"] in roles])
        if not text:
            continue
        for person in await store.active_employees_by_role(role):
            await bot.send_message(text, chat_id=str(person["telegram_user_id"]))
            sent += 1
    return sent


async def run(dry_run: bool = False) -> int:
    try:
        if not settings.explanations_enabled:
            return 0
        run_id = uuid.uuid4()
        today = today_local()
        yesterday = today - timedelta(days=1)
        employees = [e for e in await store.list_active_employees()
                     if e.get("role") not in explanations.NEVER_ASKED and e.get("telegram_user_id")]
        day_off = await store.is_day_off(yesterday)
        asks, unmatched = ([], 0) if day_off else await collect(yesterday, employees, run_id)
        if dry_run:
            print(f"asks: {len(asks)} ({sum(1 for a in asks if a[1] == 'absent')} absent, "
                  f"{sum(1 for a in asks if a[1] == 'report')} report), absent without a bot account: {unmatched}")
            return 0
        if settings.bots_frozen:
            return 0
        asked = 0
        async with TelegramBot(agent=AGENT, run_id=run_id,
                               bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
            await _summaries(bot, yesterday - timedelta(days=1))
            for person, kind, days in asks:
                row = await store.create_explanation(person, kind, yesterday, days)
                if row is None:  # already asked
                    continue
                try:
                    ids = await bot.send_message(explanations.ask_text(kind, names.call_name(person), days),
                                                 chat_id=str(person["telegram_user_id"]))
                    await store.set_explanation_prompt(str(row["id"]), ids[0] if ids else None)
                    asked += 1
                except Exception as exc:  # noqa: BLE001 — the next person is still asked
                    log.error("Explanation ask not sent: {}", type(exc).__name__)
        await log_action(agent=AGENT, action="explanations_asked", target_system="telegram", status="success",
                         run_id=run_id, mode="notify", payload={"asked": asked, "unmatched": unmatched})
        return 0
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Explanations — absent yesterday, report not sent.")
    parser.add_argument("--dry-run", action="store_true", help="print the counts, send nothing")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.dry_run)))


if __name__ == "__main__":
    main()
