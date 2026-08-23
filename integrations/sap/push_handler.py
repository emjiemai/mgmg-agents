"""Receives an AR-aging push from the SAP gateway's own machine.

The gateway (``SAP_B1_AI_AGENT_TEACHING.md``) is deliberately loopback-only
and stays that way — instead of this service reaching in, the gateway's own
machine reaches out to this endpoint on a schedule, with a plain PowerShell
script (no runtime install needed beyond what's already on any Windows
machine — see ``scripts/sap-gateway-push/``).

Deliberately reuses the exact same bucketing/currency-conversion logic
``integrations/sap/client.py`` uses for the real Service Layer, rather than
re-implementing it a second time on the pushing machine — one source of
truth for "what counts as overdue," not two that could drift apart.
"""

from __future__ import annotations

import uuid
from typing import Any

from integrations.common.db import audited, execute
from integrations.common.money import to_tiyin
from integrations.common.timeutil import days_between, parse_sap_date, today_local
from integrations.sap.client import aging_bucket


async def handle_ar_aging_push(payload: dict[str, Any], run_id: uuid.UUID) -> dict[str, Any]:
    """Upsert a batch of open invoices into ``ar_aging_snapshots``.

    Args:
        payload: ``{"invoices": [...]}`` — raw rows as the gateway's
            ``get_invoices`` tool returns them (``DocEntry``, ``DocNum``,
            ``CardCode``, ``CardName``, ``DocDate``, ``DocDueDate``,
            ``DocTotal``, ``DocCur``, ``DocStatus``, ``CANCELED``,
            ``SlpCode``).
        run_id: UUID grouping this call's audit rows.

    Returns:
        ``{"ok": True, "written": int, "skipped": int}``. A row is skipped
        if it isn't open+non-cancelled, or is missing ``DocEntry``/``CardCode``
        (both NOT NULL in the destination table).
    """
    raw_invoices = payload.get("invoices") or []
    as_of = today_local()
    written = 0
    skipped = 0

    async with audited(
        agent="sap-gateway-push",
        action="ar_aging_push",
        target_system="postgres",
        run_id=run_id,
        target_ref="ar_aging_snapshots",
        mode="write",
        payload={"rows_received": len(raw_invoices)},
    ) as ctx:
        for row in raw_invoices:
            if row.get("DocStatus") != "O" or row.get("CANCELED") != "N":
                skipped += 1
                continue

            doc_entry = row.get("DocEntry")
            card_code = row.get("CardCode")
            if not doc_entry or not card_code:
                skipped += 1
                continue

            due_date = parse_sap_date(row.get("DocDueDate"))
            overdue = max(days_between(due_date, as_of), 0) if due_date else 0
            doc_total_tiyin = to_tiyin(row.get("DocTotal"))
            # The gateway's get_invoices doesn't return PaidToDate yet, so
            # this can't distinguish a partially-paid open invoice from an
            # untouched one -- balance == total until that field is added
            # gateway-side. Flagged in the push script's own README too.
            paid_to_date_tiyin = 0
            balance_due_tiyin = doc_total_tiyin - paid_to_date_tiyin

            slp_code_raw = row.get("SlpCode")
            slp_code = (
                int(slp_code_raw)
                if isinstance(slp_code_raw, (int, float)) and int(slp_code_raw) != -1
                else None
            )

            await execute(
                """
                INSERT INTO ar_aging_snapshots
                    (snapshot_date, division, doc_entry, doc_num, card_code, card_name,
                     doc_date, due_date, days_overdue, aging_bucket, currency,
                     doc_total_tiyin, paid_to_date_tiyin, balance_due_tiyin,
                     sales_person_code, sales_person_name)
                VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                ON CONFLICT (snapshot_date, doc_entry) DO UPDATE SET
                    doc_num = EXCLUDED.doc_num,
                    card_code = EXCLUDED.card_code,
                    card_name = EXCLUDED.card_name,
                    doc_date = EXCLUDED.doc_date,
                    due_date = EXCLUDED.due_date,
                    days_overdue = EXCLUDED.days_overdue,
                    aging_bucket = EXCLUDED.aging_bucket,
                    currency = EXCLUDED.currency,
                    doc_total_tiyin = EXCLUDED.doc_total_tiyin,
                    paid_to_date_tiyin = EXCLUDED.paid_to_date_tiyin,
                    balance_due_tiyin = EXCLUDED.balance_due_tiyin,
                    sales_person_code = EXCLUDED.sales_person_code
                """,
                (
                    as_of,
                    doc_entry,
                    row.get("DocNum"),
                    card_code,
                    row.get("CardName"),
                    parse_sap_date(row.get("DocDate")),
                    due_date,
                    overdue,
                    aging_bucket(overdue),
                    row.get("DocCur") or "UZS",
                    doc_total_tiyin,
                    paid_to_date_tiyin,
                    balance_due_tiyin,
                    slp_code,
                ),
            )
            written += 1

        ctx["payload"]["written"] = written
        ctx["payload"]["skipped"] = skipped

    return {"ok": True, "written": written, "skipped": skipped}
