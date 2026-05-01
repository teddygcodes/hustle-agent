# Discovery Agent

Daily heuristic scan of all bot data. Surfaces outliers, emergent cohorts, and
deviations from baseline. **Pure stdlib Python** — no LLM, no API calls, no
plugin integration. Read-only on bot data; writes only to `bot/state/discovery/`.

Founding example: catches the +$172.52 SFPHI vig_stack_futures trade
(PAPER-4A16F5D2 on 2026-04-30). The regression test
`tests/test_discovery_sfphi_regression.py` locks the value prop — if it ever
breaks, the agent has lost its founding example.

## Run

```bash
python3 -m tools.discovery_agent.main
```

Outputs to `bot/state/discovery/discovery_report_YYYY-MM-DD.md` (human-readable)
and `discovery_findings_YYYY-MM-DD.jsonl` (machine-readable, drives next-run dedup).

## Data sources consumed

Eager-loaded:
- `paper_trades.json`, `trade_history.json`, `live_journal.json`,
  `positions.json`, `bot_state.json`, `clv.json`,
  `decisions.jsonl`, `predictions.jsonl`, `tracker_cadence.jsonl`,
  `strategy_audit.json`.

Streaming (memory-safe — peak RAM stays <50MB regardless of file size):
- `universe.jsonl` (~39MB), `live_ticks.jsonl` (~54MB),
  `bot/logs/bot.log` + rotated `.1`..`.5` (uncompressed).

SQLite (read-only): `outcomes.db`.

Declared but not currently on disk (skip cleanly with a load_warnings entry):
- `universe_archive/` directory
- `order_microstructure.jsonl`

A future heuristic that requires either will skip with a "skipped: missing
sources [...]" entry in the report's errors section.

## Heuristics shipped

### Session 43a (May 1)

| Name | What it catches | Tunables (constants at top of file) |
| --- | --- | --- |
| `outlier_pnl` | Single trade dominating its `(type, sport)` cohort over the last 30d | `OUTLIER_DOLLAR_THRESHOLD=$75`, `OUTLIER_PCT_OF_COHORT=30%`, `HIGH_SEVERITY_PCT=50%`, `LOOKBACK_DAYS=30` |
| `cohort_emergence` | A `(opp_type, sport, source)` cohort appearing in last 7d but absent in prior 30d | `EMERGENCE_WINDOW_DAYS=7`, `PRIOR_WINDOW_DAYS=30`, `MIN_NEW_OPP_COUNT=3` |

### Session 43b (May 1)

| Name | What it catches | Tunables (constants at top of file) |
| --- | --- | --- |
| `threshold_proximity` | Reject-gates where many recent rejects fell within 5% of their threshold (alpha potentially surrendered); cross-references near-miss tickers to clv +CLV records | `THRESHOLD_REASONS` (3 reasons: edge_below_threshold, non_stable_below_weather_floor, no_price_below_floor), `PROXIMITY_BAND_PCT=0.05`, `MIN_REJECTS_PER_BUCKET=5`, `LOOKBACK_DAYS=14` |
| `counterfactual_hotspots` | `(skipped_by_gate, sport)` buckets with high mean +CLV on settled CFs (Session 38a manual ATP query, automated) | `MIN_CF_COUNT=10`, `MIN_MEAN_CLV_CENTS=5.0`, `MIN_POSITIVE_CLV_RATE=0.60`, `MIN_NO_WON_COUNT=3` (survivorship guard), `LOOKBACK_DAYS=30`, `HIGH_SEVERITY_MEAN_CENTS=15.0` |
| `universe_gap` | `(sport, market_type)` pairs decisions log touched ≥5×/7d but absent from latest universe snapshot — scanner regression / Kalshi delisting | `LOOKBACK_DAYS=7`, `MIN_DECISION_COUNT=5` |
| `live_tick_anomalies` | Tickers with ≥3 price jumps of ≥15¢ vs rolling median (window=5 ticks); cross-checks open positions + paper trade overlap. **Streaming**, peak memory <50MB | `JUMP_THRESHOLD_CENTS=15`, `WINDOW_TICKS=5`, `MIN_JUMPS_PER_TICKER=3` |
| `cadence_outcome` | `tracker_cadence._position_check_loop` median ms-buckets where mean P&L lands ≥1 std dev below global mean — quantifies "exits firing late when loop is slow" | `CADENCE_BUCKETS_MS=[10000, 20000, 35000, 60000, 120000]`, `MIN_TRADES_PER_BUCKET=10`, `WINDOW_BEFORE_EXIT_HOURS=1`, `ALERT_STD_DEVS_BELOW_MEAN=1.0` |
| `log_error_spike` | bot.log error-message fingerprints with ≥3× recent-rate / baseline-rate (24h vs 168h windows). **Streaming**, peak memory <50MB. Would have flagged the Apr 30 12-hour wedge | `BASELINE_WINDOW_HOURS=168`, `RECENT_WINDOW_HOURS=24`, `SPIKE_RATIO=3.0`, `MIN_RECENT_COUNT=5`, `HIGH_SEVERITY_RATIO=10.0`, `HIGH_SEVERITY_MIN_COUNT=20`, `FINGERPRINT_PREFIX_LEN=80` |

