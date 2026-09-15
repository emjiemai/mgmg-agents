# MGMG Digital Command Center

Central AI operating system for MGMG (Tashkent) — connects SAP Business One,
the in-house CRM, Google Sheets, and Telegram into one hub where the
CEO/Operations Director sees everything, agents handle the repetitive work,
and two Telegram bots turn plain-language instructions into tracked,
dispatched tasks.

**Business lines:** Primus Laundry (ONDRY — industrial laundry equipment) ·
Garmin watch retail. Also referenced in older division mappings: Armin ·
IMUS-Alliance · Service center · Properties.

## Status

| # | Deliverable | State |
| - | ----------- | ----- |
| 1 | Infrastructure (Render Blueprint: API + Postgres + 1 cron job) | deployed — see `render.yaml` |
| 2 | CEO Daily Brief | built, runs on schedule |
| 3 | Receivables | built, runs on schedule |
| 4 | Lead Agent (Primus Laundry B2B sourcing) | built, runs on schedule |
| 5 | Admin Bot + OPS Manager Bot (`integrations/org_bot/`) | live — see `docs/agent-specs/05-org-bot.md` |
| 6 | Power BI Dashboard v1 | queries + DAX ready, report not built |

amoCRM, Verifix (attendance) and Microsoft Planner/Teams were removed from the
project on 2026-09-15 — none of them were in use. Their old database tables
are left in place with their history; nothing writes to them anymore.

Every `[PLACEHOLDER]` in `.env`/Render's `mgmg-shared` env group must be filled
before the agent that needs it will run — each one refuses to start while its
own placeholders remain. A known example: while `CRM_API_KEY` is a
placeholder, the CRM pipeline snapshot (`v_pipeline_latest`) never gets
populated, and anything reading it (including OPS Manager Bot's CRM agent
query) reports "no data" rather than failing silently.

## Architecture

```
SAP Business One ─┐
In-house CRM ──────┼─→ Python integration clients ─→ PostgreSQL ─→ Power BI (dashboard)
Google Sheets ──────┘         │
                               └─→ Telegram — briefs and alerts
                                        └─→ Admin Bot + OPS Manager Bot
                                             (employee onboarding, AI task routing —
                                              see docs/agent-specs/05-org-bot.md)

mgmg-api      — always-on FastAPI web service (integrations/api/app.py): both
                org_bot bots' webhooks and the SAP gateway pushes
1 cron job    — mgmg-morning-agents (CEO brief + Lead Agent + receivables,
                run back to back daily via scripts/run_morning_agents.py)
```

n8n is referenced in some older docs/history but is **not** part of the
current deployment (see the header comment in `render.yaml`) — every agent
talks to SAP/CRM/Sheets/Telegram directly from Python.

Money is stored as integer **tiyin** (1 UZS = 100 tiyin) everywhere. Time is
stored in **UTC** and displayed in **Asia/Tashkent**. Both rules are enforced in
`integrations/common/money.py` and `integrations/common/timeutil.py`.

### AI providers

Every AI call goes through `integrations/ai/openrouter_client.py`, which
supports two OpenAI-compatible providers (OpenRouter and DeepSeek):
- **Lead Agent** uses the global `AI_PROVIDER`, currently `openrouter` with
  `google/gemini-3.8-flash` (fallback `google/gemini-3.7-flash`).
- **OPS Manager Bot** has its own switch (`OPS_MANAGER_BOT_PROVIDER`,
  `OPS_MANAGER_BOT_MODEL`, `OPS_MANAGER_BOT_FALLBACK_MODELS`), currently the
  same OpenRouter + Gemini 3.8 Flash setup, independent of `AI_PROVIDER` via
  `provider_override`/`model_override` on the client.
- DeepSeek stays supported as a one-line switch back (`deepseek` provider).

## Security model

