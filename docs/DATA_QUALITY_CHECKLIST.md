# DATA_QUALITY_CHECKLIST — RFM ES Real-Flow Monitor

> Manual verification checklist for data integrity. Use periodically and
> when investigating anomalies. Read-only — checking quality does not
> change the data.

---

## Use cases

- Pre-checkpoint review (before filling n=10 / n=15 / n=30 template)
- Multi-rule WARN investigation (cross-rule template)
- After Flask restart or recovery action
- Weekly pulse (Monday morning during open market)

---

## Severity levels

| level | meaning | action |
|---|---|---|
| 🟢 GREEN | within budget, expected behavior | none |
| 🟡 YELLOW | drifting toward unhealthy; document | annotate, monitor |
| 🔴 RED | breaks data integrity assumptions; stop trusting affected window | exclude from sample, recover, redo |

---

## Section 1 — File presence

For each, check existence and recent mtime:

| file | expected | red threshold |
|---|---|---|
| `outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl` | exists, append-only | missing or zero bytes |
| `outputs/order_flow/realflow_r7_shadow_outcomes_ESM6_15m.jsonl` | exists, append-only | missing |
| `outputs/order_flow/realflow_outcomes_pending_ESM6_15m.json` | exists, valid JSON list | missing or invalid |
| `outputs/order_flow/realflow_r7_shadow_pending_ESM6_15m.json` | exists | missing |
| `outputs/order_flow/realflow_outcomes_summary_ESM6_15m.json` | exists | older than ~30 min during open market |
| `outputs/order_flow/realflow_r7_shadow_summary_ESM6_15m.json` | exists | same |
| `outputs/order_flow/realflow_diagnostic_ESM6_15m.json` | exists | older than ~30 min during open market |
| `outputs/order_flow/.live_checkpoint_state.json` | exists | missing |
| `outputs/order_flow/.health_state.json` | exists | missing |
| `outputs/order_flow/health_monitor.log` | exists, growing | mtime > 30 min during open market |
| `outputs/order_flow/cache_refresh.log` | exists, growing | mtime > 2h during open market |
| `outputs/order_flow/monitor_loop.log` | exists, growing | mtime > 30 min during open market |

Quick check:
```bash
cd "/Users/ray/Dev/Sentiment analysis projtect"
ls -la outputs/order_flow/*.json outputs/order_flow/*.jsonl outputs/order_flow/*.log outputs/order_flow/.*state.json 2>/dev/null
```

---

## Section 2 — Live data freshness

| metric | green | yellow | red |
|---|---|---|---|
| `ESM6_1m_live.parquet` last_bar age | < 30 min | 30-60 min | > 60 min during open market |
| `ESM6_1m_live.parquet` mtime | < 35 min | 35-90 min | > 90 min during open market |
| Latest TAPE ALERT in `/tmp/flask.log` | < 5 min | 5-15 min | > 15 min during open market |
| `realflow_diagnostic.json` `freshness.label` | `fresh` or `recent` | `recent` (high lag) | `stale` |
| Live SDK lifecycle | `Live SDK started` exists, no errors | warnings | reconnect / disconnect / abort lines |

Check:
```bash
.venv/bin/python -c "
import pandas as pd, os
from datetime import datetime, timezone
p = 'order_flow_engine/data/processed/ESM6_1m_live.parquet'
df = pd.read_parquet(p)
df.index = pd.to_datetime(df.index)
if df.index.tz is None: df.index = df.index.tz_localize('UTC')
now = pd.Timestamp.now(tz='UTC')
print('last_bar:', df.index.max())
print('age_min:', round((now - df.index.max().to_pydatetime()).total_seconds()/60, 1))
print('mtime_min:', round((now.timestamp() - os.path.getmtime(p))/60, 1))
"

grep "TAPE ALERT" /tmp/flask.log | tail -3
```

---

## Section 3 — Raw cache integrity

| metric | green | yellow | red |
|---|---|---|---|
| `data/raw/ESM6_15m.parquet` last_bar age | < 60 min | 60-120 min | > 120 min during open market |
| `data/raw/ESM6_15m.parquet` mtime | < 65 min | 65-90 min | > 120 min during open market |
| `cache_refresh.log` last entry | OK with raw_max recent | SKIP within last hour | FAIL or no entries > 2h |
| `cache_refresh_FAILED.flag` | absent | absent | present (3+ consecutive failures) |

Check:
```bash
.venv/bin/python -c "
import pandas as pd
df = pd.read_parquet('order_flow_engine/data/raw/ESM6_15m.parquet')
df.index = pd.to_datetime(df.index)
print('raw last_bar:', df.index.max())
print('raw n bars:', len(df))
"
tail -3 outputs/order_flow/cache_refresh.log | grep -E "OK:|SKIP:|FAIL:"
ls outputs/order_flow/cache_refresh_FAILED.flag 2>/dev/null
```

