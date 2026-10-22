"""Files sent as the daily report become report text (2026-10-22, the owner).

"Pictures, audios and videos — we need to summarize them and consider them as hisobot":

  * a **voice message or audio** — speech to text: the report says what was said;
  * a **video** (or a round video message) — the AI watches it and writes in two or
    three sentences what it shows of the work and what is said in it;
  * a **photo or a document** — kept as it is (it may hold anything); the report
    says "[расм]" / "[файл]" with the caption, and the file is sent from the
    database when someone asks for that report.

Only files the employee marked «бугунги ҳисобот» are read — a file for the Director
still goes to him as it is, unread. Report files are no longer pushed to the
Director at once: they wait with the report, and whoever asks for that person's
report (the Director; the Garmin manager for his team) gets the row and its files
(``analyst.employee_report``). Nothing is guessed: what the AI can't make out is
said as such. Counts-only logs — never the text.
"""

from __future__ import annotations

import uuid
from typing import Any

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging

AGENT = "report-media"
log = setup_logging(AGENT)

LISTEN_KINDS = ("voice", "audio")
WATCH_KINDS = ("video", "video_note")
# Telegram lets a bot download files up to 20 MB; a video is sent to the AI inline (base64), so a little less.
MAX_AUDIO_BYTES = 20_000_000
MAX_VIDEO_BYTES = 18_000_000
LABELS = {"voice": "овозли хабар", "audio": "аудио", "video": "видео", "video_note": "видео хабар",
          "photo": "расм", "document": "файл", "animation": "анимация"}

LISTEN_PROMPT = """You transcribe an employee's voice message — their daily work report at MGMG \
(Tashkent). The speech is Uzbek or Russian, often mixed. Write down what was said, in the \
language spoken; Uzbek in Cyrillic script. Keep numbers, names of customers, places and \
products exactly. Remove only filler ("ээ", "мм"). Add nothing, explain nothing. If nothing \
can be made out, reply exactly: -"""

WATCH_PROMPT = """An employee sent this video as their daily work report at MGMG (Tashkent: \
Garmin watch shops, Primus laundry equipment, Londry self-service laundries). In Uzbek, \
Cyrillic script, write at most three short sentences: what the video shows of the work \
(place, what was done, equipment or goods, quantities you can see) and what is said in it. \
Only what is visible or audible — no guesses, no judging the person, no faces described. \
If nothing about work can be made out, reply exactly: -"""


def file_of(message: dict[str, Any]) -> dict[str, Any] | None:
    """The Telegram file a message carries: kind, file_id, MIME type, size — the largest photo size."""
    for kind in LABELS:
        value = message.get(kind)
        if not value:
            continue
        item = value[-1] if isinstance(value, list) else value
        if not isinstance(item, dict) or not item.get("file_id"):
            return None
        mime = item.get("mime_type") or {"photo": "image/jpeg", "voice": "audio/ogg", "video_note": "video/mp4"}.get(kind)
        return {"kind": kind, "file_id": item["file_id"], "mime": mime, "size": int(item.get("file_size") or 0)}
    return None


def audio_format(mime: str | None) -> str:
    """OpenRouter's ``input_audio`` format for a MIME type (Telegram voice = OGG)."""
    m = (mime or "").lower()
    for key, fmt in (("ogg", "ogg"), ("opus", "ogg"), ("mpeg", "mp3"), ("mp3", "mp3"), ("wav", "wav"), ("x-wav", "wav"),
                     ("mp4", "m4a"), ("m4a", "m4a"), ("aac", "aac"), ("flac", "flac"), ("aiff", "aiff")):
        if key in m:
            return fmt
    return "ogg"


def video_mime(mime: str | None) -> str:
    m = (mime or "").lower()
    return m if m in ("video/mp4", "video/mpeg", "video/mov", "video/quicktime", "video/webm") else "video/mp4"


def placeholder(kind: str, caption: str | None) -> str:
    """What the report says for a file before (or instead of) its text: «[расм] caption»."""
    text = f"[{LABELS.get(kind, 'файл')}]"
    return f"{text} {caption.strip()}" if (caption or "").strip() else text


def report_line(kind: str, text: str) -> str:
    """The report's line for a voice / video once it's text: «[овозли хабар] …»; «-» = nothing made out."""
    text = " ".join((text or "").split())
    if not text or text == "-":
        return f"[{LABELS.get(kind, 'файл')}: матнга айлантириб бўлмади]"
    return f"[{LABELS.get(kind, 'файл')}] {text}"


def readable(kind: str, size: int) -> bool:
    """Whether the AI gets to read this file: a voice / audio up to 20 MB, a video up to 18 MB."""
    if kind in LISTEN_KINDS:
        return size <= MAX_AUDIO_BYTES
    if kind in WATCH_KINDS:
        return size <= MAX_VIDEO_BYTES
    return False


async def to_text(kind: str, data: bytes, mime: str | None, run_id: uuid.UUID) -> str:
    """Voice → what was said; video → what it shows and says. Raises OpenRouterError when the AI can't."""
    from integrations.ai.openrouter_client import OpenRouterClient

    async with OpenRouterClient(agent=AGENT, run_id=run_id, model_override=settings.ops_manager_bot_model,
                                fallback_override=settings.ops_manager_bot_fallback_models, timeout=180.0) as ai:
        if kind in LISTEN_KINDS:
            return await ai.media_text(LISTEN_PROMPT, "Transcribe this report.", data, "audio", audio_format(mime))
        return await ai.media_text(WATCH_PROMPT, "Describe this work report video.", data, "video", video_mime(mime))


def done_text(kind: str, line: str) -> str:
    """Told to the employee once their voice / video is text — so a wrong word can be fixed with /hisobot."""
    from integrations.org_bot.tone import casual
    from integrations.telegram.bot import escape

    return (casual(f"{LABELS.get(kind, 'файл')} ҳисоботингизга матн бўлиб қўшилди, хато бўлса /hisobot орқали тузатинг")
            + f"\n\n<i>{escape(line[:1500])}</i>")


def media_caption(who: str | None, at: Any, text: str | None) -> str:
    """«Абдурашид Хожиев · 22.10 18:05» and, for a voice / video, its text — under the file sent on request."""
    from integrations.common.timeutil import to_local
    from integrations.telegram.bot import escape

    head = " · ".join(x for x in (who or "", f"{to_local(at):%d.%m %H:%M}" if at else "") if x)
    body = escape(" ".join((text or "").split())[:800])
    return f"<b>{escape(head)}</b>" + (f"\n{body}" if body else "")
