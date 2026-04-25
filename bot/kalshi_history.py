"""Kalshi history layer — fetches settled-market closes for back-testing
opportunities on tickers the bot never actually traded.

Public API:
    fetch_settled_close(ticker) -> float | None
        Returns 100.0 if YES won, 0.0 if NO won, None if not yet settled
        or fetch failed. Settled values are permanently cached on disk
        under bot/state/cache/kalshi_settled_closes.json (settled markets
        never change).

Used by tools/backtest.py when --include-history is set, as a fallback
on clv.json miss for back-tested opportunities targeting tickers outside
our historical trade set.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Optional

from agent.kalshi_client import get_market as _kalshi_get_market

from bot.config import BOT_STATE_DIR

logger = logging.getLogger(__name__)

_CACHE_FILE: Path = BOT_STATE_DIR / "cache" / "kalshi_settled_closes.json"
_cache: Optional[dict[str, float]] = None
_cache_lock = Lock()


def _load_cache() -> dict[str, float]:
    """Read cache from disk; empty dict on any failure."""
    global _cache
    if _cache is not None:
        return _cache
    if not _CACHE_FILE.exists():
        _cache = {}
        return _cache
    try:
        with open(_CACHE_FILE) as f:
            data = json.load(f)
        _cache = data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        _cache = {}
    return _cache


def _save_cache(cache: dict[str, float]) -> None:
    """Atomic write — write to .tmp then rename."""
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
        tmp.replace(_CACHE_FILE)
    except OSError as e:
        logger.warning("kalshi_history cache write failed: %s", e)


def fetch_settled_close(ticker: str) -> float | None:
    """Return YES-side settlement value (100.0 / 0.0) or None.

    Cache-first: settled markets never change, so once we know the
    settlement value it's permanent. None responses (still-open markets)
    are NOT cached — those must re-fetch.
    """
    if not ticker:
        return None

    with _cache_lock:
        cache = _load_cache()
        if ticker in cache:
            return cache[ticker]

    try:
        market = _kalshi_get_market(ticker)
    except Exception as e:
        logger.warning("kalshi_history fetch failed for %s: %s", ticker, e)
        return None

    if not isinstance(market, dict) or market.get("error"):
        return None

    # Kalshi reports settled markets with status="finalized" (and sometimes
    # "settled"); the authoritative signal is the `result` field, which is
    # "yes" or "no" once a market has resolved. Empty/unknown result means
    # not yet settled regardless of status string.
    result = market.get("result")
    if result == "yes":
        close = 100.0
    elif result == "no":
        close = 0.0
    else:
        # "" / void / cancelled / unknown — decline to assert a close
        return None

    with _cache_lock:
        cache = _load_cache()
        cache[ticker] = close
        _save_cache(cache)
    return close
