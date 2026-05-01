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

## Heuristics shipped (Session 43a)

| Name | What it catches | Tunables (constants at top of file) |
| --- | --- | --- |
| `outlier_pnl` | Single trade dominating its `(type, sport)` cohort over the last 30d | `OUTLIER_DOLLAR_THRESHOLD=$75`, `OUTLIER_PCT_OF_COHORT=30%`, `HIGH_SEVERITY_PCT=50%`, `LOOKBACK_DAYS=30` |
| `cohort_emergence` | A `(opp_type, sport, source)` cohort appearing in last 7d but absent in prior 30d | `EMERGENCE_WINDOW_DAYS=7`, `PRIOR_WINDOW_DAYS=30`, `MIN_NEW_OPP_COUNT=3` |

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
