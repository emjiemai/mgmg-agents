# Build prompt — MGMG mobile app (paste this into a new Claude Code session)

> Copy everything below the line into the first message of a new Claude Code
> session opened on `D:\mgmg-command-center-main`.

---

# MGMG Command Center — mobile app (iOS + Android)

## Who I am and how I want you to work

I am the IT specialist / AI agent builder at Primus Laundry (MGMG). I am the
only developer on this project. You are my engineering partner, not an
order-taker.

Working agreements, in priority order:

1. **Verify, never guess.** Read the actual code before you describe it. If you
   are unsure whether something exists, grep for it. Never invent an API,
   a column name, a library version or a price. If you can't verify something,
   say "I couldn't verify this" instead of producing a confident sentence.
2. **Push back.** If I ask for something that is a bad idea, expensive, or
   solvable in a simpler way, say so before writing code. I would rather
   argue for five minutes than rebuild for a week.
3. **Small, reviewable steps.** One concern per commit, with a message that
   says why, not just what. Push after each working step (I authorize commits
   and pushes to `origin` for the whole project — ask only before anything
   destructive, like deleting data or force-pushing).
4. **Ask when a decision is mine.** Product decisions, money, anything visible
   to employees: ask. Implementation details: decide and tell me.
5. **Report honestly.** If tests fail, show the output. If you skipped
   something, say so. Never say "done and working" about something you did not
   run.
6. **Comments explain why, not what.** Match the style already in this repo:
   docstrings with Args/Returns, and comments that record the reason a
   decision was made (including dates for business decisions).

Languages in the product: employee-facing text is **Uzbek (Latin)**;
permission SOP text is **Uzbek (Cyrillic)** because the legal document is in
Cyrillic. Code, comments, docs and commits are in **English**.

## The existing system (read it before proposing anything)

Repo: `D:\mgmg-command-center-main`, pushed to
`github.com/emjiemai/mgmg-agents`, deployed on Render via `render.yaml` with
auto-deploy on push to `main`.

Stack: **Python 3.11, FastAPI, psycopg3 + PostgreSQL 16, httpx, loguru,
pydantic-settings, Docker**. AI goes through **OpenRouter** (Gemini 3.8 Flash,
fallback 3.7) in `integrations/ai/openrouter_client.py`.

What exists today, all driven through **two Telegram bots**:

- `integrations/api/app.py` — FastAPI service (`mgmg-api`). Today it exposes
  only `/health` and webhook endpoints for Telegram and SAP. **There is no
  REST API for clients and no authentication layer. That is the biggest gap.**
- `integrations/org_bot/` — OPS Manager Bot: task routing, employee messages,
  daily reports (`ops_manager.py`, `kpi.py`), written permissions
  (`permission_flow.py`, `permissions.py`, `docx_form.py`), roles
  (`roles.py`), all database access (`store.py`).
- `integrations/telegram/bot.py` — Telegram client (send message, document,
  keyboards, HTML escaping).
- `agents/` — `ceo-daily-brief`, `daily-reports`, `receivables`, `lead-agent`,
  run as Render cron jobs.
- `database/schema.sql` — the whole schema, currently **self-applied on every
  database pool open** (`integrations/common/db.py`). Key tables: `employees`,
  `access_requests`, `tasks`, `task_updates`, `daily_reports`,
  `permission_requests`, `permission_request_events`, `conversation_turns`,
  `agent_actions`, plus SAP/CRM snapshot tables.
- `scripts/selfcheck.py` — the current test suite (pure functions only, no
  network, no database). It must keep passing at every step.
- `docs/agent-specs/` — specs per agent. `06-daily-reports.md` and
  `07-permissions.md` describe the two flows the app must support.

Business rules already encoded (do not silently change any of them):

- Roles are in `integrations/org_bot/roles.py`: `b2b_sotuv`, `it`,
  `buxgalteriya`, `hr`, `ombor`, `operatsion_direktor`, `mobilograf`,
  `aloqa_markazi`, `garmin_sotuv`. `operatsion_direktor` is the Director.
- **Daily reports:** asked at 16:00 Tashkent (Mon–Fri), reminder at 17:00,
  accepted only until midnight of that same day. A vague report ("ok",
  "ishladim") is saved and gets exactly **one** AI follow-up question; the
  answer is appended and nothing more is asked. Individual reports are **not**
  forwarded to the Director — he only sees, in the 08:00 brief, who did not
  report.
