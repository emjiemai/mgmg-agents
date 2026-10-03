"""OPS Manager Bot — the AI "brain" the Operations Director gives tasks to.

Employees only ever talk to this bot, never to Admin Bot directly — Telegram
cannot be cold-messaged (a bot may only DM a user who has already started a
chat with that specific bot), so the post-approval role picker is sent here,
on the chat the requester already started with THIS bot, not a new one.

Two things a Director's free-text message can trigger, and one thing anyone
registered can trigger:
  - An unregistered sender -> a join request (delegates to ``admin.py``).
  - A registered Director -> AI classification + dispatch, run in the
    background (see ``_dispatch_director_task``) so a slow model call can't
    make Telegram retry the webhook and double-dispatch.
  - Any registered employee tapping "Mark Done" on a task card.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import BackgroundTasks

from integrations.ai.openrouter_client import OpenRouterClient, OpenRouterError
from integrations.common.config import settings
from integrations.common.db import fetch_all, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_money
from integrations.common.timeutil import now_local, today_local
from integrations.google.sheets_client import SheetsClient, SheetsError
from integrations.org_bot import (
    admin, ai_chat, cheer, kpi, kpi_flow, kpi_score, leads, names, permission_flow, report_tools, store, task_picker,
    task_tracker,
)
from integrations.org_bot.tone import casual, is_polite
from integrations.org_bot.prompt import (
    ANSWER_SYSTEM_PROMPT,
    CLASSIFY_SYSTEM_PROMPT,
    GARMIN_CATALOG,
    build_answer_message,
    build_classify_message,
    format_history,
)
from integrations.org_bot.roles import (
    AGENT_LABELS,
    AGENT_SLUGS,
    DIRECTOR_ROLE,
    ROLE_LABELS,
    ROLE_SLUGS,
    ROUTABLE_ROLE_SLUGS,
    role_picker_keyboard,
)
from integrations.telegram.bot import TelegramBot, TelegramError, escape, sanitize_model_html

AGENT = "ops-manager-bot"
log = setup_logging(AGENT)

# Telegram message fields that mean "this is media/a file, not plain text".
# Deliberately excludes sticker/location/contact/poll/video_note: those
# don't support captions in the Bot API, which the Start/Done card edit
# (editMessageCaption) relies on -- keeping this list to types that do.
MEDIA_FIELDS = ("photo", "video", "audio", "voice", "document", "animation")

# An employee asking to change their own name; the admin decides (admin.py).
NAME_CHANGE_COMMANDS = ("/ism", "/исм", "/name")


# ------------------------------------------------------------------ pure logic
# No DB/network here — kept separate and side-effect-free so scripts/selfcheck.py
# can exercise it directly.


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Split a callback's ``data`` into its action prefix and remainder.

    Args:
        data: The raw ``callback_query.data`` string, e.g.
            ``"setrole:it:3fa8..."`` or ``"taskdone:9c21..."``.

    Returns:
        ``(prefix, rest)``, or None if there's no colon to split on.
    """
    if ":" not in data:
        return None
    prefix, rest = data.split(":", 1)
    return prefix, rest


def parse_role_and_request(rest: str) -> tuple[str, str] | None:
    """Split a ``setrole`` callback's remainder into role slug and request id.

    Args:
        rest: Everything after ``"setrole:"``, e.g. ``"it:3fa8..."``.

    Returns:
        ``(role_slug, request_id)``, or None if malformed.
    """
    if ":" not in rest:
        return None
    role_slug, request_id = rest.split(":", 1)
    return role_slug, request_id


def asks_something(text: str) -> bool:
    """Whether a message asks a question (a "?" — Latin or Arabic)."""
    return "?" in text or "؟" in text


def report_message_kind(replied_to_ask: bool, replied_to_task: bool, is_question: bool) -> str:
    """What a message sent while today's report is still owed should be.

    A Reply to the 16:00 ask or the 17:00 reminder is plainly the report. A
    Reply to a task card is a question about that task (for the AI). Any
    other message is confirmed with one tap — the bot never guesses a report
    (the owner, 2026-10-02). ``is_question`` no longer changes the answer;
    it's kept so callers don't change.

    Returns:
        "report", "task_question" or "ask".
    """
    if replied_to_ask:
        return "report"
    if replied_to_task:
        return "task_question"
    return "ask"


def report_or_ai_keyboard(held_id: str) -> dict[str, Any]:
    """The one-tap choice for a message that could be the report or a question for the AI."""
    return {
        "inline_keyboard": [[
            {"text": "ҳа, ҳисобот", "callback_data": f"asrep:{held_id}"},
            {"text": "йўқ, бу савол", "callback_data": f"asai:{held_id}"},
        ]]
    }


def validate_classification(result: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    """Validate the classification model's output against the known vocabulary.

    The code-level backstop that substitutes for a second AI pass here (see
    the module docstring) — a 12-way enum is cheap to check exhaustively in
    code, unlike Lead Agent's open-ended qualification.

    Args:
        result: The parsed JSON the classification call returned.

    Returns:
        ``(target_type, target_role, target_agent)`` if the output is
        internally consistent and every slug is real (``target_type`` is
        "none" or "refused" — refused is the guardrail path, its refusal
        text lives in ``task_summary``, not here) — else None. Callers must
        treat None the same as an explicit "none" (ask the Director to
        clarify), never as a silent default route.
    """
    target_type = result.get("target_type")
    target_role = result.get("target_role")
    target_agent = result.get("target_agent")

    if target_type in ("none", "refused"):
        return target_type, None, None
    if target_type == "employee" and target_role in ROUTABLE_ROLE_SLUGS:
        return "employee", target_role, None
    if target_type == "agent" and target_agent in AGENT_SLUGS:
        return "agent", None, target_agent
    return None


# --------------------------------------------------------------------- entry


async def handle_update(update: dict[str, Any], run_id: uuid.UUID, background: BackgroundTasks) -> str:
    """Dispatch one Telegram update: a button press or a plain message.

    Args:
        update: The full Telegram ``Update`` object.
        run_id: UUID grouping this webhook call's audit rows.
        background: FastAPI background queue, used to run AI classification
            after the webhook has already replied 200.

    Returns:
        A short outcome string, for the webhook route's response body.
    """
    callback = update.get("callback_query")
    if callback:
        return await _handle_callback(callback, run_id, background)

    message = update.get("message")
    if not message:
        return "ignored"

    return await _handle_message(message, run_id, background)


# ---------------------------------------------------------- callback buttons


async def _handle_callback(callback: dict[str, Any], run_id: uuid.UUID, background: BackgroundTasks | None = None) -> str:
    """Route a button press to the role-picker or mark-done handler."""
    data = callback.get("data", "")
    query_id = callback.get("id", "")
    parsed = parse_callback_data(data)
    if parsed is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"

    prefix, rest = parsed
    if prefix == "setrole":
        return await _handle_set_role(rest, callback, run_id)
    if prefix == "taskstart":
        return await _handle_task_start(rest, callback, run_id)
    if prefix == "taskdone":
        return await _handle_task_done(rest, callback, run_id)
    if prefix == "dispatchrole":
        return await _handle_dispatch_role(rest, callback, run_id)
    if prefix == "taskdue":
        return await _handle_task_due(rest, callback, run_id)
    if prefix == "tdt":
        return await _handle_draft_toggle(rest, callback, run_id)
    if prefix == "tds":
        return await _handle_draft_send(rest, callback, run_id)
    if prefix == "tdx":
        return await _handle_draft_cancel(rest, callback, run_id)
    if prefix in ("relayok", "relayno"):
        # Messages to the Director through the bot were removed (2026-10-02);
        # an old "send it" button left in someone's chat does nothing.
        await _answer(query_id, "бот орқали директорга хабар юбориш ўчирилган")
        return "relay_disabled"
    if prefix == "asai":
        return await _handle_held_as_question(rest, callback, run_id, background)
    if prefix == "fp":
        return await _handle_file_purpose(rest, callback, run_id)
    if prefix in ("rpe", "rpd", "rpdy", "rpn"):
        return await _handle_report_button(prefix, rest, callback, run_id)
    if prefix == "asrep":
        return await _handle_save_as_report(rest, callback, run_id)
    if prefix == "cheer":
        return await _handle_cheer_answer(rest, callback, run_id)
    if prefix == "ld":
        return await _handle_lead_status(rest, callback, run_id)
    if prefix == "lc":
        return await _handle_lead_choice(rest, callback, run_id)

    # KPI: goals, results, the Director's 1–5 ratings (kpi_flow.py).
    kpi_outcome = await kpi_flow.handle_callback(prefix, rest, callback, run_id)
    if kpi_outcome is not None:
        return kpi_outcome

    # Written permission buttons (send / cancel / the four SOP decisions).
    permission_outcome = await permission_flow.handle_callback(prefix, rest, callback, run_id)
    if permission_outcome is not None:
        await _answer(query_id, "OK")
        return permission_outcome

    await _answer(query_id, "Номаълум амал")
    return "unrecognized"


def _task_card_text(task_summary: str, raw_message: str | None = None, due_date: date | None = None) -> str:
    """The base text/caption every task card starts with — recomputed (not
    stored) so edits can append a status line without needing to fetch or
    guess the message's current content.

    ``task_summary`` is AI-generated (sanitized to allow its own <b>/<i>
    tags through rather than escaping them into visible literal text — the
    bug that made "<b>" show up as plain text in a real reply). ``raw_message``
    is the Director's own words, shown underneath when it adds anything the
    summary might have compressed away or gotten wrong — a real fallback for
    "the AI's phrasing is confusing", not just a formatting nicety.
    """
    text = f"📋 <b>Янги топшириқ</b>\n\n{sanitize_model_html(task_summary)}"
    if raw_message and raw_message.strip() and raw_message.strip() != task_summary.strip():
        text += f"\n\n<i>Директордан:</i>\n{escape(raw_message.strip())}"
    deadline = task_tracker.deadline_line(due_date, today_local())
    if deadline:
        text += f"\n\n{deadline}"
    return text


def _task_keyboard(task_id: str, started: bool = False) -> dict[str, Any]:
    """The Start+Done (or just Done, once started) inline keyboard."""
    buttons = []
    if not started:
        buttons.append({"text": "▶️ Бошладим", "callback_data": f"taskstart:{task_id}"})
    buttons.append({"text": "✅ Бажардим", "callback_data": f"taskdone:{task_id}"})
    return {"inline_keyboard": [buttons]}


async def _edit_task_card(
    bot: TelegramBot, chat_id: str, message_id: int, has_media: bool, text: str, keyboard: dict[str, Any]
) -> None:
    """Edit a task card in place, using the right Bot API method for its type.

    Telegram requires ``editMessageCaption`` for a media message and
    ``editMessageText`` for a plain one — calling the wrong one fails
    outright, which is why every task row tracks ``has_media``.
    """
    try:
        if has_media:
            await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                "editMessageCaption",
                {"chat_id": chat_id, "message_id": message_id, "caption": text, "parse_mode": "HTML", "reply_markup": keyboard},
                mode="notify",
                target_ref=chat_id,
            )
        else:
            await bot._edit_message(chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard)  # noqa: SLF001
    except TelegramError as err:
        log.warning("Task card edit failed: {}", err)


