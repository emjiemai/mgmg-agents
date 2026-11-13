"""Message budget (F5, 2026-11-13, docs/plans/2026-11-09-new-specs-plan.md): one counter per person per day.

Every message OPS Manager Bot sends to a person's private chat (text, photo, file) adds one to
``message_budget`` (day in Tashkent, the person's Telegram id). Nothing is counted for groups or channels.

When a person has already had ``MESSAGE_BUDGET_PER_DAY`` messages today, a low-priority message — one from an
agent in ``MESSAGE_BUDGET_DEFER_AGENTS``, with no buttons and not a reply — is not sent now: it is kept as a
``held`` morning note for the next day and goes out inside that person's ONE morning message
(agents/morning-notes). Never held: anything with buttons (a card, a question), a reply to the person's own
message, anything to the Director, the work AI's and analyst's answers (they are replies to what was asked).
``MESSAGE_BUDGET_ENFORCE=false`` only counts. Counts only in logs and IT's /texnik — never the text.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from integrations.common.config import settings
from integrations.common.db import execute, fetch_one
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local

log = setup_logging("message-budget")

SEND_METHODS = frozenset({"sendMessage", "sendPhoto", "sendDocument", "sendVideo", "sendVoice", "sendAudio",
                          "copyMessage", "forwardMessage"})
HELD_CHARS = 400  # a held message is a short reminder; longer is cut with «…»


def _ops_token() -> str:
    return settings.ops_manager_bot_telegram_bot_token.get_secret_value()


def _private(chat_id: Any) -> int | None:
    """A person's chat id (positive), or None for groups/channels/names."""
    try:
        chat = int(str(chat_id))
    except (TypeError, ValueError):
        return None
    return chat if chat > 0 else None


def defer_agents() -> set[str]:
    return {a.strip() for a in settings.message_budget_defer_agents.split(",") if a.strip()}


async def count(bot_token: str | None, method: str, chat_id: Any) -> None:
    """One more message to this person today (never raises: a counter must not stop a send)."""
    chat = _private(chat_id)
    if chat is None or method not in SEND_METHODS or not bot_token or bot_token != _ops_token():
        return
    try:
        await execute(
            """INSERT INTO message_budget (day, telegram_user_id, sent) VALUES (%s, %s, 1)
               ON CONFLICT (day, telegram_user_id) DO UPDATE SET sent = message_budget.sent + 1""",
            (today_local(), chat))
    except Exception as exc:  # noqa: BLE001
        log.warning("Message counter not saved: {}", type(exc).__name__)


def holdable(agent: str, bot_token: str | None, chat_id: Any, *, has_buttons: bool, is_reply: bool) -> bool:
    """Could this send be held at all (before looking at the counter)."""
    return (settings.message_budget_enforce and not has_buttons and not is_reply
            and bool(bot_token) and bot_token == _ops_token()
            and _private(chat_id) is not None and agent in defer_agents())


async def hold_if_over(agent: str, chat_id: Any, text: str) -> bool:
    """True = over today's budget: the text is kept for tomorrow's morning message instead of being sent."""
    chat = _private(chat_id)
    try:
        row = await fetch_one(
            """SELECT e.id, e.telegram_user_id, e.role, coalesce(b.sent, 0) AS sent
               FROM employees e LEFT JOIN message_budget b ON b.telegram_user_id = e.telegram_user_id AND b.day = %s
               WHERE e.telegram_user_id = %s AND e.status = 'active' LIMIT 1""",
            (today_local(), chat))
    except Exception as exc:  # noqa: BLE001 — unsure = send it
        log.warning("Message budget not read: {}", type(exc).__name__)
        return False
    from integrations.org_bot.roles import DIRECTOR_ROLE

    if not row or row["role"] == DIRECTOR_ROLE or int(row["sent"]) < settings.message_budget_per_day:
        return False
    short = " ".join(re.sub(r"<[^>]+>", "", str(text)).split())  # already escaped HTML; tags dropped
    short = short if len(short) <= HELD_CHARS else short[:HELD_CHARS - 1].rstrip() + "…"
    from integrations.org_bot import store

    try:
        await store.add_morning_note({"id": row["id"], "telegram_user_id": row["telegram_user_id"]},
                                     today_local() + timedelta(days=1), "held", {"agent": agent, "text": short})
        await execute(
            """INSERT INTO message_budget (day, telegram_user_id, held) VALUES (%s, %s, 1)
               ON CONFLICT (day, telegram_user_id) DO UPDATE SET held = message_budget.held + 1""",
            (today_local(), chat))
    except Exception as exc:  # noqa: BLE001 — couldn't keep it: send it now rather than lose it
        log.warning("Held message not saved, sent instead: {}", type(exc).__name__)
        return False
    log.info("Message held for the morning (agent {}, over budget)", agent)
    return True
