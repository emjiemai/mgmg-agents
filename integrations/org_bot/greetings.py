"""Holiday and birthday greetings (2026-11-18, the Boss's task T-2026-11-0052: «AI операцион менежер ҳаммани байрамлар ва
туғилган кун билан табриклашни бошласин, бунинг учун маълумотлар олдиндан киргазилсин»).

The data is entered in advance in the duties register's Google Sheet (``DUTIES_SHEET_ID``), two tabs the bot creates:

  * «Байрамлар» — Сана | Номи | Кимга | Табрик. Сана «01.09» = every year, «20.03.2026» = that year only (Рамазон /
    Қурбон ҳайит move every year — the Boss adds them). Кимга: ҳамма / эркаклар / аёллар (by the admin's ака / опа —
    never guessed; nobody set = not sent a men's / women's greeting). Табрик: own text, or empty for the house one.
    Written first with Uzbekistan's fixed public holidays — the Boss edits, adds or deletes rows.
  * «Туғилган кунлар» — Исм | Сана («15.04» or «15.04.1990»), matched to an active employee by name.

Every morning (agents/greetings, after the brief): each person whose day it is gets ONE polite message (friendly
voice: one sentence, no emoji, their name with ака / опа); on a birthday the Director is told too, so he can greet.
Once per person, day and occasion (``greetings_sent``). Counts only in logs.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging

AGENT = "greetings"
log = setup_logging(AGENT)

HOLIDAYS_TAB = "Байрамлар"
BIRTHDAYS_TAB = "Туғилган кунлар"
HOLIDAY_COLUMNS = ("Сана", "Номи", "Кимга", "Табрик")
BIRTHDAY_COLUMNS = ("Исм", "Сана")
# Uzbekistan's fixed public holidays (the moving Ҳайит days are added in the sheet each year).
SEED_HOLIDAYS = (
    ("01.01", "Янги йил", "ҳамма", ""),
    ("14.01", "Ватан ҳимоячилари куни", "эркаклар", ""),
    ("08.03", "Халқаро хотин-қизлар куни", "аёллар", ""),
    ("21.03", "Наврўз байрами", "ҳамма", ""),
    ("09.05", "Хотира ва қадрлаш куни", "ҳамма", ""),
    ("01.09", "Мустақиллик куни", "ҳамма", ""),
    ("01.10", "Ўқитувчи ва мураббийлар куни", "ҳамма", ""),
    ("08.12", "Конституция куни", "ҳамма", ""),
)
WHO = {"ҳамма": "all", "хамма": "all", "all": "all", "эркаклар": "aka", "эркак": "aka", "аёллар": "opa", "аёл": "opa"}
_DATE = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?\s*$")


@dataclass(frozen=True)
class Occasion:
    kind: str          # holiday | birthday
    key: str           # the holiday's name / "birthday"
    text: str          # the greeting for this person


def parse_day(value: str) -> tuple[int, int, int | None] | None:
    """«01.09» → (1, 9, None) every year; «20.03.2026» → (20, 3, 2026) that year; anything else → None."""
    m = _DATE.match(str(value or ""))
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return day, month, year


def falls_on(value: str, today: date, yearly_only_date: bool = False) -> bool:
    """Whether a sheet date is today (29.02 is greeted on 28.02 in a year without it)."""
    parsed = parse_day(value)
    if parsed is None:
        return False
    day, month, year = parsed
    if year is not None and not yearly_only_date and year != today.year:
        return False
    if (day, month) == (29, 2) and not _leap(today.year):
        day = 28
    return (day, month) == (today.day, today.month)


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _header_index(rows: list[list[str]], columns: Iterable[str]) -> tuple[dict[str, int], list[list[str]]]:
    if not rows:
        return {}, []
    head = [str(h).strip().lower() for h in rows[0]]
    return {c: head.index(c.lower()) for c in columns if c.lower() in head}, rows[1:]


def _cell(row: list[str], index: dict[str, int], column: str) -> str:
    i = index.get(column)
    return str(row[i]).strip() if i is not None and i < len(row) else ""


def _words(text: str | None) -> set[str]:
    from integrations.org_bot.explanations import _key

    return set(_key(text))


def match(name: str, employees: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one active employee this sheet name means: every word of the shorter name inside the other
    («Ширин Умматова» ↔ «Умматова Ширин»; «Ширин» alone only if one employee is called that). None if none or several."""
    sheet = _words(name)
    if not sheet:
        return None
    fits = [e for e in employees
            if (mine := _words(e.get("full_name")) or _words(e.get("display_name")))
            and (sheet <= mine or mine <= sheet)]
    return fits[0] if len(fits) == 1 else None


def holiday_text(name: str, holiday: str, own: str = "") -> str:
    from integrations.org_bot.tone import casual
    from integrations.telegram.bot import escape

    if own.strip():
        return f"{name}, {escape(own.strip())}"
    return casual(f"{name}, {escape(holiday)} муборак бўлсин, сизга ва оилангизга соғлик, тинчлик ва бахт тилаймиз, "
                  "ЭМЖИЕМ жамоаси", keep=[name, escape(holiday), "ЭМЖИЕМ"])


def birthday_text(name: str) -> str:
    from integrations.org_bot.tone import casual

    return casual(f"{name}, туғилган кунингиз муборак бўлсин, сизга мустаҳкам соғлик, оилавий бахт ва ишларингизда "
                  "омад тилаймиз, ЭМЖИЕМ жамоаси", keep=[name, "ЭМЖИЕМ"])


