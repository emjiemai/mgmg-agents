"""The written-permission conversation — EMJ-SOP-ADM-01, end to end.

An employee says they need permission; the bot opens a request, fills in
whatever their message already states, asks for the rest one question at a
time, shows them the finished form, and sends it to the authorised approvers
with the SOP's four outcomes as buttons. The decision comes back to the
requester with a filled .docx of the SOP's own form attached.

Two SOP rules are enforced in code rather than trusted to habit:
  * Nobody approves their own request (§3) — the requester is removed from the
    approver list, and a request with no one left to decide it is not sent.
  * The approver, the timestamps and the full decision history are recorded
    (§3), which is what lets an electronic approval count at all.

Kept out of ops_manager.py so that file stays about task routing: this module
owns every message, button and document of the permission flow.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from integrations.ai.openrouter_client import OpenRouterClient, OpenRouterError
from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import docx_form, permissions, store
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "permissions"
log = setup_logging(AGENT)

PREFILL_SYSTEM = """You extract fields for a written permission request form \
(EMJ-SOP-ADM-01) from an employee's own message.

Return ONLY a JSON object with exactly these string keys:
  subject — what permission is being asked for
  reason — why it is needed and what they propose
  amount — the amount and currency exactly as written ("0" if they say there is no cost)
  execute_by — by when the work must be done
  decision_needed_by — by when they need the decision
  urgency — the stated reason for urgency, or "оддий" if they say it is not urgent
  attachments — attachment names, or "йўқ" if they say there are none

Rules: never invent a value. Copy the employee's own wording and alphabet. \
If the message does not clearly state a field, return "" for it — an empty \
string is always better than a guess, because the bot will simply ask."""


def _label(role: str) -> str:
    """Role label as the SOP form's "Бўлим"/position column."""
    return ROLE_LABELS.get(role, role)


def _with_labels(request: dict[str, Any]) -> dict[str, Any]:
    """A request row plus the role label the card and form display."""
    enriched = dict(request)
    enriched["requester_role_label"] = _label(request.get("requester_role", ""))
    return enriched


async def _send(chat_id: int, text: str, run_id: uuid.UUID, keyboard: dict[str, Any] | None = None) -> list[int]:
    """Send one message on OPS Manager Bot's token."""
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        return await bot.send_message(text, chat_id=str(chat_id), reply_markup=keyboard)


async def _send_document(path: Path, chat_id: int, caption: str, run_id: uuid.UUID) -> None:
    """Send the filled form, logging (not raising) if Telegram refuses it."""
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        try:
            await bot.send_document(str(path), chat_id=str(chat_id), caption=caption)
        except TelegramError as exc:
            log.error("Could not send the permission form to {}: {}", chat_id, exc)


async def _approvers(exclude_telegram_user_id: int) -> list[dict[str, Any]]:
    """Who may decide a request, minus the person who raised it.

    Directors plus the deputies named in ``PERMISSION_DEPUTY_TELEGRAM_IDS``.
    Excluding the requester is the SOP's "do not approve your own request"
    rule (§3), enforced here rather than left to the approver to notice.
    """
    found: dict[int, dict[str, Any]] = {}
    for director in await store.active_employees_by_role(DIRECTOR_ROLE):
        found[director["telegram_user_id"]] = director
    for deputy_id in settings.permission_deputy_ids:
        deputy = await store.get_employee_by_telegram_id(deputy_id)
        if deputy is not None and deputy["status"] == "active":
            found[deputy["telegram_user_id"]] = deputy
    found.pop(exclude_telegram_user_id, None)
    return list(found.values())


