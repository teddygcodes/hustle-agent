"""
Hustle Agent — Instincts Engine

Computed learning from action outcomes. Not freeform lessons — math-driven
instincts that modify projections automatically.

Three data files:
  state/actions.json   — every action the agent takes
  state/priors.json    — seed base rates (hardcoded defaults → research-validated)
  state/instincts.json — computed model from resolved actions
"""

import json
import math
import uuid
import datetime
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
ACTIONS_FILE = STATE_DIR / "actions.json"
INSTINCTS_FILE = STATE_DIR / "instincts.json"
PRIORS_FILE = STATE_DIR / "priors.json"

# ---------------------------------------------------------------------------
# Category normalization
# ---------------------------------------------------------------------------

CATEGORY_MAP = {
    "polymarket": "polymarket",
    "product_sale": "product",
    "product": "product",
    "service": "service",
    "content": "content",
    "arbitrage": "arbitrage",
    "outreach": "outreach",
}

ALL_CATEGORIES = ["polymarket", "outreach", "product", "content", "service", "arbitrage", "other"]


def normalize_category(strategy_type: str) -> str:
    return CATEGORY_MAP.get(strategy_type.lower().strip(), "other")


# ---------------------------------------------------------------------------
# Hardcoded default priors
# ---------------------------------------------------------------------------

DEFAULT_PRIORS = {
    "polymarket": {
        "win_rate": 0.52, "avg_roi": 0.08,
        "source": "default", "validated": False, "research_date": None,
        "note": "Prediction markets avg ~52% for informed bettors"
    },
    "outreach": {
        "win_rate": 0.10, "avg_roi": 2.0,
        "source": "default", "validated": False, "research_date": None,
        "note": "Cold outreach: low response, high value if successful"
    },
    "product": {
        "win_rate": 0.20, "avg_roi": 3.0,
        "source": "default", "validated": False, "research_date": None,
        "note": "Digital products: power law returns"
    },
    "content": {
        "win_rate": 0.30, "avg_roi": 0.50,
        "source": "default", "validated": False, "research_date": None,
        "note": "Content monetization: low hit rate, moderate upside"
    },
    "service": {
        "win_rate": 0.65, "avg_roi": 0.80,
        "source": "default", "validated": False, "research_date": None,
        "note": "Service work: high close rate for small gigs"
    },
    "arbitrage": {
        "win_rate": 0.70, "avg_roi": 0.05,
        "source": "default", "validated": False, "research_date": None,
        "note": "Arb: high success but thin margins"
    },
}

INSTINCTS_HISTORY_CAP = 20


# ---------------------------------------------------------------------------
# File I/O (atomic writes, same pattern as all other modules)
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)


def load_actions() -> list:
    if ACTIONS_FILE.exists():
        with open(ACTIONS_FILE, "r") as f:
            return json.load(f)
    return []


def save_actions(actions: list):
    _atomic_write(ACTIONS_FILE, actions)


