"""Plan vs fact, and the system's check of what people report (2026-11-02, week 3 of automatic delegation).

The ЭМЖИЕМ МИЯ document: «ҳар даъво тизимда текширилади» — «Сотдим» → SAP; the Boss's monthly plans (Garmin 70K,
Primus 45K, …) against what was sold. Where a system has the data, the bot measures it instead of asking:

  * **sales facts** (``collect``): SAP pushes the last 14 days of invoices every 30 minutes; each line is kept here
    (``sales_facts``) so the month adds up after it leaves the window. Dollars: SAP's local currency is USD — a
    document's total (with VAT) is split over its lines by their net amounts. The product group (Garmin, Tanita,
    Primus, Parts, Londry) comes from SAP's item master; the salesperson from the invoice (``SlpCode``).
  * **who is who**: the bot's people are matched to SAP's salespeople by their names, spelling-tolerant
    (Ходжиев = Хожиев, Ёрқинов = Ёркинов, Мирпўлатов = Мирпулатов).
  * **a duty's fact** (``duty_fact``): a dollar target checked in SAP — the person's own invoices when they are a
    SAP salesperson, else the business named in the duty (Garmin / Primus), dead stock (bought a year or more ago),
    spare parts (Parts); checked in Billz — the shop's net sales at SAP's own dollar rate (so'm ÷ $ of the period's
    invoices). Anything else is not measurable and is never guessed.
  * **plans** (``plans_vs_facts``): the «Режалар» tab against the month's facts, with the pace a month needs.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date, timedelta
from typing import Any

from integrations.common.db import execute, fetch_all, fetch_one
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local

AGENT = "plan-facts"
log = setup_logging(AGENT)

GROUPS = {"garmin": ("Garmin", "Tanita"), "primus": ("Primus", "Parts"), "londry": ("Londry",), "parts": ("Parts",)}
DEAD_STOCK_DAYS = 365
MISMATCH_SHARE = 0.10  # a typed number more than 10 % above the system's fact is flagged


def fold(text: Any) -> str:
    """A name for spelling-tolerant matching: lower case, ў→у, қ→к, ҳ→х, ғ→г, ё→е, дж→ж, letters only."""
    from integrations.common.translit import latin_to_cyrillic

    t = str(text or "")
    t = latin_to_cyrillic(t) if re.search(r"[A-Za-z]", t) else t
    t = t.lower().translate(str.maketrans("ўқҳғё", "укхге")).replace("дж", "ж")
    return " ".join(re.sub(r"[^а-яa-z ]+", " ", t).split())


def slp_of(person: dict[str, Any], salespeople: list[dict[str, Any]]) -> str | None:
    """The SAP salesperson code of a person: every word of their name in SlpName (one match only)."""
    words = set(fold(person.get("full_name") or person.get("display_name")).split())
    if len(words) < 2:
        return None
    found = [s for s in salespeople if words <= set(fold(s.get("SlpName")).split())]
    return str(found[0]["SlpCode"]) if len(found) == 1 else None


def lines_to_facts(headers: list[dict[str, Any]], lines: list[dict[str, Any]], groups: dict[str, str]) -> list[dict[str, Any]]:
    """SAP's pushed invoices → one fact per line: the document's gross total split by the lines' net amounts (USD),
    so'm the same way; credit notes negative; cancelled documents left out."""
    head = {(str(h.get("ObjType") or "13"), str(h.get("DocEntry"))): h for h in headers}
    by_doc: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for line in lines:
        by_doc.setdefault((str(line.get("ObjType") or "13"), str(line.get("DocEntry"))), []).append(line)
    out = []
    for key, mine in by_doc.items():
        h = head.get(key, mine[0])
        if str(h.get("CANCELED") or mine[0].get("CANCELED") or "N").upper() != "N":
            continue
        net = sum(float(x.get("LineTotal") or 0) for x in mine) or 1.0
        sign = -1.0 if key[0] == "14" else 1.0
        total_usd, total_som = float(h.get("DocTotal") or 0), float(h.get("DocTotalSy") or 0)
        for x in mine:
            share = float(x.get("LineTotal") or 0) / net
            out.append({"obj_type": key[0], "doc_entry": key[1], "line_num": int(x.get("LineNum") or 0),
                        "day": str(h.get("DocDate") or x.get("DocDate"))[:10], "slp_code": str(h.get("SlpCode") or x.get("SlpCode") or ""),
                        "item_code": str(x.get("ItemCode") or ""), "item_group": groups.get(str(x.get("ItemCode") or ""), ""),
                        "wh": str(x.get("WhsCode") or "").strip(), "amount_usd": round(sign * total_usd * share, 2),
                        "amount_som": round(sign * total_som * share, 2)})
    return out


