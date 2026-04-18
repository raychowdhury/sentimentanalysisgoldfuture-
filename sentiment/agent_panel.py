"""
Multi-persona LLM sentiment panel.

Scores a gold/XAUUSD-relevant article through several trader personas in a
single LLM call and returns a weighted aggregate. Each persona reads the
same text but from a different vantage point, so the aggregate captures
disagreement (e.g. macro-hawk sees hawkish Fed as bearish for gold while
safe-haven-bug still weighs geopolitical stress).

Persona scores are in [-1.0, +1.0] where positive = bullish for gold.

Design:
  • One Anthropic API call per article (all personas in one prompt).
  • Strict JSON response schema validated on parse.
  • Graceful degrade: missing SDK, missing key, or API error → returns
    None so the aggregator falls back to VADER/FinBERT only.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

import requests

import config
from utils.logger import setup_logger
from utils.text_cleaner import truncate_for_bert

logger = setup_logger(__name__)


_PERSONA_BRIEFS: dict[str, str] = {
    "macro_hawk":      "Hawkish macro economist. Focus on rates, inflation, Fed policy. Rising real yields = bearish gold.",
    "safe_haven_bug":  "Gold perma-bull. Weight geopolitical stress, war risk, currency debasement, crisis narratives.",
    "dollar_bull":     "USD strength trader. Strong dollar = bearish gold. Watches DXY, US economic surprise index.",
    "technical_bull":  "Chartist. Trend-following. Price action, breakouts, momentum — ignore narrative unless confirmed by tape.",
    "quant_bear":      "Skeptical quant. Mean-reversion bias. Treats positive news at highs as fade opportunity.",
}


_SCHEMA_HINT = (
    '{"scores": {'
    + ", ".join(f'"{p}": <-1.0..1.0>' for p in _PERSONA_BRIEFS)
    + '}, "rationale": "<1 sentence>"}'
)


def _build_prompt(title: str, body: str) -> str:
    personas_block = "\n".join(f"- {k}: {v}" for k, v in _PERSONA_BRIEFS.items())
    return (
        "You are a panel of trader personas scoring a gold-market news item.\n"
        "For each persona, output a sentiment score in [-1.0, +1.0] where "
        "positive = bullish for gold, negative = bearish, 0 = neutral.\n\n"
        f"PERSONAS:\n{personas_block}\n\n"
        f"HEADLINE: {title}\n"
        f"BODY: {body}\n\n"
        "Respond with ONLY a JSON object matching this schema — no prose, no code fences:\n"
        f"{_SCHEMA_HINT}\n"
    )


def _parse_response(raw: str) -> Optional[dict]:
    """Extract JSON object from model response, tolerant of code fences."""
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        logger.warning(f"Agent panel: JSON parse failed: {e}")
        return None
    scores = data.get("scores")
    if not isinstance(scores, dict):
        return None
    cleaned: dict[str, float] = {}
    for persona in _PERSONA_BRIEFS:
        v = scores.get(persona)
        if isinstance(v, (int, float)):
            cleaned[persona] = max(-1.0, min(1.0, float(v)))
    if not cleaned:
        return None
    return {"scores": cleaned, "rationale": str(data.get("rationale", ""))[:300]}


class AgentPanel:
    """
    Holds the Anthropic client and invokes the persona panel.

    Degrades to no-op when the SDK is missing or no API key is set. Callers
    check `ready` before invoking `analyze` (or rely on `analyze` returning
    None).
    """

    def __init__(self) -> None:
        self.ready = False
        self._client = None
        self._backend = getattr(config, "AGENT_PANEL_BACKEND", "anthropic")
        self._max_tokens = 400

        if not getattr(config, "AGENT_PANEL_ENABLED", False):
            return

        if self._backend == "anthropic":
            self._init_anthropic()
        elif self._backend == "ollama":
            self._init_ollama()
        else:
            logger.warning(f"Agent panel: unknown backend '{self._backend}'")

    def _init_anthropic(self) -> None:
        self._model = getattr(config, "AGENT_PANEL_MODEL", "claude-haiku-4-5-20251001")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("Agent panel (anthropic): ANTHROPIC_API_KEY not set")
            return
        try:
            from anthropic import Anthropic
            self._client = Anthropic(api_key=api_key)
            self.ready = True
            logger.info(f"Agent panel ready — anthropic/{self._model}")
        except ImportError:
            logger.warning("Agent panel (anthropic): `anthropic` package not installed")
        except Exception as e:
            logger.warning(f"Agent panel (anthropic) init failed: {e}")

    def _init_ollama(self) -> None:
        self._model = getattr(config, "OLLAMA_MODEL", "qwen2.5:3b")
        self._ollama_host = getattr(config, "OLLAMA_HOST", "http://localhost:11434")
        try:
            r = requests.get(f"{self._ollama_host}/api/tags", timeout=3)
            r.raise_for_status()
            tags = [m["name"] for m in r.json().get("models", [])]
        except Exception as e:
            logger.warning(f"Agent panel (ollama): cannot reach {self._ollama_host} — {e}")
            return
        if self._model not in tags:
            logger.warning(
                f"Agent panel (ollama): model '{self._model}' not pulled. "
                f"Available: {tags}. Run `ollama pull {self._model}`."
            )
            return
        self.ready = True
        logger.info(f"Agent panel ready — ollama/{self._model}")

    def _call_anthropic(self, prompt: str) -> Optional[str]:
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text if resp.content else ""
        except Exception as e:
            logger.warning(f"Agent panel (anthropic) API error: {e}")
            return None

    def _call_ollama(self, prompt: str) -> Optional[str]:
        try:
            r = requests.post(
                f"{self._ollama_host}/api/chat",
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": self._max_tokens},
                },
                timeout=60,
            )
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"Agent panel (ollama) API error: {e}")
            return None

    def analyze(self, title: str, body: str) -> Optional[dict]:
        """
        Run the panel on one article. Returns:
            {
              "score": float,            # weighted aggregate, -1..+1
              "label": "positive|negative|neutral",
              "scores": {persona: float, ...},
              "rationale": str,
              "model": "agent_panel",
            }
        or None when the panel is unavailable / errored.
        """
        if not self.ready:
            return None

        text_body = truncate_for_bert(body or "", config.MAX_TEXT_CHARS)
        prompt = _build_prompt(title or "", text_body)

        if self._backend == "anthropic":
            raw_text = self._call_anthropic(prompt)
        elif self._backend == "ollama":
            raw_text = self._call_ollama(prompt)
        else:
            raw_text = None
        if raw_text is None:
            return None

        parsed = _parse_response(raw_text)
        if not parsed:
            logger.warning("Agent panel: response did not parse — skipping")
            return None

        weights = getattr(config, "AGENT_PERSONA_WEIGHTS", {})
        total_w = 0.0
        weighted_sum = 0.0
        for persona, score in parsed["scores"].items():
            w = float(weights.get(persona, 1.0))
            weighted_sum += score * w
            total_w += w
        agg_score = round(weighted_sum / total_w, 4) if total_w else 0.0

        # Disagreement = population variance of persona scores. Scores are in
        # [-1, +1] so variance is in [0, 1]. High variance means the personas
        # pulled in opposite directions (e.g. safe_haven_bug bullish while
        # dollar_bull bearish), signalling a contested narrative. Consumed by
        # signals/confidence.py to downgrade borderline signals.
        values = list(parsed["scores"].values())
        if len(values) >= 2:
            mean_v = sum(values) / len(values)
            variance = sum((v - mean_v) ** 2 for v in values) / len(values)
        else:
            variance = 0.0

        pos = getattr(config, "PANEL_POS_THRESHOLD", 0.15)
        neg = getattr(config, "PANEL_NEG_THRESHOLD", -0.15)
        if agg_score >= pos:
            label = "positive"
        elif agg_score <= neg:
            label = "negative"
        else:
            label = "neutral"

        return {
            "score":     agg_score,
            "label":     label,
            "scores":    parsed["scores"],
            "variance":  round(variance, 4),
            "rationale": parsed["rationale"],
            "model":     "agent_panel",
        }