async def send_role_picker(access_request: dict[str, Any], run_id: uuid.UUID, *, retry: bool = False) -> None:
    """Send an accepted requester their role-picker keyboard.

    Called from ``admin.py`` after the admin accepts the person, and again
    after the admin rejects the role they picked (``retry=True``) —
    deliberately on OPS Manager Bot's token, not Admin Bot's, since the
    requester already started a chat with this bot (see module docstring).

    Args:
        access_request: The ``access_requests`` row.
        run_id: UUID grouping this webhook call's audit rows.
        retry: The admin rejected their previous pick; say so.
    """
    keyboard = role_picker_keyboard(str(access_request["id"]))
    if retry:
        role = ROLE_LABELS.get(access_request.get("requested_role") or "", "")
        text = f"❌ Админ <b>{escape(role)}</b> ролини тасдиқламади. Бошқа ролни танланг:"
    else:
        text = "🎉 Тасдиқландингиз! Ролингизни танланг — танловингизни админ тасдиқлайди."
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot.send_message(text, chat_id=str(access_request["telegram_user_id"]), reply_markup=keyboard)


async def send_registration_confirmed(
    access_request: dict[str, Any], run_id: uuid.UUID, employee: dict[str, Any] | None = None
) -> None:
    """Tell the requester the admin confirmed their role and they're registered.

    Args:
        access_request: The ``access_requests`` row, role now approved.
        run_id: UUID grouping this webhook call's audit rows.
        employee: Their ``employees`` row as just saved. Someone removed and
            registered again keeps the name they gave before — they're not
            asked for it, since their answer would no longer be read as a
            name and would go to the work AI as a question.
    """
    role = ROLE_LABELS.get(access_request["requested_role"], access_request["requested_role"])
    text = f"✅ Сиз <b>{escape(role)}</b> сифатида рўйхатдан ўтдингиз."
    known_name = ((employee or {}).get("full_name") or "").strip()
    # Everyone except the Director gives their real name straight away, so
    # the Director can address them by it (see names.py).
    if access_request["requested_role"] != DIRECTOR_ROLE and known_name:
        text += f"\n\nИсмингиз: <b>{escape(known_name)}</b>. Хато бўлса, /ism деб ёзинг."
    elif access_request["requested_role"] != DIRECTOR_ROLE:
        text += f"\n\n{names.ASK_TEXT}"
        await store.mark_name_asked(access_request["telegram_user_id"])
    await _reply(access_request["telegram_user_id"], run_id, text)


