"""The B2B lead tracker at work (2026-10-28, the Boss; rules and wording in ``leads.py``, spec 14).

  * **08:00** (``agents/lead-handout --morning``, after the Lead Agent): the leads sheet is copied in, each B2B
    sales person at work gets one numbered lead as a card, the Boss gets the list. Mondays the Boss also gets
    the statistics and the PDF table.
  * **15:00** (the API's 5-minute round, ``tick``): leads never contacted in a week are closed; every open lead
    is asked about — contacted? then sold / not sold / still going. The answers are buttons; what was sold and
    for how much (and a reason of one's own) is typed (``take_message``). A sale reaches the Boss at once.
  * **/lidlar**: the Director — the statistics, ⏸ / ▶️ and 📄 the PDF table; the admin (Admin Bot) — only on or
    off, no figures (the Director's order of 07.10); a B2B seller — their own open leads.
  * **Stop / start**: ``lead_handout_switch``; no row = stopped, so nothing goes out until someone presses ▶️.
    Stopped = no hand-out and no 15:00 questions; the history and the statistics stay.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local, to_local, today_local
from integrations.org_bot import kpi, leads, names, store
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.org_bot.tone import casual
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = leads.AGENT
log = setup_logging(AGENT)

COMMANDS = ("/lidlar", "/лидлар")
SWITCH_PREFIX = "lsw"  # lsw:on / lsw:off — the Director's (OPS Manager) and the admin's (Admin Bot) buttons
TABLE_PREFIX = "lpdf"  # lpdf:<days> — the PDF table
PREFIXES = (leads.PREFIX, SWITCH_PREFIX, TABLE_PREFIX)
STATS_DAYS = 30


def _bot(run_id: uuid.UUID | None) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def _send(chat_id: int, text: str, run_id: uuid.UUID, keyboard: dict[str, Any] | None = None) -> int | None:
    async with _bot(run_id) as bot:
        ids = await bot.send_message(text, chat_id=str(chat_id), reply_markup=keyboard)
    return ids[-1] if ids else None


async def _answer(callback: dict[str, Any], text: str) -> None:
    async with _bot(None) as bot:
        await bot._answer_callback(callback.get("id", ""), text)  # noqa: SLF001


async def _edit(callback: dict[str, Any], text: str, keyboard: dict[str, Any] | None, run_id: uuid.UUID) -> None:
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not (chat_id and message.get("message_id")):
        return
    async with _bot(run_id) as bot:
        try:
            await bot._edit_message(chat_id=str(chat_id), message_id=message["message_id"], text=text,  # noqa: SLF001
                                    reply_markup=keyboard or {"inline_keyboard": []})
        except TelegramError as exc:
            log.warning("Lead message not edited: {}", type(exc).__name__)


async def _directors() -> list[dict[str, Any]]:
    return await store.active_employees_by_role(DIRECTOR_ROLE)


# ---------------------------------------------------------------- stop / start


async def is_on() -> bool:
    row = await store.lead_switch()
    return bool(row and row["enabled"])


def switch_line(row: dict[str, Any] | None) -> str:
    """«▶️ Ишлаяпти — Директор, 28.10 14:05» — who set it and when (no figures: the admin sees it too)."""
    if row is None:
        return "⏸ Тўхтатилган (ҳали ёқилмаган)"
    head = "▶️ Ишлаяпти" if row["enabled"] else "⏸ Тўхтатилган"
    return f"{head} — {escape(row['set_by'])}, {to_local(row['set_at']):%d.%m %H:%M}"


def switch_button(enabled: bool) -> dict[str, str]:
    return ({"text": "⏸ Тўхтатиш", "callback_data": f"{SWITCH_PREFIX}:off"} if enabled
            else {"text": "▶️ Бошлаш", "callback_data": f"{SWITCH_PREFIX}:on"})


def admin_view(row: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Admin Bot's /lidlar: on or off only — no leads, people or sums (IT sees no company figures)."""
    text = ("🎯 <b>B2B лидлар</b> (ҳар куни 08:00 лид, 15:00 сўров)\n" + switch_line(row)
            + "\n\n<i>Тўхтатилса: лид берилмайди, 15:00 да сўралмайди; статистика сақланади. Директорга хабар берилади.</i>")
    return text, {"inline_keyboard": [[switch_button(bool(row and row["enabled"]))]]}


