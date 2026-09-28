"""The page a client sees after scanning the company's feedback QR code — ``/f``.

One screen, no JavaScript, a few kilobytes: a choice between an opinion and
a complaint, a text box, and an optional name and phone. Empty name and
phone = anonymous. The form posts back here; the message is stored and sent
to the Director by ``integrations/org_bot/feedback.py``.

Four languages (2026-09-28): Uzbek Cyrillic (the default), Uzbek Latin,
Russian and English. The page opens in the phone's language when it is one
of these — ``?lang=`` (the switcher links) wins over the browser's
Accept-Language header. A phone set to plain "uz" gets Latin: that is how
phones write Uzbek (the CLDR default script). The choice rides along in a
hidden field, so errors and the thank-you page stay in the same language.

Public, so it defends itself:
  * a hidden "website" field that people never fill and bots do — such a
    post gets the thank-you page and is dropped;
  * at most 5 messages per 10 minutes from one address (kept in memory only;
    no IP address is ever stored or sent anywhere);
  * length limits, and everything shown back or sent on is escaped.
"""

from __future__ import annotations

import html
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from integrations.common.config import settings
from integrations.common.logging_setup import setup_logging
from integrations.org_bot import feedback
from integrations.org_bot.feedback import DEFAULT_LANG

log = setup_logging("feedback-page")
router = APIRouter(prefix="/f", include_in_schema=False)

RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 600
_recent: dict[str, deque[float]] = defaultdict(deque)

_HEADERS = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    "Vary": "Accept-Language",
}

# Each language written out by its own speakers' conventions; {n} is a number.
TEXTS: dict[str, dict[str, str]] = {
    "uz_cyrl": {
        "html_lang": "uz-Cyrl",
        "switch": "Ўзбекча",
        "title": "Фикр ва шикоятлар",
        "intro": "Ҳар бир хабарни раҳбарият ўқийди",
        "feedback": "Фикр",
        "complaint": "Шикоят",
        "message": "Хабарингиз",
        "placeholder": "Фикрингизни шу ерга ёзинг…",
        "name": "Исмингиз (ихтиёрий)",
        "phone": "Телефон рақамингиз (ихтиёрий)",
        "note": "Исм ва телефонни ёзмасангиз — хабар аноним юборилади.",
        "send": "Юбориш",
        "thanks": "Раҳмат!",
        "sent": "Хабарингиз раҳбариятга юборилди.",
        "contact": "Керак бўлса, сиз билан боғланамиз.",
        "again": "Яна ёзиш",
        "err_empty": "Фикрингизни ёзинг.",
        "err_too_long": "Хабар жуда узун — {n} белгигача ёзинг.",
        "err_phone": "Телефон рақами нотўғри. Масалан: +998 90 123 45 67 — ёки бўш қолдиринг.",
        "err_rate": "Кўп хабар юборилди. Бир оздан кейин қайта уриниб кўринг.",
        "err_failed": "Хатолик юз берди. Бир оздан кейин қайта юборинг.",
    },
    "uz_latn": {
        "html_lang": "uz-Latn",
        "switch": "Oʻzbekcha",
        "title": "Fikr va shikoyatlar",
        "intro": "Har bir xabarni rahbariyat oʻqiydi",
        "feedback": "Fikr",
        "complaint": "Shikoyat",
        "message": "Xabaringiz",
        "placeholder": "Fikringizni shu yerga yozing…",
        "name": "Ismingiz (ixtiyoriy)",
        "phone": "Telefon raqamingiz (ixtiyoriy)",
        "note": "Ism va telefonni yozmasangiz — xabar anonim yuboriladi.",
        "send": "Yuborish",
        "thanks": "Rahmat!",
        "sent": "Xabaringiz rahbariyatga yuborildi.",
        "contact": "Kerak boʻlsa, siz bilan bogʻlanamiz.",
        "again": "Yana yozish",
        "err_empty": "Fikringizni yozing.",
        "err_too_long": "Xabar juda uzun — {n} belgigacha yozing.",
        "err_phone": "Telefon raqami notoʻgʻri. Masalan: +998 90 123 45 67 — yoki boʻsh qoldiring.",
        "err_rate": "Koʻp xabar yuborildi. Birozdan keyin qayta urinib koʻring.",
        "err_failed": "Xatolik yuz berdi. Birozdan keyin qayta yuboring.",
    },
    "ru": {
        "html_lang": "ru",
        "switch": "Русский",
        "title": "Отзывы и жалобы",
        "intro": "Каждое сообщение читает руководство",
        "feedback": "Отзыв",
        "complaint": "Жалоба",
        "message": "Ваше сообщение",
        "placeholder": "Напишите ваш отзыв здесь…",
        "name": "Ваше имя (необязательно)",
        "phone": "Ваш телефон (необязательно)",
        "note": "Если не указать имя и телефон — сообщение будет анонимным.",
        "send": "Отправить",
        "thanks": "Спасибо!",
        "sent": "Ваше сообщение отправлено руководству.",
        "contact": "При необходимости мы с вами свяжемся.",
        "again": "Написать ещё",
        "err_empty": "Напишите ваше сообщение.",
        "err_too_long": "Сообщение слишком длинное — не больше {n} символов.",
        "err_phone": "Неверный номер телефона. Например: +998 90 123 45 67 — или оставьте поле пустым.",
        "err_rate": "Слишком много сообщений. Попробуйте чуть позже.",
        "err_failed": "Произошла ошибка. Попробуйте отправить ещё раз чуть позже.",
    },
    "en": {
        "html_lang": "en",
        "switch": "English",
        "title": "Feedback and complaints",
        "intro": "Every message is read by management",
        "feedback": "Feedback",
        "complaint": "Complaint",
        "message": "Your message",
        "placeholder": "Write your feedback here…",
        "name": "Your name (optional)",
        "phone": "Your phone number (optional)",
        "note": "Leave the name and phone empty to send it anonymously.",
        "send": "Send",
        "thanks": "Thank you!",
        "sent": "Your message has been sent to management.",
        "contact": "We will contact you if needed.",
        "again": "Write another",
        "err_empty": "Please write your message.",
        "err_too_long": "The message is too long — up to {n} characters.",
        "err_phone": "Invalid phone number. Example: +998 90 123 45 67 — or leave it empty.",
        "err_rate": "Too many messages. Please try again a little later.",
        "err_failed": "Something went wrong. Please try again shortly.",
    },
}

