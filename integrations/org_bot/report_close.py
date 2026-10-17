"""22:00 — daily reports close; no report costs KPI points (2026-10-15, the Director).

The Director: reports until 22:00; whoever hasn't sent one gets minus points,
every day, and is told so. At 22:00 Tashkent every ask of the day still open
gets ``REPORT_MISS_PENALTY`` points (``daily_reports.penalty_points``, once —
``penalized_at``), which come off that month's KPI total (``kpi_score.py``),
and the person gets one polite line with the minus and the month's sum.

Runs inside the API service (``app.py`` checks every 5 minutes), so it needs
no cron of its own. People who don't report by agreement (📝 off) and days
off are never asked, so never here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local
from integrations.org_bot import kpi, names, store
from integrations.telegram.bot import TelegramBot, TelegramError

AGENT = "report-close"
# The first day that costs points: 15.10's 16:00 ask didn't say «22:00» yet, so nobody
# loses points for a rule they weren't told (asks from 16.10 say it).
PENALTY_FROM = date(2026, 10, 16)
log = setup_logging(AGENT)


async def close_day(now: datetime | None = None) -> int:
    """Close today's open asks once it's 22:00 or later; tell each person. How many were closed."""
    now = now or now_local()
    if now.hour < store.REPORT_CLOSE_HOUR:
        return 0
    points = max(0, settings.report_miss_penalty) if now.date() >= PENALTY_FROM else 0
    await store.expire_held_messages()  # untapped «ҳисобот / савол» questions close with the day
    rows = await store.close_missed_reports(now.date(), points)
    if not rows:
        return 0
    told = 0
    if points:
        run_id = uuid.uuid4()
        async with TelegramBot(agent=AGENT, run_id=run_id,
                               bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
            for row in rows:
                text = kpi.build_penalty_text(names.call_name(row), points, int(row["month_points"] or points))
                try:
                    await bot.send_message(text, chat_id=str(row["telegram_user_id"]))
                    told += 1
                except TelegramError as exc:
                    log.error("Could not tell someone about the missed report: {}", type(exc).__name__)
    # Counts only — never who.
    await log_action(agent=AGENT, action="reports_closed", target_system="postgres", status="success",
                     mode="write", payload={"day": now.date().isoformat(), "missed": len(rows), "points": points,
                                            "told": told})
    log.info("Closed {} missed report(s) for {}, {} told", len(rows), now.date(), told)
    return len(rows)
