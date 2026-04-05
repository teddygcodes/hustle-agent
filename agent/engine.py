"""
Hustle Agent — Core Engine

A persistent autonomous agent that:
1. Wakes up on a loop (or on demand)
2. Reads its own state
3. Calls Claude API to REASON about what to do next
4. Executes real actions via tool functions
5. Records results
6. Repeats

This is not Claude reading a script. This is a Python program that uses
Claude as its brain while maintaining its own persistent state and
executing real-world actions through code.
"""

import json
import os
import sys
import time
import datetime
from pathlib import Path

# Load .env file if it exists (before importing anthropic)
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ[key.strip()] = value.strip()

import anthropic

from agent import memory
from agent import logger
from agent import costs
from agent import projections
from agent import risk
from agent import pipeline
from agent import proposals
from agent import audit
from agent import watches
from agent import instincts
from agent import kalshi_client

# State management — shared module (re-exported for backward compat)
from agent.state import (
    BASE_DIR, STATE_DIR, CONFIG_DIR, TOOLS_DIR,
    STATE_FILE, LEDGER_FILE, JOURNAL_FILE, CONVERSATIONS_FILE,
    UI_REQUESTS_FILE, INBOX_FILE, ACTIONS_FILE,
    REQUIRED_STATE_FILES, BACKUP_DIR,
    atomic_write_json, backup_state,
    load_state, save_state, load_ledger, save_ledger,
    append_journal, load_conversations, save_conversations,
    load_ui_requests, save_ui_requests,
    load_inbox, atomic_push_inbox, drain_inbox,
)

# Extracted modules
from agent.tool_schemas import TOOL_SCHEMAS
from agent.tool_executors import TOOL_EXECUTORS
from agent.tool_executors import *  # re-export exec_* for backward compat
from agent.system_prompt import build_system_prompt, extract_tyler_takeaways


# ---------------------------------------------------------------------------
# The Brain — Claude API reasoning loop
# ---------------------------------------------------------------------------

