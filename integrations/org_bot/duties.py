"""The duties register — who does what, how often, to which target (2026-11-02, the Boss's T-0051, T-0054, T-0055).

«Ходимларнинг вазифалари, KPI, савдо режалари ва иш жараёнларини ўрганиб … топшириқларни автоматик ва тизимлаштирилган
тарзда беришни йўлга қўйинг» and «AI ни созлаш ва ўзгартириш осон бўлсин». Week 1 of the plan to 30.11: the register
and each person's work profile; the 08:45 daily tasks from it come in week 2.

  * **Where it lives:** a Google Sheet (``DUTIES_SHEET_ID``) with two tabs — «Вазифалар» (one row per duty) and
    «Режалар» (the monthly plans). The Boss or a department head edits a row; the bot reads it every morning
    (``sync``, 07:30 in the API's round) and ``/vazifalar`` now. Columns are found by their header, so moved or
    added columns are fine. Until the sheet is set, the register is ``SEED`` — the Boss's own example of 02.11 — and
    an empty sheet gets the seed written into it once.
  * **History:** every added, changed or removed row is kept in ``duty_changes`` (what it was, what it became).
  * **Work profile:** each person's duties become ``employees.responsibilities`` — the work AI already reads it, so
    it knows each person's job; ``/vazifalar`` shows a person their own list, the Director the whole register.
  * Names are matched to the bot's people by their words (any order, Latin or Cyrillic); a name nobody in the bot
    has (the technicians, until they join) stays in the register, marked «ботда йўқ».
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from integrations.common.config import settings
from integrations.common.db import execute, fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.translit import latin_to_cyrillic
from integrations.telegram.bot import escape

AGENT = "duties"
log = setup_logging(AGENT)

COMMANDS = ("/vazifalar", "/вазифалар")
TAB, PLANS_TAB = "Вазифалар", "Режалар"
COLUMNS = ("Ходим", "Лавозим", "Вазифа", "Қачон", "Кун / сана", "Соат", "Мақсад", "Бирлик", "Далил", "Текшириш", "Фаол", "Изоҳ")
PLAN_COLUMNS = ("Йўналиш", "Ойлик режа, $", "Жавобгар", "Изоҳ")
FIELDS = ("person", "position", "title", "cadence", "day", "time", "target", "unit", "proof", "verify", "active", "note")
CADENCES = {  # what people write in «Қачон» → the code
    "ҳар куни": "daily", "har kuni": "daily", "кунлик": "daily",
    "ҳар ҳафта": "weekly", "har hafta": "weekly", "ҳафталик": "weekly",
    "ҳар ой": "monthly", "har oy": "monthly", "ойлик": "monthly",
    "муддат": "deadline", "muddat": "deadline", "гача": "deadline",
    "қоида": "rule", "qoida": "rule", "норма": "rule",
    "зарур бўлганда": "as_needed", "зарурда": "as_needed",
}
CADENCE_WORDS = {"daily": "ҳар куни", "weekly": "ҳар ҳафта", "monthly": "ҳар ой", "deadline": "муддат",
                 "rule": "қоида", "as_needed": "зарур бўлганда"}
WEEKDAYS = ("душанба", "сешанба", "чоршанба", "пайшанба", "жума", "шанба", "якшанба")
VERIFY = ("SAP", "Billz", "Verifix", "1C", "қўлда")


@dataclass
class Duty:
    person: str
    position: str
    title: str
    cadence: str          # a CADENCES code
    day: str = ""         # weekday (weekly), day of month (monthly), dd.mm.yyyy (deadline)
    time: str = ""        # HH:MM — by when in the day
    target: str = ""      # a number (2, 1350, 70000) or empty
    unit: str = ""        # та, $, %, …
    proof: str = ""       # what shows it was done
    verify: str = "қўлда"
    active: bool = True
    note: str = ""

    @property
    def key(self) -> str:
        """Stable identity of a row: the person, the duty and how often — a changed target is a change, not a new row."""
        raw = "|".join(_norm(x) for x in (self.person, self.title, self.cadence, self.day))
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def line(self) -> str:
        """One line of a profile: «ҳар куни 09:30 гача — тўлов тақвимини янгилаш (мақсад: 2 та)»."""
        when = CADENCE_WORDS.get(self.cadence, self.cadence)
        if self.day:
            when += f", {self.day}"
        if self.time:
            when += f", {self.time} гача"
        aim = f" (мақсад: {self.target}{' ' + self.unit if self.unit else ''})" if self.target else ""
        proof = f"; далил: {self.proof}" if self.proof else ""
        return f"{when} — {self.title}{aim}{proof}"


def D(person: str, position: str, cadence: str, title: str, target: str = "", unit: str = "", *, day: str = "",
      time: str = "", proof: str = "", verify: str = "қўлда", note: str = "") -> Duty:
    return Duty(person, position, title, cadence, day, time, target, unit, proof, verify, True, note)


# The Boss's example of 02.11.2026 (T-2026-11-0055), names as the bot knows them. Шоҳруҳ = Эшонов Шахзод (the owner).
SEED: list[Duty] = [
    # 1. Ғиёсиддин — Primus раҳбари
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "daily", "Техниклар маршрутини тасдиқлаш"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "daily", "1 та катта лойиҳа бўйича ишлаш", "1", "та"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "daily", "Ўлик захирадан мижозга таклиф бериш", "1", "та", proof="таклиф файли"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "daily", "Кунлик сервис сотуви", "1350", "$", verify="SAP"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "weekly", "Янги B2B таклифлар", "5", "та", proof="таклиф файллари"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "weekly", "Primus OKR баҳоси", day="душанба"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "weekly", "Абдукарим ва техниклар натижасини кўриб чиқиш"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "monthly", "Primus сотуви", "30000", "$", verify="SAP"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "monthly", "Янги йиллик сервис шартномалари", "3", "та", proof="шартнома"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "deadline", "Ўлик захирадан сотув", "70000", "$", day="24.12.2026", verify="SAP"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "deadline", "Эҳтиёт қисм сотуви (жавобгар)", "20000", "$", day="24.12.2026", verify="SAP"),
    D("Ғиёсиддин Махкамов", "Primus раҳбари", "rule", "Техникларга топшириқ фақат реестр орқали берилади"),
    # 2. Абдукарим — Primus B2B
    D("Абдукарим Зунунов", "Primus B2B", "daily", "Тижорий таклиф юбориш", "2", "та", proof="таклиф файли"),
    D("Абдукарим Зунунов", "Primus B2B", "daily", "Ўлик захира рўйхатидан мижозларга қўнғироқ", "3", "та", proof="натижаси ёзилади"),
    D("Абдукарим Зунунов", "Primus B2B", "rule", "Ҳар бир лидни 1 соат ичида AmoCRM га киритиш"),
    D("Абдукарим Зунунов", "Primus B2B", "weekly", "Мижоз билан учрашув", "3", "та"),
    D("Абдукарим Зунунов", "Primus B2B", "weekly", "Янги B2B таклиф (2027 феврал–март учун)", "5", "та", proof="таклиф файллари"),
    D("Абдукарим Зунунов", "Primus B2B", "monthly", "Сотув", "30000", "$", verify="SAP"),
    # 3. Рушан, Жамолиддин — техниклар (not in the bot yet)
    *[d for name in ("Рушан", "Жамолиддин") for d in (
        D(name, "Техник", "daily", "Маршрут бўйича ташриф", "3", "та", proof="акт, сурат"),
        D(name, "Техник", "daily", "Ҳар ташрифда сервис шартномаси ва эҳтиёт қисм таклифи", proof="акт, сурат"),
        D(name, "Техник", "daily", "Ишлатилган эҳтиёт қисмни SAP га киритиш", verify="SAP"),
        D(name, "Техник", "weekly", "LONDRY машиналарига профилактика"),
        D(name, "Техник", "rule", "Носозликка 48 соат ичида етиб бориш"),
        D(name, "Техник", "rule", "Ариза рақамисиз ташриф қилинмайди"),
    )],
    # 4. Абдурашид — Garmin дўкони
    D("Абдурашид Хожиев", "Garmin дўкони", "daily", "Дўкон жамоаси билан кунлик режа", "1600", "$", verify="Billz"),
    D("Абдурашид Хожиев", "Garmin дўкони", "daily", "Эски мижозларга қўнғироқ", "10", "та", proof="натижаси ёзилади"),
    D("Абдурашид Хожиев", "Garmin дўкони", "daily", "Витрина ва нархларни текшириш"),
    D("Абдурашид Хожиев", "Garmin дўкони", "daily", "Кечки касса ҳисоботи", verify="Billz"),
    D("Абдурашид Хожиев", "Garmin дўкони", "deadline", "Эски Garmin қолдиғидан сотув (жамоа билан)", "10000", "$", day="24.12.2026", verify="SAP"),
    # 5. Ширин — Garmin дўкони ва чатлар
    D("Ширин Умматова", "Garmin дўкони ва чатлар", "daily", "Абдурашид билан кунлик режа", "1600", "$", verify="Billz"),
    D("Ширин Умматова", "Garmin дўкони ва чатлар", "daily", "Барча чатларга 15 дақиқада жавоб бериш"),
    D("Ширин Умматова", "Garmin дўкони ва чатлар", "daily", "Эски мижозларга қўнғироқ", "5", "та", proof="натижаси ёзилади"),
    D("Ширин Умматова", "Garmin дўкони ва чатлар", "weekly", "Instagram да эълон", "3", "та", proof="ҳавола"),
    # 6. Рустам — Garmin сервис ва буюртма
    D("Рустам Ёркинов", "Garmin сервис ва буюртма", "daily", "Очиқ кафолат аризаларини 48 соатда ёпиш"),
    D("Рустам Ёркинов", "Garmin сервис ва буюртма", "daily", "SAP да захирани текшириш", verify="SAP"),
    D("Рустам Ёркинов", "Garmin сервис ва буюртма", "as_needed", "Товар 2 ҳафталик захирадан кам қолса — буюртма лойиҳаси (нархни Эгаси келишади)"),
    # 7. Мурод — Garmin раҳбари
    D("Умаров Мурод", "Garmin раҳбари", "daily", "B2B таклиф", "2", "та", proof="таклиф файли"),
    D("Умаров Мурод", "Garmin раҳбари", "daily", "Дўкон жамоаси билан 10 дақиқа учрашув"),
    D("Умаров Мурод", "Garmin раҳбари", "weekly", "Мижоз билан учрашув", "2", "та"),
    D("Умаров Мурод", "Garmin раҳбари", "weekly", "Garmin OKR баҳоси", day="душанба"),
    D("Умаров Мурод", "Garmin раҳбари", "monthly", "Garmin бўлими сотуви", "60000", "$", verify="SAP"),
    # 8. Зиёда — LONDRY
    D("Мирсагатова Зиёда", "LONDRY", "daily", "Тозалик", "2", "марта", proof="сурат"),
    D("Мирсагатова Зиёда", "LONDRY", "daily", "Машиналар ҳолатини текшириш"),
    D("Мирсагатова Зиёда", "LONDRY", "daily", "Мижоз шикоятларини ёзиш"),
    D("Мирсагатова Зиёда", "LONDRY", "daily", "Янги жой ва франшиза бўйича иш"),
    D("Мирсагатова Зиёда", "LONDRY", "weekly", "Франшиза сўровлари рўйхатини Эгасига бериш"),
    D("Мирсагатова Зиёда", "LONDRY", "deadline", "QR / Payme тўловини ишга тушириш (Шоҳруҳ билан)", day="30.11.2026"),
    # 9. Зайнаб — бэк-офис
    D("Нажотова Зайнабхон", "Бэк-офис", "daily", "1C, SAP, Billz фарқларини кўриб чиқиш ва тузатиш (банк ↔ SAP ↔ 1C ↔ Billz)", time="09:00", note="09:00 да бот фарқларни юборади (/farqlar)"),
    D("Нажотова Зайнабхон", "Бэк-офис", "daily", "Эгасининг қоғоздаги топшириқларини реестрга киритиш", time="09:30"),
    D("Нажотова Зайнабхон", "Бэк-офис", "daily", "Тўлов тақвимини янгилаш", time="10:00"),
    D("Нажотова Зайнабхон", "Бэк-офис", "daily", "Эгаси учун 1 саҳифани чоп этиш", time="12:00"),
    D("Нажотова Зайнабхон", "Бэк-офис", "daily", "Didox ҳужжатларини шу куни ёпиш"),
    D("Нажотова Зайнабхон", "Бэк-офис", "weekly", "Номзодлар билан суҳбат (Garmin раҳбари ва импорт менежери ўрнига)", "3", "та"),
    # 10. Отабек — омбор
    D("Мирпулатов Отабек", "Омбор", "daily", "Позицияларни санаш", "20", "та", proof="санаш рўйхати"),
    D("Мирпулатов Отабек", "Омбор", "daily", "Кирим ва чиқимни шу куни SAP га киритиш", verify="SAP"),
    D("Мирпулатов Отабек", "Омбор", "daily", "Ўлик захира товарини суратга олиш (сотувчилар учун)", proof="сурат"),
    D("Мирпулатов Отабек", "Омбор", "rule", "FIFO / LIFO бўйича сотувчиларга вазифа бериш"),
    D("Мирпулатов Отабек", "Омбор", "rule", "Ҳар бир партия сертификациясини муддатида ўтказиш"),
    D("Мирпулатов Отабек", "Омбор", "deadline", "935 позициянинг ҳаммаси саналган, фарқ ≤ 1 %", "935", "та", day="31.12.2026"),
    # 11. Улуғбек — IT, агент
    D("Улуғбек Исоқов", "IT, агент", "daily", "Агент маълумот йиғганини текшириш", time="08:45"),
    D("Улуғбек Исоқов", "IT, агент", "daily", "«Фойдасиз» деб баҳоланган топшириқларни таҳлил қилиш"),
    D("Улуғбек Исоқов", "IT, агент", "weekly", "Агентни ҳафта натижалари бўйича тузатиш", day="жума"),
    D("Улуғбек Исоқов", "IT, агент", "deadline", "Барча ходимлар агентга уланган", day="30.11.2026"),
    # 12. Шоҳруҳ (Шахзод) — IT, улаш
    D("Эшонов Шахзод", "IT, улаш", "daily", "18:15 даги ҳисобот ўз вақтида чиққанини текшириш", time="18:15"),
    D("Эшонов Шахзод", "IT, улаш", "deadline", "SAP B1, AmoCRM ва Telegram агентга уланган", day="30.11.2026"),
    D("Эшонов Шахзод", "IT, улаш", "deadline", "LONDRY да QR / Payme тўлови ишлайди", day="30.11.2026"),
]
SEED_PLANS: list[tuple[str, str, str, str]] = [  # (direction, $ a month, owner, note)
    ("Garmin", "70000", "Умаров Мурод", ""),
    ("Primus", "45000", "Ғиёсиддин Махкамов", ""),
    ("Сервис шартномалари (йиллик)", "15000", "Ғиёсиддин Махкамов", "ойига"),
    ("Эҳтиёт қисм", "10000", "Ғиёсиддин Махкамов", ""),
    ("Химикат", "10000", "", ""),
    ("Ўлик захира ва туриб қолган товарлар (скидка билан)", "15000–20000", "", ""),
]


# ---------------------------------------------------------------------------------------------------- the sheet


def _norm(text: Any) -> str:
    text = latin_to_cyrillic(str(text or "")) if re.search(r"[A-Za-z]", str(text or "")) else str(text or "")
    text = re.sub(r"[\"'ʼ‘’`«».,()]+", " ", text.lower().replace("ё", "е"))
    return " ".join(text.split())


def cadence_of(text: str) -> str | None:
    t = _norm(text)
    for word, code in CADENCES.items():
        if _norm(word) in t:
            return code
    return None


def seed_rows() -> list[list[str]]:
    """The seed as sheet rows, in COLUMNS order."""
    return [[d.person, d.position, d.title, CADENCE_WORDS[d.cadence], d.day, d.time, d.target, d.unit, d.proof, d.verify,
             "ҳа", d.note] for d in SEED]


def parse(rows: list[list[str]]) -> tuple[list[Duty], list[str]]:
    """The «Вазифалар» tab (header first) → duties, and what's wrong with any row («5-қатор: «Қачон» тушунарсиз»)."""
    if not rows:
        return [], []
    header = [_norm(h) for h in rows[0]]
    where = {f: header.index(_norm(c)) for f, c in zip(FIELDS, COLUMNS) if _norm(c) in header}
    problems = [f"«{c}» устуни йўқ" for f, c in zip(FIELDS, COLUMNS) if f in ("person", "title", "cadence") and f not in where]
    if problems:
        return [], problems
    out: list[Duty] = []
    for n, row in enumerate(rows[1:], start=2):
        cell = {f: (row[i].strip() if i < len(row) and row[i] else "") for f, i in where.items()}
        if not any(cell.values()):
            continue
        if not cell.get("person") or not cell.get("title"):
            problems.append(f"{n}-қатор: ходим ёки вазифа ёзилмаган")
            continue
        cadence = cadence_of(cell.get("cadence", ""))
        if cadence is None:
            problems.append(f"{n}-қатор: «Қачон» тушунарсиз ({cell.get('cadence') or 'бўш'})")
            continue
        day = cell.get("day", "")
        if cadence == "deadline" and not re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", day):
            problems.append(f"{n}-қатор: муддат санаси кк.оо.йййй бўлиши керак ({day or 'бўш'})")
        if cadence == "weekly" and day and _norm(day) not in WEEKDAYS:
            problems.append(f"{n}-қатор: ҳафта куни тушунарсиз ({day})")
        target = cell.get("target", "").replace(" ", "").replace(",", ".")
        if target and not re.fullmatch(r"\d+(\.\d+)?([–-]\d+(\.\d+)?)?", target):
            problems.append(f"{n}-қатор: мақсад рақам бўлиши керак ({cell.get('target')})")
        out.append(Duty(cell["person"], cell.get("position", ""), cell["title"], cadence, day, cell.get("time", ""),
                        cell.get("target", ""), cell.get("unit", ""), cell.get("proof", ""), cell.get("verify") or "қўлда",
                        _norm(cell.get("active", "ҳа")) not in ("йўқ", "йук", "yo q", "yoq", "0", "нет", "no"), cell.get("note", "")))
    return out, problems


