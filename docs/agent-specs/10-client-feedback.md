# Client feedback via QR codes

**Code:** `integrations/api/feedback_page.py` (the page), `integrations/org_bot/feedback.py`
(validation, delivery), `integrations/org_bot/qr_card.py` (the printable card),
`scripts/make_feedback_qr.py` (cards in bulk)
**Runs inside:** `mgmg-api` — no new service
**Stored in:** `client_feedback`

## What a client does

1. Scans the QR card at a location — red card, black code on a white square
   in the centre, "ФИКР ВА ШИКОЯТЛАР" on top.
2. Gets one screen (`/f/<place>`): **💬 Фикр** or **⚠️ Шикоят**, a text box,
   and an optional name and phone. Leaving both empty sends it anonymously.
3. Presses **Юбориш** → "✅ Раҳмат! Хабарингиз раҳбариятга юборилди."

The Director immediately gets, through OPS Manager Bot:

    ⚠️ Мижоз: шикоят — 📍 garmin

    «Кассада навбат жуда узун эди»

    📞 +998901234567 · Алишер

and can ask the bot any time: "mijozlar nima deyapti?", "shikoyatlar bormi?"
(the `mijoz_fikrlari` agent, last 60 days).

## Places

Each card carries a place label in its address — `/f/garmin`,
`/f/ondry-chilonzor` — so every message says where it came from. Labels are
lowercase Latin letters, digits and dashes; `/f` alone is "umumiy" (general).

## Making the cards

- **Admin Bot:** `/qr garmin` → a print-ready PNG (1200×1500 px, 300 dpi,
  about 10×12.7 cm) for that place.
- **In bulk, on your computer:**
  `python scripts/make_feedback_qr.py --base https://<address> garmin ondry-chilonzor`

The white square behind the code is deliberate: phone scanners read
brightness, and black straight on red is hard for many of them; the QR
standard also requires a light margin. `scripts/selfcheck.py` reads the
printed card back pixel by pixel and checks it is exactly the right code,
black on white, with that margin.

**Before printing:** the address is baked into every printed card and can't
be changed afterwards. Set `PUBLIC_BASE_URL` to a domain the company
controls (e.g. `https://fikr.<company domain>`, pointed at the Render service)
rather than the `onrender.com` address, so moving hosting later doesn't break
cards already on counters. Without it, the Render address is used.

**Speed:** `mgmg-api` is on Render's free plan, which sleeps after 15 minutes
without traffic; the first scan after that waits about a minute for it to
wake. A client at the counter won't wait that long — move `mgmg-api` to the
Starter plan (~$7/month; it also makes the bots answer instantly) before the
cards go out.

## Protection (the page is public)

- A hidden field bots fill and people don't — such posts are thanked and
  dropped.
- At most 5 messages per 10 minutes from one address (in memory; **no IP
  address is stored** or sent anywhere — anonymous means anonymous).
- Message 3–2000 characters, name up to 60, phone checked as a phone number.
- Everything the client typed is escaped before it reaches Telegram.
- If saving fails, the client sees "Хатолик юз берди…" with their text kept,
  never a lost message dressed as success.

## Switch

`FEEDBACK_ENABLED` (default `true`) — `false` makes `/f` return 404.
