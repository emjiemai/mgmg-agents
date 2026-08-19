"""Verifix HR client — attendance exceptions for the CEO brief.

Verifix can be read two ways, selected by ``VERIFIX_MODE``:

    api  — REST endpoint with a bearer token
    csv  — the daily export that Verifix mails out, dropped into
           ``VERIFIX_CSV_DIR`` as ``attendance_YYYY-MM-DD.csv``

CSV is the default because the API token is not issued yet. Both modes return
the same ``AttendanceRecord`` objects, so switching later is a one-line env
change with no agent code touched.

The CSV column names below follow the standard Verifix export. If your export
differs, adjust ``COLUMN_ALIASES`` — that is the only place column names live.
"""

from __future__ import annotations

import csv
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import httpx
from pydantic import BaseModel

from integrations.common import divisions
from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import TASHKENT, to_utc, today_local

log = setup_logging("verifix")

# Canonical field -> accepted header spellings in the CSV export (case-insensitive).
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "employee_id": ("employee_id", "tabel", "tabel_id", "id", "табельный номер"),
    "employee_name": ("employee_name", "fio", "full_name", "name", "фио"),
    "department": ("department", "otdel", "подразделение", "отдел"),
    "scheduled_start": ("scheduled_start", "shift_start", "plan_start", "начало смены"),
    "checked_in_at": ("checked_in_at", "check_in", "first_in", "вход"),
    "status": ("status", "state", "статус"),
    "late_minutes": ("late_minutes", "late", "opozdanie", "опоздание"),
}

# Check-ins within this many minutes of the shift start are not lateness.
# Verifix Face ID timestamps are exact, so a small grace window keeps the
# brief free of one-minute "exceptions".
LATE_GRACE_MINUTES = 5

STATUS_ALIASES: dict[str, str] = {
    "present": "present",
    "ok": "present",
    "на месте": "present",
    "late": "late",
    "опоздал": "late",
    "опоздание": "late",
    "absent": "absent",
    "отсутствует": "absent",
    "прогул": "absent",
    "leave": "leave",
    "отпуск": "leave",
    "больничный": "leave",
    "holiday": "holiday",
    "выходной": "holiday",
}


class AttendanceRecord(BaseModel):
    """One employee's attendance state for one day."""

    employee_id: str
    employee_name: str | None = None
    department: str | None = None
    division: str | None = None
    scheduled_start: datetime | None = None
    checked_in_at: datetime | None = None
    late_minutes: int = 0
    status: str = "unknown"


class AttendanceSummary(BaseModel):
    """Attendance exceptions for one day."""

    snapshot_date: date
    late: list[AttendanceRecord] = []
    absent: list[AttendanceRecord] = []
    total_records: int = 0
    source: str = "verifix"

    @property
    def has_exceptions(self) -> bool:
        """True when anyone was late or absent."""
        return bool(self.late or self.absent)


class VerifixError(RuntimeError):
    """Raised when attendance data cannot be obtained."""


