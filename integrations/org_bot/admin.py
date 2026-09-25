"""Admin Bot — deterministic employee-access approval, no AI involved.

Onboarding takes two admin decisions, both on this bot:

1. **Access** — someone new messages OPS Manager Bot; the admin gets an
   Accept/Reject card for the person.
2. **Role** — once accepted, they pick a role in OPS Manager Bot; the admin
   gets a second Accept/Reject card for that specific role. Only this second
   Accept registers them. Without it, anyone past step 1 could pick
   Operatsion Direktor and receive every report and give the bot orders.

Admin Bot is admin-only: it is never messaged by employees directly (see the
module docstring in ``ops_manager.py`` for why — Telegram cannot be
cold-messaged, so everything employee-facing, including the role picker, goes
through OPS Manager Bot, the chat the requester already started).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import store
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS
from integrations.telegram.bot import TelegramBot, escape

AGENT = "admin-bot"
log = setup_logging(AGENT)

EMPLOYEE_LIST_COMMANDS = ("/employees", "/users", "/list", "/xodimlar")
# Sends the name question to every employee who hasn't given one (names.py).
ASK_NAMES_COMMANDS = ("/ismlar", "/names")
# Runs the weekly data-quality report (B4) now.
DATA_QUALITY_COMMANDS = ("/sifat", "/quality")


def _person_line(request: dict[str, Any]) -> str:
    """Clickable name plus @username, for an admin card."""
    name = request.get("display_name") or str(request["telegram_user_id"])
    username = request.get("telegram_username")
    username_line = f" (@{escape(username)})" if username else ""
    return f'<a href="tg://user?id={request["telegram_user_id"]}">{escape(name)}</a>{username_line}'


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

    text = f"🆕 <b>Кириш сўрови</b>\n\n{_person_line(row)} OPS Manager Bot'га қўшилмоқчи."
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Қабул қилиш", "callback_data": f"access_approve:{row['id']}"},
                {"text": "❌ Рад этиш", "callback_data": f"access_reject:{row['id']}"},
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


def role_decision_keyboard(request_id: str) -> dict[str, Any]:
    """Accept/Reject buttons for a role request card.

    Args:
        request_id: ``access_requests.id``.

    Returns:
        A Telegram ``reply_markup`` dict.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Қабул қилиш", "callback_data": f"role_approve:{request_id}"},
                {"text": "❌ Рад этиш", "callback_data": f"role_reject:{request_id}"},
            ]
        ]
    }


async def request_role_approval(access_request: dict[str, Any], run_id: uuid.UUID) -> None:
    """Send the admin a card asking to confirm the role someone just picked.

    Args:
        access_request: The ``access_requests`` row, with ``requested_role`` set.
        run_id: UUID grouping this webhook call's audit rows.
    """
    role_slug = access_request["requested_role"]
    role = ROLE_LABELS.get(role_slug, role_slug)
    text = f"🧩 <b>Роль сўрови</b>\n\n{_person_line(access_request)} сўраган роль: <b>{escape(role)}</b>"
    if role_slug == DIRECTOR_ROLE:
        text += (
            "\n\n⚠️ <b>Директор роли</b> — ходимларга топшириқ беради, ҳисобот юбормаганлар рўйхатини, "
            "ходимларнинг хабарларини ва ёзма рухсат сўровларини олади."
        )

    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        message_ids = await bot.send_message(text, reply_markup=role_decision_keyboard(str(access_request["id"])))

    if message_ids:
        await store.set_role_admin_message_id(str(access_request["id"]), message_ids[0])
    log.info("Role request {} ({}) sent to the admin", str(access_request["id"])[:8], role_slug)


async def handle_admin_message(message: dict[str, Any], run_id: uuid.UUID) -> str:
    """Handle a text command sent to Admin Bot by the admin.

    Currently just the employee list/removal tool ("i need this because i am
    testing now, i will give it to them later" — the business owner's own
    words) — gated by ``ADMIN_BOT_ADMIN_USER_ID`` the same way callback
    decisions already are, when it's set.

    Args:
        message: The ``message`` object from a Telegram update.
        run_id: UUID grouping this webhook call's audit rows.

    Returns:
        A short outcome string.
    """
    sender = message.get("from", {})
    admin_id = settings.admin_bot_admin_user_id
    if admin_id and sender.get("id") != admin_id:
        log.warning("Admin command attempted by non-admin telegram_user_id={}", sender.get("id"))
        return "unauthorized"

    text = (message.get("text") or "").strip().lower()
    if text in EMPLOYEE_LIST_COMMANDS:
        return await _list_employees(run_id)
    if text in ASK_NAMES_COMMANDS:
        return await _ask_names(run_id)
    if text in DATA_QUALITY_COMMANDS:
        from integrations.common.agent_loader import load_agent  # the agent lives in a hyphenated folder

        return await load_agent("data-quality").check_now(run_id)

    return "ignored"


