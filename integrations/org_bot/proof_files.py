"""A task proof's file, read for the AI checker (2026-11-18, the owner: "it wasn't 4 characters — it had tables and
everything that was done").

A proof sent as a document with no caption was stored as the word «файл», and the AI checker — which never read
employees' files — could only say «unsure». The owner allowed this exception (18.11): the file of a TASK PROOF is
read, only to judge that proof. Nothing else an employee sends is read; nothing read is stored or logged — the
extracted text goes to the AI once and is dropped.

  * photo, or an image sent as a file → the picture itself (the AI's image reading, as the Director's cheques);
  * PDF → its text (pypdfium2); a scanned PDF with no text → its first page as a picture;
  * Word (.docx) → paragraphs and table rows; Excel (.xlsx) → each sheet's rows; .txt / .csv → the text;
  * anything else (video, voice, archives) → not readable here: the checker stays unsure.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.telegram.bot import TelegramBot

log = setup_logging("proof-files")

MAX_BYTES = 15_000_000
TEXT_MAX = 12_000
PDF_PAGES = 20
SHEET_ROWS = 300
IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FILE_KINDS = ("photo", "document", "video", "voice", "audio", "animation", "video_note")


@dataclass
class ProofFile:
    file_id: str
    kind: str          # photo | document | video | voice | …
    mime: str = ""
    name: str = ""


@dataclass
class Reading:
    """What the AI may see of the file: text, or one picture (bytes + mime), or neither (unreadable)."""
    text: str = ""
    image: bytes | None = None
    image_mime: str = "image/jpeg"
    note: str = ""     # «PDF, 3 бет», «Excel: 2 варақ» — for the AI's facts


def file_of(message: dict[str, Any]) -> ProofFile | None:
    """The file in an employee's proof message: a photo's largest size, or the document / video / voice."""
    photos = message.get("photo") or []
    if photos and isinstance(photos, list):
        return ProofFile(str(photos[-1].get("file_id") or ""), "photo", "image/jpeg")
    for kind in FILE_KINDS[1:]:
        item = message.get(kind)
        if isinstance(item, dict) and item.get("file_id"):
            return ProofFile(str(item["file_id"]), kind, str(item.get("mime_type") or "").lower(),
                             str(item.get("file_name") or ""))
    return None


def _kind_of(f: ProofFile) -> str:
    name, mime = f.name.lower(), f.mime
    if f.kind == "photo" or mime in IMAGE_MIMES or name.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "image"
    if mime == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if mime == DOCX or name.endswith(".docx"):
        return "docx"
    if mime == XLSX or name.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    if mime.startswith("text/") or name.endswith((".txt", ".csv")):
        return "text"
    return "other"


def readable(f: ProofFile | None) -> bool:
    return f is not None and f.kind in ("photo", "document") and _kind_of(f) != "other"


def _cut(text: str) -> str:
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX] + f"\n[… яна {len(text) - TEXT_MAX} белги кесилди]"


def _pdf(data: bytes) -> Reading:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    pages = len(pdf)
    parts = []
    for i in range(min(pages, PDF_PAGES)):
        page = pdf[i]
        parts.append(page.get_textpage().get_text_range())
    text = _cut("\n".join(parts))
    if len(text.strip()) >= 40:
        return Reading(text=text, note=f"PDF, {pages} бет")
    # a scan: the first page as a picture
    picture = pdf[0].render(scale=1.6).to_pil()
    buf = io.BytesIO()
    picture.convert("RGB").save(buf, "JPEG", quality=85)
    return Reading(image=buf.getvalue(), note=f"PDF (скан), {pages} бет — биринчи бети расм")


def _docx(data: bytes) -> Reading:
    import docx

    d = docx.Document(io.BytesIO(data))
    lines = [p.text for p in d.paragraphs if p.text.strip()]
    for t, table in enumerate(d.tables, start=1):
        lines.append(f"[жадвал {t}]")
        for row in table.rows[:SHEET_ROWS]:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return Reading(text=_cut("\n".join(lines)), note=f"Word, {len(d.tables)} жадвал")


def _xlsx(data: bytes) -> Reading:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"[варақ: {ws.title}]")
        for n, row in enumerate(ws.iter_rows(values_only=True)):
            if n >= SHEET_ROWS:
                lines.append(f"[… {ws.title}: қолган қаторлар кесилди]")
                break
            cells = ["" if v is None else str(v) for v in row]
            if any(cells):
                lines.append(" | ".join(cells).rstrip(" |"))
    count = len(wb.worksheets)
    wb.close()
    return Reading(text=_cut("\n".join(lines)), note=f"Excel, {count} варақ")


def extract(f: ProofFile, data: bytes) -> Reading:
    """The file as the AI may see it (never raises: an unreadable file is an empty Reading)."""
    kind = _kind_of(f)
    try:
        if kind == "image":
            return Reading(image=data, image_mime=f.mime if f.mime in IMAGE_MIMES else "image/jpeg", note="расм")
        if kind == "pdf":
            return _pdf(data)
        if kind == "docx":
            return _docx(data)
        if kind == "xlsx":
            return _xlsx(data)
        if kind == "text":
            return Reading(text=_cut(data.decode("utf-8", "replace")), note="матн файли")
    except Exception as exc:  # noqa: BLE001 — a broken file is «unsure», not a crash
        log.warning("Proof file not read: {}", type(exc).__name__)
    return Reading()


def _bot(run_id: Any) -> TelegramBot:
    return TelegramBot(agent="ai-checker", run_id=run_id,
                       bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value())


async def download(f: ProofFile, run_id: Any = None) -> bytes:
    async with _bot(run_id) as bot:
        return await bot.download_file(f.file_id, max_bytes=MAX_BYTES)


async def recover(chat_id: int, message_id: int, run_id: Any = None) -> ProofFile | None:
    """A proof whose file id wasn't kept (before 18.11): the message forwarded silently within the same chat — the
    forward carries the file id — and the forward deleted at once."""
    async with _bot(run_id) as bot:
        forwarded = await bot._call("forwardMessage", {  # noqa: SLF001 — the Bot API primitive
            "chat_id": str(chat_id), "from_chat_id": str(chat_id), "message_id": int(message_id),
            "disable_notification": True}, mode="notify", target_ref=str(chat_id))
        if not forwarded:
            return None
        try:
            await bot._call("deleteMessage", {"chat_id": str(chat_id),  # noqa: SLF001
                                              "message_id": forwarded["message_id"]}, mode="write", target_ref=str(chat_id))
        except Exception as exc:  # noqa: BLE001 — the copy stays in their own chat; the reading goes on
            log.warning("Forwarded proof copy not deleted: {}", type(exc).__name__)
        return file_of(forwarded)
