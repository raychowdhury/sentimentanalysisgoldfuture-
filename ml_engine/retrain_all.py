"""Weekly retrain orchestrator.

For each symbol in config.SYMBOL_MAP × {15m, 1h}:
    1. Backfill latest history from Databento (overwrite)
    2. Train with --macro --path-labels
    3. Log AUC + new model meta

Usage (manual):
    python -m ml_engine.retrain_all
    python -m ml_engine.retrain_all --schemas ohlcv-1h --symbols ES,NQ
"""
import argparse
import json
import logging
import time
from datetime import datetime, timezone

from ml_engine import config
from ml_engine.backfill import fetch, save
from ml_engine.models.trainer import train

logger = logging.getLogger(__name__)

DEFAULT_SCHEMAS = [config.SCHEMA_15M, "ohlcv-1h"]
# 1m needed for path-labels first-touch sim; backfill it but don't train.
PATH_FINE_SCHEMA = "ohlcv-1m"


def run(symbols: list[str] | None = None,
        schemas: list[str] | None = None,
        years: int = 2) -> dict:
    syms = symbols or list(config.SYMBOL_MAP.keys())
    schs = schemas or DEFAULT_SCHEMAS
    started = datetime.now(timezone.utc).isoformat()
    summary = {"started": started, "symbols": syms, "schemas": schs, "results": []}

    for s in syms:
        # Fine 1m needed for path-correct labels
        try:
            df = fetch(s, years, PATH_FINE_SCHEMA)
            save(df, s, PATH_FINE_SCHEMA)
        except Exception as e:
            logger.warning(f"backfill {s} 1m failed: {e}")
            summary["results"].append({"symbol": s, "stage": "backfill_1m", "error": str(e)})
            continue

        for sch in schs:
            try:
                t0 = time.time()
                df = fetch(s, years, sch)
                save(df, s, sch)
                meta = train(s, sch, include_macro=True, path_labels=True)
                summary["results"].append({
                    "symbol": s, "schema": sch,
                    "rows": meta["rows"],
                    "auc_long":  meta["long"]["test_auc"],
                    "auc_short": meta["short"]["test_auc"],
                    "elapsed_sec": round(time.time() - t0, 1),
                })
                logger.info(f"[retrain] {s} {sch} auc={meta['long']['test_auc']:.3f}/"
                            f"{meta['short']['test_auc']:.3f}")
            except Exception as e:
                logger.exception(f"retrain {s} {sch} failed")
                summary["results"].append({"symbol": s, "schema": sch, "error": str(e)})

    summary["finished"] = datetime.now(timezone.utc).isoformat()
    # Persist log
    log_path = config.ARTIFACTS_DIR / "retrain_log.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(summary, default=str) + "\n")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated, e.g. ES,NQ")
    ap.add_argument("--schemas", help="comma-separated, e.g. ohlcv-1h")
    ap.add_argument("--years", type=int, default=2)
    args = ap.parse_args()
    syms = args.symbols.split(",") if args.symbols else None
    schs = args.schemas.split(",") if args.schemas else None
    out = run(syms, schs, args.years)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
