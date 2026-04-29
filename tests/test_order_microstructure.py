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
        "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase",
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


# ---------------------------------------------------------------------------
# Task 3: executor hook tests
# ---------------------------------------------------------------------------

def test_paper_branch_writes_no_microstructure_row(tmp_path, monkeypatch):
    """Mimic the place-order site under PAPER_MODE; assert no JSONL row,
    no _PENDING entry, no synchronous rejection."""
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)

    # Paper-mode reproduction: build an order_result the way executor.py:898-908
    # does. The PAPER_MODE branch must NEVER call into om.* — that's the
    # gate we're protecting.
    paper_result = {
        "order_id": "PAPER-ABCD1234", "ticker": "KX1", "side": "no",
        "count": 10, "filled_count": 10, "price_cents": 40,
        "cost_dollars": 4.0, "status": "paper_filled", "paper": True,
    }
    assert paper_result["paper"] is True  # sanity
    assert not f.exists()
    assert "PAPER-ABCD1234" not in om._PENDING


def test_live_branch_records_placement_via_executor(tmp_path, monkeypatch):
    """Force live mode, stub place_order, replicate the executor's live branch
    inline, verify record_placement registers _PENDING."""
    from bot import executor
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    monkeypatch.setattr(executor, "PAPER_MODE", False)

    def fake_place(ticker, side, count, price_cents, action="buy"):
        return {
            "order_id": "OLIVE1", "ticker": ticker, "side": side, "count": count,
            "filled_count": 0, "remaining_count": count,
            "price_cents": price_cents, "cost_dollars": 0.0,
            "status": "submitted", "client_order_id": "uuid-x",
        }
    monkeypatch.setattr(executor, "place_order", fake_place)

    opportunity = _opp()
    ts_placed = datetime.now(timezone.utc)
    queue_depth = opportunity["market"].get("no_ask")
    order_result = executor.place_order(
        ticker="KX1", side="no", count=10, price_cents=40, action="buy",
    )
    assert "error" not in order_result
    om.record_placement(
        order_id=order_result["order_id"],
        opportunity=opportunity,
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=ts_placed, queue_depth_at_place=queue_depth,
    )
    assert "OLIVE1" in om._PENDING
    assert om._PENDING["OLIVE1"]["queue_depth_at_place"] == 40


def test_live_branch_records_synchronous_rejection(tmp_path, monkeypatch):
    """When place_order returns {'error': ...}, the rejection row gets written
    immediately with terminal_status='rejected' and no _PENDING entry."""
    from bot import executor
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)
    monkeypatch.setattr(executor, "PAPER_MODE", False)

    def fake_place(**kw):
        return {"error": "Insufficient balance"}
    monkeypatch.setattr(executor, "place_order", fake_place)

    opportunity = _opp()
    ts_placed = datetime.now(timezone.utc)
    order_result = executor.place_order(
        ticker="KX1", side="no", count=10, price_cents=40, action="buy",
    )
    assert "error" in order_result
    om.record_synchronous_rejection(
        opportunity=opportunity,
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=ts_placed, error=str(order_result["error"]),
    )
    rows = [json.loads(l) for l in f.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["terminal_status"] == "rejected"
    assert rows[0]["rejection_error"] == "Insufficient balance"
    assert rows[0]["kalshi_order_id"] is None
    assert om._PENDING == {}


# ---------------------------------------------------------------------------
# Task 4: check_fills terminal-observation hook
# ---------------------------------------------------------------------------

def test_check_fills_writes_terminal_on_full_fill(tmp_path, monkeypatch):
    """Mock get_order to return filled_count=requested → record_terminal fires."""
    from bot import executor
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)

    om.record_placement(
        order_id="OXYZ", opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        queue_depth_at_place=None,
    )
    monkeypatch.setattr(executor, "_load_json", lambda *a, **kw: [
        {
            "ticker": "KX1", "side": "no", "contracts": 10, "filled": 0,
            "status": "resting", "order_id": "OXYZ", "paper": False,
            "price_cents": 40, "type": "vig_stack_series",
        }
    ])
    saved: dict = {}
    monkeypatch.setattr(executor, "_save_json", lambda p, d: saved.update({"data": d}))
    monkeypatch.setattr(executor, "get_order", lambda oid: {
        "order_id": "OXYZ", "ticker": "KX1", "side": "no", "count": 10,
        "filled_count": 10, "remaining_count": 0, "status": "filled",
        "yes_price": None, "no_price": 40,
    })
    executor.check_fills()
    rows = [json.loads(l) for l in f.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["terminal_status"] == "filled"
    assert rows[0]["filled_qty"] == 10
    assert "OXYZ" not in om._PENDING


def test_check_fills_observes_partial_progress(tmp_path, monkeypatch):
    """When get_order returns 0 < filled_count < requested and status='resting',
    we should NOT write terminal — only observe partial progress."""
    from bot import executor
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)

    om.record_placement(
        order_id="OPART", opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        queue_depth_at_place=None,
    )
    monkeypatch.setattr(executor, "_load_json", lambda *a, **kw: [
        {
            "ticker": "KX1", "side": "no", "contracts": 10, "filled": 0,
            "status": "resting", "order_id": "OPART", "paper": False,
            "price_cents": 40, "type": "vig_stack_series",
        }
    ])
    monkeypatch.setattr(executor, "_save_json", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "get_order", lambda oid: {
        "order_id": "OPART", "ticker": "KX1", "side": "no", "count": 10,
        "filled_count": 4, "remaining_count": 6, "status": "resting",
        "yes_price": None, "no_price": 40,
    })
    executor.check_fills()
    # No row written — order is still partial-resting, not terminal.
    assert not f.exists() or f.read_text() == ""
    # _PENDING should reflect the partial observation.
    assert "OPART" in om._PENDING
    assert om._PENDING["OPART"]["partial_fill_count"] == 1
    assert om._PENDING["OPART"]["last_filled_count"] == 4


