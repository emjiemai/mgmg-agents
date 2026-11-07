"""Start HARI: database, knowledge base, Telegram polling and the scheduler.

    python -m hari.main
"""

from __future__ import annotations

import asyncio
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from loguru import logger

from hari import scheduler
from hari.bot import common
from hari.bot.handlers import employee, staff, start
from hari.bot.middleware import DedupeMiddleware
from hari.config import settings
from hari.db.session import create_schema, session_scope
from hari.knowledge.loader import load_directory


_dispatcher: Dispatcher | None = None


def build_dispatcher() -> Dispatcher:
    """One dispatcher per process: aiogram routers can be attached to a single parent only."""
    global _dispatcher
    if _dispatcher is None:
        dp = Dispatcher(storage=MemoryStorage())
        dp.update.outer_middleware(DedupeMiddleware())
        dp.include_router(start.router)
        dp.include_router(staff.router)
        dp.include_router(employee.router)  # last: its catch-all answers questions
        _dispatcher = dp
    return _dispatcher


async def prepare() -> None:
    await create_schema()
    async with session_scope() as session:
        result = await load_directory(session, settings.knowledge_dir)
    logger.info("Knowledge: {} loaded, {} unchanged, {} refused, {} retired",
                len(result.loaded), len(result.unchanged), len(result.refused), len(result.retired))
    for name, why in result.refused:
        logger.warning("Refused {}: {}", name, why)
    if not settings.openrouter_api_key.get_secret_value():
        logger.warning("OPENROUTER_API_KEY is empty: answers use the approved excerpt only (no AI text)")


async def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add("logs/hari.log", rotation="5 MB", retention=10, level="INFO", encoding="utf-8")
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (.env)")
    await prepare()
    bot = Bot(token)
    me = await bot.get_me()
    common.bot_username = me.username or ""
    logger.info("HARI is running as @{}", common.bot_username)
    dp = build_dispatcher()
    timers = asyncio.create_task(scheduler.run_forever(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        timers.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
