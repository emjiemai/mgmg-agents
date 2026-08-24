"""Lead Agent — daily B2B lead sourcing for Primus Laundry Uzbekistan.

Replaces the n8n "Lead Agent" workflow with a native Python cron job, so it
runs on Render without paying for an n8n web service + persistent disk.

Pipeline: search multiple engines/sources -> dedupe by URL -> AI-qualify
against the two-track brief in prompt.py -> drop anything not grounded in a
real search result -> check against the existing Google Sheet -> append only
genuinely new leads -> post a summary to the Telegram leads group.

Writes are gated behind AGENT_WRITES_ENABLED (security rule #1) same as every
other agent — the target sheet holds real, human-curated lead data, so the
first runs should be reviewed as dry-run output before the gate is opened.

Run:
    python agents/lead-agent/agent.py --dry-run   # print, touch nothing
    python agents/lead-agent/agent.py             # send (once writes enabled)

Cron (VPS/Render, UTC — 03:00 UTC = 08:00 Tashkent):
    0 3 * * * ... python agents/lead-agent/agent.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.ai.openrouter_client import OpenRouterClient, OpenRouterError
from integrations.common.config import settings
from integrations.common.db import close_pool, log_action
from integrations.common.logging_setup import setup_logging
from integrations.google.sheets_client import SheetsClient, SheetsError
from integrations.search.models import RawLead
from integrations.search.serpapi_client import SerpAPIClient, SerpAPIError
from integrations.search.tavily_client import TavilyClient, TavilyError
from integrations.org_bot.notify import notify_directors
from integrations.telegram.bot import escape
from integrations.tenders.uzex_client import UzExClient
from integrations.tenders.worldbank_client import WorldBankClient, WorldBankError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompt import (  # noqa: E402
    SYSTEM_PROMPT,
    VERIFY_SYSTEM_PROMPT,
    build_user_message,
    build_verify_message,
)

AGENT = "lead-agent"
log = setup_logging(AGENT)

SHEET_TAB = "Sheet1"
SHEET_RANGE = f"{SHEET_TAB}!A:T"  # 20 columns, company_name..track
SHEET_COLUMNS = [
    "company_name", "project_name", "industry", "location", "project_stage",
    "estimated_opening", "signal", "signal_source_url", "signal_date",
    "estimated_size", "contact_name", "contact_role", "contact_method",
    "confidence", "priority", "recheck_date", "notes", "date_added",
    "dedupe_key", "track",
]

# Domains that can never contain a lead, only noise. Booking/review sites in
# particular flooded earlier runs — a hotel having a Booking.com page says
# nothing about whether it is buying laundry equipment, and one such URL was
# the exact hallucinated "source" the grounding check caught in a prior run.
# Filtering here (rather than asking the model to ignore them) cuts prompt
# size, cost, and the opportunity to cite them at all.
JUNK_DOMAINS = {
    "booking.com", "expedia.com", "tripadvisor.com", "agoda.com", "hotels.com",
    "airbnb.com", "trivago.com", "kayak.com", "makemytrip.com", "hostelworld.com",
    "youtube.com", "pinterest.com", "reddit.com", "quora.com", "wikipedia.org",
    "facebook.com", "twitter.com", "x.com", "tiktok.com",
    "amazon.com", "alibaba.com", "aliexpress.com", "ebay.com", "made-in-china.com",
    "indeed.com", "glassdoor.com", "olx.uz",
}

# ---------------------------------------------------------------------------
# Code-level qualification backstops (added 2026-08-20).
#
# The AI qualification prompt is the primary filter, but a live audit of the
# real sheet found 17 of 80 rows with no real laundry/textile-care connection
# — catering tenders, a bank IT tender, oxygen-generator maintenance, a state-
# share valuation, a furniture tender with invented "implies dorm laundry"
# reasoning, and one hotel that was actually in Russia, not Uzbekistan. Every
# one of those slipped past the prompt's own rules. These checks catch the
# same failure modes independent of whether the model followed the prompt —
# the same defense-in-depth idea as the existing URL-grounding check below,
# just aimed at content instead of source authenticity.
# ---------------------------------------------------------------------------

# Specific procurement categories that showed up as false positives. Kept to
# multi-word, specific phrases rather than single ambiguous words (e.g. not
# bare "bank" or "mebel") to avoid rejecting genuine leads that merely mention
# an adjacent word in passing.
DISQUALIFYING_KEYWORDS = [
    # catering / food service — the exact mistake that triggered this fix
    "приготовлен", "обществен питани", "meal preparation", "meal service",
    "food service", "food outsourcing", "catering", "taomnoma", "ovqat tayyorlash",
    "ovkat tayyorlash", "овкат тайёрлаш", "иссик овкат", "issiq ovqat", "nutrition service",
    # banking / financial IT — wrong industry entirely
    "payment terminal", "платежный терминал", "платёжный терминал",
    "core banking", "банковской инфраструктур", "банковская инфраструктур",
    # medical gas systems — a different equipment category
    "oxygen generator", "oxygen concentrator", "кислородный генератор",
    "кислородная станция", "psa plant", "medical gas",
    # furniture-only procurement
    "furniture supply", "поставка мебели", "mebel yetkazib berish",
    # ownership / privatization actions, not equipment purchases
    "privatization", "приватизаци", "valuation of state share",
    "оценка государственной доли", "давлат улуши",
]

# Laundry/textile-care terms across every language these sources appear in.
# Checked against the model's own signal/notes/relevance_quote text.
LAUNDRY_KEYWORDS = [
    "laundry", "washer-extractor", "washer extractor", "tumble dryer",
    "flatwork ironer", "dry-clean", "dry clean", "textile care", "linen",
    "прачечн", "стиральн", "гладильн", "сушильн", "химчист", "текстил",
    "kir yuvish", "kir yuvuvchi", "kimyoviy tozalash", "dazmol",
    "quritish mashinasi", "кир ювиш", "кимёвий тозалаш", "дазмол",
]

# Facility types that inherently run their own laundry — allowed to qualify
# without a literal laundry keyword ONLY when the signal is specifically about
# that facility being built/opened (see is_construction_stage in
# passes_hard_filters). This is the code counterpart of the prompt's rule 2.
QUALIFYING_FACILITY_KEYWORDS = [
    "hotel", "гостиниц", "mehmonxona", "hostel", "resort",
    "hospital", "больниц", "shifoxona", "clinic", "поликлиник", "klinika",
    "dry-clean", "химчист", "kimyoviy tozalash", "laundry", "прачечн", "kir yuvish",
]

UZ_LOCATION_KEYWORDS = [
    "uzbekistan", "o'zbekiston", "ozbekiston", "ўзбекистон", "узбекистон",
    "tashkent", "toshkent", "ташкент", "samarkand", "samarqand", "самарканд",
    "bukhara", "buxoro", "бухара", "khiva", "xiva", "хива", "fergana",
    "farg'ona", "fargona", "фергана", "namangan", "наманган", "andijan",
    "andijon", "андижан", "nukus", "нукус", "qarshi", "карши", "termez",
    "термез", "navoiy", "navoi", "навои", "jizzax", "джизак", "guliston",
    "гулистон", "urgench", "urganch", "ургенч",
]

# Countries a brand-name search ("Hilton Uzbekistan") can accidentally surface
# a property from. If one of these appears with no Uzbekistan marker also
# present, the lead is almost certainly geographically wrong — this is exactly
# how a Russian AZIMUT hotel (Repino, Leningrad Oblast) slipped through.
FOREIGN_RED_FLAGS = [
    "russia", "rossiya", "росси", "moscow", "москва", "saint petersburg",
    "petersburg", "санкт-петербург", "repino", "kazakhstan", "qozog'iston",
    "казахстан", "kyrgyzstan", "киргизия", "кыргызстан", "tajikistan",
    "таджикистан", "turkmenistan", "туркменистан", "ukraine", "украина",
    "belarus", "беларусь",
]

# How many candidates go to the model per qualification call. One 600-item
# prompt is both a reliability risk (large payloads fail more often on
# congested free models) and a quality one (attention spread thin across
# hundreds of items). Batching also means a single failed batch costs one
# slice of the day's leads, not all of them.
QUALIFY_BATCH_SIZE = 60

# Starting query set — tune freely, this is not meant to be exhaustive.
# (query text, track hint for logging only; the AI decides the real track).
TRACK1_QUERIES = [
    ("новая гостиница строительство Узбекистан", "equipment_sales"),
    ("yangi mehmonxona qurilishi Ozbekiston", "equipment_sales"),
    ("new hotel construction Uzbekistan opening 2026 2027", "equipment_sales"),
    ("hospital construction Uzbekistan new facility", "equipment_sales"),
    ("инвестиционный проект гостиница Узбекистан", "equipment_sales"),
    ("free economic zone Uzbekistan hotel investment", "equipment_sales"),
    ("тендер строительство гостиницы больницы Узбекистан", "equipment_sales"),
    ("pre-opening manager hotel Uzbekistan hiring", "equipment_sales"),
    ("Hyatt Radisson Hilton Marriott new hotel Uzbekistan", "equipment_sales"),
]
TRACK2_QUERIES = [
    ("техническое обслуживание прачечное оборудование тендер Узбекистан", "service_maintenance"),
    ("ремонт прачечного оборудования Узбекистан", "service_maintenance"),
    ("laundry equipment service maintenance contract Uzbekistan", "service_maintenance"),
]
# Site-scoped searches of real tender portals — a stopgap for eTender UZEX /
# xt-xarid / data.egov.uz until their real API shapes are confirmed (see the
# module docstring). Added 2026-08-19 after comparing against n8n workflow
# output: the tender-portal leads were visibly the highest-signal category
# (real tender numbers, deadlines, budgets) versus social-media guesses.
TENDER_SITE_QUERIES = [
    ("site:bicotender.ru гостиница OR больница OR поликлиника тендер Узбекистан", "equipment_sales"),
    ("site:bicotender.ru прачечное оборудование техническое обслуживание", "service_maintenance"),
    ("site:etender.uzex.uz гостиница строительство", "equipment_sales"),
    ("site:etender.uzex.uz кир ювиш ускунаси", "equipment_sales"),
    ("site:xarid.uzex.uz прачечное оборудование гостиница больница", "equipment_sales"),
]
# Freshness matters differently by query type: a hiring post or investment
# announcement is only a "new signal" if it's actually new, but a tender
# portal page can rank in Google days after posting while the tender itself
# is still open — restricting that to "indexed in the last 24h" would drop
# real open tenders for no reason. So only the news-style queries are time-
# boxed to the past day; tender-portal searches run unrestricted.
FRESH_QUERIES = TRACK1_QUERIES + TRACK2_QUERIES
ALL_QUERIES = FRESH_QUERIES + TENDER_SITE_QUERIES


async def collect_raw_leads(run_id: uuid.UUID) -> list[RawLead]:
    """Fetch every source, tolerating individual failures, and dedupe by URL.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        Deduplicated raw leads across SerpAPI (Google/Bing/Yandex), Tavily,
        and World Bank. eTender UZEX, xt-xarid and data.egov.uz are not yet
        wired in — see docs/agent-specs/04-lead-agent.md.
    """
    results: list[RawLead] = []
    # Bounds concurrent requests per source so a dozen queries don't all fire
    # at once against one API — polite to the provider, and avoids tripping
    # any undocumented rate limit.
    concurrency = asyncio.Semaphore(4)

    async def _serpapi_one(client: SerpAPIClient, query: str, past_day_only: bool) -> list[RawLead]:
        async with concurrency:
            try:
                return await client.search_all_engines(query, num=10, past_day_only=past_day_only)
            except SerpAPIError as err:
                log.error("SerpAPI failed for '{}': {}", query, err)
                return []

    async def _serpapi_all() -> list[RawLead]:
        async with SerpAPIClient(agent=AGENT, run_id=run_id) as client:
            tasks = [_serpapi_one(client, q, True) for q, _t in FRESH_QUERIES]
            tasks += [_serpapi_one(client, q, False) for q, _t in TENDER_SITE_QUERIES]
            batches = await asyncio.gather(*tasks)
        return [lead for batch in batches for lead in batch]

    async def _tavily_one(client: TavilyClient, query: str, days: int) -> list[RawLead]:
        async with concurrency:
            try:
                return await client.search_news(query, days=days, max_results=10)
            except TavilyError as err:
                log.error("Tavily failed for '{}': {}", query, err)
                return []

    async def _tavily_all() -> list[RawLead]:
        async with TavilyClient(agent=AGENT, run_id=run_id) as client:
            # 1 day for news-style queries (matches SerpAPI's past_day_only);
            # tender-portal pages are left at Tavily's "week" bucket for the
            # same reason SerpAPI skips the freshness filter on them.
            tasks = [_tavily_one(client, q, 1) for q, _t in FRESH_QUERIES]
            tasks += [_tavily_one(client, q, 3) for q, _t in TENDER_SITE_QUERIES]
            batches = await asyncio.gather(*tasks)
        return [lead for batch in batches for lead in batch]

    async def _worldbank() -> list[RawLead]:
        try:
            async with WorldBankClient(agent=AGENT, run_id=run_id) as client:
                return await client.recent_projects(rows=20, country="Uzbekistan")
        except WorldBankError as err:
            log.error("World Bank failed: {}", err)
            return []

    async def _uzex() -> list[RawLead]:
        """Official government procurement — the highest-signal source."""
        try:
            async with UzExClient(agent=AGENT, run_id=run_id) as client:
                return await client.search_all_keywords()
        except Exception as err:  # noqa: BLE001 — never let one source kill the run
            log.error("UzEx failed: {}", err)
            return []

    # All sources run concurrently, not one after another.
    for batch in await asyncio.gather(
        _serpapi_all(), _tavily_all(), _worldbank(), _uzex(), return_exceptions=True
    ):
        if isinstance(batch, BaseException):
            log.error("A source raised unexpectedly: {}", batch)
        else:
            results.extend(batch)

    seen: dict[str, RawLead] = {}
    junked = 0
    for lead in results:
        if not lead.url:
            continue
        if _is_junk(lead.url):
            junked += 1
            continue
        if lead.dedupe_key not in seen:
            seen[lead.dedupe_key] = lead

    if junked:
        log.info("Filtered {} result(s) from non-lead domains before qualification", junked)

    log.info("Collected {} raw result(s), {} unique by URL", len(results), len(seen))
    # Government tenders first: they carry named buyers, budgets and firm
    # deadlines, so if a batch or the model budget runs short, the most
    # actionable material has already been seen.
    ordered = sorted(seen.values(), key=lambda l: 0 if l.source.startswith("uzex") else 1)
    return ordered


def _is_junk(url: str) -> bool:
    """True if a URL is from a domain that cannot contain a real lead.

    Args:
        url: The result URL.

    Returns:
        Whether to discard it before qualification. Matches the registered
        domain and its subdomains, so ``www.booking.com`` and
        ``uz.booking.com`` are both caught.
    """
    host = url.split("//")[-1].split("/")[0].lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in JUNK_DOMAINS)


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace, for loose substring comparison."""
    return " ".join(text.lower().split())


