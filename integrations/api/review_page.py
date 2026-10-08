"""Google review pages — ``/r`` (Londry) and ``/r/garmin``, behind their own QR codes.

From the owner, 2026-10-08: gather Google reviews from clients. Built from
the "google review pack" he sent (a star page that sends every rating to the
same Google review link), made to fit this project:

  * **separate from the complaint pages** ``/f`` and ``/f/garmin``, which
    stay exactly as they are — a complaint goes to the Director, a review
    goes to Google;
  * one page per business, the branch picked with big buttons (each branch
    is its own Google Business Profile, with its own review link);
    ``?branch=`` (on a branch's own printed card) shows only that branch;
  * Uzbek Cyrillic (default), Russian, English — no Uzbek Latin; the same
    layout as ``/f`` (large type, no JavaScript) but in positive colours —
    Google green and gold stars, never the complaint page's red;
  * **honest**: every client gets the same Google link whatever they think —
    no rating is asked first and nobody is steered away from Google
    (Google's policy forbids "review gating"; the pack's own note said so too).

Safety: the link a button opens comes only from ``GOOGLE_REVIEW_URLS`` (set
in Render), and only https links on Google's own hosts are used — the pack's
``?url=`` parameter, which would have let anyone turn the page into a
redirect to any site, is not kept. Each tap is counted in ``agent_actions``
(``review_click``, the branch only — nothing about the person).
"""

from __future__ import annotations

import html
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from integrations.api.feedback_page import BRANCH_LABELS, _CSS, lang_from
from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.org_bot.feedback import BRANCHES, PLACES

log = setup_logging("review-page")
router = APIRouter(prefix="/r", include_in_schema=False)

PAGES: dict[str, str] = {"laundry": "/r", "garmin": "/r/garmin"}
assert set(PAGES) == set(PLACES)
# branch key -> its business (the keys are unique across businesses)
BRANCH_PLACE: dict[str, str] = {b: place for place, branches in BRANCHES.items() for b in branches}
assert len(BRANCH_PLACE) == sum(len(b) for b in BRANCHES.values())

# Where a review link may point: Google's own review / maps / short-link hosts.
GOOGLE_HOSTS = frozenset({"g.page", "www.google.com", "google.com", "maps.app.goo.gl", "goo.gl",
                          "search.google.com", "maps.google.com", "www.google.co.uz", "google.co.uz"})

_HEADERS = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Vary": "Accept-Language",
}

TEXTS: dict[str, dict[str, str]] = {
    "uz_cyrl": {
        "html_lang": "uz-Cyrl", "switch": "Ўзбекча",
        "title": "Google'да баҳоланг",
        "intro": "Фикрингиз бошқа мижозларга танлашда ёрдам беради.",
        "branch": "Қайси филиал?",
        "go": "Google'да шарҳ қолдириш",
        "soon": "тез орада",
        "note": "Ҳар қандай баҳо — яхши ёки ёмон — бир хил Google саҳифасини очади. Илтимос, ҳалол ёзинг.",
        "all": "Бошқа филиаллар",
    },
    "ru": {
        "html_lang": "ru", "switch": "Русский",
        "title": "Оцените нас в Google",
        "intro": "Ваш отзыв помогает другим клиентам сделать выбор.",
        "branch": "Какой филиал?",
        "go": "Оставить отзыв в Google",
        "soon": "скоро",
        "note": "Любая оценка — хорошая или плохая — открывает одну и ту же страницу Google. Пожалуйста, пишите честно.",
        "all": "Другие филиалы",
    },
    "en": {
        "html_lang": "en", "switch": "English",
        "title": "Review us on Google",
        "intro": "Your review helps other clients choose.",
        "branch": "Which branch?",
        "go": "Leave a review on Google",
        "soon": "coming soon",
        "note": "Every rating — good or bad — opens the same Google page. Please be honest.",
        "all": "Other branches",
    },
}

_STAR = "<path d='M12 2.6l2.8 5.7 6.3.9-4.6 4.4 1.1 6.2L12 16.9l-5.6 2.9 1.1-6.2-4.6-4.4 6.3-.9z'/>"
# Positive colours (2026-10-08, the owner: red is the complaint page's colour,
# a review page in red isn't right): Google green, gold stars. The shared /f
# stylesheet is reused, its red variables replaced here.
GREEN, GREEN_PRESS = "#1e8e3e", "#17733a"
_CSS_EXTRA = """
:root{--red:#1e8e3e;--red-press:#17733a;--head:#1e8e3e;--head-soft:#e6f4ea;--tint:#e6f4ea;--ring:#1e8e3e}
@media (prefers-color-scheme:dark){:root{--red:#2a9d4b;--red-press:#1e8e3e;--head:#146c2e;--head-soft:#cdebd6;
 --tint:#15301f;--ring:#5cc77d}}
::selection{background:#1e8e3e}
nav span{color:#17733a}
.stars{display:flex;justify-content:center;gap:6px;margin:0 0 24px;color:#f2b01e}
.stars svg{width:36px;height:36px;fill:currentColor}
.go{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:72px;padding:12px 16px;border-radius:16px;
 background:var(--red);color:#fff;text-decoration:none;font-size:19px;font-weight:700;text-align:center;transition:background-color .15s}
.go small{font-size:15px;font-weight:550;opacity:.92}
.go:hover{background:var(--red-press)}
.go+.go,.go+.off,.off+.go,.off+.off{margin-top:12px}
.off{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:72px;padding:12px 16px;border-radius:16px;
 border:1.5px dashed var(--line);color:var(--muted);font-size:19px;font-weight:650;text-align:center}
.off small{font-size:15px;font-weight:500}
.note{margin:22px 0 0;font-size:16px;color:var(--muted);text-align:center}
.all{display:block;margin-top:18px;text-align:center;font-size:16px;color:var(--muted)}
"""


