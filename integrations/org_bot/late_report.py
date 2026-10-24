"""Late reports — yesterday's report sent the next day, approved by the Boss or the Garmin manager (2026-10-23).

«When they forget to send the report, from yesterday's, their report goes to the Garmin manager or the Boss
asking to approve or decline yesterday's report; it will be set to green when approved.»

  * The morning question «your report of yesterday didn't come» (morning_notes.py) carries **📝 Кечаги ҳисоботни
    юбориш**. Tapped, the person's next message (within ``AWAIT_HOURS``) is that report — typed, a voice / video
    (turned into text) or a photo / file (sent on as it is); it also answers the morning question.
  * Typed as an answer to the question without the tap, it is recognised as the report itself (ops_manager's
    check: reason / report / other) and goes the same way (2026-10-24).
  * It goes as a card to the Director, and to the Garmin manager for his team: **✅ Тасдиқлаш / ❌ Рад этиш**.
  * Approved → the day's report counts as sent (the calendar's square turns green, the 22:00 minus is taken
    off); declined → it stays missed. The person is told either way; the first decision wins. No report
    bonus for a late report. When the Garmin manager decides, the Director is told.

2026-10-24 (the owner: "so it doesn't make mistakes"): the wait after the tap is 3 hours, not 12 — today's
evening report is never taken for yesterday's; a reply to today's 16:00 ask never is either.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local, to_local
from integrations.org_bot import excused, names, report_media, report_tools, store
from integrations.org_bot.report_calendar import TEAM_OF
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS
from integrations.org_bot.tone import casual
from integrations.telegram.bot import TelegramBot, escape

AGENT = "ops-manager-bot"
log = setup_logging(AGENT)

PREFIXES = ("lrs", "lrok", "lrno")
AWAIT_HOURS = 3  # after the tap, the next message is the report this long (was 12 until 2026-10-24)
CARD_TEXT = 1500
CAPTION_TEXT = 600  # a card under a photo / voice: Telegram's caption is 1024 characters


def ask_button(day: date) -> dict[str, Any]:
    """On the morning «report didn't come» question: send it now."""
    return {"inline_keyboard": [[{"text": "📝 Кечаги ҳисоботни юбориш", "callback_data": f"lrs:{day:%Y%m%d}"}]]}


def prompt_text(day: date) -> str:
    return casual(f"марҳамат, {day:%d.%m} кунги ҳисоботингизни битта хабарда ёзиб ёки овозли хабар қилиб юборинг, у "
                  "раҳбариятга тасдиқлаш учун юборилади, раҳмат")


def sent_text() -> str:
    return casual("раҳмат, кечаги ҳисоботингиз раҳбариятга тасдиқлаш учун юборилди")


def reading_text() -> str:
    return casual("раҳмат, овозли хабарингиз матнга айлантирилмоқда, кейин раҳбариятга тасдиқлаш учун юборилади")


def decided_text(day: date, approved: bool) -> str:
    if approved:
        return casual(f"{day:%d.%m} кунги ҳисоботингиз тасдиқланди, ҳисобот юборилган деб ҳисобланди, раҳмат")
    return casual(f"{day:%d.%m} кунги ҳисоботингиз бу сафар қабул қилинмади, кейинги ҳисоботларни 22:00 гача юборсангиз "
                  "жуда яхши бўлади, раҳмат")


def card(person: str, role: str, day: date, sent_at: datetime | None, content: str, limit: int = CARD_TEXT) -> str:
    when = f", юборилди {to_local(sent_at):%d.%m %H:%M}" if sent_at else ""
    return (f"🕘 <b>Кечиккан ҳисобот</b> — {escape(person)} ({escape(role)})\n{day:%d.%m} учун{when}\n"
            f"«{escape(' '.join(content.split())[:limit])}»")


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


async def deliver(employee: dict[str, Any], row_id: str, content: str, run_id: uuid.UUID,
                  file_message_id: int | None = None) -> str | None:
    """Keep ``content`` as the day's late report and put it before the deciders (a file goes as it was sent, the
    card its caption); the morning question is answered by it. None when that day's report can't take one."""
    row = await store.save_late_report(row_id, content)
    if row is None:
        return None
    day = row["report_date"]
    asks = await store.open_report_explanations(str(employee["id"]), day)
    if asks:
        await store.answer_explanations([a["id"] for a in asks], "[кечиккан ҳисобот юборилди]")
    person = names.person_name(employee)
    role = ROLE_LABELS.get(employee.get("role") or "", employee.get("role") or "")
    delivered = 0
    async with _bot(run_id) as bot:
        for who in managers_of(employee.get("role")):
            for boss in await store.active_employees_by_role(who):
                try:
                    if file_message_id:
                        await bot._call("copyMessage", {  # noqa: SLF001 — same-package reuse of the Bot API primitive
                            "chat_id": str(boss["telegram_user_id"]), "from_chat_id": str(employee["telegram_user_id"]),
                            "message_id": file_message_id, "parse_mode": "HTML",
                            "caption": card(person, role, day, row.get("late_sent_at"), content, CAPTION_TEXT),
                            "reply_markup": card_keyboard(str(row["id"]))}, mode="notify",
                            target_ref=str(boss["telegram_user_id"]))
                    else:
                        await bot.send_message(card(person, role, day, row.get("late_sent_at"), content),
                                               chat_id=str(boss["telegram_user_id"]), reply_markup=card_keyboard(str(row["id"])))
                    delivered += 1
                except Exception as exc:  # noqa: BLE001 — the next one still gets it
                    log.error("Late report not delivered: {}", type(exc).__name__)
    await _send(employee["telegram_user_id"], sent_text(), run_id)
    await log_action(agent=AGENT, action="late_report_sent", target_system="telegram", status="success",
                     run_id=run_id, mode="notify", payload={"delivered": delivered, "file": bool(file_message_id)})
    return "late_report_sent"


