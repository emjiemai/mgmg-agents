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