| Rule | How it is enforced |
| ---- | ------------------ |
| 1. Read-only for SAP and the CRM | The SAP client has no write path at all, and the CRM API key has no write scope. Other agent writes (e.g. the Lead Agent's Google Sheet) are gated behind `AGENT_WRITES_ENABLED`; while closed, every intended write is audited as `dry_run` and nothing is sent. |
| 2. Everything audited | `integrations/common/db.audited()` wraps every external call; rows land in `agent_actions`. Audit failures are logged, never silently swallowed. |
| 3. No hardcoded secrets | All credentials come from `.env`/Render's env group through `integrations/common/config.py`, wrapped in `SecretStr` so they cannot leak into logs or tracebacks. |
| 4. Human in the loop | No agent takes autonomous action against an external system: the scheduled agents report, and org_bot only acts on what a person explicitly sent or tapped. |
| 5. Least privilege | One service account per system; the SAP user is read-only in SAP itself, and a separate `powerbi` Postgres role has `SELECT` only. |
| 6. org_bot is the one deliberate exception to rule 1 | Admin Bot and OPS Manager Bot write directly to Postgres/Telegram (task status, employee registration) with no `AGENT_WRITES_ENABLED` gate — those tables aren't touched by any other agent. Every write is still a direct, bounded reflection of something a human explicitly did (a task the Director sent, a button an employee tapped), never an autonomous decision the model made — see `docs/agent-specs/05-org-bot.md`'s "On write access" section. |

## Setup

### Production — Render Blueprint

```bash
git push origin main   # then in the Render dashboard: New -> Blueprint, point at this repo
```

`render.yaml` deploys `mgmg-db` (Postgres), `mgmg-api` (the always-on web
service — both org_bot bots and the SAP gateway pushes), and one cron service
(`mgmg-morning-agents`: CEO brief, Lead Agent, receivables). Secrets marked
`sync: false` are entered once in the Render dashboard under the `mgmg-shared`
environment group, not committed to this repo — every service reads from that
one group.

Register both org_bot webhooks once (see `docs/agent-specs/05-org-bot.md` for
the exact URLs).

### Local development

```bash
cp .env.example .env   # edit .env
docker compose up -d   # PostgreSQL (schema applied automatically) + the FastAPI app
```

Check what's still missing:

```bash
python -c "from integrations.common.config import settings; print(settings.missing_placeholders())"
```

Applying the schema to an existing database by hand:

```bash
psql -U <user> -d mgmg -f database/schema.sql
```

### Verify before going live

```bash
python scripts/selfcheck.py                          # offline logic checks, no credentials needed
python agents/ceo-daily-brief/agent.py --dry-run     # real data, nothing sent
python agents/receivables/agent.py --dry-run
python agents/lead-agent/agent.py --dry-run
```

## Layout

```
agents/                      scheduled, cron-run agents (one process per run, then exit)
  ceo-daily-brief/           morning brief
  receivables/               AR aging alert
  lead-agent/                B2B lead sourcing (Primus Laundry)
integrations/
  api/                       FastAPI webhook receiver (mgmg-api's entry point)
  common/                    config, logging, DB + audit, retrying HTTP, money, time
  sap/                       SAP Business One client (read-only) + gateway push handler
  crm/                       MGMG's own CRM client (read-only)
  org_bot/                   Admin Bot + OPS Manager Bot — see docs/agent-specs/05-org-bot.md
  ai/                        multi-provider LLM client (OpenRouter / DeepSeek)
  google/                    Google Sheets client (Lead Agent's data store)
  search/, tenders/          Lead Agent's search and tender sources
  telegram/                  bot primitives — send, edit, HTML sanitization
database/                    schema.sql — self-applying, no separate migration tool
dashboard/powerbi-queries/   SQL sources, DAX measures, build guide
scripts/                     selfcheck, setup scripts, crontab
docs/agent-specs/            what each agent/bot does, and its runbook
render.yaml                  production deployment (Render Blueprint)
docker-compose.yml           local development
```

## Conventions

- Python 3.11+, `httpx` (async), `pydantic`/`pydantic-settings`, `loguru`
- Every external call retries 3× with exponential backoff and jitter
- Every function has a docstring stating what it does, what it returns, and what it can fail on
- Agents degrade rather than crash: one dead source never blocks the whole brief
- No migration framework: schema changes are `ALTER TABLE ... IF NOT EXISTS`
  statements appended to `database/schema.sql`, re-applied safely on every boot
- Comments and docstrings state the *why*, not the *what* — code that needs a
  comment to explain what it does gets rewritten instead

## Before first production run

These need real-world values that cannot be guessed from here:

- [ ] `CASH_ACCOUNT_CODES` and `BANK_NAME_BY_ACCOUNT` — `integrations/sap/client.py`
- [ ] Division mappings — `integrations/common/divisions.py`
- [ ] Reconcile one day of AR output against SAP's own aging report
- [ ] `CRM_API_KEY` — the CRM pipeline snapshot silently stays empty until
      it's filled in
- [ ] OpenRouter account balance — both Lead Agent and OPS Manager Bot
      (`google/gemini-3.8-flash`) fail with HTTP 402 if the account runs out
      of credits; DeepSeek (`AI_PROVIDER=deepseek` /
      `OPS_MANAGER_BOT_PROVIDER=deepseek`) is a separately-billed path to
      switch back to if that happens
