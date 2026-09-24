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

# Latest sensible deadline: a "deadline" a year out is a misread, not a plan.
MAX_DEADLINE_DAYS = 366

# A report counts as on time when it arrives before people go home.
REPORT_ON_TIME_HOUR = 18

# The plan's thresholds: ≥ this is healthy (🟢), below the red line stops the
# rollout (🔴), in between is a warning (🟡).
A1_GREEN, A1_RED = 0.80, 0.50
A3_GREEN, A3_RED = 0.70, 0.50

WEEKDAYS_UZ = ("dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba")

# One-tap deadline choices offered when the Director didn't state one:
# callback code -> (button label, days from today; None = no deadline).
DEADLINE_CHOICES: dict[str, tuple[str, int | None]] = {
    "0": ("Bugun", 0),
    "1": ("Ertaga", 1),
    "3": ("3 kun", 3),
    "7": ("1 hafta", 7),
    "n": ("Muddatsiz", None),
}


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
        when = "bugun"
    elif due == today + timedelta(days=1):
        when = "ertaga"
    else:
        when = WEEKDAYS_UZ[due.weekday()]
    return f"⏰ <b>Muddat: {fmt_day(due)}</b> ({when})"


def completed_on_time(task: dict[str, Any]) -> bool | None:
    """Whether a task was finished by its deadline.

    Returns:
        True/False for a finished task with a deadline, None otherwise.
    """
    due = task.get("due_date")
    completed = task.get("completed_at")
    if due is None or task.get("status") != "done" or completed is None:
        return None
    finished = to_local(completed).date() if isinstance(completed, datetime) else completed
    return finished <= due


def reminder_text(task: dict[str, Any], today: date) -> str:
    """The morning reminder an employee gets before a deadline."""
    due = task["due_date"]
    when = "Bugun" if due == today else "Ertaga"
    return (
        f"⏰ <b>{when} muddati tugaydi</b> ({fmt_day(due)})\n"
        f"{_h(_one_line(task['task_summary']))}\n\n"
        "<i>Bajarib bo'lsangiz, topshiriqdagi «✅ Bajardim» tugmasini bosing.</i>"
    )


def overdue_employee_text(task: dict[str, Any]) -> str:
    """The one message an employee gets when a deadline passes."""
    return (
        f"⚠️ <b>Muddat o'tdi</b> ({fmt_day(task['due_date'])})\n"
        f"{_h(_one_line(task['task_summary']))}\n\n"
        "<i>Direktorga xabar berildi. Holatni shu yerga yozing.</i>"
    )


def overdue_director_text(tasks: list[dict[str, Any]]) -> str:
    """One message to the Director listing every task that just went overdue."""
    lines = [f"⚠️ <b>Muddati o'tgan topshiriqlar: {len(tasks)} ta</b>"]
    for task in tasks[:15]:
        lines.append(
            f"• {_h(task.get('display_name') or '—')} — {_h(_short(task['task_summary']))} "
            f"<i>({fmt_day(task['due_date'])})</i>"
        )
    if len(tasks) > 15:
        lines.append(f"<i>+yana {len(tasks) - 15} ta</i>")
    return "\n".join(lines)


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
        score.asked += 1
        if row.get("status") != "submitted":
            name = row.get("display_name") or "—"
            score.missed_by_person[name] = score.missed_by_person.get(name, 0) + 1
            continue
        score.reported += 1
        submitted = row.get("submitted_at")
        if isinstance(submitted, datetime):
            local = to_local(submitted)
            if local.date() == row["report_date"] and local.hour < REPORT_ON_TIME_HOUR:
                score.on_time += 1
    return score


def weekly_text(
    start: date,
    end: date,
    tasks: TaskScore,
    reports: ReportScore | None,
    permissions: dict[str, int] | None,
) -> str:
    """The Friday scorecard for the Director — totals plus only the problems.

    Args:
        start: Monday of the week.
        end: The day it's sent (Friday).
        tasks: A3 scorecard.
        reports: A1 scorecard, or None when daily reports are switched off.
        permissions: Counts by status for the week's permission requests, or
            None to omit the section.
    """
    lines = [f"📊 <b>Haftalik natija — {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}</b>", ""]

    if tasks.due:
        idx = tasks.index or 0.0
        lines.append(
            f"{_mark(idx, A3_GREEN, A3_RED)} <b>Topshiriqlar:</b> {tasks.on_time}/{tasks.due} o'z vaqtida "
            f"(И = {idx:.2f})"
        )
        if tasks.late:
            lines.append(f"   Kechikib bajarilgan: {tasks.late}")
        if tasks.open_overdue:
            lines.append(f"   Bajarilmagan, muddati o'tgan: {tasks.open_overdue}")
            for task in tasks.overdue_items[:8]:
                lines.append(
                    f"   • {_h(task.get('display_name') or '—')} — {_h(_short(task['task_summary'], 50))}"
                )
        weak = sorted(
            ((name, due, done) for name, (due, done) in tasks.by_person.items() if done < due),
            key=lambda item: item[2] / item[1],
        )
        if weak:
            lines.append("   Kim ortda: " + ", ".join(f"{_h(n)} {d}/{t}" for n, t, d in weak[:6]))
    else:
        lines.append("📋 <b>Topshiriqlar:</b> bu hafta muddatli topshiriq bo'lmadi")

    if reports is not None:
        lines.append("")
        if reports.asked:
            idx = reports.index or 0.0
            lines.append(
                f"{_mark(idx, A1_GREEN, A1_RED)} <b>Kunlik hisobotlar:</b> {reports.reported}/{reports.asked} "
                f"yuborildi, {reports.on_time} tasi o'z vaqtida (И = {idx:.2f})"
            )
            missed = sorted(reports.missed_by_person.items(), key=lambda kv: -kv[1])
            if missed:
                lines.append("   Yubormaganlar: " + ", ".join(f"{_h(n)} ({c} kun)" for n, c in missed[:8]))
        else:
            lines.append("📝 <b>Kunlik hisobotlar:</b> bu hafta so'ralmagan")

    if permissions is not None:
        total = sum(permissions.values())
        lines.append("")
        if total:
            waiting = permissions.get("submitted", 0) + permissions.get("info_needed", 0)
            approved = permissions.get("approved", 0) + permissions.get("approved_conditional", 0)
            lines.append(
                f"📄 <b>Ёзма рухсатлар:</b> {total} ta — тасдиқланди {approved}, "
                f"рад {permissions.get('rejected', 0)}, кутилмоқда {waiting}"
            )
        else:
            lines.append("📄 <b>Ёзма рухсатлар:</b> бу ҳафта сўров бўлмади")

    return "\n".join(lines)