def usd_rate(lines: list[dict[str, Any]]) -> float | None:
    """So'm per dollar, from SAP's own lines (system-currency total ÷ local-currency total) — the median."""
    rates = [float(x["TotalSumSy"]) / float(x["LineTotal"]) for x in lines
             if float(x.get("LineTotal") or 0) > 0 and float(x.get("TotalSumSy") or 0) > 0]
    return statistics.median(rates) if rates else None


async def _tool(tool: str) -> list[dict[str, Any]]:
    rows = await fetch_all("SELECT raw FROM v_sap_gateway_latest WHERE tool = %s", (tool,))
    return [r["raw"] if isinstance(r["raw"], dict) else json.loads(r["raw"] or "{}") for r in rows]


PUSH_WINDOW_SAFE_DAYS = 13  # SAP pushes invoices dated / entered in the last 14 days; one day's margin


def gone_documents(kept: list[dict[str, Any]], pushed: set[tuple[str, str]], start: date) -> list[tuple[str, str]]:
    """Documents kept in ``sales_facts`` that are dated inside the push window but no longer pushed: cancelled (or
    deleted) in SAP — the gateway sends only CANCELED = 'N' (2026-11-10 review: a cancelled invoice stayed in the
    month's sales for good)."""
    gone = {(str(r["obj_type"]), str(r["doc_entry"])) for r in kept
            if r.get("day") is not None and r["day"] >= start} - pushed
    return sorted(gone)


COLLECT_EVERY = 600.0  # seconds: PULSE refreshes every 5 minutes and /reja may be tapped — the 0.1-CPU database
_last_collect = -COLLECT_EVERY


async def collect(force: bool = False) -> int:
    """Keep the latest pushed invoice lines (a changed document replaces its lines). How many lines are kept now;
    -1 when it ran less than COLLECT_EVERY ago (2026-11-03 review: every PULSE refresh rewrote ~130 rows)."""
    import time

    global _last_collect
    if not force and time.monotonic() - _last_collect < COLLECT_EVERY:
        return -1
    _last_collect = time.monotonic()
    headers, lines, products = await _tool("sales"), await _tool("sales_lines"), await _tool("products")
    if not lines:
        return 0
    groups = {str(p.get("ItemCode")): str(p.get("ItmsGrpNam") or "") for p in products}
    facts = lines_to_facts(headers, lines, groups)
    # Cancelled documents leave the push: drop their kept lines — only inside the window the push covers, measured
    # from the day SAP last sent, so a stale or failed push never removes anything.
    pushed = {(str(r.get("ObjType") or "13"), str(r.get("DocEntry"))) for r in headers + lines if r.get("DocEntry")}
    sent = await fetch_one("SELECT (max(captured_at) AT TIME ZONE 'Asia/Tashkent')::date AS d "
                           "FROM v_sap_gateway_latest WHERE tool = 'sales_lines'")
    if pushed and sent and sent.get("d"):
        start = sent["d"] - timedelta(days=PUSH_WINDOW_SAFE_DAYS)
        kept = await fetch_all("SELECT DISTINCT obj_type, doc_entry, day FROM sales_facts WHERE day >= %s", (start,))
        gone = gone_documents(kept, pushed, start)
        for doc in gone:
            await execute("DELETE FROM sales_facts WHERE obj_type = %s AND doc_entry = %s", doc)
        if gone:
            log.info("Sales facts: {} cancelled document(s) removed", len(gone))
    for doc in {(f["obj_type"], f["doc_entry"]) for f in facts}:
        await execute("DELETE FROM sales_facts WHERE obj_type = %s AND doc_entry = %s", doc)
    for f in facts:
        await execute(
            """INSERT INTO sales_facts (obj_type, doc_entry, line_num, day, slp_code, item_code, item_group, wh, amount_usd, amount_som)
               VALUES (%(obj_type)s, %(doc_entry)s, %(line_num)s, %(day)s, %(slp_code)s, %(item_code)s, %(item_group)s, %(wh)s,
                       %(amount_usd)s, %(amount_som)s)
               ON CONFLICT (obj_type, doc_entry, line_num) DO UPDATE SET day = EXCLUDED.day, slp_code = EXCLUDED.slp_code,
                   item_group = EXCLUDED.item_group, amount_usd = EXCLUDED.amount_usd, amount_som = EXCLUDED.amount_som""", f)
    return len(facts)


