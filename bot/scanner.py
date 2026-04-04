"""
Nexus Trading Bot — Continuous Market Scanner

Polls sportsbook odds + Kalshi markets, detects edges.
Smart scheduling: scan fast during live games, slow when idle.
Tracks Odds API usage to stay under monthly limit.
"""

from __future__ import annotations

import json
import sys
import time
import re
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.parlay import parse_parlay_title, price_parlay, NBA_TEAM_ALIASES, MLB_TEAM_ALIASES, NHL_TEAM_ALIASES, NCAAB_TEAM_ALIASES
from agent.player_stats import estimate_player_prop_probability
from agent.kalshi_client import get_markets, get_market
from bot.odds_scraper import fetch_consensus_odds

from bot.config import (
    ACTIVE_SPORTS, LINE_MOVEMENT_THRESHOLD,
    MIN_RELATIVE_EDGE, SCAN_INTERVAL_IDLE, SCAN_INTERVAL_PREGAME,
    SCAN_INTERVAL_LIVE, NWS_CITIES, NWS_BIAS_CORRECTION, WEATHER_STD_DEV,
    ODDS_SNAPSHOTS_FILE, BOT_STATE_FILE, WEATHER_SERIES_TICKERS,
    ACTIVE_STRATEGIES, WEATHER_MIN_HOURS_TO_CLOSE,
)
from bot.math_engine import calculate_parlay_edge, calculate_weather_edge, calculate_vig_stack, _self_check_edge
from bot.kalshi_series import scan_series_markets, _ODDS_API_GAME_MAP as _SERIES_GAME_MAP
import bot.kalshi_series as _kalshi_series_mod


# ---------------------------------------------------------------------------
# Fix #7: Dynamic confidence based on historical win rate
# ---------------------------------------------------------------------------

def _get_dynamic_confidence(opp_type: str, default: float = 0.80) -> float:
    """
    Return a confidence score that blends a hardcoded prior with the bot's
    actual historical win rate for this opportunity type.

    Requires at least 20 resolved trades of this type before deviating from
    the default. Fully weighted after 100 trades.

    Why: a strategy winning 55% when the model says 50% deserves more sizing;
    one winning 30% deserves less — and should auto-suspend at deep negatives.
    """
    try:
        from bot.tracker import get_roi_by_strategy
        stats = get_roi_by_strategy().get(opp_type, {})
        n = stats.get("total", 0)
        if n < 20:
            return default
        win_rate = stats.get("winrate", default)
        weight = min(1.0, (n - 20) / 80)  # 0→1 as n goes from 20→100
        blended = default * (1 - weight) + win_rate * weight
        return round(max(0.1, min(0.99, blended)), 3)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# State I/O helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict | list:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Live game detection
# ---------------------------------------------------------------------------

