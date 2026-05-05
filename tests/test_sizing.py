"""Tests for bot/sizing.py — Kelly sizing with per-sport size_multiplier.

Session 49 (May 1, 2026 — per-sport size_multiplier on live_momentum):
adds an optional `sport` kwarg to `kelly_size()` that scales fractional
Kelly by `SPORT_PROFILES[sport]["size_multiplier"]` (default 1.0). NBA
and UFC ship at 0.5x (bleed cohort cuts); NHL/MLB explicit at 1.0x;
all other sports + sport=None default to 1.0 — making vig_stack and
other strategies byte-identical pre/post Session 49.

Pre-Session-49 there were no tests for kelly_size in this repo. These
seven cases lock the post-49 contract and the pre-49 default-path
behavior simultaneously.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.sizing import kelly_size  # noqa: E402


# ---------------------------------------------------------------------------
# Scenarios chosen so the multiplier is visible in contract count — i.e.
# the dynamic ceiling ($200 cap) and dollar floor ($1.00) DO NOT bind.
# Walked by hand:
#   price_cents=70, probability=0.75, balance=500, edge=0.04, confidence=0.80
#   b = 1/0.70 - 1 = 0.4286
#   full_kelly = (b*0.75 - 0.25) / b ≈ 0.1667 (16.67%)
#   fractional (1.0x) = 0.1667 * 0.25 = 0.0417  → under 0.05 cap, multiplier visible
#   fractional (0.5x) = 0.1667 * 0.25 * 0.5 = 0.0208
#   risk_dollars (1.0x) = 500 * 0.0417 ≈ 20.83  → ceiling=min(25, 200)=25, floor=1.0, both miss
#   risk_dollars (0.5x) = 500 * 0.0208 ≈ 10.42  → ceiling=25, floor=1.0, both miss
#   contracts (1.0x) = floor(20.83/0.70) = 29
#   contracts (0.5x) = floor(10.42/0.70) = 14   ← ~half (off by 1 due to math.floor)
# ---------------------------------------------------------------------------
BASE_SCENARIO = dict(
    edge=0.04,
    probability=0.75,
    balance=500.0,
    price_cents=70,
    confidence=0.80,
)


def test_kelly_size_default_sport_none_unchanged():
    """Calling kelly_size without `sport` (the pre-Session-49 call shape)
    produces the same output as explicit sport=None. Locks the
    default-path-byte-identical contract."""
    no_kwarg = kelly_size(**BASE_SCENARIO)
    explicit_none = kelly_size(**BASE_SCENARIO, sport=None)
    assert no_kwarg == explicit_none
    # Sanity: this scenario actually sizes (not a no-op).
    assert no_kwarg["contracts"] > 0
    assert no_kwarg["reason"] == "sized"


def test_kelly_size_sport_unknown_defaults_to_one():
    """An unrecognized sport string resolves multiplier to 1.0 via the
    SPORT_PROFILES.get(..., {}).get(..., 1.0) fallback chain."""
    baseline = kelly_size(**BASE_SCENARIO, sport=None)
    unknown = kelly_size(**BASE_SCENARIO, sport="not_a_real_sport")
    assert unknown == baseline


def test_kelly_size_nba_halves_contracts():
    """sport='nba' (multiplier 0.5) produces ~half the contracts of
    sport='nhl' (multiplier 1.0) on the same scenario.

    Tolerance: ±1 contract due to `math.floor` rounding in the contract
    calculation. We assert nba_contracts is in [floor(nhl/2)-1, floor(nhl/2)+1]."""
    nhl = kelly_size(**BASE_SCENARIO, sport="nhl")
    nba = kelly_size(**BASE_SCENARIO, sport="nba")
    assert nhl["reason"] == "sized" and nba["reason"] == "sized"
    expected_nba_low = (nhl["contracts"] // 2) - 1
    expected_nba_high = (nhl["contracts"] // 2) + 1
    assert expected_nba_low <= nba["contracts"] <= expected_nba_high, (
        f"NBA contracts={nba['contracts']} not ~half of NHL contracts={nhl['contracts']}"
    )


def test_kelly_size_ufc_halves_contracts():
    """sport='ufc' (multiplier 0.5) — same shape as NBA test."""
    nhl = kelly_size(**BASE_SCENARIO, sport="nhl")
    ufc = kelly_size(**BASE_SCENARIO, sport="ufc")
    assert nhl["reason"] == "sized" and ufc["reason"] == "sized"
    expected_low = (nhl["contracts"] // 2) - 1
    expected_high = (nhl["contracts"] // 2) + 1
    assert expected_low <= ufc["contracts"] <= expected_high, (
        f"UFC contracts={ufc['contracts']} not ~half of NHL contracts={nhl['contracts']}"
    )


@pytest.mark.parametrize(
    "balance,scenario_label,expected_min_contracts",
    [
        # Tiny balance: $20 * 0.0417 ≈ $0.83; floor=$1.00 binds; dynamic_max
        # = min($1.00, $200) = $1.00; contracts = max(1, floor(1.00/0.70)) = 1.
        # NBA's 0.5x doesn't reduce contracts below 1 because the floor
        # binds first AND max(1, ...) preserves at least one contract.
        (20.0, "floor_binds_tiny_balance", 1),
        # Huge balance: $50000 * 0.0417 ≈ $2086; ceiling = min($2500, $200)
        # = $200; contracts = floor(200/0.70) = 285. Same for 0.5x because
        # 0.5 * 0.0417 = 0.0208 → $50000 * 0.0208 = $1041 → still > $200
        # ceiling. Ceiling-bound scenario where multiplier is invisible.
        (50000.0, "ceiling_binds_huge_balance", 285),
    ],
)
def test_kelly_size_floor_and_ceiling_still_apply(
    balance, scenario_label, expected_min_contracts
):
    """Floor (MIN_BET_DOLLARS=$1.00) and ceiling (min(balance*5%, $200))
    bind regardless of multiplier. NBA's 0.5x cannot escape the safety
    bounds — at tiny balance the floor preserves a 1-contract minimum;
    at huge balance the ceiling caps both equally."""
    scen = {**BASE_SCENARIO, "balance": balance}
    nhl = kelly_size(**scen, sport="nhl")
    nba = kelly_size(**scen, sport="nba")
    assert nhl["reason"] == "sized" and nba["reason"] == "sized", (
        f"{scenario_label}: a leg returned non-sized: nhl={nhl}, nba={nba}"
    )
    # Both produce the same contract count when a safety bound binds.
    assert nhl["contracts"] == nba["contracts"], (
        f"{scenario_label}: NHL={nhl['contracts']}, NBA={nba['contracts']} — "
        f"safety bound should make multiplier invisible here"
    )
    assert nba["contracts"] >= expected_min_contracts


def test_kelly_size_uppercase_sport_normalized():
    """`sport.lower()` at the lookup site means callers passing 'NBA' or
    mixed-case strings produce the same result as canonical 'nba'."""
    lower = kelly_size(**BASE_SCENARIO, sport="nba")
    upper = kelly_size(**BASE_SCENARIO, sport="NBA")
    mixed = kelly_size(**BASE_SCENARIO, sport="Nba")
    assert lower == upper == mixed


def test_kelly_size_vig_stack_path_unaffected_when_sport_None():
    """vig_stack-shaped sizing call — 95¢ NO entry, p=0.97, $10k balance —
    with sport=None must produce identical output to a hand-computed
    pre-Session-49 baseline. This is the cross-strategy non-regression
    contract: vig_stack opportunities don't carry sport (per the
    Canonical Data Schema Reference) and must therefore be byte-identical."""
    vig_scen = dict(
        edge=0.05,
        probability=0.97,
        balance=10000.0,
        price_cents=95,
        confidence=0.75,
    )
    no_kwarg = kelly_size(**vig_scen)
    explicit_none = kelly_size(**vig_scen, sport=None)
    # Cross-strategy regression: an irrelevant sport string also defaults to 1.0,
    # so it must equal the no-sport call too. (Defends against a future bug
    # where sport accidentally affects vig_stack via a stray dict mutation.)
    fake_sport = kelly_size(**vig_scen, sport="vig_stack_should_never_be_a_sport")
    assert no_kwarg == explicit_none == fake_sport
    assert no_kwarg["reason"] == "sized"
    assert no_kwarg["contracts"] > 0


# ---------------------------------------------------------------------------
# Session 53 (May 4, 2026) — per-family max_position_dollars for vig_stack.
#
# At post-Apr-29 balance bump, KXINX flipped EV-negative purely on sizing
# (78% WR unchanged; +$0.52 → −$22.94/trade). Adds optional `family` kwarg
# to kelly_size(); when provided, replaces the $200 hardcode in `dynamic_max`
# with VIG_STACK_FAMILY_MAX_POSITION_DOLLARS[family] (default $200).
#
# Scenario chosen so the family cap is the BINDING constraint:
#   balance=10_000, MAX_BET_FRACTION=0.05 → balance × 5% = $500
#   family cap = $50 (KXINX) → dynamic_max = min(500, 50) = $50
#   risk_dollars after Kelly is well above $50, so the cap binds.
# ---------------------------------------------------------------------------
CAP_BOUND_SCENARIO = dict(
    edge=0.10,
    probability=0.78,
    balance=10_000.0,
    price_cents=50,
    confidence=0.80,
)


def test_kelly_size_default_family_none_unchanged():
    """Calling kelly_size without `family` is byte-identical to family=None,
    which is byte-identical to pre-Session-53 behavior. Paranoid no-op test."""
    no_kwarg = kelly_size(**CAP_BOUND_SCENARIO)
    explicit_none = kelly_size(**CAP_BOUND_SCENARIO, family=None)
    assert no_kwarg == explicit_none
    assert no_kwarg["reason"] == "sized"


def test_kelly_size_family_kxinx_caps_at_50():
    """KXINX has $50 cap. At $10k balance, balance × 5% = $500 would otherwise
    bind; family cap of $50 wins. total_cost ≤ $50."""
    sized = kelly_size(**CAP_BOUND_SCENARIO, family="KXINX")
    assert sized["reason"] == "sized"
    # $50 cap / $0.50 price = 100 contracts max
    assert sized["contracts"] <= 100
    assert sized["total_cost"] <= 50.0
    # And it must be SMALLER than the no-family default-$200 path.
    none_path = kelly_size(**CAP_BOUND_SCENARIO, family=None)
    assert sized["contracts"] < none_path["contracts"]


def test_kelly_size_family_unknown_uses_default_200():
    """Unconfigured family falls back to VIG_STACK_DEFAULT_MAX_POSITION_DOLLARS
    ($200) — same effective ceiling as family=None."""
    unknown = kelly_size(**CAP_BOUND_SCENARIO, family="KXNEWFAMILY")
    none_path = kelly_size(**CAP_BOUND_SCENARIO, family=None)
    assert unknown["contracts"] == none_path["contracts"]
    assert unknown["total_cost"] == none_path["total_cost"]


def test_kelly_size_balance_pct_still_wins_on_small_balance():
    """Pre-balance-bump regime: at $500 balance, 5% = $25 < $200 family cap.
    Family cap is non-binding — balance × MAX_BET_FRACTION still binds.
    Session 53 must not change small-balance sizing."""
    small = dict(CAP_BOUND_SCENARIO, balance=500.0)
    with_family = kelly_size(**small, family="KXHIGHAUS")  # cap=$200
    without = kelly_size(**small, family=None)
    assert with_family["contracts"] == without["contracts"]


def test_kelly_size_live_momentum_path_unaffected_by_family_kwarg():
    """live_momentum still passes sport= without family=. Sport multiplier
    behavior is unchanged by Session 53; family=None defaults preserve $200."""
    nba_pre = kelly_size(**CAP_BOUND_SCENARIO, sport="nba")
    nba_post = kelly_size(**CAP_BOUND_SCENARIO, sport="nba", family=None)
    assert nba_pre == nba_post


@pytest.mark.parametrize(
    "family,expected_cap",
    [
        ("KXINX",      50),
        ("KXMLBGAME",  50),
        ("KXHIGHCHI", 150),
        ("KXHIGHDEN", 150),
        ("KXHIGHNY",  150),
        ("KXHIGHAUS", 200),
        ("KXHIGHMIA", 200),
    ],
)
def test_kelly_size_every_configured_family_respects_its_cap(family, expected_cap):
    """Every family in VIG_STACK_FAMILY_MAX_POSITION_DOLLARS sizes within its
    own cap at $10k balance (where balance × 5% = $500 would otherwise bind).
    Guards against typos in future dict edits."""
    sized = kelly_size(**CAP_BOUND_SCENARIO, family=family)
    assert sized["reason"] == "sized"
    assert sized["total_cost"] <= float(expected_cap), (
        f"{family}: total_cost={sized['total_cost']} exceeds cap=${expected_cap}"
    )