async def rate_now() -> float | None:
    return usd_rate(await _tool("sales_lines"))


async def dead_items(day: date) -> list[str]:
    cut = day - timedelta(days=DEAD_STOCK_DAYS)
    return [str(p.get("ItemCode")) for p in await _tool("products")
            if p.get("LastPurDat") and str(p["LastPurDat"])[:10] <= cut.isoformat()]


async def sum_usd(start: date, end: date, *, slp: str | None = None, groups: tuple[str, ...] | None = None,
                  items: list[str] | None = None) -> float:
    where, params = ["day BETWEEN %s AND %s"], [start, end]
    if slp is not None:
        where.append("slp_code = %s")
        params.append(slp)
    if groups is not None:
        where.append("item_group = ANY(%s)")
        params.append(list(groups))
    if items is not None:
        where.append("item_code = ANY(%s)")
        params.append(items)
    row = await fetch_one(f"SELECT coalesce(sum(amount_usd), 0) AS s FROM sales_facts WHERE {' AND '.join(where)}", params)
    return float(row["s"]) if row else 0.0


async def billz_usd(start: date, end: date) -> float | None:
    """The shop's net sales (Billz) in dollars at SAP's rate; None when Billz or the rate can't be read."""
    import uuid

    from integrations.billz.client import BillzClient
    from integrations.billz.sales import day_sales

    rate = await rate_now()
    if not rate:
        return None
    try:
        async with BillzClient(agent=AGENT, run_id=uuid.uuid4()) as client:
            rows = await client.shop_days(start, end)
    except Exception as exc:  # noqa: BLE001
        log.warning("Billz not read: {}", type(exc).__name__)
        return None
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return sum(day_sales(rows, d).net_sales for d in days) / rate


def measurable(duty: dict[str, Any]) -> bool:
    """A dollar target the system can check (SAP or Billz)."""
    return str(duty.get("unit") or "").strip() == "$" and str(duty.get("verify") or "").strip() in ("SAP", "Billz")


def scope_of(duty: dict[str, Any]) -> str:
    """What a SAP-checked duty counts: dead stock, spare parts, a business, or the person's own sales."""
    t = fold(duty.get("title"))
    if "улик" in t or "эски garmin" in t or "эски" in t and "колдиг" in t:
        return "dead"
    if "эхтиет" in t or "кисм" in t:
        return "parts"
    if "garmin" in t or "гармин" in t:
        return "garmin"
    if "primus" in t or "примус" in t:
        return "primus"
    return "person"


async def duty_fact(duty: dict[str, Any], person: dict[str, Any], start: date, end: date,
                    salespeople: list[dict[str, Any]]) -> tuple[float | None, str]:
    """(dollars, where from) for a measurable duty over [start, end]; (None, why not) otherwise."""
    if not measurable(duty):
        return None, "қўлда"
    if str(duty.get("verify")).strip() == "Billz":
        value = await billz_usd(start, end)
        return (value, "Billz") if value is not None else (None, "Billz ўқилмади")
    scope = scope_of(duty)
    slp = slp_of(person, salespeople)
    if scope == "dead":
        return await sum_usd(start, end, slp=slp, items=await dead_items(end)), "SAP (ўлик захира)"
    if scope == "parts":
        return await sum_usd(start, end, slp=slp, groups=GROUPS["parts"]), "SAP (Parts)"
    if scope in ("garmin", "primus") and slp is None:
        return await sum_usd(start, end, groups=GROUPS[scope]), f"SAP ({scope.capitalize()})"
    if slp is None:
        return None, "SAP'да сотувчи сифатида йўқ"
    return await sum_usd(start, end, slp=slp), "SAP (сотувчи)"


