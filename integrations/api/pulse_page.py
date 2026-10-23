"""PULSE — the Boss's live dashboard page (2026-10-22, docs/agent-specs/28-pulse.md).

    GET /pulse/enter?t=…          the link from OPS Manager Bot's /pulse: sets the session, drops the token
    GET /pulse                    the page (static HTML + the vendored TradingView chart library)
    GET /pulse/api/overview       the period's index, tickers, people and sectors (?days=7|30|90)
    GET /pulse/api/tape           today's actions, newest first
    GET /pulse/api/person/{id}    one person's line and day (?days=…&day=YYYY-MM-DD)
    GET /pulse/static/{file}      the page's own files and fonts

Read-only, private: only the Director (everyone) and the Garmin manager (his team), checked on every request.
No outside scripts (CSP 'self'); nothing cached by the browser; logs keep no figures.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from integrations.common.db import fetch_one
from integrations.common.logging_setup import setup_logging
from integrations.pulse import access, data

log = setup_logging("pulse")
router = APIRouter(prefix="/pulse", include_in_schema=False)

STATIC = Path(__file__).parent / "static"
FILES = {
    "pulse.css": (STATIC / "pulse.css", "text/css"),
    "pulse.js": (STATIC / "pulse.js", "text/javascript"),
    "lightweight-charts.4.2.0.js": (STATIC / "lightweight-charts.4.2.0.js", "text/javascript"),
    # Latin + Cyrillic subsets of the reports' Noto Sans (integrations/reports/fonts/LICENSE.md): 87 KB, not 1 MB.
    "NotoSans-Regular.woff2": (STATIC / "NotoSans-Regular.woff2", "font/woff2"),
    "NotoSans-Bold.woff2": (STATIC / "NotoSans-Bold.woff2", "font/woff2"),
}
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # style 'unsafe-inline': the chart library sets inline styles on its own elements (2026-10-23); scripts stay 'self'.
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                                "form-action 'none'"),
}
EXPIRED_PAGE = """<!doctype html><html lang="uz-Cyrl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PULSE</title>
<link rel="stylesheet" href="/pulse/static/pulse.css"></head>
<body class="gate"><main><p class="gate-mark">PULSE</p><p>Ҳавола эскирган. Ботда /pulse ни босинг.</p></main></body></html>"""


async def viewer(request: Request) -> tuple[int, tuple[str, ...] | None] | None:
    """(Telegram id, the roles they see — None = everyone) for a valid session of an active Director or
    Garmin manager; None otherwise."""
    from integrations.org_bot.report_calendar import TEAM_OF
    from integrations.org_bot.roles import DIRECTOR_ROLE

    tg = access.verify(request.cookies.get(access.COOKIE), "session")
    if tg is None:
        return None
    row = await fetch_one("SELECT role FROM employees WHERE telegram_user_id = %s AND status = 'active'", (tg,))
    if row is None or row["role"] not in access.ROLES:
        return None
    return tg, (None if row["role"] == DIRECTOR_ROLE else TEAM_OF.get(row["role"], ()))


def _json(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=HEADERS)


@router.get("/enter")
async def enter(t: str = "") -> Response:
    tg = access.verify(t, "link")
    if tg is None:
        return HTMLResponse(EXPIRED_PAGE, status_code=403, headers=HEADERS)
    response = RedirectResponse("/pulse", status_code=303, headers=HEADERS)
    response.set_cookie(access.COOKIE, access.session_token(tg), max_age=access.SESSION_HOURS * 3600, httponly=True,
                        secure=True, samesite="lax", path="/pulse")
    return response


@router.get("")
async def page(request: Request) -> Response:
    if await viewer(request) is None:
        return HTMLResponse(EXPIRED_PAGE, status_code=403, headers=HEADERS)
    return HTMLResponse((STATIC / "pulse.html").read_text(encoding="utf-8"), headers=HEADERS)


@router.get("/api/overview")
async def overview(request: Request, days: int = 30) -> Response:
    who = await viewer(request)
    if who is None:
        return _json({"error": "expired"}, 401)
    try:
        return _json(await data.overview(days, who[1]))
    except Exception as exc:  # noqa: BLE001 — the page shows "couldn't load" and retries
        log.error("PULSE overview failed: {}", type(exc).__name__)
        return _json({"error": "failed"}, 500)


@router.get("/api/tape")
async def tape(request: Request) -> Response:
    who = await viewer(request)
    if who is None:
        return _json({"error": "expired"}, 401)
    try:
        return _json({"items": await data.tape(who[1])})
    except Exception as exc:  # noqa: BLE001
        log.error("PULSE tape failed: {}", type(exc).__name__)
        return _json({"error": "failed"}, 500)


@router.get("/api/person/{person_id}")
async def person(request: Request, person_id: str, days: int = 30, day: str = "") -> Response:
    who = await viewer(request)
    if who is None:
        return _json({"error": "expired"}, 401)
    try:
        chosen = date.fromisoformat(day) if day else None
    except ValueError:
        chosen = None
    try:
        found = await data.person(person_id, days, chosen, who[1])
    except Exception as exc:  # noqa: BLE001
        log.error("PULSE person failed: {}", type(exc).__name__)
        return _json({"error": "failed"}, 500)
    return _json(found) if found is not None else _json({"error": "not_found"}, 404)


@router.get("/static/{name}")
async def static(name: str) -> Response:
    if name not in FILES:
        return Response(status_code=404, headers=HEADERS)
    path, media = FILES[name]
    # The library and fonts never change under their name: kept a year. The page's own files: checked every time
    # (ETag), so a deploy is seen at once.
    keep = name.startswith("lightweight-charts.") or name.endswith(".woff2")
    headers = {**HEADERS, "Cache-Control": "private, max-age=31536000, immutable" if keep else "no-cache"}
    return FileResponse(path, media_type=media, headers=headers)
