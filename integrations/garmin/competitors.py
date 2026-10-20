"""Garmin competitors in Uzbekistan (2026-10-19, the owner) — who else sells Garmin, and for how much.

"We need an agent that searches for competitors inside Uzbekistan for Garmin
products, sent to the Garmin manager." Public sources only, read-only:

  * **OLX.uz** — every listing with «Garmin» / «гармин» in it, read from the
    public listing API the site's own pages use (``/api/v1/offers/``; not
    disallowed by olx.uz/robots.txt): title, price in so'm (OLX converts
    у.е. itself), new or used, a shop or a private person, date, link.
  * **Web search** (SerpAPI Google, Uzbekistan, Russian) — shops and pages
    selling Garmin in Uzbekistan (.uz sites, Telegram, Instagram): the site,
    the title, the link, and the price only when the result states one.

Our price is **garmin.com.uz** (the company's public site, ``/api/public/products``),
read live. Compared by model — family + model number (+ «pro», + the MARQ
edition), and the size when both say it; a variant (colour, glass, band) can
still differ, and the page says so. Copies («1:1», «копия»), straps and parts,
and repairs are left out; a new item at under 30 % of our price is listed
apart as suspicious (a copy or a wrong price), never as "cheaper".

Privacy: a private seller is «хусусий шахс» — no name, no phone, nothing about
them is stored. Shops (OLX business accounts, sites, Telegram channels) are listed
with what they publish for customers — name, phones, Instagram, Telegram, site,
address (``competitor_shops.py``, 2026-10-20). Phones never go to the AI.

Stored in ``garmin_competitor_offers`` (first and last seen, price, the price
before), so the page marks what is new since the last check and what got
cheaper. Weekly for the Garmin manager (agents/garmin-competitors) and
``/raqobat`` any time.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from integrations.common.config import settings
from integrations.common.db import audited, execute, fetch_all
from integrations.common.http import request_with_retry
from integrations.common.logging_setup import setup_logging
from integrations.common.timeutil import now_local, to_local, today_local

AGENT = "garmin-competitors"
log = setup_logging(AGENT)

COMMANDS = ("/raqobat", "/рақобат")
OLX_URL = "https://www.olx.uz/api/v1/offers/"
OLX_QUERIES = ("garmin", "гармин")
OLX_PAGE = 50
OLX_MAX = 600  # listings per query — 155 on 19.10.2026
OUR_PRICES_URL = "https://api.garmin.com.uz/api/public/products"
OUR_SITE = "garmin.com.uz"
USER_AGENT = "Mozilla/5.0 (compatible; MGMG-CommandCenter/1.0)"

# Google, Uzbekistan, Russian — shops and pages that sell Garmin here.
WEB_QUERIES = (
    "Garmin купить Ташкент",
    "Garmin часы цена Ташкент магазин",
    "Garmin soat narxi Toshkent",
    "Garmin fenix 8 купить Узбекистан",
    "Garmin Forerunner купить Ташкент",
    "site:uzum.uz Garmin",
    "site:t.me Garmin Ташкент",
)
WEB_REUSE_HOURS = 20   # /raqobat within this of the last search reuses its results (search credits)
CHEAPER = 0.95          # "cheaper" = under 95 % of our lowest price for the model
SUSPICIOUS = 0.30       # a new item under 30 % of ours: a copy or a wrong price

# Model families, as they are written once normalised (no diacritics: fēnix → fenix).
FAMILIES = frozenset({
    "fenix", "epix", "forerunner", "venu", "vivoactive", "vivomove", "vivosmart", "vivofit", "instinct",
    "tactix", "enduro", "marq", "quatix", "descent", "approach", "lily", "bounce", "edge", "cirqa", "gpsmap",
    "hrm", "varia", "striker", "index", "montana", "etrex", "inreach", "tacx", "foretrex", "echomap", "swim",
})
_MARQ_EDITIONS = ("athlete", "adventurer", "golfer", "aviator", "captain", "commander", "diver")
_MODEL_WORDS = frozenset({"crossover", "e", "sq"})
# A model number or code — not a size («47mm») and not a year («New 2026»).
_MODEL = re.compile(r"^(?!\d+mm$)(?!20[12]\d$)(?:\d{1,4}[a-z]{0,2}|[a-z]{1,3}\d{1,4}[a-z]{0,2})$")
_GLUED = re.compile(r"\b(fenix|epix|forerunner|venu|vivoactive|vivomove|vivosmart|instinct|tactix|enduro|edge|"
                    r"descent|lily|approach)(\d)")
_SIZE = re.compile(r"\b(\d{2})\s?(?:mm|мм)\b")
# Cyrillic spellings sellers use, normalised the same way as the text.
_TRANSLIT = {"гармин": "garmin", "феникс": "fenix", "фeникс": "fenix", "форераннер": "forerunner",
             "форранер": "forerunner", "форраннер": "forerunner", "инстинкт": "instinct", "тактикс": "tactix",
             "эпикс": "epix", "вену": "venu", "монтана": "montana", "эндуро": "enduro", "вивоактив": "vivoactive",
             "circa": "cirqa", "venus": "venu", "феникс8": "fenix 8"}

_COPY = re.compile(r"\b1\s*[:x]\s*1\b|коп[иі]я|реплик|replica|\bcopy\b|аналог|lux\s*copy|люкс\s*коп", re.I)
_PART = re.compile(r"ремеш|ремен|remesh|\bstrap|безел|bezel|стекл|пл[её]нк|защитн|заряд|кабел|\bcable|charger|чехол|бампер|"
                   r"крепл|держател|адаптер|совмест|для\s+(?:garmin|гармин)|garmin\s+uchun|запчаст|аккумулятор|"
                   r"батаре|\bbattery", re.I)
_REPAIR = re.compile(r"ремонт|remont|repair|прошив|русифик|ta['ʼ’]?mir", re.I)
_PRICE_IN_TEXT = re.compile(r"(\d{1,3}(?:[  .,]\d{3})+|\d{5,})\s*(?:сум|сўм|so['ʻʼ’`]?m|sum\b|uzs)", re.I)
_BUY_WORDS = re.compile(r"купить|цена|narx|sotib|сум|so['ʻʼ’`]?m|в наличии|доставка|магазин|do['ʻʼ’`]?kon|sotuv|"
                        r"buy|price|shop|store|заказ|buyurtma", re.I)
_NEWS_SITES = frozenset({"kun.uz", "daryo.uz", "gazeta.uz", "uza.uz", "podrobno.uz", "spot.uz", "review.uz",
                         "xabar.uz", "qalampir.uz", "uz24.uz", "nuz.uz", "repost.uz", "sports.uz"})
_SOCIAL = frozenset({"t.me", "instagram.com", "facebook.com"})


def normalize(text: str | None) -> str:
    """Lowercase, no diacritics or marks, Cyrillic model names spelt in Latin, sizes as «47mm»."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^\w]+", " ", t.replace("®", " ").replace("™", " "))
    for ru, en in _TRANSLIT.items():
        t = re.sub(rf"\b{ru}\b", en, t)
    t = _GLUED.sub(r"\1 \2", t)
    t = _SIZE.sub(r"\1mm", t)
    return " ".join(t.split())


