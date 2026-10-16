# 24 — Garmin market watch: tenders, competitors, leads

**From the owner, 2026-10-16:** for the Garmin business, find leads, competitors and tenders.
**Code:** `agents/garmin-market/agent.py`, rules and texts in `integrations/garmin/market.py`,
UzEx keywords in `integrations/tenders/uzex_client.py` (`GARMIN_KEYWORDS`).
**When:** in the 08:00 job — tenders daily, competitors on Mondays (`--competitors` any day).
**To:** the Garmin manager (role `garmin_manager`); the Director while nobody holds it (`notify.notify_role`).
**Off:** `GARMIN_MARKET_ENABLED=false`.

## What was checked first (16.10.2026)

- **No official Garmin distributor in Uzbekistan** turned up; the Golden Pages "GARMIN STORE, Abay
  street" is our own shop. The competition is everyone else selling Garmin: marketplaces (Uzum,
  Asaxiy, Texnomart, Mediapark, Idea), classifieds (OLX, Prom) and price comparison (Glotr).
- **UzEx e-tender** (`apietender.uzex.uz/api/common/TradeList`, public, no key) answers; on 16.10 no
  open lot matched any Garmin word — such lots are rare, so the check runs daily and sends only when
  one appears.
- **xarid.uzex.uz** (the state e-shop, small purchases) has public endpoints (`xarid-api-shop.uzex.uz`
  `/Common/GetCompletedDeals`, `GetProcurementPlans`, …) found in its JS bundle, but their filter
  fields aren't known yet (every guess returned `[]`). Not wired in; the Lead Agent still reaches it
  through `site:xarid.uzex.uz` searches. Next step if wanted: its completed deals would show **who
  sells Garmin-type goods to the state and at what price**.

## Tenders (daily)

1. UzEx lots of both live trade types whose title has a `GARMIN_KEYWORDS` stem (garmin, tanita, tacx,
   смарт-часы / aqlli soat, спортивные часы, фитнес-браслет, пульсометр, навигатор, эхолот,
   велокомпьютер / велотренажер, состав тела / медицинские и напольные весы, спутниковый коммуникатор).
2. Lots judged before are skipped (`garmin_market`, kind `tender`, key = lot id).
3. The AI keeps a lot only when its **title** buys a Garmin / Tanita / Tacx product (no wall clocks,
   phones, GPS-tracking services, generic sports equipment); an id it invents is dropped.
4. New relevant lots → one message: title, buyer, budget, deadline, link. None → nothing sent.

## Competitors (Mondays)

1. 12 Google searches through SerpAPI (`COMPETITOR_QUERIES`: one per marketplace + model / Uzbek
   phrasings) — about 12 SerpAPI credits a week.
2. The AI reads each result's own title and snippet into a sighting: seller, model, price, currency,
   "official" claim. Kept only if the text names Garmin; Garmin's own and our sites, social and news
   pages are dropped; a **price is kept only when its digits are in the text**; "official" only when
   the page says so.
3. Stored (`garmin_market`, kind `competitor`, key = page address, `last_seen`).
4. The weekly message: where Garmin is sold (sellers per site), who says "official", **new sellers**
   since last week, each model's price range in so'm.

## Leads

Stay with the Lead Agent (`04-lead-agent.md`, Track 3 — Garmin / Tanita / Tacx sponsorship and
bulk-sale signals, five more segments added 16.10: flight schools / airlines, fishing and hunting
clubs, rescue services, triathlon, regional races). New: its Garmin leads (`is_garmin_lead`) also go
to the Garmin manager with the signal and source link; the Director's summary is unchanged.

## Data and IT

Readings in `garmin_market` (not a `/db` technical table). The run's `agent_actions` row keeps counts
only (`tenders`, `sightings`). Not done: our own prices next to the competitors' (Billz has them),
Instagram / Telegram shops (not searchable through Google).
