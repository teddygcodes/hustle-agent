"""live_tick_anomalies — streaming heuristic flagging tickers with repeated price jumps."""

from __future__ import annotations

import datetime as dt
import tracemalloc
from types import SimpleNamespace

from tools.discovery_agent.heuristics.live_tick_anomalies import (
    JUMP_THRESHOLD_CENTS,
    LiveTickAnomalies,
    MIN_JUMPS_PER_TICKER,
    WINDOW_TICKS,
)


_NOW = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)


def _ctx(ticks=None, positions=None, paper_trades=None):
    rows = list(ticks or [])

    def live_ticks_iter():
        return iter(rows)

    return SimpleNamespace(
        live_ticks_iter=live_ticks_iter,
        positions=positions or [],
        paper_trades=paper_trades or [],
        loaded_at=_NOW,
    )


def _tick(ticker, price, seconds_offset=0):
    ts = _NOW + dt.timedelta(seconds=seconds_offset)
    return {"ticker": ticker, "price": price, "ts": ts.isoformat()}


def test_live_tick_anomalies_positive_three_jumps_one_ticker():
    """3+ jumps of >=15¢ on one ticker → fires."""
    ticks = [
        _tick("KX-A", 50, 0), _tick("KX-A", 51, 1), _tick("KX-A", 52, 2),  # baseline
        _tick("KX-A", 70, 3),  # jump #1: 70 vs median 51 = 19¢
        _tick("KX-A", 53, 4),  # back to baseline
        _tick("KX-A", 55, 5),
        _tick("KX-A", 75, 6),  # jump #2
        _tick("KX-A", 58, 7),
        _tick("KX-A", 90, 8),  # jump #3
    ]
    findings = LiveTickAnomalies().run(_ctx(ticks=ticks))
    assert len(findings) == 1
    assert findings[0].evidence["ticker"] == "KX-A"
    assert findings[0].evidence["jump_count"] >= MIN_JUMPS_PER_TICKER


def test_live_tick_anomalies_negative_below_min_jumps():
    """Only 2 jumps → no finding."""
    ticks = [
        _tick("KX-A", 50, 0), _tick("KX-A", 51, 1), _tick("KX-A", 52, 2),
        _tick("KX-A", 70, 3),  # jump 1
        _tick("KX-A", 55, 4),
        _tick("KX-A", 90, 5),  # jump 2
    ]
    findings = LiveTickAnomalies().run(_ctx(ticks=ticks))
    assert findings == []


def test_live_tick_anomalies_negative_jump_below_threshold():
    """Jumps of <15¢ → not counted."""
    ticks = [
        _tick("KX-A", 50, 0), _tick("KX-A", 51, 1), _tick("KX-A", 52, 2),
        _tick("KX-A", 60, 3),  # only 9¢ jump
        _tick("KX-A", 55, 4),
        _tick("KX-A", 65, 5),  # only ~12¢ jump
        _tick("KX-A", 58, 6),
        _tick("KX-A", 67, 7),  # only ~10¢ jump
    ]
    findings = LiveTickAnomalies().run(_ctx(ticks=ticks))
    assert findings == []


def test_live_tick_anomalies_boundary_exactly_at_threshold():
    """Jump of exactly 15¢ counts (>= comparison)."""
    ticks = [
        _tick("KX-A", 50, 0), _tick("KX-A", 50, 1), _tick("KX-A", 50, 2),
        _tick("KX-A", 65, 3),  # exactly 15¢
        _tick("KX-A", 50, 4),
        _tick("KX-A", 65, 5),
        _tick("KX-A", 50, 6),
        _tick("KX-A", 65, 7),
    ]
    findings = LiveTickAnomalies().run(_ctx(ticks=ticks))
    assert len(findings) == 1


def test_live_tick_anomalies_per_ticker_isolation():
    """Two tickers with 2 jumps each → no finding (each below MIN)."""
    ticks = []
    for ticker in ("KX-A", "KX-B"):
        ticks += [
            _tick(ticker, 50, 0), _tick(ticker, 51, 1), _tick(ticker, 52, 2),
            _tick(ticker, 70, 3), _tick(ticker, 55, 4), _tick(ticker, 75, 5),
        ]
    findings = LiveTickAnomalies().run(_ctx(ticks=ticks))
    assert findings == []


def test_live_tick_anomalies_promotes_severity_when_held():
    """Open position on the ticker during jump → severity 'notable'."""
    ticks = [
        _tick("KX-A", 50, 0), _tick("KX-A", 51, 1), _tick("KX-A", 52, 2),
        _tick("KX-A", 70, 3), _tick("KX-A", 55, 4), _tick("KX-A", 75, 5),
        _tick("KX-A", 58, 6), _tick("KX-A", 90, 7),
    ]
    positions = [{"ticker": "KX-A", "filled": 100, "status": "filled"}]
    findings = LiveTickAnomalies().run(_ctx(ticks=ticks, positions=positions))
    assert len(findings) == 1
    assert findings[0].severity == "notable"
    assert findings[0].evidence["held_during_jump"] is True


def test_live_tick_anomalies_paper_trade_overlap_detection():
    """Paper trade interval overlapping a jump → flagged."""
    ticks = [
        _tick("KX-A", 50, 0), _tick("KX-A", 51, 1), _tick("KX-A", 52, 2),
        _tick("KX-A", 70, 3), _tick("KX-A", 55, 4), _tick("KX-A", 75, 5),
        _tick("KX-A", 58, 6), _tick("KX-A", 90, 7),
    ]
    paper_trades = [{
        "ticker": "KX-A",
        "timestamp": _NOW.isoformat(),
        "resolved_at": (_NOW + dt.timedelta(seconds=10)).isoformat(),
    }]
    findings = LiveTickAnomalies().run(_ctx(ticks=ticks, paper_trades=paper_trades))
    assert findings[0].evidence["paper_trade_overlap"] is True
    assert findings[0].severity == "notable"


def test_live_tick_anomalies_missing_source_returns_empty():
    findings = LiveTickAnomalies().run(_ctx())
    assert findings == []


def test_live_tick_anomalies_memory_safety_100k_ticks():
    """100k ticks across 1000 tickers → peak memory < 50MB."""
    ticks = []
    for tid in range(1000):
        for i in range(100):
            # mostly stable, occasional jump
            price = 50 + (20 if (tid + i) % 47 == 0 else (i % 3))
            ticks.append(_tick(f"KX-{tid:04d}", price, i))

    tracemalloc.start()
    findings = LiveTickAnomalies().run(_ctx(ticks=ticks))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 50 * 1024 * 1024, f"Peak memory {peak / 1024 / 1024:.1f}MB exceeds 50MB"
    # Sanity: at least the heuristic ran without crashing
    assert isinstance(findings, list)
