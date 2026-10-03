# MGMG Digital Command Center

The company's own "AI office": two Telegram bots and a set of scheduled
agents that read the business systems (SAP, 1C, BILLZ, Verifix, Google
Sheets), tell the Director what matters every morning, and ask, remind and
record employees' work so nobody has to do it by hand.

Built for **MGMG / ЭМЖИЕМ** (Tashkent) — **Primus Londry** (industrial
laundry equipment) and **Garmin** retail. The roadmap is the owner's plan
*ЭМЖИЕМ AI Агентлар Тизими* (21 agents, codes like A2 / B1 below).

> **If you are taking this over, read in this order:** this file →
> `CLAUDE.md` (rules and decisions that must not be undone) →
> `docs/agents-status.md` (what runs, what's left) → the spec of the agent
> you're touching in `docs/agent-specs/`.

---

## 1. What runs, and when

All times Asia/Tashkent. Everything the bots say is **Uzbek Cyrillic**.

| When | What | Who gets it | Code |
| ---- | ---- | ----------- | ---- |
| always | **OPS Manager Bot** — the Director gives tasks (ticks who gets them before sending) and asks questions about any data; employees send reports and files, and talk to the work AI about their own tasks | Director, employees | `integrations/org_bot/ops_manager.py` |
| always | **Admin Bot** — access requests, roles, names, days off, announcements, system checks | admin | `integrations/org_bot/admin.py` |
| 08:00 | Morning brief (A2): cash (1C), yesterday's sales, stock, customer debt, today's payments, shop sales (BILLZ), who didn't report, attendance (Verifix) | Director | `agents/ceo-daily-brief/` |
| 08:00 | Billz → SAP check: every shop cheque must be in SAP the same day | Director (on trial: the admin) | `agents/billz-sap-check/` |
| 08:00 | Lead Agent (F2): new tenders/leads into the Google sheet | Director | `agents/lead-agent/` |
| 08:00 | Lead hand-out: one lead to each B2B sales person | B2B Sotuv | `agents/lead-handout/` |
| 08:00 | Receivables alert: overdue customer debt by age | Director | `agents/receivables/` |
| 08:00 | Task reminders and overdue notices (A3) | employees, Director | `agents/task-tracker/` |
| Mon 08:00 | Data quality (B4); 30-day cash calendar (B2) | admin; Director + accountants | `agents/data-quality/`, `agents/cash-calendar/` |
| 1st, 08:00 | Monthly KPI (E1) | Director, HR | `agents/task-tracker/ --monthly` |
| 10:00, 17:35 | Team cheer — one friendly line | employees | `agents/team-cheer/` |
| 15:00 | Lead check-in: "жараёнда / рад этилди / бажарилди?" | B2B Sotuv | `agents/lead-handout/ --checkin` |
| 16:00 | Daily report ask (A1) | employees | `agents/daily-reports/ --ask` |
| 17:00 | Report reminder; on Fridays the weekly task scorecard | employees; Director | `agents/daily-reports/ --remind`, `task-tracker --weekly` |
| on request | Written permission requests (B1) with approvals | requester, approvers | `integrations/org_bot/permission_flow.py` |
| on a QR scan | Client complaints — Londry `/f`, Garmin `/f/garmin` | Director (🔴) | `integrations/api/feedback_page.py` |

Days off set with Admin Bot `/dam` silence everything employee-facing.
Full status, and the agents still waiting: `docs/agents-status.md`.

---

## 2. How it fits together

```
 SAP B1 (HANA) ── SAP gateway (Node.js, "server laptop") ── push-ar-aging.ps1 ──┐  every 30 min
 1C (Clobus OData) · BILLZ · Verifix · Google Sheets · OpenRouter (AI) ───────┐ │  (Windows task)
                                                                              ▼ ▼
 Telegram ◀──webhooks──▶ mgmg-api (FastAPI, Render) ──▶ PostgreSQL (mgmg-db) ◀── Render cron jobs
                         Admin Bot · OPS Manager Bot                              (the agents above)
                         SAP receiver · /f pages · /db viewer
```

- **Render** runs everything in the cloud from `render.yaml` (a Blueprint):
  `mgmg-db` (Postgres), `mgmg-api` (always-on web service) and four cron
  jobs (`mgmg-morning-agents` 08:00, `mgmg-daily-reports` 16:00,
  `mgmg-report-reminder` 17:00, `mgmg-team-cheer` 10:00–17:35). Secrets live
  in Render's environment group **`mgmg-shared`**, never in this repo.
- **SAP is never reached from the cloud.** A PowerShell script on the
  gateway machine calls the gateway's fixed tools and pushes the results to
  `mgmg-api` (`scripts/sap-gateway-push/`). The gateway belongs to the IT
  colleague who runs SAP access (Abdulbosit, 2026). Its rules
  (`SAP_B1_AI_AGENT_TEACHING_UPDATED.md`): no SQL and no database passwords
  outside it — new data means a **new gateway tool**, written up like
  `docs/sap-gateway-tools.md`.
- The **database schema applies itself** on every start
  (`database/schema.sql`, only `CREATE … IF NOT EXISTS` / `ALTER … IF NOT
  EXISTS`) — there is no migration tool to run.
- Every external call is written to the audit table `agent_actions`.

---

## 3. Where everything lives (accounts to hand over)

No passwords here — only where to find them. Whoever takes over needs
access to each of these:

| What | Where |
| ---- | ----- |
| Code | GitHub `emjiemai/mgmg-agents` (this repo) |
| Hosting, database, all secrets | Render — `mgmg-api`, `mgmg-db`, the cron jobs; env group `mgmg-shared` |
| Telegram bots | @BotFather, on the account that created Admin Bot and OPS Manager Bot |
| SAP push | The gateway machine: `C:\mgmg-push\push-ar-aging.ps1` + Windows task "MGMG SAP push" |
| SAP gateway itself | Its maintainer (Node.js, `localhost:3000`, its own `.env`) |
| Leads sheet | Google Sheets "POSSIBLE Leads" (`GOOGLE_LEADS_SHEET_ID`), shared with the service account |
| AI | OpenRouter account (`OPENROUTER_API_KEY`) |
| Shop tills | BILLZ → Настройки → Компания → Ключи интеграции |
| Attendance | Verifix (login + organisation ID) |
| Accounting / cash | 1C «Бухгалтерия для Узбекистана» on Clobus (OData user) |
| Garmin AI bot | Separate repo `emjiemai/Garmin-AI-bot` — sends its leads here |

---

## 4. Running it day to day (the admin)

**Admin Bot commands**

| Command | Does |
| ------- | ---- |
| `/xodimlar` | Employees: name, role, ака/опа, weekend work, AI on/off, cheer on/off, remove |
| `/ismlar` | Ask everyone without a typed name for it |
| `/dam` | Mark days off (no asks, reminders, cheer or leads that day) |
| `/elon <text>` | Announce to every employee (preview, then confirm); `/elon` alone = a "technical error" notice |
| `/grafik` | Who is on "эркин график" (never shown as late/absent) |
| `/verifix`, `/billz`, `/1c` | Is that system connected, and what does it show today |
| `/sifat` | Data quality check now (SAP feeds, missing columns, unnamed people…) |
| `/qr` | The two printable complaint QR cards |

**New people** write anything to **OPS Manager Bot** → the admin gets an
Accept card in Admin Bot → they pick a role → the admin confirms the role →
they type their full name. The **Director** is simply the person whose role
is "Операцион директор".

**OPS Manager Bot commands** — Director: `/maqsad`, `/maqsadlar`, `/kpi`,
`/baho`; employees: `/hisobot` (change or delete today's report), `/kpi`,
`/natija`, `/ism`, `/ruxsat` (written permission request), `/bekor`.
Everything else is plain text: the Director writes a task or a question, an
employee writes their report or asks the work AI.

**Emergency stop:** `BOTS_FROZEN=true` in `mgmg-shared` → both bots and every
cron job stop sending. Each agent also has its own `*_ENABLED` switch.

**Look at the data:** `https://<mgmg-api host>/db` (read-only, password
`DB_VIEWER_PASSWORD`), or any Postgres client with Render's External
Database URL.

**Logs:** Render dashboard → the service → Logs. A cron job can be run by
hand there ("Trigger Run") — note that it really sends.

---

## 5. Setting it up from scratch

Only needed for a new Render account or a full rebuild.

1. **Telegram bots.** In @BotFather create two bots (admin, operations) and
   keep their tokens. Get the admin's Telegram user id and the chat id where
   Admin Bot should post.
2. **Render.** New → Blueprint → this repo. It creates `mgmg-db`, `mgmg-api`,
   the cron jobs and the `mgmg-shared` group. Keep the paid plans in
   `render.yaml` (a free database expires after 30 days).
3. **Secrets** in `mgmg-shared` — section 6. Make up long random values for
   the webhook secrets.
4. **Webhooks** (once, in a browser):
   ```
   https://api.telegram.org/bot<ADMIN_BOT_TOKEN>/setWebhook?url=https://<mgmg-api host>/webhooks/telegram/admin/<ADMIN_BOT_WEBHOOK_SECRET>
   https://api.telegram.org/bot<OPS_MANAGER_BOT_TOKEN>/setWebhook?url=https://<mgmg-api host>/webhooks/telegram/ops/<OPS_MANAGER_BOT_WEBHOOK_SECRET>
   ```
5. **People.** Each person (the Director first) writes to OPS Manager Bot and
   is accepted in Admin Bot (section 4).
6. **SAP push** on the gateway machine: copy `scripts/sap-gateway-push/` to
   `C:\mgmg-push\`, fill in the four values at the top of
   `push-ar-aging.ps1`, run it with `-Check`, then run `install-task.ps1` in
   an administrator PowerShell (every 30 minutes). Details:
   `scripts/sap-gateway-push/README.md`.
7. **Other systems**, each optional and checked from Admin Bot: BILLZ
   (`/billz`), Verifix (`/verifix`), 1C (`/1c`), Google Sheets (share the
   sheet with the service account's e-mail as Editor), Garmin AI bot (the
   same `GARMIN_LEADS_SECRET` on both sides).
8. **Check:** Render → `mgmg-morning-agents` → Trigger Run, and read what the
   Director got.

---

## 6. Settings (Render `mgmg-shared`)

Every setting with its meaning is in **`.env.example`**; the defaults are in
`integrations/common/config.py`. The ones that matter:

| Group | Settings |
| ----- | -------- |
| Database | set by the Blueprint (`DATABASE_URL` from `mgmg-db`) |
| Telegram | `ADMIN_BOT_TELEGRAM_BOT_TOKEN`, `ADMIN_BOT_TELEGRAM_CHAT_ID`, `ADMIN_BOT_ADMIN_USER_ID`, `ADMIN_BOT_WEBHOOK_SECRET`, `OPS_MANAGER_BOT_TELEGRAM_BOT_TOKEN`, `OPS_MANAGER_BOT_WEBHOOK_SECRET` |
| AI | `OPENROUTER_API_KEY` (+ optional model overrides) |
| SAP | `SAP_PUSH_WEBHOOK_SECRET` (the same value goes into `push-ar-aging.ps1`), `SAP_DEFAULT_CURRENCY=USD` |
| Leads | `LEAD_AGENT_ENABLED`, `SERPAPI_API_KEY`, `TAVILY_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_LEADS_SHEET_ID`, `AGENT_WRITES_ENABLED` (lets the Lead Agent write to the sheet) |
| Systems | `ONEC_ODATA_URL` / `ONEC_LOGIN` / `ONEC_PASSWORD`; `BILLZ_SECRET_TOKEN`; `VERIFIX_LOGIN` / `VERIFIX_PASSWORD` / `VERIFIX_FILIAL_ID`; `GARMIN_LEADS_SECRET` |
| Billz → SAP | `BILLZ_SAP_WAREHOUSES` (the shops' warehouses in SAP), `BILLZ_SAP_CHECK_TRIAL` (true = only to the admin), `BILLZ_SAP_CHECK_ROLES` |
| Switches | `DAILY_REPORTS_ENABLED`, `TASK_TRACKER_ENABLED`, `PERMISSIONS_ENABLED`, `LEAD_HANDOUT_ENABLED`, `TEAM_CHEER_ENABLED`, `FEEDBACK_ENABLED`, `BOTS_FROZEN` |
| Other | `PERMISSION_APPROVAL_TIERS` (payment limits), `PUBLIC_BASE_URL` (address on the QR codes — `/f` is already printed, never move it), `DB_VIEWER_PASSWORD` |

---

## 7. Changing things safely

```bash
pip install -r requirements.txt pyflakes
python scripts/selfcheck.py                  # ~900 offline checks, no credentials — must end "All checks passed."
python -m pyflakes integrations agents scripts
```

- **Commit only when both pass**, then `git push` — Render deploys `main`
  automatically and the schema applies itself.
- Every change gets checks in `scripts/selfcheck.py` and an update to its
  `docs/agent-specs/*.md` and `docs/agents-status.md`.
- Try an agent on real data without sending anything: add `--dry-run`, e.g.
  `python agents/ceo-daily-brief/agent.py --dry-run` (needs a `.env` with
  the real secrets: `cp .env.example .env`).
- Local stack: `docker compose up -d` (Postgres + the API).
- **New SAP data** = ask the gateway's maintainer for a new tool (written up
  like `docs/sap-gateway-tools.md`), add its kind to `FULL_DATASETS` and
  `EXPECTED_COLUMNS` in `integrations/sap/push_handler.py`, and a
  `Push-CompleteTool` line in `push-ar-aging.ps1`.
- **A new role:** `integrations/org_bot/roles.py` (selfcheck makes sure its
  labels agree everywhere).
- **A new scheduled job:** add the agent to a list in
  `scripts/run_morning_agents.py`, or give it its own cron in `render.yaml`.

**Rules that must not be broken** (the owner's decisions — full list in
`CLAUDE.md`): every bot text in Uzbek Cyrillic, polite "сиз", names keep
their capitals; the business is "Londry", never "Laundry"; the bots carry no
text between employees and the Director; reports and tasks are confirmed
before they're saved or sent; numbers are never guessed — partial data says
"камида", missing data says so.

---

## 8. When something is wrong

| Symptom | Look at |
| ------- | ------- |
| Bots don't answer | `mgmg-api` logs; `BOTS_FROZEN`; the webhook (`https://api.telegram.org/bot<TOKEN>/getWebhookInfo`) |
| No morning brief | `mgmg-morning-agents` logs; is there an active Director in `/xodimlar`? |
| SAP numbers "камида" / "ўқилмади" | on the gateway machine: `C:\mgmg-push\push-ar-aging.log` and `push-ar-aging.ps1 -Check`; Admin Bot `/sifat` lists missing columns |
| SAP numbers out of date | the Windows task (`Get-ScheduledTaskInfo -TaskName "MGMG SAP push"`); is the laptop on and the gateway (`npm start`) running? |
| BILLZ / Verifix / 1C missing from the brief | Admin Bot `/billz`, `/verifix`, `/1c` say why |
| Someone is "late" every day | mark them in `/grafik` if they're on "эркин график" |
| Anything else | the `agent_actions` table in `/db` — every external call with its result |

---

## 9. Code layout

```
agents/                    scheduled agents — one process per run, each has --dry-run
integrations/
  api/                     FastAPI: webhooks, SAP receiver, /f complaint pages, /db viewer
  org_bot/                 both bots: registration, tasks, reports, KPI, permissions, leads, AI chat
  sap/                     SAP push receiver, receivables buckets, brief figures
  billz/ onec/ verifix/    BILLZ, 1C, Verifix clients and their rules
  google/ search/ tenders/ Lead Agent's sources and the leads sheet
  garmin/                  leads from the Garmin AI bot
  ai/ telegram/ common/    OpenRouter, Telegram primitives, config / DB / audit / money / time
database/schema.sql        the whole schema, self-applying
scripts/                   selfcheck, cron runner, SAP push (sap-gateway-push/)
docs/agent-specs/          one spec + runbook per agent
docs/agents-status.md      what runs, what's left, what each is waiting for
docs/sap-gateway-tools.md  the SAP gateway tools this system needs
CLAUDE.md                  working rules and the owner's decisions
```

Conventions: Python 3.11, async `httpx`, `pydantic-settings`, `psycopg` 3,
`loguru`. Money is stored as integer minor units, time in UTC and shown in
Tashkent time. External calls retry with backoff; an agent whose source fails
says so in its message instead of crashing. Comments explain *why*.
