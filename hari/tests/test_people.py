"""Employee cards, invites, identity, access (spec §4–5; A01, A02)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from hari import onboarding as O
from hari.access import can_view_employee, is_staff
from hari.db.models import Employee, Invite, OnboardingPlan
from hari.employees import BindError, CardError, CardInput, bind_telegram, by_telegram, create_card, make_invite
from hari.translit import cyrillic_to_latin, latin_to_cyrillic, normalize
from hari.worktime import now_utc


def card(**kw) -> CardInput:  # noqa: ANN003
    base = dict(full_name="Каримов Алишер Бахтиёрович", department="Garmin сотув", position="Сотувчи",
                start_date=date(2026, 11, 10))
    base.update(kw)
    return CardInput(**base)


async def test_a01_same_card_three_times_gives_one_employee_one_plan(session_scope):
    async with session_scope() as s:
        ids = set()
        for _ in range(3):
            emp, _ = await create_card(s, card(), created_by=None)
            await O.create_plan(s, emp, emp.start_date)
            ids.add(emp.id)
        assert len(ids) == 1
        assert (await s.execute(select(func.count()).select_from(Employee))).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(OnboardingPlan))).scalar_one() == 1


async def test_same_card_typed_in_latin_is_still_one_person(session_scope):
    async with session_scope() as s:
        a, created_a = await create_card(s, card(), created_by=None)
        b, created_b = await create_card(s, card(full_name="Karimov Alisher Baxtiyorovich"), created_by=None)
    assert created_a and not created_b and a.id == b.id


async def test_card_validation(session_scope):
    async with session_scope() as s:
        with pytest.raises(CardError):
            await create_card(s, card(full_name="Алишер"), created_by=None)
        with pytest.raises(CardError):
            await create_card(s, card(role="director"), created_by=None)


async def test_invite_binds_once_and_only_one_account(session_scope):
    async with session_scope() as s:
        emp, _ = await create_card(s, card(start_date=date(2020, 1, 1)), created_by=None)
        other, _ = await create_card(s, card(full_name="Тошматов Ботир"), created_by=None)
        token = await make_invite(s, emp, None)
        token_other = await make_invite(s, other, None)
    async with session_scope() as s:
        bound = await bind_telegram(s, token, 111)
        assert bound.telegram_user_id == 111 and bound.status == "active" and is_staff(bound)
    async with session_scope() as s:
        with pytest.raises(BindError):  # reused link
            await bind_telegram(s, token, 222)
    async with session_scope() as s:
        with pytest.raises(BindError, match="account_taken"):  # one Telegram account = one person
            await bind_telegram(s, token_other, 111)


async def test_new_invite_kills_old_one_and_expired_fails(session_scope):
    async with session_scope() as s:
        emp, _ = await create_card(s, card(), created_by=None)
        old = await make_invite(s, emp, None)
        new = await make_invite(s, emp, None)
    async with session_scope() as s:
        with pytest.raises(BindError):
            await bind_telegram(s, old, 5)
    async with session_scope() as s:
        inv = (await s.execute(select(Invite).where(Invite.used_at.is_(None)))).scalars().all()
        for i in inv:
            i.expires_at = now_utc() - timedelta(minutes=1)
    async with session_scope() as s:
        with pytest.raises(BindError):
            await bind_telegram(s, new, 5)


async def test_unbound_or_candidate_has_no_rights(session_scope, staff):
    async with session_scope() as s:
        emp = await staff(s, "Тест Номзод")  # card exists, no Telegram verified
        assert not is_staff(emp)
        emp.status = "candidate"
        emp.telegram_user_id = 9
        assert await by_telegram(s, 9) is not None
        assert not is_staff(emp)


async def test_a02_employee_cannot_view_another_employee(session_scope, staff):
    async with session_scope() as s:
        boss = await staff(s, "Раҳбаров Бек", "manager", tg=1)
        a = await staff(s, "Аълоев Анвар", manager=boss, tg=2)
        b = await staff(s, "Бобоев Ботир", tg=3)
        hr = await staff(s, "Кадрова Зайнаб", "hr", tg=4)
        assert can_view_employee(a, a)
        assert not can_view_employee(a, b)
        assert can_view_employee(boss, a) and not can_view_employee(boss, b)
        assert can_view_employee(hr, b)


def test_translit_both_ways():
    assert latin_to_cyrillic("o'qish ta'til shikoyat ertaga") == "ўқиш таътил шикоят эртага"
    assert normalize("Ta'til olish") == normalize("Таътил олиш")
    assert cyrillic_to_latin("Ўзбекистон ва Ғалаба, шикоят") == "Oʻzbekiston va Gʻalaba, shikoyat"
