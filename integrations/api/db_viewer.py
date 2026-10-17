"""Read-only database viewer at ``/db`` — look at the tables, nothing else.

Render gives Postgres connection strings and ``psql``, but no way to just
look at the data. This is the smallest thing that fills that gap, served by
the API service that is already running (no extra cost):

  * ``/db``            every table and view, with row counts
  * ``/db/{table}``    rows, newest first, 50 per page, with a text search —
                       only for ``TECHNICAL_TABLES`` — since 2026-10-17 every
                       table of the bot's own records; the platforms' data
                       (SAP, 1C, Billz, Verifix) stays closed and
                       shows its structure only (2026-10-15): columns, row
                       count, when rows were written — never a value

Safety, in layers:
  * Off unless ``DB_VIEWER_PASSWORD`` is set (404 otherwise), then HTTP Basic
    auth with that password over Render's HTTPS; a failed login waits a
    second, which makes guessing slow.
  * No SQL is ever typed by the visitor: the table must be one that exists
    in ``public``, identifiers are quoted by psycopg, values are bound.
  * Every query runs in a READ ONLY transaction with a 5-second timeout, so
    even a bug here cannot change or lock anything.
  * Pages are marked noindex/no-store and can't be framed.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import secrets
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from psycopg import sql

from integrations.common.config import settings
from integrations.common.db import connection, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import TASHKENT, to_local

log = setup_logging("db-viewer")
router = APIRouter(prefix="/db", include_in_schema=False)

PAGE_SIZE = 50
CELL_CHARS = 120
STATEMENT_TIMEOUT = "5s"

# Tables whose rows the viewer shows. 2026-10-07 (the Director's order): technical
# tables only. 2026-10-17 (the Director approved, recorded in
# docs/access-review-2026-10-07.md §4): every table of the bot's own records is
# open; the platforms' data stays closed — SAP, 1C, Billz, Verifix, the old CRM,
# and what is built straight from their figures (briefs, receivables alerts,
# the Billz → SAP check). A new table is closed until it is listed here.
TECHNICAL_TABLES = frozenset({
    "agent_actions",        # the audit log: what ran, when, did it fail
    "access_requests",      # who asked to join the bot
    "employee_changes",     # name / role / settings changes, by whom
    "days_off",
    "attendance_flexible",  # Verifix ids marked "эркин график" (ids only)
    "announcements",
    # the bot's own records (2026-10-17)
    "employees", "tasks", "task_events", "task_updates", "task_drafts", "pending_dispatches",
    "daily_reports", "pending_relays", "employee_files",
    "conversation_turns", "ai_chat_turns", "director_clarifications",
    "kpi_goals", "kpi_ratings", "kpi_periods",
    "permission_requests", "permission_request_events", "approvals",
    "client_feedback", "cheques", "garmin_leads",
    "leads", "lead_assignments", "lead_checkins",
})
# Open for IT until this day only (2026-10-17, the Boss: find and fix the briefs' SAP / Billz / 1C
# mismatches — docs/access-review-2026-10-07.md §4); closed again by itself after it.
PLATFORM_OPEN_UNTIL = date(2026, 10, 31)


def is_open(table: str, today: date | None = None) -> bool:
    """Whether /db shows this table's rows: the bot's own tables; the platforms' only until PLATFORM_OPEN_UNTIL."""
    from integrations.common.timeutil import today_local

    if table in TECHNICAL_TABLES:
        return True
    return table in PLATFORM_TABLES and (today or today_local()) <= PLATFORM_OPEN_UNTIL


# The platforms' data (and what is built from their figures): closed except while PLATFORM_OPEN_UNTIL lasts.
PLATFORM_TABLES = frozenset({
    "ar_aging_snapshots", "v_ar_aging_latest", "sap_gateway_snapshots", "v_sap_gateway_latest",
    "cash_balance_snapshots", "v_cash_latest", "sales_summary_snapshots", "attendance_snapshots",
    "billz_sap_checks", "daily_briefs", "alerts",
    "amocrm_deal_events", "amocrm_pipeline_snapshots", "v_pipeline_latest", "crm_stats_snapshots",
    "v_crm_stats_latest", "crm_employee_reports", "planner_task_snapshots",
})
CLOSED_TEXT = ("Бу жадвал ёпиқ: SAP, 1C, Billz ёки Verifix маълумоти (ёки уларнинг рақамларидан тузилган). "
               "Бот ёзувлари очиқ (Директор рухсати, 17.10.2026).")
