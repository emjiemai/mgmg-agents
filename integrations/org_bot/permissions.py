"""Written permission requests — EMJ-SOP-ADM-01 ("Ёзма рухсат ва тасдиқ олиш тартиби").

The SOP's page-2 form is the contract this module implements: the same fields,
in the same order, with the same four decision outcomes. Everything an
employee or approver reads here is in Uzbek Cyrillic, matching the document.

Answers are kept exactly as the employee wrote them — they go onto the official
form word for word, so nothing here rewrites, completes or reformats them.

Pure functions only (no DB, no Telegram), so the question order, amount
parsing and card rendering are exercised offline in scripts/selfcheck.py.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

SOP_CODE = "EMJ-SOP-ADM-01"

# Both alphabets, because people type either one: "ruxsat", "рухсат".
TRIGGER_WORDS = ("ruxsat", "рухсат", "ruhsat", "разреш", "permission")
COMMANDS = ("/ruxsat", "/ruhsat", "/permission")
CANCEL_COMMANDS = ("/bekor", "/cancel", "/бекор")


@dataclass(frozen=True)
class Field:
    """One question the bot asks, mapped to a blank on the SOP form.

    Attributes:
        key: Column in ``permission_requests``.
        label: The SOP form's own wording.
        question: What the bot asks, in Uzbek Cyrillic.
        rule: What a valid answer looks like — given to the AI that checks
            each answer before it is accepted onto the official form.
    """

    key: str
    label: str
    question: str
    rule: str


# Order follows the SOP form top to bottom.
FIELDS: tuple[Field, ...] = (
    Field(
        "requester_full_name",
        "Исм ва фамилия",
        "Исмингиз ва фамилиянгизни тўлиқ ёзинг (масалан: Алишер Каримов).",
        "a person's real first name AND surname (two words at least); a nickname, a single name "
        "or a Telegram-style handle is not enough",
    ),
    Field(
        "requester_position",
        "Лавозим",
        "Лавозимингиз қандай? (масалан: сотув менежери, бухгалтер)",
        "a real job title or position",
    ),
    Field(
        "department",
        "Бўлим",
        "Қайси бўлимда ишлайсиз?",
        "a real department or team name",
    ),
    Field(
        "subject",
        "Нимага рухсат сўралади",
        "Нимага рухсат сўраяпсиз? Қисқа ва аниқ ёзинг.",
        "a concrete, specific thing the employee asks permission for",
    ),
    Field(
        "reason",
        "Сабаб ва таклиф",
        "Сабабини ва таклифингизни ёзинг: нима учун керак, қандай ҳал қилишни таклиф қиласиз?",
        "an actual explanation of why it is needed and what they propose",
    ),
    Field(
        "amount",
        "Сумма ва валюта",
        "Сумма ва валютани ёзинг. Харажат бўлмаса «0» деб ёзинг.",
        "'0' when there is no cost, or an amount that contains a number (a currency is expected; "
        "an approximate amount with a number is acceptable)",
    ),
    Field(
        "execute_by",
        "Бажариш муддати",
        "Ишни қачонгача бажариш керак? (масалан: 25.09.2026)",
        "an identifiable date or a clear time frame (e.g. 25.09.2026, 'эртага', 'шу ҳафта ичида')",
    ),
    Field(
        "decision_needed_by",
        "Қарор керак бўлган сана ва вақт",
        "Қарор қачонгача керак? (сана ва вақт)",
        "an identifiable date/time or a clear time reference (e.g. '22.09.2026 12:00', 'бугун 15:00', "
        "'ҳозир', 'эртага')",
    ),
    Field(
        "urgency",
        "Шошилинчлик сабаби ёки «оддий»",
        "Шошилинч бўлса сабабини ёзинг. Шошилинч бўлмаса «оддий» деб ёзинг.",
        "either the word 'оддий'/'oddiy' (not urgent) or a real reason why it is urgent",
    ),
    Field(
        "attachments",
        "Иловалар",
        "Илова қиладиган ҳужжат борми? Номини ёзинг, бўлмаса «йўқ» деб ёзинг.",
        "names of the documents attached, or 'йўқ'/'yo'q' when there are none",
    ),
)

FIELD_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}

# Asked of an approver (once — then remembered) and for the text they owe
# with a conditional approval, a rejection or an information request.
APPROVER_NAME_FIELD = Field(
    "approver_full_name",
    "Тасдиқловчи исми",
    "Шаклга ёзиш учун исмингиз ва фамилиянгизни тўлиқ ёзинг:",
    "a person's real first name AND surname (two words at least)",
)
DECISION_NOTE_FIELDS: dict[str, Field] = {
    "approved_conditional": Field(
        "decision_note", "Шартлар", "Шартларни, тасдиқланган сумма ва амал қилиш муддатини ёзинг:",
        "the actual conditions of the approval",
    ),
    "rejected": Field(
        "decision_note", "Рад этиш сабаби", "Рад этиш сабабини ёзинг:",
        "an actual reason for the rejection",
    ),
    "info_needed": Field(
        "decision_note", "Керакли маълумот", "Қандай қўшимча маълумот керак? Ёзинг:",
        "what additional information is needed",
    ),
}

# Role names as they appear on the Cyrillic SOP form (roles.py holds the
# Latin labels the rest of the bot uses).
ROLE_LABELS_CYR: dict[str, str] = {
    "b2b_sotuv": "B2B сотув бўлими",
    "it": "IT бўлими",
    "buxgalteriya": "Бухгалтерия",
    "hr": "Кадрлар бўлими (HR)",
    "ombor": "Омбор",
    "operatsion_direktor": "Операцион директор",
    "mobilograf": "Мобилограф",
    "aloqa_markazi": "Алоқа маркази",
    "garmin_sotuv": "Garmin сотув бўлими",
}


def role_label_cyr(role: str) -> str:
    """A role's name for the SOP form."""
    return ROLE_LABELS_CYR.get(role, role)

