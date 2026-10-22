"""Who may open PULSE, and how (2026-10-22): a link from OPS Manager Bot, no password.

The Director (everyone) and the Garmin manager (his team, report_calendar.TEAM_OF) send /pulse; the bot
answers with a link that works for ``LINK_MINUTES``. Opening it sets a session cookie (``SESSION_HOURS``)
and drops the token from the address. Tokens are HMAC-signed with a key derived from the OPS Manager
webhook secret; every request checks the person is still active in that role.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from integrations.common.config import settings

LINK_MINUTES = 15
SESSION_HOURS = 12
COOKIE = "pulse"
ROLES = ("operatsion_direktor", "garmin_manager")


def _key() -> bytes:
    secret = settings.ops_manager_bot_webhook_secret.get_secret_value()
    if not secret:
        raise RuntimeError("OPS_MANAGER_BOT_WEBHOOK_SECRET is not set — PULSE links can't be signed")
    return hashlib.sha256(b"pulse|" + secret.encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(telegram_user_id: int, kind: str, seconds: int, now: float | None = None) -> str:
    body = _b64(json.dumps({"t": int(telegram_user_id), "k": kind, "e": int((now or time.time()) + seconds)}).encode())
    return f"{body}.{_b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())}"


def verify(token: str | None, kind: str, now: float | None = None) -> int | None:
    """The Telegram id a valid, unexpired token of this kind carries; None otherwise."""
    try:
        body, sig = (token or "").split(".", 1)
        good = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        data: dict[str, Any] = json.loads(_unb64(body))
    except (ValueError, TypeError, RuntimeError):
        return None
    if data.get("k") != kind or int(data.get("e") or 0) < (now or time.time()):
        return None
    return int(data["t"])


def link_token(telegram_user_id: int) -> str:
    return sign(telegram_user_id, "link", LINK_MINUTES * 60)


def session_token(telegram_user_id: int) -> str:
    return sign(telegram_user_id, "session", SESSION_HOURS * 3600)


def link(telegram_user_id: int) -> str:
    return f"{settings.public_url}/pulse/enter?t={link_token(telegram_user_id)}"
