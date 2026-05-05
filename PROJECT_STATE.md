# PROJECT_STATE — RFM ES Real-Flow Monitor

_Last updated: 2026-05-04T17:30Z (R1 n=10 + R2 n=15 finalized; R7 shadow n=12 status refresh; R2 Deceleration Watch policy approved; Live SDK Watchdog Stage 1 read-only built)_

Decision-focused snapshot. Update after every major change.

## Phase status

| phase | status | note |
|-------|--------|------|
| 1A — real-flow comparison | done | `realflow_compare.py` |
| 1B — diagnostic | done | per-bar trace + threshold sensitivity |
| 1C — dashboard view | done | `/order-flow/realflow-diagnostic` |
| 1D — readiness plan | done | 5 gates defined |
| 1E — monitoring extension | done | drift/vol/session/fires |
| 1F — historical backfill | done | `realflow_history_backfill.py`, cap=18d |
| 1G — volume recon investigation | done | RTH-open/close auction skew identified |
| 1H — denominator switch test | done | no-op confirmed; threshold retune chosen instead |
| **2A — R1/R2/R3-R6 real-flow thresholds** | **ACTIVE — R2 n=10 KEEP, R1 n=7 WARN** | calibrated, env-flagged; R2 first checkpoint passed 2026-05-04 |
| 2B — R7 calibration | **deferred → shadow-only — n=10 STAY-SHADOW** | shadow tracking continues; production -0.50 untouched (zero fires per `r7_shadow_vs_production.md`) |
| 2C — direction-sign investigation | **substantially weakened by n=11 evidence** | first 2 long shadow fires both wins; sign-bug hypothesis no longer leading explanation |
| 2D Stage 1 — outcome tracker | done | `realflow_outcome_tracker.py` |
| 2D Stage 2 — dashboard surface | done | "Phase 2A Live Outcomes" card |
| 2D Stage 3 — decision logic | NOT scoped | manual revert via env flag only |

## Current production thresholds

| constant | value | path |
|----------|-------|------|
| `RULE_DELTA_DOMINANCE` | 0.30 | proxy |
| `RULE_ABSORPTION_DELTA` | 0.50 | proxy |
| `RULE_TRAP_DELTA` | 0.30 | proxy |
| `RULE_DELTA_DOMINANCE_REAL` | **0.04** | real (Phase 2A) |
| `RULE_ABSORPTION_DELTA_REAL` | **0.20** | real (Phase 2A) |
| `RULE_TRAP_DELTA_REAL` | **0.12** | real (Phase 2A) |
| `RULE_CVD_CORR_THRESH` | -0.50 | both paths (R7) |
| `RULE_CVD_CORR_WINDOW` | 20 | both paths (R7) |

Path is selected per-bar by `bar_proxy_mode` column inside `rule_engine.apply_rules` when `OF_REAL_THRESHOLDS_ENABLED=1`.

## Shadow-only constants (NOT in config.py)

| constant | value | location |
|----------|-------|----------|
| `RULE_CVD_CORR_THRESH_REAL_SHADOW` | -0.20 | `realflow_r7_shadow.py` module-local |

Shadow tracks what R7 would do at -0.20 without firing in production.

## Active env flags

```
OF_REAL_THRESHOLDS_ENABLED=1     # Phase 2A R1/R2/R3-R6 real-flow thresholds active
OF_DATABENTO_ENABLED=1
OF_DATABENTO_LIVE=1
OF_DATABENTO_SYMBOLS=ESM6
OF_DATABENTO_TFS=1m,15m
```

Rollback: set `OF_REAL_THRESHOLDS_ENABLED=0` to revert all bars to proxy thresholds.

## Active commands

```bash
# Flask (auto-starts Databento Live SDK + outcome_tracker thread)
.venv/bin/python app.py

# Monitor loop — diagnose + settle_pass + r7_shadow every 15 min
.venv/bin/python -m order_flow_engine.src.monitor_loop \
    --symbol ESM6 --tf 15m --interval 900 \
    --log outputs/order_flow/monitor_loop.log
```

Both run inside tmux session `rfm`. Dashboard: `http://localhost:5001/order-flow/realflow-diagnostic?symbol=ESM6&tf=15m` via SSH tunnel.

## Operational automation

Hourly raw OHLCV cache refresh is active via launchd.

```
Label:           com.rfm.cache-refresh
Script:          scripts/cache_refresh_esm6.sh
Plist:           ~/Library/LaunchAgents/com.rfm.cache-refresh.plist
Cadence:         StartInterval=3600 (1h), RunAtLoad=true
Lock:            POSIX mkdir atomic at /tmp/rfm-cache-refresh.lockdir
mtime skip:      30 min — skip if raw last_bar < 30 min old
Log:             outputs/order_flow/cache_refresh.log
Failure flag:    outputs/order_flow/cache_refresh_FAILED.flag (after 3 consecutive fails)
Stop:            launchctl unload ~/Library/LaunchAgents/com.rfm.cache-refresh.plist
```

Purpose: refresh raw ESM6 15m OHLCV cache hourly so joined window stays aligned with live tail and pending outcomes can settle (joined-window cap = `raw.index ∩ real.index`; raw is the bottleneck since live SDK already streams `real` forward in real-time).

Notes:
- Uses `data_loader --symbol ESM6 --no-cache`
- Does NOT run `realflow_history_backfill` (manual only — heavy trades fetch)
- Does NOT change rules, thresholds, models, ml_engine, predictor, alert_engine, ingest, or trading behavior
- Read+append only on output files

## Health Monitor Phase 1

Status: ACTIVE

Purpose: observe the local ES real-flow monitor stack every 5 minutes and notify if anything becomes unhealthy.

Files:
- `scripts/health_monitor.py`
- `scripts/health_monitor.sh`
- `~/Library/LaunchAgents/com.rfm.health-monitor.plist`
- `outputs/order_flow/health_monitor.log`
- `outputs/order_flow/.health_state.json`
- `outputs/order_flow/HEALTH_*.flag`

Launchd label:
- `com.rfm.health-monitor`

Cadence:
- every 5 minutes

Current behavior:
- observe/log/notify only
- no self-healing yet
- macOS notifications via `osascript`
- flags created on unhealthy transition and removed on recovery

Current probes:
- Flask process
- Flask HTTP 200
- Live SDK tape alerts
- ESM6 1m live parquet freshness
- stale ESM6 15m live parquet absence
- raw OHLCV freshness
- monitor_loop process
- cache refresh launchd status
- cache_refresh.log freshness
- cache_refresh_FAILED.flag
- disk usage
- pending backlog

Latest first run:
- all 12 probes healthy
- pending backlog: 1
- disk used: 39.3%

Stop command:
```bash
launchctl unload ~/Library/LaunchAgents/com.rfm.health-monitor.plist
```

## Trader-Friendly Dashboard Redesign

Status: ACTIVE

What changed:
- Real-flow Diagnostic now uses a 4-layer layout.
- Layer 1 is always visible:
  - Trader View
  - Live Signal Trade Details
- Layer 2 is collapsed:
  - Analyst details
  - Phase 2A outcomes
  - R7 Shadow
  - Daily metrics
  - Joined Window
- Layer 3 is collapsed:
  - Technical diagnostics
  - Generated/Freshness
  - Phase gates
  - Distribution
  - Volume reconciliation
  - Threshold sensitivity
  - Top diff bars
  - Per-bar trace
- Layer 4 is collapsed:
  - Help / glossary

Backup:
`templates/realflow_diagnostic.html.pre-redesign-20260501T183358Z`

Current badges:
- System: Warning
- Live data: Connected
- R1: Watching
- R2: Watching
- R7: Shadow only
- Action: Keep monitoring

Note:
System warning is likely transient from the 15m parquet rename workaround and should resolve after next health monitor check.

Operational note:
After Flask restart, stale `ESM6_15m_live.parquet` reappeared and was renamed to:
`ESM6_15m_live.parquet.stale_20260501T184624Z`

Verification:
- Flask HTTP 200
- No template errors
- monitor_loop joined_n: 1322
- R1/R2 settled: 246
- R7 shadow: 100 settled, 0 pending

