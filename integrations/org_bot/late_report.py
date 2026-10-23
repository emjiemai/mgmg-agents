"""Late reports — yesterday's report sent the next day, approved by the Boss or the Garmin manager (2026-10-23).

«When they forget to send the report, from yesterday's, their report goes to the Garmin manager or the Boss
asking to approve or decline yesterday's report; it will be set to green when approved.»

  * The morning «your report of yesterday didn't come» ask (explanations.py) carries **📝 Кечаги ҳисоботни
    юбориш**. Tapped, the person's next text (within ``AWAIT_HOURS``) is that report; it also answers the ask.
  * It goes as a card to the Director, and to the Garmin manager for his team: **✅ Тасдиқлаш / ❌ Рад этиш**.
  * Approved → the day's report counts as sent (the calendar's square turns green, the 22:00 minus is taken
    off); declined → it stays missed. The person is told either way; the first decision wins. No report
    bonus for a late report. When the Garmin manager decides, the Director is told.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local, to_local
from integrations.org_bot import excused, names, store
from integrations.org_bot.report_calendar import TEAM_OF
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS
from integrations.org_bot.tone import casual
from integrations.telegram.bot import TelegramBot, escape

AGENT = "ops-manager-bot"
log = setup_logging(AGENT)

PREFIXES = ("lrs", "lrok", "lrno")
AWAIT_HOURS = 12  # after the tap, the next message is the report this long


def ask_button(day: date) -> dict[str, Any]:
    """On the morning «report didn't come» ask: send it now."""
    return {"inline_keyboard": [[{"text": "📝 Кечаги ҳисоботни юбориш", "callback_data": f"lrs:{day:%Y%m%d}"}]]}


def prompt_text(day: date) -> str:
    return casual(f"марҳамат, {day:%d.%m} кунги ҳисоботингизни битта хабарда ёзиб юборинг, у раҳбариятга тасдиқлаш "
                  "учун юборилади, раҳмат")


def sent_text() -> str:
    return casual("раҳмат, кечаги ҳисоботингиз раҳбариятга тасдиқлаш учун юборилди")


def decided_text(day: date, approved: bool) -> str:
    if approved:
        return casual(f"{day:%d.%m} кунги ҳисоботингиз тасдиқланди, ҳисобот юборилган деб ҳисобланди, раҳмат")
    return casual(f"{day:%d.%m} кунги ҳисоботингиз бу сафар қабул қилинмади, кейинги ҳисоботларни 22:00 гача юборсангиз "
                  "жуда яхши бўлади, раҳмат")


def card(person: str, role: str, day: date, sent_at: datetime | None, content: str) -> str:
    when = f", юборилди {to_local(sent_at):%d.%m %H:%M}" if sent_at else ""
    return (f"🕘 <b>Кечиккан ҳисобот</b> — {escape(person)} ({escape(role)})\n{day:%d.%m} учун{when}\n"
            f"«{escape(' '.join(content.split())[:1500])}»")


def card_keyboard(report_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "✅ Тасдиқлаш", "callback_data": f"lrok:{report_id}"},
                                 {"text": "❌ Рад этиш", "callback_data": f"lrno:{report_id}"}]]}


def managers_of(role: str | None) -> list[str]:
    """Who decides: the Director, and the manager whose team the person is in."""
    return [DIRECTOR_ROLE, *(m for m, team in TEAM_OF.items() if role in team)]


# ---------------------------------------------------------------------------------------------------- sending

def _bot(run_id: uuid.UUID | None) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def _answer(callback: dict[str, Any], text: str) -> None:
    async with _bot(None) as bot:
        await bot._answer_callback(callback.get("id", ""), text)  # noqa: SLF001


async def _send(chat_id: int, text: str, run_id: uuid.UUID, keyboard: dict[str, Any] | None = None) -> None:
    async with _bot(run_id) as bot:
        await bot.send_message(text, chat_id=str(chat_id), reply_markup=keyboard)


# ---------------------------------------------------------------------------------------------------- handling

