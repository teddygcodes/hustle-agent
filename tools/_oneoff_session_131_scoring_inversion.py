"""Session 131 — live_momentum confidence/DQS scoring-direction investigation.

Tests whether the S130 sub-finding (high-conf trades structurally lose,
mid-conf trades structurally win) is real, statistically meaningful,
and actionable — or a small-N artifact.

Cohort: type=='live_momentum', status in {won, lost, exited_early},
confidence is not None and confidence > 0 (S50 forward-only).

Analyses A–H per S131 brief. Investigation-only — no scoring/config
changes. Safe to delete after S131 ships.

Run:
    python3 tools/_oneoff_session_131_scoring_inversion.py
"""

from __future__ import annotations

import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

random.seed(131)

PAPER_TRADES_PATH = Path(__file__).resolve().parent.parent / "bot" / "state" / "paper_trades.json"

# Finer-resolution confidence/DQS bands (right edge >1.0 to include clamped 1.0 trades)
BANDS = [(0.00, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]

# Filter sweep thresholds for Analysis H
FILTER_THRESHOLDS = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90]

# Bootstrap iterations for Analysis C
BOOTSTRAP_N = 10_000


# ============================================================================
# Helpers
# ============================================================================

def load_cohort() -> list[dict]:
    """Post-S50 confidence-populated live_momentum cohort."""
    trades = json.loads(PAPER_TRADES_PATH.read_text())
    return [
        t for t in trades
        if t.get("type") == "live_momentum"
        and t.get("status") in ("won", "lost", "exited_early")
        and (t.get("confidence") or 0) > 0
    ]


def _pnl(trade: dict) -> float:
    return float(trade.get("pnl") or 0.0)


def _wr_status(cohort: list[dict]) -> float:
    if not cohort:
        return 0.0
    return sum(1 for t in cohort if t.get("status") == "won") / len(cohort)


def _wr_pnl(cohort: list[dict]) -> float:
    if not cohort:
        return 0.0
    return sum(1 for t in cohort if _pnl(t) > 0) / len(cohort)


def _in_band(value: float, lo: float, hi: float) -> bool:
    return lo <= value < hi


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _duration_seconds(trade: dict) -> float | None:
    ts = trade.get("timestamp")
    rs = trade.get("resolved_at")
    if not ts or not rs:
        return None
    try:
        return (datetime.fromisoformat(rs) - datetime.fromisoformat(ts)).total_seconds()
    except (ValueError, TypeError):
        return None


def _exit_category(trade: dict) -> str:
    er = trade.get("exit_reason") or ""
    prefix = er.split(":")[0].strip().upper() if er else "NONE"
    if not prefix:
        return "NONE"
    return prefix


def _actual_outcome(trade: dict) -> float:
    """Continuous outcome for Brier-style analysis.

    won → 1.0
    lost → 0.0
    exited_early → exit_price (closing price as partial outcome)
    """
    status = trade.get("status")
    if status == "won":
        return 1.0
    if status == "lost":
        return 0.0
    # exited_early: use closing price
    return float(trade.get("exit_price") or 0.0)


def _reconstruct_wp(trade: dict) -> tuple[float | None, str]:
    """Reconstruct estimated_win_prob; returns (value, source_tag).

    Sources:
      'native'                       — uses logged estimated_win_prob (post-S99)
      'backsolve_exact'              — backsolved wp_edge from confidence/dqs (confidence < 1.0)
      'backsolve_clamped_lowerbound' — confidence == 1.0 (clamped); wp_edge >= (1/dqs - 1)
                                       result is a LOWER BOUND on estimated_win_prob
      'unreconstructable'            — missing inputs

    Formula: clamp(wp_edge + leader_price, 0.05, 0.95); leader_price ≈ entry_price
    for live_momentum (we buy the leader).
    """
    native = trade.get("estimated_win_prob")
    if native is not None:
        return float(native), "native"

    confidence = trade.get("confidence")
    dqs = trade.get("dqs")
    entry_price = trade.get("entry_price")
    if confidence is None or dqs is None or entry_price is None or dqs <= 0:
        return None, "unreconstructable"

    confidence = float(confidence)
    dqs = float(dqs)
    entry_price = float(entry_price)

    if abs(confidence - 1.0) < 1e-9:
        # Clamped — wp_edge is only known as >= (1/dqs - 1)
        wp_edge_lb = max(0.0, (1.0 / dqs) - 1.0)
        ewp_lb = max(0.05, min(0.95, wp_edge_lb + entry_price))
        return ewp_lb, "backsolve_clamped_lowerbound"

    wp_edge = max(0.0, (confidence / dqs) - 1.0)
    ewp = max(0.05, min(0.95, wp_edge + entry_price))
    return ewp, "backsolve_exact"