def model_key(title: str | None) -> str | None:
    """«fenix 8», «fenix 8 pro», «epix 2 pro», «marq athlete 2», «forerunner 970» — or None."""
    words = normalize(title).split()
    for i, word in enumerate(words):
        if word not in FAMILIES:
            continue
        rest = words[i + 1:i + 7]
        parts = [word]
        if word == "marq":
            edition = next((r for r in rest if r in _MARQ_EDITIONS), None)
            if edition:
                parts.append(edition)
        model = next((r for r in rest if r in _MODEL_WORDS or _MODEL.match(r)), None)
        if model:
            parts.append(model)
        if "pro" in rest[:4]:
            parts.append("pro")
        return " ".join(parts)
    return None


def size_mm(title: str | None) -> int | None:
    found = re.search(r"\b(\d{2})mm\b", normalize(title))
    return int(found.group(1)) if found else None


# Names only Garmin uses — «Forerunner 165 Music» is Garmin's without the word «Garmin».
_GARMIN_ONLY = frozenset({"fenix", "epix", "forerunner", "vivoactive", "vivomove", "vivosmart", "vivofit", "instinct",
                          "tactix", "enduro", "marq", "quatix", "cirqa", "gpsmap", "etrex", "inreach", "foretrex", "echomap"})
_NAME = re.compile(r"\b(?:garmin|" + "|".join(sorted(_GARMIN_ONLY)) + r")\b")


