"""Our own lead search index — Uzbek news, Telegram channels, Google News (2026-10-30, the owner).

«Lead agent is working bad — we are losing opportunities; build a search engine like Tavily and SerpApi.»
The Lead Agent ran 51 searches every morning through SerpAPI (× Google, Bing, Yandex) and Tavily — about
4 600 + 1 500 requests a month against their plans' limits — so after the first days of each month every
search failed (429 / 432) and the agent found nothing. A general search engine can't be built (billions of
pages; Google, Bing and DuckDuckGo block servers), but our leads come from a few dozen Uzbek sources, so we
collect those ourselves and search them in our own database:

  * **collect** (``crawl``): every ``CRAWL_EVERY_HOURS`` in the day (the API's 5-minute round) and before the
    08:00 Lead Agent — the news sites' RSS feeds, public Telegram channels (``t.me/s/<channel>``, no login)
    and, once a day, Google News RSS for the Lead Agent's own queries (free, no key). Each article is kept
    once (by URL): title, a short text, link, date — ``KEEP_DAYS`` days.
  * **search** (``search``): PostgreSQL full-text search over that index — an «object» word (гостиница /
    mehmonxona / больница / прачечная / марафон…) AND a «signal» word (строительство / qurilish / открытие /
    тендер / спонсор…), prefixes, Uzbek Latin and Cyrillic, Russian, English. Only the matches go to the AI.
  * Sources are lists below: a site's feed or a Telegram channel is added in one line. Nothing is sent to
    anyone; logs keep counts only.
"""

from __future__ import annotations

import asyncio
import html
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import httpx

from integrations.common.db import execute, fetch_all, fetch_one, log_action
from integrations.common.logging_setup import setup_logging
from integrations.search.models import RawLead

AGENT = "lead-crawler"
log = setup_logging(AGENT)

CRAWL_EVERY_HOURS = 3
CRAWL_HOURS = range(6, 23)  # Tashkent time
KEEP_DAYS = 60
TIMEOUT = 20.0
CONCURRENCY = 4
BODY_CHARS = 1500
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Checked live 2026-10-30 (each answered with 15–60 items). Add a feed: one line.
RSS_FEEDS: dict[str, str] = {
    "kun.uz": "https://kun.uz/news/rss",
    "kun.uz/ru": "https://kun.uz/ru/news/rss",
    "gazeta.uz/ru": "https://www.gazeta.uz/ru/rss/",
    "gazeta.uz/oz": "https://www.gazeta.uz/oz/rss/",
    "daryo.uz": "https://daryo.uz/feed",
    "daryo.uz/ru": "https://daryo.uz/ru/feed",
    "spot.uz/ru": "https://www.spot.uz/ru/rss/",
    "spot.uz/oz": "https://www.spot.uz/oz/rss/",
    "uza.uz/ru": "https://uza.uz/ru/rss",
    "review.uz": "https://review.uz/rss",
    "nuz.uz": "https://nuz.uz/feed",
}
# Public channels read at t.me/s/<name> (active on 2026-10-30): construction ministry, sport ministry and
# Olympic committee (federations, events, sponsors), sport news, business news.
TELEGRAM_CHANNELS: tuple[str, ...] = ("minstroyuz", "minsportuz", "olympicuz", "sportuz", "uzdaily")
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}+when:90d&hl={hl}&gl=UZ&ceid=UZ:{hl}"

# What a lead looks like: an object we sell to / for AND a sign that something is starting.
OBJECT_WORDS = (
    # hotels, resorts
    "гостиниц", "отел", "hotel", "mehmonxon", "меҳмонхон", "мехмонхон", "курорт", "kurort", "resort", "санатори",
    "sanatori", "hilton", "hyatt", "marriott", "radisson", "wyndham", "accor", "ibis", "novotel",
    # medicine
    "больниц", "клиник", "klinik", "clinic", "поликлиник", "госпитал", "hospital", "shifoxon", "шифохон",
    "медцентр", "перинатал", "perinatal", "tibbiyot",
    # laundry, workwear, industry
    "прачечн", "laundr", "химчистк", "стирк", "kiryuv", "кирювиш", "спецодежд",
    # sport, fitness, events (Garmin / Tanita)
    "марафон", "marafon", "marathon", "велоспорт", "velosport", "cycling", "велогонк", "триатлон", "triathlon",
    "фитнес", "fitnes", "fitness", "olimpiya", "олимпий", "федерац", "federatsiya", "федерация",
)
SIGNAL_WORDS = (
    "строител", "строит", "построя", "qurilish", "қурилиш", "quril", "қурил", "construct", "build",
    "открыт", "откро", "ochil", "очил", "opening", "open", "запуск", "launch",
    "тендер", "tender", "закупк", "xarid", "харид", "конкурс", "tanlov",
    "инвест", "invest", "проект", "loyiha", "лойиҳа", "project",
    "спонсор", "sponsor", "homiy", "ҳомий", "партнер", "партнёр", "partner", "hamkor", "ҳамкор",
    "соглашен", "kelishuv", "келишув", "меморандум", "memorandum",
    "чемпионат", "championship", "турнир", "tournament", "забег", "старт",
)
MAX_MATCHES = 150  # newest first — what the Lead Agent's AI qualifies in one morning