### `cohort_emergence` refinement (Session 43b, May 1)

Per the Session 43-investigate finding: the original heuristic over-stated the
SFPHI lead because it counted decisions records, not unique tickers or accepts.
Refinement adds:

- `unique_tickers_recent`, `accepts_recent`, `paper_trades_recent` to evidence
- Severity demotes to `info` when `accepts_recent == 0 AND paper_trades_recent == 0` —
  a cohort with thousands of decisions but zero accepts is interesting context
  but not actionable
- Sport classification distinguishes futures from per-game via
  `tools/discovery_agent/_sport_classifier.py` (KXMLB-* → `mlb_futures`,
  KXMLBGAME-* → `mlb_game`, etc.). Wrapper is local to discovery_agent — does
  NOT modify `bot/regime.py:SPORT_PREFIXES` (canonical for 5 regime-axis writers)

**Vocabulary policy.** `decisions.opp_type` and `paper_trades.type` are kept
SEPARATE — the `cohort_emergence` cohort key includes the source name. The
vocabulary mismatch (e.g. `vig_stack_futures` in decisions vs `vig_stack` in
paper_trades) IS the signal we want to surface, not noise to normalize away.

## How to add a heuristic (Session 43b and beyond)

1. Create `tools/discovery_agent/heuristics/<name>.py`:

   ```python
   from ..findings import Finding

   THRESHOLD_X = ...  # tunable at top of file

   class MyHeuristic:
       name = "my_heuristic"
       data_sources = ("decisions", "live_ticks_iter")  # ctx attribute names

       def run(self, ctx) -> list[Finding]:
           ...
   ```

2. Every `Finding.evidence` dict MUST include `_fingerprint_keys: list[str]` —
   the evidence-keys whose values define identity for cross-run dedup.

3. Add an instance to `DEFAULT_HEURISTICS` in `tools/discovery_agent/main.py`.

4. Add `tests/test_discovery_<name>.py` covering positive, negative, boundary,
   and missing-source cases.

5. Tunable thresholds at the top of the heuristic file. No magic numbers in logic.

## Dedup (NEW / STABLE / RESOLVED)

Each finding has a 16-char SHA256 fingerprint over `(heuristic, *fingerprint_key_values)`.

- **NEW**: fingerprint not in yesterday's findings file
- **STABLE**: fingerprint in both yesterday and today
- **RESOLVED**: in yesterday but not today

Yesterday's file is `discovery_findings_YYYY-MM-DD.jsonl` from
`bot/state/discovery/`. The most recent JSONL file (by date in filename) is the
baseline — gaps in the schedule degrade gracefully (a 3-day gap just compares
against the most recent run).

## Heuristic isolation

One broken heuristic does NOT abort the rest. Each runs under a try/except in
`main.py`; failures land in the report's "Heuristic errors / skips" section
with the full traceback. Schema-aware skip: if a heuristic's declared
`data_sources` include an empty container, it skips cleanly with
`skipped: missing sources [...]`.

## Scheduling

Daily 6:00 AM ET via launchd:
`~/Library/LaunchAgents/com.hustle-agent.discovery.plist`.

Disable temporarily:
```bash
launchctl unload ~/Library/LaunchAgents/com.hustle-agent.discovery.plist
```

## Layout note

This is the only `tools/<name>/` subdirectory in the repo (others are single
`.py` files). The 9-file architecture justifies the directory: framework
(context, findings, base) + heuristics package + main + README. Future tools
that grow beyond 2-3 files should follow this pattern.

`tools/` is gitignored at the repo level; `tools/discovery_agent/` is
re-included via `!tools/discovery_agent/`.

## Tests

```bash
python3 -m pytest tests/test_discovery_*.py -v
```

Each test should complete in <5s. If a test hangs, that's a bug to investigate
(no `pytest-timeout` suppression is wired into the verification commands).
