#!/usr/bin/env python3
"""
One-shot backfill: extend mfe_cents on existing settled clv records to
include the settlement event (Session 16).

Pre-Session-16 settled records have mfe_cents capped at the highest *observed
bid* during open life — for winners that's typically 1-2¢ below the
settlement payout (yes_bid never quite reaches 100, no_bid never quite
reaches 100), producing a structural -1¢ gap in tools/excursion_report.py.

This script ratchets each settled record's mfe_cents (and mfe_at if changed)
to max(existing, clv_cents), clamped to ≥ 0 (MFE convention is non-negative
magnitude). Matches the new live behavior in bot/clv.py:check_clv_settlements
so post-deploy live records and pre-deploy backfilled records are shape-
identical.

For losers (clv_cents < 0), the ratchet is a no-op since existing mfe_cents
≥ 0 > clv_cents. Only winners get extended.

Idempotent: re-running after the first pass does nothing.
Safety: stops if the bot is running; backs up clv.json before writing.

Usage:
    cd hustle-agent
    python3 tools/backfill_extended_mfe.py --yes
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import CLV_FILE, BOT_STATE_DIR
from bot.clv import _save


def _bot_running() -> int | None:
    lock = BOT_STATE_DIR / "bot.lock"
    if not lock.exists():
        return None
    try:
        pid = int(lock.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, PermissionError):
        return None


def _extend_one(rec: dict) -> bool:
    """Return True if rec was modified."""
    if rec.get("status") != "settled":
        return False
    existing_mfe = rec.get("mfe_cents")
    clv_cents = rec.get("clv_cents")
    if existing_mfe is None or clv_cents is None:
        return False
    try:
        target = max(0, int(round(float(clv_cents))))
    except (TypeError, ValueError):
        return False
    if target <= existing_mfe:
        return False
    rec["mfe_cents"] = target
    rec["mfe_at"] = rec.get("settled_at") or datetime.now(timezone.utc).isoformat()
    return True


def main():
    if "--yes" not in sys.argv:
        print("Refusing to run without --yes flag.")
        print(f"Will extend mfe_cents on settled records in: {CLV_FILE}")
        sys.exit(1)

    pid = _bot_running()
    if pid:
        print(f"ABORT: bot is running (PID {pid}). Stop it first.")
        sys.exit(2)

    if not CLV_FILE.exists():
        print(f"No clv.json at {CLV_FILE} — nothing to do.")
        return

    raw = json.loads(CLV_FILE.read_text())
    if not isinstance(raw, list):
        print("clv.json is not a list — refusing to touch.")
        sys.exit(3)

    settled = [r for r in raw if isinstance(r, dict) and r.get("status") == "settled"]
    candidates = [
        r for r in settled
        if r.get("mfe_cents") is not None and r.get("clv_cents") is not None
    ]
    print(f"clv.json: {len(raw)} records, {len(settled)} settled, "
          f"{len(candidates)} with mfe_cents+clv_cents.")

    diffs: list[tuple[str, int, int]] = []
    for rec in raw:
        before = rec.get("mfe_cents") if isinstance(rec, dict) else None
        if isinstance(rec, dict) and _extend_one(rec):
            diffs.append((rec.get("ticker", "?"), before, rec["mfe_cents"]))

    if not diffs:
        print("No records to extend (already backfilled, or all losers).")
        return

    backup = CLV_FILE.with_name(f"clv.json.bak-{date.today().strftime('%Y%m%d')}")
    shutil.copy2(CLV_FILE, backup)
    print(f"Backup written: {backup}")

    _save(raw)

    print(f"\nExtended {len(diffs)} records:")
    for ticker, before, after in diffs:
        print(f"  {ticker:40} mfe_cents {before} -> {after}")


if __name__ == "__main__":
    main()
