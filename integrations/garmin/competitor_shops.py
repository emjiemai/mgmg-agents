"""Competitor shops' contacts — name, phone, Instagram, Telegram, site (2026-10-20, the owner).

"We need to gather details like do'kon nomi, phone number, and Instagram page too." For the
**shops** selling Garmin (``competitors.py`` finds them) — only what the shop itself publishes
for customers:

  * an OLX business account: its name; when it has an OLX shop page (``<name>.olx.uz``), the
    page's own contact block — phones, its Telegram / site link, address;
  * its site (from the OLX page or the Google results): ``tel:`` links and +998 numbers,
    Instagram and Telegram links, the site's name;
  * its Telegram channel (``t.me/<channel>``, the public preview page): the channel's name and
    description (phones, Instagram written there);
  * no Instagram yet: one Google search for "<name> instagram" in the weekly run (at most
    ``SEARCH_BUDGET`` shops, each once in ``SEARCH_DAYS``), taken only when the result's title
    or address holds the shop's name.

**Private persons are never looked up**: OLX shows their phone only behind its "show phone"
button, and a person's number is personal data — only shops (business accounts, sites,
channels) are. Pages are read only where the site's robots.txt allows it. Contacts are kept
in ``garmin_competitor_shops`` and re-read after ``CHECK_DAYS``. Phones never go to the AI.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from integrations.common.db import audited, execute, fetch_all
from integrations.common.logging_setup import setup_logging

AGENT = "garmin-competitors"
log = setup_logging(AGENT)

USER_AGENT = "Mozilla/5.0 (compatible; MGMG-CommandCenter/1.0)"
CHECK_DAYS = 7        # contacts read again after this
SEARCH_DAYS = 30      # Google asked for a shop's Instagram at most this often
SEARCH_BUDGET = 8     # shops searched per weekly run (search credits)
PAGE_BUDGET = 40      # pages read per run
MAX_BYTES = 3_000_000
OLX_SUPPORT = "+998 71 202 07 07"  # OLX's own number, on every OLX page

_PHONE = re.compile(r"(?<![\d+])\+?\s?998[\s\-.]?\(?(\d{2})\)?[\s\-.]?(\d{3})[\s\-.]?(\d{2})[\s\-.]?(\d{2})(?!\d)")
_TEL = re.compile(r"href=[\"']tel:([^\"']+)", re.I)
_IG = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)
_IG_TEXT = re.compile(r"(?:instagram|insta|инстаграм)\s*[:\-–—]?\s*@([A-Za-z0-9_.]{2,30})", re.I)
_TG = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})", re.I)
_IG_RESERVED = frozenset({"p", "reel", "reels", "explore", "accounts", "stories", "share", "tv", "about", "legal",
                          "developer", "direct", "web", "instagram"})
_TG_RESERVED = frozenset({"share", "joinchat", "addstickers", "proxy", "socks", "iv", "login", "setlanguage", "s"})
_META = re.compile(r"<meta\s+[^>]*(?:property|name)=[\"'](og:[a-z_]+|description)[\"'][^>]*content=[\"']([^\"']*)[\"']", re.I)
_TITLE = re.compile(r"<title[^>]*>([^<]{1,200})</title>", re.I)


def phone(value: str | None) -> str | None:
    """Any way of writing a Uzbek number → «+998 90 123 45 67»; None when it isn't one."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) != 12 or not digits.startswith("998"):
        return None
    return f"+998 {digits[3:5]} {digits[5:8]} {digits[8:10]} {digits[10:12]}"


def contacts_in(text: str | None) -> tuple[list[str], str | None, str | None]:
    """(phones, Instagram handle, Telegram handle) written in a page or a description."""
    text = text or ""
    found: list[str] = []
    for raw in [*_TEL.findall(text), *("998" + "".join(m) for m in _PHONE.findall(text))]:
        p = phone(raw)
        if p and p != OLX_SUPPORT and p not in found:
            found.append(p)
    ig = next((h.rstrip(".").lower() for h in [*_IG.findall(text), *_IG_TEXT.findall(text)]
               if h.rstrip(".").lower() not in _IG_RESERVED), None)
    tg = next((h.lower() for h in _TG.findall(text) if h.lower() not in _TG_RESERVED), None)
    return found[:4], ig, tg


