"""Trading platform — paper broker stack.

Reads existing rule-engine fire JSONL; routes ARMED rule fires to a paper
broker; tracks orders, fills, positions, P&L, risk, audit.

NO real broker connection. NO real orders. NO real money.
NO edits to rule_engine.py / outcome_tracker.py / ml_engine/ / config.py
constants / env flags. Strategy ARM toggles are manual per-rule.

Swap broker.py to point at a real broker (IBKR/Tradovate/Rithmic/etc) when
ready. OMS, risk engine, positions, audit log are broker-agnostic.
"""
