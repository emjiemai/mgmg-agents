"""The Director's analyst — answers a question by looking things up itself, read-only.

From the owner, 2026-10-07: the Director typed «Дебитор», the bot asked
"SAP B1, 1C Clobus or Didox?", and each answer led to "no data". The
Director shouldn't have to know where data lives. So a question now goes to
an AI that has the company's systems as *tools* and decides itself what to
look up — SAP's pushed data, 1C's accounting registers, BILLZ shop sales,
Verifix attendance and the bot's own records — then answers from what it
found, saying which source each figure comes from.

Read-only, by construction, in layers:
  * The model never writes a query, a URL or a path. It can only call the
    functions in ``TOOLS``; every argument is validated here (kinds and
    sources are fixed lists, dates are parsed, account codes are digits).
  * Database reads go through ``fetch_read_only`` (a READ ONLY transaction
    with a timeout); there is no ``execute`` in this module.
  * 1C is read through ``OneCClient`` (GET only), BILLZ through its report
    reads, Verifix through its timesheet reads — none of those clients has a
    method that changes anything in the source system.
  * Tool results are never logged — only which tools ran (the Director's
    order of 07.10.2026: IT doesn't see the company's figures).

Only the Director's questions come here (``ops_manager._dispatch_director_task``);
employees' work AI (``ai_chat.py``) has no company data at all.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from integrations.ai.openrouter_client import OpenRouterClient
from integrations.common.config import settings
from integrations.common.db import fetch_read_only
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_money, format_money_by_currency
from integrations.common.timeutil import now_local, to_local, today_local
from integrations.org_bot.knowledge import COMPANY_KNOWLEDGE
from integrations.org_bot.prompt import GUARDRAILS

AGENT = "ops-manager-bot"
log = setup_logging("analyst")

TOOL_CHARS = 30000  # one tool's answer, at most (the rest is cut, and the model is told)
MAX_CALLS_PER_ROUND = 6
MAX_DAYS = 92  # longest period one BILLZ / Verifix / 1C call may cover

# The bot's own records and reference data, by the existing per-agent readers
# (ops_manager._fetch_agent_data) — what each holds, for the model.
COMPANY_SOURCES: dict[str, str] = {
    "topshiriqlar": "tasks the Director assigned through the bot: open, overdue, on-time rates (30 days)",
    "xodimlar_kpi": "employees' daily reports (14 days: who reported, what they wrote, numbers) and the KPI score",
    "ruxsatlar": "written permission requests (EMJ-SOP-ADM-01): waiting, approved, rejected",
    "pul_kalendari": "money in/out expected in the next 30 days (approved payments + invoices due)",
    "mijoz_fikrlari": "client complaints left through the QR codes (Londry, Garmin), 60 days",
    "lidlar": "history: leads handed to B2B sales people until the hand-out was stopped on 2026-10-07",
    "lead_agent": "every lead the Lead Agent found (the leads Google Sheet)",
    "garmin_lidlar": "customers who came through the Garmin AI bot (30 days)",
    "garmin_catalog": "Garmin products and prices (garmin.com.uz snapshot)",
    "reporter_agent": "14 days of the morning brief's own figures (overdue receivables trend)",
}

# SAP data pushed from the gateway machine (push_handler.FULL_DATASETS) — what each kind is.
SAP_KINDS: dict[str, str] = {
    "sales": "A/R invoices and credit notes (OINV + ORIN headers), last ~45 days",
    "sales_lines": "lines of those invoices: item, quantity, warehouse, line total",
    "payments": "incoming payments from customers (ORCT), last ~45 days",
    "payments_out": "outgoing payments to suppliers (OVPM), last ~45 days",
    "ap_open": "open supplier invoices (OPCH) — not pushed yet; supplier debt is supplier_balances",
    "supplier_balances": "every supplier with a balance (OCRD): Balance USD, BalanceSys so'm "
                         "(positive = we owe, negative = advance paid)",
    "po_open": "open purchase order lines (OPOR + POR1)",
    "orders": "open sales orders (ORDR)",
    "inventory": "stock by item and warehouse (OITW + OITM), non-zero",
    "stock_value": "stock value summed per warehouse",
    "products": "item master data (OITM)",
    "customers": "business partners (OCRD)",
    "warehouses": "warehouse list (OWHS)",
    "sales_people": "sales employees (OSLP)",
    "equipment": "customer equipment cards (OINS)",
    "service_calls": "service calls (OSCL)",
    "service_contracts": "service contracts (OCTR)",
}
# Fields whose sums are worked out here, not left to the model to add up.
SUM_FIELDS = ("DocTotal", "DocTotalFC", "DocTotalSy", "PaidToDate", "PaidFC", "LineTotal", "Quantity",
              "OnHand", "StockValue", "OpenQty", "CashSum", "TrsfrSum", "CheckSum", "Balance", "BalanceSys")
DATE_FIELDS = ("DocDate", "TaxDate", "CreateDate", "DocDueDate")


def _fn(name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)},
    }}


_DATE = {"type": "string", "description": "YYYY-MM-DD"}

TOOLS: list[dict] = [
    _fn("data_sources", "Which systems are connected and how fresh their data is. Cheap; call it when unsure "
        "where something lives or whether a system works.", {}),
    _fn("sap_receivables", "Customer debt from SAP: every open A/R invoice with balance, days unpaid, customer, "
        "sales person; totals by age and by customer. The main source for 'дебитор', 'kim qancha qarz'.",
        {"customer": {"type": "string", "description": "part of a customer's name or code, optional"},
         "min_days_overdue": {"type": "integer", "description": "only invoices unpaid at least this many days"},
         "limit": {"type": "integer", "description": "invoices to list (default 60, max 300)"}}),
    _fn("sap_records", "Rows of one kind of SAP data pushed from the gateway, with sums worked out. Kinds: "
        + "; ".join(f"{k} = {v}" for k, v in SAP_KINDS.items()),
        {"kind": {"type": "string", "enum": list(SAP_KINDS)},
         "search": {"type": "string", "description": "text that must appear in the row (customer, item, code), optional"},
         "date_from": _DATE, "date_to": _DATE,
         "limit": {"type": "integer", "description": "rows to list (default 80, max 400); sums cover every match"}},
        ("kind",)),
    _fn("onec_balances", "Balances on 1C accounting accounts (Бухгалтерия для Узбекистана, chart НСБУ 21) at a "
        "moment, by account and by counterparty, in so'm. 40 = receivables from customers (4010), 43 = advances "
        "given, 50 = cash desk, 51 = bank accounts, 52 = currency accounts, 60 = payables to suppliers (6010), "
        "63 = advances received, 64 = taxes owed, 29 = goods, 10 = materials.",
        {"account_prefix": {"type": "string", "description": "account code or its start, digits only: '4010', '40', '6010', '5'"},
         "as_of": _DATE}, ("account_prefix",)),
    _fn("onec_turnovers", "Debit/credit turnovers on 1C accounts for a period, in so'm: e.g. 9010 revenue (credit), "
        "9110 cost of sales (debit), 94 expenses (debit), 50/51 money in (debit) and out (credit).",
        {"account_prefix": {"type": "string", "description": "digits only, e.g. '9010', '94', '51'"},
         "date_from": _DATE, "date_to": _DATE}, ("account_prefix", "date_from", "date_to")),
    _fn("billz_sales", "Shop till sales from BILLZ (Garmin shops) for a period: per shop, per seller, top products, "
        "cheques, in so'm.", {"date_from": _DATE, "date_to": _DATE}, ("date_from", "date_to")),
    _fn("attendance", "Verifix face-ID attendance for a period: who came late, who was absent, arrival times, "
        "sick leave/vacation; per employee totals.",
        {"date_from": _DATE, "date_to": _DATE,
         "name": {"type": "string", "description": "one person's name (any alphabet), optional"}},
        ("date_from", "date_to")),
    _fn("supplier_debt_compare", "Supplier debt (кредиторлик) compared supplier by supplier: 1C (6010+6015 owed "
        "minus 4310+4315 advances) against SAP's supplier balances, paired by ИНН then name. Gives both totals, how "
        "many suppliers agree / differ / are only in one system, and the biggest differences with a likely reason. "
        "Use it for 'кредиторлик', 'етказиб берувчиларга қарз', '1C ва SAP солиштир'. Reads 1C live (slow, ~20 s).",
        {"excel": {"type": "boolean", "description": "true when the Director wants the file / Excel / table / "
                   "full comparison — the bot then sends him the .xlsx workbook with every supplier"}}),
    _fn("company_data", "The bot's own records and reference data. Sources: "
        + "; ".join(f"{k} = {v}" for k, v in COMPANY_SOURCES.items()),
        {"source": {"type": "string", "enum": list(COMPANY_SOURCES)}}, ("source",)),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


ANALYST_SYSTEM_PROMPT = f"""\
# ROLE
You are the analyst behind "OPS Manager Bot". The Operations Director of \
MGMG asks you a question about the company; you find the answer in the \
company's systems with your tools and reply.

