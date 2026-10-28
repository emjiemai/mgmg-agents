"""Leads to B2B sales — the lead tracker (the Boss, 2026-10-28), and the leads Google Sheet's columns.

History: the hand-out (one lead each morning, "how is it going?" at 15:00 — 2026-09-30) was stopped by the
Director on 2026-10-07. On 2026-10-28 the Boss brought it back with his own design:

  * every lead the Lead Agent finds has a **number** (``leads.id`` — «Лид №33 Hilton»); each morning every
    B2B sales person at work gets one, and the Boss gets the list of who got which («менга ҳам келсин»);
  * at 15:00 the AI follows each open lead: **contacted?** (бугун / кеча / ҳали йўқ) → **sold?** — sold: what
    and how much (typed); not sold: why (buttons); still going: asked again tomorrow;
  * a lead nobody contacted in ``NOT_CONTACTED_CLOSE_DAYS`` is closed as «алоқага чиқилмади»;
  * the Boss's statistics: given / contacted (in how many days, how many late) / sold (how much) / not sold by
    reason / the lost opportunity — on /lidlar, every Monday, in PULSE and as a PDF table;
  * **stop / start** (the Boss: «вақтинча stop / start қилиш керак») — the Director's and the admin's /lidlar.

The rules (newest lead first, an open lead asked every day, every track to B2B sales) are the owner's from
2026-09-30 / 10-02. Pure logic here (tested offline); ``lead_tracker.py`` sends and takes the answers,
``agents/lead-handout`` runs the morning. Everything a sales person sees is Uzbek Cyrillic.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, time
from typing import Any

from integrations.common.translit import latin_to_cyrillic
from integrations.org_bot.tone import is_polite
from integrations.telegram.bot import escape

AGENT = "lead-handout"
SALES_ROLE = "b2b_sotuv"

# The Lead Agent's Google Sheet (agents/lead-agent writes it; selfcheck keeps
# the two column lists identical). Sales people may edit it (2026-10-02), so
# columns are found by their header, never by position: a moved or added
# column doesn't shift anything. Reads take a wide range for that reason.
SHEET_TAB = "Sheet1"
SHEET_RANGE = f"{SHEET_TAB}!A:T"
SHEET_READ_RANGE = f"{SHEET_TAB}!A:AZ"
SHEET_COLUMNS = [
    "company_name", "project_name", "industry", "location", "project_stage",
    "estimated_opening", "signal", "signal_source_url", "signal_date",
    "estimated_size", "contact_name", "contact_role", "contact_method",
    "confidence", "priority", "recheck_date", "notes", "date_added",
    "dedupe_key", "track",
]

CHECKIN_TIME = time(15, 0)
CHECKIN_WINDOW_MINUTES = 25  # the API's 5-minute round asks between 15:00 and 15:25
ANSWER_WINDOW_MINUTES = 180  # after «Сотилди» / «Бошқа сабаб» the typed answer is taken this long
NOT_CONTACTED_CLOSE_DAYS = 7  # never contacted in a week → closed as «алоқага чиқилмади»
LATE_AFTER_DAYS = 1  # contacted the day it came or the next = on time; later = late

STATUS_LABELS = {"new": "боғланилмаган", "in_progress": "жараёнда", "dismissed": "сотилмади", "done": "сотилди"}
# Why a lead didn't sell — the buttons; the last one means "let me write it".
REASONS = ("лид эскирган", "эҳтиёжи йўқ", "бошқадан олди", "нархи тўғри келмади", "жавоб бермади", "бошқа сабаб")
AUTO_REASON = "алоқага чиқилмади"  # closed by the bot after NOT_CONTACTED_CLOSE_DAYS
PREFIX = "lq"  # the 15:00 buttons: lq:<checkin id>:<value>

STAGES = {
    "permitted": "рухсатнома олинган",
    "under construction": "қурилмоқда",
    "pre-opening hiring": "очилиш олдидан ходим ёлламоқда",
    "tender open": "тендер очиқ",
    "recently opened": "яқинда очилган",
    "sponsorship signed": "ҳомийлик имзоланган",
    "sponsorship open": "ҳомийлик очиқ",
    "early-stage cooperation": "ҳамкорлик бошланмоқда",
}
PRIORITIES = {"high": "юқори", "medium": "ўрта", "low": "паст"}
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


# ------------------------------------------------------------ the sheet


def _number(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def column_order(header: list[str]) -> list[str]:
    """The sheet's columns in their current order, read from its header row.

    When the header doesn't carry our column names (renamed or missing), the
    standard order is assumed — the sheet as the Lead Agent first wrote it.
    Extra columns people add are kept in place and simply not read.
    """
    names = [str(h).strip() for h in header or []]
    if len(set(names) & set(SHEET_COLUMNS)) >= 0.8 * len(SHEET_COLUMNS):
        return names
    return list(SHEET_COLUMNS)


def sheet_records(rows: list[list[str]]) -> list[dict[str, str]]:
    """The sheet's data rows as {column: value}, by header (``column_order``)."""
    if not rows:
        return []
    order = column_order(rows[0])
    return [
        {c: (row[i].strip() if i < len(row) and row[i] else "") for i, c in enumerate(order) if c}
        for row in rows[1:]
    ]


