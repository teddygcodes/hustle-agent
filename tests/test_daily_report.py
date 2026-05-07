"""Tests for tools/daily_report.py — Session 35 daily report orchestrator.

Same correctness shape as the Session-24 weekly digest tests, adapted for the
new 10-section layout:

  - Smoke: 10 section headers render against real on-disk state.
  - Date arg parsing (valid + invalid).
  - Crash invariant: if one section's renderer raises, the OUTPUT FILE EXISTS
    with the affected section showing `[section unavailable: ...]` and the
    other 9 sections still render.
  - Default date = yesterday in ET when --date omitted.
  - Footer present.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools._report_helpers as helpers  # noqa: E402
import tools.daily_report as daily_report  # noqa: E402


SECTION_HEADER_PREFIXES = [f"# {n}. " for n in range(1, 11)]


# ───────────────────────────────────────────────────────────── smoke + structure

def test_full_daily_report_renders_against_real_state(tmp_path):
    """All 10 sections produce output against live state, written to tmp_path."""
    out = daily_report.build_report(
        helpers.yesterday_in_et(),
        out_dir=tmp_path,
    )
    assert out.exists()
    md = out.read_text()
    for prefix in SECTION_HEADER_PREFIXES:
        assert prefix in md, f"missing section header {prefix!r}"
    assert "Last Run Stamp:" in md
    assert "_Sections rendered:" in md
    assert out.stat().st_size > 1000


def test_daily_report_runs_against_current_production_state(tmp_path):
    """Production-shape state should render a complete non-empty report."""
    out = daily_report.build_report(
        datetime(2026, 5, 7, tzinfo=helpers.ET),
        out_dir=tmp_path,
    )
    md = out.read_text()
    assert out.name == "daily_report_2026-05-07.md"
    assert out.stat().st_size > 1000
    for prefix in SECTION_HEADER_PREFIXES:
        assert prefix in md, f"missing section header {prefix!r}"
    assert "[section unavailable:" not in md


def test_daily_report_has_exactly_ten_sections(tmp_path):
    out = daily_report.build_report(helpers.yesterday_in_et(), out_dir=tmp_path)
    md = out.read_text()
    headers = [
        line for line in md.splitlines()
        if line.startswith("# ") and not line.startswith("## ")
    ]
    # Title + 10 numbered section headers = 11.
    assert len(headers) == 11, f"expected 11 H1 lines (1 title + 10 sections), got {len(headers)}: {headers}"


# ───────────────────────────────────────────────────────────── date parsing

def test_invalid_date_arg_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        daily_report.main(["--date", "not-a-date"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid --date" in err


def test_default_date_is_yesterday_in_et(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(helpers, "REPORTS_DIR", tmp_path)
    # Stub the section renderers so this test runs fast and doesn't fail on
    # whatever live state happens to look like today.
    for _, fn_name in helpers.SHARED_SECTIONS:
        monkeypatch.setattr(helpers, fn_name, lambda *a, **k: f"# stub\n")

    rc = daily_report.main([])
    assert rc == 0

    files = list((tmp_path / "daily").glob("daily_report_*.md"))
    assert len(files) == 1
    name = files[0].name
    yesterday = helpers.yesterday_in_et().date().isoformat()
    assert name == f"daily_report_{yesterday}.md"


# ─────────────────────────────────────────────────────── crash invariant

def test_crash_in_one_section_does_not_break_report(tmp_path, monkeypatch):
    """If §5 raises, output file exists with §5 marked unavailable; §6 still renders."""
    def boom(*_a, **_k):
        raise RuntimeError("synthetic test failure")
    monkeypatch.setattr(helpers, "render_cf_coverage", boom)

    out = daily_report.build_report(helpers.yesterday_in_et(), out_dir=tmp_path)

    assert out.exists()
    md = out.read_text()
    # All 10 section headers still appear (header explicit + body fallback).
    for prefix in SECTION_HEADER_PREFIXES:
        assert prefix in md, f"missing section header {prefix!r}"
    # The unavailable marker is present and names the exception.
    assert "[section unavailable:" in md
    assert "RuntimeError" in md
    # §6 still renders.
    assert "# 6. Live momentum events" in md
    # Footer reflects the skipped section.
    assert "5. CF coverage" in md
    assert "Sections rendered: 9/10" in md


def test_crash_invariant_partial_file_survives_mid_run(tmp_path, monkeypatch):
    """First I/O writes the header before any section runs — verify the file
    is created on disk before the (synthetic) crash, so a real mid-run crash
    would still leave a partial report."""
    written: list[Path] = []

    real_write_header = helpers.write_header

    def tracking_write_header(out_path, *args, **kwargs):
        real_write_header(out_path, *args, **kwargs)
        written.append(out_path)
        # Header must be on disk now — verify before sections run.
        assert out_path.exists()
        assert "Daily Report" in out_path.read_text()

    monkeypatch.setattr(helpers, "write_header", tracking_write_header)

    def boom(*_a, **_k):
        raise RuntimeError("crash mid-section")
    # Crash the first section.
    monkeypatch.setattr(helpers, "render_health_pulse", boom)

    out = daily_report.build_report(helpers.yesterday_in_et(), out_dir=tmp_path)
    assert out in written
    assert out.exists()


# ───────────────────────────────────────────────────── empty data fallback

def test_empty_data_renders_no_data_sentinels(tmp_path, monkeypatch):
    """With every state file empty, sections render explicit "no data" sentinels
    rather than crashing."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(helpers, "STATE_DIR", state)
    monkeypatch.setattr(helpers, "ARCHIVE_DIR", state / "archive")
    monkeypatch.setattr(helpers, "PAPER_TRADES_FILE", state / "paper_trades.json")
    monkeypatch.setattr(helpers, "CLV_FILE", state / "clv.json")
    monkeypatch.setattr(helpers, "BOT_STATE_FILE", state / "bot_state.json")
    monkeypatch.setattr(helpers, "LOCK_FILE", state / "bot.lock")
    monkeypatch.setattr(helpers, "CADENCE_FILE", state / "tracker_cadence.jsonl")
    monkeypatch.setattr(helpers, "DECISIONS_FILE", state / "decisions.jsonl")
    monkeypatch.setattr(helpers, "LIVE_TICKS_FILE", state / "live_ticks.jsonl")
    monkeypatch.setattr(helpers, "UNIVERSE_FILE", state / "universe.jsonl")
    monkeypatch.setattr(helpers, "LIVE_JOURNAL_FILE", state / "live_journal.json")
    monkeypatch.setattr(helpers, "LOG_FILE", state / "bot.log")

    # Create empty/stub files so sub-tools that load_decisions/load_journal
    # against the production paths don't see the live data either.
    (state / "paper_trades.json").write_text("[]")
    (state / "clv.json").write_text("[]")
    (state / "live_journal.json").write_text("[]")
    (state / "bot_state.json").write_text("{}")
    (state / "decisions.jsonl").write_text("")
    (state / "live_ticks.jsonl").write_text("")
    (state / "universe.jsonl").write_text("")
    (state / "tracker_cadence.jsonl").write_text("")
    (state / "bot.log").write_text("")

    # Tools called by sections (cohort, journal_analysis) read from their own
    # module-level paths, not these — stub their entry points to keep this
    # test focused on the helpers' own renderers.
    monkeypatch.setattr(helpers, "render_decision_audit",
                        lambda *a, **k: "# 3. Decision audit\n\n_No decisions in window._")
    monkeypatch.setattr(helpers, "render_live_momentum_events",
                        lambda *a, **k: "# 6. Live momentum events\n\n_No live_journal events in window._")

    out = daily_report.build_report(helpers.yesterday_in_et(), out_dir=tmp_path)
    md = out.read_text()
    # Each "no data" section uses an explicit italic sentinel.
    sentinels = [
        ("# 2. Scanner activity", "_No universe.jsonl rows in window._"),
        ("# 4. Trade activity", "_No trades resolved in window._"),
        ("# 5. CF coverage", "_No counterfactuals emitted in window._"),
        ("# 7. DQS + regime distribution", "_No live_ticks rows in window._"),
        ("# 8. Cadence health", "_No tracker_cadence rows in window._"),
        ("# 9. Errors", "_No errors logged._"),
    ]
    for header, sentinel in sentinels:
        assert header in md, f"missing {header}"
        assert sentinel in md, f"section {header!r} should render sentinel {sentinel!r}"


