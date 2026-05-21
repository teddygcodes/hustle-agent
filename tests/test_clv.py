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
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase",
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
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase",
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

        with patch("agent.kalshi_client.get_markets",
                   return_value=_settled_batch("KXHIGHMIA-26APR24-T80", "YES")):
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
        with patch("agent.kalshi_client.get_markets",
                   return_value=_settled_batch("KXHIGHMIA-26APR24-T80", "NO")):
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
    """Mock get_market response for a settled market (per-record fallback path)."""
    return {
        "market": {
            "status": "settled",
            "result": result,
            "yes_bid": 0,
            "yes_ask": 0,
        }
    }


def _settled_batch(ticker: str, result: str = "YES", **extra) -> dict:
    """Mock get_markets response for the S152 batch path: one settled market
    keyed by `ticker` (the batch matches records by exact ticker)."""
    m = {"ticker": ticker, "status": "settled", "result": result,
         "yes_bid": 0, "yes_ask": 0}
    m.update(extra)
    return {"markets": [m], "cursor": None}


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
        with patch("agent.kalshi_client.get_markets",
                   return_value=_settled_batch("KXLM-NOPOS-LOSE", "NO")):
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


# ---------------------------------------------------------------------------
# Session 23 (Apr 27+) — live_momentum counterfactuals
# ---------------------------------------------------------------------------

from datetime import datetime, timezone, timedelta  # noqa: E402


