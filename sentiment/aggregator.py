from typing import Optional


def aggregate(
    vader_result: Optional[dict],
    finbert_result: Optional[dict],
    text_mode: str,
) -> dict:
    """
    Combine VADER and FinBERT results into a single final sentiment.

    Final label rule (transparent and deterministic):
    - If only one model is available → use its label directly.
    - If both models agree on label → use that label.
    - If they disagree → use FinBERT's label (finance-domain model preferred).
    - Final score = average of all available numeric scores.

    Note: This is a sentiment analysis pipeline, not a trading signal or forecast.
    """
    models_used: list[str] = []
    scores: list[float] = []
    labels: list[str] = []

    if vader_result:
        models_used.append("vader")
        scores.append(vader_result["score"])
        labels.append(vader_result["label"])

    if finbert_result:
        models_used.append("finbert")
        scores.append(finbert_result["score"])
        labels.append(finbert_result["label"])

    if not scores:
        return {
            "final_label": "neutral",
            "final_score": 0.0,
            "text_mode_used": text_mode,
            "models_used": [],
        }

    final_score = round(sum(scores) / len(scores), 4)

    if len(labels) == 1:
        final_label = labels[0]
    elif labels[0] == labels[1]:
        # Both models agree
        final_label = labels[0]
    else:
        # Disagree: prefer FinBERT (finance-trained)
        final_label = finbert_result["label"]  # type: ignore[index]

    return {
        "final_label": final_label,
        "final_score": final_score,
        "text_mode_used": text_mode,
        "models_used": models_used,
    }
