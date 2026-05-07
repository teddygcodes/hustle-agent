"""Production-state consistency guard for positions.json vs paper_trades.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def test_paper_trades_open_count_matches_positions_active():
    positions = json.loads((REPO_ROOT / "bot/state/positions.json").read_text())
    trades = json.loads((REPO_ROOT / "bot/state/paper_trades.json").read_text())

    active_positions = {
        p["ticker"]
        for p in positions
        if (
            isinstance(p, dict)
            and p.get("filled", 0) > 0
            and p.get("status") in ("filled", "partial")
            and p.get("ticker")
        )
    }
    open_trades = {
        t["ticker"]
        for t in trades
        if isinstance(t, dict) and t.get("status") == "open" and t.get("ticker")
    }

    assert open_trades == active_positions, (
        "paper_trades open rows must match active positions; "
        f"trades_only={sorted(open_trades - active_positions)} "
        f"positions_only={sorted(active_positions - open_trades)}"
    )
