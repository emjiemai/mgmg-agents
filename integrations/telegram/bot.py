"""Telegram Bot API client.

This is the delivery channel for briefs, alerts, and both org_bot bots
(Admin Bot and OPS Manager Bot).

Messages are HTML-formatted and split at 4096 characters on line boundaries so
a long brief never gets truncated or breaks a tag mid-way.
"""

from __future__ import annotations

import asyncio

import html
import json
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx

from integrations.common.config import settings
from integrations.common.db import audited, log_action
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging

log = setup_logging("telegram")

SAFE_CHUNK = 3900  # leaves room for the "(1/3)" continuation marker

Severity = Literal["critical", "warning", "info"]

SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "warning": "🟡",
    "info": "🟢",
}


# A kept file sent again by its file_id: the Bot API method and its field, per kind of file.
MEDIA_METHODS: dict[str, tuple[str, str]] = {
    "photo": ("sendPhoto", "photo"), "video": ("sendVideo", "video"), "voice": ("sendVoice", "voice"),
    "audio": ("sendAudio", "audio"), "document": ("sendDocument", "document"),
    "animation": ("sendAnimation", "animation"), "video_note": ("sendVideoNote", "video_note"),
}

class TelegramError(RuntimeError):
    """Raised when the Telegram Bot API rejects a request."""


# One kept-alive connection pool per bot token and event loop (2026-10-13).
# Opening a new HTTPS connection for every call cost ~400–650 ms each (the
# audit log: answerCallbackQuery 458 ms, editMessageText 663 ms on average),
# and one button press makes 2–4 calls — buttons felt slow. A reused
# connection skips the TCP + TLS handshake. Keyed by loop as well: a cron run
# has its own ``asyncio.run`` loop, and a client must not outlive its loop.
_SHARED: dict[str, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def _shared_client(bot_token: str) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    entry = _SHARED.get(bot_token)
    if entry is not None and entry[0] is loop and not entry[1].is_closed:
        return entry[1]
    client = httpx.AsyncClient(
        base_url=f"https://api.telegram.org/bot{bot_token}",
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=120.0),
    )
    _SHARED[bot_token] = (loop, client)
    return client


async def close_shared_clients() -> None:
    """Close the kept-alive connections of this loop (the API service's shutdown)."""
    loop = asyncio.get_running_loop()
    for token, (owner, client) in list(_SHARED.items()):
        if owner is loop:
            await client.aclose()
            _SHARED.pop(token, None)


# Button presses already answered (answerCallbackQuery may be sent once).
_ACKED: dict[str, None] = {}
_ACK_TASKS: set[asyncio.Task] = set()
# Seconds: a handler slower than this gets its spinner stopped for it. Long enough that a
# normal handler answers first with its own text ("Рухсат йўқ", "Аллақачон ҳал қилинган").
ACK_AFTER = 1.0
# Buttons whose answer IS the content: never answered early (none since team cheer was removed, 2026-10-17).
NO_EARLY_ACK: tuple[str, ...] = ()


