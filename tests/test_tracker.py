"""Tests for bot/tracker.py MFE/MAE excursion tracking (Session 9).

Covers:
  - First-observation lazy init of mfe/mae/ticks_observed
  - Pre-Session-9 position records (missing fields) don't crash update
  - Side-aware ratcheting (NO: no_bid movement; YES: yes_bid movement)
  - Monotonic ratchet of mfe/mae and ticks_observed
  - Settlement propagation from positions.json to clv.json record
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import tracker, tracker_cadence, clv  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _base_position(
    *,
    ticker: str = "KXTEST-1",
    order_id: str = "PAPER-TEST-1",
    side: str = "no",
    price_cents: int = 79,
    filled: int = 10,
) -> dict:
    return {
        "ticker": ticker,
        "title": f"{ticker} title",
        "side": side,
        "contracts": filled,
        "filled": filled,
        "price_cents": price_cents,
        "cost": filled * price_cents / 100.0,
        "order_id": order_id,
        "type": "vig_stack_series",
        "opp_type": "vig_stack_series",
        "opened_at": "2026-04-24T00:00:00+00:00",
        "status": "filled",
        "paper": True,
    }


@pytest.fixture
def tracker_env(tmp_path, monkeypatch):
    """Sandbox positions.json and mock get_market for tracker tests."""
    positions_file = tmp_path / "positions.json"
    positions_file.write_text("[]")
    monkeypatch.setattr(tracker, "POSITIONS_FILE", positions_file)

    # Session 17: isolate tracker_cadence's append-only log to tmp_path so
    # tests don't pollute the production bot/state/tracker_cadence.jsonl.
    cadence_file = tmp_path / "tracker_cadence.jsonl"
    monkeypatch.setattr(tracker_cadence, "CADENCE_FILE", cadence_file)
    monkeypatch.setattr(tracker_cadence, "BOT_STATE_DIR", tmp_path)
    # Reset the per-call-site last_ts cache so tests get clean ms_since_last_call.
    tracker_cadence._last_call_ts.clear()

    # Silence the resting-orders sweep: it also calls get_market for any
    # status=="resting" position. Our tests only use "filled".
    market_response = {"yes_bid": 50, "no_bid": 50, "yes_ask": 52, "no_ask": 52, "status": "active"}

    def fake_get_market(ticker):
        return dict(market_response)

    monkeypatch.setattr(tracker, "get_market", fake_get_market)

    def set_market(**kwargs):
        market_response.update(kwargs)

    def seed(positions: list[dict]):
        positions_file.write_text(json.dumps(positions))

    def read() -> list[dict]:
        return json.loads(positions_file.read_text())

    return {
        "set_market": set_market,
        "seed": seed,
        "read": read,
        "file": positions_file,
        "cadence_file": cadence_file,
    }


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestMFEMAEInitialization:

    def test_first_observation_initializes_to_zero(self, tracker_env):
        tracker_env["seed"]([_base_position(side="yes", price_cents=45)])
        tracker_env["set_market"](yes_bid=45, no_bid=55)  # at entry
        tracker.update_positions()

        pos = tracker_env["read"]()[0]
        assert pos["mfe_cents"] == 0
        assert pos["mae_cents"] == 0
        assert pos["ticks_observed"] == 1
        assert pos["mfe_at"] is not None
        assert pos["mae_at"] is not None

    def test_pre_session_9_position_survives_update(self, tracker_env):
        # Simulate a position that was opened before Session 9 deployed:
        # no mfe_cents/mae_cents/ticks_observed keys on the dict.
        legacy = _base_position(side="yes", price_cents=60)
        tracker_env["seed"]([legacy])
        tracker_env["set_market"](yes_bid=65, no_bid=35)
        tracker.update_positions()  # must not raise

        pos = tracker_env["read"]()[0]
        # Fields were added lazily on first observation
        assert pos["mfe_cents"] == 5  # yes_bid rose 60 -> 65
        assert pos["mae_cents"] == 0
        assert pos["ticks_observed"] == 1

    def test_regime_tagged_once_at_first_observation(self, tracker_env):
        """Session 14: regime is set ONCE on first MFE/MAE observation.
        Subsequent updates must not overwrite — regime is a property of
        the position derived from its opened_at + ticker."""
        pos_in = _base_position(
            side="yes", price_cents=45, ticker="KXNBAGAME-26APR25-LAL"
        )
        pos_in["opened_at"] = "2026-04-25T18:00:00+00:00"
        tracker_env["seed"]([pos_in])
        tracker_env["set_market"](yes_bid=45, no_bid=55)
        tracker.update_positions()

        pos = tracker_env["read"]()[0]
        assert "regime" in pos
        regime = pos["regime"]
        assert set(regime.keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase",
        }
        # NBA on Apr 25 → playoffs; opened_at 18:00 UTC → 14:00 ET → afternoon
        assert regime["sport_phase"] == "playoffs"
        assert regime["time_of_day"] == "afternoon"
        initial = dict(regime)

        # Second update must not re-tag
        tracker_env["set_market"](yes_bid=50, no_bid=50)
        tracker.update_positions()
        pos2 = tracker_env["read"]()[0]
        assert pos2["regime"] == initial

    def test_regime_falls_back_to_now_when_opened_at_malformed(self, tracker_env):
        """If opened_at is missing or unparseable, tagger still runs using
        the current update timestamp — never blocks the position update."""
        bad = _base_position(side="yes", price_cents=45)
        bad["opened_at"] = "not-a-date"
        tracker_env["seed"]([bad])
        tracker_env["set_market"](yes_bid=45, no_bid=55)
        tracker.update_positions()  # must not raise
        pos = tracker_env["read"]()[0]
        assert "regime" in pos
        assert set(pos["regime"].keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase",
        }


# ---------------------------------------------------------------------------
# Ratcheting
# ---------------------------------------------------------------------------

class TestMFEMAERatcheting:

    def test_no_side_yes_price_drop_bumps_mfe(self, tracker_env):
        """NO at entry 79. Yes price drops = no_bid rises. mfe bumps."""
        tracker_env["seed"]([_base_position(side="no", price_cents=79)])
        tracker_env["set_market"](yes_bid=21, no_bid=79)
        tracker.update_positions()
        tracker_env["set_market"](yes_bid=15, no_bid=85)
        tracker.update_positions()

        pos = tracker_env["read"]()[0]
        assert pos["mfe_cents"] == 6  # 85 - 79
        assert pos["mae_cents"] == 0

    def test_no_side_yes_price_spike_bumps_mae(self, tracker_env):
        """NO at entry 79. Yes price spikes = no_bid drops. mae bumps."""
        tracker_env["seed"]([_base_position(side="no", price_cents=79)])
        tracker_env["set_market"](yes_bid=21, no_bid=79)
        tracker.update_positions()
        tracker_env["set_market"](yes_bid=28, no_bid=72)
        tracker.update_positions()

        pos = tracker_env["read"]()[0]
        assert pos["mfe_cents"] == 0
        assert pos["mae_cents"] == 7  # 79 - 72

    def test_yes_side_mirror_behavior(self, tracker_env):
        """YES at entry 45. Track both favorable (up) and adverse (down)."""
        tracker_env["seed"]([_base_position(side="yes", price_cents=45)])
        tracker_env["set_market"](yes_bid=45, no_bid=55)
        tracker.update_positions()
        tracker_env["set_market"](yes_bid=52, no_bid=48)  # favorable
        tracker.update_positions()
        tracker_env["set_market"](yes_bid=40, no_bid=60)  # adverse
        tracker.update_positions()

        pos = tracker_env["read"]()[0]
        assert pos["mfe_cents"] == 7  # 52 - 45
        assert pos["mae_cents"] == 5  # 45 - 40

    def test_mfe_is_monotonic(self, tracker_env):
        """mfe/mae never decrease across oscillating observations."""
        tracker_env["seed"]([_base_position(side="no", price_cents=50)])
        sequence = [50, 55, 52, 58, 54, 57, 45, 51]  # no_bid values
        for nb in sequence:
            tracker_env["set_market"](yes_bid=100 - nb, no_bid=nb)
            tracker.update_positions()

        pos = tracker_env["read"]()[0]
        # Peak favorable: no_bid=58, mfe=58-50=8
        assert pos["mfe_cents"] == 8
        # Peak adverse: no_bid=45, mae=50-45=5
        assert pos["mae_cents"] == 5

    def test_ticks_observed_monotonic(self, tracker_env):
        tracker_env["seed"]([_base_position(side="yes", price_cents=50)])
        for _ in range(5):
            tracker_env["set_market"](yes_bid=50, no_bid=50)
            tracker.update_positions()
        pos = tracker_env["read"]()[0]
        assert pos["ticks_observed"] == 5

    def test_mfe_at_updates_only_on_new_peak(self, tracker_env):
        """Timestamp advances on a new mfe peak, not on every tick."""
        tracker_env["seed"]([_base_position(side="yes", price_cents=50)])
        tracker_env["set_market"](yes_bid=55, no_bid=45)
        tracker.update_positions()
        pos_after_peak = tracker_env["read"]()[0]
        first_peak_ts = pos_after_peak["mfe_at"]

        # Three non-peak ticks at equal or lower yes_bid
        for yb in (53, 54, 55):
            tracker_env["set_market"](yes_bid=yb, no_bid=100 - yb)
            tracker.update_positions()
        pos_after = tracker_env["read"]()[0]
        # No strict gt than 55/50=5, so mfe_at stays at first peak
        assert pos_after["mfe_cents"] == 5
        assert pos_after["mfe_at"] == first_peak_ts


# ---------------------------------------------------------------------------
# Settlement propagation
# ---------------------------------------------------------------------------

@pytest.fixture
def clv_env(tmp_path, monkeypatch):
    """Sandbox clv.json AND positions.json for settlement propagation tests."""
    clv_file = tmp_path / "clv.json"
    positions_file = tmp_path / "positions.json"
    positions_file.write_text("[]")
    clv_file.write_text("[]")
    monkeypatch.setattr(clv, "_CLV_FILE", clv_file)
    # bot.clv imports POSITIONS_FILE *inside* check_clv_settlements via
    # `from bot.config import POSITIONS_FILE`, so patching the attribute
    # on bot.config is what takes effect at call time.
    monkeypatch.setattr("bot.config.POSITIONS_FILE", positions_file)
    return {"clv_file": clv_file, "positions_file": positions_file}


class TestSettlementPropagation:

    def test_settlement_carries_mfe_to_clv_record(self, clv_env):
        # Session 16: settlement-time MFE extension. Pos has mfe=12 (observed
        # high-water mark in side's-own-bid-cents). NO @79 settles NO, so
        # closing_yes=0 → clv_cents = (100-79) - 0 = 21. Extension ratchets
        # mfe to max(12, 21) = 21 and mfe_at to settled_at. mae untouched.
        positions = [{
            **_base_position(order_id="PAPER-SETTLE-1", side="no", price_cents=79),
            "mfe_cents": 12,
            "mae_cents": 4,
            "mfe_at": "2026-04-24T10:00:00+00:00",
            "mae_at": "2026-04-24T09:00:00+00:00",
            "ticks_observed": 42,
        }]
        clv_env["positions_file"].write_text(json.dumps(positions))

        # Seed a matching clv record (status="open")
        clv.record_clv_entry(
            ticker="KXTEST-1", opp_type="vig_stack_series", side="no",
            entry_price_cents=79, fair_value_cents=90.0, edge_at_trade=0.1,
            contracts=10, trade_id="PAPER-SETTLE-1", paper=True,
        )

        fake_market = {"market": {"status": "settled", "result": "NO",
                                  "yes_bid": 0, "yes_ask": 0}}
        with patch("agent.kalshi_client.get_market", return_value=fake_market):
            settled_now = clv.check_clv_settlements()

        assert len(settled_now) == 1
        rec = json.loads(clv_env["clv_file"].read_text())[0]
        assert rec["status"] == "settled"
        # Session 16: mfe extended from observed 12 → settlement-favorable 21.
        assert rec["mfe_cents"] == 21
        assert rec["mfe_at"] == rec["settled_at"]
        # mae NOT extended (out of scope for Session 16 — report doesn't read it).
        assert rec["mae_cents"] == 4
        assert rec["mae_at"] == "2026-04-24T09:00:00+00:00"
        assert rec["ticks_observed"] == 42

    def test_settlement_without_position_match_seeds_mfe_to_clv(self, clv_env):
        """CLV record whose trade_id has no matching position still settles
        cleanly. Session 16: the settlement-time MFE extension runs even
        when pos is None — for winners it seeds mfe_cents to clv_cents so
        the excursion report can include the record. mae/ticks remain
        absent (no observed-life data to fabricate)."""
        clv.record_clv_entry(
            ticker="KXTEST-ORPHAN", opp_type="vig_stack_series", side="yes",
            entry_price_cents=60, fair_value_cents=70.0, edge_at_trade=0.15,
            contracts=5, trade_id="PAPER-NO-SUCH-POSITION", paper=True,
        )

        fake_market = {"market": {"status": "settled", "result": "YES",
                                  "yes_bid": 0, "yes_ask": 0}}
        with patch("agent.kalshi_client.get_market", return_value=fake_market):
            settled_now = clv.check_clv_settlements()

        assert len(settled_now) == 1
        rec = json.loads(clv_env["clv_file"].read_text())[0]
        assert rec["status"] == "settled"
        # Session 16: winner without pos → mfe seeded to clv_cents = 100-60 = 40.
        assert rec["mfe_cents"] == 40
        assert rec["mfe_at"] == rec["settled_at"]
        # mae/ticks have no observed-life data and the extension only touches mfe.
        assert "mae_cents" not in rec
        assert "ticks_observed" not in rec


# ---------------------------------------------------------------------------
# Cadence logging (Session 17)
# ---------------------------------------------------------------------------

class TestCadenceLogging:
    """Verify update_positions writes to tracker_cadence.jsonl on every call,
    with called_from threaded through and ms_since_last_call computed
    per-call-site (so _main_loop and _position_check_loop don't pollute each
    other's deltas)."""

    def test_cadence_log_records_called_from_and_open_count(self, tracker_env):
        tracker_env["seed"]([_base_position(side="no", price_cents=79)])
        tracker.update_positions(called_from="_position_check_loop")

        rows = [
            json.loads(l)
            for l in tracker_env["cadence_file"].read_text().splitlines()
            if l.strip()
        ]
        assert len(rows) == 1
        r = rows[0]
        assert r["called_from"] == "_position_check_loop"
        assert r["num_open_positions"] == 1
        assert r["ms_since_last_call"] is None  # first call has no prior delta
        assert "ts" in r

    def test_cadence_log_default_called_from_is_unspecified(self, tracker_env):
        # Backward-compat: existing callers / tests not yet passing called_from
        # land in the "unspecified" bucket rather than crashing.
        tracker_env["seed"]([_base_position(side="no", price_cents=79)])
        tracker.update_positions()

        rows = [
            json.loads(l)
            for l in tracker_env["cadence_file"].read_text().splitlines()
            if l.strip()
        ]
        assert rows[0]["called_from"] == "unspecified"

    def test_cadence_log_ms_delta_is_per_callsite(self, tracker_env):
        # Two calls from _main_loop, then one from _position_check_loop:
        # the position_check_loop's first row should have ms_since_last_call=None
        # because its bucket is independent of _main_loop's history.
        tracker_env["seed"]([_base_position(side="no", price_cents=79)])
        tracker.update_positions(called_from="_main_loop")
        tracker.update_positions(called_from="_main_loop")
        tracker.update_positions(called_from="_position_check_loop")

        rows = [
            json.loads(l)
            for l in tracker_env["cadence_file"].read_text().splitlines()
            if l.strip()
        ]
        assert len(rows) == 3
        assert rows[0]["called_from"] == "_main_loop"
        assert rows[0]["ms_since_last_call"] is None
        assert rows[1]["called_from"] == "_main_loop"
        assert rows[1]["ms_since_last_call"] is not None and rows[1]["ms_since_last_call"] >= 0
        assert rows[2]["called_from"] == "_position_check_loop"
        assert rows[2]["ms_since_last_call"] is None

    def test_cadence_log_counts_only_filled_or_partial(self, tracker_env):
        # exited / resolved / cancelled_stale positions don't count toward
        # num_open_positions — that field measures the per-call work.
        tracker_env["seed"]([
            _base_position(ticker="KXOPEN-1", order_id="PAPER-1", side="no", price_cents=79),
            {**_base_position(ticker="KXEXITED-1", order_id="PAPER-2", side="no",
                              price_cents=79), "status": "exited"},
            {**_base_position(ticker="KXRESOLVED-1", order_id="PAPER-3", side="no",
                              price_cents=79), "status": "resolved"},
        ])
        tracker.update_positions(called_from="_main_loop")

        rows = [
            json.loads(l)
            for l in tracker_env["cadence_file"].read_text().splitlines()
            if l.strip()
        ]
        assert rows[0]["num_open_positions"] == 1  # only the filled one

    def test_cadence_log_never_raises_even_when_positions_empty(self, tracker_env):
        # Empty positions.json (load returns []) must still produce a cadence row,
        # so the operator can see the loop is firing during quiet windows.
        tracker_env["seed"]([])
        tracker.update_positions(called_from="_main_loop")

        rows = [
            json.loads(l)
            for l in tracker_env["cadence_file"].read_text().splitlines()
            if l.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["num_open_positions"] == 0

    def test_ticks_observed_still_increments_per_call_with_called_from(self, tracker_env):
        # Session 9 contract preserved: ticks_observed increments exactly once
        # per call per open position, regardless of called_from value.
        tracker_env["seed"]([_base_position(side="no", price_cents=79)])
        tracker_env["set_market"](yes_bid=21, no_bid=79)  # at entry
        for _ in range(4):
            tracker.update_positions(called_from="_position_check_loop")
        pos = tracker_env["read"]()[0]
        assert pos["ticks_observed"] == 4
