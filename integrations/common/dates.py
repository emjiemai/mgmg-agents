"""Read a calendar day out of what a person typed — strictly, never guessed.

Used where a free-text answer ("Бажариш муддати: 25.09.2026", "эртага")
has to become a date for a schedule: today's payments in the morning brief,
the 30-day cash calendar. Only explicit forms are read:

  * numeric dates — 25.09.2026, 25.09.26, 25/09/2026, 2026-09-25, 25.09
  * a day and a month name — "25 сентябр", "25-sentabr", "25 сентября"
  * бугун/bugun/сегодня, эртага/ertaga/завтра, индин/indin/послезавтра
  * "3 кун ичида" / "3 kun" / "3 дня" — that many days after it was written

Anything else ("шу ҳафта ичида", "тезроқ", "ойнинг охиригача") is not a
day and returns None — the caller shows it as "date unclear" rather than
putting it on a day it may not belong to.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# Stems, so every case ending matches: сентябр/сентябрь/сентября, sentabr/sentyabr.
_MONTHS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, ("январ", "yanvar", "january")),
    (2, ("феврал", "fevral", "february")),
    (3, ("март", "mart", "march")),
    (4, ("апрел", "aprel", "april")),
    (5, ("май", "мая", "may")),
    (6, ("июн", "iyun", "june")),
    (7, ("июл", "iyul", "july")),
    (8, ("август", "avgust", "august")),
    (9, ("сентябр", "sentabr", "sentyabr", "september")),
    (10, ("октябр", "oktabr", "oktyabr", "october")),
    (11, ("ноябр", "noyabr", "november")),
    (12, ("декабр", "dekabr", "december")),
)

_RELATIVE: tuple[tuple[int, tuple[str, ...]], ...] = (
    (2, ("индин", "indin", "послезавтра")),
    (1, ("эртага", "ertaga", "завтра")),
    (0, ("бугун", "bugun", "сегодня")),
)

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
_IN_DAYS = re.compile(r"\b(\d{1,2})\s*(?:кун|kun|дн|день|days?)", re.IGNORECASE)
_NAMED = re.compile(r"\b(\d{1,2})\s*[-–]?\s*(?:[a-zа-яёўқғҳ']*?)?([a-zа-яёўқғҳ']{3,})", re.IGNORECASE)

# A day-and-month with no year more than this far in the past is read as
# next year's ("05.01" typed on 28 December means January 5th coming).
_PAST_TOLERANCE_DAYS = 60


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _with_year(month: int, day: int, reference: date) -> date | None:
    """A day-month with the year it most plausibly means, relative to reference."""
    candidate = _safe(reference.year, month, day)
    if candidate is None:
        return None
    if (reference - candidate).days > _PAST_TOLERANCE_DAYS:
        return _safe(reference.year + 1, month, day)
    return candidate


def _month_of(word: str) -> int | None:
    lowered = word.lower()
    for month, stems in _MONTHS:
        if any(lowered.startswith(stem) for stem in stems):
            return month
    return None


def parse_day(text: str | None, reference: date) -> date | None:
    """The calendar day ``text`` names, or None if it doesn't name one.

    Args:
        text: What the person wrote.
        reference: The day they wrote it — resolves "эртага" and dates typed
            without a year.

    Returns:
        The day, or None for anything that isn't an explicit day.
    """
    if not text:
        return None
    lowered = text.lower().replace("ʻ", "'").replace("’", "'").replace("`", "'")

    # An explicit date wins; a pattern that looks like one but isn't a real
    # day ("15.00" as a time) falls through to the next rule.
    match = _ISO.search(lowered)
    if match:
        found = _safe(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if found:
            return found

    for match in _NUMERIC.finditer(lowered):
        day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
        if year is None:
            found = _with_year(month, day, reference)
        else:
            year_value = int(year) + (2000 if int(year) < 100 else 0)
            found = _safe(year_value, month, day)
        if found:
            return found

    for match in _NAMED.finditer(lowered):
        month = _month_of(match.group(2))
        if month is not None:
            found = _with_year(month, int(match.group(1)), reference)
            if found:
                return found

    match = _IN_DAYS.search(lowered)
    if match:
        return reference + timedelta(days=int(match.group(1)))

    for offset, words in _RELATIVE:
        if any(re.search(rf"(?<![a-zа-яёўқғҳ]){re.escape(word)}", lowered) for word in words):
            return reference + timedelta(days=offset)
    return None