async def _ask_names(run_id: uuid.UUID) -> str:
    """Ask every employee without a name for it now, and tell the admin how many."""
    from integrations.org_bot import names  # local import: names -> store is fine, keeps admin light

    asked = await names.ask_missing_names(run_id)
    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        if asked:
            await bot.send_message(f"👤 {asked} та ходимдан исм ва фамилияси сўралди.")
        else:
            await bot.send_message("👤 Сўрайдиган ходим қолмади — ҳамма исмини ёзган ёки аллақачон сўралган.")
    return "names_asked"


async def _list_employees(run_id: uuid.UUID) -> str:
    """Send the admin every active employee, one Remove button each."""
    employees = await store.list_active_employees()
    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        if not employees:
            await bot.send_message("Рўйхатдан ўтган ходимлар йўқ.")
            return "empty"

        lines = ["<b>Рўйхатдан ўтган ходимлар</b>\n"]
        buttons = []
        for emp in employees:
            label = ROLE_LABELS.get(emp["role"], emp["role"])
            username = f" (@{escape(emp['telegram_username'])})" if emp.get("telegram_username") else ""
            full_name = (emp.get("full_name") or "").strip()
            name = escape(full_name) if full_name else f"{escape(emp['display_name'])} <i>(исм ёзилмаган)</i>"
            lines.append(f"• {name}{username} — {label}")
            buttons.append(
                [{"text": f"🗑 {full_name or emp['display_name']} ({label})", "callback_data": f"removeuser:{emp['id']}"}]
            )
        lines.append("\n<i>Исм ёзмаганлардан сўраш: /ismlar · Маълумот сифати: /sifat</i>")
        await bot.send_message("\n".join(lines), reply_markup={"inline_keyboard": buttons})
    return "listed"


async def handle_admin_callback(callback: dict[str, Any], run_id: uuid.UUID) -> str:
    """Resolve a button press on an Admin Bot card.

    Args:
        callback: The ``callback_query`` object from a Telegram update.
        run_id: UUID grouping this webhook call's audit rows.

    Returns:
        A short outcome string: 'approved', 'rejected', 'role_approved',
        'role_rejected', 'removed', an 'already_*' variant, 'not_found',
        'unauthorized', or 'unrecognized'.
    """
    data = callback.get("data", "")
    query_id = callback.get("id", "")
    clicker = callback.get("from", {})
    decided_by = clicker.get("username") or str(clicker.get("id", "unknown"))

    if ":" not in data:
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"

    action, target_id = data.split(":", 1)

    admin_id = settings.admin_bot_admin_user_id
    if admin_id and clicker.get("id") != admin_id:
        log.warning("Admin action attempted by non-admin telegram_user_id={}", clicker.get("id"))
        await _answer(query_id, "Рухсат йўқ")
        return "unauthorized"

    if action == "removeuser":
        return await _handle_remove_user(target_id, query_id, decided_by, callback, run_id)

    if action in ("role_approve", "role_reject"):
        return await _handle_role_decision(target_id, action == "role_approve", query_id, decided_by, run_id)

    if action not in ("access_approve", "access_reject"):
        await _answer(query_id, "Номаълум амал")
        return "unrecognized"

    request_id = target_id
    request = await store.get_access_request(request_id)
    if request is None:
        await _answer(query_id, "Сўров топилмади")
        return "not_found"

    if request["status"] != "pending":
        await _answer(query_id, f"Аллақачон ҳал қилинган ({request['status']})")
        return f"already_{request['status']}"

    decision: Literal["approved", "rejected"] = "approved" if action == "access_approve" else "rejected"
    changed = await store.decide_access_request(request_id, decision, decided_by)
    if not changed:
        # Lost a race to a concurrent tap -- report the outcome, don't error.
        current = await store.get_access_request(request_id)
        status = current["status"] if current else decision
        await _answer(query_id, f"Аллақачон ҳал қилинган ({status})")
        return f"already_{status}"

    marker = "✅ Қабул қилинди" if decision == "approved" else "❌ Рад этилди"
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
                text=f"{marker} — {escape(name)}\n\n<i>@{escape(decided_by)}</i>",
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


