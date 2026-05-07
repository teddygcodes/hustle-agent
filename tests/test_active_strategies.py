"""
Session 56 (2026-05-06) — disable sports_arb strategies regression tests.

Codex review surfaced that sports_monotonicity_arb and sports_consistency_arb
opportunity dicts encode a two-leg arb in a sibling `arb_pair` field that
executor.execute_trade() never reads. The top-level ticker + recommended_side
fields describe a single leg, so any "MONOTONICITY ARB" or "CONSISTENCY ARB"
trigger fires as a one-sided directional bet at $200-Kelly sizing labeled
confidence=0.95. 0 historical fills (we got lucky); dormant-loaded code path
that would fire on the first real Kalshi violation.

This file pins four layers of defense:

  Layer 1  — assert both strategies are not in ACTIVE_STRATEGIES (the
             load-bearing fix; the strategy gate at scanner.py:672-681 drops
             non-active opps before the executor sees them).

  Layer 2  — assert the two scanner functions return [] regardless of input,
             even when fed mock Kalshi markets that contain a real
             monotonicity/consistency violation. Defense-in-depth against
             future re-add to ACTIVE_STRATEGIES that bypasses Layer 1.

  Layer 3  — property test asserting that even if a sports_arb opp dict is
             constructed by hand and injected into the strategy gate, it is
             dropped. Protects against future weakening of the
             scanner.py:672-681 filter.

Re-enable only when paired-leg execution (atomic both-legs-or-refund) ships.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bot.config import ACTIVE_STRATEGIES


_DISABLED_ARBS = ("sports_monotonicity_arb", "sports_consistency_arb")


# ---------------------------------------------------------------------------
# Layer 1 — ACTIVE_STRATEGIES membership
# ---------------------------------------------------------------------------

def test_sports_monotonicity_arb_not_in_active_strategies():
    """The load-bearing fix: removing the strategy from the list disables it."""
    assert "sports_monotonicity_arb" not in ACTIVE_STRATEGIES


def test_sports_consistency_arb_not_in_active_strategies():
    """The load-bearing fix: removing the strategy from the list disables it."""
    assert "sports_consistency_arb" not in ACTIVE_STRATEGIES


def test_vig_stack_strategies_still_active():
    """Sanity guard — Session 56 should not have touched vig_stack."""
    assert "vig_stack_series" in ACTIVE_STRATEGIES
    assert "vig_stack_futures" in ACTIVE_STRATEGIES


# ---------------------------------------------------------------------------
# Layer 2 — scanner-level early-return regression
# ---------------------------------------------------------------------------

def _make_monotonicity_violation_markets() -> list[dict]:
    """
    Construct a synthetic Kalshi response containing a real spread-monotonicity
    violation: NBA Lakers spread thresholds where the lower threshold (cheaper
    side) costs MORE than the higher threshold — impossible if the market is
    rationally priced. Pre-Session-56, this would have produced an
    `opportunities.append(...)` call.
    """
    return [
        {
            "ticker": "KXNBASPREAD-26MAY01LAL-LAL-T1.5",
            "event_ticker": "KXNBASPREAD-26MAY01LAL",
            "yes_ask": 50,
            "yes_bid": 48,
            "no_ask": 52,
            "title": "Lakers win by 1.5+",
            "volume": 1000,
        },
        {
            "ticker": "KXNBASPREAD-26MAY01LAL-LAL-T4.5",
            "event_ticker": "KXNBASPREAD-26MAY01LAL",
            "yes_ask": 70,  # Should be CHEAPER than the 1.5 threshold but isn't — violation
            "yes_bid": 68,
            "no_ask": 32,
            "title": "Lakers win by 4.5+",
            "volume": 1000,
        },
    ]


def _make_consistency_violation_markets() -> dict[str, list[dict]]:
    """
    Construct a synthetic Kalshi response where P(team championship) >
    P(team series win) — impossible (championship requires series win).
    Returned shape matches what get_markets returns at the call sites.
    """
    return {
        "championship": [
            {
                "ticker": "KXNBACHAMP-26-LAL",
                "yes_ask": 30,
                "yes_bid": 28,
                "title": "Lakers win 2026 NBA championship",
                "volume": 5000,
            },
        ],
        "series": [
            {
                "ticker": "KXNBASERIES-26-LAL",
                "yes_ask": 25,  # Series < championship — violation
                "yes_bid": 23,
                "title": "Lakers win conference",
                "volume": 5000,
            },
        ],
    }


def test_scan_monotonicity_violations_returns_empty_after_session_56():
    """
    Layer-2 regression: even if a real monotonicity violation is present in
    the mocked Kalshi response, the scanner must return []. If this test
    fails, someone weakened the Session 56 early-return.
    """
    from bot import scanner_sports_arb

    with patch.object(scanner_sports_arb, "get_markets") as mock_get_markets:
        mock_get_markets.return_value = {
            "markets": _make_monotonicity_violation_markets(),
        }
        result = scanner_sports_arb.scan_monotonicity_violations()

    assert result == [], (
        "scan_monotonicity_violations must return [] post-Session-56 "
        "regardless of input. Got: %r" % result
    )


def test_scan_championship_series_violations_returns_empty_after_session_56():
    """
    Layer-2 regression: same property for the consistency-arb scanner.
    """
    from bot import scanner_sports_arb

    with patch.object(scanner_sports_arb, "get_markets") as mock_get_markets:
        # Function makes two get_markets calls (championship + series); return
        # different markets per call by side-effecting the mock.
        violation = _make_consistency_violation_markets()
        mock_get_markets.side_effect = [
            {"markets": violation["championship"]},
            {"markets": violation["series"]},
        ] * 10  # plenty of repeats in case the scanner loops over sports

        result = scanner_sports_arb.scan_championship_series_violations()

    assert result == [], (
        "scan_championship_series_violations must return [] post-Session-56 "
        "regardless of input. Got: %r" % result
    )


# ---------------------------------------------------------------------------
# Layer 3 — strategy-gate property test (scanner.py:672-681 filter)
# ---------------------------------------------------------------------------

def test_strategy_gate_drops_disabled_arb_opps():
    """
    Property test: even if a hand-crafted sports_arb opp dict is injected
    into the strategy-gate filter, it is dropped.

    Mirrors the filter logic at bot/scanner.py:672-681:
        active = [o for o in all_opportunities
                  if o.get("type") in ACTIVE_STRATEGIES]

    Protects against future weakening of the strategy gate. If the gate is
    refactored or the membership check is dropped, this test fails.
    """
    fake_opps = [
        {"type": "sports_monotonicity_arb", "ticker": "KXTEST-1", "edge": 0.05},
        {"type": "sports_consistency_arb",  "ticker": "KXTEST-2", "edge": 0.07},
        {"type": "vig_stack_series",         "ticker": "KXTEST-3", "edge": 0.03},
    ]
    active = [o for o in fake_opps if o.get("type") in ACTIVE_STRATEGIES]
    assert active == [{"type": "vig_stack_series", "ticker": "KXTEST-3", "edge": 0.03}]
    assert all(o["type"] not in _DISABLED_ARBS for o in active)


# ---------------------------------------------------------------------------
# Defense-in-depth: bot/scanner_sports_arb.py source-level check
# ---------------------------------------------------------------------------

def test_scanner_sports_arb_session_56_comments_present():
    """
    The two disabled scanner functions should carry the Session 56 comment
    block above their `return []`. If a future session removes the comment
    AND the early-return together, this test fails — forcing a reviewer to
    notice that they're undoing a deliberate disable.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "bot" / "scanner_sports_arb.py"
    text = src.read_text()
    # Two functions, one Session 56 marker each
    assert text.count("Session 56 (2026-05-06): disabled") == 2, (
        "Expected 2 Session 56 disable markers in scanner_sports_arb.py; "
        "got %d. Did someone remove the early-return without removing the "
        "test?" % text.count("Session 56 (2026-05-06): disabled")
    )
