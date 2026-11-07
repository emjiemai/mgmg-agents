"""HR, managers, mentors and other handlers: new employee cards, the work queue, accepting
onboarding steps and answering HR requests. Every action re-checks the right on the server."""

from __future__ import annotations

from datetime import date, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from hari import cases as C
from hari import onboarding as O
from hari.access import can_accept_step, can_handle_case, can_manage_employees
from hari.bot import texts as T
from hari.config import settings
from hari.bot.common import invite_link, staff_by_tg
from hari.bot.notifications import case_buttons, case_card
from hari.bot.ui import CaseActCb, PickCb, StepCb, inline, notify
from hari.db.models import Employee, OnboardingPlan, OnboardingStep
from hari.db.session import session_scope
from hari.employees import CardError, CardInput, create_card, list_staff, make_invite
from hari.worktime import today_local

router = Router(name="staff")

ROLE_LABELS = {"employee": "Ходим", "manager": "Раҳбар", "hr": "HR", "accountant": "Бухгалтер",
               "lawyer": "Юрист", "it_admin": "IT админ", "owner": "Эгаси"}
LANG_LABELS = {"uz_cyrl": "Ўзбек (кирилл)", "uz_latn": "O'zbek (lotin)", "ru": "Русский"}


class NewEmp(StatesGroup):
    full_name = State()
    department = State()
    position = State()
    role = State()
    manager = State()
    mentor = State()
    start_date = State()
    language = State()
    confirm = State()


class ReturnReason(StatesGroup):
    waiting = State()


class CaseAnswer(StatesGroup):
    text = State()
    close = State()


# --------------------------------------------------------------------------- new employee card

async def _hr(message_or_query, session) -> Employee | None:  # noqa: ANN001
    emp = await staff_by_tg(session, message_or_query.from_user.id)
    return emp if emp is not None and can_manage_employees(emp) else None


@router.message(F.text.in_(T.both(T.BTN_NEW_EMP)))
@router.message(Command("yangi"))
async def new_employee(message: Message, state: FSMContext) -> None:
    async with session_scope() as session:
        hr = await _hr(message, session)
    if hr is None:
        await message.answer(T.NOT_ALLOWED)
        return
    await state.clear()
    await state.set_state(NewEmp.full_name)
    await message.answer(T.say(hr, "➕ Янги ходим карточкаси. Ҳар қадамда /bekor — бекор қилиш.\n\n"
                                   "1/8. Ходимнинг тўлиқ ФИШини ёзинг (фамилия, исм, отасининг исми):"))


@router.message(NewEmp.full_name, F.text)
async def ne_name(message: Message, state: FSMContext) -> None:
    if len(message.text.split()) < 2:
        await message.answer("Камида фамилия ва исм керак. Қайта ёзинг:")
        return
    await state.update_data(full_name=" ".join(message.text.split()))
    await state.set_state(NewEmp.department)
    await message.answer("2/8. Бўлим (масалан: Garmin сотув, Londry, Бухгалтерия):")


@router.message(NewEmp.department, F.text)
async def ne_dept(message: Message, state: FSMContext) -> None:
    await state.update_data(department=message.text.strip())
    await state.set_state(NewEmp.position)
    await message.answer("3/8. Лавозим:")


@router.message(NewEmp.position, F.text)
async def ne_position(message: Message, state: FSMContext) -> None:
    await state.update_data(position=message.text.strip())
    await state.set_state(NewEmp.role)
    async with session_scope() as session:
        hr = await _hr(message, session)
    roles = [r for r in ROLE_LABELS if r != "owner" or (hr and hr.role == "owner")]
    rows = [[(ROLE_LABELS[r], PickCb(field="role", value=r)) for r in roles[i:i + 3]] for i in range(0, len(roles), 3)]
    await message.answer("4/8. HARIдаги роли (ҳуқуқлар шундан келиб чиқади):", reply_markup=inline(rows))


async def _people_buttons(field: str) -> list:
    async with session_scope() as session:
        people = await list_staff(session)
    rows = [[(f"{p.full_name} — {p.position}", PickCb(field=field, value=p.id))] for p in people[:25]]
    rows.append([("— Йўқ / кейин —", PickCb(field=field, value="none"))])
    return rows


@router.callback_query(NewEmp.role, PickCb.filter(F.field == "role"))
async def ne_role(query: CallbackQuery, callback_data: PickCb, state: FSMContext) -> None:
    await state.update_data(role=callback_data.value)
    await state.set_state(NewEmp.manager)
    await query.message.edit_text(f"4/8. Роль: {ROLE_LABELS.get(callback_data.value, callback_data.value)}")
    await query.message.answer("5/8. Раҳбари:", reply_markup=inline(await _people_buttons("manager")))
    await query.answer()


