"""HR requests register (spec §10, §18, §32).

Every request gets a number, an owner, a first-answer deadline (4 work hours) and a
privacy class. HARI never decides: it routes, reminds and records. "Answer sent" and
"resolved" are separate states. A complaint never goes to the person it is about.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.access import can_handle_case
from hari.config import settings
from hari.db.models import CaseCounter, CaseEvent, Employee, HRCase
from hari.worktime import add_work_hours, local, now_utc


@dataclass(frozen=True)
class CaseType:
    slug: str
    label: str
    route: tuple[str, ...]  # who handles it, in order: "manager" or a role
    privacy: str = "normal"


CASE_TYPES: dict[str, CaseType] = {c.slug: c for c in [
    CaseType("leave", "Таътил ёки ишда бўлмаслик", ("manager", "hr")),
    CaseType("schedule", "Иш жадвали ёки масофавий иш", ("manager", "hr")),
    CaseType("timesheet", "Давомат (табель) хатоси", ("hr",)),
    CaseType("certificate", "Маълумотнома керак", ("hr",)),
    CaseType("data_fix", "Профилимдаги хатони тузатиш", ("hr",)),
    CaseType("training", "Ўқиш ёки ёрдам сўрови", ("manager", "hr")),
    CaseType("pay_question", "Иш ҳақи ҳисоби бўйича савол", ("accountant", "hr"), "sensitive"),
    CaseType("legal", "Ҳуқуқий савол (шартнома, низо)", ("lawyer", "hr"), "sensitive"),
    CaseType("complaint", "Шикоят ёки хавфсиз мурожаат", ("hr", "owner"), "sensitive"),
    CaseType("support", "Шахсий суҳбат ёки қўллаб-қувватлаш", ("hr",), "sensitive"),
    CaseType("question", "HARI жавоб топа олмаган савол", ("hr",)),
    CaseType("other", "Бошқа", ("hr",)),
]}


class CaseError(ValueError):
    pass


async def _next_number(session: AsyncSession) -> str:
    period = local(now_utc()).strftime("%Y-%m")
    counter = await session.get(CaseCounter, period)
    if counter is None:
        counter = CaseCounter(period=period, last=0)
        session.add(counter)
    counter.last += 1
    await session.flush()
    return f"HR-{period}-{counter.last:04d}"


async def _first_active(session: AsyncSession, role: str, exclude: set[str]) -> Employee | None:
    rows = (await session.execute(
        select(Employee).where(Employee.role == role, Employee.status == "active",
                               Employee.telegram_user_id.is_not(None)).order_by(Employee.code)
    )).scalars().all()
    return next((e for e in rows if e.id not in exclude), None)


async def choose_assignee(session: AsyncSession, requester: Employee, ctype: CaseType,
                          about: Employee | None) -> Employee | None:
    """Follow the route; skip the requester and whoever the request is about.
    If HR itself is the subject of a complaint, it goes to the owner (spec §18, A13)."""
    exclude = {requester.id} | ({about.id} if about else set())
    route = list(ctype.route)
    if about is not None and about.role in ("hr", "owner"):
        route = ["owner", "hr"] if about.role == "hr" else ["hr"]
    for step in route:
        if step == "manager":
            if requester.manager_id and requester.manager_id not in exclude:
                mgr = await session.get(Employee, requester.manager_id)
                if mgr and mgr.status == "active" and mgr.telegram_user_id:
                    return mgr
            continue
        person = await _first_active(session, step, exclude)
        if person:
            return person
    return await _first_active(session, "owner", exclude)


async def open_case(session: AsyncSession, requester: Employee, type_slug: str, body: str,
                    about: Employee | None = None, dedupe: str | None = None) -> tuple[HRCase, bool]:
    """Returns (case, created). The same request replayed (a retried update) opens it once."""
    ctype = CASE_TYPES.get(type_slug)
    if ctype is None:
        raise CaseError("unknown_type")
    body = body.strip()
    if not body:
        raise CaseError("empty")
    key_basis = dedupe or f"{requester.id}|{type_slug}|{body}|{local(now_utc()).date()}"
    key = hashlib.sha256(key_basis.encode("utf-8")).hexdigest()
    existing = (await session.execute(select(HRCase).where(HRCase.idempotency_key == key))).scalar_one_or_none()
    if existing:
        return existing, False

    assignee = await choose_assignee(session, requester, ctype, about)
    now = now_utc()
    case = HRCase(
        number=await _next_number(session), type=type_slug, requester_id=requester.id, privacy=ctype.privacy,
        status="submitted", assignee_id=assignee.id if assignee else None, title=ctype.label,
        body=body[:4000], about_employee_id=about.id if about else None, idempotency_key=key,
        first_response_due=add_work_hours(assignee, now, settings.case_first_response_hours),
    )
    session.add(case)
    await session.flush()
    session.add(CaseEvent(case_id=case.id, actor_id=requester.id, event="submitted"))
    await audit.record(session, "case.open", "submitted" if assignee else "unassigned", actor_id=requester.id,
                       object_type="case", object_id=case.number, detail=type_slug)
    return case, True


async def get_by_number(session: AsyncSession, number: str) -> HRCase | None:
    return (await session.execute(select(HRCase).where(HRCase.number == number))).scalar_one_or_none()


async def _require_handler(session: AsyncSession, actor: Employee, case: HRCase, action: str) -> None:
    if not can_handle_case(actor, case.assignee_id, case.about_employee_id):
        await audit.record(session, f"case.{action}", "denied", actor_id=actor.id, object_type="case",
                           object_id=case.number)
        raise CaseError("not_allowed")


async def take(session: AsyncSession, actor: Employee, case: HRCase) -> HRCase:
    await _require_handler(session, actor, case, "take")
    if case.status == "submitted":
        case.status, case.assignee_id = "accepted", actor.id
        case.first_response_at = case.first_response_at or now_utc()
        session.add(CaseEvent(case_id=case.id, actor_id=actor.id, event="accepted"))
        await audit.record(session, "case.take", "accepted", actor_id=actor.id, object_type="case",
                           object_id=case.number)
    return case


async def answer(session: AsyncSession, actor: Employee, case: HRCase, decision: str, close: bool) -> HRCase:
    """The handler's written answer/decision. Close only when the matter is actually done."""
    await _require_handler(session, actor, case, "answer")
    if case.status in ("closed", "rejected"):
        raise CaseError("closed")
    case.decision = decision.strip()[:4000]
    case.first_response_at = case.first_response_at or now_utc()
    case.status = "closed" if close else "decided"
    if close:
        case.closed_at = now_utc()
    session.add(CaseEvent(case_id=case.id, actor_id=actor.id, event=case.status))
    await audit.record(session, "case.answer", case.status, actor_id=actor.id, object_type="case",
                       object_id=case.number)
    return case


async def open_for_handler(session: AsyncSession, actor: Employee) -> list[HRCase]:
    stmt = select(HRCase).where(HRCase.status.in_(("submitted", "accepted", "in_review", "decided")))
    rows = (await session.execute(stmt.order_by(HRCase.created_at))).scalars().all()
    return [c for c in rows if can_handle_case(actor, c.assignee_id, c.about_employee_id)
            and (c.assignee_id == actor.id or c.privacy == "normal" or actor.role == "owner")]


async def mine(session: AsyncSession, requester: Employee, limit: int = 10) -> list[HRCase]:
    return list((await session.execute(
        select(HRCase).where(HRCase.requester_id == requester.id).order_by(HRCase.created_at.desc()).limit(limit)
    )).scalars().all())
