"""The written-permission conversation — EMJ-SOP-ADM-01, end to end.

The bot asks the SOP form's questions one at a time. Each answer is checked by
the AI before it is accepted: an answer that is unclear, off-topic or filler
("alo", "test", a "date" with no date in it) is not written onto the form —
the bot says what is missing and asks again. An accepted answer is written in
Uzbek Cyrillic with its spelling fixed, to match the document — the meaning,
facts, numbers and names stay exactly the employee's. The requester sees the
whole form before sending it, so any change the AI made is visible.

Names on the form are the full names people type, never Telegram profile
names; signatures are left blank for signing by hand.

When the form is complete the requester confirms it, it goes to the
authorised approvers with the SOP's four outcomes as buttons, and the decision
returns with the company's own SOP document filled in (see docx_form.py).

Two SOP rules are enforced in code rather than trusted to habit:
  * Nobody approves their own request (§3).
  * The approver, the timestamps and the decision history are recorded (§3).
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

CANCEL_HINT = "\n\n<i>Бекор қилиш: /bekor</i>"

VALIDATE_SYSTEM = """You check ONE answer on a written permission request form \
(EMJ-SOP-ADM-01, written in Uzbek Cyrillic) before it goes onto the company's \
official document.

You are given the form field, what a valid answer looks like, the question the \
employee was asked, and their answer. Employees write in Uzbek (Latin or \
Cyrillic), Russian or English.

1. Decide whether the answer genuinely and clearly answers THIS question.
   Reject greetings ("alo", "salom"), filler or test text ("test", "asd", \
"...", "?"), an answer to a different question, and anything too vague to \
stand on an official document. Do NOT reject for spelling, alphabet, language \
or brevity when the meaning is clear.
2. If it is acceptable, write it for the form:
   - in Uzbek Cyrillic (transliterate Uzbek Latin; translate Russian or \
English words into Uzbek, e.g. "IT Specialist" -> "IT мутахассис");
   - with spelling and grammar corrected, and shaped to fit the form's field \
(e.g. for "Бўлим", "IT bo'limida" -> "IT бўлими");
   - keep abbreviations and brand names as they are (IT, AI, CRM, SAP, \
Telegram, Render);
   - people's names: transliterate to Cyrillic, never change them;
   - NEVER add, remove or change any fact, number, amount, date, time or \
name. Do not expand or embellish: same meaning, roughly the same length.

Return ONLY JSON. Acceptable: {"ok": true, "value": "<the answer for the form>"}. \
Not acceptable: {"ok": false, "follow_up": "<one short, polite question in \
Uzbek Cyrillic that says exactly what is missing or unclear>"}."""


def _label(role: str) -> str:
    """Role label, for the approver list shown to the requester."""
    return ROLE_LABELS.get(role, role)


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
    """Who may decide a request, minus the person who raised it (SOP §3)."""
    found: dict[int, dict[str, Any]] = {}
    for director in await store.active_employees_by_role(DIRECTOR_ROLE):
        found[director["telegram_user_id"]] = director
    for deputy_id in settings.permission_deputy_ids:
        deputy = await store.get_employee_by_telegram_id(deputy_id)
        if deputy is not None and deputy["status"] == "active":
            found[deputy["telegram_user_id"]] = deputy
    found.pop(exclude_telegram_user_id, None)
    return list(found.values())


async def _tier_approver(request: dict[str, Any]) -> dict[str, Any] | None:
    """The B1 limit holder for this request's amount, if one applies.

    Never the requester themselves (SOP §3) and never someone inactive — in
    either case the request simply goes to the Director instead.
    """
    tiers = permissions.parse_tiers(settings.permission_approval_tiers)
    telegram_id = permissions.tier_approver(request.get("amount_tiyin"), request.get("currency"), tiers)
    if telegram_id is None or telegram_id == request["requester_telegram_user_id"]:
        return None
    holder = await store.get_employee_by_telegram_id(telegram_id)
    return holder if holder is not None and holder["status"] == "active" else None


async def _route(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Who receives the card: the limit holder for this amount, else the Director(s)."""
    holder = await _tier_approver(request)
    if holder is not None:
        return [holder]
    return await _approvers(request["requester_telegram_user_id"])