async def _handle_set_role(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Record a role-picker press as a role request for the admin to confirm.

    Nobody is registered here — picking a role only asks for it. The
    employee row is created in ``admin.py`` when the admin accepts the role,
    otherwise anyone past the first approval could pick Operatsion Direktor
    and immediately receive every report and give the bot orders.
    """
    query_id = callback.get("id", "")
    parsed = parse_role_and_request(rest)
    if parsed is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"

    role_slug, request_id = parsed
    if role_slug not in ROLE_SLUGS:
        await _answer(query_id, "Номаълум роль")
        return "unrecognized"

    request = await store.get_access_request(request_id)
    if request is None or request["status"] != "approved":
        await _answer(query_id, "Сўров топилмади ёки тасдиқланмаган")
        return "not_found"

    clicker = callback.get("from", {})
    if clicker.get("id") != request["telegram_user_id"]:
        await _answer(query_id, "Бу сизнинг сўровингиз эмас")
        return "unauthorized"

    updated = await store.request_role(request_id, role_slug)
    if updated is None:
        await _answer(query_id, "Админга аллақачон юборилган")
        return "already_requested"

    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        message = callback.get("message") or {}
        if message.get("message_id") and message.get("chat", {}).get("id"):
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]),
                message_id=message["message_id"],
                text=f"⏳ <b>{escape(ROLE_LABELS[role_slug])}</b> ролини танладингиз. Админ тасдиқлашини кутинг.",
                # Clear the picker: Telegram keeps the old keyboard unless
                # told otherwise, and a second tap must not look possible.
                reply_markup={"inline_keyboard": []},
            )
        await bot._answer_callback(query_id, "Админга юборилди")  # noqa: SLF001

    await admin.request_role_approval(updated, run_id)

    await log_action(
        agent=AGENT,
        action="role_requested",
        target_system="telegram",
        status="success",
        run_id=run_id,
        target_ref=str(request_id),
        mode="write",
        payload={"role": role_slug, "telegram_user_id": request["telegram_user_id"]},
    )
    log.info("Access request {} asked for role {}", str(request_id)[:8], role_slug)
    return "role_requested"


async def _actor_display_name(clicker: dict[str, Any]) -> str:
    """A human-readable name for whoever tapped a button, for Director-facing
    notifications — never the raw numeric Telegram id: it's meaningless to
    the Director and, wrapped in "(@...)", reads as a broken mention rather
    than the harmless fallback it was meant to be.
    """
    telegram_user_id = clicker.get("id")
    if telegram_user_id is not None:
        employee = await store.get_employee_by_telegram_id(telegram_user_id)
        if employee:
            return names.person_name(employee)
    username = clicker.get("username")
    if username:
        return f"@{username}"
    name = " ".join(filter(None, [clicker.get("first_name"), clicker.get("last_name")]))
    return name or "Номаълум ходим"


async def _handle_task_start(task_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Resolve a "Start" button press and notify the Director."""
    query_id = callback.get("id", "")
    clicker = callback.get("from", {})
    started_by = clicker.get("username") or str(clicker.get("id", "unknown"))

    task = await store.mark_task_started(task_id, started_by)
    if task is None:
        await _answer(query_id, "Аллақачон бошланган ёки бажарилган")
        return "already_started"

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            text = (
                _task_card_text(task["task_summary"], task.get("raw_message"), task.get("due_date"))
                + "\n\n▶️ Бошланди"
            )
            keyboard = _task_keyboard(str(task["id"]), started=True)
            await _edit_task_card(
                bot, str(message["chat"]["id"]), message["message_id"], bool(task.get("has_media")), text, keyboard
            )
        await bot._answer_callback(query_id, "Бошланди")  # noqa: SLF001

    try:
        actor = await _actor_display_name(clicker)
        await _reply(
            task["director_telegram_user_id"],
            run_id,
            f"▶️ Бошланди: {sanitize_model_html(task['task_summary'])}\n— {escape(actor)}",
        )
    except TelegramError as exc:
        log.warning("Could not notify Director that a task started: {}", exc)

    return "started"


async def _handle_task_done(task_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Resolve a "Mark Done" button press and notify the Director."""
    query_id = callback.get("id", "")
    clicker = callback.get("from", {})
    completed_by = clicker.get("username") or str(clicker.get("id", "unknown"))

    task = await store.mark_task_done(task_id, completed_by)
    if task is None:
        await _answer(query_id, "Аллақачон бажарилган")
        return "already_done"

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            text = (
                _task_card_text(task["task_summary"], task.get("raw_message"), task.get("due_date"))
                + "\n\n✅ Бажарилди"
            )
            await _edit_task_card(
                bot, str(message["chat"]["id"]), message["message_id"], bool(task.get("has_media")), text,
                {"inline_keyboard": []},
            )
        await bot._answer_callback(query_id, "Бажарилди")  # noqa: SLF001

    try:
        actor = await _actor_display_name(clicker)
        await _reply(
            task["director_telegram_user_id"],
            run_id,
            f"✅ Бажарилди: {sanitize_model_html(task['task_summary'])}\n— {escape(actor)}",
        )
    except TelegramError as exc:
        log.warning("Could not notify Director of task completion: {}", exc)

    return "done"


async def _handle_cheer_answer(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """A tap on a team-cheer answer: its warm reply as a brief pop-up, and the buttons go.

    No new message — the reply is the tap's own pop-up, so answering never
    adds to the chat. One answer per person per message; the tap is not
    reported to anyone (see ``cheer.py``).
    """
    query_id = callback.get("id", "")
    parsed = cheer.parse_callback(rest)
    clicker_id = (callback.get("from") or {}).get("id")
    if parsed is None or clicker_id is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    cheer_id, index = parsed
    row = await store.answer_cheer(cheer_id, clicker_id, index)
    options = (row or {}).get("options") or []
    if row is None or not 0 <= index < len(options):
        await _answer(query_id, "раҳмат, жавобингизни олдим")
        return "cheer_already_answered"
    await _answer(query_id, options[index]["reply"])
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
            chat_id=str(clicker_id),
            message_id=row["message_id"],
            text=row["text"],
            reply_markup={"inline_keyboard": []},
        )
    return "cheer_answered"


async def _edit_lead_message(chat_id: int, message_id: int | None, text: str, keyboard: dict[str, Any], run_id: uuid.UUID) -> None:
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
            chat_id=str(chat_id), message_id=message_id, text=text, reply_markup=keyboard
        )


async def _ask_lead_question(telegram_user_id: int, checkin_id: str, question: str, run_id: uuid.UUID) -> None:
    """Send the one follow-up after a tap and remember it, so the typed answer lands on this lead."""
    ids = await _reply(telegram_user_id, run_id, casual(question, "🙂"))
    await store.ask_lead_question(checkin_id, question, ids[-1] if ids else None)


async def _handle_lead_status(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """A 15:00 lead tap: жараёнда → "next step?"; рад этилди / бажарилди → reason/result buttons."""
    query_id = callback.get("id", "")
    parsed = leads.parse_callback(rest)
    clicker_id = (callback.get("from") or {}).get("id")
    if parsed is None or parsed[1] not in leads.STATUSES or clicker_id is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    checkin_id, letter = parsed
    status = leads.STATUSES[letter][0]
    row = await store.answer_lead_checkin(checkin_id, clicker_id, status)
    if row is None:
        await _answer(query_id, "бу лид бўйича жавобингиз олинган")
        return "lead_already_answered"
    await _answer(query_id, "раҳмат")
    if status == "in_progress":
        await _edit_lead_message(clicker_id, row["message_id"], leads.status_line(row, status),
                                 {"inline_keyboard": []}, run_id)
        await _ask_lead_question(clicker_id, checkin_id, leads.IN_PROGRESS_QUESTION, run_id)
    else:
        await _edit_lead_message(clicker_id, row["message_id"], leads.choice_question(row, status),
                                 leads.choice_keyboard(checkin_id, status), run_id)
    return f"lead_{status}"


async def _handle_lead_choice(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """The reason a lead was dismissed, or what came of it; "other" asks them to write it."""
    query_id = callback.get("id", "")
    parsed = leads.parse_callback(rest)
    choice = leads.parse_choice(parsed[1]) if parsed else None
    clicker_id = (callback.get("from") or {}).get("id")
    if parsed is None or choice is None or clicker_id is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    checkin_id = parsed[0]
    status, index, outcome = choice
    row = await store.choose_lead_outcome(checkin_id, clicker_id, outcome)
    if row is None:
        await _answer(query_id, "бу лид бўйича жавобингиз олинган")
        return "lead_already_answered"
    await _answer(query_id, "раҳмат, ёзиб қўйдим")
    await _edit_lead_message(clicker_id, row["message_id"], leads.status_line(row, status, outcome),
                             {"inline_keyboard": []}, run_id)
    if leads.is_other(status, index):
        await _ask_lead_question(clicker_id, checkin_id, leads.OTHER_QUESTION, run_id)
    return "lead_outcome"


async def _answer(query_id: str, text: str) -> None:
    """Acknowledge a button press on OPS Manager Bot's own token."""
    if not query_id:
        return
    async with TelegramBot(
        agent=AGENT, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._answer_callback(query_id, text)  # noqa: SLF001


# --------------------------------------------------------------- text messages


async def _handle_message(message: dict[str, Any], run_id: uuid.UUID, background: BackgroundTasks) -> str:
    """Route an incoming plain-text message by sender registration/role."""
    sender = message.get("from", {})
    telegram_user_id = sender.get("id")
    if telegram_user_id is None:
        return "ignored"

    employee = await store.get_employee_by_telegram_id(telegram_user_id)
    if employee is None or employee["status"] != "active":
        return await _handle_unregistered_sender(telegram_user_id, sender, run_id)

    # Every employee's real name first (names.py): until the bot has it,
    # nothing they send is relayed, filed or read as a report.
    if names.needs_name(employee):
        return await names.collect_name(employee, message, run_id)

    if (message.get("text") or "").strip().lower() in NAME_CHANGE_COMMANDS:
        return await _request_name_change(employee, run_id)

    # Written permission requests (EMJ-SOP-ADM-01) come before everything
    # else, for the Director too: an answer to the form's own question, or an
    # approver's conditions, must not be re-read as a task or a task update.
    permission_outcome = await permission_flow.handle_message(employee, message, run_id)
    if permission_outcome is not None:
        return permission_outcome

    # KPI commands and answers (/maqsad, /natija, /kpi, /baho, a goal's text,
    # a result's number) — before reports and task routing read them.
    kpi_outcome = await kpi_flow.handle_message(employee, message, run_id)
    if kpi_outcome is not None:
        return kpi_outcome

    if employee["role"] != DIRECTOR_ROLE:
        return await _handle_employee_message(employee, message, run_id, background)

    has_media = any(message.get(field) for field in MEDIA_FIELDS)
    text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()

    if not has_media and not text:
        return "ignored"

    await _show_typing(telegram_user_id, run_id)

    if has_media:
        background.add_task(_dispatch_director_media, telegram_user_id, message.get("message_id"), caption, run_id)
    else:
        background.add_task(_dispatch_director_task, telegram_user_id, text, message.get("message_id"), run_id)
    return "queued"


async def _route_to_ai(
    employee: dict[str, Any], text: str, task: dict[str, Any] | None, run_id: uuid.UUID,
    background: BackgroundTasks | None,
) -> str:
    """An employee's message about their work goes to the AI (``ai_chat.py``), never to a person."""
    telegram_user_id = employee["telegram_user_id"]
    if employee.get("ai_chat_off"):
        await _reply(telegram_user_id, run_id, ai_chat.off_text())
        return "ai_off"
    await _show_typing(telegram_user_id, run_id)
    if background is not None:
        background.add_task(_answer_ai_chat, employee, text, task, run_id)
    else:
        await _answer_ai_chat(employee, text, task, run_id)
    return "ai_chat"


async def _answer_ai_chat(employee: dict[str, Any], text: str, task: dict[str, Any] | None, run_id: uuid.UUID) -> None:
    """Answer one message — with the person's own tasks, leads and duties, nothing of anyone else's."""
    telegram_user_id = employee["telegram_user_id"]
    try:
        if await store.ai_questions_today(telegram_user_id) >= ai_chat.DAILY_LIMIT:
            await _reply(telegram_user_id, run_id, ai_chat.limit_text())
            return
        history = await store.recent_ai_turns(telegram_user_id)
        tasks = await store.open_tasks_for_employee(telegram_user_id)
        own_leads = await store.open_leads_for_employee(str(employee["id"])) if employee.get("id") else []
        await store.log_ai_turn(telegram_user_id, "employee", text)
        async with OpenRouterClient(
            agent=ai_chat.AGENT,
            run_id=run_id,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            raw = await ai.complete(
                ai_chat.system_prompt(employee, ai_chat.work_context(employee, tasks, own_leads, today_local())),
                ai_chat.user_prompt(history, text, task),
            )
        answer = ai_chat.clean_answer(raw)
        await store.log_ai_turn(telegram_user_id, "assistant", answer)
        await _reply(telegram_user_id, run_id, answer)
    except Exception as exc:  # noqa: BLE001 — a background failure must be told, not raised
        log.error("AI chat failed for {}: {}", telegram_user_id, exc)
        try:
            await _reply(telegram_user_id, run_id, ai_chat.error_text())
        except Exception:  # noqa: BLE001
            pass


async def _request_name_change(employee: dict[str, Any], run_id: uuid.UUID) -> str:
    """Pass an employee's /ism to the admin — a name on documents isn't self-service."""
    telegram_user_id = employee["telegram_user_id"]
    requested = await store.request_name_change(telegram_user_id)
    if requested is None:
        await _reply(telegram_user_id, run_id, "⏳ Исм ўзгартириш сўровингиз аллақачон админга юборилган.")
        return "name_change_pending"
    await admin.request_name_change(requested, run_id)
    await _reply(
        telegram_user_id,
        run_id,
        "📨 Исм ўзгартириш сўровингиз админга юборилди. Рухсат берилса, бот исмингизни сўрайди.",
    )
    return "name_change_requested"


async def _handle_unregistered_sender(telegram_user_id: int, sender: dict[str, Any], run_id: uuid.UUID) -> str:
    """Start or acknowledge a join request for a never-seen sender."""
    # Accepted by the admin but not registered yet (role to pick, or picked
    # and waiting): whatever they type — often their name — is not a new
    # join request. Point them at the one step left.
    halfway = await store.registration_in_progress(telegram_user_id)
    if halfway is not None:
        if halfway.get("role_status") == "pending":
            role = ROLE_LABELS.get(halfway.get("requested_role") or "", "")
            await _reply(
                telegram_user_id,
                run_id,
                f"⏳ <b>{escape(role)}</b> ролини танладингиз, админ тасдиқлашини кутинг. "
                "Тасдиқлангач, исмингизни сўрайман.",
            )
            return "role_pending"
        await _reply(
            telegram_user_id,
            run_id,
            "👇 Аввал ролингизни танланг — танловингизни админ тасдиқлайди. Исмингизни ундан кейин сўрайман.",
            reply_markup=role_picker_keyboard(str(halfway["id"])),
        )
        return "role_picker_resent"

    existing = await store.get_pending_access_request(telegram_user_id)
    if existing is not None:
        await _reply(
            telegram_user_id,
            run_id,
            "⏳ Сўровингиз ҳали кўриб чиқилмоқда.",
        )
        return "already_pending"

    display_name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])) or str(
        telegram_user_id
    )
    outcome = await admin.request_access(
        telegram_user_id=telegram_user_id,
        telegram_username=sender.get("username"),
        display_name=display_name,
        run_id=run_id,
    )
    await _reply(
        telegram_user_id,
        run_id,
        "📨 Сўровингиз админга юборилди. Тасдиқлангандан кейин хабар берамиз.",
    )
    return outcome


async def _reply(
    telegram_user_id: int, run_id: uuid.UUID, text: str, reply_markup: dict[str, Any] | None = None
) -> list[int]:
    """Send a message to one user via OPS Manager Bot's own token.

    Returns:
        The Telegram message id(s) of what was sent -- callers that need to
        track a message for later reply-routing use this; callers that don't
        care can ignore the return value.
    """
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        return await bot.send_message(text, chat_id=str(telegram_user_id), reply_markup=reply_markup)


async def _show_typing(telegram_user_id: int, run_id: uuid.UUID) -> None:
    """Show Telegram's native typing indicator instead of a canned text ack —
    a real classification/answer call takes a few seconds; this signals
    "working on it" without leaving a repetitive message in the chat."""
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot.send_chat_action(chat_id=str(telegram_user_id))


async def _reply_and_log(director_telegram_user_id: int, run_id: uuid.UUID, text: str) -> None:
    """Send the Director a substantive reply and remember it as conversation
    memory — used for the actual outcome of a request, not UX filler like the
    "got it, routing..." ack (logging every ack would bury real content in noise)."""
    await _reply(director_telegram_user_id, run_id, text)
    await store.log_conversation_turn(director_telegram_user_id, "bot", text)


# ---------------------------------------------------------------- daily reports

REPORT_CHECK_SYSTEM = """You read an employee's end-of-day work report. The \
Director wants reports he can check — a little accuracy, not an essay. Decide \
whether it needs ONE short follow-up question.

Ask when:
- it says nothing concrete ("ok", "ishladim", "hammasi yaxshi", an emoji); or
- it only lists general activities and the ones that matter have no result — \
e.g. "answered calls, consulted clients, sales" with no count, amount, client \
or outcome.
Do NOT ask when the main activities already have something checkable: a number \
(calls, clients, orders, an amount), a client or company name, a document or \
task name, or a result ("3 ta KP yubordim", "Rich Home bilan shartnoma \
imzolandi", "омборни санадим, 12 та камчилик").
Routine chores (cleaning, tidying shelves or displays) never need numbers — \
ignore them. When in doubt, do not ask.

If you ask: ONE short, warm question in Uzbek, in Cyrillic script, all \
lowercase, no emoji, like a colleague asking — about the 1–2 most important \
vague items from THEIR report, asking for a number or a result, fitted to their \
role. Example for a sales person who wrote "answered calls, consulted clients, \
sales": "нечта қўнғироққа жавоб бердингиз ва бугун қанча сотув бўлди?". Never \
scold, never ask about chores, never more than one question. Always the \
respectful "сиз" form — never "сен" or its verb forms (-сан, -санг, -динг); \
be especially courteous with women.

Return ONLY JSON: {"ask": false} or {"ask": true, "follow_up": "<the question>"}."""

# A report this long is detailed by any measure — don't spend an AI call on it.
_REPORT_CHECK_MAX_LEN = 3000


async def _report_follow_up(text: str, role: str, run_id: uuid.UUID) -> str | None:
    """One short question when a report is vague or has no checkable result, else None.

    Never blocks the report: on any AI failure the report simply stands.
    """
    if len(text) > _REPORT_CHECK_MAX_LEN:
        return None
    try:
        async with OpenRouterClient(
            agent=AGENT,
            run_id=run_id,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            verdict = await ai.complete_json(
                REPORT_CHECK_SYSTEM, f"Role: {ROLE_LABELS.get(role, role)}\nReport:\n{text}"
            )
    except OpenRouterError as exc:
        log.warning("Report check unavailable, accepting as is: {}", exc)
        return None
    if verdict.get("ask") is not True:
        return None
    default = "бугун аниқ нималарни бажардингиз, натижасини ёзиб берасизми?"
    question = casual(str(verdict.get("follow_up") or "").strip() or default)
    return question if is_polite(question) else default


async def _try_daily_report(
    employee: dict[str, Any], text: str, reply_to_message_id: int | None, run_id: uuid.UUID
) -> str | None:
    """Handle this message as today's daily report, if that's what it is.

    Args:
        employee: The sending employee's row.
        text: Their message.
        reply_to_message_id: What they replied to, if anything.
        run_id: UUID grouping this webhook call's audit rows.

    Returns:
        An outcome string when the message WAS the report (or the one-tap
        question about it), or None to fall through to the AI — including
        when late numbers were merged into an already-submitted report.
    """
    day = today_local()
    metrics_def = kpi.metrics_for_role(employee["role"])
    pending = await store.pending_report(employee["telegram_user_id"], day)

    if pending is None:
        # The answer to the one follow-up question on a vague report. It is
        # added to the report and the conversation ends there — no further
        # questions, whatever it says.
        # A question asked in that window is a question, not the answer.
        followup = None if asks_something(text) else await store.open_report_followup(employee["telegram_user_id"], day)
        if followup is not None:
            if await store.answer_report_followup(str(followup["id"]), text) is not None:
                fresh = kpi.parse_metrics(text, metrics_def) if metrics_def else {}
                if fresh:
                    await store.merge_report_metrics(str(followup["id"]), fresh)
                await _reply(employee["telegram_user_id"], run_id, casual("раҳмат каттакон, ҳисоботингизга қўшиб қўйдим", "😊"))
                return "daily_report_followup"

        # A reply to an earlier day's ask: reports close at midnight, so say
        # so instead of relaying a stale report to the Director.
        if reply_to_message_id:
            expired = await store.expired_report_for_prompt(employee["telegram_user_id"], reply_to_message_id)
            if expired is not None:
                await _reply(
                    employee["telegram_user_id"],
                    run_id,
                    casual(
                        f"{expired['report_date'].strftime('%d.%m')} кунги ҳисоботнинг вақти ўтиб кетибди, "
                        "ҳисоботлар шу куннинг ўзида соат 24:00 гача олинади",
                        "🙂",
                    ),
                )
                return "daily_report_expired"

        # Numbers arriving a minute after a report already sent in words:
        # merge them so the scorecard is complete, and still fall through so
        # the message itself gets its answer.
        if metrics_def:
            submitted = await store.submitted_report_today(employee["telegram_user_id"], day)
            if submitted is not None:
                already = submitted.get("metrics") or {}
                fresh = {k: v for k, v in kpi.parse_metrics(text, metrics_def).items() if k not in already}
                if fresh:
                    await store.merge_report_metrics(str(submitted["id"]), fresh)
                    log.info("Merged late numbers {} into {}'s report", fresh, employee["display_name"])
        return None

    # No one has to reply to anything (2026-09-25): a reply to the 16:00 ask
    # or the 17:00 reminder is the report, a reply to a task card is a
    # question about it (for the AI), and a plain message is the report —
    # unless it asks something, when the bot asks with one tap.
    telegram_user_id = employee["telegram_user_id"]
    replied_to_ask = reply_to_message_id is not None and reply_to_message_id in (
        pending.get("prompt_message_id"),
        pending.get("reminder_message_id"),
    )
    replied_to_task = (
        not replied_to_ask
        and reply_to_message_id is not None
        and await store.find_task_by_message_id(reply_to_message_id, telegram_user_id) is not None
    )
    kind = report_message_kind(replied_to_ask, replied_to_task, asks_something(text))
    if kind == "task_question":
        return None
    if kind == "ask":
        return await _ask_report_or_ai(employee, text, run_id)
    # None means Telegram delivered this same report twice and the first copy
    # already saved it — stop here rather than offer the copy to the Director.
    return await _save_daily_report(employee, pending, text, run_id) or "daily_report_duplicate"


async def _save_daily_report(
    employee: dict[str, Any], pending: dict[str, Any], text: str, run_id: uuid.UUID
) -> str | None:
    """Store today's report and answer the employee.

    Returns:
        The outcome, or None if it had already been saved (a duplicate
        webhook delivery got there first).
    """
    day = today_local()
    metrics_def = kpi.metrics_for_role(employee["role"])
    values = kpi.parse_metrics(text, metrics_def)
    tasks_done = await store.count_tasks_completed(str(employee["id"]), day)
    saved = await store.save_report(
        report_id=str(pending["id"]), content=text, metrics=values, tasks_done=tasks_done
    )
    if saved is None:
        return None  # a duplicate webhook delivery got here first

    # Deliberately NOT forwarded to the Director (the business's decision):
    # the Director only hears who didn't report, in the next 08:00 brief.
    # The report stays stored and queryable through the xodimlar_kpi agent.

    # A vague report ("ok", "ishladim") or one without any checkable result
    # ("answered calls, consulted clients, sales") is already saved — the
    # employee has reported — but gets ONE short question asking for a number
    # or result. Whatever comes back is added to the report; nothing more is
    # asked (2026-09-28: "a little accuracy is enough").
    follow_up = await _report_follow_up(text, employee["role"], run_id)
    if follow_up:
        await store.mark_report_followup(str(saved["id"]))
        await _reply(employee["telegram_user_id"], run_id, casual(escape(follow_up), "🙂"))
        return "daily_report_weak"

    ack = casual("раҳмат каттакон, ҳисоботингиз қабул қилинди, ўзгартириш учун /hisobot, чарчаманг", "😊")
    missing = kpi.missing_metrics(values, metrics_def)
    if missing:
        ack = casual(
            f"раҳмат, ҳисоботингиз қабул қилинди, {escape(', '.join(missing))} рақамларини ҳам ёзсангиз қўшиб қўяман",
            "🙏",
        )
    await _reply(employee["telegram_user_id"], run_id, ack)
    return "daily_report"


async def _ask_report_or_ai(employee: dict[str, Any], text: str, run_id: uuid.UUID) -> str:
    """Ask whether a message is today's report or a question for the AI."""
    held = await store.create_pending_relay(employee["telegram_user_id"], text, None)
    await _reply(
        employee["telegram_user_id"],
        run_id,
        casual("бу бугунги ҳисоботингизми?", "🙂"),
        report_or_ai_keyboard(str(held["id"])),
    )
    return "report_or_ai_asked"


async def _ask_file_purpose(employee: dict[str, Any], message: dict[str, Any], kind: str, run_id: uuid.UUID) -> str:
    """Hold a file and ask what it's for; "today's report" only when a report was asked today."""
    telegram_user_id = employee["telegram_user_id"]
    held = await store.create_employee_file(telegram_user_id, message["message_id"], kind, message.get("caption"))
    report = await store.report_for_day(telegram_user_id, today_local())
    await _reply(
        telegram_user_id, run_id, report_tools.ask_purpose_text(kind),
        report_tools.purpose_keyboard(str(held["id"]), report is not None),
    )
    return "file_purpose_asked"


async def _handle_file_purpose(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Today's report / to the Director → forward the file as it is; cancel → nothing."""
    query_id = callback.get("id", "")
    file_id, _, letter = rest.rpartition(":")
    purpose = report_tools.PURPOSES.get(letter)
    clicker_id = (callback.get("from") or {}).get("id")
    if not file_id or purpose is None or clicker_id is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    held = await store.resolve_employee_file(file_id, clicker_id, purpose)
    if held is None:
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "file_already_resolved"
    await _answer(query_id, "OK")
    message = callback.get("message") or {}

    async def settle(text: str) -> None:
        if message.get("message_id"):
            await _edit_lead_message(clicker_id, message["message_id"], text, {"inline_keyboard": []}, run_id)

    if purpose == "cancelled":
        await settle(report_tools.cancelled_text())
        return "file_cancelled"
    employee = await store.get_employee_by_telegram_id(clicker_id)
    if employee is None or employee["status"] != "active":
        return "unknown_employee"
    if purpose == "report" and await store.attach_media_to_report(clicker_id, today_local(), held.get("caption")) is None:
        await settle(report_tools.no_report_today_text())
        return "file_no_report"

    caption = report_tools.director_caption(
        names.person_name(employee), ROLE_LABELS.get(employee["role"], employee["role"]), purpose, held.get("caption")
    )
    delivered = 0
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for director in await store.active_employees_by_role(DIRECTOR_ROLE):
            try:
                result = await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                    "copyMessage",
                    {"chat_id": str(director["telegram_user_id"]), "from_chat_id": str(clicker_id),
                     "message_id": held["message_id"], "caption": caption, "parse_mode": "HTML"},
                    mode="notify", target_ref=str(director["telegram_user_id"]),
                )
                delivered += 1 if result else 0
            except TelegramError as exc:
                log.error("Could not forward a file to the Director: {}", exc)
    await store.set_employee_file_sent(file_id, delivered)
    await settle(report_tools.sent_text(purpose, delivered > 0))
    return f"file_{purpose}"


async def _show_today_report(employee: dict[str, Any], run_id: uuid.UUID) -> str:
    """/hisobot: today's report with ✏️ / 🗑, or how to send it."""
    report = await store.report_for_day(employee["telegram_user_id"], today_local())
    if report is None:
        await _reply(employee["telegram_user_id"], run_id, report_tools.no_report_today_text())
        return "report_not_asked"
    text, keyboard = report_tools.report_card(report)
    await _reply(employee["telegram_user_id"], run_id, text, keyboard)
    return "report_shown"


async def _handle_report_button(prefix: str, report_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """✏️ / 🗑 on today's report (only today's, only one's own)."""
    query_id = callback.get("id", "")
    clicker_id = (callback.get("from") or {}).get("id")
    message = callback.get("message") or {}
    today = today_local()
    report = await store.report_for_day(clicker_id, today) if clicker_id else None
    if report is None or str(report["id"]) != report_id or report["status"] != "submitted":
        await _answer(query_id, "фақат бугунги ҳисоботни ўзгартириш мумкин")
        return "report_not_today"
    await _answer(query_id, "OK")
    if prefix == "rpe":
        await store.start_report_edit(report_id, clicker_id, today)
        await _edit_lead_message(clicker_id, message.get("message_id"), report_tools.edit_prompt_text(),
                                 {"inline_keyboard": []}, run_id)
        return "report_edit_started"
    if prefix == "rpd":
        await _edit_lead_message(clicker_id, message.get("message_id"), report_tools.delete_question_text(),
                                 report_tools.delete_confirm_keyboard(report_id), run_id)
        return "report_delete_asked"
    if prefix == "rpn":
        text, keyboard = report_tools.report_card(report)
        await _edit_lead_message(clicker_id, message.get("message_id"), text, keyboard or {"inline_keyboard": []}, run_id)
        return "report_delete_kept"
    await store.delete_report(report_id, clicker_id, today)
    await _edit_lead_message(clicker_id, message.get("message_id"), report_tools.deleted_text(),
                             {"inline_keyboard": []}, run_id)
    return "report_deleted"


async def _handle_held_as_question(
    held_id: str, callback: dict[str, Any], run_id: uuid.UUID, background: BackgroundTasks | None
) -> str:
    """"йўқ, бу савол": the held message goes to the AI instead of the report."""
    query_id = callback.get("id", "")
    clicker_id = (callback.get("from") or {}).get("id")
    held = await store.resolve_pending_relay(held_id, clicker_id, "ai")
    if held is None:
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "held_already_resolved"
    employee = await store.get_employee_by_telegram_id(clicker_id)
    await _answer(query_id, "OK")
    message = callback.get("message") or {}
    if message.get("message_id") and message.get("chat", {}).get("id"):
        async with TelegramBot(
            agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
        ) as bot:
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]), message_id=message["message_id"],
                text=casual("савол сифатида қабул қилдим", "🙂"), reply_markup={"inline_keyboard": []},
            )
    if employee is None or employee["status"] != "active":
        return "unknown_employee"
    return await _route_to_ai(employee, held["message_text"], None, run_id, background)


