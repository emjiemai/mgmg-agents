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
- **The ask is a new message (2026-11-04).** On 03.11 six tasks got ✅ on
  day-old cards; the ask was only an edit of those cards far up the chat, gone
  after 30 minutes — no proof came, nothing closed, the 04.11 brief showed them
  red. Now ✅ (and ⛔) also send **a new message under the card**
  (`task_register.prompt_text`: the code, the task's words, what to send, reply
  box open — `force_reply`), its id in `tasks.awaiting_message_id`. **A reply to
  that message or to the card is the task's proof / reason however much later**
  (`store.task_awaiting_input(..., replied_to=)`); a plain message still only
  within 30 minutes of the tap. Several ✅ in a row: each proof's thanks names
  the tasks still waiting (`waiting_line`) — one proof is one task's.
- **Told the next morning:** ✅ pressed before today and still no proof → one
  line in the morning message («…«Бажардим» босилган, лекин далил келмаган —
  топшириқ картасига жавоб қилиб далил юборинг»), once per tap
  (`tasks.proof_reminded_at`, `store.tasks_proof_missing`). The morning hint
  under due tasks now says to press «Бажардим» **and send the proof**.
- In the 08:00 brief such a task reads «далил кутилмоқда» — not red, not
  counted as overdue (`01-ceo-daily-brief.md`).
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

Still to come from the same spec: the route table (which message goes to which
position). Deputies and escalation by time: built 2026-11-12 (see the end).

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
| `TASK_ACCEPTER_ENABLED` | `true` (default): the Director can let a TEAM_OF manager check the proof (only for his whole team). `false` hides the choice. |
| `TASK_PROOF_HINT_ENABLED` | `false` (default, until the owner decides on personal data before AI): `true` sends a task's expected result and the typed proof (never the file) to the AI for one **advisory** line on the review card. It decides nothing. |
| `TASK_HELPER_ENABLED` | `false` (default, until the owner agrees to the new relay exception): `true` puts «🙋 Ёрдамчи тайинлаш» under the ⛔ message to the assigner — he gives a help task through the normal draft. |
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


**2026-10-30:** the Friday result counts active employees only — a removed person's tasks and report days are kept as history but left out (`store.tasks_due_between`, `store.reports_between`; also the brief's day before and `recent_reports`).

**2026-11-05:** a file sent without replying to the card: the «нима учун?» question offers «топшириқ далили» while ✅ waits (one task → it; several → pick), then the same review as a reply to the card (`_handle_file_proof`).

**2026-11-09 — shared tasks:** copies with the same assigner and `source_message_id` are one shared task: the brief groups them (one row, the furthest state, «+N»), overdue/due-soon notices skip a copy while another copy awaits proof or is in review, the review card names the others (`task_register.shared_line`), and accepting one closes the rest (`store.close_shared_copies`, completion time of the accepted one; each colleague told).

**2026-11-14 — each copy is its own task (bug fix):** one task the Boss sent to 12 people (T-2026-11-0067…0078) was
closed for all when one proof was accepted, and the 11 others were told «… бажарди». Removed: `close_shared_copies`,
`move_shared_copies` (a new deadline moved every copy), the «colleague did it» clause in the reminder / overdue /
escalation queries and «one ask per shared task». Kept as display only: the brief's one row («+N»), now in the state of
the most urgent copy (asked > blocked > overdue > review > proof > waiting > today); `shared_line` on the review card
says how many others got it and that theirs close separately.

**2026-11-09 fix:** the task-pick buttons carry the task code without «T-» (`report_tools.proof_key`) — file id + task id exceeded Telegram's 64-byte `callback_data` limit.

**2026-11-10 — priority, new deadline, escalation:**
- Priority `tasks.priority` 1–4 (🔴 Шошилинч, 🟠 Муҳим = default, 🟢 Оддий, ⚪ Кутиши мумкин): P1–P4 row on the Director's
  send card (`tdpri:`), preset by `task_register.guess_priority` from his words; card line when ≠ P2; brief sorts P1 first
  (🔴); the overdue list marks P1.
- New deadline: «📅 Муддатни узайтириш» (`taskres:`, only with a deadline and no pending ask) → +1 / +2 / +7 days from
  max(deadline, today) (`tdp:` / `tdpx:`) → the reason (`awaiting='reschedule'`, a reply) → the assigner's card with
  from → to, the reason, «аввал N марта» (`tdpa:` ✅ / `tdpr:` ❌, only the assigner) → ✅ sets `due_date`, counts
  `reschedules`, resets the reminder and overdue notice; ❌ keeps it; the employee is told. Events `reschedule_asked`,
  `rescheduled`, `reschedule_refused`. The brief shows a pending ask (red) until decided.
- Escalation: the morning overdue list also goes to the employee's department manager (`report_calendar.TEAM_OF`),
  without the tasks that manager gave himself.
- Still to come: «who can help» on ⛔ (a who-helps-with-what list), deputies.

**2026-11-12 — register fixes (review of the task flow):**
- `source_message_id = 0` (older clarification-born tasks) is never "one shared task": the reminder and overdue
  queries' sibling clause has `t.source_message_id <> 0` (as `ask_reschedule` already had), and
  `analyst.bot_tasks.shared_key` is the task's own code for them, not `director:0`.
- A task born from a clarification tap (`_handle_clarification`) takes the clarification card's message id as its
  source, so `uq_task_dispatch` no longer silently drops a second such task to the same person; it now also gets the
  «⏰ қачонгача?» deadline buttons like any other message.
- `create_task` → None (already dispatched from this message to that person) is no longer a silent `continue`:
  `_send_task_cards` collects them and the Director's send card says «ℹ️ Бу топшириқ уларга аввал юборилган — қайта
  юборилмади: …» (`task_picker.duplicate_text`).
- ▶️ Қабул қилдим only by the assignee: `store.mark_task_started(task_id, by, telegram_user_id)` joins
  `employees.telegram_user_id` (as `await_task_input`); anyone else gets «Бу топшириқ сизга берилмаган».
- A proof that could not reach the assigner (`reopen_submission`) is logged as its own event `resubmit_asked`
  («далил етиб бормади»), never `returned` — a delivery failure is not rework. PULSE names it «Далил қайта сўралди»
  (and the three reschedule events now have Uzbek labels there too).

**2026-11-12 — rework: returns counted and shown:**
- ↩️ Қайтариш on the review card no longer returns at once: its buttons turn into four reasons
  (`task_register.RETURN_REASONS`, `tbr:<id>:<e|q|r|o>` — «Далил етарли эмас», «Сифати талабга жавоб бермайди»,
  «Натижа бошқа», «Бошқа сабаб») and «⬅️ Ортга» (`tbrx:<id>`, back to ✅ / ↩️). Only the assigner, only in review;
  the card's text is untouched until a reason is tapped (`editMessageReplyMarkup`). The reason is a tap, never typed.
