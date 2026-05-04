#!/usr/bin/env python3
"""
Backfill `regime` field on existing records (Session 14).

Local-only, gitignored. Idempotent: records that already have `regime` are
skipped. Best-effort: historical records without close_ts get
event_horizon_hr=None; records with malformed/missing ts are left untouched.

Files processed:
  bot/state/decisions.jsonl   + bot/state/archive/decisions-*.jsonl.gz
  bot/state/predictions.jsonl + bot/state/archive/predictions-*.jsonl.gz
  bot/state/clv.json
  bot/state/positions.json
  bot/state/universe.jsonl    + bot/state/archive/universe-*.jsonl.gz

Usage:
  python3 tools/backfill_regime.py [--dry-run]

Safety:
  - Refuses to run if bot is alive (checks bot/state/bot.lock PID).
  - Atomic writes via temp + os.replace.
  - Each .jsonl.gz archive is rewritten in place atomically.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.regime import tag as regime_tag  # noqa: E402


def _try_tag(rec: dict) -> bool:
    """Tag rec in-place. Returns True if tagged or extended, False if skipped.

    Session 14 behavior: untagged records get a fresh regime dict.
    Session 34 extension: records with an older regime dict (missing
    `match_phase` or any future axis) get the missing keys added without
    touching pre-existing axes — preserves byte-identical legacy values.
    Records already carrying every REGIME_KEYS axis are skipped.
    """
    from bot.regime import REGIME_KEYS  # imported lazily to track schema growth

    existing = rec.get("regime") if isinstance(rec.get("regime"), dict) else None
    if existing is not None and all(k in existing for k in REGIME_KEYS):
        return False
    ts_iso = rec.get("ts") or rec.get("recorded_at") or rec.get("opened_at") or rec.get("entry_at")
    if not isinstance(ts_iso, str) or not ts_iso:
        return False
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return False
    ticker = rec.get("ticker") or ""
    try:
        new_regime = regime_tag(ts=ts, ticker=ticker, market_state=rec)
        if existing is None:
            rec["regime"] = new_regime
        else:
            for key, val in new_regime.items():
                if key not in existing:
                    existing[key] = val
        return True
    except (ValueError, TypeError):
        return False


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_gzip(path: Path, lines: list[str]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt") as f:
            f.writelines(lines)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backfill_jsonl(path: Path, dry_run: bool = False) -> int:
    """Tag a JSONL file in place. Preserves blank lines and unparseable lines.

    Returns count of records newly tagged or extended. Session 34: records
    with an older regime dict missing the new `match_phase` axis are extended
    in-place by `_try_tag`; records already carrying every REGIME_KEYS axis
    are skipped (counted as `skipped_complete`).
    """
    if not path.exists():
        return 0
    from bot.regime import REGIME_KEYS

    out_lines: list[str] = []
    tagged = 0
    skipped_complete = 0
    skipped_bad = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            out_lines.append(line + "\n")
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out_lines.append(line + "\n")
            skipped_bad += 1
            continue
        existing = rec.get("regime") if isinstance(rec.get("regime"), dict) else None
        if existing is not None and all(k in existing for k in REGIME_KEYS):
            skipped_complete += 1
            out_lines.append(json.dumps(rec, separators=(",", ":")) + "\n")
            continue
        if _try_tag(rec):
            tagged += 1
            out_lines.append(json.dumps(rec, separators=(",", ":")) + "\n")
        else:
            skipped_bad += 1
            out_lines.append(json.dumps(rec, separators=(",", ":")) + "\n")
    if not dry_run and tagged > 0:
        _atomic_write_text(path, "".join(out_lines))
    print(f"  {path.name}: tagged={tagged} skipped_complete={skipped_complete} skipped_bad={skipped_bad}")
    return tagged


def backfill_gz(path: Path, dry_run: bool = False) -> int:
    """Tag a .jsonl.gz archive in place. Same semantics as backfill_jsonl."""
    if not path.exists():
        return 0
    from bot.regime import REGIME_KEYS

    with gzip.open(path, "rt") as f:
        text = f.read()
    out_lines: list[str] = []
    tagged = 0
    for line in text.splitlines():
        if not line.strip():
            out_lines.append(line + "\n")
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out_lines.append(line + "\n")
            continue
        existing = rec.get("regime") if isinstance(rec.get("regime"), dict) else None
        if existing is not None and all(k in existing for k in REGIME_KEYS):
            out_lines.append(json.dumps(rec, separators=(",", ":")) + "\n")
            continue
        if _try_tag(rec):
            tagged += 1
        out_lines.append(json.dumps(rec, separators=(",", ":")) + "\n")
    if not dry_run and tagged > 0:
        _atomic_write_gzip(path, out_lines)
    print(f"  {path.name}: tagged={tagged}")
    return tagged


def backfill_json_array(path: Path, dry_run: bool = False) -> int:
    """Tag a JSON-array file (clv.json, positions.json) in place."""
    if not path.exists():
        return 0
    try:
        records = json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"  {path.name}: malformed JSON; skipping")
        return 0
    if not isinstance(records, list):
        print(f"  {path.name}: not a list; skipping")
        return 0
    tagged = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if _try_tag(rec):
            tagged += 1
    if not dry_run and tagged > 0:
        _atomic_write_text(path, json.dumps(records, indent=2, default=str))
    print(f"  {path.name}: tagged={tagged}")
    return tagged


def _bot_alive() -> bool:
    lock = ROOT / "bot" / "state" / "bot.lock"
    if not lock.exists():
        return False
    try:
        pid = int(lock.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill regime field on existing records.")
    ap.add_argument("--dry-run", action="store_true", help="Report counts without writing.")
    args = ap.parse_args()

    if _bot_alive() and not args.dry_run:
        print("ERROR: bot.lock indicates a live process. Stop the bot before backfilling.", file=sys.stderr)
        return 2

    state = ROOT / "bot" / "state"
    archive = state / "archive"
    print(f"{'DRY-RUN: ' if args.dry_run else ''}Backfilling regime in {state} ...")

    total = 0
    total += backfill_jsonl(state / "decisions.jsonl", args.dry_run)
    total += backfill_jsonl(state / "predictions.jsonl", args.dry_run)
    total += backfill_jsonl(state / "universe.jsonl", args.dry_run)
    total += backfill_json_array(state / "clv.json", args.dry_run)
    total += backfill_json_array(state / "positions.json", args.dry_run)

    # Per spec: only the writer-attached archives. live_ticks is excluded —
    # those rows are NOT regime-tagged at write time, so backfilling them
    # would create a gap going forward (write-time tags vs backfill-only tags).
    if archive.exists():
        for prefix in ("decisions-", "predictions-", "universe-"):
            for gz in sorted(archive.glob(f"{prefix}*.jsonl.gz")):
                total += backfill_gz(gz, args.dry_run)

    print(f"\nTotal records tagged: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
