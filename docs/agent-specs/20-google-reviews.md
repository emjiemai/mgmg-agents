# 20 — Google reviews (2026-10-08)

**Code:** `integrations/api/review_page.py` (the pages), `integrations/org_bot/qr_card.py`
(`stars=True`), Admin Bot `/qr sharh` (`admin._send_review_qr`)
**Pages:** `/r` Londry, `/r/garmin` Garmin; `/r/go/{branch}` opens that branch's Google page
**Setting:** `GOOGLE_REVIEW_URLS` — `yunusobod=…;vuzgorodok=…;abay=…;minor=…`

From the owner: gather Google reviews from clients. Built from the "google
review pack" he sent (a star page sending every rating to one Google link),
made to fit this project.

## Separate from complaints

`/f` and `/f/garmin` stay the complaint pages (to the Director, never
changed — Londry's `/f` is already printed). Reviews go to Google, from their
own pages and their own printed cards.

## The page

The same layout as `/f` (large type, no JavaScript) but in **positive colours —
Google green (#1e8e3e) and gold stars, never the complaint red** (the owner,
2026-10-08). Uzbek
Cyrillic by default, Russian, English — no Uzbek Latin. Five drawn stars, then
one big button per branch (Londry Юнусобод / Вузгородок, Garmin Абай /
Минор); a branch without a link shows "тез орада". A branch's own card opens
`/r?branch=yunusobod`, which shows only that branch (and "Бошқа филиаллар").

**Honest by design:** no rating is asked first and nobody is steered away
from Google — every client gets the same Google link, and the page says so
("Ҳар қандай баҳо — яхши ёки ёмон — бир хил Google саҳифасини очади").
Google's policy forbids review gating; the pack's own note said the same.
Complaints still have their own page and card.

## Safety

- The link comes only from `GOOGLE_REVIEW_URLS` and only https links on
  Google's hosts (g.page, google.com, search.google.com, maps.app.goo.gl…)
  are used; anything else is skipped with a log warning. The pack's
  `?url=` parameter (an open redirect) is not kept.
- Each tap is one `agent_actions` row (`review-page` / `review_click`,
  `target_ref` = branch) — nothing about the person.

## Getting the links

For each branch: Google Business Profile → Ask for reviews / "Get more
reviews" → copy the link (looks like `https://g.page/r/…/review`), and put
all four in `GOOGLE_REVIEW_URLS` in Render (mgmg-shared). Then Admin Bot
`/qr sharh` sends one printable card per branch that has a link.

## Cards

The project's QR card layout (the business's logo in white, the code on a
white square, no text) on **green** instead of red, with five white stars
under the code — a printed review card can't be taken for a complaint card.

## Tests

`scripts/selfcheck.py`, `test_review_pages`: links validated, page honest and
Cyrillic, branch preselect, redirect only to the configured link (never the
address bar's), tap counted, `/f` untouched, star card, `/qr sharh` without links.