def _lm_kwargs(**overrides) -> dict:
    """Default kwargs for record_live_momentum_counterfactual_skip."""
    base = {
        "ticker": "KXATPMATCH-26APR27ALCSIN-ALC",
        "sport": "atp",
        "skip_reason": "no_leader",
        "side": "yes",
        "entry_price_cents": 60,
        "opponent_ticker": "KXATPMATCH-26APR27ALCSIN-SIN",
        "measured_value": 60.0,
        "threshold_value": 65.0,
        "scan_event_ts": datetime(2026, 4, 27, 18, 30, 21, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


class TestLiveMomentumCounterfactual:
    """Mirrors TestStratifiedCFSampling for the live_watcher tick-replay path."""

    def test_record_writes_expected_schema(self, tmp_clv_file):
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs())
        recs = _read(tmp_clv_file)
        assert len(recs) == 1
        r = recs[0]
        assert r["opp_type"] == "live_momentum"
        assert r["status"] == "counterfactual_open"
        assert r["paper"] is True
        assert r["contracts"] == 0
        assert r["sport"] == "atp"
        assert r["side"] == "yes"
        assert r["entry_price_cents"] == 60
        assert r["opponent_ticker"] == "KXATPMATCH-26APR27ALCSIN-SIN"
        assert r["skipped_by_gate"] == "no_leader"
        assert r["measured_value"] == 60.0
        assert r["threshold_value"] == 65.0
        assert r["closing_yes_price"] is None
        assert r["clv_cents"] is None
        assert r["trade_id"] == "CF-LM-20260427-KXATPMATCH-26APR27ALCSIN-ALC"
        # Regime tagged at write time (Session 14 discipline; 6 writers + this = 7).
        assert "regime" in r
        assert set(r["regime"].keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase",
        }

    def test_idempotent_on_repeat_call(self, tmp_clv_file):
        # Same (ticker, sport, skip_reason, day) → one record (Session 86 dedup semantic).
        for _ in range(3):
            clv.record_live_momentum_counterfactual_skip(**_lm_kwargs())
        assert len(_read(tmp_clv_file)) == 1

    def test_skips_invalid_skip_reason(self, tmp_clv_file):
        # Defensive allowlist — caller bug shouldn't pollute clv.json with
        # unactionable rows (not_today is structural per Session 21-followup).
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(skip_reason="not_today"))
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(skip_reason="bad_event_shape"))
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(skip_reason="disabled_sport"))
        assert _read(tmp_clv_file) == []

    def test_skips_zero_or_negative_entry_price(self, tmp_clv_file):
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(entry_price_cents=0))
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(entry_price_cents=-5))
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(entry_price_cents=None))
        assert _read(tmp_clv_file) == []

    def test_per_day_cap_enforced_per_sport_skip_reason(self, tmp_clv_file):
        # 10 candidates for (ufc, no_leader) on the same UTC day → only 5
        # written. Then (nba, no_leader) goes through (cross-sport isolation).
        # Then (ufc, low_volume) goes through (cross-skip_reason isolation).
        base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            ts = base + timedelta(minutes=i)
            ok = clv._should_emit_live_momentum_cf(
                sport="ufc", skip_reason="no_leader", now=ts,
            )
            if ok:
                clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(
                    ticker=f"KXUFC-{i}",
                    sport="ufc",
                    skip_reason="no_leader",
                    scan_event_ts=ts,
                ))
        ufc_no_leader = [r for r in _read(tmp_clv_file)
                         if r["sport"] == "ufc" and r["skipped_by_gate"] == "no_leader"]
        assert len(ufc_no_leader) == 5

        # Cross-sport: (nba, no_leader) is its own bucket.
        ts_nba = base + timedelta(hours=1)
        assert clv._should_emit_live_momentum_cf(
            sport="nba", skip_reason="no_leader", now=ts_nba,
        ) is True
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(
            ticker="KXNBA-1", sport="nba", skip_reason="no_leader", scan_event_ts=ts_nba,
        ))
        # Cross-skip_reason: (ufc, low_volume) is its own bucket.
        ts_ufc_lv = base + timedelta(hours=2)
        assert clv._should_emit_live_momentum_cf(
            sport="ufc", skip_reason="low_volume", now=ts_ufc_lv,
        ) is True

    def test_per_day_cap_resets_at_utc_midnight(self, tmp_clv_file, monkeypatch):
        # Fill the cap on day 1; day 2 (UTC midnight rollover) gets a fresh cap.
        # Production sets `recorded_at = datetime.now(...)` (wall-clock write time, NOT
        # `scan_event_ts` — by design: the cap shouldn't be vulnerable to back-dated
        # scan_event_ts injection). To exercise the day1/day2 boundary deterministically,
        # monkeypatch bot.clv.datetime so day-1 writes are stamped with day-1 wall-clock.
        day1 = datetime(2026, 4, 27, 23, 0, 0, tzinfo=timezone.utc)
        day2 = datetime(2026, 4, 28, 0, 30, 0, tzinfo=timezone.utc)

        frozen = {"now": day1}

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                t = frozen["now"]
                return t if tz is None else t.astimezone(tz)
        monkeypatch.setattr(clv, "datetime", _FrozenDatetime)

        for i in range(5):
            frozen["now"] = day1 + timedelta(minutes=i)
            clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(
                ticker=f"KXUFC-D1-{i}",
                sport="ufc",
                skip_reason="no_leader",
                scan_event_ts=day1 + timedelta(minutes=i),
            ))

        # Cap check uses `now=day2` directly, doesn't depend on the patched class for
        # `today_start_utc(now)`. Records' `recorded_at` is on day1 → count=0 at day2.
        assert clv._should_emit_live_momentum_cf(
            sport="ufc", skip_reason="no_leader", now=day2,
        ) is True

    def test_settlement_flips_status_and_fills_fields(
        self, tmp_clv_file, tmp_positions_file
    ):
        # Live_momentum CF flows through check_clv_settlements unchanged
        # (opp_type-agnostic poller). YES @60, settles YES (closing=100 → clv=40).
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs())
        tmp_positions_file.write_text(json.dumps([]))
        with patch("agent.kalshi_client.get_markets",
                   return_value=_settled_batch("KXATPMATCH-26APR27ALCSIN-ALC", "YES")):
            settled_now = clv.check_clv_settlements()
        # CF settlements are NOT in the return list (Telegram noise filter).
        assert settled_now == []
        rec = _read(tmp_clv_file)[0]
        assert rec["status"] == "counterfactual_settled"
        assert rec["closing_yes_price"] == 100
        assert rec["clv_cents"] == 40.0
        # MFE extension skipped for CFs (Session 16 gates on `not is_cf`).
        assert rec.get("mfe_cents") is None

    def test_record_works_when_market_state_missing_close_ts(self, tmp_clv_file):
        # no_vol_growth_idle hooks pass market_state=None — regime tag still
        # populates the 4-axis dict but event_horizon_hr stays None.
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(
            skip_reason="no_vol_growth_idle",
            measured_value=320.0,
            threshold_value=500.0,
            market_state=None,
        ))
        rec = _read(tmp_clv_file)[0]
        assert rec["regime"]["event_horizon_hr"] is None
        # Other 3 regime axes still derived from scan_event_ts + ticker.
        assert rec["regime"]["day_of_week"] is not None
        assert rec["regime"]["time_of_day"] is not None

    def test_per_day_dedup_same_ticker_same_skip_reason(self, tmp_clv_file, monkeypatch):
        """Session 86: same (ticker, sport, skip_reason) within one UTC day → one row only.

        Pre-Session-86 the trade_id used %Y%m%dT%H%M%SZ precision, so two emits 4
        seconds apart produced different trade_ids and both rows landed. Session 85
        quantified this on no_vol_growth_first_seen/nhl_game (33 raw → 17 unique).
        """
        # Bypass the per-(sport, skip_reason)-per-day cap so this test isolates dedup.
        monkeypatch.setattr(clv, "_should_emit_live_momentum_cf", lambda **kw: True)

        ts_a = datetime(2026, 5, 8, 14, 30, 22, tzinfo=timezone.utc)
        ts_b = datetime(2026, 5, 8, 14, 30, 26, tzinfo=timezone.utc)  # 4s later

        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(
            ticker="KXNHLGAME-26MAY08TEST",
            sport="nhl",
            skip_reason="no_vol_growth_first_seen",
            scan_event_ts=ts_a,
        ))
        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(
            ticker="KXNHLGAME-26MAY08TEST",
            sport="nhl",
            skip_reason="no_vol_growth_first_seen",
            scan_event_ts=ts_b,
        ))

        rows = _read(tmp_clv_file)
        assert len(rows) == 1, f"expected 1 row after per-day dedup, got {len(rows)}: {rows}"

    def test_per_day_dedup_cross_day_same_ticker_stays_distinct(self, tmp_clv_file, monkeypatch):
        """Session 86: same ticker emitted on May 8 AND May 9 → two rows.

        BOSBUF and MINCOL exhibited this pattern in production data: a game that
        starts late evening US time spans UTC midnight, and pre-game first-sight
        on day-of-game (UTC date 1) plus in-game first-sight (UTC date 2) are
        distinct decision points worth preserving as separate CF records.
        """
        monkeypatch.setattr(clv, "_should_emit_live_momentum_cf", lambda **kw: True)

        ts_day1 = datetime(2026, 5, 8, 23, 50, tzinfo=timezone.utc)
        ts_day2 = datetime(2026, 5, 9, 0, 10, tzinfo=timezone.utc)  # 20 min later, next UTC day

        for ts in (ts_day1, ts_day2):
            clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(
                ticker="KXNHLGAME-26MAY08CROSSDAY",
                sport="nhl",
                skip_reason="no_vol_growth_first_seen",
                scan_event_ts=ts,
            ))

        rows = _read(tmp_clv_file)
        assert len(rows) == 2, f"expected 2 cross-day rows, got {len(rows)}: {rows}"

    def test_per_day_dedup_legacy_second_precision_blocks_new_day_precision(
        self, tmp_clv_file, monkeypatch,
    ):
        """Session 86: a pre-Session-86 second-precision row from earlier today
        blocks a Session-86 day-precision emit for the same (ticker, sport, skip_reason).

        Without the semantic (ticker, sport, skip_reason, day) check, the 30-day
        transition window would allow same-day double-counts: legacy row
        (second-precision trade_id) + new row (day-precision trade_id) would both
        land because trade_id strings don't match.
        """
        monkeypatch.setattr(clv, "_should_emit_live_momentum_cf", lambda **kw: True)

        # Hand-write a legacy-format row simulating a pre-Session-86 entry from earlier today.
        legacy_row = {
            "ticker": "KXNHLGAME-26MAY08LEGACY",
            "opp_type": "live_momentum",
            "sport": "nhl",
            "side": "yes",
            "entry_price_cents": 70,
            "trade_id": "CF-LM-20260508T100000Z-KXNHLGAME-26MAY08LEGACY",  # second precision
            "scan_event_ts": "2026-05-08T10:00:00+00:00",
            "recorded_at": "2026-05-08T10:00:00.000+00:00",
            "status": "counterfactual_open",
            "skipped_by_gate": "no_vol_growth_first_seen",
            "contracts": 0,
            "paper": True,
        }
        tmp_clv_file.write_text(json.dumps([legacy_row]))

        ts_now = datetime(2026, 5, 8, 14, 30, 0, tzinfo=timezone.utc)  # later same UTC day

        clv.record_live_momentum_counterfactual_skip(**_lm_kwargs(
            ticker="KXNHLGAME-26MAY08LEGACY",
            sport="nhl",
            skip_reason="no_vol_growth_first_seen",
            scan_event_ts=ts_now,
        ))

        rows = _read(tmp_clv_file)
        assert len(rows) == 1, (
            f"expected 1 row (legacy second-precision blocks new day-precision), "
            f"got {len(rows)}: {rows}"
        )