# ============================================================================
# Header
# ============================================================================

def print_header(cohort: list[dict]) -> None:
    print("=" * 78)
    print("SESSION 131 — live_momentum scoring-direction investigation")
    print("=" * 78)
    print(f"Cohort: post-S50 live_momentum with confidence>0, status resolved")
    print(f"  N = {len(cohort)}")
    print(f"  Status mix: " + ", ".join(
        f"{s}={sum(1 for t in cohort if t.get('status') == s)}"
        for s in ("won", "lost", "exited_early")
    ))
    sports = Counter(t.get("sport") or "?" for t in cohort)
    print(f"  Sport mix: " + ", ".join(f"{s}={n}" for s, n in sports.most_common()))
    timestamps = sorted(t.get("timestamp") for t in cohort if t.get("timestamp"))
    if timestamps:
        print(f"  Date range: {timestamps[0]} → {timestamps[-1]}")
    print()


# ============================================================================
# Analysis A — Finer confidence buckets
# ============================================================================

def _print_band_table(cohort: list[dict], key: str, title: str) -> None:
    print(f"  Band            | N  | WR_pnl | WR_st | mean    | med     | p25     | p75     | total")
    print(f"  " + "-" * 90)
    for lo, hi in BANDS:
        sub = [t for t in cohort if t.get(key) is not None and _in_band(float(t[key]), lo, hi)]
        n = len(sub)
        band_label = f"[{lo:.2f}, {hi:.2f})" if hi <= 1.0 else f"[{lo:.2f}, 1.00]"
        if n < 5:
            print(f"  {band_label:<15} | {n:>2} | (N<5, skipping stable stats)")
            continue
        pnls = [_pnl(t) for t in sub]
        print(
            f"  {band_label:<15} | {n:>2} | "
            f"{_wr_pnl(sub):>6.1%} | {_wr_status(sub):>5.1%} | "
            f"${statistics.mean(pnls):>+7.2f} | ${statistics.median(pnls):>+7.2f} | "
            f"${_percentile(pnls, 0.25):>+7.2f} | ${_percentile(pnls, 0.75):>+7.2f} | "
            f"${sum(pnls):>+7.2f}"
        )


def analysis_a(cohort: list[dict]) -> None:
    print("─" * 78)
    print("ANALYSIS A — Finer confidence buckets")
    print("─" * 78)
    print("Per-band: N, WR_pnl (PnL>0), WR_st (status=='won'), mean/med/p25/p75, total")
    print()
    _print_band_table(cohort, "confidence", "Confidence")
    print()


# ============================================================================
# Analysis B — Finer DQS buckets
# ============================================================================

def analysis_b(cohort: list[dict]) -> None:
    print("─" * 78)
    print("ANALYSIS B — Finer DQS buckets")
    print("─" * 78)
    print("Same bucket structure on dqs field.")
    print()
    _print_band_table(cohort, "dqs", "DQS")
    print()


# ============================================================================
# Analysis C — Statistical significance
# ============================================================================

