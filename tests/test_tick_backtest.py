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