async def take_typed(employee: dict[str, Any], day: date, text: str, run_id: uuid.UUID) -> str | None:
    """The report typed as the answer to the morning question, without the tap (ops_manager decided it is the
    report itself): the same way as after the tap. None when that day's report can't take it."""
    if day != today_local() - timedelta(days=1):
        return None
    row = await store.report_for_day(employee["telegram_user_id"], day)
    if row is None or row.get("status") != "asked" or row.get("late_content"):
        return None
    return await deliver(employee, str(row["id"]), text, run_id)


async def _voice_to_late(employee: dict[str, Any], row_id: str, file: dict[str, Any], message_id: int,
                         run_id: uuid.UUID) -> None:
    """In the background: a voice / video late report as text, then delivered with the file itself."""
    from integrations.org_bot import ops_manager  # local: ops_manager imports this module

    try:
        content = report_media.report_line(file["kind"], await ops_manager._file_text(file, run_id))  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 — the file still goes, without its text
        log.warning("Late report file not read as text: {}", type(exc).__name__)
        content = report_media.report_line(file["kind"], "")
    try:
        await deliver(employee, row_id, content, run_id, file_message_id=message_id)
    except Exception as exc:  # noqa: BLE001 — a background failure is logged, not raised
        log.error("Late report (voice) failed: {}", type(exc).__name__)


async def take_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID,
                       background: Any = None) -> str | None:
    """The person's message after the tap is yesterday's report. None when nothing waits, or it is something else:
    a command, or a reply to today's 16:00 ask."""
    text = (message.get("text") or message.get("caption") or "").strip()
    if text.startswith("/"):
        return None
    try:
        waiting = await store.late_report_awaiting(employee["telegram_user_id"], AWAIT_HOURS)
    except Exception as exc:  # noqa: BLE001 — the message goes on to the usual flow
        log.warning("Late report state not read: {}", type(exc).__name__)
        return None
    if waiting is None:
        return None
    replied = (message.get("reply_to_message") or {}).get("message_id")
    if replied:
        today = await store.report_for_day(employee["telegram_user_id"], today_local())
        if today and replied in (today.get("prompt_message_id"), today.get("reminder_message_id")):
            return None  # today's report, not yesterday's
    kind = report_tools.file_kind(message)
    if kind is None:
        return await deliver(employee, str(waiting["id"]), text, run_id) if text else None
    file = report_media.file_of(message)
    if file and kind in (*report_media.LISTEN_KINDS, *report_media.WATCH_KINDS) and report_media.readable(kind, file["size"]):
        await _send(employee["telegram_user_id"], reading_text(), run_id)
        job = (_voice_to_late, employee, str(waiting["id"]), file, message["message_id"], run_id)
        if background is not None:
            background.add_task(*job)
        else:
            await _voice_to_late(*job[1:])
        return "late_report_reading"
    # a photo, a document: sent on as it is, its caption the report's words
    return await deliver(employee, str(waiting["id"]), report_media.placeholder(kind, message.get("caption")), run_id,
                         file_message_id=message["message_id"])


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
    role = ROLE_LABELS.get(person.get("role") or "", "")
    chat_id, message_id = str((message.get("chat") or {}).get("id")), message.get("message_id")
    async with _bot(run_id) as bot:
        if message.get("caption") is not None:  # the card under a file: its caption changes
            await bot._call("editMessageCaption", {  # noqa: SLF001
                "chat_id": chat_id, "message_id": message_id, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": []},
                "caption": card(names.person_name(person), role, row["report_date"], row.get("late_sent_at"),
                                row.get("late_content") or "", CAPTION_TEXT) + f"\n{verdict} — {escape(names.person_name(clicker))}"},
                mode="write", target_ref=chat_id)
        else:
            await bot._edit_message(  # noqa: SLF001
                chat_id=chat_id, message_id=message_id,
                text=card(names.person_name(person), role, row["report_date"], row.get("late_sent_at"),
                          row.get("late_content") or "") + f"\n{verdict} — {escape(names.person_name(clicker))}",
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
