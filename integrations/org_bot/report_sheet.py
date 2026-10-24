"""Admin Bot: one person's report sheet — the month's days, each green / red / grey, set by the admin (2026-10-24).

The owner: "give me access to edit reports and set grey, red or green to employees as Admin — /xodimlar and then the
report sheet, ~30 buttons (monthly)". On the /xodimlar card: **📊 Ҳисобот варағи**. A tap moves a day on:

    ⬜ grey (not asked / excused) → 🟩 sent → 🟥 not sent → ⬜ excused → …
    a day still to come: blank ↔ ⬜ excused in advance.

  * 🟩 sent — the report counts as sent, its minus is 0 (a day with no report row gets one, marked
    «[админ: юборилган деб белгиланди]»; a row's own text is kept);
  * 🟥 not sent — the minus point once the day is closed (22:00, from 16.10); today before 22:00 it is still
    owed (🟨) and closes at 22:00 as any other;
  * ⬜ excused — the same as the Director's ⬜ Сабабли (excused_days): the report isn't counted, and neither is
    Verifix's absent / late that day.

Nothing is deleted: a report's text survives every turn of the wheel (daily_reports.status 'excused'). Every change
is kept (report_cell_edits) and the Director is told once — on ✅ Тайёр, or at 22:00 for what was left open. The
employee isn't messaged. Admin Bot only (its callbacks check the admin's Telegram id).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import names
from integrations.org_bot.report_calendar import REPORT_CLOSE_HOUR, WEEKDAYS, month_days, month_start
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS
from integrations.org_bot.task_tracker import MONTHS_UZ
from integrations.telegram.bot import TelegramBot, escape

AGENT = "admin-bot"
log = setup_logging(AGENT)

ACTIONS = ("rsh", "rsm", "rsd", "rsok", "rsx")
MARKS = {"sent": "🟩", "missed": "🟥", "open": "🟨", "excused": "⬜", "none": "▫️", "future": ""}
WORDS = {"sent": "юборилди", "missed": "юборилмади", "open": "очиқ", "excused": "сабабли", "none": "сўралмаган",
         "future": "бўш", "clear": "бўш"}
ADMIN_NOTE = "[админ: юборилган деб белгиланди]"


def state(row: dict[str, Any] | None, excused: bool, day: date, now: datetime) -> str:
    """What the day shows: sent / missed / open / excused / none (not asked) / future."""
    status = (row or {}).get("status")
    if status == "submitted":
        return "sent"
    if excused or status == "excused":
        return "excused"
    if day > now.date():
        return "future"
    if row is None:
        return "none"
    if day == now.date() and now.hour < REPORT_CLOSE_HOUR:
        return "open"
    return "missed"


def next_target(current: str) -> str:
    """Where a tap takes the day: grey → sent → not sent → excused; a future day: excused ↔ blank."""
    if current == "future":
        return "excused"
    if current == "excused":
        return "sent"
    return {"sent": "missed", "missed": "excused", "open": "excused", "none": "sent"}[current]


def sheet_view(emp: dict[str, Any], month: date, rows: list[dict[str, Any]], excused: set[date],
               now: datetime) -> tuple[str, dict[str, Any]]:
    """The month as a calendar of buttons (Monday first), with its totals and the way out."""
    by_day = {r["report_date"]: r for r in rows}
    days = month_days(month_start(month))
    states = {d: state(by_day.get(d), d in excused, d, now) for d in days}
    eid = emp["id"]
    grid: list[list[dict[str, str]]] = [[{"text": w, "callback_data": "rsx:0"} for w in WEEKDAYS]]
    week = [{"text": "·", "callback_data": "rsx:0"}] * days[0].weekday()
    for d in days:
        week = week + [{"text": f"{MARKS[states[d]]}{d.day}", "callback_data": f"rsd:{eid}:{d:%Y%m%d}"}]
        if len(week) == 7:
            grid.append(week)
            week = []
    if week:
        grid.append(week + [{"text": "·", "callback_data": "rsx:0"}] * (7 - len(week)))
    nav = [{"text": "◀️", "callback_data": f"rsm:{eid}:{(days[0] - timedelta(days=1)):%Y%m}"}]
    if days[-1] < now.date():
        nav.append({"text": "▶️", "callback_data": f"rsm:{eid}:{(days[-1] + timedelta(days=1)):%Y%m}"})
    nav += [{"text": "✅ Тайёр", "callback_data": f"rsok:{eid}"}, {"text": "← Ходим", "callback_data": f"emp:{eid}"}]
    grid.append(nav)
    shown = list(states.values())
    role = ROLE_LABELS.get(emp.get("role") or "", emp.get("role") or "")
    text = (f"📊 <b>Ҳисобот варағи — {escape(names.person_name(emp))}</b> ({escape(role)})\n"
            f"{MONTHS_UZ[days[0].month - 1].capitalize()} {days[0].year}: 🟩 {shown.count('sent')} · "
            f"🟥 {shown.count('missed')} · ⬜ сабабли {shown.count('excused')}\n"
            "Кунни босинг: ⬜ → 🟩 юборилди → 🟥 юборилмади (−1 балл) → ⬜ сабабли. Келажак кун: ⬜ олдиндан сабабли.\n"
            "<i>Ўзгаришлар Директорга «✅ Тайёр» босилганда (ёки соат 22:00 да) маълум қилинади.</i>")
    return text, {"inline_keyboard": grid}


async def apply(employee: dict[str, Any], day: date, target: str, admin_id: int, now: datetime) -> None:
    """Turn the day into ``target`` (sent / missed / excused / clear)."""
    from integrations.org_bot import store
    from integrations.org_bot.report_close import PENALTY_FROM

    eid = str(employee["id"])
    if target == "excused":
        await store.excuse_day(eid, day, admin_id, "админ белгилади")
        return
    await store.unexcuse_day(eid, day)
    if target == "sent":
        await store.set_report_sent(employee, day, ADMIN_NOTE)
    elif target == "missed":
        closed = day < now.date() or now.hour >= REPORT_CLOSE_HOUR
        points = max(0, settings.report_miss_penalty) if day >= PENALTY_FROM else 0
        await store.set_report_missed(employee, day, points, closed)


def summary_text(edits: list[dict[str, Any]]) -> str | None:
    """The Director's one message: per person, each day changed from → to (several taps on a day = one line)."""
    people: dict[str, tuple[str, dict[date, list[str]]]] = {}
    for e in edits:
        name, days = people.setdefault(str(e["employee_id"]), (names.person_name(e), {}))
        first_last = days.setdefault(e["day"], [e["before"], e["after"]])
        first_last[1] = e["after"]
    lines = []
    for name, days in people.values():
        changed = [f"{d:%d.%m} {MARKS.get(b, '') or '·'}→{MARKS.get(a, '') or '·'} {WORDS.get(a, a)}"
                   for d, (b, a) in sorted(days.items()) if b != a]
        if changed:
            lines.append(f"• {escape(name)}: " + ", ".join(changed))
    if not lines:
        return None
    return ("⚙️ <b>Ҳисобот варағи ўзгартирилди (админ)</b>\n" + "\n".join(lines)
            + "\n<i>🟩 юборилган · 🟥 юборилмаган (−1 балл) · ⬜ сабабли — ҳисобот ва давомат ҳисобланмайди</i>")


async def tell_director(run_id: uuid.UUID) -> int:
    """Every change not told yet, in one message to each active Director; how many changes were told."""
    from integrations.org_bot import store

    edits = await store.untold_cell_edits()
    if not edits:
        return 0
    text = summary_text(edits)
    if text and not settings.bots_frozen:
        async with TelegramBot(agent=AGENT, run_id=run_id,
                               bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
            for boss in await store.active_employees_by_role(DIRECTOR_ROLE):
                try:
                    await bot.send_message(text, chat_id=str(boss["telegram_user_id"]))
                except Exception as exc:  # noqa: BLE001 — marked told anyway: the edits stay in report_cell_edits
                    log.error("Report sheet summary not sent: {}", type(exc).__name__)
    await store.mark_cell_edits_told([str(e["id"]) for e in edits])
    return len(edits)
