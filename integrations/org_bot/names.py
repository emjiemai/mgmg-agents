"""Every employee's real name — typed by them, never taken from Telegram.

Telegram profile names ("GMHRD", "Ulug'bek AI") are useless on documents and
make it impossible for the Director to say "tell Alisher ..." and have the
bot know who that is. So the bot asks each employee once for their first
name and surname, checks it with the same AI answer check the permission
form uses, and keeps it in ``employees.full_name`` (Uzbek Cyrillic).

Until an employee has given their name, the bot answers every message they
send with the question, and processes nothing else — one short, one-time
step, rather than tasks and reports landing under a nickname.

The Director is never gated: their messages are orders to route, and a
"what's your name?" in front of them would swallow a real task.
"""

from __future__ import annotations

import uuid
from typing import Any

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import answer_check, store
from integrations.org_bot.permissions import Field
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "ops-manager-bot"
log = setup_logging("names")

NAME_FIELD = Field(
    "full_name",
    "Исм ва фамилия",
    "Исм ва фамилиянгизни тўлиқ ёзинг (масалан: Алишер Каримов).",
    "a real person's first name AND surname (at least two words); a nickname, a single name, a "
    "Telegram-style handle, a greeting or any other message is not an answer",
)
NAME_CONTEXT = "the company's employee list, which the Director uses to address people by name"

ASK_TEXT = (
    "👤 <b>Ўзингизни таништиринг</b>\n\n"
    "Директор топшириқни айнан сизга бера олиши учун исм ва фамилиянгизни тўлиқ ёзинг "
    "(масалан: Алишер Каримов)."
)
NOT_SENT_NOTE = "\n\n<i>Хабарингиз ҳали юборилмади — исмингизни ёзганингиздан кейин уни қайта юборинг.</i>"


def person_name(employee: dict[str, Any] | None) -> str:
    """The name to show for an employee: the one they typed, else Telegram's."""
    if not employee:
        return "—"
    return (employee.get("full_name") or "").strip() or employee.get("display_name") or "—"


def needs_name(employee: dict[str, Any]) -> bool:
    """Whether the bot must get this employee's name before anything else."""
    return employee.get("role") != DIRECTOR_ROLE and not (employee.get("full_name") or "").strip()


def _bot(run_id: uuid.UUID) -> TelegramBot:
    return TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    )


async def collect_name(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str:
    """Handle a message from an employee the bot doesn't have a name for yet.

    If they haven't been asked, this message is something else (a report, a
    question): ask for the name and say the message wasn't sent. If they have
    been asked, this message is taken as the answer and checked.

    Returns:
        An outcome string for the webhook log.
    """
    telegram_user_id = employee["telegram_user_id"]
    text = (message.get("text") or "").strip()

    async with _bot(run_id) as bot:
        if not employee.get("name_asked_at") or not text:
            await store.mark_name_asked(telegram_user_id)
            await bot.send_message(ASK_TEXT + NOT_SENT_NOTE, chat_id=str(telegram_user_id))
            return "name_asked"

        acceptable, follow_up, value = await answer_check.check_answer(
            NAME_FIELD, text, run_id, agent=AGENT, context=NAME_CONTEXT
        )
        if not acceptable:
            await bot.send_message(
                f"🔁 {escape(follow_up or NAME_FIELD.question)}"
                "\n\n<i>Аввал исм ва фамилиянгизни ёзинг — шундан кейин бошқа хабарларингиз қабул қилинади.</i>",
                chat_id=str(telegram_user_id),
            )
            return "name_reasked"

        await store.set_employee_full_name(telegram_user_id, value)
        await bot.send_message(
            f"✅ Раҳмат, <b>{escape(value)}</b>! Исмингиз сақланди.", chat_id=str(telegram_user_id)
        )
    log.info("Employee {} gave their name", telegram_user_id)
    return "name_saved"


async def ask_missing_names(run_id: uuid.UUID) -> int:
    """Ask every active employee without a name, once each.

    Returns:
        How many were asked.
    """
    employees = await store.employees_to_ask_name()
    asked = 0
    async with _bot(run_id) as bot:
        for employee in employees:
            try:
                await bot.send_message(ASK_TEXT, chat_id=str(employee["telegram_user_id"]))
            except TelegramError as exc:
                log.warning("Could not ask {} for their name: {}", employee["telegram_user_id"], exc)
                continue
            await store.mark_name_asked(employee["telegram_user_id"])
            asked += 1
    log.info("Asked {} employee(s) for their name", asked)
    return asked
