"""Backfill multi-year OHLCV from Databento -> parquet history files.

Usage:
    python -m ml_engine.backfill GC --years 5 --schema ohlcv-15m
"""
import argparse
import os
from datetime import date, timedelta

import pandas as pd
from dotenv import load_dotenv

from ml_engine import config

load_dotenv()


_NATIVE_SCHEMAS = {"ohlcv-1m", "ohlcv-1h", "ohlcv-1d"}
_RESAMPLE_RULE = {
    "ohlcv-15m": ("ohlcv-1m", "15min"),
    "ohlcv-5m":  ("ohlcv-1m", "5min"),
    "ohlcv-30m": ("ohlcv-1m", "30min"),
    "ohlcv-4h":  ("ohlcv-1h", "4h"),
}


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min",
           "Close": "last", "Volume": "sum"}
    return df.resample(rule).agg(agg).dropna(subset=["Open", "Close"])


def fetch(symbol: str, years: int, schema: str) -> pd.DataFrame:
    import databento as db
    key = os.getenv("DATABENTO_API_KEY")
    if not key:
        raise SystemExit("DATABENTO_API_KEY missing from .env")

    client = db.Historical(key)
    sym = config.SYMBOL_MAP[symbol]
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=int(365.25 * years))

    if schema in _NATIVE_SCHEMAS:
        native_schema, resample_rule = schema, None
    elif schema in _RESAMPLE_RULE:
        native_schema, resample_rule = _RESAMPLE_RULE[schema]
        print(f"[resample] target {schema} -> fetch {native_schema}, resample {resample_rule}")
    else:
        raise SystemExit(f"Unsupported schema: {schema}")

    print(f"[databento] {sym} {native_schema} {start} -> {end}")
    data = client.timeseries.get_range(
        dataset=config.DATASET,
        symbols=sym,
        stype_in=getattr(config, "STYPE_IN", "parent"),
        schema=native_schema,
        start=start.isoformat(),
        end=end.isoformat(),
    )
    df = data.to_df()
    rename = {"open": "Open", "high": "High", "low": "Low",
              "close": "Close", "volume": "Volume"}
    df = df.rename(columns=rename)
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if resample_rule:
        df = _resample(df, resample_rule)
        print(f"[resample] {len(df)} bars after {resample_rule}")
    return df


def save(df: pd.DataFrame, symbol: str, schema: str) -> str:
    tag = schema.replace("ohlcv-", "")
    path = config.HISTORY_DIR / f"{symbol}_{tag}_history.parquet"
    df.to_parquet(path)
    print(f"[saved] {path}  rows={len(df)}")
    return str(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", choices=list(config.SYMBOL_MAP.keys()))
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--schema", default=config.SCHEMA_15M,
                    choices=[config.SCHEMA_1M, config.SCHEMA_15M, "ohlcv-1h", "ohlcv-1d"])
    args = ap.parse_args()

    df = fetch(args.symbol, args.years, args.schema)
    save(df, args.symbol, args.schema)


if __name__ == "__main__":
    main()
