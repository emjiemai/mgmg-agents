"""The daily activity score — pure, tested offline (2026-10-22, the Boss: "monitor their work, like a stock chart").

One number per person per working day, 0–100, from what passes through the company's own systems:

  * **attendance** (40) — Verifix: on time 1, late ≤ 30 min 0.6, later 0.3, absent 0;
  * **report** (30) — the daily report: sent 1, put together from the day's messages 0.7, missed 0;
  * **tasks** (30) — the tasks in play that day: done on time 1, done late 0.5, due and not done 0.

Only the parts that apply count: a day off, an excused or «эркин график» day, a day without a report
ask or a task leaves that part out instead of scoring it 0. A day with no part at all has no score.
The company line is the average of everyone's score that day.
"""

from __future__ import annotations

from datetime import date, datetime
from statistics import mean
from typing import Any, Iterable

WEIGHTS = {"attendance": 40, "report": 30, "tasks": 30}
CLOSED = ("done", "cancelled")
REPORT_CLOSE_HOUR = 22


def attendance_value(status: str | None, late_minutes: int = 0) -> float | None:
    if status in ("on_time", "present"):
        return 1.0
    if status == "late":
        return 0.6 if late_minutes <= 30 else 0.3
    if status == "absent":
        return 0.0
    return None  # off, excused, эркин график, not yet / before start


def report_value(row: dict[str, Any] | None, day: date, now: datetime) -> float | None:
    if row is None:
        return None
    if row.get("status") == "submitted":
        return 0.7 if row.get("auto_built") else 1.0
    if day < now.date() or now.hour >= REPORT_CLOSE_HOUR:
        return 0.0
    return None  # still open today


def _day(value: Any) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def task_value(tasks: Iterable[dict[str, Any]], day: date, local_day: Any = _day) -> float | None:
    """The tasks in play on ``day``: due that day, closed that day, or overdue and still open then."""
    values: list[float] = []
    for t in tasks:
        due = t.get("due_date")
        done = local_day(t.get("completed_at")) if t.get("status") == "done" else None
        cancelled = t.get("status") == "cancelled"
        created = local_day(t.get("created_at"))
        if created and created > day:
            continue
        if done == day:
            values.append(1.0 if due is None or done <= due else 0.5)
        elif due is not None and due <= day and not cancelled and (done is None or done > day):
            values.append(0.0)
    return mean(values) if values else None


def day_score(parts: dict[str, float | None]) -> int | None:
    counted = {k: v for k, v in parts.items() if v is not None}
    if not counted:
        return None
    return round(100 * sum(WEIGHTS[k] * v for k, v in counted.items()) / sum(WEIGHTS[k] for k in counted))


def average(values: Iterable[float | int | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return mean(vals) if vals else None


def delta(now: float | None, before: float | None, pct: bool = False) -> float | None:
    """The change against the period before: in points, or in % (sales)."""
    if now is None or before is None:
        return None
    if pct:
        return None if not before else round(100 * (now - before) / abs(before), 1)
    return round(now - before, 1)
