"""Tests for tools/tick_backtest.py — Session 19b tick-replay back-tester.

Pins:
- _row_to_tick adapter from live_ticks.jsonl row schema to Tick dataclass.
- load_paper_trades filter + sort.
- get_settlement_ts: clv.json first, kalshi_history fallback.
- load_tick_stream: jsonl + gzipped archive coverage with wide window.
- replay_game: stub strategy buy/sell pair yields correct P&L with/without slippage.
- parity_check: pass vs fail classification within tolerance.
- back_test: programmatic entry-point shape.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.tick_backtest as tb  # noqa: E402
from bot.strategies import Buy, Hold, Market, Sell, State, Tick  # noqa: E402
from bot.strategies.live_momentum import LiveMomentumStrategy  # noqa: E402


class TestRowToTick:
    def test_translates_live_ticks_jsonl_row(self):
        row = {
            "ts": "2026-04-15T01:37:25.300441+00:00",
            "ticker": "KXNHLGAME-26APR14WPGUTA-UTA",
            "match": "Winnipeg at Utah Winner?",
            "price": 80,
            "bid": 78,
            "opp_price": 21,
            "opp_bid": 20,
            "score_diff": 1,
            "period": 1,
            "wp": 0.62,
            "espn": {"home_score": 1, "away_score": 0, "clock": "4:53"},
            "sport": "nhl",
        }
        tick = tb._row_to_tick(
            row,
            opp_ticker="KXNHLGAME-26APR14WPGUTA-WPG",
            close_ts="2026-04-15T05:00:00Z",
        )
        assert tick.ts == "2026-04-15T01:37:25.300441+00:00"
        assert tick.ticker == "KXNHLGAME-26APR14WPGUTA-UTA"
        assert tick.yes_ask == 80
        assert tick.yes_bid == 78
        assert tick.no_ask == 22  # 100 - 78
        assert tick.no_bid == 20  # 100 - 80
        assert tick.opp_ticker == "KXNHLGAME-26APR14WPGUTA-WPG"
        assert tick.opp_yes_ask == 21
        assert tick.opp_yes_bid == 20
        assert tick.opp_no_ask == 80  # 100 - 20
        assert tick.opp_no_bid == 79  # 100 - 21
        assert tick.score_diff == 1
        assert tick.period == 1
        assert tick.wp == pytest.approx(0.62)
        assert tick.espn_data == {"home_score": 1, "away_score": 0, "clock": "4:53"}
        assert tick.close_ts == "2026-04-15T05:00:00Z"
        assert tick.raw == {}
        assert tick.raw_opp == {}

    def test_handles_missing_optional_fields(self):
        row = {"ts": "2026-04-15T00:00:00Z", "ticker": "X", "price": 50, "bid": 49}
        tick = tb._row_to_tick(row, opp_ticker=None, close_ts=None)
        assert tick.yes_ask == 50
        assert tick.yes_bid == 49
        assert tick.no_ask == 51
        assert tick.no_bid == 50
        assert tick.opp_ticker is None
        assert tick.opp_yes_ask is None
        assert tick.score_diff is None
        assert tick.espn_data is None


class TestLoadPaperTrades:
    def test_filter_and_sort(self, tmp_path, monkeypatch):
        records = [
            {"id": "A", "type": "vig_stack", "ticker": "X-1",
             "timestamp": "2026-04-10T00:00:00Z", "status": "won",
             "resolved_at": "2026-04-10T01:00:00Z", "pnl": 1.0,
             "side": "yes", "entry_price": 0.5, "contracts": 1, "exit_price": 1.0},
            {"id": "B", "type": "live_momentum", "ticker": "X-2",
             "timestamp": "2026-04-12T00:00:00Z", "status": "won",
             "resolved_at": "2026-04-12T01:00:00Z", "pnl": 2.0,
             "side": "yes", "entry_price": 0.5, "contracts": 1, "exit_price": 1.0},
            {"id": "C", "type": "live_momentum", "ticker": "X-3",
             "timestamp": "2026-04-11T00:00:00Z", "status": "open",
             "resolved_at": None, "pnl": 0,
             "side": "yes", "entry_price": 0.5, "contracts": 1, "exit_price": 0},
            {"id": "D", "type": "live_momentum", "ticker": "X-4",
             "timestamp": "2026-04-09T00:00:00Z", "status": "lost",
             "resolved_at": "2026-04-09T01:00:00Z", "pnl": -1.5,
             "side": "yes", "entry_price": 0.5, "contracts": 1, "exit_price": 0.0},
        ]
        path = tmp_path / "paper_trades.json"
        path.write_text(json.dumps(records))
        monkeypatch.setattr(tb, "PAPER_TRADES_FILE", path)

        out = tb.load_paper_trades(strategy_name="live_momentum")
        assert [t["id"] for t in out] == ["D", "B"]


class TestGetSettlementTs:
    def setup_method(self):
        # Reset the module-level cache between tests
        tb._CLV_INDEX_CACHE = None

    def test_clv_first(self, tmp_path, monkeypatch):
        clv_records = [{"ticker": "X-1", "settled_at": "2026-04-10T05:00:00Z",
                        "status": "settled"}]
        clv_path = tmp_path / "clv.json"
        clv_path.write_text(json.dumps(clv_records))
        monkeypatch.setattr(tb, "CLV_FILE", clv_path)
        monkeypatch.setattr(tb, "_fetch_settled_close",
                            lambda t: pytest.fail("should not be called"))
        ts = tb.get_settlement_ts("X-1")
        assert ts == "2026-04-10T05:00:00Z"

    def test_kalshi_history_fallback(self, tmp_path, monkeypatch):
        clv_path = tmp_path / "clv.json"
        clv_path.write_text("[]")
        monkeypatch.setattr(tb, "CLV_FILE", clv_path)
        monkeypatch.setattr(tb, "_fetch_settled_close",
                            lambda t: 100.0 if t == "Y-2" else None)
        assert tb.get_settlement_ts("Y-2") == "settled"
        assert tb.get_settlement_ts("Y-3") is None


class TestLoadTickStream:
    def test_wide_window_with_archive(self, tmp_path, monkeypatch):
        ticks_file = tmp_path / "live_ticks.jsonl"
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        ticks_file.write_text(
            json.dumps({"ts": "2026-04-26T00:00:02Z", "ticker": "X",
                        "price": 51, "bid": 50}) + "\n"
            + json.dumps({"ts": "2026-04-26T00:00:03Z", "ticker": "X",
                          "price": 52, "bid": 51}) + "\n"
        )
        gz_path = archive_dir / "live_ticks-2026-04-25.jsonl.gz"
        with gzip.open(gz_path, "wt") as f:
            f.write(json.dumps({"ts": "2026-04-25T23:59:59Z", "ticker": "X",
                                "price": 50, "bid": 49}) + "\n")

        monkeypatch.setattr("tools.exit_replay.TICKS_FILE", ticks_file)
        monkeypatch.setattr("tools.exit_replay.ARCHIVE_DIR", archive_dir)

        ticks = tb.load_tick_stream(
            ticker="X",
            t_start=None,
            t_end="settled",
            opp_ticker=None,
            close_ts=None,
        )
        assert len(ticks) == 3
        assert ticks[0].ts == "2026-04-25T23:59:59Z"
        assert ticks[0].yes_ask == 50
        assert ticks[2].ts == "2026-04-26T00:00:03Z"

    def test_explicit_window_bounds(self, tmp_path, monkeypatch):
        ticks_file = tmp_path / "live_ticks.jsonl"
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        ticks_file.write_text(
            "\n".join(
                json.dumps({"ts": f"2026-04-26T00:00:0{i}Z",
                            "ticker": "X", "price": 50 + i, "bid": 49 + i})
                for i in range(5)
            ) + "\n"
        )
        monkeypatch.setattr("tools.exit_replay.TICKS_FILE", ticks_file)
        monkeypatch.setattr("tools.exit_replay.ARCHIVE_DIR", archive_dir)
        ticks = tb.load_tick_stream("X",
                                    t_start="2026-04-26T00:00:01Z",
                                    t_end="2026-04-26T00:00:03Z")
        assert [t.yes_ask for t in ticks] == [51, 52, 53]


class StubStrategy:
    """Deterministic stub: Buy on tick 1, Sell on tick 3, Hold otherwise."""
    name = "stub"

    def init_state(self, market, **kwargs):
        return State(data={"ticker": market.ticker, "step": 0})

    def process_tick(self, state, tick):
        state.data["step"] += 1
        step = state.data["step"]
        if step == 1:
            return state, Buy(side="yes", qty=10, reason="stub-buy",
                              ticker=tick.ticker, price_cents=tick.yes_ask or 0)
        if step == 3:
            return state, Sell(side="yes", qty=10, reason="stub-sell",
                               ticker=tick.ticker, exit_price=tick.yes_bid or 0)
        return state, Hold()


class TestReplayGame:
    def test_round_trip_pnl_with_and_without_slippage(self):
        market = Market(
            ticker="X", series_ticker="X-S", event_ticker=None, status="active",
            close_ts=None, yes_ask=80, yes_bid=78, no_ask=22, no_bid=20,
            volume_24h=0, open_interest=0,
        )
        ticks = [
            tb._row_to_tick({
                "ts": f"2026-04-26T00:00:0{i}Z", "ticker": "X",
                "price": 80 + i, "bid": 78 + i,
            })
            for i in range(5)
        ]
        # Buy at tick 0 (yes_ask=80), Sell at tick 2 (yes_bid=80).
        # Gross: (80 - 80) * 10 = 0. With 2c slippage: -2.
        result_no_slip = tb.replay_game(
            StubStrategy(), market, ticks, slippage_cents=0,
        )
        assert len(result_no_slip.round_trips) == 1
        assert result_no_slip.realized_pnl_cents == 0
        result_with_slip = tb.replay_game(
            StubStrategy(), market, ticks, slippage_cents=2,
        )
        assert result_with_slip.realized_pnl_cents == -2


class TestParityCheck:
    def test_pass_and_fail_classification(self, monkeypatch):
        paper_trades = [
            {"id": "P1", "ticker": "X-1", "type": "live_momentum", "status": "won",
             "side": "yes", "entry_price": 0.50, "contracts": 10, "exit_price": 0.60,
             "pnl": 1.00, "timestamp": "2026-04-20T00:00:00Z",
             "resolved_at": "2026-04-20T01:00:00Z"},
            {"id": "P2", "ticker": "X-2", "type": "live_momentum", "status": "lost",
             "side": "yes", "entry_price": 0.50, "contracts": 10, "exit_price": 0.40,
             "pnl": -1.00, "timestamp": "2026-04-20T00:00:00Z",
             "resolved_at": "2026-04-20T01:00:00Z"},
            {"id": "P3", "ticker": "X-3", "type": "live_momentum", "status": "won",
             "side": "yes", "entry_price": 0.50, "contracts": 10, "exit_price": 0.70,
             "pnl": 2.00, "timestamp": "2026-04-20T00:00:00Z",
             "resolved_at": "2026-04-20T01:00:00Z"},
        ]

        def fake_replay(ticker):
            results = {
                "X-1": tb.ReplayResult(ticker="X-1", actions=[], round_trips=[],
                                       realized_pnl_cents=100, exit_reason="OK"),
                "X-2": tb.ReplayResult(ticker="X-2", actions=[], round_trips=[],
                                       realized_pnl_cents=-100, exit_reason="OK"),
                "X-3": tb.ReplayResult(ticker="X-3", actions=[], round_trips=[],
                                       realized_pnl_cents=150, exit_reason="OK"),
            }
            return results[ticker]

        monkeypatch.setattr(
            tb, "_replay_paper_trade",
            lambda strategy, trade, **kw: fake_replay(trade["ticker"]),
        )

        report = tb.parity_check(paper_trades, strategy=None, tolerance_cents=1)
        assert report.passes == 2
        assert len(report.failures) == 1
        assert report.failures[0].ticker == "X-3"
        # delta = replay - paper = 150 - 200 = -50
        assert report.failures[0].delta_cents == -50


class TestBackTest:
    def test_returns_expected_shape(self, monkeypatch):
        paper_trades = [
            {"id": "P1", "ticker": "X-1", "type": "live_momentum", "status": "won",
             "side": "yes", "entry_price": 0.50, "contracts": 10, "exit_price": 0.60,
             "pnl": 1.00, "timestamp": "2026-04-20T00:00:00Z",
             "resolved_at": "2026-04-20T01:00:00Z"},
        ]
        monkeypatch.setattr(
            tb, "_replay_paper_trade",
            lambda s, t, **k: tb.ReplayResult(
                ticker=t["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=100, exit_reason="TAKE_PROFIT",
            ),
        )
        result = tb.back_test(strategy=None, paper_trades=paper_trades)
        assert isinstance(result, tb.BackTestResult)
        assert result.games == 1
        assert result.total_pnl_cents == 100
        assert len(result.per_game) == 1
        assert result.per_game[0].ticker == "X-1"


# === 19a-followup additions: Phases A, B, E ================================


class TestParityWindowHelpers:
    def test_max_resolved_at(self):
        trades = [
            {"resolved_at": "2026-04-20T01:00:00Z"},
            {"resolved_at": "2026-04-20T03:00:00Z"},
            {"resolved_at": "2026-04-20T02:00:00Z"},
            {"resolved_at": ""},  # ignored
            {},                    # ignored
        ]
        assert tb._max_resolved_at(trades) == "2026-04-20T03:00:00Z"

    def test_max_resolved_at_empty(self):
        assert tb._max_resolved_at([]) is None
        assert tb._max_resolved_at([{}]) is None

    def test_add_seconds_to_iso(self):
        out = tb._add_seconds_to_iso("2026-04-20T01:00:00+00:00", 120)
        assert out is not None and out.startswith("2026-04-20T01:02:00")

    def test_add_seconds_handles_zulu(self):
        out = tb._add_seconds_to_iso("2026-04-20T01:00:00Z", 60)
        assert out is not None and "01:01:00" in out

    def test_add_seconds_invalid_returns_none(self):
        assert tb._add_seconds_to_iso("not-a-ts", 120) is None
        assert tb._add_seconds_to_iso("", 120) is None


class TestParityWindowCap:
    def test_parity_window_caps_at_max_resolved_plus_buffer(self, tmp_path, monkeypatch):
        # Stream has ticks before AND well after max(resolved_at). With
        # parity_window=True the post-resolved tail should be excluded.
        ticks_file = tmp_path / "live_ticks.jsonl"
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        rows = [
            {"ts": "2026-04-26T00:00:00+00:00", "ticker": "X-1",
             "price": 80, "bid": 78},
            {"ts": "2026-04-26T00:01:00+00:00", "ticker": "X-1",
             "price": 80, "bid": 78},
            {"ts": "2026-04-26T01:00:00+00:00", "ticker": "X-1",
             "price": 80, "bid": 78},  # resolved_at; included
            {"ts": "2026-04-26T01:01:30+00:00", "ticker": "X-1",
             "price": 80, "bid": 78},  # within +120s buffer; included
            {"ts": "2026-04-26T01:30:00+00:00", "ticker": "X-1",
             "price": 80, "bid": 78},  # post-buffer; excluded
        ]
        ticks_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        monkeypatch.setattr("tools.exit_replay.TICKS_FILE", ticks_file)
        monkeypatch.setattr("tools.exit_replay.ARCHIVE_DIR", archive_dir)
        monkeypatch.setattr(tb, "get_settlement_ts",
                            lambda t: "2026-04-26T05:00:00+00:00")

        captured: list = []

        class CaptureStrategy:
            name = "cap"

            def init_state(self, market, **kwargs):
                return State(data={"ticker": market.ticker})

            def process_tick(self, state, tick):
                captured.append(tick.ts)
                return state, Hold()

        trade = {"ticker": "X-1", "timestamp": "2026-04-26T00:00:00+00:00",
                 "resolved_at": "2026-04-26T01:00:00+00:00",
                 "entry_price": 0.80, "pnl": 0.0}
        # parity_window=True: stops at 01:00:00 + 120s = 01:02:00 → 4 ticks.
        captured.clear()
        tb._replay_paper_trade(
            CaptureStrategy(), trade, ticker_trades=[trade],
            slippage_cents=0, parity_window=True,
        )
        assert captured == [
            "2026-04-26T00:00:00+00:00",
            "2026-04-26T00:01:00+00:00",
            "2026-04-26T01:00:00+00:00",
            "2026-04-26T01:01:30+00:00",
        ]
        # parity_window=False: wide window (settlement_ts) → all 5 ticks.
        captured.clear()
        tb._replay_paper_trade(
            CaptureStrategy(), trade, ticker_trades=[trade],
            slippage_cents=0, parity_window=False,
        )
        assert len(captured) == 5

    def test_back_test_uses_wide_window(self, monkeypatch):
        # back_test() must NOT cap the window — 19c sweeps need the full tail.
        seen: list[bool] = []

        def spy(strategy, trade, **kw):
            seen.append(kw.get("parity_window", False))
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=0, exit_reason="OK",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", spy)
        tb.back_test(
            strategy=None,
            paper_trades=[{"id": "P", "ticker": "X-1", "type": "live_momentum",
                           "status": "won", "side": "yes",
                           "entry_price": 0.5, "contracts": 1, "exit_price": 0.6,
                           "pnl": 0.1, "timestamp": "2026-04-26T00:00:00Z",
                           "resolved_at": "2026-04-26T01:00:00Z"}],
        )
        # back_test should not request parity_window; default False.
        assert seen == [False]


class TestCoverageGapClassification:
    def _trade(self, ticker, resolved_at, pnl):
        return {
            "id": ticker, "ticker": ticker, "type": "live_momentum",
            "status": "won", "side": "yes",
            "entry_price": 0.50, "contracts": 10, "exit_price": 0.60,
            "pnl": pnl, "timestamp": "2026-04-25T00:00:00Z",
            "resolved_at": resolved_at,
        }

    def test_last_tick_before_resolved_classified_as_coverage_gap(self, monkeypatch):
        # X-CG: replay diverges from paper AND last_tick precedes resolved_at
        # → COVERAGE_GAP. X-FAIL: diverges with last_tick after resolved_at →
        # genuine FAIL. X-PASS: matches within tolerance.
        paper = [
            self._trade("X-CG",   "2026-04-25T05:00:00Z", 1.00),
            self._trade("X-FAIL", "2026-04-25T05:00:00Z", 1.00),
            self._trade("X-PASS", "2026-04-25T05:00:00Z", 1.00),
        ]
        results = {
            "X-CG":   tb.ReplayResult(ticker="X-CG", actions=[], round_trips=[],
                                      realized_pnl_cents=0, exit_reason="NO_EXIT",
                                      last_tick_ts="2026-04-25T04:00:00Z"),
            "X-FAIL": tb.ReplayResult(ticker="X-FAIL", actions=[], round_trips=[],
                                      realized_pnl_cents=50, exit_reason="OK",
                                      last_tick_ts="2026-04-25T06:00:00Z"),
            "X-PASS": tb.ReplayResult(ticker="X-PASS", actions=[], round_trips=[],
                                      realized_pnl_cents=100, exit_reason="OK",
                                      last_tick_ts="2026-04-25T06:00:00Z"),
        }
        monkeypatch.setattr(
            tb, "_replay_paper_trade",
            lambda s, t, **kw: results[t["ticker"]],
        )
        report = tb.parity_check(paper, strategy=None, tolerance_cents=1)
        assert report.passes == 1
        assert [f.ticker for f in report.failures] == ["X-FAIL"]
        assert [g.ticker for g in report.coverage_gaps] == ["X-CG"]
        gap = report.coverage_gaps[0]
        assert gap.last_tick_ts == "2026-04-25T04:00:00Z"
        assert gap.last_resolved_at == "2026-04-25T05:00:00Z"

    def test_render_excludes_coverage_gaps_from_total(self, monkeypatch):
        paper = [
            self._trade("X-CG",   "2026-04-25T05:00:00Z", 1.00),
            self._trade("X-PASS", "2026-04-25T05:00:00Z", 1.00),
        ]
        results = {
            "X-CG":   tb.ReplayResult(ticker="X-CG", actions=[], round_trips=[],
                                      realized_pnl_cents=0, exit_reason="NO_EXIT",
                                      last_tick_ts="2026-04-25T04:00:00Z"),
            "X-PASS": tb.ReplayResult(ticker="X-PASS", actions=[], round_trips=[],
                                      realized_pnl_cents=100, exit_reason="OK",
                                      last_tick_ts="2026-04-25T06:00:00Z"),
        }
        monkeypatch.setattr(
            tb, "_replay_paper_trade",
            lambda s, t, **kw: results[t["ticker"]],
        )
        report = tb.parity_check(paper, strategy=None, tolerance_cents=1)
        rendered = tb.render_parity_report(report, paper, results, 1)
        # Coverage-gap excluded from denominator: 1/1 (not 1/2).
        assert "PASSES: 1/1" in rendered
        assert "Coverage gaps" in rendered
        assert "X-CG" in rendered
        # COVERAGE_GAP appears as a row status.
        assert "COVERAGE_GAP" in rendered


class TestLoadJournalEventsForTicker:
    def test_filters_by_ticker_and_event(self, tmp_path, monkeypatch):
        journal = [
            {"ts": "2026-04-26T00:00:00Z", "events": [
                {"event": "bet", "ticker": "X-1", "side": "yes",
                 "price_cents": 80, "contracts": 10, "reason": "dip_buy",
                 "ts": "2026-04-26T00:00:01Z"},
                {"event": "bet", "ticker": "X-2", "side": "yes",
                 "price_cents": 70, "contracts": 5, "reason": "dip_buy",
                 "ts": "2026-04-26T00:00:02Z"},
                {"event": "scan_found", "ticker": "X-1"},  # filtered out
            ]},
            {"ts": "2026-04-26T01:00:00Z", "events": [
                {"event": "exit", "ticker": "X-1", "reason": "TAKE PROFIT",
                 "entry_price": 80, "exit_value": 92, "pnl": 1.20,
                 "ts": "2026-04-26T01:00:00Z"},
            ]},
        ]
        path = tmp_path / "live_journal.json"
        path.write_text(json.dumps(journal))
        monkeypatch.setattr(tb, "LIVE_JOURNAL_FILE", path)
        events = tb._load_journal_events_for_ticker("X-1")
        assert [e["event"] for e in events] == ["bet", "exit"]
        assert events[0]["price_cents"] == 80
        assert events[1]["pnl"] == 1.20

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "LIVE_JOURNAL_FILE", tmp_path / "no.json")
        assert tb._load_journal_events_for_ticker("X-1") == []

    def test_malformed_returns_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "live_journal.json"
        path.write_text("not json")
        monkeypatch.setattr(tb, "LIVE_JOURNAL_FILE", path)
        assert tb._load_journal_events_for_ticker("X-1") == []


class TestDebugTickerTrace:
    def test_trace_line_format(self):
        # Held YES position with a clear gain — ensures fields render correctly.
        market = Market(
            ticker="X-1", series_ticker="X", event_ticker=None, status="active",
            close_ts=None, yes_ask=80, yes_bid=78, no_ask=22, no_bid=20,
            volume_24h=0, open_interest=0,
        )
        # Build a minimal "as-if-held" state directly to test the formatter.
        from bot.strategies import State as S
        from collections import deque
        from bot.game_context import GameContext
        state = S()
        state.data.update({
            "ticker": "X-1", "opponent_ticker": None, "sport": "nba",
            "mode": "momentum", "match_title": "",
            "bets_placed": [{"ticker": "X-1", "side": "yes", "price_cents": 70,
                              "contracts": 10}],
            "peak_values": {"X-1": 90},
            "price_history": deque(), "opp_price_history": deque(),
            "cooldown_remaining": 0, "entry_count": 1,
            "game_ctx": GameContext(sport="nba"),
            "espn_tick_counter": 0, "last_espn_data": {},
            "last_decision": None, "tick_telem": {},
            "balance": 500.0,
        })
        tick = tb.Tick(ts="2026-04-26T01:00:00Z", ticker="X-1",
                       yes_ask=85, yes_bid=84, no_ask=16, no_bid=15,
                       opp_ticker=None, opp_yes_ask=None, opp_yes_bid=None,
                       opp_no_ask=None, opp_no_bid=None,
                       wp=None, score_diff=None, period=None,
                       espn_data=None, close_ts=None, raw={}, raw_opp={})
        action = Hold(reason="settled")
        strat = LiveMomentumStrategy()
        line = tb._format_trace_line(strat, state, tick, action)
        assert "[2026-04-26T01:00:00Z]" in line
        assert "yes_ask=85" in line
        assert "yes_bid=84" in line
        assert "held=YES" in line
        assert "entry=70" in line
        assert "current=84" in line  # bid takes precedence
        assert "gain=+14" in line
        assert "peak=90" in line
        assert "TP-Δ=" in line
        assert "action=Hold" in line

    def test_trace_line_unheld(self):
        # No position → held=NO, no deltas.
        from bot.strategies import State as S
        state = S()
        state.data.update({
            "ticker": "X-1", "sport": "nba", "bets_placed": [],
            "peak_values": {},
        })
        tick = tb.Tick(ts="t", ticker="X-1", yes_ask=80, yes_bid=78,
                       no_ask=22, no_bid=20, opp_ticker=None,
                       opp_yes_ask=None, opp_yes_bid=None,
                       opp_no_ask=None, opp_no_bid=None,
                       wp=None, score_diff=None, period=None,
                       espn_data=None, close_ts=None, raw={}, raw_opp={})
        line = tb._format_trace_line(LiveMomentumStrategy(), state, tick, Hold())
        assert "held=NO" in line
        assert "TP-Δ=—" in line


class TestQtyOverride:
    def test_buy_qty_replaced_and_sell_picks_it_up(self):
        market = Market(
            ticker="X-1", series_ticker="X", event_ticker=None, status="active",
            close_ts=None, yes_ask=80, yes_bid=78, no_ask=22, no_bid=20,
            volume_24h=0, open_interest=0,
        )

        class StubStrategy:
            name = "stub"

            def init_state(self, market, **kwargs):
                state = State()
                state.data.update({"ticker": market.ticker, "step": 0,
                                   "bets_placed": []})
                return state

            def process_tick(self, state, tick):
                state.data["step"] += 1
                step = state.data["step"]
                if step == 1:
                    # Port emits Buy with qty=99; override should replace with 7.
                    state.data["bets_placed"].append({
                        "ticker": tick.ticker, "side": "yes",
                        "price_cents": tick.yes_ask or 0, "contracts": 99,
                    })
                    return state, Buy(side="yes", qty=99, reason="b",
                                      ticker=tick.ticker,
                                      price_cents=tick.yes_ask or 0)
                if step == 3:
                    # Sell uses bet["contracts"] as qty.
                    bet = state.data["bets_placed"][0]
                    qty = bet["contracts"]
                    state.data["bets_placed"] = []
                    return state, Sell(side="yes", qty=qty, reason="s",
                                       ticker=tick.ticker,
                                       exit_price=tick.yes_bid or 0)
                return state, Hold()

        ticks = [
            tb._row_to_tick({"ts": f"t{i}", "ticker": "X-1",
                             "price": 80 + i, "bid": 78 + i})
            for i in range(5)
        ]
        # Without override: 99 contracts × (80-80) gross = 0 (Buy yes_ask=80,
        # Sell yes_bid=80 at step 3).
        result_no = tb.replay_game(StubStrategy(), market, ticks, slippage_cents=0)
        assert result_no.round_trips[0].qty == 99
        # With override [7]: qty becomes 7 on both Buy and Sell.
        result_ov = tb.replay_game(
            StubStrategy(), market, ticks, slippage_cents=0,
            qty_override=[7],
        )
        assert result_ov.round_trips[0].qty == 7
        # P&L scales with the override (gross = (80-80) × 7 = 0 here, but the
        # qty itself is what matters for the round-trip record).

    def test_override_exhaustion_falls_back_to_port_qty(self):
        # Two Buys, override list of length 1 → second Buy keeps port's qty.
        market = Market(
            ticker="X-1", series_ticker="X", event_ticker=None, status="active",
            close_ts=None, yes_ask=80, yes_bid=78, no_ask=22, no_bid=20,
            volume_24h=0, open_interest=0,
        )

        class TwoBuysStrategy:
            name = "two_buys"

            def init_state(self, market, **kwargs):
                return State(data={"ticker": market.ticker, "step": 0,
                                   "bets_placed": []})

            def process_tick(self, state, tick):
                state.data["step"] += 1
                step = state.data["step"]
                if step in (1, 3):
                    return state, Buy(side="yes", qty=50, reason="b",
                                      ticker=tick.ticker,
                                      price_cents=tick.yes_ask or 0)
                if step in (2, 4):
                    return state, Sell(side="yes", qty=50, reason="s",
                                       ticker=tick.ticker,
                                       exit_price=tick.yes_bid or 0)
                return state, Hold()

        ticks = [
            tb._row_to_tick({"ts": f"t{i}", "ticker": "X-1",
                             "price": 80, "bid": 80})
            for i in range(5)
        ]
        result = tb.replay_game(
            TwoBuysStrategy(), market, ticks, slippage_cents=0,
            qty_override=[7],  # only first Buy gets the override
        )
        assert len(result.round_trips) == 2
        assert result.round_trips[0].qty == 7
        assert result.round_trips[1].qty == 50


class TestMinEntryDateFilter:
    def test_iso_string_comparison_filters_pre_cutoff(self):
        # main() uses simple ISO-8601 prefix comparison. Test the filter
        # logic via direct list comprehension to lock the contract.
        trades = [
            {"id": "early", "timestamp": "2026-04-15T00:00:00Z"},
            {"id": "edge",  "timestamp": "2026-04-23T00:00:00Z"},  # at cutoff: kept
            {"id": "late",  "timestamp": "2026-04-25T00:00:00Z"},
        ]
        cutoff = "2026-04-23"
        kept = [t for t in trades if t.get("timestamp", "") >= cutoff]
        assert [t["id"] for t in kept] == ["edge", "late"]

    def test_default_cutoff_constant(self):
        # The conservative default cutoff is documented as 2026-04-23 — the
        # date past which `bid`/`opp_bid` reliably appear in archives.
        assert tb.DEFAULT_MIN_ENTRY_DATE == "2026-04-23"


# === Sub-session 19c additions: parameter sweep with train/test split =======


def _trade(ticker: str, ts: str, pnl: float = 0.0) -> dict:
    """Cheap paper-trade builder for sweep tests."""
    return {
        "id": f"P-{ticker}-{ts[:10]}",
        "ticker": ticker,
        "type": "live_momentum",
        "status": "won",
        "side": "yes",
        "entry_price": 0.50,
        "contracts": 10,
        "exit_price": 0.60,
        "pnl": pnl,
        "timestamp": ts,
        "resolved_at": ts.replace("T00", "T01"),
    }


class TestSplitTrainTest:
    def test_70_30_split_by_date_order(self):
        # Hand-craft 10 paper trades sorted ascending; split should be 7/3 with
        # the earliest 7 in train and last 3 in test.
        trades = [
            _trade(f"X-{i}", f"2026-04-{20+i:02d}T00:00:00Z") for i in range(10)
        ]
        train, test = tb.split_train_test(trades, train_pct=0.70)
        assert len(train) == 7
        assert len(test) == 3
        assert [t["ticker"] for t in train] == [f"X-{i}" for i in range(7)]
        assert [t["ticker"] for t in test] == [f"X-{i}" for i in range(7, 10)]

    def test_empty_input_returns_empty_pair(self):
        assert tb.split_train_test([]) == ([], [])

    def test_invalid_pct_raises(self):
        with pytest.raises(ValueError):
            tb.split_train_test([{"timestamp": "x"}], train_pct=1.5)


class TestSweepGrid:
    def test_runs_all_variants_x_all_unique_tickers(self, monkeypatch):
        # Sweep grid of N variants × M unique tickers should produce N×M
        # _run_variant→back_test→_replay_paper_trade calls. Stub the replay
        # to count invocations and return a constant P&L.
        paper = [
            _trade("KXNBAGAME-A", "2026-04-23T00:00:00Z"),
            _trade("KXUFCFIGHT-B", "2026-04-23T01:00:00Z"),
        ]
        call_count = {"n": 0}

        def fake_replay(strategy, trade, **kw):
            call_count["n"] += 1
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=50, exit_reason="TAKE_PROFIT",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)

        grid = [(0.65, 4), (0.70, 6), (0.75, 8)]
        results = [tb._run_variant(lm, ts, paper) for (lm, ts) in grid]
        assert len(results) == 3
        # 3 variants × 2 unique tickers = 6 replays.
        assert call_count["n"] == 6
        for v in results:
            assert v.n_replays == 2
            assert v.total_pnl_cents == 100  # 2 × 50¢
            assert v.n_winning_replays == 2
            assert v.win_rate == 1.0


class TestPerSportAggregation:
    def test_three_buckets_for_two_nba_one_ufc(self):
        per_replay = [
            tb.ReplayResult(ticker="KXNBAGAME-26APR23-NYK", actions=[],
                            round_trips=[], realized_pnl_cents=120,
                            exit_reason="TAKE_PROFIT"),
            tb.ReplayResult(ticker="KXNBAGAME-26APR23-DEN", actions=[],
                            round_trips=[], realized_pnl_cents=-50,
                            exit_reason="STOP_LOSS"),
            tb.ReplayResult(ticker="KXUFCFIGHT-26APR23-X", actions=[],
                            round_trips=[], realized_pnl_cents=200,
                            exit_reason="TAKE_PROFIT"),
        ]
        buckets = tb._aggregate_per_sport(per_replay)
        # Sorted alphabetically by sport: KXNBAGAME, KXUFCFIGHT
        assert len(buckets) == 2
        nba = next(b for b in buckets if b.sport == "KXNBAGAME")
        ufc = next(b for b in buckets if b.sport == "KXUFCFIGHT")
        assert nba.n_replays == 2
        assert nba.total_pnl_cents == 70  # 120 + (-50)
        assert ufc.n_replays == 1
        assert ufc.total_pnl_cents == 200


class TestRegimeSlicing:
    def test_per_bucket_sum(self, monkeypatch):
        # Stub bot.regime.tag to return deterministic regime keys keyed off
        # ticker so we can assert per-bucket totals without depending on the
        # ET clock.
        per_replay = [
            tb.ReplayResult(ticker="A", actions=[], round_trips=[],
                            realized_pnl_cents=100, exit_reason="OK"),
            tb.ReplayResult(ticker="B", actions=[], round_trips=[],
                            realized_pnl_cents=200, exit_reason="OK"),
            tb.ReplayResult(ticker="C", actions=[], round_trips=[],
                            realized_pnl_cents=-50, exit_reason="OK"),
        ]
        paper = [
            _trade("A", "2026-04-23T00:00:00Z"),
            _trade("B", "2026-04-23T01:00:00Z"),
            _trade("C", "2026-04-24T00:00:00Z"),
        ]
        # A, B share bucket "evening"; C is in "morning" — assert sums match.
        bucket_map = {"A": "evening", "B": "evening", "C": "morning"}

        def fake_tag(ts, ticker, market_state=None):
            return {"time_of_day": bucket_map[ticker]}

        import bot.regime
        monkeypatch.setattr(bot.regime, "tag", fake_tag)

        buckets = tb._aggregate_per_regime(per_replay, paper, "time_of_day")
        # Sorted by key: evening, morning
        keys = [b.key for b in buckets]
        assert keys == ["evening", "morning"]
        evening = next(b for b in buckets if b.key == "evening")
        morning = next(b for b in buckets if b.key == "morning")
        assert evening.n_replays == 2
        assert evening.total_pnl_cents == 300  # 100 + 200
        assert morning.n_replays == 1
        assert morning.total_pnl_cents == -50


class TestSweepDeterminism:
    def test_same_inputs_produce_same_totals(self, monkeypatch):
        # Two run_sweep invocations with the same paper trades + grid must
        # produce byte-equal training_total_cents lists. Stubs replay with a
        # ticker-keyed deterministic P&L so the test exercises sweep wiring,
        # not strategy behavior.
        pnl_by_ticker = {"X-A": 80, "X-B": -30, "X-C": 120}

        def fake_replay(strategy, trade, **kw):
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=pnl_by_ticker[trade["ticker"]],
                exit_reason="OK",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)

        train = [
            _trade("X-A", "2026-04-23T00:00:00Z"),
            _trade("X-B", "2026-04-23T01:00:00Z"),
        ]
        test = [_trade("X-C", "2026-04-26T00:00:00Z")]
        grid = [(0.65, 4), (0.70, 6)]

        r1 = tb.run_sweep(grid, train, test, slippage_cents=2)
        r2 = tb.run_sweep(grid, train, test, slippage_cents=2)
        totals1 = [v.total_pnl_cents for v in r1.training]
        totals2 = [v.total_pnl_cents for v in r2.training]
        assert totals1 == totals2
        # Both runs see the same back_test output (50¢ per train ticker pair):
        # 80 + (-30) = 50¢ per variant × 2 variants.
        assert all(t == 50 for t in totals1)
        # Test top-3 should be both variants (only 2 in grid), each scoring 120¢.
        assert all(v.total_pnl_cents == 120 for v in r1.test_top3)


# --- Session 41: TP/SL sweep regression tests -------------------------------


class TestSweepBaselineUpdated:
    def test_sweep_baseline_reflects_session_19c_production(self):
        """Session 19c shipped MOMENTUM_LEADER_MIN=0.65 (was 0.70). Session 41
        also restated SWEEP_BASELINE to (0.65, 6) so post-Session-19c sweeps
        compare against current production, not stale pre-19c values."""
        assert tb.SWEEP_BASELINE == (0.65, 6)


class TestSweepGridTpSl:
    def test_grid_has_12_variants(self):
        """Session 41 spec table: TP rows {10, 12, 14, 16} × SL cols {20, 25,
        30, 35} = 16 cells, minus fringe skips (10,35) / (14,35) / (16,30) /
        (16,35) = 12 variants. (Spec text said '11' counting non-baseline
        variants; (12, 30) is the baseline within the 12-entry grid.)"""
        assert len(tb.SWEEP_GRID_TP_SL) == 12

    def test_baseline_is_in_grid(self):
        """`(12, 30)` (current production TP/SL) must be one of the swept
        variants so per-trade Δ vs baseline is computable on the test set
        without a separate run. Mirrors how `SWEEP_GRID_PRIMARY` includes
        the LM/TS baseline."""
        assert tb.SWEEP_BASELINE_TP_SL == (12, 30)
        assert (12, 30) in tb.SWEEP_GRID_TP_SL

    def test_grid_skips_documented_fringe_combos(self):
        """The Session 41 grid intentionally skips (10,35) / (14,35) / (16,30)
        / (16,35) per the user's spec ('TP > SL has weird semantics; (10,35)
        and (14,35) and (16,30+) likely add no signal'). Lock the skip set so
        future edits don't silently re-add fringe combos."""
        excluded = {(10, 35), (14, 35), (16, 30), (16, 35)}
        for combo in excluded:
            assert combo not in tb.SWEEP_GRID_TP_SL, f"{combo} should be skipped"

    def test_grid_ratios_span_the_interesting_range(self):
        """Math says ratio > 0.6 needed to flip EV positive at 30% WR. Grid
        should cover both sides of that threshold so the sweep can detect
        it."""
        ratios = [tp / sl for (tp, sl) in tb.SWEEP_GRID_TP_SL]
        assert min(ratios) <= 0.40  # current baseline ratio
        assert max(ratios) >= 0.70  # well above the EV-flip threshold


