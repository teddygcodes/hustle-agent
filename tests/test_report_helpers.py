"""Tests for tools/_report_helpers.py — Session 35 shared helpers.

Cover the small primitives in isolation so failures land here, not in the
larger daily/weekly orchestrator tests where the failure surface is muddier:

  - JSONL streaming with malformed-line tolerance + ts filtering
  - Window math (ET → UTC, yesterday/last-Sunday)
  - Health pulse rendering (one row per branch)
  - State file growth math (uncompressed-vs-gzipped fair comparison)
  - Traceback tail dedupe + window filter
  - process_alive thresholds
  - _safe_section wrapper
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import _report_helpers as h  # noqa: E402


# ───────────────────────────────────────────────────────── JSONL streaming

def test_iter_jsonl_tolerant_skips_malformed(tmp_path, capsys):
    p = tmp_path / "x.jsonl"
    p.write_text(
        '{"ts": "2026-04-29T10:00:00Z", "v": 1}\n'
        'not json at all\n'
        '{"ts": "2026-04-29T11:00:00Z", "v": 2}\n'
    )
    rows = list(h.iter_jsonl_tolerant(p))
    assert [r["v"] for r in rows] == [1, 2]
    err = capsys.readouterr().err
    assert "skipped 1 malformed lines" in err


def test_iter_jsonl_tolerant_window_filter(tmp_path):
    p = tmp_path / "y.jsonl"
    p.write_text(
        '{"ts": "2026-04-28T10:00:00Z", "v": "before"}\n'
        '{"ts": "2026-04-29T10:00:00Z", "v": "in"}\n'
        '{"ts": "2026-04-30T10:00:00Z", "v": "after"}\n'
    )
    since = datetime(2026, 4, 29, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 4, 30, 0, 0, tzinfo=timezone.utc)
    rows = list(h.iter_jsonl_tolerant(p, since_utc=since, until_utc=until))
    assert [r["v"] for r in rows] == ["in"]


def test_iter_jsonl_tolerant_missing_file_returns_empty(tmp_path):
    rows = list(h.iter_jsonl_tolerant(tmp_path / "nope.jsonl"))
    assert rows == []


# ───────────────────────────────────────────────────────────── window math

def test_parse_window_uses_et_midnight():
    rd = datetime(2026, 4, 29, tzinfo=h.ET)
    start, end = h.parse_window(rd, days=1)
    # ET midnight = 04:00 UTC during EDT.
    assert start.tzinfo is timezone.utc
    assert start.hour == 4
    assert (end - start).days == 1


def test_yesterday_in_et_is_one_day_before_today():
    now = datetime(2026, 4, 29, 21, 30, tzinfo=timezone.utc)
    y = h.yesterday_in_et(now)
    # 21:30 UTC on Apr 29 = 17:30 ET → today_et is Apr 29 → yesterday is Apr 28
    assert y.date().isoformat() == "2026-04-28"
    assert y.tzinfo is h.ET


def test_last_sunday_in_et_includes_today():
    # 2026-04-26 is a Sunday. Test on Sunday and on Monday.
    sun = datetime(2026, 4, 26, 18, 0, tzinfo=timezone.utc)
    assert h.last_sunday_in_et(sun).date().isoformat() == "2026-04-26"
    mon = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    assert h.last_sunday_in_et(mon).date().isoformat() == "2026-04-26"
    sat = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
    assert h.last_sunday_in_et(sat).date().isoformat() == "2026-04-26"


# ───────────────────────────────────────────────────────── health pulse

def test_compute_health_pulse_returns_six_rows_with_required_axes(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "STATE_DIR", tmp_path)
    monkeypatch.setattr(h, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(h, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(h, "BOT_STATE_FILE", tmp_path / "bot_state.json")
    monkeypatch.setattr(h, "UNIVERSE_FILE", tmp_path / "universe.jsonl")
    monkeypatch.setattr(h, "DECISIONS_FILE", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(h, "PAPER_TRADES_FILE", tmp_path / "paper_trades.json")
    monkeypatch.setattr(h, "LOG_FILE", tmp_path / "bot.log")

    rows = h.compute_health_pulse(datetime(2026, 4, 29, 12, tzinfo=timezone.utc))
    assert len(rows) == 6
    assert [r["axis"] for r in rows] == [
        "Bot alive", "Scanner health", "Decisions volume", "Trades fired",
        "Telegram delivery", "Errors",
    ]
    # Empty state → bot dead, scanner empty (🚨), decisions zero (🚨), no trades (⚠️),
    # Telegram never-success warning (⚠️), zero errors (✅).
    statuses = [r["status"] for r in rows]
    assert statuses[0] == "🚨"  # no lock
    assert statuses[1] == "🚨"  # no scans
    assert statuses[2] == "🚨"  # no decisions
    assert statuses[4] == "⚠️"  # no Telegram success recorded yet
    assert statuses[5] == "✅"  # zero errors


def test_compute_health_pulse_alive_with_fresh_lock_and_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "STATE_DIR", tmp_path)
    monkeypatch.setattr(h, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(h, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(h, "BOT_STATE_FILE", tmp_path / "bot_state.json")
    monkeypatch.setattr(h, "UNIVERSE_FILE", tmp_path / "universe.jsonl")
    monkeypatch.setattr(h, "DECISIONS_FILE", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(h, "PAPER_TRADES_FILE", tmp_path / "paper_trades.json")
    monkeypatch.setattr(h, "LOG_FILE", tmp_path / "bot.log")

    # Lock points at our own PID so process_alive returns True.
    pid = os.getpid()
    (tmp_path / "bot.lock").write_text(str(pid))
    now = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
    (tmp_path / "bot_state.json").write_text(json.dumps({
        "last_heartbeat": (now - timedelta(seconds=15)).isoformat(),
    }))

    # Decisions: 2 of 100 accepted in last 24h.
    lines = []
    for i in range(98):
        lines.append(json.dumps({"ts": (now - timedelta(hours=1)).isoformat(),
                                  "decision": "reject", "reason": "x", "opp_type": "y"}))
    for i in range(2):
        lines.append(json.dumps({"ts": (now - timedelta(hours=1)).isoformat(),
                                  "decision": "accept", "reason": "x", "opp_type": "y"}))
    (tmp_path / "decisions.jsonl").write_text("\n".join(lines) + "\n")

    # Universe: 3 scans, 1500 rows each, none partial (cursor_rows median high → ✅).
    universe_rows = []
    for sid in range(3):
        for r in range(1500):
            universe_rows.append(json.dumps({
                "ts": (now - timedelta(hours=2)).isoformat(),
                "scan_id": f"S{sid}",
                "ticker": f"T{r}",
                "partial": False,
            }))
    (tmp_path / "universe.jsonl").write_text("\n".join(universe_rows) + "\n")

    rows = h.compute_health_pulse(now)
    assert rows[0]["status"] == "✅"
    assert rows[1]["status"] == "✅"
    assert rows[2]["status"] == "✅"  # 100 decisions
    # Trades fired: empty paper_trades file → ⚠️
    assert rows[3]["status"] == "⚠️"
    by_axis = {row["axis"]: row for row in rows}
    assert by_axis["Telegram delivery"]["status"] == "⚠️"
    assert by_axis["Errors"]["status"] == "✅"


# ───────────────────────────────────────────────────────── state file growth

def test_state_file_growth_uses_gzip_uncompressed_size_for_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "STATE_DIR", tmp_path)
    monkeypatch.setattr(h, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(h, "DECISIONS_FILE", tmp_path / "decisions.jsonl")

    (tmp_path / "decisions.jsonl").write_text("x" * 2_000_000)
    (tmp_path / "archive").mkdir()
    archive_path = tmp_path / "archive" / "decisions-2026-04-28.jsonl.gz"
    raw = b"y" * 1_000_000
    with gzip.open(archive_path, "wb") as f:
        f.write(raw)

    monkeypatch.setattr(h, "TRACKED_STATE_FILES", (
        ("decisions.jsonl", h.DECISIONS_FILE, "decisions"),
    ))

    week_end = datetime(2026, 4, 29, 12, tzinfo=timezone.utc)
    rows = h.state_file_growth(week_end, baseline_days=1)
    assert len(rows) == 1
    row = rows[0]
    # Current is 2MB, baseline (uncompressed) is 1MB → +1MB delta, +100% pct.
    assert row["baseline"] == 1_000_000
    assert row["current"] == 2_000_000
    assert "+" in row["delta_str"]
    assert "100%" in row["delta_str"]


def test_state_file_growth_no_baseline_when_archive_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "STATE_DIR", tmp_path)
    monkeypatch.setattr(h, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(h, "PAPER_TRADES_FILE", tmp_path / "paper_trades.json")
    (tmp_path / "paper_trades.json").write_text("[]")
    monkeypatch.setattr(h, "TRACKED_STATE_FILES", (
        ("paper_trades.json", h.PAPER_TRADES_FILE, None),
    ))
    rows = h.state_file_growth(datetime(2026, 4, 29, tzinfo=timezone.utc))
    assert rows[0]["delta_str"] == "(no baseline)"


# ───────────────────────────────────────────────────────── traceback tail

def test_tail_tracebacks_dedupes_by_signature(tmp_path):
    log = tmp_path / "bot.log"
    # bot.log timestamps are local ET — match the format helper expects.
    base = datetime(2026, 4, 29, 12, 0, 0)  # naive ET
    block_a = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in foo\n'
        "    raise ValueError(\"oops\")\n"
        "ValueError: oops\n"
    )
    block_b = (
        "Traceback (most recent call last):\n"
        '  File "y.py", line 2, in bar\n'
        "    raise KeyError(\"missing\")\n"
        "KeyError: 'missing'\n"
    )
    lines = []
    for i in range(3):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{ts}] [INFO] glint.x: scanning\n")
        for ln in block_a.splitlines():
            lines.append(f"[{ts}] [ERROR] glint.x: {ln}\n" if "Traceback" in ln else ln + "\n")
    ts = (base + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    for ln in block_b.splitlines():
        lines.append(f"[{ts}] [ERROR] glint.x: {ln}\n" if "Traceback" in ln else ln + "\n")
    log.write_text("".join(lines))

    since_utc = datetime(2026, 4, 29, 14, 0, tzinfo=timezone.utc)
    until_utc = datetime(2026, 4, 29, 18, 0, tzinfo=timezone.utc)
    sigs = h.tail_tracebacks(log, since_utc, until_utc)
    sig_to_count = {sig: count for sig, count, _ in sigs}
    # Two distinct signatures: ValueError ×3, KeyError ×1.
    val_keys = [s for s in sig_to_count if "ValueError" in s]
    key_keys = [s for s in sig_to_count if "KeyError" in s]
    assert val_keys and sig_to_count[val_keys[0]] == 3
    assert key_keys and sig_to_count[key_keys[0]] == 1


def test_tail_tracebacks_empty_when_no_log(tmp_path):
    sigs = h.tail_tracebacks(
        tmp_path / "nope.log",
        datetime(2026, 4, 29, tzinfo=timezone.utc),
        datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    assert sigs == []


# ───────────────────────────────────────────────────────── process_alive

def test_process_alive_false_when_lock_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "LOCK_FILE", tmp_path / "missing.lock")
    monkeypatch.setattr(h, "BOT_STATE_FILE", tmp_path / "missing.json")
    alive, reason = h.process_alive(datetime(2026, 4, 29, tzinfo=timezone.utc))
    assert not alive and "missing" in reason


def test_process_alive_false_when_pid_dead(tmp_path, monkeypatch):
    lock = tmp_path / "bot.lock"
    # Pick a PID very unlikely to exist (max+1).
    lock.write_text("99999999")
    monkeypatch.setattr(h, "LOCK_FILE", lock)
    monkeypatch.setattr(h, "BOT_STATE_FILE", tmp_path / "x.json")
    alive, reason = h.process_alive(datetime.now(timezone.utc))
    assert not alive and "not running" in reason


def test_process_alive_false_when_lock_stale(tmp_path, monkeypatch):
    lock = tmp_path / "bot.lock"
    lock.write_text(str(os.getpid()))
    # Set mtime to 5 min ago.
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp()
    os.utime(lock, (old, old))
    monkeypatch.setattr(h, "LOCK_FILE", lock)
    monkeypatch.setattr(h, "BOT_STATE_FILE", tmp_path / "x.json")
    alive, reason = h.process_alive(datetime.now(timezone.utc))
    assert not alive and "stale" in reason


# ───────────────────────────────────────────────────────────── _safe_section

def test_safe_section_returns_marker_on_exception():
    def raiser():
        raise KeyError("missing_key")
    body, reason = h._safe_section(raiser)
    assert "[section unavailable:" in body
    assert "KeyError" in body
    assert reason and "KeyError" in reason


def test_safe_section_passes_through_on_success():
    body, reason = h._safe_section(lambda: "# fine\nbody\n")
    assert body == "# fine\nbody\n"
    assert reason is None


def test_safe_section_marks_non_string_returns():
    body, reason = h._safe_section(lambda: 42)
    assert "[section unavailable:" in body
    assert reason == "non-string return"


# ─────────────────────────────────────────────────────── prior weekly report

def test_read_prior_weekly_report_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "REPORTS_DIR", tmp_path)
    week_end = datetime(2026, 4, 26, tzinfo=h.ET)
    assert h.read_prior_weekly_report(week_end) is None


def test_read_prior_weekly_report_finds_prior_iso_week(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "REPORTS_DIR", tmp_path)
    weekly_dir = tmp_path / "weekly"
    weekly_dir.mkdir()
    # 2026-04-19 (the Sunday before 2026-04-26) is in ISO week 16.
    prior_path = weekly_dir / "weekly_report_2026-W16.md"
    prior_path.write_text("# stub\n")
    week_end = datetime(2026, 4, 26, tzinfo=h.ET)
    assert h.read_prior_weekly_report(week_end) == "# stub\n"


def test_extract_headline_metric_first_line_only():
    md = "ignored\n| Decisions volume | 100 | ok |\n| Decisions volume | 200 | ok |\n"
    line = h.extract_headline_metric(md, r"\| Decisions volume \|")
    assert line and "100" in line and "200" not in line