class TestSession151BatchCap:
    """Session 151 (2026-05-18): check_clv_settlements caps per-call work
    at _CHECK_CLV_BATCH_SIZE=200 records to bound runtime inside the 900s
    S150 outer guard. Records past the cap roll over to the next call.
    Oldest recorded_at processed first — backlog drains FIFO.

    Phase 0 measured 3,528 open CF records in production (2026-05-18) —
    sequential traversal under the S146 rate limiter (1 call per ~3s)
    would take ~176 minutes, triggering the 900s guard every iteration
    and leaking executor threads. The batch cap eliminates that wedge
    class while preserving full record coverage across iterations.
    """

    def _make_open_cf(self, idx: int, recorded_at: str, ticker: str | None = None) -> dict:
        """Build a counterfactual_open record. opp_type kept in active set
        so clv._load()'s active-strategy filter doesn't drop it."""
        return {
            "trade_id": f"CF-S151-{idx:04d}",
            "ticker": ticker or f"KXFAKE-{idx:04d}",
            "opp_type": "vig_stack_series",
            "side": "no",
            "entry_price_cents": 92,
            "fair_value_cents": 95.5,
            "edge_at_trade": 0.08,
            "contracts": 0,
            "paper": False,
            "status": "counterfactual_open",
            "skipped_by_gate": "edge_below_threshold",
            "recorded_at": recorded_at,
            "closing_yes_price": None,
            "clv_cents": None,
        }

    def _stamps(self, n: int) -> list[str]:
        """Generate n strictly-increasing ISO 8601 timestamps."""
        from datetime import datetime, timezone, timedelta
        base = datetime(2026, 4, 1, tzinfo=timezone.utc)
        return [
            (base + timedelta(minutes=i)).isoformat()
            for i in range(n)
        ]

    def test_caps_to_batch_size_when_backlog_exceeds(
        self, tmp_clv_file, tmp_positions_file
    ):
        """N=250 open records → exactly 200 queried this call, oldest first."""
        stamps = self._stamps(250)
        records = [
            self._make_open_cf(i, stamps[i])
            for i in range(250)
        ]
        tmp_clv_file.write_text(json.dumps(records))

        queried: list[str] = []

        def mock_get_market(ticker):
            queried.append(ticker)
            # Return still-open so records stay open; isolate the cap behavior
            # from the settlement-write behavior (tested elsewhere).
            return {"market": {"status": "open", "result": "", "yes_bid": 50, "yes_ask": 55}}

        with patch("agent.kalshi_client.get_market", side_effect=mock_get_market):
            clv.check_clv_settlements()

        assert len(queried) == clv._CHECK_CLV_BATCH_SIZE, (
            f"expected exactly {clv._CHECK_CLV_BATCH_SIZE} queries, got {len(queried)}"
        )
        # Oldest-first FIFO: the first 200 by recorded_at must have been queried.
        expected_oldest = [f"KXFAKE-{i:04d}" for i in range(clv._CHECK_CLV_BATCH_SIZE)]
        assert queried == expected_oldest

    def test_does_not_cap_when_backlog_under_limit(
        self, tmp_clv_file, tmp_positions_file
    ):
        """N=50 open records → all 50 queried in one call (no cap fires)."""
        stamps = self._stamps(50)
        records = [
            self._make_open_cf(i, stamps[i])
            for i in range(50)
        ]
        tmp_clv_file.write_text(json.dumps(records))

        queried: list[str] = []

        def mock_get_market(ticker):
            queried.append(ticker)
            return {"market": {"status": "open", "result": "", "yes_bid": 50, "yes_ask": 55}}

        with patch("agent.kalshi_client.get_market", side_effect=mock_get_market):
            clv.check_clv_settlements()

        assert len(queried) == 50

    def test_already_settled_records_excluded_before_cap(
        self, tmp_clv_file, tmp_positions_file
    ):
        """100 settled + 250 open → 200 open queried (cap fires on the OPEN
        subset, not on the total). The filter must run before the slice or
        a backlog full of settled records would starve the open subset."""
        stamps = self._stamps(350)
        records: list[dict] = []
        # 100 already-settled records (older recorded_at — would sort first if
        # the filter ran AFTER the sort+slice, starving open records).
        for i in range(100):
            r = self._make_open_cf(i, stamps[i], ticker=f"KXMIXS-{i:04d}")
            r["status"] = "counterfactual_settled"
            r["closing_yes_price"] = 100.0
            r["clv_cents"] = 8.0
            r["clv_relative"] = 0.087
            r["settled_at"] = stamps[i]
            r["market_result"] = "yes"
            records.append(r)
        # 250 still-open records.
        for i in range(250):
            records.append(
                self._make_open_cf(
                    100 + i, stamps[100 + i], ticker=f"KXMIXO-{i:04d}"
                )
            )
        tmp_clv_file.write_text(json.dumps(records))

        queried: list[str] = []

        def mock_get_market(ticker):
            queried.append(ticker)
            return {"market": {"status": "open", "result": "", "yes_bid": 50, "yes_ask": 55}}

        with patch("agent.kalshi_client.get_market", side_effect=mock_get_market):
            clv.check_clv_settlements()

        # Exactly 200 open queried — not 200 mixed, not 350 total.
        assert len(queried) == clv._CHECK_CLV_BATCH_SIZE
        # Every queried ticker is from the OPEN set (KXMIXO-...), never settled.
        assert all(t.startswith("KXMIXO-") for t in queried), (
            f"settled records leaked into batch: "
            f"{[t for t in queried if not t.startswith('KXMIXO-')][:5]}"
        )
        # Specifically the 200 oldest open ones.
        expected = [f"KXMIXO-{i:04d}" for i in range(clv._CHECK_CLV_BATCH_SIZE)]
        assert queried == expected