---

## Section 4 — Joined window integrity

| metric | green | yellow | red |
|---|---|---|---|
| `realflow_diagnostic.json` `joined.n_bars` | matches expected (~1300+) | drops by < 5% vs prior | drops by ≥ 5% with no explanation |
| `joined.end` | within 30 min of now during open market | within 60 min | > 60 min behind |
| `joined.start` | stable across runs | shifts by 1-2 bars | shifts by > 5 bars (history rebuild) |
| `monitoring.bars_added_today` | > 0 during open market | 0 if pre-open | 0 during open market = ingest stalled |

Check:
```bash
.venv/bin/python -c "
import json
d = json.load(open('outputs/order_flow/realflow_diagnostic_ESM6_15m.json'))
j = d['joined']
print('joined window:', j['start'], '->', j['end'])
print('n_bars:', j['n_bars'])
print('bars added today:', d['monitoring']['bars_added_today'])
"
```

---

## Section 5 — Outcome tracker integrity

| check | green | yellow | red |
|---|---|---|---|
| pending file is valid JSON list | always | n/a | parse error |
| settled JSONL strictly increasing | append-only respected | n/a | row count drops |
| every settled row has `signal_id`, `rule`, `mode`, `outcome`, `fwd_r_signed` | always | missing optional fields | missing required fields |
| pending row's `settle_eta_utc` matches `fire_ts + 12 * tf_min` | always | minor float drift | mismatch by > 1 minute |
| live mode rows have `entry_close` price within ±1% of historical for same ts | always | small drift | major price diff (data corruption) |
| `n_settled` in summary == row count in JSONL | always | n/a | mismatch |

Check:
```bash
.venv/bin/python <<'PY'
import json
from pathlib import Path
p = Path('outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl')
rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
print(f'JSONL rows: {len(rows)}')
required = {'signal_id', 'rule', 'mode', 'outcome', 'fwd_r_signed'}
bad = [r for r in rows if not required.issubset(r.keys())]
print(f'rows missing required fields: {len(bad)}')
modes = {}
for r in rows:
    modes[r.get('mode')] = modes.get(r.get('mode'), 0) + 1
print(f'mode distribution: {modes}')
s = json.load(open('outputs/order_flow/realflow_outcomes_summary_ESM6_15m.json'))
print(f'summary n_settled: {s["n_settled"]}, JSONL count: {len(rows)}, match: {s["n_settled"] == len(rows)}')
PY
```

---

## Section 6 — Mode tagging integrity

| check | green | yellow | red |
|---|---|---|---|
| every fire's `mode` ∈ {historical, live, unknown} | always | unknown rare | unknown common |
| live rows are within rolling 1m tail window OR realflow_history overlap | always | n/a | live rows from far past dates |
| historical rows are within `realflow_history.parquet` index | always | n/a | historical rows for ts not in history |
| recent fires (within 8h) eventually tagged live (not historical) at settle | always | rare exceptions | most fires settle as historical (1m tail too short) |

Check (last 20 settled):
```bash
.venv/bin/python <<'PY'
import json
from pathlib import Path
p = Path('outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl')
rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
for r in rows[-20:]:
    print(f"{r['fire_ts_utc']}  mode={r['mode']}  rule={r['rule']}  outcome={r['outcome']}")
PY
```

---

## Section 7 — Settlement integrity

| check | green | yellow | red |
|---|---|---|---|
| pending fires whose `settle_eta_utc` is in the past | none > 30 min past | 1-3 fires < 6h past | many > 6h past |
| forward bar at fire_ts + 12*tf must exist in joined frame for settle to happen | always | n/a | missing forward bar despite wall-clock past |
| `mae_r` ≤ 0 always | always | n/a | mae_r > 0 (logic inversion) |
| `mfe_r` ≥ 0 always | always | n/a | mfe_r < 0 (logic inversion) |
| `outcome` ∈ {win, loss, flat} | always | n/a | other values |
| `hit_1r` is bool | always | n/a | string or null |
| `stopped_out_1atr` is bool | always | n/a | string or null |