def passes_hard_filters(lead: dict, source: RawLead | None) -> tuple[bool, str]:
    """Code-level backstop the AI's own judgment cannot skip, no matter the prompt.

    Mirrors the URL-grounding check already applied to every lead, but aimed
    at content: a specific-category denylist (catering/banking/medical-gas/
    valuation/furniture — the exact categories that slipped through before),
    a requirement that the laundry connection be either an explicit keyword or
    a genuine facility-under-construction signal, a check that
    ``relevance_quote`` is both present and actually appears in the source text
    it claims to quote, and a Uzbekistan-geography check.

    Args:
        lead: A qualified lead dict that already passed URL-grounding and the
            track check.
        source: The raw candidate this lead's ``signal_source_url`` matched,
            for checking ``relevance_quote`` against real source text.

    Returns:
        ``(True, "")`` if the lead passes every backstop, else
        ``(False, reason)`` with a short human-readable reason for the drop.
    """
    combined = " ".join(
        str(lead.get(f, "") or "") for f in ("signal", "notes", "project_name", "industry")
    ).lower()
    quote = str(lead.get("relevance_quote", "") or "").strip()

    if any(kw in combined for kw in DISQUALIFYING_KEYWORDS):
        return False, "matched a disqualifying-category keyword (catering/banking/medical-gas/valuation/furniture)"

    if not quote:
        return False, "no relevance_quote provided"

    if source is not None:
        source_text = _normalize(f"{source.title} {source.snippet or ''}")
        if _normalize(quote) not in source_text:
            return False, "relevance_quote does not appear in the original source text"

    has_laundry_kw = any(kw in combined or kw in quote.lower() for kw in LAUNDRY_KEYWORDS)
    is_construction_stage = lead.get("project_stage") in {
        "under construction", "permitted", "pre-opening hiring", "tender open",
    }
    qualifying_facility = any(kw in combined for kw in QUALIFYING_FACILITY_KEYWORDS)
    if not has_laundry_kw and not (is_construction_stage and qualifying_facility):
        return False, "no explicit laundry/textile-care connection and not a qualifying facility under construction"

    location_text = _normalize(
        f"{lead.get('location', '')} {lead.get('signal', '')} {lead.get('notes', '')}"
    )
    has_foreign_flag = any(kw in location_text for kw in FOREIGN_RED_FLAGS)
    has_uz_marker = any(kw in location_text for kw in UZ_LOCATION_KEYWORDS)
    if has_foreign_flag and not has_uz_marker:
        return False, "location matches a foreign-country red flag with no Uzbekistan marker"

    return True, ""