- `verify_task(accept=False, reason=…)`: status `started`, `tasks.returns + 1`, `awaiting` stays NULL (never
  `'evidence'` — a returned copy is not «a colleague did it», not «✅ pressed, proof missing», and a plain message
  isn't taken as proof); the `returned` event's note is the reason label. The employee: «↩️ T-… қайтарилди: <сабаб>.
  Илтимос, тузатиб, ✅ Бажардим орқали янги далил юборинг.» — he presses ✅ again for the new proof.
- Shown: the review card «⚠️ Аввал N марта қайтарилган.» (text and caption); the employee's card status
  «· ↩️ N марта қайтарилган»; the brief's task row « · ↩️N» (no new group); the Friday scorecard «Қайта ишлаш: X та
  топшириқ Y марта қайтарилди» + up to five codes with names (`store.returns_between` — `returned` events of Mon..Fri;
  `resubmit_asked` never counts); `analyst.bot_tasks.returns` (appended last, in `sql_catalog`).
- KPI and PULSE unchanged: a returned task finishes later, which the on-time rule already counts. A shared task's
  return counts on that copy only.

**2026-11-12 — accepting manager (the proof checked by the team's manager):**
- On the Director's task card a row «🔎 Текширади: Ўзим / <name>» (`tdm:<draft>`) shows **only** when a TEAM_OF
  manager (`report_calendar.TEAM_OF`, today the Garmin manager for Garmin сотув) has EVERY ticked person in his team
  and is not ticked himself (`task_picker.accepter_candidates`), or one is already picked. Otherwise no row — no noise.
  Tapping it freezes the managers offered on the draft (`task_drafts.accepter_candidate_ids`) and shows «🔎 Ўзим»
  (`tdmp:<draft>:0`, the Director himself) + each manager (`tdmp:<draft>:<i>`); a pick is checked again against the
  ticks (`accepter_allowed`; no longer fits → back to the Director, «accepter_refused»). The card says
  «🔎 Текширади: <name>». Nothing is sent before 📨, as before.
- 📨 checks again (the ticks may have changed): still fits → every copy of the task gets `tasks.accepter_employee_id`
  (event `accepter_set`, note = his name) and the sent card says «🔎 Текширади: <name>»; no longer fits → sent with
  no accepter and one line «ℹ️ <name> текширмайди — белгиланганларнинг ҳаммаси унинг жамоасида эмас; далилни ўзингиз
  текширасиз.» A task given again while still open (the reminder path) keeps the accepter it had.
- The proof (`_consume_task_input` → `_deliver_review`) goes to the accepter when he is active, has a chat and has not
  blocked the bot (`store.review_accepter`); a failed send to him tries the assigner before the employee is asked
  again (`reopen_submission`). The employee reads «… текширувга юборилди (текширади: <name>).»
- ✅ / ↩️ (`store.MAY_VERIFY`): the assigner or the active accepter, never the task's own assignee; the first tap wins
  (status `review`). An accepter no longer active gets «Рухсат йўқ». After the accepter decides, the assigner gets one
  line: «✅ T-… ёпилди — <name> текширди.» / «↩️ T-… қайтарилди — <name> (<сабаб>).» — no proof text. Shared copies
  close as before. A new deadline (📅) stays the assigner's (`decide_reschedule` unchanged).
- The accepter removed or moved to another role (`store.revoke_employee` / `change_employee_role` →
  `clear_accepter_of`): his open tasks go back to the assigner (NULL, `accepter_set` with a note), and the reviews
  waiting for him are re-sent to the assigner by Admin Bot (`admin._resend_reviews` →
  `ops_manager.resend_reviews_to_assigner`, the file copied from the employee's chat) with «ℹ️ Текширувчи энди йўқ —
  далилни ўзингиз текширинг.»
- `analyst.bot_tasks.accepter` (appended last, `sql_catalog`); PULSE names the event «Текширувчи белгиланди».
  `TASK_ACCEPTER_ENABLED=false` hides the row (tasks already given keep theirs).


**2026-11-12 — reminder gaps closed (one reminder before, one line after, in the morning message):**
- **Weekend / /dam deadlines.** `task_tracker.next_work_day(day, off_days)` skips Saturday, Sunday and Admin `/dam`
  days; `remind_until(today, off_days) = max(tomorrow, next_work_day − 1)`. `store.tasks_needing_reminder(today,
  until)` covers due dates `today … until`: on Friday that is Saturday and Sunday (the line names the weekday —
  «муддати якшанба, 15.11»), the day before a `/dam` day reaches over it. The next working day itself is never
  reminded early, so its own morning still says «бугун» (one reminder per task — Monday's isn't used up on Friday).
- **Not at work this morning** (`task_tracker.held_today`: `kpi.works_on` on the person's week, or an excused day
  from `/sababli`): no note, `reminded_at` / `overdue_notified_at` untouched, not on the Director's / manager's list
  that morning — their next working morning catches it (as an overdue line once the date has passed). Someone whose
  week has no Monday–Friday day is never held (the job doesn't run at weekends — they'd never hear). Both queries
  now select `work_days`, `works_saturday`, `works_sunday`.
- **✅ pressed (awaiting proof):** that copy gets the proof line only, no overdue line on top; it still goes on the
  assigner's list and is marked notified.
- **Who was told:** `tasks_newly_overdue` selects `assigner_role`; `task_tracker.overdue_told` puts `told`
  (`director` / `assigner`) and `manager` (the TEAM_OF manager got it) in the note. The line reads «…муддати ўтди
  (11.11), директорга хабар берилди», «…топшириқ берган кишига хабар берилди», «… ва бўлим раҳбарига …»; an older
  note without `told` reads as before. The dry run logs codes only, no task text.
- Tests: `test_task_reminders_1112`.

**2026-11-12 — expected result + the advisory proof hint:**
- **Heard, never invented.** The classifier (`prompt.CLASSIFY_SYSTEM_PROMPT`, «EXPECTED RESULT») returns
  `expected_result`: what must exist when the task is done, only in the Director's own words, Uzbek Cyrillic,
  ≤ 200 characters; `null` when he didn't say (the same rule as `due_date`). `task_register.clean_expected` keeps it
  one line without tags. Text tasks and file captions both carry it (`_dispatch_director_task` /
  `_dispatch_director_media` → `_propose_task` → `task_drafts.expected_result`).
- **The Director confirms it.** The draft card shows «🎯 Кутилган натижа: …» and a row «🎯 Натижани олиб ташлаш»
  (`tdrx:<draft>`, 41 bytes; `store.drop_task_draft_expected`, the same guards as the priority tap) for a wrong
  guess. On 📨 every copy of the task gets the same `tasks.expected_result`; the sent card repeats the line.
- **Shown where the work is done:** the employee's card (text and file caption — `_task_caption`, used on the first
  send and on every redraw, the line capped at 120 characters there), the ✅ proof ask («Далилда шу кўринсин: …»),
  the reminder card and the review card (text; caption capped at 120 so the proof keeps its room).
- **Advisory proof hint** (`TASK_PROOF_HINT_ENABLED`, **off** by default — the personal-data decision before AI is
  still open): after the proof is saved and before the review card goes, `ops_manager._proof_hint` →
  `task_register.proof_hint(expected, typed proof, has_file, ai)` sends only those two texts
  (`complete_json` inside `asyncio.wait_for`, 6 s). A file with no text never calls the AI: «… файлни ўзингиз
  кўринг». Verdicts → one line: «🤖 Маслаҳат (қарорни сиз қиласиз): далил кутилган натижага мос кўринади.» /
  «… далил қисман мос: <gap>» / «… натижа кўринмаяпти: <gap>»; unknown, an error or a timeout → nothing and the card
  goes anyway. Stored in `tasks.proof_hint` for history (the only write on that path); never a status, never a
  button. Logs keep the verdict only.
- `expected_result` and `proof_hint` are hidden from IT in `/db` and the local copy (`db_viewer.HIDDEN_COLUMNS`);
  `analyst.bot_tasks.has_expected_result` (a flag, appended last, `sql_catalog`) — no text in the view.
- Tests: `test_task_expected_1112`.

**2026-11-12 — dependencies (a task waits for another T- code):**
- **Only from the Director's own words.** `task_register.parse_dependency` finds exactly one code
  (`[TТ]-\d{4}-\d{2}-\d{4,}` — Latin or Cyrillic «Т», codes past 9999 too) **and** a waiting word («тугагач»,
  «кейин», «қабул қилингач», «ёпилгач», «после», …); two different codes or no waiting word → nothing. No AI guess.
  `_propose_task` resolves it with `store.task_by_code` (open tasks only) into `task_drafts.depends_on` /
  `depends_code`. The draft card says «⏳ T-… қабул қилингач бошланади» with a row «⏳ Боғлиқликни олиб ташлаш»
  (`tddx:<draft>`, 41 bytes; `store.drop_task_draft_dependency`, the same guards as the priority tap).
- **📨 checks again** (`store.dependency_open`): still open → every copy gets `tasks.depends_on` (`create_task`
  logs `depends_set` with the code) and the sent card repeats the line; closed meanwhile → dropped, one line
  «ℹ️ T-… аллақачон ёпилган — топшириқ кутмасдан бошланади.» A repeated task keeps what it had.
- **Waiting** = `store.WAITING`: `depends_on` set, `dependency_done_at` NULL, and some copy of what it waits for
  — the whole shared group (same Director + source message, source ≠ 0) — still open. While waiting:
  `tasks_needing_reminder`, `tasks_newly_overdue` and `tasks_proof_missing` leave it out (no reminder, no overdue
  notice, no Director / manager list); the employee's card says «⏳ T-… кутилмоқда — у қабул қилингач бошланг» (the
  buttons stay — he can prepare or ask a new date); the brief has a `waiting` group «кутмоқда T-… · …», never red
  (a shared task only when every copy waits); the Director's task answers add «(кутмоқда: T-…)».
  `open_tasks_with_names` / `task_with_employee` select `waiting_for`.
- **Release** (`store.release_dependants`, once per task — `dependency_done_at`): after an accept (the task + every
  copy `close_shared_copies` closed, `_handle_task_verify`) and after the cancel paths (removed / role changed —
  `revoke_employee` / `change_employee_role` return `cancelled_task_ids`, admin.py calls
  `ops_manager.release_dependants(..., cancelled=True)`). A task is released only when no copy of what it waited
  for is still open. Its `reminded_at` / `overdue_notified_at` start again; a deadline of today or already past moves
  forward by the days it waited (`due + (today − created day)`, never before today) and is logged `rescheduled`
  with a note — waiting for someone else is never counted late in the KPI (`reschedules` is not raised). Logged
  `depends_done`.
- **Who hears:** the card is redrawn; the person is told now (a reply under the card: «▶️ T-… қабул қилинди — энди
  T-… «…» топшириғини бошлашингиз мумкин.» + the moved deadline) when he works today (`kpi.works_on`, not a /dam day,
  not excused), else a `task` note with `released: true` for his next working morning
  (`task_tracker.next_day_at_work`); a copy already in review is not told. The assigner gets one line («⏳ T-… (name)
  энди бошланади — T-… қабул қилинди.» / «⚠️ T-… бекор бўлди — T-… (name) кутишда эди, энди кутмасдан бошланади.»).
  Logs counts only. A task the DB review cancels (`db_review.py`) releases nothing by message, but `WAITING` sees
  nothing open, so the reminders come back by themselves.
- No cycles: `depends_on` always points at an older, existing task. `analyst.bot_tasks.waiting_for` (the code while
  not released, appended last, `sql_catalog`). PULSE labels «Бошқа топшириқни кутади» / «Кутиш тугади».
- Tests: `test_task_dependency_1112`.

## 2026-11-12 — ⛔ helper (off by default)

- **Switch:** `TASK_HELPER_ENABLED`, **off** by default. The blocker reaching another person is a new exception to the
  no-relay rule — the owner confirms it before it is switched on. Off: the ⛔ message to the assigner is as before.
- **The button:** on, the ⛔ message (`blocked_text`) goes with «🙋 Ёрдамчи тайинлаш» (`tbh:<task uuid>`, 40 bytes;
  `_tell_assigner(..., reply_markup)`). Only the assigner (`director_telegram_user_id` = the clicker), only while the
  task is still blocked, and only one at a time: `store.helper_pending` refuses while an open draft (last 24 h) or an
  open help task with `helper_for` = this task exists («Ёрдам топшириғи аллақачон тайёрланган»).
- **The draft is an ordinary one** (`_propose_task(..., exclude_ids, helper_for, priority)`): the blocked person is
  not offered, the Director is never listed, nobody is ticked; priority = the parent's; no dependency is read; the
  source = the ⛔ message's id (unique per block — the help task is never a shared copy of the parent, and
  `close_shared_copies` never touches it). The text is the parent's code and summary only
  («T-… «…» топшириғига ёрдам беринг», `task_register.helper_summary`). The **blocker is shown on the Director's
  draft only** («⛔ Тўсиқ (фақат сизга кўринади): …» — the draft's `raw_message`, never sent: `_send_task_cards`
  sends and stores the summary as the help task's `raw_message`). Ticks, priority, 🔎, 📨 and the deadline ask work
  as for any task. `tasks.helper_for` / `task_drafts.helper_for` (UUID).
- **After 📨** (`_helper_sent`): the parent logs `helper_asked` (note = the helpers' names), its card shows
  «· 🙋 ёрдам: T-… (name)» while blocked (`task_with_employee.helping`), and its person is told who helps.
- **The help task accepted** (`_handle_task_verify` → `_helper_done` → `store.helper_done`): the parent is **not**
  unblocked by itself — its `reminded_at` / `overdue_notified_at` are reset, `helper_done` is logged (note = the help
  task's code), and its person is asked under his card «🙋 T-… ёрдами тайёр — T-… «…»: давом эттира оласизми?» with
  ▶️ Давом эттирдим (`taskgo`) / ⛔ Ҳали тўсиқ бор (`taskblock`, a new reason). A parent unblocked by hand meanwhile
  gets only the note; a closed parent nothing. The assigner gets one line. Never fatal to the accept.
- `store.HAS_OPEN_HELPER` (an open help task for ``t``) is there for the escalation ladder (piece 5) to skip a
  blocked parent someone is helping with. Logs keep no blocker / task text. PULSE labels «Ёрдамчи тайинланди» /
  «Ёрдам тайёр».
- Tests: `test_task_helper_1112`.

## 2026-11-12 — escalation ladder + deputies

- `tasks.escalation_level` (0 nobody above the assigner told; 1 the department manager or his deputy; 2 the
  Director) and `escalated_at`; reset with `reminded_at` / `overdue_notified_at` (new deadline, return, release,
  unblock, help done). Each step logged as task event `escalated` (note `manager` / `deputy` / `director`).
- Day 0 (08:00 task tracker): the overdue list to the assigner as before; the TEAM_OF manager's list now goes to
  the manager if he is at work today (`kpi.works_on`, not excused, has not blocked the bot), else to his deputy
  («… бугун ишда эмас — сиз ўринбосари сифатида кўряпсиз»). A send that went out sets level 1.
- The ladder (`task_tracker.ladder_steps` / `escalation_plan`, `store.tasks_for_escalation`, same 08:00 run, not on
  weekends or /dam days): overdue and still at 0 `TASK_ESCALATE_MANAGER_DAYS` (1) after the day-0 notice → the
  manager / deputy; `TASK_ESCALATE_DIRECTOR_DAYS` (3) days late → every active Director except the one who gave it.
  Blocked (from the last ⛔, not while a help task is open): manager at `TASK_BLOCKED_MANAGER_DAYS` (2), every
  Director at `TASK_BLOCKED_DIRECTOR_DAYS` (4). A shared task goes once with every copy's id; a copy waiting for
  proof or another task is skipped. Recorded only after the message went out. `TASK_ESCALATION_ENABLED=false`
  stops the ladder (the day-0 lists stay). Logs: counts only. Employees are never messaged by it.
- Deputies: `employees.deputy_employee_id`, Admin Bot /xodimlar → 👥 Ўринбосар, only on a TEAM_OF manager's card,
  chosen from his team (`store.deputy_candidates`; buttons `dps:<8 chars of id>:<manager id>`, ❌ Ўринбосарсиз).
  The Director is told of each change in OPS Manager Bot, also when a removal / role change clears the link
  (`store.clear_deputy_links`). A deputy who left the team is ignored by the ladder.
- Tests: `test_task_ladder_1112`.

**2026-11-13 — ladder review fixes:**
- Day counts are WORKING days (`task_tracker.working_days_between`: Mon–Fri, not /dam days); the line still shows
  calendar days. Director overdue step: `TASK_ESCALATE_DIRECTOR_DAYS` working days after the day-0 notice.
- Level 2 never goes to the Director who gave the task — overdue or blocked (he had the list / the ⛔ card).
- A ⛔ resets `escalation_level` (`store.block_task`): the blocked ladder starts fresh, so the manager hears
  «тўсиқда» even after the overdue list. One shared level otherwise (after an unblock the level stays).
- The blocked clock counts from the last ⛔ or the last accepted help task (`helper_done`).
- Shared task across departments: level 1 asks each copy's own manager; only the copies he was told of are marked.
- A step is recorded only when every send of that level for the task went out (`task_tracker.ids_to_mark`); a
  failed recipient → retried next morning.
- Day-0 manager list: not for tasks waiting for proof, never twice (level ≥ 1 skipped); a deputy send logs `deputy`.
- Someone off / excused: his overdue task is still on the assigner's and manager's lists; only his own line waits
  (morning note dated `next_day_at_work`).
- PULSE label for `escalated`: «Раҳбарга етказилди». Tests: `test_task_ladder_fixes_1113`.