No changes to:
- rules
- thresholds
- config
- models
- `ml_engine/`
- predictor
- alert_engine
- ingest
- trading logic
- outcome scoring

## Live Checkpoint Tracker

Status: ACTIVE

Purpose:
Track early live validation checkpoints for R1, R2, and R7 shadow without waiting silently for n=30.

Signals tracked:
- R1 live
- R2 live
- R7 shadow live

Checkpoints:
- n >= 10: early warning review
- n >= 15: soft decision review
- n >= 30: first verdict review

Current status:
- R1 n=5, WAITING
- R2 n=7, WAITING
- R7 shadow under 10, WAITING
- all checkpoint cells currently NOT_REACHED / WAITING

State file:
`outputs/order_flow/.live_checkpoint_state.json`

Health monitor:
- added S13 checkpoint probe
- first run silently pre-seeded state
- future transitions notify with macOS notification
- no self-healing
- no auto-retune
- no auto-promotion

Dashboard:
- new Checkpoints card visible between Trader View and Live Signal Trade Details
- 9 cells total: R1/R2/R7 × n10/n15/n30
- WAITING shown before threshold is reached

Warning logic:
- R1/R2 warning if mean_r <= 0 OR retention < 0.5 OR hit_rate < 0.45
- R7 shadow warning if mean_r <= 0 OR hit_rate < 0.45

Safety:
- notify/log only
- no threshold changes
- no rule changes
- no model changes
- no ml_engine changes
- no trading behavior changes

Operational note:
After Flask restart, `ESM6_15m_live.parquet` re-seeded and was renamed to:
`ESM6_15m_live.parquet.stale_20260501T191808Z`

Recommendation:
Continue monitoring. R1 is already negative at n=5 but below the n=10 early-warning threshold, so no action yet.

## Planning Documents

Status: ACTIVE — documentation only

Purpose:
Prepare future phases without changing current system behavior.

Files:
- `docs/PAPER_TRADING_PLAN.md`
- `docs/SIGNAL_REVIEW_PLAYBOOKS.md`

### PAPER_TRADING_PLAN.md

Purpose:
Defines the future paper-trading phase as a simulated-only research extension.

Key points:
- Phase 4 only, not active now
- simulated paper journal only
- no broker integration
- no real-money execution
- no auto-trading
- preconditions required before implementation
- placeholder risk assumption: 1R per paper trade
- suggested paper daily stop: -3R, final value requires approval
- Phase 5 real capital is explicitly out of scope

### SIGNAL_REVIEW_PLAYBOOKS.md

Purpose:
Defines how to review R1, R2, and R7 shadow at live checkpoints.

Checkpoints:
- n=10 early warning
- n=15 soft review
- n=30 first verdict

Includes:
- common diagnostic checklist
- R1 review process
- R2 review process
- R7 shadow review process
- cross-rule multi-WARN review
- review file template:
  `docs/reviews/<rule>_n<level>_<UTC>.md`

Safety:
- documentation only
- no code changes
- no thresholds changed
- no rules changed
- no model changes
- no ml_engine edits
- no trading behavior changed
- no paper trading implementation yet

Recommendation:
Use these documents only when checkpoint reviews trigger.
Do not start Phase 4 paper trading until n=30 checkpoint evidence and explicit approval.

## Checkpoint Review Templates

Status: ACTIVE — documentation only

Purpose:
Provide ready-to-use templates for manual checkpoint reviews when R1, R2, or R7 shadow reaches n=10, n=15, or n=30.

Files:
- `docs/reviews/templates/R1_REVIEW_TEMPLATE.md`
- `docs/reviews/templates/R2_REVIEW_TEMPLATE.md`
- `docs/reviews/templates/R7_SHADOW_REVIEW_TEMPLATE.md`
- `docs/reviews/templates/CROSS_RULE_REVIEW_TEMPLATE.md`

Usage:
When health monitor S13 fires a checkpoint notification:

```text
Copy the correct template to:
docs/reviews/<rule>_n<level>_<UTC>.md

Then fill it in with:
- snapshot pulled from .live_checkpoint_state.json + outcomes summary
- per-fire detail from dashboard's Live Signal Trade Details card
- diagnostic checklist marked
- decision section completed
```

Cross-rule template:
Use `CROSS_RULE_REVIEW_TEMPLATE.md` when 2+ rules WARN at the same checkpoint. Supplements (does not replace) the per-rule reviews.

Common structure (all 4 templates):
- header: rule, baseline, checkpoint level, reviewer, UTC
- snapshot: live n, mean_r, hit_rate, retention, session breakdown
- per-fire detail table: entry/stop/target/MFE/MAE/outcome
- diagnostic checklist
- rule-specific failure modes
- convergence check (n=15 / n=30)
- statistical sanity (n=30 only)
- decision matrix
- explicit "do NOT" list per checkpoint level
- next checkpoint plan

Per-rule differences:
- R1: short bias, baseline 1.18, retention concerns; default skepticism high
- R2: long bias, baseline 0.75, retention strong; WARN is urgent
- R7 shadow: direction = sign(cvd_slope), special STAY-SHADOW / ABANDON-SHADOW / PROPOSE-PROMOTION decision space; production -0.50 stays untouched
- Cross-rule: hypothesis tree (single-regime / common-mode / calibration-drift / regime-change / noise)

Safety:
- documentation only
- no code changes
- no thresholds changed
- no rules changed
- no model changes
- no ml_engine edits
- no trading behavior changed
- decisions implemented via separate manual env-flag edit only

Recommendation:
Always write the review file at every n=10 / n=15 / n=30 transition, even if the verdict is "no action". Never skip a checkpoint. Never act between checkpoints without a written review backing the action.

## Investor One-Pager + Ops Runbook

Status: ACTIVE — documentation only

Files:
- `docs/INVESTOR_ONE_PAGER.md`
- `docs/OPS_RUNBOOK.md`

### INVESTOR_ONE_PAGER.md

Purpose:
Plain-English transparency document. What the system is, what it isn't, current state, risks, realistic timeline, and what we will NOT do. Honest framing — research system, not trading system. Not a fundraise.

Sections:
- What this is / isn't
- Why it exists
- Current state (real numbers: R1 weak, R2 strong, R7 shadow failing live)
- How decisions are made (n=10 / n=15 / n=30 checkpoints)
- What's working / what's risky
- What we will not do
- Realistic timeline (~10-14 days to first verdicts)
- Three-outcome framing (only-R2-survives / all-fail / all-survive)
- Cost / contact

### OPS_RUNBOOK.md

Purpose:
Operational reference. How to bring system up, daily routine, recover from common failures, and know when to do nothing.

Sections:
- Quick reference (URLs + key files)
- Cold start / graceful down
- Daily routine
- 10 recovery scenarios A–J
- Weekend hibernation behavior
- "What NOT to do" boundary list
- Status one-liners
- Log priority order
- Environment vars + emergency revert command

Recovery scenarios covered:
- A: Flask down
- B: Live SDK silent during open market
- C: 15m parquet present after Flask boot (workaround applied)
- D: Raw cache stale
- E: cache_refresh.log idle during open market (Mac sleeping)
- F: Pending fires not settling
- G: Checkpoint notification fired (S13)
- H: Disk space low
- I: Health monitor process not firing
- J: Dashboard shows old data

Safety:
- documentation only
- no code changes
- no thresholds changed
- no rules changed
- no model changes
- no ml_engine edits
- no trading behavior changed

Recommendation:
Use OPS_RUNBOOK.md as the first reference when something appears wrong. Update INVESTOR_ONE_PAGER.md when project state shifts materially (after each n=30 verdict, or after any phase transition).

## Architecture + Data Quality Documentation

Status: ACTIVE — documentation only

Files:
- `docs/ARCHITECTURE.md`
- `docs/DATA_QUALITY_CHECKLIST.md`

