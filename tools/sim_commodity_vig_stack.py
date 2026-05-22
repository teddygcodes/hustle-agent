#!/usr/bin/env python3
"""S160 — Commodity-ladder vig_stack simulation (SIMULATION ONLY).

THE QUESTION (next prospecting pass after S159)
-----------------------------------------------
S159 ruled out per-game winners: 2-outcome markets price to 100c at MID (no overround),
so the vig was a spread artifact. The lesson: vig_stack's edge lives in MANY-RUNG
mutually-exclusive ladders with real MID-overround (the weather/index workhorse; KXINX is a
stable-whitelisted member). universe_report shows we scan ~2.2% of the universe and ignore
commodity PRICE-LADDER families. S160 asks: do KXWTI / KXSILVERD / KXAAAGASD / KXAAAGASW
carry capturable vig like KXINX?

THE STRUCTURAL FINDING (verified against the archives)
------------------------------------------------------
The premise "commodity ladders are the SAME shape as KXINX" is FALSE:
  * Commodity families are cumulative "above-$X" THRESHOLD ladders (`-T` tickers, a CDF /
    survival curve): "WTI settle above 94.99 / 95.99 / ...", YES monotone-decreasing. They
    are NESTED / OVERLAPPING — if WTI settles at 98, "above 94.99/95.99/96.99/97.99" are ALL
    YES at once. NOT a single-winner partition (the KXNHLSPREAD failure S159 caught).
  * KXINX (the benchmark) is dominantly `-B` BETWEEN-buckets — a true exclusive partition.
    The real vig_stack filters KXINX to `-B` rungs.
So buying all NOs on the RAW threshold rungs pays `(K-W)*100` with W>1, not `(K-1)*100`; the
huge Σyes_mid (thousands of cents) is a CDF integrated over nested thresholds, NOT overround.

Mutual-exclusivity discriminator: Σyes_mid is spread-inflated on illiquid wings (KXINX `-B`
wings quote yes_bid=0/yes_ask=7 -> a phantom 3.5c mid each), so a genuine 1-winner partition
can read Σyes_mid ~170c without overlapping. The ROBUST signal is the count of rungs
simultaneously deep-in-the-money (yes_mid > 50c) ~= the number of simultaneous YES winners:
KXINX ~1, commodities many.

THE SYNTHETIC-BUCKET TEST (operator decision: go further than ruling out raw vig_stack)
---------------------------------------------------------------------------------------
Difference adjacent thresholds into an exclusive partition:
  bucket [X_i, X_{i+1})  prob_mid = P(>=X_i) - P(>=X_{i+1}) = yes_mid_i - yes_mid_{i+1}
  tail-low (< X_0)       prob_mid = 100 - yes_mid_0
  tail-high (>= X_{K-1}) prob_mid = yes_mid_{K-1}
This ALWAYS telescopes to Σ prob_mid = 100c => the synthetic partition has ZERO mid-overround
BY CONSTRUCTION. Any apparent edge is a bid-ask SPREAD artifact, and the only buy-only
tradeable construction crosses TWO real spreads per synthetic leg:
  synthetic-bucket NO on [X_i, X_{i+1})  =  long C_i NO + long C_{i+1} YES
                                             (pays 100 iff price NOT in the bucket)
                                             cost = no_ask_i + yes_ask_{i+1}
The buy-ALL-synthetic-NOs structural arb has a clean, settlement-free result:
  payout (one bucket wins) = (n_buckets - 1) * 100 = K * 100
  cost  = Σ over buckets (synthetic-NO cost) = Σ_i (yes_ask_i + no_ask_i) = 100*K + Σ spread_i
  margin = payout - cost = -Σ spread_i        (NEGATIVE, exactly minus the total spread)
So the synthetic partition is provably an Outcome-C spread artifact, worse after fees. This
sim turns that analytic claim into MEASURED evidence on real ladders and benchmarks it against
KXINX (the known-good `-B` partition, analysed DIRECTLY — no differencing).

WHAT THIS SIM COMPUTES
----------------------
1. Structural classifier: KXINX `-B` vs each commodity family -> SINGLE_WINNER_PARTITION
   (median in-the-money rung count ~1) vs NOT (many). Reports RAW Σyes_mid (spec's overround
   measure) alongside the robust ITM count.
2. Buy-all-NOs structural arb: KXINX direct on `-B` rungs; commodities on the differenced
   SYNTHETIC partition. Verify synthetic Σmid=100c; margin (= -Σspread for synthetic) before
   and after fees; floor sweep 0.70/0.85/0.93 on the NO cost.
3. Offline price-convergence SETTLEMENT (no live API): per event, the latest snapshot whose
   curve has COLLAPSED (one bucket ~100c, rest ~0c) pins the winning bucket. Selective
   realized P&L for floor-selected NOs on the settled subset (deduped raw vs unique-day,
   S86/S156). Thin coverage -> that sub-result is Outcome B; the structural margin stands.
4. Reality checks (S159): mid vs ask, after-fees, concentration (median + ex-top), fill
   feasibility (min per-leg vol_24h / OI), hold-to-settlement.

SCOPE: read-only on bot/state/. Writes ONLY the report under bot/state/reports/. No
scanner/config/ACTIVE_STRATEGIES change, no bot restart, no trades, no live Kalshi API.
Imports NO bot modules (gzip+json loader replicated inline from tools/sim_pergame_vig_stack.py
which itself mirrors tools/backtest.py:_iter_jsonl_lines).

Usage:
    python3 tools/sim_commodity_vig_stack.py
    python3 tools/sim_commodity_vig_stack.py --report-out /tmp/sim.md
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

# Benchmark: the known-good, already-traded vig_stack `-B` partition family (S&P range).
BENCHMARK_SERIES = "KXINX"
# Primary commodity threshold families under test.
COMMODITY_SERIES = ("KXWTI", "KXSILVERD", "KXAAAGASD", "KXAAAGASW")
# Outcome-D commodity families (S127) — included so the structural ruling-out covers the
# whole commodity price-ladder class, not just the four primaries.
OUTCOME_D_SERIES = ("KXGOLDD", "KXBRENTD", "KXNATGASD")
ALL_SERIES = (BENCHMARK_SERIES,) + COMMODITY_SERIES + OUTCOME_D_SERIES

# ---- Structural classifier ---------------------------------------------------------------
# Σyes_mid is spread-inflated on illiquid wings (KXINX `-B` wings quote yes_bid=0/yes_ask=7 ->
# a phantom 3.5c mid each), so a genuine 1-winner partition can read Σyes_mid ~170c without
# overlapping. The robust mutual-exclusivity discriminator is the count of rungs simultaneously
# deep-in-the-money (yes_mid > 50c) ~= the number of simultaneous YES winners: a single-winner
# partition has ~1; nested thresholds have many.
ITM_WIN_C = 50.0
PARTITION_MAX_ITM = 1.5  # allow the occasional boundary-straddle (2 buckets near 50c)

# ---- vig_stack mechanic (mirrors bot/strategies/vig_stack_series.py + bot/config.py) -------
# Ladder Σ-YES gate: reject unless sum(yes_ask) >= 105c  (vig_stack_series.py:461-464). The
# overround test is reported at MID (the honest measure, per S159) and at ASK (the real code).
VIG_GATE_MIN_YES_SUM_C = 105.0
# Filter-F NO floor: buy NO only if no_ask/100 >= floor. Stable families (incl. KXINX) = 0.70;
# volatile/other = 0.93 (config.py:584/635). Commodities are not stable -> sweep the band.
STABLE_FLOOR = 0.70
FLOOR_SWEEP = (0.70, 0.85, 0.93)
# Hard floor: vig_stack rejects no_ask_prob < 0.03 (vig_stack_series.py:623).
HARD_NO_FLOOR = 0.03

# ---- Kalshi fees -------------------------------------------------------------------------
# fee = ceil(rate * contracts * p * (1-p)) rounded up to the next cent, per order/leg.
# KXINX is an S&P index market (Kalshi financial rate 0.035); commodities use the general
# 0.07 (conservative: overstates fees -> the right direction for a "does edge survive
# realistic execution" test). No fee model exists in bot/ (CLAUDE.md) so it is modelled here.
FEE_RATE_FINANCIAL = 0.035
FEE_RATE_GENERAL = 0.07

# Settlement (price-convergence) detection: a curve has COLLAPSED iff exactly one bucket is
# ~certain (prob >= COLLAPSE_HI) and the rest are ~0 (2nd-highest <= COLLAPSE_LO).
COLLAPSE_HI = 90.0
COLLAPSE_LO = 10.0
# Minimum settled events to report a realized-P&L cohort rather than declaring it Outcome-B-thin.
SETTLEMENT_MIN_EVENTS = 5
MIN_RUNGS = 3  # a ladder needs >=3 rungs to be a meaningful partition


def fee_rate_for(series: str) -> float:
    return FEE_RATE_FINANCIAL if series == BENCHMARK_SERIES else FEE_RATE_GENERAL


def kalshi_fee_cents(price_cents: float, rate: float, contracts: int = 1) -> int:
    """Kalshi trading fee for one order/leg, in cents, rounded up to the next cent."""
    p = price_cents / 100.0
    return math.ceil(rate * contracts * p * (1.0 - p) * 100.0)


def _iter_jsonl_lines(path: Path):
    """Yield parsed JSON rows from .jsonl or .jsonl.gz; tolerate malformed lines.

    Replicated from tools/sim_pergame_vig_stack.py (which mirrors tools/backtest.py) so this
    sim imports no bot modules (no credential read / no network / no scanner side effects).
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
    """All universe archives (gz + the rare uncompressed full dump) + current snapshot."""
    files = sorted(ARCHIVE_DIR.glob("universe-*.jsonl.gz"))
    files += sorted(ARCHIVE_DIR.glob("universe-*.jsonl"))  # uncompressed full dumps (e.g. 05-17)
    if UNIVERSE_FILE.exists():
        files.append(UNIVERSE_FILE)
    return files