class TestSession152SettlementHelper:
    """S152: pin the full settlement-write behavior before extracting it into
    _apply_clv_settlement (must stay byte-identical for batch + fallback paths)."""

    def test_real_trade_settlement_writes_all_fields(self, tmp_clv_file, tmp_positions_file):
        rec = {"ticker": "KXHIGHMIA-26APR24-T80", "opp_type": "vig_stack_series",
               "side": "no", "entry_price_cents": 92, "contracts": 1,
               "trade_id": "PAPER-1", "paper": True, "status": "open",
               "recorded_at": "2026-04-24T12:00:00+00:00", "closing_yes_price": None,
               "clv_cents": None, "clv_relative": None, "settled_at": None}
        tmp_clv_file.write_text(json.dumps([rec]))
        tmp_positions_file.write_text(json.dumps(
            [{"order_id": "PAPER-1", "mfe_cents": 5, "mae_cents": -3, "ticks_observed": 12}]))
        with patch("agent.kalshi_client.get_markets",
                   return_value=_settled_batch("KXHIGHMIA-26APR24-T80", "NO")):
            settled = clv.check_clv_settlements()
        r = _read(tmp_clv_file)[0]
        assert r["status"] == "settled"
        assert r["closing_yes_price"] == 0
        assert r["market_result"] == "NO"
        assert r["clv_cents"] == 8.0          # NO: (100-92) - 0 = 8
        assert r["mae_cents"] == -3 and r["ticks_observed"] == 12
        assert r["mfe_cents"] == 8            # S16: max(existing 5, settlement 8)
        assert len(settled) == 1