def load_instincts() -> dict:
    if INSTINCTS_FILE.exists():
        with open(INSTINCTS_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_instincts(data: dict):
    _atomic_write(INSTINCTS_FILE, data)


def load_priors() -> dict:
    if PRIORS_FILE.exists():
        with open(PRIORS_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_priors(data: dict):
    _atomic_write(PRIORS_FILE, data)


# ---------------------------------------------------------------------------
# Priors
# ---------------------------------------------------------------------------

def seed_priors():
    """Write default priors if priors.json doesn't exist or is empty."""
    existing = load_priors()
    if existing:
        return existing
    _save_priors(DEFAULT_PRIORS)
    return DEFAULT_PRIORS


def update_priors_from_research(category: str, win_rate: float, avg_roi: float, note: str = "") -> dict:
    """Called by the agent after researching real base rates for a category."""
    priors = load_priors()
    if not priors:
        priors = dict(DEFAULT_PRIORS)

    cat = normalize_category(category)
    priors[cat] = {
        "win_rate": max(0.0, min(1.0, win_rate)),
        "avg_roi": avg_roi,
        "source": "research",
        "validated": True,
        "research_date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": note or priors.get(cat, {}).get("note", ""),
    }
    _save_priors(priors)
    return priors[cat]


def priors_need_validation() -> bool:
    """Check if any priors still have validated=False."""
    priors = load_priors()
    if not priors:
        return True
    return any(not p.get("validated", False) for p in priors.values())


# ---------------------------------------------------------------------------
# Bayesian blend
# ---------------------------------------------------------------------------

def _blend(prior_val: float, earned_val: float, earned_count: int) -> float:
    """Bayesian blend: 0 earned = 100% prior, 5+ earned = 100% earned."""
    if earned_count <= 0:
        return prior_val
    if earned_count >= 5:
        return earned_val
    weight = earned_count / 5.0
    return prior_val * (1 - weight) + earned_val * weight


# ---------------------------------------------------------------------------
# Action creation / resolution
# ---------------------------------------------------------------------------

def _get_time_bucket() -> str:
    hour = datetime.datetime.now().hour
    if 6 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    return "night"


def _get_day_bucket() -> str:
    return "weekend" if datetime.datetime.now().weekday() >= 5 else "weekday"


def create_action(
    category: str,
    subcategory: str,
    cost: float,
    expected_return: float,
    time_horizon_days: float,
    confidence: int,
    balance: float,
    risk_posture: str,
    projection_id: str = None,
    market_odds: float = None,
) -> dict:
    """Create a new pending action entry."""
    actions = load_actions()
    action = {
        "action_id": str(uuid.uuid4())[:8],
        "projection_id": projection_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "category": normalize_category(category),
        "subcategory": subcategory,
        "cost": cost,
        "conditions": {
            "time_horizon_days": time_horizon_days,
            "market_odds": market_odds,
            "confidence_at_decision": confidence,
            "capital_percentage": round((cost / balance * 100) if balance > 0 else 0, 1),
            "time_of_day": _get_time_bucket(),
            "day_of_week": _get_day_bucket(),
            "risk_posture_at_time": risk_posture,
            "balance_at_time": balance,
        },
        "expected_return": expected_return,
        "status": "pending",
        "actual_return": None,
        "actual_time_days": None,
        "resolved_at": None,
    }
    actions.append(action)
    save_actions(actions)
    return action


def resolve_action(projection_id: str, actual_return: float,
                   actual_time_days: float, status: str) -> dict | None:
    """Resolve an action by its linked projection_id. Returns updated action or None."""
    if not projection_id:
        return None
    actions = load_actions()
    target = None
    for a in actions:
        if a.get("projection_id") == projection_id and a["status"] == "pending":
            target = a
            break
    if not target:
        return None

    target["actual_return"] = actual_return
    target["actual_time_days"] = max(0, actual_time_days)

    target["resolved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Determine status from profit
    if status in ("won", "lost", "partial", "expired"):
        target["status"] = status
    else:
        profit = actual_return - target["cost"]
        target["status"] = "won" if profit > 0 else "lost"

    save_actions(actions)
    return target


# ---------------------------------------------------------------------------
# Core instinct computations
# ---------------------------------------------------------------------------

def _get_resolved(actions: list = None) -> list:
    if actions is None:
        actions = load_actions()
    return [a for a in actions if a["status"] in ("won", "lost", "partial", "expired")]


def _compute_category_scores(resolved: list) -> dict:
    """Per-category: win rate, avg ROI, avg return time, capital efficiency, trend, calibration gap."""
    by_cat = defaultdict(list)
    for a in resolved:
        by_cat[a["category"]].append(a)

    scores = {}
    for cat, actions in by_cat.items():
        n = len(actions)
        wins = sum(1 for a in actions if a["status"] == "won")
        win_rate = wins / n if n > 0 else 0

        rois = []
        for a in actions:
            if a["cost"] > 0 and a["actual_return"] is not None:
                rois.append((a["actual_return"] - a["cost"]) / a["cost"])
        avg_roi = sum(rois) / len(rois) if rois else 0

        return_times = [a["actual_time_days"] for a in actions if a.get("actual_time_days") is not None]
        avg_return_time = sum(return_times) / len(return_times) if return_times else 0

        total_invested = sum(a["cost"] for a in actions)
        total_returned = sum(a["actual_return"] or 0 for a in actions)
        capital_efficiency = total_returned / total_invested if total_invested > 0 else 0

        # Trend: compare last 3 vs all-time
        if n >= 4:
            recent = actions[-3:]
            recent_wr = sum(1 for a in recent if a["status"] == "won") / len(recent)
            if recent_wr > win_rate + 0.1:
                trend = "improving"
            elif recent_wr < win_rate - 0.1:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"

        # Confidence calibration gap
        confs = [a["conditions"]["confidence_at_decision"] for a in actions
                 if a["conditions"].get("confidence_at_decision")]
        avg_conf = sum(confs) / len(confs) / 100 if confs else 0.5
        calibration_gap = avg_conf - win_rate  # positive = overconfident

        scores[cat] = {
            "win_rate": round(win_rate, 3),
            "avg_roi": round(avg_roi, 3),
            "avg_return_time_days": round(avg_return_time, 1),
            "capital_efficiency": round(capital_efficiency, 3),
            "trend": trend,
            "confidence_calibration_gap": round(calibration_gap, 3),
            "sample_size": n,
        }
    return scores


def _compute_dimension_scores(resolved: list) -> dict:
    """Cross-cutting dimension slices."""
    dimensions = {
        "time_horizon": {
            "under_1d": lambda a: a["conditions"].get("time_horizon_days", 0) < 1,
            "1_to_7d": lambda a: 1 <= a["conditions"].get("time_horizon_days", 0) <= 7,
            "over_7d": lambda a: a["conditions"].get("time_horizon_days", 0) > 7,
        },
        "capital_size": {
            "under_5": lambda a: a["cost"] < 5,
            "5_to_15": lambda a: 5 <= a["cost"] <= 15,
            "over_15": lambda a: a["cost"] > 15,
        },
        "confidence_level": {
            "high": lambda a: a["conditions"].get("confidence_at_decision", 0) >= 70,
            "medium": lambda a: 40 <= a["conditions"].get("confidence_at_decision", 0) < 70,
            "low": lambda a: a["conditions"].get("confidence_at_decision", 0) < 40,
        },
        "time_of_day": {
            "morning": lambda a: a["conditions"].get("time_of_day") == "morning",
            "afternoon": lambda a: a["conditions"].get("time_of_day") == "afternoon",
            "evening": lambda a: a["conditions"].get("time_of_day") == "evening",
            "night": lambda a: a["conditions"].get("time_of_day") == "night",
        },
        "day_of_week": {
            "weekday": lambda a: a["conditions"].get("day_of_week") == "weekday",
            "weekend": lambda a: a["conditions"].get("day_of_week") == "weekend",
        },
        "risk_posture": {
            "aggressive": lambda a: a["conditions"].get("risk_posture_at_time") == "aggressive",
            "normal": lambda a: a["conditions"].get("risk_posture_at_time") == "normal",
            "preservation": lambda a: a["conditions"].get("risk_posture_at_time") == "preservation",
        },
    }

    scores = {}
    for dim_name, buckets in dimensions.items():
        dim_scores = {}
        for bucket_name, filter_fn in buckets.items():
            matching = [a for a in resolved if filter_fn(a)]
            n = len(matching)
            if n == 0:
                continue
            wins = sum(1 for a in matching if a["status"] == "won")
            rois = []
            for a in matching:
                if a["cost"] > 0 and a["actual_return"] is not None:
                    rois.append((a["actual_return"] - a["cost"]) / a["cost"])
            dim_scores[bucket_name] = {
                "win_rate": round(wins / n, 3),
                "avg_roi": round(sum(rois) / len(rois), 3) if rois else 0,
                "sample_size": n,
            }
        if dim_scores:
            scores[dim_name] = dim_scores
    return scores


def _compute_cross_patterns(resolved: list, category_scores: dict) -> list:
    """Multi-dimensional combo patterns ranked by signal strength."""
    if len(resolved) < 3:
        return []

    # Define 2-dimension combos to check
    def _cat(a): return a["category"]
    def _horizon(a):
        d = a["conditions"].get("time_horizon_days", 0)
        return "short" if d < 1 else ("medium" if d <= 7 else "long")
    def _cap(a):
        c = a["cost"]
        return "small" if c < 5 else ("medium" if c <= 15 else "large")
    def _conf(a):
        c = a["conditions"].get("confidence_at_decision", 50)
        return "high" if c >= 70 else ("medium" if c >= 40 else "low")

    combos = [
        ("category_x_horizon", _cat, _horizon),
        ("category_x_capital", _cat, _cap),
        ("category_x_confidence", _cat, _conf),
        ("horizon_x_capital", _horizon, _cap),
        ("confidence_x_capital", _conf, _cap),
    ]

    patterns = []
    for combo_name, fn_a, fn_b in combos:
        cells = defaultdict(list)
        for a in resolved:
            key = f"{fn_a(a)}+{fn_b(a)}"
            cells[key].append(a)

        for key, group in cells.items():
            n = len(group)
            if n < 2:
                continue
            wins = sum(1 for a in group if a["status"] == "won")
            wr = wins / n

            # Get category mean for signal strength
            parts = key.split("+")
            cat = parts[0] if combo_name.startswith("category") else None
            cat_mean = category_scores.get(cat, {}).get("win_rate", 0.5) if cat else 0.5
            # Overall mean as fallback
            if not cat:
                total_wins = sum(1 for a in resolved if a["status"] == "won")
                cat_mean = total_wins / len(resolved) if resolved else 0.5

            signal = abs(wr - cat_mean) * math.log(n + 1)

            rois = []
            for a in group:
                if a["cost"] > 0 and a["actual_return"] is not None:
                    rois.append((a["actual_return"] - a["cost"]) / a["cost"])

            patterns.append({
                "combo": combo_name,
                "key": key,
                "win_rate": round(wr, 3),
                "avg_roi": round(sum(rois) / len(rois), 3) if rois else 0,
                "sample_size": n,
                "signal_strength": round(signal, 3),
                "description": f"{key.replace('+', ' + ')}: {wr*100:.0f}% win rate (n={n})",
            })

    patterns.sort(key=lambda p: p["signal_strength"], reverse=True)
    return patterns[:20]


def _compute_calibration(resolved: list, priors: dict) -> dict:
    """Per-category and overall calibration multipliers with Bayesian blending."""
    by_cat = defaultdict(list)
    for a in resolved:
        by_cat[a["category"]].append(a)

    per_category = {}
    for cat, actions in by_cat.items():
        n = len(actions)
        wins = sum(1 for a in actions if a["status"] == "won")
        actual_wr = wins / n if n > 0 else 0
        confs = [a["conditions"]["confidence_at_decision"] for a in actions
                 if a["conditions"].get("confidence_at_decision")]
        avg_conf = sum(confs) / len(confs) / 100 if confs else 0.5

        earned_cal = actual_wr / avg_conf if avg_conf > 0 else 1.0
        earned_cal = max(0.3, min(1.5, earned_cal))

        # Blend with prior calibration (assume prior calibration = 1.0)
        prior_cal = 1.0
        blended = _blend(prior_cal, earned_cal, n)
        per_category[cat] = round(blended, 3)

    # Overall
    if resolved:
        total_wins = sum(1 for a in resolved if a["status"] == "won")
        total_wr = total_wins / len(resolved)
        all_confs = [a["conditions"]["confidence_at_decision"] for a in resolved
                     if a["conditions"].get("confidence_at_decision")]
        avg_all_conf = sum(all_confs) / len(all_confs) / 100 if all_confs else 0.5
        overall = total_wr / avg_all_conf if avg_all_conf > 0 else 1.0
        overall = max(0.3, min(1.5, overall))
    else:
        overall = 1.0

    return {"overall": round(overall, 3), "per_category": per_category}


def _generate_sentences(category_scores: dict, dimension_scores: dict,
                        cross_patterns: list, calibration: dict) -> list:
    """Auto-generate 5-10 plain English instinct sentences from the math."""
    sentences = []

    # Category performance sentences
    for cat, s in sorted(category_scores.items(), key=lambda x: x[1]["sample_size"], reverse=True):
        if s["sample_size"] < 2:
            continue
        wr_pct = s["win_rate"] * 100
        roi_pct = s["avg_roi"] * 100
        trend_str = f" (trend: {s['trend']})" if s["trend"] not in ("stable", "insufficient_data") else ""
        sentences.append({
            "text": f"{cat}: {wr_pct:.0f}% win rate, {roi_pct:.0f}% avg ROI across {s['sample_size']} actions{trend_str}.",
            "signal": s["sample_size"] * 0.5,
        })

    # Cross-pattern sentences (top 5 by signal strength)
    for p in cross_patterns[:5]:
        if p["signal_strength"] > 0.1:
            sentences.append({
                "text": p["description"],
                "signal": p["signal_strength"],
            })

    # Calibration sentence
    cal_overall = calibration.get("overall", 1.0)
    if abs(cal_overall - 1.0) > 0.1:
        direction = "overconfident" if cal_overall < 1.0 else "underconfident"
        pct = abs(1.0 - cal_overall) * 100
        sentences.append({
            "text": f"Calibration: you're {pct:.0f}% {direction} overall. When you feel 80% confident, you're actually right {80 * cal_overall:.0f}% of the time.",
            "signal": pct * 0.1,
        })

    # Dimension highlights (best and worst buckets)
    for dim_name, buckets in dimension_scores.items():
        best = max(buckets.items(), key=lambda x: x[1]["win_rate"]) if buckets else None
        worst = min(buckets.items(), key=lambda x: x[1]["win_rate"]) if buckets else None
        if best and worst and best[0] != worst[0] and best[1]["sample_size"] >= 2 and worst[1]["sample_size"] >= 2:
            diff = best[1]["win_rate"] - worst[1]["win_rate"]
            if diff > 0.15:
                sentences.append({
                    "text": f"{dim_name}: {best[0]} ({best[1]['win_rate']*100:.0f}% win) outperforms {worst[0]} ({worst[1]['win_rate']*100:.0f}% win).",
                    "signal": diff * math.log(min(best[1]["sample_size"], worst[1]["sample_size"]) + 1),
                })

    sentences.sort(key=lambda s: s["signal"], reverse=True)
    return [s["text"] for s in sentences[:10]]


# ---------------------------------------------------------------------------
# Exploration vs exploitation
# ---------------------------------------------------------------------------

def get_exploration_mode(actions: list = None) -> str:
    """Returns 'explore' or 'exploit' based on data coverage."""
    resolved = _get_resolved(actions)
    categories_with_data = defaultdict(int)
    for a in resolved:
        categories_with_data[a["category"]] += 1

    # Need 5+ resolved actions in 3+ categories to shift to exploit
    cats_with_enough = sum(1 for c in categories_with_data.values() if c >= 5)
    if cats_with_enough >= 3:
        return "exploit"
    return "explore"


def get_exploration_progress(actions: list = None) -> dict:
    """Returns progress toward exploit mode for UI display."""
    resolved = _get_resolved(actions)
    by_cat = defaultdict(int)
    for a in resolved:
        by_cat[a["category"]] += 1

    return {
        "mode": get_exploration_mode(actions),
        "total_resolved": len(resolved),
        "categories": {cat: min(count, 5) for cat, count in by_cat.items()},
        "categories_ready": sum(1 for c in by_cat.values() if c >= 5),
        "categories_needed": 3,
        "target_per_category": 5,
    }


# ---------------------------------------------------------------------------
# Main recompute
# ---------------------------------------------------------------------------

def recompute_instincts():
    """Recompute all instinct scores from resolved actions. Called after every resolution."""
    actions = load_actions()
    resolved = _get_resolved(actions)
    priors = load_priors()

    if not resolved:
        # Nothing to compute yet, save minimal state
        data = {
            "last_computed": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "action_count_at_compute": 0,
            "exploration_mode": "explore",
            "category_scores": {},
            "dimension_scores": {},
            "cross_patterns": [],
            "calibration": {"overall": 1.0, "per_category": {}},
            "instinct_sentences": [],
            "history": [],
        }
        _save_instincts(data)
        return data

    category_scores = _compute_category_scores(resolved)
    dimension_scores = _compute_dimension_scores(resolved)
    cross_patterns = _compute_cross_patterns(resolved, category_scores)
    calibration = _compute_calibration(resolved, priors)
    sentences = _generate_sentences(category_scores, dimension_scores, cross_patterns, calibration)
    mode = get_exploration_mode(actions)

    # Load existing for history
    existing = load_instincts()
    history = existing.get("history", [])
    history.append({
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sentences": sentences[:5],
        "overall_calibration": calibration["overall"],
        "action_count": len(resolved),
    })
    if len(history) > INSTINCTS_HISTORY_CAP:
        history = history[-INSTINCTS_HISTORY_CAP:]

    data = {
        "last_computed": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action_count_at_compute": len(resolved),
        "exploration_mode": mode,
        "category_scores": category_scores,
        "dimension_scores": dimension_scores,
        "cross_patterns": cross_patterns,
        "calibration": calibration,
        "instinct_sentences": sentences,
        "history": history,
    }
    _save_instincts(data)
    return data


# ---------------------------------------------------------------------------
# Projection adjustments (called before creating a projection)
# ---------------------------------------------------------------------------

def get_adjustments_for_action(category: str, conditions: dict) -> dict:
    """
    Get instinct-based adjustments for a proposed action.
    Returns calibration multiplier, ROI adjustment, warnings, exploration bonus.
    """
    cat = normalize_category(category)
    instincts = load_instincts()
    priors = load_priors()
    actions = load_actions()
    resolved = _get_resolved(actions)

    # Count earned data for this category
    cat_resolved = [a for a in resolved if a["category"] == cat]
    earned_count = len(cat_resolved)

    # --- Calibration multiplier ---
    instinct_cal = instincts.get("calibration", {}).get("per_category", {}).get(cat)
    prior_cal = 1.0  # prior assumes no miscalibration

    if instinct_cal is not None:
        cal_multiplier = _blend(prior_cal, instinct_cal, earned_count)
    else:
        cal_multiplier = prior_cal

    # --- ROI adjustment ---
    cat_score = instincts.get("category_scores", {}).get(cat, {})
    prior_roi = priors.get(cat, {}).get("avg_roi")
    earned_roi = cat_score.get("avg_roi")

    # ROI adjustment: scales expected return based on historical ROI for this category.
    # blended_roi is in [-1, +∞) where -1 = total loss, 0 = break-even, +1 = 100% return.
    # Negative ROI: 1.0 + (-0.3) = 0.7x multiplier (clamped at 0.3x floor).
    # Low ROI + overconfident: dampen to prevent optimistic projections.
    roi_adjustment = 1.0
    if earned_roi is not None and prior_roi is not None:
        blended_roi = _blend(prior_roi, earned_roi, earned_count)
        if blended_roi < 0:
            roi_adjustment = max(0.3, 1.0 + blended_roi)
        elif blended_roi < 0.5 and conditions.get("confidence_at_decision", 50) > 60:
            roi_adjustment = max(0.5, 0.7 + blended_roi * 0.6)

    # --- Cross-pattern warnings ---
    warnings = []
    for p in instincts.get("cross_patterns", []):
        if p["sample_size"] >= 2 and p["win_rate"] < 0.35:
            # Check if this pattern matches the proposed action
            key_parts = p["key"].lower().split("+")
            if cat in key_parts:
                warnings.append(p["description"])
        if len(warnings) >= 3:
            break

    # --- Exploration bonus ---
    mode = get_exploration_mode(actions)
    exploration_note = ""
    if mode == "explore" and earned_count < 5:
        exploration_note = f"Category '{cat}' has {earned_count} data point(s). Exploratory bet recommended to build instincts."

    # --- Win rate context ---
    prior_wr = priors.get(cat, {}).get("win_rate", 0.5)
    earned_wr = cat_score.get("win_rate")
    if earned_wr is not None:
        blended_wr = _blend(prior_wr, earned_wr, earned_count)
    else:
        blended_wr = prior_wr

    return {
        "calibration_multiplier": round(cal_multiplier, 3),
        "roi_adjustment": round(roi_adjustment, 3),
        "cross_pattern_warnings": warnings,
        "exploration_note": exploration_note,
        "blended_win_rate": round(blended_wr, 3),
        "earned_count": earned_count,
        "data_source": "earned" if earned_count >= 5 else ("blend" if earned_count > 0 else "prior"),
    }


# ---------------------------------------------------------------------------
# System prompt context
# ---------------------------------------------------------------------------

def get_instincts_context() -> str:
    """Build the YOUR INSTINCTS block for the system prompt. 200-400 tokens max."""
    instincts = load_instincts()
    priors = load_priors()
    actions = load_actions()
    mode = get_exploration_mode(actions)

    if not instincts and not priors:
        return ""

    resolved = _get_resolved(actions)
    total = len(resolved)

    if mode == "explore":
        return _build_explore_context(instincts, priors, actions, total)
    else:
        return _build_exploit_context(instincts, total)


def _build_explore_context(instincts: dict, priors: dict, actions: list, total: int) -> str:
    lines = [f"YOUR INSTINCTS — EXPLORATION MODE ({total} actions resolved, building data):"]

    # Progress
    progress = get_exploration_progress(actions)
    cat_strs = [f"{cat} ({n}/5)" for cat, n in progress["categories"].items()]
    if cat_strs:
        lines.append(f"  Progress: {', '.join(cat_strs)}")
    lines.append(f"  Need 5+ resolved in 3+ categories to shift to exploit. Currently {progress['categories_ready']}/3 ready.")

    # Priors
    if priors:
        unvalidated = [cat for cat, p in priors.items() if not p.get("validated", False)]
        if unvalidated:
            lines.append(f"  Unvalidated priors (research these): {', '.join(unvalidated)}")
        prior_strs = [f"{cat} ~{p['win_rate']*100:.0f}% win" for cat, p in priors.items()]
        lines.append(f"  Base rates: {', '.join(prior_strs)}")

    # Early earned signals
    sentences = instincts.get("instinct_sentences", [])
    if sentences:
        lines.append("  Early signals:")
        for s in sentences[:3]:
            lines.append(f"    - {s}")

    lines.append("  Directive: Small bets ($2-5), diversify across categories, maximize learning per dollar.")
    return "\n".join(lines)


def _build_exploit_context(instincts: dict, total: int) -> str:
    cat_scores = instincts.get("category_scores", {})
    cal = instincts.get("calibration", {})
    sentences = instincts.get("instinct_sentences", [])
    cross = instincts.get("cross_patterns", [])

    overall_wr = 0
    if cat_scores:
        total_wins = sum(s["win_rate"] * s["sample_size"] for s in cat_scores.values())
        total_n = sum(s["sample_size"] for s in cat_scores.values())
        overall_wr = (total_wins / total_n * 100) if total_n > 0 else 0

    lines = [f"YOUR INSTINCTS ({total} actions, {overall_wr:.0f}% overall win rate):"]

    # Instinct sentences
    for s in sentences[:7]:
        lines.append(f"  - {s}")

    # Calibration
    cal_overall = cal.get("overall", 1.0)
    lines.append(f"  Calibration: When you feel 80% confident you're actually right {80 * cal_overall:.0f}% of the time.")

    # Strongest / weakest plays from cross patterns
    strong = [p for p in cross if p["win_rate"] >= 0.6 and p["sample_size"] >= 3][:3]
    weak = [p for p in cross if p["win_rate"] <= 0.35 and p["sample_size"] >= 3][:3]
    if strong:
        lines.append("  Strongest plays: " + "; ".join(p["description"] for p in strong))
    if weak:
        lines.append("  Weakest plays: " + "; ".join(p["description"] for p in weak))

    return "\n".join(lines)
