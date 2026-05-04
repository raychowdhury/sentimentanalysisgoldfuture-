"""
Train SPY-only next-day direction classifier (item 11).

Universe-aggregated predictions are noisy. SPY OHLCV is already in the
pipeline as `spy_ret_1d`/`spy_ret_5d` etc. but never gets its own model —
all rows are individual tickers. Here we build a single-row-per-day SPY
feature set and train a dedicated XGBoost classifier on SPY direction.

Use it alongside the universe-aggregate composite for a second opinion.

Outputs:
  outputs/stocks/_spy_direct_model.pkl
  outputs/stocks/_spy_direct_metrics.json

Usage:  python -m research.train_spy_direct
"""
from __future__ import annotations

import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
import yfinance as yf

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from data.pipeline import _fetch_yf, _fetch_fred, FRED_SERIES, MARKET_SYMBOLS, SECTOR_ETFS

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_PKL  = ROOT / "outputs" / "stocks" / "_spy_direct_model.pkl"
OUT_META = ROOT / "outputs" / "stocks" / "_spy_direct_metrics.json"


def _build_spy_features(lookback_days: int = 1500) -> pd.DataFrame:
    spy = _fetch_yf(MARKET_SYMBOLS["SPY"], lookback_days)
    vix = _fetch_yf(MARKET_SYMBOLS["VIX"], lookback_days)
    dxy = _fetch_yf(MARKET_SYMBOLS["DXY"], lookback_days)

    df = pd.DataFrame(index=spy.index)
    c = spy["Close"]
    df["close"]      = c
    df["ret_1d"]     = c.pct_change()
    df["ret_5d"]     = c.pct_change(5)
    df["ret_20d"]    = c.pct_change(20)
    df["vol_20d"]    = df["ret_1d"].rolling(20).std()
    ema10 = c.ewm(span=10).mean()
    ema50 = c.ewm(span=50).mean()
    df["ema_gap"]    = (ema10 - ema50) / ema50
    delta = c.diff()
    g = delta.clip(lower=0).rolling(14).mean()
    l = (-delta.clip(upper=0)).rolling(14).mean()
    rs = g / l.replace(0, np.nan)
    df["rsi_14"]     = 100 - 100 / (1 + rs)
    df["vol_ratio"]  = (spy["Volume"] / spy["Volume"].rolling(20).mean()).fillna(1.0)

    df["vix"]        = vix["Close"]
    df["vix_ret_5d"] = vix["Close"].pct_change(5)
    df["dxy_ret_5d"] = dxy["Close"].pct_change(5)

    # Sector dispersion: std of sector ETFs' 5d returns — high dispersion often = trending market
    sector_5d = []
    for _, etf in SECTOR_ETFS.items():
        try:
            sector_5d.append(_fetch_yf(etf, lookback_days)["Close"].pct_change(5).rename(etf))
        except Exception:
            pass
    if sector_5d:
        sec_df = pd.concat(sector_5d, axis=1)
        df["sector_dispersion"] = sec_df.std(axis=1)
        df["sector_avg_5d"]     = sec_df.mean(axis=1)

    fred = {name: _fetch_fred(sid) for name, sid in FRED_SERIES.items()}
    fdf = pd.concat(fred.values(), axis=1)
    fdf.columns = list(fred.keys())
    fdf = fdf.ffill()
    df["real10y"]        = fdf["REAL10Y"].reindex(df.index, method="ffill")
    df["real10y_chg_5d"] = df["real10y"].diff(5)
    df["fed"]            = fdf["FED"].reindex(df.index, method="ffill")
    df["cpi_yoy"]        = fdf["CPI"].reindex(df.index, method="ffill").pct_change(252)

    df["y_next_ret"] = df["close"].shift(-1) / df["close"] - 1.0
    df["y_next_dir"] = (df["y_next_ret"] > 0).astype(int)
    df = df.drop(columns=["close"])
    return df.dropna(subset=[c for c in df.columns if c not in ("y_next_ret", "y_next_dir")])


def main() -> None:
    df = _build_spy_features()
    df_train = df.dropna(subset=["y_next_dir"]).copy()

    cutoff = df_train.index[int(len(df_train) * 0.85)]
    train = df_train[df_train.index <  cutoff]
    valid = df_train[df_train.index >= cutoff]

    feature_cols = [c for c in df_train.columns if c not in ("y_next_dir", "y_next_ret")]
    X_tr, y_tr = train[feature_cols], train["y_next_dir"]
    X_va, y_va = valid[feature_cols], valid["y_next_dir"]

    clf = xgb.XGBClassifier(
        n_estimators=400, max_depth=4, learning_rate=0.05,
        tree_method="hist", verbosity=0, n_jobs=-1,
    )
    clf.fit(X_tr, y_tr)

    p_va = clf.predict_proba(X_va)[:, 1]
    pred_va = (p_va >= 0.5).astype(int)
    acc  = float((pred_va == y_va).mean())

    # Equity curve PnL
    signal = np.where(pred_va == 1, 1.0, -1.0)
    daily  = signal * valid["y_next_ret"].to_numpy()
    sharpe = float(np.mean(daily) / (np.std(daily) + 1e-9) * np.sqrt(252))
    eq = (1 + pd.Series(daily, index=valid.index)).cumprod()
    max_dd = float((eq / eq.cummax() - 1).min() * -1)

    importance = dict(zip(feature_cols, clf.feature_importances_.tolist()))
    meta = {
        "trained_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_train":      int(len(train)),
        "n_valid":      int(len(valid)),
        "valid_window": f"{str(valid.index.min())[:10]} → {str(valid.index.max())[:10]}",
        "accuracy":     round(acc, 4),
        "sharpe":       round(sharpe, 4),
        "max_drawdown": round(max_dd, 4),
        "pred_up_rate": round(float(pred_va.mean()), 4),
        "base_rate":    round(float(y_va.mean()), 4),
        "feature_importance": {k: round(v, 4) for k, v in
                                sorted(importance.items(), key=lambda x: -x[1])},
        "feature_cols": feature_cols,
    }

    with open(OUT_PKL, "wb") as f:
        pickle.dump({"model": clf, "features": feature_cols}, f)
    OUT_META.write_text(json.dumps(meta, indent=2))

    print(json.dumps(meta, indent=2))
    print(f"\nwrote {OUT_PKL}")
    print(f"wrote {OUT_META}")


if __name__ == "__main__":
    main()
