"""
Crypto Price Monitor — CoinGecko Integration

Fetches and caches cryptocurrency prices from the CoinGecko free API.
Used by the Glint trading bot for crypto market summaries in Telegram alerts.
"""

from __future__ import annotations

import sys
import time
import logging
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import CRYPTO_ASSETS, CRYPTO_CACHE_TTL, COINGECKO_BASE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------
_cache: dict = {"data": None, "fetched_at": 0}
_CACHE_TTL: int = CRYPTO_CACHE_TTL  # seconds

# ---------------------------------------------------------------------------
# Short-name mapping
# ---------------------------------------------------------------------------
_TICKER_MAP: dict[str, str] = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
}


def fetch_crypto_prices(assets: Optional[list] = None) -> dict:
    """Fetch current USD prices and 24h change from CoinGecko.

    Args:
        assets: CoinGecko asset ids to query. Defaults to CRYPTO_ASSETS
                from config (bitcoin, ethereum, solana).

    Returns:
        Dict keyed by asset id, e.g.::

            {
                "bitcoin":  {"usd": 68500, "usd_24h_change": 2.3},
                "ethereum": {"usd": 3450,  "usd_24h_change": -0.5},
                "solana":   {"usd": 185,   "usd_24h_change": 4.1},
            }

        On failure returns cached data if available, otherwise
        ``{"error": "<message>"}``.
    """
    if assets is None:
        assets = CRYPTO_ASSETS

    # Return cached data if still fresh
    if _cache["data"] is not None and (time.time() - _cache["fetched_at"]) < _CACHE_TTL:
        logger.debug("Returning cached crypto prices (age %.0fs)", time.time() - _cache["fetched_at"])
        return _cache["data"]

    url = f"{COINGECKO_BASE}/simple/price"
    params = {
        "ids": ",".join(assets),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    headers = {
        "User-Agent": "GlintTradingBot/1.0",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data: dict = resp.json()

        # Update cache
        _cache["data"] = data
        _cache["fetched_at"] = time.time()
        logger.info("Fetched crypto prices for %s", ", ".join(assets))
        return data

    except Exception as e:
        logger.warning("CoinGecko request failed: %s", e)
        if _cache["data"] is not None:
            logger.info("Returning stale cached crypto prices")
            return _cache["data"]
        return {"error": str(e)}


def format_crypto_summary(prices: dict) -> str:
    """Format crypto prices into a compact Telegram-friendly string.

    Args:
        prices: Dict returned by :func:`fetch_crypto_prices`.

    Returns:
        Formatted string, e.g.::

            "BTC $68,500 (+2.3%) | ETH $3,450 (-0.5%) | SOL $185 (+4.1%)"

        Returns ``"Crypto data unavailable"`` if prices contains an error.
    """
    if "error" in prices:
        return "Crypto data unavailable"

    parts: list[str] = []
    for asset_id, ticker in _TICKER_MAP.items():
        info = prices.get(asset_id)
        if info is None:
            continue

        price = info.get("usd", 0)
        change = info.get("usd_24h_change", 0)

        sign = "+" if change >= 0 else ""
        parts.append(f"{ticker} ${price:,.0f} ({sign}{change:.1f}%)")

    return " | ".join(parts) if parts else "Crypto data unavailable"
