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
# Session 10: gate diagnostics
# ---------------------------------------------------------------------------

def _forecast_distance_from_bucket(forecast_temp: float, lo: float, hi: float) -> float:
    """Signed distance in degrees from a forecast to a contract bucket.

    Negative when the forecast is INSIDE [lo, hi] (magnitude = depth from
    nearest edge). Positive when the forecast is OUTSIDE the bucket
    (magnitude = gap from nearest edge). Used by the forecast_in_bucket
    reject log so cohort_report can distinguish "deep inside" from
    "just outside the ±2° margin".
    """
    if lo <= forecast_temp <= hi:
        return -min(forecast_temp - lo, hi - forecast_temp)
    return min(abs(forecast_temp - lo), abs(forecast_temp - hi))


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
# Vig Stack Series Scanner (structural NO edge — no external odds needed)
# ---------------------------------------------------------------------------

# Map Kalshi series ticker prefixes to NWS city keys for forecast filtering.
_SERIES_TO_NWS: dict[str, str] = {
    "KXHIGHNY":  "NYC",
    "KXHIGHAUS": "Austin",
    "KXHIGHCHI": "Chicago",
    "KXHIGHDEN": "Denver",
    "KXHIGHMIA": "Miami",
    "KXHIGHBOS": "Boston",
    "KXHIGHDC":  "DC",
    "KXHIGHSF":  "SF",
    "KXHIGHLA":  "LA",
    "KXHIGHSEA": "Seattle",
    "KXHIGHPHO": "Phoenix",
    "KXHIGHDAL": "Dallas",
    "KXHIGHATL": "Atlanta",
    "KXHIGHPHL": "Philadelphia",
    "KXHIGHLV":  "Las Vegas",
    "KXHIGHPDX": "Portland",
    "KXHIGHMIN": "Minneapolis",
    "KXHIGHNSH": "Nashville",
}


def _parse_weather_bucket(ticker: str) -> tuple[float, float] | None:
    """Parse a weather ticker into (low, high) temperature range.

    Examples:
        KXHIGHNY-26APR15-B89.5  → (89.0, 90.0)   "between" bucket
        KXHIGHCHI-26APR15-T73   → (73.0, 999.0)   "threshold" (≥73°F)

    Returns None if the ticker can't be parsed.
    """
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    bucket_part = parts[-1]  # e.g. "B89.5" or "T73"
    try:
        if bucket_part.startswith("B"):
            low = float(bucket_part[1:])
            # Kalshi "B89.5" means the range 89–90°F (integer boundaries)
            # low is the x.5 midpoint → bucket is int(low) to int(low)+1
            return (float(int(low)), float(int(low) + 1))
        elif bucket_part.startswith("T"):
            threshold = float(bucket_part[1:])
            return (threshold, 999.0)  # ≥ threshold
    except ValueError:
        pass
    return None


def _fetch_vig_stack_forecasts() -> dict[str, float]:
    """Fetch NWS forecasts for all weather series cities.

    Returns {series_ticker: forecast_high_F} for tomorrow's daytime high.
    Only includes cities where a forecast was successfully fetched.
    """
    import re
    forecasts: dict[str, float] = {}
    _now = datetime.now(timezone.utc)

    for series_ticker, nws_city in _SERIES_TO_NWS.items():
        if nws_city not in NWS_CITIES:
            continue
        lat, lon = NWS_CITIES[nws_city]
        fc = _fetch_nws_forecast(nws_city, lat, lon)
        if not fc:
            continue

        # Find the next daytime high (skip "Tonight" periods)
        for period in fc.get("periods", []):
            name = period.get("name", "").lower()
            if "night" in name:
                continue
            temp = period.get("temperature")
            if temp is not None:
                forecasts[series_ticker] = float(temp)
                break

    return forecasts


