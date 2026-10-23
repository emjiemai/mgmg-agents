"""Excused days — a reason to not come, set by the Director or the Garmin manager (2026-10-23, the Boss).

«Тушунтириш хати … Причина: … День Рождения. Я заранее отпросилась» — set her grey, and give the
Boss and the Garmin manager a way to do it whenever someone has a reason. Two ways:

  * the explanation card (explanations.py) carries **⬜ Сабабли** — every day the card is about is
    excused at once; ↩ takes it back;
  * **/sababli** — pick a person (the Director: everyone; the Garmin manager: his team), then tap days
    (two weeks back, two weeks ahead): a tapped day turns grey, tapped again it comes back.

An excused day: Verifix's absent / late becomes «excused» (``attendance.excuse`` — the brief, KPI,
PULSE and the morning «why absent?» ask all read it there); an unsent report ask that day is taken out
with its minus point (the calendar shows grey; ↩ puts the ask back as it was); no report is asked
on an excused day. When the Garmin manager sets or clears one, the Director is told.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local
from integrations.org_bot import names, store
from integrations.org_bot.kpi import NO_REPORT_ROLES
from integrations.org_bot.report_calendar import TEAM_OF, WEEKDAYS
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS
from integrations.telegram.bot import TelegramBot, escape

AGENT = "ops-manager-bot"
log = setup_logging(AGENT)

COMMANDS = ("/sababli", "/сабабли")
PREFIXES = ("exc", "exu", "exp", "exd", "exl")
PAST_DAYS = 14  # how far back a day can be excused (today included)
AHEAD_DAYS = 14  # and ahead (a day off asked for in advance)


def team_of(employee: dict[str, Any]) -> tuple[str, ...] | None:
    """None = everyone (the Director); the manager's team roles; () = may not excuse."""
    if employee.get("role") == DIRECTOR_ROLE:
        return None
    return TEAM_OF.get(employee.get("role") or "", ())


def may_excuse(viewer: dict[str, Any], person: dict[str, Any] | None) -> bool:
    team = team_of(viewer)
    if team == () or person is None or person.get("role") in NO_REPORT_ROLES:
        return False
    return team is None or person.get("role") in team


def card_keyboard(explanation_id: str, excused: bool) -> dict[str, Any]:
    """The explanation card's button: excuse the days it is about, or take that back."""
    if excused:
        return {"inline_keyboard": [[{"text": "↩️ Сабаблини бекор қилиш", "callback_data": f"exu:{explanation_id}"}]]}
    return {"inline_keyboard": [[{"text": "⬜ Сабабли (кулранг қилиш)", "callback_data": f"exc:{explanation_id}"}]]}


