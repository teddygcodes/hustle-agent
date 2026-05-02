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

## Canonical Data Schema (READ BEFORE EDITING ANY HEURISTIC)

**Why this section exists.** Two schema mistakes in 24 hours during the May 1
Session 43b/44/45 cycle:
- Session 43b plan-time fix: `outcome_clv_cents` → `clv_cents`,
  `outcome_settlement` → `market_result`, `skip_reason` → `skipped_by_gate`
  for `clv.json` records
- Session 45 verification error: `market_result == 'no_won'` when actual
  canonical value is `'no'`. Returned 0 across all cohorts; correct count
  was 30/130. Falsified a Layer-3 disqualification — Layer-1 (no tunable
  threshold) saved the right outcome on a different rationale than documented

If a third instance happens, the next decision could ride on falsified
evidence. Single source of truth is `CLAUDE.md` "Canonical Data Schema
Reference" section; mirrored here for coder context. **If this section
disagrees with what you find on disk, the disk wins — but flag the
discrepancy and update both this README and CLAUDE.md in the same commit.**

### `bot/state/clv.json` records

| Field | Type | Values / Notes |
| --- | --- | --- |
| `trade_id` | str | `'PAPER-...'` for real trades; `'CF-{scan_id}-{ticker}'` for counterfactuals |
| `ticker` | str | full Kalshi ticker |
| `side` | str | `'yes'` \| `'no'` — the side our bot WOULD have / DID enter on |
| `entry_price` | float | cents 0-100, intended entry price |
| `fair_value` | float | cents 0-100, model's fair value at scan time |
| `recorded_at` | str | ISO 8601 UTC, when CF/trade was recorded |
| `settled_at` | str \| None | ISO 8601 UTC, when market settled (null until settlement) |
| `status` | str | `'open'` \| `'settled'` \| `'counterfactual_open'` \| `'counterfactual_settled'` |
| **`skipped_by_gate`** | str \| None | reject reason (CFs only — real trades skip this). **NOT `'skip_reason'`** |
| **`clv_cents`** | float \| None | CLV at settlement, signed. **NOT `'outcome_clv_cents'`** |
| **`market_result`** | str \| None | `'yes'` \| `'no'` — which side actually won. **NOT `'yes_won'` / `'no_won'`** (Session 45 error). None until settlement |
| `sport` | str \| None | sport family from ticker prefix |
| `regime` | dict | 5-axis: time_of_day, day_of_week, sport_phase, event_horizon_hr, match_phase |
| `extra` | dict | gate-specific: distance-from-threshold, no_ask_prob, floor, etc. |

**Counterfactual filter pattern (canonical):**
```python
cfs = [r for r in clv if r.get("status") == "counterfactual_settled"
       and r.get("skipped_by_gate") is not None
       and r.get("clv_cents") is not None]
```

**Survivorship n_no count (canonical):**
```python
n_no = sum(1 for r in cfs if r.get("market_result") == "no")
```
**NOT `'no_won'`.** This is the exact bug that produced the falsified
Layer-3 in Session 45. Reproduce by checking the agent's
`counterfactual_hotspots.py:64` — the heuristic uses `== "no"` and is
correct.

### `bot/state/decisions.jsonl` records

| Field | Type | Values / Notes |
| --- | --- | --- |
| **`ts`** | str | ISO 8601 UTC. Canonical timestamp for decisions. **NOT `'timestamp'`** (paper_trades uses that) |
| `ticker` | str | |
| **`opp_type`** | str | rich vocabulary: `vig_stack_series`, `vig_stack_futures`, `live_momentum`, etc. **DIFFERENT vocabulary from `paper_trades.type`** — DO NOT normalize |
| `edge` | float | signed |
| `gates` | dict | per-gate boolean pass/fail |
| **`decision`** | str | `'accept'` \| `'reject'`. **NOT `'take'` / `'skip'`** |
| **`reason`** | str | reject reason (when `decision='reject'`). **NOT `'skip_reason'`** |
| `extra` | dict | gate-specific context |
| `regime` | dict | 5-axis tag |

### `bot/state/paper_trades.json` records

| Field | Type | Values / Notes |
| --- | --- | --- |
| `id` | str | `'PAPER-...'` |
| `ticker` | str | |
| **`type`** | str | canonical opp_type field on paper_trades. **NOT `'opp_type'`**. Vocabulary is COARSER than `decisions.opp_type` — only `'vig_stack'` / `'live_momentum'` (no series/futures distinction). Vocabulary mismatch is intentional |
| `side` | str | `'yes'` \| `'no'` |
| `entry_price` | float | **0.0-1.0 dollars (NOT cents)** — different unit from clv.json |
| `exit_price` | float \| None | dollars |
| `contracts` | int | |
| `edge_at_entry` | float | |
| `confidence` | float | 0.0-1.0 |
| `pnl` | float \| None | dollars, signed |
| `status` | str | `'open'` \| `'won'` \| `'lost'` \| `'exited_early'` \| `'cancelled_stale'` |
| `exit_reason` | str \| None | `'auto_take_profit'` \| `'auto_cut_loss'` \| `'edge_flipped'` \| `'manual'` \| etc. Forward-only persistence since Session 36 (Apr 29) |
| **`timestamp`** | str | ISO 8601 UTC entry time. **NOT `'ts'`** (decisions.jsonl uses that) |
| `resolved_at` | str \| None | settlement time |

