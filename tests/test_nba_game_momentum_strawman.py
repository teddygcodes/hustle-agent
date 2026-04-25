"""Tests for bot/strategies/nba_game_momentum_strawman.py — verifies the
Strategy contract is general enough to host a brand-new strategy targeting
a market family no production code touches (KXNBAGAME)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bot.strategies import Market
from bot.strategies.nba_game_momentum_strawman import NbaGameMomentumStrawman


def _market(*, ticker: str, series: str = "KXNBAGAME",
            yes_ask: int = 50, no_ask: int = 50, no_bid: int | None = None,
            volume_24h: int = 200, status: str = "active") -> Market:
    if no_bid is None:
        no_bid = max(no_ask - 1, 1)
    return Market(
        ticker=ticker,
        series_ticker=series,
        event_ticker=f"{series}-EV",
        status=status,
        close_ts="2026-12-31T23:59:00Z",
        yes_ask=yes_ask,
        yes_bid=max(yes_ask - 1, 1),
        no_ask=no_ask,
        no_bid=no_bid,
        volume_24h=volume_24h,
        open_interest=50,
        ts="2026-04-25T12:00:00Z",
        scan_id="test-scan",
        raw={"close_time": "2026-12-31T23:59:00Z", "title": "test"},
    )


def _run(strategy, markets):
    with patch("bot.decisions.log_decision"):
        candidates = strategy.candidate_markets(markets)
        opps = [strategy.evaluate(m) for m in candidates]
        strategy.finalize("test-scan")
    return [o for o in opps if o is not None], candidates


class TestCandidateMarkets:
    def test_filters_by_kxnbagame_prefix(self):
        markets = [
            _market(ticker="KXNBAGAME-26APR25-A"),
            _market(ticker="KXMLBGAME-26APR25-A", series="KXMLBGAME"),
            _market(ticker="KXHIGHMIA-26APR25-A", series="KXHIGHMIA"),
        ]
        _, cands = _run(NbaGameMomentumStrawman(), markets)
        assert len(cands) == 1
        assert cands[0].ticker == "KXNBAGAME-26APR25-A"

    def test_filters_low_volume(self):
        markets = [
            _market(ticker="KXNBAGAME-HIGH", volume_24h=200),
            _market(ticker="KXNBAGAME-LOW", volume_24h=50),
        ]
        _, cands = _run(
            NbaGameMomentumStrawman(min_volume_24h=100),
            markets,
        )
        kept = {c.ticker for c in cands}
        assert "KXNBAGAME-HIGH" in kept
        assert "KXNBAGAME-LOW" not in kept

    def test_filters_non_active_status(self):
        markets = [
            _market(ticker="KXNBAGAME-OPEN", status="active"),
            _market(ticker="KXNBAGAME-CLOSED", status="closed"),
            _market(ticker="KXNBAGAME-SETTLED", status="settled"),
        ]
        _, cands = _run(NbaGameMomentumStrawman(), markets)
        kept = {c.ticker for c in cands}
        assert kept == {"KXNBAGAME-OPEN"}


class TestEvaluate:
    def test_emits_no_when_rule_fires(self):
        # yes_ask=20 → edge = (80/20) - 1 = 3.0 (huge "edge" by spec formula)
        # Rule preconditions: yes_ask<30 ✓, no_bid>70 ✓ (71>70).
        m = _market(ticker="KXNBAGAME-FAVE", yes_ask=20, no_ask=72,
                    no_bid=71, volume_24h=300)
        opps, _ = _run(NbaGameMomentumStrawman(min_relative_edge=0.05), [m])
        assert len(opps) == 1
        opp = opps[0]
        assert opp["ticker"] == "KXNBAGAME-FAVE"
        assert opp["recommended_side"] == "no"
        assert opp["type"] == "nba_game_momentum_strawman"
        assert opp["edge"] == 3.0

    def test_skips_when_rule_does_not_fire(self):
        # yes_ask=50, no_bid=49 → tight, rule does NOT fire
        m = _market(ticker="KXNBAGAME-TIGHT", yes_ask=50, no_ask=51,
                    no_bid=49, volume_24h=300)
        opps, _ = _run(NbaGameMomentumStrawman(), [m])
        assert opps == []

    def test_skips_when_no_bid_too_low(self):
        # Rule precondition: no_bid > 70. Setting no_bid=70 (not >70) skips.
        m = _market(ticker="KXNBAGAME-NB70", yes_ask=20, no_ask=78,
                    no_bid=70, volume_24h=300)
        opps, _ = _run(NbaGameMomentumStrawman(), [m])
        assert opps == []

    def test_min_relative_edge_filters_emitted_opps(self):
        # yes_ask=20 → edge = (80/20) - 1 = 3.0
        # yes_ask=25 → edge = (75/25) - 1 = 2.0
        m_lo_ya = _market(ticker="KXNBAGAME-LO", yes_ask=20, no_ask=72,
                          no_bid=71, volume_24h=300)
        m_hi_ya = _market(ticker="KXNBAGAME-HI", yes_ask=25, no_ask=70,
                          no_bid=72, volume_24h=300)
        # Permissive (default 0.05): both emit
        opps, _ = _run(
            NbaGameMomentumStrawman(min_relative_edge=0.05),
            [m_lo_ya, m_hi_ya],
        )
        assert len(opps) == 2
        # Strict 2.5: only yes_ask=20 (edge=3.0) clears; yes_ask=25 (edge=2.0) filtered
        opps, _ = _run(
            NbaGameMomentumStrawman(min_relative_edge=2.5),
            [m_lo_ya, m_hi_ya],
        )
        kept = {o["ticker"] for o in opps}
        assert kept == {"KXNBAGAME-LO"}

    def test_opportunity_dict_has_all_required_fields(self):
        m = _market(ticker="KXNBAGAME-FIELDS", yes_ask=20, no_ask=72,
                    no_bid=71, volume_24h=300)
        opps, _ = _run(NbaGameMomentumStrawman(min_relative_edge=0.05), [m])
        assert len(opps) == 1
        opp = opps[0]
        # Match the keys VigStackSeries emits
        for key in ("type", "ticker", "edge", "relative_edge",
                    "recommended_side", "edge_result", "no_ask_cents"):
            assert key in opp, f"missing {key}"
        assert opp["edge_result"]["self_check_passed"] is True
        # no_ask_cents is what the back-tester uses for entry_price_cents fallback
        assert opp["no_ask_cents"] == 72


class TestFinalize:
    def test_finalize_does_nothing_for_strawman(self):
        """Strawman has no telemetry / CF emission. Finalize is a no-op."""
        s = NbaGameMomentumStrawman()
        s.candidate_markets([])
        s.finalize("test-scan")  # Should not raise.
