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
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS, ROLE_SLUGS, ROLES
from integrations.telegram.bot import TelegramBot, TelegramError, escape

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


def employee_list_view(employees: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """The /xodimlar list: every active employee, one button each to open their card."""
    if not employees:
        return "Рўйхатдан ўтган ходимлар йўқ.", {"inline_keyboard": []}
    lines = ["<b>Рўйхатдан ўтган ходимлар</b>\n"]
    buttons = []
    for emp in employees:
        label = ROLE_LABELS.get(emp["role"], emp["role"])
        username = f" (@{escape(emp['telegram_username'])})" if emp.get("telegram_username") else ""
        full_name = (emp.get("full_name") or "").strip()
        name = escape(full_name) if full_name else f"{escape(emp['display_name'])} <i>(исм ёзилмаган)</i>"
        lines.append(f"• {name}{username} — {label}")
        buttons.append([{"text": f"👤 {full_name or emp['display_name']} ({label})", "callback_data": f"emp:{emp['id']}"}])
    lines.append(
        "\n<i>Ходимни танланг: исм, роль ёки ўчириш. "
        "Исм ёзмаганлардан сўраш: /ismlar · Маълумот сифати: /sifat</i>"
    )
    return "\n".join(lines), {"inline_keyboard": buttons}


def employee_card(emp: dict[str, Any], note: str = "") -> tuple[str, dict[str, Any]]:
    """One employee's card: who they are, and what the admin can change."""
    full_name = (emp.get("full_name") or "").strip()
    role = ROLE_LABELS.get(emp["role"], emp["role"])
    username = f" (@{escape(emp['telegram_username'])})" if emp.get("telegram_username") else ""
    lines = [
        f"👤 <b>{escape(full_name)}</b>" if full_name else "👤 <i>исм ёзилмаган</i>",
        f"Роль: {escape(role)}",
        f"Телеграм: {escape(emp['display_name'])}{username}",
    ]
    if emp["role"] == DIRECTOR_ROLE:
        lines.append("<i>Директорга савол юборилмайди: исми кейинги ёзма рухсат қарорида сўралади.</i>")
    if note:
        lines += ["", note]
    employee_id = emp["id"]
    keyboard = {
        "inline_keyboard": [
            [{"text": "✏️ Исмни қайта сўраш", "callback_data": f"rename:{employee_id}"}],
            [{"text": "🔁 Ролни ўзгартириш", "callback_data": f"rerole:{employee_id}"}],
            [{"text": "🗑 Ўчириш", "callback_data": f"rmask:{employee_id}"}],
            [{"text": "← Рўйхат", "callback_data": "emplist:all"}],
        ]
    }
    return "\n".join(lines), keyboard


def role_picker_view(emp: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pick someone's new role (their current one isn't offered)."""
    name = (emp.get("full_name") or "").strip() or emp["display_name"]
    text = (
        f"🔁 <b>{escape(name)}</b> учун янги роль:\n"
        "<i>Операцион директор роли — топшириқ беради, ҳисобот юбормаганлар ва ёзма рухсатларни олади.</i>"
    )
    rows = [
        [{"text": role.label, "callback_data": f"cr:{role.slug}:{emp['id']}"}]
        for role in ROLES
        if role.slug != emp["role"]
    ]
    rows.append([{"text": "← Орқага", "callback_data": f"emp:{emp['id']}"}])
    return text, {"inline_keyboard": rows}


def confirm_remove_view(emp: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One more tap before someone is removed."""
    name = (emp.get("full_name") or "").strip() or emp["display_name"]
    text = f"🗑 <b>{escape(name)}</b> ўчирилсинми?\n<i>Кейин ботдан фойдаланиш учун қайта рўйхатдан ўтиши керак бўлади.</i>"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🗑 Ҳа, ўчириш", "callback_data": f"removeuser:{emp['id']}"},
                {"text": "← Бекор", "callback_data": f"emp:{emp['id']}"},
            ]
        ]
    }
    return text, keyboard


def name_request_view(emp: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The card the admin gets when an employee asks (/ism) to change their name."""
    name = (emp.get("full_name") or "").strip() or emp["display_name"]
    role = ROLE_LABELS.get(emp["role"], emp["role"])
    text = f"✏️ <b>Исм ўзгартириш сўрови</b>\n\n{escape(name)} ({escape(role)}) исмини ўзгартирмоқчи."
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Рухсат бериш", "callback_data": f"nameok:{emp['id']}"},
                {"text": "❌ Рад этиш", "callback_data": f"nameno:{emp['id']}"},
            ]
        ]
    }
    return text, keyboard


async def _list_employees(run_id: uuid.UUID) -> str:
    """Send the admin every active employee, each opening their own card."""
    text, keyboard = employee_list_view(await store.list_active_employees())
    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        await bot.send_message(text, reply_markup=keyboard)
    return "listed"


async def request_name_change(employee: dict[str, Any], run_id: uuid.UUID) -> None:
    """Send the admin an employee's own request (/ism) to change their name."""
    text, keyboard = name_request_view(employee)
    async with TelegramBot(
        agent=AGENT,
        run_id=run_id,
        bot_token=settings.admin_bot_telegram_bot_token.get_secret_value(),
        default_chat_id=settings.admin_bot_telegram_chat_id,
    ) as bot:
        await bot.send_message(text, reply_markup=keyboard)


async def _edit(callback: dict[str, Any], text: str, keyboard: dict[str, Any], run_id: uuid.UUID) -> None:
    """Replace the tapped Admin Bot message with a new view."""
    message = callback.get("message") or {}
    if not (message.get("message_id") and message.get("chat", {}).get("id")):
        return
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.admin_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
            chat_id=str(message["chat"]["id"]), message_id=message["message_id"], text=text, reply_markup=keyboard
        )


