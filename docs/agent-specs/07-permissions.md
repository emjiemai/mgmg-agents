# Written permissions (EMJ-SOP-ADM-01)

**Code:** `integrations/org_bot/permission_flow.py` (conversation),
`permissions.py` (form fields, wording, parsing), `docx_form.py` (filled form)
**Runs inside:** OPS Manager Bot, in the always-on `mgmg-api` service
**Mode:** writes to `permission_requests` + `permission_request_events` and
Telegram; no external system is touched
**Owner:** Operations Director

## Purpose

The director's order is that every request, task, agreement and permission
must be in writing, and that asking verbally — in person, by phone or by voice
message — does not count. This flow is the written channel: an employee asks
the bot, the bot fills the SOP's own form by asking the missing questions, and
an authorised person decides with one of the SOP's four outcomes.

## Before this counts as a real approval

SOP §3 accepts an electronic approval **only** in the system the director
officially designates, with the approver and the decision history preserved,
and says explicitly that personal chats and voice messages are not a
substitute. Technically this flow satisfies the second half — every decision
records who decided, from which Telegram account, when, and an append-only
event trail — but **the director must designate this bot in writing** (a
one-page addendum to EMJ-SOP-ADM-01) before anyone relies on it. Until then,
it is an accurate register, not an authority.

## Flow

1. An employee writes to OPS Manager Bot: `/ruxsat`, or a sentence that both
   mentions permission and reads like a request ("рухсат керак…", "ruxsat
   kerak…", "прошу разрешение…"). Merely mentioning permission ("директор
   рухсат берди") does **not** open a form.
2. The bot opens a draft, and one AI call extracts whatever the opening
   message already states. Nothing is invented: a field the message doesn't
   clearly state stays empty and is asked.
3. The bot asks the remaining questions one at a time, in the SOP form's
   order: what is asked for, reason and proposal, amount and currency,
   execution deadline, when the decision is needed, urgency, attachments.
4. It shows the finished form and asks the requester to confirm with
   **📨 Юбориш** (or **❌ Бекор қилиш**).
5. On sending, the request gets its number (`EMJ-2026-0001`, from a database
   sequence so two people submitting at once can't share one) and goes to
   every approver with four buttons: **Тасдиқлаш · Шарт билан · Рад этиш ·
   Маълумот**.
6. For anything except a plain approval, the bot asks the approver to type the
   conditions, the rejection reason, or what information is missing.
7. The requester gets the decision, and both sides get the **filled .docx**
   of the SOP's page-2 form, including the decision block and the history.

## Who may decide

Whoever holds `operatsion_direktor`, plus any deputies listed in
`PERMISSION_DEPUTY_TELEGRAM_IDS` (comma-separated Telegram user ids, set in
Render). The SOP allows a deputy to decide only with written authority, so
deputies are an explicit list rather than a role.

**Self-approval is impossible by construction:** the requester is removed from
the approver list for their own request. If that leaves nobody, the request is
not sent and the requester is told to contact the admin — better than a
request that looks sent but can never be decided.

## The register

`permission_requests` plus `permission_request_events` is the single register
SOP §5 asks the documents coordinator to keep: number, time received, who is
responsible, the decision, and the execution state. The Director can ask the
bot directly ("ruxsat so'rovlari", "кутилаётган рухсатлар", "EMJ-2026-0004"),
answered by the `ruxsatlar` agent from the last 60 days.

Events are append-only — never updated, never deleted — because a decision
history that can be edited is not a decision history.

## Configuration

| Setting | Effect |
| ------- | ------ |
| `PERMISSIONS_ENABLED` | `true` (default) enables the flow; `false` makes the bot ignore permission messages entirely |
| `PERMISSION_DEPUTY_TELEGRAM_IDS` | Comma-separated Telegram ids who may also decide |

## Deliberately not built yet

- **Response deadlines (§4).** The SOP gives 1 working day, or 2 working hours
  for a stated-urgent request, and escalation when that passes. Nothing chases
  anyone today (the business's choice); the requested decision time is
  recorded on every request, so this can be added without changing the data.
- **Manager-first routing (§2-3).** The SOP routes to the direct manager, who
  escalates beyond their limit. This needs a manager map and money limits per
  role; today everything goes to the Director and named deputies.
- **Completion evidence (§5).** `completion_note` exists on the row but no
  flow fills it, so "result or expense proof attached" is still manual.
- **Emergency post-registration (§6).** Acting first and writing it up by the
  next working day isn't a separate path yet; such a case can be entered as a
  normal request afterwards.
- **Splitting detection (§5).** The SOP forbids splitting a request to get
  under a limit. With no limits configured, there is nothing to split under.
