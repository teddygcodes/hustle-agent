"""Blocked-trade shadow evidence ledger.

Session 95: append-only rows for a narrow set of blocked opportunities whose
outcomes we want to evaluate later. This is evidence capture only; it never
changes trading decisions.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone

from bot.config import BOT_STATE_DIR
from bot.regime import tag as regime_tag

SHADOW_TRADES_FILE = BOT_STATE_DIR / "shadow_trades.jsonl"
ALLOWED_BLOCKED_REASONS = {
    "family_disabled_reject",
    "sport_disabled",
    "reentry_blocked",
}

_LOCK = threading.Lock()
_logger = logging.getLogger("glint.shadow_trades")


def _dollars_from_cents(price_cents: int | float | None) -> float | None:
    if price_cents is None:
        return None
    try:
        return round(float(price_cents) / 100.0, 4)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _append(record: dict) -> None:
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    with _LOCK:
        BOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(SHADOW_TRADES_FILE, "a") as f:
            f.write(line)


def record_blocked_trade(
    *,
    ticker: str,
    opp_type: str,
    blocked_reason: str,
    source: str,
    source_decision_reason: str | None = None,
    would_side: str | None = None,
    would_entry_price: float | None = None,
    would_entry_price_cents: int | float | None = None,
    would_contracts: int | None = None,
    family: str | None = None,
    sport: str | None = None,
    close_ts: str | None = None,
    extra: dict | None = None,
    ts: datetime | None = None,
) -> dict | None:
    """Append one shadow row. Never raises; returns the row on success.

    `would_entry_price` is decimal dollars (0.93), matching paper_trades.
    Callers may pass `would_entry_price_cents` for convenience.
    """
    if blocked_reason not in ALLOWED_BLOCKED_REASONS:
        return None
    now = ts or datetime.now(timezone.utc)
    if would_entry_price is None:
        would_entry_price = _dollars_from_cents(would_entry_price_cents)
    if would_entry_price is not None:
        try:
            would_entry_price = round(float(would_entry_price), 4)
        except (TypeError, ValueError):
            would_entry_price = None
    contracts = _int_or_none(would_contracts)
    sizing_status = (
        "available"
        if would_entry_price is not None and contracts is not None
        else "unavailable"
    )
    would_notional = (
        round(contracts * would_entry_price, 2)
        if sizing_status == "available"
        else None
    )
    clean_extra = dict(extra) if isinstance(extra, dict) else {}
    if close_ts and "close_ts" not in clean_extra:
        clean_extra["close_ts"] = close_ts

    row = {
        "id": f"SHADOW-{uuid.uuid4().hex[:12].upper()}",
        "ts": now.isoformat(),
        "ticker": ticker,
        "opp_type": opp_type,
        "blocked_reason": blocked_reason,
        "would_side": would_side,
        "would_entry_price": would_entry_price,
        "would_contracts": contracts,
        "would_notional": would_notional,
        "sizing_status": sizing_status,
        "family": family,
        "sport": sport,
        "close_ts": close_ts,
        "status": "open",
        "settled_at": None,
        "market_result": None,
        "would_pnl": None,
        "source": source,
        "source_decision_reason": source_decision_reason or blocked_reason,
        "extra": clean_extra,
        "regime": {},
    }
    try:
        row["regime"] = regime_tag(
            ts=datetime.fromisoformat(row["ts"]),
            ticker=ticker,
            market_state=clean_extra or None,
        )
    except Exception:
        _logger.exception("shadow_trades: regime_tag failed for %s", ticker)
    try:
        _append(row)
        return row
    except Exception:
        _logger.exception("shadow_trades.record_blocked_trade failed for %s", ticker)
        return None