def olx_shop_page(html: str) -> dict[str, Any] | None:
    """An OLX shop page's own contact block (the page's embedded data): name, phones, link, address."""
    m = re.search(r'window\.__PRERENDERED_STATE__\s*=\s*"((?:[^"\\]|\\.)*)"', html or "")
    if not m:
        return None
    try:
        state = json.loads(json.loads(f'"{m.group(1)}"'))
    except (ValueError, TypeError):
        return None

    def find(node: Any, depth: int = 0) -> dict[str, Any] | None:
        if depth > 14:
            return None
        if isinstance(node, dict):
            if isinstance(node.get("contact"), dict) and "phones" in node["contact"] and "name" in node:
                return node
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            return None
        for child in children:
            hit = find(child, depth + 1)
            if hit:
                return hit
        return None

    page = find(state)
    if not page:
        return None
    contact = page["contact"]
    address = page.get("address") if isinstance(page.get("address"), dict) else {}
    street = " ".join(str(address.get(k) or "").strip() for k in ("street", "number") if address.get(k)
                      and str(address.get(k)).strip() not in ("0000",))
    return {"name": str(page.get("name") or "").strip() or None,
            "phones": [p for p in (phone(x) for x in contact.get("phones") or []) if p],
            "link": str(contact.get("external_page_url") or "").strip() or None,
            "address": street or None}


def page_meta(html: str) -> dict[str, str]:
    """og:title / og:site_name / og:description / description, and <title>."""
    meta = {k.lower(): v.strip() for k, v in _META.findall(html or "")}
    title = _TITLE.search(html or "")
    if title:
        meta.setdefault("title", " ".join(title.group(1).split()))
    return meta


def _domain(url: str | None) -> str | None:
    host = (urlparse(url or "").hostname or "").lower()
    return (host[4:] if host.startswith("www.") else host) or None


def _norm(text: str | None) -> str:
    from integrations.garmin.competitors import normalize

    return normalize(text).replace(" ", "")


_GENERIC = frozenset({"seller", "продавец", "магазин", "shop", "store", "sotuvchi", "dokon", "дўкон", "дукон"})


def real_name(name: str | None) -> str | None:
    """A shop's name as shown — None for «Seller», «Магазин» and the like."""
    name = " ".join((name or "").split())
    return None if not name or _norm(name) in _GENERIC else name


def looks_like_shop(shop: "Shop") -> bool:
    """Worth a Google search for its Instagram: it has an OLX shop page or a site, or a name no person has —
    two words, a dot / dash / digit («A.Mobile», «Hyper Phone»). A lone first name («Умида», «Rustam») is
    skipped: a search would find somebody else's page."""
    name = shop.name or ""
    return bool(shop.olx_page or shop.website or len(name.split()) > 1 or re.search(r"[.\-_\d]", name))


def name_matches(name: str | None, text: str | None) -> bool:
    """The shop's name (4+ letters) is in the text — «Texnikach» in «Texnikach (@texnikach) • Instagram»."""
    n = _norm(name)
    return len(n) >= 4 and n in _norm(text)


@dataclass
class Shop:
    key: str                          # olx:<account> / site:<domain> / tg:<channel> / ig:<handle>
    name: str | None = None
    phones: list[str] = field(default_factory=list)
    instagram: str | None = None
    telegram: str | None = None
    website: str | None = None
    address: str | None = None
    olx_page: str | None = None
    sources: set[str] = field(default_factory=set)
    offers: list[Any] = field(default_factory=list)   # its OLX listings / web pages this run
    checked_at: datetime | None = None
    searched_at: datetime | None = None

    def add(self, phones: list[str] = (), instagram: str | None = None, telegram: str | None = None,
            website: str | None = None) -> None:
        for p in phones:
            if p not in self.phones and len(self.phones) < 4:
                self.phones.append(p)
        self.instagram = self.instagram or instagram
        self.telegram = self.telegram or telegram
        self.website = self.website or website


def gather(olx: list[Any], web: list[Any]) -> list[Shop]:
    """The shops behind this run's listings: OLX business accounts and the sites / channels Google found.

    A private person's listing makes no shop (not looked up, not stored).
    """
    shops: dict[str, Shop] = {}
    for o in olx:
        if not o.business or not o.shop_ref:
            continue
        shop = shops.setdefault(f"olx:{o.shop_ref}", Shop(key=f"olx:{o.shop_ref}", sources={"olx"}))
        shop.name = shop.name or real_name(o.seller)
        shop.olx_page = shop.olx_page or o.shop_page
        if o.seller_phone:
            shop.add(phones=[o.seller_phone])
        shop.offers.append(o)
    for o in web:
        domain = _domain(o.url) or ""
        parts = urlparse(o.url).path.strip("/").split("/")
        handle = (parts[1] if parts[0] == "s" and len(parts) > 1 else parts[0]).lower()  # t.me/s/<channel> too
        if domain in ("t.me", "telegram.me") and handle and handle not in _TG_RESERVED:
            key, kind = f"tg:{handle}", "telegram"
        elif domain.endswith("instagram.com") and handle and handle not in _IG_RESERVED:
            key, kind = f"ig:{handle}", "instagram"
        elif domain and not domain.endswith(("t.me", "instagram.com", "facebook.com")):
            key, kind = f"site:{domain}", "website"
        else:
            continue
        shop = shops.setdefault(key, Shop(key=key, sources={"google"}))
        setattr(shop, kind, getattr(shop, kind) or (handle if kind != "website" else domain))
        shop.offers.append(o)
    return list(shops.values())


