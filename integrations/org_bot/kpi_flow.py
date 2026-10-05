"""KPI in OPS Manager Bot — goals, results, the Director's ratings, the month's table.

The scoring rules are in ``kpi_score.py``; this module reads the data, talks
to people and sends the messages.

Commands
  Director:  /maqsad    set a monthly goal for someone (a number to reach)
             /maqsadlar this period's goals — enter a result, cancel a goal
             /kpi       everyone's KPI for the period, best first
             /baho      the 1–5 rating cards again
  Employee:  /kpi       their own KPI card
             /natija    enter the result of their own goal

The month (run by agents/task-tracker --monthly):
  1st   the Director gets last month's table so far and one rating card per
        person (4 criteria × 1–5 buttons); everyone with a goal whose result
        isn't in yet is asked for it.
  after the last card is rated, or on the 5th at the latest, the final table
        goes to the Director and HR — once.

Typed answers are caught before reports and task routing: a goal's text right
after /maqsad (the Director), and a bare number right after "enter the
result" (anyone). /bekor cancels either.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import today_local
from integrations.org_bot import answer_check, kpi_score, store
from integrations.org_bot.kpi_score import RATING_BY_LETTER, RATINGS
from integrations.org_bot.permissions import Field, is_cancel
from integrations.org_bot.roles import DIRECTOR_ROLE, ROLE_LABELS
from integrations.telegram.bot import TelegramBot, TelegramError, escape

AGENT = "kpi"
log = setup_logging(AGENT)

GOAL_COMMANDS = ("/maqsad", "/мақсад")
GOALS_COMMANDS = ("/maqsadlar", "/мақсадлар")
RESULT_COMMANDS = ("/natija", "/натижа")
KPI_COMMANDS = ("/kpi", "/кпи")
RATE_COMMANDS = ("/baho", "/баҳо")
HR_ROLE = "hr"
CANCEL_HINT = "\n\n<i>Бекор қилиш: /bekor</i>"

GOAL_FIELD = Field(
    key="goal",
    label="Ойлик мақсад",
    question="Мақсадни рақам билан ёзинг: масалан «20 та янги шартнома» ёки «сотув 500 млн сўм».",
    rule=(
        "A measurable monthly goal for one employee: what must be achieved, WITH a number to reach "
        "(e.g. '20 new contracts', 'sales of 500 million so'm', '15 service visits', '30 posts'). "
        "Reject greetings, questions, and goals with no number in them."
    ),
)
GOAL_CONTEXT = "a monthly KPI goal the Director sets for an employee"


# ------------------------------------------------------------------ sending


def _bot(run_id: uuid.UUID | None) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def _send(chat_id: int, text: str, run_id: uuid.UUID | None, keyboard: dict[str, Any] | None = None) -> list[int]:
    async with _bot(run_id) as bot:
        return await bot.send_message(text, chat_id=str(chat_id), reply_markup=keyboard)


async def _send_table(chat_id: int, month: date, people: list[kpi_score.EmployeeMonth], final: bool,
                      run_id: uuid.UUID | None) -> None:
    """Everyone's KPI as a PDF table (2026-10-05) — the text table if the PDF can't be drawn."""
    try:
        from integrations.reports import render as reports

        document = reports.pdf("kpi.html", **kpi_score.table_view(month, people, final))
    except Exception as exc:  # noqa: BLE001 — never lose the table over its layout
        log.error("KPI PDF failed, sending text: {}", exc)
        await _send(chat_id, kpi_score.table_text(month, people, final), run_id)
        return
    async with _bot(run_id) as bot:
        await bot.send_file(document, f"kpi-{month:%Y-%m}.pdf", str(chat_id),
                            kpi_score.table_caption(month, people, final))


async def _edit(callback: dict[str, Any], text: str, keyboard: dict[str, Any] | None, run_id: uuid.UUID) -> None:
    message = callback.get("message") or {}
    async with _bot(run_id) as bot:
        await bot._edit_message(  # noqa: SLF001 — same helper the other flows use
            chat_id=str((message.get("chat") or {}).get("id")),
            message_id=message.get("message_id"),
            text=text,
            reply_markup=keyboard or {"inline_keyboard": []},
        )


async def _answer(callback: dict[str, Any], text: str) -> None:
    async with _bot(None) as bot:
        await bot._answer_callback(callback.get("id", ""), text)  # noqa: SLF001


# --------------------------------------------------------------------- data


async def _people() -> list[dict[str, Any]]:
    """Everyone scored: active employees except the Director."""
    return [e for e in await store.list_active_employees() if e["role"] != DIRECTOR_ROLE]


async def load_month(month: date, today: date | None = None, with_attendance: bool = True) -> list[kpi_score.EmployeeMonth]:
    """Score ``month`` (up to today, if it is still running)."""
    today = today or today_local()
    start, end = month, min(kpi_score.month_end(month), today)
    employees = await _people()
    rows = await store.kpi_month_rows(start, end)
    attendance = await _attendance(employees, start, end) if with_attendance else None
    return kpi_score.build(
        employees,
        rows["reports"],
        rows["tasks"],
        {str(r["employee_id"]): int(r["n"]) for r in rows["done"]},
        await store.goals_for_months([month]),
        await store.ratings_for_month(month),
        start,
        end,
        attendance=attendance,
        role_labels=ROLE_LABELS,
        lead_rows=rows.get("leads"),
    )


async def _attendance(employees: list[dict[str, Any]], start: date, end: date) -> dict[str, kpi_score.Attendance] | None:
    """Verifix days per employee, matched by name; None when Verifix isn't connected or fails."""
    if not settings.verifix_configured:
        return None
    from integrations.verifix import attendance  # local: only when Verifix is set up

    try:
        recs = await attendance.load(start, end, run_id=None, agent=AGENT)
    except Exception as exc:  # noqa: BLE001 — KPI goes out without attendance rather than not at all
        log.warning("Verifix unavailable for the KPI: {}", exc)
        return None
    verifix_names = {r.employee_id: r.name for r in recs}
    matched = kpi_score.match_attendance(
        {str(e["id"]): (e.get("full_name") or e["display_name"]) for e in employees}, verifix_names
    )
    result = {}
    for eid, vid in matched.items():
        days = [r for r in recs if r.employee_id == vid and r.working and r.status != "before_start"]
        result[eid] = kpi_score.Attendance(
            working=sum(1 for r in days if r.status != "excused"),
            late=sum(1 for r in days if r.status == "late"),
            absent=sum(1 for r in days if r.status == "absent"),
        )
    return result