def left_out(title: str | None) -> str | None:
    """Why a listing isn't a Garmin product for sale: "copy", "repair", "not_garmin", "part" — or None.

    A part word counts when it comes before the product's name («Ремешок для Garmin Fenix») or the title
    names no model («Garmin кабель+зарядка») — not «Garmin Venu 3S … с коробкой и зарядкой».
    """
    raw = title or ""
    if _COPY.search(raw):
        return "copy"
    if _REPAIR.search(raw):
        return "repair"
    text = normalize(raw)
    name = _NAME.search(text)
    if name is None:
        return "not_garmin"
    part = _PART.search(text)
    if part and (part.start() < name.start() or model_key(raw) is None):
        return "part"
    return None


@dataclass
class OurPrices:
    """garmin.com.uz's prices by model: key → [(size, price so'm, name)]."""

    by_key: dict[str, list[tuple[int | None, int, str]]] = field(default_factory=dict)
    count: int = 0

    @classmethod
    def from_products(cls, products: list[dict[str, Any]]) -> "OurPrices":
        ours = cls()
        for p in products:
            try:
                price = int(float(p.get("price") or 0))
            except (TypeError, ValueError):
                continue
            key = model_key(f"garmin {p.get('name') or ''}")
            if price <= 0 or not key:  # 0 = "ask for the price" on the site
                continue
            ours.by_key.setdefault(key, []).append((size_mm(p.get("name")), price, str(p.get("name") or "").strip()))
            ours.count += 1
        return ours

    def range(self, key: str | None, size: int | None = None) -> tuple[int, int] | None:
        """(lowest, highest) of our prices for the model — that size's when we sell that size."""
        items = self.by_key.get(key or "")
        if not items:
            return None
        sized = [i for i in items if size and i[0] == size]
        prices = [i[1] for i in (sized or items)]
        return min(prices), max(prices)


@dataclass
class Offer:
    """One OLX listing (or, with source "web", one web page)."""

    source: str
    external_id: str
    title: str
    url: str
    price: int | None = None        # so'm
    state: str | None = None        # "new" / "used" / None (not said)
    business: bool = False
    seller: str | None = None       # a shop's name (its OLX name); never a private person's
    shop_ref: str | None = None     # the shop's OLX account — groups its listings (not shown)
    shop_page: str | None = None    # its OLX shop page (<name>.olx.uz), when it has one
    seller_phone: str | None = None  # a shop that put its number as its OLX name
    posted: date | None = None
    city: str | None = None
    olx_prev: int | None = None     # OLX's own "price before"
    key: str | None = None
    size: int | None = None
    # filled by compare / diff
    ours: tuple[int, int] | None = None
    is_new: bool = False
    dropped_from: int | None = None


