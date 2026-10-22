"""PULSE data — the company line, the tickers, each person's line and day (2026-10-22).

Read-only. Sources: the bot's own tables (tasks and their events, daily reports, explanations,
complaints), Verifix attendance (live), BILLZ shop sales (live), SAP sales (the latest push). A source
that can't be read leaves its part empty — never a guessed zero. Built for a period of 7, 30 or 90 days,
with the period before it for the ▲▼; kept ``CACHE_SECONDS`` per scope.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.db import fetch_all
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local, to_local
from integrations.pulse import score as sc

AGENT = "pulse"
log = setup_logging(AGENT)

RANGES = (7, 30, 90)
CACHE_SECONDS = 600
NOT_SCORED = ("operatsion_direktor", "garmin_manager")  # the Boss and the Garmin manager themselves
BUSINESS = {"garmin_sotuv": "garmin", "b2b_sotuv": "primus", "londry": "londry"}  # everyone else: the office
BUSINESS_LABELS = {"garmin": "Garmin", "primus": "Primus", "londry": "Londry", "office": "Офис"}
_cache: dict[tuple[int, tuple[str, ...] | None], tuple[float, dict[str, Any]]] = {}


@dataclass
class Person:
    id: str
    tg: int
    name: str
    role: str
    role_label: str
    business: str
    verifix_id: str | None = None


def _local_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_local(value).date() if value.tzinfo else value.date()
    return value


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


async def people(roles: tuple[str, ...] | None) -> list[Person]:
    from integrations.org_bot import names
    from integrations.org_bot.roles import ROLE_LABELS

    rows = await fetch_all("SELECT * FROM employees WHERE status = 'active' ORDER BY display_name")
    out = []
    for e in rows:
        role = e.get("role") or ""
        if role in NOT_SCORED or (roles is not None and role not in roles):
            continue
        out.append(Person(str(e["id"]), int(e["telegram_user_id"]), names.person_name(e), role,
                          ROLE_LABELS.get(role, role), BUSINESS.get(role, "office")))
    return out


async def _verifix(start: date, end: date, staff: list[Person], run_id: uuid.UUID) -> list[Any] | None:
    """Verifix days of the period, each tied to a person (by name); None when Verifix can't be read."""
    from integrations.common.config import settings
    from integrations.org_bot import explanations

    if not settings.verifix_configured:
        return None
    try:
        from integrations.verifix import attendance

        recs = await attendance.load(start, end, run_id, AGENT, now=now_local().replace(tzinfo=None))
    except Exception as exc:  # noqa: BLE001 — attendance stays empty, the rest still shows
        log.warning("Verifix not read: {}", type(exc).__name__)
        return None
    as_rows = [{"id": p.id, "full_name": p.name} for p in staff]
    tie: dict[str, str | None] = {}
    for r in recs:
        if r.employee_id not in tie:
            hit = explanations.match_employee(r.name, as_rows)
            tie[r.employee_id] = hit["id"] if hit else None
    for p in staff:
        p.verifix_id = next((vid for vid, pid in tie.items() if pid == p.id), None)
    return [r for r in recs if tie.get(r.employee_id)]


async def _sap_sales(start: date, end: date, garmin_only: bool) -> dict[date, float] | None:
    """SAP invoices minus credit notes per document date, in so'm; Garmin's warehouses only for the Garmin manager."""
    from integrations.org_bot import old_stock

    try:
        rows = await fetch_all("SELECT tool, raw FROM v_sap_gateway_latest WHERE tool IN ('sales', 'sales_lines', 'warehouses')")
    except Exception as exc:  # noqa: BLE001
        log.warning("SAP sales not read: {}", type(exc).__name__)
        return None
    kinds: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        kinds[r["tool"]].append(r["raw"] if isinstance(r["raw"], dict) else {})
    if not kinds["sales"]:
        return None
    keep: set[tuple[str, str]] | None = None
    if garmin_only:
        codes = old_stock.garmin_codes(kinds["warehouses"])
        keep = {(str(x.get("ObjType") or "13"), str(x.get("DocEntry"))) for x in kinds["sales_lines"]
                if str(x.get("WhsCode") or "").strip() in codes}
    out: dict[date, float] = defaultdict(float)
    for h in kinds["sales"]:
        if str(h.get("CANCELED") or "N").upper() != "N":
            continue
        key = (str(h.get("ObjType") or "13"), str(h.get("DocEntry")))
        if keep is not None and key not in keep:
            continue
        try:
            day = date.fromisoformat(str(h.get("DocDate") or "")[:10])
            amount = float(h.get("DocTotalSy") or 0)
        except (TypeError, ValueError):
            continue
        if start <= day <= end:
            out[day] += -amount if key[0] == "14" else amount
    return dict(out)


