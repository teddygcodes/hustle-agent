"""
Hustle Agent — Projection System

Before spending money, the agent must project outcomes.
After resolution, it records what actually happened for calibration.
"""

import json
import uuid
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
PROJECTIONS_FILE = STATE_DIR / "projections.json"

# Strategy-type weights for verdict calculation
STRATEGY_WEIGHTS = {
    "kalshi": {"time_weight": 0.3, "data_weight": 0.4, "confidence_weight": 0.3},
    "product_sale": {"time_weight": 0.2, "data_weight": 0.3, "confidence_weight": 0.5},
    "service": {"time_weight": 0.2, "data_weight": 0.2, "confidence_weight": 0.6},
    "content": {"time_weight": 0.1, "data_weight": 0.3, "confidence_weight": 0.6},
    "arbitrage": {"time_weight": 0.4, "data_weight": 0.4, "confidence_weight": 0.2},
    "default": {"time_weight": 0.25, "data_weight": 0.35, "confidence_weight": 0.4},
}

VERDICTS = ["strong_buy", "lean_yes", "coin_flip", "lean_no", "hard_pass"]


def _load() -> list:
    if PROJECTIONS_FILE.exists():
        with open(PROJECTIONS_FILE, "r") as f:
            return json.load(f)
    return []


def _save(projections: list):
    tmp = PROJECTIONS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(projections, f, indent=2)
    tmp.rename(PROJECTIONS_FILE)


def _compute_verdict(roi_percent: float, confidence: int, time_days: float,
                     strategy_type: str, operational_overhead: float,
                     capital_velocity_cost: float) -> str:
    """Compute verdict based on strategy-weighted scoring."""
    weights = STRATEGY_WEIGHTS.get(strategy_type, STRATEGY_WEIGHTS["default"])

    # Normalize inputs to 0-1 scores
    roi_score = min(max(roi_percent / 100, -1), 2)  # cap at -100% to +200%
    time_score = max(0, 1 - (time_days / 30))  # faster = better, 30d+ = 0
    conf_score = confidence / 100

    # Deductions for overhead
    overhead_penalty = min(operational_overhead / max(roi_percent * 0.01, 0.01), 0.5)
    velocity_penalty = min(capital_velocity_cost / max(roi_percent * 0.01, 0.01), 0.3)

    weighted = (
        roi_score * weights["data_weight"]
        + time_score * weights["time_weight"]
        + conf_score * weights["confidence_weight"]
        - overhead_penalty * 0.2
        - velocity_penalty * 0.1
    )

    if weighted >= 0.65:
        return "strong_buy"
    elif weighted >= 0.45:
        return "lean_yes"
    elif weighted >= 0.30:
        return "coin_flip"
    elif weighted >= 0.15:
        return "lean_no"
    else:
        return "hard_pass"


def create_projection(
    action: str,
    cost: float,
    strategy_type: str,
    expected_return: float,
    estimated_days: float,
    confidence: int,
    assumptions: list[str],
    risks: list[str],
    comparables: str,
    bull_case: str,
    bear_case: str,
    research_summary: str,
    current_balance: float,
    operational_cost_per_cycle: float,
    calibration_multiplier: float = 1.0,
) -> dict:
    """Create and store a projection. Returns the full projection."""
    expected_profit = expected_return - cost
    roi_percent = (expected_profit / cost * 100) if cost > 0 else 0

    # Apply calibration multiplier to confidence
    calibrated_confidence = int(min(100, confidence * calibration_multiplier))

    # Operational overhead: estimate cycles needed for this strategy
    estimated_cycles = max(1, int(estimated_days * 2))  # ~2 cycles per day
    operational_overhead = estimated_cycles * operational_cost_per_cycle

    # Capital velocity cost: what else could this money do?
    daily_opportunity = current_balance * 0.01  # assume 1% daily opportunity cost
    capital_velocity_cost = daily_opportunity * estimated_days

    verdict = _compute_verdict(
        roi_percent, calibrated_confidence, estimated_days,
        strategy_type, operational_overhead, capital_velocity_cost
    )

    projection = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action": action,
        "cost": cost,
        "strategy_type": strategy_type,
        "expected_return": expected_return,
        "expected_profit": round(expected_profit, 2),
        "roi_percent": round(roi_percent, 1),
        "time_to_return_days": estimated_days,
        "confidence_raw": confidence,
        "confidence_calibrated": calibrated_confidence,
        "calibration_multiplier": calibration_multiplier,
        "assumptions": assumptions,
        "risks": risks,
        "comparables": comparables,
        "bull_case": bull_case,
        "bear_case": bear_case,
        "research_summary": research_summary,
        "operational_overhead": round(operational_overhead, 4),
        "capital_velocity_cost": round(capital_velocity_cost, 4),
        "verdict": verdict,
        "status": "pending",
        "resolution": None,
    }

    projections = _load()
    projections.append(projection)
    _save(projections)
    return projection


