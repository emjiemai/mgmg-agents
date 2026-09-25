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
from integrations.common.timeutil import now_utc, today_local
from integrations.google.sheets_client import SheetsClient, SheetsError
from integrations.org_bot import admin, kpi, names, permission_flow, store, task_tracker
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
    ROLES,
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


def report_message_kind(replied_to_ask: bool, replied_to_task: bool, open_tasks: int) -> str:
    """What a message sent while today's report is still owed should be.

    Nobody has to use Telegram's reply feature: a plain message is the report,
    unless the employee has a task in flight — then it could be either, and
    the bot asks with one tap instead of guessing.

    Returns:
        "report" (save it as today's report), "task_update" (a reply to a
        task card — the normal relay), or "ask" (report or message? buttons).
    """
    if replied_to_ask:
        return "report"
    if replied_to_task:
        return "task_update"
    return "ask" if open_tasks else "report"


def report_or_relay_keyboard(relay_id: str) -> dict[str, Any]:
    """The one-tap choice for a message that could be the report or a message."""
    return {
        "inline_keyboard": [
            [
                {"text": "📝 Ҳа, ҳисобот", "callback_data": f"asrep:{relay_id}"},
                {"text": "📨 Директорга хабар", "callback_data": f"relayok:{relay_id}"},
            ],
            [{"text": "❌ Бекор қилиш", "callback_data": f"relayno:{relay_id}"}],
        ]
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
        return await _handle_callback(callback, run_id)

    message = update.get("message")
    if not message:
        return "ignored"

    return await _handle_message(message, run_id, background)


# ---------------------------------------------------------- callback buttons


async def _handle_callback(callback: dict[str, Any], run_id: uuid.UUID) -> str:
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
    if prefix in ("relayok", "relayno"):
        return await _handle_relay_decision(rest, prefix == "relayok", callback, run_id)
    if prefix == "asrep":
        return await _handle_save_as_report(rest, callback, run_id)

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


async def send_registration_confirmed(access_request: dict[str, Any], run_id: uuid.UUID) -> None:
    """Tell the requester the admin confirmed their role and they're registered.

    Args:
        access_request: The ``access_requests`` row, role now approved.
        run_id: UUID grouping this webhook call's audit rows.
    """
    role = ROLE_LABELS.get(access_request["requested_role"], access_request["requested_role"])
    text = f"✅ Сиз <b>{escape(role)}</b> сифатида рўйхатдан ўтдингиз."
    # Everyone except the Director gives their real name straight away, so
    # the Director can address them by it (see names.py).
    if access_request["requested_role"] != DIRECTOR_ROLE:
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

    # Written permission requests (EMJ-SOP-ADM-01) come before everything
    # else, for the Director too: an answer to the form's own question, or an
    # approver's conditions, must not be re-read as a task or a task update.
    permission_outcome = await permission_flow.handle_message(employee, message, run_id)
    if permission_outcome is not None:
        return permission_outcome

    if employee["role"] != DIRECTOR_ROLE:
        return await _handle_employee_message(employee, message, run_id)

    has_media = any(message.get(field) for field in MEDIA_FIELDS)
    text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()

    if not has_media and not text:
        return "ignored"

    reply_to = message.get("reply_to_message") or {}
    if not has_media and reply_to.get("message_id"):
        if await _try_forward_director_reply(telegram_user_id, reply_to["message_id"], text, run_id):
            return "relayed"

    await _show_typing(telegram_user_id, run_id)

    if has_media:
        background.add_task(_dispatch_director_media, telegram_user_id, message.get("message_id"), caption, run_id)
    else:
        background.add_task(_dispatch_director_task, telegram_user_id, text, message.get("message_id"), run_id)
    return "queued"


async def _try_forward_director_reply(
    director_id: int, reply_to_message_id: int, text: str, run_id: uuid.UUID
) -> bool:
    """If the Director is replying to a relayed employee message, forward
    the reply straight to that employee instead of running it through task
    classification -- a targeted reply to a specific person is the other
    half of a conversation already in progress, not a new task to route.

    Args:
        director_id: The replying Director's Telegram numeric id.
        reply_to_message_id: ``message.reply_to_message.message_id``.
        text: The Director's reply text.
        run_id: UUID grouping this webhook call's audit rows.

    Returns:
        True if this was a matching reply and has been handled (the caller
        must not also run classification on it); False if it doesn't match
        a known relay, so the caller should fall through to normal dispatch.
    """
    relay = await store.find_relay_by_director_message(director_id, reply_to_message_id)
    if relay is None:
        return False

    employee_telegram_user_id = relay["employee_telegram_user_id"]
    try:
        await _reply(employee_telegram_user_id, run_id, f"💬 Директордан:\n{escape(text)}")
    except TelegramError as exc:
        log.warning("Could not forward the Director's reply to employee {}: {}", employee_telegram_user_id, exc)
        return True  # matched a known relay -- don't fall through to classification even on delivery failure

    await store.create_task_update(
        task_id=relay.get("task_id"),
        employee_telegram_user_id=employee_telegram_user_id,
        message_text=text,
        director_telegram_user_id=director_id,
        direction="director_to_employee",
    )
    return True


async def _handle_unregistered_sender(telegram_user_id: int, sender: dict[str, Any], run_id: uuid.UUID) -> str:
    """Start or acknowledge a join request for a never-seen sender."""
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

WEAK_REPORT_SYSTEM = """You read an employee's end-of-day work report and \
decide ONLY whether it is too weak to be a report.

WEAK means it says nothing concrete about what they did today: "ok", "ishladim", \
"hammasi yaxshi", "bajarildi", "normal", "ish qildim", an emoji, a single vague \
word or phrase.
NOT weak: anything that names at least one concrete piece of work, however \
short ("mijozlarga qo'ng'iroq qildim", "omborni sanadim", "3 ta KP yubordim"). \
Do not judge how much they did or how well — that is not your job. When in \
doubt, it is NOT weak.

Return ONLY JSON: {"weak": false} or {"weak": true, "follow_up": "<one short, \
friendly question in Uzbek, in Cyrillic script, asking what concretely they \
did today>"}."""

# Longer than this is never "ok"/"ishladim" — skip the AI call entirely.
_WEAK_REPORT_MAX_LEN = 120


async def _weak_report_follow_up(text: str, run_id: uuid.UUID) -> str | None:
    """A short follow-up question if the report is vague, else None.

    Never blocks the report: on any AI failure the report simply stands.
    """
    if len(text) > _WEAK_REPORT_MAX_LEN:
        return None
    try:
        async with OpenRouterClient(
            agent=AGENT,
            run_id=run_id,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            verdict = await ai.complete_json(WEAK_REPORT_SYSTEM, text)
    except OpenRouterError as exc:
        log.warning("Weak-report check unavailable, accepting as is: {}", exc)
        return None
    if verdict.get("weak") is not True:
        return None
    return str(verdict.get("follow_up") or "").strip() or "Бугун аниқ қандай ишларни бажардингиз? Қисқача ёзинг."


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
        An outcome string when the message WAS the report (the caller must
        then not also relay it as a task update), or None to fall through to
        the normal relay — including when late numbers were merged into an
        already-submitted report, since that message still deserves to reach
        the Director like any other.
    """
    day = today_local()
    metrics_def = kpi.metrics_for_role(employee["role"])
    pending = await store.pending_report(employee["telegram_user_id"], day)

    if pending is None:
        # The answer to the one follow-up question on a vague report. It is
        # added to the report and the conversation ends there — no further
        # questions, whatever it says.
        followup = await store.open_report_followup(employee["telegram_user_id"], day)
        if followup is not None:
            if await store.answer_report_followup(str(followup["id"]), text) is not None:
                fresh = kpi.parse_metrics(text, metrics_def) if metrics_def else {}
                if fresh:
                    await store.merge_report_metrics(str(followup["id"]), fresh)
                await _reply(employee["telegram_user_id"], run_id, "✅ Раҳмат, ҳисоботингизга қўшилди.")
                return "daily_report_followup"

        # A reply to an earlier day's ask: reports close at midnight, so say
        # so instead of relaying a stale report to the Director.
        if reply_to_message_id:
            expired = await store.expired_report_for_prompt(employee["telegram_user_id"], reply_to_message_id)
            if expired is not None:
                await _reply(
                    employee["telegram_user_id"],
                    run_id,
                    f"⏰ {expired['report_date'].strftime('%d.%m')} кунги ҳисобот муддати тугаган — "
                    "ҳисоботлар ўша куни соат 24:00 гача қабул қилинади.",
                )
                return "daily_report_expired"

        # Numbers arriving a minute after a report already sent in words:
        # merge them so the scorecard is complete, and still fall through so
        # the Director sees the message itself.
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
    # or the 17:00 reminder is the report, a reply to a task card is a task
    # update, and a plain message is the report — unless a task is in flight,
    # when the bot asks with one tap instead of guessing.
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
    open_tasks = [] if replied_to_ask or replied_to_task else await store.open_tasks_for_employee(telegram_user_id)
    kind = report_message_kind(replied_to_ask, replied_to_task, len(open_tasks))
    if kind == "task_update":
        return None
    if kind == "ask":
        return await _ask_report_or_relay(employee, text, open_tasks[0] if len(open_tasks) == 1 else None, run_id)
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

    # A vague report ("ok", "ishladim") is already saved — the employee has
    # reported — but gets ONE short question. Whatever comes back is added to
    # the report and nothing more is asked.
    follow_up = await _weak_report_follow_up(text, run_id)
    if follow_up:
        await store.mark_report_followup(str(saved["id"]))
        await _reply(employee["telegram_user_id"], run_id, f"📝 {escape(follow_up)}")
        return "daily_report_weak"

    ack = "✅ Ҳисобот қабул қилинди, раҳмат!"
    missing = kpi.missing_metrics(values, metrics_def)
    if missing:
        ack += (
            f"\n\n⚠️ Рақамлар топилмади: {escape(', '.join(missing))}.\n"
            "Рақамларни шу ерга юборсангиз, ҳисоботингизга қўшаман."
        )
    await _reply(employee["telegram_user_id"], run_id, ack)
    return "daily_report"


async def _ask_report_or_relay(
    employee: dict[str, Any], text: str, task: dict[str, Any] | None, run_id: uuid.UUID
) -> str:
    """Ask whether a message is today's report or a message for the Director."""
    pending = await store.create_pending_relay(employee["telegram_user_id"], text, str(task["id"]) if task else None)
    preview = text if len(text) <= 300 else text[:299].rstrip() + "…"
    await _reply(
        employee["telegram_user_id"],
        run_id,
        f"📝 <b>Бу хабар — бугунги ҳисоботингизми?</b>\n\n«{escape(preview)}»",
        report_or_relay_keyboard(str(pending["id"])),
    )
    return "report_or_relay_asked"


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
        status_line = "📝 Кунлик ҳисобот сифатида сақланди."
    elif await store.submitted_report_today(clicker_id, day) is not None:
        status_line = "✅ Бугунги ҳисоботингиз аллақачон қабул қилинган."
        outcome = "report_already_submitted"
    else:
        status_line = "⏰ Ҳисобот қабул қилинмади — ҳисоботлар ўша куни соат 24:00 гача қабул қилинади."
        outcome = "report_window_closed"

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]),
                message_id=message["message_id"],
                text=f"{status_line}\n\n«{escape(relay['message_text'][:300])}»",
                reply_markup={"inline_keyboard": []},
            )
        await bot._answer_callback(query_id, "OK")  # noqa: SLF001
    return outcome


