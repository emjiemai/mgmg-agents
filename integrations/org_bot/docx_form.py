"""Fills EMJ-SOP-ADM-01's page-2 form as a .docx.

The SOP's own form is what the documents coordinator files (§5), so the
generated file mirrors it field for field, in the same order and the same
Uzbek Cyrillic wording, with the four decision boxes and one of them ticked.

Two blocks exist in the file that the paper form has no room for, and which
§3 requires of an electronic approval: the approver's identity (name, role and
Telegram id, standing in for the signature) and the full decision history.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Sequence

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from integrations.org_bot import permissions


def _stamp(value: Any) -> str:
    """Format a timestamp the way the form's date/time fields expect."""
    return value.strftime("%d.%m.%Y %H:%M") if hasattr(value, "strftime") else (str(value) if value else "—")


def _add_kv_table(document: Document, rows: Sequence[tuple[str, str]]) -> None:
    """Add a two-column label/value table, the paper form's filled-in lines."""
    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value or "—"
        for paragraph in cells[0].paragraphs:
            for run in paragraph.runs:
                run.bold = True
    document.add_paragraph()


def build_form(request: dict[str, Any], events: Sequence[dict[str, Any]]) -> Path:
    """Write one filled request/decision form and return its path.

    Args:
        request: A ``permission_requests`` row.
        events: That request's ``permission_request_events``, oldest first.

    Returns:
        Path to a .docx in a temp directory — the caller sends it and deletes it.
    """
    document = Document()
    style = document.styles["Normal"]
    style.font.size = Pt(10)

    heading = document.add_paragraph("EMJIEM")
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    heading.runs[0].bold = True
    heading.runs[0].font.size = Pt(16)

    title = document.add_paragraph("Ёзма сўров ва қарор шакли")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.runs[0].bold = True
    title.runs[0].font.size = Pt(13)

    subtitle = document.add_paragraph(
        f"{permissions.SOP_CODE} ҳужжатига илова   |   Сўров рақами: {request.get('request_no') or '—'}"
    )
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER

    document.add_paragraph()

    request_rows: list[tuple[str, str]] = [
        ("Сўров рақами", request.get("request_no") or "—"),
        ("Қабул қилинган сана ва вақт", _stamp(request.get("submitted_at"))),
        (
            "Ходимнинг исми ва лавозими",
            f"{request.get('requester_name', '—')} — "
            f"{request.get('requester_role_label') or request.get('requester_role', '—')}",
        ),
        ("Бўлим", request.get("requester_role_label") or request.get("requester_role") or "—"),
        ("Кимга тақдим этилади", request.get("submitted_to") or "—"),
    ]
    request_rows += [
        (field.label, permissions.field_value(request, field)) for field in permissions.FIELDS
    ]
    request_rows.append(
        (
            "Ходим имзоси",
            f"Электрон тасдиқ · Telegram ID {request.get('requester_telegram_user_id', '—')} · "
            f"{_stamp(request.get('submitted_at'))}",
        )
    )
    _add_kv_table(document, request_rows)

    decision_heading = document.add_paragraph("Ваколатли шахснинг қарори")
    decision_heading.runs[0].bold = True

    status = request.get("status") or ""
    boxes = " ".join(
        f"{'☑' if status == key else '☐'} {label}" for key, (_emoji, label) in permissions.DECISIONS.items()
    )
    document.add_paragraph(boxes)

    _add_kv_table(
        document,
        [
            ("Тасдиқланган сумма ва амал қилиш муддати", request.get("approved_terms") or "—"),
            ("Шартлар ёки қарор сабаби", request.get("decision_note") or "—"),
            ("Тасдиқловчи исми ва лавозими", request.get("decided_by") or "—"),
            (
                "Имзо",
                f"Электрон тасдиқ · Telegram ID {request.get('decided_by_telegram_user_id') or '—'}"
                if request.get("decided_by")
                else "—",
            ),
            ("Қарор санаси ва вақти", _stamp(request.get("decided_at"))),
            ("Якун далили", request.get("completion_note") or "—"),
        ],
    )

    history_heading = document.add_paragraph("Қарор тарихи")
    history_heading.runs[0].bold = True
    for line in permissions.history_lines(events) or ["—"]:
        document.add_paragraph(line, style="List Bullet")

    note = document.add_paragraph(
        f"Ушбу шакл {permissions.SOP_CODE} тартибига мувофиқ тизимда автоматик тўлдирилган. "
        "Тасдиқловчи ва қарор тарихи тизимда сақланади (3-банд)."
    )
    note.runs[0].italic = True

    target = Path(tempfile.mkdtemp(prefix="mgmg-perm-")) / f"{request.get('request_no') or 'sorov'}.docx"
    document.save(str(target))
    return target
