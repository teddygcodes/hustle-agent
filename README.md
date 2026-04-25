# Hustle Agent — Glint Trading Bot

An autonomous prediction-market trading system built on top of Kalshi's REST API. It runs a continuous scan→rank→size→execute loop across a curated set of active strategies, enforces a multi-layer position safety chain, and manages a full paper-trading sandbox before any real money changes hands.

The original agentic reasoning layer (`agent/`) still exists and owns the Kalshi API client, but **Glint** (`bot/`) is the production system. This document covers the bot in depth.

---

## Current Status (Apr 25, 2026)

- **Mode:** `PAPER_MODE = True` — $500 simulated balance, full pipeline, zero live orders.
- **Active strategies (`ACTIVE_STRATEGIES`):** `vig_stack_series`, `vig_stack_futures`, `sports_monotonicity_arb`, `sports_consistency_arb`. Plus `live_momentum` via the live-game watcher subsystem.
- **Paper performance** (post-Apr-20 settlement-pipeline rebuild — from `bot/state/paper_trades.json`, the ground-truth ledger):
  - `vig_stack_series`: 54 settled, **−$110.62**, 29W/25L (54% WR)
  - `live_momentum`: 39 settled, **+$12.30**, 24W/15L (62% WR)
  - Net: **≈ −$98** across the whole history