async def _billz(start: date, end: date, run_id: uuid.UUID) -> dict[date, float] | None:
    from integrations.common.config import settings

    if not settings.billz_configured:
        return None
    try:
        from integrations.billz.client import BillzClient

        async with BillzClient(agent=AGENT, run_id=run_id) as client:
            rows = await client.shop_days(start, end)
    except Exception as exc:  # noqa: BLE001
        log.warning("Billz not read: {}", type(exc).__name__)
        return None
    out: dict[date, float] = defaultdict(float)
    for r in rows:
        try:
            out[date.fromisoformat(str(r.get("date") or "")[:10])] += float(r.get("net_gross_sales") or 0)
        except (TypeError, ValueError):
            continue
    return dict(out)


async def _db(start: date, end: date, ids: list[str], garmin_only: bool) -> dict[str, list[dict[str, Any]]]:
    reports = await fetch_all(
        """SELECT employee_id::text AS pid, report_date, status, auto_built, submitted_at, penalty_points
           FROM daily_reports WHERE report_date BETWEEN %s AND %s AND employee_id::text = ANY(%s)""", (start, end, ids))
    tasks = await fetch_all(
        """SELECT id::text, code, task_summary, assigned_employee_id::text AS pid, status, due_date, created_at, completed_at
           FROM tasks WHERE assigned_employee_id::text = ANY(%s)
             AND (created_at >= %s OR status NOT IN ('done', 'cancelled') OR completed_at >= %s)""",
        (ids, start - timedelta(days=60), start))
    complaints = await fetch_all(
        """SELECT created_at, ticket_status, resolved_at, place FROM client_feedback
           WHERE kind = 'complaint' AND (created_at >= %s OR ticket_status = 'open')""", (start,))
    if garmin_only:
        complaints = [c for c in complaints if c.get("place") == "garmin"]
    return {"reports": reports, "tasks": tasks, "complaints": complaints}


def _series(values: dict[date, float | None], days: list[date]) -> list[dict[str, Any]]:
    return [{"d": d.isoformat(), "v": values.get(d)} for d in days]


def _share(hits: int, total: int) -> float | None:
    return round(100 * hits / total, 1) if total else None


