"""
Glint Trading Bot — Pattern Analysis Engine

Pure-math statistical analysis of trade history.
No LLM needed — finds win-rate breakdowns, correlations,
streak info, and threshold adjustment suggestions.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import PATTERNS_FILE, MIN_RELATIVE_EDGE

log = logging.getLogger(__name__)

EQUITY_CHART_PATH = Path(__file__).resolve().parent / "state" / "equity_chart.png"

# ── Helpers ──────────────────────────────────────────────────────────────────

SPORT_KEYWORDS = {
    "nba": ["nba", "basketball", "lakers", "celtics", "warriors", "bucks",
            "nuggets", "76ers", "knicks", "heat", "suns", "nets"],
    "mlb": ["mlb", "baseball", "yankees", "dodgers", "astros", "braves",
            "mets", "phillies", "padres", "rangers", "cubs", "red sox"],
    "nhl": ["nhl", "hockey", "bruins", "penguins", "oilers", "rangers",
            "maple leafs", "panthers", "avalanche", "lightning"],
}


def _detect_sport(trade: dict) -> str:
    """Best-effort sport detection from title or legs."""
    text = trade.get("title", "").lower()
    legs = trade.get("legs", [])
    if isinstance(legs, list):
        for leg in legs:
            if isinstance(leg, dict):
                text += " " + leg.get("title", "").lower()
            elif isinstance(leg, str):
                text += " " + leg.lower()

    for sport, keywords in SPORT_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return sport
    return "other"


def _count_legs(trade: dict) -> int:
    """Count parlay legs. Use legs field if present, else count 'yes' in title."""
    legs = trade.get("legs")
    if isinstance(legs, list) and len(legs) > 0:
        return len(legs)
    title = trade.get("title", "").lower()
    count = title.count("yes")
    return max(count, 1)


def _edge_bucket(edge: float) -> str:
    """Bucket a relative edge (0-1 scale) into a category."""
    if edge < 0.20:
        return "<20%"
    elif edge < 0.30:
        return "20-30%"
    elif edge < 0.50:
        return "30-50%"
    else:
        return "50%+"


def _time_bucket(iso_str: str) -> str:
    """Bucket an ISO timestamp into a time-of-day category."""
    try:
        dt = datetime.fromisoformat(iso_str)
        hour = dt.hour
    except (ValueError, TypeError):
        return "unknown"

    if 6 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 22:
        return "evening"
    else:
        return "night"


def _day_of_week(iso_str: str) -> str:
    """Return lowercase day name from ISO timestamp."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%A").lower()
    except (ValueError, TypeError):
        return "unknown"


def _resolved_trades(trade_history: list[dict]) -> list[dict]:
    """Filter to only resolved trades with a result."""
    return [
        t for t in trade_history
        if t.get("status") == "resolved" and t.get("result") in ("won", "lost")
    ]


def _winrate_entry(trades: list[dict]) -> dict:
    """Compute win rate and ROI for a group of resolved trades."""
    total = len(trades)
    if total == 0:
        return {"total": 0, "wins": 0, "winrate": 0.0, "roi": 0.0}
    wins = sum(1 for t in trades if t.get("result") == "won")
    total_cost = sum(t.get("cost", 0) for t in trades)
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    roi = (total_pnl / total_cost) if total_cost > 0 else 0.0
    return {
        "total": total,
        "wins": wins,
        "winrate": round(wins / total, 4),
        "roi": round(roi, 4),
    }


# ── 1. Load / Save Patterns ─────────────────────────────────────────────────

def load_patterns() -> dict:
    """Read saved patterns from PATTERNS_FILE."""
    if PATTERNS_FILE.exists():
        try:
            return json.loads(PATTERNS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load patterns file: %s", e)
    return {}


def save_patterns(patterns: dict) -> None:
    """Atomic write patterns to PATTERNS_FILE (write .tmp then rename)."""
    PATTERNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = PATTERNS_FILE.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(patterns, indent=2, default=str))
        os.replace(str(tmp_path), str(PATTERNS_FILE))
        log.info("Saved patterns to %s", PATTERNS_FILE)
    except OSError as e:
        log.error("Failed to save patterns: %s", e)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ── 2. Analyze Patterns ─────────────────────────────────────────────────────

