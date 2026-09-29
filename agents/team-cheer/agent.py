"""Agent — team cheer: encouragement, a joke, thanks — three times a working day.

10:00 encouragement, 14:00 a joke or fun fact with a fun question, 17:35
thanks for the day with "how was it?" — to every employee except the
Director (Saturday/Sunday only the weekend workers). What each slot says and
why is in ``integrations/org_bot/cheer.py``; taps and replies are handled by
OPS Manager Bot (``ops_manager.py``).

One Render cron service runs this at :00 and :35 of 10, 14 and 17 o'clock;
each run sends the slot that is due and exits quietly otherwise.

Run:
    python agents/team-cheer/agent.py                        # whatever is due now
    python agents/team-cheer/agent.py --slot evening --dry-run   # print, send nothing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date
from pathlib import Path

# The folder name contains a hyphen, so this file cannot be imported as a
# package. Put the project root on sys.path and run it as a script instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.ai.openrouter_client import OpenRouterClient
from integrations.common.config import settings
from integrations.common.db import close_pool
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local
from integrations.org_bot import cheer, kpi, names, store
from integrations.org_bot.roles import DIRECTOR_ROLE
from integrations.telegram.bot import TelegramBot, TelegramError

AGENT = cheer.AGENT
log = setup_logging(AGENT)

REQUIRED_SETTINGS = {"ops_manager_bot_telegram_bot_token"}


async def write(slot: str, day: date, run_id: uuid.UUID) -> cheer.Cheer:
    """Today's message for ``slot``: the AI's, or the built-in one if the AI fails or breaks a rule."""
    try:
        recent = await store.recent_cheer_texts()
    except Exception as exc:  # noqa: BLE001 — a missing history must not cost the message
        log.warning("Could not read recent cheer messages: {}", exc)
        recent = []
    try:
        async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai:
            raw = await ai.complete(cheer.SYSTEM_PROMPT, cheer.user_prompt(slot, day, recent), json_mode=True)
    except Exception as exc:  # noqa: BLE001 — any AI failure (network, every model down) falls back
        log.warning("AI could not write the {} message ({}) — using the built-in one", slot, exc)
        return cheer.fallback(slot, day)
    written = cheer.parse_ai(raw, slot)
    if written is None:
        log.warning("AI's {} message broke a rule — using the built-in one: {}", slot, raw[:300])
        return cheer.fallback(slot, day)
    return written


async def send_slot(slot: str, run_id: uuid.UUID) -> int:
    """Send ``slot`` to everyone at work today, once.

    Returns:
        How many people got it.
    """
    day = now_local().date()
    employees = [
        e for e in await store.list_active_employees() if e["role"] != DIRECTOR_ROLE and kpi.works_on(e, day)
    ]
    if not employees:
        log.info("Nobody at work today besides the Director — nothing to send")
        return 0

    if settings.dry_run:
        # Before any write: a dry run must not take the slot a real run needs.
        message = await write(slot, day, run_id)
        log.info("[dry run] {} ({}):\n{}", slot, message.source, cheer.message_text(slot, message, "Исм"))
        for option in message.options:
            log.info("[dry run]   [{}] -> {}", option["label"], option["reply"])
        log.info("[dry run] would go to {} employee(s)", len(employees))
        return 0

    claimed = await store.claim_cheer(day, slot)
    if claimed is None:
        log.info("The {} message for {} was already sent — skipping", slot, day)
        return 0
    cheer_id = str(claimed["id"])
    message = await write(slot, day, run_id)
    await store.set_cheer_content(
        cheer_id, text=message.text, question=message.question, options=message.options, source=message.source
    )

    sent = 0
    reply_markup = cheer.keyboard(cheer_id, message)
    async with TelegramBot(
        agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()
    ) as bot:
        for employee in employees:
            text = cheer.message_text(slot, message, names.person_name(employee))
            try:
                ids = await bot.send_message(
                    text, chat_id=str(employee["telegram_user_id"]), reply_markup=reply_markup
                )
            except TelegramError as exc:
                log.error("Could not send the {} message to {}: {}", slot, employee["display_name"], exc)
                continue
            await store.save_cheer_delivery(cheer_id, employee["telegram_user_id"], ids[-1] if ids else None, text)
            sent += 1

    log.info("{} message ({}) sent to {} of {} employee(s)", slot, message.source, sent, len(employees))
    return sent


async def run(slot: str | None, dry_run: bool = False) -> int:
    """Send the due (or given) slot.

    Returns:
        Process exit code — 0 on success or nothing due, 2 if config is incomplete.
    """
    if dry_run:
        settings.dry_run = True
    run_id = uuid.uuid4()

    slot = slot or cheer.due_slot(now_local())
    if slot is None:
        log.info("No cheer message is due at {:%H:%M} — nothing to do", now_local())
        return 0
    log.info("Team cheer run {} starting (slot={}, dry_run={})", run_id, slot, settings.dry_run)

    unfilled = REQUIRED_SETTINGS & set(settings.missing_placeholders())
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(sorted(unfilled)))
        return 2
    if not settings.team_cheer_enabled and not settings.dry_run:
        log.info("Team cheer is paused (TEAM_CHEER_ENABLED=false) — nothing sent")
        return 0
    if settings.bots_frozen:
        log.info("Bots frozen (BOTS_FROZEN=true) — nothing sent")
        return 0

    await send_slot(slot, run_id)
    return 0


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Friendly messages to the team, three times a day.")
    parser.add_argument("--slot", choices=sorted(cheer.SLOTS), help="send this slot now, whatever the time")
    parser.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(_main(args.slot, args.dry_run))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


async def _main(slot: str | None, dry_run: bool) -> int:
    try:
        return await run(slot, dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