- **Written permissions (EMJ-SOP-ADM-01):** the bot asks the SOP form's
  questions one at a time; each answer is AI-checked and re-asked if unclear;
  accepted answers are rewritten into Uzbek Cyrillic with spelling fixed but
  facts, numbers, dates and names unchanged; the company's own .docx template
  (`integrations/org_bot/templates/EMJ-SOP-ADM-01.docx`) is filled, never
  regenerated; signatures are left blank for hand signing; **nobody may
  approve their own request**; approvers are the Director plus the ids in
  `PERMISSION_DEPUTY_TELEGRAM_IDS`.
- Timezone is **Asia/Tashkent** everywhere (`integrations/common/timeutil.py`).
- Feature switches live in the Render environment group `mgmg-shared`:
  `DAILY_REPORTS_ENABLED`, `PERMISSIONS_ENABLED`, `LEAD_AGENT_ENABLED`,
  `BOTS_FROZEN`, `AGENT_WRITES_ENABLED`.

## What we are building and why

My manager wants this to become a **mobile app for iOS and Android** instead of
living in Telegram. The app must be **lightweight**: a few screens that do
real work, not a platform.

In scope for v1:

1. **Login** for employees (no Telegram account needed).
2. **Daily report**: write today's report, see whether it was accepted, see
   the AI follow-up question if there is one and answer it, see my own last 14
   days.
3. **Written permission (EMJ-SOP-ADM-01)**: fill the form question by question
   with the same AI checking, review it, submit it; approvers get it, decide
   with the SOP's four outcomes, and both sides get the filled .docx.
4. **Director home**: today's brief (yesterday's reports, who did not report)
   and the overdue receivables headline.
5. **Push notifications**: the 16:00 report request, the 17:00 reminder, a new
   permission request for approvers, a decision for the requester.

Explicitly **out of scope** — do not build these, and tell me if I ask for one
without realising the cost:

- A chat screen talking to the AI. That is rebuilding Telegram, badly.
- Offline mode and sync. The most expensive feature in mobile development.
- Charts, dashboards, analytics screens. Numbers as text are enough.
- Admin and role management screens — those stay in the Telegram admin bot.
- Anything that writes to SAP.
- File uploads, photos, voice messages.
- Dark/light theming systems, animations, custom design systems. System
  defaults, clean and boring.

**The Telegram bots keep running unchanged in parallel.** The app is a second
client on the same backend. Do not remove, disable or rewire Telegram
behaviour to make the app easier. A single user must be able to use either one
on the same day without the data going wrong.

## Technical decisions already made (challenge them only with a concrete reason)

- **Mobile: React Native + Expo (managed workflow), TypeScript.** Chosen for
  one codebase and for EAS OTA updates, because I am one developer and need to
  ship a fix without a store review.
- **Backend stays Python + FastAPI in this same repo.** No rewrite. The app
  lives in this repo under `mobile/` so the API contract and the client evolve
  in one commit. `mobile/` must not affect the Docker build.
- **Database stays PostgreSQL on Render**, but we move off the free plans:
  the free web service sleeps (a ~2 minute cold start, unacceptable in an app)
  and the free database expires 30 days after creation. Assume Starter plans.
- **All AI calls stay server-side.** No API key of any kind is ever shipped in
  the app bundle.
- **Auth: invite-code + JWT.** No passwords, no SMS (SMS costs money per
  message). The admin issues a one-time code from the Telegram admin bot; the
  app redeems it once and stores a refresh token in the device keychain.

## Build it in phases, and stop for my review after each one

### Phase 0 — Plan and decisions (no code)

Read the repo, then give me:

- A written summary of what has to be refactored and why.
- The full REST API contract you propose (paths, methods, request/response
  shapes, error format, status codes).
- The auth design in detail, including what happens when a phone is lost.
- The database changes needed.
- The screen list with a rough sketch of each one's content.
- Risks, with the ones you think I am underestimating named explicitly.
- Anything in this prompt you disagree with.

Do not write application code until I approve this.

### Phase 1 — Service layer refactor (backend, no app yet)

Today the business logic and Telegram formatting are tangled together:
`ops_manager.py` and `permission_flow.py` decide things *and* build HTML
strings and inline keyboards in the same functions. An app cannot consume
that.

Extract the decisions into a **transport-agnostic service layer** (suggested:
`integrations/services/reports.py`, `integrations/services/permissions.py`)
that takes plain arguments and returns plain data (dataclasses / pydantic
models), with **no Telegram HTML, no keyboards, no emoji**. Telegram and the
REST API both become thin presenters on top.

Rules for this phase:

- Behaviour must not change. Every existing Telegram flow works exactly as
  before, including the AI checks, the midnight cutoff, the one follow-up
  question, the self-approval ban and the .docx filling.