async def _may_decide(request: dict[str, Any], telegram_user_id: int) -> bool:
    """The Director and deputies can always decide; a limit holder only their own tier."""
    allowed = {a["telegram_user_id"] for a in await _approvers(request["requester_telegram_user_id"])}
    holder = await _tier_approver(request)
    if holder is not None:
        allowed.add(holder["telegram_user_id"])
    return telegram_user_id in allowed


async def _check_answer(
    field: permissions.Field, answer: str, run_id: uuid.UUID
) -> tuple[bool, str | None, str]:
    """Ask the AI whether this answer can go onto the official form.

    Returns:
        ``(acceptable, follow-up question, text for the form)``. If the AI
        can't be reached the answer is accepted as typed when it's more than
        a character or two — a provider outage must not trap an employee in
        an endless loop — and the requester still reviews the form.
    """
    message = (
        f"Field: {field.label}\n"
        f"A valid answer is: {field.rule}\n"
        f"Question asked: {field.question}\n"
        f"Employee's answer: {answer}"
    )
    try:
        async with OpenRouterClient(
            agent=AGENT,
            run_id=run_id,
            provider_override=settings.ops_manager_bot_provider,
            model_override=settings.ops_manager_bot_model,
            fallback_override=settings.ops_manager_bot_fallback_models,
        ) as ai:
            verdict = await ai.complete_json(VALIDATE_SYSTEM, message)
    except OpenRouterError as exc:
        log.warning("Answer check unavailable, accepting '{}' for {}: {}", answer[:60], field.key, exc)
        return len(answer.strip()) >= 2, None, answer.strip()

    if verdict.get("ok") is True:
        value = " ".join(str(verdict.get("value") or "").split())
        return True, None, value or answer.strip()
    follow_up = str(verdict.get("follow_up") or "").strip()
    return False, follow_up or None, ""


async def _ask_next(request: dict[str, Any], run_id: uuid.UUID) -> str:
    """Ask the next missing question, or show the finished form for sending."""
    field = permissions.next_missing_field(request)
    if field is not None:
        await store.set_permission_pending_field(str(request["id"]), field.key)
        await _send(request["requester_telegram_user_id"], f"📝 {escape(field.question)}", run_id)
        return "permission_question"

    await store.set_permission_pending_field(str(request["id"]), None)
    card = permissions.request_card(request, for_approver=False)
    await _send(
        request["requester_telegram_user_id"],
        f"{card}\n\n<i>Жавоблар расмий шакл учун кирилл ёзувига ўтказилди ва имлоси тузатилди. "
        "Текширинг — тўғри бўлса, юборинг:</i>",
        run_id,
        permissions.confirm_keyboard(str(request["id"])),
    )
    return "permission_ready"


async def _store_answer(draft: dict[str, Any], field: permissions.Field, answer: str) -> dict[str, Any]:
    """Save an accepted answer (already in its form wording)."""
    if field.key == "amount":
        amount, currency = permissions.parse_amount(answer)
        return await store.set_permission_amount(str(draft["id"]), amount, currency, answer) or draft
    return await store.set_permission_field(str(draft["id"]), field.key, answer) or draft


