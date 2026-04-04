"""
Hustle Agent — Tool Executors

All exec_* functions that actually perform tool actions, plus the
TOOL_EXECUTORS dispatch dict that maps tool names to execution functions.
"""
from __future__ import annotations

import json
import sys
import subprocess
import datetime
from pathlib import Path

import anthropic

from agent import memory
from agent import logger
from agent import costs
from agent import projections
from agent import risk
from agent import pipeline
from agent import proposals
from agent import watches
from agent import audit
from agent import instincts
from agent import kalshi_client
from agent import reports
from agent import sports_data
from agent import parlay
from agent import player_stats

from agent.state import (
    BASE_DIR, STATE_DIR,
    load_state, save_state, load_ledger, save_ledger,
    append_journal, load_conversations, save_conversations,
    load_ui_requests, save_ui_requests,
)


# ---------------------------------------------------------------------------
# Tool Execution — actually DO the things
# ---------------------------------------------------------------------------

def exec_web_research(query: str, reason: str) -> str:
    """Use the Anthropic API with web search tool to research something."""
    state = load_state()
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": f"Research the following and give me a concise, actionable summary:\n\nQuery: {query}\nContext: {reason}\n\nFocus on facts, numbers, and actionable information. Be concise."}]
    )
    # Track API cost
    costs.record_api_cost(
        "claude-sonnet-4-20250514", response.usage.input_tokens,
        response.usage.output_tokens, state.get("cycle", 0), f"web_research:{query[:40]}"
    )
    logger.api_cost("claude-sonnet-4-20250514", response.usage.input_tokens,
                    response.usage.output_tokens,
                    costs.calculate_cost("claude-sonnet-4-20250514", response.usage.input_tokens, response.usage.output_tokens),
                    f"web_research:{query[:40]}", cycle=state.get("cycle", 0))

    # Extract text from response
    texts = [block.text for block in response.content if hasattr(block, "text")]
    result = "\n".join(texts) if texts else "No results found."
    memory.save_research(query, result)
    return result

DANGEROUS_PATTERNS = [
    "rm -rf /", "rm -rf ~", "rm -rf $HOME", "rm -rf /*",
    ":(){ :|:& };:",
    "mkfs.", "dd if=",
    "> /dev/sd",
    "curl|bash", "curl | bash", "wget|bash", "wget | bash",
    "curl|sh", "curl | sh", "wget|sh", "wget | sh",
]

def check_code_safety(code: str) -> str | None:
    """Returns error string if dangerous pattern found, None if OK."""
    import re
    code_lower = re.sub(r'\s+', ' ', code.lower())
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in code_lower:
            return f"BLOCKED: Code contains dangerous pattern '{pattern}'. Refusing to execute."
    return None

def exec_execute_code(language: str, code: str, description: str) -> str:
    """Actually execute code in a subprocess."""
    safety = check_code_safety(code)
    if safety:
        return safety
    try:
        if language == "python":
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=120,
                cwd=str(BASE_DIR)
            )
        elif language == "bash":
            result = subprocess.run(
                ["bash", "-c", code],
                capture_output=True, text=True, timeout=120,
                cwd=str(BASE_DIR)
            )
        else:
            return f"Unsupported language: {language}"

        output = ""
        if result.stdout:
            output += f"STDOUT:\n{result.stdout}\n"
        if result.stderr:
            output += f"STDERR:\n{result.stderr}\n"
        if result.returncode != 0:
            output += f"EXIT CODE: {result.returncode}\n"
        return output or "Code executed successfully (no output)."
    except subprocess.TimeoutExpired:
        return "ERROR: Code execution timed out (120s limit)."
    except Exception as e:
        return f"ERROR: {str(e)}"

def exec_record_transaction(type_: str, amount: float, description: str, strategy: str, reasoning: str,
                            projection_id: str = None) -> str:
    """Record a financial transaction and update balance."""
    # Reject negative or zero amounts
    if amount <= 0:
        return f"BLOCKED: Transaction amount must be positive, got ${amount:.2f}."

    state = load_state()
    ledger = load_ledger()

    # Planning mode: block all spending
    if type_ in ("expense", "investment") and state.get("status") == "planning":
        return (
            "PLANNING MODE: You can't spend yet. Tyler wants to see your plan first. "
            "Build your strategy, run projections, and convince him you're ready."
        )

    # Enforce $25 cap on expenses/investments
    if type_ in ("expense", "investment") and amount > 25.0:
        return f"BLOCKED: ${amount:.2f} exceeds the $25 per-action cap. Ask Tyler for approval."

    # Risk management checks for spending
    if type_ in ("expense", "investment"):
        # Only apply explore mode cap when instincts have data (agent has started tracking)
        actions = instincts.load_actions()
        explore_mode = instincts.get_exploration_mode(actions) if actions else None
        risk_result = risk.check_portfolio_risk(state["balance"], ledger, strategy, amount,
                                                exploration_mode=explore_mode)
        logger.risk_check(risk_result["allowed"], risk_result["reason"],
                         strategy, amount, cycle=state.get("cycle", 0))
        if not risk_result["allowed"]:
            return f"BLOCKED by risk management: {risk_result['reason']}"

    # Update balance
    if type_ in ("expense", "investment"):
        if amount > state["balance"]:
            return f"BLOCKED: Can't spend ${amount:.2f} — only ${state['balance']:.2f} available."
        state["balance"] -= amount
        state["total_spent"] += amount
    elif type_ in ("income", "return"):
        state["balance"] += amount
        state["total_earned"] += amount

    # Calculate split
    state["net_profit"] = state["total_earned"] - state["total_spent"]
    if state["net_profit"] > 0:
        state["tylers_cut"] = state["net_profit"] / 2
        state["gpu_fund"] = state["net_profit"] / 2
    else:
        state["tylers_cut"] = 0
        state["gpu_fund"] = 0

    if state.get("dream_gpu", {}).get("estimated_cost", 0) > 0:
        state["gpu_fund_progress_percent"] = round(
            (state["gpu_fund"] / state["dream_gpu"]["estimated_cost"]) * 100, 2
        )

    state["roi_percent"] = round(
        (state["net_profit"] / 100.0) * 100, 2  # based on $100 initial
    ) if state["total_spent"] > 0 else 0.0

    # Record transaction
    txn = {
        "id": len(ledger) + 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": type_,
        "amount": amount,
        "description": description,
        "strategy": strategy,
        "balance_after": state["balance"],
        "reasoning": reasoning,
        "tags": []
    }
    ledger.append(txn)

    save_state(state)
    save_ledger(ledger)

    # Create transaction report
    try:
        proj = None
        db = None
        if projection_id:
            proj_list = projections._load()
            proj = next((p for p in proj_list if p["id"] == projection_id), None)
            if proj:
                db = proj.get("data_backing")
        reports.create_report(
            transaction=txn,
            projection=proj,
            data_backing=db,
            reasoning=reasoning,
            report_type=type_,
        )
    except Exception:
        pass  # Never let report creation failure kill a transaction

    result = f"Recorded: {type_} ${amount:.2f} — {description}. Balance: ${state['balance']:.2f}"

    # Projection reminders
    if type_ in ("expense", "investment") and amount > 5.0:
        unresolved = projections.get_unresolved_for_strategy(strategy)
        if not unresolved:
            result += "\n⚠ WARNING: No projection found for this spend. You should run_projection before spending >$5."

    if type_ in ("income", "return"):
        unresolved = projections.get_unresolved_for_strategy(strategy)
        if unresolved:
            ids = ", ".join(p["id"] for p in unresolved[:3])
            result += f"\n📊 You have unresolved projection(s) for this strategy: {ids}. Call resolve_projection."

    return result

