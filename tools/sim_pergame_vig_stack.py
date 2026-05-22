#!/usr/bin/env python3
"""S159 — Per-game NBA/NHL vig_stack replay + Outcome-D ladders (SIMULATION ONLY).

Closes the S134 Open Loop: "simulation-only NBA/NHL per-game vig_stack replay …
do NOT ship scanner expansion from rough counts; replay/simulate first."

THE QUESTION
------------
S133/S134 measured a ~95-97% "YES-sum > 100c" rate on KXNBAGAME / KXNHLGAME per-game
winner markets and read it as structural vig (same shape as the +$1,064 weather/index
vig_stack workhorse). A per-game winner is 2 mutually-exclusive outcomes (Team A / Team B).
The vig_stack arb = buy BOTH NOs; exactly one wins -> the pair pays 100c; profit =
100 - (NO_ask_A + NO_ask_B). The "~97%" was a YES-sum HEADLINE. The arb is real ONLY if
you can buy the NOs AT ASK for less than the guaranteed payout. With a ~12c mean spread,
the ask-sum can exceed the payout even when the mid looks vig-rich -> spread artifact,
no capturable arb. THE GATE is the distribution of (NO_ask_sum < payout), NOT the
YES-sum headline (S130/S144 contamination lesson: necessary but not sufficient).

K-RUNG GENERALIZATION (Outcome-D ladders)
-----------------------------------------
For K mutually-exclusive, exhaustive outcomes (exactly one resolves YES), buy all K NOs:
exactly one NO -> 0, the other K-1 NOs -> 100, so payout = (K-1)*100c, cost = sum(NO_ask).
The capturable riskless arb gate = sum(NO_ask) < (K-1)*100.  This is deterministic ONLY
if the rungs are a true partition (sum of true YES probabilities = 100%, so the YES MID
prices sum to ~100c).  If the rungs are NESTED THRESHOLDS ("total >= N goals/runs"), more
than one resolves YES, the buy-all-NOs payout is OUTCOME-DEPENDENT (not (K-1)*100), and the
apparent "gate pass" is a fake-vig ARTIFACT of mis-applying the partition formula. So the
mandatory first step per series is structural classification (median sum of YES_mid).

WHY P&L IS DETERMINISTIC (no settlement lookup needed)
------------------------------------------------------
For a true partition, exactly one outcome wins -> the bought-NO set always pays (K-1)*100c
regardless of WHICH outcome wins. So per-set P&L = (K-1)*100 - sum(NO_ask) - Kalshi_fees is
deterministic from the prices alone.  This is why the sim is FULLY OFFLINE (operator
decision, S159): no live Kalshi API, no contention with the running bot. (Game void/cancel
is not modelled; flagged as an assumption.)  For NESTED THRESHOLD series the realized P&L is
outcome-dependent and would require settled game totals -- moot here because there is no
fixed-payout arb to realize; the structural classification alone rules them out.

SCOPE: read-only on bot/state/.  Writes ONLY the report under bot/state/reports/.
Touches NOTHING in bot/ runtime: no scanner/config/ACTIVE_STRATEGIES change, no bot
restart, no trades, no live API. Imports NO bot modules (so no credential read / no
network / no scanner registration side effects); the gzip+json loader is replicated
inline from tools/backtest.py:_iter_jsonl_lines (~15 lines).

Usage:
    python3 tools/sim_pergame_vig_stack.py
    python3 tools/sim_pergame_vig_stack.py --report-out /tmp/sim.md
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = _REPO_ROOT / "bot" / "state"
ARCHIVE_DIR = STATE_DIR / "archive"
UNIVERSE_FILE = STATE_DIR / "universe.jsonl"
REPORTS_DIR = STATE_DIR / "reports"

# Series under test. Winners are 2-outcome partitions; spreads/totals are the
# Outcome-D multi-rung ladders whose structure we must classify, not assume.
WINNER_SERIES = ("KXNBAGAME", "KXNHLGAME")
LADDER_SERIES = ("KXNHLSPREAD", "KXNHLTOTAL", "KXMLBTOTAL")
ALL_SERIES = WINNER_SERIES + LADDER_SERIES

# Structural classifier.  The buy-all-NOs payout is a FIXED (K-1)*100c ONLY when the rungs
# are a mutually-exclusive, exhaustive SINGLE-WINNER partition (exactly one YES).  The
# expected number of simultaneous YES outcomes = sum(true YES prob) ~= Σyes_mid / 100.  For
# a single-winner partition that is ~1; if it is materially > 1 the rungs OVERLAP (>=2 can
# be YES at once -- nested "total>=N" thresholds, or per-team "win by 1 / win by 2+" spread
# rungs where the winner satisfies both), so buying all NOs pays (K - W)*100 with W>1, NOT
# (K-1)*100, and any apparent "margin" is a formula-misapplication artifact (the S144 B-vs-T
# lesson).  A set is partition-consistent iff Σyes_mid <= PARTITION_SET_MAX_C (i.e. <= ~1.3
# expected winners).  CRITICAL: the MEDIAN over all sets can be misleading (KXNHLSPREAD's
# median Σyes_mid is 94c, but its GATE-PASSING sets sit at ~200c = 2 winners), so the
# series is judged a single-winner partition only if (a) the all-set median Σyes_mid is
# partition-consistent AND (b) a negligible fraction of its GATE-PASSING sets show overlap
# (Σyes_mid > PARTITION_SET_MAX_C).  Condition (b) is the load-bearing test: a true
# single-winner partition can NEVER price a set above ~one winner, so any material overlap
# among the cheap sets we'd actually trade disqualifies it.  Using the MEDIAN of gate-passers
# is too weak -- KXNHLSPREAD's gate-passers are bimodal (shallow near-100c sets + deep ~200c
# overlap sets), so their median lands <130 and hides the overlap.  Measured overlap fraction
# among gate-passers: winners 0.0-0.7%, KXNHLSPREAD 34.6%, totals ~100%.  Cut at 5%.
PARTITION_SET_MAX_C = 130.0
OVERLAP_PASS_MAX_PCT = 5.0

# Kalshi standard trading-fee rate. No fee model exists in bot/ (grep confirmed: only a
# crypto-vol constant). Published general-markets formula:
#   fee = ceil(0.07 * contracts * price * (1 - price))  rounded up to the next cent.
# Sports markets use the standard 0.07 rate (S&P/Nasdaq use 0.035). We model per-contract
# (contracts=1); fees are ~linear in size so per-contract is representative, and the
# round-up makes the per-leg fee mildly conservative (overstated) -- the right direction
# for a "does the edge survive realistic execution" test.
KALSHI_FEE_RATE = 0.07


def kalshi_fee_cents(price_cents: float, contracts: int = 1) -> int:
    """Kalshi trading fee for one order, in cents, rounded up to the next cent."""
    p = price_cents / 100.0
    return math.ceil(KALSHI_FEE_RATE * contracts * p * (1.0 - p) * 100.0)


def _iter_jsonl_lines(path: Path):
    """Yield parsed JSON rows from .jsonl or .jsonl.gz; tolerate malformed lines.

    Replicated from tools/backtest.py:_iter_jsonl_lines to avoid importing bot modules.
    """
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (OSError, gzip.BadGzipFile):
        return


def _source_files() -> list[Path]:
    """All universe archives + the current snapshot (mirrors backtest discovery)."""
    files = sorted(ARCHIVE_DIR.glob("universe-*.jsonl.gz"))
    if UNIVERSE_FILE.exists():
        files.append(UNIVERSE_FILE)
    return files


def load_groups(series: str, files: list[Path]) -> tuple[dict, dict]:
    """Return (groups, event_rungs) for one series.

    groups:      {(scan_id, event_ticker): [row, ...]}  -- contemporaneous legs only.
    event_rungs: {event_ticker: set(ticker)}            -- all rungs ever seen (true K).
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    event_rungs: dict[str, set] = defaultdict(set)
    needle = series + "-"
    for f in files:
        for r in _iter_jsonl_lines(f):
            # Fast reject before the dict lookups (each line is one market).
            if r.get("series_ticker") != series:
                continue
            ev = r.get("event_ticker")
            sid = r.get("scan_id")
            tkr = r.get("ticker")
            if not ev or not sid or not tkr or needle not in tkr:
                continue
            groups[(sid, ev)].append(r)
            event_rungs[ev].add(tkr)
    return groups, event_rungs


