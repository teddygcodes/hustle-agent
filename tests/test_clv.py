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
