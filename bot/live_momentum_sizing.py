"""Shared live_momentum sizing helpers.

Session 108: keep the counterfactual shadow-row sizing path on the same math
as the real live watcher entry path, without placing orders or mutating state.
"""
from __future__ import annotations

import math
from typing import Any

from bot.config import (
    CONVICTION_SIZE_FACTOR,
    MOMENTUM_SCALE_LARGE_DIP,
    MOMENTUM_SCALE_MED_DIP,
    MOMENTUM_SCALE_SMALL_DIP,
)
from bot.game_context import SportInstincts


def live_momentum_dip_size_multiplier(
    dip_cents: float,
    sport_profile: dict | None,
) -> float:
    """Scale live_momentum size by dip size using the sport's min dip."""
    profile = sport_profile or {}
    min_dip = profile.get("min_dip", 4)
    if dip_cents >= min_dip + 6:
        return MOMENTUM_SCALE_LARGE_DIP
    if dip_cents >= min_dip + 3:
        return MOMENTUM_SCALE_MED_DIP
    return MOMENTUM_SCALE_SMALL_DIP


def size_live_momentum_entry(
    *,
    price_cents: int | float | None,
    dip_cents: int | float | None,
    sport: str | None,
    balance: float | None,
    game_ctx: Any = None,
    espn_data: dict | None = None,
    sport_profile: dict | None = None,
    conviction: bool = False,
) -> dict:
    """Return the live_momentum sizing package without side effects.

    The returned `contracts` is only populated when the real entry path would
    have a positive contract count. Unavailable/unsized cases carry explicit
    diagnostics for shadow rows.
    """
    missing = []
    if price_cents is None:
        missing.append("price_cents")
    if dip_cents is None:
        missing.append("dip_cents")
    if balance is None:
        missing.append("balance")
    if missing:
        return {
            "contracts": None,
            "sizing": None,
            "missing_sizing_fields": missing,
            "sizing_unavailable_reason": "missing_inputs",
        }

    try:
        price = int(price_cents)
        dip = float(dip_cents)
        bal = float(balance)
    except (TypeError, ValueError):
        return {
            "contracts": None,
            "sizing": None,
            "missing_sizing_fields": ["numeric_inputs"],
            "sizing_unavailable_reason": "invalid_inputs",
        }

    fair_prob_source = "fallback_price_plus_dip"
    if game_ctx is not None:
        try:
            if game_ctx._snapshots and game_ctx.win_probability > 0:
                fair_prob = min(0.95, game_ctx.win_probability)
                fair_prob_source = "game_context"
            else:
                fair_prob = min(0.95, (price + dip) / 100.0)
        except Exception:
            fair_prob = min(0.95, (price + dip) / 100.0)
    else:
        fair_prob = min(0.95, (price + dip) / 100.0)

    assumed_edge = dip / 100.0
    from bot.sizing import kelly_size

    sizing = kelly_size(
        edge=assumed_edge,
        probability=fair_prob,
        balance=bal,
        price_cents=price,
        confidence=0.80,
        sport=sport,
    )
    if sizing["contracts"] <= 0:
        return {
            "contracts": None,
            "sizing": sizing,
            "fair_prob": fair_prob,
            "fair_prob_source": fair_prob_source,
            "assumed_edge": assumed_edge,
            "missing_sizing_fields": [],
            "sizing_unavailable_reason": sizing.get("reason") or "unsized",
        }

    size_mult = live_momentum_dip_size_multiplier(dip, sport_profile)
    scaled_contracts = max(1, math.floor(sizing["contracts"] * size_mult))
    if conviction:
        scaled_contracts = max(1, int(scaled_contracts * CONVICTION_SIZE_FACTOR))
    post_conviction_contracts = scaled_contracts

    bet_instincts = SportInstincts.detect(game_ctx, espn_data, sport or "")
    if bet_instincts.should_reduce_size:
        scaled_contracts = max(1, scaled_contracts // 2)

    max_contracts = (sport_profile or {}).get("max_contracts")
    capped_by_sport = False
    uncapped_contracts = scaled_contracts
    if max_contracts and scaled_contracts > max_contracts:
        scaled_contracts = max_contracts
        capped_by_sport = True

    sizing = dict(sizing)
    sizing["contracts"] = scaled_contracts
    sizing["total_cost"] = round(scaled_contracts * price / 100.0, 2)
    sizing["max_payout"] = round(scaled_contracts * 1.0, 2)

    return {
        "contracts": scaled_contracts,
        "sizing": sizing,
        "fair_prob": fair_prob,
        "fair_prob_source": fair_prob_source,
        "assumed_edge": assumed_edge,
        "size_multiplier": size_mult,
        "post_conviction_contracts": post_conviction_contracts,
        "uncapped_contracts": uncapped_contracts,
        "instincts": bet_instincts.flags if bet_instincts.flags else [],
        "capped_by_sport": capped_by_sport,
        "missing_sizing_fields": [],
        "sizing_unavailable_reason": None,
    }
