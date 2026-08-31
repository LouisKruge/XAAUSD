"""News ingestion from RSS/Atom feeds.

Free, no key required, and enough for the risk layer. A commercial API can be dropped
in behind the same interface.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from xauusd.intelligence.news import NewsItem, is_gold_relevant
from xauusd.monitoring.logging import get_logger

log = get_logger(__name__)

DEFAULT_FEEDS: dict[str, str] = {
    "reuters_markets": "https://www.reutersagency.com/feed/?best-topics=business-finance",
    "cnbc_world": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    "investing_commodities": "https://www.investing.com/rss/commodities.rss",
    "kitco": "https://www.kitco.com/rss/KitcoNews.xml",
    "fed_press": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ecb_press": "https://www.ecb.europa.eu/rss/press.html",
}

_TAG = re.compile(r"<[^>]+>")


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return _TAG.sub("", el.text).strip()


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_feed(xml_text: str, source: str) -> list[NewsItem]:
    """Parse RSS 2.0 or Atom. Malformed feeds yield nothing rather than raising."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("feed_parse_failed", source=source, error=str(exc))
        return []

    items: list[NewsItem] = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = _text(node.find("title")) or _text(node.find("{http://www.w3.org/2005/Atom}title"))
        if not title:
            continue
        desc = (
            _text(node.find("description"))
            or _text(node.find("summary"))
            or _text(node.find("{http://www.w3.org/2005/Atom}summary"))
        )
        date_raw = (
            _text(node.find("pubDate"))
            or _text(node.find("published"))
            or _text(node.find("updated"))
            or _text(node.find("{http://www.w3.org/2005/Atom}published"))
        )
        published = _parse_date(date_raw) or datetime.now(UTC)
        link_el = node.find("link")
        link = _text(link_el) or (link_el.get("href", "") if link_el is not None else "")
        items.append(NewsItem(published, title, source, desc[:2000], link))
    return items


class NewsFetcher:
    def __init__(self, feeds: dict[str, str] | None = None, timeout: float = 15.0) -> None:
        self.feeds = feeds or DEFAULT_FEEDS
        self.timeout = timeout

    def fetch_all(self, gold_only: bool = True) -> list[NewsItem]:
        import httpx

        out: list[NewsItem] = []
        for source, url in self.feeds.items():
            try:
                r = httpx.get(
                    url,
                    timeout=self.timeout,
                    headers={"User-Agent": "xauusd-bot/1.0"},
                    follow_redirects=True,
                )
                r.raise_for_status()
                items = parse_feed(r.text, source)
            except Exception as exc:
                log.warning("feed_fetch_failed", source=source, error=str(exc))
                continue
            out.extend(i for i in items if not gold_only or is_gold_relevant(i))
        # Deduplicate across feeds: the same story appears in several.
        seen: set[str] = set()
        deduped: list[NewsItem] = []
        for i in sorted(out, key=lambda x: x.published_ts, reverse=True):
            if i.content_hash in seen:
                continue
            seen.add(i.content_hash)
            deduped.append(i)
        return deduped
