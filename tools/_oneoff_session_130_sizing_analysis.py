"""Session 130 one-off: live_momentum sizing-on-losses investigation.

Tests whether realized Kelly fraction / entry price / confidence / DQS correlate
with outcome on the post-Apr-23 live_momentum cohort. NO production code or
state is mutated; this script only reads paper_trades.json and prints tables.

Reproducible: rerun any time the cohort has grown. Safe to delete after S130
ships if no follow-up sizing session is queued.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable

TRADES_PATH = Path(__file__).resolve().parent.parent / "bot" / "state" / "paper_trades.json"

COHORT_START = "2026-04-23"
BANKROLL_BUMP_DATE = "2026-04-29"  # PAPER_STARTING_BALANCE bumped from $500 to $10,500
S54_FIX_DATE = "2026-05-05"        # sizer call site fixed to use PAPER_STARTING_BALANCE
PAPER_STARTING_BALANCE_PRE = 500.0
PAPER_STARTING_BALANCE_POST = 10_500.0
LM_STATUS_KEEP = {"won", "lost", "exited_early"}
CAP_SWEEP = [0.01, 0.02, 0.03, 0.05, 0.10, 0.15, 0.20]


def starting_balance_for(ts: str) -> float:
    return PAPER_STARTING_BALANCE_PRE if ts < BANKROLL_BUMP_DATE else PAPER_STARTING_BALANCE_POST


def resolved_pnl(trade: dict) -> float:
    s = trade.get("status")
    if s in {"won", "lost", "exited_early"}:
        return float(trade.get("pnl", 0.0))
    return 0.0


def reconstruct_bankroll_at_entry(all_trades: list[dict], target: dict) -> float:
    """Bankroll = starting_balance(target.timestamp) + sum(realized_pnl of trades resolved BEFORE target.timestamp).

    "Resolved before" = trade.resolved_at < target.timestamp (when resolved_at exists),
    else trade.timestamp < target.timestamp as fallback for non-resolved trades (which contribute 0 anyway).
    """
    start = starting_balance_for(target["timestamp"])
    cutoff = target["timestamp"]
    pnl_so_far = 0.0
    for t in all_trades:
        if t is target:
            continue
        resolve_ts = t.get("resolved_at") or t.get("timestamp", "")
        if resolve_ts and resolve_ts < cutoff:
            pnl_so_far += resolved_pnl(t)
    return start + pnl_so_far


def quartile_bin(value: float, edges: list[float]) -> int:
    """0..3 for quartile membership. edges must be [q1, q2, q3]."""
    if value <= edges[0]:
        return 0
    if value <= edges[1]:
        return 1
    if value <= edges[2]:
        return 2
    return 3


def quartile_edges(values: list[float]) -> list[float]:
    if len(values) < 4:
        return [0.0, 0.0, 0.0]
    s = sorted(values)
    return [
        s[int(0.25 * (len(s) - 1))],
        s[int(0.50 * (len(s) - 1))],
        s[int(0.75 * (len(s) - 1))],
    ]


def entry_price_band(p: float) -> str:
    if p < 0.60:
        return "<60c"
    if p < 0.70:
        return "60-69c"
    if p < 0.80:
        return "70-79c"
    if p < 0.90:
        return "80-89c"
    return ">=90c"


def confidence_band(c: float | None) -> str | None:
    if c is None:
        return None
    if c < 0.25:
        return "0.00-0.25"
    if c < 0.50:
        return "0.25-0.50"
    if c < 0.75:
        return "0.50-0.75"
    return "0.75-1.00"


def winrate(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


def p95_loss(pnls: list[float]) -> float:
    """5th percentile (most negative tail). Returns 0 if no losses present."""
    if not pnls:
        return 0.0
    s = sorted(pnls)
    idx = max(0, int(0.05 * (len(s) - 1)))
    return s[idx]


def fmt_band_row(label: str, pnls: list[float]) -> str:
    if not pnls:
        return f"  {label:>12}  N=0"
    return (
        f"  {label:>12}  N={len(pnls):>3}  WR={winrate(pnls):.2%}  "
        f"mean=${statistics.mean(pnls):+7.2f}  "
        f"median=${statistics.median(pnls):+7.2f}  "
        f"p95_loss=${p95_loss(pnls):+7.2f}  "
        f"total=${sum(pnls):+8.2f}"
    )


def print_band_table(title: str, groups: dict[str, list[float]], min_n: int = 8) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    skipped = []
    for label in sorted(groups.keys()):
        pnls = groups[label]
        if len(pnls) < min_n:
            skipped.append(f"{label} (N={len(pnls)})")
            continue
        print(fmt_band_row(label, pnls))
    if skipped:
        print(f"  [skipped <N={min_n}: {', '.join(skipped)}]")


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def rank(xs: list[float]) -> list[float]:
    """Average-rank for ties."""
    indexed = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and xs[indexed[j + 1]] == xs[indexed[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    return pearson(rank(xs), rank(ys))


def main() -> None:
    all_trades = json.loads(TRADES_PATH.read_text())
    cohort = [
        t for t in all_trades
        if t.get("type") == "live_momentum"
        and t.get("status") in LM_STATUS_KEEP
        and t.get("timestamp", "") >= COHORT_START
    ]
    cohort.sort(key=lambda t: t["timestamp"])

    # Derive per-trade fields.
    rows = []
    for t in cohort:
        contracts = float(t.get("contracts", 0) or 0)
        entry = float(t.get("entry_price", 0.0) or 0.0)
        pnl = float(t.get("pnl", 0.0) or 0.0)
        notional = contracts * entry
        bankroll = reconstruct_bankroll_at_entry(all_trades, t)
        if bankroll <= 0:
            kelly_frac = float("nan")
        else:
            kelly_frac = notional / bankroll
        conf = t.get("confidence")
        if conf == 0 or conf is None:
            conf_val = None  # treat 0 as "not populated" (pre-S50)
        else:
            conf_val = float(conf)
        dqs = t.get("dqs")
        dqs_val = float(dqs) if dqs is not None else None
        sport = t.get("sport") or "?"
        rows.append({
            "timestamp": t["timestamp"],
            "ticker": t.get("ticker"),
            "status": t["status"],
            "sport": sport,
            "contracts": contracts,
            "entry_price": entry,
            "pnl": pnl,
            "notional": notional,
            "bankroll": bankroll,
            "kelly_frac": kelly_frac,
            "confidence": conf_val,
            "dqs": dqs_val,
            "is_post_s54": t["timestamp"] >= S54_FIX_DATE,
        })

    n_full = len(rows)
    n_post_apr29 = sum(1 for r in rows if r["timestamp"] >= BANKROLL_BUMP_DATE)
    n_post_s54 = sum(1 for r in rows if r["is_post_s54"])
    print("=" * 70)
    print("SESSION 130 — live_momentum sizing-on-losses investigation")
    print("=" * 70)
    print(f"Cohort source: {TRADES_PATH}")
    print(f"Filter: type=='live_momentum' AND status in {sorted(LM_STATUS_KEEP)} AND timestamp >= {COHORT_START}")
    print(f"N: full={n_full}, post-Apr-29={n_post_apr29}, post-S54={n_post_s54}")
    print()
    print(f"Total cohort PnL: ${sum(r['pnl'] for r in rows):+.2f}")
    print(f"Status mix: " + ", ".join(
        f"{s}={sum(1 for r in rows if r['status']==s)}"
        for s in ("won", "lost", "exited_early")
    ))

    # --- Table A: kelly_band quartile ---
    full_kf = [r["kelly_frac"] for r in rows if not math.isnan(r["kelly_frac"])]
    edges_full = quartile_edges(full_kf)
    print(f"\nKelly-fraction quartile edges (full cohort): "
          f"Q1<={edges_full[0]:.4f}  Q2<={edges_full[1]:.4f}  Q3<={edges_full[2]:.4f}")
    groups_a_full: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if math.isnan(r["kelly_frac"]):
            continue
        q = quartile_bin(r["kelly_frac"], edges_full)
        groups_a_full[f"Q{q+1}"].append(r["pnl"])
    print_band_table("TABLE A.1 — kelly_band quartile (FULL N=69)", groups_a_full)

    post_s54_rows = [r for r in rows if r["is_post_s54"]]
    post_s54_kf = [r["kelly_frac"] for r in post_s54_rows if not math.isnan(r["kelly_frac"])]
    edges_s54 = quartile_edges(post_s54_kf)
    print(f"\nKelly-fraction quartile edges (post-S54 cohort): "
          f"Q1<={edges_s54[0]:.4f}  Q2<={edges_s54[1]:.4f}  Q3<={edges_s54[2]:.4f}")
    groups_a_s54: dict[str, list[float]] = defaultdict(list)
    for r in post_s54_rows:
        if math.isnan(r["kelly_frac"]):
            continue
        q = quartile_bin(r["kelly_frac"], edges_s54)
        groups_a_s54[f"Q{q+1}"].append(r["pnl"])
    print_band_table("TABLE A.2 — kelly_band quartile (POST-S54 N=32)", groups_a_s54)

    # --- Table B: entry_price_band ---
    groups_b: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        groups_b[entry_price_band(r["entry_price"])].append(r["pnl"])
    print_band_table("TABLE B — entry_price_band (FULL N=69)", groups_b)

    # --- Table C: confidence_band (informational; sizer ignores confidence) ---
    groups_c: dict[str, list[float]] = defaultdict(list)
    n_c_populated = 0
    for r in rows:
        if r["confidence"] is None:
            continue
        n_c_populated += 1
        band = confidence_band(r["confidence"])
        if band:
            groups_c[band].append(r["pnl"])
    print(f"\n[Confidence populated on {n_c_populated}/{n_full} trades — pre-S50 trades have confidence=0/None]")
    print_band_table("TABLE C — confidence_band (informational; sizer uses constant 0.75)", groups_c)

    # --- Table D: dqs_band quartile ---
    dqs_values = [r["dqs"] for r in rows if r["dqs"] is not None]
    n_d_populated = len(dqs_values)
    if n_d_populated >= 4:
        dqs_edges = quartile_edges(dqs_values)
        print(f"\n[DQS populated on {n_d_populated}/{n_full} trades; quartile edges: "
              f"Q1<={dqs_edges[0]:.3f}  Q2<={dqs_edges[1]:.3f}  Q3<={dqs_edges[2]:.3f}]")
        groups_d: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            if r["dqs"] is None:
                continue
            q = quartile_bin(r["dqs"], dqs_edges)
            groups_d[f"Q{q+1}"].append(r["pnl"])
        print_band_table("TABLE D — dqs_band quartile (informational; sizer ignores DQS)", groups_d)
    else:
        print(f"\n[DQS populated on {n_d_populated}/{n_full} — too thin for quartile table]")

    # --- Table E: sport x kelly_band (post-S54) ---
    print("\nTABLE E — sport × kelly_band (POST-S54 N=32; cells with N<3 shown but flagged)")
    print("-" * 68)
    sport_cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    sports_in_s54 = sorted({r["sport"] for r in post_s54_rows})
    for r in post_s54_rows:
        if math.isnan(r["kelly_frac"]):
            continue
        q = quartile_bin(r["kelly_frac"], edges_s54)
        sport_cells[(r["sport"], f"Q{q+1}")].append(r["pnl"])
    header = "  sport   " + "   ".join(f"  Q{i+1}        " for i in range(4))
    print(header)
    for sport in sports_in_s54:
        cells = []
        for q in range(4):
            pnls = sport_cells.get((sport, f"Q{q+1}"), [])
            if not pnls:
                cells.append("   --       ")
            elif len(pnls) < 3:
                cells.append(f"N={len(pnls)} thin*")
            else:
                cells.append(f"N={len(pnls)} ${statistics.mean(pnls):+5.2f}")
        print(f"  {sport:<7}  " + "  ".join(cells))
    print("  [thin* = N<3, do not interpret]")

    # --- Correlation ---
    print("\nCORRELATION — kelly_frac vs pnl")
    print("-" * 30)
    full_pairs = [(r["kelly_frac"], r["pnl"]) for r in rows if not math.isnan(r["kelly_frac"])]
    xs_f = [p[0] for p in full_pairs]
    ys_f = [p[1] for p in full_pairs]
    print(f"  full N={len(full_pairs)}:    pearson={pearson(xs_f, ys_f):+.3f}   spearman={spearman(xs_f, ys_f):+.3f}")
    s54_pairs = [(r["kelly_frac"], r["pnl"]) for r in post_s54_rows if not math.isnan(r["kelly_frac"])]
    xs_s = [p[0] for p in s54_pairs]
    ys_s = [p[1] for p in s54_pairs]
    print(f"  post-S54 N={len(s54_pairs)}: pearson={pearson(xs_s, ys_s):+.3f}   spearman={spearman(xs_s, ys_s):+.3f}")

    # --- Counterfactual cap sweep ---
    def cap_sweep(rows_in: list[dict], label: str) -> None:
        print(f"\nCOUNTERFACTUAL CAP SWEEP — {label} (N={len(rows_in)})")
        print("-" * 68)
        uncapped_total = sum(r["pnl"] for r in rows_in)
        print(f"  uncapped total_pnl = ${uncapped_total:+.2f}")
        print(f"  {'cap':>5}  {'n_capped':>8}  {'cohort_pnl':>12}  {'Δ vs uncap':>12}  {'Δ/trade':>10}")
        for cap in CAP_SWEEP:
            total = 0.0
            n_capped = 0
            for r in rows_in:
                if r["bankroll"] <= 0 or r["entry_price"] <= 0 or r["contracts"] <= 0:
                    total += r["pnl"]
                    continue
                cap_dollars = cap * r["bankroll"]
                capped_contracts = math.floor(cap_dollars / r["entry_price"])
                capped_contracts = min(int(r["contracts"]), capped_contracts)
                if capped_contracts < r["contracts"]:
                    n_capped += 1
                # Proportional rescale: in binary 0/1 markets, dollar P&L scales linearly with contract count.
                if r["contracts"] == 0:
                    scaled = 0.0
                else:
                    scaled = r["pnl"] * (capped_contracts / r["contracts"])
                total += scaled
            delta = total - uncapped_total
            per_trade = delta / len(rows_in) if rows_in else 0.0
            flag = " <-- ACTION" if per_trade >= 1.00 else ""
            print(f"  {cap*100:>4.0f}%  {n_capped:>8}  ${total:+11.2f}  ${delta:+11.2f}  ${per_trade:+9.2f}{flag}")

    cap_sweep(rows, "FULL cohort N=69")
    cap_sweep(post_s54_rows, "POST-S54 cohort N=32")

    # --- Per-trade dump for spot-checking ---
    print("\nPER-TRADE DUMP (sorted by kelly_frac desc, top 12)")
    print("-" * 110)
    print(f"  {'timestamp':<26}  {'sport':<5}  {'status':<13}  {'contracts':>3}  {'entry':>5}  "
          f"{'pnl':>7}  {'notional':>8}  {'bankroll':>9}  {'kelly%':>7}")
    by_kf = sorted([r for r in rows if not math.isnan(r["kelly_frac"])], key=lambda r: r["kelly_frac"], reverse=True)
    for r in by_kf[:12]:
        print(f"  {r['timestamp']:<26}  {r['sport']:<5}  {r['status']:<13}  "
              f"{int(r['contracts']):>3}  ${r['entry_price']:.2f}  ${r['pnl']:+6.2f}  "
              f"${r['notional']:>7.2f}  ${r['bankroll']:>8.2f}  {r['kelly_frac']*100:>6.2f}%")
    print("\nPER-TRADE DUMP (sorted by pnl asc — biggest losses first, top 12)")
    print("-" * 110)
    by_loss = sorted(rows, key=lambda r: r["pnl"])
    for r in by_loss[:12]:
        kf = "nan" if math.isnan(r["kelly_frac"]) else f"{r['kelly_frac']*100:.2f}%"
        print(f"  {r['timestamp']:<26}  {r['sport']:<5}  {r['status']:<13}  "
              f"{int(r['contracts']):>3}  ${r['entry_price']:.2f}  ${r['pnl']:+6.2f}  "
              f"${r['notional']:>7.2f}  ${r['bankroll']:>8.2f}  {kf:>7}")

    # --- Subgroup audit: pre-S54 buggy bankroll period notes ---
    pre_apr29 = [r for r in rows if r["timestamp"] < BANKROLL_BUMP_DATE]
    bug_window = [r for r in rows if BANKROLL_BUMP_DATE <= r["timestamp"] < S54_FIX_DATE]
    print(f"\nSUBGROUP AUDIT:")
    print(f"  pre-Apr-29 (true bankroll ~$500): N={len(pre_apr29)}, total_pnl=${sum(r['pnl'] for r in pre_apr29):+.2f}")
    print(f"  Apr-29 to May-4 (bug: bot used $500+pnl, true bankroll ~$10,500): N={len(bug_window)}, total_pnl=${sum(r['pnl'] for r in bug_window):+.2f}")
    print(f"  post-S54 (bot uses true bankroll): N={n_post_s54}, total_pnl=${sum(r['pnl'] for r in post_s54_rows):+.2f}")


if __name__ == "__main__":
    main()