class TestSession152Grouping:
    """S152: group derivation + batched settled-market fetch."""

    def test_event_ticker_derivation(self):
        assert clv._event_ticker("KXHIGHMIA-26APR24-T80") == "KXHIGHMIA-26APR24"
        assert clv._event_ticker("KXNBA-26-OKC") == "KXNBA-26"
        assert clv._event_ticker("KXATPMATCH-26MAY21ABCDEF-A") == "KXATPMATCH-26MAY21ABCDEF"
        assert clv._event_ticker("KXHIGHDEN-A") is None      # 2-seg -> fallback
        assert clv._event_ticker("") is None

    def test_fetch_settled_paginates_and_maps_by_ticker(self):
        pages = [
            {"markets": [{"ticker": "KXHIGHNY-26MAY20-T70", "status": "settled", "result": "yes"}],
             "cursor": "c2"},
            {"markets": [{"ticker": "KXHIGHNY-26MAY20-T72", "status": "settled", "result": "no"}],
             "cursor": None},
        ]
        with patch("agent.kalshi_client.get_markets", side_effect=pages):
            m = clv._fetch_settled_markets("KXHIGHNY-26MAY20")
        assert set(m) == {"KXHIGHNY-26MAY20-T70", "KXHIGHNY-26MAY20-T72"}
        assert m["KXHIGHNY-26MAY20-T70"]["result"] == "yes"

    def test_fetch_settled_handles_error(self):
        with patch("agent.kalshi_client.get_markets", return_value={"error": "boom"}):
            assert clv._fetch_settled_markets("KXX-26") == {}


