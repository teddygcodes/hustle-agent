"""
Nexus Trading Bot — Weather Market Scanner

Fetches NWS forecasts and compares to Kalshi weather market prices.
Completely independent of sports odds or parlay pipeline.
"""

from __future__ import annotations

import logging
import re
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("glint.weather")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.kalshi_client import get_markets
from bot.math_engine import calculate_weather_edge
from bot.config import (
    MIN_RELATIVE_EDGE,
    NWS_BIAS_CORRECTION,
    NWS_CITIES,
    WEATHER_MIN_HOURS_TO_CLOSE,
    WEATHER_SERIES_TICKERS,
)

# City alias map — Kalshi weather market titles use full city names / airport
# codes that don't match the short NWS_CITIES keys directly.
_CITY_ALIASES: dict[str, list[str]] = {
    "NYC":          ["new york", "nyc", "ny city", "central park", "manhattan"],
    "Chicago":      ["chicago", "chi", "midway", "o'hare"],
    "Miami":        ["miami"],
    "Austin":       ["austin"],
    "Denver":       ["denver"],
    "Boston":       ["boston", "fenway"],
    "DC":           ["washington", "d.c.", " dc ", "reagan", "dulles"],
    "SF":           ["san francisco", " sf ", "sfo", "bay area"],
    "LA":           ["los angeles", " la ", "lax", "anaheim"],
    "Seattle":      ["seattle"],
    "Phoenix":      ["phoenix"],
    "Dallas":       ["dallas"],
    "Atlanta":      ["atlanta"],
    "Philadelphia": ["philadelphia", "philly"],
    "Las Vegas":    ["las vegas", "vegas"],
    "Portland":     ["portland"],
    "Minneapolis":  ["minneapolis", "twin cities"],
    "Nashville":    ["nashville"],
}


def _get_forecast_temp_for_date(forecast: dict, target_date) -> Optional[float]:
    """
    Return the NWS daytime high for a specific calendar date.

    Iterates the forecast periods (stored with 'start' in local time with
    UTC offset) and returns the temperature for the first non-night period
    whose start date matches target_date.  Returns None if no match found.
    """
    for period in forecast.get("periods", []):
        if "night" in period.get("name", "").lower():
            continue
        try:
            start_dt = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
            if start_dt.date() == target_date:
                return float(period["temperature"])
        except Exception:
            continue
    return None


def _fetch_nws_forecast(city: str, lat: float, lon: float) -> Optional[dict]:
    """Fetch NWS forecast for a city."""
    try:
        points_url = f"https://api.weather.gov/points/{lat},{lon}"
        resp = requests.get(points_url, headers={"User-Agent": "NexusBot/1.0"}, timeout=10)
        resp.raise_for_status()
        forecast_url = resp.json()["properties"]["forecast"]

        f_resp = requests.get(forecast_url, headers={"User-Agent": "NexusBot/1.0"}, timeout=10)
        f_resp.raise_for_status()
        periods = f_resp.json()["properties"]["periods"]

        return {
            "city": city,
            "periods": [
                {
                    "name": p["name"],
                    "temperature": p["temperature"],
                    "unit": p["temperatureUnit"],
                    "start": p["startTime"],
                }
                for p in periods[:4]
            ],
        }
    except Exception as e:
        logger.warning("%s forecast error: %s", city, e)
        return None


