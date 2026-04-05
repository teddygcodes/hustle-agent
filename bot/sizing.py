"""
Nexus Trading Bot — Kelly Criterion Bet Sizing

Fractional Kelly with hard caps. Conservative by default.
"""

import math
from bot.config import KELLY_FRACTION, MAX_BET_FRACTION, MIN_BET_DOLLARS


def kelly_size(
    edge: float,
    probability: float,
    balance: float,
    price_cents: int,
    max_fraction: float = MAX_BET_FRACTION,
    uncertainty_discount: float = 1.0,
    confidence: float = 0.75,
) -> dict:
    """
    Calculate optimal bet size using fractional Kelly criterion.

    Args:
        edge: Absolute edge (fair_value - kalshi_price), e.g. 0.11
        probability: Fair probability of winning, e.g. 0.29
        balance: Current account balance in dollars
        price_cents: Cost per contract in cents (1-99)
        max_fraction: Maximum fraction of balance per trade
        uncertainty_discount: Multiplicative discount on probability for model uncertainty.
            Use 0.85 for model-derived edges (weather, ELO) to prevent Kelly oversizing
            when the model's probability estimate may be wrong.
        confidence: Scanner confidence score (0-1). Applied as additional probability
            discount — high-confidence bets size slightly larger at identical edges.

    Returns:
        {contracts, price_cents, total_cost, max_payout, kelly_fraction, reason}
    """
    # Apply uncertainty discount only. Confidence gates trade entry (scanner level),
    # not probability inside Kelly — double-discounting kills all mid-edge trades.
    probability = probability * uncertainty_discount

    if edge <= 0 or probability <= 0 or probability >= 1 or balance <= 0 or price_cents <= 0:
        return {
            "contracts": 0,
            "price_cents": price_cents,
            "total_cost": 0.0,
            "max_payout": 0.0,
            "kelly_fraction": 0.0,
            "reason": "no_edge" if edge <= 0 else "invalid_inputs",
        }

    price_dollars = price_cents / 100.0

    # Kelly criterion: f = (bp - q) / b
    # b = net odds received on the wager (payout / cost - 1)
    # p = probability of winning, q = 1 - p
    b = (1.0 / price_dollars) - 1.0  # e.g. 18c contract: b = (1/0.18) - 1 = 4.56
    p = probability
    q = 1.0 - p

    full_kelly = (b * p - q) / b if b > 0 else 0.0

    if full_kelly <= 0:
        return {
            "contracts": 0,
            "price_cents": price_cents,
            "total_cost": 0.0,
            "max_payout": 0.0,
            "kelly_fraction": full_kelly,
            "reason": "kelly_negative",
        }

    # Fractional Kelly (25% of full)
    fractional = full_kelly * KELLY_FRACTION

    # Hard cap at max_fraction of balance
    capped = min(fractional, max_fraction)

    # Dollar amount to risk
    risk_dollars = balance * capped

    # Dynamic cap: 5% of balance, never more than $200
    dynamic_max = min(balance * MAX_BET_FRACTION, 200.0)

    # Apply dollar floor and ceiling
    risk_dollars = max(risk_dollars, MIN_BET_DOLLARS)
    risk_dollars = min(risk_dollars, dynamic_max)

    # Can't bet more than we have
    risk_dollars = min(risk_dollars, balance * max_fraction)

    # If still below minimum after all adjustments, skip
    if risk_dollars < MIN_BET_DOLLARS:
        return {
            "contracts": 0,
            "price_cents": price_cents,
            "total_cost": 0.0,
            "max_payout": 0.0,
            "kelly_fraction": full_kelly,
            "reason": "below_minimum",
        }

    # Calculate contract count
    contracts = max(1, math.floor(risk_dollars / price_dollars))
    total_cost = contracts * price_dollars
    max_payout = contracts * 1.00  # Each contract pays $1 on win

    return {
        "contracts": contracts,
        "price_cents": price_cents,
        "total_cost": round(total_cost, 2),
        "max_payout": round(max_payout, 2),
        "kelly_fraction": round(full_kelly, 4),
        "reason": "sized",
    }
