"""Telegram notification and approval client.

This is the primary delivery channel for briefs and alerts, and the approval
surface for security rule #4: anything touching payments, contracts or HR must
be confirmed by a human on an inline button before an agent may act.

Messages are HTML-formatted and split at 4096 characters on line boundaries so
a long brief never gets truncated or breaks a tag mid-way.
"""

from __future__ import annotations

import html
import uuid
from typing import Any, Literal

import httpx

from integrations.common.config import settings
from integrations.common.db import audited, execute, fetch_one, log_action
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.money import format_uzs

log = setup_logging("telegram")

TELEGRAM_MAX_CHARS = 4096
SAFE_CHUNK = 3900  # leaves room for the "(1/3)" continuation marker

Severity = Literal["critical", "warning", "info"]

SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "warning": "🟡",
    "info": "🟢",
}


class TelegramError(RuntimeError):
    """Raised when the Telegram Bot API rejects a request."""


class TelegramBot:
    """Async Telegram Bot API client.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TelegramBot":
        """Open the HTTP client against the bot's API root.

        Returns:
            The ready bot client.

        Raises:
            TelegramError: if the bot token is unset.
        """
        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")

        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}",
            timeout=httpx.Timeout(30.0),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- sending

    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool = False,
    ) -> list[int]:
        """Send an HTML message, splitting it if it exceeds Telegram's limit.

        Args:
            text: HTML body (use ``escape`` for untrusted substrings).
            chat_id: Destination; defaults to the CEO chat.
            reply_markup: Inline keyboard, attached to the final chunk only.
            disable_notification: Send silently.

        Returns:
            The message ids created, in order.

        Raises:
            TelegramError: if Telegram rejects a chunk.
        """
        chat = chat_id or settings.telegram_ceo_chat_id
        chunks = split_message(text)
        message_ids: list[int] = []

        for index, chunk in enumerate(chunks, start=1):
            body = chunk if len(chunks) == 1 else f"{chunk}\n\n<i>({index}/{len(chunks)})</i>"
            payload: dict[str, Any] = {
                "chat_id": chat,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": disable_notification,
            }
            if reply_markup is not None and index == len(chunks):
                payload["reply_markup"] = reply_markup

            result = await self._call("sendMessage", payload, mode="notify", target_ref=str(chat))
            if result is not None:
                message_ids.append(result["message_id"])

        return message_ids

    async def send_alert(
        self,
        title: str,
        body: str,
        severity: Severity = "info",
        chat_id: str | None = None,
    ) -> list[int]:
        """Send a severity-prefixed alert message.

        Args:
            title: Short headline.
            body: HTML body text.
            severity: 'critical' | 'warning' | 'info' — sets the emoji.
            chat_id: Destination; defaults to the alerts chat.

        Returns:
            The message ids created.

        Raises:
            TelegramError: if Telegram rejects the message.
        """
        emoji = SEVERITY_EMOJI.get(severity, "🟢")
        text = f"{emoji} <b>{escape(title)}</b>\n\n{body}"
        return await self.send_message(
            text,
            chat_id or settings.telegram_alerts_chat_id or settings.telegram_ceo_chat_id,
        )

    async def request_approval(
        self,
        *,
        title: str,
        description: str,
        category: Literal["payment", "contract", "hr", "communication", "other"],
        proposed_action: dict[str, Any],
        amount_tiyin: int | None = None,
        chat_id: str | None = None,
    ) -> str:
        """Create a pending approval and post it with Approve/Decline buttons.

        The approval row is written **before** the message is sent, so a send
        failure leaves an auditable pending request rather than a silent gap.

        Args:
            title: One-line summary of what needs approval.
            description: Detail shown under the title.
            category: Approval category driving who may decide.
            proposed_action: Machine-readable description of what runs on
                approval — the agent reads this back, it is not free text.
            amount_tiyin: Amount at stake, for payment approvals.
            chat_id: Destination; defaults to the CEO chat.

        Returns:
            The approval id (UUID string).

        Raises:
            TelegramError: if Telegram rejects the message.
            psycopg.Error: if the approval row cannot be written.
        """
        import json

        chat = chat_id or settings.telegram_ceo_chat_id
        row = await fetch_one(
            """
            INSERT INTO approvals
                (requested_by_agent, category, title, description,
                 amount_tiyin, proposed_action, chat_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                self.agent,
                category,
                title,
                description,
                amount_tiyin,
                json.dumps(proposed_action, ensure_ascii=False, default=str),
                chat,
            ),
        )
        approval_id = str(row["id"])  # type: ignore[index]

        lines = ["🔐 <b>Approval required</b>\n", f"<b>{escape(title)}</b>", escape(description)]
        if amount_tiyin is not None:
            lines.append(f"\n💰 <b>{escape(format_uzs(amount_tiyin))}</b>")
        lines.append(f"\n<i>Request {approval_id[:8]} · expires in 24h</i>")

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{approval_id}"},
                    {"text": "❌ Decline", "callback_data": f"decline:{approval_id}"},
                ]
            ]
        }

        message_ids = await self.send_message("\n".join(lines), chat, reply_markup=keyboard)
        if message_ids:
            await execute(
                "UPDATE approvals SET telegram_message_id = %s WHERE id = %s",
                (message_ids[0], approval_id),
            )

        log.info("Approval {} requested: {}", approval_id[:8], title)
        return approval_id

    # -------------------------------------------------------------- callbacks

    async def handle_callback_query(self, callback: dict[str, Any]) -> str:
        """Resolve an Approve/Decline button press.

        Idempotent: pressing a button on an already-decided or expired request
        answers with the existing outcome instead of changing it.

        Args:
            callback: The ``callback_query`` object from a Telegram update.

        Returns:
            A short human-readable outcome, also shown as the button toast.
        """
        data = callback.get("data", "")
        query_id = callback.get("id", "")
        user = callback.get("from", {})
        decider = user.get("username") or str(user.get("id", "unknown"))

        if ":" not in data:
            await self._answer_callback(query_id, "Unrecognized action")
            return "unrecognized"

        decision, approval_id = data.split(":", 1)
        if decision not in ("approve", "decline"):
            await self._answer_callback(query_id, "Unrecognized action")
            return "unrecognized"

        approval = await fetch_one("SELECT * FROM approvals WHERE id = %s", (approval_id,))
        if approval is None:
            await self._answer_callback(query_id, "Request not found")
            return "not_found"

        if approval["status"] != "pending":
            outcome = f"Already {approval['status']}"
            await self._answer_callback(query_id, outcome)
            return approval["status"]

        new_status = "approved" if decision == "approve" else "declined"
        await execute(
            """
            UPDATE approvals
            SET status = %s, decided_at = now(), decided_by = %s
            WHERE id = %s AND status = 'pending'
            """,
            (new_status, decider, approval_id),
        )

        await log_action(
            agent=self.agent,
            action="approval_decision",
            target_system="telegram",
            status="success",
            run_id=self.run_id,
            target_ref=approval_id,
            mode="write",
            payload={"decision": new_status, "decided_by": decider, "title": approval["title"]},
        )

        marker = "✅ Approved" if new_status == "approved" else "❌ Declined"
        await self._edit_message(
            chat_id=str(approval["chat_id"]),
            message_id=approval["telegram_message_id"],
            text=f"{marker} by @{escape(decider)}\n\n<b>{escape(approval['title'])}</b>",
        )
        await self._answer_callback(query_id, marker)

        log.info("Approval {} {} by {}", approval_id[:8], new_status, decider)
        return new_status

    async def _answer_callback(self, callback_query_id: str, text: str) -> None:
        """Acknowledge a button press so the client stops its spinner."""
        if not callback_query_id:
            return
        try:
            await self._call(
                "answerCallbackQuery",
                {"callback_query_id": callback_query_id, "text": text},
                mode="notify",
            )
        except TelegramError as err:
            log.warning("answerCallbackQuery failed: {}", err)

    async def _edit_message(self, chat_id: str, message_id: int | None, text: str) -> None:
        """Replace an approval message with its resolved state."""
        if not message_id:
            return
        try:
            await self._call(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
                mode="notify",
                target_ref=chat_id,
            )
        except TelegramError as err:
            log.warning("editMessageText failed: {}", err)

    # ----------------------------------------------------------------- plumbing

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        mode: Literal["read", "write", "notify"] = "notify",
        target_ref: str | None = None,
    ) -> dict[str, Any] | None:
        """Invoke one Bot API method with retries and auditing.

        Args:
            method: Bot API method name, e.g. ``sendMessage``.
            payload: JSON body.
            mode: Audit mode.
            target_ref: Chat id or entity reference for the audit row.

        Returns:
            The ``result`` object from Telegram, or ``None`` in dry-run mode.

        Raises:
            TelegramError: if Telegram answers ``ok: false`` or an HTTP error.
        """
        if settings.dry_run and mode != "read":
            log.info("[dry run] telegram.{} -> {}", method, str(payload)[:300])
            await log_action(
                agent=self.agent,
                action=f"telegram_{method}",
                target_system="telegram",
                status="dry_run",
                run_id=self.run_id,
                target_ref=target_ref,
                mode=mode,
                payload={"method": method, "chars": len(str(payload.get("text", "")))},
            )
            return None

        assert self._client is not None
        async with audited(
            agent=self.agent,
            action=f"telegram_{method}",
            target_system="telegram",
            run_id=self.run_id,
            target_ref=target_ref,
            mode=mode,
            payload={"method": method},
        ) as ctx:
            response = await request_with_retry(self._client, "POST", f"/{method}", json=payload)
            ctx["http_status"] = response.status_code
            body = response.json()
            if not body.get("ok"):
                raise TelegramError(f"Telegram {method} failed: {body.get('description', response.text[:300])}")
            return body.get("result")


def escape(text: str | None) -> str:
    """Escape a string for Telegram HTML parse mode.

    Args:
        text: Untrusted text (customer names, deal titles…).

    Returns:
        The text with ``&``, ``<`` and ``>`` escaped; "" for None.
    """
    return html.escape(str(text), quote=False) if text is not None else ""


def split_message(text: str, limit: int = SAFE_CHUNK) -> list[str]:
    """Split a message into Telegram-sized chunks on line boundaries.

    Splitting on newlines keeps HTML tags intact, since every tag this project
    emits opens and closes within a single line.

    Args:
        text: The full message body.
        limit: Maximum characters per chunk.

    Returns:
        One or more chunks, each at most ``limit`` characters (a single line
        longer than ``limit`` is hard-cut as a last resort).
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in text.split("\n"):
        while len(line) > limit:  # pathological single line
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1

    if current:
        chunks.append("\n".join(current))
    return chunks