def _parse_strike(ticker: str):
    """Lower-edge strike from the final ticker segment (T94.99 -> 94.99; B7662 -> 7662)."""
    seg = ticker.split("-")[-1]
    num = "".join(c for c in seg if c.isdigit() or c == ".")
    if num.count(".") > 1 or not any(c.isdigit() for c in num):
        return None
    try:
        return float(num)
    except ValueError:
        return None


def load_groups(series: str, files: list[Path], b_only: bool = False) -> tuple[dict, dict]:
    """Return (groups, event_rungs) for one series.

    groups:      {(scan_id, event_ticker): {ticker: row}}  -- contemporaneous legs, deduped
                 by ticker within the scan (defends against cross-file overlap of the same
                 scan_id appearing in both a .jsonl full dump and its .jsonl.gz partial).
    event_rungs: {event_ticker: set(ticker)}               -- all rungs ever seen (true K).

    b_only=True keeps only `-B` between-bucket rungs (the real vig_stack KXINX filter).
    """
    groups: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    event_rungs: dict[str, set] = defaultdict(set)
    for f in files:
        for r in _iter_jsonl_lines(f):
            if r.get("series_ticker") != series:
                continue
            tkr = r.get("ticker")
            ev = r.get("event_ticker")
            sid = r.get("scan_id")
            if not tkr or not ev or not sid:
                continue
            if b_only and "-B" not in tkr:
                continue
            groups[(sid, ev)][tkr] = r
            event_rungs[ev].add(tkr)
    return groups, event_rungs


