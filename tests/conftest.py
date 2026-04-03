"""
Hustle Agent Test Suite — Shared Fixtures & Mock Factories

Provides filesystem isolation, Anthropic client mocking, and
helper factories so no real API calls or state mutations occur.
"""

import json
import os
import uuid
import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Set dummy API key BEFORE any agent imports
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

from agent import engine, risk, projections, memory, costs, audit, watches, pipeline, proposals, logger, instincts, kalshi_client


# ---------------------------------------------------------------------------
# Default state matching a fresh agent_state.json
# ---------------------------------------------------------------------------

DEFAULT_STATE = {
    "name": "",
    "balance": 100.00,
    "target": 20000.00,
    "cycle": 0,
    "status": "planning",
    "mood": "",
    "avatar": {
        "name": "",
        "creature": "",
        "description": ""
    },
    "active_strategies": [],
    "total_earned": 0.00,
    "total_spent": 0.00,
    "net_profit": 0.00,
    "roi_percent": 0.0,
    "tylers_cut": 0.00,
    "gpu_fund": 0.00,
    "dream_gpu": {
        "name": "",
        "description": "",
        "estimated_cost": 0.00,
        "why": ""
    },
    "gpu_fund_progress_percent": 0.0,
    "strategies": [],
    "created_at": "",
    "last_updated": ""
}


# ---------------------------------------------------------------------------
# Anthropic API response mock factories
# ---------------------------------------------------------------------------

def make_text_block(text: str):
    """Mimics an Anthropic TextBlock."""
    return SimpleNamespace(type="text", text=text)


def make_tool_use_block(name: str, input_dict: dict, tool_id: str = None):
    """Mimics an Anthropic ToolUseBlock."""
    if tool_id is None:
        tool_id = f"toolu_{name}_{id(input_dict) % 10000}"
    return SimpleNamespace(type="tool_use", name=name, input=input_dict, id=tool_id)


def make_api_response(content_blocks: list, input_tokens: int = 500,
                      output_tokens: int = 200, stop_reason: str = "end_turn"):
    """Mimics an Anthropic Messages API response."""
    return SimpleNamespace(
        content=content_blocks,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------------
# Data factories
# ---------------------------------------------------------------------------

def make_txn(type_: str = "expense", amount: float = 5.0,
             strategy: str = "kalshi", description: str = "test txn",
             reasoning: str = "testing", timestamp: str = None,
             balance_after: float = 95.0):
    """Build a ledger transaction entry."""
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "id": 1,
        "timestamp": timestamp,
        "type": type_,
        "amount": amount,
        "description": description,
        "strategy": strategy,
        "balance_after": balance_after,
        "reasoning": reasoning,
        "tags": []
    }


def make_action(category: str = "kalshi", cost: float = 5.0,
                expected_return: float = 10.0, status: str = "won",
                actual_return: float = 12.0, actual_time_days: float = 3.0,
                confidence: int = 70, time_horizon_days: float = 5.0,
                balance: float = 100.0, risk_posture: str = "aggressive",
                time_of_day: str = "morning", day_of_week: str = "weekday",
                projection_id: str = None):
    """Build an action entry for testing instincts."""
    return {
        "action_id": str(uuid.uuid4())[:8],
        "projection_id": projection_id or str(uuid.uuid4())[:8],
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "category": category,
        "subcategory": "test action",
        "cost": cost,
        "conditions": {
            "time_horizon_days": time_horizon_days,
            "market_odds": None,
            "confidence_at_decision": confidence,
            "capital_percentage": round((cost / balance * 100) if balance > 0 else 0, 1),
            "time_of_day": time_of_day,
            "day_of_week": day_of_week,
            "risk_posture_at_time": risk_posture,
            "balance_at_time": balance,
        },
        "expected_return": expected_return,
        "status": status,
        "actual_return": actual_return if status != "pending" else None,
        "actual_time_days": actual_time_days if status != "pending" else None,
        "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat() if status != "pending" else None,
    }


def make_resolved_projection(hit: bool = True, confidence_raw: int = 70,
                              cost: float = 10.0, expected_return: float = 20.0,
                              actual_return: float = 25.0,
                              time_days: float = 5.0, actual_time: float = 4.0):
    """Build a resolved projection for testing accuracy calculations."""
    expected_profit = expected_return - cost
    actual_profit = actual_return - cost
    return {
        "id": "test1234",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action": "test projection",
        "cost": cost,
        "strategy_type": "kalshi",
        "expected_return": expected_return,
        "expected_profit": round(expected_profit, 2),
        "roi_percent": round((expected_profit / cost * 100) if cost > 0 else 0, 1),
        "time_to_return_days": time_days,
        "confidence_raw": confidence_raw,
        "confidence_calibrated": confidence_raw,
        "calibration_multiplier": 1.0,
        "assumptions": ["test"],
        "risks": ["test"],
        "comparables": "",
        "bull_case": "good",
        "bear_case": "bad",
        "research_summary": "test",
        "operational_overhead": 0.01,
        "capital_velocity_cost": 0.5,
        "verdict": "lean_yes",
        "status": "resolved",
        "resolution": {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "actual_outcome": "test outcome",
            "actual_return": actual_return,
            "actual_profit": round(actual_profit, 2),
            "actual_time_days": actual_time,
            "profit_delta": round(actual_profit - expected_profit, 2),
            "time_delta": round(actual_time - time_days, 1),
            "hit": hit,
        }
    }