async def qualify_leads(raw_leads: list[RawLead], run_id: uuid.UUID) -> list[dict]:
    """Run the two-track qualification prompt over the raw pool, in batches.

    Batching matters for both reliability and quality: one prompt carrying
    600+ candidates is a large payload that fails more often on congested
    models, and it spreads the model's attention thin. With batches, a single
    failure costs one slice of the day's leads rather than the entire run.

    Args:
        raw_leads: Deduplicated raw search results.
        run_id: UUID grouping this run's audit rows.

    Returns:
        Qualified lead dicts matching ``SHEET_COLUMNS`` minus date_added/
        dedupe_key (filled in later). Any lead whose ``signal_source_url``
        does not match a real raw URL is dropped and logged — a code-level
        backstop against the model citing a source it wasn't given.

        Returns whatever succeeded; raises only if EVERY batch failed.

    Raises:
        OpenRouterError: if no batch could be qualified at all.
    """
    if not raw_leads:
        return []

    by_url = {r.url: r for r in raw_leads}
    known_urls = set(by_url)
    batches = [
        raw_leads[i : i + QUALIFY_BATCH_SIZE]
        for i in range(0, len(raw_leads), QUALIFY_BATCH_SIZE)
    ]
    log.info("Qualifying {} candidate(s) in {} batch(es)", len(raw_leads), len(batches))

    all_leads: list[dict] = []
    failures = 0

    async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai:
        for index, batch in enumerate(batches, start=1):
            candidates = [
                {
                    "source": r.source,
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "published_at": str(r.published_at) if r.published_at else None,
                }
                for r in batch
            ]
            try:
                body = await ai.complete_json(SYSTEM_PROMPT, build_user_message(candidates))
            except OpenRouterError as err:
                failures += 1
                log.error("Batch {}/{} failed, continuing: {}", index, len(batches), err)
                continue

            leads = body.get("leads", [])
            if not isinstance(leads, list):
                log.warning("Batch {}: no leads array returned, got {}", index, type(leads))
                continue
            log.info("Batch {}/{}: {} lead(s) proposed", index, len(batches), len(leads))
            all_leads.extend(leads)

    if failures == len(batches):
        raise OpenRouterError(f"All {len(batches)} qualification batch(es) failed")

    grounded: list[dict] = []
    hard_filtered = 0
    for lead in all_leads:
        url = (lead.get("signal_source_url") or "").strip()
        source = by_url.get(url)
        if source is None:
            log.warning(
                "Dropping ungrounded lead (URL not in search results): {} -> {}",
                lead.get("company_name"),
                url,
            )
            continue
        if lead.get("track") not in ("equipment_sales", "service_maintenance"):
            log.warning(
                "Dropping lead with invalid track '{}': {}",
                lead.get("track"),
                lead.get("company_name"),
            )
            continue
        ok, reason = passes_hard_filters(lead, source)
        if not ok:
            hard_filtered += 1
            log.warning("Hard filter rejected '{}': {}", lead.get("company_name"), reason)
            continue
        grounded.append(lead)

    log.info(
        "Qualified {} lead(s) across {} batch(es) ({} failed), {} passed grounding+hard filters ({} hard-filtered)",
        len(all_leads),
        len(batches),
        failures,
        len(grounded),
        hard_filtered,
    )
    return grounded


