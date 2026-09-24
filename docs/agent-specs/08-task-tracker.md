# Task tracker (A3) and the payment gate (B1)

**Code:** `integrations/org_bot/task_tracker.py` (rules, scorecards),
`agents/task-tracker/agent.py` (scheduled part), deadline handling in
`integrations/org_bot/ops_manager.py`; B1 routing in
`integrations/org_bot/permissions.py` + `permission_flow.py`
**Runs inside:** OPS Manager Bot (`mgmg-api`), the 08:00 morning cron and the
17:00 evening cron — no new Render service
**Source:** the owner's plan *ЭМЖИЕМ AI Агентлар Тизими* (25.08.2026), agents
A3 and B1, and its И coefficient

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
   without a deadline is never nagged about.
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
