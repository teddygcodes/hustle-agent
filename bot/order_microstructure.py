"""Per-order microstructure capture for live Kalshi orders. Append-only JSONL;
daily rotation via scheduler. Paper-mode codepath does NOT call this module —
records here are LIVE-ORDER ONLY (Session 15, Apr 25+).

Sign convention (signed slippage):
    slippage_cents = filled_price_cents - requested_price_cents
For YES buys: positive = filled higher = paid more = adverse.
For NO buys:  positive = filled higher = paid more = adverse.
Convention: "positive slippage = adverse to the trader" across both sides.

slippage_source enum:
  - "limit_price_echo" (v1): Kalshi's place_order SDK return computes
    cost_dollars = round(filled * price_cents / 100.0, 2) — it echoes the
    limit price, not a true VWAP. So slippage will read 0 in production
    until v2 wires the /portfolio/fills endpoint.
  - "fills_endpoint" (v2, future): true VWAP from /portfolio/fills.
  - "none": synchronous rejection / never-filled.

v1 known gaps:
  - Bot crash between place_order and terminal observation: that order's
    record is lost (the in-memory _PENDING dict is process-local). Acceptable
    at tens-to-hundreds of orders/day.
  - Orders canceled by Kalshi's matching engine and pruned from get_order
    return {"error": ...}; check_fills swallows the error today, so we
    never observe a terminal status for those orders. v1 punts.

Never raises — failures logged at ERROR, caller continues.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from bot.config import BOT_STATE_DIR

MICROSTRUCTURE_FILE = BOT_STATE_DIR / "order_microstructure.jsonl"

_LOCK = threading.Lock()
_logger = logging.getLogger("glint.microstructure")

# In-memory placement registry. Key: kalshi_order_id. Lifetime: place_order
# success → terminal observation in check_fills. Lost on process restart.
_PENDING: dict[str, dict] = {}


def _append_record(record: dict) -> None:
    """Atomic append + never-raises. Mirrors bot/decisions.py."""
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        with _LOCK:
            BOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(MICROSTRUCTURE_FILE, "a") as f:
                f.write(line)
    except Exception:
        _logger.exception("microstructure._append_record failed")