def assemble(staff: list[Person], data: dict[str, list[dict[str, Any]]], recs: list[Any] | None,
             sap: dict[date, float] | None, billz: dict[date, float] | None, days: int, now: datetime) -> dict[str, Any]:
    """Everything the page shows for one period — pure, so it is tested offline."""
    today = now.date()
    cur = _days(today - timedelta(days=days - 1), today)
    prev = _days(today - timedelta(days=2 * days - 1), today - timedelta(days=days))
    every = prev + cur
    reports = {(r["pid"], r["report_date"]): r for r in data["reports"]}
    tasks_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in data["tasks"]:
        tasks_of[t["pid"]].append(t)
    att: dict[tuple[str, date], Any] = {}
    by_verifix = {p.verifix_id: p.id for p in staff if p.verifix_id}
    for r in recs or []:
        pid = by_verifix.get(r.employee_id)
        if pid:
            att[(pid, r.day)] = r

    scores: dict[str, dict[date, int | None]] = {}
    for p in staff:
        scores[p.id] = {}
        for d in every:
            a = att.get((p.id, d))
            parts = {
                "attendance": sc.attendance_value(a.status, a.late_minutes) if a else None,
                "report": sc.report_value(reports.get((p.id, d)), d, now),
                "tasks": sc.task_value(tasks_of[p.id], d, _local_day),
            }
            scores[p.id][d] = sc.day_score(parts)

    company = {d: sc.average(scores[p.id][d] for p in staff) for d in every}
    company_now, company_before = sc.average(company[d] for d in cur), sc.average(company[d] for d in prev)

    def period(days_: list[date]) -> dict[str, Any]:
        sent = asked = on_time = judged = done_ok = in_play = 0
        for p in staff:
            for d in days_:
                r = reports.get((p.id, d))
                v = sc.report_value(r, d, now)
                if v is not None:
                    asked += 1
                    sent += 1 if v > 0 else 0
                a = att.get((p.id, d))
                av = sc.attendance_value(a.status, a.late_minutes) if a else None
                if av is not None:
                    judged += 1
                    on_time += 1 if av == 1.0 else 0
        for p in staff:
            for t in tasks_of[p.id]:
                due = t.get("due_date")
                if due is None or not (days_[0] <= due <= days_[-1]) or t.get("status") == "cancelled":
                    continue
                if due > today:
                    continue
                in_play += 1
                done = _local_day(t.get("completed_at")) if t.get("status") == "done" else None
                done_ok += 1 if done and done <= due else 0
        new_complaints = sum(1 for c in data["complaints"] if days_[0] <= _local_day(c["created_at"]) <= days_[-1])
        return {"reports": _share(sent, asked), "attendance": _share(on_time, judged), "tasks": _share(done_ok, in_play),
                "complaints": new_complaints}

    now_p, before_p = period(cur), period(prev)

    def money(source: dict[date, float] | None) -> dict[str, Any] | None:
        if source is None:
            return None
        total_now = sum(source.get(d, 0.0) for d in cur)
        total_before = sum(source.get(d, 0.0) for d in prev)
        return {"value": round(total_now), "delta": sc.delta(total_now, total_before, pct=True),
                "spark": [round(source.get(d, 0.0)) for d in cur]}

    def daily_share(kind: str) -> list[float | None]:
        return [period([d])[kind] for d in cur]

    open_now = sum(1 for c in data["complaints"] if c.get("ticket_status") == "open")
    tickers = [
        {"key": "sales", "label": "Сотув", "unit": "som", **(money(sap) or {"value": None, "delta": None, "spark": []})},
        {"key": "shop", "label": "Дўкон", "unit": "som", **(money(billz) or {"value": None, "delta": None, "spark": []})},
        {"key": "tasks", "label": "Топшириқ", "unit": "pct", "value": now_p["tasks"],
         "delta": sc.delta(now_p["tasks"], before_p["tasks"]), "spark": daily_share("tasks")},
        {"key": "reports", "label": "Ҳисобот", "unit": "pct", "value": now_p["reports"],
         "delta": sc.delta(now_p["reports"], before_p["reports"]), "spark": daily_share("reports")},
        {"key": "attendance", "label": "Давомат", "unit": "pct", "value": now_p["attendance"] if recs is not None else None,
         "delta": sc.delta(now_p["attendance"], before_p["attendance"]) if recs is not None else None,
         "spark": daily_share("attendance") if recs is not None else []},
        {"key": "complaints", "label": "Шикоят", "unit": "count", "value": open_now, "invert": True,
         "delta": sc.delta(now_p["complaints"], before_p["complaints"]), "spark": [period([d])["complaints"] for d in cur]},
    ]

    board = []
    for p in staff:
        mine_now = sc.average(scores[p.id][d] for d in cur)
        mine_before = sc.average(scores[p.id][d] for d in prev)
        a = att.get((p.id, today))
        board.append({"id": p.id, "name": p.name, "role": p.role_label, "business": p.business,
                      "score": None if mine_now is None else round(mine_now),
                      "delta": sc.delta(None if mine_now is None else round(mine_now, 1),
                                        None if mine_before is None else round(mine_before, 1)),
                      "spark": [scores[p.id][d] for d in cur],
                      "today": a.status if a else None})
    board.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0), x["name"]))

    sectors = []
    for key in ("garmin", "primus", "londry", "office"):
        mine = [p for p in staff if p.business == key]
        if not mine:
            continue
        line = {d: sc.average(scores[p.id][d] for p in mine) for d in every}
        now_v, before_v = sc.average(line[d] for d in cur), sc.average(line[d] for d in prev)
        sectors.append({"key": key, "label": BUSINESS_LABELS[key],
                        "score": None if now_v is None else round(now_v),
                        "delta": sc.delta(None if now_v is None else round(now_v, 1), None if before_v is None else round(before_v, 1)),
                        "series": _series({d: None if line[d] is None else round(line[d], 1) for d in cur}, cur),
                        "people": [p.id for p in mine]})

    return {
        "asOf": now.strftime("%d.%m.%Y %H:%M"), "days": days,
        "index": {"value": None if company_now is None else round(company_now),
                  "delta": sc.delta(None if company_now is None else round(company_now, 1),
                                    None if company_before is None else round(company_before, 1)),
                  "series": _series({d: None if company[d] is None else round(company[d], 1) for d in cur}, cur),
                  "volume": _series({d: round(sap.get(d, 0.0)) for d in cur} if sap is not None else {}, cur)},
        "tickers": tickers, "people": board, "sectors": sectors,
        "sources": {"verifix": recs is not None, "sap": sap is not None, "billz": billz is not None},
    }


