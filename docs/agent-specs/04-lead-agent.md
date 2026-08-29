# Agent 4 — Lead Agent (Primus Laundry + EMJIEM sports-tech)

**Code:** `agents/lead-agent/agent.py` + `agents/lead-agent/prompt.py`
**Schedule:** 08:00 Asia/Tashkent (03:00 UTC), daily
**Mode:** read-only from every source; writes to Google Sheets + Telegram, both gated
**Owner:** sales / business development

## Purpose

Find businesses in Uzbekistan that will need industrial laundry equipment
(Track 1) or equipment servicing (Track 2) — **before** the buying decision is
made. A hotel that has already opened has already bought its machines. As of
2026-08-29 also finds Garmin/Tanita/Tacx sponsorship, partnership, and bulk
equipment-sale opportunities (Track 3 — different buying motion, no
construction timeline, see `prompt.py`'s Track 3 section).

Replaces the n8n "Lead Agent" workflow, which needed a paid n8n host plus a
persistent disk on Render; as a native cron job it costs ~$1/month instead.

**Product scope** (`prompt.py`'s grounding):
- ONDRY/laundry (confirmed live against `primuslaundry.uz/products` and
  `/services` 2026-08-20): hardware is Washer-Extractors, Tumble Dryers, and
  Flatwork Ironers; services are Design, Installation, Maintenance, and Spare
  Parts. Chemicals/detergents are in scope per the business owner directly —
  not shown on the public site, so unconfirmable the way the rest was.
- IMUS-Alliance/sports-tech (confirmed directly by the business owner
  2026-08-29, no public product page checked yet): the full Garmin range —
  consumer sport watches through tactical/government-grade GPS — plus Tanita
  professional body-composition analyzers and Tacx indoor cycling trainers.
  Geographic scope confirmed Uzbekistan-only, matching Track 1/2.

Track 3's search queries and qualification rules were drafted from the CEO's
"EMJIEM A-Z Watch List" and verified via an 18-agent live-web-search pass
before being written into `prompt.py`/`agent.py` — see the published field
report for the 15 real leads that pass surfaced and, more importantly, which
query patterns turned out to be structural dead ends (generic "seeking a
sponsor" phrasing, retail/wholesale-intent queries, and direct web search
for defense/police procurement, which runs through non-public channels).
Defense/Police leads are handled the same way Track 2 already handles
military laundry-service leads: "requires direct institutional contact, not
web-sourced" rather than fabricated.

## Sources, ranked by signal quality

| Source | What it gives | Notes |
| ------ | ------------- | ----- |
| **UzEx** (`etender.uzex.uz`) | **Official government tenders** — named buying org, exact budget, tax id, firm deadline | Highest signal by far. ~640 open lots. No key needed. |
| SerpAPI | Google + Bing + Yandex organic results | Yandex matters for Russian/Uzbek regional coverage |
| Tavily | Recent news | Good for investment/opening announcements |
| World Bank | Uzbekistan project pipeline | Health/urban development projects, no key needed |
| bicotender.ru | Aggregated tenders | Reached via `site:` search — well indexed, unlike the official portals |

### The UzEx integration — how it was found

This is worth recording, because it is not discoverable from the outside.

`etender.uzex.uz` is an Angular SPA. Every plausible API path on that host
returns the HTML shell, which is why guessing URLs (including the ones the n8n
workflow appeared to use) produced nothing but markup.

The real API was found by downloading the site's own JS bundle and reading its
configuration:

- The backend is a **separate host**: `apietender.uzex.uz` (the bundle's `serverUrl`).
- The public listing endpoint is `POST /api/common/TradeList`.
- Pagination uses **`From` / `To`** (1-based, inclusive) — not `page`/`pageSize`.
  Wrong field names return the Uzbek error *"Sahifa chegaralari noto'g'ri"*
  ("page boundaries are incorrect"), which is what makes this hard to guess.
- Filter fields (`Keyword`, `TypeId`, `RegionId`, `PriceMin/Max`,
  `DeadlineStart/End`, `CustomerTin`, …) were read from the bundle's own
  `tradeListFilter` model, then verified against live responses.

Trade types, verified live 2026-08-19:

| `TypeId` | Meaning | Open lots |
| -------- | ------- | --------- |
| 1 | competitive bidding | 591 |
| 2 | tender | 51 |
| 3–6 | frame agreement / other | 0 |

Only 1 and 2 are polled. The client runs an unfiltered sweep of each **plus**
keyword searches in Uzbek and Russian — keyword lists always have blind spots,
so the unfiltered pass is what catches a relevant lot phrased unexpectedly.

**If UzEx changes:** re-run the bundle inspection. Pull
`https://etender.uzex.uz/main.<hash>.js`, grep for `serverUrl`, and grep for
`tradeListFilter\.[A-Za-z]+` to re-derive the field names.

## Pipeline

```
fetch all sources concurrently
  → drop junk domains (booking/review/social/marketplace/classifieds)
  → dedupe by normalized URL
  → sort UzEx first (most actionable)
  → AI-qualify in batches of 60 (pass 1)
  → drop any lead whose source URL wasn't in the search results
  → drop any lead that fails the code-level hard filters (see below)
  → AI-verify pass 2 — adversarial re-check of whatever survived pass 1
  → dedupe against the existing sheet
  → append new rows + post Telegram summary
```

### Why batching

One prompt with 600+ candidates is both a reliability risk (large payloads fail
more often) and a quality one (attention spread thin). At 60 per batch, a
failed batch costs one slice of the day's leads instead of the whole run. The
run only fails if *every* batch fails.

### The grounding check — do not remove this

The model is instructed to only cite URLs it was actually given. It does not
always comply: in one run it produced a "Hyatt Regency Tashkent" lead citing an
Expedia URL that was never in the search results. `qualify_leads` therefore
verifies every `signal_source_url` against the real candidate set in code and
drops mismatches. Prompt instructions are not a substitute for this.

Notably, the old n8n workflow's output contained the *same* unverified Hyatt
Regency claim (sourced from an unrelated Facebook page) — independent
corroboration that this backstop catches real noise.

### Two-pass qualification + hard filters (added 2026-08-20)

A live audit of the real sheet (80 rows) found 17 with no genuine laundry/
textile-care connection — catering tenders, a bank IT tender, oxygen-generator
maintenance, a state-share valuation, and one hotel that was actually in
Russia — despite passing the grounding check above. The common root cause:
the model's own reasoning *inferred* a laundry connection ("large facility
likely needs X") instead of finding one stated in the source text. All 17 were
removed from the live sheet after re-confirming each against fresh data.

Two layers were added on top of the existing grounding check, neither of which
depends on the model choosing to follow prompt instructions correctly:

1. **`passes_hard_filters()` in `agent.py`** — runs on every lead pass 1
   proposes, right after the URL/track checks. Rejects on:
   - a match against `DISQUALIFYING_KEYWORDS` (catering, banking/payment,
     medical gas, furniture-only, privatization/valuation — the exact
     categories that slipped through before), checked first and overriding
     everything else;
   - a missing or ungrounded `relevance_quote` — a new required output field
     the model must fill with real text copied from the source, checked in
     code against that source's actual title/snippet (same idea as the URL
     check, aimed at content instead of the citation itself);
   - no match against `LAUNDRY_KEYWORDS`, unless the lead is a qualifying
     facility type (`QUALIFYING_FACILITY_KEYWORDS`) genuinely under
     construction/tender (the code counterpart of the prompt's "hotel/hospital
     construction needs no literal word 'laundry'" rule);
   - a `FOREIGN_RED_FLAGS` match (Russia, Kazakhstan, etc.) with no
     `UZ_LOCATION_KEYWORDS` match — this is what would have caught the Repino
     (Russia) hotel in code even if the prompt had been ignored.

   `relevance_quote` is internal to the pipeline — it is not one of the 20
   sheet columns and is never written to the sheet.

2. **Pass 2 — `verify_leads()` / `VERIFY_SYSTEM_PROMPT`** — a second, separate
   AI call shown each surviving lead *alongside its original source snippet*,
   told explicitly that a prior pass has made these exact mistakes before, and
   instructed to default to rejecting on any doubt. This catches judgment
   calls the keyword filters can't, e.g. rejecting a tender for outsourced
   laundry *services* (the buyer wants someone to run their laundry for them,
   which is a different business than buying/maintaining Primus equipment) —
   confirmed on a live test run, along with a generic government policy
   announcement and an OLX classifieds listing.

   Fails **closed**, unlike pass 1: if a verify batch's API call fails, or the
   model skips a lead without giving it a verdict, that lead is dropped rather
   than shipped unverified. If the entire pass fails to run, `run()` ships zero
   leads that day rather than falling back to pass-1-only output — consistent
   with "an empty result is correct and expected, a wrong one is not."

### Model fallback

Free (`:free`) OpenRouter models share a congested pool and returned 429 on 3
of 4 local test runs — confirmed via OpenRouter's own request logs, which show
requests reaching Google AI Studio / Darkbloom and being rejected *there*, so
it is upstream congestion, not an account quota.

`OPENROUTER_MODEL` is tried first, then each entry in
`OPENROUTER_FALLBACK_MODELS` (comma-separated). **End that chain on a cheap
paid model** — this agent runs unattended and must not silently produce nothing.

## Output

The Google Sheet's existing 20-column schema is matched exactly:

```
company_name, project_name, industry, location, project_stage,
estimated_opening, signal, signal_source_url, signal_date, estimated_size,
contact_name, contact_role, contact_method, confidence, priority,
recheck_date, notes, date_added, dedupe_key, track
```

`dedupe_key` is `{company_name lowercased}|{track}`, matching the convention
already in the sheet's newer rows. Deduplication checks **both** that key and
the raw `signal_source_url`, because older rows in the sheet use URL-only.

## Write gate

`AGENT_WRITES_ENABLED=false` means the agent computes everything, logs exactly
what it *would* write, and sends nothing — to either the sheet or Telegram.
**This is the usual reason "nothing appeared" after a successful run.** The
sheet holds real, human-curated lead data, so review a dry run before opening
the gate.

## Runbook

```bash
python agents/lead-agent/agent.py --dry-run   # print, write nothing
python agents/lead-agent/agent.py             # live (needs the write gate open)
```

**Nothing in the sheet / Telegram?** Check `AGENT_WRITES_ENABLED` first, then
the logs for `[write gate closed] would append N row(s)`.

**Every batch failing?** The model chain is exhausted — check the OpenRouter
dashboard's request log to distinguish account-level 429s from upstream
provider congestion, then add a paid model to the chain.

## Known gaps

- `xarid.uzex.uz` (the mandatory state procurement portal) runs a separate set
  of microservices (`xarid-api-trade`, `-purchase`, `-shop`, `-auction`); their
  listing endpoints appear to be authentication-gated, unlike etender's.
  Worth revisiting with a logged-in session's network trace.
- `data.egov.uz` and `xt-xarid.uz` endpoints remain unconfirmed.
- Chamber of Commerce tender board (`chamber.uz`) — covers *private* company
  tenders, but the host was unreachable during testing.
- ADB / Islamic Development Bank pipelines are not yet integrated (World Bank is).
- SerpAPI returned HTTP 429 on roughly half of queries during a 2026-08-20 test
  run (some exhausted all 3 retries and were dropped entirely) — unrelated to
  the qualification-quality fix made that day. Worth checking the SerpAPI
  dashboard's plan/quota before assuming every low-volume day is the new
  filters being too strict; Tavily and UzEx were unaffected in the same run.
  Reconfirmed 2026-08-29: still 429ing on a single test call, so this is an
  ongoing plan/quota ceiling, not a one-off — the Track 3 query volume added
  that day makes this worse, not better, until the plan is upgraded or the
  per-run query count is trimmed. Tavily was unaffected again in the same test.
