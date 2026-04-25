"""Parameter override tests for refactored Strategy classes.

These tests verify constructor kwargs flow into evaluate() gate decisions.
The 13a golden-file test (tests/test_vig_stack_series_strategy.py) covers
the *defaults*; this file covers *overrides*.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bot.strategies import Market
from bot.strategies.vig_stack_series import VigStackSeries


def _stable_ladder_market(ticker: str, yes_ask: int, no_ask: int) -> Market:
    """KXHIGHMIA-prefixed (stable family) market with viable vig."""
    return Market(
        ticker=ticker,
        series_ticker="KXHIGHMIA",
        event_ticker="KXHIGHMIA-EV",
        status="active",
        close_ts="2026-12-31T23:59:00Z",
        yes_ask=yes_ask,
        yes_bid=max(yes_ask - 1, 1),
        no_ask=no_ask,
        no_bid=max(no_ask - 1, 1),
        volume_24h=200,
        open_interest=50,
        ts="2026-04-25T12:00:00Z",
        scan_id="test-scan",
        raw={"close_time": "2026-12-31T23:59:00Z",
             "title": "Stable ladder test"},
    )


def _run_strategy(strategy: VigStackSeries, markets: list[Market]) -> list[dict]:
    with patch("bot.strategies.vig_stack_series._fetch_vig_stack_forecasts",
               return_value={}), \
         patch("bot.decisions.log_decision"), \
         patch("bot.clv.record_counterfactual_skip"):
        candidates = strategy.candidate_markets(markets)
        opps = [strategy.evaluate(m) for m in candidates]
        strategy.finalize("test-scan")
    return [o for o in opps if o is not None]


class TestVigStackSeriesParams:
    """Verify constructor kwargs change which opps are emitted."""

    def _ladder(self) -> list[Market]:
        # 5-rung KXHIGHMIA ladder. yes_sum=135 → vig_factor=1.35.
        # For yes_ask=25: yes_fair = 25/1.35 = 18.52¢ → no_fair = 81.48¢
        #   no_ask=75 → no_edge = (81.48 - 75) / 75 ≈ 0.0864
        # For yes_ask=30: yes_fair = 30/1.35 = 22.22¢ → no_fair = 77.78¢
        #   no_ask=70 → no_edge = (77.78 - 70) / 70 ≈ 0.1111
        return [
            _stable_ladder_market("KXHIGHMIA-26APR26-T70",  25, 75),
            _stable_ladder_market("KXHIGHMIA-26APR26-B71.5", 30, 70),
            _stable_ladder_market("KXHIGHMIA-26APR26-B73.5", 25, 75),
            _stable_ladder_market("KXHIGHMIA-26APR26-B75.5", 25, 75),
            _stable_ladder_market("KXHIGHMIA-26APR26-T78",  30, 70),
        ]

    def test_default_emits_all_rungs(self):
        """Sanity: with default min_relative_edge=0.02, all 5 rungs emit
        (each clears the 2% threshold by a wide margin)."""
        opps = _run_strategy(VigStackSeries(), self._ladder())
        assert len(opps) == 5

    def test_strict_min_relative_edge_filters_out_low_edge_rungs(self):
        """With min_relative_edge=0.10, only rungs with edge >= 10% emit.
        On the ladder above, yes_ask=30 rungs (edge ~11.1%) emit, yes_ask=25
        rungs (edge ~8.6%) do not."""
        opps = _run_strategy(
            VigStackSeries(min_relative_edge=0.10),
            self._ladder(),
        )
        kept_tickers = {o["ticker"] for o in opps}
        # The two yes_ask=30 rungs (edge ~11%) clear; the three yes_ask=25
        # rungs (edge ~8.6%) get rejected by edge_below_threshold.
        assert "KXHIGHMIA-26APR26-B71.5" in kept_tickers
        assert "KXHIGHMIA-26APR26-T78" in kept_tickers
        assert len(opps) == 2

    def test_high_min_relative_edge_filters_all(self):
        """With min_relative_edge=0.50, no rung clears."""
        opps = _run_strategy(
            VigStackSeries(min_relative_edge=0.50),
            self._ladder(),
        )
        assert opps == []

    def test_stable_min_no_override_blocks_low_no_prices(self):
        """Default stable_min_no=0.70 admits no_ask=70¢ rungs.
        Override to 0.71 — the 70¢ rungs get rejected by
        no_price_below_floor while no_ask=75 rungs survive."""
        opps = _run_strategy(
            VigStackSeries(stable_min_no=0.71),
            self._ladder(),
        )
        # Only no_ask=75 rungs survive.
        kept = {o["ticker"] for o in opps}
        assert all("B71.5" not in t and "T78" not in t for t in kept)
        assert len(opps) == 3  # the three no_ask=75 rungs

    def test_volatile_min_no_override_does_not_affect_stable_family(self):
        """KXHIGHMIA is in VIG_STACK_STABLE_FAMILIES, so the stable gate
        runs, not the volatile gate. Setting volatile_min_no=0.99 should
        change nothing on a stable-family ladder."""
        opps_default = _run_strategy(VigStackSeries(), self._ladder())
        opps_override = _run_strategy(
            VigStackSeries(volatile_min_no=0.99),
            self._ladder(),
        )
        assert len(opps_default) == len(opps_override) == 5