def merge(shops: list[Shop]) -> list[Shop]:
    """One row per shop: the same Telegram, Instagram, site or name is the same shop."""
    out: list[Shop] = []
    for shop in shops:
        ids = shop_ids(shop)
        twin = next((s for s in out if ids & shop_ids(s)), None)
        if twin is None:
            out.append(shop)
            continue
        twin.name = twin.name or shop.name
        twin.add(shop.phones, shop.instagram, shop.telegram, shop.website)
        twin.address = twin.address or shop.address
        twin.olx_page = twin.olx_page or shop.olx_page
        twin.sources |= shop.sources
        twin.offers += shop.offers
    return out


def shop_ids(shop: Shop) -> set[str]:
    ids = {f"tg:{shop.telegram}" if shop.telegram else "", f"ig:{shop.instagram}" if shop.instagram else "",
           f"site:{shop.website}" if shop.website else "", f"name:{_norm(shop.name)}" if len(_norm(shop.name)) >= 4 else ""}
    return ids - {""}


# ---- reading their pages -----------------------------------------------------------------------------------

class _Reader:
    """Polite page reader: robots.txt first, a page budget, a size cap."""

    def __init__(self, http: httpx.AsyncClient, budget: int = PAGE_BUDGET) -> None:
        self.http, self.budget = http, budget
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            rules = None
            try:
                r = await self.http.get(origin + "/robots.txt")
                if r.status_code == 200:
                    rules = robotparser.RobotFileParser()
                    rules.parse(r.text.splitlines())
            except httpx.HTTPError:
                pass
            self._robots[origin] = rules
        rules = self._robots[origin]
        return rules is None or rules.can_fetch(USER_AGENT, url)

    async def read(self, url: str) -> str | None:
        if self.budget <= 0 or not url.startswith(("http://", "https://")) or not await self.allowed(url):
            return None
        self.budget -= 1
        try:
            r = await self.http.get(url)
        except httpx.HTTPError:
            return None
        return r.text[:MAX_BYTES] if r.status_code == 200 else None


async def _read_shop(reader: _Reader, shop: Shop) -> None:
    """Its OLX shop page, its site, its Telegram channel — what each says about how to reach the shop."""
    if shop.olx_page:
        page = olx_shop_page(await reader.read(shop.olx_page) or "")
        if page:
            shop.name = real_name(page["name"]) or shop.name
            shop.address = page["address"] or shop.address
            link = page["link"] or ""
            _, ig, tg = contacts_in(link)
            site = _domain(link) if link and not ig and not tg else None
            shop.add(page["phones"], ig, tg, site)
    if shop.website:
        html = await reader.read(f"https://{shop.website}/") or ""
        phones, ig, tg = contacts_in(html)
        shop.add(phones, ig, tg)
        meta = page_meta(html)
        shop.name = shop.name or meta.get("og:site_name") or (meta.get("title") or "").split("|")[0].split(" - ")[0].strip() or None
    if shop.telegram:
        html = await reader.read(f"https://t.me/{shop.telegram}") or ""
        meta = page_meta(html)
        phones, ig, _ = contacts_in(meta.get("og:description"))
        shop.add(phones, ig)
        title = meta.get("og:title")
        if title and not title.lower().startswith("telegram"):
            shop.name = shop.name or title


async def _search_instagram(client: Any, shop: Shop) -> None:
    """Google "<name> instagram" — an Instagram (and Telegram, site) whose title or address holds the name."""
    results = await client.search(f"{shop.name} instagram", "google", num=10, freshness=None, country="uz", language="ru")
    for r in results:
        domain = _domain(r.url) or ""
        handle = urlparse(r.url).path.strip("/").split("/")[0].lower()
        if not name_matches(shop.name, f"{r.title} {handle}"):
            continue
        if domain.endswith("instagram.com") and handle and handle not in _IG_RESERVED:
            shop.instagram = shop.instagram or handle
        elif domain in ("t.me", "telegram.me") and handle and handle not in _TG_RESERVED:
            shop.telegram = shop.telegram or handle
        elif domain.endswith(".uz") and not domain.endswith("olx.uz"):
            shop.website = shop.website or domain


