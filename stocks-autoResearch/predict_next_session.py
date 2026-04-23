"""
One-shot inference: predict next-session direction per ticker using
the production pooled model + per-ticker residuals where promoted.

Writes outputs/stocks/_aggregate.json for the dashboard.

Usage:  python predict_next_session.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings import settings
from data import pipeline as pl
from models.model_registry import registry
from agents.residual_agent import RESIDUAL_FEATURES, _logit

_PARENT_ROOT = settings.root_dir.parent
if str(_PARENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PARENT_ROOT))

from stocks.stock_universe import UNIVERSE  # noqa: E402

# Macro signals (sentiment + event gate) from parent project.
try:
    from events.calendar import get_events
    from events.blackout import is_blackout
except Exception:
    get_events = None
    is_blackout = None


# Hard-to-borrow shortlist (illustrative; replace with broker feed when available).
# Tickers here get a borrow flag so trader knows short execution may be expensive
# or impossible. Conservative starter set: meme/recent-IPO/special-situation names.
HTB_TICKERS: set[str] = {
    "GME", "AMC", "BBBYQ", "MULN", "TLRY", "RIVN", "LCID", "SNDL",
    "SOFI",  # historically tight at times
    "DJT", "RDDT",  # recent IPOs
}

# Residual sanity thresholds. A residual model that predicts near-constant
# probability (very low std) or always-up / always-down (extreme pred_up_rate)
# is degenerate — skip it and fall back to pooled.
RESIDUAL_MIN_STD       = 0.03
RESIDUAL_PRED_UP_RANGE = (0.20, 0.80)

logger = logging.getLogger(__name__)


def build_live_frame(lookback_days: int = 400) -> pd.DataFrame:
    """Same as pipeline.build_feature_matrix but DOES NOT drop rows where
    y_next_* is NaN (the latest bar has no next-day target yet)."""
    spy = pl._fetch_yf(pl.MARKET_SYMBOLS["SPY"], lookback_days)
    vix = pl._fetch_yf(pl.MARKET_SYMBOLS["VIX"], lookback_days)
    dxy = pl._fetch_yf(pl.MARKET_SYMBOLS["DXY"], lookback_days)
    mkt = pl._market_features(spy, vix, dxy)

    sector_frames = {}
    for sector, etf in pl.SECTOR_ETFS.items():
        try:
            s = pl._fetch_yf(etf, lookback_days)["Close"].pct_change(5).rename(f"sector_ret_5d_{etf}")
            sector_frames[sector] = s
        except Exception as exc:
            logger.warning("sector ETF %s fetch failed: %s", etf, exc)

    fred_frames = {name: pl._fetch_fred(sid) for name, sid in pl.FRED_SERIES.items()}
    fred_df = pd.concat(fred_frames.values(), axis=1)
    fred_df.columns = list(fred_frames.keys())
    macro = pl._macro_features(fred_df)

    per_ticker = []
    for stock in UNIVERSE:
        try:
            ohlcv = pl._fetch_yf(stock.ticker, lookback_days)
        except Exception as exc:
            logger.warning("ticker %s fetch failed: %s — skipping", stock.ticker, exc)
            continue
        feat = pl._per_ticker_features(ohlcv, settings.default_lookback)
        feat["ticker"] = stock.ticker
        feat["sector"] = stock.sector
        sector_ret = sector_frames.get(stock.sector)
        feat["sector_rel_5d"] = (
            feat["ret_5d"] - sector_ret.reindex(feat.index)
            if sector_ret is not None else 0.0
        )
        feat = feat.join(mkt, how="left").join(macro, how="left")
        feat["y_next_ret"] = feat["close"].shift(-1) / feat["close"] - 1.0
        feat["y_next_dir"] = (feat["y_next_ret"] > 0).astype(int)
        per_ticker.append(feat)

    long = pd.concat(per_ticker, axis=0)
    long.index.name = "date"
    long = long.reset_index().sort_values(["date", "ticker"])
    macro_cols = list(macro.columns) + ["fed", "real10y"]
    for c in macro_cols:
        if c in long.columns:
            long[c] = long.groupby("ticker")[c].ffill()

    feature_cols = [c for c in long.columns
                    if c not in ("y_next_ret", "y_next_dir")]
    long = long.dropna(subset=feature_cols).copy()
    long["ticker"] = long["ticker"].astype("category")
    long["sector"] = long["sector"].astype("category")
    return long


def _latest_sentiment(parent_root: Path) -> dict | None:
    """Pull most recent swing sentiment row from outputs/sentiment_cache.jsonl."""
    p = parent_root / "outputs" / "sentiment_cache.jsonl"
    if not p.exists():
        return None
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    swing = [r for r in rows if r.get("timeframe") == "swing"]
    if not swing:
        return None
    return max(swing, key=lambda r: r.get("ts", ""))


def _latest_real10y_change(live_frame: pd.DataFrame) -> dict | None:
    """real10y 5d / 20d change in pp from the live feature frame.
    Uses one ticker's series since macro is ffilled identically across tickers."""
    if "real10y" not in live_frame.columns:
        return None
    one = live_frame[live_frame["ticker"].astype(str) == "AAPL"]
    if one.empty:
        one = live_frame.groupby("ticker", observed=True).head(99999)
    s = one.sort_values("date")["real10y"].dropna()
    if len(s) < 25:
        return None
    return {
        "current": round(float(s.iloc[-1]), 4),
        "chg_5d":  round(float(s.iloc[-1] - s.iloc[-6]), 4),
        "chg_20d": round(float(s.iloc[-1] - s.iloc[-21]), 4),
    }


