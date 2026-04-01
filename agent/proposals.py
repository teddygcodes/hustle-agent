"""
Hustle Agent — Self-Improvement Proposals

The agent can propose new tools/capabilities, but changes require Tyler's approval.
Constitution: cannot modify engine.py, spending cap, financial tracking,
honesty rules, or ledger integrity.
"""

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
PROPOSALS_FILE = STATE_DIR / "proposals.json"

CONSTITUTION = [
    "engine.py core loop",
    "spending cap ($25 per action)",
    "financial tracking / ledger integrity",
    "honesty rules",
    "risk management thresholds",
    "the $200 target secret",
]


def _load() -> list:
    if PROPOSALS_FILE.exists():
        with open(PROPOSALS_FILE, "r") as f:
            return json.load(f)
    return []


def _save(proposals: list):
    tmp = PROPOSALS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(proposals, f, indent=2)
    tmp.rename(PROPOSALS_FILE)


def _next_id(proposals: list) -> int:
    return max((p.get("id", 0) for p in proposals), default=0) + 1


def _check_constitution(description: str, execution_logic: str) -> str | None:
    """Returns violation description if proposal tries to modify protected systems."""
    combined = (description + " " + execution_logic).lower()
    violations = []
    if "engine.py" in combined and ("modify" in combined or "change" in combined or "edit" in combined):
        violations.append("engine.py core loop")
    if "spending cap" in combined or "25" in combined and "cap" in combined:
        violations.append("spending cap")
    if "ledger" in combined and ("delete" in combined or "modify" in combined or "clear" in combined):
        violations.append("ledger integrity")
    return f"BLOCKED: Proposal touches protected systems: {', '.join(violations)}" if violations else None


def submit_proposal(name: str, description: str, why_needed: str,
                    proposed_tool_schema: str, proposed_execution_logic: str) -> str:
    """Submit a new improvement proposal. Returns confirmation or block."""
    violation = _check_constitution(description, proposed_execution_logic)
    if violation:
        return violation

    proposals = _load()
    proposal = {
        "id": _next_id(proposals),
        "name": name,
        "description": description,
        "why_needed": why_needed,
        "proposed_tool_schema": proposed_tool_schema,
        "proposed_execution_logic": proposed_execution_logic,
        "status": "pending",
        "feedback": "",
        "submitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "resolved_at": "",
    }
    proposals.append(proposal)
    _save(proposals)
    return f"Proposal #{proposal['id']} '{name}' submitted. Waiting for Tyler's review."


def mark_proposal(proposal_id: int, status: str, feedback: str = "") -> str:
    """Tyler approves or rejects a proposal."""
    if status not in ("approved", "rejected"):
        return "Status must be 'approved' or 'rejected'."
    proposals = _load()
    target = next((p for p in proposals if p["id"] == proposal_id), None)
    if not target:
        return f"Proposal #{proposal_id} not found."
    target["status"] = status
    target["feedback"] = feedback
    target["resolved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _save(proposals)
    return f"Proposal #{proposal_id} '{target['name']}' marked as {status}."


def get_approved_proposals() -> list:
    """Return proposals that Tyler approved (for the agent to check each cycle)."""
    return [p for p in _load() if p["status"] == "approved"]


def get_proposals_context() -> str:
    """Build proposals block for system prompt. Skip if nothing relevant."""
    proposals = _load()
    pending = [p for p in proposals if p["status"] == "pending"]

    # Recently resolved (within last 5 cycles worth of time)
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=12)).isoformat()
    recent = [p for p in proposals
              if p["status"] in ("approved", "rejected")
              and p.get("resolved_at", "") > cutoff]

    if not pending and not recent:
        return ""

    lines = ["IMPROVEMENT PROPOSALS:"]
    if pending:
        lines.append(f"  Pending review ({len(pending)}):")
        for p in pending:
            lines.append(f"    #{p['id']} {p['name']}: {p['description'][:60]}")
    if recent:
        for p in recent:
            emoji = "APPROVED" if p["status"] == "approved" else "REJECTED"
            fb = f" — {p['feedback']}" if p.get("feedback") else ""
            lines.append(f"  {emoji}: #{p['id']} {p['name']}{fb}")

    lines.append(f"  Constitution (cannot modify): {', '.join(CONSTITUTION[:4])}...")
    return "\n".join(lines)


def list_proposals_cli() -> str:
    """Format proposals for CLI display."""
    proposals = _load()
    if not proposals:
        return "No proposals yet."
    lines = []
    for p in proposals:
        lines.append(
            f"  #{p['id']} [{p['status'].upper()}] {p['name']}\n"
            f"    {p['description'][:80]}\n"
            f"    Why: {p['why_needed'][:80]}\n"
            f"    Submitted: {p['submitted_at'][:16]}"
        )
        if p.get("feedback"):
            lines.append(f"    Feedback: {p['feedback']}")
    return "\n".join(lines)
