"""Session 64: per-sport MOMENTUM_LEADER_MIN override tests.

Locks the architectural infrastructure shipped in Pattern B:
- Helper resolution semantics (per-sport override; default fallback)
- Alias isolation (setting wta does not leak to atp/atp_c/wta_c)
- Session 19c global default (0.65) is unchanged
- Production safety (dict empty unless wta is also un-disabled)
"""

from bot.config import (
    MOMENTUM_DISABLED_SPORTS,
    MOMENTUM_LEADER_MIN,
    MOMENTUM_LEADER_MIN_PER_SPORT,
    get_leader_min_for_sport,
)


def test_session_19c_global_leader_min_unchanged():
    """Adding the per-sport override layer must not change the global
    Session 19c value of 0.65."""
    assert MOMENTUM_LEADER_MIN == 0.65


def test_per_sport_leader_min_override_resolution(monkeypatch):
    """get_leader_min_for_sport returns the per-sport value when the
    override dict has an entry for that sport; falls back to the global
    MOMENTUM_LEADER_MIN otherwise; case-insensitive lookup."""
    monkeypatch.setitem(MOMENTUM_LEADER_MIN_PER_SPORT, "wta", 0.55)
    assert get_leader_min_for_sport("wta") == 0.55
    assert get_leader_min_for_sport("WTA") == 0.55
    assert get_leader_min_for_sport("Wta") == 0.55
    assert get_leader_min_for_sport("nba") == MOMENTUM_LEADER_MIN
    assert get_leader_min_for_sport("nhl") == MOMENTUM_LEADER_MIN
    assert get_leader_min_for_sport(None) == MOMENTUM_LEADER_MIN
    assert get_leader_min_for_sport("") == MOMENTUM_LEADER_MIN


def test_per_sport_leader_min_no_alias_leak(monkeypatch):
    """Setting per-sport leader_min for wta must NOT affect atp,
    atp_challenger, or wta_challenger. Locks against the SPORT_PROFILES
    aliasing trap at config.py:374-375 — Session 42 regression style."""
    monkeypatch.setitem(MOMENTUM_LEADER_MIN_PER_SPORT, "wta", 0.55)
    assert get_leader_min_for_sport("wta") == 0.55
    for circuit in ("atp", "atp_challenger", "wta_challenger"):
        assert get_leader_min_for_sport(circuit) == MOMENTUM_LEADER_MIN, (
            f"{circuit} should fall back to the global default, "
            f"not inherit wta's override"
        )


def test_dict_empty_in_production_until_wta_unidisabled():
    """Pattern B production invariant: if MOMENTUM_LEADER_MIN_PER_SPORT
    has a 'wta' entry, then 'wta' must also have been removed from
    MOMENTUM_DISABLED_SPORTS — otherwise the lever is documented but
    silently null-op, which is confusing. Guards against accidentally
    activating one half of the two-lever bet."""
    if "wta" in MOMENTUM_LEADER_MIN_PER_SPORT:
        assert "wta" not in MOMENTUM_DISABLED_SPORTS, (
            "wta has a leader_min override but is still disabled — "
            "either complete activation (remove from disabled set) "
            "or revert (remove the override)"
        )


def test_helper_does_not_mutate_override_dict():
    """get_leader_min_for_sport is a pure read; lookup must not insert
    keys for missing sports."""
    snapshot = dict(MOMENTUM_LEADER_MIN_PER_SPORT)
    get_leader_min_for_sport("ipl")
    get_leader_min_for_sport(None)
    get_leader_min_for_sport("")
    assert dict(MOMENTUM_LEADER_MIN_PER_SPORT) == snapshot
