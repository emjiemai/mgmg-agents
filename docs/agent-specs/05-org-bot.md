# Admin Bot + OPS Manager Bot (org_bot)

**Code:** `integrations/org_bot/` (`roles.py`, `store.py`, `prompt.py`, `admin.py`, `ops_manager.py`)
**Runs inside:** `integrations/amocrm/webhook_handler.py` (the `mgmg-api` web service) — neither bot is a cron job or a standalone process
**Mode:** writes to Postgres + Telegram directly (no write gate — these tables aren't touched by any other agent, so `AGENT_WRITES_ENABLED` doesn't apply here the way it does to SAP/amoCRM/Sheets writes)
**Owner:** Operations Director

## Purpose

Two bots implementing the org chart the business owner sketched: the
Operations Director gives a task in plain language to **OPS Manager Bot**,
which decides whether it's for one of 8 human roles or one of 4 AI agents and
sends it. **Admin Bot** is a separate, admin-only bot that turns an
unregistered user's first message into an Accept/Reject decision.

## Why two bots, and why employees never talk to Admin Bot

Telegram cannot be cold-messaged: a bot may only DM a user who has already
started a chat with *that specific bot*. So the flow is built so every
employee-facing message — including the post-approval role picker — comes
from OPS Manager Bot, the one chat the employee already started. Admin Bot
exists only to receive the admin's own Accept/Reject tap.

## Flow — join & role assignment

1. An unregistered `telegram_user_id` messages OPS Manager Bot (anything —
   even `/start`). If they already have a pending request, they're told so and
   nothing else happens (a partial unique index prevents duplicate pending
   rows from repeated messages, not just app-level logic).
2. Otherwise, an `access_requests` row is created and **Admin Bot** posts a
   card to the admin's chat: requester name + `tg://user?id=` profile link +
   Accept/Reject buttons.
3. The admin taps a button. The decision is idempotent — a second tap (or a
   race between two admins) reports the existing outcome instead of
   double-processing. The card is edited in place to show the result.
4. On Accept, **OPS Manager Bot** (not Admin Bot) sends the original requester
   an 8-button role picker.
5. The requester taps their role → an `employees` row is created → confirmed.

## Flow — task routing

6. A message from a registered `operatsion_direktor` gets an immediate "got
   it, routing…" reply (no AI call — cheap UX so the Director doesn't re-send
   while waiting), then classification runs **in the background**
   (`FastAPI BackgroundTasks`), not inline in the webhook handler. This
   matters: a slow AI call risks a Telegram-side webhook retry, which would
   re-run classification and could double-dispatch the same task. The
   `tasks` table's own unique constraint
   (`director_telegram_user_id, source_message_id, assigned_employee_id`) is
   the backstop against that.
7. Classification (`integrations/org_bot/prompt.py:CLASSIFY_SYSTEM_PROMPT`) is
   one AI call against a **closed 12-way enum** (8 roles + 4 agents + "none"),
   not free text. The model's raw output is validated in code
   (`ops_manager.validate_classification`) against `roles.py`'s known slugs
   before anything is trusted — this is the proportionate backstop for a
   bounded enum, versus Lead Agent's full second-pass verification (which
   exists because open-ended lead qualification has a much wider failure
   surface). An unrecognized or missing target — including the model's own
   "none" — asks the Director to clarify rather than guessing.
8. **Target is a human role**: every *active* employee with that role gets
   their own `tasks` row and their own task card with a "✅ Mark Done" button.
   Zero active employees for that role → the Director is told so explicitly,
   not left with silence.
9. **Target is an AI agent**: no live invocation. Each agent's most recently
   *already-computed* output is read and handed to a second AI call along
   with the Director's question, which answers directly. Deferred
   deliberately — see Known gaps.
10. Any employee tapping "Mark Done" resolves their task idempotently and
    best-effort notifies the Director. Free text from a non-Director employee
    gets a canned "only the Director can assign tasks here" reply — no NLP on
    the employee side, keeps v1 fully deterministic.

## Agent-name mapping (org-chart name → what actually gets read)