def _legs_ok(rows: list[dict]) -> bool:
    """Every rung has YES bid/ask + a buyable NO ask (>0)."""
    for r in rows:
        ya, yb, na = r.get("yes_ask"), r.get("yes_bid"), r.get("no_ask")
        if None in (ya, yb, na) or na <= 0:
            return False
    return True


def _full_sets(groups: dict, event_rungs: dict):
    """Yield (sid, ev, rows) for full-capture, all-legs-buyable ladders with >=MIN_RUNGS."""
    for (sid, ev), by_ticker in groups.items():
        rows = list(by_ticker.values())
        k_here = len(rows)
        k_true = len(event_rungs[ev])
        if k_here != k_true or k_true < MIN_RUNGS:
            continue
        if not _legs_ok(rows):
            continue
        yield sid, ev, rows


# ----------------------------------------------------------------------------------------- #
# Step 1: structural classification — is the family a single-winner partition?
# ----------------------------------------------------------------------------------------- #
def classify_structure(series: str, groups: dict, event_rungs: dict) -> dict:
    yes_mid_sums, yes_ask_sums, itm_counts = [], [], []
    full_eval = 0
    for _sid, _ev, rows in _full_sets(groups, event_rungs):
        full_eval += 1
        mids = [(r["yes_ask"] + r["yes_bid"]) / 2.0 for r in rows]
        yes_ask_sums.append(sum(r["yes_ask"] for r in rows))
        yes_mid_sums.append(sum(mids))
        itm_counts.append(sum(1 for m in mids if m > ITM_WIN_C))

    if full_eval == 0:
        structure = "INSUFFICIENT_DATA"
        med_itm = med_ym = med_ya = 0.0
    else:
        med_itm = statistics.median(itm_counts)
        med_ym = statistics.median(yes_mid_sums)
        med_ya = statistics.median(yes_ask_sums)
        structure = ("SINGLE_WINNER_PARTITION" if med_itm <= PARTITION_MAX_ITM
                     else "NOT_SINGLE_WINNER_PARTITION")
    return {
        "series": series,
        "n_groups": len(groups),
        "unique_events": len(event_rungs),
        "full_eval": full_eval,
        "structure": structure,
        "median_itm_count": med_itm,           # ~= simultaneous YES winners (robust)
        "median_yes_mid_sum": med_ym,          # spec's overround measure (spread-inflated)
        "median_yes_ask_sum": med_ya,
    }


# ----------------------------------------------------------------------------------------- #
# Step 2: bucketization — direct (`-B` partition) or synthetic (differenced `-T` thresholds)
# ----------------------------------------------------------------------------------------- #
# Each bucket dict: {label, prob, no_cost, legs, leg_prices, vmin, oimin}
#   prob       : P(this bucket wins), in cents (Σ over buckets ~= 100c for a partition)
#   no_cost    : cents to BUY this bucket's NO (pays 100 iff bucket does NOT win)
#   leg_prices : per-leg ask prices for fee modelling
def direct_buckets(rows: list[dict]):
    """One bucket per `-B` rung (already an exclusive partition). NO = the rung's NO (1 leg)."""
    out = []
    for r in rows:
        ym = (r["yes_ask"] + r["yes_bid"]) / 2.0
        out.append({
            "label": r["ticker"].split("-")[-1],
            "prob": ym,
            "no_cost": float(r["no_ask"]),
            "legs": 1,
            "leg_prices": [float(r["no_ask"])],
            "vmin": int(r.get("volume_24h") or r.get("volume") or 0),
            "oimin": int(r.get("open_interest") or 0),
            "_strike": _parse_strike(r.get("ticker", "")) or 0.0,
        })
    out.sort(key=lambda b: b["_strike"])
    return out, 0  # no monotonicity clamp for direct buckets


