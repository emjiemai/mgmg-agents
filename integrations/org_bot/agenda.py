"""What the bot is waiting for from one employee right now (2026-10-24, the owner).

"So it doesn't overwhelm or make mistakes, it needs to know what its job is for one specific employee." The work
AI (ai_chat.py) is given this list, so «nima qilishim kerak?» / «что от меня нужно?» gets a true answer, and the
AI never asks for something the bot isn't waiting for. Read-only; an unreadable part is left out, never guessed.
"""

from __future__ import annotations

from typing import Any

from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local

log = setup_logging("ops-manager-bot")


def _days(values: Any) -> str:
    return ", ".join(f"{d:%d.%m}" for d in values or [])


async def pending(employee: dict[str, Any]) -> list[str]:
    """Lines for the AI: the open morning question, a late report being sent, today's report."""
    from integrations.org_bot import explanations, late_report, store

    tg = employee["telegram_user_id"]
    now = now_local()
    lines: list[str] = []
    try:
        for x in await store.open_explanations(tg, explanations.OPEN_HOURS):
            if x["kind"] == "absent":
                lines.append(f"- This morning's question: Verifix showed them absent on {_days(x['days'])}. They just "
                             "write the reason here (or say they did come); it goes to the Director.")
            else:
                lines.append(f"- This morning's question: their daily report of {_days(x['days'])} never came. They "
                             "write the reason here, or tap «📝 Кечаги ҳисоботни юбориш» under the morning message and "
                             "send the report — the Director or their manager approves it, then it counts as sent.")
        late = await store.late_report_awaiting(tg, late_report.AWAIT_HOURS)
        if late is not None:
            lines.append(f"- They tapped «send yesterday's report»: their next message here is the "
                         f"{late['report_date']:%d.%m} report (text or a voice message).")
        today = await store.report_for_day(tg, now.date())
        if today is not None and today.get("status") == "asked" and now.hour < 22:
            lines.append("- Today's daily report: asked at 16:00, accepted until 22:00 — they write it here.")
        elif today is not None and today.get("status") == "submitted":
            lines.append("- Today's daily report: sent (it can be changed with /hisobot until midnight).")
    except Exception as exc:  # noqa: BLE001 — the AI then answers without this, as before
        log.warning("Agenda not read: {}", type(exc).__name__)
        return []
    return lines