async def handle_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """Handle one message if it belongs to the permission flow.

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
        return await _approver_reply(awaiting, text, employee, run_id)

    draft = await store.get_permission_draft(telegram_user_id)

    # 2. Cancelling the request being filled in.
    if draft is not None and permissions.is_cancel(text):
        await store.cancel_permission_request(str(draft["id"]), telegram_user_id)
        await store.add_permission_event(
            request_id=str(draft["id"]),
            actor=employee["display_name"],
            actor_telegram_user_id=telegram_user_id,
            action="cancelled",
        )
        await _send(telegram_user_id, "❌ Сўров бекор қилинди.", run_id)
        return "permission_cancelled"

    # 3. The answer to the question just asked — checked before it is kept.
    if draft is not None and draft.get("pending_field"):
        field = permissions.FIELD_BY_KEY.get(draft["pending_field"])
        if field is not None:
            acceptable, follow_up, value = await _check_answer(field, text, run_id)
            if not acceptable:
                question = follow_up or f"Жавоб тушунарсиз. {field.question}"
                await _send(telegram_user_id, f"🔁 {escape(question)}{CANCEL_HINT}", run_id)
                return "permission_reasked"
            draft = await _store_answer(draft, field, value)
            return await _ask_next(draft, run_id)

    # 4. A new request.
    if not permissions.wants_permission(text):
        return None

    if draft is not None:
        await _send(
            telegram_user_id,
            f"Сизда тугалланмаган сўров бор — аввал шуни тугатинг.{CANCEL_HINT}",
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
        "Бир неча савол бераман. Жавобларингиз расмий шаклга айнан ёзилади.\n"
        f"Бекор қилиш: /bekor",
        run_id,
    )
    return await _ask_next(created, run_id)


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
            )
            await _send(clicker_id, "❌ Сўров бекор қилинди.", run_id)
        return "permission_cancelled"

    if prefix == "permsend":
        return await _submit(rest, clicker_id, run_id)

    if prefix == "permdec":
        code, _, request_id = rest.partition(":")
        decision = permissions.DECISION_CODES.get(code)
        if decision is None:
            return "permission_unknown_decision"
        return await _take_decision(request_id, decision, clicker, run_id)

    return None


async def _submit(request_id: str, clicker_id: int, run_id: uuid.UUID) -> str:
    """Send a finished draft to the approvers."""
    request = await store.get_permission_request(request_id)
    if request is None or request["status"] != "draft":
        return "permission_already_sent"
    if request["requester_telegram_user_id"] != clicker_id:
        return "permission_not_yours"
    if permissions.next_missing_field(request) is not None:
        return await _ask_next(request, run_id)

    approvers = await _route(request)
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

    # The form names the position it goes to, not anyone's Telegram name.
    submitted_to = ", ".join(dict.fromkeys(permissions.role_label_cyr(a["role"]) for a in approvers))
    request = await store.submit_permission_request(request_id, submitted_to) or request
    await store.add_permission_event(
        request_id=request_id,
        actor=request["requester_name"],
        actor_telegram_user_id=clicker_id,
        action="submitted",
        detail=f"Кимга: {submitted_to}",
    )

    card = permissions.request_card(request, for_approver=True)
    keyboard = permissions.decision_keyboard(request_id)
    delivered = 0
    for approver in approvers:
        try:
            await _send(approver["telegram_user_id"], card, run_id, keyboard)
            delivered += 1
        except TelegramError as exc:
            log.error("Could not deliver request {} to {}: {}", request.get("request_no"), approver["telegram_user_id"], exc)

    if delivered == 0:
        # Never tell someone "sent" when no approver actually has it.
        await _send(
            clicker_id,
            f"⚠️ Сўров № {escape(request.get('request_no') or '—')} сақланди, лекин тасдиқловчига "
            "етказиб бўлмади. Админга хабар беринг.",
            run_id,
        )
        return "permission_undelivered"

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
    clicker_id = clicker.get("id")
    request = await store.get_permission_request(request_id)
    if request is None:
        return "permission_not_found"

    if not await _may_decide(request, clicker_id):
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
    has_name = bool(approver and (approver.get("full_name") or "").strip())
    if decision == "approved" and has_name:
        return await _finalize_decision(request, decision, None, approver, run_id)

    started = await store.start_permission_decision(request_id, decision, clicker_id)
    if started is None:
        return "permission_already_decided"

    # The form needs the approver's real name: asked once, then remembered.
    field = permissions.DECISION_NOTE_FIELDS[decision] if has_name else permissions.APPROVER_NAME_FIELD
    await _send(clicker_id, f"✍️ {escape(field.question)}", run_id)
    return "permission_decision_pending"


async def _approver_reply(
    request: dict[str, Any], text: str, approver: dict[str, Any], run_id: uuid.UUID
) -> str:
    """The approver's typed text: their full name first (once), then the note."""
    decision = request["pending_decision"]
    approver_id = approver["telegram_user_id"]
    needs_name = not (approver.get("full_name") or "").strip()
    field = permissions.APPROVER_NAME_FIELD if needs_name else permissions.DECISION_NOTE_FIELDS.get(decision)
    if field is None:  # a plain approval owes no note
        return await _finalize_decision(request, decision, None, approver, run_id)

    acceptable, follow_up, value = await _check_answer(field, text, run_id)
    if not acceptable:
        await _send(approver_id, f"🔁 {escape(follow_up or field.question)}", run_id)
        return "permission_reasked"

    if needs_name:
        await store.set_employee_full_name(approver_id, value)
        approver = {**approver, "full_name": value}
        note_field = permissions.DECISION_NOTE_FIELDS.get(decision)
        if note_field is None:
            return await _finalize_decision(request, decision, None, approver, run_id)
        await _send(approver_id, f"✍️ {escape(note_field.question)}", run_id)
        return "permission_decision_pending"

    return await _finalize_decision(request, decision, value, approver, run_id)


