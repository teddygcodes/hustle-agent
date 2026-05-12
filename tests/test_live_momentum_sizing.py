from __future__ import annotations

from bot.config import SPORT_PROFILES
from bot.live_momentum_sizing import (
    live_momentum_dip_size_multiplier,
    size_live_momentum_entry,
)
from bot.sizing import kelly_size


def test_live_momentum_sizing_matches_existing_kelly_then_dip_scale():
    result = size_live_momentum_entry(
        price_cents=70,
        dip_cents=7,
        sport="nhl",
        balance=500.0,
        sport_profile=SPORT_PROFILES["nhl"],
    )
    base = kelly_size(
        edge=0.07,
        probability=0.77,
        balance=500.0,
        price_cents=70,
        confidence=0.80,
        sport="nhl",
    )
    mult = live_momentum_dip_size_multiplier(7, SPORT_PROFILES["nhl"])
    expected_contracts = min(
        int(base["contracts"] * mult),
        SPORT_PROFILES["nhl"]["max_contracts"],
    )

    assert result["sizing"]["reason"] == "sized"
    assert result["contracts"] == expected_contracts
    assert result["sizing"]["total_cost"] == round(result["contracts"] * 0.70, 2)


def test_live_momentum_sizing_applies_sport_max_contracts():
    result = size_live_momentum_entry(
        price_cents=70,
        dip_cents=10,
        sport="ufc",
        balance=10_500.0,
        sport_profile=SPORT_PROFILES["ufc"],
    )

    assert result["contracts"] == SPORT_PROFILES["ufc"]["max_contracts"]
    assert result["capped_by_sport"] is True
    assert result["sizing"]["total_cost"] == 7.0


def test_live_momentum_sizing_missing_inputs_are_explicit():
    result = size_live_momentum_entry(
        price_cents=None,
        dip_cents=5,
        sport="ipl",
        balance=10_500.0,
        sport_profile=None,
    )

    assert result["contracts"] is None
    assert result["sizing"] is None
    assert result["missing_sizing_fields"] == ["price_cents"]
    assert result["sizing_unavailable_reason"] == "missing_inputs"


def test_live_momentum_sizing_unsized_kelly_is_not_fabricated():
    result = size_live_momentum_entry(
        price_cents=86,
        dip_cents=0,
        sport="atp",
        balance=10_500.0,
        sport_profile=SPORT_PROFILES["atp"],
    )

    assert result["contracts"] is None
    assert result["sizing"]["contracts"] == 0
    assert result["sizing_unavailable_reason"] == "no_edge"
