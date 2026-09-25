# MGMG Digital Command Center

Central AI operating system for MGMG (Tashkent): SAP Business One data,
Telegram bots and scheduled agents in one place, so the Director sees what
matters and employees are asked, reminded and recorded without anyone doing
it by hand. The roadmap is the owner's plan, *ЭМЖИЕМ AI Агентлар Тизими*
(21 agents; agent codes like A2/B1 below refer to it).

**Business lines:** Primus Laundry (ONDRY — industrial laundry equipment) ·
Garmin watch retail.

## What runs

| Plan | What | Where |
| ---- | ---- | ----- |
| H0 | Hub: FastAPI + PostgreSQL + two Telegram bots on Render | `integrations/api/`, `render.yaml` |
| A1 | Daily reports: 16:00 ask, 17:00 reminder, one follow-up for a vague report | `docs/agent-specs/06-daily-reports.md` |
| A2 | Morning brief — five numbers + who didn't report | `docs/agent-specs/01-ceo-daily-brief.md` |
| A3 | Task tracker: deadlines, reminders, overdue notices, Friday scorecard | `docs/agent-specs/08-task-tracker.md` |
| B1 | Written permissions (EMJ-SOP-ADM-01) + payment gate by amount | `docs/agent-specs/07-permissions.md` |
| — | Receivables alert (overdue debt by age) | `docs/agent-specs/03-receivables.md` |
| — | OPS Manager Bot: routes the Director's tasks (to a department or one named person), answers questions from data | `docs/agent-specs/05-org-bot.md` |
| — | Lead Agent — paused by the business (`LEAD_AGENT_ENABLED`) | `docs/agent-specs/04-lead-agent.md` |

Every message the bots send is Uzbek Cyrillic. Nothing reaches the Director
from an employee without the employee confirming it. Every employee gives
their real name once; Telegram profile names are never used on documents.

## Architecture

```
SAP gateway (its own Windows machine) ──push──┐
                                              ▼
Telegram ◀──▶ mgmg-api (FastAPI) ──▶ PostgreSQL ◀── cron: 08:00 morning agents
             Admin Bot, OPS Manager Bot,             16:00 report ask
             SAP push webhooks, /db viewer           17:00 reminder + Friday scorecard
```

- **mgmg-api** — always-on web service: both bots' webhooks, the SAP pushes,
  and the read-only database viewer.
- **mgmg-morning-agents** (08:00) — brief, Lead Agent, receivables, task
  reminders, name requests — `scripts/run_morning_agents.py`.
- **mgmg-daily-reports** (16:00 Mon–Fri) and **mgmg-report-reminder**
  (17:00 Mon–Fri, also the Friday scorecard).

SAP data arrives **only by push** from the gateway's own machine
(`scripts/sap-gateway-push/`); nothing reaches into SAP. Each tool is pushed
with a row limit, so totals built from a capped push are shown as lower
bounds ("камида") — see the brief spec.

Money is stored as integer minor units (tiyin/cents) everywhere; time is
stored in UTC and shown in Asia/Tashkent (`integrations/common/money.py`,
`timeutil.py`). AI calls go through OpenRouter only
(`integrations/ai/openrouter_client.py`, Gemini 3.8 Flash → 3.7 Flash).

## Looking at the database

`https://<mgmg-api host>/db` — a minimal read-only viewer: every table with
its row count, rows newest first, and a text search. It is **off** until
`DB_VIEWER_PASSWORD` is set in Render's `mgmg-shared` group; then the browser
asks for a login (any name, that password). It can't change anything: every
query runs in a read-only transaction. For heavier work, any Postgres client
(DBeaver, TablePlus) connects with Render's External Database URL.

## Security model

| Rule | How it is enforced |
| ---- | ------------------ |
| Read-only for SAP | SAP data only arrives by push; there is no code path that writes to SAP. |
| Everything audited | `integrations/common/db.audited()` wraps every external call; rows land in `agent_actions`. |
| No hardcoded secrets | All credentials come from `.env`/Render's env group through `integrations/common/config.py`, wrapped in `SecretStr`. |
| Human in the loop | The bots act only on what a person sent or tapped; scheduled agents report, they don't act. |
| Written approvals | Nobody approves their own permission request; every decision records who, when and from which account. |

## Setup

### Production — Render Blueprint

```bash
git push origin main   # Render auto-deploys; the Blueprint is render.yaml
```

Secrets marked `sync: false` live in the Render dashboard under the
`mgmg-shared` environment group, never in this repo. Switches worth knowing:
`DAILY_REPORTS_ENABLED`, `TASK_TRACKER_ENABLED`, `PERMISSIONS_ENABLED`,
`PERMISSION_APPROVAL_TIERS`, `LEAD_AGENT_ENABLED`, `BOTS_FROZEN`,
`DB_VIEWER_PASSWORD` — each is described in `.env.example`.

### Local development

```bash
cp .env.example .env   # edit .env
docker compose up -d   # PostgreSQL (schema applied automatically) + the API
```

### Checks

```bash
python scripts/selfcheck.py                          # offline logic checks, no credentials needed
python agents/ceo-daily-brief/agent.py --dry-run     # real data, nothing sent
python agents/task-tracker/agent.py --weekly --dry-run --force
```

## Layout

```
agents/                      scheduled agents (one process per run, then exit)
  ceo-daily-brief/           08:00 brief — the five numbers (A2)
  receivables/               overdue debt alert
  daily-reports/             16:00 ask + 17:00 reminder (A1)
  task-tracker/              reminders, overdue notices, Friday scorecard (A3)
  lead-agent/                B2B lead sourcing (paused)
integrations/
  api/                       FastAPI app: webhooks + /db viewer
  common/                    config, logging, DB + audit, HTTP retry, money, time, dates
  sap/                       gateway push handler, aging buckets, dashboard figures
  org_bot/                   Admin Bot + OPS Manager Bot, permissions, tasks, names
  ai/                        OpenRouter client
  telegram/                  bot primitives — send, edit, HTML sanitization
  google/, search/, tenders/ Lead Agent's sources and sheet
database/schema.sql          self-applying schema (ALTER ... IF NOT EXISTS, no migration tool)
dashboard/powerbi-queries/   SQL sources + DAX for a Power BI report (not built)
scripts/                     selfcheck, cron runner, SAP gateway push script
docs/agent-specs/            what each agent does, and its runbook
```

## Conventions

- Python 3.11+, `httpx` (async), `pydantic`/`pydantic-settings`, `loguru`
- Every external call retries with backoff; agents degrade rather than crash
- Numbers are never guessed: missing, stale or partial data is said out loud
- Comments and docstrings state the *why*, not the *what*
