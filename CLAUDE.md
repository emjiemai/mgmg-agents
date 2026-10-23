# CLAUDE.md — read this first (any machine)

## Who I am
Ulugbek (GitHub Ulugbek220907), IT specialist / AI agent builder at Primus
Londry (MGMG / ЭМЖИЕМ). I build and run this system alone. I switch between a
PC and a laptop — this file is the shared context; chat history is not.

## How to work with me
- **Don't guess — verify.** Read the code/docs/API before claiming anything.
- **Concise answers.** Results first, short.
- **Every bot and user-facing text is Uzbek Cyrillic** (names too: Latin → Cyrillic).
  Code comments, docs, commit messages: English. **One exception** (2026-10-10): a person the admin
  set to Russian (`/xodimlar` → 🌐, `employees.lang`) gets the AI's answers (analyst, work AI) in
  Russian; the supplier-debt `.xlsx` comes in Uzbek and Russian. Fixed bot messages stay Uzbek.
- **After each fix: run checks, then commit and push** (authorized). Commit only
  if `PYTHONIOENCODING=utf-8 python scripts/selfcheck.py` exits 0 (don't judge by
  piped/tailed output). Also `python -m pyflakes integrations agents scripts`.
- Add tests to `scripts/selfcheck.py` for every change; update the matching
  `docs/agent-specs/*.md` and `docs/agents-status.md`.
- **ALWAYS update `README.md` with every change, even a small one** (the owner, 2026-10-10): what
  changed, how to use it, new settings/commands/links, and a dated line in its **§10 Change log**
  (newest first) — README is the handover guide for whoever comes next.
- On Windows, never pipe Cyrillic through stdin heredocs — write patch
  scripts/files with the editor instead.

## What this is
MGMG Digital Command Center: Python + FastAPI + PostgreSQL on Render
(`render.yaml`, env group `mgmg-shared`, secrets `sync: false`).
- **mgmg-api** (Starter plan, always on): Admin Bot (admin) + OPS Manager Bot
  (employees + Director) webhooks, SAP gateway push receiver, `/db` read-only
  viewer, `/f` client complaints page (Uzbek Cyrillic, Russian, English).
- **Crons:** 08:00 `scripts/run_morning_agents.py` (brief, Lead Agent,
  receivables, task tracker, data quality Mon, cash calendar Mon,
  monthly KPI 1st); 16:00 daily-reports ask; 17:00 reminder + Friday
  scorecard; 22:00 reports close (inside the API). **Team cheer removed 2026-10-17** (the owner: not
  effective; code, tests, cron gone — delete the `mgmg-team-cheer` service in Render). Daily; weekends only
  for employees marked as weekend workers.
- **Leads to B2B sales: STOPPED 2026-10-07** by the Director (he will do it another way) — the AI
  sends nothing to B2B Sotuv; `agents/lead-handout/` is deleted; history kept (`14-lead-handout.md`).
  The Lead Agent still fills the sheet ("POSSIBLE Leads", tab Sheet1).
- **Database review** (2026-10-17, `agents/db-review/`, `docs/agent-specs/24-db-review.md`, Admin Bot `/baza`): the bot's own
  tables only, safe UPDATE fixes, counts to IT. **Report calendar**: the Director's `/hisobotlar` (PNG).
- IT's technical report (`agents/tech-report/`, last in the 08:00 job, Admin Bot `/texnik`): connections,
  SAP push, brief delivered, runs/errors, DB size, refused attempts — counts and times only.