async def take_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """The person's text after the tap is yesterday's report: to the deciders as a card. None otherwise."""
    text = (message.get("text") or "").strip()
    if not text or text.startswith("/"):
        return None
    try:
        waiting = await store.late_report_awaiting(employee["telegram_user_id"], AWAIT_HOURS)
    except Exception as exc:  # noqa: BLE001 — the message goes on to the usual flow
        log.warning("Late report state not read: {}", type(exc).__name__)
        return None
    if waiting is None:
        return None
    row = await store.save_late_report(str(waiting["id"]), text)
    if row is None:
        return None
    day = row["report_date"]
    # it answers the morning «why no report?» ask too — the card below is what the deciders read
    asks = await store.open_report_explanations(str(employee["id"]), day)
    if asks:
        await store.answer_explanations([a["id"] for a in asks], "[кечиккан ҳисобот юборилди]")
    person = names.person_name(employee)
    role = ROLE_LABELS.get(employee.get("role") or "", employee.get("role") or "")
    shown = card(person, role, day, row.get("late_sent_at"), text)
    delivered = 0
    async with _bot(run_id) as bot:
        for who in managers_of(employee.get("role")):
            for boss in await store.active_employees_by_role(who):
                try:
                    await bot.send_message(shown, chat_id=str(boss["telegram_user_id"]), reply_markup=card_keyboard(str(row["id"])))
                    delivered += 1
                except Exception as exc:  # noqa: BLE001 — the next one still gets it
                    log.error("Late report not delivered: {}", type(exc).__name__)
    await _send(employee["telegram_user_id"], sent_text(), run_id)
    await log_action(agent=AGENT, action="late_report_sent", target_system="telegram", status="success",
                     run_id=run_id, mode="notify", payload={"delivered": delivered})
    return "late_report_sent"


async def handle_callback(prefix: str, rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str | None:
    if prefix not in PREFIXES:
        return None
    clicker_id = (callback.get("from") or {}).get("id")
    clicker = await store.get_employee_by_telegram_id(clicker_id) if clicker_id else None
    if clicker is None or clicker.get("status") != "active":
        await _answer(callback, "Рухсат йўқ")
        return "late_unauthorized"

    if prefix == "lrs":  # the person: «send yesterday's report»
        try:
            day = datetime.strptime(rest, "%Y%m%d").date()
        except ValueError:
            await _answer(callback, "Номаълум амал")
            return "late_bad_day"
        if day != today_local() - timedelta(days=1) or await store.await_late_report(clicker_id, day) is None:
            await _answer(callback, "Бу ҳисоботни энди юбориб бўлмайди")
            return "late_closed"
        await _answer(callback, "📝")
        await _send(clicker_id, prompt_text(day), run_id)
        return "late_report_asked"

    report = await store.get_report(rest)
    person = await store.get_employee(str(report["employee_id"])) if report else None
    if report is None or not excused.may_excuse(clicker, person):
        await _answer(callback, "Рухсат йўқ")
        return "late_unauthorized"
    approve = prefix == "lrok"
    row = await store.decide_late_report(rest, approve, clicker_id)
    message = callback.get("message") or {}
    if row is None:
        await _answer(callback, "Аллақачон ҳал қилинган")
        return "late_already_decided"
    verdict = "✅ Тасдиқланди" if approve else "❌ Рад этилди"
    async with _bot(run_id) as bot:
        await bot._edit_message(  # noqa: SLF001
            chat_id=str((message.get("chat") or {}).get("id")), message_id=message.get("message_id"),
            text=f"{card(names.person_name(person), ROLE_LABELS.get(person.get('role') or '', ''), row['report_date'], row.get('late_sent_at'), row.get('late_content') or '')}"
                 f"\n{verdict} — {escape(names.person_name(clicker))}",
            reply_markup={"inline_keyboard": []})
    await _answer(callback, verdict)
    try:
        await _send(person["telegram_user_id"], decided_text(row["report_date"], approve), run_id)
    except Exception as exc:  # noqa: BLE001
        log.error("Late report decision not told: {}", type(exc).__name__)
    if clicker.get("role") != DIRECTOR_ROLE:
        for boss in await store.active_employees_by_role(DIRECTOR_ROLE):
            try:
                await _send(boss["telegram_user_id"], f"🕘 {escape(names.person_name(clicker))}: "
                            f"{escape(names.person_name(person))} — {row['report_date']:%d.%m} кечиккан ҳисобот "
                            f"{'тасдиқланди' if approve else 'рад этилди'}", run_id)
            except Exception as exc:  # noqa: BLE001
                log.error("Director not told of a late report: {}", type(exc).__name__)
    await log_action(agent=AGENT, action="late_report_approved" if approve else "late_report_declined",
                     target_system="postgres", status="success", run_id=run_id, mode="write")
    return "late_report_approved" if approve else "late_report_declined"
