"""
Kalshi Series Ticker Scanner

Browses Kalshi markets by known series tickers, matches to Bovada/CoinGecko
prices, and finds edges on individual game and BTC markets.
"""

from __future__ import annotations

import math
import json
import ssl
import time as _time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from agent.kalshi_client import get_markets, get_market
from agent.parlay import (
    NBA_TEAM_ALIASES, MLB_TEAM_ALIASES,
    NHL_TEAM_ALIASES, NCAAB_TEAM_ALIASES,
    IPL_TEAM_ALIASES,
)
from bot.config import (
    MIN_RELATIVE_EDGE, BOVADA_BASE, BOVADA_SPORT_PATHS,
    COINGECKO_BASE,
)
from bot.math_engine import _self_check_edge
from bot.odds_scraper import _odds_api_fallback, fetch_consensus_odds, fetch_therundown_odds
from bot.injuries import check_back_to_back as _check_b2b, get_last_10 as _get_l10

import logging
logger = logging.getLogger("nexus.kalshi_series")

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------------------
# Series definitions
# ---------------------------------------------------------------------------

# Series tickers confirmed to have open markets on Kalshi
SPORTS_SERIES: dict[str, str] = {
    "nba":   "KXNBAGAME",
    "mlb":   "KXMLBGAME",
    "nhl":   "KXNHLGAME",
    "ncaab": "KXNCAAMBGAME",
}

# Alias dicts: lowercase abbrev → canonical team name (matches Bovada names)
_ALIAS_DICTS: dict[str, dict[str, str]] = {
    "nba":   NBA_TEAM_ALIASES,
    "mlb":   MLB_TEAM_ALIASES,
    "nhl":   NHL_TEAM_ALIASES,
    "ncaab": NCAAB_TEAM_ALIASES,
}

BTC_SERIES = "KXBTCD"
IPL_SERIES = "KXIPLGAME"
BTC_DAILY_VOL = 0.035   # fallback if CoinGecko is unavailable
COINGECKO_BTC_URL = f"{COINGECKO_BASE}/simple/price?ids=bitcoin&vs_currencies=usd"
COINGECKO_BTC_HISTORY_URL = (
    f"{COINGECKO_BASE}/coins/bitcoin/market_chart"
    "?vs_currency=usd&days=10&interval=daily"
)

# ActionNetwork — free, no key, consensus odds from DK/FD/BetMGM/bet365
# Covers today + tomorrow; cached 15 min (fresh enough for series scanner)
_AN_BASE = "https://api.actionnetwork.com/web/v1/scoreboard"
_AN_BOOK_IDS = "15,30,76,123,69"  # DraftKings, FanDuel, bet365, BetMGM, PointsBet
_AN_SPORT_SLUGS: dict[str, str] = {
    "nba": "nba", "mlb": "mlb", "nhl": "nhl", "ncaab": "ncaab",
}
_AN_CACHE: dict[str, tuple[float, list]] = {}
_AN_CACHE_TTL = 900  # 15 min

# TTL cache for Odds API lookups — 500/month free tier = ~15/day with 8h cache
# With ActionNetwork covering today/tomorrow, Odds API is only needed for 2-5 day games.
# 5 sports × 2 calls/day = 10/day = 300/month (comfortable buffer)
_ODDS_API_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_ODDS_API_CACHE_TTL = 28800  # 8 hours — was 1800 (30 min) which burned ~7,200 calls/month

# Companion game map: sport → {team_name_lower → {home_team, away_team, home_away}}
# Populated during _build_odds_api_lookup(); used for Elo cross-check.
# Cached alongside _ODDS_API_CACHE so cache hits also restore the game map.
_ODDS_API_GAME_MAP: dict[str, dict[str, dict]] = {}
_ODDS_API_GAME_MAP_CACHE: dict[str, tuple[float, dict[str, dict]]] = {}

# BTC realized vol cache — 30 min TTL
_BTC_VOL_CACHE: tuple[float, float] | None = None
_BTC_VOL_CACHE_TTL = 1800  # 30 min

# ETH constants and cache
ETH_SERIES = "KXETHD"
ETH_DAILY_VOL = 0.045   # fallback if CoinGecko unavailable
COINGECKO_ETH_URL = f"{COINGECKO_BASE}/simple/price?ids=ethereum&vs_currencies=usd"
COINGECKO_ETH_HISTORY_URL = (
    f"{COINGECKO_BASE}/coins/ethereum/market_chart"
    "?vs_currency=usd&days=10&interval=daily"
)

_ETH_VOL_CACHE: tuple[float, float] | None = None
_ETH_SPOT_CACHE: tuple[float, float] | None = None
_ETH_CACHE_TTL = 1800  # 30 min

# ---------------------------------------------------------------------------
# Multi-asset, multi-timeframe crypto scanner
# ---------------------------------------------------------------------------

# Per-asset spot/vol caches (keyed by CoinGecko asset id)
_CRYPTO_SPOT_CACHE: dict[str, dict] = {}
_CRYPTO_VOL_CACHE: dict[str, dict] = {}
_CRYPTO_SPOT_TTL = 15     # 15 sec — spot prices move fast
_CRYPTO_VOL_TTL = 1800    # 30 min — vol is stable intraday

# Binance symbol mapping (CoinGecko id → Binance trading pair)
# Coinbase pair mapping (CoinGecko id → Coinbase trading pair)
_COINBASE_PAIRS: dict[str, str] = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "solana": "SOL-USD",
    "ripple": "XRP-USD",
    "dogecoin": "DOGE-USD",
    "binancecoin": "BNB-USD",
    "hyperliquid": "HYPE-USD",
}

# Asset config: coingecko_id → (fallback_vol, vol_cap_low, vol_cap_high, confidence, opp_type)
CRYPTO_ASSETS_CONFIG: dict[str, tuple] = {
    "bitcoin":  (0.035, 0.010, 0.12, 0.70, "btc_price_edge"),
    "ethereum": (0.045, 0.010, 0.15, 0.68, "eth_price_edge"),
    "solana":   (0.055, 0.015, 0.18, 0.65, "sol_price_edge"),
    "ripple":   (0.050, 0.010, 0.15, 0.60, "xrp_price_edge"),
    "dogecoin":    (0.070, 0.020, 0.20, 0.55, "doge_price_edge"),
    "binancecoin": (0.045, 0.010, 0.15, 0.60, "bnb_price_edge"),
    "hyperliquid": (0.060, 0.015, 0.20, 0.55, "hype_price_edge"),
}

# (asset_id, timeframe) → Kalshi series ticker
# Tickers inferred from Kalshi naming convention; fail-safe: _fetch_series_markets returns []
CRYPTO_SERIES_MAP: dict[tuple, str] = {
    # Daily (5pm EDT / 21:00 UTC)
    ("bitcoin",  "daily"): "KXBTCD",
    ("ethereum", "daily"): "KXETHD",
    ("solana",   "daily"): "KXSOLD",
    ("ripple",   "daily"): "KXXRPD",
    ("dogecoin", "daily"): "KXDOGED",
    # Hourly
    ("bitcoin",  "hourly"): "KXBTC",
    ("ethereum", "hourly"): "KXETH",
    ("solana",   "hourly"): "KXSOL",
    ("ripple",   "hourly"): "KXXRP",
    ("dogecoin", "hourly"): "KXDOGE",
    # 15-minute
    ("bitcoin",  "15min"): "KXBTC15M",
    ("ethereum", "15min"): "KXETH15M",
    ("solana",   "15min"): "KXSOL15M",
    ("ripple",   "15min"): "KXXRP15M",
    ("dogecoin", "15min"): "KXDOGE15M",
    # BNB
    ("binancecoin", "daily"): "KXBNBD",
    ("binancecoin", "hourly"): "KXBNB",
    ("binancecoin", "15min"): "KXBNB15M",
    # HYPE (Hyperliquid)
    ("hyperliquid", "daily"): "KXHYPED",
    ("hyperliquid", "hourly"): "KXHYPE",
    ("hyperliquid", "15min"): "KXHYPE15M",
}