def _event_status(target: date) -> dict:
    """Blackout status + nearest upcoming event for the target date."""
    if get_events is None or is_blackout is None:
        return {"blocked": False, "reason": None, "next_event": None}
    events = get_events(target - timedelta(days=7), target + timedelta(days=21))
    blocked, reason = is_blackout(target, events)
    upcoming = sorted(
        [e for e in events if e.date >= target],
        key=lambda e: e.date,
    )
    nxt = upcoming[0] if upcoming else None
    return {
        "blocked": bool(blocked),
        "reason":  reason,
        "next_event": (
            {"date": str(nxt.date), "kind": nxt.kind, "label": nxt.label,
             "days_away": (nxt.date - target).days}
            if nxt else None
        ),
    }


_DEFAULT_WEIGHTS = {
    "model_signal":   0.35,
    "stock_sent":     0.12,
    "real_yield":     0.10,
    "sector_agree":   0.10,
    "trend":          0.13,
    "credit":         0.08,
    "vix_slope":      0.05,
    "fomc_prox":      0.03,
    "tom":            0.02,
    "dow":            0.02,
}


def _load_fitted_weights(parent_root: Path, vix: float | None) -> tuple[dict | None, dict, str]:
    """Load fitted composite weights + calibration info, regime-aware.

    Schemas supported, newest first:
      - Tier-3 xgb_v1:    regimes + shared XGBoost model file for p_up.
      - Tier-2 regime_v1: regimes + per-regime intercept/coefs (LR).
      - Tier-1 flat:      {weights, raw_coefs, intercept}.

    Returns (weights_dict | None, calibration_dict, regime_label).
    """
    p = parent_root / "outputs" / "stocks" / "_composite_weights.json"
    if not p.exists():
        return None, {}, "default"
    try:
        d = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None, {}, "default"

    # Tier-3: XGBoost. Regime-aware — prefer per-regime model file, fall back to shared.
    if isinstance(d, dict) and d.get("schema") == "xgb_v1":
        thresh = float(d.get("vix_threshold", 18.0))
        regimes = d.get("regimes") or {}
        regime_label = "high_vix" if (vix is not None and vix >= thresh) else "low_vix"
        fit = regimes.get(regime_label) or {}
        model_file = fit.get("model_file") or d.get("model_file") or "_composite_model.ubj"
        calib = {
            "schema":          "xgb_v1",
            "model_file":      model_file,
            "components_used": d.get("components_used") or [],
            "target":          d.get("target"),
            "walk_forward":    fit.get("wf") or d.get("walk_forward"),
        }
        return fit.get("weights"), calib, regime_label

    # Tier-2 regime-aware schema.
    if isinstance(d, dict) and d.get("schema") == "regime_v1":
        thresh = float(d.get("vix_threshold", 18.0))
        regimes = d.get("regimes") or {}
        regime_label = "high_vix" if (vix is not None and vix >= thresh) else "low_vix"
        fit = regimes.get(regime_label) or {}
        if not fit:
            return None, {}, "default"
        calib = {
            "schema":          "regime_v1",
            "intercept":       fit.get("intercept"),
            "raw_coefs":       fit.get("raw_coefs") or {},
            "components_used": d.get("components_used") or [],
            "target":          d.get("target"),
            "walk_forward":    fit.get("wf"),
        }
        return fit.get("weights"), calib, regime_label

    # Tier-1 flat schema fallback.
    if isinstance(d, dict) and "weights" in d:
        calib = {
            "schema":      "flat",
            "intercept":   d.get("intercept"),
            "raw_coefs":   d.get("raw_coefs") or {},
            "components_used": d.get("components_used") or [],
        }
        return d["weights"], calib, "flat"

    return None, {}, "default"