- KPI (the Director's 15 criteria → 6 parts, `docs/agent-specs/13-kpi.md`): `/maqsad` `/maqsadlar`
  `/kpi` `/baho` (Director), `/kpi` `/natija` (employees); ratings on the 1st, final by the 5th.
- Garmin AI bot (separate repo emjiemai/Garmin-AI-bot, cloned at D:\Garmin-AI-bot) POSTs its leads to
  `/webhooks/garmin-lead/{GARMIN_LEADS_SECRET}` → table `garmin_leads`; Q&A agent `garmin_lidlar`.
- 1C «Бухгалтерия для Узбекистана» on Clobus, OData read-only (`integrations/onec/`, `docs/agent-specs/16-1c.md`):
  Admin `/1c` check; Q&A `pul_qoldigi` = 1C class-5000 balances (bank / нақд). **No «Касса» in the 08:00
  brief** (the owner, 2026-10-16): it opens with vision/mission (`COMPANY_VISION`/`COMPANY_MISSION`), month
  goals, tasks and reports, then sales, stock, debt, payments.
- Verifix logs in with VERIFIX_LOGIN/PASSWORD/FILIAL_ID (works since 2026-10-01). Most staff are on
  "эркин график" (come when needed / when the Director calls): Admin Bot **`/grafik`** marks them by
  Verifix id (2026-10-03) — never late/absent, the brief only counts them, KPI doesn't penalize them.
- BILLZ (shop tills, read-only, `integrations/billz/`, `docs/agent-specs/15-billz.md`): brief block,
  Q&A agent `billz_savdo`, Admin `/billz`. `BILLZ_SECRET_TOKEN` is set in Render and works
  (one shop sells: GARMIN ABAY) — don't list it as missing.
- **Billz → SAP check** (2026-10-03, `docs/agent-specs/17-billz-sap-check.md`): 08:00, cheque by cheque —
  not entered / wrong amount / entered late / in SAP but not Billz. The shop enters one A/R invoice per
  cheque (warehouses G.A._01, 05; SAP user "Гармин (филиал Абай)"), often a day or more late.
- **Reports are designed pages** (2026-10-05, `docs/agent-specs/18-reports.md`): the 08:00 brief is
  one PNG picture (Telegram photo); cash calendar, KPI table, Billz → SAP (when wrong) and data
  quality are PDFs — Jinja2 templates in `integrations/reports/templates/` → WeasyPrint → pypdfium2.
  Light page, red only for what needs the Director, drawn icons (no emoji), Uzbek Cyrillic; the text
  is the fallback. Lead Agent and receivables messages stay text. Website/QR untouched.
- AI: OpenRouter only (`integrations/ai/openrouter_client.py`).
- SAP is B1 on **HANA, schema `MGM`**; local currency USD, system currency UZS. Data arrives only by
  push from the gateway machine (`scripts/sap-gateway-push/push-ar-aging.ps1`) **through the
  gateway's fixed tools** — never direct DB access or HANA credentials (the gateway owner's rules,
  `SAP_B1_AI_AGENT_TEACHING_UPDATED.md`; Abdulbosit (IT) runs it). Today's tools: 100-row cap →
  "камида". New data = a new gateway tool: `docs/sap-gateway-tools.md` specifies
  `get_open_invoices`, `get_sales_by_date`, `get_stock_value`, `get_supplier_balances`
  (→ `/webhooks/sap-data/...`); the script uses each as soon as it exists.
- **The gateway's source is in this repo: `sap-gateway/`** (Node.js; the owner got admin access
  2026-10-09). Edit there, then copy `src/` to the gateway computer and restart `npm start`. Its SQL
  is checked against SAP's columns by selfcheck (`test_sap_gateway_code`); values are bound
  parameters (`exec(sql, params)`). No Node on this PC — JS is syntax-checked with a parser only.
- Schema self-applies on startup (`database/schema.sql`, idempotent ALTERs).

Status of the owner's 21-agent plan: `docs/agents-status.md` (running: H0, A1,
A2, A3, A4, B1, B2, B4, E1, F2 — the Lead Agent, resumed 2026-09-30). Goal:
~10–15 working agents, not all at once.

## Decisions already made (don't re-suggest)
- Business name is **Londry** (Primus Londry), not "Laundry".
- Render plans are paid and settled: mgmg-api starter (0.5c-512mb), mgmg-db
  basic-256mb (0.1c). Never raise plans, cold starts or DB expiry again.
- QR cards: red background, the business's logo in white on top, the code on
  a white square — no text. Logos: `integrations/org_bot/logos/`.
- No company domain for the QR page — Render's `mgmg-api-eeky.onrender.com` stays.
- Complaints only (no "fikr"), **two pages, two QR codes**: `/f` = Londry
  (already printed — never move it), `/f/garmin` = Garmin. The only choice on
  the page is the **branch** (required, 2026-10-04): Londry Юнусобод /
  Вузгородок, Garmin Абай / Минор (`feedback.BRANCHES`; `?branch=` preselects).
  Complaints are **tickets for the department, not the Boss** (2026-10-13, `routes.py`): Londry → Londry
  role, Garmin → Garmin сотув; ✅ Ҳал бўлди + reason code; "ҳал бўлдими?" hourly 09–20, max 3. No Uzbek Latin.
  "Director" = the owner = the Boss (2026-10-13). Positions = the roles people register with.
  **Garmin complaints → the Garmin manager** (2026-10-14, role `garmin_manager`, not Garmin сотув).