def ack_soon(bot_token: str, callback_query_id: str, delay: float = ACK_AFTER, data: str = "") -> None:
    """Stop a button's spinner within ``delay`` seconds even if its handler is still working (2026-10-13).

    A handler that answers first keeps its own text ("OK", "Аллақачон ҳал
    қилинган"); one that is slower gets a silent answer here, and its own
    later answer is skipped (``_answer_callback``).
    """
    if not callback_query_id or not bot_token or data.startswith(NO_EARLY_ACK):
        return

    async def later() -> None:
        await asyncio.sleep(delay)
        if callback_query_id in _ACKED:
            return
        try:
            async with TelegramBot(agent="telegram-ack", bot_token=bot_token) as bot:
                await bot._answer_callback(callback_query_id, "")
        except Exception as exc:  # noqa: BLE001 — a late ack is a nicety, never an error
            log.warning("Early button answer failed: {}", type(exc).__name__)

    task = asyncio.get_running_loop().create_task(later())
    _ACK_TASKS.add(task)
    task.add_done_callback(_ACK_TASKS.discard)


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

        self._client = _shared_client(self._bot_token)
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Let go of the client — the connection stays open for the next call (``_shared_client``)."""
        self._client = None

    # ---------------------------------------------------------------- sending

    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool = False,
        reply_to_message_id: int | None = None,
    ) -> list[int]:
        """Send an HTML message, splitting it if it exceeds Telegram's limit.

        ``reply_to_message_id``: shown as a reply to that message (the first chunk), so an
        answer sits under its own question; sent anyway if that message is gone.

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
            if reply_to_message_id and index == 1:
                payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}

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
        """Upload a file from disk to one chat (see ``send_file``)."""
        return await self.send_file(Path(path).read_bytes(), Path(path).name, chat_id, caption)

    async def send_file(
        self, content: bytes, filename: str, chat_id: str, caption: str | None = None, *, photo: bool = False,
        reply_to: int | None = None,
    ) -> int | None:
        """Upload bytes to one chat: a document (PDF, PNG kept sharp) or, with
        ``photo``, a picture shown inline (the morning brief).

        Uses multipart rather than ``_call``'s JSON body — Telegram takes an
        upload only as form data.

        Args:
            content: The file's bytes.
            filename: The name the chat shows.
            chat_id: Destination chat.
            caption: Optional HTML caption (Telegram allows 1024 characters).
            photo: Send with sendPhoto instead of sendDocument.
            reply_to: Shown as a reply to that message (the question it answers); sent anyway if it's gone.

        Returns:
            The Telegram message id, or None in dry-run mode.

        Raises:
            TelegramError: if Telegram rejects the upload.
        """
        method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
        if settings.dry_run:
            log.info("[dry run] telegram.{} -> {} ({}, {} bytes)", method, chat_id, filename, len(content))
            return None

        assert self._client is not None
        payload: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        if reply_to:  # form data: the nested object goes as JSON text
            payload["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})

        async with audited(
            agent=self.agent,
            action=f"telegram_{method}",
            target_system="telegram",
            run_id=self.run_id,
            target_ref=str(chat_id),
            mode="notify",
            payload={"file": filename, "bytes": len(content)},
        ) as ctx:
            response = await self._client.post(f"/{method}", data=payload, files={field: (filename, content)})
            ctx["http_status"] = response.status_code
            body = response.json()
            if not body.get("ok"):
                raise TelegramError(f"Telegram {method} failed: {body.get('description', response.text[:300])}")
            return (body.get("result") or {}).get("message_id")

    async def download_file(self, file_id: str, max_bytes: int = 10_000_000) -> bytes:
        """A file someone sent the bot, as bytes (getFile, then the file URL).

        Raises:
            TelegramError: if Telegram has no such file, it is larger than
                ``max_bytes``, or the download fails.
        """
        assert self._client is not None
        info = await self._call("getFile", {"file_id": file_id}, mode="read") or {}
        path = info.get("file_path")
        if not path:
            raise TelegramError("Telegram getFile returned no file_path")
        if int(info.get("file_size") or 0) > max_bytes:
            raise TelegramError(f"file too large ({info.get('file_size')} bytes)")
        # The file URL is /file/bot<token>/<path>, beside the API root /bot<token>.
        response = await self._client.get(f"https://api.telegram.org/file/bot{self._bot_token}/{path}")
        if response.status_code != 200 or len(response.content) > max_bytes:
            raise TelegramError(f"Telegram file download failed: HTTP {response.status_code}")
        return response.content

    async def send_media(self, kind: str, file_id: str, chat_id: str, caption: str | None = None,
                         reply_to: int | None = None) -> list[int]:
        """Send again a file the bot was sent (by its Telegram file_id): a report's photo, video, voice…
        (2026-10-22 — kept in the database, sent when someone asks for that report)."""
        method, field = MEDIA_METHODS.get(kind, ("sendDocument", "document"))
        payload: dict[str, Any] = {"chat_id": chat_id, field: file_id}
        if caption and kind != "video_note":  # a round video takes no caption
            payload.update(caption=caption[:1024], parse_mode="HTML")
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = await self._call(method, payload, mode="notify", target_ref=str(chat_id))
        return [result["message_id"]] if isinstance(result, dict) and result.get("message_id") else []

    # -------------------------------------------------------------- callbacks

    async def _answer_callback(self, callback_query_id: str, text: str) -> None:
        """Acknowledge a button press so the client stops its spinner — once (see ``ack_soon``)."""
        if not callback_query_id or callback_query_id in _ACKED:
            return
        _ACKED[callback_query_id] = None
        while len(_ACKED) > 2000:
            _ACKED.pop(next(iter(_ACKED)))
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
        # An edit can't be split like a new message: one over Telegram's limit
        # was refused outright (MESSAGE_TOO_LONG, 2026-10-07) and the message
        # stayed as it was. Shorten it instead.
        text = fit_for_edit(text)
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


EDIT_LIMIT = 4000  # Telegram's 4096 visible characters, with room for the "…" note


def fit_for_edit(text: str, limit: int = EDIT_LIMIT) -> str:
    """An HTML message body short enough to edit a message with.

    Telegram counts the visible text (after the tags). Within the limit it is
    returned as it is; over it, the tags are dropped (cutting inside one would
    break the HTML) and the text is cut on a line or word boundary with "…".
    """
    import re

    visible = html.unescape(re.sub(r"<[^>]+>", "", text))
    if len(visible) <= limit:
        return text
    cut = visible[:limit]
    boundary = max(cut.rfind("\n"), cut.rfind(" "))
    if boundary > limit * 0.8:
        cut = cut[:boundary]
    return html.escape(cut.rstrip(), quote=False) + "\n…"


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
