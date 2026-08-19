"""Agent 3 — Receivables Agent.

Pulls the AR aging from SAP once a day, sorts what is overdue into buckets, and
sends the CEO a Telegram alert naming the amount, the customer and the person
responsible for collecting it.

The alert is deliberately owner-first: an aging report nobody owns does not get
collected. Where SAP has no sales employee on the invoice, the division owner
is named instead, and unmapped invoices are shown as "Other" rather than being
dropped.

Read-only: this agent never touches SAP, sends no email, and creates no
documents. Its only side effects are the Telegram message and its own rows in
``alerts`` and ``agent_actions``.

Run:
    python agents/receivables/agent.py             # send
    python agents/receivables/agent.py --dry-run   # print, send nothing
    python agents/receivables/agent.py --min-days 30

Cron (VPS, UTC — 04:00 UTC = 09:00 Tashkent, an hour after the CEO brief):
    0 4 * * * cd /opt/mgmg-command-center && docker compose run --rm api \\
        python agents/receivables/agent.py >> logs/cron-receivables.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.common import snapshots
from integrations.common.config import settings
from integrations.common.db import close_pool, execute
from integrations.common.divisions import label as division_label
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_uzs, format_uzs_short
from integrations.common.timeutil import fmt_date
from integrations.sap.client import SAPClient
from integrations.sap.models import ARAging, ARInvoice
from integrations.telegram.bot import TelegramBot, escape

AGENT = "receivables"
log = setup_logging(AGENT)

BUCKET_ORDER = ["90_plus", "61_90", "31_60", "1_30"]
BUCKET_LABELS = {
    "1_30": "1–30 days",
    "31_60": "31–60 days",
    "61_90": "61–90 days",
    "90_plus": "90+ days",
}
BUCKET_SEVERITY = {"1_30": "info", "31_60": "warning", "61_90": "warning", "90_plus": "critical"}
BUCKET_EMOJI = {"1_30": "🟢", "31_60": "🟡", "61_90": "🟡", "90_plus": "🔴"}

# Invoices shown per bucket before the rest are summarized.
MAX_PER_BUCKET = 5


async def collect(run_id: uuid.UUID) -> ARAging:
    """Pull the AR aging from SAP and snapshot it.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        The full aging result.

    Raises:
        SAPError: if SAP is unreachable or rejects the query.
    """
    async with SAPClient(agent=AGENT, run_id=run_id) as sap:
        aging = await sap.get_ar_aging()

    await snapshots.persist_ar_aging(aging)
    return aging


def render(aging: ARAging, min_days: int) -> str:
    """Format the receivables alert as a Telegram HTML message.

    Args:
        aging: The aging result.
        min_days: Minimum days overdue to include.

    Returns:
        The message body.
    """
    overdue = [i for i in aging.invoices if i.days_overdue >= min_days]

    if not overdue:
        return (
            f"🟢 <b>Receivables — {fmt_date(aging.snapshot_date)}</b>\n\n"
            f"Nothing overdue by {min_days}+ day(s). "
            f"Total open: {escape(format_uzs_short(aging.total_open_tiyin))}."
        )

    total_overdue = sum(i.balance_due_tiyin for i in overdue)
    critical = aging.bucket_totals_tiyin.get("90_plus", 0)
    headline = "🔴" if critical > 0 else "🟡"

    lines = [
        f"{headline} <b>Receivables — {fmt_date(aging.snapshot_date)}</b>",
        "",
        f"<b>Overdue: {escape(format_uzs(total_overdue))}</b> across {len(overdue)} invoice(s)",
        f"Total open AR: {escape(format_uzs_short(aging.total_open_tiyin))}",
        "",
    ]

    by_bucket: dict[str, list[ARInvoice]] = defaultdict(list)
    for invoice in overdue:
        by_bucket[invoice.aging_bucket].append(invoice)

    for bucket in BUCKET_ORDER:
        invoices = sorted(by_bucket.get(bucket, []), key=lambda i: i.balance_due_tiyin, reverse=True)
        if not invoices:
            continue

        bucket_total = sum(i.balance_due_tiyin for i in invoices)
        lines.append(
            f"{BUCKET_EMOJI[bucket]} <b>{BUCKET_LABELS[bucket]}: "
            f"{escape(format_uzs_short(bucket_total))}</b> ({len(invoices)})"
        )

        for invoice in invoices[:MAX_PER_BUCKET]:
            lines.append(f"   • {_invoice_line(invoice)}")

        if len(invoices) > MAX_PER_BUCKET:
            rest = sum(i.balance_due_tiyin for i in invoices[MAX_PER_BUCKET:])
            lines.append(
                f"   <i>+{len(invoices) - MAX_PER_BUCKET} more, "
                f"{escape(format_uzs_short(rest))}</i>"
            )
        lines.append("")

    owners = _by_owner(overdue)
    if owners:
        lines.append("<b>By owner:</b>")
        for owner, amount in sorted(owners.items(), key=lambda kv: kv[1], reverse=True)[:8]:
            lines.append(f"   {escape(owner)} — {escape(format_uzs_short(amount))}")

    return "\n".join(lines)


def _invoice_line(invoice: ARInvoice) -> str:
    """Render one invoice as a single alert line.

    Args:
        invoice: The overdue invoice.

    Returns:
        HTML-safe text naming customer, amount, age and owner.
    """
    owner = invoice.sales_person_name or division_label(invoice.division)
    customer = invoice.card_name or invoice.card_code
    doc = f"#{invoice.doc_num}" if invoice.doc_num else f"DocEntry {invoice.doc_entry}"
    return (
        f"{escape(customer)} — <b>{escape(format_uzs_short(invoice.balance_due_tiyin))}</b>, "
        f"{invoice.days_overdue} d, {escape(doc)} ({escape(owner)})"
    )


def _by_owner(invoices: list[ARInvoice]) -> dict[str, int]:
    """Total overdue balance per responsible person.

    Args:
        invoices: Overdue invoices.

    Returns:
        Mapping of owner name to total overdue tiyin.
    """
    totals: dict[str, int] = defaultdict(int)
    for invoice in invoices:
        owner = invoice.sales_person_name or division_label(invoice.division)
        totals[owner] += invoice.balance_due_tiyin
    return dict(totals)


async def record_alerts(run_id: uuid.UUID, aging: ARAging, min_days: int, message_id: int | None) -> None:
    """Write one ``alerts`` row per aging bucket that has overdue money in it.

    Per-bucket rows (rather than one row for the whole message) let the CEO
    acknowledge the 90+ bucket while leaving the rest open, and give Power BI a
    clean series to trend.

    Args:
        run_id: UUID of this run.
        aging: The aging result.
        min_days: Threshold used for this run.
        message_id: Telegram message id, or ``None`` if nothing was sent.

    Raises:
        psycopg.Error: on a database failure.
    """
    import json

    overdue = [i for i in aging.invoices if i.days_overdue >= min_days]
    by_bucket: dict[str, list[ARInvoice]] = defaultdict(list)
    for invoice in overdue:
        by_bucket[invoice.aging_bucket].append(invoice)

    for bucket, invoices in by_bucket.items():
        total = sum(i.balance_due_tiyin for i in invoices)
        top = max(invoices, key=lambda i: i.balance_due_tiyin)
        await execute(
            """
            INSERT INTO alerts
                (agent, run_id, alert_key, severity, title, body,
                 amount_tiyin, owner, channel, status, telegram_message_id, context)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                AGENT,
                str(run_id),
                f"ar_overdue:{bucket}:{aging.snapshot_date.isoformat()}",
                BUCKET_SEVERITY.get(bucket, "info"),
                f"AR overdue {BUCKET_LABELS.get(bucket, bucket)}: {format_uzs(total)}",
                f"{len(invoices)} invoice(s); largest: {top.card_name or top.card_code}",
                total,
                top.sales_person_name or division_label(top.division),
                "telegram",
                "sent" if message_id else "failed",
                message_id,
                json.dumps(
                    {
                        "bucket": bucket,
                        "invoice_count": len(invoices),
                        "doc_entries": [i.doc_entry for i in invoices[:50]],
                    },
                    ensure_ascii=False,
                ),
            ),
        )