def resolve_projection(projection_id: str, actual_outcome: str,
                       actual_return: float, actual_time_days: float) -> dict:
    """Resolve a projection with actual results. Returns the updated projection."""
    projections = _load()
    target = None
    for p in projections:
        if p["id"] == projection_id:
            target = p
            break

    if not target:
        return {"error": f"Projection {projection_id} not found"}

    actual_profit = actual_return - target["cost"]
    predicted_profit = target["expected_profit"]
    profit_delta = actual_profit - predicted_profit
    time_delta = actual_time_days - target["time_to_return_days"]
    hit = actual_profit > 0

    target["status"] = "resolved"
    target["resolution"] = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "actual_outcome": actual_outcome,
        "actual_return": actual_return,
        "actual_profit": round(actual_profit, 2),
        "actual_time_days": actual_time_days,
        "profit_delta": round(profit_delta, 2),
        "time_delta": round(time_delta, 1),
        "hit": hit,
    }

    _save(projections)
    return target


def get_unresolved_for_strategy(strategy: str) -> list:
    """Find pending projections for a given strategy."""
    return [p for p in _load()
            if p["status"] == "pending" and strategy.lower() in p["action"].lower()]


def get_projection_accuracy() -> dict:
    """Compute projection accuracy stats for calibration."""
    resolved = [p for p in _load() if p["status"] == "resolved" and p["resolution"]]
    if not resolved:
        return {"count": 0, "calibration_multiplier": 1.0}

    hits = sum(1 for p in resolved if p["resolution"]["hit"])
    avg_confidence = sum(p["confidence_raw"] for p in resolved) / len(resolved)
    actual_hit_rate = hits / len(resolved) * 100

    # If avg confidence was 70% but actual hit rate is 42%, multiplier = 42/70 = 0.6
    if avg_confidence > 0:
        calibration = actual_hit_rate / avg_confidence
    else:
        calibration = 1.0
    calibration = max(0.3, min(1.5, calibration))  # clamp

    avg_time_error = 0
    time_count = 0
    for p in resolved:
        if p["resolution"]["actual_time_days"] > 0 and p["time_to_return_days"] > 0:
            avg_time_error += abs(p["resolution"]["time_delta"])
            time_count += 1

    return {
        "count": len(resolved),
        "hits": hits,
        "avg_confidence": round(avg_confidence, 1),
        "actual_hit_rate": round(actual_hit_rate, 1),
        "calibration_multiplier": round(calibration, 2),
        "avg_time_error_days": round(avg_time_error / time_count, 1) if time_count else 0,
    }


def get_projections_context() -> str:
    """Build projections block for system prompt. Only pending + last 3 resolved."""
    projections = _load()
    pending = [p for p in projections if p["status"] == "pending"]
    resolved = [p for p in projections if p["status"] == "resolved"][-3:]

    if not pending and not resolved:
        return ""

    lines = ["PROJECTIONS:"]
    if pending:
        lines.append("  Pending:")
        for p in pending:
            lines.append(
                f"    [{p['id']}] {p['action'][:60]} — ${p['cost']:.2f} → "
                f"${p['expected_return']:.2f} ({p['verdict']}, "
                f"{p['confidence_calibrated']}% conf, {p['time_to_return_days']}d)"
            )
    if resolved:
        lines.append("  Recent resolved:")
        for p in resolved:
            r = p["resolution"]
            lines.append(
                f"    [{p['id']}] {p['action'][:40]} — predicted ${p['expected_profit']:.2f}, "
                f"actual ${r['actual_profit']:.2f} ({'HIT' if r['hit'] else 'MISS'}, "
                f"delta ${r['profit_delta']:+.2f})"
            )

    accuracy = get_projection_accuracy()
    if accuracy["count"] >= 3:
        lines.append(
            f"  Calibration: {accuracy['actual_hit_rate']:.0f}% hit rate vs "
            f"{accuracy['avg_confidence']:.0f}% avg confidence → "
            f"multiplier {accuracy['calibration_multiplier']}"
        )

    return "\n".join(lines)
