"""The leads Google Sheet kept readable (2026-11-03, the owner).

«Delete all the leads added before DeepSeek and reorder them; when a lead is sent to B2B Sotuv, colour it — when I
open the sheet it's hard to know which one was sent.»

  * **Archive** (Admin Bot ``/lidlar arxiv`` → preview → ✅): rows added before ``CUTOFF`` (DeepSeek's first run) are
    moved to the tab «Архив» — copied there first, then removed from «Sheet1»; nothing is lost (Google also keeps the
    sheet's history; delete the «Архив» tab yourself if it isn't wanted). The same leads leave the hand-out queue
    (``leads.archived_at``); ones already given keep their history.
  * **Every morning** after the hand-out (``refresh``): the sheet sorted newest first (by «date_added», Sheets' own
    sort — colours and people's notes move with their rows), each row's lead number in «№», and every lead given to
    B2B Sotuv marked «B2B'га берилди: 03.11 · Абдукарим Зунунов» with a light green row. People's own colours on
    other rows are left alone.
Columns are found by their header (people edit the sheet); the two new ones are added at the end if missing.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from integrations.common.db import execute, fetch_all
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import leads

AGENT = "lead-sheet"
log = setup_logging(AGENT)

CUTOFF = date(2026, 10, 31)  # DeepSeek web search's first run (deepseek_web.py: every third day from 31.10)
ARCHIVE_TAB = "Архив"
NUMBER_COL, GIVEN_COL = "№", "B2B'га берилди"
GREEN = {"red": 0.85, "green": 0.94, "blue": 0.85}
PREFIX = "lsa"  # Admin Bot: lsa:go / lsa:no


def key_of(record: dict[str, str]) -> str:
    """The row's dedupe key, as the import gives it (``leads.rows_to_leads``)."""
    name = record.get("company_name") or record.get("project_name") or ""
    return record.get("dedupe_key") or f"{name.lower()}|{record.get('track') or ''}"


def column(n: int) -> str:
    return leads.column_letter(n)


def plan(rows: list[list[str]], cutoff: date) -> dict[str, Any]:
    """Which data rows are old (to the archive) and which stay — by «date_added»; a row without a date stays."""
    if not rows:
        return {"old": [], "keep": [], "header": []}
    header, records = rows[0], leads.sheet_records(rows)
    old, keep = [], []
    for i, rec in enumerate(records, start=1):  # i = the row's index in ``rows`` (0 = header)
        added = leads._date(rec.get("date_added", ""))  # noqa: SLF001 — the sheet's own date reader
        (old if added and added < cutoff else keep).append(i)
    return {"old": old, "keep": keep, "header": header}


def row_spans(indexes: list[int]) -> list[tuple[int, int]]:
    """Contiguous [start, end) spans of row indexes, last first (so deleting one doesn't move the next)."""
    spans: list[list[int]] = []
    for i in sorted(indexes):
        if spans and spans[-1][1] == i:
            spans[-1][1] = i + 1
        else:
            spans.append([i, i + 1])
    return [tuple(s) for s in reversed(spans)]


async def _given() -> dict[str, dict[str, Any]]:
    """dedupe_key → the lead's number and, if handed out, when and to whom."""
    rows = await fetch_all(
        """SELECT l.dedupe_key, l.id, a.assigned_on, coalesce(nullif(btrim(e.full_name), ''), e.display_name) AS person
           FROM leads l LEFT JOIN lead_assignments a ON a.lead_id = l.id LEFT JOIN employees e ON e.id = a.employee_id""")
    return {r["dedupe_key"]: r for r in rows}


async def preview(run_id: uuid.UUID | None = None) -> dict[str, int]:
    """Counts for the admin before archiving: rows, old ones, staying, already given."""
    from integrations.google.sheets_client import SheetsClient

    async with SheetsClient(agent=AGENT, run_id=run_id) as sheets:
        rows = await sheets.get_values(leads.SHEET_READ_RANGE)
    p = plan(rows, CUTOFF)
    given = await _given()
    records = leads.sheet_records(rows)
    return {"rows": len(records), "old": len(p["old"]), "keep": len(p["keep"]),
            "given": sum(1 for r in records if (given.get(key_of(r)) or {}).get("assigned_on"))}