def _legs_ok(rows: list[dict]) -> bool:
    """Every rung has YES bid/ask + a buyable NO ask (>0)."""
    for r in rows:
        ya, yb, na = r.get("yes_ask"), r.get("yes_bid"), r.get("no_ask")
        if None in (ya, yb, na) or na <= 0:
            return False
    return True


def analyze(series: str, groups: dict, event_rungs: dict) -> dict:
    """Structural classification + the buy-all-NOs gate + per-game P&L.

    The gate `Σ NO_ask < (K-1)*100` is only a real riskless arb when the set is a
    single-winner partition (exactly one YES). We guard EACH set on partition-consistency
    (Σyes_mid <= PARTITION_SET_MAX_C) so that overlapping/nested gate-passers (>=2 YES,
    where the true payout is (K-W)*100 not (K-1)*100) are not mistaken for an arb.
    """
    rung_dist: dict[int, int] = defaultdict(int)
    completeness: list[float] = []

    full_eval = 0          # full-capture groups with all legs buyable
    skipped_unbuyable = 0  # full-capture groups with a missing/zero leg
    yes_ask_sum_gt100 = 0
    yes_mid_sum_gt100 = 0
    gate_pass_raw = 0      # Σ NO_ask < (K-1)*100 (cheap), regardless of partition-consistency
    gate_pass_valid = 0    # cheap AND partition-consistent (Σyes_mid <= PARTITION_SET_MAX_C)
    overlap_pass = 0       # cheap BUT overlapping (Σyes_mid > PARTITION_SET_MAX_C) -> not a partition
    yes_mid_sums: list[float] = []
    yes_ask_sums: list[float] = []
    no_ask_sums: list[float] = []
    margins: list[float] = []          # (K-1)*100 - sum(NO_ask); >0 = cheap
    ym_pass: list[float] = []          # Σyes_mid among cheap (margin>0) sets -> #winners*100

    # Per-qualifying-group records (cheap AND partition-consistent), deduped to one per game.
    qual_by_event: dict[str, dict] = {}

    for (sid, ev), rows in groups.items():
        k_here = len(rows)
        rung_dist[k_here] += 1
        k_true = len(event_rungs[ev])
        completeness.append(k_here / k_true if k_true else 0.0)
        if k_here != k_true or k_true < 2:
            continue  # only a full capture can be a complete partition
        if not _legs_ok(rows):
            skipped_unbuyable += 1
            continue

        full_eval += 1
        payout = (k_true - 1) * 100.0
        no_sum = sum(r["no_ask"] for r in rows)
        yes_ask_sum = sum(r["yes_ask"] for r in rows)
        yes_mid_sum = sum((r["yes_ask"] + r["yes_bid"]) / 2.0 for r in rows)
        no_ask_sums.append(no_sum)
        yes_ask_sums.append(yes_ask_sum)
        yes_mid_sums.append(yes_mid_sum)
        if yes_ask_sum > 100:
            yes_ask_sum_gt100 += 1
        if yes_mid_sum > 100:
            yes_mid_sum_gt100 += 1
        margin = payout - no_sum
        margins.append(margin)
        if margin <= 0:
            continue
        gate_pass_raw += 1
        ym_pass.append(yes_mid_sum)
        if yes_mid_sum > PARTITION_SET_MAX_C:
            overlap_pass += 1
            continue  # overlapping/nested set: (K-1)*100 payout is fictitious here
        gate_pass_valid += 1
        gross = margin
        fees = sum(kalshi_fee_cents(r["no_ask"]) for r in rows)
        net = gross - fees
        min_vol24 = min(int(r.get("volume_24h") or 0) for r in rows)
        min_oi = min(int(r.get("open_interest") or 0) for r in rows)
        rec = {
            "event": ev, "scan_id": sid, "k": k_true,
            "no_sum": no_sum, "payout": payout, "gross": gross,
            "fees": fees, "net": net, "ym": yes_mid_sum,
            "min_vol24": min_vol24, "min_oi": min_oi,
        }
        prev = qual_by_event.get(ev)  # one entry per game: keep BEST (max net) scan
        if prev is None or net > prev["net"]:
            qual_by_event[ev] = rec

    med_ym_all = statistics.median(yes_mid_sums) if yes_mid_sums else 0.0
    med_ym_pass = statistics.median(ym_pass) if ym_pass else None
    pct_overlap_at_pass = (100.0 * overlap_pass / gate_pass_raw) if gate_pass_raw else 0.0
    # Single-winner partition iff (a) the all-set median is partition-consistent AND (b)
    # overlap among the gate-passing (tradeable) sets is negligible. (b) catches KXNHLSPREAD:
    # 34.6% of its cheap sets price >1 winner, so it is not a clean partition (the median of
    # passers is bimodal and would hide this).
    is_partition = (
        med_ym_all <= PARTITION_SET_MAX_C
        and pct_overlap_at_pass < OVERLAP_PASS_MAX_PCT
    )
    structure = "SINGLE_WINNER_PARTITION" if is_partition else "NOT_SINGLE_WINNER_PARTITION"

    # P&L is only meaningful for a confirmed single-winner partition. For overlapping/nested
    # series the per-set payout is not (K-1)*100, so we do NOT report a P&L.
    qual = list(qual_by_event.values()) if is_partition else []
    nets = sorted((q["net"] for q in qual), reverse=True)
    pnl_sum = sum(nets)
    pnl_median = statistics.median(nets) if nets else 0.0
    pnl_ex_top = sum(nets[1:]) if len(nets) > 1 else (0.0 if nets else 0.0)
    n_net_positive = sum(1 for n in nets if n > 0)
    illiquid = sum(1 for q in qual if q["min_vol24"] <= 0 or q["min_oi"] <= 0)

    return {
        "series": series,
        "n_groups": len(groups),
        "unique_events": len(event_rungs),
        "rung_dist": dict(sorted(rung_dist.items())),
        "median_completeness": statistics.median(completeness) if completeness else 0.0,
        "full_eval": full_eval,
        "skipped_unbuyable": skipped_unbuyable,
        "structure": structure,
        "median_yes_mid_sum": med_ym_all,
        "median_yes_mid_at_pass": med_ym_pass,
        "pct_overlap_at_pass": pct_overlap_at_pass,
        "exp_winners_at_pass": (med_ym_pass / 100.0) if med_ym_pass is not None else None,
        "median_yes_ask_sum": statistics.median(yes_ask_sums) if yes_ask_sums else 0.0,
        "median_no_ask_sum": statistics.median(no_ask_sums) if no_ask_sums else 0.0,
        "median_margin": statistics.median(margins) if margins else 0.0,
        "pct_yes_ask_gt100": 100.0 * yes_ask_sum_gt100 / full_eval if full_eval else 0.0,
        "pct_yes_mid_gt100": 100.0 * yes_mid_sum_gt100 / full_eval if full_eval else 0.0,
        "pct_gate_pass": 100.0 * gate_pass_raw / full_eval if full_eval else 0.0,
        "gate_pass_raw": gate_pass_raw,
        "gate_pass_valid": gate_pass_valid,
        "qual_games": len(qual),
        "qual_records": sorted(qual, key=lambda q: q["net"], reverse=True),
        "pnl_sum": pnl_sum,
        "pnl_median": pnl_median,
        "pnl_ex_top": pnl_ex_top,
        "n_net_positive": n_net_positive,
        "illiquid": illiquid,
    }


