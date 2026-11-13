"""Task deadlines, reminders and the weekly scorecard — agent A3 (and A1's И).

From the owner's plan (ЭМЖИЕМ AI Агентлар Тизими, 25.08.2026): 11 projects
started, 2 finished, and "employees don't work unless reminded" is the
company's most expensive habit. A3 catches every task the Director hands out,
gives it a deadline, reminds the employee before it, tells the Director once
when it's missed, and on Friday lists what was and wasn't done.

The plan measures each agent with a coefficient И and stops the rollout when
it falls below 0.50. Two are computed here from data the bot already owns:

    A1 (daily reports):  И = (reported ÷ asked) × (on time ÷ reported)
    A3 (tasks):          И = (done on time ÷ tasks due)

Deadlines are never invented. The AI only extracts one the Director actually
stated ("ertaga", "juma kuni", "25-sentabrgacha"); otherwise the Director is
offered one-tap choices, and "no deadline" is a valid answer.

Pure functions only (no DB, no Telegram), so every rule here is exercised
offline in scripts/selfcheck.py.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from integrations.common.timeutil import to_local
from integrations.org_bot import kpi
from integrations.org_bot.roles import DIRECTOR_ROLE

# Latest sensible deadline: a "deadline" a year out is a misread, not a plan.
MAX_DEADLINE_DAYS = 366

# A report counts as on time when it arrives before people go home.
REPORT_ON_TIME_HOUR = 18

# The plan's thresholds: ≥ this is healthy (🟢), below the red line stops the
# rollout (🔴), in between is a warning (🟡).
A1_GREEN, A1_RED = 0.80, 0.50
A3_GREEN, A3_RED = 0.70, 0.50

WEEKDAYS_UZ = ("душанба", "сешанба", "чоршанба", "пайшанба", "жума", "шанба", "якшанба")

# One-tap deadline choices offered when the Director didn't state one:
# callback code -> (button label, days from today; None = no deadline).
DEADLINE_CHOICES: dict[str, tuple[str, int | None]] = {
    "0": ("Бугун", 0),
    "1": ("Эртага", 1),
    "3": ("3 кун", 3),
    "7": ("1 ҳафта", 7),
    "n": ("Муддатсиз", None),
}


def next_work_day(day: date, off_days: set[date] | frozenset[date] = frozenset()) -> date:
    """The next day after ``day`` the 08:00 job runs: not Saturday / Sunday, not an Admin /dam day."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5 or nxt in off_days:
        nxt += timedelta(days=1)
    return nxt


def remind_until(today: date, off_days: set[date] | frozenset[date] = frozenset()) -> date:
    """The last due date this morning's reminder covers (2026-11-12): tomorrow, and on the last working day before
    a weekend or /dam days also those days — a task due on Sunday has no morning of its own. Never the next
    working day itself: that morning says «бугун» (one reminder per task, so it isn't used up early)."""
    return max(today + timedelta(days=1), next_work_day(today, off_days) - timedelta(days=1))


def held_today(task: dict[str, Any], today: date, excused_ids: set[str]) -> bool:
    """The person isn't at work this morning (their week says no, or the day is excused): no note, nothing marked,
    so their next working morning catches it. Someone working only weekends is never held — the job doesn't run
    then, they'd never hear."""
    if str(task.get("assigned_employee_id")) in excused_ids:
        return True
    if kpi.works_on(task, today):
        return False
    return any(kpi.works_on(task, today + timedelta(days=i - today.weekday())) for i in range(5))


def next_day_at_work(employee: dict[str, Any], today: date, off_days: set[date] | frozenset[date] = frozenset()) -> date:
    """The next day after ``today`` this person works (their week, not a /dam day) — when a note left for him is read
    (2026-11-12: a task released while he is off). Three weeks out at most; else tomorrow."""
    for i in range(1, 22):
        day = today + timedelta(days=i)
        if kpi.works_on(employee, day) and day not in off_days:
            return day
    return today + timedelta(days=1)


