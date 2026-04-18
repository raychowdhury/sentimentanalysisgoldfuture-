"""
Aggregate closed-trade records into performance metrics.

Per-trade R multiple = pnl / risk  (risk = entry − stop in price units).
Win = R > 0.

Reports:
  overall summary
  breakdown by signal class  (BUY / STRONG_BUY / SELL / STRONG_SELL)
  breakdown by exit reason   (TP / STOP / TIME)
  equity curve (cumulative R)
  max drawdown (in R)
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean


def _r_multiple(trade: dict) -> float:
    risk = trade.get("risk")
    pnl  = trade.get("pnl")
    if not risk or risk <= 0 or pnl is None:
        return 0.0
    return pnl / risk


def _summary(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}

    r_mults = [_r_multiple(t) for t in trades]
    wins    = [r for r in r_mults if r > 0]
    losses  = [r for r in r_mults if r <= 0]

    total_r = sum(r_mults)
    avg_win  = mean(wins)   if wins   else 0.0
    avg_loss = mean(losses) if losses else 0.0
    expectancy = total_r / len(trades)

    return {
        "trades":      len(trades),
        "wins":        len(wins),
        "losses":      len(losses),
        "win_rate":    round(len(wins) / len(trades) * 100, 2),
        "total_r":     round(total_r,    3),
        "expectancy":  round(expectancy, 3),
        "avg_win_r":   round(avg_win,    3),
        "avg_loss_r":  round(avg_loss,   3),
        "best_r":      round(max(r_mults), 3),
        "worst_r":     round(min(r_mults), 3),
    }


def _by_key(trades: list[dict], key: str) -> dict[str, dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        buckets[t.get(key, "?")].append(t)
    return {k: _summary(v) for k, v in buckets.items()}


def _equity_curve(trades: list[dict]) -> list[float]:
    curve, eq = [], 0.0
    for t in trades:
        eq += _r_multiple(t)
        curve.append(round(eq, 3))
    return curve


def _max_drawdown(curve: list[float]) -> float:
    peak = dd = 0.0
    for eq in curve:
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return round(dd, 3)


def report(trades: list[dict]) -> dict:
    curve = _equity_curve(trades)
    return {
        "overall":          _summary(trades),
        "by_signal":        _by_key(trades, "signal"),
        "by_exit_reason":   _by_key(trades, "exit_reason"),
        "by_regime":        _by_key(trades, "regime"),
        "equity_curve_r":   curve,
        "max_drawdown_r":   _max_drawdown(curve),
    }


def print_report(trades: list[dict]) -> None:
    r = report(trades)
    o = r["overall"]
    print("\n── Backtest Report ───────────────────────────────")
    if not trades:
        print("  No trades generated.")
        return
    print(f"  Trades      : {o['trades']}")
    print(f"  Win rate    : {o['win_rate']}%  ({o['wins']}W / {o['losses']}L)")
    print(f"  Total R     : {o['total_r']:+.2f}")
    print(f"  Expectancy  : {o['expectancy']:+.3f} R / trade")
    print(f"  Avg win     : {o['avg_win_r']:+.2f} R")
    print(f"  Avg loss    : {o['avg_loss_r']:+.2f} R")
    print(f"  Best / Worst: {o['best_r']:+.2f} / {o['worst_r']:+.2f} R")
    print(f"  Max drawdown: {r['max_drawdown_r']:+.2f} R")

    print("\n  By signal:")
    for sig, s in r["by_signal"].items():
        print(f"    {sig:<12} n={s['trades']:>3}  win%={s['win_rate']:>5.1f}  "
              f"exp={s['expectancy']:+.3f}R  total={s['total_r']:+.2f}R")

    print("\n  By exit reason:")
    for reason, s in r["by_exit_reason"].items():
        print(f"    {reason:<5} n={s['trades']:>3}  win%={s['win_rate']:>5.1f}  "
              f"total={s['total_r']:+.2f}R")

    print("\n  By regime:")
    for reg, s in r["by_regime"].items():
        print(f"    {reg:<5} n={s['trades']:>3}  win%={s['win_rate']:>5.1f}  "
              f"exp={s['expectancy']:+.3f}R  total={s['total_r']:+.2f}R")
    print("──────────────────────────────────────────────────\n")
