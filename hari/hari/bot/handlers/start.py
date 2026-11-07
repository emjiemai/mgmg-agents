"""/start: binding a Telegram account through a one-time link, and the main menu."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from hari.bot import texts as T
from hari.bot.common import handles_work, staff_by_tg
from hari.bot.ui import main_menu
from hari.db.models import Employee
from hari.db.session import session_scope
from hari.employees import BindError, bind_telegram

router = Router(name="start")


async def _welcome(message: Message, emp: Employee) -> None:
    async with session_scope() as session:
        emp = await session.get(Employee, emp.id)
        manager = (await session.get(Employee, emp.manager_id)).full_name if emp.manager_id else None
        mentor = (await session.get(Employee, emp.mentor_id)).full_name if emp.mentor_id else None
        menu = main_menu(emp, await handles_work(session, emp))
    await message.answer(T.say(emp, T.welcome(emp, manager, mentor)), reply_markup=menu)


@router.message(CommandStart(deep_link=True))
async def start_with_token(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    token = (command.args or "").strip()
    try:
        async with session_scope() as session:
            emp = await bind_telegram(session, token, message.from_user.id)
    except BindError as exc:
        await message.answer(T.INVITE_TAKEN if str(exc) == "account_taken" else T.INVITE_INVALID)
        return
    await _welcome(message, emp)


@router.message(CommandStart())
async def start_plain(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
    if emp is None:
        await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id))
        return
    await _welcome(message, emp)


@router.message(Command("bekor", "cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
    await message.answer(T.say(emp, T.CANCELLED))


@router.message(F.text.in_(T.both(T.BTN_HELP)))
@router.message(Command("help", "yordam"))
async def help_(message: Message) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
    await message.answer(T.say(emp, T.HELP) if emp else T.UNKNOWN_USER.format(tg_id=message.from_user.id))
