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
    sports = ["KXNBAGAME-X", "KXMLBGAME-Y", "KXNHLGAME-Z", "KXWX-NYC", "KXTREASURY"]
    for _ in range(100):
        ts = _utc(2026, 1, 1) + timedelta(minutes=rng.randint(0, 525_600))
        ticker = rng.choice(sports)
        if rng.random() < 0.7:
            close_ts = (ts + timedelta(hours=rng.uniform(0.1, 300))).isoformat()
            ms = {"close_ts": close_ts}
        else:
            ms = None
        a = tag(ts, ticker, ms)
        b = tag(ts, ticker, ms)
        assert a == b
        assert set(a.keys()) == {"time_of_day", "day_of_week", "sport_phase", "event_horizon_hr"}


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