def row_for_sheet(values: dict[str, str], header: list[str] | None) -> list[str]:
    """One row to append, its values in the sheet's current column order."""
    return [str(values.get(c, "") or "") for c in column_order(header or [])]


def column_letter(count: int) -> str:
    """The letter of the ``count``-th column (1 → A, 27 → AA)."""
    letters = ""
    while count:
        count, rest = divmod(count - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def rows_to_leads(rows: list[list[str]]) -> list[dict[str, Any]]:
    """The sheet's rows (header first) as lead dicts ready for ``leads``; unnamed rows are skipped."""
    found = []
    for record in sheet_records(rows):
        cells = {c: record.get(c, "") for c in SHEET_COLUMNS}
        name = cells["company_name"] or cells["project_name"]
        if not name:
            continue
        found.append({
            "dedupe_key": cells["dedupe_key"] or f"{name.lower()}|{cells['track']}",
            **{c: cells[c] or None for c in (
                "company_name", "project_name", "industry", "location", "project_stage", "signal",
                "signal_source_url", "contact_name", "contact_role", "contact_method", "track",
            )},
            "priority": cells["priority"].lower() or None,
            "confidence": _number(cells["confidence"]),
            "date_added": _date(cells["date_added"]),
        })
    return found


# ------------------------------------------------------------ the hand-out


def handout_order(lead: dict[str, Any]) -> tuple:
    """Sort key: the newest lead first — "from the end" of the sheet (the owner, 2026-10-02).

    A fresh lead negotiates best and leaves the most time: by the day it was added, then by its place in the
    sheet (``leads.id`` follows the sheet's order); priority only breaks a tie.
    """
    added = lead.get("date_added")
    return (
        -(added.toordinal() if added else 0),
        -int(lead.get("id") or 0),
        _PRIORITY_RANK.get((lead.get("priority") or "").lower(), 3),
    )


def plan_handout(people: list[dict[str, Any]], free_leads: list[dict[str, Any]]) -> list[tuple[dict, dict]]:
    """Who gets which lead this morning: one each, newest leads first, until the leads run out."""
    ordered = sorted(free_leads, key=handout_order)
    people = sorted(people, key=lambda p: ((p.get("full_name") or p.get("display_name") or "").lower(), str(p["id"])))
    return list(zip(people, ordered))


def checkin_due(now_local: datetime) -> bool:
    """Whether this round is the 15:00 follow-up."""
    start = CHECKIN_TIME.hour * 60 + CHECKIN_TIME.minute
    minutes = now_local.hour * 60 + now_local.minute
    return start <= minutes < start + CHECKIN_WINDOW_MINUTES


def lead_name(lead: dict[str, Any]) -> str:
    return (lead.get("company_name") or lead.get("project_name") or lead.get("company") or "—").strip()


def label(lead: dict[str, Any]) -> str:
    """«Лид №33 Hilton» — the number the Boss and the seller both use (escaped)."""
    number = lead.get("lead_id") or lead.get("id")
    return f"№{number} {escape(lead_name(lead))}" if number else escape(lead_name(lead))


BRIEF_SYSTEM = """\
You brief a sales person at MGMG (Tashkent) on one lead the company's lead
finder found. MGMG sells industrial laundry equipment and its service
(Primus Londry), and Garmin/Tanita sports tech (sponsorships, partnerships).

Write in Uzbek, Cyrillic script, for the sales person:
- "brief": two short sentences — what is happening at this company or
  project, and what we could offer them. Company and brand names stay as
  they are. No greeting, no emoji, at most 260 characters.
- "location": the city/region in Uzbek Cyrillic (e.g. "Тошкент"), or "".

Use only the facts given; never invent a contact, a date or a number.
Always polite: the respectful "сиз" form, never "сен" or its verb forms.
Answer with JSON only: {"brief": "...", "location": "..."}
"""


def brief_prompt(lead: dict[str, Any]) -> str:
    """The lead's facts, for the AI."""
    fields = ("company_name", "project_name", "industry", "location", "project_stage", "signal", "track")
    return "\n".join(f"{f}: {lead.get(f) or ''}" for f in fields)


_CYRILLIC = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳ]")
_LETTER = re.compile(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]")


