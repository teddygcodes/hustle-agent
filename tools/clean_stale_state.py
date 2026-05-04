#!/usr/bin/env python3
"""
One-shot deletion of confirmed-stale state files.

Session 5 (Apr 23 redemption plan): bot/state/ accumulated zombie files
that no longer have writers or readers. Verified by code search before
deletion. The Apr 20 .bak (Session 1 explicit backup) is preserved.

Stop the bot before running.

Usage:
    cd hustle-agent
    python3 tools/clean_stale_state.py --yes
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import BOT_STATE_DIR

# Files confirmed stale by Session 5 audit (Apr 23):
#   - odds_snapshots.json: last write Apr 8, no active reader using it
#   - price_cache.json: last write Apr 8, supplanted by in-process cache
#   - watchlist.json: 3 bytes (empty), last touched Apr 5
#   - paper_trades_archive.json: no code reads or writes it (rotation retired)
#   - strategy_audit.json.bak-1776538205: Apr 18 leftover from Session 1 dev
#   - strategy_audit.json.bak-1776539380: Apr 18 leftover from Session 1 dev
STALE_FILES = [
    "odds_snapshots.json",
    "price_cache.json",
    "watchlist.json",
    "paper_trades_archive.json",
    "strategy_audit.json.bak-1776538205",
    "strategy_audit.json.bak-1776539380",
]

# Explicitly KEEP — Session 1 backup, cited in CLAUDE.md
KEEP_BAK = "strategy_audit.json.bak-20260421"


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


def main():
    if "--yes" not in sys.argv:
        print("Refusing to run without --yes flag.")
        print("Will delete from", BOT_STATE_DIR, ":")
        for name in STALE_FILES:
            path = BOT_STATE_DIR / name
            if path.exists():
                print(f"  - {name} ({path.stat().st_size} bytes)")
            else:
                print(f"  - {name} (already gone)")
        print(f"Will KEEP: {KEEP_BAK}")
        sys.exit(1)

    pid = _bot_running()
    if pid:
        print(f"ABORT: bot is running (PID {pid}). Stop it first.")
        sys.exit(2)

    keep_path = BOT_STATE_DIR / KEEP_BAK
    if keep_path.exists():
        print(f"OK: keep {KEEP_BAK} present.")

    freed_bytes = 0
    deleted = 0
    for name in STALE_FILES:
        path = BOT_STATE_DIR / name
        if not path.exists():
            continue
        size = path.stat().st_size
        path.unlink()
        freed_bytes += size
        deleted += 1
        print(f"  rm {name} ({size} bytes)")

    print(f"\nDeleted {deleted} files, freed {freed_bytes:,} bytes ({freed_bytes/1024:.1f} KB).")


if __name__ == "__main__":
    main()
