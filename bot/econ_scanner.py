"""
Economic Markets Scanner — CPI / Inflation

Compares Cleveland Fed CPI Nowcast probability to Kalshi KXCPIYOY market prices.
No auth required — Cleveland Fed nowcast is a free public API.
"""
from __future__ import annotations

import json
import math
import re
import ssl
import time as _time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from agent.kalshi_client import get_markets
from bot.config import MIN_RELATIVE_EDGE
from bot.math_engine import _self_check_edge

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# Kalshi series tickers for economic markets
ECON_SERIES_TICKERS = [
    "KXCPIYOY",   # CPI year-over-year
    "KXACPI",     # Annual CPI
    "KXCPI",      # Monthly CPI
]

# Cleveland Fed Inflation Nowcasting — free, no auth
CLEVELAND_FED_NOWCAST_URL = (
    "https://www.clevelandfed.org/api/inflation-nowcasting/nowcast"
)

_NOWCAST_CACHE: tuple[float, Optional[float]] | None = None
_NOWCAST_CACHE_TTL = 3600  # 1 hour — nowcast updates infrequently


def _get_json(url: str, timeout: int = 12, max_retries: int = 3) -> dict | list | None:
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt < max_retries - 1:
                _time.sleep(2 ** attempt)
            else:
                print(f"  [EconHTTP] Error fetching {url[:80]}: {e}")
    return None


def _get_cpi_nowcast() -> Optional[float]:
    """
    Fetch Cleveland Fed CPI nowcast point estimate (percent YoY).

    Returns e.g. 3.2 meaning the nowcast predicts 3.2% CPI year-over-year.
    Returns None if the API is unavailable.
    """
    global _NOWCAST_CACHE
    if _NOWCAST_CACHE and (_time.monotonic() - _NOWCAST_CACHE[0]) < _NOWCAST_CACHE_TTL:
        return _NOWCAST_CACHE[1]

    data = _get_json(CLEVELAND_FED_NOWCAST_URL)
    if not data:
        _NOWCAST_CACHE = (_time.monotonic(), None)
        return None

    try:
        if isinstance(data, dict):
            val = data.get("nowcast") or data.get("value") or data.get("estimate")
            if val is not None:
                result = float(val)
                _NOWCAST_CACHE = (_time.monotonic(), result)
                return result
        if isinstance(data, list) and data:
            val = data[0].get("nowcast") or data[0].get("value")
            if val is not None:
                result = float(val)
                _NOWCAST_CACHE = (_time.monotonic(), result)
                return result
    except (KeyError, TypeError, ValueError):
        pass

    _NOWCAST_CACHE = (_time.monotonic(), None)
    return None


def _fetch_kalshi_econ_markets() -> list[dict]:
    """Fetch open markets across all economic series tickers."""
    markets = []
    for series in ECON_SERIES_TICKERS:
        result = get_markets(series_ticker=series, status="open", limit=50)
        if "error" not in result:
            markets.extend(result.get("markets", []))
    return markets


def _prob_from_nowcast(nowcast_val: float, threshold: float, direction: str) -> float:
    """
    Convert a Cleveland Fed nowcast point estimate to P(CPI [direction] threshold).

    Uses ±0.3% standard deviation — typical 1-month CPI forecast uncertainty.
    """
    std = 0.30
    z = (threshold - nowcast_val) / std
    cdf_above = 0.5 * math.erfc(z / math.sqrt(2))
    return cdf_above if direction == "above" else 1.0 - cdf_above


def scan_econ_markets() -> list[dict]:
    """
    Scan Kalshi CPI/inflation markets for edges vs Cleveland Fed nowcast.

    Returns opportunity dicts with type='econ_cpi_edge'.
    """
    markets = _fetch_kalshi_econ_markets()
    if not markets:
        print("  [Econ] No open economic markets found")
        return []

    nowcast = _get_cpi_nowcast()
    if nowcast is None:
        print("  [Econ] Cleveland Fed nowcast unavailable — skipping")
        return []

    print(f"  [Econ] {len(markets)} markets | Cleveland Fed CPI nowcast={nowcast:.2f}%")

    now_utc = datetime.now(timezone.utc)
    opportunities = []

    for market in markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        volume = market.get("volume") or 0
        open_interest = market.get("open_interest") or 0
        if volume < 20 and open_interest < 10:
            continue

        above_m = re.search(r"(?:above|>)\s*(\d+(?:\.\d+)?)\s*%", title, re.IGNORECASE)
        below_m = re.search(r"(?:below|<)\s*(\d+(?:\.\d+)?)\s*%", title, re.IGNORECASE)
        if above_m:
            threshold = float(above_m.group(1))
            direction = "above"
        elif below_m:
            threshold = float(below_m.group(1))
            direction = "below"
        else:
            continue

        fair_value = _prob_from_nowcast(nowcast, threshold, direction)
        kalshi_price = yes_ask / 100.0
        edge = fair_value - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        check_ok, check_msg = _self_check_edge(fair_value, kalshi_price, edge)
        if not check_ok:
            continue

        if kalshi_price <= 0.03 or fair_value <= 0.03:
            continue

        if abs(relative_edge) < MIN_RELATIVE_EDGE:
            continue

        hours_left = 0.0
        try:
            close_str = market.get("close_time") or market.get("expiration_time", "")
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            hours_left = (close_dt - now_utc).total_seconds() / 3600
        except Exception:
            pass

        opportunities.append({
            "type": "econ_cpi_edge",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": 0.60,  # Conservative — nowcast has real uncertainty
            "recommended_side": "yes" if edge > 0 else "no",
            "nowcast_val": nowcast,
            "threshold": threshold,
            "direction": direction,
            "fair_value": round(fair_value, 4),
            "kalshi_price": round(kalshi_price, 4),
            "hours_to_close": round(hours_left, 1),
            "edge_result": {
                "fair_value": round(fair_value, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": 0.60,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities
