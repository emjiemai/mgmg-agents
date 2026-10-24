# Task tracker (A3) and the payment gate (B1)

**Code:** `integrations/org_bot/task_tracker.py` (rules, scorecards),
`agents/task-tracker/agent.py` (scheduled part), deadline handling in
`integrations/org_bot/ops_manager.py`; B1 routing in
`integrations/org_bot/permissions.py` + `permission_flow.py`
**Runs inside:** OPS Manager Bot (`mgmg-api`), the 08:00 morning cron and the
17:00 evening cron — no new Render service
**Source:** the owner's plan *ЭМЖИЕМ AI Агентлар Тизими* (25.08.2026), agents
A3 and B1, and its И coefficient

## The one task register (2026-10-13, the owner's spec of 11.10, Day 1)

`integrations/org_bot/task_register.py`. Every task has a **code**
(`T-2026-10-0147`, `tasks.code` from `task_code_seq`; old tasks got one
too) and a **state**: `sent` Яратилди → `started` Қабул қилинди →
`blocked` Тўсиқ бор (reason) / `review` Текширувда → `done` Ёпилди (or
`cancelled`). The employee's card: **▶️ Қабул қилдим · ⛔ Тўсиқ бор ·
✅ Бажардим**.

- ✅ doesn't close anything: the bot asks for **proof** — a photo, a file or a
  short text; the employee's next message within **30 minutes** is taken as it
  (`tasks.awaiting`; never a /command, never a Reply to another message such as
  the 16:00 report ask). Without proof the task stays open.
- If the proof card can't reach the assigner (as a file, then as text), the
  task goes back to "send the proof" — never stuck in review with no buttons.
  A file's caption is fitted to 1024 characters without cutting a tag.
- Returned (↩️): its reminder and overdue notice can go out again. KPI counts
  a task finished when its proof went in (in review), like the on-time index.
- The proof goes to **the one who assigned it** (a file is copied as it is)
  with **✅ Қабул** (→ `done`) and **↩️ Қайтариш** (→ back to `started`, the
  employee is told to redo it with new proof). Only that person can press them.
- ⛔ asks what blocks it; the reason is saved and sent to the assigner;
  **▶️ Давом эттирдим** ends the block.
- Every step is in `task_events` (created, accepted, blocked, unblocked,
  submitted, closed, returned — who, when, the reason / proof text).
- On time = the proof went in by the deadline (`review` counts as finished).
- The proof and the blocker reason are task data, so they reach the
  assigner — the one exception to "no text between employees and the
  Director"; everything else stays as it was.

Still to come from the same spec: positions with deputies, the route table
(which message goes to which position) and escalation by time.

## A3 — task tracker

The plan's audit: 11 projects started, 2 finished; "employees don't work
unless reminded". A3 does the reminding.

1. **Deadline.** When the Director gives a task, the classifier extracts a
   deadline only if the Director stated one ("ertaga", "juma kuni",
   "25-sentabrgacha"), resolved against today's Tashkent date. It never
   invents one. If none was stated, the Director's "Yuborildi" confirmation
   carries one-tap choices: **Bugun · Ertaga · 3 kun · 1 hafta · Muddatsiz**.
   A deadline picked afterwards is sent to the employee too.
2. **Card.** The employee's task card shows `⏰ Muddat: 25.09.2026 (juma)`.
3. **08:00, Mon–Fri** (`--morning`): an open task due today or tomorrow gets
   **one** reminder. A task past its deadline: the employee is told once, and
   the Director gets **one** message listing every newly overdue task. A task
   without a deadline is never nagged about. Since 2026-10-24 the employee's
   reminders are lines in their one morning message (spec 30) — «T-… «…»
   топшириғининг муддати бугун» — not a message per task; the task card keeps
   its buttons.
4. **Friday 17:00** (`--weekly`): the Director's scorecard — tasks done on time
   (A3's И), daily reports sent and on time (A1's И, only when daily reports
   are on), and the week's written permissions. Totals plus only the problem
   names.
5. The Director can ask any time: "qaysi topshiriqlar muddati o'tgan?", "kim
   kechikyapti?" — routed to the `topshiriqlar` agent.

### The plan's И coefficients

| Agent | Formula | Green | Red (stop the rollout) |
| ----- | ------- | ----- | ---------------------- |
| A1 daily reports | (reported ÷ asked) × (before 18:00 ÷ reported) | ≥ 0.80 | < 0.50 |
| A3 tasks | done by the deadline ÷ tasks due | ≥ 0.70 | < 0.50 |

A task that is still open on its last day is not judged yet. A task finished
any time on its deadline day counts as on time.

## B1 — payment gate (routing by amount)

The written permission flow (EMJ-SOP-ADM-01, see `07-permissions.md`) is the
single form. B1 adds **who decides, by amount**:

`PERMISSION_APPROVAL_TIERS="5000000:111111111,20000000:222222222"` means up
to 5 mln so'm → Telegram id 111111111, up to 20 mln → 222222222, above → the
Director (and deputies).

- **Empty by default** — the limits are the business's to write down (the
  plan says the limit exists but isn't written). Until set, everything goes to
  the Director, as before.
- Only whole so'm amounts are routed. "0", dollars/euros and unreadable amounts
  always go to the Director.
- A limit holder who is the requester, or inactive, is skipped — the Director
  decides instead (SOP §3: nobody approves their own request).
- The Director and deputies can always decide any request; a limit holder
  only the ones routed to them.
- **No automatic approval.** The plan suggests auto-approving below a limit;
  the SOP requires a written decision by an authorised person, so every
  request still gets one.

## Switches

| Setting | Effect |
| ------- | ------ |
| `TASK_TRACKER_ENABLED` | `true` (default). `false` stops reminders, overdue notices and the scorecard. Deadlines are still recorded. |
| `PERMISSION_APPROVAL_TIERS` | B1 limits, see above. Empty = everything to the Director. |

## Not built, and why

The plan's other critical agents need data this system does not yet have in
full:

- **A2 five numbers, B2 cash calendar, B3 reconciliation, C3 dead stock, D1
  reorder signal** — the SAP gateway push sends only the first 100 rows of
  inventory/payments/orders (20 of products), and those field names are
  unconfirmed (`integrations/sap/push_handler.py`). Totals built on a sample
  would be wrong. Needed: full pushes, with the real field names checked once.
  Cash balances have no source at all yet.
- **C1 leads, C4 service reminders, C5 win-back, F1 content** — need WhatsApp
  Business / Instagram / a customer machine list; none is connected.
- **G1 owner's assistant** — needs Outlook access.
- **E2 AI operations manager** — the plan says build it last, on top of A1,
  A2, A3, B1, B2. OPS Manager Bot already covers its routing part.

## The same task again; a role change (2026-10-17)

The owner: a task the Director gives again while the person's earlier one is
still open is **not added a second time** — a reminder (🔁) goes as a reply on
their existing card, the history gets `repeated`, and the Director's card says
who was reminded. "The same" = the summaries or the Director's words ≥ 80 %
alike (`task_register.same_task`). Someone without it open gets a new task as
before (each person has their own row).

A role change is a clean start (`store.change_employee_role`): that person's
open tasks are cancelled (a co-assignee keeps theirs), today's unanswered
report ask closes without a minus, the work AI forgets the old chat, and KPI
counts only the role held now. Removal already did this (2026-10-14).
