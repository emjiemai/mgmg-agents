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

- **Top:** PULSE · the time · 7 / 30 / 90 кун.
- **Six tickers:** Сотув (SAP invoices − credit notes, so'm, by document date), Дўкон (Billz shops), Топшириқ (due
  tasks done on time, %), Ҳисобот (daily reports sent, %), Давомат (on time, %), Шикоят (open now; ▲ is bad) —
  each with ▲▼ against the period before and a sparkline.
- **Индекс:** the company line (everyone's daily score averaged) over SAP sales bars; the legend follows the
  crosshair (that day's index and sales, numbers only).
- **Бугун (the tape):** today's actions, newest first — arrivals (late in amber), leaving, task steps, reports,
  explanations, complaints in and resolved. Refreshes every minute; a name opens that person.
- **Ходимлар:** everyone ranked by their period score, today's arrival dot, sparkline, ▲▼; Ҳамма / Garmin /
  Primus / Londry / Офис filter.
- **One person** (a panel; a sheet on a phone): their score line (tap a day), and that day in time order —
  arrived, task received / started / done / accepted, report, explanation, left.

A source that can't be read shows «—», never 0. The Garmin manager sees his team and Garmin's sales only.

## The daily score (score.py)

Per person per day, 0–100, only the parts that apply: **attendance 40** (on time 1, late ≤ 30 min 0.6, later 0.3,
absent 0; day off / excused / эркин график not counted), **report 30** (sent 1, «[чатдан тузилди]» 0.7, missed
0; still open today not counted), **tasks 30** (in play that day: done on time 1, done late 0.5, due and not done
0). The Director and the Garmin manager themselves aren't scored. Computed live, cached 10 minutes (the tape 2).

## What it measures, what it doesn't

Only what passes through the company's own systems (the bot's tables, Verifix, Billz, SAP). No screens,
keystrokes, location or private chats. Read-only.

## Tests

`scripts/selfcheck.py`, `test_pulse`: the score parts and weights, the period (index, tickers, board, sectors),
the tape and a person's day, the tokens, who may open what, the CSP, the served files, the vendored library,
text-only rendering, the /pulse command and menus.