class TestSession152BatchSettlement:
    """S152: event-batch settle path + per-record fallback for un-groupable tickers."""

    def _open(self, ticker, side="no", price=92, stamp="2026-04-24T12:00:00+00:00",
              tid="PAPER-X", status="open"):
        return {"ticker": ticker, "opp_type": "vig_stack_series", "side": side,
                "entry_price_cents": price, "contracts": 1, "trade_id": tid,
                "paper": True, "status": status, "recorded_at": stamp,
                "closing_yes_price": None, "clv_cents": None, "clv_relative": None,
                "settled_at": None}

    def test_one_fetch_settles_all_rungs_in_event(self, tmp_clv_file, tmp_positions_file):
        # 3 open rungs, same event -> ONE get_markets call settles all 3.
        recs = [self._open(f"KXHIGHNY-26MAY20-T{t}", tid=f"PAPER-{t}") for t in (70, 72, 74)]
        tmp_clv_file.write_text(json.dumps(recs)); tmp_positions_file.write_text("[]")
        calls = {"n": 0}

        def fake_markets(**kw):
            calls["n"] += 1
            return {"markets": [{"ticker": f"KXHIGHNY-26MAY20-T{t}", "status": "settled",
                                 "result": "yes"} for t in (70, 72, 74)], "cursor": None}

        with patch("agent.kalshi_client.get_markets", side_effect=fake_markets):
            settled = clv.check_clv_settlements()
        assert calls["n"] == 1                       # ONE call, not 3
        rows = _read(tmp_clv_file)
        assert all(r["status"] == "settled" for r in rows)
        assert all(r["closing_yes_price"] == 100 for r in rows)
        assert len(settled) == 3

    def test_cf_settles_via_batch_to_counterfactual_settled(self, tmp_clv_file, tmp_positions_file):
        rec = self._open("KXHIGHNY-26MAY20-T70", tid="CF-S1-KXHIGHNY-26MAY20-T70",
                         status="counterfactual_open")
        tmp_clv_file.write_text(json.dumps([rec])); tmp_positions_file.write_text("[]")
        with patch("agent.kalshi_client.get_markets", return_value={"markets":
                [{"ticker": "KXHIGHNY-26MAY20-T70", "status": "settled", "result": "no"}],
                "cursor": None}):
            settled = clv.check_clv_settlements()
        r = _read(tmp_clv_file)[0]
        assert r["status"] == "counterfactual_settled"
        assert settled == []                          # CFs excluded from notify list

    def test_open_market_in_event_leaves_record_open(self, tmp_clv_file, tmp_positions_file):
        rec = self._open("KXHIGHNY-26MAY20-T70")
        tmp_clv_file.write_text(json.dumps([rec])); tmp_positions_file.write_text("[]")
        with patch("agent.kalshi_client.get_markets", return_value={"markets": [], "cursor": None}):
            clv.check_clv_settlements()                # status="settled" returns nothing
        assert _read(tmp_clv_file)[0]["status"] == "open"

    def test_ungroupable_ticker_uses_per_record_fallback(self, tmp_clv_file, tmp_positions_file):
        rec = self._open("KXHIGHDEN-A")               # 2-seg -> _event_ticker None
        tmp_clv_file.write_text(json.dumps([rec])); tmp_positions_file.write_text("[]")
        with patch("agent.kalshi_client.get_markets") as gm, \
             patch("agent.kalshi_client.get_market", return_value={"market":
                {"status": "settled", "result": "no", "yes_bid": 0, "yes_ask": 0}}) as g1:
            clv.check_clv_settlements()
        gm.assert_not_called()                         # not batchable
        g1.assert_called_once()                        # fell back to per-record
        assert _read(tmp_clv_file)[0]["status"] == "settled"

    def test_event_budget_cap(self, tmp_clv_file, tmp_positions_file):
        # >cap distinct events; only the oldest _CHECK_CLV_BATCH_SIZE are fetched.
        n = clv._CHECK_CLV_BATCH_SIZE + 5
        recs = [self._open(f"KXHIGHNY-26MAY{d:03d}-T70", tid=f"P{d}",
                           stamp=f"2026-05-{(d % 28) + 1:02d}T00:00:00+00:00") for d in range(n)]
        tmp_clv_file.write_text(json.dumps(recs)); tmp_positions_file.write_text("[]")
        fetched = []

        def fake(**kw):
            fetched.append(kw.get("event_ticker") or kw.get("series_ticker"))
            return {"markets": [], "cursor": None}

        with patch("agent.kalshi_client.get_markets", side_effect=fake):
            clv.check_clv_settlements()
        assert len(fetched) == clv._CHECK_CLV_BATCH_SIZE   # capped


