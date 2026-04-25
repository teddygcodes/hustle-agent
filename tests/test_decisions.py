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
