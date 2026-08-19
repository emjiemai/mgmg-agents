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
from integrations.telegram.bot import TelegramBot, escape
from integrations.tenders.worldbank_client import WorldBankClient, WorldBankError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompt import SYSTEM_PROMPT, build_user_message  # noqa: E402

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
ALL_QUERIES = TRACK1_QUERIES + TRACK2_QUERIES


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

    async def _serpapi_one(client: SerpAPIClient, query: str) -> list[RawLead]:
        async with concurrency:
            try:
                return await client.search_all_engines(query, num=10)
            except SerpAPIError as err:
                log.error("SerpAPI failed for '{}': {}", query, err)
                return []

    async def _serpapi_all() -> list[RawLead]:
        async with SerpAPIClient(agent=AGENT, run_id=run_id) as client:
            batches = await asyncio.gather(*(_serpapi_one(client, q) for q, _t in ALL_QUERIES))
        return [lead for batch in batches for lead in batch]

    async def _tavily_one(client: TavilyClient, query: str) -> list[RawLead]:
        async with concurrency:
            try:
                return await client.search_news(query, days=3, max_results=10)
            except TavilyError as err:
                log.error("Tavily failed for '{}': {}", query, err)
                return []

    async def _tavily_all() -> list[RawLead]:
        async with TavilyClient(agent=AGENT, run_id=run_id) as client:
            batches = await asyncio.gather(*(_tavily_one(client, q) for q, _t in ALL_QUERIES))
        return [lead for batch in batches for lead in batch]

    async def _worldbank() -> list[RawLead]:
        try:
            async with WorldBankClient(agent=AGENT, run_id=run_id) as client:
                return await client.recent_projects(rows=20, country="Uzbekistan")
        except WorldBankError as err:
            log.error("World Bank failed: {}", err)
            return []

    # The three sources themselves also run concurrently, not one after another.
    for batch in await asyncio.gather(_serpapi_all(), _tavily_all(), _worldbank(), return_exceptions=True):
        if isinstance(batch, BaseException):
            log.error("A source raised unexpectedly: {}", batch)
        else:
            results.extend(batch)

    seen: dict[str, RawLead] = {}
    for lead in results:
        if lead.url and lead.dedupe_key not in seen:
            seen[lead.dedupe_key] = lead

    log.info("Collected {} raw result(s), {} unique by URL", len(results), len(seen))
    return list(seen.values())


async def qualify_leads(raw_leads: list[RawLead], run_id: uuid.UUID) -> list[dict]:
    """Run the two-track qualification prompt over the raw pool.

    Args:
        raw_leads: Deduplicated raw search results.
        run_id: UUID grouping this run's audit rows.

    Returns:
        Qualified lead dicts matching ``SHEET_COLUMNS`` minus date_added/
        dedupe_key (filled in later). Any lead whose ``signal_source_url``
        does not match a real raw URL is dropped and logged — a code-level
        backstop against the model citing a source it wasn't given.

    Raises:
        OpenRouterError: if the qualification call itself fails.
    """
    if not raw_leads:
        return []

    candidates = [
        {
            "source": r.source,
            "title": r.title,
            "url": r.url,
            "snippet": r.snippet,
            "published_at": str(r.published_at) if r.published_at else None,
        }
        for r in raw_leads
    ]
    known_urls = {r.url for r in raw_leads}

    async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai:
        body = await ai.complete_json(SYSTEM_PROMPT, build_user_message(candidates))

    leads = body.get("leads", [])
    if not isinstance(leads, list):
        log.warning("OpenRouter did not return a leads array, got: {}", type(leads))
        return []

    grounded: list[dict] = []
    for lead in leads:
        url = (lead.get("signal_source_url") or "").strip()
        if url not in known_urls:
            log.warning("Dropping ungrounded lead (URL not in search results): {} -> {}", lead.get("company_name"), url)
            continue
        if lead.get("track") not in ("equipment_sales", "service_maintenance"):
            log.warning("Dropping lead with invalid track '{}': {}", lead.get("track"), lead.get("company_name"))
            continue
        grounded.append(lead)

    log.info("Qualified {} lead(s), {} passed the grounding check", len(leads), len(grounded))
    return grounded


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
    """Post a summary of today's new leads to the leads Telegram group.

    Args:
        new_leads: Leads that were (or would be) added this run.
        run_id: UUID grouping this run's audit rows.
    """
    if not settings.telegram_leads_chat_id:
        log.warning("TELEGRAM_LEADS_CHAT_ID is not set — skipping the Telegram summary")
        return

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

    async with TelegramBot(agent=AGENT, run_id=run_id) as bot:
        await bot.send_message(text, chat_id=settings.telegram_leads_chat_id)


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

    unfilled = settings.missing_placeholders()
    if unfilled and not settings.dry_run:
        log.error("Refusing to run — unfilled placeholders in .env: {}", ", ".join(unfilled))
        return 2

    raw_leads = await collect_raw_leads(run_id)

    try:
        qualified = await qualify_leads(raw_leads, run_id)
    except OpenRouterError as exc:
        log.error("Qualification failed: {}", exc)
        return 1

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
