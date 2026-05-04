#!/usr/bin/env python3
"""Session 19b tick-replay back-tester.

Replays historical live_ticks.jsonl streams through any TickStrategy
(currently LiveMomentumStrategy) and emits per-game realized P&L.

Primary deliverable: parity_check() runs the back-tester against 10
known paper trades and asserts |replay P&L - paper P&L| <= 1c. This is
the differential validation of the 19a port (per the Option-1
acknowledgment in commit 46c4978).

Bonus: --fix-peak-tracking-bug simulates the one-line fix to the
production peak-tracking bug at bot/live_watcher.py:2225-2228 (mirrored
at bot/strategies/live_momentum.py:266-270) and reports P&L delta.

Usage:
    python3 tools/tick_backtest.py --paper-trades 10
    python3 tools/tick_backtest.py --paper-trades 10 --fix-peak-tracking-bug
    python3 tools/tick_backtest.py --debug-ticker KXNBAGAME-26APR26CLETOR-CLE
    python3 tools/tick_backtest.py --paper-trades 10 --min-entry-date 2026-04-23
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "bot" / "state"
PAPER_TRADES_FILE = STATE_DIR / "paper_trades.json"
CLV_FILE = STATE_DIR / "clv.json"
LIVE_JOURNAL_FILE = STATE_DIR / "live_journal.json"

# Pre-Apr-18 live_ticks.jsonl rows lack `bid`/`opp_bid` fields — production's
# _log_tick (bot/live_watcher.py:1473) only started writing them in commit
# c0c5049 (2026-04-18). Earlier rows drift from production exit prices because
# the port's current_value falls back to yes_ask when yes_bid is None, while
# production at runtime always had a real bid from the live Kalshi API.
# 2026-04-23 is the conservative cutoff used by --min-entry-date by default.
DEFAULT_MIN_ENTRY_DATE = "2026-04-23"

# Buffer added to the parity-comparison window (max paper resolved_at + buffer).
# Production stops watching the game shortly after position exit; the parity
# check honors that by capping the wide replay window. The wide window remains
# available for back_test() (used by 19c sweeps) — see parity_window kwarg.
PARITY_WINDOW_BUFFER_SECONDS = 120

sys.path.insert(0, str(ROOT))
from bot.strategies import Market, Tick, State, Buy, Sell, Hold, TickAction  # noqa: E402
from bot.strategies.live_momentum import LiveMomentumStrategy  # noqa: E402
from bot.kalshi_history import fetch_settled_close as _fetch_settled_close  # noqa: E402
from bot.regime import _ticker_to_sport  # noqa: E402
from bot.config import MOMENTUM_DISABLED_SPORTS  # noqa: E402
from tools.exit_replay import load_tick_index, slice_ticks  # noqa: E402


SETTLED_STATUSES = {"exited_early", "won", "lost"}

# Maps live_ticks.jsonl ticker prefix → MOMENTUM_DISABLED_SPORTS keys.
# The disabled-sports gate is checked against `sport_lc = (state.data["sport"] or "").lower()`,
# which the back-tester sets via `bot.regime._ticker_to_sport(ticker)`. For pre-flight
# trade filtering (parity sample selection) we use the same prefix→sport mapping.
_TICKER_PREFIX_TO_SPORT = {
    "KXNBAGAME": "nba",
    "KXNHLGAME": "nhl",
    "KXMLBGAME": "mlb",
    "KXUFCFIGHT": "ufc",
    "KXIPLGAME": "ipl",
    "KXATPMATCH": "atp",
    "KXATPCHALLENGERMATCH": "atp_challenger",
    "KXWTAMATCH": "wta",
    "KXWTACHALLENGERMATCH": "wta_challenger",
}


def _is_disabled_sport(ticker: str) -> bool:
    """Return True if the ticker's sport is in MOMENTUM_DISABLED_SPORTS.

    The current strategy gates entry on disabled sports — replaying historical
    trades from those sports would always show 0 round-trips because the gate
    blocks entry. Filter such trades out of the parity sample.
    """
    prefix = ticker.split("-")[0]
    sport = _TICKER_PREFIX_TO_SPORT.get(prefix, "")
    return sport in MOMENTUM_DISABLED_SPORTS


@dataclass
class RoundTrip:
    """One buy->sell pair captured during replay."""
    ticker: str
    side: str
    qty: int
    entry_price_cents: int
    exit_price_cents: int
    entry_ts: str
    exit_ts: str
    exit_reason: str

    @property
    def gross_pnl_cents(self) -> int:
        return (self.exit_price_cents - self.entry_price_cents) * self.qty


@dataclass
class ReplayResult:
    ticker: str
    actions: list
    round_trips: list[RoundTrip]
    realized_pnl_cents: int
    exit_reason: str
    # Last tick timestamp seen during replay (None if stream was empty). Used by
    # parity_check() to classify COVERAGE_GAP vs FAIL — when the archive's last
    # tick precedes production's resolved_at, the port can't have observed the
    # exit production saw and the divergence is a data-coverage limitation, not
    # a port bug.
    last_tick_ts: Optional[str] = None


@dataclass
class ParityFailure:
    ticker: str
    paper_pnl_cents: int
    replay_pnl_cents: int
    delta_cents: int
    note: str = ""
    # Coverage-gap diagnostics — populated only on the coverage_gaps list.
    last_tick_ts: Optional[str] = None
    last_resolved_at: Optional[str] = None


@dataclass
class ParityReport:
    passes: int
    failures: list[ParityFailure]
    skipped: list[tuple[str, str]]
    # Tickers where the divergence is a tick-archive coverage gap (last
    # available tick precedes production's resolved_at). Reported separately
    # from FAIL so the gate can honestly exclude them from the genuine sample.
    coverage_gaps: list[ParityFailure] = field(default_factory=list)


@dataclass
class BackTestResult:
    games: int
    total_pnl_cents: int
    per_game: list[ReplayResult]


# --- Schema adapters --------------------------------------------------------


def _row_to_tick(
    row: dict,
    opp_ticker: Optional[str] = None,
    close_ts: Optional[str] = None,
) -> Tick:
    """Translate a live_ticks.jsonl row into a Tick dataclass.

    Row schema (from bot/live_watcher.py:_log_tick): price=yes_ask,
    bid=yes_bid, opp_price=opp_yes_ask, opp_bid=opp_yes_bid, espn=full
    ESPN snapshot. no_* fields are derived as 100-bid and 100-price.

    `opp_ticker` and `close_ts` come from the caller (paper trade record
    + settlement lookup) since they aren't logged in tick rows.
    """
    yes_ask = row.get("price")
    yes_bid = row.get("bid")
    opp_yes_ask = row.get("opp_price")
    opp_yes_bid = row.get("opp_bid")

    return Tick(
        ts=row["ts"],
        ticker=row["ticker"],
        yes_ask=yes_ask,
        yes_bid=yes_bid,
        no_ask=(100 - yes_bid) if yes_bid is not None else None,
        no_bid=(100 - yes_ask) if yes_ask is not None else None,
        opp_ticker=opp_ticker,
        opp_yes_ask=opp_yes_ask,
        opp_yes_bid=opp_yes_bid,
        opp_no_ask=(100 - opp_yes_bid) if opp_yes_bid is not None else None,
        opp_no_bid=(100 - opp_yes_ask) if opp_yes_ask is not None else None,
        wp=row.get("wp"),
        score_diff=row.get("score_diff"),
        period=row.get("period"),
        espn_data=row.get("espn"),
        close_ts=close_ts,
        raw={},
        raw_opp={},
    )


# --- Paper trade loader -----------------------------------------------------


def load_paper_trades(strategy_name: str = "live_momentum") -> list[dict]:
    """Load + filter + sort paper trades from bot/state/paper_trades.json.

    Filters: type == strategy_name, status in {exited_early, won, lost},
    resolved_at is set. Sorted by timestamp (entry) ascending.
    """
    if not PAPER_TRADES_FILE.exists():
        return []
    data = json.loads(PAPER_TRADES_FILE.read_text())
    out = [
        t for t in data
        if t.get("type") == strategy_name
        and t.get("status") in SETTLED_STATUSES
        and t.get("resolved_at")
    ]
    out.sort(key=lambda t: t.get("timestamp", ""))
    return out


# --- Journal events for --debug-ticker --------------------------------------


def _load_journal_events_for_ticker(ticker: str) -> list[dict]:
    """Return bet/exit events from live_journal.json filtered to `ticker`,
    sorted by timestamp ascending.

    Used by --debug-ticker for side-by-side comparison of port action trace
    vs production journal events. Reads bot/state/live_journal.json directly;
    no schema conversion — caller renders fields it cares about.
    """
    if not LIVE_JOURNAL_FILE.exists():
        return []
    try:
        data = json.loads(LIVE_JOURNAL_FILE.read_text())
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        events = entry.get("events", [])
        if not isinstance(events, list):
            continue
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("event") not in {"bet", "exit"}:
                continue
            if ev.get("ticker") != ticker:
                continue
            # Carry top-level timestamp through if event lacks one.
            ts = ev.get("ts") or ev.get("timestamp") or entry.get("ts") or ""
            out.append({**ev, "_ts": ts})
    out.sort(key=lambda e: e.get("_ts", ""))
    return out


# --- Parity window helpers --------------------------------------------------


def _parse_iso(ts: str) -> Optional[datetime]:
    """Lenient ISO 8601 parse. Returns None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _add_seconds_to_iso(ts: str, seconds: int) -> Optional[str]:
    """Add `seconds` to an ISO 8601 timestamp; return ISO string or None."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return (dt + timedelta(seconds=seconds)).isoformat()


def _max_resolved_at(trades: list[dict]) -> Optional[str]:
    """Return the latest non-empty `resolved_at` across trades, or None."""
    candidates = [t.get("resolved_at", "") for t in trades if t.get("resolved_at")]
    return max(candidates) if candidates else None


# --- Settlement lookup ------------------------------------------------------


_CLV_INDEX_CACHE: Optional[dict[str, str]] = None


def _load_clv_index() -> dict[str, str]:
    """Build {ticker: settled_at} index from clv.json. Cached per-process."""
    global _CLV_INDEX_CACHE
    if _CLV_INDEX_CACHE is not None:
        return _CLV_INDEX_CACHE
    if not CLV_FILE.exists():
        _CLV_INDEX_CACHE = {}
        return _CLV_INDEX_CACHE
    data = json.loads(CLV_FILE.read_text())
    idx: dict[str, str] = {}
    for r in data:
        if r.get("settled_at") and r.get("ticker"):
            idx[r["ticker"]] = r["settled_at"]
    _CLV_INDEX_CACHE = idx
    return idx


def get_settlement_ts(ticker: str) -> Optional[str]:
    """Return ISO 8601 settlement timestamp for `ticker`, or None.

    Source priority:
      1. clv.json (settled_at). Exact ts.
      2. bot.kalshi_history.fetch_settled_close - returns the closing
         price (100/0/None). If non-None we know the market is settled
         but don't have a precise ts - return the sentinel "settled" and
         let the caller cap the tick window at the last available tick.
    """
    idx = _load_clv_index()
    if ticker in idx:
        return idx[ticker]
    close = _fetch_settled_close(ticker)
    if close is not None:
        return "settled"
    return None


# --- Tick stream loader -----------------------------------------------------


def load_tick_stream(
    ticker: str,
    t_start: Optional[str] = None,
    t_end: Optional[str] = None,
    *,
    opp_ticker: Optional[str] = None,
    close_ts: Optional[str] = None,
) -> list[Tick]:
    """Load + slice + translate ticks for one ticker over [t_start, t_end].

    Reuses tools/exit_replay.load_tick_index (reads live_ticks.jsonl +
    all gzipped archives) and slice_ticks (bisect-based, inclusive).

    Window bounds (per user spec - wide window from Prereq A of 18.5):
      - `t_start=None` -> first available tick for ticker.
      - `t_end="settled"` or None -> last available tick.
      - Explicit ts values are honored (inclusive on both ends).
    """
    index = load_tick_index()
    rows = index.get(ticker, [])
    if not rows:
        return []
    if t_start is None:
        t_start = rows[0].get("ts", "")
    if t_end in (None, "settled"):
        t_end = rows[-1].get("ts", t_start)
    sliced = slice_ticks(rows, t_start, t_end)
    return [_row_to_tick(r, opp_ticker=opp_ticker, close_ts=close_ts) for r in sliced]


# --- Replay loop ------------------------------------------------------------


def _format_trace_line(strategy, state: State, tick: Tick, action: TickAction) -> str:
    """Render a single per-tick line for --debug-ticker output.

    Recomputes gate deltas inline against the strategy's sport profile rather
    than reaching into the port's gate evaluation — keeps the trace honest
    even if the port's internal accounting drifts. Held/entry/peak come from
    state.data after process_tick has run.
    """
    d = state.data
    bets = d.get("bets_placed", [])
    held = "NO"
    entry = current = peak = 0
    gain = 0
    tp_delta = sl_delta = trail_delta = "—"
    if bets:
        bet = bets[0]
        held_side = (bet.get("side") or "").lower()
        held = held_side.upper() or "?"
        entry = int(bet.get("price_cents") or 0)
        if held_side == "yes":
            current = tick.yes_bid if tick.yes_bid is not None else int(tick.yes_ask or 0)
        else:
            current = 100 - int(tick.yes_ask or 100)
        gain = int(current) - entry
        peak = int(d.get("peak_values", {}).get(bet.get("ticker"), current))
        sport_profile = strategy._get_sport_profile(d.get("sport") or "")
        sport_tp = sport_profile.get("take_profit", strategy._take_profit_cents)
        sport_sl = sport_profile.get("stop_loss", strategy._stop_loss_cents)
        sport_trail = sport_profile.get("trail_stop", strategy._trail_stop_cents)
        tp_delta = f"{sport_tp - gain:+d}"
        sl_delta = f"{sport_sl - (entry - current):+d}"
        trail_delta = f"{sport_trail - (peak - current):+d}"
    action_name = type(action).__name__
    reason = getattr(action, "reason", "") or ""
    return (
        f"[{tick.ts}] yes_ask={tick.yes_ask} yes_bid={tick.yes_bid} "
        f"held={held} entry={entry} current={current} gain={gain:+d} peak={peak} | "
        f"TP-Δ={tp_delta} SL-Δ={sl_delta} trail-Δ={trail_delta} | "
        f"action={action_name} reason={reason}"
    )


def replay_game(
    strategy,
    market: Market,
    tick_stream: list[Tick],
    *,
    sport: str = "",
    opponent_ticker: Optional[str] = None,
    balance: float = 500.0,
    slippage_cents: int = 2,
    fix_peak_tracking_bug: bool = False,
    trace: bool = False,
    qty_override: Optional[list[int]] = None,
) -> ReplayResult:
    """Run a TickStrategy over a tick stream and return realized P&L.

    Tracks Buy/Sell as round-trips. Slippage is subtracted from each
    round-trip's gross P&L (default +2c pessimism per round-trip).

    If `fix_peak_tracking_bug` is True, after each Buy we inject
    state.data["peak_values"][ticker] = entry_price_cents to simulate the
    one-line fix to the production bug at bot/live_watcher.py:2225-2228 /
    live_momentum.py:266-270.

    If `trace` is True, prints a per-tick action trace to stdout (--debug-ticker).

    If `qty_override` is provided, the Nth Buy emitted has its qty replaced
    with qty_override[N]. Used by parity_check: the port's sizing math
    (kelly + multipliers + sport-cap + instinct halving) depends on the
    bot's real-time balance, which paper_trades.json does not record. To
    measure GATE fidelity (entry/exit decisions, prices, reasons) without
    conflating SIZING fidelity (a separate subsystem with its own
    correctness story), we lock contracts to paper's recorded value.
    Beyond the override list length, the port's emitted qty is preserved.
    """
    from bot import decisions as _decisions
    orig_log = _decisions.log_decision
    _decisions.log_decision = lambda *args, **kwargs: None
    try:
        state = strategy.init_state(
            market,
            sport=sport,
            opponent_ticker=opponent_ticker,
            balance=balance,
            mode="momentum",
            match_title="",
        )
        actions: list = []
        round_trips: list[RoundTrip] = []
        open_buys: dict[str, tuple[Buy, str]] = {}
        last_exit_reason = "no_exit"
        last_tick_ts: Optional[str] = None
        buy_count = 0  # tracks index into qty_override

        for tick in tick_stream:
            state, action = strategy.process_tick(state, tick)
            last_tick_ts = tick.ts
            if isinstance(action, Buy) and qty_override is not None:
                if buy_count < len(qty_override):
                    new_qty = qty_override[buy_count]
                    action = Buy(
                        side=action.side, qty=new_qty, reason=action.reason,
                        ticker=action.ticker, price_cents=action.price_cents,
                        extra=action.extra,
                    )
                    # Mutate the open entry record so the matching Sell picks
                    # up the same contracts (Sell.qty = bet["contracts"]).
                    bets = state.data.get("bets_placed", [])
                    if bets:
                        bets[-1]["contracts"] = new_qty
                buy_count += 1
            actions.append(action)
            if isinstance(action, Buy):
                open_buys[action.ticker] = (action, tick.ts)
                if fix_peak_tracking_bug:
                    state.data.setdefault("peak_values", {})[action.ticker] = action.price_cents
            elif isinstance(action, Sell):
                buy_pair = open_buys.pop(action.ticker, None)
                if buy_pair is not None:
                    buy, entry_ts = buy_pair
                    round_trips.append(RoundTrip(
                        ticker=action.ticker,
                        side=action.side,
                        qty=buy.qty,
                        entry_price_cents=buy.price_cents,
                        exit_price_cents=action.exit_price,
                        entry_ts=entry_ts,
                        exit_ts=tick.ts,
                        exit_reason=action.reason,
                    ))
                    last_exit_reason = action.reason
            if trace:
                print(_format_trace_line(strategy, state, tick, action))

        gross = sum(rt.gross_pnl_cents for rt in round_trips)
        slippage = slippage_cents * len(round_trips)
        return ReplayResult(
            ticker=market.ticker,
            actions=actions,
            round_trips=round_trips,
            realized_pnl_cents=gross - slippage,
            exit_reason=last_exit_reason,
            last_tick_ts=last_tick_ts,
        )
    finally:
        _decisions.log_decision = orig_log


# --- Per-trade replay + parity check ----------------------------------------


def _paper_pnl_cents(trade: dict) -> int:
    """Convert paper_trades.json `pnl` (dollars) to cents."""
    return int(round(trade.get("pnl", 0.0) * 100))


def _paper_trade_to_market(trade: dict) -> Market:
    """Synthesize a minimal Market from a paper trade record. The
    strategy's init_state only reads market.ticker / market.close_ts.
    """
    return Market(
        ticker=trade["ticker"],
        series_ticker=trade["ticker"].rsplit("-", 1)[0],
        event_ticker=None,
        status="finalized",
        close_ts=None,
        yes_ask=int(round(trade.get("entry_price", 0.0) * 100)),
        yes_bid=int(round(trade.get("entry_price", 0.0) * 100)),
        no_ask=None,
        no_bid=None,
        volume_24h=None,
        open_interest=None,
    )


def _replay_paper_trade(
    strategy,
    trade: dict,
    *,
    ticker_trades: Optional[list[dict]] = None,
    slippage_cents: int = 2,
    fix_peak_tracking_bug: bool = False,
    parity_window: bool = False,
    trace: bool = False,
    qty_override: Optional[list[int]] = None,
) -> Optional[ReplayResult]:
    """Run replay_game for one paper trade. Returns None if we can't
    resolve a settlement window or have no tick coverage.

    Window:
      - Default (parity_window=False): [first_tick, settlement_ts_or_last_tick]
        — the WIDE window per Prereq A from 18.5. Production exit_ts is NOT
        the upper bound; 19c sweeps need access to ticks past the production
        exit to honestly evaluate variants that delay exit.
      - parity_window=True: [first_tick, max(resolved_at)+PARITY_WINDOW_BUFFER_SECONDS].
        Production faithfully reproduces a quirk: it stops watching the game
        shortly after the position exits. The wide window over-trades on the
        post-exit tail because the port has no equivalent "stopped watching"
        signal. The parity comparison must mirror production's actual watch
        window — `ticker_trades` (all paper trades for this ticker) provides
        max resolved_at; PARITY_WINDOW_BUFFER_SECONDS adds slack for the next
        legal re-entry that would have happened in production.

    `trace=True` is forwarded to replay_game for --debug-ticker output.
    """
    ticker = trade["ticker"]
    settlement_ts = get_settlement_ts(ticker)
    if settlement_ts is None:
        return None
    if parity_window and ticker_trades:
        max_resolved = _max_resolved_at(ticker_trades)
        capped = (
            _add_seconds_to_iso(max_resolved, PARITY_WINDOW_BUFFER_SECONDS)
            if max_resolved else None
        )
        # Use the capped window if we have one; otherwise fall back to settled.
        t_end = capped or settlement_ts
    else:
        t_end = settlement_ts
    ticks = load_tick_stream(
        ticker,
        t_start=None,
        t_end=t_end,
        opp_ticker=None,
        close_ts=None,
    )
    if not ticks:
        return None
    market = _paper_trade_to_market(trade)
    sport = _ticker_to_sport(ticker) or ""
    return replay_game(
        strategy,
        market,
        ticks,
        sport=sport,
        opponent_ticker=None,
        balance=500.0,
        slippage_cents=slippage_cents,
        fix_peak_tracking_bug=fix_peak_tracking_bug,
        trace=trace,
        qty_override=qty_override,
    )


def parity_check(
    paper_trades: list[dict],
    strategy,
    *,
    tolerance_cents: int = 1,
    slippage_cents: int = 0,
    fix_peak_tracking_bug: bool = False,
) -> ParityReport:
    """For each unique ticker in `paper_trades`, replay the strategy
    over the parity window and compare aggregated P&L to the SUM of all
    paper trade pnls for that ticker.

    Window: [first_tick, max(resolved_at) + PARITY_WINDOW_BUFFER_SECONDS].
    PRODUCTION QUIRK faithfully reproduced: production stops watching the
    game shortly after position exit; the wide-window replay used for 19c
    sweeps over-trades on the post-exit tail. parity_check honors the
    actual production watch window so the diff measures port faithfulness,
    not back-tester window choice.

    Per-ticker aggregation reconciles two facts:
      1. The strategy may re-enter within the parity window
         (`MOMENTUM_REENTRY_COOLDOWN` allows it).
      2. paper_trades.json records ONE round-trip per row - multiple
         entries on the same ticker produce multiple rows.
      Comparing per-ticker totals handles both single- and multi-entry
      cases without drift.

    Tickers whose archive's last tick precedes their max(resolved_at) are
    classified as COVERAGE_GAP rather than FAIL — the port can't have seen
    the exit production saw, so the divergence is a data limitation.

    `slippage_cents` defaults to 0 here: paper_trades.json already records
    realized P&L from actual fills, so the +2¢ forward-projection pessimism
    used by back_test() (for 19c) would just create a constant 2¢ × N_round_trips
    offset that has nothing to do with port faithfulness. Callers can override
    if they want a slippage-inclusive comparison.
    """
    by_ticker: dict[str, list[dict]] = {}
    for t in paper_trades:
        by_ticker.setdefault(t["ticker"], []).append(t)

    passes = 0
    failures: list[ParityFailure] = []
    skipped: list[tuple[str, str]] = []
    coverage_gaps: list[ParityFailure] = []
    for ticker, trades in by_ticker.items():
        paper_sum = sum(_paper_pnl_cents(t) for t in trades)
        first_trade = min(trades, key=lambda t: t.get("timestamp", ""))
        # Lock contracts to paper's recorded values so parity measures gate
        # fidelity, not balance-state-dependent sizing math. Order by entry
        # timestamp so the Nth port-emitted Buy aligns with the Nth paper entry.
        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", ""))
        qty_override = [
            int(t.get("contracts") or 0)
            for t in sorted_trades
            if t.get("contracts")
        ]
        result = _replay_paper_trade(
            strategy, first_trade,
            ticker_trades=trades,
            slippage_cents=slippage_cents,
            fix_peak_tracking_bug=fix_peak_tracking_bug,
            parity_window=True,
            qty_override=qty_override or None,
        )
        if result is None:
            skipped.append((ticker, "no settlement or no tick coverage"))
            continue
        delta = result.realized_pnl_cents - paper_sum
        note = f"{len(trades)} entries" if len(trades) > 1 else ""
        if abs(delta) <= tolerance_cents:
            passes += 1
            continue
        # Classify divergences > tolerance: COVERAGE_GAP if the archive's
        # last tick precedes production's resolved_at (port can't have
        # observed the exit production saw); otherwise a genuine FAIL.
        max_resolved = _max_resolved_at(trades)
        is_coverage_gap = bool(
            result.last_tick_ts and max_resolved
            and result.last_tick_ts < max_resolved
        )
        record = ParityFailure(
            ticker=ticker,
            paper_pnl_cents=paper_sum,
            replay_pnl_cents=result.realized_pnl_cents,
            delta_cents=delta,
            note=note,
            last_tick_ts=result.last_tick_ts if is_coverage_gap else None,
            last_resolved_at=max_resolved if is_coverage_gap else None,
        )
        if is_coverage_gap:
            coverage_gaps.append(record)
        else:
            failures.append(record)
    return ParityReport(
        passes=passes, failures=failures,
        skipped=skipped, coverage_gaps=coverage_gaps,
    )


def back_test(
    strategy,
    paper_trades: Optional[list[dict]] = None,
    *,
    slippage_cents: int = 2,
    fix_peak_tracking_bug: bool = False,
) -> BackTestResult:
    """General entry: replay each paper trade and aggregate.

    Defaults to all settled live_momentum trades. Per-ticker dedup so
    each unique ticker is replayed once even if multiple paper trade
    rows reference it.
    """
    if paper_trades is None:
        paper_trades = load_paper_trades(
            strategy_name=getattr(strategy, "name", "live_momentum")
        )
    seen: set[str] = set()
    per_game: list[ReplayResult] = []
    for trade in paper_trades:
        ticker = trade["ticker"]
        if ticker in seen:
            continue
        seen.add(ticker)
        result = _replay_paper_trade(
            strategy, trade,
            slippage_cents=slippage_cents,
            fix_peak_tracking_bug=fix_peak_tracking_bug,
        )
        if result is not None:
            per_game.append(result)
    total = sum(r.realized_pnl_cents for r in per_game)
    return BackTestResult(games=len(per_game), total_pnl_cents=total, per_game=per_game)


# --- CLI / Markdown ---------------------------------------------------------


def _select_diverse_paper_trades(trades: list[dict], n: int) -> list[dict]:
    """Pick up to N trades round-robin across sports (ticker prefix).

    Honors user spec ('Spread across sports - NBA, UFC, MLB at minimum').
    Within each sport bucket, preserves the chronological order from
    load_paper_trades (sorted by timestamp ascending).
    """
    by_sport: dict[str, list[dict]] = {}
    for t in trades:
        sport = t["ticker"].split("-")[0]
        by_sport.setdefault(sport, []).append(t)
    out: list[dict] = []
    while len(out) < n and any(v for v in by_sport.values()):
        for sport in list(by_sport.keys()):
            if not by_sport[sport]:
                continue
            out.append(by_sport[sport].pop(0))
            if len(out) >= n:
                break
    return out


def render_parity_report(
    report: ParityReport,
    paper_trades: list[dict],
    replay_results: dict[str, ReplayResult],
    tolerance_cents: int,
) -> str:
    """Markdown table per the user spec. Per-ticker grouping (sums all
    paper trade pnls for a given ticker)."""
    by_ticker: dict[str, list[dict]] = {}
    for t in paper_trades:
        by_ticker.setdefault(t["ticker"], []).append(t)

    lines = [
        "## Parity check (vs known paper trades)",
        "",
        "| Ticker | Paper P&L¢ | Replay P&L¢ | Δ¢ | Status | Note |",
        "|---|---|---|---|---|---|",
    ]
    skipped_set = {t for t, _ in report.skipped}
    coverage_set = {f.ticker for f in report.coverage_gaps}
    for ticker, trades in by_ticker.items():
        paper = sum(_paper_pnl_cents(t) for t in trades)
        n_paper = len(trades)
        note = f"{n_paper} entries" if n_paper > 1 else ""
        if ticker in skipped_set:
            lines.append(f"| {ticker} | {paper:+d} | — | — | SKIP | {note} |")
            continue
        result = replay_results.get(ticker)
        if result is None:
            lines.append(f"| {ticker} | {paper:+d} | — | — | SKIP | {note} |")
            continue
        delta = result.realized_pnl_cents - paper
        if abs(delta) <= tolerance_cents:
            status = "PASS"
        elif ticker in coverage_set:
            status = "COVERAGE_GAP"
        else:
            status = "FAIL"
        lines.append(
            f"| {ticker} | {paper:+d} | {result.realized_pnl_cents:+d} | "
            f"{delta:+d} | {status} | {note} |"
        )
    # Genuine sample size = total tickers minus skips minus coverage gaps.
    # Coverage gaps are honest exclusions (data limit, not port bug).
    n_total = len(by_ticker) - len(report.skipped) - len(report.coverage_gaps)
    lines.append("")
    lines.append(
        f"**PASSES: {report.passes}/{n_total} within {tolerance_cents}¢ tolerance** "
        f"(post-coverage-gap exclusion)."
    )
    if report.skipped:
        lines.append(
            f"**SKIPPED: {len(report.skipped)}** — "
            + "; ".join(f"{t} ({r})" for t, r in report.skipped)
        )
    if report.coverage_gaps:
        lines.append("")
        lines.append("### Coverage gaps (rationalized — excluded from gate)")
        lines.append("")
        lines.append(
            "| Ticker | Δ¢ | Last tick | Resolved at | Gap |"
        )
        lines.append("|---|---|---|---|---|")
        for cg in report.coverage_gaps:
            gap_str = "—"
            d_last = _parse_iso(cg.last_tick_ts or "")
            d_res = _parse_iso(cg.last_resolved_at or "")
            if d_last and d_res:
                delta_secs = int((d_res - d_last).total_seconds())
                gap_str = f"{delta_secs}s"
            lines.append(
                f"| {cg.ticker} | {cg.delta_cents:+d} | "
                f"{cg.last_tick_ts or '—'} | {cg.last_resolved_at or '—'} | "
                f"{gap_str} |"
            )
    return "\n".join(lines)


def render_bonus_section(
    bugged: BackTestResult,
    fixed: BackTestResult,
    paper_trades: list[dict],
) -> str:
    """Markdown for --fix-peak-tracking-bug delta (per-ticker)."""
    bugged_by_ticker = {r.ticker: r for r in bugged.per_game}
    fixed_by_ticker = {r.ticker: r for r in fixed.per_game}
    seen: set[str] = set()
    lines = [
        "## Bonus: --fix-peak-tracking-bug mode",
        "",
        "| Ticker | Bugged P&L¢ | Fixed P&L¢ | Δ¢ |",
        "|---|---|---|---|",
    ]
    total_delta = 0
    for trade in paper_trades:
        ticker = trade["ticker"]
        if ticker in seen:
            continue
        seen.add(ticker)
        b = bugged_by_ticker.get(ticker)
        f = fixed_by_ticker.get(ticker)
        if b is None or f is None:
            continue
        delta = f.realized_pnl_cents - b.realized_pnl_cents
        total_delta += delta
        lines.append(
            f"| {ticker} | {b.realized_pnl_cents:+d} | "
            f"{f.realized_pnl_cents:+d} | {delta:+d} |"
        )
    lines.append("")
    lines.append(
        f"**Total P&L delta from peak-tracking fix: {total_delta:+d}¢** "
        f"({bugged.total_pnl_cents:+d}¢ bugged → {fixed.total_pnl_cents:+d}¢ fixed)"
    )
    return "\n".join(lines)


# --- Sub-session 19c: parameter sweep with train/test split ----------------
#
# Sweeps a 2D grid (MOMENTUM_LEADER_MIN × MOMENTUM_DQS_TRAIL_STOP) over the
# training set, validates the top-3 training variants on a held-out test set,
# and emits per-sport + per-regime breakdown for the best test variant.
#
# Discipline (carried from 18.5 + spec):
#   - Sample sorted by entry timestamp ascending (matches load_paper_trades).
#   - 70/30 split by COUNT (not date span). Train = first 70% by index.
#   - Sweep runs on training set only; test set is touched only to validate
#     the top-3 training variants.
#   - Decision number is TEST set Σ P&L delta vs production baseline. Training
#     ranks; only test gates shipping.
#   - Slippage = 2¢/round-trip on both train and test (Prereq 4).


# Production baseline as of Sub-session 19c (LM=0.65 shipped Apr 27,
# was 0.70 pre-Session-19c). Session 41 (Apr 30) restated to current production
# while extending the back-tester for the TP/SL sweep.
SWEEP_BASELINE = (0.65, 6)  # (MOMENTUM_LEADER_MIN, MOMENTUM_DQS_TRAIL_STOP)

SWEEP_GRID_PRIMARY: list[tuple[float, int]] = [
    (lm, ts)
    for lm in (0.65, 0.70, 0.75)
    for ts in (4, 5, 6, 7, 8)
]

# --- Session 41: TP/SL sweep grid -------------------------------------------
# Holds (LM=0.65, TS=6) fixed at current production; varies LIVE_TAKE_PROFIT_CENTS
# and LIVE_STOP_LOSS_CENTS only. 12 variants per the user's Session 41 spec
# table (skips fringe combos: (10,35) / (14,35) / (16,30) / (16,35) where
# TP > SL has weird semantics or the ratio is far from interesting). 11 of
# the 12 are non-baseline test variants; (12, 30) is the baseline.
SWEEP_BASELINE_TP_SL = (12, 30)  # (LIVE_TAKE_PROFIT_CENTS, LIVE_STOP_LOSS_CENTS)

SWEEP_GRID_TP_SL: list[tuple[int, int]] = [
    (10, 20), (10, 25), (10, 30),
    (12, 20), (12, 25), (12, 30), (12, 35),
    (14, 20), (14, 25), (14, 30),
    (16, 20), (16, 25),
]

# LM/TS values held fixed during Session 41 TP/SL sweep (current production).
SWEEP_TP_SL_FIXED_LM = 0.65
SWEEP_TP_SL_FIXED_TS = 6

# --- Session 42: per-sport TP/SL sweep grids --------------------------------
# Each grid is a 12-combo (TP, SL) variant list, anchored on that sport's
# CURRENT SPORT_PROFILES value as one of the variants. Architecture: every
# sport's TP/SL resolves at the gate site as
#   override → SPORT_PROFILES → strategy default
# so passing `sport_overrides={target_sport: {"take_profit": tp, "stop_loss": sl}}`
# to LiveMomentumStrategy is the cleanest way to vary one sport's gates while
# leaving other sports' SPORT_PROFILES untouched.
#
# Per Plan-agent revision #1: this session sweeps ONLY take_profit + stop_loss.
# trail_stop / max_contracts / max_entry / dip_buy / dip_max remain
# SPORT_PROFILES-controlled per sport.
#
# Grid choices (current values from bot/config.py SPORT_PROFILES):
#   - nba: TP=12 / SL=10 baseline. Sweep 12 variants (skips fringes
#     (16,8)/(16,10)/(10,15)/(14,15) where ratio is too aggressive or loose).
#   - nhl: TP=15 / SL=10 baseline. Full 3×4 = 12 combos.
#   - ufc: TP=12 / SL=10 baseline. UFC-specific: TP grid floor=8 (Plan-agent
#     revision #2 — TP=6 below noise floor at Kelly-sized contracts) probes
#     the user's hypothesis that current TP=12 is too WIDE for 123s-median
#     UFC fights.
SWEEP_GRID_TP_SL_PER_SPORT: dict[str, list[tuple[int, int]]] = {
    "nba": [
        (10, 8), (10, 10), (10, 12),
        (12, 8), (12, 10), (12, 12), (12, 15),
        (14, 8), (14, 10), (14, 12),
        (16, 12), (16, 15),
    ],
    "nhl": [
        (12, 8), (12, 10), (12, 12), (12, 15),
        (15, 8), (15, 10), (15, 12), (15, 15),
        (18, 8), (18, 10), (18, 12), (18, 15),
    ],
    "ufc": [
        (8,  6), (8,  8), (8, 10),
        (10, 6), (10, 8), (10, 10),
        (12, 6), (12, 8), (12, 10),
        (14, 6), (14, 8), (14, 10),
    ],
}

# Per-sport baselines (current SPORT_PROFILES values). Decision-gate math
# computes test-Δ relative to these.
SWEEP_BASELINE_TP_SL_PER_SPORT: dict[str, tuple[int, int]] = {
    "nba": (12, 10),
    "nhl": (15, 10),
    "ufc": (12, 10),
}


@dataclass
class VariantResult:
    leader_min: float
    trail_stop_cents: int
    total_pnl_cents: int
    n_replays: int
    n_winning_replays: int
    per_replay: list[ReplayResult]
    # Session 41: optional TP/SL overrides for the TP/SL grid sweep. None on
    # the LM/TS sweep path (Session 19c) — strategy uses LIVE_TAKE_PROFIT_CENTS
    # and LIVE_STOP_LOSS_CENTS defaults from bot.config.
    take_profit_cents: Optional[int] = None
    stop_loss_cents: Optional[int] = None

    @property
    def win_rate(self) -> float:
        return (self.n_winning_replays / self.n_replays) if self.n_replays else 0.0

    @property
    def label(self) -> str:
        # Session 41: when TP/SL overrides are set (TP/SL sweep mode), label
        # by what varies (TP/SL) since LM/TS are held fixed. Otherwise (Session
        # 19c LM/TS sweep), label by LM/TS.
        if self.take_profit_cents is not None and self.stop_loss_cents is not None:
            return f"TP={self.take_profit_cents} SL={self.stop_loss_cents}"
        return f"LM={self.leader_min:.2f} TS={self.trail_stop_cents}"


@dataclass
class SportBreakdown:
    sport: str
    n_replays: int
    total_pnl_cents: int


@dataclass
class RegimeBucket:
    key: str           # e.g. "evening", "regular", "fri"
    n_replays: int
    total_pnl_cents: int


@dataclass
class SweepReport:
    train_n: int
    test_n: int
    train_first_ts: Optional[str]
    train_last_ts: Optional[str]
    test_first_ts: Optional[str]
    test_last_ts: Optional[str]
    slippage_cents: int
    baseline: tuple[float, int]
    training: list[VariantResult]                   # sorted desc by total_pnl_cents
    test_top3: list[VariantResult]                  # for the top-3 training variants
    baseline_test: Optional[VariantResult]          # baseline run on the test set (for decision)
    best_test_variant: Optional[VariantResult]
    best_per_sport: list[SportBreakdown]
    best_per_regime: dict[str, list[RegimeBucket]]  # regime_key -> buckets


def split_train_test(
    trades: list[dict], train_pct: float = 0.70,
) -> tuple[list[dict], list[dict]]:
    """Index split of an entry-time-sorted list. Caller is responsible for
    ensuring `trades` is already sorted ascending by `timestamp` (which is the
    contract of `load_paper_trades`)."""
    if not trades:
        return [], []
    if not 0.0 < train_pct < 1.0:
        raise ValueError(f"train_pct must be in (0,1), got {train_pct}")
    split = int(len(trades) * train_pct)
    return list(trades[:split]), list(trades[split:])


def _run_variant(
    leader_min: float,
    trail_stop_cents: int,
    paper_trades: list[dict],
    *,
    slippage_cents: int = 2,
    take_profit_cents: Optional[int] = None,
    stop_loss_cents: Optional[int] = None,
    sport_overrides: Optional[dict[str, dict]] = None,
) -> VariantResult:
    """Instantiate LiveMomentumStrategy with the given overrides, run
    back_test() over `paper_trades`, and aggregate.

    Session 41: when `take_profit_cents` / `stop_loss_cents` are provided,
    they're threaded through to `LiveMomentumStrategy.__init__` (which
    already accepts both as constructor kwargs at
    bot/strategies/live_momentum.py:59,61). When None, the strategy uses
    its own defaults from bot.config (LIVE_TAKE_PROFIT_CENTS=12 /
    LIVE_STOP_LOSS_CENTS=30) — preserves the Session 19c LM/TS sweep path
    byte-identically.

    Session 42: when `sport_overrides` is provided, threaded through so the
    strategy resolves TP/SL per-sport (override → SPORT_PROFILES → strategy
    default). When None, pre-Session-42 behavior preserved byte-identical.
    """
    kwargs: dict[str, Any] = {
        "leader_min": leader_min,
        "trail_stop_cents": trail_stop_cents,
    }
    if take_profit_cents is not None:
        kwargs["take_profit_cents"] = take_profit_cents
    if stop_loss_cents is not None:
        kwargs["stop_loss_cents"] = stop_loss_cents
    if sport_overrides is not None:
        kwargs["sport_overrides"] = sport_overrides
    strategy = LiveMomentumStrategy(**kwargs)
    bt = back_test(
        strategy,
        paper_trades=paper_trades,
        slippage_cents=slippage_cents,
        fix_peak_tracking_bug=False,
    )
    n_winners = sum(1 for r in bt.per_game if r.realized_pnl_cents > 0)
    return VariantResult(
        leader_min=leader_min,
        trail_stop_cents=trail_stop_cents,
        total_pnl_cents=bt.total_pnl_cents,
        n_replays=bt.games,
        n_winning_replays=n_winners,
        per_replay=bt.per_game,
        take_profit_cents=take_profit_cents,
        stop_loss_cents=stop_loss_cents,
    )


def _aggregate_per_sport(per_replay: list[ReplayResult]) -> list[SportBreakdown]:
    """Bucket ReplayResults by ticker prefix (e.g. KXNBAGAME, KXUFCFIGHT)."""
    buckets: dict[str, list[ReplayResult]] = {}
    for r in per_replay:
        prefix = r.ticker.split("-")[0]
        buckets.setdefault(prefix, []).append(r)
    out = [
        SportBreakdown(
            sport=k,
            n_replays=len(v),
            total_pnl_cents=sum(x.realized_pnl_cents for x in v),
        )
        for k, v in buckets.items()
    ]
    out.sort(key=lambda b: b.sport)
    return out


def _aggregate_per_regime(
    per_replay: list[ReplayResult],
    paper_trades: list[dict],
    regime_key: str,
) -> list[RegimeBucket]:
    """Bucket ReplayResults by regime tag derived from the paper trade's
    `timestamp` and ticker. `regime_key` is one of: time_of_day, day_of_week,
    sport_phase. Tickers without a matching paper trade row (shouldn't happen
    but defend) are bucketed under '_unknown'."""
    from bot.regime import tag as regime_tag

    ts_by_ticker: dict[str, str] = {}
    for t in paper_trades:
        ts_by_ticker.setdefault(t["ticker"], t.get("timestamp", ""))

    buckets: dict[str, list[ReplayResult]] = {}
    for r in per_replay:
        ts_str = ts_by_ticker.get(r.ticker, "")
        ts_dt = _parse_iso(ts_str) if ts_str else None
        bucket = "_unknown"
        if ts_dt is not None:
            tags = regime_tag(ts_dt, r.ticker)
            bucket = str(tags.get(regime_key, "_unknown") or "_none")
        buckets.setdefault(bucket, []).append(r)
    out = [
        RegimeBucket(
            key=k,
            n_replays=len(v),
            total_pnl_cents=sum(x.realized_pnl_cents for x in v),
        )
        for k, v in buckets.items()
    ]
    out.sort(key=lambda b: b.key)
    return out


def run_sweep(
    grid: list[tuple[float, int]],
    train: list[dict],
    test: list[dict],
    *,
    slippage_cents: int = 2,
    top_k: int = 3,
    baseline: tuple[float, int] = SWEEP_BASELINE,
) -> SweepReport:
    """Run the full sweep on `train`, validate top-K on `test`, and aggregate
    per-sport + per-regime for the best test variant."""

    training_results: list[VariantResult] = [
        _run_variant(lm, ts, train, slippage_cents=slippage_cents)
        for (lm, ts) in grid
    ]
    training_results.sort(key=lambda v: v.total_pnl_cents, reverse=True)

    top_variants = training_results[:top_k]
    test_results: list[VariantResult] = [
        _run_variant(v.leader_min, v.trail_stop_cents, test,
                     slippage_cents=slippage_cents)
        for v in top_variants
    ]

    # Always run the production baseline on the test set so the decision number
    # (best test Σ - baseline test Σ) is computable even when the baseline
    # isn't in the top-K training variants.
    lm0, ts0 = baseline
    baseline_test: Optional[VariantResult] = None
    if test:
        baseline_test = _run_variant(lm0, ts0, test, slippage_cents=slippage_cents)

    best_test = max(test_results, key=lambda v: v.total_pnl_cents) if test_results else None
    if best_test is not None:
        best_per_sport = _aggregate_per_sport(best_test.per_replay)
        best_per_regime = {
            "time_of_day": _aggregate_per_regime(best_test.per_replay, test, "time_of_day"),
            "day_of_week": _aggregate_per_regime(best_test.per_replay, test, "day_of_week"),
            "sport_phase": _aggregate_per_regime(best_test.per_replay, test, "sport_phase"),
        }
    else:
        best_per_sport = []
        best_per_regime = {}

    def _ts_range(rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
        if not rows:
            return None, None
        ts = [r.get("timestamp", "") for r in rows if r.get("timestamp")]
        if not ts:
            return None, None
        return min(ts), max(ts)

    train_first, train_last = _ts_range(train)
    test_first, test_last = _ts_range(test)

    return SweepReport(
        train_n=len(train),
        test_n=len(test),
        train_first_ts=train_first,
        train_last_ts=train_last,
        test_first_ts=test_first,
        test_last_ts=test_last,
        slippage_cents=slippage_cents,
        baseline=baseline,
        training=training_results,
        test_top3=test_results,
        baseline_test=baseline_test,
        best_test_variant=best_test,
        best_per_sport=best_per_sport,
        best_per_regime=best_per_regime,
    )


# --- Session 41: TP/SL sweep driver ----------------------------------------


@dataclass
class TpSlSweepReport:
    """Mirror of SweepReport for the Session 41 TP/SL sweep. Same shape,
    different semantics on baseline (TP, SL ints instead of LM, TS) and a
    fixed-axis pair (LM, TS held at production)."""
    train_n: int
    test_n: int
    train_first_ts: Optional[str]
    train_last_ts: Optional[str]
    test_first_ts: Optional[str]
    test_last_ts: Optional[str]
    slippage_cents: int
    leader_min_fixed: float           # held fixed during the sweep
    trail_stop_cents_fixed: int       # held fixed during the sweep
    baseline: tuple[int, int]         # (TP, SL)
    training: list[VariantResult]     # sorted desc by total_pnl_cents
    test_top3: list[VariantResult]
    baseline_test: Optional[VariantResult]
    best_test_variant: Optional[VariantResult]
    best_per_sport: list[SportBreakdown]
    best_per_regime: dict[str, list[RegimeBucket]]


def run_sweep_tp_sl(
    grid: list[tuple[int, int]],
    train: list[dict],
    test: list[dict],
    *,
    slippage_cents: int = 2,
    top_k: int = 3,
    baseline: tuple[int, int] = SWEEP_BASELINE_TP_SL,
    leader_min: float = SWEEP_TP_SL_FIXED_LM,
    trail_stop_cents: int = SWEEP_TP_SL_FIXED_TS,
) -> TpSlSweepReport:
    """Session 41: run TP/SL grid sweep on `train`, validate top-K on `test`.

    Holds (`leader_min`, `trail_stop_cents`) fixed at current production while
    varying (TP, SL) per `grid`. Always runs the production baseline `(TP, SL)`
    on the test set so the decision number (best test Σ - baseline test Σ)
    is computable even when the baseline isn't in the top-K training variants.

    Aggregations (per-sport, per-regime) are computed on the best test variant
    only — same discipline as Session 19c's `run_sweep`.
    """
    training_results: list[VariantResult] = [
        _run_variant(
            leader_min, trail_stop_cents, train,
            slippage_cents=slippage_cents,
            take_profit_cents=tp,
            stop_loss_cents=sl,
        )
        for (tp, sl) in grid
    ]
    training_results.sort(key=lambda v: v.total_pnl_cents, reverse=True)

    top_variants = training_results[:top_k]
    test_results: list[VariantResult] = [
        _run_variant(
            leader_min, trail_stop_cents, test,
            slippage_cents=slippage_cents,
            take_profit_cents=v.take_profit_cents,
            stop_loss_cents=v.stop_loss_cents,
        )
        for v in top_variants
    ]

    tp0, sl0 = baseline
    baseline_test: Optional[VariantResult] = None
    if test:
        baseline_test = _run_variant(
            leader_min, trail_stop_cents, test,
            slippage_cents=slippage_cents,
            take_profit_cents=tp0,
            stop_loss_cents=sl0,
        )

    best_test = max(test_results, key=lambda v: v.total_pnl_cents) if test_results else None
    if best_test is not None:
        best_per_sport = _aggregate_per_sport(best_test.per_replay)
        best_per_regime = {
            "time_of_day": _aggregate_per_regime(best_test.per_replay, test, "time_of_day"),
            "day_of_week": _aggregate_per_regime(best_test.per_replay, test, "day_of_week"),
            "sport_phase": _aggregate_per_regime(best_test.per_replay, test, "sport_phase"),
        }
    else:
        best_per_sport = []
        best_per_regime = {}

    def _ts_range(rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
        if not rows:
            return None, None
        ts = [r.get("timestamp", "") for r in rows if r.get("timestamp")]
        if not ts:
            return None, None
        return min(ts), max(ts)

    train_first, train_last = _ts_range(train)
    test_first, test_last = _ts_range(test)

    return TpSlSweepReport(
        train_n=len(train),
        test_n=len(test),
        train_first_ts=train_first,
        train_last_ts=train_last,
        test_first_ts=test_first,
        test_last_ts=test_last,
        slippage_cents=slippage_cents,
        leader_min_fixed=leader_min,
        trail_stop_cents_fixed=trail_stop_cents,
        baseline=baseline,
        training=training_results,
        test_top3=test_results,
        baseline_test=baseline_test,
        best_test_variant=best_test,
        best_per_sport=best_per_sport,
        best_per_regime=best_per_regime,
    )


def render_sweep_report(report: SweepReport) -> str:
    """Markdown sweep report: header → training table → test table →
    per-sport for best → regime slicing for best → empty findings stub.
    Operator authors the findings section after reading the numbers."""
    lines: list[str] = []
    lm0, ts0 = report.baseline
    lines.append("# Tick-Replay Sweep — Sub-session 19c")
    lines.append("")
    lines.append(
        f"**Sample.** Train N={report.train_n} "
        f"(`{report.train_first_ts}` → `{report.train_last_ts}`); "
        f"Test N={report.test_n} "
        f"(`{report.test_first_ts}` → `{report.test_last_ts}`). "
        f"Trades sorted ascending by entry timestamp; 70/30 split by count."
    )
    lines.append(
        f"**Slippage.** {report.slippage_cents}¢ per round-trip pessimism "
        f"(Prereq 4 — restored from parity's 0¢)."
    )
    lines.append(
        f"**Baseline.** MOMENTUM_LEADER_MIN={lm0:.2f}, "
        f"MOMENTUM_DQS_TRAIL_STOP={ts0}¢ (production)."
    )
    lines.append(
        "**Peak-tracking fix.** Live in production (Session 19a-peakfix); "
        "default replay behavior matches production."
    )
    lines.append("")
    lines.append("## Training sweep (full grid)")
    lines.append("")
    lines.append("| Variant | Σ P&L¢ | n_replays | n_wins | win % |")
    lines.append("|---|---|---|---|---|")
    for v in report.training:
        marker = " ← baseline" if (v.leader_min == lm0 and v.trail_stop_cents == ts0) else ""
        lines.append(
            f"| {v.label}{marker} | {v.total_pnl_cents:+d} | "
            f"{v.n_replays} | {v.n_winning_replays} | "
            f"{v.win_rate*100:.0f}% |"
        )
    lines.append("")

    lines.append("## Test validation (top 3 training variants + baseline)")
    lines.append("")
    lines.append("| Variant | Train Σ¢ | Test Σ¢ | Δ vs baseline test¢ | Test n |")
    lines.append("|---|---|---|---|---|")
    baseline_test_pnl = (
        report.baseline_test.total_pnl_cents if report.baseline_test else None
    )
    train_pnl_lookup = {
        (v.leader_min, v.trail_stop_cents): v.total_pnl_cents for v in report.training
    }

    def _row(v: VariantResult) -> str:
        train_pnl = train_pnl_lookup.get((v.leader_min, v.trail_stop_cents), 0)
        is_baseline = (v.leader_min == lm0 and v.trail_stop_cents == ts0)
        marker = " ← baseline" if is_baseline else ""
        delta_str = "—"
        if baseline_test_pnl is not None and not is_baseline:
            delta_str = f"{v.total_pnl_cents - baseline_test_pnl:+d}"
        elif is_baseline:
            delta_str = "0"
        return (
            f"| {v.label}{marker} | {train_pnl:+d} | "
            f"{v.total_pnl_cents:+d} | {delta_str} | {v.n_replays} |"
        )

    seen_labels: set[str] = set()
    for v in report.test_top3:
        lines.append(_row(v))
        seen_labels.add(v.label)
    # Always show the baseline row, even if it wasn't in the top-3 training variants.
    if report.baseline_test is not None and report.baseline_test.label not in seen_labels:
        lines.append(_row(report.baseline_test))
    lines.append("")

    if report.best_test_variant is not None:
        best = report.best_test_variant
        lines.append(f"## Per-sport breakdown — best test variant ({best.label})")
        lines.append("")
        lines.append("| Sport | n_replays | Σ P&L¢ |")
        lines.append("|---|---|---|")
        for b in report.best_per_sport:
            lines.append(f"| {b.sport} | {b.n_replays} | {b.total_pnl_cents:+d} |")
        lines.append("")

        lines.append(f"## Regime slicing — best test variant ({best.label})")
        lines.append("")
        for regime_key, buckets in report.best_per_regime.items():
            lines.append(f"### By {regime_key}")
            lines.append("")
            lines.append(f"| {regime_key} | n_replays | Σ P&L¢ |")
            lines.append("|---|---|---|")
            for b in buckets:
                lines.append(f"| {b.key} | {b.n_replays} | {b.total_pnl_cents:+d} |")
            lines.append("")

    lines.append("## Findings")
    lines.append("")
    lines.append("_(authored by operator after running)_")
    return "\n".join(lines)


def render_sweep_tp_sl_report(report: TpSlSweepReport) -> str:
    """Session 41 markdown sweep report: same shape as `render_sweep_report`
    but with TP/SL header semantics. The fixed (LM, TS) pair is shown in the
    header to make it explicit those values are not part of the sweep."""
    lines: list[str] = []
    tp0, sl0 = report.baseline
    lines.append("# Tick-Replay Sweep — Session 41 (TP/SL ratio)")
    lines.append("")
    lines.append(
        f"**Sample.** Train N={report.train_n} "
        f"(`{report.train_first_ts}` → `{report.train_last_ts}`); "
        f"Test N={report.test_n} "
        f"(`{report.test_first_ts}` → `{report.test_last_ts}`). "
        f"Trades sorted ascending by entry timestamp; 70/30 split by count."
    )
    lines.append(
        f"**Slippage.** {report.slippage_cents}¢ per round-trip pessimism "
        f"(Prereq 4)."
    )
    lines.append(
        f"**Baseline.** LIVE_TAKE_PROFIT_CENTS={tp0}, "
        f"LIVE_STOP_LOSS_CENTS={sl0} (production, ratio={tp0/sl0:.2f})."
    )
    lines.append(
        f"**Held fixed.** MOMENTUM_LEADER_MIN={report.leader_min_fixed:.2f}, "
        f"MOMENTUM_DQS_TRAIL_STOP={report.trail_stop_cents_fixed}¢ "
        f"(post-Session-19c production)."
    )
    lines.append(
        "**Sport-profile caveat.** Tennis-alias sports have explicit "
        "`take_profit=15`/`stop_loss=8` in SPORT_PROFILES that override the "
        "global default. This sweep affects only sports without that override "
        "(NBA / NHL / UFC / IPL / etc.)."
    )
    lines.append("")
    lines.append("## Training sweep (full grid)")
    lines.append("")
    lines.append("| Variant | Σ P&L¢ | n_replays | n_wins | win % |")
    lines.append("|---|---|---|---|---|")
    for v in report.training:
        is_baseline = (
            v.take_profit_cents == tp0 and v.stop_loss_cents == sl0
        )
        marker = " ← baseline" if is_baseline else ""
        lines.append(
            f"| {v.label}{marker} | {v.total_pnl_cents:+d} | "
            f"{v.n_replays} | {v.n_winning_replays} | "
            f"{v.win_rate*100:.0f}% |"
        )
    lines.append("")

    lines.append("## Test validation (top 3 training variants + baseline)")
    lines.append("")
    lines.append("| Variant | Train Σ¢ | Test Σ¢ | Δ vs baseline test¢ | Test n | Test per-trade Δ¢ |")
    lines.append("|---|---|---|---|---|---|")
    baseline_test_pnl = (
        report.baseline_test.total_pnl_cents if report.baseline_test else None
    )
    baseline_test_n = (
        report.baseline_test.n_replays if report.baseline_test else 0
    )
    train_pnl_lookup = {
        (v.take_profit_cents, v.stop_loss_cents): v.total_pnl_cents
        for v in report.training
    }

    def _row(v: VariantResult) -> str:
        train_pnl = train_pnl_lookup.get(
            (v.take_profit_cents, v.stop_loss_cents), 0
        )
        is_baseline = (v.take_profit_cents == tp0 and v.stop_loss_cents == sl0)
        marker = " ← baseline" if is_baseline else ""
        delta_str = "—"
        per_trade_delta_str = "—"
        if baseline_test_pnl is not None and not is_baseline:
            delta = v.total_pnl_cents - baseline_test_pnl
            delta_str = f"{delta:+d}"
            # Per-trade Δ uses baseline_test_n as the divisor (per-trade is
            # measured against the same test cohort the baseline ran on).
            if baseline_test_n:
                per_trade_delta_str = f"{delta/baseline_test_n:+.1f}"
        elif is_baseline:
            delta_str = "0"
            per_trade_delta_str = "0.0"
        return (
            f"| {v.label}{marker} | {train_pnl:+d} | "
            f"{v.total_pnl_cents:+d} | {delta_str} | {v.n_replays} | "
            f"{per_trade_delta_str} |"
        )

    seen_labels: set[str] = set()
    for v in report.test_top3:
        lines.append(_row(v))
        seen_labels.add(v.label)
    if report.baseline_test is not None and report.baseline_test.label not in seen_labels:
        lines.append(_row(report.baseline_test))
    lines.append("")

    if report.best_test_variant is not None:
        best = report.best_test_variant
        lines.append(f"## Per-sport breakdown — best test variant ({best.label})")
        lines.append("")
        lines.append("| Sport | n_replays | Σ P&L¢ |")
        lines.append("|---|---|---|")
        for b in report.best_per_sport:
            lines.append(f"| {b.sport} | {b.n_replays} | {b.total_pnl_cents:+d} |")
        lines.append("")

        lines.append(f"## Regime slicing — best test variant ({best.label})")
        lines.append("")
        for regime_key, buckets in report.best_per_regime.items():
            lines.append(f"### By {regime_key}")
            lines.append("")
            lines.append(f"| {regime_key} | n_replays | Σ P&L¢ |")
            lines.append("|---|---|---|")
            for b in buckets:
                lines.append(f"| {b.key} | {b.n_replays} | {b.total_pnl_cents:+d} |")
            lines.append("")

    lines.append("## Decision gate (Session 41 spec)")
    lines.append("")
    lines.append(
        "Pattern A (ship): one variant has test per-trade Δ ≥ +50¢ vs "
        "baseline AND train/test sign agreement (both positive).  "
        "Pattern B (ship best + watch-list): multiple positive variants but no "
        "clear best.  "
        "Pattern C (no ship): no variant beats baseline at the +50¢ test-Δ "
        "gate. Mirror Session 18.5 / 38a-2 / 40 outcomes."
    )
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    lines.append("_(authored by operator after running)_")
    return "\n".join(lines)


def _run_sweep_cli(min_entry_date: str, slippage_cents: int) -> int:
    """--sweep dispatch: load eligible trades, split, sweep, render. Returns
    process exit code (0 on success, non-zero if sample is too small)."""
    all_paper = load_paper_trades(strategy_name="live_momentum")
    eligible = [
        t for t in all_paper
        if not _is_disabled_sport(t["ticker"])
        and t.get("timestamp", "") >= min_entry_date
    ]
    if len(eligible) < 4:
        print(
            f"# ERROR: only {len(eligible)} eligible trade(s) after filters "
            f"(min_entry_date={min_entry_date}, MOMENTUM_DISABLED_SPORTS gate). "
            "Need at least 4 to run a 70/30 split sweep.",
            file=sys.stderr,
        )
        return 2
    train, test = split_train_test(eligible, train_pct=0.70)
    if not train or not test:
        print(
            f"# ERROR: split produced empty side (train={len(train)}, "
            f"test={len(test)}). Need a larger sample.",
            file=sys.stderr,
        )
        return 2

    print(
        f"# Sweep: grid={len(SWEEP_GRID_PRIMARY)} variants × "
        f"train={len(train)} → top-3 → test={len(test)}, "
        f"slippage={slippage_cents}¢",
        file=sys.stderr,
    )
    report = run_sweep(
        SWEEP_GRID_PRIMARY, train, test,
        slippage_cents=slippage_cents,
    )
    print(render_sweep_report(report))
    return 0


def _run_sweep_tp_sl_cli(min_entry_date: str, slippage_cents: int) -> int:
    """--sweep-tp-sl dispatch (Session 41): same load/filter/split discipline
    as --sweep, but iterates `SWEEP_GRID_TP_SL` while holding (LM, TS) fixed
    at current production. Returns process exit code (0 on success, non-zero
    if sample is too small)."""
    all_paper = load_paper_trades(strategy_name="live_momentum")
    eligible = [
        t for t in all_paper
        if not _is_disabled_sport(t["ticker"])
        and t.get("timestamp", "") >= min_entry_date
    ]
    if len(eligible) < 4:
        print(
            f"# ERROR: only {len(eligible)} eligible trade(s) after filters "
            f"(min_entry_date={min_entry_date}, MOMENTUM_DISABLED_SPORTS gate). "
            "Need at least 4 to run a 70/30 split sweep.",
            file=sys.stderr,
        )
        return 2
    train, test = split_train_test(eligible, train_pct=0.70)
    if not train or not test:
        print(
            f"# ERROR: split produced empty side (train={len(train)}, "
            f"test={len(test)}). Need a larger sample.",
            file=sys.stderr,
        )
        return 2

    print(
        f"# Sweep (TP/SL): grid={len(SWEEP_GRID_TP_SL)} variants × "
        f"train={len(train)} → top-3 → test={len(test)}, "
        f"slippage={slippage_cents}¢, "
        f"fixed (LM={SWEEP_TP_SL_FIXED_LM:.2f}, TS={SWEEP_TP_SL_FIXED_TS}¢)",
        file=sys.stderr,
    )
    report = run_sweep_tp_sl(
        SWEEP_GRID_TP_SL, train, test,
        slippage_cents=slippage_cents,
    )
    print(render_sweep_tp_sl_report(report))
    return 0


# --- Session 42: per-sport TP/SL sweep --------------------------------------


def filter_trades_to_sport(trades: list[dict], target_sport: str) -> list[dict]:
    """Filter paper trades to only those whose ticker resolves to `target_sport`.

    Uses the existing `_TICKER_PREFIX_TO_SPORT` map (single-source ticker→sport
    resolution shared with `_is_disabled_sport`). Applies AFTER the existing
    `MOMENTUM_DISABLED_SPORTS` and `--min-entry-date` filters per Session 19a-followup
    discipline (preserve single-sourced filtering chain).
    """
    target = target_sport.lower()
    out: list[dict] = []
    for t in trades:
        prefix = t.get("ticker", "").split("-")[0]
        sport = _TICKER_PREFIX_TO_SPORT.get(prefix, "")
        if sport == target:
            out.append(t)
    return out


def run_sweep_tp_sl_per_sport(
    target_sport: str,
    train: list[dict],
    test: list[dict],
    *,
    slippage_cents: int = 2,
    top_k: int = 3,
    leader_min: float = SWEEP_TP_SL_FIXED_LM,
    trail_stop_cents: int = SWEEP_TP_SL_FIXED_TS,
    grid: Optional[list[tuple[int, int]]] = None,
    baseline: Optional[tuple[int, int]] = None,
) -> TpSlSweepReport:
    """Session 42: per-sport TP/SL grid sweep.

    Threads `sport_overrides={target_sport: {"take_profit": tp, "stop_loss": sl}}`
    per variant — every variant only changes the target sport's resolved TP/SL.
    Other sports' SPORT_PROFILES entries pass through untouched. `train` and
    `test` should already be filtered to the target sport (caller's job — usually
    via `filter_trades_to_sport`).

    Caller can override `grid` and `baseline` for tests; defaults pull from
    `SWEEP_GRID_TP_SL_PER_SPORT[target_sport]` and
    `SWEEP_BASELINE_TP_SL_PER_SPORT[target_sport]`.
    """
    if grid is None:
        if target_sport not in SWEEP_GRID_TP_SL_PER_SPORT:
            raise ValueError(
                f"No SWEEP_GRID_TP_SL_PER_SPORT entry for {target_sport!r}; "
                f"known sports: {sorted(SWEEP_GRID_TP_SL_PER_SPORT.keys())}"
            )
        grid = SWEEP_GRID_TP_SL_PER_SPORT[target_sport]
    if baseline is None:
        if target_sport not in SWEEP_BASELINE_TP_SL_PER_SPORT:
            raise ValueError(
                f"No SWEEP_BASELINE_TP_SL_PER_SPORT entry for {target_sport!r}; "
                f"known sports: {sorted(SWEEP_BASELINE_TP_SL_PER_SPORT.keys())}"
            )
        baseline = SWEEP_BASELINE_TP_SL_PER_SPORT[target_sport]

    def _overrides(tp: int, sl: int) -> dict[str, dict]:
        return {target_sport: {"take_profit": tp, "stop_loss": sl}}

    training_results: list[VariantResult] = [
        _run_variant(
            leader_min, trail_stop_cents, train,
            slippage_cents=slippage_cents,
            take_profit_cents=tp,
            stop_loss_cents=sl,
            sport_overrides=_overrides(tp, sl),
        )
        for (tp, sl) in grid
    ]
    training_results.sort(key=lambda v: v.total_pnl_cents, reverse=True)

    top_variants = training_results[:top_k]
    test_results: list[VariantResult] = [
        _run_variant(
            leader_min, trail_stop_cents, test,
            slippage_cents=slippage_cents,
            take_profit_cents=v.take_profit_cents,
            stop_loss_cents=v.stop_loss_cents,
            sport_overrides=_overrides(v.take_profit_cents, v.stop_loss_cents),
        )
        for v in top_variants
    ]

    tp0, sl0 = baseline
    baseline_test: Optional[VariantResult] = None
    if test:
        baseline_test = _run_variant(
            leader_min, trail_stop_cents, test,
            slippage_cents=slippage_cents,
            take_profit_cents=tp0,
            stop_loss_cents=sl0,
            sport_overrides=_overrides(tp0, sl0),
        )

    best_test = max(test_results, key=lambda v: v.total_pnl_cents) if test_results else None
    if best_test is not None:
        best_per_sport = _aggregate_per_sport(best_test.per_replay)
        best_per_regime = {
            "time_of_day": _aggregate_per_regime(best_test.per_replay, test, "time_of_day"),
            "day_of_week": _aggregate_per_regime(best_test.per_replay, test, "day_of_week"),
            "sport_phase": _aggregate_per_regime(best_test.per_replay, test, "sport_phase"),
        }
    else:
        best_per_sport = []
        best_per_regime = {}

    def _ts_range(rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
        if not rows:
            return None, None
        ts = [r.get("timestamp", "") for r in rows if r.get("timestamp")]
        if not ts:
            return None, None
        return min(ts), max(ts)

    train_first, train_last = _ts_range(train)
    test_first, test_last = _ts_range(test)

    return TpSlSweepReport(
        train_n=len(train),
        test_n=len(test),
        train_first_ts=train_first,
        train_last_ts=train_last,
        test_first_ts=test_first,
        test_last_ts=test_last,
        slippage_cents=slippage_cents,
        leader_min_fixed=leader_min,
        trail_stop_cents_fixed=trail_stop_cents,
        baseline=baseline,
        training=training_results,
        test_top3=test_results,
        baseline_test=baseline_test,
        best_test_variant=best_test,
        best_per_sport=best_per_sport,
        best_per_regime=best_per_regime,
    )


def render_sweep_tp_sl_per_sport_report(
    report: TpSlSweepReport, target_sport: str,
) -> str:
    """Session 42 markdown wrapper: same body as `render_sweep_tp_sl_report`,
    but the title and header carry the target sport. Note in header that
    OTHER sports' SPORT_PROFILES are untouched."""
    body = render_sweep_tp_sl_report(report)
    # Replace the Session 41 title with a Session 42 per-sport title; insert
    # a one-line per-sport header right after.
    body = body.replace(
        "# Tick-Replay Sweep — Session 41 (TP/SL ratio)",
        f"# Tick-Replay Sweep — Session 42 ({target_sport.upper()} per-sport TP/SL)",
        1,
    )
    body = body.replace(
        "**Sport-profile caveat.** Tennis-alias sports have explicit "
        "`take_profit=15`/`stop_loss=8` in SPORT_PROFILES that override the "
        "global default. This sweep affects only sports without that override "
        "(NBA / NHL / UFC / IPL / etc.).",
        f"**Per-sport scope.** Sweep varies ONLY `{target_sport.upper()}`'s "
        f"resolved TP/SL via `sport_overrides`; other sports' SPORT_PROFILES "
        f"entries are untouched. Trades pre-filtered to "
        f"`KX{target_sport.upper()}*` ticker prefixes only.",
        1,
    )
    return body


