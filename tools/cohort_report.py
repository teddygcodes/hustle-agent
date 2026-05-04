#!/usr/bin/env python3
"""Session 6 cohort report — gate effectiveness × counterfactual outcomes.

Reads `bot/state/decisions.jsonl` (current + last N days of gzipped archives)
and `bot/state/clv.json` (counterfactual records). For each (opp_type, gate)
pair: invocations, reject rate, mean edge of rejects, plus settled CF
outcomes for opportunities the gate killed.

A gate that consistently rejects positive-CLV opportunities is mis-tuned —
those become Session 7 retuning targets.

Usage:
    python3 tools/cohort_report.py --days 7
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "bot" / "state"
ARCHIVE_DIR = STATE_DIR / "archive"
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"
CLV_FILE = STATE_DIR / "clv.json"


def _parse_ts(s):
    if not s:
        return None
    try:
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


def load_decisions(window_start: datetime) -> list[dict]:
    """Read decisions.jsonl + matching archives; keep records >= window_start."""
    records: list[dict] = []
    sources: list[tuple[Path, callable]] = []
    if DECISIONS_FILE.exists():
        sources.append((DECISIONS_FILE, open))
    if ARCHIVE_DIR.exists():
        for path in sorted(ARCHIVE_DIR.glob("decisions-*.jsonl.gz")):
            sources.append((path, gzip.open))
    for path, opener in sources:
        try:
            for rec in _iter_jsonl_lines(path, opener):
                ts = _parse_ts(rec.get("ts"))
                if ts and ts >= window_start:
                    records.append(rec)
        except (OSError, EOFError):
            print(f"# warning: could not read {path}", file=sys.stderr)
            continue
    return records


def load_cf_records(window_start: datetime) -> list[dict]:
    """Read clv.json; keep records with status counterfactual_*, recorded >= window_start."""
    if not CLV_FILE.exists():
        return []
    try:
        with open(CLV_FILE) as f:
            all_records = json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"# warning: could not parse {CLV_FILE}", file=sys.stderr)
        return []
    out = []
    for r in all_records:
        if r.get("status") not in ("counterfactual_open", "counterfactual_settled"):
            continue
        ts = _parse_ts(r.get("recorded_at"))
        if ts and ts >= window_start:
            out.append(r)
    return out


REGIME_AXES = ("time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase")
_NO_REGIME = "_all_"


def _regime_value(rec: dict, axis: str | None) -> str:
    """Extract the regime axis value, or '_all_' when no axis selected, or
    'unknown_regime' when the record lacks the axis (pre-Session-14)."""
    if not axis:
        return _NO_REGIME
    regime = rec.get("regime") or {}
    val = regime.get(axis)
    if val is None:
        return "unknown_regime"
    return str(val)


def aggregate_decisions(decisions: list[dict], regime_by: str | None = None) -> dict[tuple, dict]:
    bins: dict[tuple, dict] = defaultdict(lambda: {
        "invocations": 0,
        "rejects": 0,
        "accepts": 0,
        "edges_of_rejects": [],
        "distances": [],  # Session 10: distance-from-threshold for enriched rejects
    })
    for d in decisions:
        opp_type = d.get("opp_type") or "unknown"
        reason = d.get("reason") or "unknown"
        decision = d.get("decision")
        edge = d.get("edge")
        regime = _regime_value(d, regime_by)
        b = bins[(opp_type, regime, reason)]
        b["invocations"] += 1
        if decision == "reject":
            b["rejects"] += 1
            if edge is not None:
                b["edges_of_rejects"].append(edge)
            dist = distance_from_pass(reason, d.get("extra"))
            if dist is not None:
                b["distances"].append(dist)
        elif decision == "accept":
            b["accepts"] += 1
    return bins


def aggregate_cf(cf_records: list[dict], regime_by: str | None = None) -> dict[tuple, dict]:
    bins: dict[tuple, dict] = defaultdict(lambda: {
        "total": 0,
        "settled": 0,
        "pending": 0,
        "sum_clv_rel": 0.0,
    })
    for r in cf_records:
        opp_type = r.get("opp_type") or "unknown"
        gate = r.get("skipped_by_gate") or "unknown"
        regime = _regime_value(r, regime_by)
        b = bins[(opp_type, regime, gate)]
        b["total"] += 1
        clv_rel = r.get("clv_relative")
        if r.get("status") == "counterfactual_settled" and clv_rel is not None:
            b["settled"] += 1
            b["sum_clv_rel"] += float(clv_rel)
        else:
            b["pending"] += 1
    return bins


def _fmt_edge(values: list[float]) -> str:
    if not values:
        return "n/a"
    return f"{sum(values) / len(values):+.4f}"


def _fmt_signed(v: float | None, dash: str = "—") -> str:
    return dash if v is None else f"{v:+.4f}"


# ---------------------------------------------------------------------------
# Session 10 — distance-from-threshold histograms
# ---------------------------------------------------------------------------

DISTANCE_BUCKETS = [
    ("<10%",   0.00, 0.10),
    ("10-25%", 0.10, 0.25),
    ("25-50%", 0.25, 0.50),
    ("50-100%", 0.50, 1.00),
    (">100%",  1.00, float("inf")),
]


def distance_from_pass(gate: str, extra: dict | None) -> float | None:
    """Return |actual − threshold| / threshold for a rejected decision.

    Smaller = closer to passing (i.e. more of a tuning candidate).
    Returns None when extra is missing required keys or the gate is not
    instrumented for distance — pre-Session-10 records and gates outside
    the Session 10 enrichment list both fall into this bucket and are
    silently skipped from the histogram.
    """
    if not extra:
        return None
    if gate == "edge_below_threshold":
        edge, thresh = extra.get("edge"), extra.get("min_edge")
        if edge is None or not thresh:
            return None
        return abs(thresh - edge) / abs(thresh)
    if gate == "low_liquidity":
        v = extra.get("volume")
        oi = extra.get("open_interest")
        mv = extra.get("min_volume")
        moi = extra.get("min_open_interest")
        if None in (v, oi, mv, moi) or not mv or not moi:
            return None
        # Both must be below threshold to fire; report whichever was closer to passing
        return min((mv - v) / mv, (moi - oi) / moi)
    if gate == "forecast_in_bucket":
        d = extra.get("distance")
        if d is None:
            return None
        return abs(d) / 2.0  # ±2° is the gate margin
    if gate == "cooldown":
        age, cd = extra.get("last_trade_age_min"), extra.get("cooldown_min")
        if age is None or not cd:
            return None
        return max(0.0, (cd - age) / cd)
    if gate in ("position_cap", "strategy_budget", "total_exposure"):
        exp_pct = extra.get("exposure_pct")
        cap_pct = extra.get("max_pct")
        if exp_pct is None or not cap_pct:
            return None
        return max(0.0, (exp_pct - cap_pct) / cap_pct)
    if gate == "daily_loss":
        loss, limit = extra.get("daily_ticker_loss"), extra.get("limit")
        if loss is None or not limit:
            return None
        return max(0.0, (loss - limit) / limit)
    if gate == "price_moved":
        move, kill = extra.get("move_cents"), extra.get("kill_cents")
        if move is None or not kill:
            return None
        return max(0.0, (move - kill) / kill)
    if gate == "edge_evaporated":
        new, thresh = extra.get("new_relative"), extra.get("edge_threshold")
        if new is None or not thresh:
            return None
        return abs(thresh - new) / abs(thresh)
    return None


def bucket_distance(d: float) -> str:
    for label, lo, hi in DISTANCE_BUCKETS:
        if lo <= d < hi:
            return label
    return DISTANCE_BUCKETS[-1][0]


def render_distance_histogram(distances: list[float], width: int = 30) -> list[str]:
    """ASCII histogram across DISTANCE_BUCKETS. Returns markdown table rows."""
    if not distances:
        return ["_No distance data (pre-Session-10 records or unhandled gate)._"]
    counts = {label: 0 for label, _, _ in DISTANCE_BUCKETS}
    for d in distances:
        counts[bucket_distance(d)] += 1
    total = sum(counts.values())
    max_count = max(counts.values()) or 1
    lines = ["| Distance bucket | Count | % | Histogram |", "|---|---:|---:|---|"]
    for label, _, _ in DISTANCE_BUCKETS:
        c = counts[label]
        pct = c / total * 100 if total else 0.0
        bar = "█" * int(round(c / max_count * width))
        lines.append(f"| {label} | {c} | {pct:.1f}% | {bar} |")
    return lines


def render_markdown(
    decisions: list[dict],
    cf_records: list[dict],
    days: int,
    decision_bins: dict,
    cf_bins: dict,
    window_start: datetime,
    window_end: datetime,
    regime_by: str | None = None,
) -> str:
    out: list[str] = []
    suffix = f" (by {regime_by})" if regime_by else ""
    out.append(f"# Glint Cohort Report — Last {days} day(s){suffix}")
    out.append("")
    out.append(f"Window: `{window_start.isoformat()}` → `{window_end.isoformat()}` UTC")
    out.append("")

    total_dec = len(decisions)
    total_rej = sum(1 for d in decisions if d.get("decision") == "reject")
    total_acc = sum(1 for d in decisions if d.get("decision") == "accept")
    out.append(f"- Total decisions: **{total_dec:,}**  (rejects {total_rej:,} · accepts {total_acc:,})")

    cf_total = len(cf_records)
    cf_settled = sum(1 for r in cf_records if r.get("status") == "counterfactual_settled")
    out.append(f"- Counterfactual records: **{cf_total:,}**  (settled {cf_settled:,} · pending {cf_total - cf_settled:,})")
    out.append("")

    if total_dec == 0:
        out.append("_No decisions in window._ Run the bot to populate `bot/state/decisions.jsonl`.")
        return "\n".join(out)

    # Group bins by (opp_type, regime) for the per-strategy table.
    # When --regime-by is unset, every bin's regime is "_all_" so output
    # collapses to one section per opp_type (identical to pre-Session-14).
    by_opp_regime: dict[tuple[str, str], list[tuple[str, dict]]] = defaultdict(list)
    for (opp, regime, gate), v in decision_bins.items():
        by_opp_regime[(opp, regime)].append((gate, v))

    out.append("## Per-strategy gate cohort")
    out.append("")

    for (opp, regime) in sorted(by_opp_regime):
        if regime_by:
            out.append(f"### {opp} — {regime_by}={regime}")
        else:
            out.append(f"### {opp}")
        out.append("")
        out.append("| Gate / Reason | Inv. | Rej. | Rej % | Mean rej edge | CF settled | Σ CF clv_rel | Mean CF clv_rel |")
        out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for gate, v in sorted(by_opp_regime[(opp, regime)], key=lambda x: -x[1]["invocations"]):
            inv = v["invocations"]
            rej = v["rejects"]
            rej_pct = (rej / inv * 100) if inv else 0.0
            mean_edge_s = _fmt_edge(v["edges_of_rejects"])
            cf = cf_bins.get((opp, regime, gate))
            cf_n = cf["settled"] if cf else 0
            cf_sum = cf["sum_clv_rel"] if cf else 0.0
            cf_mean = (cf_sum / cf_n) if cf_n else None
            cf_sum_s = _fmt_signed(cf_sum if cf_n else None)
            cf_mean_s = _fmt_signed(cf_mean)
            out.append(
                f"| {gate} | {inv:,} | {rej:,} | {rej_pct:.1f}% | {mean_edge_s} | {cf_n} | {cf_sum_s} | {cf_mean_s} |"
            )
        out.append("")

        # Session 10 — distance-from-threshold per gate (rejects only).
        # Pre-Session-10 records contribute zero distances; gates outside the
        # enriched set (no_vig, market_closed, etc.) also contribute zero. Skip
        # the section for gates with no enriched rejects.
        gates_with_distance = [(g, v) for g, v in by_opp_regime[(opp, regime)] if v["distances"]]
        if gates_with_distance:
            out.append("#### Distance-from-threshold (rejects only)")
            out.append("")
            out.append("_Smaller distance = closer to passing. Gates with >50% in `<10%` are tuning candidates._")
            out.append("")
            for gate, v in sorted(gates_with_distance, key=lambda x: -len(x[1]["distances"])):
                out.append(f"**{gate}** — {len(v['distances'])} enriched record(s)")
                out.extend(render_distance_histogram(v["distances"]))
                out.append("")

    out.append("## Mis-tuning candidates")
    out.append("")
    out.append("_Gates with reject rate ≥ 50% AND positive cumulative CF clv_relative across ≥ 5 settled CFs._")
    out.append("_Positive Σ CF clv_rel means rejected trades closed in our favor → gate may be too strict._")
    out.append("")
    candidates: list[dict] = []
    for (opp, regime, gate), v in decision_bins.items():
        inv = v["invocations"]
        rej = v["rejects"]
        rej_pct = (rej / inv * 100) if inv else 0.0
        cf = cf_bins.get((opp, regime, gate))
        if cf and cf["settled"] >= 5 and cf["sum_clv_rel"] > 0 and rej_pct >= 50:
            candidates.append({
                "opp": opp,
                "regime": regime,
                "gate": gate,
                "rej_pct": rej_pct,
                "cf_n": cf["settled"],
                "cf_sum": cf["sum_clv_rel"],
                "cf_mean": cf["sum_clv_rel"] / cf["settled"],
            })

    if not candidates:
        out.append("None. Either gates are correctly tuned, or there are not yet enough settled CFs to judge.")
    else:
        candidates.sort(key=lambda c: -c["cf_sum"])
        for c in candidates:
            label = f"{c['opp']} / {c['gate']}"
            if regime_by:
                label = f"{c['opp']} / {regime_by}={c['regime']} / {c['gate']}"
            out.append(
                f"- **{label}** — rejects {c['rej_pct']:.1f}%, "
                f"Σ CF clv_rel = +{c['cf_sum']:.4f} across {c['cf_n']} settled "
                f"(mean +{c['cf_mean']:.4f}). Consider relaxing this gate."
            )
    out.append("")

    # Session 10 — second mis-tuning criterion: gates where ≥50% of enriched
    # rejects sit in the <10% bucket (almost-passed). Independent of the CF
    # criterion above; flagged as a separate list so the user can see both
    # signals.
    out.append("## Mis-tuning candidates (by distance)")
    out.append("")
    out.append("_Gates where ≥50% of enriched rejects fall in the `<10%` bucket across ≥5 records._")
    out.append("")
    dist_candidates: list[dict] = []
    for (opp, regime, gate), v in decision_bins.items():
        ds = v["distances"]
        if len(ds) >= 5:
            close_frac = sum(1 for d in ds if d < 0.10) / len(ds)
            if close_frac >= 0.50:
                dist_candidates.append({
                    "opp": opp, "regime": regime, "gate": gate, "n": len(ds),
                    "close_frac": close_frac,
                })
    if not dist_candidates:
        out.append("None. Either no gate has ≥50% of rejects within 10% of threshold, or fewer than 5 enriched records exist yet.")
    else:
        dist_candidates.sort(key=lambda c: -c["close_frac"])
        for c in dist_candidates:
            label = f"{c['opp']} / {c['gate']}"
            if regime_by:
                label = f"{c['opp']} / {regime_by}={c['regime']} / {c['gate']}"
            out.append(
                f"- **{label}** — {c['close_frac']:.0%} of "
                f"{c['n']} enriched rejects within 10% of threshold. "
                f"Consider tightening or removing this gate."
            )
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=7, help="Window in days (default 7)")
    ap.add_argument(
        "--regime-by",
        choices=list(REGIME_AXES),
        default=None,
        help="Sub-group each strategy section by a regime axis (Session 14). "
             "Records lacking the regime field bucket as 'unknown_regime'.",
    )
    args = ap.parse_args()

    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=args.days)

    decisions = load_decisions(window_start)
    cf_records = load_cf_records(window_start)

    decision_bins = aggregate_decisions(decisions, regime_by=args.regime_by)
    cf_bins = aggregate_cf(cf_records, regime_by=args.regime_by)

    md = render_markdown(
        decisions, cf_records, args.days, decision_bins, cf_bins,
        window_start, now, regime_by=args.regime_by,
    )
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