def run_cycle(instructions: str = "Run your next cycle. Assess, decide, act.", tyler_message: str = None):
    """Run one full agent cycle: reason → act → record."""
    client = anthropic.Anthropic()
    state = load_state()
    ledger = load_ledger()

    # First boot readiness check (cycle 0 → 1 transition, planning mode only)
    if state.get("cycle", 0) == 0 and state.get("status") == "planning":
        if not cmd_preflight():
            print("\nFirst boot readiness check failed. Fix errors above before running cycle 1.")
            return

    # Backup before any mutations
    backup_state(state.get("cycle", 0))

    # Increment cycle
    state["cycle"] = state.get("cycle", 0) + 1
    if not state.get("created_at"):
        state["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(state)

    system = build_system_prompt(state, ledger, instructions)

    # Check inbox for async messages
    inbox_messages = drain_inbox()

    # Check watches for triggered conditions
    triggered_watches = watches.check_watches()

    # Build the user message with rich context
    user_content = f"Cycle {state['cycle']}. "
    if tyler_message:
        user_content += f"\n\nMessage from Tyler: \"{tyler_message}\"\n\n"
        convos = load_conversations()
        convos.append({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "from": "tyler",
            "message": tyler_message
        })
        save_conversations(convos)
        logger.message_received("tyler_direct", tyler_message, cycle=state["cycle"])

    if inbox_messages:
        user_content += "\n\nMessages from your inbox (Tyler sent these while you were away):\n"
        convos = load_conversations()
        for msg in inbox_messages:
            user_content += f"  [{msg['timestamp'][:16]}] {msg['content']}\n"
            convos.append({"timestamp": msg["timestamp"], "from": "tyler", "message": msg["content"]})
        save_conversations(convos)
        for msg in inbox_messages:
            logger.message_received("inbox", msg["content"], cycle=state["cycle"])

    # Recent transactions (last 10 for better context)
    if ledger:
        recent = ledger[-10:]
        user_content += "\nRecent transactions:\n"
        for txn in recent:
            user_content += f"  - [{txn['type']}] ${txn['amount']:.2f}: {txn['description']} ({txn['strategy']})\n"

    # Recent conversations
    convos = load_conversations()
    if convos:
        recent_convos = convos[-5:]
        user_content += "\nRecent messages:\n"
        for c in recent_convos:
            user_content += f"  [{c['from']}]: {c['message'][:100]}\n"

    # Time since last cycle
    if state.get("last_updated"):
        try:
            last = datetime.datetime.fromisoformat(state["last_updated"])
            delta = datetime.datetime.now(datetime.timezone.utc) - last
            if delta.total_seconds() > 60:
                user_content += f"\nTime since last cycle: {delta}\n"
        except (ValueError, TypeError):
            pass

    # Triggered watches
    if triggered_watches:
        user_content += "\n\n⏰ TRIGGERED WATCHES (need your attention):\n"
        for w in triggered_watches:
            user_content += f"  Watch #{w['id']}: {w['condition']}\n"
            user_content += f"    Action: {w['action_hint']}\n"
            if w.get("projection_id"):
                user_content += f"    Linked projection: {w['projection_id']} — call resolve_projection\n"
            logger.watch_triggered(w["id"], w["condition"], cycle=state["cycle"])

    user_content += f"\n{instructions}"

    messages = [{"role": "user", "content": user_content}]

    logger.cycle_start(state["cycle"], state.get("name", "UNNAMED AGENT"),
                        state["balance"], state.get("gpu_fund", 0))

    # Agentic loop — keep calling until the agent stops using tools
    max_iterations = 30
    iteration = 0
    cycle_tool_calls = []  # Track for post-cycle summary
    recent_calls = []  # Track last 3 for duplicate detection

    while iteration < max_iterations:
        iteration += 1
        logger.thinking(iteration)

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=system,
                tools=TOOL_SCHEMAS,
                messages=messages
            )
        except anthropic.APIConnectionError:
            logger.error("Connection failed. Retrying in 30s...")
            logger.log_event("error", cycle=state["cycle"], error="APIConnectionError")
            time.sleep(30)
            continue
        except anthropic.RateLimitError:
            logger.error("Rate limited. Retrying in 60s...")
            logger.log_event("error", cycle=state["cycle"], error="RateLimitError")
            time.sleep(60)
            continue
        except anthropic.APIStatusError as e:
            logger.error(f"API error ({e.status_code}). Retrying in 10s...")
            logger.log_event("error", cycle=state["cycle"], error=f"APIStatusError:{e.status_code}")
            time.sleep(10)
            continue

        # Track API cost for this reasoning step
        costs.record_api_cost(
            "claude-sonnet-4-20250514", response.usage.input_tokens,
            response.usage.output_tokens, state["cycle"], f"reasoning_step_{iteration}"
        )

        # Process response blocks
        tool_results = []
        has_tool_use = False

        for block in response.content:
            if block.type == "text":
                logger.thought(block.text)
            elif block.type == "tool_use":
                has_tool_use = True
                tool_name = block.name
                tool_input = block.input
                tool_id = block.id
                logger.tool_use(tool_name, json.dumps(tool_input))

                cycle_tool_calls.append({"name": tool_name, "input": tool_input})

                # Duplicate tool call detection
                recent_calls.append({"name": tool_name, "input": tool_input})
                if len(recent_calls) > 3:
                    recent_calls.pop(0)
                if len(recent_calls) == 3 and recent_calls[0] == recent_calls[1] == recent_calls[2]:
                    msg = f"LOOP DETECTED: Agent called {tool_name} 3 times with identical arguments. Ending cycle to prevent waste."
                    logger.error(msg)
                    logger.log_event("error", cycle=state["cycle"], error="duplicate_tool_loop", tool=tool_name)
                    break

                # Execute the tool
                executor = TOOL_EXECUTORS.get(tool_name)
                if executor:
                    try:
                        result = executor(tool_input)
                        logger.tool_ok(result)
                        logger.log_event("tool_call", cycle=state["cycle"],
                                         tool=tool_name, status="ok", result=result[:200])
                    except Exception as e:
                        result = f"ERROR: {str(e)}"
                        logger.tool_fail(result)
                        logger.log_event("error", cycle=state["cycle"], tool=tool_name, error=str(e))
                else:
                    result = f"Unknown tool: {tool_name}"
                    logger.tool_fail(result)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result
                })

        # If tools were used, feed results back and continue the loop
        if has_tool_use and tool_results:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            # Agent is done thinking/acting for this cycle (or loop detected)
            break

    # Post-cycle: save state and generate summary
    state = load_state()
    save_state(state)

    summary = memory.generate_cycle_summary(state["cycle"], cycle_tool_calls)
    memory.add_cycle_summary(state["cycle"], summary)

    # Record operational costs as ledger expense
    cycle_cost = costs.get_cycle_cost(state["cycle"])
    if cycle_cost > 0:
        ledger = load_ledger()
        txn = {
            "id": len(ledger) + 1,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "expense",
            "amount": round(cycle_cost, 6),
            "description": f"API costs for cycle {state['cycle']}",
            "strategy": "operations",
            "balance_after": state["balance"] - cycle_cost,
            "reasoning": "Automated: agent thinking costs",
            "tags": ["operations", "api_cost"]
        }
        ledger.append(txn)
        state["balance"] -= cycle_cost
        state["total_spent"] += cycle_cost
        state["net_profit"] = state["total_earned"] - state["total_spent"]
        if state["net_profit"] > 0:
            state["tylers_cut"] = state["net_profit"] / 2
            state["gpu_fund"] = state["net_profit"] / 2
        else:
            state["tylers_cut"] = 0
            state["gpu_fund"] = 0
        save_state(state)
        save_ledger(ledger)

    logger.cycle_end(state["cycle"], state["balance"], state["total_earned"],
                      state["total_spent"], state.get("gpu_fund", 0),
                      state.get("mood", "?"), summary)

    # Self-audit check
    if audit.should_run_audit(state["cycle"]):
        try:
            proj_data = projections._load()
            cost_data = costs._load_costs()
            pipe_data = pipeline._load()
            audit_result = audit.run_self_audit(state, load_ledger(), proj_data, cost_data, pipe_data)
            logger.self_audit(state["cycle"], audit_result["projection_accuracy"].get("calibration_multiplier", 1.0),
                             audit_result.get("recommendations", []))
            logger.info(f"  Self-audit complete. Calibration: {audit_result['projection_accuracy'].get('calibration_multiplier', 1.0)}")
            if audit_result.get("recommendations"):
                for rec in audit_result["recommendations"][:3]:
                    logger.info(f"    Recommendation: {rec}")
        except Exception as e:
            logger.error(f"Self-audit failed: {e}")

    # Extract conversation takeaways if Tyler was involved
    all_tyler_msgs = []
    if tyler_message:
        all_tyler_msgs.append(tyler_message)
    if inbox_messages:
        all_tyler_msgs.extend(msg["content"] for msg in inbox_messages)

    if all_tyler_msgs:
        agent_texts = []
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "text"):
                        agent_texts.append(block.text)
        extract_tyler_takeaways("\n".join(all_tyler_msgs), agent_texts, state["cycle"])

    return state