def _welch_t(g1: list[float], g2: list[float]) -> tuple[float, float, float]:
    """Welch's t-test. Returns (t-stat, dof, two-sided p via normal approx)."""
    n1, n2 = len(g1), len(g2)
    m1, m2 = statistics.mean(g1), statistics.mean(g2)
    v1 = statistics.variance(g1) if n1 > 1 else 0.0
    v2 = statistics.variance(g2) if n2 > 1 else 0.0
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0:
        return 0.0, 0.0, 1.0
    t = (m1 - m2) / se
    num = (v1 / n1 + v2 / n2) ** 2
    den = ((v1 / n1) ** 2 / max(n1 - 1, 1)) + ((v2 / n2) ** 2 / max(n2 - 1, 1))
    dof = num / den if den > 0 else 0.0
    # Two-sided p via normal approx (small-N + no scipy)
    p = 2 * (1 - _norm_cdf(abs(t)))
    return t, dof, p


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _mann_whitney_u(g1: list[float], g2: list[float]) -> tuple[float, float]:
    """Mann-Whitney U-test. Returns (U, two-sided p via normal approx)."""
    n1, n2 = len(g1), len(g2)
    combined = [(v, "g1") for v in g1] + [(v, "g2") for v in g2]
    combined.sort()
    # Assign ranks, handling ties via average rank
    ranks: dict[int, float] = {}
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-indexed
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    r1 = sum(ranks[k] for k, (_, g) in enumerate(combined) if g == "g1")
    u1 = r1 - n1 * (n1 + 1) / 2
    u2 = n1 * n2 - u1
    u = min(u1, u2)
    # Normal approximation
    mean_u = n1 * n2 / 2
    sd_u = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    if sd_u == 0:
        return u, 1.0
    z = (u - mean_u) / sd_u
    p = 2 * _norm_cdf(-abs(z))
    return u, p


def _bootstrap_diff_ci(g1: list[float], g2: list[float], n: int = BOOTSTRAP_N) -> tuple[float, float, float]:
    """Bootstrap diff-of-means with 95% CI. Returns (mean_diff_obs, lo, hi)."""
    obs = statistics.mean(g1) - statistics.mean(g2)
    diffs = []
    for _ in range(n):
        s1 = [random.choice(g1) for _ in range(len(g1))]
        s2 = [random.choice(g2) for _ in range(len(g2))]
        diffs.append(statistics.mean(s1) - statistics.mean(s2))
    diffs.sort()
    lo = diffs[int(0.025 * n)]
    hi = diffs[int(0.975 * n)]
    return obs, lo, hi


