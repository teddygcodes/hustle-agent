# HUSTLE AGENT — Project Guide for Claude Code

## Project Scope (Read First)

**Bot only.** Per user instruction in `~/.claude/projects/.../memory/project_scope.md`: the agent LLM reasoning loop in `agent/engine.py` is **excluded from all work**. Do not run, modify, extend, or debug the agent's LLM cycle. The `agent/` directory is kept around because it owns the **Kalshi REST client** (`agent/kalshi_client.py`), team-alias dicts (`agent/parlay.py`), and player stat helpers (`agent/player_stats.py`) — the bot imports from these. Everything else in `agent/` is legacy.

**The bot is the product.** It's called **Glint**, lives in `bot/`, and is what every session should focus on.

---

## What Glint Is

Glint is an autonomous prediction-market trading bot that runs 24/7 against **Kalshi** and takes edge across four active strategies. It's a pure Python `asyncio` application — one process, one Telegram bot interface, one orchestrator class (`GlintBot` in `bot/main.py`). No LLM in the trading loop. Every decision is deterministic math + safety checks + Kelly sizing.

**Starting capital:** $500 simulated (`PAPER_STARTING_BALANCE` in `config.py`). Real Kalshi account runs in parallel when `PAPER_MODE = False`.

**Current mode:** `PAPER_MODE = True` (see `bot/config.py:569`). Paper and live share the full pipeline — only `execute_trade()` branches on this flag.

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
│   ├── config.py           ← Every threshold, tuning constant, API path (630 lines)
│   ├── executor.py         ← Trade execution + 5-layer safety chain (1285 lines)
│   ├── live_watcher.py     ← Per-game 10s-tick watcher, momentum/arb strategies (2769 lines)
│   ├── scanner.py          ← Main scan_cycle(), opportunity aggregation (1035 lines)
│   ├── scanner_sports.py   ← Sports parlay + live game scanners (594 lines)
│   ├── scanner_sports_arb.py ← Monotonicity + consistency riskless arb (544 lines)
│   ├── scanner_weather.py  ← NWS bias-corrected weather markets (341 lines)
│   ├── kalshi_series.py    ← Series ticker scanner — THE STAR: vig_stack_series (1774 lines)
│   ├── math_engine.py      ← All edge math + self-checking (forward & backward) (786 lines)
│   ├── odds_scraper.py     ← DK/Bovada/FanDuel/ESPN/TheRundown aggregator (1372 lines)
│   ├── tracker.py          ← Position tracking, P&L, settlement resolver (762 lines)
│   ├── notifier.py         ← Telegram send/edit, button callbacks, command registry (950 lines)
│   ├── game_context.py     ← Live game intelligence: momentum, wp, DQS, instincts (884 lines)
│   ├── sizing.py           ← Fractional Kelly with hard caps (115 lines)
│   ├── patterns.py         ← Historical win rate analysis per strategy type (452 lines)
│   ├── position_monitor.py ← Edge-recheck loop for open positions (464 lines)
│   ├── market_maker.py     ← Spread-capture MM pairs (409 lines)
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

Active strategies live in `ACTIVE_STRATEGIES` in `config.py:578`. **Only these fire trades.** Everything else is disabled with commented-out reasons (data-driven kill decisions from the Apr 14 audit).

### ACTIVE

**Performance numbers below are post–Apr 20 rebuild**, rebuilt from `paper_trades.json` (PAPER_MODE=True) as ground truth. Apr 20 Session 1 wired `exited_early` settlements into the audit pipeline (they were previously invisible), so the numbers now include all 59 early exits plus the 34 market-close won/lost. Invariant warning fires if paper/log/rollup counts ever drift again.

| Strategy | Location | Description | Real Perf (paper, Apr 20) |
|---|---|---|---|
| `vig_stack_series` | `kalshi_series.py` | Mutually-exclusive ladders (weather, S&P ranges) where YES prices sum > 100¢. Buy the cheap NOs. Structural arb, no prediction. **Currently net loser** due to volatile-family ladders (hot-weather cities + fast-moving indices). Filter F stable families `KXHIGHMIA / KXHIGHAUS / KXINX` enter freely; volatile families require NO ≥ 0.93 (Apr 20, raised from 0.90 after bucket analysis showed only [92-96¢) is breakeven). | 54 settled, **−$110.62**, 29W/25L (54%) |
| `vig_stack_futures` | `kalshi_series.py` | Same math on championship futures: NBA (17% vig), NHL (22% vig), MLB (6% vig). Gated by Filter F same as series. | 0 settled |
| `sports_monotonicity_arb` | `scanner_sports_arb.py` | Riskless arb: spread/total threshold contracts must be monotonic. Violations = free money. | 0 real fills yet |
| `sports_consistency_arb` | `scanner_sports_arb.py` | Riskless arb: P(championship) ≤ P(individual series win). | 0 real fills yet |

### ACTIVE via live_watcher (separate from `ACTIVE_STRATEGIES`)

| Strategy | Location | Description | Real Perf (paper, Apr 18) |
|---|---|---|---|
| `live_momentum` | `live_watcher.py` | Buy dips on the clear leader in 1v1 live matches (UFC now; NBA/NHL via team-sport watchers). Tennis (main ATP, WTA, and both challenger tours) disabled Apr 20 via `MOMENTUM_DISABLED_SPORTS` — 72% of momentum volume for −$6.20 net. Leader floor is 0.70 (Apr 20 Session 2 briefly raised to 0.75 but reverted same day — the bump admitted the [75-80¢) dead zone instead of skipping it; see config.py:69). Auto-scans every 60s; 20% of equity via `STRATEGY_BUDGETS`. | 39 settled, **+$12.30**, 24W/15L (62%) |
| `live_momentum` (conviction) | `live_watcher.py` | When there's no dip but game state screams value — wp_edge > 8%, positive momentum, 68-82¢ entry — buy anyway. NBA/NHL only (MLB 12% hit rate). | Rolled into live_momentum numbers above |

### DISABLED (data-driven kills)

All disabled strategies have `# Disabled: reason` comments directly below the `ACTIVE_STRATEGIES` list in `config.py` (around lines 585-605). Briefly:

- `series_game_edge` — 26% WR, sportsbook odds are efficient
- `weather` (single-market) — 17% WR, NWS bias model too imprecise for individual strikes (**note:** vig_stack applied to the same weather ladders works — that's different math)
- `btc/eth/sol/xrp/doge/bnb_price_edge` — all crypto disabled (`CRYPTO_ENABLED = False`), vol model overestimates intraday movement
- `live_latency_arb` — replaced by `live_momentum` watcher system (2-min scan too slow)

**The audit lives in `bot/state/strategy_audit.json`.** Every strategy has: status, real_trades, real_pnl, real_wr, ghost_trades (from paper fill bug era), concerns, borrowed_concepts. Update it when strategies are added/removed/settled.

---

## The Safety Architecture

Every trade passes through `execute_trade()` in `bot/executor.py:451`. The chain:

1. **`verify_contract_direction()`** — MANDATORY. Parses the ticker + title, computes fair value, confirms the "recommended_side" is actually the side with edge. Catches backwards bets (buying NO when YES has the edge). Never bypass this.
2. **`_check_balance()`** — Paper mode reconstructs balance from `paper_trades.json`; live mode calls Kalshi. Enforces `PAPER_STARTING_BALANCE * 0.10` reserve floor.
3. **`_check_position_limits()`** — Multi-layer:
   - No position > 20% of balance (`MAX_POSITION_PERCENT`)
   - No duplicate entry on same ticker (auto-closes orphans first)
   - No opposite-side bet in same game (`GAME` ticker dedupe)
   - 4-hour cooldown after any exit/resolve on that ticker
   - Daily loss limit of $1.00/ticker (`_DAILY_TICKER_LOSS_LIMIT`)
   - **Total exposure ≤ `MAX_TOTAL_EXPOSURE` of equity (currently 100%, where equity = balance + total_exposure — the Apr 16 fix)**, counting only `filled > 0` AND `status in ('filled', 'partial')` — NOT ghost resting orders, NOT exited positions
   - **Per-strategy budgets** (`STRATEGY_BUDGETS`, Apr 16) — vig_stack 60%, live_momentum 20%, arbs 20% of equity. Rejections surface as `STRATEGY_BUDGET: <strategy> has $X of $Y budget`. See Gotcha #11.
4. **`_verify_edge_still_exists()`** — Re-fetches current Kalshi price, recomputes edge with the same price basis used at scan time (`yes_ask` for both YES/NO trades). **3¢ kill switch** (`MAX_PRICE_MOVE_CENTS`): abort if price moved more than 3¢ since the alert. Momentum trades skip this (pure price action, no model fair value). Vig stack uses a 2% threshold (structural); everything else uses `MIN_RELATIVE_EDGE = 15%`.
5. **Self-check math** — `_self_check_edge()` runs forward (fair - price = edge) and backward (price + edge = fair). If they don't match within `EPSILON = 1e-6`, don't trade.

Paper mode does the same checks. It diverges only in `execute_trade()` after all gates pass: instead of calling Kalshi's `place_order`, it writes to `paper_trades.json` with `paper_filled` or `paper_resting` based on whether the limit price ≥ current ask.

---

## The Live Game Watcher (`bot/live_watcher.py`)

This is the most complex single file in the bot (2769 lines). It handles two activation modes and two trading strategies:

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

**Sport profiles (`SPORT_PROFILES` in `config.py:150-260`):**
Each sport (nba, nhl, mlb, tennis, ufc, atp aliases) has its own `min_dip`, `max_dip`, `max_entry`, `take_profit`, `stop_loss`, `trail_stop`, `max_contracts`. MLB is `"disabled": True` based on 13 trades, -$11.85. Tennis has `skip_dqs=True` for tick-speed but now gates entries through a `variance_quality_gate` (Tier 2.4) — a lightweight replacement for full DQS that rejects flat-variance windows (set breaks, no info). UFC uses 10 max contracts (KO risk).

**Conviction entry (`CONVICTION_*` constants, `config.py:118-126`):** Sometimes there's no dip — the leader just keeps climbing. Conviction buys without a dip when ALL true: win_prob is 8%+ above Kalshi price, positive momentum, lead not shrinking, price in 68-82¢ zone, ≥12 ticks of game history, sport has a reliable wp model (NBA/NHL only — MLB/tennis/UFC excluded), game is Q3+ (`MIN_COMPLETION = 0.50`). Size is 70% of normal dip entry.

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
Edit `bot/config.py:569`: `PAPER_MODE = False`. That's it. Everything else is the same code path. **Do not flip without 20+ resolved paper trades showing +CLV per active strategy.**

---

## State Files (`bot/state/`)

All runtime persistence lives here. Written atomically via `bot/state_io.py` (one `threading.Lock` serializes writes).

| File | Purpose |
|---|---|
| `bot.lock` | PID lockfile. Deleted on graceful shutdown. **Mtime advances every 30s via a dedicated heartbeat task (Apr 24, Session 7; was per-scan in Session 5)** — fresh mtime + dead PID means the process was killed between heartbeats without releasing the lock. |
| `bot_state.json` | Last scan time, heartbeat, scan counters, DK/FD disabled flags (12h TTL), Session-5 `last_ticks_rotation` flag. |
| `positions.json` | **Source of truth for open positions.** Exposure calc reads this. |
| `trade_history.json` | **Order log.** Every `execute_trade()` and `execute_hedge()` appends a record (filled OR resting). `tracker.resolve_trades()` updates entries in-place when markets settle. Distinct from `paper_trades.json` (the paper-mode resolution log). Read by the Telegram `HISTORY` command and `patterns.analyze_patterns()`. |
| `paper_trades.json` | **Paper resolution log.** Balance is reconstructed from this. NOT the same as `trade_history.json` (orders) — paper-mode resolutions live here, with `status ∈ {won, lost, exited_early}` driving the post-Session-1 settlement pipeline. |
| `archive/live_ticks-YYYY-MM-DD.jsonl.gz` | Daily gzipped tick archives. Created by `scheduler._rotate_live_ticks()` at midnight ET (Apr 23, Session 5). |
| `decisions.jsonl` | **Per-decision audit log (Apr 24, Session 6).** Every scan-time accept/reject from scanner + executor + live_watcher with a gate fingerprint. Read by `tools/cohort_report.py`. Daily rotation to `archive/`. |
| `archive/decisions-YYYY-MM-DD.jsonl.gz` | Daily gzipped decision archives. Created by `scheduler._rotate_decisions_log()` at midnight ET (Apr 24, Session 6). |
| `predictions.jsonl` | **Per-prediction fair-value vs. actual log (Apr 25, Session 11).** One row per opp evaluated (acted-on or counterfactual). Settlement poller fills `closing_yes_price`. Read by `tools/calibration_report.py`. Daily rotation to `archive/`. |
| `archive/predictions-YYYY-MM-DD.jsonl.gz` | Daily gzipped prediction archives. Created by `scheduler._rotate_predictions_log()` at midnight ET (Apr 25, Session 11). |
| `order_microstructure.jsonl` | **Per-live-order microstructure log (Apr 25, Session 15).** One row per live Kalshi order at terminal status (filled / canceled / rejected). Schema: `{ts_placed, ts_filled, ts_canceled, requested/filled price+qty, slippage_cents, slippage_source, latency_ms, queue_depth_at_place, partial_fill_count, strategy_name, opp_type, ticker, side, terminal_status, kalshi_order_id, regime}`. Read by `tools/microstructure_report.py`. **Empty until PAPER_MODE=False.** Paper trades do NOT write here — paper write path is unchanged. Daily rotation to `archive/`. |
| `archive/order_microstructure-YYYY-MM-DD.jsonl.gz` | Daily gzipped microstructure archives. Created by `scheduler._rotate_order_microstructure_log()` at midnight ET (Apr 25, Session 15). |
| `pending.json` | Queue of opportunities waiting for GO/SKIP. Max `PENDING_MAX = 20`. |
| `live_journal.json` | Live watcher journal: scan_found, bet, exit, session_end events. Feeds RECAP. |
| `live_ticks.jsonl` | Append-only **enriched** tick log: price, leader, wp, momentum, lead_trend, completion, wp_edge, bid, opp_bid, volatility, espn_scores, game_state. Feeds ANALYZE. |
| `strategy_audit.json` | **Read this to understand every strategy's real-money status.** Auto-updated by `tracker.py` on every settlement (append to `settlement_log`). |
| `patterns.json` | Historical win rate per strategy type for dynamic confidence (`scanner.py:_get_dynamic_confidence`). |
| `clv.json` | Closing-line value records per trade. **`clv._load()` filters to active strategies on read (Apr 23, Session 5)** — disabled-strategy records get dropped and never re-saved. **Also stores counterfactual records (`status="counterfactual_open"` → `"counterfactual_settled"`, `trade_id` prefixed `CF-`) for top-5 rejected opportunities per scan (Apr 24, Session 6)** — `get_clv_report()` filters `status=="settled"` so CFs do not pollute paper-trade stats. |
| `outcomes.db` | SQLite: alert → outcome log for calibration. |
| `elo_ratings.json` | Sport ELO ratings (lightly used). |
| `mm_positions.json` | Market-maker pair state. |
| `daily_log.json` | Rolling daily performance snapshot. |

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

**Apr 24 (Session 7):** A dedicated 30s heartbeat task in `GlintBot.start()` now touches `bot.lock` independently of `scan_interval`, so worst-case stale gap is ≤60s rather than ≤30 min. Failure signatures: (a) **fresh `bot.lock` mtime + dead PID** → killed between heartbeats without releasing the lock (`rm` the lock and restart); (b) **`bot.lock` mtime > 60s stale + `_pid_is_running` → True** → heartbeat task wedged or event loop blocked (check `bot.log` for stuck I/O or held asyncio lock). The Session-5 per-scan `LOCK_FILE.touch()` after `_save_bot_state()` in `_main_loop` is preserved as an extra signal.

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

**Post-rebuild numbers (Apr 20, after Session 1 settlement-pipeline fix):**

| Strategy | Settled | P&L | W / L | WR |
|---|---|---|---|---|
| `vig_stack_series` | 54 | **−$110.62** | 29 / 25 | 54% |
| `live_momentum` | 39 | **+$12.30** | 24 / 15 | 62% |
| Everything else | 0 | $0 | — | — |

**Grand total paper P&L: ≈ −$98.** Apr 20 rebuild was necessary because `exited_early` trades (59 of 93 resolved) were silently missing from `settlement_log` — only market-close won/lost were being logged. Post-fix: `executor._paper_record_exit` calls `tracker.log_settlement` + `patterns.record_resolution` on every paper exit, so the three counts (paper resolved, settlement_log length, sum of `strategies[k].real_trades`) stay in lock-step. Invariant warning fires if they drift.

The Apr 18 numbers (43 vig_stack / 16 live_momentum) were "honest" given the then-visible data but missed 50 exited_early trades. Don't trust any pre-Apr-20 summary for early-exit strategies.

**Why vig_stack is negative:** of 54 settled trades, 25 closed at a loss — the weight concentrated in volatile ladders. Ground truth by family: volatile (`KXHIGHDEN/NY/CHI`) = 36 trades, −$126.88, 69% early-cut; whitelist (`KXHIGHMIA/AUS/INX`) = 18 trades, +$16.26. Apr 18 Filter F set the volatile floor at NO ≥ 0.90; Apr 20 Session 2 raised it to **0.93** after bucket analysis showed only [92-96¢) is breakeven. Going forward we expect `real_pnl` to drift positive on new volatile-family trades. If a post-0.93 cohort of 10+ still prints negative, escalate.

**Why live_momentum is positive:** NBA + NHL alone = +$19.60 on 10 trades. Tennis was the drag: 72% of momentum volume for −$6.20 net (ATP Challenger −$7.80 / 82% cut, WTA −$7.00 / 71% cut). Apr 20 Session 2 added `MOMENTUM_DISABLED_SPORTS = {atp, atp_challenger, wta, wta_challenger}` (blanket tennis kill). Session 2 also briefly raised `MOMENTUM_LEADER_MIN` from 0.70 to 0.75 to "skip the [75-80¢) dead zone" — but MIN is a floor, so 0.75 admits the dead zone while surrendering the positive [70-75¢) bucket. Reverted to 0.70 same day; proper dead-zone filter (explicit [75-80¢) exclusion in `is_leader`) is TODO. `STRATEGY_BUDGETS` (live_momentum: 20% of equity, wired Apr 16) also stopped conviction trades from being starved by vig_stack's pool.

**Open exposure** (from positions.json, check at session start): ~10-16 open positions, mostly vig_stack. Whitelist families (`KXHIGHMIA` / `KXHIGHAUS` / `KXINX`) enter freely; volatile families (`KXHIGHDEN` / `KXHIGHNY` / `KXHIGHCHI`) now require NO ≥ 0.93 (post–Session 2). Any already-open position with a pre-0.93 entry continues to exit on normal rules — the floor gates entries, not exits.

**Settlement idempotency (Apr 18):** `_log_settlements_to_audit` in `tracker.py` had a bug — every call appended to `settlement_log` without dedup, so `resolve_trades` re-runs on already-settled positions double-counted. One ticker had 14 duplicate entries. Fixed with a `(ticker, strategy, result, pnl, contracts)` fingerprint check; the strategy totals also skip on dup so rollups stay clean.

---

## Apr 20 Audit — Remediation Plan

The Apr 20 state audit surfaced 12 issues across real bugs, tuning opportunities, and dead weight. Bundled into 5 focused sessions below. Each is self-contained, verifies against `bot/state/`, and can land independently. **Planned order: 1 → 2 → 5 → 3 → 4.** All five sessions shipped (Apr 20 / Apr 23).

Status legend: ☐ pending · ◐ in-progress · ☑ done.

### ☑ Session 1 — Settlement + pattern pipeline (Apr 20)
**Problem (pre-fix).** 58 resolved paper trades missing from `strategy_audit.settlement_log`, all `exited_early`. Ground-truth paper_trades.json showed 93 resolved (32W / 2L / 59 exited_early), settlement_log had 35, patterns.json had `total_resolved: 0`.

**Root cause.** All `exited_early` writes funnel through `executor._paper_record_exit`, which never called `_log_settlements_to_audit` or `patterns.record_resolution` (the latter didn't exist).

**What shipped.**
- `tracker.py` — extracted `log_settlement(trade)` per-trade helper; accepts both paper_trades schema (type/status/pnl) and trade_history schema (opp_type/result/contracts). Maps `type="vig_stack"` → `strategy="vig_stack_series"`. Derives won/lost from pnl sign for `exited_early`. Kept `(ticker, strategy, result, pnl, contracts)` dedup.
- `tracker.check_settlement_invariant()` — logs WARNING if `paper_resolved ≠ len(settlement_log) ≠ sum(strategies[k].real_trades)`. Called at end of batch writes, not per-call.
- `patterns.py` — new `record_resolution(trade)` does a full rebuild of `patterns.json` from `paper_trades.json` (matches existing `analyze_patterns` + `save_patterns` style — harder to desync than incremental). `_resolved_trades` extended to accept paper_trades `status ∈ {won, lost, exited_early}`.
- `executor._paper_record_exit` — single hook point. Lazy imports `log_settlement` + `record_resolution`. Covers live_watcher `_check_exit`, manual SELL, EXITALL (all three paths flow through this one function — verified).
- `tracker.resolve_trades` — also calls `patterns.record_resolution` after the existing `_log_settlements_to_audit` for market-close won/lost.
- `tools/rebuild_strategy_audit.py` — one-shot script. Backs up audit → `.bak-YYYYMMDD`, resets rollups, replays every resolved paper trade through `log_settlement`.

**Post-rebuild ground truth (Apr 20 2026):**

| | Paper | settlement_log | rollup |
|---|---|---|---|
| Total | 93 | 93 | 93 |
| vig_stack_series | 54 | 54 | 54 (−$110.62, 29W/54, 54%) |
| live_momentum | 39 | 39 | 39 (+$12.30, 24W/39, 62%) |
| patterns.json total_resolved | — | — | 93 |

Backup: `bot/state/strategy_audit.json.bak-20260421`.

---

### ☑ Session 2 — Active strategy retuning (Apr 20)
**Problem.** Two active dollar leaks visible after the Session-1 settlement-pipeline rebuild (ground-truth recompute from `paper_trades.json`):

- **Vig_stack volatile branch**: KXHIGHDEN/NY/CHI = **−$126.88 on 36 trades, 69% early-cut**. Whitelist families (KXHIGHMIA/AUS/INX) = **+$16.26 on 18 trades**. Volatile-family entry-price buckets: `<92¢` = −$110.79 / 42 trades (deeply negative); `[92-96¢)` = +$0.17 / 12 trades, 11W/1L (92% WR) — the sole breakeven band.
- **Live_momentum tennis**: ATP Challenger = 2W/1L/14 EE, **−$7.80, 82% cut**. WTA = 1W/1L/5 EE, **−$7.00, 71% cut**. Tennis combined = 72% of live_momentum volume for **−$6.20 net**. NBA + NHL alone = **+$19.60 on 10 trades**.
- **Momentum entry dead zone**: [75-80¢) bucket = **−$3.20 across 9 trades**, bracketed by positive [70-75¢) (+$9.30) and [80-85¢) (+$8.40).

**What shipped.**
- `VIG_STACK_WEATHER_MIN_PRICE`: 0.90 → **0.93** (`config.py:408`). 1¢ safety margin above the bottom of the [92-96¢) breakeven bucket. Stable-family carve-out (`VIG_STACK_STABLE_FAMILIES` = MIA/AUS/INX at 0.70) preserved.
- `MOMENTUM_LEADER_MIN`: 0.70 → 0.75 → **0.70 (reverted same day)** (`config.py:69`). The bump was meant to skip the [75-80¢) dead zone but MIN is a floor (`is_leader = prob >= MIN` at `live_watcher.py:863`), so 0.75 *admits* the dead zone while surrendering the positive [70-75¢) bucket (+$9.30). Bucket EV: 0.70 = +$14.50, 0.75 (shipped) = +$5.20, 0.80 = +$8.40. Revert gets back to the highest-EV of the single-threshold options. **TODO:** add an explicit [75-80¢) exclusion in the `is_leader` check (lines 863, 873, 2622, 2624) to capture both positive buckets — theoretical ~+$17.70.
- New `MOMENTUM_DISABLED_SPORTS = {"atp", "atp_challenger", "wta", "wta_challenger"}` (`config.py`, under the MOMENTUM block). Blanket tennis kill — main ATP included precautionarily.
- One-line `can_enter` gate added in `live_watcher._tick_momentum` (~line 972). Blocks new entries for disabled sports; `_check_exit` does not consult this set, so held positions still exit on TP/SL/trailing.

**Why not `SPORT_PROFILES[x]["disabled"] = True`.** Tennis variants (`atp`, `atp_challenger`, `wta`, `wta_challenger`) all alias to the same `tennis` profile dict (`config.py:258-259`). Setting `disabled` there also kills main ATP + tennis. More importantly, the existing `disabled` check fires at scan-spawn time (`live_watcher.py:2566`), which would prevent a watcher from ever spawning for an already-open tennis position — no watcher = no TP/SL/trailing. A `can_enter` gate is the right tool for "block entries, preserve exits."

**Verify.** 2h of scans after restart: zero new entries into `KXHIGHDEN/NY/CHI` (or any non-whitelist HIGH family below 93¢) or into `KXATPMATCH` / `KXWTAMATCH` / `KXATPCHALLENGERMATCH` / `KXWTACHALLENGERMATCH`. No new momentum entries with `price_cents < 75`. Any held tennis positions still exit on normal TP/SL/trailing.

---

### ☑ Session 3 — Live-watcher ESPN restoration (Apr 23)
**Problem (pre-fix).** 3000/3000 recent live ticks had `espn_scores: None`; `wp` defaulted to 0.5 on most. Watcher was running on Kalshi price alone — TP/SL still fired, but momentum/DQS/conviction was degraded (`wp_edge > 8%` gate never passed).

**Root cause.** The ESPN scoreboard fetch was silently failing on three fronts at once: missing User-Agent header (ESPN started 403'ing requests without one), default SSL context using the system store (intermittent cert validation failures), and exceptions getting swallowed by a bare `except:` so nothing ever surfaced in `bot.log`.

**What shipped.**
- `bot/config.py` — `ESPN_BASE` + `ESPN_SPORT_PATHS` constants (nba/mlb/nhl/nfl/ncaab path mapping) hoisted out of `live_watcher.py` and into config so they can be reused/tested.
- `bot/live_watcher.py:_fetch_espn_score` — added `User-Agent: GlintBot/1.0` header; switched to `_ESPN_SSL_CTX = ssl.create_default_context(cafile=certifi.where())`; replaced bare except with structured error logging; added one-shot success log per (ticker, sport) so a working fetch is visible without log spam; added "no events matched query" warning with the sample of returned team names so pre-game-vs-tomorrow mismatches are debuggable.
- Sports without ESPN scoreboard support (tennis variants, UFC) get a one-shot "ESPN not configured for sport=..." warning instead of silently returning empty — confirms the sport is recognized as unsupported, not broken.

**Test results (live, last 500 ticks 2026-04-23):**

| Sport | Total | espn_scores | wp | wp_edge |
|---|---|---|---|---|
| nhl | 68 | 68/68 ✓ | 68/68 | 68/68 |
| nba | 217 | (live games OK; pre-game queries against tomorrow's scoreboard return None as expected) | 217/217 | 217/217 |
| atp_challenger | 215 | 0/215 (expected — tennis has no ESPN scoreboard) | 215/215 | 215/215 |

`bot.log` confirms periodic `ESPN fetch OK` lines for in-progress NHL and NBA games (e.g., `colorado avalanche @ los angeles kings`, `denver nuggets @ minnesota timberwolves`). Pre-game NBA tickers (`KXNBAGAME-26APR24*`) get the new "events had N, none matched query" warning — surfacing the explainable miss instead of silent None.

**Verify.** `grep espn_scores bot/state/live_ticks.jsonl | tail -100` — non-null for live NHL/NBA games. `grep "ESPN fetch OK\|ESPN not configured\|none matched query" bot/logs/bot.log | tail` — see the three success/info patterns above. `python3 -c "import json; ticks=[json.loads(l) for l in open('bot/state/live_ticks.jsonl')][-500:]; print(sum(1 for t in ticks if t.get('wp') is not None), '/', len(ticks))"` — should be 100%.

---

### ☑ Session 4 — Scheduler + bot_state revival (Apr 23)
**Problem (pre-fix).** Cron-style subsystems dead or drifting, five separate symptoms that turned out to be three distinct bugs plus one doc error plus one cosmetic stale counter:
- `last_morning_briefing`: Apr 12 (11d stale on Apr 23).
- `last_nightly_summary`: Apr 19 (4d stale).
- `last_odds_api_request`: Apr 5 (18d stale).
- `crypto_trades_today: 2902` despite `CRYPTO_ENABLED = False`.
- `total_pnl: 0` — never written.

**Diagnosis.**
- The Apr 20 plan assumed `GlintBot.start()` schedules `scheduler.run_forever` in a task group. **That function does not exist.** The scheduler is inline-polled from the main scan loop at `main.py:1015` via `check_scheduled_events(self)`. Correcting the architectural premise was step zero.
- `scheduler.py` hour gate was `current_hour == MORNING_BRIEFING_HOUR`. Main loop sleeps 2-30 min. If the bot was restarting, crashed, or just polled during hour 7 then next hour 9, it saw the `==` fail and skipped the whole day. Hour 21 reconcile reliably fired because its window is an hour wide and the loop always hits it; hour 0 nightly + hour 8 morning were the narrow windows that kept getting missed. `==` is the bug; `>=` with a same-day fire-once flag is the fix.
- `_send_nightly_summary` computed `total_pnl` from `compute_daily_summary()` but only used it in the Telegram message body. Never persisted back to `bot_state.json`.
- Latent write-ordering bug discovered while fixing the above: outer `state` dict in `check_scheduled_events` was loaded once, then inner helpers mutated state on disk, then outer code wrote its stale copy back — clobbering `total_pnl`, `last_known_kalshi_balance`, `last_balance_reconcile`.
- `crypto_trades_today: 2902` is a stale counter frozen at the last pre-Mar 1 `CRYPTO_ENABLED = True` session. The loop that increments it (`_crypto_scan_loop`) isn't even started now. Cosmetic, not a live bug.
- `last_odds_api_request` is **not** dead code. `_increment_odds_api_count` at `odds_scraper.py:1108` writes it, but only from `_odds_api_fallback` — the paid-tier Odds API last-resort path hit after DK / FD / ESPN / TheRundown all fail. A stale timestamp just means the paid fallback hasn't been hit in 18 days, not that odds data stopped flowing. Doc error in the Apr 20 audit.

**What shipped.**
- `bot/scheduler.py` — Morning gate `== 8` → `8 <= hour < 20` (`MORNING_BRIEFING_CUTOFF_HOUR = 20` prevents "morning" briefing firing at 11pm after a late restart). Nightly gate gained a catch-up clause: `(hour == 0 and last != today) or (last and last < yesterday)` — if we missed a day, fire at any hour. Reconcile left at `== 21` (hour-long window, always fires). Three `except Exception as e: logger.error(...)` upgraded to `logger.exception(...)` so tracebacks surface in `bot.log`.
- `bot/scheduler.py` — `_send_nightly_summary` now persists `total_pnl` + `today_pnl` to `bot_state.json` via `_load_bot_state` → mutate → `_save_bot_state`, matching Session 4's plan.
- `bot/scheduler.py` — Write-ordering fix: after `_send_nightly_summary` and `_reconcile_daily_balance` return, `check_scheduled_events` now `_load_bot_state()` fresh before stamping `last_nightly_summary` / `last_balance_reconcile_date`. Outer no longer clobbers inner writes.
- `bot/main.py` — Daily-rollover block at `main.py:1037` now zeroes `crypto_trades_today` alongside `scans_today` on date change. Still needs one-time hand-edit of `bot_state.json` to kill the existing 2902 (see coordination item below).
- `bot/main.py` — New startup drift check in `GlintBot.start()` after the heartbeat watchdog: reads `last_morning_briefing` and `last_nightly_summary`, logs `WARNING` if either is >2 days stale. Surfaces silent scheduler failures next time instead of discovering them 11 days later.
- `bot/odds_scraper.py` — Multi-line docstring on `_increment_odds_api_count` documenting that this is the paid-fallback liveness signal, not a general odds-flow signal.
- `tests/test_scheduler.py` (new, 14 tests across 4 classes) — Mock-clock via `datetime` subclass. Covers: fires-at-8am-if-not-yet-today, catch-up-at-9:30am, no-refire-same-day, no-fire-before-8am, no-fire-after-8pm-cutoff, next-day-rollover, midnight-nightly, catch-up-missed-day, no-false-catch-up-if-fired-yesterday, 21:00-reconcile, outside-21-does-not-fire, no-refire-same-day-reconcile, total-pnl-persists-to-bot-state.

**Test results.** `python3 -m pytest tests/test_scheduler.py -v` → 14 passed. The broader suite has 7 unrelated pre-existing failures (stale Apr 18 pin on `WEATHER_MIN_PRICE`, live_watcher `_trailing_active` attribute missing in two session-summary tests, one position-limit test that predates Session 2) plus 2 `test_watchdog_*` failures that are pre-existing (the watchdog alert path has `# Watchdog alert silenced` at `main.py:313` — alert is set then immediately cleared without sending, the tests expect `send_message` to be called). None are caused by Session 4 changes.

**Coordination with user (pending).** Stop bot → hand-edit `bot/state/bot_state.json` to set `crypto_trades_today: 0` → restart. The daily-rollover fix prevents regrowth but can't retroactively zero the stale value.

**What Session 4 was NOT.** Did not restructure the scheduler into a dedicated 60s asyncio task — the inline-polled architecture works once the gate is correct. Did not touch Session 3 ESPN restoration. Did not change `NIGHTLY_SUMMARY_HOUR` from 0 to 23 (arguably better semantics, but behavior preserved).

**Verify (live, over next 24h).** After restart:
1. `bot.log` shows `"Scheduler drift: morning briefing 11 days stale..."` and `"Scheduler drift: nightly summary 4 days stale..."` warnings at startup.
2. Next 8am ET → Telegram morning briefing arrives. `bot_state.json.last_morning_briefing` advances to today.
3. Next midnight ET → Telegram nightly summary arrives. `bot_state.json.last_nightly_summary` advances. `bot_state.json.total_pnl` becomes a non-zero number matching `compute_daily_summary()["total_pnl"]` (expected ≈ −$98 per post-Apr-20 ground truth).
4. Day after: `crypto_trades_today` stays 0 after date rollover.

---

### ☑ Session 5 — State hygiene (Apr 23)
**Problem (pre-fix).** Apr 20 audit cited 216MB of `bot/state/` with ~150MB bloat. Reality check at Apr 23 22:48 was 117MB (paper rotation already trimmed half), of which **108MB was `live_ticks.jsonl` alone** (148,851 lines, Apr 9 → Apr 24, growing unbounded). Other zombies: 236/448 (53%) of `clv.json` records were for disabled strategies; six confirmed-stale files (`odds_snapshots.json`, `price_cache.json`, `watchlist.json`, `paper_trades_archive.json`, two Apr 18 `.bak` leftovers); `bot.lock` mtime frozen since startup so it couldn't act as a liveness signal; CLAUDE.md state-files table conflated `trade_history.json` (order log) with `paper_trades.json` (paper resolution log). No trading impact — purely cleanup.

**What shipped.**
- `bot/clv.py` — new `_active_strategies()` helper (returns `set(ACTIVE_STRATEGIES) | {"live_momentum"}`); `_load()` filters out any record whose `opp_type` isn't in that set. Single read site (`record_clv_entry`, `check_clv_settlements`, `get_clv_report` all go through it), so the next `_save` automatically drops disabled-strategy noise from disk.
- `tools/purge_clv_disabled.py` — one-shot. Asserts `bot.lock` is gone or PID is dead, requires `--yes`, backs up `clv.json` → `clv.json.bak-YYYYMMDD`, drops 236 records (kept 212 active: live_momentum 110, vig_stack_series 101, vig_stack_futures 1).
- `tools/clean_stale_state.py` — one-shot. Same safety gates. Deletes the six stale files (~210KB total). Explicitly preserves `strategy_audit.json.bak-20260421` (Session 1 backup, still cited above).
- `bot/scheduler.py` — new `_rotate_live_ticks(today_str)` helper + new gate in `check_scheduled_events` mirroring the Session 4 pattern (hour 0 ET + same-day flag `last_ticks_rotation` + catch-up clause for missed days). Rotation: rename `live_ticks.jsonl` → `state/archive/live_ticks-YYYY-MM-DD.jsonl`, gzip via `shutil.copyfileobj`, unlink the .jsonl. Race-safe because `live_watcher._log_tick` reopens the file every write — a tick that fires mid-rotation lands in a fresh file at the original path. Collisions get `-2`, `-3` suffixes. Skip if file < 1KB.
- `bot/main.py` — single line `LOCK_FILE.touch()` after `_save_bot_state(state)` in the heartbeat block (around line 1061). Lock mtime now advances every scan, becoming a real liveness signal. No code currently reads the mtime (Explore-agent verified), so this is purely additive — startup PID checks at `_acquire_lock` are unchanged.
- `tests/test_scheduler.py` — new `TestLiveTicksRotation` class, 5 cases: fires-at-midnight-and-archives, skip-if-file-too-small, no-refire-same-day, catch-up-if-missed-a-day, collision-appends-suffix. Uses a tmp-path fixture that monkeypatches `live_watcher.TICK_LOG_FILE`.
- `CLAUDE.md` — state-files table updated: `trade_history.json` and `paper_trades.json` rows now distinguish order log vs resolution log; `bot.lock`, `bot_state.json`, `clv.json` rows annotated with Session 5 changes; `archive/live_ticks-*.jsonl.gz` row added; deleted-file rows (`odds_snapshots`, `price_cache`, `watchlist`, `paper_trades_archive`) removed. Gotcha #6 extended with two new bot.lock failure signatures.

**Test results.** `python3 -m pytest tests/test_scheduler.py -v` → 19 passed (14 existing + 5 new rotation cases). The broader suite still has the same 7+2 pre-existing failures noted in Session 4 — none are caused by Session 5 changes.

**Coordination with user (deploy).** Stop bot → `python3 tools/purge_clv_disabled.py --yes` → `python3 tools/clean_stale_state.py --yes` → restart. Code-path changes (clv filter, scheduler rotation, lock touch) deploy via the restart itself.

**Verify (live).**
1. `du -sh bot/state/` — drops by ~210KB immediately after one-shots; drops by ~108MB at next midnight ET (rotation). Steady-state should be ~10MB after first rotation.
2. `python3 -c "import json; clv=json.load(open('bot/state/clv.json')); print(sorted(set(r['opp_type'] for r in clv)))"` — only active strategies present.
3. `ls bot/state/*.bak*` — only `strategy_audit.json.bak-20260421` remains.
4. `stat -f "%Sm" bot/state/bot.lock` — sample twice 60s apart, mtime advances.
5. After next midnight ET (or temporarily set `last_ticks_rotation` to a stale date in `bot_state.json` and trigger): `ls bot/state/archive/` shows `live_ticks-2026-04-24.jsonl.gz`; fresh `live_ticks.jsonl` is small; `gzip -dc bot/state/archive/live_ticks-2026-04-24.jsonl.gz | wc -l` matches the pre-rotation line count.
6. Telegram `CLV` returns a report (no crash on empty disabled-strategy bins).

---

### ☑ Session 6 — Closed-loop data collection (Apr 24)
**Problem (pre-fix).** `trade_history.json` shows what fired, but not *what almost fired and was killed by which gate, or what would have happened if we'd taken it anyway*. Without that, every gate threshold is folklore: `MIN_RELATIVE_EDGE = 0.15`, the 4h cooldown, `STRATEGY_BUDGETS` 60/20/20, the 3¢ kill switch, the new Filter F 0.93 floor — all guesses until rejected trades have outcome-attached counterfactuals. The bot was opaque to its own decisions.

**What shipped.**
- `bot/decisions.py` (new, 63 lines) — `log_decision(ticker, opp_type, edge, gates, decision, reason, extra=None)`. Atomic append to `bot/state/decisions.jsonl` under module-level `threading.Lock`. Wrapped in try/except — never raises, so audit-log failure can't block a trade. Single write site; no reader API (analysis tools read the file directly).
- `bot/scanner.py` — `scan_vig_stack_series` instrumented at all 7 gate sites (`low_liquidity`, `no_vig`, `forecast_in_bucket`, `price_floor`, `edge_below_threshold`, `self_check`, plus accept-path). New module-level `_VIG_STACK_GATES` ordered list + `_vig_stack_gate_fingerprint(reason)` helper that returns `{gate: True}` for upstream-passed, `{reason: False}` for the firing gate, omits downstream-not-yet-checked. Local `rejected_opps` list collected for downstream CF emission. Existing `_telem` counters preserved.
- `bot/scanner.py` (end of `scan_vig_stack_series`) — top-5 reject CF emission. Sort `rejected_opps` by `-edge`, take top-5, call `clv.record_counterfactual_skip(opp, opp["skip_reason"], scan_id)`. Wrapped in nested try/except so CF-emit failure never propagates.
- `bot/clv.py` — new `record_counterfactual_skip(opp, gate, scan_id)`. Idempotent on `trade_id = f"CF-{scan_id}-{ticker}"`. Records carry `status="counterfactual_open"` + `skipped_by_gate` field + `contracts=0` + `paper=False`. Resilient to missing fields (falls back `price_cents` → `yes_ask`; skips silently if no usable price). `check_clv_settlements()` extended: settles status `"open"` and `"counterfactual_open"` alike, but writes `"counterfactual_settled"` (NOT `"settled"`) for CFs and excludes them from the return list. So `get_clv_report()` (filters `status == "settled"`) stays clean of CF noise.
- `bot/executor.py` — instrumented all 7 `_check_position_limits` gates (position_cap, duplicate, same_game, cooldown, daily_loss, strategy_budget, total_exposure) and all 4 `_verify_edge_still_exists` gates (market_data, yes_ask, price_moved, edge_evaporated). Module-level `_pos_gate_fingerprint` / `_edge_gate_fingerprint` + `_log_position_reject` / `_log_edge_reject` helpers. Accept-path log right after self-check passes (before order placement) carries full `gates: all True` + `extras={contracts, price_cents, cost_dollars, paper, side}`.
- `bot/live_watcher.py` — instrumented `_tick_momentum` with a **dampener** (`self._last_decision: tuple[str,str] | None`). New `_log_decision_dampened(decision, reason, gates, edge, extra)` method emits only when `(decision, reason)` differs from the last entry. Without this, a flat-market live ticker would emit ~6 reject logs/sec → 50k records/day per match. With it, one record per state-transition. Gates instrumented: `can_enter` (sport_disabled / max_entries / cooldown / position_open), `dip_too_big`, `variance_quality`, `dqs_fail`, plus accept paths (`dip_buy`, `conviction`).
- `bot/scheduler.py` — extracted shared `_rotate_jsonl(source, prefix, today_str)` helper covering rename → gzip → unlink with collision-suffix loop and size-saved INFO log. `_rotate_live_ticks` and new `_rotate_decisions_log` are now 2-line wrappers. New gate in `check_scheduled_events` mirrors live_ticks: hour 0 + same-day flag `last_decisions_rotation` + catch-up clause for missed days.
- `tools/cohort_report.py` (new) — reads `decisions.jsonl` + last N days of gzipped archives, joins to CF records in `clv.json` (status `counterfactual_*`). Per-strategy Markdown table: gate, invocations, rejects, reject %, mean reject edge, CF settled count, Σ CF clv_relative, mean CF clv_relative. **Mis-tuning candidates section** flags gates with reject rate ≥ 50% AND positive Σ CF clv_relative across ≥ 5 settled CFs — those are the gates surrendering alpha.
- `tests/test_decisions.py` (new, 8 cases) — schema integrity (5: required fields, None-edge, extra round-trip, extra omitted when None, edge rounded to 4dp); atomic append (2: 200 concurrent writes from 20 threads, state-dir creation); never-raises (1: disk failure swallowed).
- `tests/test_clv.py` (new, 9 cases) — CF recording (5: schema, idempotency, distinct scan IDs, missing-price skip, yes_ask fallback); active-strategy filter (2: CF passes, disabled-strategy CF dropped); settlement (2: CF → counterfactual_settled, real → settled); report exclusion (1: CF settled excluded from `get_clv_report`).
- `tests/test_scheduler.py` — new `TestDecisionsRotation` class (5 cases) mirroring Session-5 `TestLiveTicksRotation`: fires-at-midnight, skip-if-too-small, no-refire, catch-up-missed-day, collision-suffix.

**Test results.** `python3 -m pytest tests/test_decisions.py tests/test_clv.py tests/test_scheduler.py::TestDecisionsRotation -v` → 23 passed. Broader suite unchanged from Session 4/5 baseline (same 7+2 pre-existing failures).

**Coordination with user (deploy).** Stop bot → restart. Code-path changes deploy via the restart itself; no one-shot scripts needed for this session. `decisions.jsonl` is created lazily on first write.

**Verify (live, post-restart).**
1. `tail -20 bot/state/decisions.jsonl` — within 1 hour, real records flow. Each line has `ts, ticker, opp_type, edge, gates, decision, reason`.
2. `python3 -c "import json; recs=[json.loads(l) for l in open('bot/state/decisions.jsonl')]; from collections import Counter; print(Counter((r['opp_type'], r['decision']) for r in recs))"` — sanity-check spread across opp_types and accept/reject mix.
3. `python3 -c "import json; clv=json.load(open('bot/state/clv.json')); cf=[r for r in clv if r.get('status','').startswith('counterfactual')]; print(f'{len(cf)} CF records, {sum(1 for r in cf if r[\"status\"]==\"counterfactual_open\")} pending settlement')"` — CF records exist within first 1-2 scan cycles.
4. Dampener works: `tail -100 bot/state/decisions.jsonl | grep <live_ticker>` shows ≤1 record per state-transition (not per tick).
5. Next midnight ET → `ls bot/state/archive/` shows `decisions-2026-04-25.jsonl.gz`; fresh `decisions.jsonl` is small.
6. After 7 days: `python3 tools/cohort_report.py --days 7` → first real cohort report. Look for the **Mis-tuning candidates** section — those gates become Session 7 retuning targets.

---

### ☑ Session 7 — Decision-log observability gaps (Apr 24, planned)
**Problem (post-Session-6 audit, first 24h of decisions data).** Two gaps surfaced once the Session-6 audit log went live:

1. **Live-momentum decisions are info-poor.** 12/15 records carry `edge=null` because `_tick_momentum` has no scalar edge concept — momentum trades are pure price-action with no model fair value. Reject *rate* per gate (sport_disabled, dqs_fail, dip_too_big, variance_quality) is queryable today; "edge surrendered by gate" is not. Live-side gate retuning (DQS threshold, dip floor, conviction zone, leader_min) is blind without it.
2. **Lock-touch cadence is per-scan, not per-second.** `bot/main.py:1061` touches `bot.lock` only at scan boundaries (`scan_interval` 2-30 min). Session-5 Gotcha #6 treats `bot.lock` mtime > 15 min stale + alive PID as "scan loop wedged" — a healthy bot in idle 30-min cycle can falsely trip this. Lock observed at 19 min stale during the Session-6 wedge-fix verification, with a healthy event loop and live ticks flowing every 10s.

**Plan.**
- **Edge proxy for live momentum.** Wire `wp_edge` (already computed each tick — see `live_watcher._tick_momentum`) into `_log_decision_dampened()` as the `edge` arg. Also persist a small `extra` dict (`{wp, kalshi_price, dip_cents, dqs}`) so the cohort report can join on something analytically useful. Same dampener — only emits on `(decision, reason)` change, so volume stays bounded.
- **Heartbeat lock-touch.** Either (a) extend the existing 60s `_live_scan_loop` to call `LOCK_FILE.touch()` at the top, or (b) add a dedicated 30s heartbeat task in `GlintBot.start()`. Brings worst-case stale gap from 30 min → ≤60s. Update Gotcha #6 + state-files table accordingly.

**Out of scope.** Cohort-report changes (works on rate-only analysis until live-side edge data accumulates); decision-history schema migration; back-filling missing-edge records (forward-only).

**Verify.**
1. `tail bot/state/decisions.jsonl | grep live_momentum` — `edge` non-null for new entries; `extra` carries wp/dqs/dip context.
2. `stat -f "%Sm" bot/state/bot.lock` — sample twice 60s apart, mtime advances regardless of scan_interval.
3. `python3 -c "import json; recs=[json.loads(l) for l in open('bot/state/decisions.jsonl')]; lm=[r for r in recs if r['opp_type']=='live_momentum']; print(f'edge coverage: {sum(1 for r in lm if r[\"edge\"] is not None)}/{len(lm)}')"` — coverage > 80% within 24h.

---

### ☑ Session 8 — Stratified CF sampling (Apr 24, shipped)
**Problem (Session-6 design flaw, surfaced in first 24h of data).** CF emission selects "top-5 highest-edge rejects per scan" globally ([scanner.py:891-894](hustle-agent/bot/scanner.py:891)). This systematically excludes the gates that absorb the most rejects, because they reject *low-edge* opps by definition:

| Gate | Rejects | CFs | Why |
|---|---|---|---|
| vig_stack_series `forecast_in_bucket` | 143 (48%) | **0** | Mixed-edge rejects, lose top-5 race |
| vig_stack_futures `edge_below_threshold` | 130 (79%) | **0** | By construction, edges < 0.02 |
| vig_stack_series `edge_below_threshold` | 114 (38%) | **0** | Same — sub-threshold edges |
| vig_stack_series `non_stable_below_weather_floor` | 19 (6%) | 19 | Real economic edges 0.04-0.20 → wins top-5 every scan |

The gates we most need to retune (`edge_below_threshold`, `forecast_in_bucket`) have zero outcome attribution. The cohort report Session 6 was built for will have a black hole at exactly the gates that matter. The Session-6 verification step ("look for the **Mis-tuning candidates** section") cannot fire on a gate that has no CF settled records.

**Plan.**
- **Stratify CF emission by gate.** In `scanner.scan_vig_stack_series` (and any future scan-time CF site), replace the single `top_rejects = sorted(...)[:5]` with two-stage sampling:
  1. Per-gate top-K: for each gate that fired ≥1 reject, take its top-3 highest-edge rejects (still ≥3¢ entry).
  2. Global top-K floor: union with the global top-5 by edge so the very-best opportunities are never starved by the per-gate cap.
  3. Dedup by ticker. Cap total at 15/scan as a runaway guard.
- **Volume math.** ~6 vig_stack gates × 3/gate = 18 max per scan; with 30-min idle scan_interval that's ≤900/day, well under the original Session-6 budget of 1500/day. Active-scan windows (2-min interval) push to ~13k/day worst case — add the 15/scan cap to bound this.
- **No schema change.** CF records already carry `skipped_by_gate`. Cohort report joins on it natively.
- **Live momentum stays out of CF emission for now** (see Session 7 — needs `wp_edge` proxy first, separate work stream).

**Out of scope.** CF settlement maturity (need calendar time, not code); cohort-report changes (works as-is once CF coverage is real); Session 7 work (live-momentum edge proxy + heartbeat lock-touch).

**Verify.**
1. After first scan post-deploy: `python3 -c "import json; clv=json.load(open('bot/state/clv.json')); from collections import Counter; cf=[r for r in clv if r.get('status','').startswith('counterfactual')]; print(Counter(r['skipped_by_gate'] for r in cf))"` — every gate that rejected ≥1 opp this scan has ≥1 CF.
2. After 24h: `forecast_in_bucket`, `edge_below_threshold` both have non-zero CF counts (the previous black holes).
3. After 7 days: cohort report's mis-tuning candidates section flags real gates, not just `non_stable_below_weather_floor`.
4. CF growth ≤900/day idle, ≤13k/day active. `wc -l bot/state/clv.json && du -h bot/state/clv.json` — file stays <2 MB.

#### Session 8 Part 2 — launchd supervision fix (Apr 24, shipped)

**Problem.** While verifying the Session-8 restart, launchd-supervised bot crash-looped every 5s with no visible stack. Root cause: `run_bot.sh:5` hardcoded `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/...Python` but `bot/daily_log.py:26` uses PEP 604 union syntax (`def _load_json(path: Path) -> list | dict:`) which requires Python 3.10+. The wrapper had been silently broken since that syntax landed, and the bot was actually running out-of-supervision via ad-hoc `nohup python3 -m bot.main` instead of under launchd. Telegram `STOP` would unload launchd (no-op, since it was already quietly disabled) but the nohup'd process was the real one.

Secondary finding: launchd service was in the user-domain *disabled* database (independent from plist `RunAtLoad`), so `launchctl load` returned `Input/output error 5` until cleared via `launchctl enable gui/$UID/com.hustle-agent.bot` (no sudo required — user domain only).

**What shipped.**
- `run_bot.sh` — replaced hardcoded 3.9 binary path with `PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"` at the top of the script. Chosen over `/usr/bin/env python3` because launchd's minimal `PATH=/usr/bin:/bin:/usr/sbin:/sbin` does not include `/Library/Frameworks/.../3.14/bin`, and over modifying the plist `EnvironmentVariables` because a single explicit variable in the wrapper is the smallest surface to maintain.
- User-domain launchd re-enabled (`launchctl enable gui/$UID/com.hustle-agent.bot`) and plist reloaded (`launchctl load ~/Library/LaunchAgents/com.hustle-agent.bot.plist`). Stale `bot/state/bot.lock` from the nohup session deleted before load.

**Maintenance note.** When upgrading past Python 3.14, edit `PYTHON_BIN` in `run_bot.sh`. That's the single point of change.

**Verify.**
1. `ps aux | grep bot.main | grep -v grep` — exactly one PID, parent is launchd (`ppid=1`), binary path contains `Versions/3.14`.
2. `tail bot/logs/watchdog.log` — no new `Bot exited (code 0), restarting in 5s...` lines after the fix (the crash loop fingerprint was a consecutive run of those with <10s deltas).
3. `tail bot/logs/bot.log` — shows normal startup sequence: `Telegram connected — bot is live`, `SCAN CYCLE — …`, scanner loops firing. Observed on first launchd-supervised boot post-fix.
4. Telegram `STOP` now cleanly unloads launchd + kills the bot; `START` (or reboot / `launchctl load`) brings it back under supervision.

---

### ☑ Session 9 — MFE/MAE per position (Apr 24, shipped)

**Problem.** `clv.json` records entry, settlement, and final CLV. It does NOT record what the price did *between* entry and settlement. Two trades can have identical CLV but very different lived experiences: one drifted straight to close, the other spiked +30¢ then unwound. The first vindicates conviction sizing; the second is a missed-exit signal. Without max-favorable / max-adverse excursion, we can't tell whether `MAX_HOLD_HOURS`, the 3¢ kill switch, or the dynamic exit ladder are leaving alpha on the table.

**Shipped.**
- `bot/tracker.py:update_positions` now ratchets `mfe_cents` / `mae_cents` / `mfe_at` / `mae_at` / `ticks_observed` on every price observation. Side-aware: YES tracks `yes_bid − entry`, NO tracks `no_bid − entry`. Lazy-init so pre-Session-9 open positions upgrade without crashing.
- `bot/clv.py:check_clv_settlements` builds an `order_id → position` map once per call and copies the five excursion fields into each real-trade settlement record. Counterfactuals untouched.
- `tools/excursion_report.py` groups settled clv records by `opp_type`, computes median(MFE − exit) per strategy, flags medians > 5¢ with ⚠️. Skips records missing `mfe_cents`.
- Tests: `tests/test_tracker.py` (10 tests) covers init, side-aware ratchet, monotonic behavior, timestamp semantics, and settlement propagation.

**Out of scope.** Tick-level price path storage (too large); intra-trade re-entry; live-momentum tick logging (already in `live_ticks.jsonl`, just not joined to position outcomes).

**Verify.**
1. After first paper trade settles: corresponding `clv.json` record has non-null `mfe_cents`, `mae_cents`, both within [0, 100].
2. After 7 days: `python3 tools/excursion_report.py` produces per-strategy MFE-vs-exit gap. Gaps >5¢ on median are exit-logic candidates.
3. Position records during open: tail `bot/state/positions.json` and watch `mfe_cents` ratchet up monotonically across loops.

---

### ☑ Session 10 — Gate-context enrichment in `extra` (Apr 24, done)

**Problem.** `decisions.jsonl` records *which* gate fired but not *by how much*. `forecast_in_bucket` reject — was the forecast 1¢ outside the bucket or 30¢? `edge_below_threshold` reject — was edge 0.149 (a hair under MIN_RELATIVE_EDGE=0.15) or 0.02? This distinction is everything for retuning. A gate that rejects "just barely" 80% of the time is a tuning candidate; a gate that rejects "by a mile" 80% of the time is doing its job.

**Plan.**
- Each `decisions.log_decision` call site populates `extra={...}` with gate-relevant diagnostics:
  - `forecast_in_bucket` → `{"forecast": 0.42, "bucket_lo": 0.50, "bucket_hi": 0.65, "distance": 0.08}`
  - `edge_below_threshold` → `{"edge": 0.12, "threshold": 0.15, "vig": 0.04, "time_to_settle_hr": 6.5}`
  - `low_liquidity` → `{"yes_volume": 23, "no_volume": 31, "min_required": 50}`
  - `cooldown` → `{"last_trade_age_min": 145, "cooldown_min": 240}`
  - `position_cap` → `{"current_positions": 12, "cap": 12, "exposure_pct": 0.94}`
- Update `tools/cohort_report.py` to render distance-from-threshold histograms per gate (replacing the current binary reject-rate).

**Out of scope.** Schema migration of historical records (pre-Session-10 records simply won't have `extra` for these gates — that's fine, cohort_report falls back gracefully).

**Verify.**
1. After deploy + 1 scan: `tail -50 bot/state/decisions.jsonl | jq 'select(.decision=="reject") | .extra'` — every reject has populated `extra` for its gate type.
2. After 7 days: cohort report shows distance-from-threshold distribution per gate. Gates with median distance < 5% of threshold value are tuning candidates.
3. No record-size blowup. `wc -c bot/state/decisions.jsonl` grows ≤30% vs pre-Session-10 baseline (each `extra` is ~80-150 bytes).

---

### ☑ Session 11 — Fair-value calibration loop (Apr 25, done)

**Problem.** Every edge calc is `(fair_value - market_price) / market_price`. The whole bot is one big bet on `fair_value` being right, but pre-Session-11 we had no record of whether predictions correlated with closing prices. A scanner that consistently overestimates fair value by 5¢ on `vig_stack_series` is hemorrhaging money on every trade — and CLV alone won't catch it (CLV measures execution, not prediction).

**Done.**
- `bot/calibration.py` (new) — `record_prediction(ticker, opp_type, predicted_fair_cents, market_price_cents, scan_id, recorded_at=None)` and `update_prediction_close(ticker, recorded_at, closing_yes_price)`. Module-level `threading.Lock`, atomic JSONL append, never-raises wrapper. Idempotent on `(scan_id, ticker)` via cheap substring scan. Skips silently when `predicted_fair_cents` is None/0 (live_momentum has no usable fair value — Session 7 limitation).
- `bot/clv.py` — `record_clv_entry()` gained optional `scan_id`; both it and `record_counterfactual_skip` now call `record_prediction` after `_save`. `check_clv_settlements` calls `update_prediction_close(ticker, recorded_at, closing_yes)` for every newly-settled record (real + CF).
- `bot/executor.py` — `record_clv_entry()` call now passes `scan_id=opportunity.get("scan_id")`. (Currently None since scanner does not stamp scan_id on opps; the `scan_id or f"trade-{trade_id}"` fallback inside `record_clv_entry` produces a unique idempotency key per real trade.)
- `bot/scheduler.py` — added `_rotate_predictions_log()` (2-line wrapper around `_rotate_jsonl`) + parallel midnight-ET rotation gate using `last_predictions_rotation` flag in `bot_state.json`. Reuses Session-6 `_rotate_jsonl` helper as-is.
- `tools/calibration_report.py` (new, gitignored) — reads `predictions.jsonl` + last 7 daily archives, groups by `opp_type`, emits per-strategy mean-bias / stdev / per-bucket hit-rate / Brier score Markdown. Flags bucket [80,90) <70% YES and buckets [0,10)/[10,20) >30% YES (sign-error / systematic miscalibration).
- `tests/test_calibration.py` (new) — 25 tests: schema, atomic append (20 threads × 10 writes), never-raises, idempotency on (scan_id, ticker), settlement matching ±60s window, missing-file/bad-date graceful handling, Brier handcraft (5 records → 0.082), bucket boundary semantics.
- `tests/test_scheduler.py::TestPredictionsRotation` — 6 tests mirroring `TestDecisionsRotation`: midnight-fire, <1KB skip, no-refire-same-day, catch-up after missed day, missing-archive-dir auto-create, collision-suffix.
- `tests/conftest.py` — added `_isolate_predictions_log` autouse fixture mirroring `_isolate_decisions_log` to keep test runs from polluting the live `predictions.jsonl`.

**Out of scope.** Auto-retuning of fair-value formulas (Session 12+); pre-Session-11 historical predictions (settlement records have entry but not the prediction-at-time-of-decision in a structured way); live_momentum prediction coverage (needs Session 7 follow-up to expose a `wp_edge`-derived fair value).

**Verify.**
1. After first scan: `bot/state/predictions.jsonl` has one row per opp the scanner evaluated (accepted + CF). Schema: `{ts, scan_id, ticker, opp_type, predicted_fair_cents, market_price_cents, closing_yes_price: null}`.
2. After 7 days of settlements: `python3 tools/calibration_report.py` produces per-strategy calibration table. Brier score reported.
3. Sanity check: `vig_stack_series` predicted bucket [80,90) should resolve YES ~85% of the time. If it resolves <70% or >95%, fair-value formula needs a tuning pass.

---

## Apr 25+ Pivot-Enabling Instrumentation Arc (Sessions 12–15)

> **Sessions 12-15 shipped 2026-04-25 — pivot-enabling instrumentation arc complete.** Session 15 verification deferred until `PAPER_MODE=False`; the live-mode verification checks fire when going live, not on the day Session 15 ships.

Sessions 6–11 instrumented the bot to *tune itself* — every accept/reject, every prediction-vs-actual, every position's MFE/MAE. That answers "is the bot we have well-calibrated?" but **not** "are the strategies we're running the right ones to be running?" The current data only records what happens inside markets we already chose to look at, framed as opportunities for strategies we already wrote.

This arc instruments the *escape route from our current strategy frame*. Sessions sequenced so the chat reading the reports gets to "I can tell you what we should be doing differently" fastest:

```
Session 12: Universe log              (1 session — pure data accumulation)
            ↓
Session 13: Hypothetical strategy     (2-3 sub-sessions — biggest lift)
            framework
            ↓
Session 14: Regime tags               (1 session — taxonomy informed by 13's back-tests)
            ↓
Session 15: Live order microstructure (1 session — defer until PAPER_MODE=False)
```

**Why this order, not regime-first:** regime tags are cheap and immediately useful, but they make us better *inside* the existing frame. Universe + framework is what lets us evaluate alternatives. Defining regime taxonomy a priori is also error-prone — better to ship 12 and 13 first, run back-tests, then define regime axes based on which slices actually moved strategy outcomes (Session 13's evidence informs Session 14's taxonomy).

---

### ☑ Session 12 — Universe log (Apr 25, shipped)

**Problem.** Every existing collection point (`decisions.jsonl`, CFs in `clv.json`, `predictions.jsonl`) only fires on opportunities a strategy scanner already considered. Kalshi has thousands of active markets at any time; we scan a curated handful. We have **zero signal** on what's happening in the rest — political markets, single-event futures, contract types our scanners don't template. Without a record of the full universe, we can't ask "what alpha is hiding in markets we don't even look at?" The current reports answer "are our strategies well-tuned?"; the universe log answers "are our strategies the right ones to be running?"

**Plan.**
- **New module `bot/universe.py`** — `snapshot_universe(scan_id) -> int` pulls every active Kalshi market via the existing `agent.kalshi_client.list_markets()` paginated cursor, writes one JSONL row per market to `bot/state/universe.jsonl`. Schema:
  ```
  {ts, scan_id, ticker, series_ticker, event_ticker, status, close_ts,
   yes_ask, yes_bid, no_ask, no_bid, volume_24h, open_interest,
   scanned_by: [list of strategy names that DID look at this market this scan]}
  ```
  `scanned_by` is the join key. Empty list = ignored by every active strategy. Non-empty = part of an existing strategy's flow.
- **`bot/main.py:_main_loop`** — one new line: `snapshot_universe(scan_id)` immediately before each `scan_cycle`. Each scanner appends its name to `scanned_by` for every ticker it reads (~3 lines per scanner, threaded through `scan_cycle`).
- **Daily rotation** in `bot/scheduler.py` — mirrors `_rotate_decisions_log`, archive to `state/archive/universe-YYYY-MM-DD.jsonl.gz`. Universe is the largest of any log so rotation matters most here.
- **New `tools/universe_report.py`** (local-only, .gitignored). Reads current + last 7 archives. Per (series_ticker prefix, event_type): total markets, % we scanned, $volume distribution of ignored markets, price-action of ignored markets (mean spread, settlement-flip rate). Markdown to stdout. **Sanity flag:** ignored markets with high volume + wide spreads are candidate strategy territory.

**Out of scope.** Strategy evaluation against universe (Session 13). Regime tagging on universe rows (Session 14). Live order microstructure (Session 15). `scanned_by` enrichment for the live-watcher subsystem (live_momentum operates per-game, not per-scan; revisit when 13 ships).

**Verify.**
1. After first scan post-deploy: `wc -l bot/state/universe.jsonl` shows ~1000-3000 rows (Kalshi's typical active-market count at any moment).
2. After 24h: `python3 -c "import json; r=[json.loads(l) for l in open('bot/state/universe.jsonl')]; print(f'total: {len(r)}, unscanned: {sum(1 for x in r if not x[\"scanned_by\"])}, scanned: {sum(1 for x in r if x[\"scanned_by\"])}')"` — unscanned dominant (~80%+ expected, since active strategies cover only weather + sports + a handful of indices).
3. After 7 days: `python3 tools/universe_report.py` produces a markdown report. Investigate any ignored market family with >$100/day volume.
4. File growth: ≤30 MB/day before rotation. If higher, check for cursor-pagination bug pulling expired markets.

---

### ☑ Session 13 — Hypothetical strategy framework (Apr 25+, shipped, 3 sub-sessions)

**Problem.** Even with universe log shipped (Session 12), we have no way to answer "would strategy X have made money on these ignored markets?" or "would `vig_stack_series` with `MIN_RELATIVE_EDGE=0.10` instead of 0.15 have made more money?" Each existing scanner is bespoke 200–500-line code mixing data fetch, edge math, and gating. To back-test a hypothetical variant requires cloning that code — friction high enough that nobody does it. **This is the session where frame-escape actually becomes possible.** Plan as a 2–3 sub-session arc.

**Sub-session 13a — Strategy contract.** Define a pure-function strategy interface in `bot/strategies/__init__.py`:
```python
class Strategy(Protocol):
    name: str
    def name_for(self, market: Market) -> str: ...
    def candidate_markets(self, universe: list[Market]) -> list[Market]: ...
    def evaluate(self, market: Market) -> Opportunity | None: ...
    def finalize(self, scan_id: str) -> None: ...
```
Refactor `vig_stack_series` first (smallest, most mechanical) into `bot/strategies/vig_stack_series.py` matching this contract. Keep the existing scanner's behavior byte-identical. The point is the contract, not a rewrite.

*Shipped 2026-04-25.* `VigStackSeries` handles all three families (weather, index_range, sports_futures) and emits both `vig_stack_series` and `vig_stack_futures` opp_types — the legacy `scan_vig_stack_series` function did the same thing under one name. `name_for(market)` preserves per-family universe attribution. Behavior preservation locked by `tests/test_vig_stack_series_strategy.py` against frozen golden-file outputs (5 hand-crafted ladder scenarios — stable+edge, volatile-below-floor, no-vig, mixed-edge, near-threshold). Live verification: post-deploy `decisions.jsonl` matched pre-deploy gates on shared tickers within natural drift (1/203 reason-mismatches; same `checked` counts). `universe.jsonl scanned_by` attribution preserved (vig_stack_series 116→116, vig_stack_futures 138→136 via natural ladder churn). Universe writer also captures `volume` (lifetime) and `title` now; the `low_liquidity` gate reads lifetime, not 24h. `bot.scanner` re-exports `_forecast_distance_from_bucket` and `_stratified_cf_rejects` from the new module so existing tests still work. ~602 lines deleted from `scanner.py`. **Other scanners (sports arbs, sports parlay, weather direct, econ, series_markets, live_momentum) keep their bespoke entry points — refactor each only when 13b/13c needs to back-test it.**

**Sub-session 13b — Offline back-tester.** New `tools/backtest.py` (local). Inputs: a Strategy class + a date range. Reads `universe.jsonl` archives for that range, runs `evaluate()` against every market, joins results to actual closing prices (settled CLV records or direct Kalshi history API). Outputs: per-day P&L if we'd taken every signal, win rate, mean edge, mean CLV. **Critical discipline:** back-test math uses the *same* `bot/clv.py:compute_clv` function as live trading — no parallel codepath. Avoids "back-test green, live red" divergence.

*Shipped 2026-04-25.* `tools/backtest.py` (local, gitignored). Reads `bot/state/universe.jsonl` + matching gzipped daily archives, groups rows by `scan_id`, replays `Strategy.candidate_markets()` then `evaluate()` per snapshot — never `finalize()` (CF emission writes production state). Joins emitted opportunities to settled clv records via `(ticker, recorded_at±60s)` window, reusing `bot.calibration._within_window`. CLV math is the same `bot.clv.compute_clv_cents()` the live settler uses — extracted from inline arithmetic at `bot/clv.py:284-299` as a 13b prereq, both call sites unified by the same function reference. Monkey-patches `bot.decisions.log_decision` to no-op (would write `decisions.jsonl`) and caches `_fetch_vig_stack_forecasts` once per run (caveat: weather opps evaluate against today's NWS forecast, not historical — verification mode subsets to actually-taken trades where this is irrelevant since live's NWS data is already baked into clv records). `--verify-against-clv-report` flag prints back-test mean CLV alongside `bot.clv.get_clv_report()` mean CLV and asserts `|diff| < 1e-6` for the actually-taken subset (status=="settled" only, excludes counterfactual_settled). Asymmetric vacuous handling: bt-empty + live non-empty is OK with a coverage-gap explanation (universe.jsonl shipped 2026-04-25, so the first 7-day back-test has no overlap with older clv records — that's a coverage gap, not a divergence bug); bt non-empty + live empty IS FAIL (back-tester claims matches that don't exist). `--strategy=<unrefactored>` (sports_monotonicity_arb, sports_consistency_arb, live_momentum, etc.) prints a clean error pointing at the 13a pattern and exits 2. Tests: 11 cases in `tests/test_backtest.py` (universe + gzip loader, ±60s join with 4 boundary cases, snapshot replay with stub strategy that asserts finalize is never called, "not refactored" error, verification mode mean-CLV match, compute_clv_cents reuse — both source-level and runtime no-parallel-codepath check); plus 6 cases in `tests/test_clv.py::TestComputeClvCents` for the extraction. Bot was not restarted (read-only tool, no production state writes). **Session 13c (hypothetical-variant report sweeping `MIN_RELATIVE_EDGE` and other parameters across the captured universe) is the next kickoff — Session 13 stays ☐ until 13c lands.**

**Sub-session 13c — Hypothetical strategy report.** New `tools/hypothetical_report.py` runs N variants of a strategy (e.g., `vig_stack_series` with `MIN_RELATIVE_EDGE` swept across [0.05, 0.10, 0.15, 0.20, 0.25]) against the captured universe and prints a comparison table. Lets us A/B parameter changes without going live. Same tool eventually evaluates entirely-new strategies against ignored markets.

*Shipped 2026-04-25.* Four parts landed in sequence:

1. **VigStackSeries param overrides.** `__init__` gained `min_relative_edge` (default `VIG_STACK_MIN_EDGE = 0.02`), `stable_min_no` (default `VIG_STACK_MIN_NO_ENTRY_PRICE = 0.70`), `volatile_min_no` (default `VIG_STACK_WEATHER_MIN_PRICE = 0.93`). Gate code reads `self._*` instead of imported constants. Naming note: spec example used `MIN_RELATIVE_EDGE` (config.py = 0.15) but defaulting to that would break the 13a golden-file test — so the param is named `min_relative_edge` per spec but defaults to `VIG_STACK_MIN_EDGE` for behavior preservation. Live verification: post-restart `decisions.jsonl` shows preserved gates with `extra={'min_edge': 0.02, 'floor': 0.93}`. 12/12 13a golden tests still pass.

2. **`bot/kalshi_history.py` + cache.** `fetch_settled_close(ticker) -> float | None` wraps `agent.kalshi_client.get_market` (no new method needed in agent/) and returns 100.0 / 0.0 / None based on the `result` field. **Important:** Kalshi reports settled markets with `status="finalized"` (NOT `"settled"`) — the authoritative settle signal is `result in {"yes", "no"}`. Cache at `bot/state/cache/kalshi_settled_closes.json` is permanent for any ticker once written (settled markets never change). Unsettled responses are not cached so they re-fetch on next call.

3. **`run_backtest()` programmatic entry point + `--include-history` flag.** `tools/backtest.py:main()` extracted into `run_backtest(strategy, *, start, end, include_history)` returning the same `aggregate_results` dict. New `--include-history` CLI flag falls back to `fetch_settled_close` on clv miss; synthesized matches carry `status="history_settled"` so they're excluded from the actually-taken verification subset (real-trade-only).

4. **`NbaGameMomentumStrawman` + `tools/hypothetical_report.py`.** ~60-line strategy targeting KXNBAGAME ($262K vol family, 0% scanned). Filter: KXNBAGAME prefix + `status=="active"` + `volume_24h >= min_volume_24h`. Rule: `yes_ask < 30 and no_bid > 70`, edge per spec formula `(100 - yes_ask) / yes_ask - 1.0` (odds-ratio metric, not CLV-comparable — Kalshi's positive vig means a fair-value-based edge is structurally non-positive on real data; the strawman is a contract-verification artifact, not a profit claim). NOT registered in `REGISTERED_STRATEGIES`. Wired into `backtest._resolve_strategy` directly. **Zero changes to `bot/strategies/__init__.py` were needed — the contract held cleanly.**

**Verification results:**
- *vig_stack_series sweep* across `[0.05, 0.10, 0.15, 0.20, 0.25]` over 7 days: opps **strictly monotonic decreasing** (47 → 28 → 18 → 3 → 0), mean_edge **strictly monotonic increasing** (0.0983 → 0.1497) — proves the param refactor wires through correctly. Σ CLV uniformly 0.00 across variants because `universe.jsonl` shipped 2026-04-25 (Session 12) so the 7-day window has at most 1 day of universe coverage with zero overlap to older clv records — the documented 13b "bt-empty + live non-empty vacuous OK" coverage gap, not a bug. Will surface real CLV variation as universe coverage accumulates.
- *Strawman back-test* `python3 tools/backtest.py --strategy nba_game_momentum_strawman --days 7 --include-history`: **33 opps emitted, 3 matched via Kalshi history fallback, +28.00¢ Σ CLV, 66.7% win rate.** The 33-opp / 3-matched count reflects 1 day of universe coverage and only 1 of 4 emitted-on-tickers being finalized (`KXNBAGAME-26APR25DETORL-DET` resolved NO). Cache file written: `bot/state/cache/kalshi_settled_closes.json` with the one settled ticker. **Whether profitable is irrelevant per spec — the success criterion is "system worked end-to-end on a strategy targeting an ignored family in 50-ish lines."**

**Tests:** 56 cases across 6 files all green — `test_vig_stack_series_strategy.py` (12, 13a regression), `test_strategies.py::TestVigStackSeriesParams` (5, new), `test_kalshi_history.py` (12, new — including a `status="finalized"` regression guard for the bug discovered in PART 4 verification), `test_backtest.py` (14, +3 new for `run_backtest`/`--include-history`), `test_hypothetical_report.py` (4, new), `test_nba_game_momentum_strawman.py` (9, new).

**Bot:** restarted once after PART 1 (PID 60317 → 41567); post-restart `decisions.jsonl` shows preserved gate behavior. No restart needed for PARTs 2–4 (read-only tools).

**Session 13 done.** The bot now genuinely supports evaluating alternatives — sweep known parameters, back-test brand-new strategies on never-traded markets, all without going live. The contract from 13a graded out as general; the strawman fit it without modifications.

**Out of scope.** Auto-promotion of best-performing variant to `ACTIVE_STRATEGIES` — always a human gate. Refactoring all existing scanners to the contract is incremental: only refactor when you want to back-test that scanner. Microstructure (Session 15).

**Verify.**
1. After 13a: paper trades produced post-refactor are byte-identical (or within float-epsilon) to what the old `vig_stack_series` would produce on the same market input. Add a regression test to lock this.
2. After 13b: `python3 tools/backtest.py --strategy vig_stack_series --days 7` produces a P&L number that matches the same window's `clv_report` within a small tolerance. If they diverge, the back-tester has a bug.
3. After 13c: hypothetical sweep across `MIN_RELATIVE_EDGE` produces a U-shape or monotonic curve. If flat, either the parameter doesn't matter (interesting!) or the back-tester is broken.
4. After 13c plus 7 days of universe data: pick one ignored market family from `universe_report` and write a 50-line strawman strategy class. Back-test it. If it shows positive CLV at >50 trade volume, that's a real Session-16+ candidate.

---

### ☑ Session 14 — Regime tags (Apr 25, shipped)

**Problem.** A strategy net-negative on average might be +EV in a specific regime — NBA playoffs, weekday mornings, close-to-settlement markets. Without regime context on every record, we can't slice outcomes by regime.

*Shipped 2026-04-25.* New pure module `bot/regime.py:tag(ts, ticker, market_state)` returns a fixed-key dict with 4 axes: `time_of_day` (morning/afternoon/evening/overnight in America/New_York), `day_of_week`, `sport_phase` (preseason/regular/playoffs/off/null), `event_horizon_hr` (<2h/2-12h/12-48h/48-168h/>168h). Pure: same inputs → same output, no I/O, no clock reads. Five writers tag records at write time: `bot/decisions.py:log_decision`, `bot/calibration.py:record_prediction`, `bot/clv.py:record_clv_entry` + `record_counterfactual_skip`, `bot/tracker.py:update_positions` (set-once at first MFE/MAE observation, anchored to `opened_at`), and `bot/universe.py` per-row in `_add_row`. `tools/backfill_regime.py` (local-only, gitignored) idempotently tagged 18,515 historical records (decisions, predictions, universe live + 1 archive, clv, positions) — coverage went 0/N → N/N (100%) on every state file. Four reports gained `--regime-by <axis>`: `tools/cohort_report.py`, `excursion_report.py`, `calibration_report.py`, `universe_report.py`. Bin keys are 3-tuple `(opp_type, regime_value, gate)`; without the flag every bin uses sentinel `_all_` so output is identical to pre-Session-14. Pre-Session-14 records (or any future records the writers can't tag) bucket as `unknown_regime`. 165 tests covering the tagger (DST boundaries, all 7 days, sport phase transitions, event_horizon buckets, 100x determinism property), the 5 writers, the backfill (idempotency, dry-run, gzipped archives, JSON arrays), and the cohort report flag.

**v1 sport_phase limitation.** ESPN's scoreboard API doesn't expose preseason/regular/playoffs and `live_watcher` only caches per-game live state (score/period/clock), not season schedule. So `sport_phase` derives from a hardcoded date table in `bot/regime.py:SPORT_PHASES` covering NBA/NHL/MLB/NCAAB only. ATP/WTA/UFC/IPL/F1 return null. Update `SPORT_PHASES` yearly when each new league season's calendar is published. Future session can add proper ESPN schedule integration to retire the hardcoded table.

**Out of scope (deferred).** `market_vol_tier` axis (needs per-ticker price history infra — `live_ticks.jsonl` exists for live markets but not vig_stack tickers; generalizing is its own session). Regime-adaptive trading — Session 16+, let humans interpret reports first. Auto-clustering of new regimes — manual taxonomy is fine for v1.

**Verify.**
1. After deploy: `tail bot/state/decisions.jsonl | jq .regime` — every new row has populated regime dict with all 4 axes. ✓
2. After backfill: `python3 -c "import json; r=[json.loads(l) for l in open('bot/state/decisions.jsonl')]; print(sum(1 for x in r if 'regime' in x), '/', len(r))"` — coverage > 99%. ✓ 100% on all 5 state files.
3. After 7 days: `python3 tools/cohort_report.py --regime-by sport_phase` produces a per-regime breakdown. Any strategy that's flat overall but has wide regime dispersion (e.g., +20% in NBA playoffs, −15% in regular season) is a regime-adaptive candidate for Session 16+. — Manual check, deferred until enough post-deploy data.
4. Tagger property test: 100x random inputs produce identical outputs. ✓ `test_tag_is_deterministic_property` in `tests/test_regime.py`.

---

### ☑ Session 15 — Live order microstructure (Apr 25+, shipped, plumbing-only — verification deferred until PAPER_MODE=False)

**Problem.** Once we flip to live trading, real orders introduce variables we currently can't measure: fill latency, partial fills, slippage on market orders, queue position on limit orders, order rejection patterns. Without microstructure capture, a strategy that paper-traded green could be quietly unprofitable after live execution costs eat the edge. **Building this now is YAGNI** — only matters when going live.

**Plan.**
- **New module `bot/order_microstructure.py`** — wraps `agent.kalshi_client.place_order` to capture per-order lifecycle:
  ```
  {ts_placed, ts_filled, ts_canceled, requested_price, filled_price,
   requested_qty, filled_qty, order_type, slippage_cents, latency_ms,
   queue_depth_at_place (if available from ticker snapshot), partial_fill_count,
   strategy_name, opp_type}
  ```
  Append to `bot/state/order_microstructure.jsonl`.
- **Hook in `bot/executor.py:_place_order_live` only** — paper mode untouched.
- **Daily rotation** via scheduler.
- **New `tools/microstructure_report.py`** — per-strategy distribution of slippage, fill latency, partial-fill rate. Flags "slippage > 2¢ median" or "fill latency > 5s p95" as execution-quality issues. Joins to CLV records to compute "slippage-adjusted CLV" — true edge net of execution friction.

**Out of scope.** Order routing optimization (use this data to inform changes manually, then iterate). Smart-order-router. Anything paper-mode.

**Verify.** (Defer until live.)
1. First live order: row appears in `order_microstructure.jsonl` with all fields populated.
2. After 50 live orders: `microstructure_report` produces median slippage / latency. Any strategy with median slippage > 2¢ or latency > 5s p95 is a Session-16+ execution-tuning candidate.
3. Slippage-adjusted CLV per strategy matches paper CLV within ~1–2¢. If it diverges by >3¢, paper-mode is over-optimistic and we need to bake a slippage assumption into paper-trade simulation.

*Shipped 2026-04-25.* New module `bot/order_microstructure.py` mirrors `bot/decisions.py` exactly: module-level `threading.Lock`, atomic JSONL append, never-raises wrapper. Three public write functions — `record_placement` (stash placement in in-memory `_PENDING[order_id]` dict), `observe_fill_progress` (increment partial-fill count when a non-terminal fill grows the count), `record_terminal` (compute slippage + latency, append the row, pop from `_PENDING`) — plus `record_synchronous_rejection` for orders that fail at `place_order` (`{"error": ...}`). Hooked into [bot/executor.py:909-944](hustle-agent/bot/executor.py:909) (live branch only — paper-mode codepath at [bot/executor.py:881-908](hustle-agent/bot/executor.py:881) is byte-identical to pre-Session-15) and [bot/executor.py:1054-1090](hustle-agent/bot/executor.py:1054) (`check_fills` LIVE branch — terminal observation triggered by Kalshi-side status OR local `filled` transition; intermediate partial increments via `observe_fill_progress`). Daily rotation in `bot/scheduler.py` reuses the shared `_rotate_jsonl` helper (Session 5). `tools/microstructure_report.py` (local, .gitignored) reads current + last 7 archives, joins to `clv.json` via `(ticker, ts_placed)` ±60s window REUSING `bot.calibration._within_window` and `_parse_iso` (Session 13b precedent), and computes per-strategy slippage / latency / fill-rate stats plus slippage-adjusted CLV. Microstructure rows carry `regime` tags (Session 14 discipline; 6th writer to do so).

**v1 limitations** (documented in module docstring): (1) `slippage_source: "limit_price_echo"` — Kalshi's `place_order` SDK return computes `cost_dollars = round(filled * price_cents / 100.0, 2)` ([agent/kalshi_client.py:390](hustle-agent/agent/kalshi_client.py:390)), echoing the limit price. So slippage will read 0 in production until v2 wires the `/portfolio/fills` endpoint. The schema's `slippage_source` field makes the migration observable. (2) Bot crashes between `place_order` and terminal observation lose that order's microstructure row (`_PENDING` is process-local). Acceptable at tens-to-hundreds of orders/day. (3) Orders pruned from Kalshi after cancellation return `{"error": ...}` from `get_order`; `check_fills` swallows the error today. Punt to v2 (stale-pending sweep).

Tests: 16 cases in `tests/test_order_microstructure.py` (mocked Kalshi client throughout) + 5 cases in `tests/test_scheduler.py::TestOrderMicrostructureRotation` mirroring `TestPredictionsRotation`. Bot restarted at end (Telegram STOP → launchd kickstart). Paper mode behavior verified unchanged — `MICROSTRUCTURE_FILE` is empty after a paper trade fires (regression test in suite).

---

### ☑ Session 15.5 — Data integrity hardening before 7-day unattended run (Apr 25, shipped)

**Problem.** Sessions 6–15 instrumented the bot comprehensively but four subtle gaps would let silent corruption accumulate over a week of unattended operation: (1) `_heartbeat_loop` only touched `bot.lock`, never refreshed `bot_state.last_heartbeat` — Telegram /STATUS and any future watchdog would falsely flag healthy bots as wedged. (2) Pytest runs polluted `bot/logs/bot.log` with mocked errors (190 fake ERROR/Traceback entries accumulated over 24h, ~67KB per suite run) because `bot/main.py:28` calls `setup_file_logging()` at import time — `grep ERROR bot/logs/bot.log` was an unreliable health check. (3) Universe `partial: True` snapshots (90s deadline / cursor failure / Kalshi error) were tagged but unmetered — a 30% sustained partial rate would silently bias every downstream report. (4) `event_horizon_hr` regime coverage on `decisions.jsonl` was 0/4309 because the regime tagger reads `close_ts` from the gate-context `extra` dict but no caller threaded it. (5) Two recent UFC paper positions in `positions.json` lacked regime tags (152/154). (6) No 7-day retuning checklist for the chat reading the data come May 2.

**What shipped.**
- `bot/main.py:_heartbeat_loop` now writes `bot_state.last_heartbeat = now` alongside `LOCK_FILE.touch()` every 30s. State-io errors logged-and-continued so the heartbeat task never crashes on transient FS issues.
- `tests/conftest.py` gained an autouse session-scoped `_isolate_glint_loggers` fixture that snapshots root + glint.*/nexus.* handlers, replaces them with NullHandler, force-marks `bot.logger._initialized=True` so any later import of bot.main can't re-attach the RotatingFileHandler, and removes any `RotatingFileHandler` that slipped in pre-fixture. Restores on teardown. Tests asserting on log output use pytest's `caplog`.
- `bot/universe.py:snapshot_universe` persists `total_snapshots_today` + `partial_snapshots_today` to `bot_state.json` (atomic via `state_io`, reset at midnight ET via `last_universe_metering_reset`). A trailing 10-snapshot deque emits a WARN if partial rate ≥ 10%.
- `bot/executor.py` threads `close_ts` through all 13 `log_decision` call sites: `_log_position_reject` gains a `close_ts` kwarg (used by 7 position-cap gates via `_check_position_limits`'s new `close_ts` param), `_log_edge_reject` extracts close_ts from opportunity (covers 5 verify-edge gates), and the 2 direct sites (self_check_failed, all_gates_passed) include `close_ts` in extras. Caller-supplied `close_ts` in extras always wins.
- `bot/strategies/vig_stack_series.py` adds `close_ts` to the extra dict on all 10 log_decision sites (low_liquidity, no_vig, market_closed [where extra was previously absent — added with just close_ts], forecast_in_bucket, no_price_too_low, no_price_below_floor, non_stable_below_weather_floor, edge_below_threshold, self_check_failed, all_gates_passed). Behavior-preservation regression tests still pass.
- `bot/live_watcher.py:_log_decision_dampened` gains a `close_ts` kwarg; `_tick_momentum` extracts close_ts from the freshly-fetched market dict once and passes it to all 5 dampener call sites. Closes the last 0%-coverage gap on `event_horizon_hr` for live_momentum decisions.
- `tools/backfill_regime.py` re-run on `positions.json` tagged the 2 UFC fight positions (`KXUFCFIGHT-26APR25VIEMCC-VIE/MCC`); both get `sport_phase: None` (UFC isn't in `SPORT_PHASES`, by design — UFC has no traditional season; defer to a future SPORT_PHASES expansion if Tyler wants UFC seasonality). `event_horizon_hr` stays null because positions.json records don't carry close_ts (not a regression — schema thing).
- `CLAUDE.md` adds "When Tyler Asks for the 7-Day Retuning Report" section (mirrors the "How is it looking?" / "Check the Data" triad) plus check #12 in the Check the Data section for the daily partial-rate ratio.

**Tests.** 5 conftest fixture cases (`tests/test_conftest_logger_isolation.py`), 2 heartbeat dual-update cases (`tests/test_main.py`), 5 universe partial-rate cases (`tests/test_universe.py::TestPartialRateMetering`), 7 close_ts threading cases (`tests/test_bot_executor.py::TestCloseTsThreading`), 5 vig_stack close_ts cases (`tests/test_vig_stack_series_strategy.py::test_every_decision_extra_carries_close_ts`), 2 dampener close_ts cases (`tests/test_live_watcher.py`). 26 new tests total; existing behavior-preservation suites still pass. The 9 pre-existing failures noted in Sessions 4/5/6 remain (unrelated).

**Verify (post-restart).**
1. `python3 -c "import json, datetime as dt; s=json.load(open('bot/state/bot_state.json')); print('hb age:', (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(s['last_heartbeat'])).total_seconds(), 's')"` — < 60s sustained.
2. `python3 -m pytest tests/ -q` does not add ERROR/Traceback lines to `bot/logs/bot.log` (use `grep -cE 'ERROR|CRITICAL|Traceback' bot/logs/bot.log` before/after).
3. `python3 -c "import json; s=json.load(open('bot/state/bot_state.json')); print(s.get('total_snapshots_today'), s.get('partial_snapshots_today'))"` — both fields present after first scan post-restart.
4. After ~10 scans post-restart: `python3 -c "import json; r=[json.loads(l) for l in open('bot/state/decisions.jsonl')]; recent=[x for x in r if x.get('ts','') >= '2026-04-26']; with_ehr=sum(1 for x in recent if x.get('regime',{}).get('event_horizon_hr') is not None); print(f'{with_ehr}/{len(recent)} ({100*with_ehr/len(recent) if recent else 0:.0f}%)')"` — > 60% target, > 90% goal.
5. `python3 -c "import json; p=json.load(open('bot/state/positions.json')); wr=sum(1 for x in p if isinstance(x,dict) and 'regime' in x); print(f'{wr}/{len(p)}')"` — `154/154`.

---

## Apr 26+ Strategy Iteration Arc (Sessions 16–20)

Sessions 12–15.5 built the eyes; this arc uses them. The first run of `tools/excursion_report.py` on Apr 25 surfaced two real findings (a math bug in the gap computation and a structural cadence gap for `live_momentum` positions) and a third gap by absence (`live_journal.json` sitting unread by any tool). Sessions 16–18 close those gaps so the data we look at is trustworthy.

**Honest framing on the May 2 / Day-7 horizon:** Day 7 is when *vig_stack-related* signal stabilizes (cohort/calibration reports get ~250 settled CFs/gate, ~2000 settled predictions). Day 7 is **not** when live_momentum signal stabilizes — live_momentum has no fair value (no calibration), short holds (broken excursion until Session 17), and only 3 reject types (no rich cohort signal). For our profitable strategy, the path is engineering-blocked, not calendar-blocked: Sessions 17+18 unlock more live_momentum signal than another month of calendar time would. See "When Tyler Asks for the 7-Day Retuning Report" below for the per-report confidence breakdown.

Sessions 19–20 are explicitly DEFERRED — they're the right next moves but each has clear prerequisites that haven't been met yet (sample-size accumulation for tick-replay back-tester; `PAPER_MODE=False` decision for live microstructure verification).

```
Session 16: Excursion gap-math fix             (small, urgent — bug)
            ↓
Session 17: Tracker cadence audit              (medium — evidence-driven)
            ↓
Session 18: live_journal.json analysis tool    (small-medium — net new tool)
            ↓
[May 2: full retuning report runs]
            ↓
Session 19: Tick-replay back-tester (DEFERRED — prereqs: 16+17+18 + 30+ MFE-instrumented live_momentum settlements + retuning report greenlights live_momentum as the highest-leverage target)
            ↓
Session 20: Live microstructure verification (DEFERRED — prereq: PAPER_MODE=False decision)
```

**Why this order, not "build tick-replay back-tester now":** the tick-replay back-tester proposal is a good *eventual* project, not the right *next* project. The Strategy Protocol from Session 13a doesn't fit tick-replay (snapshot vs stateful), `compute_clv_cents` isn't the right metric for swing trading (need realized P&L), and 39 settled trades is too small for parameter-sweep validation without overfitting. Sessions 17 + 18 produce more live_momentum signal at less cost than building the back-tester immediately.

---

### ☑ Session 16 — Excursion gap-math fix (Apr 25, shipped, deeper-fix scope)

**Problem (pre-fix).** First production run of `tools/excursion_report.py` on Apr 25 returned median gap = -1¢ for both `live_momentum` (n=5) and `vig_stack_series` (n=3). User intuition: gap should be ≥ 0¢ if MFE means "max favorable excursion from entry." Until fixed, every excursion-based decision was built on suspect math.

**Diagnosis.** Not a units bug. Not a sign bug. The math was self-consistent — `gap = mfe_cents - clv_cents`, and `clv_cents` IS exit_favorable_magnitude under the YES-cents convention (verified by reading `bot/clv.py:compute_clv_cents`). The actual cause was structural: `mfe_cents` from `positions.json` tracks the highest *observed bid* during open life — for winners that caps at ~99¢ (yes_bid never quite reaches 100, no_bid never quite reaches 100), while `clv_cents` at settlement uses the payout value (100/0). So gap = (99-entry) - (100-entry) = -1¢ for every winning held-to-settlement position. Confirmed against the 8 settled records: 6 winners all showed gap=-1, 2 losers showed gap=+74 / +78. Median trended to winner-cluster (-1) when winners outnumbered losers.

**What shipped.**
- `bot/clv.py:check_clv_settlements` — Session 16 ratchet at the settlement-time propagation site ([bot/clv.py:363-392](hustle-agent/bot/clv.py:363)). After copying mfe/mae/ticks from positions to the settled clv record, extends `mfe_cents = max(existing, max(0, clv_cents_rounded))`. For winners, this folds the settlement event into MFE (mfe goes from 99-entry to 100-entry = clv); for losers, the ratchet is a no-op (clv negative ≤ existing 0). Updates `mfe_at` to `settled_at` only when the value actually changed. `mae_cents` NOT extended — out of scope, report doesn't read MAE.
- `tools/excursion_report.py` — refactored to compute `exit_favorable_magnitude(side, entry, closing_yes)` explicitly per side ([tools/excursion_report.py:35-46](hustle-agent/tools/excursion_report.py:35)). Mathematically equivalent to `compute_clv_cents` but legible at the report site (Session 13b discipline: same formula in both places). Updated load filter to require `closing_yes_price`, `side`, and `entry_price_cents` defensively. Module docstring spells out the YES-cents-favorable convention and the `gap ≥ 0` invariant.
- `bot/tracker.py:83-100` — expanded MFE-update site comment. Documents (a) the YES-cents-favorable convention (NO-side conversion), (b) that observed bids ARE NOT folded with settlement payouts in the live path, and (c) where the settlement extension lives (`bot/clv.py:check_clv_settlements`), with a "don't move it back here" note.
- `tools/backfill_extended_mfe.py` (new, gitignored) — one-shot mirroring `tools/purge_clv_disabled.py`. Asserts `bot.lock` gone or PID dead, requires `--yes`, backs up `clv.json` → `.bak-YYYYMMDD`. Iterates settled records and ratchets `mfe_cents = max(existing, max(0, clv_cents))`, advancing `mfe_at` only when value changes. Idempotent — re-runs are no-ops. Expected output on production: 6 records updated (all winners). Losers unchanged.
- `tests/test_excursion_report.py` (new) — 29 cases. 7 helper tests on `exit_favorable_magnitude`. 8 hand-crafted gap tests covering YES/NO winners (extended), losers (positive gap), MFE=0 adverse-only positions (acceptance criterion: exit_favorable ≤ 0 → gap > 0), perfect-extension case, and the pre-Session-16 -1¢ shape. Two property tests (each 100 random `(side, entry, mfe, closing_yes)` tuples seeded at 42/123): the report's helper matches by-hand re-derivation within float epsilon, and post-extension records yield non-negative gaps. 12 load-filter defensive cases (missing/invalid `mfe_cents` / `clv_cents` / `closing_yes_price` / `side` / `entry_price_cents`, malformed JSON, non-list).
- `tests/test_clv.py` — appended `class TestSettlementMfeExtension` with 9 cases: winner pos.mfe < settlement (extension fires, mfe_at advances to settled_at), winner pos.mfe ≥ settlement (no-op, preserves both), loser (no-op, clv negative), no matching pos / winner (seeds mfe to clv_cents), no matching pos / loser (clamps to 0 — report includes record with gap = -clv > 0), counterfactual record untouched (mfe_cents stays None), idempotency on re-settlement (second call doesn't re-touch settled records), and frozen mfe_at when value didn't change.
- `tests/test_tracker.py` — updated 2 propagation tests in `TestSettlementPropagation` to match new behavior. `test_settlement_carries_mfe_to_clv_record`: pos.mfe=12 with NO@79 settles at clv=21 → mfe extended to 21 (was: assertion 12). `test_settlement_without_position_match_seeds_mfe_to_clv` (renamed from `..._leaves_clv_unchanged`): no-pos winner gets mfe seeded to clv_cents=40 (was: assertion mfe_cents not in rec). Both now reflect the Session 16 contract.

**Tests.** 220+ passed across the touched surfaces (`test_clv.py`, `test_tracker.py`, `test_excursion_report.py`, `test_vig_stack_series_strategy.py`, plus `test_backtest`, `test_calibration`, `test_decisions`, `test_strategies`, `test_universe`, `test_main`, `test_regime`, `test_backfill_regime`). The 13a golden-file regression and Session 9's MFE/MAE ratchet tests both still pass — confirming the tracker.py change was truly comment-only and the gap fix didn't reach into shared code.

**Coordination with user (deploy).** Stop bot → `python3 tools/backfill_extended_mfe.py --yes` (extends 6 winning records on production data, backs up clv.json) → `python3 tools/excursion_report.py` (verify gap ≥ 0 on every record) → restart bot. Code-path change in `bot/clv.py` deploys via the restart. After next live settlement, verify the extension fires (any new settled clv record will have `mfe_cents ≥ clv_cents`).

**Out of scope (flagged in commit message).**
- MAE extension at settlement (parallel pattern; report doesn't read MAE — defer).
- Other report tools (`cohort_report.py`, `calibration_report.py`, `microstructure_report.py`, `universe_report.py`, `journal_analysis.py`) may have similar untested propagation patterns. Not investigated this session.
- Backfilling MFE on pre-Session-9 records (impossible — data wasn't captured).
- Tracker cadence audit (Session 17 — separate problem; the median-1-tick issue for live_momentum is unrelated to the gap-math fix).

**Verify (post-deploy).**
1. `python3 tools/excursion_report.py` — every gap value ≥ 0¢. Specifically: 6 winners go to gap=0 (mfe extended to clv); 2 losers stay at +74 / +78. Median for both strategies is now 0¢ on this small sample.
2. `python3 -m pytest tests/test_excursion_report.py tests/test_clv.py -v` — property test passes (100 random tuples) plus 9 settlement-extension cases.
3. Convention documented in three places: `tools/excursion_report.py` module docstring, `bot/tracker.py` MFE-update comment, `bot/clv.py:check_clv_settlements` extension comment.
4. Existing tests still pass: `python3 -m pytest tests/test_tracker.py tests/test_vig_stack_series_strategy.py -v` — Session 9's MFE/MAE tests + 13a golden-file regression.

---

### ☑ Session 17 — Tracker cadence audit for live_momentum (Apr 26, shipped, Outcome B)

**Problem (pre-fix).** Apr 25's first excursion_report showed `median ticks = 1` for `live_momentum`. Phase-1 read of [bot/state/positions.json](hustle-agent/bot/state/positions.json) revealed the actual problem was worse: **54/60 (90%) of live_momentum positions had `ticks_observed = None`** — never observed by tracker at all. Three causes needed disambiguation: (a) cadence-limited polling, (b) structurally-fast positions, (c) bug in `update_positions`.

**Diagnosis (Outcome B — cadence-limited).** Phase-1 evidence:
1. `tracker.update_positions` was called from exactly one place, [bot/main.py:1175](hustle-agent/bot/main.py:1175), once per `_main_loop` iteration. Cadence = `scan_interval`.
2. `scan_interval` is set by [bot/scanner.py:get_scan_interval](hustle-agent/bot/scanner.py:141) which inspects the *odds-API* games list (NBA/NHL/MLB pregame). It does not see Kalshi-native sports (UFC fights, IPL cricket, individual-match markets) that `live_watcher` actually bets on. So `scan_interval` was IDLE (1800s) most of the time live_momentum was open.
3. live_journal.json (n=95 paired bet→exit cycles): median lifetime 647s (10.8 min), p25 = 171s, **35% exit in <5 min**. UFC fights especially were sub-2min and raced tracker to zero.
4. live_watcher's exit path sets `pos["status"] = "exited"` ([bot/live_watcher.py:2391](hustle-agent/bot/live_watcher.py:2391)). After that, tracker's `if status not in (filled, partial): continue` guard ([bot/tracker.py:50](hustle-agent/bot/tracker.py:50)) permanently locks the position out of observation. Positions that exited before update_positions ever fired stayed at `ticks=None` forever.
5. For the 6/60 ticked positions, implied cadence (lifetime / ticks) was 1312s–3235s, median ~2168s — consistent with IDLE, NOT with SCAN_INTERVAL_LIVE (120s). Confirms scanner saw no live games during those windows.

No bug in update_positions itself (Outcome C ruled out). Not structurally meaningless (Outcome A applies only to the sub-30s tail).

**What shipped.**
- **New [bot/tracker_cadence.py](hustle-agent/bot/tracker_cadence.py)** — append-only `tracker_cadence.jsonl` with schema `{ts, num_open_positions, ms_since_last_call, called_from}`. Mirrors [bot/decisions.py](hustle-agent/bot/decisions.py) (threading.Lock, atomic append, never-raises). `ms_since_last_call` is per-call-site (keyed by `called_from`) so `_main_loop` and `_position_check_loop` deltas don't pollute each other.
- **[bot/tracker.py:update_positions](hustle-agent/bot/tracker.py:35)** — added `called_from: str = "unspecified"` parameter. Calls `tracker_cadence.log_cadence` at the top of every invocation, before iterating positions. `num_open_positions` counts only `(filled, partial)` so the field measures the per-call work, not raw list size.
- **[bot/main.py:_position_check_loop](hustle-agent/bot/main.py)** — new async loop alongside `_main_loop` / `_live_scan_loop` / `_heartbeat_loop`. Wakes every 30s after a 20s startup delay, calls `update_positions(called_from="_position_check_loop")` via `loop.run_in_executor` (avoids blocking the event loop with the synchronous market-fetch loop). **Discards alerts** — alert→exit_position flow stays driven by `_main_loop`'s call site to avoid double-firing take_profit/cut_loss. Safe because `update_positions` is idempotent (mfe/mae use `>` comparisons; ticks++ is per-call). Wraps in try/except so a market-fetch hiccup doesn't kill the observation loop.
- **[bot/main.py:1175](hustle-agent/bot/main.py:1175)** — existing main_loop call site updated to `update_positions(called_from="_main_loop")`.
- **[bot/scheduler.py](hustle-agent/bot/scheduler.py)** — added daily rotation block + `_rotate_tracker_cadence_log` helper, mirroring the existing `_rotate_decisions_log` / `_rotate_universe_log` pattern. Uses `last_tracker_cadence_rotation` key in bot_state.
- **`tests/test_tracker.py`** — 6 new cases under `TestCadenceLogging`: schema integrity, default `called_from="unspecified"`, per-call-site delta isolation, num_open filters by status, empty-positions case, ticks_observed monotonicity preserved with new param. Plus the `tracker_env` fixture now isolates `tracker_cadence.CADENCE_FILE` to tmp_path so existing tests don't pollute production state.
- **`tests/test_main.py`** — 2 new cases: `test_position_check_loop_calls_update_positions_each_cycle` (3 work cycles, asserts called_from threaded through and 30s cadence) and `test_position_check_loop_swallows_update_errors` (transient errors don't kill the loop).

**Tests.** 124 passed across `test_tracker.py`, `test_main.py`, `test_clv.py`, `test_decisions.py`, `test_excursion_report.py`, `test_bot_tracker.py`. Session 9 ratchet tests + Session 16 settlement-extension tests both still pass — confirming the called_from default keeps backward compat and the tracker_cadence write doesn't disturb existing MFE/MAE behavior.

**Coordination with user (deploy).** Bot must restart to pick up the new `_position_check_loop` task and the modified `update_positions` signature. After restart, `tracker_cadence.jsonl` should have ≥1 row within the first minute (the 20s startup delay + first run_in_executor) and ≥10 rows within five minutes.

**Out of scope (flagged in commit message).**
- Outcome D (live_watcher's 10s tick poll updating tracker per-game) — defer until 30s loop's effectiveness is measured. Sub-30s positions remain unobserved (see Known Quirk below).
- `position_observation_log.jsonl` (per-position-per-tick row) — prompt made it optional; the existing `ticks_observed` field on positions.json plus the new cadence log gives the same correlation.
- Re-running [tools/excursion_report.py](hustle-agent/tools/excursion_report.py) — meaningful only after 48–72h of post-restart data accumulates. Schedule a follow-up agent rather than re-run prematurely.
- Decoupling `scanner.py:get_scan_interval` from live_watcher's view of "live" (the architectural root cause). The 30s loop sidesteps this without rearchitecting; a deeper unification is out of scope.

**Known quirk (residual structural — Outcome A applies to a small tail).** ~7% of live_momentum positions exit in under 30s (mostly UFC squash matches). Even with the new 30s loop, these will keep `ticks_observed ≤ 1`. To capture sub-30s observations would require integrating tracker into live_watcher's 10s tick poll (Outcome D). When reading `ticks_observed` distributions on live_momentum, treat the sub-30s tail as a known-empty bucket, not a data-quality issue. excursion_report's `n` for live_momentum is therefore a *fraction* of total positions — by design after Session 17, since the unbounded median-tick floor was the actual symptom.

**Verify (post-deploy).**
1. **Within 1 minute of restart:** `wc -l bot/state/tracker_cadence.jsonl` ≥ 1. `tail -3 bot/state/tracker_cadence.jsonl | jq .` shows the schema (ts, num_open_positions, ms_since_last_call, called_from) and both `_main_loop` + `_position_check_loop` will appear after a few minutes.
2. **Within 5 minutes:** median `ms_since_last_call` for `_position_check_loop` ≈ 30000ms. Quick check:
   ```bash
   python3 -c "import json; from collections import Counter; rows=[json.loads(l) for l in open('bot/state/tracker_cadence.jsonl')]; print(Counter(r['called_from'] for r in rows)); deltas=sorted(r['ms_since_last_call'] for r in rows if r['ms_since_last_call'] and r['called_from']=='_position_check_loop'); print(f'pos-check median: {deltas[len(deltas)//2]/1000:.1f}s' if deltas else 'no data')"
   ```
3. **48-72h post-deploy** (scheduled follow-up agent — NOT this session): re-run `python3 tools/excursion_report.py`. Median ticks for live_momentum should improve to ≥5 (positions lasting >5 min now get observed regularly). Also: `positions.json` ticks_observed distribution — instead of 90% None, expect majority populated for new positions opened after the restart. Legacy ones keep their None.
4. **Existing tests still pass:** `python3 -m pytest tests/test_tracker.py tests/test_main.py -v` — 24 tests including the 6 new TestCadenceLogging + 2 new _position_check_loop tests.

---

### ☐ Session 18 — live_journal.json analysis tool (Apr 26+, planned)

**Problem.** `bot/state/live_journal.json` (~600 KB and growing) records every per-game event live_watcher emits (`scan_found`, `bet`, `exit`, `session_end`) for every game. This is rich behavioral data that NO existing analysis tool reads. It captures things excursion_report and cohort_report can't:
- Time-to-exit distribution per strategy (where does the actual hold-time density live?)
- Exit-reason breakdown (take_profit vs trailing_stop vs near_settle vs hard_cap — each is a tunable)
- "Watched but didn't enter" funnel (per game: did we see it? Why didn't we enter? `no_leader`, `low_volume`, `disabled_sport`, `no_dip`)
- Per-sport breakdown of all the above
- Session_end summary per game (P&L, max drawdown, win/loss)

Sessions 6–15 instrumented decisions. Session 18 reads the BEHAVIOR data sitting unanalyzed. This is also the better Step-1 we should have run before excursion_report on Apr 25.

**Plan.**
- New `tools/journal_analysis.py` (gitignored, local-only — match the `cohort_report` / `excursion_report` / `calibration_report` / `universe_report` / `microstructure_report` convention).
- Read current `live_journal.json` plus any rotation archives if they exist. (If `live_journal.json` doesn't have rotation today, that's a separate small task — flag in commit but don't scope-creep into adding it here.)
- Per-strategy aggregations:
  - **Time-to-exit distribution**: bucket {<5min, 5-15min, 15-60min, >60min}. For live_momentum, where does the density live?
  - **Exit-reason breakdown**: % via take_profit / trailing_stop / stop_loss / near_settle / hard_cap. Surface the most-common and least-common — each rare reason is either dead config or bad-data signal.
  - **Watch-but-no-enter funnel**: aggregate `scan_found` events that have NO matching `bet`. Group by `skip_reason` if recorded; otherwise mark as `unknown_skip`.
  - **Per-sport split**: same metrics segmented by sport (NBA / NHL / MLB / UFC / IPL).
  - **Per-game session_end**: P&L distribution, drawdown distribution.
- Markdown to stdout. Designed to be the **primary analysis tool for live_momentum** until tick-replay back-tester (Session 19) ships.
- Optional: regime-slice support via `--regime-by` flag mirroring the other reports. Skip if it adds non-trivial complexity; the per-sport split already captures the most important regime axis for sports strategies.

**Out of scope.** Tick-replay (Session 19). Modifying `live_watcher` to record additional event fields (forward-compatible — work with what's there). Rotation of `live_journal.json` itself if absent (separate trivial task).

**Verify.**
1. `python3 tools/journal_analysis.py` runs without errors on production journal.
2. Output makes intuitive sense: NBA hold times ~10-30 min match expectation, exit-reason mix is roughly balanced (not 100% one path).
3. **Find at least one actionable insight.** Examples: "85% of NBA exits via take_profit, 0% stop_loss → stop_loss is set too generous, never fires"; "60% of UFC games never get entered because of `no_leader` → leader threshold is too strict for UFC"; "median NBA hold time is 4 min → live_momentum is more scalp than swing → take_profit could be tightened." Record the insight in the Session 18 commit message; it's the deliverable, not the tool.

---

### ☐ Session 19 — Tick-replay back-tester for live_momentum (Apr 26+, planned, DEFERRED)

**Problem.** Live_momentum is our only profitable strategy (+$12.30, 62% WR over 39 trades). Tick data has been accumulating in `live_ticks-*.jsonl.gz` archives since Session 5 (Apr 23). The natural next move is to back-test the swing-trading strategy across parameter sweeps. **But building this BEFORE the prereqs are met would be premature for four reasons surfaced in the Apr 25 design discussion:**
1. The Strategy Protocol from Session 13a is *snapshot-based*; live_momentum is *stateful per-game*. The contract needs an extension (`TickStrategy` with a `process_tick()` method), not reuse.
2. `compute_clv_cents` measures entry-vs-close. Swing trading needs entry-vs-EXIT (realized P&L). Different metric.
3. 39 settled trades is too small for parameter-sweep validation without overfitting. Standard error on 62% WR with n=39 is ~8 percentage points.
4. Market-impact slippage on Kalshi's thin sports markets is real and unmeasured (PAPER_MODE=True → microstructure data is empty). A naive back-test will look 25-50% more profitable than reality.

**Prereqs (gates the session — do not start until all four met).**
1. Sessions 16 (excursion bug fix) + 17 (tracker cadence) + 18 (journal analysis) shipped.
2. Sessions 17 + 18 outputs (tracker cadence + journal_analysis) confirm that live_momentum exit-logic / parameter retuning is the highest-leverage candidate. If journal_analysis instead points at a config issue (e.g., 60% of UFC games never enter due to leader threshold), fix THAT first — it's cheaper than a back-tester. The back-tester only earns its scope when the existing analysis tools have been exhausted on live_momentum specifically.
3. At least 30 settled live_momentum positions with MFE/MAE coverage (vs Apr 25's 5). Calendar-bound.
4. Honest acknowledgment of slippage as an UNMEASURED variable; design the back-tester with a configurable slippage assumption.

**Plan (when prereqs met).**
- Extend `bot/strategies/__init__.py` Protocol with a `TickStrategy` variant: `process_tick(state, tick) -> TickAction | None` where `TickAction` is `Buy(side, qty)` / `Sell(side, qty)` / `None`. Stateful (carries position/price-history state across ticks) — distinct from the snapshot-based `Strategy`.
- New `bot/strategies/live_momentum.py` (or refactor existing live_watcher entry/exit logic) implementing the `TickStrategy` contract — pure function from `(state, tick) → action`. Mirror the 13a discipline: behavior-preserving refactor first, golden-file test locking equivalence to current live_watcher behavior, THEN parameter knobs.
- New `tools/tick_backtest.py` (local-only) reads `live_ticks-*.jsonl.gz` archives, replays through TickStrategy, emits per-game realized P&L.
- Parameter sweep grid: `dip_threshold` × `take_profit` × `stop_loss` × `max_entry_price`. Cap total combos at ~50 to avoid overfitting bait.
- **Train/test split discipline:** sweep on 70% of games (by date order), validate on held-out 30%. Report BOTH numbers; only the validation P&L matters for retuning decisions.
- **Slippage pessimism:** every round-trip subtracts a configurable slippage (default `+2¢` per round trip — ungenerous on purpose). Document the assumption explicitly in the report output.
- Regime slicing via `bot/regime.py` tagger on game start time.
- Output: per-variant table (validation_pnl, validation_winrate, validation_round_trips) + best-validated variant callout. NEVER auto-promote.

**Out of scope.** Auto-promotion of best variant to live (always human gate). Refactoring live_watcher to USE the TickStrategy contract immediately (only refactor when ready to back-test live changes; pre-refactor lock-via-golden-test is the prereq).

**Verify (when shipped).**
1. **Replay matches reality:** take a known live_momentum trade from `paper_trades.json`, replay its game's ticks through TickStrategy with current parameters, assert P&L matches within 1¢. If not, the back-tester has a bug — fix before reporting any sweep.
2. Sweep produces non-flat result curves (parameter actually matters across the range).
3. Validation P&L ≥ 0 for the best variant. **If validation is negative when training is positive, we're overfitting** — narrow the sweep grid or reject the result.
4. Commit message names the best-validated parameter set as a Session-21+ retuning candidate (NOT auto-promoted).

---

### ☐ Session 20 — Live order microstructure verification (Apr 26+, planned, DEFERRED)

**Problem.** Session 15 shipped `bot/order_microstructure.py` plumbing. Verification was deferred to "when PAPER_MODE=False" since paper trades produce zero microstructure rows by construction. When the bot eventually flips live, the first 50 orders are the moment of truth: does the capture work end-to-end? Are slippage / latency / partial-fill numbers in the expected range? Does paper-mode CLV match slippage-adjusted live CLV? Without this verification, going live is flying blind on execution costs.

**Prereq.** `PAPER_MODE = False` decision made and deployed. This is a Tyler-decision, not a code-decision; do not pre-empt.

**Plan (when shipped).**
- After flip to live, monitor `bot/state/order_microstructure.jsonl` after first order: row populated with all expected fields, sign convention correct (positive slippage = adverse), `terminal_status` set.
- Run `python3 tools/microstructure_report.py` after first 10 / 50 orders.
- Compare slippage-adjusted live CLV to paper CLV for the same strategy over the same period. **Target: ≤2¢ divergence.** If >3¢ divergence: paper-mode is over-optimistic and Session 21+ needs to bake a slippage assumption into paper simulation.
- Per-strategy slippage / latency: any strategy with median slippage > 2¢ or fill latency > 5s p95 is a Session-21+ execution-tuning candidate.
- Update `tools/microstructure_report.py` if live-order data reveals fields/edge cases the mock tests didn't catch (Kalshi's API has historically had quirks — see the `finalized` vs `settled` discovery in Session 13c).

**Out of scope.** Order routing optimization (use this data to inform changes manually, then iterate — Session 21+). Smart-order-router. Anything that touches paper mode.

**Verify (when shipped).**
1. First live order populates a complete row in `order_microstructure.jsonl`.
2. After 50 live orders: `microstructure_report` shows median slippage / fill latency / partial-fill rate per strategy.
3. Slippage-adjusted CLV per strategy matches paper CLV within ≤2¢. If diverges >3¢: open Session 21+ to bake slippage into paper simulation.

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

## When Tyler Asks to Check the Data

This is the *data-quality* checklist (vs. the *bot-health* checklist above). Walk through every collection point. For each: run the inspect command, compare actual vs. expected, note known gaps. Don't skim — the whole point of Session 6's instrumentation is that bad data masquerades as good data unless you actually look.

### 1. `bot/state/decisions.jsonl` — per-decision audit log (Session 6)
- **Inspect:** `wc -l bot/state/decisions.jsonl && tail -5 bot/state/decisions.jsonl | jq .`
- **Expect:** non-zero line count growing at ~5-30 records/min during active scans. Each record has `ts, ticker, opp_type, edge, gates, decision, reason`.
- **Distribution check:** `python3 -c "import json; from collections import Counter; recs=[json.loads(l) for l in open('bot/state/decisions.jsonl')]; print(Counter((r['opp_type'], r['decision']) for r in recs).most_common(20))"` — should show spread across opp_types and a healthy reject:accept ratio (rejects vastly outnumber accepts).
- **Gate spread check:** `python3 -c "import json; from collections import Counter; recs=[json.loads(l) for l in open('bot/state/decisions.jsonl') if json.loads(l).get('decision')=='reject']; print(Counter(r['reason'] for r in recs).most_common(20))"` — every gate from `bot/scanner.py`, `bot/executor.py`, and `bot/live_watcher.py` should show ≥1 reject. Gates with ZERO rejects are either dead code or mis-instrumented.
- **Known gaps:** Session 7 (live-momentum gates emit `edge=null`). Session 10 (Apr 24) added distance-from-threshold context to scanner.py + executor.py reject `extra` dicts; pre-Session-10 records remain `extra`-less and are silently skipped by `cohort_report`'s distance histogram.
- **Session 14:** every record carries `regime` (time_of_day, day_of_week, sport_phase, event_horizon_hr).

### 2. `bot/state/clv.json` — counterfactual + real-trade record book (Sessions 5, 6, 8)
- **Inspect:** `python3 -c "import json; r=json.load(open('bot/state/clv.json')); from collections import Counter; print('total:', len(r)); print('status:', Counter(x.get('status') for x in r)); print('opp_type:', Counter(x.get('opp_type') for x in r))"`
- **Expect:** `counterfactual_open` records accumulating between settlements; `counterfactual_settled` records growing as markets resolve; `paper`/`settled` records for actual trades. opp_type spread matches `ACTIVE_STRATEGIES`.
- **Pollution check:** `python3 -c "import json; r=json.load(open('bot/state/clv.json')); bad=[x for x in r if (x.get('entry_price_cents') or 100) < 3 or x.get('ticker','').startswith('KXTEST')]; print(f'{len(bad)} polluted records — should be 0')"` — Apr 24 follow-up gated CF entry < 3¢; KXTEST records are debug residue. Both should be 0.
- **CF-gate coverage:** `python3 -c "import json; from collections import Counter; r=json.load(open('bot/state/clv.json')); cf=[x for x in r if x.get('status','').startswith('counterfactual')]; print(Counter(x.get('skipped_by_gate') for x in cf))"` — pre-Session-8 this is dominated by 1-2 gates (top-K-by-edge selection bias). Post-Session-8, every gate from `decisions.jsonl` rejects also appears here.
- **Known gaps:** Session 8 (top-5-by-edge globally → stratified per-gate sampling).
- **Session 14:** real + CF records carry `regime` (CF rows resolve `event_horizon_hr` from opp's close_ts; real entries leave it null).

### 3. `bot/state/bot_state.json` — main loop heartbeat
- **Inspect:** `python3 -c "import json, datetime as dt; s=json.load(open('bot/state/bot_state.json')); hb=dt.datetime.fromisoformat(s['last_heartbeat']); age=(dt.datetime.now(dt.timezone.utc)-hb).total_seconds(); print(f'heartbeat age: {age:.0f}s (scans_today={s[\"scans_today\"]}, last_scan_at={s.get(\"last_scan_at\")})')"`
- **Expect:** heartbeat age < scan_interval + 60s slack (default 1860s). `scans_today` ratchets up across the day. `last_decisions_rotation` and `last_live_ticks_rotation` set to today after 00:00 ET.
- **Caveat:** heartbeat updates per scan, so age can legitimately be ~30 min during normal idle. Use it for "is the loop alive" not "is the loop responsive."
- **Known gaps:** Session 7 (lock-touch is also per-scan only; no per-second heartbeat for liveness).

### 4. `bot/state/bot.lock` — process liveness signal
- **Inspect:** `stat -f 'lock mtime=%Sm pid=%z' bot/state/bot.lock 2>/dev/null && cat bot/state/bot.lock`
- **Expect:** mtime within last scan interval. PID matches `ps aux | grep bot.main`.
- **Caveat:** lock-touch fires at scan boundaries only ([bot/main.py:1061](hustle-agent/bot/main.py:1061)) — between scans, mtime can be stale up to 30 min and that's fine. Session 7 will add per-second heartbeat for true liveness.

### 5. `bot/state/strategy_audit.json` — settlement + PnL log
- **Inspect:** `python3 -c "import json; s=json.load(open('bot/state/strategy_audit.json')); sl=s.get('settlement_log',[]); print(f'{len(sl)} settlements'); [print(f'  {x.get(\"ticker\")}: {x.get(\"pnl\",0):+.2f}') for x in sl[-10:]]"`
- **Expect:** settlement records grow as markets resolve. PnL has both wins and losses (a strategy with 100% wins is suspicious — usually means tiny sample).
- **Cross-check:** count of settled positions in `clv.json` (status=`settled`) should match settlement_log entries within ±1.
- **Caveat:** no per-strategy PnL aggregation here — use `tools/cohort_report.py` once Session 6 has 7 days of data.

### 6. `bot/state/live_ticks.jsonl` — momentum scanner observations
- **Inspect:** `wc -l bot/state/live_ticks.jsonl && tail -3 bot/state/live_ticks.jsonl | jq .`
- **Expect:** non-zero only during active games (NFL/MLB/NBA windows). Quiet evenings = legitimately empty.
- **Rotation:** `ls bot/state/archive/live_ticks-*.jsonl.gz | tail -3` — yesterday's archive present after midnight ET.
- **Caveat:** can grow fast during multi-game windows (>100k records/day). If `du -h` exceeds 50 MB before rotation, investigate.

### 7. `bot/state/positions.json` — open positions
- **Inspect:** `python3 -c "import json; p=json.load(open('bot/state/positions.json')); active=[x for x in p if isinstance(x,dict) and x.get('filled',0)>0 and x.get('status') in ('filled','partial')]; print(f'{len(active)} active, ${sum(x.get(\"cost\",0) for x in active):.2f} exposure')"`
- **Expect:** count ≤ `MAX_POSITIONS`, exposure ≤ `MAX_TOTAL_EXPOSURE` (see [bot/config.py](hustle-agent/bot/config.py)). Same-game count ≤ `MAX_PER_GAME`.
- **Stale-position check:** `python3 -c "import json, datetime as dt; p=json.load(open('bot/state/positions.json')); now=dt.datetime.now(dt.timezone.utc); old=[x for x in p if isinstance(x,dict) and x.get('status')=='filled' and (now-dt.datetime.fromisoformat(x.get('entry_at',now.isoformat()))).total_seconds() > 86400]; print(f'{len(old)} positions older than 24h — investigate if any')"` — orphaned positions usually mean a settlement check is failing.
- **Session 14:** open positions carry `regime` set once at first MFE/MAE observation, anchored to `opened_at`.

### 8. `bot/logs/bot.log` — operational log
- **Inspect:** `tail -50 bot/logs/bot.log` — look for `SCAN CYCLE`, `Edge accepted`, `Edge rejected`, gate-name patterns.
- **Error scan:** `grep -E 'ERROR|CRITICAL|Traceback' bot/logs/bot.log | tail -20` — any repeated exception is investigable. One-offs from API timeouts are normal.
- **Scan cadence check:** `grep 'SCAN CYCLE' bot/logs/bot.log | tail -10 | awk '{print $1, $2}'` — gaps should be ≤ scan_interval + 60s. Larger gaps = wedge or DarkWake event (Apr 24 fix should prevent these).

### 9. `bot/state/predictions.jsonl` — per-prediction fair-value log (Session 11)
- **Inspect:** `wc -l bot/state/predictions.jsonl && tail -3 bot/state/predictions.jsonl | jq .`
- **Expect:** ≥1 row per opp the scanner evaluates (real trades + stratified CFs). Schema: `{ts, scan_id, ticker, opp_type, predicted_fair_cents, market_price_cents, closing_yes_price}`. `closing_yes_price=null` until settlement.
- **Settlement coverage:** `python3 -c "import json; r=[json.loads(l) for l in open('bot/state/predictions.jsonl')]; n=len(r); s=sum(1 for x in r if x.get('closing_yes_price') is not None); print(f'{s}/{n} settled ({100*s/n if n else 0:.0f}%)')"` — 0% same-day, climbs to ~100% within 7 days for resolved markets.
- **Run report:** `python3 tools/calibration_report.py` — needs ≥7 days of settled data for stable Brier scores.
- **Known gaps:** live_momentum predictions skipped (`predicted_fair_cents=None` is silently dropped). Pre-Session-11 trades have no prediction record. Predictions count ≈ count of `clv.json` records where `status in (open, counterfactual_open)` minus live_momentum CLV rows.
- **Session 14:** every record carries `regime` (event_horizon_hr is null at this writer — close_ts isn't threaded through the calibration call).

### 10. `bot/state/universe.jsonl` — active-market snapshot per scan (Session 12)
- **Inspect:** `wc -l bot/state/universe.jsonl && tail -3 bot/state/universe.jsonl | jq .`
- **Expect:** ~800–1,500 rows per scan after dedupe. Schema: `{ts, scan_id, ticker, series_ticker, event_ticker, status, close_ts, yes_ask, yes_bid, no_ask, no_bid, volume_24h, open_interest, scanned_by, partial?}`. Roughly 50/50 scanned vs. ignored — shadow-fetch-by-active-series guarantees coverage of strategy-relevant tickers; cursor walk picks up the long-tail ignored families.
- **Coverage check:** `python3 -c "import json; r=[json.loads(l) for l in open('bot/state/universe.jsonl')]; print(f'total: {len(r)}, unscanned: {sum(1 for x in r if not x[\"scanned_by\"])}, scanned: {sum(1 for x in r if x[\"scanned_by\"])}')"`.
- **Run report:** `python3 tools/universe_report.py [--by-scanner]` — per-prefix breakdown with ignored-volume + ignored-spread; surfaces Session-13 candidates (e.g. observed Apr 25: KXNBAGAME with $262K vol completely unscanned, KXMLBTOTAL with 165 ignored markets despite TOTAL_SERIES["mlb"] being defined, full KXNHL* series uncovered).
- **Architecture:** two-pass snapshot. Pass 1 cursor-paginates `status=open` markets (bounded by 90s deadline); pass 2 explicitly fetches each active-strategy series (`WEATHER_SERIES_TICKERS` / `INDEX_RANGE_SERIES_TICKERS` / `SPORTS_FUTURES_TICKERS` / sports-arb series dicts). Rows are written to `universe.jsonl` after `scan_cycle` returns so `scanned_by` is fully populated by `on_market_seen` callbacks fired during scanning.
- **Rotation:** `ls bot/state/archive/universe-*.jsonl.gz | tail -3` — yesterday's archive present after midnight ET.
- **Caveat — KXMVE filter:** Multi-Variate Event parlay expansions (`KXMVE*` tickers) are dropped at write time. Kalshi creates 50K+ at any moment (parlay product variants); they overwhelm the log without informing strategy gaps. Lift the filter in `bot/universe.py:_MVE_PREFIX` if Session 13 wants to back-test parlay strategies.
- **Caveat — `partial: true`:** Cursor pass under load (live_watcher polling Kalshi during games) often hits the 90s deadline before exhausting the cursor — those rows carry `partial: true`. Shadow pass still runs and populates active-series coverage. Reports include partial rows in per-prefix breakdown but flag the partial percentage at the top so absolute long-tail counts can be discounted.
- **Caveat — file size:** ≤30 MB/day before rotation. If you see growth that exceeds this, the MVE filter regressed or `status="open"` isn't sticking.
- **Known gaps:** `live_watcher` per-tick scanning is not attributed (per-game, not per-scan; revisit when Session 13 ships). The `_active_series_tickers()` list in `bot/universe.py` is hand-maintained — if a new active scanner ships, add its series prefixes there or attribution will go missing.
- **Session 14:** every row carries `regime` (event_horizon_hr resolves from each row's close_ts).

### 11. `bot/state/order_microstructure.jsonl` — per-live-order microstructure (Session 15)
- **Inspect:** `wc -l bot/state/order_microstructure.jsonl 2>/dev/null && tail -3 bot/state/order_microstructure.jsonl 2>/dev/null | jq .`
- **Expect (PAPER_MODE=True, current state):** file does not exist, OR exists empty. **Paper trades do not write here by design.** If non-zero rows appear while PAPER_MODE=True, investigate immediately — the paper-mode regression test caught a regression.
- **Expect (PAPER_MODE=False, post-flip):** ≥1 row per live order at terminal status. Schema in `bot/order_microstructure.py` module docstring.
- **Run report (post-flip):** `python3 tools/microstructure_report.py --days 7` produces per-strategy slippage / latency / fill-rate breakdown plus slippage-adjusted CLV (joins to `clv.json` via `(ticker, ts_placed)` ±60s window).
- **Deferred verification (post-flip):** (1) First live order writes a row with all fields populated. (2) After 50 live orders: any strategy with median slippage > 2¢ or p95 latency > 5s is a Session-16+ tuning candidate. (3) Per-strategy `slippage_adjusted_clv` should match paper-CLV within ~1-2¢; divergence > 3¢ means paper-mode is over-optimistic and we need to bake slippage into paper simulation (its own session).
- **v1 known gaps:** (a) `slippage_source: "limit_price_echo"` means production slippage will read 0 until a `/portfolio/fills` endpoint integration (Kalshi's `place_order` SDK echoes limit price as `cost_dollars`). (b) Bot crashes between place_order and terminal observation lose that row (in-memory `_PENDING` dict is process-local). (c) Kalshi-side cancellation pruning returns errors that get_order swallows.
- **Session 14:** every row carries `regime` (event_horizon_hr resolves from the order's market close_ts).

### 12. `bot/state/bot_state.json` — universe partial-rate ratio (Session 15.5)
- **Inspect:** `python3 -c "import json; s=json.load(open('bot/state/bot_state.json')); t=s.get('total_snapshots_today',0); p=s.get('partial_snapshots_today',0); print(f'snapshots today: {t}, partial: {p} ({100*p/t if t else 0:.1f}%)')"`
- **Expect:** `total_snapshots_today` ratchets up across the day, resets at midnight ET via `last_universe_metering_reset`. `partial_snapshots_today` should stay near 0 — Kalshi cursor pagination usually exhausts within the 90s deadline.
- **WARN signal:** `bot/universe.py` logs a WARN when the trailing 10-snapshot window has ≥10% partial rate. Surfaces in `bot.log` as `universe partial rate elevated: N% over last 10 snapshots` — if you see it sustained, the bot is silently working with incomplete universe rows; investigate Kalshi rate-limiting (usually live_watcher polling competing for connections) or extend the 90s deadline.
- **Caveat:** an occasional partial during heavy live-game windows is normal. The 10% threshold is the bar at which downstream analysis (cohort_report, hypothetical back-tests) starts being biased by missing markets.

### Cross-cutting checks
- **Decisions ↔ CFs.** `decisions.jsonl` rejects in the last 30 min should produce ≤5 new CF records per scan in `clv.json` (top-K selection). If decisions has 200 rejects/scan but CFs aren't growing, CF emission broke.
- **Active strategies ↔ records.** Every strategy in `ACTIVE_STRATEGIES` ([bot/config.py:578](hustle-agent/bot/config.py:578)) should appear in `decisions.jsonl` within 1 hour. Missing = scanner not loading that strategy.
- **No silent loss.** `clv.json` records with `closing_yes_price=null` AND `recorded_at > 7 days ago` mean the settlement poller is stuck on those tickers. Investigate per-ticker.

### Known caveats and active gaps
- Sessions 7–11 all shipped (Apr 24–25). The data-quality stack is now closed-loop: decisions → CFs → predictions → settlements → reports.
- Calibration data needs ~7 days of settlements before `tools/calibration_report.py` Brier scores stabilize. Pre-Session-11 trades have no prediction record.
- `live_momentum` predictions are skipped (`predicted_fair_cents=None`) because Session 7 left a known coverage gap there — live momentum has no model-predicted fair value to log against. Surfaces as a hole in `predictions.jsonl` for that opp_type only.
- Anything before 2026-04-24 in `clv.json` may have polluted records (KXTEST, entry<3¢ CFs) that were cleaned but pre-cleanup counts in archives differ.
- `decisions.jsonl` started fresh on Session-6 deploy date and `predictions.jsonl` on Session-11 deploy date — historical scans before each is not reconstructible.
- Session 14 (Apr 25): `sport_phase` derived from a hardcoded date table in `bot/regime.py` (no ESPN integration); needs yearly bump. ATP/WTA/UFC/IPL/F1 return `null`.

---

## When Tyler Asks for the 7-Day Retuning Report

After ~7 days of unattended operation (first viable date: ~May 2, 2026 from Apr 25 deploy), Tyler will ask "what should we retune?" This checklist walks through the four reports, but **the four reports are not symmetric** — they produce wildly different signal quality on Day 7, and confusing them leads to the wrong conclusions. Read this framing first.

### The honest per-report confidence table

| Report | Day-7 confidence | Strategy coverage | What it actually tells you |
|---|---|---|---|
| `cohort_report` | **HIGH** | vig_stack-dominated | Gate-by-gate "edge surrendered by rejects" — the mis-tuning signal |
| `calibration_report` | **HIGH** | vig_stack only (live_momentum has no fair value) | Per-bucket Brier scores — is the fair-value model right? |
| `universe_report` | **HIGH** | All strategies | What market families we ignore — independent of trade volume |
| `excursion_report` | **WEAK to LIMITED** | Borderline-useful for vig_stack; **BLOCKED for live_momentum** until Session 17 fixes the median-1-tick cadence problem | Whether exit logic leaves alpha on the table — only if MFE/MAE has enough observations per position |

### What this means strategically

**Day 7 is a vig_stack story, not a live_momentum story.**

- **vig_stack_series is bleeding money** (−$110.62 over 54 trades). Day 7 reports will say a lot about it: which gates are mis-tuned, whether the fair-value model is biased, whether the entire strategy is structurally broken. **Real retuning value here.**
- **live_momentum is profitable** (+$12.30, 62% WR over 39 trades). Day 7 reports will tell us **almost nothing actionable** about it. Excursion is blocked by tracker cadence. Calibration is structurally absent (no fair value). Cohort has only 3 reject types so no "many gates need tuning" signal possible.

**For live_momentum, the path to real signal is engineering-blocked, not calendar-blocked:**
- Session 17 (tracker cadence audit) — without this, no MFE-based analysis works for sub-hour holds
- Session 18 (live_journal.json analysis) — actually-rich live_momentum data
- Session 19 (tick-replay back-tester, deferred) — the real answer

If the Apr 26+ arc has shipped Sessions 16-18 by Day 7, you'll have richer live_momentum signal from `journal_analysis.py` than from the Day-7 reports. Don't pretend May 2 is an oracle.

---

### The actual checklist

**1. Verify rotations fired correctly.**

```bash
ls bot/state/archive/*-2026-04-26.jsonl.gz
ls bot/state/archive/*-2026-05-01.jsonl.gz
```

If any day is missing, the data is partial — flag it before drawing conclusions.

**2. Run the high-confidence reports first.**

```bash
python3 tools/cohort_report.py --days 7
python3 tools/calibration_report.py --days 7
python3 tools/universe_report.py --days 7
```

These three produce trustworthy Day-7 signal. Spend 80% of analysis time here.

**3. Run excursion only as a sanity check, not for decisions.**

```bash
python3 tools/excursion_report.py --days 7
```

Sample size is borderline-useful for vig_stack and structurally meaningless for live_momentum (until Session 17 ships). If Session 16 has shipped, the gap math is at least correct; if not, treat any non-zero gap with deep skepticism.

**4. Run regime-sliced versions for the high-signal axes.**

```bash
python3 tools/cohort_report.py --days 7 --regime-by sport_phase
python3 tools/cohort_report.py --days 7 --regime-by time_of_day
python3 tools/cohort_report.py --days 7 --regime-by event_horizon_hr
python3 tools/calibration_report.py --days 7 --regime-by sport_phase
```

**5. If Sessions 17/18 have shipped, ALSO run journal_analysis for live_momentum.**

```bash
python3 tools/journal_analysis.py
```

This is where actual live_momentum retuning candidates surface — exit-reason mix, hold-time distribution, watch-but-no-enter funnel. Far richer than the Day-7 reports for our profitable strategy.

**6. What to look for in each.**

- **COHORT (vig_stack-dominated, high confidence):** gates with >50% reject rate AND positive mean CLV on rejects → mis-tuned (surrendering alpha). Distance-histogram: gates with >50% of rejects clustered <10% from threshold are boundary candidates for loosening. **This is where vig_stack retuning targets come from.**
- **CALIBRATION (vig_stack only, high confidence):** any strategy where predicted bucket [80, 90¢) resolves YES <70% → fair-value formula has systematic bias. Brier > 0.18 means the strategy is poorly calibrated and shouldn't size up. **No live_momentum row will appear; that's expected, not a bug.**
- **UNIVERSE (all strategies, high confidence):** ignored market families with >$100/day volume + spread >5¢ → candidates for new strategies via `tools/backtest.py --include-history`.
- **EXCURSION (low confidence, sanity check only):** if median(MFE − exit) > 5¢ for vig_stack, that's a real exit-logic candidate. For live_momentum, treat any number with extreme skepticism unless Session 17 confirmed cadence is healthy.
- **JOURNAL_ANALYSIS (live_momentum, available if Session 18 shipped):** exit-reason distribution, time-to-exit histogram, watch-but-no-enter funnel. **This is where live_momentum retuning targets come from.**

**7. Cross-strategy intersection (vig_stack only).**

For vig_stack: gates flagged by cohort AND calibration are top-priority retuning targets. Single-report flags are interesting but lower-priority. A gate that fails one lens may be an artifact; one that fails both is structural.

For live_momentum: cross-intersection doesn't apply. Use journal_analysis findings + (if Session 19 has shipped) tick-replay back-tester results as the primary source.

**8. Caveats.**

- `calibration_report` has zero live_momentum coverage by design (Session 7 noted no usable scalar fair value). This is a structural gap, not a data gap. Don't expect Day 14 / Day 30 to fix it — only a future "live momentum fair value proxy" session will.
- `excursion_report` is sample-limited for both strategies and cadence-broken for live_momentum until Session 17 ships.
- `sport_phase` is a hardcoded date table (`bot/regime.py:SPORT_PHASES`) — verify it's not stale (NBA playoffs end ~Jun 22, 2026; UFC isn't in the table by design).
- `event_horizon_hr` will be near-zero on rows written before Session 15.5 (the historical decisions.jsonl rows have null); slice on rows from Apr 25, 2026+ only for that axis.
- `partial_snapshots_today` from `bot_state.json` (Session 15.5): if any day in the window had a partial-rate WARN, that day's `universe_report` and `cohort_report` are biased toward markets that survived the truncated cursor; flag in writeup.
- **The Day-7 framing is convenient, not magic.** If Sessions 16/17/18 ship before May 2, run reports earlier and re-run after each session lands. If they don't ship by May 2, the Day-7 report is mostly a vig_stack-retuning report — which is still valuable, just don't oversell it as a "we now know what to do about live_momentum" moment.

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
| What strategies are active? | `bot/config.py:578` (`ACTIVE_STRATEGIES`) |
| What's the current mode? | `bot/config.py:569` (`PAPER_MODE`) |
| Why is a trade being blocked? | `bot/executor.py:121` (`_check_position_limits`) |
| How is edge calculated? | `bot/math_engine.py` |
| How are watchers started? | `bot/main.py:766` (`handle_watch`) + `bot/live_watcher.py:368` (`start`) |
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
| Where's the DQS (dip quality score)? | `bot/game_context.py` + `bot/config.py:136` (`MOMENTUM_DQS_THRESHOLD`) |

**"Why is X happening?"**

| Symptom | Likely cause | File |
|---|---|---|
| Every trade blocked with "Total exposure too high" | Account is over-allocated (not a bug, a math reality) | `executor.py:323` |
| Every trade blocked with "STRATEGY_BUDGET: …" | Per-strategy budget exhausted; another bucket has headroom | `executor.py:304-313` (Apr 16) |
| Trades keep blocked with "Already hold open position" | Duplicate entry guard, check if market actually settled | `executor.py:204` |
| Every live game edge shows the same price for 2 minutes | `bypass_cache=True` not being passed to `fetch_consensus_odds` | `live_watcher.py` |
| Paper trades all show `filled=0` | Paper fill bug regressed | `executor.py:execute_trade` (PAPER_MODE branch) |
| Two bot processes running | `RESTART` didn't cleanly kill | `kill -9 <pid>` |
| Bot stopped ticking | Crashed silently, check watchdog heartbeat | `bot_state.json.last_heartbeat` |
| `bot.lock` exists but no process | Stale lock from previous crash | Delete it and restart |
| Conviction entry never fires | Completion < 50%, or wrong sport, or wp_edge too small | `CONVICTION_*` in config + log output |

---

## Final Note

Glint is a production trading system that has lost real money and is slowly learning. Every config value is someone's scar. Every disabled strategy has a reason. When in doubt: read the comment, check `strategy_audit.json`, pull the evidence from `trade_history.json` or `live_ticks.jsonl` before changing anything. The data is the source of truth, not intuition.