async def _tell_employee(telegram_user_id: int, text: str, run_id: uuid.UUID) -> None:
    """Message an employee on OPS Manager Bot — the chat they already use."""
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        try:
            await bot.send_message(text, chat_id=str(telegram_user_id))
        except TelegramError as exc:
            log.warning("Could not message employee {}: {}", telegram_user_id, exc)


EMPLOYEE_ACTIONS = ("emp", "emplist", "rename", "rerole", "cr", "rmask", "nameok", "nameno")


async def _handle_employee_action(
    action: str, target: str, query_id: str, decided_by: str, callback: dict[str, Any], run_id: uuid.UUID
) -> str:
    """The /xodimlar card buttons and the answer to an employee's /ism request.

    A new person on an account, a corrected name, or a move to another
    department: the admin re-asks the name or changes the role here, and each
    change is logged in ``employee_changes``.
    """
    if action == "emplist":
        text, keyboard = employee_list_view(await store.list_active_employees())
        await _edit(callback, text, keyboard, run_id)
        await _answer(query_id, "OK")
        return "employee_list"

    role_slug = None
    employee_id = target
    if action == "cr":
        role_slug, _, employee_id = target.partition(":")
        if role_slug not in ROLE_SLUGS:
            await _answer(query_id, "Номаълум роль")
            return "unrecognized"

    employee = await store.get_employee(employee_id)
    if employee is None or employee["status"] != "active":
        await _answer(query_id, "Ходим топилмади")
        return "not_found"

    if action == "emp":
        text, keyboard = employee_card(employee)
        await _edit(callback, text, keyboard, run_id)
    elif action == "rerole":
        text, keyboard = role_picker_view(employee)
        await _edit(callback, text, keyboard, run_id)
    elif action == "rmask":
        text, keyboard = confirm_remove_view(employee)
        await _edit(callback, text, keyboard, run_id)
    elif action in ("rename", "nameok"):
        updated = await store.reset_employee_name(employee_id, decided_by)
        if updated is None:
            await _answer(query_id, "Ходим топилмади")
            return "not_found"
        from integrations.org_bot import names  # local import keeps admin light

        delivered = await names.ask_to_change(updated, run_id)
        if updated["role"] == DIRECTOR_ROLE:
            note = "✏️ Исм ўчирилди — кейинги ёзма рухсат қарорида сўралади."
        elif delivered:
            note = "✏️ Исм қайта сўралди — ходим жавоб бергунча бошқа хабарлари қабул қилинмайди."
        else:
            note = "⚠️ Исм ўчирилди, лекин саволни етказиб бўлмади — ходимнинг кейинги хабарида сўралади."
        if action == "nameok":
            old = (employee.get("full_name") or "").strip() or employee["display_name"]
            await _edit(callback, f"✅ Рухсат берилди — {escape(old)} исмини янгилайди.\n\n<i>{escape(note)}</i>",
                        {"inline_keyboard": []}, run_id)
        else:
            text, keyboard = employee_card(updated, note)
            await _edit(callback, text, keyboard, run_id)
        await log_action(
            agent=AGENT, action="employee_name_reset", target_system="postgres", status="success", run_id=run_id,
            target_ref=employee_id, mode="write", payload={"by": decided_by, "on_request": action == "nameok"},
        )
    elif action == "nameno":
        name = (employee.get("full_name") or "").strip() or employee["display_name"]
        await _tell_employee(employee["telegram_user_id"], "❌ Админ исм ўзгартириш сўровингизни рад этди.", run_id)
        await _edit(callback, f"❌ Рад этилди — {escape(name)}", {"inline_keyboard": []}, run_id)
    elif action == "cr":
        changed = await store.change_employee_role(employee_id, role_slug, decided_by)
        if changed is None:
            await _answer(query_id, "Роль ўзгармади")
            return "role_unchanged"
        before, after = changed
        old_label = ROLE_LABELS.get(before["role"], before["role"])
        new_label = ROLE_LABELS.get(after["role"], after["role"])
        await _tell_employee(
            after["telegram_user_id"],
            f"🔁 Ролингиз ўзгартирилди: <b>{escape(old_label)}</b> → <b>{escape(new_label)}</b>.",
            run_id,
        )
        text, keyboard = employee_card(after, f"🔁 Роль ўзгартирилди: {escape(old_label)} → {escape(new_label)}")
        await _edit(callback, text, keyboard, run_id)
        await log_action(
            agent=AGENT, action="employee_role_changed", target_system="postgres", status="success", run_id=run_id,
            target_ref=employee_id, mode="write", payload={"by": decided_by, "from": before["role"], "to": after["role"]},
        )

    await _answer(query_id, "OK")
    return f"employee_{action}"


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

    if action in EMPLOYEE_ACTIONS:
        return await _handle_employee_action(action, target_id, query_id, decided_by, callback, run_id)

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
