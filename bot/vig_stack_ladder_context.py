"""Session 100: forward-only ladder context for vig_stack decisions.

Persists metadata describing each accepted opp's position within its ladder
(family, rung count, selected rung rank ascending+descending, rung
strike+kind, forecast distance, time to close, source forecast metadata).
Mirrors Session 99's live_momentum_proxy shape: pure function with no I/O,
no logging, no imports from bot.strategies. Returned dict is un-prefixed;
caller adds `paper_` prefix when merging into the opp dict so the executor's
existing rename pattern (paper_X -> X on paper_trades.json) picks it up.

v0 scope: instrumentation only. No behavior changes anywhere.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

MODEL_SOURCE = "vig_stack_ladder_context_v1"

# Canonical key list. Single source of truth for the executor inject loop
# at bot/executor.py and for the report function at tools/calibration_report.py.
# Adding/removing a key here is the only edit needed to extend the schema.
LADDER_CONTEXT_KEYS: tuple[str, ...] = (
    "family",
    "ladder_total_yes_sum_cents",
    "rung_count",
    "selected_rung_rank_asc",
    "selected_rung_rank_desc",
    "rung_strike",
    "rung_kind",
    "no_price_cents",
    "forecast_bucket_distance",
    "source_forecast_temp",
    "source_city",
    "time_to_close_hr",
    "ladder_context_source",
)


def _parse_strike(ticker: str) -> tuple[Optional[float], Optional[str]]:
    """Parse (strike_lo, kind) from the last segment of a Kalshi ticker.

    Mirrors the convention in bot/strategies/vig_stack_series._parse_weather_bucket:
        B89.5 -> (89.0, "B")  one-degree bucket starting at floor(B value)
        T73   -> (73.0, "T")  threshold (>=73)
        else  -> (None, None)
    """
    if not ticker:
        return (None, None)
    parts = ticker.split("-")
    if len(parts) < 3:
        return (None, None)
    bucket = parts[-1]
    try:
        if bucket.startswith("B"):
            return (float(int(float(bucket[1:]))), "B")
        if bucket.startswith("T"):
            return (float(bucket[1:]), "T")
    except ValueError:
        pass
    return (None, None)


def compute_ladder_context(
    *,
    ticker: str,
    valid_tickers: set[str],
    yes_sum_cents: Optional[int],
    valid_count: Optional[int],
    no_ask_cents: Optional[int],
    forecast_temp: Optional[float],
    source_city: Optional[str],
    is_weather: bool,
    close_ts: Optional[str],
    now_utc: datetime,
) -> dict[str, object | None]:
    """Compute ladder-shape context for one vig_stack opp.

    Returns un-prefixed dict. Caller adds `paper_` prefix when merging into
    the opp dict; executor renames back at persist time. Fields not
    applicable to this opp (e.g. forecast fields on a futures opp, rank
    fields on an unsortable ladder) return None; caller omits None values
    from both decisions.jsonl extras and paper_trades.json rows so
    non-applicable rows don't carry dead columns.
    """
    family = ticker.split("-")[0] if ticker else None

    this_strike, this_kind = _parse_strike(ticker)

    # selected_rung_rank — sort parseable rungs by strike ascending.
    # rank_asc=1 means lowest strike in the ladder; rank_desc=1 means highest.
    # Both directions persisted: operators may think either way about "edge of
    # forecast bound." Two ints is cheap; lets the report pivot at no extra cost.
    parsed: list[tuple[float, str]] = []
    for t in valid_tickers:
        strike, _ = _parse_strike(t)
        if strike is not None:
            parsed.append((strike, t))
    parsed.sort(key=lambda x: x[0])

    rank_asc: Optional[int] = None
    rank_desc: Optional[int] = None
    for i, (_, t) in enumerate(parsed):
        if t == ticker:
            rank_asc = i + 1
            rank_desc = len(parsed) - i
            break

    # forecast_bucket_distance — weather only. Signed convention matches
    # bot/strategies/vig_stack_series._forecast_distance_from_bucket:
    # negative when forecast is inside the bucket (magnitude = depth from
    # nearest edge), positive when outside (magnitude = gap from nearest edge).
    forecast_distance: Optional[float] = None
    if (is_weather and forecast_temp is not None
            and this_strike is not None and this_kind is not None):
        if this_kind == "B":
            lo, hi = this_strike, this_strike + 1.0
        else:  # "T" threshold
            lo, hi = this_strike, 999.0
        if lo <= forecast_temp <= hi:
            forecast_distance = -min(forecast_temp - lo, hi - forecast_temp)
        else:
            forecast_distance = min(abs(forecast_temp - lo), abs(forecast_temp - hi))
        forecast_distance = round(float(forecast_distance), 2)

    time_to_close_hr: Optional[float] = None
    if close_ts:
        try:
            close_dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
            time_to_close_hr = round((close_dt - now_utc).total_seconds() / 3600.0, 2)
        except (ValueError, TypeError):
            time_to_close_hr = None

    return {
        "family": family,
        "ladder_total_yes_sum_cents": (
            int(yes_sum_cents) if yes_sum_cents is not None else None
        ),
        "rung_count": int(valid_count) if valid_count is not None else None,
        "selected_rung_rank_asc": rank_asc,
        "selected_rung_rank_desc": rank_desc,
        "rung_strike": this_strike,
        "rung_kind": this_kind,
        "no_price_cents": int(no_ask_cents) if no_ask_cents is not None else None,
        "forecast_bucket_distance": forecast_distance,
        "source_forecast_temp": (
            round(float(forecast_temp), 2)
            if (forecast_temp is not None and is_weather) else None
        ),
        "source_city": source_city if is_weather else None,
        "time_to_close_hr": time_to_close_hr,
        "ladder_context_source": MODEL_SOURCE,
    }