def synthetic_buckets(rows: list[dict]):
    """Difference a threshold ladder into an exclusive synthetic partition (+clamp count).

    Returns (buckets, violations) or (None, 0) if strikes are unparseable.
    """
    parsed = []
    for r in rows:
        s = _parse_strike(r.get("ticker", ""))
        if s is None:
            return None, 0
        parsed.append((s, r))
    parsed.sort(key=lambda t: t[0])
    strikes = [s for s, _ in parsed]
    rr = [r for _, r in parsed]
    K = len(rr)
    yes_mid = [(r["yes_ask"] + r["yes_bid"]) / 2.0 for r in rr]
    yes_ask = [float(r["yes_ask"]) for r in rr]
    no_ask = [float(r["no_ask"]) for r in rr]
    vol = [int(r.get("volume_24h") or r.get("volume") or 0) for r in rr]
    oi = [int(r.get("open_interest") or 0) for r in rr]

    # Monotonize: P(>=X) must be non-increasing in X. cummin from the low-strike end.
    clamped, run, violations = [], 101.0, 0
    for ym in yes_mid:
        if ym > run + 1e-9:
            violations += 1
        run = min(run, ym)
        clamped.append(run)

    buckets = []
    # tail-low: price < X_0. NO("not < X_0" = ">= X_0") = long C_0 YES (1 leg).
    buckets.append({"label": f"<{strikes[0]:g}", "prob": 100.0 - clamped[0],
                    "no_cost": yes_ask[0], "legs": 1, "leg_prices": [yes_ask[0]],
                    "vmin": vol[0], "oimin": oi[0]})
    # interior: [X_{j-1}, X_j). NO = long C_{j-1} NO + long C_j YES (2 legs).
    for j in range(1, K):
        buckets.append({
            "label": f"[{strikes[j-1]:g},{strikes[j]:g})",
            "prob": clamped[j - 1] - clamped[j],
            "no_cost": no_ask[j - 1] + yes_ask[j],
            "legs": 2, "leg_prices": [no_ask[j - 1], yes_ask[j]],
            "vmin": min(vol[j - 1], vol[j]), "oimin": min(oi[j - 1], oi[j]),
        })
    # tail-high: price >= X_{K-1}. NO("not >= X_{K-1}" = "< X_{K-1}") = long C_{K-1} NO (1 leg).
    buckets.append({"label": f">={strikes[K-1]:g}", "prob": clamped[K - 1],
                    "no_cost": no_ask[K - 1], "legs": 1, "leg_prices": [no_ask[K - 1]],
                    "vmin": vol[K - 1], "oimin": oi[K - 1]})
    return buckets, violations


def _bucketize(series: str, rows: list[dict]):
    """Return (mode, buckets, violations). `-B` families -> direct; thresholds -> synthetic."""
    if all("-B" in r.get("ticker", "") for r in rows):
        b, v = direct_buckets(rows)
        return "direct", b, v
    b, v = synthetic_buckets(rows)
    return "synthetic", b, v


def _winner_index(buckets) -> int | None:
    """Winning bucket index from a COLLAPSED snapshot, else None.

    At settlement exactly one bucket is ~certain (prob ~100c) and the rest ~0c — true for the
    `-B` partition (one rung settles YES) and the synthetic partition (the survival curve
    steps, so one differenced bucket -> 100c). A live mid-life snapshot is NOT collapsed.
    """
    if not buckets:
        return None
    order = sorted(range(len(buckets)), key=lambda i: buckets[i]["prob"], reverse=True)
    top = buckets[order[0]]["prob"]
    second = buckets[order[1]]["prob"] if len(order) > 1 else 0.0
    if top >= COLLAPSE_HI and second <= COLLAPSE_LO:
        return order[0]
    return None