def overdue_told(task: dict[str, Any], manager_told: bool) -> dict[str, Any]:
    """Who the overdue line says was told: the Director, or whoever gave the task; the department manager too."""
    told = "director" if (task.get("assigner_role") or DIRECTOR_ROLE) == DIRECTOR_ROLE else "assigner"
    return {"told": told, "manager": bool(manager_told)}


def _h(value: Any) -> str:
    """Escape anything a person typed before it goes into Telegram HTML."""
    return html.escape(str(value), quote=False)


def fmt_day(day: date) -> str:
    """A deadline as the form people read it: 25.09.2026."""
    return day.strftime("%d.%m.%Y")


def parse_due_date(value: Any, today: date) -> date | None:
    """Validate the deadline the classifier extracted.

    Args:
        value: What the model returned — expected "YYYY-MM-DD" or null.
        today: Today in Tashkent.

    Returns:
        The date, or None when there is none, it doesn't parse, it's in the
        past, or it's implausibly far away. A bad value is dropped rather
        than guessed at: the Director is then simply offered the choices.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        due = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
    if due < today or due > today + timedelta(days=MAX_DEADLINE_DAYS):
        return None
    return due


def due_from_choice(code: str, today: date) -> tuple[bool, date | None]:
    """Resolve a deadline button.

    Returns:
        ``(recognised, deadline)`` — deadline None means "no deadline".
    """
    choice = DEADLINE_CHOICES.get(code)
    if choice is None:
        return False, None
    days = choice[1]
    return True, (None if days is None else today + timedelta(days=days))


def deadline_keyboard(source_message_id: int) -> dict[str, Any]:
    """The Director's one-tap deadline choices for a just-dispatched task."""
    buttons = [
        {"text": label, "callback_data": f"taskdue:{code}:{source_message_id}"}
        for code, (label, _days) in DEADLINE_CHOICES.items()
    ]
    return {"inline_keyboard": [buttons[:3], buttons[3:]]}


def deadline_line(due: date | None, today: date) -> str:
    """The deadline line on a task card ("" when there is none)."""
    if due is None:
        return ""
    if due == today:
        when = "бугун"
    elif due == today + timedelta(days=1):
        when = "эртага"
    else:
        when = WEEKDAYS_UZ[due.weekday()]
    return f"⏰ <b>Муддат: {fmt_day(due)}</b> ({when})"


def completed_on_time(task: dict[str, Any]) -> bool | None:
    """Whether a task was finished by its deadline.

    Returns:
        True/False for a finished task with a deadline, None otherwise.
    """
    due = task.get("due_date")
    completed = task.get("completed_at")
    # 'review' = done, proof sent, waiting for the check (task_register.py): finished when the proof went in.
    if due is None or task.get("status") not in ("done", "review") or completed is None:
        return None
    finished = to_local(completed).date() if isinstance(completed, datetime) else completed
    return finished <= due


def reminder_text(task: dict[str, Any], today: date) -> str:
    """The morning reminder an employee gets before a deadline."""
    due = task["due_date"]
    when = "Бугун" if due == today else "Эртага"
    expected = _one_line(task.get("expected_result"))  # 2026-11-12: the Director's expected result, when he said it
    return (
        f"⏰ <b>{when} муддати тугайди</b> ({fmt_day(due)})\n"
        f"{_h(_one_line(task['task_summary']))}\n"
        + (f"🎯 Кутилган натижа: {_h(expected)}\n" if expected else "")
        + "\n<i>Бажариб бўлсангиз, топшириқдаги «✅ Бажардим» тугмасини босинг.</i>"
    )


def overdue_employee_text(task: dict[str, Any]) -> str:
    """The one message an employee gets when a deadline passes."""
    return (
        f"⚠️ <b>Муддат ўтди</b> ({fmt_day(task['due_date'])})\n"
        f"{_h(_one_line(task['task_summary']))}\n\n"
        "<i>Директорга хабар берилди. Ҳолатни шу ерга ёзинг.</i>"
    )


