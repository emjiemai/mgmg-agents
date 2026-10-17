"""The daily-report calendar — one picture of the month (2026-10-17, the owner).

"When an employee writes the report it is green, if not red — an image, just
like the daily brief." People down the side, the month's days across:

  * green — sent;
  * red — asked and not sent by 22:00 (the minus point, ``report_close.py``);
  * amber — asked today and still open (before 22:00);
  * grey — not asked: a weekend for someone who doesn't work it, a day off
    (/dam), reports switched off, or before the person joined;
  * blank — days still to come.

The Director gets it with /hisobotlar (OPS Manager Bot); it is drawn from
``daily_reports`` (the asks are the truth: a row exists for everyone asked).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.timeutil import now_local
from integrations.org_bot import names
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS
from integrations.org_bot.task_tracker import MONTHS_UZ

COMMANDS = ("/hisobotlar", "/ҳисоботлар")
WEEKDAYS = ("Ду", "Се", "Чо", "Па", "Жу", "Ша", "Як")
REPORT_CLOSE_HOUR = 22
# 31 days across need a wider page than the brief's 540 (the PNG is twice this in pixels).
CALENDAR_WIDTH_CSS = 960


@dataclass
class Person:
    name: str
    role: str
    cells: list[str]
    sent: int
    missed: int
    minus: int
    off: bool


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_days(start: date) -> list[date]:
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return [start + timedelta(days=i) for i in range((nxt - start).days)]


def cell(row: dict[str, Any] | None, day: date, now: datetime) -> str:
    """One square: sent / missed / open / none / future."""
    if day > now.date():
        return "future"
    if row is None:
        return "none"
    if row.get("status") == "submitted":
        return "sent"
    if day == now.date() and now.hour < REPORT_CLOSE_HOUR:
        return "open"
    return "missed"


def build(start: date, now: datetime, employees: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The template's values: one row per active employee (not the Director), every day of the month."""
    days = month_days(start)
    by_key = {(str(r["employee_id"]), r["report_date"]): r for r in rows}
    people = []
    for emp in sorted((e for e in employees if e.get("role") != DIRECTOR_ROLE),
                      key=lambda e: (ROLE_LABELS.get(e.get("role") or "", ""), names.person_name(e))):
        cells = [cell(by_key.get((str(emp["id"]), d)), d, now) for d in days]
        mine = [by_key[(str(emp["id"]), d)] for d in days if (str(emp["id"]), d) in by_key]
        people.append(Person(
            name=names.person_name(emp), role=ROLE_LABELS.get(emp.get("role") or "", emp.get("role") or ""),
            cells=cells, sent=cells.count("sent"), missed=cells.count("missed"),
            minus=sum(int(r.get("penalty_points") or 0) for r in mine), off=bool(emp.get("reports_off")),
        ))
    asked = sum(p.sent + p.missed for p in people)
    return {
        "title": "Кунлик ҳисоботлар",
        "month": f"{MONTHS_UZ[start.month - 1].capitalize()} {start.year}",
        "when": f"{now:%d.%m.%Y %H:%M} ҳолатига",
        "days": [{"n": d.day, "wd": WEEKDAYS[d.weekday()], "weekend": d.weekday() >= 5, "today": d == now.date()}
                 for d in days],
        "people": people,
        "sent": sum(p.sent for p in people),
        "missed": sum(p.missed for p in people),
        "share": round(100 * sum(p.sent for p in people) / asked) if asked else None,
    }


def caption(view: dict[str, Any]) -> str:
    share = f", {view['share']}% ўз вақтида" if view["share"] is not None else ""
    return (f"🗓 <b>Кунлик ҳисоботлар — {view['month']}</b>\n"
            f"🟩 юборилди {view['sent']} · 🟥 юборилмади {view['missed']}{share}")


def text_fallback(view: dict[str, Any]) -> str:
    """If the picture can't be drawn: the same numbers per person."""
    lines = [caption(view), ""]
    for p in view["people"]:
        minus = f", −{p.minus} балл" if p.minus else ""
        lines.append(f"• {p.name} ({p.role}): ✅ {p.sent} · ❌ {p.missed}{minus}" + (" — ҳисобот ўчирилган" if p.off else ""))
    return "\n".join(lines)


async def picture(day: date | None = None) -> tuple[bytes | None, dict[str, Any]]:
    """The month's calendar as a PNG (None if it couldn't be drawn), and its values."""
    from integrations.org_bot import store
    from integrations.reports import render

    now = now_local()
    start = month_start(day or now.date())
    employees = await store.list_active_employees()
    rows = await store.reports_between_rows(start, month_days(start)[-1])
    view = build(start, now, employees, rows)
    try:
        return render.image("reports_calendar.html", width_css=CALENDAR_WIDTH_CSS, **view), view
    except Exception:  # noqa: BLE001 — the text goes instead
        return None, view