- **Filter F (Apr 18 → Apr 20):** `vig_stack` entries are whitelisted to stable families (`KXHIGHMIA`, `KXHIGHAUS`, `KXINX`) at 0.70+; volatile families require **NO ≥ 0.93** (raised from 0.90 on Apr 20 after bucket analysis showed only [92-96¢) is breakeven on volatile ladders).
- **Tennis disabled (Apr 20):** `MOMENTUM_DISABLED_SPORTS = {atp, atp_challenger, wta, wta_challenger}` blocks new live-momentum entries on tennis variants — they were 72% of momentum volume for −$6.20 net. Held positions still exit normally.
- **STRATEGY_BUDGETS (Apr 16):** `vig_stack` 60%, `live_momentum` 20%, `arbs` 20% (fractions of equity). Prevents any one strategy from starving the others.
- **Disabled (data-driven kills):** weather single-market (17% WR), series_game_edge (26% WR), all crypto (`CRYPTO_ENABLED=False`), economic indicators, parlay edge. See `config.py:ACTIVE_STRATEGIES` for the current truth.
- **Apr 20 redemption plan: complete (Sessions 1–5).** Settlement-pipeline rebuild, active-strategy retuning, ESPN fetch restoration, scheduler hardening + drift warnings, state hygiene (live_ticks rotation + clv filter + lock heartbeat).
- **Apr 24–25 closed-loop data collection: complete (Sessions 6–11).** Per-decision audit log, counterfactual CLV records, stratified sampling across (gate, opp_type), live-momentum edge proxy + 30s heartbeat lock-touch, per-position MFE/MAE, gate-context enrichment, fair-value calibration loop. The bot now self-instruments — every accept and every reject carries a gate fingerprint and downstream outcome attribution. Calibration data needs ~7 days to mature before reports become actionable. See [Recent Improvements](#recent-improvements-apr-2025) below.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [How It Works End-to-End](#how-it-works-end-to-end)
3. [Strategy Types](#strategy-types)
4. [Edge Quality & Safety Controls](#edge-quality--safety-controls)
5. [Position Sizing](#position-sizing)
6. [Execution Pipeline](#execution-pipeline)
7. [Paper Trading Sandbox](#paper-trading-sandbox)
8. [Closing-Line Value Tracking](#closing-line-value-tracking)
9. [Telegram Interface](#telegram-interface)
10. [Configuration Reference](#configuration-reference)
11. [Architecture & File Map](#architecture--file-map)
12. [Running the Bot](#running-the-bot)
13. [Testing](#testing)
14. [Dashboard UI](#dashboard-ui)
15. [Recent Improvements (Apr 20–25)](#recent-improvements-apr-2025)

---

## What It Does

Glint watches Kalshi prediction markets 24/7 and looks for contracts whose market-implied probability is meaningfully wrong relative to an independent model. When it finds one — with edge above a minimum threshold, sizing above a minimum dollar amount, and no position conflicts — it alerts via Telegram and either executes automatically (paper mode) or sends a GO prompt for manual approval.

It covers (see [Strategy Types](#strategy-types) for per-strategy active/disabled status):

- **Vig stacking** (`vig_stack_series`, `vig_stack_futures`) — **ACTIVE.** Structural overprice in Kalshi contract ladders. No external data needed. Net negative on paper before Filter F; the whitelist + NO ≥ 0.93 gate (Apr 20) is the repair.
- **Live game momentum** (`live_momentum`) — **ACTIVE via the live-watcher subsystem.** Dip-buy the leader on 1v1 live matches; take-profit + trailing stop exits. Paper-positive (+$12.30 on 39 settled). Tennis variants disabled Apr 20.
- **Sports arbs** (`sports_monotonicity_arb`, `sports_consistency_arb`) — **ACTIVE but no fills yet.** Riskless arbs on threshold-ladder monotonicity and championship-vs-series consistency.
- **Weather single-market**, **sports series edge**, **parlay edge**, **crypto price edge**, **economic indicators** — **DISABLED** (data-driven kills from the Apr 14 audit; see the table in Strategy Types).

Everything runs in **paper mode by default** — full edge detection, sizing, and trade lifecycle management with zero real capital at risk until you flip one line in `config.py`.

---

## How It Works End-to-End

### The Main Loop

`bot/main.py` runs three concurrent `asyncio` tasks (one disabled):

1. **`_main_loop()`** — the primary scan cycle. Runs immediately on startup, then on an adaptive schedule:
   - 2-minute cadence when games are live
   - 10-minute cadence in pregame windows
   - 30-minute cadence when nothing is live

   Each cycle calls `scan_cycle()` in `bot/scanner.py`, which fans out to all strategy scanners in parallel (ThreadPoolExecutor), deduplicates results across strategies, ranks by edge, and returns a prioritized opportunity list. The main loop then:
   - Filters to `ACTIVE_STRATEGIES` only
   - Re-runs Kelly sizing with current live balance
   - Runs `verify_contract_direction()` to confirm the title semantics match the intended trade side
   - Checks position limits and cooldowns
   - Executes automatically in paper mode, or sends a GO prompt in live mode
   - Handles Telegram callbacks (GO / SELL / status commands)
   - Runs fill reconciliation, trailing stop checks, and CLV settlement scans as background steps

2. **`_live_scan_loop()`** — independent loop running every 60s. Discovers live 1v1 matches on Kalshi and auto-spawns per-match `LiveGameWatcher` tasks that poll every 10s. This is where `live_momentum` runs.

3. **`_crypto_scan_loop()`** — disabled. Kept wired up behind `CRYPTO_ENABLED = False`; the crypto log-normal model was killed in the Apr 14 audit.

### Single Scan Cycle

```
scan_cycle()
  ├── prefetch: Kalshi parlay markets (400+ contracts, cached)
  ├── scanner_weather.py   → [DISABLED] still imported but returns [] for single-market edge
  ├── scanner_sports.py    → [DISABLED] series_game_edge killed in Apr 14 audit (26% WR)
  ├── kalshi_series.py     → [ACTIVE] vig_stack_series + vig_stack_futures (crypto ladder also killed)
  ├── scanner_sports_arb.py → [ACTIVE] monotonicity + consistency riskless arbs
  ├── econ_scanner.py      → [DISABLED] CPI nowcast retired
  └── filter to ACTIVE_STRATEGIES + deduplicate + rank by edge → return top N
```
Results are filtered to `ACTIVE_STRATEGIES` in `main.py` before execution. Disabled scanners are kept plumbed so re-enabling is a one-line config change, not a code change.

Each scanner returns a list of opportunity dicts with a common schema:
```
{
  ticker, title, side, edge, relative_edge, fair_value,
  kalshi_price, confidence, opp_type, math_chain, ...
}
```

---

## Strategy Types

> **Active vs disabled:** Only the strategies in `ACTIVE_STRATEGIES` actually place trades. As of Apr 23 that's `vig_stack_series`, `vig_stack_futures`, `sports_monotonicity_arb`, `sports_consistency_arb`, plus `live_momentum` via the live-watcher subsystem. Weather single-market, series_game_edge, parlay edge, crypto, and econ are all **disabled** — kept for reference but not called from the main scan cycle. Each section below flags its status. The honest current-money-maker (in aggregate) is `live_momentum`; `vig_stack_series` is net-negative on paper and is being rehabilitated via Filter F (stable-family whitelist + NO ≥ 0.93 on volatile families, raised from 0.90 on Apr 20).

### 1. Weather (`weather`) — DISABLED

**Signal:** NWS grid-point forecast minus a documented 1.5°F warm bias, compared against Kalshi's implied temperature distribution.

**Mechanics:**
- Fetches hourly/daily forecast from `api.weather.gov` for 18 US cities
- Maps city names to Kalshi series tickers (`KXHIGHNY`, `KXHIGHMIA`, etc.)
- Applies `NWS_BIAS_CORRECTION = 1.5°F` — NWS historically runs warm
- Computes probability using a normal distribution with dynamically estimated σ (derived from contract spacing in the Kalshi ladder)
- Converts that probability to a fair YES price; compares to Kalshi ask

**Filters:**
- Skips markets closing within 8 hours (`WEATHER_MIN_HOURS_TO_CLOSE = 8`) — same-day markets price off real-time temperature observations, not forecasts
- Applies the 25% absolute edge cap shared across all strategies
- Only scans next-day contracts where NWS forecast data is most reliable

**Coverage:** NYC (Central Park), Miami (MIA airport), Chicago (Midway), Denver, Austin, Boston, DC, SF, LA, Seattle, Phoenix, Dallas, Atlanta, Philadelphia, Las Vegas, Portland, Minneapolis, Nashville

---

### 2. Series Game Edge (`series_game_edge`, `ipl_game_edge`) — DISABLED (26% WR)

**Signal:** Sportsbook consensus moneyline vs Kalshi win/loss market for the same game.

**Mechanics:**
- Fetches moneylines from a priority cascade of four sources: DraftKings → Bovada → FanDuel → TheRundown (Odds API backup)
- Normalizes each team name to a canonical form and fuzzy-matches to Kalshi market titles
- Converts American odds to implied probability, removes the vig to get fair probabilities
- Compares against Kalshi bid/ask mid price
- Runs forward/backward self-check on every probability calculation

**Sports:** NBA, MLB, NHL, NCAAB, IPL

**Odds source cascade:** DraftKings is preferred but blocked on some residential IPs (Akamai WAF). The bot auto-disables DK for the session on a 403 and promotes Bovada. If Bovada fails, FanDuel, then ESPN, then TheRundown.

---

### 3. Parlay Edge (`parlay_yes`, `parlay_no`) — DISABLED

**Signal:** Independent-leg probability product vs Kalshi parlay contract price.

**Mechanics:**
- Parses Kalshi parlay market titles ("yes Boston wins, yes LA wins") into individual legs using `agent/parlay.py`
- Prices each leg from sportsbook moneylines, then multiplies assuming independence
- Applies a correlation discount (parlays are not perfectly independent — favorites tend to correlate)
- Compares the resulting fair value to the Kalshi YES ask or NO bid

---

### 4. Vig Stack (`vig_stack_series`, `vig_stack_futures`) — ACTIVE

**Signal:** Pure structural arbitrage — Kalshi contract ladders routinely over-price the NO side across adjacent strikes.

**Mechanics:**
- For any series with multiple mutually-exclusive thresholds (e.g., "NYC high < 60°F", "NYC high 60-65°F", "NYC high > 65°F"), the sum of YES probabilities across bins must equal 1.0
- When the ladder mis-prices and the implied sum exceeds 1.0, the cheapest NO contracts carry positive expected value with no external model needed
- No weather data, no sportsbook odds, no external API required — this edge is purely computational

**Filter F (Apr 18 → Apr 20):** The structural math is right but the *ladders* differ in quality. Stable ladders (Miami highs, Austin highs, S&P INX) sit on tight distributions where the NO edge converts to wins. Volatile ladders (high-variance weather cities, fast-moving indices) blow out in the tails and turn +EV math into −$100 of realized losses on paper. Filter F whitelists stable families via `VIG_STACK_STABLE_FAMILIES` and requires NO ≥ **0.93** (`VIG_STACK_WEATHER_MIN_PRICE`) on everything else. Apr 20 raised this from 0.90 to 0.93 after bucket analysis showed only [92-96¢) is breakeven on volatile families (`<92¢` was −$110.79 / 42 trades; the new 0.93 floor sits 1¢ above the bottom of the breakeven band).

**Paper performance (Apr 20 ground truth):** 54 settled, **−$110.62**, 29W/25L (54% WR). Filter F is expected to drift this positive on new entries; the historical loss pool doesn't retroactively fix. By family: volatile (`KXHIGHDEN/NY/CHI`) = 36 trades / −$126.88 / 69% early-cut; whitelist (`KXHIGHMIA/AUS/INX`) = 18 trades / +$16.26.

**This is the most mechanical strategy in the bot** — it doesn't predict outcomes, it exploits pricing inconsistencies in the market structure itself.

---

### 5. Crypto Price Edge (`btc_price_edge`, `eth_price_edge`, `sol_price_edge`, `xrp_price_edge`, `doge_price_edge`) — DISABLED (`CRYPTO_ENABLED=False`)

**Signal:** Log-normal price model vs Kalshi intraday/daily price threshold contracts.

**Mechanics:**
- Fetches current spot price and 30-day realized volatility from CoinGecko (30-minute cache TTL)
- Models next price as log-normal: `ln(S_T/S_0) ~ N(0, σ√T)`
- Computes `P(price > threshold)` using the normal CDF
- Compares to Kalshi YES ask for above-threshold contracts

**Independent loop:** Runs on its own 5-minute cadence (`_crypto_scan_loop`) to avoid interfering with sports/weather timing and to prevent doubled CoinGecko requests.

**Timeframes:** 5-hour and 10-hour intraday contracts, plus daily close contracts.

---

### 6. Economic Edge (`econ_cpi_edge`) — DISABLED

**Signal:** CPI nowcast model vs Kalshi inflation/economic indicator markets.

**Mechanics:**
- Pulls 100 active Kalshi economic markets
- Applies an econometric nowcast model for CPI (currently seeded at 2.43% for the current cycle)
- Computes probability that the realized number lands above/below each Kalshi threshold
- Surface to Telegram as opportunities with the same edge/sizing pipeline as other strategies

---

### 7. Live Game Momentum (`live_momentum`) — ACTIVE (via watcher subsystem)

**Signal:** Buy dips on the clear leader of a live 1v1 or head-to-head match (UFC, NBA, NHL) and ride the trailing stop / take-profit. Tennis variants disabled Apr 20.

**Mechanics:**
- `_live_scan_loop()` in `bot/main.py` polls every 60s, discovers live matches on Kalshi, auto-spawns a `LiveGameWatcher` task for each match with a clear leader
- The watcher polls Kalshi every 10s (`LIVE_POLL_INTERVAL`), tracks price history in a deque, recomputes a `GameContext` (momentum, win probability, lead trend, dip quality score) every tick. ESPN scoreboard fetch supplies `wp` / `wp_edge` / score / period — restored Apr 23 (Session 3) after silently failing for ~10 days on a missing UA header + cert validation
- Buys when the leader dips 4–8¢ from its recent high AND the dip quality score passes sport-specific thresholds
- **Conviction entry:** if there's no dip but game state screams value (wp_edge > 8%, positive momentum, 68–82¢ entry, ≥ Q3 completion), buys a 70%-sized position. NBA/NHL only; MLB and tennis excluded from conviction
- Exits: take-profit (12¢), trailing stop (6¢ from peak), stop-loss (10¢), near-settle lock (≥93¢), hard-cap ($5 max loss)
- Per-sport tuning in `SPORT_PROFILES` (`config.py:150-260`)
- **Tennis disabled (Apr 20):** `MOMENTUM_DISABLED_SPORTS = {atp, atp_challenger, wta, wta_challenger}` blocks new entries via a `can_enter` gate in `_tick_momentum`. Held tennis positions still exit normally — the gate only blocks entries

**Paper performance (Apr 20 ground truth):** 39 settled, **+$12.30**, 24W/15L (62% WR). NBA + NHL alone = +$19.60 on 10 trades; tennis was the drag (72% of volume for −$6.20 net). Apr 16 `STRATEGY_BUDGETS` (20% equity allocation) stopped vig_stack from starving live_momentum's pool.

---

## Edge Quality & Safety Controls

Every opportunity passes through a gauntlet before a trade is placed.

### 1. Minimum Relative Edge
```python
MIN_RELATIVE_EDGE = 0.15   # 15% — skip anything weaker
```
Relative edge = `(fair_value - market_price) / market_price`. This filters out small mispricings that don't justify execution cost and slippage.

### 2. Maximum Absolute Edge Cap (25%)
```python
MAX_CRYPTO_EDGE = 0.25
```
Applied in `_scan_crypto_series()` and `scanner_weather.py`. Any edge above 25¢ absolute is treated as stale pricing, near-expiry noise, or broken liquidity — not a real opportunity. The math is correct; the market is broken. Implemented after the bot was entering XRP contracts at 5¢ when XRP spot was 41% above the threshold and fair value was ~99.8%.

### 3. Direction Verification
```python
verify_contract_direction(ticker, title, side, fair_value)
```
Run as a mandatory pre-trade check in the executor. Parses the contract title to confirm the semantic direction matches the intended trade side. Uses a bank of regex patterns for above/below/between temperature syntax, and separate parsers for sports and crypto contracts. If the direction check fails, the trade is hard-blocked with an error log.

### 4. Price Movement Kill Switch
```python
MAX_PRICE_MOVE_CENTS = 3
```
Before executing a GO, the executor re-fetches the current Kalshi price. If the market has moved more than 3 cents since the opportunity was identified, the trade is aborted. This prevents chasing a price that was already consumed.

### 5. Position Deduplication
Re-entry on a currently-held ticker is blocked regardless of strategy type. If `positions.json` already has an open position on `KXHIGHDEN-26APR06-T63`, that ticker is skipped on all future scans until the position closes.

### 6. 4-Hour Cooldown After Exit
After any position exits (win, loss, or early cut), the same ticker is blocked for 4 hours. Implemented after observing `KXHIGHDEN-26APR06-T63` being entered 5 times in a single day — the position kept hitting the cut-loss threshold, getting exited, then being re-detected as an edge on the next scan.

```python
_COOLDOWN = timedelta(hours=4)
```

### 7. Exposure Limits

```python
MAX_POSITION_PERCENT = 0.20   # No single trade > 20% of balance
MAX_TOTAL_EXPOSURE   = 1.00   # Global cap — 100% of equity (balance + open exposure, Apr 16)
STRATEGY_BUDGETS = {
    "vig_stack":     0.60,    # 60% of equity
    "live_momentum": 0.20,    # 20% of equity
    "arbs":          0.20,    # 20% of equity
}
```

- `MAX_TOTAL_EXPOSURE` is enforced against `balance + total_exposure` (equity), not just cash.
- `STRATEGY_BUDGETS` caps each strategy's open exposure independently, so a heavy vig_stack position can't starve live_momentum. Rejections surface as `STRATEGY_BUDGET: vig_stack has $X of $Y budget` in the logs.
- Both live together: a trade must pass the global cap **and** the per-strategy budget.

### 8. Math Self-Check
`math_engine.py` runs every probability and edge calculation forward and backward:
- Probability complement check: `p + (1-p) == 1.0` within 1e-6
- Edge check: `fair_value - market_price == edge` and `market_price + edge == fair_value`

If any self-check fails, the trade is blocked and the failure logged with the full math chain for debugging.

---

## Position Sizing

Sizing lives in `bot/sizing.py` and uses fractional Kelly criterion.

### The Formula

```
full_kelly  = (b × p - q) / b
            where b = (1 / price_dollars) - 1   # net odds on a $1 payout contract
                  p = fair probability
                  q = 1 - p

fractional  = full_kelly × KELLY_FRACTION        # 25% of full Kelly
capped      = min(fractional, MAX_BET_FRACTION)  # hard cap at 5% of balance
risk_dollars = balance × capped
risk_dollars = max(risk_dollars, $1.00)          # floor
risk_dollars = min(risk_dollars, min(balance × 5%, $200))  # dynamic ceiling
```

### Why 25% Fractional Kelly
Full Kelly maximizes long-run log wealth but has brutal drawdowns when probability estimates are wrong. At 25% fractional Kelly, you sacrifice roughly half the growth rate of full Kelly in exchange for drawdowns that are ~6× smaller. For a bot operating on model-derived edge estimates, this is appropriate.

### Uncertainty Discount
Kelly assumes you know the true probability. You don't. The `uncertainty_discount` parameter (default 0.85 for model-derived edges) scales the input probability down before computing Kelly fraction, giving the model credit for being wrong ~15% of the time. High-confidence scans (confidence ≥ 0.9) can use a smaller discount.

### Resulting Behavior
A 15% relative edge at a 20¢ contract on a $500 paper balance produces roughly 14 contracts (~$2.80 total cost). A 22% edge at 7¢ on the same balance produces roughly 14 contracts (~$0.98), hitting the minimum floor and sizing up to the floor minimum.

---

## Execution Pipeline

The execution path for a single trade (in `bot/executor.py`):

```
execute_trade(opportunity)
  1. verify_contract_direction()     ← hard-block if title semantics don't match
  2. kelly_size()                    ← compute contracts and total cost
  3. _check_balance()                ← paper: derived from paper_trades.json; live: API
  4. _check_position_limits()        ← dedup + 4h cooldown + MAX_POSITION_PERCENT 20%
                                       + MAX_TOTAL_EXPOSURE 100% (vs equity, Apr 16)
                                       + STRATEGY_BUDGETS (vig_stack 60% / live_momentum 20% / arbs 20%)
  5. re-fetch Kalshi price           ← abort if moved > 3 cents
  6. place_order() or paper record   ← live: Kalshi API; paper: write to paper_trades.json
  7. record_clv_entry()              ← log entry price and fair value for CLV tracking
  8. append to positions.json        ← position tracker picks this up for P&L monitoring
```

**Live exit path:**
```
exit_position(ticker, reason)
  1. Look up position in positions.json
  2. In PAPER_MODE: simulate exit at current yes_bid; update paper_trades.json
  3. In live mode: place market sell order via Kalshi API; cancel any resting orders
  4. Mark position exited_at timestamp (starts cooldown clock)
  5. Send Telegram notification with realized P&L
```

---

## Paper Trading Sandbox

`PAPER_MODE = True` by default. The paper sandbox is a full-fidelity simulation:

- **Entry** — Written to `bot/state/paper_trades.json` with entry price, contracts, cost, fair value, and edge at trade time
- **Balance** — Computed from scratch on every balance check by walking `paper_trades.json`. Starting balance is $500. Open positions subtract cost. Won positions credit $1/contract. Early exits credit the simulated exit price. This makes the balance tracking self-contained and independent of the live Kalshi balance.
- **Exit** — Simulated at current `yes_bid` (what you'd actually get if you sold into the market). Realistic — not mid or ask.
- **Resolution** — When `resolve_trades()` detects a market has settled, it updates both `positions.json` and `paper_trades.json` with the actual result, payout, and realized P&L.
- **CLV** — Recorded at entry, updated at settlement. This is the primary signal for whether strategies are worth going live with.

**When to go live:** Only after ≥50 settled paper trades per strategy with average CLV > 0 and a positive-CLV rate above 55%. Flip `PAPER_MODE = False` in `config.py`.

---

## Closing-Line Value Tracking

CLV is the most important metric in this system. It measures whether the market moved in your favor after you entered — independent of whether you actually won. A strategy with positive average CLV has real edge. A strategy with negative CLV is getting lucky on wins or just picking off illiquid contracts.

**Calculation (in `bot/clv.py`):**

```
YES trade:  CLV = closing_yes_price - entry_yes_price
NO trade:   CLV = (100 - entry_yes_price) - closing_yes_price
            (positive CLV = market moved toward NO = good for our position)
```

Closing price is the settlement value (100 for YES result, 0 for NO result), or the mid price if the market is still open (for interim tracking).

**Report format (via `/clv` in Telegram):**
```
📈 CLV REPORT (Closing-Line Value)

Overall (38 trades): ✅ avg CLV = +4.2¢ (+18.3%) | beat line: 63%

By strategy:
  ✅ weather (12): +3.1¢ avg | 67% beat rate
  ✅ vig_stack_series (18): +6.8¢ avg | 72% beat rate
  ❌ series_game_edge (8): -1.2¢ avg | 38% beat rate
```

A strategy with a beat rate below 50% is a red flag regardless of win/loss record.

---

## Telegram Interface

The bot communicates exclusively via Telegram. No web UI required for operation.

### Automatic Alerts

| Event | Message |
|-------|---------|
| New opportunity found | Formatted opportunity card with edge %, price, fair value, strategy type |
| Trade executed (paper) | Confirmation with contracts, cost, entry price |
| Take profit trigger | Position up ≥50% — prompt to exit |
| Cut loss trigger | Position down ≥30% — auto-exits in paper mode |
| Market resolved | Result + realized P&L + CLV |
| Morning briefing (8am ET) | Weather scan results + open positions. Catches up if missed (Apr 23 Session 4) |
| Nightly summary (midnight ET) | Daily P&L, trade count, win rate. Persists `total_pnl` to `bot_state.json` |
| Startup drift warning | Logged if `last_morning_briefing` or `last_nightly_summary` is >2 days stale (Apr 23 Session 4) |

### Commands

Commands are case-insensitive and not slash-prefixed. Sent as plain Telegram messages.

**Trading**
| Command | Response |
|---------|----------|
| `GO [n]` | Execute pending opportunity #n (default 1). Also fired by inline GO button |
| `SKIP [n]` | Remove opportunity #n from queue |
| `LIST` / `PENDING` | Show the pending queue |
| `DETAIL [n]` | Full breakdown of pending opportunity #n |
| `SCAN` | Force a scan cycle now |
| `EDGES` | Show current top 3 edges found |

**Position management**
| Command | Response |
|---------|----------|
| `LIVE` / `POSITIONS` | Open positions with unrealized P&L |
| `SELL <ticker>` | Immediate exit |
| `EXITALL` | Exit all open positions |
| `TRAIL <ticker> <pct>` | Set a trailing stop |

**Live game watching**
| Command | Response |
|---------|----------|
| `WATCH <team>` | Start a `LiveGameWatcher` for that query (10s polling) |
| `UNWATCH` | Stop all active watchers |
| `RECAP [date]` | Human-readable journal recap from `live_journal.json` |
| `ANALYZE [date]` | Tick-level dip analysis: what dip size led to profitable exits |

**Status & stats**
| Command | Response |
|---------|----------|
| `STATUS` | Balance, today P&L, total P&L, win rate, open positions, streak |
| `BALANCE` | Raw Kalshi balance check |
| `STATS` | Paper stats with strategy breakdown |
| `HISTORY [n]` | Last n resolved trades (reads `trade_history.json`) |
| `WINRATE` | Overall + per-strategy win rate + ROI |
| `ROI` | Per-strategy ROI table |
| `CLV` | Closing-line value report (active strategies only — Apr 23 Session 5 filter) |
| `MODE` | Current PAPER/LIVE mode + active strategies |

**System**
| Command | Response |
|---------|----------|
| `LOGS` | Tail last 20 lines of `bot/logs/bot.log` |
| `RESTART` | `kill -9 $pid` — watchdog (`run_bot.sh` or launchd) brings it back |
| `STOP` | Unload launchd + kill |

### Pending Queue

Opportunities are written to `bot/state/pending.json` (max 20 entries) with an expiry timestamp set to 2 hours after the market closes. Stale entries are pruned on every scan. This allows GO commands to reference opportunities that were identified several minutes ago without re-scanning.

---

## Configuration Reference

All tunables live in `bot/config.py`. No scattered constants anywhere else.

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `PAPER_MODE` | `True` | Simulate all trades; flip to `False` for live |
| `PAPER_STARTING_BALANCE` | `$500` | Simulated starting balance |
| `MIN_RELATIVE_EDGE` | `0.15` | Skip opportunities below 15% relative edge |
| `KELLY_FRACTION` | `0.25` | Use 25% of full Kelly for sizing |
| `MAX_BET_FRACTION` | `0.05` | Hard cap at 5% of balance per trade |
| `MIN_BET_DOLLARS` | `$1.00` | Don't execute below this cost |
| `MAX_POSITION_PERCENT` | `0.20` | No single position > 20% of balance |
| `MAX_TOTAL_EXPOSURE` | `1.00` | Global cap — up to 100% of equity deployed (Apr 16) |
| `STRATEGY_BUDGETS` | `{vig_stack: 0.60, live_momentum: 0.20, arbs: 0.20}` | Per-strategy exposure caps vs equity (Apr 16) |
| `VIG_STACK_STABLE_FAMILIES` | `{KXHIGHMIA, KXHIGHAUS, KXINX}` | Filter F whitelist — only these vig_stack families trade freely (Apr 18) |
| `VIG_STACK_WEATHER_MIN_PRICE` | `0.93` | Filter F — volatile vig_stack families require NO ≥ 0.93 (raised from 0.90 Apr 20) |
| `MOMENTUM_LEADER_MIN` | `0.70` | Live-momentum entry floor; below this, leader probability isn't strong enough |
| `MOMENTUM_DISABLED_SPORTS` | `{atp, atp_challenger, wta, wta_challenger}` | Tennis variants blocked from new live-momentum entries (Apr 20) |
| `CUT_LOSS_THRESHOLD` | `-0.30` | Auto-cut at -30% unrealized P&L |
| `TAKE_PROFIT_THRESHOLD` | `0.50` | Alert at +50% unrealized P&L |
| `MAX_PRICE_MOVE_CENTS` | `3` | Abort GO if price moved >3¢ since alert |
| `NWS_BIAS_CORRECTION` | `1.5°F` | NWS documented warm bias correction |
| `WEATHER_MIN_HOURS_TO_CLOSE` | `8` | Skip same-day weather markets |
| `SCAN_INTERVAL_LIVE` | `120s` | Scan every 2 min when games are live |
| `SCAN_INTERVAL_PREGAME` | `600s` | Scan every 10 min in pregame window |
| `SCAN_INTERVAL_IDLE` | `1800s` | Scan every 30 min when nothing is live |
| `CRYPTO_SCAN_INTERVAL` | `300s` | Crypto-only scan every 5 min |
| `CRYPTO_CACHE_TTL` | `60s` | CoinGecko price cache TTL |
| `PENDING_MAX` | `20` | Max queued opportunities |
| `PENDING_GO_WINDOW_HOURS` | `2` | Opportunity expires 2h after market close |
| `ACTIVE_STRATEGIES` | (list) | Strategy types that trigger trades |

**Credentials** are loaded from `config/` JSON files at startup — never hardcoded:
- `config/kalshi.json` — `api_key_id`, `private_key_path`, `environment`
- `config/telegram.json` — `bot_token`, `chat_id`
- `config/sports_data.json` — Odds API key
- `config/therundown.json` — TheRundown API key (free tier, 20K/day)

---

## Architecture & File Map

```
hustle-agent/
├── bot/
│   ├── main.py              # Entrypoint — asyncio event loop, scan cycle, Telegram
│   ├── config.py            # All constants, thresholds, credentials loader
│   ├── scanner.py           # Orchestrator — fans out to all scanners, deduplicates
│   │
│   ├── scanner_weather.py   # NWS forecast → normal distribution → Kalshi edge
│   ├── scanner_sports.py    # Parlay/moneyline edge (NBA, MLB, NHL, NCAAB)
│   ├── kalshi_series.py     # Series game edges + vig stack + crypto ladders
│   ├── econ_scanner.py      # CPI nowcast vs Kalshi economic indicator markets
│   │
│   ├── scanner_sports_arb.py # Monotonicity + consistency riskless arb scanners (ACTIVE)
│   ├── math_engine.py       # All edge math with forward/backward self-checks
│   ├── sizing.py            # Fractional Kelly criterion with uncertainty discount
│   ├── executor.py          # Trade execution — safety pipeline, paper + live, STRATEGY_BUDGETS, gate-context-rich reject logging (Apr 24 Session 10)
│   ├── tracker.py           # P&L tracking, market resolution, CLV settlement (idempotent), per-position MFE/MAE ratchet (Apr 24 Session 9)
│   ├── clv.py               # Closing-Line Value per strategy + counterfactual records for stratified rejected opportunities (Apr 24 Sessions 6/8); MFE/MAE propagation at settlement (Apr 24 Session 9); paired prediction emission (Apr 25 Session 11)
│   ├── decisions.py         # Per-decision audit log — atomic JSONL append (Apr 24 Session 6)
│   ├── calibration.py       # Per-prediction fair-value log + ±60s settlement matching (Apr 25 Session 11)
│   │
│   ├── live_watcher.py      # Per-game 10s-tick watcher — live_momentum + live arb; wp_edge proxy + dampened decision logging (Apr 24 Session 7)
│   ├── game_context.py      # Live game intelligence: momentum, win_prob, DQS, instincts
│   ├── position_monitor.py  # Edge-recheck loop for open positions
│   │
│   ├── odds_scraper.py      # DraftKings / Bovada / FanDuel / ESPN / TheRundown cascade
│   ├── crypto.py            # CoinGecko spot price + 30d volatility cache (currently disabled)
│   ├── elo.py               # Team Elo ratings for sports edge adjustment
│   ├── injuries.py          # Injury report integration
│   ├── market_maker.py      # Passive limit-order market-making
│   ├── price_monitor.py     # Line movement detection (5pp threshold)
│   ├── outcome_tracker.py   # Trade outcome logging for calibration
│   ├── notifier.py          # Telegram formatting and HTTP sender
│   ├── patterns.py          # Historical win rate per strategy type (dynamic confidence)
│   ├── scheduler.py         # Cron events (morning briefing, nightly summary, balance reconcile, live_ticks/decisions/predictions rotation)
│   ├── daily_log.py         # Rolling daily performance log
│   ├── state_io.py          # Atomic JSON read/write (write-to-tmp-then-rename)
│   ├── logger.py            # RotatingFileHandler — bot/logs/bot.log, 10 MB × 5
│   └── tools/               # In-package diagnostic scripts (e.g. clv_by_strategy.py)
│
├── tools/                   # Top-level analysis tools (gitignored — local-only by convention)
│   ├── cohort_report.py     # Per-(opp_type, gate) reject-rate + distance-from-threshold histograms (Apr 24 Sessions 6/10)
│   ├── excursion_report.py  # Per-strategy median(MFE − exit) — flags exit-logic candidates (Apr 24 Session 9)
│   └── calibration_report.py # Per-strategy mean-bias / Brier score / per-bucket hit-rate (Apr 25 Session 11)
│
├── agent/
│   ├── kalshi_client.py     # Kalshi REST API (used by bot for all API calls)
│   ├── parlay.py            # Parlay title parser + multi-leg pricer
│   ├── player_stats.py      # Player prop probability estimator
│   ├── engine.py            # Original reasoning loop (Claude API)
│   └── ...
│
├── bot/state/               # Runtime state (gitignored)
│   ├── bot.lock             # PID lockfile; touched every 30s by dedicated _heartbeat_loop task (Apr 24 Session 7); per-scan touch retained as belt-and-suspenders
│   ├── bot_state.json       # Scan count, session stats, heartbeat, last_*_rotation flags, total_pnl
│   ├── positions.json       # All open + resolved positions; carries mfe_cents/mae_cents/mfe_at/mae_at/ticks_observed (Apr 24 Session 9)
│   ├── paper_trades.json    # Paper RESOLUTION log — balance reconstructed from this. Ground truth
│   ├── trade_history.json   # ORDER log — every execute_trade/execute_hedge appends here. Distinct from paper_trades
│   ├── pending.json         # Queued opportunities with expiry
│   ├── clv.json             # CLV records per trade. _load() filters to active strategies (Apr 23 Session 5). Also stores counterfactual records (status=counterfactual_open|counterfactual_settled, trade_id=CF-{scan_id}-{ticker}) for stratified rejected opportunities (Apr 24 Sessions 6/8). Settled records carry MFE/MAE (Apr 24 Session 9)
│   ├── decisions.jsonl      # Per-decision audit log (Apr 24 Session 6). Every scan-time accept and reject with {ts, ticker, opp_type, edge, gates, decision, reason, extra}. extra carries gate-specific distance-from-threshold context (Apr 24 Session 10). Daily rotation to archive/
│   ├── predictions.jsonl    # Per-prediction fair-value vs. actual log (Apr 25 Session 11). One row per opp evaluated (real trade or CF). Brier-scored by tools/calibration_report.py
│   ├── strategy_audit.json  # Per-strategy status + settlement_log (idempotent, Apr 18; rebuilt Apr 20 Session 1)
│   ├── live_journal.json    # Live-watcher events: scan_found, bet, exit, session_end
│   ├── live_ticks.jsonl     # Enriched per-tick log: price, wp, momentum, DQS, game_state, espn_scores
│   ├── archive/             # Daily gzipped JSONL archives — created by scheduler at midnight ET
│   │   ├── live_ticks-YYYY-MM-DD.jsonl.gz   # (Apr 23 Session 5)
│   │   ├── decisions-YYYY-MM-DD.jsonl.gz    # (Apr 24 Session 6)
│   │   └── predictions-YYYY-MM-DD.jsonl.gz  # (Apr 25 Session 11)
│   ├── patterns.json        # Historical win rate per strategy type (dynamic confidence)
│   ├── outcomes.db          # SQLite: alert → outcome log for calibration
│   ├── elo_ratings.json     # Sport ELO ratings
│   ├── daily_log.json       # Rolling daily performance snapshot
│   └── mm_positions.json    # Market-making pair tracker
│
├── bot/logs/
│   └── bot.log              # Rotating log — tail this for real-time status
│
├── tests/                   # pytest suite (see Testing section for current count)
│   ├── test_bot_executor.py    # Execution pipeline, position limits, cooldowns, STRATEGY_BUDGETS
│   ├── test_bot_scanners.py    # Scanner math, weather parsing, series matching
│   ├── test_bot_tracker.py     # P&L resolution, paper trade updates, CLV, settlement idempotency
│   ├── test_bot_improvements.py # Edge cap, cooldown, logging fixes
│   ├── test_live_watcher.py    # Watcher start/stop, tick processing, exit paths
│   ├── test_sport_instincts.py # Per-sport instinct filters (avoid_entry etc.)
│   ├── test_instincts.py       # Heuristic edge detection
│   ├── test_data_driven_fixes.py # Regression guard for Apr 14/16/18 tuning decisions
│   ├── test_kalshi.py          # Kalshi client mocking and response parsing
│   ├── test_parlay.py          # Parlay leg parsing and pricing math
│   ├── test_player_stats.py    # Player prop estimator
│   ├── test_to_8.py            # Batch improvements and regressions
│   └── test_agent.py           # Original agent subsystems (legacy, kept for kalshi_client coverage)
│
└── ui/                      # React dashboard (Vite + React + TypeScript + Tailwind)
    ├── server/index.ts      # Express API — reads state files, serves JSON
    └── src/                 # 10 pages covering positions, P&L, CLV, strategies
```

---

## Running the Bot

### Prerequisites

```bash
pip install -r requirements.txt
```

Populate `config/`:
```bash
config/
├── kalshi.json          # {"api_key_id": "...", "private_key_path": "kalshi-private-key.pem"}
├── kalshi-private-key.pem
├── telegram.json        # {"bot_token": "...", "chat_id": "..."}
├── sports_data.json     # {"api_key": "..."}    ← Odds API key (optional)
└── therundown.json      # {"api_key": "..."}    ← TheRundown (optional, free tier)
```

### Start

```bash
# Foreground (recommended for first run — watch the logs directly)
cd hustle-agent
python3 -m bot.main

# Background
nohup python3 -m bot.main > /tmp/glint.log 2>&1 &

# Follow the log
tail -f bot/logs/bot.log
```

The bot will:
1. Load any pending opportunities from disk
2. Reconcile `positions.json` against the live Kalshi API
3. Send a Telegram startup message
4. Begin the first scan cycle immediately
5. Start `_live_scan_loop()` on a 60s cadence (spawns `LiveGameWatcher` tasks for live matches)
6. (`_crypto_scan_loop` is wired up but gated off by `CRYPTO_ENABLED = False` — no work happens there until re-enabled)

### Stopping

Send `SIGTERM` or `SIGINT` (Ctrl-C). The bot catches both signals, releases `bot/state/bot.lock` via the shutdown handler, sends a Telegram shutdown message, and exits cleanly. If the process is mid-scan or wedged on I/O it can take a while to actually exit (the lock file is released early in the handler, so a missing lock + a still-alive PID is a known intermediate state) — escalate with `kill -9 <pid>` if it doesn't exit within ~30s.

The Telegram `STOP` command is the same path plus an `unload` against launchd; `RESTART` is `kill -9` and relies on `run_bot.sh` (or launchd) bringing the bot back.

---

## Testing

```bash
python3 -m pytest tests/ -q
# 616 tests collected across 20 test files (Apr 24–25: +109 tests across Sessions 6–11)
# Current state: ~9 known pre-existing failures (5 stale, 2 watchdog harness, 2 misc), rest pass or are skipped behind live-call guards
```

> **Known stale tests (Apr 18 + carried forward):** A handful of tests became outdated as the bot evolved and have not been refreshed yet:
> - `test_bot_executor.py::test_position_limit_fail_aborts` and `test_data_driven_fixes.py::test_ticker_exceeding_daily_loss_blocked` — both hit the reserve-guard message before reaching the position-limit check they're asserting on
> - `test_bot_improvements.py::test_watchdog_*` — heartbeat test harness drifted from current watchdog semantics (alert path is silenced at `main.py:313`)
> - `test_bot_scanners.py::test_eth_in_active_strategies` — asserts `eth_price_edge` is active, but crypto was disabled Apr 14
> - One stale Apr-18 pin on `WEATHER_MIN_PRICE` (now 0.93) and two `live_watcher._trailing_active` attribute drifts in session-summary tests
>
> These are documentation debt on the test layer, not bugs in the trading code. They will be repaired in a dedicated cleanup pass. New code shipped Apr 20–23 (settlement pipeline, scheduler hardening, live_ticks rotation, clv filter) is fully covered — see `tests/test_scheduler.py` for the 19 scheduler/rotation tests added in Sessions 4 and 5.

All tests mock external APIs — no real Kalshi calls, no CoinGecko, no sportsbook requests, no Telegram messages. The test suite covers:

- **Executor:** balance check (paper and live), position limit enforcement, 4-hour cooldown, price movement kill switch, direction verification, paper trade lifecycle, `STRATEGY_BUDGETS` (Apr 16), Filter F gate (Apr 18 → 0.93 Apr 20)
- **Scanners:** weather normal distribution math, NWS response parsing, city alias mapping, series game edge calculation, vig stack detection, crypto log-normal model
- **Tracker:** market resolution logic, P&L computation, paper_trades.json update on settlement, settlement-log idempotency (Apr 18), `exited_early` settlement pipeline + `record_resolution` (Apr 20 Session 1)
- **Scheduler (Apr 23 — `test_scheduler.py`, 19 tests):** morning briefing fire-at-8am-or-catch-up, nightly summary midnight + missed-day catch-up, balance reconcile at 21:00, `total_pnl` persistence, `live_ticks.jsonl` midnight rotation + collision-suffix + skip-if-too-small
- **Live watcher:** watcher start/stop, tick-level dip + DQS + variance_quality_gate (Tier 2.4), conviction-entry gating, exit paths (take-profit, trail, stop, near-settle), sport-instinct avoid_entry guards, `MOMENTUM_DISABLED_SPORTS` `can_enter` gate (Apr 20)
- **Sizing:** Kelly formula correctness, fractional cap, uncertainty discount, dollar floor/ceiling
- **CLV:** entry recording, settlement computation (YES and NO sides), report generation, active-strategy filter at `_load` (Apr 23), counterfactual schema + idempotency + stratified selection (Apr 24 Sessions 6/8), MFE/MAE propagation at settlement (Apr 24 Session 9)
- **Parlay:** title parsing for multi-leg contracts, edge calculation with correlation discount
- **Closed-loop instrumentation (Apr 24–25 — Sessions 6–11):** `test_decisions.py` (atomic JSONL append under contention, schema integrity, dampener), `test_tracker.py` (10 MFE/MAE cases — side-aware ratchet, lazy-init, monotonic, settlement propagation), `test_cohort_report.py` (distance-from-threshold histogram math), `test_calibration.py` (31 cases including Brier handcraft, ±60s settlement matching window, idempotency), `test_main.py` (heartbeat lock-touch task), `test_scheduler.py` extensions (decisions + predictions rotation)
- **Regression guards:** `test_bot_improvements.py` + `test_data_driven_fixes.py` lock in the specific fixes from the Apr 14/16/18/20 audits (edge cap, cooldown, UW-exit removal, SCORE-FLIP momentum gate, Filter F, tennis disable, etc.) so they cannot silently regress

---

## Dashboard UI

> **Status:** The React dashboard under `ui/` is **legacy and not actively maintained.** The bot is operated entirely via Telegram. The static `bot/dashboard.html` file is a lighter read-only view that works without a Node toolchain.

A React dashboard reads all state files and shows the bot's activity in real-time.

```bash
cd ui && npm install && npm run dev
```

Starts:
- Express API server on port `3001` (reads `../bot/state/` files)
- Vite dev server on port `5173`
- Polls every 5 seconds

| Page | What it shows |
|------|--------------|
| Command Center | Paper balance, open position count, scan stats, recent activity feed |
| Finances | Full paper trade ledger, balance over time, realized P&L |
| Strategies | Per-strategy win rate, ROI, and CLV stats |
| Positions | Open positions with current bid, unrealized P&L, entry detail |
| CLV | Closing-Line Value breakdown by strategy and timeframe |
| The Dream | GPU fund progress (original agent concept) |
| Journal | Agent diary entries |
| Chat | Message history |

---

## Design Principles

A few decisions that shaped the system:

**No prediction is better than a wrong prediction.** Every strategy either has an external reference model (NWS, sportsbook moneyline, CoinGecko + log-normal) or exploits a structural pricing property (vig stack). There's no pure sentiment or momentum trading.

**Paper mode is a first-class mode, not a toggle.** Balance accounting, exit simulation, and CLV tracking all work identically in paper mode. Going live is a one-line config change, not a system change.

**CLV gates live trading, not win rate.** Win rate is a lagging indicator and is heavily influenced by contract resolution luck. CLV tells you whether the market confirmed your edge in real-time. A strategy with 60% win rate and negative CLV is getting lucky. A strategy with 45% win rate and positive CLV is finding real edge.

**Math self-checks are not optional.** The bot runs forward/backward verification on every edge calculation. The cost is microseconds per trade. The benefit is catching sign errors, unit errors, and inversion bugs before they execute real orders.

---

## Recent Improvements (Apr 20–25)

Two arcs.

**Apr 20–23 — Redemption (Sessions 1–5).** The Apr 20 state audit surfaced 12 issues across real bugs, tuning opportunities, and dead weight. Bundled into 5 focused sessions, all shipped.

**Apr 24–25 — Closed-loop data collection (Sessions 6–11).** With the bot stable, the missing piece for retuning was outcome attribution: the trade log told us *what fired* but not *what almost fired and was killed by which gate, and what would have happened if we'd taken it anyway*. Sessions 6–11 instrument the bot end-to-end so gate calibration becomes a regression problem instead of folklore. All shipped; calibration data needs ~7 days to mature before reports become actionable.

### ☑ Session 1 — Settlement + pattern pipeline (Apr 20)
58 of 93 resolved paper trades (`exited_early`) were silently missing from `strategy_audit.settlement_log` because `executor._paper_record_exit` never called `_log_settlements_to_audit` or `patterns.record_resolution`. Fixed by extracting `tracker.log_settlement(trade)` per-trade helper, adding `patterns.record_resolution`, wiring both into `_paper_record_exit`, and rebuilding the audit via `tools/rebuild_strategy_audit.py`. Post-rebuild: paper / settlement_log / rollup all reconcile to 93 trades. Backup at `bot/state/strategy_audit.json.bak-20260421`.

### ☑ Session 2 — Active strategy retuning (Apr 20)
Two dollar leaks:
- **Vig_stack volatile branch**: KXHIGHDEN/NY/CHI = −$126.88 on 36 trades. Bucket analysis showed only [92-96¢) was breakeven on volatile families. → `VIG_STACK_WEATHER_MIN_PRICE` raised 0.90 → **0.93** (1¢ safety margin above the breakeven floor).
- **Live_momentum tennis**: 72% of momentum volume for −$6.20 net. → New `MOMENTUM_DISABLED_SPORTS = {atp, atp_challenger, wta, wta_challenger}`; `can_enter` gate in `live_watcher._tick_momentum` blocks new entries while preserving normal exits on held positions.
- Briefly raised `MOMENTUM_LEADER_MIN` 0.70 → 0.75 to skip the [75-80¢) dead zone, then reverted same day — MIN is a floor so 0.75 *admitted* the dead zone while surrendering the positive [70-75¢) bucket. Proper dead-zone exclusion in `is_leader` is TODO.

### ☑ Session 3 — Live-watcher ESPN restoration (Apr 23)
3000/3000 recent live ticks had `espn_scores: None`; `wp` defaulted to 0.5. Three silent failures stacked: missing `User-Agent` header (ESPN started 403'ing), default SSL context (intermittent cert validation), and a bare `except:` swallowing the exception. Fixed in `bot/live_watcher.py:_fetch_espn_score`: `User-Agent: GlintBot/1.0`, `_ESPN_SSL_CTX = ssl.create_default_context(cafile=certifi.where())`, structured error logging, one-shot success log per (ticker, sport). `ESPN_BASE` + `ESPN_SPORT_PATHS` hoisted into `bot/config.py`. Verification (last 500 ticks Apr 23): NHL 68/68 ✓, NBA live games OK, all sports `wp` 100% populated.

### ☑ Session 4 — Scheduler + bot_state revival (Apr 23)
`last_morning_briefing` was 11 days stale, `last_nightly_summary` 4 days stale, `total_pnl` was always 0. Root causes: scheduler hour-gate was `current_hour == HOUR` (narrow window the polling loop kept skipping); `_send_nightly_summary` computed `total_pnl` but never persisted it; latent write-ordering bug clobbered concurrent state writes; `crypto_trades_today` was a stale counter not zeroed on date rollover. Fixed in `bot/scheduler.py` (hour gate `>=` + same-day flag + missed-day catch-up clause; persist `total_pnl` to `bot_state.json`; reload state before stamping to fix write ordering) and `bot/main.py` (zero `crypto_trades_today` on rollover; new startup drift warning if scheduler timestamps are >2 days stale). 14 new tests in `test_scheduler.py`.

### ☑ Session 5 — State hygiene (Apr 23)
117MB of `bot/state/`, 108MB of which was `live_ticks.jsonl` growing unbounded; 53% of `clv.json` records were for disabled strategies; six confirmed-stale files; `bot.lock` mtime frozen since startup. Fixed:
- `bot/clv.py:_load()` now filters records to active strategies (`ACTIVE_STRATEGIES + live_momentum`). Single read site, so disabled-strategy noise gets dropped on the next save.
- `bot/scheduler.py` — new `_rotate_live_ticks(today_str)` + midnight-ET gate. Renames `live_ticks.jsonl` → `state/archive/live_ticks-YYYY-MM-DD.jsonl`, gzips, unlinks. Race-safe because `_log_tick` reopens the file every write. 5 new rotation tests.
- `bot/main.py` — one-line `LOCK_FILE.touch()` in the heartbeat block. `bot.lock` mtime is now a liveness signal (purely additive — no reader consumed it before).
- One-shot `tools/purge_clv_disabled.py` + `tools/clean_stale_state.py` to drain on-disk noise. Deleted: `odds_snapshots.json`, `price_cache.json`, `watchlist.json`, `paper_trades_archive.json`, two Apr-18 `.bak` leftovers. Kept: `strategy_audit.json.bak-20260421` (Session 1 backup).
- CLAUDE.md state-files table now distinguishes `trade_history.json` (order log) from `paper_trades.json` (paper resolution log).

**Result**: `bot/state/` from 117MB → 5.4MB after one-shot + rotation. Zero trading-logic changes; all five sessions are safety/observability/cleanup.

### ☑ Session 6 — Closed-loop data collection foundation (Apr 24)
The trade log answered "what fired"; nothing answered "what almost fired and what would have happened." Three new pieces:
- `bot/decisions.py` (new, ~120 lines) — `log_decision(ticker, opp_type, edge, gates, decision, reason, extra)` atomically appends to `bot/state/decisions.jsonl`. Single write site, threading lock, never raises.
- `bot/clv.py:record_counterfactual_skip` — top rejected opportunities per scan get a CLV record (`status=counterfactual_open`, `trade_id=CF-{scan_id}-{ticker}`). The existing settlement poller fills `closing_yes_price` on them naturally.
- Instrumented every scanner reject (`scanner.py` vig_stack gates) and the executor's 7 position-limit + 3 verify-edge gates with `log_decision` calls. Live-momentum gates use a dampener (only emit on `(decision, reason)` change) so a flat-market ticker doesn't spam 50k records/day.
- Daily rotation of `decisions.jsonl` to `archive/decisions-YYYY-MM-DD.jsonl.gz` mirrors the Session-5 live_ticks pattern.
- New `tools/cohort_report.py` (local-only) joins decisions to CLV CF records to compute "edge left on table" per gate.
- Follow-up the same day: filter CF entry-price < 3¢ (relative-edge math `(fair-price)/price` blows up at 1-2¢ entries, crowding out legitimate higher-quality rejects in top-K selection).

### ☑ Session 7 — Decision-log observability gaps (Apr 24)
First 24h of `decisions.jsonl` surfaced two gaps:
- **Live-momentum decisions logged `edge=null`** because `_tick_momentum` has no scalar edge concept. Wired `wp_edge` (already computed each tick for `live_ticks.jsonl`) into `_log_decision_dampened` at all 5 reject sites. Added `mom_ctx={wp, kalshi_price, dip_cents, dqs}` to `extra` so the cohort report can join on something useful.
- **`bot.lock` mtime advanced only at scan boundaries** (2-30 min), making healthy idle bots look wedged per Gotcha #6's 15-min stale-mtime rule. Added a dedicated `_heartbeat_loop` task on `GlintBot` that touches `LOCK_FILE` every 30s. Per-scan touch in `_main_loop` retained as belt-and-suspenders. Worst-case stale gap drops 30 min → ≤60s.

### ☑ Session 8 — Stratified CF sampling (Apr 24)
First 24h of CF data showed 29/29 records attributed to `non_stable_below_weather_floor` (real 4-20¢ edges) while the gates we most need to retune had **zero** outcome attribution: `vig_stack_series forecast_in_bucket` (143 rejects, 0 CFs), `vig_stack_futures edge_below_threshold` (130 rejects, 0 CFs), `vig_stack_series edge_below_threshold` (114 rejects, 0 CFs). The Session-6 "top-5 by global edge" rule starved low-edge-by-design gates. Replaced with two-stage stratified sampling in `bot/scanner.py:_stratified_cf_rejects`:
1. **Stratified core** — 1 highest-edge reject per `(opp_type, skip_reason)` group
2. **Budget fill** — highest-edge leftovers up to total_budget=10
3. **Dedup by ticker** (higher edge wins); hard cap 15/scan

Bonus fix: `forecast_in_bucket` rejects were logged to `decisions.jsonl` but never appended to `rejected_opps` because fair-value computation ran *after* the short-circuit. Hoisted the fair-value block above the forecast check so forecast-rejected contracts now enter the CF sample with a real edge.

Budget math: ≤480/day idle, ≤7200/day active. Well under the ≤900/day idle, ≤13k/day active envelope. **Part 2:** `run_bot.sh` hardcoded a Python 3.9 binary path but `bot/daily_log.py` uses PEP 604 union syntax requiring Python 3.10+; bumped to Python 3.14 framework path and re-enabled the user-domain launchd service.

### ☑ Session 9 — Per-position MFE/MAE tracking (Apr 24)
`clv.json` recorded entry, settlement, and final CLV but nothing about what the price did *between* entry and settlement. Two trades can have identical CLV but very different lived experiences — one drifted straight to close, the other spiked +30¢ then unwound. The first vindicates conviction sizing; the second is a missed-exit signal.
- `bot/tracker.py:update_positions` ratchets `mfe_cents`/`mae_cents`/`mfe_at`/`mae_at`/`ticks_observed` on every price observation. Side-aware via `current_bid` (yes_bid for YES, no_bid for NO). Lazy-init on first observation so pre-Session-9 open positions upgrade cleanly.
- `bot/clv.py:check_clv_settlements` builds `order_id → position` lookup and copies the five excursion fields into real-trade settlement records. Counterfactuals untouched.
- New `tools/excursion_report.py` (local) groups settled CLV by `opp_type` and flags `median(MFE − exit) > 5¢` as exit-logic candidates.
- 10 new tests covering init, side-aware ratchet, monotonicity, timestamp semantics, settlement propagation.

### ☑ Session 10 — Gate-context enrichment in `extra` (Apr 24)
`decisions.jsonl` recorded *which* gate fired but not *by how much*. A gate that rejects "just barely" 80% of the time is a tuning candidate; a gate that rejects "by a mile" 80% of the time is doing its job. Backfilled `extra` across every reject site with gate-relevant diagnostics:
- `forecast_in_bucket` → `forecast_temp`, `bucket_lo`, `bucket_hi`, `distance` (rounded cents)
- `edge_below_threshold` → `edge` (actual), `vig`, `time_to_settle_hr`, `min_edge` threshold
- `low_liquidity` → `volume`, `open_interest`, `min_volume`, `min_open_interest`
- Executor: refactored `_log_position_reject`/`_log_edge_reject` helpers to accept an `extra` kwarg, then enriched all 7 position-limit gates (position_cap, duplicate, same_game, cooldown, daily_loss, strategy_budget, total_exposure) plus edge-verify and self-check gates with their respective context.
- Updated `tools/cohort_report.py` to render distance-from-threshold histograms (replacing the binary reject-rate).
- Proper TDD: 8 commits, failing tests first, then implementation.

### ☑ Session 11 — Fair-value calibration loop (Apr 25)
Every edge calc is `(fair_value - market_price) / market_price` — the whole bot is one big bet on `fair_value` being right, and CLV alone can't catch a scanner that consistently overestimates fair value (CLV measures execution, not prediction).
- New `bot/calibration.py` (171 lines) mirrors the `decisions.py` pattern: `record_prediction()` appends to `bot/state/predictions.jsonl` on every CLV entry (real trade or CF). Atomic JSONL append, threading lock, never raises. Idempotent on `(scan_id, ticker)`. Skips rows where `predicted_fair_cents` is None/0 (live_momentum has no usable fair value — Session 7 cross-reference).
- `bot/clv.py:check_clv_settlements` calls `update_prediction_close()` to fill `closing_yes_price` on matching prediction rows via ticker + recorded_at ±60s window (handles the small lag between `record_clv_entry` and `record_prediction`).
- Daily rotation of `predictions.jsonl` to `archive/predictions-YYYY-MM-DD.jsonl.gz` mirrors the existing patterns.
- New `tools/calibration_report.py` (local) emits per-strategy mean-bias / Brier score / per-bucket hit-rate. After 7 days of settlements, `vig_stack_series` predicted bucket [80,90¢) resolving <70% YES is a flag for fair-value retuning.
- 31 new tests including atomic append under contention, idempotency, settlement matching window, missing-archive rotation, and Brier handcraft (5 records → 0.082 by hand).

**Result**: the bot is now self-instrumented end-to-end. Every accept and every reject carries a gate fingerprint with distance-from-threshold context. Every prediction (acted-on or counterfactual) is paired with its eventual closing price. Every position carries excursion data. Three local-only analysis tools (`cohort_report`, `excursion_report`, `calibration_report`) join the streams. Once 7 days of data accumulate, gate retuning becomes a regression problem instead of folklore.
