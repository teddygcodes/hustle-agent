"""Session 99: scalar win-probability proxy for live_momentum.

v0 model: direct lift of game_context.win_probability, reconstructed as
wp_edge + leader_price/100. Mathematically identical to game_ctx.win_probability
when both inputs are available; this module exists to persist that scalar in a
calibratable shape forward-only. Sizing-touching changes are explicitly out of
scope for the Session 99 ship.
"""
from __future__ import annotations

MODEL_SOURCE = "game_context_win_probability_v1"
PROB_FLOOR = 0.05
PROB_CEIL = 0.95


def estimate_live_momentum_win_prob(context: dict | None) -> dict:
    """Compute scalar win-prob from live_momentum decision context.

    Returns a dict with three keys: estimated_win_prob (float|None),
    model_source (str), confidence_components (dict). estimated_win_prob is
    None when context is missing, when either required input (wp_edge,
    leader_price) is absent, or when an input is non-numeric.
    """
    if not isinstance(context, dict):
        return {
            "estimated_win_prob": None,
            "model_source": MODEL_SOURCE,
            "confidence_components": {"reason": "no_context"},
        }
    wp_edge = context.get("wp_edge")
    leader_price = context.get("leader_price")
    if wp_edge is None or leader_price is None:
        return {
            "estimated_win_prob": None,
            "model_source": MODEL_SOURCE,
            "confidence_components": {
                "reason": "missing_required_input",
                "wp_edge_present": wp_edge is not None,
                "leader_price_present": leader_price is not None,
            },
        }
    try:
        market_implied = float(leader_price) / 100.0
        raw_prob = float(wp_edge) + market_implied
    except (TypeError, ValueError):
        return {
            "estimated_win_prob": None,
            "model_source": MODEL_SOURCE,
            "confidence_components": {"reason": "non_numeric_input"},
        }
    clamped = max(PROB_FLOOR, min(PROB_CEIL, raw_prob))
    return {
        "estimated_win_prob": round(clamped, 4),
        "model_source": MODEL_SOURCE,
        "confidence_components": {
            "wp_edge": round(float(wp_edge), 4),
            "market_implied": round(market_implied, 4),
            "pre_clamp_raw": round(raw_prob, 4),
            "clamped": clamped != raw_prob,
        },
    }
