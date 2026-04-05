# Hustle Agent — Glint Trading Bot

An autonomous prediction-market trading system built on top of Kalshi's REST API. It runs a continuous scan→rank→size→execute loop across five independent strategy types, enforces multi-layer position safety, and manages a full paper-trading sandbox before any real money changes hands.

The original agentic reasoning layer (`agent/`) still exists and owns the Kalshi API client, but **Glint** (`bot/`) is the production system. This document covers the bot in depth.

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

---

## What It Does

Glint watches Kalshi prediction markets 24/7 and looks for contracts whose market-implied probability is meaningfully wrong relative to an independent model. When it finds one — with edge above a minimum threshold, sizing above a minimum dollar amount, and no position conflicts — it alerts via Telegram and either executes automatically (paper mode) or sends a GO prompt for manual approval.

It covers:
- **Weather** — NWS next-day high temperature forecasts vs Kalshi temp contracts
- **Sports series** — Sportsbook moneylines and parlay prices vs Kalshi game markets
- **Vig stacking** — Structural overprice in Kalshi contract ladders (no external data needed)
- **Crypto prices** — Log-normal price distribution model vs Kalshi daily/intraday price contracts
- **Economic indicators** — CPI nowcast model vs Kalshi macro markets

Everything runs in **paper mode by default** — full edge detection, sizing, and trade lifecycle management with zero real capital at risk until you flip one line in `config.py`.

---

## How It Works End-to-End

### The Main Loop

`bot/main.py` runs two concurrent `asyncio` tasks:

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

2. **`_crypto_scan_loop()`** — independent loop running every 5 minutes. Calls `scan_all_crypto_markets()` directly, bypassing the main scanner, so crypto scans don't interfere with sports/weather timing and don't double-count CoinGecko requests.

### Single Scan Cycle

```
scan_cycle()
  ├── prefetch: Kalshi parlay markets (400+ contracts, cached)
  ├── scanner_weather.py   → NWS forecast + edge calc (all 18 cities)
  ├── scanner_sports.py    → odds scraper + parlay math (NBA/MLB/NHL/NCAAB)
  ├── kalshi_series.py     → series game edges + vig stack + crypto ladders
  ├── econ_scanner.py      → CPI nowcast vs Kalshi econ markets
  └── deduplicate + rank by edge → return top N
```

Each scanner returns a list of opportunity dicts with a common schema:
```
{
  ticker, title, side, edge, relative_edge, fair_value,
  kalshi_price, confidence, opp_type, math_chain, ...
}
```

---

## Strategy Types

### 1. Weather (`weather`)

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

### 2. Series Game Edge (`series_game_edge`, `ipl_game_edge`)

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

### 3. Parlay Edge (`parlay_yes`, `parlay_no`)

**Signal:** Independent-leg probability product vs Kalshi parlay contract price.

**Mechanics:**
- Parses Kalshi parlay market titles ("yes Boston wins, yes LA wins") into individual legs using `agent/parlay.py`
- Prices each leg from sportsbook moneylines, then multiplies assuming independence
- Applies a correlation discount (parlays are not perfectly independent — favorites tend to correlate)
- Compares the resulting fair value to the Kalshi YES ask or NO bid

---

### 4. Vig Stack Series (`vig_stack_series`)

**Signal:** Pure structural arbitrage — Kalshi contract ladders routinely over-price the NO side across adjacent strikes.

**Mechanics:**
- For any series with multiple price thresholds (e.g., "BTC > $65K", "BTC > $66K", "BTC > $67K"), the sum of NO probabilities across exclusive bins must equal 1.0
- When the ladder mis-prices and the implied sum exceeds 1.0, the cheapest NO contracts carry positive expected value with no external model needed
- No weather data, no sportsbook odds, no external API required — this edge is purely computational

**This is the most mechanical strategy in the bot** — it doesn't predict outcomes, it exploits pricing inconsistencies in the market structure itself.

---

### 5. Crypto Price Edge (`btc_price_edge`, `eth_price_edge`, `sol_price_edge`, `xrp_price_edge`, `doge_price_edge`)

**Signal:** Log-normal price model vs Kalshi intraday/daily price threshold contracts.

**Mechanics:**
- Fetches current spot price and 30-day realized volatility from CoinGecko (30-minute cache TTL)
- Models next price as log-normal: `ln(S_T/S_0) ~ N(0, σ√T)`
- Computes `P(price > threshold)` using the normal CDF
- Compares to Kalshi YES ask for above-threshold contracts

**Independent loop:** Runs on its own 5-minute cadence (`_crypto_scan_loop`) to avoid interfering with sports/weather timing and to prevent doubled CoinGecko requests.

**Timeframes:** 5-hour and 10-hour intraday contracts, plus daily close contracts.

---

### 6. Economic Edge (`econ_cpi_edge`)

**Signal:** CPI nowcast model vs Kalshi inflation/economic indicator markets.

