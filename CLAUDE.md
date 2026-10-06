# CLAUDE.md — read this first (any machine)

## Who I am
Ulugbek (GitHub Ulugbek220907), IT specialist / AI agent builder at Primus
Londry (MGMG / ЭМЖИЕМ). I build and run this system alone. I switch between a
PC and a laptop — this file is the shared context; chat history is not.

## How to work with me
- **Don't guess — verify.** Read the code/docs/API before claiming anything.
- **Concise answers.** Results first, short.
- **Every bot and user-facing text is Uzbek Cyrillic** (names too: Latin → Cyrillic).
  Code comments, docs, commit messages: English.
- **After each fix: run checks, then commit and push** (authorized). Commit only
  if `PYTHONIOENCODING=utf-8 python scripts/selfcheck.py` exits 0 (don't judge by
  piped/tailed output). Also `python -m pyflakes integrations agents scripts`.
- Add tests to `scripts/selfcheck.py` for every change; update the matching
  `docs/agent-specs/*.md` and `docs/agents-status.md`.
- On Windows, never pipe Cyrillic through stdin heredocs — write patch
  scripts/files with the editor instead.

## What this is
MGMG Digital Command Center: Python + FastAPI + PostgreSQL on Render
(`render.yaml`, env group `mgmg-shared`, secrets `sync: false`).
- **mgmg-api** (Starter plan, always on): Admin Bot (admin) + OPS Manager Bot
  (employees + Director) webhooks, SAP gateway push receiver, `/db` read-only
  viewer, `/f` client complaints page (Uzbek Cyrillic, Russian, English).
- **Crons:** 08:00 `scripts/run_morning_agents.py` (brief, Lead Agent, lead
  hand-out, receivables, task tracker, data quality Mon, cash calendar Mon,
  monthly KPI 1st); 16:00 daily-reports ask; 17:00 reminder + Friday
  scorecard; daytime job `mgmg-team-cheer` (`run_morning_agents.py --daytime`)
  = team cheer 10:00 / 17:35 (answers by button only, never shown to the
  Director; the 14:00 joke was deleted 2026-10-02) + lead check-in 15:00. Daily; weekends only for employees
  marked as weekend workers.
- **Leads to B2B sales** (2026-09-30, `docs/agent-specs/14-lead-handout.md`):
  08:00 one lead per B2B Sotuv person (newest first — "from the end" of the
  sheet; all tracks; sheet: "POSSIBLE Leads", tab Sheet1), 15:00 жараёнда / рад этилди / бажарилди + one question; open
  leads re-asked daily and pile up by design; KPI counts them inside the
  existing parts.
- KPI (the Director's 15 criteria → 6 parts, `docs/agent-specs/13-kpi.md`): `/maqsad` `/maqsadlar`
  `/kpi` `/baho` (Director), `/kpi` `/natija` (employees); ratings on the 1st, final by the 5th.
- Garmin AI bot (separate repo emjiemai/Garmin-AI-bot, cloned at D:\Garmin-AI-bot) POSTs its leads to
  `/webhooks/garmin-lead/{GARMIN_LEADS_SECRET}` → table `garmin_leads`; Q&A agent `garmin_lidlar`.
- 1C «Бухгалтерия для Узбекистана» on Clobus, OData read-only (`integrations/onec/`, `docs/agent-specs/16-1c.md`):
  Admin `/1c` check; brief «💰 Касса» = 1C class-5000 balances (bank / нақд); Q&A `pul_qoldigi`.
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
  `get_open_invoices`, `get_sales_by_date`, `get_stock_value` (→ `/webhooks/sap-data/...`); the
  script uses each as soon as it exists.
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
  Complaints reach OPS Manager Bot marked 🔴 with the branch. No Uzbek Latin.
  Mobile app: not now.
- **Friendly voice** (2026-09-30) for cheer, motivation and daily-report
  messages: one short lowercase sentence, no line breaks, no bold, one emoji
  only at the very end, addressed "Алишер ака" (ака/опа set by the admin, never
  guessed). **Names always keep their capital** — person, company
  (2026-10-01). **Always polite "сиз", never "сен"** or -сан/-санг/-динг forms,
  extra courtesy to women; `tone.is_polite` rejects AI text that breaks it. Like the owner's example: "ака, ишларингиз билан чарчамаяпсизми,
  илтимос ҳисобот ёзиб юборинг, раҳмат каттакон, чарчаманг". No jokes/anecdotes
  (deleted 2026-10-02). Tasks keep their card format. `integrations/org_bot/tone.py`.
- **OPS Manager Bot is for work only** (2026-10-02): **no text messages between
  employees and the Director (either way) through the bot** — the relay is
  deleted. **Files are the exception**: a photo/video/file is asked "what is
  it for?" (бугунги ҳисобот / директорга / бекор) and forwarded to the Director
  as it is — the AI never reads files. **Reports are never guessed** (any typed
  message while it's owed is confirmed); **/hisobot** changes/deletes today's
  report only. Cheer can be switched off per person (/xodimlar → 💬). Employees' messages about their work go to the **work AI**
  (`docs/agent-specs/15-ai-chat.md`): it knows their role, own open tasks, own
  leads and (once uploaded) `employees.responsibilities`; **honesty first —
  "билмайман", never a guess**; no company data, no other people; Garmin sales
  get the public catalog; 60/day; admin can switch it off per person (🤖).
- **Leads sheet is edited by people** (2026-10-02): share it from Google
  Sheets yourself (Editor); the bot reads/writes columns **by header name**,
  so moved/added columns are fine — don't rename the header cells.
- **Days off**: Admin Bot `/dam` marks a day off (holiday) — no report asks,
  cheer, leads, 15:00 lead questions or task reminders that day. **Announce**:
  `/elon` (no text = "техник хатолик юз берди…" notice) or `/elon <text>`,
  preview + confirm, goes to every active employee once (2026-10-01).
- Daily reports accepted only until midnight of that day; one follow-up
  question max for vague reports; a question during the report window gets
  "ҳисоботми ёки савол?".
- **Director's tasks are confirmed before sending** (2026-10-02): a card lists
  everyone as tick-boxes with the bot's guess ticked; nothing goes out before
  📨 Юбориш (`task_picker.py`, `task_drafts`). Files too.
- Signatures on the SOP permission form are by hand; never use Telegram profile names.
- In-house CRM, amoCRM, MS Planner/Teams: not used (removed).

## Open items
- SAP: the three gateway tools work — push resumed 2026-10-06, every 30 min as Windows task
  "MGMG SAP push" (`install-task.ps1`), all kinds 200. Still missing columns (so'm totals
  `DocTotalFC`/`PaidFC`/`DocTotalSy`, `SlpName`, `CreateDate`/`CreateTS`) — ask Abdulbosit,
  list in `docs/sap-gateway-tools.md` "As built vs this spec". Billz → SAP check is on **trial** (results to the admin) until the owner
  confirms them, then `BILLZ_SAP_CHECK_TRIAL=false`. Admin to tick "эркин график" people in `/grafik`.
- `PERMISSION_APPROVAL_TIERS` not set. Employees' written duties (SOPs) not given yet.
