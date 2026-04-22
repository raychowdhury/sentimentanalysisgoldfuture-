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

# Direct RSS feeds — used alongside Google News keyword search. Each entry is
# {name, url, filter}. When `filter` is True, items are kept only if their title
# matches GOLD_FILTER_KEYWORDS (firehose feeds covering all asset classes). When
# False, all items pass (feed is already gold/metals-scoped).
RSS_FEEDS: list[dict] = [
    # FinancialJuice squawk firehose — broad macro; filter to gold-relevant.
    {"name": "FinancialJuice", "url": "https://www.financialjuice.com/feed.ashx?xy=rss", "filter": True},
    # MarketWatch top stories + breaking pulse — broad market news; filter.
    {"name": "MarketWatch Top", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "filter": True},
    {"name": "MarketWatch Pulse", "url": "https://feeds.marketwatch.com/marketwatch/marketpulse/", "filter": True},
]
MAX_PER_FEED: int = 40

# Case-insensitive word-boundary match against title. Firehose feeds are
# filtered against this to keep only items that actually move gold futures.
# Covers Tier 1 (primary: Fed, yields, dollar, inflation) + Tier 2 (secondary:
# jobs, central banks, geopolitical, fiscal). Matching uses \b boundaries so
# "war" matches "war" but not "awarded"; "rate" matches "rate" but not "separate".
GOLD_FILTER_KEYWORDS: list[str] = [
    # ── Direct metals ────────────────────────────────────────────────────
    "gold", "xau", "xauusd", "bullion", "precious metal", "comex",
    "silver", "platinum", "palladium",
    # ── Fed / US monetary policy ─────────────────────────────────────────
    "fed", "fomc", "powell", "federal reserve", "fed chair", "fed funds",
    "fed minutes", "rate cut", "rate hike", "rate decision", "dot plot",
    "jackson hole", "hawkish", "dovish", "pivot", "tightening", "easing",
    "quantitative",
    # ── Yields / bonds ───────────────────────────────────────────────────
    "treasury yield", "10-year yield", "10y yield", "2-year yield",
    "real yield", "tips yield", "breakeven", "yield curve", "bond market",
    # ── US dollar ────────────────────────────────────────────────────────
    "dxy", "dollar index", "us dollar", "greenback", "dollar strength",
    "dollar weakness", "dollar rally", "dollar slide",
    # ── Inflation / prices ───────────────────────────────────────────────
    "cpi", "core cpi", "ppi", "pce", "core pce", "inflation", "deflation",
    "disinflation", "price index", "consumer prices",
    # ── Jobs / growth ────────────────────────────────────────────────────
    "nfp", "non-farm", "nonfarm", "payroll", "jobless", "unemployment",
    "jolts", "ism", "pmi", "gdp", "retail sales", "consumer confidence",
    # ── Central banks ────────────────────────────────────────────────────
    "ecb", "boe", "boj", "pboc", "rba", "snb", "central bank",
    "bullion reserves", "gold reserves", "world gold council",
    # ── Geopolitical / safe-haven catalysts ──────────────────────────────
    "war", "conflict", "sanction", "invasion", "missile", "ceasefire",
    "iran", "ukraine", "russia", "israel", "gaza", "hormuz", "taiwan",
    "north korea", "nuclear", "embargo", "tariff", "trade war",
    # ── Fiscal / credit ──────────────────────────────────────────────────
    "debt ceiling", "downgrade", "credit rating", "deficit",
    "treasury auction", "bond auction",
]

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

# ── Agent Panel (LLM multi-persona sentiment) ────────────────────────────────
# Opt-in: requires ANTHROPIC_API_KEY env var and `anthropic` package installed.
# Runs once per article, scoring through 5 trader personas in a single LLM
# call, then aggregates with VADER + FinBERT in sentiment/aggregator.py.
# Disabled by default because it adds latency + API cost per pipeline run.
AGENT_PANEL_ENABLED: bool = True
# Backend: "anthropic" (paid API) or "ollama" (local, free).
AGENT_PANEL_BACKEND: str  = "ollama"
AGENT_PANEL_MODEL: str    = "claude-haiku-4-5-20251001"  # used when backend="anthropic"

