"""Keyboards, callback data and the helper that messages other people safely."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from hari.access import HR_ROLES
from hari.bot import texts as T
from hari.db.models import Employee


class StepCb(CallbackData, prefix="st"):
    action: str  # ack | done | ok | back
    step_id: str


class DocCb(CallbackData, prefix="doc"):
    doc_id: str


class CaseTypeCb(CallbackData, prefix="ct"):
    slug: str


class OfferCaseCb(CallbackData, prefix="oc"):
    slug: str


class CaseActCb(CallbackData, prefix="ca"):
    action: str  # take | answer | close_yes | close_no
    number: str


class HelpfulCb(CallbackData, prefix="hf"):
    log_id: str
    ok: bool


class PickCb(CallbackData, prefix="pk"):
    field: str  # role | manager | mentor | lang | confirm | mylang
    value: str


def main_menu(emp: Employee, handles_work: bool) -> ReplyKeyboardMarkup:
    rows = [[T.BTN_PLAN, T.BTN_CASE], [T.BTN_MY_CASES, T.BTN_PROFILE]]
    extra = []
    if handles_work:
        extra.append(T.BTN_QUEUE)
    if emp.role in HR_ROLES:
        extra += [T.BTN_NEW_EMP, T.BTN_STAFF]
    if extra:
        rows.append(extra[:2])
        if len(extra) > 2:
            rows.append(extra[2:])
    rows.append([T.BTN_HELP])
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T.say(emp, label)) for label in row] for row in rows],
        resize_keyboard=True,
    )


def inline(rows: list[list[tuple[str, CallbackData]]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*[_button(text, cb) for text, cb in row])
    return builder.as_markup()


def _button(text: str, cb: CallbackData):  # noqa: ANN202
    from aiogram.types import InlineKeyboardButton

    return InlineKeyboardButton(text=text[:60], callback_data=cb.pack())


async def notify(bot: Bot, person: Employee | None, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    """Message a staff member; never crash a flow because one person blocked the bot."""
    if person is None or not person.telegram_user_id:
        return False
    try:
        await bot.send_message(person.telegram_user_id, T.say(person, text), reply_markup=markup)
        return True
    except TelegramAPIError as exc:
        logger.warning("Message to {} not delivered: {}", person.code, type(exc).__name__)
        return False


async def send_proof(bot: Bot, person: Employee, proof_file_id: str | None) -> None:
    if not proof_file_id or not person.telegram_user_id:
        return
    kind, _, file_id = proof_file_id.partition(":")
    try:
        if kind == "photo":
            await bot.send_photo(person.telegram_user_id, file_id)
        else:
            await bot.send_document(person.telegram_user_id, file_id)
    except TelegramAPIError as exc:
        logger.warning("Proof file to {} not delivered: {}", person.code, type(exc).__name__)