@router.callback_query(NewEmp.manager, PickCb.filter(F.field == "manager"))
async def ne_manager(query: CallbackQuery, callback_data: PickCb, state: FSMContext) -> None:
    await state.update_data(manager_id=None if callback_data.value == "none" else callback_data.value)
    await state.set_state(NewEmp.mentor)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer("6/8. Мураббийи:", reply_markup=inline(await _people_buttons("mentor")))
    await query.answer()


@router.callback_query(NewEmp.mentor, PickCb.filter(F.field == "mentor"))
async def ne_mentor(query: CallbackQuery, callback_data: PickCb, state: FSMContext) -> None:
    await state.update_data(mentor_id=None if callback_data.value == "none" else callback_data.value)
    await state.set_state(NewEmp.start_date)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer("7/8. Ишни бошлаш санаси (кк.оо.йййй) ёки «бугун»:")
    await query.answer()


def parse_date(text: str) -> date | None:
    text = text.strip().lower()
    if text in ("бугун", "bugun", "сегодня"):
        return today_local()
    for pattern in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


@router.message(NewEmp.start_date, F.text)
async def ne_start(message: Message, state: FSMContext) -> None:
    start = parse_date(message.text)
    if start is None:
        await message.answer("Санани кк.оо.йййй шаклида ёзинг, масалан 10.11.2026:")
        return
    await state.update_data(start_date=start.isoformat())
    await state.set_state(NewEmp.language)
    await message.answer("8/8. Мулоқот тили:", reply_markup=inline(
        [[(label, PickCb(field="lang", value=code)) for code, label in LANG_LABELS.items()]]))


@router.callback_query(NewEmp.language, PickCb.filter(F.field == "lang"))
async def ne_lang(query: CallbackQuery, callback_data: PickCb, state: FSMContext) -> None:
    await state.update_data(language=callback_data.value)
    data = await state.get_data()
    async with session_scope() as session:
        mgr = await session.get(Employee, data["manager_id"]) if data.get("manager_id") else None
        mnt = await session.get(Employee, data["mentor_id"]) if data.get("mentor_id") else None
    summary = (
        "Текширинг:\n\n"
        f"ФИШ: {data['full_name']}\nБўлим: {data['department']}\nЛавозим: {data['position']}\n"
        f"Роль: {ROLE_LABELS[data['role']]}\nРаҳбар: {mgr.full_name if mgr else '—'}\n"
        f"Мураббий: {mnt.full_name if mnt else '—'}\n"
        f"Бошлаш: {date.fromisoformat(data['start_date']):%d.%m.%Y}\nТил: {LANG_LABELS[data['language']]}\n\n"
        "Ишга қабул қарори алоҳида тасдиқланган бўлиши керак. Карточкани сақлайми?"
    )
    await state.set_state(NewEmp.confirm)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(summary, reply_markup=inline([[
        ("✅ Сақлаш", PickCb(field="confirm", value="yes")), ("❌ Бекор", PickCb(field="confirm", value="no"))]]))
    await query.answer()


