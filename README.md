# NewsSentimentScanner — Gold / XAUUSD Bias Engine

A local, rules-based decision-support system for gold market analysis.

> **This is not financial advice. It is not a trading predictor. It is a transparent
> sentiment and trend analysis pipeline that produces a directional bias and trade
> setup levels for informational purposes only.**

---

## What it does

1. Fetches gold/XAUUSD news from Google News RSS (7 built-in queries)
2. Scrapes article content with graceful fallback to title-only
3. Analyzes sentiment using **VADER** and/or **FinBERT**
4. Deduplicates articles by URL and normalized title
5. Fetches live market data for **Gold (GC=F)**, **DXY**, and **US 10Y Yield**
6. Scores each factor and combines them into a final **directional bias signal**
7. Applies **veto rules** to prevent contradictory signals
8. Computes **trade setup levels** (entry / stop / take-profit) with a minimum 1:2 RR check
9. Saves all output to `outputs/` as timestamped JSON and CSV
10. Runs a local Flask dashboard at `http://localhost:5001`

---

## Setup

**Requirements:** Python 3.10+

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> FinBERT (~440 MB) downloads automatically from HuggingFace on first run.
> Market data is fetched live from Yahoo Finance via `yfinance` (no API key needed).

---

## How to run

```bash
# Sentiment only (fast — no market data fetch)
python main.py

# Fast test — VADER only, 10 articles, title mode
python main.py --mode title --model vader --limit 10

# Full run: sentiment + market signal
python main.py --signal

# Full run + trade setup levels
python main.py --signal --trade-setup

# Combined mode, both models, signal + trade setup
python main.py --mode combined --model both --signal --trade-setup --limit 20

# Dashboard (shows saved output files)
python app.py
# → open http://localhost:5001
```

---

## CLI options

| Flag | Choices | Default | Description |
|------|---------|---------|-------------|
| `--mode` | `title`, `body`, `combined` | `combined` | Text input for sentiment |
| `--model` | `vader`, `finbert`, `both` | `both` | Sentiment model(s) |
| `--limit` | integer | `50` | Max articles per run |
| `--output-dir` | path | `outputs` | Output directory |
| `--signal` | flag | off | Fetch market data and compute bias signal |
| `--trade-setup` | flag | off | Compute entry/stop/take-profit levels |

---

## How the signal engine works

### Step 1 — Four scored factors

| Factor | Score range | Source |
|--------|-------------|--------|
| Sentiment | −2 to +2 | Average of article final scores |
| DXY trend | −2 to +2 | yfinance `DX-Y.NYB` (inverse: DXY up = negative for gold) |
| US 10Y yield | −2 to +2 | yfinance `^TNX` (inverse: yield up = negative for gold) |
| Gold trend | **−3 to +3** | yfinance `GC=F` (direct, dominant factor) |

**Sentiment mapping:**

| Average score | Component score |
|---------------|-----------------|
| > 0.15 | +2 |
| 0.05 to 0.15 | +1 |
| −0.05 to 0.05 | 0 |
| −0.15 to −0.05 | −1 |
| < −0.15 | −2 |

**DXY / Yield mapping** (both require EMA position AND momentum to agree):

| Condition | Score |
|-----------|-------|
| Above EMA20 + EMA50 and 5d move strong | −2 (headwind) |
| Above EMA20 and 5d move mild | −1 |
| Below EMA20 and 5d move mild | +1 |
| Below EMA20 + EMA50 and 5d move strong | +2 (tailwind) |
| Otherwise | 0 |

**Gold trend mapping** (dominant factor, wider range):

| Condition | Score |
|-----------|-------|
| Above EMA20 + EMA50 and 5d return > 1.5% | +3 |
| Above EMA20 and 5d return > 0.5% | +1 |
| Below EMA20 and 5d return < −0.5% | −1 |
| Below EMA20 + EMA50 and 5d return < −1.5% | −3 |
| Otherwise | 0 |