Purpose:
- `ARCHITECTURE.md` explains the full single-Mac system architecture, runtime processes, data flow, key modules, file outputs, and known caveats.
- `DATA_QUALITY_CHECKLIST.md` gives copy-paste checks for live freshness, raw cache freshness, joined window health, pending outcome integrity, mode tagging, volume reconciliation, drift, live tail freeze, and health-probe correlation.

Safety:
- documentation only
- no code changes
- no rules changed
- no thresholds changed
- no config changed
- no model changes
- no `ml_engine/` edits
- no trading behavior changed

Recommendation:
Use `DATA_QUALITY_CHECKLIST.md` when a health flag appears or when live outcomes stop moving. Use `ARCHITECTURE.md` when onboarding a new Claude/ChatGPT session or explaining how the system works.

## OpenAI Reviewer Bridge

Status: ACTIVE — read-only research aid

Purpose:
Reduce copy-paste between Claude Code and ChatGPT. Claude scaffolds a brief, user reviews + sends, OpenAI response is written back to a file for Claude/user to read. Human-approved at every send. No automatic code changes.

Files:
- `scripts/update_chatgpt_brief.py` — scaffold the brief
- `scripts/ask_chatgpt_review.py` — gated send to OpenAI
- `scripts/validate_chatgpt_response.py` — validator (Phase 2)
- `outputs/order_flow/chatgpt_brief.md` — current brief
- `outputs/order_flow/chatgpt_response.md` — latest response (overwritten)
- `outputs/order_flow/chatgpt_response_review.md` — latest validator review (overwritten)
- `outputs/order_flow/chatgpt_history/<UTC>_response.md` — archived prior responses

Defaults:
- model: `gpt-4o-mini`
- max_tokens: 4000
- cost ceiling: $0.25/call (refuse unless `--force`)
- rate limit: 60s between sends (lockfile `/tmp/chatgpt_review.lockfile`)
- brief size cap: 50 KB
- response size cap: 100 KB (truncated)
- system prompt fixed: research-only review, no rule/threshold/model/ml_engine/trading code changes
- temperature: 0.2

API key:
`OPENAI_API_KEY` from `os.environ` first, then `.env`. Never on CLI, never logged.
**NOT YET PROVISIONED.** Add `OPENAI_API_KEY=sk-...` to `.env` before first real send.

Workflow:
```bash
# 1. scaffold brief (10 fixed sections always present)
.venv/bin/python scripts/update_chatgpt_brief.py \
    --question "your question here" \
    --concern "free-text current concern" \
    --files-checked "path/already/looked/at" \
    --include-project-state --include-outcomes-json

# 2. user reviews outputs/order_flow/chatgpt_brief.md

# 3. preview cost (dry-run, no network)
.venv/bin/python scripts/ask_chatgpt_review.py --dry-run

# 4. send (interactive y/N confirm)
.venv/bin/python scripts/ask_chatgpt_review.py

# 5. validate response BEFORE acting on it
.venv/bin/python scripts/validate_chatgpt_response.py
echo "exit=$?"   # 0 SAFE, 1 FLAG, 2 BLOCK, 3 ERROR

# 6. read outputs/order_flow/chatgpt_response_review.md FIRST
# 7. read outputs/order_flow/chatgpt_response.md
# 8. user approves any action manually (never on BLOCK without override)
```

Validator (`validate_chatgpt_response.py`):
- 10 categories scanned: invented commands (filesystem-checked), code blocks, threshold change verbs, R7 promotion language, ml_engine/model touch, trading/order keywords, auto-action language, config/rule edit hints, safe content allow-list, response-shape conformance to section 9
- Verdict: SAFE / FLAG / BLOCK / ERROR with matching exit codes (0/1/2/3)
- Lenient default; `--strict` promotes all FLAGs to BLOCK
- `--allow-pattern REGEX` (repeatable) skips known false-positive command patterns
- `--extra-forbidden KEYWORD` (repeatable) treats extra terms as BLOCK
- Stdlib only, atomic write, source SHA pin so review is bound to specific response
- Phase 2A REAL constants (`RULE_DELTA_DOMINANCE_REAL`, `RULE_ABSORPTION_DELTA_REAL`, `RULE_TRAP_DELTA_REAL`, `RULE_CVD_CORR_THRESH`, `RULE_CVD_CORR_WINDOW`, `RULE_CVD_CORR_THRESH_REAL_SHADOW`) all included in BLOCK keyword set

Smoke test (2026-05-03 v1 response from before v2 brief): verdict BLOCK, exit 2 — caught all 3 invented `python -m order_flow_engine.src.{check_data_integrity,compare_shadow_performance,analyze_alerts}` commands and flagged missing caveats/hypotheses/out-of-scope sections.

Smoke test (2026-05-03):
- scaffolder wrote 11152-byte brief
- dry-run estimated cost $0.0028 (well under $0.25 ceiling)
- gpt-4o-mini token estimate: ~2856 in, ≤4000 out

CLI flags (`ask_chatgpt_review.py`):
- `--model gpt-4o-mini` (default; also `gpt-4o`, `o1-mini` priced)
- `--max-tokens 4000` (default)
- `--dry-run` — preview cost + brief, no network
- `--yes` — skip interactive confirm
- `--force` — bypass cost ceiling
- `--brief PATH` — override default brief path

CLI flags (`update_chatgpt_brief.py`):
- `--question "..."` (required) — exact user question, populates section 1
- `--concern "..."` — free-text current signal concern, prepended to section 5
- `--files-checked PATH` (repeatable) — extra paths already inspected, merged into section 6
- `--include-project-state` — append head 80 lines of `PROJECT_STATE.md` as Appendix A
- `--include-outcomes-json` — append raw R1/R2 + R7 shadow summary JSON as Appendices B+C
- `--out PATH` — override default brief path

Brief structure (always 10 sections, deterministic):
1. Exact question from user
2. Current market/session state (CME ES open/closed + diagnostic latest_live_bar/freshness/joined window)
3. Current health state (13-probe table from `.health_state.json`)
4. Current checkpoint table (9-cell rule×level grid from `.live_checkpoint_state.json`)
5. Current signal concern (free-text + auto-extracted ⚠ flags from outcome summaries when mean_r ≤ 0 or hit_rate < 0.45)
6. Files/data already checked (defaults + `--files-checked` extras)
7. Allowed actions (read-only inspection + clarifying questions only)
8. Forbidden actions (no rule/threshold/config/model/ml_engine/predictor/alert_engine/ingest/monitor_loop/health_monitor/cache_refresh edits, no R7 promotion, no invented commands, no code changes)
9. Expected response format (6-step reply structure: direct answer → caveats → ≤3 hypotheses → read-only checks → open questions → out-of-scope confirmation)
10. No invented commands (explicit ban; phrase as "if a script existed at X" instead of fabricating CLI)

Safety:
- read-only on engine state (PROJECT_STATE, health/checkpoint/outcomes JSON)
- atomic tmp+rename for both brief and response writes
- archive prior response before overwrite
- no retries on send failure (avoid silent double-charge)
- no streaming, no multi-turn, no attachments
- stdlib only (`urllib.request`); no `openai` SDK / `requests` dep
- responses are advisory — never auto-applied
- `outputs/` already in `.gitignore` so chatgpt_*.md and history excluded

No changes to:
- rules
- thresholds
- config
- models
- `ml_engine/`
- predictor
- alert_engine
- ingest
- trading behavior
- monitor_loop
- health_monitor
- cache_refresh

## R2 Validation + Paper Journal Replay (Phase 2A analysis tools)

Status: ACTIVE — read-only analysis

### `scripts/r2_validation_report.py`

Purpose: hybrid PASS/WAIT/FAIL verdict for R2 (or any rule) using historical (in-sample) + live (out-of-sample) evidence.

Default gates:
- historical n ≥ 100
- live n ≥ 15
- mean_r > 0 (both modes)
- hit_rate ≥ 0.50 (both modes)
- historical max drawdown ≤ 8R
- sessions covered ≥ 2
- live dates covered ≥ 5
- live MFE_med / |MAE_med| ≥ 1.0

