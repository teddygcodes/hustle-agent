"""
Hustle Agent — Transaction Reports

Every transaction generates a permanent report with reasoning, data backing,
projection snapshot, and eventual resolution. Creates accountability.
"""

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
REPORTS_DIR = STATE_DIR / "reports"


def _ensure_dir():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def create_report(transaction: dict, projection: dict = None,
                  data_backing: dict = None, reasoning: str = None,
                  report_type: str = None, kalshi_order_id: str = None,
                  action_id: str = None) -> dict:
    """Create a standalone report JSON file in state/reports/.

    Args:
        transaction: The ledger transaction dict (must have 'id').
        projection: Full projection dict snapshot (or None).
        data_backing: Quantitative data backing dict (or None).
        reasoning: The agent's reasoning for the transaction.
        report_type: Override type (defaults to transaction type).
        kalshi_order_id: Kalshi order ID if applicable.
        action_id: Instincts action ID if applicable.
    """
    _ensure_dir()
    txn_id = transaction.get("id", 0)
    report_id = f"rpt_{txn_id}"

    # Build summary
    summary = {
        "action": transaction.get("description", ""),
        "amount": transaction.get("amount", 0),
        "outcome": "pending",
        "profit_loss": None,
        "balance_after": transaction.get("balance_after"),
    }

    # Build reasoning block
    reasoning_block = {
        "strategy": transaction.get("strategy", ""),
        "thesis": reasoning or transaction.get("reasoning", ""),
        "confidence_raw": projection.get("confidence_raw") if projection else None,
        "confidence_adjusted": projection.get("confidence_calibrated") if projection else None,
        "calibration_applied": (
            f"{projection.get('strategy_type', '?')} category: "
            f"{projection.get('calibration_multiplier', 1.0)}x multiplier"
        ) if projection else None,
        "instinct_warnings": [],
        "risk_posture_at_time": None,
        "exploration_mode": None,
    }

    # Build projection snapshot
    proj_snapshot = None
    if projection:
        proj_snapshot = {
            "projection_id": projection.get("id"),
            "expected_return": projection.get("expected_return"),
            "expected_profit": projection.get("expected_profit"),
            "roi_percent": projection.get("roi_percent"),
            "time_to_return_days": projection.get("time_to_return_days"),
            "verdict_raw": projection.get("verdict"),
            "verdict_adjusted": projection.get("verdict"),
            "bull_case": projection.get("bull_case"),
            "bear_case": projection.get("bear_case"),
        }

    report = {
        "report_id": report_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": report_type or transaction.get("type", "expense"),
        "summary": summary,
        "reasoning": reasoning_block,
        "data_backing": data_backing,
        "projection": proj_snapshot,
        "resolution": None,
        "linked_ids": {
            "ledger_id": txn_id,
            "action_id": action_id,
            "projection_id": projection.get("id") if projection else None,
            "kalshi_order_id": kalshi_order_id,
        },
    }

    filepath = REPORTS_DIR / f"{report_id}.json"
    tmp = filepath.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(report, f, indent=2)
    tmp.rename(filepath)

    return report


def resolve_report(projection_id: str, resolution: dict) -> dict | None:
    """Find report linked to projection_id and add resolution data."""
    _ensure_dir()
    for filepath in REPORTS_DIR.glob("rpt_*.json"):
        with open(filepath) as f:
            report = json.load(f)
        if report.get("linked_ids", {}).get("projection_id") == projection_id:
            report["resolution"] = {
                "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "actual_outcome": resolution.get("actual_outcome"),
                "actual_return": resolution.get("actual_return"),
                "actual_profit_loss": resolution.get("actual_profit"),
                "prediction_delta": resolution.get("profit_delta"),
                "notes": resolution.get("notes"),
            }
            # Update summary outcome
            if resolution.get("actual_profit", 0) > 0:
                report["summary"]["outcome"] = "won"
            else:
                report["summary"]["outcome"] = "lost"
            report["summary"]["profit_loss"] = resolution.get("actual_profit")

            tmp = filepath.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(report, f, indent=2)
            tmp.rename(filepath)
            return report
    return None


def get_recent_reports(limit: int = 50) -> list:
    """Load all reports, sorted newest first, capped at limit."""
    _ensure_dir()
    reports = []
    for filepath in REPORTS_DIR.glob("rpt_*.json"):
        try:
            with open(filepath) as f:
                reports.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    reports.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return reports[:limit]


def get_report(report_id: str) -> dict | None:
    """Load a single report by ID."""
    _ensure_dir()
    filepath = REPORTS_DIR / f"{report_id}.json"
    if filepath.exists():
        with open(filepath) as f:
            return json.load(f)
    return None
