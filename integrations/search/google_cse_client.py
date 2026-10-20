"""Google Programmable Search (Custom Search JSON API) — the competitor agent's search (2026-10-20).

``GET https://www.googleapis.com/customsearch/v1?key&cx&q`` → ``items[]`` with ``title`` /
``link`` / ``snippet``; at most 10 a request (``start`` pages on). The engine (``GOOGLE_SEARCH_CX``)
searches only the sites set in it — the Uzbek shops, OLX, Uzum, Telegram, Instagram — so it suits
the competitor agent, not the Lead Agent (the whole web stays on SerpAPI). Same ``search`` call as
``SerpAPIClient``; ``search_client`` picks Google when set, SerpAPI otherwise.
"""

from __future__ import annotations

import uuid

import httpx

from integrations.common.config import settings
from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.search.models import RawLead
from integrations.search.serpapi_client import SerpAPIClient, SerpAPIError

BASE_URL = "https://www.googleapis.com/customsearch/v1"


class GoogleSearchError(SerpAPIError):
    """A failed Google search (a SerpAPIError, so callers handle both alike)."""


def google_configured() -> bool:
    return bool(settings.google_search_api_key.get_secret_value() and settings.google_search_cx)


def search_configured() -> bool:
    return google_configured() or bool(settings.serpapi_api_key.get_secret_value())


def search_client(agent: str, run_id: uuid.UUID | str | None = None):
    """Google Programmable Search when its key and engine are set, SerpAPI otherwise."""
    return GoogleSearchClient(agent, run_id) if google_configured() else SerpAPIClient(agent=agent, run_id=run_id)


class GoogleSearchClient:
    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent, self.run_id = agent, run_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GoogleSearchClient":
        if not google_configured():
            raise GoogleSearchError("Google search is not configured — GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, engine: str = "google", num: int = 10, *, freshness: str | None = None,
                     country: str | None = None, language: str | None = None) -> list[RawLead]:
        """Up to ``num`` results (10 a request). ``country`` → ``gl``, ``language`` → ``hl``; ``freshness``
        ('d'/'w'/'m'/'y') → ``dateRestrict``."""
        assert self._client is not None
        out: list[RawLead] = []
        start = 1
        while len(out) < num and start <= 91:
            params = {"key": settings.google_search_api_key.get_secret_value(), "cx": settings.google_search_cx,
                      "q": query, "num": min(10, num - len(out)), "start": start}
            if country:
                params["gl"] = country
            if language:
                params["hl"] = language
            if freshness:
                params["dateRestrict"] = f"{freshness}1"
            async with audited(agent=self.agent, action="api_call", target_system="google_cse", run_id=self.run_id,
                               target_ref=BASE_URL, payload={"query": query}) as ctx:
                try:
                    response = await request_with_retry(self._client, "GET", BASE_URL, params=params)
                except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                    raise GoogleSearchError(f"Google search failed after retries: {type(exc).__name__}") from exc
                ctx["http_status"] = response.status_code
                if response.status_code != 200:
                    raise GoogleSearchError(f"Google search failed: HTTP {response.status_code}")
                items = response.json().get("items") or []
                ctx["payload"]["results"] = len(items)
            out += [RawLead(source="google_cse", title=i.get("title", ""), url=i.get("link", ""), snippet=i.get("snippet"))
                    for i in items]
            if len(items) < params["num"]:
                break
            start += len(items)
        return out
