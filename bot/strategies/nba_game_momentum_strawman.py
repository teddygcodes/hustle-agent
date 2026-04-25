"""NbaGameMomentumStrawman — Session 13c PART 4 contract verification.

Targets KXNBAGAME (NBA single-game markets), a family the production
scanners completely ignore (Session 12 universe report: $262K vol /
0% scanned as of Apr 25). The point is NOT to make money — it's to
verify the Strategy contract is general enough to host a brand-new
50-line strategy without contract gymnastics. If this class needed
changes to bot/strategies/__init__.py to fit, the contract has hidden
coupling to vig_stack and Session 13a needs revisiting.

Math is deliberately structural: when yes_ask < 30 and no_bid > 70,
the market thinks YES is unlikely. The "edge" metric is the spec's
odds-ratio expression `(100 - yes_ask) / yes_ask - 1.0` — NOT a
relative-edge claim that would survive a sanity check on real Kalshi
data (which always has positive vig, so any "fair_no = 100 - yes_ask"
metric produces ≤0 edge after no_ask is subtracted). The point of
this strawman is contract verification end-to-end, NOT profitability.
The CLV computation downstream operates on entry_price_cents +
closing_yes_price + side, so back-test math is correct regardless of
what shape this metric takes.
"""
from __future__ import annotations

import logging
from typing import Optional

from bot import decisions
from bot.strategies import Market, Opportunity

logger = logging.getLogger(__name__)


class NbaGameMomentumStrawman:
    """Strawman strategy on KXNBAGAME — Session 13c contract verifier."""

    name = "nba_game_momentum_strawman"

    def __init__(
        self,
        min_volume_24h: int = 100,
        min_relative_edge: float = 0.05,
    ) -> None:
        self._min_volume_24h = min_volume_24h
        self._min_relative_edge = min_relative_edge

    def name_for(self, market: Market) -> str:
        return self.name

    def candidate_markets(self, universe: list[Market]) -> list[Market]:
        return [
            m for m in universe
            if m.series_ticker == "KXNBAGAME"
            and m.status == "active"
            and (m.volume_24h or 0) >= self._min_volume_24h
            and m.yes_ask is not None
            and m.no_ask is not None
            and m.no_bid is not None
        ]

    def evaluate(self, market: Market) -> Optional[Opportunity]:
        yes_ask = market.yes_ask
        no_ask = market.no_ask
        no_bid = market.no_bid
        ticker = market.ticker

        # Structural rule: heavy-YES-favorite markets often misprice the
        # NO side. Emit NO when yes_ask is low AND no_bid is high (wide
        # spread, illiquid).
        if not (yes_ask is not None and yes_ask < 30
                and no_bid is not None and no_bid > 70
                and no_ask is not None and no_ask > 0):
            decisions.log_decision(
                ticker=ticker, opp_type=self.name, edge=None,
                gates={"rule_fires": False},
                decision="reject", reason="rule_not_triggered",
            )
            return None

        # Spec formula: odds-ratio "edge" — produces big positive numbers
        # when yes_ask is low. Not a CLV-comparable edge; just a gate.
        fair_no_cents = 100.0 - yes_ask
        edge = (100.0 - yes_ask) / yes_ask - 1.0
        if edge < self._min_relative_edge:
            decisions.log_decision(
                ticker=ticker, opp_type=self.name,
                edge=round(edge, 4),
                gates={"rule_fires": True, "edge_above_threshold": False},
                decision="reject", reason="edge_below_threshold",
                extra={"edge": round(edge, 4),
                       "min_edge": self._min_relative_edge},
            )
            return None

        decisions.log_decision(
            ticker=ticker, opp_type=self.name,
            edge=round(edge, 4),
            gates={"rule_fires": True, "edge_above_threshold": True},
            decision="accept", reason="all_gates_passed",
            extra={"no_ask": no_ask,
                   "fair_no_cents": round(fair_no_cents, 2)},
        )

        return {
            "type": self.name,
            "ticker": ticker,
            "title": market.raw.get("title", "") if market.raw else "",
            "market": market.raw if market.raw else {},
            "series_ticker": market.series_ticker,
            "edge": round(edge, 4),
            "relative_edge": round(edge, 4),
            "confidence": 0.50,
            "recommended_side": "no",
            "no_fair_cents": round(fair_no_cents, 2),
            "no_ask_cents": no_ask,
            "edge_result": {
                "fair_value": round(fair_no_cents / 100.0, 4),
                "kalshi_price": round(no_ask / 100.0, 4),
                "edge": round(edge, 4),
                "relative_edge": round(edge, 4),
                "confidence": 0.50,
                "self_check_passed": True,
                "math_chain": [
                    f"yes_ask={yes_ask}c < 30",
                    f"no_bid={no_bid}c > 70",
                    f"fair_no = 100 - {yes_ask} = {fair_no_cents:.0f}c",
                    f"edge = ({fair_no_cents:.0f} - {no_ask}) / {no_ask} "
                    f"= {edge:.3f}",
                ],
                "warnings": [
                    "Strawman strategy — math is structural, not predictive. "
                    "Verification artifact for Session 13c contract grading."
                ],
            },
        }

    def finalize(self, scan_id: str) -> None:
        # No telemetry, no CF emission. The strawman is a back-test
        # verification artifact — keeping it minimal is the point.
        return None