async def _handle_save_as_report(relay_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """The employee said the held message is their daily report."""
    query_id = callback.get("id", "")
    clicker_id = callback.get("from", {}).get("id")
    relay = await store.resolve_pending_relay(relay_id, clicker_id, "report")
    if relay is None:
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "relay_already_resolved"

    day = today_local()
    employee = await store.get_employee_by_telegram_id(clicker_id)
    pending = await store.pending_report(clicker_id, day)
    outcome = None
    if employee is not None and pending is not None:
        outcome = await _save_daily_report(employee, pending, relay["message_text"], run_id)
    if outcome is not None:
        status_line = casual("ҳисобот сифатида сақладим, раҳмат каттакон", "😊")
    elif await store.submitted_report_today(clicker_id, day) is not None:
        status_line = casual("бугунги ҳисоботингиз аллақачон қабул қилинган", "🙂")
        outcome = "report_already_submitted"
    else:
        status_line = casual("ҳисоботнинг вақти ўтиб кетибди, ҳисоботлар шу куннинг ўзида олинади", "🙂")
        outcome = "report_window_closed"

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]),
                message_id=message["message_id"],
                text=status_line,
                reply_markup={"inline_keyboard": []},
            )
        await bot._answer_callback(query_id, "OK")  # noqa: SLF001
    return outcome