**Note:** `paper_trades.json` has NO `sport` field — derive from ticker prefix
via `_sport_classifier.sport_from_ticker_distinguished()`.

### Ticker prefix → sport map

The discovery agent's `_sport_classifier.sport_from_ticker_distinguished()`
distinguishes per-game from futures:

| Prefix | Sport (per-game) | Prefix | Sport (futures) |
| --- | --- | --- | --- |
| `KXMLBGAME-` | `mlb_game` | `KXMLB-` | `mlb_futures` |
| `KXNBAGAME-` | `nba_game` | `KXNBA-` | `nba_futures` |
| `KXNHLGAME-` | `nhl_game` | `KXNHL-` | `nhl_futures` |
| `KXATPMATCH-` | `atp` | `KXATPCHALLENGERMATCH-` | `atp_challenger` |
| `KXWTAMATCH-` | `wta` | `KXWTACHALLENGERMATCH-` | `wta_challenger` |
| `KXUFC` | `ufc` | `KXIPL` | `ipl` |
| `KXHIGH*` | `weather_high` | `KXLOW*` | `weather_low` |
| `KXINX*` | `index` | | |

**The bot's `_TICKER_PREFIX_TO_SPORT` map (in `bot/scanner.py` /
`bot/live_watcher.py`) is COARSER** — it doesn't distinguish per-game from
futures. Don't conflate. Discovery agent always uses
`_sport_classifier.sport_from_ticker_distinguished()`; bot code uses
`_TICKER_PREFIX_TO_SPORT`.

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
| `counterfactual_hotspots` | `(skipped_by_gate, sport)` buckets with high mean +CLV on settled CFs (Session 38a manual ATP query, automated) | `MIN_CF_COUNT=10`, `MIN_MEAN_CLV_CENTS=5.0`, `MIN_POSITIVE_CLV_RATE=0.60`, `MIN_NO_WON_COUNT=3` (survivorship guard), `LOOKBACK_DAYS=30`, `HIGH_SEVERITY_MEAN_CENTS=15.0`, `CROSS_COHORT_MEAN_DEMOTION_FLOOR=0.0¢`, `CROSS_COHORT_TRIMMED_MEAN_FLOOR=3.0¢`, `DISABLED_SPORT_DEMOTION=True` (Session 47) |
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

### `counterfactual_hotspots` refinement (Session 47, May 1)

Per Sessions 45 + 46 — both shipped Outcome C HOLD on the same failure mode:
per-cohort flag positive, cross-cohort distribution flat-or-negative,
strongest positive sports in `MOMENTUM_DISABLED_SPORTS` (gate-tuning
structurally neutralized — relaxing the gate produces zero new actual trades
for them). Each session burned ~1.5h re-deriving the same cross-cohort math
the heuristic could have computed once. Refinement adds:

- Cross-cohort context to every Finding's evidence (`cross_cohort_total_n`,
  `cross_cohort_n_sports`, `cross_cohort_mean_clv_cents`,
  `cross_cohort_trimmed_mean_clv_cents`,
  `cross_cohort_n_positive_sports` / `n_negative_sports`,
  `cross_cohort_breakdown` (top-10 by n desc), plus the gate-flow caveat keys
  `n_disabled_sport_cohorts_in_top3` and `this_cohort_is_disabled_sport`).