class TestSession152Prune:
    """S152: terminal-mark records whose market will never produce a binary CLV
    (malformed shape with no API call; confirmed 404 in the per-record path)."""

    def _rec(self, ticker, tid, status="counterfactual_open", stamp="2026-04-20T00:00:00+00:00"):
        return {"ticker": ticker, "opp_type": "vig_stack_series", "side": "no",
                "entry_price_cents": 50, "contracts": 0, "trade_id": tid, "paper": False,
                "status": status, "recorded_at": stamp, "closing_yes_price": None,
                "clv_cents": None, "clv_relative": None, "settled_at": None}

    def test_dead_event_shape_terminalized_without_api(self, tmp_clv_file, tmp_positions_file):
        recs = [self._rec("KXHIGHNY-26APR-70", "CF-1-KXHIGHNY-26APR-70"),
                self._rec("KXTEST-26APR-1", "CF-1-KXTEST-26APR-1")]
        tmp_clv_file.write_text(json.dumps(recs)); tmp_positions_file.write_text("[]")
        with patch("agent.kalshi_client.get_markets") as gm, \
             patch("agent.kalshi_client.get_market") as g1:
            clv.check_clv_settlements()
        gm.assert_not_called(); g1.assert_not_called()        # no API for dead shapes
        rows = _read(tmp_clv_file)
        assert all(r["status"] == "settlement_failed" for r in rows)
        assert all(r["settlement_note"] == "malformed_ticker" for r in rows)

    def test_confirmed_404_terminalized(self, tmp_clv_file, tmp_positions_file):
        rec = self._rec("KXHIGHDEN-A", "CF-1-KXHIGHDEN-A")     # 2-seg -> fallback
        tmp_clv_file.write_text(json.dumps([rec])); tmp_positions_file.write_text("[]")
        with patch("agent.kalshi_client.get_market",
                   return_value={"error": "Kalshi API error: HTTP Error 404: Not Found"}):
            clv.check_clv_settlements()
        r = _read(tmp_clv_file)[0]
        assert r["status"] == "settlement_failed"
        assert r["settlement_note"] == "market_not_found"

    def test_active_market_not_terminalized(self, tmp_clv_file, tmp_positions_file):
        # un-groupable ticker, get_market says active -> LEFT open (terminal-mark
        # is 404-only; never terminalizes a live/slow market).
        rec = self._rec("KXFOO-BAR", "CF-1-x")     # 2-seg -> per-record fallback
        tmp_clv_file.write_text(json.dumps([rec])); tmp_positions_file.write_text("[]")
        with patch("agent.kalshi_client.get_market",
                   return_value={"status": "active", "result": "", "yes_bid": 40, "yes_ask": 45}):
            clv.check_clv_settlements()
        assert _read(tmp_clv_file)[0]["status"] == "counterfactual_open"   # untouched

    def test_futures_not_dead_marked(self, tmp_clv_file, tmp_positions_file):
        # season futures (SERIES-YY-TEAM) are valid open markets -> grouped and
        # batch-checked, never terminal-marked by the malformed-shape fast-path.
        rec = self._rec("KXNBA-26-OKC", "CF-1-y")
        tmp_clv_file.write_text(json.dumps([rec])); tmp_positions_file.write_text("[]")
        with patch("agent.kalshi_client.get_markets", return_value={"markets": [], "cursor": None}):
            clv.check_clv_settlements()
        assert _read(tmp_clv_file)[0]["status"] == "counterfactual_open"   # not dead-marked


