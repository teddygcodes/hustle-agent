"""
Hustle Agent — Instincts Engine Tests

Tests for action tracking, priors, instinct computation,
explore/exploit mode, and projection integration.
"""

import json
import datetime
from pathlib import Path

import pytest

from agent import instincts, engine, risk, audit
from tests.conftest import make_action, make_resolved_projection


# ---------------------------------------------------------------------------
# Priors
# ---------------------------------------------------------------------------

class TestPriors:
    def test_seed_priors_creates_file(self, isolated_fs):
        result = instincts.seed_priors()
        assert "kalshi" in result
        assert "service" in result
        assert result["kalshi"]["source"] == "default"
        assert result["kalshi"]["validated"] is False

    def test_seed_priors_does_not_overwrite(self, isolated_fs):
        instincts.seed_priors()
        instincts.update_priors_from_research("kalshi", 0.60, 0.12, "my research")
        priors = instincts.seed_priors()  # should not overwrite
        assert priors["kalshi"]["win_rate"] == 0.60
        assert priors["kalshi"]["validated"] is True

    def test_update_priors_from_research(self, isolated_fs):
        instincts.seed_priors()
        result = instincts.update_priors_from_research("kalshi", 0.55, 0.10, "actual data")
        assert result["win_rate"] == 0.55
        assert result["avg_roi"] == 0.10
        assert result["source"] == "research"
        assert result["validated"] is True
        assert result["research_date"] is not None

    def test_update_priors_clamps_win_rate(self, isolated_fs):
        instincts.seed_priors()
        result = instincts.update_priors_from_research("service", 1.5, 0.5)
        assert result["win_rate"] == 1.0

    def test_priors_need_validation_true_initially(self, isolated_fs):
        instincts.seed_priors()
        assert instincts.priors_need_validation() is True

    def test_priors_need_validation_false_after_research(self, isolated_fs):
        instincts.seed_priors()
        for cat in instincts.DEFAULT_PRIORS:
            instincts.update_priors_from_research(cat, 0.5, 0.5)
        assert instincts.priors_need_validation() is False

    def test_normalize_category(self):
        assert instincts.normalize_category("product_sale") == "product"
        assert instincts.normalize_category("kalshi") == "kalshi"
        assert instincts.normalize_category("KALSHI") == "kalshi"
        assert instincts.normalize_category("unknown_thing") == "other"


# ---------------------------------------------------------------------------
# Bayesian blending
# ---------------------------------------------------------------------------

class TestBlending:
    def test_blend_zero_earned(self):
        assert instincts._blend(0.5, 0.8, 0) == 0.5

    def test_blend_full_earned(self):
        assert instincts._blend(0.5, 0.8, 5) == 0.8
        assert instincts._blend(0.5, 0.8, 10) == 0.8

    def test_blend_partial(self):
        result = instincts._blend(0.5, 0.8, 3)
        expected = 0.5 * 0.4 + 0.8 * 0.6  # weight = 3/5 = 0.6
        assert abs(result - expected) < 0.001


# ---------------------------------------------------------------------------
# Action tracking
# ---------------------------------------------------------------------------