def analysis_c(cohort: list[dict]) -> dict:
    print("─" * 78)
    print("ANALYSIS C — Statistical significance (conf_high vs conf_low)")
    print("─" * 78)
    confs = [float(t["confidence"]) for t in cohort]
    median_conf = statistics.median(confs)
    print(f"  Cohort confidence median = {median_conf:.4f} (clamped at 1.0 — median split degenerate)")
    print(f"  Using S130-comparable split: threshold = 0.75 (matches the original sub-finding)")
    threshold = 0.75
    high = [_pnl(t) for t in cohort if float(t["confidence"]) > threshold]
    low = [_pnl(t) for t in cohort if float(t["confidence"]) <= threshold]
    if not high or not low:
        print(f"  WARNING: degenerate split (high={len(high)}, low={len(low)})")
        return {
            "threshold": threshold, "high_N": len(high), "low_N": len(low),
            "degenerate": True,
        }
    print(f"  conf_high (>0.75): N={len(high)}, mean=${statistics.mean(high):+.2f}, total=${sum(high):+.2f}")
    print(f"  conf_low  (≤0.75): N={len(low)}, mean=${statistics.mean(low):+.2f}, total=${sum(low):+.2f}")
    print(f"  Mean difference (high − low): ${statistics.mean(high) - statistics.mean(low):+.2f}")
    print()

    t, dof, p_t = _welch_t(high, low)
    u, p_u = _mann_whitney_u(high, low)
    obs, lo, hi = _bootstrap_diff_ci(high, low)

    print(f"  Welch's t-test:    t = {t:+.3f}, dof = {dof:.1f}, p (two-sided, normal-approx) = {p_t:.4f}")
    print(f"  Mann-Whitney U:    U = {u:.1f}, p (two-sided, normal-approx) = {p_u:.4f}")
    print(f"  Bootstrap diff:    mean = ${obs:+.2f}, 95% CI = (${lo:+.2f}, ${hi:+.2f})  [N={BOOTSTRAP_N} resamples]")
    excludes_zero = lo > 0 or hi < 0
    print(f"  → Bootstrap CI {'EXCLUDES' if excludes_zero else 'INCLUDES'} zero")
    print()

    # Auxiliary: clamped (=1.0) vs unclamped (<1.0) — captures the mechanism more directly
    clamped = [_pnl(t) for t in cohort if abs(float(t["confidence"]) - 1.0) < 1e-9]
    unclamped = [_pnl(t) for t in cohort if abs(float(t["confidence"]) - 1.0) >= 1e-9]
    if clamped and unclamped:
        print(f"  AUX split (clamped=1.0 vs unclamped<1.0):")
        print(f"    clamped:    N={len(clamped)}, mean=${statistics.mean(clamped):+.2f}, total=${sum(clamped):+.2f}")
        print(f"    unclamped:  N={len(unclamped)}, mean=${statistics.mean(unclamped):+.2f}, total=${sum(unclamped):+.2f}")
        aux_obs, aux_lo, aux_hi = _bootstrap_diff_ci(clamped, unclamped)
        print(f"    Bootstrap diff (clamped − unclamped): mean=${aux_obs:+.2f}, 95% CI = (${aux_lo:+.2f}, ${aux_hi:+.2f})")
        aux_excludes_zero = aux_lo > 0 or aux_hi < 0
        print(f"    → Bootstrap CI {'EXCLUDES' if aux_excludes_zero else 'INCLUDES'} zero")
    print()
    return {
        "threshold": threshold,
        "median_conf": median_conf,
        "high_mean": statistics.mean(high),
        "low_mean": statistics.mean(low),
        "diff": statistics.mean(high) - statistics.mean(low),
        "high_N": len(high), "low_N": len(low),
        "t": t, "p_t": p_t, "u": u, "p_u": p_u,
        "boot_obs": obs, "boot_lo": lo, "boot_hi": hi,
        "ci_excludes_zero": excludes_zero,
        "degenerate": False,
        "clamped_N": len(clamped),
        "unclamped_N": len(unclamped),
        "clamped_mean": statistics.mean(clamped) if clamped else None,
        "unclamped_mean": statistics.mean(unclamped) if unclamped else None,
    }


# ============================================================================
# Analysis D — Sport breakdown
# ============================================================================

def analysis_d(cohort: list[dict]) -> dict:
    print("─" * 78)
    print("ANALYSIS D — Sport breakdown of the inversion")
    print("─" * 78)
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for t in cohort:
        by_sport[t.get("sport") or "?"].append(t)
    print(f"  Per-sport split: confidence > 0.75 (high) vs ≤ 0.75 (low) — matches S130 convention.")
    print()
    print(f"  Sport     | N  | total    | mean     | high(N) mean   | low(N) mean    | inversion?")
    print(f"  " + "-" * 90)
    sport_results = {}
    for sport, sub in sorted(by_sport.items(), key=lambda x: -len(x[1])):
        n = len(sub)
        total = sum(_pnl(t) for t in sub)
        if n < 5:
            print(f"  {sport:<9} | {n:>2} | ${total:>+7.2f} | (N<5 — skipping split)")
            sport_results[sport] = {"N": n, "total": total}
            continue
        high = [_pnl(t) for t in sub if float(t["confidence"]) > 0.75]
        low = [_pnl(t) for t in sub if float(t["confidence"]) <= 0.75]
        high_mean = statistics.mean(high) if high else None
        low_mean = statistics.mean(low) if low else None
        if high_mean is None or low_mean is None:
            inverted_str = "n/a (one side empty)"
            inverted_val = None
        else:
            inverted_val = high_mean < low_mean
            inverted_str = "YES (high<low)" if inverted_val else "no"
        mean = statistics.mean([_pnl(t) for t in sub])
        high_str = f"({len(high)}) ${high_mean:+.2f}" if high_mean is not None else f"({len(high)}) ---"
        low_str = f"({len(low)}) ${low_mean:+.2f}" if low_mean is not None else f"({len(low)}) ---"
        print(
            f"  {sport:<9} | {n:>2} | ${total:>+7.2f} | ${mean:>+7.2f} | {high_str:<14} | {low_str:<14} | {inverted_str}"
        )
        sport_results[sport] = {
            "N": n, "total": total, "mean": mean,
            "high_mean": high_mean, "low_mean": low_mean,
            "inverted": inverted_val,
            "high_N": len(high), "low_N": len(low),
        }
    print()
    return sport_results


