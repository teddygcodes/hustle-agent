#!/usr/bin/env python3
"""
One-shot purge of disabled-strategy records from clv.json.

Session 5 (Apr 23 redemption plan): clv.json had 236/448 records (53%) for
strategies that have been disabled (series_game_edge, weather, btc/eth/sol/
xrp/doge/bnb_price_edge, ipl_game_edge, live_latency_arb). The runtime filter
in bot/clv.py:_load() now drops these on every read, but the file on disk
still carries them. This script removes them once.

Stop the bot before running. Backs up clv.json first.

Usage:
    cd hustle-agent
    python3 tools/purge_clv_disabled.py --yes
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import CLV_FILE, BOT_STATE_DIR
from bot.clv import _active_strategies, _save


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
        print(f"Will purge disabled-strategy records from: {CLV_FILE}")
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
        print(f"clv.json is not a list — refusing to touch.")
        sys.exit(3)

    before = len(raw)
    active = _active_strategies()
    kept = [r for r in raw if r.get("opp_type") in active]
    dropped = before - len(kept)

    if dropped == 0:
        print(f"All {before} records are active-strategy. Nothing to drop.")
        return

    backup = CLV_FILE.with_name(f"clv.json.bak-{date.today().strftime('%Y%m%d')}")
    shutil.copy2(CLV_FILE, backup)
    print(f"Backup written: {backup}")

    _save(kept)

    by_type: dict[str, int] = {}
    for r in raw:
        if r.get("opp_type") not in active:
            t = r.get("opp_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1

    print(f"\nDropped {dropped}, kept {len(kept)} (was {before}).")
    print("Per-strategy drop:")
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c}")
    print(f"\nActive set: {sorted(active)}")


if __name__ == "__main__":
    main()
