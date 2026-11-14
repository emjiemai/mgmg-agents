"""One-time repair (2026-11-14): reopen the 11 task copies the old shared-task rule closed by mistake.

The Boss sent one task to 12 people (source message 4908, T-2026-11-0067…0078). When T-2026-11-0072's proof was
accepted, the removed ``close_shared_copies`` closed the other 11 copies and told each person «… бажарди». None of
them had sent proof. On API startup this puts each copy back where it was (started → started, else sent), redraws its
card and tells the person once that the earlier message was a mistake. The UPDATE only matches copies still marked
«T-2026-11-0072 орқали», so it runs once even across restarts. The old message can't be deleted: its id wasn't kept.
Safe to delete this module (and its call in ``integrations/api/app.py``) after it has run.
"""

from __future__ import annotations

import uuid
from typing import Any

from integrations.common.db import fetch_all, log_action
from integrations.common.logging_setup import setup_logging

AGENT = "ops-manager-bot"
log = setup_logging("reopen-fix")

SOURCE_MESSAGE_ID = 4908
CLOSED_BY = "T-2026-11-0072 орқали"


def correction_text(code: str) -> str:
    return (f"узр, {code} бўйича олдинги «ёпилди» хабари хато юборилган эди — топшириғингиз ҳали очиқ, "
            "уни бажариб, топшириқ картасига жавоб қилиб далил юборинг, раҳмат")


async def reopen(run_id: uuid.UUID | None = None) -> dict[str, int]:
    """Reopen the wrongly closed copies, redraw their cards, send one correction each. Counts only."""
    from integrations.org_bot import ops_manager, store, task_register

    run_id = run_id or uuid.uuid4()
    rows: list[dict[str, Any]] = await fetch_all(
        """UPDATE tasks SET status = CASE WHEN started_at IS NULL THEN 'sent' ELSE 'started' END,
                            completed_at = NULL, completed_by = NULL, verified_at = NULL
           WHERE source_message_id = %s AND status = 'done' AND completed_by = %s
           RETURNING *""",
        (SOURCE_MESSAGE_ID, CLOSED_BY))
    counts = {"reopened": len(rows), "told": 0, "failed": 0}
    for row in rows:
        await store.log_task_event(str(row["id"]), "reopened", "it", "хато ёпилган умумий нусха (14.11)")
        try:
            await ops_manager._refresh_task_card(row, run_id)
            full = await store.task_with_employee(str(row["id"]))
            if full:
                await ops_manager._reply(full["employee_telegram_user_id"], run_id,
                                         correction_text(task_register.code_of(row)),
                                         reply_to=full.get("telegram_message_id"))
                counts["told"] += 1
        except Exception as exc:  # noqa: BLE001 — one person's chat must not stop the others
            log.warning("Correction not delivered: {}", type(exc).__name__)
            counts["failed"] += 1
    if rows:
        await log_action(agent=AGENT, action="tasks_reopened_fix", target_system="postgres", status="success",
                         run_id=run_id, mode="write", payload=counts)
    return counts
