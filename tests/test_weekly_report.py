"""Tests for tools/weekly_report.py — Session 35 weekly report (migrated + extended).

Migrated tests from the old test_weekly_digest.py (smoke, crash recovery,
stdout/file parity, idempotency-style stub coverage). Plus six new cases for
the weekly-only sections (§§11–16): week-over-week deltas, calibration findings
math, retuning candidates derivation, etc.

The weekly report is heavy (§13 rebuilds the live_momentum dataset, ~30s) so
unless explicitly testing the integration we stub the §§12–14 renderers.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools._report_helpers as helpers  # noqa: E402
import tools.weekly_report as weekly_report  # noqa: E402


SHARED_HEADER_PREFIXES = [f"# {n}. " for n in range(1, 11)]
WEEKLY_HEADER_PREFIXES = [f"# {n}. " for n in range(11, 17)]


@pytest.fixture
def stub_heavy_sections(monkeypatch):
    """Stub §§12–14 (and §13 dataset rebuild) to avoid slow runs in tests."""
    monkeypatch.setattr(weekly_report, "render_buckets", lambda: "# 12. Bucket analysis\n\n_stubbed for tests._")
    monkeypatch.setattr(
        weekly_report, "render_dataset_rebuild_summary",
        lambda ws, we: "# 13. Research dataset rebuild\n\n_stubbed for tests._",
    )
    monkeypatch.setattr(
        weekly_report, "render_excursion_and_exit_replay",
        lambda regime_by: "# 14. Excursion + exit-replay\n\n_stubbed for tests._",
    )


# ───────────────────────────────────────────────────────── smoke + structure

def test_full_weekly_report_renders_all_sixteen_sections(tmp_path, stub_heavy_sections):
    week_end = helpers.last_sunday_in_et()
    out = weekly_report.build_report(week_end, out_dir=tmp_path)
    assert out.exists()
    md = out.read_text()
    for prefix in SHARED_HEADER_PREFIXES:
        assert prefix in md, f"missing shared section header {prefix!r}"
    for prefix in WEEKLY_HEADER_PREFIXES:
        assert prefix in md, f"missing weekly-only section header {prefix!r}"
    assert "Last Run Stamp:" in md
    assert "_Sections rendered:" in md


def test_weekly_report_has_exactly_sixteen_h1_sections(tmp_path, stub_heavy_sections):
    week_end = helpers.last_sunday_in_et()
    out = weekly_report.build_report(week_end, out_dir=tmp_path)
    md = out.read_text()
    headers = [
        line for line in md.splitlines()
        if line.startswith("# ") and not line.startswith("## ")
    ]
    # Title + 16 numbered section H1s = 17.
    assert len(headers) == 17, f"expected 17 H1 lines (1 title + 16 sections), got {len(headers)}"


# ───────────────────────────────────────────────────── failure tolerance

def test_failure_tolerance_one_section_raises(tmp_path, monkeypatch, stub_heavy_sections):
    def boom(*_a, **_k):
        raise RuntimeError("synthetic test failure")
    monkeypatch.setattr(helpers, "render_decision_audit", boom)

    out = weekly_report.build_report(helpers.last_sunday_in_et(), out_dir=tmp_path)
    md = out.read_text()
    # All 16 section headers still appear.
    for prefix in SHARED_HEADER_PREFIXES + WEEKLY_HEADER_PREFIXES:
        assert prefix in md
    assert "[section unavailable:" in md
    assert "RuntimeError" in md
    assert "Sections rendered: 15/16" in md
    assert "3. Decision audit" in md  # listed in skipped


def test_failure_tolerance_multiple_sections_raise(tmp_path, monkeypatch, stub_heavy_sections):
    def boom(*_a, **_k):
        raise ValueError("oops")
    monkeypatch.setattr(helpers, "render_excursion_and_exit_replay", boom, raising=False)
    monkeypatch.setattr(helpers, "render_cf_coverage", boom)
    monkeypatch.setattr(weekly_report, "render_buckets", boom)

    out = weekly_report.build_report(helpers.last_sunday_in_et(), out_dir=tmp_path)
    md = out.read_text()
    assert "Sections rendered: 14/16" in md
    assert "5. CF coverage" in md
    assert "12. Bucket analysis" in md


# ───────────────────────────────────────────────────────── regime-by passthrough

def test_regime_by_flag_passthrough(tmp_path, stub_heavy_sections):
    out = weekly_report.build_report(helpers.last_sunday_in_et(), regime_by="sport_phase", out_dir=tmp_path)
    md = out.read_text()
    assert "Regime axis: `sport_phase`" in md
    headers = [line for line in md.splitlines() if line.startswith("# ") and not line.startswith("## ")]
    assert len(headers) == 17


# ─────────────────────────────────────────────────── stdout/file parity

def test_stdout_matches_file_content(tmp_path, monkeypatch, capsys, stub_heavy_sections):
    monkeypatch.setattr(helpers, "REPORTS_DIR", tmp_path)
    week_end = helpers.last_sunday_in_et()
    rc = weekly_report.main(["--week-end", week_end.date().isoformat()])
    assert rc == 0
    captured = capsys.readouterr().out
    files = list((tmp_path / "weekly").glob("weekly_report_*.md"))
    assert len(files) == 1
    file_content = files[0].read_text()
    assert captured == file_content


# ─────────────────────────────────────────────────────────── §11 deltas

def test_section_11_no_baseline_renders_explicit_marker(tmp_path, monkeypatch, stub_heavy_sections):
    monkeypatch.setattr(helpers, "REPORTS_DIR", tmp_path)
    out = weekly_report.build_report(helpers.last_sunday_in_et(), out_dir=tmp_path / "weekly")
    md = out.read_text()
    assert "# 11. Week-over-week deltas" in md
    assert "_No baseline yet._" in md


def test_section_11_with_prior_file_renders_delta_table(tmp_path, monkeypatch, stub_heavy_sections):
    monkeypatch.setattr(helpers, "REPORTS_DIR", tmp_path)
    weekly_dir = tmp_path / "weekly"
    weekly_dir.mkdir()

    # Synthesize a prior weekly report file that carries one of the headline patterns.
    week_end = helpers.last_sunday_in_et()
    prior_end = week_end - timedelta(days=7)
    iso_year, iso_week, _ = prior_end.isocalendar()
    prior_path = weekly_dir / f"weekly_report_{iso_year}-W{iso_week:02d}.md"
    prior_path.write_text(
        "# Weekly Report — stub\n\n"
        "Scans observed: **42** (1.0/hr over 168.0h).\n"
        "Total ticks in window: **9,999**.\n"
    )

    out = weekly_report.build_report(week_end, out_dir=weekly_dir)
    md = out.read_text()
    assert "# 11. Week-over-week deltas" in md
    # Prior week's headlines should appear in the delta table.
    assert "Scans observed" in md
    assert "9,999" in md or "**9,999**" in md


# ───────────────────────────────────────────────────── §15 calibration findings

def test_section_15_filters_to_misturned_gates_only():
    """Synthesize cohort bins so one gate qualifies (high reject rate + positive
    mean CLV) and one doesn't, assert only the qualifying one appears."""
    findings = _build_findings_from_synthetic_cohort_bins(
        decision_bins={
            ("vig_stack_series", "_all_", "low_liquidity"): {
                "invocations": 100, "rejects": 80, "accepts": 20,
                "edges_of_rejects": [0.05] * 80, "distances": [],
            },
            ("vig_stack_series", "_all_", "non_stable_below_weather_floor"): {
                # Reject rate above 50% but CLV negative — must NOT appear.
                "invocations": 200, "rejects": 199, "accepts": 1,
                "edges_of_rejects": [0.03] * 199, "distances": [],
            },
            ("vig_stack_series", "_all_", "edge_below_threshold"): {
                # High reject rate, but no CFs settled → can't compute mean CLV.
                "invocations": 500, "rejects": 500, "accepts": 0,
                "edges_of_rejects": [0.001] * 500, "distances": [],
            },
        },
        cf_bins={
            ("vig_stack_series", "_all_", "low_liquidity"): {
                "total": 30, "settled": 20, "pending": 10, "sum_clv_rel": 0.40,  # mean +0.020
            },
            ("vig_stack_series", "_all_", "non_stable_below_weather_floor"): {
                "total": 30, "settled": 20, "pending": 10, "sum_clv_rel": -0.20,  # mean -0.010
            },
            # No CF bin for edge_below_threshold.
        },
    )
    gates = [(f["opp_type"], f["gate"]) for f in findings]
    assert ("vig_stack_series", "low_liquidity") in gates
    assert ("vig_stack_series", "non_stable_below_weather_floor") not in gates
    assert ("vig_stack_series", "edge_below_threshold") not in gates


