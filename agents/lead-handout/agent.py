"""Agent — hand the Lead Agent's leads to B2B sales, and ask how they're going.

08:00 (``--morning``, after the Lead Agent in the morning job): copies the
leads sheet into ``leads``, then gives each B2B sales person at work today
one lead — today's new ones first, then the best older ones nobody has
worked yet — as a card with a short Uzbek summary.

15:00 (``--checkin``, in the daytime job): for every lead still open, one
friendly line with жараёнда / рад этилди / бажарилди. The taps and the
follow-up answer are handled by OPS Manager Bot; KPI counts them. The rules
and wording are in ``integrations/org_bot/leads.py``.

Run:
    python agents/lead-handout/agent.py --morning --dry-run    # who would get what
    python agents/lead-handout/agent.py --checkin              # 15:00 (skips outside 15:00–15:25)
    python agents/lead-handout/agent.py --checkin --force      # ask now, whatever the time
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

# The folder name contains a hyphen, so this file cannot be imported as a
# package. Put the project root on sys.path and run it as a script instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.ai.openrouter_client import OpenRouterClient
from integrations.common.config import settings
from integrations.common.db import close_pool
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local
from integrations.google.sheets_client import SheetsClient, SheetsError
from integrations.org_bot import kpi, leads, names, store
from integrations.telegram.bot import TelegramBot, TelegramError

AGENT = leads.AGENT
log = setup_logging(AGENT)

REQUIRED_SETTINGS = {"ops_manager_bot_telegram_bot_token"}


async def import_sheet(run_id: uuid.UUID) -> None:
    """Copy new rows of the leads sheet into ``leads``; an unreadable sheet leaves what's already there."""
    try:
        async with SheetsClient(agent=AGENT, run_id=run_id) as sheets:
            rows = await sheets.get_values(leads.SHEET_RANGE)
    except SheetsError as exc:
        log.warning("Could not read the leads sheet, handing out from what's already imported: {}", exc)
        return
    added = await store.upsert_leads(leads.rows_to_leads(rows))
    log.info("Leads sheet: {} new lead(s) imported", added)


async def write_brief(ai: OpenRouterClient | None, lead: dict[str, Any]) -> tuple[str, str]:
    """(summary, location) for the card: kept from before, the AI's, or plain words."""
    if lead.get("brief"):
        return lead["brief"], ""
    parsed = None
    if ai is not None:
        try:
            parsed = leads.parse_brief(await ai.complete(leads.BRIEF_SYSTEM, leads.brief_prompt(lead), json_mode=True))
        except Exception as exc:  # noqa: BLE001 — the card goes out with plain words instead
            log.warning("AI could not summarise lead {}: {}", lead.get("id"), exc)
    if parsed is None:
        return leads.fallback_brief(lead), ""
    return parsed


async def morning(run_id: uuid.UUID) -> int:
    """One lead to each B2B sales person at work today. Returns how many were handed out."""
    day = now_local().date()
    await import_sheet(run_id)
    people = [e for e in await store.active_employees_by_role(leads.SALES_ROLE) if kpi.works_on(e, day)]
    if not people:
        log.info("No B2B sales person at work today — no leads handed out")
        return 0
    plan = leads.plan_handout(people, await store.free_leads(), day)
    if len(plan) < len(people):
        log.warning("{} lead(s) for {} sales people — the rest get none today", len(plan), len(people))

    if settings.dry_run:
        for person, lead in plan:
            log.info("[dry run] {} ← {} ({}, {})", names.call_name(person), leads.lead_name(lead),
                     lead.get("priority"), lead.get("date_added"))
        return 0

    given = 0
    async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai, TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for person, lead in plan:
            assignment = await store.create_lead_assignment(lead["id"], str(person["id"]), day)
            if assignment is None:
                log.info("{} already has today's lead, or the lead was taken — skipping", person["display_name"])
                continue
            brief, location = await write_brief(ai, lead)
            if not lead.get("brief"):
                await store.set_lead_brief(lead["id"], brief)
            try:
                ids = await bot.send_message(leads.card_text(lead, brief, location), chat_id=str(person["telegram_user_id"]))
            except TelegramError as exc:
                log.error("Could not send {} their lead: {}", person["display_name"], exc)
                continue
            await store.set_lead_assignment_message(str(assignment["id"]), ids[-1] if ids else None)
            given += 1
    log.info("Handed out {} lead(s) to {} sales people", given, len(people))
    return given


async def checkin(run_id: uuid.UUID) -> int:
    """15:00: ask about every open lead of everyone at work today. Returns how many were asked."""
    day = now_local().date()
    open_leads = [row for row in await store.open_leads_to_ask(day) if kpi.works_on(row, day)]
    if not open_leads:
        log.info("No open leads to ask about")
        return 0
    if settings.dry_run:
        for row in open_leads:
            log.info("[dry run] {}", leads.checkin_text(names.call_name(row), row, (day - row["assigned_on"]).days + 1))
        return 0

    asked = 0
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for row in open_leads:
            opened = await store.create_lead_checkin(str(row["assignment_id"]), day, row["telegram_user_id"])
            if opened is None:
                continue  # already asked today (a retried run)
            text = leads.checkin_text(names.call_name(row), row, (day - row["assigned_on"]).days + 1)
            try:
                ids = await bot.send_message(
                    text, chat_id=str(row["telegram_user_id"]), reply_markup=leads.checkin_keyboard(str(opened["id"]))
                )
            except TelegramError as exc:
                log.error("Could not ask about lead {}: {}", row["assignment_id"], exc)
                continue
            await store.set_lead_checkin_message(str(opened["id"]), ids[-1] if ids else None)
            asked += 1
    log.info("Asked about {} open lead(s)", asked)
    return asked


async def run(mode: str, dry_run: bool = False, force: bool = False) -> int:
    """Run ``mode`` ('morning' or 'checkin'). Returns the process exit code."""
    if dry_run:
        settings.dry_run = True
    run_id = uuid.uuid4()
    if mode == "checkin" and not force and not leads.checkin_due(now_local()):
        log.info("Not 15:00 ({:%H:%M}) — no lead check-in this run", now_local())
        return 0
    log.info("Lead hand-out run {} starting (mode={}, dry_run={})", run_id, mode, settings.dry_run)

    unfilled = REQUIRED_SETTINGS & set(settings.missing_placeholders())
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(sorted(unfilled)))
        return 2
    if not settings.lead_handout_enabled and not settings.dry_run:
        log.info("Lead hand-out is paused (LEAD_HANDOUT_ENABLED=false) — nothing sent")
        return 0
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0
    # A day off set by the admin (/dam): nothing goes to employees.
    if await store.is_day_off(now_local().date()):
        log.info("Today is a day off (Admin Bot /dam) — no leads and no 15:00 question")
        return 0

    await (morning(run_id) if mode == "morning" else checkin(run_id))
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Hand leads to B2B sales and ask how they're going.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--morning", action="store_true", help="08:00 — one lead to each sales person")
    group.add_argument("--checkin", action="store_true", help="15:00 — how is each open lead going")
    parser.add_argument("--force", action="store_true", help="check in now, whatever the time")
    parser.add_argument("--dry-run", action="store_true", help="print, send nothing")
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(_main("morning" if args.morning else "checkin", args.dry_run, args.force))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


async def _main(mode: str, dry_run: bool, force: bool) -> int:
    try:
        return await run(mode, dry_run, force)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
