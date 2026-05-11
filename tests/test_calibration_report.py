"""Session 99: tests for the live_momentum proxy calibration section in
tools/calibration_report.py. The existing vig_stack section is exercised
indirectly elsewhere; these tests pin the new live_momentum section's
behavior — empty paper_trades, mixed rows, malformed rows."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.calibration_report as cr  # noqa: E402


def test_missing_paper_trades_file_renders_caveat(tmp_path, monkeypatch):
    """When paper_trades.json doesn't exist, the section header still renders
    + a 'missing' caveat. No crash."""
    monkeypatch.setattr(cr, "PAPER_TRADES_FILE", tmp_path / "does_not_exist.json")
    out = cr.report_live_momentum_calibration(days=7)
    assert "## live_momentum proxy calibration" in out
    assert "paper_trades.json missing" in out


def test_empty_paper_trades_renders_forward_only_caveat(tmp_path, monkeypatch):
    """Empty paper_trades.json (no live_momentum rows yet) renders the
    'ships forward-only' caveat from the spec — operators understand pre-data
    state without thinking the proxy is broken."""
    pt = tmp_path / "paper_trades.json"
    pt.write_text("[]")
    monkeypatch.setattr(cr, "PAPER_TRADES_FILE", pt)
    out = cr.report_live_momentum_calibration(days=7)
    assert "n=0" in out
    assert "ships forward-only" in out
    assert "14d" in out


def test_unreadable_paper_trades_renders_unreadable_caveat(tmp_path, monkeypatch):
    """Corrupt JSON in paper_trades.json doesn't crash; surfaces a clear
    'unreadable' caveat for operator triage."""
    pt = tmp_path / "paper_trades.json"
    pt.write_text("{not valid json")
    monkeypatch.setattr(cr, "PAPER_TRADES_FILE", pt)
    out = cr.report_live_momentum_calibration(days=7)
    assert "unreadable" in out


def test_non_list_paper_trades_renders_schema_caveat(tmp_path, monkeypatch):
    """If paper_trades.json is valid JSON but not a list, surface a clear
    schema-unexpected caveat rather than crashing."""
    pt = tmp_path / "paper_trades.json"
    pt.write_text('{"oops": "not a list"}')
    monkeypatch.setattr(cr, "PAPER_TRADES_FILE", pt)
    out = cr.report_live_momentum_calibration(days=7)
    assert "schema unexpected" in out


def test_only_unresolved_live_momentum_trades_renders_empty(tmp_path, monkeypatch):
    """Open trades (status='open') don't calibrate; section renders with n=0
    + forward-only caveat. Verifies the resolved-status filter."""
    pt = tmp_path / "paper_trades.json"
    trades = [
        {"id": "PAPER-1", "type": "live_momentum", "status": "open",
         "estimated_win_prob": 0.65},
        {"id": "PAPER-2", "type": "live_momentum", "status": "open",
         "estimated_win_prob": 0.78},
    ]
    pt.write_text(json.dumps(trades))
    monkeypatch.setattr(cr, "PAPER_TRADES_FILE", pt)
    out = cr.report_live_momentum_calibration(days=7)
    assert "n=0" in out


def test_resolved_records_without_estimated_win_prob_dropped(tmp_path, monkeypatch):
    """Pre-Session-99 resolved live_momentum trades (no estimated_win_prob
    field) are silently dropped. Forward-only — calibration starts at restart."""
    pt = tmp_path / "paper_trades.json"
    trades = [
        {"id": "PAPER-OLD-1", "type": "live_momentum", "status": "won"},  # no field
        {"id": "PAPER-OLD-2", "type": "live_momentum", "status": "lost"},
    ]
    pt.write_text(json.dumps(trades))
    monkeypatch.setattr(cr, "PAPER_TRADES_FILE", pt)
    out = cr.report_live_momentum_calibration(days=7)
    assert "n=0" in out


def test_resolved_live_momentum_with_proxy_renders_full_calibration(tmp_path, monkeypatch):
    """Populated case: 6 resolved trades render Brier + bias + bucket table.
    Verifies the math is plumbed correctly end-to-end."""
    pt = tmp_path / "paper_trades.json"
    trades = [
        # 3 trades in [0.6, 0.7) bucket: 2 won, 1 lost → 66.7% hit rate
        {"id": "P1", "type": "live_momentum", "status": "won",
         "estimated_win_prob": 0.62},
        {"id": "P2", "type": "live_momentum", "status": "won",
         "estimated_win_prob": 0.65},
        {"id": "P3", "type": "live_momentum", "status": "lost",
         "estimated_win_prob": 0.68},
        # 3 trades in [0.8, 0.9) bucket: 3 won → 100% hit rate
        {"id": "P4", "type": "live_momentum", "status": "won",
         "estimated_win_prob": 0.82},
        {"id": "P5", "type": "live_momentum", "status": "won",
         "estimated_win_prob": 0.85},
        {"id": "P6", "type": "live_momentum", "status": "won",
         "estimated_win_prob": 0.88},
    ]
    pt.write_text(json.dumps(trades))
    monkeypatch.setattr(cr, "PAPER_TRADES_FILE", pt)
    out = cr.report_live_momentum_calibration(days=7)
    assert "n=6" in out
    assert "Mean predicted" in out
    assert "Brier score" in out
    # Bucket table renders both buckets with their hit rates.
    assert "[0.60,0.70)" in out
    assert "[0.80,0.90)" in out
    assert "66.7%" in out
    assert "100.0%" in out


def test_vig_stack_records_excluded_from_live_momentum_section(tmp_path, monkeypatch):
    """A populated paper_trades.json with mixed vig_stack + live_momentum
    rows: the live_momentum section only counts live_momentum rows. Spillover
    prevention at the read layer (independent of the write layer's check)."""
    pt = tmp_path / "paper_trades.json"
    trades = [
        # vig_stack trades shouldn't appear in the live_momentum count even if
        # they (hypothetically) carry estimated_win_prob — the filter is on type.
        {"id": "V1", "type": "vig_stack", "status": "won",
         "estimated_win_prob": 0.55},
        {"id": "V2", "type": "vig_stack", "status": "lost",
         "estimated_win_prob": 0.65},
        # Only this one counts.
        {"id": "M1", "type": "live_momentum", "status": "won",
         "estimated_win_prob": 0.72},
    ]
    pt.write_text(json.dumps(trades))
    monkeypatch.setattr(cr, "PAPER_TRADES_FILE", pt)
    out = cr.report_live_momentum_calibration(days=7)
    assert "n=1" in out