async def set_switch(enabled: bool, by: str, from_admin: bool, run_id: uuid.UUID) -> None:
    """Turn the tracker on or off; the other side is told (the Director when the admin did it, and back)."""
    await store.set_lead_switch(enabled, by)
    await log_action(agent=AGENT, action="lead_tracker_on" if enabled else "lead_tracker_off", target_system="postgres",
                     status="success", run_id=run_id, mode="write", payload={"by_admin": from_admin})
    word = "ёқди — эртадан 08:00 да лид берилади, 15:00 да сўралади" if enabled else "тўхтатди"
    if from_admin:
        for boss in await _directors():
            try:
                await _send(boss["telegram_user_id"], f"🎯 IT ({escape(by)}) B2B лидлар тарқатишни {word}. /lidlar", run_id)
            except TelegramError as exc:
                log.warning("Director not told about the lead switch: {}", type(exc).__name__)
    else:
        from integrations.org_bot import admin  # local: admin imports ops_manager, which imports this

        await admin.tell_admin(f"🎯 {escape(by)} B2B лидлар тарқатишни {word}.", run_id)


# ---------------------------------------------------------------- 08:00


async def import_sheet(run_id: uuid.UUID) -> None:
    """Copy new rows of the leads sheet into ``leads``; an unreadable sheet leaves what's already there."""
    from integrations.google.sheets_client import SheetsClient, SheetsError

    try:
        async with SheetsClient(agent=AGENT, run_id=run_id) as sheets:
            rows = await sheets.get_values(leads.SHEET_READ_RANGE)
    except SheetsError as exc:
        log.warning("Could not read the leads sheet, handing out from what's already imported: {}", type(exc).__name__)
        return
    added = await store.upsert_leads(leads.rows_to_leads(rows))
    log.info("Leads sheet: {} new lead(s) imported", added)


async def write_brief(ai: Any, lead: dict[str, Any]) -> tuple[str, str]:
    """(summary, location) for the card: kept from before, the AI's, or plain words."""
    if lead.get("brief"):
        return lead["brief"], ""
    parsed = None
    if ai is not None:
        try:
            parsed = leads.parse_brief(await ai.complete(leads.BRIEF_SYSTEM, leads.brief_prompt(lead), json_mode=True))
        except Exception as exc:  # noqa: BLE001 — the card goes out with plain words instead
            log.warning("AI could not summarise lead {}: {}", lead.get("id"), type(exc).__name__)
    return parsed or (leads.fallback_brief(lead), "")


async def morning(run_id: uuid.UUID) -> int:
    """One lead to each B2B sales person at work today, the Boss told who got which. How many were given."""
    from integrations.ai.openrouter_client import OpenRouterClient

    day = now_local().date()
    await import_sheet(run_id)
    people = [e for e in await store.active_employees_by_role(leads.SALES_ROLE) if kpi.works_on(e, day)]
    if not people:
        log.info("No B2B sales person at work today — no leads handed out")
        return 0
    plan = leads.plan_handout(people, await store.free_leads())
    if len(plan) < len(people):
        log.warning("{} lead(s) for {} sales people — the rest get none today", len(plan), len(people))
    if settings.dry_run:
        for person, lead in plan:
            log.info("[dry run] {} ← №{}", names.call_name(person), lead.get("id"))
        return 0

    given: list[tuple[str, dict[str, Any]]] = []
    async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai, _bot(run_id) as bot:
        for person, lead in plan:
            assignment = await store.create_lead_assignment(lead["id"], str(person["id"]), day)
            if assignment is None:
                continue  # already given today (a retried run), or the lead was taken
            brief, location = await write_brief(ai, lead)
            if not lead.get("brief"):
                await store.set_lead_brief(lead["id"], brief)
            try:
                ids = await bot.send_message(leads.card_text(lead, brief, location), chat_id=str(person["telegram_user_id"]))
            except TelegramError as exc:
                log.error("A lead card was not sent: {}", type(exc).__name__)
                continue
            await store.set_lead_assignment_message(str(assignment["id"]), ids[-1] if ids else None)
            given.append((names.person_name(person), lead))
        if given:
            for boss in await _directors():
                try:
                    await bot.send_message(leads.boss_copy_text(given, day), chat_id=str(boss["telegram_user_id"]))
                except TelegramError as exc:
                    log.warning("The Boss's copy was not sent: {}", type(exc).__name__)
    log.info("Handed out {} lead(s) to {} sales people", len(given), len(people))
    return len(given)


