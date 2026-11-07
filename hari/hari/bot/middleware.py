"""Update de-duplication: a Telegram update handled once is never handled again (A09)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Update
from sqlalchemy.exc import IntegrityError

from hari.db.models import ProcessedUpdate
from hari.db.session import session_scope


class DedupeMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
                       event: Update, data: dict[str, Any]) -> Any:
        try:
            async with session_scope() as session:
                session.add(ProcessedUpdate(update_id=event.update_id))
        except IntegrityError:
            return None  # already handled
        return await handler(event, data)