def parse_brief(raw: str) -> tuple[str, str] | None:
    """(brief, location) from the AI, or None if it isn't a short, mostly-Cyrillic, polite answer."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    brief = " ".join(str(data.get("brief") or "").split())
    location = " ".join(str(data.get("location") or "").split())
    letters = _LETTER.findall(brief)
    if not brief or len(brief) > 300 or "<" in brief or len(_CYRILLIC.findall(brief)) < 0.7 * len(letters):
        return None
    if not is_polite(brief):
        return None
    if re.search(r"[A-Za-z]", location) or len(location) > 60:
        location = ""
    return brief, location


def fallback_brief(lead: dict[str, Any]) -> str:
    """The card's summary line without the AI: the facts below it carry the rest."""
    return "Батафсил маълумот манбада."


def card_text(lead: dict[str, Any], brief: str, location: str = "") -> str:
    """The 08:00 card — work, so a card, not a chat line (tasks keep their format)."""
    place = location or latin_to_cyrillic(lead.get("location") or "")
    lines = [f"🎯 <b>Лид {label(lead)}</b>", "", escape(brief)]
    facts = [escape(place)] if place else []
    stage = STAGES.get((lead.get("project_stage") or "").strip().lower())
    if stage:
        facts.append(stage)
    priority = PRIORITIES.get((lead.get("priority") or "").lower())
    if priority:
        facts.append(f"муҳимлиги {priority}")
    if facts:
        lines += ["", "📍 " + " · ".join(facts)]
    contact = ", ".join(escape(v) for v in (lead.get("contact_name"), lead.get("contact_role"), lead.get("contact_method")) if v)
    if contact:
        lines.append(f"📞 {contact}")
    url = (lead.get("signal_source_url") or "").strip()
    if url.startswith(("http://", "https://")):
        lines.append(f'🔗 <a href="{escape(url)}">манба</a>')
    lines += ["", "<i>Соат 15:00 да боғландингизми, натижаси қандай — сўрайман.</i>"]
    return "\n".join(lines)


def boss_copy_text(given: list[tuple[str, dict[str, Any]]], day: date) -> str:
    """The Boss's copy of the morning hand-out: «№33 Hilton → Абдукарим», one line each."""
    lines = [f"🎯 <b>Бугун берилган лидлар</b> · {day:%d.%m}"]
    lines += [f"• {label(lead)} → {escape(person)}" for person, lead in given]
    lines.append("\n<i>Ҳар куни 15:00 да ҳар бир лид бўйича сўралади. Статистика: /lidlar</i>")
    return "\n".join(lines)


# ------------------------------------------------------ the 15:00 follow-up


def _days_text(days_open: int) -> str:
    return f" ({days_open}-кун)" if days_open > 1 else ""


def contact_question(lead: dict[str, Any], days_open: int) -> str:
    return f"🎯 <b>Лид {label(lead)}</b>{_days_text(days_open)}\nМижоз билан боғландингизми?"


def contact_keyboard(checkin_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "✅ Ҳа, бугун", "callback_data": f"{PREFIX}:{checkin_id}:t"},
         {"text": "✅ Ҳа, кеча", "callback_data": f"{PREFIX}:{checkin_id}:y"}],
        [{"text": "❌ Ҳали йўқ", "callback_data": f"{PREFIX}:{checkin_id}:n"}],
    ]}


def result_question(lead: dict[str, Any], days_open: int) -> str:
    return f"🎯 <b>Лид {label(lead)}</b>{_days_text(days_open)}\nНатижаси қандай?"


def result_keyboard(checkin_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "💰 Сотилди", "callback_data": f"{PREFIX}:{checkin_id}:s"},
         {"text": "❌ Сотилмади", "callback_data": f"{PREFIX}:{checkin_id}:x"}],
        [{"text": "⏳ Жараёнда", "callback_data": f"{PREFIX}:{checkin_id}:p"}],
    ]}