Verdict logic:
- PASS = all gates pass
- WAIT = only sample-size gates failing (n / dates / sessions)
- FAIL = any quality gate failing (mean_r / hit_rate / drawdown / mfe-mae ratio)

Outputs:
- `outputs/order_flow/r2_validation_report.md`
- `outputs/order_flow/r2_validation_report.json`

CLI: `--rule r2_seller_up` (default), `--hist-n-min 100`, `--live-n-min 15`, `--mean-r-min 0.0`, `--hit-rate-min 0.50`, `--max-dd-max 8.0`, `--sessions-min 2`, `--live-dates-min 5`, `--mfe-mae-ratio-min 1.0`, `--no-json`, `--stop-r N` (optional stop-loss simulation; trades with `mae_r ≤ -|N|` clipped to -|N|R for mean_r and max_dd; default = OFF; raw output reflects horizon accounting).

Stop-aware comparison (R2, post-backfill):
- `--stop-r off` → verdict FAIL (max_dd 23.64R fails 8R gate)
- `--stop-r 1.0` → verdict FAIL (max_dd passes at 8.00R, but live hit_rate drops to 4/9=0.444 below 0.50 floor — live sample too small to absorb stop reclassifications)
- `--stop-r 2.0` → verdict FAIL (looser stop preserves hit but max_dd 14.65R still fails)
- Honest interpretation: at n=9 live, no stop choice satisfies all gates simultaneously. Need n=15 live to retest with stop-aware accounting.

Smoke test (2026-05-04): R2 verdict FAIL — historical max DD 23.64R > 8R cap, live n=7 < 15, live dates < 5. R2 historical (in-sample) hit 65.6% mean_r +0.88R across 125 trades is strong; live n still small. WAIT-with-DD-warning is realistic posture once live n ≥ 15. May raise --max-dd-max for paper-journal context where 1R risk per trade is theoretical.

### `scripts/paper_journal_replay.py`

Purpose: per-trade replay + equity curve + drawdown for any subset of settled rule fires. Treats each settled outcome as 1R-risk paper trade using existing `fwd_r_signed`. NO broker, NO execution, NO real money.

Outputs:
- `outputs/order_flow/paper_journal.md` (default = R1+R2 combined)
- `outputs/order_flow/paper_journal.json`
- override `--out PATH` for filtered runs (e.g. `paper_journal_R2.md`)

Sections in MD:
- aggregate (trades, hit, mean_R, expectancy, sharpe-ish, final equity, max DD, DD duration, daily mean R, best/worst day)
- equity curve (text histogram)
- drawdown curve (text histogram)
- by rule + by mode breakouts
- per-trade journal (every settled fire with running equity + DD)

CLI: `--rules r2_seller_up,r1_buyer_down` (comma-sep, default = all rules in outcomes file), `--mode all|historical|live`, `--start-utc`, `--end-utc`, `--include-shadow` (off by default — shadow is research not paper), `--outcomes PATH`, `--out PATH`, `--no-json`, `--stop-r N` (optional risk cap; trades with `mae_r ≤ -|N|` simulated as stopped at -|N|R; default = no stop).

Smoke test (2026-05-04 unclipped):
- R2 only (n=125): hit 65.6%, mean +0.875R, final +109.40R, max DD 23.64R
- R1+R2 combined (n=247): hit 60.7%, mean +0.491R, final +121.38R, max DD 15.84R
- R1 alone is the drag (negative contributor to combined mean), but combining smooths drawdown

Stop-aware comparison (R2 only, post-backfill n=130):
| stop      | trades | hit    | mean_R  | final     | max_DD  |
|-----------|--------|--------|---------|-----------|---------|
| none      | 130    | 0.6615 | +0.876  | +113.92R  | 23.64R  |
| -1R       | 130    | 0.5462 | +0.913  | +118.72R  | **8.00R** (passes 8R gate) |
| -2R       | 130    | 0.6308 | +0.869  | +112.95R  | 14.65R  |

Findings:
- -1R stop INCREASES final equity (+113→+119) by capping outsized losers
- -1R stop COLLAPSES max DD (23.64→8.00, exactly at gate)
- Hit rate "drops" with stop because near-miss trades that would have recovered to break-even now register as -1R losses; this is correct accounting
- Use `--stop-r 1.0` for honest risk-managed view; default OFF retains horizon-based reference

Caveats baked into every output:
- IGNORES slippage, spread, commissions, execution latency
- IGNORES position sizing risk (1R fixed per trade)
- IGNORES overlap risk between adjacent trades
- `fwd_r_signed` computed at fixed 12-bar horizon; real exit logic may differ
- Sharpe-ish per-trade not annualized; relative metric only
- Historical = in-sample, live = out-of-sample; combined view mixes both
- NOT a track record. NOT a guarantee.

Safety:
- read-only on outcomes JSONL
- atomic tmp+rename for both .md and .json
- stdlib only (no new deps)
- no subprocess, no network
- idempotent — rerun anytime overwrites output

No changes to:
- rules
- thresholds
- config
- models
- `ml_engine/`
- predictor
- alert_engine
- ingest
- monitor_loop
- health_monitor
- cache_refresh
- outcome_tracker
- trading behavior

Workflow for MVP soft verdict (early June 2026 target):
```bash
# 1. validate R2 hybrid evidence
.venv/bin/python scripts/r2_validation_report.py

# 2. equity curve for R2 alone (paper sim)
.venv/bin/python scripts/paper_journal_replay.py \
    --rules r2_seller_up \
    --out outputs/order_flow/paper_journal_R2.md

# 3. compare combined R1+R2 (default run)
.venv/bin/python scripts/paper_journal_replay.py

# 4. read both reports; declare MVP soft verdict if R2 PASSes at n=15 live
```

Future tools deferred (separate approval):
- `scripts/r1_investigation_report.py` — R1 weakness diagnosis
- `scripts/r7_shadow_vs_production_report.py` — trend-day filter analysis

## Realflow History Backfill (Scheduled)

Status: BUILT — awaiting `launchctl load` to activate

