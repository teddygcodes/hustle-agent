# Sweep (TP/SL): grid=12 variants × train=35 → top-3 → test=16, slippage=2¢, fixed (LM=0.65, TS=6¢)
# Tick-Replay Sweep — Session 41 (TP/SL ratio)

**Sample.** Train N=35 (`2026-04-23T00:01:49.935391+00:00` → `2026-05-04T01:40:24.079442+00:00`); Test N=16 (`2026-05-05T03:32:15.498179+00:00` → `2026-05-13T01:35:54.343645+00:00`). Trades sorted ascending by entry timestamp; 70/30 split by count.
**Slippage.** 2¢ per round-trip pessimism (Prereq 4).
**Baseline.** LIVE_TAKE_PROFIT_CENTS=12, LIVE_STOP_LOSS_CENTS=30 (production, ratio=0.40).
**Held fixed.** MOMENTUM_LEADER_MIN=0.65, MOMENTUM_DQS_TRAIL_STOP=6¢ (post-Session-19c production).
**Sport-profile caveat.** Tennis-alias sports have explicit `take_profit=15`/`stop_loss=8` in SPORT_PROFILES that override the global default. This sweep affects only sports without that override (NBA / NHL / UFC / IPL / etc.).

## Training sweep (full grid)

| Variant | Σ P&L¢ | n_replays | n_wins | win % |
|---|---|---|---|---|
| TP=12 SL=20 | +277 | 31 | 15 | 48% |
| TP=12 SL=25 | +277 | 31 | 15 | 48% |
| TP=12 SL=30 ← baseline | +277 | 31 | 15 | 48% |
| TP=12 SL=35 | +277 | 31 | 15 | 48% |
| TP=10 SL=20 | +184 | 31 | 15 | 48% |
| TP=10 SL=25 | +184 | 31 | 15 | 48% |
| TP=10 SL=30 | +184 | 31 | 15 | 48% |
| TP=14 SL=20 | -179 | 31 | 14 | 45% |
| TP=14 SL=25 | -179 | 31 | 14 | 45% |
| TP=14 SL=30 | -179 | 31 | 14 | 45% |
| TP=16 SL=20 | -179 | 31 | 14 | 45% |
| TP=16 SL=25 | -179 | 31 | 14 | 45% |

## Test validation (top 3 training variants + baseline)

| Variant | Train Σ¢ | Test Σ¢ | Δ vs baseline test¢ | Test n | Test per-trade Δ¢ |
|---|---|---|---|---|---|
| TP=12 SL=20 | +277 | -1716 | +0 | 13 | +0.0 |
| TP=12 SL=25 | +277 | -1716 | +0 | 13 | +0.0 |
| TP=12 SL=30 ← baseline | +277 | -1716 | 0 | 13 | 0.0 |

## Per-sport breakdown — best test variant (TP=12 SL=20)

| Sport | n_replays | Σ P&L¢ |
|---|---|---|
| KXIPLGAME | 3 | -1646 |
| KXNBAGAME | 6 | -4 |
| KXNHLGAME | 2 | +138 |
| KXUFCFIGHT | 2 | -204 |

## Regime slicing — best test variant (TP=12 SL=20)

### By time_of_day

| time_of_day | n_replays | Σ P&L¢ |
|---|---|---|
| afternoon | 3 | -566 |
| evening | 7 | -80 |
| morning | 1 | -1182 |
| overnight | 2 | +112 |

### By day_of_week

| day_of_week | n_replays | Σ P&L¢ |
|---|---|---|
| fri | 1 | -222 |
| mon | 3 | -1068 |
| sat | 3 | +32 |
| sun | 2 | -650 |
| thu | 2 | -22 |
| tue | 2 | +214 |

### By sport_phase

| sport_phase | n_replays | Σ P&L¢ |
|---|---|---|
| _none | 5 | -1850 |
| playoffs | 8 | +134 |

## Decision gate (Session 41 spec)

Pattern A (ship): one variant has test per-trade Δ ≥ +50¢ vs baseline AND train/test sign agreement (both positive).  Pattern B (ship best + watch-list): multiple positive variants but no clear best.  Pattern C (no ship): no variant beats baseline at the +50¢ test-Δ gate. Mirror Session 18.5 / 38a-2 / 40 outcomes.

## Findings

**Session 129 classification: (a) — SL-axis-flat finding STILL HOLDS.**

Raw S41-filter cohort is `N=69` settled post-Apr-23 `live_momentum` trades:
status mix `exited_early=46 / won=17 / lost=6`; sport mix by ticker
`nba=18 / nhl=10 / ipl=13 / ufc=10 / atp=18`. The sweep CLI applies the
current S97 disabled-sport gate, excluding `atp=18`, so the replayable sweep
cohort is `N=51` with train/test split `35/16`.

Operator caveat correction: the generated caveat above is stale. Current
`SPORT_PROFILES` sets TP/SL for NBA, NHL, UFC, and tennis aliases; only IPL
falls through to the global TP/SL defaults among this cohort. That makes S42's
per-sport architectural caveat load-bearing for any future TP/SL shipping
decision.

Training table, S129:

| TP \ SL | 20 | 25 | 30 (baseline) | 35 |
|---|---:|---:|---:|---:|
| 10 | +184 | +184 | +184 | — |
| 12 | +277 | +277 | +277 | +277 |
| 14 | -179 | -179 | -179 | — |
| 16 | -179 | -179 | — | — |

S41 comparison: same structure as the N=31 table (`TP=12` row led training;
all valid SL values tied within every TP row), but the expanded cohort reduces
the TP=12 training edge from `+886¢` to `+277¢` and pushes TP=14/16 negative
(`-179¢`).

Test validation: top-3 training variants were all `TP=12` and all tied the
baseline exactly on test (`-1716¢`, `13` replays, `+0.0¢/trade` delta). No TP
variant meets the `+50¢/trade` Pattern A gate.

Test per-sport context for the best test variant (`TP=12 SL=20`): IPL drove
the loss (`KXIPLGAME n=3, -1646¢`), UFC was negative (`KXUFCFIGHT n=2,
-204¢`), NBA was near-flat (`KXNBAGAME n=6, -4¢`), and NHL remained positive
(`KXNHLGAME n=2, +138¢`).

Test day-of-week context: `mon n=3 -1068¢`, `sun n=2 -650¢`,
`fri n=1 -222¢`, `thu n=2 -22¢`, `sat n=3 +32¢`, `tue n=2 +214¢`.

Decision: Pattern C / no config change. Update the S41 watch-list from
"small-N SL flatness hypothesis" to "global SL axis confirmed structurally
flat at raw N=69 / sweep N=51 under current architecture." Re-open global
TP/SL only if the no-profile/global-fallback cohort (principally IPL) becomes
large enough to test independently, or open a separate per-sport TP/SL session
if a future per-sport signal warrants it.