Check:
```bash
.venv/bin/python <<'PY'
import json
from pathlib import Path
from datetime import datetime, timezone

pend = json.load(open('outputs/order_flow/realflow_outcomes_pending_ESM6_15m.json'))
now = datetime.now(timezone.utc)
overdue = []
for r in pend:
    eta = datetime.fromisoformat(r['settle_eta_utc'].replace('Z','+00:00'))
    age_h = (now - eta).total_seconds() / 3600
    if age_h > 0.5:
        overdue.append((r['rule'], r['fire_ts_utc'], round(age_h, 1)))
print(f'overdue pending: {len(overdue)}')
for o in overdue[:10]: print(f'  {o}')

p = Path('outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl')
rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
bad_mae = [r for r in rows if r.get('mae_r', 0) > 0]
bad_mfe = [r for r in rows if r.get('mfe_r', 0) < 0]
bad_outcome = [r for r in rows if r.get('outcome') not in ('win','loss','flat')]
print(f'mae_r > 0 (should be ≤0): {len(bad_mae)}')
print(f'mfe_r < 0 (should be ≥0): {len(bad_mfe)}')
print(f'outcome not in {{win,loss,flat}}: {len(bad_outcome)}')
PY
```

---

## Section 8 — Volume reconciliation

| check | green | yellow | red |
|---|---|---|---|
| `volume_recon.mean` (real_total / cache_volume) | 0.95-1.05 | 0.85-1.15 | < 0.85 or > 1.15 |
| `volume_recon.n_mismatch_5pct / n_bars` | < 0.30 | 0.30-0.55 | > 0.55 |
| `monitoring.session_split.RTH.mismatch_5pct_rate` | < 0.40 | 0.40-0.60 | > 0.60 |
| `monitoring.session_split.ETH.mismatch_5pct_rate` | < 0.55 | 0.55-0.70 | > 0.70 |

ETH mismatches are higher by design (thinner liquidity, asymmetric prints).
Phase 1G investigation already documented this.

Check:
```bash
.venv/bin/python -c "
import json
d = json.load(open('outputs/order_flow/realflow_diagnostic_ESM6_15m.json'))
v = d['volume_recon']
print(f'mean: {v[\"mean\"]} | mismatch_rate: {v[\"n_mismatch_5pct\"]}/{v[\"n_bars\"]} = {v[\"n_mismatch_5pct\"]/v[\"n_bars\"]:.2%}')
ss = d['monitoring']['session_split']
for sess, s in ss.items():
    print(f'  {sess}: mismatch_5pct_rate={s[\"mismatch_5pct_rate\"]}')
"
```

---

## Section 9 — Drift / regime stability

| check | green | yellow | red |
|---|---|---|---|
| `delta_ratio_drift.drift_pct` (rolling 100 vs current) | < 0.10 | 0.10-0.25 | > 0.25 |
| `vol_match.median` | 0.98-1.02 | 0.95-1.05 | outside 0.95-1.05 |
| `vol_match.iqr` | tight | widening 5-10% | widening > 10% over week |

---

## Section 10 — bar_proxy_mode integrity (Phase 2A invariant)

When `OF_REAL_THRESHOLDS_ENABLED=1`, every bar in the joined window with
real-flow data should have `bar_proxy_mode == 0`. Any bar with
`bar_proxy_mode == 1` means real-flow features were unavailable for that
bar and the proxy threshold path was used instead.

| check | green | yellow | red |
|---|---|---|---|
| % of joined bars with `bar_proxy_mode == 0` | > 95% | 90-95% | < 90% |
| % of joined bars with `bar_proxy_mode == 1` | < 5% | 5-10% | > 10% |
| `bar_proxy_mode` flips during a session | none in middle | 1-2 transitions | > 2 transitions in same session |

Check (sample 100 joined bars):
```bash
.venv/bin/python <<'PY'
import pandas as pd
from order_flow_engine.src import realflow_compare as rfc
raw, real, common, proxy_bars, real_bars, proxy_feat, real_feat = rfc._load_pair('ESM6', '15m')
df = real_feat.copy()
if 'bar_proxy_mode' in df.columns:
    pm = df['bar_proxy_mode'].fillna(1).astype(int)
    print(f'bar_proxy_mode==0 (real): {(pm==0).sum()} / {len(pm)} = {(pm==0).mean():.2%}')
    print(f'bar_proxy_mode==1 (proxy): {(pm==1).sum()} / {len(pm)} = {(pm==1).mean():.2%}')
else:
    print('NO bar_proxy_mode column — investigate')
PY
```

---

## Section 11 — Rule fire counts (cumulative joined window)

| rule | green range | yellow | red |
|---|---|---|---|
| R1 | 100-200 (depends on lookback) | sudden spike +50 in one day | sudden drop ≥ 30% |
| R2 | 100-200 | spike | drop |
| R3 absorption_resistance | 0-10 | n/a | spike > 10 |
| R4 absorption_support | 0-5 | n/a | n/a |
| R5 bull_trap | 5-30 | spike | n/a |
| R6 bear_trap | 0-5 | n/a | n/a |
| R7 cvd_divergence | 5-20 | spike | n/a |

