"""Who gets a task: the Director confirms it on a list of people first.

2026-10-02: the Director wrote a task, the bot sent it to a whole department,
and his correction a minute later ("faqat Ulug'bek Isoqovga jo'nat") went
out as a second task. Now nothing reaches an employee straight away: every
task — text or file — comes back to the Director as a card with everyone on
it, the bot's guess already ticked (the named person, or the department),
and goes out only when he taps "Юбориш". He can tick one or many people,
from any department.

Pure logic here (tested offline); ``ops_manager`` holds the draft, handles
the taps and sends.
"""

from __future__ import annotations

from typing import Any

from integrations.org_bot import names
from integrations.org_bot.roles import ROLE_LABELS
from integrations.telegram.bot import escape, sanitize_model_html

PER_ROW = 2


def candidates(people: list[dict[str, Any]], suggested_ids: set[str]) -> list[dict[str, Any]]:
    """The list in button order: the bot's guess first, then everyone else by department and name."""
    def key(person: dict[str, Any]) -> tuple:
        return (
            str(person["id"]) not in suggested_ids,
            ROLE_LABELS.get(person.get("role") or "", person.get("role") or ""),
            names.person_name(person).lower(),
        )
    return sorted(people, key=key)


def accepter_candidates(people: list[dict[str, Any]], selected: set[str]) -> list[dict[str, Any]]:
    """Who may check the proof instead of the Director (2026-11-12): a TEAM_OF manager whose team holds EVERY ticked
    person, and who is not ticked himself — so a manager never sees proof from outside his team, nor his own."""
    from integrations.org_bot.report_calendar import TEAM_OF

    chosen = [p for p in people if str(p["id"]) in selected]
    if not chosen:
        return []
    return [m for m in people
            if m.get("role") in TEAM_OF and str(m["id"]) not in selected
            and all((p.get("role") or "") in TEAM_OF[m["role"]] for p in chosen)]


def accepter_allowed(accepter_id: str | None, people: list[dict[str, Any]], selected: set[str]) -> bool:
    """Is this accepter still right for who is ticked now (checked again on 📨: the ticks may have changed)?"""
    return bool(accepter_id) and str(accepter_id) in {str(m["id"]) for m in accepter_candidates(people, selected)}


def accepter_line(name: str) -> str:
    return f"🔎 Текширади: <b>{escape(name)}</b>"


def accepter_dropped_text(name: str) -> str:
    """📨 with an accepter who no longer fits the ticks: back to the Director, said in one line."""
    return (f"ℹ️ {escape(name)} текширмайди — белгиланганларнинг ҳаммаси унинг жамоасида эмас; "
            "далилни ўзингиз текширасиз.")


def accepter_keyboard(draft_id: str, managers: list[dict[str, Any]]) -> dict[str, Any]:
    """The picker: «Ўзим» (index 0 = the Director) and each manager offered (index into the frozen list + 1)."""
    rows = [[{"text": "🔎 Ўзим", "callback_data": f"tdmp:{draft_id}:0"}]]
    rows += [[{"text": f"🔎 {names.person_name(m)}", "callback_data": f"tdmp:{draft_id}:{i}"}]
             for i, m in enumerate(managers, start=1)]
    return {"inline_keyboard": rows}


def card_text(task_summary: str, raw_message: str, picked: list[dict[str, Any]], note: str = "",
              priority: int | None = None, accepter: str | None = None, expected: str | None = None,
              depends: str | None = None, blocker: str | None = None) -> str:
    """The card the Director confirms: the task, the expected result he said (12.11), the task it waits for (12.11),
    its priority, who checks the proof (when not him), who is ticked. ``blocker`` (12.11, a 🙋 help draft): the
    employee's ⛔ reason, shown here only — in place of «Сиздан»."""
    from integrations.org_bot.task_register import dependency_line, expected_line, helper_blocker_line, priority_label

    who = ", ".join(escape(names.person_name(p)) for p in picked) or "ҳеч ким"
    lines = ["📋 <b>Топшириқ — кимга юборилсин?</b>", "", sanitize_model_html(task_summary)]
    if expected:
        lines.append(expected_line({"expected_result": expected}))
    if depends:
        lines.append(dependency_line(depends))
    lines += ["", f"Устуворлик: <b>{priority_label(priority)}</b>"]
    if accepter:
        lines.append(accepter_line(accepter))
    if note:
        lines += ["", f"<i>{escape(note)}</i>"]
    if blocker is not None:
        lines += ["", helper_blocker_line(blocker)]
    elif raw_message.strip() and raw_message.strip() != task_summary.strip():
        lines += ["", f"<i>Сиздан:</i> {escape(raw_message.strip()[:300])}"]
    lines += ["", f"👥 Белгиланган: {who}", "<i>Исмни босиб белгиланг ёки олиб ташланг, кейин «Юбориш».</i>"]
    return "\n".join(lines)


