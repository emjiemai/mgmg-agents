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
    build_answer_message,
    build_classify_message,
)
from integrations.org_bot.roles import (
    AGENT_LABELS,
    AGENT_SLUGS,
    DIRECTOR_ROLE,
    ROLE_LABELS,
    ROLE_SLUGS,
    role_picker_keyboard,
)
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "ops-manager-bot"
log = setup_logging(AGENT)


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
        internally consistent and every slug is real, else None — callers
        must treat None the same as an explicit "none" (ask the Director to
        clarify), never as a silent default route.
    """
    target_type = result.get("target_type")
    target_role = result.get("target_role")
    target_agent = result.get("target_agent")

    if target_type == "none":
        return "none", None, None
    if target_type == "employee" and target_role in ROLE_SLUGS:
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
    if prefix == "taskdone":
        return await _handle_task_done(rest, callback, run_id)

    await _answer(query_id, "Unrecognized action")
    return "unrecognized"


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
            await bot._edit_message(  # noqa: SLF001
                chat_id=str(message["chat"]["id"]),
                message_id=message["message_id"],
                text=f"✅ Bajarildi — {escape(task['task_summary'])}",
            )
        await bot._answer_callback(query_id, "Marked done")  # noqa: SLF001

    try:
        await _reply(
            task["director_telegram_user_id"],
            run_id,
            f"✅ Bajarildi / Done: {escape(task['task_summary'])} (@{escape(completed_by)})",
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
        await _reply(
            telegram_user_id,
            run_id,
            "Bu botda faqat Operatsion Direktor topshiriq bera oladi.\n"
            "Only the Operations Director can assign tasks here.",
        )
        return "not_director"

    text = (message.get("text") or "").strip()
    if not text:
        return "ignored"

    await _reply(telegram_user_id, run_id, "👍 Qabul qildim, yo'naltiryapman... / Got it, routing...")
    background.add_task(_dispatch_director_task, telegram_user_id, text, message.get("message_id"), run_id)
    return "queued"


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


async def _reply(telegram_user_id: int, run_id: uuid.UUID, text: str) -> None:
    """Send a plain message to one user via OPS Manager Bot's own token."""
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot.send_message(text, chat_id=str(telegram_user_id))


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
    try:
        async with OpenRouterClient(
            agent=AGENT,
            run_id=run_id,
            provider_override=settings.ops_manager_bot_provider,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            result = await ai.complete_json(CLASSIFY_SYSTEM_PROMPT, build_classify_message(raw_message))

        task_summary = (result.get("task_summary") or raw_message[:200]).strip()
        validated = validate_classification(result)

        if validated is None:
            # Model returned an out-of-enum value -- code-level backstop,
            # the proportionate equivalent of Lead Agent's second AI pass for
            # this much narrower (12-way) classification task.
            log.warning("Classification returned unrecognized target: {}", result)
            await _reply(
                director_telegram_user_id,
                run_id,
                "Aniq tushunmadim — iltimos, aniqroq yozing.\nCouldn't tell — could you clarify?",
            )
            return

        target_type, target_role, target_agent = validated
        if target_type == "employee":
            await _dispatch_to_role(
                director_telegram_user_id, source_message_id, raw_message, target_role, task_summary, run_id
            )
        elif target_type == "agent":
            await _answer_from_agent(director_telegram_user_id, target_agent, raw_message, run_id)
        else:  # "none"
            await _reply(
                director_telegram_user_id,
                run_id,
                "Buni kimga yo'naltirishni tushunmadim — aniqroq yozib bera olasizmi?\n"
                "I couldn't tell who this is for — could you rephrase it?",
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
        await _reply(
            director_id,
            run_id,
            f"{ROLE_LABELS[role_slug]} uchun hali hech kim ro'yxatdan o'tmagan.\n"
            f"No one is registered for {ROLE_LABELS[role_slug]} yet.",
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

            keyboard = {"inline_keyboard": [[{"text": "✅ Bajarildi / Mark Done", "callback_data": f"taskdone:{task['id']}"}]]}
            text = f"📋 <b>Yangi topshiriq / New task</b>\n\n{escape(task_summary)}"
            message_ids = await bot.send_message(
                text, chat_id=str(employee["telegram_user_id"]), reply_markup=keyboard
            )
            if message_ids:
                await store.set_task_message_id(str(task["id"]), message_ids[0])
            sent_names.append(employee["display_name"])

    if sent_names:
        names = ", ".join(escape(n) for n in sent_names)
        await _reply(director_id, run_id, f"Yuborildi / Sent to: {names} ({ROLE_LABELS[role_slug]}).")


async def _answer_from_agent(director_id: int, agent_slug: str, question: str, run_id: uuid.UUID) -> None:
    """Answer a Director's question using one agent's already-computed data."""
    data = await _fetch_agent_data(agent_slug)
    async with OpenRouterClient(
        agent=AGENT,
        run_id=run_id,
        provider_override=settings.ops_manager_bot_provider,
        model_override=settings.ops_manager_bot_model,
        fallback_override=settings.ops_manager_bot_fallback_models,
    ) as ai:
        answer = await ai.complete(ANSWER_SYSTEM_PROMPT, build_answer_message(AGENT_LABELS[agent_slug], data, question))
    await _reply(director_id, run_id, escape(answer))


# ------------------------------------------------------------- agent data reads
# No live agent invocation in v1 (per the business owner's own instruction) --
# each fetcher reads whatever that agent already computed and stored.


async def _fetch_agent_data(agent_slug: str) -> str:
    """Dispatch to the per-agent data fetcher below."""
    fetchers = {
        "lead_agent": _fetch_lead_agent_data,
        "finance_agent": _fetch_finance_agent_data,
        "crm_agent": _fetch_crm_agent_data,
        "reporter_agent": _fetch_reporter_agent_data,
    }
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
    """All open receivables + recent alerts, not just the top 15/5."""
    aging = await fetch_all(
        "SELECT card_name, days_overdue, aging_bucket, balance_due_uzs, due_date, sales_person_name "
        "FROM v_ar_aging_latest ORDER BY balance_due_uzs DESC LIMIT 200"
    )
    alerts = await fetch_all(
        "SELECT title, body, created_at FROM alerts WHERE agent = 'receivables' ORDER BY created_at DESC LIMIT 30"
    )
    if not aging and not alerts:
        return "No receivables data recorded yet."

    lines = [f"{len(aging)} open receivable(s):"]
    for r in aging:
        lines.append(
            f"- {r['card_name']}: {r['balance_due_uzs']} UZS, {r['days_overdue']}d overdue "
            f"({r['aging_bucket']}), due {r['due_date']}, owner={r['sales_person_name']}"
        )
    if alerts:
        lines.append("\nRecent receivables alerts:")
        for a in alerts:
            lines.append(f"- {a['title']}: {a.get('body') or ''} ({a['created_at']})")
    return "\n".join(lines)


async def _fetch_crm_agent_data() -> str:
    """The full pipeline snapshot + recent deal events, not just the last 10."""
    pipeline = await fetch_all(
        "SELECT pipeline_name, status_name, deals_count, deals_value_uzs, division FROM v_pipeline_latest"
    )
    events = await fetch_all(
        "SELECT event_type, lead_id, price_tiyin, received_at, processing_status FROM amocrm_deal_events "
        "ORDER BY received_at DESC LIMIT 100"
    )
    if not pipeline and not events:
        return "No CRM pipeline data recorded yet."

    lines = ["Pipeline snapshot (every stage):"]
    for p in pipeline:
        lines.append(
            f"- {p.get('pipeline_name')} / {p.get('status_name')} ({p.get('division')}): "
            f"{p.get('deals_count')} deal(s), {p.get('deals_value_uzs')} UZS"
        )
    if events:
        lines.append(f"\nRecent deal events ({len(events)}):")
        for e in events:
            price = f", {e['price_tiyin'] / 100:.0f} UZS" if e.get("price_tiyin") else ""
            lines.append(f"- {e['event_type']} lead {e['lead_id']}{price} ({e['received_at']}, {e['processing_status']})")
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