def _int(value: Any) -> int | None:
    try:
        return int(round(float(value))) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_olx(o: dict[str, Any]) -> Offer | None:
    """One OLX listing → an Offer, or None when it isn't a Garmin product for sale (or has no title)."""
    title = " ".join(str(o.get("title") or "").split())
    if not title or left_out(title):
        return None
    params = {p.get("key"): p.get("value") for p in o.get("params") or [] if isinstance(p, dict)}
    price_v = params.get("price") if isinstance(params.get("price"), dict) else {}
    if price_v.get("currency") == "UZS" or not price_v.get("converted_value"):
        price = _int(price_v.get("value")) if price_v.get("currency") in ("UZS", None) else None
        prev = _int(price_v.get("previous_value")) if price_v.get("currency") in ("UZS", None) else None
    else:  # у.е. — OLX's own conversion to so'm
        price, prev = _int(price_v.get("converted_value")), _int(price_v.get("converted_previous_value"))
    state_v = params.get("state") if isinstance(params.get("state"), dict) else {}
    state = {"new": "new", "used": "used"}.get(str(state_v.get("key") or ""))
    business = bool(o.get("business"))
    user = o.get("user") if isinstance(o.get("user"), dict) else {}
    shop = o.get("shop") if isinstance(o.get("shop"), dict) else {}
    seller = seller_phone = shop_page = None
    if business:  # a shop: the name it sells under on OLX — a private person's is never read
        from integrations.garmin.competitor_shops import phone

        contact = o.get("contact") if isinstance(o.get("contact"), dict) else {}
        shown = str(user.get("company_name") or "").strip() or str(contact.get("name") or "").strip()
        seller_phone = phone(shown) if re.fullmatch(r"\D?\d{9,12}", shown) else None
        seller = None if re.fullmatch(r"\D?\d+", shown) else (shown or None)  # digits alone aren't a name
        sub = str(shop.get("subdomain") or "").strip().lower()
        shop_page = f"https://{sub}.olx.uz/" if re.fullmatch(r"[a-z0-9-]{2,40}", sub) else None
    posted = None
    try:
        posted = datetime.fromisoformat(str(o.get("created_time"))).date()
    except (TypeError, ValueError):
        pass
    city = ((o.get("location") or {}).get("city") or {}).get("name") if isinstance(o.get("location"), dict) else None
    return Offer(source="olx", external_id=str(o.get("id")), title=title, url=str(o.get("url") or ""),
                 price=price if price and price > 0 else None, state=state, business=business, seller=seller,
                 shop_ref=str(user.get("id")) if business else None, shop_page=shop_page, seller_phone=seller_phone,
                 posted=posted, city=city,
                 olx_prev=prev if prev and price and prev > price else None, key=model_key(title), size=size_mm(title))


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def parse_web(result: dict[str, Any]) -> Offer | None:
    """One search result → a web Offer when it is a Uzbek page selling Garmin (not OLX, not ours, not news)."""
    url = str(result.get("url") or result.get("link") or "")
    title = " ".join(str(result.get("title") or "").split())
    snippet = " ".join(str(result.get("snippet") or "").split())
    domain = _domain(url)
    if not url or not domain or domain.endswith("olx.uz") or domain.endswith(OUR_SITE) or domain in _NEWS_SITES:
        return None
    if not (domain.endswith(".uz") or domain in _SOCIAL or any(domain.endswith("." + s) for s in _SOCIAL)):
        return None
    text = f"{title} {snippet}"
    if "garmin" not in normalize(text).split() or left_out(title) in ("copy", "repair", "part"):
        return None
    if not _BUY_WORDS.search(text):
        return None
    price = None
    found = _PRICE_IN_TEXT.search(text)
    if found:
        value = int(re.sub(r"\D", "", found.group(1)))
        price = value if value >= 100_000 else None  # a watch, not a strap or a monthly instalment
    key = url.split("?")[0].split("#")[0].rstrip("/").lower()
    return Offer(source="web", external_id=key, title=title[:200], url=url, price=price, seller=domain,
                 key=model_key(title), size=size_mm(title))


def compare(offers: list[Offer], ours: OurPrices | None) -> None:
    """Fill each offer's our-price range (by model, and size when both say it)."""
    for o in offers:
        o.ours = ours.range(o.key, o.size) if ours else None


def cheaper(o: Offer) -> bool:
    """A NEW item under 95 % of our lowest price for the model (and not suspiciously cheap)."""
    return bool(o.state == "new" and o.price and o.ours and SUSPICIOUS * o.ours[0] <= o.price < CHEAPER * o.ours[0])


def suspicious(o: Offer) -> bool:
    return bool(o.state == "new" and o.price and o.ours and o.price < SUSPICIOUS * o.ours[0])


def diff(previous: dict[tuple[str, str], int | None], offers: list[Offer]) -> bool:
    """Mark what's new since the last check and what got cheaper; False on the very first check (nothing to compare).

    A source checked for the first time marks nothing as new (everything would be).
    """
    checked = {source for source, _ in previous}
    for o in offers:
        before = previous.get((o.source, o.external_id), -1)
        o.is_new = before == -1 and o.source in checked
        stored_drop = before if isinstance(before, int) and before > 0 and o.price and before > o.price else None
        o.dropped_from = stored_drop or o.olx_prev
    return bool(previous)