# ---------------------------------------------------------------- 15:00


async def checkin(run_id: uuid.UUID) -> int:
    """Close leads nobody contacted in a week, then ask about every open lead of everyone at work. How many asked."""
    day = now_local().date()
    for row in await store.close_uncontacted_leads(day, leads.NOT_CONTACTED_CLOSE_DAYS):
        if row.get("person_status") != "active" or settings.dry_run:
            continue
        try:
            await _send(row["telegram_user_id"], leads.CLOSED_NOTICE.format(
                label=leads.label(row), days=leads.NOT_CONTACTED_CLOSE_DAYS), run_id)
        except TelegramError as exc:
            log.warning("Closed-lead notice not sent: {}", type(exc).__name__)
    open_leads = [r for r in await store.open_leads_to_ask(day) if kpi.works_on(r, day)]
    if settings.dry_run:
        log.info("[dry run] {} open lead(s) would be asked about", len(open_leads))
        return 0
    asked = 0
    async with _bot(run_id) as bot:
        for row in open_leads:
            opened = await store.create_lead_checkin(str(row["assignment_id"]), day, row["telegram_user_id"])
            if opened is None:
                continue  # already asked today (an overlapping round)
            days_open = (day - row["assigned_on"]).days + 1
            cid = str(opened["id"])
            text, keyboard = ((leads.result_question(row, days_open), leads.result_keyboard(cid)) if row.get("contacted_on")
                              else (leads.contact_question(row, days_open), leads.contact_keyboard(cid)))
            try:
                ids = await bot.send_message(text, chat_id=str(row["telegram_user_id"]), reply_markup=keyboard)
            except TelegramError as exc:
                log.error("A lead question was not sent: {}", type(exc).__name__)
                continue
            await store.set_lead_checkin_message(cid, ids[-1] if ids else None)
            asked += 1
    log.info("Asked about {} open lead(s)", asked)
    return asked


async def tick(run_id: uuid.UUID) -> int:
    """The API's 5-minute round: the 15:00 follow-up, when it's on, not frozen and not a day off."""
    if not leads.checkin_due(now_local()) or settings.bots_frozen or not await is_on():
        return 0
    if await store.is_day_off(today_local()):
        return 0
    return await checkin(run_id)


# ---------------------------------------------------------------- the buttons