def _load_reliability(parent_root: Path) -> dict | None:
    """Load walk-forward reliability JSON if present."""
    p = parent_root / "outputs" / "stocks" / "_reliability.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _calibration_bin(p_up: float | None, reliability: dict | None) -> dict | None:
    """Find the reliability bin that contains p_up.

    Returns {p_lo, p_hi, n, predicted, actual, gap} or None.
    """
    if p_up is None or not reliability:
        return None
    bins = reliability.get("bins") or []
    for b in bins:
        if b.get("n", 0) == 0:
            continue
        lo = float(b.get("p_lo", 0))
        hi = float(b.get("p_hi", 1))
        # Last bin is inclusive on the top edge.
        if (p_up >= lo and p_up < hi) or (hi >= 1.0 and p_up <= hi):
            gap = float(b["actual"]) - float(b["predicted"])
            return {
                "p_lo":      lo,
                "p_hi":      hi,
                "n":         int(b["n"]),
                "predicted": float(b["predicted"]),
                "actual":    float(b["actual"]),
                "gap":       round(gap, 4),
            }
    return None


_XGB_MODEL_CACHE: dict | None = None


def _load_xgb_model(parent_root: Path, model_file: str):
    """Lazy-load the fitted XGBoost composite model from disk."""
    global _XGB_MODEL_CACHE
    if _XGB_MODEL_CACHE and _XGB_MODEL_CACHE.get("file") == model_file:
        return _XGB_MODEL_CACHE["model"]
    try:
        from xgboost import XGBClassifier
        m = XGBClassifier()
        m.load_model(str(parent_root / "outputs" / "stocks" / model_file))
        _XGB_MODEL_CACHE = {"file": model_file, "model": m}
        return m
    except Exception as e:
        logger.warning(f"xgb model load failed: {e}")
        return None


def _calibrated_p_up(components: dict, calib: dict, parent_root: Path | None = None) -> float | None:
    """Compute p_up. Uses XGBoost predict_proba if available, else LR coefs.

    Missing components default to 0. Component x/100 scaling matches fit.
    """
    schema = (calib or {}).get("schema")

    if schema == "xgb_v1" and parent_root is not None:
        model = _load_xgb_model(parent_root, calib.get("model_file") or "_composite_model.ubj")
        feats = calib.get("components_used") or list(components.keys())
        if model is not None:
            try:
                x = np.array([[float(components.get(k, 0.0)) / 100.0 for k in feats]])
                p = float(model.predict_proba(x)[0, 1])
                return p
            except Exception as e:
                logger.warning(f"xgb predict failed: {e}")

    # LR fallback (Tier-1/2 schemas)
    intercept = (calib or {}).get("intercept")
    coefs     = (calib or {}).get("raw_coefs") or {}
    if intercept is None or not coefs:
        return None
    try:
        z = float(intercept)
        for k, beta in coefs.items():
            x = float(components.get(k, 0.0)) / 100.0
            z += float(beta) * x
        return float(1.0 / (1.0 + np.exp(-z)))
    except (ValueError, TypeError):
        return None


def _vix_regime(vix: float | None) -> dict:
    """VIX regime classifier + position-size damper (item 20)."""
    if vix is None or pd.isna(vix):
        return {"vix": None, "regime": "UNKNOWN", "size_mult": 1.0}
    if vix >= 35:
        return {"vix": float(vix), "regime": "PANIC",  "size_mult": 0.0}
    if vix >= 25:
        return {"vix": float(vix), "regime": "ELEVATED", "size_mult": 0.5}
    if vix >= 18:
        return {"vix": float(vix), "regime": "NORMAL", "size_mult": 1.0}
    return {"vix": float(vix), "regime": "CALM", "size_mult": 1.0}


