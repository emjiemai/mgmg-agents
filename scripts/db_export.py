"""The bot's own tables as PDFs — read-only (the owner, 2026-10-17).

Two files:

  * ``topshiriqlar.pdf`` — every task (``tasks``) with who got it, its state,
    deadline and history count; tasks the Director sent to several people are
    shown together, so "one task in three rows" is visible as what it is.
  * ``jadvallar.pdf`` — every other table of the bot (employees, reports,
    KPI, permissions, complaints, cheques, logs, …): row count, columns, rows.

**Never** the platforms' data (SAP, Billz, 1C, Verifix, old amoCRM / CRM /
Planner snapshots) — ``PLATFORM_TABLES`` / ``PLATFORM_PREFIXES``. Views are
skipped (they are those tables again).

Read-only: one ``READ ONLY`` transaction with a statement timeout; nothing is
written to the database. Needs ``DATABASE_URL`` (Render → mgmg-db → Connect →
External, with ``?sslmode=require``).

These tables hold company data and people's messages (the Director's tasks,
daily reports, cheques, clients' phones) — under the Director's order of
07.10.2026 IT sees them only with his permission, as on 09.10. The script
prints counts only; the PDFs are for whoever he allows.

Run:
    python scripts/db_export.py --out-dir export            # both files
    python scripts/db_export.py --out-dir export --limit 0  # every row of every table (big)
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The platforms' copies — never exported (the owner: "I don't need information from platforms").
PLATFORM_TABLES = frozenset({
    "sap_gateway_snapshots", "ar_aging_snapshots", "sales_summary_snapshots", "cash_balance_snapshots",
    "attendance_snapshots", "billz_sap_checks", "amocrm_deal_events", "amocrm_pipeline_snapshots",
    "crm_stats_snapshots", "crm_employee_reports", "planner_task_snapshots",
})
PLATFORM_PREFIXES = ("sap_", "billz", "onec", "1c_", "verifix", "amocrm", "crm_", "ar_aging", "cash_balance")
CELL_LIMIT = 300  # characters of one cell shown; longer text ends with "…"
TIME_COLUMNS = ("created_at", "captured_at", "submitted_at", "report_date", "first_seen", "set_at", "ts")


def is_platform(table: str) -> bool:
    return table in PLATFORM_TABLES or table.startswith(PLATFORM_PREFIXES)


def cell(value: Any) -> str:
    """One value as page text: dates readable, JSON compact, long text cut."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        text = value.strftime("%d.%m.%Y %H:%M")
    elif isinstance(value, date):
        text = value.strftime("%d.%m.%Y")
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, (bytes, memoryview)):
        text = f"<{len(bytes(value))} байт>"
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= CELL_LIMIT else text[: CELL_LIMIT - 1] + "…"


def table_html(title: str, columns: list[str], rows: list[dict[str, Any]], note: str = "") -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(cell(r.get(c)))}</td>" for c in columns) + "</tr>" for r in rows)
    return (f"<h2>{html.escape(title)}</h2>" + (f"<p class='note'>{html.escape(note)}</p>" if note else "")
            + (f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>" if rows else
               "<p class='note'>жадвал бўш</p>"))


def page(title: str, sections: list[str]) -> str:
    from integrations.reports.render import FONTS

    fonts = FONTS.as_uri()
    return f"""<!doctype html><html lang="uz-Cyrl"><head><meta charset="utf-8"><style>
@font-face {{ font-family: "Noto Sans"; font-weight: 400; src: url("{fonts}/NotoSans-Regular.ttf"); }}
@font-face {{ font-family: "Noto Sans"; font-weight: 700; src: url("{fonts}/NotoSans-Bold.ttf"); }}
@page {{ size: A4 landscape; margin: 10mm; @bottom-right {{ content: counter(page) " / " counter(pages); font-size: 7pt; }} }}
body {{ font-family: "Noto Sans"; font-size: 6.5pt; color: #15171c; }}
h1 {{ font-size: 14pt; margin: 0 0 4mm; }} h2 {{ font-size: 10pt; margin: 6mm 0 2mm; page-break-after: avoid; }}
.note {{ color: #6b7280; margin: 0 0 2mm; }}
table {{ border-collapse: collapse; width: 100%; table-layout: auto; }}
th, td {{ border: 0.3pt solid #c9ced6; padding: 1pt 2pt; vertical-align: top; text-align: left;
          overflow-wrap: anywhere; word-break: break-word; }}
th {{ background: #f1f3f6; font-weight: 700; }} thead {{ display: table-header-group; }}
tr {{ page-break-inside: avoid; }}
</style></head><body><h1>{html.escape(title)}</h1>{''.join(sections)}</body></html>"""