def _is_director(employee: dict[str, Any]) -> bool:
    return employee.get("role") == DIRECTOR_ROLE


# ----------------------------------------------------------------- messages


async def handle_message(employee: dict[str, Any], message: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """Handle one message if it belongs to the KPI flow; None to fall through."""
    text = (message.get("text") or "").strip()
    if not text:
        return None
    user_id = employee["telegram_user_id"]
    command = text.split()[0].lower().split("@")[0]

    # 1. A result someone was asked to type.
    awaiting = await store.goal_awaiting_actual(user_id)
    if awaiting is not None and not (command.startswith("/") and not is_cancel(text)):
        return await _take_actual(awaiting, employee, text, run_id)

    # 2. The Director's goal text after /maqsad.
    if _is_director(employee):
        draft = await store.goal_draft(user_id)
        if draft is not None and not (command.startswith("/") and not is_cancel(text)):
            return await _take_goal(draft, text, run_id, user_id)

    if command in KPI_COMMANDS:
        return await _show_kpi(employee, run_id)
    if command in RESULT_COMMANDS:
        return await _ask_which_result(employee, run_id)
    if _is_director(employee):
        if command in GOAL_COMMANDS:
            return await _pick_employee(user_id, run_id)
        if command in GOALS_COMMANDS:
            return await _list_goals(user_id, run_id)
        if command in RATE_COMMANDS:
            month = kpi_score.score_period(today_local())
            sent = await send_rating_cards(user_id, month, run_id)
            return "kpi_rating_cards" if sent else "kpi_nobody_to_rate"
    return None


async def _pick_employee(director_id: int, run_id: uuid.UUID) -> str:
    people = sorted(await _people(), key=lambda e: (e.get("full_name") or e["display_name"]).lower())
    if not people:
        await _send(director_id, "Рўйхатдан ўтган ходим йўқ.", run_id)
        return "kpi_no_employees"
    month = kpi_score.goal_month(today_local())
    keyboard = {
        "inline_keyboard": [
            [{"text": f"👤 {(e.get('full_name') or e['display_name'])[:30]} ({ROLE_LABELS.get(e['role'], e['role'])})",
              "callback_data": f"kg:{e['id']}"}]
            for e in people
        ]
    }
    await _send(director_id, f"🎯 <b>{kpi_score.month_title(month)}</b> учун кимга мақсад қўясиз?", run_id, keyboard)
    return "kpi_pick_employee"


async def _take_goal(draft: dict[str, Any], text: str, run_id: uuid.UUID, director_id: int) -> str:
    if is_cancel(text):
        await store.cancel_goal_drafts(director_id)
        await _send(director_id, "❌ Мақсад бекор қилинди.", run_id)
        return "kpi_goal_cancelled"
    ok, follow_up, value = await answer_check.check_answer(GOAL_FIELD, text, run_id, agent=AGENT, context=GOAL_CONTEXT)
    target = kpi_score.parse_number(value) if ok else None
    if target is None or target <= 0:
        question = follow_up if not ok and follow_up else GOAL_FIELD.question
        await _send(director_id, f"🔁 {escape(question)}{CANCEL_HINT}", run_id)
        return "kpi_goal_reasked"
    goal = await store.activate_goal(str(draft["id"]), value, target)
    month = kpi_score.month_title(goal["month"])
    await _send(
        director_id,
        f"✅ Мақсад қўйилди: <b>{escape(goal['name'])}</b> — «{escape(value)}» ({month}).\n"
        f"Ўлчов: {kpi_score.fmt_number(target)}. Ой охирида натижа сўралади.",
        run_id,
        {"inline_keyboard": [
            [{"text": "➕ Шу ходимга яна мақсад", "callback_data": f"kg:{goal['employee_id']}"}],
            [{"text": "👥 Бошқа ходим", "callback_data": "kgl:"}],
        ]},
    )
    try:
        await _send(
            goal["employee_telegram_user_id"],
            f"🎯 <b>{month}</b> учун янги мақсадингиз: «{escape(value)}».\n"
            "Натижани истаган пайт /natija орқали киритинг; ой охирида бот ўзи сўрайди.",
            run_id,
        )
    except TelegramError as exc:
        log.warning("Could not tell {} about the goal: {}", goal["name"], exc)
    return "kpi_goal_set"


async def _take_actual(goal: dict[str, Any], employee: dict[str, Any], text: str, run_id: uuid.UUID) -> str:
    user_id = employee["telegram_user_id"]
    if is_cancel(text):
        await store.clear_goal_awaiting(user_id)
        await _send(user_id, "❌ Бекор қилинди.", run_id)
        return "kpi_actual_cancelled"
    actual = kpi_score.only_number(text)
    if actual is None:
        await _send(user_id, f"🔢 Фақат рақам ёзинг (масалан <b>17</b>).{CANCEL_HINT}", run_id)
        return "kpi_actual_reasked"
    who = (employee.get("full_name") or employee["display_name"]).strip()
    goal = await store.set_goal_actual(str(goal["id"]), actual, who)
    share = kpi_score.Goal(goal["title"], float(goal["target"]), actual).done_share
    await _send(
        user_id,
        f"✅ Қайд этилди: «{escape(goal['title'])}» — {kpi_score.fmt_number(actual)} / "
        f"{kpi_score.fmt_number(float(goal['target']))} ({round(100 * share)}%).",
        run_id,
    )
    return "kpi_actual_saved"


async def _ask_which_result(employee: dict[str, Any], run_id: uuid.UUID) -> str:
    today = today_local()
    months = sorted({kpi_score.score_period(today), kpi_score.month_start(today)})
    goals = await store.goals_for_months(months, str(employee["id"]))
    if not goals:
        await _send(employee["telegram_user_id"], "🎯 Сизга ҳозирча мақсад қўйилмаган.", run_id)
        return "kpi_no_goals"
    keyboard = {"inline_keyboard": [
        [{"text": f"✏️ {g['title'][:40]} ({kpi_score.fmt_number(_f(g['actual']))}/{kpi_score.fmt_number(_f(g['target']))})",
          "callback_data": f"ka:{g['id']}"}]
        for g in goals
    ]}
    await _send(employee["telegram_user_id"], "🎯 Қайси мақсаднинг натижасини киритасиз?", run_id, keyboard)
    return "kpi_pick_goal"


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


async def _list_goals(director_id: int, run_id: uuid.UUID) -> str:
    today = today_local()
    months = sorted({kpi_score.score_period(today), kpi_score.month_start(today), kpi_score.goal_month(today)})
    goals = await store.goals_for_months(months)
    if not goals:
        await _send(director_id, "🎯 Мақсадлар йўқ. Қўйиш: /maqsad", run_id)
        return "kpi_no_goals"
    lines, rows = ["🎯 <b>Мақсадлар</b>", ""], []
    for g in goals[:30]:
        actual = _f(g["actual"])
        state = "натижа йўқ" if actual is None else f"{kpi_score.fmt_number(actual)} / {kpi_score.fmt_number(_f(g['target']))}"
        lines.append(f"• {kpi_score.month_title(g['month'])} · <b>{escape(g['name'])}</b> — {escape(g['title'])}: {state}")
        rows.append([
            {"text": f"✏️ {g['name'].split()[0][:14]}: {g['title'][:22]}", "callback_data": f"ka:{g['id']}"},
            {"text": "🗑", "callback_data": f"kx:{g['id']}"},
        ])
    lines += ["", "<i>✏️ — натижани киритиш · 🗑 — мақсадни бекор қилиш</i>"]
    await _send(director_id, "\n".join(lines), run_id, {"inline_keyboard": rows})
    return "kpi_goals_listed"


async def _show_kpi(employee: dict[str, Any], run_id: uuid.UUID) -> str:
    month = kpi_score.score_period(today_local())
    people = await load_month(month)
    if _is_director(employee):
        period = await store.kpi_period(month)
        final = bool(period and period.get("final_sent_at"))
        await _send_table(employee["telegram_user_id"], month, people, final, run_id)
        return "kpi_table"
    me = next((p for p in people if p.employee_id == str(employee["id"])), None)
    if me is None:
        await _send(employee["telegram_user_id"], "KPI маълумоти ҳали йўқ.", run_id)
        return "kpi_empty"
    await _send(employee["telegram_user_id"], kpi_score.card_text(month, me), run_id)
    return "kpi_card"


# ------------------------------------------------------------------ buttons


async def handle_callback(prefix: str, rest: str, callback: dict[str, Any], run_id: uuid.UUID) -> str | None:
    """The flow's buttons: kg (goal for), kgl (pick again), ka (enter result), kx (cancel goal), kr (rating)."""
    if prefix not in ("kg", "kgl", "ka", "kx", "kr"):
        return None
    clicker_id = (callback.get("from") or {}).get("id")
    clicker = await store.get_employee_by_telegram_id(clicker_id) if clicker_id else None
    if clicker is None or clicker.get("status") != "active":
        await _answer(callback, "Рухсат йўқ")
        return "kpi_unauthorized"
    director = _is_director(clicker)

    if prefix == "ka":
        goal = await store.get_goal(rest)
        if goal is None or goal["status"] != "active" or not (director or str(goal["employee_id"]) == str(clicker["id"])):
            await _answer(callback, "Мақсад топилмади")
            return "kpi_goal_missing"
        await store.await_goal_actual(rest, clicker_id)
        await _answer(callback, "✏️")
        await _send(
            clicker_id,
            f"🎯 «{escape(goal['title'])}» — мақсад {kpi_score.fmt_number(_f(goal['target']))}.\n"
            f"Қанча бажарилди? Фақат рақам ёзинг.{CANCEL_HINT}",
            run_id,
        )
        return "kpi_actual_asked"

    if not director:
        await _answer(callback, "Фақат директор учун")
        return "kpi_unauthorized"

    if prefix == "kgl":
        await _answer(callback, "👥")
        return await _pick_employee(clicker_id, run_id)
    if prefix == "kg":
        person = await store.get_employee(rest)
        if person is None or person["status"] != "active":
            await _answer(callback, "Ходим топилмади")
            return "kpi_employee_missing"
        month = kpi_score.goal_month(today_local())
        await store.start_goal_draft(rest, month, clicker_id)
        await _answer(callback, "🎯")
        name = (person.get("full_name") or person["display_name"]).strip()
        await _send(
            clicker_id,
            f"🎯 <b>{escape(name)}</b> — {kpi_score.month_title(month)} мақсади.\n{GOAL_FIELD.question}{CANCEL_HINT}",
            run_id,
        )
        return "kpi_goal_started"
    if prefix == "kx":
        goal = await store.cancel_goal(rest)
        await _answer(callback, "🗑")
        if goal is not None:
            await _send(clicker_id, f"🗑 Бекор қилинди: <b>{escape(goal['name'])}</b> — «{escape(goal['title'] or '')}».", run_id)
        return "kpi_goal_removed"
    return await _rate(rest, callback, clicker_id, run_id)


async def _rate(rest: str, callback: dict[str, Any], director_id: int, run_id: uuid.UUID) -> str:
    rating_id, _, pick = rest.rpartition(":")
    criterion = RATING_BY_LETTER.get(pick[:1])
    if not rating_id or criterion is None or pick[1:] not in {"1", "2", "3", "4", "5"}:
        await _answer(callback, "Номаълум амал")
        return "kpi_bad_rating"
    row = await store.set_rating(rating_id, criterion, int(pick[1:]), director_id)
    if row is None:
        await _answer(callback, "Топилмади")
        return "kpi_rating_missing"
    await _answer(callback, f"{RATINGS[criterion][0]} {pick[1:]}")
    person = await store.get_employee(str(row["employee_id"]))
    ratings = {k: row[k] for k in RATINGS if row.get(k)}
    await _edit(callback, rating_text(row["month"], person, ratings), kpi_score.rating_keyboard(rating_id, ratings), run_id)
    if all(ratings.get(k) for k in RATINGS):
        rows = await store.ratings_for_month(row["month"])
        if rows and all(all(r.get(k) for k in RATINGS) for r in rows):
            await send_final(row["month"], run_id)
    return "kpi_rated"


def rating_text(month: date, person: dict[str, Any] | None, ratings: dict[str, int]) -> str:
    """The rating card for one person (the name, their role, the four marks)."""
    return kpi_score.rating_text(
        month,
        kpi_score.EmployeeMonth(
            employee_id=str((person or {}).get("id")),
            name=((person or {}).get("full_name") or (person or {}).get("display_name") or "—").strip(),
            role_label=ROLE_LABELS.get((person or {}).get("role"), ""),
            ratings=ratings,
        ),
    )


# ---------------------------------------------------------------- the month


async def send_rating_cards(director_id: int, month: date, run_id: uuid.UUID | None) -> int:
    """One 1–5 card per person to the Director; returns how many."""
    people = sorted(await _people(), key=lambda e: (e.get("full_name") or e["display_name"]).lower())
    for person in people:
        row = await store.ensure_rating(str(person["id"]), month)
        ratings = {k: row[k] for k in RATINGS if row.get(k)}
        await _send(director_id, rating_text(month, person, ratings), run_id, kpi_score.rating_keyboard(str(row["id"]), ratings))
    return len(people)


async def open_month(month: date, run_id: uuid.UUID) -> None:
    """The 1st: the table so far + rating cards to the Director; results asked for."""
    if not await store.mark_kpi_period(month, "ratings_requested_at"):
        log.info("KPI for {} already opened — not sending again", month)
        return
    people = await load_month(month)
    directors = await store.active_employees_by_role(DIRECTOR_ROLE)
    for director in directors:
        try:
            await _send_table(director["telegram_user_id"], month, people, False, run_id)
            await _send(
                director["telegram_user_id"],
                f"⭐ <b>{kpi_score.month_title(month)}</b>: ҳар бир ходимни 4 мезон бўйича 1–5 баҳоланг "
                "(5 — аъло). Ҳаммаси баҳолангач, якуний KPI сизга ва HR'га юборилади "
                "(баҳоланмаса ҳам — ойнинг 5-кунида).",
                run_id,
            )
            await send_rating_cards(director["telegram_user_id"], month, run_id)
        except TelegramError as exc:
            log.error("Could not send the KPI rating cards to {}: {}", director["display_name"], exc)
    for goal in await store.goals_for_months([month]):
        if goal["actual"] is not None:
            continue
        try:
            await _send(
                goal["employee_telegram_user_id"],
                f"🎯 {kpi_score.month_title(month)} мақсадингиз: «{escape(goal['title'])}» "
                f"(мақсад {kpi_score.fmt_number(_f(goal['target']))}). Натижани киритинг:",
                run_id,
                {"inline_keyboard": [[{"text": "✏️ Натижани киритиш", "callback_data": f"ka:{goal['id']}"}]]},
            )
        except TelegramError as exc:
            log.warning("Could not ask {} for the goal result: {}", goal["name"], exc)
    log.info("KPI {} opened: {} person(s), {} director(s)", month, len(people), len(directors))


async def send_final(month: date, run_id: uuid.UUID | None) -> bool:
    """The final table to the Director(s) and HR — once per month."""
    if not await store.mark_kpi_period(month, "final_sent_at"):
        return False
    people = await load_month(month)
    recipients = {
        e["telegram_user_id"] for role in (DIRECTOR_ROLE, HR_ROLE) for e in await store.active_employees_by_role(role)
    }
    for telegram_user_id in recipients:
        try:
            await _send_table(telegram_user_id, month, people, True, run_id)
        except TelegramError as exc:
            log.error("Could not send the final KPI to {}: {}", telegram_user_id, exc)
    log.info("Final KPI {} sent to {} person(s)", month, len(recipients))
    return True


async def describe(month: date) -> str:
    """Plain-text KPI data for OPS Manager Bot's answers."""
    people = await load_month(month)
    lines = [
        f"KPI SCORES for {month:%Y-%m} (0-100, weighted: results/OKR 30, tasks 20, Director rating 20, "
        "volume 10, process 10, commitment 10; parts with no data are left out):"
    ]
    for p in people:
        if p.total is None:
            continue
        parts = ", ".join(f"{k} {round(v)}" for k, v in p.parts.items() if v is not None)
        goals = "; ".join(
            f"goal '{g.title}' {kpi_score.fmt_number(g.actual)}/{kpi_score.fmt_number(g.target)}" for g in p.goals
        )
        lines.append(f"- {p.name} ({p.role_label}): {round(p.total)} [{parts}]" + (f" {goals}" if goals else ""))
    return "\n".join(lines)
