"""Written permission requests — EMJ-SOP-ADM-01 ("Ёзма рухсат ва тасдиқ олиш тартиби").

The SOP's page-2 form is the contract this module implements: the same fields,
in the same order, with the same four decision outcomes. Everything an
employee or approver reads here is in Uzbek Cyrillic, matching the document
itself — a filled form in one alphabet and a chat trail in another would be
awkward to file together.

Pure functions only (no DB, no Telegram), so the question order, amount
parsing and card rendering are exercised offline in scripts/selfcheck.py.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Sequence

SOP_CODE = "EMJ-SOP-ADM-01"

# Both alphabets, because people type either one: "ruxsat", "рухсат".
TRIGGER_WORDS = ("ruxsat", "рухсат", "ruhsat", "разреш", "permission")
COMMANDS = ("/ruxsat", "/ruhsat", "/permission")


@dataclass(frozen=True)
class Field:
    """One question the bot asks, mapped to a field of the SOP form.

    Attributes:
        key: Column in ``permission_requests``.
        label: The SOP form's own wording, used in the card and the .docx.
        question: What the bot asks, in Uzbek Cyrillic.
    """

    key: str
    label: str
    question: str


# Order matters: it is the order of the SOP form, so a filled request reads
# top to bottom exactly like the paper version.
FIELDS: tuple[Field, ...] = (
    Field(
        "subject",
        "Нимага рухсат сўралади",
        "Нимага рухсат сўраяпсиз? Қисқа ва аниқ ёзинг.",
    ),
    Field(
        "reason",
        "Сабаб ва таклиф",
        "Сабабини ва таклифингизни ёзинг: нима учун керак, қандай ҳал қилишни таклиф қиласиз?",
    ),
    Field(
        "amount",
        "Сумма ва валюта",
        "Сумма ва валютани ёзинг. Харажат бўлмаса «0» деб ёзинг.",
    ),
    Field(
        "execute_by",
        "Бажариш муддати",
        "Ишни қачонгача бажариш керак? (масалан: 25.09.2026)",
    ),
    Field(
        "decision_needed_by",
        "Қарор керак бўлган сана ва вақт",
        "Қарор қачонгача керак? (сана ва вақт)",
    ),
    Field(
        "urgency",
        "Шошилинчлик сабаби",
        "Шошилинч бўлса сабабини ёзинг. Шошилинч бўлмаса «оддий» деб ёзинг.",
    ),
    Field(
        "attachments",
        "Иловалар",
        "Илова қиладиган ҳужжат борми? Номини ёзинг, бўлмаса «йўқ» деб ёзинг.",
    ),
)

FIELD_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}

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
    "&" or "<" in an employee's answer ("A&B", "нарх < 5 млн") would otherwise
    make the approver's card silently fail to arrive.
    """
    return html.escape(str(value), quote=False)


def wants_permission(text: str) -> bool:
    """Whether this message is starting a written permission request.

    A command always counts. Otherwise the message must both mention
    permission and read like a request — and be short enough to be one: a
    long message that happens to contain the word is almost always a report
    about a permission, not a request for one.

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


def parse_amount(text: str) -> tuple[int | None, str]:
    """Read "Сумма ва валюта" out of a free-text answer.

    Args:
        text: What the employee typed, e.g. "5 000 000 сўм", "$1200", "0".

    Returns:
        ``(amount in minor units, currency code)``. The amount is None when no
        number was found at all — never guessed, so the form shows the raw
        answer rather than an invented figure. Currency defaults to UZS.
    """
    lowered = (text or "").lower().replace(" ", " ")
    currency = "UZS"
    for code, markers in _CURRENCIES:
        if any(marker in lowered for marker in markers):
            currency = code
            break

    # Keep digits with their separators, then drop the separators: "5 000 000"
    # and "5.000.000" are both five million, not 5.
    match = re.search(r"\d[\d\s.,]*", lowered)
    if not match:
        return None, currency
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None, currency
    return int(digits) * 100, currency


def format_amount(amount_tiyin: int | None, currency: str, raw: str | None = None) -> str:
    """Render the amount for the card and the form.

    Args:
        amount_tiyin: Stored minor units, or None when nothing parsed.
        currency: Currency code.
        raw: The employee's original answer, used when nothing parsed.

    Returns:
        "0" for a no-cost request, a grouped amount with its currency
        otherwise, or the raw answer when it couldn't be read as a number.
    """
    if amount_tiyin is None:
        return (raw or "").strip() or "—"
    if amount_tiyin == 0:
        return "0"
    whole = amount_tiyin // 100
    grouped = f"{whole:,}".replace(",", " ")
    suffix = {"UZS": "сўм", "USD": "$", "EUR": "€"}.get(currency, currency)
    return f"{grouped} {suffix}" if currency == "UZS" else f"{suffix}{grouped}"


def _amount_answered(request: dict[str, Any]) -> bool:
    """Whether "Сумма ва валюта" has been answered.

    An answer that isn't a plain number ("тахминан 2 млн", "ҳали аниқ эмас")
    still counts as answered — it's kept verbatim in ``amount_raw`` and shown
    as typed, rather than re-asking the same question forever.
    """
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
    """One field's value as plain text (the .docx uses this directly).

    Not HTML-safe on its own — Telegram cards must go through
    ``summary_lines``/``request_card``, which escape it.
    """
    if field.key == "amount":
        return format_amount(
            request.get("amount_tiyin"), request.get("currency") or "UZS", request.get("amount_raw")
        )
    return (str(request.get(field.key) or "—")).strip()


def summary_lines(request: dict[str, Any]) -> list[str]:
    """The request's fields as HTML-safe "label: value" lines, in SOP order."""
    return [f"<b>{field.label}:</b> {_h(field_value(request, field))}" for field in FIELDS]


def request_card(request: dict[str, Any], *, for_approver: bool) -> str:
    """The request as a Telegram card.

    Args:
        request: A ``permission_requests`` row.
        for_approver: Include who is asking; the requester's own copy
            doesn't need it.

    Returns:
        Telegram HTML, with every typed value escaped.
    """
    number = request.get("request_no") or "—"
    lines = [f"📄 <b>Ёзма рухсат сўрови</b> № {_h(number)}", f"<i>{SOP_CODE}</i>", ""]
    if for_approver:
        role = request.get("requester_role_label") or request.get("requester_role", "—")
        lines.append(f"<b>Ходим:</b> {_h(request.get('requester_name', '—'))} ({_h(role)})")
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


def history_lines(events: Sequence[dict[str, Any]]) -> list[str]:
    """The decision history, oldest first, for the form's audit block."""
    labels = {
        "created": "Сўров бошланди",
        "submitted": "Сўров юборилди",
        "decided": "Қарор қабул қилинди",
        "completed": "Бажарилди",
        "cancelled": "Бекор қилинди",
    }
    lines = []
    for event in events:
        when = event.get("occurred_at")
        stamp = when.strftime("%d.%m.%Y %H:%M") if hasattr(when, "strftime") else str(when or "")
        action = labels.get(event.get("action", ""), event.get("action", ""))
        detail = f" — {event['detail']}" if event.get("detail") else ""
        lines.append(f"{stamp} · {action} · {event.get('actor', '')}{detail}")
    return lines
