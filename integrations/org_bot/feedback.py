"""Client feedback and complaints from one QR code — to the Director via OPS Manager Bot.

A client scans the company's QR code, gets a one-screen page (served by
``integrations/api/feedback_page.py`` at ``/f``), writes an opinion or a
complaint and, if they want, a name and phone number. Leaving both empty is
anonymous. The message is stored in ``client_feedback`` and sent to the
Director(s).

One code for the whole company (2026-09-28: the business isn't split into
separate services for clients), so there is no place label. The QR image
itself is drawn by ``qr_card.py``.

The page speaks four languages (Uzbek Cyrillic and Latin, Russian, English);
this module returns error *keys* the page translates. What the Director gets
is always Uzbek Cyrillic, with the client's language noted when it isn't the
default one.

Pure validation and message text here (tested offline); the page, storage
and delivery call into it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import to_local
from integrations.org_bot import store
from integrations.org_bot.notify import notify_directors
from integrations.telegram.bot import escape

AGENT = "client-feedback"
log = setup_logging(AGENT)

KINDS: dict[str, tuple[str, str]] = {
    "feedback": ("💬", "Фикр"),
    "complaint": ("⚠️", "Шикоят"),
}
# The page's languages, as named to the Director.
LANGS: dict[str, str] = {
    "uz_cyrl": "ўзбекча (кирилл)",
    "uz_latn": "ўзбекча (лотин)",
    "ru": "русча",
    "en": "инглизча",
}
DEFAULT_LANG = "uz_cyrl"
MESSAGE_MIN, MESSAGE_MAX = 3, 2000
NAME_MAX = 60
_PHONE_CHARS = re.compile(r"^[0-9+()\-\s]{7,25}$")


@dataclass
class Submission:
    """One validated form: what the client wrote, and how to reach them (if at all)."""

    kind: str
    message: str
    name: str
    phone: str
    lang: str = DEFAULT_LANG

    @property
    def anonymous(self) -> bool:
        return not self.name and not self.phone


def normalize_phone(raw: str) -> str | None:
    """A readable phone number, or None if it isn't one.

    Accepts the ways people type Uzbek numbers ("90 123 45 67",
    "+998901234567", "(90) 123-45-67"); keeps the digits and a leading +.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not _PHONE_CHARS.match(raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return None
    return ("+" if raw.startswith("+") else "") + digits


def clean(form: dict[str, str]) -> tuple[Submission | None, str | None]:
    """Validate a submitted form.

    Returns:
        ``(submission, None)`` when it's acceptable, else ``(None, error)``
        with an error key — "empty", "too_long" or "phone" — that the page
        shows in the client's language.
    """
    kind = form.get("kind", "feedback")
    if kind not in KINDS:
        kind = "feedback"
    lang = form.get("lang") if form.get("lang") in LANGS else DEFAULT_LANG
    message = " ".join((form.get("message") or "").split())
    if len(message) < MESSAGE_MIN:
        return None, "empty"
    if len(message) > MESSAGE_MAX:
        return None, "too_long"
    name = " ".join((form.get("name") or "").split())[:NAME_MAX]
    phone = normalize_phone(form.get("phone") or "")
    if phone is None:
        return None, "phone"
    return Submission(kind=kind, message=message, name=name, phone=phone, lang=lang), None


def director_text(sub: Submission) -> str:
    """The message the Director gets (everything the client typed is escaped)."""
    emoji, label = KINDS[sub.kind]
    lines = [f"{emoji} <b>Мижоз: {label.lower()}</b>", "", f"«{escape(sub.message)}»", ""]
    if sub.anonymous:
        lines.append("👤 Аноним")
    else:
        contact = " · ".join(part for part in (escape(sub.phone) if sub.phone else "", escape(sub.name)) if part)
        lines.append(f"📞 {contact}")
    if sub.lang != DEFAULT_LANG:
        lines.append(f"🌐 Мижоз тили: {LANGS[sub.lang]}")
    return "\n".join(lines)


async def submit(sub: Submission) -> int:
    """Store a submission and send it to the Director(s).

    Returns:
        How many Directors received it (it's stored either way).
    """
    row = await store.save_client_feedback(
        kind=sub.kind, message=sub.message, contact_name=sub.name or None, phone=sub.phone or None
    )
    delivered = await notify_directors(director_text(sub), agent=AGENT, run_id=uuid.uuid4())
    if row is not None and delivered:
        await store.mark_client_feedback_sent(row["id"], delivered[0])
    log.info("Client {} stored and sent to {} director(s)", sub.kind, len(delivered))
    return len(delivered)


def describe(rows: list[dict[str, Any]]) -> str:
    """Plain-text data for OPS Manager Bot's answers ("mijozlar fikri")."""
    if not rows:
        return "No client feedback or complaints received through the QR code in the last 60 days."
    complaints = sum(1 for r in rows if r["kind"] == "complaint")
    lines = [f"Client feedback via the QR code, last 60 days: {len(rows)} ({complaints} complaint(s)), newest first:"]
    for r in rows:
        contact = r.get("phone") or r.get("contact_name") or "anonymous"
        lines.append(f"- [{to_local(r['created_at']):%Y-%m-%d %H:%M}] {r['kind']} | {r['message']} | contact: {contact}")
    return "\n".join(lines)