def analyze_patterns(trade_history: list[dict]) -> dict:
    """
    Break down resolved trades by multiple dimensions and compute
    win rate + ROI for each bucket.
    """
    resolved = _resolved_trades(trade_history)
    if not resolved:
        return {
            "by_type": {},
            "by_sport": {},
            "by_legs": {},
            "by_day": {},
            "by_edge_bucket": {},
            "by_time_of_day": {},
            "total_resolved": 0,
        }

    # Group trades by each dimension
    by_type: dict[str, list] = {}
    by_sport: dict[str, list] = {}
    by_legs: dict[str, list] = {}
    by_day: dict[str, list] = {}
    by_edge_bucket: dict[str, list] = {}
    by_time: dict[str, list] = {}

    for t in resolved:
        trade_type = t.get("type", "unknown")
        by_type.setdefault(trade_type, []).append(t)

        sport = _detect_sport(t)
        by_sport.setdefault(sport, []).append(t)

        legs = _count_legs(t)
        leg_key = str(legs) if legs <= 6 else "7+"
        by_legs.setdefault(leg_key, []).append(t)

        day = _day_of_week(t.get("opened_at", ""))
        by_day.setdefault(day, []).append(t)

        edge = t.get("relative_edge", t.get("edge", 0))
        bucket = _edge_bucket(edge)
        by_edge_bucket.setdefault(bucket, []).append(t)

        time_b = _time_bucket(t.get("opened_at", ""))
        by_time.setdefault(time_b, []).append(t)

    return {
        "by_type": {k: _winrate_entry(v) for k, v in by_type.items()},
        "by_sport": {k: _winrate_entry(v) for k, v in by_sport.items()},
        "by_legs": {k: _winrate_entry(v) for k, v in by_legs.items()},
        "by_day": {k: _winrate_entry(v) for k, v in by_day.items()},
        "by_edge_bucket": {k: _winrate_entry(v) for k, v in by_edge_bucket.items()},
        "by_time_of_day": {k: _winrate_entry(v) for k, v in by_time.items()},
        "total_resolved": len(resolved),
    }


# ── 3. Edge Accuracy Lookup ─────────────────────────────────────────────────

def get_edge_accuracy(opportunity: dict) -> str | None:
    """
    Look up an opportunity's type + edge bucket in saved patterns.
    Return a human-readable accuracy string if we have enough data.
    """
    patterns = load_patterns()
    if not patterns:
        return None

    trade_type = opportunity.get("type", "unknown")
    edge = opportunity.get("relative_edge", opportunity.get("edge", 0))
    bucket = _edge_bucket(edge)

    # Check type-level stats
    by_type = patterns.get("by_type", {})
    type_stats = by_type.get(trade_type)

    # Check edge-bucket stats
    by_edge = patterns.get("by_edge_bucket", {})
    edge_stats = by_edge.get(bucket)

    # Prefer the more specific intersection; fall back to edge bucket
    best = None
    if type_stats and type_stats.get("total", 0) >= 10:
        best = type_stats
        label = f"{trade_type} trades"
    if edge_stats and edge_stats.get("total", 0) >= 10:
        # If both qualify, prefer edge bucket as it's more specific to this trade
        best = edge_stats
        label = f"edges in the {bucket} bucket"

    if best is None:
        return None

    winrate_pct = round(best["winrate"] * 100, 1)
    n = best["total"]
    return f"Edges like this have won {winrate_pct}% of the time ({n} trades)"


# ── 4. Streak Detection ─────────────────────────────────────────────────────

def get_streak(trade_history: list[dict]) -> dict:
    """
    Find the current consecutive win or loss streak
    from the most recent resolved trade backwards.
    """
    resolved = _resolved_trades(trade_history)
    if not resolved:
        return {"type": "none", "count": 0}

    # Sort by resolved_at descending (most recent first)
    resolved.sort(
        key=lambda t: t.get("resolved_at", ""),
        reverse=True,
    )

    streak_type = resolved[0].get("result", "lost")  # "won" or "lost"
    streak_label = "win" if streak_type == "won" else "loss"
    count = 0

    for t in resolved:
        if t.get("result") == streak_type:
            count += 1
        else:
            break

    return {"type": streak_label, "count": count}


# ── 5. Correlations ─────────────────────────────────────────────────────────

def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """
    Manual Pearson correlation coefficient.
    Returns None if the calculation is undefined (zero variance).
    """
    n = len(xs)
    if n < 2:
        return None

    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_x2 = sum(x * x for x in xs)
    sum_y2 = sum(y * y for y in ys)

    numerator = n * sum_xy - sum_x * sum_y
    denom_a = n * sum_x2 - sum_x * sum_x
    denom_b = n * sum_y2 - sum_y * sum_y

    if denom_a <= 0 or denom_b <= 0:
        return None

    denominator = math.sqrt(denom_a * denom_b)
    if denominator == 0:
        return None

    return numerator / denominator


