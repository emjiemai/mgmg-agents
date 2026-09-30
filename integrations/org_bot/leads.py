"""Leads to B2B sales: one each morning, "how is it going?" at 15:00.

The owner's request (2026-09-30): the Lead Agent finds leads; every morning
each B2B sales person gets one, and at 15:00 the bot asks how it is going
with three buttons — жараёнда (in progress), рад этилди (dismissed),
бажарилди (done) — then one question about that result. KPI counts it.

Decisions from the owner, the same day:
- a thin morning (fewer new leads than people) is topped up with the best
  older leads nobody has worked yet;
- an in-progress lead stays with the person and is asked about again every
  day at 15:00 until it is closed — and a new lead still comes every
  morning, so open leads can pile up;
- every track goes to B2B sales (Londry equipment and service, and the
  Garmin/Tanita sponsorship track);
- KPI counts leads inside its existing parts (``kpi_score``).

The Lead Agent writes English; everything a sales person sees is Uzbek
Cyrillic: the card carries a short summary the AI writes once per lead
(``brief``), and stage and priority in plain words.

Pure logic here (tested offline); ``agents/lead-handout/agent.py`` sends,
``ops_manager`` handles the buttons and the typed answer.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from typing import Any

from integrations.common.translit import latin_to_cyrillic
from integrations.org_bot.tone import casual
from integrations.telegram.bot import escape

AGENT = "lead-handout"
SALES_ROLE = "b2b_sotuv"

# The Lead Agent's Google Sheet (agents/lead-agent writes it; selfcheck keeps
# the two column lists identical).
SHEET_RANGE = "Sheet1!A:T"
SHEET_COLUMNS = [
    "company_name", "project_name", "industry", "location", "project_stage",
    "estimated_opening", "signal", "signal_source_url", "signal_date",
    "estimated_size", "contact_name", "contact_role", "contact_method",
    "confidence", "priority", "recheck_date", "notes", "date_added",
    "dedupe_key", "track",
]

CHECKIN_TIME = time(15, 0)
CHECKIN_WINDOW_MINUTES = 25  # a cron that starts a little late still asks; 15:35 doesn't
# A typed message counts as the answer to the follow-up question for this long.
ANSWER_WINDOW_MINUTES = 90

# callback letter -> (status, what the button says)
STATUSES: dict[str, tuple[str, str]] = {
    "p": ("in_progress", "жараёнда"),
    "x": ("dismissed", "рад этилди"),
    "d": ("done", "бажарилди"),
}
STATUS_LABELS = {status: label for status, label in STATUSES.values()}
# The follow-up to each result. The last choice of each list means "let me write it".
DISMISS_REASONS = ("эҳтиёжи йўқ", "боғланиб бўлмади", "бошқадан олишган", "бошқа сабаб")
DONE_RESULTS = ("учрашув бўлди", "таклиф юборилди", "шартнома тузилди", "бошқа натижа")
CHOICES = {"dismissed": DISMISS_REASONS, "done": DONE_RESULTS}
IN_PROGRESS_QUESTION = "кейинги қадамингиз нима ва қачон?"
OTHER_QUESTION = "қисқача ёзиб берасизми?"

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


def rows_to_leads(rows: list[list[str]]) -> list[dict[str, Any]]:
    """The sheet's rows (header first) as lead dicts ready for ``leads``; unnamed rows are skipped."""
    leads = []
    for row in rows[1:]:
        cells = {c: (row[i].strip() if i < len(row) and row[i] else "") for i, c in enumerate(SHEET_COLUMNS)}
        name = cells["company_name"] or cells["project_name"]
        if not name:
            continue
        leads.append({
            "dedupe_key": cells["dedupe_key"] or f"{name.lower()}|{cells['track']}",
            **{c: cells[c] or None for c in (
                "company_name", "project_name", "industry", "location", "project_stage", "signal",
                "signal_source_url", "contact_name", "contact_role", "contact_method", "track",
            )},
            "priority": cells["priority"].lower() or None,
            "confidence": _number(cells["confidence"]),
            "date_added": _date(cells["date_added"]),
        })
    return leads


def handout_order(lead: dict[str, Any], today: date) -> tuple:
    """Sort key: today's new leads first, then priority, confidence, and the newer one."""
    added = lead.get("date_added")
    return (
        added != today,
        _PRIORITY_RANK.get((lead.get("priority") or "").lower(), 3),
        -float(lead.get("confidence") or 0),
        -(added.toordinal() if added else 0),
    )


def plan_handout(people: list[dict[str, Any]], free_leads: list[dict[str, Any]], today: date) -> list[tuple[dict, dict]]:
    """Who gets which lead this morning: one each, best leads first, until the leads run out."""
    ordered = sorted(free_leads, key=lambda lead: handout_order(lead, today))
    people = sorted(people, key=lambda p: ((p.get("full_name") or p.get("display_name") or "").lower(), str(p["id"])))
    return list(zip(people, ordered))


def checkin_due(now_local: datetime) -> bool:
    """Whether this run is the 15:00 check-in."""
    start = CHECKIN_TIME.hour * 60 + CHECKIN_TIME.minute
    minutes = now_local.hour * 60 + now_local.minute
    return start <= minutes < start + CHECKIN_WINDOW_MINUTES


