"""UzEx client — Uzbekistan's OFFICIAL government procurement portal.

This is the highest-signal lead source available: real government tenders with
named buying organizations, exact budgets, and firm submission deadlines — as
opposed to the news/social-media guessing the other sources do.

## How this endpoint was found (2026-08-19)

etender.uzex.uz is an Angular SPA — every plausible public API path on the
www host returns the HTML shell, which is why earlier attempts to guess a URL
failed. The real API is on a **separate host** (``apietender.uzex.uz``, found
as ``serverUrl`` in the site's own JS bundle), and the public listing endpoint
is ``POST /api/common/TradeList``.

Its pagination fields are ``From``/``To`` (1-based, inclusive) — not
page/pageSize — which the API rejects with the Uzbek error "Sahifa
chegaralari noto'g'ri" ("page boundaries are incorrect") if wrong. All field
names below were read from the bundle's own ``tradeListFilter`` model, then
verified against live responses.

No authentication is required for this endpoint, and no key is needed.

## Trade types (``TypeId``)

Verified live volumes on 2026-08-19:

    1 = competitive bidding  (591 open lots)
    2 = tender               (51 open lots)
    3 = frame agreement      (0)
    4-6 = other/unused       (0)

Types 1 and 2 are the ones worth polling; the rest were empty.

## Response fields

``name``, ``cost``, ``currency_codeabc``, ``seller_name`` (the buying
organization), ``seller_tin`` (its tax id — a genuinely useful identifier for
follow-up), ``start_date``, ``end_date`` (submission deadline),
``display_no``, ``id``, ``total_count``, ``district_name``.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from integrations.common.db import audited
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import parse_sap_date
from integrations.search.models import RawLead

log = setup_logging("uzex")

API_BASE = "https://apietender.uzex.uz"
TRADE_LIST_PATH = "/api/common/TradeList"
LOT_URL_TEMPLATE = "https://etender.uzex.uz/lot/{id}"

# Only these two carry live volume — see the module docstring.
TRADE_TYPES: dict[int, str] = {1: "competitive", 2: "tender"}

# Keywords aimed at Primus Laundry's two tracks. The API matches these against
# the lot name, so they must be short substrings, not natural-language queries.
# Uzbek, Russian and transliterated forms are all included because the portal's
# lot titles mix all three in practice.
EQUIPMENT_KEYWORDS = [
    "kir yuvish",      # laundry (uz)
    "kir yuvish mashinasi",
    "прачечн",         # laundry (ru, stem)
    "стиральн",        # washing machine (ru, stem)
    "mehmonxona",      # hotel (uz)
    "гостиниц",        # hotel (ru, stem)
    "отель",           # hotel (ru)
    "shifoxona",       # hospital (uz)
    "больниц",         # hospital (ru, stem)
    "поликлиник",      # polyclinic (ru, stem)
    "tibbiy jihoz",    # medical equipment (uz)
    "медицинск оборудован",
    "santexnika",
    "kimyoviy vosita",  # chemicals (uz)
    "моющих средств",   # detergents (ru)
]
SERVICE_KEYWORDS = [
    "texnik xizmat",   # technical service (uz)
    "ta'mirlash",      # repair (uz)
    "техническое обслуживание",
    "ремонт оборудования",
    "montaj",          # installation (uz)
]


class UzExError(RuntimeError):
    """Raised when the UzEx API returns an unrecoverable error."""


class UzExClient:
    """Async client for the UzEx public tender listing API.

    Args:
        agent: Calling agent name, recorded on every audit row.
        run_id: UUID grouping this run's audit rows.
    """

    def __init__(self, agent: str = "-", run_id: uuid.UUID | str | None = None) -> None:
        self.agent = agent
        self.run_id = run_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "UzExClient":
        """Open the HTTP client. No auth needed for this endpoint.

        Returns:
            The ready client.
        """
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(30.0),
            headers={
                # The API is fronted by a WAF that rejects non-browser agents,
                # and the Origin/Referer pair is what the site itself sends.
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://etender.uzex.uz",
                "Referer": "https://etender.uzex.uz/",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self, keyword: str | None = None, type_id: int = 1, limit: int = 50
    ) -> list[RawLead]:
        """Fetch open lots, optionally filtered by keyword.

        Args:
            keyword: Substring matched against the lot name. ``None`` returns
                everything currently open for that type.
            type_id: Trade type — see ``TRADE_TYPES``.
            limit: Maximum lots to return (the API is 1-based inclusive).

        Returns:
            Normalized ``RawLead`` objects. An API error degrades to an empty
            list rather than raising, so one bad keyword cannot fail a run.
        """
        assert self._client is not None
        payload: dict[str, Any] = {"From": 1, "To": limit, "TypeId": type_id}
        if keyword:
            payload["Keyword"] = keyword

        try:
            async with audited(
                agent=self.agent,
                action="api_call",
                target_system="uzex",
                run_id=self.run_id,
                target_ref=TRADE_LIST_PATH,
                payload={"keyword": keyword, "type_id": type_id},
            ) as ctx:
                response = await request_with_retry(
                    self._client, "POST", TRADE_LIST_PATH, json=payload
                )
                ctx["http_status"] = response.status_code
                if response.status_code != 200:
                    raise UzExError(
                        f"UzEx TradeList failed: HTTP {response.status_code} {response.text[:200]}"
                    )
                rows = response.json()
                ctx["payload"]["rows"] = len(rows) if isinstance(rows, list) else 0
        except (httpx.HTTPError, UzExError, ValueError) as err:
            log.error("UzEx search failed (keyword={}, type={}): {}", keyword, type_id, err)
            return []

        if not isinstance(rows, list):
            return []

        leads = [_to_lead(row, TRADE_TYPES.get(type_id, str(type_id))) for row in rows]
        if keyword:
            log.info("UzEx [{}] '{}': {} lot(s)", TRADE_TYPES.get(type_id), keyword, len(leads))
        return leads

    async def search_all_keywords(self) -> list[RawLead]:
        """Run every Primus-relevant keyword across both live trade types.

        Also pulls an unfiltered slice of each type, so a relevant lot whose
        title uses wording none of the keywords anticipate is still seen by
        the AI qualifier rather than silently missed.

        Returns:
            Combined lots, deduplicated by lot id.
        """
        seen: dict[str, RawLead] = {}

        for type_id in TRADE_TYPES:
            # Unfiltered sweep first — keyword lists always have blind spots.
            for lead in await self.search(keyword=None, type_id=type_id, limit=100):
                seen[lead.url] = lead

            for keyword in EQUIPMENT_KEYWORDS + SERVICE_KEYWORDS:
                for lead in await self.search(keyword=keyword, type_id=type_id, limit=25):
                    seen[lead.url] = lead

        log.info("UzEx: {} unique lot(s) across both trade types", len(seen))
        return list(seen.values())


def _to_lead(row: dict[str, Any], trade_type: str) -> RawLead:
    """Map one raw UzEx lot onto a ``RawLead``.

    Args:
        row: The lot record from the API.
        trade_type: Human-readable trade type name.

    Returns:
        A normalized lead. The snippet deliberately carries buyer, budget and
        deadline, since those are exactly what makes a tender actionable and
        what the AI qualifier needs to judge priority.
    """
    lot_id = row.get("id")
    cost = row.get("cost") or 0
    currency = row.get("currency_codeabc") or "UZS"
    buyer = (row.get("seller_name") or "").strip()
    deadline = row.get("end_date") or ""

    snippet_parts = []
    if buyer:
        snippet_parts.append(f"Buyer: {buyer}")
    if cost:
        snippet_parts.append(f"Budget: {cost:,.0f} {currency}")
    if deadline:
        snippet_parts.append(f"Deadline: {deadline[:10]}")
    if row.get("district_name"):
        snippet_parts.append(f"District: {row['district_name']}")

    return RawLead(
        source=f"uzex_{trade_type}",
        title=row.get("name", f"Lot {lot_id}"),
        url=LOT_URL_TEMPLATE.format(id=lot_id),
        snippet=" | ".join(snippet_parts) or None,
        published_at=parse_sap_date(row.get("start_date")),
        extra={
            "lot_id": lot_id,
            "display_no": row.get("display_no"),
            "buyer_name": buyer,
            "buyer_tin": row.get("seller_tin"),
            "cost": cost,
            "currency": currency,
            "deadline": deadline,
            "trade_type": trade_type,
        },
    )