class TestActionTracking:
    def test_create_action(self, isolated_fs):
        action = instincts.create_action(
            category="kalshi", subcategory="test bet",
            cost=5.0, expected_return=10.0, time_horizon_days=3.0,
            confidence=70, balance=100.0, risk_posture="aggressive",
            projection_id="proj123",
        )
        assert action["action_id"]
        assert action["projection_id"] == "proj123"
        assert action["category"] == "kalshi"
        assert action["status"] == "pending"
        assert action["conditions"]["confidence_at_decision"] == 70
        assert action["conditions"]["capital_percentage"] == 5.0

        # Verify persisted
        actions = instincts.load_actions()
        assert len(actions) == 1

    def test_resolve_action(self, isolated_fs):
        action = instincts.create_action(
            category="service", subcategory="gig",
            cost=10.0, expected_return=20.0, time_horizon_days=2.0,
            confidence=80, balance=90.0, risk_posture="aggressive",
            projection_id="proj456",
        )
        resolved = instincts.resolve_action("proj456", 25.0, 1.5, "won")
        assert resolved is not None
        assert resolved["status"] == "won"
        assert resolved["actual_return"] == 25.0
        assert resolved["actual_time_days"] == 1.5
        assert resolved["resolved_at"] is not None

    def test_resolve_action_auto_status(self, isolated_fs):
        instincts.create_action(
            category="kalshi", subcategory="bet",
            cost=5.0, expected_return=10.0, time_horizon_days=1.0,
            confidence=60, balance=100.0, risk_posture="normal",
            projection_id="proj789",
        )
        # Profit > 0 → "won"
        resolved = instincts.resolve_action("proj789", 8.0, 1.0, "auto")
        assert resolved["status"] == "won"

    def test_resolve_action_loss(self, isolated_fs):
        instincts.create_action(
            category="kalshi", subcategory="bet",
            cost=5.0, expected_return=10.0, time_horizon_days=1.0,
            confidence=60, balance=100.0, risk_posture="normal",
            projection_id="projloss",
        )
        resolved = instincts.resolve_action("projloss", 2.0, 1.0, "auto")
        assert resolved["status"] == "lost"

    def test_resolve_nonexistent_action(self, isolated_fs):
        result = instincts.resolve_action("nonexistent", 10.0, 1.0, "won")
        assert result is None


# ---------------------------------------------------------------------------
# Instinct computation
# ---------------------------------------------------------------------------

