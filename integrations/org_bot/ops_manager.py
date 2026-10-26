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

import asyncio
import json
import re
import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import BackgroundTasks

from integrations.ai.openrouter_client import OpenRouterClient, OpenRouterError
from integrations.common.config import settings
from integrations.common.db import fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_money, format_money_by_currency
from integrations.common.timeutil import now_local, to_local, today_local
from integrations.garmin import competitors
from integrations.google.sheets_client import SheetsClient, SheetsError
from integrations.org_bot import (
    admin, agenda, ai_chat, cheques, excused, explanations, late_report, old_stock, report_calendar, report_media, data_access, kpi, kpi_flow, kpi_score, leads, names, permission_flow, report_tools, store,
    task_picker, task_register, task_tracker,
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
from integrations.sap.models import doc_balance
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
    Reply to a task card is a question about that task (for the AI). A
    message with a "?" is a question and goes to the AI — never offered as
    the report (2026-10-17: a tap on «ҳа, ҳисобот» had turned questions into
    reports, mixing the reports and the AI chat). Any other message is
    confirmed with one tap — the bot never guesses a report (2026-10-02).

    Returns:
        "report", "task_question", "question" or "ask".
    """
    if replied_to_ask:
        return "report"
    if replied_to_task:
        return "task_question"
    if is_question:
        return "question"
    return "ask"


# 2026-10-26 (the owner): the AI decides whether a message is the report — asked only when it isn't sure; reports are
# taken from 14:00 (early workers; not before 14:00 — "employees might lie"), the 16:00 ask and 17:00 reminder are
# reminders; «ҳа» / «хоп» right after a held draft means «send it».
REPORT_OPEN_HOUR = 14
REPORT_MIN_CHARS = 12  # shorter than this is never a report («ок», «раҳмат»)
HELD_CONFIRM_MINUTES = 30
CONFIRM_WORDS = frozenset({"ха", "ҳа", "xa", "ha", "хоп", "хўп", "xop", "hop", "ок", "ok", "окей", "да", "тўғри", "тугри",
                           "togri", "to'g'ri", "ҳа шундай", "ha shunaqa", "ха шундай", "юбор", "юборинг", "yubor"})
REPORT_CHECK = """An employee of MGMG (Tashkent: Garmin shops, Primus laundry equipment, Londry laundries) wrote this \
message to the work bot at {time}. Is it their DAILY WORK REPORT — what they did at work (tasks done, customers, places, \
sales, numbers)? A question, a request for help, a greeting, thanks, chatter, a complaint or only a plan is not; nor is \
a message that only points elsewhere («the report above», «same as yesterday») or refuses to say. \
Answer "unsure" only when it could really be either. JSON only: {{"kind": "report"}}, {{"kind": "other"}} or \
{{"kind": "unsure"}}."""


# 2026-10-26: «Tepadagi xisobot» ("the report above") was saved as Абдукарим's 21.10 report — a pointer, no work in it.
POINTER = re.compile(
    r"(tepadagi|тепадаги|юқоридаги|юкоридаги|юқорида|yuqoridagi|yuqorida|кечагидек|kechagidek|кечаги\s+каби|"
    r"аввалгидек|oldingidek|олдингидек|выше|как\s+вчера|как\s+и\s+вчера|above|same\s+as)", re.IGNORECASE)
POINTER_MAX_CHARS = 60


def is_pointer(text: str) -> bool:
    """A short message that only points to a report elsewhere («тепадаги ҳисобот», «кечагидек», «выше»)."""
    text = " ".join((text or "").split())
    return len(text) <= POINTER_MAX_CHARS and bool(POINTER.search(text))


def pointer_text() -> str:
    return casual("ҳисоботни шу ерга бугун нима иш қилганингиз билан тўлиқ ёзинг, «тепадаги» ёки «кечагидек» деган "
                  "хабар ҳисобот сифатида сақланмайди, раҳмат")


def is_confirmation(text: str) -> bool:
    """«Ҳа», «Хоп.», «ok!» — a short yes."""
    return re.sub(r"[^\w' ]", "", (text or "").lower()).strip() in CONFIRM_WORDS


async def _classify_report(text: str, run_id: uuid.UUID) -> str:
    """«report», «other» or «unsure» — the AI decides; a short message is never a report; if the AI can't be
    reached: unsure (the person is asked, nothing is guessed)."""
    if len(" ".join(text.split())) < REPORT_MIN_CHARS:
        return "other"
    try:
        async with OpenRouterClient(agent=AGENT, run_id=run_id, model_override=settings.ops_manager_bot_model,
                                    fallback_override=settings.ops_manager_bot_fallback_models) as ai:
            verdict = await ai.complete_json(REPORT_CHECK.format(time=f"{now_local():%H:%M}"), text)
        kind = str(verdict.get("kind") or "unsure") if isinstance(verdict, dict) else "unsure"
    except Exception:  # noqa: BLE001
        return "unsure"
    return kind if kind in ("report", "other", "unsure") else "unsure"


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
        "none", "clarify" or "refused" — their text lives in
        ``task_summary``, not here) — else None. Callers must
        treat None the same as an explicit "none" (ask the Director to
        clarify), never as a silent default route.
    """
    target_type = result.get("target_type")
    target_role = result.get("target_role")
    target_agent = result.get("target_agent")

    if target_type in ("none", "refused", "clarify"):
        return target_type, None, None
    if target_type == "employee" and target_role in ROUTABLE_ROLE_SLUGS:
        return "employee", target_role, None
    if target_type == "agent" and target_agent in AGENT_SLUGS:
        return "agent", None, target_agent
    return None


CLARIFY_MAX_OPTIONS = 3
CLARIFY_OPTION_CHARS = 120   # what is stored and sent on; the button shows less
CLARIFY_LABEL_CHARS = 60


def clarify_options(result: dict[str, Any]) -> list[str]:
    """The meanings the router offered with a "clarify" — clean, distinct, at most three."""
    raw = result.get("options")
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    for item in raw:
        text = " ".join(re.sub(r"<[^>]+>", "", str(item or "")).split())[:CLARIFY_OPTION_CHARS]
        if text and text.lower() not in (f.lower() for f in found):
            found.append(text)
    return found[:CLARIFY_MAX_OPTIONS]


def clarify_keyboard(clarification_id: str, options: list[str]) -> dict[str, Any]:
    """One button per meaning, one per row (a button carries only its index)."""
    def label(text: str) -> str:
        return text if len(text) <= CLARIFY_LABEL_CHARS else text[:CLARIFY_LABEL_CHARS - 1].rstrip() + "…"

    return {"inline_keyboard": [[{"text": label(o), "callback_data": f"clr:{clarification_id}:{i}"}]
                                for i, o in enumerate(options)]}


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
    if (message.get("chat") or {}).get("type") not in (None, "private"):
        # A group (2026-10-26): only the lead groups' binding commands — never tasks, reports or join requests.
        from integrations.org_bot import lead_groups

        return await lead_groups.handle_group_message(message, run_id)

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
    if prefix in ("taskblock", "taskgo", "taskx"):
        return await _handle_task_step(prefix, rest, callback, run_id)
    if prefix in ("taskok", "taskback"):
        return await _handle_task_verify(prefix == "taskok", rest, callback, run_id)
    if prefix in ("cfok", "cfr", "cfno"):
        return await _handle_complaint_button(prefix, rest, callback, run_id)
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
    if prefix == "clr":
        return await _handle_clarification(rest, callback, run_id, background)
    if prefix == "dax":
        return await _handle_data_access_close(rest, callback, run_id)
    if prefix == "fp":
        return await _handle_file_purpose(rest, callback, run_id, background)
    if prefix == "chq":
        return await _handle_cheque_button(rest, callback, run_id)
    if prefix in ("rpe", "rpd", "rpdy", "rpn"):
        return await _handle_report_button(prefix, rest, callback, run_id)
    if prefix == "asrep":
        return await _handle_save_as_report(rest, callback, run_id)
    if prefix in ("ld", "lc"):
        # A button on an old 15:00 lead question: the hand-out was stopped by
        # the Director on 2026-10-07 — nothing is recorded, the buttons go.
        return await _handle_stopped_lead_button(callback, run_id)

    # Excused days: the explanation card's ⬜ / ↩ and /sababli (excused.py, 2026-10-23).
    excused_outcome = await excused.handle_callback(prefix, rest, callback, run_id)
    if excused_outcome is not None:
        return excused_outcome
    # Late reports: «send yesterday's report» and the deciders' ✅ / ❌ (late_report.py, 2026-10-23).
    late_outcome = await late_report.handle_callback(prefix, rest, callback, run_id)
    if late_outcome is not None:
        return late_outcome

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


def _task_card_text(task_summary: str, raw_message: str | None = None, due_date: date | None = None, code: str = "") -> str:
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
    text = f"📋 <b>Янги топшириқ{' · ' + escape(code) if code else ''}</b>\n\n{sanitize_model_html(task_summary)}"
    if raw_message and raw_message.strip() and raw_message.strip() != task_summary.strip():
        text += f"\n\n<i>Директордан:</i>\n{escape(raw_message.strip())}"
    deadline = task_tracker.deadline_line(due_date, today_local())
    if deadline:
        text += f"\n\n{deadline}"
    return text


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
            registered again starts afresh (``store.create_employee`` clears
            the name and settings, 2026-10-14) and is asked their name like
            anyone new — the account may now be someone else's.
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
    if access_request["requested_role"] in data_access.ROLE_SCOPES:
        # Data that comes with the role: say what opened, and how to ask (data_access.py).
        text += "\n\n" + data_access.employee_notice({**(employee or {}), "role": access_request["requested_role"],
                                                         "status": "active"})
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


async def _refresh_task_card(task: dict[str, Any], run_id: uuid.UUID, full: dict[str, Any] | None = None) -> None:
    """Redraw the employee's task card: text, code, where it is, the buttons for that state.

    ``full``: the task with the employee's ids, when the caller has it already (one query less).
    """
    full = full or await store.task_with_employee(str(task["id"]))
    if not full or not full.get("telegram_message_id"):
        return
    status = task_register.status_line(full)
    if full.get("has_media"):
        # A file's caption: at most 1024 characters — the summary shortened, never cut inside a tag.
        code = task_register.code_of(full)
        head = f"📋 <b>Янги топшириқ{' · ' + escape(code) if code else ''}</b>\n\n"
        deadline = task_tracker.deadline_line(full.get("due_date"), today_local())
        tail = (f"\n\n{deadline}" if deadline else "") + f"\n\n{status}"
        text = task_register.fit(head, task_register.plain(full["task_summary"]), tail)
    else:
        text = (_task_card_text(full["task_summary"], full.get("raw_message"), full.get("due_date"),
                                task_register.code_of(full)) + "\n\n" + status)
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await _edit_task_card(bot, str(full["employee_telegram_user_id"]), full["telegram_message_id"],
                              bool(full.get("has_media")), text, task_register.keyboard(full))


async def _tell_assigner(task: dict[str, Any], text: str, run_id: uuid.UUID) -> None:
    try:
        await _reply(task["director_telegram_user_id"], run_id, text)
    except TelegramError as exc:
        log.warning("Could not tell the assigner about {}: {}", task_register.code_of(task), exc)


async def _handle_task_start(task_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """▶️ Қабул қилдим: the employee accepted the task; the one who assigned it is told."""
    query_id = callback.get("id", "")
    clicker = callback.get("from", {})
    started_by = clicker.get("username") or str(clicker.get("id", "unknown"))

    task = await store.mark_task_started(task_id, started_by)
    if task is None:
        await _answer(query_id, "Аллақачон қабул қилинган")
        return "already_started"
    await _answer(query_id, "Қабул қилинди")
    await _refresh_task_card(task, run_id)
    actor = await _actor_display_name(clicker)
    await _tell_assigner(task, f"▶️ {escape(task_register.code_of(task))} қабул қилинди: "
                               f"{sanitize_model_html(task['task_summary'])}\n— {escape(actor)}", run_id)
    return "started"


async def _handle_task_done(task_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """✅ Бажардим: nothing closes yet — the bot asks for proof (the next message)."""
    query_id = callback.get("id", "")
    clicker_id = (callback.get("from") or {}).get("id")
    task = await store.await_task_input(task_id, "evidence", clicker_id) if clicker_id else None
    if task is None:
        await _answer(query_id, "Аллақачон юборилган ёки ёпилган")
        return "already_done"
    await _answer(query_id, "Далил юборинг")
    await _refresh_task_card(task, run_id)
    return "evidence_asked"


async def _handle_task_step(prefix: str, task_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """⛔ Тўсиқ бор (asks the reason), ▶️ Давом эттирдим, ↩️ Бекор қилиш (stop waiting for proof / reason)."""
    query_id = callback.get("id", "")
    clicker = callback.get("from") or {}
    clicker_id = clicker.get("id")
    if clicker_id is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    if prefix == "taskblock":
        task = await store.await_task_input(task_id, "blocker", clicker_id)
        outcome, toast = "blocker_asked", "Тўсиқни ёзинг"
    elif prefix == "taskgo":
        task = await store.unblock_task(task_id, clicker_id, clicker.get("username") or str(clicker_id))
        outcome, toast = "unblocked", "Давом этяпти"
    else:
        task = await store.cancel_task_input(task_id, clicker_id)
        outcome, toast = "input_cancelled", "Бекор қилинди"
    if task is None:
        await _answer(query_id, "Бу топшириқда бу амал мумкин эмас")
        return "task_step_refused"
    await _answer(query_id, toast)
    await _refresh_task_card(task, run_id)
    if prefix == "taskgo":
        actor = await _actor_display_name(clicker)
        await _tell_assigner(task, f"▶️ {escape(task_register.code_of(task))}: тўсиқ ҳал бўлди, давом этяпти — "
                                   f"{escape(actor)}", run_id)
    return outcome


async def _consume_task_input(employee: dict[str, Any], task: dict[str, Any], message: dict[str, Any],
                              run_id: uuid.UUID) -> str:
    """The message right after ✅ (proof) or ⛔ (reason): saved on the task, sent to the one who assigned it."""
    telegram_user_id = employee["telegram_user_id"]
    who = names.person_name(employee)
    by = employee.get("telegram_username") or str(telegram_user_id)
    if task.get("awaiting") == "blocker":
        reason = (message.get("text") or message.get("caption") or "").strip()[:task_register.BLOCKER_MAX]
        if not reason:
            await _reply(telegram_user_id, run_id, "Тўсиқни қисқа матн билан ёзинг.")
            return "blocker_needs_text"
        blocked = await store.block_task(str(task["id"]), by, reason)
        if blocked is None:
            return "task_input_stale"
        await _refresh_task_card(blocked, run_id)
        await _reply(telegram_user_id, run_id, "Тўсиқ ёзилди ва топшириқ берган кишига юборилди.")
        await _tell_assigner(blocked, task_register.blocked_text(blocked, who), run_id)
        return "task_blocked"

    evidence = task_register.evidence_of(message)
    if evidence is None:
        await _reply(telegram_user_id, run_id, "Далил сифатида фото, файл ёки қисқа матн юборинг.")
        return "evidence_empty"
    text, has_file = evidence
    submitted = await store.submit_task(str(task["id"]), by, text, message.get("message_id") if has_file else None)
    if submitted is None:
        return "task_input_stale"
    review = task_register.review_text(submitted, who)
    keyboard = task_register.review_keyboard(str(submitted["id"]))
    assigner = str(submitted["director_telegram_user_id"])
    review_id = None
    delivered = False
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if has_file:
            try:
                result = await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                    "copyMessage",
                    {"chat_id": assigner, "from_chat_id": str(telegram_user_id), "message_id": message.get("message_id"),
                     "caption": task_register.review_caption(submitted, who), "parse_mode": "HTML",
                     "reply_markup": keyboard},
                    mode="notify", target_ref=assigner,
                )
                review_id, delivered = (result or {}).get("message_id"), True
            except TelegramError as exc:
                log.warning("Could not copy {}'s proof for review, sending the text: {}",
                            task_register.code_of(submitted), exc)
        if not delivered:
            try:
                ids = await bot.send_message(review, chat_id=assigner, reply_markup=keyboard)
                review_id, delivered = (ids[0] if ids else None), True
            except TelegramError as exc:
                log.error("Could not send {} for review: {}", task_register.code_of(submitted), exc)
    if not delivered:
        # Nobody has the ✅ / ↩️ buttons: don't leave it stuck in review — ask for the proof again.
        reopened = await store.reopen_submission(str(submitted["id"]))
        if reopened is not None:
            await _refresh_task_card(reopened, run_id)
        await _reply(telegram_user_id, run_id, "Далилни текширувга юбориб бўлмади — бироздан кейин қайта юборинг.")
        return "task_review_failed"
    await _refresh_task_card(submitted, run_id)
    await _reply(telegram_user_id, run_id, f"Раҳмат, {escape(task_register.code_of(submitted))} текширувга юборилди.")
    await store.set_task_review_message(str(submitted["id"]), review_id)
    return "task_submitted"


async def _handle_task_verify(accept: bool, task_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """✅ Қабул (closed) or ↩️ Қайтариш (back to the employee) — only by the one who assigned it."""
    query_id = callback.get("id", "")
    clicker = callback.get("from") or {}
    clicker_id = clicker.get("id")
    task = await store.verify_task(task_id, clicker_id, accept, clicker.get("username") or str(clicker_id)) \
        if clicker_id else None
    if task is None:
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "verify_closed"
    await _answer(query_id, "Қабул қилинди" if accept else "Қайтарилди")
    code = escape(task_register.code_of(task))
    full = await store.task_with_employee(task_id)
    who = (full or {}).get("display_name") or ""
    message = callback.get("message") or {}
    if message.get("message_id") and (message.get("chat") or {}).get("id"):
        status = "✅ Қабул қилинди — ёпилди" if accept else "↩️ Қайтарилди — ходим қайта бажаради"
        async with TelegramBot(
            agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
        ) as bot:
            is_media = message.get("caption") is not None and message.get("text") is None
            text = (task_register.review_caption(task, who, status) if is_media
                    else f"{task_register.review_text(task, who)}\n\n{status}")
            await _edit_task_card(bot, str(message["chat"]["id"]), message["message_id"], is_media, text,
                                  {"inline_keyboard": []})
    await _refresh_task_card(task, run_id, full)
    if full:
        note = (f"✅ {code} қабул қилинди, раҳмат." if accept
                else f"↩️ {code} қайтарилди: қайта бажариб, ✅ Бажардим орқали янги далил юборинг.")
        await _reply(full["employee_telegram_user_id"], run_id, note)
    return "task_closed" if accept else "task_returned"


async def _handle_complaint_button(prefix: str, rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """A complaint ticket's buttons (feedback.py): ✅ → the reason → resolved on every card; ❌ → asked again later.

    Only someone the ticket was sent to can press them.
    """
    from integrations.org_bot import feedback

    query_id = callback.get("id", "")
    clicker = callback.get("from") or {}
    ticket_part, _, reason = rest.partition(":")
    if not ticket_part.isdigit():
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    row = await store.get_client_feedback(int(ticket_part))
    if row is None or clicker.get("id") not in {c.get("chat") for c in (row.get("cards") or [])}:
        await _answer(query_id, "Рухсат йўқ")
        return "unrecognized"
    if row.get("ticket_status") != "open":
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "complaint_closed"
    message = callback.get("message") or {}

    async def edit_pressed(text: str, keyboard: dict[str, Any]) -> None:
        if message.get("message_id") and (message.get("chat") or {}).get("id"):
            async with TelegramBot(
                agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
            ) as bot:
                await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                    chat_id=str(message["chat"]["id"]), message_id=message["message_id"], text=text, reply_markup=keyboard)

    if prefix == "cfok":
        await _answer(query_id, "Сабабини танланг")
        await edit_pressed(feedback.ticket_text(row) + "\n\n<b>Сабаби нима эди?</b>",
                           feedback.ticket_keyboard(row, picking_reason=True))
        return "complaint_reason_asked"
    if prefix == "cfno":
        await store.complaint_not_yet(row["id"])
        await _answer(query_id, "Қайд этилди")
        await edit_pressed(feedback.ask_text(row) + "\n\n❌ Ҳали ҳал бўлмаган — 1 соатдан кейин яна сўралади.",
                           {"inline_keyboard": []})
        return "complaint_not_yet"
    if reason not in feedback.REASONS:
        await _answer(query_id, "Номаълум сабаб")
        return "unrecognized"
    resolved = await store.resolve_complaint(row["id"], await _actor_display_name(clicker), reason)
    if resolved is None:
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "complaint_closed"
    await _answer(query_id, "Ҳал бўлди")
    await feedback.update_cards(resolved, run_id)
    if message.get("message_id") not in {c.get("message") for c in (resolved.get("cards") or [])}:
        await edit_pressed(feedback.ticket_text(resolved), {"inline_keyboard": []})
    await log_action(agent=AGENT, action="complaint_resolved", target_system="postgres", status="success",
                     run_id=run_id, target_ref=str(resolved.get("code")), mode="write", payload={"reason": reason})
    return "complaint_resolved"


async def _edit_lead_message(chat_id: int, message_id: int | None, text: str, keyboard: dict[str, Any], run_id: uuid.UUID) -> None:
    """Edit one of the bot's own messages in an employee's chat (today's report card)."""
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
            chat_id=str(chat_id), message_id=message_id, text=text, reply_markup=keyboard
        )


async def _handle_stopped_lead_button(callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Old lead buttons: say the hand-out has stopped and take the buttons off that message."""
    await _answer(callback.get("id", ""), "лидлар тарқатиш тўхтатилган")
    message = callback.get("message") or {}
    if message.get("message_id") and (message.get("chat") or {}).get("id"):
        try:
            async with TelegramBot(
                agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
            ) as bot:
                await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                    chat_id=str(message["chat"]["id"]), message_id=message["message_id"],
                    text=message.get("text") or "лидлар тарқатиш тўхтатилган", reply_markup={"inline_keyboard": []},
                )
        except TelegramError as exc:
            log.warning("Could not take the buttons off an old lead message: {}", exc)
    return "lead_handout_stopped"


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

    # /sababli — the Director (everyone) and the Garmin manager (his team) excuse a day (excused.py, 2026-10-23).
    excused_outcome = await excused.handle_message(employee, message, run_id)
    if excused_outcome is not None:
        return excused_outcome

    if employee["role"] != DIRECTOR_ROLE:
        return await _handle_employee_message(employee, message, run_id, background)

    # /hisobotlar: the month's report calendar as a picture (report_calendar.py, 2026-10-17).
    # /eski: goods a year or more in the warehouse, every warehouse (old_stock.py, 2026-10-17).
    if (message.get("text") or "").strip().lower() in old_stock.COMMANDS:
        return await old_stock.send(telegram_user_id, None, run_id)
    if (message.get("text") or "").strip().lower() in PULSE_COMMANDS:  # the live dashboard (2026-10-22)
        return await _send_pulse_link(telegram_user_id, run_id)
    # /hisobot too — the Director sends no report of his own (2026-10-17).
    if (message.get("text") or "").strip().lower() in report_calendar.COMMANDS + report_tools.REPORT_COMMANDS:
        return await _send_report_calendar(telegram_user_id, run_id)

    has_media = any(message.get(field) for field in MEDIA_FIELDS)
    text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()

    if not has_media and not text:
        return "ignored"

    await _show_typing(telegram_user_id, run_id)

    if has_media:
        background.add_task(_dispatch_director_media, telegram_user_id, message.get("message_id"), caption, run_id,
                            cheques.image_of(message))
    else:
        background.add_task(_dispatch_director_task, telegram_user_id, text, message.get("message_id"), run_id)
    return "queued"


# One person's questions are answered one at a time, in order (the 14.10 test: two quick
# questions ran at once, the answers mixed them and arrived a question late). Keyed by the
# event loop too — a lock belongs to its loop.
_PERSON_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _person_lock(telegram_user_id: int) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), int(telegram_user_id))
    if key not in _PERSON_LOCKS:
        if len(_PERSON_LOCKS) > 500:
            _PERSON_LOCKS.clear()  # idle locks only matter while held; a rare reset is harmless
        _PERSON_LOCKS[key] = asyncio.Lock()
    return _PERSON_LOCKS[key]


async def _answer_reply(telegram_user_id: int, run_id: uuid.UUID, text: str, reply_to: int | None,
                        keyboard: dict[str, Any] | None = None) -> None:
    """An AI answer, shown as a reply to the question it answers (when its message id is known)."""
    if not reply_to:
        await _reply(telegram_user_id, run_id, text, keyboard)
        return
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot.send_message(text, chat_id=str(telegram_user_id), reply_to_message_id=reply_to, reply_markup=keyboard)


async def _route_to_ai(
    employee: dict[str, Any], text: str, task: dict[str, Any] | None, run_id: uuid.UUID,
    background: BackgroundTasks | None, reply_to: int | None = None, quoted: str | None = None,
) -> str:
    """An employee's message about their work goes to the AI (``ai_chat.py``), never to a person.

    Someone given company data (Бухгалтерия / Молия, ``data_access.py``,
    2026-10-10) goes to the analyst instead, with only their systems' tools —
    unless they reply to a task card, which stays with the work AI.
    """
    telegram_user_id = employee["telegram_user_id"]
    scope = data_access.granted(employee)
    if scope and task is None:
        await _show_typing(telegram_user_id, run_id)
        if background is not None:
            background.add_task(_answer_data_question, employee, text, scope, run_id, reply_to, quoted)
        else:
            await _answer_data_question(employee, text, scope, run_id, reply_to, quoted)
        return "data_question"
    if employee.get("ai_chat_off"):
        await _reply(telegram_user_id, run_id, ai_chat.off_text())
        return "ai_off"
    await _show_typing(telegram_user_id, run_id)
    if background is not None:
        background.add_task(_answer_ai_chat, employee, text, task, run_id, reply_to, quoted)
    else:
        await _answer_ai_chat(employee, text, task, run_id, reply_to, quoted)
    return "ai_chat"


async def _answer_ai_chat(employee: dict[str, Any], text: str, task: dict[str, Any] | None, run_id: uuid.UUID,
                          reply_to: int | None = None, quoted: str | None = None) -> None:
    async with _person_lock(employee["telegram_user_id"]):
        await _answer_ai_chat_now(employee, text, task, run_id, reply_to, quoted)


# How many earlier messages (questions + answers, today) the AI keeps in mind: 12 → 24 (2026-10-16).
AI_MEMORY_TURNS = 24
# How much of a quoted bot message the AI is shown.
QUOTE_CHARS = 1500


def with_quote(text: str, quoted: str | None) -> str:
    """The question as the AI sees it: with the bot message it replies to, when there is one."""
    if not quoted:
        return text
    return f"{text}\n\n[This is a reply to this earlier message of yours: «{quoted[:QUOTE_CHARS]}»]"


async def _answer_ai_chat_now(employee: dict[str, Any], text: str, task: dict[str, Any] | None, run_id: uuid.UUID,
                              reply_to: int | None, quoted: str | None = None) -> None:
    """Answer one message — with the person's own tasks and duties, nothing of anyone else's."""
    telegram_user_id = employee["telegram_user_id"]
    try:
        if await store.ai_questions_today(telegram_user_id) >= ai_chat.DAILY_LIMIT:
            await _reply(telegram_user_id, run_id, ai_chat.limit_text())
            return
        history = await store.recent_ai_turns(telegram_user_id, AI_MEMORY_TURNS)
        tasks = await store.open_tasks_for_employee(telegram_user_id)
        waiting = await agenda.pending(employee)  # what the bot waits for from them (2026-10-24)
        await store.log_ai_turn(telegram_user_id, "employee", text)
        async with OpenRouterClient(
            agent=ai_chat.AGENT,
            run_id=run_id,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            raw = await ai.complete(
                ai_chat.system_prompt(employee, ai_chat.work_context(employee, tasks, today_local(), waiting)),
                ai_chat.user_prompt(history, with_quote(text, quoted), task),
            )
        answer = ai_chat.clean_answer(raw)
        # Never «your report was accepted» from the AI; a report draft gets a button that really sends it (2026-10-26).
        answer, _ = ai_chat.strip_false_claims(answer)
        answer, draft = ai_chat.split_draft(answer)
        keyboard = None
        if draft and kpi.asked_for_reports(employee) and not employee.get("reports_off"):
            held = await store.create_pending_relay(telegram_user_id, draft, None)
            keyboard = {"inline_keyboard": [[{"text": "📝 Ҳисобот сифатида юбориш", "callback_data": f"asrep:{held['id']}"}]]}
        await store.log_ai_turn(telegram_user_id, "assistant", answer)
        await _answer_reply(telegram_user_id, run_id, answer, reply_to, keyboard)
    except Exception as exc:  # noqa: BLE001 — a background failure must be told, not raised
        log.error("AI chat failed for {}: {}", telegram_user_id, exc)
        try:
            await _reply(telegram_user_id, run_id, ai_chat.error_text())
        except Exception:  # noqa: BLE001
            pass


async def _answer_data_question(employee: dict[str, Any], text: str, scope: set[str], run_id: uuid.UUID,
                                reply_to: int | None = None, quoted: str | None = None) -> None:
    async with _person_lock(employee["telegram_user_id"]):
        await _answer_data_question_now(employee, text, scope, run_id, reply_to, quoted)


async def _answer_data_question_now(employee: dict[str, Any], text: str, scope: set[str], run_id: uuid.UUID,
                                    reply_to: int | None, quoted: str | None = None) -> None:
    """An accountant's / finance person's question: the analyst with their systems only, in their language."""
    from integrations.org_bot import analyst

    telegram_user_id = employee["telegram_user_id"]
    ru = employee.get("lang") == "ru"
    try:
        if await store.ai_questions_today(telegram_user_id) >= ai_chat.DAILY_LIMIT:
            await _reply(telegram_user_id, run_id, "На сегодня лимит вопросов исчерпан, продолжим завтра 🙂" if ru
                         else ai_chat.limit_text())
            return
        history = "\n".join(f"{'Employee' if t['role'] == 'employee' else 'You'}: {t['content']}"
                             for t in await store.recent_ai_turns(telegram_user_id, AI_MEMORY_TURNS))
        await store.log_ai_turn(telegram_user_id, "employee", text)
        result = await analyst.answer(with_quote(text, quoted), history, run_id, employee=employee, scope=scope)
        answer = sanitize_model_html(result.text.strip()) or (
            "Не удалось найти ответ, попробуйте спросить иначе." if ru else ai_chat.clean_answer(""))
        await store.log_ai_turn(telegram_user_id, "assistant", answer)
        # Which tools ran and how many rounds — never the question or the figures (the order of 07.10.2026).
        await log_action(agent=AGENT, action="data_question", target_system="openrouter", status="success",
                         run_id=run_id, mode="read",
                         payload={"systems": sorted(scope), "tools": result.tools, "rounds": result.rounds})
        await _answer_reply(telegram_user_id, run_id, answer, reply_to)
        await _send_files(telegram_user_id, run_id, result.files, reply_to)
        await _send_media(telegram_user_id, run_id, result.media, reply_to)
    except Exception as exc:  # noqa: BLE001 — a background failure must be told, not raised
        log.error("Data question failed for {}: {}", telegram_user_id, type(exc).__name__)
        await log_action(agent=AGENT, action="data_question", target_system="openrouter", status="failure",
                         run_id=run_id, mode="read", error_message=type(exc).__name__)
        try:
            await _reply(telegram_user_id, run_id, "Техническая ошибка, попробуйте чуть позже" if ru
                         else ai_chat.error_text())
        except Exception:  # noqa: BLE001
            pass


async def _handle_data_access_close(employee_id: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """The Director's ⛔ under a data-access notice: everything that person had is closed."""
    query_id = callback.get("id", "")
    clicker_id = (callback.get("from") or {}).get("id")
    clicker = await store.get_employee_by_telegram_id(clicker_id) if clicker_id else None
    if clicker is None or clicker.get("role") != DIRECTOR_ROLE or clicker.get("status") != "active":
        await _answer(query_id, "Фақат Директор")
        return "unrecognized"
    closed = await store.clear_data_access(employee_id, f"director:{clicker_id}")
    message = callback.get("message") or {}
    if closed is None:
        await _answer(query_id, "Аллақачон ёпилган")
        return "data_access_already_closed"
    await _answer(query_id, "OK")
    who = escape(data_access.person(closed))
    if message.get("message_id") and message.get("chat", {}).get("id"):
        async with TelegramBot(
            agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
        ) as bot:
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]), message_id=message["message_id"],
                text=f"🔐 {who}: маълумотга кириш <b>ёпилди</b>.", reply_markup={"inline_keyboard": []},
            )
    await _reply(closed["telegram_user_id"], run_id, data_access.employee_notice(closed))
    await admin.tell_admin(f"🔐 Директор {who} учун маълумотга киришни ёпди.", run_id)
    await log_action(agent=AGENT, action="data_access_closed", target_system="postgres", status="success",
                     run_id=run_id, target_ref=employee_id, mode="write", payload={"by": "director"})
    return "data_access_closed"


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
    telegram_user_id: int, run_id: uuid.UUID, text: str, reply_markup: dict[str, Any] | None = None,
    reply_to: int | None = None,
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
        return await bot.send_message(text, chat_id=str(telegram_user_id), reply_markup=reply_markup,
                                      reply_to_message_id=reply_to)


async def _show_typing(telegram_user_id: int, run_id: uuid.UUID) -> None:
    """Show Telegram's native typing indicator instead of a canned text ack —
    a real classification/answer call takes a few seconds; this signals
    "working on it" without leaving a repetitive message in the chat."""
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot.send_chat_action(chat_id=str(telegram_user_id))


async def _reply_and_log(director_telegram_user_id: int, run_id: uuid.UUID, text: str,
                         reply_to: int | None = None) -> None:
    """Send the Director a substantive reply and remember it as conversation
    memory — used for the actual outcome of a request, not UX filler like the
    "got it, routing..." ack (logging every ack would bury real content in noise).

    ``reply_to``: his question's message — the answer sits under it as a reply (2026-10-21, the owner:
    several questions sent at once came back as loose messages, as every other role's already were).
    """
    await _reply(director_telegram_user_id, run_id, text, reply_to=reply_to)
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
    if not kpi.asked_for_reports(employee):  # the Garmin manager sends none (2026-10-17): straight to the AI
        return None
    day = today_local()
    metrics_def = kpi.metrics_for_role(employee["role"])
    # «Ҳа» / «хоп» right after a report draft or a «is this your report?» question = send it (2026-10-26: people
    # answered «Ха» and the AI repeated the whole text back).
    if is_confirmation(text):
        held = await store.latest_open_relay(employee["telegram_user_id"], HELD_CONFIRM_MINUTES)
        if held is not None and await store.resolve_pending_relay(str(held["id"]), employee["telegram_user_id"], "report"):
            outcome, line = await _report_from_held(employee, held["message_text"], run_id)
            await _reply(employee["telegram_user_id"], run_id, line)
            return outcome
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
                await _reply(employee["telegram_user_id"], run_id, casual("раҳмат каттакон, ҳисоботингизга қўшиб қўйдим"))
                return "daily_report_followup"

        # A reply to an ask that has closed (22:00, or an earlier day), so say
        # so instead of relaying a stale report to the Director.
        if reply_to_message_id:
            expired = await store.expired_report_for_prompt(employee["telegram_user_id"], reply_to_message_id)
            if expired is not None and expired["report_date"] == day:  # today's, after 22:00: for approval (26.10)
                taken = await late_report.take_typed(employee, day, text, run_id)
                if taken is not None:
                    return taken
            if expired is not None:
                await _reply(
                    employee["telegram_user_id"],
                    run_id,
                    casual(
                        f"{expired['report_date'].strftime('%d.%m')} кунги ҳисоботнинг вақти ўтиб кетибди, "
                        "ҳисоботлар шу куннинг ўзида соат 22:00 гача олинади",
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
        # No ask open now (before 16:00, after 22:00, a day the person isn't asked): a report is still a report
        # (2026-10-26 — they were going to the AI and lost).
        if not employee.get("reports_off") and not asks_something(text):
            return await _report_outside_ask(employee, text, run_id)
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
    if kind in ("task_question", "question"):
        return None
    if is_pointer(text):  # «тепадаги ҳисобот» — never the report itself (2026-10-26)
        await _reply(telegram_user_id, run_id, pointer_text())
        return "report_pointer"
    if kind == "ask":  # the AI decides (2026-10-26); asked only when it isn't sure
        decided = await _classify_report(text, run_id)
        if decided == "other":
            return None
        if decided == "unsure":
            return await _ask_report_or_ai(employee, text, run_id)
    # None means Telegram delivered this same report twice and the first copy
    # already saved it — stop here rather than offer the copy to the Director.
    return await _save_daily_report(employee, pending, text, run_id) or "daily_report_duplicate"


async def _report_outside_ask(employee: dict[str, Any], text: str, run_id: uuid.UUID) -> str | None:
    """A message while no report ask is open: if the AI says it is the report — from 14:00 it is saved (the row
    opened now); after 22:00 today's closed ask takes it as a late report for approval; before 14:00 the person is
    told (yesterday's missed one goes for approval). None: not a report — on to the AI."""
    telegram_user_id = employee["telegram_user_id"]
    now = now_local()
    row = await store.report_for_day(telegram_user_id, now.date())
    if row is not None and row.get("status") != "asked":  # sent already, or a day with a reason
        return None
    if is_pointer(text):
        await _reply(telegram_user_id, run_id, pointer_text())
        return "report_pointer"
    kind = await _classify_report(text, run_id)
    if kind == "other" or (kind == "unsure" and not REPORT_OPEN_HOUR <= now.hour < store.REPORT_CLOSE_HOUR):
        return None
    if now.hour >= store.REPORT_CLOSE_HOUR:
        if row is not None:
            taken = await late_report.take_typed(employee, now.date(), text, run_id)
            if taken is not None:
                return taken
        await _reply(telegram_user_id, run_id, casual(
            "бугунги ҳисоботлар соат 22:00 да ёпилган, бу хабар ҳисобот сифатида сақланмади, раҳмат"))
        return "daily_report_closed"
    if now.hour < REPORT_OPEN_HOUR:
        taken = await late_report.take_typed(employee, now.date() - timedelta(days=1), text, run_id)
        if taken is not None:  # yesterday's missed report, written this morning: for approval
            return taken
        await _reply(telegram_user_id, run_id, casual(
            "бугунги ҳисобот соат 14:00 дан 22:00 гача қабул қилинади, ўшанда юборинг, раҳмат"))
        return "daily_report_too_early"
    if kind == "unsure":
        return await _ask_report_or_ai(employee, text, run_id)
    if row is None:
        await store.open_report_request(employee, now.date())
    pending = await store.pending_report(telegram_user_id, now.date())
    if pending is None:
        return None
    return await _save_daily_report(employee, pending, text, run_id) or "daily_report_duplicate"


async def _report_from_held(employee: dict[str, Any], text: str, run_id: uuid.UUID) -> tuple[str, str]:
    """A held message the person said is their report: saved (from 14:00 the row is opened if there was no ask).
    (outcome, the line to show)."""
    telegram_user_id = employee["telegram_user_id"]
    now = now_local()
    if is_pointer(text):
        return "report_pointer", pointer_text()
    pending = await store.pending_report(telegram_user_id, now.date())
    if pending is None and REPORT_OPEN_HOUR <= now.hour < store.REPORT_CLOSE_HOUR and not employee.get("reports_off") \
            and await store.report_for_day(telegram_user_id, now.date()) is None:
        await store.open_report_request(employee, now.date())
        pending = await store.pending_report(telegram_user_id, now.date())
    if pending is not None and await _save_daily_report(employee, pending, text, run_id, quiet=True) is not None:
        return "daily_report", casual("ҳисобот сифатида сақладим, раҳмат каттакон")
    if await store.submitted_report_today(telegram_user_id, now.date()) is not None:
        return "report_already_submitted", casual("бугунги ҳисоботингиз аллақачон қабул қилинган")
    if now.hour < REPORT_OPEN_HOUR:
        return "daily_report_too_early", casual("бугунги ҳисобот соат 14:00 дан 22:00 гача қабул қилинади, ўшанда юборинг")
    return "report_window_closed", casual("бугунги ҳисоботлар соат 22:00 да ёпилган, бу матн сақланмади")


async def _save_daily_report(
    employee: dict[str, Any], pending: dict[str, Any], text: str, run_id: uuid.UUID, quiet: bool = False
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
    if quiet:  # the caller says what happened (a held draft / a tap)
        return "daily_report"
    follow_up = await _report_follow_up(text, employee["role"], run_id)
    if follow_up:
        await store.mark_report_followup(str(saved["id"]))
        await _reply(employee["telegram_user_id"], run_id, casual(escape(follow_up)))
        return "daily_report_weak"

    ack = casual("раҳмат каттакон, ҳисоботингиз қабул қилинди, ўзгартириш учун /hisobot, чарчаманг")
    missing = kpi.missing_metrics(values, metrics_def)
    if missing:
        ack = casual(
            f"раҳмат, ҳисоботингиз қабул қилинди, {escape(', '.join(missing))} рақамларини ҳам ёзсангиз қўшиб қўяман",
        )
    await _reply(employee["telegram_user_id"], run_id, ack)
    return "daily_report"


async def _ask_report_or_ai(employee: dict[str, Any], text: str, run_id: uuid.UUID) -> str:
    """Ask whether a message is today's report or a question for the AI."""
    held = await store.create_pending_relay(employee["telegram_user_id"], text, None)
    await _reply(
        employee["telegram_user_id"],
        run_id,
        casual("бу бугунги ҳисоботингизми?"),
        report_or_ai_keyboard(str(held["id"])),
    )
    return "report_or_ai_asked"


async def _ask_file_purpose(employee: dict[str, Any], message: dict[str, Any], kind: str, run_id: uuid.UUID) -> str:
    """Hold a file and ask what it's for; "today's report" only when a report was asked today."""
    telegram_user_id = employee["telegram_user_id"]
    held = await store.create_employee_file(telegram_user_id, message["message_id"], kind, message.get("caption"),
                                            report_media.file_of(message))
    report = await store.report_for_day(telegram_user_id, today_local())
    waiting = await _waiting_explanations(telegram_user_id)
    await _reply(
        telegram_user_id, run_id, report_tools.ask_purpose_text(kind),
        report_tools.purpose_keyboard(str(held["id"]), report is not None, explanation=bool(waiting)),
    )
    return "file_purpose_asked"


async def _handle_file_purpose(rest: str, callback: dict[str, Any], run_id: uuid.UUID,
                               background: BackgroundTasks | None = None) -> str:
    """Today's report → kept with the report (a voice / video becomes its text, 2026-10-22); to the Director →
    forwarded as it is, unread; cancel → nothing."""
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
    if purpose == "explanation":  # a sick note, a voice — the answer to this morning's ask (2026-10-22)
        await settle(report_tools.sent_text("explanation", True))
        job = (_file_explanation, employee, held, run_id)
        if background is not None:
            background.add_task(*job)
        else:
            await _file_explanation(*job[1:])
        return "file_explanation"
    if purpose == "report":
        # Kept with the report, sent to whoever asks for it (2026-10-22) — no longer pushed to the Director at once.
        placeholder = report_media.placeholder(held["kind"], held.get("caption"))
        report = await store.attach_media_to_report(clicker_id, today_local(), held.get("caption"), placeholder)
        if report is None:
            await settle(report_tools.no_report_today_text())
            return "file_no_report"
        await store.link_file_to_report(str(held["id"]), str(report["id"]))
        await settle(report_tools.sent_text("report", True))
        if held.get("file_id") and report_media.readable(held["kind"], int(held.get("file_size") or 0)) \
                and held["kind"] in (*report_media.LISTEN_KINDS, *report_media.WATCH_KINDS):
            job = (_file_to_report_text, held, str(report["id"]), placeholder, clicker_id, run_id)
            if background is not None:
                background.add_task(*job)
            else:
                await _file_to_report_text(*job[1:])
        return "file_report"

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


async def _file_text(held: dict[str, Any], run_id: uuid.UUID) -> str:
    """A held voice / video as text (report_media); raises when it can't be downloaded or read."""
    async with TelegramBot(
        agent=report_media.AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        data = await bot.download_file(held["file_id"], max_bytes=report_media.MAX_AUDIO_BYTES)
    return await report_media.to_text(held["kind"], data, held.get("mime"), run_id)


async def _file_to_report_text(held: dict[str, Any], report_id: str, placeholder: str, telegram_user_id: int,
                               run_id: uuid.UUID) -> None:
    """In the background: the voice / video as text, put into the report, and the person shown it (2026-10-22)."""
    try:
        line = report_media.report_line(held["kind"], await _file_text(held, run_id))
    except Exception as exc:  # noqa: BLE001 — the file stays with the report; only its text is missing
        log.warning("Report file not read as text: {}", type(exc).__name__)
        line = report_media.report_line(held["kind"], "")
    await store.set_file_text(str(held["id"]), line)
    await store.add_report_line(report_id, placeholder, line)
    await log_action(agent=report_media.AGENT, action="report_file_text", target_system="openrouter", status="success",
                     run_id=run_id, mode="read", payload={"kind": held["kind"], "chars": len(line)})
    try:
        await _reply(telegram_user_id, run_id, report_media.done_text(held["kind"], line))
    except Exception:  # noqa: BLE001
        pass


async def _file_explanation(employee: dict[str, Any], held: dict[str, Any], run_id: uuid.UUID) -> None:
    """A file sent as the explanation: a voice / video as text, else its caption; delivered with the file."""
    text = report_media.placeholder(held["kind"], held.get("caption"))
    if held.get("file_id") and held["kind"] in (*report_media.LISTEN_KINDS, *report_media.WATCH_KINDS) \
            and report_media.readable(held["kind"], int(held.get("file_size") or 0)):
        try:
            text = report_media.report_line(held["kind"], await _file_text(held, run_id))
        except Exception as exc:  # noqa: BLE001 — the file itself still goes
            log.warning("Explanation file not read as text: {}", type(exc).__name__)
    try:
        await _take_explanation(employee, text, None, run_id, held=held)
    except Exception as exc:  # noqa: BLE001 — a background failure is logged, not raised
        log.error("File explanation failed: {}", type(exc).__name__)


async def _show_today_report(employee: dict[str, Any], run_id: uuid.UUID) -> str:
    """/hisobot: today's report with ✏️ / 🗑, or how to send it."""
    report = await store.report_for_day(employee["telegram_user_id"], today_local())
    # Someone with a team who has no report of their own (the Garmin manager): /hisobot meant the
    # team's — the calendar (2026-10-17: «/hisobot» answered "no report is asked of you").
    team = report_calendar.TEAM_OF.get(employee.get("role") or "")
    if report is None and team:
        return await _send_report_calendar(employee["telegram_user_id"], run_id, team)
    if report is None and employee.get("reports_off"):
        await _reply(employee["telegram_user_id"], run_id, report_tools.reports_off_text())
        return "reports_off"
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
    if not store.reports_open(today):
        await _answer(query_id, "ҳисоботлар соат 22:00 гача ўзгартирилади")
        return "report_closed"
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
                text=casual("савол сифатида қабул қилдим"), reply_markup={"inline_keyboard": []},
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

    employee = await store.get_employee_by_telegram_id(clicker_id)
    if employee is None or employee["status"] != "active":
        outcome, status_line = "unknown_employee", casual("ходим топилмади")
    else:
        outcome, status_line = await _report_from_held(employee, relay["message_text"], run_id)

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


PULSE_COMMANDS = ("/pulse", "/пулс")


async def _send_pulse_link(telegram_user_id: int, run_id: uuid.UUID) -> str:
    """/pulse: a button that opens PULSE (integrations/api/pulse_page.py) — signed, for this person, short-lived."""
    from integrations.pulse import access

    if not settings.public_url:
        await _reply(telegram_user_id, run_id, "PULSE манзили созланмаган.")
        return "pulse_no_url"
    keyboard = {"inline_keyboard": [[{"text": "PULSE", "url": access.link(telegram_user_id)}]]}
    await _reply(telegram_user_id, run_id, f"PULSE · ҳавола {access.LINK_MINUTES} дақиқа ишлайди", keyboard)
    await log_action(agent=AGENT, action="pulse_link", target_system="telegram", status="success", run_id=run_id, mode="notify")
    return "pulse_link"


async def _send_competitors(telegram_user_id: int, run_id: uuid.UUID) -> None:
    """/raqobat: the competitor check; a failure is told, not raised (it runs in the background)."""
    try:
        await competitors.send(telegram_user_id, run_id, fresh_web=False)
    except Exception as exc:  # noqa: BLE001
        log.error("Competitors check failed: {}", type(exc).__name__)
        try:
            await _reply(telegram_user_id, run_id, "Рақобатчиларни текшириб бўлмади, бир оздан кейин қайта уриниб кўринг.")
        except Exception:  # noqa: BLE001
            pass


async def _handle_employee_message(
    employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID, background: BackgroundTasks | None = None
) -> str:
    """An employee's free-text message: about their work, answered by the AI.

    2026-10-02, from the owner: OPS Manager Bot is for work only. Employees
    don't message the Director or each other through it; what they write
    goes, in order, to: a lead's follow-up, today's report —
    and otherwise to the AI, which answers about their own work and tasks
    (``ai_chat.py``). Permission requests, KPI commands and task buttons have
    their own flows and never reach this point.
    """
    telegram_user_id = employee["telegram_user_id"]
    # Right after ✅ Бажардим / ⛔ Тўсиқ бор, the next message is that task's
    # proof or blocker (task_register.py) — a file included.
    first_text = (message.get("text") or message.get("caption") or "").strip()
    if not first_text.startswith("/"):
        awaiting = await store.task_awaiting_input(telegram_user_id, task_register.AWAIT_MINUTES)
        replied = (message.get("reply_to_message") or {}).get("message_id")
        # A Reply to another message (the 16:00 report ask) is that, not this task's proof.
        if awaiting is not None and (not replied or replied == awaiting.get("telegram_message_id")):
            return await _consume_task_input(employee, awaiting, message, run_id)
    # After «📝 Кечаги ҳисоботни юбориш» the next text is yesterday's report, for approval (late_report.py).
    late = await late_report.take_message(employee, message, run_id, background)
    if late is not None:
        return late
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
    if employee.get("role") == "garmin_manager" and text.lower() in PULSE_COMMANDS:  # his team's dashboard (2026-10-22)
        return await _send_pulse_link(telegram_user_id, run_id)
    if employee.get("role") == "garmin_manager" and text.lower() in old_stock.COMMANDS:  # his warehouses' old stock
        return await old_stock.send(telegram_user_id, await old_stock.garmin_codes_now(), run_id)
    if employee.get("role") == "garmin_manager" and text.lower() in competitors.COMMANDS:  # who else sells Garmin (19.10)
        await _reply(telegram_user_id, run_id, "OLX ва бошқа сайтларни текширяпман, бир-икки дақиқа кутинг.")
        if background is not None:  # OLX pages and the search take a while — not inside the webhook
            background.add_task(_send_competitors, telegram_user_id, run_id)
        else:
            await _send_competitors(telegram_user_id, run_id)
        return "competitors_asked"
    team = report_calendar.TEAM_OF.get(employee.get("role") or "")
    if team and text.lower() in report_calendar.COMMANDS:  # the Garmin manager: his team's calendar (17.10)
        return await _send_report_calendar(telegram_user_id, run_id, team)
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
    taken = await _take_explanation(employee, text, reply_to.get("message_id"), run_id, message.get("message_id"))
    if taken is not None:  # the answer to this morning's «why weren't you at work / no report?» (2026-10-22)
        return taken
    outcome = await _try_daily_report(employee, text, reply_to.get("message_id"), run_id)
    if outcome is not None:
        return outcome

    task = None
    if reply_to.get("message_id"):
        task = await store.find_task_by_message_id(reply_to["message_id"], telegram_user_id)
    # A reply to one of the bot's own messages (an earlier answer, a complaint card, the brief):
    # the AI is shown what was quoted (2026-10-16 — "that one" was a guess before).
    quoted = None
    if task is None and (reply_to.get("from") or {}).get("is_bot"):
        quoted = (reply_to.get("text") or reply_to.get("caption") or "").strip() or None
    return await _route_to_ai(employee, text, task, run_id, background, message.get("message_id"), quoted)


EXPLANATION_CHECK = """The work bot asked an employee this morning: «{ask}». Then they wrote the message below. \
What is it?
- "reason": an answer to that question — a reason, an excuse, an apology, or a claim that they did come / did send it;
- "report": {report_rule}
- "other": anything else — a question about their work, a task update, a greeting, chatter.
JSON only: {{"kind": "reason"}}, {{"kind": "report"}} or {{"kind": "other"}}."""
REPORT_RULE = ("the question is about yesterday's missing daily report and the message IS that report — what "
               "they did at work yesterday (tasks, customers, places, numbers), not why it didn't come;")
NO_REPORT_RULE = "never (this question isn't about a report);"
ANSWER_KINDS = ("reason", "report", "other")


async def _classify_answer(ask: str, text: str, report_ask: bool, run_id: uuid.UUID) -> str:
    """What a message to the morning question is: its reason, the missing report itself, or something else
    (2026-10-24: a typed report became an explanation card; a reply about a task became the explanation).
    The AI decides; if it can't, «reason» — a missed explanation is worse than a misplaced one."""
    try:
        async with OpenRouterClient(agent=AGENT, run_id=run_id, model_override=settings.ops_manager_bot_model,
                                    fallback_override=settings.ops_manager_bot_fallback_models) as ai:
            verdict = await ai.complete_json(EXPLANATION_CHECK.format(
                ask=ask, report_rule=REPORT_RULE if report_ask else NO_REPORT_RULE), text)
        kind = str(verdict.get("kind") or "reason") if isinstance(verdict, dict) else "reason"
    except Exception:  # noqa: BLE001
        return "reason"
    if kind == "report" and not report_ask:
        return "reason"
    return kind if kind in ANSWER_KINDS else "reason"


async def _waiting_explanations(telegram_user_id: int) -> list[dict[str, Any]]:
    """This person's unanswered asks; none if they can't be read — the message then goes on as before."""
    try:
        return await store.open_explanations(telegram_user_id, explanations.OPEN_HOURS)
    except Exception as exc:  # noqa: BLE001 — a new nicety must never stop reports and questions
        log.warning("Open explanations not read: {}", type(exc).__name__)
        return []


def explanation_managers(role: str | None) -> list[str]:
    """Who gets a person's explanation besides the Director: the manager of their team (report_calendar.TEAM_OF)."""
    return [manager for manager, team in report_calendar.TEAM_OF.items() if role in team]


async def _take_explanation(employee: dict[str, Any], text: str, replied: int | None, run_id: uuid.UUID,
                            message_id: int | None = None, held: dict[str, Any] | None = None) -> str | None:
    """The answer to this morning's ask (explanations.py), or None when the message is something else.

    A reply to the ask always is; a plain message is when it isn't a command or a question («?»), no
    report is owed right now (then it's the report's), and the AI agrees it answers the ask. ``held`` —
    a file the person sent with «тушунтириш» — always is.
    """
    telegram_user_id = employee["telegram_user_id"]
    waiting = await _waiting_explanations(telegram_user_id)
    if not waiting:
        return None
    if held is None:
        if not (replied and any(w.get("prompt_message_id") == replied for w in waiting)):
            if not explanations.is_explanation(text):
                return None
            owed = await store.report_for_day(telegram_user_id, today_local())
            if owed is not None and owed.get("status") == "asked":
                return None
        # A reply to the morning message too (2026-10-24): it may also list tasks, so a reply can be about one.
        report_ask = next((w for w in waiting if w["kind"] == "report"), None)
        ask = explanations.ask_text(waiting[0]["kind"], names.call_name(employee), list(waiting[0]["days"]))
        kind = await _classify_answer(_plain(ask), text, report_ask is not None, run_id)
        if kind == "other":
            return None
        if kind == "report" and report_ask is not None:  # the missing report itself, typed: for approval
            day = report_ask.get("asked_for") or list(report_ask["days"])[-1]
            taken = await late_report.take_typed(employee, day, text, run_id)
            if taken is not None:
                return taken
    rows = await store.answer_explanations([str(w["id"]) for w in waiting], text)
    if not rows:
        return None
    person, role = names.person_name(employee), ROLE_LABELS.get(employee.get("role") or "", employee.get("role") or "")
    roles = [DIRECTOR_ROLE, *explanation_managers(employee.get("role"))]
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for row in rows:
            card = explanations.card(person, role, row["kind"], list(row["days"]), text)
            buttons = excused.card_keyboard(str(row["id"]), False)  # ⬜ Сабабли: the days turn grey (2026-10-23)
            delivered = 0
            for who in roles:
                for boss in await store.active_employees_by_role(who):
                    try:
                        if held is not None:  # the file itself, the explanation as its caption
                            await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                                "copyMessage", {"chat_id": str(boss["telegram_user_id"]), "from_chat_id": str(telegram_user_id),
                                                "message_id": held["message_id"], "caption": card[:1024], "parse_mode": "HTML",
                                                "reply_markup": buttons},
                                mode="notify", target_ref=str(boss["telegram_user_id"]))
                        else:
                            await bot.send_message(card, chat_id=str(boss["telegram_user_id"]), reply_markup=buttons)
                        delivered += 1
                    except Exception as exc:  # noqa: BLE001 — the next one still gets it
                        log.error("Explanation not delivered: {}", type(exc).__name__)
            await store.set_explanation_delivered(str(row["id"]), delivered)
            held = None if delivered else held  # the file once is enough
    await log_action(agent=AGENT, action="explanation_received", target_system="telegram", status="success",
                     run_id=run_id, mode="notify", payload={"asks": len(rows)})
    await _reply(telegram_user_id, run_id, explanations.thanks_text(), reply_to=message_id)
    return "explanation_taken"


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
                director_telegram_user_id, run_id, "Кимга юборишни аниқлай олмадим — исмни аниқроқ ёзинг.",
                source_message_id,
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
                source_message_id,
            )
            return

        target_type, target_role, target_agent = validated
        # The message itself isn't logged (the Director's order of 07.10.2026:
        # IT doesn't read management's correspondence) — only where it went.
        log.info(
            "Classified a {}-char message -> type={} role={} agent={}",
            len(raw_message), target_type, target_role, target_agent,
        )
        if target_type == "employee":
            await _propose_task(
                director_telegram_user_id, source_message_id, raw_message, task_summary, target_role, run_id,
                due_date, person,
            )
        elif target_type == "agent":
            await _answer_question(director_telegram_user_id, target_agent, raw_message, run_id, history,
                                   reply_to=source_message_id)
        elif target_type == "clarify":
            await _ask_clarification(director_telegram_user_id, task_summary, clarify_options(result), run_id,
                                     reply_to=source_message_id)
        elif target_type == "refused":
            # The guardrail path — the model's own polite refusal, already in
            # Uzbek/Russian per prompt.py's GUARDRAILS block.
            log.info("Classification refused a message from {}", director_telegram_user_id)
            await _reply_and_log(director_telegram_user_id, run_id, sanitize_model_html(task_summary), source_message_id)
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
                source_message_id,
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


async def _ask_clarification(director_id: int, question: str, options: list[str], run_id: uuid.UUID,
                             reply_to: int | None = None) -> None:
    """The router didn't understand: ask back, with its 2–3 readings as buttons (2026-10-10).

    The question and the options go into the conversation memory, so a typed
    answer ("иккинчиси", or his own words) is understood on the next message.
    """
    question = sanitize_model_html(question) or "Аниқ тушунмадим — нимани назарда тутдингиз?"
    if not options:
        await _reply_and_log(director_id, run_id, question, reply_to)
        return
    row = await store.create_clarification(director_id, _plain(question), options)
    await _reply(director_id, run_id, question, reply_markup=clarify_keyboard(str(row["id"]), options), reply_to=reply_to)
    await store.log_conversation_turn(
        director_id, "bot", question + "\n" + "\n".join(f"{i}) {o}" for i, o in enumerate(options, start=1))
    )


async def _handle_clarification(
    rest: str, callback: dict[str, Any], run_id: uuid.UUID, background: BackgroundTasks | None
) -> str:
    """The Director tapped one meaning: it goes on as if he had written it."""
    query_id = callback.get("id", "")
    clicker_id = (callback.get("from") or {}).get("id")
    clarification_id, _, index_text = rest.rpartition(":")
    if not clarification_id or not index_text.isdigit() or clicker_id is None:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"
    row = await store.choose_clarification(clarification_id, clicker_id, int(index_text))
    if row is None:
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "clarification_closed"
    choice = row["options"][row["chosen"]]
    await _answer(query_id, "OK")
    message = callback.get("message") or {}
    if message.get("message_id") and message.get("chat", {}).get("id"):
        async with TelegramBot(
            agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
        ) as bot:
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]), message_id=message["message_id"],
                text=f"{escape(row['question'])}\n\n✔️ {escape(choice)}", reply_markup={"inline_keyboard": []},
            )
    if background is not None:
        background.add_task(_dispatch_director_task, clicker_id, choice, None, run_id)
    else:
        await _dispatch_director_task(clicker_id, choice, None, run_id)
    return "clarification_chosen"


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
    reminded: list[dict[str, Any]] = []
    sent = await _send_task_cards(draft, recipients, run_id, reminded)

    if sent:
        text = task_picker.sent_text(draft["task_summary"], sent)
    elif reminded:
        text = f"📋 {sanitize_model_html(draft['task_summary'])}"
    else:
        text = "⚠️ Ҳеч кимга етказиб бўлмади."
    if reminded:
        text += "\n" + task_picker.reminded_text(reminded)
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


