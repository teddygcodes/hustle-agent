"""Tests for bot/regime.py — pure regime tagging.

Session 14 (Apr 25 pivot-enabling instrumentation arc): every record (decisions,
predictions, clv real + CF, positions, universe) carries a `regime` dict so
reports can slice strategy outcomes by regime axes. The tagger is pure: same
inputs → same output, no I/O, no clock reads.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.regime import tag  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# time_of_day axis (America/New_York buckets)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts_utc, expected", [
    # Winter (EST = UTC-5): Feb 2026
    (_utc(2026, 2, 15, 12), "morning"),    # 07:00 ET
    (_utc(2026, 2, 15, 16), "morning"),    # 11:00 ET
    (_utc(2026, 2, 15, 18), "afternoon"),  # 13:00 ET
    (_utc(2026, 2, 15, 23), "evening"),    # 18:00 ET
    (_utc(2026, 2, 16, 5),  "overnight"),  # 00:00 ET
    # Summer (EDT = UTC-4): Jul 2026
    (_utc(2026, 7, 4, 11),  "morning"),    # 07:00 EDT
    (_utc(2026, 7, 4, 23),  "evening"),    # 19:00 EDT
    # DST spring forward 2026-03-08 02:00 EST → 03:00 EDT.
    # Same UTC hour resolves to different ET hours pre- vs post-DST,
    # which can cross the bucket boundary at 06:00.
    (_utc(2026, 3, 7, 10),  "overnight"),  # 05:00 EST  (day before DST)
    (_utc(2026, 3, 8, 10),  "morning"),    # 06:00 EDT  (after DST → bucket crossed)
    (_utc(2026, 3, 8, 8),   "overnight"),  # 04:00 EDT  (post-DST but still 0-6)
    # DST fall back 2026-11-01 02:00 EDT → 01:00 EST
    (_utc(2026, 11, 1, 5),  "overnight"),  # 01:00 EDT
    (_utc(2026, 11, 1, 7),  "overnight"),  # 02:00 EST (after fall back, still 0-6)
])
def test_time_of_day_handles_dst(ts_utc, expected):
    assert tag(ts_utc, "KXANY")["time_of_day"] == expected


# ---------------------------------------------------------------------------
# day_of_week axis (America/New_York calendar day)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ts_utc, expected", [
    (_utc(2026, 4, 20, 18), "mon"),
    (_utc(2026, 4, 21, 18), "tue"),
    (_utc(2026, 4, 22, 18), "wed"),
    (_utc(2026, 4, 23, 18), "thu"),
    (_utc(2026, 4, 24, 18), "fri"),
    (_utc(2026, 4, 25, 18), "sat"),
    (_utc(2026, 4, 26, 18), "sun"),
])
def test_day_of_week_each_day(ts_utc, expected):
    assert tag(ts_utc, "KXANY")["day_of_week"] == expected


# ---------------------------------------------------------------------------
# sport_phase axis
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker, ts_utc, expected", [
    # NBA: Apr 25 2026 falls in playoffs window
    ("KXNBAGAME-26APR25-LAL", _utc(2026, 4, 25, 18), "playoffs"),
    # NBA: Nov 1 2025 is regular season
    ("KXNBAGAME-25NOV01-LAL", _utc(2025, 11, 1, 18), "regular"),
    # NBA: Aug 15 2025 is off-season (no range covers it)
    ("KXNBAGAME-25AUG15-LAL", _utc(2025, 8, 15, 18), "off"),
    # MLB: Apr 25 2026 is regular season
    ("KXMLBGAME-26APR25-LAA", _utc(2026, 4, 25, 18), "regular"),
    # NHL: Apr 25 2026 is playoffs
    ("KXNHLGAME-26APR25-NYR", _utc(2026, 4, 25, 18), "playoffs"),
    # Championship futures still get a sport-level phase
    ("KXNBA-CHAMP-26", _utc(2026, 4, 25, 18), "playoffs"),
    # Sport with no phase table (UFC) → None
    ("KXUFCFIGHT-26APR25", _utc(2026, 4, 25, 18), None),
    # Non-sport tickers → None
    ("KXWX-NYC-RAIN", _utc(2026, 4, 25, 18), None),
    ("KXTREASURY-2YR", _utc(2026, 4, 25, 18), None),
])
def test_sport_phase(ticker, ts_utc, expected):
    assert tag(ts_utc, ticker)["sport_phase"] == expected


def test_sport_prefix_longest_wins():
    """KXNBAGAME (per-game) must beat KXNBA (championship) when both could match."""
    # KXNBAGAME is longer; should resolve to nba via the per-game prefix
    result = tag(_utc(2026, 4, 25, 18), "KXNBAGAME-26APR25-LAL")
    assert result["sport_phase"] == "playoffs"


# ---------------------------------------------------------------------------
# event_horizon_hr axis
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("close_offset_hours, expected", [
    (0.5, "<2h"),
    (6,   "2-12h"),
    (24,  "12-48h"),
    (120, "48-168h"),
    (200, ">168h"),
])
def test_event_horizon_hr_buckets(close_offset_hours, expected):
    now = _utc(2026, 4, 25, 12)
    close_ts = (now + timedelta(hours=close_offset_hours)).isoformat()
    result = tag(now, "KXANY", market_state={"close_ts": close_ts})
    assert result["event_horizon_hr"] == expected


def test_event_horizon_hr_null_when_market_state_missing():
    assert tag(_utc(2026, 4, 25), "KXANY")["event_horizon_hr"] is None
    assert tag(_utc(2026, 4, 25), "KXANY", market_state={})["event_horizon_hr"] is None
    assert tag(_utc(2026, 4, 25), "KXANY", market_state=None)["event_horizon_hr"] is None


def test_close_ts_alternative_key_close_time():
    """universe.py constructs rows with close_ts derived from m['close_time'];
    historical universe rows have close_ts but raw Kalshi dicts have close_time.
    The tagger must accept either key so it works on both shapes."""
    now = _utc(2026, 4, 25, 12)
    close = (now + timedelta(hours=6)).isoformat()
    assert tag(now, "KXANY", market_state={"close_time": close})["event_horizon_hr"] == "2-12h"
    assert tag(now, "KXANY", market_state={"close_ts": close})["event_horizon_hr"] == "2-12h"


def test_event_horizon_hr_handles_z_suffix_iso():
    """Kalshi sometimes returns close_time as '...Z' instead of '...+00:00'."""
    now = _utc(2026, 4, 25, 12)
    close = (now + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    assert tag(now, "KXANY", market_state={"close_ts": close})["event_horizon_hr"] == "2-12h"


def test_event_horizon_hr_handles_malformed_close_ts():
    """Garbage close_ts → None, not a crash."""
    now = _utc(2026, 4, 25, 12)
    assert tag(now, "KXANY", market_state={"close_ts": "not-a-date"})["event_horizon_hr"] is None
    assert tag(now, "KXANY", market_state={"close_ts": 12345})["event_horizon_hr"] is None


# ---------------------------------------------------------------------------
# Determinism / purity property
# ---------------------------------------------------------------------------

def test_tag_is_deterministic_property():
    """Same inputs must produce identical output across many random samples."""
    rng = random.Random(42)
    sports = ["KXNBAGAME-X", "KXMLBGAME-Y", "KXNHLGAME-Z", "KXWX-NYC", "KXTREASURY",
              "KXATPMATCH-X", "KXUFCFIGHT-Y", "KXIPLGAME-Z"]
    for _ in range(100):
        ts = _utc(2026, 1, 1) + timedelta(minutes=rng.randint(0, 525_600))
        ticker = rng.choice(sports)
        if rng.random() < 0.7:
            close_ts = (ts + timedelta(hours=rng.uniform(0.1, 300))).isoformat()
            ms = {"close_ts": close_ts, "elapsed_seconds": rng.randint(0, 7200)}
        else:
            ms = None
        a = tag(ts, ticker, ms)
        b = tag(ts, ticker, ms)
        assert a == b
        assert set(a.keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr",
            "match_phase",
        }


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_tag_raises_on_invalid_ts():
    with pytest.raises((ValueError, TypeError)):
        tag(None, "KXANY")
    with pytest.raises((ValueError, TypeError)):
        tag("2026-04-25T12:00:00", "KXANY")  # string, not datetime


def test_tag_handles_naive_datetime_as_utc():
    naive = datetime(2026, 4, 25, 12)  # no tzinfo
    aware = _utc(2026, 4, 25, 12)
    assert tag(naive, "KXANY") == tag(aware, "KXANY")


def test_tag_handles_empty_ticker():
    result = tag(_utc(2026, 4, 25), "")
    assert result["sport_phase"] is None
    # Other axes still populate
    assert result["time_of_day"] is not None
    assert result["day_of_week"] is not None
    assert result["match_phase"] is None  # Session 34: no sport → no phase


# ---------------------------------------------------------------------------
# Session 34: match_phase axis
# ---------------------------------------------------------------------------
# v1 taxonomy:
#   tennis (atp/atp_challenger/wta/wta_challenger): set_1|set_2|set_3+ from
#     set_number, else early|mid|late from elapsed_seconds, else None.
#   ufc: round_1|round_2|round_3+ from round_num, else round_1|round_2|round_3+
#     from elapsed_seconds, else None.
#   ipl: powerplay|middle|death from over_count (1..20), else None (no time
#     fallback).
#   other sports: None.

NOW = _utc(2026, 4, 25, 18)


@pytest.mark.parametrize("ticker", [
    "KXATPMATCH-26APR25-FED",
    "KXATPCHALLENGERMATCH-26APR25-X",
    "KXWTAMATCH-26APR25-WIL",
    "KXWTACHALLENGERMATCH-26APR25-Y",
])
@pytest.mark.parametrize("elapsed, expected", [
    (0,    "early"),   # 0 min
    (1500, "early"),   # 25 min
    (1799, "early"),   # 29:59 — boundary
    (1800, "mid"),     # 30:00
    (3000, "mid"),     # 50 min
    (5399, "mid"),     # 89:59 — boundary
    (5400, "late"),    # 90:00
    (9000, "late"),    # 150 min — five-setter
])
def test_match_phase_tennis_with_elapsed_only(ticker, elapsed, expected):
    result = tag(NOW, ticker, market_state={"elapsed_seconds": elapsed})
    assert result["match_phase"] == expected


@pytest.mark.parametrize("ticker", [
    "KXATPMATCH-26APR25-FED",
    "KXWTAMATCH-26APR25-WIL",
])
@pytest.mark.parametrize("set_num, expected", [
    (1, "set_1"),
    (2, "set_2"),
    (3, "set_3+"),
    (4, "set_3+"),
    (5, "set_3+"),
])
def test_match_phase_tennis_with_set_info_overrides_elapsed(ticker, set_num, expected):
    """When set_number is present, it wins over the time fallback."""
    # elapsed alone would say "late" (>90m); set_number is what matters
    ms = {"elapsed_seconds": 6000, "set_number": set_num}
    assert tag(NOW, ticker, ms)["match_phase"] == expected


def test_match_phase_tennis_no_state():
    assert tag(NOW, "KXATPMATCH-26APR25-X", market_state=None)["match_phase"] is None
    assert tag(NOW, "KXATPMATCH-26APR25-X", market_state={})["match_phase"] is None


def test_match_phase_tennis_legacy_elapsed_key():
    """Some callers pass `elapsed` instead of `elapsed_seconds`."""
    assert tag(NOW, "KXATPMATCH-26APR25-X",
               market_state={"elapsed": 60})["match_phase"] == "early"
    assert tag(NOW, "KXATPMATCH-26APR25-X",
               market_state={"elapsed": 3600})["match_phase"] == "mid"


@pytest.mark.parametrize("elapsed, expected", [
    (0,   "round_1"),
    (299, "round_1"),
    (300, "round_2"),
    (599, "round_2"),
    (600, "round_3+"),
    (900, "round_3+"),
])
def test_match_phase_ufc_with_elapsed(elapsed, expected):
    ticker = "KXUFCFIGHT-26APR25-VIE"
    assert tag(NOW, ticker,
               market_state={"elapsed_seconds": elapsed})["match_phase"] == expected


@pytest.mark.parametrize("round_num, expected", [
    (1, "round_1"),
    (2, "round_2"),
    (3, "round_3+"),
    (4, "round_3+"),
    (5, "round_3+"),
])
def test_match_phase_ufc_with_round_info(round_num, expected):
    """When round_num is present, it wins over the time fallback."""
    ticker = "KXUFCFIGHT-26APR25-VIE"
    # elapsed alone would say "round_1" (<5min); round_num overrides
    ms = {"elapsed_seconds": 60, "round_num": round_num}
    assert tag(NOW, ticker, ms)["match_phase"] == expected


def test_match_phase_ufc_no_state():
    assert tag(NOW, "KXUFCFIGHT-26APR25-X")["match_phase"] is None
    assert tag(NOW, "KXUFCFIGHT-26APR25-X", market_state={})["match_phase"] is None


@pytest.mark.parametrize("over, expected", [
    (1,  "powerplay"),
    (3,  "powerplay"),
    (6,  "powerplay"),
    (7,  "middle"),
    (12, "middle"),
    (15, "middle"),
    (16, "death"),
    (18, "death"),
    (20, "death"),
])
def test_match_phase_ipl_with_over_count(over, expected):
    ticker = "KXIPLGAME-26APR25-CSK"
    assert tag(NOW, ticker,
               market_state={"over_count": over})["match_phase"] == expected


@pytest.mark.parametrize("over", [0, -1, 21, 99])
def test_match_phase_ipl_invalid_over_count_returns_none(over):
    """Out-of-range overs → None, not a crash."""
    ticker = "KXIPLGAME-26APR25-CSK"
    assert tag(NOW, ticker, market_state={"over_count": over})["match_phase"] is None


def test_match_phase_ipl_no_over_count_returns_none_even_with_elapsed():
    """IPL has no time fallback per spec — overs don't map cleanly to seconds
    once breaks/strategic-timeouts/innings-changes are involved."""
    ticker = "KXIPLGAME-26APR25-CSK"
    assert tag(NOW, ticker,
               market_state={"elapsed_seconds": 3600})["match_phase"] is None
    assert tag(NOW, ticker, market_state={})["match_phase"] is None


@pytest.mark.parametrize("ticker", [
    "KXNBAGAME-26APR25-LAL",  # nba
    "KXNHLGAME-26APR25-NYR",  # nhl
    "KXMLBGAME-26APR25-LAA",  # mlb
    "KXNCAAMBGAME-26APR25-X", # ncaab
    "KXF1RACE-26APR25",       # f1
    "KXWX-NYC-RAIN",          # weather
    "KXTREASURY-2YR",         # non-sport
])
def test_match_phase_for_non_listed_sport(ticker):
    """NBA/NHL/MLB/NCAAB/F1 + non-sports return None — no match_phase semantics
    in v1. Those sports either have working `period`/`completion` (sport_phase
    covers them) or aren't matches we live-watch."""
    ms = {"elapsed_seconds": 1200, "set_number": 1, "round_num": 1, "over_count": 5}
    assert tag(NOW, ticker, ms)["match_phase"] is None


