"""Server-side timers: they keep running when nobody is online (spec §1 "HR мувофиқлаштирувчи").

Every proactive message is first written to `outbound_messages` with an idempotency key, then
sent, then marked sent. A restart in between resends a pending one instead of losing it, and
the key stops a second copy (A09). Ordinary employees get at most PROACTIVE_DAILY_LIMIT
planned messages a day, only on their work days and hours (spec §10, A07).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hari import onboarding as O
from hari.bot import texts as T
from hari.config import settings
from hari.db.models import Employee, HRCase, OnboardingPlan, OutboundMessage, as_utc
from hari.db.session import session_scope
from hari.worktime import fmt, is_work_time, local, now_utc, today_local

PROACTIVE_KINDS = {"onboarding"}  # counted against the daily limit
TICK_SECONDS = 60


async def proactive_sent_today(session: AsyncSession, emp: Employee, day) -> int:  # noqa: ANN001
    return (await session.execute(
        select(func.count()).select_from(OutboundMessage).where(
            OutboundMessage.employee_id == emp.id, OutboundMessage.local_date == day,
            OutboundMessage.kind.in_(PROACTIVE_KINDS), OutboundMessage.status != "failed")
    )).scalar_one()


async def queue(session: AsyncSession, emp: Employee, kind: str, key: str, text: str, now: datetime) -> OutboundMessage | None:
    """Reserve a message. None = already sent/queued under this key, or over the daily limit."""
    day = today_local(now)
    if (await session.execute(select(OutboundMessage.id).where(OutboundMessage.idempotency_key == key))).first():
        return None
    if kind in PROACTIVE_KINDS and await proactive_sent_today(session, emp, day) >= settings.proactive_daily_limit:
        return None
    msg = OutboundMessage(employee_id=emp.id, kind=kind, idempotency_key=key, local_date=day, text=text)
    session.add(msg)
    try:
        await session.flush()
    except IntegrityError:
        return None
    return msg


async def deliver(bot: Bot, msg_id: str) -> bool:
    async with session_scope() as session:
        msg = await session.get(OutboundMessage, msg_id)
        if msg is None or msg.status == "sent":
            return False
        emp = await session.get(Employee, msg.employee_id)
        msg.attempts += 1
        text = msg.text
    ok = False
    if emp is not None and emp.telegram_user_id:
        try:
            await bot.send_message(emp.telegram_user_id, T.say(emp, text))
            ok = True
        except TelegramAPIError as exc:
            logger.warning("Proactive message to {} failed: {}", emp.code, type(exc).__name__)
    async with session_scope() as session:
        msg = await session.get(OutboundMessage, msg_id)
        if ok:
            msg.status, msg.sent_at = "sent", now_utc()
        elif msg.attempts >= 3:
            msg.status = "failed"
    return ok


def nudge_text(emp: Employee, steps: list) -> str:  # noqa: ANN001
    lines = [f"Хайрли тонг, {emp.full_name.split()[1] if len(emp.full_name.split()) > 1 else emp.full_name}. "
             "Мослашув режангиздаги очиқ қадамлар:"]
    lines += [f"• {s.title}" for s in steps[:5]]
    lines.append("\n«📋 Режам» тугмасини босинг. Қайси қадам тушунарсиз бўлса, ёзинг.")
    return "\n".join(lines)


async def onboarding_nudges(session: AsyncSession, now: datetime) -> list[str]:
    queued: list[str] = []
    plans = (await session.execute(select(OnboardingPlan).where(OnboardingPlan.status == "active"))).scalars().all()
    for plan in plans:
        emp = await session.get(Employee, plan.employee_id)
        if emp is None or emp.status != "active" or not emp.telegram_user_id or not is_work_time(emp, now):
            continue  # on leave/absent, not bound yet, or outside their hours: wait (A07)
        await session.refresh(plan, ["steps"])
        steps = O.due_open_steps(emp, plan, today_local(now))
        if not steps:
            continue
        msg = await queue(session, emp, "onboarding", f"onb:{emp.id}:{today_local(now)}", nudge_text(emp, steps), now)
        if msg:
            queued.append(msg.id)
    return queued


async def case_escalations(session: AsyncSession, now: datetime) -> list[str]:
    """First answer late → remind the handler once, and tell the owner once (spec §32 «ёпилиш стандарти»)."""
    queued: list[str] = []
    late = (await session.execute(
        select(HRCase).where(HRCase.status == "submitted", HRCase.first_response_at.is_(None))
    )).scalars().all()
    owners = (await session.execute(select(Employee).where(Employee.role == "owner", Employee.status == "active"))).scalars().all()
    for case in late:
        if as_utc(case.first_response_due) > now:
            continue
        if case.assignee_id:
            assignee = await session.get(Employee, case.assignee_id)
            msg = await queue(session, assignee, "escalation", f"case_due:{case.id}",
                              f"⏰ {case.number} ({case.title}) — биринчи жавоб муддати ўтди ({fmt(case.first_response_due)}). "
                              "«📥 Иш навбати»дан жавоб беринг.", now)
            if msg:
                queued.append(msg.id)
        for owner in owners:
            if owner.id in (case.assignee_id, case.about_employee_id):
                continue
            msg = await queue(session, owner, "escalation", f"case_esc:{case.id}:{owner.id}",
                              f"⚠️ {case.number} ({case.title}) — муддатида жавоб берилмади. Масъул: "
                              f"{'тайинланмаган' if not case.assignee_id else 'жавоб бермади'}.", now)
            if msg:
                queued.append(msg.id)
    return queued


async def activate_started(session: AsyncSession, now: datetime) -> None:
    rows = (await session.execute(select(Employee).where(Employee.status == "preparing"))).scalars().all()
    for emp in rows:
        if emp.identity_verified_at and emp.start_date and emp.start_date <= today_local(now):
            emp.status = "active"


async def pending_retries(session: AsyncSession, now: datetime) -> list[str]:
    rows = (await session.execute(select(OutboundMessage).where(OutboundMessage.status == "pending"))).scalars().all()
    return [m.id for m in rows if as_utc(m.created_at) < now - timedelta(minutes=2)
            and as_utc(m.created_at) > now - timedelta(hours=6)]


async def tick(bot: Bot, now: datetime | None = None) -> int:
    if settings.hari_paused:
        return 0
    now = now or now_utc()
    async with session_scope() as session:
        await activate_started(session, now)
        ids = await pending_retries(session, now)
        ids += await onboarding_nudges(session, now)
        ids += await case_escalations(session, now)
    sent = 0
    for msg_id in ids:
        sent += await deliver(bot, msg_id)
    return sent


async def run_forever(bot: Bot) -> None:
    logger.info("Scheduler started (every {} s, Tashkent now {})", TICK_SECONDS, local(now_utc()).strftime("%H:%M"))
    while True:
        try:
            await tick(bot)
        except Exception as exc:  # noqa: BLE001 — one bad tick must not stop the timers
            logger.exception("Scheduler tick failed: {}", exc)
        await asyncio.sleep(TICK_SECONDS)
