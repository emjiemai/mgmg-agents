# 26 — Garmin competitors in Uzbekistan (2026-10-19)

**Asked:** the owner — "an agent that searches for competitors inside Uzbekistan
for Garmin products, sent to the Garmin manager."

**Code:** `integrations/garmin/competitors.py`, `agents/garmin-competitors/agent.py`
(in the 08:00 job, on `GARMIN_COMPETITORS_WEEKDAY` — Thursday), template
`garmin_competitors.html` (PDF), table `garmin_competitor_offers`.

## Sources (public, read-only)

| Source | What | How |
| ------ | ---- | --- |
| **OLX.uz** | every listing with «garmin» / «гармин» — title, price, new / used, shop or private person, date, link | the public listing API the site's own pages use (`/api/v1/offers/?query=…`, 50 a page, gently); not disallowed by olx.uz/robots.txt. A price in у.е. is OLX's own conversion to so'm. 155 listings on 19.10.2026. |
| **Google** (SerpAPI) | other shops' pages in Uzbekistan — .uz sites, Telegram, Instagram — that sell Garmin | 7 searches (`WEB_QUERIES`, `gl=uz`, `hl=ru`, no date limit): «Garmin купить Ташкент», «site:uzum.uz Garmin» … OLX, garmin.com.uz, news sites and anything abroad left out; the price only when the result states one. Without `SERPAPI_API_KEY` — OLX only, and the page says so. |
| **garmin.com.uz** | our price | the company's public site, `api.garmin.com.uz/api/public/products` (67 products), read live. A product at «0» (price on request) isn't a price. |

## Comparing

By **model**: family + model number, «pro», the MARQ edition — «fenix 8», «fenix 8 pro»,
«epix 2 pro», «forerunner 970», «marq athlete 2» (`model_key`; Cyrillic spellings and
«fenix8» are read too; a year «2026» is not a model). When both say the size
(«47mm»), our price of that size. A variant (colour, glass, band) can still differ —
the page says so; our side is shown as a range (lowest – highest).

- **Биздан арзон** — a **new** item under 95 % of our lowest price for the model.
- **Шубҳали** — a new item under 30 % of ours: a copy or a wrong price; listed apart.
- **Left out**: copies («1:1», «1x1», «копия», «реплика»), repairs, straps / cases /
  chargers (a part word before the product's name, or no model named — «Garmin Venu 3S …
  с зарядкой» stays), and listings that aren't Garmin (no «Garmin» and no Garmin-only
  name like Forerunner / fenix / Instinct).
- Used items are listed and compared, never counted as «арзон».

## The page (PDF) and the caption

Caption: listings (new / used), how many new items are cheaper, shops, other sites.
PDF: cheaper new items (with −%), OLX shops (business accounts: listings, models,
lowest price, how many cheaper), other sites from Google, suspicious ones, then every
OLX listing by model. **ЯНГИ** = first seen since the last check; «аввал …» = it got
cheaper (OLX's own «price before», or ours from the last check). The first check marks
nothing new. The text if the PDF can't be drawn.

## Who, when

- Every **Thursday 08:00** (`GARMIN_COMPETITORS_WEEKDAY`, 0 = Monday) to everyone in the
  `garmin_manager` role; once a week (`agent_actions` `competitors_weekly`); `--force`,
  `--dry-run` (prints the counts). `GARMIN_COMPETITORS_ENABLED=false` stops it.
- **/raqobat** (his ☰ menu) any time — answered in the background ("текширяпман…"
  first); it reuses the Google results of the last 20 hours (search credits), OLX and
  our prices are read again.
- His questions to the bot: `analyst.garmin_data` source `competitors` — what the last
  check found (8 days).

## The shops' contacts (2026-10-20)

"We need to gather details like do'kon nomi, phone number, and Instagram page too."
`integrations/garmin/competitor_shops.py`, table `garmin_competitor_shops`. For each
**shop** — an OLX business account, a site or a Telegram channel Google found — what the
shop itself publishes for customers:

| Where | What |
| ----- | ---- |
| its OLX listing | the name it sells under (a number as its name → its phone; «Seller» → no name) |
| its OLX shop page (`<name>.olx.uz`, when it has one) | the page's own contact block: name, phones, its Telegram / site link, address |
| its site | `tel:` links and +998 numbers, Instagram and Telegram links, the site's name |
| its Telegram channel (`t.me/<channel>`) | the channel's name, phones / Instagram written in its description |
| Google (weekly run only) | no Instagram yet → "<name> instagram" (gl=uz); taken only when the result's title or address holds the name. At most 8 shops a run, each once in 30 days, and only shop-like names (an OLX shop page or site, two words, «A.Mobile») — a lone first name would find somebody else's page |

Pages are read only where the site's robots.txt allows it (at most 40 a run); contacts are
re-read after 7 days. The same Telegram / Instagram / site / name is one shop. The PDF's
«Рақобатчи дўконлар» table: name (and address, models), phones, Instagram, Telegram / site,
OLX listings, lowest price, how many cheaper than ours; the caption counts shops with a
phone and with an Instagram. First live read (20.10.2026): 16 shops, 6 with phones
(Texnikach: 2 phones + t.me/texnikach; Mobilon.Uz; Muslim Mobile …); Instagram comes with
the weekly Google search on Render.

**Not collected:** a private person's phone. OLX shows it only behind its "show phone"
button (login), on purpose, and a person's number is personal data — the PDF has each
listing's link instead.

## Privacy and access

A private seller is «хусусий шахс»: no name, no phone, nothing about them looked up or
stored. Shops are listed with what they publish for customers (above). Nothing goes to the
AI except, when the Garmin manager asks the bot, the stored listings and the shops' names,
Instagram, Telegram, site and address — **never phones** (personal data is masked before AI,
the owner's IT instruction). The tables hold public listings and shop contacts — no company
figures — so `/db` shows them.

## Tests

`scripts/selfcheck.py`, `test_garmin_competitors` (offline — no OLX, Google or
database): model names, sizes, what is left out, our price ranges, OLX parsing (у.е.,
shop vs private person), cheaper / suspicious, new / cheaper since the last check, web
results, the PDF, no private name or phone anywhere, no emoji, the wiring; and (20.10) phones
in any form, a page's phones / Instagram / Telegram, an OLX shop page's contact block, which
listings make a shop (never a private person), one row per shop, which names are searched,
the shops table on the page, robots.txt, phones kept out of the AI.

First live check (19.10.2026, before the first send): 114 Garmin listings (60 new, 48
used), 53 matched to our models, 30 new items cheaper than ours (fenix 8 47mm new from
10,3 млн vs our 17,8 млн; Forerunner 970 7,3 млн vs 12 млн; Venu X1 7,9 млн vs 13,4 млн),
17 OLX shops.