def reason_question(lead: dict[str, Any]) -> str:
    return f"🎯 <b>Лид {label(lead)}</b>\nНима сабабдан сотилмади?"


def reason_keyboard(checkin_id: str) -> dict[str, Any]:
    buttons = [{"text": r, "callback_data": f"{PREFIX}:{checkin_id}:r{i}"} for i, r in enumerate(REASONS)]
    return {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}


def parse_value(rest: str) -> tuple[str, str] | None:
    """``"<checkin id>:<value>"`` → (checkin id, value) for a known value, or None."""
    checkin_id, _, value = rest.rpartition(":")
    if not checkin_id:
        return None
    if value in ("t", "y", "n", "s", "x", "p"):
        return checkin_id, value
    if value[:1] == "r" and value[1:].isdigit() and int(value[1:]) < len(REASONS):
        return checkin_id, value
    return None


def is_other_reason(value: str) -> bool:
    return value == f"r{len(REASONS) - 1}"


def answered_line(lead: dict[str, Any], what: str) -> str:
    """The question's message once answered: the lead and what was chosen."""
    return f"🎯 <b>Лид {label(lead)}</b>\n✅ {escape(what)}"


SALE_QUESTION = "Нима ва қанчага сотилди? Битта хабарда ёзинг, масалан: «2 та кир ювиш машинаси, 120 млн сўм»."
SALE_AGAIN = "Суммани тушунмадим — рақам билан ёзинг, масалан: «120 млн сўм» ёки «$5 000»."
REASON_QUESTION = "Сабабини қисқача ёзиб беринг."
CLOSED_NOTICE = "🎯 Лид {label}: {days} кун ичида мижоз билан боғланилмади — лид ёпилди («алоқага чиқилмади»)."


# ------------------------------------------------------- the amount sold

_AMOUNT = re.compile(
    r"(?P<pre>\$)?\s*(?P<num>\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*"
    r"(?P<mult>млрд|миллиард|mlrd|млн|миллион|mln|million|минг|ming|тыс\.?|k\b)?\s*"
    r"(?P<cur>\$|доллар|dollar|usd|у\.?е\.?|сўм|сум|so'?m|som|uzs)?",
    re.IGNORECASE,
)
_MULT = {"млрд": 1e9, "миллиард": 1e9, "mlrd": 1e9, "млн": 1e6, "миллион": 1e6, "mln": 1e6, "million": 1e6,
         "минг": 1e3, "ming": 1e3, "тыс": 1e3, "тыс.": 1e3, "k": 1e3}
_USD = ("$", "доллар", "dollar", "usd", "уе", "у.е", "у.е.", "уе.")


def parse_amount(text: str) -> tuple[float, str] | None:
    """The sale's amount from the seller's words: (amount, 'UZS' | 'USD'), or None when it isn't clear.

    «2 та машина, 120 млн сўм» → (120 000 000, 'UZS'); «$5 000» → (5000, 'USD'). A number with a unit (млн,
    минг, сўм, $) wins over a bare one (the «2 та»); among those the largest. A bare so'm figure under 100 000
    («120») is not taken — it is asked again rather than guessed.
    """
    found = []
    for m in _AMOUNT.finditer(text or ""):
        raw = m.group("num").replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            continue
        mult = (m.group("mult") or "").lower()
        cur = (m.group("cur") or "").lower()
        usd = bool(m.group("pre")) or cur in _USD
        value *= _MULT.get(mult, 1)
        has_unit = bool(mult or cur or m.group("pre"))
        found.append((has_unit, value, "USD" if usd else "UZS", bool(mult)))
    if not found:
        return None
    with_unit = [f for f in found if f[0]]
    has_unit, value, currency, has_mult = max(with_unit or found, key=lambda f: f[1])
    if value <= 0 or (currency == "UZS" and value < 100_000 and not has_mult):
        return None
    return value, currency


def money(amount: float | None, currency: str | None = "UZS") -> str:
    """«120 млн сўм», «1,5 млрд сўм», «350 000 сўм», «$5 000»."""
    if amount is None:
        return "—"
    if (currency or "UZS") == "USD":
        return "$" + f"{amount:,.0f}".replace(",", " ")
    if amount >= 1e9:
        return f"{amount / 1e9:,.1f}".replace(",", " ").replace(".", ",").replace(",0", "") + " млрд сўм"
    if amount >= 1e6:
        return f"{amount / 1e6:,.1f}".replace(",", " ").replace(".", ",").replace(",0", "") + " млн сўм"
    return f"{amount:,.0f}".replace(",", " ") + " сўм"


