"""
Glint Bot — Market Maker

Places resting limit orders on both sides of wide-spread thin Kalshi markets.
Captures the bid-ask spread regardless of outcome.

Strategy:
  - Find open markets with spread >= MM_MIN_SPREAD_CENTS
  - Place BUY at yes_bid + 1¢ (improve best bid by one tick)
  - Place SELL at yes_ask - 1¢ (improve best ask by one tick)
  - If both fill: collect spread - 2¢ per contract
  - If only one side fills: monitor for adverse price movement
  - Cancel unfilled pair after MM_CANCEL_AFTER_HOURS

Position tracking in bot/state/mm_positions.json.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agent.kalshi_client import get_markets, get_market, place_order, cancel_order

from bot.config import (
    MM_MIN_SPREAD_CENTS,
    MM_MIN_HOURS_TO_CLOSE,
    MM_MAX_OPEN_PAIRS,
    MM_CANCEL_AFTER_HOURS,
    MM_MAX_CONTRACTS_PER_SIDE,
    MM_POSITIONS_FILE,
    WEATHER_SERIES_TICKERS,
    INDEX_RANGE_SERIES_TICKERS,
)

logger = logging.getLogger("glint.market_maker")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_mm_positions() -> list[dict]:
    if MM_POSITIONS_FILE.exists():
        try:
            return json.loads(MM_POSITIONS_FILE.read_text())
        except Exception:
            return []
    return []


def _save_mm_positions(positions: list[dict]):
    MM_POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MM_POSITIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(positions, indent=2, default=str))
    tmp.rename(MM_POSITIONS_FILE)


# ---------------------------------------------------------------------------
# Opportunity scanning
# ---------------------------------------------------------------------------

def scan_market_making_opportunities(
    exclude_tickers: set | None = None,
) -> list[dict]:
    """
    Scan Kalshi weather series for wide-spread markets suitable for MM.

    Args:
        exclude_tickers: Set of market tickers to skip (e.g. those with large
            weather edges, indicating the market is directionally mispriced).

    Returns list of MM opportunity dicts. Caller decides whether to alert
    Tyler for GO/SKIP or auto-execute (when trust is established).
    """
    opportunities = []
    open_pairs = _load_mm_positions()
    active_tickers = {p["ticker"] for p in open_pairs if p.get("status") == "open"}
    excluded = exclude_tickers or set()
    _telem = {"markets_scanned": 0, "already_active": 0, "excluded": 0, "no_quote": 0,
              "spread_too_tight": 0, "price_out_of_range": 0, "too_close_to_expiry": 0,
              "buy_gte_sell": 0, "surfaced": 0}

    if len(active_tickers) >= MM_MAX_OPEN_PAIRS:
        logger.info(f"MM at capacity ({len(active_tickers)}/{MM_MAX_OPEN_PAIRS} pairs) — skipping scan")
        return []

    now = datetime.now(timezone.utc)

    for series_ticker in list(WEATHER_SERIES_TICKERS) + list(INDEX_RANGE_SERIES_TICKERS):
        try:
            result = get_markets(series_ticker=series_ticker, status="open", limit=100)
        except Exception as e:
            logger.warning(f"MM scan error for {series_ticker}: {e}")
            continue

        if "error" in result:
            continue

        for market in result.get("markets", []):
            _telem["markets_scanned"] += 1
            ticker = market.get("ticker", "")
            if ticker in active_tickers:
                _telem["already_active"] += 1
                continue  # Already have an open MM pair here

            # Skip markets flagged as directionally mispriced (large weather edge)
            # — market-making on these exposes us to adverse selection
            if ticker in excluded:
                logger.debug(f"MM skip {ticker}: flagged by weather edge scanner")
                _telem["excluded"] += 1
                continue

            yes_bid = market.get("yes_bid", 0) or 0
            yes_ask = market.get("yes_ask", 0) or 0

            if yes_bid <= 0 or yes_ask <= 0:
                _telem["no_quote"] += 1
                continue

            spread = yes_ask - yes_bid

            if spread < MM_MIN_SPREAD_CENTS:
                _telem["spread_too_tight"] += 1
                continue

            # Skip near-certain or heavily one-sided outcomes — these are either
            # near-resolved or directionally mispriced (adverse selection risk)
            if yes_ask > 80 or yes_ask < 20:
                _telem["price_out_of_range"] += 1
                continue

            # Check time to close
            close_str = market.get("close_time") or market.get("expiration_time", "")
            hours_to_close = None
            if close_str:
                try:
                    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                    hours_to_close = (close_dt - now).total_seconds() / 3600
                except Exception:
                    pass

            if hours_to_close is not None and hours_to_close < MM_MIN_HOURS_TO_CLOSE:
                _telem["too_close_to_expiry"] += 1
                continue  # Too close to expiry

            # Target prices: one tick better than best bid/ask
            buy_price = yes_bid + 1   # cents — improve best bid
            sell_price = yes_ask - 1  # cents — improve best ask

            # Sanity: buy must be below sell
            if buy_price >= sell_price:
                _telem["buy_gte_sell"] += 1
                continue

            target_capture = sell_price - buy_price  # cents per contract

            # Contracts: small fixed size, respect max
            contracts = min(MM_MAX_CONTRACTS_PER_SIDE, max(1, 100 // sell_price))

            max_risk_buy = buy_price * contracts / 100.0   # dollars
            max_risk_sell = (100 - sell_price) * contracts / 100.0  # dollars
            target_profit = target_capture * contracts / 100.0  # dollars

            opportunities.append({
                "type": "market_making",
                "ticker": ticker,
                "title": market.get("title", ""),
                "market": market,
                "series_ticker": series_ticker,
                "spread_cents": spread,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "target_capture_cents": target_capture,
                "contracts": contracts,
                "hours_to_close": round(hours_to_close, 1) if hours_to_close else None,
                "max_risk_buy": round(max_risk_buy, 2),
                "max_risk_sell": round(max_risk_sell, 2),
                "target_profit": round(target_profit, 2),
            })

    # Sort by target capture (widest spread first)
    opportunities.sort(key=lambda x: x["spread_cents"], reverse=True)
    _telem["surfaced"] = len(opportunities)
    drops = {k: v for k, v in _telem.items() if k not in ("markets_scanned", "surfaced") and v > 0}
    logger.info("MM_TELEMETRY: markets=%d surfaced=%d drops=%s",
                _telem["markets_scanned"], _telem["surfaced"], drops or "none")
    return opportunities


def format_mm_opportunity(opp: dict) -> str:
    """Format a market making opportunity for Telegram."""
    ticker = opp.get("ticker", "?")
    title = opp.get("title", "?")
    yes_bid = opp.get("yes_bid", 0)
    yes_ask = opp.get("yes_ask", 0)
    spread = opp.get("spread_cents", 0)
    buy_price = opp.get("buy_price", 0)
    sell_price = opp.get("sell_price", 0)
    capture = opp.get("target_capture_cents", 0)
    contracts = opp.get("contracts", 0)
    hours_left = opp.get("hours_to_close")
    max_risk_buy = opp.get("max_risk_buy", 0)
    max_risk_sell = opp.get("max_risk_sell", 0)
    profit = opp.get("target_profit", 0)

    time_str = f"{hours_left:.1f}h" if hours_left else "unknown"

    return "\n".join([
        "📐 SPREAD CAPTURE",
        "",
        f"Market: {ticker}",
        f"Title: {title[:50]}",
        f"Closes: {time_str}",
        "",
        "SPREAD:",
        f"  YES bid: {yes_bid}¢ / YES ask: {yes_ask}¢ — spread: {spread}¢",
        f"  Place: BUY @ {buy_price}¢ + SELL @ {sell_price}¢",
        f"  Target capture: {capture}¢/contract",
        "",
        f"SIZING: {contracts} contracts each side",
        f"  Max risk if buy-only: ${max_risk_buy:.2f}",
        f"  Max risk if sell-only: ${max_risk_sell:.2f}",
        f"  Profit if both fill: ${profit:.2f}",
        "",
        "Reply GO to place both orders",
        "Reply SKIP to pass",
    ])


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute_mm_pair(opp: dict) -> dict:
    """
    Place both sides of a market making pair.

    Returns dict with success, buy_order_id, sell_order_id, and reason.
    """
    ticker = opp["ticker"]
    buy_price = opp["buy_price"]
    sell_price = opp["sell_price"]
    contracts = opp["contracts"]

    logger.info(f"MM: placing pair on {ticker} — BUY@{buy_price}¢ SELL@{sell_price}¢ x{contracts}")

    # Place buy order (YES)
    buy_result = place_order(
        ticker=ticker,
        side="yes",
        action="buy",
        count=contracts,
        type="limit",
        yes_price=buy_price,
    )
    if buy_result.get("error") or not buy_result.get("order_id"):
        return {"success": False, "reason": f"Buy order failed: {buy_result.get('error', 'unknown')}"}

    buy_order_id = buy_result["order_id"]
    logger.info(f"MM BUY placed: {buy_order_id}")

    # Place sell order (NO at equivalent price)
    # Selling YES at sell_price = buying NO at (100 - sell_price)
    sell_result = place_order(
        ticker=ticker,
        side="yes",
        action="sell",
        count=contracts,
        type="limit",
        yes_price=sell_price,
    )
    if sell_result.get("error") or not sell_result.get("order_id"):
        # Cancel the buy order since sell failed
        cancel_order(buy_order_id)
        return {"success": False, "reason": f"Sell order failed (buy cancelled): {sell_result.get('error', 'unknown')}"}

    sell_order_id = sell_result["order_id"]
    logger.info(f"MM SELL placed: {sell_order_id}")

    # Record the open pair
    positions = _load_mm_positions()
    positions.append({
        "ticker": ticker,
        "series_ticker": opp.get("series_ticker", ""),
        "buy_order_id": buy_order_id,
        "sell_order_id": sell_order_id,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "contracts": contracts,
        "status": "open",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=MM_CANCEL_AFTER_HOURS)).isoformat(),
    })
    _save_mm_positions(positions)

    return {
        "success": True,
        "buy_order_id": buy_order_id,
        "sell_order_id": sell_order_id,
        "ticker": ticker,
    }


# ---------------------------------------------------------------------------
# Fill monitoring
# ---------------------------------------------------------------------------

def check_mm_fills() -> list[dict]:
    """
    Check fill status of all open MM pairs. Cancel unfilled pairs after timeout.

    Returns list of events (fill, partial, cancelled) for Telegram notifications.
    """
    positions = _load_mm_positions()
    events = []
    now = datetime.now(timezone.utc)
    updated = []

    for pos in positions:
        if pos.get("status") != "open":
            updated.append(pos)
            continue

        ticker = pos["ticker"]
        buy_id = pos.get("buy_order_id")
        sell_id = pos.get("sell_order_id")

        # Check expiry — cancel if unfilled pair has timed out
        exp_str = pos.get("expires_at", "")
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if now >= exp_dt:
                    # Cancel both orders
                    if buy_id:
                        cancel_order(buy_id)
                    if sell_id:
                        cancel_order(sell_id)
                    pos["status"] = "cancelled"
                    pos["closed_at"] = now.isoformat()
                    events.append({
                        "event": "mm_cancelled",
                        "ticker": ticker,
                        "reason": "unfilled after timeout",
                    })
                    logger.info(f"MM pair cancelled (timeout): {ticker}")
                    updated.append(pos)
                    continue
            except Exception:
                pass

        # Fetch order statuses
        buy_filled = False
        sell_filled = False

        try:
            from agent.kalshi_client import get_order
            if buy_id:
                buy_order = get_order(buy_id)
                buy_filled = buy_order.get("status") in ("filled", "executed")
            if sell_id:
                sell_order = get_order(sell_id)
                sell_filled = sell_order.get("status") in ("filled", "executed")
        except Exception as e:
            logger.warning(f"MM fill check failed for {ticker}: {e}")
            updated.append(pos)
            continue

        if buy_filled and sell_filled:
            profit = (pos["sell_price"] - pos["buy_price"]) * pos["contracts"] / 100.0
            pos["status"] = "completed"
            pos["closed_at"] = now.isoformat()
            pos["realized_profit"] = profit
            events.append({
                "event": "mm_completed",
                "ticker": ticker,
                "profit": profit,
                "contracts": pos["contracts"],
            })
            logger.info(f"MM pair completed: {ticker} profit=${profit:.2f}")

        elif buy_filled and not sell_filled:
            events.append({
                "event": "mm_partial",
                "ticker": ticker,
                "side_filled": "buy",
                "message": f"BUY filled @ {pos['buy_price']}¢ — waiting for SELL @ {pos['sell_price']}¢",
            })

        elif sell_filled and not buy_filled:
            events.append({
                "event": "mm_partial",
                "ticker": ticker,
                "side_filled": "sell",
                "message": f"SELL filled @ {pos['sell_price']}¢ — waiting for BUY @ {pos['buy_price']}¢",
            })

        updated.append(pos)

    _save_mm_positions(updated)
    return events