# -------------------------------------------------------------- employee updates


async def _handle_employee_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str:
    """Relay a non-Director employee's free-text message to the Director.

    Employees can write at any time about anything — before starting a
    task, mid-task, after finishing, or something with no task behind it at
    all. A message that resolves to a specific task (via reply-to-card, or
    their one open task) is attributed to it with a stage label; anything
    else is relayed as a general message instead of refused — being able to
    talk to the Director through this bot shouldn't require an open task to
    exist.

    Nothing is relayed straight away: the employee first gets "Бу хабар
    директорга юборилсинми?" and it goes only on "✅ Ҳа, юбориш" (2026-09-25).
    Daily reports and permission requests have their own flows and never
    reach this point.
    """
    telegram_user_id = employee["telegram_user_id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return "ignored"

    reply_to = message.get("reply_to_message") or {}
    outcome = await _try_daily_report(employee, text, reply_to.get("message_id"), run_id)
    if outcome is not None:
        return outcome

    task = None
    if reply_to.get("message_id"):
        task = await store.find_task_by_message_id(reply_to["message_id"], telegram_user_id)
    if task is None:
        task = await store.find_open_task_for_employee(telegram_user_id)

    if not await store.active_employees_by_role(DIRECTOR_ROLE):
        await _reply(telegram_user_id, run_id, "Ҳозирча директор рўйхатдан ўтмаган — хабарингиз етказилмади.")
        return "no_director"

    # Nothing reaches the Director without the employee confirming it: one
    # stray message to the CEO is one too many (the business's rule).
    pending = await store.create_pending_relay(telegram_user_id, text, str(task["id"]) if task else None)
    preview = text if len(text) <= 300 else text[:299].rstrip() + "…"
    prompt = "📨 <b>Бу хабар директорга юборилсинми?</b>"
    if task is not None:
        prompt += f"\n<i>Топшириқ: {escape(_plain(task['task_summary'])[:120])}</i>"
    prompt += f"\n\n«{escape(preview)}»"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Ҳа, юбориш", "callback_data": f"relayok:{pending['id']}"},
                {"text": "❌ Йўқ", "callback_data": f"relayno:{pending['id']}"},
            ]
        ]
    }
    await _reply(telegram_user_id, run_id, prompt, keyboard)
    return "relay_confirm_asked"


