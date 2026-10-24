"""One morning message per employee — at most one question (2026-10-24, the owner).

"In the morning OPS Manager bot might ask multiple questions at once — Verifix, report and other questions.
So it doesn't overwhelm or make mistakes, it needs to know what its job is for one specific employee."

The 08:00 agents that speak to employees no longer send on their own. Each leaves a note (table morning_notes):

  * **bonus** — the report bonus (agents/report-bonus): +1 for yesterday's report, or why not this time;
  * **ask** — the «why weren't you at work / where's yesterday's report?» question (agents/explanations);
  * **task** — a task due today or tomorrow, or one that just went overdue (agents/task-tracker --morning).

Then agents/morning-notes sends each person ONE message: the facts as short lines and the question, if any,
last — with its button (📝 send yesterday's report). A missed report is said once: inside the question
(«…келмади, шунинг учун бу сафар балл берилмади…»), never as a separate «no point» message. The message
is the one a reply answers (``explanations.prompt_message_id``). One item alone reads as before: one polite
line with the name. If the 08:00 job dies before sending, the API sends what is left half an hour later.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import date
from typing import Any

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import explanations, names
from integrations.org_bot.tone import casual
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "morning-notes"
log = setup_logging(AGENT)

ORDER = {"bonus": 0, "task": 1, "ask": 2}  # good news first, the question last
FLUSH_AFTER_MINUTES = 30  # the API sends what the 08:00 job left unsent (app.py)
FLUSH_HOURS = range(8, 13)


def _day(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def bonus_line(d: dict[str, Any]) -> str:
    """The report bonus as one line without the name (agents/report-bonus)."""
    day, per = f"{_day(d['report_date']):%d.%m}", int(d.get("per") or 1)
    if int(d.get("points") or 0):
        return (f"кечаги, {day} ҳисоботингиз учун раҳмат каттакон, KPI баллингизга +{d['points']} балл қўшилди, "
                f"шу ойда жами +{d.get('month') or d['points']} балл")
    if d.get("status") == "submitted":
        return (f"кечаги, {day} ҳисоботингиз хабарларингиздан тузилгани учун бу сафар балл берилмади, кейинги сафар "
                f"ҳисоботни ўзингиз юборсангиз +{per} балл қўшилади")
    return (f"кечаги, {day} ҳисоботингиз келмагани учун бу сафар балл берилмади, бугунги ҳисоботни соат 22:00 гача "
            f"юборсангиз эртага +{per} балл қўшилади, раҳмат")


def task_line(d: dict[str, Any]) -> str:
    """A task due soon or just overdue, as one line (the task card itself keeps its buttons)."""
    code = f"{escape(d['code'])} " if d.get("code") else ""
    what = escape(" ".join(str(d.get("summary") or "").split())[:90])
    due = _day(d["due"])
    if d.get("overdue"):
        return f"{code}«{what}» топшириғининг муддати ўтди ({due:%d.%m}), директорга хабар берилди"
    when = "бугун" if due == _day(d.get("today") or due) else "эртага"
    return f"{code}«{what}» топшириғининг муддати {when}, {due:%d.%m}"


def compose(name: str, notes: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None, list[str]]:
    """One message for one person: (text, the question's buttons, the explanation ids it asks).

    ``notes``: {"kind": "bonus" | "task" | "ask", "detail": {...}}. At most one question, last; a missed
    report's «no point» goes inside the report question instead of its own line.
    """
    from integrations.org_bot import late_report  # local: late_report imports the bot's flows

    notes = sorted(notes, key=lambda n: ORDER.get(n["kind"], 9))
    asks = [n["detail"] for n in notes if n["kind"] == "ask"]
    ask = asks[0] if asks else None
    ids = [str(i) for a in asks for i in (a.get("ids") or [])]
    keep = [name, "KPI", "Verifix"]
    lines: list[str] = []  # ready to show: the friendly lines lowercased, a task's own words left as they are
    no_point = False
    for n in notes:
        d = n["detail"]
        if n["kind"] == "bonus":
            missed = not int(d.get("points") or 0) and d.get("status") != "submitted"
            if missed and ask and ask.get("kind") == "report":
                no_point = True  # said inside the question
                continue
            lines.append(casual(bonus_line(d), keep=keep))
        elif n["kind"] == "task":
            lines.append(task_line(d))
    question = ""
    keyboard = None
    if ask:
        days = [_day(x) for x in ask.get("days") or []]
        question = explanations.ask_line(ask.get("kind") or "absent", days, no_point)
        if ask.get("kind") == "report" and days:
            keyboard = late_report.ask_button(days[-1])
    if not lines:
        return casual(f"{name}, {question}", keep=keep), keyboard, ids
    if not question and len(lines) == 1:
        return f"{name}, {lines[0]}", keyboard, ids
    head = casual(f"{name}, хайрли тонг", keep=keep)
    body = [f"• {line}" for line in lines]
    if any(n["kind"] == "task" and not n["detail"].get("overdue") for n in notes):
        body.append(f"• {casual('бажарилган топшириқда «Бажардим» тугмасини босинг', keep=['Бажардим'])}")
    text = head + "\n" + "\n".join(body)
    if question:
        text += "\n\n" + casual(question, keep=keep)
    return text, keyboard, ids


async def send_day(day: date, run_id: uuid.UUID, older_than_minutes: int = 0) -> dict[str, int]:
    """Send each person their notes of ``day`` as one message. Counts only: people told, failed, skipped."""
    from integrations.org_bot import store

    counts = {"told": 0, "failed": 0, "skipped": 0}
    if settings.bots_frozen:
        return counts
    rows = await store.claim_morning_notes(day, older_than_minutes)
    if not rows:
        return counts
    asked = [str(i) for r in rows if r["kind"] == "ask" for i in ((r["detail"] or {}).get("ids") or [])]
    alive = await store.unanswered_explanation_ids(asked) if asked else set()
    people: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        if row["kind"] == "ask" and not alive.intersection(str(i) for i in (row["detail"] or {}).get("ids") or []):
            continue  # withdrawn before it went out (a day Verifix didn't record)
        people.setdefault(str(row["employee_id"]), []).append(row)
    async with TelegramBot(agent=AGENT, run_id=run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
        for notes in people.values():
            first = notes[0]
            if first.get("employee_status") != "active":
                counts["skipped"] += 1
                continue
            text, keyboard, ids = compose(escape(names.call_name(first)),
                                          [{"kind": n["kind"], "detail": n["detail"] or {}} for n in notes])
            try:
                sent = await bot.send_message(text, chat_id=str(first["telegram_user_id"]), reply_markup=keyboard)
            except TelegramError as exc:
                log.error("Morning message not sent: {}", type(exc).__name__)
                counts["failed"] += 1
                continue
            if sent:
                await store.set_morning_notes_message([str(n["id"]) for n in notes], sent[-1])
                for explanation_id in ids:  # a reply to this message answers the question
                    await store.set_explanation_prompt(explanation_id, sent[-1])
            counts["told"] += 1
    await log_action(agent=AGENT, action="morning_notes_sent", target_system="telegram", status="success",
                     run_id=run_id, mode="notify", payload={"day": day.isoformat(), "notes": len(rows), **counts})
    return counts
