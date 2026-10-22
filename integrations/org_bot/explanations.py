"""Explanations — absent yesterday, report not sent (2026-10-22, the Boss).

«Аниқланг, ростан келмаган бўлса тушунтириш берсин, AI сўрасин.» and «AI улардан изоҳ олсин,
тушунтириш хати ёзиб беришсин!» Every morning, after the brief (agents/explanations):

  * **absent** — whoever Verifix shows absent yesterday (a working day, no arrival, nothing that
    excuses it; never «эркин график») is asked why, with every working day in a row they were absent
    ("охирги 3 иш куни: 19.10, 20.10, 21.10") — and to say so if they did come (Verifix can miss
    a face);
  * **report** — whoever's daily report of yesterday didn't come (and wasn't absent — that one ask
    covers both).

The person's next message (text, or a voice turned into text) — or a reply to the ask — is the
explanation: it goes as a card to the Director, and to the Garmin manager for his team. The next
morning the Director is told who answered and who didn't. One ask per person, kind and day; the
Director and the Garmin manager themselves are never asked. Counts-only logs.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from integrations.common.translit import name_to_cyrillic
from integrations.org_bot.tone import casual
from integrations.telegram.bot import escape

NEVER_ASKED = ("operatsion_direktor", "garmin_manager")  # the Boss and the Garmin manager (kpi.NO_REPORT_ROLES)
OPEN_HOURS = 36  # an ask still waits for its answer this long
STREAK_DAYS = 14  # how far back a run of absent days is looked for


def _key(text: str | None) -> list[str]:
    from integrations.garmin.competitors import normalize

    plain = normalize(name_to_cyrillic(text or "")).replace("ў", "у").replace("ғ", "г").replace("қ", "к").replace("ҳ", "х")
    return [w for w in plain.split() if len(w) >= 3]


def match_employee(verifix_name: str, employees: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The bot's employee for a Verifix person: every word of the bot name in the Verifix name
    («Абдукарим Зунунов» ↔ «Зунунов Абдукарим Абдирахманович»); None when none or several fit."""
    words = set(_key(verifix_name))
    fits = []
    for e in employees:
        mine = _key(e.get("full_name")) or _key(e.get("display_name"))
        if len(mine) >= 2 and all(w in words for w in mine):
            fits.append(e)
    return fits[0] if len(fits) == 1 else None


def absent_streaks(recs: Iterable[Any], yesterday: date) -> dict[str, tuple[str, list[date]]]:
    """Verifix id -> (name, the working days absent in a row up to yesterday) — only people absent yesterday."""
    by_person: dict[str, list[Any]] = {}
    for r in recs:
        if r.working and r.day <= yesterday:
            by_person.setdefault(r.employee_id, []).append(r)
    out: dict[str, tuple[str, list[date]]] = {}
    for pid, days in by_person.items():
        days.sort(key=lambda r: r.day, reverse=True)
        if not days or days[0].day != yesterday or days[0].status != "absent":
            continue
        streak = []
        for r in days:
            if r.status != "absent":
                break
            streak.append(r.day)
        out[pid] = (days[0].name, sorted(streak))
    return out


def _days(days: list[date]) -> str:
    return ", ".join(f"{d:%d.%m}" for d in days)


def ask_text(kind: str, name: str, days: list[date]) -> str:
    """The polite question (the friendly voice: one line, «сиз», no emoji)."""
    if kind == "report":
        text = (f"{escape(name)}, кечаги, {days[-1]:%d.%m} кунлик ҳисоботингиз келмади, сабабини қисқа ёзиб "
                "юборсангиз, тушунтиришингиз раҳбариятга етказилади, раҳмат")
    elif len(days) == 1:
        text = (f"{escape(name)}, Verifix бўйича кеча, {days[0]:%d.%m} куни ишга келганингиз кўринмади, сабабини ёзиб "
                "юборсангиз, тушунтириш хатингиз раҳбариятга етказилади, келган бўлсангиз ҳам шуни ёзинг, раҳмат")
    else:
        text = (f"{escape(name)}, Verifix бўйича охирги {len(days)} иш куни ({_days(days)}) ишга келганингиз кўринмади, "
                "сабабини ёзиб юборсангиз, тушунтириш хатингиз раҳбариятга етказилади, келган бўлсангиз ҳам шуни "
                "ёзинг, раҳмат")
    return casual(text, keep=[escape(name), "Verifix"])


def card(person: str, role: str, kind: str, days: list[date], answer: str) -> str:
    """The explanation as the Director (and the Garmin manager) see it."""
    what = (f"ҳисобот келмаган кун: {_days(days)}" if kind == "report" else
            f"келмаган кун: {_days(days)}" if len(days) == 1 else f"келмаган кунлар: {_days(days)} ({len(days)} иш куни)")
    return (f"📝 <b>Тушунтириш хати</b> — {escape(person)} ({escape(role)})\n{what}\n"
            f"«{escape(' '.join(answer.split())[:1500])}»")


def thanks_text() -> str:
    return casual("раҳмат, тушунтиришингиз раҳбариятга етказилди")


def summary(day: date, rows: list[dict[str, Any]]) -> str | None:
    """The next morning, for the Director: yesterday's asks — who wrote, who didn't. None when nobody was asked."""
    if not rows:
        return None
    silent = [r for r in rows if not r.get("answered_at")]
    lines = [f"📝 <b>Тушунтириш хатлари — {day:%d.%m}</b>",
             f"Сўралди: {len(rows)} · ёзди: {len(rows) - len(silent)} · ёзмади: {len(silent)}"]
    if silent:
        lines.append("Ёзмаганлар: " + ", ".join(
            f"{escape(r['name'])} ({'ҳисобот' if r['kind'] == 'report' else 'келмади'})" for r in silent))
    return "\n".join(lines)


def is_explanation(text: str) -> bool:
    """A message that can be the awaited explanation: not a command, not a question to the AI («?»)."""
    text = (text or "").strip()
    return bool(text) and not text.startswith("/") and "?" not in text
