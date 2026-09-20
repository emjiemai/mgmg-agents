"""Telegram Bot API client.

This is the delivery channel for briefs, alerts, and both org_bot bots
(Admin Bot and OPS Manager Bot).

Messages are HTML-formatted and split at 4096 characters on line boundaries so
a long brief never gets truncated or breaks a tag mid-way.
"""

from __future__ import annotations

import html
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx

from integrations.common.config import settings
from integrations.common.db import audited, log_action
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

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

    Each bot uses its own token rather than one token shared across bots —
    Telegram can only edit a message with the same token that sent it, so
    sharing tokens silently breaks message edits.

    Args:
        bot_token: This bot's token (e.g. ``OPS_MANAGER_BOT_TELEGRAM_BOT_TOKEN``).
        default_chat_id: Chat used when a call site doesn't pass one
            explicitly (e.g. ``send_alert`` with no ``chat_id``).
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(
        self,
        agent: str = "-",
        run_id: uuid.UUID | str | None = None,
        *,
        bot_token: str | None = None,
        default_chat_id: str | None = None,
    ) -> None:
        self.agent = agent
        self.run_id = run_id
        self._bot_token = bot_token
        self.default_chat_id = default_chat_id or ""
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TelegramBot":
        """Open the HTTP client against the bot's API root.

        Returns:
            The ready bot client.

        Raises:
            TelegramError: if no bot token was given (the bot's token setting
                is not configured).
        """
        if not self._bot_token:
            raise TelegramError("No bot token given — this bot's *_TELEGRAM_BOT_TOKEN is not configured")

        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{self._bot_token}",
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
            chat_id: Destination; defaults to this bot's ``default_chat_id``.
            reply_markup: Inline keyboard, attached to the final chunk only.
            disable_notification: Send silently.

        Returns:
            The message ids created, in order.

        Raises:
            TelegramError: if Telegram rejects a chunk.
        """
        chat = chat_id or self.default_chat_id
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
            chat_id: Destination; defaults to this bot's ``default_chat_id``.

        Returns:
            The message ids created.

        Raises:
            TelegramError: if Telegram rejects the message.
        """
        emoji = SEVERITY_EMOJI.get(severity, "🟢")
        text = f"{emoji} <b>{escape(title)}</b>\n\n{body}"
        return await self.send_message(text, chat_id)

    async def send_chat_action(self, chat_id: str | None = None, action: str = "typing") -> None:
        """Show Telegram's native "..." typing indicator instead of a text message.

        Lasts about 5 seconds on Telegram's own side before fading — meant as
        a quick "working on it" signal for a call that's about to take a few
        seconds, without leaving a permanent, repetitive message in the chat
        the way a canned "got it, processing..." reply would.

        Args:
            chat_id: Destination; defaults to this bot's ``default_chat_id``.
            action: One of Telegram's chat action types; 'typing' fits every
                use in this project so far.
        """
        chat = chat_id or self.default_chat_id
        try:
            await self._call("sendChatAction", {"chat_id": chat, "action": action}, mode="notify", target_ref=str(chat))
        except TelegramError as err:
            log.warning("sendChatAction failed: {}", err)

    async def send_document(self, path: str, chat_id: str, caption: str | None = None) -> int | None:
        """Upload a file to one chat.

        Uses multipart rather than ``_call``'s JSON body — Telegram takes an
        uploaded document only as form data.

        Args:
            path: Local file to send.
            chat_id: Destination chat.
            caption: Optional HTML caption.

        Returns:
            The Telegram message id, or None in dry-run mode.

        Raises:
            TelegramError: if Telegram rejects the upload.
        """
        if settings.dry_run:
            log.info("[dry run] telegram.sendDocument -> {} ({})", chat_id, path)
            return None

        assert self._client is not None
        payload: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"

        async with audited(
            agent=self.agent,
            action="telegram_sendDocument",
            target_system="telegram",
            run_id=self.run_id,
            target_ref=str(chat_id),
            mode="notify",
            payload={"file": Path(path).name},
        ) as ctx:
            with open(path, "rb") as handle:
                response = await self._client.post(
                    "/sendDocument", data=payload, files={"document": (Path(path).name, handle)}
                )
            ctx["http_status"] = response.status_code
            body = response.json()
            if not body.get("ok"):
                raise TelegramError(
                    f"Telegram sendDocument failed: {body.get('description', response.text[:300])}"
                )
            return (body.get("result") or {}).get("message_id")

    # -------------------------------------------------------------- callbacks

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

    async def _edit_message(
        self,
        chat_id: str,
        message_id: int | None,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Replace a message's text with its resolved state.

        Args:
            chat_id: Chat containing the message.
            message_id: The message to edit; no-ops if falsy.
            text: New HTML body.
            reply_markup: New inline keyboard, or an explicit ``{"inline_keyboard": []}``
                to clear an existing one — Telegram keeps the old keyboard
                attached if this is omitted, it does not clear it automatically.
        """
        if not message_id:
            return
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await self._call("editMessageText", payload, mode="notify", target_ref=chat_id)
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


_MODEL_ALLOWED_TAGS = ("b", "i")


def sanitize_model_html(text: str | None) -> str:
    """Escape AI-generated text for Telegram HTML, except a small allowlist.

    Some prompts deliberately tell a model it may use ``<b>``/``<i>`` for
    emphasis (e.g. bolding a lead's name in a summary) — plain ``escape()``
    would turn those into visibly broken literal "<b>" text instead of real
    bold formatting, which is exactly the wrong tradeoff for output that's
    *meant* to contain those two tags. This escapes everything first (so a
    stray, unexpected, or malformed tag can never break the HTML parse or
    render as unintended markup), then selectively un-escapes only the exact
    allowed tags back to real ones — the model gets its two formatting tools,
    nothing else survives.

    Args:
        text: Model-generated text that may contain ``<b>``/``<i>`` tags.

    Returns:
        Telegram-HTML-safe text with only ``<b>``/``<i>`` live as real tags.
    """
    escaped = escape(text)
    for tag in _MODEL_ALLOWED_TAGS:
        escaped = escaped.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return escaped


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
