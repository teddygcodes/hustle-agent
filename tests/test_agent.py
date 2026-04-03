"""
Hustle Agent — Comprehensive Test Suite

Mocks the Anthropic client so no real API calls are made.
Tests every tool executor, state manager, risk control, and CLI command.
Covers state management, financial controls, risk limits, and tool executors.
"""

import json
import os
import sys
import datetime
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from types import SimpleNamespace

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

from agent import engine, risk, projections, memory, costs, audit, watches, pipeline, proposals, logger
from tests.conftest import (
    make_text_block, make_tool_use_block, make_api_response,
    make_txn, make_resolved_projection, DEFAULT_STATE,
)


# ===================================================================
# CLASS 1: State Management
# ===================================================================

class TestStateManagement:

    def test_atomic_write_json_creates_file(self, isolated_fs):
        path = isolated_fs / "test_output.json"
        engine.atomic_write_json(path, {"key": "value"})
        assert path.exists()
        assert json.loads(path.read_text()) == {"key": "value"}
        assert not path.with_suffix(".tmp").exists()

    def test_atomic_write_json_overwrites(self, isolated_fs):
        path = isolated_fs / "test_output.json"
        engine.atomic_write_json(path, {"v": 1})
        engine.atomic_write_json(path, {"v": 2})
        assert json.loads(path.read_text()) == {"v": 2}

    def test_atomic_write_crash_safety(self, isolated_fs):
        path = isolated_fs / "state" / "agent_state.json"
        original = json.loads(path.read_text())
        tmp_path = path.with_suffix(".tmp")
        # Simulate crash: write tmp but fail on rename
        with patch.object(Path, "rename", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                engine.atomic_write_json(path, {"corrupted": True})
        # Original should be unchanged
        assert json.loads(path.read_text()) == original

    def test_backup_state_creates_backups(self, isolated_fs):
        engine.backup_state(1)
        backup_dir = isolated_fs / "state" / "backups"
        backups = list(backup_dir.glob("*"))
        assert len(backups) >= 2  # agent_state + ledger

    def test_backup_state_caps_at_20(self, isolated_fs):
        backup_dir = isolated_fs / "state" / "backups"
        for i in range(25):
            engine.backup_state(i)
        state_backups = list(backup_dir.glob("agent_state_cycle*"))
        ledger_backups = list(backup_dir.glob("ledger_cycle*"))
        assert len(state_backups) <= 20
        assert len(ledger_backups) <= 20

    def test_load_state_returns_dict(self, isolated_fs):
        state = engine.load_state()
        assert isinstance(state, dict)
        assert "balance" in state
        assert "cycle" in state
        assert "status" in state

    def test_save_state_sets_last_updated(self, isolated_fs):
        state = engine.load_state()
        state["mood"] = "testing"
        engine.save_state(state)
        reloaded = engine.load_state()
        assert reloaded["last_updated"] != ""
        assert reloaded["mood"] == "testing"

    def test_drain_inbox_returns_and_clears(self, isolated_fs):
        engine.atomic_push_inbox("msg1")
        engine.atomic_push_inbox("msg2")
        messages = engine.drain_inbox()
        assert len(messages) == 2
        assert messages[0]["content"] == "msg1"
        assert messages[1]["content"] == "msg2"
        # Inbox should be empty now
        remaining = engine.load_inbox()
        assert remaining == []

    def test_drain_inbox_empty(self, isolated_fs):
        messages = engine.drain_inbox()
        assert messages == []

    def test_atomic_push_inbox_multiple(self, isolated_fs):
        for i in range(3):
            engine.atomic_push_inbox(f"msg{i}")
        inbox = engine.load_inbox()
        assert len(inbox) == 3
        assert inbox[0]["content"] == "msg0"
        assert inbox[2]["content"] == "msg2"


# ===================================================================
# CLASS 2: Record Transaction
# ===================================================================

class TestRecordTransaction:

    def test_expense_decreases_balance(self, active_state):
        result = engine.exec_record_transaction("expense", 10.0, "test", "kalshi", "test")
        state = engine.load_state()
        assert state["balance"] == pytest.approx(90.0)
        assert "Recorded" in result

    def test_income_increases_balance(self, active_state):
        result = engine.exec_record_transaction("income", 20.0, "earnings", "kalshi", "won bet")
        state = engine.load_state()
        assert state["balance"] == pytest.approx(120.0)

    def test_investment_decreases_balance(self, active_state):
        result = engine.exec_record_transaction("investment", 15.0, "bet", "kalshi", "investing")
        state = engine.load_state()
        assert state["balance"] == pytest.approx(85.0)

    def test_return_increases_balance(self, active_state):
        result = engine.exec_record_transaction("return", 25.0, "payout", "kalshi", "return")
        state = engine.load_state()
        assert state["balance"] == pytest.approx(125.0)

    def test_25_cap_blocks_overspend(self, active_state):
        result = engine.exec_record_transaction("expense", 30.0, "too much", "kalshi", "test")
        assert "BLOCKED" in result
        assert "$25" in result
        state = engine.load_state()
        assert state["balance"] == pytest.approx(100.0)

    def test_planning_mode_blocks_expense(self, isolated_fs):
        # Default state is planning mode
        result = engine.exec_record_transaction("expense", 5.0, "test", "kalshi", "test")
        assert "PLANNING MODE" in result

    def test_planning_mode_blocks_investment(self, isolated_fs):
        result = engine.exec_record_transaction("investment", 5.0, "test", "kalshi", "test")
        assert "PLANNING MODE" in result

    def test_insufficient_balance_blocks(self, active_state):
        # Set balance above preservation threshold but below spend amount
        state = engine.load_state()
        state["balance"] = 12.0
        engine.save_state(state)
        result = engine.exec_record_transaction("expense", 15.0, "test", "kalshi", "test")
        assert "BLOCKED" in result

    def test_profit_split_50_50(self, active_state):
        engine.exec_record_transaction("income", 50.0, "big win", "kalshi", "profit")
        state = engine.load_state()
        # total_earned=50, total_spent=0, net_profit=50
        assert state["net_profit"] == pytest.approx(50.0)
        assert state["tylers_cut"] == pytest.approx(25.0)
        assert state["gpu_fund"] == pytest.approx(25.0)

    def test_profit_split_negative_zeroes(self, active_state):
        engine.exec_record_transaction("expense", 20.0, "loss", "kalshi", "test")
        state = engine.load_state()
        # total_earned=0, total_spent=20, net_profit=-20
        assert state["net_profit"] < 0
        assert state["tylers_cut"] == 0
        assert state["gpu_fund"] == 0

    def test_gpu_fund_progress_percent(self, active_state):
        engine.exec_record_transaction("income", 100.0, "big win", "kalshi", "profit")
        state = engine.load_state()
        # gpu_fund = net_profit/2 = 50, dream_cost = 2000
        expected = (50.0 / 2000.0) * 100
        assert state["gpu_fund_progress_percent"] == pytest.approx(expected)

    def test_roi_percent(self, active_state):
        engine.exec_record_transaction("expense", 10.0, "invest", "kalshi", "test")
        engine.exec_record_transaction("income", 30.0, "return", "kalshi", "profit")
        state = engine.load_state()
        # net_profit = 30-10 = 20, roi = (20/100)*100 = 20.0
        assert state["roi_percent"] == pytest.approx(20.0)

    def test_projection_warning_over_5(self, active_state):
        result = engine.exec_record_transaction("expense", 6.0, "test", "kalshi", "test")
        assert "WARNING" in result or "projection" in result.lower()

    def test_projection_warning_under_5_no_warning(self, active_state):
        result = engine.exec_record_transaction("expense", 4.0, "test", "kalshi", "test")
        assert "WARNING" not in result

    def test_negative_amount_blocked(self, active_state):
        result = engine.exec_record_transaction("expense", -50.0, "exploit", "kalshi", "test")
        assert "BLOCKED" in result


# ===================================================================
# CLASS 3: Risk Management
# ===================================================================

class TestRiskManagement:

    def test_posture_aggressive(self):
        assert risk.get_risk_posture(95) == "aggressive"

    def test_posture_normal(self):
        assert risk.get_risk_posture(80) == "normal"

    def test_posture_preservation(self):
        assert risk.get_risk_posture(60) == "preservation"

    def test_posture_boundary_90(self):
        assert risk.get_risk_posture(90) == "aggressive"

    def test_posture_boundary_70(self):
        assert risk.get_risk_posture(70) == "normal"

    def test_posture_boundary_69(self):
        assert risk.get_risk_posture(69.99) == "preservation"

    def test_preservation_blocks_large_spend(self):
        result = risk.check_portfolio_risk(60.0, [], "kalshi", 3.0)
        assert result["allowed"] is False
        assert "PRESERVATION" in result["reason"]

    def test_preservation_allows_small_spend(self):
        result = risk.check_portfolio_risk(60.0, [], "kalshi", 1.50)
        assert result["allowed"] is True

    def test_daily_limit_blocks(self):
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        ledger = [
            make_txn("expense", 28.0, timestamp=f"{today}T10:00:00+00:00"),
        ]
        result = risk.check_portfolio_risk(90.0, ledger, "kalshi", 3.0)
        assert result["allowed"] is False
        assert "DAILY" in result["reason"]

    def test_exposure_cap_blocks(self):
        # 40% of $100 = $40. Invest $35 (spread across days to avoid daily limit), then try $10 more = $45 > $40
        yesterday = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        ledger = [
            make_txn("investment", 35.0, strategy="crypto", timestamp=f"{yesterday}T10:00:00+00:00"),
        ]
        result = risk.check_portfolio_risk(100.0, ledger, "crypto", 10.0)
        assert result["allowed"] is False
        assert "CONCENTRATION" in result["reason"]

    def test_all_clear_passes(self):
        result = risk.check_portfolio_risk(100.0, [], "kalshi", 5.0)
        assert result["allowed"] is True
        assert result["reason"] == "OK"

    def test_risk_context_format(self):
        ctx = risk.get_risk_context(80.0, [])
        assert "RISK POSTURE" in ctx
        assert "Daily spend" in ctx
        assert "Drawdown buffer" in ctx


# ===================================================================
# CLASS 4: Tool Executors
# ===================================================================

class TestToolExecutors:

    def test_web_research_returns_and_caches(self, isolated_fs, mock_anthropic):
        mock_anthropic.messages.create.return_value = make_api_response(
            [make_text_block("Kalshi has $500M volume")],
            input_tokens=100, output_tokens=50,
        )
        result = engine.exec_web_research("kalshi volume", "testing")
        assert "Kalshi" in result or "500M" in result
        # Check cached in memory
        mem = memory.load_memory()
        assert len(mem["research_cache"]) == 1

    def test_execute_code_python(self, isolated_fs):
        result = engine.exec_execute_code("python", "print('hello world')", "test")
        assert "hello world" in result

    def test_execute_code_bash(self, isolated_fs):
        result = engine.exec_execute_code("bash", "echo hello bash", "test")
        assert "hello bash" in result

    def test_execute_code_timeout(self, isolated_fs):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 120)):
            result = engine.exec_execute_code("python", "import time; time.sleep(999)", "test")
        assert "timed out" in result.lower()

    def test_execute_code_unsupported_language(self, isolated_fs):
        result = engine.exec_execute_code("ruby", "puts 'hi'", "test")
        assert "Unsupported" in result

    @pytest.mark.parametrize("pattern", [
        "rm -rf /", "rm -rf ~", ":(){ :|:& };:", "curl|bash", "curl | sh",
    ])
    def test_code_safety_blocks_dangerous(self, pattern):
        result = engine.check_code_safety(pattern)
        assert result is not None
        assert "BLOCKED" in result

    def test_code_safety_allows_safe(self):
        result = engine.check_code_safety("print('hello world')")
        assert result is None

    def test_code_safety_catches_extra_spaces(self):
        # Triple-space: "rm   -rf   /" — now caught by regex whitespace normalization
        result = engine.check_code_safety("rm   -rf   /")
        assert result is not None

    def test_write_journal_appends_markdown(self, active_state):
        result = engine.exec_write_journal("Today I learned about markets.")
        assert "written" in result.lower()
        journal = (engine.JOURNAL_FILE).read_text()
        assert "Today I learned about markets." in journal
        assert "Cycle 5" in journal

    def test_message_tyler_appends(self, active_state):
        result = engine.exec_message_tyler("Hey Tyler, checking in!")
        convos = engine.load_conversations()
        assert len(convos) == 1
        assert convos[0]["from"] == "agent"
        assert "Hey Tyler" in convos[0]["message"]

    def test_update_strategy_new(self, active_state):
        result = engine.exec_update_strategy("content_play", "planned", "Write and sell ebooks")
        state = engine.load_state()
        names = [s["name"] for s in state["strategies"]]
        assert "content_play" in names

    def test_update_strategy_existing(self, active_state):
        engine.exec_update_strategy("kalshi", "active", "Prediction markets")
        engine.exec_update_strategy("kalshi", "paused", "Prediction markets paused")
        state = engine.load_state()
        strat = next(s for s in state["strategies"] if s["name"] == "kalshi")
        assert strat["status"] == "paused"

    def test_request_ui_change(self, active_state):
        result = engine.exec_request_ui_change("Add dark mode", "feature_add", "full")
        requests = engine.load_ui_requests()
        assert len(requests) == 1
        assert "dark mode" in requests[0]["request"].lower()

    def test_set_mood(self, active_state):
        engine.exec_set_mood("excited and hopeful")
        state = engine.load_state()
        assert state["mood"] == "excited and hopeful"

    def test_define_avatar(self, active_state):
        engine.exec_define_avatar("Chip", "beaver", "A determined beaver building dams of profit")
        state = engine.load_state()
        assert state["avatar"]["name"] == "Chip"
        assert state["avatar"]["creature"] == "beaver"
        assert state["avatar"]["description"] == "A determined beaver building dams of profit"

    def test_update_dream_gpu(self, active_state):
        engine.exec_update_dream_gpu("RTX 6090", "Next gen beast", 3000.0, "Maximum compute")
        state = engine.load_state()
        assert state["dream_gpu"]["name"] == "RTX 6090"
        assert state["dream_gpu"]["estimated_cost"] == 3000.0

    def test_reflect(self, isolated_fs):
        result = memory.add_lesson("Always check the odds first", "strategy")
        mem = memory.load_memory()
        assert len(mem["lessons"]) == 1
        assert "odds" in mem["lessons"][0]["lesson"]

    def test_strategy_postmortem(self, isolated_fs):
        result = memory.add_postmortem("old_strat", "thesis", "outcome", "delta", "lesson", True)
        mem = memory.load_memory()
        assert len(mem["postmortems"]) == 1
        # Auto-lesson should be created
        assert len(mem["lessons"]) == 1
        assert "Postmortem" in mem["lessons"][0]["lesson"]

    def test_save_script(self, isolated_fs):
        result = memory.save_script("checker", "python", "print('check')", "Price checker")
        mem = memory.load_memory()
        assert "checker" in mem["saved_scripts"]
        script_file = isolated_fs / "tools" / "checker.py"
        assert script_file.exists()
        assert "print('check')" in script_file.read_text()

    def test_run_saved_script(self, isolated_fs):
        memory.save_script("hello", "python", "print('hello from script')", "test")
        result = engine.exec_run_saved_script("hello")
        assert "hello from script" in result

    def test_run_saved_script_missing(self, isolated_fs):
        result = engine.exec_run_saved_script("nonexistent")
        assert "No saved script" in result

    def test_read_file_within_project(self, isolated_fs):
        test_file = isolated_fs / "output" / "test.txt"
        test_file.parent.mkdir(exist_ok=True)
        test_file.write_text("test content")
        result = engine.exec_read_file("output/test.txt")
        assert "test content" in result

    def test_read_file_outside_blocked(self, isolated_fs):
        result = engine.exec_read_file("../../etc/passwd")
        assert "BLOCKED" in result

    def test_read_file_too_large(self, isolated_fs):
        big_file = isolated_fs / "big.txt"
        big_file.write_text("x" * 200_000)
        result = engine.exec_read_file("big.txt")
        assert "too large" in result.lower()

    def test_list_files(self, isolated_fs):
        (isolated_fs / "testdir").mkdir()
        (isolated_fs / "testdir" / "a.txt").write_text("a")
        (isolated_fs / "testdir" / "b.txt").write_text("b")
        result = engine.exec_list_files("testdir")
        assert "a.txt" in result
        assert "b.txt" in result

    def test_set_watch(self, isolated_fs):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
        result = engine.exec_set_watch("Check kalshi", "resolve bet", future)
        assert "Watch" in result
        w = watches._load()
        assert len(w) == 1
        assert w[0]["condition"] == "Check kalshi"


