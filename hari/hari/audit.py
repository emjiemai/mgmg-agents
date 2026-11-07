"""Audit trail: who did what, to which object, with what outcome. Never message text or personal data."""

from __future__ import annotations

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from hari.db.models import AuditEvent


async def record(
    session: AsyncSession,
    action: str,
    outcome: str,
    *,
    actor_id: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    detail: str | None = None,
) -> None:
    session.add(AuditEvent(
        actor_id=actor_id, action=action, outcome=outcome,
        object_type=object_type, object_id=object_id, detail=(detail or "")[:300] or None,
    ))
    logger.info("audit {} {} {}:{} -> {}", actor_id or "-", action, object_type or "-", object_id or "-", outcome)