### Step 2 — Total score and signal

```
total = sentiment + dxy + yield + gold_trend
```

| Total score | Signal |
|-------------|--------|
| ≥ 4 | STRONG_BUY |
| 2 to 3 | BUY |
| −1 to 1 | HOLD |
| −3 to −2 | SELL |
| ≤ −4 | STRONG_SELL |

### Step 3 — Veto rules

Signals are downgraded to **HOLD** if contradictory conditions exist:

**BUY / STRONG_BUY vetoed when:**
- Gold trend score is negative
- DXY is strongly rising (score = −2)
- US 10Y yields are strongly rising (score = −2)

**SELL / STRONG_SELL vetoed when:**
- Gold trend score is positive
- DXY is strongly falling (score = +2)

### Step 4 — Confidence

| Level | Meaning |
|-------|---------|
| HIGH | 3+ factors clearly agree with the gold trend direction |
| MEDIUM | 2 factors agree |
| LOW | Mixed or degraded data |

**Auto-downgraded if:**
- Article body scraping fully failed (headline-only analysis)
- Fewer than 5 unique articles were available
- 2+ market data sources could not be fetched

---

## How the trade validation layer works

A directional bias alone is **not** a trade. A setup is only marked `TRADE` when all
of these are satisfied:

1. A directional signal exists (BUY or SELL family)
2. Gold indicators are available
3. The setup geometry is valid (entry is on the correct side of invalidation)
4. **Risk/Reward ratio ≥ 1:2** (configurable via `config.MIN_RR`)

### BUY setup

```
Invalidation = min(EMA20, 14-day low)
Stop Loss    = Invalidation × (1 − 0.5% buffer)
Entry        = Current gold price
Take Profit  = Entry + 2 × Risk
```

### SELL setup

```
Invalidation = max(EMA20, 14-day high)
Stop Loss    = Invalidation × (1 + 0.5% buffer)
Entry        = Current gold price
Take Profit  = Entry − 2 × Risk
```

If `reward / risk < 2.0`, the output is `NO_TRADE` even if the directional bias is
bullish or bearish. The signal and the trade decision are kept **separate**.

---

## Output files

Each run saves to `outputs/` with a shared timestamp:

```
outputs/
├── sentiment_20260414_014632.csv   ← per-article sentiment
├── sentiment_20260414_014632.json  ← sentiment + summary
└── signal_20260414_014632.json     ← market data + signal + trade setup
```

### Signal JSON structure

```json
{
  "sentiment_score": -1,
  "dxy_score": 2,
  "yield_score": 0,
  "gold_trend_score": 1,
  "total_score": 2,
  "raw_signal": "BUY",
  "signal": "BUY",
  "veto_applied": false,
  "confidence": "LOW",
  "reasoning": [ "..." ],
  "data_quality": {
    "articles_fetched": 70,
    "unique_articles": 5,
    "successfully_scraped": 0,
    "failed_scrapes": 5,
    "text_mode_used": "title",
    "market_data_failures": 0
  },
  "market_snapshot": {
    "gold":      { "current": 4784.5, "ema20": 4736.8, "ema50": 4793.1, ... },
    "dxy":       { "current": 98.37,  "ema20": 99.21,  "ema50": 98.91,  ... },
    "yield_10y": { "current": 4.297,  "ema20": 4.298,  "ema50": 4.245,  ... }
  },
  "trade_setup": {
    "trade_decision": "TRADE",
    "entry_price": 4784.5,
    "stop_loss": 4353.62,
    "take_profit": 5646.25,
    "risk_amount": 430.88,
    "reward_amount": 861.75,
    "risk_reward_ratio": 2.0,
    "minimum_required_rr": 2.0,
    "trade_valid": true,
    "setup_note": null
  }
}
```

---

## Project structure

