"""Sends a message to the Director(s) via OPS Manager Bot's own token.

Used by scheduled agents (CEO Daily Brief, Receivables, Lead Agent) that
used to each have their own dedicated Telegram bot. Consolidated so the
Director only ever talks to two bots total — OPS Manager Bot for everything
operational, Admin Bot for access decisions — instead of one per agent.

There is no fixed chat_id here, unlike the old per-agent bots: the
recipient is resolved dynamically from ``employees`` (whoever currently
holds the Director role), the same way OPS Manager Bot already resolves who
to relay an employee's message to.
"""

from __future__ import annotations

import uuid
from typing import Literal

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import store
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.telegram.bot import TelegramBot, TelegramError

log = setup_logging("org-bot-notify")


async def notify_directors(
    text: str,
    *,
    agent: str,
    run_id: uuid.UUID | None = None,
    severity: Literal["critical", "warning", "info"] | None = None,
    title: str | None = None,
) -> list[int]:
    """Send one message to every currently-active Director.

    Args:
        text: HTML message body. Used as the ``send_message`` text when
            ``severity``/``title`` are None, or as ``send_alert``'s body
            when both are set.
        agent: Calling agent's name, recorded on Telegram's own audit rows.
        run_id: UUID grouping this call's audit rows.
        severity: If set together with ``title``, sends via
            ``TelegramBot.send_alert`` instead of a plain message.
        title: Required alongside ``severity``.

    Returns:
        Message ids created, across every active Director (normally one —
        looped for correctness if more than one is ever registered). Empty
        if no Director is currently registered; callers should treat that
        as "nothing to notify," not an error — matches how
        ``ops_manager._handle_employee_message`` already handles this case.
    """
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — {} had nothing sent", agent)
        return []

    directors = await store.active_employees_by_role(DIRECTOR_ROLE)
    if not directors:
        log.warning("No active Director registered — {} had nothing to notify", agent)
        return []

    message_ids: list[int] = []
    async with TelegramBot(
        agent=agent, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for director in directors:
            chat_id = str(director["telegram_user_id"])
            try:
                if severity is not None and title is not None:
                    ids = await bot.send_alert(title, text, severity=severity, chat_id=chat_id)
                else:
                    ids = await bot.send_message(text, chat_id=chat_id)
                message_ids.extend(ids)
            except TelegramError as exc:
                log.error("Could not notify Director {} for {}: {}", chat_id, agent, exc)

    return message_ids