async def fill_duty_facts(day: date) -> int:
    """Today's measurable duty tasks get their fact (and a typed number far above it is flagged). How many filled."""
    await collect()
    salespeople = await _tool("sales_people")
    filled = 0
    rows = await fetch_all(
        """SELECT t.*, d.verify, d.created_at AS duty_created, e.full_name, e.display_name FROM duty_tasks t
           JOIN duties d ON d.id = t.duty_id JOIN employees e ON e.id = t.employee_id
           WHERE t.period_start <= %s AND t.period_end >= %s AND trim(t.unit) = '$' AND d.verify IN ('SAP', 'Billz')""",
        (day, day))
    for t in rows:
        start = t["period_start"] if t["cadence"] != "deadline" else min(t["duty_created"].date(), t["period_start"])
        value, source = await duty_fact(t, t, start, min(day, t["period_end"]), salespeople)
        if value is None:
            continue
        mismatch = t.get("value") is not None and float(t["value"]) > value * (1 + MISMATCH_SHARE) + 1
        await execute("UPDATE duty_tasks SET fact = %s, fact_source = %s, fact_at = now(), mismatch = %s WHERE id = %s",
                      (round(value, 2), source, mismatch, t["id"]))
        filled += 1
    return filled


def direction_scope(name: str) -> str | None:
    t = fold(name)
    for key, words in (("garmin", ("garmin", "гармин")), ("primus", ("primus", "примус")), ("parts", ("эхтиет", "кисм")),
                       ("dead", ("улик", "туриб"))):
        if any(w in t for w in words):
            return key
    return None


def _amount(text: str) -> float | None:
    nums = [float(x) for x in re.findall(r"\d+", (text or "").replace(" ", ""))]
    return sum(nums) / len(nums) if nums else None


async def plans_vs_facts(day: date | None = None) -> list[dict[str, Any]]:
    """The «Режалар» tab against the month so far: plan, fact, %, the pace needed (the month's share of days gone)."""
    day = day or today_local()
    await collect()
    start = day.replace(day=1)
    out = []
    for p in await fetch_all("SELECT * FROM monthly_plans ORDER BY direction"):
        scope = direction_scope(p["direction"])
        plan = _amount(p["amount_usd"])
        if scope is None:
            fact = None
        elif scope == "dead":
            fact = await sum_usd(start, day, items=await dead_items(day))
        else:
            fact = await sum_usd(start, day, groups=GROUPS[scope])
        out.append({"direction": p["direction"], "plan": plan, "plan_text": p["amount_usd"], "fact": fact, "scope": scope,
                    "share": (fact / plan) if fact is not None and plan else None})
    return out


def money(v: float | None) -> str:
    return "—" if v is None else "$" + f"{v:,.0f}".replace(",", " ")


def plans_text(rows: list[dict[str, Any]], day: date) -> str:
    """/reja: the month's plans and facts (the Director). Plan names come from the sheet — escaped."""
    import calendar

    from integrations.telegram.bot import escape

    pace = day.day / calendar.monthrange(day.year, day.month)[1]
    lines = [f"🎯 <b>Ойлик режа ва факт</b> · {day:%d.%m} (ой {pace:.0%} ўтди)", ""]
    for r in rows:
        if r["fact"] is None:
            lines.append(f"▫️ {escape(r['direction'])}: режа ${escape(r['plan_text'])} — <i>SAP'да алоҳида ҳисобланмайди</i>")
            continue
        mark = "🟢" if r["share"] is not None and r["share"] >= pace else "🔴"
        share = f" ({r['share']:.0%})" if r["share"] is not None else ""
        lines.append(f"{mark} {escape(r['direction'])}: {money(r['fact'])} / ${escape(r['plan_text'])}{share}")
    lines += ["", "<i>Факт — SAP счёт-фактуралари (ҚҚС билан, қайтаришлар айрилган), товар гуруҳи бўйича. 🟢 — ой ўтган "
              "қисмидан олдинда, 🔴 — орқада. Сервис шартномалари ва химикат SAP'да алоҳида гуруҳ эмас.</i>"]
    return "\n".join(lines)
