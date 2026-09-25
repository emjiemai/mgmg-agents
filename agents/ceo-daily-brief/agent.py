"""Agent — CEO Daily Brief: the five numbers (A2) and who didn't report.

Runs every morning at 08:00 Tashkent time. One short Telegram message, in
Uzbek Cyrillic like every bot message:

    ☀️ CEO кунлик ҳисоботи — 26.09.2026. 08:00 Тошкент

    📊 5 рақам
    💰 Касса: уланмаган
    📈 Кечаги сотув: $12,340.00 (8 та буюртма)
    📦 Захира: камида $120,000.00*
    🧾 Мижоз қарзи: $15,200.00, муддати ўтгани $7,384.36 (16 та)
    💳 Бугунги тўловлар: 2 та — 15 000 000 сўм

    🔴 Ҳисобот юбормаганлар (25.09.2026): 1 / 5
       • Алишер Каримов (IT)

The five numbers are A2 from the owner's plan (ЭМЖИЕМ AI Агентлар Тизими):
cash, yesterday's sales, stock value, customer debt, today's payments — with
the change since the previous brief where the two days are comparable.

Honesty rules, because a wrong number here is worse than none:
  * Cash has no source yet (the SAP gateway has no cash tool): "уланмаган".
  * A SAP feed that hit its push row limit gives a lower bound: "камида",
    with a footnote. See integrations/sap/figures.py.
  * Today's payments come from approved written permissions (B1 makes that
    form the single channel for spending). A request whose date can't be read
    is counted as "sana aniq emas", never put on a guessed day.
  * A feed that failed or never arrived reads "маълумот йўқ".

Run:
    python agents/ceo-daily-brief/agent.py            # send
    python agents/ceo-daily-brief/agent.py --dry-run  # print, send nothing
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
from integrations.common.db import close_pool, execute, fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_money, format_money_by_currency
from integrations.common.timeutil import fmt_date, now_local, now_utc, today_local
from integrations.org_bot import permissions
from integrations.org_bot import store as org_store
from integrations.org_bot.notify import notify_directors
from integrations.org_bot.roles import ROLE_LABELS
from integrations.sap import figures
from integrations.sap.figures import Figure
from integrations.sap.models import ARAging, ARInvoice
from integrations.telegram.bot import escape

AGENT = "ceo-daily-brief"
log = setup_logging(AGENT)

# Only this bot's token is needed to deliver — other agents' settings (the
# Lead Agent's Google keys, say) must not block the brief if they're unset.
REQUIRED_SETTINGS = {"ops_manager_bot_telegram_bot_token"}

# How many names to list before collapsing into "+N more".
MAX_LINES = 5

# A SAP feed older than this is shown as "not updated since …", not as today's number.
STALE_AFTER_DAYS = 3


@dataclass
class PaymentsDue:
    """Approved written payments due on one day."""

    totals: dict[str, int] = field(default_factory=dict)
    count: int = 0
    unclear: int = 0  # approved, with an amount, but no readable date


@dataclass
class BriefData:
    """Everything the brief needs, plus a record of what could not be fetched."""

    aging: ARAging | None = None
    aging_capped: bool = False
    sales: Figure | None = None
    inventory: Figure | None = None
    payments: PaymentsDue | None = None
    # Rows from the last day employees were asked for a daily report (see
    # store.report_results_before); None when that fetch failed.
    report_rows: list[dict[str, Any]] | None = None
    # The previous brief's five numbers, for the "since yesterday" change.
    previous: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    def note_failure(self, source: str, error: BaseException) -> None:
        """Record that one source could not be read."""
        self.errors.append({"source": source, "error": f"{type(error).__name__}: {error}"[:400]})
        log.error("Source '{}' failed: {}", source, error)


# --------------------------------------------------------------------- collect


async def collect() -> BriefData:
    """Fetch every part of the brief, tolerating individual source failures."""
    data = BriefData()
    today = today_local()
    names = ["sap_aging", "sap_invoice_cap", "sap_orders", "sap_inventory", "payments", "daily_reports", "previous"]
    results = await asyncio.gather(
        _fetch_aging(),
        _fetch_rows_received("ar_aging_push"),
        _fetch_gateway("orders"),
        _fetch_gateway("inventory"),
        _fetch_payments_due(today),
        _fetch_report_results(),
        _fetch_previous(today),
        return_exceptions=True,
    )
    by_name = dict(zip(names, results))
    for name, result in by_name.items():
        if isinstance(result, BaseException):
            data.note_failure(name, result)

    currency = settings.sap_default_currency
    if not isinstance(by_name["sap_aging"], BaseException):
        data.aging = by_name["sap_aging"]
    if not isinstance(by_name["sap_invoice_cap"], BaseException):
        data.aging_capped = figures.is_capped("invoices", by_name["sap_invoice_cap"])
    if not isinstance(by_name["sap_orders"], BaseException):
        as_of, rows = by_name["sap_orders"]
        data.sales = _dated(figures.documents_on(rows, today - timedelta(days=1), currency, tool="orders", as_of=as_of))
    if not isinstance(by_name["sap_inventory"], BaseException):
        as_of, rows = by_name["sap_inventory"]
        data.inventory = _dated(figures.inventory_value(rows, currency, as_of=as_of))
    if not isinstance(by_name["payments"], BaseException):
        data.payments = by_name["payments"]
    if not isinstance(by_name["daily_reports"], BaseException):
        data.report_rows = by_name["daily_reports"]
    if not isinstance(by_name["previous"], BaseException):
        data.previous = by_name["previous"]
    return data


def _dated(figure: Figure) -> Figure:
    """Mark a figure stale when its feed hasn't been pushed for days."""
    if figure.as_of and (today_local() - figure.as_of).days > STALE_AFTER_DAYS:
        figure.status = "stale"
    return figure


