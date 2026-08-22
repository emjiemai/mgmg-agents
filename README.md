# MGMG Digital Command Center

Central AI operating system for MGMG (Tashkent) — connects SAP Business One,
the in-house CRM, Verifix, Microsoft 365, Google Sheets, and Telegram into one
hub where the CEO/Operations Director sees everything, agents handle the
repetitive work, and two Telegram bots turn plain-language instructions into
tracked, dispatched tasks.

**Business lines:** Primus Laundry (ONDRY — industrial laundry equipment) ·
Garmin watch retail. Also referenced in older division mappings: Armin ·
IMUS-Alliance · Service center · Properties.

## Status

| # | Deliverable | State |
| - | ----------- | ----- |
| 1 | Infrastructure (Render Blueprint: API + Postgres + 5 cron jobs) | deployed — see `render.yaml` |
| 2 | Agent 1 — CEO Daily Brief | built, runs on schedule |
| 3 | Agent 2 — CRM Follow-up | built, write gate closed |
| 4 | Agent 3 — Receivables | built, runs on schedule |
| 5 | Agent 4 — Lead Agent (Primus Laundry B2B sourcing) | built, runs on schedule |
| 6 | Admin Bot + OPS Manager Bot (`integrations/org_bot/`) | live — see `docs/agent-specs/05-org-bot.md` |
| 7 | Teams Command Center (13 channels + Planner) | script ready, not confirmed run |
| 8 | Power BI Dashboard v1 | queries + DAX ready, report not built |

Every `[PLACEHOLDER]` in `.env`/Render's `mgmg-shared` env group must be filled
before the agent that needs it will run — each one refuses to start while its
own placeholders remain. A known example still open: `CRM_API_KEY` — while
it's a placeholder, the CRM pipeline snapshot (`v_pipeline_latest`) never
gets populated, and anything reading it (including OPS Manager Bot's CRM
agent query) reports "no data" rather than failing silently.

## Architecture

```
SAP Business One ─┐
In-house CRM ──────┼─→ Python integration clients ─→ PostgreSQL ─→ Power BI (dashboard)
Verifix ───────────┤          │
Graph ─────────────┤          ├─→ Telegram — briefs, alerts, approvals
Google Sheets ──────┘         │        └─→ Admin Bot + OPS Manager Bot
                               │             (employee onboarding, AI task routing —
                               │              see docs/agent-specs/05-org-bot.md)
                               └─→ Teams / Planner (tasks)

mgmg-api      — always-on FastAPI web service: webhooks for CRM events,
                Telegram approvals, and both org_bot bots
5 cron jobs   — CEO brief, receivables, CRM follow-up sweep/drain, lead agent
                (see render.yaml; each runs once daily/hourly, then exits)
```

n8n is referenced in some older docs/history but is **not** part of the
current deployment (see the header comment in `render.yaml`) — every agent
talks to SAP/CRM/Verifix/Graph/Sheets/Telegram directly from Python.

Money is stored as integer **tiyin** (1 UZS = 100 tiyin) everywhere. Time is
stored in **UTC** and displayed in **Asia/Tashkent**. Both rules are enforced in
`integrations/common/money.py` and `integrations/common/timeutil.py`.

### AI providers

Two independent LLM providers, switchable per-caller via
`integrations/ai/openrouter_client.py`:
- **DeepSeek** (`AI_PROVIDER=deepseek`) — the global default, used by Lead
  Agent and other scheduled agents (`deepseek-v4-flash`, own API key/billing).
- **OpenRouter** — used by OPS Manager Bot specifically
  (`OPS_MANAGER_BOT_PROVIDER=openrouter`, currently `anthropic/claude-sonnet-5`
  with `google/gemini-3.7-flash` as fallback), independent of the global
  `AI_PROVIDER` switch via `provider_override`/`model_override` on the client.

## Security model