def overdue_director_text(tasks: list[dict[str, Any]], head: str | None = None) -> str:
    """One message to the Director listing every task that just went overdue.

    ``head`` (2026-11-12, the escalation ladder) replaces the first line; a task with ``ladder_note`` shows it in
    place of the deadline (a blocked task may have none)."""
    lines = [head or f"⚠️ <b>Муддати ўтган топшириқлар: {len(tasks)} та</b>"]
    for task in tasks[:15]:
        when = task.get("ladder_note") or (fmt_day(task["due_date"]) if task.get("due_date") else "муддатсиз")
        lines.append(
            f"• {'🔴 ' if int(task.get('priority') or 2) == 1 else ''}{_h(task.get('display_name') or '—')} — "
            f"{_h(_short(task['task_summary']))} "
            f"<i>({_h(when)})</i>"
        )
    if len(tasks) > 15:
        lines.append(f"<i>+яна {len(tasks) - 15} та</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------ escalation ladder (2026-11-12)

# The ladder's thresholds in days (config: TASK_ESCALATE_MANAGER_DAYS, TASK_ESCALATE_DIRECTOR_DAYS,
# TASK_BLOCKED_MANAGER_DAYS, TASK_BLOCKED_DIRECTOR_DAYS).
LADDER_DEFAULTS = {"manager": 1, "director": 3, "blocked_manager": 2, "blocked_director": 4}


def ladder_days(settings: Any) -> dict[str, int]:
    """The thresholds from the settings (never below 1 day)."""
    return {
        "manager": max(1, int(settings.task_escalate_manager_days)),
        "director": max(1, int(settings.task_escalate_director_days)),
        "blocked_manager": max(1, int(settings.task_blocked_manager_days)),
        "blocked_director": max(1, int(settings.task_blocked_director_days)),
    }


def _local_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return to_local(value).date()
    return value if isinstance(value, date) else None


def working_days_between(start: date, today: date, off_days: set[date] | frozenset[date] = frozenset()) -> int:
    """Working days d with start < d <= today (Monday–Friday, not a /dam day) — the ladder's clock (2026-11-13: a
    weekend is not two days of nobody acting). Negative never: 0 when ``start`` is today or later."""
    n, day = 0, start + timedelta(days=1)
    while day <= today:
        if day.weekday() < 5 and day not in off_days:
            n += 1
        day += timedelta(days=1)
    return n


def ladder_steps(task: dict[str, Any], today: date, days: dict[str, int] | None = None,
                 off_days: set[date] | frozenset[date] = frozenset()) -> list[tuple[int, str, int]]:
    """The ladder steps due for a task this morning: ``(level, kind, n)`` — level 1 = the department manager (or his
    deputy), 2 = the Director; kind 'overdue' / 'blocked'; n = calendar days past the deadline / days blocked (shown).

    Thresholds count WORKING days (``working_days_between``, /dam days ``off_days`` left out). Overdue days count
    from the day-0 notice (``overdue_notified_at``: that morning the assigner and an available manager were told),
    so level 1 is only a retry for a task still at 0 (no manager could be told then). Blocked days count from the
    last ⛔ or the last accepted help task (``blocked_since``); a blocked task with an open help task is not on the
    blocked ladder. A ⛔ restarts the level at 0 (``store.block_task``), so the blocked ladder starts fresh. Steps at
    or below ``escalation_level`` already went."""
    days = days or LADDER_DEFAULTS
    level = int(task.get("escalation_level") or 0)
    due = task.get("due_date")
    noticed = _local_day(task.get("overdue_notified_at"))
    is_late = due is not None and due < today
    since_notice = working_days_between(noticed, today, off_days) if (is_late and noticed) else -1
    late_work = working_days_between(due, today, off_days) if is_late else 0
    late = (today - due).days if is_late else 0
    blocked = blocked_shown = -1
    since = _local_day(task.get("blocked_since"))
    if task.get("status") == "blocked" and not task.get("has_helper") and since:
        blocked = working_days_between(since, today, off_days)
        blocked_shown = (today - since).days
    steps: list[tuple[int, str, int]] = []
    if level < 1:
        if blocked >= days["blocked_manager"]:
            steps.append((1, "blocked", blocked_shown))
        elif since_notice >= days["manager"]:
            steps.append((1, "overdue", late))
    if level < 2:
        if blocked >= days["blocked_director"]:
            steps.append((2, "blocked", blocked_shown))
        elif since_notice >= days["director"] and late_work >= days["director"]:
            steps.append((2, "overdue", late))
    return steps


def ladder_note(kind: str, n: int) -> str:
    """What the list line says instead of the deadline."""
    return f"{n} кундан бери тўсиқда" if kind == "blocked" else f"муддати {n} кун ўтди"


def present(person: dict[str, Any] | None, today: date, excused_ids: set[str] | frozenset[str] = frozenset()) -> bool:
    """Whether a manager / deputy can get a ladder step today: has a chat, hasn't blocked the bot, works today
    (``kpi.works_on``) and today isn't his excused day. (/dam days: the job doesn't run.)"""
    if not person or not person.get("telegram_user_id") or person.get("bot_blocked_at"):
        return False
    if person.get("status", "active") != "active" or str(person.get("id")) in excused_ids:
        return False
    return kpi.works_on(person, today)


def _name(person: dict[str, Any]) -> str:
    return (person.get("full_name") or "").strip() or person.get("display_name") or "—"


def manager_targets(
    task: dict[str, Any],
    managers_by_role: dict[str, list[dict[str, Any]]],
    deputies: dict[str, dict[str, Any]],
    team_of: dict[str, tuple[str, ...]],
    today: date | None,
    excused_ids: set[str] | frozenset[str] = frozenset(),
) -> list[tuple[dict[str, Any], str | None]]:
    """Who hears of a task as its department manager: ``(person, deputy_for)`` — the TEAM_OF manager when he is at
    work today, else his deputy (``deputies``: id → row, already checked to be of his team) with the manager's name;
    nobody when neither can. Never a task the person gave himself, never his own task. ``today`` None = presence not
    checked."""
    out: list[tuple[dict[str, Any], str | None]] = []
    role = task.get("employee_role")
    assigner = task.get("director_telegram_user_id")
    own = task.get("employee_telegram_user_id")
    for manager_role, team in team_of.items():
        if role not in team:
            continue
        for manager in managers_by_role.get(manager_role, []):
            if manager.get("telegram_user_id") in (assigner, own) or str(manager.get("id")) == str(task.get("assigned_employee_id")):
                continue  # he gave it (he already knows), or it is his own
            if today is None or present(manager, today, excused_ids):
                out.append((manager, None))
                continue
            deputy = deputies.get(str(manager.get("deputy_employee_id") or ""))
            if (deputy and present(deputy, today, excused_ids)
                    and deputy.get("telegram_user_id") not in (assigner, own)
                    and str(deputy.get("id")) != str(task.get("assigned_employee_id"))):
                out.append((deputy, _name(manager)))
    return out


def shared_groups(tasks: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str]]]:
    """A shared task (one message to several people, source ≠ 0) escalates once: ``(the copy shown, every copy's
    id)``; the copy shown is the one lowest on the ladder."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for task in tasks:
        source = task.get("source_message_id") or 0
        key = (task.get("director_telegram_user_id"), source) if source else ("id", str(task.get("id")))
        groups.setdefault(key, []).append(task)
    out = []
    for copies in groups.values():
        shown = min(copies, key=lambda t: int(t.get("escalation_level") or 0))
        out.append((shown, [str(t["id"]) for t in copies]))
    return out


def escalation_plan(
    tasks: list[dict[str, Any]],
    today: date,
    days: dict[str, int],
    managers_by_role: dict[str, list[dict[str, Any]]],
    deputies: dict[str, dict[str, Any]],
    directors: list[dict[str, Any]],
    team_of: dict[str, tuple[str, ...]],
    excused_ids: set[str] | frozenset[str] = frozenset(),
    off_days: set[date] | frozenset[date] = frozenset(),
) -> list[dict[str, Any]]:
    """This morning's ladder sends: one per (person, level) — ``{"chat_id", "level", "person", "deputy_for",
    "tasks", "ids"}``. Level 1: each copy of a shared task finds its own department manager (or deputy); a send's
    ``ids`` are only the copies that person was told of (a copy with no manager stays at 0). Level 2 goes to every
    active Director except the one who assigned the task (overdue or blocked — he had the day-0 list / the ⛔ card)
    and the assignee himself."""
    sends: dict[tuple[int, int], dict[str, Any]] = {}
    by_id = {str(t["id"]): t for t in tasks}
    for shown, ids in shared_groups(tasks):
        for level, kind, n in ladder_steps(shown, today, days, off_days):
            item = {**shown, "ladder_kind": kind, "ladder_days": n, "ladder_note": ladder_note(kind, n)
                    + (f", +{len(ids) - 1} киши" if len(ids) > 1 else "")}
            targets: dict[int, tuple[dict[str, Any], str | None, list[str]]] = {}
            if level == 1:
                for cid in ids:
                    copy = by_id.get(cid, shown)
                    for person, deputy_for in manager_targets(copy, managers_by_role, deputies, team_of, today,
                                                              excused_ids):
                        entry = targets.setdefault(int(person["telegram_user_id"]), (person, deputy_for, []))
                        entry[2].append(cid)
            else:
                for d in directors:
                    if (d.get("telegram_user_id") and not d.get("bot_blocked_at")
                            and d["telegram_user_id"] != shown.get("director_telegram_user_id")
                            and d["telegram_user_id"] != shown.get("employee_telegram_user_id")):
                        targets.setdefault(int(d["telegram_user_id"]), (d, None, list(ids)))
            for chat, (person, deputy_for, told_ids) in targets.items():
                send = sends.setdefault((chat, level), {
                    "chat_id": chat, "level": level, "person": person,
                    "deputy_for": deputy_for, "tasks": [], "ids": []})
                if not any(str(t["id"]) == str(shown["id"]) for t in send["tasks"]):
                    send["tasks"].append(item)
                send["ids"].extend(i for i in told_ids if i not in send["ids"])
    return list(sends.values())


def ids_to_mark(sends: list[dict[str, Any]], failed: set[int]) -> list[tuple[int, str, list[str]]]:
    """After the ladder's sends: ``(level, note, ids)`` to record — a task whose send failed for ANY recipient at
    that level is left out everywhere at that level, so tomorrow retries it (2026-11-13: a second Director whose send
    failed never got the step). ``failed`` = indexes into ``sends``."""
    bad: dict[int, set[str]] = {}
    for i in failed:
        bad.setdefault(sends[i]["level"], set()).update(sends[i]["ids"])
    out = []
    for i, send in enumerate(sends):
        if i in failed:
            continue
        ids = [x for x in send["ids"] if x not in bad.get(send["level"], set())]
        note = "director" if send["level"] == 2 else ("deputy" if send.get("deputy_for") else "manager")
        if ids:
            out.append((send["level"], note, ids))
    return out


def escalation_head(count: int, level: int, deputy_for: str | None = None) -> str:
    """The ladder list's first line(s)."""
    head = f"⏫ <b>Муддати ўтган ёки тўсиқда турган топшириқлар: {count} та</b>"
    if level == 1 and deputy_for:
        head += f"\n<i>{_h(deputy_for)} бугун ишда эмас — сиз ўринбосари сифатида кўряпсиз.</i>"
    elif level == 1:
        head += "\n<i>Бўлимингиз ходимлари — назорат қилишингизни сўраймиз.</i>"
    else:
        head += "\n<i>Бир неча кундан бери ҳал бўлмаган.</i>"
    return head


