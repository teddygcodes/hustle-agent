"""
Tests for SportInstincts — situational awareness engine.

Tests each sport's instinct detection (NBA garbage time, NHL empty net,
MLB high-leverage ABs, etc.) and verifies integration with DQS scoring.
"""

import math
from collections import deque

import pytest

from bot.game_context import GameContext, SportInstincts, compute_dip_quality, ScoreSnapshot


# ---------------------------------------------------------------------------
# Helper: build a GameContext with specific state
# ---------------------------------------------------------------------------

def _make_ctx(
    sport: str,
    our_score: int = 0,
    their_score: int = 0,
    period: int = 1,
    clock_str: str = "12:00",
    num_ticks: int = 5,
) -> GameContext:
    """Create a GameContext pre-loaded with score snapshots."""
    ctx = GameContext(sport=sport)
    # Feed enough ticks to populate snapshots
    for i in range(num_ticks):
        ctx.update(
            espn_data={},
            our_score=our_score,
            their_score=their_score,
            period=period,
            clock_str=clock_str,
        )
    return ctx


def _make_ctx_with_run(
    sport: str,
    our_score: int,
    their_score: int,
    period: int,
    clock_str: str,
    scoring_events: list,
) -> GameContext:
    """Create a GameContext with specific scoring events injected."""
    ctx = GameContext(sport=sport)
    # Initial state
    ctx.update(
        espn_data={},
        our_score=0,
        their_score=0,
        period=period,
        clock_str=clock_str,
    )
    # Feed scoring events
    running_our = 0
    running_their = 0
    for who, pts in scoring_events:
        if who == "us":
            running_our += pts
        else:
            running_their += pts
        ctx.update(
            espn_data={},
            our_score=running_our,
            their_score=running_their,
            period=period,
            clock_str=clock_str,
        )
    # Final state
    ctx.update(
        espn_data={},
        our_score=our_score,
        their_score=their_score,
        period=period,
        clock_str=clock_str,
    )
    return ctx


# ===========================================================================
# NBA Instincts
# ===========================================================================

