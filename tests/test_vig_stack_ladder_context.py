"""Session 100: unit tests for the vig_stack ladder context helper.

Mirrors the shape of tests/test_live_momentum_proxy.py (Session 99): small
unit tests on a pure function with no fixtures, no I/O. Covers weather
happy-path, non-weather omission, missing forecast cache, rank asc+desc,
unsortable ladder, unparseable close_ts, and the stable model-source string.
"""
from datetime import datetime, timezone

from bot.vig_stack_ladder_context import (
    LADDER_CONTEXT_KEYS,
    MODEL_SOURCE,
    compute_ladder_context,
)


_NOW = datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc)


def _weather_call(**overrides) -> dict:
    """Build a default weather call to compute_ladder_context, allowing
    targeted overrides for individual tests."""
    base = dict(
        ticker="KXHIGHAUS-26MAY09-B89.5",
        valid_tickers={
            "KXHIGHAUS-26MAY09-B85.5",
            "KXHIGHAUS-26MAY09-B87.5",
            "KXHIGHAUS-26MAY09-B89.5",
        },
        yes_sum_cents=135,
        valid_count=3,
        no_ask_cents=80,
        forecast_temp=89.7,
        source_city="Austin",
        is_weather=True,
        close_ts="2026-05-09T22:00:00Z",
        now_utc=_NOW,
    )
    base.update(overrides)
    return base


def test_weather_opp_returns_full_context():
    result = compute_ladder_context(**_weather_call())
    assert result["family"] == "KXHIGHAUS"
    assert result["ladder_total_yes_sum_cents"] == 135
    assert result["rung_count"] == 3
    # B89.5 is the highest of three ascending rungs (B85.5, B87.5, B89.5).
    assert result["selected_rung_rank_asc"] == 3
    assert result["selected_rung_rank_desc"] == 1
    # B89.5 parses to (89.0, "B") per the vig_stack_series.py parser convention.
    assert result["rung_strike"] == 89.0
    assert result["rung_kind"] == "B"
    assert result["no_price_cents"] == 80
    # 89.7 is inside [89.0, 90.0]; distance is -min(0.7, 0.3) = -0.3 (negative
    # = inside the bucket per vig_stack_series._forecast_distance_from_bucket).
    assert result["forecast_bucket_distance"] == -0.3
    assert result["source_forecast_temp"] == 89.7
    assert result["source_city"] == "Austin"
    # close_ts is 4 hours after now_utc.
    assert result["time_to_close_hr"] == 4.0
    assert result["ladder_context_source"] == MODEL_SOURCE


def test_non_weather_futures_opp_omits_weather_and_rank_fields():
    # KXNBA championship futures — no strike syntax, no NWS city.
    result = compute_ladder_context(
        ticker="KXNBA-26-CHAMP-LAL",
        valid_tickers={
            "KXNBA-26-CHAMP-LAL",
            "KXNBA-26-CHAMP-DEN",
            "KXNBA-26-CHAMP-BOS",
        },
        yes_sum_cents=125,
        valid_count=3,
        no_ask_cents=75,
        forecast_temp=None,
        source_city=None,
        is_weather=False,
        close_ts="2026-06-30T22:00:00Z",
        now_utc=_NOW,
    )
    assert result["family"] == "KXNBA"
    assert result["ladder_total_yes_sum_cents"] == 125
    assert result["rung_count"] == 3
    # No parseable strikes → both ranks None.
    assert result["selected_rung_rank_asc"] is None
    assert result["selected_rung_rank_desc"] is None
    assert result["rung_strike"] is None
    assert result["rung_kind"] is None
    assert result["no_price_cents"] == 75
    # Non-weather → all forecast/source fields None regardless of inputs.
    assert result["forecast_bucket_distance"] is None
    assert result["source_forecast_temp"] is None
    assert result["source_city"] is None
    assert result["time_to_close_hr"] is not None
    assert result["ladder_context_source"] == MODEL_SOURCE