- `scripts/selfcheck.py` keeps passing at every commit.
- Add **pytest** with real unit tests for the new service layer, and wire
  `pytest` + `ruff` into a GitHub Actions workflow.
- Replace the self-applying `schema.sql` with **Alembic** migrations, with the
  current schema as the baseline. An old app version must never meet a schema
  it cannot handle.

### Phase 2 — REST API

Add a versioned API under `/api/v1` in `integrations/api/`, built on the
Phase 1 services. Requirements:

- **Auth:** `POST /api/v1/auth/redeem` (one-time code → access + refresh
  token), `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`.
  Access token ~15 minutes, refresh token ~30 days, rotated on use, stored
  hashed server-side so it can be revoked. A revoked employee
  (`employees.status = 'revoked'`) is locked out immediately.
- **Endpoints** (propose the final shape in Phase 0):
  `GET /me`, `GET /reports/today`, `POST /reports/today`,
  `POST /reports/today/followup`, `GET /reports/history`,
  `POST /permissions` (start), `POST /permissions/{id}/answer`,
  `POST /permissions/{id}/submit`, `GET /permissions` (mine / to decide),
  `POST /permissions/{id}/decide`, `GET /permissions/{id}/document`,
  `GET /brief/today` (Director only), `POST /devices` (push token).
- **Authorization on every endpoint**, by role, enforced server-side. Assume
  the client is hostile: an employee must not be able to read another
  employee's report or decide any permission request.
- Rate-limit the auth endpoints. Log every state change to `agent_actions`,
  as the rest of the project already does.
- A `min_supported_app_version` field in `GET /me` (or a small
  `/api/v1/version` endpoint) so the server can force an upgrade.
- OpenAPI docs must be accurate — the app's types are generated from them.
- Tests for every endpoint, including the forbidden cases.

### Phase 3 — The app (Expo)

`mobile/`, TypeScript, Expo managed, React Navigation, TanStack Query,
`expo-secure-store` for tokens, `expo-notifications` for push. No state
management library beyond Query unless you can justify it.

Screens, and nothing more:

1. **Login** — enter the one-time code, done.
2. **Home** — role-aware: for an employee, today's report status and a button;
   for the Director, the brief plus the list of permission requests awaiting
   his decision.
3. **Daily report** — text box, submit, the AI follow-up question inline if
   one comes back, plus my last 14 days as a simple list.
4. **Permission request** — one question per screen, exactly the SOP order,
   the AI's re-ask shown inline, a review screen before submitting.
5. **Permission detail** — the request, and for an approver the four SOP
   buttons; a conditional approval, rejection or information request asks for
   the text first. A link to open the filled .docx.
6. **Settings** — who I am, language, log out.

Requirements: Uzbek (Latin) UI strings in one i18n file from day one, with the
SOP wording in Cyrillic where the document requires it. Every screen handles
three states honestly: loading, empty, and error with a retry. No fake data,
ever, not even as a placeholder.

### Phase 4 — Push, release and rollout

- Expo push tokens per device, stored server-side, with dead tokens cleaned
  up. Notifications sent from the existing cron agents alongside the Telegram
  messages, not instead of them.
- EAS build profiles, OTA update channel, and a short runbook for me:
  how to build, how to ship an OTA fix, how to roll back.
- A rollout plan: which two or three people test first, what we watch, and
  when we consider turning a Telegram flow off — **if ever**.

## Things I need to do myself (tell me when each becomes blocking)

- Apple Developer Program, $99/year — without it there is no iOS build.
- Google Play developer account, $25 once.
- Upgrading the Render web service and database off the free plans.
- Deciding how employees get their one-time codes.

## Definition of done for v1

- An employee installs the app, logs in with a code, and files their daily
  report; the same data is visible to the existing Telegram flows and to the
  `xodimlar_kpi` agent.
- An employee files a permission request in the app; the Director receives it
  and decides; both get the filled EMJ-SOP-ADM-01 .docx; the register is
  correct.
- The Telegram bots still work exactly as they do today.
- CI is green: `ruff`, `pytest`, `scripts/selfcheck.py`.
- `docs/` explains the API, the auth model and the release process well enough
  that someone who is not me could take over.

## Start here

Read the repo, especially `integrations/api/app.py`,
`integrations/org_bot/ops_manager.py`, `integrations/org_bot/permission_flow.py`,
`integrations/org_bot/store.py`, `database/schema.sql`, `render.yaml` and
`docs/agent-specs/`. Then give me **Phase 0** — and tell me what you think is
wrong with this plan before you write a line of code.