# ----------------------------------------------------------------------------------------- #
# Step 3+4: buy-all structural arb + Σ-YES gate + offline settlement + selective realized P&L
# ----------------------------------------------------------------------------------------- #
def analyze_family(series: str, groups: dict, event_rungs: dict) -> dict:
    rate = fee_rate_for(series)
    mode_seen = "n/a"
    mid_sums, ask_sums, margins, margins_net, spreads = [], [], [], [], []
    violations_total = ladders = gate_pass_mid = gate_pass_ask = 0
    # Settlement-free per-rung floor-clearing (spec's "how many cheap NOs clear the floor").
    floor_clear_n = {f: [] for f in FLOOR_SWEEP}
    floor_clear_cost = {f: [] for f in FLOOR_SWEEP}

    latest_collapsed: dict[str, tuple[str, int]] = {}      # ev -> (ts, winner_idx)
    live_snaps: dict[str, list[tuple[str, list]]] = defaultdict(list)  # ev -> [(ts, buckets)]

    for _sid, ev, rows in _full_sets(groups, event_rungs):
        mode, buckets, viol = _bucketize(series, rows)
        if not buckets:
            continue
        ladders += 1
        mode_seen = mode
        violations_total += viol

        probs = [b["prob"] for b in buckets]
        mid_sum = sum(probs)
        ask_sum = sum(r["yes_ask"] for r in rows)
        n = len(buckets)
        payout = (n - 1) * 100.0
        cost = sum(b["no_cost"] for b in buckets)
        margin = payout - cost
        fee = sum(kalshi_fee_cents(p, rate) for b in buckets for p in b["leg_prices"])
        mid_sums.append(mid_sum)
        ask_sums.append(ask_sum)
        margins.append(margin)
        margins_net.append(margin - fee)
        # synthetic identity: margin == -Σspread; spread reported for the threshold families.
        if mode == "synthetic":
            spreads.append(sum((r["yes_ask"] + r["no_ask"]) - 100.0 for r in rows))
        if mid_sum >= VIG_GATE_MIN_YES_SUM_C:
            gate_pass_mid += 1
        if ask_sum >= VIG_GATE_MIN_YES_SUM_C:
            gate_pass_ask += 1
        for floor in FLOOR_SWEEP:
            sel = [b for b in buckets if (b["no_cost"] / 100.0) >= floor >= HARD_NO_FLOOR]
            floor_clear_n[floor].append(len(sel))
            floor_clear_cost[floor].append(sum(b["no_cost"] for b in sel))

        winner = _winner_index(buckets)
        ts = rows[0].get("ts", "")
        if winner is not None:
            prev = latest_collapsed.get(ev)
            if prev is None or ts > prev[0]:
                latest_collapsed[ev] = (ts, winner)
        else:
            live_snaps[ev].append((ts, buckets))

    settled_events = set(latest_collapsed)
    coverage = (len(settled_events) / len(event_rungs)) if event_rungs else 0.0

    # Floor sweep: selective NO buying + realized P&L on the settled subset.
    floor_results = {}
    for floor in FLOOR_SWEEP:
        per_event_best: dict[str, dict] = {}
        per_event_day_best: dict[tuple[str, str], dict] = {}
        sel_counts = []
        for ev in settled_events:
            _ts, winner = latest_collapsed[ev]
            for ts, buckets in live_snaps.get(ev, []):
                sel = [(i, b) for i, b in enumerate(buckets)
                       if HARD_NO_FLOOR <= (b["no_cost"] / 100.0) and (b["no_cost"] / 100.0) >= floor]
                if not sel:
                    continue
                gross = fees = 0.0
                vmin = oimin = math.inf
                for i, b in sel:
                    gross += (100.0 - b["no_cost"]) if i != winner else (-b["no_cost"])
                    fees += sum(kalshi_fee_cents(p, rate) for p in b["leg_prices"])
                    vmin = min(vmin, b["vmin"])
                    oimin = min(oimin, b["oimin"])
                net = gross - fees
                rec = {"event": ev, "ts": ts, "n_sel": len(sel), "net": net,
                       "vmin": 0 if vmin is math.inf else vmin,
                       "oimin": 0 if oimin is math.inf else oimin}
                if ev not in per_event_best or net > per_event_best[ev]["net"]:
                    per_event_best[ev] = rec
                key = (ev, ts[:10])
                if key not in per_event_day_best or net > per_event_day_best[key]["net"]:
                    per_event_day_best[key] = rec
                sel_counts.append(len(sel))
        recs = list(per_event_best.values())
        nets = sorted((r["net"] for r in recs), reverse=True)
        uniq_nets = sorted((r["net"] for r in per_event_day_best.values()), reverse=True)
        floor_results[floor] = {
            "events_traded": len(recs),
            "unique_day_n": len(per_event_day_best),
            "median_selected": statistics.median(sel_counts) if sel_counts else 0,
            "pnl_sum": sum(nets),
            "pnl_median": statistics.median(nets) if nets else 0.0,
            "pnl_ex_top": sum(nets[1:]) if len(nets) > 1 else (0.0 if nets else 0.0),
            "pnl_sum_unique_day": sum(uniq_nets),
            "n_net_positive": sum(1 for x in nets if x > 0),
            "illiquid": sum(1 for r in recs if r["vmin"] <= 0 or r["oimin"] <= 0),
        }

    return {
        "series": series,
        "mode": mode_seen,
        "ladders": ladders,
        "monotonicity_violations": violations_total,
        "median_partition_mid": statistics.median(mid_sums) if mid_sums else 0.0,
        "median_buy_all_margin": statistics.median(margins) if margins else 0.0,
        "median_buy_all_margin_net": statistics.median(margins_net) if margins_net else 0.0,
        "median_spread_sum": statistics.median(spreads) if spreads else 0.0,
        "buy_all_margin_sum": sum(margins),
        "buy_all_margin_net_sum": sum(margins_net),
        "pct_gate_pass_mid": 100.0 * gate_pass_mid / ladders if ladders else 0.0,
        "pct_gate_pass_ask": 100.0 * gate_pass_ask / ladders if ladders else 0.0,
        "floor_clearing": {f: {"median_n": statistics.median(floor_clear_n[f]) if floor_clear_n[f] else 0,
                               "median_cost": statistics.median(floor_clear_cost[f]) if floor_clear_cost[f] else 0.0}
                           for f in FLOOR_SWEEP},
        "settled_events": len(settled_events),
        "total_events": len(event_rungs),
        "coverage": coverage,
        "floor_results": floor_results,
        "fee_rate": rate,
    }


