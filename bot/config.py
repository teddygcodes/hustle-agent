"""
Nexus Trading Bot — Central Configuration

Loads API credentials from existing config/ files.
All trading thresholds, scan intervals, and risk limits in one place.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
BOT_STATE_DIR = Path(__file__).resolve().parent / "state"

# ---------------------------------------------------------------------------
# API Credentials (loaded from existing config files)
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}

_kalshi_cfg = _load_json(CONFIG_DIR / "kalshi.json")
KALSHI_KEY_ID = _kalshi_cfg.get("api_key_id", "")
KALSHI_PRIVATE_KEY_PATH = str(CONFIG_DIR / _kalshi_cfg.get("private_key_path", "kalshi-private-key.pem").replace("config/", ""))
KALSHI_ENVIRONMENT = _kalshi_cfg.get("environment", "production")

_odds_cfg = _load_json(CONFIG_DIR / "sports_data.json")
ODDS_API_KEY = _odds_cfg.get("api_key", "")

_telegram_cfg = _load_json(CONFIG_DIR / "telegram.json")
TELEGRAM_BOT_TOKEN = _telegram_cfg.get("bot_token", "")
TELEGRAM_CHAT_ID = _telegram_cfg.get("chat_id", "")

# ---------------------------------------------------------------------------
# Trading Thresholds
# ---------------------------------------------------------------------------
MIN_RELATIVE_EDGE = 0.15       # 15% minimum relative edge to alert
CRYPTO_MIN_EDGE = 0.08         # 8% minimum absolute edge — captures hourly/15min edges
CRYPTO_MIN_RELATIVE_EDGE = 0.10  # 10% relative edge for crypto (vs 15% for sports)
MAX_BET_FRACTION = 0.05        # 5% of balance per trade — conservative until edge proven
KELLY_FRACTION = 0.25          # 25% of full Kelly (conservative)
MIN_BET_DOLLARS = 1.00         # Not worth execution cost below this
# MAX_BET_DOLLARS removed — replaced by dynamic cap: min(balance * MAX_BET_FRACTION, 200.0) in sizing.py

# ---------------------------------------------------------------------------
# Scan Intervals (seconds)
# ---------------------------------------------------------------------------
SCAN_INTERVAL_IDLE = 1800      # 30 min — no live games
SCAN_INTERVAL_PREGAME = 600    # 10 min — games starting within 1 hour
SCAN_INTERVAL_LIVE = 120       # 2 min — live games in progress
ODDS_API_MONTHLY_LIMIT = 450   # Buffer from 500 free tier limit

# ---------------------------------------------------------------------------
# Live game watcher (WATCH command)
# ---------------------------------------------------------------------------
LIVE_POLL_INTERVAL        = 10    # seconds between ticks while watching a game
LIVE_WATCH_EDGE_THRESHOLD = 0.10  # 10% relative edge to auto-bet in watch mode
LIVE_TAKE_PROFIT_CENTS   = 12    # sell when up 12¢ from entry — backtested sweet spot
# LIVE_PROFIT_TARGET / LIVE_TRAILING_STOP / LIVE_HARD_PROFIT_TARGET removed
# Apr 26 (Session 18.5 Task 7) — verified dead via grep (0 readers across
# bot/, tests/, tools/). Trailing stop fires on MOMENTUM_DQS_TRAIL_STOP, not
# on a profit-target activation gate. See [bot/live_watcher.py:2178-2361].
LIVE_STOP_LOSS_CENTS     = 30   # exit if position drops 30¢ from entry
LIVE_NEAR_SETTLE_CENTS   = 93   # exit if price >= 93¢ (match almost over, lock in win)
LINE_MOVEMENT_THRESHOLD = 0.05 # 5pp move = significant

# Momentum mode (WATCH on 1v1 matches — tennis, UFC, etc.)
MOMENTUM_LEADER_MIN      = 0.65  # Session 19c (Apr 27, shipped): lowered 0.70 → 0.65
                                 # based on the tick-replay sweep on n=22 post-Apr-23
                                 # paper trades (15 train / 7 test).
                                 #   Train Σ P&L vs baseline (LM=0.70): +408¢
                                 #   Test  Σ P&L vs baseline (LM=0.70): +488¢ on n=6 test
                                 # Sign agreement holds; clear monotonic axis (LM=0.75
                                 # was -488¢ on train). TRAIL_STOP axis showed no signal
                                 # within any LM cluster (within-tier spread ±18¢) — kept
                                 # MOMENTUM_DQS_TRAIL_STOP=6 unchanged. CAVEAT: the test
                                 # delta is dominated by ONE trade flip (KXNBAGAME-26APR26-CLE:
                                 # -424¢ → +94¢, +518¢ swing) — effect is fragile. Revisit
                                 # with a larger sample in Session 22+.
                                 # See CLAUDE.md Session 19c block for the full sweep table.
                                 #
                                 # Prior history (kept for context):
                                 # v7b (Apr 20, post-S2 revert): 0.75 → 0.70 reverted — MIN is
                                 # a FLOOR (is_leader = prob >= MIN, live_watcher.py:933),
                                 # so 0.75 ADMITS [75-80c) while surrendering [70-75c).
                                 # Apr 20 post-rebuild entry-bucket breakdown:
                                 #   [70-75c): +$9.30  (positive)
                                 #   [75-80c): -$3.20  across 9 trades (dead zone)
                                 #   [80-85c): +$8.40  (positive)
                                 # Apr 14 audit (v6 reason, 43 trades):
                                 #   <70c: 23 trades, -$67.77 (-$2.95/trade, 22% WR)
                                 #   ≥70c: 20 trades, +$15.50 (+$0.78/trade, 55% WR)
                                 # The Session 19c sweep extends the floor down by 5pp; the
                                 # newly-admitted [65-70c) bucket carries the +408/+488¢
                                 # delta on the post-Apr-23 sample.
MOMENTUM_MAX_LOSS_DOLLARS = 5.00  # HARD CAP: exit if unrealized loss exceeds $5
                                  # Data: 7 trades lost >$10, totaling -$127. Capping at $5
                                  # would have turned -$104 total into +$2.84.
MOMENTUM_DIP_BUY         = 0.04  # buy when leader dips 4+ cents from recent high
MOMENTUM_DIP_MAX         = 0.08  # SKIP dips > 8¢ — those are set changes (0% win rate)
                                 # Data: dips ≤8¢ = 75% win rate; dips 11+¢ = 0% win rate
MOMENTUM_PRICE_WINDOW    = 12    # track last N ticks for recent high (~2 min at 10s)
# UNDERWATER EXIT removed Apr 16 — data-killed in Apr 14 audit (every UW exit
# recovered to TP). Constants MOMENTUM_UW_DEPTH_CENTS / MOMENTUM_UW_TICKS and
# the branch in _check_exit deleted. HARD STOP-LOSS + DOLLAR STOP still guard
# against runaway losses. If future data argues for revival, re-add behind
# new constants with the evidence that justified it.
MOMENTUM_MAX_ENTRIES     = 3     # max entries per match (re-entry after exit allowed)
MOMENTUM_REENTRY_COOLDOWN = 5    # ticks (~50s) cooldown after exit before re-entry
MOMENTUM_SCALE_SMALL_DIP = 1.0   # 1x on min-threshold dips — they qualify but aren't special
MOMENTUM_SCALE_MED_DIP   = 1.2   # 1.2x on medium dips — bigger dip = better bounce
MOMENTUM_SCALE_LARGE_DIP = 1.5   # 1.5x on big dips — DATA: 9-12c dips = 65% win, +3.67c avg

# Momentum sports that are gated off at the entry point (Apr 20 post-rebuild).
#   ATP Challenger:  2W/1L/14 exited_early, -$7.80, 82% cut rate
#   WTA:             1W/1L/5  exited_early, -$7.00, 71% cut rate
#   Tennis combined: 72% of live_momentum volume for -$6.20 net
#   NBA + NHL alone: +$19.60 across 10 trades
# Blanket tennis kill: main ATP is included precautionarily — no positive data
# yet to justify keeping it on while the challenger + WTA cohorts are this deep
# in the red. Revisit if/when an ATP cohort prints +P&L on n=10+ trades.
#
# Gate blocks NEW entries only; already-open positions exit normally via the
# TP / SL / trailing-stop logic in live_watcher._check_exit, which does not
# consult this set (verified). See live_watcher.py `_tick_momentum` can_enter.
MOMENTUM_DISABLED_SPORTS = {"atp", "atp_challenger", "wta", "wta_challenger"}

# ---------------------------------------------------------------------------
# Sport Instincts — situational awareness thresholds
# ---------------------------------------------------------------------------
# These encode the "feel" a veteran bettor has: when to sit on your hands,
# when to size down, when the game situation makes price moves meaningless.
INSTINCT_NBA_GARBAGE_LEAD    = 20   # points ahead in Q4 = garbage time (bench players)
INSTINCT_NBA_GARBAGE_CLOCK   = 300  # seconds left in Q4 when garbage kicks in
INSTINCT_NBA_CLUTCH_MARGIN   = 5    # within 5 pts in Q4 = clutch time (max volatility)
INSTINCT_NHL_EMPTY_NET_CLOCK = 150  # seconds left in P3 — goalie likely pulled if trailing
INSTINCT_MLB_HIGH_LEV_OUTS   = 2    # 2 outs + RISP = high-leverage AB (hard block)
INSTINCT_CLUTCH_TRAIL_WIDEN  = 1.5  # widen trailing stop by 50% in clutch/empty-net
INSTINCT_CLUTCH_SIZE_FACTOR  = 0.5  # halve position size in high-volatility situations

# ---------------------------------------------------------------------------
# Conviction Entry — "read the game, buy without a dip"
# ---------------------------------------------------------------------------
# Sometimes there IS no dip. The team is dominating, the price just keeps
# climbing, and waiting for a pullback means watching free money leave.
# Conviction entry buys when the game state says the price is too low —
# even without a dip — based on win probability edge + momentum + trend.
#
# REQUIREMENTS (ALL must be true):
#   1. Win prob model says fair value is significantly above Kalshi price
#   2. Our team has positive momentum (scoring, lead growing)
#   3. Price is in the "value zone" — not already at 85¢+
#   4. Enough ticks have passed to read the game (not tick 1)
#   5. Sport has a reliable win prob model (NBA, NHL — NOT tennis/UFC)
CONVICTION_ENABLED           = True
CONVICTION_MIN_WP_EDGE       = 0.08  # win_prob must be 8%+ above Kalshi price
CONVICTION_MIN_MOMENTUM      = 0.15  # positive momentum required (our team scoring)
CONVICTION_MIN_LEAD_TREND    = 0.0   # lead must not be shrinking
CONVICTION_MIN_PRICE         = 68    # DATA: 60-67¢ entries are flat/negative. 68¢+ is where edge starts.
CONVICTION_MAX_PRICE         = 82    # don't conviction-buy above 82¢ (not enough upside)
CONVICTION_MIN_TICKS         = 12    # wait ~2 min to read the game first
CONVICTION_MIN_COMPLETION    = 0.50  # DATA: Q3+ is the sweet spot. 253 candidates, +2.8c/trade, 79% hit.
                                     # Was 0.25 — too loose. <50% completion = flat/negative returns.
CONVICTION_SIZE_FACTOR       = 0.7   # 70% of normal size (less confident than dip entry)
CONVICTION_EXCLUDED_SPORTS   = ["mlb", "tennis", "ufc"]  # DATA: MLB 12% hit rate for conviction. Stick to NBA/NHL.

# Dip Quality Score (DQS) — composite score 0.0-1.0 determining dip buyability
# Only buy when DQS >= threshold. Score is weighted average of:
#   - Score differential (is the team actually ahead?)
#   - Game stage (late game dips on leaders = highest quality)
#   - Price level (stronger leader = more likely to revert)
#   - Volatility (dip must exceed recent noise)
MOMENTUM_DQS_THRESHOLD   = 0.40  # minimum dip quality score to buy (0-1 scale) — lowered from 0.45
MOMENTUM_DQS_TRAIL_STOP  = 6     # trailing stop: 6¢ from peak — DATA v4: only positive trail value (+0.28c/trade)
                                 # 4c too tight (noise), 8c too wide (gives back gains)

# Tier 2.4 (Apr 16): lightweight quality gate for sports that skip the full DQS
# (tennis). Rejects "flat" dips where the last 6 ticks have <2c total range —
# that's effectively a set break / changeover with no information content.
# Applied in _tick_momentum when sport_profile.get("variance_quality_gate") is True.
# If data shows this hurts UFC/other 1v1 sports, flip the per-sport flag.
TENNIS_QUALITY_MIN_TICKS = 6     # need this many ticks to evaluate variance
TENNIS_QUALITY_MIN_RANGE = 2     # cents — <2c range over lookback = flat/noise

# Sport-specific tuning — different scoring dynamics need different thresholds
# format: {min_dip_cents, max_dip_cents, max_entry_price, score_diff_weight}
SPORT_PROFILES = {
    # =====================================================================
    # v6 — DATA AUDIT of 43 real trades (Apr 9-14, verified from logs)
    #
    # KEY FINDING: Entry price is the #1 predictor of profit/loss.
    #   <70c entries: 23 trades, -$67.77, 22% WR — MONEY PIT
    #   ≥70c entries: 20 trades, +$15.50, 55% WR — PROFITABLE
    #   Below 70c, the "leader" isn't leading strongly enough to revert.
    #   MOMENTUM_LEADER_MIN raised to 0.70 to enforce this globally.
    #
    # #2 FINDING: SL slippage is catastrophic (avg 21c vs 12-15c config)
    #   10-second ticks cause gaps. Tightened SL to 10c across the board.
    #
    # #3 FINDING: Trailing stop IS the edge (+$67.75 from TP exits)
    #   Keep trail_stop. Avg TP gain = +15.2c.
    #
    # #4 FINDING: Position sizing amplifies losses
    #   Max 20 contracts across all sports.
    #
    # CORRECTED P&L (from logs, not buggy paper_trades.json):
    #   NHL:    4 trades, +$9.30, 75% WR — BEST SPORT
    #   UFC:    3 trades, -$3.81 (MUR -37c gap killed it)
    #   Tennis: 17 trades, -$38.97 (but 70c+ entries = -$6.83)
    #   MLB:    13 trades, -$11.85 — DISABLED
    #   NBA:    5 trades, -$3.98 (LAL +$4.80 was only 70c+ entry)
    # =====================================================================
    "nba": {
        # DATA: 5 trades, 1W/4L, -$3.98. LAL at 77c was the only win.
        # DEN/GSW/POR/HOU all underwater — all entered via UW exit (disabled).
        # DQS still required for NBA. Only enter at 70c+ (via LEADER_MIN).
        "min_dip": 5,
        "max_dip": 20,
        "max_entry": 88,
        "min_score_diff": 3,
        "periods": 4,
        "late_game_period": 4,
        "take_profit": 12,
        "stop_loss": 10,     # TIGHTENED: 15 → 10. Avg SL was -15c, need to cut faster
        "trail_stop": 4,
        "max_contracts": 20,  # NEW: cap position size
    },
    "nhl": {
        # DATA: 4 trades, 3W/1L, +$9.30. BEST SPORT.
        # ANA +$4.32, NSH +$2.40, CAR +$5.16. Only loss: MIN at 68c (underwater exit).
        # All wins at 70c+. NHL leaders are extremely sticky.
        "min_dip": 4,
        "max_dip": 15,
        "max_entry": 88,     # RAISED: was 80. CAR entered at 82c = best trade.
        "min_score_diff": 1,
        "periods": 3,
        "late_game_period": 3,
        "take_profit": 15,
        "stop_loss": 10,     # TIGHTENED: 15 → 10
        "trail_stop": 8,
        "max_contracts": 20,
    },
    "mlb": {
        # DATA: 13 trades, 6W/7L, -$11.85. DISABLED.
        # Wins only happen at 70c+. Too many reversals, long games.
        "min_dip": 99,
        "max_dip": 100,
        "max_entry": 50,
        "min_score_diff": 99,
        "periods": 9,
        "late_game_period": 7,
        "take_profit": 10,
        "stop_loss": 15,
        "disabled": True,
    },
    "tennis": {
        # DATA: 17 trades, 5W/12L, -$38.97. BUT:
        #   <70c: 13 trades, -$42.06 — disaster
        #   ≥70c: 4 trades, -$6.83 (KOT -4.96, FIC -0.08, VIR +4.56, DUC +5.25)
        # At 70c+: 50% WR, trail stop locks in gains.
        # LEADER_MIN=70c now enforces this. skip_dqs=True for speed.
        # Tier 2.4 (Apr 16): added variance quality gate — reject "flat" dips
        # during set breaks / changeovers (pure noise, no info content).
        # See TENNIS_QUALITY_* constants below.
        "min_dip": 5,
        "max_dip": 20,
        "max_entry": 88,
        "min_score_diff": 0,
        "periods": 3,
        "late_game_period": 3,
        "take_profit": 10,
        "stop_loss": 10,     # TIGHTENED: 12 → 10. KOT dropped 16c through 12c SL.
        "skip_dqs": True,
        "variance_quality_gate": True,  # Tier 2.4 — lightweight replacement for full DQS
        "max_contracts": 20,
    },
    "ufc": {
        # DATA: 3 trades, 2W/1L, -$3.81. PAD +5.60, GAM +4.65, MUR -14.06.
        # MUR: 37c crash in 3 min (KO). SL can't help with instant crashes.
        # Tight SL + small positions are the only defense.
        "min_dip": 5,       # LOOSENED: 10 → 5. PAD entered at 63c dip.
        "max_dip": 15,
        "max_entry": 85,     # RAISED: 70 → 85. GAM entered at 78c.
        "min_score_diff": 0,
        "periods": 5,
        "late_game_period": 4,
        "take_profit": 12,
        "stop_loss": 10,     # TIGHTENED: 15 → 10
        "skip_dqs": True,
        "max_contracts": 10,  # REDUCED: 15 → 10. KO risk.
    },
}

# ATP/WTA variants all use tennis profile
for _alias in ("atp", "atp_challenger", "wta", "wta_challenger"):
    SPORT_PROFILES[_alias] = SPORT_PROFILES["tennis"]

# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
NWS_BIAS_CORRECTION = 1.5     # Degrees F — documented NWS warm bias
# WEATHER_STD_DEV removed — sigma is computed dynamically in math_engine.py

# NWS city coordinates for Kalshi weather markets
NWS_CITIES = {
    "NYC":     (40.7829, -73.9654),    # Central Park
    "Chicago": (41.7868, -87.7522),    # Midway Airport
    "Miami":   (25.7959, -80.2870),    # Miami International
    "Austin":  (30.1945, -97.6699),    # Austin-Bergstrom
    "Denver":  (39.7392, -104.9903),   # Denver downtown
}

# Kalshi weather series tickers — confirmed active (ordered by typical volume)
WEATHER_SERIES_TICKERS = [
    "KXHIGHNY",   # NYC
    "KXHIGHAUS",  # Austin
    "KXHIGHCHI",  # Chicago
    "KXHIGHDEN",  # Denver
    "KXHIGHMIA",  # Miami
    # Below: try these; silently skip if no markets exist
    "KXHIGHBOS",  # Boston
    "KXHIGHDC",   # DC / Washington
    "KXHIGHSF",   # San Francisco
    "KXHIGHLA",   # Los Angeles
    "KXHIGHSEA",  # Seattle
    "KXHIGHPHO",  # Phoenix
    "KXHIGHDAL",  # Dallas
    "KXHIGHATL",  # Atlanta
    "KXHIGHPHL",  # Philadelphia
    "KXHIGHLV",   # Las Vegas
    "KXHIGHPDX",  # Portland
    "KXHIGHMIN",  # Minneapolis
    "KXHIGHNSH",  # Nashville
]

# Kalshi index range series — range (between) contracts, mutually exclusive like weather
# These have structural vig (YES sum > 100¢) and the vig_stack math applies.
INDEX_RANGE_SERIES_TICKERS = [
    "KXINX",      # S&P 500 daily close — 25¢+ range contracts with 20-40% vig excess
]

# Kalshi sports futures — championship/winner markets are mutually exclusive
# (exactly one team wins). YES sum > 100¢ = structural vig, same math as weather.
# NBA 17% vig (20 teams), NHL 22% vig (25 teams), MLB 6% vig (30 teams).
SPORTS_FUTURES_TICKERS = [
    "KXNBA",      # NBA Championship — 17% vig, $63M+ volume
    "KXNHL",      # NHL Stanley Cup — 22% vig, $21M+ volume
    "KXMLB",      # MLB World Series — 6% vig, $9.7M+ volume
]

# ---------------------------------------------------------------------------
# Risk Limits
# ---------------------------------------------------------------------------
MAX_POSITION_PERCENT = 0.20    # Max 20% of balance in one market
MAX_TOTAL_EXPOSURE = 1.00      # Disabled for now — was 0.50
POSITION_MOVE_ALERT = 0.20     # Alert if position moves 20% against
TAKE_PROFIT_THRESHOLD = 0.50   # +50% unrealized → alert to take profit
CUT_LOSS_THRESHOLD = -0.30     # -30% unrealized → alert to cut loss
TRAILING_STOP_DEFAULT = 0.20   # 20% trailing stop default
TRAILING_STOP_MIN_HOLD = 300   # 5 min minimum hold before stop activates

# Per-strategy exposure budgets (Tier 2.1, Apr 16 plan).
# Fractions of balance reserved per strategy family so vig_stack fills cannot
# soak 100% and starve live_momentum's conviction path. The global
# MAX_TOTAL_EXPOSURE still applies (budgets sum to 1.0 and act as a lower cap,
# not a replacement). Enforced in executor._check_position_limits — rejection
# reason string: "STRATEGY_BUDGET".
#
# Keying is by positions' `opp_type` / `type` field. Unknown types fall into
# "other" (no budget limit — global cap still applies).
#
# Evidence: vig_stack currently holds $200/$234 (85% of balance), so
# conviction entries fail on the global cap. Reserving 20% for live_momentum
# gives that path ~$100 of $500 headroom at all times.
STRATEGY_BUDGETS: dict[str, float] = {
    "vig_stack": 0.60,        # matches vig_stack_series + vig_stack_futures
    "live_momentum": 0.20,    # live_watcher momentum/conviction entries
    "arbs": 0.20,             # monotonicity + consistency arbs
}

# Vig stack NO entry price floor (Apr 18, data-driven).
# Across 24 resolved vig_stack trades (paper, current + archive), entries below
# 0.70 are a net disaster: 6 trades, 2W/4L (33% WR), net −$37.60 — including
# the Apr 15 NYC blowup (−$24.91) and three other −$10+ exits. Entries at 0.70+
# were 18 trades, 15W/3L (83% WR), net +$10.01.
#
# Theory: vig_stack's edge is structural — it comes from the ladder's total vig
# being a large fraction of each rung's price. At NO = 0.95, a 10% ladder vig
# is a meaningful fraction. At NO = 0.65, 10% is a small fraction and the
# market's 35% YES estimate is well-calibrated noise, not vig-induced mispricing.
# Middle-price NOs have the weakest structural edge.
#
# Revisit: after 20 additional resolved vig_stack trades OR if the 0.80-0.89
# bucket (currently 6W/3L, net −$8.39) starts bleeding further.
#
# Apr 18 update: this floor remains the baseline but is now family-aware.
# Stable families (VIG_STACK_STABLE_FAMILIES below) enter at this 0.70 floor;
# volatile weather families must meet the stricter VIG_STACK_WEATHER_MIN_PRICE.
VIG_STACK_MIN_NO_ENTRY_PRICE = 0.70

# Vig stack stable-family whitelist (Apr 18 evening, data-driven).
# Full-day data dive on 33 post-paper-fill-bug resolved vig_stack trades
# (Apr 15-18) showed ladder family dominates success far more than entry
# price alone:
#
#   stable (MIA, AUS, INX):  n=12, 10W/2L (83% WR), net +$20.31
#   volatile (DEN, CHI, NY): n=21,  8W/13L (38% WR), net −$126.63
#
# Ladder-family × date grid proved the signal is NOT date-confounded: MIA
# won on all 3 days it traded, while DEN lost on 2 of 3. Physical reality:
# subtropical (MIA) + semi-arid-but-forecastable (AUS) climates have tight
# forecast-vs-actual distributions. Spring continental cities (DEN/CHI/NY)
# do not — a single variable-weather day zeroes every outer rung on the
# ladder simultaneously (see KXHIGHDEN Apr 17, −$59.15 in one day).
# Financial indices (INX/S&P) have structurally predictable intraday bands
# from the morning open, independent of weather physics.
#
# Trades on families in this set pass the VIG_STACK_MIN_NO_ENTRY_PRICE (0.70)
# floor. Trades on other families must meet VIG_STACK_WEATHER_MIN_PRICE (0.90).
#
# Revisit: after 20+ more resolved trades, or if any whitelisted family
# posts a single-day loss > $15.
VIG_STACK_STABLE_FAMILIES = {"KXHIGHMIA", "KXHIGHAUS", "KXINX"}

# Vig stack volatile-family price floor.
# Apr 18: introduced at 0.90 after a 33-trade split showed:
#   entry >= 0.90 (any family):     7W/0L  (100% WR), +$6.71
#   entry <  0.90, stable family:   6W/2L  (75%  WR), +$16.76
#   entry <  0.90, volatile family: 5W/13L (28%  WR), -$129.79
#
# Apr 20 raised to 0.93 after the Session-1 rebuild exposed the full picture:
#   Volatile families (KXHIGHDEN/NY/CHI):  36 trades, -$126.88, 69% early-cut
#   Whitelist families (MIA/AUS/INX):      18 trades, +$16.26
#
# Volatile-family entry-price buckets (post-rebuild ground truth):
#   <92c:       42 trades, -$110.79  (deeply negative — core of the bleed)
#   [92-96c):   12 trades, +$0.17, 11W/1L (92% WR)  ← sole breakeven band
#
# A 0.93 floor sits 1c inside the breakeven band — gives a safety margin over
# the bottom of [92-96c) while keeping the bulk of the cohort eligible.
# Structural risk/reward at NO 0.93 (bet 93c to make 7c) demands ~93% true WR
# to break even; the observed 92% WR + NWS 2F-MAE priors support the call.
#
# Applied together with VIG_STACK_STABLE_FAMILIES: stable uses 0.70 floor,
# everything else uses this 0.93 floor.
#
# Revisit: if a new cohort of 10+ trades in the volatile family [93-96c) still
# prints negative, 0.93 isn't tight enough — escalate. Also revisit if any
# stable family posts a 90+ WR loss > $10 cluster.
VIG_STACK_WEATHER_MIN_PRICE = 0.93

# Vig stack per-ladder rung concentration cap (Apr 18, data-driven).
# On Apr 17, the KXHIGHDEN ladder accumulated 6 rungs across 3 separate scan
# cycles (13-17 contracts each). The actual Denver high landed in a range
# that zeroed all 6 rungs simultaneously: net −$70.35 from one correlated
# event. The existing _cap_correlated_vig_stack() only splits contracts
# within a single scan, so cross-scan accumulation is unbounded.
#
# Cap: the count of OPEN vig_stack positions on a single ladder-event
# (series_ticker + event_key, e.g. KXHIGHDEN-26APR17) plus newly surfaced
# opportunities must not exceed this value. When capped, the highest-
# relative-edge new opps are kept.
#
# Chosen at 3 rungs: the Apr 15 NY ladder (5 rungs, 5/5 wins) and the Apr 15
# MIA ladder (3 rungs, 3/3 wins) shipped inside a 3-rung cap; the Apr 17 DEN
# 6-rung wipe would not have. Halves worst-case single-event loss.
VIG_STACK_MAX_RUNGS_PER_LADDER = 3

# ---------------------------------------------------------------------------
# DraftKings Sportsbook API (primary odds source — no auth needed)
# Blocked by Akamai WAF on some residential IPs; works from cloud/VPS.
# ---------------------------------------------------------------------------
DRAFTKINGS_BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v5"

# eventGroupId values (provided by user; verify via /api/v5/eventgroups?format=json)
DRAFTKINGS_EVENT_GROUPS = {
    "nba":        42648,
    "mlb":        84240,
    "nhl":        42133,
    "nfl":        88808,
    "ncaab":      92483,
    "epl":        40253,
    "champions":  93539,
    "laliga":     40031,
    "seriea":     40030,
    "bundesliga": 40481,
    "ligue1":     40032,
    "mls":        92573,
    "ufc":        9034,
}

# ---------------------------------------------------------------------------
# Bovada public JSON API (free fallback — no auth, no quota)
# ---------------------------------------------------------------------------
BOVADA_BASE = "https://www.bovada.lv/services/sports/event/v2/events/A/description"
BOVADA_SPORT_PATHS = {
    "nba":   "basketball/nba",
    "mlb":   "baseball/mlb",
    "nhl":   "hockey/nhl",
    "nfl":   "football/nfl",
    "ncaab": "basketball/ncaab",
    "ufc":   "mma/ufc",
    "ipl":   "cricket/indian-premier-league",
}

# ---------------------------------------------------------------------------
# FanDuel Sportsbook API (free, no auth — unofficial frontend API)
# Uses FanDuel's own frontend key (_ak). Self-disables on 403.
# ---------------------------------------------------------------------------
FANDUEL_BASE = "https://sbapi.tn.sportsbook.fanduel.com/api"
FANDUEL_AK = "FhMFpcPWXMeyZxOx"
FANDUEL_SPORT_IDS = {
    "nba":   "nba",
    "mlb":   "mlb",
    "nhl":   "nhl",
    "ncaab": "college-basketball",
    "nfl":   "nfl",
}

# ---------------------------------------------------------------------------
# ESPN Free API (scores + game status; odds often return 0)
# ---------------------------------------------------------------------------
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_SPORT_PATHS = {
    "nba": "basketball/nba",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
    "nfl": "football/nfl",
    "ncaab": "basketball/mens-college-basketball",
}

# ---------------------------------------------------------------------------
# TheRundown API (free tier — 20,000 data points/day; free key via email signup)
# Sign up at: https://therundown.io/api  — no credit card required
# Save key to config/therundown.json: {"api_key": "YOUR_KEY"}
# ---------------------------------------------------------------------------
_therundown_cfg = _load_json(CONFIG_DIR / "therundown.json")
THERUNDOWN_API_KEY = _therundown_cfg.get("api_key", "")
THERUNDOWN_BASE = "https://therundown.io/api/v1"
# Sport IDs: https://therundown.io/api/sports
THERUNDOWN_SPORT_IDS = {
    "nba":   4,
    "mlb":   3,
    "nhl":   6,
    "ncaab": 5,
    "nfl":   1,
}

# ---------------------------------------------------------------------------
# Crypto Monitoring
# ---------------------------------------------------------------------------
CRYPTO_ENABLED = False          # DISABLED — 0W/7L, -$5.83. No edge found in crypto markets.
CRYPTO_ASSETS = ["bitcoin", "ethereum", "solana", "ripple", "dogecoin"]
CRYPTO_CACHE_TTL = 60          # seconds between CoinGecko requests
CRYPTO_SCAN_INTERVAL = 180     # 3 minutes — capture more transient crypto edge
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# ---------------------------------------------------------------------------
# Scheduled Briefings (Eastern Time)
# ---------------------------------------------------------------------------
MORNING_BRIEFING_HOUR = 8      # 8am ET
NIGHTLY_SUMMARY_HOUR = 0       # Midnight ET

# ---------------------------------------------------------------------------
# Sports to scan
# ---------------------------------------------------------------------------
ACTIVE_SPORTS = ["nba", "mlb", "ncaab", "nhl"]

# ---------------------------------------------------------------------------
# NWS Weather — expanded city list
# ---------------------------------------------------------------------------
NWS_CITIES.update({
    "Boston":  (42.3601, -71.0589),
    "DC":      (38.8951, -77.0364),    # Reagan National
    "SF":      (37.6213, -122.3790),   # SFO
    "LA":      (33.9425, -118.4081),   # LAX
    "Seattle": (47.6062, -122.3321),
    "Phoenix": (33.4484, -112.0740),
    "Dallas":  (32.7767, -96.7970),
    "Atlanta": (33.7490, -84.3880),
    "Philadelphia": (39.9526, -75.1652),
    "Las Vegas": (36.1699, -115.1398),
    "Portland": (45.5051, -122.6750),
    "Minneapolis": (44.9778, -93.2650),
    "Nashville": (36.1627, -86.7816),
})

# ---------------------------------------------------------------------------
# Market Making
# ---------------------------------------------------------------------------
MM_MIN_SPREAD_CENTS = 4        # Minimum spread to attempt market making (was 6 — weather ladders usually 3-5¢)
MM_MIN_HOURS_TO_CLOSE = 4      # Don't MM markets closing in < 4h
MM_MAX_OPEN_PAIRS = 10         # Max concurrent MM pairs
MM_CANCEL_AFTER_HOURS = 2      # Cancel unfilled pair after 2h
MM_MAX_CONTRACTS_PER_SIDE = 20 # Max contracts per side
MM_POSITIONS_FILE = BOT_STATE_DIR / "mm_positions.json"

# ---------------------------------------------------------------------------
# Pending opportunities queue
# ---------------------------------------------------------------------------
PENDING_FILE = BOT_STATE_DIR / "pending.json"
PENDING_MAX = 20               # Max queued opportunities
PENDING_GO_WINDOW_HOURS = 2    # Opportunity expires this many hours after market closes

# ---------------------------------------------------------------------------
# Paper trading mode
# ---------------------------------------------------------------------------
# Set PAPER_MODE = True to run without placing real orders.
# All edge detection, sizing, and alerts run normally — only execution is skipped.
# Flip to False only after paper trading shows consistent +CLV.
PAPER_MODE = True
PAPER_STARTING_BALANCE = 500.0  # Simulated starting balance for paper trading ($)

# ---------------------------------------------------------------------------
# Active strategies — only these edge types trigger trades
# ---------------------------------------------------------------------------
# "weather"         — NWS bias correction (next-day markets only)
# "vig_stack_series" — Series ladder NO edge (mechanical, no prediction)
# Add others only after 20+ resolved paper trades with +CLV on each.
ACTIVE_STRATEGIES = [
    "vig_stack_series",       # 79% WR (159/201) — structural arb, best strategy
    "vig_stack_futures",      # Same math as vig_stack_series, applied to championship/futures
    "sports_monotonicity_arb", # Riskless arb — spread/total threshold violations
    "sports_consistency_arb",  # Riskless arb — championship > series violations
]
# Disabled (data-driven, 2026-04-14 audit):
#   btc_price_edge: 33% WR in paper (-$35.06), model overestimates intraday vol
#   eth_price_edge: 0% WR in paper (-$3.14), same vol model issue
#   sol_price_edge: 0% WR in paper (-$4.38), same issue
#   series_game_edge: 26% WR (-$30.95), sportsbook odds already efficient
#   weather: 17% WR (-$4.41), NWS bias model too imprecise
#   live_momentum: 52% WR but avg_loss 2x avg_win = -$104 total (runs separately via watcher)
#   xrp/doge/bnb: all losing, crypto disabled

# ---------------------------------------------------------------------------
# Weather strategy — next-day filter
# ---------------------------------------------------------------------------
# Skip any weather market closing in fewer than this many hours.
# Same-day markets are priced by real-time temperature, not forecasts.
WEATHER_MIN_HOURS_TO_CLOSE = 8

# ---------------------------------------------------------------------------
# Price movement kill switch (executor)
# ---------------------------------------------------------------------------
# Cancel a GO if the Kalshi price has moved more than this many cents
# since the alert was sent. Market is in motion — don't chase.
#
# Default (3¢) is tuned for price-action strategies where price IS the signal.
# Per-strategy overrides in PRICE_MOVE_CENTS_BY_STRATEGY: vig_stack's edge is
# structural (ladder math) — a few cents of drift on long-duration futures
# doesn't invalidate it. Strict 3¢ was silencing vig_stack_futures entirely.
MAX_PRICE_MOVE_CENTS = 3

PRICE_MOVE_CENTS_BY_STRATEGY = {
    "vig_stack_series": 8,    # weather/index ladders — structural edge survives small drift
    "vig_stack_futures": 8,   # championship ladders — price drifts between scans, but ladder math unchanged
    "sports_monotonicity_arb": 5,  # riskless by construction — let through reasonable drift
    "sports_consistency_arb": 5,
    "market_maker": 2,        # MM places tight both-sides; big move = re-quote, don't chase
}

# ---------------------------------------------------------------------------
# State file paths
# ---------------------------------------------------------------------------
POSITIONS_FILE = BOT_STATE_DIR / "positions.json"
TRADE_HISTORY_FILE = BOT_STATE_DIR / "trade_history.json"
ODDS_SNAPSHOTS_FILE = BOT_STATE_DIR / "odds_snapshots.json"
BOT_STATE_FILE = BOT_STATE_DIR / "bot_state.json"
WATCHLIST_FILE = BOT_STATE_DIR / "watchlist.json"
PATTERNS_FILE = BOT_STATE_DIR / "patterns.json"
CLV_FILE = BOT_STATE_DIR / "clv.json"
PAPER_TRADES_FILE = BOT_STATE_DIR / "paper_trades.json"