@dataclass
class Report:
    olx: list[Offer]
    web: list[Offer]
    ours: OurPrices | None
    compared: bool              # an earlier check exists (new / cheaper marks mean something)
    olx_failed: bool = False
    web_state: str = "ok"       # ok / reused / off / failed
    at: datetime | None = None
    shop_list: list[Any] | None = None  # competitor_shops.Shop rows with their contacts (None: not read)

    @property
    def cheaper(self) -> list[Offer]:
        return sorted((o for o in self.olx if cheaper(o)), key=lambda o: (o.price or 0) / (o.ours[0] if o.ours else 1))

    @property
    def suspicious(self) -> list[Offer]:
        return [o for o in self.olx if suspicious(o)]

    def shops(self) -> list[dict[str, Any]]:
        """The competitor shops — OLX business accounts and the sites / channels Google found — with their contacts."""
        from integrations.garmin import competitor_shops as cs

        items = self.shop_list if self.shop_list is not None else cs.merge(cs.gather(self.olx, self.web))
        rows = []
        for shop in items:
            olx = [o for o in shop.offers if o.source == "olx"]
            prices = [o.price for o in olx if o.price]
            models = sorted({o.key for o in shop.offers if o.key})
            link = shop.olx_page or (olx[0].url if olx else f"https://{shop.website}/" if shop.website
                                     else shop.offers[0].url if shop.offers else "")
            shown = (shop.name or shop.website or (f"t.me/{shop.telegram}" if shop.telegram else None)
                     or (f"@{shop.instagram}" if shop.instagram else None) or "номи кўрсатилмаган")
            rows.append({"name": shown, "address": shop.address or "",
                         "phones": list(shop.phones), "instagram": shop.instagram or "", "telegram": shop.telegram or "",
                         "website": shop.website or "", "url": link, "count": len(olx), "pages": len(shop.offers) - len(olx),
                         "new": sum(1 for o in olx if o.state == "new"),
                         "models": ", ".join(models[:5]) + (f" +{len(models) - 5}" if len(models) > 5 else ""),
                         "low": min(prices) if prices else None, "low_text": som(min(prices) if prices else None),
                         "cheaper": sum(1 for o in olx if cheaper(o)), "fresh": any(o.is_new for o in shop.offers)})
        # Named shops first (most listings first), then the unnamed ones.
        return sorted(rows, key=lambda r: (r["name"] == "номи кўрсатилмаган", -r["count"], -r["pages"], r["name"]))


# ---- reading the sources ----------------------------------------------------------------------------------

async def fetch_olx(run_id: uuid.UUID | str | None = None) -> list[Offer]:
    """Every OLX listing with «garmin» / «гармин», each once (OLX's public listing API)."""
    seen: dict[str, Offer] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers={"User-Agent": USER_AGENT,
                                                                       "Accept": "application/json"}) as http:
        for query in OLX_QUERIES:
            async with audited(agent=AGENT, action="api_call", target_system="olx", run_id=run_id,
                               target_ref=OLX_URL, payload={"query": query}) as ctx:
                offset, total, rows = 0, None, 0
                while offset < OLX_MAX and (total is None or offset < total):
                    response = await request_with_retry(http, "GET", OLX_URL, params={
                        "offset": offset, "limit": OLX_PAGE, "query": query})
                    ctx["http_status"] = response.status_code
                    response.raise_for_status()
                    body = response.json()
                    data = body.get("data") or []
                    total = int((body.get("metadata") or {}).get("total_elements") or 0)
                    rows += len(data)
                    for raw in data:
                        offer = parse_olx(raw)
                        if offer is not None:
                            seen.setdefault(offer.external_id, offer)
                    if not data:
                        break
                    offset += OLX_PAGE
                    await asyncio.sleep(0.5)  # gently
                ctx["payload"].update(rows=rows, total=total)
    return list(seen.values())


async def fetch_our_prices(run_id: uuid.UUID | str | None = None) -> OurPrices:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers={"User-Agent": USER_AGENT}) as http:
        async with audited(agent=AGENT, action="api_call", target_system="garmin_com_uz", run_id=run_id,
                           target_ref=OUR_PRICES_URL) as ctx:
            response = await request_with_retry(http, "GET", OUR_PRICES_URL)
            ctx["http_status"] = response.status_code
            response.raise_for_status()
            products = response.json().get("data") or []
            ctx["payload"]["products"] = len(products)
    return OurPrices.from_products(products)


async def search_web(run_id: uuid.UUID | str | None = None) -> list[Offer]:
    """Google (SerpAPI) in Uzbekistan, Russian: pages selling Garmin here. Raises SerpAPIError when not set up."""
    from integrations.search.google_cse_client import search_client
    from integrations.search.serpapi_client import SerpAPIError

    found: dict[str, Offer] = {}
    failures = 0
    async with search_client(AGENT, run_id) as client:
        for query in WEB_QUERIES:
            try:
                results = await client.search(query, "google", num=20, freshness=None, country="uz", language="ru")
            except SerpAPIError as exc:  # one query failing doesn't drop the others
                failures += 1
                log.warning("Web search failed: {}", type(exc).__name__)
                continue
            for r in results:
                offer = parse_web({"url": r.url, "title": r.title, "snippet": r.snippet})
                if offer is not None:
                    found.setdefault(offer.external_id, offer)
    if failures == len(WEB_QUERIES):
        raise SerpAPIError("every web search failed")
    return list(found.values())