async def _prefill(text: str, run_id: uuid.UUID) -> dict[str, str]:
    """Pull whatever the opening message already states, via one AI call.

    Returns:
        Field values keyed like the SOP form; empty on any failure — the bot
        then simply asks every question, which is the normal path anyway.
    """
    if len(text.strip()) < 25:  # "ruxsat kerak" carries nothing to extract
        return {}
    try:
        async with OpenRouterClient(
            agent=AGENT,
            run_id=run_id,
            provider_override=settings.ops_manager_bot_provider,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            result = await ai.complete_json(PREFILL_SYSTEM, text)
    except OpenRouterError as exc:
        log.warning("Permission prefill failed, asking every field instead: {}", exc)
        return {}
    return {key: str(value).strip() for key, value in result.items() if isinstance(value, (str, int, float))}


async def _apply_prefill(request: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Store the extracted values on the draft, skipping blanks."""
    current = request
    for field in permissions.FIELDS:
        value = (values.get(field.key) or "").strip()
        if not value:
            continue
        if field.key == "amount":
            amount, currency = permissions.parse_amount(value)
            current = await store.set_permission_amount(str(request["id"]), amount, currency, value) or current
        else:
            current = await store.set_permission_field(str(request["id"]), field.key, value) or current
    return current


async def _ask_next(request: dict[str, Any], run_id: uuid.UUID) -> str:
    """Ask the next missing question, or show the finished form for sending."""
    field = permissions.next_missing_field(request)
    if field is not None:
        await store.set_permission_pending_field(str(request["id"]), field.key)
        await _send(request["requester_telegram_user_id"], f"📝 {escape(field.question)}", run_id)
        return "permission_question"

    await store.set_permission_pending_field(str(request["id"]), None)
    card = permissions.request_card(_with_labels(request), for_approver=False)
    await _send(
        request["requester_telegram_user_id"],
        f"{card}\n\nЮборишни тасдиқланг:",
        run_id,
        permissions.confirm_keyboard(str(request["id"])),
    )
    return "permission_ready"


async def handle_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """Handle one message if it belongs to the permission flow.

    Args:
        employee: The sender's employee row.
        message: The Telegram ``message`` object.
        run_id: UUID grouping this webhook call's audit rows.

    Returns:
        An outcome string when this message was part of the flow (the caller
        must not process it further), or None to fall through to normal task
        routing.
    """
    if not settings.permissions_enabled:
        return None

    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        return None

    telegram_user_id = employee["telegram_user_id"]

    # 1. An approver owes conditions, a rejection reason or an information
    #    request — their next message is that text.
    awaiting = await store.awaiting_permission_note(telegram_user_id)
    if awaiting is not None:
        return await _finalize_decision(awaiting, awaiting["pending_decision"], text, employee, run_id)

    # 2. A half-filled request is waiting for this answer.
    draft = await store.get_permission_draft(telegram_user_id)
    if draft is not None and draft.get("pending_field"):
        field = permissions.FIELD_BY_KEY.get(draft["pending_field"])
        if field is not None:
            if field.key == "amount":
                amount, currency = permissions.parse_amount(text)
                draft = await store.set_permission_amount(str(draft["id"]), amount, currency, text) or draft
            else:
                draft = await store.set_permission_field(str(draft["id"]), field.key, text) or draft
            return await _ask_next(draft, run_id)

    # 3. A new request.
    if not permissions.wants_permission(text):
        return None

    if draft is not None:
        await _send(
            telegram_user_id,
            "Сизда тугалланмаган сўров бор. Аввал шуни тугатинг ёки «❌ Бекор қилиш» тугмасини босинг.",
            run_id,
        )
        return await _ask_next(draft, run_id)

    created = await store.create_permission_draft(employee)
    if created is None:  # lost a race with another message
        return None

    await store.add_permission_event(
        request_id=str(created["id"]),
        actor=employee["display_name"],
        actor_telegram_user_id=telegram_user_id,
        action="created",
        detail=text[:500],
    )
    await _send(
        telegram_user_id,
        "📄 <b>Ёзма рухсат сўрови</b>\n"
        f"<i>{permissions.SOP_CODE} — оғзаки рухсат ҳисобланмайди</i>\n\n"
        "Бир неча савол бераман, сўнг сўровни ваколатли шахсга юбораман.",
        run_id,
    )
    filled = await _apply_prefill(created, await _prefill(text, run_id))
    return await _ask_next(filled, run_id)


async def handle_callback(prefix: str, rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """Handle the flow's own buttons: send, cancel, and the four decisions.

    Returns:
        An outcome string, or None if the button wasn't one of this flow's.
    """
    clicker = callback.get("from", {})
    clicker_id = clicker.get("id")

    if prefix == "permcancel":
        cancelled = await store.cancel_permission_request(rest, clicker_id)
        if cancelled is not None:
            await store.add_permission_event(
                request_id=rest,
                actor=clicker.get("username") or str(clicker_id),
                actor_telegram_user_id=clicker_id,
                action="cancelled",
                detail=None,
            )
            await _send(clicker_id, "❌ Сўров бекор қилинди.", run_id)
        return "permission_cancelled"

    if prefix == "permsend":
        return await _submit(rest, clicker_id, run_id)

    if prefix == "permdec":
        decision, _, request_id = rest.partition(":")
        return await _take_decision(request_id, decision, clicker, run_id)

    return None


async def _submit(request_id: str, clicker_id: int, run_id: uuid.UUID) -> str:
    """Send a finished draft to the approvers."""
    request = await store.get_permission_request(request_id)
    if request is None or request["status"] != "draft":
        return "permission_already_sent"
    if request["requester_telegram_user_id"] != clicker_id:
        return "permission_not_yours"

    approvers = await _approvers(clicker_id)
    if not approvers:
        # SOP §3: nobody may approve their own request. Better to refuse than
        # to let a request sit as "sent" with no one able to decide it.
        await _send(
            clicker_id,
            "⚠️ Ҳозирча ваколатли шахс топилмади (ўз сўровингизни ўзингиз тасдиқлай олмайсиз).\n"
            "Админга мурожаат қилинг — директор ёки ваколатли ўринбосар белгиланиши керак.",
            run_id,
        )
        return "permission_no_approver"

    submitted_to = ", ".join(f"{a['display_name']} ({_label(a['role'])})" for a in approvers)
    request = await store.submit_permission_request(request_id, submitted_to) or request
    await store.add_permission_event(
        request_id=request_id,
        actor=request["requester_name"],
        actor_telegram_user_id=clicker_id,
        action="submitted",
        detail=f"Кимга: {submitted_to}",
    )

    card = permissions.request_card(_with_labels(request), for_approver=True)
    keyboard = permissions.decision_keyboard(request_id)
    for approver in approvers:
        try:
            await _send(approver["telegram_user_id"], card, run_id, keyboard)
        except TelegramError as exc:
            log.error("Could not deliver request {} to {}: {}", request.get("request_no"), approver["telegram_user_id"], exc)

    await _send(
        clicker_id,
        f"📨 Сўров юборилди. Рақами: <b>{escape(request.get('request_no') or '—')}</b>\n"
        f"Кимга: {escape(submitted_to)}\n\n"
        "<i>Қарор келгунича ишни бошламанг (EMJ-SOP-ADM-01).</i>",
        run_id,
    )
    log.info("Permission request {} submitted to {}", request.get("request_no"), submitted_to)
    return "permission_submitted"


async def _take_decision(request_id: str, decision: str, clicker: dict[str, Any], run_id: uuid.UUID) -> str:
    """Record which outcome an approver picked, asking for text when needed."""
    if decision not in permissions.DECISIONS:
        return "permission_unknown_decision"

    clicker_id = clicker.get("id")
    request = await store.get_permission_request(request_id)
    if request is None:
        return "permission_not_found"

    approvers = await _approvers(request["requester_telegram_user_id"])
    if clicker_id not in {a["telegram_user_id"] for a in approvers}:
        # Covers both "not an approver" and the requester tapping their own card.
        await _send(clicker_id, "⚠️ Бу сўров бўйича қарор қабул қилиш ваколатингиз йўқ.", run_id)
        return "permission_not_authorised"

    if request["status"] not in ("submitted", "info_needed"):
        await _send(
            clicker_id,
            f"Бу сўров бўйича қарор аллақачон қабул қилинган: "
            f"{escape(permissions.STATUS_LABELS.get(request['status'], request['status']))}.",
            run_id,
        )
        return "permission_already_decided"

    approver = await store.get_employee_by_telegram_id(clicker_id)
    if decision == "approved":
        return await _finalize_decision(request, decision, None, approver, run_id)

    started = await store.start_permission_decision(request_id, decision, clicker_id)
    if started is None:
        return "permission_already_decided"

    prompts = {
        "approved_conditional": "Шартларни, тасдиқланган сумма ва амал қилиш муддатини ёзинг:",
        "rejected": "Рад этиш сабабини ёзинг:",
        "info_needed": "Қандай қўшимча маълумот керак? Ёзинг:",
    }
    await _send(clicker_id, f"✍️ {prompts[decision]}", run_id)
    return "permission_decision_pending"


async def _finalize_decision(
    request: dict[str, Any],
    decision: str,
    note: str | None,
    approver: dict[str, Any] | None,
    run_id: uuid.UUID,
) -> str:
    """Store the decision, tell the requester, and file the filled form."""
    approver_name = (
        f"{approver['display_name']} ({_label(approver['role'])})" if approver else "Ваколатли шахс"
    )
    approver_id = approver["telegram_user_id"] if approver else None

    if decision == "approved":
        approved_terms = (
            f"{permissions.field_value(_with_labels(request), permissions.FIELD_BY_KEY['amount'])} · "
            f"{request.get('execute_by') or '—'}"
        )
    elif decision == "approved_conditional":
        approved_terms = note
    else:
        approved_terms = None

    decided = await store.decide_permission_request(
        request_id=str(request["id"]),
        status=decision,
        decided_by=approver_name,
        decided_by_telegram_user_id=approver_id,
        approved_terms=approved_terms,
        decision_note=note,
    )
    if decided is None:
        return "permission_already_decided"

    await store.add_permission_event(
        request_id=str(request["id"]),
        actor=approver_name,
        actor_telegram_user_id=approver_id,
        action="decided",
        detail=f"{permissions.DECISIONS[decision][1]}" + (f" — {note}" if note else ""),
    )

    enriched = _with_labels(decided)
    requester_text = permissions.decision_text(enriched)
    if decision in ("approved", "approved_conditional"):
        requester_text += "\n\n<i>Фақат тасдиқланган ҳажм ва шартларда бажаринг.</i>"
    elif decision == "rejected":
        requester_text += "\n\n<i>Рад жавоби билан иш бошланмайди.</i>"
    else:
        requester_text += "\n\n<i>Сўралган маълумотни ёзиб юборинг.</i>"

    await _send(decided["requester_telegram_user_id"], requester_text, run_id)
    if approver_id:
        await _send(approver_id, f"✅ Қарор қайд этилди: № {escape(decided.get('request_no') or '—')}", run_id)

    events = await store.permission_events(str(request["id"]))
    try:
        path = docx_form.build_form(enriched, events)
    except Exception as exc:  # noqa: BLE001 — a document failure must not lose the decision
        log.error("Could not build the permission form for {}: {}", decided.get("request_no"), exc)
        return "permission_decided"

    caption = f"📄 {escape(decided.get('request_no') or '')} — {permissions.DECISIONS[decision][1]}"
    recipients = {decided["requester_telegram_user_id"]}
    if approver_id:
        recipients.add(approver_id)
    for chat_id in recipients:
        await _send_document(path, chat_id, caption, run_id)

    log.info("Permission request {} decided: {}", decided.get("request_no"), decision)
    return "permission_decided"


async def registry_data() -> str:
    """The permission registry, for OPS Manager Bot's own agent answers.

    Returns:
        Plain text for the answering model: what is still waiting for a
        decision first, then the decided requests.
    """
    rows = await store.permission_registry(days=60)
    if not rows:
        return (
            "No written permission requests yet. Employees start one by writing "
            "\"ruxsat\"/\"рухсат\" to this bot (EMJ-SOP-ADM-01); nothing is recorded until then."
        )

    pending = [r for r in rows if r["status"] in ("submitted", "info_needed")]
    lines = [f"Written permission requests (EMJ-SOP-ADM-01), last 60 days: {len(rows)} total."]
    lines.append(f"Awaiting a decision: {len(pending)}.")
    for row in rows:
        enriched = _with_labels(row)
        amount = permissions.field_value(enriched, permissions.FIELD_BY_KEY["amount"])
        submitted = row.get("submitted_at")
        stamp = submitted.strftime("%d.%m.%Y %H:%M") if hasattr(submitted, "strftime") else "—"
        line = (
            f"- {row.get('request_no') or '(no number)'} | {stamp} | "
            f"{row['requester_name']} ({_label(row['requester_role'])}) | "
            f"status: {permissions.STATUS_LABELS.get(row['status'], row['status'])} | "
            f"what: {row.get('subject') or '—'} | amount: {amount} | "
            f"needed by: {row.get('decision_needed_by') or '—'}"
        )
        if row.get("decided_by"):
            line += f" | decided by {row['decided_by']}"
        if row.get("approved_terms"):
            line += f" | approved: {row['approved_terms']}"
        if row.get("decision_note"):
            line += f" | note: {row['decision_note']}"
        lines.append(line)
    return "\n".join(lines)