| Rule | How it is enforced |
| ---- | ------------------ |
| 1. Read-only for SAP | The SAP client has no write path at all. CRM/Planner/Teams writes are gated behind `AGENT_WRITES_ENABLED=false`; while closed, every intended write is audited as `dry_run` and nothing is sent. |
| 2. Everything audited | `integrations/common/db.audited()` wraps every external call; rows land in `agent_actions`. Audit failures are logged, never silently swallowed. |
| 3. No hardcoded secrets | All credentials come from `.env`/Render's env group through `integrations/common/config.py`, wrapped in `SecretStr` so they cannot leak into logs or tracebacks. |
| 4. Human approval | `approvals` table + Telegram inline buttons (`TelegramBot.request_approval`). Approvals are idempotent, expire in 24 h, and record who decided. |
| 5. Least privilege | One service account per system; the SAP user is read-only in SAP itself, and a separate `powerbi` Postgres role has `SELECT` only. |
| 6. org_bot is the one deliberate exception to rule 1 | Admin Bot and OPS Manager Bot write directly to Postgres/Telegram (task status, employee registration) with no `AGENT_WRITES_ENABLED` gate — those tables aren't touched by any other agent. Every write is still a direct, bounded reflection of something a human explicitly did (a task the Director sent, a button an employee tapped), never an autonomous decision the model made — see `docs/agent-specs/05-org-bot.md`'s "On write access" section. |

## Setup

### Production — Render Blueprint

```bash
git push origin main   # then in the Render dashboard: New -> Blueprint, point at this repo
```

`render.yaml` deploys `mgmg-db` (Postgres), `mgmg-api` (the always-on web
service — webhooks for CRM, Telegram approvals, and both org_bot bots), and
five cron services (CEO brief, receivables, CRM follow-up sweep + drain, lead
agent). Secrets marked `sync: false` are entered once in the Render dashboard
under the `mgmg-shared` environment group, not committed to this repo — every
service reads from that one group.

Register both org_bot webhooks once (see `docs/agent-specs/05-org-bot.md` for
the exact URLs), and the primary Telegram approval webhook the same way.

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

### Create the Teams structure (optional)

```bash
python scripts/setup/create_teams_structure.py --dry-run
python scripts/setup/create_teams_structure.py
```

Copy the printed `MS_PLANNER_GROUP_ID` and `MS_PLANNER_DEFAULT_PLAN_ID` into `.env`.

### Verify before going live

```bash
python scripts/selfcheck.py                          # offline logic checks, no credentials needed
python agents/ceo-daily-brief/agent.py --dry-run     # real data, nothing sent
python agents/amocrm-followup/agent.py --dry-run     # real data, nothing sent
python agents/receivables/agent.py --dry-run
python agents/lead-agent/agent.py --dry-run
```

## Layout

```
agents/                      scheduled, cron-run agents (one process per run, then exit)
  ceo-daily-brief/           Agent 1 — morning brief
  amocrm-followup/           Agent 2 — stalled-deal follow-up (CRM)
  receivables/               Agent 3 — AR aging alert
  lead-agent/                Agent 4 — B2B lead sourcing (Primus Laundry)
integrations/
  common/                    config, logging, DB + audit, retrying HTTP, money, time
  sap/                       SAP Business One Service Layer client (read-only)
  amocrm/                    CRM client + FastAPI webhook receiver (mgmg-api's entry point)
  org_bot/                   Admin Bot + OPS Manager Bot — see docs/agent-specs/05-org-bot.md
  ai/                        multi-provider LLM client (OpenRouter / DeepSeek)
  google/                    Google Sheets client (Lead Agent's data store)
  verifix/                   HR attendance (API or CSV)
  microsoft/                 Graph client (app-only) + Teams notifier
  telegram/                  bot primitives — send, edit, approval buttons, HTML sanitization
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
- [ ] Verifix CSV column names — `COLUMN_ALIASES` in `integrations/verifix/client.py`
- [ ] Reconcile one day of AR output against SAP's own aging report
- [ ] Confirm the Azure app has admin consent for the application permissions
      listed in `integrations/microsoft/client.py`
- [ ] `CRM_API_KEY` — still a placeholder as of this writing; the CRM
      pipeline snapshot silently stays empty until it's filled in
- [ ] OpenRouter account balance — OPS Manager Bot's primary model
      (`anthropic/claude-sonnet-5`) fails with HTTP 402 if the account runs
      low on credits; DeepSeek (`OPS_MANAGER_BOT_PROVIDER=deepseek`) is a
      separately-billed fallback path that doesn't share this risk
