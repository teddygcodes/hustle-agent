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
from bot.regime import tag as regime_tag

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


def _regime_for(ts: datetime, ticker: str, market_close_ts: str | None) -> dict | None:
    try:
        return regime_tag(
            ts=ts, ticker=ticker,
            market_state={"close_ts": market_close_ts} if market_close_ts else None,
        )
    except Exception:
        _logger.exception("microstructure: regime_tag failed for %s", ticker)
        return None


def record_placement(
    order_id: str,
    opportunity: dict,
    requested_price_cents: int,
    requested_qty: int,
    side: str,
    ts_placed: datetime,
    queue_depth_at_place: int | None = None,
) -> None:
    """Stash placement info in _PENDING for terminal-time write. Never raises."""
    try:
        opp_type = opportunity.get("opp_type") or opportunity.get("type") or "unknown"
        market = opportunity.get("market") if isinstance(opportunity.get("market"), dict) else {}
        with _LOCK:
            _PENDING[order_id] = {
                "ts_placed": ts_placed,
                "requested_price_cents": int(requested_price_cents),
                "requested_qty": int(requested_qty),
                "side": side,
                "opp_type": opp_type,
                "ticker": opportunity.get("ticker", ""),
                "queue_depth_at_place": queue_depth_at_place,
                "market_close_ts": market.get("close_ts") or market.get("close_time"),
                "partial_fill_count": 0,
                "last_filled_count": 0,
            }
    except Exception:
        _logger.exception("record_placement failed for order_id=%s", order_id)


def observe_fill_progress(order_id: str, filled_count: int) -> None:
    """Increment partial_fill_count when a non-terminal fill grows the count.
    Called from check_fills between placement and terminal status. Never raises.
    """
    try:
        with _LOCK:
            entry = _PENDING.get(order_id)
            if entry is None:
                return
            if filled_count > entry["last_filled_count"]:
                entry["partial_fill_count"] += 1
                entry["last_filled_count"] = filled_count
    except Exception:
        _logger.exception("observe_fill_progress failed for order_id=%s", order_id)


def record_terminal(
    order_id: str,
    kalshi_status: str,
    filled_count: int,
    cost_dollars: float | None,
    ts_terminal: datetime,
) -> None:
    """Pop from _PENDING, build full row, append. Never raises.

    kalshi_status: from get_order return; one of 'filled', 'canceled',
    'expired', 'rejected', or any other Kalshi status string.
    """
    try:
        with _LOCK:
            entry = _PENDING.pop(order_id, None)
        if entry is None:
            # Placement we never saw (pre-Session-15 / paper / restart).
            return
        # partial_fill_count tracks INTERMEDIATE partial observations only.
        # The terminal observation itself isn't a "partial" — caller should
        # have invoked observe_fill_progress for any pre-terminal increments.
        # Derive filled_price_cents from cost_dollars echoed by Kalshi SDK.
        # See module docstring re: slippage_source = "limit_price_echo".
        filled_price_cents: float | None = None
        if filled_count > 0 and cost_dollars is not None:
            filled_price_cents = round((cost_dollars / filled_count) * 100, 2)
        slippage_cents: float | None = None
        slippage_source = "none"
        if filled_price_cents is not None:
            slippage_cents = round(
                filled_price_cents - entry["requested_price_cents"], 2
            )
            slippage_source = "limit_price_echo"
        # Map Kalshi status → our terminal_status.
        if kalshi_status == "filled" or filled_count >= entry["requested_qty"]:
            terminal_status = "filled"
            ts_filled = ts_terminal
            ts_canceled = None
        elif kalshi_status in ("canceled", "expired"):
            terminal_status = "canceled"
            ts_filled = None
            ts_canceled = ts_terminal
        elif kalshi_status == "rejected":
            terminal_status = "rejected"
            ts_filled = None
            ts_canceled = None
        else:
            # Unknown statuses → canceled, to avoid misclassifying as filled.
            terminal_status = "canceled"
            ts_filled = None
            ts_canceled = ts_terminal
        latency_ms = round(
            (ts_terminal - entry["ts_placed"]).total_seconds() * 1000
        )
        record = {
            "ts_placed": entry["ts_placed"].isoformat(),
            "ts_filled": ts_filled.isoformat() if ts_filled else None,
            "ts_canceled": ts_canceled.isoformat() if ts_canceled else None,
            "requested_price_cents": entry["requested_price_cents"],
            "filled_price_cents": filled_price_cents,
            "requested_qty": entry["requested_qty"],
            "filled_qty": filled_count,
            "order_type": "limit",
            "slippage_cents": slippage_cents,
            "slippage_source": slippage_source,
            "latency_ms": latency_ms,
            "queue_depth_at_place": entry["queue_depth_at_place"],
            "partial_fill_count": entry["partial_fill_count"],
            "strategy_name": entry["opp_type"],
            "opp_type": entry["opp_type"],
            "ticker": entry["ticker"],
            "side": entry["side"],
            "terminal_status": terminal_status,
            "kalshi_order_id": order_id,
        }
        regime = _regime_for(entry["ts_placed"], entry["ticker"], entry["market_close_ts"])
        if regime is not None:
            record["regime"] = regime
        _append_record(record)
    except Exception:
        _logger.exception("record_terminal failed for order_id=%s", order_id)


def record_synchronous_rejection(
    opportunity: dict,
    requested_price_cents: int,
    requested_qty: int,
    side: str,
    ts_placed: datetime,
    error: str,
) -> None:
    """Order rejected immediately by place_order ({"error": ...}). No order_id.
    Never raises."""
    try:
        opp_type = opportunity.get("opp_type") or opportunity.get("type") or "unknown"
        ticker = opportunity.get("ticker", "")
        market = opportunity.get("market") if isinstance(opportunity.get("market"), dict) else {}
        market_close_ts = market.get("close_ts") or market.get("close_time")
        record = {
            "ts_placed": ts_placed.isoformat(),
            "ts_filled": None,
            "ts_canceled": None,
            "requested_price_cents": int(requested_price_cents),
            "filled_price_cents": None,
            "requested_qty": int(requested_qty),
            "filled_qty": 0,
            "order_type": "limit",
            "slippage_cents": None,
            "slippage_source": "none",
            "latency_ms": 0,
            "queue_depth_at_place": None,
            "partial_fill_count": 0,
            "strategy_name": opp_type,
            "opp_type": opp_type,
            "ticker": ticker,
            "side": side,
            "terminal_status": "rejected",
            "kalshi_order_id": None,
            "rejection_error": error,
        }
        regime = _regime_for(ts_placed, ticker, market_close_ts)
        if regime is not None:
            record["regime"] = regime
        _append_record(record)
    except Exception:
        _logger.exception(
            "record_synchronous_rejection failed for %s",
            opportunity.get("ticker", "?"),
        )
