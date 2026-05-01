"""Behavior-preserving port of bot/live_watcher.py's momentum-mode tick
handling into a TickStrategy implementation (Session 19a).

Same gates, same exit priority, same decision.log_decision reason
strings, same kelly+multiplier sizing math as the live production path
in bot/live_watcher.py: LiveGameWatcher._tick_momentum (entry path) and
LiveGameWatcher._check_exit (momentum exit branches) plus
LiveGameWatcher._auto_bet_momentum (sizing). Line numbers are omitted
intentionally — function names are stable, line numbers drift on every
edit.

The only thing that changes is the SHAPE: from a method on
LiveGameWatcher that mutates self.X and calls executor functions, to a
pure(-ish) function that takes (state, tick) and returns
(new_state, action).

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
    MOMENTUM_MAX_LOSS_DOLLARS, MOMENTUM_DISABLED_SPORTS,
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
        # Session 42: per-sport TP/SL overrides for the back-tester's
        # per-sport sweep mode. Keyed by sport name (e.g. "ufc", "nba").
        # Each value is a partial dict — only "take_profit" and
        # "stop_loss" keys are honored at the gate site; other keys are
        # silently ignored (Plan-agent revision #1: scope-limited to
        # TP+SL this session). When None, resolution falls through to
        # SPORT_PROFILES then to the strategy default — i.e. pre-Session-42
        # behavior is preserved byte-identical.
        sport_overrides: Optional[dict[str, dict]] = None,
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
        self._sport_overrides = sport_overrides

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
        LiveGameWatcher.__init__ initialization. Line numbers are
        omitted intentionally — function names are stable, line numbers
        drift on every edit.
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
        """Mirror bot/live_watcher.py: LiveGameWatcher._tick_momentum
        (entry path) plus LiveGameWatcher._check_exit (momentum-mode
        branches only). Line numbers are omitted intentionally —
        function names are stable, line numbers drift on every edit.

        Returns (new_state, action). Mutates state.data in place.
        Pure-function semantics: same (state, tick) → same outputs,
        modulo log_decision telemetry.

        One-action-per-tick. Production cooldown blocks same-tick
        re-entry after exit, so we return AT MOST one action per call.
        Caller is responsible for translating Buy/Sell into orders and
        for journal/disk writes."""
        d = state.data
        d["tick_telem"]["ticks"] += 1

        yes_ask = tick.yes_ask or 0
        if not yes_ask:
            return state, Hold(reason="no_yes_ask")

        opp_yes_ask = tick.opp_yes_ask or 0
        opp_yes_bid = tick.opp_yes_bid
        # close_ts threaded through every dampener call (Session 15.5
        # parity: bot.regime.tag uses event_horizon_hr).
        _close_ts = tick.close_ts

        # --- Settlement check (mirrors _tick_momentum prep block) ---
        # Strategy signals "settled" via Hold; caller decides how to
        # wind down any open positions (production calls _journal_append
        # and sets active=False).
        primary_settled = yes_ask >= 97 or yes_ask <= 3
        opp_settled = opp_yes_ask >= 97 or opp_yes_ask <= 3
        if primary_settled or opp_settled:
            return state, Hold(reason="settled")

        # --- Price history ---
        d["price_history"].append(yes_ask)
        if opp_yes_ask > 0:
            d["opp_price_history"].append(opp_yes_ask)

        # --- Cooldown decrement ---
        if d["cooldown_remaining"] > 0:
            d["cooldown_remaining"] -= 1

        # --- GameContext update from tick.espn_data ---
        # Caller pre-derives "_we_are_home" and stashes it in
        # tick.espn_data — the strategy stays out of team-name
        # normalization (production lines mid-_tick_momentum). Our/their
        # score derivation still happens here so the opponent
        # GameContext build (entry path opponent DQS branch) has access.
        our_score = 0
        their_score = 0
        period = tick.period
        espn_data = tick.espn_data
        if espn_data is not None:
            d["last_espn_data"] = espn_data
            we_are_home = espn_data.get("_we_are_home", True)
            h_score = espn_data.get("home_score", 0)
            a_score = espn_data.get("away_score", 0)
            our_score = h_score if we_are_home else a_score
            their_score = a_score if we_are_home else h_score
            if d["game_ctx"] and period:
                d["game_ctx"].update(
                    espn_data=espn_data,
                    our_score=our_score,
                    their_score=their_score,
                    period=period,
                    clock_str=espn_data.get("clock", ""),
                )
        d["espn_tick_counter"] += 1

        # --- Primary side stats ---
        player_prob = yes_ask / 100.0
        is_leader = player_prob >= self._leader_min
        recent_high = max(d["price_history"]) if d["price_history"] else yes_ask
        dip_cents = recent_high - yes_ask

        # --- Opponent side stats ---
        opp_is_leader = False
        opp_recent_high = 0
        opp_dip_cents = 0
        if opp_yes_ask > 0:
            opp_prob = opp_yes_ask / 100.0
            opp_is_leader = opp_prob >= self._leader_min
            opp_recent_high = (
                max(d["opp_price_history"]) if d["opp_price_history"] else opp_yes_ask
            )
            opp_dip_cents = opp_recent_high - opp_yes_ask

        # Telemetry: neither side leads
        if not is_leader and not opp_is_leader:
            d["tick_telem"]["no_leader"] += 1

        sport_profile = self._get_sport_profile(d["sport"])

        # === EXIT LOGIC (port of _check_exit momentum-mode branches) ===
        # Production runs exit logic before entry logic. If we exit, we
        # set cooldown_remaining = MOMENTUM_REENTRY_COOLDOWN (production
        # _tick_momentum lines after _check_exit) which blocks entry on
        # the SAME tick — so we return Sell immediately and skip entry.
        if d["bets_placed"]:
            bet = d["bets_placed"][0]  # one position max in momentum mode
            held_side = bet.get("side")
            ticker = bet.get("ticker")
            entry_price = bet.get("price_cents", 0)

            if not ticker or not entry_price:
                # Production `continue`s past such bets; we just hold.
                return state, Hold(reason="invalid_bet")

            # Resolve which market this bet maps to (primary vs opponent)
            if ticker == d["opponent_ticker"]:
                bet_yes_bid = opp_yes_bid
                bet_yes_ask = opp_yes_ask or 0
            else:
                bet_yes_bid = tick.yes_bid
                bet_yes_ask = yes_ask

            # Current value (mirrors _check_exit) — for YES use yes_bid
            # if not None (yes_bid=0 is valid), else fall back to
            # yes_ask. For NO use 100 - yes_ask.
            if held_side == "yes":
                current_value = bet_yes_bid if bet_yes_bid is not None else (bet_yes_ask or 0)
            else:
                current_value = 100 - (bet_yes_ask or 100)

            gain_cents = current_value - entry_price

            # Peak tracking (high-water mark).
            # Fix Apr 26 (Session 19a-peakfix): mirrors bot/live_watcher.py:2225 fix
            # so the back-tester's port↔production parity stays at 8/8. The port
            # faithfully preserved the peak-tracking bug per Session 19a's
            # behavior-preservation discipline; now that production is fixed, the
            # port matches.
            prev_peak = d["peak_values"].setdefault(ticker, entry_price)
            if current_value > prev_peak:
                d["peak_values"][ticker] = current_value
                prev_peak = current_value

            # Session 42: per-sport TP/SL override layer. Resolves
            # override → sport_profile → strategy default. Override is
            # partial-only (only take_profit / stop_loss honored;
            # trail_stop and other keys remain profile-then-default).
            override = self._sport_overrides.get(d["sport"], {}) if self._sport_overrides else {}
            sport_tp = override.get("take_profit", sport_profile.get("take_profit", self._take_profit_cents))
            sport_sl = override.get("stop_loss", sport_profile.get("stop_loss", self._stop_loss_cents))
            sport_trail = sport_profile.get("trail_stop", self._trail_stop_cents)

            should_exit = False
            reason = ""

            # 1. TAKE PROFIT
            if gain_cents >= sport_tp:
                should_exit = True
                reason = (
                    f"TAKE PROFIT: +{gain_cents}¢ "
                    f"({entry_price}¢ → {current_value}¢)"
                )

            # 2. NEAR-SETTLEMENT (yes-only)
            if (not should_exit and held_side == "yes"
                    and current_value >= self._near_settle_cents):
                should_exit = True
                reason = (
                    f"NEAR-SETTLE: {current_value}¢ (match nearly won, "
                    f"entry {entry_price}¢, gain +{gain_cents}¢)"
                )

            # 2b. TRAILING STOP (with SportInstincts widen-stops)
            if not should_exit and d["mode"] == "momentum":
                from bot.game_context import SportInstincts
                drop_from_peak = prev_peak - current_value
                effective_trail = sport_trail
                exit_instincts = SportInstincts.detect(
                    d["game_ctx"], d["last_espn_data"], d["sport"]
                )
                if exit_instincts.should_widen_stops:
                    # Clutch/empty-net: widen trail by 50%
                    effective_trail = int(sport_trail * 1.5)
                if drop_from_peak >= effective_trail and gain_cents > 0:
                    should_exit = True
                    reason = (
                        f"TRAILING STOP: peaked at {prev_peak}¢, "
                        f"dropped {drop_from_peak}¢ "
                        f"(entry {entry_price}¢ → {current_value}¢, "
                        f"locking +{gain_cents}¢)"
                    )

            # 2c. SCORE FLIP / OPP RUN EXIT (mirrors production exactly:
            # for opponent ticker, effective_score_diff = -score_diff)
            if not should_exit and d["mode"] == "momentum" and d["game_ctx"]:
                gc = d["game_ctx"]
                effective_score_diff = tick.score_diff
                if ticker == d["opponent_ticker"]:
                    effective_score_diff = (
                        -tick.score_diff if tick.score_diff is not None else None
                    )
                flip_confirmed = gc.momentum < 0 and gc.lead_trend < 0
                if (effective_score_diff is not None
                        and effective_score_diff < 0
                        and flip_confirmed):
                    should_exit = True
                    reason = (
                        f"SCORE FLIP: trailing by {abs(effective_score_diff)}, "
                        f"mom={gc.momentum:+.2f} lead_trend={gc.lead_trend:+.2f} "
                        f"({entry_price}¢ → {current_value}¢, {gain_cents:+d}¢)"
                    )
                elif gc.opponent_on_run and gain_cents < 0:
                    should_exit = True
                    reason = (
                        f"OPP RUN EXIT: opponent on scoring run, position underwater "
                        f"({entry_price}¢ → {current_value}¢, {gain_cents:+d}¢)"
                    )

            # 4. HARD STOP-LOSS
            if not should_exit:
                drop_cents = entry_price - current_value
                if drop_cents >= sport_sl:
                    should_exit = True
                    reason = (
                        f"STOP-LOSS: dropped {drop_cents}¢ from entry "
                        f"({entry_price}¢ → {current_value}¢)"
                    )

            # 4b. DOLLAR STOP
            if not should_exit:
                contracts = bet.get("contracts", 1)
                unrealized_loss = (entry_price - current_value) / 100.0 * contracts
                if unrealized_loss >= self._max_loss_dollars:
                    should_exit = True
                    reason = (
                        f"DOLLAR STOP: ${unrealized_loss:.2f} loss exceeds "
                        f"${self._max_loss_dollars:.2f} cap "
                        f"({entry_price}¢ → {current_value}¢ x{contracts})"
                    )

            if should_exit:
                # Strategy mutates state and emits Sell. Caller is
                # responsible for journal/paper_trades writes; we don't
                # do the I/O. Production also clears _peak_values and
                # _trailing_active for the ticker.
                trough = (
                    min(d["price_history"]) if d["price_history"] else current_value
                )
                contracts = bet.get("contracts", 1)
                d["bets_placed"] = []
                d["peak_values"].pop(ticker, None)
                d["trailing_active"].pop(ticker, None)
                d["cooldown_remaining"] = self._reentry_cooldown
                d["last_exit_side"] = held_side
                d["exits"].append({
                    **bet,
                    "reason": reason,
                    "exit_price": current_value,
                    "peak_price": prev_peak,
                    "trough_price": trough,
                })
                return state, Sell(
                    side=held_side, qty=contracts, reason=reason,
                    ticker=ticker, exit_price=current_value,
                    extra={
                        "peak_price": prev_peak,
                        "trough_price": trough,
                        "gain_cents": gain_cents,
                        "entry_price": entry_price,
                        "entry_record": bet,
                    },
                )
            # else: still holding, fall through. Entry block won't fire
            # because bets_placed is non-empty. Result will be Hold.

        # === ENTRY LOGIC (port of _tick_momentum entry block) ===
        max_entry = sport_profile.get("max_entry", 75)
        max_dip = sport_profile.get("max_dip", int(MOMENTUM_DIP_MAX * 100))
        min_dip = sport_profile.get("min_dip", int(MOMENTUM_DIP_BUY * 100))

        # Sport-disable gate: block new entries in disabled sports.
        # Exits are unaffected (handled above without consulting this).
        sport_lc = (d["sport"] or "").lower()
        can_enter = (
            d["entry_count"] < self._max_entries
            and d["cooldown_remaining"] <= 0
            and not d["bets_placed"]  # one position at a time
            and sport_lc not in MOMENTUM_DISABLED_SPORTS
        )

        # wp_edge proxy + context for decision log (Session 7 parity).
        gc_wp = d["game_ctx"].win_probability if d["game_ctx"] else None
        wp_edge = round(gc_wp - yes_ask / 100.0, 3) if gc_wp is not None else None
        mom_ctx = {
            "wp": round(gc_wp, 3) if gc_wp is not None else None,
            "kalshi_price": yes_ask,
            "dip_cents": dip_cents,
            "dqs": None,  # filled at sites where DQS was actually computed
        }

        if not can_enter:
            if sport_lc in MOMENTUM_DISABLED_SPORTS:
                _reason = "sport_disabled"
                _gates = {"can_enter": False, "sport_enabled": False}
            elif d["entry_count"] >= self._max_entries:
                _reason = "max_entries"
                _gates = {"can_enter": False, "sport_enabled": True, "max_entries": False}
            elif d["cooldown_remaining"] > 0:
                _reason = "cooldown"
                _gates = {"can_enter": False, "sport_enabled": True, "cooldown": False}
            elif d["bets_placed"]:
                _reason = "position_open"
                _gates = {"can_enter": False, "sport_enabled": True, "position_open": False}
            else:
                _reason = "cannot_enter"
                _gates = {"can_enter": False}
            self._log_decision_dampened(
                state,
                decision="reject", reason=_reason, gates=_gates,
                edge=wp_edge,
                extra={**mom_ctx, "sport": sport_lc,
                       "entry_count": d["entry_count"],
                       "cooldown_remaining": d["cooldown_remaining"],
                       "open_bets": len(d["bets_placed"])},
                close_ts=_close_ts,
            )

        buy_ticker: Optional[str] = None
        buy_market: Optional[dict] = None
        buy_price = 0
        buy_reason = ""
        buy_dip = 0
        buy_dqs = 0.0
        dqs_breakdown: dict = {}

        skip_dqs = sport_profile.get("skip_dqs", False)

        if can_enter:
            from bot.game_context import compute_dip_quality, SportInstincts

            # --- Primary side dip detection ---
            if (is_leader and yes_ask <= max_entry
                    and len(d["price_history"]) >= 3
                    and dip_cents >= min_dip):
                if dip_cents <= max_dip:
                    if skip_dqs:
                        # Tier 2.4: variance quality gate (tennis path).
                        q_ok, q_reason = True, "ok"
                        if sport_profile.get("variance_quality_gate"):
                            q_ok, q_reason = self._variance_quality_ok(d["price_history"])
                        if not q_ok:
                            d["tick_telem"]["dqs_fail"] += 1
                            self._log_decision_dampened(
                                state,
                                decision="reject", reason="variance_quality",
                                gates={"can_enter": True, "is_leader": True,
                                       "dip_window": True, "variance_quality": False},
                                edge=wp_edge,
                                extra={**mom_ctx, "q_reason": q_reason},
                                close_ts=_close_ts,
                            )
                        else:
                            # Tennis/UFC scalp mode: pure price action
                            buy_ticker = d["ticker"]
                            buy_market = tick.raw
                            buy_price = yes_ask
                            buy_dip = dip_cents
                            buy_dqs = 1.0
                            dqs_breakdown = {
                                "mode": "price_action",
                                "skip_dqs": True,
                                "q_reason": q_reason,
                            }
                            buy_reason = (
                                f"leader at {yes_ask}c, dipped {dip_cents}c "
                                f"from recent {recent_high}c "
                                f"(SCALP MODE — pure price action)"
                            )
                    else:
                        # Compute DQS with full game intelligence
                        dqs, dqs_bd = compute_dip_quality(
                            game_ctx=d["game_ctx"],
                            dip_cents=dip_cents,
                            price=yes_ask,
                            price_history=d["price_history"],
                            sport=d["sport"] or "",
                            espn_data=espn_data or None,
                        )
                        if dqs >= self._dqs_threshold:
                            buy_ticker = d["ticker"]
                            buy_market = tick.raw
                            buy_price = yes_ask
                            buy_dip = dip_cents
                            buy_dqs = dqs
                            dqs_breakdown = dqs_bd
                            buy_reason = (
                                f"leader at {yes_ask}c, dipped {dip_cents}c "
                                f"from recent {recent_high}c "
                                f"(DQS={dqs:.2f} wp={dqs_bd.get('wp_edge','?')} "
                                f"mom={dqs_bd.get('momentum_raw','?')})"
                            )
                        else:
                            d["tick_telem"]["dqs_fail"] += 1
                            self._log_decision_dampened(
                                state,
                                decision="reject", reason="dqs_fail",
                                gates={"can_enter": True, "is_leader": True,
                                       "dip_window": True, "dqs": False},
                                edge=wp_edge,
                                extra={**mom_ctx, "dqs": round(dqs, 3),
                                       "threshold": self._dqs_threshold},
                                close_ts=_close_ts,
                            )
                else:
                    d["tick_telem"]["dip_too_big"] += 1
                    self._log_decision_dampened(
                        state,
                        decision="reject", reason="dip_too_big",
                        gates={"can_enter": True, "is_leader": True,
                               "dip_min": True, "dip_max": False},
                        edge=wp_edge,
                        extra={**mom_ctx, "max_dip": max_dip},
                        close_ts=_close_ts,
                    )

            # --- Opponent side dip detection (only if primary didn't qualify) ---
            if (not buy_ticker and tick.raw_opp and opp_is_leader
                    and opp_yes_ask <= max_entry
                    and len(d["opp_price_history"]) >= 3
                    and opp_dip_cents >= min_dip):
                if opp_dip_cents <= max_dip:
                    if skip_dqs:
                        q_ok, q_reason = True, "ok"
                        if sport_profile.get("variance_quality_gate"):
                            q_ok, q_reason = self._variance_quality_ok(
                                d["opp_price_history"]
                            )
                        if not q_ok:
                            d["tick_telem"]["dqs_fail"] += 1
                            # Production does not log_decision here for
                            # opponent variance reject (only logs for
                            # primary). Mirror that.
                        else:
                            buy_ticker = d["opponent_ticker"]
                            buy_market = tick.raw_opp
                            buy_price = opp_yes_ask
                            buy_dip = opp_dip_cents
                            buy_dqs = 1.0
                            dqs_breakdown = {
                                "mode": "price_action",
                                "skip_dqs": True,
                                "q_reason": q_reason,
                            }
                            buy_reason = (
                                f"OPP leader at {opp_yes_ask}c, dipped "
                                f"{opp_dip_cents}c from recent {opp_recent_high}c "
                                f"(SCALP MODE — pure price action)"
                            )
                    else:
                        # Build temporary opponent GameContext (inverted scores)
                        opp_game_ctx = None
                        if d["game_ctx"] and d["game_ctx"]._snapshots:
                            from bot.game_context import GameContext as _GC
                            opp_game_ctx = _GC(sport=d["sport"] or "")
                            if espn_data and period:
                                opp_game_ctx.update(
                                    espn_data=espn_data,
                                    our_score=their_score,
                                    their_score=our_score,
                                    period=period,
                                    clock_str=espn_data.get("clock", ""),
                                )
                        dqs, dqs_bd = compute_dip_quality(
                            game_ctx=opp_game_ctx,
                            dip_cents=opp_dip_cents,
                            price=opp_yes_ask,
                            price_history=d["opp_price_history"],
                            sport=d["sport"] or "",
                            espn_data=espn_data or None,
                        )
                        if dqs >= self._dqs_threshold:
                            buy_ticker = d["opponent_ticker"]
                            buy_market = tick.raw_opp
                            buy_price = opp_yes_ask
                            buy_dip = opp_dip_cents
                            buy_dqs = dqs
                            dqs_breakdown = dqs_bd
                            buy_reason = (
                                f"OPP leader at {opp_yes_ask}c, dipped "
                                f"{opp_dip_cents}c from recent {opp_recent_high}c "
                                f"(DQS={dqs:.2f} wp={dqs_bd.get('wp_edge','?')} "
                                f"mom={dqs_bd.get('momentum_raw','?')})"
                            )
                        else:
                            d["tick_telem"]["dqs_fail"] += 1
                            # Production does not log_decision here for
                            # opponent dqs_fail (parity).
                else:
                    d["tick_telem"]["dip_too_big"] += 1
                    # Production does not log_decision here for opponent
                    # dip_too_big (parity).

            # --- Conviction Entry: "read the game, buy without a dip" ---
            # Only fires for non-skip_dqs sports (court sports). Lazy
            # imports inside the gate to mirror production.
            if not buy_ticker and not skip_dqs:
                from bot.config import (
                    CONVICTION_ENABLED, CONVICTION_MIN_WP_EDGE,
                    CONVICTION_MIN_MOMENTUM, CONVICTION_MIN_LEAD_TREND,
                    CONVICTION_MIN_PRICE, CONVICTION_MAX_PRICE,
                    CONVICTION_MIN_TICKS, CONVICTION_MIN_COMPLETION,
                    CONVICTION_EXCLUDED_SPORTS,
                )
                sport_key = (d["sport"] or "").lower().split("_")[0]
                if (CONVICTION_ENABLED
                        and sport_key not in CONVICTION_EXCLUDED_SPORTS
                        and d["game_ctx"]
                        and len(d["game_ctx"]._snapshots) >= CONVICTION_MIN_TICKS):
                    d["tick_telem"]["conviction_checked"] += 1
                    gc = d["game_ctx"]
                    wp = gc.win_probability
                    kalshi_implied = yes_ask / 100.0
                    wp_edge = wp - kalshi_implied  # reassigned (matches production)
                    completion = gc.game_completion_pct

                    conv_instincts = SportInstincts.detect(
                        gc, espn_data, d["sport"] or ""
                    )
                    if conv_instincts.should_avoid_entry:
                        d["tick_telem"]["instinct_avoid"] += 1

                    conviction_ok = (
                        wp_edge >= CONVICTION_MIN_WP_EDGE
                        and gc.momentum >= CONVICTION_MIN_MOMENTUM
                        and gc.lead_trend >= CONVICTION_MIN_LEAD_TREND
                        and CONVICTION_MIN_PRICE <= yes_ask <= CONVICTION_MAX_PRICE
                        and completion >= CONVICTION_MIN_COMPLETION
                        and gc.score_diff > 0  # must actually be winning
                        and not conv_instincts.should_avoid_entry
                        and is_leader
                    )

                    if conviction_ok:
                        d["tick_telem"]["conviction_eligible"] += 1
                        buy_ticker = d["ticker"]
                        buy_market = tick.raw
                        buy_price = yes_ask
                        buy_dip = 0  # no dip — conviction entry
                        buy_dqs = 0.9
                        dqs_breakdown = {
                            "mode": "conviction",
                            "wp": round(wp, 3),
                            "wp_edge": round(wp_edge, 3),
                            "momentum": round(gc.momentum, 2),
                            "lead_trend": round(gc.lead_trend, 2),
                            "completion": round(completion, 2),
                            "score_diff": gc.score_diff,
                            "instincts": conv_instincts.flags,
                        }
                        buy_reason = (
                            f"CONVICTION: wp={wp:.0%} vs price={yes_ask}c "
                            f"(+{wp_edge:.0%} edge) mom={gc.momentum:+.1f} "
                            f"trend={gc.lead_trend:+.1f} {completion:.0%} done"
                        )
                    elif wp_edge >= 0.05 and gc.momentum > 0:
                        d["tick_telem"]["conviction_near_miss"] += 1

                    # Opponent-side conviction (only if primary did not fire)
                    if not buy_ticker and opp_yes_ask > 0 and opp_is_leader:
                        opp_wp = 1.0 - wp if wp > 0 else 0.5
                        opp_kalshi = opp_yes_ask / 100.0
                        opp_wp_edge = opp_wp - opp_kalshi
                        opp_momentum = -gc.momentum
                        opp_lead_trend = -gc.lead_trend
                        opp_score_diff = -gc.score_diff

                        opp_conviction_ok = (
                            opp_wp_edge >= CONVICTION_MIN_WP_EDGE
                            and opp_momentum >= CONVICTION_MIN_MOMENTUM
                            and opp_lead_trend >= CONVICTION_MIN_LEAD_TREND
                            and CONVICTION_MIN_PRICE <= opp_yes_ask <= CONVICTION_MAX_PRICE
                            and completion >= CONVICTION_MIN_COMPLETION
                            and opp_score_diff > 0
                            and not conv_instincts.should_avoid_entry
                            and opp_is_leader
                        )
                        if opp_conviction_ok:
                            buy_ticker = d["opponent_ticker"]
                            buy_market = tick.raw_opp
                            buy_price = opp_yes_ask
                            buy_dip = 0
                            buy_dqs = 0.9
                            dqs_breakdown = {
                                "mode": "conviction",
                                "wp": round(opp_wp, 3),
                                "wp_edge": round(opp_wp_edge, 3),
                                "momentum": round(opp_momentum, 2),
                                "side": "opponent",
                            }
                            buy_reason = (
                                f"CONVICTION OPP: wp={opp_wp:.0%} vs "
                                f"price={opp_yes_ask}c (+{opp_wp_edge:.0%} edge) "
                                f"mom={opp_momentum:+.1f}"
                            )

        # --- Accept-path log + sizing ---
        if buy_ticker and buy_market is not None:
            d["tick_telem"]["execute_attempt"] += 1
            reentry_tag = f" [RE-ENTRY #{d['entry_count'] + 1}]" if d["entry_count"] > 0 else ""
            conviction_mode = buy_dip == 0 and "conviction" in buy_reason.lower()

            self._log_decision_dampened(
                state,
                decision="accept",
                reason="conviction" if conviction_mode else "dip_buy",
                gates={"can_enter": True, "is_leader": True,
                       "dip_window": True, "dqs": True},
                edge=wp_edge,
                extra={**mom_ctx,
                       "dqs": round(buy_dqs, 3) if buy_dqs else None,
                       "buy_price": buy_price, "buy_dip": buy_dip,
                       "ticker": buy_ticker},
                close_ts=_close_ts,
            )

            # --- Sizing (port of _auto_bet_momentum) ---
            from bot.sizing import kelly_size

            side = "yes"
            price_cents = buy_price
            sizing_dip = buy_dip if not conviction_mode else 4

            # Fair probability: prefer GameContext win_prob, else
            # fallback to price + dip assumption.
            if (d["game_ctx"] and d["game_ctx"]._snapshots
                    and d["game_ctx"].win_probability > 0):
                fair_prob = min(0.95, d["game_ctx"].win_probability)
            else:
                fair_prob = min(0.95, (buy_price + sizing_dip) / 100.0)

            balance = d.get("balance", 500.0)
            size_mult = self._dip_size_multiplier(sizing_dip, d["sport"] or "")
            assumed_edge = sizing_dip / 100.0
            confidence = 0.80
            sizing = kelly_size(
                edge=assumed_edge,
                probability=fair_prob,
                balance=balance,
                price_cents=price_cents,
                confidence=confidence,
            )
            if sizing["contracts"] <= 0:
                # Production logs debug + returns. We hold (no order).
                return state, Hold(reason="")

            import math
            scaled_contracts = max(1, math.floor(sizing["contracts"] * size_mult))

            # Conviction sizing
            if conviction_mode:
                from bot.config import CONVICTION_SIZE_FACTOR
                scaled_contracts = max(1, int(scaled_contracts * CONVICTION_SIZE_FACTOR))

            # Instinct halving
            from bot.game_context import SportInstincts
            bet_instincts = SportInstincts.detect(
                d["game_ctx"], d["last_espn_data"], d["sport"] or ""
            )
            if bet_instincts.should_reduce_size:
                scaled_contracts = max(1, scaled_contracts // 2)

            # Sport-profile cap
            max_contracts = sport_profile.get("max_contracts")
            if max_contracts and scaled_contracts > max_contracts:
                scaled_contracts = max_contracts

            sizing["contracts"] = scaled_contracts
            sizing["total_cost"] = round(scaled_contracts * price_cents / 100.0, 2)
            sizing["max_payout"] = round(scaled_contracts * 1.0, 2)

            gc = d["game_ctx"]
            entry_record = {
                "ticker": buy_ticker,
                "side": side,
                "entered_at": time.time(),
                "contracts": scaled_contracts,
                "price_cents": price_cents,
                "order_id": "PAPER",  # caller may override after execute
                "filled": 0,
                "entry_reason": buy_reason + reentry_tag,
                "entry_mode": "conviction" if conviction_mode else "dip",
                "sport": d["sport"] or "",
                "dip_cents": sizing_dip,
                "dqs_score": buy_dqs,
                "game_state": {
                    "score_diff": gc.score_diff if gc else None,
                    # NOTE: production live_watcher.py:1684 uses
                    # `_snapshots[-1].get("period")` which raises
                    # AttributeError on ScoreSnapshot dataclasses.
                    # Production silently swallows via the outer
                    # try/except at live_watcher.py:498. The port has no
                    # such wrapper, so we use attribute access directly
                    # (semantic intent is identical — capture `period`).
                    # This is a port-correctness fix per the Session 19a
                    # Option-1 acknowledgment; affects only telemetry,
                    # not entry/exit decisions or P&L.
                    "period": (gc._snapshots[-1].period
                               if gc and gc._snapshots else None),
                    "completion": round(gc.game_completion_pct, 3) if gc else None,
                    "wp": round(gc.win_probability, 3) if gc else None,
                    "momentum": round(gc.momentum, 3) if gc else None,
                    "lead_trend": round(gc.lead_trend, 3) if gc else None,
                    "wp_edge": (round(gc.win_probability - price_cents / 100.0, 3)
                                if gc else None),
                } if gc else None,
                "instincts": bet_instincts.flags if bet_instincts.flags else [],
                "size_multiplier": size_mult,
            }
            d["bets_placed"].append(entry_record)
            d["entry_count"] += 1
            d["tick_telem"]["execute_success"] += 1

            return state, Buy(
                side=side, qty=scaled_contracts,
                reason=f"MOMENTUM: {buy_reason + reentry_tag}",
                ticker=buy_ticker, price_cents=price_cents,
                extra={
                    "dip_cents": sizing_dip,
                    "dqs_score": buy_dqs,
                    "dqs_breakdown": dqs_breakdown,
                    "conviction": conviction_mode,
                    "fair_prob": fair_prob,
                    "size_multiplier": size_mult,
                    "sport": d["sport"] or "",
                    "instincts": bet_instincts.flags if bet_instincts.flags else [],
                    "market": buy_market,
                    "entry_record": entry_record,
                    "sizing": sizing,
                },
            )

        return state, Hold(reason="")

    def _get_sport_profile(self, sport: str) -> dict:
        """Mirror LiveGameWatcher._get_sport_profile. Looks up
        bot.config.SPORT_PROFILES keyed by sport, falls back to a
        hardcoded default dict (NOT a SPORT_PROFILES['default'] entry —
        production builds the default inline from MOMENTUM_DIP_BUY /
        MOMENTUM_DIP_MAX). Line numbers are omitted intentionally —
        function names are stable, line numbers drift on every edit.

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
        """Mirror LiveGameWatcher._dip_size_multiplier. Dip-scaled
        sizing: large dips = best signal = biggest bet. Line numbers
        are omitted intentionally — function names are stable, line
        numbers drift on every edit.

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
        """Mirror LiveGameWatcher._variance_quality_ok. Tier 2.4
        lightweight dip-quality gate for sports that skip full DQS —
        rejects flat dips during set breaks/changeovers/timeouts.
        Line numbers are omitted intentionally — function names are
        stable, line numbers drift on every edit.

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
        """Mirror LiveGameWatcher._log_decision_dampened. Only emits
        log_decision on (decision, reason) state change. Line numbers
        are omitted intentionally — function names are stable, line
        numbers drift on every edit.

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