def test_missing_forecast_cache_entry_still_returns_source_city():
    # NWS forecast fetch failed for this city this scan: forecast_temp=None
    # but we still know the city from _SERIES_TO_NWS. source_city must
    # survive; forecast_bucket_distance/source_forecast_temp must be None.
    result = compute_ladder_context(**_weather_call(forecast_temp=None))
    assert result["forecast_bucket_distance"] is None
    assert result["source_forecast_temp"] is None
    assert result["source_city"] == "Austin"
    # Other weather fields still populated.
    assert result["rung_strike"] == 89.0
    assert result["rung_kind"] == "B"


def test_selected_rung_rank_asc_and_desc_indexing():
    # 5-rung ladder. This opp is the 3rd-from-lowest (B89.5).
    call = _weather_call(
        ticker="KXHIGHAUS-26MAY09-B89.5",
        valid_tickers={
            "KXHIGHAUS-26MAY09-B85.5",
            "KXHIGHAUS-26MAY09-B87.5",
            "KXHIGHAUS-26MAY09-B89.5",
            "KXHIGHAUS-26MAY09-B91.5",
            "KXHIGHAUS-26MAY09-B93.5",
        },
        valid_count=5,
    )
    result = compute_ladder_context(**call)
    assert result["selected_rung_rank_asc"] == 3
    assert result["selected_rung_rank_desc"] == 3

    # Move the opp to 4th-from-lowest (B91.5).
    call["ticker"] = "KXHIGHAUS-26MAY09-B91.5"
    result = compute_ladder_context(**call)
    assert result["selected_rung_rank_asc"] == 4
    assert result["selected_rung_rank_desc"] == 2


def test_unsortable_ladder_returns_none_rank():
    # Futures ladder where no rung parses (all CHAMP-XXX, no B/T strike).
    result = compute_ladder_context(
        ticker="KXNBA-26-CHAMP-LAL",
        valid_tickers={"KXNBA-26-CHAMP-LAL", "KXNBA-26-CHAMP-DEN"},
        yes_sum_cents=120, valid_count=2, no_ask_cents=70,
        forecast_temp=None, source_city=None, is_weather=False,
        close_ts="2026-06-30T22:00:00Z", now_utc=_NOW,
    )
    assert result["selected_rung_rank_asc"] is None
    assert result["selected_rung_rank_desc"] is None


def test_unparseable_close_ts_returns_none_time_to_close():
    result = compute_ladder_context(**_weather_call(close_ts="not-a-timestamp"))
    assert result["time_to_close_hr"] is None
    # Other fields still populated.
    assert result["family"] == "KXHIGHAUS"
    assert result["ladder_context_source"] == MODEL_SOURCE


def test_missing_close_ts_returns_none_time_to_close():
    result = compute_ladder_context(**_weather_call(close_ts=None))
    assert result["time_to_close_hr"] is None


def test_ladder_context_source_is_stable_string():
    # The report groups by this; any rename is a breaking schema change.
    # If this fails, increment to v2 and update CLAUDE.md's "Canonical
    # Data Schema Reference → paper_trades.json" intentionally.
    assert MODEL_SOURCE == "vig_stack_ladder_context_v1"


def test_canonical_key_list_matches_returned_dict_keys():
    # LADDER_CONTEXT_KEYS is the source of truth for the executor inject
    # and report function. Drift between this tuple and the helper's
    # returned dict would silently drop fields at persist time.
    result = compute_ladder_context(**_weather_call())
    assert set(result.keys()) == set(LADDER_CONTEXT_KEYS)


def test_forecast_distance_sign_convention():
    # Negative when forecast inside the bucket; positive when outside.
    # Bucket for B89.5 is [89.0, 90.0).
    inside = compute_ladder_context(**_weather_call(forecast_temp=89.5))
    assert inside["forecast_bucket_distance"] == -0.5  # equidistant from edges

    outside_above = compute_ladder_context(**_weather_call(forecast_temp=92.0))
    assert outside_above["forecast_bucket_distance"] == 2.0  # 92.0 - 90.0

    outside_below = compute_ladder_context(**_weather_call(forecast_temp=87.0))
    assert outside_below["forecast_bucket_distance"] == 2.0  # 89.0 - 87.0
