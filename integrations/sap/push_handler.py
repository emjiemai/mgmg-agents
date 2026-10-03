"""Receives pushes from the SAP gateway's own machine.

The gateway (``SAP_B1_AI_AGENT_TEACHING.md``) is deliberately loopback-only
and stays that way — instead of this service reaching in, the gateway's own
machine reaches out to these endpoints on a schedule, with a plain
PowerShell script (no runtime install needed beyond what's already on any
Windows machine — see ``scripts/sap-gateway-push/``).

Three paths:
  handle_full_push — (2026-10-03) the push script reads SAP's own database
      directly and sends every row of each kind (``FULL_DATASETS``), with
      no row cap. Open invoices replace today's ``ar_aging_snapshots``;
      everything else replaces today's rows in ``sap_gateway_snapshots``.
      Field names are the database's own columns, checked against the SAP
      export of 2026-10-02. Each push is marked ``complete`` in its audit
      row, so no figure built from it is shown as "камида".
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
from datetime import date, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import audited, connection, execute, fetch_all
from integrations.common.logging_setup import setup_logging
from integrations.common.money import to_tiyin
from integrations.common.timeutil import days_between, parse_sap_date, today_local
from integrations.sap.aging import aging_bucket

log = setup_logging("sap-push-handler")

# Candidate field names for an invoice's currency, kept for the next time
# someone has gateway access to actually check (see module note below) --
# "DocCur" is OINV's real SQL column name and was the only one ever tried
# (2026-08 to 2026-09); "DocCurrency" is the Service Layer OData property
# name for the same column, in case the gateway normalizes toward that
# instead. NOT currently used to decide anything (see _extract_currency) --
# two rounds of shipping "detect it, fall back to USD" did not produce a
# dollar sign in production, and rather than ship a third guess, this now
# just always returns USD. Real diagnosis (what does the gateway actually
# send, field-by-field) is deferred -- pull it back into _extract_currency's
# candidate-matching logic once someone can inspect a raw gateway response
# directly, per the module note below.
_CURRENCY_CANDIDATES = ("DocCur", "DocCurrency", "Currency", "currency")


def _extract_currency(row: dict[str, Any]) -> str:
    """Always SAP_DEFAULT_CURRENCY for now -- see the module note above.

    Deliberately not trying _CURRENCY_CANDIDATES against ``row`` anymore.
    Two rounds of "detect the real field, fall back to USD if none match"
    still showed so'm in production, with no way from here to tell whether
    that was a deploy-timing issue, a wrong candidate list, or something
    else entirely -- rather than ship a third unverified guess, this just
    always returns the default, which is correct today because every known
    SAP AR invoice at this business is USD-denominated. Revisit once
    real gateway data can actually be inspected (see the module note).

    Args:
        row: One raw invoice row from the gateway's get_invoices. Currently
            unused -- kept as a parameter so this function's call site
            doesn't change shape when real detection comes back.

    Returns:
        ``settings.sap_default_currency``.
    """
    del row  # not read yet -- see docstring
    return settings.sap_default_currency


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
                    _extract_currency(row),
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


# ------------------------------------------------- full pushes (2026-10-03)

# Every kind of data the push script sends straight from SAP's database, and
# the columns that identify one row of it (the script's SELECTs use these
# exact names). "ar_open" goes to ar_aging_snapshots; the rest are stored
# under the same name as a sap_gateway_snapshots tool — the six names the
# gateway already used keep their meaning, so every reader keeps working.
FULL_DATASETS: dict[str, tuple[str, ...]] = {
    "ar_open": ("DocEntry",),                      # OINV, open
    "sales": ("ObjType", "DocEntry"),              # OINV + ORIN, last 45 days
    "sales_lines": ("ObjType", "DocEntry", "LineNum"),  # INV1 + RIN1 of those
    "inventory": ("ItemCode", "WhsCode"),          # OITW + OITM, non-zero
    "products": ("ItemCode",),                     # OITM
    "customers": ("CardCode",),                    # OCRD
    "warehouses": ("WhsCode",),                    # OWHS
    "sales_people": ("SlpCode",),                  # OSLP
    "payments": ("DocEntry",),                     # ORCT, last 45 days
    "payments_out": ("DocEntry",),                 # OVPM, last 45 days
    "orders": ("DocEntry",),                       # ORDR, open
    "ap_open": ("DocEntry",),                      # OPCH, open
    "po_open": ("DocEntry", "LineNum"),            # OPOR + POR1, open lines
    "equipment": ("insID",),                       # OINS
    "service_calls": ("callID",),                  # OSCL
    "service_contracts": ("ContractID",),          # OCTR
}

# A full push is one POST per kind; the biggest today is ~2,100 rows.
MAX_FULL_ROWS = 20_000
# Older days of a fully pushed kind are deleted (the brief keeps its own
# history of the five numbers); a few days are kept for "what changed".
KEEP_DAYS = 3


def full_key(dataset: str, row: dict[str, Any]) -> str:
    """The row's identity from its ``FULL_DATASETS`` columns, else a hash."""
    columns = FULL_DATASETS[dataset]
    values = [row.get(c) for c in columns]
    if all(v is not None and v != "" for v in values):
        return ":".join(str(v) for v in values)
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    return f"unrecognized:{digest[:16]}"


