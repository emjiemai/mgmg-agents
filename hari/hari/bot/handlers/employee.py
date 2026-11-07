"""What every verified employee can do: plan, steps, documents, requests, profile, questions."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from hari import cases as C
from hari import onboarding as O
from hari.access import can_read_document
from hari.answer import answer_question
from hari.bot import texts as T
from hari.bot.common import handles_work, staff_by_tg
from hari.bot.notifications import acceptor_label, tell_case_handler, tell_step_acceptor
from hari.bot.ui import CaseTypeCb, DocCb, HelpfulCb, OfferCaseCb, PickCb, StepCb, inline, main_menu, notify
from hari.db.models import AnswerLog, Employee, KnowledgeChunk, KnowledgeDocument
from hari.db.session import session_scope
from hari.knowledge.search import Hit
from hari.answer import source_line
from hari.worktime import fmt, today_local

router = Router(name="employee")

# Types an employee picks from the menu ("question" is only offered after an unanswered question).
MENU_CASE_TYPES = ["leave", "schedule", "timesheet", "certificate", "data_fix", "training",
                   "pay_question", "legal", "complaint", "support", "other"]


class AskProof(StatesGroup):
    waiting = State()


class NewCase(StatesGroup):
    body = State()


# --------------------------------------------------------------------------- plan

def _plan_text(emp: Employee, plan, today) -> tuple[str, list]:  # noqa: ANN001
    done, total = O.progress(plan)
    due = {s.id for s in O.due_open_steps(emp, plan, today)}
    from hari.worktime import work_days_between

    elapsed = work_days_between(emp, plan.started_on, today)
    lines = [f"📋 Мослашув режаси — {done}/{total} қадам қабул қилинган", f"Бошланган: {plan.started_on:%d.%m.%Y}"]
    phase = None
    for s in plan.steps:
        if s.phase != phase:
            phase = s.phase
            lines.append(f"\n{O.PHASE_LABELS.get(phase, phase)}")
        icon = T.STEP_ICONS.get(s.status, "▫️") if s.day_offset <= elapsed or s.status != "pending" else T.STEP_ICON_FUTURE
        lines.append(f"{icon} {s.title} — {T.KIND_LABELS[s.kind]}, қабул: {T.ACCEPTOR_LABELS[s.acceptor]}")
        if s.status == "returned" and s.return_reason:
            lines.append(f"   ↩️ {s.return_reason}")
    buttons = []
    for s in plan.steps:
        if s.id not in due:
            continue
        if s.kind == "read":
            row = [(f"📖 Танишдим: {s.title}", StepCb(action="ack", step_id=s.id))]
            if s.doc_id:
                row.insert(0, ("📄 Ўқиш", DocCb(doc_id=s.doc_id)))
            buttons.append(row)
        else:
            buttons.append([(f"✅ Бажардим: {s.title}", StepCb(action="done", step_id=s.id))])
    if due:
        lines.append("\nБугунги очиқ қадамлар пастда.")
    return "\n".join(lines), buttons


@router.message(F.text.in_(T.both(T.BTN_PLAN)))
async def show_plan(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
        if emp is None:
            await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id))
            return
        plan = await O.get_plan(session, emp.id)
        if plan is None:
            await message.answer(T.say(emp, "Сизга ҳали мослашув режаси бириктирилмаган. Керак бўлса, "
                                            "«📝 Мурожаат» орқали HRдан сўранг."))
            return
        text, buttons = _plan_text(emp, plan, today_local())
    await message.answer(T.say(emp, text), reply_markup=inline(
        [[(T.say(emp, t), cb) for t, cb in row] for row in buttons]) if buttons else None)


@router.callback_query(DocCb.filter())
async def show_doc(query: CallbackQuery, callback_data: DocCb) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, query.from_user.id)
        doc = await session.get(KnowledgeDocument, callback_data.doc_id)
        # Opening checks the right again; an unknown and a forbidden document look the same (spec §22).
        if emp is None or doc is None or not can_read_document(emp, doc, today_local()):
            await query.answer(T.say(emp, "Ҳужжат ҳозир очиқ эмас ёки ҳали тасдиқланмаган."), show_alert=True)
            return
        chunks = (await session.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.doc_id, KnowledgeChunk.doc_version == doc.version)
            .order_by(KnowledgeChunk.position)
        )).scalars().all()
    body = "\n\n".join(f"{c.section}\n{c.text}" if c.section != doc.title else c.text for c in chunks)
    hit = Hit("", doc.doc_id, doc.title, doc.version, "", "", 0)
    text = f"{doc.title}\n\n{body}"[:3600] + f"\n\n{source_line([hit])}"
    await query.message.answer(T.say(emp, text))
    await query.answer()


@router.callback_query(StepCb.filter(F.action == "ack"))
async def ack_step(query: CallbackQuery, callback_data: StepCb) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, query.from_user.id)
        if emp is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        try:
            step = await O.acknowledge(session, emp, callback_data.step_id)
        except O.StepError:
            await query.answer(T.say(emp, "Бу қадам аллақачон ёпилган ёки сизга тегишли эмас."), show_alert=True)
            return
        plan = await O.get_plan(session, emp.id)
        finished = plan is not None and plan.status == "done"
    await query.answer(T.say(emp, "Қайд этилди ✅"))
    await query.message.answer(T.say(emp, f"✅ «{step.title}» — танишганингиз қайд этилди."))
    if finished:
        await query.message.answer(T.say(emp, T.PLAN_DONE))


@router.callback_query(StepCb.filter(F.action == "done"))
async def ask_proof(query: CallbackQuery, callback_data: StepCb, state: FSMContext) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, query.from_user.id)
        step = await session.get(O.OnboardingStep, callback_data.step_id)
    if emp is None or step is None:
        await query.answer(T.NOT_ALLOWED, show_alert=True)
        return
    await state.set_state(AskProof.waiting)
    await state.update_data(step_id=step.id)
    await query.message.answer(T.say(emp, T.ASK_PROOF.format(title=step.title, acceptor=acceptor_label(step))))
    await query.answer()


@router.message(AskProof.waiting, F.text | F.photo | F.document)
async def receive_proof(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    file_id = None
    if message.photo:
        file_id = f"photo:{message.photo[-1].file_id}"
    elif message.document:
        file_id = f"doc:{message.document.file_id}"
    text = message.text or message.caption
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
        if emp is None:
            await state.clear()
            return
        try:
            step, owner = await O.submit(session, emp, data.get("step_id", ""), text, file_id)
        except O.StepError as exc:
            await state.clear()
            await message.answer(T.say(emp, "Натижа сақланмади: " + (
                "матн ёки файл юборинг." if str(exc) == "no_proof" else "бу қадам сизга тегишли эмас ёки ёпилган.")))
            return
        acceptor = await O.acceptor_for(session, step, owner)
    await state.clear()
    delivered = acceptor is not None and await tell_step_acceptor(bot, acceptor, owner, step)
    await message.answer(T.say(emp, T.PROOF_SENT.format(acceptor=acceptor_label(step)) if delivered
                               else T.PROOF_NO_ACCEPTOR))


# --------------------------------------------------------------------------- requests

def _case_type_buttons(emp: Employee):  # noqa: ANN202
    rows = [[(T.say(emp, C.CASE_TYPES[s].label), CaseTypeCb(slug=s))] for s in MENU_CASE_TYPES]
    return inline(rows)


@router.message(F.text.in_(T.both(T.BTN_CASE)))
async def case_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
    if emp is None:
        await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id))
        return
    await message.answer(T.say(emp, T.CASE_PICK), reply_markup=_case_type_buttons(emp))


@router.callback_query(CaseTypeCb.filter())
async def case_type_picked(query: CallbackQuery, callback_data: CaseTypeCb, state: FSMContext) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, query.from_user.id)
    ctype = C.CASE_TYPES.get(callback_data.slug)
    if emp is None or ctype is None:
        await query.answer(T.NOT_ALLOWED, show_alert=True)
        return
    await state.set_state(NewCase.body)
    await state.update_data(case_type=ctype.slug)
    text = T.CASE_ASK_BODY.format(label=ctype.label)
    if ctype.slug == "complaint":
        text += "\n\n" + T.CASE_COMPLAINT_NOTE
    await query.message.answer(T.say(emp, text))
    await query.answer()


async def _open_and_tell(message: Message, bot: Bot, telegram_user_id: int, case_type: str, body: str) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, telegram_user_id)
        if emp is None:
            return
        case, created = await C.open_case(session, emp, case_type, body)
        assignee = await session.get(Employee, case.assignee_id) if case.assignee_id else None
        if created:
            await tell_case_handler(bot, session, case)
            if assignee is None:  # nobody holds the role: HR / owner must still see it
                for hr in await _any_hr(session):
                    await notify(bot, hr, f"⚠️ {case.number} учун масъул топилмади. «📥 Иш навбати»да кўринг.")
    who = f"{assignee.full_name} ({assignee.position})" if assignee else T.CASE_UNASSIGNED
    await message.answer(T.say(emp, T.CASE_OPENED.format(
        number=case.number, label=case.title, who=who, due=fmt(case.first_response_due))))


async def _any_hr(session) -> list[Employee]:  # noqa: ANN001
    rows = (await session.execute(select(Employee).where(Employee.role.in_(("hr", "owner")),
                                                         Employee.status == "active"))).scalars().all()
    return [r for r in rows if r.telegram_user_id]


@router.message(NewCase.body, F.text)
async def case_body(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await _open_and_tell(message, bot, message.from_user.id, data.get("case_type", "other"), message.text)


@router.callback_query(OfferCaseCb.filter())
async def offered_case(query: CallbackQuery, callback_data: OfferCaseCb, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    question = data.get("last_question")
    await query.answer()
    if not question:
        await query.message.answer("Саволни қайта ёзинг ёки «📝 Мурожаат» менюсидан фойдаланинг.")
        return
    await state.update_data(last_question=None)
    await query.message.edit_reply_markup(reply_markup=None)
    await _open_and_tell(query.message, bot, query.from_user.id, callback_data.slug, question)


@router.message(F.text.in_(T.both(T.BTN_MY_CASES)))
async def my_cases(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
        if emp is None:
            await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id))
            return
        rows = await C.mine(session, emp)
    if not rows:
        await message.answer(T.say(emp, "Сизда ҳали мурожаат йўқ."))
        return
    labels = {"submitted": "юборилди", "accepted": "кўриб чиқилмоқда", "in_review": "кўриб чиқилмоқда",
              "decided": "жавоб берилди, иш очиқ", "closed": "ёпилди", "rejected": "рад этилди"}
    lines = ["📂 Мурожаатларингиз:"]
    for c in rows:
        lines.append(f"\n{c.number} — {c.title}\nҲолат: {labels.get(c.status, c.status)}")
        if c.decision:
            lines.append(f"Жавоб: {c.decision}")
    await message.answer(T.say(emp, "\n".join(lines)))


# --------------------------------------------------------------------------- profile

@router.message(F.text.in_(T.both(T.BTN_PROFILE)))
async def profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
        if emp is None:
            await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id))
            return
        manager = await session.get(Employee, emp.manager_id) if emp.manager_id else None
        mentor = await session.get(Employee, emp.mentor_id) if emp.mentor_id else None
    lang = {"uz_cyrl": "ўзбек (кирилл)", "uz_latn": "ўзбек (лотин)", "ru": "рус"}[emp.language]
    lines = [
        f"👤 {emp.full_name}", f"ID: {emp.code}", f"Бўлим: {emp.department}", f"Лавозим: {emp.position}",
        f"Раҳбар: {manager.full_name if manager else '—'}", f"Мураббий: {mentor.full_name if mentor else '—'}",
        f"Бошлаш санаси: {emp.start_date:%d.%m.%Y}" if emp.start_date else "Бошлаш санаси: —",
        f"Иш вақти: {emp.work_start_hour}:00–{emp.work_end_hour}:00", f"Тил: {lang}", "",
        "Лавозим, раҳбар ва жадвални фақат HR ўзгартиради. Хато бўлса, тузатиш сўровини юборинг.",
    ]
    text = "\n".join(lines)
    await message.answer(T.say(emp, text), reply_markup=inline([
        [(T.say(emp, "✏️ Хатони тузатиш"), CaseTypeCb(slug="data_fix"))],
        [("Ўзбек (кирилл)", PickCb(field="mylang", value="uz_cyrl")),
         ("O'zbek (lotin)", PickCb(field="mylang", value="uz_latn")),
         ("Русский", PickCb(field="mylang", value="ru"))],
    ]))


@router.callback_query(PickCb.filter(F.field == "mylang"))
async def set_language(query: CallbackQuery, callback_data: PickCb) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, query.from_user.id)
        if emp is None or callback_data.value not in ("uz_cyrl", "uz_latn", "ru"):
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        emp.language = callback_data.value
        menu = main_menu(emp, await handles_work(session, emp))
    await query.answer()
    await query.message.answer(T.say(emp, "Тил сақланди. AI жавоблари шу тилда бўлади."), reply_markup=menu)


# --------------------------------------------------------------------------- questions (catch-all)

@router.callback_query(HelpfulCb.filter())
async def helpful(query: CallbackQuery, callback_data: HelpfulCb) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, query.from_user.id)
        log = await session.get(AnswerLog, callback_data.log_id)
        if emp is not None and log is not None and log.employee_id == emp.id:
            log.was_helpful = callback_data.ok
    await query.answer("Раҳмат!")
    await query.message.edit_reply_markup(reply_markup=None)


@router.message(StateFilter(None), F.text)
async def question(message: Message, state: FSMContext, bot: Bot) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
        if emp is None:
            await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id))
            return
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        result = await answer_question(session, emp, message.text, today_local())
    buttons = []
    if result.offer_case:
        await state.update_data(last_question=message.text)
        label = "📨 HR масъулига юбориш" if result.offer_case == "question" else "📨 Мурожаат очиш"
        buttons.append([(T.say(emp, label), OfferCaseCb(slug=result.offer_case))])
    if result.outcome == "answered" and result.log_id:
        buttons.append([("👍 Тушунарли", HelpfulCb(log_id=result.log_id, ok=True)),
                        ("👎 Тушунарсиз", HelpfulCb(log_id=result.log_id, ok=False))])
    # Model text is sent as plain text (no parse mode): nothing in it becomes a link or markup.
    await message.answer(T.say(emp, result.text), reply_markup=inline(buttons) if buttons else None)
