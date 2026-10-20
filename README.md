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
| always | **OPS Manager Bot** — the Director gives tasks (ticks who gets them before sending) and asks questions about any data, which the analyst looks up itself across SAP, 1C, BILLZ and Verifix, read-only (`analyst.py`); employees send reports and files, and talk to the work AI about their own tasks | Director, employees | `integrations/org_bot/ops_manager.py` |
| always | **Admin Bot** — access requests, roles, names, days off, announcements, system checks | admin | `integrations/org_bot/admin.py` |
| always | **Data for accounting and finance** — people the admin gives SAP / 1C / Billz to ask about it in OPS Manager Bot (only their systems; Russian answers if set); the Director is told and can close it | accountant, finance | `integrations/org_bot/data_access.py`, `docs/agent-specs/22-data-access.md` |
| 08:00 | Morning brief (A2): cash (1C), yesterday's sales, stock, customer debt, today's payments, shop sales (BILLZ), who didn't report, attendance (Verifix) | Director | `agents/ceo-daily-brief/` |
| 08:00 | Billz → SAP check: every shop cheque must be in SAP the same day | Director + Garmin manager (on trial: the admin, counts only) | `agents/billz-sap-check/` |
| 08:00 | **Garmin brief** — the same picture as the Director's, Garmin only: shop sales (Billz), SAP sales of the Garmin warehouses (as entered by 08:00), open complaints, Garmin bot leads, Billz → SAP (08:00 state), who didn't report | Garmin manager | `agents/garmin-brief/` |
| 08:00 | Lead Agent (F2): new tenders/leads into the Google sheet | Director | `agents/lead-agent/` |
| 08:00 | Receivables alert: overdue customer debt by age | Director | `agents/receivables/` |
| 08:00 | Task reminders and overdue notices (A3) | employees, Director | `agents/task-tracker/` |
| 08:00 | IT's technical report (after the other 08:00 agents): connections, SAP push, brief delivered, errors, DB, security — no figures | admin | `agents/tech-report/` |
| Mon 08:00 | Data quality (B4); 30-day cash calendar (B2) | admin; Director + accountants | `agents/data-quality/`, `agents/cash-calendar/` |
| Mon 08:00 | Old stock — goods a year or more in the warehouse, and close to it (PDF); `/eski` any time | Director (all), Garmin manager (Garmin warehouses) | `agents/old-stock/` |
| Thu 08:00 | **Garmin competitors** — who else sells Garmin in Uzbekistan (OLX listings, other shops' sites via Google) and for how much against our garmin.com.uz prices (PDF); `/raqobat` any time | Garmin manager | `agents/garmin-competitors/` |
| 1st, 08:00 | Monthly KPI (E1) | Director, HR | `agents/task-tracker/ --monthly` |
| 16:00 | Daily report ask (A1) | employees | `agents/daily-reports/ --ask` |
| 17:00 | Report reminder; on Fridays the weekly task scorecard | employees; Director | `agents/daily-reports/ --remind`, `task-tracker --weekly` |
| on request | Written permission requests (B1) with approvals | requester, approvers | `integrations/org_bot/permission_flow.py` |
| on a QR scan | Client complaints — Londry `/f`, Garmin `/f/garmin` — a ticket with ✅ Ҳал бўлди + reason; asked "ҳал бўлдими?" every hour (09–20, max 3) | the Londry role / the Garmin manager (the Boss only if nobody holds the role) | `integrations/org_bot/feedback.py`, `routes.py` |
| daily, 13:00 | Encrypted database backup fetched by an office computer — the copy outside Render (IT-5) | stays on that computer + a second disk | `integrations/backup/`, `scripts/backup/` |

The brief reaches the Director as one designed **picture**; the cash
calendar, KPI table, Billz → SAP check and data-quality report as **PDFs**
(`integrations/reports/`, `docs/agent-specs/18-reports.md`). Days off set
with Admin Bot `/dam` silence everything employee-facing.
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
| SAP gateway itself | Code in this repo, `sap-gateway/` (Node.js); runs on the gateway computer at `localhost:3000` with its own `.env` (HANA user, `API_TOKEN`) — admin access since 2026-10-09 |
| Leads sheet | Google Sheets "POSSIBLE Leads" (`GOOGLE_LEADS_SHEET_ID`), shared with the service account |
| AI | OpenRouter account (`OPENROUTER_API_KEY`) |
| Shop tills | BILLZ → Настройки → Компания → Ключи интеграции |
| Attendance | Verifix (login + organisation ID) |
| Accounting / cash | 1C «Бухгалтерия для Узбекистана» on Clobus (OData user) |
| Garmin AI bot | Separate repo `emjiemai/Garmin-AI-bot` — sends its leads here |
| Database backups | The office computer: `C:\mgmg-backup-script\pull-backup.ps1` + Windows task "MGMG database backup"; copies in `D:\mgmg-backup` and the second disk; the key that opens them is in the owner's password vault (`scripts/backup/README.md`) |

Every link the system uses (pages, webhooks, services): `docs/links.md`.

---

## 4. Running it day to day (the admin)

**Admin Bot commands**

| Command | Does |
| ------- | ---- |
| `/xodimlar` | Employees: name, role, ака/опа, weekend work, AI on/off, cheer on/off, daily reports on/off, **AI language (ўзбекча / русча)**, **data access SAP / 1C / Billz** (Бухгалтерия and Молия only — the Director is told each time and can close it), remove. The Garmin менежер's data comes with the role (the Boss is told). Removing someone ends everything of theirs; registering again starts fresh |
| `/ismlar` | Ask everyone without a typed name for it |
| `/dam` | Mark days off (no asks, reminders, cheer or leads that day) |
| `/elon <text>` | Announce to every employee (preview, then confirm); `/elon` alone = a "technical error" notice |
| `/grafik` | Who is on "эркин график" (never shown as late/absent) |
| `/verifix`, `/billz`, `/1c` | Is that system connected and readable — status and counts only, no figures (the Director's order of 07.10.2026) |
| `/texnik` | IT's technical report now: connections, SAP push, brief delivered, errors, DB, security (also every morning) |
| `/sifat` | Data quality check now (SAP feeds, missing columns, unnamed people…) |
| `/qr` | The two printable complaint QR cards |
| `/qr sharh` | The printable Google review cards (`/r`), one per branch with a link in `GOOGLE_REVIEW_URLS` — green card with five white stars |

**New people** write anything to **OPS Manager Bot** → the admin gets an
Accept card in Admin Bot → they pick a role → the admin confirms the role →
they type their full name. The **Director** is simply the person whose role
is "Операцион директор".

**Tasks** — each card has a code (T-2026-10-0147). The employee taps
▶️ Қабул қилдим, ⛔ Тўсиқ бор (then writes the reason) or ✅ Бажардим (then
sends proof: a photo, a file or a short text). The one who assigned it gets
the proof with ✅ Қабул (closed) or ↩️ Қайтариш (redo). Without proof a
task never closes.

**Cheque photos** (2026-10-14) — the Director sends a photo of a cheque to
OPS Manager Bot. The AI reads the eight details (фискал белги, фискал модул
рақами, чек рақами, санаси, терминал RRN коди, суммаси, товар номи,
контрагент ИНН). All there and nothing wrong → the photo with the details
goes to everyone in Бухгалтерия, and the Director is told who got it.
Something missing, unreadable or wrong (ИНН not 9/14 digits, RRN not 12, date
in the future or over 90 days old, not in so'm, the same cheque already sent)
→ the bot asks him: 📨 Шундай юбориш · 📋 Бу чек эмас (→ the people list) ·
❌ Бекор, or he sends a clearer photo. A photo that isn't a cheque goes the
old way (the people list). Off: `CHEQUE_READER_ENABLED=false`.

**OPS Manager Bot commands** — Director: `/maqsad`, `/maqsadlar`, `/kpi`,
`/baho`; employees: `/hisobot` (change or delete today's report), `/kpi`,
`/natija`, `/ism`, `/ruxsat` (written permission request), `/bekor`.
Everything else is plain text: the Director writes a task or a question, an
employee writes their report or asks the work AI.

**Emergency stop:** `BOTS_FROZEN=true` in `mgmg-shared` → both bots and every
cron job stop sending. Each agent also has its own `*_ENABLED` switch.

**Look at the data:** `https://<mgmg-api host>/db` (read-only, password
`DB_VIEWER_PASSWORD`) — technical tables only since 2026-10-07 (audit log,
access requests, changes, days off); financial and confidential tables are
closed there. Render's External Database URL still opens everything: it is
one of the broad rights listed in `docs/access-review-2026-10-07.md`.

**Logs:** Render dashboard → the service → Logs. A cron job can be run by
hand there ("Trigger Run") — note that it really sends.

**Backups:** three copies — the live database, Render's own recovery (3–7
days, Render → mgmg-db → Recovery) and an encrypted copy every day on an
office computer, plus a second disk (30 days + 12 months). Only the owner's
key opens it. Setup, the monthly restore test and how to restore for real:
`scripts/backup/README.md`. The 08:00 technical report shows the last copy.

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
| Garmin competitors | `GARMIN_COMPETITORS_ENABLED` (true), `GARMIN_COMPETITORS_WEEKDAY` (3 = Thursday; 0 = Monday); web search: `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_CX` (Google Programmable Search, engine `b184cf4c519404a9b`, the Uzbek shop sites set in it — Google Cloud project lead-agent-506007, key restricted to Custom Search API), else `SERPAPI_API_KEY` (neither: OLX only) |
| CEO brief | `COMPANY_VISION`, `COMPANY_MISSION` (the owner's own words, shown at the top of the 08:00 brief; empty = hidden); `AR_PAYMENT_DAYS` (30 — days to pay when SAP has no payment terms, for «муддати ўтгани») |
| Cheques | `CHEQUE_READER_ENABLED` (true), `CHEQUE_RECIPIENT_ROLES` (`buxgalteriya`; comma-separated role slugs) |
| Switches | `DAILY_REPORTS_ENABLED`, `TASK_TRACKER_ENABLED`, `PERMISSIONS_ENABLED`, `LEAD_HANDOUT_ENABLED`, `TEAM_CHEER_ENABLED`, `FEEDBACK_ENABLED`, `BOTS_FROZEN` |
| Other | `PERMISSION_APPROVAL_TIERS` (payment limits), `PUBLIC_BASE_URL` (address on the QR codes — `/f` is already printed, never move it), `DB_VIEWER_PASSWORD` |
| Backups | `BACKUP_SECRET` (the same value goes into `pull-backup.ps1`), `BACKUP_PUBLIC_KEY` (the owner's GnuPG **public** key — never the private one) |

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
- **New SAP data** = a new gateway tool in `sap-gateway/src/` (one file per
  tool, registered in `server.js` and `tools.js`, values as bound
  parameters), its kind in `FULL_DATASETS` and `EXPECTED_COLUMNS` in
  `integrations/sap/push_handler.py`, and a `Push-CompleteTool` line in
  `push-ar-aging.ps1`. Then copy `sap-gateway/src/` to the gateway computer
  and restart it.
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
| Buttons slow | `/db` → `agent_actions`, `duration_ms` of `telegram_*` rows: a warm call is ~100–200 ms; much more means Telegram or Render is slow, not the bot |
| No morning brief | `mgmg-morning-agents` logs; is there an active Director in `/xodimlar`? |
| SAP numbers "камида" / "ўқилмади" | on the gateway machine: `C:\mgmg-push\push-ar-aging.log` and `push-ar-aging.ps1 -Check`; Admin Bot `/sifat` lists missing columns |
| SAP numbers out of date | the Windows task (`Get-ScheduledTaskInfo -TaskName "MGMG SAP push"`); is the laptop on and the gateway (`npm start`) running? |
| BILLZ / Verifix / 1C missing from the brief | Admin Bot `/billz`, `/verifix`, `/1c` say why |
| Someone is "late" every day | mark them in `/grafik` if they're on "эркин график" |
| ❌ "Офисдаги нусха" in the technical report | on the office computer: `pull-backup.log`, `pull-backup.ps1 -Check`; is the computer on, the second disk plugged in? |
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
  backup/                  encrypted pg_dump for the office computer's copy
  reports/                 HTML templates → the brief picture and the PDF reports (fonts bundled)
  ai/ telegram/ common/    OpenRouter, Telegram primitives, config / DB / audit / money / time
database/schema.sql        the whole schema, self-applying
scripts/                   selfcheck, cron runner, SAP push (sap-gateway-push/), backups (backup/)
docs/agent-specs/          one spec + runbook per agent
docs/agents-status.md      what runs, what's left, what each is waiting for
docs/sap-gateway-tools.md  the SAP gateway tools this system needs
docs/links.md              every link: public pages, webhooks, services, where things are managed
docs/it-instruction-2026-10.md  the owner's IT plan (08.10–31.12) against what exists
CLAUDE.md                  working rules and the owner's decisions
```

Conventions: Python 3.11, async `httpx`, `pydantic-settings`, `psycopg` 3,
`loguru`. Money is stored as integer minor units, time in UTC and shown in
Tashkent time. External calls retry with backoff; an agent whose source fails
says so in its message instead of crashing. Comments explain *why*.

---

## 10. Change log

Every change, even a small one, gets a line here (newest first) — the rule
since 2026-10-10. Details are in git history and the specs.

| Date | What changed |
| ---- | ---- |
| 2026-10-20 | **Google Programmable Search** for the competitor agent (`integrations/search/google_cse_client.py`): `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_CX` in Render `mgmg-shared`; used when both are set, SerpAPI otherwise. The engine searches only the sites set in it (OLX, Uzum, Texnomart, Mediapark, Asaxiy, Alifshop, Glotr, Idea, t.me, Instagram) — the Lead Agent (whole web) stays on SerpAPI. |
| 2026-10-20 | **Competitor shops' contacts** (the owner: «дўкон номи, телефон, Instagram»): the competitors PDF now has «Рақобатчи дўконлар» — each shop's name, phones, Instagram, Telegram, site, address, its OLX listings and how many are cheaper than ours. Read from what the shop publishes: its OLX shop page (`<name>.olx.uz` contact block), its site, its Telegram channel; a missing Instagram is searched on Google by the shop's name in the weekly run (8 shops a run, each once a month, only shop-like names). robots.txt respected. **Private sellers' phones are not collected** (OLX hides them behind a login on purpose; personal data). Phones never go to the AI. Table `garmin_competitor_shops`; `integrations/garmin/competitor_shops.py`; spec 26. Bezels added to the parts left out. |
| 2026-10-19 | **Garmin competitors agent** (`docs/agent-specs/26-garmin-competitors.md`): every Thursday 08:00 the Garmin manager gets a PDF — every OLX.uz listing of Garmin (new / used, shop or private person, price, link), other Uzbek shops' pages from Google (gl=uz), and our price from garmin.com.uz side by side by model; «биздан арзон» = a new item under 95 % of our lowest price (first live check: 114 listings, 30 new items cheaper — e.g. fenix 8 47mm at 10,3 млн vs our 17,8 млн), «шубҳали» under 30 % (a copy or a wrong price); copies, straps, repairs left out. **/raqobat** any time (his ☰ menu; reuses the day's Google search). The bot answers «fenix 8 ни OLX'да қанчадан сотишмоқда?» (`garmin_data` → competitors). Stored in `garmin_competitor_offers` (new / cheaper since last week). A private seller is never named; nothing goes to the AI. Settings `GARMIN_COMPETITORS_ENABLED`, `GARMIN_COMPETITORS_WEEKDAY`. README §1 table: the old team-cheer row removed, old stock added. |
| 2026-10-19 | **No emoji in the friendly messages** (the owner): the 16:00 ask, the 17:00 reminder, «report accepted», the 22:00 minus, the work AI's short replies, the file question, `/elon`'s error notice — `tone.casual` removes every emoji and adds none (it used to end each with 🙏 / 😊 / 🙂). Buttons and the status marks on cards (✅ ⛔ 📨) stay. |
| 2026-10-19 | **Morning briefs fixed** (the owner: «the brief shows wrong things»). Checked against SAP: the 11 cheques the 08:02 Garmin brief listed as «not in SAP» were entered by the shops on 19.10 at 12:05–14:39 — the picture is the 08:00 state, the bot checks live; now said on the page («08:00 ҳолатига», «08:00 гача SAP'га киритилгани»). **Sales in so'm** (`DocTotalSy`), not SAP's USD, each document once. **Overdue debt**: SAP has no payment terms (due = invoice date), so every invoice was «overdue» from day one — now due `AR_PAYMENT_DAYS` (30) days after the invoice date. **Garmin warehouses** = the shops + every SAP warehouse named «Garmin» (09, 10, 03, 04, 06 were missed) — Garmin brief SAP sales, `/eski`, the analyst's `garmin_sap`. **Garmin brief: «Garmin захираси» removed** (four figures). Cash: the brief has had no cash line since 16.10; 1C has no Uzum account (only 193 395 so'm in 5720 «Uzum Bank»). |
| 2026-10-17 | Old stock: display dummies («Dummy Unit…», codes 010-M…) are left out (Garmin 208 → 137 rows, all 428 → 318); the note explains dates shared by many items (a big shipment or the first stock load into SAP — at least that old). |
| 2026-10-17 | **Old stock warning** (`docs/agent-specs/25-old-stock.md`): every Monday 08:00 the Director (all warehouses) and the Garmin manager (Garmin warehouses) get the goods a year or more in stock and those within a month of it — PDF list with value, last purchase date, sold in 90 days; **/eski** any time. **`/db`: 1C and Billz** pages read live (not stored): 1C balances now and this month's turnovers by counterparty, Billz cheques with items (3 / 7 / 14 days), Billz shops — open while the platform tables are (until 31.10). Selfcheck blanks the platform and AI keys and fails database calls at once (it had called the real Billz once its key was in .env, >15 min → 6 s). |
| 2026-10-17 | **`/db`: SAP as separate tables.** At the top of `/db`, «SAP маълумотлари»: each kind of the latest push as its own table with a name (Мижозлар, Товарлар, Омбор қолдиғи, Сотув ҳужжатлари қаторлари, Очиқ харид ҳисоб-фактуралари …), one column per SAP field, search and paging — `/db/sap_<kind>`. Open while the SAP tables are (until 31.10.2026). |
| 2026-10-17 | Line so'm (`GTotalSC`) arrives since the 14:13 push (gateway updated, old task deleted). Line amounts are before a whole-document discount — what was paid is the document total; the bot is told so. |
| 2026-10-17 | The old duplicate push task (:17 / :47) is now ignored for customers, orders, products and payments too while a complete push of that kind is under 6 hours old — it had overwritten 100 complete rows with thinner ones and put closed orders among the open ones. The new push script works (13:55: all 18 kinds); the gateway still needs the new `sales-by-date.js` for the line so'm (`GTotalSC`). |
| 2026-10-17 | **SAP line amounts fixed.** SAP sends each invoice line with the whole invoice's so'm total (DocTotalSy); the bot added those up per line, so a two-line invoice counted twice (#2496: 30M instead of 15M) — now document totals are summed once per document. The gateway's sales tool also returns each line's own so'm (`GTotalSC` with VAT, `TotalSumSy` without) — copy `sap-gateway/src/sales-by-date.js` and the new `push-ar-aging.ps1` to the gateway computer. **`/db`: SAP / 1C / Billz / Verifix tables open to IT until 31.10.2026** (the Boss's order to fix the briefs, access review §4), then closed by themselves. |
| 2026-10-17 | **Billz → SAP answers are live.** Billz is read live everywhere (nothing stored); only the 08:00 check result was stored and the bot's «қайси чеклар SAP'га киритилмаган?» read it — a cheque entered at noon still showed as missing. Now the bot re-checks on the spot: Billz now, against SAP's latest push (every 30 min), the last 14 days by cheque; the stored 08:00 results only if Billz can't be read. The 08:00 check and the Garmin brief are unchanged (a snapshot of yesterday, by design). |
| 2026-10-17 | Access review §4: the Boss told IT to find and fix the SAP / Billz / 1C mismatches in the briefs — IT may read those figures until fixed. First finding (Garmin): the brief is right; GARMIN MINOR (SAP warehouse 05) doesn't enter its Billz cheques into SAP (14.10, five of six on 16.10), ABAY enters days late (11.10 → 16.10), one cheque has a different amount. |
| 2026-10-17 | **The Garmin manager (Murod Umarov, the Boss's brother) runs Garmin at the Boss's level:** not scored — no KPI card, goals or ratings of his own, not in KPI tables; `/kpi` = his team's table, `/maqsad` `/maqsadlar` `/natija` = his team's goals and results (only Garmin сотув; his buttons can't reach anyone else); `/baho` stays the Boss's. His ☰ menu: /hisobotlar /kpi /maqsad /maqsadlar /natija /bekor. His card has no working-day or report switches. The bot's company knowledge says who he is. |
| 2026-10-17 | **The Garmin manager sends no daily report** (like the Director, `kpi.NO_REPORT_ROLES`): never asked or reminded, no minus, not on the calendar, his messages never become a report; no 📝 switch or /hisobot for him — /hisobotlar shows his team. |
| 2026-10-17 | `/hisobot` (one letter short of `/hisobotlar`) now shows the team calendar to the Director and the Garmin manager, who send no report of their own — it had answered «no report is asked of you». |
| 2026-10-17 | **Shift weeks for Garmin shop staff:** Admin Bot `/xodimlar` → a Garmin сотув person → seven day buttons (Ду … Як). The report is asked only on ✅ days — a day off is never red, never a minus, not in KPI. Others keep Mon–Fri + Saturday / Sunday. **The Garmin manager's /hisobotlar** (his ☰ menu): the report calendar of his team. |
| 2026-10-17 | **Four more from the owner's review.** (1) A task given again while the person's earlier one is open isn't added twice — they get 🔁 a reminder on the existing card. (2) A role change is a clean start: their open tasks are cancelled (a co-assignee keeps theirs), today's report ask closes without a minus, the AI forgets the old chat, KPI counts only the new role. (3) **/hisobotlar** (the Director, ☰ menu): the month's report calendar as a picture — green sent, red missed, amber today, grey not asked. (4) **Database review** every morning + Admin Bot **/baza**: the bot's own tables (stale held messages, removed people's tasks, test / duplicate rows, questions saved as reports…), safe fixes by UPDATE only, counts to IT (`docs/agent-specs/24-db-review.md`). The tasks, task drafts, old task updates and the old lead hand-out tables were emptied by the owner. |
| 2026-10-17 | **Clean-up after the owner's database review.** Team cheer **removed** (code, 💬 toggle, buttons, the daytime cron in render.yaml, tests; delete the `mgmg-team-cheer` service in Render). A message with «?» while the report is owed goes to the AI and never becomes the report (questions had been saved as reports). Held «ҳисобот / савол» messages nobody tapped close at 22:00 (`expired`). Selfcheck now runs against an unreachable database, so no test can read or write production. Data to delete by hand (SQL in the reply): test permission requests, test complaints, the cheer tables. |
| 2026-10-17 | **`/db`: the bot's own tables are open** (the Boss approved; `docs/access-review-2026-10-07.md` §4): employees, tasks, reports, KPI, permissions, complaints, cheques, AI chats, leads — rows, search, paging. SAP, 1C, Billz, Verifix (and the briefs, receivables alerts and Billz → SAP results built from them) stay 🔒, structure only. A new table is closed until listed in `db_viewer.TECHNICAL_TABLES`. |
| 2026-10-16 | Gateway updated on the SAP computer: `-Check` shows all 16 tools working. The script now expects `get_stock_value`'s item columns (no more «missing columns: Items» warning) — copy `push-ar-aging.ps1` again, keeping its settings. |
| 2026-10-16 | **SAP: everything the plan needs, complete** — 12 new gateway tools (`sap-gateway/src/complete.js`): every customer, every item with reorder levels, open sales orders, incoming and outgoing payments (45 days), open supplier invoices, open purchase orders, sales people, equipment, service calls and contracts, item sales of 90 days. The push calls them all (a capped tool only while one is missing), the receiver stores them (new kind `item_sales_90d`, up to 200 000 rows), the analyst knows each. **The push script no longer writes a log file** on the gateway computer (and deletes the old one); `-Check` shows types, never figures. To deploy: copy `sap-gateway/src/` and the new `push-ar-aging.ps1` (keep its four settings) — `docs/sap-gateway-tools.md`. |
| 2026-10-16 | **Stock by item** — «X нечта қолди?» now works: the gateway's `get_stock_value` already sends every item row (~1,035), but the receiver kept only the per-warehouse totals; it now also keeps the item rows as the kind `stock_items` (ItemCode, ItemName, warehouse, OnHand, value), and the analyst and the Garmin manager's `garmin_sap` look there. The old `inventory` list stays capped (5 items per warehouse). No change on the gateway computer. |
| 2026-10-16 | **Garmin manager: better memory, his team's reports.** A reply to one of the bot's messages (an earlier answer, a complaint card, the brief) now shows the AI what was quoted; the AI keeps 24 messages of the day, not 12 (everyone with the analyst or the work AI). The Garmin manager now sees the Garmin сотув team's daily reports (14 days: what each wrote, when, who didn't send one, the minus) — `garmin_data` → `reports`; nobody else's. |
| 2026-10-16 | **☰ command menu in both bots** (`integrations/telegram/menus.py`): the menu button next to the message box lists the / commands. Admin Bot — the admin's commands (/xodimlar, /texnik, /grafik, /dam, /elon, /ismlar, /sifat, /qr, /billz, /1c, /verifix); OPS Manager — employees: /hisobot, /kpi, /natija, /ruxsat, /ism, /bekor; the Director: /kpi, /maqsad, /maqsadlar, /baho, /bekor. Set at every start of the API; a role change, approval or removal re-sets that person's menu. Telegram may need the chat reopened to show it. |
| 2026-10-16 | **CEO brief: goals, tasks and reports first, no «Касса»** (the owner): the 08:00 picture now opens with the vision and mission (`COMPANY_VISION`, `COMPANY_MISSION` — hidden until set), this month's goals (/maqsad, result vs the month's pace, behind in red), the tasks (overdue, blocked, waiting for his check, due today) and who didn't report; then sales, stock, debt, payments, shops, attendance. Cash is no longer in the brief (still in the analyst and the cash calendar). `docs/agent-specs/01-ceo-daily-brief.md`. |
| 2026-10-15 | **Daily reports until 22:00, minus points** (the Director): the 16:00 ask and 17:00 reminder say «соат 22:00 гача»; after 22:00 no report is taken and ✏️ / 🗑 stop. At 22:00 every open ask costs `REPORT_MISS_PENALTY` KPI points (default 1, env setting; 0 = none) off that month's total, and the person gets one polite line with today's minus and the month's sum (🔴). `/kpi`, the KPI table and PDF show the minus in red. Runs inside the API every 5 minutes — no new cron. Starts with 16.10's asks. Spec: `06-daily-reports.md`, `13-kpi.md`. |
| 2026-10-15 | **SAP supplier balance sign fixed in the analyst:** a negative SAP balance is our debt, a positive one an advance (checked against 1C: 21 of 35 agree only that way). The analyst read it the other way and said «SAP: 234 млн қарз» while the comparison said «аванс»; the comparison was right. |
| 2026-10-15 | **`/db`: closed tables show their structure** — click 🔒 a table: row count, columns and their types, when rows were written (newest, oldest, rows per day for 14 days) and, for the SAP pushes, rows per kind with the last write. Values stay closed (the Director's order of 07.10.2026). **What the database keeps from SAP / 1C / Billz / Verifix** and what can be deleted safely: `docs/sap-data-check-2026-10-15.md` §5 — only the two SAP copy tables come back by themselves (next push); 1C, Billz and Verifix are read live, nothing stored. |
| 2026-10-15 | **The Бухгалтер test, fixes** (`docs/sap-data-check-2026-10-15.md` §4): SAP supplier balances were "0" when a period was asked (they have no dates) — undated SAP kinds now ignore the period; `BalanceSys` is labelled so'm; 1C revenue is 9020 / 9030 and cost 9120 here (9010 / 9110 are empty) — the tool and `knowledge.py` say so and an empty account names its neighbours with rows; new tool `billz_sap_check` (which cheques aren't in SAP) for accounting with Billz + SAP; refusals say attendance / KPI / tasks are the Director's only; the 1C ↔ SAP supplier pairing drops Latin legal forms (YaTT, LTD, S.R.L.…) — UMAROVA and Tintolav now pair (33 → 35). Corrected: the brief's customer debt total was right — only «камида» was wrong. |
| 2026-10-15 | **Admin Bot `/xodimlar` toggles fixed:** the SAP / 1C / Billz and 🌐 language buttons failed since 10.10 — the database didn't allow `data_access` / `lang` in `employee_changes`, the change was saved but its log line wasn't, the bot answered an error and Telegram re-sent the press, so the toggle flipped back and forth (and the Director wasn't told). The database now allows them (selfcheck compares every logged field with the schema), and both Telegram webhooks answer 200 even when a handler fails, so a press is never replayed — the log keeps the error type and line only. Check each person's access on `/xodimlar` once after the deploy. |
| 2026-10-15 | **The Director's brief checked against SAP** (`docs/sap-data-check-2026-10-15.md`): customer debt was overwritten every :17/:47 by an old duplicate push task (capped list, full totals) — now ignored while the complete list is newer; stock value was 0 everywhere (the gateway used the item-level price) — `sap-gateway/src/stock-value.js` now uses SAP's per-warehouse value (deploy it to the gateway computer); Garmin complaints carry their own codes; the Garmin manager can see which cheques aren't in SAP (`garmin_data` → `billz_sap`); Garmin stock of exactly 0 shows as "no data". |
| 2026-10-14 | **Removal and re-registration fixed:** removing someone deletes their AI memory, closes held messages/files, cancels their open tasks and data access; registering the same Telegram account again starts fresh — the name is asked again, settings, duties and language reset, the new @username kept. People who left no longer appear in overdue notices. **AI answers in order:** one person's questions are answered one at a time, each as a reply to its question. **Garmin менежер:** data opens with the role (no Boss confirmation — the owner); the person is told what opened at registration. |
| 2026-10-14 | **Code-review fixes:** a proof card that can't reach the assigner sends the task back for proof (never stuck in review); captions fitted to 1024 without cutting HTML; proof only within 30 min of ✅ and never a Reply to another message; the early button answer waits 1 s and never takes a cheer reply; the **Garmin manager's data opens only after the Director's ✅** and covers **Garmin's SAP only** (`garmin_sap`); a role change sends one notice; returned tasks can be flagged overdue again; KPI counts tasks with proof; complaints during the emergency stop still become tickets; the client's language stays on resolved cards; both SAP warnings in the Garmin brief; the two briefs share `integrations/reports/brief_parts.py`; the old task keyboard removed. |
| 2026-10-14 | **New role «Garmin менежер»** (`garmin_manager`, picked at registration, approved in Admin Bot): every Garmin complaint goes to him (not Garmin сотув); SAP, Billz and Garmin's own records through the bot come with the role; a **Garmin morning brief** at 08:00 (the same picture as the Director's, Garmin only); the Billz → SAP check goes to him once off trial. The Director is told when the role is given or taken. `docs/agent-specs/23-garmin-manager.md`. |
| 2026-10-14 | **Fixes from a project scan:** the hourly complaint question reached people who then couldn't press it (now they can, and it follows whoever holds the role); the weekly data check missed blocked / in-review tasks; 1C's comparison read the machine's clock instead of Tashkent's; the work AI's Garmin catalog, IT's report and the bot's company knowledge didn't know the new role. |
| 2026-10-14 | **Cheque photos → accounting** (the owner): a photo the Director sends to OPS Manager Bot is read by the AI (`integrations/org_bot/cheques.py`). A cheque with all eight details (фискал белги, ФМ рақами, чек рақами, сана, RRN, сумма, товар, ИНН) goes to Бухгалтерия as the photo with the details under it; anything missing or wrong (bad ИНН / RRN, future or 90+ day date, not so'm, already sent) → the Director is asked (📨 send anyway / 📋 not a cheque / ❌ cancel). Not a cheque → the people list as before. Readings kept in `cheques` (not in `/db`); logs keep counts only. The owner's exception to "the AI never reads files" — the Director's photos only. `docs/agent-specs/23-cheques.md`. Also: selfcheck no longer needs a database (three older suites reached it). |
| 2026-10-13 | **Complaints go to the department, not the Boss** (his answer of 13.10; spec A4/A5): the route table `integrations/org_bot/routes.py` sends Londry complaints to the Londry role and Garmin ones to Garmin сотув. Each complaint is a ticket (code T-…) with ✅ Ҳал бўлди → reason code; an hour later, if open, "ҳал бўлдими?" ✅/❌ (09:00–20:00, max 3). Nobody in the role → the Boss. "Director" = the owner = the Boss. |
| 2026-10-13 | **Task register (the owner's spec of 11.10, Day 1):** every task has a code (T-2026-10-0147, old tasks too) and a state — Яратилди, Қабул қилинди, Тўсиқ бор, Текширувда, Ёпилди. The card's buttons: ▶️ Қабул қилдим · ⛔ Тўсиқ бор · ✅ Бажардим. ✅ asks for proof (photo, file or text); the assigner gets it with ✅ Қабул / ↩️ Қайтариш; ⛔ sends the reason. Every step in `task_events`. `integrations/org_bot/task_register.py`, `docs/agent-specs/08-task-tracker.md`. |
| 2026-10-13 | **Faster buttons:** each Telegram call used to open a new HTTPS connection (~400–650 ms each, 2–4 per button press). Now one kept-alive connection per bot is reused (`integrations/telegram/bot.py`, `_shared_client`), and if a handler hasn't answered a button within 0.25 s the bot stops the spinner itself (`ack_soon`). |
| 2026-10-10 | **Data for accounting and finance:** on `/xodimlar` the admin gives SAP / 1C / Billz to people in Бухгалтерия or Молия; they ask in OPS Manager Bot and the analyst uses only their systems' tools (never attendance or the bot's records). The Director is told of every change with ⛔ to close it; a role change or removal closes it. **AI language per person** (ўзбекча / русча) — the accountant gets Russian answers. The supplier-debt `.xlsx` now comes in Uzbek **and Russian**, each with every column explained under the table (SAP's own column names). `docs/agent-specs/22-data-access.md`. |
| 2026-10-10 | **Database backups (IT-5):** the server makes an encrypted `pg_dump` (owner's GnuPG public key) on request; an office computer fetches one daily, checks its SHA-256, keeps 30 days + 12 months and a second copy; monthly restore test script; the technical report shows the last copy. New settings `BACKUP_SECRET`, `BACKUP_PUBLIC_KEY`; the image now has `gnupg` and `postgresql-client-16`. `scripts/backup/README.md`. |
| 2026-10-10 | `docs/links.md` (every link) and `docs/it-instruction-2026-10.md` (the owner's IT instruction against what exists). The bot's company knowledge now names three businesses: PRIMUS (B2B equipment), LONDRY (self-service laundries), Garmin. |
| 2026-10-10 | OPS Manager knows the company (`integrations/org_bot/knowledge.py`) and, when it doesn't understand, asks one question with 2–3 meanings as buttons. The Director's temporary permission of 09.10 marked closed. |
| 2026-10-10 | Supplier debt 1C ↔ SAP from OPS Manager Bot: a short comparison, and the `.xlsx` on «солиштир» / «excel» / «файл» (`integrations/onec/payables.py`). |
