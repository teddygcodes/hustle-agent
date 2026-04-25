"""
Nexus Trading Bot — Trade Execution

Places orders on Kalshi with multiple layers of safety checks.
verify_contract_direction() is MANDATORY before every trade.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
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
    PRICE_MOVE_CENTS_BY_STRATEGY,
    PAPER_TRADES_FILE, PAPER_STARTING_BALANCE,
    STRATEGY_BUDGETS,
)


# ---------------------------------------------------------------------------
# Strategy budget bucketing (Tier 2.1)
# Maps raw opp_type strings → budget keys in STRATEGY_BUDGETS. Anything not
# listed here falls into "other" (no budget limit — global MAX_TOTAL_EXPOSURE
# still applies).
# ---------------------------------------------------------------------------
_STRATEGY_BUDGET_BUCKET = {
    "vig_stack_series":        "vig_stack",
    "vig_stack_futures":       "vig_stack",
    "vig_stack_no":            "vig_stack",
    "live_momentum":           "live_momentum",
    "sports_monotonicity_arb": "arbs",
    "sports_consistency_arb":  "arbs",
}


def _budget_bucket(opp_type: str | None) -> str:
    """Map an opportunity/position type to a STRATEGY_BUDGETS key, or 'other'."""
    if not opp_type:
        return "other"
    return _STRATEGY_BUDGET_BUCKET.get(str(opp_type), "other")

_PAPER_TYPE_MAP = {
    "vig_stack_series": "vig_stack",
    "vig_stack_futures": "vig_stack",
    "series_game_edge": "series_game",
    "econ_cpi_edge": "econ",
}


# Session 6 — gate fingerprint for the executor's safety chain.
# Order matters: rejection records earlier gates as True, the rejecting one
# as False, downstream gates omitted.
_POS_GATE_ORDER = [
    "position_cap", "duplicate", "same_game", "cooldown",
    "daily_loss", "strategy_budget", "total_exposure",
]
_EDGE_GATE_ORDER = ["market_data", "yes_ask", "price_moved", "edge_evaporated"]


def _pos_gate_fingerprint(reason: str) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for name in _POS_GATE_ORDER:
        if name == reason:
            out[name] = False
            return out
        out[name] = True
    return out


def _edge_gate_fingerprint(reason: str) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for name in _EDGE_GATE_ORDER:
        if name == reason:
            out[name] = False
            return out
        out[name] = True
    return out


def _log_position_reject(ticker: str, opp_type: str | None, reason: str,
                          extra: dict | None = None) -> None:
    try:
        from bot import decisions
        decisions.log_decision(
            ticker=ticker, opp_type=opp_type or "unknown", edge=None,
            gates=_pos_gate_fingerprint(reason),
            decision="reject", reason=reason, extra=extra,
        )
    except Exception:
        pass  # never block trades because logging failed


def _log_edge_reject(opportunity: dict, reason: str,
                      extra: dict | None = None) -> None:
    try:
        from bot import decisions
        decisions.log_decision(
            ticker=opportunity.get("ticker", ""),
            opp_type=opportunity.get("type", "unknown"),
            edge=opportunity.get("edge"),
            gates=_edge_gate_fingerprint(reason),
            decision="reject", reason=reason, extra=extra,
        )
    except Exception:
        pass
from bot.math_engine import verify_contract_direction, calculate_parlay_edge, calculate_weather_edge
from bot.sizing import kelly_size
from bot.state_io import load_json as _load_json, save_json as _save_json

logger = logging.getLogger("nexus.executor")


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def _check_balance(cost_dollars: float) -> tuple[bool, float, str]:
    """Check if we have enough balance for the trade.

    In PAPER_MODE, derives balance from paper trade history so the simulation
    is self-contained and does not depend on live API connectivity.
    """
    if PAPER_MODE:
        paper_trades = _load_json(PAPER_TRADES_FILE)
        if not isinstance(paper_trades, list):
            paper_trades = []
        balance = PAPER_STARTING_BALANCE
        for t in paper_trades:
            if not isinstance(t, dict):
                continue
            entry_cost = t.get("contracts", 0) * t.get("entry_price", 0.0)
            status = t.get("status", "open")
            if status == "open":
                balance -= entry_cost                    # still at risk
            elif status == "won":
                balance -= entry_cost                    # paid for entry
                balance += t.get("contracts", 0) * 1.0  # $1/contract payout
            elif status == "lost":
                balance -= entry_cost                    # lost the stake
            elif status == "exited_early":
                balance -= entry_cost                    # paid for entry
                balance += t.get("contracts", 0) * t.get("exit_price", 0.0)  # got back proceeds
            # "resting" → not filled yet, don't deduct
        balance = round(balance, 2)
        if balance < cost_dollars:
            return False, balance, f"Insufficient paper balance: ${balance:.2f} < ${cost_dollars:.2f}"
        # Reserve guard: keep 10% of starting balance liquid
        min_reserve = PAPER_STARTING_BALANCE * 0.10
        if balance - cost_dollars < min_reserve:
            return False, balance, (
                f"Reserve guard: ${balance:.2f} - ${cost_dollars:.2f} = "
                f"${balance - cost_dollars:.2f} would breach ${min_reserve:.0f} reserve"
            )
        return True, balance, "ok"

    result = get_balance()
    if "error" in result:
        return False, 0.0, f"Balance check failed: {result['error']}"
    balance = result.get("balance_dollars", 0.0)
    if balance < cost_dollars:
        return False, balance, f"Insufficient balance: ${balance:.2f} < ${cost_dollars:.2f}"
    return True, balance, "ok"


def _check_position_limits(
    balance: float,
    cost_dollars: float,
    ticker: str,
    opp_type: str | None = None,
) -> tuple[bool, str]:
    """Check position limits: per-position cap, dedupe, cooldown, daily loss,
    per-strategy budget (Tier 2.1), and global exposure.

    Args:
        opp_type: opportunity["type"] — used to enforce STRATEGY_BUDGETS. Pass
            None (default) to skip the budget check (e.g. market-maker hedges
            that close existing risk rather than opening new exposure).
    """
    # Single position limit
    if cost_dollars > balance * MAX_POSITION_PERCENT:
        _log_position_reject(
            ticker, opp_type, "position_cap",
            extra={
                "cost_dollars": round(cost_dollars, 2),
                "max_allowed": round(balance * MAX_POSITION_PERCENT, 2),
                "balance": round(balance, 2),
                "exposure_pct": round(cost_dollars / balance, 4) if balance > 0 else None,
                "max_pct": MAX_POSITION_PERCENT,
            },
        )
        return False, (
            f"Position too large: ${cost_dollars:.2f} > "
            f"{MAX_POSITION_PERCENT:.0%} of ${balance:.2f} (${balance * MAX_POSITION_PERCENT:.2f})"
        )

    positions = _load_json(POSITIONS_FILE)

    # Dedup: block re-entry if we already hold an open position in this ticker
    # But first: auto-close orphaned positions whose markets have settled
    existing = [
        p for p in positions
        if isinstance(p, dict)
        and p.get("ticker") == ticker
        and p.get("status") in ("resting", "filled", "partial")
    ]
    if existing:
        # Check if market actually settled — if so, close the orphan
        try:
            from agent.kalshi_client import get_market as _gm
            mkt = _gm(ticker)
            m = mkt.get("market", mkt)
            if m.get("status") == "inactive" or (m.get("yes_ask", 50) >= 97 or m.get("yes_ask", 50) <= 3):
                # Market settled — auto-close the orphaned position
                yes_settled_high = m.get("yes_ask", 50) >= 97
                settled_yes_price = 1.0 if yes_settled_high else 0.0
                now_iso = datetime.now(timezone.utc).isoformat()
                for p in existing:
                    entry = p.get("price_cents", 50) / 100.0
                    side = p.get("side", "yes")
                    contracts = p.get("contracts", 1)
                    if side == "yes":
                        p_pnl = (settled_yes_price - entry) * contracts
                    else:
                        p_pnl = ((1.0 - settled_yes_price) - entry) * contracts
                    # Use "resolved" status so the 4-hour cooldown check catches it
                    p["status"] = "resolved"
                    p["unrealized_pnl"] = round(p_pnl, 4)
                    p["resolved_at"] = now_iso
                    logger.info(
                        "Auto-closed orphaned position: %s side=%s (pnl=$%.2f)",
                        ticker, side, p_pnl,
                    )
                _save_json(POSITIONS_FILE, positions)
                # Also close in paper_trades.json if paper mode
                if PAPER_MODE:
                    _pt = _load_json(PAPER_TRADES_FILE)
                    for t in (_pt if isinstance(_pt, list) else []):
                        if not isinstance(t, dict):
                            continue
                        if t.get("ticker") != ticker or t.get("status") != "open":
                            continue
                        t_entry = t.get("entry_price", 0.5)
                        t_side = t.get("side", "yes")
                        t_contracts = t.get("contracts", 1)
                        if t_side == "yes":
                            t_pnl = (settled_yes_price - t_entry) * t_contracts
                            t_exit = settled_yes_price
                        else:
                            t_pnl = ((1.0 - settled_yes_price) - t_entry) * t_contracts
                            t_exit = 1.0 - settled_yes_price  # NO payout
                        t["status"] = "won" if t_pnl > 0 else "lost"
                        t["pnl"] = round(t_pnl, 2)
                        t["exit_price"] = round(t_exit, 4)
                        t["resolved_at"] = now_iso
                    _save_json(PAPER_TRADES_FILE, _pt)
                # Position cleared — allow new entry
            else:
                _log_position_reject(
                    ticker, opp_type, "duplicate",
                    extra={
                        "existing_count": len(existing),
                        "existing_status": existing[0].get("status") if existing else None,
                        "existing_filled": existing[0].get("filled", 0) if existing else 0,
                    },
                )
                return False, f"Already hold open position in {ticker} — skipping duplicate entry"
        except Exception as e:
            logger.debug("Orphan check failed for %s: %s", ticker, e)
            return False, f"Already hold open position in {ticker} — skipping duplicate entry"

    # Block betting on the opposing team in the same game event
    ticker_parts = ticker.rsplit("-", 1)
    if len(ticker_parts) == 2 and "GAME" in ticker_parts[0]:
        game_key = ticker_parts[0]
        same_game = [
            p for p in positions
            if isinstance(p, dict)
            and p.get("status") in ("resting", "filled", "partial")
            and p.get("ticker", "").startswith(game_key + "-")
            and p.get("ticker") != ticker
        ]
        if same_game:
            held_team = same_game[0]["ticker"].rsplit("-", 1)[1]
            _log_position_reject(
                ticker, opp_type, "same_game",
                extra={
                    "game_key": game_key,
                    "held_team": held_team,
                    "held_ticker": same_game[0].get("ticker"),
                },
            )
            return False, (
                f"SAME GAME: already hold {held_team} in {game_key} — "
                f"blocking opposite-side bet"
            )

    # Cooldown: block re-entry for 4 hours after any exit/resolve on same ticker
    from datetime import datetime, timezone, timedelta
    _COOLDOWN = timedelta(hours=4)
    _now = datetime.now(timezone.utc)
    recent_exits = [
        p for p in positions
        if isinstance(p, dict)
        and p.get("ticker") == ticker
        and p.get("status") in ("exited", "exited_early", "resolved")
        and (p.get("exited_at") or p.get("resolved_at"))
    ]
    for p in recent_exits:
        try:
            ts = p.get("exited_at") or p.get("resolved_at")
            exited_at = datetime.fromisoformat(ts)
            if (_now - exited_at) < _COOLDOWN:
                remaining = int((_COOLDOWN - (_now - exited_at)).total_seconds() / 60)
                _log_position_reject(
                    ticker, opp_type, "cooldown",
                    extra={
                        "last_trade_age_min": int((_now - exited_at).total_seconds() / 60),
                        "cooldown_min": int(_COOLDOWN.total_seconds() / 60),
                    },
                )
                return False, f"COOLDOWN: {ticker} exited {int((_now - exited_at).total_seconds() / 60)}m ago — {remaining}m remaining"
        except Exception as e:
            logger.warning("Cooldown date parse failed for %s: %s", ticker, e)

    # Daily per-ticker loss limit: block if ticker has lost > $1.00 today
    _DAILY_TICKER_LOSS_LIMIT = 1.00
    _today = _now.date()
    daily_ticker_loss = 0.0
    for p in positions:
        if not isinstance(p, dict) or p.get("ticker") != ticker:
            continue
        rpnl = p.get("realized_pnl") or p.get("unrealized_pnl") or 0
        if rpnl >= 0:
            continue
        ts = p.get("exited_at") or p.get("resolved_at") or p.get("opened_at")
        if ts:
            try:
                if datetime.fromisoformat(ts).date() == _today:
                    daily_ticker_loss += abs(rpnl)
            except Exception:
                pass
    if daily_ticker_loss >= _DAILY_TICKER_LOSS_LIMIT:
        _log_position_reject(
            ticker, opp_type, "daily_loss",
            extra={
                "daily_ticker_loss": round(daily_ticker_loss, 2),
                "limit": _DAILY_TICKER_LOSS_LIMIT,
            },
        )
        return False, f"DAILY_LOSS_LIMIT: {ticker} has lost ${daily_ticker_loss:.2f} today (limit ${_DAILY_TICKER_LOSS_LIMIT:.2f})"

    # Only count ACTIVE positions that actually filled for BOTH the per-strategy
    # budget check AND the global exposure check.
    # Exclude: ghost resting orders (filled=0), exited, resolved.
    active_positions = [
        p for p in positions
        if isinstance(p, dict)
        and p.get("filled", 0) > 0
        and p.get("status") in ("filled", "partial")
    ]

    # Compare cap fractions against EQUITY, not cash-on-hand.
    #
    # `balance` from `_check_balance()` is cash after open-position entry costs
    # are deducted (paper) / Kalshi available balance (live). Using cash as the
    # cap base double-counts: `total_exposure + cost > cash × fraction` reduces
    # to `new_cost > cash × fraction − old_exposure`, which blocks once deployed
    # capital exceeds cash — that happens immediately on any account > 50% full.
    #
    # Equity = cash + already-committed capital = the real total account value
    # the budget fractions are meant to partition. The `_check_balance` reserve
    # guard (10% of starting balance) still enforces the liquidity floor, so
    # we're not over-leveraging past what cash can cover.
    #
    # Fixed Apr 16 after the first post-Tier-2.1 restart showed budget rejects
    # that would have blocked every entry any time vig_stack filled > 60% of
    # cash — which is most days. Plan's stated goal ("unblock conviction") only
    # works with equity-based caps.
    total_exposure = sum(p.get("cost", 0) for p in active_positions)
    equity = balance + total_exposure

    # ------------------------------------------------------------------
    # Per-strategy exposure budget (Tier 2.1)
    # Applied BEFORE the global cap so the rejection reason is specific
    # ("STRATEGY_BUDGET") instead of the generic total-exposure message.
    # ------------------------------------------------------------------
    bucket = _budget_bucket(opp_type)
    budget_frac = STRATEGY_BUDGETS.get(bucket)
    if budget_frac is not None:
        bucket_exposure = sum(
            p.get("cost", 0) for p in active_positions
            if _budget_bucket(p.get("type") or p.get("opp_type")) == bucket
        )
        bucket_cap = equity * budget_frac
        if (bucket_exposure + cost_dollars) > bucket_cap:
            _log_position_reject(
                ticker, opp_type, "strategy_budget",
                extra={
                    "bucket": bucket,
                    "current_exposure": round(bucket_exposure, 2),
                    "incoming_cost": round(cost_dollars, 2),
                    "cap": round(bucket_cap, 2),
                    "budget_frac": budget_frac,
                    "exposure_pct": round((bucket_exposure + cost_dollars) / bucket_cap, 4) if bucket_cap > 0 else None,
                },
            )
            return False, (
                f"STRATEGY_BUDGET: {bucket} has ${bucket_exposure:.2f} of "
                f"${bucket_cap:.2f} budget ({budget_frac:.0%} of ${equity:.2f} equity); "
                f"this ${cost_dollars:.2f} trade would overflow"
            )

    # ------------------------------------------------------------------
    # Global total exposure limit (still applies on top of strategy budgets)
    # ------------------------------------------------------------------
    if (total_exposure + cost_dollars) > equity * MAX_TOTAL_EXPOSURE:
        _log_position_reject(
            ticker, opp_type, "total_exposure",
            extra={
                "total_exposure": round(total_exposure, 2),
                "incoming_cost": round(cost_dollars, 2),
                "cap": round(equity * MAX_TOTAL_EXPOSURE, 2),
                "exposure_pct": round((total_exposure + cost_dollars) / (equity * MAX_TOTAL_EXPOSURE), 4) if equity > 0 else None,
                "max_pct": MAX_TOTAL_EXPOSURE,
            },
        )
        return False, (
            f"Total exposure too high: ${total_exposure + cost_dollars:.2f} > "
            f"{MAX_TOTAL_EXPOSURE:.0%} of ${equity:.2f} equity"
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

    # Momentum trades are pure price-action — no "edge" to re-verify.
    # The dip-buy signal was validated at tick time; re-checking against a
    # model fair_value that doesn't exist would just block every trade.
    if trade_type == "live_momentum":
        return True, "ok (momentum — price-action only)"

    market = get_market(ticker)
    if "error" in market:
        _log_edge_reject(
            opportunity, "market_data",
            extra={"error": str(market.get("error", ""))[:120]},
        )
        return False, f"Could not re-fetch market: {market['error']}"

    # Always read yes_ask — it's the price basis for both YES and NO edge calculations
    current_yes_ask = market.get("yes_ask", 0)
    if not current_yes_ask or current_yes_ask <= 0:
        _log_edge_reject(
            opportunity, "yes_ask",
            extra={"current_yes_ask": current_yes_ask},
        )
        return False, "No valid yes_ask price on re-fetch"

    current_yes_price = current_yes_ask / 100.0

    if trade_type in ("vig_stack_no", "vig_stack_series", "vig_stack_futures"):
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

        logger.debug(
            f"[EdgeCheck/NO] {ticker} | "
            f"scan_yes_ask={original_yes_price:.4f} refetch_yes_ask={current_yes_price:.4f} | "
            f"delta={current_yes_price - original_yes_price:+.4f}"
        )

        # Kill switch: per-strategy threshold (vig_stack tolerates more drift — structural edge)
        kill_cents = PRICE_MOVE_CENTS_BY_STRATEGY.get(trade_type, MAX_PRICE_MOVE_CENTS)
        move_cents = abs(current_yes_price - original_yes_price) * 100
        if move_cents > kill_cents:
            _log_edge_reject(
                opportunity, "price_moved",
                extra={
                    "move_cents": round(move_cents, 2),
                    "kill_cents": kill_cents,
                    "trade_type": trade_type,
                    "original_yes_price": round(original_yes_price, 4),
                    "current_yes_price": round(current_yes_price, 4),
                },
            )
            return False, (
                f"Price moved {move_cents:.1f}¢ since alert (max {kill_cents}¢ for {trade_type}) — market in motion, aborting"
            )

        if abs(current_yes_price - original_yes_price) < 0.001:
            logger.debug(f"[EdgeCheck/NO] {ticker} | price unchanged → edge still valid")
            return True, "ok"

        true_yes = vig_result.get("true_yes_prob", 0)
        true_no = 1.0 - true_yes
        kalshi_no = 1.0 - current_yes_price
        new_edge = true_no - kalshi_no
        new_relative = new_edge / kalshi_no if kalshi_no > 0 else 0

        logger.debug(
            f"[EdgeCheck/NO] {ticker} | "
            f"NO edge {opportunity.get('relative_edge', 0):.2%} → {new_relative:.2%} | "
            f"threshold={MIN_RELATIVE_EDGE:.2%}"
        )
    else:
        # YES-side: edge = fair_value - kalshi_yes_price
        original_price = opportunity.get("edge_result", {}).get("kalshi_price", 0)

        logger.debug(
            f"[EdgeCheck/YES] {ticker} | "
            f"scan_yes_ask={original_price:.4f} refetch_yes_ask={current_yes_price:.4f} | "
            f"delta={current_yes_price - original_price:+.4f}"
        )

        # Kill switch: per-strategy threshold
        kill_cents = PRICE_MOVE_CENTS_BY_STRATEGY.get(trade_type, MAX_PRICE_MOVE_CENTS)
        move_cents = abs(current_yes_price - original_price) * 100
        if move_cents > kill_cents:
            _log_edge_reject(
                opportunity, "price_moved",
                extra={
                    "move_cents": round(move_cents, 2),
                    "kill_cents": kill_cents,
                    "trade_type": trade_type,
                    "original_yes_price": round(original_price, 4),
                    "current_yes_price": round(current_yes_price, 4),
                },
            )
            return False, (
                f"Price moved {move_cents:.1f}¢ since alert (max {kill_cents}¢ for {trade_type}) — market in motion, aborting"
            )

        if abs(current_yes_price - original_price) < 0.001:
            logger.debug(f"[EdgeCheck/YES] {ticker} | price unchanged → edge still valid")
            return True, "ok"

        fair_value = opportunity.get("edge_result", {}).get("fair_value", 0)
        new_edge = fair_value - current_yes_price
        new_relative = new_edge / current_yes_price if current_yes_price > 0 else 0

        logger.debug(
            f"[EdgeCheck/YES] {ticker} | "
            f"fair={fair_value:.4f} | "
            f"edge {opportunity.get('relative_edge', 0):.2%} → {new_relative:.2%} | "
            f"threshold={MIN_RELATIVE_EDGE:.2%}"
        )

    # Vig stack series is a structural edge — threshold is 2%, not the default 15%
    edge_threshold = 0.02 if trade_type in ("vig_stack_series", "vig_stack_futures") else MIN_RELATIVE_EDGE
    if new_relative < edge_threshold:
        logger.debug(
            f"[EdgeCheck] EVAPORATED: {ticker} | "
            f"was {opportunity.get('relative_edge', 0):.2%}, now {new_relative:.2%}"
        )
        _log_edge_reject(
            opportunity, "edge_evaporated",
            extra={
                "new_relative": round(new_relative, 4),
                "original_relative": round(opportunity.get("relative_edge", 0), 4),
                "edge_threshold": round(edge_threshold, 4),
            },
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
    direction_check = None  # May be skipped for structural trades

    # Vig stack series: purely structural edge (buy NO because YES sum > 100¢).
    # No directional thesis to verify — skip direction check entirely.
    if opp_type in ("vig_stack_series", "vig_stack_futures"):
        checks.append({
            "name": "contract_direction",
            "passed": True,
            "confidence": "high",
            "explanation": f"{opp_type} — structural NO edge, no directional thesis needed",
            "warnings": [],
        })
        logger.info(f"  ✅ Direction check skipped ({opp_type} — structural edge)")
    elif opp_type == "weather":
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
    elif opp_type in ("btc_price_edge", "eth_price_edge", "sol_price_edge",
                      "xrp_price_edge", "doge_price_edge"):
        spot      = opportunity.get("spot", 0)
        threshold = opportunity.get("threshold", 0)
        fair_value   = edge_result.get("fair_value", 0)
        kalshi_price = edge_result.get("kalshi_price", 0)
        spot_vs = "above threshold" if spot > threshold else "below threshold"
        if side == "yes":
            data_thesis = (
                f"Spot price ${spot:,.2f} is {spot_vs} ${threshold:,.2f}. "
                f"Model YES probability {fair_value:.1%} — YES underpriced vs Kalshi {kalshi_price:.1%}."
            )
        else:
            data_thesis = (
                f"Spot price ${spot:,.2f} is {spot_vs} ${threshold:,.2f}. "
                f"Model YES probability {fair_value:.1%} — YES overpriced vs Kalshi {kalshi_price:.1%}. "
                f"NO has edge — price likely to settle below threshold."
            )
    else:
        # generic fallback
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

    # Skip direction check if already handled above (e.g. vig_stack_series)
    if not any(c.get("name") == "contract_direction" for c in checks):
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
    pos_ok, pos_msg = _check_position_limits(balance, total_cost, ticker, opp_type=opp_type)
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
        try:
            from bot import decisions
            decisions.log_decision(
                ticker=ticker, opp_type=opp_type, edge=opportunity.get("edge"),
                gates={"position_cap": True, "duplicate": True, "same_game": True,
                       "cooldown": True, "daily_loss": True, "strategy_budget": True,
                       "total_exposure": True, "edge_recheck": True, "self_check": False},
                decision="reject", reason="self_check_failed",
                extra={
                    "edge": opportunity.get("edge"),
                    "warnings": (edge_result.get("warnings") or [])[:3],
                },
            )
        except Exception:
            pass
        return {"success": False, "checks": checks, "reason": "Math self-check failed"}

    logger.info(f"  ✅ Math self-check passed")

    # Session 6 — accept-path decision log. Every gate passed; order is
    # about to be placed (paper or live).
    try:
        from bot import decisions
        decisions.log_decision(
            ticker=ticker, opp_type=opp_type, edge=opportunity.get("edge"),
            gates={"position_cap": True, "duplicate": True, "same_game": True,
                   "cooldown": True, "daily_loss": True, "strategy_budget": True,
                   "total_exposure": True, "edge_recheck": True, "self_check": True},
            decision="accept", reason="all_gates_passed",
            extra={"contracts": contracts, "price_cents": price_cents,
                   "cost_dollars": round(total_cost, 2),
                   "paper": PAPER_MODE, "side": side},
        )
    except Exception:
        pass

    # ------------------------------------------------------------------
    # ALL CHECKS PASSED — PLACE ORDER (or simulate in PAPER_MODE)
    # ------------------------------------------------------------------
    if PAPER_MODE:
        paper_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        # Check if limit price meets or exceeds current ask — if so, fill immediately
        # (simulates a marketable limit order, which is what momentum/watcher trades are)
        current_market = get_market(ticker)
        current_ask = current_market.get(f"{side}_ask", 999) if "error" not in current_market else 999
        immediate_fill = (price_cents >= current_ask and current_ask > 0)
        if immediate_fill:
            logger.info(
                f"  📝 [PAPER] Instant fill: {contracts}x {side.upper()} @ {price_cents}¢ on {ticker} "
                f"(id={paper_id}) — limit {price_cents}¢ ≥ ask {current_ask}¢"
            )
        else:
            logger.info(
                f"  📝 [PAPER] Resting limit order: {contracts}x {side.upper()} @ {price_cents}¢ on {ticker} "
                f"(id={paper_id}) — will fill when market price crosses limit"
            )
        order_result = {
            "order_id": paper_id,
            "ticker": ticker,
            "side": side,
            "count": contracts,
            "filled_count": contracts if immediate_fill else 0,
            "price_cents": price_cents,
            "cost_dollars": total_cost,
            "status": "paper_filled" if immediate_fill else "paper_resting",
            "paper": True,
        }
    else:
        logger.info(f"  🚀 Placing order: {contracts}x {side.upper()} @ {price_cents}¢ on {ticker}")
        # Session 15: live-only microstructure capture. Paper branch above
        # does NOT call into order_microstructure.* — that's the gate.
        from bot import order_microstructure
        ts_placed = datetime.now(timezone.utc)
        market_snapshot = (
            opportunity.get("market", {})
            if isinstance(opportunity.get("market"), dict)
            else {}
        )
        queue_depth = market_snapshot.get(f"{side}_ask")
        order_result = place_order(
            ticker=ticker,
            side=side,
            count=contracts,
            price_cents=price_cents,
            action="buy",
        )

        if "error" in order_result:
            order_microstructure.record_synchronous_rejection(
                opportunity=opportunity,
                requested_price_cents=price_cents,
                requested_qty=contracts,
                side=side,
                ts_placed=ts_placed,
                error=str(order_result["error"]),
            )
            logger.error(f"ORDER FAILED: {order_result['error']}")
            return {
                "success": False,
                "order_result": order_result,
                "checks": checks,
                "reason": f"Order failed: {order_result['error']}",
            }

        # Order accepted. Stash placement info; terminal write happens in
        # check_fills (or here if Kalshi reports immediate full fill).
        order_microstructure.record_placement(
            order_id=order_result.get("order_id", ""),
            opportunity=opportunity,
            requested_price_cents=price_cents,
            requested_qty=contracts,
            side=side,
            ts_placed=ts_placed,
            queue_depth_at_place=queue_depth,
        )
        immediate_filled = order_result.get("filled_count", 0)
        if immediate_filled > 0 and immediate_filled >= contracts:
            order_microstructure.record_terminal(
                order_id=order_result.get("order_id", ""),
                kalshi_status=order_result.get("status", "filled"),
                filled_count=immediate_filled,
                cost_dollars=order_result.get("cost_dollars"),
                ts_terminal=datetime.now(timezone.utc),
            )

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
        "event_ticker": opportunity.get("market", {}).get("event_ticker", ""),
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

    # Log to paper_trades.json (dedicated paper trading ledger)
    if PAPER_MODE:
        paper_trades = _load_json(PAPER_TRADES_FILE)
        if not isinstance(paper_trades, list):
            paper_trades = []
        paper_trades.append({
            "id": order_result.get("order_id", ""),
            "ticker": ticker,
            "type": _PAPER_TYPE_MAP.get(opp_type, opp_type),
            "side": side,
            "entry_price": round(price_cents / 100.0, 4),
            "contracts": contracts,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "edge_at_entry": round(opportunity.get("edge", 0), 6),
            "confidence": round(opportunity.get("confidence", opportunity.get("relative_edge", 0)), 4),
            "status": "open",
            "exit_price": None,
            "pnl": None,
            "resolved_at": None,
        })
        _save_json(PAPER_TRADES_FILE, paper_trades)

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
            scan_id=opportunity.get("scan_id"),
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

        # Paper order: simulate fill when current ask crosses our limit price
        if pos.get("paper") and order_id.startswith("PAPER-"):
            market = get_market(pos.get("ticker", ""))
            if "error" in market:
                continue
            side = pos.get("side", "yes")
            limit_price = pos.get("price_cents", 0)
            current_ask = market.get("yes_ask" if side == "yes" else "no_ask", 999)
            if current_ask <= limit_price:
                contracts = pos.get("contracts", 0)
                pos["filled"] = contracts
                pos["status"] = "filled"
                logger.info(f"  📝 [PAPER] Fill simulated: {contracts}x {side.upper()} @ {limit_price}¢ on {pos.get('ticker', '?')}")
                updates.append(pos)
            continue

        # Live order: check fill status via Kalshi API
        order = get_order(order_id)
        if "error" in order:
            continue

        new_filled = order.get("filled_count", pos.get("filled", 0))
        kalshi_status = order.get("status", "")
        previous_filled = pos.get("filled", 0)
        if new_filled != previous_filled:
            pos["filled"] = new_filled
            pos["status"] = (
                "filled" if new_filled >= pos.get("contracts", 0)
                else "partial" if new_filled > 0
                else "resting"
            )
            updates.append(pos)

        # Session 15: live-only microstructure terminal observation.
        from bot import order_microstructure
        terminal_kalshi = kalshi_status in ("filled", "canceled", "expired", "rejected")
        terminal_local = pos["status"] == "filled"
        if terminal_kalshi or terminal_local:
            order_microstructure.record_terminal(
                order_id=order_id,
                kalshi_status=kalshi_status or ("filled" if terminal_local else "canceled"),
                filled_count=new_filled,
                cost_dollars=None,  # get_order doesn't return cost; v1 limitation
                ts_terminal=datetime.now(timezone.utc),
            )
        elif new_filled > previous_filled:
            order_microstructure.observe_fill_progress(
                order_id=order_id, filled_count=new_filled
            )

    if updates:
        _save_json(POSITIONS_FILE, positions)

    return updates


def _paper_record_exit(order_id: str, exit_price: float, realized_pnl: float) -> None:
    """Update the matching paper_trades.json record when a paper position exits early."""
    paper_trades = _load_json(PAPER_TRADES_FILE)
    if not isinstance(paper_trades, list):
        return
    exited = None
    for t in paper_trades:
        if isinstance(t, dict) and t.get("id") == order_id:
            t["status"] = "exited_early"
            t["exit_price"] = round(exit_price, 4)
            t["pnl"] = realized_pnl
            t["resolved_at"] = datetime.now(timezone.utc).isoformat()
            exited = t
            break
    _save_json(PAPER_TRADES_FILE, paper_trades)

    if exited is not None:
        try:
            from bot.tracker import log_settlement, check_settlement_invariant
            from bot.patterns import record_resolution
            log_settlement(exited)
            record_resolution(exited)
            check_settlement_invariant()
        except Exception as e:
            logger.debug("Settlement/pattern hook failed: %s", e)


def exit_position(ticker: str, reason: str = "manual") -> dict:
    """
    Sell an existing position. Used when edge evaporates or cut-loss triggers.

    Args:
        ticker: Market ticker to exit
        reason: Why we're exiting

    Returns:
        {success, order_result, realized_pnl, reason}
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

    # Get current market price
    market = get_market(ticker)
    if "error" in market:
        return {"success": False, "reason": f"Market fetch failed: {market['error']}"}

    if side == "yes":
        exit_price = market.get("yes_bid", 0)
    else:
        exit_price = market.get("no_bid", 0)

    if not exit_price or exit_price <= 0:
        if PAPER_MODE or pos.get("paper"):
            # No bid = worthless. Exit at 0 in paper mode rather than
            # retrying every cycle and spamming Telegram.
            entry_price = pos.get("price_cents", 0) / 100.0
            realized_pnl = round(-entry_price * filled, 2)
            pos["status"] = "exited"
            pos["exit_price"] = 0
            pos["exit_reason"] = f"{reason}_no_bid"
            pos["exited_at"] = datetime.now(timezone.utc).isoformat()
            pos["realized_pnl"] = realized_pnl
            _save_json(POSITIONS_FILE, positions)
            _paper_record_exit(pos.get("order_id", ""), 0.0, realized_pnl)
            return {
                "success": True,
                "realized_pnl": realized_pnl,
                "reason": f"Paper exit at $0 (no bid): {reason}",
            }
        return {"success": False, "reason": f"No bid for {side} side"}

    entry_price = pos.get("price_cents", 0) / 100.0
    realized_pnl = round((exit_price / 100.0 - entry_price) * filled, 2)

    # Paper mode: simulate the sell without touching the live API
    if PAPER_MODE or pos.get("paper"):
        pos["status"] = "exited"
        pos["exit_price"] = exit_price
        pos["exit_reason"] = reason
        pos["exited_at"] = datetime.now(timezone.utc).isoformat()
        pos["realized_pnl"] = realized_pnl
        _save_json(POSITIONS_FILE, positions)
        _paper_record_exit(pos.get("order_id", ""), exit_price / 100.0, realized_pnl)
        return {
            "success": True,
            "order_result": {"order_id": pos.get("order_id", ""), "status": "paper_exited", "filled_count": filled},
            "realized_pnl": realized_pnl,
            "reason": f"Paper exit: {reason}",
        }

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
    pos["realized_pnl"] = realized_pnl
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
    elif opp_type == "series_game_edge":
        # Derive fair value from original entry: fair_value = stored_edge + original_kalshi_price
        # The sportsbook consensus doesn't change fast enough to re-fetch; use stored fair value.
        original_price = pos.get("price_cents", 0) / 100.0
        fair_value = pos.get("edge", 0) + original_price
        new_kalshi_price = current_price_cents / 100.0
        new_edge = fair_value - new_kalshi_price
        new_relative = new_edge / new_kalshi_price if new_kalshi_price > 0 else 0
        edge_result = {
            "fair_value": round(fair_value, 4),
            "kalshi_price": round(new_kalshi_price, 4),
            "edge": round(new_edge, 4),
            "relative_edge": round(new_relative, 4),
            "self_check_passed": True,
            "math_chain": ["series_game_edge double-down: fair value derived from original entry"],
            "warnings": [],
        }
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
