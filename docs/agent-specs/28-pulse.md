# 28 — PULSE, the Boss's live dashboard (2026-10-22)

**Asked:** the Boss — "AI needs to monitor workflow and give analytics about employees and the overall
performance just like trading websites, graphs like stock exchange, every action and activity". The owner:
"build the PULSE dashboard as a web page … make it clear and easy to understand, please don't add more text".
Viewers: the Director (everyone) and the Garmin manager (his team); phone and computer equally; company first.

**Code:** `integrations/pulse/` (score.py — the daily score, pure; data.py — the period, the tape, one person;
access.py — links and sessions), `integrations/api/pulse_page.py` (routes), `integrations/api/static/pulse.{html,css,js}`,
TradingView Lightweight Charts 4.2.0 vendored (`lightweight-charts.4.2.0.js` + its Apache-2.0 licence).
Design: PRODUCT.md, DESIGN.md, `.impeccable/surfaces/integrations-api-static-pulse-html.md` (layout «Chart and tape»).

## Opening it

OPS Manager Bot **/pulse** (☰ menu of the Director and the Garmin manager) → a «PULSE» button with that person's
link (signed, `LINK_MINUTES` 15). Opening it sets a session cookie for /pulse only (HttpOnly, Secure, 12 hours)
and drops the token from the address. Every request checks the person is still an active Director or Garmin
manager; anyone else (IT included) gets nothing. Needs `PUBLIC_BASE_URL` (or Render's own URL) and
`OPS_MANAGER_BOT_WEBHOOK_SECRET` (the signing key is derived from it). Strict CSP: no outside scripts, no framing,
nothing cached by the browser.

## The page

**Look (2026-10-23, the owner's reference):** a navy analytics dashboard in two themes, dark and white (☀/☾ in
the left rail, remembered per device): rounded cards, curved index line over a gradient, purple sales bars,
«Бизнеслар» progress bars, avatars. Tokens and layout: DESIGN.md «PULSE screen».

- **Top:** PULSE · the time · 7 / 30 / 90 кун.
- **Six tickers:** Сотув (SAP invoices − credit notes, so'm, by document date), Дўкон (Billz shops), Топшириқ (due
  tasks done on time, %), Ҳисобот (daily reports sent, %), Давомат (on time, %), Шикоят (open now; ▲ is bad) —
  each with ▲▼ against the period before and a sparkline.
- **Индекс (компаниянинг кунлик балли):** the company line — everyone's daily score averaged, 0–100; the line under
  the title says what makes it up (давомат 40 + ҳисобот 30 + топшириқ 30). Its own chart since 2026-10-23.
- **Сотув (кунлик савдо):** SAP invoices − credit notes per day, in so'm, on its own chart with a visible so'm scale
  (2026-10-23 — the owner found one chart with two meanings confusing). Both charts share one crosshair: a day
  under the finger on one is shown on the other; each legend shows that day's number.
- **No TradingView logo on the charts** (2026-10-23, the owner: `attributionLogo: false`); the credit the library
  asks for instead is a small «TradingView графиклари» link at the page's foot (new tab, `noreferrer`).
- **The «Бизнеслар» card was removed** (2026-10-23, the owner); the businesses stay as the board's filter.
- **Бугун (the tape):** today's actions, newest first — arrivals (late in amber), leaving, task steps, reports,
  explanations, complaints in and resolved. Refreshes every minute; a name opens that person. A line under the
  title explains it; the ⓘ button opens a key of the marks (Келди, Кечикди, Кетди, Топшириқ, Ҳисобот, Сабабини
  ёзди, Шикоят келди, Шикоят ҳал бўлди) — open on the first visit, open/closed kept per browser; every mark also
  has its meaning as a hover title.
- **Motion (2026-10-23):** on entry and on a new period the cards rise in, the index and person lines rise from
  the bottom left to right, the sales bars grow, numbers count up, sparklines draw, board and tape rows follow
  one by one; the last index point breathes (live). The scale is fixed to the final values so nothing jumps. A
  refresh every few minutes doesn't move anything; `prefers-reduced-motion` turns it all off.
- **Ходимлар:** everyone ranked by their period score, today's arrival dot, sparkline, ▲▼; Ҳамма / Garmin /
  Primus / Londry / Офис filter.
- **One person** (a panel; a sheet on a phone): their score line (tap a day), the day's three parts as numbers by
  their drawn icons (attendance / report / tasks; a 0 in red; follows the crosshair), and that day in time order —
  arrived, task received / started / done / accepted, report, explanation, left.

A source that can't be read shows «—», never 0. The Garmin manager sees his team and Garmin's sales only.

## The daily score (score.py)

Per person per day, 0–100, only the parts that apply: **attendance 40** (on time 1, late ≤ 30 min 0.6, later 0.3,
absent 0; day off / excused / эркин график not counted), **report 30** (sent 1, «[чатдан тузилди]» 0.7, missed
0; still open today not counted), **tasks 30** (in play that day: done on time 1, done late 0.5, due and not done
0). The Director and the Garmin manager themselves aren't scored. Computed live, cached 10 minutes (the tape 2).

## Speed (2026-10-23 — the owner: "loading is so slow")

- **Data:** the sources (the bot's tables, Verifix, SAP, Billz) are read at the same time (`asyncio.gather`).
  Verifix and Billz are read once per range and shared (`data._sources`): today's tape and a person's month are cut
  from the overview's 60-day Verifix read instead of a new login each time. A kept answer is given at once; an
  aged one is answered at once and rebuilt in the background (`data._cached`, one build per key). Only the very
  first ask after a deploy waits.
- **Kept warm:** `data.keep_warm` (started with the API) builds the 30-day view and the tape for the Director and
  the Garmin manager's team at start and every 9 minutes from 08:00 to 21:00 (Tashkent).
- **Files:** gzip on every response over 1 KB (the 164 KB chart library goes out ~50 KB); fonts are Latin +
  Cyrillic WOFF2 subsets of Noto Sans (87 KB instead of 1 MB of TTF); the library and fonts are kept by the
  browser a year (`immutable`), pulse.css / pulse.js revalidate by ETag.

## What it measures, what it doesn't

Only what passes through the company's own systems (the bot's tables, Verifix, Billz, SAP). No screens,
keystrokes, location or private chats. Read-only.

## Tests

`scripts/selfcheck.py`, `test_pulse`: the score parts and weights, the period (index, tickers, board, sectors),
the tape and a person's day, the tokens, who may open what, the CSP, the served files, the vendored library,
text-only rendering, the /pulse command and menus; speed — a kept answer at once and rebuilt behind it, one
Verifix read shared, sources read at the same time, gzip, WOFF2 fonts kept a year.

**27.10:** the «Шикоят» ticker's number is the new complaints in the period (as its ▲▼ and line); the open ones stay in the data as `open`.