# Ollama settings (backend="ollama"). Host assumes `ollama serve` on default port.
OLLAMA_MODEL: str = "qwen2.5:3b"
OLLAMA_HOST:  str = "http://localhost:11434"

# Contribution of each source to the final article sentiment score.
# Re-normalized over only the sources actually present in any given run.
AGENT_PANEL_WEIGHTS: dict[str, float] = {
    "vader":   0.2,
    "finbert": 0.4,
    "panel":   0.4,
}

# Per-persona weights inside the panel aggregate.
AGENT_PERSONA_WEIGHTS: dict[str, float] = {
    "macro_hawk":     1.0,
    "safe_haven_bug": 0.8,
    "dollar_bull":    1.0,
    "technical_bull": 0.6,
    "quant_bear":     0.8,
}

# Thresholds that bucket the panel's aggregate score into a label.
PANEL_POS_THRESHOLD: float =  0.15
PANEL_NEG_THRESHOLD: float = -0.15

# Panel persona-disagreement threshold (population variance across 5 personas
# on the same article, averaged per run). Variance range 0..1. At >=0.35 the
# personas are meaningfully split — signals/confidence.py downgrades one
# level to reflect a contested narrative.
PANEL_DISAGREEMENT_HIGH: float = 0.35

# Per-article pipeline worker count. Each worker processes one article
# through scrape → VADER → FinBERT → panel. Ollama handles concurrent
# requests; Google News URL decoder and scraping are I/O-bound so threads
# help. FinBERT inference serializes through the GIL but its share of
# per-article time is small. Tune down if Ollama gets overloaded.
PIPELINE_WORKERS: int = 6

# ── Signal Extraction Weighting (Pillar 1) ───────────────────────────────────
# Per-article weight = relevance × source_tier × time_decay. Replaces the plain
# mean used previously so fresh, on-topic, high-quality sources dominate the
# aggregate sentiment score and irrelevant / stale / low-tier items are damped
# instead of dropped.
#
# SOURCE_TIERS: substring → weight. First match wins. Unknown sources get
# SOURCE_TIER_DEFAULT. Keys are lowercase; matched against the article's
# `source` field (Google News sets publisher, direct feeds set feed name).
SOURCE_TIERS: dict[str, float] = {
    # Tier 1 — premium newswires / institutional
    "reuters":              1.0,
    "bloomberg":            1.0,
    "wall street journal":  1.0,
    "wsj":                  1.0,
    "financial times":      1.0,
    "ft.com":               1.0,
    "dow jones":            1.0,
    # Tier 2 — strong financial / commodity specialists
    "marketwatch":          0.8,
    "cnbc":                 0.8,
    "kitco":                0.8,
    "fxstreet":             0.8,
    "mining.com":           0.8,
    "barron":               0.8,
    "financialjuice":       0.8,
    "financial juice":      0.8,
    "reuters commodities":  1.0,
    # Tier 3 — mainstream financial / aggregators with editorial
    "yahoo":                0.5,
    "fortune":              0.5,
    "investing.com":        0.5,
    "fxempire":             0.5,
    "forbes":               0.5,
    "seeking alpha":        0.5,
    "business insider":     0.5,
    # Tier 4 — retail-lean aggregators / blogs
    "benzinga":             0.3,
    "zacks":                0.3,
    "motley fool":          0.3,
}
SOURCE_TIER_DEFAULT: float = 0.4

# Time-decay τ in hours. weight_decay = exp(-age_hours / τ).
# Day-mode horizon is ~1 day; τ=12h means 24h-old news is already worth ~14%
# of fresh news. Swing holds span days; τ=48h keeps week-old catalysts in play
# at ~3%. Values validated against common 3-day half-life used in equity
# sentiment literature — gold reacts faster, so τ runs shorter.
SENTIMENT_TAU_HOURS: dict[str, float] = {
    "swing": 48.0,
    "day":   12.0,
}

