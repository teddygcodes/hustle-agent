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
| `bot.lock` | PID lockfile. Deleted on graceful shutdown. **Mtime advances each heartbeat (Apr 23, Session 5)** — fresh mtime + dead PID means the process was killed between scans without releasing the lock. |
| `bot_state.json` | Last scan time, heartbeat, scan counters, DK/FD disabled flags (12h TTL), Session-5 `last_ticks_rotation` flag. |
| `positions.json` | **Source of truth for open positions.** Exposure calc reads this. |
| `trade_history.json` | **Order log.** Every `execute_trade()` and `execute_hedge()` appends a record (filled OR resting). `tracker.resolve_trades()` updates entries in-place when markets settle. Distinct from `paper_trades.json` (the paper-mode resolution log). Read by the Telegram `HISTORY` command and `patterns.analyze_patterns()`. |
| `paper_trades.json` | **Paper resolution log.** Balance is reconstructed from this. NOT the same as `trade_history.json` (orders) — paper-mode resolutions live here, with `status ∈ {won, lost, exited_early}` driving the post-Session-1 settlement pipeline. |
| `archive/live_ticks-YYYY-MM-DD.jsonl.gz` | Daily gzipped tick archives. Created by `scheduler._rotate_live_ticks()` at midnight ET (Apr 23, Session 5). |
| `pending.json` | Queue of opportunities waiting for GO/SKIP. Max `PENDING_MAX = 20`. |
| `live_journal.json` | Live watcher journal: scan_found, bet, exit, session_end events. Feeds RECAP. |
| `live_ticks.jsonl` | Append-only **enriched** tick log: price, leader, wp, momentum, lead_trend, completion, wp_edge, bid, opp_bid, volatility, espn_scores, game_state. Feeds ANALYZE. |
| `strategy_audit.json` | **Read this to understand every strategy's real-money status.** Auto-updated by `tracker.py` on every settlement (append to `settlement_log`). |
| `patterns.json` | Historical win rate per strategy type for dynamic confidence (`scanner.py:_get_dynamic_confidence`). |
| `clv.json` | Closing-line value records per trade. **`clv._load()` filters to active strategies on read (Apr 23, Session 5)** — disabled-strategy records get dropped and never re-saved. |
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

**Apr 23 (Session 5):** `bot.lock` mtime now also advances each heartbeat (one-line `LOCK_FILE.touch()` after `_save_bot_state(state)` in the main scan loop). Two new failure signatures: (a) **fresh `bot.lock` mtime + dead PID** → process was killed between scans without releasing the lock (rm the lock and restart); (b) **`bot.lock` mtime > 15 min stale + `_pid_is_running` → True** → process is alive but the scan loop is wedged (check `bot.log` for stuck I/O or held asyncio lock).

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
