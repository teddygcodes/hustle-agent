# Regime Tagger Design - Session 104 Outcome B

Date: 2026-05-11

## Decision

Do not ship a regime classifier or global `size_multiplier` in Session 104.

The premise check failed the implementation bar. The last-30-day paper ledger has
327 settled trades, but only 27 entry days. That is below the requested minimum
of 30 days with at least 3 trades per bucket, and the measured regime effect is
mixed rather than globally directional.

The high-level read:

- `vig_stack` does not justify a high-volatility size-down. Its win rate rises
  across the primary volatility buckets.
- `live_momentum` degrades in the all-settled high-volatility bucket, but the
  current-active-only cohort is too thin and no longer confirms the same effect.
- A global high-volatility multiplier would likely punish the current strongest
  strategy (`vig_stack`) while leaning on a `live_momentum` signal confounded by
  disabled sports and small sample size.

## Data Sources And Proxy

Primary source:

- `bot/state/paper_trades.json`, filtered to settled statuses:
  `won`, `lost`, `exited_early`.

Volatility proxy:

- Daily mean absolute `yes_ask` movement from archived/current universe snapshots:
  `bot/state/archive/universe-*.jsonl.gz` plus `bot/state/universe.jsonl`.
- Fallback for early days without universe snapshots: daily mean absolute
  live-tick price movement from `live_ticks*.jsonl`.

Supporting slices:

- `decisions*.jsonl` edge dispersion and reject pressure were inspected as
  sensitivity checks.
- `outlier_pnl` count was considered but not used as the main classifier input
  because it is outcome-tainted.

P&L caveat:

- Per-trade P&L is materially confounded by the Apr 29 paper-balance bump from
  `$500` to `$10,500`; post-bump trades are much larger. Win rate is the safer
  first-pass comparison across the whole Apr 15-May 11 ledger.

## Bucket Results

Primary proxy: daily mean absolute market movement. Tercile cutoffs on entry
days with settled trades were approximately `1.3161` and `3.8674`.

All settled trades:

| Bucket | Strategy | Days | Trades | WR | Avg P&L | Total P&L |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Low | live_momentum | 9 | 46 | 60.9% | $0.41 | $18.88 |
| Low | vig_stack | 8 | 82 | 63.4% | -$1.47 | -$120.35 |
| Mid | live_momentum | 7 | 25 | 60.0% | -$1.07 | -$26.67 |
| Mid | vig_stack | 8 | 66 | 78.8% | -$0.48 | -$31.68 |
| High | live_momentum | 8 | 37 | 37.8% | -$1.12 | -$41.37 |
| High | vig_stack | 9 | 71 | 87.3% | $15.17 | $1077.02 |

Current-active-only slice:

- Excludes vig_stack families disabled by Session 93: `KXHIGHCHI`, `KXINX`.
- Excludes live_momentum sports disabled by Session 97: `atp`,
  `atp_challenger`, `nba_game`, `wta`, `wta_challenger`.

| Bucket | Strategy | Days | Trades | WR | Avg P&L | Total P&L |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Low | live_momentum | 3 | 6 | 66.7% | $1.25 | $7.48 |
| Low | vig_stack | 9 | 60 | 60.0% | -$1.62 | -$96.93 |
| Mid | live_momentum | 6 | 18 | 55.6% | -$0.47 | -$8.42 |
| Mid | vig_stack | 7 | 40 | 80.0% | $2.54 | $101.42 |
| High | live_momentum | 6 | 12 | 58.3% | $4.76 | $57.15 |
| High | vig_stack | 9 | 50 | 88.0% | $21.25 | $1062.66 |

Interpretation:

- The `vig_stack` high-volatility signal points the opposite direction from the
  proposed global high-volatility size-down.
- The `live_momentum` all-settled high-volatility weakness is real enough to
  watch, but current-active-only N is too small to justify a new sizing layer.
- The result supports a future per-strategy regime analysis, not a global
  multiplier.

## Future Design If Revisited

Classifier:

- Start with three states: `calm`, `normal`, `volatile`.
- Use trailing daily market-motion percentiles, not realized P&L or outlier
  counts, as the classifier input.
- Update hourly to avoid scan-by-scan whipsaw.
- Expose the current value in `bot/state/bot_state.json` as `current_regime`,
  with supporting fields such as `current_regime_score`,
  `current_regime_updated_at`, and `current_regime_multiplier`.

Multiplier:

- Do not use a global multiplier unless future data shows both active strategies
  degrade in the same regime direction.
- Prefer per-strategy multipliers if the effect persists:
  `{"vig_stack": ..., "live_momentum": ...}`.
- Derive multiplier magnitudes from forward data. Small WR gaps should map to
  small multipliers.

Hook point:

- Future hook should apply after Kelly sizing in `bot/sizing.py`, before the
  existing dollar/family caps bind.
- S92 family caps, S93 disabled families, S97 disabled sports, and S90
  re-entry breaker remain orthogonal and must still win downstream.

## Revisit Trigger

Re-run the analysis when either condition is met:

- At least 60 calendar days of settled trade data exist.
- There are at least 30 current-active settled trades per strategy per
  volatility bucket.

Until then, regime sizing remains leverage-without-enough-edge and should stay
out of the production sizing path.
