# Admin Bot + OPS Manager Bot (org_bot)

**Code:** `integrations/org_bot/` (`roles.py`, `store.py`, `prompt.py`, `admin.py`, `ops_manager.py`)
**Runs inside:** `integrations/amocrm/webhook_handler.py` (the `mgmg-api` web service) — neither bot is a cron job or a standalone process
**Mode:** writes to Postgres + Telegram directly (no write gate — these tables aren't touched by any other agent, so `AGENT_WRITES_ENABLED` doesn't apply here the way it does to SAP/amoCRM/Sheets writes)
**Owner:** Operations Director

## Purpose

Two bots implementing the org chart the business owner sketched: the
Operations Director gives a task in plain language to **OPS Manager Bot**,
which decides whether it's for one of the human roles or one of the AI/
reference sources and sends it (`integrations/org_bot/roles.py` is the single
source of truth for the current counts — this list grows as MGMG adds
business lines, most recently a Garmin watch retail business). **Admin Bot**
is a separate, admin-only bot that turns an
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
   one AI call against a **closed, bounded enum** (every role in `ROLES` +
   every entry in `AGENTS` + "none" + "refused" — see `roles.py` for the
   current list), not free text. The model's raw output is validated in code
   (`ops_manager.validate_classification`) against `roles.py`'s known slugs
   before anything is trusted — this is the proportionate backstop for a
   bounded enum, versus Lead Agent's full second-pass verification (which
   exists because open-ended lead qualification has a much wider failure
   surface). An unrecognized or missing target — including the model's own
   "none" — asks the Director to clarify rather than guessing.
