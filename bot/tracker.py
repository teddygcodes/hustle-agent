"""
Nexus Trading Bot — Position Tracking & P&L

Monitors open positions, resolves settled markets, computes P&L.
Sends Telegram alerts for significant moves and daily summaries.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.kalshi_client import get_market, get_balance, get_positions as kalshi_get_positions

from bot.config import (
    POSITIONS_FILE, TRADE_HISTORY_FILE, BOT_STATE_FILE,
    POSITION_MOVE_ALERT, TAKE_PROFIT_THRESHOLD, CUT_LOSS_THRESHOLD,
    PAPER_TRADES_FILE,
)
from bot.state_io import load_json as _load_json, save_json as _save_json

logger = logging.getLogger("nexus.tracker")


# ---------------------------------------------------------------------------
# Position updates
# ---------------------------------------------------------------------------

def update_positions() -> list[dict]:
    """
    Check all open positions against current Kalshi prices.
    Calculate unrealized P&L and flag significant moves.

    Returns:
        List of positions that moved significantly (for alerting).
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return []

    alerts = []

    for pos in positions:
        if pos.get("status") not in ("filled", "partial"):
            continue

        ticker = pos.get("ticker", "")
        side = pos.get("side", "yes")
        entry_price = pos.get("price_cents", 0) / 100.0
        filled = pos.get("filled", 0)

        if filled <= 0 or entry_price <= 0:
            continue

        # Fetch current market price
        market = get_market(ticker)
        if "error" in market:
            continue

        # Get current bid (what we could sell for)
        if side == "yes":
            current_bid = (market.get("yes_bid") or 0) / 100.0
        else:
            current_bid = (market.get("no_bid") or 0) / 100.0

        # Calculate unrealized P&L
        cost = filled * entry_price
        current_value = filled * current_bid
        unrealized_pnl = current_value - cost
        pnl_percent = unrealized_pnl / cost if cost > 0 else 0

        pos["current_bid"] = round(current_bid, 4)
        pos["unrealized_pnl"] = round(unrealized_pnl, 2)
        pos["pnl_percent"] = round(pnl_percent, 4)
        pos["last_checked"] = datetime.now(timezone.utc).isoformat()

        # Categorized alerts
        if pnl_percent >= TAKE_PROFIT_THRESHOLD:
            alerts.append({
                "type": "take_profit",
                "ticker": ticker,
                "title": pos.get("title", ""),
                "side": side,
                "entry_price": entry_price,
                "current_bid": current_bid,
                "unrealized_pnl": unrealized_pnl,
                "pnl_percent": pnl_percent,
                "contracts": filled,
            })
            logger.info(f"Take profit alert: {ticker} +{pnl_percent:.0%}")
        elif pnl_percent <= CUT_LOSS_THRESHOLD:
            alerts.append({
                "type": "cut_loss",
                "ticker": ticker,
                "title": pos.get("title", ""),
                "side": side,
                "entry_price": entry_price,
                "current_bid": current_bid,
                "unrealized_pnl": unrealized_pnl,
                "pnl_percent": pnl_percent,
                "contracts": filled,
            })
            logger.warning(f"Cut loss alert: {ticker} {pnl_percent:.0%}")
        elif pnl_percent < -POSITION_MOVE_ALERT:
            alerts.append({
                "type": "position_move",
                "ticker": ticker,
                "title": pos.get("title", ""),
                "side": side,
                "entry_price": entry_price,
                "current_bid": current_bid,
                "unrealized_pnl": unrealized_pnl,
                "pnl_percent": pnl_percent,
                "contracts": filled,
            })

    # Check resting orders that have been unfilled for >30 minutes
    for pos in positions:
        if pos.get("status") == "resting":
            opened_at = pos.get("opened_at", "")
            if opened_at:
                try:
                    opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                    age_minutes = (datetime.now(timezone.utc) - opened).total_seconds() / 60
                    if age_minutes > 30:
                        alerts.append({
                            "type": "resting_expiry",
                            "ticker": pos.get("ticker", ""),
                            "title": pos.get("title", ""),
                            "age_minutes": round(age_minutes),
                        })
                except (ValueError, TypeError):
                    pass

    _save_json(POSITIONS_FILE, positions)
    return alerts


# ---------------------------------------------------------------------------
# Trade resolution
# ---------------------------------------------------------------------------