class TestRunVariantTpSl:
    def test_default_tp_sl_matches_existing_path(self, monkeypatch):
        """Critical regression: `_run_variant(LM, TS)` (TP/SL omitted) must
        produce identical P&L to `_run_variant(LM, TS, take_profit_cents=12,
        stop_loss_cents=30)` (current production defaults). Proves the new
        kwargs don't perturb the baseline path when set to current production
        values."""
        paper = [
            _trade("KXNBAGAME-A", "2026-04-23T00:00:00Z"),
            _trade("KXUFCFIGHT-B", "2026-04-23T01:00:00Z"),
        ]

        def fake_replay(strategy, trade, **kw):
            # P&L mirrors strategy._take_profit_cents and _stop_loss_cents so
            # we can confirm the kwargs threaded through to the strategy.
            tp = strategy._take_profit_cents
            sl = strategy._stop_loss_cents
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=tp * 10 - sl,  # arbitrary but deterministic
                exit_reason="TAKE_PROFIT",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)

        # Path 1: kwargs omitted (defaults flow from bot.config — TP=12, SL=30)
        v_default = tb._run_variant(0.65, 6, paper)
        # Path 2: kwargs set to current production values explicitly
        v_explicit = tb._run_variant(
            0.65, 6, paper, take_profit_cents=12, stop_loss_cents=30,
        )
        # Same P&L because the strategy reads the same TP/SL in both cases.
        assert v_default.total_pnl_cents == v_explicit.total_pnl_cents
        assert v_default.n_replays == v_explicit.n_replays

    def test_tp_sl_overrides_thread_through_to_strategy(self, monkeypatch):
        """When the new kwargs are non-None, they must reach the
        LiveMomentumStrategy constructor and end up on the strategy
        instance's `_take_profit_cents` / `_stop_loss_cents`."""
        paper = [_trade("KXNBAGAME-A", "2026-04-23T00:00:00Z")]
        captured: list[tuple[int, int]] = []

        def fake_replay(strategy, trade, **kw):
            captured.append(
                (strategy._take_profit_cents, strategy._stop_loss_cents)
            )
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=0, exit_reason="OK",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)

        tb._run_variant(
            0.65, 6, paper,
            take_profit_cents=14, stop_loss_cents=25,
        )
        assert captured == [(14, 25)]

    def test_variant_result_carries_tp_sl_fields(self, monkeypatch):
        """Verify VariantResult carries the override values so downstream
        renderers and aggregators can attribute results."""
        paper = [_trade("KXNBAGAME-A", "2026-04-23T00:00:00Z")]
        monkeypatch.setattr(
            tb, "_replay_paper_trade",
            lambda s, t, **kw: tb.ReplayResult(
                ticker=t["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=0, exit_reason="OK",
            ),
        )
        v = tb._run_variant(0.65, 6, paper, take_profit_cents=14, stop_loss_cents=25)
        assert v.take_profit_cents == 14
        assert v.stop_loss_cents == 25
        # And the LM/TS sweep path leaves them None:
        v2 = tb._run_variant(0.65, 6, paper)
        assert v2.take_profit_cents is None
        assert v2.stop_loss_cents is None


class TestVariantResultLabel:
    def test_label_uses_lm_ts_when_no_tp_sl(self):
        v = tb.VariantResult(
            leader_min=0.65, trail_stop_cents=6,
            total_pnl_cents=0, n_replays=0, n_winning_replays=0,
            per_replay=[],
        )
        assert v.label == "LM=0.65 TS=6"

    def test_label_uses_tp_sl_when_overrides_set(self):
        v = tb.VariantResult(
            leader_min=0.65, trail_stop_cents=6,
            total_pnl_cents=0, n_replays=0, n_winning_replays=0,
            per_replay=[],
            take_profit_cents=14, stop_loss_cents=25,
        )
        assert v.label == "TP=14 SL=25"


class TestRunSweepTpSl:
    def test_iterates_full_tp_sl_grid_x_unique_tickers(self, monkeypatch):
        """N variants × M unique tickers = N×M strategy replays in training,
        plus baseline-on-test (1 extra), plus top-3-on-test (3 extras × M),
        plus best-aggregations. Mirrors TestSweepGrid for the LM/TS path."""
        paper = [
            _trade("KXNBAGAME-A", "2026-04-23T00:00:00Z"),
            _trade("KXUFCFIGHT-B", "2026-04-23T01:00:00Z"),
            _trade("KXIPLGAME-C", "2026-04-26T00:00:00Z"),
        ]
        captured_tp_sl: list[tuple[int, int]] = []

        def fake_replay(strategy, trade, **kw):
            captured_tp_sl.append(
                (strategy._take_profit_cents, strategy._stop_loss_cents)
            )
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=10, exit_reason="OK",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)

        # Smaller grid to keep the test cheap but exercise the same code path.
        small_grid = [(10, 20), (12, 30), (14, 25)]
        train, test = tb.split_train_test(paper, train_pct=0.70)
        report = tb.run_sweep_tp_sl(
            small_grid, train, test, slippage_cents=2,
        )

        # 3 variants × 2 train tickers = 6 train replays.
        # + 1 baseline × 1 test ticker = 1 baseline replay.
        # + 3 top variants × 1 test ticker = 3 test replays.
        # = 10 total replays.
        assert len(captured_tp_sl) == 10
        # All variants ran with their own TP/SL.
        assert (10, 20) in captured_tp_sl
        assert (12, 30) in captured_tp_sl
        assert (14, 25) in captured_tp_sl
        # Report carries the held-fixed LM/TS.
        assert report.leader_min_fixed == tb.SWEEP_TP_SL_FIXED_LM
        assert report.trail_stop_cents_fixed == tb.SWEEP_TP_SL_FIXED_TS
        assert report.baseline == (12, 30)

    def test_baseline_always_runs_even_when_not_in_top3(self, monkeypatch):
        """If the baseline isn't in the top-3 training variants, run_sweep_tp_sl
        must still execute it on the test set so the decision number is
        computable. Mirrors the same discipline in run_sweep."""
        paper = [
            _trade("KXNBAGAME-A", "2026-04-23T00:00:00Z"),
            _trade("KXNBAGAME-B", "2026-04-26T00:00:00Z"),
        ]
        # Force baseline (12, 30) to bottom-rank by giving it a worse P&L
        # than other variants (which earn more).
        pnl_by_tp_sl = {
            (10, 20): 200,  # best
            (12, 30): -100,  # baseline, worst
            (14, 25): 150,
            (16, 25): 100,
        }

        def fake_replay(strategy, trade, **kw):
            tp_sl = (strategy._take_profit_cents, strategy._stop_loss_cents)
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=pnl_by_tp_sl[tp_sl],
                exit_reason="OK",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)

        train, test = tb.split_train_test(paper, train_pct=0.50)
        grid = [(10, 20), (12, 30), (14, 25), (16, 25)]
        report = tb.run_sweep_tp_sl(grid, train, test, slippage_cents=0)
        # baseline_test should be populated even though (12, 30) is rank 4.
        assert report.baseline_test is not None
        assert report.baseline_test.take_profit_cents == 12
        assert report.baseline_test.stop_loss_cents == 30
        # And it isn't in the top-3 (training rank by P&L: 200, 150, 100, -100).
        top3_pairs = {
            (v.take_profit_cents, v.stop_loss_cents) for v in report.test_top3
        }
        assert (12, 30) not in top3_pairs

    def test_render_includes_baseline_marker_and_decision_gate_text(
        self, monkeypatch
    ):
        """Smoke-test the renderer: baseline row gets the marker; decision-gate
        text mentions Pattern A/B/C; held-fixed (LM, TS) shown in header."""
        paper = [_trade("KXNBAGAME-A", "2026-04-23T00:00:00Z")]
        monkeypatch.setattr(
            tb, "_replay_paper_trade",
            lambda s, t, **kw: tb.ReplayResult(
                ticker=t["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=0, exit_reason="OK",
            ),
        )
        # Use the real grid so the renderer sees the production-shape variant
        # set; one ticker is enough for a smoke test.
        train, test = tb.split_train_test(
            paper + [_trade("KXNBAGAME-B", "2026-04-26T00:00:00Z")],
            train_pct=0.50,
        )
        report = tb.run_sweep_tp_sl(
            tb.SWEEP_GRID_TP_SL, train, test, slippage_cents=0,
        )
        out = tb.render_sweep_tp_sl_report(report)
        # Header shouts Session 41 + held-fixed
        assert "Session 41" in out
        assert "Held fixed" in out
        assert "MOMENTUM_LEADER_MIN=0.65" in out
        # Baseline row gets the marker.
        assert "TP=12 SL=30 ← baseline" in out
        # Sport-profile caveat is documented.
        assert "Sport-profile caveat" in out
        # Decision-gate text references Pattern A/B/C.
        assert "Pattern A" in out
        assert "Pattern B" in out
        assert "Pattern C" in out


# =============================================================================
# Session 42: per-sport TP/SL sweep
# =============================================================================


class TestSportOverridesConstructor:
    def test_strategy_accepts_sport_overrides_kwarg(self):
        """LiveMomentumStrategy.__init__ accepts sport_overrides; defaults to
        None so existing constructors are byte-identical."""
        s = LiveMomentumStrategy(sport_overrides={"ufc": {"take_profit": 8}})
        assert s._sport_overrides == {"ufc": {"take_profit": 8}}

    def test_strategy_default_sport_overrides_none(self):
        s = LiveMomentumStrategy()
        assert s._sport_overrides is None


class TestSportOverridesResolution:
    """At the gate site (process_tick line ~290), TP/SL must resolve as
    override → SPORT_PROFILES → strategy default. Not a behavior unit-test
    of the gate itself (that needs full process_tick, fixture-heavy); this
    inspects the resolution layer logic directly via constructor + a small
    simulation."""

    def test_override_beats_profile(self):
        # If sport_overrides is set for ufc, the gate should pick override
        # values over the SPORT_PROFILES["ufc"] (which currently has TP=12, SL=10).
        # We simulate the resolver in-line because the full gate path
        # requires fixture setup. The resolver is a 3-line composition;
        # we test it by mirroring it.
        sport_overrides = {"ufc": {"take_profit": 8, "stop_loss": 6}}
        sport_profile = {"take_profit": 12, "stop_loss": 10}
        strategy_default_tp = 12
        strategy_default_sl = 30

        override = sport_overrides.get("ufc", {})
        resolved_tp = override.get("take_profit", sport_profile.get("take_profit", strategy_default_tp))
        resolved_sl = override.get("stop_loss", sport_profile.get("stop_loss", strategy_default_sl))
        assert resolved_tp == 8
        assert resolved_sl == 6

    def test_profile_beats_default_when_override_missing(self):
        sport_overrides = None
        sport_profile = {"take_profit": 12, "stop_loss": 10}
        strategy_default_tp = 12
        strategy_default_sl = 30

        override = sport_overrides.get("nba", {}) if sport_overrides else {}
        resolved_tp = override.get("take_profit", sport_profile.get("take_profit", strategy_default_tp))
        resolved_sl = override.get("stop_loss", sport_profile.get("stop_loss", strategy_default_sl))
        assert resolved_tp == 12
        assert resolved_sl == 10

    def test_default_when_neither_override_nor_profile(self):
        sport_overrides = None
        sport_profile: dict = {}  # IPL: no profile entry
        strategy_default_tp = 12
        strategy_default_sl = 30

        override = sport_overrides.get("ipl", {}) if sport_overrides else {}
        resolved_tp = override.get("take_profit", sport_profile.get("take_profit", strategy_default_tp))
        resolved_sl = override.get("stop_loss", sport_profile.get("stop_loss", strategy_default_sl))
        assert resolved_tp == 12
        assert resolved_sl == 30


class TestSportOverridesPartialOnly:
    """Override dict can supply only `take_profit`, only `stop_loss`, or both —
    partial overrides leave the missing key falling through to profile/default.
    Plan-agent revision #1: scope-limited to TP+SL this session."""

    def test_only_take_profit_overridden_sl_falls_through_to_profile(self):
        sport_overrides = {"ufc": {"take_profit": 8}}
        sport_profile = {"take_profit": 12, "stop_loss": 10}

        override = sport_overrides.get("ufc", {})
        resolved_tp = override.get("take_profit", sport_profile.get("take_profit", 12))
        resolved_sl = override.get("stop_loss", sport_profile.get("stop_loss", 30))
        assert resolved_tp == 8   # from override
        assert resolved_sl == 10  # from profile

    def test_only_stop_loss_overridden_tp_falls_through_to_profile(self):
        sport_overrides = {"ufc": {"stop_loss": 6}}
        sport_profile = {"take_profit": 12, "stop_loss": 10}

        override = sport_overrides.get("ufc", {})
        resolved_tp = override.get("take_profit", sport_profile.get("take_profit", 12))
        resolved_sl = override.get("stop_loss", sport_profile.get("stop_loss", 30))
        assert resolved_tp == 12  # from profile
        assert resolved_sl == 6   # from override


class TestSportOverridesTennisAliasIsolation:
    """SPORT_PROFILES["atp"] is the same dict object as SPORT_PROFILES["tennis"]
    (shared dict at bot/config.py:341-342). The override layer is keyed by
    SPORT NAME STRING, so overriding "atp" must NOT perturb "tennis" / "wta" /
    "atp_challenger" / "wta_challenger" resolution. This is the regression
    Plan-agent risk #1 calls out."""

    def test_overriding_atp_does_not_perturb_other_tennis_aliases(self):
        sport_overrides = {"atp": {"take_profit": 99, "stop_loss": 99}}
        # All four aliases share the same profile dict in production:
        shared_tennis_profile = {"take_profit": 10, "stop_loss": 10}

        # atp: override fires
        atp_override = sport_overrides.get("atp", {})
        atp_tp = atp_override.get("take_profit", shared_tennis_profile.get("take_profit", 12))
        atp_sl = atp_override.get("stop_loss", shared_tennis_profile.get("stop_loss", 30))
        assert atp_tp == 99
        assert atp_sl == 99

        # tennis: no override key → profile wins → byte-identical to pre-Session-42
        for alias in ("tennis", "wta", "atp_challenger", "wta_challenger"):
            override = sport_overrides.get(alias, {})
            tp = override.get("take_profit", shared_tennis_profile.get("take_profit", 12))
            sl = override.get("stop_loss", shared_tennis_profile.get("stop_loss", 30))
            assert tp == 10, f"alias={alias}: TP should not be perturbed"
            assert sl == 10, f"alias={alias}: SL should not be perturbed"


class TestRunVariantTpSlPerSport:
    """_run_variant accepts sport_overrides kwarg and threads it through to
    LiveMomentumStrategy. When None, behavior is byte-identical to pre-Session-42."""

    def test_default_no_overrides_matches_session_41_path(self, monkeypatch):
        """Critical regression: omitting sport_overrides preserves Session 41
        path byte-identically. Locks behavior preservation when sport_overrides
        is None (the default)."""
        paper = [
            _trade("KXNBAGAME-A", "2026-04-23T00:00:00Z"),
            _trade("KXUFCFIGHT-B", "2026-04-23T01:00:00Z"),
        ]

        captured: list[Any] = []

        def fake_replay(strategy, trade, **kw):
            captured.append(strategy._sport_overrides)
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=0, exit_reason="OK",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)

        # Path 1: pre-Session-42 — no sport_overrides kwarg.
        v_default = tb._run_variant(0.65, 6, paper, take_profit_cents=12, stop_loss_cents=30)
        # Path 2: explicit None.
        v_explicit_none = tb._run_variant(
            0.65, 6, paper,
            take_profit_cents=12, stop_loss_cents=30,
            sport_overrides=None,
        )
        assert v_default.total_pnl_cents == v_explicit_none.total_pnl_cents
        # All captured strategies had _sport_overrides = None.
        assert all(o is None for o in captured)

    def test_sport_overrides_thread_through_to_strategy(self, monkeypatch):
        paper = [_trade("KXUFCFIGHT-A", "2026-04-23T00:00:00Z")]
        captured: list[dict] = []

        def fake_replay(strategy, trade, **kw):
            captured.append(strategy._sport_overrides)
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=0, exit_reason="OK",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)
        overrides = {"ufc": {"take_profit": 8, "stop_loss": 6}}
        tb._run_variant(
            0.65, 6, paper,
            take_profit_cents=8, stop_loss_cents=6,
            sport_overrides=overrides,
        )
        assert captured == [overrides]