def match_person(name: str, people: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bot's person for a register name: every word of the name in theirs (any order); one match only."""
    words = set(_norm(name).split())
    if not words:
        return None
    found = [p for p in people if words <= set(_norm(f"{p.get('full_name') or ''} {p.get('display_name') or ''}").split())]
    return found[0] if len(found) == 1 else None


def profile_text(duties: list[Duty]) -> str:
    """A person's work profile — what the work AI reads (``employees.responsibilities``)."""
    if not duties:
        return ""
    lines = [f"Лавозими: {duties[0].position}" if duties[0].position else "", "Вазифалари (реестрдан):"]
    order = {c: i for i, c in enumerate(CADENCE_WORDS)}
    for d in sorted(duties, key=lambda d: order.get(d.cadence, 9)):
        lines.append("- " + d.line())
    return "\n".join(x for x in lines if x)


# ---------------------------------------------------------------------------------------------------- sync


async def read_sheet(run_id: uuid.UUID | None) -> tuple[list[list[str]] | None, list[list[str]] | None]:
    """The two tabs' rows, writing the seed into an empty sheet first; (None, None) when no sheet is set."""
    if not settings.duties_sheet_id.strip():
        return None, None
    from integrations.google.sheets_client import SheetsClient

    async with SheetsClient(agent=AGENT, run_id=run_id, spreadsheet_id=settings.duties_sheet_id.strip()) as sheets:
        tabs = await sheets.tab_titles()
        for tab, header, seed in ((TAB, COLUMNS, seed_rows()), (PLANS_TAB, PLAN_COLUMNS, [list(p) for p in SEED_PLANS])):
            if tab not in tabs:
                await sheets.add_tab(tab)
            if not await sheets.get_values(f"{tab}!A1:A2"):
                await sheets.append_rows(f"{tab}!A1", [list(header), *seed])
                log.info("Duties sheet: {} written with the seed ({} rows)", tab, len(seed))
        return await sheets.get_values(f"{TAB}!A:Z"), await sheets.get_values(f"{PLANS_TAB}!A:Z")


async def sync(run_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Read the register (the sheet, or the seed), keep it with its history, refresh everyone's work profile."""
    rows, plan_rows = await read_sheet(run_id)
    if rows is None:
        duties, problems, source = list(SEED), [], "seed"
    else:
        duties, problems = parse(rows)
        source = "sheet"
    people = await fetch_all("SELECT * FROM employees WHERE status = 'active'")
    old = {r["row_key"]: r for r in await fetch_all("SELECT * FROM duties WHERE active")}
    seen: set[str] = set()
    added = changed = 0
    for d in duties:
        if not d.active or d.key in seen:
            continue
        seen.add(d.key)
        person = match_person(d.person, people)
        now = {k: v for k, v in asdict(d).items() if k != "active"}
        before = old.get(d.key)
        if before is None:
            added += 1
        elif any(str(before.get(k) or "") != str(v or "") for k, v in now.items()):
            changed += 1
        else:
            continue
        await execute(
            """INSERT INTO duties (row_key, person, position, title, cadence, day, time, target, unit, proof, verify, note,
                                   employee_id, active, updated_at)
               VALUES (%(key)s, %(person)s, %(position)s, %(title)s, %(cadence)s, %(day)s, %(time)s, %(target)s, %(unit)s,
                       %(proof)s, %(verify)s, %(note)s, %(emp)s, TRUE, now())
               ON CONFLICT (row_key) DO UPDATE SET person = EXCLUDED.person, position = EXCLUDED.position,
                   title = EXCLUDED.title, cadence = EXCLUDED.cadence, day = EXCLUDED.day, time = EXCLUDED.time,
                   target = EXCLUDED.target, unit = EXCLUDED.unit, proof = EXCLUDED.proof, verify = EXCLUDED.verify,
                   note = EXCLUDED.note, employee_id = EXCLUDED.employee_id, active = TRUE, updated_at = now()""",
            {**now, "key": d.key, "emp": person["id"] if person else None})
        await execute("INSERT INTO duty_changes (row_key, change, before, after) VALUES (%s, %s, %s, %s)",
                      (d.key, "added" if before is None else "changed",
                       json.dumps({k: str(before.get(k) or "") for k in now}, ensure_ascii=False) if before else None,
                       json.dumps(now, ensure_ascii=False)))
    removed = [k for k in old if k not in seen]
    for key in removed:
        await execute("UPDATE duties SET active = FALSE, updated_at = now() WHERE row_key = %s", (key,))
        await execute("INSERT INTO duty_changes (row_key, change, before) VALUES (%s, 'removed', %s)",
                      (key, json.dumps({k: str(old[key].get(k) or "") for k in FIELDS if k != "active"}, ensure_ascii=False)))
    # Refresh links to people who joined since, then the profiles.
    for r in await fetch_all("SELECT row_key, person FROM duties WHERE active AND employee_id IS NULL"):
        person = match_person(r["person"], people)
        if person:
            await execute("UPDATE duties SET employee_id = %s WHERE row_key = %s", (person["id"], r["row_key"]))
    profiles = 0
    for person in people:
        mine = [Duty(**{k: r[k] for k in FIELDS if k != "active"}) for r in await fetch_all(
            "SELECT * FROM duties WHERE active AND employee_id = %s ORDER BY id", (person["id"],))]
        if mine:
            text = profile_text(mine)
            if (person.get("responsibilities") or "") != text:
                await execute("UPDATE employees SET responsibilities = %s WHERE id = %s", (text, person["id"]))
            profiles += 1
    if plan_rows:
        await save_plans(plan_rows)
    elif rows is None:
        await save_plans([list(PLAN_COLUMNS), *[list(p) for p in SEED_PLANS]])
    unmatched = sorted({d.person for d in duties if d.active and match_person(d.person, people) is None})
    counts = {"source": source, "duties": len(seen), "added": added, "changed": changed, "removed": len(removed),
              "profiles": profiles, "unmatched": len(unmatched), "problems": len(problems)}
    await log_action(agent=AGENT, action="sync", target_system="postgres", status="success" if not problems else "partial",
                     run_id=run_id, mode="write", payload=counts)
    return {**counts, "problem_list": problems, "unmatched_list": unmatched}


async def save_plans(rows: list[list[str]]) -> None:
    header = [_norm(h) for h in rows[0]]
    at = {c: header.index(_norm(c)) for c in PLAN_COLUMNS if _norm(c) in header}
    if PLAN_COLUMNS[0] not in at:
        return
    keep = []
    for row in rows[1:]:
        def cell(c: str) -> str:
            i = at.get(c)
            return row[i].strip() if i is not None and i < len(row) and row[i] else ""
        if cell(PLAN_COLUMNS[0]):
            keep.append((cell(PLAN_COLUMNS[0]), cell(PLAN_COLUMNS[1]), cell(PLAN_COLUMNS[2]), cell(PLAN_COLUMNS[3])))
    await execute("DELETE FROM monthly_plans")
    for direction, amount, owner, note in keep:
        await execute("INSERT INTO monthly_plans (direction, amount_usd, owner, note) VALUES (%s, %s, %s, %s)",
                      (direction, amount, owner, note))


# ---------------------------------------------------------------------------------------------------- views


def sheet_url() -> str:
    sid = settings.duties_sheet_id.strip()
    return f"https://docs.google.com/spreadsheets/d/{sid}" if sid else ""


async def own_view(employee: dict[str, Any]) -> str:
    """/vazifalar for an employee: their own register lines."""
    rows = await fetch_all("SELECT * FROM duties WHERE active AND employee_id = %s ORDER BY id", (employee["id"],))
    if not rows:
        return "📋 Сизнинг вазифаларингиз реестрга ҳали киритилмаган."
    duties = [Duty(**{k: r[k] for k in FIELDS if k != "active"}) for r in rows]
    return "📋 <b>Вазифаларингиз</b> (реестрдан)\n\n" + escape(profile_text(duties))


async def director_view() -> str:
    """/vazifalar for the Director: who has how many duties, who isn't in the bot, the plans, the sheet's link."""
    rows = await fetch_all("SELECT person, employee_id, cadence FROM duties WHERE active")
    plans = await fetch_all("SELECT * FROM monthly_plans ORDER BY direction")
    changes = await fetch_one("SELECT count(*) AS n FROM duty_changes WHERE at > now() - interval '7 days'")
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["person"], []).append(r)
    lines = [f"📋 <b>Вазифалар реестри</b> — {len(rows)} та вазифа, {len(by)} киши"]
    for person, mine in sorted(by.items()):
        daily = sum(1 for r in mine if r["cadence"] == "daily")
        tag = "" if mine[0]["employee_id"] else " · <i>ботда йўқ</i>"
        lines.append(f"• {escape(person)} — {len(mine)} та (кунлик {daily}){tag}")
    if plans:
        lines += ["", "<b>Ойлик режалар, $</b>"] + [f"• {escape(p['direction'])} — {escape(p['amount_usd'])}" for p in plans]
    lines.append(f"\nСўнгги 7 кунда ўзгариш: {changes['n'] if changes else 0} та")
    url = sheet_url()
    lines.append(f'<a href="{url}">Реестрни очиш ва ўзгартириш</a>' if url else
                 "<i>Реестр жадвали ҳали уланмаган — ҳозир Эгасининг 02.11 даги намунаси ишлатилмоқда.</i>")
    return "\n".join(lines)


def admin_text(result: dict[str, Any]) -> str:
    """Admin Bot /vazifalar: the sync's counts and the rows to fix — no targets or sums (IT sees no company figures)."""
    from integrations.google.sheets_client import service_account_email

    src = "Google жадвал" if result["source"] == "sheet" else "намуна (жадвал уланмаган)"
    lines = [f"📋 <b>Вазифалар реестри</b> — манба: {src}",
             f"Вазифалар: {result['duties']} · қўшилди {result['added']}, ўзгарди {result['changed']}, олиб ташланди {result['removed']}",
             f"Профиллар янгиланди: {result['profiles']} киши"]
    if result["unmatched_list"]:
        lines.append("Ботда йўқ: " + ", ".join(escape(n) for n in result["unmatched_list"]))
    if result["problem_list"]:
        lines += ["", "<b>Тузатиш керак:</b>"] + [f"• {escape(p)}" for p in result["problem_list"][:20]]
    if result["source"] != "sheet":
        email = service_account_email()
        lines += ["", "Улаш: бўш Google жадвал очинг, уни "
                  + (f"<code>{escape(email)}</code>" if email else "сервис аккаунтга")
                  + " билан «Редактор» қилиб улашинг ва жадвал ID сини Render'да <code>DUTIES_SHEET_ID</code> га ёзинг — "
                  "бот намунани ўзи ёзади."]
    return "\n".join(lines)


async def due(today: date) -> bool:
    """Once a day, from 07:30 (before the 08:45 tasks of week 2)."""
    row = await fetch_one("SELECT max(at) AS at FROM duty_changes WHERE change = 'synced'")
    from integrations.common.timeutil import to_local

    return row is None or row["at"] is None or to_local(row["at"]).date() < today


async def tick(now: Any) -> dict[str, Any] | None:
    """The API's 5-minute round: the morning sync, from 07:30, once a day."""
    if (now.hour, now.minute) < (7, 30) or not await due(now.date()):
        return None
    result = await sync(uuid.uuid4())
    await execute("INSERT INTO duty_changes (row_key, change) VALUES ('-', 'synced')")
    return result
