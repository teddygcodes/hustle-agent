"""Strategy contract — pure-function strategies that take Market data in
and return Opportunity dicts out. No live API calls inside strategies.

This package was created in Session 13a. The contract enables Session 13b
(offline back-tester) to feed `universe.jsonl` rows into `evaluate()` and
get opportunities without touching Kalshi.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple, Optional, Protocol, Union, runtime_checkable


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


# ---------------------------------------------------------------------------
# TickStrategy contract (Session 19a) — distinct from snapshot Strategy above.
#
# Live games produce a stream of price+context observations that a strategy
# must process statefully (price_history, peak_value, cooldown_remaining),
# but the state is passed explicitly through process_tick rather than
# mutated on self. The new types below are additive — no existing
# definition above this line is modified.
# ---------------------------------------------------------------------------


@dataclass
class Tick:
    """One per-game observation. Caller produces these from a Kalshi market
    snapshot + matched opponent market + ESPN game data.

    Mirrors the row shape of bot/state/live_ticks.jsonl plus the raw market
    dicts needed for downstream sizing.
    """
    ts: str                              # ISO 8601 timestamp
    ticker: str
    yes_bid: Optional[int]
    yes_ask: Optional[int]
    no_bid: Optional[int]
    no_ask: Optional[int]
    opp_ticker: Optional[str] = None
    opp_yes_bid: Optional[int] = None
    opp_yes_ask: Optional[int] = None
    opp_no_bid: Optional[int] = None
    opp_no_ask: Optional[int] = None
    wp: Optional[float] = None           # ESPN-derived win probability if available
    score_diff: Optional[int] = None     # our-team minus their-team
    period: Optional[int] = None
    espn_data: Optional[dict] = None     # full ESPN snapshot for SportInstincts.detect / GameContext.update
    close_ts: Optional[str] = None       # market close (settlement) timestamp; threaded into log_decision
    raw: dict = field(default_factory=dict)            # full primary market dict (for sizing)
    raw_opp: dict = field(default_factory=dict)        # full opponent market dict


@dataclass
class State:
    """Strategy-internal state carried across ticks. Mutable but passed
    explicitly through process_tick (no global mutation, no self.X writes
    on the strategy instance).

    Schema is intentionally a dict for now; iterate during 19b/19c as
    back-test needs sharpen the contract. Keys used by LiveMomentumStrategy:
      bets_placed, exits, entry_count, peak_values, trailing_active,
      price_history, opp_price_history, cooldown_remaining, game_ctx,
      espn_tick_counter, last_espn_data, last_decision, tick_telem,
      balance, started_at, mode, sport, ticker, opponent_ticker.
    """
    data: dict = field(default_factory=dict)


# NOTE: NamedTuple does not support mutable defaults safely. Using
# Optional[dict] = None and unwrapping at read sites (`action.extra or {}`)
# avoids the shared-default-instance gotcha entirely. Recommended in the
# Session 19a plan.


class Buy(NamedTuple):
    side: str                          # "yes" | "no"
    qty: int                           # contracts to buy at tick price (computed by strategy via kelly + multipliers)
    reason: str                        # human-readable, mirrors production "MOMENTUM: ..." reason
    ticker: str = ""                   # which side to buy (primary ticker or opponent ticker)
    price_cents: int = 0               # entry price (yes_ask of `ticker`'s market) — telemetry/audit
    extra: Optional[dict] = None       # opaque metadata: dip_cents, dqs_score, conviction flag, dqs_breakdown, fair_prob, sport, instincts, size_multiplier


class Sell(NamedTuple):
    side: str                          # "yes" | "no" — side of the held position being closed
    qty: int                           # contracts to sell (== current position size)
    reason: str                        # mirrors production exit reasons: "TAKE PROFIT: +12¢ (...)", "STOP-LOSS: ...", etc.
    ticker: str = ""                   # which position to close
    exit_price: int = 0                # current_value at decision (yes_bid for YES, 100-yes_ask for NO)
    extra: Optional[dict] = None       # opaque metadata: peak_price, trough_price, gain_cents, hold_seconds, exit_game_state


class Hold(NamedTuple):
    reason: str = ""                   # optional telemetry: "no_leader", "cooldown", "dqs_fail", "" (silent hold)


TickAction = Union[Buy, Sell, Hold]


@runtime_checkable
class TickStrategy(Protocol):
    """A pure(-ish) tick-stream strategy. Distinct from snapshot-based
    Strategy: live games produce a stream of price+context observations
    that the strategy must process statefully (price_history, peak_value,
    cooldown_remaining), but the state is passed explicitly through
    process_tick rather than mutated on self.

    Side effects allowed:
      - Telemetry calls to bot.decisions.log_decision (monkey-patchable in tests).
    Side effects NOT allowed:
      - Order placement (executor.execute_trade, exit_position): caller's job.
      - Disk writes (paper_trades.json, journal): caller's job.
      - self.X mutation: state must round-trip through (state, tick) → (new_state, action).

    Pure function semantics: same (state, tick) → same (new_state, action),
    modulo telemetry calls.
    """
    name: str

    def init_state(self, market: Market) -> State:
        """Return fresh state for a new market subscription. Called once
        per game/match before the first tick. `market` provides the
        primary ticker, sport (caller derives), and close_ts."""
        ...

    def process_tick(self, state: State, tick: Tick) -> tuple[State, TickAction]:
        """Pure function: same inputs → same outputs.

        Returns (new_state, action). Hold actions are no-ops; caller may
        discard them or use them for telemetry. Mutating `state.data` in
        place is acceptable (it's owned by caller); returning a new
        State() with copied dict is also acceptable. Implementation
        choice."""
        ...
