"""Tests for tools/hypothetical_report.py — parameter sweep table generation."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# tools/ is gitignored but importable as a path; tests need to add it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import hypothetical_report as hr


def _stub_summary(opp_count, matched, sum_clv, mean_edge=0.05, mean_clv=2.0):
    """Build a fake aggregate_results-shaped summary."""
    return {
        "per_day": {},
        "totals": {
            "stub_opp_type": {
                "opp_count": opp_count,
                "matched_count": matched,
                "matched_pct": (100 * matched / opp_count)
                                if opp_count else 0.0,
                "mean_edge": mean_edge,
                "mean_clv_cents": mean_clv,
                "sum_clv_cents": sum_clv,
                "win_rate_pct": 60.0,
            }
        },
        "matched_actually_taken": [],
    }


class TestSweepTable:
    def test_emits_one_row_per_value(self, monkeypatch):
        values = [0.05, 0.10, 0.15, 0.20, 0.25]
        summaries = [_stub_summary(100 - i*10, 50 - i*5, 50.0 - i*2)
                     for i in range(len(values))]
        call_count = {"n": 0}

        def fake_run_backtest(strategy, *, start, end, include_history):
            i = call_count["n"]
            call_count["n"] += 1
            return summaries[i]

        monkeypatch.setattr(hr, "run_backtest", fake_run_backtest)

        rows = hr.run_sweep(
            strategy_name="vig_stack_series",
            param_name="min_relative_edge",
            values=values,
            start=date(2026, 4, 18),
            end=date(2026, 4, 25),
            include_history=False,
        )
        assert len(rows) == 5

    def test_rows_sorted_by_sum_clv_descending(self, monkeypatch):
        values = [0.05, 0.10, 0.15]
        # Make 0.10 the best
        sum_clvs = {0.05: 30.0, 0.10: 80.0, 0.15: 50.0}

        def fake_run_backtest(strategy, *, start, end, include_history):
            # Read which value this strategy was instantiated with
            return _stub_summary(100, 50, sum_clvs[strategy._min_relative_edge])
        monkeypatch.setattr(hr, "run_backtest", fake_run_backtest)

        rows = hr.run_sweep(
            strategy_name="vig_stack_series",
            param_name="min_relative_edge",
            values=values,
            start=date(2026, 4, 18),
            end=date(2026, 4, 25),
            include_history=False,
        )
        assert rows[0]["param_value"] == 0.10
        assert rows[0]["sum_clv_cents"] == 80.0
        assert rows[1]["param_value"] == 0.15
        assert rows[2]["param_value"] == 0.05

    def test_markdown_calls_out_best_variant(self):
        rows = [
            {"param_value": 0.10, "sum_clv_cents": 80.0, "opp_count": 90,
             "matched_count": 50, "match_pct": 55.0, "win_rate_pct": 60.0,
             "mean_edge": 0.05, "mean_clv_cents": 2.0},
            {"param_value": 0.05, "sum_clv_cents": 30.0, "opp_count": 100,
             "matched_count": 60, "match_pct": 60.0, "win_rate_pct": 50.0,
             "mean_edge": 0.04, "mean_clv_cents": 1.0},
        ]
        md = hr.format_sweep_table(
            strategy_name="vig_stack_series",
            param_name="min_relative_edge",
            rows=rows,
        )
        # Best variant value should appear in the Best: line
        assert "Best:" in md
        assert "min_relative_edge=0.1" in md
        # Both rows should be in the table
        assert "0.1" in md
        assert "0.05" in md

    def test_unknown_strategy_raises_clear_error(self):
        with pytest.raises(SystemExit) as exc:
            hr.run_sweep(
                strategy_name="bogus_strategy",
                param_name="min_relative_edge",
                values=[0.05],
                start=date(2026, 4, 18),
                end=date(2026, 4, 25),
                include_history=False,
            )
        assert exc.value.code == 2
