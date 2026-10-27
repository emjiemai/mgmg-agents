# Agents — what runs, what's left

Status against the owner's plan (*ЭМЖИЕМ AI Агентлар Тизими*, 21 agents).
Everything runs through the same engine: two Telegram bots (OPS Manager Bot
for everyone, Admin Bot for the admin) on the always-on `mgmg-api` service,
plus scheduled jobs at 08:00, 16:00 and 17:00. Last updated 2026-10-03. Board for the Director: `docs/board/` (SVG, drag into Figma).

## Working

| Plan | Agent | When it acts | Who gets it | Switch |
| ---- | ----- | ------------ | ----------- | ------ |
| H0 | Hub — API, database, both bots, SAP push receiver | always on | — | — |
| A1 | Daily reports: ask, remind, one follow-up on a vague report | 16:00 ask · 17:00 reminder · **22:00 close: no report = −1 KPI point, told** (from 16.10) (Mon–Fri; Sat/Sun only weekend workers — Admin Bot `/xodimlar`; off per person: /xodimlar → 📝) | employees; non-reporters in the 08:00 brief | `DAILY_REPORTS_ENABLED` (**must be `true`**) |
| A2 | Morning brief — one designed picture (2026-10-05, `18-reports.md`); since 2026-10-16 it opens with vision/mission, the month's goals, the tasks and who didn't report, then sales, stock, debt, payments — **no «Касса»** | 08:00 daily | Director | `COMPANY_VISION`, `COMPANY_MISSION` (**not set yet**) |
| A3 | Task tracker: deadlines, reminders, overdue notices, weekly scorecard | on each task · 08:00 · Friday 17:00 | employees, Director | `TASK_TRACKER_ENABLED` |
| A4 | Attendance from Verifix: late / didn't come / excused; people on "эркин график" (Admin Bot `/grafik`) are only counted, never late or absent | 08:00 brief (yesterday) · any time: "kim kechikdi?" · `/verifix` check · `/grafik` marks | Director | `VERIFIX_LOGIN` / `VERIFIX_PASSWORD` / `VERIFIX_FILIAL_ID` (set, working since 2026-10-01) |
| B1 | Written permissions (EMJ-SOP-ADM-01) + payment gate by amount | on request ("ruxsat") | requester, approvers | `PERMISSIONS_ENABLED`, limits in `PERMISSION_APPROVAL_TIERS` (**not set yet**) |
| B2 | 30-day cash calendar | Monday 08:00 · any time: "pul kalendari" | Director, accountants | `CASH_CALENDAR_ENABLED` |
| B4 | Data quality check (no amounts since 2026-10-07) | Monday 08:00 · `/sifat` | admin (IT) | `DATA_QUALITY_ENABLED` |
| E1 | KPI per employee — the Director's 15 criteria as 6 parts (goals/OKR, tasks, rating, volume, process, commitment) | `/maqsad` `/kpi` `/natija` `/baho` · 1st: ratings · final by the 5th | Director, HR, each employee (own card) | `MONTHLY_KPI_ENABLED` |
| — | Receivables alert (overdue debt by age) — complete, paid part taken off (gateway `get_open_invoices`, 2026-10-03) | 08:00 daily | Director | — |
| F2 | Lead Agent: tender and lead search (Londry, Garmin/Tanita), to the leads sheet | 08:00 daily (resumed 2026-09-30) | Director (summary) + Google leads sheet | `LEAD_AGENT_ENABLED` (**true**) |
| — | Q&A — the Director asks anything about the company; since 2026-10-07 the **analyst** looks it up itself across SAP, 1C (balances, turnovers, by counterparty), BILLZ, Verifix and the bot's records with read-only tools, never asks "which system?" (`19-director-analyst.md`); since 2026-10-10 it knows the company (`knowledge.py`) and, when it doesn't understand, asks one question with the likely meanings as buttons | on question | Director | `OPS_ANALYST_ENABLED` (true) |
| — | IT's technical report — connections (SAP push, 1C, BILLZ, Verifix, AI), brief delivered, runs and errors, DB size, the last backup copy, refused attempts; **no figures** (the Director's order of 07.10.2026) | 08:00 (after the other agents) · `/texnik` | admin (IT) | — |
| — | **Brief vs SAP check** (2026-10-15) — every SAP kind and every brief line checked; causes and the to-do list in `docs/sap-data-check-2026-10-15.md` | — | IT, accounting | — |
| — | **Garmin manager** (2026-10-14) — every Garmin complaint; SAP + Billz + Garmin data through the bot (by role); a Garmin morning brief picture (`23-garmin-manager.md`; four figures, no stock since 19.10, every «Garmin» warehouse) | 08:00 (after the Billz → SAP check) · on question | Garmin manager | role `garmin_manager` |
| — | Company data for accounting and finance (2026-10-10) — 1C, SAP, Billz through OPS Manager Bot for people the admin gives them to (Бухгалтерия, Молия only); the Director is told and can close it; Russian answers if set (`22-data-access.md`); the toggles work since 15.10 (they flipped back and forth before) | on question | accountant, finance | admin: `/xodimlar` → SAP / 1C / Billz |
| — | Database backups (IT-5, 2026-10-10) — encrypted to the owner's key, fetched daily by an office computer + second disk; monthly restore test (`scripts/backup/README.md`) | daily 13:00 (office computer) | owner (key), IT (files) | `BACKUP_SECRET`, `BACKUP_PUBLIC_KEY` (**not set yet**) |
| — | Tasks: the Director ticks who gets each one (the bot's guess pre-ticked), nothing goes out before Юбориш. **Task register** (2026-10-13, spec of 11.10 Day 1): every task has a code (T-2026-10-0147) and a state; ✅ needs proof; the assigner accepts or returns it; ⛔ with the reason; history in `task_events` | on message | employees, Director | — |
| — | **Cheque photos → accounting** (2026-10-14): the Director's photo is read by the AI; a cheque with all eight details (фискал белги, ФМ рақами, чек рақами, сана, RRN, сумма, товар, ИНН) goes to Бухгалтерия with the photo, otherwise the Director is asked; not a cheque → the people list (`23-cheques.md`) | on photo | Бухгалтерия, Director | `CHEQUE_READER_ENABLED` (true), `CHEQUE_RECIPIENT_ROLES` |
| — | Names and roles — every employee's typed name; admin re-asks a name or changes a role | on registration · `/xodimlar` · `/ism` | admin, employees | — |
| — | ~~Team cheer~~ — **removed 2026-10-17** (the owner: not effective); code, tests and the cron gone (`12-team-cheer.md` kept as history) | — | — | — |
| — | Days off and announcements: Admin Bot `/dam` stops everything employee-facing on a holiday; `/elon` tells everyone (e.g. a technical error) | on demand | employees | — |
| A2+ | Cash in the brief from 1C (bank + cash desk, change since yesterday); "hisobda qancha pul?" | 08:00 · any time · `/1c` check | Director | `ONEC_ODATA_URL` / `ONEC_LOGIN` / `ONEC_PASSWORD` |
| — | Shop sales from BILLZ: yesterday per shop in the brief; shops, sellers, top products on question | 08:00 · any time: "do'konlarda savdo" · `/billz` check | Director | `BILLZ_SECRET_TOKEN` (set, working since 2026-10-02) |
| B3− | Billz → SAP check: every shop cheque must be in SAP the same day — not entered, wrong amount, entered late, in SAP but not in Billz (`docs/agent-specs/17-billz-sap-check.md`) | 08:00 daily (yesterday; missing cheques repeated until entered) | Director (+ `BILLZ_SAP_CHECK_ROLES`) | `BILLZ_SAP_CHECK_ENABLED`; **trial**: to the admin until the first results are confirmed (`BILLZ_SAP_CHECK_TRIAL=false` → Director) |
| — | Work AI: every employee's message about their work and tasks is answered by the AI (honest, no company data); the bot carries no text between people — only files, after "what is it for?" | on message, 60 a day | employees | Admin Bot `/xodimlar` → 🤖 (off for one person) |
| — | Database viewer (read-only) — since 2026-10-07 technical tables only (audit log, access requests, changes, days off…); financial and confidential tables are listed closed | `/db` on the API | admin | `DB_VIEWER_PASSWORD` |
| — | Client complaints via QR codes — Londry `/f`, Garmin `/f/garmin` (anonymous allowed; Uzbek Cyrillic, Russian, English) — since 2026-10-13 a **ticket for the department** (Londry role / Garmin менежер), ✅ + reason code, hourly "ҳал бўлдими?" | when a client scans · `/qr` makes both cards | Londry / Garmin sales (the Boss only if nobody is in the role) | `FEEDBACK_ENABLED` |
| — | Google reviews via their own QR codes — Londry `/r`, Garmin `/r/garmin` (separate from the complaint pages; green, not red): pick the branch → that branch's Google review page; every client gets the same link (no review gating); taps counted (`20-google-reviews.md`) | when a client scans · `/qr sharh` makes the cards | Google (public reviews) | `GOOGLE_REVIEW_URLS` (**not set yet**) |

**10 of the plan's 21** are running (H0, A1, A2, A3, A4, B1, B2, B4, E1, F2). Of these,
only H0 has been running long enough to call proven; the rest are in their
first weeks — the plan asks for each one's И to be measured before the next
is started (A1 and A3 are measured automatically every Friday).

## Stopped by the business

- **F2+ Lead hand-out to B2B sales** (08:00 one lead each, 15:00 "how is it
  going?") — stopped and removed on 2026-10-07 by the Director, who will do
  it another way. Nothing goes to B2B Sotuv; its history stays in the data,
  in October's KPI and in the Director's answers (`14-lead-handout.md`).

The Lead Agent (F2) itself — the tender and lead search into the sheet —
keeps running (paused 2026-09-16, back on 2026-09-30).

## Left — and what each is waiting for

| Plan | Agent | What unblocks it |
| ---- | ----- | ---------------- |
| C3 | Dead-stock sales | A gateway tool for slow-moving stock (OITW + last purchase dates) — `docs/sap-gateway-tools.md`, "Later" |
| D1 | Stock & reorder signal | Gateway tools for item stock, sales lines (`get_sales_by_date`) and open purchase orders |
| C2 | Sales forecast & targets | Sales per sales person over months (`get_sales_by_date` covers 31 days at a time) |
| B3 | Reconciliation bank ↔ SAP ↔ 1C ↔ Didox — **started 2026-10-09**: supplier debt 1C ↔ SAP (`integrations/onec/payables.py`, `21-ap-reconcile.md`): SAP's `supplier_balances` arrive from the gateway; the Director asks OPS Manager Bot (text, and the .xlsx on «солиштир» / «excel», 2026-10-10) | SAP supplier balances from the gateway; bank and Didox access |
| C1 | Lead collector (Telegram, WhatsApp, Instagram) — **in progress**: the Garmin AI bot (separate repo emjiemai/Garmin-AI-bot: Instagram Reels → web catalog → AI Telegram bot → manager) is live; since 2026-10-01 its leads are stored in the Command Center (`garmin_leads`, the Director asks "garmin lidlari") — `docs/agent-specs/14-garmin-leads.md` | WhatsApp Business API and Instagram access |
| C4 | Service & contract reminders | Gateway tools for equipment cards (OINS — 1,678 machines in SAP), service calls (OSCL), contracts (OCTR) |
| C5 | Customer win-back | Complete purchase history per customer from SAP (a gateway tool) |
| F1 | Marketing & content plan | Instagram access (a plan-only version could be built without it) |
| G1 | Owner's personal assistant (email) | Outlook access |
| D2 | Documents & certificates | Nothing technical — rare work, the plan puts it later |
| E2 | AI operations manager | The plan says build it last, on top of A1, A2, A3, B1, B2; OPS Manager Bot already covers part of it |

**The cheapest unblock:** more SAP gateway tools of the same kind as the
three added on 2026-10-03 (`docs/sap-gateway-tools.md`, "Later"): equipment
cards + service calls open C4; item stock with last purchase dates opens C3
and D1; open purchase invoices complete B2's money out; longer sales history
opens C2 and C5.

## Keeping this current

Update this file and the tracker (`ЭМЖИЕМ_AI_Агентлар_Трекери`, column
ҲОЛАТ) whenever an agent changes state; each agent's details are in
`docs/agent-specs/`.

**Bot menus (2026-10-16):** both bots show their / commands behind the ☰ button — per role in OPS Manager (`integrations/telegram/menus.py`).

**SAP data (2026-10-16):** every kind the plan needs is a complete gateway tool (`sap-gateway/src/complete.js`, `docs/sap-gateway-tools.md` "Everything the plan needs") — waiting for the gateway computer to be updated.

**Database review (2026-10-17):** every morning the bot's own tables are checked, safe fixes made (UPDATEs only), counts to IT — `24-db-review.md`, Admin Bot `/baza`. **Report calendar:** the Director's `/hisobotlar`.

**Old stock (2026-10-17):** Mondays 08:00, the Director and the Garmin manager — goods a year or more in stock (`25-old-stock.md`), /eski.

**Garmin competitors (2026-10-19):** Thursdays 08:00, the Garmin manager — who else sells Garmin in Uzbekistan (OLX, Google) and for how much against garmin.com.uz (`26-garmin-competitors.md`), /raqobat. Since 2026-10-20 with each shop's name, phones, Instagram, Telegram, site (what the shop publishes; private persons never).

**Explanations (2026-10-22):** 08:00 after the brief — absent yesterday (Verifix) or report not sent → asked why; answers to the Director as cards (`27-explanations.md`). /grafik 🕘 own start times.

**PULSE (2026-10-22):** the Boss's live dashboard at /pulse (OPS Manager Bot /pulse link): index over sales, tickers, today's tape, people ranked, one person's day (`28-pulse.md`). 2026-10-23: new navy look, dark + white themes; loads fast (parallel reads,
shared Verifix, kept copy, warm 08–21, gzip, WOFF2 fonts).

**Report bonus (2026-10-22):** 08:00, Garmin сотув — yesterday's report sent → +1 KPI, missed → politely none (spec 06).

**Excused days and late reports (2026-10-23):** ⬜ Сабабли on explanation cards and /sababli turn a day grey; a missed report sent the next day goes to the Director / Garmin manager — approved, green (`29-excused-and-late-reports.md`).

**One morning message (2026-10-24):** `agents/morning-notes/` — the report bonus, the explanation question and task reminders come to each employee as ONE message, at most one question, last (`30-morning-message.md`); the work AI knows what the bot waits for from each person.

**Verifix days without data + admin report sheet (2026-10-24):** a day the terminals didn't record is «no data» (automatic, or ⛔ on Admin `/verifix`) — nobody absent or late, wrong questions withdrawn with an apology (`11-attendance.md`); Admin `/xodimlar` → 📊 the month's report days, tap 🟩 / 🟥 / ⬜, the Director gets a summary (`29-excused-and-late-reports.md`).


**Lead groups (2026-10-26):** Garmin and Primus leads to their own Telegram groups, one message per lead (`lead_groups.py`; specs 04, 14).

**Reports any time from 14:00, the AI decides (2026-10-26):** sure → saved, unsure → asked; after 22:00 → for approval; the work AI never claims a report was accepted (specs 06, 15).

**Garmin stock as quantity (2026-10-27):** one row in the Garmin brief (pieces, models, top warehouses); the analyst answers stock questions in pieces.

**Garmin stock (2026-10-27):** `/qoldiq` and the analyst's `garmin_stock_list` — every product left, pieces per warehouse, Excel; no buying advice.