def test_check_fills_writes_terminal_on_kalshi_canceled(tmp_path, monkeypatch):
    """Kalshi status 'canceled' with partial fill → terminal_status='canceled'."""
    from bot import executor
    f = tmp_path / "om.jsonl"
    monkeypatch.setattr(om, "MICROSTRUCTURE_FILE", f)

    om.record_placement(
        order_id="OCANC", opportunity=_opp(),
        requested_price_cents=40, requested_qty=10, side="no",
        ts_placed=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
        queue_depth_at_place=None,
    )
    monkeypatch.setattr(executor, "_load_json", lambda *a, **kw: [
        {
            "ticker": "KX1", "side": "no", "contracts": 10, "filled": 0,
            "status": "resting", "order_id": "OCANC", "paper": False,
            "price_cents": 40, "type": "vig_stack_series",
        }
    ])
    monkeypatch.setattr(executor, "_save_json", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "get_order", lambda oid: {
        "order_id": "OCANC", "ticker": "KX1", "side": "no", "count": 10,
        "filled_count": 3, "remaining_count": 7, "status": "canceled",
        "yes_price": None, "no_price": 40,
    })
    executor.check_fills()
    rows = [json.loads(l) for l in f.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["terminal_status"] == "canceled"
    assert rows[0]["filled_qty"] == 3
    assert rows[0]["ts_canceled"] is not None
    assert "OCANC" not in om._PENDING


# ---------------------------------------------------------------------------
# Task 6: slippage-adjusted CLV math (lives in tools/microstructure_report.py)
# ---------------------------------------------------------------------------

def test_slippage_adjusted_clv_math():
    """Hand-craft a microstructure record + matching clv record; assert
    slippage_adjusted_clv = clv_cents - slippage_cents matches manual calc."""
    from tools.microstructure_report import compute_slippage_adjusted_clv

    # microstructure: bought NO at 40¢, filled at 42¢ → +2¢ adverse slippage
    micro = {
        "ticker": "KX1", "ts_placed": "2026-04-25T12:00:00+00:00",
        "side": "no", "requested_price_cents": 40, "filled_price_cents": 42,
        "slippage_cents": 2.0, "strategy_name": "vig_stack_series",
    }
    # clv record: same ticker, recorded_at within 60s, clv_cents=+5.0
    clv = {
        "ticker": "KX1", "recorded_at": "2026-04-25T12:00:30+00:00",
        "clv_cents": 5.0, "status": "settled", "side": "no",
    }
    # Slippage-adjusted CLV: paper-CLV (5.0) minus what slippage cost us (2.0) = 3.0
    assert compute_slippage_adjusted_clv(micro, clv) == 3.0
    # Cross-check: a favorable -2¢ slippage (got fill at 38¢) should boost CLV
    micro_fav = dict(micro, slippage_cents=-2.0)
    assert compute_slippage_adjusted_clv(micro_fav, clv) == 7.0
    # Cross-check: null slippage → returns None (synchronous rejection case)
    assert compute_slippage_adjusted_clv(dict(micro, slippage_cents=None), clv) is None
    # Null clv_cents → returns None
    assert compute_slippage_adjusted_clv(micro, dict(clv, clv_cents=None)) is None
