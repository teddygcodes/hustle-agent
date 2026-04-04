"""
Economic Markets Scanner — CPI / Inflation

Compares latest FRED CPI data to Kalshi KXCPIYOY market prices.

Data source: FRED API (Federal Reserve Economic Data).
Free key registration at https://fred.stlouisfed.org/docs/api/api_key.html
Set key in config/fred.json: {"api_key": "YOUR_KEY_HERE"}

If no key is configured, the scanner skips gracefully every cycle.
"""
from __future__ import annotations

import json
import math
import re
import ssl
import time as _time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.kalshi_client import get_markets
from bot.config import MIN_RELATIVE_EDGE, CONFIG_DIR
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

# FRED API — free key required (register at fred.stlouisfed.org)
# Series CPIAUCSL = CPI for All Urban Consumers (YoY % change computed below)
FRED_BASE = "https://api.stlouisfed.org/fred"
FRED_SERIES_CPI = "CPIAUCSL"

_CPI_CACHE: tuple[float, Optional[float]] | None = None
_CPI_CACHE_TTL = 3600  # 1 hour


def _load_fred_key() -> Optional[str]:
    """Load FRED API key from config/fred.json if present."""
    try:
        cfg_path = Path(CONFIG_DIR) / "fred.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            key = cfg.get("api_key", "").strip()
            return key if key else None
    except Exception:
        pass
    return None


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
    Fetch latest 12-month CPI inflation rate from FRED (CPIAUCSL).

    Computes YoY % change from the two most recent monthly observations.
    Returns the annualized rate as a float (e.g. 3.2 = 3.2% YoY).
    Returns None if FRED key is missing or API is unavailable.
    """
    global _CPI_CACHE
    if _CPI_CACHE and (_time.monotonic() - _CPI_CACHE[0]) < _CPI_CACHE_TTL:
        return _CPI_CACHE[1]

    api_key = _load_fred_key()
    if not api_key:
        print("  [Econ] No FRED API key — register free at fred.stlouisfed.org/docs/api/api_key.html")
        print("  [Econ] Save key to config/fred.json: {\"api_key\": \"YOUR_KEY\"}")
        _CPI_CACHE = (_time.monotonic(), None)
        return None

    # Fetch last 13 months of CPIAUCSL to compute latest 12-month change
    url = (
        f"{FRED_BASE}/series/observations"
        f"?series_id={FRED_SERIES_CPI}&api_key={api_key}"
        f"&file_type=json&sort_order=desc&limit=13"
    )
    data = _get_json(url)
    if not data or "observations" not in data:
        _CPI_CACHE = (_time.monotonic(), None)
        return None

    obs = [o for o in data["observations"] if o.get("value", ".") != "."]
    if len(obs) < 13:
        _CPI_CACHE = (_time.monotonic(), None)
        return None

    try:
        latest = float(obs[0]["value"])
        year_ago = float(obs[12]["value"])
        yoy_pct = ((latest - year_ago) / year_ago) * 100.0
        print(f"  [Econ] FRED CPI: latest={latest:.3f} year_ago={year_ago:.3f} YoY={yoy_pct:.2f}%")
        _CPI_CACHE = (_time.monotonic(), yoy_pct)
        return yoy_pct
    except (ValueError, ZeroDivisionError):
        _CPI_CACHE = (_time.monotonic(), None)
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
