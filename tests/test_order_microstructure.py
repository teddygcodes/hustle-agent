"""Tests for bot.order_microstructure (Session 15).

All tests use mocks for the Kalshi client — verification of real-order behavior
is deferred until PAPER_MODE=False per spec.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from bot import order_microstructure as om


@pytest.fixture(autouse=True)
def _reset_pending(monkeypatch):
    """Each test starts with an empty _PENDING dict."""
    monkeypatch.setattr(om, "_PENDING", {})


def _opp(ticker="KX1", opp_type="vig_stack_series", side="no", market=None):
    return {
        "ticker": ticker,
        "type": opp_type,
        "opp_type": opp_type,
        "recommended_side": side,
        "market": market or {
            "yes_ask": 60, "no_ask": 40, "volume_24h": 100,
            "close_ts": "2026-04-26T00:00:00+00:00",
        },
    }


def test_append_record_writes_one_line_jsonl(tmp_path, monkeypatch):
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    om._append_record({"ts_placed": "2026-04-25T00:00:00+00:00", "ticker": "KX1"})
    lines = f.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["ticker"] == "KX1"


def test_immediate_fill_at_requested_price_zero_slippage(tmp_path, monkeypatch):
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    ts_placed = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    om.record_placement(
        order_id="O1", opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=ts_placed, queue_depth_at_place=100,
    )
    om.record_terminal(
        order_id="O1", kalshi_status="filled",
        filled_count=10, cost_dollars=4.0,
        ts_terminal=datetime(2026, 4, 25, 12, 0, 0, 500_000, tzinfo=timezone.utc),
    )
    rows = [json.loads(l) for l in f.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["terminal_status"] == "filled"
    assert r["slippage_cents"] == 0.0
    assert r["latency_ms"] == 500
    assert r["filled_qty"] == 10
    assert r["partial_fill_count"] == 0
    assert r["queue_depth_at_place"] == 100
    assert "regime" in r
    assert set(r["regime"].keys()) == {
        "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr",
    }
    assert "O1" not in om._PENDING  # popped


def test_immediate_fill_at_adverse_price_positive_slippage(tmp_path, monkeypatch):
    """cost_dollars implies fill at higher price → +slippage."""
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    om.record_placement(
        order_id="O2", opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        queue_depth_at_place=None,
    )
    # cost_dollars=4.20 → avg fill 42¢ → +2¢ slippage
    om.record_terminal(
        order_id="O2", kalshi_status="filled",
        filled_count=10, cost_dollars=4.20,
        ts_terminal=datetime(2026, 4, 25, 12, 0, 0, 100_000, tzinfo=timezone.utc),
    )
    r = json.loads(f.read_text().splitlines()[0])
    assert r["slippage_cents"] == 2.0
    assert r["filled_price_cents"] == 42
    assert r["slippage_source"] == "limit_price_echo"


def test_immediate_fill_at_favorable_price_negative_slippage(tmp_path, monkeypatch):
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    om.record_placement(
        order_id="O3", opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        queue_depth_at_place=None,
    )
    om.record_terminal(
        order_id="O3", kalshi_status="filled",
        filled_count=10, cost_dollars=3.80,
        ts_terminal=datetime(2026, 4, 25, 12, 0, 0, 100_000, tzinfo=timezone.utc),
    )
    r = json.loads(f.read_text().splitlines()[0])
    assert r["slippage_cents"] == -2.0


def test_partial_then_canceled(tmp_path, monkeypatch):
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    ts0 = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    om.record_placement(
        order_id="O4", opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=ts0, queue_depth_at_place=None,
    )
    # Two intermediate partial-fill observations before cancel
    om.observe_fill_progress(order_id="O4", filled_count=3)
    om.observe_fill_progress(order_id="O4", filled_count=7)
    om.record_terminal(
        order_id="O4", kalshi_status="canceled",
        filled_count=7, cost_dollars=2.80,
        ts_terminal=datetime(2026, 4, 25, 12, 0, 30, tzinfo=timezone.utc),
    )
    r = json.loads(f.read_text().splitlines()[0])
    assert r["terminal_status"] == "canceled"
    assert r["filled_qty"] == 7
    assert r["partial_fill_count"] == 2  # two increments seen
    assert r["ts_canceled"] is not None
    assert r["ts_filled"] is None


def test_synchronous_rejection_no_order_id(tmp_path, monkeypatch):
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    om.record_synchronous_rejection(
        opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        error="Insufficient balance",
    )
    r = json.loads(f.read_text().splitlines()[0])
    assert r["terminal_status"] == "rejected"
    assert r["filled_qty"] == 0
    assert r["filled_price_cents"] is None
    assert r["slippage_cents"] is None
    assert r["slippage_source"] == "none"
    assert r["kalshi_order_id"] is None
    assert r["latency_ms"] == 0
    assert r["rejection_error"] == "Insufficient balance"


def test_queue_depth_null_when_market_missing(tmp_path, monkeypatch):
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    opp_no_market = {
        "ticker": "KX1", "type": "vig_stack_series",
        "opp_type": "vig_stack_series", "recommended_side": "no",
    }
    om.record_placement(
        order_id="O5", opportunity=opp_no_market,
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        queue_depth_at_place=None,
    )
    om.record_terminal(
        order_id="O5", kalshi_status="filled",
        filled_count=10, cost_dollars=4.0,
        ts_terminal=datetime(2026, 4, 25, 12, 0, 0, 100_000, tzinfo=timezone.utc),
    )
    r = json.loads(f.read_text().splitlines()[0])
    assert r["queue_depth_at_place"] is None


def test_concurrent_appends_all_parsable(tmp_path, monkeypatch):
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)

    def worker(i):
        om._append_record({"i": i, "ticker": f"KX{i}"})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = [json.loads(l) for l in f.read_text().splitlines() if l]
    assert len(rows) == 100
    assert {r["i"] for r in rows} == set(range(100))


def test_logging_failure_swallowed(tmp_path, monkeypatch):
    """When _append_record raises, caller continues — never propagates."""
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", tmp_path / "nope" / "x.jsonl")

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(om, "_append_record", boom)
    # Each public function must swallow the failure.
    om.record_synchronous_rejection(
        opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        error="x",
    )  # must not raise

    om.record_placement(
        order_id="OZ", opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        queue_depth_at_place=None,
    )
    om.record_terminal(
        order_id="OZ", kalshi_status="filled",
        filled_count=10, cost_dollars=4.0,
        ts_terminal=datetime(2026, 4, 25, 12, 0, 0, 100_000, tzinfo=timezone.utc),
    )  # must not raise even though _append_record raises
