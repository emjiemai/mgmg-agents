"""Shared query layer for the org_bot package.

The one deliberate departure from this codebase's usual inline-SQL-per-module
style (every existing example inlines its own SQL because each has a single
owning module) — justified here because ``employees`` is read/written by both
``admin.py`` and ``ops_manager.py``, so a shared layer avoids duplicating the
same lookups in two files.

Every guarded UPDATE here checks the affected row count and treats zero as
"already handled" rather than trusting a prior SELECT, so an employee
double-tapping a button on a slow connection can't apply the same change twice.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from integrations.common.db import execute, fetch_all, fetch_one
from integrations.common.timeutil import today_local

# ------------------------------------------------------------------ employees


async def get_employee_by_telegram_id(telegram_user_id: int) -> dict[str, Any] | None:
    """Look up a registered, active-or-revoked employee by Telegram user id.

    Args:
        telegram_user_id: The sender's Telegram numeric id.

    Returns:
        The employee row, or None if never registered.
    """
    return await fetch_one(
        "SELECT * FROM employees WHERE telegram_user_id = %s", (telegram_user_id,)
    )


async def set_employee_full_name(telegram_user_id: int, full_name: str) -> None:
    """Remember the name a person gave for official documents."""
    await execute("UPDATE employees SET full_name = %s WHERE telegram_user_id = %s", (full_name, telegram_user_id))


async def create_employee(
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str,
    role: str,
    approved_by: str | None,
) -> dict[str, Any]:
    """Register a newly-approved employee with their chosen role.

    Args:
        telegram_user_id: The employee's Telegram numeric id.
        telegram_username: Their @username, if set.
        display_name: Full name shown in task cards / admin notifications.
        role: One of ``roles.ROLE_SLUGS``.
        approved_by: The deciding admin's identifier, for the audit trail.

    Returns:
        The new employee row.

    Raises:
        psycopg.Error: on a database failure, including a role outside the
            CHECK constraint (should never happen — callers validate against
            ``roles.ROLE_SLUGS`` first).
    """
    row = await fetch_one(
        """
        INSERT INTO employees (telegram_user_id, telegram_username, display_name, role, approved_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (telegram_user_id) DO UPDATE
            SET role = EXCLUDED.role, status = 'active', approved_by = EXCLUDED.approved_by
        RETURNING *
        """,
        (telegram_user_id, telegram_username, display_name, role, approved_by),
    )
    assert row is not None
    return row


async def active_employees_by_role(role: str) -> list[dict[str, Any]]:
    """List every active employee holding a given role.

    Args:
        role: One of ``roles.ROLE_SLUGS``.

    Returns:
        Matching employee rows (possibly empty — callers must handle "no one
        registered for this role yet" explicitly, not silently).
    """
    return await fetch_all(
        "SELECT * FROM employees WHERE role = %s AND status = 'active'", (role,)
    )


async def list_active_employees() -> list[dict[str, Any]]:
    """List every active employee, for Admin Bot's removal UI.

    Returns:
        All active employee rows, ordered by role then name.
    """
    return await fetch_all("SELECT * FROM employees WHERE status = 'active' ORDER BY role, display_name")


async def revoke_employee(employee_id: str, revoked_by: str) -> dict[str, Any] | None:
    """Revoke an employee's access, idempotently.

    Args:
        employee_id: ``employees.id``.
        revoked_by: The admin's identifier, for the audit trail.

    Returns:
        The updated row if this call actually changed it, else None —
        callers must treat None as "already revoked / not found", not an error.
    """
    return await fetch_one(
        """
        UPDATE employees
        SET status = 'revoked', revoked_at = now(), revoked_by = %s
        WHERE id = %s AND status = 'active'
        RETURNING *
        """,
        (revoked_by, employee_id),
    )


# ------------------------------------------------------------- access requests


async def get_pending_access_request(telegram_user_id: int) -> dict[str, Any] | None:
    """Find an already-pending join request for this user, if any.

    Args:
        telegram_user_id: The requester's Telegram numeric id.

    Returns:
        The pending row, or None.
    """
    return await fetch_one(
        "SELECT * FROM access_requests WHERE telegram_user_id = %s AND status = 'pending'",
        (telegram_user_id,),
    )


async def create_access_request(
    *, telegram_user_id: int, telegram_username: str | None, display_name: str | None
) -> dict[str, Any] | None:
    """Create a pending join request, unless one is already pending.

    Relies on the partial unique index ``uq_access_requests_pending`` rather
    than a select-then-insert check, which would have its own race — a user
    who sends 3 messages while waiting for approval must not spam the admin
    with 3 duplicate Accept/Reject cards.

    Args:
        telegram_user_id: The requester's Telegram numeric id.
        telegram_username: Their @username, if set.
        display_name: Full name for the admin's notification card.

    Returns:
        The new row, or None if a pending request already existed (call
        ``get_pending_access_request`` to fetch the existing one).
    """
    return await fetch_one(
        """
        INSERT INTO access_requests (telegram_user_id, telegram_username, display_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_user_id) WHERE status = 'pending' DO NOTHING
        RETURNING *
        """,
        (telegram_user_id, telegram_username, display_name),
    )


async def get_access_request(request_id: str) -> dict[str, Any] | None:
    """Fetch one access request by id.

    Args:
        request_id: ``access_requests.id``.

    Returns:
        The row, or None if it doesn't exist.
    """
    return await fetch_one("SELECT * FROM access_requests WHERE id = %s", (request_id,))


async def set_access_request_message_id(request_id: str, message_id: int) -> None:
    """Record the admin card's Telegram message id, for later in-place edits.

    Args:
        request_id: ``access_requests.id``.
        message_id: The message id returned by ``sendMessage``.
    """
    await execute(
        "UPDATE access_requests SET admin_message_id = %s WHERE id = %s", (message_id, request_id)
    )


async def decide_access_request(
    request_id: str, decision: Literal["approved", "rejected"], decided_by: str
) -> bool:
    """Resolve a pending join request, idempotently.

    Args:
        request_id: ``access_requests.id``.
        decision: 'approved' or 'rejected'.
        decided_by: The deciding admin's identifier.

    Returns:
        True if this call actually changed the row (first decision wins);
        False if it was already decided — callers must treat False as
        "already handled", not as an error.
    """
    rows_affected = await execute(
        """
        UPDATE access_requests
        SET status = %s, decided_at = now(), decided_by = %s
        WHERE id = %s AND status = 'pending'
        """,
        (decision, decided_by, request_id),
    )
    return rows_affected > 0


async def request_role(request_id: str, role: str) -> dict[str, Any] | None:
    """Record the role an accepted requester picked, pending the admin's confirmation.

    Only allowed when no role request is outstanding — the first pick, or a
    new pick after the admin rejected the previous one — so a double-tap or a
    second button press can't swap the role under a card the admin is
    already looking at.

    Args:
        request_id: ``access_requests.id``.
        role: One of ``roles.ROLE_SLUGS``.

    Returns:
        The updated row, or None if a role is already pending/approved or the
        person was never accepted — callers must treat None as "already
        handled", not an error.
    """
    return await fetch_one(
        """
        UPDATE access_requests
        SET requested_role = %s, role_status = 'pending',
            role_decided_at = NULL, role_decided_by = NULL
        WHERE id = %s AND status = 'approved'
          AND (role_status IS NULL OR role_status = 'rejected')
        RETURNING *
        """,
        (role, request_id),
    )


async def set_role_admin_message_id(request_id: str, message_id: int) -> None:
    """Record the admin's role-request card message id, for in-place edits.

    Args:
        request_id: ``access_requests.id``.
        message_id: The message id returned by ``sendMessage``.
    """
    await execute(
        "UPDATE access_requests SET role_admin_message_id = %s WHERE id = %s", (message_id, request_id)
    )


async def decide_role_request(
    request_id: str, decision: Literal["approved", "rejected"], decided_by: str
) -> dict[str, Any] | None:
    """Resolve a pending role request, idempotently.

    Args:
        request_id: ``access_requests.id``.
        decision: 'approved' or 'rejected'.
        decided_by: The deciding admin's identifier.

    Returns:
        The updated row if this call made the decision (first tap wins), or
        None if there was no pending role request to decide.
    """
    return await fetch_one(
        """
        UPDATE access_requests
        SET role_status = %s, role_decided_at = now(), role_decided_by = %s
        WHERE id = %s AND role_status = 'pending'
        RETURNING *
        """,
        (decision, decided_by, request_id),
    )


# ------------------------------------------------------------------------ tasks


async def create_task(
    *,
    director_telegram_user_id: int,
    source_message_id: int,
    raw_message: str,
    target_type: Literal["employee", "agent"],
    target_role: str | None,
    target_agent: str | None,
    assigned_employee_id: str | None,
    task_summary: str,
    has_media: bool = False,
    due_date: date | None = None,
) -> dict[str, Any] | None:
    """Create one task-dispatch row (one per recipient employee).

    Args:
        director_telegram_user_id: The Director's Telegram numeric id.
        source_message_id: Telegram message id of the Director's original
            request — part of the dedupe key against duplicate webhook delivery.
        raw_message: The Director's original free-text message (or the
            caption, for a media dispatch).
        target_type: 'employee' or 'agent'.
        target_role: Role slug, when ``target_type == 'employee'``.
        target_agent: Agent slug, when ``target_type == 'agent'``.
        assigned_employee_id: The specific employee this row is for
            (None only for an 'agent'-type row, which isn't expected to be
            written via this function — agent queries don't create tasks).
        task_summary: Short model-written summary shown to the recipient.
        has_media: True if this was delivered via ``copyMessage`` (photo/
            video/audio/voice/document/animation) rather than plain text —
            determines whether later edits use ``editMessageCaption`` instead
            of ``editMessageText``.
        due_date: The deadline the Director stated, if any (A3).

    Returns:
        The new row, or None if this exact (director, message, employee)
        combination was already dispatched (duplicate webhook delivery).
    """
    return await fetch_one(
        """
        INSERT INTO tasks
            (director_telegram_user_id, source_message_id, raw_message,
             target_type, target_role, target_agent, assigned_employee_id, task_summary, has_media, due_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT uq_task_dispatch DO NOTHING
        RETURNING *
        """,
        (
            director_telegram_user_id,
            source_message_id,
            raw_message,
            target_type,
            target_role,
            target_agent,
            assigned_employee_id,
            task_summary,
            has_media,
            due_date,
        ),
    )


# ------------------------------------------------------ A3: task deadlines


async def set_dispatch_due_date(
    director_telegram_user_id: int, source_message_id: int, due_date: date
) -> list[dict[str, Any]]:
    """Give every still-open task from one Director message the same deadline.

    Only tasks without a deadline yet are touched, so a second tap on the
    choice buttons can't quietly move a deadline already set.

    Returns:
        The updated rows, each with the employee's Telegram id and name.
    """
    return await fetch_all(
        """
        UPDATE tasks t
        SET due_date = %s
        FROM employees e
        WHERE t.director_telegram_user_id = %s AND t.source_message_id = %s
          AND t.due_date IS NULL AND t.status IN ('sent', 'started')
          AND e.id = t.assigned_employee_id
        RETURNING t.*, e.telegram_user_id AS employee_telegram_user_id, e.display_name
        """,
        (due_date, director_telegram_user_id, source_message_id),
    )


async def tasks_needing_reminder(today: date) -> list[dict[str, Any]]:
    """Open tasks due today or tomorrow that haven't had their reminder yet."""
    return await fetch_all(
        """
        SELECT t.*, e.telegram_user_id AS employee_telegram_user_id, e.display_name
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_employee_id
        WHERE t.status IN ('sent', 'started') AND t.reminded_at IS NULL
          AND t.due_date BETWEEN %s AND %s::date + 1
          AND e.status = 'active'
        ORDER BY t.due_date, t.created_at
        """,
        (today, today),
    )


async def mark_task_reminded(task_id: str) -> None:
    """Record that a task's one reminder went out."""
    await execute("UPDATE tasks SET reminded_at = now() WHERE id = %s", (task_id,))


async def tasks_newly_overdue(today: date) -> list[dict[str, Any]]:
    """Open tasks past their deadline whose overdue notice hasn't gone out."""
    return await fetch_all(
        """
        SELECT t.*, e.telegram_user_id AS employee_telegram_user_id, e.display_name
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_employee_id
        WHERE t.status IN ('sent', 'started') AND t.overdue_notified_at IS NULL
          AND t.due_date < %s
        ORDER BY t.director_telegram_user_id, t.due_date
        """,
        (today,),
    )


async def mark_task_overdue_notified(task_id: str) -> None:
    """Record that a task's one overdue notice went out."""
    await execute("UPDATE tasks SET overdue_notified_at = now() WHERE id = %s", (task_id,))


async def tasks_due_between(start: date, end: date) -> list[dict[str, Any]]:
    """Every task whose deadline falls in ``[start, end]``, with the employee's name."""
    return await fetch_all(
        """
        SELECT t.id, t.task_summary, t.status, t.due_date, t.completed_at, t.created_at,
               t.director_telegram_user_id, e.display_name, e.role
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_employee_id
        WHERE t.due_date BETWEEN %s AND %s
        ORDER BY t.due_date, e.display_name
        """,
        (start, end),
    )


async def open_tasks_with_names(limit: int = 60) -> list[dict[str, Any]]:
    """Every unfinished task, oldest deadline first, for the Director's questions."""
    return await fetch_all(
        """
        SELECT t.task_summary, t.status, t.due_date, t.created_at, e.display_name, e.role
        FROM tasks t
        JOIN employees e ON e.id = t.assigned_employee_id
        WHERE t.status IN ('sent', 'started')
        ORDER BY t.due_date NULLS LAST, t.created_at
        LIMIT %s
        """,
        (limit,),
    )


async def reports_between(start: date, end: date) -> list[dict[str, Any]]:
    """Every daily report row in ``[start, end]``, with the employee's name (A1's И)."""
    return await fetch_all(
        """
        SELECT r.report_date, r.status, r.submitted_at, e.display_name, e.role
        FROM daily_reports r
        JOIN employees e ON e.id = r.employee_id
        WHERE r.report_date BETWEEN %s AND %s
        ORDER BY r.report_date, e.display_name
        """,
        (start, end),
    )


async def permission_counts_between(start: date, end: date) -> dict[str, int]:
    """Permission requests submitted in ``[start, end]`` (Tashkent), by status."""
    rows = await fetch_all(
        """
        SELECT status, count(*) AS n FROM permission_requests
        WHERE submitted_at IS NOT NULL
          AND (submitted_at AT TIME ZONE 'Asia/Tashkent')::date BETWEEN %s AND %s
        GROUP BY status
        """,
        (start, end),
    )
    return {row["status"]: int(row["n"]) for row in rows}


async def set_task_message_id(task_id: str, message_id: int) -> None:
    """Record a task card's Telegram message id, for later in-place edits.

    Args:
        task_id: ``tasks.id``.
        message_id: The message id returned by ``sendMessage``/``copyMessage``.
    """
    await execute("UPDATE tasks SET telegram_message_id = %s WHERE id = %s", (message_id, task_id))


async def mark_task_started(task_id: str, started_by: str) -> dict[str, Any] | None:
    """Resolve a task as started, idempotently.

    Args:
        task_id: ``tasks.id``.
        started_by: The tapping employee's identifier.

    Returns:
        The updated row if this call actually changed it (first tap wins,
        and only from 'sent' — tapping Start after Done is a no-op), else
        None — callers must treat None as "already started or done", not an error.
    """
    return await fetch_one(
        """
        UPDATE tasks
        SET status = 'started', started_at = now(), started_by = %s
        WHERE id = %s AND status = 'sent'
        RETURNING *
        """,
        (started_by, task_id),
    )


async def mark_task_done(task_id: str, completed_by: str) -> dict[str, Any] | None:
    """Resolve a task as done, idempotently.

    Args:
        task_id: ``tasks.id``.
        completed_by: The tapping employee's identifier.

    Returns:
        The updated row if this call actually changed it (first tap wins;
        valid from either 'sent' or 'started' — Done doesn't require Start
        to have been tapped first), else None — callers must treat None as
        "already done", not an error.
    """
    return await fetch_one(
        """
        UPDATE tasks
        SET status = 'done', completed_at = now(), completed_by = %s
        WHERE id = %s AND status IN ('sent', 'started')
        RETURNING *
        """,
        (completed_by, task_id),
    )


async def get_task(task_id: str) -> dict[str, Any] | None:
    """Fetch one task by id.

    Args:
        task_id: ``tasks.id``.

    Returns:
        The row, or None if it doesn't exist.
    """
    return await fetch_one("SELECT * FROM tasks WHERE id = %s", (task_id,))


# ------------------------------------------------------------- pending dispatches


async def create_pending_dispatch(
    *, director_telegram_user_id: int, source_message_id: int, caption: str | None
) -> dict[str, Any]:
    """Park a media/file dispatch that needs a role picked before it can be sent.

    Args:
        director_telegram_user_id: The Director's Telegram numeric id.
        source_message_id: Telegram message id of the media to later ``copyMessage``.
        caption: The original caption, if any (may be empty/None).

    Returns:
        The new row.
    """
    row = await fetch_one(
        """
        INSERT INTO pending_dispatches (director_telegram_user_id, source_message_id, caption)
        VALUES (%s, %s, %s)
        RETURNING *
        """,
        (director_telegram_user_id, source_message_id, caption),
    )
    assert row is not None
    return row


async def resolve_pending_dispatch(pending_id: str) -> dict[str, Any] | None:
    """Resolve a pending dispatch, idempotently.

    Args:
        pending_id: ``pending_dispatches.id``.

    Returns:
        The row if this call actually resolved it (first tap wins), else
        None — callers must treat None as "already resolved", not an error.
    """
    return await fetch_one(
        """
        UPDATE pending_dispatches
        SET resolved_at = now()
        WHERE id = %s AND resolved_at IS NULL
        RETURNING *
        """,
        (pending_id,),
    )


# --------------------------------------------------------------- task updates


async def create_task_update(
    *,
    task_id: str | None,
    employee_telegram_user_id: int,
    message_text: str,
    director_telegram_user_id: int | None = None,
    director_message_id: int | None = None,
    direction: Literal["employee_to_director", "director_to_employee"] = "employee_to_director",
) -> dict[str, Any]:
    """Record one message in an employee<->Director thread.

    A task progress note when ``task_id`` is set, a general message when
    it's None -- both share this table so a Director sees one continuous
    thread with each employee regardless of whether a task happens to be
    open.

    Args:
        task_id: ``tasks.id`` this relates to, or None for a message not
            tied to any specific task.
        employee_telegram_user_id: The employee's Telegram numeric id (the
            other party in the thread, regardless of ``direction``).
        message_text: The message itself.
        director_telegram_user_id: Which Director this thread is with.
        director_message_id: The Telegram message id of the relay sent to
            the Director's chat, when ``direction == "employee_to_director"``
            -- lets a Director's reply-to-that-message route back to the
            right employee without going through task classification.
        direction: Which way this message went.

    Returns:
        The new row.
    """
    row = await fetch_one(
        """
        INSERT INTO task_updates
            (task_id, employee_telegram_user_id, message_text,
             director_telegram_user_id, director_message_id, direction)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            task_id,
            employee_telegram_user_id,
            message_text,
            director_telegram_user_id,
            director_message_id,
            direction,
        ),
    )
    assert row is not None
    return row


async def find_relay_by_director_message(
    director_telegram_user_id: int, telegram_message_id: int
) -> dict[str, Any] | None:
    """Resolve a Director's reply-to-message into which employee it's about.

    Scoped by both the Director's id and the message id -- Telegram message
    ids are only unique within one chat, so matching on the message id alone
    could, in principle, cross-match a different Director's chat.

    Args:
        director_telegram_user_id: The replying Director's Telegram numeric id.
        telegram_message_id: ``message.reply_to_message.message_id`` from
            their update.

    Returns:
        The most recent matching relay row, or None if this message id
        isn't a known relay in that Director's chat.
    """
    return await fetch_one(
        """
        SELECT * FROM task_updates
        WHERE direction = 'employee_to_director'
          AND director_telegram_user_id = %s
          AND director_message_id = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (director_telegram_user_id, telegram_message_id),
    )


async def find_task_by_message_id(telegram_message_id: int, employee_telegram_user_id: int) -> dict[str, Any] | None:
    """Find the task a reply-to-message references, scoped to that recipient.

    Scoping by the replying employee (not just the message id) means one
    employee can't attach an update to a task card that was actually sent to
    someone else, even though message ids aren't otherwise employee-specific.

    Args:
        telegram_message_id: ``message.reply_to_message.message_id`` from the update.
        employee_telegram_user_id: The replying employee's Telegram numeric id.

    Returns:
        The matching task, or None.
    """
    return await fetch_one(
        """
        SELECT t.* FROM tasks t
        JOIN employees e ON e.id = t.assigned_employee_id
        WHERE t.telegram_message_id = %s AND e.telegram_user_id = %s
        """,
        (telegram_message_id, employee_telegram_user_id),
    )


async def find_open_task_for_employee(employee_telegram_user_id: int) -> dict[str, Any] | None:
    """The employee's single open task, if exactly one exists.

    Used as a fallback when a progress-update message isn't a reply to any
    specific task card — if the employee has exactly one task in flight, it's
    unambiguous which one they mean; with zero or multiple, it isn't, and
    callers should ask them to reply directly to the right card instead.

    Args:
        employee_telegram_user_id: The employee's Telegram numeric id.

    Returns:
        The task, or None if there isn't exactly one open task.
    """
    rows = await fetch_all(
        """
        SELECT t.* FROM tasks t
        JOIN employees e ON e.id = t.assigned_employee_id
        WHERE e.telegram_user_id = %s AND t.status IN ('sent', 'started')
        ORDER BY t.created_at DESC
        """,
        (employee_telegram_user_id,),
    )
    return rows[0] if len(rows) == 1 else None


# ---------------------------------------------------------- conversation memory


async def log_conversation_turn(telegram_user_id: int, role: Literal["director", "bot"], content: str) -> None:
    """Record one turn of OPS Manager Bot's short-term memory.

    Args:
        telegram_user_id: Whose conversation this belongs to (the Director).
        role: 'director' or 'bot'.
        content: The message text.
    """
    await execute(
        "INSERT INTO conversation_turns (telegram_user_id, role, content) VALUES (%s, %s, %s)",
        (telegram_user_id, role, content),
    )


async def recent_conversation(telegram_user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Fetch recent conversation turns, oldest first (prompt-ready order).

    Args:
        telegram_user_id: Whose conversation to fetch.
        limit: Maximum turns to return — bounded so history can't grow
            unboundedly relevant/irrelevant into every future prompt.

    Returns:
        Up to ``limit`` most recent turns, in chronological order.
    """
    rows = await fetch_all(
        "SELECT role, content, created_at FROM conversation_turns "
        "WHERE telegram_user_id = %s ORDER BY created_at DESC LIMIT %s",
        (telegram_user_id, limit),
    )
    return list(reversed(rows))


# ------------------------------------------------------------- daily reports


async def open_report_request(
    *, employee: dict[str, Any], report_date: date
) -> dict[str, Any] | None:
    """Record that this employee was asked for today's report.

    Idempotent: a second run on the same day returns None rather than asking
    the same person twice, so a retried cron run can't spam anyone.

    Args:
        employee: The employee row being asked.
        report_date: The working day being reported on.

    Returns:
        The new row, or None if this employee was already asked today.
    """
    return await fetch_one(
        """
        INSERT INTO daily_reports (report_date, employee_id, telegram_user_id, role)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (report_date, employee_id) DO NOTHING
        RETURNING *
        """,
        (report_date, str(employee["id"]), employee["telegram_user_id"], employee["role"]),
    )


async def set_report_prompt_message_id(report_id: str, message_id: int) -> None:
    """Record the 16:00 ask's Telegram message id.

    A reply to that exact message is the unambiguous signal that an
    employee's text is their daily report rather than a task update.

    Args:
        report_id: ``daily_reports.id``.
        message_id: Telegram message id of the ask.
    """
    await execute(
        "UPDATE daily_reports SET prompt_message_id = %s WHERE id = %s", (message_id, report_id)
    )


async def pending_report(telegram_user_id: int, report_date: date) -> dict[str, Any] | None:
    """The report this employee was asked for today and hasn't sent yet.

    Reports are accepted until midnight of the day they were asked for (the
    business's rule); after that the ask is closed and counts as missed.

    Args:
        telegram_user_id: The employee's Telegram numeric id.
        report_date: The working day.

    Returns:
        The awaiting row, or None if they were never asked or already answered.
    """
    return await fetch_one(
        """
        SELECT * FROM daily_reports
        WHERE telegram_user_id = %s AND report_date = %s AND status = 'asked'
        """,
        (telegram_user_id, report_date),
    )


async def expired_report_for_prompt(telegram_user_id: int, message_id: int) -> dict[str, Any] | None:
    """An earlier day's unanswered ask that this message replies to, if any."""
    return await fetch_one(
        """
        SELECT * FROM daily_reports
        WHERE telegram_user_id = %s AND prompt_message_id = %s AND status = 'asked'
        """,
        (telegram_user_id, message_id),
    )


async def save_report(
    *, report_id: str, content: str, metrics: dict[str, int], tasks_done: int
) -> dict[str, Any] | None:
    """Store an employee's answer, idempotently.

    Args:
        report_id: ``daily_reports.id``.
        content: Their written report.
        metrics: Parsed KPI numbers (may be empty).
        tasks_done: Tasks they completed today, counted from ``tasks``.

    Returns:
        The updated row, or None if it was already submitted — callers must
        treat None as "already answered", not an error.
    """
    return await fetch_one(
        """
        UPDATE daily_reports
        SET status = 'submitted', content = %s, metrics = %s::jsonb,
            tasks_done = %s, submitted_at = now()
        WHERE id = %s AND status = 'asked'
        RETURNING *
        """,
        (content, json.dumps(metrics), tasks_done, report_id),
    )


async def submitted_report_today(telegram_user_id: int, report_date: date) -> dict[str, Any] | None:
    """Today's already-submitted report for this employee, if any.

    Used for the common follow-up: someone writes their report in words,
    then sends the numbers in a second message a minute later.

    Args:
        telegram_user_id: The employee's Telegram numeric id.
        report_date: The working day.

    Returns:
        The submitted row, or None.
    """
    return await fetch_one(
        """
        SELECT * FROM daily_reports
        WHERE telegram_user_id = %s AND report_date = %s AND status = 'submitted'
        """,
        (telegram_user_id, report_date),
    )


async def mark_report_followup(report_id: str) -> None:
    """Record that a vague report got its one follow-up question."""
    await execute("UPDATE daily_reports SET followup_asked_at = now() WHERE id = %s", (report_id,))


# The follow-up question on a vague report waits this long for an answer,
# and never past midnight (reports close with the day). After that it lapses
# quietly: the report stands as first sent, and a later message (a task
# update, a question) is never glued onto it by mistake.
REPORT_FOLLOWUP_HOURS = 3


async def open_report_followup(telegram_user_id: int, report_date: date) -> dict[str, Any] | None:
    """Today's report whose follow-up question was asked recently and not answered."""
    return await fetch_one(
        """
        SELECT * FROM daily_reports
        WHERE telegram_user_id = %s AND report_date = %s AND status = 'submitted'
          AND followup_asked_at > now() - make_interval(hours => %s)
          AND followup_answered_at IS NULL
        """,
        (telegram_user_id, report_date, REPORT_FOLLOWUP_HOURS),
    )


async def answer_report_followup(report_id: str, text: str) -> dict[str, Any] | None:
    """Append the follow-up answer to the report, once.

    Returns:
        The updated row, or None if it was already answered.
    """
    return await fetch_one(
        """
        UPDATE daily_reports
        SET content = COALESCE(content, '') || E'\n' || %s, followup_answered_at = now()
        WHERE id = %s AND followup_answered_at IS NULL
        RETURNING *
        """,
        (text, report_id),
    )


async def merge_report_metrics(report_id: str, metrics: dict[str, int]) -> dict[str, Any] | None:
    """Add late-arriving numbers to an already-submitted report.

    Merges rather than replaces, so a second message carrying one number
    cannot wipe the ones already recorded.

    Args:
        report_id: ``daily_reports.id``.
        metrics: Newly parsed values.

    Returns:
        The updated row, or None if the report no longer exists.
    """
    return await fetch_one(
        """
        UPDATE daily_reports
        SET metrics = metrics || %s::jsonb
        WHERE id = %s
        RETURNING *
        """,
        (json.dumps(metrics), report_id),
    )


async def reports_awaiting_reminder(report_date: date) -> list[dict[str, Any]]:
    """Everyone asked today who hasn't answered and hasn't been nudged yet.

    Args:
        report_date: The working day.

    Returns:
        Rows joined with the employee's display name, oldest ask first.
    """
    return await fetch_all(
        """
        SELECT r.*, e.display_name
        FROM daily_reports r
        JOIN employees e ON e.id = r.employee_id
        WHERE r.report_date = %s AND r.status = 'asked' AND r.reminded_at IS NULL
          AND e.status = 'active'
        ORDER BY r.asked_at
        """,
        (report_date,),
    )


async def mark_report_reminded(report_id: str) -> None:
    """Record that the one reminder for this report has been sent.

    Args:
        report_id: ``daily_reports.id``.
    """
    await execute("UPDATE daily_reports SET reminded_at = now() WHERE id = %s", (report_id,))


async def count_tasks_completed(employee_id: str, day: date) -> int:
    """How many tasks this employee marked done on a given day.

    Counted from ``tasks`` rather than asked for, so the discipline figure
    can't be self-reported.

    Args:
        employee_id: ``employees.id``.
        day: The working day, in Tashkent terms.

    Returns:
        Completed task count.
    """
    row = await fetch_one(
        """
        SELECT count(*) AS done FROM tasks
        WHERE assigned_employee_id = %s AND status = 'done'
          AND (completed_at AT TIME ZONE 'Asia/Tashkent')::date = %s
        """,
        (employee_id, day),
    )
    return int(row["done"]) if row else 0


# ------------------------------------------------- written permission requests


# Columns an answer may fill, so a field name can never reach SQL unchecked.
_PERMISSION_TEXT_FIELDS = frozenset(
    {
        "requester_full_name",
        "requester_position",
        "department",
        "subject",
        "reason",
        "execute_by",
        "decision_needed_by",
        "urgency",
        "attachments",
    }
)


async def create_permission_draft(employee: dict[str, Any]) -> dict[str, Any] | None:
    """Open a draft request for this employee.

    Returns:
        The new row, or None if they already have an unfinished draft (one
        per person — the bot fills a request one answer at a time, and two
        drafts would make every answer ambiguous).
    """
    return await fetch_one(
        """
        INSERT INTO permission_requests
            (requester_employee_id, requester_telegram_user_id, requester_name, requester_role)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (requester_telegram_user_id) WHERE status = 'draft' DO NOTHING
        RETURNING *
        """,
        (
            str(employee["id"]),
            employee["telegram_user_id"],
            employee["display_name"],
            employee["role"],
        ),
    )


async def get_permission_draft(telegram_user_id: int) -> dict[str, Any] | None:
    """This person's unfinished draft, if any."""
    return await fetch_one(
        "SELECT * FROM permission_requests WHERE requester_telegram_user_id = %s AND status = 'draft'",
        (telegram_user_id,),
    )


async def set_permission_field(request_id: str, field: str, value: str) -> dict[str, Any] | None:
    """Store one text answer on a draft.

    Args:
        request_id: ``permission_requests.id``.
        field: One of the SOP form's text fields.
        value: The employee's answer.

    Returns:
        The updated row.

    Raises:
        ValueError: if ``field`` isn't a fillable column.
    """
    if field not in _PERMISSION_TEXT_FIELDS:
        raise ValueError(f"not a fillable permission field: {field}")
    return await fetch_one(
        f"UPDATE permission_requests SET {field} = %s WHERE id = %s RETURNING *",  # noqa: S608 — whitelisted above
        (value, request_id),
    )


async def set_permission_amount(
    request_id: str, amount_tiyin: int | None, currency: str, raw: str
) -> dict[str, Any] | None:
    """Store the "Сумма ва валюта" answer.

    Keeps the raw text as well as the parsed figure: an answer the parser
    can't read ("тахминан 2 млн") is still a real answer, and the form shows
    it as typed instead of inventing a number or asking again.
    """
    return await fetch_one(
        """
        UPDATE permission_requests
        SET amount_tiyin = %s, currency = %s, amount_raw = %s
        WHERE id = %s
        RETURNING *
        """,
        (amount_tiyin, currency, raw, request_id),
    )


async def set_permission_pending_field(request_id: str, field: str | None) -> None:
    """Record which answer the bot is waiting for next."""
    await execute("UPDATE permission_requests SET pending_field = %s WHERE id = %s", (field, request_id))


async def submit_permission_request(request_id: str, submitted_to: str) -> dict[str, Any] | None:
    """Assign the request number and hand it to the approvers.

    The number comes from a sequence inside the same statement, so two people
    submitting at once can never share one.

    Returns:
        The submitted row, or None if it wasn't a draft anymore.
    """
    return await fetch_one(
        """
        UPDATE permission_requests
        SET status = 'submitted',
            submitted_at = now(),
            submitted_to = %s,
            pending_field = NULL,
            request_no = 'EMJ-' || to_char(now() AT TIME ZONE 'Asia/Tashkent', 'YYYY') || '-' ||
                         lpad(nextval('permission_request_no_seq')::text, 4, '0')
        WHERE id = %s AND status = 'draft'
        RETURNING *
        """,
        (submitted_to, request_id),
    )


async def get_permission_request(request_id: str) -> dict[str, Any] | None:
    """Fetch one request by id."""
    return await fetch_one("SELECT * FROM permission_requests WHERE id = %s", (request_id,))


async def start_permission_decision(
    request_id: str, decision: str, approver_telegram_user_id: int
) -> dict[str, Any] | None:
    """Park a picked outcome while the approver types their conditions/reason.

    Returns:
        The row if it was still awaiting a decision, else None — so a second
        approver tapping the same card is told it's already handled.
    """
    return await fetch_one(
        """
        UPDATE permission_requests
        SET pending_decision = %s, note_awaited_from = %s
        WHERE id = %s AND status IN ('submitted', 'info_needed')
        RETURNING *
        """,
        (decision, approver_telegram_user_id, request_id),
    )


async def awaiting_permission_note(approver_telegram_user_id: int) -> dict[str, Any] | None:
    """The request whose conditions/reason this approver still owes."""
    return await fetch_one(
        "SELECT * FROM permission_requests WHERE note_awaited_from = %s AND pending_decision IS NOT NULL",
        (approver_telegram_user_id,),
    )


async def decide_permission_request(
    *,
    request_id: str,
    status: str,
    decided_by: str,
    decided_by_telegram_user_id: int,
    approved_terms: str | None,
    decision_note: str | None,
) -> dict[str, Any] | None:
    """Record the approver's decision, idempotently.

    Returns:
        The decided row, or None if it was already decided — callers must
        treat None as "already handled", not an error.
    """
    return await fetch_one(
        """
        UPDATE permission_requests
        SET status = %s, decided_by = %s, decided_by_telegram_user_id = %s,
            approved_terms = %s, decision_note = %s, decided_at = now(),
            pending_decision = NULL, note_awaited_from = NULL
        WHERE id = %s AND status IN ('submitted', 'info_needed')
        RETURNING *
        """,
        (status, decided_by, decided_by_telegram_user_id, approved_terms, decision_note, request_id),
    )


async def cancel_permission_request(request_id: str, telegram_user_id: int) -> dict[str, Any] | None:
    """Let the requester drop their own unfinished draft."""
    return await fetch_one(
        """
        UPDATE permission_requests
        SET status = 'cancelled', pending_field = NULL
        WHERE id = %s AND requester_telegram_user_id = %s AND status = 'draft'
        RETURNING *
        """,
        (request_id, telegram_user_id),
    )


async def add_permission_event(
    *,
    request_id: str,
    actor: str,
    actor_telegram_user_id: int | None,
    action: str,
    detail: str | None = None,
) -> None:
    """Append one line to a request's history (never updated, never deleted)."""
    await execute(
        """
        INSERT INTO permission_request_events
            (request_id, actor, actor_telegram_user_id, action, detail)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (request_id, actor, actor_telegram_user_id, action, detail),
    )


async def permission_events(request_id: str) -> list[dict[str, Any]]:
    """One request's history, oldest first."""
    return await fetch_all(
        "SELECT * FROM permission_request_events WHERE request_id = %s ORDER BY occurred_at",
        (request_id,),
    )


async def permission_registry(days: int = 60) -> list[dict[str, Any]]:
    """The registry the SOP asks the coordinator to keep (§5), newest first.

    Args:
        days: How far back to include.

    Returns:
        Submitted/decided requests — drafts and cancelled ones are left out,
        they were never part of the register.
    """
    return await fetch_all(
        """
        SELECT * FROM permission_requests
        WHERE status <> 'draft' AND status <> 'cancelled'
          AND created_at >= now() - make_interval(days => %s)
        ORDER BY created_at DESC
        """,
        (days,),
    )


async def permission_drafts() -> list[dict[str, Any]]:
    """Requests started but never sent — unfinished, cancelled-by-nobody drafts.

    Kept visible in the registry answer because a request that was filled in
    but never reached an approver (e.g. refused for having no one else to
    decide it) otherwise vanishes: "do we have requests?" would say none.
    """
    return await fetch_all(
        """
        SELECT requester_name, requester_role, subject, created_at, pending_field
        FROM permission_requests WHERE status = 'draft' ORDER BY created_at DESC
        """
    )


async def report_results_before(day: date) -> list[dict[str, Any]]:
    """Every report row from the most recent day before ``day`` that anyone was asked.

    The most recent *asked* day rather than literally yesterday: on a Monday
    morning yesterday is Sunday, when nobody is asked, and the Director still
    needs Friday's result.

    Args:
        day: Usually today; rows strictly before it are considered.

    Returns:
        That day's rows with each employee's name and role, or an empty list
        if nobody has ever been asked.
    """
    return await fetch_all(
        """
        SELECT r.report_date, r.status, e.display_name, e.role
        FROM daily_reports r
        JOIN employees e ON e.id = r.employee_id
        WHERE r.report_date = (SELECT max(report_date) FROM daily_reports WHERE report_date < %s)
        ORDER BY e.display_name
        """,
        (day,),
    )


async def recent_reports(days: int = 14) -> list[dict[str, Any]]:
    """Every daily report row of the last N days, newest first.

    Args:
        days: How far back to look.

    Returns:
        Rows joined with each employee's name and role.
    """
    return await fetch_all(
        """
        SELECT r.report_date, r.status, r.content, r.metrics, r.tasks_done,
               r.submitted_at, e.display_name, e.role
        FROM daily_reports r
        JOIN employees e ON e.id = r.employee_id
        WHERE r.report_date >= %s::date - %s::int
        ORDER BY r.report_date DESC, e.display_name
        """,
        # Tashkent's date, not the database's: in UTC the day turns over five
        # hours late, so current_date was "yesterday" until 05:00 local time.
        (today_local(), days),
    )
