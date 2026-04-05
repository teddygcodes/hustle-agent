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
MAX_BET_FRACTION = 0.05        # 5% of balance per trade
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
LINE_MOVEMENT_THRESHOLD = 0.05 # 5pp move = significant

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

# ---------------------------------------------------------------------------
# Risk Limits
# ---------------------------------------------------------------------------
MAX_POSITION_PERCENT = 0.20    # Max 20% of balance in one market
MAX_TOTAL_EXPOSURE = 0.50      # Max 50% of balance deployed
POSITION_MOVE_ALERT = 0.20     # Alert if position moves 20% against
TAKE_PROFIT_THRESHOLD = 0.50   # +50% unrealized → alert to take profit
CUT_LOSS_THRESHOLD = -0.30     # -30% unrealized → alert to cut loss
TRAILING_STOP_DEFAULT = 0.20   # 20% trailing stop default
TRAILING_STOP_MIN_HOLD = 300   # 5 min minimum hold before stop activates

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
CRYPTO_ASSETS = ["bitcoin", "ethereum", "solana", "ripple", "dogecoin"]
CRYPTO_CACHE_TTL = 60          # seconds between CoinGecko requests
CRYPTO_SCAN_INTERVAL = 300     # seconds between standalone crypto scan loop runs
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
MM_MIN_SPREAD_CENTS = 6        # Minimum spread to attempt market making
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
    "weather",
    "vig_stack_series",
    "series_game_edge",
    "econ_cpi_edge",
    "ipl_game_edge",
    "btc_price_edge",   # was missing — BTC scanner was implemented but not gated
    "eth_price_edge",
    "sol_price_edge",   # new
    "xrp_price_edge",   # new
    "doge_price_edge",  # new
]

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
MAX_PRICE_MOVE_CENTS = 3

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