async def archive(run_id: uuid.UUID | None = None) -> dict[str, int]:
    """Old rows to «Архив» (copied first, then removed), the same leads out of the hand-out queue, then ``refresh``."""
    from integrations.google.sheets_client import SheetsClient

    async with SheetsClient(agent=AGENT, run_id=run_id) as sheets:
        rows = await sheets.get_values(leads.SHEET_READ_RANGE)
        p = plan(rows, CUTOFF)
        if p["old"]:
            tabs = await sheets.tab_ids()
            if ARCHIVE_TAB not in tabs:
                await sheets.add_tab(ARCHIVE_TAB)
                tabs = await sheets.tab_ids()
            if not await sheets.get_values(f"{ARCHIVE_TAB}!A1:A2"):
                await sheets.append_rows(f"{ARCHIVE_TAB}!A1", [list(p["header"])])
            copied = await sheets.append_rows(f"{ARCHIVE_TAB}!A1", [rows[i] for i in p["old"]])
            if copied != len(p["old"]):  # never remove what wasn't copied
                raise RuntimeError(f"archive copied {copied} of {len(p['old'])} rows")
            sid = tabs[leads.SHEET_TAB]
            await sheets.batch([{"deleteDimension": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": a, "endIndex": b}}}
                                for a, b in row_spans(p["old"])], f"{len(p['old'])} rows to {ARCHIVE_TAB}")
    keys = [key_of(r) for r in leads.sheet_records([p["header"], *[rows[i] for i in p["old"]]])] if p["old"] else []
    if keys:
        await execute("""UPDATE leads l SET archived_at = now() WHERE l.dedupe_key = ANY(%s) AND l.archived_at IS NULL
                         AND NOT EXISTS (SELECT 1 FROM lead_assignments a WHERE a.lead_id = l.id)""", (keys,))
    await execute("UPDATE leads SET archived_at = now() WHERE date_added < %s AND archived_at IS NULL "
                  "AND id NOT IN (SELECT lead_id FROM lead_assignments)", (CUTOFF,))
    done = await refresh(run_id)
    return {"archived": len(p["old"]), "kept": len(p["keep"]), **done}


async def refresh(run_id: uuid.UUID | None = None) -> dict[str, int]:
    """Newest first, the lead numbers, the given ones marked and green. Counts."""
    from integrations.google.sheets_client import SheetsClient

    given = await _given()
    async with SheetsClient(agent=AGENT, run_id=run_id) as sheets:
        rows = await sheets.get_values(leads.SHEET_READ_RANGE)
        if len(rows) < 2:
            return {"rows": 0, "marked": 0}
        header = [str(h).strip() for h in rows[0]]
        adds = [c for c in (NUMBER_COL, GIVEN_COL) if c not in header]
        if adds:
            start = len(header) + 1
            await sheets.set_values([(f"{leads.SHEET_TAB}!{column(start)}1:{column(start + len(adds) - 1)}1", [adds])])
            header += adds
        sid = (await sheets.tab_ids())[leads.SHEET_TAB]
        if "date_added" in header:
            await sheets.batch([{"sortRange": {
                "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": len(rows), "startColumnIndex": 0, "endColumnIndex": len(header)},
                "sortSpecs": [{"dimensionIndex": header.index("date_added"), "sortOrder": "DESCENDING"}]}}], "sort newest first")
            rows = await sheets.get_values(leads.SHEET_READ_RANGE)
        records = leads.sheet_records([header, *rows[1:]])
        n_col, g_col = column(header.index(NUMBER_COL) + 1), column(header.index(GIVEN_COL) + 1)
        updates, green = [], []
        for i, rec in enumerate(records, start=2):  # sheet row numbers (1 = header)
            info = given.get(key_of(rec))
            if not info:
                continue
            number = str(info["id"])
            mark = f"{info['assigned_on']:%d.%m} · {info['person']}" if info.get("assigned_on") else ""
            if rec.get(NUMBER_COL, "") != number:
                updates.append((f"{leads.SHEET_TAB}!{n_col}{i}", [[number]]))
            if mark and rec.get(GIVEN_COL, "") != mark:
                updates.append((f"{leads.SHEET_TAB}!{g_col}{i}", [[mark]]))
            if mark:
                green.append(i - 1)  # 0-based row index
        await sheets.set_values(updates)
        await sheets.batch([{"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": a, "endRowIndex": b, "startColumnIndex": 0, "endColumnIndex": len(header)},
            "cell": {"userEnteredFormat": {"backgroundColor": GREEN}}, "fields": "userEnteredFormat.backgroundColor"}}
            for a, b in row_spans(green)], f"{len(green)} given rows green")
    return {"rows": len(records), "marked": len(green)}