async def run(min_days: int = 1, dry_run: bool = False) -> int:
    """Pull AR aging, alert on it, and record what was sent.

    Args:
        min_days: Minimum days overdue to report.
        dry_run: Print the message instead of sending it.

    Returns:
        Process exit code — 0 on success, 1 if the alert could not be sent,
        2 if SAP could not be read or the config is incomplete.
    """
    if dry_run:
        settings.dry_run = True

    run_id = uuid.uuid4()
    log.info("Receivables run {} starting (min_days={}, dry_run={})", run_id, min_days, settings.dry_run)

    unfilled = settings.missing_placeholders()
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(unfilled))
        return 2

    try:
        aging = await collect(run_id)
    except Exception as exc:  # noqa: BLE001 — surface SAP failure as an alert, not a stack trace
        log.error("Could not read AR aging from SAP: {}", exc)
        try:
            async with TelegramBot(agent=AGENT, run_id=run_id) as bot:
                await bot.send_alert(
                    "Receivables agent could not reach SAP",
                    f"<code>{escape(str(exc)[:300])}</code>",
                    severity="critical",
                )
        except Exception as send_exc:  # noqa: BLE001
            log.error("Could not send the SAP failure alert either: {}", send_exc)
        return 2

    message = render(aging, min_days)

    message_id: int | None = None
    async with TelegramBot(agent=AGENT, run_id=run_id) as bot:
        if settings.dry_run:
            print(message)
        else:
            ids = await bot.send_message(message)
            message_id = ids[0] if ids else None

    await record_alerts(run_id, aging, min_days, message_id)

    if settings.dry_run:
        return 0
    return 0 if message_id else 1


async def _main(args: argparse.Namespace) -> int:
    """Run the agent and close the database pool.

    Args:
        args: Parsed CLI arguments.

    Returns:
        The process exit code.
    """
    try:
        return await run(min_days=args.min_days, dry_run=args.dry_run)
    finally:
        await close_pool()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Daily receivables alert.")
    parser.add_argument("--min-days", type=int, default=1, help="minimum days overdue to report")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