def _plain(text: str | None) -> str:
    """AI-written text without its <b>/<i> tags, on one line — for previews."""
    return " ".join(re.sub(r"<[^>]+>", "", text or "").split())


# Held messages expire: a tap days later shouldn't deliver something stale.
RELAY_CONFIRM_HOURS = 24


async def _handle_relay_decision(relay_id: str, send: bool, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """The employee confirmed (or cancelled) sending a held message to the Director."""
    query_id = callback.get("id", "")
    clicker_id = callback.get("from", {}).get("id")
    relay = await store.resolve_pending_relay(relay_id, clicker_id, "sent" if send else "cancelled")
    if relay is None:
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "relay_already_resolved"

    expired = (now_utc() - relay["created_at"]).total_seconds() > RELAY_CONFIRM_HOURS * 3600
    if send and expired:
        status_line = "⌛ Муддати ўтди — хабарни қайта ёзиб юборинг."
        outcome = "relay_expired"
    elif send:
        employee = await store.get_employee_by_telegram_id(clicker_id)
        task = await store.get_task(str(relay["task_id"])) if relay.get("task_id") else None
        delivered = await _relay_to_director(employee, relay["message_text"], task, run_id) if employee else 0
        status_line = "✅ Директорга юборилди." if delivered else "⚠️ Директорга етказиб бўлмади."
        outcome = "relayed" if delivered else "relay_failed"
    else:
        status_line = "❌ Юборилмади."
        outcome = "relay_cancelled"

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]),
                message_id=message["message_id"],
                text=f"{status_line}\n\n«{escape(relay['message_text'][:300])}»",
                reply_markup={"inline_keyboard": []},
            )
        await bot._answer_callback(query_id, "OK")  # noqa: SLF001
    return outcome