| Org-chart name | Repo agent | Data source |
|---|---|---|
| Lead Agent | `agents/lead-agent` | Leads Google Sheet, last 15 rows |
| Finance Agent | `agents/receivables` | `v_ar_aging_latest` + recent `alerts WHERE agent='receivables'` |
| CRM agent | `agents/amocrm-followup` | `v_pipeline_latest` + recent `amocrm_deal_events` |
| Reporter Agent | `agents/ceo-daily-brief` | `daily_briefs`' denormalized headline figures (cash, AR overdue, pipeline, new leads, etc.) |

If "Finance Agent" was meant as the cash/financial section of the CEO brief
rather than receivables specifically, `_fetch_finance_agent_data` in
`ops_manager.py` needs to change what it queries.

## Data model

`employees`, `access_requests`, `tasks` — appended to `database/schema.sql`,
self-applying like every other table (no migration tooling in this project).
Role slugs are a hardcoded CHECK constraint mirrored in `roles.py` — Postgres
can't import that file, keep the two in sync by hand if roles ever change.

Deliberately **no `chat_id` column anywhere**: for a private 1:1 bot chat,
Telegram's `chat.id` *is* the user's `telegram_user_id`.

## Setup — register both webhooks once

```
https://api.telegram.org/bot<ADMIN_BOT_TOKEN>/setWebhook?url=https://<host>/webhooks/telegram/admin/<ADMIN_BOT_WEBHOOK_SECRET>
https://api.telegram.org/bot<OPS_MANAGER_BOT_TOKEN>/setWebhook?url=https://<host>/webhooks/telegram/ops/<OPS_MANAGER_BOT_WEBHOOK_SECRET>
```

Each Telegram route checks its own secret and always constructs `TelegramBot`
with that specific bot's own token — never the shared
`telegram_primary_bot_token` fallback the original `/webhooks/telegram/{secret}`
route uses (see Known gaps — that fallback has a real, separate, pre-existing
issue this design deliberately does not repeat).

Create both bots via @BotFather like every other bot in this project — free,
one bot per purpose, never share a token.

## Runbook

**Nothing happens when someone messages OPS Manager Bot?** Check the webhook
is actually registered (`GET https://api.telegram.org/bot<TOKEN>/getWebhookInfo`)
and that `OPS_MANAGER_BOT_WEBHOOK_SECRET` matches what's in the URL.

**Admin never gets a join-request card?** Check `ADMIN_BOT_TELEGRAM_CHAT_ID` is
the admin's real chat id, and that the admin has started a chat with Admin Bot
at least once (same cold-message restriction applies to Admin Bot itself).

**A task never reaches an employee?** Check `employees` for an `active` row
with the right `role` — a task to a role with zero active employees is
reported back to the Director, not silently dropped, so check the Director's
own chat first for that message before assuming it's a bug.

## Explicitly cut from v1

No task edit/cancel. No employee revocation UI (the `status='revoked'` value
exists in the schema, nothing sets it yet). No group-chat task delivery — every
task lands in a personal DM. No admin self-service role-list editing. No live
per-agent "ask" endpoints — v1 answers from each agent's already-computed
output only; add a real on-demand query path per agent if that proves
insufficient for a specific question shape. No mandatory Director
confirm-before-send on routing decisions — routing is auditable via
`agent_actions`/`tasks` but not gated on a second tap.

## Known gaps

- `handle_callback_query` in `integrations/telegram/bot.py` (used by the
  original `/webhooks/telegram/{secret}` route, for payment/contract/HR
  approvals) doesn't check whether its guarded UPDATE actually changed a row,
  and that route always answers via `telegram_primary_bot_token` regardless of
  which agent's bot actually sent the approval — so message edits on that
  route are silently wrong for every bot except whichever one happens to share
  the primary token. Neither issue is inherited here (every org_bot guarded
  update checks rows-affected; every org_bot route uses its own bot's token
  explicitly) — but the original route itself is unfixed.
- Model choice (`OPS_MANAGER_BOT_MODEL=deepseek-v4-pro`) is unproven as a
  *primary* model under real traffic — it has only run as someone else's
  fallback tier before. Watch early runs the way Lead Agent's DeepSeek
  reliability was watched.