# Active timeframes — weekly TBD (needs longer vol window)
ACTIVE_CRYPTO_TIMEFRAMES = ["15min", "hourly", "daily"]

# Min hours remaining to enter — skip markets too close to expiry to execute safely
_TIMEFRAME_MIN_HOURS: dict[str, float] = {"15min": 0.04, "hourly": 0.10, "daily": 0.50}


# ---------------------------------------------------------------------------
# ELO fallback helper
# ---------------------------------------------------------------------------

def _elo_fallback_prob(
    ticker: str,
    abbrev: str,
    canonical: str | None,
    alias_dict: dict,
    sport: str,
) -> float | None:
    """
    Use Elo ratings as fallback probability when no bookmaker odds are available.
    Parses the opponent's abbreviation from the ticker's middle segment.
    Convention: away team abbreviation listed first in the ticker, then home team.
    Returns P(our team wins) or None if Elo can't compute.
    """
    import re as _re
    if not canonical:
        return None
    try:
        parts = ticker.split("-")
        if len(parts) < 3:
            return None
        date_seg = parts[1]  # e.g. "26APR04SASDEN" or "26APR041410TORCWS"
        m = _re.match(r"\d{2}[A-Z]{3}\d{2}(?:\d{4})?([A-Z]+)", date_seg)
        if not m:
            return None
        teams_part = m.group(1)  # e.g. "SASDEN"
        abbrev_upper = abbrev.upper()

        if teams_part.startswith(abbrev_upper):
            # Our team is listed first = AWAY
            opp_abbrev = teams_part[len(abbrev_upper):]
            is_home = False
        elif teams_part.endswith(abbrev_upper):
            # Our team is listed second = HOME
            opp_abbrev = teams_part[:-len(abbrev_upper)]
            is_home = True
        else:
            return None

        if not opp_abbrev:
            return None
        opp_canonical = alias_dict.get(opp_abbrev.lower())
        if not opp_canonical:
            return None

        from bot.elo import get_elo_prob as _get_elo_prob
        home_name = canonical if is_home else opp_canonical
        away_name = opp_canonical if is_home else canonical
        elo_raw = _get_elo_prob(home_name, away_name, sport)
        if elo_raw is None:
            return None
        prob = elo_raw if is_home else (1.0 - elo_raw)
        return round(prob, 4)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: int = 12, max_retries: int = 3) -> dict | list | None:
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "application/json",
                }
            )
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < max_retries - 1:
                _time.sleep(2 ** attempt)  # 1s, 2s backoff
            else:
                logger.warning("SeriesHTTP error fetching %s: %s", url[:80], e)
    return None


# ---------------------------------------------------------------------------
# Kalshi series helpers
# ---------------------------------------------------------------------------

def _fetch_series_markets(series_ticker: str) -> list[dict]:
    """Paginate through all open markets for a series ticker."""
    markets = []
    cursor = None
    while True:
        kwargs: dict = {"series_ticker": series_ticker, "status": "open", "limit": 100}
        if cursor:
            kwargs["cursor"] = cursor
        result = get_markets(**kwargs)
        if "error" in result:
            logger.warning("Series error fetching %s: %s", series_ticker, result['error'])
            break
        batch = result.get("markets", [])
        markets.extend(batch)
        cursor = result.get("cursor")
        if not cursor or not batch:
            break
    return markets


def _extract_team_abbrev(ticker: str) -> str | None:
    """
    Extract team abbreviation from Kalshi ticker.
    e.g. 'KXNBAGAME-26APR05HOUGSW-HOU' → 'hou'
    """
    parts = ticker.split("-")
    if len(parts) >= 3:
        return parts[-1].lower()
    return None


def _resolve_team_name(abbrev: str, alias_dict: dict[str, str]) -> str | None:
    """Map lowercase abbreviation to canonical team name via alias dict."""
    return alias_dict.get(abbrev)


# ---------------------------------------------------------------------------
# Bovada helpers
# ---------------------------------------------------------------------------

def _fetch_bovada_game_lines(sport: str) -> list[dict]:
    """
    Fetch Bovada game lines for a sport.
    Returns list of {home_team, away_team, home_prob, away_prob}.
    """
    path = BOVADA_SPORT_PATHS.get(sport.lower())
    if not path:
        return []
    url = f"{BOVADA_BASE}/{path}"
    data = _get_json(url)
    if not isinstance(data, list):
        return []

    # Bovada wraps events: [{path: ..., events: [...]}]
    raw_events: list[dict] = []
    for item in data:
        if isinstance(item, dict) and "events" in item:
            raw_events.extend(item["events"])
        elif isinstance(item, dict) and "competitors" in item:
            raw_events.append(item)

    results = []
    for event in raw_events:
        # Skip live and completed games — pregame odds are stale once a game starts.
        # Bovada sets live=True for in-progress games; completed games are typically
        # dropped from the feed entirely.
        if event.get("live"):
            continue

        competitors = event.get("competitors", [])
        if len(competitors) < 2:
            continue
        home_team = next((c["name"] for c in competitors if c.get("home")), None)
        away_team = next((c["name"] for c in competitors if not c.get("home")), None)
        if not home_team or not away_team:
            continue

        # Find moneyline in displayGroups
        home_ml = away_ml = None
        for dg in event.get("displayGroups", []):
            if "game line" not in dg.get("description", "").lower():
                continue
            for mkt in dg.get("markets", []):
                if "moneyline" not in mkt.get("description", "").lower():
                    continue
                outcomes = mkt.get("outcomes", [])
                if len(outcomes) >= 2:
                    # Match each outcome to home/away by description (team name)
                    # rather than assuming outcomes[0]=away, outcomes[1]=home.
                    home_last = home_team.split()[-1].lower()
                    away_last = away_team.split()[-1].lower()
                    for oc in outcomes:
                        desc = oc.get("description", "").lower()
                        price = oc.get("price", {}).get("american", "")
                        if home_last in desc or home_team.lower() in desc:
                            home_ml = price
                        elif away_last in desc or away_team.lower() in desc:
                            away_ml = price
                    # Fallback to index order if description matching failed
                    if not home_ml or not away_ml:
                        away_ml = outcomes[0].get("price", {}).get("american", "")
                        home_ml = outcomes[1].get("price", {}).get("american", "")
                break
            if home_ml:
                break

        if not home_ml or not away_ml:
            continue

        def _implied(american: str) -> float:
            s = str(american).strip().upper()
            if s in ("EVEN", "PK"):
                return 0.5
            try:
                n = int(s.replace("+", ""))
                if n > 0:
                    return 100 / (100 + n)
                return abs(n) / (abs(n) + 100)
            except (ValueError, TypeError):
                return 0.0

        hi = _implied(home_ml)
        ai = _implied(away_ml)
        total = hi + ai
        if total <= 0:
            continue
        results.append({
            "home_team": home_team,
            "away_team": away_team,
            "home_prob": hi / total,
            "away_prob": ai / total,
        })
    return results


def _build_bovada_lookup(game_lines: list[dict]) -> dict[str, float]:
    """Build lowercase-team-name → true_prob dict from game lines."""
    lookup: dict[str, float] = {}
    for gl in game_lines:
        lookup[gl["home_team"].lower()] = gl["home_prob"]
        lookup[gl["away_team"].lower()] = gl["away_prob"]
        # Also add last-word key for fuzzy matching
        lookup[gl["home_team"].split()[-1].lower()] = gl["home_prob"]
        lookup[gl["away_team"].split()[-1].lower()] = gl["away_prob"]
    return lookup


_ODDS_API_FAIL_TTL = 300  # 5 min retry on failure vs 30 min on success


