"""Client complaints from one QR code — a ticket for the department (2026-10-13), via OPS Manager Bot.

Since 2026-10-13 (the owner's spec of 11.10, A4 LONDRY-CARE / A5
GARMIN-CARE): a complaint is a **ticket** with a code (T-…), sent to its
department by the route table (``routes.py``) — Londry → the Londry role,
Garmin → Garmin sales — **not to the Boss** (he can still ask the bot about
complaints). The card has ✅ Ҳал бўлди → a reason code
(``REASONS``); an hour later, if still open, the department is asked
"ҳал бўлдими?" (✅ / ❌), during 09:00–20:00, at most three times. Nobody in
the role: it falls back to the Boss. Below: the text from before tickets.

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
from integrations.common.config import settings
from integrations.common.timeutil import now_local
from integrations.org_bot import routes, store
from integrations.telegram.bot import TelegramBot, TelegramError
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


# Why a complaint happened — saved with the ticket (the spec: "сабаб коди базага").
REASONS: dict[str, str] = {
    "quality": "Сифат",
    "service": "Хизмат / муомала",
    "machine": "Машина ёки тўлов",
    "delay": "Кечикиш",
    "other": "Бошқа",
}
ASK_HOURS = (9, 20)  # the hourly question only from 09:00 to before 20:00 Tashkent


def ticket_text(row: dict[str, Any]) -> str:
    """The department's card for a complaint ticket (everything the client typed is escaped)."""
    code = f" · {escape(row['code'])}" if row.get("code") else ""
    lines = [f"🔴 <b>Мижоз шикояти — {where(row.get('place'), row.get('branch'))}{code}</b>", "",
             f"«{escape(row['message'])}»", ""]
    if not (row.get("phone") or row.get("contact_name")):
        lines.append("👤 Аноним")
    else:
        contact = " · ".join(p for p in (escape(row.get("phone") or ""), escape(row.get("contact_name") or "")) if p)
        lines.append(f"📞 {contact}")
    if row.get("lang") and row["lang"] != DEFAULT_LANG:
        lines.append(f"🌐 Мижоз тили: {LANGS[row['lang']]}")
    if row.get("ticket_status") == "resolved":
        lines += ["", f"✅ Ҳал бўлди — {escape(row.get('resolved_by') or '')}, сабаби: "
                      f"{REASONS.get(row.get('reason_code') or '', row.get('reason_code') or '')}"]
    return "\n".join(lines)


def ticket_keyboard(row: dict[str, Any], picking_reason: bool = False) -> dict[str, Any]:
    if row.get("ticket_status") != "open":
        return {"inline_keyboard": []}
    if picking_reason:
        buttons = [{"text": label, "callback_data": f"cfr:{row['id']}:{key}"} for key, label in REASONS.items()]
        return {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}
    return {"inline_keyboard": [[{"text": "✅ Ҳал бўлди", "callback_data": f"cfok:{row['id']}"}]]}


def ask_text(row: dict[str, Any]) -> str:
    return (f"⏰ <b>{escape(row.get('code') or '')}</b> — {where(row.get('place'), row.get('branch'))} шикояти "
            "ҳал бўлдими?")


def ask_keyboard(row: dict[str, Any]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "✅ Ҳал бўлди", "callback_data": f"cfok:{row['id']}"},
                                 {"text": "❌ Ҳали йўқ", "callback_data": f"cfno:{row['id']}"}]]}


def _bot(run_id: uuid.UUID) -> TelegramBot:
    return TelegramBot(agent=AGENT, run_id=run_id, bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def _send_cards(row: dict[str, Any], people: list[dict[str, Any]], text: str, keyboard: dict[str, Any],
                      run_id: uuid.UUID) -> list[dict[str, int]]:
    cards: list[dict[str, int]] = []
    async with _bot(run_id) as bot:
        for person in people:
            try:
                ids = await bot.send_message(text, chat_id=str(person["telegram_user_id"]), reply_markup=keyboard)
            except TelegramError as exc:
                log.warning("Complaint {} not delivered to one recipient: {}", row.get("code"), exc)
                continue
            if ids:
                cards.append({"chat": int(person["telegram_user_id"]), "message": int(ids[0])})
    return cards


async def submit(sub: Submission) -> int:
    """Store a complaint, make it a ticket and send it to its department (``routes.py``).

    Returns:
        How many people received it (it's stored either way).
    """
    row = await store.save_client_feedback(
        place=sub.place, branch=sub.branch, kind=sub.kind, message=sub.message,
        contact_name=sub.name or None, phone=sub.phone or None,
    )
    if row is None:
        return 0
    if settings.bots_frozen:  # the emergency stop: stored, nothing sent
        log.info("Bots frozen — complaint stored, not sent")
        return 0
    kind = f"complaint:{sub.place}"
    people, fell_back = await routes.recipients(kind)
    # The kind, not who got it: the hourly question goes to whoever holds the role then.
    ticket = await store.open_complaint_ticket(row["id"], kind) or row
    text = ticket_text({**ticket, "lang": sub.lang})
    if fell_back:
        text += "\n\n<i>Бу бўлимда ҳозир ҳеч ким рўйхатдан ўтмаган — шунинг учун сизга келди.</i>"
    run_id = uuid.uuid4()
    cards = await _send_cards(ticket, people, text, ticket_keyboard(ticket), run_id)
    if cards:
        await store.set_complaint_cards(row["id"], cards)
        await store.mark_client_feedback_sent(row["id"], cards[0]["message"])
    log.info("Complaint ticket {} ({}) sent to {} person(s){}", ticket.get("code"), sub.place, len(cards),
             " — fell back to the Director" if fell_back else "")
    return len(cards)


async def update_cards(row: dict[str, Any], run_id: uuid.UUID) -> None:
    """Every card of a ticket shows its new state (resolved: who, why; no buttons)."""
    async with _bot(run_id) as bot:
        for card in row.get("cards") or []:
            try:
                await bot._edit_message(  # noqa: SLF001 — same-package reuse of a generic edit helper
                    chat_id=str(card["chat"]), message_id=card["message"], text=ticket_text(row),
                    reply_markup=ticket_keyboard(row),
                )
            except TelegramError as exc:
                log.warning("Complaint card not updated: {}", exc)


async def ask_status(now: Any = None) -> int:
    """The hourly question for open tickets (09:00–20:00); returns how many tickets were asked about."""
    now = now or now_local()
    if not ASK_HOURS[0] <= now.hour < ASK_HOURS[1]:
        return 0
    due = await store.complaints_due_status_check()
    run_id = uuid.uuid4()
    for row in due:
        kind = row.get("routed_to") or f"complaint:{row.get('place')}"
        people, _fell_back = await routes.recipients(kind)
        asked = await _send_cards(row, people, ask_text(row), ask_keyboard(row), run_id)
        # The question's messages join the ticket's cards: whoever was asked can press ✅ / ❌ (even
        # someone who took the role after the complaint came), and they're updated when it's resolved.
        if asked:
            await store.set_complaint_cards(row["id"], asked)
    return len(due)


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
