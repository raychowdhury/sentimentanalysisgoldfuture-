from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from config import VADER_THRESHOLDS

# Loaded once at module import — no per-article re-initialization
_analyzer = SentimentIntensityAnalyzer()


def analyze(text: str) -> dict:
    """
    Run VADER sentiment analysis on the given text.

    Returns:
        dict with keys: score (float), label (str), model (str)
    """
    if not text or not text.strip():
        return {"score": 0.0, "label": "neutral", "model": "vader"}

    compound = _analyzer.polarity_scores(text)["compound"]

    if compound >= VADER_THRESHOLDS["positive"]:
        label = "positive"
    elif compound <= VADER_THRESHOLDS["negative"]:
        label = "negative"
    else:
        label = "neutral"

    return {"score": compound, "label": label, "model": "vader"}
