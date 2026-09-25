"""Time handling.

Rule for the whole project: everything is stored in UTC and displayed in
Tashkent local time (Asia/Tashkent, UTC+5, no DST).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

TASHKENT = ZoneInfo("Asia/Tashkent")
UTC = timezone.utc


def now_utc() -> datetime:
    """Current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def now_local() -> datetime:
    """Current instant as a timezone-aware Tashkent datetime."""
    return datetime.now(TASHKENT)


def today_local() -> date:
    """Today's calendar date in Tashkent — the business day agents report on."""
    return now_local().date()


def to_local(dt: datetime) -> datetime:
    """Convert any datetime to Tashkent time.

    Args:
        dt: Aware or naive datetime. Naive input is assumed to be UTC, which is
            what every one of our sources returns.

    Returns:
        The same instant, expressed in Asia/Tashkent.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(TASHKENT)


def to_utc(dt: datetime) -> datetime:
    """Convert any datetime to UTC.

    Args:
        dt: Aware or naive datetime. Naive input is assumed to be Tashkent
            local, which is how humans and SAP/Verifix express wall times.

    Returns:
        The same instant, expressed in UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TASHKENT)
    return dt.astimezone(UTC)


def fmt_date(d: date | None) -> str:
    """Format a date as dd.mm.yyyy, or "—" when None."""
    return d.strftime("%d.%m.%Y") if d else "—"


def parse_sap_date(value: str | None) -> date | None:
    """Parse a SAP date string into a ``date``.

    Args:
        value: e.g. ``"2026-08-18"`` or ``"2026-08-18T00:00:00Z"``.

    Returns:
        The calendar date, or ``None`` if unparseable.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def days_between(earlier: date | None, later: date | None = None) -> int:
    """Whole days from ``earlier`` to ``later``.

    Args:
        earlier: Start date (e.g. an invoice due date).
        later: End date; defaults to today in Tashkent.

    Returns:
        Day count — positive when ``earlier`` is in the past, 0 if ``earlier``
        is None.
    """
    if earlier is None:
        return 0
    return ((later or today_local()) - earlier).days