- **Garmin manager** (2026-10-14, `docs/agent-specs/23-garmin-manager.md`): every Garmin complaint; Garmin's SAP
  (`garmin_sap`, its warehouses only) + Billz + Garmin data by role (`data_access.ROLE_SCOPES`, never 1C) — **opens
  with the role, no Boss confirmation** (the owner, 2026-10-14; the Boss is told); Garmin morning brief (`agents/garmin-brief/`,
  same picture as the Director's, shared `integrations/reports/brief_parts.py`); Billz → SAP check recipient.
  **Sends no daily report** (2026-10-17, `kpi.NO_REPORT_ROLES`); /hisobotlar = his team's report calendar.
  **Competitors** (2026-10-19, `integrations/garmin/competitors.py`, spec 26): Thursdays 08:00 + `/raqobat` — OLX.uz
  listings and Uzbek shops' pages (Google gl=uz) vs our garmin.com.uz prices by model; private sellers never named.
  Shops' contacts (2026-10-20, `competitor_shops.py`): name, phones, Instagram, Telegram, site, address — only what the
  shop publishes (OLX shop page, site, Telegram channel; Instagram by Google name search). Private persons' phones: never.
  Mobile app: not now.
- **Google reviews: separate pages and QR codes** (2026-10-08, `docs/agent-specs/20-google-reviews.md`):
  `/r` Londry, `/r/garmin` Garmin — the branch button opens that branch's Google review page
  (`GOOGLE_REVIEW_URLS`, Google https hosts only). `/f` stays complaints only (red). Reviews are
  **green** (positive, never the complaint red), gold stars. Honest: no rating asked first, everyone
  gets the same link (no review gating). Cards: `/qr sharh` — green card, logo, five white stars.
- **Friendly voice** (2026-09-30) for motivation and daily-report
  messages: one short lowercase sentence, no line breaks, no bold, **no emoji**
  (the owner, 2026-10-19 — it used to end with one), addressed "Алишер ака" (ака/опа set by the admin, never
  guessed). **Names always keep their capital** — person, company
  (2026-10-01). **Always polite "сиз", never "сен"** or -сан/-санг/-динг forms,
  extra courtesy to women; `tone.is_polite` rejects AI text that breaks it. Like the owner's example: "ака, ишларингиз билан чарчамаяпсизми,
  илтимос ҳисобот ёзиб юборинг, раҳмат каттакон, чарчаманг". No jokes/anecdotes
  (deleted 2026-10-02). Tasks keep their card format. `integrations/org_bot/tone.py`.
- **OPS Manager Bot is for work only** (2026-10-02): **no text messages between
  employees and the Director (either way) through the bot** — the relay is
  deleted. **Files are the exception**: a photo/video/file is asked "what is
  it for?" (бугунги ҳисобот / директорга / бекор); «директорга» is forwarded as it is, unread. **Report files
  (2026-10-22, `report_media.py`):** a voice / video marked «бугунги ҳисобот» is turned into report text by the
  AI (the person is shown it); a photo / document is kept unread; report files wait with the report and go to
  whoever asks for that person's report (`analyst.employee_report`: the Director anyone's, the Garmin manager his
  team's) — no longer pushed at once. **Reports aren't guessed during the day** (any typed message while it's
  owed is confirmed), **but at 22:00** an owed report is put together from the person's own unconfirmed messages
  of the day (held text, files, work-AI messages — `report_close.build_from_chat`, «[чатдан тузилди]», no minus;
  the owner: "human error"); **/hisobot** changes/deletes today's
  report only. A message with «?» in the report window goes to the AI, never into the report (2026-10-17). Daily reports can be switched off per person (📝, 2026-10-06:
  never asked, never counted as missed). Employees' messages about their work go to the **work AI**
  (`docs/agent-specs/15-ai-chat.md`): it knows their role, own open tasks and
  (once uploaded) `employees.responsibilities`; **honesty first —
  "билмайман", never a guess**; no company data, no other people; Garmin sales
  get the public catalog; 60/day; admin can switch it off per person (🤖).
- **Leads sheet is edited by people** (2026-10-02): share it from Google
  Sheets yourself (Editor); the bot reads/writes columns **by header name**,
  so moved/added columns are fine — don't rename the header cells.
- **Days off**: Admin Bot `/dam` marks a day off (holiday) — no report asks,
  cheer or task reminders that day. **Announce**:
  `/elon` (no text = "техник хатолик юз берди…" notice) or `/elon <text>`,
  preview + confirm, goes to every active employee once (2026-10-01).
- **Report bonus** (2026-10-22, `agents/report-bonus/`): 08:00, Garmin сотув — yesterday's report sent by them → +1 KPI
  (polite thanks + month total); missed / «[чатдан тузилди]» → politely no point. KPI = parts + bonus − minus (≤ 100).
- Daily reports accepted only until **22:00** of that day (the Director, 2026-10-15); one not in by then
  costs `REPORT_MISS_PENALTY` (1) KPI points and the person is told (`report_close.py`, from 16.10); one follow-up
  question max for vague reports; a question during the report window gets
  "ҳисоботми ёки савол?".
- **Cheque photos → accounting** (2026-10-14, `integrations/org_bot/cheques.py`, spec 23): the AI reads a
  photo **the Director** sends (the one exception to "the AI never reads files" — employees' files stay
  unread). A cheque with all eight details (фискал белги, ФМ рақами, чек рақами, сана, RRN, сумма, товар,
  контрагент ИНН) goes straight to Бухгалтерия (`CHEQUE_RECIPIENT_ROLES`) as the photo + details; missing /
  wrong / already sent → he is asked (📨 / 📋 not a cheque / ❌). Never guessed; counts-only logs.
- **Director's tasks are confirmed before sending** (2026-10-02): a card lists
  everyone as tick-boxes with the bot's guess ticked; nothing goes out before
  📨 Юбориш (`task_picker.py`, `task_drafts`). Files too.
- **Task register** (2026-10-13, the owner's spec «EMJIEM AI ТЕХ ЗАД» of 11.10, Day 1; spec in
  `docs/agent-specs/08-task-tracker.md`): code T-YYYY-MM-NNNN, states sent/started/blocked/review/done/
  cancelled, ✅ needs proof → the assigner accepts or returns; ⛔ with reason; `task_events` history.
  Proof and blocker text reach the assigner (task data — the one exception to the no-relay rule).
- **PULSE** (2026-10-22, spec 28, `integrations/pulse/`, `/pulse`): the Boss's live dashboard — index over sales, six
  tickers, today's tape, people ranked, one person's day; /pulse link for the Director (all) and the Garmin manager (team).
  Daily score = attendance 40 + report 30 + tasks 30. Design system: PRODUCT.md / DESIGN.md (impeccable skill).
  **Look 2026-10-23** (the owner's reference): navy analytics dashboard, dark + white themes (DESIGN.md «PULSE screen»).
  **Speed:** sources read in parallel, Verifix/Billz read shared, kept copy served then rebuilt, warm 08–21, gzip, WOFF2.
- **Explanations** (2026-10-22, the Boss, `explanations.py`, spec 27): 08:00 after the brief — absent yesterday in
  Verifix (with the days in a row) or report not sent → asked why; the answer goes to the Director (+ the Garmin
  manager for his team) as a card — the second exception to the no-relay rule. **/grafik 🕘** = a person's own
  start time (late counted from it).
  Next from the spec: positions + deputies, route table, escalation; then Day 2 complaint tickets.
- **Removal is a clean end; re-registration a fresh start** (2026-10-14): `store.revoke_employee` deletes the work-AI
  memory, closes held messages/files, cancels open tasks, clears data access; `create_employee` on a revoked row
  clears the name (asked again), duties, settings and language. One person's AI questions are answered one at
  a time, each as a reply to its question.
- Signatures on the SOP permission form are by hand; never use Telegram profile names.
- In-house CRM, amoCRM, MS Planner/Teams: not used (removed).
- **IT sees no company figures** (the Director's order of 07.10.2026, «Маълумотларга кириш
  ҳуқуқларини чеклаш»): nothing sent to the admin / Admin Bot carries amounts, customers,
  other people's attendance or the Director's messages — `/billz` `/1c` `/verifix` are status
  only, Billz → SAP trial is counts only, data quality has no amounts, `/db` opens the bot's own
  tables (the Boss approved, 17.10; access-review §4) — SAP / 1C / Billz / Verifix tables show their structure only (open to IT until 31.10.2026 to fix the briefs, the Boss 17.10),
  logs keep no question/data/answer text. IT gets `tech_report.py` instead
  (08:00 + `/texnik`). Table for the Director: `docs/access-review-2026-10-07.md`.
- **The Director's questions go to the analyst** (2026-10-07, `docs/agent-specs/19-director-analyst.md`):
  the AI looks things up itself with read-only tools (SAP pushed data, 1C balances/turnovers by
  counterparty, BILLZ, Verifix, the bot's records) and **never asks "which system?"**; debt = SAP
  and 1C both. No tool can write; the old one-source answer is the fallback. Supplier debt 1C ↔ SAP
  (2026-10-10, `integrations/onec/payables.py`, spec 21): a short text comparison; with «солиштир» /
  «excel» / «файл» the bot also sends the `.xlsx` workbook (built on the server).
- **Accounting and finance get company data through the bot** (2026-10-10, the Director;
  `docs/agent-specs/22-data-access.md`): the admin gives SAP / 1C / Billz per person on `/xodimlar`,
  only to Бухгалтерия / Молия; the Director is told each time and has ⛔ to close it; their questions
  go to the analyst with only those systems' tools — never attendance or the bot's own records.
- **The bot knows the company and asks when unsure** (2026-10-10): `integrations/org_bot/knowledge.py`
  (businesses, branches, departments, which system holds what, the Director's words — facts only,
  no figures) goes into every OPS Manager prompt; keep it current when the business changes. Not
  understood → `target_type="clarify"`: one question + 2–3 meanings as buttons
  (`director_clarifications`); never asks "which system?" or for a period.

## Open items
- **Brief vs SAP check (15.10, `docs/sap-data-check-2026-10-15.md`):** delete the old duplicate push task
  (:17/:47 — it re-wrote 17 debt rows and made the brief say «камида»; now ignored in code); deploy `sap-gateway/src/` (stock value fix + 12 complete tools, 16.10) and the new `push-ar-aging.ps1` (keep its settings; no log file);
  accounting to decide what «касса / банк» counts (today: every class-5000 account incl. 5530/5710/5720) and
  whether «Кечаги сотув» shows so'm by entry date; Garmin bot leads never arrive (check its webhook secret).
- **The owner's IT instruction** (08.10–31.12.2026, received 10.10): phases Ф0–Ф3, 10 rules, 8 agents,
  А1–А8 — mapped in `docs/it-instruction-2026-10.md` with the owner's open decisions (personal data
  on a server in Uzbekistan + masking before AI — **not met today**; AmoCRM; who gets PULSE; n8n or
  this service; Microsoft 365). Three businesses: GARMIN, PRIMUS (B2B equipment), LONDRY
  (self-service laundries, tokens/cash). All links: `docs/links.md`.
- SAP: push every 30 min as Windows task "MGMG SAP push" (`install-task.ps1`). The missing columns
  (so'm totals, `SlpName`, `CreateDate`/`CreateTS`, `ObjType`, `CodeBars`, credit notes, entered-late
  sales) and `get_supplier_balances` are written in `sap-gateway/` (2026-10-09); supplier balances
  **arrive** (09.10, every column). An older duplicate Windows task also runs at :17/:47 — delete it. Billz → SAP check is on **trial** (results to the admin) until the owner
  confirms them, then `BILLZ_SAP_CHECK_TRIAL=false`. Admin to tick "эркин график" people in `/grafik`.
- **Backups (IT-5, due 24.10):** code done 2026-10-10 (`scripts/backup/README.md`). To do by hand: the
  owner makes the GnuPG key (Kleopatra); set `BACKUP_SECRET` + `BACKUP_PUBLIC_KEY` in Render; install
  `pull-backup.ps1` + the task on an office computer with a second disk; restore test with the owner.
- **Gateway update (21.10.2026):** copy `sap-gateway/src/` to the gateway computer and restart — customers'
  `CreateDate`, service calls' `TechnicianName` / `AssigneeName` (joins OHEM / OUSR: if `service_calls` stops
  arriving, the gateway's HANA user lacks SELECT on them). Customers' phone / address: the owner to decide (personal
  data rule — server in Uzbekistan + masking before AI, not met today).
- **1C OData «410 Gone» (20.10.2026):** the web client works, OData doesn't — re-enable the database's OData publication in Clobus.
- `GOOGLE_REVIEW_URLS` not set yet (each branch's Google Business "Get more reviews" link).
- `PERMISSION_APPROVAL_TIERS` not set. Employees' written duties (SOPs) not given yet.