async def _relay_to_director(
    employee: dict[str, Any], text: str, task: dict[str, Any] | None, run_id: uuid.UUID
) -> int:
    """Deliver an employee's confirmed message to every Director.

    Returns:
        How many Directors received it.
    """
    who = f"<b>{escape(names.person_name(employee))}</b> ({escape(ROLE_LABELS.get(employee['role'], employee['role']))})"
    if task is not None:
        stage_label = {"sent": "бошланмаган", "started": "давом этмоқда", "done": "бажарилган"}.get(
            task["status"], task["status"]
        )
        relay_text = (
            f"💬 {who}, топшириқ {stage_label}:\n{escape(text)}\n\n"
            f"<i>Топшириқ: {escape(_plain(task['task_summary']))}</i>"
        )
    else:
        relay_text = f"💬 {who}:\n{escape(text)}"

    delivered = 0
    for director in await store.active_employees_by_role(DIRECTOR_ROLE):
        director_id = director["telegram_user_id"]
        try:
            message_ids = await _reply(director_id, run_id, relay_text)
        except TelegramError as exc:
            log.warning("Could not relay employee message to Director {}: {}", director_id, exc)
            continue
        delivered += 1
        await store.create_task_update(
            task_id=str(task["id"]) if task else None,
            employee_telegram_user_id=employee["telegram_user_id"],
            message_text=text,
            director_telegram_user_id=director_id,
            director_message_id=message_ids[0] if message_ids else None,
        )
    return delivered