async def _previous() -> dict[tuple[str, str], int | None]:
    """The stored price of everything seen before."""
    rows = await fetch_all("SELECT source, external_id, price_uzs FROM garmin_competitor_offers")
    return {(r["source"], r["external_id"]): (int(r["price_uzs"]) if r["price_uzs"] is not None else None) for r in rows}


async def _store(offers: list[Offer]) -> None:
    for o in offers:
        await execute(
            """INSERT INTO garmin_competitor_offers
                   (source, external_id, title, model_key, state, price_uzs, seller, is_business, url, posted_on)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (source, external_id) DO UPDATE SET
                   title = EXCLUDED.title, model_key = EXCLUDED.model_key, state = EXCLUDED.state,
                   seller = EXCLUDED.seller, is_business = EXCLUDED.is_business, url = EXCLUDED.url,
                   prev_price_uzs = CASE WHEN garmin_competitor_offers.price_uzs IS DISTINCT FROM EXCLUDED.price_uzs
                                         THEN garmin_competitor_offers.price_uzs
                                         ELSE garmin_competitor_offers.prev_price_uzs END,
                   price_uzs = EXCLUDED.price_uzs, last_seen = now()""",
            (o.source, o.external_id, o.title, o.key, o.state, o.price, o.seller, o.business, o.url, o.posted),
        )


async def _stored_web() -> list[Offer] | None:
    """The last web search's pages when it ran within WEB_REUSE_HOURS — None otherwise."""
    rows = await fetch_all(
        """SELECT external_id, title, url, price_uzs, seller, model_key
             FROM garmin_competitor_offers
            WHERE source = 'web' AND last_seen >= (SELECT max(last_seen) FROM garmin_competitor_offers WHERE source = 'web')
                                                  - interval '1 hour'
              AND last_seen >= now() - make_interval(hours => %s)""",
        (WEB_REUSE_HOURS,),
    )
    if not rows:
        return None
    return [Offer(source="web", external_id=r["external_id"], title=r["title"], url=r["url"],
                  price=int(r["price_uzs"]) if r["price_uzs"] is not None else None, seller=r["seller"],
                  key=r["model_key"]) for r in rows]


def search_configured() -> bool:
    from integrations.search.google_cse_client import search_configured as configured

    return configured()


async def collect(run_id: uuid.UUID | str | None = None, *, fresh_web: bool = True) -> Report:
    """Read every source, compare with our prices, store, and mark new / cheaper. A failing source says so."""
    report = Report(olx=[], web=[], ours=None, compared=False, at=now_local())
    try:
        report.ours = await fetch_our_prices(run_id)
    except Exception as exc:  # noqa: BLE001 — the listings still go, without our price
        log.warning("Our prices not read: {}", type(exc).__name__)
    try:
        report.olx = await fetch_olx(run_id)
    except Exception as exc:  # noqa: BLE001
        report.olx_failed = True
        log.warning("OLX not read: {}", type(exc).__name__)
    reused = None if fresh_web else await _stored_web()
    if reused is not None:
        report.web, report.web_state = reused, "reused"
    elif not search_configured():
        report.web_state = "off"
    else:
        try:
            report.web = await search_web(run_id)
        except Exception as exc:  # noqa: BLE001
            report.web_state = "failed"
            log.warning("Web search failed: {}", type(exc).__name__)
    compare(report.olx, report.ours)
    fresh = report.olx + ([] if report.web_state == "reused" else report.web)
    previous = await _previous()
    report.compared = diff(previous, fresh)
    await _store(fresh)
    try:  # the shops' names, phones, Instagram, Telegram, site (2026-10-20)
        from integrations.garmin import competitor_shops

        report.shop_list = await competitor_shops.collect(report.olx, report.web, run_id,
                                                          search=fresh_web and report.web_state == "ok")
    except Exception as exc:  # noqa: BLE001 — the listings still go, the shops without contacts
        log.warning("Shop contacts not read: {}", type(exc).__name__)
    return report


# ---- the page --------------------------------------------------------------------------------------------

def som(value: int | None) -> str:
    """12 500 000 → «12,5 млн», 850 000 → «850 минг»."""
    if not value:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".replace(".", ",").replace(",0", "") + " млн"
    return f"{round(value / 1000):d} минг"


def _range(ours: tuple[int, int] | None) -> str:
    if not ours:
        return "—"
    low, high = ours
    return som(low) if low == high else f"{som(low)} – {som(high)}"


