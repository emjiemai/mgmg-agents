"""The page a client sees after scanning the company's QR code — ``/f``.

The company's complaints channel (2026-09-28: complaints only, no
"opinion" choice). One screen, no JavaScript, a few kilobytes: what
happened, and an optional name and phone. Empty name and phone = anonymous.
The form posts back to its own address; the complaint is stored and sent to
the Director by ``integrations/org_bot/feedback.py``.

Two pages, one per business, each behind its own printed QR code — the
address decides the business, the client never chooses:

    /f          Londry — the address on the first printed card; keep it
    /f/garmin   Garmin

Three languages: Uzbek Cyrillic (the default), Russian and English (Uzbek
Latin dropped 2026-09-28). The page opens in the phone's language when it
is one of these — ``?lang=`` (the switcher links) wins over the browser's
Accept-Language header, and any Uzbek phone gets Cyrillic. The choice rides
along in a hidden field, so errors and the thank-you page stay in the same
language.

Read on a phone, often by someone who is already annoyed: large type
(18 px body), large tap targets, errors next to the field they are about,
and nothing but the form on the page.

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
from integrations.org_bot.feedback import DEFAULT_LANG, PLACES

# Each business's page. "/f" is printed on Londry's cards and can't move.
PAGES: dict[str, str] = {"laundry": "/f", "garmin": "/f/garmin"}
assert set(PAGES) == set(PLACES)

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
        "title": "Шикоят қолдириш",
        "intro": "Ҳар бир шикоятни раҳбарият ўқийди.",
        "message": "Нима бўлди?",
        "placeholder": "Қачон, қаерда ва нима бўлганини ёзинг",
        "contact": "Сиз билан боғланайликми?",
        "contact_hint": "Ихтиёрий. Исм ва телефонни ёзмасангиз, шикоят аноним юборилади.",
        "name": "Исмингиз",
        "phone": "Телефон рақамингиз",
        "send": "Шикоятни юбориш",
        "thanks": "Раҳмат!",
        "sent": "Шикоятингиз раҳбариятга юборилди.",
        "follow_up": "Керак бўлса, сиз билан боғланамиз.",
        "again": "Яна шикоят ёзиш",
        "err_empty": "Нима бўлганини ёзинг.",
        "err_too_long": "Шикоят жуда узун — {n} белгигача ёзинг.",
        "err_phone": "Телефон рақами нотўғри. Масалан: +998 90 123 45 67 — ёки бўш қолдиринг.",
        "err_rate": "Кўп шикоят юборилди. Бир оздан кейин қайта уриниб кўринг.",
        "err_failed": "Хатолик юз берди, шикоят юборилмади. Бир оздан кейин қайта юборинг.",
    },
    "ru": {
        "html_lang": "ru",
        "switch": "Русский",
        "title": "Оставить жалобу",
        "intro": "Каждую жалобу читает руководство.",
        "message": "Что случилось?",
        "placeholder": "Напишите, когда, где и что произошло",
        "contact": "Связаться с вами?",
        "contact_hint": "Необязательно. Без имени и телефона жалоба будет анонимной.",
        "name": "Ваше имя",
        "phone": "Ваш телефон",
        "send": "Отправить жалобу",
        "thanks": "Спасибо!",
        "sent": "Ваша жалоба отправлена руководству.",
        "follow_up": "При необходимости мы с вами свяжемся.",
        "again": "Написать ещё одну",
        "err_empty": "Опишите, что случилось.",
        "err_too_long": "Жалоба слишком длинная — не больше {n} символов.",
        "err_phone": "Неверный номер телефона. Например: +998 90 123 45 67 — или оставьте поле пустым.",
        "err_rate": "Слишком много жалоб. Попробуйте чуть позже.",
        "err_failed": "Произошла ошибка, жалоба не отправлена. Попробуйте ещё раз чуть позже.",
    },
    "en": {
        "html_lang": "en",
        "switch": "English",
        "title": "Make a complaint",
        "intro": "Every complaint is read by management.",
        "message": "What happened?",
        "placeholder": "Tell us when, where and what happened",
        "contact": "Should we contact you?",
        "contact_hint": "Optional. Leave both empty to stay anonymous.",
        "name": "Your name",
        "phone": "Your phone number",
        "send": "Send complaint",
        "thanks": "Thank you!",
        "sent": "Your complaint has been sent to management.",
        "follow_up": "We will contact you if needed.",
        "again": "Make another complaint",
        "err_empty": "Please tell us what happened.",
        "err_too_long": "The complaint is too long — up to {n} characters.",
        "err_phone": "Invalid phone number. Example: +998 90 123 45 67 — or leave it empty.",
        "err_rate": "Too many complaints. Please try again a little later.",
        "err_failed": "Something went wrong and the complaint was not sent. Please try again shortly.",
    },
}

# Which field each error belongs next to; the rest go above the button.
_ERROR_FIELD = {"empty": "message", "too_long": "message", "phone": "phone"}

# Drawn icons, one 24-unit grid; colour comes from CSS.
_SVG = "<svg viewBox='0 0 24 24' aria-hidden='true' focusable='false'>{}</svg>"
_ICONS = {
    "alert": _SVG.format("<circle cx='12' cy='12' r='9'/><path d='M12 7.5v5.5M12 16.5h.01'/>"),
    "check": _SVG.format("<path class='tick' d='M5 12.5l4.5 4.5L19 7.5'/>"),
}

_CSS = """
:root{color-scheme:light dark;
 --red:#d71920;--red-press:#b3141a;--head:#d71920;--head-ink:#fff;--head-soft:#fff1f1;
 --bg:#f4f1f0;--surface:#fff;--fg:#1c1718;--muted:#5d5455;--line:#958a8b;--line-strong:#6b6162;
 --tint:#fdecec;--ring:#d71920;--err:#b3141a;--err-bg:#fdecec;--err-line:#f0b9bb;
 --shadow:0 1px 2px rgba(60,20,20,.06),0 8px 24px rgba(60,20,20,.08)}