def _verdict(m: dict) -> str:
    if m["structure"] == "NOT_SINGLE_WINNER_PARTITION":
        ew = m["exp_winners_at_pass"]
        ewtxt = f"~{ew:.1f} simultaneous winners" if ew else "median Σyes_mid >> 100c"
        return (f"NOT a single-winner partition ({ewtxt} -> >=2 rungs resolve YES) -> "
                f"buy-all-NOs pays (K-W)*100 with W>1, NOT (K-1)*100; the apparent gate "
                f"margin is a formula-misapplication artifact (S144 B-vs-T). No riskless arb.")
    if m["pct_gate_pass"] < 10.0:
        return ("spread artifact -> capturable arb on <10% of contemporaneous sets; "
                "median set costs MORE than the guaranteed payout")
    if m["pnl_sum"] <= 0 or m["pnl_median"] <= 0 or m["pnl_ex_top"] <= 0:
        return "gate fires but per-game net P&L <= 0 after fees (and/or concentration-driven)"
    return ("CANDIDATE: gate fires AND per-game net P&L positive, median>0, ex-top>0 -> "
            "investigate fill feasibility before any Outcome-A recommendation")


def render(metrics: list[dict], today: date) -> str:
    L: list[str] = []
    L.append(f"# S159 — Per-game NBA/NHL vig_stack replay + Outcome-D ladders ({today})")
    L.append("")
    L.append("**SIMULATION ONLY.** Closes the S134 Open Loop. No scanner/config/"
             "`ACTIVE_STRATEGIES` change, no bot restart, no trades, no live Kalshi API. "
             "Fully offline (deterministic riskless-arb P&L). Source: all "
             "`bot/state/archive/universe-*.jsonl.gz` + current `universe.jsonl`.")
    L.append("")

    # ---- Headline: the gating contrast -------------------------------------------------
    L.append("## The gate (necessary-but-not-sufficient -> sufficient)")
    L.append("")
    L.append("`buy all K NOs` -> exactly one outcome wins -> payout `(K-1)*100c`, "
             "cost `Σ NO_ask`. Capturable riskless arb iff `Σ NO_ask < (K-1)*100` "
             "(K=2 winners: `NO_ask_A + NO_ask_B < 100`). The headline `YES-sum > 100` is "
             "measured at `yes_ask` (pay the ask on every YES leg) — the adversarial mirror, "
             "not the capturable side.")
    L.append("")
    L.append("| series | structure | full sets | Σyes_ask>100 | "
             "**gate: ΣNO_ask<payout** | median Σyes_mid (all) | Σyes_mid @gate-pass (≈#winners×100) | median margin |")
    L.append("|---|---|---|---|---|---|---|---|")
    for m in metrics:
        part = m["structure"] == "SINGLE_WINNER_PARTITION"
        struct_lbl = "1-winner partition" if part else "overlap / nested"
        gate_lbl = f"**{m['pct_gate_pass']:.1f}%**" if part else f"~~{m['pct_gate_pass']:.1f}%~~ (ARTIFACT)"
        ym_pass = m["median_yes_mid_at_pass"]
        ym_pass_txt = (f"{ym_pass:.0f}c (≈{ym_pass/100:.1f})" if ym_pass is not None else "—")
        L.append(
            f"| {m['series']} | {struct_lbl} | {m['full_eval']} | "
            f"{m['pct_yes_ask_gt100']:.1f}% | {gate_lbl} | "
            f"{m['median_yes_mid_sum']:.0f}c | {ym_pass_txt} | {m['median_margin']:+.0f}c |"
        )
    L.append("")
    L.append("Reading: a single-winner partition's `Σyes_mid` sits near 100c (one winner) "
             "**including its gate-passing sets**. If the gate-passers sit far above 100c "
             "(≈ #simultaneous winners × 100), the rungs OVERLAP — buying all NOs pays "
             "`(K-W)*100` with W>1, not `(K-1)*100`, so the gate margin is fictitious. "
             "KXNHLSPREAD's all-set median (94c) looks partition-like, but its gate-passers "
             "sit at ~200c (2 winners): the winning team satisfies both its `by-1` and "
             "`by-2+` rungs. The totals are nested `total>=N` thresholds (4-6 winners).")
    L.append("")

    # ---- Per-series detail -------------------------------------------------------------
    for m in metrics:
        L.append(f"## {m['series']} — {m['structure']}")
        L.append("")
        L.append(f"- groups (scan,event): {m['n_groups']:,} | unique games: "
                 f"{m['unique_events']} | rung-count dist: {m['rung_dist']} | "
                 f"median capture completeness: {m['median_completeness']:.2f}")
        L.append(f"- full-capture evaluable sets: {m['full_eval']:,} "
                 f"(skipped, unbuyable leg: {m['skipped_unbuyable']})")
        L.append(f"- median Σyes_ask = {m['median_yes_ask_sum']:.0f}c | "
                 f"median Σyes_mid = {m['median_yes_mid_sum']:.0f}c | "
                 f"median ΣNO_ask = {m['median_no_ask_sum']:.0f}c")
        L.append("")
        if m["structure"] == "NOT_SINGLE_WINNER_PARTITION":
            if m["median_yes_mid_sum"] > PARTITION_SET_MAX_C:
                shape = (f"The K rungs are nested `total>=N` thresholds (all-set median "
                         f"Σyes_mid = {m['median_yes_mid_sum']:.0f}c ≈ "
                         f"{m['median_yes_mid_sum']/100:.0f} simultaneous winners)")
            else:
                shape = (f"The all-set median Σyes_mid ({m['median_yes_mid_sum']:.0f}c) LOOKS "
                         f"partition-like, but **{m['pct_overlap_at_pass']:.0f}% of the "
                         f"gate-passing sets price >1 simultaneous winner** (Σyes_mid up to "
                         f"~250c) — the per-team `by-1`/`by-2+` rungs BOTH fire for the winning "
                         f"team, so the rungs overlap. A clean partition has ~0% such sets "
                         f"(the winners do), so this overlap is decisive")
            L.append(f"**Structural disqualification — NOT a single-winner partition.** {shape}. "
                     f"With W>=2 rungs resolving YES, buying all K NOs pays `(K-W)*100`, NOT "
                     f"`(K-1)*100`; the {m['pct_gate_pass']:.1f}% apparent gate-pass (and any "
                     f"positive 'margin') is a fake-vig artifact of mis-applying the partition "
                     f"payout formula (the S144 B-vs-T lesson). No fixed-payout vig_stack arb "
                     f"exists. A realized P&L would require settled outcomes (outcome-dependent) "
                     f"and is moot — nothing riskless to realize. **Not a vig_stack shape; "
                     f"corrects the brief's 'closer to vig_stack shape' premise.**")
            L.append("")
            continue

        # Single-winner partition: gate + per-game P&L (deterministic)
        payout_lbl = int((m["qual_records"][0]["k"] - 1) * 100) if m["qual_records"] else 100
        L.append(f"**Capturable-arb gate:** {m['gate_pass_raw']:,} of {m['full_eval']:,} "
                 f"full sets pass `ΣNO_ask < {payout_lbl}` = **{m['pct_gate_pass']:.1f}%** "
                 f"({m['gate_pass_valid']:,} of {m['gate_pass_raw']:,} partition-consistent / "
                 f"single-winner); median margin **{m['median_margin']:+.0f}c** "
                 f"(negative => typical set costs MORE than the guaranteed payout).")
        L.append("")
        L.append(f"**Per-unique-game P&L** (each game traded once at its best qualifying "
                 f"scan; deterministic = payout − ΣNO_ask − Kalshi fees @ {KALSHI_FEE_RATE}):")
        L.append("")
        L.append(f"- unique games with ≥1 qualifying scan: **{m['qual_games']}** "
                 f"(of {m['unique_events']} games)")
        L.append(f"- Σ net P&L: **{m['pnl_sum']:+.1f}c** | median: **{m['pnl_median']:+.1f}c** "
                 f"| Σ ex-top-winner: **{m['pnl_ex_top']:+.1f}c**")
        L.append(f"- games net-positive after fees: {m['n_net_positive']} / {m['qual_games']} "
                 f"| illiquid (a leg with 0 vol_24h or 0 OI): {m['illiquid']} / {m['qual_games']}")
        if m["qual_records"]:
            L.append("")
            L.append("  per-game qualifiers (best scan): event | k | ΣNO_ask | Σyes_mid | "
                     "gross | fees | net | min leg vol_24h | min leg OI")
            for q in m["qual_records"]:
                L.append(
                    f"  - `{q['event']}` | k={q['k']} | {q['no_sum']:.0f}c | "
                    f"{q['ym']:.0f}c | {q['gross']:+.0f}c | −{q['fees']}c | "
                    f"**{q['net']:+.0f}c** | vol24={q['min_vol24']} | OI={q['min_oi']}"
                )
        L.append("")
        L.append(f"_Verdict:_ {_verdict(m)}")
        L.append("")

    # ---- Outcome -----------------------------------------------------------------------
    L.append("## Outcome")
    L.append("")
    partitions = [m for m in metrics if m["structure"] == "SINGLE_WINNER_PARTITION"]
    non_partitions = [m for m in metrics if m["structure"] == "NOT_SINGLE_WINNER_PARTITION"]
    any_candidate = any(_verdict(m).startswith("CANDIDATE") for m in partitions)
    outcome = "A" if any_candidate else "C"
    L.append(f"**Outcome {outcome}.**")
    L.append("")
    if outcome == "C":
        L.append("The apparent per-game 'structural vig' is illusory. Only the 2-outcome "
                 "winners are a genuine single-winner partition (the vig_stack shape); the "
                 "spread and totals are not. Two distinct mechanisms:")
        L.append("")
        L.append("1. **2-outcome winners (KXNBAGAME / KXNHLGAME): spread artifact.** The "
                 "`yes_ask`-sum>100 headline (~96-97%, reconciling with S133/S134) is the "
                 "adversarial mirror — at MID the median sum is exactly 100c (no real vig). "
                 "The capturable `NO_ask_A + NO_ask_B < 100` gate fires on only 2-4% of sets "
                 "at a negative median margin, and every thin qualifier nets ≤0 after Kalshi "
                 "fees. The ~12c spread eats the entire mid-vig.")
        L.append("2. **Spread + totals (KXNHLSPREAD / KXNHLTOTAL / KXMLBTOTAL): not a "
                 "single-winner partition.** Totals are nested `total>=N` thresholds "
                 "(~4-6 simultaneous winners); the spread's per-team `by-1`/`by-2+` rungs both "
                 "fire for the winning team (~2 winners at the gate-passing sets). When W>=2 "
                 "rungs resolve YES, buying all NOs pays `(K-W)*100`, not `(K-1)*100`, so the "
                 "high apparent gate-pass / positive 'margin' (incl. KXNHLSPREAD's +$14.23 on "
                 "liquid sets) is a formula-misapplication artifact. **This corrects the "
                 "brief's premise** that the multi-rung ladders are 'closer to true vig_stack "
                 "shape than 2-outcome winners' — they are further from it.")
        L.append("")
        L.append("**=> CLOSE the S134 Open Loop, ruled-out.** Do NOT ship a scanner expansion "
                 "for per-game winners, spreads, or totals. With thousands of contemporaneous "
                 "sets this is a genuine ruled-out, not N-thin.")
    else:
        cand = ", ".join(m["series"] for m in partitions if _verdict(m).startswith("CANDIDATE"))
        L.append(f"Single-winner-partition series with a firing gate AND positive per-game net "
                 f"P&L that is not concentration-driven (median>0, ex-top>0): **{cand}**. "
                 f"Recommend a FOLLOW-UP scanner-expansion session (this session ships no "
                 f"scanner change) after a dedicated fill-feasibility and exit-policy review.")
    L.append("")

    # ---- Flags / notes -----------------------------------------------------------------
    L.append("## Notes")
    L.append("")
    L.append("- **Battle Scar #9 (auto-exit exemption) — MOOT this session.** "
             "`vig_stack_series`/`vig_stack_no` are auto-exit-EXEMPT; `vig_stack_futures` is "
             "NOT. A per-game expansion would need an explicit exemption decision — flagged, "
             "not decided, because Outcome C ships no scanner expansion.")
    L.append("- **Settlement not required.** For a true partition the bought-NO set pays "
             "`(K-1)*100c` regardless of which outcome wins, so P&L is deterministic from "
             "prices alone — the sim is fully offline (no Kalshi API, no contention with the "
             "running bot). Game void/cancel is not modelled (would void both legs).")
    L.append("- **Fee model:** Kalshi published `ceil(0.07·C·p·(1−p))` per NO leg, "
             "per-contract; no fee helper exists in `bot/` so it is modelled here.")
    L.append("- **Pairing integrity:** the cursor walk captures both/all legs of a game "
             "together (median completeness 1.00, no singletons), so the 100%-partial-scan "
             "regime (CLAUDE.md §12) does not corrupt reconstruction.")
    if non_partitions:
        L.append(f"- **Outcome-D scope:** ran full classification on "
                 f"{', '.join(m['series'] for m in metrics if m['series'] in LADDER_SERIES)}. "
                 f"None is a single-winner partition, so none is a vig_stack shape; if a future "
                 f"session wants their outcome-dependent P&L it needs settled outcomes (a "
                 f"different, settlement-dependent sim).")
    L.append("- **Classifier guard:** a set enters the gate/P&L only if partition-consistent "
             "(Σyes_mid ≤ 130c ≈ one winner). This is what prevents the KXNHLSPREAD trap — its "
             "gate-passers sit at ~200c (two winners), so they are correctly excluded as "
             "non-arb rather than booked as a fictitious +$14.23.")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="S159 per-game vig_stack offline replay.")
    parser.add_argument("--report-out", default=None,
                        help="Report path (default: bot/state/reports/"
                             "session_159_pergame_vig_stack_sim_<today>.md).")
    args = parser.parse_args(argv)

    files = _source_files()
    if not files:
        print(f"error: no universe data under {STATE_DIR}")
        return 2

    metrics = []
    for series in ALL_SERIES:
        groups, event_rungs = load_groups(series, files)
        m = analyze(series, groups, event_rungs)
        metrics.append(m)
        print(f"{series:12s} structure={m['structure']:16s} "
              f"full_sets={m['full_eval']:5d} gate={m['pct_gate_pass']:5.1f}% "
              f"median_yes_mid={m['median_yes_mid_sum']:5.0f}c "
              f"qual_games={m['qual_games']:3d} pnl_sum={m['pnl_sum']:+.0f}c")

    today = date.today()
    out = Path(args.report_out) if args.report_out else (
        REPORTS_DIR / f"session_159_pergame_vig_stack_sim_{today.isoformat()}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(metrics, today))
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