def keyboard(draft_id: str, people: list[dict[str, Any]], selected: set[str], priority: int | None = None,
             accepter_row: str | None = None, expected_row: bool = False,
             depends_row: bool = False) -> dict[str, Any]:
    """A tick-box per person (two to a row), the priority P1–P4 (2026-11-10), who checks the proof (2026-11-12 —
    only when ``accepter_row`` is given: a manager can be offered, or one is picked), then Send / Cancel."""
    from integrations.org_bot.task_register import DEFAULT_PRIORITY, PRIORITIES
    buttons = [
        {"text": f"{'✅' if str(p['id']) in selected else '▫️'} {names.person_name(p)}",
         "callback_data": f"tdt:{draft_id}:{i}"}
        for i, p in enumerate(people)
    ]
    rows = [buttons[i:i + PER_ROW] for i in range(0, len(buttons), PER_ROW)]
    current = int(priority or DEFAULT_PRIORITY)
    rows.append([{"text": f"{'• ' if n == current else ''}{icon} P{n}", "callback_data": f"tdpri:{draft_id}:{n}"}
                 for n, (icon, _word) in PRIORITIES.items()])
    if accepter_row is not None:
        rows.append([{"text": f"🔎 Текширади: {accepter_row}", "callback_data": f"tdm:{draft_id}"}])
    if expected_row:  # the expected result the AI heard is wrong: one tap drops it (2026-11-12)
        rows.append([{"text": "🎯 Натижани олиб ташлаш", "callback_data": f"tdrx:{draft_id}"}])
    if depends_row:  # it shouldn't wait for that task after all (2026-11-12)
        rows.append([{"text": "⏳ Боғлиқликни олиб ташлаш", "callback_data": f"tddx:{draft_id}"}])
    count = sum(1 for p in people if str(p["id"]) in selected)
    rows.append([
        {"text": f"📨 Юбориш ({count})", "callback_data": f"tds:{draft_id}"},
        {"text": "❌ Бекор", "callback_data": f"tdx:{draft_id}"},
    ])
    return {"inline_keyboard": rows}


def parse_toggle(rest: str) -> tuple[str, int] | None:
    """``"<draft id>:<index>"`` from a tick-box, or None."""
    draft_id, _, index = rest.rpartition(":")
    return (draft_id, int(index)) if draft_id and index.isdigit() else None


def sent_text(task_summary: str, sent: list[dict[str, Any]]) -> str:
    """The card once sent: who got it, with their department."""
    who = ", ".join(
        f"{escape(names.person_name(p))} ({escape(ROLE_LABELS.get(p.get('role') or '', p.get('role') or ''))})"
        for p in sent
    )
    return f"📋 {sanitize_model_html(task_summary)}\n\n✅ Юборилди: {who}."


def reminded_text(people: list[dict[str, Any]]) -> str:
    """Those who already had this task open: reminded, not given it twice (2026-10-17)."""
    who = ", ".join(escape(names.person_name(p)) for p in people)
    return f"🔁 Аввалги топшириғи ҳали тугамаган — эслатилди: {who}."


def duplicate_text(people: list[dict[str, Any]]) -> str:
    """Those the store refused as already given this very message (2026-11-12): said, never silently dropped."""
    who = ", ".join(escape(names.person_name(p)) for p in people)
    return f"ℹ️ Бу топшириқ уларга аввал юборилган — қайта юборилмади: {who}."


def cancelled_text(task_summary: str) -> str:
    return f"📋 {sanitize_model_html(task_summary)}\n\n❌ Бекор қилинди — ҳеч кимга юборилмади."