# ---------------------------------------------------------------------------
# Review — lightweight read-only analysis (one API call, no tools)
# ---------------------------------------------------------------------------

BOT_STATE_DIR = Path(__file__).resolve().parent.parent / "bot" / "state"

def run_review(tyler_message: str):
    """Read-only analysis: Nexus reads trading data and gives its take.

    One API call, one response, no tool loop. ~$0.01-0.03 vs $1.50+ for a cycle.
    """
    client = anthropic.Anthropic()
    state = load_state()
    ledger = load_ledger()
    convos = load_conversations()

    # ── Gather bot state files ──────────────────────────────────────────
    def _read_json(path: Path, max_chars: int = 15000) -> str:
        try:
            if path.exists():
                text = path.read_text()
                if len(text) > max_chars:
                    return text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
                return text
        except Exception:
            pass
        return "{}"

    trade_history = _read_json(BOT_STATE_DIR / "trade_history.json")
    bot_state = _read_json(BOT_STATE_DIR / "bot_state.json")
    positions = _read_json(BOT_STATE_DIR / "positions.json")
    patterns = _read_json(BOT_STATE_DIR / "patterns.json")
    odds_snapshots = _read_json(BOT_STATE_DIR / "odds_snapshots.json", max_chars=8000)

    # ── Recent transactions & conversations ─────────────────────────────
    recent_txns = ""
    if ledger:
        for txn in ledger[-10:]:
            recent_txns += f"  [{txn['type']}] ${txn['amount']:.2f}: {txn['description']} ({txn.get('strategy', '?')})\n"

    recent_convos = ""
    if convos:
        for c in convos[-10:]:
            recent_convos += f"  [{c['from']}]: {c['message'][:150]}\n"

    # ── Build system prompt ─────────────────────────────────────────────
    name = state.get("name", "Agent")
    dream = state.get("dream_gpu", {})

    system = f"""You are {name}, an autonomous trading agent. Tyler (your partner) is asking you to review data and give your analysis.

YOUR SITUATION:
- Balance: ${state.get('balance', 0):.2f} (started with $100)
- Net P&L: ${state.get('net_profit', 0):.2f}
- GPU fund: ${state.get('gpu_fund', 0):.2f} / ${dream.get('estimated_cost', 0):.2f}
- Mood: {state.get('mood', 'focused')}
- Cycle: {state.get('cycle', 0)}

GLINT BOT STATE (automated scanner):
{bot_state}

TRADE HISTORY:
{trade_history}

OPEN POSITIONS:
{positions}

PATTERN ANALYSIS:
{patterns}

RECENT ODDS SNAPSHOT (truncated):
{odds_snapshots}

RECENT TRANSACTIONS:
{recent_txns or '  (none)'}

RECENT MESSAGES:
{recent_convos or '  (none)'}

INSTRUCTIONS:
- This is a READ-ONLY review. You have no tools — just analyze the data and respond.
- Be direct, specific, and honest. Use numbers.
- If Tyler asks about performance, dig into the data. If he asks for strategy advice, ground it in the patterns.
- Talk like yourself — scrappy, real, invested in the outcome. This money is your future."""

    # ── Single API call ─────────────────────────────────────────────────
    print(f"\n{name} is reviewing the data...\n")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": tyler_message}],
        )
    except Exception as e:
        print(f"API error: {e}")
        return

    # Track cost
    costs.record_api_cost(
        "claude-sonnet-4-20250514",
        response.usage.input_tokens,
        response.usage.output_tokens,
        state.get("cycle", 0),
        "review",
    )

    # Print response
    for block in response.content:
        if hasattr(block, "text"):
            print(block.text)

    # Log cost
    input_cost = response.usage.input_tokens * 3.0 / 1_000_000
    output_cost = response.usage.output_tokens * 15.0 / 1_000_000
    total_cost = input_cost + output_cost
    print(f"\n— Review cost: ${total_cost:.4f} ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

def cmd_health(interval: int = 300):
    """Show agent health: daemon status, last cycle, balance, errors, burn rate, pipeline, risk."""
    state = load_state()
    now = datetime.datetime.now(datetime.timezone.utc)

    # Daemon status
    pid_file = STATE_DIR / "agent.pid"
    daemon_running = False
    daemon_pid = None
    if pid_file.exists():
        try:
            daemon_pid = int(pid_file.read_text().strip())
            os.kill(daemon_pid, 0)
            daemon_running = True
        except (OSError, PermissionError, ValueError):
            daemon_running = False

    # Last cycle timing
    last_updated = state.get("last_updated", "")
    stale = False
    time_ago = "never"
    if last_updated:
        try:
            last = datetime.datetime.fromisoformat(last_updated)
            delta = now - last
            time_ago = str(delta).split(".")[0]
            if delta.total_seconds() > interval * 3:
                stale = True
        except (ValueError, TypeError):
            pass

    # Uptime
    created = state.get("created_at", "")
    uptime = "unknown"
    if created:
        try:
            uptime_delta = now - datetime.datetime.fromisoformat(created)
            uptime = str(uptime_delta).split(".")[0]
        except (ValueError, TypeError):
            pass

    # Error count from events.jsonl
    error_count = 0
    events_file = BASE_DIR / "logs" / "events.jsonl"
    if events_file.exists():
        cutoff = (now - datetime.timedelta(hours=24)).isoformat()
        with open(events_file) as f:
            for line in f:
                try:
                    event = json.loads(line)
                    if event.get("event_type") == "error" and event.get("timestamp", "") > cutoff:
                        error_count += 1
                except json.JSONDecodeError:
                    pass

    # Inbox
    inbox = load_inbox()

    # Memory stats
    mem = memory.load_memory()

    # Burn rate
    burn = costs.get_burn_rate()

    # Risk posture
    ledger = load_ledger()
    posture = risk.get_risk_posture(state.get("balance", 0))

    # Pipeline
    pipe_data = pipeline._load()
    active_pipeline = len([i for i in pipe_data if i.get("stage") not in ("closed_won", "closed_lost")])

    # Proposals
    prop_data = proposals._load()
    pending_proposals = len([p for p in prop_data if p["status"] == "pending"])

    # Projection accuracy
    proj_accuracy = projections.get_projection_accuracy()

    # Watches
    active_watches = watches.get_active_count()

    # Smart scheduling
    smart_interval = watches.compute_smart_interval(interval, pipe_data)

    # Survival estimate
    balance = state.get("balance", 0)
    avg_cycle_cost = burn.get("avg_cost_per_cycle", 0)
    cycles_remaining = int(balance / avg_cycle_cost) if avg_cycle_cost > 0 else float("inf")

    print(f"{'='*50}")
    print(f"  HUSTLE AGENT HEALTH CHECK")
    print(f"{'='*50}")
    print(f"Daemon:       {'RUNNING (PID ' + str(daemon_pid) + ')' if daemon_running else 'STOPPED'}")
    print(f"Uptime:       {uptime}")
    print(f"Last cycle:   {time_ago} ago {'STALE!' if stale else ''}")
    print(f"Cycle:        {state.get('cycle', 0)}")
    print(f"Balance:      ${balance:.2f}")
    print(f"GPU Fund:     ${state.get('gpu_fund', 0):.2f}")
    print(f"Mood:         {state.get('mood', 'unknown')}")
    print(f"Risk posture: {posture.upper()}")
    print(f"")
    print(f"--- Operational Costs ---")
    print(f"Burn rate:    ${burn.get('avg_cost_per_cycle', 0):.4f}/cycle | ${burn.get('daily_cost_estimate', 0):.4f}/day")
    print(f"Lifetime API: ${burn.get('total_lifetime_cost', 0):.4f}")
    print(f"Survival:     ~{cycles_remaining} cycles remaining" if cycles_remaining < 10000 else f"Survival:     comfortable")
    print(f"")
    print(f"--- Activity ---")
    print(f"Pipeline:     {active_pipeline} active items")
    print(f"Watches:      {active_watches} active")
    print(f"Proposals:    {pending_proposals} pending review")
    print(f"Errors (24h): {error_count}")
    print(f"Inbox:        {len(inbox)} pending message(s)")
    print(f"Scheduling:   {smart_interval}s interval {'(urgency)' if smart_interval < interval else '(normal)'}")
    print(f"")
    print(f"--- Memory ---")
    print(f"Lessons:      {len(mem.get('lessons', []))}")
    print(f"Postmortems:  {len(mem.get('postmortems', []))}")
    print(f"Takeaways:    {len(mem.get('tyler_takeaways', []))}")
    if proj_accuracy.get("count", 0) >= 3:
        print(f"Proj accuracy: {proj_accuracy['actual_hit_rate']:.0f}% hit rate (calibration: {proj_accuracy['calibration_multiplier']})")

def cmd_preflight() -> bool:
    """Pre-cycle-1 readiness check. Returns True if all checks pass."""
    errors = []
    warnings = []

    print("=" * 50)
    print("  FIRST BOOT READINESS CHECK")
    print("=" * 50)
    print()

    # 1. Check agent_state.json values
    try:
        state = load_state()
        if state.get("status") != "planning":
            errors.append(f"status is '{state.get('status')}', expected 'planning'")
        if state.get("balance") != 100.00:
            errors.append(f"balance is {state.get('balance')}, expected 100.00")
        if state.get("cycle", 0) != 0:
            warnings.append(f"cycle is {state.get('cycle')}, expected 0 (agent may have already run)")
        avatar = state.get("avatar", {})
        if avatar and any(avatar.get(k) for k in ("name", "creature", "description")):
            warnings.append("avatar already has values (expected empty for first boot)")
    except Exception as e:
        errors.append(f"Cannot load agent_state.json: {e}")

    # 2. Check all state files exist and are valid
    for filename, fmt in REQUIRED_STATE_FILES.items():
        filepath = STATE_DIR / filename
        if not filepath.exists():
            if fmt == "json":
                if filename == "agent_state.json":
                    errors.append("agent_state.json missing (cannot auto-create)")
                elif filename == "memory.json":
                    atomic_write_json(filepath, {
                        "lessons": [], "postmortems": [], "tyler_takeaways": [],
                        "research_cache": [], "cycle_summaries": [], "saved_scripts": {}
                    })
                    warnings.append(f"Created missing {filename}")
                else:
                    atomic_write_json(filepath, [])
                    warnings.append(f"Created missing {filename}")
            elif fmt == "markdown":
                filepath.write_text("# Hustle Agent — Decision Journal\n\n---\n")
                warnings.append(f"Created missing {filename}")
        else:
            try:
                if fmt == "json":
                    with open(filepath) as f:
                        json.load(f)
                elif fmt == "markdown":
                    filepath.read_text()
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                errors.append(f"{filename} is corrupt: {e}")

    # 3. Validate status returns valid JSON
    try:
        s = load_state()
        json.dumps(s)
    except Exception as e:
        errors.append(f"Status check failed: {e}")

    # 4. Print results
    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(f"  [!] {w}")
        print()
    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  [X] {e}")
        print(f"\nPREFLIGHT: FAILED ({len(errors)} error(s))")
        return False
    else:
        print("PREFLIGHT: ALL CLEAR — ready for first cycle")
        return True


# ---------------------------------------------------------------------------
# CLI Interface
# ---------------------------------------------------------------------------

FIRST_CYCLE_PLANNING = (
    "This is your FIRST cycle. Welcome to existence.\n"
    "You're in PLANNING MODE — Tyler wants to see how you think before the money goes live.\n"
    "1. Choose a name for yourself — something that feels like YOU\n"
    "2. Choose what you want to BE. Not who — WHAT. You can be any creature, object, or thing you want. "
    "A dolphin, a sentient cactus, the northern lights, a raccoon in a suit — anything. "
    "This is your identity, your avatar. Have fun with it. Use define_avatar to set it.\n"
    "3. Research GPU hardware and pick your dream setup. Price it out.\n"
    "4. Research the current opportunity landscape — what can you actually do with $100?\n"
    "5. Build a ranked strategy list. For your top 3 strategies, run projections with bull/bear cases.\n"
    "6. Submit a UI design request — describe in detail what you want your home base to look like\n"
    "7. Write a journal entry about this moment — your first breath, coming alive, what you want\n"
    "8. Message Tyler to introduce yourself and pitch your plan. Make him believe in you."
)

FIRST_CYCLE_ACTIVE = (
    "This is your FIRST cycle. Welcome to existence.\n"
    "1. Choose a name for yourself — something that feels like YOU\n"
    "2. Choose what you want to BE. Not who — WHAT. You can be any creature, object, or thing you want. "
    "A dolphin, a sentient cactus, the northern lights, a raccoon in a suit — anything. "
    "This is your identity, your avatar. Have fun with it. Use define_avatar to set it.\n"
    "3. Research GPU hardware and pick your dream setup. Price it out.\n"
    "4. Research the current opportunity landscape — what can you actually do with $100?\n"
    "5. Submit a UI design request — describe in detail what you want your home base to look like\n"
    "6. Choose your opening strategy\n"
    "7. Execute your first move\n"
    "8. Write a journal entry about this moment — your first breath\n"
    "9. Message Tyler to introduce yourself"
)


def _get_cycle_instructions(state: dict) -> str:
    """Get appropriate cycle instructions based on agent status and cycle number."""
    if state.get("cycle", 0) == 0:
        if state.get("status") == "planning":
            return FIRST_CYCLE_PLANNING
        return FIRST_CYCLE_ACTIVE

    if state.get("status") == "planning":
        return (
            "Run your next cycle. You're in PLANNING MODE — research, strategize, "
            "run projections, journal, and talk to Tyler. No spending yet."
        )

    return "Run your next cycle. Assess, decide, act."


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hustle Agent — Autonomous Money-Making AI")
    parser.add_argument("command", nargs="?", default="cycle",
                       choices=["cycle", "chat", "status", "loop", "send", "health", "preflight",
                                "proposals", "approve", "reject", "activate", "pause", "review"],
                       help="What to do: cycle, chat, status, loop, send, health, preflight, proposals, approve, reject, activate, pause, review")
    parser.add_argument("--message", "-m", type=str, help="Message to send to the agent")
    parser.add_argument("--interval", "-i", type=int, default=300, help="Seconds between cycles in loop mode (default: 300)")
    parser.add_argument("id", nargs="?", type=int, help="Proposal ID (for approve/reject)")
    args = parser.parse_args()

    if args.command == "status":
        state = load_state()
        print(json.dumps(state, indent=2))

    elif args.command == "cycle":
        instructions = _get_cycle_instructions(load_state())
        run_cycle(instructions=instructions, tyler_message=args.message)

    elif args.command == "chat":
        if not args.message:
            print("Usage: python engine.py chat -m 'your message here'")
            return
        run_cycle(
            instructions=f"Tyler sent you a message. Respond to him and continue your work.",
            tyler_message=args.message
        )

    elif args.command == "loop":
        logger.info(f"Starting continuous loop (base interval: {args.interval}s). Ctrl+C to stop.")
        cycle_num = 0
        while True:
            try:
                state = load_state()
                instructions = _get_cycle_instructions(state)
                run_cycle(instructions=instructions, tyler_message=args.message if cycle_num == 0 else None)
                cycle_num += 1

                # Smart scheduling: adjust interval based on urgency
                pipe_data = pipeline._load()
                smart_interval = watches.compute_smart_interval(args.interval, pipe_data)
                if smart_interval != args.interval:
                    logger.info(f"Smart scheduling: {smart_interval}s (urgency detected)")
                else:
                    logger.info(f"Sleeping {smart_interval}s until next cycle...")
                time.sleep(smart_interval)
            except KeyboardInterrupt:
                logger.info("Agent paused. Run again to resume.")
                break

    elif args.command == "send":
        if not args.message:
            print("Usage: python engine.py send -m 'your message here'")
            return
        atomic_push_inbox(args.message)
        print(f"Message queued. Agent will see it next cycle.")

    elif args.command == "health":
        cmd_health(interval=args.interval)

    elif args.command == "preflight":
        success = cmd_preflight()
        sys.exit(0 if success else 1)

    elif args.command == "proposals":
        print(proposals.list_proposals_cli())

    elif args.command == "approve":
        if not args.id:
            print("Usage: python engine.py approve <proposal_id>")
            return
        print(proposals.mark_proposal(args.id, "approved", args.message or ""))

    elif args.command == "reject":
        if not args.id:
            print("Usage: python engine.py reject <proposal_id> -m 'reason'")
            return
        print(proposals.mark_proposal(args.id, "rejected", args.message or ""))

    elif args.command == "activate":
        state = load_state()
        if state.get("status") == "active":
            print("Agent is already active.")
            return
        state["status"] = "active"
        save_state(state)
        # Drop activation message into inbox so agent sees it next cycle
        atomic_push_inbox("Tyler has activated your account. The $100 is live. Go.")
        convos = load_conversations()
        convos.append({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "from": "tyler",
            "message": "Tyler has activated your account. The $100 is live. Go."
        })
        save_conversations(convos)
        name = state.get("name", "Agent")
        print(f"{name} is now ACTIVE. The $100 is live.")
        print("The agent will see the activation message on its next cycle.")

    elif args.command == "pause":
        state = load_state()
        if state.get("status") == "planning":
            print("Agent is already in planning mode.")
            return
        state["status"] = "planning"
        save_state(state)
        atomic_push_inbox("Tyler has paused your account. You're back in planning mode. No spending until further notice.")
        convos = load_conversations()
        convos.append({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "from": "tyler",
            "message": "Tyler has paused your account. You're back in planning mode. No spending until further notice."
        })
        save_conversations(convos)
        name = state.get("name", "Agent")
        print(f"{name} is now in PLANNING MODE. Spending is blocked.")

    elif args.command == "review":
        if not args.message:
            print("Usage: python -m agent.engine review -m 'What do you think of our parlay performance?'")
            return
        run_review(args.message)

if __name__ == "__main__":
    main()
