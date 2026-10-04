"""Client complaints from one QR code — to the Director via OPS Manager Bot.

A client scans a business's QR code — Londry's leads to ``/f``, Garmin's
to ``/f/garmin`` (pages in ``integrations/api/feedback_page.py``) — writes
the complaint and, if they want, a name and phone number. Leaving both empty
is anonymous. The complaint is stored in ``client_feedback`` with its
business and sent to the Director(s).

Complaints only since 2026-09-28: the page is the company's complaints
channel, so the "opinion" choice is gone (older rows may still say
'feedback'). The QR image itself is drawn by ``qr_card.py``.

Each business has two branches (2026-10-04), and the client picks one with a
button at the top of the page: Londry — Юнусобод / Вузгородок, Garmin — Абай /
Минор. The branch is required, stored with the complaint, and named in the
Director's message.

The page speaks three languages (Uzbek Cyrillic, Russian, English); this
module returns error *keys* the page translates. What the Director gets is
always Uzbek Cyrillic, marked 🔴 so it stands out in OPS Manager Bot, with the
client's language noted when it isn't the default one.

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

KIND = "complaint"
# Where a complaint can be about: URL key -> the brand, as clients know it.
PLACES: dict[str, str] = {
    "laundry": "Londry",
    "garmin": "Garmin",
}
# Each business's branches: key (in the form and the database) -> name for the Director.
BRANCHES: dict[str, dict[str, str]] = {
    "laundry": {"yunusobod": "Юнусобод", "vuzgorodok": "Вузгородок"},
    "garmin": {"abay": "Абай", "minor": "Минор"},
}
assert set(BRANCHES) == set(PLACES)
# The page's languages, as named to the Director.
LANGS: dict[str, str] = {
    "uz_cyrl": "ўзбекча",
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

    place: str
    branch: str
    message: str
    name: str
    phone: str
    lang: str = DEFAULT_LANG
    kind: str = KIND

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
        with an error key — "branch", "empty", "too_long" or "phone" — that
        the page shows in the client's language ("place" means the caller
        passed no valid business: a bug, not the client's mistake).
    """
    place = form.get("place", "")
    if place not in PLACES:
        return None, "place"
    branch = form.get("branch", "")
    if branch not in BRANCHES[place]:
        return None, "branch"
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
    return Submission(place=place, branch=branch, message=message, name=name, phone=phone, lang=lang), None


def where(place: str | None, branch: str | None) -> str:
    """«Garmin · Абай» — the business, and the branch when known."""
    business = PLACES.get(place or "", "")
    branch_name = BRANCHES.get(place or "", {}).get(branch or "", "")
    return " · ".join(part for part in (business, branch_name) if part)


def director_text(sub: Submission) -> str:
    """The message the Director gets (everything the client typed is escaped)."""
    lines = [f"🔴 <b>Мижоз шикояти — {where(sub.place, sub.branch)}</b>", "", f"«{escape(sub.message)}»", ""]
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
        place=sub.place, branch=sub.branch, kind=sub.kind, message=sub.message,
        contact_name=sub.name or None, phone=sub.phone or None,
    )
    delivered = await notify_directors(director_text(sub), agent=AGENT, run_id=uuid.uuid4())
    if row is not None and delivered:
        await store.mark_client_feedback_sent(row["id"], delivered[0])
    log.info("Client complaint ({}) stored and sent to {} director(s)", sub.place, len(delivered))
    return len(delivered)


def describe(rows: list[dict[str, Any]]) -> str:
    """Plain-text data for OPS Manager Bot's answers ("mijozlar fikri")."""
    if not rows:
        return "No client complaints received through the QR code in the last 60 days."
    lines = [f"Client complaints via the QR code, last 60 days: {len(rows)}, newest first:"]
    for r in rows:
        contact = r.get("phone") or r.get("contact_name") or "anonymous"
        place = where(r.get("place"), r.get("branch")) or "not stated"
        # Rows from before 2026-09-28 may be opinions rather than complaints.
        kind = "" if r["kind"] == KIND else f" ({r['kind']})"
        lines.append(f"- [{to_local(r['created_at']):%Y-%m-%d %H:%M}] {place}{kind} | {r['message']} | contact: {contact}")
    return "\n".join(lines)
