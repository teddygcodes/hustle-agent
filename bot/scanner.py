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
    WEATHER_SERIES_TICKERS, INDEX_RANGE_SERIES_TICKERS, ACTIVE_STRATEGIES,
    SPORTS_FUTURES_TICKERS, VIG_STACK_MIN_NO_ENTRY_PRICE,
    VIG_STACK_STABLE_FAMILIES, VIG_STACK_WEATHER_MIN_PRICE,
    VIG_STACK_MAX_RUNGS_PER_LADDER, POSITIONS_FILE,
)
from bot.math_engine import _self_check_edge
from bot.kalshi_series import scan_series_markets
from bot.scanner_weather import _fetch_nws_forecast, NWS_CITIES, _get_forecast_temp_for_date
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
# Session 10: gate diagnostics — moved to bot.strategies.vig_stack_series in
# Session 13a (Apr 25). Re-exported here so existing tests + tools that
# imported from bot.scanner keep working.
# ---------------------------------------------------------------------------

from bot.strategies.vig_stack_series import (  # noqa: E402,F401
    _forecast_distance_from_bucket,
    _stratified_cf_rejects,
)


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


def _ladder_event_key(ticker: str) -> str:
    """Collapse a market ticker to its ladder-event identifier.

    KXHIGHDEN-26APR17-B57.5   → KXHIGHDEN-26APR17
    KXINX-26APR17H1600-B7062  → KXINX-26APR17H1600
    Tickers with fewer than two hyphen segments are returned unchanged.
    """
    parts = (ticker or "").split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else (ticker or "")


def _cap_ladder_rungs(vig_stack_opps: list[dict]) -> list[dict]:
    """
    Enforce VIG_STACK_MAX_RUNGS_PER_LADDER across scans by counting already-
    open positions on the same ladder-event.

    Motivation (Apr 18, data-driven): on Apr 17 the KXHIGHDEN ladder
    accumulated 6 rungs across 3 separate scans; the actual Denver high
    zeroed all 6 simultaneously for −$70.35. The within-scan contract
    splitter (_cap_correlated_vig_stack) can't see positions already opened
    on earlier scans, so cross-scan concentration was unbounded.

    Algorithm:
      1. Read open vig_stack positions from POSITIONS_FILE; count per ladder.
      2. For each ladder group in the new opps, keep only enough
         (highest-relative_edge first) to stay at or below the cap.
      3. Log how many were dropped per ladder.
    """
    if not vig_stack_opps:
        return vig_stack_opps

    cap = VIG_STACK_MAX_RUNGS_PER_LADDER
    if cap <= 0:
        return vig_stack_opps

    # Count already-open vig_stack positions per ladder-event.
    open_by_ladder: dict[str, int] = defaultdict(int)
    try:
        import json
        with open(POSITIONS_FILE) as fh:
            positions = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        positions = []

    if isinstance(positions, list):
        for p in positions:
            if not isinstance(p, dict):
                continue
            if p.get("filled", 0) <= 0:
                continue
            if p.get("status") not in ("filled", "partial"):
                continue
            ptype = p.get("type") or p.get("opp_type") or ""
            if "vig_stack" not in ptype:
                continue
            open_by_ladder[_ladder_event_key(p.get("ticker", ""))] += 1

    # Group new opps by ladder, sort within each by relative_edge desc so
    # the best signals survive the cap.
    by_ladder: dict[str, list[dict]] = defaultdict(list)
    for opp in vig_stack_opps:
        by_ladder[_ladder_event_key(opp.get("ticker", ""))].append(opp)

    kept: list[dict] = []
    dropped_by_ladder: dict[str, int] = {}
    for ladder, opps in by_ladder.items():
        opps_sorted = sorted(opps, key=lambda o: o.get("relative_edge", 0), reverse=True)
        slots = max(0, cap - open_by_ladder.get(ladder, 0))
        if slots >= len(opps_sorted):
            kept.extend(opps_sorted)
        else:
            kept.extend(opps_sorted[:slots])
            dropped_by_ladder[ladder] = len(opps_sorted) - slots

    if dropped_by_ladder:
        total = sum(dropped_by_ladder.values())
        logger.info(
            "LADDER_CAP: dropped %d vig_stack opps (cap=%d per ladder-event) — %s",
            total, cap,
            ", ".join(f"{k}:{v}" for k, v in sorted(dropped_by_ladder.items())),
        )

    return kept


