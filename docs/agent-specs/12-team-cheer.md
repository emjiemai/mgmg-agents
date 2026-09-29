# Team cheer — encouragement, a joke, thanks

**Code:** `agents/team-cheer/agent.py` (sends), `integrations/org_bot/cheer.py`
(times, the AI's rules, the built-in messages, what people see),
`integrations/org_bot/ops_manager.py` (taps and replies)
**Runs:** cron service `mgmg-team-cheer` (render.yaml)
**Stored in:** `cheer_messages` (one row per day and time), `cheer_deliveries`
(who got which message, and their tap)
**Switch:** `TEAM_CHEER_ENABLED` (default `true`)

Asked for by the owner on 2026-09-29: work runs 09:00–18:00, and people
should get a little encouragement or a joke at the start, in the middle and
at the end of it — something that makes them happy.

## What goes out

| Time | Message | Answer |
| ---- | ------- | ------ |
| 10:00 | ☀️ Хайрли кун, *name*! — a short encouragement for the day | — |
| 14:00 | 😄 *name*, бир дақиқалик танаффус! — a clean joke or a true fun fact, then a fun question (tea or coffee, favourite season…) | 2–4 buttons |
| 17:35 | 🌇 *name*, иш куни якунланяпти! — thanks for the day, then "how was your day / what made you happy / evening plans" | 2–4 buttons |

14:00 is the middle of the day that fits: right after the usual 13:00–14:00
lunch, when a smile helps most. A tap replaces the buttons with the answer
and a warm one-line reply ("😴 Чарчадим" → "Яхшилаб дам олинг — сиз бунга
лойиқсиз! 🌙"); every answer, tired ones included, gets a kind reply.

**Who:** every active employee except the Director — the same people the
daily reports ask. Saturday/Sunday: only those marked as weekend workers
(Admin Bot `/xodimlar`). Everyone gets the same text that day, with their
own name in front.

## Who writes it

The AI (OpenRouter, the usual model chain) writes each time slot once a day,
given a theme idea and the last 30 messages so it doesn't repeat itself.
Its answer is checked, and **thrown away** if it:

- has any Latin letter (Uzbek Cyrillic only — the business's rule),
- is too long (text 400, question 110, button 28, reply 160 characters),
- has markup, a question without 2–4 answers, an answer without a reply, or
  two identical buttons, or isn't JSON at all.

The prompt also forbids politics, religion, ethnicity, gender, appearance,
age, health, alcohol, money, promises and anything that mocks anyone.

When the AI fails or its answer is thrown away, a **hand-written** message
from `cheer.py` goes out instead (10 mornings, 6 middays, 6 evenings × 3
questions, rotating by date). Nobody ever gets nothing, and nobody gets
something odd. `cheer_messages.source` says which it was (`ai`/`fallback`).

## Why buttons, not typing

At 17:35 most people still have today's report open (asked at 16:00). A
typed "бугун зўр ўтди" would be saved as their daily report, or offered to
the Director as a message. A tap can't be mistaken. A typed reply *to* one of
these messages (Telegram's Reply) is caught before the report and relay flows
and gets a friendly "😊 Раҳмат!" — never filed, never forwarded.

## Privacy

Taps are stored only so each person answers once. They are **not** shown to
the Director, in the brief, in KPI or in the bot's answers — a mood question
the boss reads is no longer a friendly question.

## Schedule, and why it runs six times

Render gives a cron service one expression, and one expression can't hold
three different minutes. `mgmg-team-cheer` runs `0,35 5,9,12 * * *` (UTC) =
10:00, 10:35, 14:00, 14:35, 17:00, 17:35 Tashkent. Each run sends the slot
whose time passed less than 25 minutes ago (so a cron that starts a few
minutes late still sends), and exits at once otherwise. `cheer_messages` has
one row per day and slot, so a retried or doubled run never sends twice.
One service, ~$1/month.

## Runbook

```bash
python agents/team-cheer/agent.py --slot evening --dry-run   # real AI + employees, prints, sends nothing
python agents/team-cheer/agent.py --slot morning             # send now (only if not sent today)
```

- **Pause:** `TEAM_CHEER_ENABLED=false` in Render's `mgmg-shared`
  (`BOTS_FROZEN=true` stops it too).
- **Change the times:** `SLOTS` in `cheer.py` *and* the schedule in
  `render.yaml` — `selfcheck.py` fails if the schedule no longer hits each
  time exactly once.
- **Change the built-in messages:** `cheer.py`; `selfcheck.py` checks every
  one against the same rules as the AI's.