def test_section_15_sorted_by_clv_descending():
    findings = _build_findings_from_synthetic_cohort_bins(
        decision_bins={
            ("a", "_all_", "g1"): {"invocations": 10, "rejects": 8, "accepts": 2, "edges_of_rejects": [], "distances": []},
            ("a", "_all_", "g2"): {"invocations": 10, "rejects": 8, "accepts": 2, "edges_of_rejects": [], "distances": []},
        },
        cf_bins={
            ("a", "_all_", "g1"): {"total": 5, "settled": 5, "pending": 0, "sum_clv_rel": 0.10},  # +0.020
            ("a", "_all_", "g2"): {"total": 5, "settled": 5, "pending": 0, "sum_clv_rel": 0.50},  # +0.100
        },
    )
    assert [f["gate"] for f in findings] == ["g2", "g1"]


def test_section_16_renders_one_bullet_per_finding():
    findings = [
        {"opp_type": "vig_stack", "gate": "low_liquidity", "reject_rate": 0.85,
         "mean_clv_relative": 0.04, "n_settled_cfs": 12},
        {"opp_type": "live_momentum", "gate": "dip_too_big", "reject_rate": 0.60,
         "mean_clv_relative": 0.02, "n_settled_cfs": 8},
    ]
    body = weekly_report.render_retuning_candidates(findings)
    assert "vig_stack" in body and "low_liquidity" in body
    assert "live_momentum" in body and "dip_too_big" in body
    bullets = [l for l in body.splitlines() if l.startswith("- Consider Session")]
    assert len(bullets) == 2