# -------------------------------------------------------------- employee updates


async def _handle_employee_message(
    employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID, background: BackgroundTasks | None = None
) -> str:
    """An employee's free-text message: about their work, answered by the AI.

    2026-10-02, from the owner: OPS Manager Bot is for work only. Employees
    don't message the Director or each other through it; what they write
    goes, in order, to: a cheer reply, a lead's follow-up, today's report —
    and otherwise to the AI, which answers about their own work and tasks
    (``ai_chat.py``). Permission requests, KPI commands and task buttons have
    their own flows and never reach this point.
    """
    telegram_user_id = employee["telegram_user_id"]
    # A photo, video or file: ask what it's for — it goes to the Director as
    # it is, never to the AI (report_tools.py).
    kind = report_tools.file_kind(message)
    if kind is not None:
        return await _ask_file_purpose(employee, message, kind, run_id)
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return "ignored"
    if text.lower() in report_tools.REPORT_COMMANDS:
        return await _show_today_report(employee, run_id)
    editing = await store.report_being_edited(telegram_user_id, today_local(), report_tools.EDIT_MINUTES)
    if editing is not None and not text.startswith("/"):
        await store.replace_report(str(editing["id"]), text)
        await _reply(telegram_user_id, run_id, report_tools.edited_text())
        return "report_edited"
    if text.lower() == ai_chat.COMMAND:
        await _reply(telegram_user_id, run_id, ai_chat.hint_text())
        return "ai_hint"
    if text.startswith("/"):
        return "ignored"  # an unknown command: not a question

    reply_to = message.get("reply_to_message") or {}
    # A typed answer to a team-cheer message ("how was your day?") is chat,
    # not a report and not a question (cheer.py).
    if reply_to.get("message_id") and await store.cheer_delivery_for_message(telegram_user_id, reply_to["message_id"]):
        await _reply(telegram_user_id, run_id, cheer.text_reply(reply_to["message_id"]))
        return "cheer_reply"

    # The answer to a lead's follow-up ("кейинги қадамингиз нима?"): a Reply
    # to it, or the next message soon after — unless the 16:00 report ask
    # came in between, in which case this is more likely the report.
    question = None
    if reply_to.get("message_id"):
        question = await store.lead_question_by_message(telegram_user_id, reply_to["message_id"])
    # A message that asks something isn't taken as the answer just for
    # arriving soon after — only a Reply to the question is (it goes to the AI).
    if question is None and not asks_something(text):
        question = await store.pending_lead_question(telegram_user_id, leads.ANSWER_WINDOW_MINUTES)
        if question is not None:
            report = await store.pending_report(telegram_user_id, today_local())
            if report is not None and report.get("asked_at") and report["asked_at"] > question["question_asked_at"]:
                question = None
    if question is not None and await store.save_lead_note(str(question["id"]), text) is not None:
        await _reply(telegram_user_id, run_id, casual("раҳмат каттакон, ёзиб қўйдим", "😊"))
        return "lead_note"

    outcome = await _try_daily_report(employee, text, reply_to.get("message_id"), run_id)
    if outcome is not None:
        return outcome

    task = None
    if reply_to.get("message_id"):
        task = await store.find_task_by_message_id(reply_to["message_id"], telegram_user_id)
    return await _route_to_ai(employee, text, task, run_id, background)


def _plain(text: str | None) -> str:
    """AI-written text without its <b>/<i> tags, on one line — for previews."""
    return " ".join(re.sub(r"<[^>]+>", "", text or "").split())


