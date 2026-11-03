"""Excel workbooks the bot builds: names from SAP / 1C / Billz never become formulas (2026-11-03 review).

openpyxl writes a string that starts with «=» as a formula — a counterparty or product named «=HYPERLINK(…)» would run
when the workbook is opened (formula injection). Every workbook passes through ``as_text`` before it's saved.
"""

from __future__ import annotations

from typing import Any


def as_text(wb: Any) -> Any:
    """Every cell that would be a formula is kept as plain text — the bot never writes real formulas."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    return wb