STRUCTURE_TEXT = "Қийматлар кўрсатилмайди — фақат тузилиши: устунлар, қаторлар сони ва қачон ёзилгани."

# Newest-first ordering: the first of these a table has.
_ORDER_COLUMNS = (
    "occurred_at", "created_at", "captured_at", "generated_at", "submitted_at",
    "asked_at", "decided_at", "snapshot_date", "report_date", "id",
)
# A closed table shows its shape, never a value (2026-10-15, the owner wants to see what is in the
# tables; the Director's order keeps the values closed): its columns and their types, the row
# count, when rows were written (the first of these columns it has: newest, oldest, rows per day)
# and, for the SAP pushes, rows per kind — enough to watch data arrive, be deleted and come back.
_WRITTEN_COLUMNS = tuple(c for c in _ORDER_COLUMNS if c != "id")
GROUP_COLUMNS = {"sap_gateway_snapshots": "tool"}
DAYS_SHOWN = 14

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
}

_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--head:#f5f5f7;--link:#0a66c2}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#ececec;--muted:#9a9aa0;--line:#2c2c2e;--head:#1c1c1e;--link:#6aa9ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:18px;font-weight:600;margin:0 0 16px}
h2{font-size:15px;font-weight:600;margin:20px 0 8px}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.muted{color:var(--muted)}
.list{list-style:none;padding:0;margin:0;border-top:1px solid var(--line)}
.list li{display:flex;justify-content:space-between;gap:12px;padding:9px 2px;border-bottom:1px solid var(--line)}
form{display:flex;gap:8px;margin:0 0 12px}
input{flex:1;min-width:0;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
button{padding:7px 14px;border:1px solid var(--line);border-radius:6px;background:var(--head);color:var(--fg);font:inherit;cursor:pointer}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:6px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);white-space:nowrap}
th{position:sticky;top:0;background:var(--head);font-weight:600}
td{max-width:360px;overflow:hidden;text-overflow:ellipsis}
tr:last-child td{border-bottom:0}
.null{color:var(--muted)}
.pager{display:flex;justify-content:space-between;align-items:center;margin-top:12px}
"""


# ------------------------------------------------------------------ access


def _password_ok(request: Request) -> bool:
    expected = settings.db_viewer_password.get_secret_value()
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _user, _, password = decoded.partition(":")
    return secrets.compare_digest(password.encode(), expected.encode())


async def _gate(request: Request) -> Response | None:
    """None when the visitor may see the page, else the response to send."""
    if not settings.db_viewer_password.get_secret_value():
        return Response(status_code=404)
    if _password_ok(request):
        return None
    await asyncio.sleep(1)  # makes guessing the password slow
    return Response(
        status_code=401,
        headers={**_HEADERS, "WWW-Authenticate": 'Basic realm="MGMG DB", charset="UTF-8"'},
    )


# ------------------------------------------------------------------ queries


async def _read(query: sql.Composable | str, params: Any = None) -> list[dict[str, Any]]:
    """Run one query in a read-only transaction with a timeout."""
    async with connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                await cur.execute(query, params)
                return await cur.fetchall()


async def _relations() -> list[dict[str, Any]]:
    """Every table and view in the public schema."""
    return await _read(
        """
        SELECT c.relname AS name, c.relkind AS kind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v')
        ORDER BY c.relkind, c.relname
        """
    )


async def _count(table: str, search: str = "") -> int | None:
    query = sql.SQL("SELECT count(*) AS n FROM {} t").format(sql.Identifier(table))
    params: list[Any] = []
    if search:
        query = query + sql.SQL(" WHERE t::text ILIKE %s")
        params.append(f"%{search}%")
    try:
        rows = await _read(query, params)
    except Exception as exc:  # noqa: BLE001 — a slow count must not break the page
        log.warning("Count of {} failed: {}", table, exc)
        return None
    return int(rows[0]["n"])


# ------------------------------------------------------------------ rendering


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='uz'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>",
        headers=_HEADERS,
    )


def cell_text(value: Any) -> str:
    """How one database value reads in a cell (timestamps in Tashkent time)."""
    if isinstance(value, datetime):
        return (to_local(value) if value.tzinfo else value).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def _cell(value: Any) -> str:
    if value is None:
        return "<td class='null'>—</td>"
    text = cell_text(value)
    short = text if len(text) <= CELL_CHARS else text[: CELL_CHARS - 1] + "…"
    return f"<td title='{_e(text)}'>{_e(short)}</td>"


def _grid(head: list[str], rows: list[list[Any]]) -> str:
    return ("<div class='wrap'><table><thead><tr>" + "".join(f"<th>{_e(h)}</th>" for h in head)
            + "</tr></thead><tbody>" + "".join("<tr>" + "".join(_cell(v) for v in row) + "</tr>" for row in rows)
            + "</tbody></table></div>")


async def _structure_page(table: str) -> Response:
    """A closed table's shape — columns, row count, when rows were written, rows per kind — never a value."""
    columns = await _read(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    names = [c["column_name"] for c in columns]
    total = await _count(table)
    parts = [
        "<p><a href='/db'>← Жадваллар</a></p>",
        f"<h1>🔒 {_e(table)} <span class='muted'>{total if total is not None else '?'} қатор</span></h1>",
        f"<p class='muted'>{_e(CLOSED_TEXT)} {_e(STRUCTURE_TEXT)}</p>",
    ]
    t = sql.Identifier(table)
    written = next((c for c in _WRITTEN_COLUMNS if c in names), None)
    try:
        if written and total:
            w = sql.Identifier(written)
            is_date = next(c.get("data_type") for c in columns if c["column_name"] == written) == "date"
            day = w if is_date else sql.SQL("({} AT TIME ZONE %s)::date").format(w)
            span = (await _read(sql.SQL("SELECT min({w}) AS oldest, max({w}) AS newest FROM {t}").format(w=w, t=t)))[0]
            per_day = await _read(
                sql.SQL("SELECT {d} AS day, count(*) AS n FROM {t} GROUP BY 1 ORDER BY 1 DESC LIMIT %s").format(d=day, t=t),
                [*([] if is_date else [TASHKENT.key]), DAYS_SHOWN],
            )
            parts.append(f"<h2>Қачон ёзилган ({_e(written)})</h2>"
                         + _grid(["Энг эскиси", "Энг янгиси"], [[span["oldest"], span["newest"]]])
                         + f"<p class='muted'>Кунлар бўйича, охирги {DAYS_SHOWN} кун:</p>"
                         + _grid(["Кун", "Қатор"], [[r["day"], r["n"]] for r in per_day]))
        group = GROUP_COLUMNS.get(table)
        if group in names and total:
            last = sql.SQL(", max({}) AS last").format(sql.Identifier(written)) if written else sql.SQL(", NULL AS last")
            kinds = await _read(sql.SQL("SELECT {g} AS k, count(*) AS n{last} FROM {t} GROUP BY 1 ORDER BY 1").format(
                g=sql.Identifier(group), last=last, t=t))
            parts.append(f"<h2>Тури бўйича ({_e(group)})</h2>"
                         + _grid(["Тури", "Қатор", "Охирги ёзилгани"], [[r["k"], r["n"], r["last"]] for r in kinds]))
    except Exception as exc:  # noqa: BLE001 — the columns still show
        log.warning("Structure of {} failed: {}", table, type(exc).__name__)
    parts.append("<h2>Устунлар</h2>" + _grid(["Устун", "Маълумот тури"],
                                             [[c["column_name"], c.get("data_type")] for c in columns]))
    return _page(f"🔒 {table} — MGMG", "".join(parts))


# ------------------------------------------------------------------ pages


@router.get("", response_class=HTMLResponse)
async def tables(request: Request) -> Response:
    """Every table and view, with its row count."""
    denied = await _gate(request)
    if denied:
        return denied

    items = []
    for relation in await _relations():
        name = relation["name"]
        if relation["kind"] == "v":
            size = "<span class='muted'>кўриниш</span>"
        else:
            count = await _count(name)
            size = f"<span class='muted'>{count if count is not None else '?'} қатор</span>"
        if is_open(name):
            items.append(f"<li><a href='/db/{quote(name)}'>{_e(name)}</a>{size}</li>")
        else:
            # closed: the link opens its structure only (no values)
            items.append(f"<li><a href='/db/{quote(name)}'>🔒 {_e(name)}</a>{size}</li>")
    return _page("MGMG — база", f"<h1>Жадваллар</h1><p class='muted'>🔒 — SAP, 1C, Billz, Verifix маълумоти: фақат "
                                "тузилиши кўринади. Бот ёзувлари очиқ (Директор рухсати, 17.10.2026).</p>"
                                f"<ul class='list'>{''.join(items)}</ul>")


@router.get("/{table}", response_class=HTMLResponse)
async def table_rows(request: Request, table: str, page: int = 1, q: str = "") -> Response:
    """One table's rows, newest first, with an optional text search."""
    denied = await _gate(request)
    if denied:
        return denied

    names = {r["name"] for r in await _relations()}
    if table not in names:
        return _page("Топилмади", "<p><a href='/db'>← Жадваллар</a></p><p>Бундай жадвал йўқ.</p>")
    if not is_open(table):
        log.info("Closed table {} asked for in /db — values refused, structure shown", table)
        await log_action(agent="db-viewer", action="db_viewer_refused", target_system="postgres",
                         status="skipped", target_ref=table, mode="read")
        return await _structure_page(table)

    page = max(page, 1)
    search = q.strip()[:100]
    columns = [
        r["column_name"]
        for r in await _read(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
    ]
    order = next((c for c in _ORDER_COLUMNS if c in columns), None)

    query = sql.SQL("SELECT * FROM {} t").format(sql.Identifier(table))
    params: list[Any] = []
    if search:
        query = query + sql.SQL(" WHERE t::text ILIKE %s")
        params.append(f"%{search}%")
    if order:
        query = query + sql.SQL(" ORDER BY {} DESC NULLS LAST").format(sql.Identifier(order))
    query = query + sql.SQL(" LIMIT %s OFFSET %s")
    params += [PAGE_SIZE, (page - 1) * PAGE_SIZE]

    try:
        rows = await _read(query, params)
    except Exception as exc:  # noqa: BLE001 — show the error instead of a 500
        log.warning("Reading {} failed: {}", table, exc)
        return _page(table, f"<p><a href='/db'>← Жадваллар</a></p><p>Ўқиб бўлмади: {_e(exc)}</p>")
    total = await _count(table, search)

    head = "".join(f"<th>{_e(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(_cell(row.get(c)) for c in columns) + "</tr>" for row in rows)
    if not rows:
        body = f"<tr><td class='null' colspan='{max(len(columns), 1)}'>Қатор йўқ</td></tr>"

    first = (page - 1) * PAGE_SIZE + 1 if rows else 0
    last = (page - 1) * PAGE_SIZE + len(rows)
    shown = f"{first}–{last} / {total if total is not None else '?'}"

    def link(to_page: int) -> str:
        return f"/db/{quote(table)}?" + urlencode({"page": to_page, **({"q": search} if search else {})})

    prev_link = f"<a href='{_e(link(page - 1))}'>← Олдинги</a>" if page > 1 else "<span></span>"
    has_more = total is None and len(rows) == PAGE_SIZE or (total is not None and last < total)
    next_link = f"<a href='{_e(link(page + 1))}'>Кейинги →</a>" if has_more else "<span></span>"

    html_body = (
        f"<p><a href='/db'>← Жадваллар</a></p>"
        f"<h1>{_e(table)} <span class='muted'>{_e(shown)}</span></h1>"
        f"<form method='get'><input name='q' value='{_e(search)}' placeholder='Қидириш'>"
        f"<button>Қидириш</button></form>"
        f"<div class='wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
        f"<div class='pager'>{prev_link}<span class='muted'>{_e(shown)}</span>{next_link}</div>"
    )
    return _page(f"{table} — MGMG", html_body)
