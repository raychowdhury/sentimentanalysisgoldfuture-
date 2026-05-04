"""Load history + live parquet, return aligned OHLCV frame."""
from pathlib import Path

import pandas as pd

from ml_engine import config

LIVE_ROOT = Path(__file__).resolve().parents[1] / "order_flow_engine" / "data" / "processed"


def _live_path(symbol: str, schema: str) -> Path | None:
    """Map symbol+schema to live parquet (e.g. GC + ohlcv-15m -> GCM6_15m_live.parquet).

    Live files use front-month root (GCM6 etc); pick first match.
    """
    tag = schema.replace("ohlcv-", "")
    for p in LIVE_ROOT.glob(f"{symbol}*_{tag}_live.parquet"):
        return p
    return None


def load(symbol: str, schema: str = config.SCHEMA_15M) -> pd.DataFrame:
    """Concat history + live, dedupe on index, sort."""
    tag = schema.replace("ohlcv-", "")
    hist = config.HISTORY_DIR / f"{symbol}_{tag}_history.parquet"

    frames = []
    if hist.exists():
        frames.append(pd.read_parquet(hist))

    live = _live_path(symbol, schema)
    if live and live.exists():
        df_live = pd.read_parquet(live)
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df_live.columns]
        frames.append(df_live[keep])

    if not frames:
        raise FileNotFoundError(
            f"No data for {symbol} {schema}. Run: python -m ml_engine.backfill {symbol}"
        )

    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = _filter_roll_artifacts(df)
    return df


def _filter_roll_artifacts(df: pd.DataFrame, max_jump: float = 0.03) -> pd.DataFrame:
    """Drop bars whose Open jumped >max_jump vs prior Close — continuous-symbol
    contract roll artifacts. Iterates until stable."""
    if df.empty:
        return df
    while True:
        prev_close = df["Close"].shift(1)
        gap = (df["Open"] - prev_close).abs() / prev_close
        bad = gap > max_jump
        # Also flag bars whose own High/Low range relative to prev close is huge
        bad |= (df["Close"] - prev_close).abs() / prev_close > max_jump
        bad = bad.fillna(False)
        if not bad.any():
            break
        df = df.loc[~bad].copy()
    return df