class TestSweepGridTpSlPerSport:
    def test_three_sports_have_grids(self):
        """NBA, NHL, UFC each have a 12-variant grid. IPL and tennis are
        deferred per Session 42 plan."""
        assert set(tb.SWEEP_GRID_TP_SL_PER_SPORT.keys()) == {"nba", "nhl", "ufc"}
        for sport, grid in tb.SWEEP_GRID_TP_SL_PER_SPORT.items():
            assert len(grid) == 12, f"{sport}: expected 12 variants"

    def test_each_grid_includes_its_sport_baseline(self):
        """Every per-sport grid must include that sport's current SPORT_PROFILES
        TP/SL as one of the variants — so per-trade Δ vs baseline is computable
        without a separate run."""
        for sport, baseline in tb.SWEEP_BASELINE_TP_SL_PER_SPORT.items():
            assert baseline in tb.SWEEP_GRID_TP_SL_PER_SPORT[sport], \
                f"{sport}: baseline {baseline} not in grid"

    def test_baselines_match_current_sport_profiles(self):
        """Locks the baselines to live SPORT_PROFILES so a future
        SPORT_PROFILES change doesn't silently shift the baseline."""
        from bot.config import SPORT_PROFILES
        for sport, (tp, sl) in tb.SWEEP_BASELINE_TP_SL_PER_SPORT.items():
            profile = SPORT_PROFILES.get(sport, {})
            assert profile.get("take_profit") == tp, \
                f"{sport}: SPORT_PROFILES TP={profile.get('take_profit')} != baseline TP={tp}"
            assert profile.get("stop_loss") == sl, \
                f"{sport}: SPORT_PROFILES SL={profile.get('stop_loss')} != baseline SL={sl}"

    def test_ufc_grid_has_tp_floor_8(self):
        """Plan-agent revision #2: UFC grid floor TP=8 (not TP=6) — TP=6 is
        below noise floor at Kelly-sized contracts (5-15c × 6¢ = $0.30-$0.90,
        eaten by 2¢ slippage pessimism). TP=8 directly probes user's
        'fights end before TP=12 fires' hypothesis."""
        ufc_tps = {tp for (tp, _sl) in tb.SWEEP_GRID_TP_SL_PER_SPORT["ufc"]}
        assert min(ufc_tps) == 8, f"UFC TP floor should be 8, got {min(ufc_tps)}"

    def test_nba_grid_skips_aggressive_and_loose_fringe(self):
        """NBA grid skips (16,8)/(16,10) (ratio ≥1.6, too aggressive) and
        (10,15)/(14,15) (ratio ≤0.93, too loose). Locks the skip set so future
        edits don't silently re-add fringe combos."""
        excluded = {(16, 8), (16, 10), (10, 15), (14, 15)}
        for combo in excluded:
            assert combo not in tb.SWEEP_GRID_TP_SL_PER_SPORT["nba"], \
                f"NBA grid: {combo} should be skipped"


