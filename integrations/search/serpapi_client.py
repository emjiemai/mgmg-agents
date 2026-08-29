"""SerpAPI client — Google, Bing and Yandex organic search results.

Verified against SerpAPI's own docs (serpapi.com/search-api,
serpapi.com/bing-search-api, serpapi.com/yandex-search-api) on 2026-08-19.
All three engines return an ``organic_results`` array with ``title``/``link``/
``snippet`` — Bing additionally has ``tracking_link`` and ``displayed_link``,
which this client ignores in favor of the plain ``link``.
"""

from __future__ import annotations

import uuid
from typing import Literal

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.search.models import RawLead

log = setup_logging("serpapi")

Engine = Literal["google", "bing", "yandex"]

BASE_URL = "https://serpapi.com/search"


class SerpAPIError(RuntimeError):
    """Raised when SerpAPI returns an error the client cannot recover from."""


class SerpAPIClient:
    """Async client for SerpAPI's organic search results.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SerpAPIClient":
        """Open the HTTP client.

        Returns:
            The ready client.

        Raises:
            SerpAPIError: if the API key is unset.
        """
        if not settings.serpapi_api_key.get_secret_value():
            raise SerpAPIError("SerpAPI is not configured — fill SERPAPI_API_KEY in .env")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self, query: str, engine: Engine, num: int = 10, *, freshness: str | None = "d"
    ) -> list[RawLead]:
        """Run one search and return normalized leads.

        Args:
            query: Search query text.
            engine: 'google' | 'bing' | 'yandex'.
            num: Requested result count (SerpAPI may return fewer).
            freshness: Google's standard freshness window via ``tbs=qdr:X`` —
                'd' (past day), 'w' (past week), 'm' (past month), 'y' (past
                year), or ``None`` for unrestricted. Confirmed live
                2026-08-19 that ``qdr:d`` is accepted (no error) on all three
                engines through SerpAPI; the other windows use the same
                Google parameter family. Without this, results are not
                time-bounded at all and old articles rank alongside new ones
                — a real risk for anything but the tender-portal searches,
                where the tender's own deadline matters more than crawl date.

        Returns:
            Normalized ``RawLead`` objects, ranked order preserved.

        Raises:
            SerpAPIError: on a non-200 response.
        """
        assert self._client is not None
        # Confirmed live 2026-08-19: Yandex takes `text`, not `q` — Google and
        # Bing both use `q`. SerpAPI's own error message names the field, so
        # this isn't guessed.
        query_param = "text" if engine == "yandex" else "q"
        params = {
            "engine": engine,
            query_param: query,
            "api_key": settings.serpapi_api_key.get_secret_value(),
            "num": num,
        }
        if freshness:
            params["tbs"] = f"qdr:{freshness}"

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system=f"serpapi_{engine}",
            run_id=self.run_id,
            target_ref=BASE_URL,
            payload={"query": query, "engine": engine},
        ) as ctx:
            response = await request_with_retry(self._client, "GET", BASE_URL, params=params)
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise SerpAPIError(f"SerpAPI {engine} search failed: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()
            if "error" in body:
                raise SerpAPIError(f"SerpAPI {engine} search error: {body['error']}")
            results = body.get("organic_results", [])
            ctx["payload"]["results"] = len(results)

        leads = [
            RawLead(
                source=f"serpapi_{engine}",
                title=r.get("title", ""),
                url=r.get("link", ""),
                snippet=r.get("snippet"),
                extra={"position": r.get("position")},
            )
            for r in results
            if r.get("link")
        ]
        log.info("SerpAPI {}: '{}' -> {} result(s)", engine, query, len(leads))
        return leads

    async def search_all_engines(
        self, query: str, num: int = 10, *, freshness: str | None = "d"
    ) -> list[RawLead]:
        """Run the same query across Google, Bing and Yandex.

        A failure on one engine is logged and skipped rather than failing the
        whole call — one down search engine shouldn't block the other two.

        Args:
            query: Search query text.
            num: Requested result count per engine.
            freshness: See ``search`` — pass ``None`` for queries where the
                page's index date doesn't matter (e.g. a tender-portal search,
                where the tender's own deadline is what matters, not when
                Google crawled the listing).

        Returns:
            Combined leads from every engine that succeeded.
        """
        leads: list[RawLead] = []
        for engine in ("google", "bing", "yandex"):
            try:
                leads.extend(await self.search(query, engine, num, freshness=freshness))
            except SerpAPIError as err:
                log.error("SerpAPI {} failed, continuing with other engines: {}", engine, err)
        return leads