def test_match_phase_handles_string_numerics():
    """JSON round-tripping sometimes turns ints into strings; coerce."""
    assert tag(NOW, "KXATPMATCH-X",
               market_state={"elapsed_seconds": "1800"})["match_phase"] == "mid"
    assert tag(NOW, "KXIPLGAME-X",
               market_state={"over_count": "12"})["match_phase"] == "middle"


def test_match_phase_handles_float_elapsed():
    assert tag(NOW, "KXATPMATCH-X",
               market_state={"elapsed_seconds": 1800.5})["match_phase"] == "mid"


def test_match_phase_rejects_bool_as_int():
    """bool is technically int in Python; explicit check to avoid True→1 mishaps."""
    # set_number=True would otherwise become "set_1"
    assert tag(NOW, "KXATPMATCH-X",
               market_state={"set_number": True})["match_phase"] is None


def test_match_phase_handles_negative_elapsed_as_none():
    """Defensive: negative elapsed shouldn't bucket as anything."""
    assert tag(NOW, "KXATPMATCH-X",
               market_state={"elapsed_seconds": -5})["match_phase"] is None


# ---------------------------------------------------------------------------
# Session 34: regression guard — existing 4 axes must produce IDENTICAL values
# pre vs post-Session-34. Only `match_phase` is new.
# ---------------------------------------------------------------------------

def test_existing_regime_fields_unchanged():
    """Frozen pre-Session-34 expected values for the 4 original axes. If this
    test fails, the Session 34 change is no longer additive — investigate
    before shipping."""
    cases = [
        # (ts, ticker, market_state, expected 4-axis dict)
        (_utc(2026, 4, 25, 18), "KXNBAGAME-26APR25-LAL", None,
         {"time_of_day": "afternoon", "day_of_week": "sat",
          "sport_phase": "playoffs", "event_horizon_hr": None}),
        (_utc(2026, 4, 25, 18), "KXNBAGAME-26APR25-LAL",
         {"close_ts": _utc(2026, 4, 25, 23).isoformat()},
         {"time_of_day": "afternoon", "day_of_week": "sat",
          "sport_phase": "playoffs", "event_horizon_hr": "2-12h"}),
        (_utc(2026, 4, 22, 12), "KXATPMATCH-26APR22-X", None,
         {"time_of_day": "morning", "day_of_week": "wed",
          "sport_phase": None, "event_horizon_hr": None}),
        (_utc(2026, 4, 25, 5), "KXIPLGAME-26APR25-CSK", None,
         {"time_of_day": "overnight", "day_of_week": "sat",
          "sport_phase": None, "event_horizon_hr": None}),
        (_utc(2025, 11, 1, 18), "KXNBAGAME-25NOV01-LAL",
         {"close_ts": (_utc(2025, 11, 1, 18) + timedelta(hours=24)).isoformat()},
         {"time_of_day": "afternoon", "day_of_week": "sat",
          "sport_phase": "regular", "event_horizon_hr": "12-48h"}),
        (_utc(2026, 4, 25, 18), "", {},
         {"time_of_day": "afternoon", "day_of_week": "sat",
          "sport_phase": None, "event_horizon_hr": None}),
    ]
    for ts, ticker, ms, expected_orig in cases:
        result = tag(ts, ticker, ms)
        for axis, val in expected_orig.items():
            assert result[axis] == val, (
                f"axis={axis} regressed for ticker={ticker!r}: "
                f"got {result[axis]!r}, expected {val!r}"
            )
        # Session 34: the 5th key exists; we don't pin its value here.
        assert "match_phase" in result
