"""Agent B4 — data quality: what's missing or wrong in the data, every Monday.

From the owner's plan (ЭМЖИЕМ AI Агентлар Тизими): errors and gaps in the
files and databases cost real money and nobody checks them. This agent
checks what this system can see and tells IT (Admin Bot) once a week —
never the Director, and never with a guessed fix:

SAP (from the gateway pushes)
  * open invoices with no sales person (they show as "Бошқа" everywhere)
  * open invoices with no due date, or due before they were issued
  * balances that can't be right (negative, or above the invoice total)
  * each feed: when it last arrived, whether it hit its row limit, and rows
    whose columns couldn't be read
  * stock rows with a negative quantity or no cost (in what was pushed)

The bot's own records
  * employees who haven't given their name
  * open tasks with no deadline, older than 3 days
  * approved payments whose date can't be read (they can't be scheduled)
  * permission requests started and abandoned for 3+ days

Run:
    python agents/data-quality/agent.py            # Mondays only (the 08:00 job runs it daily)
    python agents/data-quality/agent.py --force    # any day
    python agents/data-quality/agent.py --dry-run  # print, send nothing
Admin Bot: /sifat runs it immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

# The folder name contains a hyphen, so this file cannot be imported as a
# package. Put the project root on sys.path and run it as a script instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common.config import settings
from integrations.common.db import close_pool, fetch_all
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_money_by_currency
from integrations.common.timeutil import fmt_date, now_utc, to_local, today_local
from integrations.org_bot import names, permissions, store
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.sap import figures
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "data-quality"
log = setup_logging(AGENT)

MONDAY = 0
STALE_FEED_DAYS = 1
OLD_DAYS = 3
MAX_EXAMPLES = 5

FEED_LABELS = {
    "invoices": "ҳисоб-фактуралар",
    "orders": "буюртмалар",
    "inventory": "омбор қолдиғи",
    "payments": "тўловлар",
    "customers": "мижозлар",
    "warehouses": "омборлар",
    "products": "маҳсулотлар",
}


@dataclass
class Inputs:
    """Everything the checks look at, gathered once."""

    today: date
    invoices: list[dict[str, Any]] = field(default_factory=list)
    feeds: dict[str, dict[str, Any]] = field(default_factory=dict)  # tool -> {"at": datetime, "rows": int}
    unreadable: dict[str, int] = field(default_factory=dict)
    inventory: list[dict[str, Any]] = field(default_factory=list)
    unnamed: list[str] = field(default_factory=list)
    undated_tasks: int = 0
    unclear_payments: list[str] = field(default_factory=list)
    stale_drafts: int = 0


# ------------------------------------------------------------------ checks


def _examples(docs: list[dict[str, Any]]) -> str:
    numbers = [f"#{d.get('doc_num')}" for d in docs[:MAX_EXAMPLES] if d.get("doc_num")]
    more = f" +{len(docs) - MAX_EXAMPLES}" if len(docs) > MAX_EXAMPLES else ""
    return f" ({', '.join(numbers)}{more})" if numbers else ""


def _total(docs: list[dict[str, Any]]) -> str:
    return format_money_by_currency([(d["balance_due_tiyin"], d.get("currency") or "UZS") for d in docs])


def sap_findings(inp: Inputs) -> list[str]:
    """Problems in the SAP data this system receives."""
    found: list[str] = []
    invoices = inp.invoices

    no_seller = [d for d in invoices if d.get("sales_person_code") is None or d["sales_person_code"] < 0]
    if no_seller:
        found.append(
            f"Масъул сотувчиси йўқ очиқ ҳисоб-фактура: {len(no_seller)} та, {escape(_total(no_seller))}"
            f"{escape(_examples(no_seller))}"
        )
    no_due = [d for d in invoices if d.get("due_date") is None]
    if no_due:
        found.append(f"Тўлов муддати йўқ ҳисоб-фактура: {len(no_due)} та{escape(_examples(no_due))}")
    due_first = [d for d in invoices if d.get("due_date") and d.get("doc_date") and d["due_date"] < d["doc_date"]]
    if due_first:
        found.append(f"Тўлов муддати ҳужжат санасидан олдин: {len(due_first)} та{escape(_examples(due_first))}")
    wrong_balance = [
        d for d in invoices if d["balance_due_tiyin"] < 0 or d["balance_due_tiyin"] > (d.get("doc_total_tiyin") or 0)
    ]
    if wrong_balance:
        found.append(f"Қолдиғи нотўғри ҳисоб-фактура: {len(wrong_balance)} та{escape(_examples(wrong_balance))}")

    feed_notes: list[str] = []
    for tool, label in FEED_LABELS.items():
        feed = inp.feeds.get(tool)
        if feed is None:
            feed_notes.append(f"{label} — ҳеч қачон келмаган")
            continue
        age = (inp.today - to_local(feed["at"]).date()).days
        if age > STALE_FEED_DAYS:
            feed_notes.append(f"{label} — {age} кун олдин келган")
        elif figures.is_capped(tool, feed.get("rows")):
            feed_notes.append(f"{label} — чекланган ({feed.get('rows')} та, тўлиқ эмас)")
    if feed_notes:
        found.append("SAP оқимлари: " + "; ".join(feed_notes))

    unreadable = [f"{FEED_LABELS.get(tool, tool)} — {count} та" for tool, count in inp.unreadable.items() if count]
    if unreadable:
        found.append("Устунлари ўқилмаган ёзувлар: " + "; ".join(unreadable))

    negative = [r for r in inp.inventory if _number(r.get("OnHand")) is not None and _number(r["OnHand"]) < 0]
    no_cost = [
        r for r in inp.inventory
        if (_number(r.get("OnHand")) or 0) > 0 and not _number(r.get("StockValue")) and not _number(r.get("AvgPrice"))
    ]
    sample = " (келган қисмида)" if figures.is_capped("inventory", len(inp.inventory)) else ""
    if negative:
        found.append(f"Манфий қолдиқ: {len(negative)} та позиция{sample}{escape(_items(negative))}")
    if no_cost:
        found.append(f"Таннархсиз қолдиқ: {len(no_cost)} та позиция{sample}{escape(_items(no_cost))}")
    return found


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _items(rows: list[dict[str, Any]]) -> str:
    codes = [str(r.get("ItemCode")) for r in rows[:MAX_EXAMPLES] if r.get("ItemCode")]
    more = f" +{len(rows) - MAX_EXAMPLES}" if len(rows) > MAX_EXAMPLES else ""
    return f" ({', '.join(codes)}{more})" if codes else ""


def bot_findings(inp: Inputs) -> list[str]:
    """Gaps in the bot's own records."""
    found: list[str] = []
    if inp.unnamed:
        shown = ", ".join(inp.unnamed[:MAX_EXAMPLES]) + (f" +{len(inp.unnamed) - MAX_EXAMPLES}" if len(inp.unnamed) > MAX_EXAMPLES else "")
        found.append(f"Исм ёзмаган ходимлар: {len(inp.unnamed)} — {escape(shown)} <i>(Admin Bot'да /ismlar)</i>")
    if inp.undated_tasks:
        found.append(f"Муддатсиз очиқ топшириқлар ({OLD_DAYS} кундан ортиқ): {inp.undated_tasks} та")
    if inp.unclear_payments:
        found.append(
            "Санаси ўқилмаган тасдиқланган тўловлар (режага тушмайди): " + escape(", ".join(inp.unclear_payments))
        )
    if inp.stale_drafts:
        found.append(f"{OLD_DAYS} кундан бери тугалланмаган рухсат сўровлари: {inp.stale_drafts} та")
    return found


