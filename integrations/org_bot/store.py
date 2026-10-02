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


async def get_employee(employee_id: str) -> dict[str, Any] | None:
    """One employee by ``employees.id``."""
    return await fetch_one("SELECT * FROM employees WHERE id = %s", (employee_id,))


async def log_employee_change(
    employee_id: str, field: str, old_value: str | None, new_value: str | None, changed_by: str
) -> None:
    """Record a name or role change in ``employee_changes``."""
    await execute(
        "INSERT INTO employee_changes (employee_id, field, old_value, new_value, changed_by) VALUES (%s, %s, %s, %s, %s)",
        (employee_id, field, old_value, new_value, changed_by),
    )


async def reset_employee_name(employee_id: str, changed_by: str) -> dict[str, Any] | None:
    """Clear an employee's name so the bot asks for it again.

    The old name goes to ``employee_changes`` first. ``name_asked_at`` is set,
    so their next message is taken as the new name (names.py).

    Returns:
        The updated row, or None if there's no such active employee.
    """
    before = await fetch_one("SELECT * FROM employees WHERE id = %s AND status = 'active'", (employee_id,))
    if before is None:
        return None
    await log_employee_change(employee_id, "full_name", before.get("full_name"), None, changed_by)
    return await fetch_one(
        """
        UPDATE employees SET full_name = NULL, name_asked_at = now(), name_change_requested_at = NULL
        WHERE id = %s AND status = 'active'
        RETURNING *
        """,
        (employee_id,),
    )