Purpose:
Prevent realflow_history.parquet from going stale and causing silent live→historical fire demotion. 1m live tail caps at 500 bars (~8.3h per blocker #5); cadence MUST be ≤ 8h to avoid demotion gap.

Files:
- `scripts/realflow_backfill.sh` — bash wrapper (lock + skip + run + log + fail-flag)
- `~/Library/LaunchAgents/com.rfm.realflow-backfill.plist` — launchd job
- `outputs/order_flow/realflow_backfill.log` — structured log
- `outputs/order_flow/realflow_backfill_FAILED.flag` — created after 3 consecutive fails (auto-cleared on next success)
- `outputs/order_flow/realflow_backfill.launchd.{stdout,stderr}.log` — launchd capture
- `/tmp/rfm-realflow-backfill.lockdir` — POSIX mkdir atomic lock
- `/tmp/rfm-realflow-backfill.failcount` — consecutive failure counter
- `scripts/health_monitor.py` — extended with `s14_realflow_backfill_log` + `s15_realflow_backfill_failed`

Configuration:
- cadence: 28800s (8h)
- skip threshold: 240 min (4h) — skip if realflow_history mtime within 4h
- failure threshold: 3 consecutive fails → flag
- RunAtLoad: true (fires immediately on launchctl load + after sleep/restart)
- symbol: ESM6 only (NQ/CL/GC deferred per standing instruction)
- timeframe: 15m
- lookback days: 18 (matches `realflow_history_backfill.py` cap)
- credentials: `.env` sourced inside wrapper; `DATABENTO_API_KEY` never on CLI, never logged

Health probes added:
- `s14_realflow_backfill_log` — warns if `realflow_backfill.log` mtime > 8h (means schedule broken or never loaded)
- `s15_realflow_backfill_failed` — warns if `realflow_backfill_FAILED.flag` present

Smoke test (2026-05-04 05:23Z):
- bash wrapper executed cleanly, returned SKIP because realflow_history mtime was 10 min old (well under 240 min threshold)
- both new health probes return `healthy`
- exit code 0

Activation (manual, by user):
```bash
launchctl load ~/Library/LaunchAgents/com.rfm.realflow-backfill.plist
launchctl list | grep com.rfm.realflow-backfill   # verify loaded
```

Stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.rfm.realflow-backfill.plist
```

Manual run (anytime, bypasses skip-check):
```bash
bash scripts/realflow_backfill.sh --force
```

Manual run (respects skip-check):
```bash
bash scripts/realflow_backfill.sh
```

Acknowledge failure flag manually:
```bash
rm outputs/order_flow/realflow_backfill_FAILED.flag
```

Inspect log:
```bash
tail -30 outputs/order_flow/realflow_backfill.log
```

Cost / risk:
- ~$0.05-0.15 Databento credits per run × 3 runs/day = $5-14/month
- 1 min run time observed (OHLCV 1s + trades 56s)
- Skip-check prevents redundant fetches if previous run was recent
- Lock prevents overlap with other invocations
- Auto-clear of failure flag on success means health monitor self-heals on transient errors
- 8h cadence has ~0.3h safety margin under 8.3h live-tail cap

Safety:
- read+append only on parquet (backfill module already moves existing → .bak before writing new)
- atomic write via .bak rotation pattern
- POSIX lock prevents concurrent runs
- skip-check prevents wasted Databento credits
- failure escalation via flag file → health monitor s15 visibility
- launchctl unload fully reversible
- credentials sourced inside wrapper, never in process args
- ESM6-only — no cross-asset port

No changes to:
- rules
- thresholds
- config
- models
- `ml_engine/`
- predictor
- alert_engine
- ingest
- monitor_loop
- cache_refresh (separate launchd job at `com.rfm.cache-refresh`)
- outcome_tracker
- horizon_bars
- R7 promotion
- trading behavior

Coordination with existing schedules:
- `com.rfm.cache-refresh` (hourly, raw OHLCV only, lighter, ~30 min mtime skip)
- `com.rfm.realflow-backfill` (8h, full 18d trades fetch, heavier, 4h mtime skip)
- `com.rfm.health-monitor` (5m, observe-only, now includes s14/s15)
- Both data-refresh jobs hit Databento; potential rate-limit interaction unlikely but worth observing first week

## R7 Shadow vs Production Comparison

Status: ACTIVE — read-only analysis tool

Purpose:
Determine whether production R7 at `-0.50` avoided the bad shadow `-0.20` failure clusters. Provides decision-grade evidence for the n=30 R7 shadow review (Phase 2B Stage 2).

Files:
- `scripts/r7_shadow_vs_production_report.py` — the tool
- `outputs/order_flow/r7_shadow_vs_production.md` — human report
- `outputs/order_flow/r7_shadow_vs_production.json` — machine sidecar

Headline verdicts (one of):
- `PRODUCTION-NEVER-FIRED` — production has zero fires; cannot validate but cannot disprove either
- `PRODUCTION-AVOIDED-TRAPS` — production fired but skipped every shadow cluster-trap day → KEEP-PRODUCTION + ABANDON-SHADOW
- `PRODUCTION-ALSO-FIRED-AND-LOST` — production also fired on cluster days → escalate to Phase 2C (rule itself fragile)
- `INSUFFICIENT-CLUSTERS` — no cluster-trap days observed; comparison inconclusive

Smoke test (2026-05-04): **PRODUCTION-NEVER-FIRED**
- Shadow settled: 100 fires (rule=`r7_cvd_divergence_shadow`)
- Production settled: **0 fires** (rule=`r7_cvd_divergence`)
- Cluster-trap days identified: **2**
  - 2026-04-30: 9 shadow fires, mean_r -2.89, production fires=0 (AVOIDED)
  - 2026-05-01: 17 shadow fires, mean_r -1.38, production fires=0 (AVOIDED)
- Overlap analysis: 0 production matches (consistent with zero production fires)

Implication: Production R7 at -0.50 is **so strict it has not fired in the 18-day window**. The strict threshold means R7 production has not generated any tradable signal but also has not been hurt by the shadow's regime traps. R7 production is **structurally protected**.

Recommendation embedded in report: **KEEP -0.50 PRODUCTION + ABANDON SHADOW (likely path)**. Decisive evidence for the n=30 shadow review.

CLI flags:
- `--prod-outcomes PATH` (default `realflow_outcomes_ESM6_15m.jsonl`)
- `--shadow-outcomes PATH` (default `realflow_r7_shadow_outcomes_ESM6_15m.jsonl`)
- `--shadow-pending PATH` (default `realflow_r7_shadow_pending_ESM6_15m.json`)
- `--diagnostic PATH` (default `realflow_diagnostic_ESM6_15m.json`)
- `--cluster-min-fires 3` (default; min shadow fires per day to flag as cluster)
- `--cluster-mean-r-max -1.0` (default; max mean_r for cluster classification)
- `--overlap-bars 1` (default; ±1 bar tolerance for "match" between thresholds)
- `--out PATH` (override .md path)
- `--no-json` (skip JSON sidecar)

Exit codes: 0 = ok, 2 = inputs missing, 3 = no shadow fires.

Safety:
- read-only on outcomes JSONL + pending JSON + diagnostic JSON
- atomic tmp+rename for both .md and .json outputs
- stdlib only (no new deps)
- no subprocess, no network
- no rule, threshold, model, ml_engine, predictor, alert_engine, ingest, outcome_tracker, R7 promotion, or trading change

When to re-run:
- Before each n=10 / n=15 / n=30 R7 shadow review checkpoint
- After any realflow_history backfill (shadow re-discovery may shift cluster boundaries)
- When verdict is being drafted

## Demotion Rate Probe (s17)

Status: ACTIVE — visibility-only health probe (refined to filter backfill rediscovery)

Purpose:
Surface TRUE silent live → historical fire demotion. The 1m live tail caps at
500 bars (~8.3h per blocker #5). A fire is considered a "true silent demotion"
ONLY when its discovery gap (`discovered_at - fire_ts`) is within the 8.3h
tail cap AND it was tagged `mode=historical` instead of `mode=live`. Fires
whose discovery gap exceeds 8.3h are correctly classified as backfill
rediscoveries (Live SDK could not have captured them) and excluded from the
demotion ratio.

Watched sources:
- `outputs/order_flow/realflow_outcomes_ESM6_15m.jsonl`
- `outputs/order_flow/realflow_r7_shadow_outcomes_ESM6_15m.jsonl`

Behavior:
- Reads up to 50 latest rows from each source, combines, sorts by fire_ts_utc desc, takes top 50
- Computes `discovery_gap = discovered_at - fire_ts` per row
- Splits into:
  - `candidate_live` = fires with gap ≤ 8.3h (Live SDK should have caught)
  - `backfill_rediscovery` = fires with gap > 8.3h tagged historical (correctly tagged, excluded)
- Computes `true_demotion_ratio = (candidate_live tagged historical) / candidate_live`
- Also surfaces raw_demotion_ratio for transparency
- Threshold: true_demotion_ratio > 0.30 → unhealthy
- Notify on transition (existing health monitor pattern)
- Skipped if no candidate_live fires in window (e.g. quiet period)

Constants:
- `DEMOTION_WINDOW_N = 50`
- `DEMOTION_RATIO_THRESHOLD = 0.30`
- `LIVE_TAIL_CAP_HOURS = 8.3` (= TAIL_LEN(500) × 1m / 60)

Refinement smoke test (2026-05-04 06:35Z):
- Before refinement: status UNHEALTHY (raw_ratio 0.56)
- After refinement: **HEALTHY** (true_demotion_ratio 0.09)
- 22 candidate-live fires in window; 2 true silent demotions (9%)
- 26 backfill rediscoveries correctly excluded from demotion count

Investigation finding: of last 50 settled fires, only 2 represent actual real-time silent demotion. The remaining 26 historical-tagged fires were re-discovered by scheduled backfill from past dates (Live SDK could not have seen them). True silent demotion rate is operationally healthy.

What it does NOT change:
- Live tail logic (untouched per blocker #5)
- Outcome scoring (untouched)
- Tracker code (untouched)
- Production rule firing (untouched)
- All settled outcomes JSONL (read-only)

Operational implications:
- Live n=10 / n=15 / n=30 checkpoints will trip more slowly than fire-rate suggests
- MVP timeline already absorbs this (R2 0.37 fires/day calibrated on what bumps live counter)
- 8h scheduled realflow_backfill helps but does NOT fix root cause
- Root cause fix would touch ingest.py / TAIL_LEN — explicitly out of scope per standing instruction

If demotion ratio drops below 0.30:
- Probe auto-clears (returns healthy)
- No manual intervention needed
- Indicates fewer late-discovered fires (Live SDK + monitor_loop both keeping up)

## Pending Disappearance Probe (s16)

Status: ACTIVE — visibility-only health probe

Purpose:
Detect silent data loss in pending fires. The R7 shadow tracker (and R1/R2 outcome tracker by similar pattern) rebuilds the pending list from the current `fires_mask` on each pass and overwrites the pending JSON file. After a `realflow_history` backfill, recomputed `cvd_z` values can shift the rolling correlation enough that a previously-pending fire's mask flips False on re-discovery — the fire silently disappears from pending without ever being settled to JSONL.

Confirmed loss: `ESM6_15m_2026-05-03T22:00:00Z_r7_shadow` vanished after 2026-05-04 backfill. Not in pending. Not in settled JSONL. No log trace.

Probe behavior:
- Reads current pending IDs from both R1/R2 and R7 shadow pending JSON files
- Compares against previous snapshot saved at `outputs/order_flow/.pending_snapshot.json`
- For each ID present in prior snapshot but missing from current pending, checks the corresponding settled JSONL
- If disappeared AND not settled → silent loss → unhealthy with `silent_losses` detail
- Updates snapshot file in place each run (atomic tmp+rename)
- First-run-after-install: always healthy (no prior snapshot)

Watched sources:
- `outputs/order_flow/realflow_outcomes_pending_ESM6_15m.json` ↔ `realflow_outcomes_ESM6_15m.jsonl` (label=`r1r2`)
- `outputs/order_flow/realflow_r7_shadow_pending_ESM6_15m.json` ↔ `realflow_r7_shadow_outcomes_ESM6_15m.jsonl` (label=`r7_shadow`)

State file:
`outputs/order_flow/.pending_snapshot.json`

Smoke test (2026-05-04 05:31Z):
- first run healthy; snapshot created with `r1r2: 2` + `r7_shadow: 2` IDs
- second run healthy (no diff)
- 16 total probes registered cleanly

Operational note:
Probe runs every 5 min via launchd `com.rfm.health-monitor`. If cadence is too coarse (rapid fire/disappear within 5 min), some losses could go undetected. For 8h backfill cadence + ~1 fire/hr generation rate, 5-min probe is sufficient to catch backfill-induced losses.

Limitations:
- Visibility only — does NOT prevent loss, does NOT recover lost fires, does NOT change scoring
- Cannot detect losses that happened BEFORE this probe was installed (no historical snapshot)
- snapshot file is single-source-of-truth; deleting it resets disappearance detection to "no prior knowledge"

What this does NOT change:
- Outcome scoring logic (untouched per standing instruction)
- Shadow tracker code (untouched)
- Production R7 / R1 / R2 firing (untouched)
- Settled JSONL (read-only from this probe)
- Pending JSON files (read-only from this probe)
- rules / thresholds / config / models / `ml_engine/` / predictor / alert_engine / ingest / monitor_loop / cache_refresh / outcome_tracker / horizon / R7 promotion / trading

If silent losses recur:
Approve Option B (warning log in shadow tracker itself) or Option A (preserve pending across passes) as separate scope expansion. Both touch outcome scoring path and require explicit standing-instruction relaxation.

## Session Handoff

Status: ACTIVE

File:
`docs/HANDOFF.md`

Purpose:
Copy-paste-ready handoff for a new Claude Code session.

Use when:
- starting a new Claude session
- onboarding another assistant
- recovering context after a long break

New session instruction:
1. Read `docs/HANDOFF.md`
2. Run the sanity-check commands listed inside it
3. Confirm current state before making changes
4. Preserve standing instruction:
   - no rule changes
   - no threshold changes
   - no model changes
   - no `ml_engine/` edits
   - no R7 promotion
   - no trading behavior changes unless explicitly approved

Safety:
documentation pointer only, no runtime changes.

## Evidence Acceleration Strategy

Status: ACTIVE — strategy only

Companion: `docs/RESEARCH_TAKEAWAYS.md` (full 10-point checklist with diversity gates, hybrid validation rule, and effective-lambda prerequisites).

Core decision:
Do not make R1/R2/R7 fire faster by loosening thresholds. Acceleration should come from better observation reliability and earlier checkpoint reviews.

Approved acceleration methods:
- keep realflow history fresh (8h scheduled backfill via `com.rfm.realflow-backfill`)
- prevent joined-window freezes (raw cache hourly + realflow_history every 8h)
- detect pending disappearance (probe s16, snapshot diff)
- detect true live→historical demotion (probe s17, refined to filter backfill rediscovery)
- keep only one monitor_loop (operational hygiene; duplicates risk JSONL race + double-notification)
- use n=10 early warning reviews (pre-staged R2 + R7 shadow review files)
- use n=15 soft decision reviews (act early on convergent signals; accept lower CI)
- use R2 as MVP candidate if it stays positive (R2 is the only live-positive signal)

Current signal roles:
- R2 = MVP candidate (load-bearing positive signal; everything rides on its n=15/n=30 verdict)
- R1 = investigation track (live mean_r negative; expected ABANDON at n=30 unless trajectory flips)
- R7 shadow = futility/research track (live mean_r -3.56R; expected ABANDON-SHADOW at n=30; production R7 -0.50 untouched)

Forbidden acceleration methods:
- lower thresholds (would invalidate sample, reset checkpoint clock)
- shorten official horizon (12-bar horizon is part of outcome scoring contract)
- combine R1/R2/R7 counts (each rule has independent verdict; mixing dilutes signal)
- promote R7 shadow (production -0.50 is structurally protected)
- retune R1 mid-sample (no rescue tuning; honest negative result if applicable)
- retrain models (`ml_engine/` artifacts frozen)
- modify `ml_engine/` (out of scope per standing instruction)
- add broker / live trading (Phase 5 deferred, OUT OF SCOPE)

Safety:
- documentation only
- no code change
- no rule, threshold, config, model, ml_engine, outcome_scoring, horizon_bars, R7 promotion, or trading behavior change
- standing instruction reinforced, not relaxed

## R2 Deceleration Watch

Status: ACTIVE — manual review policy only

Approved 2026-05-04T17:30Z. Documentation/review workflow only. No code change, no probe, no dashboard card, no health monitor change. No rule / threshold / config / models / `ml_engine/` / outcome scoring / horizon / R7 promotion / trading behavior change.

Companion: `docs/RESEARCH_TAKEAWAYS.md` section 5b (full hypothesis tree, decision matrix, verdict boundary).

Purpose:
Detect whether R2 edge is decaying or sample noise between n=15 and n=30. R2 passed n=15 KEEP but trajectory is degrading — n=10 mean_r +0.9808, n=16 mean_r +0.6450, new 6 fires averaged +0.085R.

### Soft trip lines (manual, observation only)

| probe | trigger | action |
|---|---|---|
| P1 rolling-6 cold | last 6 settled R2 fires mean_r ≤ +0.30 | flag investigation |
| P2 retention floor | live retention < 0.80 | pre-stage investigate review |
| P3 hit floor | live hit_rate < 0.55 | pre-stage investigate review |
| P4 mean negative | live mean_r ≤ 0 | hard escalate / pre-stage REVERT review |
| P5 RTH starvation | n=20 reached AND RTH_open + RTH_close both still 0 | document ETH-only caveat |
| P6 single-day concentration | any new date contributes > 40% of fires since n=15 | flag regime concentration |

None auto-act. None coded. Reviewer reads `.live_checkpoint_state.json` + outcomes JSONL manually.

### Informal n=20 soft probe

Not in the standard n=10/n=15/n=30 schedule. Manual mid-checkpoint at 4 fires past n=16.

At R2 n=20 compute:
- mean_r for fires 17–20
- mean_r for fires 11–20
- hit rate for fires 17–20
- session distribution
- ATR / regime distribution
- whether 2026-05-04 cluster still dominates

Decision guide:
- mean_r fires 17–20 > +0.50 → recovering; continue silently to n=30
- mean_r fires 17–20 in 0 to +0.50 → WATCH-CONTINUE; flag here in PROJECT_STATE
- mean_r fires 17–20 ≤ 0 → investigate pre-n=30; pre-stage decay-investigation review

Reuse `R2_REVIEW_TEMPLATE.md`; checkpoint level field = "n=20 soft probe (informal)". Save as `docs/reviews/r2_n20probe_<UTC>.md`.

### Pre-staged review files (when triggered)

| trip | pre-stage file | finalize when |
|---|---|---|
| n=20 informal | `docs/reviews/r2_n20probe_<UTC>.md` | 4 more fires settled |
| P1 persists 2 consecutive 6-windows | `docs/reviews/r2_decay_investigation_<UTC>.md` | flag set |
| P2 retention < 0.80 | `docs/reviews/r2_retention_breach_<UTC>.md` | breach detected |
| P4 mean ≤ 0 | `docs/reviews/r2_revert_candidate_<UTC>.md` | flag set |

All copies of existing R2 review template; only headers + checkpoint level changed.

### Open question this policy does NOT solve

If R2 falls below KEEP gates at n=20 mid-probe, the standing instruction still bars threshold change. Only available lever is REVERT (`OF_REAL_THRESHOLDS_ENABLED=0`). No retune. Document and accept honest negative result as risk.

### Safety

- documentation only
- no code change, no probe, no dashboard card, no health monitor change
- no rule / threshold / config / models / `ml_engine/` / outcome scoring / horizon / R7 promotion / trading behavior change
- standing instruction reinforced, not relaxed

## Live SDK Watchdog (Stage 1 — read-only)

Status: BUILT — awaiting `launchctl load` to activate. Stage 1 only. Read-only observation. NO restart behavior.

Purpose:
Detect the known failure mode where Flask remains HTTP 200 but the Databento Live SDK silently stops emitting tape alerts and 1m bars. Stage 1 evaluates restart criteria and logs the decision per tick; Stage 2 (actual restart) requires separate explicit approval after Stage 1 observation.

Files:
- `scripts/live_sdk_watchdog.py` — Python core; reuses `market_open()`, `latest_tape_alert_utc()`, `last_bar_age_min()` primitives in standalone form (no `health_monitor.py` import to keep watchdog self-contained)
- `scripts/live_sdk_watchdog.sh` — bash wrapper, venv-aware, exits 0 on python failure (avoid launchd thrash)
- `~/Library/LaunchAgents/com.rfm.live-sdk-watchdog.plist` — launchd job
- `outputs/order_flow/live_sdk_watchdog.log` — JSON-per-line decision record
- `outputs/order_flow/live_sdk_watchdog.launchd.stdout.log` — launchd stdout capture
- `outputs/order_flow/live_sdk_watchdog.launchd.stderr.log` — launchd stderr capture

Mode: `read_only_stage1`

Cadence: 300s (5 min, matches `com.rfm.health-monitor`) once launchd is loaded. `RunAtLoad=true`. `ThrottleInterval=60` to prevent rapid relaunch on crash.

Current behavior (Stage 1):
- Observes Flask PID via `pgrep -f "python.*app\.py"`
- Observes Flask HTTP 200 via `urlopen http://localhost:5001/`
- Observes latest `TAPE ALERT` age in `/tmp/flask.log`
- Observes latest bar age in `ESM6_1m_live.parquet`
- Observes presence of stale `ESM6_15m_live.parquet`
- Applies `market_open()` guard (CME approx: closed Fri 22Z → Sun 22Z)
- Logs one decision per tick: `SKIP_MARKET_CLOSED`, `WOULD_RESTART`, `FLASK_DOWN`, `FLASK_HTTP_DOWN`, `PARTIAL_TAPE_STALE`, `PARTIAL_1M_STALE`, or `HEALTHY`
- Decision criteria for `WOULD_RESTART` (logged only, not executed): market open AND Flask PID present AND HTTP 200 AND tape alert age > 30 min AND 1m parquet age > 30 min

Restart criteria thresholds:
- `TAPE_AGE_RESTART_MIN = 30.0` minutes
- `PARQUET_AGE_RESTART_MIN = 30.0` minutes
- Both must be exceeded simultaneously to log `WOULD_RESTART` (single-stale conditions log `PARTIAL_*` for diagnosis)

NO restart behavior in Stage 1:
- no Flask kill (`SIGTERM` / `SIGKILL`)
- no Flask spawn (`nohup` / `tmux respawn`)
- no `ESM6_15m_live.parquet` rename
- no `monitor_loop` invocation
- no parquet, JSONL, or pending-state writes
- no health monitor probe extension yet (s18/s19 deferred to Stage 2)
- no FAILED.flag, no DISABLED switch, no `/tmp/` cooldown/lockdir sentinels (Stage 2 territory)

Stage 1 smoke test (2026-05-04T18:17:01Z, manual run):
- decision: `PARTIAL_TAPE_STALE`
- would_restart: `false`
- flask_pid: 61206
- flask_http_200: `true`
- tape_alert_age_min: 623.3 (stale)
- parquet_1m_age_min: 19.0 (fresh)
- parquet_15m_present: `false` (workaround per blocker #5 already applied)
- reason: tape stale but 1m parquet fresh — log rotation or quiet tape

Observation note for Stage 1 review:
The very first tick already shows tape alert log can lag wall-clock by 10+ hours while the 1m parquet stays fresh. This means a naive "tape stale ⇒ restart" rule would have produced false positives. The conjunctive (tape AND parquet) restart rule prevents this. Stage 1's job is to build evidence over a few days that:
1. `WOULD_RESTART` only fires during genuine SDK outages (not log rotations)
2. `PARTIAL_*` events outnumber `WOULD_RESTART` events
3. `HEALTHY` is the dominant decision during open market hours

Activation (manual, by user):
```bash
launchctl load ~/Library/LaunchAgents/com.rfm.live-sdk-watchdog.plist
launchctl list | grep com.rfm.live-sdk-watchdog
```

Stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.rfm.live-sdk-watchdog.plist
```

Manual run (anytime, bypasses launchd schedule):
```bash
bash scripts/live_sdk_watchdog.sh
tail -1 outputs/order_flow/live_sdk_watchdog.log
```

Inspect log:
```bash
tail -30 outputs/order_flow/live_sdk_watchdog.log
```

Stage 2 (NOT yet approved — separate approval required):
- Add restart sequence (SIGTERM Flask + spawn under nohup + rename 15m parquet + run one monitor_loop iteration)
- Add cooldown sentinel (`/tmp/rfm-live-sdk-watchdog.cooldown`, 30 min)
- Add lockdir (`/tmp/rfm-live-sdk-watchdog.lockdir`)
- Add FAILED.flag (3-strike escalation)
- Add DISABLED kill-switch (`outputs/order_flow/live_sdk_watchdog.DISABLED`)
- Add health probes s18 (watchdog log freshness) + s19 (FAILED.flag presence)
- Required Stage 1 review before Stage 2 approval

Coordination with existing schedules:
- `com.rfm.cache-refresh` (hourly raw OHLCV refresh)
- `com.rfm.realflow-backfill` (8h heavy backfill)
- `com.rfm.health-monitor` (5m observe-only, 17 probes)
- `com.rfm.live-sdk-watchdog` (5m observe-only Stage 1; will add restart action in Stage 2)
- All four read different files; no race condition expected
- Watchdog read-only path: `pgrep`, `urlopen`, `/tmp/flask.log` (read), `ESM6_1m_live.parquet` (read), `ESM6_15m_live.parquet` (existence check), append-only `live_sdk_watchdog.log`

Safety:
- read-only on all engine state in Stage 1
- atomic append on log file (one JSON line per run)
- stdlib + pandas only (already venv requirements)
- launchd unload fully reversible
- standing instruction reinforced: no rule / threshold / config / model / `ml_engine/` / predictor / alert_engine / ingest / outcome scoring / horizon / R7 promotion / trading behavior change

No changes to:
- rules
- thresholds
- config
- models
- `ml_engine/`
- predictor
- alert_engine
- ingest
- monitor_loop
- cache_refresh
- realflow_backfill
- health_monitor (no s18/s19 yet)
- outcome_tracker
- horizon_bars
- R7 promotion
- trading behavior

## Standing instruction

- No detector behavior changes.
- No edits to rules / thresholds / labels / models / `ml_engine/` / predictor / alert_engine / ingest.
- No trades.
- No auto-revert / auto-promote.
- All trackers read+append-only.
- Phase 2A active. Phase 2B deferred (shadow-tracked at -0.20).
- Manual dashboard checks by user.

## Next checkpoint

| signal | target n | current n | n=10 status | n=15 status | n=30 ETA |
|---|---|---|---|---|---|
| R1 live | 30 | **11 ✅ TRIPPED** | **OK** (KEEP-WITH-CAVEAT, finalized 2026-05-04T17:30Z) | NOT_REACHED (~4 fires away) | ~12 wks |
| R2 live | 30 | **16 ✅ TRIPPED** | **OK** (KEEP) | **OK** (KEEP, finalized 2026-05-04T17:30Z) | ~7 wks |
| R7 shadow live | 30 | **12 ✅ TRIPPED** | **WARN** (STAY-SHADOW; n=12 refresh 2026-05-04T17:30Z) | NOT_REACHED (~3 fires away) | ~5 wks |

When R1 live ≥ 15, re-review and populate convergence row.
When R2 live ≥ 30, run Phase 2D hard verdict (keep / retune / continue) with bootstrap CI.
When R7 shadow live ≥ 15, soft-decision review using `R7_SHADOW_REVIEW_TEMPLATE.md`.
When R7 shadow live ≥ 30, evaluate Phase 2B Stage 2 readiness.

### Recent checkpoint events

- **2026-05-04 17:30Z** — R1 live n=10 trip (current n=11). Verdict **KEEP-WITH-CAVEAT**. Review finalized at `docs/reviews/r1_n10_20260504T173000Z.md`. mean_r=+0.7858, hit=0.8182 (9/11), retention=0.666 (below 0.8 KEEP gate but 9.5× historical 0.07). 100% ETH coverage; two outlier losses (MAE -6.16R, -12.01R; fwd_R -3.54, -11.57) on adjacent bars 2026-05-01 10:30/11:00 dominate the mean. Sign-test 9/11 wins p≈0.033 vs 50% null.
- **2026-05-04 17:30Z** — R2 live n=15 trip (current n=16). Verdict **KEEP** with deceleration flag. Review finalized at `docs/reviews/r2_n15_20260504T173000Z.md`. mean_r=+0.6450 (down from +0.9808 at n=10), hit=0.5625, retention=0.860. All gates still pass but **6 new fires averaged only +0.085R** vs prior +0.98R — degrading trajectory. RTH coverage gap unchanged (14 ETH / 2 RTH_mid / 0 RTH_open / 0 RTH_close). Single-day concentration: 6/16 fires on 05-04. Soft-warning probe at n=20-25 if next 5 fires don't recover above +0.3R.
- **2026-05-04 17:30Z** — R7 shadow live n=12 status refresh. Status STILL **WARN** (verdict unchanged STAY-SHADOW). State now n=12, mean_r=-2.44 (improved from -2.7725 at n=10 review), hit=0.500, retention=-3.419. New review file NOT written (n=12 is between checkpoints; next review at n=15). Production R7 -0.50 still has zero fires (structurally protected per `r7_shadow_vs_production.md`).
- **2026-05-04 07:15Z** — R2 live n=10 trip. Verdict KEEP. Review at `docs/reviews/r2_n10_20260504T051805Z.md`.
- **2026-05-04 07:30Z** — R7 shadow live n=10/n=11 trip. Verdict STAY-SHADOW. Review at `docs/reviews/r7sh_n10_20260503T231500Z.md`.
- Cross-reference: R7 shadow vs production report (`r7_shadow_vs_production.md`) confirms production R7 fired ZERO times on either trend-trap day (2026-04-30, 2026-05-01) — production structurally protected.

## Known blockers

1. ~~**Live SDK in Databento 422 error.**~~ **RESOLVED 2026-05-01.** `data_schema_not_fully_available` no longer observed. Live SDK subscribes raw front-month (ESM6/GCM6/CLM6/NQM6) cleanly. Tape alerts streaming. Residual `data_end_after_available_end` 422 from outcome_tracker is harmless settle-lag against historical dataset publication (~1-3min behind wall-clock).
2. **Raw OHLCV cache stale risk.** `data/raw/ESM6_15m.parquet` does not auto-refresh in current Flask session. Joined window caps at raw `last_bar`, which blocks live fires from settling once their forward-horizon timestamps trail past it. **Workaround:** periodic `python -m order_flow_engine.src.data_loader --symbol ESM6 --no-cache` refreshes raw + downstream realflow_history. No code change. Candidate for scheduled cadence.
3. **R1 retention 0.13 in 18d historical.** Below 0.5 gate but mean_r still positive. Per recommendation logic: continue monitoring on live-only sample.
4. **Vol_match mismatch 0.48 (gate ≤0.25).** Phase 1H showed denominator switch is no-op; gate is informational only. Not a blocker.
5. **Live 15m parquet persistence freezes after startup-seed.** Live SDK only emits 1m bars (`realtime_databento_live.py:312` hardcodes `timeframe="1m"`). 15m live parquet receives the startup backfill seed then never updates. **Workaround applied 2026-05-01:** `ESM6_15m_live.parquet` renamed to `*.stale_<UTC>` to force `realflow_loader` + `realflow_outcome_tracker` 1m→15m resample fallback. New fires now resolve via fresh 1m tail. Fix is one-shot per Flask startup — re-rename if Flask restart re-seeds the 15m parquet. Underlying persistence path needs code review (currently OUT OF SCOPE per standing instruction). 1m live tail rolls at 500-bar cap (~8.3h window); fires older than that at settle time get reclassified `historical` rather than `live`.

## Active triggers (ping when any fires)

1. `phase2_gates.all_pass == true`
2. `joined_bar_count ≥ 200` AND `vol_match_mismatch_5pct > 0.40`
3. `delta_ratio_drift.drift_pct != null` AND `drift_pct > 0.30`
4. `live_nan_rows / joined_bar_count > 0.05`
5. `joined_bar_count ≥ 500` ✅ already cleared
6. `joined_bar_count ≥ 1000` AND `r7_cvd_divergence ≥ 30`
7. R1 settled ≥ 100 AND R2 settled ≥ 100 ✅ already cleared
8. R1 live ≥ 30 AND R2 live ≥ 30 → Phase 2D live verdict
9. R7 shadow live ≥ 30 → Phase 2B Stage 2 review

## Symbols & data

- Primary: ESM6 (E-mini S&P June 2026), 15m timeframe.
- Cache: `order_flow_engine/data/raw/ESM6_15m.parquet` (~7869 bars, 6 months).
- Live tail: `order_flow_engine/data/processed/ESM6_15m_live.parquet` + 1m equivalent.
- Historical real-flow: `order_flow_engine/data/processed/ESM6_15m_realflow_history.parquet` (18-day window, ~920 bars, all `historical_realflow_tick_rule`).
- Joined window: 1289 bars (cache ∩ real merged).

## Coordination files

- [PROJECT_STATE.md](PROJECT_STATE.md) — this file (decision snapshot)
- [NEXT_ACTIONS.md](NEXT_ACTIONS.md) — open work and proposed next moves
- [RUNBOOK.md](RUNBOOK.md) — common commands and recovery
- [outputs/order_flow/latest_status.md](outputs/order_flow/latest_status.md) — last diagnostic snapshot