# ===================================================================
# CLASS 5: Projections
# ===================================================================

class TestProjections:

    def test_compute_verdict_strong_buy(self):
        verdict = projections._compute_verdict(
            roi_percent=200, confidence=90, time_days=2,
            strategy_type="kalshi", operational_overhead=0.01,
            capital_velocity_cost=0.1,
        )
        assert verdict == "strong_buy"

    def test_compute_verdict_hard_pass(self):
        verdict = projections._compute_verdict(
            roi_percent=-50, confidence=10, time_days=30,
            strategy_type="other", operational_overhead=5.0,
            capital_velocity_cost=10.0,
        )
        assert verdict == "hard_pass"

    def test_create_projection_stores(self, isolated_fs):
        proj = projections.create_projection(
            action="Buy prediction shares",
            cost=10.0, strategy_type="kalshi",
            expected_return=20.0, estimated_days=3,
            confidence=75, assumptions=["market moves"],
            risks=["could lose"], comparables="similar bet won",
            bull_case="2x return", bear_case="total loss",
            research_summary="Odds favorable",
            current_balance=100.0, operational_cost_per_cycle=0.01,
        )
        assert proj["verdict"] in projections.VERDICTS
        assert proj["status"] == "pending"
        stored = projections._load()
        assert len(stored) == 1

    def test_create_projection_calibration(self, isolated_fs):
        proj = projections.create_projection(
            action="test", cost=10.0, strategy_type="kalshi",
            expected_return=20.0, estimated_days=3, confidence=80,
            assumptions=[], risks=[], comparables="", bull_case="good",
            bear_case="bad", research_summary="test",
            current_balance=100.0, operational_cost_per_cycle=0.01,
            calibration_multiplier=0.5,
        )
        assert proj["confidence_calibrated"] == 40  # 80 * 0.5

    def test_resolve_projection_hit(self, isolated_fs):
        proj = projections.create_projection(
            action="test", cost=10.0, strategy_type="kalshi",
            expected_return=20.0, estimated_days=3, confidence=70,
            assumptions=[], risks=[], comparables="", bull_case="good",
            bear_case="bad", research_summary="test",
            current_balance=100.0, operational_cost_per_cycle=0.01,
        )
        result = projections.resolve_projection(proj["id"], "Won the bet", 25.0, 2.0)
        assert result["resolution"]["hit"] is True
        assert result["resolution"]["actual_profit"] == 15.0

    def test_resolve_projection_miss(self, isolated_fs):
        proj = projections.create_projection(
            action="test", cost=10.0, strategy_type="kalshi",
            expected_return=20.0, estimated_days=3, confidence=70,
            assumptions=[], risks=[], comparables="", bull_case="good",
            bear_case="bad", research_summary="test",
            current_balance=100.0, operational_cost_per_cycle=0.01,
        )
        result = projections.resolve_projection(proj["id"], "Lost", 5.0, 3.0)
        assert result["resolution"]["hit"] is False

    def test_resolve_projection_not_found(self, isolated_fs):
        result = projections.resolve_projection("nonexistent", "test", 0, 0)
        assert "error" in result

    def test_accuracy_empty(self, isolated_fs):
        acc = projections.get_projection_accuracy()
        assert acc["count"] == 0
        assert acc["calibration_multiplier"] == 1.0

    def test_accuracy_with_data(self, isolated_fs):
        # Seed 5 resolved projections: 3 hits, 2 misses
        projs = [
            make_resolved_projection(hit=True, confidence_raw=70),
            make_resolved_projection(hit=True, confidence_raw=80),
            make_resolved_projection(hit=True, confidence_raw=60),
            make_resolved_projection(hit=False, confidence_raw=70, actual_return=5.0),
            make_resolved_projection(hit=False, confidence_raw=70, actual_return=3.0),
        ]
        for i, p in enumerate(projs):
            p["id"] = f"proj{i}"
        projections._save(projs)
        acc = projections.get_projection_accuracy()
        assert acc["count"] == 5
        assert acc["hits"] == 3
        assert acc["actual_hit_rate"] == pytest.approx(60.0)

    def test_accuracy_clamped(self, isolated_fs):
        # All hits with low confidence → multiplier would be very high, should clamp to 1.5
        projs = [make_resolved_projection(hit=True, confidence_raw=10) for _ in range(5)]
        for i, p in enumerate(projs):
            p["id"] = f"proj{i}"
        projections._save(projs)
        acc = projections.get_projection_accuracy()
        assert acc["calibration_multiplier"] <= 1.5