def _composite_score(
    mean_p: float,
    breadth: float,
    holdout_acc: float,
    sectors: list[dict],
    stock_sent_mean: float | None,
    real10y: dict | None,
    event: dict,
    vix_regime: dict,
    trend: dict | None = None,
    credit: dict | None = None,
    vix_slope_snap: dict | None = None,
    fomc_prox_val: float | None = None,
    tom_val: float | None = None,
    dow_val: float | None = None,
    weights: dict | None = None,
    calibration: dict | None = None,
    reliability: dict | None = None,
    parent_root: Path | None = None,
) -> dict:
    """
    Composite directional score (-100..+100) + monotonic confidence tier.

    Components (after Tier-1 rework):
      model_signal     (mean_p - 0.5) × 200
      stock_sent       per-ticker sentiment mean × 250
      real_yield       -DFII10 5d Δ × 1000
      sector_agree     net long-short sectors / N × 100
      trend            SPY 20d return z × 33   (NEW — momentum)
      credit           HYG/IEF 60d z × 33      (NEW — risk appetite)

    `breadth_signal` removed: collinear with model_signal (same per-ticker
    proba). Regression now fits orthogonal factor set.

    p_up returned alongside score when calibration data available.
    """
    from research.macro_overlays import z_to_component

    components = {}

    components["model_signal"] = round(max(-100.0, min(100.0, (mean_p - 0.5) * 200)), 1)

    if stock_sent_mean is not None:
        components["stock_sent"] = round(max(-100.0, min(100.0, stock_sent_mean * 250)), 1)
    else:
        components["stock_sent"] = 0.0

    if real10y:
        components["real_yield"] = round(max(-100.0, min(100.0, -float(real10y.get("chg_5d", 0)) * 1000)), 1)
    else:
        components["real_yield"] = 0.0

    if sectors:
        sec_long  = sum(1 for s in sectors if s["mean_proba"] >= 0.5)
        sec_short = len(sectors) - sec_long
        components["sector_agree"] = round((sec_long - sec_short) / len(sectors) * 100, 1)
    else:
        components["sector_agree"] = 0.0

    components["trend"]  = round(z_to_component(
        (trend or {}).get("trend_z") if trend else None
    ), 1)
    components["credit"] = round(z_to_component(
        (credit or {}).get("credit_z") if credit else None
    ), 1)
    components["vix_slope"] = round(z_to_component(
        (vix_slope_snap or {}).get("slope_z") if vix_slope_snap else None
    ), 1)
    components["fomc_prox"] = round(float(fomc_prox_val or 0.0) * 100.0, 1)
    components["tom"]       = round(float(tom_val or 0.0)  * 100.0, 1)
    components["dow"]       = round(float(dow_val or 0.0)  * 100.0, 1)

    weights = weights or dict(_DEFAULT_WEIGHTS)
    raw_score = sum(components[k] * weights.get(k, 0.0) for k in components)

    # Event blackout damps conviction.
    dampers_applied = []
    if event.get("blocked"):
        raw_score *= 0.5
        dampers_applied.append("event_blackout ×0.5")

    nxt = event.get("next_event") or {}
    if nxt.get("kind") == "FOMC" and (nxt.get("days_away") or 99) <= 2:
        raw_score *= 0.7
        dampers_applied.append("pre-FOMC ×0.7")

    # VIX regime damper: panic flat, elevated half (item 20)
    if vix_regime.get("size_mult", 1.0) < 1.0:
        raw_score *= max(vix_regime["size_mult"], 0.3)  # floor so signal not zeroed
        dampers_applied.append(f"VIX {vix_regime['regime']} ×{vix_regime['size_mult']}")

    score = max(-100.0, min(100.0, raw_score))

    # ── Monotonic confidence ladder (item 14) ──
    # Start at HIGH, demote stepwise. Each demotion goes one tier down only.
    tiers = ["HIGH", "MEDIUM", "LOW", "VERY_LOW"]
    tier_idx = 0
    reasons: list[str] = []

    def _demote(reason: str) -> None:
        nonlocal tier_idx
        if tier_idx < len(tiers) - 1:
            tier_idx += 1
        reasons.append(reason)

    if holdout_acc is None or holdout_acc < 0.52:
        _demote(f"holdout acc {holdout_acc} too weak")
        _demote("(extra demote: model untrustworthy)")
    elif holdout_acc < 0.55:
        _demote(f"holdout acc {holdout_acc:.3f} marginal")

    if abs(score) < 10:
        _demote(f"|score| {abs(score):.0f} < 10 (noise)")
    elif abs(score) < 25:
        _demote(f"|score| {abs(score):.0f} < 25 (modest)")

    sign = 1 if score >= 0 else -1
    agree = sum(
        1 for v in components.values()
        if (sign > 0 and v > 5) or (sign < 0 and v < -5)
    )
    n_components = len(components)
    if agree < n_components - 1:
        _demote(f"only {agree}/{n_components} signals agree")

    if event.get("blocked"):
        _demote(f"event blackout: {event.get('reason')}")

    if vix_regime.get("regime") == "PANIC":
        _demote("VIX PANIC regime")

    # ── Calibration-based demotions (walk-forward reliability) ──
    p_up = _calibrated_p_up(components, calibration or {}, parent_root)
    calib_bin = _calibration_bin(p_up, reliability)
    if reliability:
        ece = reliability.get("ece")
        if ece is not None and ece > 0.10:
            _demote(f"global ECE {ece:.3f} > 0.10 (model mis-calibrated)")
    if calib_bin:
        gap = abs(calib_bin["gap"])
        if gap > 0.15:
            _demote(f"p_up bin {calib_bin['p_lo']:.1f}-{calib_bin['p_hi']:.1f} gap {calib_bin['gap']:+.2f} (poorly calibrated)")
        if calib_bin["n"] < 30:
            _demote(f"p_up bin n={calib_bin['n']} (thin sample)")

    confidence = tiers[tier_idx]

    if score >= 30:
        bias = "STRONG LONG"
    elif score >= 10:
        bias = "LONG"
    elif score <= -30:
        bias = "STRONG SHORT"
    elif score <= -10:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "score":          round(score, 1),
        "bias":           bias,
        "confidence":     confidence,
        "p_up":           round(p_up, 4) if p_up is not None else None,
        "calib_bin":      calib_bin,
        "components":     components,
        "weights":        weights,
        "weights_source": "fitted" if weights != _DEFAULT_WEIGHTS else "default",
        "agree_count":    agree,
        "n_components":   n_components,
        "reasons":        reasons,
        "dampers":        dampers_applied,
    }


