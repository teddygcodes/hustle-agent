"""
Hustle Agent — Risk Management

Portfolio-level controls: per-strategy exposure, daily spend limits,
drawdown trigger for capital preservation mode.
"""

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"

MAX_EXPOSURE_PER_STRATEGY = 0.40  # 40% of current balance
MAX_DAILY_SPEND = 30.0
DRAWDOWN_THRESHOLD = 70.0  # capital preservation below this


def get_risk_posture(balance: float) -> str:
    if balance >= 90:
        return "aggressive"
    elif balance >= DRAWDOWN_THRESHOLD:
        return "normal"
    else:
        return "preservation"


def _get_strategy_exposure(ledger: list) -> dict[str, float]:
    """Calculate net exposure (invested - returned) per strategy."""
    exposure = {}
    for txn in ledger:
        strat = txn.get("strategy", "unknown")
        if strat not in exposure:
            exposure[strat] = 0
        if txn["type"] in ("expense", "investment"):
            exposure[strat] += txn["amount"]
        elif txn["type"] in ("income", "return"):
            exposure[strat] -= txn["amount"]
    # Only show positive exposure (net capital still at risk)
    return {k: max(0, v) for k, v in exposure.items()}


def _get_daily_spend(ledger: list) -> float:
    """Sum of today's expenses/investments."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return sum(
        txn["amount"] for txn in ledger
        if txn["type"] in ("expense", "investment")
        and txn["timestamp"].startswith(today)
    )


def check_portfolio_risk(balance: float, ledger: list,
                         proposed_strategy: str, proposed_amount: float,
                         exploration_mode: str = None) -> dict:
    """Check if a proposed spend passes all risk controls."""
    posture = get_risk_posture(balance)

    # Exploration mode cap: $5/action when still exploring
    if exploration_mode == "explore" and proposed_amount > 5.0:
        return {
            "allowed": False,
            "reason": f"EXPLORATION MODE: Max $5.00 per action while building instincts. Proposed: ${proposed_amount:.2f}. Keep bets small and diverse.",
            "risk_posture": posture,
        }

    # Drawdown check
    if posture == "preservation" and proposed_amount > 2.0:
        return {
            "allowed": False,
            "reason": f"CAPITAL PRESERVATION: Balance ${balance:.2f} is below ${DRAWDOWN_THRESHOLD:.0f}. Max spend is $2.00 in this mode.",
            "risk_posture": posture,
        }

    # Daily spend limit
    daily = _get_daily_spend(ledger)
    if daily + proposed_amount > MAX_DAILY_SPEND:
        remaining = max(0, MAX_DAILY_SPEND - daily)
        return {
            "allowed": False,
            "reason": f"DAILY LIMIT: Already spent ${daily:.2f} today. Max ${MAX_DAILY_SPEND:.0f}/day. Remaining: ${remaining:.2f}.",
            "risk_posture": posture,
        }

    # Per-strategy exposure
    exposure = _get_strategy_exposure(ledger)
    current = exposure.get(proposed_strategy, 0)
    max_allowed = balance * MAX_EXPOSURE_PER_STRATEGY
    if current + proposed_amount > max_allowed:
        return {
            "allowed": False,
            "reason": f"CONCENTRATION: Strategy '{proposed_strategy}' exposure would be ${current + proposed_amount:.2f}, exceeding {MAX_EXPOSURE_PER_STRATEGY*100:.0f}% limit (${max_allowed:.2f}).",
            "risk_posture": posture,
        }

    return {"allowed": True, "reason": "OK", "risk_posture": posture}


def get_risk_context(balance: float, ledger: list) -> str:
    """Build risk block for system prompt. Always included."""
    posture = get_risk_posture(balance)
    daily = _get_daily_spend(ledger)
    daily_remaining = max(0, MAX_DAILY_SPEND - daily)
    exposure = _get_strategy_exposure(ledger)
    distance = balance - DRAWDOWN_THRESHOLD

    lines = [
        f"RISK POSTURE: {posture.upper()}",
        f"  Daily spend: ${daily:.2f} / ${MAX_DAILY_SPEND:.0f} (${daily_remaining:.2f} remaining)",
        f"  Drawdown buffer: ${distance:.2f} above ${DRAWDOWN_THRESHOLD:.0f} threshold",
    ]

    if exposure:
        active = {k: v for k, v in exposure.items() if v > 0 and k != "operations"}
        if active:
            lines.append("  Strategy exposure:")
            for strat, amt in sorted(active.items(), key=lambda x: -x[1]):
                pct = (amt / balance * 100) if balance > 0 else 0
                lines.append(f"    {strat}: ${amt:.2f} ({pct:.0f}%)")

    if posture == "preservation":
        lines.append("  *** CAPITAL PRESERVATION MODE: Focus on low-risk, fast-return actions. No speculative bets. Max $2/action. ***")

    return "\n".join(lines)