def test_section_16_renders_empty_marker_when_no_findings():
    body = weekly_report.render_retuning_candidates([])
    assert "_No retuning candidates this week._" in body


# ─────────────────────────────────────────────── helpers for synthesizing tests

def _build_findings_from_synthetic_cohort_bins(decision_bins, cf_bins):
    """Run the §15 finder against handwritten cohort_report-shaped bins.

    The real `_calibration_findings` calls cohort_report.load_decisions/aggregate.
    For unit tests we monkey-patch those out so we can drive the math directly.
    """
    import tools.cohort_report as cohort_report

    class _MonkeyPatcher:
        def __init__(self):
            self._patches = []
        def setattr(self, mod, attr, val):
            self._patches.append((mod, attr, getattr(mod, attr, None)))
            setattr(mod, attr, val)
        def undo(self):
            for mod, attr, old in reversed(self._patches):
                setattr(mod, attr, old)

    mp = _MonkeyPatcher()
    try:
        mp.setattr(cohort_report, "load_decisions", lambda *_a, **_k: [])
        mp.setattr(cohort_report, "load_cf_records", lambda *_a, **_k: [])
        mp.setattr(cohort_report, "aggregate_decisions", lambda *_a, **_k: decision_bins)
        mp.setattr(cohort_report, "aggregate_cf", lambda *_a, **_k: cf_bins)
        return weekly_report._calibration_findings(
            window_start=datetime(2026, 4, 22, tzinfo=timezone.utc),
            window_end=datetime(2026, 4, 29, tzinfo=timezone.utc),
            regime_by=None,
        )
    finally:
        mp.undo()
