"""cadence_outcome heuristic — flag P&L underperformance in slow-cadence buckets."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tools.discovery_agent.heuristics.cadence_outcome import (
    CadenceOutcome,
    MIN_TRADES_PER_BUCKET,
)


_NOW = dt.datetime(2026, 5, 1, 12, tzinfo=dt.timezone.utc)


def _ctx(tracker_cadence=None, paper_trades=None):
    return SimpleNamespace(
        tracker_cadence=tracker_cadence or [],
        paper_trades=paper_trades or [],
        loaded_at=_NOW,
    )


def _cadence_record(minutes_ago, ms, called_from="_position_check_loop"):
    ts = _NOW - dt.timedelta(minutes=minutes_ago)
    return {"ts": ts.isoformat(), "ms_since_last_call": ms, "called_from": called_from,
            "num_open_positions": 1}


def _settled_trade(tid, resolved_minutes_ago, pnl, status="exited_early"):
    ts = _NOW - dt.timedelta(minutes=resolved_minutes_ago)
    return {
        "id": tid, "ticker": f"KX-{tid}", "type": "live_momentum",
        "status": status, "pnl": pnl,
        "timestamp": (ts - dt.timedelta(hours=1)).isoformat(),
        "resolved_at": ts.isoformat(),
    }


def _build_cadence_for_trade(trade_resolved_minutes_ago, median_ms, count=10):
    """Generate a window's worth of cadence records covering the trade's pre-exit hour."""
    rows = []
    for i in range(count):
        # spread across the (resolved - 1h, resolved] window
        offset_minutes = trade_resolved_minutes_ago + (i * 5) + 1  # 1..50 min before resolve
        rows.append(_cadence_record(offset_minutes, median_ms))
    return rows


def test_cadence_outcome_positive_slow_bucket_underperforms():
    """Slow bucket (>120s) trades net materially below global mean → flagged.

    Math: with two equal-size buckets at distinct means, each bucket lands
    exactly 1 std from the global mean, perpetually tied at threshold. Need
    unequal-size buckets so the smaller-and-worse cohort can sit BELOW global
    mean by >1 std.
    """
    fast_trades, fast_cadence = [], []
    slow_trades, slow_cadence = [], []
    # 20 trades in the fast (<=10s) bucket: each profits +$5
    for i in range(20):
        resolved = 60 + (i * 60)
        fast_trades.append(_settled_trade(f"FAST{i}", resolved, 5.0))
        fast_cadence += _build_cadence_for_trade(resolved, median_ms=5000)
    # 12 trades in the slow (>120s) bucket: each loses $10
    for i in range(12):
        resolved = 60 + (20 + i) * 60 + 30
        slow_trades.append(_settled_trade(f"SLOW{i}", resolved, -10.0))
        slow_cadence += _build_cadence_for_trade(resolved, median_ms=150000)
    findings = CadenceOutcome().run(_ctx(
        tracker_cadence=fast_cadence + slow_cadence,
        paper_trades=fast_trades + slow_trades,
    ))
    slow_findings = [f for f in findings if "120" in f.evidence["cadence_bucket"]]
    assert len(slow_findings) == 1
    assert slow_findings[0].evidence["bucket_mean_pnl"] < 0
    assert slow_findings[0].evidence["std_devs_below_mean"] > 1.0


def test_cadence_outcome_negative_below_min_trade_count():
    """Bucket with <10 trades → not flagged."""
    trades, cadence = [], []
    for i in range(MIN_TRADES_PER_BUCKET - 1):
        resolved = 60 + (i * 60)
        trades.append(_settled_trade(f"T{i}", resolved, -10.0))
        cadence += _build_cadence_for_trade(resolved, median_ms=150000)
    findings = CadenceOutcome().run(_ctx(tracker_cadence=cadence, paper_trades=trades))
    assert findings == []


def test_cadence_outcome_negative_uniform_pnl_no_variance():
    """All trades same P&L → global_std=0 → bail (no variance to detect outliers)."""
    trades, cadence = [], []
    for i in range(20):
        resolved = 60 + (i * 60)
        trades.append(_settled_trade(f"T{i}", resolved, 5.0))
        cadence += _build_cadence_for_trade(resolved, median_ms=5000)
    findings = CadenceOutcome().run(_ctx(tracker_cadence=cadence, paper_trades=trades))
    assert findings == []


def test_cadence_outcome_skips_main_loop_cadence():
    """Only _position_check_loop cadence counts (it's the loop driving exits)."""
    trades = [_settled_trade(f"T{i}", 60 + i * 60, -50.0) for i in range(15)]
    cadence = []
    # Slow cadence — but recorded under _main_loop, not _position_check_loop
    for tr in trades:
        cadence += _build_cadence_for_trade(60 + trades.index(tr) * 60, median_ms=150000)
    cadence_main_loop_only = [
        {**c, "called_from": "_main_loop"} for c in cadence
    ]
    findings = CadenceOutcome().run(_ctx(
        tracker_cadence=cadence_main_loop_only, paper_trades=trades,
    ))
    # Trades with no _position_check_loop cadence in window get dropped; no buckets populated
    assert findings == []


def test_cadence_outcome_skips_unsettled_trades():
    """Open trades have no resolved_at → dropped."""
    trades = [{"id": "OPEN", "status": "open", "pnl": -50.0,
               "timestamp": _NOW.isoformat()}] * 15
    findings = CadenceOutcome().run(_ctx(paper_trades=trades))
    assert findings == []


def test_cadence_outcome_boundary_bucket_assignment():
    """Median exactly at a boundary lands in that bucket (<= comparison)."""
    from tools.discovery_agent.heuristics.cadence_outcome import _bucket_for
    assert _bucket_for(10000) == "<=10s"   # exactly 10000ms → fits in <=10s
    assert _bucket_for(10001) == "<=20s"   # just over → next bucket
    assert _bucket_for(120000) == "<=120s"
    assert _bucket_for(120001) == ">120s"


def test_cadence_outcome_missing_source_returns_empty():
    findings = CadenceOutcome().run(_ctx())
    assert findings == []