# The SOP's four outcomes, with the status stored for each.
DECISIONS: dict[str, tuple[str, str]] = {
    "approved": ("✅", "Тасдиқланди"),
    "approved_conditional": ("⚠️", "Шарт билан тасдиқланди"),
    "rejected": ("❌", "Рад этилди"),
    "info_needed": ("ℹ️", "Қўшимча маълумот керак"),
}

STATUS_LABELS: dict[str, str] = {
    "draft": "Тўлдирилмоқда",
    "submitted": "Қарор кутилмоқда",
    "cancelled": "Бекор қилинди",
    **{key: label for key, (_emoji, label) in DECISIONS.items()},
}

_CURRENCIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("USD", ("$", "usd", "доллар", "dollar")),
    ("EUR", ("€", "eur", "евро", "euro")),
    ("UZS", ("сўм", "сум", "so'm", "som", "uzs")),
)

# Words that turn a mention of "рухсат" into an actual request. Without one,
# "директор рухсат берди" ("the director gave permission") would open a form.
_REQUEST_CUES = (
    "kerak", "керак", "so'ra", "sora", "сўра", "сура", "bering", "беринг",
    "mumkinmi", "мумкинми", "olsam", "олсам", "прошу", "нужно", "нужна", "можно",
)


def _h(value: Any) -> str:
    """Escape anything a person typed before it goes into Telegram HTML.

    Telegram rejects a whole message whose HTML doesn't parse, so a single
    "&" or "<" in an answer would otherwise make the approver's card silently
    fail to arrive.
    """
    return html.escape(str(value), quote=False)