def _clean(text: str | None) -> str:
    """HTML → plain text, one space between words."""
    text = re.sub(r"<br\s*/?>", " ", text or "", flags=re.I)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_rss(xml_text: str, source: str) -> list[dict[str, Any]]:
    """RSS 2.0 items or Atom entries → {url, title, body, published_at, source}."""
    try:
        root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:  # a feed that isn't valid XML (review.uz: an undeclared «atom:» prefix) — read by pattern
        return _parse_rss_loosely(xml_text, source)
    atom = "{http://www.w3.org/2005/Atom}"
    out = []
    for item in root.iter("item"):
        url = (item.findtext("link") or "").strip()
        title = _clean(item.findtext("title"))
        body = _clean(item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded"))
        if url and title:
            out.append({"url": url, "title": title[:500], "body": body[:BODY_CHARS], "source": source,
                        "published_at": _date(item.findtext("pubDate"))})
    for entry in root.iter(f"{atom}entry"):
        link = entry.find(f"{atom}link")
        url = (link.get("href") if link is not None else "") or ""
        title = _clean(entry.findtext(f"{atom}title"))
        if url and title:
            out.append({"url": url.strip(), "title": title[:500], "source": source,
                        "body": _clean(entry.findtext(f"{atom}summary") or entry.findtext(f"{atom}content"))[:BODY_CHARS],
                        "published_at": _date(entry.findtext(f"{atom}updated") or entry.findtext(f"{atom}published"))})
    return out


_ITEM = re.compile(r"<item[\s>].*?</item>", re.S)


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    return re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", m.group(1).strip(), flags=re.S) if m else ""


def _parse_rss_loosely(xml_text: str, source: str) -> list[dict[str, Any]]:
    out = []
    for block in _ITEM.findall(xml_text or ""):
        url, title = _tag(block, "link").strip(), _clean(_tag(block, "title"))
        if url and title:
            out.append({"url": url, "title": title[:500], "body": _clean(_tag(block, "description"))[:BODY_CHARS],
                        "source": source, "published_at": _date(_tag(block, "pubDate"))})
    return out


_TG_POST = re.compile(r'data-post="([A-Za-z0-9_]+/\d+)"')
_TG_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TG_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')


def parse_telegram(page: str, channel: str) -> list[dict[str, Any]]:
    """A public channel's web page (t.me/s/<channel>) → one item per post with text."""
    out = []
    for block in page.split("tgme_widget_message_wrap")[1:]:
        post, text = _TG_POST.search(block), _TG_TEXT.search(block)
        if not (post and text):
            continue
        body = _clean(text.group(1))
        if not body:
            continue
        when = _TG_TIME.search(block)
        out.append({"url": f"https://t.me/{post.group(1)}", "title": body[:160], "body": body[:BODY_CHARS],
                    "source": f"telegram:{channel}", "published_at": _date(when.group(1)) if when else None})
    return out


def google_news_url(query: str) -> str:
    """Google News RSS for one query — Russian or Uzbek interface by the query's letters."""
    hl = "ru" if re.search(r"[А-Яа-яЁё]", query) else ("uz" if re.search(r"o'z|ozbek|o‘|yangi|qurilish", query, re.I) else "en")
    return GOOGLE_NEWS.format(q=quote_plus(query), hl=hl)


def tsquery() -> str:
    """(object words) & (signal words), each a prefix — for to_tsquery('simple', …)."""
    def group(words: tuple[str, ...]) -> str:
        return "(" + " | ".join(f"{w}:*" for w in dict.fromkeys(w.lower() for w in words)) + ")"

    return f"{group(OBJECT_WORDS)} & {group(SIGNAL_WORDS)}"


# ---------------------------------------------------------------------------------------------------- storage


async def store(items: list[dict[str, Any]]) -> int:
    """Keep new items (by URL); how many were new."""
    added = 0
    for item in items:
        row = await fetch_one(
            """INSERT INTO crawl_items (url, source, title, body, published_at)
               VALUES (%(url)s, %(source)s, %(title)s, %(body)s, %(published_at)s)
               ON CONFLICT (url) DO NOTHING RETURNING id""",
            item,
        )
        added += row is not None
    return added


async def search(fetched_since: datetime, limit: int = MAX_MATCHES) -> list[RawLead]:
    """The index's matches fetched since then, newest first, as the Lead Agent's raw leads."""
    rows = await fetch_all(
        """SELECT url, source, title, body, published_at FROM crawl_items
           WHERE fetched_at >= %s AND tsv @@ to_tsquery('simple', %s)
           ORDER BY coalesce(published_at, fetched_at) DESC LIMIT %s""",
        (fetched_since, tsquery(), limit),
    )
    return [RawLead(source=f"index:{r['source']}", title=r["title"], url=r["url"], snippet=r["body"][:600] if r["body"] else None,
                    published_at=r["published_at"]) for r in rows]


# ---------------------------------------------------------------------------------------------------- crawling


async def _get(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    return response.text


async def crawl(run_id: uuid.UUID | None = None, *, google_news_queries: list[str] | None = None) -> dict[str, int]:
    """Fetch every source once and keep what's new. Returns counts (sources, failed, items, new)."""
    jobs: list[tuple[str, str, Any]] = [(name, url, lambda text, name=name: parse_rss(text, name))
                                        for name, url in RSS_FEEDS.items()]
    jobs += [(f"telegram:{c}", f"https://t.me/s/{c}", lambda text, c=c: parse_telegram(text, c)) for c in TELEGRAM_CHANNELS]
    jobs += [("google_news", google_news_url(q), lambda text: parse_rss(text, "google_news")) for q in google_news_queries or []]
    gate = asyncio.Semaphore(CONCURRENCY)
    failed: list[str] = []

    async def one(client: httpx.AsyncClient, name: str, url: str, parse: Any) -> list[dict[str, Any]]:
        async with gate:
            try:
                return parse(await _get(client, url))
            except Exception as exc:  # noqa: BLE001 — one source down never stops the others
                failed.append(f"{name}: {type(exc).__name__}")
                return []

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        batches = await asyncio.gather(*[one(client, n, u, p) for n, u, p in jobs])
    items = {i["url"]: i for batch in batches for i in batch}
    new = await store(list(items.values()))
    await execute("DELETE FROM crawl_items WHERE fetched_at < now() - make_interval(days => %s)", (KEEP_DAYS,))
    await execute("INSERT INTO crawl_runs (sources, failed, items, new_items) VALUES (%s, %s, %s, %s)",
                  (len(jobs), len(failed), len(items), new))
    counts = {"sources": len(jobs), "failed": len(failed), "items": len(items), "new": new}
    await log_action(agent=AGENT, action="crawl", target_system="web", status="success" if not failed else "partial",
                     run_id=run_id, mode="read", payload={**counts, "failed_sources": failed[:20]})
    log.info("Crawl: {} sources ({} failed), {} items, {} new", len(jobs), len(failed), len(items), new)
    return counts


async def due(now: datetime) -> bool:
    """Whether the API's round should crawl now: working hours, the last crawl ``CRAWL_EVERY_HOURS`` ago."""
    if now.hour not in CRAWL_HOURS:
        return False
    row = await fetch_one("SELECT max(started_at) AS at FROM crawl_runs")
    last = row["at"] if row else None
    return last is None or datetime.now(timezone.utc) - last >= timedelta(hours=CRAWL_EVERY_HOURS)


async def tick(now: datetime) -> dict[str, int] | None:
    """The API's 5-minute round: crawl when due (news and Telegram; Google News only with the 08:00 agent)."""
    if not await due(now):
        return None
    return await crawl(uuid.uuid4())
