"""Onboarding plan: first day to 90 days (spec §9).

A step is closed only by the named person: "read" steps by the employee's own
"танишдим" (acknowledgement is recorded); "task" and "meeting" steps by the mentor,
manager or HR after proof. "Бажардим" from the employee is a submission, not acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hari import audit
from hari.access import can_accept_step
from hari.db.models import Employee, OnboardingPlan, OnboardingStep
from hari.worktime import now_utc, work_days_between


@dataclass(frozen=True)
class StepTemplate:
    phase: str
    day_offset: int  # work days after the first day (0 = first day)
    title: str
    kind: str  # read | task | meeting
    acceptor: str  # self | mentor | manager | hr
    description: str = ""
    doc_id: str | None = None


# Spec §9: first day, 2–7, 8–15, 30, 60, 90. Offsets are work days.
STANDARD: list[StepTemplate] = [
    StepTemplate("day1", 0, "Компания миссияси ва мақсадлари билан танишинг", "read", "self",
                 "Компания нима қилади, нимага интилади ва сизнинг ўрнингиз.", "EMJ-INTRO-001"),
    StepTemplate("day1", 0, "Одоб ва хавфсизлик қоидалари", "read", "self",
                 "Ҳурмат, ҳалол ҳисобот, махфийлик ва хавфсизлик.", "EMJ-ETHICS-001"),
    StepTemplate("day1", 0, "Савол ва мурожаат қандай берилади", "read", "self",
                 "HARIдан қандай фойдаланиш, ёрдам канали ва HR мурожаати.", "HARI-GUIDE-001"),
    StepTemplate("day1", 0, "Мураббийингиз билан танишув", "meeting", "mentor",
                 "Мураббий билан учрашинг ва биринчи ҳафта режасини келишинг."),
    StepTemplate("d2_7", 1, "Лавозимингиз SOPи билан танишинг", "read", "self",
                 "Ишингизнинг асосий қадамлари. SOP ҳали тасдиқланмаган бўлса, мураббийдан сўранг."),
    StepTemplate("d2_7", 3, "Биринчи намунали вазифа", "task", "mentor",
                 "Мураббий берган намунали вазифани бажаринг ва натижасини юборинг."),
    StepTemplate("d8_15", 9, "Малака чек-листи ва HR суҳбати", "meeting", "hr",
                 "Нималарни ўргангансиз, нима ҳали қийин — HR билан қисқа суҳбат."),
    StepTemplate("d30", 25, "30 кунлик хулоса: натижа, билим, юклама", "meeting", "manager",
                 "Раҳбарингиз билан 30 кунлик натижани муҳокама қилинг."),
    StepTemplate("d60", 50, "60 кун: мустақил иш ва ривожланиш режаси", "meeting", "manager"),
    StepTemplate("d90", 75, "90 кун: якуний хулоса", "meeting", "manager",
                 "Мослашув якуни. Хулосани раҳбар ёзма қабул қилади."),
]

PHASE_LABELS = {
    "day1": "Биринчи кун", "d2_7": "2–7 кун", "d8_15": "8–15 кун",
    "d30": "30 кун", "d60": "60 кун", "d90": "90 кун",
}


async def create_plan(session: AsyncSession, emp: Employee, started_on: date,
                      template: list[StepTemplate] | None = None, actor_id: str | None = None) -> OnboardingPlan:
    """One plan per person: calling again returns the existing plan (A01)."""
    existing = await get_plan(session, emp.id)
    if existing:
        return existing
    plan = OnboardingPlan(employee_id=emp.id, template="standard", started_on=started_on)
    for pos, tpl in enumerate(template or STANDARD):
        plan.steps.append(OnboardingStep(
            position=pos, phase=tpl.phase, day_offset=tpl.day_offset, title=tpl.title,
            description=tpl.description, kind=tpl.kind, doc_id=tpl.doc_id, acceptor=tpl.acceptor,
        ))
    session.add(plan)
    await session.flush()
    await audit.record(session, "onboarding.plan", "created", actor_id=actor_id, object_type="employee",
                       object_id=emp.id)
    return plan


async def get_plan(session: AsyncSession, employee_id: str) -> OnboardingPlan | None:
    return (await session.execute(
        select(OnboardingPlan).options(selectinload(OnboardingPlan.steps))
        .where(OnboardingPlan.employee_id == employee_id)
    )).scalar_one_or_none()


def due_open_steps(emp: Employee, plan: OnboardingPlan, today: date) -> list[OnboardingStep]:
    """Steps whose day has come and that are not accepted yet."""
    elapsed = work_days_between(emp, plan.started_on, today)
    return [s for s in plan.steps if s.day_offset <= elapsed and s.status in ("pending", "returned")]


def progress(plan: OnboardingPlan) -> tuple[int, int]:
    return sum(1 for s in plan.steps if s.status == "accepted"), len(plan.steps)


class StepError(ValueError):
    pass


async def _step_with_owner(session: AsyncSession, step_id: str) -> tuple[OnboardingStep, Employee]:
    step = await session.get(OnboardingStep, step_id)
    if step is None:
        raise StepError("not_found")
    plan = await session.get(OnboardingPlan, step.plan_id)
    owner = await session.get(Employee, plan.employee_id) if plan else None
    if owner is None:
        raise StepError("not_found")
    return step, owner


async def acknowledge(session: AsyncSession, actor: Employee, step_id: str) -> OnboardingStep:
    """The employee confirms they read a "read" step — closes it, with the time recorded."""
    step, owner = await _step_with_owner(session, step_id)
    if owner.id != actor.id or step.kind != "read" or step.status == "accepted":
        raise StepError("not_allowed")
    step.status, step.accepted_by, step.accepted_at = "accepted", actor.id, now_utc()
    await audit.record(session, "onboarding.ack", "accepted", actor_id=actor.id, object_type="step", object_id=step.id)
    await _maybe_finish(session, step.plan_id)
    return step


async def submit(session: AsyncSession, actor: Employee, step_id: str, proof_text: str | None,
                 proof_file_id: str | None) -> tuple[OnboardingStep, Employee]:
    step, owner = await _step_with_owner(session, step_id)
    if owner.id != actor.id or step.kind == "read" or step.status == "accepted":
        raise StepError("not_allowed")
    if not (proof_text and proof_text.strip()) and not proof_file_id:
        raise StepError("no_proof")
    step.status, step.proof_text, step.proof_file_id = "submitted", (proof_text or "").strip()[:2000], proof_file_id
    step.submitted_at, step.return_reason = now_utc(), None
    await audit.record(session, "onboarding.submit", "submitted", actor_id=actor.id, object_type="step",
                       object_id=step.id)
    return step, owner


async def decide(session: AsyncSession, actor: Employee, step_id: str, accept: bool,
                 reason: str | None = None) -> tuple[OnboardingStep, Employee]:
    step, owner = await _step_with_owner(session, step_id)
    if step.status != "submitted":
        raise StepError("not_submitted")
    if not can_accept_step(actor, step, owner):
        await audit.record(session, "onboarding.decide", "denied", actor_id=actor.id, object_type="step",
                           object_id=step.id)
        raise StepError("not_allowed")
    if accept:
        step.status, step.accepted_by, step.accepted_at = "accepted", actor.id, now_utc()
    else:
        step.status, step.return_reason = "returned", (reason or "").strip()[:1000] or None
    await audit.record(session, "onboarding.decide", "accepted" if accept else "returned", actor_id=actor.id,
                       object_type="step", object_id=step.id)
    if accept:
        await _maybe_finish(session, step.plan_id)
    return step, owner


async def _maybe_finish(session: AsyncSession, plan_id: str) -> None:
    plan = await session.get(OnboardingPlan, plan_id)
    if plan is None:
        return
    await session.refresh(plan, ["steps"])
    if all(s.status == "accepted" for s in plan.steps):
        plan.status = "done"


async def acceptor_for(session: AsyncSession, step: OnboardingStep, owner: Employee) -> Employee | None:
    """Who must look at a submitted step: the named person, else any HR."""
    target_id = {"mentor": owner.mentor_id, "manager": owner.manager_id}.get(step.acceptor)
    if target_id:
        person = await session.get(Employee, target_id)
        if person and person.telegram_user_id:
            return person
    hr = (await session.execute(
        select(Employee).where(Employee.role == "hr", Employee.status == "active",
                               Employee.telegram_user_id.is_not(None)).order_by(Employee.code)
    )).scalars().first()
    return hr
