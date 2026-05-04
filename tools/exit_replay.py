#!/usr/bin/env python3
"""Session 18.5 exit-logic replay tool.

Mirrors `bot/live_watcher._check_exit` (lines 2178-2361) byte-for-byte against
the historical bet→exit pairs in `bot/state/live_journal.json`, replaying the
exit decision against the per-tick stream in `bot/state/live_ticks.jsonl` (+
gzipped archives in `bot/state/archive/`).

Sweep axis: `MOMENTUM_DQS_TRAIL_STOP` ∈ [3, 4, 5, 6, 7, 8]¢. The swept value
is applied as an OVERRIDE to ALL sports — without this, NBA would stay at 4¢
and NHL at 8¢ via SPORT_PROFILES regardless of the global default. Per-sport
breakdown decomposes the impact.

Tick window per pair: [bet.ts, exit.ts] inclusive. Variants can fire EARLIER
than the production exit (e.g., tighter trail width → earlier trailing-stop)
but never later. Widening the trail makes trailing less likely; the sim falls
through to TP/SL/NO_EXIT — all within the window.

NO_EXIT fall-through: if no rule fires across the entire stream, treat as
"open at last tick" → realized P&L = last_current_value − entry_price.

Out of scope: entry-decision replay (Session 19), Strategy Protocol extension
(Session 19), arb-mode exits (EDGE_REVERSAL/FADING), UNDERWATER_EXIT (dead
code, removed Apr 16).

Usage:
    python3 tools/exit_replay.py
    python3 tools/exit_replay.py --cohort strict
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "bot" / "state"
ARCHIVE_DIR = STATE_DIR / "archive"
JOURNAL_FILE = STATE_DIR / "live_journal.json"
TICKS_FILE = STATE_DIR / "live_ticks.jsonl"

sys.path.insert(0, str(ROOT))
from bot.config import (  # noqa: E402
    LIVE_TAKE_PROFIT_CENTS,
    LIVE_STOP_LOSS_CENTS,
    LIVE_NEAR_SETTLE_CENTS,
    MOMENTUM_DQS_TRAIL_STOP,
    MOMENTUM_MAX_LOSS_DOLLARS,
    SPORT_PROFILES,
)
from bot.regime import _ticker_to_sport  # noqa: E402


SWEEP_TRAIL_STOPS = [3, 4, 5, 6, 7, 8]
DEFAULT_TRAIL_STOP = MOMENTUM_DQS_TRAIL_STOP  # 6 — current production global

STRICT_MIN_TICKS = 10
RELAXED_MIN_TICKS = 5


# ─── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BetExitPair:
    bet: dict
    exit: dict
    ticks: list[dict] = field(default_factory=list)


@dataclass
class SimulatedExit:
    exit_ts: str | None
    exit_reason: str  # TAKE_PROFIT | TRAILING_STOP | STOP_LOSS | DOLLAR_STOP | NEAR_SETTLE | SCORE_FLIP | OPP_RUN_EXIT | NO_EXIT
    exit_price_cents: int
    realized_pnl_cents: int
    realized_pnl_dollars: float
    peak_value_cents: int
    ticks_to_exit: int


@dataclass
class SweepResult:
    name: str
    trail_stop: int
    n_pairs: int
    total_pnl_cents: int
    win_count: int
    loss_count: int
    median_pnl_cents: float
    reason_breakdown: Counter
    per_sport_pnl_cents: dict[str, int]


@dataclass
class ExclusionSummary:
    total_bets: int
    total_exits: int
    paired: int
    open_bets: int
    settled_excluded: int
    no_ticks: int
    thin_coverage: int  # paired but <STRICT_MIN_TICKS in window
    strict_n: int
    relaxed_n: int


# ─── Loaders ──────────────────────────────────────────────────────────────────


def _parse_ts(s):
    """Parse ISO-8601 timestamp string → datetime, returning None on failure."""
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _iter_jsonl_lines(path: Path, opener):
    with opener(path, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _is_settled_reason(reason: str | None) -> bool:
    """Exit reasons starting with 'SETTLED at' are natural settlements (not
    exit-decision driven). Excluded from replay because the simulator can't
    affect settlement."""
    if not reason:
        return False
    return reason.startswith("SETTLED at")


def load_bet_exit_pairs(journal_path: Path | None = None) -> tuple[list[tuple[dict, dict]], dict]:
    """Pair bets to exits greedily by ticker + chronological order.

    Returns (pairs, exclusion_counts) where exclusion_counts has keys:
    total_bets, total_exits, paired, open_bets, settled_excluded.

    A pair is the (bet, exit) tuple where:
    - both events are on the same ticker
    - exit.timestamp >= bet.timestamp
    - exit reason does NOT start with "SETTLED at" (those are excluded)
    - the exit hasn't already been claimed by an earlier bet
    """
    if journal_path is None:
        journal_path = JOURNAL_FILE  # resolved at call time so monkeypatch works

    counts = {
        "total_bets": 0,
        "total_exits": 0,
        "paired": 0,
        "open_bets": 0,
        "settled_excluded": 0,
    }

    if not journal_path.exists():
        return [], counts

    try:
        with open(journal_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return [], counts

    if not isinstance(data, list):
        return [], counts

    bets = [e for e in data if isinstance(e, dict) and e.get("event") == "bet"]
    exits = [e for e in data if isinstance(e, dict) and e.get("event") == "exit"]
    counts["total_bets"] = len(bets)
    counts["total_exits"] = len(exits)

    # Filter SETTLED exits up front (count them, then exclude from pairing)
    real_exits = []
    for e in exits:
        if _is_settled_reason(e.get("reason")):
            counts["settled_excluded"] += 1
        else:
            real_exits.append(e)

    # Bucket by ticker; sort each bucket by timestamp.
    bets_by_ticker: dict[str, list[dict]] = defaultdict(list)
    exits_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for b in bets:
        if b.get("ticker"):
            bets_by_ticker[b["ticker"]].append(b)
    for e in real_exits:
        if e.get("ticker"):
            exits_by_ticker[e["ticker"]].append(e)
    for tk in bets_by_ticker:
        bets_by_ticker[tk].sort(key=lambda x: x.get("timestamp", ""))
    for tk in exits_by_ticker:
        exits_by_ticker[tk].sort(key=lambda x: x.get("timestamp", ""))

    pairs: list[tuple[dict, dict]] = []
    for tk, tk_bets in bets_by_ticker.items():
        tk_exits = exits_by_ticker.get(tk, [])
        ei = 0
        for b in tk_bets:
            b_ts = b.get("timestamp", "")
            # Advance exit pointer to first exit at or after this bet's timestamp
            while ei < len(tk_exits) and tk_exits[ei].get("timestamp", "") < b_ts:
                ei += 1
            if ei < len(tk_exits):
                pairs.append((b, tk_exits[ei]))
                ei += 1
            else:
                counts["open_bets"] += 1

    counts["paired"] = len(pairs)
    return pairs, counts


# ─── Tick stream loading & slicing ────────────────────────────────────────────


def load_tick_index(
    ticks_file: Path | None = None,
    archive_dir: Path | None = None,
) -> dict[str, list[dict]]:
    """Read live_ticks.jsonl + all gzipped archives. Return {ticker: [tick, ...]}
    sorted by ts (string lex order works for ISO-8601 with consistent timezone).
    """
    if ticks_file is None:
        ticks_file = TICKS_FILE
    if archive_dir is None:
        archive_dir = ARCHIVE_DIR

    sources: list[tuple[Path, callable]] = []
    if ticks_file.exists():
        sources.append((ticks_file, open))
    if archive_dir.exists():
        for path in sorted(archive_dir.glob("live_ticks-*.jsonl.gz")):
            sources.append((path, gzip.open))

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for path, opener in sources:
        try:
            for rec in _iter_jsonl_lines(path, opener):
                tk = rec.get("ticker")
                ts = rec.get("ts")
                if tk and ts:
                    by_ticker[tk].append(rec)
        except (OSError, EOFError):
            print(f"# warning: could not read {path}", file=sys.stderr)
            continue

    for tk in by_ticker:
        by_ticker[tk].sort(key=lambda t: t.get("ts", ""))
    return dict(by_ticker)


def slice_ticks(tick_list: list[dict], t_start: str, t_end: str) -> list[dict]:
    """Return ticks in [t_start, t_end] inclusive. Assumes tick_list is sorted by ts."""
    if not tick_list or not t_start or not t_end:
        return []
    keys = [t.get("ts", "") for t in tick_list]
    lo = bisect.bisect_left(keys, t_start)
    hi = bisect.bisect_right(keys, t_end)
    return tick_list[lo:hi]


def attach_ticks(
    pairs: list[tuple[dict, dict]],
    tick_index: dict[str, list[dict]],
) -> tuple[list[BetExitPair], dict]:
    """Build BetExitPair objects with attached tick streams. Returns
    (pairs_with_ticks, coverage_counts) where coverage_counts has:
    - no_ticks: pairs where the ticker isn't in tick_index OR window is empty
    - thin_coverage: paired with <STRICT_MIN_TICKS in window
    - relaxed_only: paired with [RELAXED_MIN_TICKS, STRICT_MIN_TICKS) ticks
    - strict: paired with >= STRICT_MIN_TICKS
    """
    coverage = {"no_ticks": 0, "thin_coverage": 0, "relaxed_only": 0, "strict": 0}
    out: list[BetExitPair] = []
    for bet, ex in pairs:
        tk = bet.get("ticker", "")
        bts = bet.get("timestamp", "")
        ets = ex.get("timestamp", "")
        ticks = slice_ticks(tick_index.get(tk, []), bts, ets)
        n = len(ticks)
        if n == 0:
            coverage["no_ticks"] += 1
        elif n < RELAXED_MIN_TICKS:
            coverage["thin_coverage"] += 1
        elif n < STRICT_MIN_TICKS:
            coverage["relaxed_only"] += 1
        else:
            coverage["strict"] += 1
        out.append(BetExitPair(bet=bet, exit=ex, ticks=ticks))
    return out, coverage


# ─── Exit simulator ───────────────────────────────────────────────────────────


# Default param dict for simulate_exit. The sweep overrides `trail_stop`.
DEFAULT_PARAMS: dict = {
    "trail_stop": DEFAULT_TRAIL_STOP,        # MOMENTUM_DQS_TRAIL_STOP = 6
    "take_profit_cents": LIVE_TAKE_PROFIT_CENTS,  # 12
    "stop_loss_cents": LIVE_STOP_LOSS_CENTS,      # 30
    "near_settle_cents": LIVE_NEAR_SETTLE_CENTS,  # 93
    "max_loss_dollars": MOMENTUM_MAX_LOSS_DOLLARS,  # $5
    # `apply_trail_globally` defaults True: swept value overrides per-sport
    # `trail_stop` profile keys. Set False to mirror production (NBA=4, NHL=8).
    "apply_trail_globally": True,
    # `apply_tp_sl_globally` defaults False: production lookup uses sport
    # profile (e.g., tennis tp=15 stays 15). Set True to apply globally.
    "apply_tp_sl_globally": False,
}


def _resolve_sport_thresholds(sport: str | None, params: dict) -> tuple[int, int, int]:
    """Resolve (effective_tp, effective_sl, effective_trail) for this bet's sport.

    Mirrors production lookup: SPORT_PROFILES[sport].get(key, global_default).
    Honors params["apply_trail_globally"] / ["apply_tp_sl_globally"] overrides.
    """
    profile = SPORT_PROFILES.get(sport or "", {})
    if params["apply_tp_sl_globally"]:
        tp = params["take_profit_cents"]
        sl = params["stop_loss_cents"]
    else:
        tp = profile.get("take_profit", params["take_profit_cents"])
        sl = profile.get("stop_loss", params["stop_loss_cents"])
    if params["apply_trail_globally"]:
        trail = params["trail_stop"]
    else:
        trail = profile.get("trail_stop", params["trail_stop"])
    return tp, sl, trail


def _current_value_for_tick(tick: dict, held_side: str) -> int | None:
    """Return current_value in cents for a YES or NO position at this tick.

    Mirrors live_watcher.py:2215-2222:
      held_side == "yes" → current_value = yes_bid (tick.bid)
      held_side == "no"  → current_value = 100 - yes_ask (100 - tick.price)
    Returns None if the relevant price field is missing.
    """
    if held_side == "yes":
        bid = tick.get("bid")
        if bid is None:
            # Production fallback: yes_ask if bid missing (bot/live_watcher.py:2218)
            ask = tick.get("price")
            return ask if ask is not None else None
        return bid
    else:
        ask = tick.get("price")
        if ask is None:
            return None
        return 100 - ask


def simulate_exit(pair: BetExitPair, params: dict | None = None) -> SimulatedExit:
    """Replay live_watcher._check_exit logic against a tick stream with overridable params.

    Mirrors bot/live_watcher.py:2178-2361 priority order:
      1.  TAKE_PROFIT     gain >= sport_tp
      2.  NEAR_SETTLE     yes side AND current >= near_settle_cents
      2b. TRAILING_STOP   momentum AND drop_from_peak >= sport_trail AND gain > 0
                          (NO LIVE_PROFIT_TARGET activation gate — verified dead)
      2c. SCORE_FLIP      momentum AND tick has score_diff/momentum/lead_trend
                          AND effective_score_diff < 0 AND momentum < 0 AND lead_trend < 0
                          (OPP_RUN_EXIT depends on opponent_on_run which is computed
                          in bot.game_context, not in raw tick → SKIPPED in replay)
      4.  STOP_LOSS       drop >= sport_sl
      4b. DOLLAR_STOP     unrealized_loss_$ >= max_loss_dollars

    NO_EXIT fall-through: if no rule fires, exit at last tick's current_value.
    """
    if params is None:
        params = DEFAULT_PARAMS

    bet = pair.bet
    held_side = bet.get("side", "yes")
    entry_price = int(bet.get("price_cents", 0))
    contracts = int(bet.get("contracts", 1))
    sport = bet.get("sport") or _ticker_to_sport(bet.get("ticker", ""))
    # All journal bets are momentum-mode watchers (live_momentum strategy).
    # If we ever ingest arb-mode bets, the trailing/score-flip blocks would skip.
    is_momentum_mode = True

    sport_tp, sport_sl, sport_trail = _resolve_sport_thresholds(sport, params)

    if entry_price <= 0 or not pair.ticks:
        return SimulatedExit(
            exit_ts=None,
            exit_reason="NO_EXIT",
            exit_price_cents=entry_price,
            realized_pnl_cents=0,
            realized_pnl_dollars=0.0,
            peak_value_cents=entry_price,
            ticks_to_exit=0,
        )

    peak = entry_price
    last_value = entry_price
    last_ts = pair.ticks[-1].get("ts")

    for i, tick in enumerate(pair.ticks, start=1):
        cv = _current_value_for_tick(tick, held_side)
        if cv is None:
            continue
        last_value = cv
        if cv > peak:
            peak = cv
        gain_cents = cv - entry_price

        # 1. TAKE PROFIT
        if gain_cents >= sport_tp:
            return _make_exit("TAKE_PROFIT", tick, cv, entry_price, contracts, peak, i)

        # 2. NEAR-SETTLEMENT (YES only)
        if held_side == "yes" and cv >= params["near_settle_cents"]:
            return _make_exit("NEAR_SETTLE", tick, cv, entry_price, contracts, peak, i)

        # 2b. TRAILING STOP
        if is_momentum_mode:
            drop_from_peak = peak - cv
            if drop_from_peak >= sport_trail and gain_cents > 0:
                return _make_exit("TRAILING_STOP", tick, cv, entry_price, contracts, peak, i)

        # 2c. SCORE FLIP (best-effort — uses tick.leader to gate ticker-perspective)
        if is_momentum_mode:
            sd = tick.get("score_diff")
            mom = tick.get("momentum")
            lt = tick.get("lead_trend")
            if sd is not None and mom is not None and lt is not None:
                # If our ticker is the leader's ticker, use score_diff as-is.
                # If our ticker is the opponent's, negate. tick.leader tells us.
                effective_sd = sd if tick.get("leader", True) else -sd
                if effective_sd < 0 and mom < 0 and lt < 0:
                    return _make_exit("SCORE_FLIP", tick, cv, entry_price, contracts, peak, i)
            # OPP_RUN_EXIT requires opponent_on_run from game_context; not in raw tick. Skip.

        # 4. STOP LOSS
        drop_cents = entry_price - cv
        if drop_cents >= sport_sl:
            return _make_exit("STOP_LOSS", tick, cv, entry_price, contracts, peak, i)

        # 4b. DOLLAR STOP
        unrealized_loss = drop_cents / 100.0 * contracts
        if unrealized_loss >= params["max_loss_dollars"]:
            return _make_exit("DOLLAR_STOP", tick, cv, entry_price, contracts, peak, i)

    # No exit fired across the entire tick stream → "open at last tick".
    realized_cents = last_value - entry_price
    return SimulatedExit(
        exit_ts=last_ts,
        exit_reason="NO_EXIT",
        exit_price_cents=last_value,
        realized_pnl_cents=realized_cents,
        realized_pnl_dollars=realized_cents / 100.0 * contracts,
        peak_value_cents=peak,
        ticks_to_exit=len(pair.ticks),
    )


def _make_exit(
    reason: str,
    tick: dict,
    cv: int,
    entry_price: int,
    contracts: int,
    peak: int,
    ticks_to_exit: int,
) -> SimulatedExit:
    realized_cents = cv - entry_price
    return SimulatedExit(
        exit_ts=tick.get("ts"),
        exit_reason=reason,
        exit_price_cents=cv,
        realized_pnl_cents=realized_cents,
        realized_pnl_dollars=realized_cents / 100.0 * contracts,
        peak_value_cents=peak,
        ticks_to_exit=ticks_to_exit,
    )


# ─── Sweep + render ───────────────────────────────────────────────────────────


def sweep(
    pairs_with_ticks: list[BetExitPair],
    trail_stops: list[int] | None = None,
) -> list[SweepResult]:
    """Run simulate_exit across all pairs for each trail_stop variant. Returns
    one SweepResult per variant with aggregate stats + per-sport P&L breakdown.
    """
    if trail_stops is None:
        trail_stops = SWEEP_TRAIL_STOPS

    results: list[SweepResult] = []
    for ts in trail_stops:
        params = {**DEFAULT_PARAMS, "trail_stop": ts, "apply_trail_globally": True}
        per_pair_pnl: list[int] = []
        per_sport_pnl: dict[str, int] = defaultdict(int)
        reasons: Counter = Counter()
        wins = losses = 0
        total = 0
        for pair in pairs_with_ticks:
            if not pair.ticks:
                continue  # skip no-coverage pairs
            sim = simulate_exit(pair, params)
            per_pair_pnl.append(sim.realized_pnl_cents)
            total += sim.realized_pnl_cents
            reasons[sim.exit_reason] += 1
            if sim.realized_pnl_cents > 0:
                wins += 1
            elif sim.realized_pnl_cents < 0:
                losses += 1
            sport = pair.bet.get("sport") or _ticker_to_sport(pair.bet.get("ticker", "")) or "unknown"
            per_sport_pnl[sport] += sim.realized_pnl_cents

        median_pnl = float(statistics.median(per_pair_pnl)) if per_pair_pnl else 0.0
        is_current = ts == DEFAULT_TRAIL_STOP
        name = f"{ts}¢{' (current global)' if is_current else ''}"
        results.append(
            SweepResult(
                name=name,
                trail_stop=ts,
                n_pairs=len(per_pair_pnl),
                total_pnl_cents=total,
                win_count=wins,
                loss_count=losses,
                median_pnl_cents=median_pnl,
                reason_breakdown=reasons,
                per_sport_pnl_cents=dict(per_sport_pnl),
            )
        )
    return results


def _filter_cohort(pairs: list[BetExitPair], min_ticks: int) -> list[BetExitPair]:
    return [p for p in pairs if len(p.ticks) >= min_ticks]


def _pct(num: int, denom: int) -> str:
    return f"{100 * num / denom:.0f}%" if denom else "—"


def _fmt_signed(v: int | float) -> str:
    return f"{v:+.0f}" if isinstance(v, (int, float)) else str(v)


REASON_KEYS = ["TAKE_PROFIT", "TRAILING_STOP", "STOP_LOSS", "DOLLAR_STOP", "NEAR_SETTLE", "SCORE_FLIP", "NO_EXIT"]


def render_sweep_table(results: list[SweepResult], cohort_label: str, n: int) -> str:
    if not results:
        return f"## Sweep results — {cohort_label} (n={n})\n\nNo pairs in cohort.\n"
    headers = ["trail_stop", "n", "Σ P&L¢", "Win%", "Median¢"] + [
        f"{k}%" for k in REASON_KEYS
    ]
    lines = [
        f"## Sweep results — {cohort_label} (n={n})",
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for r in results:
        win_pct = _pct(r.win_count, r.n_pairs)
        row = [
            r.name,
            str(r.n_pairs),
            _fmt_signed(r.total_pnl_cents),
            win_pct,
            _fmt_signed(r.median_pnl_cents),
        ]
        for k in REASON_KEYS:
            row.append(_pct(r.reason_breakdown.get(k, 0), r.n_pairs))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


def render_per_sport(results: list[SweepResult]) -> str:
    """Per-sport P&L breakdown comparing best variant vs current."""
    if not results:
        return ""
    current = next((r for r in results if r.trail_stop == DEFAULT_TRAIL_STOP), None)
    best = max(results, key=lambda r: r.total_pnl_cents)
    if current is None or best.trail_stop == current.trail_stop:
        # No change to highlight
        return ""
    sports = sorted(set(current.per_sport_pnl_cents) | set(best.per_sport_pnl_cents))
    lines = [
        f"## Per-sport breakdown — best variant ({best.name}) vs current ({current.name})",
        "",
        "| Sport | Current Σ¢ | Best Σ¢ | Δ¢ |",
        "|---|---|---|---|",
    ]
    for sport in sports:
        cur = current.per_sport_pnl_cents.get(sport, 0)
        bst = best.per_sport_pnl_cents.get(sport, 0)
        delta = bst - cur
        lines.append(f"| {sport} | {_fmt_signed(cur)} | {_fmt_signed(bst)} | {_fmt_signed(delta)} |")
    lines.append("")
    return "\n".join(lines)


# Authored after Task 5's dry-run on production data (n=53 strict, n=64 relaxed,
# 86 paired bet→exit cycles after excluding 10 SETTLED + 29 still-open).
FINDINGS: list[str] = [
    (
        "**Tightening MOMENTUM_DQS_TRAIL_STOP fires trailing far more often "
        "(0% prod → 32% at 3¢) but does NOT improve total P&L.** Strict cohort "
        "(n=53) Σ P&L: trail=3 → +15¢, trail=4 → +23¢ (ties current), trail=5 "
        "→ +15¢, trail=6 (current) → +23¢. The trailing fires that the sweep "
        "induces do NOT capture more value than letting TAKE_PROFIT do its "
        "work — they cut winners short. Confidence: HIGH."
    ),
    (
        "**Widening MOMENTUM_DQS_TRAIL_STOP shows the best apparent total P&L "
        "(trail=8 → +39¢ strict, +74¢ relaxed), but the result is METHODOLOGICALLY "
        "BIASED and NOT trustworthy as a config recommendation.** The replay "
        "window is [bet.ts, exit.ts] — widening the trail means trailing rarely "
        "fires within that window, so positions fall through to NO_EXIT (47% "
        "at trail=6 → 53% at trail=8). NO_EXIT realized P&L = last_observed_value "
        "− entry, which captures whatever momentary price the position happened "
        "to be at when production exited — NOT what would have actually happened "
        "under the wider trail. Confidence: LOW. Acting on this would be "
        "manufacturing a config change from noise."
    ),
    (
        "**Sweep is non-monotonic across the 6 variants** (strict cohort Σ¢: "
        "3→+15, 4→+23, 5→+15, 6→+23, 7→+12, 8→+39). Best variant delta vs "
        "current = +16¢ (8¢ vs 6¢ in strict; same in relaxed) — well below the "
        "+50¢ \"clear winner\" threshold defined in the Session 18.5 plan. "
        "Combined with the widening-direction bias (Finding 2), no config "
        "change is justified by this data."
    ),
    (
        "**Companion dead-config finding (Phase-1 grep):** `LIVE_PROFIT_TARGET`, "
        "`LIVE_TRAILING_STOP`, `LIVE_HARD_PROFIT_TARGET` are all defined in "
        "`bot/config.py:61-63` and imported in `bot/live_watcher.py:36` but "
        "**never read in any logic anywhere** (verified across all `*.py` files). "
        "Session 18 commit-message claim that trailing is gated by "
        "`LIVE_PROFIT_TARGET=0.50` is factually wrong — that gate doesn't exist. "
        "The real reason TRAILING_STOP fires 0/95 in production is that "
        "TAKE_PROFIT (sport_tp default 12¢) fires first whenever peak reaches "
        "the TP threshold. The constants will be removed in Session 18.5 Task 7."
    ),
    (
        "**Decision: OUTCOME B (marginal/noisy + methodologically constrained).** "
        "Do NOT update `MOMENTUM_DQS_TRAIL_STOP`. Two real takeaways for Session "
        "19: (a) the [bet.ts, exit.ts] tick window is fundamentally inadequate "
        "for evaluating ANY exit-logic change that DELAYS exit relative to "
        "production — Session 19's tick-replay needs ticks beyond the production "
        "exit ts (cap at game settlement); (b) any exit-logic sweep needs "
        "train/test split discipline (current sweep is in-sample on n=53). "
        "Session 18.5 sharpens the case for Session 19 with concrete prereqs."
    ),
]


def render_findings() -> str:
    if not FINDINGS:
        return "## Findings\n\n_No findings authored yet — run Task 5 (Step 5.3) on production data first._\n"
    lines = ["## Findings", ""]
    for i, f in enumerate(FINDINGS, start=1):
        lines.append(f"{i}. {f}")
        lines.append("")
    return "\n".join(lines)


def render_markdown(
    strict_results: list[SweepResult],
    relaxed_results: list[SweepResult],
    excl: ExclusionSummary,
) -> str:
    parts = [
        "# Exit Replay — MOMENTUM_DQS_TRAIL_STOP sweep",
        "",
        f"Pairs total: {excl.total_bets} bets / {excl.total_exits} exits → "
        f"{excl.paired} paired (after excluding {excl.settled_excluded} SETTLED, "
        f"{excl.open_bets} still-open).",
        f"Tick coverage: {excl.no_ticks} no-tick + {excl.thin_coverage} thin (<{RELAXED_MIN_TICKS}) "
        f"+ {excl.relaxed_n - excl.strict_n} relaxed-only "
        f"+ {excl.strict_n} strict (≥{STRICT_MIN_TICKS}).",
        "",
        f"**Cohort sizes: strict (≥{STRICT_MIN_TICKS} ticks) = {excl.strict_n}, "
        f"relaxed (≥{RELAXED_MIN_TICKS} ticks) = {excl.relaxed_n}.**",
        "",
        "Sport-profile note: swept value applied as override to ALL sports "
        "(would otherwise leave NBA=4¢ / NHL=8¢ untouched per `SPORT_PROFILES`).",
        "",
        render_sweep_table(strict_results, "strict cohort", excl.strict_n),
        "",
        render_per_sport(strict_results),
        "",
        render_sweep_table(relaxed_results, "relaxed cohort (sensitivity-only)", excl.relaxed_n),
        "",
        render_findings(),
    ]
    return "\n".join(parts)


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--journal",
        type=Path,
        default=None,
        help=f"Path to live_journal.json (default: {JOURNAL_FILE})",
    )
    parser.add_argument(
        "--ticks",
        type=Path,
        default=None,
        help=f"Path to live_ticks.jsonl (default: {TICKS_FILE})",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help=f"Path to archive dir (default: {ARCHIVE_DIR})",
    )
    parser.add_argument(
        "--trail-stops",
        type=int,
        nargs="+",
        default=SWEEP_TRAIL_STOPS,
        help=f"Trail-stop values to sweep, in cents (default: {SWEEP_TRAIL_STOPS})",
    )
    args = parser.parse_args(argv)

    journal_path = args.journal or JOURNAL_FILE
    ticks_path = args.ticks or TICKS_FILE
    archive_path = args.archive or ARCHIVE_DIR

    pairs, counts = load_bet_exit_pairs(journal_path)
    if not pairs:
        print("# No paired bet→exit cycles found. Nothing to replay.", file=sys.stderr)
        return 1

    print(f"# Loading tick streams from {ticks_path} + {archive_path}...", file=sys.stderr)
    tick_index = load_tick_index(ticks_path, archive_path)
    print(
        f"# Loaded {sum(len(v) for v in tick_index.values()):,} ticks across "
        f"{len(tick_index):,} tickers.",
        file=sys.stderr,
    )

    pairs_with_ticks, coverage = attach_ticks(pairs, tick_index)
    strict = _filter_cohort(pairs_with_ticks, STRICT_MIN_TICKS)
    relaxed = _filter_cohort(pairs_with_ticks, RELAXED_MIN_TICKS)

    excl = ExclusionSummary(
        total_bets=counts["total_bets"],
        total_exits=counts["total_exits"],
        paired=counts["paired"],
        open_bets=counts["open_bets"],
        settled_excluded=counts["settled_excluded"],
        no_ticks=coverage["no_ticks"],
        thin_coverage=coverage["thin_coverage"],
        strict_n=len(strict),
        relaxed_n=len(relaxed),
    )

    print(f"# Sweeping trail_stop ∈ {args.trail_stops} on strict cohort (n={len(strict)})...", file=sys.stderr)
    strict_results = sweep(strict, trail_stops=args.trail_stops)
    print(f"# Sweeping trail_stop ∈ {args.trail_stops} on relaxed cohort (n={len(relaxed)})...", file=sys.stderr)
    relaxed_results = sweep(relaxed, trail_stops=args.trail_stops)

    print(render_markdown(strict_results, relaxed_results, excl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
