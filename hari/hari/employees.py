"""Employee cards, one-time invite links and identity binding (spec §5).

1. HR enters the card. The server checks required fields and duplicates.
2. HARI makes a one-time link; HR hands it to the person (not a group chat).
3. The person opens it from their own Telegram: the account is bound to the card.
A role or status never comes from chat text — only from this card.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.config import settings
from hari.db.models import LANGUAGES, ROLES, Employee, Invite, as_utc
from hari.translit import normalize
from hari.worktime import now_utc, today_local


class CardError(ValueError):
    """A card field is wrong; the message is shown to HR as is (Uzbek)."""


@dataclass
class CardInput:
    full_name: str
    department: str
    position: str
    role: str = "employee"
    manager_id: str | None = None
    mentor_id: str | None = None
    start_date: date | None = None
    language: str = "uz_cyrl"
    work_start_hour: int = 9
    work_end_hour: int = 18
    work_days: str = "0,1,2,3,4,5"


def dedupe_key(card: CardInput) -> str:
    basis = "|".join([normalize(card.full_name), normalize(card.department), str(card.start_date or "")])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def validate(card: CardInput) -> None:
    if len(card.full_name.split()) < 2:
        raise CardError("ФИШни тўлиқ киритинг (камида исм ва фамилия).")
    if not card.department.strip() or not card.position.strip():
        raise CardError("Бўлим ва лавозим мажбурий.")
    if card.role not in ROLES:
        raise CardError("Роль нотўғри.")
    if card.language not in LANGUAGES:
        raise CardError("Тил нотўғри.")
    if not (0 <= card.work_start_hour < card.work_end_hour <= 24):
        raise CardError("Иш соати нотўғри.")


async def _next_code(session: AsyncSession) -> str:
    count = (await session.execute(select(func.count()).select_from(Employee))).scalar_one()
    return f"E-{count + 1:04d}"


async def create_card(session: AsyncSession, card: CardInput, created_by: str | None) -> tuple[Employee, bool]:
    """Returns (employee, created). The same card sent again returns the existing one (A01)."""
    validate(card)
    key = dedupe_key(card)
    existing = (await session.execute(select(Employee).where(Employee.dedupe_key == key))).scalar_one_or_none()
    if existing:
        await audit.record(session, "employee.create", "duplicate", actor_id=created_by,
                           object_type="employee", object_id=existing.id)
        return existing, False
    for ref in (card.manager_id, card.mentor_id):
        if ref and await session.get(Employee, ref) is None:
            raise CardError("Кўрсатилган раҳбар ёки мураббий топилмади.")
    emp = Employee(
        code=await _next_code(session), full_name=" ".join(card.full_name.split()),
        department=card.department.strip(), position=card.position.strip(), role=card.role,
        status="preparing", manager_id=card.manager_id, mentor_id=card.mentor_id,
        start_date=card.start_date, language=card.language, work_start_hour=card.work_start_hour,
        work_end_hour=card.work_end_hour, work_days=card.work_days, dedupe_key=key, created_by=created_by,
    )
    session.add(emp)
    await session.flush()
    await audit.record(session, "employee.create", "created", actor_id=created_by, object_type="employee",
                       object_id=emp.id)
    return emp, True


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def make_invite(session: AsyncSession, emp: Employee, created_by: str | None) -> str:
    """A new one-time token; earlier unused ones for this person stop working."""
    for old in (await session.execute(
        select(Invite).where(Invite.employee_id == emp.id, Invite.used_at.is_(None))
    )).scalars():
        old.expires_at = now_utc()
    token = secrets.token_urlsafe(24)
    session.add(Invite(employee_id=emp.id, token_hash=_hash(token), created_by=created_by,
                       expires_at=now_utc() + timedelta(hours=settings.invite_ttl_hours)))
    await audit.record(session, "invite.create", "created", actor_id=created_by, object_type="employee",
                       object_id=emp.id)
    return token


class BindError(ValueError):
    pass


async def bind_telegram(session: AsyncSession, token: str, telegram_user_id: int) -> Employee:
    """Bind the Telegram account that opened the link. One account = one person (spec §3, §5)."""
    invite = (await session.execute(select(Invite).where(Invite.token_hash == _hash(token)))).scalar_one_or_none()
    now = now_utc()
    if invite is None or invite.used_at is not None or as_utc(invite.expires_at) <= now:
        await audit.record(session, "invite.use", "invalid", detail=f"tg={telegram_user_id}")
        raise BindError("invalid")
    emp = await session.get(Employee, invite.employee_id)
    assert emp is not None
    other = (await session.execute(
        select(Employee).where(Employee.telegram_user_id == telegram_user_id, Employee.id != emp.id)
    )).scalar_one_or_none()
    if other is not None:
        await audit.record(session, "invite.use", "account_taken", object_type="employee", object_id=emp.id)
        raise BindError("account_taken")
    emp.telegram_user_id = telegram_user_id
    emp.identity_verified_at = now
    if emp.status == "preparing" and emp.start_date and emp.start_date <= today_local(now):
        emp.status = "active"
    invite.used_at = now
    await audit.record(session, "invite.use", "bound", actor_id=emp.id, object_type="employee", object_id=emp.id)
    return emp


async def by_telegram(session: AsyncSession, telegram_user_id: int) -> Employee | None:
    emp = (await session.execute(
        select(Employee).where(Employee.telegram_user_id == telegram_user_id)
    )).scalar_one_or_none()
    if emp is None or emp.status == "archived":
        return None
    return emp




async def list_staff(session: AsyncSession, roles: set[str] | None = None) -> list[Employee]:
    stmt = select(Employee).where(Employee.status.in_(("preparing", "active", "absent", "exiting")))
    rows = (await session.execute(stmt.order_by(Employee.full_name))).scalars().all()
    return [e for e in rows if roles is None or e.role in roles]