# ──────────────────────────────────────────────────────── footer + last-run

def test_footer_carries_iso_timestamp(tmp_path):
    out = daily_report.build_report(helpers.yesterday_in_et(), out_dir=tmp_path)
    md = out.read_text()
    last_lines = [l for l in md.splitlines() if l.startswith("Last Run Stamp:")]
    assert last_lines, "footer Last Run Stamp line missing"
    stamp = last_lines[-1].split(":", 1)[1].strip()
    # Parses as a real ISO timestamp.
    dt = datetime.fromisoformat(stamp)
    assert dt.tzinfo is not None


# ─────────────────────────────────────────────────── stdout-vs-file parity

def test_stdout_matches_file_content(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(helpers, "REPORTS_DIR", tmp_path)
    # Stub all renderers for speed.
    for _, fn_name in helpers.SHARED_SECTIONS:
        monkeypatch.setattr(helpers, fn_name, lambda *a, **k: "# stub\n")
    rc = daily_report.main(["--date", "2026-04-29"])
    assert rc == 0
    captured = capsys.readouterr().out
    files = list((tmp_path / "daily").glob("daily_report_*.md"))
    assert len(files) == 1
    file_content = files[0].read_text()
    assert captured == file_content


def test_daily_report_writes_to_expected_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(helpers, "REPORTS_DIR", tmp_path)
    for _, fn_name in helpers.SHARED_SECTIONS:
        monkeypatch.setattr(helpers, fn_name, lambda *a, **k: "# stub\n")

    rc = daily_report.main(["--date", "2026-05-07"])

    assert rc == 0
    capsys.readouterr()
    out = tmp_path / "daily" / "daily_report_2026-05-07.md"
    assert out.exists()
    assert out.stat().st_size > 0