class VerifixClient:
    """Attendance reader for Verifix, in API or CSV mode.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self.mode = settings.verifix_mode.lower()

    async def get_attendance(self, day: date | None = None) -> AttendanceSummary:
        """Fetch attendance records for a day and split out the exceptions.

        Args:
            day: Business day in Tashkent; defaults to today.

        Returns:
            An ``AttendanceSummary``. A missing CSV or unreachable API is
            reported as an empty summary with ``source`` marked unavailable —
            HR data missing must not stop the CEO brief from going out.

        Raises:
            VerifixError: only when ``VERIFIX_MODE`` is not 'api' or 'csv'.
        """
        day = day or today_local()

        if self.mode == "api":
            records = await self._read_api(day)
        elif self.mode == "csv":
            records = self._read_csv(day)
        else:
            raise VerifixError(f"Unknown VERIFIX_MODE '{self.mode}' — use 'api' or 'csv'")

        summary = AttendanceSummary(
            snapshot_date=day,
            late=[r for r in records if r.status == "late"],
            absent=[r for r in records if r.status == "absent"],
            total_records=len(records),
            source=f"verifix:{self.mode}" if records else f"verifix:{self.mode}:unavailable",
        )
        summary.late.sort(key=lambda r: r.late_minutes, reverse=True)

        log.info(
            "Verifix {}: {} record(s), {} late, {} absent",
            day,
            summary.total_records,
            len(summary.late),
            len(summary.absent),
        )
        return summary

    # ------------------------------------------------------------------ modes

    async def _read_api(self, day: date) -> list[AttendanceRecord]:
        """Read attendance from the Verifix REST API.

        Args:
            day: Business day to fetch.

        Returns:
            Parsed records; empty list if the API is unreachable or unconfigured.
        """
        if not settings.verifix_base_url or not settings.verifix_api_token.get_secret_value():
            log.warning("Verifix API mode selected but VERIFIX_BASE_URL/TOKEN are unset")
            return []

        try:
            async with httpx.AsyncClient(
                base_url=settings.verifix_base_url.rstrip("/"),
                timeout=httpx.Timeout(30.0),
                headers={"Authorization": f"Bearer {settings.verifix_api_token.get_secret_value()}"},
            ) as client:
                async with audited(
                    agent=self.agent,
                    action="api_call",
                    target_system="verifix",
                    run_id=self.run_id,
                    target_ref="/attendance",
                    payload={"date": day.isoformat()},
                ) as ctx:
                    response = await request_with_retry(
                        client, "GET", "/attendance", params={"date": day.isoformat()}
                    )
                    ctx["http_status"] = response.status_code
                    if response.status_code != 200:
                        raise VerifixError(f"HTTP {response.status_code}: {response.text[:300]}")
                    rows = response.json()
                    rows = rows.get("data", rows) if isinstance(rows, dict) else rows
                    ctx["payload"]["rows"] = len(rows)
        except (httpx.HTTPError, VerifixError, ValueError) as err:
            log.error("Verifix API read failed: {}", err)
            return []

        return [self._to_record(row, day) for row in rows]

    def _read_csv(self, day: date) -> list[AttendanceRecord]:
        """Read attendance from the daily CSV export.

        Looks for ``attendance_YYYY-MM-DD.csv`` in ``VERIFIX_CSV_DIR``, falling
        back to the most recent ``attendance_*.csv`` if that exact file is not
        there yet (the export sometimes lands late).

        Args:
            day: Business day to fetch.

        Returns:
            Parsed records; empty list if no export file exists.
        """
        directory = Path(settings.verifix_csv_dir)
        exact = directory / f"attendance_{day.isoformat()}.csv"

        path: Path | None = None
        if exact.exists():
            path = exact
        elif directory.exists():
            candidates = sorted(directory.glob("attendance_*.csv"))
            if candidates:
                path = candidates[-1]
                log.warning("No export for {} — falling back to {}", day, path.name)

        if path is None:
            log.error("No Verifix CSV export found in {}", directory)
            return []

        with path.open(encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            try:
                dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.DictReader(handle, dialect=dialect))

        log.debug("Verifix CSV {}: {} row(s)", path.name, len(rows))
        return [self._to_record(row, day) for row in rows]

    # ---------------------------------------------------------------- mapping

    def _to_record(self, row: dict[str, Any], day: date) -> AttendanceRecord:
        """Map one raw row (API or CSV) onto an ``AttendanceRecord``.

        Args:
            row: Raw row keyed by whatever the source calls its columns.
            day: The business day, used to anchor time-only values.

        Returns:
            A normalized record. Lateness is computed from the check-in and
            shift-start times when the export does not state it, and any record
            more than ``LATE_GRACE_MINUTES`` late is classified 'late' even if
            the export called it 'present'. Statuses that are explicitly
            'absent', 'leave' or 'holiday' are left alone.
        """
        normalized = {str(k).strip().lower(): v for k, v in row.items() if k}

        def pick(field: str) -> Any:
            for alias in COLUMN_ALIASES[field]:
                if alias in normalized and normalized[alias] not in ("", None):
                    return normalized[alias]
            return None

        department = pick("department")
        checked_in = _to_datetime(pick("checked_in_at"), day)
        scheduled = _to_datetime(pick("scheduled_start"), day)

        # Lateness is established before the status is decided, because some
        # exports mark a visibly late check-in as "present" and leave the late
        # column empty — trusting the status alone would hide the exception.
        late_minutes = _to_int(pick("late_minutes"))
        if late_minutes == 0 and checked_in and scheduled:
            late_minutes = max(int((checked_in - scheduled).total_seconds() // 60), 0)

        raw_status = str(pick("status") or "").strip().lower()
        status = STATUS_ALIASES.get(raw_status, "unknown")
        if status in ("present", "unknown") and late_minutes > LATE_GRACE_MINUTES:
            status = "late"

        return AttendanceRecord(
            employee_id=str(pick("employee_id") or "").strip() or "unknown",
            employee_name=_clean(pick("employee_name")),
            department=_clean(department),
            division=divisions.division_from_verifix(_clean(department)),
            scheduled_start=scheduled,
            checked_in_at=checked_in,
            late_minutes=late_minutes,
            status=status,
        )


async def persist_attendance(summary: AttendanceSummary, records: Iterable[AttendanceRecord]) -> int:
    """Write an attendance snapshot to PostgreSQL.

    Args:
        summary: The summary being persisted (supplies the snapshot date).
        records: Records to store — normally the exceptions only.

    Returns:
        Number of rows written.

    Raises:
        psycopg.Error: on a database failure.
    """
    from integrations.common.db import execute_many

    rows = [
        (
            summary.snapshot_date,
            r.employee_id,
            r.employee_name,
            r.division,
            r.department,
            r.scheduled_start,
            r.checked_in_at,
            r.late_minutes,
            r.status,
            summary.source,
        )
        for r in records
    ]
    return await execute_many(
        """
        INSERT INTO attendance_snapshots
            (snapshot_date, employee_id, employee_name, division, department,
             scheduled_start, checked_in_at, late_minutes, status, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_date, employee_id) DO UPDATE SET
            late_minutes = EXCLUDED.late_minutes,
            status       = EXCLUDED.status,
            checked_in_at = EXCLUDED.checked_in_at,
            captured_at  = now()
        """,
        rows,
    )


def _clean(value: Any) -> str | None:
    """Trim a string field, returning None for blanks."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int:
    """Parse an integer from a CSV/JSON field, returning 0 on failure."""
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def _to_datetime(value: Any, day: date) -> datetime | None:
    """Parse a timestamp or bare time into an aware UTC datetime.

    Verifix exports wall-clock Tashkent time; times without a date are anchored
    to ``day``.

    Args:
        value: ``"2026-08-18 09:12"``, ``"09:12"``, or an ISO string.
        day: The business day used to anchor time-only values.

    Returns:
        Aware UTC datetime, or ``None`` if unparseable.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()

    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern.startswith("%H"):
            parsed = datetime.combine(day, parsed.time())
        return to_utc(parsed.replace(tzinfo=TASHKENT))

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return to_utc(parsed)