def _fetch_action_network(sport: str) -> list[dict]:
    """
    Fetch upcoming games with consensus moneylines from ActionNetwork.
    No API key required. Returns list of game dicts with 'home_team', 'away_team',
    'commence_time', and 'consensus' {team_name: vig_free_prob}.
    Cached 15 minutes per sport.
    """
    cached = _AN_CACHE.get(sport)
    if cached and (_time.monotonic() - cached[0]) < _AN_CACHE_TTL:
        return cached[1]

    slug = _AN_SPORT_SLUGS.get(sport.lower())
    if not slug:
        return []

    url = f"{_AN_BASE}/{slug}?period=game&bookIds={_AN_BOOK_IDS}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer": "https://www.actionnetwork.com/",
            },
        )
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as r:
            data = json.loads(r.read())
    except Exception as e:
        logger.warning("ActionNetwork/%s fetch failed: %s", sport.upper(), e)
        _AN_CACHE[sport] = (_time.monotonic(), [])
        return []

    results = []
    for game in data.get("games", []):
        teams = game.get("teams", [])
        if len(teams) < 2:
            continue
        away_id = game.get("away_team_id")
        home_team = next((t["full_name"] for t in teams if t["id"] != away_id), None)
        away_team = next((t["full_name"] for t in teams if t["id"] == away_id), None)
        if not home_team or not away_team:
            continue

        # Build consensus from all available books
        away_probs, home_probs = [], []
        for odds_entry in game.get("odds", []):
            ml_away = odds_entry.get("ml_away")
            ml_home = odds_entry.get("ml_home")
            if ml_away is None or ml_home is None:
                continue
            try:
                def _to_implied(ml):
                    ml = float(ml)
                    if ml > 0:
                        return 100.0 / (ml + 100.0)
                    return abs(ml) / (abs(ml) + 100.0)
                raw_a = _to_implied(ml_away)
                raw_h = _to_implied(ml_home)
                total = raw_a + raw_h
                if total > 0:
                    away_probs.append(raw_a / total)
                    home_probs.append(raw_h / total)
            except (TypeError, ValueError):
                continue

        if not away_probs:
            continue

        avg_away = sum(away_probs) / len(away_probs)
        avg_home = sum(home_probs) / len(home_probs)
        results.append({
            "home_team": home_team,
            "away_team": away_team,
            "commence_time": game.get("start_time", ""),
            "consensus": {home_team: avg_home, away_team: avg_away},
        })

    _AN_CACHE[sport] = (_time.monotonic(), results)
    return results


