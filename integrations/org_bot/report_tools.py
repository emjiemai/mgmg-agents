"""Employees' files, and changing today's report (2026-10-02).

From the owner:
- a photo, video or file an employee sends is asked about — "what is it
  for?" — and, as today's report or "for the Director", forwarded to the
  Director as it is. The AI never reads it: it wouldn't understand most of
  it, and nobody wants it described frame by frame. Text still goes to the
  AI, never to the Director (the bot is for work, not messages);
- a report is never guessed: a typed one is confirmed first ("бу бугунги
  ҳисоботингизми?"), and only today's report counts;
- things change, so today's report can be changed or deleted: /hisobot
  shows it with ✏️ / 🗑 (until midnight; a deleted report is owed again).

Pure logic here (tested offline); ``ops_manager`` holds the files, edits the
reports and forwards.
"""

from __future__ import annotations

from typing import Any

from integrations.org_bot.tone import casual
from integrations.telegram.bot import escape

REPORT_COMMANDS = ("/hisobot", "/ҳисобот", "/report")
EDIT_MINUTES = 30  # after ✏️, the next message within this is the new text
CAPTION_MAX = 1000  # Telegram allows 1024

KINDS = {
    "photo": "расм", "video": "видео", "document": "файл", "audio": "аудио", "voice": "овозли хабар",
    "animation": "анимация",
}
PURPOSES = {"r": "report", "d": "director", "x": "cancelled"}


def file_kind(message: dict[str, Any]) -> str | None:
    """Which kind of file this message carries, if any."""
    return next((kind for kind in KINDS if message.get(kind)), None)


def purpose_keyboard(file_id: str, report_today: bool) -> dict[str, Any]:
    """What the file is for: today's report (only when one is asked today), the Director, or nothing."""
    first = []
    if report_today:
        first.append({"text": "бугунги ҳисобот", "callback_data": f"fp:{file_id}:r"})
    first.append({"text": "директорга юбориш", "callback_data": f"fp:{file_id}:d"})
    return {"inline_keyboard": [first, [{"text": "бекор қилиш", "callback_data": f"fp:{file_id}:x"}]]}


def ask_purpose_text(kind: str) -> str:
    return casual(f"бу {KINDS.get(kind, 'файл')} нима учун?", "🙂")


def director_caption(name: str, role_label: str, purpose: str, caption: str | None) -> str:
    """The caption the Director sees on a forwarded file."""
    what = "бугунги ҳисоботи" if purpose == "report" else "сизга юборди"
    lines = [f"📎 <b>{escape(name)}</b> ({escape(role_label)}) — {what}"]
    if (caption or "").strip():
        lines.append(escape(caption.strip()))
    return "\n".join(lines)[:CAPTION_MAX]


def sent_text(purpose: str, delivered: bool) -> str:
    if not delivered:
        return casual("директорга етказиб бўлмади, бир оздан кейин қайта юборинг", "🙏")
    if purpose == "report":
        return casual("раҳмат каттакон, ҳисоботингиз қабул қилинди ва директорга юборилди, чарчаманг", "😊")
    return casual("директорга юборилди, раҳмат", "🙂")


def cancelled_text() -> str:
    return casual("бекор қилинди, ҳеч кимга юборилмади", "🙂")


def no_report_today_text() -> str:
    return casual("бугун ҳисобот ҳали сўралмаган, ҳисобот соат 16:00 дан кейин қабул қилинади", "🙂")


def reports_off_text() -> str:
    """/hisobot for someone whose daily reports the admin switched off."""
    return casual("сиздан кунлик ҳисобот сўралмайди, ёзишингиз шарт эмас", "🙂")


def report_card(report: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """/hisobot: today's report as it stands, with ✏️ / 🗑 once it's sent."""
    if report.get("status") != "submitted":
        return casual("бугунги ҳисоботингиз ҳали юборилмаган, ёзиб юборинг", "🙂"), None
    files = f"\n📎 файллар: {report['media_count']}" if report.get("media_count") else ""
    text = f"📝 <b>Бугунги ҳисоботингиз</b>\n\n{escape(report.get('content') or '')}{files}"
    keyboard = {"inline_keyboard": [[
        {"text": "✏️ ўзгартириш", "callback_data": f"rpe:{report['id']}"},
        {"text": "🗑 ўчириш", "callback_data": f"rpd:{report['id']}"},
    ]]}
    return text, keyboard


def delete_confirm_keyboard(report_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "ҳа, ўчириш", "callback_data": f"rpdy:{report_id}"},
        {"text": "йўқ", "callback_data": f"rpn:{report_id}"},
    ]]}


def edit_prompt_text() -> str:
    return casual("ҳисоботингизнинг янги матнини ёзиб юборинг", "✏️")


def edited_text() -> str:
    return casual("раҳмат, ҳисоботингиз янгиланди", "😊")


def delete_question_text() -> str:
    return casual("бугунги ҳисоботингиз ўчирилсинми?", "🤔")


def deleted_text() -> str:
    return casual("ҳисоботингиз ўчирилди, ярим тунгача қайта ёзиб юборишингиз мумкин", "🙂")


def too_late_text() -> str:
    return casual("фақат бугунги ҳисоботни ўзгартириш мумкин", "🙂")