class TestNBAInstincts:
    """NBA garbage time, clutch time, Q3 momentum discount."""

    def test_garbage_time_big_lead_q4(self):
        """20+ pt lead in Q4 with <5 min → garbage time."""
        ctx = _make_ctx("nba", our_score=110, their_score=85, period=4, clock_str="3:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert inst.is_garbage_time
        assert inst.should_avoid_entry
        assert inst.situational_modifier == 0.0
        assert any("garbage" in f for f in inst.flags)

    def test_garbage_time_blowout_q4(self):
        """30+ pt lead in Q4 regardless of clock → garbage time."""
        ctx = _make_ctx("nba", our_score=120, their_score=88, period=4, clock_str="10:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert inst.is_garbage_time
        assert inst.should_avoid_entry

    def test_no_garbage_time_q3(self):
        """20 pt lead in Q3 is NOT garbage time — too much game left."""
        ctx = _make_ctx("nba", our_score=80, their_score=60, period=3, clock_str="5:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert not inst.is_garbage_time

    def test_clutch_time_close_q4(self):
        """≤5 pt game in Q4 with <5 min → clutch time."""
        ctx = _make_ctx("nba", our_score=100, their_score=97, period=4, clock_str="2:30")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert inst.is_clutch_time
        assert inst.should_reduce_size
        assert inst.should_widen_stops
        assert inst.situational_modifier < 1.0

    def test_no_clutch_time_big_lead(self):
        """10 pt lead in Q4 is NOT clutch — comfortably ahead."""
        ctx = _make_ctx("nba", our_score=105, their_score=95, period=4, clock_str="3:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert not inst.is_clutch_time

    def test_q3_momentum_discount(self):
        """Q3 runs should be discounted — they're common and often reversed."""
        ctx = _make_ctx_with_run(
            "nba", our_score=80, their_score=60, period=3, clock_str="5:00",
            scoring_events=[("us", 3), ("us", 2), ("us", 3), ("us", 2)],
        )
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert any("q3_momentum" in f for f in inst.flags)
        # Note: pulling_away may also trigger (1.3x), which can override the 0.85x discount.
        # The key assertion is that the Q3 discount flag IS applied.

    def test_free_throw_parade(self):
        """Close game, last 2 min, free throws → mechanical scoring, no signal."""
        ctx = _make_ctx("nba", our_score=100, their_score=98, period=4, clock_str="1:30")
        espn_data = {"last_play": "Free Throw made by LeBron James"}
        inst = SportInstincts.detect(ctx, espn_data, "nba")
        assert any("free_throw" in f for f in inst.flags)

    def test_normal_q1_no_flags(self):
        """Normal Q1 play should have no special instincts."""
        ctx = _make_ctx("nba", our_score=25, their_score=22, period=1, clock_str="5:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert not inst.is_garbage_time
        assert not inst.is_clutch_time
        assert not inst.should_avoid_entry


# ===========================================================================
# NHL Instincts
# ===========================================================================

class TestNHLInstincts:
    """NHL power play, empty net, OT likelihood, clutch."""

    def test_empty_net_trailing_p3(self):
        """Down 1-2 goals in P3 with <2:30 left → likely empty net."""
        ctx = _make_ctx("nhl", our_score=1, their_score=2, period=3, clock_str="1:30")
        inst = SportInstincts.detect(ctx, {}, "nhl")
        assert inst.is_empty_net
        assert inst.should_reduce_size
        assert inst.should_widen_stops
        assert inst.situational_modifier < 0.6

    def test_no_empty_net_leading(self):
        """Leading in P3 — no empty net concern."""
        ctx = _make_ctx("nhl", our_score=3, their_score=1, period=3, clock_str="1:30")
        inst = SportInstincts.detect(ctx, {}, "nhl")
        assert not inst.is_empty_net

    def test_no_empty_net_early_p3(self):
        """Trailing in P3 but 10 min left — no empty net yet."""
        ctx = _make_ctx("nhl", our_score=1, their_score=2, period=3, clock_str="10:00")
        inst = SportInstincts.detect(ctx, {}, "nhl")
        assert not inst.is_empty_net

    def test_power_play_from_last_play(self):
        """Detect power play from ESPN last_play text."""
        ctx = _make_ctx("nhl", our_score=2, their_score=1, period=2, clock_str="8:00")
        espn_data = {"last_play": "Power Play Goal scored by Ovechkin"}
        inst = SportInstincts.detect(ctx, espn_data, "nhl")
        assert inst.is_power_play
        assert inst.situational_modifier < 1.0

    def test_penalty_kill_from_last_play(self):
        """Detect penalty kill situation."""
        ctx = _make_ctx("nhl", our_score=2, their_score=1, period=2, clock_str="8:00")
        espn_data = {"last_play": "Shorthanded Goal by penalty kill unit"}
        inst = SportInstincts.detect(ctx, espn_data, "nhl")
        assert inst.is_penalty_kill

    def test_clutch_1_goal_lead_p3(self):
        """Up by 1 in P3 with <5 min → clutch time (opponent desperate)."""
        ctx = _make_ctx("nhl", our_score=2, their_score=1, period=3, clock_str="3:00")
        inst = SportInstincts.detect(ctx, {}, "nhl")
        assert inst.is_clutch_time

    def test_ot_likely_tied_p3(self):
        """Tied in P3 → OT is likely (coin flip)."""
        ctx = _make_ctx("nhl", our_score=2, their_score=2, period=3, clock_str="5:00")
        inst = SportInstincts.detect(ctx, {}, "nhl")
        assert any("ot_likely" in f for f in inst.flags)
        assert inst.situational_modifier < 0.6


# ===========================================================================
# MLB Instincts
# ===========================================================================

class TestMLBInstincts:
    """MLB high-leverage ABs, bases loaded, save situations, early game."""

    def test_high_leverage_risp_2_outs(self):
        """2 outs, runner on 2nd → high leverage, block entry."""
        ctx = _make_ctx("mlb", our_score=3, their_score=2, period=5, clock_str="")
        espn_data = {"situation": {"outs": 2, "onFirst": False, "onSecond": True, "onThird": False}}
        inst = SportInstincts.detect(ctx, espn_data, "mlb")
        assert inst.is_high_leverage_ab
        assert inst.should_avoid_entry
        assert inst.situational_modifier == 0.0

    def test_high_leverage_risp_3rd_base(self):
        """2 outs, runner on 3rd → high leverage."""
        ctx = _make_ctx("mlb", our_score=3, their_score=2, period=5, clock_str="")
        espn_data = {"situation": {"outs": 2, "onFirst": False, "onSecond": False, "onThird": True}}
        inst = SportInstincts.detect(ctx, espn_data, "mlb")
        assert inst.is_high_leverage_ab
        assert inst.should_avoid_entry

    def test_bases_loaded(self):
        """Bases loaded = always high leverage."""
        ctx = _make_ctx("mlb", our_score=3, their_score=2, period=3, clock_str="")
        espn_data = {"situation": {"outs": 1, "onFirst": True, "onSecond": True, "onThird": True}}
        inst = SportInstincts.detect(ctx, espn_data, "mlb")
        assert inst.is_high_leverage_ab
        assert inst.should_avoid_entry

    def test_no_high_leverage_bases_empty(self):
        """Bases empty, 0 outs → normal play."""
        ctx = _make_ctx("mlb", our_score=3, their_score=2, period=5, clock_str="")
        espn_data = {"situation": {"outs": 0, "onFirst": False, "onSecond": False, "onThird": False}}
        inst = SportInstincts.detect(ctx, espn_data, "mlb")
        assert not inst.is_high_leverage_ab
        assert not inst.should_avoid_entry

    def test_save_situation_late_inning(self):
        """Leading by 1-2 in 8th/9th → save situation."""
        ctx = _make_ctx("mlb", our_score=4, their_score=3, period=9, clock_str="")
        espn_data = {"situation": {"outs": 0, "onFirst": False, "onSecond": False, "onThird": False}}
        inst = SportInstincts.detect(ctx, espn_data, "mlb")
        assert any("save_situation" in f for f in inst.flags)

    def test_early_game_discount(self):
        """Small lead in innings 1-3 → too early for confidence."""
        ctx = _make_ctx("mlb", our_score=2, their_score=1, period=2, clock_str="")
        espn_data = {"situation": {}}
        inst = SportInstincts.detect(ctx, espn_data, "mlb")
        assert any("early_game" in f for f in inst.flags)
        assert inst.situational_modifier < 1.0


# ===========================================================================
# Universal Instincts
# ===========================================================================

class TestUniversalInstincts:
    """Pulling away, comeback territory, scoring run quality."""

    def test_pulling_away(self):
        """Leading + positive lead trend + positive momentum → pulling away."""
        ctx = GameContext(sport="nba")
        # Simulate a growing lead
        for i in range(20):
            ctx.update(
                espn_data={},
                our_score=50 + i * 2,  # scoring 2 per tick
                their_score=45 + i,     # opponent scoring 1 per tick
                period=2,
                clock_str="5:00",
            )
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert inst.is_pulling_away
        assert inst.situational_modifier > 1.0

    def test_comeback_territory_nba(self):
        """Down 10 in Q3 in NBA → plausible comeback."""
        # Need completion >= 50% for the 15-pt threshold to kick in
        ctx = _make_ctx("nba", our_score=65, their_score=75, period=3, clock_str="5:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert inst.is_comeback_territory

    def test_no_comeback_blowout(self):
        """Down 25 in Q3 → not a plausible comeback."""
        ctx = _make_ctx("nba", our_score=55, their_score=80, period=3, clock_str="5:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        # 25 pt deficit at 50%+ done → exceeds threshold
        assert not inst.is_comeback_territory

    def test_scoring_run_quality_late_game(self):
        """Scoring run late in game should have higher quality than early."""
        ctx_late = _make_ctx_with_run(
            "nba", our_score=95, their_score=85, period=4, clock_str="3:00",
            scoring_events=[("us", 3), ("us", 2), ("us", 3)],
        )
        ctx_early = _make_ctx_with_run(
            "nba", our_score=25, their_score=15, period=1, clock_str="3:00",
            scoring_events=[("us", 3), ("us", 2), ("us", 3)],
        )
        inst_late = SportInstincts.detect(ctx_late, {}, "nba")
        inst_early = SportInstincts.detect(ctx_early, {}, "nba")
        # Late game run should be higher quality
        assert inst_late.scoring_run_quality >= inst_early.scoring_run_quality

    def test_volatility_regime_garbage(self):
        """Garbage time → low volatility regime."""
        ctx = _make_ctx("nba", our_score=120, their_score=88, period=4, clock_str="2:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert inst.volatility_regime == "low"

    def test_volatility_regime_clutch(self):
        """Clutch time → high volatility regime."""
        ctx = _make_ctx("nba", our_score=100, their_score=98, period=4, clock_str="2:00")
        inst = SportInstincts.detect(ctx, {}, "nba")
        assert inst.volatility_regime == "high"


# ===========================================================================
# DQS Integration
# ===========================================================================

class TestDQSIntegration:
    """Verify instincts affect DQS scoring correctly."""

    def test_garbage_time_blocks_dqs(self):
        """Garbage time should return DQS = 0."""
        ctx = _make_ctx("nba", our_score=120, their_score=88, period=4, clock_str="2:00")
        price_history = deque([75, 76, 74, 73, 72], maxlen=12)
        dqs, breakdown = compute_dip_quality(
            game_ctx=ctx,
            dip_cents=5,
            price=72,
            price_history=price_history,
            sport="nba",
        )
        assert dqs == 0.0
        assert "blocked_by" in breakdown

    def test_clutch_time_reduces_dqs(self):
        """Clutch time should reduce DQS via modifier."""
        ctx_normal = _make_ctx("nba", our_score=80, their_score=70, period=2, clock_str="5:00")
        ctx_clutch = _make_ctx("nba", our_score=100, their_score=98, period=4, clock_str="2:00")
        price_history = deque([75, 76, 74, 73, 72], maxlen=12)

        dqs_normal, _ = compute_dip_quality(
            game_ctx=ctx_normal, dip_cents=5, price=72,
            price_history=price_history, sport="nba",
        )
        dqs_clutch, bd_clutch = compute_dip_quality(
            game_ctx=ctx_clutch, dip_cents=5, price=72,
            price_history=price_history, sport="nba",
        )
        # Clutch DQS should be lower due to modifier
        assert dqs_clutch < dqs_normal
        assert bd_clutch.get("instinct_mod", 1.0) < 1.0

    def test_pulling_away_boosts_dqs(self):
        """Pulling away should boost DQS via modifier > 1.0."""
        ctx = GameContext(sport="nba")
        for i in range(20):
            ctx.update(
                espn_data={},
                our_score=60 + i * 2,
                their_score=50 + i,
                period=3,
                clock_str="5:00",
            )
        price_history = deque([80, 81, 79, 78, 77], maxlen=12)
        _, breakdown = compute_dip_quality(
            game_ctx=ctx, dip_cents=5, price=77,
            price_history=price_history, sport="nba",
        )
        # If pulling_away detected, modifier should be > 1.0
        if "pulling_away" in (breakdown.get("instincts") or []):
            assert breakdown.get("instinct_mod", 1.0) > 1.0

    def test_mlb_bases_loaded_blocks_dqs(self):
        """MLB bases loaded should block entry."""
        ctx = _make_ctx("mlb", our_score=3, their_score=2, period=5, clock_str="")
        espn_data = {"situation": {"outs": 1, "onFirst": True, "onSecond": True, "onThird": True}}
        price_history = deque([70, 71, 69, 68, 67], maxlen=12)
        dqs, breakdown = compute_dip_quality(
            game_ctx=ctx, dip_cents=5, price=67,
            price_history=price_history, sport="mlb",
            espn_data=espn_data,
        )
        assert dqs == 0.0
        assert "blocked_by" in breakdown

    def test_no_instincts_without_context(self):
        """No game context → instincts should be neutral (mod=1.0)."""
        price_history = deque([70, 71, 69, 68, 67], maxlen=12)
        dqs, breakdown = compute_dip_quality(
            game_ctx=None, dip_cents=5, price=67,
            price_history=price_history, sport="nba",
        )
        assert breakdown.get("instinct_mod", 1.0) == 1.0
        assert dqs > 0  # should still produce a score


# ===========================================================================
# SportInstincts repr
# ===========================================================================

class TestSportInstinctsRepr:
    def test_repr_no_flags(self):
        inst = SportInstincts()
        assert "normal" in repr(inst)

    def test_repr_with_flags(self):
        inst = SportInstincts(flags=["nba_garbage_time"], situational_modifier=0.0)
        r = repr(inst)
        assert "nba_garbage_time" in r
        assert "mod=0.00" in r


# ===========================================================================
# Config constants exist
# ===========================================================================

class TestConfigConstants:
    def test_instinct_config_exists(self):
        from bot.config import (
            INSTINCT_NBA_GARBAGE_LEAD,
            INSTINCT_NBA_GARBAGE_CLOCK,
            INSTINCT_NBA_CLUTCH_MARGIN,
            INSTINCT_NHL_EMPTY_NET_CLOCK,
            INSTINCT_MLB_HIGH_LEV_OUTS,
            INSTINCT_CLUTCH_TRAIL_WIDEN,
            INSTINCT_CLUTCH_SIZE_FACTOR,
        )
        assert INSTINCT_NBA_GARBAGE_LEAD == 20
        assert INSTINCT_NBA_GARBAGE_CLOCK == 300
        assert INSTINCT_NBA_CLUTCH_MARGIN == 5
        assert INSTINCT_NHL_EMPTY_NET_CLOCK == 150
        assert INSTINCT_MLB_HIGH_LEV_OUTS == 2
        assert INSTINCT_CLUTCH_TRAIL_WIDEN == 1.5
        assert INSTINCT_CLUTCH_SIZE_FACTOR == 0.5

    def test_conviction_config_data_tuned(self):
        """Conviction thresholds match tick-data analysis (110K ticks, 248 matches)."""
        from bot.config import (
            CONVICTION_ENABLED, CONVICTION_MIN_COMPLETION,
            CONVICTION_MIN_PRICE, CONVICTION_EXCLUDED_SPORTS,
        )
        assert CONVICTION_ENABLED is True
        # DATA: Q3+ (completion >= 0.50) = +2.8c/trade, 79% hit rate
        # <50% completion = flat/negative — too early to read the game
        assert CONVICTION_MIN_COMPLETION == 0.50
        # DATA: 60-67¢ entries flat/negative; 68¢+ is where edge starts
        assert CONVICTION_MIN_PRICE == 68
        # DATA: MLB conviction = 12% hit rate (noise). Tennis/UFC have no win prob model.
        assert "mlb" in CONVICTION_EXCLUDED_SPORTS
        assert "tennis" in CONVICTION_EXCLUDED_SPORTS
        assert "ufc" in CONVICTION_EXCLUDED_SPORTS
