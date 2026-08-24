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

import uuid
from typing import Any

from fastapi import BackgroundTasks

from integrations.ai.openrouter_client import OpenRouterClient, OpenRouterError
from integrations.common.config import settings
from integrations.common.db import fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_uzs
from integrations.google.sheets_client import SheetsClient, SheetsError
from integrations.org_bot import admin, store
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
# can exercise it directly, the same way it already imports _extract_lead_events
# from webhook_handler.py.


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
        await _answer(query_id, "Unrecognized action")
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

    await _answer(query_id, "Unrecognized action")
    return "unrecognized"


def _task_card_text(task_summary: str, raw_message: str | None = None) -> str:
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
    text = f"📋 <b>Yangi topshiriq / New task</b>\n\n{sanitize_model_html(task_summary)}"
    if raw_message and raw_message.strip() and raw_message.strip() != task_summary.strip():
        text += f"\n\n<i>Direktordan / From the Director:</i>\n{escape(raw_message.strip())}"
    return text


def _task_keyboard(task_id: str, started: bool = False) -> dict[str, Any]:
    """The Start+Done (or just Done, once started) inline keyboard."""
    buttons = []
    if not started:
        buttons.append({"text": "▶️ Boshladim / Start", "callback_data": f"taskstart:{task_id}"})
    buttons.append({"text": "✅ Bajardim / Done", "callback_data": f"taskdone:{task_id}"})
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


async def send_role_picker(access_request: dict[str, Any], run_id: uuid.UUID) -> None:
    """Send an approved requester their role-picker keyboard.

    Called from ``admin.py`` after an Accept decision — deliberately on OPS
    Manager Bot's token, not Admin Bot's, since the requester already started
    a chat with this bot (see module docstring).

    Args:
        access_request: The now-approved ``access_requests`` row.
        run_id: UUID grouping this webhook call's audit rows.
    """
    keyboard = role_picker_keyboard(str(access_request["id"]))
    text = "🎉 Tasdiqlandingiz! Rolingizni tanlang:\n🎉 You're approved! Pick your role:"
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot.send_message(text, chat_id=str(access_request["telegram_user_id"]), reply_markup=keyboard)


