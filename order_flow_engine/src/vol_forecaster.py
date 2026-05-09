"""
Volatility / range forecaster for ES futures across 1m / 5m / 15m horizons.

Predicts the next-bar realized range in basis points. Outputs three quantiles
(P10 / P50 / P90) so downstream code can size stops and targets from the band
rather than a point estimate.

Inputs:
  Bars from order_flow_engine/data/processed/<SYMBOL>_<TF>_live.parquet
  (or a passed-in DataFrame). 5m bars are resampled from the 1m parquet when
  not present on disk.

Pipeline:
  1. Load bars → ensure DatetimeIndex (UTC).
  2. Reuse feature_engineering.add_orderflow_proxies for cvd / delta_ratio.
  3. Add vol-specific features: lagged realized range, abs-return lags,
     rolling vol, range expansion ratio, time-of-day (hour sin/cos), dow.
  4. Target = log1p(next-bar (H-L)/Close * 1e4).
  5. Walk-forward train one XGBRegressor per quantile (0.10, 0.50, 0.90)
     with reg:quantileerror objective.
  6. Persist (model_p10, model_p50, model_p90, feature_names, metadata) to
     order_flow_engine/models/vol_<symbol>_<tf>_<ts>.pkl.

Metrics reported per fold:
  mae_bps      — |actual_bps - pred_p50_bps|.mean()
  pinball_<q>  — quantile pinball loss in log space
  band_cover   — fraction of actuals inside [P10, P90] (target ~0.80)
  corr         — Pearson(pred_p50, actual)
"""

from __future__ import annotations

import json
import pickle
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

from order_flow_engine.src import config as of_cfg
from order_flow_engine.src import feature_engineering as fe


QUANTILES: tuple[float, ...] = (0.10, 0.50, 0.90)

# ── data load ────────────────────────────────────────────────────────────────

def _norm_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = out.index
    if not isinstance(idx, pd.DatetimeIndex):
        idx = pd.to_datetime(idx, utc=True, errors="coerce")
    elif idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    out.index = idx
    return out.sort_index()


def load_bars(symbol: str, tf: str) -> pd.DataFrame:
    """
    Load bars for (symbol, tf). Preference order, longest history first:
        1. data/raw/<symbol>_<tf>.parquet         (Databento OHLCV bulk pull)
        2. data/processed/<symbol>_<tf>_realflow_history.parquet
        3. data/processed/<symbol>_<tf>_live.parquet
        4. resample of 1m_live for tf=5m as last resort

    Raw files lack buy_vol_real / sell_vol_real — feature pipeline falls back
    to CLV proxy automatically. That's fine for vol/range target since
    forecasting realized range doesn't require true buy/sell split.
    """
    proc = of_cfg.OF_PROCESSED_DIR
    raw  = of_cfg.OF_RAW_DIR

    raw_path = raw / f"{symbol}_{tf}.parquet"
    if raw_path.exists():
        return _norm_index(pd.read_parquet(raw_path))

    hist = proc / f"{symbol}_{tf}_realflow_history.parquet"
    if hist.exists():
        return _norm_index(pd.read_parquet(hist))

    live = proc / f"{symbol}_{tf}_live.parquet"
    if live.exists():
        return _norm_index(pd.read_parquet(live))

    if tf == "5m":
        one = proc / f"{symbol}_1m_live.parquet"
        if one.exists():
            df1 = _norm_index(pd.read_parquet(one))
            agg_spec = {
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }
            for col in ("buy_vol_real", "sell_vol_real"):
                if col in df1.columns:
                    agg_spec[col] = "sum"
            return (df1.resample("5min", label="right", closed="right")
                       .agg(agg_spec)
                       .dropna(subset=["Open", "High", "Low", "Close"]))

    raise FileNotFoundError(f"No parquet for {symbol} {tf}")


# ── features ─────────────────────────────────────────────────────────────────

def _hour_encoding(idx: pd.DatetimeIndex) -> tuple[pd.Series, pd.Series]:
    """Sin/cos of hour-of-day (UTC) — captures intraday vol seasonality."""
    h = idx.hour + idx.minute / 60.0
    radians = 2 * np.pi * h / 24.0
    return (
        pd.Series(np.sin(radians), index=idx, name="hour_sin"),
        pd.Series(np.cos(radians), index=idx, name="hour_cos"),
    )


