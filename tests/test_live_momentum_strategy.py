"""Golden-file regression test for LiveMomentumStrategy (Session 19a Task 4).

Locks the contract: LiveMomentumStrategy.process_tick produces a stable
sequence of (action_per_tick, log_decision_calls, state_snapshots) on
hand-crafted tick streams. The fixtures in tests/fixtures/live_momentum/
were captured by tools/regenerate_live_momentum_fixtures.py running the
new code path.

This is a self-consistency regression — it catches future drift in the
new strategy. The byte-identical-to-legacy proof comes from the manual
spec review during Session 19a planning + the broader behavior
preservation that 19b's full back-tester will exercise against real
paper trade outcomes.

To regenerate fixtures (e.g., when an intentional behavior change
lands), run:

    python3 tools/regenerate_live_momentum_fixtures.py [scenario]

Five scenarios cover the key paths:
  - take_profit:    +10c gain → TAKE_PROFIT exit
  - stop_loss:      -10c drop → STOP_LOSS exit
  - near_settle:    yes_bid >= 93c → NEAR_SETTLE exit
  - trailing_stop:  TRAIL-eligible price path that PRODUCES NO EXIT due
                    to a production peak-tracking bug preserved by 19a
                    (see scenario docstring in the regenerator tool)
  - no_exit:        oscillation that never crosses any threshold;
                    position stays open at last tick
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.strategies import Buy, Hold, Market, Sell, Tick
from bot.strategies.live_momentum import LiveMomentumStrategy

FIXTURES = Path(__file__).parent / "fixtures" / "live_momentum"

SCENARIOS = ["take_profit", "stop_loss", "near_settle", "trailing_stop", "no_exit"]


def _load_fixture(name: str) -> dict:
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        pytest.skip(f"fixture {name}.json not generated; run tools/regenerate_live_momentum_fixtures.py")
    return json.loads(path.read_text())


def _serialize_action(a) -> dict:
    if isinstance(a, Buy):
        return {"type": "Buy", "side": a.side, "qty": a.qty,
                "reason": a.reason, "ticker": a.ticker,
                "price_cents": a.price_cents}
    if isinstance(a, Sell):
        return {"type": "Sell", "side": a.side, "qty": a.qty,
                "reason": a.reason, "ticker": a.ticker,
                "exit_price": a.exit_price}
    return {"type": "Hold", "reason": a.reason}


def _replay_fixture(fixture: dict) -> dict:
    """Replay the fixture's tick stream through LiveMomentumStrategy and
    return captured (actions, log_calls, states) — same shape as the
    fixture's expected block."""
    ticks = fixture["ticks"]
    market = Market(
        ticker=ticks[0]["ticker"],
        series_ticker=ticks[0]["ticker"].split("-")[0],
        event_ticker=None, status="active",
        close_ts=ticks[0].get("close_ts"),
        yes_ask=ticks[0]["yes_ask"], yes_bid=ticks[0]["yes_bid"],
        no_ask=ticks[0]["no_ask"], no_bid=ticks[0]["no_bid"],
        volume_24h=100, open_interest=50, raw={},
    )
    s = LiveMomentumStrategy()
    state = s.init_state(
        market, sport="tennis",
        opponent_ticker=ticks[0].get("opp_ticker"),
        balance=500.0, mode="momentum", match_title="Test Match",
    )

    captured = {"actions": [], "log_calls": [], "states": []}

    def capture_log(**kw):
        captured["log_calls"].append({
            "ticker": kw.get("ticker", ""),
            "decision": kw.get("decision", ""),
            "reason": kw.get("reason", ""),
        })

    with patch("bot.decisions.log_decision", side_effect=capture_log):
        for t in ticks:
            tick = Tick(
                ts=t["ts"], ticker=t["ticker"],
                yes_bid=t["yes_bid"], yes_ask=t["yes_ask"],
                no_bid=t["no_bid"], no_ask=t["no_ask"],
                opp_ticker=t.get("opp_ticker"),
                opp_yes_bid=t.get("opp_yes_bid"),
                opp_yes_ask=t.get("opp_yes_ask"),
                opp_no_bid=t.get("opp_no_bid"),
                opp_no_ask=t.get("opp_no_ask"),
                wp=t.get("wp"), score_diff=t.get("score_diff"),
                period=t.get("period"), espn_data=t.get("espn_data"),
                close_ts=t.get("close_ts"),
                raw=t.get("raw", {}), raw_opp=t.get("raw_opp", {}),
            )
            state, action = s.process_tick(state, tick)
            captured["actions"].append(_serialize_action(action))
            captured["states"].append({
                "bets_placed_count": len(state.data["bets_placed"]),
                "entry_count": state.data["entry_count"],
                "cooldown_remaining": state.data["cooldown_remaining"],
                "peak_values": dict(state.data["peak_values"]),
            })
    return captured


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_action_sequence_matches_fixture(scenario: str) -> None:
    fixture = _load_fixture(scenario)
    actual = _replay_fixture(fixture)
    expected = fixture["expected"]["actions"]
    assert actual["actions"] == expected, (
        f"Action sequence drift in {scenario}:\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual['actions']}"
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_state_snapshots_match_fixture(scenario: str) -> None:
    fixture = _load_fixture(scenario)
    actual = _replay_fixture(fixture)
    expected = fixture["expected"]["states"]
    assert actual["states"] == expected, (
        f"State drift in {scenario}: first divergence shown.\n"
        f"  expected[0]: {expected[0]}\n"
        f"  actual[0]:   {actual['states'][0]}"
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_log_decision_calls_match_fixture(scenario: str) -> None:
    fixture = _load_fixture(scenario)
    actual = _replay_fixture(fixture)
    expected = fixture["expected"]["log_calls"]
    assert actual["log_calls"] == expected, (
        f"log_decision call sequence drift in {scenario}:\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual['log_calls']}"
    )


# ---------------------------------------------------------------------------
# Targeted sanity checks — assert the SCENARIO INTENT, not just fixture parity.
# These catch silent bugs where the fixture happens to be wrong (e.g., if
# the regenerator was run against broken code).
# ---------------------------------------------------------------------------

def test_take_profit_actually_exits_with_take_profit_reason():
    fixture = _load_fixture("take_profit")
    actions = fixture["expected"]["actions"]
    sells = [a for a in actions if a["type"] == "Sell"]
    assert len(sells) == 1, f"expected 1 Sell, got {len(sells)}"
    assert sells[0]["reason"].startswith("TAKE PROFIT:"), sells[0]["reason"]


def test_stop_loss_actually_exits_with_stop_loss_reason():
    fixture = _load_fixture("stop_loss")
    actions = fixture["expected"]["actions"]
    sells = [a for a in actions if a["type"] == "Sell"]
    assert len(sells) == 1
    assert sells[0]["reason"].startswith("STOP-LOSS:"), sells[0]["reason"]


def test_near_settle_actually_exits_with_near_settle_reason():
    fixture = _load_fixture("near_settle")
    actions = fixture["expected"]["actions"]
    sells = [a for a in actions if a["type"] == "Sell"]
    assert len(sells) == 1
    assert sells[0]["reason"].startswith("NEAR-SETTLE:"), sells[0]["reason"]


def test_trailing_stop_does_NOT_exit_due_to_production_peak_tracking_bug():
    """Production peak_values[ticker] is never written on the first call
    because `prev_peak = peak_values.get(ticker, current_value)` defaults
    to current_value, then the strict `>` comparison fails. Result:
    drop_from_peak is always 0, TRAILING_STOP never fires.

    This scenario documents the bug. Session 19a preserves it faithfully
    per behavior-preservation discipline. Fixing the bug is a separate
    follow-up tracked in the commit message and CLAUDE.md."""
    fixture = _load_fixture("trailing_stop")
    actions = fixture["expected"]["actions"]
    sells = [a for a in actions if a["type"] == "Sell"]
    assert len(sells) == 0, (
        "TRAILING_STOP unexpectedly fired — the production peak-tracking "
        "bug is fixed? If so, update this test and the regenerator's "
        "trailing_stop docstring. Current Sell actions: " + str(sells)
    )
    # Position stayed open
    final_state = fixture["expected"]["states"][-1]
    assert final_state["bets_placed_count"] == 1


def test_no_exit_position_stays_open():
    fixture = _load_fixture("no_exit")
    actions = fixture["expected"]["actions"]
    sells = [a for a in actions if a["type"] == "Sell"]
    assert len(sells) == 0
    final_state = fixture["expected"]["states"][-1]
    assert final_state["bets_placed_count"] == 1
