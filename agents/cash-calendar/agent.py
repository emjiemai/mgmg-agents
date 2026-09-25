"""Agent B2 — the 30-day cash calendar: what money comes in and goes out, and when.

From the owner's plan (ЭМЖИЕМ AI Агентлар Тизими): there is no 30-day cash
plan, so shortfalls are found when they happen. Every Monday the Director and
the accountants get the next 30 days in four blocks:

  ⬇️ in  — open SAP invoices by due date (what customers should pay), plus
           what is already overdue and still unpaid
  ⬆️ out — approved written payments (B1) by their "Бажариш муддати"

What it deliberately does not claim:
  * no balance line: cash isn't connected, so this is a plan of movements,
    not a forecast of the account;
  * salaries and payments made without a written approval aren't in it —
    the payment gate is what makes the "out" side complete;
  * an approval whose date can't be read is counted as "сана аниқ эмас",
    never put on a guessed day;
  * a capped invoice feed makes the "in" side a lower bound ("камида").

Run:
    python agents/cash-calendar/agent.py            # Mondays only (the 08:00 job runs it daily)
    python agents/cash-calendar/agent.py --force    # any day
    python agents/cash-calendar/agent.py --dry-run  # print, send nothing
The Director can also ask OPS Manager Bot ("pul kalendari") any day.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# The folder name contains a hyphen, so this file cannot be imported as a
# package. Put the project root on sys.path and run it as a script instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common.config import settings
from integrations.common.db import close_pool, fetch_all, fetch_one
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_money_by_currency
from integrations.common.timeutil import today_local
from integrations.org_bot import permissions, store
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.sap import figures
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "cash-calendar"
log = setup_logging(AGENT)

MONDAY = 0
HORIZON_DAYS = 30
# Four blocks: three weeks, then the rest of the 30 days.
BLOCK_STARTS = (0, 7, 14, 21)
MAX_PAYMENT_LINES = 8
ACCOUNTANT_ROLE = "buxgalteriya"


@dataclass
class Block:
    """One stretch of days and the money due in it."""

    start: date
    end: date
    totals: dict[str, int] = field(default_factory=dict)
    count: int = 0


@dataclass
class Calendar:
    """The next 30 days of expected money movements."""

    start: date
    end: date
    incoming: list[Block] = field(default_factory=list)
    overdue: dict[str, int] = field(default_factory=dict)
    overdue_count: int = 0
    outgoing: list[dict[str, Any]] = field(default_factory=list)  # {"day", "totals", "subject", "no"}
    outgoing_totals: dict[str, int] = field(default_factory=dict)
    unclear: int = 0
    invoices_capped: bool = False


def _add(totals: dict[str, int], currency: str, amount: int) -> None:
    totals[currency] = totals.get(currency, 0) + amount


def build(invoices: list[dict[str, Any]], payments: list[dict[str, Any]], today: date, capped: bool) -> Calendar:
    """Lay invoices and approved payments out over the next 30 days.

    Args:
        invoices: Open invoices (``due_date``, ``balance_due_tiyin``, ``currency``).
        payments: Approved written requests with an amount (``store.approved_payment_requests``).
        today: First day of the window.
        capped: The invoice push hit its row limit.

    Returns:
        The calendar.
    """
    end = today + timedelta(days=HORIZON_DAYS - 1)
    cal = Calendar(start=today, end=end, invoices_capped=capped)
    for index, offset in enumerate(BLOCK_STARTS):
        block_start = today + timedelta(days=offset)
        next_offset = BLOCK_STARTS[index + 1] if index + 1 < len(BLOCK_STARTS) else HORIZON_DAYS
        cal.incoming.append(Block(start=block_start, end=today + timedelta(days=next_offset - 1)))

    for invoice in invoices:
        due = invoice.get("due_date")
        amount = int(invoice.get("balance_due_tiyin") or 0)
        currency = invoice.get("currency") or settings.sap_default_currency
        if due is None or amount <= 0:
            continue
        if due < today:
            _add(cal.overdue, currency, amount)
            cal.overdue_count += 1
            continue
        for block in cal.incoming:
            if block.start <= due <= block.end:
                _add(block.totals, currency, amount)
                block.count += 1
                break

    for request in payments:
        due = permissions.due_day(request)
        if due is None:
            cal.unclear += 1
            continue
        if not (today <= due <= end):
            continue
        currency = request.get("currency") or "UZS"
        amount = int(request["amount_tiyin"])
        cal.outgoing.append(
            {"day": due, "totals": {currency: amount}, "subject": request.get("subject") or "—", "no": request.get("request_no")}
        )
        _add(cal.outgoing_totals, currency, amount)
    cal.outgoing.sort(key=lambda p: p["day"])
    return cal


def _money(totals: dict[str, int]) -> str:
    return format_money_by_currency([(amount, currency) for currency, amount in totals.items()])


def _short(text: str, limit: int = 40) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def render(cal: Calendar) -> str:
    """The Monday message."""
    lower = "камида " if cal.invoices_capped else ""
    lines = [
        f"📅 <b>30 кунлик пул календари — {cal.start:%d.%m}–{cal.end:%d.%m.%Y}</b>",
        "",
        "⬇️ <b>Келиши кутилаётган</b> <i>(очиқ ҳисоб-фактуралар, тўлов муддати бўйича)</i>",
    ]
    for block in cal.incoming:
        span = f"{block.start:%d.%m}–{block.end:%d.%m}"
        value = f"{lower}{escape(_money(block.totals))} ({block.count} та)" if block.count else "—"
        lines.append(f"   {span}: {value}")
    if cal.overdue_count:
        lines.append(f"   ⚠️ Муддати ўтган, ҳали тушмаган: {lower}{escape(_money(cal.overdue))} ({cal.overdue_count} та)")

    lines += ["", "⬆️ <b>Кетадиган</b> <i>(тасдиқланган ёзма рухсатлар)</i>"]
    if cal.outgoing:
        for payment in cal.outgoing[:MAX_PAYMENT_LINES]:
            number = f" ({escape(payment['no'])})" if payment.get("no") else ""
            lines.append(
                f"   {payment['day']:%d.%m} — {escape(_money(payment['totals']))} — {escape(_short(payment['subject']))}{number}"
            )
        if len(cal.outgoing) > MAX_PAYMENT_LINES:
            lines.append(f"   <i>+яна {len(cal.outgoing) - MAX_PAYMENT_LINES} та</i>")
        lines.append(f"   Жами: {escape(_money(cal.outgoing_totals))}")
    else:
        lines.append("   —")
    if cal.unclear:
        lines.append(f"   ❔ Санаси аниқ эмас: {cal.unclear} та")

    lines.append("")
    lines.append(
        "<i>Касса қолдиғи уланмаган — бу фақат кирим ва чиқим режаси. "
        "Иш ҳақи ва ёзма рухсатсиз тўловлар бу ерда йўқ.</i>"
    )
    if cal.invoices_capped:
        lines.append("<i>SAP'дан чекланган миқдордаги ҳисоб-фактура келди — кирим тўлиқ эмас.</i>")
    return "\n".join(lines)


def describe(cal: Calendar) -> str:
    """Plain-text data for OPS Manager Bot's answers ("pul kalendari")."""
    lines = [f"Cash calendar {cal.start.isoformat()}..{cal.end.isoformat()} (no cash balance: not connected)."]
    lines.append("INCOMING = open SAP invoices by due date" + (" (LOWER BOUND: invoice feed capped)" if cal.invoices_capped else "") + ":")
    for block in cal.incoming:
        lines.append(f"- {block.start.isoformat()}..{block.end.isoformat()}: {_money(block.totals) if block.count else 'none'} ({block.count} invoice(s))")
    lines.append(f"- already overdue, still unpaid: {_money(cal.overdue) if cal.overdue_count else 'none'} ({cal.overdue_count})")
    lines.append("OUTGOING = approved written payment requests by their execution date:")
    for payment in cal.outgoing:
        lines.append(f"- {payment['day'].isoformat()}: {_money(payment['totals'])} — {payment['subject']} ({payment.get('no')})")
    if not cal.outgoing:
        lines.append("- none scheduled in the window")
    lines.append(f"Approved payments whose date can't be read: {cal.unclear}.")
    lines.append("Salaries and payments made without a written approval are NOT included.")
    return "\n".join(lines)


