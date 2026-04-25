"""Tests for bot/decisions.py — append-only JSONL audit log.

Session 6 (Apr 24 closed-loop data collection): the scanner / executor /
live_watcher all funnel through `log_decision`. These tests pin schema
integrity, atomic append under contention, and the never-raise contract.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import decisions  # noqa: E402


@pytest.fixture
def tmp_decisions_file(tmp_path, monkeypatch):
    f = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)
    return f


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestSchemaIntegrity:

    def test_basic_record_has_required_fields(self, tmp_decisions_file):
        decisions.log_decision(
            ticker="KXHIGHMIA-26APR24-T80",
            opp_type="vig_stack_series",
            edge=0.152,
            gates={"low_liquidity": True, "no_vig": True, "edge_below_threshold": False},
            decision="reject",
            reason="edge_below_threshold",
        )
        recs = _read_records(tmp_decisions_file)
        assert len(recs) == 1
        r = recs[0]
        assert r["ticker"] == "KXHIGHMIA-26APR24-T80"
        assert r["opp_type"] == "vig_stack_series"
        assert r["edge"] == 0.152
        assert r["decision"] == "reject"
        assert r["reason"] == "edge_below_threshold"
        assert r["gates"]["edge_below_threshold"] is False
        assert "ts" in r and r["ts"].endswith("+00:00")

    def test_none_edge_serializes_as_null(self, tmp_decisions_file):
        decisions.log_decision(
            ticker="X", opp_type="live_momentum", edge=None,
            gates={"can_enter": False}, decision="reject", reason="cooldown",
        )
        recs = _read_records(tmp_decisions_file)
        assert recs[0]["edge"] is None

    def test_extra_dict_round_trips(self, tmp_decisions_file):
        extra = {"contracts": 4, "price_cents": 87, "side": "no"}
        decisions.log_decision(
            ticker="X", opp_type="vig_stack_series", edge=0.20,
            gates={"all": True}, decision="accept", reason="all_gates_passed",
            extra=extra,
        )
        recs = _read_records(tmp_decisions_file)
        assert recs[0]["extra"] == extra

    def test_extra_omitted_when_none(self, tmp_decisions_file):
        decisions.log_decision(
            ticker="X", opp_type="vig_stack_series", edge=0.20,
            gates={"a": True}, decision="accept", reason="ok",
        )
        recs = _read_records(tmp_decisions_file)
        assert "extra" not in recs[0]

    def test_edge_rounded_to_4_decimals(self, tmp_decisions_file):
        decisions.log_decision(
            ticker="X", opp_type="vig_stack_series", edge=0.123456789,
            gates={"a": True}, decision="reject", reason="x",
        )
        recs = _read_records(tmp_decisions_file)
        assert recs[0]["edge"] == 0.1235

    def test_record_carries_regime_dict(self, tmp_decisions_file):
        """Session 14: every decision row gets `regime` with all 4 axes."""
        decisions.log_decision(
            ticker="KXNBAGAME-26APR25-LAL",
            opp_type="vig_stack_series",
            edge=0.12,
            gates={"min_edge": True},
            decision="accept",
            reason="ok",
            extra={"close_ts": "2026-04-26T03:00:00+00:00"},
        )
        r = _read_records(tmp_decisions_file)[-1]
        assert "regime" in r
        regime = r["regime"]
        assert set(regime.keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr",
        }
        # close_ts was passed through extra → event_horizon_hr should populate
        assert regime["event_horizon_hr"] is not None
        # NBA ticker → sport_phase resolves
        assert regime["sport_phase"] in {"preseason", "regular", "playoffs", "off"}

    def test_regime_present_when_extra_omitted(self, tmp_decisions_file):
        """Tagger handles None market_state — regime still populates other axes."""
        decisions.log_decision(
            ticker="KXWX-NYC", opp_type="weather_arb", edge=0.05,
            gates={"x": True}, decision="reject", reason="y",
        )
        r = _read_records(tmp_decisions_file)[-1]
        assert set(r["regime"].keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr",
        }
        assert r["regime"]["event_horizon_hr"] is None  # no close_ts available
        assert r["regime"]["sport_phase"] is None       # non-sport ticker


class TestAtomicAppend:

    def test_concurrent_writes_all_land(self, tmp_decisions_file):
        """20 threads × 10 writes each = 200 records, one per line, JSON-parseable."""
        N_THREADS = 20
        N_PER_THREAD = 10

        def writer(thread_id):
            for i in range(N_PER_THREAD):
                decisions.log_decision(
                    ticker=f"T{thread_id}-{i}",
                    opp_type="vig_stack_series",
                    edge=0.1,
                    gates={"a": True},
                    decision="reject",
                    reason="contention",
                )

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        recs = _read_records(tmp_decisions_file)
        assert len(recs) == N_THREADS * N_PER_THREAD
        assert {r["ticker"] for r in recs} == {
            f"T{t}-{i}" for t in range(N_THREADS) for i in range(N_PER_THREAD)
        }

    def test_creates_state_dir_if_missing(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "nest" / "state"
        f = nested / "decisions.jsonl"
        monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
        monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", nested)

        decisions.log_decision(
            ticker="X", opp_type="vig_stack_series", edge=0.1,
            gates={"a": True}, decision="reject", reason="r",
        )
        assert f.exists()
        assert len(_read_records(f)) == 1


class TestNeverRaises:
    """The trade path must never blow up because of audit-log failure."""

    def test_disk_failure_is_swallowed(self, tmp_decisions_file):
        with patch("bot.decisions.open", side_effect=OSError("disk full")):
            # Must not raise.
            decisions.log_decision(
                ticker="X", opp_type="vig_stack_series", edge=0.1,
                gates={"a": True}, decision="reject", reason="r",
            )
        # Nothing got written.
        assert _read_records(tmp_decisions_file) == []


class TestScannerGateExtra:
    """Session 10 — scanner gate rejects must include distance-from-threshold
    fields in extra. Schema tests confirm the shape; helper tests pin the
    distance math."""

    def test_low_liquidity_extra_includes_min_required(self, tmp_decisions_file):
        decisions.log_decision(
            ticker="KX-T", opp_type="vig_stack_series", edge=None,
            gates={"low_liquidity": False, "no_vig": True},
            decision="reject", reason="low_liquidity",
            extra={"volume": 5, "open_interest": 2,
                   "min_volume": 10, "min_open_interest": 5},
        )
        recs = _read_records(tmp_decisions_file)
        e = recs[0]["extra"]
        assert e["min_volume"] == 10
        assert e["min_open_interest"] == 5
        assert e["volume"] < e["min_volume"]
        assert e["open_interest"] < e["min_open_interest"]

    def test_forecast_in_bucket_distance_negative_when_inside(self):
        from bot.strategies.vig_stack_series import _forecast_distance_from_bucket
        # forecast=72, bucket [70,80]: inside, depth = min(72-70, 80-72) = 2 → distance = -2.0
        assert _forecast_distance_from_bucket(72.0, 70.0, 80.0) == -2.0
        # forecast=75, dead-center: depth = min(5, 5) = 5 → distance = -5.0
        assert _forecast_distance_from_bucket(75.0, 70.0, 80.0) == -5.0

    def test_forecast_in_bucket_distance_positive_when_outside_margin(self):
        from bot.strategies.vig_stack_series import _forecast_distance_from_bucket
        # forecast=81, bucket [70,80]: outside but within +1° → distance = 1.0
        assert _forecast_distance_from_bucket(81.0, 70.0, 80.0) == 1.0
        # forecast=68, bucket [70,80]: outside on low side, 2° below
        assert _forecast_distance_from_bucket(68.0, 70.0, 80.0) == 2.0
        # forecast at exact edge → distance = 0
        assert _forecast_distance_from_bucket(70.0, 70.0, 80.0) == 0.0

    def test_edge_below_threshold_extra_includes_edge_vig_tts(self, tmp_decisions_file):
        decisions.log_decision(
            ticker="KX-E", opp_type="vig_stack_series", edge=0.0150,
            gates={"low_liquidity": True, "edge_below_threshold": False},
            decision="reject", reason="edge_below_threshold",
            extra={"min_edge": 0.02, "edge": 0.015,
                   "vig": 7.5, "time_to_settle_hr": 6.5},
        )
        e = _read_records(tmp_decisions_file)[0]["extra"]
        assert e["edge"] == 0.015
        assert e["min_edge"] == 0.02
        assert e["vig"] == 7.5
        assert e["time_to_settle_hr"] == 6.5

    def test_edge_below_threshold_tolerates_none_tts(self, tmp_decisions_file):
        # If close_time can't be parsed, time_to_settle_hr should be null
        decisions.log_decision(
            ticker="KX-E2", opp_type="vig_stack_series", edge=0.005,
            gates={"edge_below_threshold": False}, decision="reject",
            reason="edge_below_threshold",
            extra={"min_edge": 0.02, "edge": 0.005,
                   "vig": 6.0, "time_to_settle_hr": None},
        )
        e = _read_records(tmp_decisions_file)[0]["extra"]
        assert e["time_to_settle_hr"] is None
