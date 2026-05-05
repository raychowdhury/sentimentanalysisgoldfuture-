"""Paper broker stub.

Synthesizes fills based on:
  - entry_close from fire (paper assumption: market order at fire bar close)
  - 0.25 ATR adverse slippage
  - $2.50 commission per round-trip (split $1.25 entry / $1.25 exit)

Exit logic mirrors outcome tracker contract:
  - 12-bar horizon
  - 1R adverse stop (1 ATR)
  - exit at horizon close OR stop-out, whichever first

NOT a real broker. Replace this module to point OMS at a live broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

DOLLAR_PER_R = 100.0
COMMISSION_PER_RT = 2.50
SLIPPAGE_ATR = 0.25


@dataclass
class PaperFill:
    fill_id: str
    order_id: str
    signal_id: str
    symbol: str
    side: str
    qty: int
    fill_price: float
    fill_ts_utc: str
    commission: float
    slippage_dollars: float
    note: str = ""


def synth_entry_fill(order_id: str, signal_id: str, symbol: str, side: str,
                     qty: int, decision_price: float, atr: float) -> PaperFill:
    slip = SLIPPAGE_ATR * atr * (1 if side == "buy" else -1)
    fill_price = decision_price + slip
    return PaperFill(
        fill_id=f"{order_id}-entry",
        order_id=order_id,
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        qty=qty,
        fill_price=fill_price,
        fill_ts_utc=datetime.now(timezone.utc).isoformat(),
        commission=COMMISSION_PER_RT / 2,
        slippage_dollars=abs(slip),
        note="paper_entry",
    )


def synth_exit_fill(order_id: str, signal_id: str, symbol: str, side: str,
                    qty: int, exit_price: float, atr: float,
                    exit_reason: str) -> PaperFill:
    side_close = "sell" if side == "buy" else "buy"
    slip = SLIPPAGE_ATR * atr * (-1 if side_close == "buy" else 1)
    fill_price = exit_price + slip
    return PaperFill(
        fill_id=f"{order_id}-exit",
        order_id=order_id,
        signal_id=signal_id,
        symbol=symbol,
        side=side_close,
        qty=qty,
        fill_price=fill_price,
        fill_ts_utc=datetime.now(timezone.utc).isoformat(),
        commission=COMMISSION_PER_RT / 2,
        slippage_dollars=abs(slip),
        note=f"paper_exit_{exit_reason}",
    )


def is_connected() -> bool:
    return False


def broker_name() -> str:
    return "PAPER_BROKER_STUB"
