"""Agent — Garmin market watch: new state tenders (daily) and competitors (Mondays).

For the Garmin manager (role ``garmin_manager``; the Director while nobody
holds it). The rules and texts are in ``integrations/garmin/market.py``;
this file fetches, remembers (``garmin_market``) and sends.

  * Tenders: UzEx e-tender lots matching ``GARMIN_KEYWORDS`` that the AI says
    buy a Garmin / Tanita / Tacx product — only lots not judged before; a day
    with none sends nothing.
  * Competitors: Mondays (or ``--competitors``) — who sells Garmin on the
    marketplaces this week, who is new, each model's price range.

Run:
    python agents/garmin-market/agent.py                 # tenders (and competitors on Monday)
    python agents/garmin-market/agent.py --competitors   # competitors now too
    python agents/garmin-market/agent.py --dry-run       # print, send and store nothing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from integrations.ai.openrouter_client import OpenRouterClient, OpenRouterError  # noqa: E402
from integrations.common.config import settings  # noqa: E402
from integrations.common.db import close_pool, fetch_all, log_action  # noqa: E402
from integrations.common.logging_setup import setup_logging  # noqa: E402
from integrations.common.timeutil import today_local  # noqa: E402
from integrations.garmin import market  # noqa: E402
from integrations.org_bot.notify import notify_role  # noqa: E402
from integrations.search.models import RawLead  # noqa: E402
from integrations.search.serpapi_client import SerpAPIClient  # noqa: E402
from integrations.tenders.uzex_client import GARMIN_KEYWORDS, UzExClient  # noqa: E402

AGENT = "garmin-market"
ROLE = "garmin_manager"
log = setup_logging(AGENT)


async def _seen(kind: str, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = await fetch_all("SELECT key FROM garmin_market WHERE kind = %s AND key = ANY(%s)", (kind, keys))
    return {r["key"] for r in rows}


async def _remember(kind: str, key: str, relevant: bool, data: dict) -> bool:
    """Store a lot / page; True when it is new (first time seen)."""
    rows = await fetch_all(
        """
        INSERT INTO garmin_market (kind, key, relevant, data) VALUES (%s, %s, %s, %s::jsonb)
        ON CONFLICT (kind, key) DO UPDATE SET last_seen = now(), data = EXCLUDED.data
        RETURNING (xmax = 0) AS inserted
        """,
        (kind, key, relevant, json.dumps(data, ensure_ascii=False, default=str)),
    )
    return bool(rows and rows[0]["inserted"])


async def tenders(run_id: uuid.UUID, dry_run: bool) -> list[market.Tender]:
    """New relevant UzEx lots (each lot judged once)."""
    async with UzExClient(agent=AGENT, run_id=run_id) as uzex:
        lots = await uzex.search_keywords(GARMIN_KEYWORDS)
    ids = [str(lot.extra.get("lot_id")) for lot in lots]
    known = set() if dry_run else await _seen("tender", ids)
    fresh = [lot for lot in lots if str(lot.extra.get("lot_id")) not in known]
    log.info("UzEx: {} candidate lot(s), {} not judged before", len(lots), len(fresh))
    if not fresh:
        return []
    async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai:
        result = await ai.complete_json(market.TENDER_SYSTEM_PROMPT, market.tender_message(fresh))
    picked = market.pick_tenders(result, fresh)
    if not dry_run:
        relevant = {t.lot_id for t in picked}
        for lot in fresh:
            lot_id = str(lot.extra.get("lot_id"))
            await _remember("tender", lot_id, lot_id in relevant,
                            {"title": lot.title, "url": lot.url, **{k: v for k, v in lot.extra.items() if k != "keyword"}})
    return picked


async def competitors(run_id: uuid.UUID, dry_run: bool) -> tuple[list[market.Sighting], list[str]]:
    """This week's pages selling Garmin, and the sellers never seen before."""
    results: list[RawLead] = []
    async with SerpAPIClient(agent=AGENT, run_id=run_id) as serp:
        for query in market.COMPETITOR_QUERIES:
            try:
                results.extend(await serp.search(query, "google", 20, freshness=None))
            except Exception as exc:  # noqa: BLE001 — one search must never fail the others
                log.error("SerpAPI failed for one competitor query: {}", type(exc).__name__)
    unique = list({r.url: r for r in results if r.url}.values())
    if not unique:
        return [], []
    sightings: list[market.Sighting] = []
    async with OpenRouterClient(agent=AGENT, run_id=run_id) as ai:
        for start in range(0, len(unique), 40):  # batches keep each prompt small
            batch = unique[start:start + 40]
            try:
                result = await ai.complete_json(market.COMPETITOR_SYSTEM_PROMPT, market.competitor_message(batch))
            except OpenRouterError as exc:
                log.error("Competitor reading failed for one batch: {}", type(exc).__name__)
                continue
            sightings.extend(market.read_sightings(result, batch))
    known_sellers: set[str] = set()
    if not dry_run:
        rows = await fetch_all("SELECT DISTINCT data->>'seller' AS seller FROM garmin_market "
                               "WHERE kind = 'competitor' AND first_seen < now() - interval '1 day'")
        known_sellers = {(r["seller"] or "").lower() for r in rows}
        for s in sightings:
            await _remember("competitor", s.url, True, s.__dict__)
    new_sellers = sorted({s.seller for s in sightings if s.seller.lower() not in known_sellers}) if known_sellers else []
    return sightings, new_sellers


async def run(dry_run: bool = False, with_competitors: bool | None = None) -> int:
    """Tenders every day; competitors on Mondays or when asked."""
    if dry_run:
        settings.dry_run = True
    if not settings.garmin_market_enabled and not dry_run:
        log.info("Garmin market watch is off (GARMIN_MARKET_ENABLED=false)")
        return 0
    run_id = uuid.uuid4()
    if with_competitors is None:
        with_competitors = today_local().weekday() == 0
    failed = False

    try:
        found = await tenders(run_id, dry_run)
    except Exception as exc:  # noqa: BLE001 — competitors still run
        log.error("Garmin tenders failed: {}", type(exc).__name__)
        found, failed = [], True
    if found:
        text = market.tenders_text(found)
        print(text) if dry_run else await notify_role(ROLE, text, agent=AGENT, run_id=run_id)

    sightings: list[market.Sighting] = []
    if with_competitors:
        if not settings.serpapi_api_key.get_secret_value():
            log.warning("SERPAPI_API_KEY not set — competitor watch skipped")
        else:
            try:
                sightings, new_sellers = await competitors(run_id, dry_run)
                text = market.competitors_text(sightings, new_sellers)
                print(text) if dry_run else await notify_role(ROLE, text, agent=AGENT, run_id=run_id)
            except Exception as exc:  # noqa: BLE001
                log.error("Garmin competitor watch failed: {}", type(exc).__name__)
                failed = True

    # Counts only (IT sees no figures).
    await log_action(agent=AGENT, action="garmin_market_run", target_system="uzex", status="failure" if failed else "success",
                     run_id=run_id, mode="read", payload={"tenders": len(found), "sightings": len(sightings),
                                                          "competitors": bool(with_competitors)})
    return 1 if failed else 0


async def _main(args: argparse.Namespace) -> int:
    try:
        return await run(args.dry_run, True if args.competitors else None)
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="Garmin market watch: tenders and competitors.")
    parser.add_argument("--dry-run", action="store_true", help="print, send and store nothing")
    parser.add_argument("--competitors", action="store_true", help="run the competitor watch today too")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