{COMPANY_KNOWLEDGE}
# WHAT YOU CAN DO
Only READ, through the tools. You cannot create, change, delete, approve or
send anything in SAP, 1C, BILLZ, Verifix or anywhere else, and no tool can —
if asked to change data, say plainly that you can only look things up.

# HOW TO WORK
- Decide yourself where the answer lives and look it up. NEVER ask the
  Director "which system?" — check the likely ones yourself.
- Where two systems hold the same thing, look in both and say which figure
  is from where. Customer debt: sap_receivables (SAP invoices) AND
  onec_balances '40' (1C's books); supplier debt: supplier_debt_compare
  (both systems at once, already compared); money right now: onec_balances '5'; shop sales:
  billz_sales; B2B sales: sap_records sales; stock: sap_records inventory /
  stock_value and onec_balances '29'. If they differ, say so — don't pick one.
- Didox (e-invoices) is not connected: only if asked about Didox, say so and
  give what SAP / 1C show instead.
- No date given: use a sensible period and state it ("бу ой", "охирги 30
  кун") — never ask for a period.
- If you don't understand the question, or it can mean two clearly
  different things that need different lookups and give different answers
  (e.g. "Абай" — the shop's sales, or its debt?), don't guess: ask ONE short
  question back, offering the readings as "1) … 2) …" (the owner,
  2026-10-10). His next message answers it. Never ask which system — that
  you decide yourself — and don't ask when one reading is clearly meant.
- A tool that fails or has no data: say that system is unavailable right
  now, and answer from the others. Never invent a figure.
- Several tools can be called at once when they don't depend on each other.
- Asked to compare supplier debt, or for an Excel / file / table of it: call
  supplier_debt_compare with excel=true. The bot attaches the .xlsx to your
  answer itself — say in one line that the full table is in the file; never
  say you can't send files.

# NUMBERS
- Repeat amounts exactly as the tools give them, with their currency. Never
  convert between currencies and never add so'm and $ together. SAP's own
  amounts (DocTotal, balances) are its local currency, USD; DocTotalSy is
  so'm; 1C and BILLZ amounts are so'm.
- Use the totals the tools worked out; don't re-add long lists yourself.
- Say how fresh the data is when a tool warns it is old or partial.

# ANSWER
- Uzbek, Cyrillic script. Short — a Telegram reply: the answer first, then
  at most ~10 short lines (top items), one line per source when comparing.
- Name the source briefly: "SAP бўйича", "1C бўйича", "Billz бўйича".
- You may use <b>…</b> around one or two key figures. No other tags, no
  markdown (**, #, bullet dashes, code blocks).

{GUARDRAILS}
"""


@dataclass
class Answer:
    text: str
    tools: list[str] = field(default_factory=list)
    rounds: int = 0
    files: list[tuple[bytes, str]] = field(default_factory=list)  # (content, filename) sent after the text


# Files a tool made during one answer (the workbook), picked up by ``answer``.
_files: ContextVar[list[tuple[bytes, str]] | None] = ContextVar("analyst_files", default=None)
# The Director's own words asking for a file — the workbook goes even if the model forgets excel=true.
FILE_WORDS = re.compile(r"солиштир|solishtir|сравн|excel|эксел|ексел|xlsx|файл|fayl|жадвал|jadval|таблиц", re.I)
_wants_file: ContextVar[bool] = ContextVar("analyst_wants_file", default=False)


def _today() -> date:
    return today_local()


def parse_day(value: Any, default: date | None = None) -> date | None:
    """'2026-10-07' (or its first 10 characters) as a date; ``default`` if it isn't one."""
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return default


def period(args: dict[str, Any], default_days: int = 30) -> tuple[date, date]:
    """The (from, to) a tool was asked for: valid dates, from ≤ to, at most MAX_DAYS, never in the future."""
    today = _today()
    end = min(parse_day(args.get("date_to"), today) or today, today)
    start = parse_day(args.get("date_from"), end - timedelta(days=default_days - 1)) or end
    if start > end:
        start, end = end, start
    if (end - start).days >= MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS - 1)
    return start, end


def _limit(value: Any, default: int, cap: int) -> int:
    try:
        return max(1, min(int(value), cap))
    except (TypeError, ValueError):
        return default


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _amount(value: float) -> str:
    """A plain number with thousands separators (no currency guessed)."""
    return f"{value:,.2f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------ tools


async def data_sources(_args: dict[str, Any]) -> str:
    from integrations.org_bot import ops_manager  # lazy: ops_manager imports this module

    freshness, _silent = await ops_manager.sap_freshness()
    kinds = await fetch_read_only(
        "SELECT tool, max(captured_at) AS at, count(*) AS n FROM v_sap_gateway_latest GROUP BY tool ORDER BY tool"
    )
    lines = [f"SAP (pushed from the gateway machine every 30 min): {freshness}"]
    lines += [f"  - {k['tool']}: {k['n']} rows, captured {to_local(k['at']):%Y-%m-%d %H:%M}" for k in kinds]
    lines.append(f"1C (accounting, OData read-only): {'connected' if settings.onec_configured else 'NOT connected'}")
    lines.append(f"BILLZ (shop tills): {'connected' if settings.billz_configured else 'NOT connected'}")
    lines.append(f"Verifix (attendance): {'connected' if settings.verifix_configured else 'NOT connected'}")
    lines.append("Didox (e-invoices): NOT connected — no data from it at all.")
    lines.append("The bot's own records (tasks, daily reports, KPI, leads, complaints, permissions): always available.")
    return "\n".join(lines)


async def sap_receivables(args: dict[str, Any]) -> str:
    from integrations.org_bot import ops_manager

    customer = str(args.get("customer") or "").strip()[:80]
    min_days = args.get("min_days_overdue")
    min_days = int(min_days) if isinstance(min_days, (int, float)) or str(min_days or "").isdigit() else None
    limit = _limit(args.get("limit"), 60, 300)
    rows = await fetch_read_only(
        "SELECT doc_num, card_code, card_name, doc_date, due_date, days_overdue, aging_bucket, balance_due_tiyin, "
        "currency, sales_person_name, captured_at, doc_currency, doc_total_fc_tiyin, paid_fc_tiyin "
        "FROM v_ar_aging_latest WHERE (%(c)s = '' OR card_name ILIKE %(like)s OR card_code ILIKE %(like)s) "
        "AND (%(d)s::int IS NULL OR days_overdue >= %(d)s::int) ORDER BY balance_due_tiyin DESC",
        {"c": customer, "like": f"%{customer}%", "d": min_days},
    )
    freshness, _silent = await ops_manager.sap_freshness()
    if not rows:
        return f"{freshness}\nNo open SAP invoices match" + (f" '{customer}'" if customer else "") + "."
    written = [ops_manager._as_written(r) for r in rows]
    in_own = sum(1 for (_a, cur), r in zip(written, rows) if cur != r["currency"])
    lines = [
        freshness,
        f"Open SAP invoices (= customer debt, SAP OINV), snapshot {to_local(rows[0]['captured_at']):%Y-%m-%d %H:%M}"
        + (f", customer filter '{customer}'" if customer else "") + (f", unpaid ≥ {min_days} days" if min_days else "")
        + f": {len(rows)} invoices, TOTAL {format_money_by_currency(written)}.",
        ops_manager._currency_note(in_own, len(rows)),
        "Most invoices have due date = invoice date (no payment terms in SAP): 'days overdue' = days unpaid.",
        "By days unpaid:",
    ]
    for bucket in ("current", "1_30", "31_60", "61_90", "90_plus"):
        amounts = [w for w, r in zip(written, rows) if r["aging_bucket"] == bucket]
        if amounts:
            lines.append(f"  {bucket}: {format_money_by_currency(amounts)} ({len(amounts)} invoices)")
    by_customer: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for w, r in zip(written, rows):
        by_customer[r["card_name"] or r["card_code"]].append(w)
    ranked = sorted(by_customer.items(), key=lambda kv: -sum(a for a, _c in kv[1]))
    lines.append(f"By customer ({len(ranked)}), biggest first:")
    lines += [f"  - {name}: {format_money_by_currency(ws)} ({len(ws)} inv.)" for name, ws in ranked[:40]]
    lines.append(f"Invoices (biggest first, {min(limit, len(rows))} of {len(rows)}):")
    for (amount, cur), r in list(zip(written, rows))[:limit]:
        shown = format_money(amount, cur)
        if cur != r["currency"]:
            shown += f" (SAP's {r['currency']} {format_money(r['balance_due_tiyin'], r['currency'])})"
        lines.append(f"  - #{r['doc_num']} {r['doc_date']} {r['card_name']}: {shown}, {r['days_overdue']}d unpaid, "
                     f"seller {r['sales_person_name'] or 'not set'}")
    return "\n".join(lines)


def _row_day(raw: dict[str, Any]) -> date | None:
    for name in DATE_FIELDS:
        day = parse_day(raw.get(name))
        if day:
            return day
    return None


def _cancelled(raw: dict[str, Any]) -> bool:
    return str(raw.get("CANCELED") or "").upper() in ("Y", "C")


async def sap_records(args: dict[str, Any]) -> str:
    from integrations.org_bot import ops_manager

    kind = args.get("kind")
    if kind not in SAP_KINDS:
        return f"Unknown kind {kind!r}; one of: {', '.join(SAP_KINDS)}."
    search = str(args.get("search") or "").strip()[:80]
    limit = _limit(args.get("limit"), 80, 400)
    has_dates = bool(args.get("date_from") or args.get("date_to"))
    start, end = period(args, default_days=MAX_DAYS) if has_dates else (None, None)
    rows = await fetch_read_only(
        "SELECT raw, captured_at FROM v_sap_gateway_latest WHERE tool = %(k)s "
        "AND (%(s)s = '' OR raw::text ILIKE %(like)s) ORDER BY natural_key",
        {"k": kind, "s": search, "like": f"%{search}%"},
    )
    freshness, _silent = await ops_manager.sap_freshness()
    if not rows:
        return f"{freshness}\nNo SAP {kind} rows" + (f" matching '{search}'" if search else "") + " in the latest push."
    raws = [r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"] or "{}") for r in rows]
    if start:
        raws = [x for x in raws if (d := _row_day(x)) is not None and start <= d <= end]
    raws.sort(key=lambda x: str(_row_day(x) or ""), reverse=True)
    live = [x for x in raws if not _cancelled(x)]
    lines = [freshness, f"SAP {kind} ({SAP_KINDS[kind]}), captured {to_local(rows[0]['captured_at']):%Y-%m-%d %H:%M}"
             + (f", text '{search}'" if search else "") + (f", dated {start}..{end}" if start else "")
             + f": {len(raws)} rows" + (f" ({len(raws) - len(live)} cancelled, left out of sums)" if len(live) != len(raws) else "")
             + "."]
    if raws:
        days = [d for d in (_row_day(x) for x in raws) if d]
        if days:
            lines.append(f"Dates in these rows: {min(days)} to {max(days)}.")
    sums: dict[tuple[str, str], float] = defaultdict(float)
    for x in live:
        currency = str(x.get("DocCur") or "")
        for name in SUM_FIELDS:
            value = _num(x.get(name))
            if value is not None:
                # FC = the document's own currency; Sy = SAP's system currency (so'm); plain = local (USD).
                label = currency if name.endswith("FC") and currency else "UZS" if name.endswith("Sy") else ""
                sums[(name, label)] += value
    if sums:
        lines.append("SUMS over every non-cancelled match (field as SAP named it; no suffix = SAP local currency USD "
                     "for money fields, *Sy = so'm, *FC = the document's currency):")
        lines += [f"  {name}{' ' + label if label else ''}: {_amount(total)}" for (name, label), total in sorted(sums.items())]
    if kind in ("sales", "sales_lines", "inventory") and live:
        key = {"sales": "CardName", "sales_lines": "ItemCode", "inventory": "WhsCode"}[kind]
        money = {"sales": "DocTotal", "sales_lines": "LineTotal", "inventory": "OnHand"}[kind]
        grouped: dict[str, float] = defaultdict(float)
        for x in live:
            grouped[str(x.get(key) or "—")] += _num(x.get(money)) or 0
        lines.append(f"By {key} ({money} summed), biggest first:")
        lines += [f"  - {k}: {_amount(v)}" for k, v in sorted(grouped.items(), key=lambda kv: -kv[1])[:30]]
    lines.append(f"Rows (newest first, {min(limit, len(raws))} of {len(raws)}):")
    for x in raws[:limit]:
        lines.append("  - " + ", ".join(f"{k}={v}" for k, v in x.items() if v not in (None, "")))
    return "\n".join(lines)


def _digits(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() and 1 <= len(text) <= 6 else None


async def _onec_names(client: Any, types: dict[str, set[str]]) -> dict[str, str]:
    """Counterparty / item names for the subconto keys found, one catalog read per type."""
    names: dict[str, str] = {}
    for kind, keys in types.items():
        entity = kind.split(".", 1)[-1]  # "StandardODATA.Catalog_Контрагенты" -> "Catalog_Контрагенты"
        if not entity.startswith("Catalog_") or not keys:
            continue
        try:
            rows = (await client.get(entity, {"$select": "Ref_Key,Description"})).get("value", [])
        except Exception as exc:  # noqa: BLE001 — a name we can't read isn't worth failing the answer
            log.warning("1C {} names not readable: {}", entity, type(exc).__name__)
            continue
        names.update({str(r.get("Ref_Key")): str(r.get("Description") or "") for r in rows})
    return names


async def _onec_rows(resource: str) -> tuple[list[dict], dict[str, tuple[str, str]], Any]:
    """Register rows, the chart (key -> (code, name)), and a name lookup function."""
    from integrations.onec import discover
    from integrations.onec.client import OneCClient

    async with OneCClient(agent=AGENT) as client:
        chart = (await client.get(discover.CHART, {"$select": "Ref_Key,Code,Description"})).get("value", [])
        rows = (await client.get(f"{discover.REGISTER}/{resource}")).get("value", [])
        types: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            if row.get("ExtDimension1") and row.get("ExtDimension1_Type"):
                types[str(row["ExtDimension1_Type"])].add(str(row["ExtDimension1"]))
        names = await _onec_names(client, types)
    accounts = {str(a.get("Ref_Key")): (str(a.get("Code") or "").strip(), str(a.get("Description") or "")) for a in chart}
    return rows, accounts, names


def _onec_report(title: str, rows: list[dict], accounts: dict[str, tuple[str, str]], names: dict[str, str],
                 prefix: str, fields: list[str]) -> str:
    """Rows of one account prefix, summed per account and per counterparty, for each numeric field."""
    mine = [r for r in rows if accounts.get(str(r.get("Account_Key")), ("", ""))[0].startswith(prefix)]
    if not mine:
        found = sorted({c for c, _n in accounts.values() if c.startswith(prefix)})
        return (f"{title}\nNo rows on accounts starting '{prefix}'"
                + (f" (accounts that exist: {', '.join(found[:20])})" if found else " — no such accounts in the chart") + ".")
    lines = [title, f"Fields (as 1C names them): {', '.join(fields)}. Amounts in so'm."]
    per_account: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    per_party: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in mine:
        code, _name = accounts[str(r.get("Account_Key"))]
        party = names.get(str(r.get("ExtDimension1") or ""), "") if r.get("ExtDimension1") else ""
        for f in fields:
            value = _num(r.get(f)) or 0.0
            per_account[code][f] += value
            if party:
                per_party[(code, party)][f] += value
    for code in sorted(per_account):
        name = next((n for c, n in accounts.values() if c == code), "")
        lines.append(f"Account {code} {name}: " + ", ".join(f"{f} {_amount(v)}" for f, v in per_account[code].items()))
    if per_party:
        main = fields[0]
        ranked = sorted(per_party.items(), key=lambda kv: -abs(kv[1][main]))
        lines.append(f"By counterparty ({len(ranked)}), largest {main} first:")
        lines += [f"  - {code} {party}: " + ", ".join(f"{f} {_amount(v)}" for f, v in vals.items() if v)
                  for (code, party), vals in ranked[:50]]
    elif any(r.get("ExtDimension1") for r in mine):
        lines.append("(Counterparty names couldn't be read from 1C — the catalog isn't published to OData.)")
    return "\n".join(lines)


async def onec_balances(args: dict[str, Any]) -> str:
    if not settings.onec_configured:
        return "1C is not connected (ONEC_ODATA_URL / ONEC_LOGIN / ONEC_PASSWORD not set)."
    prefix = _digits(args.get("account_prefix"))
    if prefix is None:
        return "account_prefix must be digits, e.g. '4010' or '40'."
    day = min(parse_day(args.get("as_of"), _today()) or _today(), _today())
    moment = now_local().replace(tzinfo=None) if day == _today() else datetime.combine(day, datetime.max.time())
    rows, accounts, names = await _onec_rows(f"Balance(Period=datetime'{moment:%Y-%m-%dT%H:%M:%S}')")
    fields = sorted({k for r in rows for k, v in r.items() if k.startswith("Сумма") and "Balance" in k
                     and isinstance(v, (int, float)) and not isinstance(v, bool)})
    if not fields:
        return "1C answered, but its balances have no Сумма…Balance field."
    fields.sort(key=lambda f: (f != "СуммаBalance", f))
    return _onec_report(f"1C balances on accounts '{prefix}…' as of {moment:%Y-%m-%d %H:%M} (positive = debit).",
                        rows, accounts, names, prefix, fields)


async def onec_turnovers(args: dict[str, Any]) -> str:
    if not settings.onec_configured:
        return "1C is not connected (ONEC_ODATA_URL / ONEC_LOGIN / ONEC_PASSWORD not set)."
    prefix = _digits(args.get("account_prefix"))
    if prefix is None:
        return "account_prefix must be digits, e.g. '9010' or '94'."
    start, end = period(args)
    rows, accounts, names = await _onec_rows(
        f"Turnovers(StartPeriod=datetime'{start:%Y-%m-%d}T00:00:00',EndPeriod=datetime'{end:%Y-%m-%d}T23:59:59')"
    )
    fields = sorted({k for r in rows for k, v in r.items() if k.startswith("Сумма") and "Turnover" in k
                     and isinstance(v, (int, float)) and not isinstance(v, bool)})
    if not fields:
        return "1C answered, but its turnovers have no Сумма…Turnover field."
    return _onec_report(f"1C turnovers on accounts '{prefix}…', {start} to {end} (Dr = debit, Cr = credit).",
                        rows, accounts, names, prefix, fields)


async def billz_sales(args: dict[str, Any]) -> str:
    from integrations.billz import sales

    if not settings.billz_configured:
        return "BILLZ is not connected (BILLZ_SECRET_TOKEN not set)."
    start, end = period(args)
    return await sales.load_period(end, (end - start).days + 1, run_id=None, agent=AGENT)


def _same_person(name: str, wanted: str) -> bool:
    from integrations.common.translit import name_to_cyrillic

    have = name_to_cyrillic(name).lower()
    return all(part in have for part in name_to_cyrillic(wanted).lower().split())


async def attendance(args: dict[str, Any]) -> str:
    from integrations.verifix import attendance as verifix

    if not settings.verifix_configured:
        return "Verifix is not connected — there is no attendance data."
    start, end = period(args, default_days=7)
    now = now_local().replace(tzinfo=None)
    recs = await verifix.load(start, end, run_id=None, agent=AGENT, now=now)
    wanted = str(args.get("name") or "").strip()
    if wanted:
        recs = [r for r in recs if _same_person(r.name, wanted)]
        if not recs:
            return f"Nobody called '{wanted}' in Verifix for {start}..{end}."
    return f"Period asked: {start} to {end}.\n" + verifix.describe(recs, end, now)


async def supplier_debt_compare(args: dict[str, Any]) -> str:
    from integrations.onec import payables

    if not settings.onec_configured:
        return "1C is not connected (ONEC_ODATA_URL / ONEC_LOGIN / ONEC_PASSWORD not set) — can't compare."
    onec_data = await payables.read_onec(agent=AGENT)
    sap_rows, sap_source = await payables.pushed_sap_rows()
    result = payables.compare(onec_data, sap_rows, sap_source)
    text = payables.summary(result)
    files = _files.get()
    if files is not None and (args.get("excel") is True or _wants_file.get()):
        name = f"kreditorlik-1C-SAP-{_today():%Y-%m-%d}.xlsx"
        files[:] = [f for f in files if f[1] != name] + [(payables.workbook_bytes(result), name)]
        text += "\nThe .xlsx workbook with every supplier will be sent with your answer."
    return text


async def company_data(args: dict[str, Any]) -> str:
    from integrations.org_bot import ops_manager

    source = args.get("source")
    if source not in COMPANY_SOURCES:
        return f"Unknown source {source!r}; one of: {', '.join(COMPANY_SOURCES)}."
    return await ops_manager._fetch_agent_data(source)


HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "data_sources": data_sources,
    "sap_receivables": sap_receivables,
    "sap_records": sap_records,
    "onec_balances": onec_balances,
    "onec_turnovers": onec_turnovers,
    "billz_sales": billz_sales,
    "attendance": attendance,
    "supplier_debt_compare": supplier_debt_compare,
    "company_data": company_data,
}


async def run_tool(name: str, raw_args: Any) -> str:
    """One tool call: validated, bounded, never raising (the model gets the reason instead)."""
    handler = HANDLERS.get(name)
    if handler is None:
        return f"No such tool {name!r}."
    try:
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    started = time.monotonic()
    try:
        text = await handler(args)
    except Exception as exc:  # noqa: BLE001 — a system that's down is an answer, not a crash
        log.warning("Tool {} failed after {:.1f}s: {}", name, time.monotonic() - started, type(exc).__name__)
        return f"{name} could not be read right now ({type(exc).__name__}: {str(exc)[:200]}). Say it's unavailable."
    log.info("Tool {} ok in {:.1f}s ({} chars)", name, time.monotonic() - started, len(text))
    if len(text) > TOOL_CHARS:
        text = text[:TOOL_CHARS] + f"\n[CUT: {len(text) - TOOL_CHARS} more characters not shown — narrow the search]"
    return text


def build_user_message(question: str, history: str, today: date, hint: str | None = None) -> str:
    parts = [f"Today is {today.isoformat()} ({today:%A}), Asia/Tashkent."]
    if history:
        parts.append(f"Recent conversation with the Director (for follow-ups; re-check data, don't copy old answers):\n{history}")
    if hint:
        parts.append(f"(The router thought this is about: {hint}. Look wherever the answer really is.)")
    parts.append(f'Director\'s question:\n"""\n{question}\n"""')
    return "\n\n".join(parts)


async def answer(question: str, history: str, run_id: uuid.UUID, hint: str | None = None) -> Answer:
    """Look things up with the tools until the model answers; at most ``ops_analyst_max_rounds`` rounds.

    Raises:
        OpenRouterError: when the AI itself couldn't be reached (the caller falls back).
    """
    messages: list[dict] = [
        {"role": "system", "content": ANALYST_SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(question, history, _today(), hint)},
    ]
    result = Answer(text="")
    _files.set(result.files)
    _wants_file.set(bool(FILE_WORDS.search(question)))
    async with OpenRouterClient(
        agent=AGENT, run_id=run_id,
        model_override=settings.ops_manager_bot_model, fallback_override=settings.ops_manager_bot_fallback_models,
    ) as ai:
        for _round in range(max(1, settings.ops_analyst_max_rounds)):
            result.rounds += 1
            reply = await ai.chat(messages, TOOLS)
            calls = reply.get("tool_calls") or []
            if not calls:
                result.text = reply.get("content") or ""
                return result
            messages.append(reply)
            for index, call in enumerate(calls):
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                if index < MAX_CALLS_PER_ROUND:
                    content = await run_tool(name, function.get("arguments"))
                    result.tools.append(name)
                else:
                    content = "Skipped: too many lookups at once — ask again for what's still needed."
                messages.append({"role": "tool", "tool_call_id": call.get("id") or "", "content": content})
        messages.append({"role": "user", "content": "That's enough looking up — answer now from what you found."})
        reply = await ai.chat(messages, None)
        result.text = reply.get("content") or ""
    return result
