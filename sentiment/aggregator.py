from typing import Optional

import config


def aggregate(
    vader_result: Optional[dict],
    finbert_result: Optional[dict],
    text_mode: str,
    panel_result: Optional[dict] = None,
) -> dict:
    """
    Combine VADER, FinBERT, and (optional) agent-panel results into a
    single final sentiment.

    Scoring rule: weighted average of available numeric scores using
    config.AGENT_PANEL_WEIGHTS (keys: vader, finbert, panel). Missing
    contributors are dropped and remaining weights re-normalized.

    Label rule:
      - Zero contributors → neutral / 0.0.
      - One contributor   → its label.
      - Multiple          → majority label; tie broken by, in order:
                              panel → finbert → vader (most domain-aware first).
    """
    sources: list[tuple[str, float, str, float]] = []  # (name, score, label, weight)
    weights = getattr(config, "AGENT_PANEL_WEIGHTS", {"vader": 0.5, "finbert": 0.5, "panel": 0.0})

    if vader_result:
        sources.append(("vader", float(vader_result["score"]),
                        vader_result["label"], float(weights.get("vader", 0.0))))
    if finbert_result:
        sources.append(("finbert", float(finbert_result["score"]),
                        finbert_result["label"], float(weights.get("finbert", 0.0))))
    if panel_result:
        sources.append(("panel", float(panel_result["score"]),
                        panel_result["label"], float(weights.get("panel", 0.0))))

    if not sources:
        return {
            "final_label":    "neutral",
            "final_score":    0.0,
            "text_mode_used": text_mode,
            "models_used":    [],
        }

    total_w = sum(w for *_, w in sources)
    if total_w <= 0:
        # All weights zero — fall back to unweighted mean
        final_score = round(sum(s for _, s, _, _ in sources) / len(sources), 4)
    else:
        final_score = round(sum(s * w for _, s, _, w in sources) / total_w, 4)

    if len(sources) == 1:
        final_label = sources[0][2]
    else:
        tally: dict[str, int] = {}
        for _, _, label, _ in sources:
            tally[label] = tally.get(label, 0) + 1
        max_count = max(tally.values())
        winners = {lbl for lbl, c in tally.items() if c == max_count}
        if len(winners) == 1:
            final_label = winners.pop()
        else:
            priority = {"panel": 0, "finbert": 1, "vader": 2}
            ranked = sorted(sources, key=lambda row: priority.get(row[0], 99))
            final_label = next((lbl for _, _, lbl, _ in ranked if lbl in winners), ranked[0][2])

    return {
        "final_label":    final_label,
        "final_score":    final_score,
        "text_mode_used": text_mode,
        "models_used":    [name for name, *_ in sources],
    }
