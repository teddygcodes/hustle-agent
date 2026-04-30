"""Backfill safety tests for tools/backfill_regime.py (Session 14).

Covers idempotency (no double-tag), --dry-run (no writes), partial-record
tolerance, and gzipped archive support.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backfill_regime import backfill_jsonl, backfill_json_array, backfill_gz  # noqa: E402


def test_backfill_jsonl_is_idempotent(tmp_path):
    p = tmp_path / "decisions.jsonl"
    p.write_text(
        json.dumps({"ts": "2026-04-25T12:00:00+00:00", "ticker": "KXNBAGAME-X"}) + "\n"
    )
    n1 = backfill_jsonl(p)
    first = json.loads(p.read_text().strip())
    n2 = backfill_jsonl(p)
    second = json.loads(p.read_text().strip())
    assert n1 == 1 and n2 == 0
    assert first == second
    assert "regime" in first


def test_backfill_extends_records_with_partial_regime(tmp_path):
    """Session 34: a record with an incomplete regime dict gets extended in
    place — the existing key value is preserved byte-identical, missing
    REGIME_KEYS are added."""
    p = tmp_path / "decisions.jsonl"
    existing = {
        "ts": "2026-04-25T12:00:00+00:00",
        "ticker": "KXNBAGAME-X",
        "regime": {"time_of_day": "afternoon"},  # 1 of 5 keys
    }
    p.write_text(json.dumps(existing) + "\n")
    n = backfill_jsonl(p)
    assert n == 1
    out = json.loads(p.read_text().strip())["regime"]
    assert out["time_of_day"] == "afternoon"  # preserved byte-identical
    for key in ("day_of_week", "sport_phase", "event_horizon_hr", "match_phase"):
        assert key in out


def test_backfill_skips_records_with_complete_regime(tmp_path):
    """Session 34: when all REGIME_KEYS are present, backfill is a no-op."""
    from bot.regime import REGIME_KEYS
    p = tmp_path / "decisions.jsonl"
    complete_regime = {key: None for key in REGIME_KEYS}  # all 5 keys
    complete_regime["time_of_day"] = "afternoon"  # at least one non-None
    existing = {
        "ts": "2026-04-25T12:00:00+00:00",
        "ticker": "KXNBAGAME-X",
        "regime": complete_regime,
    }
    p.write_text(json.dumps(existing) + "\n")
    n = backfill_jsonl(p)
    assert n == 0
    assert json.loads(p.read_text().strip())["regime"] == complete_regime


def test_backfill_dry_run_does_not_write(tmp_path):
    p = tmp_path / "decisions.jsonl"
    raw = json.dumps({"ts": "2026-04-25T12:00:00+00:00", "ticker": "KXNBAGAME-X"}) + "\n"
    p.write_text(raw)
    n = backfill_jsonl(p, dry_run=True)
    assert n == 1  # would-have-tagged count
    assert p.read_text() == raw  # unchanged


def test_backfill_handles_partial_record_missing_ts(tmp_path):
    """Records missing ts/recorded_at can't be tagged — they're preserved
    in place (untouched) and the rest of the file still gets tagged."""
    p = tmp_path / "decisions.jsonl"
    p.write_text(
        json.dumps({"ticker": "KXNBAGAME-X"}) + "\n"
        + json.dumps({"ts": "2026-04-25T12:00:00+00:00", "ticker": "KXNBAGAME-Y"}) + "\n"
    )
    n = backfill_jsonl(p)
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    assert n == 1
    assert "regime" not in rows[0]  # bad row preserved without tagging
    assert "regime" in rows[1]


def test_backfill_handles_gzipped_archive(tmp_path):
    p = tmp_path / "decisions-2026-04-24.jsonl.gz"
    raw = json.dumps({"ts": "2026-04-24T12:00:00+00:00", "ticker": "KXMLBGAME-X"}) + "\n"
    with gzip.open(p, "wt") as f:
        f.write(raw)
    n = backfill_gz(p)
    assert n == 1
    with gzip.open(p, "rt") as f:
        row = json.loads(f.read().strip())
    assert "regime" in row


def test_backfill_json_array_extends_partial_regime(tmp_path):
    """clv.json / positions.json store records as a JSON array. Session 34:
    backfill_json_array extends every dict that's missing any REGIME_KEY,
    preserving non-regime fields and any pre-existing regime sub-keys."""
    p = tmp_path / "clv.json"
    p.write_text(json.dumps([
        {"ticker": "KXNBAGAME-X", "recorded_at": "2026-04-25T12:00:00+00:00"},
        {"ticker": "KXMLBGAME-Y", "recorded_at": "2026-04-25T13:00:00+00:00",
         "regime": {"already": "tagged"}},
    ]))
    n = backfill_json_array(p)
    records = json.loads(p.read_text())
    assert n == 2
    assert "regime" in records[0]
    assert "regime" in records[1]
    # Pre-existing regime sub-key preserved alongside the new REGIME_KEYS
    assert records[1]["regime"]["already"] == "tagged"
    for key in ("time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase"):
        assert key in records[1]["regime"]


def test_backfill_jsonl_handles_blank_lines(tmp_path):
    """Trailing or interspersed blank lines must be preserved unchanged."""
    p = tmp_path / "decisions.jsonl"
    p.write_text(
        json.dumps({"ts": "2026-04-25T12:00:00+00:00", "ticker": "KXNBAGAME-X"}) + "\n"
        + "\n"
        + json.dumps({"ts": "2026-04-25T13:00:00+00:00", "ticker": "KXNBAGAME-Y"}) + "\n"
    )
    n = backfill_jsonl(p)
    assert n == 2
    lines = p.read_text().splitlines()
    # 3 lines total: 2 records + 1 blank kept positionally
    assert len([l for l in lines if l.strip()]) == 2
    rows = [json.loads(l) for l in lines if l.strip()]
    assert all("regime" in r for r in rows)
