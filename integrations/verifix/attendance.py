"""Who came late, who didn't come — from Verifix's timesheet (A4).

Pure functions over ``timesheet$export`` rows (tested offline in
scripts/selfcheck.py) plus ``load`` that reads them. Used by the 08:00
brief (yesterday), OPS Manager Bot's ``davomat`` answers (today so far and
the last 30 days) and Admin Bot's ``/verifix`` check.

The rules, stated so nobody has to guess what a number means:

  * Only the employee's working days count (Verifix ``day_kind`` W); days
    off and holidays are never "absent".
  * **Late** = first arrival more than ``VERIFIX_LATE_GRACE_MINUTES``
    (default 5) after the schedule's start. The minutes are computed from
    the two times themselves: Verifix documents its time facts as seconds,
    but its own example only adds up as minutes, so their size is not used.
  * **Absent** = a working day with no arrival and nothing that excuses it.
    Today, before the day is over, that reads "ҳали келмади", not "келмади".
  * **Excused** = Verifix recorded sick leave, vacation, unpaid leave, a
    business trip or a day off for that day; an hourly leave excuses coming
    late. Which fact is which is read from the company's own time-kind
    names (time_kind$list), so a renamed kind still lands right.
  * **Own start time** (2026-10-22) = a person the admin gave a start time in
    Admin Bot /grafik (e.g. 10:00): late is counted from it, not from
    Verifix's schedule start.
  * **Эркин график** (2026-10-03) = people the admin marked in Admin Bot
    /grafik: they come when work needs them or the Director calls, so they
    are never late or absent — the brief only counts them, and the
    Director's questions still see when they came.
  * **No data** (2026-10-24, the owner: "Verifix didn't work — employees
    blamed the bot") = a day the terminals didn't record: IT marked it on
    Admin Bot /verifix, or a finished working day where at most 20 % of at
    least 4 people judged have any arrival. Nobody is absent or late that
    day (status ``no_data``); the brief says the data is missing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import fmt_date, today_local
from integrations.common.translit import name_to_cyrillic
from integrations.telegram.bot import escape

log = setup_logging("verifix-attendance")

# Category -> how the Director reads it.
EXCUSES = {
    "sick": "касаллик варақаси",
    "vacation": "таътил",
    "unpaid": "ҳақ тўланмайдиган таътил",
    "trip": "хизмат сафари",
    "day_off": "жавоб олган",
    "hourly_off": "соатбай жавоб",
    "by_manager": "раҳбар рухсати билан",  # an excused day the Director / Garmin manager set (2026-10-23)
}
FULL_DAY_EXCUSES = ("sick", "vacation", "unpaid", "trip", "day_off")

# (category, words in the kind's name) — first match wins, so the specific
# ones ("неоплачиваемый отпуск", "почасовой отгул") come before the general.
_KIND_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("unpaid", ("неоплачиваем", "без сохранения", "ҳақ тўланмайдиган")),
    ("hourly_off", ("почасов", "соатбай")),
    ("late", ("опоздан", "кечик")),
    ("early", ("ранний уход", "эрта кет")),
    ("absent", ("отсутств", "прогул", "келмаган")),
    ("sick", ("больничн", "касал")),
    ("trip", ("командиров", "сафар")),
    ("vacation", ("отпуск", "таътил")),
    ("day_off", ("отгул", "жавоб")),
]
# Verifix's standard letter codes, when a name says nothing recognisable.
_KIND_LETTERS = {"ОП": "late", "РУ": "early", "ОТС": "absent", "Б": "sick", "К": "trip", "ОТ": "vacation",
                 "НО": "unpaid", "О": "day_off", "ПО": "hourly_off"}


def classify_kinds(kinds: Iterable[dict[str, Any]]) -> dict[str, str]:
    """``time_kind_id`` -> category ("late", "sick"...; "other" for the rest)."""
    result: dict[str, str] = {}
    for kind in kinds:
        name = str(kind.get("name") or "").lower()
        category = next((cat for cat, words in _KIND_WORDS if any(w in name for w in words)), None)
        if category is None:
            category = _KIND_LETTERS.get(str(kind.get("letter_code") or "").strip().upper(), "other")
        result[str(kind.get("time_kind_id"))] = category
    return result


def parse_time(value: Any) -> datetime | None:
    """Verifix date-times: "dd.mm.yyyy hh:mm:ss" (also seen: "yyyy-mm-dd hh:mm:ss")."""
    text = str(value or "").strip()
    for pattern in ("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


@dataclass
class DayRecord:
    """One employee's one day."""

    employee_id: str
    name: str  # Cyrillic
    job: str
    day: date
    working: bool
    begin: datetime | None = None
    end: datetime | None = None
    arrived: datetime | None = None
    left: datetime | None = None
    categories: set[str] = field(default_factory=set)
    status: str = "off"  # off | on_time | present | late | absent | not_yet | before_start | excused | flexible | no_data
    late_minutes: int = 0
    early_minutes: int = 0
    excuse: str | None = None
    flexible: bool = False  # on "эркин график" (Admin Bot /grafik)