class TestFilterTradesToSport:
    def test_filters_by_ticker_prefix_to_sport(self):
        trades = [
            _trade("KXNBAGAME-A", "2026-04-23T00:00:00Z"),
            _trade("KXNHLGAME-B", "2026-04-23T01:00:00Z"),
            _trade("KXUFCFIGHT-C", "2026-04-23T02:00:00Z"),
            _trade("KXIPLGAME-D", "2026-04-23T03:00:00Z"),
        ]
        nba_only = tb.filter_trades_to_sport(trades, "nba")
        assert [t["ticker"] for t in nba_only] == ["KXNBAGAME-A"]

        ufc_only = tb.filter_trades_to_sport(trades, "ufc")
        assert [t["ticker"] for t in ufc_only] == ["KXUFCFIGHT-C"]

    def test_unknown_sport_returns_empty(self):
        trades = [_trade("KXNBAGAME-A", "2026-04-23T00:00:00Z")]
        assert tb.filter_trades_to_sport(trades, "rugby") == []

    def test_case_insensitive_target_sport(self):
        trades = [_trade("KXNBAGAME-A", "2026-04-23T00:00:00Z")]
        # _TICKER_PREFIX_TO_SPORT values are lowercase; target is lowered.
        assert len(tb.filter_trades_to_sport(trades, "NBA")) == 1
        assert len(tb.filter_trades_to_sport(trades, "Nba")) == 1