def render(inp: Inputs) -> str:
    """The weekly message for IT."""
    lines = [f"🧹 <b>Маълумот сифати — {fmt_date(inp.today)}</b>", ""]
    for title, found in (("SAP", sap_findings(inp)), ("Бот", bot_findings(inp))):
        lines.append(f"<b>{title}</b>")
        if found:
            lines.extend(f"• {item}" for item in found)
        else:
            lines.append("✅ тоза")
        lines.append("")
    return "\n".join(lines).rstrip()


# ------------------------------------------------------------------ gather


async def gather_inputs(today: date) -> Inputs:
    """Read everything the checks need."""
    inp = Inputs(today=today)
    inp.invoices = await fetch_all(
        "SELECT doc_num, card_name, doc_date, due_date, currency, doc_total_tiyin, balance_due_tiyin, "
        "sales_person_code FROM v_ar_aging_latest ORDER BY balance_due_tiyin DESC"
    )

    for row in await fetch_all(
        """
        SELECT DISTINCT ON (action) action, occurred_at, payload FROM agent_actions
        WHERE agent = 'sap-gateway-push' AND status = 'success'
        ORDER BY action, occurred_at DESC
        """
    ):
        action = row["action"]
        tool = "invoices" if action == "ar_aging_push" else action.removeprefix("gateway_push_")
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
        inp.feeds[tool] = {"at": row["occurred_at"], "rows": payload.get("rows_received")}

    for row in await fetch_all(
        "SELECT tool, count(*) AS n FROM v_sap_gateway_latest WHERE natural_key LIKE 'unrecognized:%%' GROUP BY tool"
    ):
        inp.unreadable[row["tool"]] = int(row["n"])

    inp.inventory = [
        r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"])
        for r in await fetch_all("SELECT raw FROM v_sap_gateway_latest WHERE tool = 'inventory'")
    ]

    inp.unnamed = [
        names.person_name(e) for e in await store.list_active_employees()
        if e["role"] != DIRECTOR_ROLE and names.needs_name(e)
    ]
    rows = await fetch_all(
        "SELECT count(*) AS n FROM tasks WHERE status IN ('sent', 'started') AND due_date IS NULL "
        "AND created_at < now() - make_interval(days => %s)",
        (OLD_DAYS,),
    )
    inp.undated_tasks = int(rows[0]["n"]) if rows else 0

    for request in await store.approved_payment_requests(days=60):
        if permissions.due_day(request) is None:
            inp.unclear_payments.append(request.get("request_no") or "—")

    inp.stale_drafts = sum(
        1 for d in await store.permission_drafts()
        if isinstance(d.get("created_at"), datetime) and (now_utc() - d["created_at"]).days >= OLD_DAYS
    )
    return inp


async def check_now(run_id: uuid.UUID) -> str:
    """Run every check and send the result to the admin chat (also /sifat)."""
    text = render(await gather_inputs(today_local()))
    if settings.dry_run:
        print(text)
        return "dry_run"
    if not settings.admin_bot_telegram_chat_id:
        log.warning("ADMIN_BOT_TELEGRAM_CHAT_ID is not set — nowhere to send the data-quality report")
        return "no_admin_chat"
    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        try:
            await bot.send_message(text)
        except TelegramError as exc:
            log.error("Could not send the data-quality report: {}", exc)
            return "failed"
    return "sent"


async def run(force: bool = False, dry_run: bool = False) -> int:
    """Weekly entry point (Mondays unless forced)."""
    if dry_run:
        settings.dry_run = True
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0
    if not settings.data_quality_enabled and not settings.dry_run:
        log.info("Data-quality checks are paused (DATA_QUALITY_ENABLED is not true)")
        return 0
    if today_local().weekday() != MONDAY and not force:
        log.info("Not Monday — no data-quality report")
        return 0
    outcome = await check_now(uuid.uuid4())
    log.info("Data-quality report: {}", outcome)
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Weekly data-quality report for IT.")
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
