# HUSTLE AGENT — Project Guide for Claude Code

## Project Scope (Read First)

**Bot only.** Per user instruction in `~/.claude/projects/.../memory/project_scope.md`: the agent LLM reasoning loop in `agent/engine.py` is **excluded from all work**. Do not run, modify, extend, or debug the agent's LLM cycle. The `agent/` directory is kept around because it owns the **Kalshi REST client** (`agent/kalshi_client.py`), team-alias dicts (`agent/parlay.py`), and player stat helpers (`agent/player_stats.py`) — the bot imports from these. Everything else in `agent/` is legacy.

**The bot is the product.** It's called **Glint**, lives in `bot/`, and is what every session should focus on.

---

## What Glint Is

Glint is an autonomous prediction-market trading bot that runs 24/7 against **Kalshi** and takes edge across four active strategies. It's a pure Python `asyncio` application — one process, one Telegram bot interface, one orchestrator class (`GlintBot` in `bot/main.py`). No LLM in the trading loop. Every decision is deterministic math + safety checks + Kelly sizing.

**Starting capital:** $500 simulated (`PAPER_STARTING_BALANCE` in `config.py`). Real Kalshi account runs in parallel when `PAPER_MODE = False`.

**Current mode:** `PAPER_MODE = True` (see `bot/config.py:453`). Paper and live share the full pipeline — only `execute_trade()` branches on this flag.

**Top-level loop (from `GlintBot`):**
1. Scan for opportunities (every 2min live, 10min pregame, 30min idle)
2. Resolve settled positions + update patterns
3. Check fills on resting orders
4. Update P&L on open positions, fire take-profit / cut-loss alerts
5. Process new opportunities — paper auto-executes, live queues for GO button
6. Sleep until next scan

Concurrent with that main loop:
- **`_live_scan_loop()`** — every 60s scans Kalshi for live 1v1 matches (tennis, UFC, NBA, etc.) and auto-spawns `LiveGameWatcher` tasks for matches with clear leaders
- **`_crypto_scan_loop()`** — currently disabled (`CRYPTO_ENABLED = False`)

---

## Directory Map

```
hustle-agent/
├── bot/                    ← GLINT. This is the product.
│   ├── main.py             ← GlintBot orchestrator, Telegram commands, main loop (1326 lines)
│   ├── config.py           ← Every threshold, tuning constant, API path (501 lines)
│   ├── executor.py         ← Trade execution + 5-layer safety chain (1201 lines)
│   ├── live_watcher.py     ← Per-game 10s-tick watcher, momentum/arb strategies (2610 lines)
│   ├── scanner.py          ← Main scan_cycle(), opportunity aggregation (905 lines)
│   ├── scanner_sports.py   ← Sports parlay + live game scanners (594 lines)
│   ├── scanner_sports_arb.py ← Monotonicity + consistency riskless arb (539 lines)
│   ├── scanner_weather.py  ← NWS bias-corrected weather markets (341 lines)
│   ├── kalshi_series.py    ← Series ticker scanner — THE STAR: vig_stack_series (1774 lines)
│   ├── math_engine.py      ← All edge math + self-checking (forward & backward) (786 lines)
│   ├── odds_scraper.py     ← DK/Bovada/FanDuel/ESPN/TheRundown aggregator (1372 lines)
│   ├── tracker.py          ← Position tracking, P&L, settlement resolver (676 lines)
│   ├── notifier.py         ← Telegram send/edit, button callbacks, command registry (950 lines)
│   ├── game_context.py     ← Live game intelligence: momentum, wp, DQS, instincts (884 lines)
│   ├── sizing.py           ← Fractional Kelly with hard caps (115 lines)
│   ├── patterns.py         ← Historical win rate analysis per strategy type (452 lines)
│   ├── position_monitor.py ← Edge-recheck loop for open positions (464 lines)
│   ├── market_maker.py     ← Spread-capture MM pairs (394 lines)
│   ├── clv.py              ← Closing-line value tracking (310 lines)
│   ├── elo.py              ← ELO ratings (295 lines, lightly used)
│   ├── injuries.py         ← Injury + back-to-back data (415 lines)
│   ├── daily_log.py        ← Rolling daily performance log (217 lines)
│   ├── scheduler.py        ← Morning briefing + nightly summary cron (254 lines)
│   ├── outcome_tracker.py  ← SQLite outcome log for alert calibration (224 lines)
│   ├── price_monitor.py    ← Price delta cache (103 lines)
│   ├── logger.py           ← Rotating file handler (35 lines)
│   ├── state_io.py         ← Thread-safe JSON read/write (32 lines)
│   ├── econ_scanner.py     ← CPI/economic markets (disabled) (265 lines)
│   ├── crypto.py           ← Crypto price helpers (disabled) (123 lines)
│   ├── dashboard.html      ← Static HTML dashboard (read-only view of state)
│   ├── logs/bot.log        ← Rotating 10MB × 5 backups
│   └── state/              ← ALL runtime state lives here — SEE "State Files" below
│
├── agent/                  ← Legacy. DO NOT touch the LLM engine (engine.py).
│   ├── kalshi_client.py    ← The bot imports all Kalshi calls from here. KEEP.
│   ├── parlay.py           ← Team alias dicts + parlay parsing. KEEP.
│   ├── player_stats.py     ← Player prop probability. KEEP.
│   └── engine.py, pipeline.py, reports.py, etc. ← LLM loop. IGNORE per scope.
│
├── config/                 ← API credentials (gitignored)
│   ├── kalshi.json         ← api_key_id, private_key_path, environment
│   ├── kalshi-private-key.pem
│   ├── telegram.json       ← bot_token, chat_id
│   ├── sports_data.json    ← Odds API key (last-resort fallback)
│   ├── therundown.json     ← TheRundown free-tier key
│   └── fred.json           ← FRED economic data (disabled)
│
├── tests/                  ← pytest suite
│   ├── test_bot_executor.py, test_bot_scanners.py, test_bot_tracker.py
│   ├── test_live_watcher.py, test_sport_instincts.py, test_instincts.py
│   ├── test_data_driven_fixes.py, test_bot_improvements.py
│   └── test_kalshi.py, test_parlay.py, test_player_stats.py
│
├── state/                  ← Legacy agent state. IGNORE (per scope).
├── ui/                     ← Legacy React dashboard. Not actively maintained.
├── docs/                   ← Plans, migration notes
├── run_bot.sh              ← Watchdog shell loop: restart on exit
├── requirements.txt        ← anthropic, kalshi-python, python-telegram-bot, requests, matplotlib, certifi, bs4
├── CLAUDE.md               ← You are reading this
└── README.md               ← Deep prose overview of Glint (keep in sync)
```

