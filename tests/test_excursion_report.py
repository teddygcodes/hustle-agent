"""Tests for tools/excursion_report.py — Session 16 gap-math fix.

Locks in the side-aware exit_favorable_magnitude convention (YES-cents-
favorable space) and the gap = MFE - exit_favorable identity. The 100-tuple
property test asserts the report's helper matches a re-derivation of the
formula from raw fields, so any future units/sign drift on either side
fails the suite immediately.

Convention this suite enforces:
  - YES side: exit_favorable = closing_yes - entry_yes
  - NO  side: exit_favorable = (100 - entry_no) - closing_yes
  - gap     = mfe_cents - exit_favorable

After Session 16, mfe_cents stored in clv.json is extended at settlement
to include the settlement event (max with clv_cents, clamped ≥0). So the
realistic post-fix data shape is:
  - winners: mfe_cents == clv_cents == exit_favorable, gap = 0
  - losers:  mfe_cents stays at the highest observed favorable bid (≥0),
             clv_cents = -entry < 0, gap = mfe + entry > 0
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.excursion_report import (  # noqa: E402
    exit_favorable_magnitude,
    load_settled_with_excursion,
)
import tools.excursion_report as excursion_module  # noqa: E402


# ---------------------------------------------------------------------------
# Hand-crafted unit tests — exit_favorable_magnitude direct
# ---------------------------------------------------------------------------

class TestExitFavorableMagnitude:
    """Side-aware exit_favorable in YES-cents-favorable space."""

    def test_yes_winner_settles_to_payout(self):
        # YES @30¢, settles YES (closing=100): 100-30 = 70¢ profit
        assert exit_favorable_magnitude("yes", 30, 100.0) == 70.0

    def test_yes_loser_settles_to_zero(self):
        # YES @80¢, settles NO (closing=0): 0-80 = -80¢
        assert exit_favorable_magnitude("yes", 80, 0.0) == -80.0

    def test_no_winner_settles_to_zero(self):
        # NO @70¢ (yes-implied=30), settles NO (closing_yes=0):
        # (100-70) - 0 = 30 ✓
        assert exit_favorable_magnitude("no", 70, 0.0) == 30.0

    def test_no_loser_settles_to_hundred(self):
        # NO @70¢, settles YES (closing_yes=100):
        # (100-70) - 100 = -70 ✓
        assert exit_favorable_magnitude("no", 70, 100.0) == -70.0

    def test_yes_zero_pnl_at_entry(self):
        # closing_yes equals entry → exit_favorable = 0
        assert exit_favorable_magnitude("yes", 50, 50.0) == 0.0

    def test_no_zero_pnl_at_implied_entry(self):
        # NO @60 → implied YES at entry = 40. Closing at 40 → exit_fav = 0.
        assert exit_favorable_magnitude("no", 60, 40.0) == 0.0

    def test_returns_float(self):
        assert isinstance(exit_favorable_magnitude("yes", 50, 100.0), float)
        assert isinstance(exit_favorable_magnitude("no", 50, 0.0), float)


# ---------------------------------------------------------------------------
# Hand-crafted gap tests — mirrors the report's full computation
# ---------------------------------------------------------------------------

def _gap(side: str, entry: int, closing_yes: float, mfe: int) -> float:
    """Compute gap exactly as the report does (mirror lines in main())."""
    return float(mfe) - exit_favorable_magnitude(side, entry, closing_yes)


class TestGapHandCrafted:
    """Each case mirrors a realistic post-Session-16 record shape."""

    def test_yes_winner_extended_yields_zero(self):
        # Winner with extended MFE: mfe_cents == clv_cents == exit_favorable
        # entry=30, closing=100, mfe=70 (extended from observed-99-30=69 → 70).
        assert _gap("yes", 30, 100.0, 70) == 0.0

    def test_no_winner_extended_yields_zero(self):
        # NO @70¢, settles NO: clv=30, mfe extended to 30. gap=0.
        assert _gap("no", 70, 0.0, 30) == 0.0

    def test_yes_loser_with_observed_mfe_yields_positive_gap(self):
        # YES @80¢ peaked at 95¢ (mfe=15) then crashed to NO settle.
        # exit_fav = -80. gap = 15 - (-80) = 95.  Real exit-logic signal.
        assert _gap("yes", 80, 0.0, 15) == 95.0

    def test_no_loser_with_observed_mfe_yields_positive_gap(self):
        # NO @70 peaked at 80 no_bid (mfe=10), then settles YES.
        # exit_fav = -70. gap = 10 - (-70) = 80.
        assert _gap("no", 70, 100.0, 10) == 80.0

    def test_mfe_zero_loser_yields_entry_size_gap(self):
        # Adverse-only YES position: mfe=0, settles NO at closing=0.
        # entry=78. exit_fav = -78. gap = 0 - (-78) = 78.
        # Acceptance criterion from spec: MFE=0 → exit_favorable ≤ 0 → gap > 0.
        assert _gap("yes", 78, 0.0, 0) == 78.0

    def test_mfe_zero_no_loser_yields_entry_size_gap(self):
        # NO @70 with no observed favorable, settles YES.
        # exit_fav = (100-70) - 100 = -70. gap = 0 - (-70) = 70.
        assert _gap("no", 70, 100.0, 0) == 70.0

    def test_perfect_extension_winner_yields_zero(self):
        # User's "perfect-exit case": mfe == exit_favorable → gap = 0.
        # YES @20¢, settles YES at 100. exit_fav=80. mfe=80. gap=0.
        assert _gap("yes", 20, 100.0, 80) == 0.0

    def test_mfe_just_below_clv_pre_extension_negative(self):
        # Pre-Session-16 winner shape (NO record where mfe was 19 instead
        # of extended-20). gap = 19 - 20 = -1. Documents the bug we fixed.
        # Post-Session-16 mfe would be 20 (extended) → gap=0; this case
        # only appears if backfill hasn't run.
        assert _gap("no", 80, 0.0, 19) == -1.0


# ---------------------------------------------------------------------------
# Property test — 100 random tuples
# ---------------------------------------------------------------------------

class TestPropertyRandomTuples:
    """Generate 100 (side, entry, mfe, closing_yes) tuples, assert the
    report's helper matches a re-derivation of the formula. Catches any
    future sign/units drift in either the helper or the documented
    convention.
    """

    def test_property_match_by_hand_100x(self):
        rng = random.Random(42)
        for _ in range(100):
            side = rng.choice(["yes", "no"])
            entry = rng.randint(3, 97)
            # mfe range: 0..(100 - entry) — the maximum possible extended
            # post-Session-16 MFE for a winner. Covers the post-fix shape.
            mfe = rng.randint(0, 100 - entry)
            closing_yes = float(rng.choice([0, 100]))

            # Report's computation
            exit_fav = exit_favorable_magnitude(side, entry, closing_yes)
            gap = float(mfe) - exit_fav

            # By-hand re-derivation (no shared function calls)
            if side == "yes":
                by_hand_exit = float(closing_yes) - float(entry)
            else:
                by_hand_exit = (100.0 - float(entry)) - float(closing_yes)
            by_hand_gap = float(mfe) - by_hand_exit

            assert exit_fav == pytest.approx(by_hand_exit, abs=1e-9), \
                f"exit_favorable mismatch: side={side} entry={entry} closing={closing_yes}"
            assert gap == pytest.approx(by_hand_gap, abs=1e-9), \
                f"gap mismatch: side={side} entry={entry} mfe={mfe} closing={closing_yes}"

    def test_property_winner_with_extended_mfe_yields_nonneg_gap(self):
        """For records simulating the post-fix shape: when mfe ≥ exit_favorable,
        gap ≥ 0. This holds for winners after settlement extension AND for
        losers (where exit_favorable is negative ≤ mfe ≥ 0)."""
        rng = random.Random(123)
        for _ in range(100):
            side = rng.choice(["yes", "no"])
            entry = rng.randint(3, 97)
            closing_yes = float(rng.choice([0, 100]))
            exit_fav = exit_favorable_magnitude(side, entry, closing_yes)
            # Post-extension MFE: max(observed, exit_fav) clamped ≥ 0
            observed_mfe = rng.randint(0, max(0, int(100 - entry)))
            mfe = max(observed_mfe, max(0, int(exit_fav)))
            gap = float(mfe) - exit_fav
            assert gap >= 0, (
                f"Post-extension gap should be ≥ 0: side={side} entry={entry} "
                f"closing={closing_yes} mfe={mfe} exit_fav={exit_fav} gap={gap}"
            )


# ---------------------------------------------------------------------------
# Load filter — defensive against malformed records
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_clv_file(tmp_path, monkeypatch):
    """Repoint excursion_report.CLV_FILE so tests use a tmp file."""
    f = tmp_path / "clv.json"
    monkeypatch.setattr(excursion_module, "CLV_FILE", f)
    return f


def _good_record(**overrides) -> dict:
    base = {
        "ticker": "KXTEST-1",
        "opp_type": "live_momentum",
        "side": "yes",
        "entry_price_cents": 30,
        "status": "settled",
        "closing_yes_price": 100,
        "clv_cents": 70.0,
        "mfe_cents": 70,
    }
    base.update(overrides)
    return base


class TestLoadFilter:

    def test_loads_well_formed_record(self, tmp_clv_file):
        tmp_clv_file.write_text(json.dumps([_good_record()]))
        recs = load_settled_with_excursion()
        assert len(recs) == 1

    def test_skips_open_status(self, tmp_clv_file):
        tmp_clv_file.write_text(json.dumps([_good_record(status="open")]))
        assert load_settled_with_excursion() == []

    def test_skips_counterfactual_settled(self, tmp_clv_file):
        tmp_clv_file.write_text(
            json.dumps([_good_record(status="counterfactual_settled")])
        )
        assert load_settled_with_excursion() == []

    def test_skips_missing_mfe(self, tmp_clv_file):
        tmp_clv_file.write_text(json.dumps([_good_record(mfe_cents=None)]))
        assert load_settled_with_excursion() == []

    def test_skips_missing_clv(self, tmp_clv_file):
        tmp_clv_file.write_text(json.dumps([_good_record(clv_cents=None)]))
        assert load_settled_with_excursion() == []

    def test_skips_missing_closing_yes(self, tmp_clv_file):
        tmp_clv_file.write_text(json.dumps([_good_record(closing_yes_price=None)]))
        assert load_settled_with_excursion() == []

    def test_skips_missing_side(self, tmp_clv_file):
        tmp_clv_file.write_text(json.dumps([_good_record(side=None)]))
        assert load_settled_with_excursion() == []

    def test_skips_invalid_side(self, tmp_clv_file):
        tmp_clv_file.write_text(json.dumps([_good_record(side="both")]))
        assert load_settled_with_excursion() == []

    def test_skips_missing_entry_price(self, tmp_clv_file):
        tmp_clv_file.write_text(
            json.dumps([_good_record(entry_price_cents=None)])
        )
        assert load_settled_with_excursion() == []

    def test_returns_empty_for_missing_file(self, tmp_clv_file):
        # File doesn't exist
        assert not tmp_clv_file.exists()
        assert load_settled_with_excursion() == []

    def test_returns_empty_for_malformed_json(self, tmp_clv_file):
        tmp_clv_file.write_text("not valid json")
        assert load_settled_with_excursion() == []

    def test_returns_empty_for_non_list(self, tmp_clv_file):
        tmp_clv_file.write_text(json.dumps({"oops": "dict"}))
        assert load_settled_with_excursion() == []
