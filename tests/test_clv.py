"""Tests for bot/clv.py — counterfactual record support.

Session 6 (Apr 24): top-N rejected opportunities per scan get a CF record
that the existing settlement poller fills in. Two invariants matter:

1. CF records carry status="counterfactual_open" → "counterfactual_settled"
   so `get_clv_report()` (filters status=="settled") never pollutes
   paper-trade stats with CF data.
2. `_load()`'s active-strategy filter (Apr 23 Session 5) does not drop CFs
   because we set `opp_type` to the would-have-been strategy.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import clv  # noqa: E402


@pytest.fixture
def tmp_clv_file(tmp_path, monkeypatch):
    """Repoint clv._CLV_FILE so tests use a tmp file."""
    f = tmp_path / "clv.json"
    monkeypatch.setattr(clv, "_CLV_FILE", f)
    return f


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


SAMPLE_OPP = {
    "ticker": "KXHIGHMIA-26APR24-T80",
    "opp_type": "vig_stack_series",
    "side": "no",
    "price_cents": 92,
    "fair_value_cents": 95.5,
    "edge": 0.0823,
}


class TestCounterfactualRecording:

    def test_basic_record_has_required_fields(self, tmp_clv_file):
        clv.record_counterfactual_skip(SAMPLE_OPP, "edge_below_threshold", "20260424T120000")
        recs = _read(tmp_clv_file)
        assert len(recs) == 1
        r = recs[0]
        assert r["ticker"] == "KXHIGHMIA-26APR24-T80"
        assert r["opp_type"] == "vig_stack_series"
        assert r["side"] == "no"
        assert r["entry_price_cents"] == 92
        assert r["fair_value_cents"] == 95.5
        assert r["edge_at_trade"] == 0.0823
        assert r["contracts"] == 0  # never traded
        assert r["paper"] is False
        assert r["status"] == "counterfactual_open"
        assert r["skipped_by_gate"] == "edge_below_threshold"
        assert r["closing_yes_price"] is None
        assert r["clv_cents"] is None
        assert r["trade_id"] == "CF-20260424T120000-KXHIGHMIA-26APR24-T80"

    def test_idempotent_on_repeat_call(self, tmp_clv_file):
        clv.record_counterfactual_skip(SAMPLE_OPP, "low_liquidity", "SCAN1")
        clv.record_counterfactual_skip(SAMPLE_OPP, "low_liquidity", "SCAN1")
        clv.record_counterfactual_skip(SAMPLE_OPP, "low_liquidity", "SCAN1")
        assert len(_read(tmp_clv_file)) == 1

    def test_different_scan_ids_produce_separate_records(self, tmp_clv_file):
        clv.record_counterfactual_skip(SAMPLE_OPP, "x", "SCAN1")
        clv.record_counterfactual_skip(SAMPLE_OPP, "x", "SCAN2")
        assert len(_read(tmp_clv_file)) == 2

    def test_skips_when_no_usable_entry_price(self, tmp_clv_file):
        bad = {**SAMPLE_OPP, "price_cents": None, "yes_ask": None}
        clv.record_counterfactual_skip(bad, "x", "SCAN1")
        assert _read(tmp_clv_file) == []

    def test_falls_back_to_yes_ask_when_no_price_cents(self, tmp_clv_file):
        opp = {**SAMPLE_OPP}
        opp.pop("price_cents")
        opp["yes_ask"] = 87
        clv.record_counterfactual_skip(opp, "x", "SCAN1")
        recs = _read(tmp_clv_file)
        assert len(recs) == 1
        assert recs[0]["entry_price_cents"] == 87


class TestActiveStrategyFilter:
    """The Session-5 _load() filter must not drop CF records."""

    def test_cf_record_passes_active_strategy_filter(self, tmp_clv_file):
        clv.record_counterfactual_skip(SAMPLE_OPP, "x", "SCAN1")
        # Re-loading via _load() should still see it (opp_type=vig_stack_series is active)
        loaded = clv._load()
        cfs = [r for r in loaded if r.get("status") == "counterfactual_open"]
        assert len(cfs) == 1

    def test_cf_record_for_disabled_strategy_dropped(self, tmp_clv_file):
        opp = {**SAMPLE_OPP, "opp_type": "btc_price_edge"}  # disabled
        clv.record_counterfactual_skip(opp, "x", "SCAN1")
        # File still has it (we wrote it), but _load() filters it out
        assert len(_read(tmp_clv_file)) == 1
        assert clv._load() == []


class TestSettlementHandling:
    """check_clv_settlements must process CF records too, but mark them
    'counterfactual_settled' (not 'settled') so paper CLV reports stay clean."""

    def test_cf_settles_to_counterfactual_settled_status(self, tmp_clv_file):
        clv.record_counterfactual_skip(SAMPLE_OPP, "edge_below_threshold", "SCAN1")

        fake_market = {"market": {"status": "settled", "result": "YES",
                                  "yes_bid": 0, "yes_ask": 0}}
        with patch("agent.kalshi_client.get_market", return_value=fake_market):
            settled_now = clv.check_clv_settlements()

        # CF settlements are NOT in the return list (paper-only notification)
        assert settled_now == []

        # But the record was updated on disk
        recs = _read(tmp_clv_file)
        assert len(recs) == 1
        r = recs[0]
        assert r["status"] == "counterfactual_settled"
        assert r["closing_yes_price"] == 100  # YES resolved
        # NO side: clv_cents = (100 - entry) - closing = (100-92) - 100 = -92
        # Paid 92¢ for NO, market closed YES → NO worth 0 → -92¢ CLV
        assert r["clv_cents"] == -92.0

    def test_real_trade_still_settles_normally(self, tmp_clv_file):
        clv.record_clv_entry(
            ticker="KXHIGHMIA-26APR24-T80", opp_type="vig_stack_series", side="no",
            entry_price_cents=92, fair_value_cents=95.5, edge_at_trade=0.08,
            contracts=2, trade_id="PAPER-X1", paper=True,
        )
        fake_market = {"market": {"status": "settled", "result": "NO",
                                  "yes_bid": 0, "yes_ask": 0}}
        with patch("agent.kalshi_client.get_market", return_value=fake_market):
            settled_now = clv.check_clv_settlements()

        # Real trade settlement returns to caller
        assert len(settled_now) == 1
        recs = _read(tmp_clv_file)
        assert recs[0]["status"] == "settled"  # NOT "counterfactual_settled"


class TestReportExclusion:
    """get_clv_report() filters status=='settled' — CF settled records must not appear."""

    def test_cf_settled_excluded_from_report(self, tmp_clv_file):
        # One real trade (settled), one CF (counterfactual_settled) — only real should count
        records = [
            {
                "ticker": "X1", "opp_type": "vig_stack_series", "side": "no",
                "entry_price_cents": 90, "fair_value_cents": 95, "edge_at_trade": 0.1,
                "contracts": 2, "trade_id": "PAPER-1", "paper": True,
                "recorded_at": "2026-04-24T12:00:00+00:00",
                "status": "settled", "closing_yes_price": 100,
                "clv_cents": -90.0, "clv_relative": -1.0,
                "settled_at": "2026-04-24T13:00:00+00:00",
            },
            {
                "ticker": "X2", "opp_type": "vig_stack_series", "side": "no",
                "entry_price_cents": 92, "fair_value_cents": 95, "edge_at_trade": 0.08,
                "contracts": 0, "trade_id": "CF-SCAN1-X2", "paper": False,
                "recorded_at": "2026-04-24T12:30:00+00:00",
                "status": "counterfactual_settled", "skipped_by_gate": "edge_below_threshold",
                "closing_yes_price": 100, "clv_cents": -92.0, "clv_relative": -1.0,
                "settled_at": "2026-04-24T13:30:00+00:00",
            },
        ]
        tmp_clv_file.write_text(json.dumps(records))

        report = clv.get_clv_report()
        # Only the one real settlement counted
        assert report["overall"]["count"] == 1
        # CF didn't double the count or skew the average
        assert report["by_strategy"]["vig_stack_series"]["count"] == 1


# ---------------------------------------------------------------------------
# Session 8 (Apr 24) — stratified CF sampling
# ---------------------------------------------------------------------------

def _mk_reject(ticker: str, opp_type: str, skip_reason: str,
               edge: float, price_cents: int = 50) -> dict:
    """Shape matches scanner._build_reject_opp output."""
    return {
        "ticker": ticker,
        "title": f"{ticker} title",
        "type": opp_type,
        "opp_type": opp_type,
        "side": "no",
        "recommended_side": "no",
        "price_cents": price_cents,
        "fair_value_cents": price_cents + int(edge * price_cents),
        "edge": edge,
        "skip_reason": skip_reason,
    }


class TestStratifiedCFSampling:
    """Session 8: every (opp_type, gate) that fires must get ≥1 CF record.

    The Session-6 global top-5 starved low-edge-by-design gates like
    edge_below_threshold (0/130 CFs over 24h) because non_stable_below_
    weather_floor (edges 4-20¢) always won the sort. Stratified sampling
    guarantees per-gate attribution, with global budget fill on top.
    """

    def test_every_gate_gets_at_least_one_cf(self):
        """20 rejects across 4 gates × 2 opp_types — every pair selected."""
        from bot.strategies.vig_stack_series import _stratified_cf_rejects
        rejects = [
            # forecast_in_bucket on vig_stack_series — low edges
            _mk_reject("A1", "vig_stack_series", "forecast_in_bucket", 0.005),
            _mk_reject("A2", "vig_stack_series", "forecast_in_bucket", 0.008),
            _mk_reject("A3", "vig_stack_series", "forecast_in_bucket", 0.003),
            # edge_below_threshold on vig_stack_futures — very low edges
            _mk_reject("B1", "vig_stack_futures", "edge_below_threshold", 0.01),
            _mk_reject("B2", "vig_stack_futures", "edge_below_threshold", 0.015),
            _mk_reject("B3", "vig_stack_futures", "edge_below_threshold", 0.012),
            # edge_below_threshold on vig_stack_series — low edges
            _mk_reject("C1", "vig_stack_series", "edge_below_threshold", 0.018),
            _mk_reject("C2", "vig_stack_series", "edge_below_threshold", 0.014),
            # non_stable_below_weather_floor — high edges (would dominate top-5)
            _mk_reject("D1", "vig_stack_series", "non_stable_below_weather_floor", 0.12),
            _mk_reject("D2", "vig_stack_series", "non_stable_below_weather_floor", 0.18),
            _mk_reject("D3", "vig_stack_series", "non_stable_below_weather_floor", 0.09),
            _mk_reject("D4", "vig_stack_series", "non_stable_below_weather_floor", 0.15),
            _mk_reject("D5", "vig_stack_series", "non_stable_below_weather_floor", 0.20),
            _mk_reject("D6", "vig_stack_series", "non_stable_below_weather_floor", 0.07),
            # no_price_too_low on vig_stack_series
            _mk_reject("E1", "vig_stack_series", "no_price_too_low", 0.25, price_cents=3),
            _mk_reject("E2", "vig_stack_series", "no_price_too_low", 0.35, price_cents=3),
        ]
        selected = _stratified_cf_rejects(rejects)

        pairs = {(r["opp_type"], r["skip_reason"]) for r in selected}
        expected_pairs = {
            ("vig_stack_series", "forecast_in_bucket"),
            ("vig_stack_futures", "edge_below_threshold"),
            ("vig_stack_series", "edge_below_threshold"),
            ("vig_stack_series", "non_stable_below_weather_floor"),
            ("vig_stack_series", "no_price_too_low"),
        }
        missing = expected_pairs - pairs
        assert not missing, f"gates with zero CF attribution: {missing}"

    def test_respects_hard_cap(self):
        """30 rejects across 2 gates — result ≤ hard_cap."""
        from bot.strategies.vig_stack_series import _stratified_cf_rejects
        rejects = [
            _mk_reject(f"T{i}", "vig_stack_series",
                       "forecast_in_bucket" if i % 2 else "edge_below_threshold",
                       0.01 + i * 0.001)
            for i in range(30)
        ]
        assert len(_stratified_cf_rejects(rejects, hard_cap=15)) <= 15
        assert len(_stratified_cf_rejects(rejects, hard_cap=5)) <= 5

    def test_entry_below_3_cents_filtered(self):
        """Even the highest-edge reject is dropped if entry < 3¢."""
        from bot.strategies.vig_stack_series import _stratified_cf_rejects
        rejects = [
            _mk_reject("LOW", "vig_stack_series", "edge_below_threshold", 50.0, price_cents=2),
            _mk_reject("OK", "vig_stack_series", "edge_below_threshold", 0.01, price_cents=5),
        ]
        selected = _stratified_cf_rejects(rejects)
        tickers = {r["ticker"] for r in selected}
        assert "LOW" not in tickers
        assert "OK" in tickers

    def test_none_edge_filtered(self):
        """Rejects with edge=None are filtered (they'd break the sort)."""
        from bot.strategies.vig_stack_series import _stratified_cf_rejects
        no_edge = _mk_reject("NOEDGE", "vig_stack_series", "x", 0.1)
        no_edge["edge"] = None
        with_edge = _mk_reject("OK", "vig_stack_series", "x", 0.05)
        selected = _stratified_cf_rejects([no_edge, with_edge])
        tickers = {r["ticker"] for r in selected}
        assert "NOEDGE" not in tickers
        assert "OK" in tickers

    def test_duplicate_ticker_deduped_keeps_higher_edge(self):
        """Same ticker in two groups: keep one copy, higher edge wins."""
        from bot.strategies.vig_stack_series import _stratified_cf_rejects
        rejects = [
            _mk_reject("DUP", "vig_stack_series", "forecast_in_bucket", 0.02),
            _mk_reject("DUP", "vig_stack_series", "edge_below_threshold", 0.08),
            _mk_reject("OTHER", "vig_stack_series", "non_stable_below_weather_floor", 0.15),
        ]
        selected = _stratified_cf_rejects(rejects)
        dup_entries = [r for r in selected if r["ticker"] == "DUP"]
        assert len(dup_entries) == 1
        assert dup_entries[0]["edge"] == 0.08  # higher-edge copy wins

    def test_opp_type_preserved_for_active_strategy_filter(self):
        """Selected opps must keep opp_type so clv._load() active-strategy
        filter (clv.py:39-44) doesn't silently drop the resulting CFs."""
        from bot.strategies.vig_stack_series import _stratified_cf_rejects
        rejects = [
            _mk_reject("A", "vig_stack_series", "edge_below_threshold", 0.02),
            _mk_reject("B", "vig_stack_futures", "edge_below_threshold", 0.02),
        ]
        selected = _stratified_cf_rejects(rejects)
        for r in selected:
            assert r["opp_type"] in ("vig_stack_series", "vig_stack_futures")

    def test_empty_input_returns_empty(self):
        from bot.strategies.vig_stack_series import _stratified_cf_rejects
        assert _stratified_cf_rejects([]) == []

    def test_budget_fill_adds_high_edge_leftovers(self):
        """With 1 gate × 8 rejects and total_budget=5: 1 stratified core + 4 fill."""
        from bot.strategies.vig_stack_series import _stratified_cf_rejects
        rejects = [
            _mk_reject(f"T{i}", "vig_stack_series", "forecast_in_bucket", 0.01 + i * 0.001)
            for i in range(8)
        ]
        selected = _stratified_cf_rejects(rejects, total_budget=5)
        assert len(selected) == 5
        # Highest-edge rejects dominate: T7 (edge 0.017) must be in
        assert any(r["ticker"] == "T7" for r in selected)
