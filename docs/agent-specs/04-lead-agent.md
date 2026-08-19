# Agent 4 — Lead Agent (Primus Laundry)

**Code:** `agents/lead-agent/agent.py` + `agents/lead-agent/prompt.py`
**Schedule:** 08:00 Asia/Tashkent (03:00 UTC), daily
**Mode:** read-only from every source; writes to Google Sheets + Telegram, both gated
**Owner:** sales / business development

## Purpose

Find businesses in Uzbekistan that will need industrial laundry equipment
(Track 1) or equipment servicing (Track 2) — **before** the buying decision is
made. A hotel that has already opened has already bought its machines.

Replaces the n8n "Lead Agent" workflow, which needed a paid n8n host plus a
persistent disk on Render; as a native cron job it costs ~$1/month instead.

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
  → drop junk domains (booking/review/social/marketplace)
  → dedupe by normalized URL
  → sort UzEx first (most actionable)
  → AI-qualify in batches of 60
  → drop any lead whose source URL wasn't in the search results
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
