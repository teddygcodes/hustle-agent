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
    _save(records)
    logger.info(
        f"CLV recorded: {ticker} | {side.upper()} @ {entry_price_cents}¢ | "
        f"fair={fair_value_cents:.1f}¢ | edge={edge_at_trade:.1%} | {'PAPER' if paper else 'REAL'}"
    )


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

    records = _load()
    settled_now = []
    changed = False

    for rec in records:
        if rec.get("status") != "open":
            continue

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

        # Compute CLV in YES-price space
        side = rec["side"]
        entry_price = rec["entry_price_cents"]

        if side == "yes":
            # We paid entry_price for YES. Did the line move in our favor?
            # Positive = closing YES > what we paid = market agreed with us
            clv_cents = closing_yes - entry_price
        else:
            # We paid entry_price for NO (implying YES at 100 - entry_price).
            # Our implicit YES price = 100 - entry_price.
            # Positive CLV = closing YES < our implicit YES (market moved against YES = good for NO)
            our_implied_yes = 100 - entry_price
            clv_cents = our_implied_yes - closing_yes

        clv_relative = clv_cents / entry_price if entry_price > 0 else 0.0

        rec["status"] = "settled"
        rec["closing_yes_price"] = closing_yes
        rec["market_result"] = result
        rec["clv_cents"] = round(clv_cents, 2)
        rec["clv_relative"] = round(clv_relative, 4)
        rec["settled_at"] = datetime.now(timezone.utc).isoformat()
        changed = True

        settled_now.append(rec.copy())
        logger.info(
            f"CLV settled: {ticker} | {side.upper()} entry={entry_price}¢ "
            f"close={closing_yes}¢ | CLV={clv_cents:+.1f}¢ ({clv_relative:+.1%}) | "
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
