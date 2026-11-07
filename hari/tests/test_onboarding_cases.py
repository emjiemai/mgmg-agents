"""Onboarding acceptance by humans (spec §9) and HR request routing (spec §18, §32; A13)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from hari import cases as C
from hari import onboarding as O
from hari.worktime import add_work_hours, work_days_between
from tests.conftest import at_tashkent


@pytest.fixture
async def team(session_scope, staff):
    async with session_scope() as s:
        owner = await staff(s, "Эгаев Улуғ", "owner", department="Бошқарув", position="Эгаси", tg=10)
        hr = await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=11)
        boss = await staff(s, "Раҳбаров Бек", "manager", position="Бўлим раҳбари", tg=12)
        mentor = await staff(s, "Устозов Анвар", tg=13)
        new = await staff(s, "Янгиев Ботир", manager=boss, mentor=mentor, tg=14, start=date(2026, 11, 2))
        other = await staff(s, "Бегонаев Сардор", tg=15)
        acct = await staff(s, "Ҳисобчиева Мадина", "accountant", department="Бухгалтерия", position="Бухгалтер", tg=16)
        plan = await O.create_plan(s, new, date(2026, 11, 2))
        return {"owner": owner, "hr": hr, "boss": boss, "mentor": mentor, "new": new, "other": other,
                "acct": acct, "plan_id": plan.id}


def step(plan, title_part):  # noqa: ANN001
    return next(s for s in plan.steps if title_part in s.title)


async def test_steps_become_due_by_work_days(team, session_scope):
    async with session_scope() as s:
        plan = await O.get_plan(s, team["new"].id)
        first_day = O.due_open_steps(team["new"], plan, date(2026, 11, 2))
        assert {x.phase for x in first_day} == {"day1"}
        later = O.due_open_steps(team["new"], plan, date(2026, 11, 6))  # 4 work days later
        assert any(x.phase == "d2_7" for x in later) and not any(x.phase == "d8_15" for x in later)


async def test_read_step_is_closed_by_the_employee_only(team, session_scope):
    async with session_scope() as s:
        plan = await O.get_plan(s, team["new"].id)
        read = step(plan, "миссияси")
        with pytest.raises(O.StepError):
            await O.acknowledge(s, team["other"], read.id)
        await O.acknowledge(s, team["new"], read.id)
        assert read.status == "accepted"


async def test_task_needs_proof_and_named_human_acceptance(team, session_scope):
    async with session_scope() as s:
        plan = await O.get_plan(s, team["new"].id)
        task = step(plan, "намунали вазифа")
        with pytest.raises(O.StepError):
            await O.submit(s, team["new"], task.id, "  ", None)  # "бажардим" without proof
        await O.submit(s, team["new"], task.id, "Ҳисоботни тайёрладим", None)
        assert task.status == "submitted"  # submitted is not accepted
        for intruder in (team["new"], team["other"], team["boss"]):  # self, stranger, not the mentor
            with pytest.raises(O.StepError):
                await O.decide(s, intruder, task.id, accept=True)
        await O.decide(s, team["mentor"], task.id, accept=False, reason="Жадвални тўлдиринг")
        assert task.status == "returned" and task.return_reason == "Жадвални тўлдиринг"
        await O.submit(s, team["new"], task.id, "Тузатдим", "photo:abc")
        await O.decide(s, team["hr"], task.id, accept=True)  # HR is the backup acceptor
        assert task.status == "accepted"
        acceptor = await O.acceptor_for(s, task, team["new"])
        assert acceptor.id == team["mentor"].id


async def test_plan_finishes_when_every_step_is_accepted(team, session_scope):
    async with session_scope() as s:
        plan = await O.get_plan(s, team["new"].id)
        for st in plan.steps:
            if st.kind == "read":
                await O.acknowledge(s, team["new"], st.id)
            else:
                await O.submit(s, team["new"], st.id, "бажарилди", None)
                await O.decide(s, team["hr"], st.id, accept=True)
        plan = await O.get_plan(s, team["new"].id)
        assert plan.status == "done" and O.progress(plan) == (len(plan.steps), len(plan.steps))


async def test_routing_leave_to_manager_pay_to_accountant(team, session_scope):
    async with session_scope() as s:
        leave, _ = await C.open_case(s, team["new"], "leave", "15–20 ноябрь таътил")
        pay, _ = await C.open_case(s, team["new"], "pay_question", "ҳисоб варағимда бир қатор тушунарсиз")
        assert leave.assignee_id == team["boss"].id
        assert pay.assignee_id == team["acct"].id and pay.privacy == "sensitive"
        assert leave.number.startswith("HR-") and leave.number != pay.number


async def test_complaint_never_goes_to_the_person_it_is_about(team, session_scope):
    async with session_scope() as s:
        about_boss, _ = await C.open_case(s, team["new"], "complaint", "раҳбар қўпол гапирди", about=team["boss"])
        assert about_boss.assignee_id == team["hr"].id
        about_hr, _ = await C.open_case(s, team["new"], "complaint", "HR жавоб бермаяпти", about=team["hr"])
        assert about_hr.assignee_id == team["owner"].id  # A13: independent route
        with pytest.raises(C.CaseError):
            await C.answer(s, team["hr"], about_hr, "ёпдим", close=True)


async def test_case_is_opened_once_and_states_are_separate(team, session_scope):
    async with session_scope() as s:
        a, created_a = await C.open_case(s, team["new"], "certificate", "иш жойидан маълумотнома")
        b, created_b = await C.open_case(s, team["new"], "certificate", "иш жойидан маълумотнома")
        assert created_a and not created_b and a.id == b.id
        with pytest.raises(C.CaseError):
            await C.take(s, team["other"], a)
        await C.take(s, team["hr"], a)
        assert a.status == "accepted" and a.first_response_at is not None
        await C.answer(s, team["hr"], a, "Эртага тайёр бўлади", close=False)
        assert a.status == "decided" and a.closed_at is None  # answered is not resolved
        await C.answer(s, team["hr"], a, "Берилди", close=True)
        assert a.status == "closed"
        with pytest.raises(C.CaseError):
            await C.answer(s, team["hr"], a, "яна", close=True)


async def test_handler_queue_hides_sensitive_cases_of_others(team, session_scope):
    async with session_scope() as s:
        await C.open_case(s, team["new"], "pay_question", "ҳисоб саволи")  # → accountant
        await C.open_case(s, team["new"], "certificate", "маълумотнома")  # → HR
        hr_queue = {c.type for c in await C.open_for_handler(s, team["hr"])}
        acct_queue = {c.type for c in await C.open_for_handler(s, team["acct"])}
        assert hr_queue == {"certificate"} and acct_queue == {"pay_question"}


def test_work_hours_and_days(team):
    friday_17 = at_tashkent(date(2026, 11, 6), 17)
    due = add_work_hours(team["new"], friday_17, 4)  # 1 h Friday + 3 h Saturday
    assert due == at_tashkent(date(2026, 11, 7), 12)
    saturday_17 = at_tashkent(date(2026, 11, 7), 17)
    assert add_work_hours(team["new"], saturday_17, 4) == at_tashkent(date(2026, 11, 9), 12)  # Sunday skipped
    assert work_days_between(team["new"], date(2026, 11, 7), date(2026, 11, 9)) == 1
    assert work_days_between(team["new"], date(2026, 11, 9), date(2026, 11, 2)) == -1
    assert timedelta(0) < due - friday_17
