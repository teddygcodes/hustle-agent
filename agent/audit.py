"""
Hustle Agent — Meta-Cognition / Self-Audit

Every N cycles, analyze decision-making patterns and auto-calibrate.
"""

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
AUDITS_FILE = STATE_DIR / "audits.json"
INSTINCTS_FILE = STATE_DIR / "instincts.json"

AUDIT_INTERVAL = 10


def _load() -> list:
    if AUDITS_FILE.exists():
        with open(AUDITS_FILE, "r") as f:
            return json.load(f)
    return []


def _save(audits: list):
    tmp = AUDITS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(audits, f, indent=2)
    tmp.rename(AUDITS_FILE)


def should_run_audit(cycle: int) -> bool:
    return cycle > 0 and cycle % AUDIT_INTERVAL == 0


def run_self_audit(state: dict, ledger: list, projections: list,
                   costs: list, pipeline: list) -> dict:
    """Comprehensive self-audit. Returns audit results."""
    now = datetime.datetime.now(datetime.timezone.utc)
    audit = {
        "cycle": state.get("cycle", 0),
        "timestamp": now.isoformat(),
        "projection_accuracy": _audit_projections(projections),
        "strategy_trends": _audit_strategies(ledger),
        "operational_efficiency": _audit_efficiency(ledger, costs),
        "pipeline_health": _audit_pipeline(pipeline),
        "recommendations": [],
    }

    # Generate recommendations
    recs = []
    pa = audit["projection_accuracy"]
    if pa["count"] >= 3:
        if pa["calibration_multiplier"] < 0.7:
            recs.append(f"Projections are {(1-pa['calibration_multiplier'])*100:.0f}% too optimistic. Apply calibration discount.")
        elif pa["calibration_multiplier"] > 1.2:
            recs.append("Projections are too pessimistic — missing opportunities. Be bolder.")
        if pa["avg_time_error_days"] > 3:
            recs.append(f"Time estimates are off by ~{pa['avg_time_error_days']:.1f} days. Add buffer to estimates.")

    eff = audit["operational_efficiency"]
    if eff.get("cost_per_dollar_earned", 0) > 0.5:
        recs.append(f"Spending ${eff['cost_per_dollar_earned']:.2f} in API costs per $1 earned. Reduce research cycles or use cheaper models.")
    if eff.get("avg_cycle_cost", 0) > 0.50:
        recs.append(f"Avg cycle costs ${eff['avg_cycle_cost']:.2f}. Consider batching actions and using fewer tool calls.")

    st = audit["strategy_trends"]
    for strat, info in st.items():
        if info.get("roi", 0) < -20:
            recs.append(f"Strategy '{strat}' has {info['roi']:.0f}% ROI. Consider retiring it.")

    ph = audit["pipeline_health"]
    if ph.get("stale_count", 0) > 2:
        recs.append(f"{ph['stale_count']} pipeline items haven't moved in 3+ days. Follow up or close them.")

    # Cross-check with instincts calibration
    instinct_recs = _audit_instincts_crosscheck(pa, state.get("cycle", 0))
    recs.extend(instinct_recs)

    audit["recommendations"] = recs

    audits = _load()
    audits.append(audit)
    # Keep last 20 audits
    if len(audits) > 20:
        audits = audits[-20:]
    _save(audits)

    return audit


def _audit_projections(projections: list) -> dict:
    resolved = [p for p in projections if p.get("status") == "resolved" and p.get("resolution")]
    if not resolved:
        return {"count": 0, "calibration_multiplier": 1.0}

    hits = sum(1 for p in resolved if p["resolution"]["hit"])
    avg_conf = sum(p["confidence_raw"] for p in resolved) / len(resolved)
    hit_rate = hits / len(resolved) * 100

    calibration = (hit_rate / avg_conf) if avg_conf > 0 else 1.0
    calibration = max(0.3, min(1.5, calibration))

    time_errors = [abs(p["resolution"]["time_delta"])
                   for p in resolved if p["resolution"].get("actual_time_days", 0) > 0]
    avg_time_error = sum(time_errors) / len(time_errors) if time_errors else 0

    return {
        "count": len(resolved),
        "hits": hits,
        "avg_confidence": round(avg_conf, 1),
        "actual_hit_rate": round(hit_rate, 1),
        "calibration_multiplier": round(calibration, 2),
        "avg_time_error_days": round(avg_time_error, 1),
    }