def _state(o: Offer) -> str:
    return {"new": "янги", "used": "б/у"}.get(o.state or "", "—")


def _seller(o: Offer) -> str:
    if o.source == "web":
        return o.seller or ""
    return (o.seller or "дўкон (номсиз)") if o.business else "хусусий шахс"


def _row(o: Offer) -> dict[str, Any]:
    diff_pct = round(100 * (o.price - o.ours[0]) / o.ours[0]) if o.price and o.ours else None
    return {"title": o.title, "url": o.url, "key": o.key or "—", "state": _state(o), "price": som(o.price),
            "ours": _range(o.ours), "diff": f"{diff_pct:+d}%" if diff_pct is not None else "",
            "seller": _seller(o), "date": o.posted.strftime("%d.%m.%Y") if o.posted else "",
            "fresh": o.is_new, "dropped": som(o.dropped_from) if o.dropped_from else ""}


def view(report: Report) -> dict[str, Any]:
    olx = report.olx
    listed = sorted(olx, key=lambda o: (o.key or "яяя", o.state != "new", o.price or 0))
    web = sorted(report.web, key=lambda o: (not o.is_new, o.seller or "", o.title))
    notes = []
    if report.olx_failed:
        notes.append("OLX'дан маълумот олиб бўлмади.")
    if report.ours is None:
        notes.append("garmin.com.uz'дан нархларимизни олиб бўлмади — солиштирилмади.")
    web_note = {"off": "веб қидирув уланмаган (GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_CX ёки SERPAPI_API_KEY)", "failed": "веб қидирув ишламади",
                "reused": "охирги қидирув натижаси (кредит тежаш учун бир кунда бир марта қидирилади)"}.get(report.web_state)
    return {
        "title": "Garmin рақобатчилари — Ўзбекистон",
        "date": today_local().strftime("%d.%m.%Y"),
        "when": to_local(report.at).strftime("%d.%m.%Y %H:%M") if report.at else "",
        "olx_count": len(olx), "olx_new": sum(1 for o in olx if o.state == "new"),
        "olx_used": sum(1 for o in olx if o.state == "used"),
        "shops": report.shops(), "cheaper": [_row(o) for o in report.cheaper],
        "suspicious": [_row(o) for o in report.suspicious], "all": [_row(o) for o in listed],
        "web": [_row(o) for o in web], "web_note": web_note, "compared": report.compared,
        "fresh_count": sum(1 for o in olx if o.is_new) if report.compared else None,
        "ours_count": report.ours.count if report.ours else 0, "notes": notes,
    }


def caption(report: Report) -> str:
    v = view(report)
    lines = [f"<b>{v['title']}</b> · {v['date']}"]
    if report.olx_failed:
        lines.append("OLX: маълумот олиб бўлмади")
    else:
        fresh = f", {v['fresh_count']} таси кейинги текширувдан бери янги" if v["fresh_count"] else ""
        lines.append(f"OLX: {v['olx_count']} та Garmin эълони ({v['olx_new']} та янги товар, {v['olx_used']} та б/у){fresh}")
        if report.ours is not None:
            lines.append(f"Янги товарда нархи биздан арзон: <b>{len(v['cheaper'])}</b> та (бизнинг нарх — garmin.com.uz)")
        lines.append(f"Рақобатчи дўконлар: {len(v['shops'])} та — телефони бор: {sum(1 for x in v['shops'] if x['phones'])}, "
                     f"Instagram'и бор: {sum(1 for x in v['shops'] if x['instagram'])}")
    if v["web_note"] and report.web_state != "reused":
        lines.append(f"Бошқа сайтлар: {v['web_note']}")
    else:
        lines.append(f"Бошқа сайтлар ва дўконлар (Google): {len(v['web'])} та")
    lines.append("Батафсил — PDF'да.")
    return "\n".join(lines)


