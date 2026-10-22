# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **The Director ("the Boss")** of MGMG (ЭМЖИЕМ), Tashkent — runs three businesses (Garmin shops, Primus
  B2B laundry equipment, Londry self-service laundries) and every department. Reads the bots' output in
  Telegram on his phone and on a computer, equally.
- **The Garmin manager** — runs the Garmin business at the Boss's level; sees only his Garmin сотув team.
- **Employees** — use OPS Manager Bot in Telegram (reports, tasks, the work AI). They never see PULSE.
- **IT (the admin)** — builds and runs the system; sees no company figures (the Director's order of 07.10.2026).

## Product Purpose

MGMG Digital Command Center: Telegram bots (OPS Manager Bot, Admin Bot) and a FastAPI service that read the
company's systems (SAP B1, 1C, BILLZ, Verifix) read-only, turn them into morning briefs, reports and answers,
and run the daily work loop (tasks, daily reports, KPI, complaints, explanations). Success: the Boss knows
how the company and each person are doing without asking anyone.

**PULSE** (2026-10-22): a private web page for the Boss — "like trading websites, graphs like stock
exchange, every action and activity" — the company first (a pulse index and sales over time), then each
person's activity, then one person's day.

## Operating Context

- Opened from a link the bot sends in Telegram (no passwords to remember); phone and desktop equally.
- Data: the bot's own tables (tasks and task events, daily reports, explanations, complaints), Verifix
  attendance (live), BILLZ shop sales (live), SAP sales pushed every 30 minutes.
- Times are Asia/Tashkent. Money is so'm.

## Capabilities and Constraints

- Every user-facing word is **Uzbek, Cyrillic script**. Minimal text: "please don't add more text in it"
  (the owner, 2026-10-22).
- Stack: Python FastAPI on Render (`integrations/api/`), Jinja2 templates; no Node build step.
- Read-only: PULSE changes nothing anywhere.
- Personal data stays on the company's service; never in URLs beyond a signed access token.
- Measures only what passes through the company's own systems — no screens, keystrokes, location or
  private chats.

## Brand Commitments

- Business names: **Londry** (never "Laundry"), Primus, Garmin.
- Reports elsewhere in the system are light pages, red only for what needs the Director, drawn icons, no
  emoji (`integrations/reports/`, docs/agent-specs/18-reports.md).

## Evidence on Hand

Real data only (the tables and platforms above). No invented figures, people or benchmarks.

## Product Principles

1. Company first, then people, then one person's day.
2. A number is never shown without where it comes from being knowable; missing data is shown as missing.
3. Red only for what needs the Boss's attention.
4. As little text as possible: numbers, arrows, charts.
5. The Garmin manager sees only his team.
