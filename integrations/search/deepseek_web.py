"""DeepSeek web search through OpenRouter — every 3 days, into our own lead index (2026-10-30, the owner).

DeepSeek has no search engine of its own; OpenRouter's ``web`` plugin searches with **Exa** for models
without native search ($0.007 a request, up to 10 results — ≈ $0.30 for the 42 lead queries, ≈ $3 a month at
one run every ``EVERY_DAYS``). DeepSeek reads the results; we keep the **cited pages** (``url_citation``
annotations: link, title, text) in ``crawl_items``, where the Lead Agent's full-text search and AI qualifier
treat them like every other source. Same OpenRouter key; no monthly quota to run out, only money — the first
payment / limit error (402 / 429) stops the run. Exa searches the world's web (strongest in English): it adds
international news our Uzbek feeds miss.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any

import httpx

from integrations.ai.openrouter_client import BASE_URL
from integrations.common.config import settings
from integrations.common.db import log_action
from integrations.common.logging_setup import setup_logging
from integrations.search import crawler

AGENT = "lead-web-search"
log = setup_logging(AGENT)

MODEL = "deepseek/deepseek-v4.1-flash"
EVERY_DAYS = 3
MAX_RESULTS = 10
CONCURRENCY = 3
TIMEOUT = 90.0
STOP_STATUSES = (402, 429)  # out of credit / over the key's limit — no point sending the rest
PROMPT = ("Find recent news (last 12 months) about this in Uzbekistan and list each finding in one line with its "
          "source. Facts only. Query: {q}")


def _client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"Authorization": f"Bearer {key}"})


def due(day: date) -> bool:
    """Every third day (by the calendar, so a restart never doubles it)."""
    return day.toordinal() % EVERY_DAYS == 0


def payload(query: str) -> dict[str, Any]:
    return {"model": MODEL, "max_tokens": 600,
            "messages": [{"role": "user", "content": PROMPT.format(q=query)}],
            "plugins": [{"id": "web", "engine": "exa", "max_results": MAX_RESULTS}]}


def citations(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The pages the answer cites (``url_citation`` annotations) → index items."""
    out = []
    for choice in body.get("choices") or []:
        for note in (choice.get("message") or {}).get("annotations") or []:
            cite = note.get("url_citation") or {}
            url = (cite.get("url") or "").strip()
            if note.get("type") == "url_citation" and url.startswith(("http://", "https://")):
                title = crawler._clean(cite.get("title")) or url  # noqa: SLF001 — the index's own cleaner
                out.append({"url": url, "title": title[:500], "body": crawler._clean(cite.get("content"))[:crawler.BODY_CHARS],  # noqa: SLF001
                            "source": "deepseek_web", "published_at": None})
    return out


async def search(queries: list[str], run_id: uuid.UUID | None = None) -> dict[str, int]:
    """Run every query once; keep the cited pages in the index. Returns counts."""
    key = settings.openrouter_api_key.get_secret_value().strip()
    if not key:
        return {"queries": 0, "found": 0, "new": 0, "failed": 0}
    gate, stop = asyncio.Semaphore(CONCURRENCY), asyncio.Event()
    failed: list[str] = []

    async def one(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
        async with gate:
            if stop.is_set():
                return []
            try:
                response = await client.post(BASE_URL, json=payload(query))
            except httpx.HTTPError as exc:
                failed.append(type(exc).__name__)
                return []
            if response.status_code in STOP_STATUSES:
                stop.set()
                failed.append(f"HTTP {response.status_code}")
                return []
            if response.status_code != 200:
                failed.append(f"HTTP {response.status_code}")
                return []
            return citations(response.json())

    async with _client(key) as client:
        batches = await asyncio.gather(*[one(client, q) for q in queries])
    items = {i["url"]: i for batch in batches for i in batch}
    new = await crawler.store(list(items.values()))
    counts = {"queries": len(queries), "found": len(items), "new": new, "failed": len(failed)}
    await log_action(agent=AGENT, action="web_search", target_system="openrouter",
                     status="success" if not failed else "partial", run_id=run_id, mode="read",
                     payload={**counts, "model": MODEL, "errors": sorted(set(failed))[:5], "stopped": stop.is_set()})
    log.info("DeepSeek web search: {} queries, {} pages, {} new, {} failed", len(queries), len(items), new, len(failed))
    return counts
