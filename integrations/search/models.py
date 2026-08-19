"""Shared result shape for every search/tender source the Lead Agent reads.

Every source client (SerpAPI, Tavily, World Bank, and the still-pending
Uzbek tender portals) normalizes its own response into this one model, so
the Lead Agent's dedupe/qualify/store pipeline never needs to know which
source a lead came from.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class RawLead(BaseModel):
    """One candidate lead/opportunity from any source, before AI qualification."""

    source: str  # 'serpapi_google' | 'tavily_news' | 'worldbank' | ...
    title: str
    url: str
    snippet: str | None = None
    published_at: date | datetime | None = None
    extra: dict = {}  # source-specific fields worth keeping for the AI qualifier

    @property
    def dedupe_key(self) -> str:
        """Stable key for cross-source deduplication.

        Uses the URL, normalized: no scheme, no trailing slash, no query
        string — the same article/tender linked with different tracking
        params or http/https should still collapse to one lead.
        """
        url = self.url.strip().lower()
        for prefix in ("https://", "http://"):
            if url.startswith(prefix):
                url = url[len(prefix) :]
                break
        url = url.rstrip("/")
        return url.split("?")[0]