def _conflict_action(model_long: bool, rule_signal: str) -> dict:
    """Map conflict between model & rule-based signal to actionable policy (item 10).

    Rules: HOLD/None = no conflict. Strong rule disagreement → SKIP. Weak → DOWNSIZE.
    """
    sig = (rule_signal or "").upper()
    if sig in ("", "HOLD", "NONE", "NEUTRAL"):
        return {"conflict": None, "action": "TAKE", "note": None}
    rule_long = "BUY" in sig
    rule_short = "SELL" in sig
    strong_rule = sig.startswith("STRONG_")
    if model_long and rule_short:
        return {
            "conflict": "model LONG vs rules SELL",
            "action":   "SKIP" if strong_rule else "DOWNSIZE",
            "note":     "rule-based system says short; respect it or halve size",
        }
    if (not model_long) and rule_long:
        return {
            "conflict": "model SHORT vs rules BUY",
            "action":   "SKIP" if strong_rule else "DOWNSIZE",
            "note":     "rule-based system says long; respect it or halve size",
        }
    return {"conflict": None, "action": "TAKE", "note": None}


def main() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    meta = registry.production_metadata()
    if meta is None:
        raise SystemExit("no production model")
    model, _ = registry.load(meta.version)
    print(f"production model: {meta.version}  acc={meta.accuracy}  "
          f"sharpe={meta.sharpe}  pred_up_rate={meta.pred_up_rate}")

    print("building live feature frame…")
    df = build_live_frame()

    last_date = df["date"].max()
    live = df[df["date"] == last_date].copy()
    print(f"live row date: {last_date.date()}  tickers={live['ticker'].nunique()}")

    pooled_proba = np.asarray(model.predict_proba(live[model.features]), dtype=float)
    live["pooled_proba"] = pooled_proba
    live["pooled_logit"] = _logit(pooled_proba)

    proba = pooled_proba.copy()
    model_used = np.array(["pooled"] * len(live), dtype=object)
    tickers_arr = live["ticker"].astype(str).to_numpy()
    residual_audit: list[dict] = []
    for ticker in sorted(set(tickers_arr)):
        residual = registry.load_residual(ticker)
        if residual is None:
            continue
        mask = tickers_arr == ticker
        if not mask.any():
            continue
        # ── Residual sanity check (item 7) ──
        # Probe residual on the *full historical frame* for this ticker so we
        # see prediction distribution, not just one row. Pinned residuals get
        # rejected and we keep pooled.
        hist = df[df["ticker"].astype(str) == ticker]
        if hist.empty:
            continue
        try:
            hist_proba = np.asarray(
                residual.predict_proba(hist[RESIDUAL_FEATURES]),
                dtype=float,
            )
        except Exception as exc:
            residual_audit.append({"ticker": ticker, "verdict": "error",
                                    "reason": str(exc)})
            continue
        std = float(hist_proba.std())
        up_rate = float((hist_proba >= 0.5).mean())
        ok = (std >= RESIDUAL_MIN_STD
              and RESIDUAL_PRED_UP_RANGE[0] <= up_rate <= RESIDUAL_PRED_UP_RANGE[1])
        residual_audit.append({
            "ticker": ticker, "std": round(std, 4), "up_rate": round(up_rate, 4),
            "verdict": "ok" if ok else "rejected",
            "reason": None if ok else
                ("low std" if std < RESIDUAL_MIN_STD else "extreme up_rate"),
        })
        if not ok:
            continue
        proba[mask] = np.asarray(
            residual.predict_proba(live.loc[mask, RESIDUAL_FEATURES]),
            dtype=float,
        )
        model_used[mask] = "residual"
    live["proba"] = proba
    live["model_used"] = model_used

    live["pred"] = (live["proba"] >= 0.5).astype(int)
    live = live.sort_values("proba", ascending=False)

    n = len(live)
    n_up = int(live["pred"].sum())
    mean_p = float(live["proba"].mean())
    breadth = n_up / n
    print()
    print(f"=== NEXT-SESSION BIAS (after close {last_date.date()}) ===")
    print(f"n_tickers={n}  predicted_up={n_up} ({breadth:.0%})  "
          f"predicted_down={n - n_up} ({1-breadth:.0%})")
    print(f"mean P(up)={mean_p:.3f}  median P(up)={live['proba'].median():.3f}")
    if mean_p > 0.55 and breadth > 0.6:
        lean = "LONG"
    elif mean_p < 0.45 and breadth < 0.4:
        lean = "SHORT"
    else:
        lean = "NEUTRAL"
    print(f"S&P 500 directional lean: {lean}")
    print()
    print("Top 10 long conviction:")
    print(live[["ticker", "proba", "model_used"]].head(10).to_string(index=False))
    print()
    print("Top 10 short conviction:")
    bottom = live.sort_values("proba").head(10)
    print(bottom[["ticker", "proba", "model_used"]].to_string(index=False))

    # ── Trader enrichment: pull per-ticker JSON for trade plan + sentiment ──
    stocks_dir = settings.root_dir.parent / "outputs" / "stocks"

    def _load_ticker_json(t: str) -> dict | None:
        p = stocks_dir / f"{t}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None

    def _trade_plan(price: float, atr_pct: float, side: str) -> dict:
        """ATR-based: 1.5 ATR stop, 2/3 ATR targets. Matches Explorer convention."""
        if not price or not atr_pct:
            return {}
        atr = price * (atr_pct / 100.0)
        if side == "LONG":
            stop, tp1, tp2 = price - 1.5 * atr, price + 2.0 * atr, price + 3.0 * atr
        else:
            stop, tp1, tp2 = price + 1.5 * atr, price - 2.0 * atr, price - 3.0 * atr
        risk = abs(price - stop)
        return {
            "entry": round(price, 2),
            "stop":  round(stop, 2),
            "tp1":   round(tp1, 2),
            "tp2":   round(tp2, 2),
            "risk_pct":   round(abs(price - stop) / price * 100, 2),
            "rr_tp1":     round(abs(tp1 - price) / risk, 2) if risk else None,
            "rr_tp2":     round(abs(tp2 - price) / risk, 2) if risk else None,
            "atr_pct":    round(atr_pct, 2),
        }

    def _enrich(picks: list, side: str) -> list:
        enriched = []
        for p in picks:
            j = _load_ticker_json(p["ticker"])
            row = dict(p)
            if j is not None:
                ps = j.get("price_summary") or {}
                fs = j.get("factor_scores") or {}
                price = ps.get("current")
                atr_pct = ps.get("atr_pct")
                row.update({
                    "company":       j.get("company_name"),
                    "sector":        j.get("sector"),
                    "signal":        j.get("signal"),
                    "confidence":    j.get("confidence"),
                    "sent_label":    j.get("sentiment_label"),
                    "sent_score":    j.get("sentiment_score"),
                    "factor_total":  fs.get("total"),
                    "price":         price,
                    "ema20":         ps.get("ema20"),
                    "ema50":         ps.get("ema50"),
                    "return_5d_pct": ps.get("return_5d_pct"),
                    "return_1d_pct": ps.get("return_1d_pct"),
                    "vol_ratio":     ps.get("volume_ratio"),
                    "trade_plan":    _trade_plan(price, atr_pct, side),
                })
                # Actionable conflict policy (item 10).
                conf = _conflict_action(p["proba"] >= 0.5, j.get("signal"))
                row["conflict"]        = conf["conflict"]
                row["conflict_action"] = conf["action"]
                row["conflict_note"]   = conf["note"]
            else:
                row["conflict"] = None
                row["conflict_action"] = "TAKE"
                row["conflict_note"] = None
            # HTB borrow flag (item 19) — only meaningful for shorts.
            row["htb"] = p["ticker"] in HTB_TICKERS
            enriched.append(row)
        return enriched

    top_long_raw = [
        {"ticker": r.ticker, "proba": round(float(r.proba), 4),
         "model_used": str(r.model_used)}
        for r in live.head(15).itertuples()
    ]
    top_short_raw = [
        {"ticker": r.ticker, "proba": round(float(r.proba), 4),
         "model_used": str(r.model_used)}
        for r in live.sort_values("proba").head(15).itertuples()
    ]

    # ── Sector breakdown (rotation signal) ──
    live_sec = live.copy()
    live_sec["sector"] = live_sec["sector"].astype(str)
    sector_rows = []
    for sector, g in live_sec.groupby("sector"):
        n_s = len(g)
        n_u = int((g["proba"] >= 0.5).sum())
        sector_rows.append({
            "sector":     sector,
            "n":          n_s,
            "n_up":       n_u,
            "n_down":     n_s - n_u,
            "breadth_up": round(n_u / n_s, 4) if n_s else 0.0,
            "mean_proba": round(float(g["proba"].mean()), 4),
        })
    sector_rows.sort(key=lambda r: r["mean_proba"], reverse=True)

    # ── Composite SPX score + confidence ──
    parent_root = settings.root_dir.parent
    gold_sentiment = _latest_sentiment(parent_root)  # kept for reference, not in score
    real10y   = _latest_real10y_change(df)
    next_session = last_date.date() + timedelta(days=1)
    event = _event_status(next_session)

    # Item 9: per-ticker stock sentiment mean (replaces gold proxy in composite).
    stock_sents: list[float] = []
    for stock in UNIVERSE:
        j = _load_ticker_json(stock.ticker)
        if j and isinstance(j.get("sentiment_score"), (int, float)):
            stock_sents.append(float(j["sentiment_score"]))
    stock_sent_mean = (sum(stock_sents) / len(stock_sents)) if stock_sents else None

    # Item 20: VIX regime damper. Pull last VIX from live frame (already a feature).
    last_vix = float(live["vix"].iloc[0]) if "vix" in live.columns and len(live) else None
    vix_reg = _vix_regime(last_vix)

    # Item 2: load fitted weights + calibration (intercept/coefs for p_up).
    # Tier-2: regime-aware — pick low_vix/high_vix weight set from current VIX.
    fitted_weights, calibration, regime_label = _load_fitted_weights(parent_root, last_vix)
    reliability_data = _load_reliability(parent_root)

    # Tier-1/3 overlays: SPY momentum, HYG/IEF credit, VIX term slope, event/seasonal.
    from research.macro_overlays import (
        spy_trend, credit_zscore, vix_slope,
        turn_of_month_flag, day_of_week_feat, fomc_proximity,
    )
    trend_snap  = spy_trend(next_session)
    credit_snap = credit_zscore(next_session)
    vslope_snap = vix_slope(next_session)

    # FOMC proximity from event gate
    fomc_days = None
    nxt_ev = (event.get("next_event") or {})
    if nxt_ev.get("kind") == "FOMC":
        fomc_days = nxt_ev.get("days_away")
    fomc_val = fomc_proximity(fomc_days)
    tom_val  = turn_of_month_flag(next_session)
    dow_val  = day_of_week_feat(next_session)

    composite = _composite_score(
        mean_p=mean_p, breadth=breadth, holdout_acc=meta.accuracy,
        sectors=sector_rows, stock_sent_mean=stock_sent_mean,
        real10y=real10y, event=event, vix_regime=vix_reg,
        trend=trend_snap, credit=credit_snap,
        vix_slope_snap=vslope_snap,
        fomc_prox_val=fomc_val, tom_val=tom_val, dow_val=dow_val,
        weights=fitted_weights, calibration=calibration,
        reliability=reliability_data,
        parent_root=parent_root,
    )
    composite["trend_snap"]  = trend_snap
    composite["credit_snap"] = credit_snap
    composite["vix_slope_snap"] = vslope_snap
    composite["regime"]      = regime_label
    composite["target"]      = calibration.get("target") if calibration else None
    composite["walk_forward"] = calibration.get("walk_forward") if calibration else None
    print()
    print(f"=== SPX COMPOSITE === score={composite['score']:+.1f}  "
          f"bias={composite['bias']}  confidence={composite['confidence']}  "
          f"weights={composite['weights_source']}")
    for k, v in composite["components"].items():
        print(f"  {k:>15s}: {v:+.1f}  (w={composite['weights'].get(k, 0):.2f})")
    if composite["dampers"]:
        print("  dampers: " + "; ".join(composite["dampers"]))
    if composite["reasons"]:
        print("  notes: " + "; ".join(composite["reasons"]))

    # Item 16: market-hours warning. NYSE RTH = 13:30..20:00 UTC, Mon-Fri.
    now_utc = datetime.now(timezone.utc)
    is_weekday = now_utc.weekday() < 5
    is_rth = is_weekday and (
        (now_utc.hour, now_utc.minute) >= (13, 30)
        and (now_utc.hour, now_utc.minute) < (20, 0)
    )
    market_warning = None
    if is_rth:
        market_warning = (
            f"NYSE RTH currently OPEN ({now_utc.strftime('%H:%M UTC')}). "
            f"Features anchored at last close {last_date.date()}; live prices may have moved."
        )
        print(f"\n⚠ {market_warning}")

    out = {
        "asof_close":         str(last_date.date()),
        "generated_at":       now_utc.isoformat(timespec="seconds"),
        "model_version":      meta.version,
        "model_holdout_acc":  meta.accuracy,
        "model_sharpe":       meta.sharpe,
        "n_tickers":          n,
        "n_up":               n_up,
        "n_down":             n - n_up,
        "breadth_up":         round(breadth, 4),
        "mean_prob_up":       round(mean_p, 4),
        "median_prob_up":     round(float(live["proba"].median()), 4),
        "lean":               lean,
        "top_long":           _enrich(top_long_raw, "LONG"),
        "top_short":          _enrich(top_short_raw, "SHORT"),
        "sectors":            sector_rows,
        "composite":          composite,
        "stock_sent_mean":    round(stock_sent_mean, 4) if stock_sent_mean is not None else None,
        "stock_sent_n":       len(stock_sents),
        "gold_sent":          gold_sentiment,
        "real10y":            real10y,
        "event":              event,
        "vix_regime":         vix_reg,
        "residual_audit":     residual_audit,
        "residuals_active":   sum(1 for a in residual_audit if a.get("verdict") == "ok"),
        "residuals_rejected": sum(1 for a in residual_audit if a.get("verdict") == "rejected"),
        "market_warning":     market_warning,
    }
    out_path = stocks_dir / "_aggregate.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {out_path}")

    # Item 5: sync per-ticker JSONs so Explorer page uses the same probability.
    written = 0
    proba_by_ticker = dict(zip(tickers_arr, live["proba"].to_numpy()))
    model_used_by_ticker = dict(zip(tickers_arr, live["model_used"].to_numpy()))
    for ticker, prob in proba_by_ticker.items():
        path = stocks_dir / f"{ticker}.json"
        if not path.exists():
            continue
        try:
            obj = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        obj["ml"] = {
            "prob_up":     round(float(prob), 4),
            "pooled_prob": round(float(proba_by_ticker[ticker]), 4),
            "source":      str(model_used_by_ticker.get(ticker, "pooled")),
            "asof_date":   str(last_date.date()),
        }
        path.write_text(json.dumps(obj, indent=2))
        written += 1
    print(f"synced ml.prob_up to {written} per-ticker JSONs")

    # Item 13: history log (one line per run) for backtest + calibration.
    log_path = stocks_dir / "_composite_history.jsonl"
    log_row = {
        "asof_close":   str(last_date.date()),
        "generated_at": now_utc.isoformat(timespec="seconds"),
        "score":        composite["score"],
        "bias":         composite["bias"],
        "confidence":   composite["confidence"],
        "components":   composite["components"],
        "weights":      composite["weights"],
        "weights_source": composite["weights_source"],
        "mean_prob_up": round(mean_p, 4),
        "breadth_up":   round(breadth, 4),
        "vix":          last_vix,
        "vix_regime":   vix_reg["regime"],
    }
    with log_path.open("a") as f:
        f.write(json.dumps(log_row) + "\n")
    print(f"appended → {log_path.name}")


if __name__ == "__main__":
    main()