async def overview(days: int, roles: tuple[str, ...] | None) -> dict[str, Any]:
    """The page's data for a period and a scope (None = everyone), from the cache when fresh."""
    days = days if days in RANGES else 30
    key = (days, roles)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    now = now_local()
    start = now.date() - timedelta(days=2 * days - 1)
    run_id = uuid.uuid4()
    staff = await people(roles)
    data = await _db(start, now.date(), [p.id for p in staff], roles is not None)
    recs = await _verifix(start, now.date(), staff, run_id)
    sap = await _sap_sales(start, now.date(), roles is not None)
    billz = await _billz(start, now.date(), run_id)
    result = assemble(staff, data, recs, sap, billz, days, now)
    _cache[key] = (time.monotonic(), result)
    if len(_cache) > 12:
        _cache.pop(next(iter(_cache)))
    return result


# ---- one person ---------------------------------------------------------------------------------------------

TASK_EVENT_LABELS = {"created": "Топшириқ олди", "accepted": "Бошлади", "blocked": "Тўсиқ", "unblocked": "Давом этди",
                     "submitted": "Бажардим деди", "closed": "Қабул қилинди", "returned": "Қайтарилди",
                     "cancelled": "Бекор қилинди", "repeated": "Қайта эслатилди"}


def timeline(day: date, rec: Any | None, task_events: list[dict[str, Any]], report: dict[str, Any] | None,
             explanations_: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One person's day, in time order: arrival and leaving, task steps, the report, an explanation."""
    items: list[tuple[str, str, str, str]] = []  # (HH:MM, kind, label, detail)
    if rec is not None:
        if rec.arrived:
            late = f"+{rec.late_minutes} дақ" if rec.status == "late" else ""
            items.append((f"{rec.arrived:%H:%M}", "late" if late else "in", "Келди", late))
        elif rec.status == "absent":
            items.append(("", "absent", "Келмади", ""))
        if rec.left:
            items.append((f"{rec.left:%H:%M}", "out", "Кетди", ""))
    for e in task_events:
        items.append((f"{to_local(e['at']):%H:%M}", "task", TASK_EVENT_LABELS.get(e["event"], e["event"]),
                      f"{e.get('code') or ''} {' '.join((e.get('task_summary') or '').split())[:60]}".strip()))
    if report is not None:
        if report.get("submitted_at"):
            items.append((f"{to_local(report['submitted_at']):%H:%M}", "report",
                          "Ҳисобот (чатдан)" if report.get("auto_built") else "Ҳисобот", ""))
        elif report.get("status") == "asked":
            items.append(("", "missed", "Ҳисобот йўқ", ""))
    for x in explanations_:
        if x.get("answered_at"):
            items.append((f"{to_local(x['answered_at']):%H:%M}", "explain", "Тушунтириш ёзди", ""))
    items.sort(key=lambda i: (i[0] == "", i[0]))
    return [{"t": t, "kind": k, "label": label, "detail": d} for t, k, label, d in items]


async def person(person_id: str, days: int, day: date | None, roles: tuple[str, ...] | None) -> dict[str, Any] | None:
    """One person's line (daily score with its parts) and one day's timeline; None outside the asker's scope."""
    staff = await people(roles)
    me = next((p for p in staff if p.id == person_id), None)
    if me is None:
        return None
    days = days if days in RANGES else 30
    now = now_local()
    start = now.date() - timedelta(days=days - 1)
    day = day if day and start <= day <= now.date() else now.date()
    data = await _db(start, now.date(), [me.id], False)
    recs = await _verifix(start, now.date(), [me], uuid.uuid4()) or []
    att = {r.day: r for r in recs}
    reports = {r["report_date"]: r for r in data["reports"]}
    line = []
    for d in _days(start, now.date()):
        a = att.get(d)
        parts = {"attendance": sc.attendance_value(a.status, a.late_minutes) if a else None,
                 "report": sc.report_value(reports.get(d), d, now),
                 "tasks": sc.task_value(data["tasks"], d, _local_day)}
        line.append({"d": d.isoformat(), "v": sc.day_score(parts),
                     "parts": {k: None if v is None else round(100 * v) for k, v in parts.items()}})
    events = await fetch_all(
        """SELECT e.at, e.event, t.code, t.task_summary FROM task_events e JOIN tasks t ON t.id = e.task_id
           WHERE t.assigned_employee_id::text = %s AND (e.at AT TIME ZONE 'Asia/Tashkent')::date = %s ORDER BY e.at""",
        (me.id, day))
    explanations_ = await fetch_all(
        "SELECT answered_at FROM explanations WHERE employee_id::text = %s AND (answered_at AT TIME ZONE 'Asia/Tashkent')::date = %s",
        (me.id, day))
    return {"id": me.id, "name": me.name, "role": me.role_label, "business": me.business, "line": line,
            "day": day.isoformat(), "timeline": timeline(day, att.get(day), events, reports.get(day), explanations_)}


# ---- the tape: today's actions, newest first ----------------------------------------------------------------

TAPE_SECONDS = 120
TAPE_MAX = 80
_tape_cache: dict[tuple[str, ...] | None, tuple[float, list[dict[str, Any]]]] = {}


def tape_items(staff: list[Person], recs: list[Any] | None, events: list[dict[str, Any]], reports: list[dict[str, Any]],
               answers: list[dict[str, Any]], complaints: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Today's actions as tape rows (time, who, what, kind) — pure, so it is tested offline."""
    by_id = {p.id: p for p in staff}
    by_verifix = {p.verifix_id: p for p in staff if p.verifix_id}
    rows: list[dict[str, Any]] = []

    def add(at: datetime | None, who: Person | None, label: str, kind: str, detail: str = "") -> None:
        if at is None:
            return
        rows.append({"t": f"{at:%H:%M}", "sort": at.strftime("%H%M%S"), "who": who.name if who else "",
                     "pid": who.id if who else None, "label": label, "kind": kind, "detail": detail})

    for r in recs or []:
        p = by_verifix.get(r.employee_id)
        if p and r.day == today and r.arrived:
            add(r.arrived, p, "Келди", "late" if r.status == "late" else "in",
                f"+{r.late_minutes} дақ" if r.status == "late" else "")
        if p and r.day == today and r.left:
            add(r.left, p, "Кетди", "out")
    for e in events:
        add(to_local(e["at"]), by_id.get(e["pid"]), TASK_EVENT_LABELS.get(e["event"], e["event"]), "task", e.get("code") or "")
    for r in reports:
        if r.get("submitted_at"):
            add(to_local(r["submitted_at"]), by_id.get(r["pid"]), "Ҳисобот", "report")
    for x in answers:
        add(to_local(x["answered_at"]), by_id.get(x["pid"]), "Тушунтириш", "explain")
    from integrations.org_bot.feedback import PLACES

    for c in complaints:
        place = PLACES.get(c.get("place") or "", "")  # «Londry» / «Garmin», never the raw key
        if _local_day(c["created_at"]) == today:
            add(to_local(c["created_at"]), None, "Шикоят келди", "complaint", place)
        if c.get("resolved_at") and _local_day(c["resolved_at"]) == today:
            add(to_local(c["resolved_at"]), None, "Шикоят ҳал бўлди", "resolved", place)
    rows = [r for r in rows if r["pid"] is not None or r["kind"] in ("complaint", "resolved")]
    rows.sort(key=lambda r: r["sort"], reverse=True)
    for r in rows:
        del r["sort"]
    return rows[:TAPE_MAX]


async def tape(roles: tuple[str, ...] | None) -> list[dict[str, Any]]:
    hit = _tape_cache.get(roles)
    if hit and time.monotonic() - hit[0] < TAPE_SECONDS:
        return hit[1]
    today = now_local().date()
    staff = await people(roles)
    ids = [p.id for p in staff]
    recs = await _verifix(today, today, staff, uuid.uuid4())
    events = await fetch_all(
        """SELECT e.at, e.event, t.code, t.assigned_employee_id::text AS pid FROM task_events e JOIN tasks t ON t.id = e.task_id
           WHERE t.assigned_employee_id::text = ANY(%s) AND (e.at AT TIME ZONE 'Asia/Tashkent')::date = %s""", (ids, today))
    reports = await fetch_all(
        """SELECT employee_id::text AS pid, submitted_at FROM daily_reports
           WHERE employee_id::text = ANY(%s) AND (submitted_at AT TIME ZONE 'Asia/Tashkent')::date = %s""", (ids, today))
    answers = await fetch_all(
        """SELECT employee_id::text AS pid, answered_at FROM explanations
           WHERE employee_id::text = ANY(%s) AND (answered_at AT TIME ZONE 'Asia/Tashkent')::date = %s""", (ids, today))
    complaints = await fetch_all(
        """SELECT created_at, resolved_at, place FROM client_feedback WHERE kind = 'complaint'
           AND ((created_at AT TIME ZONE 'Asia/Tashkent')::date = %s OR (resolved_at AT TIME ZONE 'Asia/Tashkent')::date = %s)""",
        (today, today))
    if roles is not None:
        complaints = [c for c in complaints if c.get("place") == "garmin"]
    result = tape_items(staff, recs, events, reports, answers, complaints, today)
    _tape_cache[roles] = (time.monotonic(), result)
    return result