# ============================================================================
# Analysis E — Time-in-trade & exit-reason mix
# ============================================================================

def analysis_e(cohort: list[dict]) -> None:
    print("─" * 78)
    print("ANALYSIS E — Time-in-trade & exit-reason mix (conf_high vs conf_low)")
    print("─" * 78)
    print(f"  Split at conf=0.75 (matching S130 / Analysis C convention).")
    high = [t for t in cohort if float(t["confidence"]) > 0.75]
    low = [t for t in cohort if float(t["confidence"]) <= 0.75]
    print()
    for name, sub in [("conf_high", high), ("conf_low", low)]:
        durations = [d for d in (_duration_seconds(t) for t in sub) if d is not None]
        if durations:
            print(
                f"  {name} (N={len(sub)}): "
                f"mean_duration={statistics.mean(durations)/60:.1f}min, "
                f"median={statistics.median(durations)/60:.1f}min"
            )
        else:
            print(f"  {name} (N={len(sub)}): no usable durations")
    print()
    print("  Exit-reason cross-tab:")
    print(f"  {'category':<25} | conf_high | conf_low")
    print(f"  " + "-" * 50)
    h_cats = Counter(_exit_category(t) for t in high)
    l_cats = Counter(_exit_category(t) for t in low)
    all_cats = sorted(set(h_cats) | set(l_cats))
    for cat in all_cats:
        print(f"  {cat:<25} | {h_cats[cat]:>9} | {l_cats[cat]:>8}")
    print()


# ============================================================================
# Analysis F — Confidence × wp_edge interaction (reconstructed)
# ============================================================================

def analysis_f(cohort: list[dict]) -> dict:
    print("─" * 78)
    print("ANALYSIS F — Confidence × wp_edge interaction (reconstructed estimated_win_prob)")
    print("─" * 78)
    print("  estimated_win_prob is reconstructed retroactively. Sources:")
    print("    native: logged S99+ field (precise)")
    print("    backsolve_exact: from confidence/dqs (confidence<1.0)")
    print("    backsolve_clamped_lowerbound: clamped (=1.0); estimate is LOWER BOUND")
    print()

    enriched = []
    sources = Counter()
    for t in cohort:
        ewp, src = _reconstruct_wp(t)
        sources[src] += 1
        if ewp is None:
            continue
        enriched.append({
            "trade": t,
            "ewp": ewp,
            "outcome": _actual_outcome(t),
            "source": src,
        })

    print(f"  Reconstruction tally: {dict(sources)}")
    print(f"  Usable for analysis: N={len(enriched)} (of {len(cohort)})")
    print()
    print(f"  Conf band       | N  | mean_bias  | Brier   | sources")
    print(f"  " + "-" * 70)
    band_summary = {}
    for lo, hi in BANDS:
        sub = [e for e in enriched if _in_band(float(e["trade"]["confidence"]), lo, hi)]
        n = len(sub)
        band_label = f"[{lo:.2f}, {hi:.2f})" if hi <= 1.0 else f"[{lo:.2f}, 1.00]"
        if n < 3:
            print(f"  {band_label:<15} | {n:>2} | (N<3, skipping)")
            continue
        biases = [e["ewp"] - e["outcome"] for e in sub]
        briers = [(e["ewp"] - e["outcome"]) ** 2 for e in sub]
        srcs = Counter(e["source"] for e in sub)
        src_str = ", ".join(f"{k}={v}" for k, v in srcs.items())
        mean_bias = statistics.mean(biases)
        mean_brier = statistics.mean(briers)
        print(f"  {band_label:<15} | {n:>2} | {mean_bias:>+8.4f}  | {mean_brier:>5.4f}  | {src_str}")
        band_summary[band_label] = {
            "N": n, "mean_bias": mean_bias, "brier": mean_brier, "sources": dict(srcs),
        }
    print()
    print("  Interpretation: positive bias = model OVER-estimates win prob.")
    print("  If high-conf band has higher bias AND higher Brier, wp-quality hypothesis is supported.")
    print()
    return {"sources": dict(sources), "by_band": band_summary, "N_enriched": len(enriched)}