@media (prefers-color-scheme:dark){:root{
 --red:#cc2129;--red-press:#b01a21;--head:#8f1117;--head-ink:#fff;--head-soft:#f6d3d5;
 --bg:#141011;--surface:#1f1a1b;--fg:#f4eeee;--muted:#b9aeaf;--line:#75696a;--line-strong:#a09495;
 --tint:#3a1a1c;--ring:#ff6b72;--err:#ff8f94;--err-bg:#351719;--err-line:#6b2a2e;
 --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35)}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);caret-color:var(--red);
 font:18px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
::selection{background:var(--red);color:#fff}
a{color:inherit}
:focus-visible{outline:3px solid var(--ring);outline-offset:3px}
header{background:var(--head);color:var(--head-ink);padding:14px 20px 56px}
.wrap{max-width:560px;margin:0 auto}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 28px}
.brand{font-size:19px;font-weight:700;letter-spacing:.01em}
nav{display:flex;gap:4px;width:max-content;padding:4px;border-radius:999px;background:rgba(0,0,0,.2)}
nav a,nav span{display:block;padding:8px 14px;border-radius:999px;font-size:16px;line-height:24px;text-decoration:none;color:var(--head-soft)}
nav span{background:#fff;color:#b3141a;font-weight:600}
nav a:focus-visible{outline-color:#fff;outline-offset:1px}
h1{margin:0;font-size:clamp(30px,8vw,38px);line-height:1.15;letter-spacing:-.01em;font-weight:750;text-wrap:balance}
header p{margin:10px 0 0;font-size:18px;color:var(--head-soft)}
main{padding:0 12px 40px}
.sheet{max-width:560px;margin:-32px auto 0;background:var(--surface);border-radius:20px;box-shadow:var(--shadow);padding:28px 20px 24px}
fieldset{border:0;margin:0;padding:0;min-width:0}
legend,.label{display:block;padding:0;margin:0 0 12px;font-size:20px;line-height:1.3;font-weight:650}
.group+.group{margin-top:32px}
textarea,input[type=text],input[type=tel]{display:block;width:100%;padding:14px 16px;border:1.5px solid var(--line);border-radius:14px;
 background:var(--surface);color:var(--fg);font:inherit;font-size:18px;line-height:1.45;transition:border-color .15s}
textarea{min-height:168px;resize:vertical}
textarea:hover,input[type=text]:hover,input[type=tel]:hover{border-color:var(--line-strong)}
textarea:focus-visible,input[type=text]:focus-visible,input[type=tel]:focus-visible{border-color:var(--red);outline-offset:1px}
::placeholder{color:var(--muted);opacity:1}
.hint{margin:-6px 0 16px;font-size:16px;color:var(--muted)}
.field+.field{margin-top:16px}
.field label{display:block;margin:0 0 6px;font-size:17px;font-weight:550}
textarea[aria-invalid=true],input[aria-invalid=true]{border-color:var(--err)}
.err{display:flex;gap:10px;align-items:flex-start;margin:10px 0 0;color:var(--err);font-size:17px;font-weight:550}
.err svg{flex:none;width:22px;height:22px;margin-top:2px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round}
.err.box{margin:24px 0 0;padding:12px 14px;border:1px solid var(--err-line);border-radius:12px;background:var(--err-bg)}
button{display:block;width:100%;margin-top:28px;min-height:60px;padding:16px;border:0;border-radius:16px;background:var(--red);color:#fff;
 font:inherit;font-size:20px;font-weight:700;cursor:pointer;transition:background-color .15s,transform .1s}
button:hover{background:var(--red-press)}
button:active{transform:scale(.99)}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.done{text-align:center;padding:40px 20px 32px}
.done .mark{display:grid;place-items:center;width:84px;height:84px;margin:0 auto 20px;border-radius:50%;background:var(--tint)}
.done .mark svg{width:52px;height:52px;fill:none;stroke:var(--red);stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}
.tick{stroke-dasharray:24;stroke-dashoffset:0;animation:draw .5s cubic-bezier(.16,1,.3,1) .1s backwards}
@keyframes draw{from{stroke-dashoffset:24}}
@media (prefers-reduced-motion:reduce){.tick{animation:none}*{transition:none!important}}
.done h2{margin:0 0 8px;font-size:30px;line-height:1.2;font-weight:750}
.done p{margin:0 auto;max-width:34ch;font-size:18px;color:var(--muted)}
.done p+p{margin-top:6px}
.again{display:flex;align-items:center;justify-content:center;min-height:56px;margin-top:28px;padding:12px 16px;border:1.5px solid var(--line-strong);
 border-radius:16px;font-size:18px;font-weight:600;text-decoration:none}
.again:hover{border-color:var(--red);color:var(--red)}
@media (min-width:600px){header{padding:18px 24px 64px}.sheet{padding:36px 36px 32px}main{padding:0 24px 56px}}
"""


def _e(value: str) -> str:
    return html.escape(value or "", quote=True)


def _link(place: str, lang: str) -> str:
    """A business's page in a given language."""
    return f"{PAGES[place]}?lang={lang}"


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
            return "uz_cyrl"
        if tag.split("-")[0] in ("ru", "en"):
            return tag.split("-")[0]
    return DEFAULT_LANG


def _page(body: str, lang: str, place: str, status: int = 200) -> HTMLResponse:
    t = TEXTS[lang]
    switcher = "".join(
        f"<span aria-current='true'>{_e(TEXTS[code]['switch'])}</span>" if code == lang
        else f"<a href='{_e(_link(place, code))}' hreflang='{TEXTS[code]['html_lang']}' lang='{TEXTS[code]['html_lang']}'>"
             f"{_e(TEXTS[code]['switch'])}</a>"
        for code in TEXTS
    )
    return HTMLResponse(
        f"<!doctype html><html lang='{t['html_lang']}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='theme-color' content='#d71920'>"
        f"<title>{_e(t['title'])} · {_e(PLACES[place])}</title><style>{_CSS}</style></head><body>"
        f"<header><div class='wrap'><div class='top'><span class='brand' lang='en'>{_e(PLACES[place])}</span>"
        f"<nav>{switcher}</nav></div><h1>{_e(t['title'])}</h1><p>{_e(t['intro'])}</p></div></header>"
        f"<main><div class='sheet'>{body}</div></main></body></html>",
        status_code=status,
        headers={**_HEADERS, "Content-Language": t["html_lang"]},
    )


def error_text(key: str, lang: str) -> str:
    """An error key from ``feedback.clean`` (or "rate"/"failed") in the page's language."""
    return TEXTS[lang][f"err_{key}"].format(n=feedback.MESSAGE_MAX)


def _error(key: str, lang: str, box: bool = False) -> str:
    return (
        f"<p class='err{' box' if box else ''}' id='err' role='alert'>"
        f"{_ICONS['alert']}<span>{_e(error_text(key, lang))}</span></p>"
    )


def form_html(
    values: dict[str, str] | None = None, error: str | None = None, lang: str = DEFAULT_LANG, place: str = "laundry"
) -> str:
    """The form, keeping what was typed when something needs fixing.

    Args:
        values: What the client sent.
        error: An error key; its text is shown next to the field it is about.
        lang: The page's language.
        place: Whose page this is; the form posts back to it.
    """
    t = TEXTS[lang]
    values = values or {}
    field = _ERROR_FIELD.get(error or "", "form" if error else "")

    def err(name: str) -> str:
        return _error(error, lang, box=name == "form") if field == name else ""

    def invalid(name: str) -> str:
        return " aria-invalid='true' aria-describedby='err'" if field == name else ""

    return (
        f"<form method='post' action='{PAGES[place]}' novalidate>"
        f"<input type='hidden' name='lang' value='{lang}'>"
        "<div class='group'>"
        f"<label class='label' for='m'>{_e(t['message'])}</label>"
        f"<textarea id='m' name='message' maxlength='{feedback.MESSAGE_MAX}' required{invalid('message')} "
        f"placeholder='{_e(t['placeholder'])}'>{_e(values.get('message', ''))}</textarea>{err('message')}</div>"
        "<fieldset class='group'>"
        f"<legend>{_e(t['contact'])}</legend><p class='hint'>{_e(t['contact_hint'])}</p>"
        f"<div class='field'><label for='n'>{_e(t['name'])}</label>"
        f"<input id='n' type='text' name='name' autocomplete='name' maxlength='{feedback.NAME_MAX}' "
        f"value='{_e(values.get('name', ''))}'></div>"
        f"<div class='field'><label for='p'>{_e(t['phone'])}</label>"
        f"<input id='p' type='tel' name='phone' inputmode='tel' autocomplete='tel' maxlength='25' "
        f"placeholder='+998 90 123 45 67'{invalid('phone')} value='{_e(values.get('phone', ''))}'>{err('phone')}</div>"
        "</fieldset>"
        "<div class='hp' aria-hidden='true'><input type='text' name='website' tabindex='-1' autocomplete='off'></div>"
        f"{err('form')}<button type='submit'>{_e(t['send'])}</button></form>"
    )


def thanks_html(contact_given: bool, lang: str = DEFAULT_LANG, place: str = "laundry") -> str:
    t = TEXTS[lang]
    follow_up = f"<p>{_e(t['follow_up'])}</p>" if contact_given else ""
    return (
        f"<div class='done' role='status'><div class='mark'>{_ICONS['check']}</div>"
        f"<h2>{_e(t['thanks'])}</h2><p>{_e(t['sent'])}</p>{follow_up}"
        f"<a class='again' href='{_e(_link(place, lang))}'>{_e(t['again'])}</a></div>"
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


def _show_form(request: Request, place: str, lang: str | None) -> Response:
    if not settings.feedback_enabled:
        return Response(status_code=404)
    chosen = lang_from(lang, request.headers.get("accept-language"))
    return _page(form_html(lang=chosen, place=place), chosen, place)


@router.get("", response_class=HTMLResponse)
async def laundry_form(request: Request, lang: str | None = None) -> Response:
    """Londry's page — the address on the printed Londry card."""
    return _show_form(request, "laundry", lang)


@router.get("/garmin", response_class=HTMLResponse)
async def garmin_form(request: Request, lang: str | None = None) -> Response:
    """Garmin's page."""
    return _show_form(request, "garmin", lang)


@router.get("/{other}", include_in_schema=False)
async def other_link(other: str) -> Response:
    """Anything else under /f (early test codes, /f/laundry) goes to Londry's page."""
    return RedirectResponse("/f", status_code=301)


@router.post("", response_class=HTMLResponse)
async def laundry_submit(request: Request) -> Response:
    return await _submit(request, "laundry")


@router.post("/garmin", response_class=HTMLResponse)
async def garmin_submit(request: Request) -> Response:
    return await _submit(request, "garmin")


async def _submit(request: Request, place: str) -> Response:
    """Take one complaint for ``place``: validate, store, send to the Director, say thanks."""
    if not settings.feedback_enabled:
        return Response(status_code=404)

    body = (await request.body())[:20_000].decode("utf-8", errors="replace")
    form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
    lang = lang_from(form.get("lang"), request.headers.get("accept-language"))

    if form.get("website"):  # the hidden field: only bots fill it
        log.info("Feedback honeypot hit — dropped")
        return _page(thanks_html(contact_given=False, lang=lang, place=place), lang, place)

    submission, error = feedback.clean({**form, "lang": lang, "place": place})
    if error:
        return _page(form_html(form, error, lang, place), lang, place, status=400)

    if not _allowed(_client_address(request)):
        return _page(form_html(form, "rate", lang, place), lang, place, status=429)

    try:
        await feedback.submit(submission)
    except Exception as exc:  # noqa: BLE001 — the client must see a clear page, not a stack trace
        log.error("Could not store client complaint: {}", exc)
        return _page(form_html(form, "failed", lang, place), lang, place, status=503)
    return _page(thanks_html(contact_given=not submission.anonymous, lang=lang, place=place), lang, place)
