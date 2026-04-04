"""
Hustle Agent — Memory System

Persistent learning across cycles:
- Lessons learned (freeform insights, capped at 50)
- Strategy postmortems (structured, unlimited)
- Research cache (past web research, capped at 30)
- Cycle summaries (one-paragraph recaps, capped at 100)
- Saved scripts (reusable code the agent wrote)
- Tyler takeaways (extracted conversation insights, capped at 100)
"""
from __future__ import annotations

import json
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
TOOLS_DIR = BASE_DIR / "tools"
MEMORY_FILE = STATE_DIR / "memory.json"

EMPTY_MEMORY = {
    "lessons": [],
    "postmortems": [],
    "research_cache": [],
    "cycle_summaries": [],
    "saved_scripts": {},
    "tyler_takeaways": []
}


def _escape_braces(text: str) -> str:
    """Escape { and } so str.format() doesn't choke on user-generated content."""
    return text.replace("{", "{{").replace("}", "}}")


def _atomic_write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)


def load_memory() -> dict:
    if MEMORY_FILE.exists():
        with open(MEMORY_FILE, "r") as f:
            mem = json.load(f)
        for key, default in EMPTY_MEMORY.items():
            mem.setdefault(key, type(default)())
        return mem
    return {k: type(v)() for k, v in EMPTY_MEMORY.items()}


def save_memory(mem: dict):
    _atomic_write_json(MEMORY_FILE, mem)


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------

MAX_LESSONS = 50

def add_lesson(lesson: str, category: str) -> str:
    mem = load_memory()
    mem["lessons"].append({
        "lesson": lesson,
        "category": category,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })
    if len(mem["lessons"]) > MAX_LESSONS:
        mem["lessons"] = mem["lessons"][-MAX_LESSONS:]
    save_memory(mem)
    return f"Lesson recorded ({category}): {lesson[:80]}..."


# ---------------------------------------------------------------------------
# Strategy Postmortems
# ---------------------------------------------------------------------------

def add_postmortem(strategy_name: str, thesis: str, outcome: str, delta: str,
                   lesson: str, would_retry: bool) -> str:
    mem = load_memory()
    mem["postmortems"].append({
        "strategy": strategy_name,
        "thesis": thesis,
        "outcome": outcome,
        "delta": delta,
        "lesson": lesson,
        "would_retry": would_retry,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })
    # Auto-copy lesson into the lessons list
    mem["lessons"].append({
        "lesson": f"[Postmortem: {strategy_name}] {lesson}",
        "category": "strategy",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })
    if len(mem["lessons"]) > MAX_LESSONS:
        mem["lessons"] = mem["lessons"][-MAX_LESSONS:]
    save_memory(mem)
    return f"Postmortem recorded for '{strategy_name}'. Lesson auto-saved to memory."


# ---------------------------------------------------------------------------
# Tyler Takeaways (conversation memory)
# ---------------------------------------------------------------------------

MAX_TYLER_TAKEAWAYS = 100

def add_tyler_takeaway(takeaway: str, type_: str, cycle: int) -> str:
    mem = load_memory()
    mem["tyler_takeaways"].append({
        "takeaway": takeaway,
        "type": type_,
        "cycle": cycle,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })
    if len(mem["tyler_takeaways"]) > MAX_TYLER_TAKEAWAYS:
        mem["tyler_takeaways"] = mem["tyler_takeaways"][-MAX_TYLER_TAKEAWAYS:]
    save_memory(mem)
    return f"Takeaway recorded ({type_}): {takeaway[:60]}..."


def get_tyler_context() -> str:
    """Build YOUR RELATIONSHIP WITH TYLER block for system prompt."""
    mem = load_memory()
    takeaways = mem.get("tyler_takeaways", [])
    if not takeaways:
        return ""
    recent = takeaways[-30:]
    grouped = {"decision": [], "preference": [], "feedback": [], "action_item": []}
    for t in recent:
        bucket = grouped.get(t.get("type", "feedback"), grouped["feedback"])
        bucket.append(t["takeaway"])
    lines = ["YOUR RELATIONSHIP WITH TYLER (extracted from past conversations):"]
    labels = {"decision": "Decisions made", "preference": "Tyler's preferences",
              "feedback": "Feedback given", "action_item": "Outstanding action items"}
    for key, label in labels.items():
        items = grouped[key]
        if items:
            lines.append(f"  {label}:")
            for item in items[-8:]:
                lines.append(f"    - {item}")
    return _escape_braces("\n".join(lines))


# ---------------------------------------------------------------------------
# Research Cache
# ---------------------------------------------------------------------------

MAX_RESEARCH = 30

def save_research(query: str, result: str):
    mem = load_memory()
    mem["research_cache"].append({
        "query": query,
        "result": result[:2000],
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })
    if len(mem["research_cache"]) > MAX_RESEARCH:
        mem["research_cache"] = mem["research_cache"][-MAX_RESEARCH:]
    save_memory(mem)


def search_past_research(query: str) -> str:
    mem = load_memory()
    query_lower = query.lower()
    matches = []
    for entry in mem["research_cache"]:
        if query_lower in entry["query"].lower() or query_lower in entry["result"].lower():
            matches.append(entry)
    if not matches:
        return "No matching past research found."
    matches = matches[-3:]
    parts = []
    for m in matches:
        parts.append(f"Query: {m['query']}\nDate: {m['timestamp'][:10]}\nResult: {m['result'][:500]}")
    return "\n---\n".join(parts)


