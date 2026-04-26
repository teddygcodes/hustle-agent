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

    def test_cf_record_carries_regime_dict(self, tmp_clv_file):
        """Session 14: counterfactual records get `regime` with all 4 axes.
        opp dict has close_ts, so event_horizon_hr should populate."""
        opp = {
            "ticker": "KXNBAGAME-26APR25-LAL",
            "opp_type": "vig_stack_series",
            "side": "yes",
            "price_cents": 38,
            "fair_value_cents": 45.0,
            "edge": 0.18,
            "close_ts": "2026-04-26T03:00:00+00:00",
        }
        clv.record_counterfactual_skip(opp, "min_edge", "scan-x")
        rec = _read(tmp_clv_file)[-1]
        assert "regime" in rec
        assert set(rec["regime"].keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr",
        }
        # opp had close_ts → event_horizon_hr resolves
        assert rec["regime"]["event_horizon_hr"] is not None

    def test_real_entry_carries_regime_dict(self, tmp_clv_file):
        """Session 14: record_clv_entry tags regime. Caller doesn't pass
        close_ts, so event_horizon_hr is expected None — other axes still
        populate."""
        clv.record_clv_entry(
            ticker="KXNHLGAME-26APR25-NYR",
            opp_type="vig_stack_series",
            side="yes",
            entry_price_cents=45,
            fair_value_cents=52.0,
            edge_at_trade=0.07,
            contracts=10,
            trade_id="t-1",
            paper=False,
        )
        rec = _read(tmp_clv_file)[-1]
        assert "regime" in rec
        assert set(rec["regime"].keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr",
        }
        assert rec["regime"]["event_horizon_hr"] is None


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


class TestComputeClvCents:
    """Session 13b: behavior-preservation test for the extracted compute_clv_cents().

    The live settler at bot.clv.check_clv_settlements() and the back-tester at
    tools/backtest.py both call this function. Divergence here = back-tester is
    wrong, not live (user spec, Session 13b)."""

    def test_yes_side_positive_clv(self):
        # Bought YES at 80, market closes YES at 100 -> +20 CLV
        cents, rel = clv.compute_clv_cents("yes", 80, 100.0)
        assert cents == 20.0
        assert rel == pytest.approx(0.25, abs=1e-6)

    def test_yes_side_negative_clv(self):
        # Bought YES at 80, market closes YES at 0 -> -80 CLV
        cents, rel = clv.compute_clv_cents("yes", 80, 0.0)
        assert cents == -80.0
        assert rel == pytest.approx(-1.0, abs=1e-6)

    def test_no_side_positive_clv(self):
        # Bought NO at 90 (implying YES at 10), YES closes at 0 -> +10 CLV (NO wins)
        cents, rel = clv.compute_clv_cents("no", 90, 0.0)
        assert cents == 10.0
        # Function rounds clv_relative to 4dp; 10/90 = 0.1111 after rounding.
        assert rel == round(10 / 90, 4)

    def test_no_side_negative_clv(self):
        # Bought NO at 90, YES closes at 100 -> -90 CLV (NO loses)
        cents, rel = clv.compute_clv_cents("no", 90, 100.0)
        assert cents == -90.0
        assert rel == pytest.approx(-1.0, abs=1e-6)

    def test_zero_entry_price_zero_relative(self):
        cents, rel = clv.compute_clv_cents("yes", 0, 50.0)
        assert cents == 50.0
        assert rel == 0.0

    def test_rounding_two_and_four_dp(self):
        cents, rel = clv.compute_clv_cents("yes", 33, 100.0)
        assert cents == 67.0
        assert rel == round(67 / 33, 4)


# ---------------------------------------------------------------------------
# Session 16 (Apr 26+) — Settlement-time MFE extension
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_positions_file(tmp_path, monkeypatch):
    """Repoint bot.config.POSITIONS_FILE so check_clv_settlements reads
    a tmp positions.json. The lazy `from bot.config import POSITIONS_FILE`
    inside check_clv_settlements resolves to the patched value."""
    import bot.config
    f = tmp_path / "positions.json"
    monkeypatch.setattr(bot.config, "POSITIONS_FILE", f)
    return f


def _settled_market(result: str = "YES") -> dict:
    """Mock get_market response for a settled market."""
    return {
        "market": {
            "status": "settled",
            "result": result,
            "yes_bid": 0,
            "yes_ask": 0,
        }
    }


