# config.py — Central configuration for NewsSentimentScanner + Gold Bias Engine

# ── RSS / News ────────────────────────────────────────────────────────────────
RSS_QUERIES: list[str] = [
    "gold price",
    "gold market",
    "XAUUSD",
    "precious metals",
    "gold forecast",
    "gold investment",
    "gold",
]

MAX_PER_QUERY: int = 10
MAX_ARTICLES: int  = 50

# ── Scraping ──────────────────────────────────────────────────────────────────
SCRAPE_TIMEOUT: int  = 10
SCRAPE_RETRIES: int  = 2
SCRAPE_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── Sentiment ─────────────────────────────────────────────────────────────────
DEFAULT_TEXT_MODE: str   = "combined"
DEFAULT_MODELS: list[str] = ["vader", "finbert"]
FINBERT_MODEL_NAME: str  = "ProsusAI/finbert"
MAX_TEXT_CHARS: int      = 1800
VADER_THRESHOLDS: dict[str, float] = {"positive": 0.05, "negative": -0.05}

# ── Output ────────────────────────────────────────────────────────────────────
OUTPUT_DIR: str = "outputs"

# ── Market Data ───────────────────────────────────────────────────────────────
# Symbols used with yfinance
MARKET_SYMBOLS: dict[str, str] = {
    "gold":      "GC=F",       # Gold Futures (COMEX)
    "dxy":       "DX-Y.NYB",   # US Dollar Index
    "yield_10y": "^TNX",       # US 10-Year Treasury Yield
}
MARKET_LOOKBACK_DAYS: int  = 90   # fetch 90 days → ~63 trading days (enough for EMA50)
RETURN_WINDOW_DAYS: int    = 5    # n-day return for trend detection
EMA_SHORT: int             = 20
EMA_LONG: int              = 50

# ── Trend Scoring Thresholds ──────────────────────────────────────────────────
DXY_STRONG_MOVE_PCT: float  = 1.0    # % change over RETURN_WINDOW_DAYS = "strong"
DXY_MILD_MOVE_PCT: float    = 0.3
YIELD_STRONG_MOVE: float    = 0.15   # absolute change in yield (ppt) = "strong"
YIELD_MILD_MOVE: float      = 0.05
GOLD_STRONG_MOVE_PCT: float = 1.5
GOLD_MILD_MOVE_PCT: float   = 0.5

# ── Trade Setup ───────────────────────────────────────────────────────────────
MIN_RR: float          = 2.0    # Minimum risk-reward ratio (1:2)
STOP_BUFFER_PCT: float = 0.005  # 0.5% buffer beyond the invalidation level
MIN_RISK_PCT: float    = 0.003  # Minimum risk = 0.3% of entry price

# ── Timeframe Profiles ────────────────────────────────────────────────────────
# Each profile overrides market / scoring / trade-setup parameters for that
# trading style.  Pass the resolved dict through the pipeline instead of
# reading individual config constants.

TIMEFRAME_PROFILES: dict[str, dict] = {
    # Swing / position trading — unchanged from original single-timeframe defaults
    "swing": {
        "lookback_days":       90,
        "ema_short":           20,
        "ema_long":            50,
        "return_window":        5,
        "high_low_window":     14,
        "dxy_strong_pct":      DXY_STRONG_MOVE_PCT,
        "dxy_mild_pct":        DXY_MILD_MOVE_PCT,
        "yield_strong":        YIELD_STRONG_MOVE,
        "yield_mild":          YIELD_MILD_MOVE,
        "gold_strong_pct":     GOLD_STRONG_MOVE_PCT,
        "gold_mild_pct":       GOLD_MILD_MOVE_PCT,
        "min_rr":              MIN_RR,
        "stop_buffer_pct":     STOP_BUFFER_PCT,
    },
    # Day trading — tighter EMAs, 1-day momentum, shorter high/low window,
    # lower move thresholds, 1:1.5 minimum RR, tighter stop buffer
    "day": {
        "lookback_days":       30,
        "ema_short":            5,
        "ema_long":            13,
        "return_window":        1,
        "high_low_window":      5,
        "dxy_strong_pct":       0.4,
        "dxy_mild_pct":         0.15,
        "yield_strong":         0.05,
        "yield_mild":           0.02,
        "gold_strong_pct":      0.5,
        "gold_mild_pct":        0.15,
        "min_rr":               1.5,
        "stop_buffer_pct":      0.002,
    },
}
