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
from datetime import datetime, timezone

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
from bot.odds_scraper import _odds_api_fallback
from bot.injuries import check_back_to_back as _check_b2b, get_last_10 as _get_l10

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

# TTL cache for Odds API lookups — avoids burning the 500/month quota
# One call per sport per 30 min maximum regardless of how often the scanner runs
_ODDS_API_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_ODDS_API_CACHE_TTL = 1800  # seconds

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
                print(f"  [SeriesHTTP] Error fetching {url[:80]}: {e}")
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
            print(f"  [Series] Error fetching {series_ticker}: {result['error']}")
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


def _build_odds_api_lookup(sport: str) -> dict[str, float]:
    """
    Build lowercase-team-name → vig-removed-prob dict from The Odds API.

    Cached per-sport for 30 minutes (success) or 5 minutes (failure/empty) to
    stay within the 500 req/month free tier while retrying transient errors quickly.

    Always called alongside Bovada so we cover games Bovada doesn't list yet
    (typically games 48+ hours out).
    """
    cached = _ODDS_API_CACHE.get(sport)
    if cached:
        age = _time.monotonic() - cached[0]
        ttl = _ODDS_API_FAIL_TTL if cached[1] == {} else _ODDS_API_CACHE_TTL
        if age < ttl:
            # Restore game map from its own cache so Elo cross-check works on cache hits
            gm_cached = _ODDS_API_GAME_MAP_CACHE.get(sport)
            if gm_cached:
                _ODDS_API_GAME_MAP[sport] = gm_cached[1]
            return cached[1]

    now_utc = datetime.now(timezone.utc)
    print(f"  [OddsAPI/{sport.upper()}] Fetching from The Odds API...")
    try:
        result = _odds_api_fallback(sport)
        if "error" in result:
            print(f"  [OddsAPI/{sport.upper()}] ERROR: {result['error']}")
            _ODDS_API_CACHE[sport] = (_time.monotonic(), {})
            return {}

        raw_games = result.get("games", [])
        print(f"  [OddsAPI/{sport.upper()}] Got {len(raw_games)} games total")

        lookup: dict[str, float] = {}
        skipped_started = 0
        skipped_no_consensus = 0

        for game in raw_games:
            home = game.get("home_team", "?")
            away = game.get("away_team", "?")
            commence_raw = game.get("commence_time", "")

            # Pregame only — skip games that have already started
            if commence_raw:
                try:
                    commence_dt = datetime.fromisoformat(
                        commence_raw.replace("Z", "+00:00")
                    )
                    if commence_dt <= now_utc:
                        skipped_started += 1
                        continue
                except (ValueError, TypeError):
                    pass  # Unknown format — include the game

            consensus = game.get("consensus", {})
            if not consensus:
                skipped_no_consensus += 1
                print(f"  [OddsAPI/{sport.upper()}]   {away} @ {home}: NO consensus data")
                continue

            if sport not in _ODDS_API_GAME_MAP:
                _ODDS_API_GAME_MAP[sport] = {}
            for team_name, prob in consensus.items():
                if prob and prob > 0:
                    lookup[team_name.lower()] = prob
                    lookup[team_name.split()[-1].lower()] = prob
                    is_home = team_name == home
                    _ODDS_API_GAME_MAP[sport][team_name.lower()] = {
                        "home_team": home,
                        "away_team": away,
                        "home_away": "home" if is_home else "away",
                    }

        print(
            f"  [OddsAPI/{sport.upper()}] Result: {len(lookup)//2} teams added | "
            f"skipped {skipped_started} started, {skipped_no_consensus} no-consensus"
        )
        _ODDS_API_CACHE[sport] = (_time.monotonic(), lookup)
        if sport in _ODDS_API_GAME_MAP:
            _ODDS_API_GAME_MAP_CACHE[sport] = (_time.monotonic(), _ODDS_API_GAME_MAP[sport])
        return lookup

    except Exception as e:
        print(f"  [OddsAPI/{sport.upper()}] Exception: {e}")
        _ODDS_API_CACHE[sport] = (_time.monotonic(), {})
        return {}


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
        print(f"  [Series/{sport.upper()}] No open markets for {series_ticker}")
        return []

    game_lines = _fetch_bovada_game_lines(sport)
    bovada_lookup = _build_bovada_lookup(game_lines)

    # Always fetch Odds API as a supplement — covers games Bovada doesn't have yet
    # (e.g. games 48+ hours out). Cached 30 min so it doesn't burn the rate limit.
    odds_api_lookup = _build_odds_api_lookup(sport)

    print(
        f"  [Series/{sport.upper()}] {len(markets)} open markets | "
        f"{len(game_lines)} Bovada games | {len(odds_api_lookup)//2} Odds API teams"
    )

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
            prob = (
                bovada_lookup.get(canonical.lower())
                or bovada_lookup.get(canonical.split()[-1].lower())
            )
            if prob:
                prob_source = "bovada"
            else:
                prob = (
                    odds_api_lookup.get(canonical.lower())
                    or odds_api_lookup.get(canonical.split()[-1].lower())
                )
                if prob:
                    prob_source = "odds_api"
        else:
            # Try direct lookup by abbrev as last-word match
            prob = bovada_lookup.get(abbrev) or odds_api_lookup.get(abbrev)
            if prob:
                prob_source = "bovada" if abbrev in bovada_lookup else "odds_api"

        if prob is None or prob <= 0:
            print(
                f"    [Series/{sport.upper()}] SKIP {ticker}: "
                f"no match for abbrev={abbrev!r} canonical={canonical!r} source=none"
            )
            continue

        # Fix #5: Time-to-game edge requirement — distant games need a larger edge
        # because the odds are less reliable (more uncertainty, less sharp money).
        # Parse game date from ticker segment like "26APR05" (year=26, month=APR, day=05).
        hours_to_game = 0.0
        game_dt = None
        try:
            parts = ticker.split("-")
            if len(parts) >= 3:
                date_seg = parts[1]  # e.g. "26APR05HOUGSW"
                # Extract the leading date portion (YYMONDD pattern)
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

        # Back-to-back check for the team we're betting on
        team_b2b = False
        if canonical and game_dt:
            try:
                team_b2b = _check_b2b(canonical, sport, game_dt)
                if team_b2b:
                    print(f"    [Series/{sport.upper()}] B2B: {canonical}")
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
            print(
                f"    [Series/{sport.upper()}] SKIP {ticker}: "
                f"relative_edge={relative_edge:.1%} exceeds sanity cap (likely live game)"
            )
            continue

        # Confidence scales down for distant games (less reliable odds)
        confidence = 0.80
        if hours_to_game > 72:
            confidence = 0.65
        elif hours_to_game > 48:
            confidence = 0.72

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
                            print(f"    [Series/{sport.upper()}] B2B (opp): {opp_name}")
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
        except Exception:
            pass

        # Derive opponent from game map (populated by _build_odds_api_lookup)
        opponent_team = ""
        game_info_for_opp = _ODDS_API_GAME_MAP.get(sport, {}).get(
            canonical.lower() if canonical else ""
        )
        if game_info_for_opp:
            if game_info_for_opp["home_away"] == "home":
                opponent_team = game_info_for_opp["away_team"]
            else:
                opponent_team = game_info_for_opp["home_team"]

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

    print(f"  [Series/BTC] Realized 10d vol: {daily_vol:.2%} (from {len(prices)} daily closes)")
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
    print(f"  [Series/ETH] Realized 10d vol: {daily_vol:.2%}")
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
        print(f"  [Series/BTC] No open markets for {BTC_SERIES}")
        return []

    spot = _get_btc_spot()
    if not spot:
        print(f"  [Series/BTC] Could not fetch BTC spot price")
        return []

    now_utc = datetime.now(timezone.utc)
    # Kalshi BTC markets resolve at 21:00 UTC daily (5PM EDT)
    resolve_today = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
    hours_remaining = (resolve_today - now_utc).total_seconds() / 3600.0

    if hours_remaining < 0:
        print(f"  [Series/BTC] All today's markets already resolved (past 21:00 UTC)")
        return []

    print(
        f"  [Series/BTC] {len(markets)} open markets | "
        f"spot=${spot:,.0f} | hours_remaining={hours_remaining:.1f}h"
    )

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
    Scan KXIPLGAME series for edges vs The Odds API consensus.

    IPL cricket — Bovada doesn't carry cricket so Odds API is the sole source.
    Same edge/filter logic as scan_sports_series().
    Returns opportunity dicts with type='ipl_game_edge'.
    """
    markets = _fetch_series_markets(IPL_SERIES)
    if not markets:
        print(f"  [Series/IPL] No open markets for {IPL_SERIES}")
        return []

    odds_lookup = _build_odds_api_lookup("ipl")
    print(
        f"  [Series/IPL] {len(markets)} open markets | "
        f"{len(odds_lookup)//2} Odds API teams"
    )

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
            print(f"    [Series/IPL] SKIP {ticker}: unknown abbrev={abbrev!r}")
            continue

        prob = (
            odds_lookup.get(canonical.lower())
            or odds_lookup.get(canonical.split()[-1].lower())
        )
        if prob is None or prob <= 0:
            print(
                f"    [Series/IPL] SKIP {ticker}: "
                f"no odds match for canonical={canonical!r}"
            )
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
            print(
                f"    [Series/IPL] SKIP {ticker}: "
                f"relative_edge={relative_edge:.1%} exceeds sanity cap"
            )
            continue

        confidence = 0.75
        if hours_to_game > 72:
            confidence = 0.60
        elif hours_to_game > 48:
            confidence = 0.68

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
        print(f"  [Series/ETH] No open markets for {ETH_SERIES}")
        return []

    spot = _get_eth_spot()
    if not spot:
        print(f"  [Series/ETH] Could not fetch ETH spot price")
        return []

    now_utc = datetime.now(timezone.utc)
    resolve_today = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
    hours_remaining = (resolve_today - now_utc).total_seconds() / 3600.0

    if hours_remaining < 0:
        print(f"  [Series/ETH] All today's markets already resolved (past 21:00 UTC)")
        return []

    vol = _get_eth_realized_vol()
    print(
        f"  [Series/ETH] {len(markets)} open markets | "
        f"spot=${spot:,.0f} | vol={vol:.2%} | hours_remaining={hours_remaining:.1f}h"
    )

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
# Orchestrator
# ---------------------------------------------------------------------------

def scan_series_markets(odds_by_sport: dict | None = None) -> list[dict]:
    """
    Run all series scanners and return combined opportunities.

    Sports are scanned in parallel using a thread pool — each sport makes
    independent HTTP calls (Bovada + Odds API + Kalshi), so parallelism
    reduces wall-clock time from ~60 s → ~15 s on 4 sports.

    Args:
        odds_by_sport: Unused — Bovada is fetched directly inside scan_sports_series.
                       Kept for interface compatibility with scan_cycle().
    """
    all_opps: list[dict] = []

    with ThreadPoolExecutor(max_workers=len(SPORTS_SERIES)) as executor:
        future_to_sport = {
            executor.submit(scan_sports_series, sport): sport
            for sport in SPORTS_SERIES
        }
        for future in as_completed(future_to_sport):
            sport = future_to_sport[future]
            try:
                opps = future.result()
                print(f"  [Series/{sport.upper()}] Found {len(opps)} opportunities")
                all_opps.extend(opps)
            except Exception as e:
                print(f"  [Series/{sport.upper()}] Error: {e}")

    btc_opps = scan_bitcoin_series()
    print(f"  [Series/BTC] Found {len(btc_opps)} opportunities")
    all_opps.extend(btc_opps)

    ipl_opps = scan_ipl_series()
    print(f"  [Series/IPL] Found {len(ipl_opps)} opportunities")
    all_opps.extend(ipl_opps)

    eth_opps = scan_ethereum_series()
    print(f"  [Series/ETH] Found {len(eth_opps)} opportunities")
    all_opps.extend(eth_opps)

    return all_opps