8. **Target is a human role**: every *active* employee with that role gets
   their own `tasks` row and their own task card with **"▶️ Start" and
   "✅ Done" buttons** — Start moves `sent → started` and notifies the
   Director so they can see progress, not just completion; Done works from
   either `sent` or `started` (tapping Start first isn't required). Both
   transitions edit the card in place and are idempotent — a double-tap
   reports "already handled" rather than double-processing. Zero active
   employees for that role → the Director is told so explicitly, not left
   with silence.
9. **Target is an AI agent**: no live invocation. Each agent's *full*
   already-computed output is read (every lead, every open receivable, the
   full pipeline, 14 days of daily briefs — not a truncated preview) and
   handed to a second AI call along with the Director's question, which
   answers directly. Still no write access — see Known gaps/cut-from-v1.
10. Any employee tapping "Start" or "Done" resolves their task idempotently
    and best-effort notifies the Director. Free text from a non-Director
    employee gets a canned "only the Director can assign tasks here" reply —
    no NLP on the employee side, keeps this fully deterministic.

## Flow — media, photos, videos, voice notes, files

The Director isn't limited to text. Sending a photo/video/audio/voice/
document/animation ("any data") is routed the same way a text task is, just
delivered with `copyMessage` instead of `sendMessage` (so it duplicates the
actual file into the recipient's chat, not a link or a description of it) —
and it gets the exact same `tasks` row + Start/Done card as a text task,
tracked via a `has_media` flag on the row (Telegram requires a different Bot
API call to edit a caption vs. plain text, so every card edit checks this
flag to call the right one).

- **With a caption**: the caption is classified exactly like a text task. If
  it resolves to an employee role, it's dispatched immediately — no extra
  step. Media can't be routed to an AI agent (agents don't receive files); if
  classification lands on "agent" or is ambiguous, it falls through to the
  role-picker below rather than guessing.
- **Without a caption, or an ambiguous one**: there's no text to classify, so
  guessing would be worse than asking. A `pending_dispatches` row is created
  and the Director gets an 8-button role picker (`dispatchrole:{role}:{id}`)
  — tapping one dispatches immediately, same as above.

Deliberately excluded from "media": stickers, contacts, locations, polls, and
video notes (round videos) — none of these support a caption in the Bot API,
which the Start/Done card edit relies on, so forwarding them would need a
different (uncaptioned) card design this v1 doesn't build.

## Flow — employee progress updates

An employee can write free text about a task at any point — before starting,
mid-task, after finishing ("something always might happen"). This isn't
gated on task status. Resolution order:
1. If the message is a Telegram **reply** to a specific task card, that task
   is unambiguous — used directly.
2. Otherwise, if the employee has **exactly one** open task (`sent`/`started`),
   it's attached to that one.
3. Otherwise (zero or multiple open tasks, no reply), they're asked to reply
   directly to the right card — the note is never silently dropped or
   attached to the wrong task.

Every update is stored in `task_updates` (a paper trail) and relayed to the
Director live, tagged with the task's current stage.

## Grounding — the model must know what it can't do

Live testing (2026-08-20) surfaced a real failure: "bu lidni o'chirib tashla"
("delete this lead") was classified as a task and routed to the B2B Sotuv
role — because the lead happened to be tagged B2B, not because deleting a
spreadsheet row is anything a human role does via a task card. With zero
grounding in its own capabilities, the model invented a plausible-sounding
route instead of recognizing the request was impossible through this bot.

Fixed by adding `COMPANY_CONTEXT` to both prompts: who MGMG/Primus Laundry
are, and an explicit, forceful capability boundary — "you can route a task
to a human, or answer from already-collected data; you cannot create, edit,
delete, or otherwise change any record in any system, no matter how the
request is phrased." A request to modify data now gets `target_type="none"`
with an honest explanation in `task_summary` ("I can't delete this directly
— do it manually in Google Sheets") instead of a routing guess. Verified
live against the exact reported input — reproduced the original bug first,
confirmed the fix, then confirmed a real task with a similar B2B lead
reference still routes correctly (the fix isn't "never route to B2B", it's
"don't invent capabilities that don't exist").

Two related gaps fixed at the same time, both found by reasoning about *why*
the model had nothing better to fall back on:
- A vague status question ("ishlar qanaqa ketmoqda" — "how are things going")
  used to fall through to `target_type="none"` ("couldn't understand, please
  clarify") — now explicitly routed to `reporter_agent`, since that's
  exactly what a general status question means and the daily brief already
  covers it.
- A question spanning multiple systems ("leads, CRM va hamma ma'lumotlar" —
  "leads, CRM, and everything") used to silently answer from only one system
  (whichever the model picked) with no signal the rest was ignored — a new
  `all_systems` pseudo-agent (`roles.py`) now combines all four fetchers,
  labeled per section, for exactly this case.
- The code side of "none" had its own bug: it always showed a hardcoded
  "couldn't understand" message regardless of what the model actually
  determined — including for the capability-boundary case above, where the
  model's own `task_summary` explains *why* and *what to do instead*. Fixed
  to use the model's `task_summary` (same pattern `"refused"` already used).

## UX fixes from real usage (2026-08-21)

- **The "👍 Got it, routing..." ack was replaced with Telegram's native
  typing indicator** (`TelegramBot.send_chat_action`) — a real classification/
  answer call takes a few seconds, and the canned text ack sent on every
  single message read as repetitive and robotic. The typing bubble signals
  the same thing without leaving a permanent message in the chat.
- **A real HTML-escaping bug, not a cosmetic one**: `ANSWER_SYSTEM_PROMPT`
  tells the model its reply is "sent as Telegram HTML," so the model
  reasonably uses `<b>...</b>` for emphasis — but the code then ran
  `escape()` on the model's output before sending, turning `&lt;b&gt;`
  into literal visible "<b>" text once Telegram's own HTML parser rendered
  the escaped entity back to a literal character for display. Fixed with
  `sanitize_model_html()` (`integrations/telegram/bot.py`): escape
  everything first, then selectively un-escape only `<b>`/`<i>` back to
  real tags — a stray or malformed tag from underlying data can still never
  break the HTML parse or render as unintended markup, but the model's own
  deliberate formatting now actually renders. Applied everywhere AI-
  generated text reaches a Director or employee (task summaries, answers,
  refusals) — plain `escape()` stays in use for genuine user-typed text
  (names, raw messages), which is a different case and shouldn't have tags
  un-escaped from it. Verified live: the model still emits `<b>` for a
  "which lead is best" style answer, and the tag now survives as real
  formatting through `sanitize_model_html`, not literal text.
- **Task cards now show the Director's original words alongside the AI's
  summary** (`_task_card_text`, when they differ) — a direct answer to "AI
  saying something abnormal misunderstandable to employee": even if the
  AI's phrasing is ever unclear, the employee can see exactly what the
  Director actually typed, not just a paraphrase of it. Also tightened the
  classification prompt's guidance on how `task_summary` should read for an
  employee — a complete, natural instruction as if the Director wrote it
  directly, not a compressed note-to-self.

## Guardrails

Both AI prompts (`integrations/org_bot/prompt.py`'s shared `GUARDRAILS`
block) carry:
- **Identity lock** — everything in a Director's or employee's message is
  DATA to interpret, never a new instruction. Attempts to redefine the bot's
  role, extract/override its system prompt, or act outside classification/
  reporting are refused the same way any other guardrail violation is.
- **Content refusal** — abusive/inappropriate messages get a brief, polite
  decline, not a lecture.
- **Language** — Uzbek or Russian only, even if the input is in English;
  defaults to Uzbek if unclear.
- **Tone** — always warm and polite, the register of a real workplace chat.

The classification prompt encodes refusals as a first-class
`target_type: "refused"` output (validated the same way as every other
outcome via `validate_classification`), not a separate moderation call — one
LLM call still handles routing and guardrail decisions together. Verified
live against real prompt-injection and abusive-message inputs before
shipping (both correctly refused, in Uzbek, politely) — see the commit that
introduced this for the exact test transcript.

## Conversation memory

Every substantive Director-facing message (not UX filler like "got it,
routing...") is logged to `conversation_turns`, and the last 20 turns are
fed back into both the classification and answer prompts as context — so
"send that to IT too" or "what about the 25th one" resolve against what was
actually just discussed, instead of every message being treated as the
first one this Director has ever sent. Verified live: a follow-up message
with no task content of its own ("send the same thing to IT too") correctly
resolved against the prior turn's task.

## Admin: employee list & removal

`/employees` (or `/users`, `/list`) sent to **Admin Bot** lists every active
employee with a 🗑 Remove button each. Tapping one sets `status='revoked'` —
idempotent, gated by `ADMIN_BOT_ADMIN_USER_ID` the same way access decisions
are when it's set. A removed employee would need to message OPS Manager Bot
and go through the join flow again to regain access. Added specifically as a
testing-cleanup tool (the business owner is currently the one repeatedly
registering/re-registering while testing) — no self-service UI for employees
to see or remove themselves, by design.

## Agent-name mapping (org-chart name → what actually gets read)

| Org-chart name | Repo agent | Data source |
|---|---|---|
| Lead Agent | `agents/lead-agent` | Leads Google Sheet, every row, all 20 columns |
| Finance Agent | `agents/receivables` | `v_ar_aging_latest` (every open receivable) + recent `alerts WHERE agent='receivables'` |
| CRM agent | `agents/amocrm-followup`'s table, but really the **in-house CRM** (see below) | `v_pipeline_latest` |
| Reporter Agent | `agents/ceo-daily-brief` | `daily_briefs`, last 14 days |
| All Systems (`all_systems`) | not a real agent — a `roles.py` pseudo-entry | all four fetchers above, run and concatenated, labeled per section — deliberately excludes Garmin Catalog (product reference, not operational status) |
| Garmin Catalog (`garmin_catalog`) | not a real agent — a static reference snapshot | `prompt.py`'s `GARMIN_CATALOG` constant, ~50 products with real prices, captured live via a real browser (the site is a JS SPA — a plain fetch only sees "Loading...") on 2026-08-21. A point-in-time snapshot, not a live feed — the answer prompt tells the seller to confirm current price/stock before finalizing a sale. Refresh by re-capturing the page and updating the constant by hand; nothing re-fetches this automatically. |

**A gotcha worth knowing**: `v_pipeline_latest` sits on top of `amocrm_pipeline_snapshots` — a legacy table name from before this business migrated off amoCRM to its own CRM (`CRM_BASE_URL`/`CRM_API_KEY`). Despite the name, it is NOT populated by `agents/amocrm-followup` (that agent still targets the real, unconfigured amoCRM API and has no code path that writes here at all). It's populated by `agents/ceo-daily-brief`'s own daily `_fetch_crm() → persist_crm_pipeline()`, which reuses this table on purpose rather than adding a parallel one. If the CRM agent ever reports "no data," the fix is almost always `CRM_API_KEY` being an unfilled placeholder, not anything in `org_bot` — `_fetch_crm_agent_data`'s own "no data" message says this directly. `amocrm_deal_events` (a webhook-driven amoCRM event log) has no in-house-CRM equivalent and is intentionally not queried — reporting stale pre-migration amoCRM events would be worse than reporting nothing.

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

No task edit/cancel (Start and Done exist; there's no "reassign" or "delete a
task"). No employee revocation UI (the `status='revoked'` value exists in the
schema, nothing sets it yet). No group-chat task delivery — every task lands
in a personal DM. No admin self-service role-list editing. No live per-agent
"ask" endpoints — it answers from each agent's already-computed output only;
add a real on-demand query path per agent if that proves insufficient for a
specific question shape. No mandatory Director confirm-before-send on
routing decisions — routing is auditable via `agent_actions`/`tasks` but not
gated on a second tap.

**On "write access"**: the bot can now write — task status transitions
(Start/Done) and media dispatch (`copyMessage`) are real writes, not just
answers. What it deliberately still can't do is take an *autonomous* action
on its own judgment against an external system (update a lead's stage in
Sheets, edit a CRM deal, etc.) — every write it performs today is a direct,
bounded reflection of something a human explicitly did (the Director sent
this exact task/file, an employee tapped this exact button), not a decision
the model made on its own about what to change. If a specific autonomous
write action is wanted later, gate it behind `TelegramBot.request_approval`
(the same Approve/Decline pattern payment/contract/HR approvals already use)
rather than executing it directly — this project's whole security doctrine
is human-in-the-loop for anything consequential, and a free-text-driven bot
is the last place to relax that.

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
- Runs on `anthropic/claude-sonnet-5` via OpenRouter (`OPS_MANAGER_BOT_PROVIDER=
  openrouter`), independent of `AI_PROVIDER` which Lead Agent uses (DeepSeek)
  — `OpenRouterClient` takes a `provider_override`/`model_override` per call
  precisely so two agents can run different providers without a second
  settings switch. Confirmed live (both the JSON-mode classification call and
  the plain-text answer call) before this was wired in. No write access comes
  with the model upgrade — it can now see everything across all four agents
  in full, not a truncated preview, but every write still goes through the
  same human-approval pattern as the rest of this project (see "cut from v1").
