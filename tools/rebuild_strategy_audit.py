#!/usr/bin/env python3
"""
One-shot rebuild of strategy_audit.json settlement_log + rollups from
paper_trades.json ground truth. Also rebuilds patterns.json.

Stop the bot before running. Backs up the current audit file first.

Usage:
    cd hustle-agent
    python3 tools/rebuild_strategy_audit.py
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import PAPER_TRADES_FILE, BOT_STATE_DIR
from bot.state_io import load_json
from bot import tracker, patterns

AUDIT_PATH = BOT_STATE_DIR / "strategy_audit.json"


def backup_audit() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    bak_path = AUDIT_PATH.with_suffix(f".json.bak-{stamp}")
    shutil.copy2(AUDIT_PATH, bak_path)
    return bak_path


def reset_rollups(audit: dict) -> None:
    audit["settlement_log"] = []
    for s in audit.get("strategies", {}).values():
        s["real_trades"] = 0
        s["real_pnl"] = 0.0
        s["real_wins"] = 0
        s["real_losses"] = 0
        s["real_wr"] = "0%"


def main() -> int:
    if not AUDIT_PATH.exists():
        print(f"audit file not found: {AUDIT_PATH}", file=sys.stderr)
        return 1

    paper_trades = load_json(PAPER_TRADES_FILE)
    if not isinstance(paper_trades, list):
        print("paper_trades.json is not a list", file=sys.stderr)
        return 1

    resolved = [
        t for t in paper_trades
        if isinstance(t, dict)
        and t.get("status") in ("won", "lost", "exited_early")
        and (t.get("contracts") or t.get("filled") or 0) > 0
    ]
    print(f"Paper trades: {len(paper_trades)} total, {len(resolved)} resolved")

    bak = backup_audit()
    print(f"Backed up audit → {bak.name}")

    audit = json.loads(AUDIT_PATH.read_text())
    reset_rollups(audit)
    AUDIT_PATH.write_text(json.dumps(audit, indent=2))

    appended = 0
    skipped = 0
    for t in resolved:
        if tracker.log_settlement(t):
            appended += 1
        else:
            skipped += 1

    # Re-read to get final state
    audit = json.loads(AUDIT_PATH.read_text())
    log_len = len(audit.get("settlement_log", []))
    rollup_sum = sum(s.get("real_trades", 0) for s in audit.get("strategies", {}).values())

    print(f"\nappended: {appended}, skipped_dup: {skipped}")
    print(f"settlement_log length: {log_len}")
    print(f"rollup_sum: {rollup_sum}")
    print("\nPer-strategy totals:")
    for name, s in audit.get("strategies", {}).items():
        if s.get("real_trades", 0) > 0:
            print(f"  {name}: {s['real_trades']} trades, "
                  f"${s.get('real_pnl', 0):+.2f}, {s.get('real_wr', '0%')}")

    # Rebuild patterns.json
    patterns.record_resolution({})
    pj = load_json(Path(AUDIT_PATH.parent / "patterns.json"))
    print(f"\npatterns.json total_resolved: {pj.get('total_resolved') if isinstance(pj, dict) else 'err'}")

    if not (len(resolved) == log_len == rollup_sum):
        print(f"\nWARNING: invariant broken — paper={len(resolved)} log={log_len} rollup={rollup_sum}",
              file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