def wants_permission(text: str) -> bool:
    """Whether this message is starting a written permission request.

    A command always counts. Otherwise the message must both mention
    permission and read like a request, and be short enough to be one.

    Args:
        text: The employee's message.

    Returns:
        True if the permission flow should start.
    """
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if any(lowered.startswith(command) for command in COMMANDS):
        return True
    if not any(word in lowered for word in TRIGGER_WORDS):
        return False
    if len(lowered) > 400:
        return False
    return any(cue in lowered for cue in _REQUEST_CUES)


def is_cancel(text: str) -> bool:
    """Whether the message cancels the request being filled in."""
    return (text or "").strip().lower() in CANCEL_COMMANDS


def parse_amount(text: str) -> tuple[int | None, str]:
    """Read a number and currency out of the "Сумма ва валюта" answer.

    Used only for the register's machine-readable columns — the form itself
    always shows the answer exactly as typed.

    Args:
        text: What the employee typed, e.g. "5 000 000 сўм", "$1200", "0".

    Returns:
        ``(amount in minor units or None, currency code)``. Never guessed:
        no number means None.
    """
    lowered = (text or "").lower().replace(" ", " ")
    currency = "UZS"
    for code, markers in _CURRENCIES:
        if any(marker in lowered for marker in markers):
            currency = code
            break

    match = re.search(r"\d[\d\s.,]*", lowered)
    if not match:
        return None, currency
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None, currency
    return int(digits) * 100, currency


def parse_tiers(text: str) -> list[tuple[int, int]]:
    """Read the B1 approval limits setting ("5000000:111,20000000:222").

    Args:
        text: ``PERMISSION_APPROVAL_TIERS`` — limit in whole so'm, a colon,
            and the Telegram id of whoever decides up to that limit.

    Returns:
        ``(limit in tiyin, telegram id)`` pairs, smallest limit first.
        Malformed pairs are skipped, so a typo can't take the flow down.
    """
    tiers: list[tuple[int, int]] = []
    for part in (text or "").split(","):
        limit, _, telegram_id = part.strip().partition(":")
        limit = limit.replace(" ", "").replace("_", "")
        telegram_id = telegram_id.strip()
        if limit.isdigit() and telegram_id.lstrip("-").isdigit() and int(limit) > 0:
            tiers.append((int(limit) * 100, int(telegram_id)))
    return sorted(tiers)


def tier_approver(amount_tiyin: int | None, currency: str | None, tiers: list[tuple[int, int]]) -> int | None:
    """Who decides this amount under the B1 limits, if anyone below the Director.

    Returns:
        The Telegram id of the lowest tier whose limit covers the amount, or
        None when the Director decides: no tiers configured, no cost, not in
        so'm, unreadable, or above every limit.
    """
    if not tiers or not amount_tiyin or amount_tiyin <= 0 or (currency or "UZS") != "UZS":
        return None
    for limit, telegram_id in tiers:
        if amount_tiyin <= limit:
            return telegram_id
    return None


def format_amount(amount_tiyin: int | None, currency: str, raw: str | None = None) -> str:
    """Render a parsed amount, for places that only have the parsed figure.

    Args:
        amount_tiyin: Stored minor units, or None when nothing parsed.
        currency: Currency code.
        raw: The employee's original answer.

    Returns:
        The raw answer when there is one (the form never shows anything the
        employee didn't write), otherwise a grouped figure.
    """
    if raw and raw.strip():
        return raw.strip()
    if amount_tiyin is None:
        return "—"
    if amount_tiyin == 0:
        return "0"
    whole = amount_tiyin // 100
    grouped = f"{whole:,}".replace(",", " ")
    suffix = {"UZS": "сўм", "USD": "$", "EUR": "€"}.get(currency, currency)
    return f"{grouped} {suffix}" if currency == "UZS" else f"{suffix}{grouped}"


def _amount_answered(request: dict[str, Any]) -> bool:
    """Whether "Сумма ва валюта" has been answered (raw text is the answer)."""
    return request.get("amount_tiyin") is not None or bool((request.get("amount_raw") or "").strip())