async def _cached(keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    rows = await fetch_all("SELECT * FROM garmin_competitor_shops WHERE key = ANY(%s)", (keys,))
    return {r["key"]: r for r in rows}


async def _store(shops: list[Shop]) -> None:
    for s in shops:
        await execute(
            """INSERT INTO garmin_competitor_shops (key, name, phones, instagram, telegram, website, address, olx_page,
                                                    checked_at, searched_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET
                   name = COALESCE(EXCLUDED.name, garmin_competitor_shops.name),
                   phones = CASE WHEN cardinality(EXCLUDED.phones) > 0 THEN EXCLUDED.phones ELSE garmin_competitor_shops.phones END,
                   instagram = COALESCE(EXCLUDED.instagram, garmin_competitor_shops.instagram),
                   telegram = COALESCE(EXCLUDED.telegram, garmin_competitor_shops.telegram),
                   website = COALESCE(EXCLUDED.website, garmin_competitor_shops.website),
                   address = COALESCE(EXCLUDED.address, garmin_competitor_shops.address),
                   olx_page = COALESCE(EXCLUDED.olx_page, garmin_competitor_shops.olx_page),
                   checked_at = COALESCE(EXCLUDED.checked_at, garmin_competitor_shops.checked_at),
                   searched_at = COALESCE(EXCLUDED.searched_at, garmin_competitor_shops.searched_at),
                   last_seen = now()""",
            (s.key, s.name, s.phones, s.instagram, s.telegram, s.website, s.address, s.olx_page, s.checked_at,
             s.searched_at),
        )


def _fresh(at: datetime | None, days: int, now: datetime) -> bool:
    return at is not None and now - at < timedelta(days=days)


async def collect(olx: list[Any], web: list[Any], run_id: uuid.UUID | str | None = None, *,
                  search: bool = False) -> list[Shop]:
    """The shops of this run with their contacts: stored ones reused for CHECK_DAYS, the rest read now;
    ``search`` (the weekly run) also asks Google for a missing Instagram. One row per shop."""
    shops = gather(olx, web)
    now = datetime.now(timezone.utc)
    cached = await _cached([s.key for s in shops])
    for s in shops:
        row = cached.get(s.key)
        if row:
            s.name = row.get("name") or s.name
            s.add(list(row.get("phones") or []), row.get("instagram"), row.get("telegram"), row.get("website"))
            s.address = s.address or row.get("address")
            s.olx_page = s.olx_page or row.get("olx_page")
            s.checked_at, s.searched_at = row.get("checked_at"), row.get("searched_at")
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as http:
        reader = _Reader(http)
        async with audited(agent=AGENT, action="api_call", target_system="competitor_pages", run_id=run_id) as ctx:
            read = 0
            for s in shops:
                if _fresh(s.checked_at, CHECK_DAYS, now):
                    continue
                try:
                    await _read_shop(reader, s)
                    s.checked_at = now
                    read += 1
                except Exception as exc:  # noqa: BLE001 — one shop's page doesn't stop the others
                    log.warning("Shop page not read: {}", type(exc).__name__)
            ctx["payload"].update(shops=len(shops), read=read)
    if search:
        await _search_missing(shops, run_id, now)
    await _store(shops)
    return merge(shops)


async def _search_missing(shops: list[Shop], run_id: uuid.UUID | str | None, now: datetime) -> None:
    from integrations.search.google_cse_client import search_client, search_configured
    from integrations.search.serpapi_client import SerpAPIError

    wanted = [s for s in shops if s.name and not s.instagram and len(_norm(s.name)) >= 4 and looks_like_shop(s)
              and not _fresh(s.searched_at, SEARCH_DAYS, now)][:SEARCH_BUDGET]
    if not wanted or not search_configured():
        return
    try:
        async with search_client(AGENT, run_id) as client:
            for s in wanted:
                try:
                    await _search_instagram(client, s)
                    s.searched_at = now
                except SerpAPIError as exc:
                    log.warning("Instagram search failed: {}", type(exc).__name__)
    except SerpAPIError as exc:
        log.warning("Search not set up: {}", type(exc).__name__)
