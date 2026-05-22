"""Tests for tools/sim_commodity_vig_stack.py — S160 commodity-ladder vig_stack sim.

Pure-function coverage of the novel / load-bearing math: synthetic-bucket differencing
(telescopes to 100c), monotonicity clamping + violation counting, the buy-all margin
= −Σspread identity, collapsed-curve winner detection, the Kalshi fee model, and the
ITM-count structural classifier. No I/O, no bot imports, no network.
"""
from __future__ import annotations

import math

from tools.sim_commodity_vig_stack import (
    classify_structure,
    direct_buckets,
    kalshi_fee_cents,
    synthetic_buckets,
    _winner_index,
)


def _row(ticker, yes_ask, yes_bid, no_ask, *, vol=10, oi=10):
    return {
        "ticker": ticker, "event_ticker": "EV", "scan_id": "S1", "series_ticker": "X",
        "yes_ask": yes_ask, "yes_bid": yes_bid,
        "no_ask": no_ask, "no_bid": 100 - yes_ask,
        "volume_24h": vol, "open_interest": oi, "ts": "2026-05-01T00:00:00Z",
    }


def _threshold_rows(specs):
    """specs: list of (strike, yes_ask, yes_bid, no_ask) -> threshold ladder rows."""
    return [_row(f"KX-26MAY01-T{s:g}", ya, yb, na) for (s, ya, yb, na) in specs]


# --- synthetic differencing telescopes to 100c -------------------------------------------
def test_synthetic_buckets_sum_to_100():
    # monotone-decreasing survival curve; zero spread (bid==ask) so mid==ask.
    rows = _threshold_rows([(10, 90, 90, 10), (20, 60, 60, 40), (30, 30, 30, 70)])
    buckets, violations = synthetic_buckets(rows)
    assert violations == 0
    assert len(buckets) == 4  # K=3 thresholds -> K+1 buckets
    assert abs(sum(b["prob"] for b in buckets) - 100.0) < 1e-9
    # buckets: <10=10, [10,20)=30, [20,30)=30, >=30=30
    assert [round(b["prob"]) for b in buckets] == [10, 30, 30, 30]


# --- monotonization clamps a non-monotone curve and counts violations --------------------
def test_synthetic_monotonization_clamps_and_counts():
    # third rung (80) rises above the running min (70) -> one violation, clamped to 70.
    rows = _threshold_rows([(10, 90, 90, 10), (20, 70, 70, 30),
                            (30, 80, 80, 20), (40, 40, 40, 60)])
    buckets, violations = synthetic_buckets(rows)
    assert violations == 1
    # clamped curve 90,70,70,40 -> buckets <10=10, [10,20)=20, [20,30)=0, [30,40)=30, >=40=40
    assert [round(b["prob"]) for b in buckets] == [10, 20, 0, 30, 40]
    assert abs(sum(b["prob"] for b in buckets) - 100.0) < 1e-9


# --- buy-all margin == −Σspread (the decisive Outcome-C identity) -------------------------
def test_buy_all_margin_equals_negative_total_spread():
    # each rung carries a 2c spread (yes_ask + no_ask = 102) -> Σspread = 6c over 3 rungs.
    rows = _threshold_rows([(10, 90, 88, 12), (20, 60, 58, 42), (30, 30, 28, 72)])
    buckets, _ = synthetic_buckets(rows)
    payout = (len(buckets) - 1) * 100.0
    cost = sum(b["no_cost"] for b in buckets)
    margin = payout - cost
    spread_sum = sum((r["yes_ask"] + r["no_ask"]) - 100.0 for r in rows)
    assert spread_sum == 6.0
    assert margin == -spread_sum  # exact identity


# --- collapsed-curve winner detection ----------------------------------------------------
def test_winner_index_detects_collapsed_curve():
    # settled: yes 100,100,0 over strikes 10,20,30 -> settle price in [20,30) = bucket idx 2.
    rows = _threshold_rows([(10, 100, 100, 0), (20, 100, 100, 0), (30, 0, 0, 100)])
    buckets, _ = synthetic_buckets(rows)
    assert _winner_index(buckets) == 2


def test_winner_index_none_for_live_curve():
    # a live mid-life quote is not collapsed -> no determinable winner.
    rows = _threshold_rows([(10, 80, 80, 20), (20, 50, 50, 50), (30, 20, 20, 80)])
    buckets, _ = synthetic_buckets(rows)
    assert _winner_index(buckets) is None


def test_winner_index_tail_high():
    # all thresholds YES -> settled at/above the top strike -> tail-high bucket (last index).
    rows = _threshold_rows([(10, 100, 100, 0), (20, 100, 100, 0), (30, 100, 100, 0)])
    buckets, _ = synthetic_buckets(rows)
    assert _winner_index(buckets) == len(buckets) - 1


# --- Kalshi fee model --------------------------------------------------------------------
def test_kalshi_fee_cents():
    assert kalshi_fee_cents(50, 0.07) == 2     # ceil(0.07*0.5*0.5*100) = ceil(1.75)
    assert kalshi_fee_cents(50, 0.035) == 1    # ceil(0.035*0.25*100) = ceil(0.875)
    assert kalshi_fee_cents(99, 0.07) == 1     # ceil(0.07*0.99*0.01*100) = ceil(0.0693)
    assert kalshi_fee_cents(1, 0.07) == 1      # ceil of a tiny positive rounds up to 1c


# --- direct buckets (KXINX `-B` partition) -----------------------------------------------
def test_direct_buckets_one_per_rung():
    rows = [_row("KXINX-26MAY01H1600-B10", 60, 58, 41),
            _row("KXINX-26MAY01H1600-B20", 20, 18, 81),
            _row("KXINX-26MAY01H1600-B30", 20, 18, 81)]
    buckets, violations = direct_buckets(rows)
    assert violations == 0
    assert len(buckets) == 3
    assert buckets[0]["no_cost"] == 41 and buckets[0]["legs"] == 1
    assert abs(buckets[0]["prob"] - 59.0) < 1e-9  # yes_mid = (60+58)/2


# --- ITM-count structural classifier -----------------------------------------------------
def _groups_from_rows(rows):
    groups = {("S1", "EV"): {r["ticker"]: r for r in rows}}
    event_rungs = {"EV": {r["ticker"] for r in rows}}
    return groups, event_rungs


def test_classify_partition_one_itm_rung():
    # one rung deep ITM (yes_mid>50), rest not -> single-winner partition.
    rows = [_row("KX-B10", 60, 58, 41), _row("KX-B20", 20, 18, 81), _row("KX-B30", 15, 13, 86)]
    g, er = _groups_from_rows(rows)
    out = classify_structure("X", g, er)
    assert out["structure"] == "SINGLE_WINNER_PARTITION"
    assert out["median_itm_count"] == 1


def test_classify_nested_many_itm_rungs():
    # cumulative thresholds: many rungs deep ITM -> NOT a single-winner partition.
    rows = _threshold_rows([(10, 95, 93, 5), (20, 90, 88, 10),
                            (30, 80, 78, 20), (40, 30, 28, 70)])
    g, er = _groups_from_rows(rows)
    out = classify_structure("X", g, er)
    assert out["structure"] == "NOT_SINGLE_WINNER_PARTITION"
    assert out["median_itm_count"] == 3  # rungs at 95,90,80 are >50c


def test_classify_insufficient_data():
    out = classify_structure("X", {}, {})
    assert out["structure"] == "INSUFFICIENT_DATA"
    assert out["full_eval"] == 0
