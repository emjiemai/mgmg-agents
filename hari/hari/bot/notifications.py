"""Messages HARI sends to the people who must act: case handlers and step acceptors."""

from __future__ import annotations

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from hari.bot import texts as T
from hari.bot.ui import CaseActCb, StepCb, inline, notify, send_proof
from hari.db.models import Employee, HRCase, OnboardingStep
from hari.worktime import fmt


def case_card(case: HRCase, requester: Employee, about: Employee | None) -> str:
    lines = [
        f"📨 Янги мурожаат {case.number}",
        f"Тур: {case.title}" + (" (нозик — фақат сиз кўрасиз)" if case.privacy == "sensitive" else ""),
        f"Кимдан: {requester.full_name}, {requester.position} ({requester.department})",
    ]
    if about:
        lines.append(f"Кимга оид: {about.full_name}")
    lines += ["", case.body, "", f"Биринчи жавоб муддати: {fmt(case.first_response_due)}"]
    return "\n".join(lines)


def case_buttons(case: HRCase):  # noqa: ANN201
    return inline([[("✅ Қабул қилдим", CaseActCb(action="take", number=case.number)),
                    ("✍️ Жавоб / қарор", CaseActCb(action="answer", number=case.number))]])


async def tell_case_handler(bot: Bot, session: AsyncSession, case: HRCase) -> bool:
    if not case.assignee_id:
        return False
    assignee = await session.get(Employee, case.assignee_id)
    requester = await session.get(Employee, case.requester_id)
    about = await session.get(Employee, case.about_employee_id) if case.about_employee_id else None
    return await notify(bot, assignee, case_card(case, requester, about), case_buttons(case))


async def tell_step_acceptor(bot: Bot, acceptor: Employee, owner: Employee, step: OnboardingStep) -> bool:
    text = (
        f"🧭 Мослашув: {owner.full_name} ({owner.position}) «{step.title}» қадамини бажардим деб юборди.\n\n"
        f"Натижа: {step.proof_text or '(файл илова қилинган)'}\n\n"
        "Қабул қиласизми? Қайтарсангиз, сабабини ёзасиз."
    )
    ok = await notify(bot, acceptor, text, inline([[
        ("✅ Қабул", StepCb(action="ok", step_id=step.id)),
        ("↩️ Қайтариш", StepCb(action="back", step_id=step.id)),
    ]]))
    if ok:
        await send_proof(bot, acceptor, step.proof_file_id)
    return ok


def acceptor_label(step: OnboardingStep) -> str:
    return T.ACCEPTOR_LABELS.get(step.acceptor, step.acceptor)
