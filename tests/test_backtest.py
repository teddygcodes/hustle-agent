"""Tests for tools/backtest.py — Session 13b offline back-tester.

Cheap, hand-crafted fixtures only. The back-tester is a tool, not core code;
the runtime --verify-against-clv-report flag is the regression guard.
"""
from __future__ import annotations

import gzip
import inspect
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.strategies import Market  # noqa: E402
from tools.backtest import (  # noqa: E402
    load_universe_snapshots,
    load_clv_records,
    match_clv_record,
    replay_strategy,
    aggregate_results,
    main as backtest_main,
)


def _make_universe_row(ticker="KXTEST-1", scan_id="S1",
                       ts="2026-04-25T12:00:00+00:00", **overrides):
    row = {
        "ts": ts, "scan_id": scan_id, "ticker": ticker,
        "series_ticker": "KXTEST", "event_ticker": "KXTEST-EV",
        "status": "active", "close_ts": "2026-04-26T00:00:00Z",
        "yes_ask": 60, "yes_bid": 58, "no_ask": 42, "no_bid": 40,
        "volume_24h": 100, "open_interest": 50,
        "scanned_by": ["vig_stack_series"], "partial": False,
    }
    row.update(overrides)
    return row


class TestUniverseLoader:
    """Test 1: universe + gzip archive reader filters rows to Market dataclass
    fields and groups by scan_id."""

    def test_reads_live_universe_jsonl(self, tmp_path, monkeypatch):
        live = tmp_path / "universe.jsonl"
        live.write_text(json.dumps(_make_universe_row()) + "\n")
        monkeypatch.setattr("tools.backtest.UNIVERSE_FILE", live)
        monkeypatch.setattr("tools.backtest.ARCHIVE_DIR", tmp_path / "archive")
        snapshots = load_universe_snapshots(
            start=date(2026, 4, 25), end=date(2026, 4, 25),
        )
        assert "S1" in snapshots
        assert snapshots["S1"][0].ticker == "KXTEST-1"
        # scanned_by/partial filtered out — Market(**row) would crash otherwise
        assert not hasattr(snapshots["S1"][0], "scanned_by")

    def test_reads_gzipped_archive(self, tmp_path, monkeypatch):
        archive = tmp_path / "archive"
        archive.mkdir()
        gz = archive / "universe-2026-04-24.jsonl.gz"
        with gzip.open(gz, "wt") as f:
            f.write(json.dumps(_make_universe_row(
                ticker="KXTEST-2", scan_id="S0",
                ts="2026-04-24T12:00:00+00:00",
                volume_24h=200,
            )) + "\n")
        monkeypatch.setattr("tools.backtest.UNIVERSE_FILE", tmp_path / "no-live")
        monkeypatch.setattr("tools.backtest.ARCHIVE_DIR", archive)
        snapshots = load_universe_snapshots(
            start=date(2026, 4, 24), end=date(2026, 4, 24),
        )
        assert snapshots["S0"][0].volume_24h == 200


class TestClvJoin:
    """Test 2: ±60s ticker+ts join matches/skips correctly."""

    def test_match_within_60s_window(self):
        rec = {"ticker": "KXTEST-1",
               "recorded_at": "2026-04-25T12:00:30+00:00",
               "status": "settled", "clv_cents": 5.0}
        # Snapshot 30s before recorded_at -> within ±60s
        assert match_clv_record("KXTEST-1",
                                "2026-04-25T12:00:00+00:00", [rec]) is rec

    def test_no_match_outside_window(self):
        rec = {"ticker": "KXTEST-1",
               "recorded_at": "2026-04-25T12:02:00+00:00",
               "status": "settled", "clv_cents": 5.0}
        # 120s gap -> outside ±60s
        assert match_clv_record("KXTEST-1",
                                "2026-04-25T12:00:00+00:00", [rec]) is None

    def test_skips_unsettled_records(self):
        rec = {"ticker": "KXTEST-1",
               "recorded_at": "2026-04-25T12:00:00+00:00",
               "status": "open", "clv_cents": None}
        assert match_clv_record("KXTEST-1",
                                "2026-04-25T12:00:00+00:00", [rec]) is None

    def test_includes_counterfactual_settled(self):
        rec = {"ticker": "KXTEST-1",
               "recorded_at": "2026-04-25T12:00:00+00:00",
               "status": "counterfactual_settled", "clv_cents": -3.0}
        assert match_clv_record("KXTEST-1",
                                "2026-04-25T12:00:00+00:00", [rec]) is rec


@pytest.fixture
def _no_network():
    """Patch the NWS fetch so replay tests don't hit the network.
    replay_strategy calls _fetch_vig_stack_forecasts() once at start to
    cache. Tests don't care about the forecast content — short-circuit to {}."""
    with patch(
        "bot.strategies.vig_stack_series._fetch_vig_stack_forecasts",
        return_value={},
    ):
        yield


class TestReplayStrategy:
    """Test 3: stub Strategy + 2 hand-crafted snapshots -> opps as expected.
    Asserts candidate_markets called once per snapshot, evaluate per market,
    finalize NEVER called (CF emission would write production state)."""

    def test_replay_calls_candidate_then_evaluate_skips_finalize(self, _no_network):
        m1 = Market(
            ticker="KXTEST-1", series_ticker="KXTEST", event_ticker="EV",
            status="active", close_ts=None,
            yes_ask=60, yes_bid=58, no_ask=42, no_bid=40,
            volume_24h=100, open_interest=50,
            ts="2026-04-25T12:00:00+00:00", scan_id="S1",
        )
        m2 = Market(
            ticker="KXTEST-2", series_ticker="KXTEST", event_ticker="EV",
            status="active", close_ts=None,
            yes_ask=80, yes_bid=78, no_ask=22, no_bid=20,
            volume_24h=100, open_interest=50,
            ts="2026-04-25T12:00:00+00:00", scan_id="S1",
        )
        snapshots = {"S1": [m1, m2]}

        class StubStrategy:
            name = "stub"

            def __init__(self):
                self.calls = {"candidate": 0, "evaluate": 0, "finalize": 0}

            def name_for(self, market):
                return self.name

            def candidate_markets(self, universe):
                self.calls["candidate"] += 1
                return universe

            def evaluate(self, market):
                self.calls["evaluate"] += 1
                return {"ticker": market.ticker, "edge": 0.05,
                        "opp_type": "stub", "recommended_side": "yes",
                        "price_cents": int(market.yes_ask)}

            def finalize(self, scan_id):
                self.calls["finalize"] += 1
                raise RuntimeError(
                    "finalize() must NOT be called by back-tester")

        strat = StubStrategy()
        results = replay_strategy(strat, snapshots)

        assert strat.calls["candidate"] == 1
        assert strat.calls["evaluate"] == 2
        assert strat.calls["finalize"] == 0
        assert len(results) == 2
        assert all("scan_id" in r and "snapshot_ts" in r for r in results)
        assert {r["opp"]["ticker"] for r in results} == {"KXTEST-1", "KXTEST-2"}