# ---------------------------------------------------------------------------
# Main scan cycle
# ---------------------------------------------------------------------------

def scan_cycle(sports: Optional[list[str]] = None,
               scan_id: str | None = None,
               on_market_seen=None,
               universe=None) -> dict:
    """
    Run a full scan cycle across all active sports and weather markets.

    Args:
        sports: Optional sports filter (defaults to ACTIVE_SPORTS).
        scan_id: Session 12 — hoisted from `_main_loop` so universe.jsonl
            rows can be joined to decisions/predictions/CF records on
            `(scan_id, ticker)`. When `None` (Telegram `/scan` handler,
            tests), each scanner falls back to local generation.
        on_market_seen: Session 12 — `bot.universe.on_market_seen` callback,
            wired through so each scanner can attribute every ticker it
            evaluates. `None` makes the callback a no-op for non-loop
            callers that don't snapshot the universe.
        universe: Session 13a — the buffered Kalshi universe (list of
            Market dataclasses) collected by main.py before scan_cycle
            runs. Strategy classes operate on this data without
            re-fetching from Kalshi. When `None` and `scan_id` is
            provided, scan_cycle pulls from `bot.universe.get_buffered_markets`;
            when both are `None` (Telegram /scan, tests), the strategy
            slice is skipped (no vig_stack opps for those callers).

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

    # Scan vig stack series via the Strategy contract (Session 13a).
    # main.py snapshots the universe before scan_cycle and passes it in;
    # Telegram /scan and direct tests don't, so the strategy slice no-ops
    # for those paths.
    logger.info("VIG_STACK: scanning series ladders for structural NO edges")
    if universe is None and scan_id is not None:
        try:
            from bot import universe as _universe_mod
            universe = _universe_mod.get_buffered_markets(scan_id)
        except Exception:
            logger.exception("VIG_STACK: get_buffered_markets failed; skipping")
            universe = []
    universe = universe or []
    vig_stack_opps: list[dict] = []
    if universe:
        from bot.strategies import REGISTERED_STRATEGIES
        for _strategy in REGISTERED_STRATEGIES:
            candidates = _strategy.candidate_markets(universe)
            for _m in candidates:
                if on_market_seen and scan_id:
                    on_market_seen(scan_id, _m.ticker, _strategy.name_for(_m))
                _opp = _strategy.evaluate(_m)
                if _opp is not None:
                    vig_stack_opps.append(_opp)
            _strategy.finalize(scan_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    else:
        logger.info("VIG_STACK: skipped — no buffered universe (Telegram /scan or test path)")
    if _weather_yes_tickers:
        _before = len(vig_stack_opps)
        vig_stack_opps = [o for o in vig_stack_opps if o["ticker"] not in _weather_yes_tickers]
        _suppressed = _before - len(vig_stack_opps)
        if _suppressed:
            logger.info("VIG_STACK: suppressed %d signal(s) — weather model recommends YES on same ticker", _suppressed)
    # Cap correlated vig stack signals sharing the same series prefix
    vig_stack_opps = _cap_correlated_vig_stack(vig_stack_opps)
    # Cap per-ladder-event rung count (reads already-open positions;
    # prevents cross-scan concentration like the Apr 17 DEN 6-rung wipe).
    vig_stack_opps = _cap_ladder_rungs(vig_stack_opps)
    all_opportunities.extend(vig_stack_opps)
    logger.info("VIG_STACK: found %d structural vig stack opportunities", len(vig_stack_opps))

    # Scan cross-market sports arb (monotonicity, championship≤series, high-vig games)
    # Always runs — these are structural arbs, no strategy flag needed
    logger.info("SPORTS_ARB: scanning cross-market consistency")
    try:
        from bot.scanner_sports_arb import scan_sports_arb
        sports_arb_opps = scan_sports_arb(scan_id=scan_id, on_market_seen=on_market_seen)
        all_opportunities.extend(sports_arb_opps)
        logger.info("SPORTS_ARB: found %d total cross-market opportunities", len(sports_arb_opps))
    except Exception as e:
        logger.warning("SPORTS_ARB: error: %s", e)

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
        SCAN_INTERVAL_IDLE: "IDLE (15 min)",
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
