"""The ☰ command menus of both bots (2026-10-16, the owner: "a hamburger button for the / commands").

Telegram shows a bot's commands behind the menu button next to the message
box once the bot has them (``setMyCommands``); the button is set to show them
(``setChatMenuButton``). Who sees which:

  * Admin Bot — the admin only (chat scope; the bot answers nobody else).
  * OPS Manager Bot — everyone gets the employee menu; each active Director
    gets the Director's menu in their own chat. A role change re-sets that
    person's menu (``sync_person``); everything is set again at startup.

Only commands the bots really handle are listed (``admin.py``, ``kpi_flow.py``,
``report_tools.py``, ``permissions.py``, ``ops_manager.py``) — selfcheck
compares them. Descriptions are Uzbek Cyrillic. Best-effort: a failure is
logged and never breaks a role change.
"""

from __future__ import annotations

from typing import Any

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.telegram.bot import TelegramBot, TelegramError

AGENT = "bot-menus"
log = setup_logging(AGENT)

ADMIN: list[tuple[str, str]] = [
    ("xodimlar", "Ходимлар: роль, исм, созламалар, маълумотга кириш"),
    ("texnik", "Техник ҳисобот (суммаларсиз)"),
    ("baza", "Бот базаси текшируви: эскирган, синов, такрорий ёзувлар"),
    ("grafik", "Эркин график — Verifix'да кечикиш ҳисобланмайдиганлар"),
    ("dam", "Дам олиш кунлари"),
    ("elon", "Ҳамма ходимларга эълон"),
    ("ismlar", "Исмини айтмаганлардан исмини сўраш"),
    ("sifat", "SAP маълумоти сифати"),
    ("qr", "Шикоят QR карталари («/qr sharh» — Google шарҳ)"),
    ("billz", "Billz уланиши"),
    ("1c", "1C уланиши"),
    ("verifix", "Verifix уланиши"),
]
EMPLOYEE: list[tuple[str, str]] = [
    ("hisobot", "Бугунги ҳисобот: кўриш, ўзгартириш, ўчириш"),
    ("kpi", "Менинг KPI'им"),
    ("natija", "Мақсад натижасини киритиш"),
    ("ruxsat", "Ёзма рухсат сўраш"),
    ("ism", "Исмни ўзгартиришни сўраш"),
    ("bekor", "Бошланган ишни бекор қилиш"),
]
DIRECTOR: list[tuple[str, str]] = [
    ("kpi", "Ходимлар KPI жадвали"),
    ("maqsad", "Ходимга ойлик мақсад қўйиш"),
    ("maqsadlar", "Мақсадлар ва натижалар"),
    ("baho", "Ойлик баҳолаш карталари"),
    ("hisobotlar", "Ҳисоботлар календари: ким юборди, ким юбормади"),
    ("bekor", "Бошланган ишни бекор қилиш"),
]


# The Garmin manager (2026-10-17): the employees' menu plus his team's report calendar.
GARMIN_MANAGER: list[tuple[str, str]] = EMPLOYEE + [("hisobotlar", "Garmin сотув жамоаси ҳисоботлари календари")]


def role_menu(role: str | None) -> list[tuple[str, str]] | None:
    """A role's own menu in OPS Manager Bot, or None for the employees' default."""
    from integrations.org_bot.roles import DIRECTOR_ROLE

    return {DIRECTOR_ROLE: DIRECTOR, "garmin_manager": GARMIN_MANAGER}.get(role or "")


def _payload(commands: list[tuple[str, str]], chat_id: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"commands": [{"command": c, "description": d} for c, d in commands]}
    if chat_id:
        body["scope"] = {"type": "chat", "chat_id": chat_id}
    return body


async def _set(token: str, commands: list[tuple[str, str]], chat_id: int | None = None) -> bool:
    if not token:
        return False
    try:
        async with TelegramBot(agent=AGENT, bot_token=token) as bot:
            await bot._call("setMyCommands", _payload(commands, chat_id), mode="write")  # noqa: SLF001
            menu = {"menu_button": {"type": "commands"}, **({"chat_id": chat_id} if chat_id else {})}
            await bot._call("setChatMenuButton", menu, mode="write")  # noqa: SLF001
        return True
    except TelegramError as exc:
        log.warning("Could not set a command menu: {}", type(exc).__name__)
        return False


async def sync_person(telegram_user_id: int, role: str | None) -> bool:
    """One OPS Manager chat's menu after a role change: the role's own (Director, Garmin manager), else the employees'."""
    token = settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    own = role_menu(role)
    if own is not None:
        return await _set(token, own, telegram_user_id)
    if not token:
        return False
    try:
        async with TelegramBot(agent=AGENT, bot_token=token) as bot:
            await bot._call("deleteMyCommands", {"scope": {"type": "chat", "chat_id": telegram_user_id}},  # noqa: SLF001
                            mode="write")
        return True
    except TelegramError as exc:
        log.warning("Could not reset a command menu: {}", type(exc).__name__)
        return False


async def sync_all() -> int:
    """Set every menu: Admin Bot, OPS Manager's employees, each active Director. How many were set."""
    from integrations.org_bot import store
    from integrations.org_bot.roles import DIRECTOR_ROLE

    done = 0
    admin_id = settings.admin_bot_admin_user_id
    done += await _set(settings.admin_bot_telegram_bot_token.get_secret_value(), ADMIN, admin_id or None)
    ops = settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    done += await _set(ops, EMPLOYEE)
    for role in (DIRECTOR_ROLE, "garmin_manager"):
        for person in await store.active_employees_by_role(role):
            done += await _set(ops, role_menu(role), person["telegram_user_id"])
    log.info("Command menus set: {}", done)
    return done