def occasions(today: date, holidays: list[list[str]], birthdays: list[list[str]],
              employees: list[dict[str, Any]]) -> tuple[dict[str, list[Occasion]], list[dict[str, Any]], int]:
    """Who gets what today: {employee id: [occasions]}, the birthday people (for the Director), unmatched names."""
    from integrations.org_bot import names
    from integrations.org_bot.roles import DIRECTOR_ROLE
    from integrations.telegram.bot import escape

    staff = [e for e in employees if e.get("status", "active") == "active" and e.get("role") != DIRECTOR_ROLE]
    out: dict[str, list[Occasion]] = {}
    index, rows = _header_index(holidays, HOLIDAY_COLUMNS)
    for row in rows:
        if not falls_on(_cell(row, index, "Сана"), today):
            continue
        holiday = _cell(row, index, "Номи")
        who = WHO.get(_cell(row, index, "Кимга").lower(), "all")
        if not holiday:
            continue
        for e in staff:
            if who != "all" and (e.get("address_form") or "") != who:
                continue
            out.setdefault(str(e["id"]), []).append(
                Occasion("holiday", holiday, holiday_text(escape(names.call_name(e)), holiday, _cell(row, index, "Табрик"))))
    born: list[dict[str, Any]] = []
    unmatched = 0
    index, rows = _header_index(birthdays, BIRTHDAY_COLUMNS)
    for row in rows:
        name, when = _cell(row, index, "Исм"), _cell(row, index, "Сана")
        if not name or parse_day(when) is None:
            continue
        person = match(name, staff)
        if person is None:
            unmatched += 1
            continue
        if falls_on(when, today, yearly_only_date=True):
            born.append(person)
            out.setdefault(str(person["id"]), []).append(
                Occasion("birthday", "birthday", birthday_text(escape(names.call_name(person)))))
    return out, born, unmatched


async def read_tabs(run_id: uuid.UUID | None) -> tuple[list[list[str]], list[list[str]]] | None:
    """Both tabs' rows, creating them (holidays with the seed) when missing; None when no sheet is set."""
    if not settings.duties_sheet_id.strip():
        return None
    from integrations.google.sheets_client import SheetsClient

    async with SheetsClient(agent=AGENT, run_id=run_id, spreadsheet_id=settings.duties_sheet_id.strip()) as sheets:
        tabs = await sheets.tab_titles()
        for tab, header, seed in ((HOLIDAYS_TAB, HOLIDAY_COLUMNS, [list(h) for h in SEED_HOLIDAYS]),
                                  (BIRTHDAYS_TAB, BIRTHDAY_COLUMNS, [])):
            if tab not in tabs:
                await sheets.add_tab(tab)
            if not await sheets.get_values(f"{tab}!A1:A2"):
                await sheets.append_rows(f"{tab}!A1", [list(header), *seed])
        return await sheets.get_values(f"{HOLIDAYS_TAB}!A:D"), await sheets.get_values(f"{BIRTHDAYS_TAB}!A:B")


async def send_day(today: date, run_id: uuid.UUID) -> dict[str, int]:
    """Today's greetings, once per person and occasion; the Director told of birthdays. Counts only."""
    from integrations.common.db import execute, fetch_all, log_action
    from integrations.org_bot import names, store
    from integrations.org_bot.roles import DIRECTOR_ROLE
    from integrations.telegram.bot import TelegramBot, TelegramError, escape

    counts = {"sent": 0, "failed": 0, "birthdays": 0, "unmatched": 0}
    if not settings.greetings_enabled or settings.bots_frozen:
        return counts
    tabs = await read_tabs(run_id)
    if tabs is None:
        log.info("Greetings: no sheet set (DUTIES_SHEET_ID) — nothing to read")
        return counts
    employees = await store.list_active_employees()
    plan, born, counts["unmatched"] = occasions(today, tabs[0], tabs[1], employees)
    counts["birthdays"] = len(born)
    by_id = {str(e["id"]): e for e in employees}
    async with TelegramBot(agent=AGENT, run_id=run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
        for employee_id, items in plan.items():
            person = by_id.get(employee_id)
            for item in items:
                claimed = await fetch_all(
                    """INSERT INTO greetings_sent (day, employee_id, kind, key) VALUES (%s, %s, %s, %s)
                       ON CONFLICT DO NOTHING RETURNING 1 AS ok""", (today, employee_id, item.kind, item.key))
                if not claimed or person is None:
                    continue
                try:
                    await bot.send_message(item.text, chat_id=str(person["telegram_user_id"]))
                    counts["sent"] += 1
                except TelegramError as exc:
                    log.warning("Greeting not sent: {}", type(exc).__name__)
                    counts["failed"] += 1
                    await execute("DELETE FROM greetings_sent WHERE day = %s AND employee_id = %s AND kind = %s AND key = %s",
                                  (today, employee_id, item.kind, item.key))
        if born:
            people = ", ".join(escape(names.person_name(p)) for p in born)
            for boss in await store.active_employees_by_role(DIRECTOR_ROLE):
                try:
                    await bot.send_message(f"🎂 Бугун {people}нинг туғилган куни — бот уларни табриклади.",
                                           chat_id=str(boss["telegram_user_id"]))
                except TelegramError as exc:
                    log.warning("Birthday note to the Director not sent: {}", type(exc).__name__)
    await log_action(agent=AGENT, action="greetings_sent", target_system="telegram", status="success", run_id=run_id,
                     mode="notify", payload=counts)
    return counts