def people_view(people: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    rows, row = [], []
    for e in sorted(people, key=lambda e: names.person_name(e)):
        row.append({"text": names.person_name(e), "callback_data": f"exp:{e['id']}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    text = ("⬜ <b>Сабабли кунлар</b>\nКимнинг куни? Сабаби бор кун кулранг бўлади: келмади, кечикди ва ҳисобот "
            "йўқ деб ҳисобланмайди." if rows else "⬜ <b>Сабабли кунлар</b>\nХодим йўқ.")
    return text, {"inline_keyboard": rows}


def window(today: date) -> list[date]:
    return [today + timedelta(days=i) for i in range(-(PAST_DAYS - 1), AHEAD_DAYS + 1)]


def days_view(person: dict[str, Any], excused: set[date], today: date) -> tuple[str, dict[str, Any]]:
    """The person's days as buttons, four to a row; an excused one starts with ⬜."""
    rows, row = [], []
    for d in window(today):
        mark = "⬜ " if d in excused else ("• " if d == today else "")
        row.append({"text": f"{mark}{WEEKDAYS[d.weekday()]} {d:%d.%m}", "callback_data": f"exd:{person['id']}:{d:%Y%m%d}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "◀️ Ходимлар", "callback_data": "exl:list"}, {"text": "✅ Тайёр", "callback_data": "exl:done"}])
    role = ROLE_LABELS.get(person.get("role") or "", person.get("role") or "")
    shown = ", ".join(f"{d:%d.%m}" for d in sorted(excused)) or "йўқ"
    text = (f"⬜ <b>Сабабли кунлар</b> — {escape(names.person_name(person))} ({escape(role)})\n"
            f"Сабабли: {shown}\nКунни босинг — кулранг бўлади; яна босинг — қайтади.")
    return text, {"inline_keyboard": rows}


# ---------------------------------------------------------------------------------------------------- sending

def _bot(run_id: uuid.UUID | None) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def _answer(callback: dict[str, Any], text: str) -> None:
    async with _bot(None) as bot:
        await bot._answer_callback(callback.get("id", ""), text)  # noqa: SLF001


async def _edit(callback: dict[str, Any], text: str, keyboard: dict[str, Any] | None, run_id: uuid.UUID) -> None:
    message = callback.get("message") or {}
    async with _bot(run_id) as bot:
        await bot._edit_message(  # noqa: SLF001 — same helper the other flows use
            chat_id=str((message.get("chat") or {}).get("id")), message_id=message.get("message_id"),
            text=text, reply_markup=keyboard or {"inline_keyboard": []})


async def _swap_buttons(callback: dict[str, Any], keyboard: dict[str, Any], run_id: uuid.UUID) -> None:
    """Only the card's button changes; its text (or a file's caption) stays."""
    message = callback.get("message") or {}
    async with _bot(run_id) as bot:
        await bot._call("editMessageReplyMarkup", {  # noqa: SLF001
            "chat_id": str((message.get("chat") or {}).get("id")), "message_id": message.get("message_id"),
            "reply_markup": keyboard}, mode="write", target_ref=str((message.get("chat") or {}).get("id")))


async def _tell_director(setter: dict[str, Any], person: dict[str, Any], days: list[date], on: bool,
                         run_id: uuid.UUID) -> None:
    """The Garmin manager excused (or un-excused) someone: the Director is told."""
    if setter.get("role") == DIRECTOR_ROLE or not days:
        return
    what = "сабабли деб белгилади" if on else "сабаблини бекор қилди"
    text = (f"⬜ {escape(names.person_name(setter))}: {escape(names.person_name(person))} — "
            f"{', '.join(f'{d:%d.%m}' for d in sorted(days))} {what}")
    async with _bot(run_id) as bot:
        for boss in await store.active_employees_by_role(DIRECTOR_ROLE):
            try:
                await bot.send_message(text, chat_id=str(boss["telegram_user_id"]))
            except Exception as exc:  # noqa: BLE001
                log.error("Director not told of an excused day: {}", type(exc).__name__)


def _forget_pulse() -> None:
    """PULSE keeps Verifix and its views a few minutes — an excused day shows at once instead."""
    try:
        from integrations.pulse import data

        data._cache.clear()  # noqa: SLF001
        data._tape_cache.clear()  # noqa: SLF001
        data._sources.clear()  # noqa: SLF001
    except Exception:  # noqa: BLE001 — at worst it shows within ten minutes
        pass


async def _people(viewer: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in await store.list_active_employees() if may_excuse(viewer, e)]


# ---------------------------------------------------------------------------------------------------- handling

async def handle_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """/sababli for the Director and the Garmin manager; None for anything else."""
    command = (message.get("text") or "").strip().split(maxsplit=1)[0:1]
    if not command or command[0].lower().split("@")[0] not in COMMANDS or team_of(employee) == ():
        return None
    text, keyboard = people_view(await _people(employee))
    async with _bot(run_id) as bot:
        await bot.send_message(text, chat_id=str(employee["telegram_user_id"]), reply_markup=keyboard)
    return "excused_people_shown"


async def handle_callback(prefix: str, rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """The card's ⬜ / ↩ and /sababli's buttons; None when the button isn't this flow's."""
    if prefix not in PREFIXES:
        return None
    clicker_id = (callback.get("from") or {}).get("id")
    viewer = await store.get_employee_by_telegram_id(clicker_id) if clicker_id else None
    if viewer is None or viewer.get("status") != "active" or team_of(viewer) == ():
        await _answer(callback, "Рухсат йўқ")
        return "excused_unauthorized"

    if prefix in ("exc", "exu"):
        row = await store.get_explanation(rest)
        person = await store.get_employee(str(row["employee_id"])) if row else None
        if row is None or not may_excuse(viewer, person):
            await _answer(callback, "Рухсат йўқ")
            return "excused_unauthorized"
        days = list(row["days"] or [])
        changed = []
        for d in days:
            done = (await store.excuse_day(str(person["id"]), d, clicker_id, row.get("answer")) if prefix == "exc"
                    else await store.unexcuse_day(str(person["id"]), d))
            if done:
                changed.append(d)
        _forget_pulse()
        await _swap_buttons(callback, card_keyboard(rest, prefix == "exc"), run_id)
        await _answer(callback, "⬜ Кулранг қилинди" if prefix == "exc" else "↩️ Қайтарилди")
        await _tell_director(viewer, person, changed, prefix == "exc", run_id)
        await log_action(agent=AGENT, action="excused_set" if prefix == "exc" else "excused_cleared",
                         target_system="postgres", status="success", run_id=run_id, mode="write",
                         payload={"days": len(changed), "from": "card"})
        return "excused_set" if prefix == "exc" else "excused_cleared"

    if prefix == "exl":
        if rest == "done":
            message = callback.get("message") or {}
            await _edit(callback, (message.get("text") or "⬜ Сабабли кунлар").split("\n")[0] + "\n✅ Сақланди", None, run_id)
            await _answer(callback, "✅")
            return "excused_done"
        text, keyboard = people_view(await _people(viewer))
        await _edit(callback, text, keyboard, run_id)
        await _answer(callback, "")
        return "excused_people_shown"

    if prefix == "exp":
        person = await store.get_employee(rest)
        if not may_excuse(viewer, person):
            await _answer(callback, "Рухсат йўқ")
            return "excused_unauthorized"
        today = today_local()
        excused = await store.excused_days_of(rest, window(today)[0], window(today)[-1])
        text, keyboard = days_view(person, excused, today)
        await _edit(callback, text, keyboard, run_id)
        await _answer(callback, "")
        return "excused_days_shown"

    # exd:<employee id>:<YYYYMMDD> — one day on or off
    try:
        person_id, raw = rest.rsplit(":", 1)
        day = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        await _answer(callback, "Номаълум амал")
        return "excused_bad_day"
    person = await store.get_employee(person_id)
    today = today_local()
    if not may_excuse(viewer, person) or day not in window(today):
        await _answer(callback, "Рухсат йўқ")
        return "excused_unauthorized"
    on = await store.excuse_day(person_id, day, clicker_id, "раҳбар белгилади")
    if not on:
        await store.unexcuse_day(person_id, day)
    _forget_pulse()
    excused = await store.excused_days_of(person_id, window(today)[0], window(today)[-1])
    text, keyboard = days_view(person, excused, today)
    await _edit(callback, text, keyboard, run_id)
    await _answer(callback, f"⬜ {day:%d.%m}" if on else f"↩️ {day:%d.%m}")
    await _tell_director(viewer, person, [day], on, run_id)
    await log_action(agent=AGENT, action="excused_set" if on else "excused_cleared", target_system="postgres",
                     status="success", run_id=run_id, mode="write", payload={"days": 1, "from": "sababli"})
    return "excused_set" if on else "excused_cleared"