def build_vol_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build feature matrix for vol forecasting. Causal only — every column at
    timestamp t uses information from t and earlier.
    """
    base = fe.add_orderflow_proxies(df)
    out = base.copy()

    rng_bps = (out["High"] - out["Low"]) / out["Close"] * 1e4
    out["rng_bps"] = rng_bps  # current bar — known at close, target uses t+1
    out["rng_bps_lag1"]  = rng_bps.shift(1)
    out["rng_bps_lag2"]  = rng_bps.shift(2)
    out["rng_bps_lag5"]  = rng_bps.shift(5)
    out["rng_bps_lag20"] = rng_bps.shift(20)

    # log-return based features (more symmetric than pct change)
    log_ret = np.log(out["Close"]).diff()
    out["abs_ret_lag1"] = log_ret.abs().shift(1)
    out["abs_ret_lag5"] = log_ret.abs().shift(5)
    # 20-bar realized vol (Parkinson-ish via log returns)
    out["rv20"] = log_ret.rolling(20, min_periods=5).std()

    # RiskMetrics-style EWMA of |returns| — heavy weight on recent shocks,
    # smoother than rolling std. Use t-1 base so target is causal.
    abs_ret = log_ret.abs()
    out["ewma_abs_ret_fast"] = abs_ret.ewm(alpha=0.06, adjust=False).mean().shift(1)
    out["ewma_abs_ret_slow"] = abs_ret.ewm(alpha=0.02, adjust=False).mean().shift(1)
    # Recent max range — captures clustering after a shock bar.
    out["rng_max10"] = rng_bps.rolling(10, min_periods=3).max().shift(1)

    # Range expansion: current range vs trailing median
    med20 = rng_bps.rolling(20, min_periods=5).median()
    out["rng_expand"] = (rng_bps / med20).replace([np.inf, -np.inf], np.nan)

    # Volume z-score (high-vol bars cluster with high vol)
    vol = out["Volume"].astype(float)
    vmean = vol.rolling(50, min_periods=10).mean()
    vstd  = vol.rolling(50, min_periods=10).std().replace(0, np.nan)
    out["vol_z"] = ((vol - vmean) / vstd).fillna(0.0)

    # Flow features (already added by add_orderflow_proxies): delta_ratio, cvd_z
    # Lag them so target horizon is t+1
    out["delta_ratio_lag1"] = out["delta_ratio"].shift(1)
    out["cvd_z_lag1"]       = out["cvd_z"].shift(1)

    # Time-of-day
    sin_h, cos_h = _hour_encoding(out.index)
    out["hour_sin"] = sin_h
    out["hour_cos"] = cos_h
    out["dow"] = out.index.dayofweek.astype(float)

    return out


_TARGET_COL = "target_log1p_rng"


def make_target(df: pd.DataFrame, horizon: int = 1) -> pd.Series:
    """
    Target = log1p(realized_range_bps over next `horizon` bars).
    horizon=1 → next bar's high-low. horizon=N → max-high to min-low across N.
    """
    close = df["Close"]
    if horizon == 1:
        fwd_h = df["High"].shift(-1)
        fwd_l = df["Low"].shift(-1)
    else:
        fwd_h = df["High"].shift(-1).rolling(horizon, min_periods=1).max().shift(-(horizon - 1))
        fwd_l = df["Low"].shift(-1).rolling(horizon, min_periods=1).min().shift(-(horizon - 1))
    rng_bps_fwd = (fwd_h - fwd_l) / close * 1e4
    return np.log1p(rng_bps_fwd.clip(lower=0))


_FEATURE_NAMES: list[str] = [
    "rng_bps_lag1", "rng_bps_lag2", "rng_bps_lag5", "rng_bps_lag20",
    "abs_ret_lag1", "abs_ret_lag5", "rv20", "rng_expand",
    "ewma_abs_ret_fast", "ewma_abs_ret_slow", "rng_max10",
    "vol_z", "delta_ratio_lag1", "cvd_z_lag1",
    "hour_sin", "hour_cos", "dow",
]


def feature_columns() -> list[str]:
    return list(_FEATURE_NAMES)


# ── model ────────────────────────────────────────────────────────────────────

def _xgb_params(quantile: float, n_train: int) -> dict:
    """
    Adapt model capacity to training-set size. Tiny sets (<1k) overfit hard
    with deep trees and many rounds; clip to a smaller, more regularized
    model so quantile predictions don't collapse to noise.
    """
    if n_train < 1000:
        n_est, depth, mcw, lr = 120, 3, 20, 0.05
    elif n_train < 5000:
        n_est, depth, mcw, lr = 250, 4, 10, 0.05
    else:
        n_est, depth, mcw, lr = 400, 5, 5, 0.05
    return {
        "objective":         "reg:quantileerror",
        "quantile_alpha":    quantile,
        "n_estimators":      n_est,
        "max_depth":         depth,
        "learning_rate":     lr,
        "subsample":         0.8,
        "colsample_bytree":  0.8,
        "min_child_weight":  mcw,
        "n_jobs":            2,
        "random_state":      42,
        "tree_method":       "hist",
    }


def _fit_quantile(X: np.ndarray, y: np.ndarray, quantile: float):
    from xgboost import XGBRegressor
    m = XGBRegressor(**_xgb_params(quantile, len(y)))
    m.fit(X, y)
    return m


def _pinball(y_true: np.ndarray, y_pred: np.ndarray, q: float) -> float:
    diff = y_true - y_pred
    return float(np.mean(np.maximum(q * diff, (q - 1) * diff)))


def _fold_slices(n_rows: int, fold_size: int, n_folds: int):
    for k in range(n_folds):
        test_start = n_rows - (n_folds - k) * fold_size
        test_end   = test_start + fold_size
        if test_start <= fold_size:
            continue
        yield (k,
               np.arange(0, test_start),
               np.arange(test_start, min(test_end, n_rows)))


# ── eval + persist ───────────────────────────────────────────────────────────

@dataclass
class FoldReport:
    fold: int
    train_rows: int
    test_rows: int
    mae_bps: float
    corr: float
    band_cover: float
    pinball_p10: float
    pinball_p50: float
    pinball_p90: float


def _frame_to_xy(df: pd.DataFrame, horizon: int) -> tuple[pd.DataFrame, pd.Series]:
    feats = build_vol_features(df)
    y = make_target(feats, horizon=horizon)
    feats[_TARGET_COL] = y
    feats = feats.dropna(subset=_FEATURE_NAMES + [_TARGET_COL])
    X = feats[_FEATURE_NAMES].astype(float)
    return X, feats[_TARGET_COL].astype(float)


def walk_forward_eval(
    df: pd.DataFrame,
    horizon: int = 1,
    fold_size: int | None = None,
    n_folds: int | None = None,
) -> tuple[list[FoldReport], pd.DataFrame]:
    X, y = _frame_to_xy(df, horizon=horizon)
    n = len(X)
    if fold_size is None or n_folds is None:
        # Short series → more, smaller folds so each fold's train set still
        # learns something meaningful and we get >1 eval point.
        if n < 1000:
            fold_size = fold_size or max(40, n // 8)
            n_folds   = n_folds or 4
        else:
            fold_size = fold_size or min(of_cfg.WF_FOLD_SIZE, max(50, n // 5))
            n_folds   = n_folds or of_cfg.WF_N_FOLDS

    reports: list[FoldReport] = []
    pred_rows: list[pd.DataFrame] = []
    for k, tr, te in _fold_slices(len(X), fold_size, n_folds):
        Xtr, Xte = X.iloc[tr].values, X.iloc[te].values
        ytr, yte = y.iloc[tr].values, y.iloc[te].values

        models = {q: _fit_quantile(Xtr, ytr, q) for q in QUANTILES}
        preds  = {q: m.predict(Xte) for q, m in models.items()}
        p50_bps = np.expm1(preds[0.50])
        p10_bps = np.expm1(preds[0.10])
        p90_bps = np.expm1(preds[0.90])
        actual_bps = np.expm1(yte)

        mae   = float(np.mean(np.abs(actual_bps - p50_bps)))
        corr  = float(np.corrcoef(p50_bps, actual_bps)[0, 1]) if len(yte) > 1 else float("nan")
        cover = float(np.mean((actual_bps >= p10_bps) & (actual_bps <= p90_bps)))

        reports.append(FoldReport(
            fold=k, train_rows=len(tr), test_rows=len(te),
            mae_bps=mae, corr=corr, band_cover=cover,
            pinball_p10=_pinball(yte, preds[0.10], 0.10),
            pinball_p50=_pinball(yte, preds[0.50], 0.50),
            pinball_p90=_pinball(yte, preds[0.90], 0.90),
        ))
        pred_rows.append(pd.DataFrame({
            "ts":         X.iloc[te].index,
            "actual_bps": actual_bps,
            "p10_bps":    p10_bps,
            "p50_bps":    p50_bps,
            "p90_bps":    p90_bps,
            "fold":       k,
        }))

    pred_df = pd.concat(pred_rows, ignore_index=True) if pred_rows else pd.DataFrame()
    return reports, pred_df


def train_and_save(
    symbol: str,
    tf: str,
    horizon: int = 1,
    output_dir: Path | None = None,
) -> dict:
    df = load_bars(symbol, tf)
    X, y = _frame_to_xy(df, horizon=horizon)
    if len(X) < 100:
        raise RuntimeError(f"Too few rows for {symbol} {tf}: {len(X)}")

    reports, pred_df = walk_forward_eval(df, horizon=horizon)

    # Final model: trained on all data
    final = {q: _fit_quantile(X.values, y.values, q) for q in QUANTILES}

    out_dir = Path(output_dir) if output_dir else (of_cfg.OF_OUTPUT_DIR / "vol_forecast")
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = int(time.time())
    version = f"vol_{symbol}_{tf}_h{horizon}_{ts}"

    # Feature importance from the median model
    fi = pd.DataFrame({
        "feature":    _FEATURE_NAMES,
        "importance": final[0.50].feature_importances_,
    }).sort_values("importance", ascending=False)
    fi.to_csv(out_dir / f"{version}_feature_importance.csv", index=False)

    if not pred_df.empty:
        pred_df.to_csv(out_dir / f"{version}_predictions.csv", index=False)

    metadata = {
        "version":       version,
        "symbol":        symbol,
        "timeframe":     tf,
        "horizon":       horizon,
        "rows_total":    len(X),
        "feature_names": _FEATURE_NAMES,
        "quantiles":     list(QUANTILES),
        "folds":         [asdict(r) for r in reports],
        "fold_summary":  {
            "mae_bps_mean":    float(np.mean([r.mae_bps for r in reports])) if reports else None,
            "corr_mean":       float(np.mean([r.corr for r in reports])) if reports else None,
            "band_cover_mean": float(np.mean([r.band_cover for r in reports])) if reports else None,
        },
    }

    pkl_path = of_cfg.OF_MODELS_DIR / f"{version}.pkl"
    with pkl_path.open("wb") as f:
        pickle.dump({
            "models":         final,         # {0.10: m, 0.50: m, 0.90: m}
            "feature_names":  _FEATURE_NAMES,
            "horizon":        horizon,
            "metadata":       metadata,
        }, f)
    with (of_cfg.OF_MODELS_DIR / f"{version}.json").open("w") as f:
        json.dump(metadata, f, indent=2, default=str)
    with (out_dir / f"{version}_report.json").open("w") as f:
        json.dump(metadata, f, indent=2, default=str)

    return metadata


# ── inference helper ─────────────────────────────────────────────────────────

def predict_latest(model_pkg: dict, df: pd.DataFrame) -> dict:
    """
    Score the most recent bar with a saved model package. Returns
    {p10_bps, p50_bps, p90_bps, ts}.
    """
    feats = build_vol_features(df)
    feats = feats.dropna(subset=model_pkg["feature_names"])
    if feats.empty:
        raise RuntimeError("No bars with full feature row")
    last = feats.iloc[[-1]]
    X = last[model_pkg["feature_names"]].astype(float).values
    out = {q: float(np.expm1(m.predict(X)[0])) for q, m in model_pkg["models"].items()}
    return {
        "ts":      last.index[0].isoformat(),
        "p10_bps": out[0.10],
        "p50_bps": out[0.50],
        "p90_bps": out[0.90],
    }
