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


def scrape_article(url: str, timeout: int = 10, retries: int = 2) -> dict:
    """
    Fetch and extract readable paragraph text from an article URL.

    Returns:
        dict with keys:
            - body (str): extracted text, empty string on failure
            - extraction_success (bool): whether body text was retrieved
    """
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            body = _extract_body(response.text)
            if body:
                return {"body": body, "extraction_success": True}
            logger.debug(f"No paragraph text found at {url}")
            return {"body": "", "extraction_success": False}

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout on attempt {attempt + 1} for {url}")

        except requests.exceptions.HTTPError as e:
            logger.warning(f"HTTP {e.response.status_code} for {url} — not retrying")
            break  # 4xx/5xx: retrying won't help

        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error on attempt {attempt + 1} for {url}: {e}")

        except Exception as e:
            logger.warning(f"Unexpected scrape error for {url}: {e}")
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
