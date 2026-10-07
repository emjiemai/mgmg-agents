# The work AI — employees talk about their own work with the AI, not with people

**Code:** `integrations/org_bot/ai_chat.py` (what it knows, its rules, texts),
`integrations/org_bot/ops_manager.py` (`_handle_employee_message`, `_route_to_ai`,
`_answer_ai_chat`), `admin.py` (the 🤖 off switch)
**Stored in:** `employees.ai_chat_off`, `employees.responsibilities`,
`ai_chat_turns` (the chat's memory, one day)
**AI:** OpenRouter, the OPS Manager Bot model chain

## The rule (the owner, 2026-10-02)

OPS Manager Bot is responsible for work only. Employees don't message the
Director or each other through it, and the Director doesn't message
employees through it (task cards aside). Employees talk about **their own
work and their own tasks with the AI**. If the AI doesn't know, it says so —
honesty first. Each employee's written duties will be uploaded later.

## What happens to an employee's message

In order: a Reply to a cheer → a friendly line; today's report → saved (a message that asks something during the
report window gets "бу бугунги ҳисоботингизми ёки савол?" — **ҳа, ҳисобот /
йўқ, бу савол**); everything else → **the work AI**. A Reply to a task card
brings that task into the question. A message with "?" is never taken
as the report's one follow-up answer just for arriving
soon after them — only a Reply to that question is (2026-10-03). No command is needed (`/ai` just gets a
hint); other unknown commands are ignored. The answer runs in the background.

Nothing an employee writes reaches the Director or anyone else. Old "📨 send
to the Director" buttons do nothing.

## What the AI knows — only this

- the person: name, role, **their open tasks** (deadline, overdue, started or
  not) and **their written duties** once uploaded
  (until then it's told they aren't uploaded and says so);
- the company, public facts only: Primus Londry equipment and services,
  Garmin and Tanita; Garmin sales also get the public catalog.

It has no SAP, sales figures, debts, cash, stock, reports, KPI, customers or
anything about other employees. It tells people the two work ways that do
exist: a **file** (e.g. a photo report) sent to the bot can go to the
Director (the bot asks what it's for), and **/hisobot** changes or deletes
today's report. It can't send, save or change anything, or
pass a message to anyone — asked to "tell the Director", it says the bot
doesn't carry messages.

**Honesty first:** when it doesn't know, it says "билмайман" / "бу маълумот
менда йўқ" and who could know — never a guess, never an invented number,
name, date or price.

It writes Uzbek Cyrillic, always the polite "сиз", especially courteous with
women; a draft asked for in another language (a Russian email to a customer)
is in that language. Telegram HTML only.

## Admin

Admin Bot `/xodimlar` → the person → **🤖 AI ёрдамчи: ✅ ёқилган** — tap to
switch it off for that person (⛔); their messages then get "бу бот фақат иш
учун: ҳисобот ва топшириқлар…" and go nowhere. The card also shows
whether their duties are uploaded.

**Duties upload — next step.** `employees.responsibilities` is ready and the
AI reads it; the way to upload (a document per person, or text on the card)
comes when the files are ready.

## Limits and privacy

60 messages a day per person. The chat is kept one day as its memory (the
last 12 turns go with each message) and shown to no one. Every AI call is in
`agent_actions` like any other.

## Checks

`selfcheck.py` (`test_ai_chat_and_sheet`, `test_report_or_message`): the
context holds their tasks (overdue marked) and duties (leads removed with the hand-out, 2026-10-07); the prompt's
honesty rule, no data, no messages to the Director; Garmin-only catalog;
a message goes to the AI and nothing offers to send it to the Director; the
daily limit; `/ai` hint; unknown commands ignored; the off switch; report
window → "report or question?" → the AI; old relay buttons inert.