async def _fetch_aging() -> ARAging:
    """Read the latest AR aging snapshot pushed by the SAP gateway's machine.

    Mirrors ``agents/receivables/agent.py``'s ``collect()`` (same source,
    same model), so both agents agree on what counts as open receivables.

    Raises:
        RuntimeError: if no snapshot has ever been pushed yet.
    """
    rows = await fetch_all(
        "SELECT snapshot_date, doc_entry, doc_num, card_code, card_name, doc_date, due_date, "
        "days_overdue, aging_bucket, currency, doc_total_tiyin, paid_to_date_tiyin, "
        "balance_due_tiyin, sales_person_code, sales_person_name, division "
        "FROM v_ar_aging_latest ORDER BY balance_due_tiyin DESC"
    )
    if not rows:
        raise RuntimeError(
            "No AR aging snapshot has been pushed yet — the SAP gateway push script "
            "(scripts/sap-gateway-push/) needs to run at least once."
        )

    invoices = [
        ARInvoice(
            doc_entry=r["doc_entry"],
            doc_num=r["doc_num"],
            card_code=r["card_code"],
            card_name=r["card_name"],
            doc_date=r["doc_date"],
            due_date=r["due_date"],
            days_overdue=r["days_overdue"],
            aging_bucket=r["aging_bucket"],
            currency=r["currency"],
            doc_total_tiyin=r["doc_total_tiyin"],
            paid_to_date_tiyin=r["paid_to_date_tiyin"],
            balance_due_tiyin=r["balance_due_tiyin"],
            sales_person_code=r["sales_person_code"],
            sales_person_name=r["sales_person_name"],
            division=r["division"],
        )
        for r in rows
    ]
    aging = ARAging(snapshot_date=rows[0]["snapshot_date"], invoices=invoices)
    aging.total_open_tiyin = sum(i.balance_due_tiyin for i in invoices)
    aging.total_overdue_tiyin = sum(i.balance_due_tiyin for i in invoices if i.days_overdue > 0)
    for bucket in ("current", "1_30", "31_60", "61_90", "90_plus"):
        in_bucket = [i for i in invoices if i.aging_bucket == bucket]
        aging.bucket_totals_tiyin[bucket] = sum(i.balance_due_tiyin for i in in_bucket)
        aging.bucket_counts[bucket] = len(in_bucket)
    return aging