class TestInstinctComputation:
    def _seed_actions(self, isolated_fs, actions):
        instincts.save_actions(actions)

    def test_recompute_with_no_actions(self, isolated_fs):
        data = instincts.recompute_instincts()
        assert data["action_count_at_compute"] == 0
        assert data["exploration_mode"] == "explore"

    def test_category_win_rate(self, isolated_fs):
        actions = [
            make_action(category="kalshi", status="won"),
            make_action(category="kalshi", status="won"),
            make_action(category="kalshi", status="lost", actual_return=2.0),
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        assert data["category_scores"]["kalshi"]["win_rate"] == pytest.approx(2/3, abs=0.01)
        assert data["category_scores"]["kalshi"]["sample_size"] == 3

    def test_category_roi(self, isolated_fs):
        actions = [
            make_action(category="service", cost=10.0, actual_return=20.0, status="won"),
            make_action(category="service", cost=10.0, actual_return=5.0, status="lost"),
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        # ROI: (20-10)/10=1.0 and (5-10)/10=-0.5 → avg = 0.25
        assert data["category_scores"]["service"]["avg_roi"] == pytest.approx(0.25, abs=0.01)

    def test_dimension_scores_time_horizon(self, isolated_fs):
        actions = [
            make_action(time_horizon_days=0.5, status="won"),  # under_1d
            make_action(time_horizon_days=0.5, status="won"),
            make_action(time_horizon_days=3.0, status="lost", actual_return=2.0),  # 1_to_7d
            make_action(time_horizon_days=14.0, status="lost", actual_return=2.0),  # over_7d
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        dims = data["dimension_scores"].get("time_horizon", {})
        assert dims.get("under_1d", {}).get("win_rate") == 1.0
        assert dims.get("under_1d", {}).get("sample_size") == 2

    def test_dimension_scores_capital_size(self, isolated_fs):
        actions = [
            make_action(cost=2.0, status="won"),  # under_5
            make_action(cost=10.0, status="lost", actual_return=5.0),  # 5_to_15
            make_action(cost=20.0, status="lost", actual_return=10.0),  # over_15
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        dims = data["dimension_scores"].get("capital_size", {})
        assert "under_5" in dims
        assert dims["under_5"]["win_rate"] == 1.0

    def test_cross_patterns(self, isolated_fs):
        # Need at least 3 resolved and 2+ in a cell
        actions = [
            make_action(category="kalshi", time_horizon_days=0.5, status="won"),
            make_action(category="kalshi", time_horizon_days=0.5, status="won"),
            make_action(category="kalshi", time_horizon_days=14.0, status="lost", actual_return=2.0),
            make_action(category="kalshi", time_horizon_days=14.0, status="lost", actual_return=1.0),
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        patterns = data["cross_patterns"]
        assert len(patterns) > 0
        # Should find kalshi+short with high win rate
        short_pattern = [p for p in patterns if "short" in p["key"] and "kalshi" in p["key"]]
        if short_pattern:
            assert short_pattern[0]["win_rate"] == 1.0

    def test_calibration_per_category(self, isolated_fs):
        # Agent says 80% confidence but wins only 50% → multiplier < 1.0
        actions = [
            make_action(category="kalshi", confidence=80, status="won"),
            make_action(category="kalshi", confidence=80, status="lost", actual_return=2.0),
            make_action(category="kalshi", confidence=80, status="won"),
            make_action(category="kalshi", confidence=80, status="lost", actual_return=2.0),
            make_action(category="kalshi", confidence=80, status="won"),
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        cal = data["calibration"]["per_category"].get("kalshi")
        assert cal is not None
        # 60% win rate / 80% confidence = 0.75, fully earned (5 actions)
        assert cal == pytest.approx(0.75, abs=0.05)

    def test_instinct_sentences_generated(self, isolated_fs):
        actions = [
            make_action(category="service", status="won", confidence=70),
            make_action(category="service", status="won", confidence=70),
            make_action(category="service", status="lost", actual_return=3.0, confidence=70),
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        assert len(data["instinct_sentences"]) >= 1

    def test_history_capped(self, isolated_fs):
        actions = [make_action(status="won") for _ in range(3)]
        self._seed_actions(isolated_fs, actions)
        # Recompute many times
        for _ in range(25):
            instincts.recompute_instincts()
        data = instincts.load_instincts()
        assert len(data["history"]) <= instincts.INSTINCTS_HISTORY_CAP


# ---------------------------------------------------------------------------
# Exploration mode
# ---------------------------------------------------------------------------

class TestExplorationMode:
    def test_explore_under_5_actions(self, isolated_fs):
        actions = [make_action(category="kalshi", status="won") for _ in range(4)]
        instincts.save_actions(actions)
        assert instincts.get_exploration_mode() == "explore"

    def test_explore_under_3_categories(self, isolated_fs):
        actions = (
            [make_action(category="kalshi", status="won") for _ in range(6)]
            + [make_action(category="service", status="won") for _ in range(6)]
        )
        instincts.save_actions(actions)
        assert instincts.get_exploration_mode() == "explore"  # only 2 categories with 5+

    def test_exploit_mode_threshold(self, isolated_fs):
        actions = (
            [make_action(category="kalshi", status="won") for _ in range(5)]
            + [make_action(category="service", status="won") for _ in range(5)]
            + [make_action(category="content", status="won") for _ in range(5)]
        )
        instincts.save_actions(actions)
        assert instincts.get_exploration_mode() == "exploit"

    def test_explore_mode_risk_cap(self, isolated_fs):
        # In explore mode, risk check should block >$5
        result = risk.check_portfolio_risk(100.0, [], "test", 6.0, exploration_mode="explore")
        assert result["allowed"] is False
        assert "EXPLORATION MODE" in result["reason"]

    def test_explore_mode_allows_small(self, isolated_fs):
        result = risk.check_portfolio_risk(100.0, [], "test", 4.0, exploration_mode="explore")
        assert result["allowed"] is True

    def test_exploit_mode_no_extra_cap(self, isolated_fs):
        result = risk.check_portfolio_risk(100.0, [], "test", 15.0, exploration_mode="exploit")
        assert result["allowed"] is True

    def test_exploration_progress(self, isolated_fs):
        actions = (
            [make_action(category="kalshi", status="won") for _ in range(3)]
            + [make_action(category="service", status="won") for _ in range(5)]
        )
        instincts.save_actions(actions)
        progress = instincts.get_exploration_progress()
        assert progress["mode"] == "explore"
        assert progress["categories"]["kalshi"] == 3
        assert progress["categories"]["service"] == 5
        assert progress["categories_ready"] == 1


# ---------------------------------------------------------------------------
# Projection adjustments
# ---------------------------------------------------------------------------

class TestProjectionAdjustments:
    def test_adjustments_with_no_data(self, isolated_fs):
        instincts.seed_priors()
        adj = instincts.get_adjustments_for_action("kalshi", {
            "time_horizon_days": 5,
            "confidence_at_decision": 70,
        })
        assert adj["data_source"] == "prior"
        assert adj["calibration_multiplier"] == 1.0  # prior assumes no miscalibration
        assert adj["blended_win_rate"] == pytest.approx(0.52, abs=0.01)

    def test_adjustments_with_earned_data(self, isolated_fs):
        instincts.seed_priors()
        actions = [
            make_action(category="kalshi", confidence=80, status="won"),
            make_action(category="kalshi", confidence=80, status="won"),
            make_action(category="kalshi", confidence=80, status="lost", actual_return=2.0),
        ]
        instincts.save_actions(actions)
        instincts.recompute_instincts()

        adj = instincts.get_adjustments_for_action("kalshi", {
            "confidence_at_decision": 80,
        })
        assert adj["data_source"] == "blend"
        assert adj["earned_count"] == 3
        # Should be blended between prior and earned
        assert 0.5 < adj["blended_win_rate"] < 0.75

    def test_exploration_note_shown(self, isolated_fs):
        instincts.seed_priors()
        actions = [make_action(category="kalshi", status="won")]
        instincts.save_actions(actions)

        adj = instincts.get_adjustments_for_action("kalshi", {})
        assert "Exploratory bet" in adj["exploration_note"] or "data point" in adj["exploration_note"]


# ---------------------------------------------------------------------------
# System prompt context
# ---------------------------------------------------------------------------

class TestSystemPromptContext:
    def test_empty_context_no_data(self, isolated_fs):
        ctx = instincts.get_instincts_context()
        assert ctx == ""

    def test_explore_context(self, isolated_fs):
        instincts.seed_priors()
        actions = [make_action(category="kalshi", status="won")]
        instincts.save_actions(actions)
        instincts.recompute_instincts()

        ctx = instincts.get_instincts_context()
        assert "EXPLORATION MODE" in ctx
        assert "Small bets" in ctx

    def test_exploit_context(self, isolated_fs):
        instincts.seed_priors()
        actions = (
            [make_action(category="kalshi", status="won") for _ in range(5)]
            + [make_action(category="service", status="won") for _ in range(5)]
            + [make_action(category="content", status="won") for _ in range(5)]
        )
        instincts.save_actions(actions)
        instincts.recompute_instincts()

        ctx = instincts.get_instincts_context()
        assert "YOUR INSTINCTS" in ctx
        assert "EXPLORATION MODE" not in ctx
        assert "Calibration" in ctx


# ---------------------------------------------------------------------------
# Audit integration
# ---------------------------------------------------------------------------

class TestAuditIntegration:
    def test_audit_crosscheck_explore_mode_warning(self, isolated_fs):
        # Write instincts.json with explore mode
        instincts._save_instincts({
            "exploration_mode": "explore",
            "calibration": {"overall": 1.0, "per_category": {}},
        })
        recs = audit._audit_instincts_crosscheck({"calibration_multiplier": 1.0}, cycle=25)
        assert any("EXPLORE" in r for r in recs)

    def test_audit_crosscheck_calibration_divergence(self, isolated_fs):
        instincts._save_instincts({
            "exploration_mode": "exploit",
            "calibration": {
                "overall": 0.9,
                "per_category": {"kalshi": 0.5},  # diverges from audit
            },
        })
        recs = audit._audit_instincts_crosscheck({"calibration_multiplier": 0.9}, cycle=30)
        assert any("kalshi" in r for r in recs)


# ---------------------------------------------------------------------------
# Engine integration tests
# ---------------------------------------------------------------------------

class TestEngineIntegration:
    """Tests for engine functions that interact with the instincts system."""

    def test_run_projection_creates_action(self, isolated_fs):
        """exec_run_projection should create a pending action in actions.json."""
        instincts.seed_priors()
        output = engine.exec_run_projection(
            action="Test bet on event",
            cost=5.0,
            strategy_type="kalshi",
            expected_return=10.0,
            estimated_days_to_return=3.0,
            confidence=70,
            research_summary="Test research",
            assumptions=["assumption"],
            risks=["risk"],
            bull_case="good outcome",
            bear_case="bad outcome",
        )
        assert "PROJECTION #" in output
        actions = instincts.load_actions()
        assert len(actions) == 1
        assert actions[0]["status"] == "pending"
        assert actions[0]["category"] == "kalshi"
        assert actions[0]["cost"] == 5.0

    def test_resolve_projection_resolves_action(self, isolated_fs):
        """exec_resolve_projection should resolve the linked action and recompute instincts."""
        instincts.seed_priors()
        # Create a projection + action via the engine
        engine.exec_run_projection(
            action="Resolve test bet",
            cost=5.0,
            strategy_type="kalshi",
            expected_return=10.0,
            estimated_days_to_return=3.0,
            confidence=70,
            research_summary="Test",
            assumptions=["a"],
            risks=["r"],
            bull_case="up",
            bear_case="down",
        )
        actions = instincts.load_actions()
        proj_id = actions[0]["projection_id"]

        # Resolve it
        output = engine.exec_resolve_projection(proj_id, "bet won", 12.0, 2.0)
        assert "HIT" in output
        assert "Instincts updated" in output

        # Action should be resolved
        actions = instincts.load_actions()
        assert actions[0]["status"] == "won"
        assert actions[0]["actual_return"] == 12.0

        # Instincts should have been recomputed
        inst = instincts.load_instincts()
        assert inst.get("action_count_at_compute", 0) >= 1

    def test_resolve_projection_orphan_warning(self, isolated_fs):
        """Resolving a projection with no linked action should show a warning."""
        instincts.seed_priors()
        # Create a projection directly (no linked action)
        from agent import projections
        proj = projections.create_projection(
            action="orphan test", cost=5.0, strategy_type="kalshi",
            expected_return=10.0, estimated_days=3.0, confidence=70,
            assumptions=["a"], risks=["r"], comparables="",
            bull_case="up", bear_case="down", research_summary="test",
            current_balance=100.0, operational_cost_per_cycle=0.01,
            calibration_multiplier=1.0,
        )
        output = engine.exec_resolve_projection(proj["id"], "outcome", 8.0, 2.0)
        assert "WARNING" in output
        assert "No linked action" in output

    def test_update_prior_validates_category(self, isolated_fs):
        """exec_update_prior should reject unknown categories."""
        result = engine.exec_update_prior("nonexistent_category", 0.5, 0.5)
        assert "Unknown category" in result

    def test_update_prior_updates_priors(self, isolated_fs):
        """exec_update_prior should update priors.json with validated data."""
        instincts.seed_priors()
        result = engine.exec_update_prior("kalshi", 0.60, 0.15, "real research")
        assert "Prior updated" in result
        assert "kalshi" in result

        # Verify the file was actually updated
        priors = instincts.load_priors()
        assert priors["kalshi"]["win_rate"] == 0.60
        assert priors["kalshi"]["validated"] is True

    def test_update_prior_uses_normalized_category(self, isolated_fs):
        """exec_update_prior should normalize 'product_sale' → 'product'."""
        instincts.seed_priors()
        result = engine.exec_update_prior("product_sale", 0.40, 0.30)
        assert "product" in result
        priors = instincts.load_priors()
        assert priors["product"]["win_rate"] == 0.40


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Tests for edge cases: all-losses, all-wins, zero values, etc."""

    def _seed_actions(self, isolated_fs, actions):
        instincts.save_actions(actions)

    def test_all_losses(self, isolated_fs):
        """100% loss rate should produce valid calibration clamped at 0.3."""
        instincts.seed_priors()
        actions = [
            make_action(category="kalshi", status="lost", confidence=80,
                        actual_return=0.0, cost=5.0)
            for _ in range(5)
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        cat = data["category_scores"]["kalshi"]
        assert cat["win_rate"] == 0.0
        # Calibration: 0% win / 80% conf = 0.0, clamped to 0.3
        cal = data["calibration"]["per_category"].get("kalshi")
        assert cal is not None
        assert cal == pytest.approx(0.3, abs=0.05)

    def test_all_wins(self, isolated_fs):
        """100% win rate should produce valid calibration clamped at 1.5."""
        instincts.seed_priors()
        actions = [
            make_action(category="service", status="won", confidence=40,
                        actual_return=20.0, cost=5.0)
            for _ in range(5)
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        cat = data["category_scores"]["service"]
        assert cat["win_rate"] == 1.0
        # Calibration: 100% win / 40% conf = 2.5, clamped to 1.5
        cal = data["calibration"]["per_category"].get("service")
        assert cal is not None
        assert cal == pytest.approx(1.5, abs=0.05)

    def test_single_action(self, isolated_fs):
        """Blend math at n=1 should weight prior heavily."""
        instincts.seed_priors()
        actions = [make_action(category="kalshi", status="won", confidence=70)]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        cat = data["category_scores"]["kalshi"]
        assert cat["sample_size"] == 1
        # With n=1, blend = 80% prior + 20% earned. Prior win_rate=0.52, earned=1.0
        # Blended ≈ 0.52*0.8 + 1.0*0.2 = 0.616
        # Just verify it's between prior and earned
        assert 0.5 < cat["win_rate"] <= 1.0

    def test_zero_confidence(self, isolated_fs):
        """Actions with confidence=0 should not cause division by zero in calibration."""
        instincts.seed_priors()
        actions = [
            make_action(category="kalshi", status="won", confidence=0)
            for _ in range(5)
        ]
        self._seed_actions(isolated_fs, actions)
        # Should not raise
        data = instincts.recompute_instincts()
        assert "calibration" in data

    def test_zero_cost(self, isolated_fs):
        """Actions with cost=0 should not cause division by zero in ROI."""
        instincts.seed_priors()
        actions = [
            make_action(category="content", status="won", cost=0.0,
                        actual_return=5.0, expected_return=5.0)
            for _ in range(3)
        ]
        self._seed_actions(isolated_fs, actions)
        # Should not raise
        data = instincts.recompute_instincts()
        assert "content" in data["category_scores"]

    def test_actual_time_days_zero_included(self, isolated_fs):
        """actual_time_days=0 (instant) should be included in avg_return_time, not dropped."""
        instincts.seed_priors()
        actions = [
            make_action(category="kalshi", status="won", actual_time_days=0),
            make_action(category="kalshi", status="won", actual_time_days=4.0),
        ]
        self._seed_actions(isolated_fs, actions)
        data = instincts.recompute_instincts()
        cat = data["category_scores"]["kalshi"]
        # avg of [0, 4] = 2.0, not just [4] = 4.0
        assert cat["avg_return_time_days"] == pytest.approx(2.0, abs=0.1)

    def test_negative_time_clamped(self, isolated_fs):
        """Negative actual_time_days should be clamped to 0 by resolve_action."""
        instincts.create_action(
            category="kalshi", subcategory="test",
            cost=5.0, expected_return=10.0, time_horizon_days=3.0,
            confidence=70, balance=100.0, risk_posture="aggressive",
            projection_id="neg_time",
        )
        resolved = instincts.resolve_action("neg_time", 8.0, -2.0, "won")
        assert resolved["actual_time_days"] == 0


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

class TestMigration:
    """Tests for migrate_actions.py backfill script."""

    def test_migrate_from_projections(self, isolated_fs, monkeypatch):
        """Migration should create action entries from resolved projections."""
        from agent import migrate_actions

        # Monkeypatch migrate_actions paths
        state_dir = isolated_fs / "state"
        monkeypatch.setattr(migrate_actions, "BASE_DIR", isolated_fs)
        monkeypatch.setattr(migrate_actions, "STATE_DIR", state_dir)
        monkeypatch.setattr(migrate_actions, "PROJECTIONS_FILE", state_dir / "projections.json")
        monkeypatch.setattr(migrate_actions, "LEDGER_FILE", state_dir / "ledger.json")
        monkeypatch.setattr(migrate_actions, "ACTIONS_FILE", state_dir / "actions.json")
        monkeypatch.setattr(migrate_actions, "STATE_FILE", state_dir / "agent_state.json")

        # Seed resolved projections
        resolved_proj = make_resolved_projection(hit=True, cost=10.0, actual_return=20.0)
        (state_dir / "projections.json").write_text(json.dumps([resolved_proj]))

        migrate_actions.migrate()

        actions = json.loads((state_dir / "actions.json").read_text())
        assert len(actions) == 1
        assert actions[0]["projection_id"] == resolved_proj["id"]
        assert actions[0]["status"] == "won"
        assert actions[0]["cost"] == 10.0

    def test_migrate_idempotent(self, isolated_fs, monkeypatch):
        """Running migration twice should not create duplicate actions."""
        from agent import migrate_actions

        state_dir = isolated_fs / "state"
        monkeypatch.setattr(migrate_actions, "BASE_DIR", isolated_fs)
        monkeypatch.setattr(migrate_actions, "STATE_DIR", state_dir)
        monkeypatch.setattr(migrate_actions, "PROJECTIONS_FILE", state_dir / "projections.json")
        monkeypatch.setattr(migrate_actions, "LEDGER_FILE", state_dir / "ledger.json")
        monkeypatch.setattr(migrate_actions, "ACTIONS_FILE", state_dir / "actions.json")
        monkeypatch.setattr(migrate_actions, "STATE_FILE", state_dir / "agent_state.json")

        resolved_proj = make_resolved_projection(hit=False, cost=5.0, actual_return=2.0)
        (state_dir / "projections.json").write_text(json.dumps([resolved_proj]))

        migrate_actions.migrate()
        migrate_actions.migrate()  # second run

        actions = json.loads((state_dir / "actions.json").read_text())
        assert len(actions) == 1  # no duplicates

    def test_migrate_empty(self, isolated_fs, monkeypatch):
        """Migration on empty projections should produce no actions and not crash."""
        from agent import migrate_actions

        state_dir = isolated_fs / "state"
        monkeypatch.setattr(migrate_actions, "BASE_DIR", isolated_fs)
        monkeypatch.setattr(migrate_actions, "STATE_DIR", state_dir)
        monkeypatch.setattr(migrate_actions, "PROJECTIONS_FILE", state_dir / "projections.json")
        monkeypatch.setattr(migrate_actions, "LEDGER_FILE", state_dir / "ledger.json")
        monkeypatch.setattr(migrate_actions, "ACTIONS_FILE", state_dir / "actions.json")
        monkeypatch.setattr(migrate_actions, "STATE_FILE", state_dir / "agent_state.json")

        (state_dir / "projections.json").write_text("[]")

        migrate_actions.migrate()

        actions = json.loads((state_dir / "actions.json").read_text())
        assert len(actions) == 0