class TestSession155DrainPersistence:
    """S155: check_clv_settlements must DRAIN, not discard, on a 900s abort.

    The S152 batch shipped but its first live cycle (2026-05-21) persisted
    NOTHING — the only _save was at cycle-end, so the S150 900s outer guard
    aborted the await and threw away every in-memory settlement AND the cheap
    zero-API dead-marks. These pin the persistence fixes:
      (1) dead-marks persist BEFORE the slow fetch (independent of it),
      (2) _persist_progress re-load-merge preserves concurrent CF appends.
    """

    def _rec(self, ticker, tid, status="counterfactual_open",
             stamp="2026-04-20T00:00:00+00:00", opp_type="vig_stack_series"):
        return {"ticker": ticker, "opp_type": opp_type, "side": "no",
                "entry_price_cents": 92, "contracts": 0, "trade_id": tid,
                "paper": False, "status": status, "recorded_at": stamp,
                "closing_yes_price": None, "clv_cents": None,
                "clv_relative": None, "settled_at": None}

    def test_dead_marks_persist_before_fetch_even_if_fetch_raises(
        self, tmp_clv_file, tmp_positions_file
    ):
        """Dead-marks are persisted up-front, so a fetch that never reaches the
        final persist (proxy for the 900s abort: _fetch_settled_markets raises)
        cannot discard them. The groupable record stays open."""
        recs = [
            self._rec("KXHIGHNY-26APR-70", "CF-DEAD-1"),       # dead shape (no day)
            self._rec("KXHIGHNY-26APR-71", "CF-DEAD-2"),       # dead shape (no day)
            self._rec("KXHIGHMIA-26APR24-T80", "CF-LIVE-1"),   # groupable, real event
        ]
        tmp_clv_file.write_text(json.dumps(recs))
        tmp_positions_file.write_text("[]")

        def boom(_key):
            raise RuntimeError("simulated wedge / abort before final persist")

        with patch.object(clv, "_fetch_settled_markets", side_effect=boom):
            with pytest.raises(RuntimeError):
                clv.check_clv_settlements()

        by_id = {r["trade_id"]: r for r in _read(tmp_clv_file)}
        assert by_id["CF-DEAD-1"]["status"] == "settlement_failed"
        assert by_id["CF-DEAD-2"]["status"] == "settlement_failed"
        assert by_id["CF-DEAD-1"]["settlement_note"] == "malformed_ticker"
        # Groupable record never settled (fetch raised) — still open.
        assert by_id["CF-LIVE-1"]["status"] == "counterfactual_open"

    def test_persist_progress_preserves_concurrent_cf_append(
        self, tmp_clv_file, tmp_positions_file
    ):
        """live_watcher appends a CF mid-cycle (clv has no shared write lock).
        _persist_progress re-loads fresh and replaces only our mutated trade_ids,
        so the concurrent append is NOT clobbered by the settlement write."""
        seed = self._rec("KXHIGHMIA-26APR24-T80", "SEED-1")
        tmp_clv_file.write_text(json.dumps([seed]))
        tmp_positions_file.write_text("[]")

        concurrent_cf = self._rec(
            "KXHIGHAUS-26APR25-B80", "CONCURRENT-1", opp_type="live_momentum",
        )

        def append_then_settle(**kwargs):
            # Concurrent live_watcher append landing between cycle-start _load
            # and the final _persist_progress.
            rows = _read(tmp_clv_file)
            rows.append(concurrent_cf)
            tmp_clv_file.write_text(json.dumps(rows))
            return _settled_batch("KXHIGHMIA-26APR24-T80", "NO")

        with patch("agent.kalshi_client.get_markets", side_effect=append_then_settle):
            clv.check_clv_settlements()

        by_id = {r["trade_id"]: r for r in _read(tmp_clv_file)}
        assert by_id["SEED-1"]["status"] == "counterfactual_settled"   # our settle landed
        assert "CONCURRENT-1" in by_id                                  # append survived
        assert by_id["CONCURRENT-1"]["status"] == "counterfactual_open"

    def test_soft_time_budget_stops_group_loop_early(
        self, tmp_clv_file, tmp_positions_file, monkeypatch
    ):
        """Once the wall-clock budget is spent, no new event-fetch is started —
        so the cycle finishes under the 900s guard and remaining groups defer to
        the next cycle. With budget=10s and each group 'costing' 4s, the 4th
        budget check (elapsed 12s) breaks → exactly 3 groups fetched."""
        recs = [
            self._rec(f"KXEV{i:02d}-26APR24-T80", f"CF-EV-{i:02d}",
                      stamp=f"2026-04-20T00:{i:02d}:00+00:00")
            for i in range(10)
        ]
        tmp_clv_file.write_text(json.dumps(recs))
        tmp_positions_file.write_text("[]")

        monkeypatch.setattr(clv, "_CHECK_CLV_TIME_BUDGET_SEC", 10.0)
        fake_now = [0.0]
        monkeypatch.setattr(clv.time, "monotonic", lambda: fake_now[0])

        fetched = []

        def fetch(key):
            fetched.append(key)
            fake_now[0] += 4.0   # each group costs 4s; 0,4,8 under 10 → 3 fetched
            return {}

        monkeypatch.setattr(clv, "_fetch_settled_markets", fetch)

        clv.check_clv_settlements()

        assert len(fetched) == 3, (
            f"expected 3 groups before the 10s budget cut, got {len(fetched)}"
        )

    def test_incremental_persist_survives_hard_mid_fetch_abort(
        self, tmp_clv_file, tmp_positions_file, monkeypatch
    ):
        """A hard abort mid-fetch (proxy for a single fetch hanging into the
        900s guard) must NOT discard already-drained groups. With persist-every
        =2, groups 0+1 are persisted before group 2 raises."""
        recs = [
            self._rec(f"KXEV{i:02d}-26APR24-T80", f"CF-EV-{i:02d}",
                      stamp=f"2026-04-20T00:{i:02d}:00+00:00")
            for i in range(5)
        ]
        tmp_clv_file.write_text(json.dumps(recs))
        tmp_positions_file.write_text("[]")

        monkeypatch.setattr(clv, "_CHECK_CLV_PERSIST_EVERY", 2)

        def fetch(key):
            fetch.calls += 1
            if fetch.calls == 3:
                raise RuntimeError("abort after the first persist boundary")
            ticker = f"{key}-T80"   # event key + rung -> the record's ticker
            return {ticker: {"status": "settled", "result": "NO", "ticker": ticker}}
        fetch.calls = 0

        monkeypatch.setattr(clv, "_fetch_settled_markets", fetch)

        with pytest.raises(RuntimeError):
            clv.check_clv_settlements()

        by_id = {r["trade_id"]: r for r in _read(tmp_clv_file)}
        # Groups 0+1 persisted at the every-2 boundary before group 2 raised.
        assert by_id["CF-EV-00"]["status"] == "counterfactual_settled"
        assert by_id["CF-EV-01"]["status"] == "counterfactual_settled"
        # Groups 2-4 never settled — still open, drain next cycle.
        assert by_id["CF-EV-02"]["status"] == "counterfactual_open"
        assert by_id["CF-EV-04"]["status"] == "counterfactual_open"
