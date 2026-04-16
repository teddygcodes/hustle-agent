"""
Nexus Trading Bot — Position Monitor

Re-evaluates open positions each cycle:
1. Edge health check — is the original edge still alive?
2. Related market scan — find other props on the same event.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path

from agent.kalshi_client import get_market, get_markets
from bot.config import POSITIONS_FILE, MIN_RELATIVE_EDGE
from bot.state_io import load_json as _load_json, save_json as _save_json

logger = logging.getLogger("nexus.position_monitor")

# ---------------------------------------------------------------------------
# Edge re-check thresholds
# ---------------------------------------------------------------------------

# If the current edge is worse than this fraction of the entry edge,
# warn that the trade is degraded.  e.g. 0.25 means edge dropped to 25%
# of its original value.
_EDGE_WARN_RATIO = 0.25

# If edge has fully flipped (we'd now take the other side), flag for exit.
_EDGE_FLIP_EXIT = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_event_ticker(ticker: str) -> str | None:
    """Fetch event_ticker for a market from Kalshi API (cached on position)."""
    market = get_market(ticker)
    if "error" in market:
        return None
    return market.get("event_ticker")


def _recalc_crypto_edge(pos: dict, market: dict) -> dict | None:
    """Recalculate edge for a crypto position using current spot + vol."""
    from bot.kalshi_series import (
        _get_generic_crypto_spot, _get_generic_crypto_vol,
        _extract_crypto_threshold, CRYPTO_ASSETS_CONFIG,
    )

    opp_type = pos.get("opp_type", "")
    # Map opp_type back to asset_id
    asset_map = {v[4]: k for k, v in CRYPTO_ASSETS_CONFIG.items()}
    asset_id = asset_map.get(opp_type)
    if not asset_id:
        return None

    spot = _get_generic_crypto_spot(asset_id)
    if not spot:
        return None

    ticker = pos.get("ticker", "")
    threshold = _extract_crypto_threshold(ticker)
    if threshold is None:
        return None

    fallback_vol, cap_low, cap_high, _, _ = CRYPTO_ASSETS_CONFIG[asset_id]
    vol = _get_generic_crypto_vol(asset_id, fallback_vol, cap_low, cap_high)

    close_time = market.get("close_time") or market.get("expiration_time")
    if not close_time:
        return None
    try:
        close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except Exception:
        return None

    hours_remaining = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
    if hours_remaining <= 0:
        return None

    scaled_vol = vol * math.sqrt(hours_remaining / 24.0)
    if scaled_vol <= 0:
        return None

    log_ratio = math.log(spot / threshold)
    z = log_ratio / scaled_vol
    fair_value = 0.5 * math.erfc(-z / math.sqrt(2))

    yes_ask = market.get("yes_ask") or 0
    kalshi_price = yes_ask / 100.0 if yes_ask > 0 else 0
    if kalshi_price <= 0:
        return None

    edge = fair_value - kalshi_price
    relative_edge = edge / kalshi_price if kalshi_price > 0 else 0

    return {
        "fair_value": round(fair_value, 4),
        "kalshi_price": round(kalshi_price, 4),
        "edge": round(edge, 4),
        "relative_edge": round(relative_edge, 4),
        "spot": round(spot, 2),
        "threshold": threshold,
        "hours_remaining": round(hours_remaining, 2),
    }


def _recalc_sports_edge(pos: dict, market: dict) -> dict | None:
    """Recalculate edge for a sports position using current book odds."""
    from bot.kalshi_series import _ODDS_API_GAME_MAP

    sport = pos.get("sport", "")
    canonical = (pos.get("canonical_team") or "").lower()
    if not sport or not canonical:
        return None

    game_data = _ODDS_API_GAME_MAP.get(sport, {}).get(canonical)
    if not game_data:
        return None

    fair_value = game_data.get("consensus_prob", 0)
    if fair_value <= 0:
        return None

    yes_ask = market.get("yes_ask") or 0
    kalshi_price = yes_ask / 100.0 if yes_ask > 0 else 0
    if kalshi_price <= 0:
        return None

    edge = fair_value - kalshi_price
    relative_edge = edge / kalshi_price if kalshi_price > 0 else 0

    return {
        "fair_value": round(fair_value, 4),
        "kalshi_price": round(kalshi_price, 4),
        "edge": round(edge, 4),
        "relative_edge": round(relative_edge, 4),
    }


def _recalc_weather_edge(pos: dict, market: dict) -> dict | None:
    """Recalculate edge for a weather position using current NWS data."""
    # Weather edge recalc requires NWS forecast — too heavy for per-position check.
    # Fall back to price-based inference.
    return None


def _recalc_vig_stack_edge(pos: dict, market: dict) -> dict | None:
    """Recalculate edge for a vig stack position using current series prices."""
    series_ticker = market.get("series_ticker")
    if not series_ticker:
        return None

    result = get_markets(series_ticker=series_ticker, status="open", limit=100)
    if "error" in result:
        return None

    markets = result.get("markets", [])
    yes_asks = []
    for m in markets:
        ya = m.get("yes_ask")
        if ya and ya > 0:
            yes_asks.append(ya)

    if len(yes_asks) < 2:
        return None

    yes_sum = sum(yes_asks)
    vig_factor = yes_sum / 100.0
    if vig_factor < 1.05:
        return None

    this_yes_ask = market.get("yes_ask") or 0
    if this_yes_ask <= 0:
        return None

    yes_fair_cents = this_yes_ask / vig_factor
    no_fair_cents = 100.0 - yes_fair_cents
    no_ask = market.get("no_ask") or 0
    if no_ask <= 0:
        return None

    no_fair_prob = no_fair_cents / 100.0
    no_ask_prob = no_ask / 100.0
    edge = no_fair_prob - no_ask_prob
    relative_edge = edge / no_ask_prob if no_ask_prob > 0 else 0

    return {
        "fair_value": round(no_fair_prob, 4),
        "kalshi_price": round(no_ask_prob, 4),
        "edge": round(edge, 4),
        "relative_edge": round(relative_edge, 4),
    }


# ---------------------------------------------------------------------------
# Edge health check
# ---------------------------------------------------------------------------

def recheck_open_edges() -> list[dict]:
    """
    Re-evaluate edge on all open positions.

    For each position, recalculate the current edge using live data and
    compare to the entry edge. Returns a list of alert dicts for positions
    where edge has degraded or flipped.

    Alert types:
      - edge_degraded: edge dropped to <25% of entry value
      - edge_flipped:  edge switched sign (we'd now bet the other side)
      - edge_healthy:  edge is still good (logged at DEBUG level)
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return []

    alerts = []

    for pos in positions:
        if pos.get("status") not in ("filled", "partial"):
            continue

        ticker = pos.get("ticker", "")
        opp_type = pos.get("opp_type", "")
        entry_edge = pos.get("relative_edge", 0)
        side = pos.get("side", "yes")

        # Fetch current market data
        market = get_market(ticker)
        if "error" in market:
            continue

        # Skip settled/closed markets
        if market.get("status") in ("settled", "finalized", "closed"):
            continue

        # Recalculate edge based on position type
        recalc = None
        crypto_types = {
            "btc_price_edge", "eth_price_edge", "sol_price_edge",
            "xrp_price_edge", "doge_price_edge", "bnb_price_edge",
            "hype_price_edge",
        }

        if opp_type in crypto_types:
            recalc = _recalc_crypto_edge(pos, market)
        elif opp_type == "series_game_edge":
            recalc = _recalc_sports_edge(pos, market)
        elif opp_type in ("vig_stack_series", "vig_stack_no"):
            recalc = _recalc_vig_stack_edge(pos, market)
        elif opp_type == "weather":
            recalc = _recalc_weather_edge(pos, market)

        if not recalc:
            # Can't recalculate — use price movement as proxy
            entry_price = pos.get("price_cents", 0) / 100.0
            if side == "yes":
                current_price = (market.get("yes_ask") or 0) / 100.0
            else:
                current_price = (market.get("no_ask") or 0) / 100.0

            if entry_price <= 0 or current_price <= 0:
                continue

            # If price moved against us more than original edge, edge is likely gone
            if side == "yes":
                price_move = current_price - entry_price
            else:
                price_move = entry_price - current_price

            if abs(entry_edge) > 0 and price_move < -abs(entry_edge):
                alerts.append({
                    "type": "edge_degraded",
                    "ticker": ticker,
                    "title": pos.get("title", ""),
                    "opp_type": opp_type,
                    "entry_edge": entry_edge,
                    "price_move": round(price_move, 4),
                    "reason": "price_proxy",
                })
                logger.warning(
                    "EDGE DEGRADED (price proxy): %s — entry_edge=%.1f%% price_moved=%.1f%%",
                    ticker, entry_edge * 100, price_move * 100,
                )
            continue

        # We have a real recalculated edge
        new_edge = recalc["relative_edge"]
        new_abs_edge = recalc["edge"]

        # Store recalculated edge on position for tracking
        pos["current_edge"] = new_abs_edge
        pos["current_relative_edge"] = new_edge
        pos["edge_rechecked_at"] = datetime.now(timezone.utc).isoformat()
        if "fair_value" in recalc:
            pos["current_fair_value"] = recalc["fair_value"]

        # Check if edge has flipped direction
        entry_favors_yes = (entry_edge > 0) if side == "yes" else (entry_edge < 0)
        now_favors_yes = (new_edge > 0)

        if side == "yes" and new_edge < 0:
            # We bought YES but edge now says NO
            alerts.append({
                "type": "edge_flipped",
                "ticker": ticker,
                "title": pos.get("title", ""),
                "opp_type": opp_type,
                "side": side,
                "entry_edge": entry_edge,
                "current_edge": new_edge,
                "recalc": recalc,
            })
            logger.warning(
                "EDGE FLIPPED: %s — was YES +%.1f%%, now %.1f%% (favors NO)",
                ticker, entry_edge * 100, new_edge * 100,
            )
        elif side == "no" and new_edge > 0:
            # We bought NO but edge now says YES
            alerts.append({
                "type": "edge_flipped",
                "ticker": ticker,
                "title": pos.get("title", ""),
                "opp_type": opp_type,
                "side": side,
                "entry_edge": entry_edge,
                "current_edge": new_edge,
                "recalc": recalc,
            })
            logger.warning(
                "EDGE FLIPPED: %s — was NO, now +%.1f%% (favors YES)",
                ticker, new_edge * 100,
            )
        elif abs(entry_edge) > 0 and abs(new_edge) < abs(entry_edge) * _EDGE_WARN_RATIO:
            # Edge degraded to <25% of entry
            alerts.append({
                "type": "edge_degraded",
                "ticker": ticker,
                "title": pos.get("title", ""),
                "opp_type": opp_type,
                "side": side,
                "entry_edge": entry_edge,
                "current_edge": new_edge,
                "recalc": recalc,
            })
            logger.warning(
                "EDGE DEGRADED: %s — was %.1f%%, now %.1f%% (<25%% of entry)",
                ticker, entry_edge * 100, new_edge * 100,
            )
        else:
            logger.debug(
                "EDGE HEALTHY: %s — entry=%.1f%% current=%.1f%%",
                ticker, entry_edge * 100, new_edge * 100,
            )

    # Save updated edge data back to positions
    _save_json(POSITIONS_FILE, positions)
    return alerts


# ---------------------------------------------------------------------------
# Related market discovery
# ---------------------------------------------------------------------------

def scan_related_markets() -> list[dict]:
    """
    For each open position, find sibling markets on the same event
    that we don't already hold. Returns a list of related-market
    opportunities that passed basic edge checks.

    Uses Kalshi's event_ticker to group markets by event.
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return []

    open_positions = [
        p for p in positions
        if p.get("status") in ("filled", "partial")
    ]
    if not open_positions:
        return []

    # Collect tickers we already hold
    held_tickers = {p.get("ticker") for p in open_positions}

    # Group positions by event_ticker (fetch if missing)
    event_positions: dict[str, list[dict]] = {}
    for pos in open_positions:
        event_tk = pos.get("event_ticker")
        if not event_tk:
            # Fetch and cache on the position
            event_tk = _get_event_ticker(pos.get("ticker", ""))
            if event_tk:
                pos["event_ticker"] = event_tk

        if event_tk:
            event_positions.setdefault(event_tk, []).append(pos)

    # Save back event_tickers we fetched
    _save_json(POSITIONS_FILE, positions)

    if not event_positions:
        return []

    related_opps = []
    seen_tickers = set()

    for event_tk, event_posns in event_positions.items():
        # Fetch all markets in this event
        result = get_markets(event_ticker=event_tk, status="open", limit=50)
        if "error" in result:
            continue

        sibling_markets = result.get("markets", [])
        representative = event_posns[0]  # use first position for context

        for market in sibling_markets:
            ticker = market.get("ticker", "")

            # Skip markets we already hold or already processed
            if ticker in held_tickers or ticker in seen_tickers:
                continue
            seen_tickers.add(ticker)

            # Basic viability check
            yes_ask = market.get("yes_ask") or 0
            no_ask = market.get("no_ask") or 0
            if yes_ask <= 0 or no_ask <= 0:
                continue

            # Skip near-certain or near-zero markets
            kalshi_price = yes_ask / 100.0
            if kalshi_price < 0.05 or kalshi_price > 0.95:
                continue

            title = market.get("title", "")
            related_opps.append({
                "type": "related_market",
                "source_type": representative.get("opp_type", ""),
                "source_ticker": representative.get("ticker", ""),
                "event_ticker": event_tk,
                "ticker": ticker,
                "title": title,
                "market": market,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "sport": representative.get("sport", ""),
                "canonical_team": representative.get("canonical_team", ""),
            })

    if related_opps:
        logger.info(
            "RELATED: found %d sibling markets across %d events",
            len(related_opps), len(event_positions),
        )
    else:
        logger.debug("RELATED: no actionable sibling markets found")

    return related_opps
