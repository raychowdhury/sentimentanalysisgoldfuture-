import re

import feedparser
from urllib.parse import quote_plus

from utils.logger import setup_logger

logger = setup_logger(__name__)

_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)

# Some publishers (MarketWatch/Dow Jones) reject default python-urllib UA.
# Request with a browser UA via feedparser's request_headers.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_REQUEST_HEADERS = {"User-Agent": _BROWSER_UA}


def fetch_articles(queries: list[str], max_per_query: int = 10) -> list[dict]:
    """
    Fetch articles from Google News RSS for each search query.
    Returns a flat list of article dicts with title, url, published, source, query.
    """
    articles: list[dict] = []
    for query in queries:
        url = _GOOGLE_NEWS_RSS.format(query=quote_plus(query))
        logger.info(f"Fetching RSS — query: '{query}'")
        try:
            feed = feedparser.parse(url, request_headers=_REQUEST_HEADERS)
            entries = feed.entries[:max_per_query]
            for entry in entries:
                articles.append({
                    "title": entry.get("title", "").strip(),
                    "url": entry.get("link", "").strip(),
                    "published": entry.get("published", ""),
                    "source": _extract_source(entry),
                    "query": query,
                })
            logger.info(f"  → {len(entries)} article(s) retrieved")
        except Exception as e:
            logger.warning(f"RSS fetch failed for '{query}': {e}")
    return articles


def fetch_feeds(
    feeds: list[dict],
    max_per_feed: int = 40,
    filter_keywords: list[str] | None = None,
) -> list[dict]:
    """
    Fetch articles from a list of direct RSS feed URLs.

    Each feed dict: {name, url, filter: bool}. When `filter` is True, entries
    are kept only if their title matches any keyword from filter_keywords
    (case-insensitive, word-boundary match — "war" matches "war" not "awarded").
    """
    articles: list[dict] = []
    compiled = _compile_keywords(filter_keywords or [])

    for feed_cfg in feeds:
        name = feed_cfg.get("name", "")
        url  = feed_cfg.get("url", "")
        apply_filter = bool(feed_cfg.get("filter", False))
        if not url:
            continue

        logger.info(f"Fetching RSS feed — {name}")
        try:
            feed = feedparser.parse(url, request_headers=_REQUEST_HEADERS)
            entries = feed.entries[:max_per_feed]
            kept = 0
            for entry in entries:
                title = entry.get("title", "").strip()
                if apply_filter and not _matches_keywords(title, compiled):
                    continue
                articles.append({
                    "title": title,
                    "url": entry.get("link", "").strip(),
                    "published": entry.get("published", "") or entry.get("updated", ""),
                    "source": name or _extract_source(entry),
                    "query": f"feed:{name}",
                })
                kept += 1
            logger.info(f"  → {kept}/{len(entries)} kept after filter")
        except Exception as e:
            logger.warning(f"Feed fetch failed for '{name}': {e}")

    return articles


def _compile_keywords(keywords: list[str]) -> list[re.Pattern]:
    """
    Build one case-insensitive word-boundary regex per keyword. `\\b` anchors
    on word-char transitions so substrings inside larger words don't match.
    """
    return [re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in keywords if k]


def _matches_keywords(text: str, patterns: list[re.Pattern]) -> bool:
    if not patterns:
        return True
    return any(p.search(text) for p in patterns)


def _extract_source(entry: dict) -> str:
    """Extract publisher name from a feedparser entry."""
    source = entry.get("source", {})
    if isinstance(source, dict) and source.get("title"):
        return source["title"]
    tags = entry.get("tags", [])
    if tags and isinstance(tags[0], dict):
        return tags[0].get("term", "")
    return entry.get("author", "")
