import re

from utils.logger import setup_logger

logger = setup_logger(__name__)


def deduplicate(articles: list[dict]) -> list[dict]:
    """
    Remove duplicate articles using two passes:
    1. Exact URL match
    2. Normalized title key (lowercase, punctuation stripped)
    """
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[dict] = []

    for article in articles:
        url = article.get("url", "").strip()
        title_key = _normalize_title(article.get("title", ""))

        if url and url in seen_urls:
            continue
        if title_key and title_key in seen_titles:
            continue

        if url:
            seen_urls.add(url)
        if title_key:
            seen_titles.add(title_key)

        # Store the key so callers can include it in output
        article["dedup_key"] = title_key
        unique.append(article)

    removed = len(articles) - len(unique)
    if removed:
        logger.info(f"Deduplication: removed {removed} duplicate(s), {len(unique)} unique articles remain.")

    return unique


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    title = title.lower()
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title
