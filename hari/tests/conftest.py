"""Each test gets a fresh in-memory database. No network, no real Telegram, no real LLM."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hari import onboarding as O
from hari.db import session as db
from hari.db.models import Employee
from hari.employees import CardInput, create_card
from hari.knowledge.loader import load_directory


@pytest.fixture(autouse=True)
async def database():
    db.configure("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield
    await db.dispose()


@pytest.fixture
def session_scope():
    return db.session_scope


async def make_staff(session, name: str, role: str = "employee", *, department: str = "Garmin сотув",
                     position: str = "Сотувчи", manager: Employee | None = None, mentor: Employee | None = None,
                     tg: int | None = None, start: date | None = None) -> Employee:
    emp, _ = await create_card(session, CardInput(
        full_name=name, department=department, position=position, role=role,
        manager_id=manager.id if manager else None, mentor_id=mentor.id if mentor else None,
        start_date=start or date(2026, 11, 2),
    ), created_by=None)
    if tg is not None:
        emp.telegram_user_id = tg
        emp.identity_verified_at = datetime(2026, 11, 1, tzinfo=timezone.utc)
        emp.status = "active"
    await session.flush()
    return emp


@pytest.fixture
def staff():
    return make_staff


DOC = """---
doc_id: {doc_id}
title: "{title}"
version: "{version}"
status: {status}
classification: {cls}
roles: "{roles}"
departments: "{depts}"
effective_from: {start}
effective_to: {end}
---
## {section}

{body}
"""


def write_doc(folder: Path, doc_id: str, body: str, *, title: str = "Ҳужжат", version: str = "1",
              status: str = "approved", cls: str = "internal", roles: str = "", depts: str = "",
              start: str = "2026-01-01", end: str = "", section: str = "Бўлим") -> Path:
    path = folder / f"{doc_id}.md"
    path.write_text(DOC.format(doc_id=doc_id, title=title, version=version, status=status, cls=cls, roles=roles,
                               depts=depts, start=start, end=end, section=section, body=body), encoding="utf-8")
    return path


@pytest.fixture
def docs(tmp_path):
    """A small knowledge base: leave rules (internal), a technician SOP (role), a draft, an expired doc."""
    write_doc(tmp_path, "LEAVE", "Таътил сўраш учун мурожаат очинг. Раҳбар иш топширишни келишади, "
              "HR тартибни текширади, қарорни ваколатли шахс қабул қилади.", title="Таътил тартиби",
              section="Таътил сўраш")
    write_doc(tmp_path, "SERVICE", "Аввал наряд рақамини очинг, кейин қисм чиқимини нарядга боғланг.",
              title="Қисмни нарядга ёзиш", version="3", cls="role", roles="техник", depts="сервис",
              section="Қисм бериш")
    write_doc(tmp_path, "REMOTE", "Масофавий иш ҳафтада икки кун мумкин.", title="Масофавий иш",
              status="draft", section="Масофавий иш")
    write_doc(tmp_path, "OLDBONUS", "Эски мукофот тартиби: ҳар ой премия.", title="Эски тартиб",
              end="2026-01-31", section="Эски қоида")
    return tmp_path


@pytest.fixture
async def loaded(docs, session_scope):
    async with session_scope() as session:
        await load_directory(session, docs)
    return docs


TODAY = date(2026, 11, 6)  # a Friday


@pytest.fixture
def today():
    return TODAY


def at_tashkent(day: date, hour: int, minute: int = 0) -> datetime:
    """A UTC moment for a Tashkent wall-clock time (UTC+5)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc) - timedelta(hours=5)


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
        if self.fail:
            from aiogram.exceptions import TelegramNetworkError
            from aiogram.methods import SendMessage

            raise TelegramNetworkError(method=SendMessage(chat_id=chat_id, text=text), message="down")
        self.sent.append((chat_id, text))


__all__ = ["O", "FakeBot", "at_tashkent", "TODAY", "write_doc", "make_staff"]