async def _fetch_rows_received(action: str) -> int | None:
    """How many rows the latest push of one kind carried (from the audit log)."""
    row = await fetch_one(
        "SELECT payload FROM agent_actions WHERE agent = 'sap-gateway-push' AND action = %s "
        "ORDER BY occurred_at DESC LIMIT 1",
        (action,),
    )
    if row is None:
        return None
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
    received = payload.get("rows_received")
    return int(received) if received is not None else None


async def _fetch_gateway(tool: str) -> tuple[date | None, list[dict[str, Any]]]:
    """The latest pushed snapshot of one SAP gateway tool: (its day, raw rows)."""
    rows = await fetch_all("SELECT snapshot_date, raw FROM v_sap_gateway_latest WHERE tool = %s", (tool,))
    if not rows:
        return None, []
    raw = [r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"]) for r in rows]
    return rows[0]["snapshot_date"], raw


async def _fetch_payments_due(day: date) -> PaymentsDue:
    """Approved written payments due on ``day`` (B1's register)."""
    due = PaymentsDue()
    for request in await org_store.approved_payment_requests(days=120):
        when = permissions.due_day(request)
        if when is None:
            # Only recent ones: an old approval with an unreadable date isn't news.
            decided = request.get("decided_at")
            if decided is not None and (now_utc() - decided).days <= 30:
                due.unclear += 1
            continue
        if when == day:
            currency = request.get("currency") or "UZS"
            due.totals[currency] = due.totals.get(currency, 0) + int(request["amount_tiyin"])
            due.count += 1
    return due


async def _fetch_report_results() -> list[dict[str, Any]]:
    """Who was asked for a daily report on the last asked day, and who answered.

    Empty if nobody has ever been asked, or if daily reports are switched off
    (otherwise it would keep naming the last asked day's non-reporters).
    """
    if not settings.daily_reports_enabled:
        return []
    return await org_store.report_results_before(today_local())


async def _fetch_previous(today: date) -> dict[str, Any]:
    """The five numbers from the last brief actually sent before today."""
    row = await fetch_one(
        "SELECT sections FROM daily_briefs WHERE brief_date < %s AND status = 'sent' "
        "ORDER BY brief_date DESC, generated_at DESC LIMIT 1",
        (today,),
    )
    if row is None:
        return {}
    sections = row["sections"] if isinstance(row["sections"], dict) else json.loads(row["sections"] or "{}")
    return sections.get("a2") or {}


# ---------------------------------------------------------------------- render


def render(data: BriefData) -> str:
    """Format the brief as a Telegram HTML message."""
    day = today_local()
    parts: list[str | None] = [
        f"<b>☀️ CEO кунлик ҳисоботи — {fmt_date(day)}.</b> <i>{now_local().strftime('%H:%M')} Тошкент</i>",
        "",
        render_five(data),
        _render_missed_reports(data),
    ]
    return "\n".join(p for p in parts if p is not None).rstrip()


def _money(totals: dict[str, int]) -> str:
    return format_money_by_currency([(amount, currency) for currency, amount in totals.items()])


def _change(key: str, totals: dict[str, int], data: BriefData, comparable: bool) -> str:
    """ " (кечагига ▲ $1,200.00)" when yesterday's number is comparable, else ""."""
    previous = data.previous.get(key) or {}
    if not comparable or previous.get("status") != "ok" or previous.get("capped"):
        return ""
    delta = figures.change(totals, previous.get("totals") or {})
    parts = [
        f"{'▲' if amount > 0 else '▼'} {format_money(abs(amount), currency)}"
        for currency, amount in delta.items()
        if amount
    ]
    return f" <i>(кечагига {', '.join(parts)})</i>" if parts else ""


def _figure_value(figure: Figure | None, key: str, data: BriefData, *, what: str) -> tuple[str, bool]:
    """The value text for one SAP figure, and whether it's a lower bound."""
    if figure is None:
        return "<i>маълумот йўқ</i>", False
    if figure.status == "no_data":
        return "<i>SAP'дан маълумот келмаган</i>", False
    if figure.status == "unknown_format":
        return "<i>SAP маълумоти ўқилмади</i>", False
    if figure.status == "stale":
        return f"<i>SAP маълумоти {fmt_date(figure.as_of)} дан бери янгиланмаган</i>", False
    if not figure.totals:
        text = f"0 ({what} йўқ)" if not figure.capped else "<i>аниқлаб бўлмади</i>*"
        return text, figure.capped
    value = escape(_money(figure.totals))
    if figure.capped:
        value = f"камида {value}*"
    return value + _change(key, figure.totals, data, comparable=not figure.capped), figure.capped


def render_five(data: BriefData) -> str:
    """The five-number block (A2)."""
    lines = ["📊 <b>5 рақам</b>", "💰 Касса: <i>уланмаган</i>"]
    lower_bound = False

    sales, capped = _figure_value(data.sales, "sales", data, what="буюртма")
    if data.sales is not None and data.sales.ok and data.sales.count:
        sales += f" ({data.sales.count} та буюртма)"
    lines.append(f"📈 Кечаги сотув: {sales}")
    lower_bound |= capped

    stock, capped = _figure_value(data.inventory, "inventory", data, what="қолдиқ")
    lines.append(f"📦 Захира: {stock}")
    lower_bound |= capped

    if data.aging is None:
        lines.append("🧾 Мижоз қарзи: <i>маълумот йўқ</i>")
    else:
        aging = data.aging
        open_totals = _totals_by_currency(aging.invoices, overdue_only=False)
        overdue_totals = _totals_by_currency(aging.invoices, overdue_only=True)
        debt = escape(_money(open_totals))
        if data.aging_capped:
            debt = f"камида {debt}*"
            lower_bound = True
        debt += _change("debt", open_totals, data, comparable=not data.aging_capped)
        if overdue_totals:
            marker = " 🔴" if aging.bucket_totals_tiyin.get("90_plus", 0) > 0 else ""
            debt += f", муддати ўтгани {escape(_money(overdue_totals))} ({aging.overdue_count} та){marker}"
        lines.append(f"🧾 Мижоз қарзи: {debt}")

    if data.payments is None:
        lines.append("💳 Бугунги тўловлар: <i>маълумот йўқ</i>")
    else:
        pay = data.payments
        text = f"{pay.count} та — {escape(_money(pay.totals))}" if pay.count else "йўқ"
        if pay.unclear:
            text += f" <i>({pay.unclear} та тасдиқланган сўровда сана аниқ эмас)</i>"
        lines.append(f"💳 Бугунги тўловлар: {text}")

    if lower_bound:
        lines.append("<i>* SAP'дан фақат чекланган миқдордаги ёзув келди — рақам тўлиқ эмас.</i>")
    return "\n".join(lines) + "\n"


def _totals_by_currency(invoices: list[ARInvoice], *, overdue_only: bool) -> dict[str, int]:
    totals: dict[str, int] = {}
    for invoice in invoices:
        if overdue_only and invoice.days_overdue <= 0:
            continue
        totals[invoice.currency] = totals.get(invoice.currency, 0) + invoice.balance_due_tiyin
    return totals


def _render_missed_reports(data: BriefData) -> str | None:
    """Who didn't send OPS Manager Bot their daily report on the last asked day."""
    if data.report_rows is None:
        return "📋 <b>Кунлик ҳисоботлар</b>\n   ⚠️ Маълумот мавжуд эмас\n"
    if not data.report_rows:
        # Said out loud: nobody was asked (daily reports switched off, or no
        # working day yet) — not "everyone reported".
        return "📋 <b>Кунлик ҳисоботлар:</b> кеча ҳеч кимдан сўралмаган\n"

    day_label = fmt_date(data.report_rows[0]["report_date"])
    total = len(data.report_rows)
    missed = [r for r in data.report_rows if r["status"] != "submitted"]
    if not missed:
        return f"🟢 <b>Кунлик ҳисоботлар ({day_label}):</b> ҳаммаси юборди ({total}/{total})\n"

    lines = [f"🔴 <b>Ҳисобот юбормаганлар ({day_label}): {len(missed)} / {total}</b>"]
    for row in missed[:MAX_LINES]:
        role = ROLE_LABELS.get(row["role"], row["role"])
        lines.append(f"   • {escape(row['display_name'])} ({escape(role)})")
    if len(missed) > MAX_LINES:
        lines.append(f"   <i>+яна {len(missed) - MAX_LINES} та</i>")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------- store


def _figure_json(figure: Figure | None) -> dict[str, Any] | None:
    if figure is None:
        return None
    return {
        "status": figure.status,
        "totals": figure.totals,
        "count": figure.count,
        "capped": figure.capped,
        "as_of": figure.as_of.isoformat() if figure.as_of else None,
    }


def five_numbers_json(data: BriefData) -> dict[str, Any]:
    """The five numbers as stored, so tomorrow's brief can show the change."""
    debt = None
    if data.aging is not None:
        debt = {
            "status": "ok",
            "totals": _totals_by_currency(data.aging.invoices, overdue_only=False),
            "capped": data.aging_capped,
        }
    payments = None
    if data.payments is not None:
        payments = {"status": "ok", "totals": data.payments.totals, "count": data.payments.count}
    return {
        "sales": _figure_json(data.sales),
        "inventory": _figure_json(data.inventory),
        "debt": debt,
        "payments": payments,
    }


async def store(run_id: uuid.UUID, data: BriefData, message: str, message_id: int | None) -> None:
    """Save the brief to ``daily_briefs``."""
    sections = {
        "a2": five_numbers_json(data),
        "ar_buckets": (data.aging.bucket_totals_tiyin if data.aging else {}),
        "missed_daily_reports": [
            r["display_name"] for r in (data.report_rows or []) if r["status"] != "submitted"
        ],
    }
    status = "dry_run" if settings.dry_run else ("sent" if message_id else "failed")
    sent_at = now_utc() if message_id else None

    await execute(
        """
        INSERT INTO daily_briefs
            (run_id, brief_date, sent_at, telegram_message_id, chat_id, status,
             ar_overdue_total_tiyin, sections, message_text, source_errors)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(run_id),
            today_local(),
            sent_at,
            message_id,
            # Informational: sent via OPS Manager Bot to whoever holds the
            # Director role (see notify.py), not to one fixed chat.
            "via ops_manager_bot",
            status,
            data.aging.total_overdue_tiyin if data.aging else None,
            json.dumps(sections, ensure_ascii=False, default=str),
            message,
            json.dumps(data.errors, ensure_ascii=False),
        ),
    )


# ------------------------------------------------------------------------ main


async def run(dry_run: bool = False) -> int:
    """Collect, render, send and store the daily brief.

    Returns:
        Process exit code — 0 on success, 1 if the brief could not be sent,
        2 if config is incomplete.
    """
    if dry_run:
        settings.dry_run = True

    run_id = uuid.uuid4()
    log.info("Daily brief run {} starting (dry_run={})", run_id, settings.dry_run)

    unfilled = REQUIRED_SETTINGS & set(settings.missing_placeholders())
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(sorted(unfilled)))
        return 2

    data = await collect()
    message = render(data)
    if data.errors and all(
        getattr(data, attr) is None for attr in ("aging", "sales", "inventory", "payments", "report_rows")
    ):
        log.error("Every source failed — sending a failure notice instead of a brief")
        message = (
            f"🔴 <b>CEO кунлик ҳисоботи — {fmt_date(today_local())}</b>\n\n"
            "Ҳеч қандай тизимдан маълумот олиб бўлмади. "
            "Сервер ва интеграция логларини текширинг."
        )

    message_id: int | None = None
    try:
        if settings.dry_run:
            print(message)
        else:
            ids = await notify_directors(message, agent=AGENT, run_id=run_id)
            message_id = ids[0] if ids else None
    except Exception as exc:  # noqa: BLE001 — must still record the attempt
        log.error("Failed to send the brief: {}", exc)
        await log_action(
            agent=AGENT,
            action="send_brief",
            target_system="telegram",
            status="failure",
            run_id=run_id,
            mode="notify",
            error_message=str(exc),
        )

    await store(run_id, data, message, message_id)
    if settings.dry_run:
        return 0
    return 0 if message_id else 1


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Send the CEO daily brief.")
    parser.add_argument("--dry-run", action="store_true", help="print the brief instead of sending it")
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(_main(args.dry_run))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


async def _main(dry_run: bool) -> int:
    """Run the agent and close the database pool."""
    try:
        return await run(dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