def review_urls(setting: str | None = None) -> dict[str, str]:
    """``GOOGLE_REVIEW_URLS`` read: branch -> its Google review link (only valid Google https links)."""
    found: dict[str, str] = {}
    for part in (settings.google_review_urls if setting is None else setting).split(";"):
        key, _, url = part.partition("=")
        key, url = key.strip().lower(), url.strip()
        if key in BRANCH_PLACE and is_google_link(url):
            found[key] = url
        elif key or url:
            log.warning("GOOGLE_REVIEW_URLS: skipped '{}' (unknown branch or not a Google https link)", key or "?")
    return found


def is_google_link(url: str) -> bool:
    """An https link on one of Google's own hosts — nothing else is ever redirected to."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() in GOOGLE_HOSTS and not parts.username


def _e(value: str) -> str:
    return html.escape(value or "", quote=True)


def page_html(place: str, lang: str, branch: str | None, links: dict[str, str]) -> str:
    """The whole page for one business (and, from a branch's own card, one branch)."""
    t = TEXTS[lang]
    shown = [branch] if branch in BRANCHES[place] else list(BRANCHES[place])
    buttons = []
    for key in shown:
        label = _e(BRANCH_LABELS[lang][key])
        if key in links:
            buttons.append(f"<a class='go' href='/r/go/{key}?lang={lang}' rel='noopener'>{label}<small>{_e(t['go'])}</small></a>")
        else:
            buttons.append(f"<div class='off' aria-disabled='true'>{label}<small>{_e(t['soon'])}</small></div>")
    switcher = "".join(
        f"<span aria-current='true'>{_e(TEXTS[code]['switch'])}</span>" if code == lang
        else f"<a href='{PAGES[place]}?lang={code}" + (f"&amp;branch={branch}" if branch in BRANCHES[place] else "")
             + f"' hreflang='{TEXTS[code]['html_lang']}' lang='{TEXTS[code]['html_lang']}'>{_e(TEXTS[code]['switch'])}</a>"
        for code in TEXTS
    )
    legend = "" if len(shown) == 1 else f"<p class='label'>{_e(t['branch'])}</p>"
    others = (f"<a class='all' href='{PAGES[place]}?lang={lang}'>{_e(t['all'])}</a>"
              if len(shown) == 1 and len(BRANCHES[place]) > 1 else "")
    stars = "<div class='stars' aria-hidden='true'>" + "".join(
        f"<svg viewBox='0 0 24 24' focusable='false'>{_STAR}</svg>" for _ in range(5)) + "</div>"
    return (
        f"<!doctype html><html lang='{t['html_lang']}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta name='theme-color' content='{GREEN}'>"
        f"<title>{_e(t['title'])} · {_e(PLACES[place])}</title><style>{_CSS}{_CSS_EXTRA}</style></head><body>"
        f"<header><div class='wrap'><div class='top'><span class='brand' lang='en'>{_e(PLACES[place])}</span>"
        f"<nav>{switcher}</nav></div><h1>{_e(t['title'])}</h1><p>{_e(t['intro'])}</p></div></header>"
        f"<main><div class='sheet'>{stars}{legend}{''.join(buttons)}<p class='note'>{_e(t['note'])}</p>{others}</div></main>"
        "</body></html>"
    )


def _show(request: Request, place: str, lang: str | None) -> Response:
    chosen = lang_from(lang, request.headers.get("accept-language"))
    branch = request.query_params.get("branch", "")
    return HTMLResponse(page_html(place, chosen, branch, review_urls()),
                        headers={**_HEADERS, "Content-Language": TEXTS[chosen]["html_lang"]})


@router.get("", response_class=HTMLResponse)
async def laundry_page(request: Request, lang: str | None = None) -> Response:
    """Londry's review page."""
    return _show(request, "laundry", lang)


@router.get("/garmin", response_class=HTMLResponse)
async def garmin_page(request: Request, lang: str | None = None) -> Response:
    """Garmin's review page."""
    return _show(request, "garmin", lang)


@router.get("/go/{branch}", include_in_schema=False)
async def go(branch: str, lang: str | None = None) -> Response:
    """One tap: count it (branch only) and open that branch's Google review page."""
    url = review_urls().get(branch)
    place = BRANCH_PLACE.get(branch)
    if url is None:
        return RedirectResponse(PAGES[place] if place else "/r", status_code=303)
    await log_action(agent="review-page", action="review_click", target_system="google", status="success",
                     target_ref=branch, mode="read")
    return RedirectResponse(url, status_code=303)


@router.get("/{other}", include_in_schema=False)
async def other_link(other: str) -> Response:
    """Anything else under /r goes to Londry's review page."""
    return RedirectResponse("/r", status_code=301)
