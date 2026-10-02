# AI chat for employees — help with their work, no company data

**Code:** `integrations/org_bot/ai_chat.py` (rules, prompt, texts),
`integrations/org_bot/ops_manager.py` (`/ai`, the answers), `admin.py` (the 🤖 grant)
**Stored in:** `employees.ai_chat` (granted), `employees.ai_chat_until` (on until),
`ai_chat_turns` (the chat's memory, one day)
**AI:** OpenRouter, the OPS Manager Bot model chain

Asked for by the owner on 2026-10-02: some employees — B2B sales first —
should be able to talk with the AI about their work, without access to the
company's platforms or information.

## How it works

| Who | What |
| --- | ---- |
| Admin | Admin Bot `/xodimlar` → the person → **🤖 AI суҳбат: ☐ бериш**. They're told: "…сизга AI ёрдамчи очилди, ишингиз бўйича савол бериш учун /ai ни босинг 🤖". Tap again to take it away (ends a running chat too). |
| Employee | **`/ai`** turns it on; their plain messages then go to the AI. **`/ai`** again turns it off; 20 quiet minutes also end it. Without the grant `/ai` gets a polite "not opened for you yet". |

While it's on, a Telegram **Reply** to one of the bot's own messages (the
16:00 report ask, a lead question, a cheer) keeps its usual meaning, and
commands (`/kpi`, `/natija`…) work as always. Answers run in the background
like the Director's questions, so Telegram never waits on the AI.

## What the AI can and can't do

- **Knows:** only public facts — what MGMG sells (Primus Londry equipment and
  services; Garmin and Tanita). Garmin sales also get the public Garmin
  catalog (prices may have changed).
- **Doesn't have:** any company system or data — no SAP, sales figures,
  debts, cash, stock, reports, tasks, KPI, leads, customers or anything about
  other employees. It says so and points to the manager or the Director; it
  never invents a number, a name or a date.
- **Can't act:** it can't send, save or change anything or contact anyone.
- **Helps with:** drafting a message, email or commercial offer, preparing a
  call, answering objections, explaining a product category, planning the
  day, Excel/Word, translating a text they give it.
- **Writes:** Uzbek Cyrillic, always the polite "сиз", especially courteous
  with women; a draft asked for in another language (a Russian email) is in
  that language. Telegram HTML only.

## Limits and privacy

60 questions a day per person ("бугунги AI саволлари чегарасига етдингиз…").
The chat is kept one day as its memory (the last 12 turns are sent with each
question) and shown to no one — not the Director, not KPI. Every AI call is
in `agent_actions` like any other.

## Checks

`selfcheck.py` (`test_ai_chat_and_sheet`): who may chat and when it's on;
the prompt's "no company data / no actions / never invent"; Garmin-only
catalog; every fixed message friendly and polite; `/ai` on/off, plain
message → AI, Reply → usual flow, the answer and the daily limit; the admin
grant and the employee being told.