def escalation_text(send: dict[str, Any]) -> str:
    """One ladder message (to a manager, a deputy or the Director)."""
    return overdue_director_text(send["tasks"], head=escalation_head(len(send["tasks"]), send["level"],
                                                                     send.get("deputy_for")))


def _one_line(text: Any) -> str:
    """Collapse whitespace; task summaries can span lines."""
    return " ".join(str(text or "").split())


def _short(text: Any, limit: int = 70) -> str:
    """A task summary cut down for a list line."""
    line = _one_line(text)
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _mark(value: float, green: float, red: float) -> str:
    """🟢/🟡/🔴 by the plan's thresholds."""
    if value >= green:
        return "🟢"
    if value < red:
        return "🔴"
    return "🟡"


# ------------------------------------------------------------------ scorecards


@dataclass
class TaskScore:
    """A3 for a period: tasks that were due, and what happened to them."""

    due: int = 0
    on_time: int = 0
    late: int = 0
    open_overdue: int = 0
    by_person: dict[str, list[int]] = field(default_factory=dict)  # name -> [due, on_time]
    overdue_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def index(self) -> float | None:
        """И = done on time ÷ tasks due; None when nothing was due."""
        return self.on_time / self.due if self.due else None


def score_tasks(tasks: list[dict[str, Any]], start: date, end: date) -> TaskScore:
    """Score every task whose deadline fell inside ``[start, end]``.

    Args:
        tasks: ``tasks`` rows with ``due_date``, ``status``, ``completed_at``
            and the employee's ``display_name``.
        start: First day of the period.
        end: Last day of the period (today, for a live scorecard).

    Returns:
        The scorecard. A task still open with its deadline ahead of ``end``
        isn't judged yet, so it doesn't count either way.
    """
    score = TaskScore()
    for task in tasks:
        due = task.get("due_date")
        if due is None or not (start <= due <= end):
            continue
        name = task.get("display_name") or "—"
        on_time = completed_on_time(task)
        if on_time is None and due >= end:
            continue  # still open, and today is its last day — not late yet
        score.due += 1
        person = score.by_person.setdefault(name, [0, 0])
        person[0] += 1
        if on_time:
            score.on_time += 1
            person[1] += 1
        elif on_time is False:
            score.late += 1
        else:
            score.open_overdue += 1
            score.overdue_items.append(task)
    return score


