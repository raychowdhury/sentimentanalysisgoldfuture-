import time

import requests
from bs4 import BeautifulSoup

from utils.logger import setup_logger
from utils.text_cleaner import clean_text

logger = setup_logger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _resolve_google_news_url(url: str) -> str:
    """
    Google News RSS returns wrapped URLs of the form
    `https://news.google.com/rss/articles/<b64>`. Hitting them directly
    returns a Google interstitial, not the publisher page. The real target
    is encoded in the path segment and must be decoded via the Google News
    decoder endpoint.

    Returns the decoded publisher URL on success, or the original URL on
    any failure (so callers can still attempt a direct fetch).
    """
    if "news.google.com/rss/articles/" not in url:
        return url
    try:
        from googlenewsdecoder import gnewsdecoder
        result = gnewsdecoder(url, interval=1)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
        logger.debug(f"gnewsdecoder failed: {result.get('message', 'unknown')}")
    except ImportError:
        logger.warning("googlenewsdecoder not installed — skipping URL resolution")
    except Exception as e:
        logger.debug(f"URL resolve error for {url[:80]}: {e}")
    return url


def scrape_article(url: str, timeout: int = 10, retries: int = 2) -> dict:
    """
    Fetch and extract readable paragraph text from an article URL.

    Returns:
        dict with keys:
            - body (str): extracted text, empty string on failure
            - extraction_success (bool): whether body text was retrieved
    """
    target_url = _resolve_google_news_url(url)
    if target_url != url:
        logger.debug(f"Resolved GNews URL → {target_url[:100]}")

    for attempt in range(retries + 1):
        try:
            response = requests.get(target_url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            body = _extract_body(response.text)
            if body:
                return {"body": body, "extraction_success": True}
            logger.debug(f"No paragraph text found at {target_url}")
            return {"body": "", "extraction_success": False}

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout on attempt {attempt + 1} for {target_url}")

        except requests.exceptions.HTTPError as e:
            logger.warning(f"HTTP {e.response.status_code} for {target_url} — not retrying")
            break  # 4xx/5xx: retrying won't help

        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error on attempt {attempt + 1} for {target_url}: {e}")

        except Exception as e:
            logger.warning(f"Unexpected scrape error for {target_url}: {e}")
            break

        if attempt < retries:
            time.sleep(1.5 ** attempt)  # 1.0s, 1.5s backoff

    return {"body": "", "extraction_success": False}


def _extract_body(html: str) -> str:
    """Extract and clean paragraph text from raw HTML."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove non-content elements
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    paragraphs = soup.find_all("p")
    raw = " ".join(p.get_text() for p in paragraphs)
    return clean_text(raw)