# ---------------------------------------------------------------------------
# Cycle Summaries
# ---------------------------------------------------------------------------

MAX_SUMMARIES = 100

def add_cycle_summary(cycle_num: int, summary: str):
    mem = load_memory()
    mem["cycle_summaries"].append({
        "cycle": cycle_num,
        "summary": summary,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    })
    if len(mem["cycle_summaries"]) > MAX_SUMMARIES:
        mem["cycle_summaries"] = mem["cycle_summaries"][-MAX_SUMMARIES:]
    save_memory(mem)


# ---------------------------------------------------------------------------
# Saved Scripts
# ---------------------------------------------------------------------------

def save_script(name: str, language: str, code: str, description: str) -> str:
    mem = load_memory()
    mem["saved_scripts"][name] = {
        "language": language,
        "code": code,
        "description": description,
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    save_memory(mem)
    # Also write to tools/ directory
    TOOLS_DIR.mkdir(exist_ok=True)
    ext = ".py" if language == "python" else ".sh"
    (TOOLS_DIR / f"{name}{ext}").write_text(code)
    return f"Script '{name}' saved to memory and tools/{name}{ext}"


def get_script(name: str) -> dict | None:
    mem = load_memory()
    return mem["saved_scripts"].get(name)


def list_scripts() -> str:
    mem = load_memory()
    if not mem["saved_scripts"]:
        return "No saved scripts."
    lines = []
    for name, info in mem["saved_scripts"].items():
        lines.append(f"  - {name} ({info['language']}): {info['description']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context Builders — what the agent sees in its prompt
# ---------------------------------------------------------------------------

def compute_strategy_intelligence(state: dict, ledger: list) -> str:
    """Compute per-strategy ROI from the ledger."""
    if not ledger:
        return "No transactions yet."

    strategies = {}
    for txn in ledger:
        strat = txn.get("strategy", "unknown")
        if strat not in strategies:
            strategies[strat] = {"invested": 0, "returned": 0, "count": 0, "first": txn["timestamp"]}
        strategies[strat]["count"] += 1
        if txn["type"] in ("expense", "investment"):
            strategies[strat]["invested"] += txn["amount"]
        elif txn["type"] in ("income", "return"):
            strategies[strat]["returned"] += txn["amount"]

    lines = []
    for name, s in strategies.items():
        roi = ((s["returned"] - s["invested"]) / s["invested"] * 100) if s["invested"] > 0 else 0
        status = "PROFITABLE" if s["returned"] > s["invested"] else "LOSING" if s["invested"] > 0 else "NEW"
        days = (datetime.datetime.now(datetime.timezone.utc) -
                datetime.datetime.fromisoformat(s["first"])).days
        lines.append(
            f"  - {name}: Invested ${s['invested']:.2f}, Returned ${s['returned']:.2f}, "
            f"ROI: {roi:+.1f}%, {s['count']} txns over {days}d ({status})"
        )
    return _escape_braces("\n".join(lines))


def get_postmortems_context() -> str:
    """Format postmortems for system prompt."""
    mem = load_memory()
    if not mem["postmortems"]:
        return ""
    lines = ["STRATEGY POSTMORTEMS (learn from these):"]
    for pm in mem["postmortems"][-10:]:
        lines.append(
            f"  [{pm['strategy']}] Thesis: {pm['thesis'][:80]} | "
            f"Outcome: {pm['outcome'][:80]} | Lesson: {pm['lesson'][:80]} | "
            f"Retry: {'yes' if pm['would_retry'] else 'no'}"
        )
    return _escape_braces("\n".join(lines))


def get_context_window(n_cycles: int = 10) -> str:
    """Build the memory context block for the system prompt."""
    mem = load_memory()
    parts = []

    # Recent cycle summaries
    summaries = mem["cycle_summaries"][-n_cycles:]
    if summaries:
        parts.append("RECENT CYCLE HISTORY:")
        for s in summaries:
            parts.append(f"  Cycle {s['cycle']}: {s['summary']}")

    # Active lessons
    if mem["lessons"]:
        parts.append("\nLESSONS LEARNED (your accumulated wisdom):")
        for l in mem["lessons"][-20:]:
            parts.append(f"  [{l['category']}] {l['lesson']}")

    # Available scripts
    scripts = list_scripts()
    if scripts != "No saved scripts.":
        parts.append(f"\nSAVED SCRIPTS (reusable tools you built):\n{scripts}")

    result = "\n".join(parts) if parts else "No memory yet — this builds up over time."
    return _escape_braces(result)


def generate_cycle_summary(cycle_num: int, tool_calls: list) -> str:
    """Generate a brief summary from the tools used this cycle."""
    if not tool_calls:
        return "No actions taken."
    action_names = [tc["name"] for tc in tool_calls]
    unique = list(dict.fromkeys(action_names))
    summary = f"Used {len(tool_calls)} tools ({', '.join(unique[:6])})"
    # Add financial context if transactions happened
    txn_tools = [tc for tc in tool_calls if tc["name"] == "record_transaction"]
    if txn_tools:
        summary += f", recorded {len(txn_tools)} transaction(s)"
    return summary