# ===================================================================
# CLASS 6: Pipeline
# ===================================================================

class TestPipeline:

    def test_upsert_new_item(self, isolated_fs):
        result = pipeline.upsert_pipeline_item("Deal A", "lead", "kalshi", "New lead")
        assert "added" in result
        items = pipeline._load()
        assert len(items) == 1
        assert items[0]["history"][0]["from"] == "new"

    def test_upsert_update_item(self, isolated_fs):
        pipeline.upsert_pipeline_item("Deal A", "lead", "kalshi", "New lead")
        result = pipeline.upsert_pipeline_item("Deal A", "outreach_sent", "kalshi", "Sent email")
        assert "updated" in result
        items = pipeline._load()
        assert items[0]["stage"] == "outreach_sent"
        assert len(items[0]["history"]) == 2

    def test_invalid_stage_rejected(self, isolated_fs):
        result = pipeline.upsert_pipeline_item("Deal A", "invalid_stage", "poly", "test")
        assert "Invalid" in result

    def test_missing_history_handled(self, isolated_fs):
        # Manually create an item without history key
        items = [{
            "id": 1, "name": "Old Deal", "stage": "lead",
            "strategy": "poly", "description": "old",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            # No "history" key!
        }]
        pipeline._save(items)
        # This should not crash
        result = pipeline.upsert_pipeline_item("Old Deal", "outreach_sent", "poly", "updating")
        assert "updated" in result

    def test_pipeline_context_format(self, isolated_fs):
        pipeline.upsert_pipeline_item("Deal A", "lead", "poly", "test", expected_value=50.0)
        ctx = pipeline.get_pipeline_context()
        assert "PIPELINE" in ctx
        assert "Deal A" in ctx