def compute_correlations(trade_history: list[dict]) -> dict | None:
    """
    Compute Pearson correlations between edge/confidence and outcomes.
    Requires 20+ resolved trades.
    """
    resolved = _resolved_trades(trade_history)
    if len(resolved) < 20:
        return None

    outcomes = [1.0 if t.get("result") == "won" else 0.0 for t in resolved]
    edges = [t.get("relative_edge", t.get("edge", 0)) for t in resolved]
    confidences = [t.get("confidence", t.get("edge", 0)) for t in resolved]

    r_edge = _pearson(edges, outcomes)
    r_conf = _pearson(confidences, outcomes)

    return {
        "edge_vs_outcome": round(r_edge, 4) if r_edge is not None else None,
        "confidence_vs_outcome": round(r_conf, 4) if r_conf is not None else None,
        "sample_size": len(resolved),
    }


# ── 6. Equity Curve ─────────────────────────────────────────────────────────

def generate_equity_curve(trade_history: list[dict]) -> Path | None:
    """
    Plot cumulative P&L as a line chart and save to equity_chart.png.
    Wins are green markers, losses are red markers.
    Returns the path on success, None if no resolved trades.
    """
    resolved = _resolved_trades(trade_history)
    if not resolved:
        return None

    # Sort by resolved_at ascending
    resolved.sort(key=lambda t: t.get("resolved_at", ""))

    cumulative = []
    running = 0.0
    for t in resolved:
        running += t.get("pnl", 0)
        cumulative.append(running)

    results = [t.get("result") for t in resolved]
    trade_nums = list(range(1, len(resolved) + 1))

    # Lazy import matplotlib
    try:
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning("matplotlib not installed — cannot generate equity curve")
        return None

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(trade_nums, cumulative, color="#4A90D9", linewidth=1.5, zorder=1)

    # Mark wins green, losses red
    win_x = [x for x, r in zip(trade_nums, results) if r == "won"]
    win_y = [y for y, r in zip(cumulative, results) if r == "won"]
    loss_x = [x for x, r in zip(trade_nums, results) if r == "lost"]
    loss_y = [y for y, r in zip(cumulative, results) if r == "lost"]

    ax.scatter(win_x, win_y, color="#2ecc71", s=30, zorder=2, label="Win")
    ax.scatter(loss_x, loss_y, color="#e74c3c", s=30, zorder=2, label="Loss")

    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.7, alpha=0.5)
    ax.set_title("Glint — Equity Curve", fontsize=14, fontweight="bold")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Cumulative P&L ($)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    EQUITY_CHART_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(EQUITY_CHART_PATH), dpi=150)
    plt.close(fig)

    log.info("Saved equity curve to %s", EQUITY_CHART_PATH)
    return EQUITY_CHART_PATH


# ── 7. Threshold Adjustment Suggestions ──────────────────────────────────────

def should_adjust_thresholds(trade_history: list[dict]) -> dict | None:
    """
    After 50+ resolved trades, suggest threshold changes
    based on performance by type.
    """
    resolved = _resolved_trades(trade_history)
    if len(resolved) < 50:
        return None

    patterns = analyze_patterns(trade_history)
    by_type = patterns.get("by_type", {})
    suggestions = []

    # Weather: if win rate > 80%, suggest lowering MIN_RELATIVE_EDGE by 2pp
    weather = by_type.get("weather")
    if weather and weather["total"] >= 5 and weather["winrate"] > 0.80:
        current = MIN_RELATIVE_EDGE
        recommended = round(current - 0.02, 4)
        suggestions.append({
            "type": "weather",
            "current": current,
            "recommended": recommended,
            "reason": (
                f"Weather trades winning at {weather['winrate']:.0%} "
                f"({weather['total']} trades). Safe to lower edge threshold "
                f"from {current:.0%} to {recommended:.0%} to capture more volume."
            ),
        })

    # Vig-stack NO: if win rate < 40%, suggest raising MIN_RELATIVE_EDGE by 5pp
    vig_stack = by_type.get("vig_stack_no")
    if vig_stack and vig_stack["total"] >= 5 and vig_stack["winrate"] < 0.40:
        current = MIN_RELATIVE_EDGE
        recommended = round(current + 0.05, 4)
        suggestions.append({
            "type": "vig_stack_no",
            "current": current,
            "recommended": recommended,
            "reason": (
                f"Vig-stack NO trades winning at only {vig_stack['winrate']:.0%} "
                f"({vig_stack['total']} trades). Recommend raising edge threshold "
                f"from {current:.0%} to {recommended:.0%} to be more selective."
            ),
        })

    if not suggestions:
        return None

    return {"suggestions": suggestions}