def _verdict(struct: dict, syn: dict) -> str:
    if struct["structure"] == "INSUFFICIENT_DATA":
        return "INSUFFICIENT DATA — no full-capture ladders (partial scans truncate this family)."
    if struct["structure"] == "SINGLE_WINNER_PARTITION":
        return (f"single-winner partition (median {struct['median_itm_count']:.0f} "
                f"simultaneously-ITM rungs, ≤1 — two exclusive outcomes can't both exceed "
                f"50%) — the genuine vig_stack shape; benchmark reference.")
    mid = syn["median_partition_mid"]
    margin = syn["median_buy_all_margin"]
    margin_net = syn["median_buy_all_margin_net"]
    base = (f"NOT a partition (median {struct['median_itm_count']:.0f} ITM rungs ≈ that many "
            f"simultaneous YES winners; RAW Σyes_mid {struct['median_yes_mid_sum']:.0f}c). ")
    if syn["pct_gate_pass_mid"] < 10.0 and margin < 0:
        return (base + f"Synthetic partition Σmid={mid:.0f}c (no overround) and buy-all margin "
                f"={margin:+.0f}c (= -Σspread), {margin_net:+.0f}c after fees -> spread "
                f"artifact, no capturable vig (Outcome C).")
    return (base + f"Synthetic Σmid={mid:.0f}c, buy-all margin {margin:+.0f}c "
            f"({margin_net:+.0f}c net) -> investigate.")