class TestSettlementMfeExtension:
    """Session 16: at settlement-time propagation, mfe_cents is ratcheted
    to max(observed_mfe, clv_cents) clamped ≥0, so gap = mfe - clv ≥ 0
    in tools/excursion_report.py.

    Eliminates the structural -1¢ gap on every winning held-to-settlement
    position (where observed bid ≤ 99 but settlement payout is 100).
    """

    def test_winner_pos_mfe_below_settlement_gets_extended(
        self, tmp_clv_file, tmp_positions_file
    ):
        # YES @77, settles YES (closing=100 → clv=23). Pos observed mfe=22
        # (yes_bid topped at 99). Post-extension: mfe_cents=23, gap=0.
        clv.record_clv_entry(
            ticker="KXLM-WIN1", opp_type="live_momentum", side="yes",
            entry_price_cents=77, fair_value_cents=85.0, edge_at_trade=0.10,
            contracts=1, trade_id="ORD-WIN1", paper=True,
        )
        tmp_positions_file.write_text(json.dumps([{
            "order_id": "ORD-WIN1",
            "mfe_cents": 22,
            "mae_cents": 1,
            "mfe_at": "2026-04-25T10:00:00+00:00",
            "mae_at": "2026-04-25T09:30:00+00:00",
            "ticks_observed": 3,
        }]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("YES")):
            settled_now = clv.check_clv_settlements()

        assert len(settled_now) == 1
        rec = _read(tmp_clv_file)[0]
        assert rec["clv_cents"] == 23.0
        # Extended: max(22, 23) = 23. mfe_at advances to settled_at.
        assert rec["mfe_cents"] == 23
        assert rec["mfe_at"] == rec["settled_at"]
        # MAE NOT extended (out of scope for Session 16).
        assert rec["mae_cents"] == 1
        assert rec["mae_at"] == "2026-04-25T09:30:00+00:00"
        # gap = 23 - 23 = 0 ✓
        assert rec["mfe_cents"] - rec["clv_cents"] == 0

    def test_no_winner_extended_to_clv(self, tmp_clv_file, tmp_positions_file):
        # NO @93, settles NO (closing=0 → clv=7). Pos mfe=6.
        # Post-extension: mfe_cents=7, gap=0.
        clv.record_clv_entry(
            ticker="KXVS-WIN1", opp_type="vig_stack_series", side="no",
            entry_price_cents=93, fair_value_cents=98.0, edge_at_trade=0.05,
            contracts=2, trade_id="ORD-VS1", paper=True,
        )
        tmp_positions_file.write_text(json.dumps([{
            "order_id": "ORD-VS1",
            "mfe_cents": 6,
            "mae_cents": 0,
            "mfe_at": "2026-04-25T08:00:00+00:00",
            "mae_at": "2026-04-25T08:00:00+00:00",
            "ticks_observed": 9,
        }]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("NO")):
            clv.check_clv_settlements()
        rec = _read(tmp_clv_file)[0]
        assert rec["clv_cents"] == 7.0
        assert rec["mfe_cents"] == 7
        assert rec["mfe_at"] == rec["settled_at"]

    def test_observed_mfe_above_clv_no_change(self, tmp_clv_file, tmp_positions_file):
        # Edge case: pos.mfe = 24 (theoretically possible if record entry
        # was lower than the bid range observed). clv=23. max(24,23)=24,
        # so existing mfe_cents and mfe_at preserved.
        clv.record_clv_entry(
            ticker="KXLM-EDGE", opp_type="live_momentum", side="yes",
            entry_price_cents=77, fair_value_cents=85.0, edge_at_trade=0.10,
            contracts=1, trade_id="ORD-EDGE", paper=True,
        )
        tmp_positions_file.write_text(json.dumps([{
            "order_id": "ORD-EDGE",
            "mfe_cents": 24,
            "mfe_at": "2026-04-25T10:00:00+00:00",
        }]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("YES")):
            clv.check_clv_settlements()
        rec = _read(tmp_clv_file)[0]
        assert rec["clv_cents"] == 23.0
        # No change — observed MFE was already higher.
        assert rec["mfe_cents"] == 24
        assert rec["mfe_at"] == "2026-04-25T10:00:00+00:00"

    def test_loser_no_extension(self, tmp_clv_file, tmp_positions_file):
        # YES @78, settles NO (closing=0 → clv=-78). Pos mfe=15
        # (briefly went favorable). max(15, max(0, -78)) = max(15, 0) = 15.
        # No change — losers can never have MFE extended.
        clv.record_clv_entry(
            ticker="KXLM-LOSE", opp_type="live_momentum", side="yes",
            entry_price_cents=78, fair_value_cents=85.0, edge_at_trade=0.10,
            contracts=1, trade_id="ORD-LOSE", paper=True,
        )
        tmp_positions_file.write_text(json.dumps([{
            "order_id": "ORD-LOSE",
            "mfe_cents": 15,
            "mfe_at": "2026-04-25T11:00:00+00:00",
        }]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("NO")):
            clv.check_clv_settlements()
        rec = _read(tmp_clv_file)[0]
        assert rec["clv_cents"] == -78.0
        # No change for loser.
        assert rec["mfe_cents"] == 15
        assert rec["mfe_at"] == "2026-04-25T11:00:00+00:00"

    def test_no_matching_position_winner_seeds_mfe_to_clv(
        self, tmp_clv_file, tmp_positions_file
    ):
        # CLV record has no matching pos (e.g., positions purged).
        # Winner case: existing mfe_cents=None, clv=70. Extension sets
        # mfe_cents=70, mfe_at=settled_at. Report can include this record.
        clv.record_clv_entry(
            ticker="KXLM-NOPOS", opp_type="live_momentum", side="yes",
            entry_price_cents=30, fair_value_cents=50.0, edge_at_trade=0.20,
            contracts=1, trade_id="ORD-NOPOS", paper=True,
        )
        # Empty positions.json — pos lookup misses
        tmp_positions_file.write_text(json.dumps([]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("YES")):
            clv.check_clv_settlements()
        rec = _read(tmp_clv_file)[0]
        assert rec["clv_cents"] == 70.0
        assert rec["mfe_cents"] == 70
        assert rec["mfe_at"] == rec["settled_at"]

    def test_no_matching_position_loser_leaves_mfe_none(
        self, tmp_clv_file, tmp_positions_file
    ):
        # Loser, no matching pos: clv=-78. max(0, -78)=0. existing_mfe is
        # None, so 0 > None evaluates True (None special-case in extension).
        # Result: mfe_cents=0 (clamped). Report can still include — gap = 0 - (-78) = 78.
        clv.record_clv_entry(
            ticker="KXLM-NOPOS-LOSE", opp_type="live_momentum", side="yes",
            entry_price_cents=78, fair_value_cents=85.0, edge_at_trade=0.10,
            contracts=1, trade_id="ORD-NOPOS-LOSE", paper=True,
        )
        tmp_positions_file.write_text(json.dumps([]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("NO")):
            clv.check_clv_settlements()
        rec = _read(tmp_clv_file)[0]
        assert rec["clv_cents"] == -78.0
        # mfe_cents was None pre-settlement; extension clamp gives 0.
        # Report load filter requires mfe_cents is not None — 0 passes.
        assert rec["mfe_cents"] == 0
        # gap = 0 - (-78) = 78 ✓
        assert rec["mfe_cents"] - rec["clv_cents"] == 78.0

    def test_counterfactual_record_untouched_by_extension(
        self, tmp_clv_file, tmp_positions_file
    ):
        # CF records skip the entire if-not-is-cf block. mfe_cents stays None.
        clv.record_counterfactual_skip(
            {
                "ticker": "KXVS-CF",
                "opp_type": "vig_stack_series",
                "side": "no",
                "price_cents": 92,
                "fair_value_cents": 95.0,
                "edge": 0.05,
            },
            "edge_below_threshold",
            "SCAN-X",
        )
        tmp_positions_file.write_text(json.dumps([]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("YES")):
            clv.check_clv_settlements()
        rec = _read(tmp_clv_file)[0]
        assert rec["status"] == "counterfactual_settled"
        # MFE extension didn't fire for CF — mfe_cents stays None.
        assert rec.get("mfe_cents") is None

    def test_idempotency_on_repeat_check(self, tmp_clv_file, tmp_positions_file):
        # Running check_clv_settlements twice on the same fixtures must
        # produce identical output. After the first run the record is
        # status="settled" and the loop's status filter skips it.
        clv.record_clv_entry(
            ticker="KXLM-IDEM", opp_type="live_momentum", side="yes",
            entry_price_cents=77, fair_value_cents=85.0, edge_at_trade=0.10,
            contracts=1, trade_id="ORD-IDEM", paper=True,
        )
        tmp_positions_file.write_text(json.dumps([{
            "order_id": "ORD-IDEM",
            "mfe_cents": 22,
            "mfe_at": "2026-04-25T10:00:00+00:00",
        }]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("YES")):
            clv.check_clv_settlements()
        rec_after_first = dict(_read(tmp_clv_file)[0])
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("YES")):
            clv.check_clv_settlements()
        rec_after_second = _read(tmp_clv_file)[0]
        assert rec_after_first == rec_after_second

    def test_extension_only_advances_mfe_at_when_value_changes(
        self, tmp_clv_file, tmp_positions_file
    ):
        # Sister to test_observed_mfe_above_clv_no_change. Confirms mfe_at
        # is preserved (not advanced to settled_at) when the value didn't
        # change. Locks the gating logic — we only update both fields
        # together, never one without the other.
        clv.record_clv_entry(
            ticker="KXLM-FROZEN", opp_type="live_momentum", side="yes",
            entry_price_cents=70, fair_value_cents=80.0, edge_at_trade=0.10,
            contracts=1, trade_id="ORD-FROZEN", paper=True,
        )
        original_mfe_at = "2026-04-25T08:30:00+00:00"
        tmp_positions_file.write_text(json.dumps([{
            "order_id": "ORD-FROZEN",
            # mfe equals clv (30); ratchet condition (clv > existing) is False
            "mfe_cents": 30,
            "mfe_at": original_mfe_at,
        }]))
        with patch("agent.kalshi_client.get_market", return_value=_settled_market("YES")):
            clv.check_clv_settlements()
        rec = _read(tmp_clv_file)[0]
        assert rec["clv_cents"] == 30.0
        assert rec["mfe_cents"] == 30
        # No change → mfe_at NOT advanced to settled_at
        assert rec["mfe_at"] == original_mfe_at
