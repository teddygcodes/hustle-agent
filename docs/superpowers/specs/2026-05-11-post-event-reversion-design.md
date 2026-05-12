# Post-Event Reversion Design - Session 106 Outcome B

Date: 2026-05-11

## Decision

Do not ship a `post_event_reversion` discovery heuristic in Session 106.

The strategy class is still worth keeping alive, but the current local data
substrate is too sparse and censored to trust an automated heuristic. Outcome A
would create a strategy-candidate surface that looks quantitative while mostly
measuring which markets happen to remain observable 24 hours after a price jump.
Outcome C is too strong because the broad universe snapshot feed is close to the
right substrate and could become usable with more cadence and history.

Session 106 therefore ships Outcome B: document the substrate gap and the exact
revisit gates for a future v1.

## Substrate Check

### Live Ticks

`bot/state/live_ticks.jsonl` plus rotated archives currently contains:

- Rows: `471,329`
- Unique tickers: `861`
- Time range: `2026-04-09T05:14:06Z` to `2026-05-11T23:29:58Z`
- Dominant coverage: live sports (`nba`, `atp_challenger`, `atp`, `wta`,
  `nhl`, `wta_challenger`, `mlb`, `ufc`, `ipl`)

This is useful for live-game replay, but not for a 24-72h post-event reversion
heuristic. Against the resolved paper ledger:

- Resolved paper trades inspected: `327`
- Resolved tickers with any live ticks: `102`
- Resolved tickers with at least 24h of pre-resolution live-tick coverage: `9`
- Resolved tickers with at least 24h of post-resolution live-tick coverage: `0`

Live ticks stop around live-game windows and do not carry the post-event tail
needed to classify reversion after an inferred price shock.

### Universe Snapshots

`bot/state/universe.jsonl` plus archived `universe-*.jsonl.gz` currently
contains:

- Rows: `524,084`
- Unique tickers: `120,792`
- Time range: `2026-04-25T16:43:33Z` to `2026-05-11T23:01:50Z`
- Scan count: `342`
- Scan-gap p50 / p75 / p90 / max: approximately `37.8m / 65.8m / 135.9m / 581.7m`
- Status rows: `524,000 active`, `84 closed`
- Broad settlement/result keys: none observed

Universe snapshots are the right future source because they cover non-live
markets such as weather, index, crypto, and sports props at scan time. They are
not yet reliable enough for the heuristic because the archive is only about 16
days deep, scan cadence is too coarse for short event detection, and most
detected jumps cannot be observed again near +24h.

## Event Detection Probe

The probe used universe snapshots only, with a liquidity floor of
`volume_24h >= 10000`, because thin-market quote noise can look like a price
event. Price movement was measured as the change in midpoint:

```text
mid_cents = (yes_bid + yes_ask) / 2
```

Observed absolute midpoint-change distribution for adjacent liquid snapshots
within <=180 minutes:

| Percentile | Move |
| --- | ---: |
| p50 | 0.0c |
| p75 | 1.0c |
| p90 | 9.5c |
| p95 | 19.5c |
| p97.5 | 29.5c |
| p99 | 43.5c |

This makes a 10c threshold a permissive exploratory setting and a roughly 20c
threshold closer to a defensible p95 event threshold. Neither currently clears
the validation bar:

| Window | Threshold | Events | Observable +24h | Reverted | Reversion Rate | Indeterminate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| <=120m | 5c | 2,541 | 95 | 16 | 16.8% | 2,446 |
| <=120m | 10c | 2,122 | 40 | 3 | 7.5% | 2,082 |
| <=120m | 15c | 1,779 | 27 | 2 | 7.4% | 1,752 |
| <=120m | 20c | 1,449 | 13 | 2 | 15.4% | 1,436 |

The dominant finding is not "mean reversion is dead." It is "the observable
slice is too censored to trust." Most events, especially short-horizon sports
props and totals, settle or disappear before a clean +24h classification point.
The small observable sample is heavily shaped by longer-lived series and weather
markets, which is not representative enough to promote or kill the strategy
class.

## Future V1 Methodology

Keep the zero-LLM constraint. No news feeds, no NLP, no external event labels.
The event must be inferred from price and volume signals only.

Future deterministic shape:

- Source: universe snapshots, not live ticks, unless live ticks are extended to
  non-live markets and preserve a post-event tail.
- Liquidity filter: require a data-derived baseline liquidity floor, starting
  from `volume_24h >= 10000` until a better per-family floor is measured.
- Event definition: adjacent midpoint move above a data-derived percentile
  threshold within a bounded time window; current evidence suggests p95 liquid
  movement is about 20c, but this is not a shipped parameter.
- Reversion definition: for event price move `P0 -> P1`, classify the nearest
  snapshot around +24h as reverted when it is closer to `P0` than `P1` by the
  chosen margin; classify persisted when it remains at or beyond `P1`; classify
  indeterminate when no +24h observation exists.
- Candidate emission: only after validation clears the gates below. Findings
  should include event count, observable outcome count, reversion rate, median
  reversion magnitude, series/family breakdown, and indeterminate rate.

## Revisit Gates

Re-open `post_event_reversion` only when all implementation gates are met:

- At least `30` days of archived universe snapshots exist.
- At least `30` observable +24h event outcomes remain after the liquidity floor
  and data-derived event threshold are applied.
- Non-live-market scan cadence is preferably `<=10m`, or the event window is
  explicitly widened and validated against the observed cadence.
- If the future heuristic requires settled-only validation, broad settled-market
  outcomes must exist outside our own `paper_trades.json`; otherwise validation
  remains biased toward the strategies we already trade.

Outcome A should require at least `N >= 30` observable events and a reversion
rate materially above random, with the original session bar of `>=60%` as the
default. Borderline evidence such as `55%` at `N=25` remains Outcome B.

## What Did Not Change

- No heuristic file was added.
- No discovery-agent registration changed.
- No `tools/glint_status.py` strategy-candidate surface changed.
- No promotion-bar threshold was added to `CLAUDE.md`.
- No bot, scanner, executor, strategy, or state-schema behavior changed.
- No bot restart was needed.