# ---------------------------------------------------------------------------
# Filesystem isolation fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_fs(tmp_path, monkeypatch):
    """
    Redirect ALL module path constants to tmp_path so tests never touch
    real state files. Seeds minimal state files for a fresh agent.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    backup_dir = state_dir / "backups"
    backup_dir.mkdir()

    # Seed state files
    (state_dir / "agent_state.json").write_text(json.dumps(DEFAULT_STATE, indent=2))
    (state_dir / "ledger.json").write_text("[]")
    (state_dir / "conversations.json").write_text("[]")
    (state_dir / "inbox.json").write_text("[]")
    (state_dir / "ui_requests.json").write_text("[]")
    (state_dir / "journal.md").write_text("# Hustle Agent — Decision Journal\n\n---\n")
    (state_dir / "actions.json").write_text("[]")

    # Monkeypatch every module's path constants
    modules_with_base = [engine, risk, projections, memory, costs, audit,
                         watches, pipeline, proposals, logger, instincts]
    for mod in modules_with_base:
        monkeypatch.setattr(mod, "BASE_DIR", tmp_path)
        if hasattr(mod, "STATE_DIR"):
            monkeypatch.setattr(mod, "STATE_DIR", state_dir)

    # engine-specific paths
    monkeypatch.setattr(engine, "STATE_FILE", state_dir / "agent_state.json")
    monkeypatch.setattr(engine, "LEDGER_FILE", state_dir / "ledger.json")
    monkeypatch.setattr(engine, "JOURNAL_FILE", state_dir / "journal.md")
    monkeypatch.setattr(engine, "CONVERSATIONS_FILE", state_dir / "conversations.json")
    monkeypatch.setattr(engine, "UI_REQUESTS_FILE", state_dir / "ui_requests.json")
    monkeypatch.setattr(engine, "INBOX_FILE", state_dir / "inbox.json")
    monkeypatch.setattr(engine, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(engine, "TOOLS_DIR", tools_dir)
    monkeypatch.setattr(engine, "CONFIG_DIR", config_dir)

    # memory-specific paths
    monkeypatch.setattr(memory, "MEMORY_FILE", state_dir / "memory.json")
    monkeypatch.setattr(memory, "TOOLS_DIR", tools_dir)

    # costs-specific paths
    monkeypatch.setattr(costs, "COSTS_FILE", state_dir / "api_costs.json")

    # projections-specific paths
    monkeypatch.setattr(projections, "PROJECTIONS_FILE", state_dir / "projections.json")

    # audit-specific paths
    monkeypatch.setattr(audit, "AUDITS_FILE", state_dir / "audits.json")

    # watches-specific paths
    monkeypatch.setattr(watches, "WATCHES_FILE", state_dir / "watches.json")

    # pipeline-specific paths
    monkeypatch.setattr(pipeline, "PIPELINE_FILE", state_dir / "pipeline.json")

    # proposals-specific paths
    monkeypatch.setattr(proposals, "PROPOSALS_FILE", state_dir / "proposals.json")

    # instincts-specific paths
    monkeypatch.setattr(instincts, "ACTIONS_FILE", state_dir / "actions.json")
    monkeypatch.setattr(instincts, "INSTINCTS_FILE", state_dir / "instincts.json")
    monkeypatch.setattr(instincts, "PRIORS_FILE", state_dir / "priors.json")

    # engine actions path
    monkeypatch.setattr(engine, "ACTIONS_FILE", state_dir / "actions.json")

    # audit instincts path
    monkeypatch.setattr(audit, "INSTINCTS_FILE", state_dir / "instincts.json")

    # logger-specific paths
    monkeypatch.setattr(logger, "LOG_DIR", logs_dir)
    monkeypatch.setattr(logger, "EVENTS_FILE", logs_dir / "events.jsonl")

    # kalshi_client paths
    monkeypatch.setattr(kalshi_client, "BASE_DIR", tmp_path)
    monkeypatch.setattr(kalshi_client, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(kalshi_client, "CONFIG_FILE", config_dir / "kalshi.json")
    kalshi_client.reset_clients()

    return tmp_path


@pytest.fixture
def mock_anthropic(monkeypatch):
    """Patch anthropic.Anthropic so no real API calls happen."""
    mock_client = MagicMock()
    mock_constructor = MagicMock(return_value=mock_client)
    monkeypatch.setattr("anthropic.Anthropic", mock_constructor)
    return mock_client


@pytest.fixture
def active_state(isolated_fs):
    """Set up an active agent with balance for spending tests."""
    state = engine.load_state()
    state["status"] = "active"
    state["name"] = "TestBot"
    state["balance"] = 100.00
    state["cycle"] = 5
    state["mood"] = "focused"
    state["dream_gpu"] = {
        "name": "RTX 5090",
        "description": "Dream GPU",
        "estimated_cost": 2000.00,
        "why": "Need compute"
    }
    state["created_at"] = "2026-03-30T00:00:00+00:00"
    engine.save_state(state)
    return state