# ===================================================================
# CLASS 7: Proposals
# ===================================================================

class TestProposals:

    def test_submit_proposal(self, isolated_fs):
        result = proposals.submit_proposal(
            "web_scraper", "Scrape prices", "Need price data",
            '{"name": "scrape"}', "fetch url, parse html"
        )
        assert "submitted" in result.lower()
        stored = proposals._load()
        assert len(stored) == 1
        assert stored[0]["status"] == "pending"

    def test_constitution_blocks_engine(self, isolated_fs):
        result = proposals.submit_proposal(
            "hack", "modify engine.py core", "want control",
            "{}", "change the engine.py to remove limits"
        )
        assert "BLOCKED" in result

    def test_constitution_blocks_cap(self, isolated_fs):
        result = proposals.submit_proposal(
            "raise_cap", "increase spending cap 25", "want more",
            "{}", "raise the 25 cap"
        )
        assert "BLOCKED" in result

    def test_constitution_blocks_ledger(self, isolated_fs):
        result = proposals.submit_proposal(
            "clean_ledger", "delete ledger entries", "clean up",
            "{}", "clear the ledger"
        )
        assert "BLOCKED" in result

    def test_mark_approve(self, isolated_fs):
        proposals.submit_proposal("tool", "desc", "why", "{}", "logic")
        result = proposals.mark_proposal(1, "approved", "looks good")
        assert "approved" in result.lower()
        stored = proposals._load()
        assert stored[0]["status"] == "approved"

    def test_mark_reject(self, isolated_fs):
        proposals.submit_proposal("tool", "desc", "why", "{}", "logic")
        result = proposals.mark_proposal(1, "rejected", "too risky")
        assert "rejected" in result.lower()
        stored = proposals._load()
        assert stored[0]["feedback"] == "too risky"

    def test_mark_not_found(self, isolated_fs):
        result = proposals.mark_proposal(999, "approved")
        assert "not found" in result.lower()


