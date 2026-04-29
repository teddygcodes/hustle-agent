"""
Pure regime tagging for records (Session 14, extended Session 34).

Returns a fixed-key dict with axes:
- time_of_day:       morning|afternoon|evening|overnight   (America/New_York buckets)
- day_of_week:       mon|tue|wed|thu|fri|sat|sun           (America/New_York calendar day)
- sport_phase:       preseason|regular|playoffs|off|None   (NBA/NHL/MLB/NCAAB only in v1)
- event_horizon_hr:  <2h|2-12h|12-48h|48-168h|>168h|None   (hours-to-settle bucket)
- match_phase:       per-sport in-match position bucket | None   (Session 34, v1)

v1 known gaps:
- ESPN integration deferred. sport_phase comes from a hardcoded date table that
  needs a yearly bump (see SPORT_PHASES below). live_watcher caches per-game live
  state, NOT season schedule, so reuse wasn't possible.
- ATP/WTA/UFC/IPL/F1 sport_phase returns None (no clean preseason/regular/playoffs
  semantics; can be populated in a future revision).
- market_vol_tier deferred — requires per-ticker price history infra (live_ticks.jsonl
  exists for live markets but not vig_stack tickers). Worth its own session.

match_phase v1 (Session 34):
- Tennis (atp / atp_challenger / wta / wta_challenger):
    state path → set_1 | set_2 | set_3+ (when market_state["set_number"] is int)
    elapsed path → early (<30m) | mid (30-90m) | late (>90m)
                   (when market_state["elapsed_seconds"] or "elapsed" is numeric)
    else → None
- UFC:
    state path → round_1 | round_2 | round_3+ (when market_state["round_num"] is int)
    elapsed path → round_1 (<5m) | round_2 (5-10m) | round_3+ (>10m)
    else → None
- IPL (cricket):
    state path → powerplay (overs 1-6) | middle (7-15) | death (16-20)
                 (when market_state["over_count"] is int in 1..20)
    no elapsed fallback (overs don't map cleanly to wall-clock time during
    breaks; underestimating is worse than null)
    else → None
- All other sports (nba/nhl/mlb/ncaab/f1/etc.): None. Those sports either have
  working `period`/`completion` already (sport_phase covers them) or aren't
  matches we currently bet via live_watcher.

The state-path / elapsed-path split is intentional: today neither ESPN nor the
Kalshi market dict exposes set_number / round_num / over_count for these
sports, so live_watcher can only thread elapsed_seconds. The state path is
forward-compatible — when a future session sources the rich data, this tagger
flips automatically without touching writers.

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
REGIME_KEYS: tuple[str, ...] = (
    "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase",
)

# Sports that get a non-None match_phase. Anything not in here returns None.
_MATCH_PHASE_SPORTS: frozenset[str] = frozenset({
    "atp", "atp_challenger", "wta", "wta_challenger", "ufc", "ipl",
})


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


def _coerce_int(v) -> int | None:
    """Best-effort int coercion. None / non-numeric / NaN → None."""
    if v is None or isinstance(v, bool):  # bool is int; reject explicitly
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None
    return None


def _match_phase(sport: str | None, market_state: dict | None) -> str | None:
    """Per-sport in-match position bucket. See module docstring for taxonomy.

    Reads from market_state in this priority order, per sport:
    - tennis: set_number (int) → state path; else elapsed_seconds/elapsed (sec)
              → time path; else None.
    - ufc: round_num (int) → state path; else elapsed_seconds/elapsed → time
           path; else None.
    - ipl: over_count (int 1..20) → state path; else None (no time fallback).
    """
    if sport not in _MATCH_PHASE_SPORTS:
        return None
    if not isinstance(market_state, dict):
        return None

    # Common: elapsed in seconds (live_watcher emits this as elapsed_seconds;
    # legacy callers may use elapsed). Both numeric.
    elapsed = _coerce_int(
        market_state.get("elapsed_seconds")
        if market_state.get("elapsed_seconds") is not None
        else market_state.get("elapsed")
    )

    if sport in ("atp", "atp_challenger", "wta", "wta_challenger"):
        set_num = _coerce_int(market_state.get("set_number"))
        if set_num is not None and set_num >= 1:
            if set_num == 1:
                return "set_1"
            if set_num == 2:
                return "set_2"
            return "set_3+"
        if elapsed is not None and elapsed >= 0:
            if elapsed < 1800:        # <30 min
                return "early"
            if elapsed < 5400:        # 30-90 min
                return "mid"
            return "late"
        return None

    if sport == "ufc":
        round_num = _coerce_int(market_state.get("round_num"))
        if round_num is not None and round_num >= 1:
            if round_num == 1:
                return "round_1"
            if round_num == 2:
                return "round_2"
            return "round_3+"
        if elapsed is not None and elapsed >= 0:
            if elapsed < 300:         # <5 min
                return "round_1"
            if elapsed < 600:         # 5-10 min
                return "round_2"
            return "round_3+"
        return None

    if sport == "ipl":
        over = _coerce_int(market_state.get("over_count"))
        if over is None or over < 1 or over > 20:
            return None
        if over <= 6:
            return "powerplay"
        if over <= 15:
            return "middle"
        return "death"

    return None


def tag(ts: datetime, ticker: str, market_state: dict | None = None) -> dict:
    """Pure regime tagger. Same inputs → same output. No I/O.

    Args:
      ts: datetime of the record. tz-aware preferred; naive treated as UTC.
      ticker: Kalshi ticker string (may be empty).
      market_state: optional dict; checked for 'close_ts' / 'close_time' (event
        horizon) and per-sport match-phase keys ('set_number', 'round_num',
        'over_count', 'elapsed_seconds' / 'elapsed') — see module docstring.

    Returns dict with keys: time_of_day, day_of_week, sport_phase,
    event_horizon_hr, match_phase. Each axis is best-effort; missing inputs →
    None for that axis.

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
        "match_phase": _match_phase(sport, market_state),
    }