async def handle_callback(prefix: str, rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str | None:
    if prefix not in PREFIXES:
        return None
    clicker_id = (callback.get("from") or {}).get("id")
    if prefix == leads.PREFIX:
        return await _answer_button(rest, clicker_id, callback, run_id)
    clicker = await store.get_employee_by_telegram_id(clicker_id) if clicker_id else None
    if not (clicker and clicker.get("status") == "active" and clicker.get("role") == DIRECTOR_ROLE):
        await _answer(callback, "Рухсат йўқ")
        return "lead_unauthorized"
    if prefix == TABLE_PREFIX:
        await _answer(callback, "📄")
        return await send_table(clicker_id, run_id, int(rest) if rest.isdigit() else STATS_DAYS)
    enabled = rest == "on"
    await set_switch(enabled, names.person_name(clicker), False, run_id)
    await _answer(callback, "▶️ Ёқилди" if enabled else "⏸ Тўхтатилди")
    text, keyboard = await director_view()
    await _edit(callback, text, keyboard, run_id)
    return "lead_tracker_on" if enabled else "lead_tracker_off"


async def _answer_button(rest: str, clicker_id: int | None, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    parsed = leads.parse_value(rest)
    row = await store.lead_checkin(parsed[0]) if parsed else None
    if row is None or row["telegram_user_id"] != clicker_id:
        await _answer(callback, "Рухсат йўқ" if row else "Номаълум амал")
        return "lead_unauthorized" if row else "lead_unknown"
    if row["answer"] is not None or row["status"] not in ("new", "in_progress"):
        await _answer(callback, "Жавоб аллақачон берилган")
        await _edit(callback, leads.answered_line(row, "жавоб берилган"), None, run_id)
        return "lead_already_answered"
    cid, value = parsed
    day = row["checkin_day"]
    days_open = (day - row["assigned_on"]).days + 1
    if value in ("t", "y"):
        await store.mark_lead_contacted(cid, clicker_id, day - timedelta(days=1 if value == "y" else 0))
        await _answer(callback, "✅")
        await _edit(callback, leads.result_question(row, days_open), leads.result_keyboard(cid), run_id)
        return "lead_contacted"
    if value == "x":
        await _answer(callback, "❌")
        await _edit(callback, leads.reason_question(row), leads.reason_keyboard(cid), run_id)
        return "lead_reason_asked"
    if row.get("contacted_on") is None and value != "n":  # a result means they were contacted
        await store.mark_lead_contacted(cid, clicker_id, day)
    if value == "n":
        if await store.answer_lead_checkin(cid, clicker_id, "not_contacted") is None:
            return await _gone(callback)
        left = leads.NOT_CONTACTED_CLOSE_DAYS - (day - row["assigned_on"]).days
        await _answer(callback, "Эртага яна сўрайман")
        await _edit(callback, leads.answered_line(row, f"ҳали боғланилмаган — эртага яна сўрайман (боғланилмаса, лид "
                                                       f"{max(left, 1)} кундан кейин ёпилади)"), None, run_id)
        return "lead_not_contacted"
    if value == "p":
        if await store.answer_lead_checkin(cid, clicker_id, "in_progress") is None:
            return await _gone(callback)
        await _answer(callback, "⏳")
        await _edit(callback, leads.answered_line(row, "жараёнда — эртага яна сўрайман"), None, run_id)
        return "lead_in_progress"
    if value == "s":
        if await store.answer_lead_checkin(cid, clicker_id, "done") is None:
            return await _gone(callback)
        await _answer(callback, "💰")
        await _edit(callback, leads.answered_line(row, "сотилди"), None, run_id)
        asked = await _send(clicker_id, leads.SALE_QUESTION, run_id)
        await store.ask_lead_question(cid, "sale", asked)
        return "lead_sold"
    reason = leads.REASONS[int(value[1:])]
    if await store.answer_lead_checkin(cid, clicker_id, "dismissed", reason) is None:
        return await _gone(callback)
    await _answer(callback, "OK")
    await _edit(callback, leads.answered_line(row, f"сотилмади — {reason}"), None, run_id)
    if leads.is_other_reason(value):
        asked = await _send(clicker_id, leads.REASON_QUESTION, run_id)
        await store.ask_lead_question(cid, "reason", asked)
    return "lead_not_sold"


async def _gone(callback: dict[str, Any]) -> str:
    await _answer(callback, "Жавоб аллақачон берилган")
    return "lead_already_answered"


# ---------------------------------------------------------------- the typed answers


async def take_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """After «💰 Сотилди» / «бошқа сабаб»: the seller's next message is what was sold and for how much (or the
    reason). None when nothing waits or it's something else — a command, a reply to another message, a question,
    or (for a sale) words with no number in them: those go on to the usual flow."""
    text = (message.get("text") or "").strip()
    if not text or text.startswith("/"):
        return None
    try:
        waiting = await store.pending_lead_question(employee["telegram_user_id"], leads.ANSWER_WINDOW_MINUTES)
    except Exception as exc:  # noqa: BLE001 — the message goes on to the usual flow
        log.warning("Lead question state not read: {}", type(exc).__name__)
        return None
    if waiting is None:
        return None
    replied = (message.get("reply_to_message") or {}).get("message_id")
    if replied and replied != waiting.get("question_message_id"):
        return None
    is_reply = bool(replied)
    if waiting["question"] == "sale":
        amount = leads.parse_amount(text)
        if amount is None:
            if is_reply or any(ch.isdigit() for ch in text):
                await _send(employee["telegram_user_id"], leads.SALE_AGAIN, run_id)
                return "lead_sale_unclear"
            return None
        saved = await store.save_lead_answer(str(waiting["id"]), text, text[:300], *amount)
        if saved is None:
            return None
        await _send(employee["telegram_user_id"], casual(f"раҳмат, ёзиб қўйдим: {leads.money(*amount)}"), run_id)
        await _tell_boss_sale(employee, waiting, text, amount, run_id)
        return "lead_sale_saved"
    if "?" in text and not is_reply:
        return None
    saved = await store.save_lead_answer(str(waiting["id"]), text, f"{leads.REASONS[-1]}: {text[:280]}")
    if saved is None:
        return None
    await _send(employee["telegram_user_id"], casual("раҳмат, ёзиб қўйдим"), run_id)
    return "lead_reason_saved"


async def _tell_boss_sale(employee: dict[str, Any], lead: dict[str, Any], text: str, amount: tuple[float, str],
                          run_id: uuid.UUID) -> None:
    line = (f"💰 <b>Лид {leads.label(lead)} сотилди</b> — {escape(names.person_name(employee))}: "
            f"{leads.money(*amount)}\n<i>{escape(text[:300])}</i>")
    for boss in await _directors():
        try:
            await _send(boss["telegram_user_id"], line, run_id)
        except TelegramError as exc:
            log.warning("The Boss was not told about a sale: {}", type(exc).__name__)


# ---------------------------------------------------------------- the views


async def figures(days: int) -> tuple[dict[str, Any], list[dict[str, Any]], date]:
    """(statistics, rows, since) for the leads given in the last ``days`` days."""
    since = today_local() - timedelta(days=days - 1)
    rows = await store.lead_rows(since)
    return leads.stats(rows, today_local(), await store.average_lead_sale()), rows, since


def period_text(since: date) -> str:
    return f"{since:%d.%m}–{today_local():%d.%m}"


async def director_view(days: int = STATS_DAYS) -> tuple[str, dict[str, Any]]:
    """The Director's /lidlar: the statistics, ⏸ / ▶️ and the PDF table."""
    row = await store.lead_switch()
    s, _rows, since = await figures(days)
    text = leads.stats_text(s, f"{days} кун ({period_text(since)})", bool(row and row["enabled"]))
    text += "\n\n<i>" + switch_line(row) + "</i>"
    buttons = [switch_button(bool(row and row["enabled"]))]
    if s["given"]:
        buttons.append({"text": "📄 Жадвал (PDF)", "callback_data": f"{TABLE_PREFIX}:{days}"})
    return text, {"inline_keyboard": [buttons]}


async def send_director(chat_id: int, run_id: uuid.UUID) -> str:
    text, keyboard = await director_view()
    await _send(chat_id, text, run_id, keyboard)
    return "lead_stats_sent"


def pdf_view(s: dict[str, Any], rows: list[dict[str, Any]], since: date, enabled: bool) -> dict[str, Any]:
    """The PDF template's values."""
    def sums(p: dict[str, Any]) -> str:
        return " + ".join(leads.money(v, c) for c, v in p["sold_sum"].items()) if p["sold_sum"] else "—"

    lost = (f"≈ {leads.money(s['lost_value'])} бой берилди" if s["lost_value"] is not None
            else "бой берилган имконият — биринчи сотувдан кейин")
    if s["our_fault"]:
        lost += f"; {s['our_fault']} таси боғланилмаган ёки кеч"
    return {
        "date": f"{today_local():%d.%m.%Y}", "period": period_text(since), "s": s,
        "state": "тарқатиш ишлаяпти" if enabled else "тарқатиш тўхтатилган",
        "late_rule": f"Кеч — {leads.LATE_AFTER_DAYS + 1}-кундан кейин боғланилган ёки ҳали боғланилмаган",
        "late_short": f"{leads.LATE_AFTER_DAYS + 1}-кундан кейин боғланилган ёки боғланилмаган",
        "sold_sum": sums(s) if s["sold"] else "—", "lost": lost if s["not_sold"] else "—",
        "sellers": [{**p, "avg": str(p["avg_days"]).replace(".", ",") if p["avg_days"] is not None else "—",
                     "sum": sums(p)} for p in s["sellers"]],
        "rows": leads.table_rows(rows, today_local()),
    }


async def send_table(chat_id: int, run_id: uuid.UUID, days: int = STATS_DAYS) -> str:
    """📄 the PDF table: every lead of the period, the figures on top."""
    from integrations.reports import render

    s, rows, since = await figures(days)
    if not rows:
        await _send(chat_id, "🎯 Бу даврда лид берилмаган.", run_id)
        return "lead_table_empty"
    try:
        pdf = render.pdf("leads.html", **pdf_view(s, rows, since, await is_on()))
    except Exception as exc:  # noqa: BLE001 — the text instead
        log.warning("Leads PDF failed: {}", type(exc).__name__)
        await _send(chat_id, leads.stats_text(s, period_text(since), await is_on()), run_id)
        return "lead_table_text"
    async with _bot(run_id) as bot:
        await bot.send_file(pdf, f"lidlar-{today_local():%Y-%m-%d}.pdf", str(chat_id),
                            f"🎯 Лидлар · B2B сотув — {period_text(since)}")
    return "lead_table_sent"


async def weekly(run_id: uuid.UUID) -> int:
    """Mondays 08:00: the statistics and the PDF table to the Boss (when any lead was given in the period)."""
    s, _rows, _since = await figures(STATS_DAYS)
    if not s["given"] or settings.dry_run:
        return 0
    sent = 0
    for boss in await _directors():
        try:
            await send_director(boss["telegram_user_id"], run_id)
            await send_table(boss["telegram_user_id"], run_id)
            sent += 1
        except TelegramError as exc:
            log.warning("Weekly lead statistics not sent: {}", type(exc).__name__)
    return sent


async def send_seller(employee: dict[str, Any], run_id: uuid.UUID) -> str:
    """A B2B seller's /lidlar: their own open leads (number, company, given, contacted)."""
    rows = await store.open_leads_for_employee(str(employee["id"]))
    if not rows:
        await _send(employee["telegram_user_id"], "🎯 Сизда очиқ лид йўқ.", run_id)
        return "lead_own_none"
    lines = ["🎯 <b>Очиқ лидларингиз</b>"]
    for r in rows:
        contacted = f"боғланилди {r['contacted_on']:%d.%m}" if r.get("contacted_on") else "ҳали боғланилмаган"
        lines.append(f"• {leads.label(r)} — берилди {r['assigned_on']:%d.%m}, {contacted}")
    lines.append("\n<i>Ҳар куни 15:00 да ҳар бири бўйича сўрайман.</i>")
    await _send(employee["telegram_user_id"], "\n".join(lines), run_id)
    return "lead_own_sent"
