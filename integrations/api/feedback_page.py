"""The page a client sees after scanning the company's feedback QR code — ``/f``.

One screen, no JavaScript, a few kilobytes: a choice between an opinion and
a complaint, a text box, and an optional name and phone. Empty name and
phone = anonymous. The form posts back here; the message is stored and sent
to the Director by ``integrations/org_bot/feedback.py``.

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
}

_CSS = """
:root{--red:#d71920;--fg:#1d1d1f;--muted:#6e6e73;--line:#d9d9de;--bg:#f5f5f7;--card:#fff}
@media (prefers-color-scheme:dark){:root{--fg:#ececec;--muted:#a1a1a6;--line:#3a3a3c;--bg:#111;--card:#1c1c1e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:var(--red);color:#fff;padding:22px 16px 26px;text-align:center}
header h1{margin:0;font-size:21px;letter-spacing:.3px}
header p{margin:6px 0 0;font-size:14px;opacity:.9}
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


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='uz'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Фикр ва шикоятлар</title><style>{_CSS}</style></head><body>"
        "<header><h1>Фикр ва шикоятлар</h1><p>Ҳар бир хабарни раҳбарият ўқийди</p></header>"
        f"<main>{body}</main></body></html>",
        status_code=status,
        headers=_HEADERS,
    )


def form_html(values: dict[str, str] | None = None, error: str | None = None) -> str:
    """The form, keeping what was typed when something needs fixing."""
    values = values or {}
    kind = values.get("kind", "feedback")
    checked = {k: " checked" if kind == k else "" for k in feedback.KINDS}
    return (
        "<form method='post' action='/f'>"
        + (f"<div class='err'>{_e(error)}</div>" if error else "")
        + "<div class='kinds'>"
        f"<label><input type='radio' name='kind' value='feedback'{checked['feedback']}>💬 Фикр</label>"
        f"<label><input type='radio' name='kind' value='complaint'{checked['complaint']}>⚠️ Шикоят</label>"
        "</div>"
        "<label class='f' for='m'>Хабарингиз</label>"
        f"<textarea id='m' name='message' maxlength='{feedback.MESSAGE_MAX}' required "
        f"placeholder='Фикрингизни шу ерга ёзинг…'>{_e(values.get('message', ''))}</textarea>"
        "<label class='f' for='n'>Исмингиз (ихтиёрий)</label>"
        f"<input id='n' type='text' name='name' maxlength='{feedback.NAME_MAX}' value='{_e(values.get('name', ''))}'>"
        "<label class='f' for='p'>Телефон рақамингиз (ихтиёрий)</label>"
        f"<input id='p' type='tel' name='phone' inputmode='tel' maxlength='25' placeholder='+998 90 123 45 67' "
        f"value='{_e(values.get('phone', ''))}'>"
        "<div class='hp' aria-hidden='true'><input type='text' name='website' tabindex='-1' autocomplete='off'></div>"
        "<p class='note'>Исм ва телефонни ёзмасангиз — хабар аноним юборилади.</p>"
        "<button type='submit'>Юбориш</button></form>"
    )


def thanks_html(contact_given: bool) -> str:
    follow_up = "<p>Керак бўлса, сиз билан боғланамиз.</p>" if contact_given else ""
    return (
        "<div class='done'><h2>✅ Раҳмат!</h2><p>Хабарингиз раҳбариятга юборилди.</p>"
        f"{follow_up}<p><a href='/f'>Яна ёзиш</a></p></div>"
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
async def feedback_form() -> Response:
    """The form behind the company's QR code."""
    if not settings.feedback_enabled:
        return Response(status_code=404)
    return _page(form_html())


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

    if form.get("website"):  # the hidden field: only bots fill it
        log.info("Feedback honeypot hit — dropped")
        return _page(thanks_html(contact_given=False))

    submission, error = feedback.clean(form)
    if error:
        return _page(form_html(form, error), status=400)

    if not _allowed(_client_address(request)):
        return _page(form_html(form, "Кўп хабар юборилди. Бир оздан кейин қайта уриниб кўринг."), status=429)

    try:
        await feedback.submit(submission)
    except Exception as exc:  # noqa: BLE001 — the client must see a clear page, not a stack trace
        log.error("Could not store client feedback: {}", exc)
        return _page(form_html(form, "Хатолик юз берди. Бир оздан кейин қайта юборинг."), status=503)
    return _page(thanks_html(contact_given=not submission.anonymous))
