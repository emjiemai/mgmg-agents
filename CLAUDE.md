# CLAUDE.md — read this first (any machine)

## Who I am
Ulugbek (GitHub Ulugbek220907), IT specialist / AI agent builder at Primus
Laundry (MGMG / ЭМЖИЕМ). I build and run this system alone. I switch between a
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
  viewer, `/f` client feedback page (4 languages).
- **Crons:** 08:00 `scripts/run_morning_agents.py` (brief, receivables, task
  tracker, data quality Mon, cash calendar Mon, monthly KPI 1st);
  16:00 daily-reports ask; 17:00 reminder + Friday scorecard. Daily; weekends
  only for employees marked as weekend workers.
- AI: OpenRouter only (`integrations/ai/openrouter_client.py`).
- SAP data arrives only by push (row-capped → "камида" lower bounds).
- Schema self-applies on startup (`database/schema.sql`, idempotent ALTERs).

Status of the owner's 21-agent plan: `docs/agents-status.md` (built: H0, A1,
A2, A3, A4, B1, B2, B4, E1). Goal: ~10–15 working agents, not all at once.

## Decisions already made (don't re-suggest)
- No company domain for the QR page — Render's `mgmg-api-eeky.onrender.com` stays.
- One universal feedback QR code (no locations). Mobile app: not now.
- Daily reports accepted only until midnight of that day; one follow-up
  question max for vague reports; employee → Director messages need confirmation.
- Signatures on the SOP permission form are by hand; never use Telegram profile names.
- In-house CRM, amoCRM, MS Planner/Teams: not used (removed).

## Open items
- Verifix (A4) built, waiting for `VERIFIX_CLIENT_ID`/`VERIFIX_CLIENT_SECRET`
  (see `docs/agent-specs/11-attendance.md`); check with Admin Bot `/verifix`.
- `mgmg-db` is declared `plan: free` — Render deletes free Postgres after 30
  days; confirm the real plan in the dashboard.
- `PERMISSION_APPROVAL_TIERS` not set; SAP push cap (100 rows) limits stock/sales agents.