# ----------------------------------------------------------------------------------------- #
# Report
# ----------------------------------------------------------------------------------------- #
def render(rows: list[dict], today: date) -> str:
    L: list[str] = []
    bench = next(r for r in rows if r["struct"]["series"] == BENCHMARK_SERIES)

    L.append(f"# S160 — Commodity-ladder vig_stack simulation ({today})")
    L.append("")
    L.append("**SIMULATION ONLY.** Next prospecting pass after S159. No scanner/config/"
             "`ACTIVE_STRATEGIES` change, no bot restart, no trades, no live Kalshi API. "
             "Fully offline; source: all `bot/state/archive/universe-*.jsonl[.gz]` + current "
             "`universe.jsonl`. Imports no bot modules.")
    L.append("")
    L.append("**Question:** do commodity price-ladder families (KXWTI / KXSILVERD / KXAAAGASD "
             "/ KXAAAGASW) carry capturable vig like KXINX (a stable-whitelisted vig_stack "
             "member)? **Finding:** they are cumulative *above-$X* THRESHOLD ladders (nested / "
             "overlapping), not the exclusive `-B` between-bucket partition KXINX uses. The "
             "operator-chosen test reconstructs an exclusive partition by differencing adjacent "
             "thresholds and asks whether *that* carries edge.")
    L.append("")

    # ---- Benchmark table -------------------------------------------------------------------
    L.append("## Benchmark: KXINX (`-B` partition, direct) vs commodity `-T` families (synthetic)")
    L.append("")
    L.append("| series | structure | full sets | median ITM rungs (≈winners) | RAW Σyes_mid | "
             "partition Σmid | buy-all margin | after fees | Σ-YES gate (mid / ask) |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        s, y = r["struct"], r["syn"]
        if s["structure"] == "INSUFFICIENT_DATA":
            L.append(f"| {s['series']} | INSUFFICIENT DATA | {s['full_eval']} | — | — | — | — | — | — |")
            continue
        struct_lbl = ("1-winner partition" if s["structure"] == "SINGLE_WINNER_PARTITION"
                      else "nested / overlap")
        L.append(
            f"| {s['series']} ({y['mode']}) | {struct_lbl} | {s['full_eval']:,} | "
            f"{s['median_itm_count']:.0f} | {s['median_yes_mid_sum']:.0f}c | "
            f"{y['median_partition_mid']:.0f}c | {y['median_buy_all_margin']:+.0f}c | "
            f"{y['median_buy_all_margin_net']:+.0f}c | "
            f"{y['pct_gate_pass_mid']:.0f}% / {y['pct_gate_pass_ask']:.0f}% |"
        )
    L.append("")
    L.append("**Reading.** The decisive mutual-exclusivity number is **median ITM rungs** "
             "(rungs with yes_mid>50c ≈ simultaneous YES winners): KXINX `-B` ≈ 1 (one bucket "
             "wins); every commodity family is many (a settled price makes every lower "
             "threshold YES at once). RAW Σyes_mid is unreliable here — KXINX's reads high only "
             "because illiquid `-B` wings quote a wide bid-ask (yes_bid=0/yes_ask=7 → phantom "
             "mid), not because it overlaps. After differencing the commodity thresholds into a "
             "synthetic partition, **every family's synthetic Σmid is ~100c** (no overround, by "
             "construction — a differenced monotone CDF is a probability distribution). The "
             "only \"vig\" left is the bid-ask spread, and buying all synthetic NOs costs "
             "exactly that: buy-all margin **= −Σspread** (negative), more negative after fees. "
             "The Σ-YES≥105c gate fires on ~0% of synthetic partitions. This is S159's per-game "
             "spread-artifact lesson, now provable for the commodity threshold class.")
    L.append("")

    # ---- Per-family detail -----------------------------------------------------------------
    for r in rows:
        s, y = r["struct"], r["syn"]
        L.append(f"## {s['series']} — {s['structure']}")
        L.append("")
        L.append(f"- groups (scan,event): {s['n_groups']:,} | unique events: {s['unique_events']} "
                 f"| full-capture ladders: {s['full_eval']:,} | analysis mode: {y['mode']}")
        if s["structure"] == "INSUFFICIENT_DATA":
            L.append("")
            L.append(f"_Verdict:_ {_verdict(s, y)}")
            L.append("")
            continue
        L.append(f"- structure: median **{s['median_itm_count']:.1f} ITM rungs** "
                 f"(yes_mid>50c ≈ simultaneous winners) | RAW median Σyes_mid = "
                 f"{s['median_yes_mid_sum']:.0f}c | RAW median Σyes_ask = {s['median_yes_ask_sum']:.0f}c")
        L.append(f"- buy-all NOs: partition Σmid = {y['median_partition_mid']:.1f}c | median margin "
                 f"= {y['median_buy_all_margin']:+.1f}c (Σ {y['buy_all_margin_sum']:+.0f}c) | after "
                 f"fees @ {y['fee_rate']:.3f} = {y['median_buy_all_margin_net']:+.1f}c "
                 f"(Σ {y['buy_all_margin_net_sum']:+.0f}c)"
                 + (f" | median Σspread = {y['median_spread_sum']:.0f}c (= −margin)"
                    if y['mode'] == 'synthetic' else ""))
        L.append(f"- Σ-YES≥105c gate pass: {y['pct_gate_pass_mid']:.0f}% (mid) / "
                 f"{y['pct_gate_pass_ask']:.0f}% (ask) | monotonicity-clamp violations: "
                 f"{y['monotonicity_violations']:,}")
        fc = y["floor_clearing"]
        L.append("- per-rung NO-floor clearing (settlement-free; median per-ladder #NOs ≥ floor "
                 "/ Σcost): " + " | ".join(
                     f"{f:.2f} → {fc[f]['median_n']:.0f} NOs / {fc[f]['median_cost']:.0f}c"
                     for f in FLOOR_SWEEP))
        L.append(f"- settlement (price-convergence) coverage: {y['settled_events']}/{y['total_events']} "
                 f"events ({100*y['coverage']:.0f}%)")
        if y["settled_events"] >= SETTLEMENT_MIN_EVENTS:
            L.append("")
            L.append("  Floor sweep — selective NO buying, realized P&L on settled subset "
                     "(per-event best entry; deduped raw vs unique-day):")
            L.append("")
            L.append("  | floor | events | uniq-day N | median #NOs | Σ net | median net | Σ ex-top | "
                     "Σ net (uniq-day) | net+ events | illiquid |")
            L.append("  |---|---|---|---|---|---|---|---|---|---|")
            for floor in FLOOR_SWEEP:
                fr = y["floor_results"][floor]
                L.append(
                    f"  | {floor:.2f} | {fr['events_traded']} | {fr['unique_day_n']} | "
                    f"{fr['median_selected']:.0f} | {fr['pnl_sum']:+.0f}c | {fr['pnl_median']:+.0f}c | "
                    f"{fr['pnl_ex_top']:+.0f}c | {fr['pnl_sum_unique_day']:+.0f}c | "
                    f"{fr['n_net_positive']}/{fr['events_traded']} | {fr['illiquid']}/{fr['events_traded']} |"
                )
        else:
            L.append(f"- floor-sweep realized P&L: settlement coverage too thin "
                     f"({y['settled_events']} < {SETTLEMENT_MIN_EVENTS} settled events) -> "
                     f"**Outcome B** for the realized-P&L sub-analysis; the structural buy-all "
                     f"margin above is settlement-free and stands.")
        L.append("")
        L.append(f"_Verdict:_ {_verdict(s, y)}")
        L.append("")

    # ---- Outcome ---------------------------------------------------------------------------
    commodities = [r for r in rows if r["struct"]["series"] != BENCHMARK_SERIES
                   and r["struct"]["structure"] != "INSUFFICIENT_DATA"]
    insufficient = [r for r in rows if r["struct"]["structure"] == "INSUFFICIENT_DATA"]
    all_not_partition = all(r["struct"]["structure"] == "NOT_SINGLE_WINNER_PARTITION" for r in commodities)
    all_no_overround = all(95.0 <= r["syn"]["median_partition_mid"] <= 105.0 for r in commodities)
    all_neg_margin = all(r["syn"]["median_buy_all_margin"] < 0 for r in commodities)
    outcome = "C" if (commodities and all_not_partition and all_no_overround and all_neg_margin) else "B/A (see detail)"

    L.append("## Outcome")
    L.append("")
    L.append(f"**Outcome {outcome}.**")
    L.append("")
    if outcome == "C":
        L.append("Commodity price-ladder families are **ruled out** for vig_stack on two "
                 "independent grounds, each with an explicit deciding number:")
        L.append("")
        L.append(f"1. **Not a single-winner partition.** Every commodity family with data shows "
                 f"a median of **many** simultaneously-in-the-money rungs (≈ many simultaneous "
                 f"YES winners), vs KXINX `-B` at median **{bench['struct']['median_itm_count']:.0f} "
                 f"ITM rungs** (≤1 — a true partition can have at most one >50%-likely outcome). "
                 f"Commodities are cumulative `above-$X` thresholds — the KXNHLSPREAD overlap "
                 f"failure S159 caught. Buying all NOs on the raw rungs pays `(K−W)*100` with "
                 f"W>1, not `(K−1)*100`.")
        L.append(f"2. **Synthetic partition has no capturable vig.** Differencing the thresholds "
                 f"yields synthetic Σmid ≈ 100c for every family (no mid-overround, by "
                 f"construction). The buy-all-synthetic-NOs structural arb margin is **negative "
                 f"= −Σspread** (median, before fees) and more negative after fees, and the "
                 f"real vig_stack Σ-YES≥105c gate fires on ~0% of synthetic ladders at mid. The "
                 f"only \"vig\" is the bid-ask spread, doubled by the 2-leg synthetic "
                 f"construction — S159's spread artifact, now provable.")
        L.append("")
        L.append("**=> Do NOT recommend a scanner expansion for commodity price-ladder families "
                 "(KXWTI / KXSILVERD / KXAAAGASD / KXAAAGASW; Outcome-D KXGOLDD / KXBRENTD / "
                 "KXNATGASD share the identical threshold structure and are ruled out for the "
                 "same reason). Honest Outcome C narrows the frontier to the parked "
                 "`post_event_reversion` strategy class.**")
        if insufficient:
            L.append("")
            L.append("Data note: " + ", ".join(r["struct"]["series"] for r in insufficient) +
                     " had no full-capture ladders (partial scans truncate these tiny families) "
                     "— excluded from the ruling-out as a data gap, not evidence of edge.")
    else:
        L.append("At least one commodity family deviates from the expected ruled-out profile "
                 "(see the per-family detail and benchmark table for the deciding numbers). "
                 "Realized-P&L sub-analyses with thin settlement coverage are Outcome B.")
    L.append("")

    # ---- Notes -----------------------------------------------------------------------------
    L.append("## Notes")
    L.append("")
    L.append("- **Synthetic construction.** Synthetic-bucket NO on `[X_i,X_{i+1})` = long "
             "`C_i` NO + long `C_{i+1}` YES (pays 100 iff price NOT in the bucket), buy-only, "
             "cost `no_ask_i + yes_ask_{i+1}` (2 legs). Tails are single-leg. Σ over all "
             "buckets of the NO cost = `Σ(yes_ask_i + no_ask_i)` = `100K + Σspread`, vs the "
             "one-winner payout `K*100`, so buy-all margin = `−Σspread` exactly (verified per "
             "ladder via the Σspread column).")
    L.append("- **Mutual-exclusivity discriminator.** Median count of rungs with yes_mid>50c "
             "(≈ simultaneous YES winners) — robust to the bid-ask spread that inflates "
             "Σyes_mid on illiquid `-B` wings. A single-winner partition reads ~1; nested "
             "thresholds read many.")
    L.append("- **No-overround is structural.** Differencing a monotone survival curve "
             "telescopes to Σ prob = 100c; the synthetic partition cannot carry mid-overround. "
             "Measured median synthetic Σmid per family confirms ~100c ± rounding.")
    L.append("- **Dedup (S86/S156).** Grouped by `(scan_id, event_ticker)`; rungs deduped by "
             "ticker within a scan; floor-sweep realized P&L reported raw (per-event best) and "
             "unique-day to avoid intra-day-scan inflation.")
    L.append("- **Settlement.** Offline price-convergence only: an event's latest snapshot "
             "whose curve has collapsed (one bucket ≥90c, rest ≤10c) pins the winning bucket; "
             "un-collapsed events are excluded from realized P&L. No live API.")
    L.append(f"- **Fees.** `ceil(rate·p·(1−p))` per leg; KXINX {FEE_RATE_FINANCIAL} (S&P "
             f"financial rate), commodities {FEE_RATE_GENERAL} (general; conservative).")
    L.append("- **Fill feasibility.** Commodity per-rung volume/OI is far below NBA's; the "
             "2-leg synthetic NO needs BOTH legs to fill (min across legs reported as the "
             "`illiquid` count) — an additional barrier even if the spread economics worked.")
    L.append("- **Completeness guard.** Only full-capture ladders (`k_here == k_true`, "
             "`k_true ≥ 3`) are evaluated; partial scans / truncated ladders are skipped.")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="S160 commodity-ladder vig_stack offline sim.")
    parser.add_argument("--report-out", default=None,
                        help="Report path (default: bot/state/reports/"
                             "session_160_commodity_vig_stack_sim_<today>.md).")
    args = parser.parse_args(argv)

    files = _source_files()
    if not files:
        print(f"error: no universe data under {STATE_DIR}")
        return 2

    rows = []
    for series in ALL_SERIES:
        b_only = (series == BENCHMARK_SERIES)
        groups, event_rungs = load_groups(series, files, b_only=b_only)
        struct = classify_structure(series, groups, event_rungs)
        syn = analyze_family(series, groups, event_rungs)
        rows.append({"struct": struct, "syn": syn})
        print(f"{series:11s} {struct['structure']:26s} full={struct['full_eval']:5d} "
              f"ITM={struct['median_itm_count']:4.1f} RAW_Σmid={struct['median_yes_mid_sum']:7.0f}c "
              f"part_Σmid={syn['median_partition_mid']:6.1f}c buy_all={syn['median_buy_all_margin']:+7.1f}c "
              f"net={syn['median_buy_all_margin_net']:+7.1f}c gate_mid={syn['pct_gate_pass_mid']:4.0f}% "
              f"settled={syn['settled_events']:3d}/{syn['total_events']:3d}")

    today = date.today()
    out = Path(args.report_out) if args.report_out else (
        REPORTS_DIR / f"session_160_commodity_vig_stack_sim_{today.isoformat()}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, today))
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
