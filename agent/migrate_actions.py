"""
Hustle Agent — Backfill Migration

One-time script to create action entries from existing resolved projections
and ledger data. Idempotent — skips actions that already exist by projection_id.

Usage:
    python3 agent/migrate_actions.py
"""

import json
import uuid
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
PROJECTIONS_FILE = STATE_DIR / "projections.json"
LEDGER_FILE = STATE_DIR / "ledger.json"
ACTIONS_FILE = STATE_DIR / "actions.json"
STATE_FILE = STATE_DIR / "agent_state.json"

# Same map as instincts.py
CATEGORY_MAP = {
    "polymarket": "polymarket",
    "product_sale": "product",
    "product": "product",
    "service": "service",
    "content": "content",
    "arbitrage": "arbitrage",
    "outreach": "outreach",
}


def normalize_category(strategy_type: str) -> str:
    return CATEGORY_MAP.get(strategy_type.lower().strip(), "other")


def load_json(path: Path, default=None):
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return default if default is not None else []


def atomic_write(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)


def migrate():
    projections = load_json(PROJECTIONS_FILE, [])
    ledger = load_json(LEDGER_FILE, [])
    actions = load_json(ACTIONS_FILE, [])
    state = load_json(STATE_FILE, {})

    # Index existing actions by projection_id for idempotency
    existing_proj_ids = {a["projection_id"] for a in actions if a.get("projection_id")}

    # Build a ledger lookup: strategy+description → transaction
    ledger_by_desc = {}
    for txn in ledger:
        key = f"{txn.get('strategy', '')}:{txn.get('description', '')[:50]}"
        ledger_by_desc[key] = txn

    resolved = [p for p in projections if p.get("status") == "resolved" and p.get("resolution")]
    created = 0
    skipped = 0

    for proj in resolved:
        if proj["id"] in existing_proj_ids:
            skipped += 1
            continue

        r = proj["resolution"]
        category = normalize_category(proj.get("strategy_type", "other"))

        # Try to find the matching ledger entry for balance context
        balance_at_time = state.get("balance", 100)

        # Determine time of day from projection timestamp
        try:
            ts = datetime.datetime.fromisoformat(proj["timestamp"])
            hour = ts.hour
            if 6 <= hour < 12:
                time_of_day = "morning"
            elif 12 <= hour < 17:
                time_of_day = "afternoon"
            elif 17 <= hour < 21:
                time_of_day = "evening"
            else:
                time_of_day = "night"
            day_of_week = "weekend" if ts.weekday() >= 5 else "weekday"
        except (ValueError, TypeError):
            time_of_day = "unknown"
            day_of_week = "unknown"

        # Determine risk posture at time
        if balance_at_time >= 90:
            risk_posture = "aggressive"
        elif balance_at_time >= 70:
            risk_posture = "normal"
        else:
            risk_posture = "preservation"

        # Determine status
        status = "won" if r.get("hit") else "lost"

        action = {
            "action_id": str(uuid.uuid4())[:8],
            "projection_id": proj["id"],
            "timestamp": proj["timestamp"],
            "category": category,
            "subcategory": proj.get("action", "")[:80],
            "cost": proj.get("cost", 0),
            "conditions": {
                "time_horizon_days": proj.get("time_to_return_days", 0),
                "market_odds": None,
                "confidence_at_decision": proj.get("confidence_raw", 50),
                "capital_percentage": round((proj.get("cost", 0) / balance_at_time * 100) if balance_at_time > 0 else 0, 1),
                "time_of_day": time_of_day,
                "day_of_week": day_of_week,
                "risk_posture_at_time": risk_posture,
                "balance_at_time": balance_at_time,
            },
            "expected_return": proj.get("expected_return", 0),
            "status": status,
            "actual_return": r.get("actual_return", 0),
            "actual_time_days": r.get("actual_time_days", 0),
            "resolved_at": r.get("timestamp", ""),
        }
        actions.append(action)
        created += 1

    if created > 0:
        atomic_write(ACTIONS_FILE, actions)

    print(f"Migration complete: {created} actions created, {skipped} skipped (already exist).")
    print(f"Total actions in file: {len(actions)}")

    if created > 0:
        # Trigger instinct recomputation
        try:
            from agent import instincts
            instincts.seed_priors()
            data = instincts.recompute_instincts()
            print(f"Instincts recomputed: {data['action_count_at_compute']} resolved actions, mode: {data['exploration_mode']}")
        except Exception as e:
            print(f"Warning: Could not recompute instincts: {e}")
            print("Run manually: python3 -c 'from agent import instincts; instincts.recompute_instincts()'")


if __name__ == "__main__":
    migrate()
