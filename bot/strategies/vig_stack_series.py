"""VigStackSeries — Session 13a refactor of scan_vig_stack_series.

Behavior-preserving port: same constants, same gates, same reason
strings, same opportunity dict shape, same stratified CF emission.
The only change is SHAPE (function -> class). See CLAUDE.md Session
13 block for context.

Handles three market families in one class because the original
function did:
  - Weather (KXHIGH*, KXTEMP*, ...) -> opp_type 'vig_stack_series'
  - Index ranges (KXINX, ...) -> opp_type 'vig_stack_series'
  - Sports futures (KXNBA*, ...) -> opp_type 'vig_stack_futures'
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from bot.config import (
    INDEX_RANGE_SERIES_TICKERS,
    SPORTS_FUTURES_TICKERS,
    VIG_STACK_MIN_NO_ENTRY_PRICE,
    VIG_STACK_STABLE_FAMILIES,
    VIG_STACK_WEATHER_MIN_PRICE,
    WEATHER_SERIES_TICKERS,
)
from bot.math_engine import _self_check_edge
from bot.strategies import Market, Opportunity

logger = logging.getLogger(__name__)


# Hardcoded floor (was bot/scanner.py:908) — not in config.py.
VIG_STACK_MIN_EDGE = 0.02


# ---------------------------------------------------------------------------
# Helpers — verbatim ports from bot/scanner.py
# ---------------------------------------------------------------------------

# Was bot/scanner.py:435-454.
_SERIES_TO_NWS: dict[str, str] = {
    "KXHIGHNY":  "NYC",
    "KXHIGHAUS": "Austin",
    "KXHIGHCHI": "Chicago",
    "KXHIGHDEN": "Denver",
    "KXHIGHMIA": "Miami",
    "KXHIGHBOS": "Boston",
    "KXHIGHDC":  "DC",
    "KXHIGHSF":  "SF",
    "KXHIGHLA":  "LA",
    "KXHIGHSEA": "Seattle",
    "KXHIGHPHO": "Phoenix",
    "KXHIGHDAL": "Dallas",
    "KXHIGHATL": "Atlanta",
    "KXHIGHPHL": "Philadelphia",
    "KXHIGHLV":  "Las Vegas",
    "KXHIGHPDX": "Portland",
    "KXHIGHMIN": "Minneapolis",
    "KXHIGHNSH": "Nashville",
}


def _forecast_distance_from_bucket(forecast_temp: float, lo: float, hi: float) -> float:
    """Signed distance in degrees from a forecast to a contract bucket.

    Negative when the forecast is INSIDE [lo, hi] (magnitude = depth from
    nearest edge). Positive when the forecast is OUTSIDE the bucket
    (magnitude = gap from nearest edge). Used by the forecast_in_bucket
    reject log so cohort_report can distinguish "deep inside" from
    "just outside the ±2° margin".
    """
    if lo <= forecast_temp <= hi:
        return -min(forecast_temp - lo, hi - forecast_temp)
    return min(abs(forecast_temp - lo), abs(forecast_temp - hi))


def _parse_weather_bucket(ticker: str) -> Optional[tuple[float, float]]:
    """Parse a weather ticker into (low, high) temperature range.

    Examples:
        KXHIGHNY-26APR15-B89.5  -> (89.0, 90.0)   "between" bucket
        KXHIGHCHI-26APR15-T73   -> (73.0, 999.0)  "threshold" (>=73 F)

    Returns None if the ticker can't be parsed.
    """
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    bucket_part = parts[-1]
    try:
        if bucket_part.startswith("B"):
            low = float(bucket_part[1:])
            return (float(int(low)), float(int(low) + 1))
        elif bucket_part.startswith("T"):
            threshold = float(bucket_part[1:])
            return (threshold, 999.0)
    except ValueError:
        pass
    return None


def _fetch_vig_stack_forecasts() -> dict[str, float]:
    """Fetch NWS forecasts for all weather series cities.

    Returns {series_ticker: forecast_high_F} for tomorrow's daytime high.
    Only includes cities where a forecast was successfully fetched.
    """
    from bot.scanner_weather import _fetch_nws_forecast, NWS_CITIES
    forecasts: dict[str, float] = {}
    for series_ticker, nws_city in _SERIES_TO_NWS.items():
        if nws_city not in NWS_CITIES:
            continue
        lat, lon = NWS_CITIES[nws_city]
        fc = _fetch_nws_forecast(nws_city, lat, lon)
        if not fc:
            continue
        for period in fc.get("periods", []):
            name = period.get("name", "").lower()
            if "night" in name:
                continue
            temp = period.get("temperature")
            if temp is not None:
                forecasts[series_ticker] = float(temp)
                break
    return forecasts


# Gate fingerprint order matches scan_vig_stack_series exactly. Reject
# rows record earlier gates as True (passed), the rejecting gate as
# False, downstream gates omitted.
_VIG_STACK_GATES = [
    "low_liquidity", "no_vig", "market_closed", "forecast_in_bucket",
    "no_price_too_low", "price_floor", "edge_below_threshold", "self_check",
]
_VIG_STACK_REASON_TO_GATE = {
    "low_liquidity": "low_liquidity",
    "no_vig": "no_vig",
    "market_closed": "market_closed",
    "forecast_in_bucket": "forecast_in_bucket",
    "no_price_too_low": "no_price_too_low",
    "no_price_below_floor": "price_floor",
    "non_stable_below_weather_floor": "price_floor",
    "edge_below_threshold": "edge_below_threshold",
    "self_check_failed": "self_check",
}


def _vig_stack_gate_fingerprint(reason: str) -> dict[str, bool]:
    gate = _VIG_STACK_REASON_TO_GATE.get(reason, reason)
    out: dict[str, bool] = {}
    for name in _VIG_STACK_GATES:
        if name == gate:
            out[name] = False
            return out
        out[name] = True
    return out


def _stratified_cf_rejects(
    rejected_opps: list[dict],
    *,
    per_gate_top_k: int = 1,
    total_budget: int = 10,
    hard_cap: int = 15,
) -> list[dict]:
    """Select which rejected opps get a counterfactual CLV record.

    Verbatim port from bot/scanner.py. Two-stage sampling:
      1. Stratified core — per (opp_type, skip_reason), top-K by edge.
      2. Budget fill — highest-edge leftovers up to total_budget.

    Dedup by ticker (higher edge wins). Hard cap on final size.

    Eligibility: edge is not None AND price >= 3 cents.
    """
    eligible = [
        o for o in rejected_opps
        if o.get("edge") is not None
        and (o.get("price_cents") or o.get("yes_ask") or 0) >= 3
    ]
    if not eligible:
        return []

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for o in eligible:
        key = (
            o.get("opp_type") or o.get("type") or "",
            o.get("skip_reason") or "",
        )
        groups[key].append(o)

    core: list[dict] = []
    for grp in groups.values():
        grp.sort(key=lambda o: -o["edge"])
        core.extend(grp[:per_gate_top_k])

    seen: set[str] = set()
    selected: list[dict] = []
    for o in sorted(core, key=lambda o: -o["edge"]):
        t = o.get("ticker") or ""
        if t and t not in seen:
            seen.add(t)
            selected.append(o)

    for o in sorted(eligible, key=lambda o: -o["edge"]):
        if len(selected) >= total_budget:
            break
        t = o.get("ticker") or ""
        if t and t not in seen:
            seen.add(t)
            selected.append(o)

    return selected[:hard_cap]


# ---------------------------------------------------------------------------
# VigStackSeries strategy class
# ---------------------------------------------------------------------------

class VigStackSeries:
    """Strategy class for vig-stack-series + vig-stack-futures arbs.

    Handles three market families (weather, index_range, sports_futures)
    and emits two opp_types ('vig_stack_series' for weather+index,
    'vig_stack_futures' for sports futures) — preserved from the legacy
    scan_vig_stack_series function which did all three under one body.
    """

    name = "vig_stack_series"

    def __init__(self) -> None:
        # Per-scan state, reset in candidate_markets:
        self._ladders: dict[str, dict] = {}  # ladder_id -> ladder context
        self._ladder_id_for: dict[str, str] = {}  # ticker -> ladder_id
        self._nws_forecasts: dict[str, float] = {}  # series_ticker -> temp_F
        self._rejected_opps: list[dict] = []
        self._telem: dict[str, dict[str, int]] = self._fresh_telem()
        self._now_utc: datetime = datetime.now(timezone.utc)

    @staticmethod
    def _fresh_telem() -> dict[str, dict[str, int]]:
        keys = ["checked", "surfaced", "low_liquidity", "no_vig",
                "market_closed", "forecast_in_bucket", "no_price_too_low",
                "no_price_below_floor", "non_stable_below_weather_floor",
                "edge_below_threshold", "self_check_failed"]
        return {
            "series": {k: 0 for k in keys},
            "futures": {k: 0 for k in keys},
        }

    def name_for(self, market: Market) -> str:
        """Preserve historical attribution: futures family attributes
        as 'vig_stack_futures', everything else as 'vig_stack_series'.
        Was bot/scanner.py:710."""
        if market.series_ticker in set(SPORTS_FUTURES_TICKERS):
            return "vig_stack_futures"
        return "vig_stack_series"

    def candidate_markets(self, universe: list[Market]) -> list[Market]:
        """Group universe markets into per-event ladders.

        Resets per-scan state. Mirrors the structural-filter phase of
        scan_vig_stack_series (bot/scanner.py:660-707):
          - Restrict to weather + index_range + sports_futures families
          - Drop ladders with <2 markets
          - Index series: keep only -B (range) contracts; group by event
          - Weather/futures: all contracts in series form one ladder

        Note: low_liquidity / no_vig gates are applied lazily on
        first-evaluate per ladder, exactly as the legacy code does
        (legacy applies them inline during the per-event loop).
        """
        self._ladders.clear()
        self._ladder_id_for.clear()
        self._nws_forecasts.clear()
        self._rejected_opps.clear()
        self._telem = self._fresh_telem()
        self._now_utc = datetime.now(timezone.utc)

        weather_set = set(WEATHER_SERIES_TICKERS)
        index_set = set(INDEX_RANGE_SERIES_TICKERS)
        futures_set = set(SPORTS_FUTURES_TICKERS)
        all_active = weather_set | index_set | futures_set

        # Pre-fetch NWS forecasts for the weather families (legacy did
        # this once before the series loop).
        self._nws_forecasts = _fetch_vig_stack_forecasts()
        if self._nws_forecasts:
            logger.info("VigStack NWS forecasts: %s",
                        ", ".join(f"{k.replace('KXHIGH','')}={v:.0f}°F"
                                  for k, v in sorted(self._nws_forecasts.items())))

        # Group universe markets by series_ticker (only the families we handle)
        by_series: dict[str, list[Market]] = {}
        for m in universe:
            if m.series_ticker not in all_active:
                continue
            by_series.setdefault(m.series_ticker, []).append(m)

        # Iterate series in the same order legacy does (config order) so
        # log_decision call sequences stay byte-identical when comparing.
        ordered_series = (list(WEATHER_SERIES_TICKERS)
                          + list(INDEX_RANGE_SERIES_TICKERS)
                          + list(SPORTS_FUTURES_TICKERS))

        candidates: list[Market] = []
        for series_ticker in ordered_series:
            series_markets = by_series.get(series_ticker)
            if not series_markets or len(series_markets) < 2:
                continue
            is_weather = series_ticker in weather_set
            is_futures = series_ticker in futures_set

            # Index series: keep only -B (range) contracts; threshold (T)
            # contracts overlap and would double-count the vig.
            if not is_weather and not is_futures:
                series_markets = [m for m in series_markets if "-B" in m.ticker]

            # Group by event (date/time prefix) for index series.
            # Weather/futures: all contracts are one ladder.
            if not is_weather and not is_futures:
                by_event: dict[str, list[Market]] = {}
                for m in series_markets:
                    parts = m.ticker.split("-")
                    ev = "-".join(parts[:2]) if len(parts) >= 2 else "unknown"
                    by_event.setdefault(ev, []).append(m)
                event_groups = list(by_event.values())
            else:
                event_groups = [series_markets]

            family = "futures" if is_futures else "weather" if is_weather else "index"
            for idx, event_markets in enumerate(event_groups):
                ladder_id = f"{series_ticker}#{idx}"
                self._ladders[ladder_id] = {
                    "ladder_id": ladder_id,
                    "is_futures": is_futures,
                    "is_weather": is_weather,
                    "family": family,
                    "series_ticker": series_ticker,
                    "markets": list(event_markets),
                    "first_encounter_done": False,
                }
                for m in event_markets:
                    self._ladder_id_for[m.ticker] = ladder_id
                    candidates.append(m)
        return candidates

    def _opp_type_for(self, ladder: dict) -> str:
        return "vig_stack_futures" if ladder["is_futures"] else "vig_stack_series"

    def _sub_for(self, ladder: dict) -> str:
        return "futures" if ladder["is_futures"] else "series"

    def _build_reject_opp(self, ladder: dict, market: Market,
                          no_ask: int, no_fair_cents: float,
                          relative_no_edge: float, reason: str) -> dict:
        """Lifted from bot/scanner.py:814-826."""
        opp_type = self._opp_type_for(ladder)
        return {
            "ticker": market.ticker,
            "title": market.raw.get("title", "") if market.raw else "",
            "type": opp_type,
            "opp_type": opp_type,
            "side": "no",
            "recommended_side": "no",
            "price_cents": no_ask,
            "fair_value_cents": round(no_fair_cents, 2),
            "edge": round(relative_no_edge, 4),
            "skip_reason": reason,
        }

    def _do_first_encounter(self, ladder: dict) -> None:
        """Per-ladder pre-compute (idempotent). Applies low_liquidity to
        every market in the ladder, computes yes_sum, runs the no_vig
        gate. Equivalent to bot/scanner.py:711-768.

        Side-effects: log_decision calls for low_liquidity (per-market)
        and no_vig (once per ladder); telemetry counters; populates
        ladder["yes_sum"], ladder["vig_factor"], ladder["valid_tickers"],
        ladder["low_liq_tickers"], ladder["short_ladder"], ladder["no_vig_rejected"].
        """
        if ladder["first_encounter_done"]:
            return
        ladder["first_encounter_done"] = True
        from bot import decisions  # lazy import (matches legacy)

        sub = self._sub_for(ladder)
        opp_type = self._opp_type_for(ladder)
        is_futures = ladder["is_futures"]

        valid: list[Market] = []
        low_liq_tickers: set[str] = set()
        for m in ladder["markets"]:
            ya = m.yes_ask
            na = m.no_ask
            if not (ya and ya > 0 and na and na > 0):
                # Silently skip markets without prices — legacy does the
                # same (no telemetry, no log_decision).
                continue
            # Match legacy: lifetime `volume` field (Kalshi `volume_fp`),
            # not the 24h rolling window. universe.jsonl stores both.
            volume = (m.raw.get("volume") if m.raw else None) or 0
            open_interest = m.open_interest or 0
            if volume < 10 and open_interest < 5:
                self._telem[sub]["low_liquidity"] += 1
                decisions.log_decision(
                    ticker=m.ticker,
                    opp_type=opp_type,
                    edge=None,
                    gates=_vig_stack_gate_fingerprint("low_liquidity"),
                    decision="reject",
                    reason="low_liquidity",
                    extra={"volume": volume, "open_interest": open_interest,
                           "min_volume": 10, "min_open_interest": 5},
                )
                low_liq_tickers.add(m.ticker)
                continue
            valid.append(m)

        ladder["low_liq_tickers"] = low_liq_tickers

        if len(valid) < 2:
            ladder["short_ladder"] = True
            ladder["valid_tickers"] = set()
            return
        ladder["short_ladder"] = False

        yes_sum = sum(m.yes_ask for m in valid)
        yes_sum_prob = yes_sum / 100.0

        if yes_sum_prob < 1.05:
            self._telem[sub]["no_vig"] += len(valid)
            decisions.log_decision(
                ticker=valid[0].ticker if valid else "",
                opp_type=opp_type,
                edge=None,
                gates=_vig_stack_gate_fingerprint("no_vig"),
                decision="reject",
                reason="no_vig",
                extra={"yes_sum": yes_sum, "group_size": len(valid)},
            )
            ladder["no_vig_rejected"] = True
            ladder["valid_tickers"] = {m.ticker for m in valid}
            return
        ladder["no_vig_rejected"] = False

        ladder["yes_sum"] = yes_sum
        ladder["yes_sum_prob"] = yes_sum_prob
        ladder["vig_factor"] = yes_sum_prob
        ladder["valid_tickers"] = {m.ticker for m in valid}
        ladder["valid_count"] = len(valid)

        logger.debug(
            "VigStack/%s %d contracts | YES sum=%s¢ (%.1f%%) | vig_excess=%s¢",
            ladder["series_ticker"], len(valid), yes_sum,
            yes_sum_prob * 100, yes_sum - 100,
        )

    def evaluate(self, market: Market) -> Optional[Opportunity]:
        """Apply edge math + gates to a single market. Mirrors
        bot/scanner.py:770-996."""
        from bot import decisions  # lazy import (matches legacy)

        ladder_id = self._ladder_id_for.get(market.ticker)
        if ladder_id is None:
            return None
        ladder = self._ladders[ladder_id]
        self._do_first_encounter(ladder)

        # Markets that didn't survive the ladder-level gates produce no
        # opportunity. low_liquidity and no_vig were already logged in
        # _do_first_encounter; we just propagate None here.
        if market.ticker in ladder["low_liq_tickers"]:
            return None
        if ladder.get("short_ladder"):
            return None
        if ladder.get("no_vig_rejected"):
            return None
        if market.ticker not in ladder["valid_tickers"]:
            return None

        sub = self._sub_for(ladder)
        opp_type = self._opp_type_for(ladder)
        is_weather = ladder["is_weather"]
        is_futures = ladder["is_futures"]
        series_ticker = ladder["series_ticker"]
        vig_factor = ladder["vig_factor"]
        yes_sum = ladder["yes_sum"]
        yes_sum_prob = ladder["yes_sum_prob"]
        valid_count = ladder["valid_count"]

        ticker = market.ticker
        title = market.raw.get("title", "") if market.raw else ""
        yes_ask = market.yes_ask
        no_ask = market.no_ask
        self._telem[sub]["checked"] += 1

        # Skip CLOSED weather contracts — once temperature is observed
        # the prices snap to 0/100¢ and vig disappears.
        if is_weather:
            close_str = (market.raw.get("close_time")
                         or market.raw.get("expiration_time")
                         or market.close_ts) if market.raw else market.close_ts
            if close_str:
                try:
                    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                    if close_dt <= self._now_utc:
                        self._telem[sub]["market_closed"] += 1
                        decisions.log_decision(
                            ticker=ticker,
                            opp_type=opp_type,
                            edge=None,
                            gates=_vig_stack_gate_fingerprint("market_closed"),
                            decision="reject",
                            reason="market_closed",
                        )
                        return None
                except (ValueError, TypeError):
                    pass

        yes_fair_cents = yes_ask / vig_factor
        no_fair_cents = 100.0 - yes_fair_cents
        no_ask_prob = no_ask / 100.0
        no_fair_prob = no_fair_cents / 100.0
        no_edge = no_fair_prob - no_ask_prob
        relative_no_edge = no_edge / no_ask_prob if no_ask_prob > 0 else 0.0

        # forecast_in_bucket gate (weather only)
        if is_weather and series_ticker in self._nws_forecasts:
            forecast_temp = self._nws_forecasts[series_ticker]
            bucket = _parse_weather_bucket(ticker)
            if bucket:
                lo, hi = bucket
                if lo - 2 <= forecast_temp <= hi + 2:
                    logger.info(
                        "VigStack FORECAST SKIP: %s — NWS=%s°F lands near "
                        "bucket %.0f–%.0f°F (±2° margin)",
                        ticker, forecast_temp, lo, hi,
                    )
                    self._telem[sub]["forecast_in_bucket"] += 1
                    decisions.log_decision(
                        ticker=ticker,
                        opp_type=opp_type,
                        edge=round(relative_no_edge, 4),
                        gates=_vig_stack_gate_fingerprint("forecast_in_bucket"),
                        decision="reject",
                        reason="forecast_in_bucket",
                        extra={"forecast_temp": forecast_temp,
                               "bucket_lo": lo, "bucket_hi": hi,
                               "distance": round(_forecast_distance_from_bucket(forecast_temp, lo, hi), 2)},
                    )
                    self._rejected_opps.append(self._build_reject_opp(
                        ladder, market, no_ask, no_fair_cents, relative_no_edge,
                        "forecast_in_bucket"))
                    return None

        # Skip near-zero-price NO contracts (YES near-certain)
        if no_ask_prob < 0.03:
            self._telem[sub]["no_price_too_low"] += 1
            decisions.log_decision(
                ticker=ticker, opp_type=opp_type,
                edge=round(relative_no_edge, 4),
                gates=_vig_stack_gate_fingerprint("no_price_too_low"),
                decision="reject", reason="no_price_too_low",
                extra={"no_ask_prob": round(no_ask_prob, 4)},
            )
            self._rejected_opps.append(self._build_reject_opp(
                ladder, market, no_ask, no_fair_cents, relative_no_edge,
                "no_price_too_low"))
            return None

        # Family-aware NO entry price floor (Apr 18 evening, data-driven).
        fam = ticker.split("-")[0] if ticker else ""
        if fam in VIG_STACK_STABLE_FAMILIES:
            if no_ask_prob < VIG_STACK_MIN_NO_ENTRY_PRICE:
                self._telem[sub]["no_price_below_floor"] += 1
                decisions.log_decision(
                    ticker=ticker, opp_type=opp_type,
                    edge=round(relative_no_edge, 4),
                    gates=_vig_stack_gate_fingerprint("no_price_below_floor"),
                    decision="reject", reason="no_price_below_floor",
                    extra={"no_ask_prob": round(no_ask_prob, 4),
                           "floor": VIG_STACK_MIN_NO_ENTRY_PRICE,
                           "family": fam},
                )
                self._rejected_opps.append(self._build_reject_opp(
                    ladder, market, no_ask, no_fair_cents, relative_no_edge,
                    "no_price_below_floor"))
                return None
        else:
            if no_ask_prob < VIG_STACK_WEATHER_MIN_PRICE:
                self._telem[sub]["non_stable_below_weather_floor"] += 1
                decisions.log_decision(
                    ticker=ticker, opp_type=opp_type,
                    edge=round(relative_no_edge, 4),
                    gates=_vig_stack_gate_fingerprint("non_stable_below_weather_floor"),
                    decision="reject", reason="non_stable_below_weather_floor",
                    extra={"no_ask_prob": round(no_ask_prob, 4),
                           "floor": VIG_STACK_WEATHER_MIN_PRICE,
                           "family": fam},
                )
                self._rejected_opps.append(self._build_reject_opp(
                    ladder, market, no_ask, no_fair_cents, relative_no_edge,
                    "non_stable_below_weather_floor"))
                return None

        # edge_below_threshold gate
        if relative_no_edge < VIG_STACK_MIN_EDGE:
            self._telem[sub]["edge_below_threshold"] += 1
            close_str = (market.raw.get("close_time")
                         or market.raw.get("expiration_time")
                         or market.close_ts) if market.raw else market.close_ts
            tts_hr: Optional[float] = None
            if close_str:
                try:
                    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                    tts_hr = round((close_dt - self._now_utc).total_seconds() / 3600.0, 2)
                except (ValueError, TypeError):
                    tts_hr = None
            decisions.log_decision(
                ticker=ticker, opp_type=opp_type,
                edge=round(relative_no_edge, 4),
                gates=_vig_stack_gate_fingerprint("edge_below_threshold"),
                decision="reject", reason="edge_below_threshold",
                extra={"min_edge": VIG_STACK_MIN_EDGE,
                       "edge": round(relative_no_edge, 4),
                       "vig": round(yes_sum - 100.0, 2),
                       "time_to_settle_hr": tts_hr},
            )
            self._rejected_opps.append(self._build_reject_opp(
                ladder, market, no_ask, no_fair_cents, relative_no_edge,
                "edge_below_threshold"))
            return None

        # self_check
        check_ok, check_msg = _self_check_edge(no_fair_prob, no_ask_prob, no_edge)
        if not check_ok:
            self._telem[sub]["self_check_failed"] += 1
            decisions.log_decision(
                ticker=ticker, opp_type=opp_type,
                edge=round(relative_no_edge, 4),
                gates=_vig_stack_gate_fingerprint("self_check_failed"),
                decision="reject", reason="self_check_failed",
                extra={"check_msg": check_msg[:100] if check_msg else ""},
            )
            self._rejected_opps.append(self._build_reject_opp(
                ladder, market, no_ask, no_fair_cents, relative_no_edge,
                "self_check_failed"))
            return None

        # Accept path
        math_chain = [
            f"Series: {series_ticker} | {valid_count} contracts",
            f"YES sum: {yes_sum}¢ ({yes_sum_prob:.3f}) — vig_factor={vig_factor:.4f}",
            f"This contract YES_ask={yes_ask}¢",
            f"YES_fair = {yes_ask}¢ / {vig_factor:.4f} = {yes_fair_cents:.2f}¢",
            f"NO_fair = 100 - {yes_fair_cents:.2f} = {no_fair_cents:.2f}¢",
            f"NO_ask = {no_ask}¢ | Edge = {no_edge:.4f} ({relative_no_edge:.1%} relative)",
            check_msg,
        ]

        logger.debug(
            "VigStack/%s YES_ask=%s¢ YES_fair=%.1f¢ NO_fair=%.1f¢ NO_ask=%s¢ edge=%+.1f%%",
            ticker, yes_ask, yes_fair_cents, no_fair_cents, no_ask, relative_no_edge * 100,
        )

        confidence = 0.85 if is_futures else 0.90
        opp_type_full = "vig_stack_futures" if is_futures else "vig_stack_series"

        self._telem[sub]["surfaced"] += 1
        decisions.log_decision(
            ticker=ticker, opp_type=opp_type_full,
            edge=round(relative_no_edge, 4),
            gates={g: True for g in _VIG_STACK_GATES},
            decision="accept", reason="all_gates_passed",
            extra={"no_ask": no_ask, "no_fair_cents": round(no_fair_cents, 2)},
        )

        return {
            "type": opp_type_full,
            "ticker": ticker,
            "title": title,
            "market": market.raw if market.raw else {},
            "series_ticker": series_ticker,
            "edge": round(no_edge, 4),
            "relative_edge": round(relative_no_edge, 4),
            "confidence": confidence,
            "recommended_side": "no",
            "yes_sum_cents": yes_sum,
            "vig_factor": round(vig_factor, 4),
            "no_fair_cents": round(no_fair_cents, 2),
            "no_ask_cents": no_ask,
            "edge_result": {
                "fair_value": round(no_fair_prob, 4),
                "kalshi_price": round(no_ask_prob, 4),
                "edge": round(no_edge, 4),
                "relative_edge": round(relative_no_edge, 4),
                "confidence": confidence,
                "self_check_passed": True,
                "math_chain": math_chain,
                "warnings": [],
            },
        }

    def finalize(self, scan_id: str) -> None:
        """Telemetry log + stratified CF emission. Mirrors
        bot/scanner.py:998-1030."""
        for sub, t in self._telem.items():
            if t["checked"] == 0 and t["surfaced"] == 0:
                continue
            drops = {k: v for k, v in t.items() if k not in ("checked", "surfaced") and v > 0}
            logger.info("VIG_STACK_TELEMETRY/%s: checked=%d surfaced=%d drops=%s",
                        sub, t["checked"], t["surfaced"], drops or "none")

        if not self._rejected_opps:
            return
        try:
            from bot import clv
            sid = scan_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            for opp in _stratified_cf_rejects(self._rejected_opps):
                try:
                    clv.record_counterfactual_skip(opp, opp["skip_reason"], sid)
                except Exception:
                    logger.exception("CF emit failed for %s", opp.get("ticker"))
        except Exception:
            logger.exception("Stratified CF selection failed")
