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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from integrations.common.config import settings
from integrations.common.timeutil import fmt_date
from integrations.common.translit import name_to_cyrillic
from integrations.telegram.bot import escape

# Category -> how the Director reads it.
EXCUSES = {
    "sick": "касаллик варақаси",
    "vacation": "таътил",
    "unpaid": "ҳақ тўланмайдиган таътил",
    "trip": "хизмат сафари",
    "day_off": "жавоб олган",
    "hourly_off": "соатбай жавоб",
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
    status: str = "off"  # off | on_time | present | late | absent | not_yet | before_start | excused
    late_minutes: int = 0
    early_minutes: int = 0
    excuse: str | None = None


def _minutes(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds() // 60)


def records(
    rows: Iterable[dict[str, Any]], kinds: dict[str, str], *, grace: int, now: datetime | None = None
) -> list[DayRecord]:
    """Turn timesheet rows into per-day records with a status.

    Args:
        rows: ``timesheet$export`` data.
        kinds: ``classify_kinds`` of the company's time kinds.
        grace: Minutes after the start that still count as on time.
        now: Tashkent wall time (naive) when looking at a day still in
            progress; None for finished days.
    """
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
            )
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
    arrived: int = 0


def summarize(recs: Iterable[DayRecord], day: date) -> DaySummary:
    """The exceptions of one day (non-working days and "before start" left out)."""
    summary = DaySummary(day=day)
    for rec in recs:
        if rec.day != day or rec.status in ("off", "before_start"):
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
    if summary.scheduled == 0:
        return None
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
    label = fmt_date(summary.day)
    if not (summary.late or missing or summary.excused):
        return f"🟢 <b>Давомат ({label}):</b> ҳамма ўз вақтида келди — {summary.scheduled} киши\n"

    emoji = "🔴" if missing else "🟡" if summary.late else "🟢"
    if not (summary.late or missing):
        parts.insert(0, "кечиккан ва сабабсиз келмаган йўқ")
    lines = [f"{emoji} <b>Давомат ({label}):</b> {', '.join(parts)} — {summary.scheduled} кишидан"]
    entries = (
        [f"{escape(r.name)} — келмади" for r in summary.absent]
        + [f"{escape(r.name)} — ҳали келмади" for r in summary.not_yet]
        + [f"{escape(r.name)} — {r.late_minutes} дақиқа кечикди ({r.arrived:%H:%M})" for r in summary.late]
        + [f"{escape(r.name)} — {r.excuse}" for r in summary.excused]
    )
    lines += [f"   • {entry}" for entry in entries[:max_lines]]
    if len(entries) > max_lines:
        lines.append(f"   <i>+яна {len(entries) - max_lines} та</i>")
    return "\n".join(lines) + "\n"


def describe(recs: list[DayRecord], today: date, now: datetime) -> str:
    """Plain-text data for OPS Manager Bot's answers ("kim kechikdi?")."""
    grace = settings.verifix_late_grace_minutes
    lines = [
        "Attendance from Verifix (face-ID terminals). Times are Tashkent local. "
        f"Late = first arrival more than {grace} min after the schedule's start; absent = a working "
        "day with no arrival and no recorded sick leave/vacation/trip/day off. Days off are not counted.",
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
            late = [r for r in days if r.status == "late"]
            absent = [r for r in days if r.status == "absent"]
            excused = [r for r in days if r.status == "excused"]
            early = [r for r in days if r.early_minutes]
            parts = [f"{len(days)} working day(s)", f"late {len(late)}x ({sum(r.late_minutes for r in late)} min total)"]
            parts.append(f"absent {len(absent)}x" + (f" ({', '.join(str(r.day) for r in absent)})" if absent else ""))
            if excused:
                parts.append("excused: " + ", ".join(f"{r.day} {r.excuse}" for r in excused))
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
    return records(rows, kinds, grace=settings.verifix_late_grace_minutes, now=now)