async def _finalize_decision(
    request: dict[str, Any],
    decision: str,
    note: str | None,
    approver: dict[str, Any] | None,
    run_id: uuid.UUID,
) -> str:
    """Store the decision, tell the requester, and send the filled SOP form."""
    # "Тасдиқловчи исми ва лавозими": the name they typed, and their position.
    approver_name = (
        f"{approver.get('full_name') or ''}, {permissions.role_label_cyr(approver['role'])}".strip(", ")
        if approver
        else "Ваколатли шахс"
    )
    approver_id = approver["telegram_user_id"] if approver else None

    if decision == "approved":
        # A plain approval approves exactly what was asked — say so, in the
        # requester's own words, rather than inventing new terms.
        amount = permissions.field_value(request, permissions.FIELD_BY_KEY["amount"])
        approved_terms = f"{amount}, {request.get('execute_by') or '—'} (сўралгандек)"
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

    requester_text = permissions.decision_text(decided)
    if decision in ("approved", "approved_conditional"):
        requester_text += "\n\n<i>Фақат тасдиқланган ҳажм ва шартларда бажаринг.</i>"
    elif decision == "rejected":
        requester_text += "\n\n<i>Рад жавоби билан иш бошланмайди.</i>"
    else:
        requester_text += "\n\n<i>Сўралган маълумотни ёзиб юборинг.</i>"

    await _send(decided["requester_telegram_user_id"], requester_text, run_id)
    if approver_id:
        await _send(approver_id, f"✅ Қарор қайд этилди: № {escape(decided.get('request_no') or '—')}", run_id)

    try:
        path = docx_form.fill_form(decided)
    except Exception as exc:  # noqa: BLE001 — a document failure must not lose the decision
        log.error("Could not fill the permission form for {}: {}", decided.get("request_no"), exc)
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
    """The permission registry, for OPS Manager Bot's own agent answers."""
    rows = await store.permission_registry(days=60)
    drafts = await store.permission_drafts()

    draft_lines = []
    for draft in drafts:
        state = "not finished — still answering questions" if draft.get("pending_field") else (
            "filled in but NOT sent to any approver (if the requester is the Director and no "
            "deputy is configured, nobody else is allowed to decide it — SOP forbids self-approval)"
        )
        draft_lines.append(
            f"- DRAFT | {draft['requester_name']} ({_label(draft['requester_role'])}) | "
            f"what: {draft.get('subject') or '—'} | {state}"
        )

    if not rows and not drafts:
        return (
            "No written permission requests yet. Employees start one by writing "
            "\"ruxsat\"/\"рухсат\" to this bot (EMJ-SOP-ADM-01); nothing is recorded until then."
        )

    pending = [r for r in rows if r["status"] in ("submitted", "info_needed")]
    lines = [f"Written permission requests (EMJ-SOP-ADM-01), last 60 days: {len(rows)} sent."]
    lines.append(f"Awaiting a decision: {len(pending)}.")
    if draft_lines:
        lines.append(f"Started but never sent to an approver: {len(draft_lines)}.")
        lines.extend(draft_lines)
    for row in rows:
        amount = permissions.field_value(row, permissions.FIELD_BY_KEY["amount"])
        submitted = row.get("submitted_at")
        stamp = submitted.strftime("%d.%m.%Y %H:%M") if hasattr(submitted, "strftime") else "—"
        line = (
            f"- {row.get('request_no') or '(no number)'} | {stamp} | "
            f"{row.get('requester_full_name') or row['requester_name']} "
            f"({row.get('requester_position') or _label(row['requester_role'])}) | "
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