@router.callback_query(NewEmp.confirm, PickCb.filter(F.field == "confirm"))
async def ne_confirm(query: CallbackQuery, callback_data: PickCb, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.answer()
    if callback_data.value != "yes":
        await query.message.answer(T.CANCELLED)
        return
    async with session_scope() as session:
        hr = await _hr(query, session)
        if hr is None:
            await query.message.answer(T.NOT_ALLOWED)
            return
        start = date.fromisoformat(data["start_date"])
        try:
            emp, created = await create_card(session, CardInput(
                full_name=data["full_name"], department=data["department"], position=data["position"],
                role=data["role"], manager_id=data.get("manager_id"), mentor_id=data.get("mentor_id"),
                start_date=start, language=data["language"],
            ), created_by=hr.id)
        except CardError as exc:
            await query.message.answer(f"Сақланмади: {exc}")
            return
        await O.create_plan(session, emp, start, actor_id=hr.id)
        token = await make_invite(session, emp, created_by=hr.id)
    head = "✅ Карточка сақланди" if created else "ℹ️ Бу ходим аллақачон бор — янги карточка яратилмади"
    await query.message.answer(
        f"{head}: {emp.code} — {emp.full_name}.\nМослашув режаси тайёр.\n\n"
        f"Шахсий ҳавола (бир марталик, {settings.invite_ttl_hours} соат):\n{invite_link(token)}\n\n"
        "Ҳаволани ходимга шахсан беринг, умумий гуруҳга ташламанг. У уни ўз Telegram аккаунтидан очиши керак."
    )


@router.message(Command("taklif"))
async def reinvite(message: Message, command: CommandObject) -> None:
    code = (command.args or "").strip().upper()
    async with session_scope() as session:
        hr = await _hr(message, session)
        if hr is None:
            await message.answer(T.NOT_ALLOWED)
            return
        emp = (await session.execute(select(Employee).where(Employee.code == code))).scalar_one_or_none()
        if emp is None:
            await message.answer("Ходим топилмади. Мисол: /taklif E-0003")
            return
        token = await make_invite(session, emp, created_by=hr.id)
    await message.answer(f"Янги ҳавола ({emp.code}, {emp.full_name}). Эскиси ишламайди:\n{invite_link(token)}")


@router.message(F.text.in_(T.both(T.BTN_STAFF)))
@router.message(Command("xodimlar"))
async def staff_list(message: Message) -> None:
    async with session_scope() as session:
        hr = await _hr(message, session)
        if hr is None:
            await message.answer(T.NOT_ALLOWED)
            return
        people = await list_staff(session)
        plans = {p.employee_id: p for p in (await session.execute(select(OnboardingPlan))).scalars()}
        lines = ["👥 Ходимлар (✅ — Telegram боғланган, ⏳ — ҳали йўқ):"]
        for p in people:
            plan = plans.get(p.id)
            prog = ""
            if plan:
                await session.refresh(plan, ["steps"])
                done, total = O.progress(plan)
                prog = f", мослашув {done}/{total}"
            mark = "✅" if p.telegram_user_id else "⏳"
            lines.append(f"{mark} {p.code} {p.full_name} — {p.position}, {ROLE_LABELS.get(p.role, p.role)}{prog}")
    lines.append("\nЯнги ҳавола: /taklif E-0003")
    await message.answer("\n".join(lines)[:4000])


# --------------------------------------------------------------------------- work queue

@router.message(F.text.in_(T.both(T.BTN_QUEUE)))
async def work_queue(message: Message, bot: Bot) -> None:
    async with session_scope() as session:
        me = await staff_by_tg(session, message.from_user.id)
        if me is None:
            await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id))
            return
        steps = (await session.execute(select(OnboardingStep).where(OnboardingStep.status == "submitted"))).scalars().all()
        mine_steps = []
        for s in steps:
            plan = await session.get(OnboardingPlan, s.plan_id)
            owner = await session.get(Employee, plan.employee_id)
            if can_accept_step(me, s, owner):
                mine_steps.append((s, owner))
        my_cases = await C.open_for_handler(session, me)
        case_cards = []
        for c in my_cases:
            requester = await session.get(Employee, c.requester_id)
            about = await session.get(Employee, c.about_employee_id) if c.about_employee_id else None
            case_cards.append((c, case_card(c, requester, about)))
    if not mine_steps and not case_cards:
        await message.answer(T.say(me, "📥 Иш навбатингиз бўш."))
        return
    for s, owner in mine_steps:
        await message.answer(T.say(me, f"🧭 {owner.full_name}: «{s.title}»\nНатижа: {s.proof_text or '(файл)'}"),
                             reply_markup=inline([[("✅ Қабул", StepCb(action="ok", step_id=s.id)),
                                                   ("↩️ Қайтариш", StepCb(action="back", step_id=s.id))]]))
    for c, card in case_cards:
        status = {"submitted": "янги", "accepted": "кўрилмоқда", "decided": "жавоб берилган, очиқ"}.get(c.status, c.status)
        await message.answer(T.say(me, f"{card}\nҲолат: {status}"), reply_markup=case_buttons(c))


# --------------------------------------------------------------------------- accepting onboarding steps

@router.callback_query(StepCb.filter(F.action == "ok"))
async def step_accept(query: CallbackQuery, callback_data: StepCb, bot: Bot) -> None:
    async with session_scope() as session:
        me = await staff_by_tg(session, query.from_user.id)
        if me is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        try:
            step, owner = await O.decide(session, me, callback_data.step_id, accept=True)
        except O.StepError:
            await query.answer(T.say(me, "Қабул қилиб бўлмайди: ҳуқуқ йўқ ёки қадам аллақачон ҳал қилинган."),
                               show_alert=True)
            return
        plan = await O.get_plan(session, owner.id)
        finished = plan is not None and plan.status == "done"
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(T.say(me, f"✅ Қабул қилинди: {owner.full_name} — «{step.title}»."))
    await query.answer()
    await notify(bot, owner, T.STEP_ACCEPTED_TO_EMP.format(title=step.title))
    if finished:
        await notify(bot, owner, T.PLAN_DONE)


