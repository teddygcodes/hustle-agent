"""Direct unit tests for LiveMomentumStrategy helpers (Session 19a I-1).

Caught at code review of part 2a: helpers carry non-trivial logic
(f-string formatting in _variance_quality_ok, conditional close_ts
merging in _log_decision_dampened) that would silently fail without
direct tests. The Task 4 golden-file regression on process_tick
covers exit/entry decision flow, but doesn't necessarily exercise
every helper edge case. These tests close that gap."""
from __future__ import annotations

from collections import deque
from unittest.mock import patch

import pytest

from bot.strategies import State
from bot.strategies.live_momentum import LiveMomentumStrategy


def test_variance_quality_thin_history_returns_true_not_enough_history():
    """Production benefit-of-the-doubt: thin history → True (allow entry)."""
    s = LiveMomentumStrategy()
    history = deque([70, 71], maxlen=12)
    ok, reason = s._variance_quality_ok(history)
    assert ok is True
    assert reason == "not_enough_history"


def test_variance_quality_flat_returns_false_with_formatted_reason():
    """Flat market → False with f-string reason embedding range and ticks."""
    s = LiveMomentumStrategy()
    # 12 ticks all at 70 → range=0, fails the TENNIS_QUALITY_MIN_RANGE check
    history = deque([70] * 12, maxlen=12)
    ok, reason = s._variance_quality_ok(history)
    assert ok is False
    # Reason format: f"flat_{price_range}c_range_over_{TENNIS_QUALITY_MIN_TICKS}ticks"
    assert reason.startswith("flat_0c_range_over_")
    assert "ticks" in reason


def test_variance_quality_varied_returns_true_ok():
    """Varied prices → True, "ok"."""
    s = LiveMomentumStrategy()
    history = deque([60, 65, 70, 72, 68, 75, 78, 80, 76, 74, 71, 73], maxlen=12)
    ok, reason = s._variance_quality_ok(history)
    assert ok is True
    assert reason == "ok"


def test_log_decision_dampener_suppresses_duplicates():
    """Same (decision, reason) emitted twice → only one log_decision call."""
    s = LiveMomentumStrategy()
    state = State()
    state.data["last_decision"] = None
    state.data["ticker"] = "X"
    calls = []
    with patch("bot.decisions.log_decision", side_effect=lambda **kw: calls.append(kw)):
        s._log_decision_dampened(state, decision="reject", reason="cooldown",
                                  gates={"x": False})
        s._log_decision_dampened(state, decision="reject", reason="cooldown",
                                  gates={"x": False})  # dampened
        s._log_decision_dampened(state, decision="reject", reason="position_open",
                                  gates={"x": False})  # different reason → new call
    assert len(calls) == 2
    assert calls[0]["reason"] == "cooldown"
    assert calls[1]["reason"] == "position_open"


def test_log_decision_dampener_close_ts_merged_into_extra():
    """close_ts kwarg merges into extra; explicit extra.close_ts wins."""
    s = LiveMomentumStrategy()
    state = State()
    state.data["last_decision"] = None
    state.data["ticker"] = "X"

    # Case 1: kwarg close_ts merged into None extra
    calls = []
    with patch("bot.decisions.log_decision", side_effect=lambda **kw: calls.append(kw)):
        s._log_decision_dampened(state, decision="accept", reason="dip_buy",
                                  gates={}, close_ts="2026-04-26T00:00:00Z")
    assert calls[0]["extra"]["close_ts"] == "2026-04-26T00:00:00Z"

    # Case 2: explicit close_ts in extra wins over kwarg
    state.data["last_decision"] = None  # reset dampener
    calls = []
    with patch("bot.decisions.log_decision", side_effect=lambda **kw: calls.append(kw)):
        s._log_decision_dampened(state, decision="accept", reason="conviction",
                                  gates={},
                                  extra={"close_ts": "EXPLICIT-WINS"},
                                  close_ts="KWARG-LOSES")
    assert calls[0]["extra"]["close_ts"] == "EXPLICIT-WINS"
