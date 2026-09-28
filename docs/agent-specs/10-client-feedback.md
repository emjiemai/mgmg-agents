# Client feedback via one QR code

**Code:** `integrations/api/feedback_page.py` (the page), `integrations/org_bot/feedback.py`
(validation, delivery), `integrations/org_bot/qr_card.py` (the printable card),
`scripts/make_feedback_qr.py` (the card on your own computer)
**Runs inside:** `mgmg-api` — no new service
**Stored in:** `client_feedback`

## What a client does

1. Scans the company's QR card — red card, black code on a white square in
   the centre, "ФИКР ВА ШИКОЯТЛАР" on top. **One code for the whole company**
   (2026-09-28): clients don't see separate services, so there are no
   per-location codes.
2. Gets one screen (`/f`): **💬 Фикр** or **⚠️ Шикоят**, a text box, and an
   optional name and phone. Leaving both empty sends it anonymously.
3. Presses **Юбориш** → "✅ Раҳмат! Хабарингиз раҳбариятга юборилди."

The Director immediately gets, through OPS Manager Bot:

    ⚠️ Мижоз: шикоят

    «Кассада навбат жуда узун эди»

    📞 +998901234567 · Алишер

and can ask the bot any time: "mijozlar nima deyapti?", "shikoyatlar bormi?"
(the `mijoz_fikrlari` agent, last 60 days).

Links from early test cards (`/f/garmin`) redirect to `/f`.

## Making the card

- **Admin Bot:** `/qr` → a print-ready PNG (1200×1500 px, 300 dpi, about
  10×12.7 cm).
- **On your computer:** `python scripts/make_feedback_qr.py --base https://<address>`

The white square behind the code is deliberate: phone scanners read
brightness, and black straight on red is hard for many of them; the QR
standard also requires a light margin. `scripts/selfcheck.py` reads the
printed card back pixel by pixel and checks it is exactly the right code,
black on white, with that margin.

## The address

The address is inside the code, so clients never type it; they only see it
in the browser bar after scanning. It is also baked into every printed card
and can't be changed afterwards.

The Render address (`mgmg-api-eeky.onrender.com`) can't be made nicer: Render
chose it when the service was created. The only way to a clean address is a
domain the company already owns (or buys): add a subdomain such as
`fikr.<company-domain>` in Render (service → Settings → Custom Domains), add
the CNAME record Render shows at the domain's DNS provider, wait for Render's
free HTTPS certificate, then set `PUBLIC_BASE_URL=https://fikr.<company-domain>`
and make the card with `/qr`. Print only after that — then a future move of
hosting only means re-pointing the domain, not reprinting cards.

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
