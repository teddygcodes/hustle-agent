"""Tests for tools/live_momentum_buckets.py (Session 30 Stage 2).

Covers band assignment, low-confidence marker, interaction-grid rendering,
graceful handling of None dimensions / empty datasets, findings rendering on
thin samples, and the game-phase derivation helper.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.live_momentum_buckets as bk  # noqa: E402


def _row(**kwargs) -> dict:
    """Construct a dataset row with sane defaults; override via kwargs."""
    base = {
        "decision_id": "X-1",
        "decision_ts": "2026-04-28T12:00:00+00:00",
        "ticker": "KXNBAGAME-A",
        "match": "Game",
        "sport": "nba",
        "accept": False,
        "skip_reason": "low_volume",
        "leader_side": "yes",
        "leader_price": 70,
        "spread_cents": 1,
        "wp": 0.6,
        "wp_edge": 0.04,
        "momentum": 0.1,
        "lead_trend": 0.05,
        "dip": 2,
        "dqs": None,
        "period": 2,
        "score_diff": 5,
        "completion": 0.5,
        "elapsed": 1800,
        "volatility": "normal",
        "leader": True,
        "opp_leader": False,
        "recent_high": 72,
        "opp_recent_high": 32,
        "regime_time_of_day": "evening",
        "regime_day_of_week": "tue",
        "regime_sport_phase": "playoffs",
        "regime_event_horizon_hr": "<2h",
        "fwd_return_30s_cents": 1.0,
        "fwd_return_60s_cents": 2.0,
        "fwd_return_120s_cents": 3.0,
        "mfe_in_120s_window_cents": 5.0,
        "mae_in_120s_window_cents": 1.0,
        "outcome_clv_cents": None,
        "outcome_clv_relative": None,
        "outcome_realized_pnl": None,
        "outcome_target_yes_price_cents": None,
        "outcome_settlement": None,
    }
    base.update(kwargs)
    return base


class TestBandAssignment:
    def test_each_band_edge_lands_in_correct_bucket(self):
        rows = [
            _row(leader_price=59),  # <60
            _row(leader_price=60),  # 60-70
            _row(leader_price=70),  # 70-80
            _row(leader_price=80),  # 80-90
            _row(leader_price=90),  # >=90
            _row(leader_price=99),  # >=90
        ]
        buckets = bk.bucket_by(rows, "leader_price", bk.LEADER_PRICE_BANDS)
        assert len(buckets["<60"]) == 1
        assert len(buckets["60-70"]) == 1
        assert len(buckets["70-80"]) == 1
        assert len(buckets["80-90"]) == 1
        assert len(buckets[">=90"]) == 2

    def test_dip_band_edges(self):
        rows = [_row(dip=v) for v in (0, 2, 4, 6, 8, 10, 11)]
        buckets = bk.bucket_by(rows, "dip", bk.DIP_BANDS)
        assert len(buckets["0-2"]) == 1
        assert len(buckets["2-4"]) == 1
        assert len(buckets["4-6"]) == 1
        assert len(buckets["6-8"]) == 1
        assert len(buckets["8-10"]) == 1
        assert len(buckets[">10"]) == 2

    def test_wp_edge_negative_band(self):
        rows = [_row(wp_edge=-0.10), _row(wp_edge=-0.03), _row(wp_edge=0.06), _row(wp_edge=0.12)]
        buckets = bk.bucket_by(rows, "wp_edge", bk.WP_EDGE_BANDS)
        assert len(buckets["<-0.05"]) == 1
        assert len(buckets["-0.05-0"]) == 1
        assert len(buckets["0.05-0.10"]) == 1
        assert len(buckets[">0.10"]) == 1


class TestLowConfidenceMarker:
    def test_n_lt_5_marked_low_confidence(self):
        rows = [_row(sport="nba") for _ in range(3)] + [_row(sport="nhl") for _ in range(7)]
        buckets = bk.bucket_by(rows, "sport", None)
        out = bk.render_bucket_table(buckets, "sport", horizon_secs=120)
        # nba bucket has n=3, should show '(low-confidence)'
        nba_line = [ln for ln in out.splitlines() if ln.startswith("| nba")][0]
        assert "(low-confidence)" in nba_line
        nhl_line = [ln for ln in out.splitlines() if ln.startswith("| nhl")][0]
        assert "(low-confidence)" not in nhl_line


class TestInteractionTable:
    def test_interaction_table_renders(self):
        rows = [
            _row(sport="nba", leader_price=65, fwd_return_120s_cents=2.0),
            _row(sport="nba", leader_price=85, fwd_return_120s_cents=4.0),
            _row(sport="nhl", leader_price=85, fwd_return_120s_cents=1.0),
        ]
        out = bk.render_interaction_table(
            rows, "sport", None, "leader_price", bk.LEADER_PRICE_BANDS,
            "sport", "leader_price",
        )
        assert "### Interaction: sport x leader_price" in out
        # Header row mentions both axes
        assert "60-70" in out or "80-90" in out
        # Each cell shows n=count for occupied cells
        assert "(n=1)" in out
        # Empty cells render as em-dash
        assert "—" in out
        # Low-confidence marker (asterisk) present given n=1 cells
        assert "*" in out


class TestMissingDimension:
    def test_missing_dqs_doesnt_crash(self):
        rows = [_row(dqs=None) for _ in range(3)] + [_row(dqs=0.55)]
        buckets = bk.bucket_by(rows, "dqs", bk.DQS_BANDS)
        # None values should NOT match any band, so they go to "Other"
        assert sum(len(v) for v in buckets.values()) >= 1
        # The 0.55 row lands in 0.5-0.6
        assert len(buckets.get("0.5-0.6", [])) == 1

    def test_missing_dimension_rendering_doesnt_crash(self):
        rows = [_row(dqs=None) for _ in range(3)]
        buckets = bk.bucket_by(rows, "dqs", bk.DQS_BANDS)
        out = bk.render_bucket_table(buckets, "dqs", horizon_secs=120)
        assert "Bucket: dqs" in out


class TestEmptyDataset:
    def test_handles_empty_dataset(self, tmp_path):
        p = tmp_path / "empty.csv"
        with open(p, "w") as f:
            cols = [
                "decision_id", "decision_ts", "ticker", "match", "sport", "accept",
                "skip_reason", "leader_price", "fwd_return_120s_cents",
                "mfe_in_120s_window_cents", "mae_in_120s_window_cents",
                "outcome_clv_cents",
            ]
            csv.DictWriter(f, fieldnames=cols).writeheader()
        rows = bk.load_dataset(str(p))
        assert rows == []
        out = bk.render_report(rows)
        assert "No data" in out


class TestThinSampleFindings:
    def test_findings_section_renders_even_when_thin(self):
        rows = [_row(fwd_return_120s_cents=2.0), _row(fwd_return_120s_cents=2.5)]
        buckets = {"sport": bk.bucket_by(rows, "sport", None)}
        out = bk.author_findings(rows, buckets)
        assert "### Findings" in out
        # Either reports baseline or "no clear signal"
        assert "Baseline" in out or "no clear signal" in out


class TestGamePhaseDerivation:
    def test_nba_quarters(self):
        assert bk.derive_game_phase({"sport": "nba", "period": 1}) == "Q1"
        assert bk.derive_game_phase({"sport": "nba", "period": 2}) == "Q2"
        assert bk.derive_game_phase({"sport": "nba", "period": 4}) == "Q4"
        assert bk.derive_game_phase({"sport": "nba", "period": 5}) == "OT"

    def test_nhl_periods(self):
        assert bk.derive_game_phase({"sport": "nhl", "period": 1}) == "P1"
        assert bk.derive_game_phase({"sport": "nhl", "period": 3}) == "P3"
        assert bk.derive_game_phase({"sport": "nhl", "period": 4}) == "OT"

    def test_tennis_no_period(self):
        assert bk.derive_game_phase({"sport": "atp", "period": None}) == "Unknown"
        assert bk.derive_game_phase({"sport": "atp", "period": 2}) == "R2"

    def test_missing_sport(self):
        assert bk.derive_game_phase({"sport": None, "period": 2}) == "R2"
        assert bk.derive_game_phase({"sport": "atp", "period": None}) == "Unknown"