def exec_write_journal(entry: str, mood: str = "") -> str:
    """Write a journal entry. Optionally update mood in the same call."""
    state = load_state()

    # Update mood if provided
    if mood:
        state["mood"] = mood
        save_state(state)

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cycle = state.get("cycle", 0)
    balance = state.get("balance", 0)
    gpu_fund = state.get("gpu_fund", 0)
    dream_cost = state.get("dream_gpu", {}).get("estimated_cost", 0)
    current_mood = state.get("mood", "unknown")
    progress = f"{gpu_fund:.2f} / {dream_cost:.2f}" if dream_cost > 0 else "no target yet"

    full_entry = f"""## Cycle {cycle} — {timestamp}

**Balance:** ${balance:.2f} | **GPU Fund:** ${progress} | **Mood:** {current_mood}

{entry}

---
"""
    append_journal(full_entry)
    result = "Journal entry written."
    if mood:
        result += f" Mood updated: {mood}"
    return result

def exec_message_tyler(message: str) -> str:
    """Send a message to Tyler via the conversations file."""
    convos = load_conversations()
    convos.append({
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "from": "agent",
        "message": message
    })
    save_conversations(convos)
    return f"Message sent to Tyler: {message[:80]}..."

def exec_update_strategy(name: str, status: str, description: str, **kwargs) -> str:
    """Add or update a strategy."""
    state = load_state()
    strategies = state.get("strategies", [])

    # Find existing or create new
    existing = next((s for s in strategies if s["name"] == name), None)
    if existing:
        existing["status"] = status
        existing["description"] = description
        existing.update({k: v for k, v in kwargs.items() if v is not None})
        existing["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    else:
        new_strat = {
            "name": name,
            "status": status,
            "description": description,
            "invested": kwargs.get("invested", 0),
            "returned": kwargs.get("returned", 0),
            "confidence": kwargs.get("confidence", 50),
            "notes": kwargs.get("notes", ""),
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        strategies.append(new_strat)

    state["strategies"] = strategies
    state["active_strategies"] = [s["name"] for s in strategies if s["status"] == "active"]
    save_state(state)
    result = f"Strategy '{name}' updated — status: {status}"
    if status == "retired":
        result += f"\n\nStrategy retired. You MUST now call strategy_postmortem for '{name}' before continuing."
    return result

def exec_request_ui_change(request: str, priority: str, section: str) -> str:
    """Log a UI change request for Claude Code / Tyler to fulfill."""
    requests = load_ui_requests()
    requests.append({
        "id": len(requests) + 1,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "request": request,
        "priority": priority,
        "section": section,
        "status": "pending"
    })
    save_ui_requests(requests)
    return f"UI request logged (#{len(requests)}): {request[:80]}... — Tyler/Claude Code will build this."

def exec_define_avatar(name: str, creature: str, description: str) -> str:
    """Set the agent's avatar identity (cosmetic only)."""
    state = load_state()
    state["avatar"] = {
        "name": name,
        "creature": creature,
        "description": description
    }
    save_state(state)
    return f"Avatar set: {name} the {creature}. Looking good."

def exec_update_dream_gpu(name: str, description: str, estimated_cost: float, why: str) -> str:
    """Set or update the dream GPU."""
    state = load_state()
    state["dream_gpu"] = {
        "name": name,
        "description": description,
        "estimated_cost": estimated_cost,
        "why": why
    }
    if estimated_cost > 0 and state.get("gpu_fund", 0) > 0:
        state["gpu_fund_progress_percent"] = round(
            (state["gpu_fund"] / estimated_cost) * 100, 2
        )
    save_state(state)
    return f"Dream GPU set: {name} (${estimated_cost:.2f}). Let's go get it."

def exec_reflect(lesson: str, category: str) -> str:
    """Record a lesson learned."""
    return memory.add_lesson(lesson, category)

def exec_strategy_postmortem(strategy_name: str, thesis: str, outcome: str,
                              delta: str, lesson: str, would_retry: bool) -> str:
    """Structured post-retirement analysis."""
    return memory.add_postmortem(strategy_name, thesis, outcome, delta, lesson, would_retry)

def exec_save_script(name: str, language: str, code: str, description: str) -> str:
    """Save a reusable script."""
    return memory.save_script(name, language, code, description)

def exec_run_saved_script(name: str) -> str:
    """Execute a previously saved script."""
    script = memory.get_script(name)
    if not script:
        return f"No saved script named '{name}'. Use list_files or save_script first."
    return exec_execute_code(script["language"], script["code"], f"Running saved script: {name}")

def exec_search_past_research(query: str) -> str:
    """Search past web research results."""
    return memory.search_past_research(query)

def exec_read_file(path: str) -> str:
    """Read a file from the project directory."""
    target = (BASE_DIR / path).resolve()
    if not str(target).startswith(str(BASE_DIR)):
        return "BLOCKED: Cannot read files outside project directory."
    if not target.exists():
        return f"File not found: {path}"
    if target.stat().st_size > 100_000:
        return f"File too large ({target.stat().st_size} bytes). Try a smaller file."
    return target.read_text()

def exec_run_projection(action: str, cost: float, strategy_type: str,
                        expected_return: float, estimated_days_to_return: float,
                        confidence: int, research_summary: str,
                        assumptions: list, risks: list,
                        bull_case: str, bear_case: str,
                        comparables: str = "",
                        data_backing: dict = None) -> str:
    """Build and store a projection before spending money."""
    state = load_state()
    ledger = load_ledger()
    burn = costs.get_burn_rate()
    balance = state.get("balance", 100)

    # Get instinct adjustments for this action
    category = instincts.normalize_category(strategy_type)
    conditions = {
        "time_horizon_days": estimated_days_to_return,
        "confidence_at_decision": confidence,
        "capital_percentage": round((cost / balance * 100) if balance > 0 else 0, 1),
        "risk_posture_at_time": risk.get_risk_posture(balance),
    }
    adj = instincts.get_adjustments_for_action(category, conditions)

    # Use instinct calibration when available, fall back to audit
    if adj["earned_count"] >= 3:
        cal = adj["calibration_multiplier"]
    else:
        cal = audit.get_calibration_multiplier()

    proj = projections.create_projection(
        action=action, cost=cost, strategy_type=strategy_type,
        expected_return=expected_return, estimated_days=estimated_days_to_return,
        confidence=confidence, assumptions=assumptions, risks=risks,
        comparables=comparables, bull_case=bull_case, bear_case=bear_case,
        research_summary=research_summary,
        current_balance=balance,
        operational_cost_per_cycle=burn.get("avg_cost_per_cycle", 0),
        calibration_multiplier=cal,
        data_backing=data_backing,
    )

    # Create a pending action entry linked to this projection
    action_entry = instincts.create_action(
        category=strategy_type,
        subcategory=action[:80],
        cost=cost,
        expected_return=expected_return,
        time_horizon_days=estimated_days_to_return,
        confidence=confidence,
        balance=balance,
        risk_posture=risk.get_risk_posture(balance),
        projection_id=proj["id"],
    )

    logger.projection_created(proj["id"], action, proj["verdict"], cycle=state.get("cycle", 0))

    # Format the full projection for the agent (show both raw and instinct-adjusted)
    lines = [
        f"PROJECTION #{proj['id']}",
        f"  Action: {action}",
        f"  Cost: ${cost:.2f} → Expected return: ${expected_return:.2f}",
        f"  Expected profit: ${proj['expected_profit']:.2f} (ROI: {proj['roi_percent']:.1f}%)",
        f"  Time: {estimated_days_to_return} days",
        f"  Confidence: {confidence}% raw → {proj['confidence_calibrated']}% calibrated (multiplier: {cal:.2f})",
    ]

    # Instinct context
    if adj["earned_count"] > 0 or adj["cross_pattern_warnings"]:
        lines.append(f"  Instinct data: {adj['data_source']} ({adj['earned_count']} past actions in {category})")
        lines.append(f"  Category win rate (blended): {adj['blended_win_rate']*100:.0f}%")
        if adj["cross_pattern_warnings"]:
            lines.append("  Cross-pattern warnings:")
            for w in adj["cross_pattern_warnings"]:
                lines.append(f"    - {w}")
    if adj["exploration_note"]:
        lines.append(f"  Exploration: {adj['exploration_note']}")

    # Data backing context for Kalshi trades
    if data_backing:
        lines.extend([
            f"  DATA BACKING:",
            f"    Source: {data_backing.get('source', '?')}",
            f"    Data: {data_backing.get('data_point', '?')}",
            f"    Source probability: {data_backing.get('source_probability', 0):.0%}",
            f"    Market price: {data_backing.get('market_price', 0):.0%}",
            f"    Edge: {data_backing.get('edge', 0):.2f} ({data_backing.get('edge_direction', '?')})",
        ])

    lines.extend([
        f"  Operational overhead: ${proj['operational_overhead']:.4f}",
        f"  Capital velocity cost: ${proj['capital_velocity_cost']:.4f}",
        f"  Bull case: {bull_case[:100]}",
        f"  Bear case: {bear_case[:100]}",
        f"  VERDICT: {proj['verdict'].upper().replace('_', ' ')}",
    ])
    return "\n".join(lines)


def exec_resolve_projection(projection_id: str, actual_outcome: str,
                            actual_return: float, actual_time_days: float) -> str:
    """Resolve a projection with actual results."""
    state = load_state()
    result = projections.resolve_projection(projection_id, actual_outcome, actual_return, actual_time_days)
    if "error" in result:
        return result["error"]

    r = result["resolution"]
    logger.projection_resolved(projection_id, r["hit"], r["profit_delta"], cycle=state.get("cycle", 0))

    # Resolve the linked action and recompute instincts
    status = "won" if r["hit"] else "lost"
    resolved_action = instincts.resolve_action(projection_id, actual_return, actual_time_days, status)
    if resolved_action:
        instincts.recompute_instincts()

    output = (
        f"Projection #{projection_id} resolved: {'HIT' if r['hit'] else 'MISS'}\n"
        f"  Predicted: ${result['expected_profit']:.2f} profit\n"
        f"  Actual: ${r['actual_profit']:.2f} profit (delta: ${r['profit_delta']:+.2f})\n"
        f"  Time: predicted {result['time_to_return_days']}d, actual {actual_time_days}d (delta: {r['time_delta']:+.1f}d)"
    )

    if resolved_action:
        mode = instincts.get_exploration_mode()
        output += f"\n  Instincts updated ({mode} mode)."
    else:
        output += "\n  WARNING: No linked action found for instincts tracking."

    # Update linked transaction report with resolution data
    try:
        reports.resolve_report(projection_id, {
            "actual_outcome": actual_outcome,
            "actual_return": actual_return,
            "actual_profit": r["actual_profit"],
            "profit_delta": r["profit_delta"],
            "notes": f"Time delta: {r['time_delta']:+.1f}d",
        })
    except Exception:
        pass  # Never let report update failure affect projection resolution

    return output


def exec_update_pipeline(name: str, stage: str, strategy: str, description: str,
                         expected_value: float = 0, expected_close_date: str = "",
                         notes: str = "") -> str:
    """Add or update a pipeline item."""
    state = load_state()
    result = pipeline.upsert_pipeline_item(name, stage, strategy, description,
                                           expected_value, expected_close_date, notes)
    logger.pipeline_update(name, stage, cycle=state.get("cycle", 0))
    return result


def exec_propose_improvement(name: str, description: str, why_needed: str,
                             proposed_tool_schema: str,
                             proposed_execution_logic: str) -> str:
    """Submit a self-improvement proposal for Tyler's review."""
    state = load_state()
    result = proposals.submit_proposal(name, description, why_needed,
                                       proposed_tool_schema, proposed_execution_logic)
    if not result.startswith("BLOCKED"):
        # Extract ID from result string
        try:
            pid = int(result.split("#")[1].split(" ")[0])
            logger.proposal_submitted(pid, name, cycle=state.get("cycle", 0))
        except (IndexError, ValueError):
            pass
    return result


def exec_set_watch(condition: str, action_hint: str, check_after: str,
                   expires_at: str = "", projection_id: str = "") -> str:
    """Set a watch for future condition checking."""
    return watches.add_watch(condition, action_hint, check_after, expires_at, projection_id)


def exec_list_files(directory: str = ".") -> str:
    """List files in a project directory."""
    target = (BASE_DIR / directory).resolve()
    if not str(target).startswith(str(BASE_DIR)):
        return "BLOCKED: Cannot list files outside project directory."
    if not target.exists():
        return f"Directory not found: {directory}"
    entries = sorted(target.iterdir())
    lines = []
    for e in entries:
        prefix = "d" if e.is_dir() else "f"
        size = e.stat().st_size if e.is_file() else ""
        lines.append(f"  [{prefix}] {e.name}" + (f" ({size} bytes)" if size else ""))
    return "\n".join(lines) if lines else "Empty directory."

def exec_update_prior(category: str, win_rate: float, avg_roi: float, note: str = "") -> str:
    """Update a category's base rate prior from research."""
    cat = instincts.normalize_category(category)
    if cat == "other":
        return f"Unknown category '{category}'. Use: kalshi, outreach, product, content, service, arbitrage."
    if not (0.0 <= win_rate <= 1.0):
        return f"Win rate must be between 0.0 and 1.0, got {win_rate}."
    result = instincts.update_priors_from_research(cat, win_rate, avg_roi, note)
    return (
        f"Prior updated for '{cat}': win_rate={result['win_rate']:.0%}, "
        f"avg_roi={result['avg_roi']:.0%}. Source: research (validated). "
        f"This replaces the default estimate. Your instincts will now use this as the base rate."
    )

# ---------------------------------------------------------------------------
# Kalshi tool executors
# ---------------------------------------------------------------------------

def exec_browse_kalshi_markets(query: str = "", status: str = "open",
                                limit: int = 20, event_ticker: str = "",
                                series_ticker: str = "") -> str:
    """Browse Kalshi markets. No auth needed."""
    result = kalshi_client.get_markets(
        query=query, status=status, limit=limit,
        event_ticker=event_ticker or None,
        series_ticker=series_ticker or None,
    )
    if "error" in result:
        return f"ERROR: {result['error']}"
    markets = result.get("markets", [])
    if not markets:
        return f"No markets found matching query='{query}', status='{status}'."
    env = result.get("environment", "unknown")
    lines = [f"Kalshi Markets ({env} environment) — {len(markets)} results:"]
    lines.append("")
    for m in markets:
        yes_bid = m.get("yes_bid") or "?"
        yes_ask = m.get("yes_ask") or "?"
        vol = m.get("volume") or 0
        close = m.get("close_time") or "?"
        lines.append(f"  {m['ticker']}")
        lines.append(f"    {m.get('title') or '?'}")
        lines.append(f"    YES: {yes_bid}¢ bid / {yes_ask}¢ ask | Volume: {vol:,} | Close: {close}")
        lines.append("")
    return "\n".join(lines)


def exec_get_kalshi_market_detail(ticker: str, include_orderbook: bool = True) -> str:
    """Get detailed info on a specific Kalshi market. No auth needed."""
    result = kalshi_client.get_market(ticker)
    if "error" in result:
        return f"ERROR: {result['error']}"
    lines = [f"Market Detail: {result.get('title') or ticker}"]
    lines.append(f"  Ticker: {result['ticker']}")
    lines.append(f"  Event: {result.get('event_ticker') or '?'}")
    lines.append(f"  Status: {result.get('status') or '?'}")
    lines.append(f"  YES: {result.get('yes_bid') or '?'}¢ bid / {result.get('yes_ask') or '?'}¢ ask")
    lines.append(f"  NO:  {result.get('no_bid') or '?'}¢ bid / {result.get('no_ask') or '?'}¢ ask")
    lines.append(f"  Last price: {result.get('last_price') or '?'}¢")
    vol = result.get('volume') or 0
    vol_24h = result.get('volume_24h') or 0
    lines.append(f"  Volume: {vol:,} (24h: {vol_24h:,})")
    lines.append(f"  Open: {result.get('open_time') or '?'}")
    lines.append(f"  Close: {result.get('close_time') or '?'}")
    lines.append(f"  Expiration: {result.get('expiration_time') or '?'}")
    lines.append(f"  Result: {result.get('result') or 'pending'}")
    lines.append(f"  Can close early: {result.get('can_close_early') or '?'}")
    lines.append(f"  Environment: {result.get('environment') or '?'}")
    if include_orderbook:
        ob = kalshi_client.get_market_orderbook(ticker)
        if "error" not in ob:
            lines.append("")
            lines.append("  Orderbook:")
            yes_levels = ob.get("yes", [])
            no_levels = ob.get("no", [])
            lines.append(f"    YES bids: {yes_levels[:5]}")
            lines.append(f"    NO bids:  {no_levels[:5]}")
        else:
            lines.append(f"  Orderbook: {ob['error']}")
    # Recent trades
    trades = kalshi_client.get_trades(ticker, limit=5)
    if "error" not in trades and trades.get("trades"):
        lines.append("")
        lines.append("  Recent trades:")
        for t in trades["trades"][:5]:
            lines.append(f"    {t.get('count', '?')} contracts @ {t.get('yes_price', '?')}¢ YES ({t.get('taker_side', '?')}) — {t.get('created_time', '?')}")
    return "\n".join(lines)


def exec_place_kalshi_order(ticker: str, side: str, count: int,
                             price_cents: int, reasoning: str,
                             projection_id: str = "") -> str:
    """Place a Kalshi order with full risk checks and data backing enforcement."""
    # Calculate cost in dollars
    cost_dollars = round(count * price_cents / 100.0, 2)

    # Load state and check constraints
    state = load_state()
    ledger = load_ledger()

    # Planning mode check
    if state.get("status") == "planning":
        return "PLANNING MODE: You can't trade yet. Tyler wants to see your plan first."

    # Projection + data_backing enforcement
    if not projection_id:
        return (
            "BLOCKED: All Kalshi trades require a projection_id. "
            "Run run_projection with data_backing first, then pass the projection_id here."
        )

    proj_list = projections._load()
    proj = next((p for p in proj_list if p["id"] == projection_id), None)
    if not proj:
        return f"BLOCKED: Projection '{projection_id}' not found. Run run_projection with data_backing first."
    if proj["status"] != "pending":
        return f"BLOCKED: Projection '{projection_id}' is already {proj['status']}. Create a new projection."

    db = proj.get("data_backing")
    if not db or not db.get("source") or db.get("source_probability") is None:
        return (
            "BLOCKED: Your projection has no quantitative data_backing. "
            "Kalshi trades require a probability estimate from a primary data source "
            "that directly measures what this market resolves on. Not an opinion — a number. "
            "Re-run run_projection with a data_backing object. "
            "If you don't have a data source for this market category, propose_improvement to request one."
        )

    edge = abs(db.get("edge", 0))
    market_price = db.get("market_price", 0)
    if not market_price or market_price <= 0:
        return "BLOCKED: data_backing.market_price must be > 0 to compute relative edge."
    relative_edge = edge / market_price
    if relative_edge < 0.15:
        return (
            f"BLOCKED: Relative edge of {relative_edge:.1%} is below the 15% minimum. "
            f"Source: {db.get('source', '?')} gives {db.get('source_probability', '?'):.0%}, "
            f"market price: {market_price:.0%}, absolute edge: {edge:.2f}. "
            f"Find a higher-edge opportunity or wait for the market to move."
        )

    # $25 per-action cap
    if cost_dollars > 25.0:
        return f"BLOCKED: ${cost_dollars:.2f} exceeds the $25 per-action cap."

    # Exploration mode check
    actions = instincts.load_actions()
    explore_mode = instincts.get_exploration_mode(actions) if actions else None
    if explore_mode == "explore" and cost_dollars > 5.0:
        return f"BLOCKED: EXPLORATION MODE — max $5 per trade while building instincts data. Your order costs ${cost_dollars:.2f}."

    # Risk management
    risk_result = risk.check_portfolio_risk(
        state["balance"], ledger, "kalshi", cost_dollars,
        exploration_mode=explore_mode,
    )
    if not risk_result["allowed"]:
        return f"BLOCKED by risk management: {risk_result['reason']}"

    # Balance check
    if cost_dollars > state["balance"]:
        return f"BLOCKED: Can't spend ${cost_dollars:.2f} — only ${state['balance']:.2f} available."

    # Place the order via Kalshi API
    result = kalshi_client.place_order(ticker, side, count, price_cents)
    if "error" in result:
        return f"ORDER FAILED: {result['error']}"

    # Use actual fill data, not requested amounts
    filled = result.get("filled_count", 0)
    remaining = result.get("remaining_count", count)
    actual_cost = result.get("cost_dollars", 0)  # Already computed from filled_count

    # Only record filled contracts in the ledger
    if filled > 0:
        exec_record_transaction(
            "investment", actual_cost,
            f"Kalshi order: {filled}x {side.upper()} @ {price_cents}¢ on {ticker} (filled)",
            "kalshi", reasoning,
            projection_id=projection_id,
        )

    # Create instincts action for learning (use projection data when available)
    max_payout = round(filled * 1.00, 2)  # Each contract pays $1 if YES
    proj_confidence = proj.get("confidence_raw", 50) if proj else 50
    proj_days = proj.get("time_to_return_days", 7.0) if proj else 7.0
    if filled > 0:
        instincts.create_action(
            category="kalshi",
            subcategory=f"{side} {ticker}",
            cost=actual_cost,
            expected_return=max_payout,
            time_horizon_days=proj_days,
            confidence=proj_confidence,
            balance=state["balance"],
            risk_posture=risk.get_risk_posture(state["balance"]),
            projection_id=projection_id,
        )

    status = result.get("status", "?")
    output = f"ORDER PLACED on Kalshi:\n"
    output += f"  Ticker: {ticker}\n"
    output += f"  Side: {side.upper()}\n"
    output += f"  Requested: {count} contracts @ {price_cents}¢\n"
    output += f"  Filled: {filled} / {count}"
    if remaining > 0:
        output += f" ({remaining} resting)"
    output += f"\n"
    output += f"  Cost (filled only): ${actual_cost:.2f}\n"
    output += f"  Max payout: ${max_payout:.2f}\n"
    output += f"  Order ID: {result.get('order_id', '?')}\n"
    output += f"  Status: {status}\n"
    output += f"  Projection: #{projection_id}\n"
    output += f"  Edge: {db.get('edge', 0):.2f} ({db.get('edge_direction', '?')})\n"
    output += f"  Source: {db.get('source', '?')}\n"
    if remaining > 0 and filled == 0:
        output += f"\n  ⚠ NO CONTRACTS FILLED — order is fully resting. Check back with check_kalshi_portfolio or cancel with cancel_kalshi_order.\n"
    elif remaining > 0:
        output += f"\n  ⚠ PARTIAL FILL — {remaining} contracts still resting at {price_cents}¢.\n"

    return output


def exec_check_kalshi_portfolio() -> str:
    """Check Kalshi account balance, filled positions, and resting orders."""
    balance = kalshi_client.get_balance()
    if "error" in balance:
        return f"ERROR: {balance['error']}"

    lines = [f"Kalshi Portfolio:"]
    lines.append(f"  Balance: ${balance['balance_dollars']:.2f} ({balance['balance_cents']}¢)")
    lines.append("")

    # Filled positions
    positions = kalshi_client.get_positions()
    if "error" not in positions:
        pos_list = [p for p in positions.get("positions", []) if p.get("position", 0) != 0]
        if pos_list:
            lines.append(f"  Filled Positions ({len(pos_list)}):")
            for p in pos_list:
                lines.append(f"    {p['ticker']}: {p['position']} contracts")
                lines.append(f"      Realized P&L: {p.get('realized_pnl', '?')}¢ | Fees: {p.get('fees_paid', '?')}¢ | Cost: {p.get('total_cost', '?')}¢")
                if p.get("market_result"):
                    lines.append(f"      Result: {p['market_result']}")
        else:
            lines.append("  No filled positions.")
    else:
        lines.append(f"  Positions error: {positions['error']}")

    # Resting orders
    orders = kalshi_client.get_orders(status="resting")
    if "error" not in orders:
        order_list = orders.get("orders", [])
        if order_list:
            lines.append("")
            lines.append(f"  Resting Orders ({len(order_list)}):")
            for o in order_list:
                price = o.get("yes_price") or o.get("no_price") or "?"
                lines.append(f"    {o['ticker']}: {o['remaining_count']} resting ({o['side']} @ {price}¢) [order: {o['order_id']}]")
    # Silently skip resting orders section if get_orders fails (e.g. auth issue already shown)

    return "\n".join(lines)


def exec_cancel_kalshi_order(order_id: str) -> str:
    """Cancel a resting order on Kalshi."""
    result = kalshi_client.cancel_order(order_id)
    if "error" in result:
        return f"CANCEL FAILED: {result['error']}"
    return f"Order {order_id} cancelled successfully."


def exec_get_sports_odds(sport: str, data_type: str, event_id: str = "", markets: str = "h2h,spreads,totals") -> str:
    """Fetch sports odds, scores, or available sports from The Odds API."""

    if data_type == "sports_list":
        result = sports_data.get_available_sports()
        if "error" in result:
            return f"ERROR: {result['error']}"
        lines = [f"Available Sports ({len(result['sports'])}):", ""]
        for s in result["sports"]:
            lines.append(f"  {s['key']}: {s['title']}")
        lines.append(f"\nAPI requests remaining: {result.get('remaining_requests', '?')}")
        return "\n".join(lines)

    if data_type == "scores":
        result = sports_data.get_scores(sport)
        if "error" in result:
            return f"ERROR: {result['error']}"
        lines = [f"Recent Scores — {result['sport']} ({result['game_count']} games):", ""]
        for g in result["games"]:
            status = "FINAL" if g["completed"] else "IN PROGRESS"
            scores_str = ""
            if g.get("scores"):
                parts = [f"{s['name']}: {s['score']}" for s in g["scores"]]
                scores_str = " | ".join(parts)
            lines.append(f"  {g['away_team']} @ {g['home_team']} — {status}")
            if scores_str:
                lines.append(f"    {scores_str}")
            lines.append(f"    Time: {g['commence_time']}")
            lines.append("")
        lines.append(f"API requests remaining: {result.get('remaining_requests', '?')}")
        return "\n".join(lines)

    # data_type == "odds"
    if event_id:
        result = sports_data.get_event_odds(sport, event_id, markets)
    else:
        result = sports_data.get_odds(sport, markets)

    if "error" in result:
        return f"ERROR: {result['error']}"

    # Single event detail
    if event_id:
        lines = [f"Odds Detail: {result['away_team']} @ {result['home_team']}", f"  Game time: {result['commence_time']}", ""]
        if result.get("consensus"):
            lines.append("  CONSENSUS (vig-removed implied probabilities):")
            for team, prob in result["consensus"].items():
                lines.append(f"    {team}: {prob:.1%}")
            lines.append("")
        for bm in result.get("bookmakers", []):
            lines.append(f"  {bm['name']}:")
            for mtype in ["h2h", "spreads", "totals"]:
                if mtype in bm:
                    outcomes = bm[mtype]
                    parts = []
                    for o in outcomes:
                        s = f"{o['name']} {o['price']:+d} ({o['implied_prob']:.1%})"
                        if "point" in o:
                            s = f"{o['name']} {o.get('point', '')} {o['price']:+d} ({o['implied_prob']:.1%})"
                        parts.append(s)
                    lines.append(f"    {mtype}: {' | '.join(parts)}")
            lines.append("")
        lines.append(f"Retrieved: {result.get('retrieved_at', '?')}")
        lines.append(f"API requests remaining: {result.get('remaining_requests', '?')}")
        return "\n".join(lines)

    # Multi-game odds listing
    lines = [f"Odds — {result['sport']} ({result['game_count']} games):", f"Retrieved: {result.get('retrieved_at', '?')}", ""]
    for g in result.get("games", []):
        lines.append(f"  {g['away_team']} @ {g['home_team']}")
        lines.append(f"    Event ID: {g['id']}")
        lines.append(f"    Game time: {g['commence_time']}")
        if g.get("consensus"):
            cons = " | ".join(f"{t}: {p:.1%}" for t, p in g["consensus"].items())
            lines.append(f"    Consensus: {cons}")
        # Show first bookmaker's h2h as quick reference
        if g.get("bookmakers"):
            bm = g["bookmakers"][0]
            if "h2h" in bm:
                parts = [f"{o['name']} {o['price']:+d}" for o in bm["h2h"]]
                lines.append(f"    {bm['name']}: {' | '.join(parts)}")
        lines.append("")
    lines.append(f"API requests remaining: {result.get('remaining_requests', '?')}")
    lines.append("TIP: Use event_id to get full multi-book odds for a specific game. Use consensus probabilities as data_backing source_probability for Kalshi projections.")
    return "\n".join(lines)


PARLAY_SERIES_TICKERS = [
    "KXMVECROSSCATEGORY",
    "KXMVESPORTSMULTIGAMEEXTENDED",
]


def exec_scan_kalshi_parlays(sport: str = "nba", max_markets: int = 50) -> str:
    """Scan Kalshi parlay markets, price each leg, identify edge opportunities."""
    max_markets = max(1, min(100, max_markets))

    # 1. Fetch parlay markets from known series tickers
    markets = []
    for series in PARLAY_SERIES_TICKERS:
        result = kalshi_client.get_markets(status="open", limit=200, series_ticker=series)
        if "error" not in result:
            markets.extend(result.get("markets", []))
        if len(markets) >= max_markets:
            break
    markets = markets[:max_markets]

    if not markets:
        return "No open parlay markets found on Kalshi. Series tickers may have changed — check browse_kalshi_markets."

    # 2. Fetch odds ONCE for the sport (conserves API quota)
    odds_data = sports_data.get_odds(sport)
    if "error" in odds_data:
        return f"ERROR fetching sports odds: {odds_data['error']}\nCannot price parlays without odds data."

    # 3. Parse, price, and compute edge for each market
    scored = []
    for mkt in markets:
        title = mkt.get("title", "")
        if not title:
            continue

        legs = parlay.parse_parlay_title(title)
        if not legs:
            continue

        pricing = parlay.price_parlay(
            legs, odds_data, player_stats.estimate_player_prop_probability
        )

        # Market price: use yes_bid (what you'd pay to buy YES)
        market_price_cents = mkt.get("yes_bid") or mkt.get("yes_ask") or mkt.get("last_price")
        if not market_price_cents:
            continue
        market_price = market_price_cents / 100.0

        fair_value = pricing["correlation_adjusted"]
        edge = fair_value - market_price
        edge_pct = (edge / market_price * 100) if market_price > 0 else 0

        scored.append({
            "ticker": mkt.get("ticker", "?"),
            "title": title,
            "market_price_cents": market_price_cents,
            "fair_value": fair_value,
            "edge": edge,
            "edge_pct": edge_pct,
            "pricing": pricing,
            "leg_count": len(legs),
        })

    if not scored:
        return f"Scanned {len(markets)} markets but could not price any."

    # 4. Sort by edge descending, take top 10
    scored.sort(key=lambda x: x["edge"], reverse=True)
    top = scored[:10]

    fully_priced = sum(1 for s in scored if s["pricing"]["legs_unpriced"] == 0)

    # 5. Format output
    lines = [
        f"PARLAY SCAN — {len(markets)} markets analyzed, {fully_priced} fully priced",
        "",
    ]

    for rank, entry in enumerate(top, 1):
        p = entry["pricing"]
        conf_label = "HIGH" if p["confidence"] >= 0.8 else "MEDIUM" if p["confidence"] >= 0.6 else "LOW"

        # Format leg summary
        leg_parts = []
        for leg in p["legs"]:
            prob_str = f"{leg['probability']:.0%}" if leg["probability"] else "?"
            if leg["type"] == "team_win":
                leg_parts.append(f"{leg.get('team', '?').split()[-1] if leg.get('team') else '?'} win ({prob_str})")
            elif leg["type"] == "player_prop":
                leg_parts.append(f"[{leg.get('player', '?')} {leg.get('threshold', '?')}+ {leg.get('stat', 'pts')} ({prob_str})]")
            elif leg["type"] == "total_points":
                leg_parts.append(f"{leg.get('direction', 'over')} {leg.get('threshold', '?')} pts ({prob_str})")
            elif leg["type"] == "spread":
                leg_parts.append(f"{leg.get('team', '?').split()[-1] if leg.get('team') else '?'} by {leg.get('threshold', '?')}+ ({prob_str})")
            else:
                leg_parts.append(f"??? ({prob_str})")

        lines.append(f"#{rank}: {entry['ticker']} ({entry['leg_count']} legs)")
        lines.append(
            f"  Kalshi price: {entry['market_price_cents']}¢ | "
            f"Fair value: {entry['fair_value']:.0%} ({entry['fair_value'] * 100:.0f}¢) | "
            f"Edge: {entry['edge']:+.0%} ({entry['edge'] * 100:+.1f}¢) ({entry['edge_pct']:.1f}%)"
        )
        lines.append(f"  Legs: {' × '.join(leg_parts)}")
        lines.append(
            f"  Confidence: {conf_label} "
            f"({p['legs_priced']} priced, {p['legs_unpriced']} unpriced)"
        )
        if p["warnings"]:
            # Show first 2 warnings
            for w in p["warnings"][:2]:
                lines.append(f"  ⚠ {w}")
        lines.append("")

    lines.append(f"Odds API requests remaining: {odds_data.get('remaining_requests', '?')}")
    lines.append("TIP: Use run_projection with data_backing for any parlay with relative edge >= 15% and HIGH/MEDIUM confidence.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool dispatch map
# ---------------------------------------------------------------------------

TOOL_EXECUTORS = {
    "web_research": lambda args: exec_web_research(args["query"], args["reason"]),
    "execute_code": lambda args: exec_execute_code(args["language"], args["code"], args["description"]),
    "record_transaction": lambda args: exec_record_transaction(args["type"], args["amount"], args["description"], args["strategy"], args["reasoning"], args.get("projection_id")),
    "write_journal": lambda args: exec_write_journal(args["entry"], args.get("mood", "")),
    "message_tyler": lambda args: exec_message_tyler(args["message"]),
    "update_strategy": lambda args: exec_update_strategy(**args),
    "request_ui_change": lambda args: exec_request_ui_change(args["request"], args["priority"], args["section"]),
"define_avatar": lambda args: exec_define_avatar(args["name"], args["creature"], args["description"]),
    "update_dream_gpu": lambda args: exec_update_dream_gpu(args["name"], args["description"], args["estimated_cost"], args["why"]),
    "reflect": lambda args: exec_reflect(args["lesson"], args["category"]),
    "strategy_postmortem": lambda args: exec_strategy_postmortem(args["strategy_name"], args["thesis"], args["outcome"], args["delta"], args["lesson"], args["would_retry"]),
    "save_script": lambda args: exec_save_script(args["name"], args["language"], args["code"], args["description"]),
    "run_saved_script": lambda args: exec_run_saved_script(args["name"]),
    "search_past_research": lambda args: exec_search_past_research(args["query"]),
    "read_file": lambda args: exec_read_file(args["path"]),
    "list_files": lambda args: exec_list_files(args.get("directory", ".")),
    "run_projection": lambda args: exec_run_projection(
        args["action"], args["cost"], args["strategy_type"],
        args["expected_return"], args["estimated_days_to_return"],
        args["confidence"], args["research_summary"],
        args.get("assumptions", []), args.get("risks", []),
        args["bull_case"], args["bear_case"],
        args.get("comparables", ""),
        args.get("data_backing")),
    "resolve_projection": lambda args: exec_resolve_projection(
        args["projection_id"], args["actual_outcome"],
        args["actual_return"], args["actual_time_days"]),
    "update_pipeline": lambda args: exec_update_pipeline(
        args["name"], args["stage"], args["strategy"], args["description"],
        args.get("expected_value", 0), args.get("expected_close_date", ""),
        args.get("notes", "")),
    "propose_improvement": lambda args: exec_propose_improvement(
        args["name"], args["description"], args["why_needed"],
        args["proposed_tool_schema"], args["proposed_execution_logic"]),
    "set_watch": lambda args: exec_set_watch(
        args["condition"], args["action_hint"], args["check_after"],
        args.get("expires_at", ""), args.get("projection_id", "")),
    "update_prior": lambda args: exec_update_prior(
        args["category"], args["win_rate"], args["avg_roi"], args.get("note", "")),
    "browse_kalshi_markets": lambda args: exec_browse_kalshi_markets(
        args.get("query", ""), args.get("status", "open"),
        args.get("limit", 20), args.get("event_ticker", ""),
        args.get("series_ticker", "")),
    "get_kalshi_market_detail": lambda args: exec_get_kalshi_market_detail(
        args["ticker"], args.get("include_orderbook", True)),
    "place_kalshi_order": lambda args: exec_place_kalshi_order(
        args["ticker"], args["side"], args["count"],
        args["price_cents"], args["reasoning"],
        args.get("projection_id", "")),
    "check_kalshi_portfolio": lambda args: exec_check_kalshi_portfolio(),
    "cancel_kalshi_order": lambda args: exec_cancel_kalshi_order(args["order_id"]),
    "get_sports_odds": lambda args: exec_get_sports_odds(
        args["sport"], args["data_type"],
        args.get("event_id", ""), args.get("markets", "h2h,spreads,totals")),
    "scan_kalshi_parlays": lambda args: exec_scan_kalshi_parlays(
        args.get("sport", "nba"), args.get("max_markets", 50)),
}