def _run_sweep_tp_sl_per_sport_cli(
    target_sport: str, min_entry_date: str, slippage_cents: int,
) -> int:
    """--sweep-tp-sl-per-sport <sport> dispatch (Session 42). Same load/filter/
    split discipline as --sweep-tp-sl, plus filter_trades_to_sport before the
    train/test split. Returns process exit code (0 success, non-zero if sample
    too small or sport unknown)."""
    if target_sport not in SWEEP_GRID_TP_SL_PER_SPORT:
        print(
            f"# ERROR: no per-sport grid for {target_sport!r}. "
            f"Known sports: {sorted(SWEEP_GRID_TP_SL_PER_SPORT.keys())}",
            file=sys.stderr,
        )
        return 2

    all_paper = load_paper_trades(strategy_name="live_momentum")
    eligible = [
        t for t in all_paper
        if not _is_disabled_sport(t["ticker"])
        and t.get("timestamp", "") >= min_entry_date
    ]
    eligible = filter_trades_to_sport(eligible, target_sport)
    if len(eligible) < 4:
        print(
            f"# ERROR: only {len(eligible)} eligible {target_sport.upper()} trade(s) "
            f"after filters (min_entry_date={min_entry_date}, "
            f"MOMENTUM_DISABLED_SPORTS gate, sport={target_sport}). "
            "Need at least 4 to run a 70/30 split sweep.",
            file=sys.stderr,
        )
        return 2
    train, test = split_train_test(eligible, train_pct=0.70)
    if not train or not test:
        print(
            f"# ERROR: split produced empty side (train={len(train)}, "
            f"test={len(test)}). Need a larger sample.",
            file=sys.stderr,
        )
        return 2

    grid = SWEEP_GRID_TP_SL_PER_SPORT[target_sport]
    print(
        f"# Sweep (TP/SL per-sport={target_sport.upper()}): "
        f"grid={len(grid)} variants × train={len(train)} → top-3 → test={len(test)}, "
        f"slippage={slippage_cents}¢, "
        f"fixed (LM={SWEEP_TP_SL_FIXED_LM:.2f}, TS={SWEEP_TP_SL_FIXED_TS}¢)",
        file=sys.stderr,
    )
    report = run_sweep_tp_sl_per_sport(
        target_sport, train, test,
        slippage_cents=slippage_cents,
    )
    print(render_sweep_tp_sl_per_sport_report(report, target_sport))
    return 0


