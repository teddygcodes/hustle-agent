"""Session 99: unit tests for the live_momentum win-prob proxy."""
from bot.live_momentum_proxy import (
    estimate_live_momentum_win_prob,
    MODEL_SOURCE,
    PROB_FLOOR,
    PROB_CEIL,
)


def test_returns_expected_scalar_for_known_inputs():
    # wp_edge=0.05, leader_price=60¢ → market_implied=0.60, raw=0.65 → unclamped
    result = estimate_live_momentum_win_prob({"wp_edge": 0.05, "leader_price": 60})
    assert result["estimated_win_prob"] == 0.65
    assert result["model_source"] == MODEL_SOURCE
    assert result["confidence_components"]["wp_edge"] == 0.05
    assert result["confidence_components"]["market_implied"] == 0.6
    assert result["confidence_components"]["pre_clamp_raw"] == 0.65
    assert result["confidence_components"]["clamped"] is False


def test_clamps_at_floor():
    # wp_edge=-0.10, leader_price=10 → raw=0.0 → clamped to 0.05
    result = estimate_live_momentum_win_prob({"wp_edge": -0.10, "leader_price": 10})
    assert result["estimated_win_prob"] == PROB_FLOOR
    assert result["confidence_components"]["clamped"] is True
    assert result["confidence_components"]["pre_clamp_raw"] == 0.0


def test_clamps_at_ceiling():
    # wp_edge=0.10, leader_price=90 → raw=1.0 → clamped to 0.95
    result = estimate_live_momentum_win_prob({"wp_edge": 0.10, "leader_price": 90})
    assert result["estimated_win_prob"] == PROB_CEIL
    assert result["confidence_components"]["clamped"] is True


def test_returns_none_when_wp_edge_missing():
    result = estimate_live_momentum_win_prob({"leader_price": 70})
    assert result["estimated_win_prob"] is None
    assert result["model_source"] == MODEL_SOURCE
    assert result["confidence_components"]["reason"] == "missing_required_input"
    assert result["confidence_components"]["wp_edge_present"] is False
    assert result["confidence_components"]["leader_price_present"] is True


def test_returns_none_when_leader_price_missing():
    result = estimate_live_momentum_win_prob({"wp_edge": 0.05})
    assert result["estimated_win_prob"] is None
    assert result["confidence_components"]["leader_price_present"] is False


def test_returns_none_for_non_dict():
    result = estimate_live_momentum_win_prob(None)
    assert result["estimated_win_prob"] is None
    assert result["confidence_components"]["reason"] == "no_context"


def test_handles_non_numeric_input():
    result = estimate_live_momentum_win_prob({"wp_edge": "nope", "leader_price": 70})
    assert result["estimated_win_prob"] is None
    assert result["confidence_components"]["reason"] == "non_numeric_input"


def test_model_source_is_stable_string():
    # Calibration analysis will group by this; any rename is a breaking
    # schema change. If this assert fails, increment to v2 and update
    # CLAUDE.md's "Canonical Data Schema Reference" entry intentionally.
    assert MODEL_SOURCE == "game_context_win_probability_v1"


def test_math_identity_reconstructs_win_probability():
    # The defining property: estimated_win_prob == game_ctx.win_probability
    # when wp_edge was computed as (game_ctx.win_probability - yes_ask/100)
    # and leader_price == yes_ask. Verify three sample points inside the clamp band.
    for win_p, ask in [(0.62, 55), (0.78, 70), (0.55, 50)]:
        wp_edge = win_p - (ask / 100.0)
        result = estimate_live_momentum_win_prob({"wp_edge": wp_edge, "leader_price": ask})
        assert abs(result["estimated_win_prob"] - win_p) < 1e-3
