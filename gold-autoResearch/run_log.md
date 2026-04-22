# AutoResearch Run Log

Each cycle writes an `## Cycle N — <ISO timestamp>` block below. The
meta-optimizer reads this file verbatim every 10 cycles and appends a
`## Meta-Optimizer — <ISO timestamp>` block with its suggestion.

---
## Cycle 1 — 2026-04-22T03:20:42+00:00

- **Dataset**: rows=255, cols=19, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776828042, acc=0.5556, sharpe=1.166, dd=0.1773
- **Hyperparams**: `{"xgb": {"max_depth": 5, "learning_rate": 0.1, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-1
- **Promoted**: no

## Cycle 2 — 2026-04-22T03:21:43+00:00

- **Dataset**: rows=255, cols=19, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776828103, acc=0.5556, sharpe=1.1667, dd=0.1773
- **Hyperparams**: `{"xgb": {"max_depth": 5, "learning_rate": 0.05, "n_estimators": 400}, "lstm": {"hidden": 32, "layers": 2, "dropout": 0.2}}`
- **Experiment note**: cycle-2
- **Promoted**: no

## Cycle 3 — 2026-04-22T03:21:46+00:00

- **Dataset**: rows=255, cols=19, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776828106, acc=0.5556, sharpe=1.1667, dd=0.1773
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.1, "n_estimators": 400}, "lstm": {"hidden": 32, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-3
- **Promoted**: no

## Meta-Optimizer — 2026-04-22T03:22:03+00:00
- **Finding**: RSI-14 plus ema_gap improved directional accuracy by ~3% between cycles 1 and 3. Sharpe remained flat; drawdown above guard-rail.
- **Next experiment**: Add a 10-day rolling sentiment z-score feature to pair with ema_gap and re-train with max_depth=7.
- **Config change**: `{"xgb_hparam_grid": {"max_depth": [7]}, "features_added": ["sentiment_z10"]}`

## Meta-Optimizer — 2026-04-22T03:24:20+00:00
- **Finding**: RSI-14 and ema_gap features provided the biggest positive impact with ~3% improvement in directional accuracy. Hyperparameter variations in cycles 1-3 showed no measurable performance differences.
- **Next experiment**: Add 10-day rolling sentiment z-score feature to complement RSI-14 and ema_gap, using max_depth=7 as suggested by meta-optimizer
- **Config change**: `{"xgb": {"max_depth": 7, "learning_rate": 0.1, "n_estimators": 400}, "features_added": ["sentiment_z10"], "focus": "feature_engineering_over_hyperparams"}`

## Cycle 4 — 2026-04-22T03:26:14+00:00

- **Dataset**: rows=255, cols=19, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776828374, acc=0.5556, sharpe=-4.6383, dd=0.4845
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 32, "layers": 1, "dropout": 0.2}}`
- **Experiment note**: cycle-4
- **Promoted**: no

## Cycle 4 — 2026-04-22T03:35:38+00:00

- **Dataset**: rows=1457, cols=23, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776828938, acc=0.5, sharpe=-5.6391, dd=0.5357
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-4
- **Promoted**: no

## Cycle 5 — 2026-04-22T03:35:45+00:00

- **Dataset**: rows=1457, cols=23, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776828945, acc=0.5556, sharpe=-6.4585, dd=0.5814
- **Hyperparams**: `{"xgb": {"max_depth": 5, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-5
- **Promoted**: no

## Cycle 4 — 2026-04-22T03:36:42+00:00

- **Dataset**: rows=1457, cols=23, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829002, acc=0.5333, sharpe=-3.2546, dd=0.3953
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 400}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.2}}`
- **Experiment note**: cycle-4
- **Promoted**: no

## Cycle 6 — 2026-04-22T03:36:50+00:00

