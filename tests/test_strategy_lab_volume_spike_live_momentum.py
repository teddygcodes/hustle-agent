"""Session 75 tests for the volume-spike live_momentum lab prototype."""
from __future__ import annotations

from datetime import date

import pytest

from tools.strategy_lab import evaluator, reports
from tools.strategy_lab.candidate import CandidateOpportunity
from tools.strategy_lab.candidates import volume_spike_live_momentum as vslm


def _row(
    *,
    ticker: str = "KXNBAGAME-26MAY08BOSNYK-BOS",
    yes_ask: int,
    volume_24h: int = 100,
    dqs: float | None = 0.6,
    ts: str = "2026-05-08T12:00:00+00:00",
    scan_id: str = "20260508T120000",
) -> dict:
    row = {
        "ticker": ticker,
        "yes_ask": yes_ask,
        "yes_bid": max(0, yes_ask - 1),
        "volume_24h": volume_24h,
        "status": "active",
        "ts": ts,
        "scan_id": scan_id,
    }
    if dqs is not None:
        row["dqs"] = dqs
    return row


def _prime(
    strat: vslm.VolumeSpikeLiveMomentum,
    *,
    ticker: str = "KXNBAGAME-26MAY08BOSNYK-BOS",
    baseline_volume: int = 100,
    recent_volume: int = 400,
    dqs: float | None = 0.6,
) -> CandidateOpportunity | None:
    prices = [80, 80, 80, 80, 79, 74]
    volumes = [baseline_volume] * 4 + [recent_volume, recent_volume]
    out = None
    for i, (price, volume) in enumerate(zip(prices, volumes)):
        out = strat.evaluate(
            _row(
                ticker=ticker,
                yes_ask=price,
                volume_24h=volume,
                dqs=dqs,
                scan_id=f"TEST-{i}",
            )
        )
    return out


def test_evaluate_emits_for_volume_spike_plus_dip_and_dqs_pass():
    strat = vslm.VolumeSpikeLiveMomentum(
        volume_lookback_ticks=2,
        volume_baseline_ticks=4,
        min_vol_ratio=1.5,
    )

    out = _prime(strat)

    assert out is not None
    assert out.ticker == "KXNBAGAME-26MAY08BOSNYK-BOS"
    assert out.side == "yes"
    assert out.pair_key == "KXNBAGAME-26MAY08BOSNYK-BOS|long"
    assert out.target_price_cents == pytest.approx(74.0)
    assert out.fair_value_cents == pytest.approx(80.0)
    assert out.edge_cents == pytest.approx(6.0)
    assert out.extra is not None
    assert out.extra["volume_ratio"] == pytest.approx(4.0)
    assert out.extra["dqs_mode"] == "row_dqs"


def test_evaluate_blocks_when_volume_ratio_does_not_spike():
    strat = vslm.VolumeSpikeLiveMomentum(
        volume_lookback_ticks=2,
        volume_baseline_ticks=4,
        min_vol_ratio=1.5,
    )

    out = _prime(strat, baseline_volume=100, recent_volume=100)

    assert out is None


def test_evaluate_blocks_when_dqs_fails():
    strat = vslm.VolumeSpikeLiveMomentum(
        volume_lookback_ticks=2,
        volume_baseline_ticks=4,
        min_vol_ratio=1.5,
    )

    out = _prime(strat, dqs=0.1)

    assert out is None


def test_dip_only_baseline_uses_same_price_gates_without_volume_overlay():
    strat = vslm.DipOnlyLiveMomentumBaseline(
        volume_lookback_ticks=2,
        volume_baseline_ticks=4,
        min_vol_ratio=999.0,
    )

    out = _prime(strat, baseline_volume=100, recent_volume=100)

    assert out is not None
    assert out.extra is not None
    assert out.extra["volume_ratio"] is None


def _scored(pair_key: str, ticker: str, pnl_cents: float) -> evaluator.ScoredOpportunity:
    opp = CandidateOpportunity(
        ticker=ticker,
        side="yes",
        target_price_cents=70.0,
        fair_value_cents=75.0,
        edge_cents=5.0,
        confidence=0.5,
        reason="synthetic",
        pair_key=pair_key,
    )
    return evaluator.ScoredOpportunity(
        opp=opp,
        universe_ts="2026-05-08T12:00:00+00:00",
        sport=evaluator._ticker_to_sport(ticker),
        status="settled",
        market_result="yes",
        clv_cents=pnl_cents,
        pnl_cents=pnl_cents * evaluator.DEFAULT_CONTRACTS,
    )


def test_pair_key_dedup_collapses_stateful_reemits_to_one_unique_key():
    scored = [
        _scored("KXNBAGAME-26MAY08BOSNYK-BOS|long", "KXNBAGAME-26MAY08BOSNYK-BOS", 1.0)
        for _ in range(50)
    ]

    summary = evaluator.aggregate(scored)

    assert summary["n_total"] == 50
    assert summary["n_unique_pair_keys"] == 1
    assert summary["n_resolved_pair_keys"] == 1
    assert summary["median_emits_per_pair_key"] == pytest.approx(50.0)


def test_report_flags_single_sport_concentration_in_cross_sport_check():
    scored = [
        _scored(
            f"KXNBAGAME-26MAY08BOSNYK-{team}|long",
            f"KXNBAGAME-26MAY08BOSNYK-{team}",
            2.0,
        )
        for team in ("BOS", "NYK", "BKN")
    ]
    summary = evaluator.aggregate(scored)

    body = reports.render_markdown(
        candidate_name="synthetic_single_sport",
        start=date(2026, 5, 1),
        end=date(2026, 5, 8),
        scored=scored,
        summary=summary,
        days=8,
        universe_rows_seen=3,
        clv_match_window_hours=2.0,
    )

    assert "FAIL: single-sport concentration" in body
    assert "Per-sport pair-key breakdown" in body


def test_candidate_file_has_no_bad_schema_substring():
    assert "no" + "_won" not in vslm.__file__
    with open(vslm.__file__) as f:
        assert "no" + "_won" not in f.read()