---

## The Strategies

Active strategies live in `ACTIVE_STRATEGIES` in `config.py:462`. **Only these fire trades.** Everything else is disabled with commented-out reasons (data-driven kill decisions from the Apr 14 audit).

### ACTIVE

**Performance numbers below are post–Apr 18 audit**, rebuilt from `paper_trades.json` (PAPER_MODE=True) as ground truth. Seeded historical values in `strategy_audit.json` were inaccurate and have been replaced; `_log_settlements_to_audit` is now idempotent so the numbers stay honest going forward.

| Strategy | Location | Description | Real Perf (paper, Apr 18) |
|---|---|---|---|
| `vig_stack_series` | `kalshi_series.py` | Mutually-exclusive ladders (weather, S&P ranges) where YES prices sum > 100¢. Buy the cheap NOs. Structural arb, no prediction. **Currently net loser** due to volatile-family ladders (hot-weather cities + fast-moving indices). Filter F (Apr 18) restricts entries to stable families `KXHIGHMIA / KXHIGHAUS / KXINX`; volatile families require NO ≥ 0.90. | 43 settled, **−$101.08**, 21W/22L (49%) |
| `vig_stack_futures` | `kalshi_series.py` | Same math on championship futures: NBA (17% vig), NHL (22% vig), MLB (6% vig). Gated by Filter F same as series. | 0 settled |
| `sports_monotonicity_arb` | `scanner_sports_arb.py` | Riskless arb: spread/total threshold contracts must be monotonic. Violations = free money. | 0 real fills yet |
| `sports_consistency_arb` | `scanner_sports_arb.py` | Riskless arb: P(championship) ≤ P(individual series win). | 0 real fills yet |

### ACTIVE via live_watcher (separate from `ACTIVE_STRATEGIES`)

| Strategy | Location | Description | Real Perf (paper, Apr 18) |
|---|---|---|---|
| `live_momentum` | `live_watcher.py` | Buy dips on the clear leader in 1v1 live matches (tennis, UFC). Exit on trailing stop or take-profit. Auto-scans every 60s. Gets 20% of equity as its `STRATEGY_BUDGETS` allocation — no longer starved by vig_stack fills. | 16 settled, **+$9.80**, 9W/7L (56%) |
| `live_momentum` (conviction) | `live_watcher.py` | When there's no dip but game state screams value — wp_edge > 8%, positive momentum, 68-82¢ entry — buy anyway. NBA/NHL only (MLB 12% hit rate). | Rolled into live_momentum numbers above |

