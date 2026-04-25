"""Tests for tools/cohort_report.py — Session 10 distance histograms.

Pins the per-gate distance-from-threshold extraction, bucket assignment,
ASCII rendering, and graceful tolerance of pre-Session-10 records that
lack `extra` or have it but with missing keys.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.cohort_report import (  # noqa: E402
    DISTANCE_BUCKETS,
    aggregate_decisions,
    bucket_distance,
    distance_from_pass,
    render_distance_histogram,
)


class TestDistanceFromPass:

    def test_edge_below_threshold_close(self):
        # edge=0.018, threshold=0.02 → distance = (0.02-0.018)/0.02 = 0.10
        d = distance_from_pass("edge_below_threshold", {"edge": 0.018, "min_edge": 0.02})
        assert d == pytest.approx(0.10)

    def test_edge_below_threshold_far(self):
        # edge=0.001, threshold=0.02 → distance = 0.019/0.02 = 0.95
        d = distance_from_pass("edge_below_threshold", {"edge": 0.001, "min_edge": 0.02})
        assert d == pytest.approx(0.95)

    def test_low_liquidity_uses_closer_dimension(self):
        # volume=9 (1 below 10), oi=2 (3 below 5): dist_v=0.10, dist_oi=0.60 → min=0.10
        d = distance_from_pass(
            "low_liquidity",
            {"volume": 9, "open_interest": 2, "min_volume": 10, "min_open_interest": 5},
        )
        assert d == pytest.approx(0.10)

    def test_forecast_in_bucket_normalizes_by_2deg(self):
        # distance=1.5° → 1.5/2 = 0.75
        assert distance_from_pass("forecast_in_bucket", {"distance": 1.5}) == pytest.approx(0.75)
        # distance=-3° (deep inside) → 3/2 = 1.5
        assert distance_from_pass("forecast_in_bucket", {"distance": -3.0}) == pytest.approx(1.5)

    def test_cooldown_remaining_fraction(self):
        # age=200, cooldown=240 → remaining (240-200)/240 = 0.1667
        d = distance_from_pass("cooldown", {"last_trade_age_min": 200, "cooldown_min": 240})
        assert d == pytest.approx(40 / 240)

    def test_position_cap_uses_exposure_pct_over_max_pct(self):
        # exposure_pct=0.22, max_pct=0.20 → distance = (0.22-0.20)/0.20 = 0.10
        d = distance_from_pass("position_cap", {"exposure_pct": 0.22, "max_pct": 0.20})
        assert d == pytest.approx(0.10)

    def test_returns_none_when_extra_missing(self):
        assert distance_from_pass("edge_below_threshold", None) is None
        assert distance_from_pass("edge_below_threshold", {}) is None
        assert distance_from_pass("edge_below_threshold", {"edge": 0.01}) is None
        assert distance_from_pass("nonexistent_gate", {"foo": 1}) is None

    def test_returns_none_when_threshold_zero(self):
        assert distance_from_pass("edge_below_threshold", {"edge": 0.01, "min_edge": 0}) is None

    def test_returns_none_when_extra_has_none_values(self):
        # Pre-Session-10 records that partially populate extra shouldn't crash
        assert distance_from_pass("edge_below_threshold", {"edge": None, "min_edge": 0.02}) is None
        assert distance_from_pass("low_liquidity", {"volume": 5, "open_interest": None,
                                                     "min_volume": 10, "min_open_interest": 5}) is None


class TestBucketing:

    @pytest.mark.parametrize("d,expected", [
        (0.00, "<10%"),
        (0.05, "<10%"),
        (0.099, "<10%"),
        (0.10, "10-25%"),
        (0.20, "10-25%"),
        (0.25, "25-50%"),
        (0.49, "25-50%"),
        (0.50, "50-100%"),
        (0.99, "50-100%"),
        (1.00, ">100%"),
        (5.00, ">100%"),
    ])
    def test_bucket_assignment(self, d, expected):
        assert bucket_distance(d) == expected


class TestHistogramRendering:

    def test_empty_distances_renders_placeholder(self):
        lines = render_distance_histogram([])
        assert any("No distance data" in l for l in lines)

    def test_distribution_renders_bars(self):
        # 5 in <10%, 3 in 10-25%, 1 in >100%
        distances = [0.01, 0.02, 0.05, 0.08, 0.09, 0.15, 0.20, 0.22, 5.0]
        lines = render_distance_histogram(distances)
        joined = "\n".join(lines)
        assert "<10%" in joined
        assert "█" in joined
        # 9 distances total, 5 in first bucket → 5/9 = 55.6%
        assert "55.6%" in joined or "55.5%" in joined

    def test_all_buckets_appear_even_when_empty(self):
        # Even if no distance lands in a bucket, the row is still rendered (count=0)
        lines = render_distance_histogram([0.05])
        joined = "\n".join(lines)
        for label, _, _ in DISTANCE_BUCKETS:
            assert label in joined


class TestAggregateWithDistances:

    def test_aggregator_collects_distances_when_extra_present(self):
        decisions = [
            {"opp_type": "vig_stack_series", "reason": "edge_below_threshold",
             "decision": "reject", "edge": 0.018,
             "extra": {"edge": 0.018, "min_edge": 0.02}},
            {"opp_type": "vig_stack_series", "reason": "edge_below_threshold",
             "decision": "reject", "edge": 0.005,
             "extra": {"edge": 0.005, "min_edge": 0.02}},
            # No extra — pre-Session-10 record, should NOT contribute distance
            {"opp_type": "vig_stack_series", "reason": "edge_below_threshold",
             "decision": "reject", "edge": 0.010},
        ]
        bins = aggregate_decisions(decisions)
        bin_ = bins[("vig_stack_series", "edge_below_threshold")]
        assert bin_["rejects"] == 3
        assert len(bin_["distances"]) == 2  # only records with extra
        assert bin_["distances"][0] == pytest.approx(0.10)
        assert bin_["distances"][1] == pytest.approx(0.75)

    def test_aggregator_skips_unhandled_gates_silently(self):
        # A gate distance_from_pass doesn't recognize must not crash and must
        # not pollute the distances list
        decisions = [
            {"opp_type": "vig_stack_series", "reason": "no_vig",
             "decision": "reject", "edge": None,
             "extra": {"yes_sum": 102, "group_size": 4}},
        ]
        bins = aggregate_decisions(decisions)
        bin_ = bins[("vig_stack_series", "no_vig")]
        assert bin_["rejects"] == 1
        assert bin_["distances"] == []