def aging_row(row: dict[str, Any], as_of: date, slp_names: dict[int, str]) -> tuple | None:
    """One open SAP invoice as an ``ar_aging_snapshots`` row, or None if unusable.

    ``DocTotal``/``PaidToDate`` are in SAP's local currency (USD here) even
    for invoices written in so'm, so the balance is too.
    """
    doc_entry, card_code = row.get("DocEntry"), row.get("CardCode")
    if not doc_entry or not card_code:
        return None
    if str(row.get("DocStatus") or "O") != "O" or str(row.get("CANCELED") or "N") != "N":
        return None
    total = to_tiyin(row.get("DocTotal"))
    paid = to_tiyin(row.get("PaidToDate"))
    if total - paid <= 0:
        return None
    due = parse_sap_date(str(row.get("DocDueDate") or ""))
    overdue = max(days_between(due, as_of), 0) if due else 0
    try:
        slp = int(row.get("SlpCode"))
    except (TypeError, ValueError):
        slp = -1
    return (
        as_of, int(doc_entry), row.get("DocNum"), str(card_code), row.get("CardName"),
        parse_sap_date(str(row.get("DocDate") or "")), due, overdue, aging_bucket(overdue),
        settings.sap_default_currency, total, paid, total - paid,
        slp if slp >= 0 else None, slp_names.get(slp) if slp >= 0 else None,
    )


async def _sales_people() -> dict[int, str]:
    """SlpCode → name from the latest pushed OSLP, for the receivables' "owner"."""
    names: dict[int, str] = {}
    for r in await fetch_all("SELECT raw FROM v_sap_gateway_latest WHERE tool = 'sales_people'"):
        raw = r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"])
        try:
            names[int(raw.get("SlpCode"))] = str(raw.get("SlpName") or "").strip()
        except (TypeError, ValueError):
            continue
    return names


async def handle_full_push(dataset: str, payload: dict[str, Any], run_id: uuid.UUID) -> dict[str, Any]:
    """Replace today's rows of one kind with a complete push from SAP's database.

    Args:
        dataset: One of ``FULL_DATASETS``.
        payload: ``{"rows": [...]}`` — every row of that kind, as the script's
            SELECT returned them.
        run_id: UUID grouping this call's audit rows.

    Returns:
        ``{"ok": True, "written": int, "skipped": int}``, or
        ``{"ok": False, "error": ...}`` for an unknown kind or a bad body.
    """
    if dataset not in FULL_DATASETS:
        return {"ok": False, "error": f"unknown dataset '{dataset}', expected one of {sorted(FULL_DATASETS)}"}
    rows = payload.get("rows")
    if isinstance(rows, dict):  # PowerShell sends a one-row list as a bare object
        rows = [rows]
    if not isinstance(rows, list):
        return {"ok": False, "error": "body must be {\"rows\": [...]}"}
    if len(rows) > MAX_FULL_ROWS:
        return {"ok": False, "error": f"too many rows ({len(rows)} > {MAX_FULL_ROWS})"}
    rows = [r for r in rows if isinstance(r, dict)]
    today = today_local()

    if dataset == "ar_open":
        slp_names = await _sales_people()
        params = [p for p in (aging_row(r, today, slp_names) for r in rows) if p is not None]
        async with audited(
            agent="sap-gateway-push", action="ar_aging_push", target_system="postgres", run_id=run_id,
            target_ref="ar_aging_snapshots", mode="write",
            payload={"rows_received": len(rows), "complete": True, "source": "database"},
        ) as ctx:
            async with connection() as conn:
                async with conn.cursor() as cur:
                    # Today's snapshot is replaced, so an invoice paid since
                    # the last push stops counting as debt right away.
                    await cur.execute("DELETE FROM ar_aging_snapshots WHERE snapshot_date = %s", (today,))
                    if params:
                        await cur.executemany(
                            """
                            INSERT INTO ar_aging_snapshots
                                (snapshot_date, doc_entry, doc_num, card_code, card_name, doc_date, due_date,
                                 days_overdue, aging_bucket, currency, doc_total_tiyin, paid_to_date_tiyin,
                                 balance_due_tiyin, sales_person_code, sales_person_name)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (snapshot_date, doc_entry) DO NOTHING
                            """,
                            params,
                        )
            ctx["payload"].update(written=len(params), skipped=len(rows) - len(params))
        return {"ok": True, "written": len(params), "skipped": len(rows) - len(params)}

    keyed = {full_key(dataset, r): r for r in rows}  # a repeated key keeps its last row
    async with audited(
        agent="sap-gateway-push", action=f"gateway_push_{dataset}", target_system="postgres", run_id=run_id,
        target_ref="sap_gateway_snapshots", mode="write",
        payload={"tool": dataset, "rows_received": len(rows), "complete": True, "source": "database"},
    ) as ctx:
        async with connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM sap_gateway_snapshots WHERE tool = %s AND (snapshot_date = %s OR snapshot_date < %s)",
                    (dataset, today, today - timedelta(days=KEEP_DAYS)),
                )
                if keyed:
                    await cur.executemany(
                        "INSERT INTO sap_gateway_snapshots (tool, snapshot_date, natural_key, raw) VALUES (%s, %s, %s, %s)",
                        [
                            (dataset, today, key, json.dumps(row, ensure_ascii=False, default=str))
                            for key, row in keyed.items()
                        ],
                    )
        ctx["payload"]["written"] = len(keyed)
    return {"ok": True, "written": len(keyed), "skipped": len(rows) - len(keyed)}
