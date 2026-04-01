"""
Hustle Agent — API Cost Tracking

Tracks token usage and dollar cost of every Claude API call.
The agent pays for its own thinking out of its balance.
"""

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
COSTS_FILE = STATE_DIR / "api_costs.json"

# Anthropic pricing per million tokens (as of March 2026)
PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    # Fallback for unknown models
    "default": {"input": 3.00, "output": 15.00},
}


def _load_costs() -> list:
    if COSTS_FILE.exists():
        with open(COSTS_FILE, "r") as f:
            return json.load(f)
    return []


def _save_costs(costs: list):
    tmp = COSTS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(costs, f, indent=2)
    tmp.rename(COSTS_FILE)


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate dollar cost for a single API call."""
    rates = PRICING.get(model, PRICING["default"])
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


def record_api_cost(model: str, input_tokens: int, output_tokens: int,
                    cycle: int, purpose: str) -> dict:
    """Record an API call's cost. Returns the cost entry."""
    cost = calculate_cost(model, input_tokens, output_tokens)
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": round(cost, 6),
        "cycle": cycle,
        "purpose": purpose,
    }
    costs = _load_costs()
    costs.append(entry)
    _save_costs(costs)
    return entry


def get_cycle_cost(cycle: int) -> float:
    """Total cost for a specific cycle."""
    return sum(e["cost"] for e in _load_costs() if e["cycle"] == cycle)


def get_burn_rate() -> dict:
    """Compute burn rate statistics."""
    costs = _load_costs()
    if not costs:
        return {
            "total_lifetime_cost": 0,
            "avg_cost_per_cycle": 0,
            "cycles_tracked": 0,
            "daily_cost_estimate": 0,
            "projected_monthly": 0,
        }

    total = sum(e["cost"] for e in costs)
    cycles = set(e["cycle"] for e in costs)
    num_cycles = len(cycles) or 1
    avg_per_cycle = total / num_cycles

    # Daily cost: look at last 24h
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
    daily = sum(e["cost"] for e in costs if e["timestamp"] > cutoff)

    return {
        "total_lifetime_cost": round(total, 4),
        "avg_cost_per_cycle": round(avg_per_cycle, 4),
        "cycles_tracked": num_cycles,
        "daily_cost_estimate": round(daily, 4),
        "projected_monthly": round(daily * 30, 2),
    }


def get_cost_context() -> str:
    """Build cost awareness block for system prompt. Always included."""
    burn = get_burn_rate()
    if burn["total_lifetime_cost"] == 0:
        return "OPERATIONAL COSTS: No API calls tracked yet."

    lines = [
        "OPERATIONAL COSTS (you pay for your own thinking):",
        f"  Lifetime API spend: ${burn['total_lifetime_cost']:.4f}",
        f"  Avg cost/cycle: ${burn['avg_cost_per_cycle']:.4f}",
        f"  Last 24h: ${burn['daily_cost_estimate']:.4f}",
        f"  Projected monthly: ${burn['projected_monthly']:.2f}",
    ]
    return "\n".join(lines)