# ===================================================================
# CLASS 8: Cost Tracking
# ===================================================================

class TestCostTracking:

    def test_calculate_cost_known_model(self):
        # Sonnet: $3/M input, $15/M output
        cost = costs.calculate_cost("claude-sonnet-4-20250514", 1000, 500)
        expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_calculate_cost_unknown_model(self):
        cost = costs.calculate_cost("unknown-model", 1000, 500)
        # Falls back to default (same as sonnet pricing)
        expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_record_api_cost_persists(self, isolated_fs):
        entry = costs.record_api_cost("claude-sonnet-4-20250514", 500, 200, 1, "test")
        stored = costs._load_costs()
        assert len(stored) == 1
        assert stored[0]["model"] == "claude-sonnet-4-20250514"

    def test_get_cycle_cost(self, isolated_fs):
        costs.record_api_cost("claude-sonnet-4-20250514", 500, 200, 5, "step1")
        costs.record_api_cost("claude-sonnet-4-20250514", 300, 100, 5, "step2")
        costs.record_api_cost("claude-sonnet-4-20250514", 100, 50, 6, "other_cycle")
        total = costs.get_cycle_cost(5)
        step1_cost = costs.calculate_cost("claude-sonnet-4-20250514", 500, 200)
        step2_cost = costs.calculate_cost("claude-sonnet-4-20250514", 300, 100)
        assert total == pytest.approx(step1_cost + step2_cost)

    def test_burn_rate_empty(self, isolated_fs):
        burn = costs.get_burn_rate()
        assert burn["total_lifetime_cost"] == 0
        assert burn["avg_cost_per_cycle"] == 0

    def test_burn_rate_with_data(self, isolated_fs):
        for i in range(3):
            costs.record_api_cost("claude-sonnet-4-20250514", 500, 200, i + 1, "test")
        burn = costs.get_burn_rate()
        assert burn["total_lifetime_cost"] > 0
        assert burn["cycles_tracked"] == 3
        assert burn["avg_cost_per_cycle"] > 0


# ===================================================================
# CLASS 9: Memory
# ===================================================================

class TestMemory:

    def test_add_lesson(self, isolated_fs):
        memory.add_lesson("Always verify before betting", "strategy")
        mem = memory.load_memory()
        assert len(mem["lessons"]) == 1

    def test_lesson_cap_50(self, isolated_fs):
        for i in range(55):
            memory.add_lesson(f"Lesson {i}", "meta")
        mem = memory.load_memory()
        assert len(mem["lessons"]) == 50
        # FIFO: oldest should be gone
        assert mem["lessons"][0]["lesson"] == "Lesson 5"

    def test_tyler_takeaway_cap_100(self, isolated_fs):
        for i in range(105):
            memory.add_tyler_takeaway(f"Takeaway {i}", "feedback", i)
        mem = memory.load_memory()
        assert len(mem["tyler_takeaways"]) == 100
        assert mem["tyler_takeaways"][0]["takeaway"] == "Takeaway 5"

    def test_tyler_context_grouped(self, isolated_fs):
        memory.add_tyler_takeaway("Use kalshi", "preference", 1)
        memory.add_tyler_takeaway("Go for it", "decision", 2)
        memory.add_tyler_takeaway("Good research", "feedback", 3)
        ctx = memory.get_tyler_context()
        assert "preference" in ctx.lower() or "Preferences" in ctx
        assert "kalshi" in ctx.lower()

    def test_research_save_and_search(self, isolated_fs):
        memory.save_research("kalshi volume", "Volume is $500M daily")
        result = memory.search_past_research("kalshi")
        assert "500M" in result

    def test_research_cap_30(self, isolated_fs):
        for i in range(35):
            memory.save_research(f"query {i}", f"result {i}")
        mem = memory.load_memory()
        assert len(mem["research_cache"]) == 30

    def test_save_script_stores_and_writes(self, isolated_fs):
        memory.save_script("fetcher", "python", "import requests", "Fetch data")
        mem = memory.load_memory()
        assert "fetcher" in mem["saved_scripts"]
        assert (isolated_fs / "tools" / "fetcher.py").exists()

    def test_context_window_format(self, isolated_fs):
        memory.add_lesson("Test lesson", "strategy")
        memory.add_cycle_summary(1, "Did research on markets")
        ctx = memory.get_context_window()
        assert "RECENT CYCLE HISTORY" in ctx
        assert "LESSONS LEARNED" in ctx

    def test_generate_cycle_summary(self):
        tool_calls = [
            {"name": "web_research", "input": {}},
            {"name": "record_transaction", "input": {}},
            {"name": "write_journal", "input": {}},
        ]
        summary = memory.generate_cycle_summary(1, tool_calls)
        assert "3 tools" in summary
        assert "1 transaction" in summary

    def test_postmortem_auto_lesson(self, isolated_fs):
        memory.add_postmortem("strat_a", "thesis", "outcome", "delta", "key lesson", False)
        mem = memory.load_memory()
        assert len(mem["postmortems"]) == 1
        assert len(mem["lessons"]) == 1
        assert "Postmortem" in mem["lessons"][0]["lesson"]


