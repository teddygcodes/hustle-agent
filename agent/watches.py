"""
Hustle Agent — Time Awareness & Watches

Watches are conditions the agent sets to be reminded about later.
Linked to projections for the resolution flow.
Smart scheduling adjusts loop interval based on urgency.
"""

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
WATCHES_FILE = STATE_DIR / "watches.json"


def _load() -> list:
    if WATCHES_FILE.exists():
        with open(WATCHES_FILE, "r") as f:
            return json.load(f)
    return []


def _save(watches: list):
    tmp = WATCHES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(watches, f, indent=2)
    tmp.rename(WATCHES_FILE)


def _next_id(watches: list) -> int:
    return max((w.get("id", 0) for w in watches), default=0) + 1


def add_watch(condition: str, action_hint: str, check_after: str,
              expires_at: str = "", projection_id: str = "") -> str:
    """Add a watch. check_after is ISO datetime string."""
    watches = _load()
    watch = {
        "id": _next_id(watches),
        "condition": condition,
        "action_hint": action_hint,
        "check_after": check_after,
        "expires_at": expires_at,
        "projection_id": projection_id,
        "status": "active",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "triggered_at": "",
    }
    watches.append(watch)
    _save(watches)
    proj_note = f" (linked to projection {projection_id})" if projection_id else ""
    return f"Watch #{watch['id']} set: '{condition}' — check after {check_after}{proj_note}"


def check_watches() -> list:
    """Check all active watches. Returns list of triggered watches."""
    watches = _load()
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    triggered = []
    changed = False

    for w in watches:
        if w["status"] != "active":
            continue

        # Check expiry
        if w.get("expires_at") and w["expires_at"] < now_iso:
            w["status"] = "expired"
            changed = True
            continue

        # Check if it's time
        if w["check_after"] <= now_iso:
            w["status"] = "triggered"
            w["triggered_at"] = now_iso
            triggered.append(w)
            changed = True

    if changed:
        _save(watches)
    return triggered


def has_urgent_watches() -> bool:
    """Check if there are watches that trigger within the next hour."""
    watches = _load()
    soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
    return any(
        w["status"] == "active" and w["check_after"] <= soon
        for w in watches
    )


def get_active_count() -> int:
    return sum(1 for w in _load() if w["status"] == "active")


def get_watches_context() -> str:
    """Build watches block for system prompt. Skip if no active watches."""
    watches = _load()
    active = [w for w in watches if w["status"] == "active"]

    if not active:
        return ""

    now = datetime.datetime.now(datetime.timezone.utc)
    lines = [f"ACTIVE WATCHES ({len(active)}):"]
    for w in active:
        try:
            check_time = datetime.datetime.fromisoformat(w["check_after"])
            delta = check_time - now
            if delta.total_seconds() < 0:
                time_str = "OVERDUE"
            elif delta.total_seconds() < 3600:
                time_str = f"in {int(delta.total_seconds()/60)}min"
            elif delta.days < 1:
                time_str = f"in {delta.seconds//3600}h"
            else:
                time_str = f"in {delta.days}d"
        except (ValueError, TypeError):
            time_str = w["check_after"][:10]

        proj_note = f" [proj:{w['projection_id']}]" if w.get("projection_id") else ""
        lines.append(f"  #{w['id']} {w['condition'][:50]} — {time_str}{proj_note}")
        lines.append(f"    Action: {w['action_hint'][:60]}")

    return "\n".join(lines)


def get_time_context() -> str:
    """Current time awareness for the agent."""
    now = datetime.datetime.now(datetime.timezone.utc)
    day = now.strftime("%A")
    time_str = now.strftime("%H:%M UTC")
    date_str = now.strftime("%Y-%m-%d")

    # Business context
    hour = now.hour
    if 9 <= hour <= 17:
        market_status = "US business hours (good for outreach, markets open)"
    elif 6 <= hour <= 9:
        market_status = "Pre-market (good for research and planning)"
    elif 17 <= hour <= 21:
        market_status = "After-hours (good for content creation, async work)"
    else:
        market_status = "Off-hours (good for automated tasks, batch processing)"

    return f"TIME: {day} {date_str} {time_str} — {market_status}"


def compute_smart_interval(base_interval: int, pipeline: list) -> int:
    """Adjust loop interval based on urgency."""
    if has_urgent_watches():
        return min(base_interval, 60)  # Check every minute if something urgent

    # Active pipeline items that are time-sensitive
    active_deals = [i for i in pipeline
                    if i.get("stage") in ("negotiating", "deal_pending", "outreach_sent")]
    if active_deals:
        return min(base_interval, 120)  # Every 2 min with active deals

    if get_active_count() > 0:
        return min(base_interval, 180)  # Every 3 min with any active watches

    return min(base_interval, 600)  # Chill mode: every 10 min