@router.callback_query(StepCb.filter(F.action == "back"))
async def step_return_ask(query: CallbackQuery, callback_data: StepCb, state: FSMContext) -> None:
    await state.set_state(ReturnReason.waiting)
    await state.update_data(step_id=callback_data.step_id)
    await query.message.answer("Қайтариш сабабини ёзинг (нимани тузатиш керак). Бекор: /bekor")
    await query.answer()


@router.message(ReturnReason.waiting, F.text)
async def step_return(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    async with session_scope() as session:
        me = await staff_by_tg(session, message.from_user.id)
        if me is None:
            return
        try:
            step, owner = await O.decide(session, me, data.get("step_id", ""), accept=False, reason=message.text)
        except O.StepError:
            await message.answer(T.say(me, "Қайтариб бўлмайди: ҳуқуқ йўқ ёки қадам аллақачон ҳал қилинган."))
            return
    await message.answer(T.say(me, f"↩️ Қайтарилди: {owner.full_name} — «{step.title}»."))
    await notify(bot, owner, T.STEP_RETURNED_TO_EMP.format(title=step.title, reason=step.return_reason or "—"))


# --------------------------------------------------------------------------- HR requests

@router.callback_query(CaseActCb.filter(F.action == "take"))
async def case_take(query: CallbackQuery, callback_data: CaseActCb, bot: Bot) -> None:
    async with session_scope() as session:
        me = await staff_by_tg(session, query.from_user.id)
        case = await C.get_by_number(session, callback_data.number)
        if me is None or case is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        try:
            await C.take(session, me, case)
        except C.CaseError:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        requester = await session.get(Employee, case.requester_id)
    await query.answer(T.say(me, "Қабул қилинди"))
    await notify(bot, requester, f"📨 {case.number}: мурожаатингизни {me.full_name} кўриб чиқмоқда.")


@router.callback_query(CaseActCb.filter(F.action == "answer"))
async def case_answer_ask(query: CallbackQuery, callback_data: CaseActCb, state: FSMContext) -> None:
    async with session_scope() as session:
        me = await staff_by_tg(session, query.from_user.id)
        case = await C.get_by_number(session, callback_data.number)
        allowed = me is not None and case is not None and can_handle_case(me, case.assignee_id, case.about_employee_id)
    if not allowed:
        await query.answer(T.NOT_ALLOWED, show_alert=True)
        return
    await state.set_state(CaseAnswer.text)
    await state.update_data(number=callback_data.number)
    await query.message.answer(f"{callback_data.number}: ходимга юбориладиган жавоб ёки қарорни ёзинг. "
                               "Асос ва кейинги қадамни ҳам ёзинг. Бекор: /bekor")
    await query.answer()


@router.message(CaseAnswer.text, F.text)
async def case_answer_text(message: Message, state: FSMContext) -> None:
    await state.update_data(answer=message.text.strip())
    await state.set_state(CaseAnswer.close)
    number = (await state.get_data())["number"]
    await message.answer("Масала тўлиқ ҳал бўлдими?", reply_markup=inline([[
        ("✅ Ҳа, ёпиш", CaseActCb(action="close_yes", number=number)),
        ("⏳ Йўқ, очиқ қолсин", CaseActCb(action="close_no", number=number))]]))


@router.callback_query(CaseAnswer.close, CaseActCb.filter(F.action.in_({"close_yes", "close_no"})))
async def case_answer_send(query: CallbackQuery, callback_data: CaseActCb, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    close = callback_data.action == "close_yes"
    async with session_scope() as session:
        me = await staff_by_tg(session, query.from_user.id)
        case = await C.get_by_number(session, data.get("number", ""))
        if me is None or case is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        try:
            await C.answer(session, me, case, data.get("answer", ""), close)
        except C.CaseError:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        requester = await session.get(Employee, case.requester_id)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.answer()
    await query.message.answer(f"Юборилди. {case.number} — {'ёпилди' if close else 'очиқ қолди'}.")
    state_text = "Мурожаат ёпилди." if close else "Мурожаат очиқ: кейинги қадам бажарилгач ёпилади."
    await notify(bot, requester, f"📨 {case.number} — {case.title}\n\nЖавоб ({me.full_name}):\n{case.decision}\n\n"
                                 f"{state_text}\nЭътирозингиз бўлса, «📝 Мурожаат» орқали ёзинг.")
