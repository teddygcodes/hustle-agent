"""
Closing-Line Value (CLV) Tracker

Records the Kalshi price at trade entry, then compares to the closing price
when the market settles. Positive CLV = you beat the closing line = your
edge detection is working. Negative CLV = the market disagreed with you =
stop trading that strategy until you understand why.

All values in YES-price space (cents) for consistency:
  - YES trades:  CLV = closing_yes_price - entry_yes_price
  - NO trades:   CLV = entry_yes_price - closing_yes_price
  (positive CLV always means "market moved in my favor")

CLV is tracked per strategy type so you can see which edges are real.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bot.regime import tag as regime_tag

logger = logging.getLogger("glint.clv")

# Imported at call-site to avoid circular imports
_CLV_FILE: Optional[Path] = None


def _get_file() -> Path:
    global _CLV_FILE
    if _CLV_FILE is None:
        from bot.config import CLV_FILE
        _CLV_FILE = CLV_FILE
    return _CLV_FILE


def _active_strategies() -> set[str]:
    # Scanner-driven strategies + live_momentum (runs via live_watcher,
    # not in ACTIVE_STRATEGIES). Drop any record whose opp_type isn't here —
    # disabled-strategy CLV records are noise (Apr 23 Session 5).
    from bot.config import ACTIVE_STRATEGIES
    return set(ACTIVE_STRATEGIES) | {"live_momentum"}


def compute_clv_cents(
    side: str,
    entry_price_cents: int,
    closing_yes_price: float,
) -> tuple[float, float]:
    """Compute closing-line value in cents + relative for one trade.

    Pure function, no I/O. Single source of truth for CLV math — used by
    bot.clv.check_clv_settlements (live settler) and tools/backtest.py
    (Session 13b offline back-tester). Back-tester divergence here means
    the back-tester is wrong, not live.

    Args:
        side: "yes" or "no" — direction of the trade.
        entry_price_cents: Integer cents (1-99) paid at entry.
        closing_yes_price: Final YES price in cents (0.0-100.0). 100 if YES
            won, 0 if NO won at settlement; mid-price for partial updates.

    Returns:
        (clv_cents, clv_relative): rounded to 2dp and 4dp respectively to
        match legacy storage format. clv_relative is 0.0 when entry <= 0
        (defensive — pre-Session-6 records may have slipped through).
    """
    if side == "yes":
        clv_cents = closing_yes_price - entry_price_cents
    else:
        # NO trade: implicit YES price = 100 - entry. Positive CLV when YES
        # moved AWAY from us (down) — good for NO holder.
        our_implied_yes = 100 - entry_price_cents
        clv_cents = our_implied_yes - closing_yes_price

    clv_relative = clv_cents / entry_price_cents if entry_price_cents > 0 else 0.0
    return round(clv_cents, 2), round(clv_relative, 4)


def _load() -> list[dict]:
    f = _get_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    active = _active_strategies()
    return [r for r in data if r.get("opp_type") in active]


def _save(records: list[dict]):
    f = _get_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2, default=str))
    tmp.rename(f)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record_counterfactual_skip(opp: dict, gate: str, scan_id: str) -> None:
    """Record a 'what would the closing price have been if we'd taken this' entry.

    Session 6 (Apr 24): top-N rejected opportunities per scan get a CF record
    so we can later answer "did this gate cost us money?" — the same settlement
    poller fills closing_yes_price for these and `tools/cohort_report.py`
    joins gate ↔ CLV to surface mis-tuned thresholds.

    Idempotent on (scan_id, ticker). Records carry status="counterfactual_open"
    until settled; `get_clv_report()` filters on status=="settled" so CF rows
    do not pollute paper-trade CLV reports.
    """
    side = opp.get("side") or opp.get("recommended_side") or "yes"
    side = str(side).lower()

    entry_price = opp.get("price_cents")
    if entry_price is None:
        entry_price = opp.get("yes_ask")
    try:
        entry_price = int(entry_price) if entry_price is not None else None
    except (TypeError, ValueError):
        entry_price = None
    if entry_price is None or entry_price <= 0:
        logger.debug("CF skip: %s missing usable entry price", opp.get("ticker"))
        return

    fair = opp.get("fair_value_cents") or opp.get("fair_cents") or 0
    edge = opp.get("edge")

    cf_id = f"CF-{scan_id}-{opp.get('ticker','UNK')}"

    records = _load()
    if any(r.get("trade_id") == cf_id for r in records):
        return

    records.append({
        "ticker": opp.get("ticker", ""),
        "opp_type": opp.get("opp_type") or opp.get("type") or "vig_stack_series",
        "side": side,
        "entry_price_cents": entry_price,
        "fair_value_cents": round(float(fair), 2) if fair else 0.0,
        "edge_at_trade": round(float(edge), 4) if edge is not None else None,
        "contracts": 0,
        "trade_id": cf_id,
        "paper": False,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "counterfactual_open",
        "skipped_by_gate": gate,
        "closing_yes_price": None,
        "clv_cents": None,
        "clv_relative": None,
        "settled_at": None,
    })
    try:
        records[-1]["regime"] = regime_tag(
            ts=datetime.fromisoformat(records[-1]["recorded_at"]),
            ticker=opp.get("ticker", ""),
            market_state=opp,
        )
    except Exception:
        logger.exception("clv.record_counterfactual_skip: regime_tag failed for %s", opp.get("ticker"))
    _save(records)

    # Session 11: pair every CF with a prediction record so calibration_report
    # can compute Brier + per-bucket hit-rate over the rejected distribution.
    try:
        from bot.calibration import record_prediction
        record_prediction(
            ticker=opp.get("ticker", ""),
            opp_type=opp.get("opp_type") or opp.get("type") or "vig_stack_series",
            predicted_fair_cents=float(fair) if fair else None,
            market_price_cents=entry_price,
            scan_id=scan_id,
            recorded_at=records[-1]["recorded_at"],
        )
    except Exception:
        logger.exception("calibration record_prediction failed (non-fatal CF)")


def record_clv_entry(
    ticker: str,
    opp_type: str,
    side: str,
    entry_price_cents: int,
    fair_value_cents: float,
    edge_at_trade: float,
    contracts: int,
    trade_id: str,
    paper: bool = False,
    scan_id: str | None = None,
):
    """
    Record a new CLV entry at trade time.

    Args:
        ticker: Kalshi market ticker
        opp_type: Strategy type (weather, vig_stack_series, etc.)
        side: "yes" or "no"
        entry_price_cents: What we paid (yes_ask for YES, no_ask for NO)
        fair_value_cents: Our model's fair value in cents
        edge_at_trade: Relative edge at the time of the trade
        contracts: Number of contracts
        trade_id: Order ID or paper trade ID for cross-reference
        paper: True if this was a paper trade
        scan_id: Session 11 — scanner-emitted scan ID for prediction
            idempotency. Falls back to f"trade-{trade_id}" when missing.
    """
    records = _load()
    records.append({
        "ticker": ticker,
        "opp_type": opp_type,
        "side": side,
        "entry_price_cents": entry_price_cents,
        "fair_value_cents": round(fair_value_cents, 2),
        "edge_at_trade": round(edge_at_trade, 4),
        "contracts": contracts,
        "trade_id": trade_id,
        "paper": paper,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "closing_yes_price": None,
        "clv_cents": None,
        "clv_relative": None,
        "settled_at": None,
    })
    try:
        records[-1]["regime"] = regime_tag(
            ts=datetime.fromisoformat(records[-1]["recorded_at"]),
            ticker=ticker,
            market_state=None,
        )
    except Exception:
        logger.exception("clv.record_clv_entry: regime_tag failed for %s", ticker)
    _save(records)
    logger.info(
        f"CLV recorded: {ticker} | {side.upper()} @ {entry_price_cents}¢ | "
        f"fair={fair_value_cents:.1f}¢ | edge={edge_at_trade:.1%} | {'PAPER' if paper else 'REAL'}"
    )

    # Session 11: pair every real trade with a prediction record so
    # calibration_report can join predicted fair vs. closing yes-price.
    try:
        from bot.calibration import record_prediction
        record_prediction(
            ticker=ticker,
            opp_type=opp_type,
            predicted_fair_cents=fair_value_cents,
            market_price_cents=entry_price_cents,
            scan_id=scan_id or f"trade-{trade_id}",
            recorded_at=records[-1]["recorded_at"],
        )
    except Exception:
        logger.exception("calibration record_prediction failed (non-fatal)")


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def check_clv_settlements() -> list[dict]:
    """
    For every open CLV entry, fetch the current Kalshi market.
    If it's settled, record the closing price and compute CLV.

    Returns list of newly settled CLV entries (for Telegram notification).
    """
    from agent.kalshi_client import get_market
    from bot.config import POSITIONS_FILE
    from bot.state_io import load_json as _load_positions

    records = _load()
    settled_now = []
    changed = False

    # Session 9: build order_id -> position lookup so we can propagate
    # mfe/mae/ticks onto each clv record at settlement time. Counterfactuals
    # have no position record and are left untouched.
    _positions_raw = _load_positions(POSITIONS_FILE)
    _positions_by_order = {
        p.get("order_id"): p
        for p in (_positions_raw or [])
        if isinstance(p, dict) and p.get("order_id")
    }

    for rec in records:
        status = rec.get("status")
        if status not in ("open", "counterfactual_open"):
            continue
        is_cf = status == "counterfactual_open"

        ticker = rec["ticker"]
        try:
            market_resp = get_market(ticker)
        except Exception as e:
            logger.warning(f"CLV settlement check failed for {ticker}: {e}")
            continue

        market = market_resp if isinstance(market_resp, dict) else {}
        if "market" in market:
            market = market["market"]

        status = market.get("status", "")
        result = market.get("result", "")

        # Try to get closing price from last trade or yes_bid/yes_ask at close
        yes_bid = market.get("yes_bid") or 0
        yes_ask = market.get("yes_ask") or 0

        if status in ("settled", "finalized", "closed") and result:
            # Market resolved — closing price is the settlement value
            # YES resolves to 100¢ if YES won, 0¢ if NO won
            if result.upper() == "YES":
                closing_yes = 100
            elif result.upper() == "NO":
                closing_yes = 0
            else:
                continue  # Unknown result, skip
        elif yes_bid > 0 and yes_ask > 0:
            # Market still open — use mid price as current line for partial CLV
            closing_yes = (yes_bid + yes_ask) / 2.0
            # Don't mark as settled yet — just update with current line
            rec["current_yes_mid"] = round(closing_yes, 1)
            changed = True
            continue
        else:
            continue

        # Session 13b: single source of truth — back-tester calls the same fn.
        clv_cents_rounded, clv_relative_rounded = compute_clv_cents(
            rec["side"], rec["entry_price_cents"], closing_yes,
        )

        rec["status"] = "counterfactual_settled" if is_cf else "settled"
        rec["closing_yes_price"] = closing_yes
        rec["market_result"] = result
        rec["clv_cents"] = clv_cents_rounded
        rec["clv_relative"] = clv_relative_rounded
        rec["settled_at"] = datetime.now(timezone.utc).isoformat()
        changed = True

        # Session 11: propagate closing price to predictions.jsonl rows that
        # match this clv record by ticker + recorded_at ±60s. Fires for both
        # real trades and counterfactuals (both sides emit predictions).
        try:
            from bot.calibration import update_prediction_close
            update_prediction_close(
                ticker=ticker,
                recorded_at=rec.get("recorded_at", ""),
                closing_yes_price=closing_yes,
            )
        except Exception:
            logger.exception("calibration update_prediction_close failed (non-fatal)")

        if not is_cf:
            pos = _positions_by_order.get(rec.get("trade_id"))
            if pos is not None:
                for key in ("mfe_cents", "mae_cents", "mfe_at", "mae_at", "ticks_observed"):
                    if pos.get(key) is not None:
                        rec[key] = pos[key]
            settled_now.append(rec.copy())
            logger.info(
                f"CLV settled: {ticker} | {rec['side'].upper()} entry={rec['entry_price_cents']}¢ "
                f"close={closing_yes}¢ | CLV={clv_cents_rounded:+.1f}¢ ({clv_relative_rounded:+.1%}) | "
                f"result={result}"
            )

    if changed:
        _save(records)

    return settled_now


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def get_clv_report() -> dict:
    """
    Compute CLV statistics per strategy type.

    Returns:
        {
          overall: {avg_clv_cents, avg_clv_relative, positive_rate, count},
          by_strategy: {
            "weather": {avg_clv_cents, avg_clv_relative, positive_rate, count, paper_count},
            ...
          },
          recent: [last 5 settled entries],
        }
    """
    records = _load()
    settled = [r for r in records if r.get("status") == "settled" and r.get("clv_cents") is not None]

    def _stats(rows: list[dict]) -> dict:
        if not rows:
            return {"avg_clv_cents": 0.0, "avg_clv_relative": 0.0, "positive_rate": 0.0, "count": 0}
        clvs = [r["clv_cents"] for r in rows]
        rels = [r["clv_relative"] for r in rows]
        n = len(rows)
        pos = sum(1 for c in clvs if c > 0)
        return {
            "avg_clv_cents": round(sum(clvs) / n, 2),
            "avg_clv_relative": round(sum(rels) / n, 4),
            "positive_rate": round(pos / n, 4),
            "count": n,
        }

    by_strategy: dict[str, dict] = {}
    for rec in settled:
        stype = rec.get("opp_type", "unknown")
        by_strategy.setdefault(stype, []).append(rec)

    by_strategy_stats = {}
    for stype, rows in by_strategy.items():
        stats = _stats(rows)
        stats["paper_count"] = sum(1 for r in rows if r.get("paper"))
        by_strategy_stats[stype] = stats

    recent = sorted(settled, key=lambda r: r.get("settled_at", ""), reverse=True)[:5]

    return {
        "overall": _stats(settled),
        "by_strategy": by_strategy_stats,
        "recent": recent,
        "open_count": sum(1 for r in records if r.get("status") == "open"),
    }


def format_clv_report() -> str:
    """Format CLV report as a Telegram message."""
    report = get_clv_report()
    overall = report["overall"]
    by_strat = report["by_strategy"]
    recent = report["recent"]
    open_count = report["open_count"]

    lines = ["📈 CLV REPORT (Closing-Line Value)", ""]

    if overall["count"] == 0:
        lines.append("No settled trades yet — run paper mode for a few cycles first.")
    else:
        clv_emoji = "✅" if overall["avg_clv_cents"] > 0 else "❌"
        lines.append(
            f"Overall ({overall['count']} trades): "
            f"{clv_emoji} avg CLV = {overall['avg_clv_cents']:+.1f}¢ "
            f"({overall['avg_clv_relative']:+.1%}) | "
            f"beat line: {overall['positive_rate']:.0%}"
        )

        if by_strat:
            lines.append("")
            lines.append("By strategy:")
            for stype, s in by_strat.items():
                label = "PAPER" if s.get("paper_count", 0) == s["count"] else ""
                strat_emoji = "✅" if s["avg_clv_cents"] > 0 else "❌"
                lines.append(
                    f"  {strat_emoji} {stype} ({s['count']}{'p' if label else ''}): "
                    f"{s['avg_clv_cents']:+.1f}¢ avg | {s['positive_rate']:.0%} beat rate"
                )

    if recent:
        lines.append("")
        lines.append("Recent:")
        for r in recent:
            ticker = r["ticker"][-22:]
            clv = r["clv_cents"]
            side = r["side"].upper()
            result = r.get("market_result", "?")
            emoji = "✅" if clv > 0 else "❌"
            lines.append(f"  {emoji} {ticker} {side} | CLV={clv:+.1f}¢ | {result}")

    if open_count:
        lines.append(f"\n{open_count} open (awaiting settlement)")

    return "\n".join(lines)
