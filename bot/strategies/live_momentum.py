"""Behavior-preserving port of bot/live_watcher.py's momentum-mode tick
handling into a TickStrategy implementation (Session 19a).

Same gates, same exit priority, same decision.log_decision reason
strings, same kelly+multiplier sizing math as the live production path
at bot/live_watcher.py:846-1700, 2177-2451. The only thing that changes
is the SHAPE: from a method on LiveGameWatcher that mutates self.X and
calls executor functions, to a pure(-ish) function that takes
(state, tick) and returns (new_state, action).

NOT YET WIRED INTO PRODUCTION. This class exists alongside
_tick_momentum so 19b can build the offline tick replay back-tester
against the contract. Live wiring is a separate decision after 19c
results land.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional

from bot.config import (
    LIVE_TAKE_PROFIT_CENTS, LIVE_NEAR_SETTLE_CENTS, LIVE_STOP_LOSS_CENTS,
    MOMENTUM_LEADER_MIN, MOMENTUM_DIP_BUY, MOMENTUM_DIP_MAX,
    MOMENTUM_PRICE_WINDOW, MOMENTUM_MAX_ENTRIES, MOMENTUM_REENTRY_COOLDOWN,
    MOMENTUM_SCALE_SMALL_DIP, MOMENTUM_SCALE_MED_DIP, MOMENTUM_SCALE_LARGE_DIP,
    MOMENTUM_DQS_THRESHOLD, MOMENTUM_DQS_TRAIL_STOP,
    MOMENTUM_MAX_LOSS_DOLLARS,
    TENNIS_QUALITY_MIN_TICKS, TENNIS_QUALITY_MIN_RANGE,
    SPORT_PROFILES,
)
from bot.game_context import GameContext
from bot import decisions
from bot.strategies import Market, State, Tick, TickAction, Buy, Sell, Hold


class LiveMomentumStrategy:
    """TickStrategy implementing the swing-trading dip-buy strategy
    that bot/live_watcher.py runs in mode='momentum'. Behavior-preserving
    port — see module docstring for discipline."""

    name = "live_momentum"

    def __init__(
        self,
        # Parameter overrides for back-testing (Session 19c). Defaults =
        # production config. NEVER read these from globals at decision
        # time — always use self._<name> so sweeps work without
        # monkey-patching config.
        leader_min: float = MOMENTUM_LEADER_MIN,
        dip_buy: float = MOMENTUM_DIP_BUY,
        dip_max: float = MOMENTUM_DIP_MAX,
        take_profit_cents: int = LIVE_TAKE_PROFIT_CENTS,
        near_settle_cents: int = LIVE_NEAR_SETTLE_CENTS,
        stop_loss_cents: int = LIVE_STOP_LOSS_CENTS,
        trail_stop_cents: int = MOMENTUM_DQS_TRAIL_STOP,
        max_loss_dollars: float = MOMENTUM_MAX_LOSS_DOLLARS,
        max_entries: int = MOMENTUM_MAX_ENTRIES,
        reentry_cooldown: int = MOMENTUM_REENTRY_COOLDOWN,
        dqs_threshold: float = MOMENTUM_DQS_THRESHOLD,
    ) -> None:
        self._leader_min = leader_min
        self._dip_buy = dip_buy
        self._dip_max = dip_max
        self._take_profit_cents = take_profit_cents
        self._near_settle_cents = near_settle_cents
        self._stop_loss_cents = stop_loss_cents
        self._trail_stop_cents = trail_stop_cents
        self._max_loss_dollars = max_loss_dollars
        self._max_entries = max_entries
        self._reentry_cooldown = reentry_cooldown
        self._dqs_threshold = dqs_threshold

    def init_state(
        self,
        market: Market,
        *,
        sport: str = "",
        opponent_ticker: Optional[str] = None,
        balance: float = 500.0,
        mode: str = "momentum",
        match_title: str = "",
    ) -> State:
        """Return fresh state for a new market subscription.

        Caller must supply `sport`, `opponent_ticker`, `balance`, and
        `match_title` — these come from the live_watcher's discovery
        path (or the back-tester's per-game setup). Mirrors
        LiveGameWatcher.__init__ initialization at
        bot/live_watcher.py:306-381.
        """
        state = State()
        state.data.update({
            "ticker": market.ticker,
            "opponent_ticker": opponent_ticker,
            "sport": sport,
            "mode": mode,
            "match_title": match_title,
            "started_at": time.time(),
            "bets_placed": [],
            "exits": [],
            "entry_count": 0,
            "peak_values": {},
            "trailing_active": {},  # never set to True in current logic; preserved for parity
            "price_history": deque(maxlen=MOMENTUM_PRICE_WINDOW),
            "opp_price_history": deque(maxlen=MOMENTUM_PRICE_WINDOW),
            "cooldown_remaining": 0,
            "last_exit_side": None,
            "game_ctx": GameContext(sport=sport),
            "espn_tick_counter": 0,
            "last_espn_data": {},
            "last_decision": None,  # (decision, reason) tuple for dampener
            "tick_telem": {
                "ticks": 0,              # total tick executions
                "no_leader": 0,          # neither side at/above MOMENTUM_LEADER_MIN
                "dip_too_big": 0,        # dip outside (min, max) window
                "dqs_fail": 0,           # DQS < MOMENTUM_DQS_THRESHOLD
                "conviction_checked": 0, # conviction gate evaluated
                "conviction_eligible": 0,# conviction_ok == True
                "conviction_near_miss": 0,# wp_edge >=5% but gate failed
                "instinct_avoid": 0,     # SportInstincts.should_avoid_entry tick
                "execute_attempt": 0,    # called _auto_bet_momentum
                "execute_success": 0,    # execute_trade returned success
                "execute_failed": {},    # keyed by reason string from executor
            },
            "balance": balance,
        })
        return state

    def process_tick(self, state: State, tick: Tick) -> tuple[State, TickAction]:
        """Process one per-game observation. Returns (new_state, action).

        STUB — full implementation lands in Session 19a part 2b. Raises
        NotImplementedError so any accidental wiring fails loudly. The
        stub exists so LiveMomentumStrategy structurally satisfies the
        TickStrategy Protocol (runtime_checkable isinstance check)."""
        raise NotImplementedError(
            "LiveMomentumStrategy.process_tick lands in Session 19a part 2b. "
            "This skeleton commit only ships __init__, init_state, and helpers."
        )

    def _get_sport_profile(self, sport: str) -> dict:
        """Mirror LiveGameWatcher._get_sport_profile at
        bot/live_watcher.py:687-699. Looks up bot.config.SPORT_PROFILES
        keyed by sport, falls back to a hardcoded default dict (NOT a
        SPORT_PROFILES['default'] entry — production builds the default
        inline from MOMENTUM_DIP_BUY / MOMENTUM_DIP_MAX).

        Production uses self.sport; here we take it as an arg so the
        strategy stays stateless across games."""
        default = {
            "min_dip": int(MOMENTUM_DIP_BUY * 100),
            "max_dip": int(MOMENTUM_DIP_MAX * 100),
            "max_entry": 75,
            "min_score_diff": 0,
            "periods": 4,
            "late_game_period": 3,
        }
        if sport and sport in SPORT_PROFILES:
            return SPORT_PROFILES[sport]
        return default

    def _dip_size_multiplier(self, dip_cents: float, sport: str) -> float:
        """Mirror LiveGameWatcher._dip_size_multiplier at
        bot/live_watcher.py:667-685. Dip-scaled sizing: large dips =
        best signal = biggest bet.

        Thresholds are RELATIVE to the sport's min_dip (NOT hardcoded
        8/6 cents): min_dip+6 → LARGE (1.5x), min_dip+3 → MED (1.2x),
        else SMALL (1.0x). For NBA min_dip=6 that gives 12+ → LARGE,
        9+ → MED."""
        profile = self._get_sport_profile(sport)
        min_dip = profile.get("min_dip", 4)

        if dip_cents >= min_dip + 6:
            return MOMENTUM_SCALE_LARGE_DIP
        elif dip_cents >= min_dip + 3:
            return MOMENTUM_SCALE_MED_DIP
        else:
            return MOMENTUM_SCALE_SMALL_DIP

    def _variance_quality_ok(
        self, price_history: deque
    ) -> tuple[bool, str]:
        """Mirror LiveGameWatcher._variance_quality_ok at
        bot/live_watcher.py:701-715. Tier 2.4 lightweight dip-quality
        gate for sports that skip full DQS — rejects flat dips during
        set breaks/changeovers/timeouts.

        Returns (ok, reason). NOTE production returns (True,
        'not_enough_history') for thin history (benefit of the doubt
        early in the watch), NOT False. Flat reason is an f-string with
        the actual range and tick count."""
        if len(price_history) < TENNIS_QUALITY_MIN_TICKS:
            return True, "not_enough_history"
        last_n = list(price_history)[-TENNIS_QUALITY_MIN_TICKS:]
        price_range = max(last_n) - min(last_n)
        if price_range < TENNIS_QUALITY_MIN_RANGE:
            return False, f"flat_{price_range}c_range_over_{TENNIS_QUALITY_MIN_TICKS}ticks"
        return True, "ok"

    def _log_decision_dampened(
        self,
        state: State,
        *,
        decision: str,
        reason: str,
        gates: dict,
        edge: Optional[float] = None,
        extra: Optional[dict] = None,
        close_ts: Optional[str] = None,
    ) -> None:
        """Mirror LiveGameWatcher._log_decision_dampened at
        bot/live_watcher.py:386-423. Only emits log_decision on
        (decision, reason) state change.

        Stores the dampener key in state.data['last_decision'] so the
        strategy stays pure — no self.X mutation."""
        key = (decision, reason)
        if key == state.data.get("last_decision"):
            return
        state.data["last_decision"] = key
        merged = dict(extra) if extra else None
        if close_ts and (merged is None or "close_ts" not in merged):
            merged = merged or {}
            merged["close_ts"] = close_ts
        try:
            decisions.log_decision(
                ticker=state.data.get("ticker") or "",
                opp_type="live_momentum",
                edge=edge,
                gates=gates,
                decision=decision,
                reason=reason,
                extra=merged,
            )
        except Exception:
            pass
