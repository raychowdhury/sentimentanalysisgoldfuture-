"""Tests for backtest/metrics.py — R-multiple accounting."""

from backtest import metrics


def _trade(risk, pnl, signal="BUY", exit_reason="TP", regime="bull"):
    return {
        "risk": risk, "pnl": pnl,
        "signal": signal, "exit_reason": exit_reason, "regime": regime,
    }


def test_empty_report():
    r = metrics.report([])
    assert r["overall"] == {"trades": 0}
    assert r["equity_curve_r"] == []
    assert r["max_drawdown_r"] == 0.0


def test_r_multiple_and_expectancy():
    # risk=10, pnl=+20 → +2R ; risk=10 pnl=-10 → -1R ; risk=5 pnl=+5 → +1R
    trades = [_trade(10, 20), _trade(10, -10), _trade(5, 5)]
    o = metrics.report(trades)["overall"]
    assert o["trades"]      == 3
    assert o["wins"]        == 2
    assert o["losses"]      == 1
    assert o["total_r"]     == 2.0
    assert o["expectancy"]  == round(2 / 3, 3)
    assert o["best_r"]      == 2.0
    assert o["worst_r"]     == -1.0


def test_zero_or_missing_risk_is_zero_r():
    trades = [_trade(0, 5), _trade(None, 5)]
    o = metrics.report(trades)["overall"]
    assert o["total_r"] == 0.0


def test_by_signal_and_by_regime_bucketing():
    trades = [
        _trade(10, 20, signal="BUY",        regime="bull"),
        _trade(10, -10, signal="BUY",       regime="bull"),
        _trade(10, 30, signal="STRONG_BUY", regime="flat"),
    ]
    r = metrics.report(trades)
    assert r["by_signal"]["BUY"]["trades"] == 2
    assert r["by_signal"]["STRONG_BUY"]["trades"] == 1
    assert r["by_signal"]["STRONG_BUY"]["total_r"] == 3.0
    assert r["by_regime"]["bull"]["trades"] == 2
    assert r["by_regime"]["flat"]["trades"] == 1


def test_max_drawdown():
    # Equity curve: +2, +1, -1, +1, -1, -2  (cumulative)
    trades = [
        _trade(10, 20),   # +2R
        _trade(10, -10),  # +1R
        _trade(10, -20),  # -1R
        _trade(10, 20),   # +1R
        _trade(10, -20),  # -1R
        _trade(10, -10),  # -2R
    ]
    r = metrics.report(trades)
    # Peak was +2R at trade 1, trough -2R at trade 6 → drawdown -4R
    assert r["max_drawdown_r"] == -4.0


def test_equity_curve_is_cumulative():
    trades = [_trade(10, 10), _trade(10, -5), _trade(10, 10)]
    curve = metrics.report(trades)["equity_curve_r"]
    assert curve == [1.0, 0.5, 1.5]
