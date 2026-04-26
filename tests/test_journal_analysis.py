"""Tests for tools/journal_analysis.py — Session 18 live_watcher behavior surfaces.

Pins the loader, exit-reason classifier, time-to-exit bucketing, bet→exit
pairing, watch-but-no-enter funnel, session_end aggregator, and markdown
rendering against hand-crafted journal records.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.journal_analysis as journal_module  # noqa: E402


def _ev(event: str, ticker: str, ts: str, **extra) -> dict:
    """Construct a journal event dict for tests."""
    base = {"event": event, "ticker": ticker, "timestamp": ts}
    base.update(extra)
    return base


@pytest.fixture
def tmp_journal_file(tmp_path, monkeypatch):
    """Repoint JOURNAL_FILE so each test uses an isolated tmp file."""
    f = tmp_path / "live_journal.json"
    monkeypatch.setattr(journal_module, "JOURNAL_FILE", f)
    return f


class TestLoader:
    def test_well_formed_list_loads(self, tmp_journal_file):
        recs = [
            {"event": "scan_found", "ticker": "KXNBAGAME-1", "timestamp": "2026-04-20T12:00:00+00:00"},
            {"event": "bet", "ticker": "KXNBAGAME-1", "timestamp": "2026-04-20T12:05:00+00:00"},
        ]
        tmp_journal_file.write_text(json.dumps(recs))
        out = journal_module.load_journal()
        assert len(out) == 2
        assert out[0]["event"] == "scan_found"

    def test_missing_file_returns_empty(self, tmp_journal_file):
        assert not tmp_journal_file.exists()
        assert journal_module.load_journal() == []

    def test_malformed_json_returns_empty(self, tmp_journal_file):
        tmp_journal_file.write_text("{not valid")
        assert journal_module.load_journal() == []

    def test_non_list_returns_empty(self, tmp_journal_file):
        tmp_journal_file.write_text(json.dumps({"events": []}))
        assert journal_module.load_journal() == []

    def test_drops_non_dict_and_event_less_entries(self, tmp_journal_file):
        tmp_journal_file.write_text(json.dumps([
            {"event": "bet", "ticker": "x"},
            "not-a-dict",
            {"ticker": "missing-event"},
            {"event": "", "ticker": "empty-event"},
        ]))
        out = journal_module.load_journal()
        assert len(out) == 1
        assert out[0]["event"] == "bet"


class TestSportInference:
    def test_uses_explicit_sport_field(self):
        rec = {"sport": "nba", "ticker": "KXATPMATCH-foo"}
        assert journal_module._record_sport(rec) == "nba"

    def test_falls_back_to_ticker_prefix(self):
        rec = {"ticker": "KXUFCFIGHT-26APR25SMITH-SMI"}
        assert journal_module._record_sport(rec) == "ufc"

    def test_unknown_returns_unknown_sport(self):
        assert journal_module._record_sport({"ticker": "KXWEIRD-1"}) == "unknown_sport"
        assert journal_module._record_sport({}) == "unknown_sport"


class TestParseTs:
    def test_parses_plus_offset(self):
        ts = journal_module._parse_ts("2026-04-20T12:00:00+00:00")
        assert ts is not None and ts.year == 2026

    def test_parses_z_suffix(self):
        ts = journal_module._parse_ts("2026-04-20T12:00:00Z")
        assert ts is not None

    def test_returns_none_on_garbage(self):
        assert journal_module._parse_ts("nope") is None
        assert journal_module._parse_ts(None) is None
        assert journal_module._parse_ts("") is None


class TestExitReasonClassifier:
    @pytest.mark.parametrize("reason,expected", [
        ("TAKE PROFIT: +12¢ (77¢ → 89¢)", "take_profit"),
        ("TRAILING STOP: peaked at 90¢, dropped 6¢ from peak", "trailing_stop"),
        ("STOP-LOSS: dropped 11¢ from entry (73¢ → 62¢)", "stop_loss"),
        ("DOLLAR STOP: $5.40 loss exceeds $5.00 cap (70¢ → 60¢ x6)", "dollar_stop"),
        ("UNDERWATER EXIT: 8¢ below entry for 5 ticks (69¢ → 61¢)", "underwater_exit"),
        ("NEAR-SETTLE: 95¢ (match nearly won, entry 77¢, gain +18¢)", "near_settle"),
        ("SETTLED at 100¢ (WIN)", "settled_win"),
        ("SETTLED at 1¢ (LOSS)", "settled_loss"),
        ("SCORE FLIP: team now trailing by 1 (55¢ → 53¢, -2¢)", "score_flip"),
        ("OPP RUN EXIT: opponent on scoring run (79¢ → 78¢, -1¢)", "opp_run_exit"),
    ])
    def test_classifies_by_prefix(self, reason, expected):
        assert journal_module._classify_exit_reason(reason) == expected

    def test_unrecognized_falls_through_to_other(self):
        assert journal_module._classify_exit_reason("weird new reason") == "other"

    def test_empty_or_none_returns_other(self):
        assert journal_module._classify_exit_reason("") == "other"
        assert journal_module._classify_exit_reason(None) == "other"


class TestPairBetsToExits:
    def test_simple_pair(self):
        recs = [
            _ev("bet", "T1", "2026-04-20T12:00:00+00:00", mode="momentum"),
            _ev("exit", "T1", "2026-04-20T12:05:00+00:00", reason="TAKE PROFIT", mode="momentum"),
        ]
        paired, open_bets = journal_module._pair_bets_to_exits(recs)
        assert len(paired) == 1
        assert len(open_bets) == 0
        _bet, _ex, hold = paired[0]
        assert hold == 300.0

    def test_unpaired_bet_is_open(self):
        recs = [_ev("bet", "T1", "2026-04-20T12:00:00+00:00", mode="momentum")]
        paired, open_bets = journal_module._pair_bets_to_exits(recs)
        assert paired == []
        assert len(open_bets) == 1

    def test_two_bets_one_exit_first_pairs(self):
        # Re-entry: bet at 12:00, bet at 12:10, exit at 12:05.
        # First bet pairs to the exit; second is open.
        recs = [
            _ev("bet", "T1", "2026-04-20T12:00:00+00:00", mode="momentum"),
            _ev("bet", "T1", "2026-04-20T12:10:00+00:00", mode="momentum"),
            _ev("exit", "T1", "2026-04-20T12:05:00+00:00", reason="TAKE PROFIT", mode="momentum"),
        ]
        paired, open_bets = journal_module._pair_bets_to_exits(recs)
        assert len(paired) == 1
        assert len(open_bets) == 1


class TestComputeTimeToExit:
    def test_bucket_boundaries(self):
        # 5 paired holds: 30s (<60s), 4min (60s-5min), 10min (5-15min),
        # 30min (15-60min), 70min (>60min)
        recs = []
        spans_min = [(0.5, "A"), (4, "B"), (10, "C"), (30, "D"), (70, "E")]
        for minutes, t in spans_min:
            secs = int(minutes * 60)
            hh, mm = divmod(secs, 3600)
            mm, ss = divmod(mm, 60)
            recs.append(_ev("bet", t, "2026-04-20T12:00:00+00:00", mode="momentum", sport="nba"))
            recs.append(_ev("exit", t, f"2026-04-20T{12 + hh:02d}:{mm:02d}:{ss:02d}+00:00",
                            reason="TAKE PROFIT", mode="momentum", sport="nba"))
        agg = journal_module.compute_time_to_exit(recs)
        assert agg[("nba", "momentum")]["buckets"] == {
            "<60s": 1, "60s-5min": 1, "5-15min": 1, "15-60min": 1, ">60min": 1,
        }
        assert agg[("nba", "momentum")]["n"] == 5

    def test_open_bets_not_counted_in_n(self):
        recs = [
            _ev("bet", "A", "2026-04-20T12:00:00+00:00", mode="momentum", sport="nba"),
            _ev("exit", "A", "2026-04-20T12:05:00+00:00", reason="TAKE PROFIT",
                mode="momentum", sport="nba"),
            _ev("bet", "B", "2026-04-20T12:00:00+00:00", mode="momentum", sport="nba"),
        ]
        agg = journal_module.compute_time_to_exit(recs)
        assert agg[("nba", "momentum")]["n"] == 1
        assert agg[("nba", "momentum")]["open"] == 1


class TestComputeExitReasons:
    def test_classifies_per_sport_mode(self):
        recs = [
            _ev("bet", "T1", "2026-04-20T12:00:00+00:00", mode="momentum", sport="nba"),
            _ev("exit", "T1", "2026-04-20T12:05:00+00:00",
                reason="TAKE PROFIT: +5¢", mode="momentum", sport="nba"),
            _ev("bet", "T2", "2026-04-20T12:00:00+00:00", mode="momentum", sport="nba"),
            _ev("exit", "T2", "2026-04-20T12:05:00+00:00",
                reason="STOP-LOSS: dropped 11¢", mode="momentum", sport="nba"),
            _ev("bet", "T3", "2026-04-20T12:00:00+00:00", mode="momentum", sport="ufc"),
            _ev("exit", "T3", "2026-04-20T12:05:00+00:00",
                reason="TAKE PROFIT: +12¢", mode="momentum", sport="ufc"),
        ]
        agg = journal_module.compute_exit_reasons(recs)
        assert agg[("nba", "momentum")]["counts"]["take_profit"] == 1
        assert agg[("nba", "momentum")]["counts"]["stop_loss"] == 1
        assert agg[("nba", "momentum")]["n"] == 2
        assert agg[("ufc", "momentum")]["n"] == 1
        assert agg[("ufc", "momentum")]["counts"]["take_profit"] == 1


class TestComputeWatchFunnel:
    def test_three_scans_one_bet_yields_two_skipped(self):
        recs = [
            _ev("scan_found", "T1", "2026-04-20T12:00:00+00:00", sport="nba"),
            _ev("scan_found", "T2", "2026-04-20T12:01:00+00:00", sport="nba"),
            _ev("scan_found", "T3", "2026-04-20T12:02:00+00:00", sport="ufc"),
            _ev("bet", "T1", "2026-04-20T12:05:00+00:00", mode="momentum", sport="nba"),
        ]
        agg = journal_module.compute_watch_funnel(recs)
        assert agg["nba"] == {"unique_scans": 2, "scan_with_bet": 1, "scan_no_bet": 1}
        assert agg["ufc"] == {"unique_scans": 1, "scan_with_bet": 0, "scan_no_bet": 1}

    def test_repeat_scan_collapses_to_unique_ticker(self):
        recs = [
            _ev("scan_found", "T1", "2026-04-20T12:00:00+00:00", sport="nba"),
            _ev("scan_found", "T1", "2026-04-20T12:01:00+00:00", sport="nba"),
            _ev("scan_found", "T1", "2026-04-20T12:02:00+00:00", sport="nba"),
        ]
        agg = journal_module.compute_watch_funnel(recs)
        assert agg["nba"]["unique_scans"] == 1
        assert agg["nba"]["scan_no_bet"] == 1


class TestComputeSessionEnds:
    def test_pnl_distribution_and_extremes(self):
        recs = [
            _ev("session_end", "T1", "2026-04-20T13:00:00+00:00",
                mode="momentum", sport="nba", total_pnl=0.50),
            _ev("session_end", "T2", "2026-04-20T13:00:00+00:00",
                mode="momentum", sport="nba", total_pnl=-0.30),
            _ev("session_end", "T3", "2026-04-20T13:00:00+00:00",
                mode="momentum", sport="nba", total_pnl=0.0),
        ]
        agg = journal_module.compute_session_ends(recs)
        bucket = agg[("nba", "momentum")]
        assert bucket["profit"] == 1
        assert bucket["loss"] == 1
        assert bucket["break_even"] == 1
        assert bucket["median_pnl"] == 0.0
        assert bucket["best_5"][0]["pnl"] == 0.50
        assert bucket["worst_5"][0]["pnl"] == -0.30


class TestSchemaTolerance:
    def test_missing_mode_classifies_as_unknown_mode(self):
        recs = [
            _ev("bet", "T1", "2026-04-20T12:00:00+00:00", sport="nba"),
            _ev("exit", "T1", "2026-04-20T12:05:00+00:00",
                reason="TAKE PROFIT", sport="nba"),
        ]
        agg = journal_module.compute_time_to_exit(recs)
        assert ("nba", "unknown_mode") in agg

    def test_missing_sport_falls_back_to_ticker_inference(self):
        # Pre-Apr-16 record without sport
        recs = [
            _ev("bet", "KXUFCFIGHT-26APR09FOO-FOO", "2026-04-20T12:00:00+00:00", mode="momentum"),
            _ev("exit", "KXUFCFIGHT-26APR09FOO-FOO", "2026-04-20T12:05:00+00:00",
                reason="TAKE PROFIT", mode="momentum"),
        ]
        agg = journal_module.compute_time_to_exit(recs)
        assert ("ufc", "momentum") in agg


class TestRenderMarkdown:
    def test_empty_records_renders_placeholder(self):
        md = journal_module.render_markdown([])
        assert "# Journal Analysis" in md
        assert "No journal records" in md

    def test_full_report_includes_required_sections(self):
        recs = [
            _ev("scan_found", "KXNBAGAME-26APR20-A", "2026-04-20T12:00:00+00:00",
                sport="nba", price=70, volume=10000),
            _ev("bet", "KXNBAGAME-26APR20-A", "2026-04-20T12:01:00+00:00",
                side="yes", price_cents=70, mode="momentum", sport="nba",
                contracts=1, reason="dip buy"),
            _ev("exit", "KXNBAGAME-26APR20-A", "2026-04-20T12:06:00+00:00",
                reason="TAKE PROFIT: +5¢", side="yes", entry_price=70, exit_value=75,
                peak_value=75, pnl=0.05, mode="momentum", sport="nba"),
            _ev("session_end", "KXNBAGAME-26APR20-A", "2026-04-20T13:00:00+00:00",
                mode="momentum", sport="nba", total_pnl=0.05, duration_min=60.0,
                ticks=300, bets_placed=1, exits=1, price_history=[70, 72, 75]),
        ]
        md = journal_module.render_markdown(recs)
        assert "Findings" in md
        assert "Limitations" in md
        assert "Time-to-Exit" in md
        assert "Exit Reason" in md
        assert "Watch" in md
        assert "Session End" in md.lower() or "session" in md.lower()

    def test_findings_empty_renders_placeholder(self, monkeypatch):
        monkeypatch.setattr(journal_module, "FINDINGS", [])
        md = journal_module.render_markdown([
            _ev("bet", "X", "2026-04-20T12:00:00+00:00", mode="momentum"),
        ])
        assert "No findings recorded yet" in md

    def test_findings_populated_renders_bullets(self, monkeypatch):
        monkeypatch.setattr(journal_module, "FINDINGS", [
            "UFC stop_loss never fires (n=15) — consider tightening.",
        ])
        md = journal_module.render_markdown([
            _ev("bet", "X", "2026-04-20T12:00:00+00:00", mode="momentum"),
        ])
        assert "UFC stop_loss" in md