# ------------------------------------------------------------- the card

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
Answer with JSON only: {"brief": "...", "location": "..."}
"""


def brief_prompt(lead: dict[str, Any]) -> str:
    """The lead's facts, for the AI."""
    fields = ("company_name", "project_name", "industry", "location", "project_stage", "signal", "track")
    return "\n".join(f"{f}: {lead.get(f) or ''}" for f in fields)


_CYRILLIC = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳ]")
_LETTER = re.compile(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]")


def parse_brief(raw: str) -> tuple[str, str] | None:
    """(brief, location) from the AI, or None if it isn't a short, mostly-Cyrillic answer."""
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
    if re.search(r"[A-Za-z]", location) or len(location) > 60:
        location = ""
    return brief, location


def fallback_brief(lead: dict[str, Any]) -> str:
    """The card's summary line without the AI: the facts line below it already has stage and priority."""
    return "Батафсил маълумот манбада."


def lead_name(lead: dict[str, Any]) -> str:
    return (lead.get("company_name") or lead.get("project_name") or "—").strip()


def card_text(lead: dict[str, Any], brief: str, location: str = "") -> str:
    """The 08:00 card — work, so a card, not a chat line (tasks keep their format)."""
    place = location or latin_to_cyrillic(lead.get("location") or "")
    lines = [f"🎯 <b>Бугунги лид: {escape(lead_name(lead))}</b>", "", escape(brief)]
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
    lines += ["", "<i>соат 15:00 да қандай кетаётганини сўрайман</i>"]
    return "\n".join(lines)


# ------------------------------------------------------ the 15:00 question


def checkin_text(name: str, lead: dict[str, Any], days_open: int) -> str:
    """One friendly line per open lead."""
    since = f", {days_open}-кун" if days_open > 1 else ""
    return casual(f"{escape(name)}, «{escape(lead_name(lead))}» лиди қандай кетяпти{since}?", "🙂")


def checkin_keyboard(checkin_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": label, "callback_data": f"ld:{checkin_id}:{letter}"} for letter, (_status, label) in STATUSES.items()
    ]]}


_LETTER_OF = {status: letter for letter, (status, _label) in STATUSES.items()}


def choice_keyboard(checkin_id: str, status: str) -> dict[str, Any]:
    """The reason (dismissed) or result (done) buttons, two to a row: ``lc:<id>:<letter><index>``."""
    letter = _LETTER_OF[status]
    buttons = [{"text": c, "callback_data": f"lc:{checkin_id}:{letter}{i}"} for i, c in enumerate(CHOICES[status])]
    return {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}


def parse_callback(rest: str) -> tuple[str, str] | None:
    """``"<checkin id>:<value>"`` → (checkin id, value), or None."""
    checkin_id, _, value = rest.rpartition(":")
    return (checkin_id, value) if checkin_id and value else None


def parse_choice(value: str) -> tuple[str, int, str] | None:
    """``"x2"`` → ("dismissed", 2, "бошқадан олишган"), or None if it isn't a valid choice."""
    status = STATUSES.get(value[:1], ("", ""))[0]
    if status not in CHOICES or not value[1:].isdigit() or int(value[1:]) >= len(CHOICES[status]):
        return None
    index = int(value[1:])
    return status, index, CHOICES[status][index]


def choice_question(lead: dict[str, Any], status: str) -> str:
    """The line shown with the reason/result buttons."""
    ask = "нима сабабдан?" if status == "dismissed" else "натижаси қандай?"
    return casual(f"«{escape(lead_name(lead))}» лиди: {STATUS_LABELS[status]}, {ask}", "🙂")


def status_line(lead: dict[str, Any], status: str, outcome: str | None = None) -> str:
    """The check-in message once answered: the lead, what was chosen."""
    chosen = STATUS_LABELS[status] + (f", {outcome}" if outcome else "")
    return casual(f"«{escape(lead_name(lead))}» лиди: {escape(chosen)}", "✅")


def is_other(status: str, index: int) -> bool:
    """Whether the chosen reason/result is "other" — then the person is asked to write it."""
    return index == len(CHOICES[status]) - 1


# ------------------------------------------------------- the Director's view


def describe(rows: list[dict[str, Any]]) -> str:
    """Plain-text data for OPS Manager Bot's answers about leads (last 30 days)."""
    if not rows:
        return "No leads have been handed to sales people in the last 30 days."
    open_count = sum(1 for r in rows if r["status"] in ("new", "in_progress"))
    lines = [
        f"Leads handed to B2B sales in the last 30 days: {len(rows)} ({open_count} still open). "
        "Each person gets one every morning; at 15:00 they report in progress / dismissed / done.",
    ]
    for r in rows:
        status = {"new": "not answered yet", **STATUS_LABELS}.get(r["status"], r["status"])
        extra = f" — {r['outcome']}" if r.get("outcome") else ""
        note = f" | last note: {r['last_note']}" if r.get("last_note") else ""
        lines.append(
            f"- {r['person']}: {r['company']} (given {r['assigned_on']:%Y-%m-%d}) | {status}{extra}{note}"
        )
    return "\n".join(lines)
