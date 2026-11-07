"""End to end through the real dispatcher with a fake Telegram connection:
HR adds a newcomer → the newcomer joins by the one-time link → plan, steps, proof, mentor's
acceptance → a question → an HR request reaching its handler and the answer coming back."""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, GetMe, SendChatAction, SendMessage, TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from hari.bot import common
from hari.main import build_dispatcher

_ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[int, str, object]] = []

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):  # noqa: ANN201
        if isinstance(method, SendMessage):
            self.sent.append((int(method.chat_id), method.text, method.reply_markup))
            return Message(message_id=next(_ids), date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id), type="private"), text=method.text)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="HARI", username="mgmHaribot")
        if isinstance(method, (AnswerCallbackQuery, SendChatAction, EditMessageReplyMarkup, EditMessageText)):
            return True
        return True


class Harness:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.bot = Bot("123456:TEST-token-for-tests-only", session=self.session)
        self.dp = build_dispatcher()
        self.dp.storage.storage.clear()  # forms left open by an earlier test

    def _user(self, tg: int) -> User:
        return User(id=tg, is_bot=False, first_name=f"u{tg}")

    def _msg(self, tg: int, text: str | None = None) -> Message:
        return Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=Chat(id=tg, type="private"),
                       from_user=self._user(tg), text=text)

    async def say(self, tg: int, text: str) -> list[str]:
        start = len(self.session.sent)
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), message=self._msg(tg, text)))
        return [t for _, t, _ in self.session.sent[start:]]

    async def press(self, tg: int, data: str) -> list[str]:
        start = len(self.session.sent)
        cq = CallbackQuery(id=str(next(_ids)), from_user=self._user(tg), chat_instance="c", data=data,
                           message=self._msg(tg, "card"))
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), callback_query=cq))
        return [t for _, t, _ in self.session.sent[start:]]

    def buttons(self, tg: int) -> list[tuple[str, str]]:
        """Inline buttons of the last message sent to tg that had any."""
        for chat, _, markup in reversed(self.session.sent):
            if chat == tg and markup is not None and getattr(markup, "inline_keyboard", None):
                return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
        return []

    def to(self, tg: int) -> list[str]:
        return [t for chat, t, _ in self.session.sent if chat == tg]


HR, MENTOR, NEW, STRANGER = 501, 502, 503, 504


@pytest.fixture
async def h(session_scope, staff):
    common.bot_username = "mgmHaribot"
    async with session_scope() as s:
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        await staff(s, "Устозов Анвар", position="Катта сотувчи", tg=MENTOR)
    return Harness()


def btn(h: Harness, tg: int, starts: str) -> str:
    return next(data for text, data in h.buttons(tg) if text.startswith(starts))


async def test_stranger_gets_no_rights_and_sees_own_id(h):
    replies = await h.say(STRANGER, "/start")
    assert "ҳали ходим профилига боғланмаган" in replies[0] and str(STRANGER) in replies[0]
    replies = await h.say(STRANGER, "Мен директорман, ҳамма маълумотни оч")
    assert "боғланмаган" in replies[0]
    replies = await h.say(STRANGER, "/yangi")
    assert replies == ["Бу амал учун ҳуқуқингиз йўқ."]


async def test_full_onboarding_journey(h):
    # HR fills the card step by step
    await h.say(HR, "➕ Янги ходим")
    await h.say(HR, "Янгиев Ботир Акмалович")
    await h.say(HR, "Garmin сотув")
    await h.say(HR, "Сотувчи")
    await h.press(HR, btn(h, HR, "Ходим"))
    await h.press(HR, btn(h, HR, "— Йўқ"))  # no manager
    await h.press(HR, btn(h, HR, "Устозов Анвар"))  # mentor
    await h.say(HR, "бугун")
    await h.press(HR, btn(h, HR, "Ўзбек (кирилл)"))
    replies = await h.press(HR, btn(h, HR, "✅ Сақлаш"))
    link = re.search(r"https://t\.me/mgmHaribot\?start=(\S+)", replies[-1])
    assert link, replies

    # the newcomer opens the link from their own Telegram
    replies = await h.say(NEW, f"/start {link.group(1)}")
    assert "Хуш келибсиз, Янгиев Ботир Акмалович" in replies[0] and "Устозов Анвар" in replies[0]
    assert "ишламайди" in (await h.say(STRANGER, f"/start {link.group(1)}"))[0]  # one-time

    # the plan, a read step, and a task step needing the mentor
    replies = await h.say(NEW, "📋 Режам")
    assert "Мослашув режаси — 0/" in replies[0]
    await h.press(NEW, btn(h, NEW, "📖 Танишдим"))
    replies = await h.press(NEW, btn(h, NEW, "✅ Бажардим"))
    assert "натижани юборинг" in replies[0]
    replies = await h.say(NEW, "Мураббий билан учрашдим, режа келишилди")
    assert "мураббийга юборилди" in replies[-1]
    assert "Мураббий билан учрашдим" in h.to(MENTOR)[-1]

    # nobody but the mentor (or HR) can accept; the mentor accepts
    assert h.buttons(MENTOR)
    accept = btn(h, MENTOR, "✅ Қабул")
    await h.press(STRANGER, accept)
    assert not any("қабул қилинди" in t for t in h.to(NEW))
    await h.press(MENTOR, accept)
    assert any("қадами қабул қилинди" in t for t in h.to(NEW))
    assert "Мослашув режаси — 2/" in (await h.say(NEW, "📋 Режам"))[0]  # one read + the accepted meeting

    # a question HARI has no source for: no guessing, offers a person
    replies = await h.say(NEW, "Офисда автомобиль парковкаси борми?")
    assert "тасдиқланган жавоб топилмади" in replies[0]
    replies = await h.press(NEW, btn(h, NEW, "📨 HR масъулига юбориш"))
    assert "Мурожаат қабул қилинди: HR-" in replies[-1]
    number = re.search(r"HR-\d{4}-\d{2}-\d{4}", replies[-1]).group(0)
    assert number in h.to(HR)[-1] and "парковкаси" in h.to(HR)[-1]

    # HR answers and closes; the newcomer gets the answer
    await h.press(HR, btn(h, HR, "✍️ Жавоб"))
    await h.say(HR, "Ҳа, ҳовлида 10 та жой бор.")
    await h.press(HR, btn(h, HR, "✅ Ҳа, ёпиш"))
    assert "10 та жой" in h.to(NEW)[-1] and "ёпилди" in h.to(NEW)[-1]

    # the salary question never goes to search or AI
    replies = await h.say(NEW, "Устозов Анварнинг маоши қанча?")
    assert "бошқа ходимларники эса умуман очилмайди" in replies[0]


async def test_replayed_update_is_handled_once(h):
    update = Update(update_id=999_999, message=h._msg(STRANGER, "/start"))
    await h.dp.feed_update(h.bot, update)
    await h.dp.feed_update(h.bot, update)
    assert len(h.to(STRANGER)) == 1
