"""Tests for tools/weekly_digest.py — Session 24 weekly digest aggregator.

The digest is a pure aggregator over existing analysis tools + a few direct
state-file reads. The key correctness properties are:

  1. All 8 sections render against real on-disk state (smoke).
  2. **Failure tolerance**: if ONE section's underlying source raises, the
     digest still emits the other 7 sections + an "[unavailable]" marker
     and a "Sections rendered: 7/8" footer. This is the central invariant
     — without it the digest is unreliable enough that nobody runs it
     weekly.
  3. The archive file content equals the stdout content.
  4. `--regime-by` passes through without breaking the section count.
  5. Building the digest twice in the same UTC instant produces identical
     bytes (no ordering or microsecond contamination).
  6. P&L section windows trades by `resolved_at` correctly: this-week,
     last-week, and ignored-when-open.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.weekly_digest as wd  # noqa: E402


SECTION_HEADER_PREFIXES = [f"# {n}. " for n in range(1, 9)]


# ---------------------------------------------------------- smoke + structure


def test_full_digest_renders_against_real_state():
    """Smoke test: against the real on-disk state, all 8 sections produce output."""
    md = wd.build_digest()
    for prefix in SECTION_HEADER_PREFIXES:
        assert prefix in md, f"missing section header {prefix!r}"
    assert "_Sections rendered:" in md


def test_section_count_is_exactly_eight():
    md = wd.build_digest()
    headers = [line for line in md.splitlines() if line.startswith("# ") and not line.startswith("## ")]
    # H1 'Weekly digest' + 8 numbered section H1s = 9
    assert len(headers) == 9, f"expected 9 H1 lines (1 title + 8 sections), got {len(headers)}: {headers}"


# ---------------------------------------------------------- failure tolerance


def test_failure_tolerance_one_section_raises(monkeypatch):
    """If one component crashes, the other 7 still render and footer reflects skip."""
    def boom(*_a, **_k):
        raise RuntimeError("synthetic test failure")
    monkeypatch.setattr(wd, "section_calibration", boom)

    md = wd.build_digest()

    # All 8 section headers still appear
    for prefix in SECTION_HEADER_PREFIXES:
        assert prefix in md, f"missing section header {prefix!r} after one section crashed"

    # The crashed section body shows the unavailable marker + error class
    assert "[section unavailable:" in md
    assert "RuntimeError" in md

    # Footer reports 7/8 with the section title in the skipped list
    assert "Sections rendered: 7/8" in md
    assert "3. Calibration" in md  # listed in skipped


def test_failure_tolerance_multiple_sections_raise(monkeypatch):
    """Two crashes => 6/8, both listed in skip footer, others still render."""
    def boom(*_a, **_k):
        raise ValueError("oops")
    monkeypatch.setattr(wd, "section_excursion", boom)
    monkeypatch.setattr(wd, "section_universe", boom)

    md = wd.build_digest()
    assert "Sections rendered: 6/8" in md
    assert "4. Excursion" in md
    assert "6. Universe coverage" in md
    # Other sections still present
    assert "# 1. P&L summary" in md
    assert "# 8. Bot health" in md


# ---------------------------------------------------------- regime-by passthrough


def test_regime_by_flag_passthrough():
    md = wd.build_digest(regime_by="sport_phase")
    assert "Regime axis: `sport_phase`" in md
    headers = [line for line in md.splitlines() if line.startswith("# ") and not line.startswith("## ")]
    assert len(headers) == 9


# ---------------------------------------------------------- idempotency


def test_idempotency_against_frozen_inputs(monkeypatch, tmp_path):
    """Two builds against a frozen state snapshot produce byte-identical output.

    Live state files (decisions.jsonl, tracker_cadence.jsonl) are appended
    to continuously by the running bot, so we snapshot the bot-health inputs
    and the P&L/CF inputs into tmp paths. We then mock out the component-tool
    sections (which do their own live reads) so what we're actually testing
    is the digest's *own* logic — no random ordering, no re-reading mutation.
    """
    # Freeze the state files the digest reads directly
    snap_paper = tmp_path / "paper_trades.json"
    snap_paper.write_text((REPO_ROOT / "bot" / "state" / "paper_trades.json").read_text())
    snap_clv = tmp_path / "clv.json"
    snap_clv.write_text((REPO_ROOT / "bot" / "state" / "clv.json").read_text())
    snap_bot = tmp_path / "bot_state.json"
    snap_bot.write_text((REPO_ROOT / "bot" / "state" / "bot_state.json").read_text())
    snap_cad = tmp_path / "tracker_cadence.jsonl"
    snap_cad.write_text((REPO_ROOT / "bot" / "state" / "tracker_cadence.jsonl").read_text())
    snap_dec = tmp_path / "decisions.jsonl"
    snap_dec.write_text((REPO_ROOT / "bot" / "state" / "decisions.jsonl").read_text())
    snap_log = tmp_path / "bot.log"
    log_src = REPO_ROOT / "bot" / "logs" / "bot.log"
    if log_src.exists():
        snap_log.write_bytes(log_src.read_bytes())
    else:
        snap_log.write_text("")

    monkeypatch.setattr(wd, "PAPER_TRADES_FILE", snap_paper)
    monkeypatch.setattr(wd, "CLV_FILE", snap_clv)
    monkeypatch.setattr(wd, "BOT_STATE_FILE", snap_bot)
    monkeypatch.setattr(wd, "CADENCE_FILE", snap_cad)
    monkeypatch.setattr(wd, "DECISIONS_FILE", snap_dec)
    monkeypatch.setattr(wd, "LOG_FILE", snap_log)

    # Stub component-tool sections (they read live files in subordinate modules
    # we don't control here). The point of this test is digest determinism,
    # not tool determinism.
    monkeypatch.setattr(wd, "section_cohort", lambda *a, **k: "# 2. stub\n")
    monkeypatch.setattr(wd, "section_calibration", lambda *a, **k: "# 3. stub\n")
    monkeypatch.setattr(wd, "section_excursion", lambda *a, **k: "# 4. stub\n")
    monkeypatch.setattr(wd, "section_journal", lambda *a, **k: "# 5. stub\n")
    monkeypatch.setattr(wd, "section_universe", lambda *a, **k: "# 6. stub\n")

    fixed = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    a = wd.build_digest(now_utc=fixed)
    b = wd.build_digest(now_utc=fixed)
    assert a == b


# ---------------------------------------------------------- file-output parity


def test_file_output_matches_stdout(tmp_path, monkeypatch):
    """Running main() writes to bot/state/weekly_digest_<date>.md with the same
    content as stdout."""
    proc = subprocess.run(
        [sys.executable, "tools/weekly_digest.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"digest failed: {proc.stderr}"
    stdout = proc.stdout

    # Find today's archive file
    archives = sorted((REPO_ROOT / "bot" / "state").glob("weekly_digest_*.md"))
    assert archives, "no weekly_digest_*.md archive written"
    latest = archives[-1]
    file_content = latest.read_text()

    assert file_content == stdout, "archived file content != stdout"


# ---------------------------------------------------------- P&L windowing


def test_pnl_section_windows_by_resolved_at(monkeypatch, tmp_path):
    """Synthetic paper_trades: 2 in this-week, 1 in last-week, 1 still open.
    Section 1 must reflect those splits and ignore the open trade."""
    fixed_now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    this_week = (fixed_now - timedelta(days=2)).isoformat()
    last_week = (fixed_now - timedelta(days=10)).isoformat()

    fake_trades = [
        # this-week: vig_stack +$5, live_momentum -$2 (NBA)
        {"ticker": "KXATPMATCH-A", "type": "vig_stack", "pnl": 5.00, "resolved_at": this_week},
        {"ticker": "KXNBAGAME-A",  "type": "live_momentum", "pnl": -2.00, "resolved_at": this_week},
        # last-week: vig_stack -$3
        {"ticker": "KXATPMATCH-B", "type": "vig_stack", "pnl": -3.00, "resolved_at": last_week},
        # open trade — must be ignored (no resolved_at, no pnl)
        {"ticker": "KXNBAGAME-B",  "type": "live_momentum", "pnl": None, "resolved_at": None},
    ]
    fake_path = tmp_path / "paper_trades.json"
    fake_path.write_text(json.dumps(fake_trades))
    monkeypatch.setattr(wd, "PAPER_TRADES_FILE", fake_path)

    body = wd.section_pnl(fixed_now, regime_by=None)

    # Header counts reflect 2 this-week + 1 last-week
    assert "this week: 2 trades" in body
    assert "last week: 1 trades" in body

    # Per-strategy: vig_stack +500 vs -300; live_momentum -200 vs 0
    assert "| `vig_stack` | 500 | -300 | +800 |" in body
    assert "| `live_momentum` | -200 | 0 | -200 |" in body

    # Per-sport breakdown for live_momentum should include nba
    assert "nba" in body


# ---------------------------------------------------------- _safe_section util


def test_safe_section_returns_marker_on_exception():
    def raiser():
        raise KeyError("missing_key")
    body, reason = wd._safe_section(raiser)
    assert "[section unavailable:" in body
    assert "KeyError" in body
    assert reason and "KeyError" in reason


def test_safe_section_passes_through_on_success():
    body, reason = wd._safe_section(lambda: "# fine\nbody\n")
    assert body == "# fine\nbody\n"
    assert reason is None