async def verify_leads(leads: list[dict], raw_leads: list[RawLead], run_id: uuid.UUID) -> list[dict]:
    """Second qualification pass: a strict, adversarial re-check of the first pass.

    Explicitly requested after the live-sheet audit: the first pass alone was
    not reliable enough, so every lead that survives it (AI judgment + code
    hard filters) is shown a second time — alongside its original source text
    — to a reviewer prompt whose only job is to find a reason to reject it.
    Defaults to rejecting on any doubt or failure, since the point of this pass
    is fewer, trustworthy leads rather than more, uncertain ones.

    Args:
        leads: Leads that already passed ``qualify_leads``'s grounding, track,
            and hard-filter checks.
        raw_leads: The full deduplicated raw pool, used to look up each lead's
            original source text by URL.
        run_id: UUID grouping this run's audit rows.

    Returns:
        Only the leads this pass explicitly approved. A batch that fails
        outright (API error, unparseable response) drops its leads rather than
        passing them through unverified — and a lead with no verdict at all
        (the model skipped it) is dropped for the same reason.
    """
    if not leads:
        return []

    source_by_url = {r.url: r for r in raw_leads}
    batches = [leads[i : i + QUALIFY_BATCH_SIZE] for i in range(0, len(leads), QUALIFY_BATCH_SIZE)]
    log.info("Second pass: verifying {} lead(s) in {} batch(es)", len(leads), len(batches))

    approved: list[dict] = []
    rejected = 0
    async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai:
        for index, batch in enumerate(batches, start=1):
            source_snippets = {
                url: {"title": src.title, "snippet": src.snippet}
                for url, src in source_by_url.items()
            }
            try:
                body = await ai.complete_json(
                    VERIFY_SYSTEM_PROMPT, build_verify_message(batch, source_snippets)
                )
            except OpenRouterError as err:
                rejected += len(batch)
                log.error(
                    "Verify batch {}/{} failed — dropping its {} lead(s) rather than shipping them unverified: {}",
                    index, len(batches), len(batch), err,
                )
                continue

            verdicts = body.get("verified", [])
            if not isinstance(verdicts, list):
                rejected += len(batch)
                log.warning("Verify batch {}: no verified array returned, dropping batch", index)
                continue

            decisions = {(v.get("signal_source_url") or "").strip(): v for v in verdicts}
            for lead in batch:
                url = (lead.get("signal_source_url") or "").strip()
                verdict = decisions.get(url)
                if verdict is None:
                    rejected += 1
                    log.warning(
                        "Verify pass gave no verdict for '{}' — dropping (fail-closed)",
                        lead.get("company_name"),
                    )
                    continue
                if verdict.get("decision") != "approve":
                    rejected += 1
                    log.info(
                        "Verify pass rejected '{}': {}",
                        lead.get("company_name"),
                        verdict.get("reason"),
                    )
                    continue
                approved.append(lead)

    log.info("Second pass: {} approved, {} rejected (of {})", len(approved), rejected, len(leads))
    return approved