def scan_weather_markets() -> list[dict]:
    """
    Scan Kalshi weather markets and compare to NWS forecasts.
    Returns list of weather opportunities with edge calculations.
    """
    opportunities = []

    # Fetch Kalshi weather markets by known series tickers (keyword search returns nothing)
    weather_markets_by_ticker: dict[str, dict] = {}
    series_found = []
    for series in WEATHER_SERIES_TICKERS:
        result = get_markets(series_ticker=series, status="open", limit=50)
        if "error" not in result:
            batch = result.get("markets", [])
            if batch:
                series_found.append(f"{series}({len(batch)})")
            for m in batch:
                weather_markets_by_ticker.setdefault(m["ticker"], m)

    weather_markets = list(weather_markets_by_ticker.values())
    logger.info("Series fetched: %s — %d total markets",
                ', '.join(series_found) if series_found else 'none', len(weather_markets))
    if not weather_markets:
        logger.info("No markets found — Kalshi may have no open weather contracts today")
        return opportunities

    # Fetch NWS forecasts for all cities
    forecasts = {}
    for city, (lat, lon) in NWS_CITIES.items():
        fc = _fetch_nws_forecast(city, lat, lon)
        if fc:
            forecasts[city] = fc
            periods_summary = [(p["name"], p["temperature"]) for p in fc["periods"]]
            logger.debug("%s: %s", city, periods_summary)
        else:
            logger.warning("%s: forecast fetch FAILED", city)

    logger.info("%d Kalshi markets, %d NWS cities loaded", len(weather_markets), len(forecasts))

    # Match markets to forecasts and calculate edges
    for market in weather_markets:
        title_raw = market.get("title", "")
        title = title_raw.lower()
        ticker = market.get("ticker", "")
        yes_ask = market.get("yes_ask")
        no_ask = market.get("no_ask")

        # Verbose per-market log — fires before any filter so every market is visible
        logger.debug("%r | yes_ask=%s no_ask=%s | title=%r", ticker, yes_ask, no_ask, title_raw)

        if not yes_ask or yes_ask <= 0:
            logger.debug("SKIP %s: no yes_ask price (yes_ask=%s)", ticker, yes_ask)
            continue

        # Micro-price filter: skip near-certain outcomes (1-2¢ or 98-99¢).
        # At these extremes NWS model uncertainty exceeds the absolute edge — and
        # genuine 1¢ markets have too little liquidity for meaningful execution.
        if yes_ask <= 2 or yes_ask >= 98:
            logger.debug("SKIP %s: extreme price %s¢ — near-certain market", ticker, yes_ask)
            continue

        # Today's market filter: skip any market whose ticker date matches today (UTC).
        # Weather markets for today are priced by real-time temperature data, not NWS
        # forecasts — our model has zero edge and generates fake 1000%+ signals on 1¢ contracts.
        # Use ticker date rather than close_time because weather markets in US timezones
        # may not close until 5am UTC the next day (>8h from 8pm UTC), defeating a
        # simple hours_to_close filter.
        _today_ticker_str = datetime.now(timezone.utc).strftime("%y%b%d").upper()
        ticker_parts_for_date = ticker.split("-")
        if len(ticker_parts_for_date) >= 2:
            _date_seg = ticker_parts_for_date[1][:7]  # e.g. "26APR04"
            if _date_seg == _today_ticker_str:
                logger.debug("SKIP %s: today's market (date=%s) — priced by observed temp", ticker, _date_seg)
                continue

        # Next-day filter: also skip markets closing within WEATHER_MIN_HOURS_TO_CLOSE
        # (catches edge cases where ticker date doesn't parse cleanly)
        close_str = market.get("close_time") or market.get("expiration_time", "")
        if close_str:
            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                hours_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < WEATHER_MIN_HOURS_TO_CLOSE:
                    logger.debug("SKIP %s: closes in %.1fh (< %dh threshold)", ticker, hours_left, WEATHER_MIN_HOURS_TO_CLOSE)
                    continue
            except Exception:
                pass  # If we can't parse the time, allow it through

        # Match to a city using full-name aliases (Kalshi uses "New York", not "NYC")
        matched_city = None
        for city, aliases in _CITY_ALIASES.items():
            if city not in forecasts:
                continue  # Skip cities with no NWS data
            for alias in aliases:
                if alias in title:
                    matched_city = city
                    break
            if matched_city:
                break

        if not matched_city:
            logger.debug("SKIP %s: no city match in title", ticker)
            continue
        if matched_city not in forecasts:
            logger.debug("SKIP %s: matched city %r but NWS forecast unavailable", ticker, matched_city)
            continue

        # Extract threshold and direction from title.
        # Kalshi uses: ">75°", "<68°", or "68-69°" (bucket/range) — no F suffix.
        temp_above_m = re.search(r">\s*(\d+(?:\.\d+)?)°", title)
        temp_below_m = re.search(r"<\s*(\d+(?:\.\d+)?)°", title)
        temp_range_m = re.search(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)°", title)

        threshold: float
        threshold_high: float | None = None
        direction: str

        if temp_above_m:
            threshold = float(temp_above_m.group(1))
            direction = "above"
        elif temp_below_m:
            threshold = float(temp_below_m.group(1))
            direction = "below"
        elif temp_range_m:
            threshold = float(temp_range_m.group(1))
            threshold_high = float(temp_range_m.group(2))
            direction = "range"
        else:
            # Final fallback: old-style "or above/below" keyword matching
            old_temp = re.search(r"(\d+)\s*°?\s*f?", title)
            if old_temp:
                threshold = float(old_temp.group(1))
                if any(w in title for w in ["or above", "above", "over", "warmer", "at least"]):
                    direction = "above"
                elif any(w in title for w in ["or below", "below", "under", "cooler"]):
                    direction = "below"
                else:
                    logger.debug("SKIP %s: no direction keyword in %r", ticker, title_raw)
                    continue
            else:
                logger.debug("SKIP %s: no temperature threshold found in %r", ticker, title_raw)
                continue

        # Get forecast temp for the market's resolution date.
        # Parse the date from the TICKER (e.g. KXHIGHNY-26APR05-T67 → April 5, 2026)
        # rather than from close_time in UTC — Kalshi weather markets close at ~11pm
        # local time, so close_time in UTC crosses midnight and returns the WRONG date.
        forecast_temp = None
        target_date = None
        _MONTH_MAP = {
            "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
            "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12,
        }
        try:
            # Ticker format: KXHIGHNY-26APR05-T67
            ticker_parts = ticker.split("-")
            if len(ticker_parts) >= 2:
                date_seg = ticker_parts[1]  # e.g. '26APR05'
                m = re.match(r"(\d{2})([A-Z]{3})(\d{2})$", date_seg)
                if m:
                    yy, mon, dd = int(m.group(1)), m.group(2), int(m.group(3))
                    target_date = datetime(
                        2000 + yy, _MONTH_MAP.get(mon, 1), dd
                    ).date()
                    forecast_temp = _get_forecast_temp_for_date(forecasts[matched_city], target_date)
        except Exception:
            pass

        # Compute days_ahead from target_date (cap at 3)
        if target_date is not None:
            today = datetime.now(timezone.utc).date()
            days_ahead = max(1, min(3, (target_date - today).days + 1))
        else:
            days_ahead = 1

        # Fallback: first daytime period if ticker-date lookup failed
        if forecast_temp is None:
            for period in forecasts[matched_city]["periods"]:
                if "night" not in period.get("name", "").lower():
                    forecast_temp = period["temperature"]
                    break

        if forecast_temp is None:
            logger.debug("SKIP %s: no daytime period in NWS forecast for %s", ticker, matched_city)
            continue

        # Calculate edge
        edge_result = calculate_weather_edge(
            city=matched_city,
            forecast_temp=forecast_temp,
            threshold=threshold,
            direction=direction,
            kalshi_price_cents=yes_ask,
            threshold_high=threshold_high,
            days_ahead=days_ahead,
        )

        # Cap: edges > 25% absolute are likely stale pricing or near-expiry artefacts
        if abs(edge_result.get("edge", 0)) > 0.25:
            logger.warning(
                "SKIP %s: edge %.1f%% exceeds 25%% cap — likely stale price or near-expiry",
                ticker, edge_result["edge"] * 100,
            )
            continue

        fair_value = edge_result.get("fair_value", 0)
        rel_edge = edge_result.get("relative_edge", 0)
        corrected = edge_result.get("corrected_temp", forecast_temp)
        threshold_met = edge_result.get("relative_edge", 0) >= MIN_RELATIVE_EDGE
        logger.info(
            "%s | %s | NWS=%s°F corrected=%s°F | threshold=%s°F %s | "
            "fair=%.4f kalshi=%s¢ | edge=%+.4f rel=%+.1%% | self_check=%s | threshold_met=%s",
            ticker, matched_city, forecast_temp, corrected, threshold, direction,
            fair_value, yes_ask, edge_result.get('edge', 0), rel_edge * 100,
            'PASS' if edge_result.get('self_check_passed') else 'FAIL', threshold_met,
        )

        if edge_result["self_check_passed"] and edge_result["relative_edge"] >= MIN_RELATIVE_EDGE:
            opportunities.append({
                "type": "weather",
                "ticker": ticker,
                "title": market.get("title", ""),
                "market": market,
                "edge_result": edge_result,
                "city": matched_city,
                "forecast_temp": forecast_temp,
                "threshold": threshold,
                "direction": direction,
                "edge": edge_result["edge"],
                "relative_edge": edge_result["relative_edge"],
                "confidence": edge_result["confidence"],
                "kalshi_price": edge_result.get("kalshi_price", 0),
                "recommended_side": "yes" if edge_result["edge"] > 0 else "no",
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            })

    return opportunities