**Mechanics:**
- Pulls 100 active Kalshi economic markets
- Applies an econometric nowcast model for CPI (currently seeded at 2.43% for the current cycle)
- Computes probability that the realized number lands above/below each Kalshi threshold
- Surface to Telegram as opportunities with the same edge/sizing pipeline as other strategies

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
MAX_TOTAL_EXPOSURE   = 0.50   # No more than 50% of balance deployed simultaneously
```

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
  4. _check_position_limits()        ← dedup + 4h cooldown + 20% / 50% exposure
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
| Morning briefing (8am ET) | Weather scan results + open positions |
| Nightly summary (midnight ET) | Daily P&L, trade count, win rate |

### Commands

| Command | Response |
|---------|----------|
| `/status` | Open positions, paper balance, scan count, last scan time |
| `/opportunities` | Top-ranked current opportunities with GO buttons |
| `/positions` | All open positions with unrealized P&L |
| `/clv` | Closing-Line Value report by strategy |
| `/pnl` | Realized P&L breakdown, all-time and today |
| `/scan` | Trigger immediate scan cycle |
| `GO <n>` | Execute opportunity #n from the pending queue |
| `SELL <ticker>` | Manually exit a specific position |

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
| `MAX_TOTAL_EXPOSURE` | `0.50` | No more than 50% deployed at once |
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
│   ├── math_engine.py       # All edge math with forward/backward self-checks
│   ├── sizing.py            # Fractional Kelly criterion with uncertainty discount
│   ├── executor.py          # Trade execution — 8-step safety pipeline, paper + live
│   ├── tracker.py           # P&L tracking, market resolution, CLV settlement
│   ├── clv.py               # Closing-Line Value per strategy
│   │
│   ├── odds_scraper.py      # DraftKings / Bovada / FanDuel / ESPN / TheRundown cascade
│   ├── crypto.py            # CoinGecko spot price + 30d volatility cache
│   ├── elo.py               # Team Elo ratings for sports edge adjustment
│   ├── injuries.py          # Injury report integration
│   ├── market_maker.py      # Passive limit-order market-making
│   ├── price_monitor.py     # Line movement detection (5pp threshold)
│   ├── outcome_tracker.py   # Trade outcome logging for calibration
│   ├── notifier.py          # Telegram formatting and HTTP sender
│   ├── patterns.py          # Regex patterns for contract title parsing
│   ├── scheduler.py         # Adaptive scan frequency (live / pregame / idle)
│   ├── state_io.py          # Atomic JSON read/write (write-to-tmp-then-rename)
│   └── logger.py            # RotatingFileHandler — bot/logs/bot.log, 10 MB × 5
│
├── agent/
│   ├── kalshi_client.py     # Kalshi REST API (used by bot for all API calls)
│   ├── parlay.py            # Parlay title parser + multi-leg pricer
│   ├── player_stats.py      # Player prop probability estimator
│   ├── engine.py            # Original reasoning loop (Claude API)
│   └── ...
│
├── bot/state/               # Runtime state (gitignored)
│   ├── positions.json       # All open + resolved positions
│   ├── paper_trades.json    # Paper trade ledger (entry, exit, P&L, CLV)
│   ├── pending.json         # Queued opportunities with expiry
│   ├── clv.json             # CLV records per trade
│   ├── bot_state.json       # Scan count, session stats
│   ├── trade_history.json   # Resolved trade archive
│   └── mm_positions.json    # Market-making pair tracker
│
├── bot/logs/
│   └── bot.log              # Rotating log — tail this for real-time status
│
├── tests/                   # 404 tests across 10 test files
│   ├── test_bot_executor.py # Execution pipeline, position limits, cooldowns
│   ├── test_bot_scanners.py # Scanner math, weather parsing, series matching
│   ├── test_bot_tracker.py  # P&L resolution, paper trade updates, CLV
│   ├── test_bot_improvements.py # Edge cap, cooldown, logging fixes
│   ├── test_kalshi.py       # Kalshi client mocking and response parsing
│   ├── test_parlay.py       # Parlay leg parsing and pricing math
│   ├── test_player_stats.py # Player prop estimator
│   ├── test_instincts.py    # Heuristic edge detection
│   ├── test_to_8.py         # Batch improvements and regressions
│   └── test_agent.py        # Original agent subsystems
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
5. Start the crypto loop in parallel

### Stopping

Send `SIGTERM` or `SIGINT` (Ctrl-C). The bot catches both signals, sends a Telegram shutdown message, and exits cleanly.

---

## Testing

```bash
python3 -m pytest tests/ -q
# 404 passed, 1 warning in 6.4s
```

All tests mock external APIs — no real Kalshi calls, no CoinGecko, no sportsbook requests, no Telegram messages. The test suite covers:

- Executor: balance check (paper and live), position limit enforcement, 4-hour cooldown, price movement kill switch, direction verification, paper trade lifecycle
- Scanners: weather normal distribution math, NWS response parsing, city alias mapping, series game edge calculation, vig stack detection, crypto log-normal model
- Tracker: market resolution logic, P&L computation, paper_trades.json update on settlement
- Sizing: Kelly formula correctness, fractional cap, uncertainty discount, dollar floor/ceiling
- CLV: entry recording, settlement computation (YES and NO sides), report generation
- Parlay: title parsing for multi-leg contracts, edge calculation with correlation discount
- Regression: all `test_bot_improvements.py` tests guard the specific fixes that were made (edge cap, cooldown, logging) so they cannot silently regress

---

## Dashboard UI

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