def text_fallback(report: Report) -> str:
    v = view(report)
    lines = [caption(report), ""]
    if v["cheaper"]:
        lines.append("<b>Янги товар — биздан арзон</b> (энг арзонидан):")
        lines += [f"• {r['title'][:50]} — {r['price']} (бизда {r['ours']}, {r['diff']}) · {r['seller']}" for r in v["cheaper"][:20]]
    if v["shops"]:
        lines.append("")
        lines.append("<b>Рақобатчи дўконлар:</b>")
        lines += [f"• {s['name']}" + (f" — {', '.join(s['phones'][:2])}" if s["phones"] else "")
                  + (f" · instagram.com/{s['instagram']}" if s["instagram"] else "")
                  + (f" · t.me/{s['telegram']}" if s["telegram"] else "")
                  + (f" · {s['count']} та эълон" if s["count"] else "") for s in v["shops"][:15]]
    if v["web"]:
        lines.append("")
        lines.append("<b>Бошқа сайтлар:</b>")
        lines += [f"• {r['seller']} — {r['title'][:50]}" + (f" — {r['price']}" if r["price"] != "—" else "") for r in v["web"][:15]]
    return "\n".join(lines)


async def send(chat_id: int, run_id: uuid.UUID, *, fresh_web: bool = True) -> str:
    """The check to one chat: a PDF and a short caption (the text when the PDF can't be drawn)."""
    from integrations.reports import render
    from integrations.telegram.bot import TelegramBot

    report = await collect(run_id, fresh_web=fresh_web)
    async with TelegramBot(agent=AGENT, run_id=run_id,
                           bot_token=settings.ops_manager_bot_telegram_bot_token.get_secret_value()) as bot:
        if report.olx_failed and not report.web:
            await bot.send_message("Рақобатчилар: OLX ва бошқа сайтлардан маълумот олиб бўлмади, кейинроқ қайта уриниб кўринг.",
                                   chat_id=str(chat_id))
            return "competitors_failed"
        try:
            pdf = render.pdf("garmin_competitors.html", **view(report))
        except Exception as exc:  # noqa: BLE001 — the text instead
            log.warning("Competitors PDF failed: {}", type(exc).__name__)
            await bot.send_message(text_fallback(report), chat_id=str(chat_id))
            return "competitors_text"
        await bot.send_file(pdf, f"garmin-raqobatchilar-{today_local():%Y-%m-%d}.pdf", str(chat_id), caption(report))
    return "competitors_sent"


async def answer_lines(days: int = 8) -> str:
    """What the last check found, for the analyst (the Garmin manager's questions)."""
    rows = await fetch_all(
        """SELECT source, title, model_key, state, price_uzs, prev_price_uzs, seller, is_business, url, posted_on,
                  first_seen, last_seen
             FROM garmin_competitor_offers WHERE last_seen >= now() - make_interval(days => %s)
            ORDER BY source, model_key NULLS LAST, price_uzs NULLS LAST""",
        (days,),
    )
    if not rows:
        return ("No competitor check in the last week (OLX listings and Uzbek web shops selling Garmin are checked "
                "weekly for the Garmin manager and with /raqobat).")
    last = max(r["last_seen"] for r in rows)
    lines = [f"Garmin sold by others in Uzbekistan — the last check {to_local(last):%Y-%m-%d %H:%M} (OLX listings and web "
             "pages; prices in so'm as listed; a private seller is never named). Our own prices: the garmin_catalog source."]
    for r in rows:
        who = r["seller"] or ("private person" if r["source"] == "olx" else "")
        if r["source"] == "olx" and r["is_business"] and not r["seller"]:
            who = "shop (no name)"
        price = f"{int(r['price_uzs']):,} so'm".replace(",", " ") if r["price_uzs"] else "no price"
        before = f" (was {int(r['prev_price_uzs']):,})".replace(",", " ") if r["prev_price_uzs"] else ""
        lines.append(f"- {r['source']} | {r['model_key'] or '?'} | {r['state'] or '-'} | {price}{before} | {who} | "
                     f"{r['posted_on'] or ''} | «{r['title']}» | {r['url']}")
    shops = await fetch_all(
        """SELECT name, instagram, telegram, website, address, olx_page FROM garmin_competitor_shops
            WHERE last_seen >= now() - make_interval(days => %s) ORDER BY name NULLS LAST""",
        (days,),
    )
    if shops:
        # Phones stay out of the AI (personal data is masked before AI — the owner's IT instruction); they are in the PDF.
        lines.append("Competitor shops (their own published contacts; phone numbers: in the /raqobat PDF):")
        for r in shops:
            parts = [f"instagram.com/{r['instagram']}" if r["instagram"] else "", f"t.me/{r['telegram']}" if r["telegram"] else "",
                     r["website"] or "", r["address"] or "", r["olx_page"] or ""]
            lines.append(f"- {r['name'] or 'no name'} | " + " | ".join(x for x in parts if x))
    return "\n".join(lines)