# ============================================================================
# Analysis G — Brier eval (S99 gate check)
# ============================================================================

def analysis_g(cohort: list[dict]) -> dict:
    print("─" * 78)
    print("ANALYSIS G — Brier eval gate check (S99 watch-list pulled forward)")
    print("─" * 78)
    enriched = []
    for t in cohort:
        ewp, src = _reconstruct_wp(t)
        if ewp is None:
            continue
        enriched.append({"trade": t, "ewp": ewp, "outcome": _actual_outcome(t), "source": src})

    native = [e for e in enriched if e["source"] == "native"]
    print(f"  Native estimated_win_prob cohort: N={len(native)}")
    if native:
        n_b = statistics.mean((e["ewp"] - e["outcome"]) ** 2 for e in native)
        n_bias = statistics.mean(e["ewp"] - e["outcome"] for e in native)
        print(f"    Brier = {n_b:.4f}, bias = {n_bias:+.4f}")
        print(f"    Status vs S99 gate (N≥30): BELOW (need {30 - len(native)} more)")

    print()
    print(f"  Reconstructed full cohort: N={len(enriched)}")
    briers = [(e["ewp"] - e["outcome"]) ** 2 for e in enriched]
    biases = [e["ewp"] - e["outcome"] for e in enriched]
    overall_brier = statistics.mean(briers)
    overall_bias = statistics.mean(biases)
    print(f"    Overall Brier = {overall_brier:.4f}")
    print(f"    Overall bias  = {overall_bias:+.4f}  (positive = model over-estimates win prob)")

    if overall_brier <= 0.20:
        gate = "OK_for_sizing"
    elif overall_brier > 0.25:
        gate = "worse_than_random"
    else:
        gate = "between (0.20 < Brier ≤ 0.25)"
    print(f"    S99 gate verdict (reconstructed): {gate}")

    # Bucketed Brier
    print()
    print(f"  Bucketed Brier (by estimated_win_prob):")
    print(f"    Range          | N  | mean_ewp | mean_outcome | bias    | Brier")
    print(f"    " + "-" * 70)
    ewp_bands = [(0.05, 0.25), (0.25, 0.45), (0.45, 0.65), (0.65, 0.85), (0.85, 0.95)]
    bucket_summary = {}
    for lo, hi in ewp_bands:
        sub = [e for e in enriched if lo <= e["ewp"] < hi or (hi == 0.95 and abs(e["ewp"] - 0.95) < 1e-9)]
        if not sub:
            print(f"    [{lo:.2f}, {hi:.2f}) | 0  | (empty)")
            continue
        mean_ewp = statistics.mean(e["ewp"] for e in sub)
        mean_out = statistics.mean(e["outcome"] for e in sub)
        b_mean = statistics.mean((e["ewp"] - e["outcome"]) ** 2 for e in sub)
        bias_mean = mean_ewp - mean_out
        label = f"[{lo:.2f}, {hi:.2f})"
        print(
            f"    {label:<14} | {len(sub):>2} | "
            f"{mean_ewp:>7.4f}  | {mean_out:>11.4f}  | "
            f"{bias_mean:>+7.4f} | {b_mean:>5.4f}"
        )
        bucket_summary[label] = {
            "N": len(sub), "mean_ewp": mean_ewp, "mean_outcome": mean_out,
            "bias": bias_mean, "brier": b_mean,
        }
    print()
    return {
        "native_N": len(native),
        "reconstructed_N": len(enriched),
        "brier_reconstructed": overall_brier,
        "bias_reconstructed": overall_bias,
        "gate_verdict": gate,
        "buckets": bucket_summary,
    }