async def fetch(conn: Any, query: str, params: Any = None) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def tasks_section(conn: Any) -> tuple[list[str], int]:
    rows = await fetch(conn, """
        SELECT t.code, t.created_at, t.status, t.due_date, t.task_summary,
               COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS kimga, e.role AS lavozim,
               t.started_at, t.completed_at, t.blocker, t.source_message_id,
               (SELECT count(*) FROM tasks s WHERE s.director_telegram_user_id = t.director_telegram_user_id
                                               AND s.source_message_id = t.source_message_id) AS bir_xabardan,
               (SELECT count(*) FROM task_events v WHERE v.task_id = t.id) AS voqealar,
               t.raw_message
        FROM tasks t LEFT JOIN employees e ON e.id = t.assigned_employee_id
        ORDER BY t.created_at, t.source_message_id, kimga
    """)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    shared = sum(1 for r in rows if (r["bir_xabardan"] or 0) > 1)
    summary = ("Ҳолатлар: " + ", ".join(f"{k} — {v}" for k, v in sorted(by_status.items()))
               + f". Битта хабардан бир неча кишига кетган қаторлар: {shared} "
                 "(«bir_xabardan» > 1 — бу битта топшириқ, ҳар бир киши учун алоҳида қатор).")
    columns = ["code", "created_at", "kimga", "lavozim", "status", "due_date", "task_summary", "bir_xabardan",
               "source_message_id", "started_at", "completed_at", "blocker", "voqealar", "raw_message"]
    return [table_html(f"tasks — {len(rows)} та қатор", columns, rows, summary)], len(rows)


async def all_tables(conn: Any, limit: int) -> tuple[list[str], dict[str, int], list[str]]:
    tables = [r["table_name"] for r in await fetch(conn, """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name""")]
    skipped = [t for t in tables if is_platform(t)]
    sections: list[str] = []
    counts: dict[str, int] = {}
    for table in tables:
        if table in skipped:
            continue
        columns = [r["column_name"] for r in await fetch(conn, """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,))]
        total = (await fetch(conn, f'SELECT count(*) AS n FROM "{table}"'))[0]["n"]
        counts[table] = total
        order = next((c for c in TIME_COLUMNS if c in columns), None)
        query = f'SELECT * FROM "{table}"' + (f' ORDER BY "{order}" DESC' if order else "")
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = await fetch(conn, query)
        note = f"{total} та қатор" + (f"; энг янги {len(rows)} таси кўрсатилган" if limit and total > len(rows) else "")
        sections.append(table_html(table, columns, rows, note))
    return sections, counts, skipped


async def run(out_dir: Path, limit: int) -> None:
    import psycopg
    from psycopg.rows import dict_row

    from integrations.common.config import settings
    from integrations.reports.render import html_to_pdf

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    async with await psycopg.AsyncConnection.connect(settings.postgres_dsn, row_factory=dict_row) as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute("SET LOCAL statement_timeout = '120s'")
            task_sections, task_count = await tasks_section(conn)
            sections, counts, skipped = await all_tables(conn, limit)
    (out_dir / "topshiriqlar.pdf").write_bytes(html_to_pdf(page(f"Топшириқлар — {stamp}", task_sections)))
    contents = "<p class='note'>Платформалар (SAP, Billz, 1C, Verifix, эски CRM) жадваллари киритилмади: " \
               + html.escape(", ".join(skipped) or "—") + ".</p>"
    (out_dir / "jadvallar.pdf").write_bytes(html_to_pdf(page(f"Ботнинг жадваллари — {stamp}", [contents] + sections)))
    # Counts only — never a value.
    print(f"topshiriqlar.pdf: {task_count} task row(s)")
    print(f"jadvallar.pdf: {len(counts)} table(s), {sum(counts.values())} row(s); platform tables left out: {len(skipped)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="The bot's own tables as PDFs (read-only, no platform data).")
    parser.add_argument("--out-dir", type=Path, default=Path("export"))
    parser.add_argument("--limit", type=int, default=500, help="newest rows per table (0 = every row)")
    args = parser.parse_args()
    asyncio.run(run(args.out_dir, args.limit))


if __name__ == "__main__":
    main()
