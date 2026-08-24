"""Agent 3 — Receivables Agent.

Reads the latest AR aging snapshot (pushed periodically by the SAP gateway's
own machine — see ``scripts/sap-gateway-push/`` — into ``ar_aging_snapshots``),
sorts what is overdue into buckets, and sends the CEO a Telegram alert naming
the amount, the customer and the person responsible for collecting it.

This agent does NOT call SAP directly: SAP B1's Service Layer was never
reachable from this service (confirmed unreachable both directly and via the
gateway's own machine, which is deliberately loopback-only by design — see
``docs/agent-specs`` for the reachability investigation). Instead the
gateway's machine pushes a fresh snapshot on its own schedule, and this agent
just reads the most recent one — the same data OPS Manager Bot's Finance
Agent answers Director questions from.

The alert is deliberately owner-first: an aging report nobody owns does not get
collected. Where the data has no sales employee on the invoice, the division
owner is named instead, and unmapped invoices are shown as "Other" rather than
being dropped.

Read-only: this agent never writes to SAP or the gateway, sends no email, and
creates no documents. Its only side effects are the Telegram message and its
own rows in ``alerts`` and ``agent_actions``.

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

from integrations.common.config import settings
from integrations.common.db import close_pool, execute, fetch_all
from integrations.common.divisions import label as division_label
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_uzs, format_uzs_short
from integrations.common.timeutil import fmt_date
from integrations.org_bot.notify import notify_directors
from integrations.sap.models import ARAging, ARInvoice
from integrations.telegram.bot import escape

AGENT = "receivables"
log = setup_logging(AGENT)

BUCKET_ORDER = ["90_plus", "61_90", "31_60", "1_30"]
BUCKET_LABELS = {
    "1_30": "1–30 kun",
    "31_60": "31–60 kun",
    "61_90": "61–90 kun",
    "90_plus": "90+ kun",
}
BUCKET_SEVERITY = {"1_30": "info", "31_60": "warning", "61_90": "warning", "90_plus": "critical"}
BUCKET_EMOJI = {"1_30": "🟢", "31_60": "🟡", "61_90": "🟡", "90_plus": "🔴"}

# Invoices shown per bucket before the rest are summarized.
MAX_PER_BUCKET = 5


async def collect(run_id: uuid.UUID) -> ARAging:
    """Read the latest AR aging snapshot, pushed by the SAP gateway's own
    machine (see ``scripts/sap-gateway-push/`` and
    ``integrations/sap/push_handler.py``).

    Not a live SAP call: the SAP B1 Service Layer was never reachable from
    this service (confirmed unreachable both directly and via the gateway's
    own machine, which is deliberately loopback-only) — the gateway's
    machine pushes a fresh snapshot into ``ar_aging_snapshots`` on its own
    schedule instead, and this just reads whatever's most recently landed
    there. ``run_id`` is accepted for signature compatibility with the
    previous SAP-direct version; nothing here needs to audit a local read
    the way an external API call would.

    Args:
        run_id: Unused — kept for call-site compatibility.

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
            f"🟢 <b>Debitorlik qarzlari — {fmt_date(aging.snapshot_date)}</b>\n\n"
            f"{min_days}+ kundan ortiq muddati o'tgan qarz yo'q. "
            f"Jami ochiq: {escape(format_uzs_short(aging.total_open_tiyin))}."
        )

    total_overdue = sum(i.balance_due_tiyin for i in overdue)
    critical = aging.bucket_totals_tiyin.get("90_plus", 0)
    headline = "🔴" if critical > 0 else "🟡"

    lines = [
        f"{headline} <b>Debitorlik qarzlari — {fmt_date(aging.snapshot_date)}</b>",
        "",
        f"<b>Muddati o'tgan: {escape(format_uzs(total_overdue))}</b> ({len(overdue)} ta hisob-faktura)",
        f"Jami ochiq debitorlik: {escape(format_uzs_short(aging.total_open_tiyin))}",
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
                f"   <i>+yana {len(invoices) - MAX_PER_BUCKET} ta, "
                f"{escape(format_uzs_short(rest))}</i>"
            )
        lines.append("")

    owners = _by_owner(overdue)
    if owners:
        lines.append("<b>Mas'ul xodim bo'yicha:</b>")
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
    doc = f"#{invoice.doc_num}" if invoice.doc_num else f"Hujjat {invoice.doc_entry}"
    return (
        f"{escape(customer)} — <b>{escape(format_uzs_short(invoice.balance_due_tiyin))}</b>, "
        f"{invoice.days_overdue} kun, {escape(doc)} ({escape(owner)})"
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
                f"Muddati o'tgan debitorlik {BUCKET_LABELS.get(bucket, bucket)}: {format_uzs(total)}",
                f"{len(invoices)} ta hisob-faktura; eng kattasi: {top.card_name or top.card_code}",
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
        2 if no snapshot is available yet or the config is incomplete.
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
    except Exception as exc:  # noqa: BLE001 — surface the failure as an alert, not a stack trace
        log.error("Could not read the AR aging snapshot: {}", exc)
        try:
            await notify_directors(
                f"<code>{escape(str(exc)[:300])}</code>",
                agent=AGENT,
                run_id=run_id,
                severity="critical",
                title="Debitorlik agenti: hisobot ma'lumotlari mavjud emas",
            )
        except Exception as send_exc:  # noqa: BLE001
            log.error("Could not send the failure alert either: {}", send_exc)
        return 2

    message = render(aging, min_days)

    message_id: int | None = None
    if settings.dry_run:
        print(message)
    else:
        ids = await notify_directors(message, agent=AGENT, run_id=run_id)
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
