from config import FINBERT_MODEL_NAME, MAX_TEXT_CHARS
from utils.logger import setup_logger
from utils.text_cleaner import truncate_for_bert

logger = setup_logger(__name__)

# Maps FinBERT output labels to normalized labels
_LABEL_MAP = {
    "positive": "positive",
    "negative": "negative",
    "neutral": "neutral",
}


class FinBERTAnalyzer:
    """
    Finance-specific sentiment analysis using ProsusAI/finbert.

    The HuggingFace pipeline is loaded once at initialization.
    If loading fails (e.g. no internet, missing torch), the analyzer
    degrades gracefully by returning neutral with zero confidence.
    """

    def __init__(self, batch_size: int = 32) -> None:
        self._ready = False
        self._batch_size = batch_size
        try:
            import torch
            from transformers import pipeline as hf_pipeline
            # Prefer Apple MPS → CUDA → CPU. MPS gives ~3-5× on M1/M2.
            if torch.backends.mps.is_available():
                device = "mps"
            elif torch.cuda.is_available():
                device = 0
            else:
                device = -1
            self._pipe = hf_pipeline(
                "text-classification",
                model=FINBERT_MODEL_NAME,
                tokenizer=FINBERT_MODEL_NAME,
                truncation=True,
                max_length=512,
                device=device,
                batch_size=batch_size,
            )
            self._ready = True
            logger.info(f"FinBERT loaded (device={device}, batch={batch_size}).")
        except Exception as e:
            logger.warning(
                f"FinBERT could not be loaded — will return neutral for all articles. "
                f"Reason: {e}"
            )

    def analyze(self, text: str) -> dict:
        """
        Analyze text with FinBERT.

        Returns:
            dict with keys: score (float), label (str), confidence (float), model (str)
            score is signed: +confidence for positive, -confidence for negative, 0 for neutral.
        """
        _empty = {"score": 0.0, "label": "neutral", "confidence": 0.0, "model": "finbert"}

        if not self._ready or not text or not text.strip():
            return _empty

        text = truncate_for_bert(text, MAX_TEXT_CHARS)
        try:
            result = self._pipe(text)[0]
            raw_label = result["label"].lower()
            label = _LABEL_MAP.get(raw_label, "neutral")
            confidence = round(float(result["score"]), 4)

            # Convert to signed score for consistent aggregation.
            # Calibrate confidence via (2c - 1) so it spans [0, 1] starting from
            # the 50% decision boundary instead of the 33% softmax floor. Without
            # this, FinBERT's winning-class probability almost always sits in
            # [0.85, 0.99] and dominates the weighted mean against VADER's
            # graded compound score. Clamped at 0 to avoid flipping sign when
            # confidence < 0.5.
            calibrated = max(0.0, 2 * confidence - 1)
            if label == "positive":
                score = round(calibrated, 4)
            elif label == "negative":
                score = -round(calibrated, 4)
            else:
                score = 0.0

            return {"score": score, "label": label, "confidence": confidence, "model": "finbert"}

        except Exception as e:
            logger.warning(f"FinBERT inference error: {e}")
            return _empty

    def analyze_batch(self, texts: list[str]) -> list[dict]:
        """Vectorised version of analyze(). One forward pass per batch_size items.
        Returns one result dict per input text (same order). Empties yield neutral."""
        _empty = {"score": 0.0, "label": "neutral", "confidence": 0.0, "model": "finbert"}
        if not self._ready or not texts:
            return [_empty for _ in texts]

        truncated = [
            truncate_for_bert(t, MAX_TEXT_CHARS) if (t and t.strip()) else ""
            for t in texts
        ]
        idx_nonempty = [i for i, t in enumerate(truncated) if t]
        nonempty = [truncated[i] for i in idx_nonempty]

        out: list[dict] = [dict(_empty) for _ in texts]
        if not nonempty:
            return out

        try:
            raw = self._pipe(nonempty, batch_size=self._batch_size)
        except Exception as e:
            logger.warning(f"FinBERT batch inference error: {e}")
            return out

        for j, result in zip(idx_nonempty, raw):
            raw_label = result["label"].lower()
            label = _LABEL_MAP.get(raw_label, "neutral")
            confidence = round(float(result["score"]), 4)
            calibrated = max(0.0, 2 * confidence - 1)
            if label == "positive":
                score = round(calibrated, 4)
            elif label == "negative":
                score = -round(calibrated, 4)
            else:
                score = 0.0
            out[j] = {"score": score, "label": label, "confidence": confidence, "model": "finbert"}
        return out
