"""Agent — the report bonus, every morning (2026-10-22, the owner).

"Every day in the morning warn Garmin сотув employees about their report: one point each day, or not
(politely)." For yesterday's report of each person in ``REPORT_BONUS_ROLES`` (garmin_sotuv):

  * sent by them → +``REPORT_BONUS_POINTS`` (1) KPI, and a thank-you with the month's total;
  * missed, or put together by the bot from their messages («[чатдан тузилди]») → no point, said politely.

Once per report (``daily_reports.bonus_at``). The 22:00 minus for a missed report stays (report_close.py).
A day off or a day without an ask has no row — nothing is said; an excused day is left out.

2026-10-24: not sent from here — a note for agents/morning-notes, which sends each person one morning message
(a missed report's «no point» goes inside the «where's yesterday's report?» question, never twice).

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
from integrations.org_bot import morning_notes, names  # noqa: E402
from integrations.org_bot.tone import casual  # noqa: E402
from integrations.telegram.bot import escape  # noqa: E402

AGENT = "report-bonus"
log = setup_logging(AGENT)


def roles() -> list[str]:
    return [r.strip() for r in settings.report_bonus_roles.split(",") if r.strip()]


def detail(row: dict, points: int) -> dict:
    """The note for the morning message (morning_notes.bonus_line words it)."""
    return {"report_date": row["report_date"].isoformat(), "points": int(row["bonus_points"] or 0),
            "month": int(row["month_bonus"] or 0), "status": row["status"], "auto": bool(row["auto_built"]),
            "per": points}


def message(row: dict, points: int) -> str:
    """The polite morning line on its own: the point and the month's total, or why there is none this time."""
    name = escape(names.call_name(row))
    return casual(f"{name}, {morning_notes.bonus_line(detail(row, points))}", keep=[name, "KPI"])


async def run(dry_run: bool = False) -> int:
    try:
        day = today_local() - timedelta(days=1)
        points = max(0, settings.report_bonus_points)
        if dry_run:
            rows = await fetch_all(
                """SELECT r.status, r.auto_built FROM daily_reports r JOIN employees e ON e.id = r.employee_id
                   WHERE e.status = 'active' AND e.role = ANY(%s) AND r.report_date = %s AND r.bonus_at IS NULL
                     AND r.status <> 'excused'""",
                (roles(), day))
            sent = sum(1 for r in rows if r["status"] == "submitted" and not r["auto_built"])
            print(f"{day}: {len(rows)} reports — {sent} get +{points}, {len(rows) - sent} none")
            return 0
        if settings.bots_frozen or not roles():
            return 0
        from integrations.org_bot import store

        rows = await store.settle_report_bonus(day, roles(), points)
        noted = 0
        run_id = uuid.uuid4()
        for row in rows:  # sent with the person's other morning news (agents/morning-notes)
            try:
                await store.add_morning_note(row, today_local(), "bonus", detail(row, points))
                noted += 1
            except Exception as exc:  # noqa: BLE001 — the next person's note still goes in
                log.error("Report bonus note not saved: {}", type(exc).__name__)
        # Counts only — never who.
        await log_action(agent=AGENT, action="report_bonus", target_system="postgres", status="success", run_id=run_id,
                         mode="write", payload={"day": day.isoformat(), "rows": len(rows),
                                                "given": sum(1 for r in rows if r["bonus_points"]), "noted": noted})
        return 0
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="The report bonus — yesterday's reports, +1 or politely none.")
    parser.add_argument("--dry-run", action="store_true", help="print the counts, change nothing")
    sys.exit(asyncio.run(run(parser.parse_args().dry_run)))


if __name__ == "__main__":
    main()