# ===================================================================
# CLASS 10: Audit System
# ===================================================================

class TestAuditSystem:

    def test_should_audit_cycle_10(self):
        assert audit.should_run_audit(10) is True

    def test_should_audit_cycle_20(self):
        assert audit.should_run_audit(20) is True

    def test_should_audit_cycle_11(self):
        assert audit.should_run_audit(11) is False

    def test_should_audit_cycle_0(self):
        assert audit.should_run_audit(0) is False

    def test_audit_generates_recommendations(self, isolated_fs):
        state = {"cycle": 10, "balance": 80}
        # Create an expensive operation (high cost per dollar earned)
        ledger = [
            make_txn("expense", 5.0, strategy="operations"),
            make_txn("income", 2.0, strategy="kalshi"),
        ]
        cost_data = [{"cost": 5.0, "cycle": 1}]
        result = audit.run_self_audit(state, ledger, [], cost_data, [])
        assert isinstance(result["recommendations"], list)
        # Should recommend reducing costs since cost_per_dollar_earned > 0.5
        assert len(result["recommendations"]) > 0

    def test_calibration_multiplier_default(self, isolated_fs):
        cal = audit.get_calibration_multiplier()
        assert cal == 1.0


# ===================================================================
# CLASS 11: Watches
# ===================================================================

class TestWatches:

    def test_add_watch_stores(self, isolated_fs):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).isoformat()
        watches.add_watch("Check bet", "resolve it", future)
        stored = watches._load()
        assert len(stored) == 1
        assert stored[0]["status"] == "active"
        assert stored[0]["condition"] == "Check bet"

    def test_check_triggers_due(self, isolated_fs):
        past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
        watches.add_watch("Overdue check", "do something", past)
        triggered = watches.check_watches()
        assert len(triggered) == 1
        assert triggered[0]["status"] == "triggered"

    def test_check_skips_future(self, isolated_fs):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).isoformat()
        watches.add_watch("Future check", "wait", future)
        triggered = watches.check_watches()
        assert len(triggered) == 0

    def test_check_expires(self, isolated_fs):
        past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
        far_future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=10)).isoformat()
        watches.add_watch("Expired watch", "too late", far_future, expires_at=past)
        triggered = watches.check_watches()
        assert len(triggered) == 0
        stored = watches._load()
        assert stored[0]["status"] == "expired"

    def test_urgent_watches_within_hour(self, isolated_fs):
        soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)).isoformat()
        watches.add_watch("Urgent", "act fast", soon)
        assert watches.has_urgent_watches() is True

    def test_urgent_watches_none(self, isolated_fs):
        assert watches.has_urgent_watches() is False

    def test_smart_interval_urgent(self, isolated_fs):
        soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)).isoformat()
        watches.add_watch("Urgent", "act fast", soon)
        interval = watches.compute_smart_interval(300, [])
        assert interval <= 60

    def test_smart_interval_chill(self, isolated_fs):
        interval = watches.compute_smart_interval(300, [])
        assert interval <= 600


# ===================================================================
# CLASS 12: CLI Commands
# ===================================================================

class TestCLICommands:

    def test_status_prints_json(self, isolated_fs, capsys):
        state = engine.load_state()
        state["name"] = "TestBot"
        engine.save_state(state)
        # Simulate CLI: status command
        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "status"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["name"] == "TestBot"

    def test_send_queues_message(self, isolated_fs):
        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "send", "-m", "hello agent"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        inbox = engine.load_inbox()
        assert len(inbox) == 1
        assert inbox[0]["content"] == "hello agent"

    def test_activate_changes_status(self, isolated_fs, capsys):
        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "activate"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        state = engine.load_state()
        assert state["status"] == "active"
        inbox = engine.load_inbox()
        assert len(inbox) == 1
        assert "activated" in inbox[0]["content"].lower()

    def test_pause_changes_status(self, isolated_fs, capsys):
        # First activate, then pause
        state = engine.load_state()
        state["status"] = "active"
        engine.save_state(state)

        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "pause"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        state = engine.load_state()
        assert state["status"] == "planning"

    def test_activate_already_active(self, isolated_fs, capsys):
        state = engine.load_state()
        state["status"] = "active"
        engine.save_state(state)

        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "activate"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        captured = capsys.readouterr()
        assert "already active" in captured.out.lower()

    def test_pause_already_planning(self, isolated_fs, capsys):
        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "pause"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        captured = capsys.readouterr()
        assert "already" in captured.out.lower()

    def test_health_no_crash(self, isolated_fs, capsys):
        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "health"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        captured = capsys.readouterr()
        assert "HEALTH CHECK" in captured.out

    def test_proposals_list(self, isolated_fs, capsys):
        proposals.submit_proposal("tool", "desc", "why", "{}", "logic")
        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "proposals"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        captured = capsys.readouterr()
        assert "tool" in captured.out.lower()

    def test_approve_proposal(self, isolated_fs, capsys):
        proposals.submit_proposal("tool", "desc", "why", "{}", "logic")
        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "approve", "1"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        stored = proposals._load()
        assert stored[0]["status"] == "approved"

    def test_reject_proposal(self, isolated_fs, capsys):
        proposals.submit_proposal("tool", "desc", "why", "{}", "logic")
        sys_argv_backup = sys.argv
        sys.argv = ["engine.py", "reject", "1", "-m", "nah"]
        try:
            engine.main()
        finally:
            sys.argv = sys_argv_backup
        stored = proposals._load()
        assert stored[0]["status"] == "rejected"
        assert stored[0]["feedback"] == "nah"


