import re


def clean_text(text: str) -> str:
    """Remove junk whitespace and non-printable characters from text."""
    if not text:
        return ""
    # Strip non-printable characters except common whitespace
    text = re.sub(r"[^\x09\x0A\x0D\x20-\x7E\xA0-\uFFFF]", " ", text)
    # Collapse all whitespace (tabs, newlines, multiple spaces) to a single space
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def truncate_for_bert(text: str, max_chars: int) -> str:
    """
    Truncate text to at most max_chars characters, breaking at a word boundary.
    Used to stay within FinBERT's 512-token limit.
    """
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    # Break at word boundary only if it's not too far back
    if last_space > max_chars // 2:
        return truncated[:last_space]
    return truncated