# ------------------------------------------------------- the Boss's statistics


def _days_to_contact(row: dict[str, Any]) -> int | None:
    if row.get("contacted_on") and row.get("assigned_on"):
        return max(0, (row["contacted_on"] - row["assigned_on"]).days)
    return None


def reason_of(row: dict[str, Any]) -> str:
    """The not-sold reason's button («бошқа сабаб: …» typed → «бошқа сабаб»), for counting."""
    return (row.get("outcome") or REASONS[-1]).split(":")[0].strip() or REASONS[-1]


def is_late(row: dict[str, Any], today: date) -> bool:
    """Contacted after ``LATE_AFTER_DAYS`` — or not yet, and already past it."""
    days = _days_to_contact(row)
    if days is not None:
        return days > LATE_AFTER_DAYS
    return row.get("status") in ("new", "dismissed") and (today - row["assigned_on"]).days > LATE_AFTER_DAYS


def _tidy(value: float) -> float | int:
    """One decimal, and a whole number without it (2.0 → 2): «0,8 кунда», «2 кунда»."""
    value = round(value, 1)
    return int(value) if value == int(value) else value


def stats(rows: list[dict[str, Any]], today: date, average_sale: float | None) -> dict[str, Any]:
    """The Boss's figures for these assignments (one row each: assigned_on, contacted_on, status, outcome,
    sold_amount, sold_currency, person, employee_id). ``average_sale`` = the average so'm sale so far — it prices
    the lost opportunity (not sold × the average sale); None when nothing has sold yet."""
    def block(mine: list[dict[str, Any]]) -> dict[str, Any]:
        contacted = [r for r in mine if r.get("contacted_on")]
        days = [_days_to_contact(r) for r in contacted]
        sold = [r for r in mine if r["status"] == "done"]
        lost = [r for r in mine if r["status"] == "dismissed"]
        ours = [r for r in lost if not r.get("contacted_on") or is_late(r, today)]
        sums: dict[str, float] = {}
        for r in sold:
            if r.get("sold_amount") is not None:
                cur = r.get("sold_currency") or "UZS"
                sums[cur] = sums.get(cur, 0.0) + float(r["sold_amount"])
        return {
            "given": len(mine), "contacted": len(contacted),
            "avg_days": _tidy(sum(days) / len(days)) if days else None,
            "late": sum(1 for r in mine if is_late(r, today)),
            "sold": len(sold), "sold_sum": sums, "not_sold": len(lost),
            "open": sum(1 for r in mine if r["status"] in ("new", "in_progress")),
            "waiting": sum(1 for r in mine if r["status"] == "new"),
            "our_fault": len(ours),
            "lost_value": round(len(lost) * average_sale) if average_sale else None,
            "our_fault_value": round(len(ours) * average_sale) if average_sale else None,
        }

    reasons = Counter(reason_of(r) for r in rows if r["status"] == "dismissed")
    sellers: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sellers.setdefault(str(r.get("person") or "—"), []).append(r)
    return {**block(rows), "reasons": reasons.most_common(), "average_sale": average_sale,
            "sellers": [{"name": name, **block(mine)} for name, mine in sorted(sellers.items(), key=lambda x: -len(x[1]))]}


def _sums(sums: dict[str, float]) -> str:
    return " + ".join(money(v, c) for c, v in sorted(sums.items(), key=lambda x: x[0] != "UZS")) or "суммаси ёзилмаган"


