"""
Hustle Agent — Revenue Pipeline

Tracks deals from lead through close, recurring revenue streams,
and scheduled actions. The agent thinks in pipelines, not one-shots.
"""

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
PIPELINE_FILE = STATE_DIR / "pipeline.json"

STAGES = ["lead", "outreach_sent", "negotiating", "deal_pending",
          "closed_won", "closed_lost", "recurring"]


def _load() -> list:
    if PIPELINE_FILE.exists():
        with open(PIPELINE_FILE, "r") as f:
            return json.load(f)
    return []


def _save(items: list):
    tmp = PIPELINE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(items, f, indent=2)
    tmp.rename(PIPELINE_FILE)


def _next_id(items: list) -> int:
    return max((i.get("id", 0) for i in items), default=0) + 1


def upsert_pipeline_item(name: str, stage: str, strategy: str,
                         description: str, expected_value: float = 0,
                         expected_close_date: str = "",
                         notes: str = "") -> str:
    """Add or update a pipeline item. Matches by name."""
    if stage not in STAGES:
        return f"Invalid stage '{stage}'. Must be one of: {', '.join(STAGES)}"

    items = _load()
    existing = next((i for i in items if i["name"] == name), None)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if existing:
        old_stage = existing["stage"]
        existing["stage"] = stage
        existing["strategy"] = strategy
        existing["description"] = description
        if expected_value:
            existing["expected_value"] = expected_value
        if expected_close_date:
            existing["expected_close_date"] = expected_close_date
        if notes:
            existing["notes"] = notes
        existing["updated_at"] = now
        if "history" not in existing:
            existing["history"] = []
        existing["history"].append({"from": old_stage, "to": stage, "at": now})
        _save(items)
        return f"Pipeline '{name}' updated: {old_stage} → {stage}"
    else:
        item = {
            "id": _next_id(items),
            "name": name,
            "stage": stage,
            "strategy": strategy,
            "description": description,
            "expected_value": expected_value,
            "expected_close_date": expected_close_date,
            "notes": notes,
            "created_at": now,
            "updated_at": now,
            "history": [{"from": "new", "to": stage, "at": now}],
        }
        items.append(item)
        _save(items)
        return f"Pipeline '{name}' added at stage '{stage}'"


def get_pipeline_context() -> str:
    """Build pipeline block for system prompt. Only active items."""
    items = _load()
    active = [i for i in items if i["stage"] not in ("closed_won", "closed_lost")]

    # Also include recently closed (within last 5 days)
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)).isoformat()
    recent_closed = [i for i in items
                     if i["stage"] in ("closed_won", "closed_lost")
                     and i.get("updated_at", "") > cutoff]

    if not active and not recent_closed:
        return ""

    lines = ["REVENUE PIPELINE:"]

    # Group active by stage
    by_stage = {}
    for item in active:
        by_stage.setdefault(item["stage"], []).append(item)

    for stage in STAGES:
        if stage in ("closed_won", "closed_lost"):
            continue
        stage_items = by_stage.get(stage, [])
        if stage_items:
            lines.append(f"  {stage.replace('_', ' ').title()} ({len(stage_items)}):")
            for item in stage_items:
                val = f" — ${item['expected_value']:.2f}" if item.get("expected_value") else ""
                date = f" by {item['expected_close_date']}" if item.get("expected_close_date") else ""
                lines.append(f"    {item['name']}{val}{date}")

    if recent_closed:
        lines.append("  Recently closed:")
        for item in recent_closed:
            result = "WON" if item["stage"] == "closed_won" else "LOST"
            lines.append(f"    {item['name']} — {result}")

    total_expected = sum(i.get("expected_value", 0) for i in active)
    if total_expected > 0:
        lines.append(f"  Total pipeline value: ${total_expected:.2f}")

    return "\n".join(lines)
