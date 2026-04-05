"""
Nexus Trading Bot — Continuous Market Scanner

Polls sportsbook odds + Kalshi markets, detects edges.
Smart scheduling: scan fast during live games, slow when idle.
Tracks Odds API usage to stay under monthly limit.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.kalshi_client import get_markets
from bot.odds_scraper import fetch_consensus_odds

from bot.config import (
    ACTIVE_SPORTS, LINE_MOVEMENT_THRESHOLD,
    MIN_RELATIVE_EDGE, SCAN_INTERVAL_IDLE, SCAN_INTERVAL_PREGAME,
    SCAN_INTERVAL_LIVE, ODDS_SNAPSHOTS_FILE, BOT_STATE_FILE,
    WEATHER_SERIES_TICKERS, ACTIVE_STRATEGIES,
)
from bot.math_engine import _self_check_edge
from bot.kalshi_series import scan_series_markets
import bot.kalshi_series as _kalshi_series_mod
from bot.price_monitor import PriceMonitor
from bot.scanner_weather import scan_weather_markets
from bot.scanner_sports import (
    _fetch_all_parlay_markets,
    scan_parlays,
    scan_live_game_markets,
    scan_single_game_markets,
)

import logging
logger = logging.getLogger("glint.scanner")

_price_monitor = PriceMonitor()


# ---------------------------------------------------------------------------
# Dynamic confidence based on historical win rate
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
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupted file (e.g. incomplete write) — reset and continue
            path.write_text("{}")
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
        if opp.get("b2b"):
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
            logger.warning("VigStack/%s API error: %s", series_ticker, e)
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
                volume = m.get("volume") or 0
                open_interest = m.get("open_interest") or 0
                if volume < 10 and open_interest < 5:
                    continue
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

        logger.debug("VigStack/%s %d contracts | YES sum=%s¢ (%.1f%%) | vig_excess=%s¢",
                     series_ticker, len(valid_markets), yes_sum, yes_sum_prob * 100, vig_excess_cents)

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

            logger.debug("VigStack/%s YES_ask=%s¢ YES_fair=%.1f¢ NO_fair=%.1f¢ NO_ask=%s¢ edge=%+.1f%%",
                         ticker, yes_ask, yes_fair_cents, no_fair_cents, no_ask, relative_no_edge * 100)

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

    logger.info("SCAN CYCLE — %s", datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))

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
        logger.info("PARLAYS: pre-fetching parlay markets from Kalshi")
        all_parlay_markets = _fetch_all_parlay_markets()
        logger.info("PARLAYS: found %d total parlay markets", len(all_parlay_markets))

        for sport in sports:
            logger.info("%s: fetching odds", sport.upper())
            try:
                odds_data = fetch_consensus_odds(sport)
            except Exception as e:
                logger.warning("%s: error fetching odds: %s", sport.upper(), e)
                continue

            if "error" in odds_data:
                logger.warning("%s: %s", sport.upper(), odds_data['error'])
                continue

            games = odds_data.get("games", [])
            all_games.extend(games)
            source = odds_data.get("source", "unknown")
            games_with_odds = len([g for g in games if g.get("consensus")])
            logger.info("%s: %d games (%d with odds) | source: %s", sport.upper(), len(games), games_with_odds, source)

            # Detect line movements (still track even if not trading sports)
            prev_sport = prev_snapshot.get("current", {}).get(sport, {})
            movements = _detect_line_movements(odds_data, prev_sport)
            if movements:
                logger.info("%s: %d significant line movements detected", sport.upper(), len(movements))
                line_movements.extend(movements)

            # Save snapshot
            current_snapshot = prev_snapshot.get("current", {})
            current_snapshot[sport] = odds_data
            _save_snapshot(current_snapshot)

            if "parlay" in ACTIVE_STRATEGIES:
                logger.info("%s: scanning parlays", sport.upper())
                sport_opps = scan_parlays(sport, odds_data, parlay_markets=all_parlay_markets)
                all_opportunities.extend(sport_opps)
                logger.info("%s: found %d parlay opportunities", sport.upper(), len(sport_opps))

            if "live_latency_arb" in ACTIVE_STRATEGIES:
                logger.info("%s: scanning live game markets", sport.upper())
                live_opps = scan_live_game_markets(sport, odds_data)
                all_opportunities.extend(live_opps)
                logger.info("%s: found %d live latency arb opportunities", sport.upper(), len(live_opps))

            if "pregame_single_game" in ACTIVE_STRATEGIES:
                logger.info("%s: scanning single-game / prop markets", sport.upper())
                single_opps = scan_single_game_markets(sport, odds_data)
                all_opportunities.extend(single_opps)
                logger.info("%s: found %d single-game/prop opportunities", sport.upper(), len(single_opps))
    else:
        logger.info("SPORTS: disabled — not in ACTIVE_STRATEGIES, skipping all sports scans")

    # Scan weather markets (free — no API quota)
    logger.info("WEATHER: scanning weather markets")
    weather_opps = scan_weather_markets()
    all_opportunities.extend(weather_opps)
    logger.info("WEATHER: found %d opportunities", len(weather_opps))

    # Tickers where weather model recommends BUY YES with strong edge (>20%) —
    # these conflict with vig stack which always recommends BUY NO. Weather has
    # direct NWS signal; vig stack is mechanical. Only suppress vig stack when
    # weather edge is strong enough to trust over the structural signal.
    _VIG_SUPPRESS_EDGE = 0.20
    _weather_yes_tickers = {
        opp["ticker"] for opp in weather_opps
        if opp.get("recommended_side") == "yes"
        and abs(opp.get("edge", 0)) >= _VIG_SUPPRESS_EDGE
    }

    # Scan vig stack series (structural NO edge — no external odds, no liquidity filter)
    logger.info("VIG_STACK: scanning series ladders for structural NO edges")
    vig_stack_opps = scan_vig_stack_series()
    if _weather_yes_tickers:
        _before = len(vig_stack_opps)
        vig_stack_opps = [o for o in vig_stack_opps if o["ticker"] not in _weather_yes_tickers]
        _suppressed = _before - len(vig_stack_opps)
        if _suppressed:
            logger.info("VIG_STACK: suppressed %d signal(s) — weather model recommends YES on same ticker", _suppressed)
    # Cap correlated vig stack signals sharing the same series prefix
    vig_stack_opps = _cap_correlated_vig_stack(vig_stack_opps)
    all_opportunities.extend(vig_stack_opps)
    logger.info("VIG_STACK: found %d structural vig stack opportunities", len(vig_stack_opps))

    # Scan Kalshi series tickers (individual game markets + BTC)
    # Only run if these types are in ACTIVE_STRATEGIES
    if any(s in ACTIVE_STRATEGIES for s in ("series_game_edge", "btc_price_edge", "ipl_game_edge", "eth_price_edge")):
        logger.info("SERIES: scanning Kalshi series markets")
        series_opps = scan_series_markets()
        for opp in series_opps:
            opp_type = opp.get("type", "")
            if opp_type in ("series_game_edge", "btc_price_edge", "ipl_game_edge", "eth_price_edge"):
                opp["confidence"] = _get_dynamic_confidence(opp_type, opp.get("confidence", 0.75))
        # Apply home/away confidence modifier to sports opportunities
        series_opps = [_apply_home_away_modifier(o) for o in series_opps]
        all_opportunities.extend(series_opps)
        logger.info("SERIES: found %d total series opportunities", len(series_opps))
    else:
        logger.info("SERIES: disabled — not in ACTIVE_STRATEGIES")

    if "econ_cpi_edge" in ACTIVE_STRATEGIES:
        logger.info("ECON: scanning economic markets")
        from bot.econ_scanner import scan_econ_markets
        econ_opps = scan_econ_markets()
        logger.info("ECON: found %d opportunities", len(econ_opps))
        all_opportunities.extend(econ_opps)

    # Injury filter: drop series_game_edge opps where the team has a player OUT.
    # Uses ESPN's free injury API. Fails open — if ESPN is down, opp is NOT dropped.
    logger.info("INJURIES: checking injury reports for series game edges")
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
                        logger.warning("INJURIES: STALE %s — %s", opp.get('ticker', '?'),
                                       inj["warnings"][0] if inj["warnings"] else "injury detected")
                        continue  # drop from opportunities
                    if inj["warnings"]:
                        opp.setdefault("warnings", []).extend(inj["warnings"])
            checked_opps.append(opp)
        dropped_inj = pre_injury - len(checked_opps)
        if dropped_inj:
            logger.info("INJURIES: dropped %d STALE opportunities", dropped_inj)
        else:
            logger.debug("INJURIES: no injury-stale opportunities found")
        all_opportunities = checked_opps
    except Exception as e:
        logger.warning("INJURIES: check error (fail-open): %s", e)

    # Track every dropped candidate with reason for post-scan diagnosis
    dropped_log: list[dict] = []

    def _drop(opp: dict, reason: str) -> None:
        dropped_log.append({
            "ticker": opp.get("ticker", "?"),
            "type": opp.get("type", "?"),
            "edge_pct": round(opp.get("edge", 0) * 100, 1),
            "confidence": opp.get("confidence", 0),
            "reason": reason,
        })
        logger.debug("DROPPED %s (%s) edge=%.1f%% conf=%.2f — %s",
                     opp.get("ticker", "?"), opp.get("type", "?"),
                     opp.get("edge", 0) * 100, opp.get("confidence", 0), reason)

    # Strategy gate: drop any opp type not in ACTIVE_STRATEGIES.
    # This is the final enforcement — even if a scanner runs, it won't reach Tyler.
    active = []
    for o in all_opportunities:
        if o.get("type") in ACTIVE_STRATEGIES:
            active.append(o)
        else:
            _drop(o, f"inactive_strategy:{o.get('type')}")
    dropped_gate = len(all_opportunities) - len(active)
    all_opportunities = active
    if dropped_gate:
        logger.info("GATE: dropped %d opportunities from inactive strategies", dropped_gate)

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

    sane = []
    for o in all_opportunities:
        if _passes_sanity(o):
            sane.append(o)
        else:
            _drop(o, "failed_sanity_check")
    dropped_sanity = len(all_opportunities) - len(sane)
    all_opportunities = sane
    if dropped_sanity:
        logger.info("SANITY: dropped %d opportunity/ies with failed self-checks or near-zero values", dropped_sanity)

    # Apply B2B confidence penalty before ranking
    all_opportunities = [_apply_b2b_penalty(o) for o in all_opportunities]

    # Rank by EV (|edge| × confidence) — highest conviction bets surface first
    all_opportunities = _sort_by_ev(all_opportunities)

    # Annotate with price movement warnings (PriceMonitor)
    all_opportunities = _price_monitor.annotate_all(all_opportunities)

    # Determine next scan interval
    scan_interval = get_scan_interval(all_games)
    interval_label = {
        SCAN_INTERVAL_LIVE: "LIVE (2 min)",
        SCAN_INTERVAL_PREGAME: "PREGAME (10 min)",
        SCAN_INTERVAL_IDLE: "IDLE (30 min)",
    }.get(scan_interval, f"{scan_interval}s")

    logger.info("SUMMARY: %d opportunities | %d line moves | next scan: %s",
                len(all_opportunities), len(line_movements), interval_label)

    return {
        "opportunities": all_opportunities,
        "line_movements": line_movements,
        "games_scanned": len(all_games),
        "scan_interval": scan_interval,
        "dropped_candidates": dropped_log,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def morning_weather_scan() -> list[dict]:
    """
    Lightweight scan that only checks weather markets.
    Called by the morning briefing — no sports scan, no API usage.
    """
    logger.info("MORNING: weather-only scan")
    opportunities = scan_weather_markets()
    logger.info("MORNING: found %d weather opportunities", len(opportunities))
    return opportunities