async def existing_keys(run_id: uuid.UUID) -> tuple[set[str], set[str]]:
    """Read the sheet and build dedupe sets from both conventions in use.

    Older rows in this sheet dedupe by URL alone; newer rows use a
    ``title|track`` key. Checking both avoids re-adding a lead already
    present under either convention.

    Args:
        run_id: UUID grouping this run's audit rows.

    Returns:
        Tuple of (existing signal_source_url values, existing dedupe_key values).

    Raises:
        SheetsError: if the sheet cannot be read.
    """
    async with SheetsClient(agent=AGENT, run_id=run_id) as sheets:
        rows = await sheets.get_values(SHEET_RANGE)

    urls: set[str] = set()
    keys: set[str] = set()
    for row in rows[1:]:  # skip header
        if len(row) > 7 and row[7]:
            urls.add(row[7].strip())
        if len(row) > 18 and row[18]:
            keys.add(row[18].strip())
    return urls, keys


def compute_dedupe_key(lead: dict) -> str:
    """Compute a lead's dedupe key, matching the sheet's existing convention.

    Args:
        lead: A qualified lead dict.

    Returns:
        ``"{company_name lowercased}|{track}"``, matching the format already
        present in the sheet's most recent rows.
    """
    name = (lead.get("company_name") or lead.get("project_name") or "").strip().lower()
    return f"{name}|{lead.get('track', '')}"


