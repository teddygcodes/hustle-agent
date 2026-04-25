"""Per-prediction fair-value vs. actual log. Append-only JSONL; daily rotation
via scheduler.

Session 11 (Apr 25 redemption plan): every edge calc is
`(fair_value - market_price) / market_price`, so the bot is one big bet on
fair_value being right. CLV (Session 5) measures execution; this file measures
prediction. Pair every prediction the scanner emits (real trade or
counterfactual) with the closing yes-price once the market settles. Read by
`tools/calibration_report.py` — Brier scores + per-bucket hit-rate calibration.

Idempotent on (scan_id, ticker). Settlement matching: ticker + recorded_at
within ±60s of the corresponding clv record's recorded_at (handles the small
lag between record_clv_entry and record_prediction firing across module
boundaries).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from bot.config import BOT_STATE_DIR
from bot.regime import tag as regime_tag

PREDICTIONS_FILE = BOT_STATE_DIR / "predictions.jsonl"

# ±60s window for matching predictions to settled clv records.
SETTLEMENT_MATCH_WINDOW_SEC = 60.0

_LOCK = threading.Lock()
_logger = logging.getLogger("glint.calibration")


def record_prediction(
    ticker: str,
    opp_type: str,
    predicted_fair_cents: float | None,
    market_price_cents: int,
    scan_id: str,
    recorded_at: str | None = None,
) -> None:
    """Append one prediction row to predictions.jsonl. Never raises.

    Args:
        ticker: Kalshi market ticker.
        opp_type: Strategy name (vig_stack_series, etc.).
        predicted_fair_cents: Model-predicted fair value in cents. None or 0
            silently skips (live_momentum has no usable fair value — Session
            7 limitation).
        market_price_cents: What the market was offering at decision time.
        scan_id: Unique-per-scan identifier from the caller. (scan_id, ticker)
            is the idempotency key.
        recorded_at: ISO timestamp. Defaults to now. Pass the corresponding
            clv record's recorded_at when you want the settlement matcher
            to find this row reliably.

    Failures are logged at ERROR but the caller continues — never raises.
    """
    if not predicted_fair_cents:
        return
    record = {
        "ts": recorded_at or datetime.now(timezone.utc).isoformat(),
        "scan_id": scan_id,
        "ticker": ticker,
        "opp_type": opp_type,
        "predicted_fair_cents": round(float(predicted_fair_cents), 2),
        "market_price_cents": int(market_price_cents),
        "closing_yes_price": None,
    }
    try:
        record["regime"] = regime_tag(
            ts=datetime.fromisoformat(record["ts"]),
            ticker=ticker,
            market_state=None,
        )
    except Exception:
        _logger.exception("calibration.record_prediction: regime_tag failed for %s", ticker)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        with _LOCK:
            BOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
            if _already_written(scan_id, ticker):
                return
            with open(PREDICTIONS_FILE, "a") as f:
                f.write(line)
    except Exception:
        _logger.exception("calibration.record_prediction failed for %s", ticker)


def update_prediction_close(
    ticker: str,
    recorded_at: str,
    closing_yes_price: float,
) -> int:
    """Fill closing_yes_price on every predictions.jsonl row matching
    ticker AND ts within ±60s of recorded_at. Returns count of rows updated.

    Called from clv.check_clv_settlements after a clv record gets its
    closing price filled. The ±60s window absorbs the small lag between
    record_clv_entry and record_prediction (both fire from the same
    executor / scanner call but cross a state-file boundary).

    Read-all → modify-matching → rewrite under lock. File rotates daily so
    this stays small. Never raises.
    """
    try:
        anchor = _parse_iso(recorded_at)
    except Exception:
        _logger.warning("update_prediction_close: bad recorded_at %r", recorded_at)
        return 0
    if anchor is None:
        return 0

    try:
        with _LOCK:
            if not PREDICTIONS_FILE.exists():
                return 0
            updated = 0
            new_lines: list[str] = []
            with open(PREDICTIONS_FILE, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        new_lines.append(line)
                        continue
                    if (
                        rec.get("ticker") == ticker
                        and rec.get("closing_yes_price") is None
                        and _within_window(rec.get("ts"), anchor)
                    ):
                        rec["closing_yes_price"] = round(float(closing_yes_price), 2)
                        updated += 1
                        new_lines.append(json.dumps(rec, separators=(",", ":")) + "\n")
                    else:
                        new_lines.append(line)
            if updated:
                tmp = PREDICTIONS_FILE.with_suffix(".tmp")
                tmp.write_text("".join(new_lines))
                tmp.rename(PREDICTIONS_FILE)
            return updated
    except Exception:
        _logger.exception("calibration.update_prediction_close failed for %s", ticker)
        return 0


def _already_written(scan_id: str, ticker: str) -> bool:
    """Idempotency check. Substring scan over current-day predictions.jsonl
    (cheap because of daily rotation; we control writer output format)."""
    if not PREDICTIONS_FILE.exists():
        return False
    needle_scan = f'"scan_id":"{scan_id}"'
    needle_ticker = f'"ticker":"{ticker}"'
    try:
        with open(PREDICTIONS_FILE, "r") as f:
            for line in f:
                if needle_scan in line and needle_ticker in line:
                    return True
    except Exception:
        return False
    return False


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _within_window(ts: str | None, anchor: datetime) -> bool:
    pred_ts = _parse_iso(ts)
    if pred_ts is None:
        return False
    return abs((pred_ts - anchor).total_seconds()) <= SETTLEMENT_MATCH_WINDOW_SEC
