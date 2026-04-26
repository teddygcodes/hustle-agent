"""Per-call cadence log for tracker.update_positions. Append-only JSONL; daily
rotation via scheduler.

Session 17 (Apr 26 — tracker cadence audit): instruments every update_positions
call so we can verify the new _position_check_loop fires at ~30s and confirm
that fixing cadence resolves the 90% None-ticks problem on live_momentum
positions. No reader API — analysis is ad-hoc via shell.

Schema: {ts, num_open_positions, ms_since_last_call, called_from}.
ms_since_last_call is per-call-site (keyed by called_from) so we can compare
_main_loop vs _position_check_loop cadences side by side.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from bot.config import BOT_STATE_DIR

CADENCE_FILE = BOT_STATE_DIR / "tracker_cadence.jsonl"

_LOCK = threading.Lock()
_logger = logging.getLogger("nexus.tracker_cadence")
_last_call_ts: dict[str, datetime] = {}


def log_cadence(num_open_positions: int, called_from: str) -> None:
    """Append one cadence record to tracker_cadence.jsonl.

    Args:
        num_open_positions: Count of positions in (filled, partial) status at
            the start of update_positions. Used to size the per-call work.
        called_from: Identifier for the caller — "_main_loop",
            "_position_check_loop", or "unspecified" for legacy/test paths.

    Never raises. Failures are logged at ERROR but the caller continues.
    """
    now = datetime.now(timezone.utc)
    last = _last_call_ts.get(called_from)
    ms_since_last_call: int | None = None
    if last is not None:
        ms_since_last_call = int((now - last).total_seconds() * 1000)
    _last_call_ts[called_from] = now

    record = {
        "ts": now.isoformat(),
        "num_open_positions": int(num_open_positions),
        "ms_since_last_call": ms_since_last_call,
        "called_from": called_from,
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        with _LOCK:
            BOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(CADENCE_FILE, "a") as f:
                f.write(line)
    except Exception:
        _logger.exception("tracker_cadence.log_cadence failed (called_from=%s)", called_from)