async def _send_report_calendar(chat_id: int, run_id: uuid.UUID, roles: tuple[str, ...] | None = None) -> str:
    """/hisobotlar: green sent, red missed, this month (a text if the picture fails) — the Director
    everyone's, the Garmin manager his team's (``roles``)."""
    png, view = await report_calendar.picture(roles=roles)
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if png is not None:
            await bot.send_file(png, "hisobotlar.png", str(chat_id), report_calendar.caption(view), photo=True)
            return "report_calendar"
        await bot.send_message(report_calendar.text_fallback(view), chat_id=str(chat_id))
    return "report_calendar_text"


async def _send_task_cards(draft: dict[str, Any], employees: list[dict[str, Any]], run_id: uuid.UUID,
                           reminded: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One task row and card per person — a text card, or the Director's file copied with the card as caption.

    Someone who already has the same task open (2026-10-17, the owner) gets no second row: a reminder
    goes on their existing card and they are added to ``reminded``.
    """
    sent: list[dict[str, Any]] = []
    director_id = draft["director_telegram_user_id"]
    due_date = draft["due_date"]
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for employee in employees:
            existing = next((t for t in await store.open_tasks_of(str(employee["id"]))
                             if task_register.same_task(t, draft)), None)
            if existing is not None:
                try:
                    await bot.send_message(task_register.repeated_text(existing), chat_id=str(employee["telegram_user_id"]),
                                           reply_to_message_id=existing.get("telegram_message_id"))
                except TelegramError as exc:
                    log.error("Could not remind {} of a repeated task: {}", names.person_name(employee), exc)
                    continue
                await store.log_task_event(str(existing["id"]), "repeated", str(director_id))
                if reminded is not None:
                    reminded.append(employee)
                continue
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
                            "caption": _task_card_text(draft["task_summary"], due_date=due_date, code=task_register.code_of(task)),
                            "parse_mode": "HTML",
                            "reply_markup": task_register.keyboard(task),
                        },
                        mode="notify",
                        target_ref=str(employee["telegram_user_id"]),
                    )
                    message_id = result.get("message_id") if result else None
                else:
                    ids = await bot.send_message(
                        _task_card_text(draft["task_summary"], draft["raw_message"], due_date, task_register.code_of(task)),
                        chat_id=str(employee["telegram_user_id"]),
                        reply_markup=task_register.keyboard(task),
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
    director_telegram_user_id: int, source_message_id: int | None, caption: str, run_id: uuid.UUID,
    image: tuple[str, str] | None = None,
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
        image: The picture's (file_id, mime) when the media is one — read
            first as a possible cheque (``cheques.py``, 2026-10-14).
    """
    if source_message_id is None:
        log.warning("Media message from {} had no message_id, cannot forward", director_telegram_user_id)
        await _safe_notify_failure(director_telegram_user_id, run_id)
        return

    if image is not None and settings.cheque_reader_enabled:
        try:
            if await _handle_cheque_photo(director_telegram_user_id, source_message_id, caption, image, run_id):
                return
        except Exception as exc:  # noqa: BLE001 — a failed read leaves the photo to the old path
            log.error("Cheque reading failed ({}), the photo goes to the people list", type(exc).__name__)

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


async def _handle_cheque_photo(
    director_id: int, source_message_id: int, caption: str, image: tuple[str, str], run_id: uuid.UUID
) -> bool:
    """A Director's photo read as a cheque (``cheques.py``). False = not a cheque, the old path takes it."""
    file_id, mime = image
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        picture = await bot.download_file(file_id)
    async with OpenRouterClient(
        agent=AGENT, run_id=run_id, model_override=settings.ops_manager_bot_model,
        fallback_override=settings.ops_manager_bot_fallback_models,
    ) as ai:
        result = await ai.vision_json(cheques.READ_SYSTEM_PROMPT, cheques.READ_USER_TEXT, picture, mime)
    reading = cheques.read_result(result)
    if reading is None:
        log.info("A Director's photo is not a cheque — the people list")
        return False

    stored = cheques.stored_fields(reading)
    found = cheques.problems(reading, today_local())
    earlier = await store.earlier_cheque(stored)
    if earlier is not None:
        found.append(cheques.repeat_problem(earlier))
    cheque = await store.create_cheque(director_id, source_message_id, caption, stored, found)
    # Counts only — a cheque's details are company figures (the Director's order of 07.10).
    await log_action(
        agent=AGENT, action="cheque_read", target_system="openrouter", status="success", run_id=run_id,
        mode="read", payload={"complete": not found, "problems": len(found)},
    )
    if found:
        await _reply(director_id, run_id, cheques.question_text(stored, found), cheques.question_keyboard(str(cheque["id"])))
    else:
        await _reply(director_id, run_id, await _send_cheque(cheque, run_id))
    return True


async def _send_cheque(cheque: dict[str, Any], run_id: uuid.UUID, forced: list[str] | None = None) -> str:
    """The photo with its details to accounting; returns the line for the Director."""
    recipients = [
        person for role in cheques.recipient_roles(settings.cheque_recipient_roles)
        for person in await store.active_employees_by_role(role)
    ]
    if not recipients:
        await store.resolve_cheque(str(cheque["id"]), "no_recipient")
        return cheques.no_recipient_text()
    if await store.resolve_cheque(str(cheque["id"]), "sent", len(recipients)) is None:
        return "Аллақачон ҳал қилинган."
    caption = cheques.accounting_caption(cheque.get("fields") or {}, cheque.get("caption"), forced)
    delivered: list[str] = []
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for person in recipients:
            try:
                await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                    "copyMessage",
                    {"chat_id": str(person["telegram_user_id"]), "from_chat_id": str(cheque["director_telegram_user_id"]),
                     "message_id": cheque["source_message_id"], "caption": caption, "parse_mode": "HTML"},
                    mode="notify", target_ref=str(person["telegram_user_id"]),
                )
                delivered.append(names.person_name(person))
            except TelegramError as exc:
                log.error("Could not send a cheque to accounting: {}", type(exc).__name__)
    await log_action(
        agent=AGENT, action="cheque_sent", target_system="telegram", status="success" if delivered else "failure",
        run_id=run_id, mode="write", payload={"recipients": len(recipients), "delivered": len(delivered)},
    )
    return cheques.sent_text(delivered) if delivered else "🧾 Чекни бухгалтерияга юбориб бўлмади — бироздан кейин яна юборинг."