def _build_odds_api_lookup(sport: str) -> dict[str, float]:
    """
    Build lowercase-team-name → vig-removed-prob dict.

    Source priority:
      1. ActionNetwork — free, no key, today + tomorrow (15 min cache)
      2. The Odds API  — 2-5 day horizon gap-fill (8h cache, ~300 calls/month)
      3. Free chain overlay (DK/Bovada/FanDuel) — freshest today prices

    ActionNetwork replacing Odds API for 1-2 day games cuts monthly API usage
    from ~450 to ~300 calls and removes the rate-limit dependency for same-day games.
    Cached per-sport for 8 hours (success) or 5 minutes (failure/empty).
    """
    cached = _ODDS_API_CACHE.get(sport)
    if cached:
        age = _time.monotonic() - cached[0]
        ttl = _ODDS_API_FAIL_TTL if cached[1] == {} else _ODDS_API_CACHE_TTL
        if age < ttl:
            gm_cached = _ODDS_API_GAME_MAP_CACHE.get(sport)
            if gm_cached:
                _ODDS_API_GAME_MAP[sport] = gm_cached[1]
            return cached[1]

    now_utc = datetime.now(timezone.utc)

    def _add_games_to_lookup(games: list[dict], lookup: dict, override: bool = False):
        """Merge game consensus into lookup, populating game map. override=True replaces existing."""
        if sport not in _ODDS_API_GAME_MAP:
            _ODDS_API_GAME_MAP[sport] = {}
        for game in games:
            if not game.get("consensus"):
                continue
            home = game.get("home_team", "?")
            away = game.get("away_team", "?")
            commence_raw = game.get("commence_time", "")
            if commence_raw:
                try:
                    commence_dt = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
                    # Keep pre-game odds for games that started within the last 2 hours —
                    # the series scanner has its own stale-odds check (hours_to_game < -2).
                    if commence_dt < now_utc - timedelta(hours=2):
                        continue
                except (ValueError, TypeError):
                    pass
            for team_name, prob in game["consensus"].items():
                if prob and prob > 0:
                    key = team_name.lower()
                    words = team_name.split()
                    last_word = words[-1].lower()
                    first_word = words[0].lower()
                    if override or key not in lookup:
                        lookup[key] = prob
                        lookup[last_word] = prob
                        # First-word key helps when canonical uses different spelling
                        # e.g. "Royal Challengers Bangalore" vs "Royal Challengers Bengaluru"
                        # — first word "royal" maps to both
                        if first_word not in lookup:
                            lookup[first_word] = prob
                        _ODDS_API_GAME_MAP[sport][key] = {
                            "home_team": home,
                            "away_team": away,
                            "home_away": "home" if team_name == home else "away",
                        }

    lookup: dict[str, float] = {}

    # Step 1: ActionNetwork — free, no key, today + tomorrow consensus odds
    an_games = _fetch_action_network(sport)
    if an_games:
        _add_games_to_lookup(an_games, lookup)
        logger.debug("OddsAPI/%s ActionNetwork: %d games | %d teams",
                     sport.upper(), len(an_games), len(lookup) // 2)
    else:
        logger.warning("OddsAPI/%s ActionNetwork: no data", sport.upper())

    # Step 2: TheRundown — free key, 20k pts/day, Pinnacle lines, fills horizon gaps
    try:
        tr_result = fetch_therundown_odds(sport)
        if "error" not in tr_result:
            tr_games = tr_result.get("games", [])
            _add_games_to_lookup(tr_games, lookup)
            logger.debug("OddsAPI/%s TheRundown: %d games | %d teams",
                         sport.upper(), len(tr_games), len(lookup) // 2)
        else:
            logger.warning("OddsAPI/%s TheRundown: %s", sport.upper(), tr_result.get("error"))
    except Exception as e:
        logger.warning("OddsAPI/%s TheRundown exception: %s", sport.upper(), e)

    # Step 3: Overlay free chain (DK/Bovada/FanDuel) — freshest same-day prices take priority
    chain_result = fetch_consensus_odds(sport)
    chain_games = [g for g in chain_result.get("games", []) if g.get("consensus")]
    if chain_games:
        _add_games_to_lookup(chain_games, lookup, override=True)
        source = chain_result.get("source", "free_chain")
        logger.debug("OddsAPI/%s +%s overlay: %d total teams",
                     sport.upper(), source, len(lookup) // 2)

    # Step 4: The Odds API — last resort only, when all free sources returned nothing
    if not lookup:
        logger.warning("OddsAPI/%s all free sources empty — falling back to The Odds API (last resort)",
                       sport.upper())
        try:
            result = _odds_api_fallback(sport)
            if "error" not in result:
                raw_games = result.get("games", [])
                _add_games_to_lookup(raw_games, lookup)
                skipped_no_consensus = sum(1 for g in raw_games if not g.get("consensus"))
                logger.debug("OddsAPI/%s Odds API fallback: %d games | %d teams | %d no-consensus",
                             sport.upper(), len(raw_games), len(lookup) // 2, skipped_no_consensus)
            else:
                logger.warning("OddsAPI/%s Odds API error: %s", sport.upper(), result["error"])
        except Exception as e:
            logger.warning("OddsAPI/%s Odds API exception: %s", sport.upper(), e)

    _ODDS_API_CACHE[sport] = (_time.monotonic(), lookup if lookup else {})
    if sport in _ODDS_API_GAME_MAP:
        _ODDS_API_GAME_MAP_CACHE[sport] = (_time.monotonic(), _ODDS_API_GAME_MAP[sport])
    return lookup


# ---------------------------------------------------------------------------
# Sports series scanner
# ---------------------------------------------------------------------------

def scan_sports_series(sport: str) -> list[dict]:
    """
    Scan a sport's Kalshi series for edges vs Bovada consensus.
    Returns list of opportunity dicts.
    """
    series_ticker = SPORTS_SERIES.get(sport.lower())
    if not series_ticker:
        return []

    alias_dict = _ALIAS_DICTS.get(sport.lower(), {})
    markets = _fetch_series_markets(series_ticker)
    if not markets:
        logger.warning("Series/%s no open markets for %s", sport.upper(), series_ticker)
        return []

    game_lines = _fetch_bovada_game_lines(sport)
    bovada_lookup = _build_bovada_lookup(game_lines)

    # Seed _ODDS_API_GAME_MAP from Bovada lines so Elo cross-check always has game context,
    # even when the Odds API cache is stale or empty.
    if game_lines:
        if sport not in _ODDS_API_GAME_MAP:
            _ODDS_API_GAME_MAP[sport] = {}
        for _gl in game_lines:
            for _team, _ha in ((_gl["home_team"], "home"), (_gl["away_team"], "away")):
                _key = _team.lower()
                _ODDS_API_GAME_MAP[sport][_key] = {
                    "home_team": _gl["home_team"],
                    "away_team": _gl["away_team"],
                    "home_away": _ha,
                }
                # Also add last-word key for fuzzy matching (mirrors bovada_lookup)
                _lw = _team.split()[-1].lower()
                if _lw not in _ODDS_API_GAME_MAP[sport]:
                    _ODDS_API_GAME_MAP[sport][_lw] = _ODDS_API_GAME_MAP[sport][_key]

    # Always fetch Odds API as a supplement — covers games Bovada doesn't have yet
    # (e.g. games 48+ hours out). Cached 30 min so it doesn't burn the rate limit.
    odds_api_lookup = _build_odds_api_lookup(sport)

    logger.info("Series/%s %d open markets | %d Bovada games | %d Odds API teams",
                sport.upper(), len(markets), len(game_lines), len(odds_api_lookup) // 2)

    now_utc = datetime.now(timezone.utc)
    opportunities = []
    for market in markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        # Fix #2: Skip illiquid markets — stale prices with no real counterparty
        volume = market.get("volume") or 0
        open_interest = market.get("open_interest") or 0
        if volume < 10 and open_interest < 5:
            continue

        abbrev = _extract_team_abbrev(ticker)
        if not abbrev:
            continue

        canonical = _resolve_team_name(abbrev, alias_dict)
        prob = None
        prob_source = "none"

        if canonical:
            _cwords = canonical.split()
            prob = (
                bovada_lookup.get(canonical.lower())
                or bovada_lookup.get(_cwords[-1].lower())
                or bovada_lookup.get(_cwords[0].lower())
            )
            if prob:
                prob_source = "bovada"
            else:
                prob = (
                    odds_api_lookup.get(canonical.lower())
                    or odds_api_lookup.get(_cwords[-1].lower())
                    or odds_api_lookup.get(_cwords[0].lower())
                )
                if prob:
                    prob_source = "odds_api"
        else:
            # Try direct lookup by abbrev as last-word match
            prob = bovada_lookup.get(abbrev) or odds_api_lookup.get(abbrev)
            if prob:
                prob_source = "bovada" if abbrev in bovada_lookup else "odds_api"

        if prob is None or prob <= 0:
            # ELO fallback: parse opponent from ticker, use Elo win probability
            prob = _elo_fallback_prob(ticker, abbrev, canonical, alias_dict, sport)
            if prob is not None:
                prob_source = "elo_fallback"
            else:
                logger.debug("Series/%s SKIP %s: no match for abbrev=%r canonical=%r source=none",
                             sport.upper(), ticker, abbrev, canonical)
                continue

        # Fix #5: Time-to-game edge requirement — distant games need a larger edge
        # because the odds are less reliable (more uncertainty, less sharp money).
        # Parse game date (and optional time) from ticker like "26APR041410TORCWS".
        # MLB tickers include HHMM in Eastern Time; other sports use date only.
        hours_to_game = 0.0
        game_dt = None
        try:
            parts = ticker.split("-")
            if len(parts) >= 3:
                date_seg = parts[1]  # e.g. "26APR041410TORCWS" or "26APR05CAROTT"
                import re as _re
                # Capture YYMONDD + optional HHMM (game start time in ET)
                m = _re.match(r"(\d{2})([A-Z]{3})(\d{2})(\d{4})?", date_seg)
                if m:
                    yy, mon, dd = m.group(1), m.group(2), m.group(3)
                    time_str = m.group(4)  # e.g. "1410" or None
                    month_map = {
                        "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                        "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12
                    }
                    yr, mo, dy = 2000 + int(yy), month_map.get(mon, 1), int(dd)
                    if time_str:
                        # Convert ET→UTC: EDT (Apr–Oct) = UTC-4, EST (Nov–Mar) = UTC-5
                        et_offset_hours = 4 if 3 <= mo <= 10 else 5
                        from datetime import timedelta as _td
                        hh, mm = int(time_str[:2]), int(time_str[2:])
                        game_dt = datetime(yr, mo, dy, hh, mm, tzinfo=timezone.utc) + _td(hours=et_offset_hours)
                    else:
                        # No time in ticker — use start of game day (UTC midnight)
                        game_dt = datetime(yr, mo, dy, tzinfo=timezone.utc)
                    hours_to_game = (game_dt - now_utc).total_seconds() / 3600
        except Exception:
            pass

        # Skip games that have already started — odds from cache may be stale
        if game_dt and hours_to_game < 0:
            logger.debug("Series/%s SKIP %s: game started %.1fh ago (stale odds)",
                         sport.upper(), ticker, -hours_to_game)
            continue

        # Back-to-back check for the team we're betting on
        team_b2b = False
        if canonical and game_dt:
            try:
                team_b2b = _check_b2b(canonical, sport, game_dt)
                if team_b2b:
                    logger.debug("Series/%s B2B: %s", sport.upper(), canonical)
            except Exception:
                pass

        # Require larger edge for games more than 48h away (less reliable pricing)
        min_edge_required = MIN_RELATIVE_EDGE
        if hours_to_game > 48:
            min_edge_required = MIN_RELATIVE_EDGE + (hours_to_game - 48) * 0.002

        kalshi_price = yes_ask / 100.0
        edge = prob - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        check_ok, check_msg = _self_check_edge(prob, kalshi_price, edge)
        if not check_ok:
            continue

        if kalshi_price <= 0.03 or prob <= 0.03:
            continue

        if abs(relative_edge) < min_edge_required:
            continue

        # Sanity cap: edges > 150% are almost certainly stale/live-game prices
        if abs(relative_edge) > 1.5:
            logger.debug("Series/%s SKIP %s: relative_edge=%.1f%% exceeds sanity cap (likely live game)",
                         sport.upper(), ticker, relative_edge * 100)
            continue

        # Confidence scales down for distant games (less reliable odds)
        confidence = 0.80
        if hours_to_game > 72:
            confidence = 0.65
        elif hours_to_game > 48:
            confidence = 0.72

        if prob_source == "elo_fallback":
            confidence = min(confidence, 0.65)  # Elo is less sharp than bookmaker lines

        # Elo cross-check: boost confidence when Elo agrees with books, cut when it disagrees
        elo_prob = None
        elo_agrees = None
        opp_b2b = False
        try:
            from bot.elo import get_elo_prob as _get_elo_prob
            game_info = _ODDS_API_GAME_MAP.get(sport, {}).get(
                canonical.lower() if canonical else ""
            )
            if game_info:
                home_team = game_info["home_team"]
                away_team = game_info["away_team"]
                is_home   = game_info["home_away"] == "home"
                # Check opponent B2B and assign flags to correct home/away slot
                opp_name = away_team if is_home else home_team
                if game_dt:
                    try:
                        opp_b2b = _check_b2b(opp_name, sport, game_dt)
                        if opp_b2b:
                            logger.debug("Series/%s B2B (opp): %s", sport.upper(), opp_name)
                    except Exception:
                        pass
                home_b2b = team_b2b if is_home else opp_b2b
                away_b2b = opp_b2b if is_home else team_b2b
                elo_raw = _get_elo_prob(home_team, away_team, sport,
                                        home_b2b=home_b2b, away_b2b=away_b2b)
                if elo_raw is not None:
                    elo_prob = round(elo_raw if is_home else 1.0 - elo_raw, 4)
                    books_edge = (prob - kalshi_price) * (1 if edge > 0 else -1)
                    elo_edge   = (elo_prob - kalshi_price) * (1 if edge > 0 else -1)
                    if books_edge > 0 and elo_edge > 0:
                        confidence = min(0.92, confidence + 0.12)
                        elo_agrees = True
                    elif books_edge > 0 and elo_edge <= 0:
                        confidence = max(0.50, confidence - 0.15)
                        elo_agrees = False
        except Exception as _elo_exc:
            logger.debug("Elo cross-check failed for %s: %s", canonical, _elo_exc)
            elo_prob = None

        # Derive opponent directly from Kalshi ticker (reliable — no multi-game map collision)
        opponent_team = ""
        try:
            import re as _re2
            _parts = ticker.split("-")
            if len(_parts) >= 3:
                _m = _re2.match(r"\d{2}[A-Z]{3}\d{2}(?:\d{4})?([A-Z]+)", _parts[1])
                if _m:
                    _teams = _m.group(1)
                    _au = abbrev.upper()
                    _opp_abbrev = _teams[len(_au):] if _teams.startswith(_au) else (
                        _teams[:-len(_au)] if _teams.endswith(_au) else ""
                    )
                    if _opp_abbrev:
                        opponent_team = _resolve_team_name(_opp_abbrev.lower(), alias_dict) or ""
        except Exception:
            pass

        # Last-10 records for both teams
        team_l10 = None
        opp_l10  = None
        try:
            if canonical:
                team_l10 = _get_l10(canonical, sport)
            if opponent_team:
                opp_l10 = _get_l10(opponent_team, sport)
        except Exception:
            pass

        # Formatted game date from ticker (e.g. "Sat Apr 5")
        game_date_str = None
        if game_dt:
            game_date_str = game_dt.strftime("%a %b") + f" {game_dt.day}"

        opportunities.append({
            "type": "series_game_edge",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": confidence,
            "recommended_side": "yes" if edge > 0 else "no",
            "odds_prob": round(prob, 4),
            "odds_source": prob_source,
            "kalshi_price": round(kalshi_price, 4),
            "team_abbrev": abbrev,
            "canonical_team": canonical,
            "opponent_team": opponent_team,
            "sport": sport,
            "hours_to_game": round(hours_to_game, 1),
            "game_date_str": game_date_str,
            "b2b": team_b2b,
            "opp_b2b": opp_b2b,
            "l10": team_l10,
            "opp_l10": opp_l10,
            "elo_prob": elo_prob,
            "elo_agrees": elo_agrees,
            "edge_result": {
                "fair_value": round(prob, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": confidence,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities


# ---------------------------------------------------------------------------
# Bitcoin series scanner
# ---------------------------------------------------------------------------

def _get_btc_realized_vol() -> float:
    """
    Compute 10-day realized daily volatility from CoinGecko daily closes.
    Returns daily vol (not annualized). Falls back to BTC_DAILY_VOL on any error.
    """
    global _BTC_VOL_CACHE
    if _BTC_VOL_CACHE and (_time.monotonic() - _BTC_VOL_CACHE[0]) < _BTC_VOL_CACHE_TTL:
        return _BTC_VOL_CACHE[1]

    data = _get_json(COINGECKO_BTC_HISTORY_URL)
    if not data:
        return BTC_DAILY_VOL

    prices = [p[1] for p in data.get("prices", []) if len(p) == 2]
    if len(prices) < 4:
        return BTC_DAILY_VOL

    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    # Each return is already a daily return — variance is daily directly
    daily_variance = sum(r ** 2 for r in log_returns) / len(log_returns)
    daily_vol = math.sqrt(daily_variance)
    # Floor at 1%, ceiling at 12% — 10-day daily window
    daily_vol = max(0.01, min(0.12, daily_vol))

    logger.debug("Series/BTC realized 10d vol: %.2f%% (from %d daily closes)", daily_vol * 100, len(prices))
    _BTC_VOL_CACHE = (_time.monotonic(), daily_vol)
    return daily_vol


def _get_btc_spot() -> float | None:
    """Fetch BTC/USD spot price from CoinGecko."""
    data = _get_json(COINGECKO_BTC_URL)
    if not data:
        return None
    try:
        return float(data["bitcoin"]["usd"])
    except (KeyError, TypeError, ValueError):
        return None


def _get_eth_realized_vol() -> float:
    """Compute 10-day realized daily volatility for ETH from CoinGecko daily closes."""
    global _ETH_VOL_CACHE
    if _ETH_VOL_CACHE and (_time.monotonic() - _ETH_VOL_CACHE[0]) < _ETH_CACHE_TTL:
        return _ETH_VOL_CACHE[1]
    data = _get_json(COINGECKO_ETH_HISTORY_URL)
    if not data:
        return ETH_DAILY_VOL
    prices = [p[1] for p in data.get("prices", []) if len(p) == 2]
    if len(prices) < 4:
        return ETH_DAILY_VOL
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    # Each return is already a daily return — variance is daily directly
    daily_variance = sum(r ** 2 for r in log_returns) / len(log_returns)
    daily_vol = math.sqrt(daily_variance)
    daily_vol = max(0.01, min(0.15, daily_vol))  # 1%-15%, 10-day daily window
    logger.debug("Series/ETH realized 10d vol: %.2f%%", daily_vol * 100)
    _ETH_VOL_CACHE = (_time.monotonic(), daily_vol)
    return daily_vol


def _get_eth_spot() -> float | None:
    """Fetch ETH/USD spot price from CoinGecko."""
    global _ETH_SPOT_CACHE
    if _ETH_SPOT_CACHE and (_time.monotonic() - _ETH_SPOT_CACHE[0]) < _ETH_CACHE_TTL:
        return _ETH_SPOT_CACHE[1]
    data = _get_json(COINGECKO_ETH_URL)
    if not data:
        return None
    try:
        price = float(data["ethereum"]["usd"])
        _ETH_SPOT_CACHE = (_time.monotonic(), price)
        return price
    except (KeyError, TypeError, ValueError):
        return None


def _btc_normal_prob(spot: float, threshold: float, hours_remaining: float) -> float:
    """
    P(BTC_close > threshold) using log-normal approximation.

    Realized 24h vol from CoinGecko hourly prices, scaled to hours remaining.
    Returns probability that BTC closes above threshold.
    """
    if hours_remaining <= 0 or spot <= 0 or threshold <= 0:
        return 0.0
    vol = _get_btc_realized_vol() * math.sqrt(hours_remaining / 24.0)
    log_ratio = math.log(spot / threshold)
    # z = log_ratio / vol; P(above) = 0.5 * erfc(-z / sqrt(2))
    z = log_ratio / vol if vol > 0 else 0.0
    return 0.5 * math.erfc(-z / math.sqrt(2))


def scan_bitcoin_series() -> list[dict]:
    """
    Scan KXBTCD series for edges vs CoinGecko spot price.

    Only considers markets that resolve TODAY (based on ticker date segment),
    since far-future markets have negligible edge vs spot.
    Returns list of opportunity dicts.
    """
    markets = _fetch_series_markets(BTC_SERIES)
    if not markets:
        logger.warning("Series/BTC no open markets for %s", BTC_SERIES)
        return []

    spot = _get_btc_spot()
    if not spot:
        logger.warning("Series/BTC could not fetch BTC spot price")
        return []

    now_utc = datetime.now(timezone.utc)
    # Kalshi BTC markets resolve at 21:00 UTC daily (5PM EDT)
    resolve_today = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
    hours_remaining = (resolve_today - now_utc).total_seconds() / 3600.0

    if hours_remaining < 0:
        logger.warning("Series/BTC all today's markets already resolved (past 21:00 UTC)")
        return []

    logger.info("Series/BTC %d open markets | spot=$%s | hours_remaining=%.1fh",
                len(markets), f"{spot:,.0f}", hours_remaining)

    # Parse today's date as used in tickers (e.g. '26APR04')
    today_str = now_utc.strftime("%y%b%d").upper()

    opportunities = []
    for market in markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        # Only today's markets: ticker contains today's date string
        if today_str not in ticker:
            continue

        # Extract threshold from ticker: KXBTCD-26APR0417-T95000 → 95000
        threshold = None
        for part in ticker.split("-"):
            if part.startswith("T") and part[1:].isdigit():
                threshold = float(part[1:])
                break
        if threshold is None:
            continue

        fair_value = _btc_normal_prob(spot, threshold, hours_remaining)
        kalshi_price = yes_ask / 100.0
        edge = fair_value - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        check_ok, check_msg = _self_check_edge(fair_value, kalshi_price, edge)
        if not check_ok:
            continue

        if kalshi_price <= 0.03 or fair_value <= 0.03:
            continue

        if abs(relative_edge) < MIN_RELATIVE_EDGE:
            continue

        opportunities.append({
            "type": "btc_price_edge",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": 0.70,  # Lower confidence — vol estimate is approximate
            "recommended_side": "yes" if edge > 0 else "no",
            "btc_spot": round(spot, 0),
            "threshold": threshold,
            "fair_value": round(fair_value, 4),
            "kalshi_price": round(kalshi_price, 4),
            "hours_remaining": round(hours_remaining, 2),
            "realized_vol": round(_get_btc_realized_vol(), 4),
            "edge_result": {
                "fair_value": round(fair_value, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": 0.70,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities


def scan_ipl_series() -> list[dict]:
    """
    Scan KXIPLGAME series for edges vs consensus cricket odds.

    Source priority: Bovada (primary, all 10 teams) → Odds API (gap-fill).
    Returns opportunity dicts with type='ipl_game_edge'.
    """
    markets = _fetch_series_markets(IPL_SERIES)
    if not markets:
        logger.warning("Series/IPL no open markets for %s", IPL_SERIES)
        return []

    odds_lookup = _build_odds_api_lookup("ipl")

    # Overlay Bovada cricket — covers all 10 IPL teams (Odds API misses ~4)
    from bot.odds_scraper import fetch_bovada_odds as _fetch_bovada
    bovada_result = _fetch_bovada("ipl")
    bovada_games = [g for g in bovada_result.get("games", []) if g.get("consensus")]
    if bovada_games:
        now_utc = datetime.now(timezone.utc)
        for g in bovada_games:
            for team_name, prob in g["consensus"].items():
                if prob and prob > 0:
                    key = team_name.lower()
                    odds_lookup[key] = prob
                    odds_lookup[team_name.split()[-1].lower()] = prob
                    odds_lookup[team_name.split()[0].lower()] = prob
        logger.debug("Series/IPL +bovada overlay: %d total teams (%d games)",
                     len(odds_lookup) // 2, len(bovada_games))

    logger.info("Series/IPL %d open markets | %d Odds API teams",
                len(markets), len(odds_lookup) // 2)

    now_utc = datetime.now(timezone.utc)
    opportunities = []
    for market in markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        volume = market.get("volume") or 0
        open_interest = market.get("open_interest") or 0
        if volume < 10 and open_interest < 5:
            continue

        abbrev = _extract_team_abbrev(ticker)
        if not abbrev:
            continue

        canonical = IPL_TEAM_ALIASES.get(abbrev.lower())
        if not canonical:
            logger.debug("Series/IPL SKIP %s: unknown abbrev=%r", ticker, abbrev)
            continue

        # Try exact match, last-word match, then first-word match
        # (covers "Royal Challengers Bengaluru" vs "Royal Challengers Bangalore" etc.)
        prob = (
            odds_lookup.get(canonical.lower())
            or odds_lookup.get(canonical.split()[-1].lower())
            or odds_lookup.get(canonical.split()[0].lower())
        )
        prob_source = "odds_api"
        if prob is None or prob <= 0:
            prob = _elo_fallback_prob(ticker, abbrev, canonical, IPL_TEAM_ALIASES, "ipl")
            if prob is not None:
                prob_source = "elo_fallback"
            else:
                logger.debug("Series/IPL SKIP %s: no odds match for canonical=%r (tried: %r, %r, %r)",
                             ticker, canonical, canonical.lower(),
                             canonical.split()[-1].lower(), canonical.split()[0].lower())
                continue

        hours_to_game = 0.0
        game_dt = None
        try:
            parts = ticker.split("-")
            if len(parts) >= 3:
                date_seg = parts[1]
                import re as _re
                m = _re.match(r"(\d{2})([A-Z]{3})(\d{2})", date_seg)
                if m:
                    yy, mon, dd = m.group(1), m.group(2), m.group(3)
                    month_map = {
                        "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                        "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12
                    }
                    game_dt = datetime(
                        2000 + int(yy), month_map.get(mon, 1), int(dd),
                        tzinfo=timezone.utc
                    )
                    hours_to_game = (game_dt - now_utc).total_seconds() / 3600
        except Exception:
            pass

        min_edge_required = MIN_RELATIVE_EDGE
        if hours_to_game > 48:
            min_edge_required = MIN_RELATIVE_EDGE + (hours_to_game - 48) * 0.002

        kalshi_price = yes_ask / 100.0
        edge = prob - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        check_ok, check_msg = _self_check_edge(prob, kalshi_price, edge)
        if not check_ok:
            continue

        if kalshi_price <= 0.03 or prob <= 0.03:
            continue

        if abs(relative_edge) < min_edge_required:
            continue

        if abs(relative_edge) > 1.5:
            logger.debug("Series/IPL SKIP %s: relative_edge=%.1f%% exceeds sanity cap",
                         ticker, relative_edge * 100)
            continue

        confidence = 0.75
        if hours_to_game > 72:
            confidence = 0.60
        elif hours_to_game > 48:
            confidence = 0.68

        if prob_source == "elo_fallback":
            confidence = min(confidence, 0.65)  # Elo is less sharp than bookmaker lines

        game_date_str = game_dt.strftime("%a %b") + f" {game_dt.day}" if game_dt else None

        opportunities.append({
            "type": "ipl_game_edge",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": confidence,
            "recommended_side": "yes" if edge > 0 else "no",
            "odds_prob": round(prob, 4),
            "odds_source": "odds_api",
            "kalshi_price": round(kalshi_price, 4),
            "team_abbrev": abbrev,
            "canonical_team": canonical,
            "sport": "ipl",
            "hours_to_game": round(hours_to_game, 1),
            "game_date_str": game_date_str,
            "edge_result": {
                "fair_value": round(prob, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": confidence,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities


def scan_ethereum_series() -> list[dict]:
    """
    Scan ETH series for edges vs CoinGecko spot price.

    Uses log-normal probability model (same as BTC scanner) to compute
    P(ETH_close > threshold). ETH resolves at 21:00 UTC daily.
    Returns list of opportunity dicts with type='eth_price_edge'.
    """
    markets = _fetch_series_markets(ETH_SERIES)
    if not markets:
        logger.warning("Series/ETH no open markets for %s", ETH_SERIES)
        return []

    spot = _get_eth_spot()
    if not spot:
        logger.warning("Series/ETH could not fetch ETH spot price")
        return []

    now_utc = datetime.now(timezone.utc)
    resolve_today = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
    hours_remaining = (resolve_today - now_utc).total_seconds() / 3600.0

    if hours_remaining < 0:
        logger.warning("Series/ETH all today's markets already resolved (past 21:00 UTC)")
        return []

    vol = _get_eth_realized_vol()
    logger.info("Series/ETH %d open markets | spot=$%s | vol=%.2f%% | hours_remaining=%.1fh",
                len(markets), f"{spot:,.0f}", vol * 100, hours_remaining)

    today_str = now_utc.strftime("%y%b%d").upper()

    opportunities = []
    for market in markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        if today_str not in ticker:
            continue

        # Extract threshold: ETHUSD-26APR0421-T1800 → 1800
        threshold = None
        for part in ticker.split("-"):
            if part.startswith("T") and part[1:].isdigit():
                threshold = float(part[1:])
                break
        if threshold is None:
            continue

        scaled_vol = vol * math.sqrt(hours_remaining / 24.0)
        log_ratio = math.log(spot / threshold)
        z = log_ratio / scaled_vol if scaled_vol > 0 else 0.0
        fair_value = 0.5 * math.erfc(-z / math.sqrt(2))

        kalshi_price = yes_ask / 100.0
        edge = fair_value - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        check_ok, check_msg = _self_check_edge(fair_value, kalshi_price, edge)
        if not check_ok:
            continue

        if kalshi_price <= 0.03 or fair_value <= 0.03:
            continue

        if abs(relative_edge) < MIN_RELATIVE_EDGE:
            continue

        opportunities.append({
            "type": "eth_price_edge",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": 0.68,
            "recommended_side": "yes" if edge > 0 else "no",
            "eth_spot": round(spot, 0),
            "threshold": threshold,
            "fair_value": round(fair_value, 4),
            "kalshi_price": round(kalshi_price, 4),
            "hours_remaining": round(hours_remaining, 2),
            "realized_vol": round(vol, 4),
            "edge_result": {
                "fair_value": round(fair_value, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": 0.68,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities


# ---------------------------------------------------------------------------
# Generic multi-asset, multi-timeframe crypto helpers
# ---------------------------------------------------------------------------

def _prefetch_all_crypto_spots() -> None:
    """
    Warm cache for all assets. Coinbase primary (one call per asset, no geo-block),
    CoinGecko batch fallback.
    """
    import requests
    all_ids = list(CRYPTO_ASSETS_CONFIG.keys())
    now = _time.time()
    stale = [a for a in all_ids
             if not _CRYPTO_SPOT_CACHE.get(a) or
             (now - _CRYPTO_SPOT_CACHE[a].get("ts", 0)) >= _CRYPTO_SPOT_TTL]
    if not stale:
        return

    # --- Primary: Coinbase (one call per asset, fast, no geo-block) ---
    coinbase_failed = []
    for asset_id in stale:
        pair = _COINBASE_PAIRS.get(asset_id)
        if not pair:
            coinbase_failed.append(asset_id)
            continue
        try:
            url = f"https://api.coinbase.com/v2/prices/{pair}/spot"
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            price = float(r.json()["data"]["amount"])
            _CRYPTO_SPOT_CACHE[asset_id] = {"price": price, "ts": _time.time(), "src": "coinbase"}
            logger.debug("CRYPTO prefetch spot %s = $%s (coinbase)", asset_id, price)
        except Exception:
            coinbase_failed.append(asset_id)

    # --- Fallback: CoinGecko batch for anything Coinbase missed ---
    if coinbase_failed:
        try:
            ids_param = ",".join(coinbase_failed)
            url = f"{COINGECKO_BASE}/simple/price?ids={ids_param}&vs_currencies=usd"
            r = requests.get(url, headers={"User-Agent": "NexusBot/1.0"}, timeout=10)
            r.raise_for_status()
            data = r.json()
            ts = _time.time()
            for asset_id in coinbase_failed:
                price = data.get(asset_id, {}).get("usd")
                if price:
                    _CRYPTO_SPOT_CACHE[asset_id] = {"price": float(price), "ts": ts, "src": "coingecko"}
                    logger.debug("CRYPTO prefetch spot %s = $%s (coingecko fallback)", asset_id, price)
        except Exception as e:
            logger.warning("CRYPTO prefetch spot failed: %s — will use cached or fallback", e)


def _prefetch_all_crypto_vols() -> None:
    """
    Fetch vol for all 5 assets SEQUENTIALLY before the ThreadPoolExecutor.
    Avoids 5 parallel vol requests that trigger CoinGecko 429s.
    Short sleep between requests stays within the free tier rate limit.
    """
    import requests
    now = _time.time()
    for asset_id, (fallback_vol, cap_low, cap_high, _, _) in CRYPTO_ASSETS_CONFIG.items():
        cached = _CRYPTO_VOL_CACHE.get(asset_id, {})
        if cached and (now - cached.get("ts", 0)) < _CRYPTO_VOL_TTL:
            continue  # already fresh
        try:
            url = (f"{COINGECKO_BASE}/coins/{asset_id}/market_chart"
                   "?vs_currency=usd&days=10&interval=daily")
            r = requests.get(url, headers={"User-Agent": "NexusBot/1.0"}, timeout=10)
            r.raise_for_status()
            prices = [p[1] for p in r.json().get("prices", [])]
            returns = [math.log(prices[i] / prices[i - 1])
                       for i in range(1, len(prices)) if prices[i - 1] > 0]
            vol = (math.sqrt(sum(x ** 2 for x in returns) / len(returns))
                   if returns else fallback_vol)
            vol = max(cap_low, min(cap_high, vol))
            _CRYPTO_VOL_CACHE[asset_id] = {"vol": vol, "ts": _time.time()}
            logger.debug("CRYPTO prefetch vol %s = %.2f%%", asset_id, vol * 100)
        except Exception as e:
            logger.warning("CRYPTO prefetch vol failed for %s: %s", asset_id, e)
        _time.sleep(1.2)   # 1.2s between vol calls — 5 calls / 6s stays under free tier


def _get_generic_crypto_spot(asset_id: str) -> float | None:
    """Fetch real-time spot price. Coinbase primary, CoinGecko fallback."""
    cached = _CRYPTO_SPOT_CACHE.get(asset_id, {})
    if cached and (_time.time() - cached.get("ts", 0)) < _CRYPTO_SPOT_TTL:
        return cached["price"]

    import requests

    # --- Primary: Coinbase REST (free, no auth, no geo-block) ---
    cb_pair = _COINBASE_PAIRS.get(asset_id)
    if cb_pair:
        try:
            url = f"https://api.coinbase.com/v2/prices/{cb_pair}/spot"
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            price = float(r.json()["data"]["amount"])
            _CRYPTO_SPOT_CACHE[asset_id] = {"price": price, "ts": _time.time(), "src": "coinbase"}
            logger.debug("CRYPTO spot %s = $%s (coinbase)", asset_id, price)
            return price
        except Exception as e:
            logger.debug("CRYPTO coinbase spot failed for %s: %s, trying coingecko", asset_id, e)

    # --- Fallback: CoinGecko ---
    try:
        url = f"{COINGECKO_BASE}/simple/price?ids={asset_id}&vs_currencies=usd"
        r = requests.get(url, headers={"User-Agent": "NexusBot/1.0"}, timeout=10)
        r.raise_for_status()
        price = float(r.json()[asset_id]["usd"])
        _CRYPTO_SPOT_CACHE[asset_id] = {"price": price, "ts": _time.time(), "src": "coingecko"}
        logger.debug("CRYPTO spot %s = $%s (coingecko fallback)", asset_id, price)
        return price
    except Exception as e:
        logger.warning("CRYPTO spot fetch failed for %s: %s", asset_id, e)
        return _CRYPTO_SPOT_CACHE.get(asset_id, {}).get("price")


def _get_generic_crypto_vol(asset_id: str, fallback: float,
                             cap_low: float, cap_high: float) -> float:
    """Fetch 10-day realized vol from CoinGecko for any asset, with 30-min cache."""
    cached = _CRYPTO_VOL_CACHE.get(asset_id, {})
    if cached and (_time.time() - cached.get("ts", 0)) < _CRYPTO_VOL_TTL:
        return cached["vol"]
    try:
        import requests
        url = (f"{COINGECKO_BASE}/coins/{asset_id}/market_chart"
               "?vs_currency=usd&days=10&interval=daily")
        r = requests.get(url, headers={"User-Agent": "NexusBot/1.0"}, timeout=10)
        r.raise_for_status()
        prices = [p[1] for p in r.json().get("prices", [])]
        returns = [math.log(prices[i] / prices[i - 1])
                   for i in range(1, len(prices)) if prices[i - 1] > 0]
        vol = (math.sqrt(sum(x ** 2 for x in returns) / len(returns))
               if returns else fallback)
        vol = max(cap_low, min(cap_high, vol))
        _CRYPTO_VOL_CACHE[asset_id] = {"vol": vol, "ts": _time.time()}
        logger.debug("CRYPTO vol %s = %.2f%%", asset_id, vol * 100)
        return vol
    except Exception as e:
        logger.warning("CRYPTO vol fetch failed for %s: %s", asset_id, e)
        return _CRYPTO_VOL_CACHE.get(asset_id, {}).get("vol", fallback)


def _extract_crypto_threshold(ticker: str) -> float | None:
    """Extract price threshold from ticker suffix. e.g. KXBTCD-25APR05-T67000 → 67000.0"""
    for part in ticker.split("-"):
        if part.startswith("T") and part[1:].replace(".", "").isdigit():
            return float(part[1:])
    return None


def _scan_crypto_series(asset_id: str, timeframe: str) -> list[dict]:
    """
    Scan a single (asset, timeframe) pair against Kalshi markets.

    Uses the market's close_time field to compute hours_remaining —
    works for 15-min, hourly, and daily markets without hardcoded resolve times.
    """
    series = CRYPTO_SERIES_MAP.get((asset_id, timeframe))
    if not series:
        return []

    markets = _fetch_series_markets(series)
    if not markets:
        logger.debug("CRYPTO %s/%s: no open markets for %s", asset_id, timeframe, series)
        return []

    fallback_vol, cap_low, cap_high, confidence, opp_type = CRYPTO_ASSETS_CONFIG[asset_id]
    spot = _get_generic_crypto_spot(asset_id)
    if not spot:
        return []

    vol = _get_generic_crypto_vol(asset_id, fallback_vol, cap_low, cap_high)
    now_utc = datetime.now(timezone.utc)
    min_hours = _TIMEFRAME_MIN_HOURS.get(timeframe, 0.10)

    opportunities = []
    for market in markets:
        ticker = market.get("ticker", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        # Derive hours_remaining from close_time (works for any timeframe)
        close_time = market.get("close_time") or market.get("expiration_time")
        if not close_time:
            continue
        try:
            close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        except Exception:
            continue
        hours_remaining = (close_dt - now_utc).total_seconds() / 3600.0
        if hours_remaining < min_hours or hours_remaining > 200:
            continue

        threshold = _extract_crypto_threshold(ticker)
        if threshold is None:
            continue

        scaled_vol = vol * math.sqrt(hours_remaining / 24.0)
        if scaled_vol <= 0:
            continue
        log_ratio = math.log(spot / threshold)
        z = log_ratio / scaled_vol
        fair_value = 0.5 * math.erfc(-z / math.sqrt(2))
        kalshi_price = yes_ask / 100.0
        edge = fair_value - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        check_ok, _ = _self_check_edge(fair_value, kalshi_price, edge)
        if not check_ok:
            continue
        if kalshi_price <= 0.03 or fair_value <= 0.03:
            continue
        # OTM filter: contracts < 30¢ are too far from spot — model edge evaporates
        if kalshi_price < 0.30:
            logger.debug(
                "CRYPTO %s/%s SKIP %s: price %d¢ < 30¢ floor (too far OTM)",
                asset_id, timeframe, ticker, int(kalshi_price * 100),
            )
            continue
        if abs(relative_edge) < MIN_RELATIVE_EDGE:
            continue
        # Absolute edge cap: >25% means stale pricing, near-expiry, or broken liquidity
        if abs(edge) > 0.25:
            logger.warning(
                "CRYPTO %s/%s SKIP %s: edge %.1f%% exceeds 25%% cap — stale/near-expiry",
                asset_id, timeframe, ticker, edge * 100,
            )
            continue
        # Edge floor: crypto volatility eats thin edges before execution
        from bot.config import CRYPTO_MIN_EDGE
        if abs(edge) < CRYPTO_MIN_EDGE:
            logger.debug(
                "CRYPTO %s/%s SKIP %s: edge %.1f%% below %.0f%% floor",
                asset_id, timeframe, ticker, abs(edge) * 100, CRYPTO_MIN_EDGE * 100,
            )
            continue

        opportunities.append({
            "type": opp_type,
            "timeframe": timeframe,
            "ticker": ticker,
            "title": market.get("title", ""),
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": confidence,
            "recommended_side": "yes" if edge > 0 else "no",
            "spot": round(spot, 4),
            "threshold": threshold,
            "fair_value": round(fair_value, 4),
            "kalshi_price": round(kalshi_price, 4),
            "hours_remaining": round(hours_remaining, 2),
            "realized_vol": round(vol, 4),
            "edge_result": {
                "fair_value": round(fair_value, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "self_check_passed": True,   # already verified by _self_check_edge() above
            },
        })

    return opportunities


def scan_all_crypto_markets() -> list[dict]:
    """
    Scan all crypto assets across all active timeframes in parallel.

    Called exclusively by the standalone _crypto_scan_loop() in main.py every 5 minutes.
    """
    # Warm spot + vol caches before thread pool fires — prevents 15 parallel
    # CoinGecko calls. Spot is one batched request; vol is sequential (no batch endpoint).
    _prefetch_all_crypto_spots()
    _prefetch_all_crypto_vols()

    tasks = [
        (asset_id, timeframe)
        for asset_id in CRYPTO_ASSETS_CONFIG
        for timeframe in ACTIVE_CRYPTO_TIMEFRAMES
        if (asset_id, timeframe) in CRYPTO_SERIES_MAP
    ]
    all_opps: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_scan_crypto_series, a, t): (a, t) for a, t in tasks}
        for future in as_completed(futures):
            asset_id, timeframe = futures[future]
            try:
                opps = future.result()
                if opps:
                    logger.info("CRYPTO %s/%s: %d opportunities",
                                asset_id.upper(), timeframe, len(opps))
                all_opps.extend(opps)
            except Exception as e:
                logger.warning("CRYPTO %s/%s error: %s", asset_id, timeframe, e)
    return all_opps


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def scan_series_markets(odds_by_sport: dict | None = None) -> list[dict]:
    """
    Run all series scanners and return combined opportunities.

    Sports are scanned in parallel (4 workers) with a per-worker 1s delay to
    avoid Kalshi 429 rate limits. Wall time drops from ~20-30s to ~2-3s.

    Args:
        odds_by_sport: Unused — Bovada is fetched directly inside scan_sports_series.
                       Kept for interface compatibility with scan_cycle().
    """
    all_opps: list[dict] = []

    def _scan_one_sport(sport: str) -> list[dict]:
        _time.sleep(1.0)  # per-worker rate-limit gap
        return scan_sports_series(sport)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_scan_one_sport, sport): sport for sport in SPORTS_SERIES}
        for future in as_completed(futures):
            sport = futures[future]
            try:
                opps = future.result()
                logger.info("Series/%s found %d opportunities", sport.upper(), len(opps))
                all_opps.extend(opps)
            except Exception as e:
                logger.warning("Series/%s error: %s", sport.upper(), e)

    _time.sleep(1.0)
    ipl_opps = scan_ipl_series()
    logger.info("Series/IPL found %d opportunities", len(ipl_opps))
    all_opps.extend(ipl_opps)

    # Crypto is handled by the independent _crypto_scan_loop in main.py — not here

    return all_opps