- Severity demotion ladder. Base = `high` if per-cohort mean ≥
  `HIGH_SEVERITY_MEAN_CENTS` else `notable`. Demote one step per condition:
  (a) cross-cohort raw mean < `CROSS_COHORT_MEAN_DEMOTION_FLOOR`, (b) trimmed
  mean < `CROSS_COHORT_TRIMMED_MEAN_FLOOR` AND raw ≤ 0, (c) cohort sport in
  `MOMENTUM_DISABLED_SPORTS` (with normalization — `nba_game` / `nba_futures`
  → `nba` for the comparison since the bot's set has flat names).
- Report rendering shows the cross-cohort context inline as a separate
  paragraph between summary and suggested_action. Block fires regardless of
  severity — even `high` findings benefit from the full picture.

Net effect: per-cohort flags are still emitted (data preserved), but the
report's NEW/HIGH section no longer surfaces cherry-picked-positive cohorts
as actionable candidates when cross-cohort context contradicts. Sessions 45
and 46 retroactively demote to `info` under this logic.

Single source of truth: `MOMENTUM_DISABLED_SPORTS` is imported from
`bot.config` (NOT hardcoded in the heuristic). Tunables live at the top of
`counterfactual_hotspots.py`: `CROSS_COHORT_MEAN_DEMOTION_FLOOR`,
`CROSS_COHORT_TRIMMED_MEAN_FLOOR`, `DISABLED_SPORT_DEMOTION`.

### Session 48 (May 1)

| Name | What it catches | Tunables (constants at top of file) |
| --- | --- | --- |
| `concurrent_attack_angles` | Two finding types: (a) `concurrent_fire_candidate` — events we already trade where settled CFs on a *different* market type within the *same event* show +CLV concurrent with our primary winning trades. (b) `scanner_gap` — high-volume market types in `universe.jsonl` within series we trade that never appear in `decisions.jsonl`. | `LOOKBACK_DAYS=30`, `MIN_CONCURRENT_PAIRS=5`, `MIN_MEAN_CONCURRENT_CLV_CENTS=5.0`, `MIN_CONCURRENT_POSITIVE_RATE=0.65`, `MIN_CONCURRENT_N_NO=2`, `MIN_GAP_EVENTS=10`, `MIN_GAP_AVG_VOLUME_24H=1000`, `CONCURRENT_PAIR_WINDOW_HOURS=2`, `CROSS_FAMILY_MEAN_DEMOTION_FLOOR=0.0`, `DISABLED_SPORT_DEMOTION=True`, plus `HIGH_SEVERITY_*` bumps |

### `concurrent_attack_angles` (Session 48, May 1)

Tyler's directive: *"discover new ways to bet on the same markets we are
already betting on. multiple strategies tied to the same game that if both
proven to work can fire at the same time."*

Search-frontier expander, not a refinement. The bot bets ONE attack angle per
event today; Kalshi typically lists many market types per event. This heuristic
surfaces where a winning view on one market should support edge on others.

Two finding types from data we already collect:

- **`concurrent_fire_candidate`**. For each event family the bot has touched in
  the last 30d, find primary-strategy WINNING trades and count CFs in the same
  event on a *different* market type within ±2h of the primary timestamp. If
  ≥5 such pairs share `(series_ticker, primary_strategy, candidate_market_type)`
  AND mean CF CLV ≥ +5¢ AND +CLV rate ≥ 65% AND n_no ≥ 2, emit. HIGH severity
  bumps at n=15 / mean=10¢ / +CLV=75% / n_no=5. Survivorship guard mirrors
  Session 47's `MIN_NO_WON_COUNT=3` shape.
- **`scanner_gap`**. For each event family the bot has trades on, the
  three-class universe split (`already_trading` / `scanned_not_taken` /
  `never_scanned`) yields a per-event "what we never looked at" set. Aggregate
  across the series by missing market type. If ≥10 events share the gap AND
  avg 24h volume ≥ $1,000, emit. HIGH at ≥50 events with avg vol ≥ $5,000.

**Cross-event-family demotion mirror (Session 47 pattern).** Per-tuple flags
are preserved (data + Finding), but severity demotes when the *same*
`(primary_strategy, candidate_market_type)` pair across ALL event-family
cohorts has cross-family raw mean < 0¢ — same cherry-pick-failure mode
Sessions 45/46 hit and Session 47 fixed for `counterfactual_hotspots`. Plus a
disabled-sport demotion when the primary trade's sport is in
`MOMENTUM_DISABLED_SPORTS` (relaxing the gate produces zero new actual primary
trades; the candidate angle would be structurally neutralized downstream).

**Schema discipline.** Only `universe.jsonl` carries `event_ticker`. For
`decisions.jsonl` / `clv.json` / `paper_trades.json`, derive the event family
via `ticker.rsplit("-", 1)[0]`. Tickers without a hyphen skip cleanly. CFs are
filtered to `status == 'counterfactual_settled'` only. Survivorship counts use
`market_result == 'no'` (canonical) — NOT `'no_won'` (Session 45 lesson).

**Coarse `_market_type_from_ticker` v1.** `prefix/team` for alpha-only tails
(team codes), `prefix/<letter><n>` for letter+digit tails (totals/spreads/period
markets), `prefix/numeric` for digit-only tails. Sufficient to distinguish
winner-side markets from totals/spreads within the same series. Player-prop
sub-classification stays out of scope for v1.

**Single source of truth.** `MOMENTUM_DISABLED_SPORTS` imported from
`bot.config`; `SPORT_PREFIXES` imported from `bot.regime`. Vocab normalizer
(`_normalize_sport_for_disabled_check`) reused from
`counterfactual_hotspots.py`.

**Acting on findings.** Treat surfaced candidates the way Sessions 44–46
treated the original `counterfactual_hotspots` findings: cross-family context
is built in to filter cherry-picks. NOTABLE/HIGH candidates that hold STABLE
for ≥3 daily runs become Session 48b/48c/... (build a new scanner / extend an
existing strategy). Auto-promotion is never the move.

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
