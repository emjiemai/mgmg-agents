"""Agent A3 — task tracker: deadline reminders, overdue notices, Friday scorecard.

From the owner's plan (ЭМЖИЕМ AI Агентлар Тизими): tasks are handed out and
then silently stall, because nobody is reminded. This agent does the
reminding so the Director doesn't have to.

  --morning (08:00, with the other morning agents)
      * Employees whose open task is due today or tomorrow get one reminder.
      * Tasks that just went past their deadline: the employee is told once,
        and the Director gets one message listing all of them.
  --weekly (Friday 17:00, with the report reminder)
      * The Director gets the week's scorecard: tasks done on time (A3's И),
        daily reports sent and on time (A1's И), and written permissions.
        Sent only on Fridays, so the evening job can run it every weekday.

Deadlines come only from the Director (stated in the task, or picked with one
tap) — see integrations/org_bot/task_tracker.py. A task without a deadline is
never nagged about.

Run:
    python agents/task-tracker/agent.py --morning
    python agents/task-tracker/agent.py --weekly
    python agents/task-tracker/agent.py --weekly --force    # any weekday
    python agents/task-tracker/agent.py --morning --dry-run # print, send nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

# The folder name contains a hyphen, so this file cannot be imported as a
# package. Put the project root on sys.path and run it as a script instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common.config import settings
from integrations.common.db import close_pool
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local
from integrations.org_bot import store, task_tracker
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.telegram.bot import TelegramBot, TelegramError

AGENT = "task-tracker"
log = setup_logging(AGENT)

REQUIRED_SETTINGS = {"ops_manager_bot_telegram_bot_token"}

FRIDAY = 4


def _bot(run_id: uuid.UUID) -> TelegramBot:
    """OPS Manager Bot — the chat every employee and the Director already use."""
    return TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    )


async def morning(run_id: uuid.UUID) -> None:
    """Send due-soon reminders and overdue notices, each at most once per task."""
    today = today_local()
    # Weekends: nobody is at work to act on a reminder. A deadline that passes
    # on Saturday is reported on Monday instead (the overdue query is "< today").
    if today.weekday() >= 5:
        log.info("Weekend — no task reminders today")
        return

    due_soon = await store.tasks_needing_reminder(today)
    overdue = await store.tasks_newly_overdue(today)
    log.info("{} task(s) due soon, {} newly overdue", len(due_soon), len(overdue))
    if settings.dry_run:
        for task in due_soon:
            log.info("[dry-run] remind {}: {}", task["display_name"], task["task_summary"][:60])
        for task in overdue:
            log.info("[dry-run] overdue {}: {}", task["display_name"], task["task_summary"][:60])
        return

    async with _bot(run_id) as bot:
        for task in due_soon:
            try:
                await bot.send_message(
                    task_tracker.reminder_text(task, today), chat_id=str(task["employee_telegram_user_id"])
                )
            except TelegramError as exc:
                log.error("Could not remind {}: {}", task["display_name"], exc)
                continue
            await store.mark_task_reminded(str(task["id"]))

        by_director: dict[int, list[dict]] = defaultdict(list)
        for task in overdue:
            try:
                await bot.send_message(
                    task_tracker.overdue_employee_text(task), chat_id=str(task["employee_telegram_user_id"])
                )
            except TelegramError as exc:
                log.warning("Could not tell {} their task is overdue: {}", task["display_name"], exc)
            by_director[task["director_telegram_user_id"]].append(task)

        for director_id, tasks in by_director.items():
            try:
                await bot.send_message(task_tracker.overdue_director_text(tasks), chat_id=str(director_id))
            except TelegramError as exc:
                # Not marked notified, so tomorrow's run tries again rather
                # than the Director silently never hearing about it.
                log.error("Could not send the overdue list to the Director {}: {}", director_id, exc)
                continue
            for task in tasks:
                await store.mark_task_overdue_notified(str(task["id"]))


async def weekly(run_id: uuid.UUID, force: bool = False) -> None:
    """Send the Friday scorecard to every Director."""
    today = today_local()
    if today.weekday() != FRIDAY and not force:
        log.info("Not Friday — no weekly scorecard")
        return

    start = today - timedelta(days=today.weekday())  # Monday
    tasks = task_tracker.score_tasks(await store.tasks_due_between(start, today), start, today)
    reports = (
        task_tracker.score_reports(await store.reports_between(start, today))
        if settings.daily_reports_enabled
        else None
    )
    permissions = await store.permission_counts_between(start, today) if settings.permissions_enabled else None
    text = task_tracker.weekly_text(start, today, tasks, reports, permissions)

    directors = await store.active_employees_by_role(DIRECTOR_ROLE)
    if settings.dry_run:
        log.info("[dry-run] weekly scorecard for {} director(s):\n{}", len(directors), text)
        return

    async with _bot(run_id) as bot:
        for director in directors:
            try:
                await bot.send_message(text, chat_id=str(director["telegram_user_id"]))
            except TelegramError as exc:
                log.error("Could not send the weekly scorecard to {}: {}", director["display_name"], exc)
    log.info("Weekly scorecard sent to {} director(s)", len(directors))


async def run(mode: str, dry_run: bool = False, force: bool = False) -> int:
    """Run one mode of the agent.

    Returns:
        Process exit code — 0 on success, 2 if config is incomplete.
    """
    if dry_run:
        settings.dry_run = True

    run_id = uuid.uuid4()
    log.info("Task tracker run {} starting (mode={}, dry_run={})", run_id, mode, settings.dry_run)

    unfilled = REQUIRED_SETTINGS & set(settings.missing_placeholders())
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(sorted(unfilled)))
        return 2
    if not settings.task_tracker_enabled and not settings.dry_run:
        log.info("Task tracker is paused (TASK_TRACKER_ENABLED is not true) — nothing sent")
        return 0
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0

    if mode == "morning":
        await morning(run_id)
    else:
        await weekly(run_id, force)
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Task deadlines, reminders and the weekly scorecard.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--morning", action="store_true", help="08:00 — reminders and overdue notices")
    group.add_argument("--weekly", action="store_true", help="Friday 17:00 — the Director's scorecard")
    parser.add_argument("--force", action="store_true", help="send the weekly scorecard on any weekday")
    parser.add_argument("--dry-run", action="store_true", help="send nothing")
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(_main("morning" if args.morning else "weekly", args.dry_run, args.force))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


async def _main(mode: str, dry_run: bool, force: bool) -> int:
    """Run the agent and close the database pool."""
    try:
        return await run(mode, dry_run, force)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