async def _handle_set_role(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Resolve a role-picker button press into a new ``employees`` row."""
    query_id = callback.get("id", "")
    parsed = parse_role_and_request(rest)
    if parsed is None:
        await _answer(query_id, "Unrecognized action")
        return "unrecognized"

    role_slug, request_id = parsed
    if role_slug not in ROLE_SLUGS:
        await _answer(query_id, "Unrecognized role")
        return "unrecognized"

    request = await store.get_access_request(request_id)
    if request is None or request["status"] != "approved":
        await _answer(query_id, "Request not found or not approved")
        return "not_found"

    clicker = callback.get("from", {})
    if clicker.get("id") != request["telegram_user_id"]:
        await _answer(query_id, "This isn't your request")
        return "unauthorized"

    employee = await store.create_employee(
        telegram_user_id=request["telegram_user_id"],
        telegram_username=request.get("telegram_username"),
        display_name=request.get("display_name") or str(request["telegram_user_id"]),
        role=role_slug,
        approved_by=request.get("decided_by"),
    )

    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        message = callback.get("message") or {}
        if message.get("message_id") and message.get("chat", {}).get("id"):
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]),
                message_id=message["message_id"],
                text=f"✅ Siz <b>{escape(ROLE_LABELS[role_slug])}</b> sifatida ro'yxatdan o'tdingiz.",
            )
        await bot._answer_callback(query_id, "Registered!")  # noqa: SLF001

    await log_action(
        agent=AGENT,
        action="employee_registered",
        target_system="telegram",
        status="success",
        run_id=run_id,
        target_ref=str(employee["id"]),
        mode="write",
        payload={"role": role_slug, "telegram_user_id": request["telegram_user_id"]},
    )
    log.info("Employee {} registered as {}", employee["id"], role_slug)
    return "registered"


async def _actor_display_name(clicker: dict[str, Any]) -> str:
    """A human-readable name for whoever tapped a button, for Director-facing
    notifications — never the raw numeric Telegram id: it's meaningless to
    the Director and, wrapped in "(@...)", reads as a broken mention rather
    than the harmless fallback it was meant to be.
    """
    telegram_user_id = clicker.get("id")
    if telegram_user_id is not None:
        employee = await store.get_employee_by_telegram_id(telegram_user_id)
        if employee and employee.get("display_name"):
            return employee["display_name"]
    username = clicker.get("username")
    if username:
        return f"@{username}"
    name = " ".join(filter(None, [clicker.get("first_name"), clicker.get("last_name")]))
    return name or "Noma'lum xodim / Unknown employee"


async def _handle_task_start(task_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Resolve a "Start" button press and notify the Director."""
    query_id = callback.get("id", "")
    clicker = callback.get("from", {})
    started_by = clicker.get("username") or str(clicker.get("id", "unknown"))

    task = await store.mark_task_started(task_id, started_by)
    if task is None:
        await _answer(query_id, "Already started or done")
        return "already_started"

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            text = _task_card_text(task["task_summary"], task.get("raw_message")) + "\n\n▶️ Boshlandi / Started"
            keyboard = _task_keyboard(str(task["id"]), started=True)
            await _edit_task_card(
                bot, str(message["chat"]["id"]), message["message_id"], bool(task.get("has_media")), text, keyboard
            )
        await bot._answer_callback(query_id, "Started")  # noqa: SLF001

    try:
        actor = await _actor_display_name(clicker)
        await _reply(
            task["director_telegram_user_id"],
            run_id,
            f"▶️ Boshlandi / Started: {sanitize_model_html(task['task_summary'])}\n— {escape(actor)}",
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
        await _answer(query_id, "Already marked done")
        return "already_done"

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            text = _task_card_text(task["task_summary"], task.get("raw_message")) + "\n\n✅ Bajarildi / Done"
            await _edit_task_card(
                bot, str(message["chat"]["id"]), message["message_id"], bool(task.get("has_media")), text,
                {"inline_keyboard": []},
            )
        await bot._answer_callback(query_id, "Marked done")  # noqa: SLF001

    try:
        actor = await _actor_display_name(clicker)
        await _reply(
            task["director_telegram_user_id"],
            run_id,
            f"✅ Bajarildi / Done: {sanitize_model_html(task['task_summary'])}\n— {escape(actor)}",
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
        await _reply(employee_telegram_user_id, run_id, f"💬 Direktordan / From the Director:\n{escape(text)}")
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
            "So'rovingiz hali ko'rib chiqilmoqda.\nYour request is still pending approval.",
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
        "So'rovingiz adminga yuborildi.\nYour request has been sent for approval.",
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
    """
    telegram_user_id = employee["telegram_user_id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return "ignored"

    reply_to = message.get("reply_to_message") or {}
    task = None
    if reply_to.get("message_id"):
        task = await store.find_task_by_message_id(reply_to["message_id"], telegram_user_id)
    if task is None:
        task = await store.find_open_task_for_employee(telegram_user_id)

    if task is not None:
        stage_label = {"sent": "boshlanmagan", "started": "davom etmoqda", "done": "bajarilgan"}.get(
            task["status"], task["status"]
        )
        relay_text = (
            f"💬 {escape(employee['display_name'])} ({stage_label}):\n{escape(text)}\n\n"
            f"<i>Topshiriq / Task: {escape(task['task_summary'])}</i>"
        )
    else:
        relay_text = f"💬 {escape(employee['display_name'])}:\n{escape(text)}"

    directors = await store.active_employees_by_role(DIRECTOR_ROLE)
    if not directors:
        await _reply(
            telegram_user_id,
            run_id,
            "Hozircha Direktor ro'yxatdan o'tmagan — xabaringiz yetkazilmadi.\n"
            "No Director is registered yet — your message wasn't delivered.",
        )
        return "no_director"

    for director in directors:
        director_id = director["telegram_user_id"]
        try:
            message_ids = await _reply(director_id, run_id, relay_text)
        except TelegramError as exc:
            log.warning("Could not relay employee message to Director {}: {}", director_id, exc)
            continue
        await store.create_task_update(
            task_id=str(task["id"]) if task else None,
            employee_telegram_user_id=telegram_user_id,
            message_text=text,
            director_telegram_user_id=director_id,
            director_message_id=message_ids[0] if message_ids else None,
        )

    await _reply(telegram_user_id, run_id, "👍 Qabul qildim, direktorga yubordim. / Got it, sent to the Director.")
    return "relayed"


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
        async with OpenRouterClient(
            agent=AGENT,
            run_id=run_id,
            provider_override=settings.ops_manager_bot_provider,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            result = await ai.complete_json(CLASSIFY_SYSTEM_PROMPT, build_classify_message(raw_message, history))

        task_summary = (result.get("task_summary") or raw_message[:200]).strip()
        validated = validate_classification(result)

        if validated is None:
            # Model returned an out-of-enum value -- code-level backstop,
            # the proportionate equivalent of Lead Agent's second AI pass for
            # this much narrower classification task.
            log.warning("Classification returned unrecognized target: {}", result)
            await _reply_and_log(
                director_telegram_user_id,
                run_id,
                "Aniq tushunmadim — iltimos, aniqroq yozing.",
            )
            return

        target_type, target_role, target_agent = validated
        log.info(
            "Classified '{}' -> type={} role={} agent={}",
            raw_message[:120], target_type, target_role, target_agent,
        )
        if target_type == "employee":
            await _dispatch_to_role(
                director_telegram_user_id, source_message_id, raw_message, target_role, task_summary, run_id
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
                sanitize_model_html(task_summary) or "Buni kimga yo'naltirishni tushunmadim — aniqroq yozib bera olasizmi?",
            )

    except OpenRouterError as exc:
        log.error("Classification failed for source_message_id={}: {}", source_message_id, exc)
        await log_action(
            agent=AGENT, action="dispatch_task", target_system="deepseek",
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


async def _safe_notify_failure(director_telegram_user_id: int, run_id: uuid.UUID) -> None:
    """Best-effort failure notice — itself guarded so it can't raise."""
    try:
        await _reply(
            director_telegram_user_id,
            run_id,
            "Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.\nSomething went wrong — please try again.",
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
) -> None:
    """Create + send one task card per active employee holding ``role_slug``."""
    employees = await store.active_employees_by_role(role_slug)
    if not employees:
        await _reply_and_log(
            director_id,
            run_id,
            f"{ROLE_LABELS[role_slug]} uchun hali hech kim ro'yxatdan o'tmagan.",
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
            )
            if task is None:
                continue  # already dispatched -- duplicate webhook delivery

            message_ids = await bot.send_message(
                _task_card_text(task_summary, raw_message),
                chat_id=str(employee["telegram_user_id"]),
                reply_markup=_task_keyboard(str(task["id"])),
            )
            if message_ids:
                await store.set_task_message_id(str(task["id"]), message_ids[0])
            sent_names.append(employee["display_name"])

    if sent_names:
        names = ", ".join(escape(n) for n in sent_names)
        await _reply_and_log(director_id, run_id, f"Yuborildi: {names} ({ROLE_LABELS[role_slug]}).")


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
    if caption:
        try:
            async with OpenRouterClient(
                agent=AGENT,
                run_id=run_id,
                provider_override=settings.ops_manager_bot_provider,
                model_override=settings.ops_manager_bot_model,
                fallback_override=settings.ops_manager_bot_fallback_models,
            ) as ai:
                result = await ai.complete_json(CLASSIFY_SYSTEM_PROMPT, build_classify_message(caption))
            validated = validate_classification(result)
            if validated is not None and validated[0] == "refused":
                refusal_text = (result.get("task_summary") or "").strip() or None
        except OpenRouterError as exc:
            log.warning("Media caption classification failed, falling back to a role picker: {}", exc)

    try:
        if validated is not None and validated[0] == "employee":
            await _dispatch_media_to_role(
                director_telegram_user_id, source_message_id, caption or "Media fayl / Media file", validated[1], run_id
            )
        elif validated is not None and validated[0] == "refused":
            # Guardrail path — refuse and stop, same as the text-task flow.
            # Do NOT fall through to the role picker: an inappropriate
            # caption shouldn't still get its attached file forwarded.
            log.info("Media caption classification refused a message from {}", director_telegram_user_id)
            await _reply(
                director_telegram_user_id, run_id,
                sanitize_model_html(refusal_text) if refusal_text else "Kechirasiz, bunga yordam bera olmayman.",
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
    await _reply(director_telegram_user_id, run_id, "Kimga yuborilsin? / Who should receive this?", keyboard)


async def _handle_dispatch_role(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Resolve a pending media dispatch's role-picker button press."""
    query_id = callback.get("id", "")
    parsed = parse_role_and_request(rest)
    if parsed is None:
        await _answer(query_id, "Unrecognized action")
        return "unrecognized"

    role_slug, pending_id = parsed
    if role_slug not in ROLE_SLUGS:
        await _answer(query_id, "Unrecognized role")
        return "unrecognized"

    pending = await store.resolve_pending_dispatch(pending_id)
    if pending is None:
        await _answer(query_id, "Already handled or not found")
        return "not_found"

    clicker = callback.get("from", {})
    if clicker.get("id") != pending["director_telegram_user_id"]:
        await _answer(query_id, "This isn't your request")
        return "unauthorized"

    await _answer(query_id, "OK")
    await _dispatch_media_to_role(
        pending["director_telegram_user_id"],
        pending["source_message_id"],
        pending.get("caption") or "Media fayl / Media file",
        role_slug,
        run_id,
    )
    return "dispatched"


async def _dispatch_media_to_role(
    director_id: int, source_message_id: int, task_summary: str, role_slug: str, run_id: uuid.UUID
) -> None:
    """Create + copy one task card per active employee holding ``role_slug``.

    Mirrors ``_dispatch_to_role`` but delivers via ``copyMessage`` (which
    duplicates the Director's original media into each recipient's chat)
    instead of ``sendMessage`` — the same ``tasks`` row/Start/Done tracking
    applies either way, distinguished only by the ``has_media`` flag.
    """
    employees = await store.active_employees_by_role(role_slug)
    if not employees:
        await _reply_and_log(
            director_id,
            run_id,
            f"{ROLE_LABELS[role_slug]} uchun hali hech kim ro'yxatdan o'tmagan.",
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
            )
            if task is None:
                continue  # already dispatched -- duplicate webhook delivery

            result = await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                "copyMessage",
                {
                    "chat_id": str(employee["telegram_user_id"]),
                    "from_chat_id": str(director_id),
                    "message_id": source_message_id,
                    "caption": _task_card_text(task_summary),
                    "parse_mode": "HTML",
                    "reply_markup": _task_keyboard(str(task["id"])),
                },
                mode="notify",
                target_ref=str(employee["telegram_user_id"]),
            )
            if result and result.get("message_id"):
                await store.set_task_message_id(str(task["id"]), result["message_id"])
            sent_names.append(employee["display_name"])

    if sent_names:
        names = ", ".join(escape(n) for n in sent_names)
        await _reply_and_log(director_id, run_id, f"Yuborildi: {names} ({ROLE_LABELS[role_slug]}).")


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
    — Sonnet 5's context window makes this a non-issue size-wise; the
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
        "lead_agent": _fetch_lead_agent_data,
        "finance_agent": _fetch_finance_agent_data,
        "crm_agent": _fetch_crm_agent_data,
        "reporter_agent": _fetch_reporter_agent_data,
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


LEAD_SHEET_COLUMNS = [
    "company_name", "project_name", "industry", "location", "project_stage",
    "estimated_opening", "signal", "signal_source_url", "signal_date",
    "estimated_size", "contact_name", "contact_role", "contact_method",
    "confidence", "priority", "recheck_date", "notes", "date_added",
    "dedupe_key", "track",
]


async def _fetch_lead_agent_data() -> str:
    """Every lead, every column — Claude Sonnet 5's 200k context makes the
    old 15-row/4-column preview an unnecessary limitation (it was sized for
    DeepSeek's much smaller effective window and cost per token)."""
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
        "SELECT doc_num, card_name, days_overdue, aging_bucket, balance_due_uzs, currency, due_date, sales_person_name "
        "FROM v_ar_aging_latest ORDER BY balance_due_uzs DESC LIMIT 200"
    )
    alerts = await fetch_all(
        "SELECT title, body, created_at FROM alerts WHERE agent = 'receivables' ORDER BY created_at DESC LIMIT 30"
    )
    if not aging and not alerts:
        return "No receivables data recorded yet."

    # Say "invoice" explicitly, not just "receivable" -- a receivable *is*
    # an open SAP invoice, but a Director asking specifically for "invoices"
    # got refused once because the data was only ever labeled "receivable,"
    # with no invoice number shown, so the model didn't recognize its own
    # data as the answer to that exact question.
    lines = [f"{len(aging)} open invoice(s) / receivable(s):"]
    for r in aging:
        # currency is whatever SAP actually recorded on the invoice -- never
        # hardcode "UZS" here, some invoices are genuinely in USD/EUR and
        # mislabeling them is worse than an ugly currency code.
        lines.append(
            f"- Invoice #{r['doc_num']}, {r['card_name']}: {r['balance_due_uzs']} {r['currency'] or 'UZS'}, "
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


async def _fetch_crm_agent_data() -> str:
    """The full pipeline snapshot from the in-house CRM.

    `amocrm_pipeline_snapshots` (behind `v_pipeline_latest`) despite its
    legacy name is what the IN-HOUSE CRM writes to, once a day, via
    ceo-daily-brief's own `_fetch_crm -> persist_crm_pipeline` — the old
    amoCRM code path never wrote here and is unrelated. `amocrm_deal_events`
    (a webhook-driven amoCRM-only event log) has no in-house-CRM equivalent
    and is deliberately NOT queried here — it would only ever report stale
    amoCRM data from before the migration, which is worse than reporting
    nothing.
    """
    pipeline = await fetch_all(
        "SELECT pipeline_name, status_name, deals_count, deals_value_uzs, division, snapshot_date "
        "FROM v_pipeline_latest"
    )
    if not pipeline:
        return (
            "No CRM pipeline data recorded yet. This is populated once a day by the "
            "CEO Daily Brief agent's own CRM read — if that's been failing, the most "
            "likely cause is CRM_API_KEY still being an unfilled placeholder."
        )

    lines = [f"Pipeline snapshot as of {pipeline[0]['snapshot_date']}:"]
    for p in pipeline:
        lines.append(
            f"- {p.get('pipeline_name')} / {p.get('status_name')} ({p.get('division')}): "
            f"{p.get('deals_count')} deal(s), {p.get('deals_value_uzs')} UZS"
        )
    return "\n".join(lines)


async def _fetch_reporter_agent_data() -> str:
    """The last 14 days of daily briefs, so trend questions aren't limited
    to a single snapshot the way a one-day-only fetch would be."""
    briefs = await fetch_all(
        "SELECT brief_date, cash_total_tiyin, ar_overdue_total_tiyin, pipeline_total_tiyin, "
        "new_leads_24h, deals_without_task, employees_late, employees_absent, planner_tasks_overdue "
        "FROM daily_briefs ORDER BY generated_at DESC LIMIT 14"
    )
    if not briefs:
        return "No daily brief has been generated yet."

    lines = [f"Daily brief history, most recent first ({len(briefs)} day(s)):"]
    for brief in briefs:
        lines.append(
            f"- {brief['brief_date']}: cash={format_uzs(brief['cash_total_tiyin'] or 0)}, "
            f"AR overdue={format_uzs(brief['ar_overdue_total_tiyin'] or 0)}, "
            f"pipeline={format_uzs(brief['pipeline_total_tiyin'] or 0)}, "
            f"new leads={brief['new_leads_24h']}, deals w/o task={brief['deals_without_task']}, "
            f"late/absent={brief['employees_late']}/{brief['employees_absent']}, "
            f"overdue Planner tasks={brief['planner_tasks_overdue']}"
        )
    return "\n".join(lines)
