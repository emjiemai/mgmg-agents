# Garmin AI bot leads → Command Center

**Code:** `integrations/garmin/leads.py` (validation, storage, the Director's data),
`integrations/api/app.py` (`POST /webhooks/garmin-lead/{secret}`); in the bot's
repo (emjiemai/Garmin-AI-bot) `bot/src/leads/commandCenter.js`
**Stored in:** `garmin_leads`
**Switch:** `GARMIN_LEADS_SECRET` here = `COMMAND_CENTER_SECRET` in the Garmin bot

## Why

The Garmin AI bot (@garminofficialuzbot: Instagram Reels → web catalog → AI
Telegram bot → manager) runs on Render's free plan. Its leads lived in memory
and in a file on a disk that every restart wipes — the manager's Telegram
alert was the only lasting record. Since 2026-10-01 every lead also goes to
the Command Center's Postgres.

## How

- The bot POSTs each lead with its own `lead_id` (a retried POST is stored
  once), and a phone number shared later as `{"event": "phone"}` — it lands
  on that customer's latest lead without a phone.
- Wrong secret → 401, malformed → 422 (the bot logs and drops), database
  down → 503 (the bot retries 3 times, then every 5 minutes while running).
- The conversation never waits for it; the manager's alert still goes out.
- The bot's `/stats` says whether anything is waiting to be sent.

The Director asks OPS Manager Bot: "garmin lidlari qanday?", "botdan nechta
lid keldi?", "kim fenix so'radi?" — the `garmin_lidlar` data source (last 30
days: hot/warm, phone, product, the chat summary). The table is also in the
`/db` viewer.

## Setting it up (once)

1. Make a long random secret.
2. Command Center (Render → mgmg-shared): `GARMIN_LEADS_SECRET=<secret>`.
3. Garmin bot (Render → garmin-ai-bot → Environment):
   `COMMAND_CENTER_URL=https://mgmg-api-eeky.onrender.com`,
   `COMMAND_CENTER_SECRET=<the same secret>`.
4. Send `/stats` to the Garmin bot from the manager chat: "🗄 Command Center:
   ✅ все лиды сохранены".
