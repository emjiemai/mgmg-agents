"""Tavily client — AI-oriented news/web search, used for tender/lead news.

Verified against docs.tavily.com/documentation/api-reference/endpoint/search
on 2026-08-19: POST with a Bearer token, JSON body, results under
``results[]`` with ``title``/``url``/``content``/``score``.
"""

from __future__ import annotations

import uuid

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.search.models import RawLead

log = setup_logging("tavily")

BASE_URL = "https://api.tavily.com/search"


class TavilyError(RuntimeError):
    """Raised when Tavily returns an error the client cannot recover from."""


class TavilyClient:
    """Async client for the Tavily search API.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TavilyClient":
        """Open the HTTP client with the bearer token attached.

        Returns:
            The ready client.

        Raises:
            TavilyError: if the API key is unset.
        """
        key = settings.tavily_api_key.get_secret_value()
        if not key:
            raise TavilyError("Tavily is not configured — fill TAVILY_API_KEY in .env")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search_news(self, query: str, days: int = 1, max_results: int = 10) -> list[RawLead]:
        """Search recent news for a query.

        Args:
            query: Search query text.
            days: How many days back the ``time_range`` should cover — mapped
                to Tavily's day/week/month/year buckets, rounding up.
            max_results: Maximum results (Tavily allows 0-20).

        Returns:
            Normalized ``RawLead`` objects, Tavily's relevance order preserved.

        Raises:
            TavilyError: on a non-200 response.
        """
        assert self._client is not None
        payload = {
            "query": query,
            "topic": "news",
            "time_range": _time_range_for_days(days),
            "max_results": max_results,
            "search_depth": "basic",
        }

        async with audited(
            agent=self.agent,
            action="api_call",
            target_system="tavily",
            run_id=self.run_id,
            target_ref=BASE_URL,
            payload={"query": query, "days": days},
        ) as ctx:
            response = await request_with_retry(self._client, "POST", BASE_URL, json=payload)
            ctx["http_status"] = response.status_code
            if response.status_code != 200:
                raise TavilyError(f"Tavily search failed: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()
            results = body.get("results", [])
            ctx["payload"]["results"] = len(results)

        leads = [
            RawLead(
                source="tavily_news",
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content"),
                extra={"score": r.get("score")},
            )
            for r in results
            if r.get("url")
        ]
        log.info("Tavily news: '{}' -> {} result(s)", query, len(leads))
        return leads


def _time_range_for_days(days: int) -> str:
    """Map a day count to Tavily's time_range buckets, rounding up.

    Args:
        days: Desired lookback window in days.

    Returns:
        'day' | 'week' | 'month' | 'year'.
    """
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 30:
        return "month"
    return "year"