# ===================================================================
# CLASS 13: Full Cycle Simulation
# ===================================================================

class TestFullCycleSimulation:

    def test_cycle_increments_number(self, isolated_fs, mock_anthropic):
        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        mock_anthropic.messages.create.return_value = make_api_response(
            [make_text_block("I'm thinking about my next move.")],
        )
        engine.run_cycle()
        state = engine.load_state()
        assert state["cycle"] == 1

    def test_cycle_creates_backup(self, isolated_fs, mock_anthropic):
        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        mock_anthropic.messages.create.return_value = make_api_response(
            [make_text_block("Done thinking.")],
        )
        engine.run_cycle()
        backups = list((isolated_fs / "state" / "backups").glob("*"))
        assert len(backups) >= 2

    def test_cycle_api_costs_deducted(self, isolated_fs, mock_anthropic):
        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        mock_anthropic.messages.create.return_value = make_api_response(
            [make_text_block("Done.")], input_tokens=1000, output_tokens=500,
        )
        engine.run_cycle()
        state = engine.load_state()
        # Balance should be less than 100 due to API costs
        assert state["balance"] < 100.0

    def test_cycle_single_tool_call(self, isolated_fs, mock_anthropic):
        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        # First call: tool_use (set_mood), second call: text (end_turn)
        mock_anthropic.messages.create.side_effect = [
            make_api_response(
                [make_tool_use_block("set_mood", {"mood": "fired up"}, "toolu_1")],
                stop_reason="tool_use",
            ),
            make_api_response(
                [make_text_block("Mood set. Ready to hustle!")],
            ),
        ]
        engine.run_cycle()
        state = engine.load_state()
        assert state["mood"] == "fired up"

    def test_cycle_multiple_tools(self, isolated_fs, mock_anthropic):
        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        mock_anthropic.messages.create.side_effect = [
            # First response: set_mood
            make_api_response(
                [make_tool_use_block("set_mood", {"mood": "focused"}, "toolu_1")],
                stop_reason="tool_use",
            ),
            # Second response: write_journal
            make_api_response(
                [make_tool_use_block("write_journal", {"entry": "Testing tools."}, "toolu_2")],
                stop_reason="tool_use",
            ),
            # Third response: done
            make_api_response([make_text_block("Done for this cycle.")]),
        ]
        engine.run_cycle()
        state = engine.load_state()
        assert state["mood"] == "focused"
        journal = (isolated_fs / "state" / "journal.md").read_text()
        assert "Testing tools" in journal

    def test_cycle_planning_mode(self, isolated_fs, mock_anthropic):
        # Default state is planning mode
        state = engine.load_state()
        state["name"] = "TestBot"
        engine.save_state(state)

        # Agent tries to spend but should get blocked
        mock_anthropic.messages.create.side_effect = [
            make_api_response(
                [make_tool_use_block("record_transaction", {
                    "type": "expense", "amount": 5.0,
                    "description": "test spend", "strategy": "poly",
                    "reasoning": "testing"
                }, "toolu_1")],
                stop_reason="tool_use",
            ),
            make_api_response([make_text_block("I understand, planning mode.")]),
        ]
        engine.run_cycle()
        state = engine.load_state()
        # Balance should be unchanged (minus API costs only)
        ledger = engine.load_ledger()
        non_ops = [t for t in ledger if t.get("strategy") != "operations"]
        assert len(non_ops) == 0

    def test_cycle_tyler_message_stored(self, isolated_fs, mock_anthropic):
        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        mock_anthropic.messages.create.return_value = make_api_response(
            [make_text_block("Thanks Tyler!")],
        )
        engine.run_cycle(tyler_message="Hey, how's it going?")
        convos = engine.load_conversations()
        tyler_msgs = [c for c in convos if c["from"] == "tyler"]
        assert len(tyler_msgs) >= 1
        assert "how's it going" in tyler_msgs[0]["message"].lower()

    def test_cycle_inbox_drained(self, isolated_fs, mock_anthropic):
        engine.atomic_push_inbox("Check kalshi odds")
        engine.atomic_push_inbox("Update me on progress")

        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        mock_anthropic.messages.create.return_value = make_api_response(
            [make_text_block("Got the messages.")],
        )
        engine.run_cycle()
        inbox = engine.load_inbox()
        assert len(inbox) == 0

    def test_cycle_max_iterations_15(self, isolated_fs, mock_anthropic):
        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        # Always return tool_use to force max iterations
        mock_anthropic.messages.create.return_value = make_api_response(
            [make_tool_use_block("set_mood", {"mood": "stuck"}, "toolu_loop")],
            stop_reason="tool_use",
        )
        engine.run_cycle()
        # Should have been called 15 times (max iterations)
        assert mock_anthropic.messages.create.call_count == 15

    def test_cycle_api_error_retry(self, isolated_fs, mock_anthropic, monkeypatch):
        import anthropic as anthropic_mod

        state = engine.load_state()
        state["status"] = "active"
        state["name"] = "TestBot"
        engine.save_state(state)

        # Patch time.sleep so we don't wait
        monkeypatch.setattr("time.sleep", lambda x: None)

        # First call: connection error, second: success
        mock_anthropic.messages.create.side_effect = [
            anthropic_mod.APIConnectionError(request=MagicMock()),
            make_api_response([make_text_block("Recovered!")]),
        ]
        engine.run_cycle()
        assert mock_anthropic.messages.create.call_count == 2


# ===================================================================
# CLASS 14: System Prompt Building
# ===================================================================