def resolve_trades() -> list[dict]:
    """
    Check if any markets have resolved. Calculate realized P&L.

    Returns:
        List of resolved trades with P&L for notification.
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return []

    resolved = []

    for pos in positions:
        if pos.get("status") not in ("filled", "partial"):
            continue

        ticker = pos.get("ticker", "")
        side = pos.get("side", "yes")
        entry_price = pos.get("price_cents", 0) / 100.0
        filled = pos.get("filled", 0)

        # Check market status
        market = get_market(ticker)
        if "error" in market:
            continue

        market_status = market.get("status", "")
        market_result = market.get("result", "")

        if market_status not in ("settled", "finalized", "closed") or not market_result:
            continue

        # Market has resolved
        cost = filled * entry_price
        won = (
            (market_result.upper() == "YES" and side == "yes") or
            (market_result.upper() == "NO" and side == "no")
        )

        if won:
            payout = filled * 1.00  # $1 per contract on win
            pnl = payout - cost
            result = "won"
        else:
            payout = 0.0
            pnl = -cost
            result = "lost"

        pos["status"] = "resolved"
        pos["market_result"] = market_result
        pos["result"] = result
        pos["payout"] = round(payout, 2)
        pos["pnl"] = round(pnl, 2)
        pos["resolved_at"] = datetime.now(timezone.utc).isoformat()

        # Update paper_trades.json if this was a paper position
        if pos.get("paper"):
            order_id = pos.get("order_id", "")
            resolved_at_iso = pos["resolved_at"]
            paper_trades = _load_json(PAPER_TRADES_FILE)
            if isinstance(paper_trades, list):
                for pt in paper_trades:
                    if pt.get("id") == order_id or (
                        not order_id and pt.get("ticker") == ticker and pt.get("status") == "open"
                    ):
                        pt["status"] = "won" if won else "lost"
                        pt["exit_price"] = 1.0 if won else 0.0
                        pt["pnl"] = round(pnl, 4)
                        pt["resolved_at"] = resolved_at_iso
                        break
                _save_json(PAPER_TRADES_FILE, paper_trades)

        resolved.append({
            "ticker": ticker,
            "title": pos.get("title", ""),
            "side": side,
            "result": result,
            "cost": round(cost, 2),
            "payout": round(payout, 2),
            "pnl": round(pnl, 2),
            "contracts": filled,
            "opp_type": pos.get("opp_type", ""),
            "canonical_team": pos.get("canonical_team", ""),
            "opponent_team": pos.get("opponent_team", ""),
            "sport": pos.get("sport", ""),
        })

        # Update Elo ratings after each resolved sports game
        opp_type = pos.get("opp_type", "")
        if opp_type == "series_game_edge":
            sport = pos.get("sport", "")
            canonical = pos.get("canonical_team", "")
            opponent = pos.get("opponent_team", "")
            if canonical and opponent and sport in ("nba", "mlb"):
                try:
                    from bot.elo import update_elo
                    if result == "won":
                        # We bet on canonical → canonical won
                        update_elo(canonical, opponent, sport)
                    else:
                        update_elo(opponent, canonical, sport)
                except Exception as e:
                    logger.warning(f"Elo update failed for {ticker}: {e}")

        logger.info(f"Resolved: {ticker} → {result} (${pnl:+.2f})")

    if resolved:
        _save_json(POSITIONS_FILE, positions)

        # Also append to trade history
        history = _load_json(TRADE_HISTORY_FILE)
        if not isinstance(history, list):
            history = []
        for r in resolved:
            # Find matching entry in history and update it
            for h in history:
                if h.get("ticker") == r["ticker"] and h.get("status") != "resolved":
                    h.update(r)
                    break
        _save_json(TRADE_HISTORY_FILE, history)

    return resolved


# ---------------------------------------------------------------------------
# Daily summary stats
# ---------------------------------------------------------------------------

def compute_daily_summary() -> dict:
    """
    Compute daily trading statistics for the summary message.

    Returns:
        Dict with balance, P&L, win rate, trade counts, etc.
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        positions = []

    bot_state = _load_json(BOT_STATE_FILE)
    if not isinstance(bot_state, dict):
        bot_state = {}

    # Get current balance
    balance_result = get_balance()
    balance = balance_result.get("balance_dollars", 0.0) if "error" not in balance_result else 0.0

    today = date.today().isoformat()

    # Filter for today's resolved trades
    today_resolved = [
        p for p in positions
        if p.get("status") == "resolved"
        and p.get("resolved_at", "").startswith(today)
    ]

    # Filter for today's opened trades
    today_opened = [
        p for p in positions
        if p.get("opened_at", "").startswith(today)
    ]

    # Open positions
    open_positions = [
        p for p in positions
        if p.get("status") in ("filled", "partial", "resting")
    ]

    # All-time resolved
    all_resolved = [p for p in positions if p.get("status") == "resolved"]

    # Win rate
    wins = [p for p in all_resolved if p.get("result") == "won"]
    total_resolved = len(all_resolved)
    win_rate = len(wins) / total_resolved if total_resolved > 0 else 0.0

    # Today P&L
    today_pnl = sum(p.get("pnl", 0) for p in today_resolved)

    # Total P&L
    total_pnl = sum(p.get("pnl", 0) for p in all_resolved)

    # Best and worst trades
    best_trade = max(all_resolved, key=lambda p: p.get("pnl", 0), default=None)
    worst_trade = min(all_resolved, key=lambda p: p.get("pnl", 0), default=None)

    return {
        "balance": balance,
        "today_pnl": round(today_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "trades_today": len(today_opened),
        "resolved_today": len(today_resolved),
        "win_rate": round(win_rate, 4),
        "open_positions": len(open_positions),
        "total_trades": len(all_resolved),
        "total_wins": len(wins),
        "best_trade": {
            "ticker": best_trade.get("ticker", ""),
            "pnl": best_trade.get("pnl", 0),
        } if best_trade else None,
        "worst_trade": {
            "ticker": worst_trade.get("ticker", ""),
            "pnl": worst_trade.get("pnl", 0),
        } if worst_trade else None,
        "scans_today": bot_state.get("scans_today", 0),
        "odds_api_used": bot_state.get("odds_api_requests_this_month", 0),
        "odds_api_limit": 450,
        "date": today,
    }


# ---------------------------------------------------------------------------
# Closing Line Value tracking
# ---------------------------------------------------------------------------

def track_closing_line(position: dict, final_odds: dict | None = None):
    """
    Record closing line value (CLV) when a market resolves.

    If the closing consensus probability confirms our edge direction,
    the strategy is sharp. If it moves against us, we may be finding noise.
    """
    if not final_odds:
        return

    ticker = position.get("ticker", "")
    history = _load_json(TRADE_HISTORY_FILE)
    if not isinstance(history, list):
        return

    for entry in history:
        if entry.get("ticker") == ticker:
            entry_edge = entry.get("relative_edge", 0)
            # CLV positive = closing line moved in our favor
            entry["closing_line_data"] = final_odds
            entry["clv_positive"] = entry_edge > 0
            break

    _save_json(TRADE_HISTORY_FILE, history)


# ---------------------------------------------------------------------------
# Analytics functions
# ---------------------------------------------------------------------------

def get_clv_stats(recent_n: int = 50) -> dict:
    """
    Return closing-line-value stats across the most recent N resolved trades.

    CLV positive = the line moved in our favor after entry (sharp confirmation).
    CLV negative = the line moved against us (we may have been fading sharp money).

    A healthy model shows CLV positive rate > 50% consistently.
    """
    history = _load_json(TRADE_HISTORY_FILE)
    if not isinstance(history, list):
        return {"count": 0, "positive_rate": 0.0, "no_data": True}

    clv_entries = [
        t for t in history
        if t.get("status") == "resolved" and t.get("closing_line_data") is not None
    ]
    if not clv_entries:
        return {"count": 0, "positive_rate": 0.0, "no_data": True}

    recent = clv_entries[-recent_n:]
    positive = sum(1 for t in recent if t.get("clv_positive", False))
    return {
        "count": len(recent),
        "positive_rate": round(positive / len(recent), 3),
        "no_data": False,
    }


def get_winrate(category: str | None = None) -> dict:
    """
    Get win rate, optionally filtered by category (type, sport).

    Args:
        category: Filter key like "weather", "parlay_yes", "vig_stack_no",
                  "nba", "mlb", etc. None = overall.

    Returns:
        {total, wins, losses, winrate, roi, avg_pnl}
    """
    history = _load_json(TRADE_HISTORY_FILE)
    if not isinstance(history, list):
        return {"total": 0, "wins": 0, "losses": 0, "winrate": 0, "roi": 0, "avg_pnl": 0}

    resolved = [t for t in history if t.get("status") == "resolved"]

    if category:
        cat_lower = category.lower()
        filtered = []
        for t in resolved:
            opp_type = t.get("type", "").lower()
            title = t.get("title", "").lower()
            if cat_lower == opp_type:
                filtered.append(t)
            elif cat_lower in title:
                filtered.append(t)
        resolved = filtered

    total = len(resolved)
    wins = sum(1 for t in resolved if t.get("result") == "won")
    losses = total - wins
    total_pnl = sum(t.get("pnl", 0) for t in resolved)
    total_cost = sum(t.get("cost", 0) for t in resolved)

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "winrate": round(wins / total, 4) if total > 0 else 0,
        "roi": round(total_pnl / total_cost, 4) if total_cost > 0 else 0,
        "avg_pnl": round(total_pnl / total, 2) if total > 0 else 0,
    }