def _run_debug_ticker(strategy, all_paper: list[dict], ticker: str,
                      slippage_cents: int) -> int:
    """--debug-ticker dispatch: per-tick action trace + side-by-side journal
    events for one ticker. Returns process exit code."""
    matches = [t for t in all_paper if t.get("ticker") == ticker]
    if not matches:
        print(f"# No live_momentum paper trades found for ticker {ticker}", file=sys.stderr)
        return 1
    first_trade = min(matches, key=lambda t: t.get("timestamp", ""))
    print(f"# --debug-ticker {ticker}")
    print(f"# Paper trades for ticker: {len(matches)} "
          f"(entry={first_trade.get('timestamp')}, "
          f"resolved={first_trade.get('resolved_at')})")
    print("")
    print("## Port action trace (parity-window replay)")
    print("")
    result = _replay_paper_trade(
        strategy, first_trade,
        ticker_trades=matches,
        slippage_cents=slippage_cents,
        fix_peak_tracking_bug=False,
        parity_window=True,
        trace=True,
    )
    print("")
    if result is None:
        print("# Replay returned None — no settlement window or no tick coverage.")
    else:
        print(f"# Round-trips: {len(result.round_trips)}, "
              f"realized P&L: {result.realized_pnl_cents:+d}¢, "
              f"last_tick_ts: {result.last_tick_ts}")
    print("")
    print(f"## live_journal.json events for {ticker}")
    print("")
    events = _load_journal_events_for_ticker(ticker)
    if not events:
        print("# (no matching bet/exit events in live_journal.json)")
    else:
        for ev in events:
            ts = ev.get("_ts", "")
            kind = ev.get("event", "?")
            reason = ev.get("reason", "")
            entry_price = ev.get("entry_price", "")
            exit_value = ev.get("exit_value", "")
            pnl = ev.get("pnl", "")
            price_cents = ev.get("price_cents", "")
            side = ev.get("side", "")
            contracts = ev.get("contracts", "")
            print(
                f"[{ts}] event={kind} side={side} contracts={contracts} "
                f"price_cents={price_cents} entry={entry_price} "
                f"exit_value={exit_value} pnl={pnl} reason={reason}"
            )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paper-trades", type=int, default=10,
        help="Number of paper trades to use for parity check (default 10)",
    )
    parser.add_argument(
        "--slippage-cents", type=int, default=2,
        help="Pessimism per round-trip (default 2)",
    )
    parser.add_argument(
        "--tolerance-cents", type=int, default=1,
        help="Parity tolerance (default 1)",
    )
    parser.add_argument(
        "--fix-peak-tracking-bug", action="store_true",
        help="Bonus: also run with peak-tracking bug simulated as fixed",
    )
    parser.add_argument(
        "--min-entry-date", type=str, default=DEFAULT_MIN_ENTRY_DATE,
        help=(
            "Filter paper trades whose entry timestamp >= this ISO date "
            f"(default {DEFAULT_MIN_ENTRY_DATE}). Pre-Apr-18 archives lack the "
            "`bid` field; the port's current_value falls back to yes_ask in "
            "those rows and drifts from production. Restrict to the "
            "schema-complete sample for honest parity."
        ),
    )
    parser.add_argument(
        "--debug-ticker", type=str, default=None,
        help=(
            "Diagnostic mode: dump per-tick action trace + live_journal.json "
            "events for the given ticker. Skips the standard parity loop."
        ),
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help=(
            "Sub-session 19c parameter sweep: 3×5 grid over MOMENTUM_LEADER_MIN "
            "× MOMENTUM_DQS_TRAIL_STOP, train/test split (70/30 by entry date), "
            "top-3 training variants validated on test set, per-sport + regime "
            "slicing for best test variant. Skips the standard parity loop."
        ),
    )
    parser.add_argument(
        "--sweep-tp-sl", action="store_true",
        help=(
            "Session 41 TP/SL sweep: 12-variant grid over LIVE_TAKE_PROFIT_CENTS "
            "× LIVE_STOP_LOSS_CENTS, holding (LM=0.65, TS=6) fixed at current "
            "production. Same train/test split + top-3 + per-sport + regime "
            "discipline as --sweep. Mutually exclusive with --sweep."
        ),
    )
    parser.add_argument(
        "--sweep-tp-sl-per-sport", type=str, default=None,
        choices=sorted(SWEEP_GRID_TP_SL_PER_SPORT.keys()),
        help=(
            "Session 42 per-sport TP/SL sweep: 12-variant grid keyed off the "
            "target sport's current SPORT_PROFILES TP/SL. Threads "
            "sport_overrides per variant — varies ONLY the target sport's "
            "resolved TP/SL while leaving other SPORT_PROFILES entries "
            "untouched. Mutually exclusive with --sweep / --sweep-tp-sl."
        ),
    )
    args = parser.parse_args()

    sweep_modes = [args.sweep, args.sweep_tp_sl, args.sweep_tp_sl_per_sport is not None]
    if sum(1 for m in sweep_modes if m) > 1:
        print(
            "# ERROR: --sweep, --sweep-tp-sl, and --sweep-tp-sl-per-sport "
            "are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.sweep:
        sys.exit(_run_sweep_cli(args.min_entry_date, args.slippage_cents))

    if args.sweep_tp_sl:
        sys.exit(_run_sweep_tp_sl_cli(args.min_entry_date, args.slippage_cents))

    if args.sweep_tp_sl_per_sport:
        sys.exit(_run_sweep_tp_sl_per_sport_cli(
            args.sweep_tp_sl_per_sport,
            args.min_entry_date,
            args.slippage_cents,
        ))

    strategy = LiveMomentumStrategy()
    all_paper = load_paper_trades(strategy_name="live_momentum")

    if args.debug_ticker:
        sys.exit(_run_debug_ticker(
            strategy, all_paper, args.debug_ticker, args.slippage_cents,
        ))

    # Filter out trades from MOMENTUM_DISABLED_SPORTS — current strategy gates
    # entry for those sports, so replay would always show 0 round-trips and
    # parity is meaningless.
    excluded = [t for t in all_paper if _is_disabled_sport(t["ticker"])]
    eligible = [t for t in all_paper if not _is_disabled_sport(t["ticker"])]
    # Sample-restriction: drop pre-cutoff entries (schema drift on `bid` field).
    # ISO-8601 prefix comparison is safe — both sides are ISO timestamps.
    cutoff = args.min_entry_date
    pre_cutoff = [t for t in eligible if t.get("timestamp", "") < cutoff]
    eligible = [t for t in eligible if t.get("timestamp", "") >= cutoff]
    if pre_cutoff:
        print(
            f"# Excluded {len(pre_cutoff)} trade(s) entered before {cutoff} — "
            "live_ticks.jsonl bid/opp_bid fields missing pre-2026-04-18 "
            "(commit c0c5049). Use --min-entry-date to override.",
            file=sys.stderr,
        )
    paper_trades = _select_diverse_paper_trades(eligible, args.paper_trades)
    sports = sorted({t["ticker"].split("-")[0] for t in paper_trades})
    if excluded:
        ex_sports = sorted({t["ticker"].split("-")[0] for t in excluded})
        print(
            f"# Excluded {len(excluded)} trade(s) from disabled sports "
            f"({', '.join(ex_sports)}) — current strategy gates entry "
            f"via MOMENTUM_DISABLED_SPORTS={MOMENTUM_DISABLED_SPORTS}",
            file=sys.stderr,
        )
    if len(sports) < 3:
        print(
            f"# WARNING: only {len(sports)} sport(s) in sample: {sports}",
            file=sys.stderr,
        )

    # Per-ticker replay results used by the parity-table renderer. Built with
    # slippage_cents=0 and parity_window=True so the displayed Replay P&L
    # matches what parity_check computed internally — adding the +Nc
    # forward-projection pessimism here would just create a constant offset
    # that's not measuring port faithfulness.
    by_ticker: dict[str, list[dict]] = {}
    for t in paper_trades:
        by_ticker.setdefault(t["ticker"], []).append(t)
    bugged_results: dict[str, ReplayResult] = {}
    for ticker, trades in by_ticker.items():
        first_trade = min(trades, key=lambda t: t.get("timestamp", ""))
        sorted_t = sorted(trades, key=lambda t: t.get("timestamp", ""))
        qty_override = [int(t.get("contracts") or 0) for t in sorted_t if t.get("contracts")]
        r = _replay_paper_trade(
            strategy, first_trade,
            ticker_trades=trades,
            slippage_cents=0,
            fix_peak_tracking_bug=False,
            parity_window=True,
            qty_override=qty_override or None,
        )
        if r is not None:
            bugged_results[ticker] = r

    parity = parity_check(
        paper_trades, strategy,
        tolerance_cents=args.tolerance_cents,
        slippage_cents=0,
    )

    print("# Tick-Replay Back-Test")
    print("")
    print("Strategy: live_momentum (production parameters)")
    print(
        f"Slippage assumption: +0¢ per round-trip for parity check; "
        f"+{args.slippage_cents}¢ per round-trip for forward back-test (bonus mode)."
    )
    print(f"Sports in sample: {', '.join(sports)}")
    print("")
    print(render_parity_report(parity, paper_trades, bugged_results, args.tolerance_cents))
    print("")

    if args.fix_peak_tracking_bug:
        bugged = back_test(
            strategy, paper_trades=paper_trades,
            slippage_cents=args.slippage_cents,
            fix_peak_tracking_bug=False,
        )
        fixed = back_test(
            strategy, paper_trades=paper_trades,
            slippage_cents=args.slippage_cents,
            fix_peak_tracking_bug=True,
        )
        print(render_bonus_section(bugged, fixed, paper_trades))
        print("")

    print("## Findings")
    print("")
    print("_(authored by operator after running)_")


if __name__ == "__main__":
    main()