```
├── main.py                        # CLI + pipeline orchestration
├── config.py                      # All configuration constants
├── app.py                         # Flask dashboard server
├── requirements.txt
├── news/
│   ├── rss_fetcher.py             # Google News RSS ingestion
│   ├── article_scraper.py         # Web scraping with retries
│   └── dedup.py                   # URL + title deduplication
├── sentiment/
│   ├── vader_analyzer.py          # VADER wrapper
│   ├── finbert_analyzer.py        # FinBERT wrapper (loaded once)
│   └── aggregator.py              # Final label combining
├── market/
│   ├── data_fetcher.py            # yfinance OHLCV fetching
│   ├── indicators.py              # EMA, 5d return, 14d high/low
│   └── trend_scoring.py          # Integer scores per instrument
├── signals/
│   ├── signal_engine.py           # Score combination + veto logic
│   ├── confidence.py              # HIGH / MEDIUM / LOW confidence
│   ├── reasoning.py               # Plain-English explanation builder
│   ├── trade_setup.py             # Entry / stop / take-profit levels
│   └── risk_management.py         # RR validation
├── utils/
│   ├── logger.py
│   ├── text_cleaner.py
│   └── io_helpers.py
├── templates/
│   └── index.html                 # Dashboard template
└── outputs/                       # Auto-created, holds run results
```

---

## Configuration

Edit [config.py](config.py) to tune the engine:

| Setting | Default | Description |
|---------|---------|-------------|
| `RSS_QUERIES` | 7 gold queries | Search terms |
| `MAX_ARTICLES` | 50 | Articles per run |
| `MARKET_LOOKBACK_DAYS` | 90 | Days of history to fetch |
| `EMA_SHORT / EMA_LONG` | 20 / 50 | EMA windows |
| `RETURN_WINDOW_DAYS` | 5 | Momentum window |
| `DXY_STRONG_MOVE_PCT` | 1.0% | DXY strong move threshold |
| `YIELD_STRONG_MOVE` | 0.15 ppt | Yield strong move threshold |
| `GOLD_STRONG_MOVE_PCT` | 1.5% | Gold strong move threshold |
| `MIN_RR` | 2.0 | Minimum risk/reward ratio |
| `STOP_BUFFER_PCT` | 0.5% | Buffer beyond invalidation level |

---

## Sentiment models

### VADER
- Rule-based, no download required, very fast
- Compound score in [−1, +1]
- Positive ≥ 0.05 · Negative ≤ −0.05

### FinBERT (`ProsusAI/finbert`)
- Finance-specific BERT, trained on Financial PhraseBank + FiQA
- Truncated to ~1800 chars before inference
- Returns label + confidence; loaded once at startup

### Aggregation rule
When both models are active:
- Both agree → use that label
- Disagree → prefer FinBERT (finance-domain model)
- Final score = average of both numeric scores

---

## Market data sources

| Instrument | yfinance symbol | Notes |
|------------|----------------|-------|
| Gold Futures | `GC=F` | COMEX front-month |
| US Dollar Index | `DX-Y.NYB` | ICE DXY |
| US 10Y Yield | `^TNX` | CBOE 10-Year Treasury |

All data is fetched via `yfinance` — no API key required.
The symbol mapping is in `config.MARKET_SYMBOLS` and can be changed without
modifying any other file.

---

## Limitations

- Google News RSS may throttle; article scraping often fails on paywalled/JS sites
- Sentiment derived from headlines only when scraping fails — explicitly flagged
- FinBERT is not fine-tuned for gold-specific terminology
- Market data is end-of-day; intraday moves are not captured
- EMA50 uses fewer bars when history < 50 trading days
- Signal thresholds and veto rules are static — no machine learning
- **Not suitable for automated trading or live execution**

---

## Future improvements

- Additional news sources (Reuters, FT RSS)
- Gold-specific FinBERT fine-tuning
- Intraday price data for tighter setups
- ATR-based stop sizing instead of fixed invalidation
- Time-series signal tracking across multiple runs
- Dashboard extended to show signal history and market data