_CSS = """
:root{--red:#d71920;--fg:#1d1d1f;--muted:#6e6e73;--line:#d9d9de;--bg:#f5f5f7;--card:#fff}
@media (prefers-color-scheme:dark){:root{--fg:#ececec;--muted:#a1a1a6;--line:#3a3a3c;--bg:#111;--card:#1c1c1e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:var(--red);color:#fff;padding:12px 16px 26px;text-align:center}
header h1{margin:0;font-size:21px;letter-spacing:.3px}
header p{margin:6px 0 0;font-size:14px;opacity:.9}
nav{display:flex;flex-wrap:wrap;justify-content:center;gap:4px 14px;font-size:13px;margin-bottom:14px}
nav a{color:#fff;opacity:.8;text-decoration:none;padding:4px 0}
nav b{border-bottom:2px solid #fff;padding:4px 0}
main{max-width:520px;margin:-14px auto 32px;padding:0 16px}
form,.done{background:var(--card);border-radius:14px;padding:18px 16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.kinds{display:flex;gap:8px;margin-bottom:14px}
.kinds label{flex:1;border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center;font-size:15px}
.kinds input{margin-right:6px;accent-color:var(--red)}
label.f{display:block;font-size:14px;color:var(--muted);margin:12px 0 5px}
textarea,input[type=text],input[type=tel]{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;
 font:inherit;background:var(--card);color:var(--fg)}
textarea{min-height:130px;resize:vertical}
.note{font-size:13px;color:var(--muted);margin:10px 0 0}
.err{background:#fdecec;color:#a4161a;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:14px}
button{width:100%;margin-top:16px;padding:13px;border:0;border-radius:10px;background:var(--red);color:#fff;
 font:600 16px system-ui,sans-serif}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.done{text-align:center}.done h2{margin:6px 0 8px;font-size:20px}.done a{color:var(--red)}
"""


def _e(value: str) -> str:
    return html.escape(value or "", quote=True)


def lang_from(requested: str | None, accept_language: str | None) -> str:
    """The page's language: an explicit choice, else the phone's, else Uzbek Cyrillic.

    Args:
        requested: ``?lang=`` or the form's hidden field.
        accept_language: The browser's Accept-Language header.
    """
    if requested in TEXTS:
        return requested
    ranked = []
    for position, part in enumerate((accept_language or "").split(",")):
        tag, _, params = part.strip().lower().partition(";")
        quality = 1.0
        if params.strip().startswith("q="):
            try:
                quality = float(params.strip()[2:])
            except ValueError:
                quality = 0.0
        ranked.append((-quality, position, tag.replace("_", "-")))
    for _, _, tag in sorted(ranked):
        if tag.startswith("uz"):
            return "uz_cyrl" if "cyrl" in tag else "uz_latn"
        if tag.split("-")[0] in ("ru", "en"):
            return tag.split("-")[0]
    return DEFAULT_LANG


def _page(body: str, lang: str, status: int = 200) -> HTMLResponse:
    t = TEXTS[lang]
    switcher = " ".join(
        f"<b>{_e(TEXTS[code]['switch'])}</b>" if code == lang else f"<a href='/f?lang={code}'>{_e(TEXTS[code]['switch'])}</a>"
        for code in TEXTS
    )
    return HTMLResponse(
        f"<!doctype html><html lang='{t['html_lang']}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_e(t['title'])}</title><style>{_CSS}</style></head><body>"
        f"<header><nav>{switcher}</nav><h1>{_e(t['title'])}</h1><p>{_e(t['intro'])}</p></header>"
        f"<main>{body}</main></body></html>",
        status_code=status,
        headers={**_HEADERS, "Content-Language": t["html_lang"]},
    )


