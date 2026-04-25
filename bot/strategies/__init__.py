"""Strategy contract — pure-function strategies that take Market data in
and return Opportunity dicts out. No live API calls inside strategies.

This package was created in Session 13a. The contract enables Session 13b
(offline back-tester) to feed `universe.jsonl` rows into `evaluate()` and
get opportunities without touching Kalshi.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Market:
    """One row from universe.jsonl / one Kalshi market snapshot.

    Mirrors the universe writer's row shape (bot/universe.py:153). The
    `raw` field carries the original Kalshi market dict (when available)
    so opp output can include the full market object — old behavior.
    """
    ticker: str
    series_ticker: str
    event_ticker: Optional[str]
    status: str
    close_ts: Optional[str]
    yes_ask: Optional[int]
    yes_bid: Optional[int]
    no_ask: Optional[int]
    no_bid: Optional[int]
    volume_24h: Optional[int]
    open_interest: Optional[int]
    ts: Optional[str] = None
    scan_id: Optional[str] = None
    raw: dict = field(default_factory=dict)


Opportunity = dict[str, Any]


@runtime_checkable
class Strategy(Protocol):
    name: str

    def name_for(self, market: Market) -> str:
        """Attribution name for `on_market_seen`. Default: self.name.
        Override when one strategy class spans multiple historical
        scanner names (see VigStackSeries: 'vig_stack_series' vs
        'vig_stack_futures')."""
        ...

    def candidate_markets(self, universe: list[Market]) -> list[Market]:
        """Filter universe down to markets this strategy might evaluate.
        Cheap structural filter only (series prefix, ladder grouping).
        Strategies may stash per-ladder context on self for evaluate to
        read — call this exactly once per scan."""
        ...

    def evaluate(self, market: Market) -> Optional[Opportunity]:
        """Apply edge math + gating to one candidate. Returns an
        Opportunity dict or None. Idempotent given the same internal
        state set by candidate_markets."""
        ...

    def finalize(self, scan_id: str) -> None:
        """Called once per scan after the evaluate loop. Strategy emits
        any deferred side-effects (stratified CF emission, telemetry).
        Default: no-op."""
        ...


# Strategies registered for scan_cycle iteration. Add new classes here
# only after their behavior-preservation test passes. The list is
# materialised on first access to avoid an import cycle (the concrete
# strategy modules import Market and Strategy from this package).
def _build_registered_strategies() -> list[Strategy]:
    from bot.strategies.vig_stack_series import VigStackSeries
    return [VigStackSeries()]


REGISTERED_STRATEGIES: list[Strategy] = _build_registered_strategies()
