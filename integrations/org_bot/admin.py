"""Admin Bot — deterministic employee-access approval, no AI involved.

Admin Bot is admin-only: it is never messaged by employees directly (see the
module docstring in ``ops_manager.py`` for why — Telegram cannot be
cold-messaged, so the role-picker step after approval is sent via OPS Manager
Bot, the chat the requester already started, not this one).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import store
from integrations.telegram.bot import TelegramBot, escape

AGENT = "admin-bot"
log = setup_logging(AGENT)


async def request_access(
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str,
    run_id: uuid.UUID,
) -> Literal["created", "already_pending"]:
    """Create a join request (if none is already pending) and notify the admin.

    Args:
        telegram_user_id: The requester's Telegram numeric id.
        telegram_username: Their @username, if set.
        display_name: Full name for the admin's card.
        run_id: UUID grouping this webhook call's audit rows.

    Returns:
        'created' if a new request was made and the admin notified,
        'already_pending' if the user already has one outstanding — callers
        should not notify the admin again in that case.
    """
    row = await store.create_access_request(
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        display_name=display_name,
    )
    if row is None:
        return "already_pending"

    profile_link = f"tg://user?id={telegram_user_id}"
    username_line = f" (@{escape(telegram_username)})" if telegram_username else ""
    text = (
        "🆕 <b>Access request</b>\n\n"
        f'<a href="{profile_link}">{escape(display_name)}</a>{username_line} '
        "wants to join OPS Manager Bot."
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Accept", "callback_data": f"access_approve:{row['id']}"},
                {"text": "❌ Reject", "callback_data": f"access_reject:{row['id']}"},
            ]
        ]
    }

    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        message_ids = await bot.send_message(text, reply_markup=keyboard)

    if message_ids:
        await store.set_access_request_message_id(str(row["id"]), message_ids[0])

    log.info("Access request {} created for telegram_user_id={}", str(row["id"])[:8], telegram_user_id)
    return "created"


async def handle_admin_callback(callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Resolve an Accept/Reject button press on a join-request card.

    Args:
        callback: The ``callback_query`` object from a Telegram update.
        run_id: UUID grouping this webhook call's audit rows.

    Returns:
        A short outcome string: 'approved', 'rejected', 'already_approved',
        'already_rejected', 'not_found', 'unauthorized', or 'unrecognized'.
    """
    data = callback.get("data", "")
    query_id = callback.get("id", "")
    clicker = callback.get("from", {})
    decided_by = clicker.get("username") or str(clicker.get("id", "unknown"))

    if ":" not in data:
        await _answer(query_id, "Unrecognized action")
        return "unrecognized"

    action, request_id = data.split(":", 1)
    if action not in ("access_approve", "access_reject"):
        await _answer(query_id, "Unrecognized action")
        return "unrecognized"

    admin_id = settings.admin_bot_admin_user_id
    if admin_id and clicker.get("id") != admin_id:
        log.warning("Access decision attempted by non-admin telegram_user_id={}", clicker.get("id"))
        await _answer(query_id, "Not authorized")
        return "unauthorized"

    request = await store.get_access_request(request_id)
    if request is None:
        await _answer(query_id, "Request not found")
        return "not_found"

    if request["status"] != "pending":
        await _answer(query_id, f"Already {request['status']}")
        return f"already_{request['status']}"

    decision: Literal["approved", "rejected"] = "approved" if action == "access_approve" else "rejected"
    changed = await store.decide_access_request(request_id, decision, decided_by)
    if not changed:
        # Lost a race to a concurrent tap -- report the outcome, don't error.
        current = await store.get_access_request(request_id)
        status = current["status"] if current else decision
        await _answer(query_id, f"Already {status}")
        return f"already_{status}"

    marker = "✅ Accepted" if decision == "approved" else "❌ Rejected"
    name = request.get("display_name") or str(request["telegram_user_id"])
    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        if request.get("admin_message_id"):
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=settings.admin_bot_telegram_chat_id,
                message_id=request["admin_message_id"],
                text=f"{marker} — {escape(name)}\n\n<i>by @{escape(decided_by)}</i>",
            )
        await bot._answer_callback(query_id, marker)  # noqa: SLF001 — reuse the already-open bot, not a new one

    await log_action(
        agent=AGENT,
        action="access_decision",
        target_system="telegram",
        status="success",
        run_id=run_id,
        target_ref=str(request_id),
        mode="write",
        payload={"decision": decision, "decided_by": decided_by, "telegram_user_id": request["telegram_user_id"]},
    )

    if decision == "approved":
        from integrations.org_bot import ops_manager  # local import breaks the admin<->ops_manager cycle

        await ops_manager.send_role_picker(request, run_id)

    log.info("Access request {} {} by {}", str(request_id)[:8], decision, decided_by)
    return decision


async def _answer(query_id: str, text: str) -> None:
    """Acknowledge a button press on Admin Bot's own token."""
    if not query_id:
        return
    async with TelegramBot(
        agent=AGENT, bot_token=settings.admin_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._answer_callback(query_id, text)  # noqa: SLF001 — same-package reuse of a generic ack helper
