"""
Price Monitor — Kalshi price movement detection.

Caches YES ask prices between scans. Detects when market moves against
our recommended position, reducing confidence and adding warnings.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("nexus.price_monitor")

DEFAULT_CACHE_PATH = os.path.join(os.path.dirname(__file__), "state", "price_cache.json")

_WARN_THRESHOLD = 3   # cents: add warning
_PENALTY_THRESHOLD = 5  # cents: add warning + reduce confidence


class PriceMonitor:
    def __init__(self, cache_path: str = DEFAULT_CACHE_PATH):
        self.cache_path = cache_path
        self._cache: dict = self._load()

    def _load(self) -> dict:
        try:
            with open(self.cache_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        try:
            with open(self.cache_path, "w") as f:
                json.dump(self._cache, f)
        except OSError as e:
            logger.error("Failed to save price cache: %s", e)

    def update(self, ticker: str, yes_ask: int):
        """Store current yes_ask price for ticker, persist to disk."""
        self._cache[ticker] = {
            "yes_ask": yes_ask,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_cached(self, ticker: str) -> dict | None:
        return self._cache.get(ticker)

    def annotate(self, opp: dict, current_yes_ask: int) -> dict:
        """
        Return a copy of opp annotated with price-movement warnings.
        Modifies 'warnings' list and 'confidence' based on movement direction.
        """
        opp = dict(opp)
        opp["warnings"] = list(opp.get("warnings", []))

        ticker = opp.get("ticker", "")
        cached = self.get_cached(ticker)
        if cached is None:
            # No prior price — update cache and return unchanged
            self.update(ticker, current_yes_ask)
            return opp

        prior = cached["yes_ask"]
        side = opp.get("recommended_side", "yes")

        # Calculate movement against our position
        if side == "yes":
            # BUY YES: rising price = market correcting against us
            movement_against = current_yes_ask - prior
        else:
            # BUY NO: falling price = market correcting against us (YES rising = NO falls)
            movement_against = prior - current_yes_ask

        if movement_against > _PENALTY_THRESHOLD:
            opp["warnings"].append(
                f"Price moving against {side.upper()} position: {prior}¢ → {current_yes_ask}¢ ({movement_against:+d}¢)"
            )
            opp["confidence"] = round(opp.get("confidence", 0.70) - 0.10, 4)
        elif movement_against > _WARN_THRESHOLD:
            opp["warnings"].append(
                f"Price moving against {side.upper()} position: {prior}¢ → {current_yes_ask}¢ ({movement_against:+d}¢)"
            )

        # Update cache with current price
        self.update(ticker, current_yes_ask)
        return opp

    def annotate_all(self, opportunities: list, kalshi_client=None) -> list:
        """Apply annotate() to all opportunities. Returns annotated list."""
        result = []
        for opp in opportunities:
            yes_ask = opp.get("kalshi_price_cents") or int((opp.get("kalshi_price", 0)) * 100)
            if yes_ask > 0:
                annotated = self.annotate(opp, current_yes_ask=yes_ask)
            else:
                annotated = opp
            result.append(annotated)
        return result
