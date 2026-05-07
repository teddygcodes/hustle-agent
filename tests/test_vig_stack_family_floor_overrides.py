"""Session 67 — per-family vig_stack NO-leg floor override tests.

Mirrors Session 64's test_per_sport_leader_min.py Pattern B discipline:
  - Production override dict empty (zero behavioral change)
  - Override resolution works per-family
  - Helper does not mutate the override dict
  - No alias leak across families

The lever was shipped as Outcome B because Session 67 Phase-1 evidence
showed real cross-family signal (n=65 settled CFs, mean +12.46c) but the
signal was ISOLATED to KXHIGHMIA + KXHIGHAUS (KXINX flat-negative at every
floor band AND historically losing $48/trade). Lowering the global stable
floor would inadvertently relax KXINX too. Per-family architecture lets a
future session activate KXHIGHMIA / KXHIGHAUS without dragging KXINX along.
"""

from __future__ import annotations

import pytest

from bot.config import (
    VIG_STACK_FAMILY_FLOOR_OVERRIDES,
    VIG_STACK_MIN_NO_ENTRY_PRICE,
    VIG_STACK_WEATHER_MIN_PRICE,
    get_vig_stack_floor_for_family,
)


def test_dict_empty_in_production():
    """Session 67 Pattern B: override dict ships empty.

    Production must keep VIG_STACK_FAMILY_FLOOR_OVERRIDES empty. Activating
    any family requires deliberate evidence per Session 67 Phase-1 + a +14d
    re-validation routine. This test locks the discipline so a future session
    can't accidentally ship an activation without the discipline trail.
    """
    assert VIG_STACK_FAMILY_FLOOR_OVERRIDES == {}, (
        "VIG_STACK_FAMILY_FLOOR_OVERRIDES must ship empty (Pattern B). "
        "Activating any family requires its own session with +14d "
        "re-validation routine — see Session 64 (MOMENTUM_LEADER_MIN_PER_SPORT) "
        "and Session 49 (per-sport size_multiplier) for the precedent."
    )


def test_per_family_override_resolution(monkeypatch):
    """Override resolution returns the per-family value when present, default
    when absent."""
    monkeypatch.setitem(VIG_STACK_FAMILY_FLOOR_OVERRIDES, "KXHIGHMIA", 0.55)
    # KXHIGHMIA: returns the override value
    assert get_vig_stack_floor_for_family("KXHIGHMIA", VIG_STACK_MIN_NO_ENTRY_PRICE) == 0.55
    # KXHIGHAUS: not overridden → returns the default (stable family)
    assert get_vig_stack_floor_for_family("KXHIGHAUS", VIG_STACK_MIN_NO_ENTRY_PRICE) == VIG_STACK_MIN_NO_ENTRY_PRICE
    # KXINX: not overridden → returns the default (stable family)
    assert get_vig_stack_floor_for_family("KXINX", VIG_STACK_MIN_NO_ENTRY_PRICE) == VIG_STACK_MIN_NO_ENTRY_PRICE
    # Volatile family: returns the volatile default unchanged
    assert get_vig_stack_floor_for_family("KXHIGHCHI", VIG_STACK_WEATHER_MIN_PRICE) == VIG_STACK_WEATHER_MIN_PRICE


def test_helper_does_not_mutate_override_dict():
    """Calling the helper does NOT add keys to the override dict.

    Using `dict.get(key, default)` (not `dict.setdefault`) is load-bearing —
    a side-effect dict-write would persist across requests and silently grow
    the override surface. Lock that the helper is read-only on the dict.
    """
    initial = dict(VIG_STACK_FAMILY_FLOOR_OVERRIDES)
    get_vig_stack_floor_for_family("KXHIGHCHI", VIG_STACK_MIN_NO_ENTRY_PRICE)
    get_vig_stack_floor_for_family("KXHIGHDEN", VIG_STACK_WEATHER_MIN_PRICE)
    get_vig_stack_floor_for_family("KXNEVERSEEN", 0.50)
    assert dict(VIG_STACK_FAMILY_FLOOR_OVERRIDES) == initial, (
        "get_vig_stack_floor_for_family must not mutate the override dict. "
        "Use dict.get(key, default), not dict.setdefault."
    )


def test_per_family_floor_does_not_affect_other_families(monkeypatch):
    """Anti-aliasing: setting an override on one family must NOT affect any
    other family's resolution.

    Mirrors Session 64's tennis-aliasing-isolation regression — vig_stack
    family overrides keyed by string family name, no shared underlying dict
    reference. KXHIGHMIA / KXHIGHAUS / KXINX are independent strings, so an
    override on one must not leak to the others.
    """
    monkeypatch.setitem(VIG_STACK_FAMILY_FLOOR_OVERRIDES, "KXHIGHMIA", 0.55)
    # Other stable families not affected
    assert get_vig_stack_floor_for_family("KXHIGHAUS", 0.70) == 0.70
    assert get_vig_stack_floor_for_family("KXINX", 0.70) == 0.70
    # Volatile families not affected
    assert get_vig_stack_floor_for_family("KXHIGHCHI", 0.93) == 0.93
    assert get_vig_stack_floor_for_family("KXHIGHDEN", 0.93) == 0.93
    assert get_vig_stack_floor_for_family("KXHIGHNY", 0.93) == 0.93
    # Unknown family also not affected
    assert get_vig_stack_floor_for_family("KXNEWFAMILY", 0.85) == 0.85