# ── Output ────────────────────────────────────────────────────────────────────
OUTPUT_DIR: str = "outputs"

# ── Market Data ───────────────────────────────────────────────────────────────
# Symbols used with yfinance
MARKET_SYMBOLS: dict[str, str] = {
    "gold":      "GC=F",       # Gold Futures (COMEX)
    "dxy":       "DX-Y.NYB",   # US Dollar Index
    "vix":       "^VIX",       # CBOE Volatility Index
}

# FRED (Federal Reserve) series — fetched via public CSV endpoint, no key.
# DFII10 = 10Y TIPS real yield. Real yield is gold's true discount rate;
# nominal yield (^TNX / DGS10) mixes in inflation expectations and is a
# weaker factor. A FRED source with the same field name overrides any
# yfinance entry in MARKET_SYMBOLS.
FRED_SYMBOLS: dict[str, str] = {
    "yield_10y":         "DFII10",  # 10Y TIPS real yield (replaces ^TNX)
    "nominal_yield_10y": "DGS10",   # kept for a future breakeven-inflation factor
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

# ── ATR ───────────────────────────────────────────────────────────────────────
ATR_PERIOD: int = 14   # standard ATR lookback

# ── VWAP ─────────────────────────────────────────────────────────────────────
# % deviation from rolling VWAP to trigger score
VWAP_DEVIATION_STRONG: float = 1.0
VWAP_DEVIATION_MILD:   float = 0.3

# ── Volume Profile / TPO ─────────────────────────────────────────────────────
VP_BINS: int             = 50    # price histogram buckets
VP_VALUE_AREA_PCT: float = 0.70  # 70 % value area (standard Market Profile rule)

# VIX thresholds — level-based (not trend)
# High VIX = fear = safe-haven demand for gold (bullish)
# Low VIX  = complacency = risk-on (mildly bearish for gold)
VIX_FEAR_STRONG: float = 30.0   # VIX >= 30 → +2
VIX_FEAR_MILD:   float = 20.0   # VIX >= 20 → +1
VIX_CALM:        float = 15.0   # VIX <  15 → -1

# ── Trade Setup ───────────────────────────────────────────────────────────────
# MIN_RR raised 2.0 → 3.0 after two rounds of grid search (5yr swing):
# 3.0 produced +0.90 R expectancy vs +0.40 at 2.0 baseline. Higher bar =
# fewer trades but much larger average winner, since algo waits for value-area
# target to be fully reached.
MIN_RR: float          = 3.0    # Minimum risk-reward ratio (1:3)
STOP_BUFFER_PCT: float = 0.005  # 0.5% buffer beyond the invalidation level
MIN_RISK_PCT: float    = 0.003  # Minimum risk = 0.3% of entry price

# ── Trailing Stop (backtest-only for now) ─────────────────────────────────────
# Disabled by default. Grid showed trailing stop locks in small wins and
# strips TP potential — trail-off profiles dominate the top of the ranking
# (top 8 of 64 had trail off). Turning it on cuts expectancy roughly in half.
# Kept configurable for experimentation; do not enable without re-running the
# grid to confirm it still hurts.
TRAIL_ENABLED: bool       = False
TRAIL_ACTIVATE_R: float   = 1.0   # start trailing after +1R of progress
TRAIL_ATR_MULT: float     = 2.5   # distance from extreme, in ATR units

# ── Partial Take-Profit / Scale-Out (backtest-only for now) ───────────────────
# When enabled: once price reaches +PARTIAL_TP_R in favor, close a fraction of
# the position and move the stop on the remainder to breakeven (entry).
# Remainder continues to final TP. Idea: guarantee a positive R outcome on
# trades that reach 1.5R, while preserving upside on runners.
PARTIAL_TP_ENABLED: bool   = True
PARTIAL_TP_R: float        = 1.5   # trigger at +1.5R progress
PARTIAL_TP_FRACTION: float = 0.5   # close 50% at trigger, keep 50%

# ── Macro Gate ───────────────────────────────────────────────────────────────
# Long-only mode — block SELL / STRONG_SELL entirely.
# Backtest (5yr) showed short-side edge is broken: SELL expectancy ≈ 0 and
# bear-regime trades are net negative. Until short-side logic is reworked,
# default to long-only. Flip to False to re-enable shorts (e.g. for research).
LONG_ONLY: bool = True

# SMA200 regime filter — block BUY / STRONG_BUY when gold is below its own
# SMA200. Gold below SMA200 = macro downtrend; taking longs there is fighting
# the big trend. This is the single most effective regime filter in retail
# futures systems. Leave on by default.
SMA200_GATE: bool = True

# ── Score Weights ────────────────────────────────────────────────────────────
# Weight applied to each component score before summing to the total.
# 1.0 = pre-weight behavior. Calibrated from backtest:
#   • gold trend is dominant edge → keep high
#   • VWAP + VP were over-represented (current scoring fires on them alone) →
#     trim so trend has to agree for STRONG signals
#   • yield + dxy correlated → mild trim on yield to avoid double-counting
#   • sentiment unreliable (noisy news) → reduce
#   • VIX level useful but coarse → slight trim
# Re-run grid search after changing weights to re-tune thresholds.
SCORE_WEIGHTS: dict[str, float] = {
    "sentiment":      0.75,
    "dxy":            1.00,
    "yield":          0.80,
    "gold":           1.50,
    "vix":            0.75,
    "vwap":           0.75,
    "volume_profile": 0.75,
    # COT managed-money z-score — contrarian fade at weekly ±1σ+ extremes.
    # 5yr weight sweep (0 → 1.0) showed swing gets +2pp win-rate with near-flat
    # expectancy; day mode degrades monotonically (stale weekly data adds noise
    # to a daily signal). Keep low global weight; per-profile toggle disables
    # it entirely for day mode.
    "cot":            0.25,
}

# ── Event Gate ───────────────────────────────────────────────────────────────
# Block signals inside a window around high-impact US macro events (FOMC,
# CPI, NFP, PCE). Cuts whipsaw entries into scheduled announcements. Event
# dates live in events/calendar.py; refresh FOMC/CPI/PCE yearly from official
# schedules (NFP is derived from "first Friday of month" and needs no refresh).
EVENT_GATE_ENABLED: bool        = True
EVENT_BLACKOUT_DAYS_BEFORE: int = 1
EVENT_BLACKOUT_DAYS_AFTER: int  = 1
# "FF" includes high-impact events pulled live from the Faireconomy/ForexFactory
# export (covers BoE/ECB/BoJ + any USD events missing from the hardcoded list).
EVENT_BLACKOUT_TYPES: list[str] = ["FOMC", "CPI", "NFP", "PCE", "FF"]

# ── ForexFactory Live Calendar ───────────────────────────────────────────────
# Pulls the current week's high-impact events from the public Faireconomy JSON
# export (same data FF publishes, CDN-hosted, no auth, no Cloudflare). Adds
# live coverage of central-bank decisions outside the US and any USD events
# not yet in events/calendar.py. Cached to disk; refreshes every hour.
FF_CALENDAR_ENABLED: bool       = True
FF_CALENDAR_URL: str            = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_CALENDAR_CACHE: str          = "outputs/ff_calendar_cache.json"
FF_CALENDAR_TTL_SECONDS: int    = 3600
# Impact filter: only events at or above this rank trigger a blackout.
# "Low" | "Medium" | "High" (Holiday is ignored).
FF_IMPACT_MIN: str              = "High"
# Currency codes to include. Expand to ["USD", "EUR", "GBP", "JPY"] for
# multi-currency coverage. Keep USD-only until multi-currency scoring lands.
FF_COUNTRIES: list[str]         = ["USD"]

# ── Auto-Scheduler ───────────────────────────────────────────────────────────
# Interval (minutes) between automatic pipeline runs for each timeframe.
# Set SCHEDULER_ENABLED = True to start the scheduler when app.py launches.
SCHEDULER_ENABLED: bool        = False
SCHEDULER_TIMEFRAME: str       = "swing"   # "swing" or "day"
SCHEDULER_INTERVAL_SWING: int  = 120       # every 2 hours
SCHEDULER_INTERVAL_DAY: int    = 30        # every 30 minutes
# Pipeline defaults used by the scheduler (mirrors CLI defaults)
SCHEDULER_MODE: str            = "combined"
SCHEDULER_MODELS: list[str]    = ["vader", "finbert"]
SCHEDULER_LIMIT: int           = 50
SCHEDULER_TRADE_SETUP: bool    = True

# ── Timeframe Profiles ────────────────────────────────────────────────────────
# Each profile overrides market / scoring / trade-setup parameters for that
# trading style.  Pass the resolved dict through the pipeline instead of
# reading individual config constants.

TIMEFRAME_PROFILES: dict[str, dict] = {
    # Swing / position trading — tuned from grid search (5yr, 81 profiles)
    "swing": {
        "lookback_days":        90,
        "ema_short":            20,
        "ema_long":             50,
        "return_window":         5,
        "high_low_window":      14,
        "dxy_strong_pct":       DXY_STRONG_MOVE_PCT,
        "dxy_mild_pct":         DXY_MILD_MOVE_PCT,
        "yield_strong":         YIELD_STRONG_MOVE,
        "yield_mild":           YIELD_MILD_MOVE,
        "gold_strong_pct":      GOLD_STRONG_MOVE_PCT,
        "gold_mild_pct":        GOLD_MILD_MOVE_PCT,
        "min_rr":               MIN_RR,     # 3.0 post-grid
        "stop_buffer_pct":      STOP_BUFFER_PCT,
        # ATR stop: stop placed ATR_STOP_MULT × ATR below invalidation
        "atr_stop_mult":        1.0,
        # Max bars a swing trade stays open before forced time exit.
        # Grid showed 60 beat 40: longer holds let value-area targets hit.
        "max_hold":             60,
        # Event gate OFF for swing by default — 5yr A/B showed ±1 day blackout
        # removed 2 winners and clipped expectancy by ~0.06R. Swing trades hold
        # weeks, so event-day volatility rarely triggers the stop. Re-enable
        # after tuning a tighter swing-specific window.
        "event_gate":           False,
        # COT contrarian factor ON for swing — weekly CFTC cadence matches the
        # swing hold horizon. 5yr A/B at weight 0.25 showed +2pp win rate with
        # near-flat expectancy.
        "cot_enabled":          True,
    },
    # Day trading — tighter EMAs, 1-day momentum, shorter high/low window,
    # lower move thresholds, 1:1.5 minimum RR, tighter ATR stop
    "day": {
        "lookback_days":        30,
        "ema_short":             5,
        "ema_long":             13,
        "return_window":         1,
        "high_low_window":       5,
        "dxy_strong_pct":        0.4,
        "dxy_mild_pct":          0.15,
        "yield_strong":          0.05,
        "yield_mild":            0.02,
        "gold_strong_pct":       0.5,
        "gold_mild_pct":         0.15,
        "min_rr":                1.5,
        "stop_buffer_pct":       0.002,
        "atr_stop_mult":         0.5,   # tighter stop for day trade
        "max_hold":             10,     # ~2 trading weeks
        # Event gate ON for day mode — 5yr A/B: expectancy +50% (0.16 → 0.24 R),
        # max drawdown cut 65% (-15.1 → -5.3 R), trade count -22%. Event-day
        # gaps hit tight intraday stops hard; gating around FOMC/CPI/NFP/PCE
        # removes the worst of them.
        "event_gate":           True,
        # COT OFF for day mode — weekly release cadence is stale for a daily
        # signal. 5yr sweep showed monotonic drag on expectancy as weight grew.
        "cot_enabled":          False,
    },
}