def next_missing_field(request: dict[str, Any]) -> Field | None:
    """The next question to ask for this draft.

    Args:
        request: A ``permission_requests`` row (or dict of collected answers).

    Returns:
        The first field with no answer yet, or None when the form is complete.
    """
    for field in FIELDS:
        if field.key == "amount":
            if not _amount_answered(request):
                return field
            continue
        value = request.get(field.key)
        if value is None or not str(value).strip():
            return field
    return None


def field_value(request: dict[str, Any], field: Field) -> str:
    """One field's answer exactly as typed (plain text, not HTML-safe)."""
    if field.key == "amount":
        return format_amount(request.get("amount_tiyin"), request.get("currency") or "UZS", request.get("amount_raw"))
    return (str(request.get(field.key) or "—")).strip()


def summary_lines(request: dict[str, Any]) -> list[str]:
    """The request's fields as HTML-safe "label: value" lines, in SOP order."""
    return [f"<b>{field.label}:</b> {_h(field_value(request, field))}" for field in FIELDS]


def request_card(request: dict[str, Any], *, for_approver: bool) -> str:
    """The request as a Telegram card (every typed value escaped).

    Args:
        request: A ``permission_requests`` row.
        for_approver: Kept for callers; both sides now see the same card,
            which already starts with the requester's full name.
    """
    number = request.get("request_no") or "—"
    lines = [f"📄 <b>Ёзма рухсат сўрови</b> № {_h(number)}", f"<i>{SOP_CODE}</i>", ""]
    lines.extend(summary_lines(request))
    return "\n".join(lines)


def decision_text(request: dict[str, Any]) -> str:
    """The decision block, as the requester sees it (Telegram HTML, escaped)."""
    status = request.get("status") or ""
    emoji, label = DECISIONS.get(status, ("", STATUS_LABELS.get(status, status)))
    lines = [f"{emoji} <b>{label}</b>", f"№ {_h(request.get('request_no') or '—')}"]
    if request.get("approved_terms"):
        lines.append(f"<b>Тасдиқланган сумма ва муддат:</b> {_h(request['approved_terms'])}")
    if request.get("decision_note"):
        lines.append(f"<b>Шартлар ёки сабаб:</b> {_h(request['decision_note'])}")
    if request.get("decided_by"):
        lines.append(f"<b>Тасдиқловчи:</b> {_h(request['decided_by'])}")
    return "\n".join(lines)


def confirm_keyboard(request_id: str) -> dict[str, Any]:
    """Send / cancel buttons shown to the requester before submitting."""
    return {
        "inline_keyboard": [
            [
                {"text": "📨 Юбориш", "callback_data": f"permsend:{request_id}"},
                {"text": "❌ Бекор қилиш", "callback_data": f"permcancel:{request_id}"},
            ]
        ]
    }


# Telegram caps a button's callback_data at 64 bytes and rejects the WHOLE
# message if one button exceeds it. "permdec:approved_conditional:" plus a
# 36-character UUID is 65 bytes — which is why approvers never received a
# single card. One-letter codes keep every button far under the limit.
DECISION_CODES: dict[str, str] = {
    "a": "approved",
    "c": "approved_conditional",
    "r": "rejected",
    "i": "info_needed",
}
_CODE_BY_DECISION: dict[str, str] = {status: code for code, status in DECISION_CODES.items()}


def decision_keyboard(request_id: str) -> dict[str, Any]:
    """The approver's four SOP outcomes, two per row so labels stay readable."""

    def button(text: str, decision: str) -> dict[str, str]:
        return {"text": text, "callback_data": f"permdec:{_CODE_BY_DECISION[decision]}:{request_id}"}

    return {
        "inline_keyboard": [
            [button("✅ Тасдиқлаш", "approved"), button("⚠️ Шарт билан", "approved_conditional")],
            [button("❌ Рад этиш", "rejected"), button("ℹ️ Маълумот", "info_needed")],
        ]
    }