### DISABLED (data-driven kills)

All disabled strategies have `# Disabled: reason` comments in `config.py:468-475`. Briefly:

- `series_game_edge` — 26% WR, sportsbook odds are efficient
- `weather` (single-market) — 17% WR, NWS bias model too imprecise for individual strikes (**note:** vig_stack applied to the same weather ladders works — that's different math)
- `btc/eth/sol/xrp/doge/bnb_price_edge` — all crypto disabled (`CRYPTO_ENABLED = False`), vol model overestimates intraday movement
- `live_latency_arb` — replaced by `live_momentum` watcher system (2-min scan too slow)

**The audit lives in `bot/state/strategy_audit.json`.** Every strategy has: status, real_trades, real_pnl, real_wr, ghost_trades (from paper fill bug era), concerns, borrowed_concepts. Update it when strategies are added/removed/settled.

---

## The Safety Architecture

Every trade passes through `execute_trade()` in `bot/executor.py:367`. The chain:

1. **`verify_contract_direction()`** — MANDATORY. Parses the ticker + title, computes fair value, confirms the "recommended_side" is actually the side with edge. Catches backwards bets (buying NO when YES has the edge). Never bypass this.
2. **`_check_balance()`** — Paper mode reconstructs balance from `paper_trades.json`; live mode calls Kalshi. Enforces `PAPER_STARTING_BALANCE * 0.10` reserve floor.
3. **`_check_position_limits()`** — Multi-layer:
   - No position > 20% of balance (`MAX_POSITION_PERCENT`)
   - No duplicate entry on same ticker (auto-closes orphans first)
   - No opposite-side bet in same game (`GAME` ticker dedupe)
   - 4-hour cooldown after any exit/resolve on that ticker
   - Daily loss limit of $1.00/ticker (`_DAILY_TICKER_LOSS_LIMIT`)
   - **Total exposure ≤ `MAX_TOTAL_EXPOSURE` of balance (currently 100%)**, counting only `filled > 0` AND `status in ('filled', 'partial')` — NOT ghost resting orders, NOT exited positions
4. **`_verify_edge_still_exists()`** — Re-fetches current Kalshi price, recomputes edge with the same price basis used at scan time (`yes_ask` for both YES/NO trades). **3¢ kill switch** (`MAX_PRICE_MOVE_CENTS`): abort if price moved more than 3¢ since the alert. Momentum trades skip this (pure price action, no model fair value). Vig stack uses a 2% threshold (structural); everything else uses `MIN_RELATIVE_EDGE = 15%`.
5. **Self-check math** — `_self_check_edge()` runs forward (fair - price = edge) and backward (price + edge = fair). If they don't match within `EPSILON = 1e-6`, don't trade.

Paper mode does the same checks. It diverges only in `execute_trade()` after all gates pass: instead of calling Kalshi's `place_order`, it writes to `paper_trades.json` with `paper_filled` or `paper_resting` based on whether the limit price ≥ current ask.

---

## The Live Game Watcher (`bot/live_watcher.py`)

This is the most complex single file in the bot (2610 lines). It handles two activation modes and two trading strategies:

**Activation modes:**
- **Manual:** Telegram `WATCH <team>` → instantiates `LiveGameWatcher(query, notifier, balance=...)` → registered in `GlintBot._active_watchers`
- **Auto-scan:** `scan_live_matches()` every `LIVE_SCAN_INTERVAL` seconds discovers live 1v1 matches on Kalshi with clear leaders, auto-spawns watchers

**Trading strategies (resolved in `start()` based on sport):**
- **Arb mode (`_start_arb`)** — Team sports (NBA/NHL/MLB). Compares ESPN consensus win probability to Kalshi ask every 10s. Bets when latency gap > `LIVE_WATCH_EDGE_THRESHOLD = 10%` relative.
- **Momentum mode (`_start_momentum`)** — 1v1 matches (tennis, UFC). Tracks last 12 prices in `_price_history` deque, buys the leader when it dips `MOMENTUM_DIP_BUY = 4¢+` from recent high, skips dips > 8¢ (set changes / KO windows have 0% win rate per data audit).

**Per-tick flow (10 second cadence, `LIVE_POLL_INTERVAL`):**
1. `fetch_consensus_odds(sport, bypass_cache=True)` — bypass the 120s TTL that would starve a 10s loop
2. `_fetch_espn_score()` — score, clock, period from ESPN scoreboard
3. `get_market(ticker)` — current Kalshi `yes_ask` / `yes_bid`
4. `GameContext.update(...)` — recompute momentum, lead_trend, win_probability, DQS
5. `_compute_edge()` for arb OR dip detection + DQS check for momentum
6. `_check_exit()` — take-profit, trailing stop, stop-loss, near-settle, underwater exit (disabled — killed 100% of trades)
7. Enriched tick logged to `state/live_ticks.jsonl` with full context: price, leader, wp, momentum, lead_trend, completion, wp_edge, bid, opp_bid, volatility, espn_scores, game_state
8. `_format_status_card()` → `notifier.edit_message_by_id()` — single Telegram message edited in place, no spam

**Exit conditions (any triggers):**
- `LIVE_TAKE_PROFIT_CENTS = 12¢` from entry (backtested sweet spot)
- `LIVE_STOP_LOSS_CENTS = 30¢` from entry
- `LIVE_NEAR_SETTLE_CENTS = 93` (price ≥ 93¢, match almost over, lock in win)
- `MOMENTUM_DQS_TRAIL_STOP = 6¢` from peak after profit target hit
- `MOMENTUM_MAX_LOSS_DOLLARS = $5` hard cap per entry
- `LIVE_HARD_PROFIT_TARGET = 1.00` (100% gain — safety lock)

**Sport profiles (`SPORT_PROFILES` in `config.py:140-241`):**
Each sport (nba, nhl, mlb, tennis, ufc, atp aliases) has its own `min_dip`, `max_dip`, `max_entry`, `take_profit`, `stop_loss`, `trail_stop`, `max_contracts`. MLB is `"disabled": True` based on 13 trades, -$11.85. Tennis has `skip_dqs=True` for speed. UFC uses 10 max contracts (KO risk).

**Conviction entry (`CONVICTION_*` constants, `config.py:116-126`):** Sometimes there's no dip — the leader just keeps climbing. Conviction buys without a dip when ALL true: win_prob is 8%+ above Kalshi price, positive momentum, lead not shrinking, price in 68-82¢ zone, ≥12 ticks of game history, sport has a reliable wp model (NBA/NHL only — MLB/tennis/UFC excluded), game is Q3+ (`MIN_COMPLETION = 0.50`). Size is 70% of normal dip entry.

**Data-driven tuning:** Every `MOMENTUM_*` and `SPORT_PROFILES` value is annotated with the trade-log evidence that justifies it (see config comments). Example: `MOMENTUM_LEADER_MIN = 0.70` — below 70¢, 23 trades lost $67.77 at 22% WR; at 70¢+, 20 trades won $15.50 at 55% WR.

---

## The Telegram Interface

Glint is driven entirely through Telegram. Chat ID is loaded from `config/telegram.json`. Commands are case-insensitive and registered in `GlintBot._register_commands()` (`main.py:369-813`).

### Trading commands
| Command | What it does |
|---|---|
| `GO [n]` | Execute pending opportunity #n (default 1). Also fired by inline GO button. |
| `SKIP [n]` | Remove opportunity #n from queue. |
| `LIST` / `PENDING` | Show the pending queue. |
| `DETAIL [n]` | Full breakdown of pending opportunity #n. |
| `SCAN` | Force a scan cycle now. |
| `EDGES` | Show current top 3 edges found. |

### Position management
| Command | What it does |
|---|---|
| `LIVE` / `POSITIONS` | Open positions with unrealized P&L. |
| `SELL <ticker>` | Immediate exit. |
| `EXITALL` | Exit all open positions. |
| `TRAIL <ticker> <pct>` | Set a trailing stop. |

### Live game watching
| Command | What it does |
|---|---|
| `WATCH <team>` | Start a `LiveGameWatcher` for that query. Polls every 10s. |
| `UNWATCH` | Stop all active watchers. |
| `RECAP [date]` | Human-readable journal recap for that day (from `live_journal.json`). |
| `ANALYZE [date]` | Tick-level dip analysis: what dip size led to profitable exits. |

### Status & stats
| Command | What it does |
|---|---|
| `STATUS` | Balance, today P&L, total P&L, win rate, open positions, streak. |
| `BALANCE` | Raw Kalshi balance check. |
| `STATS` | Paper stats with strategy breakdown. |
| `HISTORY [n]` | Last n resolved trades. |
| `WINRATE` | Overall + per-strategy win rate + ROI. |
| `ROI` | Per-strategy ROI table. |
| `CLV` | Closing-line value report. |
| `MODE` | Current PAPER/LIVE mode + active strategies. |

### System
| Command | What it does |
|---|---|
| `LOGS` | Tail last 20 lines of `bot/logs/bot.log`. |
| `RESTART` | `kill -9 $pid` — watchdog (launchd or `run_bot.sh`) brings it back. |
| `STOP` | Unload launchd + kill. Only `START` from Claude Code brings it back. |

---

## Running & Operating

### Start
```bash
# From the project root
python3 -m bot.main
```
or via the watchdog:
```bash
./run_bot.sh
```
The bot acquires a PID lockfile at `bot/state/bot.lock` and aborts if another instance is running. Stale locks (PID dead) are overwritten automatically.

### Find processes
```bash
ps aux | grep -E "bot\.main|bot/main" | grep -v grep
```
If you see **more than one** Python process, kill the old one: `kill -9 <pid>`. Zombie processes are a recurring pain point — old processes don't always clear on plain `kill`, especially after pycache changes. When in doubt: `kill -9` + `find bot/ -name "*.pyc" -delete` + `rm -rf bot/__pycache__` + restart.

### Check logs
```bash
tail -50 bot/logs/bot.log
```
Rotating: 10 MB × 5 backups, all in `bot/logs/`.

### Tests
```bash
python3 -m pytest tests/ -v --tb=short
# Or a targeted file:
python3 -m pytest tests/test_live_watcher.py -v
```

### Flip PAPER → LIVE
Edit `bot/config.py:453`: `PAPER_MODE = False`. That's it. Everything else is the same code path. **Do not flip without 20+ resolved paper trades showing +CLV per active strategy.**

---

## State Files (`bot/state/`)

All runtime persistence lives here. Written atomically via `bot/state_io.py` (one `threading.Lock` serializes writes).

| File | Purpose |
|---|---|
| `bot.lock` | PID lockfile. Deleted on graceful shutdown. |
| `bot_state.json` | Last scan time, heartbeat, scan counters, DK/FD disabled flags (12h TTL). |
| `positions.json` | **Source of truth for open positions.** Exposure calc reads this. |
| `trade_history.json` | Append-only resolved trade log. |
| `paper_trades.json` | Paper mode trade log — balance is reconstructed from this. |
| `paper_trades_archive.json` | Old paper trades (rotated to prevent the main file from bloating). |
| `pending.json` | Queue of opportunities waiting for GO/SKIP. Max `PENDING_MAX = 20`. |
| `live_journal.json` | Live watcher journal: scan_found, bet, exit, session_end events. Feeds RECAP. |
| `live_ticks.jsonl` | Append-only **enriched** tick log: price, leader, wp, momentum, lead_trend, completion, wp_edge, bid, opp_bid, volatility, espn_scores, game_state. Feeds ANALYZE. |
| `strategy_audit.json` | **Read this to understand every strategy's real-money status.** Auto-updated by `tracker.py` on every settlement (append to `settlement_log`). |
| `patterns.json` | Historical win rate per strategy type for dynamic confidence (`scanner.py:_get_dynamic_confidence`). |
| `odds_snapshots.json` | Last 2 odds snapshots for line-movement detection. |
| `price_cache.json` | Short-lived price cache used by `price_monitor.py`. |
| `clv.json` | Closing-line value records per trade. |
| `outcomes.db` | SQLite: alert → outcome log for calibration. |
| `elo_ratings.json` | Sport ELO ratings (lightly used). |
| `mm_positions.json` | Market-maker pair state. |
| `daily_log.json` | Rolling daily performance snapshot. |
| `watchlist.json` | Manual watchlist tickers. |

**Never hand-edit these while the bot is running** — the bot re-reads on every scan and you'll lose writes. Stop bot → edit → restart.

---

## Critical Gotchas (Battle Scars)

These are real issues that have bitten this project. **Read before making changes to the relevant code.**

### 1. Paper mode fill bug (fixed Apr 15, but watch for regressions)
Pre-fix: `execute_trade()` in PAPER_MODE always set `filled_count: 0`. Every momentum/watcher trade for ~10 days was a "ghost" — tracked as if placed but never filled, never settling. The $34.67 reported "profit" was entirely fake. Fix in `executor.py`: paper mode now checks `current_ask <= price_cents` and instant-fills marketable limit orders, otherwise writes `paper_resting`. **If you see `filled_count: 0` in paper trade records going forward, this regressed.**

### 2. Ghost exposure blocking all trades
The exposure calc in `_check_position_limits` used to sum `p.get("cost", 0)` for ALL positions including unfilled resting orders and exited positions. Result: $115.72 of ghosts blocked every new trade. Current correct filter (do not weaken it):
```python
if isinstance(p, dict)
and p.get("filled", 0) > 0
and p.get("status") in ("filled", "partial")
```
Exited positions have `status: "exited"` (not `filled/partial`), so they're excluded. Resolved positions have `status: "resolved"`, also excluded.

### 3. Multiple processes
The watchdog (`run_bot.sh`) and launchd both try to keep the bot alive. If the Telegram `RESTART` command kills the process without cleaning up cleanly, a zombie can linger. Always verify `ps aux | grep bot.main` after RESTART. If two PIDs appear, `kill -9` the older one.

### 4. `bypass_cache=True` for live watcher
`odds_scraper.fetch_consensus_odds(sport)` caches for 120s (live) / 900s (idle). The 10s watcher tick MUST pass `bypass_cache=True` or 11 of 12 ticks see stale data. Already in place — don't remove it.

### 5. Edge price basis mismatch
YES-side trades compute edge from `yes_ask`. NO-side (vig_stack) trades MUST also use `yes_ask` as the anchor (`kalshi_no = 1 - yes_ask/100`), NOT `no_ask`. Using `no_ask` creates phantom price movement due to bid/ask spread, which trips the 3¢ kill switch on every recheck. See the explicit comment in `executor.py:_verify_edge_still_exists()`.

### 6. Watchdog heartbeat
`GlintBot.start()` checks `last_heartbeat` in `bot_state.json`. If stale > 15 min and `running=True`, logs a watchdog alert (currently silenced before sending). Useful signal that the previous process crashed silently — check `bot.log` for what happened.

### 7. Kalshi position reconcile on startup
If the bot crashed between placing an order and writing to `positions.json`, the position exists on Kalshi but not locally. On startup, `GlintBot.start()` calls `kalshi_get_positions()` and merges any missing ones with `source: "kalshi_reconcile"`. If you see reconcile warnings on startup, that's why.

### 8. Same-game opposite-side block
`_check_position_limits` blocks betting the opposing team if we already hold the other side in the same game (ticker prefix match on `GAME`). This is deliberate and catches a real failure mode where multiple scanners independently find "edge" on both sides of the same market.

### 9. Vig stack edge flip exemption
`recheck_open_edges` can auto-exit when live edge drops below entry edge. Vig stack positions are exempt (`opp_type in ("vig_stack_no", "vig_stack_series")`) because the edge is structural — individual contract prices moving doesn't invalidate the ladder math; only a collapse of the entire ladder's vig would.

### 10. NO at 90-95¢ risk/reward
Vig_stack's cheap NOs are often in the 89-95¢ range. That's 8:1 to 19:1 risk/reward against you. Even at ~50% real WR on volatile ladders, the math collapsed — 43 trades net −$101 on paper. **Filter F (Apr 18)** is the answer: whitelist stable families (`VIG_STACK_STABLE_FAMILIES = {"KXHIGHMIA","KXHIGHAUS","KXINX"}`), and require NO ≥ 0.90 (`VIG_STACK_WEATHER_MIN_PRICE`) on anything else. Do not weaken these without 48h of post-filter data.

### 11. `STRATEGY_BUDGETS` (Apr 16) — per-strategy exposure caps
`config.py:STRATEGY_BUDGETS = {"vig_stack": 0.60, "live_momentum": 0.20, "arbs": 0.20}`. Enforced in `executor._check_position_limits` against **equity** (`balance + total_exposure`), not just cash. Pre-Apr-16, a single 100% pool let vig_stack starve conviction trades indefinitely. Rejections surface as `STRATEGY_BUDGET: <strategy> has $X of $Y budget` in logs. Don't remove — live_momentum can't fire without it.

### 12. Settlement log idempotency (Apr 18)
`tracker._log_settlements_to_audit` is now idempotent on `(ticker, strategy, result, pnl, contracts)`. Pre-fix, re-running `resolve_trades` on already-settled positions double-counted — one ticker had 14 duplicate entries. If you touch that function, keep the dedup check in or the strategy totals will drift again.

---

## Data-Driven Tuning (The Apr 14 Audit)

Every kill decision and threshold in `config.py` has a one-line justification from 43 real trades logged Apr 9-14. Representative findings:

- **Entry price is the #1 predictor.** Below 70¢ entries: -$67.77 across 23 trades (22% WR). At 70¢+: +$15.50 across 20 trades (55% WR). → `MOMENTUM_LEADER_MIN = 0.70`.
- **SL slippage is catastrophic.** 10-second ticks cause gaps; avg SL hit was 21¢ vs 12-15¢ configured. → All sport profiles tightened to `stop_loss: 10`.
- **Trailing stop IS the edge.** +$67.75 from TP exits. Keep `trail_stop`. Avg TP gain = +15.2¢.
- **Conviction entry is Q3+ only.** 253 candidates at ≥50% completion: +2.8¢/trade, 79% hit. Below 50%: flat/negative. → `CONVICTION_MIN_COMPLETION = 0.50`.
- **MLB conviction hits 12%.** → excluded from `CONVICTION_EXCLUDED_SPORTS`.
- **9-12¢ dips = 65% win, +3.67¢ avg.** → `MOMENTUM_SCALE_LARGE_DIP = 1.5x`.
- **Every underwater exit recovered to TP.** → `MOMENTUM_UW_DEPTH_CENTS = 99` and `MOMENTUM_UW_TICKS = 999` (effectively disabled).

Don't change these without data. If you do change one, update the comment with the new evidence.

---

## Money (The Honest Numbers)

**Ground truth source:** `paper_trades.json` (PAPER_MODE = True). Every entry with `status ∈ {won, lost, exited_early}` is a real resolution. `trade_history.json` has 0 resolved real-money entries — nothing has settled in live mode yet.

**Post-rebuild numbers (Apr 18, after the seeded-values cleanup):**

| Strategy | Settled | P&L | W / L | WR |
|---|---|---|---|---|
| `vig_stack_series` | 43 | **−$101.08** | 21 / 22 | 49% |
| `live_momentum` | 16 | **+$9.80** | 9 / 7 | 56% |
| Everything else | 0 | $0 | — | — |

**Grand total paper P&L: ≈ −$91.** The older docs that quoted −$17 were based on seeded `strategies.real_*` values that never reconciled with `paper_trades.json`. Those were wiped and rebuilt on Apr 18; don't trust any pre-Apr-18 summary.

**Why vig_stack is negative:** 22 of 43 settled trades closed at a loss, most via early-exit on volatile ladders (hot-weather cities outside MIA/AUS, S&P minus INX). The Apr 18 **Filter F** (volatile-family gate at NO ≥ 0.90) is designed to stop new entries of this type. Going forward we expect `real_pnl` to drift positive on new vig_stack trades. If it doesn't over the next 48h, Filter F isn't tight enough.

**Why live_momentum is positive:** the 9W/7L / +$9.80 result came with NO per-strategy budget cap — conviction trades were silently starved by vig_stack's 100% exposure pool. `STRATEGY_BUDGETS` (live_momentum: 20% of equity) was wired in Apr 16 to fix that.

**Open exposure** (from positions.json, check at session start): currently 8 vig_stack positions, ~$98 exposure. All are Filter-F-compliant families (MIA/AUS/INX) — i.e., everything left in the book is the profile we *want* to hold.

**Settlement idempotency (Apr 18):** `_log_settlements_to_audit` in `tracker.py` had a bug — every call appended to `settlement_log` without dedup, so `resolve_trades` re-runs on already-settled positions double-counted. One ticker had 14 duplicate entries. Fixed with a `(ticker, strategy, result, pnl, contracts)` fingerprint check; the strategy totals also skip on dup so rollups stay clean.

---

## When Tyler Asks "How is it looking?"

Run this checklist:
1. `ps aux | grep bot.main | grep -v grep` — verify ONE process
2. `tail -30 bot/logs/bot.log` — verify ticks are firing, no repeated exceptions
3. `python3 -c "import json; p=json.load(open('bot/state/positions.json')); active=[x for x in p if isinstance(x,dict) and x.get('filled',0)>0 and x.get('status') in ('filled','partial')]; print(f'Active: {len(active)} positions, ${sum(x.get(\"cost\",0) for x in active):.2f} exposure')"` — verify exposure is under balance
4. Check `strategy_audit.json → settlement_log` for any settlements since last check
5. If exposure is maxed out (blocking new trades), note which bets are about to settle and when — that's when capital frees up

Answer in terms of: single process ✓/✗, exposure vs balance, what's settling soon, any repeated blocked-trade warnings. Don't invent performance numbers — pull them from `trade_history.json` and `strategy_audit.json`.

---

## Style Rules for This Codebase

- **Logger names are `glint.*` or `nexus.*`** (historical — bot was renamed from Nexus to Glint, not all loggers migrated). Don't standardize without a reason.
- **Money is always in dollars as floats**, contracts in integers, prices in integer cents (1-99). `price_cents / 100.0` to get the decimal price. Don't mix.
- **Every state write goes through `bot/state_io.py`** (`_load_json` / `_save_json`). Never write JSON directly from ad-hoc code — you'll race the main loop.
- **Every edge calc has a self-check.** `_self_check_edge(fair_value, market_price, edge)` runs forward and backward. Don't trust math that doesn't self-check.
- **Every trade has `verify_contract_direction()`.** Never skip. Never weaken it. The "last line of defense against backwards bets" comment is not rhetorical.
- **Config constants have comments with data.** If you add a new threshold, include the evidence or mark it `# tuned by feel — revisit after 20 resolved`.
- **Telegram messages are terse.** No prose. Bullets, numbers, action verbs. Emojis sparingly and only to highlight state (💰 profit, ❌ failure, ⏭️ skip, 🎯 edge, ♻️ restart, 🛑 stop).
- **Logs are the audit trail.** Log edge decisions, trade decisions, exit decisions. Don't log "starting loop" spam. INFO for actionable events, WARNING for safety-gate failures, ERROR for exceptions.

---

## Quick Reference: Where Things Live

**"How do I look at..."**

| Question | File |
|---|---|
| What strategies are active? | `bot/config.py:462` (`ACTIVE_STRATEGIES`) |
| What's the current mode? | `bot/config.py:453` (`PAPER_MODE`) |
| Why is a trade being blocked? | `bot/executor.py:96` (`_check_position_limits`) |
| How is edge calculated? | `bot/math_engine.py` |
| How are watchers started? | `bot/main.py:766` (`handle_watch`) + `bot/live_watcher.py:348` (`start`) |
| Where's the Telegram command list? | `bot/main.py:369-813` (`_register_commands`) |
| What did strategy X do historically? | `bot/state/strategy_audit.json` |
| What trades have settled? | `bot/state/trade_history.json` |
| What's in the pending queue? | `bot/state/pending.json` |
| What live ticks were logged? | `bot/state/live_ticks.jsonl` |
| How are positions priced mark-to-market? | `bot/tracker.py:34` (`update_positions`) |
| How does settlement resolution work? | `bot/tracker.py` (`resolve_trades` + `_log_settlements_to_audit`) |
| How is sizing calculated? | `bot/sizing.py:11` (`kelly_size`) |
| Where's the consensus odds aggregator? | `bot/odds_scraper.py:1155` (`fetch_consensus_odds`) |
| Where's the sport instincts logic? | `bot/game_context.py` + `bot/config.py:94-100` (`INSTINCT_*`) |
| Where's the DQS (dip quality score)? | `bot/game_context.py` + `bot/config.py:134` (`MOMENTUM_DQS_THRESHOLD`) |

**"Why is X happening?"**

| Symptom | Likely cause | File |
|---|---|---|
| Every trade blocked with "Total exposure too high" | Account is over-allocated (not a bug, a math reality) | `executor.py:239` |
| Trades keep blocked with "Already hold open position" | Duplicate entry guard, check if market actually settled | `executor.py:115` |
| Every live game edge shows the same price for 2 minutes | `bypass_cache=True` not being passed to `fetch_consensus_odds` | `live_watcher.py` |
| Paper trades all show `filled=0` | Paper fill bug regressed | `executor.py:execute_trade` (PAPER_MODE branch) |
| Two bot processes running | `RESTART` didn't cleanly kill | `kill -9 <pid>` |
| Bot stopped ticking | Crashed silently, check watchdog heartbeat | `bot_state.json.last_heartbeat` |
| `bot.lock` exists but no process | Stale lock from previous crash | Delete it and restart |
| Conviction entry never fires | Completion < 50%, or wrong sport, or wp_edge too small | `CONVICTION_*` in config + log output |

---

## Final Note

Glint is a production trading system that has lost real money and is slowly learning. Every config value is someone's scar. Every disabled strategy has a reason. When in doubt: read the comment, check `strategy_audit.json`, pull the evidence from `trade_history.json` or `live_ticks.jsonl` before changing anything. The data is the source of truth, not intuition.
