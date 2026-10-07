"""The leads Google Sheet's columns, and the history of the lead hand-out.

The lead hand-out to B2B sales (one lead each morning, "how is it going?"
at 15:00 — 2026-09-30) was **stopped on 2026-10-07 by the Director**: "he
will do another thing", so the AI sends nothing to B2B Sotuv any more. The
agent (``agents/lead-handout``), its buttons and its questions are removed.
What stays:
  * the sheet helpers the Lead Agent and OPS Manager Bot use to read/write
    the leads sheet by header name;
  * ``describe`` — the hand-out's past assignments, for the Director's
    questions (the data in ``lead_assignments`` / ``lead_checkins`` is kept,
    never deleted).
"""

from __future__ import annotations

from typing import Any

# The Lead Agent's Google Sheet (agents/lead-agent writes it; selfcheck keeps
# the two column lists identical). Sales people may edit it (2026-10-02), so
# columns are found by their header, never by position: a moved or added
# column doesn't shift anything. Reads take a wide range for that reason.
SHEET_TAB = "Sheet1"
SHEET_RANGE = f"{SHEET_TAB}!A:T"
SHEET_READ_RANGE = f"{SHEET_TAB}!A:AZ"
SHEET_COLUMNS = [
    "company_name", "project_name", "industry", "location", "project_stage",
    "estimated_opening", "signal", "signal_source_url", "signal_date",
    "estimated_size", "contact_name", "contact_role", "contact_method",
    "confidence", "priority", "recheck_date", "notes", "date_added",
    "dedupe_key", "track",
]

# How a past assignment's status reads.
STATUS_LABELS = {"in_progress": "жараёнда", "dismissed": "рад этилди", "done": "бажарилди"}


# ------------------------------------------------------------ the sheet


def column_order(header: list[str]) -> list[str]:
    """The sheet's columns in their current order, read from its header row.

    When the header doesn't carry our column names (renamed or missing), the
    standard order is assumed — the sheet as the Lead Agent first wrote it.
    Extra columns people add are kept in place and simply not read.
    """
    names = [str(h).strip() for h in header or []]
    if len(set(names) & set(SHEET_COLUMNS)) >= 0.8 * len(SHEET_COLUMNS):
        return names
    return list(SHEET_COLUMNS)


def sheet_records(rows: list[list[str]]) -> list[dict[str, str]]:
    """The sheet's data rows as {column: value}, by header (``column_order``)."""
    if not rows:
        return []
    order = column_order(rows[0])
    return [
        {c: (row[i].strip() if i < len(row) and row[i] else "") for i, c in enumerate(order) if c}
        for row in rows[1:]
    ]


def row_for_sheet(values: dict[str, str], header: list[str] | None) -> list[str]:
    """One row to append, its values in the sheet's current column order."""
    return [str(values.get(c, "") or "") for c in column_order(header or [])]


def column_letter(count: int) -> str:
    """The letter of the ``count``-th column (1 → A, 27 → AA)."""
    letters = ""
    while count:
        count, rest = divmod(count - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


# ------------------------------------------------------- the Director's view


def describe(rows: list[dict[str, Any]]) -> str:
    """Plain-text data for OPS Manager Bot's answers about leads (last 30 days)."""
    if not rows:
        return ("The lead hand-out to sales people was stopped by the Director on 2026-10-07; "
                "no leads were handed out in the last 30 days.")
    lines = [
        "HISTORY ONLY: the Director stopped the lead hand-out to B2B sales on 2026-10-07 — nothing is "
        f"handed out or asked any more. Leads handed out in the last 30 days before that: {len(rows)}. "
        "'not answered yet' / 'жараёнда' are as they were when it stopped.",
    ]
    for r in rows:
        status = {"new": "not answered yet", **STATUS_LABELS}.get(r["status"], r["status"])
        extra = f" — {r['outcome']}" if r.get("outcome") else ""
        note = f" | last note: {r['last_note']}" if r.get("last_note") else ""
        lines.append(
            f"- {r['person']}: {r['company']} (given {r['assigned_on']:%Y-%m-%d}) | {status}{extra}{note}"
        )
    return "\n".join(lines)