def _audit_strategies(ledger: list) -> dict:
    strategies = {}
    for txn in ledger:
        strat = txn.get("strategy", "unknown")
        if strat == "operations":
            continue
        if strat not in strategies:
            strategies[strat] = {"invested": 0, "returned": 0, "count": 0}
        if txn["type"] in ("expense", "investment"):
            strategies[strat]["invested"] += txn["amount"]
        elif txn["type"] in ("income", "return"):
            strategies[strat]["returned"] += txn["amount"]
        strategies[strat]["count"] += 1

    result = {}
    for name, s in strategies.items():
        roi = ((s["returned"] - s["invested"]) / s["invested"] * 100) if s["invested"] > 0 else 0
        result[name] = {
            "invested": round(s["invested"], 2),
            "returned": round(s["returned"], 2),
            "roi": round(roi, 1),
            "transactions": s["count"],
        }
    return result


def _audit_efficiency(ledger: list, costs: list) -> dict:
    total_api_cost = sum(c.get("cost", 0) for c in costs)
    total_earned = sum(t["amount"] for t in ledger if t["type"] in ("income", "return"))
    cycles = set(c.get("cycle", 0) for c in costs)
    num_cycles = len(cycles) or 1

    return {
        "total_api_cost": round(total_api_cost, 4),
        "total_earned": round(total_earned, 2),
        "cost_per_dollar_earned": round(total_api_cost / total_earned, 4) if total_earned > 0 else 0,
        "avg_cycle_cost": round(total_api_cost / num_cycles, 4),
        "cycles_tracked": num_cycles,
    }


def _audit_pipeline(pipeline: list) -> dict:
    active = [i for i in pipeline if i.get("stage") not in ("closed_won", "closed_lost")]
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).isoformat()
    stale = [i for i in active if i.get("updated_at", "") < cutoff]
    return {
        "active_count": len(active),
        "stale_count": len(stale),
        "total_expected_value": sum(i.get("expected_value", 0) for i in active),
    }


def _audit_instincts_crosscheck(projection_accuracy: dict, cycle: int) -> list:
    """Cross-check audit calibration with instincts per-category calibration."""
    recs = []
    if not INSTINCTS_FILE.exists():
        return recs
    try:
        with open(INSTINCTS_FILE, "r") as f:
            inst = json.load(f)
    except (json.JSONDecodeError, OSError):
        return recs

    if not inst:
        return recs

    # Check if still in explore mode after many cycles
    mode = inst.get("exploration_mode", "explore")
    if mode == "explore" and cycle >= 20:
        recs.append(f"Still in EXPLORE mode at cycle {cycle}. Diversify into more categories to build instincts faster.")

    # Compare audit global calibration vs instinct per-category
    audit_cal = projection_accuracy.get("calibration_multiplier", 1.0)
    per_cat = inst.get("calibration", {}).get("per_category", {})
    for cat, cat_cal in per_cat.items():
        divergence = abs(cat_cal - audit_cal)
        if divergence > 0.2:
            direction = "more optimistic" if cat_cal > audit_cal else "more pessimistic"
            recs.append(
                f"Instinct calibration for '{cat}' ({cat_cal:.2f}) diverges from audit global ({audit_cal:.2f}) — "
                f"category is {direction} than your overall pattern."
            )

    return recs


def get_calibration_multiplier() -> float:
    """Get the latest calibration multiplier for projections."""
    audits = _load()
    if not audits:
        return 1.0
    latest = audits[-1]
    return latest.get("projection_accuracy", {}).get("calibration_multiplier", 1.0)


def get_audit_context(current_cycle: int) -> str:
    """Build audit block for system prompt. Sparse: just multiplier unless fresh."""
    audits = _load()
    if not audits:
        return ""

    latest = audits[-1]
    audit_cycle = latest.get("cycle", 0)
    is_fresh = (current_cycle - audit_cycle) <= 2

    cal = latest.get("projection_accuracy", {}).get("calibration_multiplier", 1.0)

    if not is_fresh:
        # Just the calibration multiplier
        if cal != 1.0:
            return f"SELF-AUDIT: Projection calibration multiplier: {cal} (from cycle {audit_cycle})"
        return ""

    # Fresh audit — show summary
    lines = [f"SELF-AUDIT (cycle {audit_cycle}):"]
    pa = latest.get("projection_accuracy", {})
    if pa.get("count", 0) > 0:
        lines.append(
            f"  Projections: {pa.get('actual_hit_rate', 0):.0f}% hit rate, "
            f"calibration: {cal}"
        )

    eff = latest.get("operational_efficiency", {})
    if eff.get("avg_cycle_cost", 0) > 0:
        lines.append(f"  Efficiency: ${eff['avg_cycle_cost']:.4f}/cycle, ${eff.get('cost_per_dollar_earned', 0):.2f}/$ earned")

    recs = latest.get("recommendations", [])
    if recs:
        lines.append("  Recommendations:")
        for r in recs[:3]:
            lines.append(f"    - {r}")

    return "\n".join(lines)
