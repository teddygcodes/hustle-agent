"""Reference candidate — simple total-points UNDER stub on NBA games.

This is intentionally a STUB, not a real edge model. It exists to:

1. Show the contract: filter universe row → return ``CandidateOpportunity``.
2. Be the smoke-test target for the driver: ``python3 -m
   tools.strategy_lab.driver --candidate example_total_points_under --days 14``.
3. Acceptable to produce 0 matches — the lab handles 0-candidate runs
   gracefully, and the captured universe may not include matching
   ``KXNBAGAME-*-TOTAL`` markets in a given 14-day window.

A real candidate would compute fair value from a model (pace stats,
momentum, market-implied vs. model-implied total) and only emit when
there's a real edge. This stub just picks markets in a price band with
some volume — for *plumbing* tests, not retuning conclusions.
"""
from __future__ import annotations

from typing import Optional

from tools.strategy_lab.candidate import CandidateOpportunity


class ExampleTotalPointsUnder:
    """Stub: bet UNDER on NBA total-points markets in [40-60c yes_ask, vol>=1000]."""

    name = "example_total_points_under"
    clv_match_window_hours: float = 2.0

    def evaluate(
        self, market: dict, context: Optional[dict] = None
    ) -> Optional[CandidateOpportunity]:
        ticker = market.get("ticker", "") or ""
        # Crude market_type filter — a real candidate would use a more
        # robust event-family classifier (see bot/scanner.py patterns).
        if not ticker.startswith("KXNBAGAME"):
            return None
        if "TOTAL" not in ticker.upper():
            return None

        ya = market.get("yes_ask")
        if ya is None:
            return None
        # universe.jsonl stores yes_ask as integer cents (per bot.universe writer).
        yes_ask_cents = float(ya)
        if not (40.0 <= yes_ask_cents <= 60.0):
            return None
        if (market.get("volume_24h") or 0) < 1000:
            return None

        # Bet UNDER = NO on the over market. NO entry price = (100 - yes_ask).
        target_price_cents = 100.0 - yes_ask_cents
        # Stub fair value: pretend the model says fair NO is 55c (no real signal).
        fair_value_cents = 55.0
        edge_cents = fair_value_cents - target_price_cents

        return CandidateOpportunity(
            ticker=ticker,
            side="no",
            target_price_cents=target_price_cents,
            fair_value_cents=fair_value_cents,
            edge_cents=edge_cents,
            confidence=0.5,
            reason="example stub: yes_ask in [40, 60]c AND volume_24h >= 1000",
            extra={"yes_ask_cents": yes_ask_cents},
        )


STRATEGY = ExampleTotalPointsUnder()
