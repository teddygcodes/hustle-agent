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
        ticker="KXNHLGAME-26MAY12TEST-BUF",
        sport="nhl",
        balance=500.0,
        confidence=0.84,
        sport_profile=SPORT_PROFILES["nhl"],
    )
    base = kelly_size(
        edge=0.07,
        probability=0.77,
        balance=500.0,
        price_cents=70,
        confidence=0.84,
        sport="nhl",
        family="KXNHLGAME",
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
        ticker="KXUFCFIGHT-26MAY12TEST-ABC",
        sport="ufc",
        balance=10_500.0,
        confidence=0.92,
        sport_profile=SPORT_PROFILES["ufc"],
    )

    assert result["contracts"] == SPORT_PROFILES["ufc"]["max_contracts"]
    assert result["capped_by_sport"] is True
    assert result["sizing"]["total_cost"] == 7.0


def test_live_momentum_sizing_missing_inputs_are_explicit():
    result = size_live_momentum_entry(
        price_cents=None,
        dip_cents=5,
        ticker="KXIPLGAME-26MAY12TEST-CSK",
        sport="ipl",
        balance=10_500.0,
        confidence=0.72,
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
        ticker="KXATPMATCH-26MAY12TEST-AAA",
        sport="atp",
        balance=10_500.0,
        confidence=0.65,
        sport_profile=SPORT_PROFILES["atp"],
    )

    assert result["contracts"] is None
    assert result["sizing"]["contracts"] == 0
    assert result["sizing_unavailable_reason"] == "no_edge"


def test_live_momentum_sizer_passes_sport_from_ticker_prefix(monkeypatch):
    """Regression: pre-S135 active helper used the caller sport directly.

    This fails on the pre-fix code for ticker-derived sport reconstruction
    because `kelly_size` would receive the stale/fallback sport instead of the
    ticker prefix sport.
    """
    captured = {}

    def fake_kelly_size(**kwargs):
        captured.update(kwargs)
        return {"contracts": 1, "total_cost": 0.70, "max_payout": 1.0, "reason": "sized"}

    monkeypatch.setattr("bot.sizing.kelly_size", fake_kelly_size)

    result = size_live_momentum_entry(
        price_cents=70,
        dip_cents=7,
        ticker="KXNHLGAME-26MAY12TEST-BUF",
        sport="fallback_should_not_win",
        balance=10_500.0,
        confidence=0.84,
        sport_profile=SPORT_PROFILES["nhl"],
    )

    assert result["contracts"] == 1
    assert captured["sport"] == "nhl"
    assert captured["sport"] is not None


def test_live_momentum_sizer_passes_family_from_ticker_prefix(monkeypatch):
    """Regression: pre-S135 active helper omitted family and hit family=None.

    This fails on the pre-fix code because `kelly_size` would receive no
    family kwarg, keeping the legacy family=None branch reachable for accepted
    live_momentum entries.
    """
    captured = {}

    def fake_kelly_size(**kwargs):
        captured.update(kwargs)
        return {"contracts": 1, "total_cost": 0.70, "max_payout": 1.0, "reason": "sized"}

    monkeypatch.setattr("bot.sizing.kelly_size", fake_kelly_size)

    result = size_live_momentum_entry(
        price_cents=70,
        dip_cents=7,
        ticker="KXNHLGAME-26MAY12TEST-BUF",
        sport="nhl",
        balance=10_500.0,
        confidence=0.84,
        sport_profile=SPORT_PROFILES["nhl"],
    )

    assert result["contracts"] == 1
    assert captured["family"] == "KXNHLGAME"
    assert captured["family"] is not None