def get_streak() -> dict:
    """
    Get current win/loss streak from most recent trades.

    Returns:
        {type: "win"|"loss"|"none", count: int}
    """
    history = _load_json(TRADE_HISTORY_FILE)
    if not isinstance(history, list):
        return {"type": "none", "count": 0}

    resolved = sorted(
        [t for t in history if t.get("status") == "resolved"],
        key=lambda t: t.get("resolved_at", ""),
        reverse=True,
    )

    if not resolved:
        return {"type": "none", "count": 0}

    streak_type = resolved[0].get("result", "lost")
    count = 0
    for t in resolved:
        if t.get("result") == streak_type:
            count += 1
        else:
            break

    return {"type": "win" if streak_type == "won" else "loss", "count": count}


def get_roi_by_strategy() -> dict:
    """
    Get ROI breakdown by strategy type.

    Returns:
        {type: {total, wins, roi, total_pnl}}
    """
    history = _load_json(TRADE_HISTORY_FILE)
    if not isinstance(history, list):
        return {}

    resolved = [t for t in history if t.get("status") == "resolved"]
    by_type: dict[str, dict] = {}

    for t in resolved:
        opp_type = t.get("type", "unknown")
        if opp_type not in by_type:
            by_type[opp_type] = {"total": 0, "wins": 0, "total_cost": 0, "total_pnl": 0}

        by_type[opp_type]["total"] += 1
        if t.get("result") == "won":
            by_type[opp_type]["wins"] += 1
        by_type[opp_type]["total_cost"] += t.get("cost", 0)
        by_type[opp_type]["total_pnl"] += t.get("pnl", 0)

    result = {}
    for opp_type, data in by_type.items():
        result[opp_type] = {
            "total": data["total"],
            "wins": data["wins"],
            "winrate": round(data["wins"] / data["total"], 4) if data["total"] > 0 else 0,
            "roi": round(data["total_pnl"] / data["total_cost"], 4) if data["total_cost"] > 0 else 0,
            "total_pnl": round(data["total_pnl"], 2),
        }

    return result