def _minutes(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds() // 60)


def records(
    rows: Iterable[dict[str, Any]], kinds: dict[str, str], *, grace: int, now: datetime | None = None,
    flexible: set[str] | frozenset[str] = frozenset(), starts: dict[str, time] | None = None,
) -> list[DayRecord]:
    """Turn timesheet rows into per-day records with a status.

    Args:
        rows: ``timesheet$export`` data.
        kinds: ``classify_kinds`` of the company's time kinds.
        grace: Minutes after the start that still count as on time.
        now: Tashkent wall time (naive) when looking at a day still in
            progress; None for finished days.
        flexible: Verifix ids of the people on "эркин график".
        starts: Verifix id -> the person's own start time (Admin Bot /grafik), used instead of the schedule's.
    """
    starts = starts or {}
    out: list[DayRecord] = []
    for row in rows:
        name = name_to_cyrillic(str(row.get("employee_name") or "")) or f"#{row.get('employee_id')}"
        for day_row in row.get("days") or []:
            day = parse_time(day_row.get("date"))
            if day is None:
                continue
            rec = DayRecord(
                employee_id=str(row.get("employee_id")),
                name=name,
                job=str(row.get("job_name") or ""),
                day=day.date(),
                working=str(day_row.get("day_kind") or "").upper() == "W",
                begin=parse_time(day_row.get("begin_time")),
                end=parse_time(day_row.get("end_time")),
                arrived=parse_time(day_row.get("input_time")),
                left=parse_time(day_row.get("output_time")),
                categories={
                    kinds.get(str(f.get("time_kind_id")), "other")
                    for f in day_row.get("facts") or []
                    if _positive(f.get("fact_value"))
                },
                flexible=str(row.get("employee_id")) in flexible,
            )
            own = starts.get(rec.employee_id)
            if own is not None and rec.working:
                rec.begin = datetime.combine(rec.day, own)
            _judge(rec, grace, now)
            out.append(rec)
    return out


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _judge(rec: DayRecord, grace: int, now: datetime | None) -> None:
    if not rec.working:
        rec.status = "off"
        return
    if rec.flexible:
        # Эркин график: no start time to be late for, and a day without
        # coming in isn't an absence.
        rec.status = "flexible"
        return
    full_day = next((c for c in FULL_DAY_EXCUSES if c in rec.categories), None)
    if full_day:
        rec.status, rec.excuse = "excused", EXCUSES[full_day]
        return
    if rec.arrived is None:
        in_progress = now is not None and rec.day == now.date()
        if in_progress and rec.begin is not None and now < rec.begin + timedelta(minutes=grace):
            rec.status = "before_start"
        elif in_progress and (rec.end is None or now < rec.end):
            rec.status = "not_yet"
        else:
            rec.status = "absent"
        return
    if rec.begin is None:
        rec.status = "present"  # hourly/flexible schedule: no start time to be late for
    else:
        late = _minutes(rec.arrived, rec.begin)
        if late > grace and "hourly_off" in rec.categories:
            rec.status, rec.excuse = "excused", EXCUSES["hourly_off"]
        elif late > grace:
            rec.status, rec.late_minutes = "late", late
        else:
            rec.status = "on_time"
    finished = now is None or (rec.end is not None and now >= rec.end)
    if finished and rec.left and rec.end:
        early = _minutes(rec.end, rec.left)
        if early > grace:
            rec.early_minutes = early


@dataclass
class DaySummary:
    """One day across everyone who had a working day."""

    day: date
    scheduled: int = 0
    late: list[DayRecord] = field(default_factory=list)
    absent: list[DayRecord] = field(default_factory=list)
    not_yet: list[DayRecord] = field(default_factory=list)
    excused: list[DayRecord] = field(default_factory=list)
    no_data: list[DayRecord] = field(default_factory=list)  # Verifix didn't record them that day (2026-10-24)
    arrived: int = 0
    flexible: int = 0  # on "эркин график" with a working day — counted, never judged
    flexible_came: int = 0


def summarize(recs: Iterable[DayRecord], day: date) -> DaySummary:
    """The exceptions of one day (non-working days and "before start" left out)."""
    summary = DaySummary(day=day)
    for rec in recs:
        if rec.day != day or rec.status in ("off", "before_start"):
            continue
        if rec.status == "flexible":
            summary.flexible += 1
            summary.flexible_came += rec.arrived is not None
            continue
        if rec.status == "no_data":
            summary.no_data.append(rec)
            continue
        summary.scheduled += 1
        if rec.arrived is not None:
            summary.arrived += 1
        if rec.status == "late":
            summary.late.append(rec)
        elif rec.status == "absent":
            summary.absent.append(rec)
        elif rec.status == "not_yet":
            summary.not_yet.append(rec)
        elif rec.status == "excused":
            summary.excused.append(rec)
    summary.late.sort(key=lambda r: -r.late_minutes)
    return summary


def render_day(summary: DaySummary, max_lines: int = 5) -> str | None:
    """The brief's attendance block, or None when nobody had a working day."""
    label = fmt_date(summary.day)
    flexible = f" · эркин графикда {summary.flexible} киши" if summary.flexible else ""
    no_data = (f"⚪ <b>Давомат ({label}):</b> Verifix {len(summary.no_data)} кишининг келишини ёзмаган (қурилма "
               "ишламаган) — улар келмади ёки кечикди деб ҳисобланмади\n") if summary.no_data else ""
    if summary.scheduled == 0:
        if not summary.flexible:
            return no_data or None
        return no_data + (f"🕘 <b>Давомат ({label}):</b> фақат эркин графикдагилар — {summary.flexible} киши, "
                          f"{summary.flexible_came} таси келди\n")
    missing = summary.absent + summary.not_yet
    parts = []
    if summary.late:
        parts.append(f"{len(summary.late)} киши кечикди")
    if summary.absent:
        parts.append(f"{len(summary.absent)} киши келмади")
    if summary.not_yet:
        parts.append(f"{len(summary.not_yet)} киши ҳали келмади")
    if summary.excused:
        parts.append(f"{len(summary.excused)} киши сабабли")
    if not (summary.late or missing or summary.excused):
        everyone = "Verifix ёзганларнинг ҳаммаси" if summary.no_data else "ҳамма"  # not «everyone» when some weren't recorded
        return no_data + f"🟢 <b>Давомат ({label}):</b> {everyone} ўз вақтида келди — {summary.scheduled} киши{flexible}\n"

    emoji = "🔴" if missing else "🟡" if summary.late else "🟢"
    if not (summary.late or missing):
        parts.insert(0, "кечиккан ва сабабсиз келмаган йўқ")
    lines = [f"{emoji} <b>Давомат ({label}):</b> {', '.join(parts)} — {summary.scheduled} кишидан{flexible}"]
    entries = (
        [f"{escape(r.name)} — келмади" for r in summary.absent]
        + [f"{escape(r.name)} — ҳали келмади" for r in summary.not_yet]
        + [f"{escape(r.name)} — {r.late_minutes} дақиқа кечикди ({r.arrived:%H:%M})" for r in summary.late]
        + [f"{escape(r.name)} — {r.excuse}" for r in summary.excused]
    )
    lines += [f"   • {entry}" for entry in entries[:max_lines]]
    if len(entries) > max_lines:
        lines.append(f"   <i>+яна {len(entries) - max_lines} та</i>")
    return no_data + "\n".join(lines) + "\n"


def describe(recs: list[DayRecord], today: date, now: datetime) -> str:
    """Plain-text data for OPS Manager Bot's answers ("kim kechikdi?")."""
    grace = settings.verifix_late_grace_minutes
    lines = [
        "Attendance from Verifix (face-ID terminals). Times are Tashkent local. "
        f"Late = first arrival more than {grace} min after the schedule's start; absent = a working "
        "day with no arrival and no recorded sick leave/vacation/trip/day off. Days off are not counted. "
        "People on a flexible schedule (эркин график — they come when work needs them or the Director "
        "calls) are never late or absent: only whether and when they came.",
    ]
    for label, day in (("TODAY", today), ("YESTERDAY", today - timedelta(days=1))):
        s = summarize(recs, day)
        if s.scheduled == 0:
            lines.append(f"{label} ({day}): nobody had a working day.")
            continue
        lines.append(
            f"{label} ({day}{', as of ' + now.strftime('%H:%M') if day == today else ''}): "
            f"{s.scheduled} scheduled, {s.arrived} arrived. "
            f"Late: {_names(s.late, True) or 'nobody'}. "
            f"{'Not arrived yet' if day == today else 'Absent'}: {_names(s.not_yet + s.absent) or 'nobody'}. "
            f"Excused: {', '.join(f'{r.name} ({r.excuse})' for r in s.excused) or 'nobody'}."
            + (f" NO DATA — Verifix didn't record these (the terminal wasn't working; never call them absent or "
               f"late): {_names(s.no_data)}." if s.no_data else "")
        )
        flex = [r for r in recs if r.day == day and r.status == "flexible"]
        if flex:
            lines.append(
                f"{label} flexible schedule (not judged): "
                + ", ".join(f"{r.name} ({'came ' + r.arrived.strftime('%H:%M') if r.arrived else 'not in'})" for r in flex)
                + "."
            )

    by_person: dict[str, list[DayRecord]] = {}
    for rec in recs:
        if rec.working:
            by_person.setdefault(rec.employee_id, []).append(rec)
    if by_person:
        first = min(r.day for r in recs)
        lines.append(f"PER EMPLOYEE, {first} to {today} (working days only):")
        for days in sorted(by_person.values(), key=lambda d: d[0].name):
            name = days[0].name
            if days[0].flexible:
                came = sum(1 for r in days if r.arrived is not None)
                job = f" ({days[0].job})" if days[0].job else ""
                lines.append(f"- {name}{job}: flexible schedule (эркин график) — came on {came} of "
                             f"{len(days)} working day(s); late/absent not counted")
                continue
            late = [r for r in days if r.status == "late"]
            absent = [r for r in days if r.status == "absent"]
            excused = [r for r in days if r.status == "excused"]
            unknown = [r for r in days if r.status == "no_data"]
            early = [r for r in days if r.early_minutes]
            parts = [f"{len(days)} working day(s)", f"late {len(late)}x ({sum(r.late_minutes for r in late)} min total)"]
            parts.append(f"absent {len(absent)}x" + (f" ({', '.join(str(r.day) for r in absent)})" if absent else ""))
            if excused:
                parts.append("excused: " + ", ".join(f"{r.day} {r.excuse}" for r in excused))
            if unknown:
                parts.append("no Verifix data (not judged): " + ", ".join(str(r.day) for r in unknown))
            if early:
                parts.append(f"left early {len(early)}x")
            job = f" ({days[0].job})" if days[0].job else ""
            lines.append(f"- {name}{job}: " + "; ".join(parts))
        events = sorted((r for r in recs if r.status in ("late", "absent")), key=lambda r: (r.day, r.name))
        if events:
            lines.append("EVERY LATE ARRIVAL AND ABSENCE:")
            for r in events:
                if r.status == "late":
                    lines.append(
                        f"- [{r.day}] {r.name}: late {r.late_minutes} min "
                        f"(arrived {r.arrived:%H:%M}, start {r.begin:%H:%M})"
                    )
                else:
                    lines.append(f"- [{r.day}] {r.name}: absent")
    return "\n".join(lines)


def _names(recs: list[DayRecord], minutes: bool = False) -> str:
    return ", ".join(f"{r.name} ({r.late_minutes} min)" if minutes else r.name for r in recs)


async def load(begin: date, end: date, run_id: uuid.UUID | str | None, agent: str,
               now: datetime | None = None) -> list[DayRecord]:
    """Read and judge ``begin``..``end`` from Verifix.

    Raises:
        VerifixError: Verifix isn't configured or couldn't be read.
    """
    from integrations.verifix.client import VerifixClient  # local: keeps this module importable offline

    async with VerifixClient(agent=agent, run_id=run_id) as client:
        kinds = classify_kinds(await client.time_kinds())
        rows = await client.timesheet(begin, end)
    recs = records(rows, kinds, grace=settings.verifix_late_grace_minutes, now=now, flexible=await flexible_ids(),
                   starts=await start_times())
    recs = excuse(recs, await excused_days(recs))
    bad = broken_days(recs, now.date() if now else today_local())
    if bad:
        log.warning("Verifix: {} day(s) with almost no arrivals — treated as no data", len(bad))
    return mark_no_data(recs, bad | await marked_bad_days(recs))


# A day the terminals didn't record (2026-10-24): at most this share of at least this many people judged arrived.
BAD_DAY_MIN_JUDGED = 4
BAD_DAY_MAX_ARRIVED = 0.2
NO_DATA = "Verifix маълумоти йўқ"


def day_stats(recs: list[DayRecord]) -> dict[date, tuple[int, int]]:
    """Per day: (people judged, of them with an arrival) — counts only, for Admin Bot /verifix (no names)."""
    out: dict[date, tuple[int, int]] = {}
    for rec in recs:
        if rec.working and rec.status not in ("off", "flexible", "excused", "before_start"):
            judged, came = out.get(rec.day, (0, 0))
            out[rec.day] = (judged + 1, came + (rec.arrived is not None))
    return out


def broken_days(recs: list[DayRecord], today: date) -> set[date]:
    """Finished working days on which (almost) nobody judged has an arrival: the device, not the people."""
    by_day: dict[date, list[DayRecord]] = {}
    for rec in recs:
        if rec.day < today and rec.working and rec.status not in ("off", "flexible", "excused", "before_start"):
            by_day.setdefault(rec.day, []).append(rec)
    return {d for d, day in by_day.items() if len(day) >= BAD_DAY_MIN_JUDGED
            and sum(1 for r in day if r.arrived is not None) <= BAD_DAY_MAX_ARRIVED * len(day)}


def mark_no_data(recs: list[DayRecord], days: set[date]) -> list[DayRecord]:
    """On those days nobody is absent, late or «not yet»: they are «no data» (never judged, never asked why)."""
    for rec in recs:
        if rec.day in days and rec.status in ("absent", "late", "not_yet"):
            rec.status, rec.excuse, rec.late_minutes = "no_data", NO_DATA, 0
    return recs


async def marked_bad_days(recs: list[DayRecord]) -> set[date]:
    """The days IT marked «Verifix ишламади» on Admin Bot /verifix; none if they can't be read."""
    if not recs:
        return set()
    try:
        from integrations.org_bot import store

        return await store.verifix_bad_days(min(r.day for r in recs), max(r.day for r in recs))
    except Exception as exc:  # noqa: BLE001 — without the marks the day is judged as before
        log.warning("Could not read the Verifix bad days: {}", type(exc).__name__)
        return set()


async def excused_days(recs: list[DayRecord]) -> list[dict[str, Any]]:
    """The days the Director / Garmin manager excused in this range (excused_days); empty if they can't be read."""
    if not recs:
        return []
    try:
        from integrations.org_bot import store

        return await store.excused_between(min(r.day for r in recs), max(r.day for r in recs))
    except Exception as exc:  # noqa: BLE001 — without them the day is judged as before
        log.warning("Could not read the excused days: {}", type(exc).__name__)
        return []


def excuse(recs: list[DayRecord], rows: list[dict[str, Any]]) -> list[DayRecord]:
    """An absent / late / not-yet day a manager excused becomes «excused» (grey, never counted against them).

    ``rows``: the bot's people with their excused days; a Verifix person is tied to one by name.
    """
    if not rows:
        return recs
    from integrations.org_bot.explanations import match_employee  # local: keeps this module light

    people = list({r["id"]: r for r in rows}.values())
    days = {(r["id"], r["day"]) for r in rows}
    tie: dict[str, str | None] = {}
    for rec in recs:
        if rec.status not in ("absent", "late", "not_yet"):
            continue
        if rec.employee_id not in tie:
            hit = match_employee(rec.name, people)
            tie[rec.employee_id] = hit["id"] if hit else None
        if tie[rec.employee_id] and (tie[rec.employee_id], rec.day) in days:
            rec.status, rec.excuse, rec.late_minutes = "excused", EXCUSES["by_manager"], 0
    return recs


async def start_times() -> dict[str, time]:
    """Verifix id -> the person's own start time (Admin Bot /grafik); empty if it can't be read."""
    try:
        from integrations.org_bot import store

        return await store.attendance_start_times()
    except Exception as exc:  # noqa: BLE001 — without them Verifix's own start is used, as before
        log.warning("Could not read the start times: {}", type(exc).__name__)
        return {}


async def flexible_ids() -> set[str]:
    """Who is on "эркин график" (Admin Bot /grafik); empty if it can't be read."""
    try:
        from integrations.org_bot import store  # local: keeps this module importable offline

        return await store.flexible_schedule_ids()
    except Exception as exc:  # noqa: BLE001 — without the marks everyone is judged as before, not nobody
        log.warning("Could not read the flexible-schedule marks: {}", exc)
        return set()