def is_game_live(commence_time_iso: str) -> bool:
    """
    Check if a game is currently live based on commence time.
    Games are typically live from commence_time to ~3 hours after.
    """
    try:
        commence = datetime.fromisoformat(commence_time_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        # Game is live if it started and hasn't been going more than ~3.5 hours
        return timedelta(0) <= (now - commence) <= timedelta(hours=3, minutes=30)
    except (ValueError, TypeError):
        return False


def is_game_starting_soon(commence_time_iso: str, hours: float = 1.0) -> bool:
    """Check if a game starts within the specified hours."""
    try:
        commence = datetime.fromisoformat(commence_time_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return timedelta(0) <= (commence - now) <= timedelta(hours=hours)
    except (ValueError, TypeError):
        return False


def get_scan_interval(games: list[dict]) -> int:
    """
    Determine scan interval based on game schedule.

    Returns seconds to wait before next scan.
    """
    any_live = False
    any_soon = False

    for game in games:
        ct = game.get("commence_time", "")
        if is_game_live(ct):
            any_live = True
            break
        if is_game_starting_soon(ct):
            any_soon = True

    if any_live:
        return SCAN_INTERVAL_LIVE
    elif any_soon:
        return SCAN_INTERVAL_PREGAME
    else:
        return SCAN_INTERVAL_IDLE


# ---------------------------------------------------------------------------
# Odds snapshot management (line movement detection)
# ---------------------------------------------------------------------------

def _load_previous_snapshot() -> dict:
    return _load_json(ODDS_SNAPSHOTS_FILE)


def _save_snapshot(snapshot: dict):
    # Keep last 2 snapshots for line movement comparison
    prev = _load_previous_snapshot()
    combined = {
        "previous": prev.get("current", {}),
        "current": snapshot,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json(ODDS_SNAPSHOTS_FILE, combined)


def _detect_line_movements(current_odds: dict, previous_odds: dict) -> list[dict]:
    """
    Compare current odds snapshot to previous and find significant movements.
    Returns list of games where consensus shifted by more than threshold.
    """
    movements = []
    if not previous_odds:
        return movements

    prev_games = {g.get("id"): g for g in previous_odds.get("games", [])}

    for game in current_odds.get("games", []):
        game_id = game.get("id")
        if game_id not in prev_games:
            continue

        prev_game = prev_games[game_id]
        prev_consensus = prev_game.get("consensus", {})
        curr_consensus = game.get("consensus", {})

        for team, curr_prob in curr_consensus.items():
            prev_prob = prev_consensus.get(team, curr_prob)
            movement = abs(curr_prob - prev_prob)
            if movement >= LINE_MOVEMENT_THRESHOLD:
                movements.append({
                    "game_id": game_id,
                    "team": team,
                    "home_team": game.get("home_team"),
                    "away_team": game.get("away_team"),
                    "previous_prob": round(prev_prob, 4),
                    "current_prob": round(curr_prob, 4),
                    "movement": round(movement, 4),
                    "direction": "up" if curr_prob > prev_prob else "down",
                    "commence_time": game.get("commence_time"),
                })

    return movements


# ---------------------------------------------------------------------------
# Opportunity ranking helpers
# ---------------------------------------------------------------------------

def _sort_by_ev(opportunities: list[dict]) -> list[dict]:
    """Sort opportunities descending by |edge| × confidence (expected value)."""
    return sorted(
        opportunities,
        key=lambda o: abs(o.get("edge", 0)) * o.get("confidence", 0.5),
        reverse=True,
    )


def _apply_b2b_penalty(opp: dict) -> dict:
    """Reduce confidence by 0.10 for back-to-back game situations."""
    if not opp.get("b2b"):
        return opp
    opp = opp.copy()
    opp["confidence"] = max(0.0, opp.get("confidence", 0.5) - 0.10)
    opp.setdefault("warnings", []).append("b2b: confidence reduced 0.10 (back-to-back)")
    return opp


def _apply_home_away_modifier(opp: dict) -> dict:
    """
    Adjust confidence based on whether the opportunity's team is home or away.

    Logic:
      - Home team: confidence += 0.03
      - Away team: confidence -= 0.03
      - Away team AND B2B game: additional confidence -= 0.05

    Only applies to sports opportunity types (e.g. series_game_edge).
    Looks up the team in _ODDS_API_GAME_MAP via opp["sport"] and opp["team"].
    Fails open — if team not found in the game map, returns opp unchanged.
    """
    SPORTS_OPP_TYPES = {"series_game_edge", "pregame_single_game", "live_latency_arb"}
    if opp.get("type") not in SPORTS_OPP_TYPES:
        return opp

    sport = opp.get("sport", "")
    team = (opp.get("team") or opp.get("canonical_team") or "").lower()
    if not sport or not team:
        return opp

    # Always read from the live module-level dict so test injections take effect
    game_map = _kalshi_series_mod._ODDS_API_GAME_MAP
    sport_map = game_map.get(sport, {})
    game_data = sport_map.get(team)
    if not game_data:
        return opp

    opp = opp.copy()
    home_team = (game_data.get("home_team") or "").lower()
    is_home = (team == home_team)

    if is_home:
        opp["confidence"] = opp.get("confidence", 0.5) + 0.03
        opp.setdefault("warnings", []).append("home_away: home team +0.03 confidence")
    else:
        opp["confidence"] = opp.get("confidence", 0.5) - 0.03
        opp.setdefault("warnings", []).append("home_away: away team -0.03 confidence")
        if game_data.get("is_b2b"):
            opp["confidence"] = opp["confidence"] - 0.05
            opp.setdefault("warnings", []).append("home_away: away B2B -0.05 confidence")

    opp["confidence"] = round(max(0.0, opp["confidence"]), 4)
    return opp


def _cap_correlated_vig_stack(vig_stack_opps: list[dict]) -> list[dict]:
    """
    Cap total contracts for correlated vig stack opportunities that share the
    same base series prefix (everything before the first '-' in the ticker).

    For a group of N signals on the same series:
      per_signal_contracts = max(1, single_signal_contracts // N)

    Single-signal groups are left unchanged.
    Returns the capped list (modifies copies of each affected opp dict).
    """
    if not vig_stack_opps:
        return vig_stack_opps

    # Group by series prefix (everything before the first '-')
    from collections import defaultdict
    groups: dict[str, list[int]] = defaultdict(list)
    for idx, opp in enumerate(vig_stack_opps):
        ticker = opp.get("ticker", "")
        series = ticker.split("-")[0]
        groups[series].append(idx)

    result = [opp.copy() for opp in vig_stack_opps]

    for series, indices in groups.items():
        if len(indices) <= 1:
            continue  # single signal — leave unchanged

        group_size = len(indices)
        # Use the first opp's contract count as the single-signal reference
        first_opp = result[indices[0]]
        single_signal_contracts = (
            first_opp.get("recommended_contracts")
            or first_opp.get("contracts")
            or 1
        )
        per_signal = max(1, single_signal_contracts // group_size)

        for idx in indices:
            result[idx]["recommended_contracts"] = per_signal
            if "contracts" in result[idx]:
                result[idx]["contracts"] = per_signal

    return result


# ---------------------------------------------------------------------------
# NWS Weather scanning
# ---------------------------------------------------------------------------

def _get_forecast_temp_for_date(forecast: dict, target_date) -> Optional[float]:
    """
    Return the NWS daytime high for a specific calendar date.

    Iterates the forecast periods (stored with 'start' in local time with
    UTC offset) and returns the temperature for the first non-night period
    whose start date matches target_date.  Returns None if no match found.
    """
    for period in forecast.get("periods", []):
        if "night" in period.get("name", "").lower():
            continue
        try:
            start_dt = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
            if start_dt.date() == target_date:
                return float(period["temperature"])
        except Exception:
            continue
    return None


def _fetch_nws_forecast(city: str, lat: float, lon: float) -> Optional[dict]:
    """Fetch NWS forecast for a city."""
    try:
        points_url = f"https://api.weather.gov/points/{lat},{lon}"
        resp = requests.get(points_url, headers={"User-Agent": "NexusBot/1.0"}, timeout=10)
        resp.raise_for_status()
        forecast_url = resp.json()["properties"]["forecast"]

        f_resp = requests.get(forecast_url, headers={"User-Agent": "NexusBot/1.0"}, timeout=10)
        f_resp.raise_for_status()
        periods = f_resp.json()["properties"]["periods"]

        return {
            "city": city,
            "periods": [
                {
                    "name": p["name"],
                    "temperature": p["temperature"],
                    "unit": p["temperatureUnit"],
                    "start": p["startTime"],
                }
                for p in periods[:4]
            ],
        }
    except Exception as e:
        print(f"  [NWS] {city} forecast error: {e}")
        return None


def scan_weather_markets() -> list[dict]:
    """
    Scan Kalshi weather markets and compare to NWS forecasts.
    Returns list of weather opportunities with edge calculations.
    """
    opportunities = []

    # Fetch Kalshi weather markets by known series tickers (keyword search returns nothing)
    weather_markets_by_ticker: dict[str, dict] = {}
    series_found = []
    for series in WEATHER_SERIES_TICKERS:
        result = get_markets(series_ticker=series, status="open", limit=50)
        if "error" not in result:
            batch = result.get("markets", [])
            if batch:
                series_found.append(f"{series}({len(batch)})")
            for m in batch:
                weather_markets_by_ticker.setdefault(m["ticker"], m)

    weather_markets = list(weather_markets_by_ticker.values())
    print(f"  [Weather] Series fetched: {', '.join(series_found) if series_found else 'none'} — {len(weather_markets)} total markets")
    if not weather_markets:
        print("  [Weather] No markets found — Kalshi may have no open weather contracts today")
        return opportunities

    # Fetch NWS forecasts for all cities
    forecasts = {}
    for city, (lat, lon) in NWS_CITIES.items():
        fc = _fetch_nws_forecast(city, lat, lon)
        if fc:
            forecasts[city] = fc
            periods_summary = [(p["name"], p["temperature"]) for p in fc["periods"]]
            print(f"  [Weather/NWS] {city}: {periods_summary}")
        else:
            print(f"  [Weather/NWS] {city}: forecast fetch FAILED")

    print(f"  [Weather] {len(weather_markets)} Kalshi markets, {len(forecasts)} NWS cities loaded")

    # Match markets to forecasts and calculate edges
    for market in weather_markets:
        title_raw = market.get("title", "")
        title = title_raw.lower()
        ticker = market.get("ticker", "")
        yes_ask = market.get("yes_ask")
        no_ask = market.get("no_ask")

        # Verbose per-market log — fires before any filter so every market is visible
        print(
            f"  [Weather/DETAIL] {ticker!r} | yes_ask={yes_ask} no_ask={no_ask} | "
            f"title={title_raw!r}"
        )

        if not yes_ask or yes_ask <= 0:
            print(f"  [Weather] SKIP {ticker}: no yes_ask price (yes_ask={yes_ask})")
            continue

        # Micro-price filter: skip near-certain outcomes (1-2¢ or 98-99¢).
        # At these extremes NWS model uncertainty exceeds the absolute edge — and
        # genuine 1¢ markets have too little liquidity for meaningful execution.
        if yes_ask <= 2 or yes_ask >= 98:
            print(f"  [Weather] SKIP {ticker}: extreme price {yes_ask}¢ — near-certain market, model uncertainty dominates")
            continue

        # Today's market filter: skip any market whose ticker date matches today (UTC).
        # Weather markets for today are priced by real-time temperature data, not NWS
        # forecasts — our model has zero edge and generates fake 1000%+ signals on 1¢ contracts.
        # Use ticker date rather than close_time because weather markets in US timezones
        # may not close until 5am UTC the next day (>8h from 8pm UTC), defeating a
        # simple hours_to_close filter.
        _today_ticker_str = datetime.now(timezone.utc).strftime("%y%b%d").upper()
        ticker_parts_for_date = ticker.split("-")
        if len(ticker_parts_for_date) >= 2:
            _date_seg = ticker_parts_for_date[1][:7]  # e.g. "26APR04"
            if _date_seg == _today_ticker_str:
                print(
                    f"  [Weather] SKIP {ticker}: today's market (date={_date_seg}) — "
                    f"priced by observed temp, not forecast"
                )
                continue

        # Next-day filter: also skip markets closing within WEATHER_MIN_HOURS_TO_CLOSE
        # (catches edge cases where ticker date doesn't parse cleanly)
        close_str = market.get("close_time") or market.get("expiration_time", "")
        if close_str:
            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                hours_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < WEATHER_MIN_HOURS_TO_CLOSE:
                    print(
                        f"  [Weather] SKIP {ticker}: closes in {hours_left:.1f}h "
                        f"(< {WEATHER_MIN_HOURS_TO_CLOSE}h threshold — same-day market)"
                    )
                    continue
            except Exception:
                pass  # If we can't parse the time, allow it through

        # Match to a city using full-name aliases (Kalshi uses "New York", not "NYC")
        matched_city = None
        for city, aliases in _CITY_ALIASES.items():
            if city not in forecasts:
                continue  # Skip cities with no NWS data
            for alias in aliases:
                if alias in title:
                    matched_city = city
                    break
            if matched_city:
                break

        if not matched_city:
            print(f"  [Weather] SKIP {ticker}: no city match in title (checked: {list(_CITY_ALIASES.keys())})")
            continue
        if matched_city not in forecasts:
            print(f"  [Weather] SKIP {ticker}: matched city {matched_city!r} but NWS forecast unavailable")
            continue

        # Extract threshold and direction from title.
        # Kalshi uses: ">75°", "<68°", or "68-69°" (bucket/range) — no F suffix.
        temp_above_m = re.search(r">\s*(\d+(?:\.\d+)?)°", title)
        temp_below_m = re.search(r"<\s*(\d+(?:\.\d+)?)°", title)
        temp_range_m = re.search(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)°", title)

        threshold: float
        threshold_high: float | None = None
        direction: str

        if temp_above_m:
            threshold = float(temp_above_m.group(1))
            direction = "above"
        elif temp_below_m:
            threshold = float(temp_below_m.group(1))
            direction = "below"
        elif temp_range_m:
            threshold = float(temp_range_m.group(1))
            threshold_high = float(temp_range_m.group(2))
            direction = "range"
        else:
            # Final fallback: old-style "or above/below" keyword matching
            old_temp = re.search(r"(\d+)\s*°?\s*f?", title)
            if old_temp:
                threshold = float(old_temp.group(1))
                if any(w in title for w in ["or above", "above", "over", "warmer", "at least"]):
                    direction = "above"
                elif any(w in title for w in ["or below", "below", "under", "cooler"]):
                    direction = "below"
                else:
                    print(f"  [Weather] SKIP {ticker}: no direction keyword in {title_raw!r}")
                    continue
            else:
                print(f"  [Weather] SKIP {ticker}: no temperature threshold found in {title_raw!r}")
                continue

        # Get forecast temp for the market's resolution date.
        # Parse the date from the TICKER (e.g. KXHIGHNY-26APR05-T67 → April 5, 2026)
        # rather than from close_time in UTC — Kalshi weather markets close at ~11pm
        # local time, so close_time in UTC crosses midnight and returns the WRONG date.
        forecast_temp = None
        target_date = None
        _MONTH_MAP = {
            "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
            "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12,
        }
        try:
            # Ticker format: KXHIGHNY-26APR05-T67
            ticker_parts = ticker.split("-")
            if len(ticker_parts) >= 2:
                date_seg = ticker_parts[1]  # e.g. '26APR05'
                m = re.match(r"(\d{2})([A-Z]{3})(\d{2})$", date_seg)
                if m:
                    yy, mon, dd = int(m.group(1)), m.group(2), int(m.group(3))
                    target_date = datetime(
                        2000 + yy, _MONTH_MAP.get(mon, 1), dd
                    ).date()
                    forecast_temp = _get_forecast_temp_for_date(forecasts[matched_city], target_date)
        except Exception:
            pass

        # Compute days_ahead from target_date (cap at 3)
        if target_date is not None:
            today = datetime.now(timezone.utc).date()
            days_ahead = max(1, min(3, (target_date - today).days + 1))
        else:
            days_ahead = 1

        # Fallback: first daytime period if ticker-date lookup failed
        if forecast_temp is None:
            for period in forecasts[matched_city]["periods"]:
                if "night" not in period.get("name", "").lower():
                    forecast_temp = period["temperature"]
                    break

        if forecast_temp is None:
            print(f"  [Weather] SKIP {ticker}: no daytime period in NWS forecast for {matched_city}")
            continue

        # Calculate edge
        edge_result = calculate_weather_edge(
            city=matched_city,
            forecast_temp=forecast_temp,
            threshold=threshold,
            direction=direction,
            kalshi_price_cents=yes_ask,
            threshold_high=threshold_high,
            days_ahead=days_ahead,
        )

        fair_value = edge_result.get("fair_value", 0)
        rel_edge = edge_result.get("relative_edge", 0)
        corrected = edge_result.get("corrected_temp", forecast_temp)
        threshold_met = edge_result.get("relative_edge", 0) >= MIN_RELATIVE_EDGE
        print(
            f"  [Weather/EDGE] {ticker} | {matched_city} | "
            f"NWS={forecast_temp}°F bias=-{NWS_BIAS_CORRECTION}°F corrected={corrected}°F | "
            f"threshold={threshold}°F {direction} | "
            f"fair={fair_value:.4f} kalshi={yes_ask}¢ ({yes_ask/100:.4f}) | "
            f"edge={edge_result.get('edge', 0):+.4f} rel={rel_edge:+.1%} | "
            f"self_check={'PASS' if edge_result.get('self_check_passed') else 'FAIL'} | "
            f"threshold_met={threshold_met}"
        )

        if edge_result["self_check_passed"] and edge_result["relative_edge"] >= MIN_RELATIVE_EDGE:
            opportunities.append({
                "type": "weather",
                "ticker": ticker,
                "title": market.get("title", ""),
                "market": market,
                "edge_result": edge_result,
                "city": matched_city,
                "forecast_temp": forecast_temp,
                "threshold": threshold,
                "direction": direction,
                "edge": edge_result["edge"],
                "relative_edge": edge_result["relative_edge"],
                "confidence": edge_result["confidence"],
                "recommended_side": "yes" if edge_result["edge"] > 0 else "no",
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            })

    return opportunities


# ---------------------------------------------------------------------------
# Sports parlay scanning
# ---------------------------------------------------------------------------

# Kalshi series tickers where multi-leg parlays live
PARLAY_SERIES_TICKERS = [
    "KXMVECROSSCATEGORY",
    "KXMVESPORTSMULTIGAMEEXTENDED",
]

# City alias map — Kalshi weather market titles use full city names / airport
# codes that don't match the short NWS_CITIES keys directly.
_CITY_ALIASES: dict[str, list[str]] = {
    "NYC":          ["new york", "nyc", "ny city", "central park", "manhattan"],
    "Chicago":      ["chicago", "chi", "midway", "o'hare"],
    "Miami":        ["miami"],
    "Austin":       ["austin"],
    "Denver":       ["denver"],
    "Boston":       ["boston", "fenway"],
    "DC":           ["washington", "d.c.", " dc ", "reagan", "dulles"],
    "SF":           ["san francisco", " sf ", "sfo", "bay area"],
    "LA":           ["los angeles", " la ", "lax", "anaheim"],
    "Seattle":      ["seattle"],
    "Phoenix":      ["phoenix"],
    "Dallas":       ["dallas"],
    "Atlanta":      ["atlanta"],
    "Philadelphia": ["philadelphia", "philly"],
    "Las Vegas":    ["las vegas", "vegas"],
    "Portland":     ["portland"],
    "Minneapolis":  ["minneapolis", "twin cities"],
    "Nashville":    ["nashville"],
}

# Keyword queries to find individual live game markets per sport
LIVE_GAME_QUERIES = {
    "nba": ["nba wins", "nba moneyline", "nba game winner"],
    "mlb": ["mlb wins", "mlb moneyline", "mlb game winner"],
    "nhl": ["nhl wins", "nhl moneyline", "nhl game winner"],
    "nfl": ["nfl wins", "nfl moneyline", "nfl game winner"],
}

# Sport alias dicts for filtering parlay titles by team name
# Keys are the lowercase aliases Kalshi uses in titles (city names, abbreviations, etc.)
_SPORT_ALIAS_DICTS: dict[str, dict[str, str]] = {
    "nba": NBA_TEAM_ALIASES,
    "mlb": MLB_TEAM_ALIASES,
    "nhl": NHL_TEAM_ALIASES,
    "ncaab": NCAAB_TEAM_ALIASES,
}


def _fetch_all_parlay_markets() -> list[dict]:
    """Fetch all open parlay markets from known Kalshi series tickers."""
    all_markets = []
    for series in PARLAY_SERIES_TICKERS:
        result = get_markets(status="open", limit=200, series_ticker=series)
        if "error" not in result:
            all_markets.extend(result.get("markets", []))
    return all_markets


def _filter_markets_by_sport(markets: list[dict], sport: str) -> list[dict]:
    """Filter parlay markets to those matching a sport by team alias lookup.

    Kalshi titles use short names like "yes Boston,yes Cleveland" without
    mentioning the sport. We parse each leg's team name and check if it
    appears in the sport's alias dict from parlay.py. If ANY leg matches,
    the market is included.
    """
    alias_dict = _SPORT_ALIAS_DICTS.get(sport.lower())
    if not alias_dict:
        return markets  # unknown sport, return all

    alias_keys = set(alias_dict.keys())

    filtered = []
    for m in markets:
        title = m.get("title", "")
        # Legs are comma-separated: "yes Boston,yes Cleveland,yes Over 228.5 ..."
        legs = [leg.strip() for leg in title.split(",")]
        for leg in legs:
            # Strip "yes "/"no " prefix to get raw team/prop text
            raw = re.sub(r"^(yes|no)\s+", "", leg, flags=re.IGNORECASE).strip().lower()
            # Check exact alias match (city name, mascot, abbreviation)
            if raw in alias_keys:
                filtered.append(m)
                break
            # Also check if alias is a substring (e.g. "new york m" in "new york mets wins by...")
            for alias in alias_keys:
                if len(alias) >= 3 and alias in raw:
                    filtered.append(m)
                    break
            else:
                continue
            break  # inner break hit — market already added

    return filtered


def scan_parlays(sport: str, odds_data: dict, parlay_markets: list[dict] | None = None) -> list[dict]:
    """
    Scan Kalshi parlay markets for a sport and calculate edges.

    Finds:
    1. Vig stacking — multi-leg YES structurally overpriced (only mechanical edge)

    Args:
        sport: Sport key (e.g. "nba")
        odds_data: Pre-fetched odds data from fetch_consensus_odds()
        parlay_markets: Pre-fetched parlay markets (to avoid re-fetching per sport)

    Returns:
        List of opportunity dicts with edge calculations
    """
    opportunities = []

    # Use pre-fetched markets or fetch now
    if parlay_markets is None:
        parlay_markets = _fetch_all_parlay_markets()

    # Filter to this sport
    markets = _filter_markets_by_sport(parlay_markets, sport)
    print(f"  [Parlays] Found {len(markets)} {sport.upper()} parlay markets (from {len(parlay_markets)} total)")

    # Pre-compute sport-level consensus sharpness (Fix #4)
    # If books disagree significantly, we're working with uncertain/stale data.
    now_utc_scan = datetime.now(timezone.utc)
    all_stds = []
    game_commence_by_team: dict[str, datetime] = {}  # team_lower → earliest game dt
    for g in odds_data.get("games", []):
        for team, std_val in g.get("consensus_std", {}).items():
            all_stds.append(std_val)
        # Build team→commence_time map for time-to-game check (Fix #5)
        commence_raw = g.get("commence_time", "")
        if commence_raw:
            try:
                dt = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
                for team in [g.get("home_team", ""), g.get("away_team", "")]:
                    if team:
                        game_commence_by_team[team.lower()] = dt
            except (ValueError, TypeError):
                pass
    avg_consensus_std = sum(all_stds) / len(all_stds) if all_stds else 0.0

    for market in markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        no_ask = market.get("no_ask")

        if not yes_ask or yes_ask <= 0:
            continue

        # Fix #2: Skip illiquid markets — stale prices with no real counterparty
        volume = market.get("volume") or 0
        open_interest = market.get("open_interest") or 0
        if volume < 10 and open_interest < 5:
            continue

        # Calculate parlay edge (this parses legs and prices them)
        edge_result = calculate_parlay_edge(
            market_title=title,
            kalshi_price_cents=yes_ask,
            sport=sport,
            odds_data=odds_data,
        )

        if not edge_result["self_check_passed"]:
            continue

        # Fallback threshold scales with parlay size:
        # 2-leg parlays require all real data (1 fallback = 50% guessed)
        # 3+ leg parlays tolerate at most 1 fallback
        legs = edge_result.get("legs", [])
        fallback_count = sum(1 for leg in legs if "fallback" in (leg.get("source") or ""))
        max_allowed = 0 if len(legs) <= 2 else 1
        if fallback_count > max_allowed:
            continue

        # Fix #5: Time-to-game edge requirement
        # Find the earliest game start for any matched leg team
        leg_teams = [(leg.get("team") or "").lower() for leg in legs]
        earliest_game_dt = None
        for team in leg_teams:
            for candidate_team, dt in game_commence_by_team.items():
                if team and team in candidate_team:
                    if earliest_game_dt is None or dt < earliest_game_dt:
                        earliest_game_dt = dt
                    break
        hours_to_game = 0.0
        if earliest_game_dt:
            hours_to_game = (earliest_game_dt - now_utc_scan).total_seconds() / 3600
        min_edge_required = MIN_RELATIVE_EDGE
        if hours_to_game > 48:
            min_edge_required = MIN_RELATIVE_EDGE + (hours_to_game - 48) * 0.002

        # Build confidence with consensus width penalty and dynamic win rate
        base_confidence = edge_result.get("confidence", 0.80)
        dynamic_conf_no = _get_dynamic_confidence("vig_stack_no", base_confidence)
        # Penalize when books disagree significantly (uncertain/stale pricing)
        if avg_consensus_std > 0.05:
            dynamic_conf_no = round(dynamic_conf_no * 0.85, 3)

        # Only check for NO edge (vig stacking — Kalshi YES structurally overpriced).
        # parlay_yes is disabled: it requires a better model than the market to be
        # profitable, which we don't have. vig_stack_no is a mechanical structural edge
        # that doesn't depend on predicting game outcomes.
        leg_probs = [
            leg.get("probability", 0.5) for leg in edge_result["legs"]
            if leg.get("probability") is not None
        ]
        if len(leg_probs) >= 2 and no_ask and no_ask > 0:
            vig_result = calculate_vig_stack(leg_probs, yes_ask)
            if (vig_result["self_check_passed"]
                    and vig_result["relative_no_edge"] >= min_edge_required):
                opportunities.append({
                    "type": "vig_stack_no",
                    "ticker": ticker,
                    "title": title,
                    "market": market,
                    "edge_result": edge_result,
                    "vig_result": vig_result,
                    "edge": vig_result["no_edge"],
                    "relative_edge": vig_result["relative_no_edge"],
                    "confidence": dynamic_conf_no,
                    "recommended_side": "no",
                    "legs": edge_result["legs"],
                    "hours_to_game": round(hours_to_game, 1),
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                })

    return opportunities


# ---------------------------------------------------------------------------
# Live single-game market scanner (latency arb)
# ---------------------------------------------------------------------------

def scan_live_game_markets(sport: str, odds_data: dict) -> list[dict]:
    """
    Scan Kalshi single-game markets for live games and detect latency arb edges.

    During active games, Kalshi prices often lag behind sportsbook line movements.
    If ESPN consensus has moved but Kalshi hasn't repriced, that gap is the edge.

    Args:
        sport: Sport key (e.g. "nba")
        odds_data: Pre-fetched odds data from fetch_consensus_odds()

    Returns:
        List of opportunity dicts with type "live_latency_arb"
    """
    opportunities = []

    # Only scan during live games
    live_games = [
        g for g in odds_data.get("games", [])
        if g.get("status") in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
        and g.get("consensus")
    ]
    if not live_games:
        return opportunities

    print(f"  [Live/{sport.upper()}] {len(live_games)} live game(s) found — searching Kalshi...")

    # Build a set of all team names in live games for quick lookup
    # Also build a map: normalized_team_name -> (game, prob)
    alias_dict = _SPORT_ALIAS_DICTS.get(sport.lower(), {})

    team_to_game_prob: dict[str, tuple[dict, float]] = {}
    for game in live_games:
        for team_name, prob in game["consensus"].items():
            team_to_game_prob[team_name.lower()] = (game, prob)
            # Also index short aliases that resolve to this team
            for alias, canonical in alias_dict.items():
                if canonical == team_name:
                    team_to_game_prob[alias] = (game, prob)

    # Fetch candidate markets: use sport-specific queries + team names
    candidate_markets_by_ticker: dict[str, dict] = {}

    queries = list(LIVE_GAME_QUERIES.get(sport.lower(), []))
    # Also search by each live team's short name
    for game in live_games:
        for team in (game.get("home_team", ""), game.get("away_team", "")):
            if team:
                # Use just the city/first word to avoid overly specific queries
                short = team.split()[0].lower()
                if len(short) >= 3:
                    queries.append(short)

    for query in queries:
        result = get_markets(query=query, status="open", limit=100)
        if "error" not in result:
            for m in result.get("markets", []):
                # Exclude parlay series — those are multi-leg, not single-game
                if m.get("series_ticker") in PARLAY_SERIES_TICKERS:
                    continue
                candidate_markets_by_ticker.setdefault(m["ticker"], m)

    print(f"  [Live/{sport.upper()}] {len(candidate_markets_by_ticker)} candidate markets to check")

    for market in candidate_markets_by_ticker.values():
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        # Structural parlay detection: multi-leg parlays have multiple
        # comma-separated segments each starting with "yes" or "no".
        # This check is series-ticker-agnostic — it works regardless of
        # which series the market came from or whether series_ticker is set.
        title_segments = [s.strip() for s in title.split(",")]
        parlay_leg_count = sum(
            1 for s in title_segments
            if re.match(r"^(yes|no)\s+", s, re.IGNORECASE)
        )
        if parlay_leg_count > 1:
            # Multi-leg parlay — skip. scan_parlays() prices these correctly
            # using the product of all leg probabilities. Pricing only the
            # matched live leg here would produce a wildly wrong fair value.
            continue

        title_lower = title.lower()

        # Try to match this market's title to a live team
        matched_team = None
        matched_game = None
        espn_prob = None

        for team_key, (game, prob) in team_to_game_prob.items():
            if len(team_key) >= 3 and team_key in title_lower:
                matched_team = team_key
                matched_game = game
                espn_prob = prob
                break

        if matched_team is None or espn_prob is None:
            continue

        # Single-game market: YES = this team wins (or over/under resolves).
        # Compare the single-outcome ESPN probability to the Kalshi price.
        kalshi_price = yes_ask / 100.0

        edge = espn_prob - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        # Self-check
        check_ok, check_msg = _self_check_edge(espn_prob, kalshi_price, edge)
        if not check_ok:
            continue

        if abs(relative_edge) < MIN_RELATIVE_EDGE:
            continue

        recommended_side = "yes" if edge > 0 else "no"
        opportunities.append({
            "type": "live_latency_arb",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": 0.75,  # Moderate confidence — live odds can move fast
            "recommended_side": recommended_side,
            "espn_prob": round(espn_prob, 4),
            "kalshi_price": round(kalshi_price, 4),
            "matched_team": matched_team,
            "game": {
                "home_team": matched_game.get("home_team"),
                "away_team": matched_game.get("away_team"),
                "status": matched_game.get("status"),
            },
            # edge_result mirrors the standard format so format_opportunity() and
            # _passes_sanity() work without special-casing this type
            "edge_result": {
                "fair_value": round(espn_prob, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": 0.75,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "math_chain": [check_msg],
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities


# ---------------------------------------------------------------------------
# Single-game and player-prop market scanner (pregame + live)
# ---------------------------------------------------------------------------

# Keyword queries for Kalshi single-game / individual market search per sport
_SINGLE_GAME_QUERIES: dict[str, list[str]] = {
    "nba": ["nba wins", "nba winner", "nba moneyline", "nba game winner", "nba series winner"],
    "mlb": ["mlb wins", "mlb winner", "mlb game winner", "wins game", "wins series"],
    "nhl": ["nhl wins", "nhl winner", "nhl game winner", "nhl series"],
    "nfl": ["nfl wins", "nfl winner", "nfl game winner", "super bowl"],
    "ncaab": ["ncaab winner", "march madness", "college basketball winner"],
}

# Player prop keyword queries
_PROP_QUERIES = [
    "points scored", "assists", "rebounds", "strikeouts", "home run",
    "goals scored", "touchdowns", "rushing yards",
]


def scan_single_game_markets(sport: str, odds_data: dict) -> list[dict]:
    """
    Scan Kalshi for individual game outcome and player prop markets (pregame + live).

    Unlike scan_parlays() which only targets known parlay series tickers,
    this searches broadly by keyword and team name to find:
      - Single-team win markets (1-leg "parlays")
      - Player prop markets (points, assists, rebounds, etc.)
      - Any market outside the known parlay series tickers

    Args:
        sport: Sport key (e.g. "nba")
        odds_data: Pre-fetched odds data from fetch_consensus_odds()

    Returns:
        List of opportunity dicts with type "pregame_single_game" or
        "live_latency_arb" depending on game status.
    """
    opportunities = []

    games_with_odds = [
        g for g in odds_data.get("games", [])
        if g.get("consensus")
    ]
    if not games_with_odds:
        return opportunities

    # Build team name → (game, probability) lookup including aliases
    alias_dict = _SPORT_ALIAS_DICTS.get(sport.lower(), {})
    team_to_game_prob: dict[str, tuple[dict, float]] = {}
    for game in games_with_odds:
        for team_name, prob in game["consensus"].items():
            team_to_game_prob[team_name.lower()] = (game, prob)
            for alias, canonical in alias_dict.items():
                if canonical == team_name:
                    team_to_game_prob[alias] = (game, prob)

    # Collect search queries
    queries = list(_SINGLE_GAME_QUERIES.get(sport.lower(), []))
    queries.extend(_PROP_QUERIES)

    # Add short team names from today's games (city name = first word)
    for game in games_with_odds:
        for team in (game.get("home_team", ""), game.get("away_team", "")):
            short = team.split()[0].lower()
            if len(short) >= 3:
                queries.append(short)

    candidate_markets: dict[str, dict] = {}
    for query in queries:
        result = get_markets(query=query, status="open", limit=100)
        if "error" not in result:
            for m in result.get("markets", []):
                # Exclude markets explicitly in the known parlay series
                series = m.get("series_ticker") or ""
                if series in PARLAY_SERIES_TICKERS:
                    continue
                # Also skip multi-leg parlay titles (comma-separated yes/no legs)
                title = m.get("title", "")
                leg_count = sum(
                    1 for seg in title.split(",")
                    if re.match(r"^\s*(yes|no)\s+", seg, re.IGNORECASE)
                )
                if leg_count > 1:
                    continue
                candidate_markets.setdefault(m["ticker"], m)

    print(
        f"  [Single/{sport.upper()}] {len(games_with_odds)} games with odds | "
        f"{len(candidate_markets)} candidate Kalshi single-game/prop markets"
    )

    for market in candidate_markets.values():
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        title_lower = title.lower()

        # Try to match to a team with a known sportsbook probability
        matched_team = None
        matched_game = None
        espn_prob = None

        for team_key, (game, prob) in team_to_game_prob.items():
            if len(team_key) >= 3 and team_key in title_lower:
                matched_team = team_key
                matched_game = game
                espn_prob = prob
                break

        if matched_team is None or espn_prob is None:
            continue

        kalshi_price = yes_ask / 100.0
        edge = espn_prob - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        # Self-check
        from bot.math_engine import _self_check_edge
        check_ok, check_msg = _self_check_edge(espn_prob, kalshi_price, edge)
        if not check_ok:
            continue

        # Near-zero guard
        if kalshi_price <= 0.03 or espn_prob <= 0.03:
            continue

        if abs(relative_edge) < MIN_RELATIVE_EDGE:
            continue

        game_status = matched_game.get("status", "STATUS_SCHEDULED")
        is_live = game_status in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")

        opportunities.append({
            "type": "live_latency_arb" if is_live else "pregame_single_game",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": 0.75 if is_live else 0.80,
            "recommended_side": "yes" if edge > 0 else "no",
            "espn_prob": round(espn_prob, 4),
            "kalshi_price": round(kalshi_price, 4),
            "matched_team": matched_team,
            "game": {
                "home_team": matched_game.get("home_team"),
                "away_team": matched_game.get("away_team"),
                "status": game_status,
                "commence_time": matched_game.get("commence_time"),
            },
            "edge_result": {
                "fair_value": round(espn_prob, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": 0.75 if is_live else 0.80,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities


# ---------------------------------------------------------------------------
# Vig Stack Series Scanner (structural NO edge — no external odds needed)
# ---------------------------------------------------------------------------

def scan_vig_stack_series() -> list[dict]:
    """
    Scan Kalshi series ladders for structural NO edges caused by vig stacking.

    For any complete series (weather ladders, score ladders) where the YES
    contracts are mutually exclusive and exhaustive:
      - Sum of all YES ask prices should equal ~100¢ (1.0)
      - When sum > 105¢, every contract is structurally overpriced on the YES side
      - NO fair value = 100¢ - (YES_ask / vig_factor * 100)
      - If NO_fair > NO_ask: structural edge with NO prediction needed

    This scanner is completely independent of external odds sources.
    Wide-spread thin markets are NOT filtered out — thin markets are the opportunity.
    """
    opportunities = []

    for series_ticker in WEATHER_SERIES_TICKERS:
        try:
            result = get_markets(series_ticker=series_ticker, status="open", limit=100)
        except Exception as e:
            print(f"  [VigStack/{series_ticker}] API error: {e}")
            continue

        if "error" in result:
            continue

        markets = result.get("markets", [])
        if len(markets) < 2:
            continue  # Need at least 2 contracts to have a meaningful series

        # Collect YES ask prices for all contracts in series
        yes_asks = []
        valid_markets = []
        for m in markets:
            ya = m.get("yes_ask")
            na = m.get("no_ask")
            if ya and ya > 0 and na and na > 0:
                yes_asks.append(ya)
                valid_markets.append(m)

        if len(valid_markets) < 2:
            continue

        # Sum of YES asks in cents
        yes_sum = sum(yes_asks)
        yes_sum_prob = yes_sum / 100.0  # convert to probability

        # Only worth scanning if there's meaningful vig (>5% excess)
        if yes_sum_prob < 1.05:
            continue

        vig_factor = yes_sum_prob  # = sum/100 — the multiplier inflating each YES price
        vig_excess_cents = yes_sum - 100  # excess cents above par

        print(
            f"  [VigStack/{series_ticker}] {len(valid_markets)} contracts | "
            f"YES sum={yes_sum}¢ ({yes_sum_prob:.1%}) | vig_excess={vig_excess_cents}¢"
        )

        for market in valid_markets:
            ticker = market.get("ticker", "")
            title = market.get("title", "")
            yes_ask = market.get("yes_ask")
            no_ask = market.get("no_ask")

            # Skip today's contracts — the vig stack model assumes prices reflect
            # structural vig inflation. Today's markets are priced by observed temperature,
            # not by vig, so the math produces fake 1000%+ "edges".
            # Use ticker date (e.g. "26APR04") rather than hours_to_close because US
            # weather markets close at ~midnight local time = up to 5am UTC, which
            # can be >8h from an 8pm UTC scan.
            _today_vs = datetime.now(timezone.utc).strftime("%y%b%d").upper()
            _tp = ticker.split("-")
            if len(_tp) >= 2 and _tp[1][:7] == _today_vs:
                continue

            # Also skip contracts where yes_ask is extreme (near-certain/near-impossible)
            # — these are either resolving or heavily directional and break the vig formula
            if yes_ask >= 85 or yes_ask <= 15:
                continue

            # NO fair value = 100¢ - (YES_ask adjusted for vig)
            yes_fair_cents = yes_ask / vig_factor
            no_fair_cents = 100.0 - yes_fair_cents

            no_ask_prob = no_ask / 100.0
            no_fair_prob = no_fair_cents / 100.0
            no_edge = no_fair_prob - no_ask_prob
            relative_no_edge = no_edge / no_ask_prob if no_ask_prob > 0 else 0.0

            # Skip near-zero-price NO contracts (YES near-certain)
            if no_ask_prob < 0.03:
                continue

            # Vig stack series edge is purely structural (no prediction risk).
            # Use a lower threshold than directional trades: 2% is actionable here
            # because the math self-check guarantees the edge is real.
            VIG_STACK_MIN_EDGE = 0.02
            if relative_no_edge < VIG_STACK_MIN_EDGE:
                continue

            # Self-checks
            check_ok, check_msg = _self_check_edge(no_fair_prob, no_ask_prob, no_edge)
            if not check_ok:
                continue

            math_chain = [
                f"Series: {series_ticker} | {len(valid_markets)} contracts",
                f"YES sum: {yes_sum}¢ ({yes_sum_prob:.3f}) — vig_factor={vig_factor:.4f}",
                f"This contract YES_ask={yes_ask}¢",
                f"YES_fair = {yes_ask}¢ / {vig_factor:.4f} = {yes_fair_cents:.2f}¢",
                f"NO_fair = 100 - {yes_fair_cents:.2f} = {no_fair_cents:.2f}¢",
                f"NO_ask = {no_ask}¢ | Edge = {no_edge:.4f} ({relative_no_edge:.1%} relative)",
                check_msg,
            ]

            print(
                f"  [VigStack/{ticker}] YES_ask={yes_ask}¢ YES_fair={yes_fair_cents:.1f}¢ "
                f"NO_fair={no_fair_cents:.1f}¢ NO_ask={no_ask}¢ edge={relative_no_edge:+.1%}"
            )

            opportunities.append({
                "type": "vig_stack_series",
                "ticker": ticker,
                "title": title,
                "market": market,
                "series_ticker": series_ticker,
                "edge": round(no_edge, 4),
                "relative_edge": round(relative_no_edge, 4),
                "confidence": 0.90,  # Purely mechanical — no prediction needed
                "recommended_side": "no",
                "yes_sum_cents": yes_sum,
                "vig_factor": round(vig_factor, 4),
                "no_fair_cents": round(no_fair_cents, 2),
                "no_ask_cents": no_ask,
                "edge_result": {
                    "fair_value": round(no_fair_prob, 4),
                    "kalshi_price": round(no_ask_prob, 4),
                    "edge": round(no_edge, 4),
                    "relative_edge": round(relative_no_edge, 4),
                    "confidence": 0.90,
                    "self_check_passed": True,
                    "math_chain": math_chain,
                    "warnings": [],
                },
            })

    return opportunities


# ---------------------------------------------------------------------------
# Main scan cycle
# ---------------------------------------------------------------------------

def scan_cycle(sports: Optional[list[str]] = None) -> dict:
    """
    Run a full scan cycle across all active sports and weather markets.

    Returns:
        {
            opportunities: list[dict],  # Ranked by relative_edge
            line_movements: list[dict],
            games_scanned: int,
            scan_interval: int,         # Recommended next scan interval
            odds_api_requests_used: int,
            timestamp: str,
        }
    """
    sports = sports or ACTIVE_SPORTS
    all_opportunities = []
    all_games = []
    line_movements = []

    print(f"\n{'='*60}")
    print(f"SCAN CYCLE — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")

    # Load previous snapshot for line movement detection
    prev_snapshot = _load_previous_snapshot()

    # ACTIVE_STRATEGIES controls what runs. Parlays + sports are disabled until
    # CLV data proves they have genuine edge. Only weather + vig_stack_series trade.
    sports_active = any(
        s in ACTIVE_STRATEGIES
        for s in ("parlay", "series_game_edge", "live_latency_arb", "pregame_single_game")
    )

    if sports_active:
        # Pre-fetch all parlay markets once (avoids redundant API calls per sport)
        print(f"  [PARLAYS] Pre-fetching parlay markets from Kalshi...")
        all_parlay_markets = _fetch_all_parlay_markets()
        print(f"  [PARLAYS] Found {len(all_parlay_markets)} total parlay markets")

        for sport in sports:
            print(f"\n  [{sport.upper()}] Fetching odds...")
            try:
                odds_data = fetch_consensus_odds(sport)
            except Exception as e:
                print(f"  [{sport.upper()}] Error fetching odds: {e}")
                continue

            if "error" in odds_data:
                print(f"  [{sport.upper()}] {odds_data['error']}")
                continue

            games = odds_data.get("games", [])
            all_games.extend(games)
            source = odds_data.get("source", "unknown")
            games_with_odds = len([g for g in games if g.get("consensus")])
            print(f"  [{sport.upper()}] {len(games)} games ({games_with_odds} with odds) | source: {source}")

            # Detect line movements (still track even if not trading sports)
            prev_sport = prev_snapshot.get("current", {}).get(sport, {})
            movements = _detect_line_movements(odds_data, prev_sport)
            if movements:
                print(f"  [{sport.upper()}] {len(movements)} significant line movements detected")
                line_movements.extend(movements)

            # Save snapshot
            current_snapshot = prev_snapshot.get("current", {})
            current_snapshot[sport] = odds_data
            _save_snapshot(current_snapshot)

            if "parlay" in ACTIVE_STRATEGIES:
                print(f"  [{sport.upper()}] Scanning parlays...")
                sport_opps = scan_parlays(sport, odds_data, parlay_markets=all_parlay_markets)
                all_opportunities.extend(sport_opps)
                print(f"  [{sport.upper()}] Found {len(sport_opps)} parlay opportunities")

            if "live_latency_arb" in ACTIVE_STRATEGIES:
                print(f"  [{sport.upper()}] Scanning live game markets...")
                live_opps = scan_live_game_markets(sport, odds_data)
                all_opportunities.extend(live_opps)
                print(f"  [{sport.upper()}] Found {len(live_opps)} live latency arb opportunities")

            if "pregame_single_game" in ACTIVE_STRATEGIES:
                print(f"  [{sport.upper()}] Scanning single-game / prop markets...")
                single_opps = scan_single_game_markets(sport, odds_data)
                all_opportunities.extend(single_opps)
                print(f"  [{sport.upper()}] Found {len(single_opps)} single-game/prop opportunities")
    else:
        print(f"  [SPORTS] Disabled — not in ACTIVE_STRATEGIES. Skipping all sports scans.")

    # Scan weather markets (free — no API quota)
    print(f"\n  [WEATHER] Scanning weather markets...")
    weather_opps = scan_weather_markets()
    all_opportunities.extend(weather_opps)
    print(f"  [WEATHER] Found {len(weather_opps)} opportunities")

    # Tickers where weather model recommends BUY YES — these conflict with vig stack
    # which always recommends BUY NO. Weather has direct NWS signal; vig stack is
    # mechanical. Suppress vig stack on any ticker where they disagree.
    _weather_yes_tickers = {
        opp["ticker"] for opp in weather_opps
        if opp.get("recommended_side") == "yes"
    }

    # Scan vig stack series (structural NO edge — no external odds, no liquidity filter)
    print(f"\n  [VIG_STACK] Scanning series ladders for structural NO edges...")
    vig_stack_opps = scan_vig_stack_series()
    if _weather_yes_tickers:
        _before = len(vig_stack_opps)
        vig_stack_opps = [o for o in vig_stack_opps if o["ticker"] not in _weather_yes_tickers]
        _suppressed = _before - len(vig_stack_opps)
        if _suppressed:
            print(f"  [VIG_STACK] Suppressed {_suppressed} signal(s): weather model recommends YES on same ticker (conflicts with structural NO)")
    # Cap correlated vig stack signals sharing the same series prefix
    vig_stack_opps = _cap_correlated_vig_stack(vig_stack_opps)
    all_opportunities.extend(vig_stack_opps)
    print(f"  [VIG_STACK] Found {len(vig_stack_opps)} structural vig stack opportunities")

    # Scan Kalshi series tickers (individual game markets + BTC)
    # Only run if these types are in ACTIVE_STRATEGIES
    if any(s in ACTIVE_STRATEGIES for s in ("series_game_edge", "btc_price_edge", "ipl_game_edge", "eth_price_edge")):
        print(f"\n  [SERIES] Scanning Kalshi series markets...")
        series_opps = scan_series_markets()
        for opp in series_opps:
            opp_type = opp.get("type", "")
            if opp_type in ("series_game_edge", "btc_price_edge", "ipl_game_edge", "eth_price_edge"):
                opp["confidence"] = _get_dynamic_confidence(opp_type, opp.get("confidence", 0.75))
        # Apply home/away confidence modifier to sports opportunities
        series_opps = [_apply_home_away_modifier(o) for o in series_opps]
        all_opportunities.extend(series_opps)
        print(f"  [SERIES] Found {len(series_opps)} total series opportunities")
    else:
        print(f"  [SERIES] Disabled — not in ACTIVE_STRATEGIES.")

    if "econ_cpi_edge" in ACTIVE_STRATEGIES:
        print(f"\n  [ECON] Scanning economic markets...")
        from bot.econ_scanner import scan_econ_markets
        econ_opps = scan_econ_markets()
        print(f"  [ECON] Found {len(econ_opps)} opportunities")
        all_opportunities.extend(econ_opps)

    # Injury filter: drop series_game_edge opps where the team has a player OUT.
    # Uses ESPN's free injury API. Fails open — if ESPN is down, opp is NOT dropped.
    print(f"\n  [INJURIES] Checking injury reports for series game edges...")
    try:
        from bot.injuries import check_game_injuries
        pre_injury = len(all_opportunities)
        checked_opps = []
        for opp in all_opportunities:
            if opp.get("type") == "series_game_edge":
                canonical = opp.get("canonical_team", "")
                sport     = opp.get("sport", "")
                if canonical and sport:
                    inj = check_game_injuries(canonical, sport)
                    if inj["stale"]:
                        print(
                            f"  [INJURIES] STALE {opp.get('ticker', '?')} — "
                            + (inj["warnings"][0] if inj["warnings"] else "injury detected")
                        )
                        continue  # drop from opportunities
                    if inj["warnings"]:
                        opp.setdefault("warnings", []).extend(inj["warnings"])
            checked_opps.append(opp)
        dropped_inj = pre_injury - len(checked_opps)
        if dropped_inj:
            print(f"  [INJURIES] Dropped {dropped_inj} STALE opportunities")
        else:
            print(f"  [INJURIES] No injury-stale opportunities found")
        all_opportunities = checked_opps
    except Exception as e:
        print(f"  [INJURIES] Injury check error (fail-open): {e}")

    # Strategy gate: drop any opp type not in ACTIVE_STRATEGIES.
    # This is the final enforcement — even if a scanner runs, it won't reach Tyler.
    before_gate = len(all_opportunities)
    all_opportunities = [o for o in all_opportunities if o.get("type") in ACTIVE_STRATEGIES]
    dropped_gate = before_gate - len(all_opportunities)
    if dropped_gate:
        print(f"  [GATE] Dropped {dropped_gate} opportunities from inactive strategies")

    # Final sanity filter: drop anything with a failed self-check or near-zero prices
    # before opportunities ever reach the alert/Telegram layer
    def _passes_sanity(opp: dict) -> bool:
        # Default to False (fail-safe): self_check_passed must be explicitly True
        edge_result = opp.get("edge_result") or {}
        if not edge_result.get("self_check_passed", False):
            return False
        vig_result = opp.get("vig_result") or {}
        if vig_result and not vig_result.get("self_check_passed", False):
            return False
        # Near-zero guard: Kalshi's minimum price is 1¢ (0.01), which already
        # produces absurd relative edges. Block anything under 3¢ on either side.
        fv = edge_result.get("fair_value", 0.0)
        kp = edge_result.get("kalshi_price", 0.0)
        if fv <= 0.03 or kp <= 0.03:
            return False
        return True

    before = len(all_opportunities)
    all_opportunities = [o for o in all_opportunities if _passes_sanity(o)]
    dropped = before - len(all_opportunities)
    if dropped:
        print(f"  [SANITY] Dropped {dropped} opportunity/ies with failed self-checks or near-zero values")

    # Apply B2B confidence penalty before ranking
    all_opportunities = [_apply_b2b_penalty(o) for o in all_opportunities]

    # Rank by EV (|edge| × confidence) — highest conviction bets surface first
    all_opportunities = _sort_by_ev(all_opportunities)

    # Determine next scan interval
    scan_interval = get_scan_interval(all_games)
    interval_label = {
        SCAN_INTERVAL_LIVE: "LIVE (2 min)",
        SCAN_INTERVAL_PREGAME: "PREGAME (10 min)",
        SCAN_INTERVAL_IDLE: "IDLE (30 min)",
    }.get(scan_interval, f"{scan_interval}s")

    print(f"\n  SUMMARY: {len(all_opportunities)} opportunities | {len(line_movements)} line moves | Next scan: {interval_label}")
    print(f"{'='*60}\n")

    return {
        "opportunities": all_opportunities,
        "line_movements": line_movements,
        "games_scanned": len(all_games),
        "scan_interval": scan_interval,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def morning_weather_scan() -> list[dict]:
    """
    Lightweight scan that only checks weather markets.
    Called by the morning briefing — no sports scan, no API usage.
    """
    print(f"\n  [MORNING] Weather-only scan...")
    opportunities = scan_weather_markets()
    print(f"  [MORNING] Found {len(opportunities)} weather opportunities")
    return opportunities
