# Client complaints via QR codes — Londry and Garmin

**Code:** `integrations/api/feedback_page.py` (the page), `integrations/org_bot/feedback.py`
(validation, delivery), `integrations/org_bot/qr_card.py` (the printable card),
`scripts/make_feedback_qr.py` (the card on your own computer)
**Runs inside:** `mgmg-api` — no new service
**Stored in:** `client_feedback` (`place` = `laundry` | `garmin`, `kind` = `complaint`)

## Tickets for the department (2026-10-13)

The owner's spec of 11.10 (A4 LONDRY-CARE, A5 GARMIN-CARE) and his answer of
13.10: complaints go to the department, **not to the Boss**.

- `integrations/org_bot/routes.py` — the route table: `complaint:laundry` →
  the **Londry** role, `complaint:garmin` → the **Garmin менежер** role
  (2026-10-14 — not Garmin сотув; `23-garmin-manager.md`), everyone active
  in the role. The hourly question goes to whoever holds the role then, and
  joins the ticket's cards so they can press ✅ / ❌. Nobody in the role → the Boss, with a note saying why.
- Every complaint becomes a ticket: a code from the task sequence
  (T-2026-10-0031), `ticket_status` open → resolved, the cards each person
  got (`cards`). The card: **✅ Ҳал бўлди** → a reason code (Сифат, Хизмат /
  муомала, Машина ёки тўлов, Кечикиш, Бошқа) → every card shows who closed it
  and why. Only the people it was sent to can press.
- An hour after it arrived (or after the last question), if still open:
  "ҳал бўлдими?" with ✅ / ❌ — between 09:00 and 20:00, at most three times
  (`feedback.ask_status`, run every 5 minutes inside `mgmg-api`).
- The Boss can still ask the bot about complaints («шикоятлар бормы?»).
- Complaints from before 13.10 are not tickets (no status) and are never asked about.

Next from the spec: escalation (2 h → department head, 4 h → coordinator).

## What a client does

1. Scans the business's QR card — red card, black code on a white square.
   **Each business has its own code and page**; the address decides the
   business, the client never chooses:

   | Business | Page | |
   | -------- | ---- | - |
   | Londry | `/f` | the first printed card ("SHIKOYAT / COMPLAINT / ЖАЛОБА") points here — **never move it** |
   | Garmin | `/f/garmin` | |

   Anything else after `/f/` (early test codes, `/f/laundry`) opens Londry's page.
2. Gets one screen, complaints only (2026-09-28 — the "Фикр" choice is
   gone; this is the company's complaints channel). The business's name sits
   at the top left, then:
   - **Нима бўлди?** — the complaint;
   - **Сиз билан боғланайликми?** — optional name and phone; both empty =
     anonymous.
3. Presses **Шикоятни юбориш** → "Раҳмат! Шикоятингиз раҳбариятга юборилди."

## Design

Made for a phone, read by someone who is already annoyed: 18 px body text,
30–38 px title, 60 px send button; drawn icons, no emoji. An
error sits next to the field it is about (with the field outlined in red),
and everything typed is kept. Light and dark themes follow the phone; every
text meets WCAG AA contrast and input borders 3:1. No JavaScript — the whole
page is a few kilobytes and works on any phone.

## Branches (2026-10-04)

Each page starts with «Қайси филиал?» and two big buttons — the client must
pick one before sending — the form can't be sent without it (checked on the
server, not only in the browser):

| Page | Branches (key) |
| ---- | -------------- |
| Londry `/f` | Londry Юнусобод (`yunusobod`) · Londry Вузгородок (`vuzgorodok`) |
| Garmin `/f/garmin` | Абай (`abay`) · Минор (`minor`) |

The branch is stored in `client_feedback.branch` (NULL on older complaints)
and named in the Director's message: «🔴 Мижоз шикояти — Garmin · Абай».
The names are translated on the Russian/English pages; the Director always
gets Uzbek Cyrillic. `?branch=abay` preselects a button — so a branch can
later get its own printed code (`/f/garmin?branch=abay`) without changing
anything else. Branches live in `feedback.BRANCHES` (and their translations
in `feedback_page.BRANCH_LABELS`).

## Languages

**Uzbek Cyrillic, Russian and English** (Uzbek Latin removed 2026-09-28).
The switcher at the top — "Ўзбекча · Русский · English" — changes it at
any time (`?lang=uz_cyrl|ru|en`) and stays on the same business's page. Without a
choice the page opens in the phone's language: Russian → Russian, English →
English, Uzbek (either script) and anything else → Uzbek Cyrillic. Errors
and the thank-you page stay in the chosen language.

The Director's message is always Uzbek Cyrillic; the client's text is
passed on as written, and a non-default language is noted:
`🌐 Мижоз тили: русча`.

The Director immediately gets, through OPS Manager Bot — the 🔴 makes it
stand out in the chat:

    🔴 Мижоз шикояти — Garmin

    «Соат экрани бир ҳафтада синиб қолди»

    📞 +998901234567 · Алишер

and can ask the bot any time: "shikoyatlar bormi?", "Garmin bo'yicha
shikoyat bormi?" (the `mijoz_fikrlari` agent, last 60 days; rows from
before 2026-09-28 may be opinions and are marked so).

## Making the card

- **Admin Bot:** `/qr` → two print-ready PNGs, Londry's and Garmin's
  (`qr-shikoyat-londry.png`, `qr-shikoyat-garmin.png`; 1200×1450 px, 300 dpi,
  about 10×12.3 cm): red card, the business's logo in white on top, the code
  on a white square below — no text (2026-09-28). The logos are in
  `integrations/org_bot/logos/` with their background cut out; they came as
  small images, so a larger or vector logo with the same file name prints sharper.
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

The cards use Render's address (`mgmg-api-eeky.onrender.com`) — decided
2026-09-28: no company domain for now. Render chose that name when the
service was created and it can't be changed; it only changes if the service
is recreated, and then the cards must be reprinted. (A company domain, if
one is ever wanted: Render → service → Settings → Custom Domains, the CNAME
at the domain's DNS, `PUBLIC_BASE_URL`, then `/qr` and reprint.)

**Speed:** `mgmg-api` is on Render's Starter plan since 2026-09-28 (0.5 CPU,
512 MB, always on), so a scan opens the page at once — no cold start.

## Protection (the page is public)

- A hidden field bots fill and people don't — such posts are thanked and
  dropped.
- At most 5 messages per 10 minutes from one address (in memory; **no IP
  address is stored** or sent anywhere — anonymous means anonymous).
- Message 3–2000 characters, name up to 60, phone checked as a phone number.
- Everything the client typed is escaped before it reaches Telegram.
- If saving fails, the client sees "Хатолик юз берди, шикоят юборилмади…" (in
  their language) with their text kept, never a lost message dressed as success.

## Switch

`FEEDBACK_ENABLED` (default `true`) — `false` makes `/f` return 404.
