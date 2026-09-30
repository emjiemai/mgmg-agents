"""The employee KPI score — the Director's 15 criteria as six measured parts.

Asked for by the Director on 2026-09-30: "KPI, Performance, communication,
commitment, interaction, OKR, RACI, Task, goals, results, sales deals,
qualifications, quantity of work, jobs done ва process done". Several of these
are the same thing under two names, and two are frameworks rather than
numbers, so they are grouped into six parts, each 0–100, each from a source
that can be checked:

  Part (weight)                     Criteria covered          Source
  🎯 Натижа — results (30)          OKR, goals, results,       monthly goals the Director sets
                                    sales deals                (/maqsad); actual vs target
  ✅ Топшириқ — tasks (20)          Task, RACI                 tasks done by their deadline,
                                                               counted for the person Responsible
  ⭐ Раҳбар баҳоси — rating (20)    Performance,               the Director's 1–5 marks once a
                                    communication,             month (buttons)
                                    interaction,
                                    qualifications
  📦 Иш ҳажми — volume (10)         quantity of work,          tasks finished + reports sent,
                                    jobs done                  against the team's median
  🔄 Жараён — process (10)          process done               daily reports sent on time
  💪 Садоқат — commitment (10)      commitment                 attendance (Verifix, when
                                                               connected) and reports sent

**KPI** is the weighted total. A part with no data for someone (no goals
set, never asked for a report, Verifix not connected) is left out and the
others are re-weighted — never scored as zero. RACI: a task's assignee is
the Responsible (R) and is the one scored; the Director who gave it is
Accountable (A).

Pure functions only (tested in scripts/selfcheck.py); kpi_flow.py reads the
data and sends the messages.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from integrations.org_bot.task_tracker import MONTHS_UZ, ReportScore, TaskScore, score_reports, score_tasks
from integrations.telegram.bot import escape

# key -> (emoji, Uzbek label, weight)
PARTS: dict[str, tuple[str, str, int]] = {
    "results": ("🎯", "Натижа (OKR)", 30),
    "tasks": ("✅", "Топшириқ", 20),
    "rating": ("⭐", "Раҳбар баҳоси", 20),
    "volume": ("📦", "Иш ҳажми", 10),
    "process": ("🔄", "Жараён", 10),
    "commitment": ("💪", "Садоқат", 10),
}
# The four things the Director marks 1–5: key -> (emoji, label, callback letter).
RATINGS: dict[str, tuple[str, str, str]] = {
    "performance": ("📊", "Иш самарадорлиги", "p"),
    "communication": ("💬", "Мулоқот", "c"),
    "interaction": ("🤝", "Жамоада ишлаш", "i"),
    "qualifications": ("🎓", "Малака", "q"),
}
RATING_BY_LETTER = {letter: key for key, (_e, _l, letter) in RATINGS.items()}
GREEN, YELLOW = 80, 60


def grade(score: float | None) -> str:
    """🟢 80+, 🟡 60–79, 🔴 below 60, ⚪ nothing measured."""
    if score is None:
        return "⚪"
    return "🟢" if score >= GREEN else "🟡" if score >= YELLOW else "🔴"


def month_title(month: date) -> str:
    return f"{MONTHS_UZ[month.month - 1]} {month.year}"


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(month: date) -> date:
    return date(month.year + (month.month == 12), month.month % 12 + 1, 1)


def previous_month(month: date) -> date:
    return date(month.year - (month.month == 1), (month.month - 2) % 12 + 1, 1)


def month_end(month: date) -> date:
    return next_month(month) - timedelta(days=1)


def score_period(today: date) -> date:
    """The month /kpi shows: last month for the first 5 days (it's being closed), else this one."""
    return previous_month(month_start(today)) if today.day <= 5 else month_start(today)


def goal_month(today: date) -> date:
    """The month a new goal is for: this month, or next month from the 25th."""
    return next_month(month_start(today)) if today.day >= 25 else month_start(today)


_NUMBER = re.compile(r"\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def parse_number(text: str) -> float | None:
    """The first number in ``text`` ("1 500 000", "12,5", "20 та") or None."""
    match = _NUMBER.search(text or "")
    if match is None:
        return None
    return float(match.group(0).replace(" ", "").replace(" ", "").replace(",", "."))


def only_number(text: str) -> float | None:
    """``text`` if it is just a number (what an actual-result answer must be)."""
    text = (text or "").strip()
    if not re.fullmatch(r"[\d\s .,]+", text):
        return None
    return parse_number(text)


def fmt_number(value: float | None) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:.1f}"


@dataclass
class Goal:
    """One monthly goal with a number to reach."""

    title: str
    target: float
    actual: float | None = None

    @property
    def done_share(self) -> float:
        """0..1 of the target reached (over 100% counts as 100%)."""
        if self.actual is None or self.target <= 0:
            return 0.0
        return max(0.0, min(1.0, self.actual / self.target))


@dataclass
class Attendance:
    """A month of Verifix days for one person."""

    working: int = 0
    late: int = 0
    absent: int = 0


@dataclass
class EmployeeMonth:
    """Everything measured about one person in one month."""

    employee_id: str
    name: str
    role_label: str = ""
    reports: ReportScore = field(default_factory=ReportScore)
    tasks: TaskScore = field(default_factory=TaskScore)
    jobs_done: int = 0
    goals: list[Goal] = field(default_factory=list)
    ratings: dict[str, int] = field(default_factory=dict)
    attendance: Attendance | None = None
    parts: dict[str, float | None] = field(default_factory=dict)
    total: float | None = None

    @property
    def volume(self) -> int:
        """Finished work: tasks completed plus daily reports sent."""
        return self.jobs_done + self.reports.reported

    @property
    def rating_complete(self) -> bool:
        return all(self.ratings.get(key) for key in RATINGS)


def score(people: list[EmployeeMonth]) -> list[EmployeeMonth]:
    """Fill in each person's parts and total; best first (nothing measured last)."""
    volumes = [p.volume for p in people if p.volume > 0]
    median = statistics.median(volumes) if volumes else 0
    for p in people:
        parts: dict[str, float | None] = dict.fromkeys(PARTS)
        if p.goals:
            parts["results"] = 100 * sum(g.done_share for g in p.goals) / len(p.goals)
        if p.tasks.due:
            parts["tasks"] = 100 * (p.tasks.index or 0.0)
        marks = [p.ratings[k] for k in RATINGS if p.ratings.get(k)]
        if marks:
            parts["rating"] = 20 * sum(marks) / len(marks)  # 5 -> 100, 4 -> 80, 3 -> 60
        # Volume only for someone who was in the work that month (asked for
        # reports, given tasks) — a new or idle-by-design person isn't a zero.
        if median > 0 and (p.reports.asked or p.tasks.due or p.jobs_done):
            parts["volume"] = min(100.0, 100 * p.volume / median)
        if p.reports.asked:
            parts["process"] = 100 * p.reports.on_time / p.reports.asked
        commitment = []
        if p.attendance and p.attendance.working:
            a = p.attendance
            commitment.append(max(0.0, 100 * (a.working - a.absent - 0.5 * a.late) / a.working))
        if p.reports.asked:
            commitment.append(100 * p.reports.reported / p.reports.asked)
        if commitment:
            parts["commitment"] = sum(commitment) / len(commitment)
        p.parts = parts
        weights = {k: PARTS[k][2] for k, v in parts.items() if v is not None}
        p.total = sum(parts[k] * w for k, w in weights.items()) / sum(weights.values()) if weights else None
    return sorted(people, key=lambda p: (p.total is None, -(p.total or 0), p.name))


def build(
    employees: list[dict[str, Any]],
    report_rows: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
    done_counts: dict[str, int],
    goal_rows: list[dict[str, Any]],
    rating_rows: list[dict[str, Any]],
    start: date,
    end: date,
    attendance: dict[str, Attendance] | None = None,
    role_labels: dict[str, str] | None = None,
) -> list[EmployeeMonth]:
    """One EmployeeMonth per employee from the month's rows (all keyed by ``employee_id``)."""
    by_id: dict[str, dict[str, list]] = {}
    for kind, rows in (("reports", report_rows), ("tasks", task_rows), ("goals", goal_rows), ("ratings", rating_rows)):
        for row in rows:
            by_id.setdefault(str(row["employee_id"]), {}).setdefault(kind, []).append(row)
    people = []
    for emp in employees:
        eid = str(emp["id"])
        rows = by_id.get(eid, {})
        rating = (rows.get("ratings") or [{}])[0]
        people.append(
            EmployeeMonth(
                employee_id=eid,
                name=(emp.get("full_name") or "").strip() or emp.get("display_name") or "—",
                role_label=(role_labels or {}).get(emp.get("role"), emp.get("role") or ""),
                reports=score_reports(rows.get("reports", [])),
                tasks=score_tasks(rows.get("tasks", []), start, end),
                jobs_done=done_counts.get(eid, 0),
                goals=[
                    Goal(title=g["title"], target=float(g["target"]),
                         actual=None if g.get("actual") is None else float(g["actual"]))
                    for g in rows.get("goals", [])
                ],
                ratings={k: int(rating[k]) for k in RATINGS if rating.get(k)},
                attendance=(attendance or {}).get(eid),
            )
        )
    return score(people)


def _parts_line(p: EmployeeMonth) -> str:
    return " · ".join(
        f"{PARTS[k][0]} {round(v)}" for k, v in p.parts.items() if v is not None
    ) or "маълумот йўқ"


def table_text(month: date, people: list[EmployeeMonth], final: bool) -> str:
    """Everyone's month in one message, best first."""
    head = "📈 <b>KPI — " + month_title(month) + ("</b>" if final else " (ҳозирча)</b>")
    lines = [head, "<i>" + " · ".join(f"{e} {label}" for e, label, _w in PARTS.values()) + "</i>", ""]
    measured = [p for p in people if p.total is not None]
    if not measured:
        lines.append("Бу ой учун ҳали ўлчанадиган маълумот йўқ.")
        return "\n".join(lines)
    for place, p in enumerate(measured, 1):
        role = f" ({escape(p.role_label)})" if p.role_label else ""
        lines.append(f"{place}. {grade(p.total)} <b>{escape(p.name)}</b>{role} — <b>{round(p.total)}</b>")
        lines.append(f"      {_parts_line(p)}")
    pending = [p.name for p in people if p.total is not None and not p.rating_complete]
    if pending and not final:
        lines += ["", f"<i>⭐ Баҳоланмаган: {escape(', '.join(pending))}</i>"]
    lines += ["", "<i>🟢 80+ · 🟡 60–79 · 🔴 60 дан паст. Батафсил: /kpi</i>"]
    return "\n".join(lines)


def card_text(month: date, p: EmployeeMonth) -> str:
    """One person's month in detail — what they see with /kpi."""
    total = "—" if p.total is None else str(round(p.total))
    lines = [f"📈 <b>{escape(p.name)} — KPI, {month_title(month)}</b>", f"{grade(p.total)} Жами: <b>{total}</b> / 100", ""]
    for key, (emoji, label, weight) in PARTS.items():
        value = p.parts.get(key)
        shown = "—" if value is None else str(round(value))
        lines.append(f"{emoji} {label} ({weight}%): <b>{shown}</b>{_detail(key, p)}")
    if p.goals:
        lines += ["", "🎯 <b>Мақсадлар:</b>"]
        for g in p.goals:
            actual = "киритилмаган" if g.actual is None else f"{fmt_number(g.actual)} / {fmt_number(g.target)}"
            lines.append(f"   • {escape(g.title)} — {actual} ({round(100 * g.done_share)}%)")
    lines += ["", "<i>Маълумоти йўқ қисмлар ҳисобга олинмайди, қолганлари қайта тортилади.</i>"]
    return "\n".join(lines)


def _detail(key: str, p: EmployeeMonth) -> str:
    if key == "tasks" and p.tasks.due:
        return f" — {p.tasks.on_time}/{p.tasks.due} ўз вақтида"
    if key == "volume":
        return f" — {p.jobs_done} та иш, {p.reports.reported} та ҳисобот"
    if key == "process" and p.reports.asked:
        return f" — {p.reports.on_time}/{p.reports.asked} ҳисобот вақтида"
    if key == "commitment" and p.attendance and p.attendance.working:
        a = p.attendance
        return f" — {a.working} иш куни, {a.late} кечикиш, {a.absent} келмаган"
    if key == "rating" and p.ratings:
        return " — " + ", ".join(f"{RATINGS[k][0]}{v}" for k, v in p.ratings.items())
    return ""


def rating_text(month: date, p: EmployeeMonth) -> str:
    """The card the Director marks 1–5 for one person."""
    lines = [f"⭐ <b>{escape(p.name)}</b>" + (f" ({escape(p.role_label)})" if p.role_label else "")
             + f" — {month_title(month)} баҳоси", f"<i>Автоматик қисмлар: {_parts_line(p)}</i>", ""]
    for key, (emoji, label, _letter) in RATINGS.items():
        lines.append(f"{emoji} {label}: <b>{p.ratings.get(key) or '—'}</b>")
    lines.append("")
    lines.append("✅ Баҳоланди." if p.rating_complete else "<i>Ҳар қатордан 1–5 танланг (5 — аъло).</i>")
    return "\n".join(lines)


def rating_keyboard(rating_id: str, ratings: dict[str, int]) -> dict[str, Any]:
    """Four rows of 1–5; the chosen mark is shown as •4•."""
    rows = []
    for key, (emoji, _label, letter) in RATINGS.items():
        row = []
        for n in range(1, 6):
            shown = f"•{n}•" if ratings.get(key) == n else str(n)
            row.append({"text": f"{emoji} {shown}" if n == 1 else shown, "callback_data": f"kr:{rating_id}:{letter}{n}"})
        rows.append(row)
    return {"inline_keyboard": rows}


def match_attendance(people_names: dict[str, str], verifix_names: dict[str, str]) -> dict[str, str]:
    """employee_id -> Verifix employee id, by the same words in the name (any order).

    "Алишер Каримов" (typed in the bot) matches "Каримов Алишер" (Verifix);
    a name that matches none or more than one person is left unmatched.
    """
    def words(name: str) -> frozenset[str]:
        return frozenset(w for w in re.findall(r"\w+", name.lower().replace("ё", "е")) if len(w) > 1)

    by_words: dict[frozenset[str], list[str]] = {}
    for vid, name in verifix_names.items():
        by_words.setdefault(words(name), []).append(vid)
    result = {}
    for eid, name in people_names.items():
        found = by_words.get(words(name), [])
        if len(found) == 1:
            result[eid] = found[0]
    return result