async def change_employee_role(employee_id: str, role: str, changed_by: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Move an employee to another role (a new position), logged.

    Returns:
        ``(before, after)``, or None if there's no such active employee or
        the role is unchanged.
    """
    before = await fetch_one("SELECT * FROM employees WHERE id = %s AND status = 'active'", (employee_id,))
    if before is None or before["role"] == role:
        return None
    after = await fetch_one("UPDATE employees SET role = %s WHERE id = %s RETURNING *", (role, employee_id))
    await log_employee_change(employee_id, "role", before["role"], role, changed_by)
    return before, after


async def toggle_weekend_day(employee_id: str, day: str, changed_by: str) -> dict[str, Any] | None:
    """Switch whether an employee works on Saturday or Sunday, logged.

    Args:
        employee_id: ``employees.id``.
        day: "saturday" or "sunday".
        changed_by: Who switched it.

    Returns:
        The updated row, or None if there's no such active employee.
    """
    column = {"saturday": "works_saturday", "sunday": "works_sunday"}[day]
    after = await fetch_one(
        f"UPDATE employees SET {column} = NOT {column} WHERE id = %s AND status = 'active' RETURNING *",
        (employee_id,),
    )
    if after is not None:
        await log_employee_change(employee_id, "workdays", None, f"{day}={'on' if after[column] else 'off'}", changed_by)
    return after


async def cycle_address_form(employee_id: str, changed_by: str) -> dict[str, Any] | None:
    """Step how friendly messages address someone: name alone → ака → опа → name alone, logged.

    Returns:
        The updated row, or None if there's no such active employee.
    """
    after = await fetch_one(
        """
        UPDATE employees
        SET address_form = CASE address_form WHEN 'aka' THEN 'opa' WHEN 'opa' THEN NULL ELSE 'aka' END
        WHERE id = %s AND status = 'active' RETURNING *
        """,
        (employee_id,),
    )
    if after is not None:
        await log_employee_change(employee_id, "address_form", None, after["address_form"] or "name", changed_by)
    return after


async def request_name_change(telegram_user_id: int) -> dict[str, Any] | None:
    """Record an employee's own request to change their name (/ism).

    Returns:
        The employee row, or None if they already asked within the last hour.
    """
    return await fetch_one(
        """
        UPDATE employees SET name_change_requested_at = now()
        WHERE telegram_user_id = %s AND status = 'active'
          AND (name_change_requested_at IS NULL OR name_change_requested_at < now() - interval '1 hour')
        RETURNING *
        """,
        (telegram_user_id,),
    )


async def mark_name_asked(telegram_user_id: int) -> None:
    """Record that the bot asked this employee for their name."""
    await execute(
        "UPDATE employees SET name_asked_at = now() WHERE telegram_user_id = %s AND name_asked_at IS NULL",
        (telegram_user_id,),
    )


async def employees_to_ask_name() -> list[dict[str, Any]]:
    """Active employees (not the Director) with no name who haven't been asked yet."""
    return await fetch_all(
        """
        SELECT * FROM employees
        WHERE status = 'active' AND role <> 'operatsion_direktor'
          AND COALESCE(btrim(full_name), '') = '' AND name_asked_at IS NULL
        ORDER BY created_at
        """
    )


# --------------------------------------------- relays awaiting confirmation


async def create_pending_relay(employee_telegram_user_id: int, text: str, task_id: str | None) -> dict[str, Any]:
    """Hold an employee's message until they confirm it should reach the Director."""
    return await fetch_one(
        """
        INSERT INTO pending_relays (employee_telegram_user_id, message_text, task_id)
        VALUES (%s, %s, %s)
        RETURNING *
        """,
        (employee_telegram_user_id, text, task_id),
    )


async def resolve_pending_relay(relay_id: str, employee_telegram_user_id: int, outcome: str) -> dict[str, Any] | None:
    """Settle a held message, once, and only by the person who wrote it.

    Returns:
        The row if this call settled it, else None (already settled, not
        theirs, or unknown) — a double tap can't send twice.
    """
    return await fetch_one(
        """
        UPDATE pending_relays
        SET resolved_at = now(), outcome = %s
        WHERE id = %s AND employee_telegram_user_id = %s AND resolved_at IS NULL
        RETURNING *
        """,
        (outcome, relay_id, employee_telegram_user_id),
    )


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
        RETURNING t.*, e.telegram_user_id AS employee_telegram_user_id,
                  COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name
        """,
        (due_date, director_telegram_user_id, source_message_id),
    )


async def tasks_needing_reminder(today: date) -> list[dict[str, Any]]:
    """Open tasks due today or tomorrow that haven't had their reminder yet."""
    return await fetch_all(
        """
        SELECT t.*, e.telegram_user_id AS employee_telegram_user_id,
               COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name
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
        SELECT t.*, e.telegram_user_id AS employee_telegram_user_id,
               COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name
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
               t.director_telegram_user_id,
               COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name, e.role
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
        SELECT t.task_summary, t.status, t.due_date, t.created_at,
               COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name, e.role
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
        SELECT r.report_date, r.status, r.submitted_at,
               COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name, e.role
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


async def open_tasks_for_employee(employee_telegram_user_id: int) -> list[dict[str, Any]]:
    """Every task this employee hasn't finished yet, newest first."""
    return await fetch_all(
        """
        SELECT t.* FROM tasks t
        JOIN employees e ON e.id = t.assigned_employee_id
        WHERE e.telegram_user_id = %s AND t.status IN ('sent', 'started')
        ORDER BY t.created_at DESC
        """,
        (employee_telegram_user_id,),
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
        SELECT r.*, COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name,
               e.full_name, e.address_form
        FROM daily_reports r
        JOIN employees e ON e.id = r.employee_id
        WHERE r.report_date = %s AND r.status = 'asked' AND r.reminded_at IS NULL
          AND e.status = 'active'
        ORDER BY r.asked_at
        """,
        (report_date,),
    )


async def mark_report_reminded(report_id: str, message_id: int | None = None) -> None:
    """Record that the one reminder for this report has been sent.

    Args:
        report_id: ``daily_reports.id``.
        message_id: The reminder's Telegram message id, so a reply to it
            counts as the report.
    """
    await execute(
        "UPDATE daily_reports SET reminded_at = now(), reminder_message_id = %s WHERE id = %s",
        (message_id, report_id),
    )


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


async def save_client_feedback(
    *, place: str, kind: str, message: str, contact_name: str | None, phone: str | None
) -> dict[str, Any] | None:
    """Store one client complaint from the QR code."""
    return await fetch_one(
        """
        INSERT INTO client_feedback (place, kind, message, contact_name, phone)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (place, kind, message, contact_name, phone),
    )


async def mark_client_feedback_sent(feedback_id: int, message_id: int) -> None:
    """Record that a client message reached the Director."""
    await execute("UPDATE client_feedback SET director_message_id = %s WHERE id = %s", (message_id, feedback_id))


async def recent_client_feedback(days: int = 60) -> list[dict[str, Any]]:
    """Client complaints (and older opinions) of the last ``days`` days, newest first."""
    return await fetch_all(
        "SELECT * FROM client_feedback WHERE created_at >= now() - make_interval(days => %s) ORDER BY created_at DESC",
        (days,),
    )


async def approved_payment_requests(days: int = 120) -> list[dict[str, Any]]:
    """Approved written requests that carry an amount, decided in the last ``days`` days.

    The payment gate (B1) makes the written form the single channel for
    spending, so these are the company's approved payments — what the brief
    shows as today's payments and the cash calendar as money going out.
    """
    return await fetch_all(
        """
        SELECT id, request_no, subject, amount_tiyin, currency, amount_raw, execute_by, status,
               submitted_at, decided_at,
               COALESCE(NULLIF(btrim(requester_full_name), ''), requester_name) AS requester
        FROM permission_requests
        WHERE status IN ('approved', 'approved_conditional') AND amount_tiyin > 0
          AND decided_at >= now() - make_interval(days => %s)
        ORDER BY decided_at
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
        SELECT r.report_date, r.status,
               COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name, e.role
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
               r.submitted_at, COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS display_name, e.role
        FROM daily_reports r
        JOIN employees e ON e.id = r.employee_id
        WHERE r.report_date >= %s::date - %s::int
        ORDER BY r.report_date DESC, e.display_name
        """,
        # Tashkent's date, not the database's: in UTC the day turns over five
        # hours late, so current_date was "yesterday" until 05:00 local time.
        (today_local(), days),
    )


# --------------------------------------------------------------- team cheer


async def claim_cheer(day: date, slot: str) -> dict[str, Any] | None:
    """Take today's ``slot``; None if a run already took it (so nothing is sent twice)."""
    return await fetch_one(
        "INSERT INTO cheer_messages (day, slot) VALUES (%s, %s) ON CONFLICT (day, slot) DO NOTHING RETURNING *",
        (day, slot),
    )


async def set_cheer_content(
    cheer_id: str, *, text: str, question: str | None, options: list[dict[str, str]], source: str
) -> None:
    """Record what a claimed slot says, and whether the AI or the built-in list wrote it."""
    await execute(
        "UPDATE cheer_messages SET text = %s, question = %s, options = %s::jsonb, source = %s WHERE id = %s",
        (text, question, json.dumps(options, ensure_ascii=False), source, cheer_id),
    )


async def recent_cheer_texts(limit: int = 30) -> list[str]:
    """The latest cheer texts and questions, newest first — so the AI doesn't repeat itself."""
    rows = await fetch_all(
        "SELECT text, question FROM cheer_messages WHERE text IS NOT NULL ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    return [" ".join(part for part in (r["text"], r["question"]) if part) for r in rows]


async def save_cheer_delivery(cheer_id: str, telegram_user_id: int, message_id: int | None, text: str) -> None:
    """Remember one sent cheer, so a tap or a reply to it can be recognised."""
    await execute(
        """
        INSERT INTO cheer_deliveries (cheer_id, telegram_user_id, message_id, text)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (cheer_id, telegram_user_id) DO NOTHING
        """,
        (cheer_id, telegram_user_id, message_id, text),
    )


async def answer_cheer(cheer_id: str, telegram_user_id: int, answer_index: int) -> dict[str, Any] | None:
    """Record a tapped answer, once per person; None if already answered or not theirs.

    Returns:
        The delivery joined with its cheer (``text``, ``question``, ``options``).
    """
    return await fetch_one(
        """
        UPDATE cheer_deliveries d SET answer_index = %s, answered_at = now()
        FROM cheer_messages c
        WHERE d.cheer_id = c.id AND d.cheer_id = %s AND d.telegram_user_id = %s AND d.answer_index IS NULL
        RETURNING d.id, d.message_id, d.text, c.question, c.options
        """,
        (answer_index, cheer_id, telegram_user_id),
    )


async def cheer_delivery_for_message(telegram_user_id: int, message_id: int) -> dict[str, Any] | None:
    """The cheer this user's message replies to, if it replies to one."""
    return await fetch_one(
        "SELECT id FROM cheer_deliveries WHERE telegram_user_id = %s AND message_id = %s",
        (telegram_user_id, message_id),
    )


# ---------------------------------------------------------------------- KPI
# The Director's criteria (kpi_score.py): goals, 1–5 ratings, month data.

_GOAL_COLUMNS = """g.id, g.employee_id, g.month, g.title, g.target, g.actual, g.status,
       g.awaiting_actual_by, COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS name,
       e.telegram_user_id AS employee_telegram_user_id"""


async def start_goal_draft(employee_id: str, month: date, director_telegram_id: int) -> dict[str, Any] | None:
    """Begin a goal for ``employee_id``: the Director's next message is its text."""
    await execute(
        "DELETE FROM kpi_goals WHERE status = 'draft' AND set_by_telegram_id = %s", (director_telegram_id,)
    )
    return await fetch_one(
        "INSERT INTO kpi_goals (employee_id, month, set_by_telegram_id) VALUES (%s, %s, %s) RETURNING *",
        (employee_id, month, director_telegram_id),
    )


async def goal_draft(director_telegram_id: int) -> dict[str, Any] | None:
    """The Director's goal being written (started in the last 30 minutes)."""
    return await fetch_one(
        f"""SELECT {_GOAL_COLUMNS} FROM kpi_goals g JOIN employees e ON e.id = g.employee_id
            WHERE g.status = 'draft' AND g.set_by_telegram_id = %s AND g.created_at > now() - interval '30 minutes'
            ORDER BY g.created_at DESC LIMIT 1""",
        (director_telegram_id,),
    )


async def activate_goal(goal_id: str, title: str, target: float) -> dict[str, Any] | None:
    await execute(
        "UPDATE kpi_goals SET title = %s, target = %s, status = 'active' WHERE id = %s AND status = 'draft'",
        (title, target, goal_id),
    )
    return await get_goal(goal_id)


async def cancel_goal(goal_id: str) -> dict[str, Any] | None:
    """Cancel a goal (or a draft); the row stays, marked cancelled."""
    await execute("UPDATE kpi_goals SET status = 'cancelled', awaiting_actual_by = NULL WHERE id = %s", (goal_id,))
    return await get_goal(goal_id)


async def cancel_goal_drafts(director_telegram_id: int) -> int:
    rows = await fetch_all(
        "DELETE FROM kpi_goals WHERE status = 'draft' AND set_by_telegram_id = %s RETURNING id", (director_telegram_id,)
    )
    return len(rows)


async def get_goal(goal_id: str) -> dict[str, Any] | None:
    return await fetch_one(
        f"SELECT {_GOAL_COLUMNS} FROM kpi_goals g JOIN employees e ON e.id = g.employee_id WHERE g.id = %s",
        (goal_id,),
    )


async def goals_for_months(months: list[date], employee_id: str | None = None) -> list[dict[str, Any]]:
    """Active goals of the given months (one person's, or everyone's)."""
    return await fetch_all(
        f"""SELECT {_GOAL_COLUMNS} FROM kpi_goals g JOIN employees e ON e.id = g.employee_id
            WHERE g.status = 'active' AND g.month = ANY(%s) AND (%s::uuid IS NULL OR g.employee_id = %s::uuid)
            ORDER BY g.month, name, g.created_at""",
        (months, employee_id, employee_id),
    )


async def await_goal_actual(goal_id: str, telegram_user_id: int) -> None:
    """The next number ``telegram_user_id`` types is this goal's actual result."""
    await execute(
        "UPDATE kpi_goals SET awaiting_actual_by = NULL WHERE awaiting_actual_by = %s", (telegram_user_id,)
    )
    await execute(
        "UPDATE kpi_goals SET awaiting_actual_by = %s, awaiting_since = now() WHERE id = %s AND status = 'active'",
        (telegram_user_id, goal_id),
    )


async def goal_awaiting_actual(telegram_user_id: int) -> dict[str, Any] | None:
    """The goal this person was asked the result of (in the last 3 days)."""
    return await fetch_one(
        f"""SELECT {_GOAL_COLUMNS} FROM kpi_goals g JOIN employees e ON e.id = g.employee_id
            WHERE g.awaiting_actual_by = %s AND g.status = 'active' AND g.awaiting_since > now() - interval '3 days'
            ORDER BY g.awaiting_since DESC LIMIT 1""",
        (telegram_user_id,),
    )


async def clear_goal_awaiting(telegram_user_id: int) -> None:
    await execute("UPDATE kpi_goals SET awaiting_actual_by = NULL WHERE awaiting_actual_by = %s", (telegram_user_id,))


async def set_goal_actual(goal_id: str, actual: float, by: str) -> dict[str, Any] | None:
    await execute(
        """UPDATE kpi_goals SET actual = %s, actual_by = %s, actual_at = now(), awaiting_actual_by = NULL
           WHERE id = %s AND status = 'active'""",
        (actual, by, goal_id),
    )
    return await get_goal(goal_id)


async def ensure_rating(employee_id: str, month: date) -> dict[str, Any] | None:
    """This person's rating row for the month (created empty if missing)."""
    await execute(
        "INSERT INTO kpi_ratings (employee_id, month) VALUES (%s, %s) ON CONFLICT (employee_id, month) DO NOTHING",
        (employee_id, month),
    )
    return await fetch_one("SELECT * FROM kpi_ratings WHERE employee_id = %s AND month = %s", (employee_id, month))


async def set_rating(rating_id: str, criterion: str, value: int, rated_by: int) -> dict[str, Any] | None:
    """Store one 1–5 mark; ``criterion`` must be a kpi_score.RATINGS key."""
    if criterion not in ("performance", "communication", "interaction", "qualifications"):
        raise ValueError(criterion)
    return await fetch_one(
        f"UPDATE kpi_ratings SET {criterion} = %s, rated_by = %s, updated_at = now() WHERE id = %s RETURNING *",
        (value, rated_by, rating_id),
    )


async def ratings_for_month(month: date) -> list[dict[str, Any]]:
    return await fetch_all("SELECT * FROM kpi_ratings WHERE month = %s", (month,))


async def kpi_period(month: date) -> dict[str, Any] | None:
    await execute("INSERT INTO kpi_periods (month) VALUES (%s) ON CONFLICT (month) DO NOTHING", (month,))
    return await fetch_one("SELECT * FROM kpi_periods WHERE month = %s", (month,))


async def mark_kpi_period(month: date, column: str) -> bool:
    """Set ``ratings_requested_at`` / ``final_sent_at`` once; False if it was already set."""
    if column not in ("ratings_requested_at", "final_sent_at"):
        raise ValueError(column)
    await kpi_period(month)
    row = await fetch_one(
        f"UPDATE kpi_periods SET {column} = now() WHERE month = %s AND {column} IS NULL RETURNING month", (month,)
    )
    return row is not None


async def kpi_month_rows(start: date, end: date) -> dict[str, list[dict[str, Any]]]:
    """Everything the KPI needs for ``[start, end]``, keyed by employee_id."""
    reports = await fetch_all(
        """SELECT r.employee_id, r.report_date, r.status, r.submitted_at, e.display_name
           FROM daily_reports r JOIN employees e ON e.id = r.employee_id
           WHERE r.report_date BETWEEN %s AND %s""",
        (start, end),
    )
    tasks = await fetch_all(
        """SELECT t.assigned_employee_id AS employee_id, t.status, t.due_date, t.completed_at, t.created_at,
                  e.display_name
           FROM tasks t JOIN employees e ON e.id = t.assigned_employee_id
           WHERE t.due_date BETWEEN %s AND %s""",
        (start, end),
    )
    done = await fetch_all(
        """SELECT assigned_employee_id AS employee_id, count(*) AS n FROM tasks
           WHERE status = 'done' AND assigned_employee_id IS NOT NULL
             AND (completed_at AT TIME ZONE 'Asia/Tashkent')::date BETWEEN %s AND %s
           GROUP BY assigned_employee_id""",
        (start, end),
    )
    return {"reports": reports, "tasks": tasks, "done": done, "leads": await lead_kpi_rows(start, end)}


# ------------------------------------------------------------------ leads


async def upsert_leads(leads: list[dict[str, Any]]) -> int:
    """Copy the leads sheet into ``leads``; rows already there (same dedupe_key) are left as they are.

    Returns:
        How many were new.
    """
    added = 0
    for lead in leads:
        row = await fetch_one(
            """
            INSERT INTO leads (dedupe_key, company_name, project_name, industry, location, project_stage, signal,
                               signal_source_url, contact_name, contact_role, contact_method, priority, confidence,
                               track, date_added)
            VALUES (%(dedupe_key)s, %(company_name)s, %(project_name)s, %(industry)s, %(location)s,
                    %(project_stage)s, %(signal)s, %(signal_source_url)s, %(contact_name)s, %(contact_role)s,
                    %(contact_method)s, %(priority)s, %(confidence)s, %(track)s, %(date_added)s)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id
            """,
            lead,
        )
        added += row is not None
    return added


async def free_leads() -> list[dict[str, Any]]:
    """Leads nobody has been given yet."""
    return await fetch_all(
        """SELECT l.* FROM leads l
           WHERE NOT EXISTS (SELECT 1 FROM lead_assignments a WHERE a.lead_id = l.id)"""
    )


async def set_lead_brief(lead_id: int, brief: str) -> None:
    """Keep the Uzbek summary written for a lead's card, so it's written once."""
    await execute("UPDATE leads SET brief = %s WHERE id = %s", (brief, lead_id))


async def create_lead_assignment(lead_id: int, employee_id: str, day: date) -> dict[str, Any] | None:
    """Give a lead to someone for ``day``; None if the lead is taken or they already got one today."""
    return await fetch_one(
        """INSERT INTO lead_assignments (lead_id, employee_id, assigned_on) VALUES (%s, %s, %s)
           ON CONFLICT DO NOTHING RETURNING *""",
        (lead_id, employee_id, day),
    )


async def set_lead_assignment_message(assignment_id: str, message_id: int | None) -> None:
    await execute("UPDATE lead_assignments SET message_id = %s WHERE id = %s", (message_id, assignment_id))


async def open_leads_to_ask(day: date) -> list[dict[str, Any]]:
    """Every open lead of an active sales person not yet asked about today, with the lead and the person.

    Leads given out today are included: the 08:00 lead is asked about at 15:00.
    """
    return await fetch_all(
        """
        SELECT a.id AS assignment_id, a.assigned_on, a.lead_id,
               l.company_name, l.project_name,
               e.id AS employee_id, e.telegram_user_id, e.full_name, e.display_name, e.address_form,
               e.works_saturday, e.works_sunday, e.role
        FROM lead_assignments a
        JOIN leads l ON l.id = a.lead_id
        JOIN employees e ON e.id = a.employee_id
        WHERE a.status IN ('new', 'in_progress') AND e.status = 'active'
          AND NOT EXISTS (SELECT 1 FROM lead_checkins c WHERE c.assignment_id = a.id AND c.checkin_day = %s)
        ORDER BY e.id, a.assigned_on
        """,
        (day,),
    )


async def create_lead_checkin(assignment_id: str, day: date, telegram_user_id: int) -> dict[str, Any] | None:
    """Open today's 15:00 question for a lead; None if it was already asked today."""
    return await fetch_one(
        """INSERT INTO lead_checkins (assignment_id, checkin_day, telegram_user_id) VALUES (%s, %s, %s)
           ON CONFLICT (assignment_id, checkin_day) DO NOTHING RETURNING *""",
        (assignment_id, day, telegram_user_id),
    )


async def set_lead_checkin_message(checkin_id: str, message_id: int | None) -> None:
    await execute("UPDATE lead_checkins SET message_id = %s WHERE id = %s", (message_id, checkin_id))


async def answer_lead_checkin(checkin_id: str, telegram_user_id: int, status: str) -> dict[str, Any] | None:
    """Record the tapped status, once; dismissed/done close the lead today. None if not theirs or already answered."""
    return await fetch_one(
        """
        WITH c AS (
            UPDATE lead_checkins SET status = %(status)s, answered_at = now()
            WHERE id = %(id)s AND telegram_user_id = %(user)s AND status IS NULL
            RETURNING *
        ), a AS (
            UPDATE lead_assignments la
            SET status = %(status)s,
                closed_on = CASE WHEN %(status)s IN ('dismissed', 'done')
                                 THEN (now() AT TIME ZONE 'Asia/Tashkent')::date END
            FROM c WHERE la.id = c.assignment_id
            RETURNING la.*
        )
        SELECT c.id, c.message_id, a.id AS assignment_id, a.status, l.company_name, l.project_name
        FROM c JOIN a ON a.id = c.assignment_id JOIN leads l ON l.id = a.lead_id
        """,
        {"status": status, "id": checkin_id, "user": telegram_user_id},
    )


async def choose_lead_outcome(checkin_id: str, telegram_user_id: int, outcome: str) -> dict[str, Any] | None:
    """Record why a lead was dismissed or what came of it, once. None if not theirs or already chosen."""
    return await fetch_one(
        """
        UPDATE lead_assignments a SET outcome = %s
        FROM lead_checkins c, leads l
        WHERE c.id = %s AND c.telegram_user_id = %s AND a.id = c.assignment_id AND l.id = a.lead_id
          AND a.outcome IS NULL AND c.status IN ('dismissed', 'done')
        RETURNING c.id, c.message_id, a.status, l.company_name, l.project_name
        """,
        (outcome, checkin_id, telegram_user_id),
    )


async def ask_lead_question(checkin_id: str, question: str, question_message_id: int | None) -> None:
    """Remember the follow-up asked after a tap, so the typed answer can be recognised."""
    await execute(
        """UPDATE lead_checkins SET question = %s, question_message_id = %s, question_asked_at = now()
           WHERE id = %s""",
        (question, question_message_id, checkin_id),
    )


async def pending_lead_question(telegram_user_id: int, within_minutes: int) -> dict[str, Any] | None:
    """The latest unanswered follow-up asked of this person within ``within_minutes``."""
    return await fetch_one(
        """
        SELECT c.* FROM lead_checkins c
        WHERE c.telegram_user_id = %s AND c.question IS NOT NULL AND c.note IS NULL
          AND c.question_asked_at > now() - make_interval(mins => %s)
        ORDER BY c.question_asked_at DESC LIMIT 1
        """,
        (telegram_user_id, within_minutes),
    )


async def lead_question_by_message(telegram_user_id: int, message_id: int) -> dict[str, Any] | None:
    """The unanswered follow-up that this message replies to, if any."""
    return await fetch_one(
        """SELECT * FROM lead_checkins
           WHERE telegram_user_id = %s AND question_message_id = %s AND note IS NULL""",
        (telegram_user_id, message_id),
    )


async def save_lead_note(checkin_id: str, note: str) -> dict[str, Any] | None:
    """Keep the typed answer to a lead's follow-up, once."""
    return await fetch_one(
        "UPDATE lead_checkins SET note = %s, noted_at = now() WHERE id = %s AND note IS NULL RETURNING *",
        (note, checkin_id),
    )


async def lead_kpi_rows(start: date, end: date) -> list[dict[str, Any]]:
    """Per person for ``[start, end]``: 15:00 questions asked and answered that day, leads closed and done."""
    asked = await fetch_all(
        """
        SELECT a.employee_id, count(*) AS asked,
               count(*) FILTER (WHERE c.status IS NOT NULL
                                  AND (c.answered_at AT TIME ZONE 'Asia/Tashkent')::date = c.checkin_day) AS answered
        FROM lead_checkins c JOIN lead_assignments a ON a.id = c.assignment_id
        WHERE c.checkin_day BETWEEN %s AND %s
        GROUP BY a.employee_id
        """,
        (start, end),
    )
    closed = await fetch_all(
        """
        SELECT employee_id, count(*) AS closed, count(*) FILTER (WHERE status = 'done') AS done
        FROM lead_assignments WHERE closed_on BETWEEN %s AND %s
        GROUP BY employee_id
        """,
        (start, end),
    )
    by_id: dict[str, dict[str, Any]] = {}
    for row in asked + closed:
        by_id.setdefault(str(row["employee_id"]), {"employee_id": str(row["employee_id"])}).update(
            {k: int(v) for k, v in row.items() if k != "employee_id"}
        )
    return list(by_id.values())


async def recent_lead_assignments(days: int = 30) -> list[dict[str, Any]]:
    """Leads handed out in the last ``days`` days, with who has them, where they stand and the latest note."""
    return await fetch_all(
        """
        SELECT a.assigned_on, a.status, a.outcome,
               COALESCE(NULLIF(btrim(e.full_name), ''), e.display_name) AS person,
               COALESCE(l.company_name, l.project_name) AS company,
               (SELECT c.note FROM lead_checkins c WHERE c.assignment_id = a.id AND c.note IS NOT NULL
                ORDER BY c.noted_at DESC LIMIT 1) AS last_note
        FROM lead_assignments a
        JOIN employees e ON e.id = a.employee_id
        JOIN leads l ON l.id = a.lead_id
        WHERE a.assigned_on >= (now() AT TIME ZONE 'Asia/Tashkent')::date - make_interval(days => %s)
        ORDER BY a.assigned_on DESC, person
        """,
        (days,),
    )


# ---------------------------------------------------------------- days off


async def is_day_off(day: date) -> bool:
    """Whether the admin marked ``day`` as a day off (/dam)."""
    return await fetch_one("SELECT 1 AS off FROM days_off WHERE day = %s", (day,)) is not None


async def days_off_between(start: date, end: date) -> set[date]:
    """The days off in ``[start, end]``."""
    rows = await fetch_all("SELECT day FROM days_off WHERE day BETWEEN %s AND %s", (start, end))
    return {r["day"] for r in rows}


async def toggle_day_off(day: date, set_by: str) -> bool:
    """Mark ``day`` off, or back to a working day. Returns True if it is now a day off."""
    removed = await fetch_one("DELETE FROM days_off WHERE day = %s RETURNING day", (day,))
    if removed is not None:
        return False
    await execute("INSERT INTO days_off (day, set_by) VALUES (%s, %s) ON CONFLICT (day) DO NOTHING", (day, set_by))
    return True


# ----------------------------------------------------------- announcements


async def create_announcement(text: str, created_by: str) -> dict[str, Any] | None:
    """Keep an announcement until the admin confirms it."""
    return await fetch_one(
        "INSERT INTO announcements (text, created_by) VALUES (%s, %s) RETURNING *", (text, created_by)
    )


async def claim_announcement(announcement_id: int) -> dict[str, Any] | None:
    """Take an announcement for sending, once; None if it was already sent or cancelled."""
    return await fetch_one(
        "UPDATE announcements SET sent_at = now() WHERE id = %s AND sent_at IS NULL RETURNING *", (announcement_id,)
    )


async def finish_announcement(announcement_id: int, sent_count: int) -> None:
    await execute("UPDATE announcements SET sent_count = %s WHERE id = %s", (sent_count, announcement_id))


# ------------------------------------------------------------- task drafts


async def create_task_draft(
    *,
    director_telegram_user_id: int,
    source_message_id: int | None,
    raw_message: str,
    task_summary: str,
    role_slug: str | None,
    due_date: date | None,
    has_media: bool,
    candidate_ids: list[str],
    selected_ids: list[str],
) -> dict[str, Any] | None:
    """Hold a Director's task until they confirm who gets it."""
    return await fetch_one(
        """
        INSERT INTO task_drafts (director_telegram_user_id, source_message_id, raw_message, task_summary, role_slug,
                                 due_date, has_media, candidate_ids, selected_ids)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s::uuid[])
        RETURNING *
        """,
        (director_telegram_user_id, source_message_id, raw_message, task_summary, role_slug, due_date, has_media,
         candidate_ids, selected_ids),
    )


async def set_task_draft_message(draft_id: str, message_id: int | None) -> None:
    await execute("UPDATE task_drafts SET message_id = %s WHERE id = %s", (message_id, draft_id))


async def toggle_task_draft_person(draft_id: str, director_telegram_user_id: int, employee_id: str) -> dict[str, Any] | None:
    """Tick or untick one person on an open draft of this Director's; None if it isn't open or theirs."""
    return await fetch_one(
        """
        UPDATE task_drafts
        SET selected_ids = CASE WHEN %(e)s::uuid = ANY(selected_ids) THEN array_remove(selected_ids, %(e)s::uuid)
                                ELSE array_append(selected_ids, %(e)s::uuid) END
        WHERE id = %(id)s AND director_telegram_user_id = %(d)s AND status = 'open'
        RETURNING *
        """,
        {"e": employee_id, "id": draft_id, "d": director_telegram_user_id},
    )


async def close_task_draft(draft_id: str, director_telegram_user_id: int, status: str) -> dict[str, Any] | None:
    """Mark an open draft sent or cancelled, once; None if it was already closed, isn't theirs or is a day old."""
    return await fetch_one(
        """
        UPDATE task_drafts SET status = %s
        WHERE id = %s AND director_telegram_user_id = %s AND status = 'open'
          AND created_at > now() - interval '24 hours'
        RETURNING *
        """,
        (status, draft_id, director_telegram_user_id),
    )


async def get_task_draft(draft_id: str) -> dict[str, Any] | None:
    return await fetch_one("SELECT * FROM task_drafts WHERE id = %s", (draft_id,))


# ---------------------------------------------------------- AI chat (employees)


async def toggle_ai_chat(employee_id: str, changed_by: str) -> dict[str, Any] | None:
    """Switch the work AI off for one person, or back on; logged."""
    after = await fetch_one(
        "UPDATE employees SET ai_chat_off = NOT ai_chat_off WHERE id = %s AND status = 'active' RETURNING *",
        (employee_id,),
    )
    if after is not None:
        await log_employee_change(employee_id, "ai_chat", None, "off" if after["ai_chat_off"] else "on", changed_by)
    return after


async def toggle_cheer(employee_id: str, changed_by: str) -> dict[str, Any] | None:
    """Switch the 10:00 / 17:35 cheer messages off for one person, or back on; logged."""
    after = await fetch_one(
        "UPDATE employees SET cheer_off = NOT cheer_off WHERE id = %s AND status = 'active' RETURNING *",
        (employee_id,),
    )
    if after is not None:
        await log_employee_change(employee_id, "cheer", None, "off" if after["cheer_off"] else "on", changed_by)
    return after


async def set_responsibilities(employee_id: str, text: str | None) -> dict[str, Any] | None:
    """Keep a person's written duties, for the work AI."""
    return await fetch_one(
        "UPDATE employees SET responsibilities = %s WHERE id = %s AND status = 'active' RETURNING *",
        ((text or "").strip() or None, employee_id),
    )


async def open_leads_for_employee(employee_id: str) -> list[dict[str, Any]]:
    """This person's own open leads, newest first — for the work AI."""
    return await fetch_all(
        """
        SELECT a.assigned_on, a.status, COALESCE(l.company_name, l.project_name) AS company
        FROM lead_assignments a JOIN leads l ON l.id = a.lead_id
        WHERE a.employee_id = %s AND a.status IN ('new', 'in_progress')
        ORDER BY a.assigned_on DESC
        """,
        (employee_id,),
    )


async def log_ai_turn(telegram_user_id: int, role: str, content: str) -> None:
    await execute(
        "INSERT INTO ai_chat_turns (telegram_user_id, role, content) VALUES (%s, %s, %s)",
        (telegram_user_id, role, content),
    )


async def recent_ai_turns(telegram_user_id: int, limit: int = 12) -> list[dict[str, Any]]:
    """The last turns of someone's AI chat, oldest first."""
    rows = await fetch_all(
        """SELECT role, content FROM ai_chat_turns
           WHERE telegram_user_id = %s AND created_at > now() - interval '1 day'
           ORDER BY created_at DESC LIMIT %s""",
        (telegram_user_id, limit),
    )
    return list(reversed(rows))


async def ai_questions_today(telegram_user_id: int) -> int:
    row = await fetch_one(
        """SELECT count(*) AS n FROM ai_chat_turns
           WHERE telegram_user_id = %s AND role = 'employee'
             AND (created_at AT TIME ZONE 'Asia/Tashkent')::date = (now() AT TIME ZONE 'Asia/Tashkent')::date""",
        (telegram_user_id,),
    )
    return int(row["n"]) if row else 0


# -------------------------------------------- employees' files, today's report


async def create_employee_file(telegram_user_id: int, message_id: int, kind: str, caption: str | None) -> dict[str, Any] | None:
    """Hold a file an employee sent until they say what it's for."""
    return await fetch_one(
        "INSERT INTO employee_files (telegram_user_id, message_id, kind, caption) VALUES (%s, %s, %s, %s) RETURNING *",
        (telegram_user_id, message_id, kind, caption),
    )


async def resolve_employee_file(file_id: str, telegram_user_id: int, purpose: str) -> dict[str, Any] | None:
    """Settle a held file once, by its sender, within a day; None otherwise."""
    return await fetch_one(
        """UPDATE employee_files SET purpose = %s, resolved_at = now()
           WHERE id = %s AND telegram_user_id = %s AND resolved_at IS NULL
             AND created_at > now() - interval '24 hours'
           RETURNING *""",
        (purpose, file_id, telegram_user_id),
    )


async def set_employee_file_sent(file_id: str, sent_count: int) -> None:
    await execute("UPDATE employee_files SET sent_count = %s WHERE id = %s", (sent_count, file_id))


async def report_for_day(telegram_user_id: int, report_date: date) -> dict[str, Any] | None:
    """This person's report row for the day, asked or submitted."""
    return await fetch_one(
        "SELECT * FROM daily_reports WHERE telegram_user_id = %s AND report_date = %s",
        (telegram_user_id, report_date),
    )


async def attach_media_to_report(telegram_user_id: int, report_date: date, caption: str | None) -> dict[str, Any] | None:
    """A file sent as today's report: submits the report if it's still owed, else counts the file.

    None when no report was asked today (nothing to attach it to).
    """
    return await fetch_one(
        """
        UPDATE daily_reports
        SET media_count = media_count + 1,
            status = 'submitted',
            submitted_at = COALESCE(submitted_at, now()),
            content = CASE WHEN status = 'asked' THEN %s ELSE content END
        WHERE telegram_user_id = %s AND report_date = %s
        RETURNING *
        """,
        ((caption or "").strip() or "📎 файл", telegram_user_id, report_date),
    )


async def start_report_edit(report_id: str, telegram_user_id: int, report_date: date) -> dict[str, Any] | None:
    """Mark today's submitted report as "the next message is its new text"."""
    return await fetch_one(
        """UPDATE daily_reports SET editing_at = now()
           WHERE id = %s AND telegram_user_id = %s AND report_date = %s AND status = 'submitted'
           RETURNING *""",
        (report_id, telegram_user_id, report_date),
    )


async def report_being_edited(telegram_user_id: int, report_date: date, within_minutes: int) -> dict[str, Any] | None:
    return await fetch_one(
        """SELECT * FROM daily_reports
           WHERE telegram_user_id = %s AND report_date = %s AND status = 'submitted'
             AND editing_at > now() - make_interval(mins => %s)""",
        (telegram_user_id, report_date, within_minutes),
    )


async def replace_report(report_id: str, content: str) -> dict[str, Any] | None:
    """Today's report gets its new text; when it was first sent doesn't change."""
    return await fetch_one(
        """UPDATE daily_reports SET content = %s, editing_at = NULL
           WHERE id = %s AND status = 'submitted' RETURNING *""",
        (content, report_id),
    )


async def delete_report(report_id: str, telegram_user_id: int, report_date: date) -> dict[str, Any] | None:
    """Take back today's report: it is owed again (it can be written anew until midnight)."""
    return await fetch_one(
        """
        UPDATE daily_reports
        SET status = 'asked', content = NULL, metrics = '{}'::jsonb, submitted_at = NULL, editing_at = NULL,
            followup_asked_at = NULL, followup_answered_at = NULL, media_count = 0
        WHERE id = %s AND telegram_user_id = %s AND report_date = %s AND status = 'submitted'
        RETURNING *
        """,
        (report_id, telegram_user_id, report_date),
    )
