"""
Nexus Trading Bot — Trade Execution

Places orders on Kalshi with multiple layers of safety checks.
verify_contract_direction() is MANDATORY before every trade.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.kalshi_client import (
    get_balance, get_market, get_positions,
    place_order, cancel_order, get_order,
)
from bot.config import (
    MAX_POSITION_PERCENT, MAX_TOTAL_EXPOSURE, MIN_RELATIVE_EDGE,
    POSITIONS_FILE, TRADE_HISTORY_FILE, BOT_STATE_FILE,
    TRAILING_STOP_MIN_HOLD, PAPER_MODE, MAX_PRICE_MOVE_CENTS,
)
from bot.math_engine import verify_contract_direction, calculate_parlay_edge, calculate_weather_edge
from bot.sizing import kelly_size

logger = logging.getLogger("nexus.executor")


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> list | dict:
    if path.exists():
        return json.loads(path.read_text())
    return [] if "positions" in str(path) or "history" in str(path) else {}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def _check_balance(cost_dollars: float) -> tuple[bool, float, str]:
    """Check if we have enough balance for the trade."""
    result = get_balance()
    if "error" in result:
        return False, 0.0, f"Balance check failed: {result['error']}"
    balance = result.get("balance_dollars", 0.0)
    if balance < cost_dollars:
        return False, balance, f"Insufficient balance: ${balance:.2f} < ${cost_dollars:.2f}"
    return True, balance, "ok"


def _check_position_limits(balance: float, cost_dollars: float, ticker: str) -> tuple[bool, str]:
    """Check position limits: no more than 20% on one trade, 50% total exposure."""
    # Single position limit
    if cost_dollars > balance * MAX_POSITION_PERCENT:
        return False, (
            f"Position too large: ${cost_dollars:.2f} > "
            f"{MAX_POSITION_PERCENT:.0%} of ${balance:.2f} (${balance * MAX_POSITION_PERCENT:.2f})"
        )

    # Total exposure limit
    positions = _load_json(POSITIONS_FILE)
    total_exposure = sum(p.get("cost", 0) for p in positions if isinstance(p, dict))
    if (total_exposure + cost_dollars) > balance * MAX_TOTAL_EXPOSURE:
        return False, (
            f"Total exposure too high: ${total_exposure + cost_dollars:.2f} > "
            f"{MAX_TOTAL_EXPOSURE:.0%} of ${balance:.2f}"
        )

    return True, "ok"


def _verify_edge_still_exists(opportunity: dict) -> tuple[bool, str]:
    """Re-fetch Kalshi price and verify edge still meets threshold.

    The scan and executor must use the SAME price basis:
    - YES-side trades: edge = fair_value - (yes_ask / 100)
    - NO-side trades (vig_stack): edge = true_no - kalshi_no, where kalshi_no = 1 - (yes_ask / 100)
    Both use yes_ask as the anchor because the math is built around the YES price.
    """
    ticker = opportunity.get("ticker", "")
    trade_type = opportunity.get("type", "")

    market = get_market(ticker)
    if "error" in market:
        return False, f"Could not re-fetch market: {market['error']}"

    # Always read yes_ask — it's the price basis for both YES and NO edge calculations
    current_yes_ask = market.get("yes_ask", 0)
    if not current_yes_ask or current_yes_ask <= 0:
        return False, "No valid yes_ask price on re-fetch"

    current_yes_price = current_yes_ask / 100.0

    if trade_type in ("vig_stack_no", "vig_stack_series"):
        # NO-side vig stack: track yes_ask movement using the yes_ask stored
        # in the market dict at scan time (most accurate baseline).
        # Do NOT derive from no_ask — bid/ask spread creates phantom movement.
        vig_result = opportunity.get("vig_result") or opportunity.get("edge_result", {})
        scan_yes_ask_cents = opportunity.get("market", {}).get("yes_ask") or 0
        original_yes_price = (
            scan_yes_ask_cents / 100.0
            if scan_yes_ask_cents > 0
            else vig_result.get("kalshi_yes_price") or (1.0 - (opportunity.get("no_ask_cents", 0) / 100.0))
        )

        print(
            f"  [EdgeCheck/NO] {ticker} | "
            f"scan_yes_ask={original_yes_price:.4f} refetch_yes_ask={current_yes_price:.4f} | "
            f"delta={current_yes_price - original_yes_price:+.4f}"
        )

        # 3¢ kill switch: if price moved more than MAX_PRICE_MOVE_CENTS, abort
        move_cents = abs(current_yes_price - original_yes_price) * 100
        if move_cents > MAX_PRICE_MOVE_CENTS:
            return False, (
                f"Price moved {move_cents:.1f}¢ since alert (max {MAX_PRICE_MOVE_CENTS}¢) — market in motion, aborting"
            )

        if abs(current_yes_price - original_yes_price) < 0.001:
            print(f"  [EdgeCheck/NO] {ticker} | price unchanged → edge still valid")
            return True, "ok"

        true_yes = vig_result.get("true_yes_prob", 0)
        true_no = 1.0 - true_yes
        kalshi_no = 1.0 - current_yes_price
        new_edge = true_no - kalshi_no
        new_relative = new_edge / kalshi_no if kalshi_no > 0 else 0

        print(
            f"  [EdgeCheck/NO] {ticker} | "
            f"NO edge {opportunity.get('relative_edge', 0):.2%} → {new_relative:.2%} | "
            f"threshold={MIN_RELATIVE_EDGE:.2%}"
        )
    else:
        # YES-side: edge = fair_value - kalshi_yes_price
        original_price = opportunity.get("edge_result", {}).get("kalshi_price", 0)

        print(
            f"  [EdgeCheck/YES] {ticker} | "
            f"scan_yes_ask={original_price:.4f} refetch_yes_ask={current_yes_price:.4f} | "
            f"delta={current_yes_price - original_price:+.4f}"
        )

        # 3¢ kill switch
        move_cents = abs(current_yes_price - original_price) * 100
        if move_cents > MAX_PRICE_MOVE_CENTS:
            return False, (
                f"Price moved {move_cents:.1f}¢ since alert (max {MAX_PRICE_MOVE_CENTS}¢) — market in motion, aborting"
            )

        if abs(current_yes_price - original_price) < 0.001:
            print(f"  [EdgeCheck/YES] {ticker} | price unchanged → edge still valid")
            return True, "ok"

        fair_value = opportunity.get("edge_result", {}).get("fair_value", 0)
        new_edge = fair_value - current_yes_price
        new_relative = new_edge / current_yes_price if current_yes_price > 0 else 0

        print(
            f"  [EdgeCheck/YES] {ticker} | "
            f"fair={fair_value:.4f} | "
            f"edge {opportunity.get('relative_edge', 0):.2%} → {new_relative:.2%} | "
            f"threshold={MIN_RELATIVE_EDGE:.2%}"
        )

    # Vig stack series is a structural edge — threshold is 2%, not the default 15%
    edge_threshold = 0.02 if trade_type == "vig_stack_series" else MIN_RELATIVE_EDGE
    if new_relative < edge_threshold:
        print(
            f"  [EdgeCheck] EVAPORATED: {ticker} | "
            f"was {opportunity.get('relative_edge', 0):.2%}, now {new_relative:.2%}"
        )
        return False, (
            f"Edge evaporated: was {opportunity.get('relative_edge', 0):.2%}, "
            f"now {new_relative:.2%} (threshold: {edge_threshold:.2%})"
        )

    return True, "ok"


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def execute_trade(opportunity: dict, sizing: dict) -> dict:
    """
    Execute a trade with full safety checks.

    Safety chain (ALL must pass):
    1. verify_contract_direction() — MANDATORY, prevents backwards bets
    2. Check Kalshi balance sufficient
    3. Check position limits
    4. Verify edge still exists (re-fetch current price)
    5. Self-check math one more time

    Args:
        opportunity: Opportunity dict from scanner
        sizing: Sizing dict from kelly_size()

    Returns:
        {success, order_result, checks, reason}
    """
    ticker = opportunity.get("ticker", "")
    title = opportunity.get("title", "")
    side = opportunity.get("recommended_side", "yes")
    contracts = sizing.get("contracts", 0)
    price_cents = sizing.get("price_cents", 0)
    total_cost = sizing.get("total_cost", 0)
    checks = []

    logger.info(f"Executing trade: {ticker} | {side} | {contracts}x @ {price_cents}¢")

    if contracts <= 0 or price_cents <= 0:
        return {"success": False, "checks": checks, "reason": "Invalid sizing"}

    # ------------------------------------------------------------------
    # CHECK 1: verify_contract_direction() — THE LAST LINE OF DEFENSE
    # ------------------------------------------------------------------
    edge_result = opportunity.get("edge_result", {})
    opp_type = opportunity.get("type", "unknown")

    # Build data thesis from edge calculation
    if opp_type == "weather":
        city = opportunity.get("city", "?")
        forecast = opportunity.get("forecast_temp", "?")
        threshold = opportunity.get("threshold", "?")
        direction = opportunity.get("direction", "?")
        data_thesis = (
            f"NWS forecasts {forecast}°F for {city} after bias correction. "
            f"This is {'ABOVE' if direction == 'above' else 'BELOW'} the {threshold}°F threshold."
        )
    elif opp_type == "vig_stack_no":
        # Build thesis from actual leg probabilities so verify_contract_direction()
        # does real semantic work instead of always rubber-stamping parlays.
        fair_value   = edge_result.get("fair_value", 0)
        kalshi_price = edge_result.get("kalshi_price", 0)
        legs = edge_result.get("legs", [])
        priced_legs = [l for l in legs if l.get("probability") is not None]

        leg_parts = []
        for leg in priced_legs[:4]:
            team  = leg.get("team") or leg.get("player") or "?"
            prob  = leg.get("probability", 0)
            leg_parts.append(f"{team} {prob:.0%}")
        leg_desc = ", ".join(leg_parts) if leg_parts else "no legs priced"

        if side == "yes":
            comparison = "ABOVE" if fair_value > kalshi_price else "BELOW"
            verdict    = (
                "YES underpriced — positive edge to buy YES."
                if fair_value > kalshi_price
                else "YES overpriced — no YES edge, do not buy YES."
            )
            data_thesis = (
                f"Parlay legs: {leg_desc}. "
                f"Combined fair value {fair_value:.1%} is {comparison} Kalshi price {kalshi_price:.1%}. "
                f"{verdict}"
            )
        else:
            comparison = "BELOW" if fair_value < kalshi_price else "ABOVE"
            verdict    = (
                "YES is overpriced — NO has edge from vig stacking."
                if fair_value < kalshi_price
                else "YES underpriced — no NO edge, do not buy NO."
            )
            data_thesis = (
                f"Parlay legs: {leg_desc}. "
                f"True YES probability {fair_value:.1%} is {comparison} Kalshi YES price {kalshi_price:.1%}. "
                f"{verdict}"
            )
    elif opp_type == "series_game_edge":
        fair_value    = edge_result.get("fair_value", 0)
        kalshi_price  = edge_result.get("kalshi_price", 0)
        canonical     = opportunity.get("canonical_team", "?")
        sport         = opportunity.get("sport", "?").upper()
        odds_source   = opportunity.get("odds_source", "books")
        elo_prob      = opportunity.get("elo_prob")
        elo_agrees    = opportunity.get("elo_agrees")
        hours_to_game = opportunity.get("hours_to_game", 0)
        comparison    = "ABOVE" if fair_value > kalshi_price else "BELOW"
        elo_note = ""
        if elo_prob is not None:
            elo_note = (
                f" Elo model independently gives {elo_prob:.1%} — "
                + ("AGREES with books." if elo_agrees else "DISAGREES with books.")
            )
        verdict = (
            f"{canonical} WIN YES is underpriced — positive edge to buy YES."
            if fair_value > kalshi_price
            else f"{canonical} WIN YES is overpriced — edge is on NO side."
        )
        data_thesis = (
            f"{sport} game: {canonical}. "
            f"{odds_source.capitalize()} consensus gives {fair_value:.1%} win probability. "
            f"Kalshi prices YES at {kalshi_price:.1%} — fair value is {comparison} Kalshi. "
            f"{elo_note} "
            f"Hours to game: {hours_to_game:.0f}h. "
            f"{verdict}"
        )
    else:
        # live_latency_arb, btc_price_edge, etc.
        fair_value   = edge_result.get("fair_value", 0)
        kalshi_price = edge_result.get("kalshi_price", 0)
        comparison   = "ABOVE" if fair_value > kalshi_price else "BELOW"
        if side == "yes":
            data_thesis = (
                f"Model gives {fair_value:.1%} fair value. "
                f"Kalshi prices YES at {kalshi_price:.1%} — fair value is {comparison} Kalshi price. "
                + ("YES underpriced — positive edge."
                   if fair_value > kalshi_price
                   else "YES overpriced — no YES edge.")
            )
        else:
            data_thesis = (
                f"Model gives {fair_value:.1%} true YES probability. "
                f"Kalshi prices YES at {kalshi_price:.1%} — fair value is {comparison} Kalshi price. "
                + ("YES is overpriced — NO has edge."
                   if fair_value < kalshi_price
                   else "YES underpriced — no NO edge.")
            )

    direction_check = verify_contract_direction(
        contract_title=title,
        data_thesis=data_thesis,
        intended_side=side,
    )

    checks.append({
        "name": "contract_direction",
        "passed": direction_check["direction_correct"],
        "confidence": direction_check["confidence"],
        "explanation": direction_check["explanation"],
        "warnings": direction_check["warnings"],
    })

    if not direction_check["direction_correct"]:
        logger.warning(f"DIRECTION CHECK FAILED: {direction_check['explanation']}")
        return {
            "success": False,
            "checks": checks,
            "reason": f"Direction check failed: {direction_check['warnings']}",
        }

    logger.info(f"  ✅ Direction check passed ({direction_check['confidence']})")

    # ------------------------------------------------------------------
    # CHECK 2: Balance
    # ------------------------------------------------------------------
    bal_ok, balance, bal_msg = _check_balance(total_cost)
    checks.append({"name": "balance", "passed": bal_ok, "detail": bal_msg, "balance": balance})

    if not bal_ok:
        logger.warning(f"BALANCE CHECK FAILED: {bal_msg}")
        return {"success": False, "checks": checks, "reason": bal_msg}

    logger.info(f"  ✅ Balance OK: ${balance:.2f}")

    # ------------------------------------------------------------------
    # CHECK 3: Position limits
    # ------------------------------------------------------------------
    pos_ok, pos_msg = _check_position_limits(balance, total_cost, ticker)
    checks.append({"name": "position_limits", "passed": pos_ok, "detail": pos_msg})

    if not pos_ok:
        logger.warning(f"POSITION LIMIT CHECK FAILED: {pos_msg}")
        return {"success": False, "checks": checks, "reason": pos_msg}

    logger.info(f"  ✅ Position limits OK")

    # ------------------------------------------------------------------
    # CHECK 4: Edge still exists
    # ------------------------------------------------------------------
    edge_ok, edge_msg = _verify_edge_still_exists(opportunity)
    checks.append({"name": "edge_exists", "passed": edge_ok, "detail": edge_msg})

    if not edge_ok:
        logger.warning(f"EDGE CHECK FAILED: {edge_msg}")
        return {"success": False, "checks": checks, "reason": edge_msg}

    logger.info(f"  ✅ Edge still exists")

    # ------------------------------------------------------------------
    # CHECK 5: Self-check math
    # ------------------------------------------------------------------
    math_ok = edge_result.get("self_check_passed", False)
    checks.append({"name": "math_self_check", "passed": math_ok})

    if not math_ok:
        logger.warning("MATH SELF-CHECK FAILED")
        return {"success": False, "checks": checks, "reason": "Math self-check failed"}

    logger.info(f"  ✅ Math self-check passed")

    # ------------------------------------------------------------------
    # ALL CHECKS PASSED — PLACE ORDER (or simulate in PAPER_MODE)
    # ------------------------------------------------------------------
    import uuid as _uuid

    if PAPER_MODE:
        paper_id = f"PAPER-{_uuid.uuid4().hex[:8].upper()}"
        logger.info(
            f"  📝 [PAPER] Would place: {contracts}x {side.upper()} @ {price_cents}¢ on {ticker} "
            f"(id={paper_id})"
        )
        order_result = {
            "order_id": paper_id,
            "ticker": ticker,
            "side": side,
            "count": contracts,
            "filled_count": contracts,
            "price_cents": price_cents,
            "cost_dollars": total_cost,
            "status": "paper_filled",
            "paper": True,
        }
    else:
        logger.info(f"  🚀 Placing order: {contracts}x {side.upper()} @ {price_cents}¢ on {ticker}")
        order_result = place_order(
            ticker=ticker,
            side=side,
            count=contracts,
            price_cents=price_cents,
            action="buy",
        )

        if "error" in order_result:
            logger.error(f"ORDER FAILED: {order_result['error']}")
            return {
                "success": False,
                "order_result": order_result,
                "checks": checks,
                "reason": f"Order failed: {order_result['error']}",
            }

    # Log to positions and trade history
    filled = order_result.get("filled_count", 0)
    position_entry = {
        "ticker": ticker,
        "title": title,
        "side": side,
        "contracts": contracts,
        "filled": filled,
        "price_cents": price_cents,
        "cost": total_cost,
        "order_id": order_result.get("order_id", ""),
        "type": opp_type,
        "opp_type": opp_type,
        "edge": opportunity.get("edge", 0),
        "relative_edge": opportunity.get("relative_edge", 0),
        "canonical_team": opportunity.get("canonical_team", ""),
        "opponent_team": opportunity.get("opponent_team", ""),
        "sport": opportunity.get("sport", ""),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "filled" if filled == contracts else "partial" if filled > 0 else "resting",
        "paper": PAPER_MODE,
    }

    # Save to positions
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        positions = []
    positions.append(position_entry)
    _save_json(POSITIONS_FILE, positions)

    # Save to trade history
    history = _load_json(TRADE_HISTORY_FILE)
    if not isinstance(history, list):
        history = []
    history.append({
        **position_entry,
        "direction_check": direction_check,
        "sizing": sizing,
        "all_checks": checks,
    })
    _save_json(TRADE_HISTORY_FILE, history)

    # Record CLV entry — we'll compare entry price to closing line when market settles
    try:
        from bot.clv import record_clv_entry
        fair_value_cents = opportunity.get("edge_result", {}).get("fair_value", 0) * 100
        record_clv_entry(
            ticker=ticker,
            opp_type=opp_type,
            side=side,
            entry_price_cents=price_cents,
            fair_value_cents=fair_value_cents,
            edge_at_trade=opportunity.get("relative_edge", 0),
            contracts=filled or contracts,
            trade_id=order_result.get("order_id", ""),
            paper=PAPER_MODE,
        )
    except Exception as e:
        logger.warning(f"CLV recording failed (non-fatal): {e}")

    logger.info(f"  ✅ {'[PAPER] ' if PAPER_MODE else ''}Order placed: {filled}/{contracts} filled")

    return {
        "success": True,
        "order_result": order_result,
        "checks": checks,
        "position": position_entry,
        "reason": "paper_executed" if PAPER_MODE else "executed",
    }


def check_fills() -> list[dict]:
    """Check all resting orders and update positions."""
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return []

    updates = []
    for pos in positions:
        if pos.get("status") not in ("resting", "partial"):
            continue

        order_id = pos.get("order_id")
        if not order_id:
            continue

        order = get_order(order_id)
        if "error" in order:
            continue

        new_filled = order.get("filled_count", pos.get("filled", 0))
        if new_filled != pos.get("filled"):
            pos["filled"] = new_filled
            pos["status"] = (
                "filled" if new_filled >= pos.get("contracts", 0)
                else "partial" if new_filled > 0
                else "resting"
            )
            updates.append(pos)

    if updates:
        _save_json(POSITIONS_FILE, positions)

    return updates


def exit_position(ticker: str, reason: str = "manual") -> dict:
    """
    Sell an existing position. Used when edge evaporates.

    Args:
        ticker: Market ticker to exit
        reason: Why we're exiting

    Returns:
        {success, order_result, reason}
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return {"success": False, "reason": "No positions file"}

    # Find the position
    pos = None
    for p in positions:
        if p.get("ticker") == ticker and p.get("status") in ("filled", "partial"):
            pos = p
            break

    if not pos:
        return {"success": False, "reason": f"No open position for {ticker}"}

    side = pos.get("side", "yes")
    filled = pos.get("filled", 0)
    if filled <= 0:
        return {"success": False, "reason": "No filled contracts to exit"}

    # Get current market price for the opposite side
    market = get_market(ticker)
    if "error" in market:
        return {"success": False, "reason": f"Market fetch failed: {market['error']}"}

    # To exit, we sell on the same side
    if side == "yes":
        exit_price = market.get("yes_bid", 0)
    else:
        exit_price = market.get("no_bid", 0)

    if not exit_price or exit_price <= 0:
        return {"success": False, "reason": f"No bid for {side} side"}

    order_result = place_order(
        ticker=ticker,
        side=side,
        count=filled,
        price_cents=exit_price,
        action="sell",
    )

    if "error" in order_result:
        return {"success": False, "order_result": order_result, "reason": f"Exit order failed: {order_result['error']}"}

    # Update position status
    pos["status"] = "exited"
    pos["exit_price"] = exit_price
    pos["exit_reason"] = reason
    pos["exited_at"] = datetime.now(timezone.utc).isoformat()
    _save_json(POSITIONS_FILE, positions)

    return {"success": True, "order_result": order_result, "reason": f"Exited: {reason}"}