async def _handle_cheque_button(rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """📨 send as it is / 📋 not a cheque / ❌ cancel on a cheque question."""
    query_id = callback.get("id", "")
    action, _, cheque_id = rest.partition(":")
    cheque = await store.get_cheque(cheque_id)
    if cheque is None or action not in ("s", "t", "x"):
        await _answer(query_id, "Топилмади")
        return "not_found"
    if callback.get("from", {}).get("id") != cheque["director_telegram_user_id"]:
        await _answer(query_id, "Бу сизнинг сўровингиз эмас")
        return "unauthorized"
    if cheque.get("resolved_at") is not None:
        await _answer(query_id, "Аллақачон ҳал қилинган")
        return "already_resolved"
    await _answer(query_id, "OK")
    message = callback.get("message") or {}
    if action == "s":
        outcome = await _send_cheque(cheque, run_id, forced=list(cheque.get("problems") or []))
        result = "cheque_sent"
    elif action == "t":
        if await store.resolve_cheque(cheque_id, "as_task") is None:
            return "already_resolved"
        outcome = "📋 Чек эмас — ким учун эканини танланг."
        result = "cheque_as_task"
    else:
        if await store.resolve_cheque(cheque_id, "cancelled") is None:
            return "already_resolved"
        outcome = "❌ Бекор қилинди — чек юборилмади."
        result = "cheque_cancelled"
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._edit_message(  # noqa: SLF001
            str(cheque["director_telegram_user_id"]), message.get("message_id"), outcome, {"inline_keyboard": []},
        )
    if action == "t":
        await _ask_media_target(cheque["director_telegram_user_id"], cheque["source_message_id"],
                                cheque.get("caption") or "", run_id)
    return result


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


async def _answer_question(
    director_id: int, agent_slug: str, question: str, run_id: uuid.UUID, history: str = "", *,
    reply_to: int | None = None,
) -> None:
    """A Director's question: the analyst looks it up across every system (analyst.py).

    Falls back to the one-source answer (``_answer_from_agent``) when the
    analyst is switched off or the AI couldn't run its lookups.
    """
    if settings.ops_analyst_enabled:
        from integrations.org_bot import analyst

        hint = f"{agent_slug} ({AGENT_LABELS.get(agent_slug, agent_slug)})"
        try:
            result = await analyst.answer(question, history, run_id, hint=hint)
        except Exception as exc:  # noqa: BLE001 — the old path still answers
            log.error("Analyst failed ({}), answering from {} alone", type(exc).__name__, agent_slug)
            await log_action(
                agent=AGENT, action="analyst_fallback", target_system="openrouter", status="failure",
                run_id=run_id, mode="read", error_message=type(exc).__name__,
            )
        else:
            if result.text.strip():
                log.info("Analyst answered in {} round(s) using {}", result.rounds, ", ".join(result.tools) or "no tools")
                await log_action(
                    agent=AGENT, action="analyst_answer", target_system="openrouter", status="success",
                    run_id=run_id, mode="read", payload={"rounds": result.rounds, "tools": result.tools},
                )
                await _reply_and_log(director_id, run_id, sanitize_model_html(result.text), reply_to)
                await _send_files(director_id, run_id, result.files, reply_to)
                await _send_media(director_id, run_id, result.media, reply_to)
                return
            log.warning("Analyst returned nothing, answering from {} alone", agent_slug)
    await _answer_from_agent(director_id, agent_slug, question, run_id, history, reply_to=reply_to)


async def _send_files(director_id: int, run_id: uuid.UUID, files: list[tuple[bytes, str]],
                      reply_to: int | None = None) -> None:
    """Files the analyst made for its answer (the 1C ↔ SAP supplier-debt workbook), after the text.

    ``director_id``: whoever asked — the Director, or an employee given data access.
    """
    if not files:
        return
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for content, filename in files:
            try:
                await bot.send_file(content, filename, str(director_id), reply_to=reply_to)
            except Exception as exc:  # noqa: BLE001 — the text answer already went
                log.error("Sending {} to the Director failed: {}", filename, type(exc).__name__)
                await bot.send_message("Файлни юбориб бўлмади — бироздан кейин яна сўранг.", chat_id=str(director_id))


async def _send_media(chat_id: int, run_id: uuid.UUID, media: list[dict[str, Any]], reply_to: int | None = None) -> None:
    """A report's own files (photo, video, voice…), after the answer, as they were sent (2026-10-22): by the
    kept Telegram file_id, else copied from the employee's chat."""
    if not media:
        return
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for f in media:
            caption = report_media.media_caption(f.get("who"), f.get("created_at"), f.get("text") or f.get("caption"))
            try:
                if f.get("file_id"):
                    await bot.send_media(f["kind"], f["file_id"], str(chat_id), caption, reply_to)
                else:
                    await bot._call(  # noqa: SLF001 — same-package reuse of the low-level Bot API primitive
                        "copyMessage", {"chat_id": str(chat_id), "from_chat_id": str(f["telegram_user_id"]),
                                        "message_id": f["message_id"], "caption": caption, "parse_mode": "HTML"},
                        mode="notify", target_ref=str(chat_id))
            except Exception as exc:  # noqa: BLE001 — the next file still goes
                log.error("A report file could not be sent: {}", type(exc).__name__)


async def _answer_from_agent(
    director_id: int, agent_slug: str, question: str, run_id: uuid.UUID, history: str = "", *,
    reply_to: int | None = None,
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
    log.info("Answering from agent={} -- {} char(s) of data", agent_slug, len(data))
    async with OpenRouterClient(
        agent=AGENT,
        run_id=run_id,
        model_override=settings.ops_manager_bot_model,
        fallback_override=settings.ops_manager_bot_fallback_models,
    ) as ai:
        answer = await ai.complete(
            ANSWER_SYSTEM_PROMPT, build_answer_message(AGENT_LABELS[agent_slug], data, question, history)
        )
    log.info("Answer for agent={}: {} char(s)", agent_slug, len(answer))
    await _reply_and_log(director_id, run_id, sanitize_model_html(answer), reply_to)


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
    lines.append("States: Яратилди = not accepted yet; Қабул қилинди = in progress; Тўсиқ бор = blocked (reason shown); "
                 "Текширувда = done with proof, waiting for the one who assigned it to accept (✅) or return (↩️).")
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
                f"- {task['display_name']} ({task['role']}) | {task_register.answer_line(task)} | {state} | "
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


LEAD_SHEET_COLUMNS = leads.SHEET_COLUMNS  # the one list, shared with the Lead Agent


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


async def sap_freshness() -> tuple[str, bool]:
    """A line saying when SAP last pushed, and whether that's too long ago (the brief's rule)."""
    row = await fetch_one(
        "SELECT max(occurred_at) AS at FROM agent_actions WHERE agent = 'sap-gateway-push' AND status = 'success'"
    )
    if not row or not row["at"]:
        return "SAP has never pushed any data.", True
    pushed = to_local(row["at"])
    hours = (now_local() - pushed).total_seconds() / 3600
    if hours > 3:
        return (
            f"WARNING: SAP last pushed data at {pushed:%Y-%m-%d %H:%M} Tashkent ({int(hours)} hours ago) and has "
            "sent nothing since — the push stopped. Everything below is as of then, NOT today: say so plainly, "
            "with that date and time, and don't present it as current.",
            True,
        )
    return f"SAP data is current: last pushed {pushed:%Y-%m-%d %H:%M} Tashkent.", False


async def _fetch_finance_agent_data() -> str:
    """All open receivables (= open SAP invoices) + recent alerts, not just the top 15/5."""
    aging = await fetch_all(
        "SELECT doc_num, card_name, doc_date, days_overdue, aging_bucket, balance_due_tiyin, currency, due_date, "
        "sales_person_name, captured_at, doc_currency, doc_total_fc_tiyin, paid_fc_tiyin "
        "FROM v_ar_aging_latest ORDER BY balance_due_tiyin DESC"
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
    freshness, _silent = await sap_freshness()
    written = [_as_written(r) for r in aging]
    in_own = sum(1 for (_amount, currency), r in zip(written, aging) if currency != r["currency"])
    lines = [
        freshness,
        f"{len(aging)} open SAP invoice(s) / receivable(s) (source: SAP Business One OINV)"
        + (f", snapshot taken {to_local(aging[0]['captured_at']):%Y-%m-%d %H:%M} Tashkent" if aging else "") + ".",
        # Checked against the database 2026-10-06: the invoices are written in
        # so'm (DocCur=UZS) but SAP's DocTotal is its local currency, USD — the
        # Director looked an invoice up, saw so'm, and couldn't match "$"; asked
        # "Сделай в суммах", the bot could only say it had no rate.
        _currency_note(in_own, len(aging)),
        "OVERDUE: most of these invoices have their due date equal to the invoice date (no payment terms set in "
        "SAP), so 'days overdue' here means days since the invoice — say 'unpaid for N days' rather than "
        "implying a missed agreed deadline.",
    ]
    if aging:
        # Totals worked out here, not left to the model to add up.
        lines.append(f"TOTAL open: {format_money_by_currency(written)} ({len(aging)} invoices). By days since due date:")
        for bucket in ("current", "1_30", "31_60", "61_90", "90_plus"):
            amounts = [w for w, r in zip(written, aging) if r["aging_bucket"] == bucket]
            if amounts:
                lines.append(f"  {bucket}: {format_money_by_currency(amounts)} ({len(amounts)} invoices)")
    for (amount, currency), r in list(zip(written, aging))[:200]:
        # Pre-formatted with format_money rather than handed to the model as
        # "<number> <code>": given the raw pair, the model reasonably renders
        # "UZS" as "so'm" in an Uzbek reply, so a wrong code upstream turned
        # into confidently wrong output. Passing "$9,764.31" already formatted
        # leaves nothing to reinterpret, and makes a wrong currency obvious on
        # sight instead of laundered through translation.
        amount = format_money(amount, currency)
        if currency != r["currency"]:
            amount += f" (SAP's {r['currency']} equivalent {format_money(r['balance_due_tiyin'], r['currency'])})"
        lines.append(
            f"- Invoice #{r['doc_num']} dated {r['doc_date']}, {r['card_name']}: {amount}, "
            f"{r['days_overdue']}d past due date "
            f"({r['aging_bucket']}), due {r['due_date']}, owner={r['sales_person_name'] or 'not set in SAP'}"
        )
    if alerts:
        lines.append("\nRecent receivables alerts:")
        for a in alerts:
            lines.append(f"- {a['title']}: {a.get('body') or ''} ({a['created_at']})")
    return "\n".join(lines)


def _as_written(row: dict[str, Any]) -> tuple[int, str]:
    """(open balance, currency) as on the invoice in SAP; SAP's local amount until the gateway sends it."""
    balance = doc_balance(row.get("doc_total_fc_tiyin"), row.get("paid_fc_tiyin"))
    if row.get("doc_currency") and balance and balance > 0:
        return balance, row["doc_currency"]
    return row["balance_due_tiyin"], row["currency"]


def _currency_note(in_own: int, total: int) -> str:
    """How to read the amounts: as written on the invoices (so'm), or SAP's USD equivalents."""
    if total and in_own == total:
        return ("CURRENCY: each amount is the invoice's own, as written in SAP (so'm) — what the Director sees "
                "opening the invoice — with SAP's USD equivalent in brackets. Lead with the so'm amounts.")
    if in_own:
        return (f"CURRENCY: {in_own} of {total} invoices show their own so'm amount as written in SAP (SAP's USD "
                "equivalent in brackets); the rest are SAP's USD equivalents only. Label each amount with its own "
                "currency and never add so'm and $ together.")
    return ("CURRENCY: the amounts below are SAP's local currency, USD — the equivalent of each invoice. The "
            "invoices themselves are written in so'm (UZS); opening an invoice in SAP by its number shows its so'm "
            "amount, which the SAP connection doesn't send yet. Always say the figures are USD equivalents, never "
            "call them so'm. Asked for so'm: say the so'm amounts are being added to the SAP connection — never "
            "convert with a guessed rate.")


SAP_ANSWER_ROWS = 3000


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
    # A complete gateway tool (2026-10-03, docs/sap-gateway-tools.md) can send
    # more than 100 rows of a kind — all of it is shown.
    rows = await fetch_all(
        "SELECT natural_key, raw, captured_at FROM v_sap_gateway_latest WHERE tool = %s "
        "ORDER BY captured_at DESC LIMIT %s",
        (tool, SAP_ANSWER_ROWS),
    )
    if not rows:
        return (
            f"No {tool} data pushed yet from the SAP gateway. The push script "
            f"(scripts/sap-gateway-push/) needs to call get_{tool} and push it "
            f"to /webhooks/sap-gateway-push/{tool}/<secret> at least once."
        )

    freshness, _silent = await sap_freshness()
    push = await fetch_one(
        "SELECT payload FROM agent_actions WHERE agent = 'sap-gateway-push' AND action = %s "
        "ORDER BY occurred_at DESC LIMIT 1",
        (f"gateway_push_{tool}",),
    )
    payload = (push or {}).get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    lines = [freshness]
    if not payload.get("complete") and len(rows) in (20, 100, 101):
        # The gateway's original tools return only their latest 20/100 rows
        # (docs/sap-gateway-tools.md, "Why") — a count from them is not a total.
        lines.append(
            f"WARNING: PARTIAL LIST — the SAP gateway's {tool} tool sends only its latest {len(rows)} rows, not "
            f"all {tool}. Don't present counts, totals or 'all' from it as company-wide figures; say it covers "
            f"only the latest {len(rows)} records."
        )
    lines.append(f"{len(rows)} {tool} record(s), captured {to_local(rows[0]['captured_at']):%Y-%m-%d %H:%M} Tashkent"
                 + (f" (only the first {SAP_ANSWER_ROWS} shown):" if len(rows) >= SAP_ANSWER_ROWS else ":"))
    for r in rows:
        raw = r["raw"] if isinstance(r["raw"], dict) else {}
        fields = ", ".join(f"{k}={v}" for k, v in raw.items() if v is not None)
        lines.append(f"- {fields}")
    return "\n".join(lines)


async def _fetch_reporter_agent_data() -> str:
    """The last 14 days of daily briefs, so trend questions aren't limited
    to a single snapshot the way a one-day-only fetch would be."""
    briefs = await fetch_all(
        "SELECT DISTINCT ON (brief_date) brief_date, ar_overdue_total_tiyin, sections FROM daily_briefs "
        "ORDER BY brief_date DESC, generated_at DESC LIMIT 14"
    )
    if not briefs:
        return "No daily brief has been generated yet."

    lines = [
        f"Daily brief history, most recent first ({len(briefs)} day(s)). Amounts are SAP's local currency (USD).",
        # Until the complete gateway tool (2026-10-03) the push sent only the
        # latest ~20 invoices without payments — those days' debt was partial,
        # and the jump to the next day is a change of source, not of debt.
        "Days marked PARTIAL came from the old SAP push that sent only part of the invoices: never compare them "
        "with complete days or call the difference growth.",
    ]
    for brief in briefs:
        overdue = brief["ar_overdue_total_tiyin"]
        sections = brief["sections"] if isinstance(brief["sections"], dict) else json.loads(brief["sections"] or "{}")
        debt = (sections.get("a2") or {}).get("debt") or {}
        partial = debt.get("capped", True)
        lines.append(
            f"- {brief['brief_date']}: AR overdue="
            f"{format_money(overdue, settings.sap_default_currency) if overdue is not None else 'no data'}"
            + (" (PARTIAL — not comparable)" if partial else " (complete)")
        )
    return "\n".join(lines)