Check:
```bash
.venv/bin/python -c "
import json
d = json.load(open('outputs/order_flow/realflow_diagnostic_ESM6_15m.json'))
print(d['monitoring']['real_fires_cumulative'])
"
```

---

## Section 12 — Live tail freeze sanity

The 15m_live.parquet should NOT exist (workaround: rename to .stale_<UTC>).
The 1m parquet should exist and update every ~25 minutes.

| check | green | yellow | red |
|---|---|---|---|
| `ESM6_15m_live.parquet` exists | absent | n/a | present after Flask boot (apply rename) |
| `ESM6_1m_live.parquet` mtime delta vs prior 25 min | reasonable | 26-30 min | > 30 min during open market |

```bash
ls "/Users/ray/Dev/Sentiment analysis projtect/order_flow_engine/data/processed/ESM6_15m_live.parquet" 2>&1 || echo "✅ absent (correct)"
```

---

## Section 13 — Schema sanity

| check | green | yellow | red |
|---|---|---|---|
| live parquet columns include `Open, High, Low, Close, Volume, buy_vol_real, sell_vol_real` | always | n/a | column missing |
| `buy_vol_real`, `sell_vol_real` non-negative | always | n/a | negative values |
| `Open ≤ High`, `Open ≥ Low`, `Close ≤ High`, `Close ≥ Low` | always | n/a | OHLC invariant violated |
| index is timezone-aware UTC | always | n/a | naive datetime |
| no duplicated index values | always | n/a | duplicates |

Check:
```bash
.venv/bin/python <<'PY'
import pandas as pd
df = pd.read_parquet('order_flow_engine/data/processed/ESM6_1m_live.parquet')
df.index = pd.to_datetime(df.index)
required = {'Open','High','Low','Close','Volume','buy_vol_real','sell_vol_real'}
print(f'columns: {set(df.columns)}')
print(f'missing required: {required - set(df.columns)}')
print(f'duplicate index: {df.index.duplicated().sum()}')
print(f'tz-aware: {df.index.tz is not None}')
neg_buy = (df['buy_vol_real'] < 0).sum() if 'buy_vol_real' in df.columns else 'N/A'
print(f'negative buy_vol_real: {neg_buy}')
ohlc_violations = ((df['Open'] > df['High']) | (df['Open'] < df['Low']) | (df['Close'] > df['High']) | (df['Close'] < df['Low'])).sum()
print(f'OHLC invariant violations: {ohlc_violations}')
PY
```

---

## Section 14 — Cross-source consistency

When two sources cover the same timestamp:

| check | green | yellow | red |
|---|---|---|---|
| `realflow_history` close ↔ `live` close at overlap ts | identical or ±0.25 (1 tick) | ±0.50 | > ±1.00 |
| `realflow_history` Volume ↔ raw cache Volume at overlap ts | within 5% | within 15% | > 15% |
| Joined frame size before / after history backfill | stable | shrinks by 1-5 bars | shrinks by > 10 bars |

---

## Section 15 — Health probe correlation

Cross-check `_summary` line in `health_monitor.log` against actual file
state. They should agree.

| probe | what it should reflect |
|---|---|
| s1_flask_proc | `pgrep python.*app.py` returns ≥ 1 PID |
| s4_live_1m_freshness | 1m last_bar age check |
| s5_15m_parquet_absent | `ESM6_15m_live.parquet` doesn't exist |
| s6_raw_freshness | raw last_bar age |
| s9_cache_refresh_log | `cache_refresh.log` mtime |
| s11_disk | `df` reports < 90% used |
| s12_pending_backlog | combined pending count < 50 |
| s13_checkpoints | matches `.live_checkpoint_state.json` |

If a probe says healthy but the file says otherwise → bug in probe or stale
state. If a probe says unhealthy but the file looks fine → flag stuck;
manually delete the flag.

---

## Run-everything shortcut

The daily research report and expected-move tools were removed on
2026-05-04. Use this checklist directly, or read the live state files
listed above (`.health_state.json`, `.live_checkpoint_state.json`,
`realflow_outcomes_summary_*.json`).

---

## When to escalate

- 🔴 RED on Section 5 (outcome tracker integrity) → stop trusting current
  sample, do not act on checkpoint until resolved
- 🔴 RED on Section 10 (`bar_proxy_mode` flips) → mixed-mode contamination,
  rebuild outcomes excluding affected bars
- 🔴 RED on Section 7 (settlement integrity logic violation) → bug
  candidate; freeze and investigate, do not act
- Multiple yellows simultaneously across Sections 2, 3, 4 → likely a
  common-mode failure (e.g., Mac sleeping, Live SDK silent). Use cross-rule
  template to investigate.

---

_Read-only checklist. Reverse by deleting this file. Update as new failure
modes are observed._
