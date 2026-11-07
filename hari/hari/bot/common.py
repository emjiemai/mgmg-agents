"""Helpers shared by the handlers."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hari.access import is_staff
from hari.db.models import Employee
from hari.employees import by_telegram

bot_username: str = ""  # set at start-up from getMe, for invite links


async def staff_by_tg(session: AsyncSession, telegram_user_id: int) -> Employee | None:
    """The verified employee behind this Telegram account, or None (no rights at all)."""
    emp = await by_telegram(session, telegram_user_id)
    return emp if emp is not None and is_staff(emp) else None


async def handles_work(session: AsyncSession, emp: Employee) -> bool:
    if emp.role != "employee":
        return True
    mentee = (await session.execute(
        select(Employee.id).where(or_(Employee.mentor_id == emp.id, Employee.manager_id == emp.id)).limit(1)
    )).first()
    return mentee is not None


def invite_link(token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}" if bot_username else f"/start {token}"
