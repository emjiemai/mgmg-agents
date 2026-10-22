"""Agent — the report bonus, every morning (2026-10-22, the owner).

"Every day in the morning warn Garmin сотув employees about their report: one point each day, or not
(politely)." For yesterday's report of each person in ``REPORT_BONUS_ROLES`` (garmin_sotuv):

  * sent by them → +``REPORT_BONUS_POINTS`` (1) KPI, and a thank-you with the month's total;
  * missed, or put together by the bot from their messages («[чатдан тузилди]») → no point, said politely.

Once per report (``daily_reports.bonus_at``). The 22:00 minus for a missed report stays (report_close.py).
A day off or a day without an ask has no row — nothing is said.

Run:
    python agents/report-bonus/agent.py            # yesterday's
    python agents/report-bonus/agent.py --dry-run  # print the counts, change nothing
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
from integrations.common.db import close_pool, fetch_all, log_action  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402
from integrations.common.timeutil import today_local  # noqa: E402
from integrations.org_bot import names  # noqa: E402
from integrations.org_bot.tone import casual  # noqa: E402
from integrations.telegram.bot import TelegramBot, TelegramError, escape  # noqa: E402

AGENT = "report-bonus"
log = setup_logging(AGENT)


def roles() -> list[str]:
    return [r.strip() for r in settings.report_bonus_roles.split(",") if r.strip()]


def message(row: dict, points: int) -> str:
    """The polite morning line: the point and the month's total, or why there is none this time."""
    name, day = escape(names.call_name(row)), f"{row['report_date']:%d.%m}"
    if row["bonus_points"]:
        text = (f"{name}, кечаги, {day} ҳисоботингиз учун раҳмат каттакон, KPI баллингизга +{row['bonus_points']} балл "
                f"қўшилди, шу ойда жами +{row['month_bonus']} балл")
    elif row["status"] == "submitted":
        text = (f"{name}, кечаги, {day} ҳисоботингиз хабарларингиздан тузилгани учун бу сафар балл берилмади, кейинги сафар "
                f"ҳисоботни ўзингиз юборсангиз +{points} балл қўшилади")
    else:
        text = (f"{name}, кечаги, {day} ҳисоботингиз келмагани учун бу сафар балл берилмади, бугунги ҳисоботни соат 22:00 "
                f"гача юборсангиз эртага +{points} балл қўшилади, раҳмат")
    return casual(text, keep=[name, "KPI"])


async def run(dry_run: bool = False) -> int:
    try:
        day = today_local() - timedelta(days=1)
        points = max(0, settings.report_bonus_points)
        if dry_run:
            rows = await fetch_all(
                """SELECT r.status, r.auto_built FROM daily_reports r JOIN employees e ON e.id = r.employee_id
                   WHERE e.status = 'active' AND e.role = ANY(%s) AND r.report_date = %s AND r.bonus_at IS NULL""",
                (roles(), day))
            sent = sum(1 for r in rows if r["status"] == "submitted" and not r["auto_built"])
            print(f"{day}: {len(rows)} reports — {sent} get +{points}, {len(rows) - sent} none")
            return 0
        if settings.bots_frozen or not roles():
            return 0
        from integrations.org_bot import store

        rows = await store.settle_report_bonus(day, roles(), points)
        told = 0
        run_id = uuid.uuid4()
        if rows:
            async with TelegramBot(agent=AGENT, run_id=run_id,
                                   bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
                for row in rows:
                    try:
                        await bot.send_message(message(row, points), chat_id=str(row["telegram_user_id"]))
                        told += 1
                    except TelegramError as exc:
                        log.error("Report bonus message not sent: {}", type(exc).__name__)
        # Counts only — never who.
        await log_action(agent=AGENT, action="report_bonus", target_system="postgres", status="success", run_id=run_id,
                         mode="write", payload={"day": day.isoformat(), "rows": len(rows),
                                                "given": sum(1 for r in rows if r["bonus_points"]), "told": told})
        return 0
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="The report bonus — yesterday's reports, +1 or politely none.")
    parser.add_argument("--dry-run", action="store_true", help="print the counts, change nothing")
    sys.exit(asyncio.run(run(parser.parse_args().dry_run)))


if __name__ == "__main__":
    main()