# ============================================================================
# Analysis H — Counterfactual filter sweep
# ============================================================================

def analysis_h(cohort: list[dict]) -> list[dict]:
    print("─" * 78)
    print("ANALYSIS H — Counterfactual filter sweep")
    print("─" * 78)
    print("  Variant: 'only enter trades where confidence ≤ threshold' (rejects high-conf trades).")
    print()
    baseline_total = sum(_pnl(t) for t in cohort)
    baseline_per = baseline_total / len(cohort)
    print(f"  {'Threshold':<11} | {'N_rejected':<10} | {'N_remaining':<11} | {'Total P&L':<10} | {'Per-trade':<10} | {'Δ/trade vs baseline'}")
    print(f"  " + "-" * 90)
    print(f"  {'baseline':<11} | {0:<10} | {len(cohort):<11} | ${baseline_total:>+8.2f} | ${baseline_per:>+8.4f} | —")
    rows = []
    actionable = []
    for thresh in FILTER_THRESHOLDS:
        kept = [t for t in cohort if float(t["confidence"]) <= thresh]
        n_kept = len(kept)
        n_rej = len(cohort) - n_kept
        if n_kept == 0:
            print(f"  ≤{thresh:<10.2f}| {n_rej:<10} | {n_kept:<11} | (all rejected)")
            rows.append({"threshold": thresh, "n_rejected": n_rej, "n_remaining": 0, "total": 0.0, "per_trade": 0.0, "delta_per_trade": -baseline_per})
            continue
        total = sum(_pnl(t) for t in kept)
        per_trade = total / n_kept
        delta = per_trade - baseline_per
        flag = " ←  Δ ≥ +$1.00" if delta >= 1.00 else ""
        print(
            f"  ≤{thresh:<10.2f}| {n_rej:<10} | {n_kept:<11} | ${total:>+8.2f} | ${per_trade:>+8.4f} | ${delta:>+7.4f}{flag}"
        )
        rows.append({
            "threshold": thresh, "n_rejected": n_rej, "n_remaining": n_kept,
            "total": total, "per_trade": per_trade, "delta_per_trade": delta,
        })
        if delta >= 1.00:
            actionable.append(thresh)
    print()
    if actionable:
        print(f"  Actionable thresholds (Δ/trade ≥ +$1.00): {actionable}")
    else:
        print(f"  No threshold achieves Δ/trade ≥ +$1.00 vs baseline.")
    print()
    return rows


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    cohort = load_cohort()
    print_header(cohort)
    analysis_a(cohort)
    analysis_b(cohort)
    c_res = analysis_c(cohort)
    d_res = analysis_d(cohort)
    analysis_e(cohort)
    f_res = analysis_f(cohort)
    g_res = analysis_g(cohort)
    h_rows = analysis_h(cohort)

    print("=" * 78)
    print("DECISION FRAMING")
    print("=" * 78)
    print(f"  Median-split contrast (Analysis C): high − low = ${c_res['diff']:+.2f}")
    print(f"    bootstrap 95% CI: (${c_res['boot_lo']:+.2f}, ${c_res['boot_hi']:+.2f}) — excludes zero: {c_res['ci_excludes_zero']}")
    print(f"    Welch t p = {c_res['p_t']:.4f},  Mann-Whitney p = {c_res['p_u']:.4f}")
    inverted_sports = [s for s, r in d_res.items() if r.get("inverted") is True]
    print(f"  Sports with inverted shape (Analysis D, N≥5): {inverted_sports}")
    print(f"  S99 gate verdict (Analysis G reconstructed): {g_res['gate_verdict']}")
    actionable = [r for r in h_rows if r["delta_per_trade"] >= 1.00]
    if actionable:
        print(f"  Actionable filter thresholds (Analysis H, Δ/trade ≥ +$1.00): "
              f"{[r['threshold'] for r in actionable]}")
    else:
        print(f"  Actionable filter thresholds (Analysis H): NONE")
    print()


if __name__ == "__main__":
    main()
