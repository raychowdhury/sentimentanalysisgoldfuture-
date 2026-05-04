# RUNBOOK — RFM ES Real-Flow Monitor

_Last updated: 2026-05-01 UTC_

Common commands and recovery steps.

## Daily ops

### Attach to running tmux

```bash
tmux a -t rfm
```

Windows:
- 0: Flask (`app.py` — Live SDK auto-starts)
- 1: htop
- 2: monitor_loop (diagnose + settle_pass + r7_shadow every 15m)
- 3: spare shell

`Ctrl-B D` to detach. `Ctrl-B 0/1/2/3` to switch.

### SSH tunnel + dashboard

```bash
# from laptop
ssh -i ~/.ssh/rfm-ec2 -N -L 5001:localhost:5001 sentiment@<ec2-ip>
# browser:
http://localhost:5001/order-flow/realflow-diagnostic?symbol=ESM6&tf=15m
```

### Manual diagnostic regenerate

```bash
cd ~/sentiment-analysis-projtect
. .venv/bin/activate
set -a && source .env && set +a
.venv/bin/python -m order_flow_engine.src.realflow_compare \
    --symbol ESM6 --tf 15m --diagnostic
```

### Manual outcome tracker (production R1/R2)

```bash
.venv/bin/python -m order_flow_engine.src.realflow_outcome_tracker \
    --symbol ESM6 --tf 15m
```

### Manual R7 shadow pass

```bash
.venv/bin/python -m order_flow_engine.src.realflow_r7_shadow \
    --symbol ESM6 --tf 15m
```

### Refresh OHLCV cache (Databento Historical)

```bash
.venv/bin/python -m order_flow_engine.src.data_loader \
    --symbol ESM6 --no-cache
```

### Extend historical backfill window

```bash
# edit LOOKBACK_DAYS_CAP in order_flow_engine/src/realflow_history_backfill.py
.venv/bin/python -m order_flow_engine.src.realflow_history_backfill \
    --symbol ESM6 --tf 15m --lookback-days N
```

Currently set to 18.

## Restart

### Restart Flask only (keeps monitor running)

```bash
pkill -f "python.*app.py" && sleep 1
cd ~/sentiment-analysis-projtect && set -a && source .env && set +a
.venv/bin/python app.py > /tmp/flask.log 2>&1 &
```

### Restart everything

```bash
pkill -f "python.*app.py"
pkill -f "python.*monitor_loop"
sleep 1
tmux kill-session -t rfm 2>/dev/null
tmux new -s rfm
# re-run §Daily ops Window 0 + Window 2 commands
```

### Stop EC2 instance (save money)

AWS Console → EC2 → Instance → Stop. Storage persists. Public IP changes on restart.

## Diagnostics

### Is Flask up?

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:5001/order-flow/realflow-diagnostic
```

### Is Live SDK ingesting?

```bash
curl -sS http://localhost:5001/api/order-flow/poll/status
grep -E "Live SDK|TAPE ALERT|front-month" /tmp/flask.log | tail -10
```

### Are bars accumulating?

```bash
.venv/bin/python -c "
import pandas as pd
for f in ['ESM6_1m_live.parquet', 'ESM6_15m_live.parquet']:
    p = f'order_flow_engine/data/processed/{f}'
    df = pd.read_parquet(p)
    print(f, 'rows', len(df), 'idx_max', df.index.max())
"
```

### Latest outcomes summary

```bash
.venv/bin/python -c "
import json
s = json.load(open('outputs/order_flow/realflow_outcomes_summary_ESM6_15m.json'))
print('settled:', s['n_settled'], 'label:', s['sample_size_label'])
print('R1:', s['by_rule'].get('r1_buyer_down'))
print('R2:', s['by_rule'].get('r2_seller_up'))
"
```

### Live-only outcomes

```bash
.venv/bin/python -c "
import json, pandas as pd
rows = [json.loads(l) for l in open('outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl') if l.strip()]
df = pd.DataFrame(rows)
live = df[df['mode']=='live']
print('live n:', len(live))
print(live.groupby('rule').size())
"
```

### Shadow R7 status

```bash
.venv/bin/python -c "
import json
s = json.load(open('outputs/order_flow/realflow_r7_shadow_summary_ESM6_15m.json'))
print('shadow settled:', s['n_settled'], 'pending:', s['n_pending'])
print('hit_rate:', s.get('by_session', {}))
"
```

### Tail monitor loop log

```bash
tail -f outputs/order_flow/monitor_loop.log
```

## Recovery

### Live SDK Databento 422

`data_schema_not_fully_available` on parent-symbol resolve. Upstream issue, not code.
- Retry by restarting Flask in 5–15 min.
- Check Databento status page.
- Optional: pin raw symbol in `.env`:
  ```
  OF_DATABENTO_SYMBOLS=ESM6
  ```

### Stale loader after editing `realflow_loader.py`

Flask caches modules. Restart:
```bash
pkill -f "python.*app.py" && sleep 1
.venv/bin/python app.py > /tmp/flask.log 2>&1 &
```

### Corrupted history parquet

Backfill rewrites it; `.parquet.bak` exists if previous.
```bash
mv order_flow_engine/data/processed/ESM6_15m_realflow_history.parquet.bak \
   order_flow_engine/data/processed/ESM6_15m_realflow_history.parquet
```

### Reset outcome tracker (e.g. schema change)

```bash
rm outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl
.venv/bin/python -m order_flow_engine.src.realflow_outcome_tracker \
    --symbol ESM6 --tf 15m
```

Tracker rebuilds JSONL from current flagged events on real-flow path.

### Roll back Phase 2A real thresholds

```bash
# .env
OF_REAL_THRESHOLDS_ENABLED=0
# restart Flask
```

All bars revert to proxy thresholds. Detector behavior identical to pre-Phase-2A.

## Tests

```bash
.venv/bin/python -m pytest order_flow_engine/tests/ -q
```

Expect: 112 pass + 2 pre-existing failures (telegram, R7 rule_engine — unrelated).

## Config reference

| file | purpose |
|------|---------|
| `.env` | secrets (DATABENTO_API_KEY, env flags) |
| `order_flow_engine/src/config.py` | thresholds, paths, alert gating |
| `templates/realflow_diagnostic.html` | dashboard UI |
| `outputs/order_flow/` | all generated diagnostics, outcomes, summaries |