@dataclass
class ReportScore:
    """A1 for a period: who was asked, who answered, who answered on time."""

    asked: int = 0
    reported: int = 0
    on_time: int = 0
    same_day: int = 0  # sent on its own day (by the 22:00 deadline) — the simple KPI's «on time» (2026-11-03)
    missed_by_person: dict[str, int] = field(default_factory=dict)

    @property
    def index(self) -> float | None:
        """И = (reported ÷ asked) × (on time ÷ reported)."""
        if not self.asked or not self.reported:
            return 0.0 if self.asked else None
        return (self.reported / self.asked) * (self.on_time / self.reported)


def score_reports(rows: list[dict[str, Any]]) -> ReportScore:
    """Score a period's ``daily_reports`` rows (with ``display_name``)."""
    score = ReportScore()
    for row in rows:
        if row.get("status") == "excused":  # a day with a reason isn't an ask (2026-10-24)
            continue
        score.asked += 1
        if row.get("status") != "submitted":
            name = row.get("display_name") or "—"
            score.missed_by_person[name] = score.missed_by_person.get(name, 0) + 1
            continue
        score.reported += 1
        submitted = row.get("submitted_at")
        if isinstance(submitted, datetime):
            local = to_local(submitted)
            if local.date() == row["report_date"]:
                score.same_day += 1
                if local.hour < REPORT_ON_TIME_HOUR:
                    score.on_time += 1
    return score