def get_open_positions_detail() -> list[dict]:
    """Get all open positions with current P&L for display."""
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return []

    return [
        {
            "ticker": p.get("ticker", ""),
            "title": p.get("title", "")[:60],
            "side": p.get("side", ""),
            "contracts": p.get("filled", 0),
            "entry_price": p.get("price_cents", 0),
            "current_bid": p.get("current_bid", 0),
            "cost": round(p.get("cost", 0), 2),
            "unrealized_pnl": round(p.get("unrealized_pnl", 0), 2),
            "pnl_percent": round(p.get("pnl_percent", 0), 4),
            "type": p.get("type", ""),
            "opened_at": p.get("opened_at", ""),
        }
        for p in positions
        if p.get("status") in ("filled", "partial", "resting")
    ]


def get_trade_history(n: int = 5) -> list[dict]:
    """Get last n trades from history."""
    history = _load_json(TRADE_HISTORY_FILE)
    if not isinstance(history, list):
        return []

    resolved = sorted(
        [t for t in history if t.get("status") == "resolved"],
        key=lambda t: t.get("resolved_at", ""),
        reverse=True,
    )

    return [
        {
            "ticker": t.get("ticker", ""),
            "type": t.get("type", ""),
            "side": t.get("side", ""),
            "result": t.get("result", ""),
            "cost": round(t.get("cost", 0), 2),
            "pnl": round(t.get("pnl", 0), 2),
            "resolved_at": t.get("resolved_at", "")[:10],
        }
        for t in resolved[:n]
    ]