def filter_new(leads: list[dict], known_urls: set[str], known_keys: set[str]) -> list[dict]:
    """Drop leads already present in the sheet under either dedupe convention.

    Args:
        leads: Qualified, grounded leads.
        known_urls: Existing ``signal_source_url`` values.
        known_keys: Existing ``dedupe_key`` values.

    Returns:
        Only leads not already in the sheet.
    """
    fresh = []
    for lead in leads:
        url = (lead.get("signal_source_url") or "").strip()
        key = compute_dedupe_key(lead)
        if url in known_urls or key in known_keys:
            continue
        fresh.append(lead)
    return fresh


def to_sheet_row(lead: dict, today: str) -> list[str]:
    """Build one sheet row in the exact existing column order.

    Args:
        lead: A qualified, new lead dict.
        today: Today's date (Tashkent), ISO format, for ``date_added``.

    Returns:
        20 string values in ``SHEET_COLUMNS`` order.
    """
    values = {c: str(lead.get(c, "") or "") for c in SHEET_COLUMNS}
    values["date_added"] = today
    values["dedupe_key"] = compute_dedupe_key(lead)
    return [values[c] for c in SHEET_COLUMNS]


async def store_new_leads(new_leads: list[dict], run_id: uuid.UUID) -> int:
    """Append new leads to the sheet, gated by AGENT_WRITES_ENABLED.

    Args:
        new_leads: Leads not already present in the sheet.
        run_id: UUID grouping this run's audit rows.

    Returns:
        Number of rows actually appended (0 if the gate is closed or writes
        are dry-run — in both cases the intended rows are audited instead).

    Raises:
        SheetsError: if the append call itself fails (gate open only).
    """
    from integrations.common.timeutil import today_local

    if not new_leads:
        return 0

    today = today_local().isoformat()
    rows = [to_sheet_row(lead, today) for lead in new_leads]

    if not settings.agent_writes_enabled or settings.dry_run:
        log.info("[write gate closed] would append {} row(s) to {}", len(rows), SHEET_RANGE)
        await log_action(
            agent=AGENT,
            action="append_leads",
            target_system="google_sheets",
            status="dry_run",
            run_id=run_id,
            target_ref=SHEET_RANGE,
            mode="write",
            payload={"row_count": len(rows), "titles": [r[0] for r in rows][:20]},
        )
        return 0

    async with SheetsClient(agent=AGENT, run_id=run_id) as sheets:
        return await sheets.append_rows(SHEET_RANGE, rows)


