"""Shared query layer for the org_bot package.

The one deliberate departure from this codebase's usual inline-SQL-per-module
style (every existing example inlines its own SQL because each has a single
owning module) — justified here because ``employees`` is read/written by both
``admin.py`` and ``ops_manager.py``, so a shared layer avoids duplicating the
same lookups in two files.

Every guarded UPDATE here checks the affected row count and treats zero as
"already handled" rather than trusting a prior SELECT — tightened versus
``TelegramBot.handle_callback_query``'s pattern, which doesn't check rowcount
(a low-probability gap for a single payment approver, not for an employee
double-tapping a button on a slow connection).
"""

from __future__ import annotations

from typing import Any, Literal

from integrations.common.db import execute, fetch_all, fetch_one

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

    Returns:
        The new row, or None if this exact (director, message, employee)
        combination was already dispatched (duplicate webhook delivery).
    """
    return await fetch_one(
        """
        INSERT INTO tasks
            (director_telegram_user_id, source_message_id, raw_message,
             target_type, target_role, target_agent, assigned_employee_id, task_summary, has_media)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )


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
