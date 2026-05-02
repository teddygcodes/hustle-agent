"""Candidate-strategy contract for Strategy Lab v1.

Two public types:

- ``CandidateOpportunity`` — a single "would have bet" decision a candidate
  emits when ``evaluate(market)`` thinks the market has edge.
- ``CandidateStrategy`` — the ``Protocol`` a candidate file's ``STRATEGY``
  attribute must satisfy.

Mirrors ``bot.strategies.Strategy`` loosely (snapshot-style, pure-function),
but takes raw ``dict`` rows from ``universe.jsonl`` instead of the
``Market`` dataclass — friendlier for users writing 20-line candidates
without learning ``bot.strategies.Market``.

Canonical schema (read ``CLAUDE.md`` "Canonical Data Schema Reference"
first): ``side`` is the canonical "yes" / "no" enum — NEVER the
suffix-_won variants. The lab's tests assert that no source file under
``tools/strategy_lab/`` carries the anti-pattern literals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass
class CandidateOpportunity:
    """A single 'would have bet' decision from a candidate strategy."""

    ticker: str
    side: str  # canonical schema: "yes" | "no"
    target_price_cents: float
    fair_value_cents: float
    edge_cents: float
    confidence: float  # 0.0-1.0
    reason: str
    extra: Optional[dict] = None


@runtime_checkable
class CandidateStrategy(Protocol):
    """A user-written strategy hypothesis.

    The lab calls ``evaluate()`` on every market in the universe stream
    over the test window. Return a ``CandidateOpportunity`` to emit a
    would-have-bet, or ``None`` to skip.

    Optional instance attribute ``clv_match_window_hours: float`` (default
    2.0) widens or narrows the temporal join window the evaluator uses to
    match would-have-bets to settled clv records.
    """

    name: str

    def evaluate(
        self,
        market: dict,
        context: Optional[dict] = None,
    ) -> Optional[CandidateOpportunity]:
        """Return a ``CandidateOpportunity`` or ``None``.

        ``market`` is one row from ``universe.jsonl``. ``context`` is a
        dict the driver populates with optional helpers (e.g.,
        ``existing_decisions_by_ticker``).
        """
        ...