# Session 6 — vig_stack gate fingerprint for the per-decision audit log.
# Gate order matters: each rejection records all earlier gates as True
# (passed), the rejecting gate as False, downstream gates omitted.
_VIG_STACK_GATES = [
    "low_liquidity", "no_vig", "market_closed", "forecast_in_bucket",
    "no_price_too_low", "price_floor", "edge_below_threshold", "self_check",
]
_VIG_STACK_REASON_TO_GATE = {
    "low_liquidity": "low_liquidity",
    "no_vig": "no_vig",
    "market_closed": "market_closed",
    "forecast_in_bucket": "forecast_in_bucket",
    "no_price_too_low": "no_price_too_low",
    "no_price_below_floor": "price_floor",
    "non_stable_below_weather_floor": "price_floor",
    "edge_below_threshold": "edge_below_threshold",
    "self_check_failed": "self_check",
}


def _vig_stack_gate_fingerprint(reason: str) -> dict[str, bool]:
    gate = _VIG_STACK_REASON_TO_GATE.get(reason, reason)
    out: dict[str, bool] = {}
    for name in _VIG_STACK_GATES:
        if name == gate:
            out[name] = False
            return out
        out[name] = True
    return out


def _stratified_cf_rejects(
    rejected_opps: list[dict],
    *,
    per_gate_top_k: int = 1,
    total_budget: int = 10,
    hard_cap: int = 15,
) -> list[dict]:
    """Select which rejected opps get a counterfactual CLV record.

    Session 8 (Apr 24): replaces Session-6 global top-5 selection. That
    rule starved the gates we most need to retune — gates like
    edge_below_threshold and forecast_in_bucket reject low-edge opps by
    construction and so never won the top-5 race against
    non_stable_below_weather_floor (real 4-20¢ edges).

    Two-stage sampling:
      1. Stratified core — per (opp_type, skip_reason), take the
         per_gate_top_k highest-edge rejects. Guarantees every gate that
         fired gets ≥1 CF record.
      2. Budget fill — add highest-edge ungrouped rejects until total
         hits total_budget.

    Dedup by ticker (higher edge wins). Hard cap on final size keeps a
    pathological scan from ballooning clv.json.

    Eligibility filters (applied before grouping):
      - edge is not None (needed for ordering)
      - entry (price_cents or yes_ask) ≥ 3¢ (fc269f6 — keeps 1-2¢
        relative-edge blowups out of the sample)
    """
    eligible = [
        o for o in rejected_opps
        if o.get("edge") is not None
        and (o.get("price_cents") or o.get("yes_ask") or 0) >= 3
    ]
    if not eligible:
        return []

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for o in eligible:
        key = (
            o.get("opp_type") or o.get("type") or "",
            o.get("skip_reason") or "",
        )
        groups[key].append(o)

    core: list[dict] = []
    for grp in groups.values():
        grp.sort(key=lambda o: -o["edge"])
        core.extend(grp[:per_gate_top_k])

    seen: set[str] = set()
    selected: list[dict] = []
    for o in sorted(core, key=lambda o: -o["edge"]):
        t = o.get("ticker") or ""
        if t and t not in seen:
            seen.add(t)
            selected.append(o)

    for o in sorted(eligible, key=lambda o: -o["edge"]):
        if len(selected) >= total_budget:
            break
        t = o.get("ticker") or ""
        if t and t not in seen:
            seen.add(t)
            selected.append(o)

    return selected[:hard_cap]


