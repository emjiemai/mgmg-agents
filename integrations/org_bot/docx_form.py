"""Fills the company's own EMJ-SOP-ADM-01 document — nothing generated.

``templates/EMJ-SOP-ADM-01.docx`` is the business's SOP file, copied into the
project unchanged. Filling it means exactly what a person does with a pen:
replace the underscore blank after each label on the page-2 form with the
answer, and tick one decision box. Layout, fonts, page 1 and every label stay
as they are. A blank with no answer is left blank — nothing is made up.

Only paragraphs after the form's own title are touched, so page 1's director
approval line ("Тасдиқлайман: директор ___ Имзо ___ Сана ___"), which also
contains the word "Имзо", is never filled by mistake.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from docx import Document

from integrations.common.timeutil import to_local
from integrations.org_bot import permissions

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "EMJ-SOP-ADM-01.docx"
FORM_TITLE = "Ёзма сўров ва қарор шакли"

_BLANK = re.compile(r"_{3,}")

# Decision box text on the form, as printed.
_DECISION_BOXES: dict[str, str] = {
    "approved": "Тасдиқланди",
    "approved_conditional": "Шарт билан тасдиқланди",
    "rejected": "Рад этилди",
    "info_needed": "Қўшимча маълумот керак",
}


def _stamp(value: Any) -> str:
    """A stored timestamp as Tashkent date/time, the way the form expects it."""
    if not hasattr(value, "strftime"):
        return ""
    return to_local(value).strftime("%d.%m.%Y %H:%M") if value.tzinfo else value.strftime("%d.%m.%Y %H:%M")


def _one_line(value: Any) -> str:
    """An answer on one line — the form's blanks are single lines."""
    return " ".join(str(value or "").split())


def fill_blank_after(text: str, label: str, value: str) -> str:
    """Replace the underscore blank that follows ``label`` with ``value``.

    Args:
        text: One line of the form.
        label: The printed label, e.g. "Бўлим".
        value: The answer; empty leaves the blank as it is.

    Returns:
        The line with that one blank filled, or unchanged when the label or
        its blank isn't there.
    """
    if not value:
        return text
    start = text.find(label)
    if start < 0:
        return text
    blank = _BLANK.search(text, start + len(label))
    if blank is None:
        return text
    return text[: blank.start()] + value + text[blank.end() :]


def tick_decision(text: str, status: str) -> str:
    """Tick the one box that matches the decision, leaving the others empty."""
    box = _DECISION_BOXES.get(status)
    if not box:
        return text
    return text.replace(f"☐ {box}", f"☑ {box}", 1)


def form_values(request: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (printed label, answer) pair on the page-2 form, in form order.

    Answers are the employee's and approver's own words. The only values not
    typed by a person are facts the system records: the request number, the
    times, and the Telegram account behind each electronic signature.
    """
    position = _one_line(request.get("requester_position"))
    name = _one_line(request.get("requester_name"))
    signed_by_requester = (
        f"электрон, Telegram ID {request['requester_telegram_user_id']}"
        if request.get("submitted_at") and request.get("requester_telegram_user_id")
        else ""
    )
    signed_by_approver = (
        f"электрон, Telegram ID {request['decided_by_telegram_user_id']}"
        if request.get("decided_at") and request.get("decided_by_telegram_user_id")
        else ""
    )
    answer = {f.key: _one_line(permissions.field_value(request, f)) for f in permissions.FIELDS}
    answer = {key: ("" if value == "—" else value) for key, value in answer.items()}

    return [
        ("Сўров рақами", _one_line(request.get("request_no"))),
        ("Қабул қилинган сана ва вақт", _stamp(request.get("submitted_at"))),
        ("Ходимнинг исми ва лавозими", ", ".join(part for part in (name, position) if part)),
        ("Бўлим", answer["department"]),
        ("Кимга тақдим этилади", _one_line(request.get("submitted_to"))),
        ("Нимага рухсат сўралади", answer["subject"]),
        ("Сабаб ва таклиф", answer["reason"]),
        ("Сумма ва валюта", answer["amount"]),
        ("Бажариш муддати", answer["execute_by"]),
        ("Қарор керак бўлган сана ва вақт", answer["decision_needed_by"]),
        ("Шошилинчлик сабаби ёки «оддий»", answer["urgency"]),
        ("Иловалар", answer["attachments"]),
        ("Ходим имзоси", signed_by_requester),
        ("Тасдиқланган сумма ва амал қилиш муддати", _one_line(request.get("approved_terms"))),
        ("Шартлар ёки қарор сабаби", _one_line(request.get("decision_note"))),
        ("Тасдиқловчи исми ва лавозими", _one_line(request.get("decided_by"))),
        ("Имзо", signed_by_approver),
        ("Қарор санаси ва вақти", _stamp(request.get("decided_at"))),
    ]


def fill_form(request: dict[str, Any]) -> Path:
    """Fill the company's SOP document for one request and return its path.

    Args:
        request: A decided ``permission_requests`` row.

    Returns:
        Path to the filled .docx in a temp directory — the caller sends it
        and may delete it.
    """
    document = Document(str(TEMPLATE_PATH))
    values = form_values(request)
    status = request.get("status") or ""

    in_form = False
    for paragraph in document.paragraphs:
        text = paragraph.text
        if not in_form:
            in_form = FORM_TITLE in text
            continue

        filled = text
        for label, value in values:
            filled = fill_blank_after(filled, label, value)
        filled = tick_decision(filled, status)
        if filled == text:
            continue

        # Every line on the form is a single run in the source document; keep
        # that run (and so its font and size) and only swap its text.
        runs = paragraph.runs
        runs[0].text = filled
        for extra in runs[1:]:
            extra.text = ""

    target = Path(tempfile.mkdtemp(prefix="mgmg-perm-")) / f"{request.get('request_no') or 'sorov'}.docx"
    document.save(str(target))
    return target