async def load(today: date) -> Calendar:
    """Read invoices and approved payments and build the calendar."""
    invoices = await fetch_all("SELECT due_date, balance_due_tiyin, currency FROM v_ar_aging_latest")
    payments = await store.approved_payment_requests(days=120)
    row = await fetch_one(
        "SELECT payload FROM agent_actions WHERE agent = 'sap-gateway-push' AND action = 'ar_aging_push' "
        "ORDER BY occurred_at DESC LIMIT 1"
    )
    received = None
    if row is not None:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
        received = payload.get("rows_received")
    return build(invoices, payments, today, figures.is_capped("invoices", received))


async def send(run_id: uuid.UUID) -> int:
    """Send the calendar to the Director(s) and the accountants."""
    text = render(await load(today_local()))
    recipients = {
        e["telegram_user_id"]: e
        for role in (DIRECTOR_ROLE, ACCOUNTANT_ROLE)
        for e in await store.active_employees_by_role(role)
    }
    if settings.dry_run:
        print(text)
        log.info("[dry-run] would send to {} person(s)", len(recipients))
        return 0
    sent = 0
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for telegram_user_id in recipients:
            try:
                await bot.send_message(text, chat_id=str(telegram_user_id))
                sent += 1
            except TelegramError as exc:
                log.error("Could not send the cash calendar to {}: {}", telegram_user_id, exc)
    log.info("Cash calendar sent to {} person(s)", sent)
    return sent


async def run(force: bool = False, dry_run: bool = False) -> int:
    """Weekly entry point (Mondays unless forced)."""
    if dry_run:
        settings.dry_run = True
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0
    if not settings.cash_calendar_enabled and not settings.dry_run:
        log.info("Cash calendar is paused (CASH_CALENDAR_ENABLED is not true)")
        return 0
    if today_local().weekday() != MONDAY and not force:
        log.info("Not Monday — no cash calendar")
        return 0
    await send(uuid.uuid4())
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="30-day cash calendar for the Director and accountants.")
    parser.add_argument("--force", action="store_true", help="run on any weekday")
    parser.add_argument("--dry-run", action="store_true", help="print, send nothing")
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(_main(args.force, args.dry_run))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


async def _main(force: bool, dry_run: bool) -> int:
    try:
        return await run(force, dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
