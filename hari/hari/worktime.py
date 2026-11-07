"""Work-time helpers: Tashkent dates, work days and work hours (spec §10, A07)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from hari.config import TASHKENT
from hari.db.models import Employee


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local(dt: datetime) -> datetime:
    return dt.astimezone(TASHKENT)


def today_local(now: datetime | None = None) -> date:
    return local(now or now_utc()).date()


def fmt(dt: datetime | None) -> str:
    return local(dt).strftime("%d.%m.%Y %H:%M") if dt else "—"


def is_work_time(emp: Employee, now: datetime) -> bool:
    loc = local(now)
    return emp.works_on(loc.weekday()) and emp.work_start_hour <= loc.hour < emp.work_end_hour


def work_days_between(emp: Employee, start: date, end: date) -> int:
    """Work days from start (counted as day 0) up to end. Negative before start."""
    if end < start:
        return -1
    days, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if emp.works_on(cur.weekday()):
            days += 1
    return days


def add_work_hours(emp: Employee | None, start: datetime, hours: int,
                   default_start: int = 9, default_end: int = 18, days: str = "0,1,2,3,4,5") -> datetime:
    """The moment `hours` work hours after `start`, in the person's schedule (or the default one)."""
    s_hour = emp.work_start_hour if emp else default_start
    e_hour = emp.work_end_hour if emp else default_end
    work_days = (emp.work_days if emp else days).split(",")
    cur = local(start).replace(second=0, microsecond=0)
    remaining = timedelta(hours=hours)
    for _ in range(400):  # bounded: at most ~2 months of calendar walking
        if str(cur.weekday()) in work_days:
            day_start = cur.replace(hour=s_hour, minute=0)
            day_end = cur.replace(hour=e_hour, minute=0)
            if cur < day_start:
                cur = day_start
            if cur < day_end:
                available = day_end - cur
                if remaining <= available:
                    return (cur + remaining).astimezone(timezone.utc)
                remaining -= available
        cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
    return (cur + remaining).astimezone(timezone.utc)
