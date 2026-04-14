import feedparser
from urllib.parse import quote_plus

from utils.logger import setup_logger

logger = setup_logger(__name__)

_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)


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
            feed = feedparser.parse(url)
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


def _extract_source(entry: dict) -> str:
    """Extract publisher name from a feedparser entry."""
    source = entry.get("source", {})
    if isinstance(source, dict) and source.get("title"):
        return source["title"]
    tags = entry.get("tags", [])
    if tags and isinstance(tags[0], dict):
        return tags[0].get("term", "")
    return entry.get("author", "")