REWORK_SHOWN = 5  # the Friday «Қайта ишлаш» names at most this many returned tasks


def weekly_text(
    start: date,
    end: date,
    tasks: TaskScore,
    reports: ReportScore | None,
    permissions: dict[str, int] | None,
    rework: list[dict[str, Any]] | None = None,
) -> str:
    """The Friday scorecard for the Director — totals plus only the problems.

    Args:
        start: Monday of the week.
        end: The day it's sent (Friday).
        tasks: A3 scorecard.
        reports: A1 scorecard, or None when daily reports are switched off.
        permissions: Counts by status for the week's permission requests, or
            None to omit the section.
        rework: The week's returned tasks (``store.returns_between``: code,
            display_name, times) — «Қайта ишлаш», only when any came back.
    """
    lines = [f"📊 <b>Ҳафталик натижа — {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}</b>", ""]

    if tasks.due:
        idx = tasks.index or 0.0
        lines.append(
            f"{_mark(idx, A3_GREEN, A3_RED)} <b>Топшириқлар:</b> {tasks.on_time}/{tasks.due} ўз вақтида "
            f"(И = {idx:.2f})"
        )
        if tasks.late:
            lines.append(f"   Кечикиб бажарилган: {tasks.late}")
        if tasks.open_overdue:
            lines.append(f"   Бажарилмаган, муддати ўтган: {tasks.open_overdue}")
            for task in tasks.overdue_items[:8]:
                lines.append(
                    f"   • {_h(task.get('display_name') or '—')} — {_h(_short(task['task_summary'], 50))}"
                )
        weak = sorted(
            ((name, due, done) for name, (due, done) in tasks.by_person.items() if done < due),
            key=lambda item: item[2] / item[1],
        )
        if weak:
            lines.append("   Ким ортда: " + ", ".join(f"{_h(n)} {d}/{t}" for n, t, d in weak[:6]))
    else:
        lines.append("📋 <b>Топшириқлар:</b> бу ҳафта муддатли топшириқ бўлмади")

    if rework:
        times = sum(int(r.get("times") or 0) for r in rework)
        lines.append(f"   Қайта ишлаш: {len(rework)} та топшириқ {times} марта қайтарилди")
        for row in rework[:REWORK_SHOWN]:
            lines.append(f"   • {_h(row.get('code') or '—')} — {_h(row.get('display_name') or '—')}"
                         + (f" ({int(row['times'])} марта)" if int(row.get("times") or 0) > 1 else ""))

    if reports is not None:
        lines.append("")
        if reports.asked:
            idx = reports.index or 0.0
            lines.append(
                f"{_mark(idx, A1_GREEN, A1_RED)} <b>Кунлик ҳисоботлар:</b> {reports.reported}/{reports.asked} "
                f"юборилди, {reports.on_time} таси ўз вақтида (И = {idx:.2f})"
            )
            missed = sorted(reports.missed_by_person.items(), key=lambda kv: -kv[1])
            if missed:
                lines.append("   Юбормаганлар: " + ", ".join(f"{_h(n)} ({c} кун)" for n, c in missed[:8]))
        else:
            lines.append("📝 <b>Кунлик ҳисоботлар:</b> бу ҳафта сўралмаган")

    if permissions is not None:
        total = sum(permissions.values())
        lines.append("")
        if total:
            waiting = permissions.get("submitted", 0) + permissions.get("info_needed", 0)
            approved = permissions.get("approved", 0) + permissions.get("approved_conditional", 0)
            lines.append(
                f"📄 <b>Ёзма рухсатлар:</b> {total} та — тасдиқланди {approved}, "
                f"рад {permissions.get('rejected', 0)}, кутилмоқда {waiting}"
            )
        else:
            lines.append("📄 <b>Ёзма рухсатлар:</b> бу ҳафта сўров бўлмади")

    return "\n".join(lines)


# ------------------------------------------------------------------ E1: monthly KPI

MONTHS_UZ = (
    "январ", "феврал", "март", "апрел", "май", "июн",
    "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
)
