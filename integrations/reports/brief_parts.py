"""The pieces both morning-brief pictures fill ``brief.html`` with (2026-10-14).

The Director's brief (``agents/ceo-daily-brief``) and the Garmin manager's
(``agents/garmin-brief``) use the same template, so they build its figure
rows and blocks — and read SAP's pushed snapshots — the same way, here. A
key the template reads (StrictUndefined) is added in one place.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from integrations.common.db import fetch_all
from integrations.common.money import NBSP

MAX_LINES = 5


def split_value(value: str) -> tuple[str, str]:
    """"4,57 млрд сўм" -> ("4,57", "млрд сўм"): the number large, its unit small."""
    number, _, unit = value.partition(NBSP)
    return number, unit.replace(NBSP, " ")


def figure_row(label: str, icon: str) -> dict[str, Any]:
    """One of the brief's figures, empty — every key the template reads."""
    return {"label": label, "icon": icon, "tone": "plain", "detail": None, "number": "", "unit": "",
            "lower": False, "sub": None, "sub_dir": None, "sub_alert": False}


def people_block(title: str, icon: str, day: str | None, summary: str, tone: str,
                 rows: list[dict[str, Any]], empty: str | None = None) -> dict[str, Any]:
    """A block under the figures: a head line and at most ``MAX_LINES`` rows ("+N")."""
    return {"title": title, "icon": icon, "day": day, "summary": summary, "tone": tone,
            "rows": rows[:MAX_LINES], "more": max(len(rows) - MAX_LINES, 0), "empty": empty}


async def gateway_snapshot(tool: str) -> tuple[date | None, datetime | None, list[dict[str, Any]]]:
    """The latest pushed snapshot of one SAP gateway tool: (its day, when it arrived, raw rows)."""
    rows = await fetch_all("SELECT snapshot_date, captured_at, raw FROM v_sap_gateway_latest WHERE tool = %s", (tool,))
    if not rows:
        return None, None, []
    raw = [r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"] or "{}") for r in rows]
    return rows[0]["snapshot_date"], max(r["captured_at"] for r in rows), raw