# ----------------------------------------------------- director task routing


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
            await _dispatch_to_role(
                director_telegram_user_id, source_message_id, raw_message, target_role, task_summary, run_id,
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
            agent=AGENT, action="dispatch_task", target_system=settings.ops_manager_bot_provider,
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


async def _dispatch_to_role(
    director_id: int,
    source_message_id: int | None,
    raw_message: str,
    role_slug: str,
    task_summary: str,
    run_id: uuid.UUID,
    due_date: date | None = None,
    person: dict[str, Any] | None = None,
) -> None:
    """Create + send one task card per active employee holding ``role_slug``.

    When the Director named one person, only that person gets it.
    """
    employees = [person] if person is not None else await store.active_employees_by_role(role_slug)
    if not employees:
        await _reply_and_log(
            director_id,
            run_id,
            f"{ROLE_LABELS[role_slug]} учун ҳали ҳеч ким рўйхатдан ўтмаган.",
        )
        return

    sent_names: list[str] = []
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for employee in employees:
            task = await store.create_task(
                director_telegram_user_id=director_id,
                source_message_id=source_message_id or 0,
                raw_message=raw_message,
                target_type="employee",
                target_role=role_slug,
                target_agent=None,
                assigned_employee_id=str(employee["id"]),
                task_summary=task_summary,
                due_date=due_date,
            )
            if task is None:
                continue  # already dispatched -- duplicate webhook delivery

            message_ids = await bot.send_message(
                _task_card_text(task_summary, raw_message, due_date),
                chat_id=str(employee["telegram_user_id"]),
                reply_markup=_task_keyboard(str(task["id"])),
            )
            if message_ids:
                await store.set_task_message_id(str(task["id"]), message_ids[0])
            sent_names.append(names.person_name(employee))

    if sent_names:
        await _confirm_dispatch(director_id, source_message_id, sent_names, role_slug, due_date, run_id)


async def _confirm_dispatch(
    director_id: int,
    source_message_id: int | None,
    sent_names: list[str],
    role_slug: str,
    due_date: date | None,
    run_id: uuid.UUID,
) -> None:
    """Tell the Director who got the task — and ask for a deadline if none was stated.

    A3 needs "who, what, by when" for every task. The deadline is never
    guessed: when the Director didn't state one, they get one-tap choices,
    and "Muddatsiz" (no deadline) is an answer too.
    """
    who = ", ".join(escape(n) for n in sent_names)
    text = f"Юборилди: {who} ({ROLE_LABELS[role_slug]})."
    if due_date is not None:
        text += f"\n{task_tracker.deadline_line(due_date, today_local())}"
        await _reply_and_log(director_id, run_id, text)
        return
    if not source_message_id:
        await _reply_and_log(director_id, run_id, text)
        return
    text += "\n⏰ Муддат кўрсатилмади — қачонгача?"
    await _reply(director_id, run_id, text, task_tracker.deadline_keyboard(source_message_id))
    await store.log_conversation_turn(director_id, "bot", text)


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
            await _dispatch_media_to_role(
                director_telegram_user_id, source_message_id, caption or "Медиа файл", validated[1], run_id,
                media_due, media_person,
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
    """Park a media dispatch and ask the Director which role should get it."""
    row = await store.create_pending_dispatch(
        director_telegram_user_id=director_telegram_user_id, source_message_id=source_message_id, caption=caption
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": role.label, "callback_data": f"dispatchrole:{role.slug}:{row['id']}"}] for role in ROLES
        ]
    }
    await _reply(director_telegram_user_id, run_id, "Кимга юборилсин?", keyboard)


async def _handle_dispatch_role(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Resolve a pending media dispatch's role-picker button press."""
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
    await _dispatch_media_to_role(
        pending["director_telegram_user_id"],
        pending["source_message_id"],
        pending.get("caption") or "Медиа файл",
        role_slug,
        run_id,
    )
    return "dispatched"


async def _dispatch_media_to_role(
    director_id: int,
    source_message_id: int,
    task_summary: str,
    role_slug: str,
    run_id: uuid.UUID,
    due_date: date | None = None,
    person: dict[str, Any] | None = None,
) -> None:
    """Create + copy one task card per active employee holding ``role_slug``.

    Mirrors ``_dispatch_to_role`` but delivers via ``copyMessage`` (which
    duplicates the Director's original media into each recipient's chat)
    instead of ``sendMessage`` — the same ``tasks`` row/Start/Done tracking
    applies either way, distinguished only by the ``has_media`` flag. When the
    Director named one person, only that person gets it.
    """
    employees = [person] if person is not None else await store.active_employees_by_role(role_slug)
    if not employees:
        await _reply_and_log(
            director_id,
            run_id,
            f"{ROLE_LABELS[role_slug]} учун ҳали ҳеч ким рўйхатдан ўтмаган.",
        )
        return

    sent_names: list[str] = []
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for employee in employees:
            task = await store.create_task(
                director_telegram_user_id=director_id,
                source_message_id=source_message_id,
                raw_message=task_summary,
                target_type="employee",
                target_role=role_slug,
                target_agent=None,
                assigned_employee_id=str(employee["id"]),
                task_summary=task_summary,
                has_media=True,
                due_date=due_date,
            )
            if task is None:
                continue  # already dispatched -- duplicate webhook delivery

            result = await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                "copyMessage",
                {
                    "chat_id": str(employee["telegram_user_id"]),
                    "from_chat_id": str(director_id),
                    "message_id": source_message_id,
                    "caption": _task_card_text(task_summary, due_date=due_date),
                    "parse_mode": "HTML",
                    "reply_markup": _task_keyboard(str(task["id"])),
                },
                mode="notify",
                target_ref=str(employee["telegram_user_id"]),
            )
            if result and result.get("message_id"):
                await store.set_task_message_id(str(task["id"]), result["message_id"])
            sent_names.append(names.person_name(employee))

    if sent_names:
        await _confirm_dispatch(director_id, source_message_id, sent_names, role_slug, due_date, run_id)


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
        provider_override=settings.ops_manager_bot_provider,
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
            "(except the Director) at 16:00 Asia/Tashkent, Mon-Fri, and rows appear from that "
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

    # E1: each person's last 30 days, by the same rules as the monthly KPI.
    start = today - timedelta(days=30)
    kpis = task_tracker.employee_kpis(
        await store.reports_between(start, today), await store.tasks_due_between(start, today), start, today
    )
    if kpis:
        lines.append("PER-EMPLOYEE KPI, last 30 days (reports sent/asked, on time before 18:00; tasks on time/due):")
        for person in kpis:
            reports = (
                f"reports {person.reports.reported}/{person.reports.asked} ({person.reports.on_time} on time)"
                if person.reports.asked
                else "reports: not asked"
            )
            tasks = f"tasks {person.tasks.on_time}/{person.tasks.due} on time" if person.tasks.due else "tasks: none due"
            lines.append(f"- {person.mark} {person.name}: {reports}; {tasks}")
    return "\n".join(lines)


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


LEAD_SHEET_COLUMNS = [
    "company_name", "project_name", "industry", "location", "project_stage",
    "estimated_opening", "signal", "signal_source_url", "signal_date",
    "estimated_size", "contact_name", "contact_role", "contact_method",
    "confidence", "priority", "recheck_date", "notes", "date_added",
    "dedupe_key", "track",
]


async def _fetch_lead_agent_data() -> str:
    """Every lead, every column — Gemini 3.8 Flash's 1M-token context makes
    the old 15-row/4-column preview an unnecessary limitation."""
    try:
        async with SheetsClient(agent=AGENT) as sheets:
            rows = await sheets.get_values("Sheet1!A:T")
    except SheetsError as exc:
        return f"(could not read the leads sheet: {exc})"

    data_rows = rows[1:] if len(rows) > 1 else []
    if not data_rows:
        return "No leads recorded yet."

    lines = [f"{len(data_rows)} total leads on record, numbered in sheet order:"]
    for i, row in enumerate(data_rows, start=1):
        fields = {LEAD_SHEET_COLUMNS[j]: (row[j] if j < len(row) else "") for j in range(len(LEAD_SHEET_COLUMNS))}
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