# ---------------------------------------------------------------------------
# Batch & advanced operations
# ---------------------------------------------------------------------------

def exit_all_positions(reason: str = "manual") -> list[dict]:
    """Exit every open (filled/partial) position."""
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return []

    results = []
    for pos in positions:
        if pos.get("status") in ("filled", "partial"):
            ticker = pos.get("ticker")
            if ticker:
                result = exit_position(ticker, reason)
                results.append({"ticker": ticker, **result})
    return results


def check_trailing_stops() -> list[dict]:
    """
    Evaluate trailing-stop conditions for all open positions that have a
    ``trailing_stop_pct`` field.  Updates high-water marks and triggers
    exits when the bid drops below the HWM by the stop percentage.
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return []

    triggered: list[dict] = []
    now = datetime.now(timezone.utc)

    for pos in positions:
        if pos.get("status") not in ("filled", "partial"):
            continue

        trailing_stop_pct = pos.get("trailing_stop_pct")
        if trailing_stop_pct is None:
            continue

        # Enforce minimum hold period
        opened_at_str = pos.get("opened_at")
        if opened_at_str:
            try:
                opened_at = datetime.fromisoformat(opened_at_str)
                if (now - opened_at).total_seconds() < TRAILING_STOP_MIN_HOLD:
                    continue
            except (ValueError, TypeError):
                pass

        ticker = pos.get("ticker")
        if not ticker:
            continue

        market = get_market(ticker)
        if "error" in market:
            continue

        side = pos.get("side", "yes")
        if side == "yes":
            current_bid = market.get("yes_bid", 0) / 100.0
        else:
            current_bid = market.get("no_bid", 0) / 100.0

        # Update high-water mark
        hwm = pos.get("hwm_bid", 0)
        if current_bid > hwm:
            pos["hwm_bid"] = current_bid
            hwm = current_bid

        # Check stop condition
        if hwm > 0 and current_bid < hwm * (1 - trailing_stop_pct):
            result = exit_position(ticker, reason=f"trailing_stop ({trailing_stop_pct:.0%})")
            triggered.append({"ticker": ticker, "hwm": hwm, "current_bid": current_bid, **result})

    # Persist updated HWM values even if no stops triggered
    _save_json(POSITIONS_FILE, positions)

    return triggered


def execute_hedge(ticker: str) -> dict:
    """
    Place a hedge order on the opposite side of an existing position.
    Buys enough contracts at the current price to break even on a loss.
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return {"success": False, "reason": "No positions file"}

    # Find the open position
    pos = None
    for p in positions:
        if p.get("ticker") == ticker and p.get("status") in ("filled", "partial"):
            pos = p
            break

    if not pos:
        return {"success": False, "reason": f"No open position for {ticker}"}

    side = pos.get("side", "yes")
    hedge_side = "no" if side == "yes" else "yes"
    filled = pos.get("filled", 0)
    if filled <= 0:
        return {"success": False, "reason": "No filled contracts to hedge"}

    market = get_market(ticker)
    if "error" in market:
        return {"success": False, "reason": f"Market fetch failed: {market['error']}"}

    if hedge_side == "yes":
        hedge_price = market.get("yes_ask", 0)
    else:
        hedge_price = market.get("no_ask", 0)

    if not hedge_price or hedge_price <= 0:
        return {"success": False, "reason": f"No ask price for {hedge_side} side"}

    # Hedge size: match the number of filled contracts on the opposite side
    hedge_contracts = filled
    hedge_cost = hedge_contracts * hedge_price / 100.0

    # Safety: balance check
    bal_ok, balance, bal_msg = _check_balance(hedge_cost)
    if not bal_ok:
        return {"success": False, "reason": f"Insufficient balance for hedge: {bal_msg}"}

    # Safety: position limits
    pos_ok, pos_msg = _check_position_limits(balance, hedge_cost, ticker)
    if not pos_ok:
        return {"success": False, "reason": f"Position limit exceeded for hedge: {pos_msg}"}

    order_result = place_order(
        ticker=ticker,
        side=hedge_side,
        count=hedge_contracts,
        price_cents=hedge_price,
        action="buy",
    )

    if "error" in order_result:
        return {"success": False, "order_result": order_result, "reason": f"Hedge order failed: {order_result['error']}"}

    hedge_filled = order_result.get("filled_count", 0)
    hedge_entry = {
        "ticker": ticker,
        "title": pos.get("title", ""),
        "side": hedge_side,
        "contracts": hedge_contracts,
        "filled": hedge_filled,
        "price_cents": hedge_price,
        "cost": hedge_cost,
        "order_id": order_result.get("order_id", ""),
        "type": "hedge",
        "edge": 0,
        "relative_edge": 0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "filled" if hedge_filled == hedge_contracts else "partial" if hedge_filled > 0 else "resting",
        "parent_order_id": pos.get("order_id", ""),
    }

    # Save hedge to positions
    positions.append(hedge_entry)
    _save_json(POSITIONS_FILE, positions)

    # Save to trade history
    history = _load_json(TRADE_HISTORY_FILE)
    if not isinstance(history, list):
        history = []
    history.append(hedge_entry)
    _save_json(TRADE_HISTORY_FILE, history)

    return {"success": True, "order_result": order_result, "reason": f"Hedged {filled}x {hedge_side} @ {hedge_price}¢"}


