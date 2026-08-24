"""Agent 1 — CEO Daily Brief.

Runs every morning at 08:00 Tashkent time and sends the CEO one Telegram
message covering cash, receivables, pipeline, attendance and overdue tasks.

Design rule: **the brief always goes out.** Each source is fetched
independently and a failure in one (SAP down, no Verifix export yet) degrades
that section to a "data unavailable" line rather than killing the run. Which
sources failed is stated in the message and stored in ``daily_briefs.source_errors``,
so a quiet failure can never masquerade as good news.

Cash and Receivables are fetched independently of each other, not as one
combined SAP call: Receivables reads the AR aging snapshot the SAP gateway's
own machine pushes (see ``scripts/sap-gateway-push/`` and
``agents/receivables/agent.py``, which reads the exact same table), while
Cash still needs a live SAP B1 Service Layer call that has never been
reachable from this service. A combined fetch would fail both together the
moment Cash's SAPClient call errors, hiding Receivables data that's actually
available — splitting them means Receivables can succeed on its own.

Run:
    python agents/ceo-daily-brief/agent.py            # send
    python agents/ceo-daily-brief/agent.py --dry-run  # print, send nothing

Cron (VPS, UTC — 03:00 UTC = 08:00 Tashkent):
    0 3 * * * cd /opt/mgmg-command-center && docker compose run --rm api \\
        python agents/ceo-daily-brief/agent.py >> logs/cron-brief.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

# The folder name contains a hyphen, so this file cannot be imported as a
# package. Put the project root on sys.path and run it as a script instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dataclasses import dataclass, field
from typing import Any

from integrations.common import snapshots
from integrations.common.config import settings
from integrations.common.db import close_pool, execute, fetch_all, log_action
from integrations.common.divisions import label as division_label
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_uzs, format_uzs_short
from integrations.common.timeutil import fmt_date, now_local, now_utc, today_local
from integrations.crm.client import CRMClient
from integrations.crm.models import PipelineSummary
from integrations.microsoft.client import GraphClient
from integrations.org_bot.notify import notify_directors
from integrations.sap.client import SAPClient
from integrations.sap.models import ARAging, ARInvoice, CashAccount
from integrations.telegram.bot import escape
from integrations.verifix.client import AttendanceSummary, VerifixClient, persist_attendance

AGENT = "ceo-daily-brief"
SOURCE_COUNT = 5  # SAP cash, SAP aging (gateway-pushed), CRM, Verifix, Microsoft Graph
log = setup_logging(AGENT)

# How many line items to show per section before collapsing into "+N more".
MAX_LINES = 5


@dataclass
class BriefData:
    """Everything the brief needs, plus a record of what could not be fetched."""

    cash: list[CashAccount] | None = None
    aging: ARAging | None = None
    pipeline: PipelineSummary | None = None
    attendance: AttendanceSummary | None = None
    overdue_tasks: list[dict[str, Any]] | None = None
    errors: list[dict[str, str]] = field(default_factory=list)

    def note_failure(self, source: str, error: BaseException) -> None:
        """Record that one source could not be read.

        Args:
            source: Source system name.
            error: The exception raised.
        """
        self.errors.append({"source": source, "error": f"{type(error).__name__}: {error}"[:400]})
        log.error("Source '{}' failed: {}", source, error)

    @property
    def cash_total_tiyin(self) -> int:
        """Total across all cash and bank accounts, 0 when unavailable."""
        return sum(a.balance_tiyin for a in self.cash) if self.cash else 0


# --------------------------------------------------------------------- collect


async def collect(run_id: uuid.UUID) -> BriefData:
    """Fetch every section of the brief, tolerating individual source failures.

    Sources are queried concurrently — the run takes as long as the slowest
    system, not the sum of all of them.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        A ``BriefData`` with whatever could be fetched and a list of failures.
    """
    data = BriefData()

    results = await asyncio.gather(
        _fetch_cash(run_id),
        _fetch_aging(run_id),
        _fetch_crm(run_id),
        _fetch_verifix(run_id),
        _fetch_planner(run_id),
        return_exceptions=True,
    )
    cash_result, aging_result, crm_result, verifix_result, planner_result = results

    # Split from one combined SAP fetch into two independent ones: the AR
    # aging snapshot now comes from the gateway push (see _fetch_aging) and
    # succeeds even though live SAP cash access still doesn't exist -- a
    # combined fetch would fail both together the moment cash's SAPClient
    # call errors, hiding aging data that's actually available.
    if isinstance(cash_result, BaseException):
        data.note_failure("sap_cash", cash_result)
    else:
        data.cash = cash_result

    if isinstance(aging_result, BaseException):
        data.note_failure("sap_aging", aging_result)
    else:
        data.aging = aging_result

    if isinstance(crm_result, BaseException):
        data.note_failure("crm", crm_result)
    else:
        data.pipeline = crm_result

    if isinstance(verifix_result, BaseException):
        data.note_failure("verifix", verifix_result)
    else:
        data.attendance = verifix_result

    if isinstance(planner_result, BaseException):
        data.note_failure("msgraph", planner_result)
    else:
        data.overdue_tasks = planner_result

    return data


async def _fetch_cash(run_id: uuid.UUID) -> list[CashAccount]:
    """Pull cash balances from SAP directly, and snapshot them.

    Still a live SAP call, unlike ``_fetch_aging`` below — nothing currently
    provides cash/bank account balances via the gateway push, only AR aging.
    This keeps failing until either the SAP B1 Service Layer becomes
    reachable, or the gateway grows a cash-balance tool.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        The cash accounts.

    Raises:
        SAPError: if SAP is unreachable or rejects the query.
    """
    async with SAPClient(agent=AGENT, run_id=run_id) as sap:
        cash = await sap.get_cash_balance()

    await snapshots.persist_cash_balances(cash)
    return cash


async def _fetch_aging(run_id: uuid.UUID) -> ARAging:
    """Read the latest AR aging snapshot, pushed by the SAP gateway's own
    machine (see ``scripts/sap-gateway-push/`` and
    ``integrations/sap/push_handler.py``) — not a live SAP call.

    Mirrors ``agents/receivables/agent.py``'s ``collect()`` exactly (same
    source table, same model construction) — the two agents deliberately
    read the same snapshot rather than each defining their own version of
    "what counts as AR aging."

    Args:
        run_id: Unused — kept for call-site symmetry with ``_fetch_cash``.

    Returns:
        The full aging result, built from ``v_ar_aging_latest``.

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