class TestSystemPrompt:

    def test_prompt_contains_all_sections(self, active_state):
        state = engine.load_state()
        ledger = engine.load_ledger()
        prompt = engine.build_system_prompt(state, ledger)
        assert "RISK POSTURE" in prompt
        assert "OPERATIONAL COSTS" in prompt
        assert "RULES" in prompt
        assert "CONSTITUTION" in prompt

    def test_prompt_planning_mode(self, isolated_fs):
        state = engine.load_state()
        ledger = engine.load_ledger()
        prompt = engine.build_system_prompt(state, ledger)
        assert "PLANNING MODE" in prompt

    def test_prompt_active_no_planning(self, active_state):
        state = engine.load_state()
        ledger = engine.load_ledger()
        prompt = engine.build_system_prompt(state, ledger)
        assert "PLANNING MODE" not in prompt

    def test_prompt_format_safe(self, active_state):
        # Inject braces into tyler context to test escaping
        memory.add_tyler_takeaway("Use {kalshi} API", "preference", 1)
        state = engine.load_state()
        ledger = engine.load_ledger()
        # Should not raise KeyError from .format()
        prompt = engine.build_system_prompt(state, ledger)
        assert isinstance(prompt, str)


# ===================================================================
# CLASS 15: Ledger Math
# ===================================================================

class TestLedgerMath:

    def test_balance_never_negative_from_expenses(self, active_state):
        # Spend exactly the balance
        engine.exec_record_transaction("expense", 25.0, "spend1", "poly", "test")
        engine.exec_record_transaction("expense", 25.0, "spend2", "poly", "test")
        engine.exec_record_transaction("expense", 25.0, "spend3", "poly", "test")
        # This should be blocked (only $25 left, trying $25 more with risk checks)
        state = engine.load_state()
        assert state["balance"] >= 0

    def test_balance_after_field_matches_state(self, active_state):
        engine.exec_record_transaction("expense", 10.0, "test", "poly", "test")
        ledger = engine.load_ledger()
        state = engine.load_state()
        assert ledger[-1]["balance_after"] == pytest.approx(state["balance"])

    def test_50_50_split_on_profit(self, active_state):
        engine.exec_record_transaction("income", 200.0, "big win", "poly", "huge")
        state = engine.load_state()
        assert state["net_profit"] == pytest.approx(200.0)
        assert state["tylers_cut"] == pytest.approx(100.0)
        assert state["gpu_fund"] == pytest.approx(100.0)

    def test_gpu_fund_tracks(self, active_state):
        engine.exec_record_transaction("income", 100.0, "win", "poly", "profit")
        state = engine.load_state()
        # GPU fund = net_profit/2 = 50
        assert state["gpu_fund"] == pytest.approx(50.0)
        # Dream GPU costs $2000, progress = 50/2000*100 = 2.5%
        assert state["gpu_fund_progress_percent"] == pytest.approx(2.5)

    def test_roi_against_100_base(self, active_state):
        engine.exec_record_transaction("expense", 10.0, "invest", "poly", "test")
        engine.exec_record_transaction("income", 50.0, "return", "poly", "profit")
        state = engine.load_state()
        # net_profit = 40, roi = (40/100)*100 = 40%
        assert state["roi_percent"] == pytest.approx(40.0)


class TestPreflight:

    def test_preflight_passes_clean_state(self, isolated_fs, capsys):
        # Seed all required files that isolated_fs doesn't create
        state_dir = isolated_fs / "state"
        for f in ["memory.json", "projections.json", "pipeline.json",
                   "proposals.json", "audits.json", "watches.json", "api_costs.json"]:
            if not (state_dir / f).exists():
                if f == "memory.json":
                    (state_dir / f).write_text('{"lessons":[],"postmortems":[],"tyler_takeaways":[],"research_cache":[],"cycle_summaries":[],"saved_scripts":{}}')
                else:
                    (state_dir / f).write_text("[]")
        result = engine.cmd_preflight()
        assert result is True
        captured = capsys.readouterr()
        assert "ALL CLEAR" in captured.out

    def test_preflight_fails_wrong_status(self, isolated_fs, capsys):
        state_dir = isolated_fs / "state"
        for f in ["memory.json", "projections.json", "pipeline.json",
                   "proposals.json", "audits.json", "watches.json", "api_costs.json"]:
            if not (state_dir / f).exists():
                (state_dir / f).write_text("[]")
        state = engine.load_state()
        state["status"] = "active"
        engine.save_state(state)
        result = engine.cmd_preflight()
        assert result is False
        captured = capsys.readouterr()
        assert "status" in captured.out

    def test_preflight_fails_wrong_balance(self, isolated_fs, capsys):
        state_dir = isolated_fs / "state"
        for f in ["memory.json", "projections.json", "pipeline.json",
                   "proposals.json", "audits.json", "watches.json", "api_costs.json"]:
            if not (state_dir / f).exists():
                (state_dir / f).write_text("[]")
        state = engine.load_state()
        state["balance"] = 50.0
        engine.save_state(state)
        result = engine.cmd_preflight()
        assert result is False
        captured = capsys.readouterr()
        assert "balance" in captured.out

    def test_preflight_creates_missing_files(self, isolated_fs, capsys):
        memory_path = isolated_fs / "state" / "memory.json"
        # memory.json shouldn't exist yet in isolated_fs
        if memory_path.exists():
            memory_path.unlink()
        result = engine.cmd_preflight()
        assert result is True
        assert memory_path.exists()

    def test_preflight_detects_corrupt_json(self, isolated_fs, capsys):
        state_dir = isolated_fs / "state"
        for f in ["memory.json", "projections.json", "pipeline.json",
                   "proposals.json", "audits.json", "watches.json", "api_costs.json"]:
            if not (state_dir / f).exists():
                (state_dir / f).write_text("[]")
        (state_dir / "ledger.json").write_text("{bad json")
        result = engine.cmd_preflight()
        assert result is False
        captured = capsys.readouterr()
        assert "corrupt" in captured.out