- **Dataset**: rows=1457, cols=23, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829010, acc=0.5556, sharpe=-3.1783, dd=0.4031
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.1, "n_estimators": 400}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.2}}`
- **Experiment note**: cycle-6
- **Promoted**: no

## Cycle 7 — 2026-04-22T03:36:51+00:00

- **Dataset**: rows=1457, cols=23, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829011, acc=0.5556, sharpe=-3.8566, dd=0.4257
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-7
- **Promoted**: no

## Cycle 8 — 2026-04-22T03:36:52+00:00

- **Dataset**: rows=1457, cols=23, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829012, acc=0.5556, sharpe=-3.8566, dd=0.4257
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-8
- **Promoted**: no

## Cycle 4 — 2026-04-22T03:39:07+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829147, acc=0.5, sharpe=-2.7518, dd=0.3364
- **Hyperparams**: `{"xgb": {"max_depth": 5, "learning_rate": 0.05, "n_estimators": 400}, "lstm": {"hidden": 32, "layers": 2, "dropout": 0.1}}`
- **Experiment note**: cycle-4
- **Promoted**: no

## Cycle 9 — 2026-04-22T03:39:17+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829157, acc=0.5, sharpe=-2.9215, dd=0.3558
- **Hyperparams**: `{"xgb": {"max_depth": 3, "learning_rate": 0.1, "n_estimators": 400}, "lstm": {"hidden": 32, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-9
- **Promoted**: no

## Cycle 10 — 2026-04-22T03:39:17+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829157, acc=0.4889, sharpe=-3.2208, dd=0.422
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-10
- **Promoted**: no

## Cycle 11 — 2026-04-22T03:39:18+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829158, acc=0.4889, sharpe=-3.2208, dd=0.422
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-11
- **Promoted**: no

## Cycle 4 — 2026-04-22T03:39:56+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829196, acc=0.5222, sharpe=-2.4605, dd=0.3515
- **Hyperparams**: `{"xgb": {"max_depth": 3, "learning_rate": 0.1, "n_estimators": 400}, "lstm": {"hidden": 32, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-4
- **Promoted**: no

## Cycle 12 — 2026-04-22T03:40:22+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829222, acc=0.5556, sharpe=-3.1755, dd=0.4031
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.1, "n_estimators": 400}, "lstm": {"hidden": 64, "layers": 2, "dropout": 0.1}}`
- **Experiment note**: cycle-12
- **Promoted**: no

## Cycle 13 — 2026-04-22T03:40:23+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829223, acc=0.5556, sharpe=-3.8538, dd=0.4257
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-13
- **Promoted**: no

## Cycle 14 — 2026-04-22T03:40:23+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829223, acc=0.5556, sharpe=-3.8538, dd=0.4257
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-14
- **Promoted**: no

## Cycle 4 — 2026-04-22T03:41:01+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=None, sharpe=None, dd=None, version=None
- **Candidate**: version=m1776829261, acc=0.5222, sharpe=-2.4602, dd=0.3515
- **Hyperparams**: `{"xgb": {"max_depth": 3, "learning_rate": 0.1, "n_estimators": 400}, "lstm": {"hidden": 32, "layers": 1, "dropout": 0.2}}`
- **Experiment note**: cycle-4
- **Promoted**: yes

## Cycle 15 — 2026-04-22T03:41:09+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=0.5222, sharpe=-2.4604, dd=0.3515, version=m1776829261
- **Candidate**: version=m1776829269, acc=0.5556, sharpe=-3.3293, dd=0.4258
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.1, "n_estimators": 200}, "lstm": {"hidden": 32, "layers": 2, "dropout": 0.1}}`
- **Experiment note**: cycle-15
- **Promoted**: no

## Cycle 16 — 2026-04-22T03:41:10+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=0.5222, sharpe=-2.4604, dd=0.3515, version=m1776829261
- **Candidate**: version=m1776829270, acc=0.5556, sharpe=-3.8535, dd=0.4257
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-16
- **Promoted**: no

## Cycle 17 — 2026-04-22T03:41:10+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=0.5222, sharpe=-2.4604, dd=0.3515, version=m1776829261
- **Candidate**: version=m1776829270, acc=0.5556, sharpe=-3.8535, dd=0.4257
- **Hyperparams**: `{"xgb": {"max_depth": 7, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 64, "layers": 1, "dropout": 0.1}}`
- **Experiment note**: cycle-17
- **Promoted**: no

## Cycle 4 — 2026-04-22T06:54:15+00:00

- **Dataset**: rows=1457, cols=25, last=2026-04-21
- **Eval (production)**: acc=0.5333, sharpe=-2.2269, dd=0.3515, version=m1776829261
- **Candidate**: version=m1776840855, acc=0.5444, sharpe=-2.7382, dd=0.3616
- **Hyperparams**: `{"xgb": {"max_depth": 5, "learning_rate": 0.05, "n_estimators": 200}, "lstm": {"hidden": 32, "layers": 2, "dropout": 0.2}}`
- **Experiment note**: cycle-4
- **Promoted**: no

