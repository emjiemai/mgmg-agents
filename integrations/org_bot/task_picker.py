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


def card_text(task_summary: str, raw_message: str, picked: list[dict[str, Any]], note: str = "") -> str:
    """The card the Director confirms: the task, and who is ticked now."""
    who = ", ".join(escape(names.person_name(p)) for p in picked) or "ҳеч ким"
    lines = ["📋 <b>Топшириқ — кимга юборилсин?</b>", "", sanitize_model_html(task_summary)]
    if note:
        lines += ["", f"<i>{escape(note)}</i>"]
    if raw_message.strip() and raw_message.strip() != task_summary.strip():
        lines += ["", f"<i>Сиздан:</i> {escape(raw_message.strip()[:300])}"]
    lines += ["", f"👥 Белгиланган: {who}", "<i>Исмни босиб белгиланг ёки олиб ташланг, кейин «Юбориш».</i>"]
    return "\n".join(lines)


def keyboard(draft_id: str, people: list[dict[str, Any]], selected: set[str]) -> dict[str, Any]:
    """A tick-box per person (two to a row), then Send / Cancel."""
    buttons = [
        {"text": f"{'✅' if str(p['id']) in selected else '▫️'} {names.person_name(p)}",
         "callback_data": f"tdt:{draft_id}:{i}"}
        for i, p in enumerate(people)
    ]
    rows = [buttons[i:i + PER_ROW] for i in range(0, len(buttons), PER_ROW)]
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


def cancelled_text(task_summary: str) -> str:
    return f"📋 {sanitize_model_html(task_summary)}\n\n❌ Бекор қилинди — ҳеч кимга юборилмади."