async def _handle_role_decision(
    request_id: str, approve: bool, query_id: str, decided_by: str, run_id: uuid.UUID
) -> str:
    """Resolve the admin's Accept/Reject on a role request card.

    Accept registers the employee with that role. Reject sends them the role
    picker again — the admin turned down the role, not the person, who was
    already accepted at the first step.
    """
    decision: Literal["approved", "rejected"] = "approved" if approve else "rejected"
    request = await store.decide_role_request(request_id, decision, decided_by)
    if request is None:
        current = await store.get_access_request(request_id)
        if current is None:
            await _answer(query_id, "Сўров топилмади")
            return "not_found"
        status = current.get("role_status") or "сўралмаган"
        await _answer(query_id, f"Аллақачон ҳал қилинган ({status})")
        return f"already_{status}"

    role = ROLE_LABELS.get(request["requested_role"], request["requested_role"])
    name = request.get("display_name") or str(request["telegram_user_id"])
    marker = "✅ Роль тасдиқланди" if approve else "❌ Роль рад этилди"
    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        if request.get("role_admin_message_id"):
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=settings.admin_bot_telegram_chat_id,
                message_id=request["role_admin_message_id"],
                text=f"{marker} — {escape(name)}: <b>{escape(role)}</b>\n\n<i>@{escape(decided_by)}</i>",
            )
        await bot._answer_callback(query_id, marker)  # noqa: SLF001

    from integrations.org_bot import ops_manager  # local import breaks the admin<->ops_manager cycle

    if approve:
        employee = await store.create_employee(
            telegram_user_id=request["telegram_user_id"],
            telegram_username=request.get("telegram_username"),
            display_name=name,
            role=request["requested_role"],
            approved_by=decided_by,
        )
        await ops_manager.send_registration_confirmed(request, run_id)
        target_ref, action = str(employee["id"]), "employee_registered"
    else:
        await ops_manager.send_role_picker(request, run_id, retry=True)
        target_ref, action = str(request_id), "role_rejected"

    await log_action(
        agent=AGENT,
        action=action,
        target_system="telegram",
        status="success",
        run_id=run_id,
        target_ref=target_ref,
        mode="write",
        payload={
            "role": request["requested_role"],
            "decided_by": decided_by,
            "telegram_user_id": request["telegram_user_id"],
        },
    )
    log.info("Role request {} ({}) {} by {}", str(request_id)[:8], request["requested_role"], decision, decided_by)
    return f"role_{decision}"


async def _handle_remove_user(
    employee_id: str, query_id: str, removed_by: str, callback: dict[str, Any], run_id: uuid.UUID
) -> str:
    """Resolve a 🗑 Remove button press from `/employees`.

    Sets the employee's status to 'revoked' — they'd need to message OPS
    Manager Bot and go through the join flow again to regain access, the
    same as anyone else. Idempotent: a double-tap reports "already removed".
    """
    employee = await store.revoke_employee(employee_id, removed_by)
    if employee is None:
        await _answer(query_id, "Аллақачон ўчирилган")
        return "already_removed"

    message = callback.get("message") or {}
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.admin_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        if message.get("message_id") and message.get("chat", {}).get("id"):
            await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                chat_id=str(message["chat"]["id"]),
                message_id=message["message_id"],
                text=f"🗑 Ўчирилди — {escape((employee.get('full_name') or '').strip() or employee['display_name'])}"
                f"\n\n<i>@{escape(removed_by)}</i>",
            )
        await bot._answer_callback(query_id, "Ўчирилди")  # noqa: SLF001

    await log_action(
        agent=AGENT,
        action="employee_removed",
        target_system="telegram",
        status="success",
        run_id=run_id,
        target_ref=str(employee_id),
        mode="write",
        payload={"removed_by": removed_by, "telegram_user_id": employee["telegram_user_id"]},
    )
    log.info("Employee {} removed by {}", str(employee_id)[:8], removed_by)
    return "removed"


async def _answer(query_id: str, text: str) -> None:
    """Acknowledge a button press on Admin Bot's own token."""
    if not query_id:
        return
    async with TelegramBot(
        agent=AGENT, bot_token=settings.admin_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._answer_callback(query_id, text)  # noqa: SLF001 — same-package reuse of a generic ack helper