async def _fetch_crm(run_id: uuid.UUID) -> PipelineSummary:
    """Pull the pipeline summary from MGMG's own CRM and snapshot it.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        The pipeline summary.

    Raises:
        CRMError: if the CRM is unreachable or rejects the queries.
    """
    async with CRMClient(agent=AGENT, run_id=run_id) as crm:
        summary = await crm.get_pipeline_summary()

    await snapshots.persist_crm_pipeline(summary)
    return summary


async def _fetch_verifix(run_id: uuid.UUID) -> AttendanceSummary:
    """Pull today's attendance exceptions and snapshot them.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        The attendance summary.

    Raises:
        VerifixError: only on misconfiguration; missing data returns empty.
    """
    client = VerifixClient(agent=AGENT, run_id=run_id)
    summary = await client.get_attendance()
    await persist_attendance(summary, [*summary.late, *summary.absent])
    return summary


async def _fetch_planner(run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Pull overdue Planner tasks from Microsoft Graph and snapshot them.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        Overdue task dicts.

    Raises:
        GraphError: if Graph is unreachable or the group id is unset.
    """
    async with GraphClient(agent=AGENT, run_id=run_id) as graph:
        tasks = await graph.get_overdue_planner_tasks()

    await snapshots.persist_planner_tasks(tasks)
    return tasks


# ---------------------------------------------------------------------- render


def render(data: BriefData) -> str:
    """Format the brief as a Telegram HTML message.

    Severity markers: 🔴 needs action today, 🟡 watch, 🟢 fine.

    Args:
        data: The collected brief data.

    Returns:
        The full message body (splitting happens in the Telegram client).
    """
    day = today_local()
    parts: list[str] = [
        f"<b>☀️ CEO Kunlik Hisoboti — {fmt_date(day)}</b>",
        f"<i>{now_local().strftime('%H:%M')} Toshkent</i>",
        "",
        _render_cash(data),
        _render_receivables(data),
        _render_pipeline(data),
        _render_attendance(data),
        _render_tasks(data),
    ]

    if data.errors:
        failed = ", ".join(escape(e["source"]) for e in data.errors)
        parts.append(f"⚠️ <i>Ma'lumot yo'q: {failed} — yuqoridagi ko'rsatkichlar to'liq emas.</i>")

    return "\n".join(p for p in parts if p is not None)


def _render_cash(data: BriefData) -> str:
    """Render the cash section.

    "Not connected yet" rather than a "⚠️ unavailable" warning: nothing
    currently provides cash/bank balances at all (no gateway tool, no
    reachable SAP Service Layer) -- this is a permanent, known state, not a
    daily transient failure, and a recurring "unavailable" alert reads like
    something broken that needs fixing right now when it doesn't. The
    honest disclosure that this section is incomplete still shows in the
    brief's own "No data from: sap_cash..." footer line either way.
    """
    if data.cash is None:
        return "💰 <b>Kassa</b>\n   Hali ulanmagan — ma'lumot manbai sozlanmagan\n"
    if not data.cash:
        return "💰 <b>Kassa</b>\n   Kassa hisoblari sozlanmagan\n"

    lines = [f"💰 <b>Kassa: {escape(format_uzs(data.cash_total_tiyin))}</b>"]
    for account in sorted(data.cash, key=lambda a: a.balance_tiyin, reverse=True)[:MAX_LINES]:
        name = account.bank_name or account.account_name or account.account_code
        marker = "🔴" if account.balance_tiyin < 0 else "  "
        lines.append(f"   {marker} {escape(name)}: {escape(format_uzs_short(account.balance_tiyin))}")
    if len(data.cash) > MAX_LINES:
        lines.append(f"   <i>+yana {len(data.cash) - MAX_LINES} ta hisob</i>")
    return "\n".join(lines) + "\n"


def _render_receivables(data: BriefData) -> str:
    """Render the receivables section with aging buckets."""
    if data.aging is None:
        return "📉 <b>Debitorlik qarzlari</b>\n   ⚠️ Ma'lumot mavjud emas\n"

    aging = data.aging
    marker = "🟢"
    if aging.bucket_totals_tiyin.get("90_plus", 0) > 0:
        marker = "🔴"
    elif aging.total_overdue_tiyin > 0:
        marker = "🟡"

    lines = [
        f"{marker} <b>Muddati o'tgan qarz: {escape(format_uzs(aging.total_overdue_tiyin))}</b> "
        f"({aging.overdue_count} ta hisob-faktura)",
        f"   Jami ochiq: {escape(format_uzs_short(aging.total_open_tiyin))}",
    ]

    bucket_labels = [("1_30", "1–30 kun"), ("31_60", "31–60 kun"), ("61_90", "61–90 kun"), ("90_plus", "90+ kun")]
    for key, caption in bucket_labels:
        amount = aging.bucket_totals_tiyin.get(key, 0)
        if amount <= 0:
            continue
        emoji = "🔴" if key == "90_plus" else "🟡"
        count = aging.bucket_counts.get(key, 0)
        lines.append(f"   {emoji} {caption}: {escape(format_uzs_short(amount))} ({count})")

    worst = sorted(
        (i for i in aging.invoices if i.days_overdue > 0),
        key=lambda i: i.balance_due_tiyin,
        reverse=True,
    )[:3]
    if worst:
        lines.append("   <i>Eng kattalari:</i>")
        for inv in worst:
            owner = inv.sales_person_name or division_label(inv.division)
            lines.append(
                f"   • {escape(inv.card_name or inv.card_code)} — "
                f"{escape(format_uzs_short(inv.balance_due_tiyin))}, "
                f"{inv.days_overdue} kun ({escape(owner)})"
            )

    return "\n".join(lines) + "\n"


def _render_pipeline(data: BriefData) -> str:
    """Render the CRM pipeline section."""
    if data.pipeline is None:
        return "📊 <b>Pipeline</b>\n   ⚠️ CRM mavjud emas\n"

    pipeline = data.pipeline
    stalled = len(pipeline.deals_without_task)
    marker = "🔴" if stalled > 10 else "🟡" if stalled else "🟢"

    lines = [
        f"📊 <b>Pipeline: {escape(format_uzs_short(pipeline.total_value_tiyin))}</b> "
        f"({pipeline.total_deals} ta bitim)",
        f"   🆕 Yangi leadlar (24 soat): {pipeline.new_leads_24h}",
        f"   {marker} Vazifasiz bitimlar: {stalled}",
    ]

    for deal in pipeline.deals_without_task[:MAX_LINES]:
        # The CRM's API exposes assigned_to only as a numeric manager id —
        # no endpoint resolves it to a name, unlike amoCRM's user list.
        owner = f"Menejer #{deal.assigned_to}" if deal.assigned_to else "biriktirilmagan"
        lines.append(
            f"   • {escape(deal.title or f'Bitim {deal.id}')} — "
            f"{escape(format_uzs_short(deal.amount_tiyin))} ({escape(owner)})"
        )
    if stalled > MAX_LINES:
        lines.append(f"   <i>+yana {stalled - MAX_LINES} ta</i>")

    return "\n".join(lines) + "\n"


def _render_attendance(data: BriefData) -> str:
    """Render the HR attendance section."""
    if data.attendance is None:
        return "👥 <b>Davomat</b>\n   ⚠️ Verifix mavjud emas\n"

    att = data.attendance
    if att.total_records == 0:
        return "👥 <b>Davomat</b>\n   ⚠️ Bugun uchun eksport qabul qilinmadi\n"

    marker = "🔴" if att.absent else "🟡" if att.late else "🟢"
    lines = [
        f"{marker} <b>Davomat:</b> {len(att.late)} kechikkan, {len(att.absent)} kelmagan "
        f"(jami {att.total_records})"
    ]
    for record in att.absent[:MAX_LINES]:
        lines.append(f"   🔴 {escape(record.employee_name or record.employee_id)} — kelmagan")
    for record in att.late[: max(MAX_LINES - len(att.absent[:MAX_LINES]), 0)]:
        lines.append(
            f"   🟡 {escape(record.employee_name or record.employee_id)} — "
            f"{record.late_minutes} daqiqa kechikkan"
        )

    return "\n".join(lines) + "\n"


def _render_tasks(data: BriefData) -> str:
    """Render the overdue Planner tasks section."""
    if data.overdue_tasks is None:
        return "📋 <b>Vazifalar</b>\n   ⚠️ Microsoft Planner mavjud emas\n"

    tasks = data.overdue_tasks
    marker = "🔴" if len(tasks) > 10 else "🟡" if tasks else "🟢"
    lines = [f"{marker} <b>Muddati o'tgan Planner vazifalari: {len(tasks)}</b>"]

    for task in tasks[:MAX_LINES]:
        who = ", ".join(task.get("assignedToNames", [])) or "biriktirilmagan"
        lines.append(
            f"   • {escape(task.get('title', 'Nomsiz'))} — "
            f"{task.get('daysOverdue', 0)} kun ({escape(who)})"
        )
    if len(tasks) > MAX_LINES:
        lines.append(f"   <i>+yana {len(tasks) - MAX_LINES} ta</i>")

    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------- store


async def store(run_id: uuid.UUID, data: BriefData, message: str, message_id: int | None) -> None:
    """Save the brief to ``daily_briefs``.

    Args:
        run_id: UUID of this run.
        data: The collected data.
        message: The exact rendered message text.
        message_id: Telegram message id, or ``None`` if nothing was sent.

    Raises:
        psycopg.Error: on a database failure.
    """
    sections = {
        "cash": [a.model_dump(mode="json") for a in (data.cash or [])],
        "ar_buckets": (data.aging.bucket_totals_tiyin if data.aging else {}),
        "pipeline_by_name": (data.pipeline.by_pipeline if data.pipeline else {}),
        "attendance": {
            "late": [r.model_dump(mode="json") for r in (data.attendance.late if data.attendance else [])],
            "absent": [r.model_dump(mode="json") for r in (data.attendance.absent if data.attendance else [])],
        },
        "overdue_tasks": [
            {"title": t.get("title"), "days_overdue": t.get("daysOverdue")}
            for t in (data.overdue_tasks or [])
        ],
    }

    status = "dry_run" if settings.dry_run else ("sent" if message_id else "failed")
    sent_at = now_utc() if message_id else None

    await execute(
        """
        INSERT INTO daily_briefs
            (run_id, brief_date, sent_at, telegram_message_id, chat_id, status,
             cash_total_tiyin, ar_overdue_total_tiyin, pipeline_total_tiyin,
             new_leads_24h, deals_without_task, employees_late, employees_absent,
             planner_tasks_overdue, sections, message_text, source_errors)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(run_id),
            today_local(),
            sent_at,
            message_id,
            # No single fixed chat anymore -- sent via OPS Manager Bot to
            # whoever currently holds the Director role (see notify.py);
            # this column is informational, not used for delivery.
            "via ops_manager_bot",
            status,
            data.cash_total_tiyin if data.cash is not None else None,
            data.aging.total_overdue_tiyin if data.aging else None,
            data.pipeline.total_value_tiyin if data.pipeline else None,
            data.pipeline.new_leads_24h if data.pipeline else None,
            len(data.pipeline.deals_without_task) if data.pipeline else None,
            len(data.attendance.late) if data.attendance else None,
            len(data.attendance.absent) if data.attendance else None,
            len(data.overdue_tasks) if data.overdue_tasks is not None else None,
            json.dumps(sections, ensure_ascii=False, default=str),
            message,
            json.dumps(data.errors, ensure_ascii=False),
        ),
    )


# ------------------------------------------------------------------------ main


async def run(dry_run: bool = False) -> int:
    """Collect, render, send and store the daily brief.

    Args:
        dry_run: Print the message instead of sending it.

    Returns:
        Process exit code — 0 on success, 1 if the brief could not be sent,
        2 if every source failed (nothing worth sending).
    """
    if dry_run:
        settings.dry_run = True

    run_id = uuid.uuid4()
    log.info("Daily brief run {} starting (dry_run={})", run_id, settings.dry_run)

    unfilled = settings.missing_placeholders()
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(unfilled))
        return 2

    data = await collect(run_id)
    message = render(data)

    if len(data.errors) == SOURCE_COUNT:
        log.error("Every source failed — sending a failure notice instead of a brief")
        message = (
            f"🔴 <b>CEO Kunlik Hisoboti — {fmt_date(today_local())}</b>\n\n"
            "Hech qanday tizimdan ma'lumot olib bo'lmadi. "
            "Server va integratsiya loglarini tekshiring."
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
    """Run the agent and close the database pool.

    Args:
        dry_run: Passed through to ``run``.

    Returns:
        The process exit code.
    """
    try:
        return await run(dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