def error_text(key: str, lang: str) -> str:
    """An error key from ``feedback.clean`` (or "rate"/"failed") in the page's language."""
    return TEXTS[lang][f"err_{key}"].format(n=feedback.MESSAGE_MAX)


def form_html(values: dict[str, str] | None = None, error: str | None = None, lang: str = DEFAULT_LANG) -> str:
    """The form, keeping what was typed when something needs fixing."""
    t = TEXTS[lang]
    values = values or {}
    kind = values.get("kind", "feedback")
    checked = {k: " checked" if kind == k else "" for k in feedback.KINDS}
    return (
        "<form method='post' action='/f'>"
        + (f"<div class='err'>{_e(error)}</div>" if error else "")
        + f"<input type='hidden' name='lang' value='{lang}'>"
        "<div class='kinds'>"
        f"<label><input type='radio' name='kind' value='feedback'{checked['feedback']}>💬 {_e(t['feedback'])}</label>"
        f"<label><input type='radio' name='kind' value='complaint'{checked['complaint']}>⚠️ {_e(t['complaint'])}</label>"
        "</div>"
        f"<label class='f' for='m'>{_e(t['message'])}</label>"
        f"<textarea id='m' name='message' maxlength='{feedback.MESSAGE_MAX}' required "
        f"placeholder='{_e(t['placeholder'])}'>{_e(values.get('message', ''))}</textarea>"
        f"<label class='f' for='n'>{_e(t['name'])}</label>"
        f"<input id='n' type='text' name='name' maxlength='{feedback.NAME_MAX}' value='{_e(values.get('name', ''))}'>"
        f"<label class='f' for='p'>{_e(t['phone'])}</label>"
        f"<input id='p' type='tel' name='phone' inputmode='tel' maxlength='25' placeholder='+998 90 123 45 67' "
        f"value='{_e(values.get('phone', ''))}'>"
        "<div class='hp' aria-hidden='true'><input type='text' name='website' tabindex='-1' autocomplete='off'></div>"
        f"<p class='note'>{_e(t['note'])}</p>"
        f"<button type='submit'>{_e(t['send'])}</button></form>"
    )


def thanks_html(contact_given: bool, lang: str = DEFAULT_LANG) -> str:
    t = TEXTS[lang]
    follow_up = f"<p>{_e(t['contact'])}</p>" if contact_given else ""
    return (
        f"<div class='done'><h2>✅ {_e(t['thanks'])}</h2><p>{_e(t['sent'])}</p>"
        f"{follow_up}<p><a href='/f?lang={lang}'>{_e(t['again'])}</a></p></div>"
    )


def _client_address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def _allowed(address: str) -> bool:
    """At most RATE_LIMIT posts per window from one address (memory only)."""
    now = time.monotonic()
    times = _recent[address]
    while times and now - times[0] > RATE_WINDOW_SECONDS:
        times.popleft()
    if len(times) >= RATE_LIMIT:
        return False
    times.append(now)
    return True


@router.get("", response_class=HTMLResponse)
async def feedback_form(request: Request, lang: str | None = None) -> Response:
    """The form behind the company's QR code."""
    if not settings.feedback_enabled:
        return Response(status_code=404)
    chosen = lang_from(lang, request.headers.get("accept-language"))
    return _page(form_html(lang=chosen), chosen)


@router.get("/{old}", include_in_schema=False)
async def old_place_link(old: str) -> Response:
    """Early test codes carried a place ("/f/garmin"); send them to the one form."""
    return RedirectResponse("/f", status_code=301)


@router.post("", response_class=HTMLResponse)
async def submit(request: Request) -> Response:
    """Take one submission: validate, store, send to the Director, say thanks."""
    if not settings.feedback_enabled:
        return Response(status_code=404)

    body = (await request.body())[:20_000].decode("utf-8", errors="replace")
    form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
    lang = lang_from(form.get("lang"), request.headers.get("accept-language"))

    if form.get("website"):  # the hidden field: only bots fill it
        log.info("Feedback honeypot hit — dropped")
        return _page(thanks_html(contact_given=False, lang=lang), lang)

    submission, error = feedback.clean({**form, "lang": lang})
    if error:
        return _page(form_html(form, error_text(error, lang), lang), lang, status=400)

    if not _allowed(_client_address(request)):
        return _page(form_html(form, error_text("rate", lang), lang), lang, status=429)

    try:
        await feedback.submit(submission)
    except Exception as exc:  # noqa: BLE001 — the client must see a clear page, not a stack trace
        log.error("Could not store client feedback: {}", exc)
        return _page(form_html(form, error_text("failed", lang), lang), lang, status=503)
    return _page(thanks_html(contact_given=not submission.anonymous, lang=lang), lang)
