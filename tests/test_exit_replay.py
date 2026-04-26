"""Tests for tools/exit_replay.py — Session 18.5 exit-logic replay.

Pins the bet→exit pairing, tick stream loading + slicing, simulate_exit's
mirror of live_watcher's priority order, and the sweep+render aggregation
against hand-crafted records.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.exit_replay as exit_replay  # noqa: E402


def _bet(ticker: str, ts: str, **extra) -> dict:
    base = {
        "event": "bet",
        "ticker": ticker,
        "timestamp": ts,
        "side": "yes",
        "contracts": 10,
        "price_cents": 50,
        "mode": "momentum",
        "sport": "nba",
    }
    base.update(extra)
    return base


def _exit(ticker: str, ts: str, reason: str, **extra) -> dict:
    base = {
        "event": "exit",
        "ticker": ticker,
        "timestamp": ts,
        "reason": reason,
        "side": "yes",
        "sport": "nba",
        "entry_price": 50,
        "exit_value": 62,
        "pnl": 1.20,
        "mode": "momentum",
    }
    base.update(extra)
    return base


@pytest.fixture
def tmp_journal(tmp_path, monkeypatch):
    f = tmp_path / "live_journal.json"
    monkeypatch.setattr(exit_replay, "JOURNAL_FILE", f)
    return f


class TestLoadBetExitPairs:
    def test_simple_pair(self, tmp_journal):
        events = [
            _bet("KXNBAGAME-X", "2026-04-20T12:00:00+00:00"),
            _exit("KXNBAGAME-X", "2026-04-20T12:10:00+00:00", "TAKE PROFIT: +12¢"),
        ]
        tmp_journal.write_text(json.dumps(events))
        pairs, counts = exit_replay.load_bet_exit_pairs()
        assert counts["paired"] == 1
        assert counts["open_bets"] == 0
        assert counts["settled_excluded"] == 0
        assert len(pairs) == 1
        bet, ex = pairs[0]
        assert bet["ticker"] == "KXNBAGAME-X"
        assert ex["reason"].startswith("TAKE PROFIT")

    def test_re_entry_pairs_correctly(self, tmp_journal):
        # Two bets, two exits on the same ticker — chronological pairing.
        events = [
            _bet("KX-T", "2026-04-20T12:00:00+00:00"),
            _exit("KX-T", "2026-04-20T12:10:00+00:00", "TAKE PROFIT: +12¢"),
            _bet("KX-T", "2026-04-20T13:00:00+00:00"),
            _exit("KX-T", "2026-04-20T13:15:00+00:00", "STOP-LOSS: dropped 30¢"),
        ]
        tmp_journal.write_text(json.dumps(events))
        pairs, counts = exit_replay.load_bet_exit_pairs()
        assert counts["paired"] == 2
        assert pairs[0][1]["reason"].startswith("TAKE PROFIT")
        assert pairs[1][1]["reason"].startswith("STOP-LOSS")

    def test_open_bet_excluded(self, tmp_journal):
        # One bet, no matching exit → still-open, excluded.
        events = [_bet("KX-OPEN", "2026-04-20T12:00:00+00:00")]
        tmp_journal.write_text(json.dumps(events))
        pairs, counts = exit_replay.load_bet_exit_pairs()
        assert counts["paired"] == 0
        assert counts["open_bets"] == 1
        assert pairs == []

    def test_settled_exits_excluded(self, tmp_journal):
        events = [
            _bet("KX-A", "2026-04-20T12:00:00+00:00"),
            _exit("KX-A", "2026-04-20T20:00:00+00:00", "SETTLED at 100¢ (WIN)"),
            _bet("KX-B", "2026-04-20T12:00:00+00:00"),
            _exit("KX-B", "2026-04-20T20:00:00+00:00", "TAKE PROFIT: +12¢"),
        ]
        tmp_journal.write_text(json.dumps(events))
        pairs, counts = exit_replay.load_bet_exit_pairs()
        assert counts["settled_excluded"] == 1
        # KX-A's bet becomes "open" because its only exit was SETTLED-excluded
        assert counts["open_bets"] == 1
        assert counts["paired"] == 1
        assert pairs[0][0]["ticker"] == "KX-B"

    def test_missing_file_returns_empty(self, tmp_journal):
        # tmp_journal doesn't exist on disk yet
        assert not tmp_journal.exists()
        pairs, counts = exit_replay.load_bet_exit_pairs()
        assert pairs == []
        assert counts["total_bets"] == 0

    def test_malformed_json_returns_empty(self, tmp_journal):
        tmp_journal.write_text("{not valid")
        pairs, counts = exit_replay.load_bet_exit_pairs()
        assert pairs == []
        assert counts["total_bets"] == 0


def _tick(ticker: str, ts: str, **extra) -> dict:
    base = {"ticker": ticker, "ts": ts, "price": 50, "bid": 49}
    base.update(extra)
    return base


@pytest.fixture
def tmp_ticks(tmp_path, monkeypatch):
    f = tmp_path / "live_ticks.jsonl"
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(exit_replay, "TICKS_FILE", f)
    monkeypatch.setattr(exit_replay, "ARCHIVE_DIR", archive)
    return f, archive


class TestLoadTickIndex:
    def test_loads_current_file(self, tmp_ticks):
        ticks_file, _ = tmp_ticks
        ticks_file.write_text(
            "\n".join(
                json.dumps(t)
                for t in [
                    _tick("KX-A", "2026-04-20T12:00:00+00:00"),
                    _tick("KX-A", "2026-04-20T12:00:10+00:00"),
                    _tick("KX-B", "2026-04-20T12:00:00+00:00"),
                ]
            )
        )
        idx = exit_replay.load_tick_index()
        assert set(idx.keys()) == {"KX-A", "KX-B"}
        assert len(idx["KX-A"]) == 2
        assert idx["KX-A"][0]["ts"] < idx["KX-A"][1]["ts"]

    def test_skips_malformed_lines(self, tmp_ticks):
        ticks_file, _ = tmp_ticks
        ticks_file.write_text(
            "\n".join(
                [
                    json.dumps(_tick("KX-A", "2026-04-20T12:00:00+00:00")),
                    "{not json",
                    json.dumps(_tick("KX-A", "2026-04-20T12:00:10+00:00")),
                    "",
                ]
            )
        )
        idx = exit_replay.load_tick_index()
        assert len(idx["KX-A"]) == 2

    def test_loads_gzip_archive(self, tmp_ticks):
        import gzip as _gz
        _, archive = tmp_ticks
        gz = archive / "live_ticks-2026-04-20.jsonl.gz"
        with _gz.open(gz, "wt") as f:
            f.write(json.dumps(_tick("KX-A", "2026-04-20T11:00:00+00:00")) + "\n")
            f.write(json.dumps(_tick("KX-B", "2026-04-20T11:00:00+00:00")) + "\n")
        idx = exit_replay.load_tick_index()
        assert "KX-A" in idx and "KX-B" in idx


class TestSliceTicks:
    def test_slice_inclusive_boundaries(self):
        ticks = [
            {"ts": "2026-04-20T12:00:00+00:00"},
            {"ts": "2026-04-20T12:00:10+00:00"},
            {"ts": "2026-04-20T12:00:20+00:00"},
            {"ts": "2026-04-20T12:00:30+00:00"},
        ]
        # Window includes both endpoints
        out = exit_replay.slice_ticks(
            ticks, "2026-04-20T12:00:10+00:00", "2026-04-20T12:00:20+00:00"
        )
        assert len(out) == 2
        assert out[0]["ts"].endswith("12:00:10+00:00")
        assert out[1]["ts"].endswith("12:00:20+00:00")

    def test_empty_when_window_outside(self):
        ticks = [{"ts": "2026-04-20T12:00:00+00:00"}]
        out = exit_replay.slice_ticks(
            ticks, "2026-04-20T13:00:00+00:00", "2026-04-20T13:00:01+00:00"
        )
        assert out == []

    def test_empty_when_no_ticks(self):
        assert exit_replay.slice_ticks([], "a", "b") == []


class TestAttachTicks:
    def test_coverage_buckets(self, tmp_journal):
        # 3 pairs, varying tick counts: 0, 7 (relaxed_only), 15 (strict)
        bet1 = _bet("KX-EMPTY", "2026-04-20T12:00:00+00:00")
        ex1 = _exit("KX-EMPTY", "2026-04-20T12:10:00+00:00", "TAKE PROFIT: +12¢")
        bet2 = _bet("KX-RELAXED", "2026-04-20T12:00:00+00:00")
        ex2 = _exit("KX-RELAXED", "2026-04-20T12:10:00+00:00", "TAKE PROFIT: +12¢")
        bet3 = _bet("KX-STRICT", "2026-04-20T12:00:00+00:00")
        ex3 = _exit("KX-STRICT", "2026-04-20T12:10:00+00:00", "TAKE PROFIT: +12¢")
        pairs = [(bet1, ex1), (bet2, ex2), (bet3, ex3)]
        # Build tick index manually
        tick_index = {
            "KX-RELAXED": [
                {"ticker": "KX-RELAXED", "ts": f"2026-04-20T12:0{i//10}:{i%10}0+00:00"}
                for i in range(7)
            ],
            "KX-STRICT": [
                {"ticker": "KX-STRICT", "ts": f"2026-04-20T12:0{i//10}:{i%10}0+00:00"}
                for i in range(15)
            ],
        }
        out, cov = exit_replay.attach_ticks(pairs, tick_index)
        assert cov == {"no_ticks": 1, "thin_coverage": 0, "relaxed_only": 1, "strict": 1}
        # And the ticks list reflects the slice
        assert len(out[0].ticks) == 0
        assert len(out[1].ticks) == 7
        assert len(out[2].ticks) == 15


def _make_pair(side: str, entry: int, contracts: int, sport: str, tick_specs: list[dict]) -> exit_replay.BetExitPair:
    """Hand-build a BetExitPair for simulator tests. tick_specs is a list of dicts
    that get merged into a baseline tick (ticker/ts/leader). Each spec must include
    `bid` (for YES sims) or `price` (for NO sims) and optionally score_diff etc."""
    bet = _bet("KX-SIM", "2026-04-20T12:00:00+00:00", side=side, price_cents=entry, contracts=contracts, sport=sport)
    ex = _exit("KX-SIM", "2026-04-20T13:00:00+00:00", "TAKE PROFIT: irrelevant", side=side, sport=sport, entry_price=entry)
    ticks = []
    for i, spec in enumerate(tick_specs):
        t = {"ticker": "KX-SIM", "ts": f"2026-04-20T12:{i:02d}:00+00:00", "leader": True}
        t.update(spec)
        ticks.append(t)
    return exit_replay.BetExitPair(bet=bet, exit=ex, ticks=ticks)


class TestSimulateExit:
    def test_take_profit_fires(self):
        # YES bet at 50, ticks ramp: bid 52, 55, 62 → TP at 62 (gain=12, default tp=12 NBA)
        pair = _make_pair("yes", 50, 10, "nba", [
            {"bid": 52},
            {"bid": 55},
            {"bid": 62},
        ])
        result = exit_replay.simulate_exit(pair)
        assert result.exit_reason == "TAKE_PROFIT"
        assert result.exit_price_cents == 62
        assert result.realized_pnl_cents == 12
        assert result.ticks_to_exit == 3

    def test_trailing_stop_fires_with_tight_trail(self):
        # YES bet at 50, ticks: 53, 58, 56, 52 → with trail_stop=3, drops 6 from peak=58 to 52, gain=2 → trailing
        pair = _make_pair("yes", 50, 10, "nba", [
            {"bid": 53},
            {"bid": 58},
            {"bid": 56},  # drop 2 from peak — not yet
            {"bid": 52},  # drop 6 from peak — fires
        ])
        params = {**exit_replay.DEFAULT_PARAMS, "trail_stop": 3, "apply_trail_globally": True}
        result = exit_replay.simulate_exit(pair, params)
        # Drop 5 from peak=58 hits trail=3 at tick 3 (bid=56, drop=2 — wait recompute)
        # Recompute: peak at 58. tick3 bid=56 drop=2 < 3. tick4 bid=52 drop=6 >= 3 → fires here.
        # gain at tick4 = 52-50 = 2 > 0 → trailing fires.
        assert result.exit_reason == "TRAILING_STOP"
        assert result.exit_price_cents == 52
        assert result.realized_pnl_cents == 2
        assert result.peak_value_cents == 58

    def test_stop_loss_fires(self):
        # NBA sport_sl = 10 (per Apr 14 audit, all sports tightened to 10).
        # YES bet at 50, ticks drop monotonically — SL fires at 40 (drop=10).
        pair = _make_pair("yes", 50, 10, "nba", [
            {"bid": 48},
            {"bid": 45},
            {"bid": 42},
            {"bid": 40},
        ])
        result = exit_replay.simulate_exit(pair)
        assert result.exit_reason == "STOP_LOSS"
        assert result.exit_price_cents == 40
        assert result.realized_pnl_cents == -10

    def test_no_side_sign_convention(self):
        # NO bet at entry 40, ticks have price (yes_ask) 70 → current_value = 100-70 = 30 → loss of 10
        # SL = 10 → SL fires.
        pair = _make_pair("no", 40, 10, "nba", [
            {"price": 65},  # current = 100-65 = 35, drop = 5, no SL yet
            {"price": 70},  # current = 30, drop = 10 → SL
        ])
        result = exit_replay.simulate_exit(pair)
        assert result.exit_reason == "STOP_LOSS"
        # exit_price stored is current_value (30 for NO at price=70)
        assert result.exit_price_cents == 30
        assert result.realized_pnl_cents == -10

    def test_sweep_smoking_gun(self):
        # Same tick stream, two trail values produce DIFFERENT exits.
        # YES bet at 50, ticks: 55, 60, 57, 53.
        # Peak hits 60. After: tick3 drop=3, tick4 drop=7.
        # trail=8 → never fires → stop_loss fires when drop >= 10? No, last bid=53 → drop=0 from entry.
        #   Actually neither TP (max gain=10 < 12=tp) nor SL (max drop=0) → NO_EXIT.
        # trail=3 → fires at tick3 (drop=3 from peak=60, gain=7 > 0) → TRAILING_STOP at 57.
        ticks = [{"bid": 55}, {"bid": 60}, {"bid": 57}, {"bid": 53}]
        pair = _make_pair("yes", 50, 10, "nba", ticks)
        params_wide = {**exit_replay.DEFAULT_PARAMS, "trail_stop": 8, "apply_trail_globally": True}
        params_tight = {**exit_replay.DEFAULT_PARAMS, "trail_stop": 3, "apply_trail_globally": True}
        wide = exit_replay.simulate_exit(pair, params_wide)
        tight = exit_replay.simulate_exit(pair, params_tight)
        assert wide.exit_reason == "NO_EXIT"
        assert tight.exit_reason == "TRAILING_STOP"
        assert tight.exit_price_cents == 57

    def test_no_exit_fall_through(self):
        # YES bet at 50. Bids stay flat at 51. No exit fires.
        pair = _make_pair("yes", 50, 10, "nba", [{"bid": 51} for _ in range(5)])
        result = exit_replay.simulate_exit(pair)
        assert result.exit_reason == "NO_EXIT"
        assert result.exit_price_cents == 51
        assert result.realized_pnl_cents == 1
        assert result.ticks_to_exit == 5

    def test_score_flip_fires(self):
        # YES bet at 50. Bids stay favorable, but score_diff/momentum/lead_trend turn negative.
        pair = _make_pair("yes", 50, 10, "nba", [
            {"bid": 51, "score_diff": 5, "momentum": 0.2, "lead_trend": 0.1, "leader": True},
            {"bid": 52, "score_diff": -3, "momentum": -0.5, "lead_trend": -0.3, "leader": True},
        ])
        result = exit_replay.simulate_exit(pair)
        assert result.exit_reason == "SCORE_FLIP"
        assert result.exit_price_cents == 52

    def test_dollar_stop_fires_on_large_position(self):
        # 100 contracts YES at 50. Drop to 45 = -5¢ × 100 = $5.00 loss = DOLLAR_STOP cap.
        # SL is 10¢ for NBA — wouldn't fire at -5¢. DOLLAR STOP fires first.
        pair = _make_pair("yes", 50, 100, "nba", [
            {"bid": 48},  # -2 × 100 = -$2
            {"bid": 45},  # -5 × 100 = -$5 → DOLLAR_STOP
        ])
        result = exit_replay.simulate_exit(pair)
        assert result.exit_reason == "DOLLAR_STOP"
        assert result.exit_price_cents == 45


class TestSweepAndRender:
    def test_sweep_aggregates_correctly(self):
        # 2 pairs, 2 variants → totals match hand-computation.
        # Pair 1: YES@50, ramps to 60 then 53. trail=3 fires TRAILING at 57? Let's compute:
        #   tick1 bid=55 peak=55 gain=5
        #   tick2 bid=60 peak=60 gain=10
        #   tick3 bid=57 drop=3 from peak=60 gain=7 → trail=3 fires! TRAILING at 57, pnl=7
        # Pair 2: YES@50, ramps to 62 → TP fires at gain=12, pnl=12 (TP = 12 default)
        pairs = [
            _make_pair("yes", 50, 10, "nba", [{"bid": 55}, {"bid": 60}, {"bid": 57}, {"bid": 53}]),
            _make_pair("yes", 50, 10, "nba", [{"bid": 55}, {"bid": 58}, {"bid": 62}]),
        ]
        results = exit_replay.sweep(pairs, trail_stops=[3, 8])
        assert len(results) == 2
        # trail=3 → both pairs: pair1 TRAILING at 57 (pnl=7), pair2 TP at 62 (pnl=12). total=19.
        r3 = results[0]
        assert r3.trail_stop == 3
        assert r3.n_pairs == 2
        assert r3.total_pnl_cents == 19
        assert r3.win_count == 2
        # trail=8 → pair1: drops never reach 8 from peak (max drop=7) → no trailing.
        #   tick4 bid=53 → SL at gain=-7? SL=10 default for NBA — drop=-3 < 10. No SL fires.
        #   So pair1 falls through to NO_EXIT at last bid=53, pnl=3. Pair2 TP at 62, pnl=12. total=15.
        r8 = results[1]
        assert r8.trail_stop == 8
        assert r8.total_pnl_cents == 15
        # And the exit reason mix differs
        assert r3.reason_breakdown["TRAILING_STOP"] == 1
        assert r8.reason_breakdown["NO_EXIT"] == 1

    def test_render_includes_table_headers_and_findings(self):
        excl = exit_replay.ExclusionSummary(
            total_bets=100, total_exits=90, paired=80, open_bets=20,
            settled_excluded=10, no_ticks=5, thin_coverage=10,
            strict_n=55, relaxed_n=65,
        )
        # Empty results lists — should still produce the structural headers.
        out = exit_replay.render_markdown([], [], excl)
        assert "MOMENTUM_DQS_TRAIL_STOP sweep" in out
        assert "strict cohort" in out
        assert "relaxed cohort" in out
        assert "Findings" in out
        # Cohort summary line present
        assert "strict (≥10 ticks) = 55" in out
        assert "relaxed (≥5 ticks) = 65" in out

    def test_render_with_real_results(self):
        pairs = [
            _make_pair("yes", 50, 10, "nba", [{"bid": 55}, {"bid": 62}]),  # TP
        ]
        results = exit_replay.sweep(pairs, trail_stops=[3, 6])
        excl = exit_replay.ExclusionSummary(
            total_bets=1, total_exits=1, paired=1, open_bets=0,
            settled_excluded=0, no_ticks=0, thin_coverage=0,
            strict_n=0, relaxed_n=1,
        )
        out = exit_replay.render_markdown(results, results, excl)
        # Variant rows present in markdown — table cells use "3¢" / "6¢" style
        assert "3¢" in out
        assert "6¢" in out
