"""Receives pushes from the SAP gateway's own machine.

The gateway (``SAP_B1_AI_AGENT_TEACHING.md``) is deliberately loopback-only
and stays that way — instead of this service reaching in, the gateway's own
machine reaches out to these endpoints on a schedule, with a plain
PowerShell script (no runtime install needed beyond what's already on any
Windows machine — see ``scripts/sap-gateway-push/``).

Two paths:
  handle_ar_aging_push — get_invoices specifically, into the richer,
      bucketed ``ar_aging_snapshots`` table. Reuses the exact same
      bucketing/currency-conversion logic ``integrations/sap/client.py``
      uses for the real Service Layer, rather than re-implementing it a
      second time on the pushing machine.
  handle_gateway_push — every other tool (orders/products/customers/
      warehouses/inventory/payments), into the generic
      ``sap_gateway_snapshots`` table. Generic because the gateway's exact
      response shape for these six isn't confirmed the way get_sales/
      get_invoices' was (verified against a real documented example) --
      raw rows are kept in full (``raw`` JSONB column) so nothing is lost
      even if the best-effort key extraction below guesses a field name
      that turns out to be wrong on the real gateway.
"""

from __future__ import annotations

import hashlib
import json
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


# ------------------------------------------------------- the other 6 tools

VALID_TOOLS = {"orders", "products", "customers", "warehouses", "inventory", "payments"}

# Candidate field names to try, in order, per tool -- SAP Business One's
# well-established standard names, NOT confirmed against a live response
# for these six (see the module docstring). First match wins; if none of a
# tool's candidates are present in a row, natural_key falls back to a hash
# of the whole row so the push never fails outright on an unrecognized shape.
_KEY_CANDIDATES: dict[str, list[str] | list[list[str]]] = {
    "orders": ["DocEntry", "DocNum"],
    "products": ["ItemCode", "Code"],
    "customers": ["CardCode", "Code"],
    "warehouses": ["WhsCode", "WarehouseCode", "Code"],
    "payments": ["DocEntry", "DocNum"],
    # inventory rows are one (item, warehouse) pair -- needs both parts, not
    # just the first match, or two different items in the same warehouse
    # would collide onto the same key.
    "inventory": [["ItemCode", "WhsCode"], ["item_code", "warehouse"]],
}


def _extract_natural_key(tool: str, row: dict[str, Any]) -> str:
    """Best-effort stable key for a pushed row, for upsert deduplication.

    Args:
        tool: One of ``VALID_TOOLS``.
        row: One raw row as the gateway returned it.

    Returns:
        A field value (or "field1:field2" for inventory's compound key) if
        any candidate field is present, else a stable hash of the whole row
        — never fails, so an unrecognized response shape still gets stored
        (as its raw JSON) rather than dropped.
    """
    candidates = _KEY_CANDIDATES.get(tool, [])
    for candidate in candidates:
        if isinstance(candidate, list):
            values = [row.get(field) for field in candidate]
            if all(v is not None for v in values):
                return ":".join(str(v) for v in values)
        elif row.get(candidate) is not None:
            return str(row[candidate])

    # No known field matched -- hash the row so this tool's response shape
    # can be inspected (via the raw column) and _KEY_CANDIDATES corrected,
    # instead of the push failing or silently skipping the row.
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    return f"unrecognized:{digest[:16]}"


async def handle_gateway_push(tool: str, payload: dict[str, Any], run_id: uuid.UUID) -> dict[str, Any]:
    """Upsert a batch of raw rows from one gateway tool into ``sap_gateway_snapshots``.

    Args:
        tool: One of ``VALID_TOOLS`` — which gateway tool these rows came from.
        payload: ``{"rows": [...]}`` — raw rows exactly as that tool returned them.
        run_id: UUID grouping this call's audit rows.

    Returns:
        ``{"ok": True, "written": int}`` on success, or
        ``{"ok": False, "error": "..."}`` if ``tool`` isn't recognized.
    """
    if tool not in VALID_TOOLS:
        return {"ok": False, "error": f"unknown tool '{tool}', expected one of {sorted(VALID_TOOLS)}"}

    rows = payload.get("rows") or []
    snapshot_date = today_local()
    written = 0

    async with audited(
        agent="sap-gateway-push",
        action=f"gateway_push_{tool}",
        target_system="postgres",
        run_id=run_id,
        target_ref="sap_gateway_snapshots",
        mode="write",
        payload={"tool": tool, "rows_received": len(rows)},
    ) as ctx:
        for row in rows:
            natural_key = _extract_natural_key(tool, row)
            await execute(
                """
                INSERT INTO sap_gateway_snapshots (tool, snapshot_date, natural_key, raw)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tool, snapshot_date, natural_key) DO UPDATE SET
                    raw = EXCLUDED.raw,
                    captured_at = now()
                """,
                (tool, snapshot_date, natural_key, json.dumps(row, ensure_ascii=False, default=str)),
            )
            written += 1

        ctx["payload"]["written"] = written

    return {"ok": True, "written": written}
