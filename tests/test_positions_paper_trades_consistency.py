"""Production-state consistency guard for positions.json vs paper_trades.json.

Session 70: replaced naive read with mtime-fence helper. Bot writes each file
atomically via bot/state_io.py (tmpfile + rename), but the cross-file write
sequence (positions.json then paper_trades.json) is NOT atomic. Two reads
microseconds apart could land on either side of a mid-write boundary and see
an inconsistent snapshot. The fence retries until both files' mtimes match
across the read window.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _read_stable_snapshot(positions_path: Path, trades_path: Path,
                          max_attempts: int = 20,
                          settle_delay_secs: float = 0.05) -> tuple[list, list]:
    """Read both files such that neither was modified during the read window.

    Bot writes each file atomically (tmpfile + rename via bot/state_io.py),
    so individual file reads are consistent. Only cross-file race exists
    (bot writes positions and paper_trades in sequence, not atomically).

    Mtime-fence: pre-stat both files, read both, post-stat both. If neither
    mtime changed during the read window, the snapshot is consistent.

    Bounded by max_attempts × settle_delay_secs (default 1s total). With bot
    writes at ~30s scan cadence, finding a stable window in 1s is virtually
    guaranteed in steady state.
    """
    for attempt in range(max_attempts):
        pos_mt_1 = positions_path.stat().st_mtime
        trd_mt_1 = trades_path.stat().st_mtime
        positions = json.loads(positions_path.read_text())
        trades = json.loads(trades_path.read_text())
        pos_mt_2 = positions_path.stat().st_mtime
        trd_mt_2 = trades_path.stat().st_mtime
        if pos_mt_1 == pos_mt_2 and trd_mt_1 == trd_mt_2:
            return positions, trades
        time.sleep(settle_delay_secs)
    raise RuntimeError(
        f"Could not get stable snapshot of positions.json + paper_trades.json "
        f"after {max_attempts} attempts ({max_attempts * settle_delay_secs:.1f}s). "
        f"Bot is writing too fast or filesystem is misbehaving."
    )


def test_paper_trades_open_count_matches_positions_active():
    positions, trades = _read_stable_snapshot(
        REPO_ROOT / "bot/state/positions.json",
        REPO_ROOT / "bot/state/paper_trades.json",
    )

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


# ---------------------------------------------------------------------------
# Session 70 regression tests for _read_stable_snapshot.
#
# These lock the helper's contract so future regressions to the fence logic
# surface immediately. They use tmp_path fixtures and synthetic file churn,
# so they're independent of bot state and run in CI / locally without the
# bot.
# ---------------------------------------------------------------------------


def test_stable_snapshot_succeeds_on_quiet_files(tmp_path):
    """Helper returns on first attempt and contents match when files are quiet."""
    positions_path = tmp_path / "positions.json"
    trades_path = tmp_path / "paper_trades.json"
    positions_data = [{"ticker": "KX-A", "filled": 1, "status": "filled"}]
    trades_data = [{"ticker": "KX-A", "status": "open"}]
    positions_path.write_text(json.dumps(positions_data))
    trades_path.write_text(json.dumps(trades_data))

    positions, trades = _read_stable_snapshot(positions_path, trades_path)

    assert positions == positions_data
    assert trades == trades_data


def _bump_mtime(path: Path) -> None:
    """Atomically advance a file's mtime without touching contents.

    Mirrors the bot's atomic write semantics (tmpfile + rename) from a reader's
    perspective: contents stay valid, only mtime changes. Using write_text in
    a churner would truncate-then-write, which is NOT atomic and surfaces as
    JSONDecodeError instead of mtime mismatch — that's a different bug class
    than the cross-file race the helper is designed to handle.
    """
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 0.001))


def test_stable_snapshot_retries_when_files_modified_mid_read(tmp_path):
    """Helper retries when an atomic writer bumps mtime briefly, then succeeds."""
    positions_path = tmp_path / "positions.json"
    trades_path = tmp_path / "paper_trades.json"
    quiet_positions = [{"ticker": "KX-A", "filled": 1, "status": "filled"}]
    quiet_trades = [{"ticker": "KX-A", "status": "open"}]
    positions_path.write_text(json.dumps(quiet_positions))
    trades_path.write_text(json.dumps(quiet_trades))

    # Force the first few attempts to see an mtime mismatch by bumping mtime
    # via a stat wrapper that returns a different mtime on the post-read stat
    # for the first 2 attempts. Deterministic — no thread timing dependency.
    real_stat = Path.stat
    state = {"calls": 0}

    def flaky_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        # Calls come in pairs of (positions, trades) per pre/post phase. For
        # attempts 1-2 we bump mtime in the post-read window so the helper
        # sees a mismatch and retries. From attempt 3 onward we return real
        # mtimes (which haven't actually changed) so the helper succeeds.
        state["calls"] += 1
        # Each attempt does 4 stat calls (pos pre, trd pre, pos post, trd post).
        # Bump trades mtime on the post-read trades stat (call #4, #8) for the
        # first two attempts (calls 4, 8). After call 8, return real values.
        if state["calls"] in (4, 8) and self == trades_path:
            # Return a stat with a bumped mtime to force mismatch.
            return os.stat_result((
                result.st_mode, result.st_ino, result.st_dev, result.st_nlink,
                result.st_uid, result.st_gid, result.st_size,
                result.st_atime, result.st_mtime + 1.0, result.st_ctime,
            ))
        return result

    Path.stat = flaky_stat
    try:
        positions, trades = _read_stable_snapshot(
            positions_path, trades_path,
            max_attempts=20, settle_delay_secs=0.001,
        )
    finally:
        Path.stat = real_stat

    assert state["calls"] >= 8, "helper should have retried at least twice"
    assert positions == quiet_positions
    assert trades == quiet_trades


def test_stable_snapshot_raises_after_max_attempts_when_constantly_modified(tmp_path):
    """Helper raises RuntimeError cleanly when mtime always differs across the read window."""
    positions_path = tmp_path / "positions.json"
    trades_path = tmp_path / "paper_trades.json"
    positions_path.write_text(json.dumps([{"ticker": "KX-A"}]))
    trades_path.write_text(json.dumps([{"ticker": "KX-A"}]))

    real_stat = Path.stat
    counter = {"n": 0}

    def always_advancing_stat(self, *args, **kwargs):
        # Every stat call returns a strictly increasing mtime, guaranteeing
        # the post-read mtime never matches the pre-read mtime. Independent
        # of wall-clock timing, so deterministic.
        result = real_stat(self, *args, **kwargs)
        counter["n"] += 1
        return os.stat_result((
            result.st_mode, result.st_ino, result.st_dev, result.st_nlink,
            result.st_uid, result.st_gid, result.st_size,
            result.st_atime, result.st_mtime + counter["n"], result.st_ctime,
        ))

    Path.stat = always_advancing_stat
    try:
        with pytest.raises(RuntimeError) as exc_info:
            _read_stable_snapshot(
                positions_path, trades_path,
                max_attempts=5, settle_delay_secs=0.001,
            )
    finally:
        Path.stat = real_stat

    msg = str(exc_info.value)
    assert "stable snapshot" in msg
    assert "5 attempts" in msg
