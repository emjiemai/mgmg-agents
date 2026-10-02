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
  = team cheer 10:00 / 14:00 / 17:35 (answers by button only, never shown to
  the Director) + lead check-in 15:00. Daily; weekends only for employees
  marked as weekend workers.
- **Leads to B2B sales** (2026-09-30, `docs/agent-specs/14-lead-handout.md`):
  08:00 one lead per B2B Sotuv person (new first, then best unworked older;
  all tracks), 15:00 жараёнда / рад этилди / бажарилди + one question; open
  leads re-asked daily and pile up by design; KPI counts them inside the
  existing parts.
- KPI (the Director's 15 criteria → 6 parts, `docs/agent-specs/13-kpi.md`): `/maqsad` `/maqsadlar`
  `/kpi` `/baho` (Director), `/kpi` `/natija` (employees); ratings on the 1st, final by the 5th.
- Garmin AI bot (separate repo emjiemai/Garmin-AI-bot, cloned at D:\Garmin-AI-bot) POSTs its leads to
  `/webhooks/garmin-lead/{GARMIN_LEADS_SECRET}` → table `garmin_leads`; Q&A agent `garmin_lidlar`.
- 1C «Бухгалтерия для Узбекистана» on Clobus, OData read-only (`integrations/onec/`, `docs/agent-specs/16-1c.md`):
  Admin `/1c` check; brief «💰 Касса» = 1C class-5000 balances (bank / нақд); Q&A `pul_qoldigi`.
- Verifix logs in with VERIFIX_LOGIN/PASSWORD/FILIAL_ID (works since 2026-10-01).
- BILLZ (shop tills, read-only, `integrations/billz/`, `docs/agent-specs/15-billz.md`): brief block,
  Q&A agent `billz_savdo`, Admin `/billz`; waits for `BILLZ_SECRET_TOKEN` in Render.
- AI: OpenRouter only (`integrations/ai/openrouter_client.py`).
- SAP data arrives only by push (row-capped → "камида" lower bounds).
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
  (already printed — never move it), `/f/garmin` = Garmin. No choice buttons
  on the page. Complaints reach OPS Manager Bot marked 🔴. No Uzbek Latin.
  Mobile app: not now.
- **Friendly voice** (2026-09-30) for cheer, motivation and daily-report
  messages: one short lowercase sentence, no line breaks, no bold, one emoji
  only at the very end, addressed "Алишер ака" (ака/опа set by the admin, never
  guessed). **Names always keep their capital** — person, company, Афанди
  (2026-10-01). **Always polite "сиз", never "сен"** or -сан/-санг/-динг forms,
  extra courtesy to women; `tone.is_polite` rejects AI text that breaks it. Like the owner's example: "ака, ишларингиз билан чарчамаяпсизми,
  илтимос ҳисобот ёзиб юборинг, раҳмат каттакон, чарчаманг". 14:00 cheer is an
  Afandi latifa. Tasks keep their card format. `integrations/org_bot/tone.py`.
- **Days off**: Admin Bot `/dam` marks a day off (holiday) — no report asks,
  cheer, leads, 15:00 lead questions or task reminders that day. **Announce**:
  `/elon` (no text = "техник хатолик юз берди…" notice) or `/elon <text>`,
  preview + confirm, goes to every active employee once (2026-10-01).
- Daily reports accepted only until midnight of that day; one follow-up
  question max for vague reports; employee → Director messages need confirmation.
- **Director's tasks are confirmed before sending** (2026-10-02): a card lists
  everyone as tick-boxes with the bot's guess ticked; nothing goes out before
  📨 Юбориш (`task_picker.py`, `task_drafts`). Files too.
- Signatures on the SOP permission form are by hand; never use Telegram profile names.
- In-house CRM, amoCRM, MS Planner/Teams: not used (removed).

## Open items
- Verifix (A4) built, waiting for `VERIFIX_CLIENT_ID`/`VERIFIX_CLIENT_SECRET`
  (see `docs/agent-specs/11-attendance.md`); check with Admin Bot `/verifix`.
- `PERMISSION_APPROVAL_TIERS` not set; SAP push cap (100 rows) limits stock/sales agents.
