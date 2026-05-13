"""Small Polymarket Gamma client for live market discovery.

This module intentionally covers only the read-path needed by the
cross-platform discovery tool. It does not decide whether Gamma quote fields
are sufficient for trading or observation; that scanner price-source decision
is deferred to S125.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
REQUEST_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3


class PolymarketGammaError(RuntimeError):
    """Raised when the first Gamma page cannot be fetched after retries."""


def _parse_gamma_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _get_page(limit: int, offset: int) -> list[dict]:
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "offset": offset,
    }
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.get(
                POLYMARKET_GAMMA_MARKETS_URL,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            payload = resp.json() or []
            if not isinstance(payload, list):
                raise PolymarketGammaError(f"unexpected Gamma payload type: {type(payload).__name__}")
            return payload
        except (requests.RequestException, ValueError, PolymarketGammaError) as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
    raise PolymarketGammaError(f"Gamma markets request failed after {MAX_ATTEMPTS} attempts: {last_exc}")


def get_active_markets(limit_per_page: int = 100, max_pages: int | None = None) -> list[dict]:
    """Fetch active, open Gamma markets.

    Uses ``/markets?active=true&closed=false`` with offset pagination. A first-page
    failure is systemic and raises ``PolymarketGammaError``. A later page failure
    is logged and returns the markets fetched so far so a partial discovery run can
    still inspect already-visible inventory.
    """
    if limit_per_page <= 0:
        raise ValueError("limit_per_page must be positive")

    out: list[dict] = []
    page = 0
    while max_pages is None or page < max_pages:
        offset = page * limit_per_page
        try:
            batch = _get_page(limit_per_page, offset)
        except PolymarketGammaError:
            if page == 0:
                raise
            logger.warning(
                "Polymarket Gamma page fetch failed at page=%d offset=%d; returning %d fetched markets",
                page,
                offset,
                len(out),
                exc_info=True,
            )
            return out
        if not batch:
            break
        out.extend(batch)
        page += 1
    return out


def normalize_for_matcher(gamma_market: dict) -> dict:
    """Normalize a Gamma market into the repo's matcher/candidate shape.

    Mappings are from a live Gamma ``active=true&closed=false`` sample captured
    during S124v2 planning:
    - identity: ``id`` and ``slug`` become ``ticker``/``id``/``slug`` plus the
      canonical Polymarket URL.
    - text: ``question`` becomes ``question``/``question_text``/``title``;
      ``description`` is copied to ``description`` and ``rules_primary``.
    - timing: ``endDate`` is parsed into a tz-aware UTC ``datetime``. ``endDateIso``
      is a fallback when ``endDate`` is absent.
    - source: ``resolutionSource`` is copied to ``resolution_text`` and
      ``resolution_source``.
    - live quote fields: ``bestBid``, ``bestAsk``, ``lastTradePrice``, and
      ``volume24hr`` are preserved for downstream scanner investigation.
    """
    market_id = str(gamma_market.get("id") or gamma_market.get("conditionId") or gamma_market.get("slug") or "")
    slug = str(gamma_market.get("slug") or "")
    question = str(gamma_market.get("question") or gamma_market.get("groupItemTitle") or "").strip()
    description = str(gamma_market.get("description") or "").strip()
    resolution_source = str(gamma_market.get("resolutionSource") or "").strip()
    close_date = _parse_gamma_datetime(gamma_market.get("endDate") or gamma_market.get("endDateIso"))
    category = str(gamma_market.get("category") or "").strip()

    source_url = f"https://polymarket.com/market/{slug}" if slug else ""
    return {
        "venue": "polymarket",
        "ticker": market_id,
        "id": market_id,
        "slug": slug,
        "url": source_url,
        "question": question,
        "question_text": question,
        "title": question,
        "close_date": close_date,
        "resolution_text": resolution_source,
        "resolution_source": resolution_source,
        "rules_primary": description,
        "description": description,
        "result": "",
        "category": category,
        "source_url": source_url,
        "best_bid": gamma_market.get("bestBid"),
        "best_ask": gamma_market.get("bestAsk"),
        "last_trade_price": gamma_market.get("lastTradePrice"),
        "volume_24h": gamma_market.get("volume24hr"),
        "events": gamma_market.get("events") or [],
    }
