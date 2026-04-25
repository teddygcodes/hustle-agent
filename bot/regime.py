"""
Pure regime tagging for records (Session 14).

Returns a fixed-key dict with axes:
- time_of_day:       morning|afternoon|evening|overnight   (America/New_York buckets)
- day_of_week:       mon|tue|wed|thu|fri|sat|sun           (America/New_York calendar day)
- sport_phase:       preseason|regular|playoffs|off|None   (NBA/NHL/MLB/NCAAB only in v1)
- event_horizon_hr:  <2h|2-12h|12-48h|48-168h|>168h|None   (hours-to-settle bucket)

v1 known gaps:
- ESPN integration deferred. sport_phase comes from a hardcoded date table that
  needs a yearly bump (see SPORT_PHASES below). live_watcher caches per-game live
  state, NOT season schedule, so reuse wasn't possible.
- ATP/WTA/UFC/IPL/F1 sport_phase returns None (no clean preseason/regular/playoffs
  semantics; can be populated in a future revision).
- market_vol_tier deferred — requires per-ticker price history infra (live_ticks.jsonl
  exists for live markets but not vig_stack tickers). Worth its own session.

Pure function: same inputs → same output. No I/O. No clock reads.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Sport ticker prefix → sport key. Sourced from bot/kalshi_series.py SPORTS_SERIES /
# MATCH_SERIES and bot/config.py championship-futures prefixes. Longer prefixes match
# first (KXNBAGAME beats KXNBA) via the sort in _ticker_to_sport.
SPORT_PREFIXES: dict[str, str] = {
    "KXNBAGAME": "nba",
    "KXNHLGAME": "nhl",
    "KXMLBGAME": "mlb",
    "KXNCAAMBGAME": "ncaab",
    "KXATPMATCH": "atp",
    "KXATPCHALLENGERMATCH": "atp_challenger",
    "KXWTAMATCH": "wta",
    "KXWTACHALLENGERMATCH": "wta_challenger",
    "KXUFCFIGHT": "ufc",
    "KXIPLGAME": "ipl",
    "KXF1RACE": "f1",
    "KXNBA": "nba",  # championship futures
    "KXNHL": "nhl",
    "KXMLB": "mlb",
}

# Sport phase windows. Format per sport: list of (start_date, end_date_inclusive, phase).
# Anything outside listed ranges → "off". Update at the start of each cycle.
# Sources: official 2025-26 league schedules.
SPORT_PHASES: dict[str, list[tuple[date, date, str]]] = {
    "nba": [
        (date(2025, 10, 1),  date(2025, 10, 21), "preseason"),
        (date(2025, 10, 22), date(2026, 4, 12),  "regular"),
        (date(2026, 4, 13),  date(2026, 6, 22),  "playoffs"),
    ],
    "nhl": [
        (date(2025, 9, 21),  date(2025, 10, 6),  "preseason"),
        (date(2025, 10, 7),  date(2026, 4, 16),  "regular"),
        (date(2026, 4, 18),  date(2026, 6, 22),  "playoffs"),
    ],
    "mlb": [
        (date(2026, 2, 21),  date(2026, 3, 25),  "preseason"),
        (date(2026, 3, 26),  date(2026, 9, 28),  "regular"),
        (date(2026, 9, 29),  date(2026, 11, 4),  "playoffs"),
    ],
    "ncaab": [
        (date(2025, 11, 3),  date(2026, 3, 14),  "regular"),
        (date(2026, 3, 17),  date(2026, 4, 6),   "playoffs"),
    ],
}

_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REGIME_KEYS: tuple[str, ...] = ("time_of_day", "day_of_week", "sport_phase", "event_horizon_hr")


def _ticker_to_sport(ticker: str) -> str | None:
    if not ticker:
        return None
    for prefix in sorted(SPORT_PREFIXES, key=len, reverse=True):
        if ticker.startswith(prefix):
            return SPORT_PREFIXES[prefix]
    return None


def _sport_phase(d: date, sport: str | None) -> str | None:
    if sport is None:
        return None
    ranges = SPORT_PHASES.get(sport)
    if not ranges:
        return None
    for start, end, phase in ranges:
        if start <= d <= end:
            return phase
    return "off"


def _time_of_day(hour_et: int) -> str:
    if 6 <= hour_et < 12:
        return "morning"
    if 12 <= hour_et < 18:
        return "afternoon"
    if 18 <= hour_et < 24:
        return "evening"
    return "overnight"  # 0-6


def _event_horizon_hr(now_utc: datetime, close_iso) -> str | None:
    if not isinstance(close_iso, str) or not close_iso:
        return None
    try:
        close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if close_dt.tzinfo is None:
        close_dt = close_dt.replace(tzinfo=timezone.utc)
    hours = (close_dt - now_utc).total_seconds() / 3600.0
    if hours < 2:
        return "<2h"
    if hours < 12:
        return "2-12h"
    if hours < 48:
        return "12-48h"
    if hours < 168:
        return "48-168h"
    return ">168h"


def tag(ts: datetime, ticker: str, market_state: dict | None = None) -> dict:
    """Pure regime tagger. Same inputs → same output. No I/O.

    Args:
      ts: datetime of the record. tz-aware preferred; naive treated as UTC.
      ticker: Kalshi ticker string (may be empty).
      market_state: optional dict; checked for 'close_ts' or 'close_time'.

    Returns dict with keys: time_of_day, day_of_week, sport_phase, event_horizon_hr.
    Each axis is best-effort; missing inputs → None for that axis.

    Raises ValueError if ts is not a datetime.
    """
    if not isinstance(ts, datetime):
        raise ValueError(f"ts must be datetime, got {type(ts).__name__}")

    ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    ts_et = ts_utc.astimezone(_ET)

    sport = _ticker_to_sport(ticker)

    close_iso = None
    if isinstance(market_state, dict):
        close_iso = market_state.get("close_ts") or market_state.get("close_time")

    return {
        "time_of_day": _time_of_day(ts_et.hour),
        "day_of_week": _DOW[ts_et.weekday()],
        "sport_phase": _sport_phase(ts_et.date(), sport),
        "event_horizon_hr": _event_horizon_hr(ts_utc, close_iso),
    }
