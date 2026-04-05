"""
Nexus Trading Bot — Sports Market Scanners

Covers parlay vig-stack scanning, live latency arb, and single-game/prop markets.
All three share the same sportsbook odds source and Kalshi market search pattern.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("glint.sports")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.kalshi_client import get_markets
from agent.parlay import (
    parse_parlay_title, price_parlay,
    NBA_TEAM_ALIASES, MLB_TEAM_ALIASES, NHL_TEAM_ALIASES, NCAAB_TEAM_ALIASES,
)
from agent.player_stats import estimate_player_prop_probability
from bot.math_engine import calculate_parlay_edge, calculate_vig_stack, _self_check_edge
from bot.config import MIN_RELATIVE_EDGE


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Kalshi series tickers where multi-leg parlays live
PARLAY_SERIES_TICKERS = [
    "KXMVECROSSCATEGORY",
    "KXMVESPORTSMULTIGAMEEXTENDED",
]

# Keyword queries to find individual live game markets per sport
LIVE_GAME_QUERIES = {
    "nba": ["nba wins", "nba moneyline", "nba game winner"],
    "mlb": ["mlb wins", "mlb moneyline", "mlb game winner"],
    "nhl": ["nhl wins", "nhl moneyline", "nhl game winner"],
    "nfl": ["nfl wins", "nfl moneyline", "nfl game winner"],
}

# Sport alias dicts for filtering parlay titles by team name
_SPORT_ALIAS_DICTS: dict[str, dict[str, str]] = {
    "nba": NBA_TEAM_ALIASES,
    "mlb": MLB_TEAM_ALIASES,
    "nhl": NHL_TEAM_ALIASES,
    "ncaab": NCAAB_TEAM_ALIASES,
}

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


# ---------------------------------------------------------------------------
# Shared utility (mirrors scanner.py — reads from tracker for calibration)
# ---------------------------------------------------------------------------

def _get_dynamic_confidence(opp_type: str, default: float = 0.80) -> float:
    """
    Blend a hardcoded prior with the bot's actual historical win rate.
    Requires at least 20 resolved trades before deviating from default.
    """
    try:
        from bot.tracker import get_roi_by_strategy
        stats = get_roi_by_strategy().get(opp_type, {})
        n = stats.get("total", 0)
        if n < 20:
            return default
        win_rate = stats.get("winrate", default)
        weight = min(1.0, (n - 20) / 80)
        blended = default * (1 - weight) + win_rate * weight
        return round(max(0.1, min(0.99, blended)), 3)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Parlay market helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Parlay scanner
# ---------------------------------------------------------------------------

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
    logger.info("Found %d %s parlay markets (from %d total)", len(markets), sport.upper(), len(parlay_markets))

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

    logger.info("[Live/%s] %d live game(s) found — searching Kalshi...", sport.upper(), len(live_games))

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

    logger.info("[Live/%s] %d candidate markets to check", sport.upper(), len(candidate_markets_by_ticker))

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

    logger.info("[Single/%s] %d games with odds | %d candidate Kalshi markets",
                sport.upper(), len(games_with_odds), len(candidate_markets))

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