async def notify_telegram(new_leads: list[dict], run_id: uuid.UUID) -> None:
    """Post a summary of today's new leads to the Director, via OPS Manager Bot.

    Args:
        new_leads: Leads that were (or would be) added this run.
        run_id: UUID grouping this run's audit rows.
    """
    if not new_leads:
        text = "🔍 <b>Lead Agent</b>\n\nNo new qualified leads today."
    else:
        by_track: dict[str, list[dict]] = {}
        for lead in new_leads:
            by_track.setdefault(lead.get("track", "?"), []).append(lead)

        lines = [f"🔍 <b>Lead Agent — {len(new_leads)} new lead(s)</b>\n"]
        for track, leads in by_track.items():
            label = "🏗 Equipment sales" if track == "equipment_sales" else "🔧 Service & maintenance"
            lines.append(f"<b>{escape(label)} ({len(leads)})</b>")
            for lead in leads[:10]:
                name = lead.get("company_name") or "Unnamed"
                stage = lead.get("project_stage", "")
                priority = lead.get("priority", "")
                lines.append(f"  • {escape(name)} — {escape(stage)} ({escape(priority)})")
            lines.append("")
        text = "\n".join(lines)

    await notify_directors(text, agent=AGENT, run_id=run_id)


async def run(dry_run: bool = False) -> int:
    """Run the full Lead Agent pipeline once.

    Args:
        dry_run: Print results instead of writing to the sheet or Telegram.

    Returns:
        Process exit code — 0 on success, 2 if config is incomplete.
    """
    if dry_run:
        settings.dry_run = True

    run_id = uuid.uuid4()
    log.info("Lead Agent run {} starting (dry_run={})", run_id, settings.dry_run)

    # settings.missing_placeholders() scans the ENTIRE config (SAP, CRM,
    # amoCRM, MS Graph, Verifix included) -- checking only the fields this
    # agent actually touches, so a machine set up for one agent isn't blocked
    # by another agent's unrelated placeholders. Only the active AI
    # provider's key is required -- the other provider's is allowed to stay
    # a placeholder.
    ai_key_field = "deepseek_api_key" if settings.ai_provider.strip().lower() == "deepseek" else "openrouter_api_key"
    required = {
        "serpapi_api_key", "tavily_api_key", ai_key_field,
        "google_service_account_json", "google_leads_sheet_id",
        # Telegram delivery now goes through OPS Manager Bot's own token
        # (see integrations/org_bot/notify.py), not a dedicated Lead Agent
        # bot -- no lead_agent_telegram_* config required anymore.
    }
    unfilled = required & set(settings.missing_placeholders())
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(sorted(unfilled)))
        return 2

    raw_leads = await collect_raw_leads(run_id)

    try:
        qualified = await qualify_leads(raw_leads, run_id)
    except OpenRouterError as exc:
        log.error("Qualification failed: {}", exc)
        return 1

    try:
        qualified = await verify_leads(qualified, raw_leads, run_id)
    except OpenRouterError as exc:
        log.error("Second-pass verification failed entirely — refusing to ship unverified leads: {}", exc)
        qualified = []

    try:
        known_urls, known_keys = await existing_keys(run_id)
    except SheetsError as exc:
        log.error("Could not read the existing sheet: {}", exc)
        return 1

    new_leads = filter_new(qualified, known_urls, known_keys)
    log.info("{} qualified, {} already known, {} new", len(qualified), len(qualified) - len(new_leads), len(new_leads))

    if settings.dry_run:
        print(f"\n=== {len(new_leads)} new lead(s) (dry run — nothing written) ===\n")
        for lead in new_leads:
            print(f"[{lead.get('track')}] {lead.get('company_name')} — {lead.get('project_stage')} "
                  f"({lead.get('priority')}, confidence {lead.get('confidence')})")
            print(f"    {lead.get('signal_source_url')}")
            print(f"    {lead.get('signal')}")
            print()
    else:
        await store_new_leads(new_leads, run_id)
        # The sheet write is the part that matters and has already happened
        # by this point -- notify_directors() logs and swallows delivery
        # failures internally rather than raising, so a broken Telegram
        # destination never makes a run that successfully wrote leads
        # report as a failure.
        await notify_telegram(new_leads, run_id)

    return 0


async def _main(dry_run: bool) -> int:
    """Run the agent and close the database pool."""
    try:
        return await run(dry_run)
    finally:
        await close_pool()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Daily B2B lead sourcing agent.")
    parser.add_argument("--dry-run", action="store_true", help="print instead of writing")
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(_main(args.dry_run)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