def scan_vig_stack_series(scan_id: str | None = None,
                          on_market_seen=None) -> list[dict]:
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

    Session 12: optional `scan_id` and `on_market_seen` callback let
    `_main_loop` attribute every ticker this scanner touched to the matching
    universe.jsonl row (`scanned_by`). When called without these (Telegram
    `/scan` handler, tests), scan_id is generated locally for CF idempotency
    and the callback is a no-op.
    """
    from bot import decisions  # lazy import (circular safety)

    opportunities = []
    rejected_opps: list[dict] = []  # Session 6 — top-N → CF emission
    # Telemetry: count drops by reason per sub-type (series vs futures).
    # Lets us see in logs WHY dark scanners are silent.
    _telem: dict[str, dict[str, int]] = {
        "series": {"checked": 0, "surfaced": 0, "low_liquidity": 0, "no_vig": 0,
                   "market_closed": 0, "forecast_in_bucket": 0, "no_price_too_low": 0,
                   "no_price_below_floor": 0, "non_stable_below_weather_floor": 0,
                   "edge_below_threshold": 0, "self_check_failed": 0},
        "futures": {"checked": 0, "surfaced": 0, "low_liquidity": 0, "no_vig": 0,
                    "market_closed": 0, "forecast_in_bucket": 0, "no_price_too_low": 0,
                    "no_price_below_floor": 0, "non_stable_below_weather_floor": 0,
                    "edge_below_threshold": 0, "self_check_failed": 0},
    }

    # Weather series: filter CLOSED markets (observed temp → no vig edge left).
    # Old approach used UTC date matching, but that breaks across timezones:
    # at 9 PM ET (= Apr 15 UTC), Apr 15 weather markets got filtered even though
    # the temperature hasn't been observed yet (closes ~11 PM ET = Apr 16 UTC).
    # New approach: check close_time — if market hasn't closed, outcome is unknown.
    _now_utc = datetime.now(timezone.utc)
    _weather_set = set(WEATHER_SERIES_TICKERS)

    # Fetch NWS forecasts to avoid betting NO on the bucket the temp will
    # actually land in. Vig math gives structural edge, but forecast tells
    # us WHICH contracts are likely to lose. Skip those → higher win rate.
    _nws_forecasts = _fetch_vig_stack_forecasts()
    if _nws_forecasts:
        logger.info("VigStack NWS forecasts: %s",
                     ", ".join(f"{k.replace('KXHIGH','')}={v:.0f}°F"
                               for k, v in sorted(_nws_forecasts.items())))

    _futures_set = set(SPORTS_FUTURES_TICKERS)
    all_series = (list(WEATHER_SERIES_TICKERS) + list(INDEX_RANGE_SERIES_TICKERS)
                  + list(SPORTS_FUTURES_TICKERS))

    for series_ticker in all_series:
        is_weather = series_ticker in _weather_set
        is_futures = series_ticker in _futures_set

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

        # For index series, only count range (B) contracts — threshold (T) contracts overlap
        # Sports futures have no B/T prefix — each contract is a team, all mutually exclusive
        if not is_weather and not is_futures:
            markets = [m for m in markets if "-B" in m.get("ticker", "")]

        # Group by event (date/time) for index series — each event is a separate ladder
        # Weather and sports futures: all contracts in series are one ladder
        if not is_weather and not is_futures:
            by_event: dict[str, list] = {}
            for m in markets:
                parts = m.get("ticker", "").split("-")
                ev = "-".join(parts[:2]) if len(parts) >= 2 else "unknown"
                by_event.setdefault(ev, []).append(m)
            event_groups = list(by_event.values())
        else:
            event_groups = [markets]  # Weather/futures: all contracts are one ladder

        _sub = "futures" if is_futures else "series"
        _scanner_name = "vig_stack_futures" if is_futures else "vig_stack_series"
        for event_markets in event_groups:
            # Collect YES ask prices for all contracts in this event
            yes_asks = []
            valid_markets = []
            for m in event_markets:
                # Session 12: attribute every ticker this scanner looked at —
                # including low-liquidity / no-vig rejects below — so the
                # universe row's `scanned_by` reflects "active strategy
                # considered this," not just "passed all filters."
                if on_market_seen and scan_id:
                    on_market_seen(scan_id, m.get("ticker", ""), _scanner_name)
                ya = m.get("yes_ask")
                na = m.get("no_ask")
                if ya and ya > 0 and na and na > 0:
                    volume = m.get("volume") or 0
                    open_interest = m.get("open_interest") or 0
                    if volume < 10 and open_interest < 5:
                        _telem[_sub]["low_liquidity"] += 1
                        decisions.log_decision(
                            ticker=m.get("ticker", ""),
                            opp_type=("vig_stack_futures" if is_futures else "vig_stack_series"),
                            edge=None,
                            gates=_vig_stack_gate_fingerprint("low_liquidity"),
                            decision="reject",
                            reason="low_liquidity",
                            extra={"volume": volume, "open_interest": open_interest,
                                   "min_volume": 10, "min_open_interest": 5},
                        )
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
                _telem[_sub]["no_vig"] += len(valid_markets)
                decisions.log_decision(
                    ticker=valid_markets[0].get("ticker", "") if valid_markets else "",
                    opp_type=("vig_stack_futures" if is_futures else "vig_stack_series"),
                    edge=None,
                    gates=_vig_stack_gate_fingerprint("no_vig"),
                    decision="reject",
                    reason="no_vig",
                    extra={"yes_sum": yes_sum, "group_size": len(valid_markets)},
                )
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
                _telem[_sub]["checked"] += 1

                # Skip CLOSED weather contracts — once the temperature is
                # observed the prices snap to 0/100¢ and vig disappears.
                if is_weather:
                    close_str = market.get("close_time") or market.get("expiration_time", "")
                    if close_str:
                        try:
                            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                            if close_dt <= _now_utc:
                                _telem[_sub]["market_closed"] += 1
                                decisions.log_decision(
                                    ticker=ticker,
                                    opp_type=("vig_stack_futures" if is_futures else "vig_stack_series"),
                                    edge=None,
                                    gates=_vig_stack_gate_fingerprint("market_closed"),
                                    decision="reject",
                                    reason="market_closed",
                                )
                                continue  # Market closed — outcome known
                        except (ValueError, TypeError):
                            pass  # Can't parse — let it through, better to over-scan

                # NO fair value = 100¢ - (YES_ask adjusted for vig).
                # Session 8 (Apr 24): hoisted above the forecast_in_bucket
                # short-circuit so that forecast-rejected contracts can also
                # emit a counterfactual CLV record (previously their edge
                # was unknown at skip time and they were dropped from the
                # CF sample entirely).
                yes_fair_cents = yes_ask / vig_factor
                no_fair_cents = 100.0 - yes_fair_cents

                no_ask_prob = no_ask / 100.0
                no_fair_prob = no_fair_cents / 100.0
                no_edge = no_fair_prob - no_ask_prob
                relative_no_edge = no_edge / no_ask_prob if no_ask_prob > 0 else 0.0

                _opp_type_now = "vig_stack_futures" if is_futures else "vig_stack_series"

                def _build_reject_opp(reason: str) -> dict:
                    return {
                        "ticker": ticker,
                        "title": title,
                        "type": _opp_type_now,
                        "opp_type": _opp_type_now,
                        "side": "no",
                        "recommended_side": "no",
                        "price_cents": no_ask,
                        "fair_value_cents": round(no_fair_cents, 2),
                        "edge": round(relative_no_edge, 4),
                        "skip_reason": reason,
                    }

                # NWS FORECAST FILTER: skip NO bets where the forecast
                # says the temperature will land IN this bucket.
                if is_weather and series_ticker in _nws_forecasts:
                    forecast_temp = _nws_forecasts[series_ticker]
                    bucket = _parse_weather_bucket(ticker)
                    if bucket:
                        lo, hi = bucket
                        if lo - 2 <= forecast_temp <= hi + 2:
                            logger.info(
                                "VigStack FORECAST SKIP: %s — NWS=%s°F lands near "
                                "bucket %.0f–%.0f°F (±2° margin)",
                                ticker, forecast_temp, lo, hi,
                            )
                            _telem[_sub]["forecast_in_bucket"] += 1
                            decisions.log_decision(
                                ticker=ticker,
                                opp_type=_opp_type_now,
                                edge=round(relative_no_edge, 4),
                                gates=_vig_stack_gate_fingerprint("forecast_in_bucket"),
                                decision="reject",
                                reason="forecast_in_bucket",
                                extra={"forecast_temp": forecast_temp,
                                       "bucket_lo": lo, "bucket_hi": hi,
                                       "distance": round(_forecast_distance_from_bucket(forecast_temp, lo, hi), 2)},
                            )
                            rejected_opps.append(_build_reject_opp("forecast_in_bucket"))
                            continue

                # Skip near-zero-price NO contracts (YES near-certain)
                if no_ask_prob < 0.03:
                    _telem[_sub]["no_price_too_low"] += 1
                    decisions.log_decision(
                        ticker=ticker, opp_type=_opp_type_now,
                        edge=round(relative_no_edge, 4),
                        gates=_vig_stack_gate_fingerprint("no_price_too_low"),
                        decision="reject", reason="no_price_too_low",
                        extra={"no_ask_prob": round(no_ask_prob, 4)},
                    )
                    rejected_opps.append(_build_reject_opp("no_price_too_low"))
                    continue

                # Family-aware NO entry price floor (Apr 18 evening, data-driven).
                # Stable-climate + financial families (MIA/AUS/INX) enter at the
                # standard 0.70 floor — their forecast-vs-actual distributions
                # are tight (83% WR below 0.90 in-cohort). Volatile weather
                # families (DEN/CHI/NY/etc) must meet the stricter 0.90 floor —
                # sub-0.90 rungs there are 5W/13L (28% WR, -$129.79) while
                # 0.90+ are 7W/0L. See config.VIG_STACK_STABLE_FAMILIES and
                # VIG_STACK_WEATHER_MIN_PRICE for full evidence.
                _fam = ticker.split("-")[0] if ticker else ""
                if _fam in VIG_STACK_STABLE_FAMILIES:
                    if no_ask_prob < VIG_STACK_MIN_NO_ENTRY_PRICE:
                        _telem[_sub]["no_price_below_floor"] += 1
                        decisions.log_decision(
                            ticker=ticker, opp_type=_opp_type_now,
                            edge=round(relative_no_edge, 4),
                            gates=_vig_stack_gate_fingerprint("no_price_below_floor"),
                            decision="reject", reason="no_price_below_floor",
                            extra={"no_ask_prob": round(no_ask_prob, 4),
                                   "floor": VIG_STACK_MIN_NO_ENTRY_PRICE,
                                   "family": _fam},
                        )
                        rejected_opps.append(_build_reject_opp("no_price_below_floor"))
                        continue
                else:
                    if no_ask_prob < VIG_STACK_WEATHER_MIN_PRICE:
                        _telem[_sub]["non_stable_below_weather_floor"] += 1
                        decisions.log_decision(
                            ticker=ticker, opp_type=_opp_type_now,
                            edge=round(relative_no_edge, 4),
                            gates=_vig_stack_gate_fingerprint("non_stable_below_weather_floor"),
                            decision="reject", reason="non_stable_below_weather_floor",
                            extra={"no_ask_prob": round(no_ask_prob, 4),
                                   "floor": VIG_STACK_WEATHER_MIN_PRICE,
                                   "family": _fam},
                        )
                        rejected_opps.append(_build_reject_opp("non_stable_below_weather_floor"))
                        continue

                # Vig stack series edge is purely structural (no prediction risk).
                VIG_STACK_MIN_EDGE = 0.02
                if relative_no_edge < VIG_STACK_MIN_EDGE:
                    _telem[_sub]["edge_below_threshold"] += 1
                    _close_str = market.get("close_time") or market.get("expiration_time", "")
                    _tts_hr = None
                    if _close_str:
                        try:
                            _close_dt = datetime.fromisoformat(_close_str.replace("Z", "+00:00"))
                            _tts_hr = round((_close_dt - _now_utc).total_seconds() / 3600.0, 2)
                        except (ValueError, TypeError):
                            _tts_hr = None
                    decisions.log_decision(
                        ticker=ticker, opp_type=_opp_type_now,
                        edge=round(relative_no_edge, 4),
                        gates=_vig_stack_gate_fingerprint("edge_below_threshold"),
                        decision="reject", reason="edge_below_threshold",
                        extra={"min_edge": VIG_STACK_MIN_EDGE,
                               "edge": round(relative_no_edge, 4),
                               "vig": round(yes_sum - 100.0, 2),
                               "time_to_settle_hr": _tts_hr},
                    )
                    rejected_opps.append(_build_reject_opp("edge_below_threshold"))
                    continue

                # Self-checks
                check_ok, check_msg = _self_check_edge(no_fair_prob, no_ask_prob, no_edge)
                if not check_ok:
                    _telem[_sub]["self_check_failed"] += 1
                    decisions.log_decision(
                        ticker=ticker, opp_type=_opp_type_now,
                        edge=round(relative_no_edge, 4),
                        gates=_vig_stack_gate_fingerprint("self_check_failed"),
                        decision="reject", reason="self_check_failed",
                        extra={"check_msg": check_msg[:100] if check_msg else ""},
                    )
                    rejected_opps.append(_build_reject_opp("self_check_failed"))
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

                # Sports futures: slightly lower confidence (months to resolve,
                # capital tied up longer) and tag as sub-type for reporting
                _conf = 0.85 if is_futures else 0.90
                _type = "vig_stack_futures" if is_futures else "vig_stack_series"

                _telem[_sub]["surfaced"] += 1
                decisions.log_decision(
                    ticker=ticker, opp_type=_type,
                    edge=round(relative_no_edge, 4),
                    gates={g: True for g in _VIG_STACK_GATES},
                    decision="accept", reason="all_gates_passed",
                    extra={"no_ask": no_ask, "no_fair_cents": round(no_fair_cents, 2)},
                )
                opportunities.append({
                    "type": _type,
                    "ticker": ticker,
                    "title": title,
                    "market": market,
                    "series_ticker": series_ticker,
                    "edge": round(no_edge, 4),
                    "relative_edge": round(relative_no_edge, 4),
                    "confidence": _conf,
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
                        "confidence": _conf,
                        "self_check_passed": True,
                        "math_chain": math_chain,
                        "warnings": [],
                    },
                })

    # Report telemetry: one info line per sub-type. Critical for diagnosing
    # WHY vig_stack_futures never fires despite being in ACTIVE_STRATEGIES.
    for sub, t in _telem.items():
        if t["checked"] == 0 and t["surfaced"] == 0:
            continue  # nothing was scanned for this sub-type; skip quietly
        drops = {k: v for k, v in t.items() if k not in ("checked", "surfaced") and v > 0}
        logger.info("VIG_STACK_TELEMETRY/%s: checked=%d surfaced=%d drops=%s",
                    sub, t["checked"], t["surfaced"], drops or "none")

    # Session 8 — stratified counterfactual emission. Every (opp_type,
    # skip_reason) pair that fired gets ≥1 CF record; remaining budget
    # fills with highest-edge leftovers. Replaces Session-6 global top-5,
    # which starved low-edge-by-design gates like edge_below_threshold
    # and forecast_in_bucket (first 24h: 0 CFs for 387 rejects across
    # those two gates vs. 29 CFs for 19 rejects on the high-edge
    # non_stable_below_weather_floor gate).
    if rejected_opps:
        try:
            from bot import clv
            # Session 12: scan_id may be hoisted from _main_loop (so universe
            # rows can be joined to CF/prediction records on (scan_id, ticker)).
            # Fall back to local generation when called from Telegram handlers
            # / tests that don't pass it.
            scan_id = scan_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            for opp in _stratified_cf_rejects(rejected_opps):
                try:
                    clv.record_counterfactual_skip(opp, opp["skip_reason"], scan_id)
                except Exception:
                    logger.exception("CF emit failed for %s", opp.get("ticker"))
        except Exception:
            logger.exception("Stratified CF selection failed")

    return opportunities


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