async def _dispatch_director_task(
    director_telegram_user_id: int, raw_message: str, source_message_id: int | None, run_id: uuid.UUID
) -> None:
    """Classify a Director's message and dispatch it — runs in the background.

    Wrapped end-to-end in try/except: a failure here must be logged and
    best-effort reported to the Director, never raised, since nothing awaits
    a ``BackgroundTasks`` callback's result.

    Args:
        director_telegram_user_id: The Director's Telegram numeric id.
        raw_message: Their original free-text message.
        source_message_id: Telegram message id, part of the dedupe key
            against duplicate webhook delivery re-dispatching the same task.
        run_id: UUID grouping this webhook call's audit rows.
    """
    await store.log_conversation_turn(director_telegram_user_id, "director", raw_message)

    try:
        history = format_history(await store.recent_conversation(director_telegram_user_id))
        roster_lines, roster = await _roster()
        async with OpenRouterClient(
            agent=AGENT,
            run_id=run_id,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            result = await ai.complete_json(
                CLASSIFY_SYSTEM_PROMPT, build_classify_message(raw_message, history, today_local(), roster_lines)
            )

        task_summary = (result.get("task_summary") or raw_message[:200]).strip()
        due_date = task_tracker.parse_due_date(result.get("due_date"), today_local())
        person, known = _pick_person(result, roster)
        if not known:
            await _reply_and_log(
                director_telegram_user_id, run_id, "Кимга юборишни аниқлай олмадим — исмни аниқроқ ёзинг."
            )
            return
        validated = validate_classification(result)

        if validated is None:
            # Model returned an out-of-enum value -- code-level backstop,
            # the proportionate equivalent of Lead Agent's second AI pass for
            # this much narrower classification task.
            log.warning("Classification returned unrecognized target: {}", result)
            await _reply_and_log(
                director_telegram_user_id,
                run_id,
                "Аниқ тушунмадим — илтимос, аниқроқ ёзинг.",
            )
            return

        target_type, target_role, target_agent = validated
        log.info(
            "Classified '{}' -> type={} role={} agent={}",
            raw_message[:120], target_type, target_role, target_agent,
        )
        if target_type == "employee":
            await _propose_task(
                director_telegram_user_id, source_message_id, raw_message, task_summary, target_role, run_id,
                due_date, person,
            )
        elif target_type == "agent":
            await _answer_from_agent(director_telegram_user_id, target_agent, raw_message, run_id, history)
        elif target_type == "refused":
            # The guardrail path — the model's own polite refusal, already in
            # Uzbek/Russian per prompt.py's GUARDRAILS block.
            log.info("Classification refused a message from {}", director_telegram_user_id)
            await _reply_and_log(director_telegram_user_id, run_id, sanitize_model_html(task_summary))
        else:  # "none"
            # Use the model's own explanation (e.g. "I can't delete records,
            # that needs to be done manually in the Sheet") rather than a
            # generic "couldn't understand" — "none" covers both genuine
            # ambiguity AND out-of-capability requests, and those need
            # different messages to actually be useful to the Director.
            await _reply_and_log(
                director_telegram_user_id,
                run_id,
                sanitize_model_html(task_summary) or "Буни кимга йўналтиришни тушунмадим — аниқроқ ёзиб бера оласизми?",
            )

    except OpenRouterError as exc:
        log.error("Classification failed for source_message_id={}: {}", source_message_id, exc)
        await log_action(
            agent=AGENT, action="dispatch_task", target_system="openrouter",
            status="failure", run_id=run_id, error_message=str(exc), mode="write",
        )
        await _safe_notify_failure(director_telegram_user_id, run_id)
    except Exception as exc:  # noqa: BLE001 — a background failure must be recorded, not raised
        log.error("Dispatch failed for source_message_id={}: {}", source_message_id, exc)
        await log_action(
            agent=AGENT, action="dispatch_task", target_system="telegram",
            status="failure", run_id=run_id, error_message=str(exc), mode="write",
        )
        await _safe_notify_failure(director_telegram_user_id, run_id)


async def _roster() -> tuple[list[str], dict[str, dict[str, Any]]]:
    """The people the Director can address by name, for the classifier.

    Codes (E1, E2, ...) rather than database ids: short tokens the model
    copies back reliably, mapped to the real rows here.

    Returns:
        ``(prompt lines, code -> employee row)``.
    """
    people = [e for e in await store.list_active_employees() if e["role"] != DIRECTOR_ROLE]
    people.sort(key=lambda e: names.person_name(e).lower())
    lines: list[str] = []
    lookup: dict[str, dict[str, Any]] = {}
    for index, person in enumerate(people, start=1):
        code = f"E{index}"
        lookup[code] = person
        lines.append(f"{code} = {names.person_name(person)} ({person['role']})")
    return lines, lookup


def _pick_person(result: dict[str, Any], roster: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """Resolve the classifier's ``target_employee`` against the roster.

    Also points ``target_role`` at that person's role, so a model that named
    the right person but the wrong department still routes correctly.

    Returns:
        ``(employee or None, known)`` — known is False when the model named a
        code that isn't on the list; the caller must then ask, not fall back
        to a whole department.
    """
    code = str(result.get("target_employee") or "").strip().upper()
    if not code or code in ("NULL", "NONE"):
        return None, True
    person = roster.get(code)
    if person is None:
        return None, False
    if result.get("target_type") == "employee":
        result["target_role"] = person["role"]
    return person, True


async def _safe_notify_failure(director_telegram_user_id: int, run_id: uuid.UUID) -> None:
    """Best-effort failure notice — itself guarded so it can't raise."""
    try:
        await _reply(
            director_telegram_user_id,
            run_id,
            "Хатолик юз берди, бироздан кейин қайта уриниб кўринг.",
        )
    except Exception as exc:  # noqa: BLE001 — a second failure must not raise inside a background task
        log.error("Also failed to notify the Director of the dispatch failure: {}", exc)


async def _propose_task(
    director_id: int,
    source_message_id: int | None,
    raw_message: str,
    task_summary: str,
    role_slug: str | None,
    run_id: uuid.UUID,
    due_date: date | None = None,
    person: dict[str, Any] | None = None,
    has_media: bool = False,
) -> None:
    """Hold the task and ask the Director who gets it (``task_picker.py``).

    The bot's guess — the named person, or everyone in the department — is
    ticked; nothing reaches an employee until the Director taps "Юбориш".
    """
    people = [e for e in await store.list_active_employees() if e["role"] != DIRECTOR_ROLE]
    if not people:
        await _reply_and_log(director_id, run_id, "Ҳали ҳеч ким рўйхатдан ўтмаган.")
        return
    if person is not None:
        suggested = {str(person["id"])}
    elif role_slug:
        suggested = {str(e["id"]) for e in people if e["role"] == role_slug}
    else:
        suggested = set()
    note = ""
    if role_slug and person is None and not suggested:
        note = f"{ROLE_LABELS.get(role_slug, role_slug)} учун ҳали ҳеч ким рўйхатдан ўтмаган — керакли одамни белгиланг."
    ordered = task_picker.candidates(people, suggested)
    draft = await store.create_task_draft(
        director_telegram_user_id=director_id,
        source_message_id=source_message_id,
        raw_message=raw_message,
        task_summary=task_summary,
        role_slug=role_slug,
        due_date=due_date,
        has_media=has_media,
        candidate_ids=[str(p["id"]) for p in ordered],
        selected_ids=sorted(suggested),
    )
    picked = [p for p in ordered if str(p["id"]) in suggested]
    message_ids = await _reply(
        director_id, run_id, task_picker.card_text(task_summary, raw_message, picked, note),
        task_picker.keyboard(str(draft["id"]), ordered, suggested),
    )
    await store.set_task_draft_message(str(draft["id"]), message_ids[-1] if message_ids else None)
    await store.log_conversation_turn(director_id, "bot", "Топшириқ тайёр — кимга юборишни белгиланг.")


async def _draft_people(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The draft's people in button order (anyone removed since is left out)."""
    active = {str(e["id"]): e for e in await store.list_active_employees()}
    return [active[str(i)] for i in draft["candidate_ids"] if str(i) in active]


async def _edit_draft_card(callback: dict[str, Any], text: str, keyboard: dict[str, Any], run_id: uuid.UUID) -> None:
    message = callback.get("message") or {}
    if not (message.get("message_id") and message.get("chat", {}).get("id")):
        return
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
            chat_id=str(message["chat"]["id"]), message_id=message["message_id"], text=text, reply_markup=keyboard
        )


async def _handle_draft_toggle(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """The Director ticked or unticked one person on a task card."""
    query_id = callback.get("id", "")
    parsed = task_picker.parse_toggle(rest)
    clicker_id = (callback.get("from") or {}).get("id")
    draft = await store.get_task_draft(parsed[0]) if parsed else None
    if draft is None or parsed[1] >= len(draft["candidate_ids"]):
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    draft = await store.toggle_task_draft_person(str(draft["id"]), clicker_id, str(draft["candidate_ids"][parsed[1]]))
    if draft is None:
        await _answer(query_id, "Бу топшириқ аллақачон ҳал қилинган")
        return "draft_closed"
    people = await _draft_people(draft)
    selected = {str(i) for i in draft["selected_ids"]}
    await _edit_draft_card(
        callback,
        task_picker.card_text(draft["task_summary"], draft["raw_message"], [p for p in people if str(p["id"]) in selected]),
        task_picker.keyboard(str(draft["id"]), people, selected),
        run_id,
    )
    await _answer(query_id, "")
    return "draft_toggled"


async def _handle_draft_send(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """"Юбориш": the task goes to everyone ticked, once; then the deadline question as before."""
    query_id = callback.get("id", "")
    clicker_id = (callback.get("from") or {}).get("id")
    draft = await store.get_task_draft(rest)
    if draft is None or draft["director_telegram_user_id"] != clicker_id:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    if not draft["selected_ids"]:
        await _answer(query_id, "Ҳеч ким белгиланмаган")
        return "draft_empty"
    draft = await store.close_task_draft(str(draft["id"]), clicker_id, "sent")
    if draft is None:
        await _answer(query_id, "Бу топшириқ аллақачон ҳал қилинган ёки муддати ўтган")
        return "draft_closed"
    await _answer(query_id, "Юборилмоқда")
    selected = {str(i) for i in draft["selected_ids"]}
    recipients = [p for p in await _draft_people(draft) if str(p["id"]) in selected]
    sent = await _send_task_cards(draft, recipients, run_id)

    text = task_picker.sent_text(draft["task_summary"], sent) if sent else "⚠️ Ҳеч кимга етказиб бўлмади."
    keyboard: dict[str, Any] = {"inline_keyboard": []}
    if sent and draft["due_date"] is not None:
        text += f"\n{task_tracker.deadline_line(draft['due_date'], today_local())}"
    elif sent and draft["source_message_id"]:
        # A3 needs "who, what, by when": a deadline is asked, never guessed.
        text += "\n⏰ Муддат кўрсатилмади — қачонгача?"
        keyboard = task_tracker.deadline_keyboard(draft["source_message_id"])
    await _edit_draft_card(callback, text, keyboard, run_id)
    await store.log_conversation_turn(clicker_id, "bot", text)
    return "draft_sent"


async def _handle_draft_cancel(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    query_id = callback.get("id", "")
    clicker_id = (callback.get("from") or {}).get("id")
    draft = await store.close_task_draft(rest, clicker_id, "cancelled")
    if draft is None:
        await _answer(query_id, "Бу топшириқ аллақачон ҳал қилинган")
        return "draft_closed"
    await _answer(query_id, "Бекор қилинди")
    await _edit_draft_card(callback, task_picker.cancelled_text(draft["task_summary"]), {"inline_keyboard": []}, run_id)
    return "draft_cancelled"


async def _send_task_cards(draft: dict[str, Any], employees: list[dict[str, Any]], run_id: uuid.UUID) -> list[dict[str, Any]]:
    """One task row and card per person — a text card, or the Director's file copied with the card as caption."""
    sent: list[dict[str, Any]] = []
    director_id = draft["director_telegram_user_id"]
    due_date = draft["due_date"]
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for employee in employees:
            task = await store.create_task(
                director_telegram_user_id=director_id,
                source_message_id=draft["source_message_id"] or 0,
                raw_message=draft["raw_message"],
                target_type="employee",
                target_role=employee["role"],
                target_agent=None,
                assigned_employee_id=str(employee["id"]),
                task_summary=draft["task_summary"],
                has_media=draft["has_media"],
                due_date=due_date,
            )
            if task is None:
                continue  # already sent (a duplicate tap or webhook delivery)
            try:
                if draft["has_media"]:
                    result = await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                        "copyMessage",
                        {
                            "chat_id": str(employee["telegram_user_id"]),
                            "from_chat_id": str(director_id),
                            "message_id": draft["source_message_id"],
                            "caption": _task_card_text(draft["task_summary"], due_date=due_date),
                            "parse_mode": "HTML",
                            "reply_markup": _task_keyboard(str(task["id"])),
                        },
                        mode="notify",
                        target_ref=str(employee["telegram_user_id"]),
                    )
                    message_id = result.get("message_id") if result else None
                else:
                    ids = await bot.send_message(
                        _task_card_text(draft["task_summary"], draft["raw_message"], due_date),
                        chat_id=str(employee["telegram_user_id"]),
                        reply_markup=_task_keyboard(str(task["id"])),
                    )
                    message_id = ids[0] if ids else None
            except TelegramError as exc:
                log.error("Could not send the task to {}: {}", names.person_name(employee), exc)
                continue
            if message_id:
                await store.set_task_message_id(str(task["id"]), message_id)
            sent.append(employee)
    return sent


async def _handle_task_due(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """The Director picked a deadline for a task they just sent."""
    query_id = callback.get("id", "")
    code, _, message_part = rest.partition(":")
    clicker_id = callback.get("from", {}).get("id")
    try:
        source_message_id = int(message_part)
    except ValueError:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"

    recognised, due = task_tracker.due_from_choice(code, today_local())
    if not recognised:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"

    director = await store.get_employee_by_telegram_id(clicker_id) if clicker_id else None
    if director is None or director["role"] != DIRECTOR_ROLE or director["status"] != "active":
        await _answer(query_id, "Рухсат йўқ")
        return "unauthorized"

    updated = [] if due is None else await store.set_dispatch_due_date(clicker_id, source_message_id, due)
    label = "Муддатсиз" if due is None else task_tracker.deadline_line(due, today_local())

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            original = (message.get("text") or "").split("\n⏰", 1)[0]
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]),
                message_id=message["message_id"],
                text=f"{escape(original)}\n{label}",
                reply_markup={"inline_keyboard": []},
            )
        await bot._answer_callback(query_id, "OK")  # noqa: SLF001
        # The employee's card went out without a deadline; tell them now.
        for task in updated:
            try:
                await bot.send_message(
                    f"{task_tracker.deadline_line(due, today_local())}\n"
                    f"{sanitize_model_html(task['task_summary'])}",
                    chat_id=str(task["employee_telegram_user_id"]),
                )
            except TelegramError as exc:
                log.warning("Could not send the deadline to {}: {}", task.get("display_name"), exc)

    log.info("Deadline {} set on {} task(s) from message {}", due, len(updated), source_message_id)
    return "task_due_set"


# ------------------------------------------------------------------- media/files


async def _dispatch_director_media(
    director_telegram_user_id: int, source_message_id: int | None, caption: str, run_id: uuid.UUID
) -> None:
    """Classify a Director's media/file by its caption and dispatch it.

    Runs in the background for the same reason ``_dispatch_director_task``
    does. Unlike the text path, a classification failure here doesn't dead-
    end the request — there's an obvious fallback (ask via buttons) that a
    plain-text task doesn't have, so errors fall through to
    ``_ask_media_target`` instead of just reporting failure.

    Args:
        director_telegram_user_id: The Director's Telegram numeric id.
        source_message_id: Telegram message id of the media itself, needed
            to ``copyMessage`` it later.
        caption: The original caption, if any.
        run_id: UUID grouping this webhook call's audit rows.
    """
    if source_message_id is None:
        log.warning("Media message from {} had no message_id, cannot forward", director_telegram_user_id)
        await _safe_notify_failure(director_telegram_user_id, run_id)
        return

    validated = None
    refusal_text: str | None = None
    media_due: date | None = None
    media_person: dict[str, Any] | None = None
    if caption:
        try:
            async with OpenRouterClient(
                agent=AGENT,
                run_id=run_id,
                    model_override=settings.ops_manager_bot_model,
                fallback_override=settings.ops_manager_bot_fallback_models,
            ) as ai:
                roster_lines, roster = await _roster()
                result = await ai.complete_json(
                    CLASSIFY_SYSTEM_PROMPT, build_classify_message(caption, today=today_local(), roster=roster_lines)
                )
            media_person, known = _pick_person(result, roster)
            validated = validate_classification(result) if known else None
            media_due = task_tracker.parse_due_date(result.get("due_date"), today_local())
            if validated is not None and validated[0] == "refused":
                refusal_text = (result.get("task_summary") or "").strip() or None
        except OpenRouterError as exc:
            log.warning("Media caption classification failed, falling back to a role picker: {}", exc)

    try:
        if validated is not None and validated[0] == "employee":
            await _propose_task(
                director_telegram_user_id, source_message_id, caption or "Медиа файл", caption or "Медиа файл",
                validated[1], run_id, media_due, media_person, has_media=True,
            )
        elif validated is not None and validated[0] == "refused":
            # Guardrail path — refuse and stop, same as the text-task flow.
            # Do NOT fall through to the role picker: an inappropriate
            # caption shouldn't still get its attached file forwarded.
            log.info("Media caption classification refused a message from {}", director_telegram_user_id)
            await _reply(
                director_telegram_user_id, run_id,
                sanitize_model_html(refusal_text) if refusal_text else "Кечирасиз, бунга ёрдам бера олмайман.",
            )
        else:
            await _ask_media_target(director_telegram_user_id, source_message_id, caption, run_id)
    except Exception as exc:  # noqa: BLE001 — a background failure must be recorded, not raised
        log.error("Media dispatch failed for source_message_id={}: {}", source_message_id, exc)
        await log_action(
            agent=AGENT, action="dispatch_media", target_system="telegram",
            status="failure", run_id=run_id, error_message=str(exc), mode="write",
        )
        await _safe_notify_failure(director_telegram_user_id, run_id)


async def _ask_media_target(
    director_telegram_user_id: int, source_message_id: int, caption: str, run_id: uuid.UUID
) -> None:
    """A file with no (or an unclear) caption: the people list with nobody ticked."""
    await _propose_task(
        director_telegram_user_id, source_message_id, caption or "Медиа файл", caption or "Медиа файл", None, run_id,
        has_media=True,
    )


async def _handle_dispatch_role(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """A department tap on an older file card (before 2026-10-02): now opens the people list."""
    query_id = callback.get("id", "")
    parsed = parse_role_and_request(rest)
    if parsed is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"

    role_slug, pending_id = parsed
    if role_slug not in ROLE_SLUGS:
        await _answer(query_id, "Номаълум роль")
        return "unrecognized"

    pending = await store.resolve_pending_dispatch(pending_id)
    if pending is None:
        await _answer(query_id, "Аллақачон ҳал қилинган ёки топилмади")
        return "not_found"

    clicker = callback.get("from", {})
    if clicker.get("id") != pending["director_telegram_user_id"]:
        await _answer(query_id, "Бу сизнинг сўровингиз эмас")
        return "unauthorized"

    await _answer(query_id, "OK")
    caption = pending.get("caption") or "Медиа файл"
    await _propose_task(
        pending["director_telegram_user_id"], pending["source_message_id"], caption, caption, role_slug, run_id,
        has_media=True,
    )
    return "dispatched"


async def _answer_from_agent(
    director_id: int, agent_slug: str, question: str, run_id: uuid.UUID, history: str = ""
) -> None:
    """Answer a Director's question using one agent's already-computed data.

    Args:
        director_id: The Director's Telegram numeric id.
        agent_slug: One of ``roles.AGENT_SLUGS``.
        question: The Director's original question.
        run_id: UUID grouping this webhook call's audit rows.
        history: Formatted recent conversation (``prompt.format_history``) —
            fetched by the caller so a message already known to be a
            classification result doesn't pay for a second DB round trip;
            fetched fresh here if called with the default when history
            wasn't already on hand.
    """
    if not history:
        history = format_history(await store.recent_conversation(director_id))
    data = await _fetch_agent_data(agent_slug)
    log.info(
        "Answering '{}' from agent={} -- {} char(s) of data, preview: {}",
        question[:120], agent_slug, len(data), data[:200].replace("\n", " | "),
    )
    async with OpenRouterClient(
        agent=AGENT,
        run_id=run_id,
        model_override=settings.ops_manager_bot_model,
        fallback_override=settings.ops_manager_bot_fallback_models,
    ) as ai:
        answer = await ai.complete(
            ANSWER_SYSTEM_PROMPT, build_answer_message(AGENT_LABELS[agent_slug], data, question, history)
        )
    log.info("Answer for agent={}: {}", agent_slug, answer[:300].replace("\n", " | "))
    await _reply_and_log(director_id, run_id, sanitize_model_html(answer))


# ------------------------------------------------------------- agent data reads
# No live agent invocation in v1 (per the business owner's own instruction) --
# each fetcher reads whatever that agent already computed and stored.


async def _fetch_agent_data(agent_slug: str) -> str:
    """Dispatch to the per-agent data fetcher below.

    ``all_systems`` runs every fetcher and concatenates them, labeled, for
    questions that span more than one system ("leads and CRM and everything")
    — Gemini 3.8 Flash's 1M-token context makes this a non-issue size-wise; the
    alternative (silently picking one system and ignoring the rest of the
    question) is the actual problem this exists to avoid.
    """
    # garmin_catalog is a static reference snapshot, not a live operational
    # system — deliberately excluded from all_systems below (a "how's the
    # business doing" combined summary shouldn't be padded with a product
    # price list that has nothing to do with operational status).
    if agent_slug == "garmin_catalog":
        return GARMIN_CATALOG

    # Same reasoning as garmin_catalog above -- these are SAP gateway
    # reference lookups/periodic snapshots, not all_systems material.
    sap_gateway_tools = {
        "sap_orders": "orders",
        "sap_products": "products",
        "sap_customers": "customers",
        "sap_warehouses": "warehouses",
        "sap_inventory": "inventory",
        "sap_payments": "payments",
    }
    if agent_slug in sap_gateway_tools:
        return await _fetch_sap_gateway_data(sap_gateway_tools[agent_slug])

    fetchers = {
        "topshiriqlar": _fetch_task_tracker_data,
        "lead_agent": _fetch_lead_agent_data,
        "finance_agent": _fetch_finance_agent_data,
        "reporter_agent": _fetch_reporter_agent_data,
        "xodimlar_kpi": _fetch_kpi_agent_data,
        "ruxsatlar": permission_flow.registry_data,
        "pul_kalendari": _fetch_cash_calendar_data,
        "mijoz_fikrlari": _fetch_client_feedback_data,
        "lidlar": _fetch_lead_handout_data,
        "davomat": _fetch_attendance_data,
        "garmin_lidlar": _fetch_garmin_leads_data,
        "billz_savdo": _fetch_billz_data,
        "pul_qoldigi": _fetch_cash_balance_data,
    }
    if agent_slug == "all_systems":
        sections = []
        for slug, fetcher in fetchers.items():
            sections.append(f"=== {AGENT_LABELS[slug]} ===\n{await fetcher()}")
        return "\n\n".join(sections)

    fetcher = fetchers.get(agent_slug)
    if fetcher is None:
        return "(no data source configured for this agent)"
    return await fetcher()


async def _fetch_kpi_agent_data() -> str:
    """The bot's own daily reports: who answered, what they wrote, KPI vs target.

    Includes the rows nobody ever answered — a report that was asked for and
    never sent is the whole point of this data, and it exists only because
    agents/daily-reports opens a row for everyone at 16:00 (see
    docs/agent-specs/06-daily-reports.md).
    """
    rows = await store.recent_reports(days=14)
    if not rows:
        return (
            "No daily reports collected yet. agents/daily-reports asks every active employee "
            "(except the Director) at 16:00 Asia/Tashkent, Mon-Fri, and on Saturday/Sunday only those "
            "marked as weekend workers; rows appear from that "
            "run onward. Nobody has been asked yet, which is NOT the same as nobody reporting."
        )

    today = today_local()
    today_rows = [r for r in rows if r["report_date"] == today]
    lines = [f"Daily reports collected by this bot ({len(rows)} row(s), last 14 days):"]
    if today_rows:
        reported = [r["display_name"] for r in today_rows if r["status"] == "submitted"]
        silent = [r["display_name"] for r in today_rows if r["status"] != "submitted"]
        lines.append(
            f"TODAY ({today}): {len(reported)} reported, {len(silent)} have not. "
            f"Not reported yet: {', '.join(silent) if silent else 'nobody — everyone answered'}."
        )
    else:
        lines.append(f"TODAY ({today}): nobody has been asked yet (the 16:00 run hasn't happened).")

    for row in rows:
        who = f"{row['display_name']} ({row['role']})"
        if row["status"] != "submitted":
            lines.append(f"- [{row['report_date']}] {who}: NO REPORT SENT")
            continue
        parts = [f"- [{row['report_date']}] {who}: {row['content']}"]
        numbers = kpi.format_metrics(row["metrics"] or {}, kpi.metrics_for_role(row["role"]))
        if numbers:
            parts.append(numbers)
        lines.append(" | ".join(parts))

    # The KPI score (the Director's criteria, kpi_score.py) for the period /kpi shows.
    lines.append(await kpi_flow.describe(kpi_score.score_period(today)))
    return "\n".join(lines)


async def _fetch_cash_balance_data() -> str:
    """Bank and cash balances from 1C, right now."""
    from integrations.onec import cash as onec_cash

    if not settings.onec_configured:
        return "1C is not connected yet (ONEC_ODATA_URL / ONEC_LOGIN / ONEC_PASSWORD not set). Say so; do not guess."
    try:
        return onec_cash.describe(await onec_cash.load(now_local().replace(tzinfo=None), run_id=None, agent=AGENT))
    except Exception as exc:  # noqa: BLE001 — "unavailable" for the Director
        log.error("1C cash read failed: {!r}", exc)
        return f"1C could not be read right now ({type(exc).__name__}: {exc}). Say balances are unavailable; do not guess."


async def _fetch_billz_data() -> str:
    """BILLZ shop sales, last 30 days (shops, sellers, top products)."""
    from integrations.billz import sales as billz_sales

    if not settings.billz_configured:
        return "BILLZ (the shop till system) is not connected yet: BILLZ_SECRET_TOKEN is not set. Say so; do not guess."
    try:
        return await billz_sales.load_period(today_local(), 30, run_id=None, agent=AGENT)
    except Exception as exc:  # noqa: BLE001 — "unavailable", not an error, for the Director
        log.error("BILLZ read failed: {}", exc)
        return f"BILLZ could not be read right now ({exc}). Say shop sales are unavailable; do not guess."


async def _fetch_garmin_leads_data() -> str:
    """Leads the Garmin AI bot sent to the Command Center, last 30 days."""
    from integrations.garmin import leads as garmin_leads

    return garmin_leads.describe(await garmin_leads.recent(30))


async def _fetch_attendance_data() -> str:
    """Verifix attendance (A4): today so far, yesterday, each person's last 30 days."""
    from integrations.verifix import attendance

    if not settings.verifix_configured:
        return (
            "Verifix (face-ID attendance) is not connected yet: VERIFIX_CLIENT_ID and VERIFIX_CLIENT_SECRET "
            "are not set, so there is no attendance data at all. Say so plainly; do not guess."
        )
    today = today_local()
    now = now_local().replace(tzinfo=None)  # Verifix times are Tashkent wall time
    try:
        recs = await attendance.load(today - timedelta(days=30), today, run_id=None, agent=AGENT, now=now)
    except Exception as exc:  # noqa: BLE001 — the Director gets "unavailable", not an error
        log.error("Verifix read failed: {}", exc)
        return f"Verifix could not be read right now ({exc}). Say attendance data is unavailable; do not guess."
    return attendance.describe(recs, today, now)


async def _fetch_lead_handout_data() -> str:
    """Leads handed to sales people in the last 30 days and where each stands."""
    return leads.describe(await store.recent_lead_assignments(days=30))


async def _fetch_client_feedback_data() -> str:
    """What clients wrote through the QR codes, last 60 days."""
    from integrations.org_bot import feedback

    return feedback.describe(await store.recent_client_feedback(days=60))


async def _fetch_cash_calendar_data() -> str:
    """The next 30 days of money in and out (B2), same as the Monday message."""
    from integrations.common.agent_loader import load_agent  # the agent lives in a hyphenated folder

    calendar = load_agent("cash-calendar")
    return calendar.describe(await calendar.load(today_local()))


async def _fetch_task_tracker_data() -> str:
    """Open tasks with deadlines, what's overdue, and on-time rates (A3)."""
    today = today_local()
    open_tasks = await store.open_tasks_with_names()
    lines = [f"Today is {today.isoformat()}. Tasks the Director assigned through this bot."]
    if not open_tasks:
        lines.append("No open tasks: everything assigned has been marked done.")
    else:
        lines.append(f"OPEN TASKS ({len(open_tasks)}), oldest deadline first:")
        for task in open_tasks:
            due = task.get("due_date")
            if due is None:
                state = "no deadline"
            elif due < today:
                state = f"OVERDUE since {due.isoformat()} ({(today - due).days} day(s))"
            else:
                state = f"due {due.isoformat()}"
            lines.append(
                f"- {task['display_name']} ({task['role']}) | {task['status']} | {state} | "
                f"assigned {task['created_at'].date().isoformat()} | {' '.join(task['task_summary'].split())}"
            )

    start = today - timedelta(days=30)
    score = task_tracker.score_tasks(await store.tasks_due_between(start, today), start, today)
    if score.due:
        lines.append(
            f"LAST 30 DAYS: {score.due} task(s) were due; {score.on_time} done on time, {score.late} done late, "
            f"{score.open_overdue} still not done. On-time index = {score.index:.2f}."
        )
        for name, (due, done) in sorted(score.by_person.items()):
            lines.append(f"- {name}: {done}/{due} on time")
    else:
        lines.append("LAST 30 DAYS: no task with a deadline fell due, so there is no on-time rate yet.")
    return "\n".join(lines)


LEAD_SHEET_COLUMNS = leads.SHEET_COLUMNS  # the one list, shared with the lead hand-out


async def _fetch_lead_agent_data() -> str:
    """Every lead, every column — Gemini 3.8 Flash's 1M-token context makes
    the old 15-row/4-column preview an unnecessary limitation."""
    try:
        async with SheetsClient(agent=AGENT) as sheets:
            rows = await sheets.get_values(leads.SHEET_READ_RANGE)
    except SheetsError as exc:
        return f"(could not read the leads sheet: {exc})"

    records = leads.sheet_records(rows)  # by header: people may move columns
    if not records:
        return "No leads recorded yet."

    lines = [f"{len(records)} total leads on record, numbered in sheet order:"]
    for i, record in enumerate(records, start=1):
        fields = {c: record.get(c, "") for c in LEAD_SHEET_COLUMNS}
        lines.append(
            f"{i}. {fields['company_name']} | {fields['industry']} | {fields['location']} | "
            f"stage={fields['project_stage']} | opening={fields['estimated_opening']} | "
            f"priority={fields['priority']} | confidence={fields['confidence']} | {fields['track']}\n"
            f"   signal: {fields['signal']} ({fields['signal_source_url']})\n"
            f"   contact: {fields['contact_name']} {fields['contact_role']} {fields['contact_method']}\n"
            f"   notes: {fields['notes']}"
        )
    return "\n".join(lines)


async def _fetch_finance_agent_data() -> str:
    """All open receivables (= open SAP invoices) + recent alerts, not just the top 15/5."""
    aging = await fetch_all(
        "SELECT doc_num, card_name, days_overdue, aging_bucket, balance_due_tiyin, currency, due_date, sales_person_name "
        "FROM v_ar_aging_latest ORDER BY balance_due_tiyin DESC LIMIT 200"
    )
    alerts = await fetch_all(
        "SELECT title, body, created_at FROM alerts WHERE agent = 'receivables' ORDER BY created_at DESC LIMIT 30"
    )
    if not aging and not alerts:
        return "No receivables data recorded yet."

    # Say "SAP invoice" explicitly, not just "receivable" -- a receivable
    # *is* an open SAP invoice, but a Director asking specifically for
    # "invoices" or "SAP" data got refused twice: once because the data was
    # only ever labeled "receivable" with no invoice number shown, and once
    # because neither the label nor this text said "SAP" anywhere, so the
    # answer model had no textual basis to confirm this data was SAP data
    # (confirmed live: it said outright "these are receivables, not SAP").
    lines = [f"{len(aging)} open SAP invoice(s) / receivable(s) (source: SAP Business One OINV):"]
    for r in aging:
        # Pre-formatted with format_money rather than handed to the model as
        # "<number> <code>": given the raw pair, the model reasonably renders
        # "UZS" as "so'm" in an Uzbek reply, so a wrong code upstream turned
        # into confidently wrong output. Passing "$9,764.31" already formatted
        # leaves nothing to reinterpret, and makes a wrong currency obvious on
        # sight instead of laundered through translation.
        amount = format_money(r["balance_due_tiyin"], r["currency"])
        lines.append(
            f"- Invoice #{r['doc_num']}, {r['card_name']}: {amount}, "
            f"{r['days_overdue']}d overdue "
            f"({r['aging_bucket']}), due {r['due_date']}, owner={r['sales_person_name']}"
        )
    if alerts:
        lines.append("\nRecent receivables alerts:")
        for a in alerts:
            lines.append(f"- {a['title']}: {a.get('body') or ''} ({a['created_at']})")
    return "\n".join(lines)


async def _fetch_sap_gateway_data(tool: str) -> str:
    """The latest pushed snapshot for one SAP gateway tool (see push_handler.py).

    Formats each row's raw JSON as readable ``key: value`` pairs rather than
    a fixed set of columns — the exact field names these six tools return
    aren't confirmed the way get_invoices' were (see push_handler.py's
    module docstring), so this stays defensive: whatever fields a row
    actually has get shown, nothing assumed.

    Args:
        tool: One of push_handler.VALID_TOOLS.

    Returns:
        Plain-text listing, or a message saying nothing's been pushed yet
        for this tool.
    """
    rows = await fetch_all(
        "SELECT natural_key, raw, captured_at FROM v_sap_gateway_latest WHERE tool = %s "
        "ORDER BY captured_at DESC LIMIT 100",
        (tool,),
    )
    if not rows:
        return (
            f"No {tool} data pushed yet from the SAP gateway. The push script "
            f"(scripts/sap-gateway-push/) needs to call get_{tool} and push it "
            f"to /webhooks/sap-gateway-push/{tool}/<secret> at least once."
        )

    lines = [f"{len(rows)} {tool} record(s), most recently captured {rows[0]['captured_at']}:"]
    for r in rows:
        raw = r["raw"] if isinstance(r["raw"], dict) else {}
        fields = ", ".join(f"{k}={v}" for k, v in raw.items() if v is not None)
        lines.append(f"- {fields}")
    return "\n".join(lines)


async def _fetch_reporter_agent_data() -> str:
    """The last 14 days of daily briefs, so trend questions aren't limited
    to a single snapshot the way a one-day-only fetch would be."""
    briefs = await fetch_all(
        "SELECT brief_date, ar_overdue_total_tiyin FROM daily_briefs ORDER BY generated_at DESC LIMIT 14"
    )
    if not briefs:
        return "No daily brief has been generated yet."

    lines = [f"Daily brief history, most recent first ({len(briefs)} day(s)):"]
    for brief in briefs:
        overdue = brief["ar_overdue_total_tiyin"]
        lines.append(
            f"- {brief['brief_date']}: AR overdue="
            f"{format_money(overdue, settings.sap_default_currency) if overdue is not None else 'no data'}"
        )
    return "\n".join(lines)