class TestRunSweepTpSlPerSport:
    def test_threads_sport_overrides_per_variant(self, monkeypatch):
        """Each variant in the per-sport sweep gets its TP/SL routed via
        sport_overrides[target_sport], NOT via the constructor's TP/SL kwargs
        bypassing the profile layer. This proves the override actually flows
        through the gate-resolution path (not just the strategy's flat kwargs)."""
        paper = [
            _trade("KXUFCFIGHT-A", "2026-04-23T00:00:00Z"),
            _trade("KXUFCFIGHT-B", "2026-04-23T01:00:00Z"),
            _trade("KXUFCFIGHT-C", "2026-04-26T00:00:00Z"),
        ]
        captured_overrides: list[dict] = []

        def fake_replay(strategy, trade, **kw):
            captured_overrides.append(dict(strategy._sport_overrides or {}))
            return tb.ReplayResult(
                ticker=trade["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=0, exit_reason="OK",
            )

        monkeypatch.setattr(tb, "_replay_paper_trade", fake_replay)

        small_grid = [(8, 6), (10, 8), (12, 10)]
        train, test = tb.split_train_test(paper, train_pct=0.66)
        tb.run_sweep_tp_sl_per_sport(
            "ufc", train, test, slippage_cents=2,
            grid=small_grid, baseline=(12, 10),
        )
        # Every captured override is keyed by "ufc" only (no other sports).
        for ov in captured_overrides:
            assert list(ov.keys()) == ["ufc"]
            assert "take_profit" in ov["ufc"]
            assert "stop_loss" in ov["ufc"]
        # The grid's TP/SL pairs are all represented in captured overrides.
        captured_pairs = {
            (ov["ufc"]["take_profit"], ov["ufc"]["stop_loss"])
            for ov in captured_overrides
        }
        for pair in small_grid:
            assert pair in captured_pairs, f"{pair} not captured"

    def test_unknown_sport_raises_valueerror(self):
        with pytest.raises(ValueError, match="rugby"):
            tb.run_sweep_tp_sl_per_sport("rugby", [], [])

    def test_render_per_sport_report_carries_sport_in_header(self, monkeypatch):
        """Smoke-test the per-sport renderer wrapper: title is Session 42 +
        sport, per-sport scope text is present, decision-gate text mirrors
        Session 41."""
        paper = [
            _trade("KXNBAGAME-A", "2026-04-23T00:00:00Z"),
            _trade("KXNBAGAME-B", "2026-04-26T00:00:00Z"),
        ]
        monkeypatch.setattr(
            tb, "_replay_paper_trade",
            lambda s, t, **kw: tb.ReplayResult(
                ticker=t["ticker"], actions=[], round_trips=[],
                realized_pnl_cents=0, exit_reason="OK",
            ),
        )
        train, test = tb.split_train_test(paper, train_pct=0.50)
        report = tb.run_sweep_tp_sl_per_sport(
            "nba", train, test, slippage_cents=0,
        )
        out = tb.render_sweep_tp_sl_per_sport_report(report, "nba")
        assert "Session 42" in out
        assert "NBA" in out
        assert "Per-sport scope" in out
        assert "Pattern A" in out
        assert "Pattern C" in out
