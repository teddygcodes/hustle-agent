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

from agent import engine, state, tool_executors, risk, projections, memory, costs, audit, watches, pipeline, proposals, logger, instincts, kalshi_client, reports


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


def make_data_backing(source: str = "National Weather Service API",
                      data_point: str = "Forecast high: 94F, P(>90F) = 72%",
                      source_probability: float = 0.72,
                      market_price: float = 0.35,
                      edge: float = 0.37,
                      edge_direction: str = "market underpriced YES",
                      source_url: str = "https://api.weather.gov/test",
                      retrieved_at: str = None):
    """Build a data_backing dict for testing."""
    if retrieved_at is None:
        retrieved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "source": source,
        "data_point": data_point,
        "source_probability": source_probability,
        "market_price": market_price,
        "edge": edge,
        "edge_direction": edge_direction,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
    }


def make_report(txn_id: int = 1, report_type: str = "investment",
                projection_id: str = None, has_data_backing: bool = False,
                has_resolution: bool = False):
    """Build a transaction report dict for testing."""
    report = {
        "report_id": f"rpt_{txn_id}",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": report_type,
        "summary": {
            "action": "Test transaction",
            "amount": 5.0,
            "outcome": "pending",
            "profit_loss": None,
            "balance_after": 95.0,
        },
        "reasoning": {
            "strategy": "kalshi",
            "thesis": "Test reasoning",
            "confidence_raw": 70,
            "confidence_adjusted": 55,
            "calibration_applied": "kalshi category: 0.79x multiplier",
            "instinct_warnings": [],
            "risk_posture_at_time": "normal",
            "exploration_mode": "exploit",
        },
        "data_backing": make_data_backing() if has_data_backing else None,
        "projection": None,
        "resolution": None,
        "linked_ids": {
            "ledger_id": txn_id,
            "action_id": None,
            "projection_id": projection_id,
            "kalshi_order_id": None,
        },
    }
    if has_resolution:
        report["resolution"] = {
            "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "actual_outcome": "Contract resolved YES",
            "actual_return": 10.0,
            "actual_profit_loss": 5.0,
            "prediction_delta": 2.0,
            "notes": "Time delta: -1.0d",
        }
        report["summary"]["outcome"] = "won"
        report["summary"]["profit_loss"] = 5.0
    return report


# ---------------------------------------------------------------------------
# Auto-isolate decisions audit log so tests never touch the live bot's
# bot/state/decisions.jsonl. Session 6 (Apr 24) added log_decision calls
# on every executor/scanner gate — without this fixture, any test that
# exercises those code paths would write to production state.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_decisions_log(tmp_path_factory, monkeypatch):
    try:
        from bot import decisions as _decisions
    except Exception:
        return
    sandbox = tmp_path_factory.mktemp("decisions_isolation") / "decisions.jsonl"
    monkeypatch.setattr(_decisions, "DECISIONS_FILE", sandbox)


# ---------------------------------------------------------------------------
# Auto-isolate calibration prediction log. Session 11 (Apr 25) added
# record_prediction / update_prediction_close calls inside clv.record_clv_entry,
# record_counterfactual_skip, and check_clv_settlements — without isolation,
# any test exercising clv would write to the live predictions.jsonl.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_predictions_log(tmp_path_factory, monkeypatch):
    try:
        from bot import calibration as _calibration
    except Exception:
        return
    sandbox = tmp_path_factory.mktemp("calibration_isolation") / "predictions.jsonl"
    monkeypatch.setattr(_calibration, "PREDICTIONS_FILE", sandbox)


# ---------------------------------------------------------------------------
# Auto-isolate logger output. Session 15.5 (Apr 25): bot/main.py imports
# call bot/logger.py:setup_file_logging() at module load, which attaches a
# RotatingFileHandler pointing at bot/logs/bot.log. Without this fixture,
# any test that imports bot.main (transitively or directly) causes mocked
# error/traceback noise to land in the production log file. After 24h of
# pytest runs Tyler counted 190 ERROR/Traceback entries from tests, making
# `grep ERROR bot/logs/bot.log` an unreliable health check.
#
# This autouse session-scoped fixture snapshots root + every glint.*
# logger's handlers, replaces them with a NullHandler for the test
# session, and restores them on teardown. Tests that need to assert on
# log output should use pytest's caplog fixture, which still works
# because caplog attaches its own handler.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="session")
def _isolate_glint_loggers():
    import logging
    from logging.handlers import RotatingFileHandler

    # Strip any RotatingFileHandler that bot/logger.setup_file_logging() attached
    # during test collection / module import. Replace ALL handlers on root and
    # glint.* / nexus.* loggers with a single NullHandler.
    saved: dict[str, list[logging.Handler]] = {}
    targets: list[logging.Logger] = [logging.getLogger()]
    for name, lg in list(logging.Logger.manager.loggerDict.items()):
        if not isinstance(lg, logging.Logger):
            continue
        if name == "glint" or name.startswith("glint.") or name == "nexus" or name.startswith("nexus."):
            targets.append(lg)
    for lg in targets:
        saved[lg.name] = list(lg.handlers)
        lg.handlers = [logging.NullHandler()]

    # Also short-circuit setup_file_logging() so any later import that calls
    # it cannot re-attach a RotatingFileHandler. Force _initialized=True; the
    # function early-returns on that guard.
    saved_initialized = None
    try:
        from bot import logger as _bot_logger
        saved_initialized = _bot_logger._initialized
        _bot_logger._initialized = True
        # Also clean any handler that snuck in before this fixture ran but
        # wasn't on the targets list because the logger hadn't been created yet.
        for lg in [logging.getLogger()]:
            for h in list(lg.handlers):
                if isinstance(h, RotatingFileHandler):
                    lg.removeHandler(h)
                    h.close()
    except Exception:
        pass

    try:
        yield
    finally:
        for lg in targets:
            if lg.name in saved:
                lg.handlers = saved[lg.name]
        if saved_initialized is not None:
            try:
                from bot import logger as _bot_logger
                _bot_logger._initialized = saved_initialized
            except Exception:
                pass


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

    # Create reports directory
    reports_dir = state_dir / "reports"
    reports_dir.mkdir()

    # Monkeypatch every module's path constants
    modules_with_base = [engine, state, tool_executors, risk, projections, memory, costs, audit,
                         watches, pipeline, proposals, logger, instincts, reports]
    for mod in modules_with_base:
        monkeypatch.setattr(mod, "BASE_DIR", tmp_path)
        if hasattr(mod, "STATE_DIR"):
            monkeypatch.setattr(mod, "STATE_DIR", state_dir)

    # state module paths (load_state/save_state/etc. read these)
    monkeypatch.setattr(state, "STATE_FILE", state_dir / "agent_state.json")
    monkeypatch.setattr(state, "LEDGER_FILE", state_dir / "ledger.json")
    monkeypatch.setattr(state, "JOURNAL_FILE", state_dir / "journal.md")
    monkeypatch.setattr(state, "CONVERSATIONS_FILE", state_dir / "conversations.json")
    monkeypatch.setattr(state, "UI_REQUESTS_FILE", state_dir / "ui_requests.json")
    monkeypatch.setattr(state, "INBOX_FILE", state_dir / "inbox.json")
    monkeypatch.setattr(state, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(state, "TOOLS_DIR", tools_dir)
    monkeypatch.setattr(state, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(state, "ACTIONS_FILE", state_dir / "actions.json")

    # engine-specific paths (re-exported from state, also patched for direct access)
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

    # reports-specific paths
    monkeypatch.setattr(reports, "REPORTS_DIR", reports_dir)

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
