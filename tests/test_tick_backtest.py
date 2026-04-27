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
