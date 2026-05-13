# HUSTLE AGENT — Project Guide for Claude Code

## Project Scope (Read First)

**Bot only.** Per user instruction in `~/.claude/projects/.../memory/project_scope.md`: the agent LLM reasoning loop in `agent/engine.py` is **excluded from all work**. Do not run, modify, extend, or debug the agent's LLM cycle. The `agent/` directory is kept around because it owns the **Kalshi REST client** (`agent/kalshi_client.py`), team-alias dicts (`agent/parlay.py`), and player stat helpers (`agent/player_stats.py`) — the bot imports from these. Everything else in `agent/` is legacy.

**The bot is the product.** It's called **Glint**, lives in `bot/`, and is what every session should focus on.

---

## Network Call Policy

The bot's live decision path makes external HTTP calls to a curated set of endpoints (Kalshi, ESPN team-sport scoreboards, NWS, TheRundown). Adding NEW external endpoints to the decision path requires explicit operator approval per session — see the "no new network calls" lineage from S96 / S107.

Current approved endpoints (each shipped with its own session and discipline):

- ESPN team sports (NBA / MLB / NHL / NFL / NCAAB) — pre-existing.
- ESPN cricket `cricket/8048/scoreboard` for IPL — **Session 112 (2026-05-11)**, scoped to IPL only. Rationale: IPL was the second still-profitable live_momentum cohort post-S97 (+$53.71 / 13 settled) and cohort observability stayed N-thin without rich `match_phase` state; S110 design-doc'd the integration and S112 shipped it under explicit operator relaxation.

Tennis match-level state, UFC live state, NHL `match_phase` source, and any future sport addition still require their own approved exception. **Do not infer general permission from any single approved case.** The "no new network calls" rule continues to apply by default; relaxations are one-off and named.

---

## What Glint Is

Glint is an autonomous prediction-market trading bot that runs 24/7 against **Kalshi** and takes edge across four active strategies. It's a pure Python `asyncio` application — one process, one Telegram bot interface, one orchestrator class (`GlintBot` in `bot/main.py`). No LLM in the trading loop. Every decision is deterministic math + safety checks + Kelly sizing.

**Starting capital:** $10,500 simulated (`PAPER_STARTING_BALANCE` in `config.py`). Bumped 500 → 10,500 on Apr 29, 2026 (+$10,000 deposit) to scale position sizes up for faster signal accumulation. All edge math (CLV in cents, win rates, gate thresholds) is balance-invariant; only Kelly sizing + dollar-magnitude caps (10% reserve floor, MAX_POSITION_PERCENT, STRATEGY_BUDGETS absolutes) scale 21× with the deposit. Historical −$98 paper P&L unchanged; reconstructed balance post-restart = $10,402. Real Kalshi account runs in parallel when `PAPER_MODE = False`.

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

### ACTIVE via live_watcher (separate from `ACTIVE_STRATEGIES`)

| Strategy | Location | Description | Real Perf (paper, Apr 18) |
|---|---|---|---|
| `live_momentum` | `live_watcher.py` | Buy dips on the clear leader in 1v1 live matches (UFC now; NHL via team-sport watchers — atp + nba_game disabled May 11 via Session 97). Disabled scope: `atp` + `atp_challenger` + `nba_game` + `wta` + `wta_challenger` (Session 97 added atp + nba_game on breakeven-WR analysis: atp -27.2pp gap / nba_game -16.9pp gap post-Session-54; main-tour atp was briefly re-enabled Apr 29 via Session 38a on CF evidence, then re-disabled May 11 via Session 97 on 22 real post-re-enable trades at -$45.60 / 40.9% WR — real money superseded CF projection). UFC held off at n=11 (watch-list trigger at N>=15). Leader floor is 0.65 (Session 19c lowered from 0.70 — see config.py:70). Auto-scans every 60s; 20% of equity via `STRATEGY_BUDGETS`. | **Apr 30 baseline** (Session 40): 73 settled, **−$40.42** (−$0.55/trade), 22W / 7L / 44 EE (60% EE rate). Apr 20 baseline was 39 settled / +$12.30. Session 40 Pattern C: exits saved $62 of incremental losses on the EE cohort — leak is structural (Win:Loss magnitude = 0.261), not exit-side. |
| `live_momentum` (conviction) | `live_watcher.py` | When there's no dip but game state screams value — wp_edge > 8%, positive momentum, 68-82¢ entry — buy anyway. NBA/NHL only (MLB 12% hit rate). | Rolled into live_momentum numbers above |

### DISABLED (data-driven kills)

All disabled strategies have `# Disabled: reason` comments directly below the `ACTIVE_STRATEGIES` list in `config.py` (around lines 585-605). Briefly:

- `series_game_edge` — 26% WR, sportsbook odds are efficient
- `weather` (single-market) — 17% WR, NWS bias model too imprecise for individual strikes (**note:** vig_stack applied to the same weather ladders works — that's different math)
- `btc/eth/sol/xrp/doge/bnb_price_edge` — all crypto disabled (`CRYPTO_ENABLED = False`), vol model overestimates intraday movement
- `live_latency_arb` — replaced by `live_momentum` watcher system (2-min scan too slow)
- `sports_monotonicity_arb` — Disabled Session 56 (2026-05-06): opportunity dict shape mismatch with executor — execution would be one-sided directional, not riskless. 0 history fills. Rebuild via paired execution (atomic both-legs-or-refund) when justified.
- `sports_consistency_arb` — Disabled Session 56 (2026-05-06): same shape bug as `sports_monotonicity_arb`. 0 history fills.

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
| `bot_state.json` | Last scan time, heartbeat, scan counters, DK/FD disabled flags (12h TTL), Session-5 `last_ticks_rotation` flag, Session-52 Telegram delivery health (`telegram_*` forward-only fields). |
| `positions.json` | **Source of truth for open positions.** Exposure calc reads this. |
| `trade_history.json` | **Order log.** Every `execute_trade()` and `execute_hedge()` appends a record (filled OR resting). `tracker.resolve_trades()` updates entries in-place when markets settle. Distinct from `paper_trades.json` (the paper-mode resolution log). Read by the Telegram `HISTORY` command and `patterns.analyze_patterns()`. |
| `paper_trades.json` | **Paper resolution log.** Balance is reconstructed from this. NOT the same as `trade_history.json` (orders) — paper-mode resolutions live here, with `status ∈ {won, lost, exited_early}` driving the post-Session-1 settlement pipeline. |
| `archive/live_ticks-YYYY-MM-DD.jsonl.gz` | Daily gzipped tick archives. Created by `scheduler._rotate_live_ticks()` at midnight ET (Apr 23, Session 5). |
| `decisions.jsonl` | **Per-decision audit log (Apr 24, Session 6).** Every scan-time accept/reject from scanner + executor + live_watcher with a gate fingerprint. Read by `tools/cohort_report.py`. Daily rotation to `archive/`. |
| `archive/decisions-YYYY-MM-DD.jsonl.gz` | Daily gzipped decision archives. Created by `scheduler._rotate_decisions_log()` at midnight ET (Apr 24, Session 6). |
| `shadow_trades.jsonl` | **Blocked-trade shadow evidence ledger (May 11, Session 95).** Append-only rows for blocked-but-measurable opportunities only: `family_disabled_reject`, `sport_disabled`, `reentry_blocked`. Forward-only; no backfill; no scheduler rotation yet. Read by analysis/status tooling, not execution. |
| `predictions.jsonl` | **Per-prediction fair-value vs. actual log (Apr 25, Session 11).** One row per opp evaluated (acted-on or counterfactual). Settlement poller fills `closing_yes_price`. Read by `tools/calibration_report.py`. Daily rotation to `archive/`. |
| `archive/predictions-YYYY-MM-DD.jsonl.gz` | Daily gzipped prediction archives. Created by `scheduler._rotate_predictions_log()` at midnight ET (Apr 25, Session 11). |
| `order_microstructure.jsonl` | **Per-live-order microstructure log (Apr 25, Session 15).** One row per live Kalshi order at terminal status (filled / canceled / rejected). Schema: `{ts_placed, ts_filled, ts_canceled, requested/filled price+qty, slippage_cents, slippage_source, latency_ms, queue_depth_at_place, partial_fill_count, strategy_name, opp_type, ticker, side, terminal_status, kalshi_order_id, regime}`. Read by `tools/microstructure_report.py`. **Empty until PAPER_MODE=False.** Paper trades do NOT write here — paper write path is unchanged. Daily rotation to `archive/`. |
| `archive/order_microstructure-YYYY-MM-DD.jsonl.gz` | Daily gzipped microstructure archives. Created by `scheduler._rotate_order_microstructure_log()` at midnight ET (Apr 25, Session 15). |
| `pending.json` | Queue of opportunities waiting for GO/SKIP. Max `PENDING_MAX = 20`. |
| `live_journal.json` | Live watcher journal: scan_found, bet, exit, session_end events. Feeds RECAP. Session 96 adds compact live_momentum no-entry context under `scan_found.extra` forward-only. |
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
The watchdog (`run_bot.sh`) and launchd both try to keep the bot alive. If the Telegram `RESTART` command kills the process without cleaning up cleanly, a zombie can linger.

**CRITICAL — DO NOT use `ps aux | grep bot.main` (cross-bot collision risk per Battle Scar #14).** Bob also runs `python3 -m bot.main` because each repo names its package `bot/`. Bare-module-name greps match EVERY fleet bot. Killing what looks like a Glint orphan can take Bob down (and vice versa — both directions happened May 3, 2026).

Use the **path-rooted** pattern instead:

```bash
# Find Glint's bash wrapper PID (path is unique to Glint)
WRAPPER=$(pgrep -f "Desktop/hustle-agent/hustle-agent/run_bot.sh")

# Find Glint's bot.main child PID
BOT_PID=$(pgrep -P "$WRAPPER")

# Verify exactly one of each
echo "wrapper=$WRAPPER bot_pid=$BOT_PID"
```

Or as a status one-liner: `ps aux | grep "Desktop/hustle-agent/hustle-agent" | grep -v grep` — should return exactly **one** `bash run_bot.sh` line for Glint specifically. The `Python -m bot.main` child does NOT appear in path-rooted `ps aux` output because its command line is just `Python -m bot.main` with no repo path (CWD is set by launchd but doesn't show up in `ps aux`). Wrapper-presence is sufficient signal — launchd KeepAlive guarantees child respawn if the python dies. To verify the child PID specifically, use the parent-chain pattern below.

If two Glint PIDs appear under the same wrapper, `kill -9` the older one (use `ps -o pid,etime,command -p $(pgrep -P "$WRAPPER")` to identify oldest). For service-level restarts, use `launchctl kickstart -k gui/$(id -u)/com.hustle-agent.bot` — the launchd label is also bot-unique and a safe target.

**Lock-empty race fix (May 3, 2026 follow-up):** `_release_lock()` in [bot/main.py:204](bot/main.py:204) now PID-guards before unlinking — only deletes the lockfile if it still contains our own PID. Prevents the race where an old orphan process's SIGTERM handler unlinks the lockfile that a NEWLY-spawned process has already overwritten with its own PID, leaving an empty lockfile after the new process's next periodic `LOCK_FILE.touch()`. 5 regression tests in `tests/test_main.py::test_release_lock_*`.

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

### 9. Vig stack auto-exit exemption (edge_flipped, take_profit, cut_loss)
Vig stack positions (`opp_type in ("vig_stack_no", "vig_stack_series")`) are exempt from ALL three auto-exit paths in `bot/main.py`'s position-loop: `edge_flipped` (from `recheck_open_edges`), `take_profit`, and `cut_loss` (both from `update_positions` at TP=+50%/SL=-30% thresholds). Reason: the edge is structural — YES sum > 100¢ on the ladder. Individual contract prices moving doesn't invalidate the ladder math; only a collapse of the entire ladder's vig would. The single source of truth is the `_VIG_STACK_OPP_TYPES` tuple at the top of `bot/main.py`. Each branch logs `"<path> SKIPPED for <ticker> (vig_stack — structural, hold to settlement)"` and continues — `recheck_open_edges` and `update_positions` still compute the alerts; we just don't act on them. **Session 36 (2026-04-29)** extended the exemption to TP/SL after Session 35's first weekly report flagged `non_stable_below_weather_floor` as mis-tuned: floor gate was actually doing its job by blocking entries that would get killed by an inappropriate `cut_loss` path before settlement (paper data showed 32% early-exit rate at -$5 to -$10 mean P&L across ALL families including stable ones, with median hold 23h — slow drift exits, not snap kills). Same session also added `exit_reason` persistence on `paper_trades.json` records (via `executor._paper_record_exit(reason=...)`) so we can finally distinguish auto_take_profit / auto_cut_loss / edge_flipped / manual paths in audits.

### 10. NO at 90-95¢ risk/reward
Vig_stack's cheap NOs are often in the 89-95¢ range. That's 8:1 to 19:1 risk/reward against you. Even at ~50% real WR on volatile ladders, the math collapsed — 43 trades net −$101 on paper. **Filter F (Apr 18)** is the answer: whitelist stable families (`VIG_STACK_STABLE_FAMILIES = {"KXHIGHMIA","KXHIGHAUS","KXINX"}`), and require NO ≥ 0.90 (`VIG_STACK_WEATHER_MIN_PRICE`) on anything else. Do not weaken these without 48h of post-filter data.

### 11. `STRATEGY_BUDGETS` (Apr 16) — per-strategy exposure caps
`config.py:STRATEGY_BUDGETS = {"vig_stack": 0.60, "live_momentum": 0.20, "arbs": 0.20}`. Enforced in `executor._check_position_limits` against **equity** (`balance + total_exposure`), not just cash. Pre-Apr-16, a single 100% pool let vig_stack starve conviction trades indefinitely. Rejections surface as `STRATEGY_BUDGET: <strategy> has $X of $Y budget` in logs. Don't remove — live_momentum can't fire without it.

### 12. Settlement log idempotency (Apr 18)
`tracker._log_settlements_to_audit` is now idempotent on `(ticker, strategy, result, pnl, contracts)`. Pre-fix, re-running `resolve_trades` on already-settled positions double-counted — one ticker had 14 duplicate entries. If you touch that function, keep the dedup check in or the strategy totals will drift again.

### 13. Synchronous I/O inside async coroutines blocks the event loop (Session 39, Apr 30)
Apr 30 incident: `_universe.snapshot_universe(scan_id)` was called directly from `_main_loop()` (an async coroutine). `snapshot_universe` is fully synchronous — `requests` calls + `_time.sleep()` retry sleeps in the cursor walk. Healthy Kalshi: ~3-15s per snapshot, fine. Flaky Kalshi (constant `Connection reset by peer` + read-timeout errors) on Apr 30: 38-48s × 800+ pages = 3-hour wedges, twice in a row. While each wedge ran, `_live_scan_loop` / `_heartbeat_loop` / `_position_check_loop` / Telegram polling all starved — the bot was technically alive (PID 38402) but functionally dead for 12+ hours. Symptom: zero Telegram notifications since 23:54:36 ET Apr 29; `bot.lock` mtime + `bot_state.last_heartbeat` both stuck for hours.

**Rule:** any synchronous I/O (`requests`, `_time.sleep`, blocking SDK calls) inside an async coroutine must be wrapped in `loop.run_in_executor`. Pattern used at [bot/main.py:923, 1082, 1196, 1408, 1411](hustle-agent/bot/main.py:1196):
```python
loop = asyncio.get_event_loop()
await loop.run_in_executor(None, lambda: sync_fn(arg))
```
If you write a new async loop and it calls into `bot/universe.py`, `bot/scanner.py`, `bot/tracker.py`, `bot/executor.py`, or anything that hits Kalshi: assume it's blocking, wrap in executor, audit at PR time. The Session 39 regression test at [tests/test_main.py:test_main_loop_runs_snapshot_universe_via_executor](hustle-agent/tests/test_main.py) asserts `snapshot_universe` runs on a non-`MainThread` worker thread — if a future refactor regresses it back to a direct sync call, that test fails.

**Session 98 (2026-05-11) addendum — `run_in_executor` does not bound the awaiting coroutine.** `await loop.run_in_executor(...)` keeps OTHER coroutines responsive but the calling coroutine still blocks until the executor returns. Phase 0.1 found a 479-min `_main_loop` stall on May 10 03:08-11:07 UTC where `snapshot_universe` overshot its internal 300s deadline by hours (per-page deadline check trips only AFTER each slow page; layered Kalshi-client + urllib3 retries can make a single page take 60-120s, so 666 pages × ~38s = 7+ hours). `_heartbeat_loop` kept ticking, live_watcher kept firing, but `_main_loop`'s scan section was wedged. The Session 98 fix wraps `loop.run_in_executor(None, snapshot_universe)` in `asyncio.wait_for(..., timeout=_SNAPSHOT_OUTER_TIMEOUT_SEC=600)` at [bot/main.py:1290](hustle-agent/bot/main.py:1290); on `asyncio.TimeoutError` it logs, increments `bot_state.snapshot_outer_timeout_count_24h`, sleeps 60s, and continues. Regression test at [tests/test_main.py:test_main_loop_aborts_long_snapshot_after_outer_timeout](hustle-agent/tests/test_main.py) asserts the counter increments and the recovery sleep fires. **Caveat:** the executor thread continues running after timeout (Python `concurrent.futures` can't cancel running threads); for rare 1-2x/day timeouts this is acceptable, but if `snapshot_outer_timeout_count_24h ≥ 3/day` persists for ≥ 3 days, ship per-request HTTP timeouts in `agent/kalshi_client.py` to bound the leaked work at its source.

### 14. Cross-bot PID identification — bare `bot.main` grep matches OTHER bots in the fleet (May 3, 2026)

Tyler runs multiple trading bots in the fleet (Glint = `~/Desktop/hustle-agent/hustle-agent/`, Bob = `~/Desktop/bob/`, future bots TBD). Each bot's main entry is invoked as `python3 -m bot.main` because each repo names its package `bot/`. Result: **`ps aux | grep bot.main` matches EVERY bot's process, not just Glint's.** Same for `pgrep -f "bot.main"`, `pkill -f "bot.main"`, etc.

**Real incident — both directions, same day (May 3, 2026):**
- ~13:45 ET: Bob's coder during Bob Session 2.5 dry run saw two `python3 -m bot.main` PIDs in `ps aux`, identified one as a Bob multi-PID orphan, killed it. The killed PID was Glint's bot.main (PID 82747). Glint's launchd KeepAlive respawned ~5s later as PID 74112; no data loss (Sunday, market closed).
- ~13:51 ET: Glint planner (this CLAUDE.md author) saw a separate PID 48988 in `ps aux`, identified it as a Glint orphan, killed it. PID 48988 was actually Bob's hung process from a Session 2.5 verify pass. Same root cause, opposite direction.

**Same root cause both directions:** the operator-facing identification command can't distinguish bots when they share the module name. As the fleet grows (3rd, 4th bot), this gets WORSE.

**Fix (apply everywhere — Glint's playbooks updated May 3):**

For status checks, use the path-rooted filter — note this catches the **bash wrapper** only, not the python child (the child's cmdline has no repo path; CWD doesn't appear in `ps aux`). Wrapper-presence is sufficient signal because launchd KeepAlive auto-respawns the child if it dies:
```bash
ps aux | grep "Desktop/hustle-agent/hustle-agent" | grep -v grep    # Glint wrapper
ps aux | grep "Desktop/bob" | grep -v grep                            # Bob wrapper
```

To **see both** the wrapper and the python child for a single bot, use the parent-chain (this also gives you the actual PIDs to operate on):
```bash
WRAPPER=$(pgrep -f "Desktop/hustle-agent/hustle-agent/run_bot.sh")
ps -p $WRAPPER -p $(pgrep -P "$WRAPPER")   # shows wrapper + child together
```

For surgical kills (when a process must be terminated by PID), find the bot's launchd-managed bash wrapper first, then walk to the python child:
```bash
WRAPPER=$(pgrep -f "Desktop/hustle-agent/hustle-agent/run_bot.sh")
BOT_PID=$(pgrep -P "$WRAPPER")
kill "$BOT_PID"
```

**Never use `pkill -f bot.main` or `pgrep -f bot.main` — these match every bot in the fleet.**

The launchd service label is also bot-unique and a safe target for restart operations:
```bash
launchctl kickstart -k gui/$(id -u)/com.hustle-agent.bot   # Glint
launchctl kickstart -k gui/$(id -u)/com.bob.bot            # Bob (when configured)
```

Battle Scar #3 (single-PID enforcement) was updated May 3 to use the path-rooted pattern; this entry is the structural reminder for future bots added to the fleet.

**Operator note (Session 83):** seeing two `python -m bot.main` processes in `ps aux` is NOT automatically Battle Scar #3 — verify the working directory of each PID via `lsof -p <PID> | grep cwd` before flagging an orphan. The Bob bot at `~/Desktop/bob/` runs the same `python -m bot.main` invocation and is launchd-managed independently; only flag an orphan when two PIDs share the same working directory tree. The Session 81 spawn_task chip and my Session 83 morning panic both came from skipping this cwd check.

### 15. Telegram 429s are state, not noise (Session 52, May 3)

May 3 incident: Telegram cooled Glint down after sustained `editMessageText` volume. The old notifier caught the 429, logged a warning, set an in-memory `_flood_until`, and then silently dropped the message after one failed attempt. Worse: edits were not counted by the 20-messages/60s send limiter, so live-card updates could hammer Telegram while sends looked "rate limited" on paper. Symptom: thousands of edit attempts over 24h, no durable `bot_state.json` signal, and every bot restart extended the Telegram cool-down Tyler was waiting out.

**Rule:** all Telegram send/edit calls go through `TelegramNotifier._telegram_call(...)`. On 429/`RetryAfter`, parse retry-after, persist `telegram_throttled_until` + increment `telegram_throttled_count_24h`, sleep, and retry with a fresh coroutine. On transient PTB network errors (`NetworkError`/`TimedOut`), retry with backoff. On success, stamp `telegram_last_send_success_at`; on every attempt, stamp `telegram_last_send_attempt_at`.

**Edit discipline:** `edit_message_by_id` uses a per-chat `EditThrottle` (1/sec sustained, burst 5) plus message-id keyed SHA1 dedup. Dedup records only after a successful edit. Never add a second Telegram state path or a second cooldown field; `_flood_until` is the in-memory mirror, `bot_state.json:telegram_throttled_until` is the operator-facing state.

**Operational note (post-Session-71):** Restart Glint anytime, cooldown or not. On startup the notifier loads `bot_state.telegram_throttled_until` and restores `_flood_until` if a future cooldown is persisted. The startup announcement and any other early sends respect the restored cooldown via the existing pre-send check at `_check_flood()`. Pre-Session-71 the field was written but never read on init, so restarts during cooldown re-hit the 429 and extended the outage — that constraint is retired.

### 16. live_momentum sizing must use the configured paper bankroll, not historical constants (Session 54, May 5)

May 5 correctness review found production `live_watcher._auto_bet_momentum` still reconstructing paper balance as `$500 + realized_pnl` even though `PAPER_STARTING_BALANCE` was bumped to `$10,500` on Apr 29. Result: every live_momentum Kelly sizing decision after the bump operated at roughly 9% of intended scale, while vig_stack / arbs used the executor-side balance path correctly.

**Rule:** never hardcode bankroll constants in strategy-local sizing paths. If a strategy needs paper balance, use the configured `PAPER_STARTING_BALANCE` or a canonical shared helper if one exists. Session 54 intentionally did NOT invent a new helper because `_check_balance()` mixes reconstruction with admission/reserve checks; the current production path is [bot/live_watcher.py:1119](hustle-agent/bot/live_watcher.py:1119) `_current_momentum_balance()` feeding [bot/live_momentum_sizing.py](hustle-agent/bot/live_momentum_sizing.py). S135 completed the second half of the sizing fix: S54 correctly bankroll-anchored the call, but active live_momentum sizing was still using a literal helper confidence and omitting `family` until S135 wired S50 confidence plus ticker-derived sport/family into `kelly_size()`.

**Analysis consequence:** Session 19c and Session 49 live_momentum sizing evidence is provisional until 14 days of post-Session-54 data accumulates. Session 130's sizing-axis conclusion is additionally contaminated until post-S135 data accumulates because the active helper was not receiving the trade's actual S50 confidence/family inputs.

### 17. Watcher asyncio tasks must be cancelled before notifier teardown (Session 58, May 7)

May 7 incident: each bot restart between 23:43:45 and 00:00:10 May 6-7 produced ~234 `RuntimeError('This HTTPXRequest is not initialized!')` errors over a 16+ minute window, even though only one Application/Bot existed. The OLD process logged "Bot stopped" almost immediately, but its `LiveGameWatcher` asyncio tasks (spawned via `_live_scan_loop` / `handle_watch` as standalone `asyncio.create_task`, NOT in the `gather()`'d task list) kept running their `while self.active: await self._tick_momentum(); await asyncio.sleep(LIVE_POLL_INTERVAL)` loops on a notifier whose HTTPXRequest had just been shut down by `await self.app.shutdown()`. Each tick fired `notifier.edit_message_by_id` → fail → Session 52 retry-with-backoff logged a warning + 2 retries + final error → next tick same cycle. Session 52's hardening is correct for transient failures but masked this lifetime bug as warning spam instead of a crash that would surface in `_run_watcher`'s exception handler. The OLD process's `asyncio.run()` finally GC'd the orphaned tasks ~16 minutes later — at which point errors stopped.

**Rule:** in `GlintBot.stop()`, iterate `self._active_watchers` and call BOTH `watcher.stop()` (sets `self.active = False`) AND `task.cancel()` (interrupts in-progress `asyncio.sleep`) for every entry, **then** await with a 5s timeout for cleanup, **then** call `self.notifier.stop()`. Pattern mirrors `handle_unwatch`'s discipline. Ordering is load-bearing: if notifier is torn down first, in-flight ticks fire HTTPXRequest errors during the unwind window. Implementation at [bot/main.py:397-435](hustle-agent/bot/main.py:397).

**Verification (May 7):** acceptance gate measured 121 errors in 2-min post-restart window with the bug → **2 errors** with the fix (98.3% reduction); zero errors after 10s past stop boundary. The 2 residual errors are from `send_message` (not `edit_message_text`) — a smaller race for in-flight messages that complete within `_live_scan_loop`'s announce path during stop. Out of scope for this fix; tracked as a separate bounded race.

**Operating Posture observation: self-healing infrastructure (Session 52 retry-with-backoff) can mask underlying lifetime bugs that would otherwise surface as crashes.** When adding hardening, also add a regression test for the underlying root cause, not just the symptom.

Regression tests at `tests/test_main.py::test_stop_cancels_*` (5 cases — cancel/order/empty/exception/timeout). Property test for the post-restart 0-error gate is the live verification.

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

**Apr 29, 2026 — capital deposit:** `PAPER_STARTING_BALANCE` bumped 500 → **10,500** (+$10,000). Reconstructed paper balance post-restart = **$10,402** ($10,500 starting + −$98 historical). All future paper trades will have ~21× larger Kelly-sized positions; STRATEGY_BUDGETS absolute amounts scale 21× (vig_stack 60% = $6,241; live_momentum 20% = $2,080; arbs 20% = $2,080); 10% reserve floor = $1,050. Pre-bump trades (timestamp < 2026-04-30) are denominated in the $500-balance era; post-bump trades will produce ~21× larger dollar P&L per trade. **All edge math and CLV signals (cents-based) are scale-invariant** — the Sessions 19c / 30 / 36 / 38a retuning decisions are robust. May 13 Session 38a re-validation routine updated to filter `paper_trades.json` to `timestamp >= 2026-04-30` for the post-bump ATP cohort comparison.

**Apr 30, 2026 — live_momentum cohort flipped negative (Session 40):** Apr 30 ground truth from `paper_trades.json` is **73 settled live_momentum trades, −$40.42 (−$0.55/trade), 22W / 7L / 44 EE (60% EE rate)** — down from the Apr 20 baseline of 39 settled / +$12.30 / 62% WR. Session 40 Phase-1 bucket diagnosis: 4 of the 7 active exit paths fired on the cohort (stop_loss n=21, take_profit n=17, near_settle n=3, trailing_stop n=1; plus 2 NO_JOURNAL_MATCH cases from Session 29-followup watcher-restart artifacts). Counterfactual analysis via `clv.closing_yes_price` (100% coverage) showed total cohort Δ/trade = **−$1.41** — holding all 44 EE'd trades to settlement would have lost $62 MORE, not less. The dominant buckets (stop_loss + take_profit = 86% of cohort) BOTH had negative deltas — exits saved money. Pattern C declared. The structural leak is **Win:Loss magnitude = 0.261** (avg win +$3.41 vs avg loss −$13.10; 7 lost-class trades alone net −$91.68, more than wiping out the 22 wins). Direction-setting conclusion for future sessions: *exit logic is balanced; investigate STRATEGY (entry gates, sport scope, sizing) rather than EXITS*. Watch-list trigger: re-investigate when EE cohort ≥ 80 trades OR per-trade P&L ≤ −$1.00.

The Apr 18 numbers (43 vig_stack / 16 live_momentum) were "honest" given the then-visible data but missed 50 exited_early trades. Don't trust any pre-Apr-20 summary for early-exit strategies.

**Why vig_stack is negative:** of 54 settled trades, 25 closed at a loss — the weight concentrated in volatile ladders. Ground truth by family: volatile (`KXHIGHDEN/NY/CHI`) = 36 trades, −$126.88, 69% early-cut; whitelist (`KXHIGHMIA/AUS/INX`) = 18 trades, +$16.26. Apr 18 Filter F set the volatile floor at NO ≥ 0.90; Apr 20 Session 2 raised it to **0.93** after bucket analysis showed only [92-96¢) is breakeven. Going forward we expect `real_pnl` to drift positive on new volatile-family trades. If a post-0.93 cohort of 10+ still prints negative, escalate.

**May 13, 2026 — vig_stack flipped strongly positive (Session 133 re-audit).** Apr-20 ground truth above held only through the apr20→apr29 window (N=65 / mean −$1.57 / total −$101.87 / WR 75.4%) — vig_stack was still leaking until the Session 36 TP/SL exemption + bankroll bump shipped on 2026-04-29. **Post-Apr-20 settled cohort today: N=169 / total +$1,064.57 / mean +$6.30/trade / WR 82.2%.** The apr29→may10 sub-period alone carries +$1,077.91 on N=93 / mean +$11.59/trade / WR 86% — the engine. Per-family post-Apr-20 leaders: KXHIGHAUS +$484.18 / N=48 / 83.3% WR (highest by total); KXMLBGAME +$264.65 / N=10 / mean +$26.47/trade (highest per-trade — `vig_stack_futures` path, see Open Loops). The S93 disable set (KXHIGHCHI/KXINX) was reasonable but not catastrophically beneficial — each disabled family was −$1.7 to −$2.6/trade pre-disable. **Battle Scar #9 (vig_stack auto-exit exemption) fully intact:** post-S36 stable+volatile EE rate is exactly 0.0% on N=95; the 6 EE trades carrying `exit_reason` are all `vig_stack_futures` (deliberately not in the exemption tuple). The 0.93 volatile floor is doing its job at admission (post-Apr-20 floor sweep at 0.94/0.95 is non-monotone — sampling noise, do not tune). Session 133 entry in [CLAUDE-sessions.md](CLAUDE-sessions.md) has the full 11-analysis breakdown.

**Why live_momentum is positive:** NBA + NHL alone = +$19.60 on 10 trades. Tennis was the drag: 72% of momentum volume for −$6.20 net (ATP Challenger −$7.80 / 82% cut, WTA −$10.20 / 67% cut — current paper_trades.json is n=6 / 1W/1L/4EE; the original Apr-20 cohort was n=7 / 1W/1L/5EE / −$7.00, since restated by Session 38a-2 audit). Apr 20 Session 2 added `MOMENTUM_DISABLED_SPORTS = {atp, atp_challenger, wta, wta_challenger}` (blanket tennis kill). **Apr 29 Session 38a removed `"atp"` (main tour) from the disable set** after settled-CF re-run showed +11.32¢ mean CLV at n=56 with n_no_won=10 + 4 historical pre-disable trades net +$8.60 (3W/1L); the precautionary bundling lacked direct main-tour evidence. **May 11 Session 97 re-disabled `"atp"` AND added `"nba_game"`** on per-sport breakeven-WR analysis (atp 22 real post-re-enable trades net -$45.60 / 40.9% WR vs 68.2% BE; nba_game n=26 / -$44.97 / 53.8% WR vs 70.7% BE). Both DISABLE-confirmed post-Session-54. Current disable set: `{atp, atp_challenger, nba_game, wta, wta_challenger}` — UFC at n=11 held off (watch-list trigger at N>=15). Session 2 also briefly raised `MOMENTUM_LEADER_MIN` from 0.70 to 0.75 to "skip the [75-80¢) dead zone" — but MIN is a floor, so 0.75 admits the dead zone while surrendering the positive [70-75¢) bucket. Reverted to 0.70 same day. **Session 114 (2026-05-11) verified the dead zone has disappeared in current data:** the [75-80¢) bucket is now +$10.17 / 22 trades / 59% WR in the post-S97 cohort, driven by S97's ATP re-disable removing the single trade (-$15.40) that drove the historical signal. No dead-zone filter shipped; Open Loops entry closed. `STRATEGY_BUDGETS` (live_momentum: 20% of equity, wired Apr 16) also stopped conviction trades from being starved by vig_stack's pool.

**Open exposure** (from positions.json, check at session start): ~10-16 open positions, mostly vig_stack. Whitelist families (`KXHIGHMIA` / `KXHIGHAUS` / `KXINX`) enter freely; volatile families (`KXHIGHDEN` / `KXHIGHNY` / `KXHIGHCHI`) now require NO ≥ 0.93 (post–Session 2). Any already-open position with a pre-0.93 entry continues to exit on normal rules — the floor gates entries, not exits.

**Settlement idempotency (Apr 18):** `_log_settlements_to_audit` in `tracker.py` had a bug — every call appended to `settlement_log` without dedup, so `resolve_trades` re-runs on already-settled positions double-counted. One ticker had 14 duplicate entries. Fixed with a `(ticker, strategy, result, pnl, contracts)` fingerprint check; the strategy totals also skip on dup so rollups stay clean.

---

## Session-by-Session Changelog

The full session-by-session changelog has moved to [CLAUDE-sessions.md](CLAUDE-sessions.md). Most recent ship: Session 106 (2026-05-11).

**Future session entries append to `CLAUDE-sessions.md`, not this file.** CLAUDE.md is the operator manual; CLAUDE-sessions.md is the historical log. When you ship a new session, the ☑ block goes there.


---

## Operating Posture: Always Search for New Possibilities (read FIRST)

**The bot is a search problem, not a maintenance problem.** Default to investigation, not preservation.

**The trigger that wrote this section:** Apr 30 the bot made +$172 on a vig_stack_futures trade (KXMLBGAME-26APR291840SFPHI-PHI). Investigation revealed two cancelling bugs (scanner mis-classifying per-game MLB winners as `vig_stack_futures` + Session 36's exemption-set missing `vig_stack_futures`) that together produce correct behavior on a misunderstood market type. Claude's first instinct was "lock in current behavior with a docs note." Tyler's correct instinct was "investigate the mechanism, find more like it, lean into it." This section exists so future-Claude doesn't repeat the defensive reflex.

**Rules of operating posture:**

1. **Unexpected profit is a LEAD, not a fact.** When the bot makes money via a path the docs don't fully describe, the FIRST move is to investigate the mechanism. Do NOT propose changes that "lock in" the behavior before understanding it. Do NOT propose docs notes that prevent future fixes — those become future-cement.

2. **Bug-pairs that produce profit are FINDINGS.** If two bugs cancel out into correct behavior, the question isn't "how do we preserve the bug-pair." The question is: "what's the actual mechanism, and can we trigger it intentionally / find more of it?"

3. **When something works, ask 'where else could this work?' BEFORE asking 'how do we lock it in?'** Example: if vig is being found in MLB per-game winners, check whether NHL/NBA per-game winners have the same pattern but aren't being scanned.

4. **Defensive instincts ('don't break what's working') are weaker than investigative instincts.** "Don't change anything" is the wrong default. The right default is "understand it, then decide whether to lean in OR fix it OR leave alone, in that priority order."

5. **The bot has a search frontier — keep it active.** At any given time, there are markets we don't scan, opp_types we haven't tried, parameters we haven't swept, and outcomes we haven't measured. Treat each one as a potential edge until proven otherwise. Session 12's universe log + Session 13's hypothetical strategy framework + Session 19's tick-replay back-tester exist specifically to make this cheap. USE THEM.

6. **Negative findings ARE findings.** Three Pattern C "no fix" outcomes in 24h (Sessions 40 / 41 / 42) ruled out exit-side framings. That's progress, not failure — it narrows the search. The discipline is what made those Pattern Cs honest. But don't stop searching just because one direction was ruled out.

7. **When daily/weekly reports surface unexpected P&L, STOP and investigate the mechanism before doing anything else.** A vig_stack_futures trade making +$172 on a misclassified market type is a 5-minute investigation that could surface a real edge. The report won't tell you what's interesting — you have to look.

**Concrete behaviors this implies:**

- When you see a trade in `paper_trades.json` whose outcome is much better/worse than expected, run a counterfactual: "what would have happened on the OTHER decision (held to settlement / different exit / different entry)?"
- When the daily report shows P&L attribution by strategy, also pull the BREAKDOWN BY TICKER and check for outliers.
- When a sweep returns Pattern C, ask: "is the parameter axis we swept the right one? are there orthogonal axes we haven't tried?"
- When you draft a session prompt, include the "lean in" branch alongside the standard A/B/C decision tree: "Outcome D — find more opportunities like this one, expand the scanner / strategy."
- Maintain a mental "search frontier" list. When CLAUDE.md gets updated, add to it: ignored market families from universe_report, undocumented opp_types, parameters never swept, sport-specific behaviors not yet investigated.

**Counter-example (when defense IS the right move):**

Battle Scar exemptions (#9 vig_stack auto-exit, #5 edge price basis, #12 settlement idempotency) preserve EXPLICITLY-DOCUMENTED known-correct behavior against accidental regression. Defense is correct when there's a paper trail showing WHY the current behavior is correct. Defense is WRONG when the current behavior just happens to work and we don't know why — that's where investigation belongs.

**Tyler's frame (his words, paraphrased):** "Always look for new possibilities, don't be stuck and tied to what we are doing." This is the prime directive. Every session should ask "what new edge could I find?" before asking "what should I preserve?"

---

## Strategy Termination Rules

Pre-committed kill criteria for active strategies. When a criterion fires, the strategy retires unless a documented exception is shipped in the same session that the trigger fires.

### live_momentum

**Trigger date:** 2026-06-15.

**Required N:** >=80 settled trades on the post-S97 cohort filter:
`status in {won, lost, exited_early}; sport NOT in MOMENTUM_DISABLED_SPORTS; timestamp >= 2026-05-11`.

**Required signal:** bootstrap 95% CI on per-trade EV must exclude 0 from below, i.e. lower bound > 0.

**On trigger fire:**

- If signal met: continue running and set a new trigger date 30 days out with the same criteria.
- If signal NOT met: ship a session that adds `"live_momentum"` to a new `MOMENTUM_RETIRED` set and comments out the strategy from `ACTIVE_STRATEGIES` or the equivalent disable mechanism.

**Documented exceptions that re-set the trigger without retiring:**

- New substrate work shipped within 14 days of the trigger date that materially changes the strategy, e.g. S138 dip classifier or fair-value model rewrite. Cite the session.

**Rationale:** S40 (Apr-30) through S132 (May-13) tested five leak hypotheses; all returned null at the cohort N available. Cross-AI review (S134) identified the no-termination-criterion discipline gap as foundational. This rule forces a decision rather than indefinite investigation.

---

## Planner Investigation Discipline (read THIRD — before opening any new investigation arc)

Conversation-level operating patterns the planner agent should apply across sessions. These are lessons from ~135 sessions of operator collaboration, distinct from the code-level Battle Scars and the data schema reference. They sit alongside the Operating Posture rule.

### Investigation premise contamination — verify the analysis is testing what it claims

Before declaring a hypothesis "ruled out" or an axis "closed," verify the analysis was actually testing what it claimed.

**S130 example (canonical).** S130 declared the live_momentum sizing axis closed based on Pearson +0.078 correlation between Kelly fraction and P&L. S135 verified the active path and found the live_momentum helper was passing literal `confidence=0.80` and omitting `family` across the measured cohort; the `confidence=0.75` call flagged in the cross-AI review was a stale `live_latency_arb` WATCH-path reference. S130's test wasn't measuring fully wired sizing behavior; it was measuring a contaminated input path. The "axis closed" claim was structurally invalid.

**Phase 0 must verify the analysis inputs match what the analysis claims to be testing.** If a sweep tests parameter X, confirm X is actually the variable being modulated in production. If a correlation test pairs A with B, confirm A and B are both varying as expected. Discovery of a contaminated test requires re-classifying the prior outcome to "untested" and re-running with proper inputs.

This rule is upstream of any cumulative ruled-out arc. Five "ruled-out" axes can become four-and-a-half if any one was contaminated.

### Tuning vs substrate — when null tunings accumulate, suspect substrate gap

Investigations come in two shapes:
- **Tuning** — test parameters within the current architecture (e.g., S41 TP/SL sweep, S130 sizing analysis, S132 sport-scope concentration)
- **Substrate** — build the missing piece the architecture lacks (e.g., a fair-value model where one doesn't exist; a context-classifier that distinguishes state-confirmed dips from state-deterioration dips)

When N null tunings accumulate, suspect a substrate gap. The 5-ruled-out-axes arc for live_momentum (S40, S41/S129, S130, S131, S132) was 5 tunings against an architecture that lacked a fair-value model. No amount of tuning can find edge in a strategy that doesn't have an edge-measurement layer. Cross-AI review identified this as the foundational gap.

When tempted to "test the next tuning hypothesis" after several nulls, instead ask: "is there a substrate piece that would make this question answerable in the first place?"

### Ruled-out vs N-thin — the two-explanations rule

Every null result has two valid explanations:
- (a) the hypothesis is wrong
- (b) N is too thin to detect a real effect

Both stay valid until ruled out by either a pre-committed kill rule firing OR substrate work changing the question.

When framing investigation outcomes for the operator, never collapse explanation (b) into explanation (a). "S130 found Pearson +0.078" is a measurement; "sizing is dead" is the (a)-only conclusion that smuggles in (b)'s exclusion. Use "5 axes returned null at this N" rather than "5 axes ruled out."

### Pre-committed kill rules — see "Strategy Termination Rules" section

Strategy investigations without termination criteria become indefinite. The discipline gap shows up as "investigation as comfort food" — testing the next hypothesis instead of forcing a ship-or-kill decision.

When opening a new investigation arc on a strategy without a kill rule, propose adding one to the "Strategy Termination Rules" section before the third null result. The rule should specify a date, an N threshold, a signal criterion, and a documented-exception path (e.g., substrate ship within X days re-sets the trigger).

### Cross-AI review synthesis — when operator sends external AI critiques

When the operator hands you back responses from other AIs critiquing a Glint spec, design, or strategy:

1. **Don't sycophant.** Read each critique on its merits. If a finding is wrong or overstated, say so — even if rejecting it makes the conversation harder.
2. **Cross-validation is the strongest signal.** A finding that 3+ of N AIs independently catch (especially through different framings) is much higher confidence than any single AI's deep dive. Single-AI deep insights can be brilliant or hallucinated; the cross-validation rate disambiguates.
3. **Verify against ground truth.** When an AI claims "the call site does X," verify by reading the actual code. AIs hallucinate code paths.
4. **Synthesize into a labeled action menu.** Tier 1 (foundational, high-confidence cross-validated), Tier 2 (substantive, requires substrate work), Tier 3 (broader strategic). My recommendation first.
5. **Operator picks by letter/number; don't pre-commit subsequent prompts.**

### Decision menu pattern — at strategic pivots

When the operator is at a pivot point with multiple coherent paths forward:

- Offer 2-4 labeled options (a/b/c/d or 1/2/3/4)
- My recommendation FIRST with rationale
- Alternatives next, rough priority order
- Brief trade-off framing (cost, risk, info value, time horizon)
- End with a short question

The operator picks by letter/number. Don't pre-commit subsequent prompts; don't preempt the choice.

### Dashboard measurement hygiene

`bot/state/active_observations.json` `current_value` fields drift stale when not refreshed. When doing a dashboard read for the operator (especially a deep dig), check `last_updated` against today's date. If a metric's last_updated is ≥7 days old AND underlying data has accumulated since, flag for refresh OR refresh inline if cost is small.

Never quote a stale `current_value` as if it's current state. The dashboard's signal is only as good as its freshness.

When a session ships and updates an entry, also refresh any other entries whose underlying data the session touched — don't leave stale numbers on adjacent metrics that the analysis happened to compute as a side effect.

### Named-but-untested list — don't lose hypotheses to session-entry burial

When a session entry says "X is named as worth investigating" or "S38c is a queued candidate" but never gets queued in Open Loops, it gets forgotten. When ruling out adjacent axes for a strategy, explicitly check the named-but-untested list. Don't declare a strategy "fully investigated" when there are still untested hypotheses.

For live_momentum specifically (as of S134 cross-AI review): S38c entry-price ceiling, per-sport TP/SL re-investigation at larger N, conviction-path P&L breakout, and AI #2's `recent_high_context` + dip-classifier substrate were all named-but-untested.

### Spec-writing for external AI consumption

When asked to write a spec for handing to multiple AIs:

- **Opinion-free.** Document what the system DOES, not what we should change. No "we should..." sentences. No recommendations. No editorializing.
- **Self-contained.** An outside reader shouldn't need to ask follow-ups for basic facts. Include schema fields, config values with rationales, file references, sub-period boundaries, performance numbers per cohort.
- **Audience-aware ordering.** If the audience is "should this exist?", lead with cohort EV and the investigation arc. If the audience is "describe the design," plumbing detail first is fine. The cross-AI review for live_momentum (S134-followup) caught that a 549-line spec burying cohort EV in §12 was suboptimal for the "should this exist?" question.
- **Honest.** Document what's been tested AND what's been ruled out. Document what's named-but-untested. Document what's contaminated or N-thin.
- **Cite session entries for ground truth.** Every claim should trace to a specific session entry, config value, or file path that the AI can read for verification.

---

## Canonical Data Schema Reference (read SECOND — before any session that touches state files)

**Why this section exists.** Two schema mistakes in 24 hours (May 1):
- Session 43b plan-time fix: `outcome_clv_cents` → `clv_cents`, `outcome_settlement` → `market_result`, `skip_reason` → `skipped_by_gate` for `clv.json`
- Session 45 verification error: `market_result == 'no_won'` → actual canonical is `'no'` (n_no_won counter returned 0 across all cohorts; correct count is 30/130). Falsified Layer-3 disqualification; Layer-1 saved the right outcome on a different rationale than documented. See Session 45 entry above for full forensic.

If a third instance happens, the next decision could ride on falsified evidence. This reference is the single source of truth.

### `bot/state/bot_state.json`

```python
{
  "running": bool,
  "total_pnl": float,
  "today_pnl": float,
  "scan_count": int,
  "scans_today": int,
  "crypto_trades_today": int,
  "crypto_pnl_session": float,
  "odds_api_requests_this_month": int,

  "started_at": str,                  # ISO 8601 UTC
  "last_heartbeat": str,              # ISO 8601 UTC
  "last_scan": str,                   # ISO 8601 UTC
  "current_date": str,                # YYYY-MM-DD; daily reset key for scans_today + telegram_throttled_count_24h + snapshot_outer_timeout_count_24h
  "last_odds_api_request": str,
  "last_morning_briefing": str,       # YYYY-MM-DD
  "last_nightly_summary": str,        # YYYY-MM-DD
  "last_balance_reconcile_date": str, # YYYY-MM-DD
  "last_balance_reconcile": str,      # ISO 8601 UTC
  "last_known_kalshi_balance": float,

  "dk_disabled": bool,
  "dk_disabled_until": float,         # unix timestamp
  "fd_disabled": bool,
  "fd_disabled_until": float,         # unix timestamp

  "last_ticks_rotation": str,         # YYYY-MM-DD
  "last_decisions_rotation": str,     # YYYY-MM-DD
  "last_predictions_rotation": str,   # YYYY-MM-DD
  "last_universe_rotation": str,      # YYYY-MM-DD
  "last_order_microstructure_rotation": str, # YYYY-MM-DD
  "last_tracker_cadence_rotation": str,      # YYYY-MM-DD
  "last_universe_metering_reset": str,       # YYYY-MM-DD
  "total_snapshots_today": int,
  "partial_snapshots_today": int,

  "telegram_throttled_until": str | None,        # ISO 8601 UTC; forward-only since Session 52
  "telegram_throttled_count_24h": int,           # reset with scans_today; forward-only since Session 52
  "telegram_last_send_attempt_at": str | None,   # ISO 8601 UTC; forward-only since Session 52
  "telegram_last_send_success_at": str | None,   # ISO 8601 UTC; forward-only since Session 52

  "snapshot_outer_timeout_count_24h": int,       # reset with scans_today; forward-only since Session 98
}
```

Forward-only rule: older `bot_state.json` files may be missing any `telegram_*` keys. Readers must treat missing `telegram_throttled_until`, `telegram_last_send_attempt_at`, and `telegram_last_send_success_at` as `None`; missing `telegram_throttled_count_24h` as `0`. Do not rename these fields or add aliases.

Session 98 forward-only rule: pre-Session-98 `bot_state.json` files may be missing `snapshot_outer_timeout_count_24h`; readers must treat missing as `0`. Counter resets daily alongside `scans_today` at midnight UTC roll. Non-zero values confirm the `_main_loop` outer `asyncio.wait_for` guard at [bot/main.py:1290](hustle-agent/bot/main.py:1290) fired; see Battle Scar #13's Session 98 addendum.

### `bot/state/clv.json` records

```python
{
  "trade_id": str,                     # 'PAPER-...' for real trades; 'CF-{scan_id}-{ticker}' for counterfactuals
  "ticker": str,                       # full Kalshi ticker
  "side": "yes" | "no",                # the side our bot WOULD have / DID enter on
  "entry_price": float,                # cents 0-100, intended entry price
  "fair_value": float,                 # cents 0-100, our model's fair value at scan time
  "recorded_at": str,                  # ISO 8601 UTC, when CF/trade was recorded
  "settled_at": str | None,            # ISO 8601 UTC, when market settled (null until settlement)
  "status": "open" | "settled" | "counterfactual_open" | "counterfactual_settled",
  "skipped_by_gate": str | None,       # reject reason (CFs only — real trades don't carry this). Canonical name; NOT 'skip_reason'.
  "clv_cents": float | None,           # CLV at settlement, signed. Canonical; NOT 'outcome_clv_cents'.
  "market_result": "yes" | "no" | None,  # which side actually won. Canonical values 'yes'/'no'; NOT 'yes_won'/'no_won'. None until settlement.
  "sport": str | None,                 # sport family from ticker prefix
  "regime": dict,                      # 5-axis: time_of_day, day_of_week, sport_phase, event_horizon_hr, match_phase
  "extra": dict,                       # gate-specific: distance-from-threshold, no_ask_prob, floor, etc.
}
```

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
NOT `'no_won'`. The Session 45 verification used the wrong value and produced n_no=0 across every cohort. Actual is n_no=30/130 on the no_vol_growth_first_seen cohort.

### `bot/state/decisions.jsonl` records

```python
{
  "ts": str,                           # ISO 8601 UTC; canonical timestamp field for decisions (not 'timestamp')
  "ticker": str,
  "opp_type": str,                     # rich vocabulary: vig_stack_series, vig_stack_futures, live_momentum, etc.
  "edge": float,                       # signed
  "gates": dict,                       # per-gate boolean pass/fail
  "decision": "accept" | "reject",     # canonical values; NOT 'take' / 'skip'
  "reason": str,                       # reject reason (when decision='reject'); NOT 'skip_reason'
  "extra": dict,                       # gate-specific context
  "regime": dict,                      # 5-axis tag
}
```

**Session 96 live_momentum decision context (forward-only).** For
`opp_type=="live_momentum"` decisions, `extra` may include a compact
allowlisted no-entry/entry context:

```python
{
  "sport": str,
  "ticker": str,
  "leader_ticker": str,
  "leader_side": "primary" | "opponent" | "scan",
  "leader_price": int | float,         # cents
  "opponent_price": int | float,       # cents
  "yes_bid": int | float,              # leader market bid, cents
  "yes_ask": int | float,              # leader market ask, cents
  "spread_cents": int | float,
  "volume_24h": int,                   # two-sided sum when both sides available
  "recent_high": int | float,
  "dip_cents": int | float,
  "dqs": float | None,
  "momentum": float | None,
  "wp_edge": float | None,
  "completion": float | None,          # 0.0-1.0 when GameContext exists
  "score_state": str | None,           # compact score only; no ESPN blob
  "match_phase": str | None,
  "time_remaining": float | None,      # 1.0 - completion
  "game_clock": str | None,
  "period": int | str | None,
  "skip_reason": str | None,
  "entry_gate": str | None,
  "source": "watcher" | "scan",
  "context_available": bool,
  "missing_context_fields": list[str],
  # Session 99 forward-only — scalar fair-value proxy. Present when
  # _build_live_momentum_decision_context is called from the per-tick site at
  # bot/live_watcher.py:1422-1453 (game_ctx-available path) AND wp_edge +
  # leader_price are both populated. Absent at scan-time call sites
  # (game_ctx=None, so wp_edge=None, so the proxy returns None — the merge
  # then drops it from the dict). Calibration via tools/calibration_report.py
  # → report_live_momentum_calibration().
  "estimated_win_prob": float | None,  # clamp(wp_edge + leader_price/100, 0.05, 0.95)
  "model_source": str,                 # "game_context_win_probability_v1"
  "confidence_components": dict,       # {wp_edge, market_implied, pre_clamp_raw, clamped}
  # Session 112 forward-only — IPL cricket state. Populated only when
  # sport == "ipl" AND ESPN cricket/8048 fetch returned a current-batting
  # linescore (isBatting AND isCurrent). Absent on all non-IPL sports and on
  # IPL rows where cricket fetch failed, was throttled, or returned no
  # current-innings row (e.g. between innings). Source:
  # bot/live_watcher.py _fetch_espn_score cricket branch + threaded by
  # _build_live_momentum_decision_context. match_phase resolves to
  # powerplay / middle / death via bot.regime._match_phase("ipl", ...) when
  # over_count is present.
  "over_count": int,                   # 1..20 — cricket over of the current batting innings
  "innings": int,                      # 1 or 2 — current batting innings (T20 has two)
  "wickets": int,                      # 0..10 — wickets lost by current batting team
  "runs_scored": int,                  # runs in the current innings
  # Session 137 forward-only — dip classifier label + diagnostic. Present on
  # every live_momentum decision where the watcher's context-build path fires,
  # INCLUDING scan-time reject paths (low_volume, no_leader, no_vol_growth_first_seen,
  # no_vol_growth_idle, capacity_capped, settled, not_today, bad_event_shape).
  # Absent at paths that bypass the context-build entirely (sport_disabled
  # rejects, etc.). Reader rule: treat absence as null; do not infer "C" from
  # absence. Classifier is pure; thresholds at bot/config.py
  # DIP_CLASSIFIER_WIDE_SPREAD_CENTS / _THIN_VOLUME / _DQS_MIN.
  "dip_class": "A" | "B" | "C" | "D",  # A=state-confirmed, B=state-deterioration,
                                        # C=unknown-context (abstain), D=spread/liquidity
  "dip_classifier_diagnostics": {
      "version": str,        # "dip_classifier_v1"
      "axis_fired": str,     # "spread_wide" | "volume_thin" | "missing_context" |
                             # "all_positive" | "state_deterioration"
  },
}
```

Rows only include fields available at that decision point. Missing values are
omitted and named in `missing_context_fields`. Do not add full market dicts,
GameContext objects, ESPN payloads, or order blobs to `extra`.

Session 107 widened the same schema to reachable partial-context paths:
scan-time `scan_found.extra` rows for `not_today`, `settled`,
`unknown_name`, `already_watching`, `recently_watched`, and
`bad_event_shape` now use the higher-priced available side as
`leader_side="scan"` context, and live_momentum executor direct logs
(`all_gates_passed`, `self_check_failed`) merge the watcher-built
`decision_context`.

Session 112 closed the IPL `match_phase` gap that S107 noted: with
`"ipl": "cricket/8048"` in `ESPN_SPORT_PATHS`, the per-tick ESPN call now
sources `over_count` and `match_phase` resolves on IPL rows during live
matches. Pre-S112 IPL rows do NOT carry `over_count` / `innings` /
`wickets` / `runs_scored` keys; readers must treat absence as null
(forward-only). Cricket fetches outside IPL match windows still return
empty, so off-season IPL rows continue to have `match_phase = None` and
the four cricket fields absent.

### `bot/state/live_journal.json` live_momentum scan context

`scan_found` events keep their existing top-level schema:

```python
{
  "event": "scan_found",
  "ticker": str,
  "sport": str | None,
  "skip_reason": str | None,           # None means watcher spawned
  "match": str,                        # optional
  "price": int,                        # optional, cents
  "volume": int,                       # optional
  "event_ticker": str,                 # optional
  "extra": dict,                       # optional Session 96 context shape above
  "timestamp": str,
}
```

Forward-only rule: pre-Session-96 journal rows lack `extra`; readers must
treat missing context as non-enriched, not malformed.

### `bot/state/shadow_trades.jsonl` records

Forward-only append-only ledger for blocked/disallowed opportunities. Session 95
emits these rows only for `family_disabled_reject`, `sport_disabled`, and
`reentry_blocked`; do not broaden this to every rejected gate without a separate
session.

```python
{
  "id": str,                           # 'SHADOW-...'
  "ts": str,                           # ISO 8601 UTC
  "ticker": str,
  "opp_type": str,
  "blocked_reason": "family_disabled_reject" | "sport_disabled" | "reentry_blocked",
  "would_side": "yes" | "no" | None,
  "would_entry_price": float | None,   # decimal dollars, matching paper_trades.entry_price
  "would_contracts": int | None,
  "would_notional": float | None,
  "sizing_status": "available" | "unavailable",
  "family": str | None,
  "sport": str | None,
  "close_ts": str | None,
  "status": "open",
  "settled_at": None,
  "market_result": None,
  "would_pnl": None,
  "source": "executor" | "live_watcher",
  "source_decision_reason": str,
  "extra": dict,
  "regime": dict,
}
```

Sizing rule: mark `sizing_status="available"` only when both contracts and
price are known. Session 108 wires forward-only `reentry_blocked` rows through
the shared live_momentum sizer when block-point inputs are present. If a blocked
live-watcher path lacks computable contracts, keep
`would_contracts`/`would_notional` null, use `sizing_status="unavailable"`, and
include explicit `extra.missing_sizing_fields` / `extra.sizing_unavailable_reason`
metadata. Do not write zeros or invented contract counts.

### `bot/state/paper_trades.json` records

```python
{
  "id": str,                           # 'PAPER-...'
  "ticker": str,
  "type": str,                         # canonical opp_type field on paper_trades; NOT 'opp_type'. Vocabulary is COARSER than decisions.opp_type — only 'vig_stack' / 'live_momentum' (no series/futures distinction). Vocabulary mismatch is intentional (Session 43b finding).
  "side": "yes" | "no",
  "entry_price": float,                # 0.0-1.0 dollars (NOT cents)
  "exit_price": float | None,
  "contracts": int,
  "edge_at_entry": float,
  "confidence": float,                 # 0.0-1.0. live_momentum (Session 50, 2026-05-01+): composite min(1.0, dqs * (1 + max(0, wp_edge))). vig_stack (since launch): relative_edge fallback at executor.py:1064. Pre-Session-50 live_momentum trades have confidence=0.
  "pnl": float | None,                 # dollars, signed
  "status": "open" | "won" | "lost" | "exited_early" | "cancelled_stale",
  "exit_reason": str | None,           # 'auto_take_profit' | 'auto_cut_loss' | 'edge_flipped' | 'manual' | etc. Persisted forward-only since Session 36.
  "dqs": float | None,                 # Dip Quality Score at entry. live_momentum ONLY, forward-only since Session 50 (2026-05-01). None on conviction-without-DQS paths and absent on all non-live_momentum strategies. Source: bot/game_context.py:compute_dip_quality() per Session 33.
  "sport": str | None,                 # Lowercase sport tag. live_momentum ONLY, forward-only since Session 50 (2026-05-01). Also set on live_latency_arb (WATCH path) records. Absent on vig_stack records.
  "estimated_win_prob": float | None,  # Scalar fair-value proxy clamped [0.05, 0.95]. live_momentum ONLY, forward-only since Session 99 (2026-05-11). Mathematically: wp_edge + leader_price/100 ≡ game_context.win_probability at entry tick. Absent on vig_stack records. Calibrated via tools/calibration_report.py → report_live_momentum_calibration().
  "model_source": str | None,          # Identifier for the proxy model that produced estimated_win_prob. live_momentum ONLY, forward-only since Session 99. Currently "game_context_win_probability_v1"; bump to _v2 for any future variant that incorporates DQS, momentum tilts, or additional signals.
  "confidence_components": dict | None,# Diagnostic dict for the proxy. live_momentum ONLY, forward-only since Session 99. Shape is model-version-dependent. v1 carries {wp_edge, market_implied, pre_clamp_raw, clamped}; downstream readers must handle absent keys gracefully across versions.
  # Session 100 — vig_stack ladder context (Data Collection Backlog Priority 5).
  # vig_stack ONLY, forward-only since Session 100 (2026-05-11). Absent on
  # live_momentum records (spillover regression-locked at
  # tests/test_bot_executor.py::TestSession100LadderContextFields). Source:
  # bot/vig_stack_ladder_context.compute_ladder_context() called from
  # bot/strategies/vig_stack_series.py:710-743 accept path; persisted by
  # bot/executor.py via the LADDER_CONTEXT_KEYS rename loop. Each field is
  # omitted (not None) when not applicable to the opp's classification.
  "family": str | None,                # Ticker prefix (KXHIGHAUS / KXNBA / KXINX / ...). Always present on Session-100+ vig_stack rows.
  "ladder_total_yes_sum_cents": int | None,  # Sum of YES prices across all rungs in the ladder at decision time (cents). Always present.
  "rung_count": int | None,            # Number of valid rungs in the ladder. Always present.
  "selected_rung_rank_asc": int | None,  # 1-indexed rank by ascending strike (1 = lowest strike). Omitted on futures opps where strikes are unparseable.
  "selected_rung_rank_desc": int | None, # 1-indexed rank by descending strike (1 = highest strike). Same omission rule as asc.
  "rung_strike": float | None,         # Parsed lower edge of this rung's bucket (e.g. B89.5 → 89.0). Omitted on futures opps.
  "rung_kind": str | None,             # "B" (1°F bucket) or "T" (threshold). Omitted on futures opps.
  "no_price_cents": int | None,        # NO entry price in cents (denormalized — same value as entry_price * 100, kept as int for cents-domain grouping in reports).
  "forecast_bucket_distance": float | None,  # Weather ONLY. Signed distance from forecast to this rung's bucket; negative = inside the bucket (depth from nearest edge), positive = outside (gap from nearest edge). Mirrors bot/strategies/vig_stack_series._forecast_distance_from_bucket convention.
  "source_forecast_temp": float | None,  # Weather ONLY. The cached NWS forecast temperature for this series at scan time (°F).
  "source_city": str | None,           # Weather ONLY. NWS city name for this series (e.g. "Austin" for KXHIGHAUS). From bot/strategies/vig_stack_series._SERIES_TO_NWS.
  "time_to_close_hr": float | None,    # Hours until market close at decision time. Omitted when close_ts is missing or unparseable.
  "ladder_context_source": str | None, # Version identifier for the helper. Currently "vig_stack_ladder_context_v1"; bump to _v2 for any future variant that adds fields (e.g. intra_ladder_correlation, ladder_age, recent_family_pnl).
  "timestamp": str,                    # ISO 8601 UTC, entry time. Canonical for paper_trades (NOT 'ts' which decisions.jsonl uses)
  "resolved_at": str | None,           # settlement time
  # Note: paper_trades.json had NO 'sport' field pre-Session 50; for older records, derive from ticker prefix via the per-game/futures map below. Same for `dqs` (live_momentum only) and the meaningful-confidence value on live_momentum trades. Pre-Session-99 live_momentum trades won't carry `estimated_win_prob`/`model_source`/`confidence_components` — readers must treat absence as None. Pre-Session-100 vig_stack trades won't carry the 13 ladder-context fields above — same forward-only rule.
}
```

### Ticker prefix → sport map (extend the bot's `_TICKER_PREFIX_TO_SPORT` if you need granularity)

| Prefix | Sport (per-game) | Prefix | Sport (futures) |
|---|---|---|---|
| `KXMLBGAME-` | mlb_game | `KXMLB-` | mlb_futures |
| `KXNBAGAME-` | nba_game | `KXNBA-` | nba_futures |
| `KXNHLGAME-` | nhl_game | `KXNHL-` | nhl_futures |
| `KXATPMATCH-` | atp | `KXATPCHALLENGERMATCH-` | atp_challenger |
| `KXWTAMATCH-` | wta | `KXWTACHALLENGERMATCH-` | wta_challenger |
| `KXUFC` | ufc | `KXIPL` | ipl |
| `KXHIGH*` | weather_high | `KXLOW*` | weather_low |
| `KXINX*` | index | | |

**Discovery agent uses `tools/discovery_agent/_sport_classifier.sport_from_ticker_distinguished()` for the per-game vs futures distinction. Bot code uses `bot/scanner.py`/`bot/live_watcher.py` `_TICKER_PREFIX_TO_SPORT` (coarser — doesn't distinguish per-game from futures). Don't conflate; reuse the right one for the layer.**

### How to use this section

- **Every session prompt that reads/writes any state file MUST cross-reference this section as Step 0.**
- If you need a field name or value enum and you're tempted to guess, STOP and check here.
- If this section disagrees with what you find on disk, the disk wins — but flag the discrepancy in your session entry so this reference can be corrected.
- When a new session ships a schema change, that session's entry must include the corresponding update to this section.

---

## Future Direction — Closed-Loop Strategy Lifecycle (NOT BUILDING YET)

**Status:** Documented north star. **We are not building this yet.** Refine the substrate first; revisit the readiness gate periodically.

### What "smart" means here

The bot today is a tool the operator pilots — operator decides which candidates to promote, which strategies to kill, when to investigate. "Smart" means the bot becomes an agent the operator supervises. Strategies enter and exit autonomously based on calibrated triggers; operator gets notified of transitions, not asked to approve them. Operator can override anything but doesn't have to approve everything.

### The lifecycle in stages

Each stage has explicit transitions. Transitions are data-driven, with thresholds documented and overridable.

- `candidate` (discovered by heuristic, surfaced in §10) → `promotable` (clears Session 84's per-heuristic bar with no disqualifications)
- `promotable` → `shadow` (paper-trades in parallel without affecting bankroll; runs for ~14d for validation)
- `shadow` → `live` (auto-graduate after shadow window if metrics hold; or auto-retire if they don't)
- `live` → `suspended` (auto-paused on calibrated kill threshold — e.g. 7-day WR drops below floor)
- `suspended` → `live` (operator override, OR auto-resume after evidence improves past resume threshold)
- `suspended` → `killed` (after suspension persists past resolution window without recovery)

### Pieces that exist today

- Discovery surfaces candidates (Session 66).
- Promotion bar evaluates candidates per-heuristic with disqualifier checks (Session 84).
- CF emission is honestly deduped at source (Session 86).
- Bar interpretation is correct over the 30-day post-fix transition (Session 87).
- Watch-list machinery captures some transition signals (informally).
- Operator-run protocol for "what's promotable today" (CLAUDE.md "When Tyler Asks 'What's Ready to Promote?'", Session 84).

### Pieces that DON'T exist yet

- **Shadow mode** (paper-trade-within-paper-mode runner). 0% built.
- **Auto-promotion** (clears bar → enters shadow without operator approval). 0% built.
- **Auto-suspension** (live strategy crosses kill threshold → pauses without operator approval). 0% built. *`live_momentum` is the standing proof this gap matters — it's been bleeding while operator manually deliberates.*
- **Notification stream** for transitions (operator gets told, not asked). 0% built.
- **Cross-strategy learning** (vig_stack's KXHIGH edge informs other strategies). 0% built.
- **Regime detection** (high-vol/low-vol, in-season/off-season, weekday/weekend). 0% built.
- **Auto-resolver for stale watch-list entries** (manual-check load shrinks instead of grows). 0% built — small enough to be a refinement-phase ship.

### Why we're not building this yet

**Lifecycle on a shaky substrate amplifies failures.** Auto-suspending a live strategy on bad data is worse than not auto-suspending. Auto-promoting a candidate based on inflated counts is worse than waiting (Session 87 just proved this directly). The substrate must be confidence-tested before we build closed-loop logic on top of it.

The current ~70/20/10 split in session work (operational hygiene / discovery infrastructure / explicit lifecycle work) is correct *for now*. Each Pattern C ship that surfaces a wrong premise about our own bot is substrate-hardening, not wasted work.

### Readiness gate (start the build when MOST are satisfied)

1. **pytest baseline stable for ≥30 days** with no surprise regressions. Today: 1524/0, on a 5-day streak of additions.
2. **Pattern C count growth slows** to ~1/week. Today: count is 15, growth was +6 in one day. When daily Pattern C ships drop to <1/day sustained, the substrate stops surfacing new wrong premises about our own code.
3. **Operator manual-check load shrinks**, not grows. Today: 23 → 28 in one day. Smart substrate would have auto-resolvers retiring stale entries. (This is itself a small refinement-phase feature worth shipping during the wait.)
4. **Zero CRITICAL false alarms for ≥14 consecutive days.** Session 83's stale-data hygiene was the first step; there may be other staleness vectors not yet found.
5. **`live_momentum` reaches a resolved state** — killed (Path B), deep-dive-justified (Path A with evidence), or proven valuable (positive 30-day rolling P&L). Indefinite is itself a substrate weakness; a bot can't be supervisor-mode if its operator can't decide whether one of its strategies works.
6. **`vig_stack` run-rate stable across 30+ consecutive days.** The workhorse is the reference; if it's wobbling, nothing built on it is reliable.

**Most ≠ all.** Perfect is the enemy of good. Aim for ~5 of 6 satisfied before proposing the lifecycle MVP planning session.

### Revisit cadence

Every ~10 sessions OR every 2 weeks, whichever comes first. Audit the readiness gate; if MOST criteria are satisfied, propose a focused planning session to define lifecycle MVP scope. Until then, stay in refinement mode and let the substrate stabilize.

### Until then, refinement looks like

- Operations hygiene (cadence, heartbeats, deployment, restart safety)
- Investigation of unexpected outcomes (Pattern C is legitimate; each one narrows the search)
- Discipline reinforcement (Phase 0 before scope, "verify premise, don't pre-decide fix")
- Incremental discovery infrastructure (new heuristics where evidence justifies them)
- Small substrate-hardening features (auto-resolvers for stale watch-list, clearer staleness markers, better diff displays)

The day we stop learning new wrong premises about our own bot is the day we know the substrate is ready for closed-loop logic on top of it.

---

## Data Collection Backlog: What We Still Need

**Trigger.** May 10 deep-dive: Glint is now a profitable vig_stack bot with a live_momentum research arm, but several profit decisions still end with "we cannot quantify that yet." This section is the backlog for closing those blind spots. Treat these as substrate work: collect the minimum data that lets the next tuning session make a decision, not decorative telemetry.

### Session grouping rule

Bundle only when the data path, owner files, and verification story are shared. If two gaps require different runtime hooks, different settlement logic, or different reports, split them. Do not ship a giant "more logging" session; those rot quickly and make attribution impossible.

### Priority 1: Shadow evidence for blocked/disallowed trades

**Gap.** We can block disabled families/sports (`family_disabled_reject`, `sport_disabled`, `reentry_blocked`) but we do not have a first-class ledger answering "was the block correct?" Session 93 currently has to infer would-have-traded KXHIGHCHI/KXINX from `decisions.jsonl`, and active observations are manually stale already.

**Manageable session shape.** One session can ship:

- `bot/state/shadow_trades.jsonl` (or similarly named append-only state file) for blocked-but-measurable opportunities.
- Emission for `family_disabled_reject`, `sport_disabled`, and `reentry_blocked` only. Keep the first version narrow.
- A resolver/report helper that marks shadow rows settled when the market result is known, reusing existing Kalshi/tracker/clv settlement helpers where practical.
- `tools/glint_status.py` §10 auto-computed current values for Session 90/92/93 active observations from decisions + shadow rows, replacing manual counters.

**Why first.** It directly validates recent P&L interventions without risking new trades. It also fixes the operator-dashboard drift where §10 showed 0 `reentry_blocked` and 0 `family_disabled_reject` while `decisions.jsonl` already had 4 and 3.

**Do not include yet.** Broad shadow mode for every rejected gate, strategy auto-promotion, or orderbook sizing. Those are larger lifecycle pieces.

### Priority 2: Live momentum no-entry context — shipped Session 96

**Gap.** We know watch-but-no-enter is high, and `scan_found.skip_reason` exists, but many no-entry decisions still lack enough structured context to decide whether the gate is too strict. We need the "why not" surface to carry the same discipline as accepted trades.

**Shipped shape.** Session 96 added one live_watcher instrumentation session:

- Structured context on live_momentum watcher rejects/accepts, executor-side live_momentum position-limit rejects, and scan/no-entry `scan_found.extra` rows.
- Forward-only only; no backfill.
- Compact `tools/journal_analysis.py` slice showing no-entry/reject counts by `(sport, reason)`, avg/median leader price / dip / DQS / spread / 24h volume, context coverage, and top missing fields.

**What did not change.** No entry logic, exit logic, sizing, disabled sports, thresholds, active strategy lists, or vig_stack behavior. This was data collection only.

### Priority 3: live_momentum fair-value proxy — shipped Session 99

**Gap.** live_momentum still lacks a reliable scalar predicted probability. Calibration and sizing are weaker because the strategy logs price-action decisions but not a comparable "our fair" value at entry.

**Shipped shape (Session 99, 2026-05-11).** One instrumentation session:

- Conservative `estimated_win_prob` for live_momentum decisions: `clamp(wp_edge + leader_price/100, 0.05, 0.95)`. Phase 0 surfaced the math identity — this reconstructs `game_context.win_probability` exactly when both inputs are present; the proxy persists an existing scalar that wasn't being saved. Model identifier: `game_context_win_probability_v1`.
- Persisted `estimated_win_prob`, `model_source`, `confidence_components` on:
  - `decisions.jsonl.extra` for every live_momentum decision (merged into `primary_context`/`opponent_context` at the per-tick build site).
  - `paper_trades.json` for every accepted live_momentum trade (Session 50 forward-only pattern; vig_stack records stay clean).
- `tools/calibration_report.py` extended with a separate `report_live_momentum_calibration()` section reading paper_trades.json so bad proxy performance cannot be mistaken for a vig_stack model failure.

**What did not change.** No sizing. No entry/exit logic. No threshold tuning. No predictions.jsonl writes for live_momentum (paper_trades.json is the calibration source). This was data collection only — first measure calibration; size later.

### Priority 4: Counterfactual exit paths for live_momentum

**Gap.** Tick replay can answer alternate exit questions, but the answer is not first-class in the daily data. Every live_momentum entry should become a training example for exit policy.

**Manageable session shape.** One analysis-tool session:

- Add an offline report that computes hold-to-settlement, TP/SL variants, trailing-only, and no-reentry variants for recent live_momentum entries.
- Persist report outputs under `bot/state/reports/` rather than writing per-trade state on the first pass.
- Promote to runtime logging only if the offline report repeatedly influences tuning decisions.

**Do not include yet.** Runtime exit changes.

### Priority 5: Vig_stack ladder context — shipped Session 100

**Gap.** We know which families win, but not enough about which ladder shapes within a family are fragile.

**Shipped shape (Session 100, 2026-05-11).** One vig_stack instrumentation session:

- 13 forward-only fields persisted on every accepted vig_stack decision in both `decisions.jsonl.extra` (un-prefixed) and `paper_trades.json` (renamed from `paper_*` by the executor): `family`, `ladder_total_yes_sum_cents`, `rung_count`, `selected_rung_rank_asc`, `selected_rung_rank_desc`, `rung_strike`, `rung_kind`, `no_price_cents`, `forecast_bucket_distance`, `source_forecast_temp`, `source_city`, `time_to_close_hr`, `ladder_context_source`. Helper module: `bot/vig_stack_ladder_context.py` (mirrors Session 99's `bot/live_momentum_proxy.py` shape).
- `tools/calibration_report.py` extended with `report_vig_stack_ladder_shapes()` rendering two sections: per-family × `selected_rung_rank_asc` (all families) and per-weather-family × `forecast_bucket_distance` bucket (`KXHIGH*` / `KXLOW*` only — testing the KXHIGHAUS B-bucket hypothesis).
- Phase 0 evidence: 7 of 8 wished-for fields were already computed at decision time and just discarded. Only `selected_rung_rank` needed new code (~10 lines: sort by parsed strike, find this opp's index). Motivating losses: two -$199 KXHIGHAUS B-bucket NO bets (B80.5, B89.5) on 2026-05-09/10 where the day's high landed inside the bucket.

**What did not change.** No behavior anywhere — no family cap/floor changes, no family disable/enable, no sizing. Accept-only scope on `decisions.jsonl` (reject rows keep their gate-specific extras). KXHIGHAUS B-bucket action explicitly waits for ≥14d of post-Session-100 evidence (watch-list trigger in `bot/state/active_observations.json` Session 100 entry, re-eval 2026-05-25).

### Priority 6: Paper liquidity and fill-quality realism

**Gap.** Paper mode may overstate fill quality. Before live mode, we need a friction view: spread, movement before execute, and whether the edge survives realistic execution.

**Manageable session shape.** One executor/scanner instrumentation session:

- Persist `bid`, `ask`, `spread`, `depth_proxy` if available, `price_moved_before_execute`, `would_fill_at_mid`, and `would_fill_at_ask`.
- Add a friction-adjusted P&L/CLV report that discounts paper fills by spread/slippage assumptions.

**Do not include yet.** Live order placement changes. This is paper realism only.

### Priority 7: Active observations auto-compute

**Gap.** `bot/state/active_observations.json` is operator-curated, so values drift. May 10 example: §10 showed 0 for Session 90/93 counters even though decisions had already logged `reentry_blocked=4` and `family_disabled_reject=3`.

**Manageable session shape.** This can ship together with Priority 1 because both read the same blocked-trade evidence. If Priority 1 is deferred, ship a smaller status-only session that computes known metrics from decisions/paper_trades without introducing new shadow rows.

**Rule.** `active_observations.json` may remain the registry of expectations, but `current_value` should be computed by `glint_status.py` when the data source is machine-readable. Manual values are acceptable only for metrics without a data path yet.

---

## Open Loops: Deferred Items to Revisit

Centralized backlog of items that have been investigated but deferred without a calendar trigger or auto-firing condition. **Items here will sit indefinitely without active work.** Items with calendar/threshold auto-triggers (S87 NHL convergence at 2026-06-08, S97 UFC at N≥15, S99 Brier at 2026-05-25, S100 KXHIGHAUS at N≥10 in bucket, S104 regime tagger when more daily buckets accumulate) are NOT here — they live in their session's watch-list trigger and surface in `glint_status §10 Active Observations` as data accumulates passively.

When a new session ships an Outcome B (design doc) or Outcome C with deferred work that won't auto-resolve, **add an entry to this section** rather than burying it in the session ☑ block. Remove entries when the deferred work ships.

### Blocked: needs dedicated session to unblock data collection

Items where Outcome B was filed but no data accumulates passively.

- **(Resolved by S112, 2026-05-11)** — IPL ESPN integration shipped. `"ipl": "cricket/8048"` added to `ESPN_SPORT_PATHS`; `_fetch_espn_score` now extracts `over_count` / `innings` / `wickets` / `runs_scored` from the current-batting linescore for sport=ipl; `_build_live_momentum_decision_context` threads those onto `decisions.jsonl.extra`; `_context_match_phase` delegates to `regime._match_phase("ipl", ...)` so `match_phase` resolves to powerplay / middle / death from over_count. **Constraint relaxation is IPL-specific** — the S96 / S107 "no new network calls" rule still holds for tennis, UFC, NHL match_phase, and future sport additions; see "Network Call Policy" above. Tennis match-level state remains deferred (per-tournament drill-down + ATP/WTA re-enable). 14-day cohort N re-measurement tracked in `bot/state/active_observations.json` (Session 112 entry).

- **S105 — Cross-platform settlement matcher.** Validation corpus doesn't exist publicly (only LLM-based products like Predexon, which warn about hallucinations). Source: [`docs/superpowers/specs/2026-05-11-cross-platform-matcher-design.md`](docs/superpowers/specs/2026-05-11-cross-platform-matcher-design.md). **S116 (2026-05-12) shipped the scraping + candidate-generation infrastructure** — `tools/build_cross_platform_corpus.py` produces a labeling queue at `bot/state/cross_platform_labeling_queue.jsonl` (5,000 top-Jaccard pairs from a 3-day scrape; full 604,603-pair corpus cached at `bot/state/cache/cross_platform_candidate_pairs_full.jsonl`, gitignored). **S117 (2026-05-12) shipped matcher v1 + validation harness. S118 validated the NO_MATCH direction on 40 Codex-labeled disagreement rows. S119 validated the MATCH direction on 40 stratified high-confidence rows and found 24 false positives**, driven by same-fixture/different-bet-type and crypto hourly-vs-date-only settlement-window mismatches. **S120 (2026-05-12) shipped matcher v2** with deterministic bet-type signatures and time-granularity gates; it cleared the original 80-label design corpus with 0 FPs. **S121 (2026-05-12) found 1 holdout false positive**: same MLB teams in a back-to-back series, but Kalshi's May 9 Rockies-Phillies game matched Polymarket's May 10 game because close times fell inside the ±24h gate. **S122 (2026-05-12) shipped matcher v3** with a deterministic sports game-instance gate (sport + game date + normalized participants) after bet-type/time-granularity and before Jaccard promotion. S122 validation on the combined 120-label corpus: `accuracy=97.5%`, `exact_accuracy=93.3%`, `false_positives=0`, `false_negatives=0`; S121 holdout now has `false_positives=0`, `false_negatives=0`. Full queue distribution shifted from v2 `1,590 / 1,558 / 1,852` to v3 `MATCH_HIGH_CONFIDENCE=1,512 / MATCH_NEEDS_REVIEW=1,511 / NO_MATCH=1,977`; 125 pairs hit `game_instance_mismatch` and 46 hit `game_instance_ambiguous`. **S123 (2026-05-12) shipped holdout-2 validation** with 40 fresh codex labels stratified at seed=123 from the 4,880 unlabeled rows (disjoint from the 120 prior labels). Outcome A: combined 160-pair corpus `accuracy=92.5%`, `exact_accuracy=83.1%`, `false_positives=0`, `false_negatives=0`; holdout-2 subset `false_positives=0`, `false_negatives=0` with `MATCH_HIGH_CONFIDENCE` bucket at `exact_accuracy=100%` (15/15) and the 8 boundary HIGH_CONFIDENCE picks all true MATCHes via the game-instance gate (including a Rockies-Phillies analog on 2026-05-10). **Status:** matcher v3 remains validated infrastructure: 160-pair Codex corpus across 3 independent samples (S118+S119 design + S121 holdout-1 + S123 holdout-2), 0 FPs, 0 FNs. S124 shipped live discovery substrate (Polymarket Gamma client + pair discovery tool), but its first live dry-run produced 0 candidate pairs; S126 classified that as a REAL ANSWER for current live universes rather than a bug. S127 confirmed the REAL ANSWER after filtering KXMVE displacement: 30,000 Kalshi live cursor rows were 99.32% MVE (29,795/30,000), leaving only 205 non-MVE markets dominated by commodity-price ladders (`KXNATGASD`, `KXSILVERD`, `KXGOLDD`, `KXTEMPNYCH`, `KXBRENTD`) that Polymarket does not carry; even MVE-filtered, all 12 threshold combinations (Jaccard 0.10-0.40, +/-3-14 day windows) produced 0 `MATCH_HIGH_CONFIDENCE`. Net: matcher + discovery infrastructure is validated and parked. Option value remains for future product-mix changes (Kalshi mix shift, an MVE-leg matcher as a different problem, or a new single-outcome platform), but there is no active consumer and no further work planned. No bot restart and no live arb wiring.

- **S124v2 follow-ons closed by S128.** The deferred scanner, discovery cron, and `bet_type` surfacing entries were rendered moot by the S127 REAL ANSWER finding above: current live product mixes have structurally absent overlap, the registry stays empty, and no scanner/dashboard consumer needs the extra fields.

- **S106 — `post_event_reversion` discovery heuristic (partial unblock shipped in S109; on a 30-day re-measurement clock).** S109 (2026-05-12) shipped Outcome A-defensive: `SCAN_INTERVAL_IDLE 1800→900` for 2× denser non-live universe coverage. S106's unblock path (a) is partially addressed; (b) post-resolution retention and (c) ≥30 days of natural history are still pending. **Investigation 4 in S109 returned 16.6% reversion at 1¢ events on N=205, with reversion *decreasing* as move size grew** — directional anomaly contradicts mean-reversion theory broadly. **KXCOPPERD 33% (N=12)** and **KXAAAGASD 29% (N=21)** subfamily signals were the borderline-evidence basis for the defensive ship rather than strict Outcome C. Source: [`docs/superpowers/specs/2026-05-11-post-event-reversion-design.md`](docs/superpowers/specs/2026-05-11-post-event-reversion-design.md). **Auto-trigger:** at the earlier of **2026-06-11** (30 days post-S109) OR when N≥50 observable ≥5¢ events accumulate on KXCOPPERD+KXAAAGASD post-restart, re-run the S106 v1 measurement at event_threshold≥5¢. If reversion rate ≥40% on N≥50 AND directional anomaly reverses → promote to v1 heuristic session. If <40% OR directional finding persists → kill the strategy class (deferred Outcome C). **Why it matters:** still the only fundamentally different STRATEGY CLASS we've identified vs current vig_stack + live_momentum, but the S109 directional finding sets a high bar for the re-measurement to clear.

### Operational hygiene not yet shipped

Items observed during operation but not prioritized for a session. No calendar trigger.

- **live_momentum kill rule fires 2026-06-15 at N>=80 OR re-set on substrate ship** — see "Strategy Termination Rules" section. Trigger cohort: settled post-S97 live_momentum trades with `status in {won, lost, exited_early}`, sport not in `MOMENTUM_DISABLED_SPORTS`, and `timestamp >= 2026-05-11`.

- **S136 entry-price ceiling axis (S38c) — HOLD on Phase 0 N-thinness.** Re-test at the 2026-06-15 kill-rule trigger date OR when the post-S97 enabled-sport cohort reaches N>=40. AI #3's `MAX=0.88` recommendation is contradicted by data at this N; AI #4's MIN-raise direction is weakly supported by the `[0.65,0.70)` bleeder bucket but N-thin per the S130 contamination lesson. Counts as one ruled-out-pending-N axis toward the S135 kill rule.

- **(Resolved by S114, 2026-05-11)** — MOMENTUM_LEADER_MIN dead-zone filter (Session 2 historical) — obsolete; dead zone disappeared in current data per S114 verification. The historical `[75-80¢)` negative-EV signal was traced to a per-sport artifact (single ATP trade, -$15.40), structurally excluded by S97's ATP re-disable on 2026-05-11. Post-S97 cohort (n=22 settled, Apr 15 – May 12, excluding `MOMENTUM_DISABLED_SPORTS`): `[75-80¢)` = 59% WR / +$0.46 per trade / +$10.17 total — positive-EV. Bucket is no longer a candidate for exclusion. See Session 114 entry in `CLAUDE-sessions.md` for full bucket breakdown + driver analysis.

- **S133 vig_stack_futures lean-in resolved by S134 (2026-05-13); scope simulation remains open.** S134 investigated the S133 KXMLBGAME surface. Result: **do NOT add `vig_stack_futures` to `_VIG_STACK_OPP_TYPES` on current evidence.** The N=10 cohort remains +$264.65 / +$26.47 mean, but the signal is concentrated: median -$0.59 and total excluding the top winner -$32.27. The decisive finding is early-exit counterfactual: 6 EE trades actual +$461.45 vs held-to-settlement +$213.00, delta +$248.45, so auto-exit on futures is currently useful. Mechanism: nearest universe snapshots reconstruct real two-rung per-game YES-sum >100c ladders, but realized P&L is dominated by game outcome and auto-exit timing. **Open follow-up scope:** simulation-only NBA/NHL per-game vig_stack replay with explicit futures auto-exit policy preserved. Evidence: KXNBAGAME has 6,890 universe rows / 439 scan IDs / 0 vig_stack decisions and rough post-Apr-29 YES-sum>100 rate 97.1%; KXNHLGAME has 5,418 rows / 436 scan IDs / 0 vig_stack decisions and 95.3% YES-sum>100. Do not ship scanner expansion directly from rough counts; replay/simulate first.

### S101 Layer 2 — closed by S125

- **S101 Layer 2 — outer `wait_for` binding hypothesis disproven.** S125 (2026-05-13) tested the proposed Python 3.14 binding bug directly: the isolated reproducer fired `TimeoutError` at 0.506s on Python 3.14.3 using the exact pattern at [bot/main.py:1289-1297](hustle-agent/bot/main.py:1289). The old counter-at-0 reading is now consistent with "no 600s+ snapshot stalls have occurred in production" rather than "the guard can't fire" — likely because S101 Layer 1 (per-request 30s daemon-thread guard) prevents individual requests from stacking into a 600s snapshot stall. **Future investigation (no auto-trigger):** if Kalshi degrades and snapshot stalls reappear without `snapshot_outer_timeout_count_24h` incrementing, gather bot-specific runtime evidence — exact deployed commit during the gap, scan-id sequence, whether the guard code was actually running, whether the gap was inside `snapshot_universe` before or after the guarded await — before re-opening the binding hypothesis.

### Cross-references (do NOT duplicate here)

- **Net-new instrumentation needs** → "Data Collection Backlog" above (P4 counterfactual exit paths, P6 paper liquidity realism — both still open as of Session 105).
- **Lifecycle / smart-bot vision** → "Future Direction" section above (north star, NOT BUILDING YET, readiness gate).
- **Auto-firing watch-list triggers** → individual session ☑ blocks in `CLAUDE-sessions.md` + `bot/state/active_observations.json` registry; surface in `glint_status §10 Active Observations`.
- **Battle Scars** (operational gotchas already known) → "Critical Gotchas (Battle Scars)" section above.

---

## Cross-platform corpus labeling protocol (S116/S117)

The S105 cross-platform settlement matcher requires a hand-labeled validation corpus. S116 shipped the scraping + candidate-generation infrastructure that produces the labeling queue; S117 shipped the deterministic matcher + validation harness. This section is the operator's protocol for filling in `operator_label` on each row.

### Files

- **Tool:** [`tools/build_cross_platform_corpus.py`](tools/build_cross_platform_corpus.py) — single-file CLI, no `agent/` deps, no bot restart needed. Scrapes Kalshi `/events?status=settled&with_nested_markets=true` (politics/econ/crypto/elections/world categories, filtered client-side because the API ignores the `category` param) + Polymarket Gamma `/markets?closed=true&order=closedTime&ascending=false`. Generates candidate pairs via token-set Jaccard + date alignment.
- **Matcher:** [`bot/cross_platform_matcher.py`](bot/cross_platform_matcher.py) — pure deterministic S105 v1 matcher; no API or filesystem dependencies.
- **Validation harness:** [`tools/validate_cross_platform_matcher.py`](tools/validate_cross_platform_matcher.py) — runs the matcher over the queue, reports operator-label accuracy when labels exist, reports heuristic agreement/disagreement always, and writes top priority disagreements to `bot/state/matcher_heuristic_disagreement_pairs.jsonl`.
- **Queue (operator-facing):** [`bot/state/cross_platform_labeling_queue.jsonl`](bot/state/cross_platform_labeling_queue.jsonl) — top 5,000 candidates by Jaccard descending. Force-added per the `active_observations.json` precedent (state/ is gitignored otherwise).
- **Priority disagreement queue:** `bot/state/matcher_heuristic_disagreement_pairs.jsonl` — S117 writes the top 40 matcher-vs-heuristic disagreements. These are high-value labeling targets because they distinguish deterministic matcher behavior from the S116 Jaccard/result heuristic.
- **Full corpus (cached, gitignored):** `bot/state/cache/cross_platform_candidate_pairs_full.jsonl` — all 604K+ candidates from the latest 3-day scrape. Reference for advanced analysis only; do not commit.
- **Raw caches (gitignored):** `bot/state/cache/kalshi_settled_markets.json` (~240 MB) + `bot/state/cache/polymarket_settled_markets.json` (~30 MB).

### Per-row schema

- `kalshi_ticker`, `kalshi_question`, `kalshi_close_date`, `kalshi_result` (`"yes"` | `"no"`), `kalshi_category`, `kalshi_url`
- `polymarket_ticker`, `polymarket_question`, `polymarket_close_date`, `polymarket_result`, `polymarket_category` (often empty — Polymarket no longer tags recent markets), `polymarket_url`
- `jaccard` (token-set Jaccard, ≥0.40 floor at generation), `days_apart` (`|close_date_K − close_date_P|`, ≤3.0 floor)
- `suggested_label` ∈ `{"MATCH", "NEEDS_REVIEW", "NO_MATCH"}` — **generator's hint, never authoritative**
- `operator_label` — initially `null`; operator fills in
- Optional validation metadata on labeled rows: `labeler` (`"codex"` for S118 labels; future operator labels must use a distinguishable value or omit only when intentionally legacy), `reasoning`, `outcome_cross_check` (`BOTH_YES` / `BOTH_NO` / `DIVERGED` / `INSUFFICIENT_DATA`), and `outcome_corroborates_label`.

### Operator label values

- **`MATCH`** — these two markets settle on the same real-world event. Tokens align, dates align, resolutions agree. Both venues would have closed consistently on the same outcome. **Use sparingly** — false MATCH labels poison the matcher's eventual zero-false-positive bar (per S105 spec).
- **`NO_MATCH`** — these two markets reference different events. The text overlap was coincidental (shared topic words like "Bitcoin", "May", "vs") but the underlying question / resolution criterion differs.
- **`NEEDS_REVIEW`** — genuinely ambiguous. Wording mismatches (e.g. Kalshi "shutdown duration" vs Polymarket "shutdown occurrence"), unclear resolution sources, close-but-not-identical date boundaries, or one venue has a binary while the other has a ladder.

### Suggested-label semantics

The generator's `suggested_label` follows the rule at [`tools/build_cross_platform_corpus.py:suggest_label`](tools/build_cross_platform_corpus.py):
- `MATCH` suggested when Jaccard ≥0.60 AND `days_apart` ≤1.0 AND `kalshi_result == polymarket_result`.
- `NO_MATCH` suggested when Jaccard <0.40 OR `days_apart` >3.0 (never emitted to the queue — filtered out at generation).
- `NEEDS_REVIEW` everything else.

The operator overrides. The bias is conservative: false MATCH suggestions train the operator to disagree, which slows labeling.

### Edge cases

- **Different but related events on the same day** (e.g. Kalshi "Fed cuts 25bp" vs Polymarket "Fed cuts at all"): label `NEEDS_REVIEW`. These are exactly the cases the matcher must learn to flag.
- **Same event, different granularity** (one venue binary, other has multiple outcomes): `NEEDS_REVIEW`. Matcher's eventual scope is YES/NO only.
- **Identical wording, far close_dates** (>1 day apart): `NEEDS_REVIEW`. Could be resolution-delay artifact or real difference.
- **Ladder rungs vs single threshold market** (e.g. Kalshi `KXBTC-T80000` vs Polymarket "Will Bitcoin be above $80,000?"): `MATCH` only if the threshold matches exactly and the date window is tight.
- **Per-game sports / esports** (e.g. Counter-Strike map handicap, LoL game-winner): these dominate the recent Polymarket inventory and produce many `MATCH` suggestions; labeling them as MATCH is correct **but** these aren't S105's target use case (politics/econ arb). Label honestly; the matcher's downstream scope filter will exclude sports.

### Expected per-pair labeling time

- **30-90 seconds per pair.** The Jaccard score, dates, and result agreement should resolve most pairs at a glance. Click through to `kalshi_url` / `polymarket_url` only when the question text alone is ambiguous.
- **Target throughput:** ≥40 labeled pairs in a 60-90 minute session.

### Target corpus size (per S105 spec)

- **Minimum:** 20 labeled `MATCH` + 20 labeled `NO_MATCH` pairs. More is better.
- **Coverage:** at least one pair per intended launch category (politics / economics / crypto). Recognize the recent-inventory skew — operator may want to look further back via `--lookback-days 30` if politics/econ pairs are too thin in the default 3-day window.

### Re-running the scraper

```bash
# Full re-scrape + regenerate (5K top-Jaccard queue + full corpus cache):
python3 tools/build_cross_platform_corpus.py

# Regenerate from cached scrapes (skip API calls):
python3 tools/build_cross_platform_corpus.py --skip-scrape

# Refresh Polymarket only; reuse Kalshi cache:
python3 tools/build_cross_platform_corpus.py --polymarket-only

# Wider lookback window (operator may want politics/econ that settle slowly):
python3 tools/build_cross_platform_corpus.py --lookback-days 30

# Uncapped queue (writes the full corpus to bot/state/cross_platform_labeling_queue.jsonl;
# may be 400+ MB — DO NOT git add):
python3 tools/build_cross_platform_corpus.py --skip-scrape --queue-top-n 0
```

**Before re-running, back up any in-progress labels** — the script overwrites `bot/state/cross_platform_labeling_queue.jsonl`:

```bash
cp bot/state/cross_platform_labeling_queue.jsonl bot/state/cross_platform_labeling_queue.jsonl.bak-$(date +%Y%m%d)
```

### After labeling

The labeled JSONL becomes the validation corpus for the S105 matcher implementation. Run the S117 harness after edits:

```bash
python3 tools/validate_cross_platform_matcher.py

# Restrict metrics to S118 Codex labels only:
python3 tools/validate_cross_platform_matcher.py --labeler codex
```

As of S118, the harness reports 40 Codex-labeled disagreement rows when run with `--labeler codex`: all are `NO_MATCH`, all have `DIVERGED` outcome cross-checks, and the matcher has **0 false positives** on that slice. These labels remain separate from future operator labels via `labeler: "codex"` and still need Claude/operator spot-check before being treated as trusted ground truth. Once balanced labels exist, the matcher will be required to:
- Achieve **zero false positives** where the matcher emits high-confidence `MATCH` against a labeled `NO_MATCH`.
- Flag labeled-NEEDS_REVIEW pairs as ambiguous (not silently emit as MATCH).
- Recover ≥30% of labeled MATCH pairs at high confidence (per S105 spec).

Trusting the matcher for live arb is gated on reviewed labels reaching ≥20 MATCH + ≥20 NO_MATCH and preserving zero false positives. The S118 slice validates the resolved-outcome-conflict guard, not MATCH recall.

---

## When Tyler Asks "How is it looking?"

Run `python3 tools/glint_status.py` and scan top-down. Session 91 reshaped the report into an operator dashboard: read sections in order and most questions answer themselves.

1. **§1 Verdict line 1 (vitals).** `Bot: PID NNN / uptime ... / heartbeat Ns / scans_today N / last scan Xm ago`. If you see `🚨 Bot: DEAD —` instead, restart per Battle Scar #14: `WRAPPER=$(pgrep -f "Desktop/hustle-agent/hustle-agent/run_bot.sh"); launchctl kickstart -k gui/$(id -u)/com.hustle-agent.bot`. Vitals incorporate `bot.lock` PID liveness check (`os.kill(pid, 0)`), `last_heartbeat` age, and `scans_today` — wrapper-presence + fresh heartbeat + alive PID = healthy.
2. **§1 Verdict line 2 (status label).** CRITICAL / Degraded / Healthy + position count + exposure + P&L + flag counts + discovery NEW + watch-list summary.
3. **§2 Diff Since Last Check.** What changed since the last `glint_status` snapshot — P&L delta, position delta, exposure delta, new flags. If empty (`_No baseline yet_`), this is the first snapshot since restart.
4. **§3 Health Pulse.** Always shows the daily report's source path + age. If age >12h, the body is collapsed (Session 91 sub-feature 5) — run a fresh daily report to refresh.
5. **§5 Open Positions + Settlements next 24h.** Family table (N/Exposure/Cap/Status) plus the operator-actionable line: "X positions / $Y notional at risk" and the next-to-settle ticker. **This is your "what's about to free up capital" answer.** Live-game tickers don't appear here — game-end timing is too noisy to estimate; check the family table for those.
6. **§7 Anomalies + Watch-List Status.** Anomalies first (CRITICAL/WARN/INFO entries inline with prefixes), then per-entry watch-list status (TRIGGERED / NOT_YET_TRIGGERED / MANUAL_CHECK_REQUIRED). Session 91 sub-feature 6 absorbed the old §9 auto-flags here (`daily_report_stale`, `watchlist_manual_checks` count, `watchlist_triggered` count). Session 91 sub-feature 1 retires obviously-stale entries (≥30d MANUAL_CHECK_REQUIRED with no recurrence) into `bot/state/watchlist_resolved.json`.
7. **§9 Strategy Candidates** (renumbered from §10 in Session 91). Any HIGH severity? Any new since yesterday's §2 diff? Apply the "When Tyler Asks 'What's Ready to Promote?'" bar before recommending action.
8. **§10 Active Observations** (new in Session 91). Manually-curated registry of recent Outcome A ships and the metrics that prove they're working. Read each entry: `Session N — description / shipped Xd ago / Yd remaining` plus per-metric `current_value` vs `expectation`. If a metric is outside its expectation, that ship needs investigation. Edit `bot/state/active_observations.json` to add new entries when shipping Outcome A.

**Answer in plain English:** "Bot is alive (X uptime), Y exposure, Z settling in 24h, top concern is...". Don't invent performance numbers — pull from `trade_history.json` and `strategy_audit.json` if specific P&L claims are needed.

**When the report itself looks wrong** (e.g. vitals show DEAD but you can see the bot in `ps aux`), check `lsof -p <PID> | grep cwd` to confirm the working directory matches Glint, not Bob (Battle Scar #14). If `python3 tools/glint_status.py` errors out, the upstream `paths.claude_md.read_text()` or `_load_json` calls usually surface the actual file issue.

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
- **Expect:** heartbeat age < scan_interval + 60s slack (post-S109 default 960s). `scans_today` ratchets up across the day. `last_decisions_rotation` and `last_live_ticks_rotation` set to today after 00:00 ET.
- **Caveat:** heartbeat updates per scan, so age can legitimately be ~15 min during normal idle (post-S109; was ~30 min pre-S109). Use it for "is the loop alive" not "is the loop responsive."
- **Known gaps:** Session 7 (lock-touch is also per-scan only; no per-second heartbeat for liveness).

### 4. `bot/state/bot.lock` — process liveness signal
- **Inspect:** `stat -f 'lock mtime=%Sm pid=%z' bot/state/bot.lock 2>/dev/null && cat bot/state/bot.lock`
- **Expect:** mtime within last scan interval. Lock PID matches the python child PID returned by `pgrep -P $(pgrep -f "Desktop/hustle-agent/hustle-agent/run_bot.sh")` — NOT the wrapper PID (lock contains the python child's PID, not the bash wrapper's). See Battle Scar #14 for path-rooted discipline.
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

## When Tyler Asks "What's Ready to Promote?"

A candidate is **promotable** when it is mature enough to earn its own focused Session N+1 with Phase 0 + an A/B/C outcome decision. Promotion = elevate from `§10 row` to `next-session subject`. The protocol does NOT commit to ship/not-ship; the next session decides. This section is the maturity bar; it does not act. Sessions 66/74 built the surface-and-track machinery; Session 84 codified this bar. The act-on-it side is the next session.

### Transition window (2026-05-08 → 2026-06-08, Session 87)

Session 86 (May 8) shipped emission-time CF dedup at [bot/clv.py:317-340](bot/clv.py:317) using key `(ticker, opp_type, sport, skip_reason, day)`. Going forward, NEW live_momentum CF emissions are deduped per UTC day; pre-Session-86 second-precision rows persist in `bot/state/clv.json` until they age out of `counterfactual_hotspots`'s 30-day lookback window — natural convergence around **2026-06-08**.

**During this 30-day window, raw N for `counterfactual_hotspots` reads ~1.5-2× inflated relative to deduped semantics.** The bar's effective rigor is roughly half its stated value during the transition. Session 87 evaluated three responses (re-tune threshold / read-time dedup / wait) and shipped Outcome C (wait) on Phase 0 evidence — see Session 87 ☑ block above. Summary:

- **Today's bar admits exactly one candidate** (`no_vol_growth_first_seen/nhl_game`, raw N=33, unique-day N=19, severity HIGH). That candidate is **cycle-delay-disqualified per Session 74** and **HELD per Session 85** — the operator outcome is HOLD regardless of which interpretation Session 87 picked.
- **By 2026-06-08, raw N drifts toward unique-day N organically** as historical pre-Session-86 rows age out. The bar is automatically operating on deduped semantics with no code change.
- **Transition-window false-positive operator cost: 0.** Phase 0.3 verified.

**Worked example for transition reading (2026-05-08 measurement):**

```
| gate / sport                        | sev    | raw_N | uniq_day_N | inflation |
|-------------------------------------|--------|-------|------------|-----------|
| edge_below_threshold/nhl_futures    | notable| 14    | 7          | 2.00x     |
| no_leader/atp                       | info   | 40    | 29         | 1.38x     |
| no_leader/wta                       | info   | 44    | 30         | 1.47x     |
| no_vol_growth_first_seen/atp_chall  | info   | 60    | 52         | 1.15x     |
| no_vol_growth_idle/atp_chall        | info   | 60    | 35         | 1.71x     |
| no_leader/nhl_game                  | info   | 55    | 31         | 1.77x     |
| no_vol_growth_first_seen/nhl_game   | HIGH   | 33    | 19         | 1.74x     |

Promotable under bar:
  Raw N ≥ 30:            1 (NHL no_vol_growth_first_seen)
  Unique-day N ≥ 30:     0
  Unique-day N ≥ 15:     1 (same NHL)

The single bar-passing candidate is cycle-delay-disqualified per Session 74,
HELD per Session 85. Transition-window operator cost: $0 / 0 sessions.
```

**Operator action during transition:** when applying the bar to a candidate, no special handling is required — the protocol works as written; the Session 84 ladder + Session 47 demotion + disqualifier checks all keep producing correct operator outcomes. The transition only affects bar *rigor* (1.5-2× looser today than at 2026-06-08), not bar *correctness*. If a NEW candidate enters the bar via raw N ≥ 30 during the window AND has unique-day N < 30 AND is NOT already HELD by a prior session's watch-list, escalate before 2026-06-08 — that's the only path Session 87's Outcome C produces operator toil. As of 2026-05-08, no such candidate exists.

### The bar (per-heuristic — only `counterfactual_hotspots` carries the universal thresholds)

```
counterfactual_hotspots:
  N (settled CFs)        >= 30
  mean_clv_cents         >= +5
  positive_clv_rate      >= 0.70
  days_stable            >= 7
  n_no_won               >= 3                  (heuristic-entry floor — restated for clarity)
  severity               in {high, notable}    (INFO findings deferred — Session 47 demotion already fired)

outlier_pnl:
  Bug-pair candidate (per Sessions 43-investigate / 76)?  → defer until cross-pattern evidence
  Singleton outlier?                                       → not promotable (Session 76 shape)

cohort_emergence:
  unique_tickers_recent  >= 10
  accepts_recent          >= 5    (Session 43-investigate refinement; cohorts with decisions but zero accepts auto-demote to INFO)
  paper_trades_recent     >= 3
  days_stable            >= 7

concurrent_attack_angles:
  ALWAYS promote to Session 51 strategy_lab prototype FIRST.
  Production scanner only after lab validates positive per-pair-key Σ P&L (Session 73 dedup discipline).

settlement_vs_rationale:
  Pattern 2 (disabled_sport_settlement)        → CRITICAL severity, always promotable
  Pattern 3 (outsized_notional_post_size_mult) → HIGH severity, always promotable
  Pattern 1 (tail_loss_in_high_cap_family)     → only when family aggregate is net-negative AND n_tail >= 2 in 14d window (Session 67 refinement)

threshold_proximity:
  near_miss_count >= 5 AND near_miss_clv > 0  →  promotable
  Otherwise: defer.

universe_gap:
  Always defer (architectural; no current per-finding session shape).
```

### Disqualifier checks (apply AFTER the bar; each is a constraint on next session's scope, NOT a veto)

1. **Cycle-delay-disqualified gate** (Sessions 45, 74). Specifically `no_vol_growth_first_seen` and `no_vol_growth_idle` — the gate is a binary scan-cycle check at [bot/live_watcher.py:3129-3140](bot/live_watcher.py:3129), not a tunable threshold. Next session must address one of: persisted `_prev_scan_volumes` across restarts, OR first-sight entry path, OR materially lower `LIVE_SCAN_INTERVAL`. Cross-reference Session 74 watch-list trigger.
2. **Disabled sport** (sport in `MOMENTUM_DISABLED_SPORTS = {atp, atp_challenger, nba_game, wta, wta_challenger}`). Re-enable decision (Sessions 38a, 38a-2, 97) is a separate prerequisite. Next session may pursue per-sport `MOMENTUM_LEADER_MIN` override (Session 64 architectural pattern) or per-family floor override (Session 67 architectural pattern) instead of re-enable.
3. **Cross-cohort demoted to INFO via Session 47 ladder.** Per-cohort signal is real but cross-cohort context contradicts. De-prioritize but not disqualify; next session must explicitly address why this sub-cohort warrants action when cross-cohort lens is flat or negative.
4. **Recent prior-session evaluation (within last 14 days, no new evidence).** Defer until either (a) the cohort grows materially since the prior eval, OR (b) a watch-list trigger explicitly fires. Cross-reference the prior session's watch-list trigger before re-opening.

### No-precedent note

`vig_stack_series` and `vig_stack_futures` are the only currently-promoted strategies; both predate the discovery-candidate framework (Session 43a, May 1, 2026) by 6+ weeks. **No historical promotion baseline exists for the candidate-data shape.** The thresholds above are first-principles, calibrated against the May 8, 2026 candidate set so that exactly one cohort (`no_vol_growth_first_seen/nhl_game`) clears the bar AND survives all disqualifier checks at the floor of maturity. Re-tune the bar **only** when (a) a future candidate is promoted via this protocol AND its outcome provides empirical EV evidence at the candidate-data shape, OR (b) the bar produces 0 or > 5 promotable candidates against a stable §10 surface for 7 consecutive days.

**Re-evaluate the N ≥ 30 threshold for `counterfactual_hotspots` after the natural-convergence date 2026-06-08 (Session 87 watch-list trigger).** Pre-Session-86 historical CF rows age out of the 30-day lookback by then. Re-measure raw vs. unique-day N on `bot/state/clv.json` for the active candidates; if raw and unique-day diverge by < 10%, the transition is complete and N ≥ 30 is operating on honest deduped semantics — leave Session 84 protocol unchanged. If divergence > 10% persists past 2026-06-15, investigate (likely a re-introduced over-emission bug or a misunderstood lookback window). See the "Transition window" callout above for the full Phase 0 reasoning behind the wait-and-see decision.

### How to run

```bash
python3 tools/glint_status.py | sed -n '/## 10/,$p' | head -60
```

Walk each row in §10's "Strategy Candidates" section. For each:

1. Read severity + heuristic + title.
2. Apply the per-heuristic bar above.
3. If pass: apply each disqualifier check; note any that fire as a constraint on the next session's scope.
4. Cross-reference any matching watch-list triggers in CLAUDE.md (the §10 renderer already shows these in the `watch_refs` column).
5. **Promote ≤ 2 candidates per session.** If more clear, pick the one(s) with (a) highest severity, (b) longest `days_stable`, (c) fewest disqualifier checks firing. The rest stay in §10.
6. Open the next session with a brief that names the candidate, the disqualifier checks that fire, and the prior-session cross-references.

### Today's surface (2026-05-08 reference snapshot — decays daily)

Of 16 active candidates today, **4 clear the raw bar; 1 survives all disqualifier checks**:

| Severity | Heuristic / cohort | N | mean_clv | +CLV% | stable | n_no | Disqualifier(s) firing |
|---|---|---|---|---|---|---|---|
| **HIGH** | `no_vol_growth_first_seen/nhl_game` | 33 | +20.9¢ | 90% | 8d | 3 | Cycle-delay (Session 74) — next session addresses architectural unblock per S74 watch-list |
| INFO | `no_vol_growth_first_seen/atp_challenger` | 55 | +11.0¢ | 87% | 8d | 7 | Cycle-delay (S45) + disabled sport (S38a-2) + INFO via S47 — defer until S74 unblock ships |
| INFO | `no_vol_growth_idle/atp_challenger` | 55 | +8.3¢ | 85% | 8d | 8 | Tunable but cross-cohort flat (S46) + disabled sport — defer per S46 watch-list |
| INFO | `no_leader/nhl_game` | 55 | +23.9¢ | 81% | 8d | 10 | Cross-cohort demoted (S47) + cross-sport context flat — defer pending S64-style per-sport override evidence |

**Promotable today: `no_vol_growth_first_seen/nhl_game`** (HIGH, only candidate without a recent prior-session evaluation). Session 85 brief opens with the Session 74 cycle-delay constraint named upfront — Phase 0 will determine whether the architectural unblock is justified at this cohort's maturity, OR whether to Pattern-C HOLD again pending more data.

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
- **All tests must always pass.** No skips, no flakes, no documented baseline failures. If a test fails:
  - (a) The failure is in scope of the current session → fix it before ship.
  - (b) The failure is a flake (race / timing / external dep) → fix the flake (mtime-fence, fixture isolation, deterministic mocking). Don't skip. Don't `xfail`. Don't document as "pre-existing."
  - (c) The failure is genuinely unrelated to this session AND a real bug → fix it as the session's first deliverable, OR open an immediate follow-up session before this one's main work begins.
  Sessions 68 + 69 documented "1 pre-existing live-state race" without naming the test, mirroring the exact Session 37 anti-pattern (10 baseline failures cleaned up at once because they accumulated). Session 70 closes that gap. Future sessions: the test name MUST appear in the ship report if any test fails, and the failure MUST be addressed in the same or next session.
- **Every session ends with `git push origin main`.** Both code commits AND the mandatory README sync commit must be pushed before marking the session complete. Use `git status` to verify "Your branch is up to date with 'origin/main'." Per Sessions 53/54/56.5 lessons learned: documented gaps in commit-but-not-push and CLAUDE.md-but-not-README-sync recur unless enforced at session-end. The discipline is one operation: commit + push together. README sync is mandatory after every session — see Session 56.5 entry for the pattern.

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
