"""Agent — daily report collection.

Asks every active employee (except the Director) what they did today, at 16:00
Asia/Tashkent — two hours before the office empties at 18:00 — and nudges
anyone still silent at 17:00.

The replies are not handled here: an employee answers OPS Manager Bot in
Telegram, so ``integrations/org_bot/ops_manager.py`` saves the answer to
``daily_reports`` and forwards it to the Director as it arrives. This agent
only opens the day's rows and sends the two outgoing messages, which is what
makes "who never answered" answerable at all — a row exists for everyone who
was asked, not just for whoever chose to write.

Which numbers each role reports (and the target for each) lives in
``integrations/org_bot/kpi.py``.

Run:
    python agents/daily-reports/agent.py --ask                # 16:00
    python agents/daily-reports/agent.py --remind             # 17:00
    python agents/daily-reports/agent.py --ask --dry-run      # print, send nothing
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
from integrations.common.timeutil import today_local
from integrations.org_bot import kpi, store
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.telegram.bot import TelegramBot, TelegramError

AGENT = "daily-reports"
log = setup_logging(AGENT)

# Only this bot's token is needed — the rest of the config (SAP, CRM, search
# APIs) belongs to other agents and must not block this one from running.
REQUIRED_SETTINGS = {"ops_manager_bot_telegram_bot_token"}


async def ask_everyone(run_id: uuid.UUID) -> int:
    """Open today's report row for each employee and send them the 16:00 ask.

    Idempotent per employee per day: ``store.open_report_request`` returns
    None for anyone already asked, so a retried cron run never double-asks.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        How many people were asked.
    """
    day = today_local()
    employees = [e for e in await store.list_active_employees() if e["role"] != DIRECTOR_ROLE]
    if not employees:
        log.warning("No active employees to ask (besides the Director)")
        return 0

    if settings.dry_run:
        # Deliberately before any write: opening rows would record everyone as
        # "asked" for a message a dry run never sends, and they would then be
        # counted as not having reported.
        for employee in employees:
            log.info("[dry run] would ask {} ({})", employee["display_name"], employee["role"])
        return 0

    asked = 0
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for employee in employees:
            report = await store.open_report_request(employee=employee, report_date=day)
            if report is None:
                log.info("{} was already asked today — skipping", employee["display_name"])
                continue

            metrics = kpi.metrics_for_role(employee["role"])
            text = kpi.build_request_text(employee["display_name"], metrics)
            try:
                message_ids = await bot.send_message(text, chat_id=str(employee["telegram_user_id"]))
            except TelegramError as exc:
                # The row stays 'asked', so the 17:00 reminder retries this
                # person and they still count as not having reported.
                log.error("Could not ask {}: {}", employee["display_name"], exc)
                continue

            if message_ids:
                await store.set_report_prompt_message_id(str(report["id"]), message_ids[0])
            asked += 1

    log.info("Asked {} of {} employee(s) for today's report", asked, len(employees))
    return asked


async def remind_silent(run_id: uuid.UUID) -> int:
    """Nudge everyone asked today who still hasn't answered.

    One nudge only — ``reminded_at`` is set as each goes out, so re-running
    this cannot turn into repeated pinging.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        How many reminders were sent.
    """
    day = today_local()
    pending = await store.reports_awaiting_reminder(day)
    if not pending:
        log.info("Everyone asked today has already reported — no reminders needed")
        return 0

    sent = 0
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for report in pending:
            text = kpi.build_reminder_text(kpi.metrics_for_role(report["role"]))
            try:
                await bot.send_message(text, chat_id=str(report["telegram_user_id"]))
            except TelegramError as exc:
                log.error("Could not remind {}: {}", report.get("display_name"), exc)
                continue
            await store.mark_report_reminded(str(report["id"]))
            sent += 1

    log.info("Reminded {} of {} employee(s) who hadn't reported", sent, len(pending))
    return sent


async def run(mode: str, dry_run: bool = False) -> int:
    """Run one mode of the agent.

    Args:
        mode: 'ask' or 'remind'.
        dry_run: Print who would be contacted instead of sending anything.

    Returns:
        Process exit code — 0 on success, 2 if config is incomplete.
    """
    if dry_run:
        settings.dry_run = True

    run_id = uuid.uuid4()
    log.info("Daily reports run {} starting (mode={}, dry_run={})", run_id, mode, settings.dry_run)

    unfilled = REQUIRED_SETTINGS & set(settings.missing_placeholders())
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(sorted(unfilled)))
        return 2

    # Paused, not failed: exit 0 so the cron run doesn't show as broken. A dry
    # run still goes ahead, so the agent can be checked while it's switched off.
    if not settings.daily_reports_enabled and not settings.dry_run:
        log.info("Daily reports are paused (DAILY_REPORTS_ENABLED is not true) — nothing sent")
        return 0

    # Same kill switch the bots and every other scheduled agent honour: when
    # frozen, nothing goes out and no rows are opened, so nobody is marked as
    # having missed a report they were never asked for.
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0

    if mode == "ask":
        await ask_everyone(run_id)
    else:
        await remind_silent(run_id)
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Collect employees' daily reports.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ask", action="store_true", help="16:00 — ask everyone for today's report")
    group.add_argument("--remind", action="store_true", help="17:00 — nudge whoever hasn't answered")
    parser.add_argument("--dry-run", action="store_true", help="send nothing")
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(_main("ask" if args.ask else "remind", args.dry_run))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


async def _main(mode: str, dry_run: bool) -> int:
    """Run the agent and close the database pool.

    Args:
        mode: 'ask' or 'remind'.
        dry_run: Passed through to ``run``.

    Returns:
        The process exit code.
    """
    try:
        return await run(mode, dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
