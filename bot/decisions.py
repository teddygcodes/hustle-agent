"""Per-decision audit log. Append-only JSONL; daily rotation via scheduler.

Session 6 (Apr 24 closed-loop data collection): every scan-time accept/reject
gets a row here with a gate fingerprint. No reader API — analysis tools
(`tools/cohort_report.py`) read the file directly.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from bot.config import BOT_STATE_DIR

DECISIONS_FILE = BOT_STATE_DIR / "decisions.jsonl"

_LOCK = threading.Lock()
_logger = logging.getLogger("glint.decisions")


def log_decision(
    ticker: str,
    opp_type: str,
    edge: float | None,
    gates: dict[str, bool],
    decision: str,
    reason: str,
    extra: dict | None = None,
) -> None:
    """Append one decision record to decisions.jsonl.

    Args:
        ticker: Kalshi market ticker.
        opp_type: Strategy name (vig_stack_series, live_momentum, etc.).
        edge: Edge in relative units (0.15 = 15%) at decision time, or None.
        gates: {gate_name: passed?} fingerprint. The gate that fired = False.
        decision: "accept" | "reject".
        reason: Short reason key (e.g. "low_liquidity", "all_gates_passed").
        extra: Optional extra context — kept small (avoid market-state dumps).

    Never raises. Failures are logged at ERROR but the caller continues.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "opp_type": opp_type,
        "edge": round(edge, 4) if edge is not None else None,
        "gates": gates,
        "decision": decision,
        "reason": reason,
    }
    if extra:
        record["extra"] = extra
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        with _LOCK:
            BOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(DECISIONS_FILE, "a") as f:
                f.write(line)
    except Exception:
        _logger.exception("decisions.log_decision failed for %s", ticker)