def stats_text(s: dict[str, Any], period: str, enabled: bool) -> str:
    """/lidlar and the Monday message: the Boss's statistics as one chat message."""
    head = [f"🎯 <b>Лидлар · B2B сотув</b> — {period}",
            "▶️ Тарқатиш ишлаяпти" if enabled else "⏸ Тарқатиш тўхтатилган"]
    if not s["given"]:
        return "\n".join(head + ["", "Бу даврда лид берилмаган."])
    avg = f"; ўртача {str(s['avg_days']).replace('.', ',')} кунда" if s["avg_days"] is not None else ""
    lines = head + [
        "",
        f"Берилди: <b>{s['given']}</b> · боғланилди: {s['contacted']}{avg}",
        f"Кеч боғланилди ёки ҳали боғланилмади: {s['late']}",
        f"💰 Сотилди: <b>{s['sold']}</b>" + (f" — {_sums(s['sold_sum'])}" if s["sold"] else ""),
        f"❌ Сотилмади: <b>{s['not_sold']}</b> · ⏳ очиқ: {s['open']}",
    ]
    if s["reasons"]:
        lines += ["", "<b>Сотилмаганлик сабаби</b>"] + [f"• {escape(r)} — {n}" for r, n in s["reasons"]]
    if s["not_sold"]:
        if s["lost_value"] is not None:
            lines += ["", f"<b>Бой берилган имконият ≈ {money(s['lost_value'])}</b>",
                      f"<i>({s['not_sold']} лид × ўртача сотув {money(s['average_sale'])})</i>"]
            if s["our_fault"]:
                lines.append(f"шундан боғланилмаган ёки кеч боғланилган {s['our_fault']} лид ≈ {money(s['our_fault_value'])}")
        else:
            lines += ["", "<i>Бой берилган имконият — биринчи сотувдан кейин ҳисобланади (ўртача сотув суммаси керак).</i>"]
    if s["sellers"]:
        lines += ["", "<b>Сотувчилар</b>"]
        for p in s["sellers"]:
            avg_p = f" ({str(p['avg_days']).replace('.', ',')} кунда)" if p["avg_days"] is not None else ""
            sold = f", сотди {p['sold']}" + (f" · {_sums(p['sold_sum'])}" if p["sold"] and p["sold_sum"] else "")
            lines.append(f"• {escape(p['name'])} — {p['given']} лид, боғланди {p['contacted']}{avg_p}{sold}, "
                         f"сотмади {p['not_sold']}, кеч {p['late']}")
    return "\n".join(lines)


def table_rows(rows: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """One line per lead for the PDF table and PULSE: №, company, seller, given, contacted, state, amount / reason."""
    out = []
    for r in sorted(rows, key=lambda x: (x["assigned_on"], int(x.get("lead_id") or 0)), reverse=True):
        days = _days_to_contact(r)
        if r["status"] == "done":
            detail = " · ".join(x for x in (money(float(r["sold_amount"]), r.get("sold_currency"))
                                            if r.get("sold_amount") is not None else "", r.get("outcome") or "") if x)
        elif r["status"] == "dismissed":
            detail = r.get("outcome") or ""
        else:
            detail = ""
        out.append({
            "no": r.get("lead_id"), "company": lead_name(r), "seller": r.get("person") or "—",
            "given": f"{r['assigned_on']:%d.%m}",
            "contacted": (f"{r['contacted_on']:%d.%m} · {days} кун" if days is not None else "—"),
            "late": is_late(r, today), "status": r["status"], "state": STATUS_LABELS.get(r["status"], r["status"]),
            "detail": detail[:160],
        })
    return out


# ------------------------------------------------------- the Director's questions


def describe(rows: list[dict[str, Any]], s: dict[str, Any] | None = None) -> str:
    """Plain-text data for OPS Manager Bot's answers about B2B leads (the last 30 days)."""
    if not rows:
        return ("B2B lead tracker (back since 2026-10-28; the Boss can stop/start it with /lidlar): "
                "no leads were handed out in the last 30 days.")
    lines = ["B2B lead tracker — leads handed to B2B sales in the last 30 days (each has a number, «№33»). "
             "Statuses: боғланилмаган (not contacted yet) / жараёнда / сотилди / сотилмади."]
    if s:
        lines.append(
            f"Totals: given {s['given']}, contacted {s['contacted']} (avg {s['avg_days']} days), late or not "
            f"contacted {s['late']}, sold {s['sold']} ({_sums(s['sold_sum'])}), not sold {s['not_sold']}, open "
            f"{s['open']}. Not-sold reasons: " + ", ".join(f"{r} {n}" for r, n in s["reasons"])
            + (f". Lost opportunity estimate: {money(s['lost_value'])} (not sold × average sale "
               f"{money(s['average_sale'])})." if s["lost_value"] is not None else "."))
    for r in rows:
        days = _days_to_contact(r)
        contacted = f"contacted {r['contacted_on']:%Y-%m-%d} ({days} d)" if days is not None else "not contacted"
        sold = (f" — {money(float(r['sold_amount']), r.get('sold_currency'))}" if r.get("sold_amount") is not None else "")
        extra = f" — {r['outcome']}" if r.get("outcome") else ""
        lines.append(f"- №{r.get('lead_id')} {lead_name(r)} → {r['person']} (given {r['assigned_on']:%Y-%m-%d}) | "
                     f"{contacted} | {STATUS_LABELS.get(r['status'], r['status'])}{sold}{extra}")
    return "\n".join(lines)