def execute_double(ticker: str) -> dict:
    """
    Double down on an existing position if the edge still exists.
    Re-fetches the market, recalculates edge, and runs through the
    full execute_trade() safety chain.
    """
    positions = _load_json(POSITIONS_FILE)
    if not isinstance(positions, list):
        return {"success": False, "reason": "No positions file"}

    # Find the open position
    pos = None
    for p in positions:
        if p.get("ticker") == ticker and p.get("status") in ("filled", "partial"):
            pos = p
            break

    if not pos:
        return {"success": False, "reason": f"No open position for {ticker}"}

    side = pos.get("side", "yes")
    opp_type = pos.get("type", "unknown")

    # Re-fetch market
    market = get_market(ticker)
    if "error" in market:
        return {"success": False, "reason": f"Market fetch failed: {market['error']}"}

    if side == "yes":
        current_price_cents = market.get("yes_ask", 0)
    else:
        current_price_cents = market.get("no_ask", 0)

    if not current_price_cents or current_price_cents <= 0:
        return {"success": False, "reason": f"No valid ask price for {side} side"}

    # Recalculate edge based on opportunity type
    if opp_type == "weather":
        edge_result = calculate_weather_edge(
            city=pos.get("city", ""),
            forecast_temp=pos.get("forecast_temp", 0),
            threshold=pos.get("threshold", 0),
            direction=pos.get("direction", "above"),
            kalshi_price_cents=current_price_cents,
        )
    else:
        edge_result = calculate_parlay_edge(
            market_title=pos.get("title", ""),
            kalshi_price_cents=current_price_cents,
            sport=pos.get("sport", "nba"),
        )

    if "error" in edge_result:
        return {"success": False, "reason": f"Edge recalculation failed: {edge_result['error']}"}

    relative_edge = edge_result.get("relative_edge", 0)
    if relative_edge < MIN_RELATIVE_EDGE:
        return {"success": False, "reason": "Edge no longer exists"}

    # Build an opportunity dict that mirrors the original for execute_trade()
    opportunity = {
        "ticker": ticker,
        "title": pos.get("title", ""),
        "recommended_side": side,
        "type": opp_type,
        "edge": edge_result.get("edge", 0),
        "relative_edge": relative_edge,
        "edge_result": edge_result,
        # Carry forward weather fields if applicable
        "city": pos.get("city", ""),
        "forecast_temp": pos.get("forecast_temp", 0),
        "threshold": pos.get("threshold", 0),
        "direction": pos.get("direction", ""),
    }

    # Use same contract count as original, at the current price
    contracts = pos.get("filled", 0) or pos.get("contracts", 0)
    if contracts <= 0:
        return {"success": False, "reason": "Original position has no contracts to mirror"}

    total_cost = contracts * current_price_cents / 100.0

    sizing = {
        "contracts": contracts,
        "price_cents": current_price_cents,
        "total_cost": total_cost,
    }

    # Run through the full safety chain via execute_trade()
    return execute_trade(opportunity, sizing)
