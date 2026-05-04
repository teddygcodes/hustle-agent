"""Calibration report — fair-value prediction quality per strategy.

Session 11 (Apr 25): the bot is one big bet on fair_value being right. CLV
measures execution; this measures prediction.

Reads bot/state/predictions.jsonl + last 7 daily archives. For each opp_type:
  - mean(predicted - closing_yes)  — bias signal
  - stdev / variance
  - per-bucket calibration: predicted bucketed [0,10), [10,20) ... [90,100],
    actual hit-rate (% resolved YES) per bucket
  - Brier score: mean((predicted/100 - actual_indicator)^2),
    actual_indicator = 1 if closing_yes ≥ 50 (resolved YES), 0 if NO
    Lower is better-calibrated; perfect = 0.0, random = 0.25.

Flags strategies with bucket [80,90) resolving <70% YES, or buckets [0,10) /
[10,20) resolving >30% YES (sign-error / systematic miscalibration).

Skips rows where closing_yes_price is None (still pending) or
predicted_fair_cents is None (no usable prediction).
"""
from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREDICTIONS_FILE = REPO_ROOT / "bot/state/predictions.jsonl"
ARCHIVE_DIR = REPO_ROOT / "bot/state/archive"

BUCKETS = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
           (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]

REGIME_AXES = ("time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase")


def _regime_value(rec: dict, axis: str | None) -> str:
    if not axis:
        return "_all_"
    val = (rec.get("regime") or {}).get(axis)
    return str(val) if val is not None else "unknown_regime"


def load_records(days: int = 7) -> list[dict]:
    """Load settled prediction records from the live file + last `days` archives."""
    recs: list[dict] = []
    if PREDICTIONS_FILE.exists():
        with open(PREDICTIONS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    if ARCHIVE_DIR.exists():
        for gz in sorted(ARCHIVE_DIR.glob("predictions-*.jsonl.gz"))[-days:]:
            try:
                with gzip.open(gz, "rt") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            recs.append(json.loads(line))
                        except Exception:
                            continue
            except Exception:
                continue
    return [r for r in recs
            if r.get("closing_yes_price") is not None
            and r.get("predicted_fair_cents") is not None]


def bucket_of(cents: float) -> tuple[int, int]:
    """Return the [lo, hi) bucket that contains cents. Top bucket is inclusive."""
    for lo, hi in BUCKETS:
        if lo <= cents < hi:
            return (lo, hi)
    return BUCKETS[-1]


def brier_score(preds: list[tuple[float, int]]) -> float:
    """preds = [(predicted_cents, actual_indicator)] — actual is 1 if YES, 0 if NO.

    Brier = mean((predicted_prob - actual)^2). predicted_prob = predicted_cents/100.
    Lower is better; perfect = 0.0, random = 0.25 (when base rate is 50%).
    """
    if not preds:
        return float("nan")
    return sum(((p / 100.0) - a) ** 2 for p, a in preds) / len(preds)


def report(days: int = 7, regime_by: str | None = None) -> str:
    recs = load_records(days)
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in recs:
        opp = r.get("opp_type", "unknown")
        regime = _regime_value(r, regime_by)
        by_key[(opp, regime)].append(r)

    suffix = f" (by {regime_by})" if regime_by else ""
    out: list[str] = [f"# Calibration Report{suffix}",
                      f"\n_Settled prediction records over last {days} days: {len(recs)}_\n"]

    if not recs:
        out.append("\n_No settled predictions yet — needs ~7 days of bot uptime + market settlements._")
        return "\n".join(out)

    for (strat, regime), rows in sorted(by_key.items()):
        if regime_by:
            out.append(f"\n## {strat} — {regime_by}={regime} (n={len(rows)})\n")
        else:
            out.append(f"\n## {strat} (n={len(rows)})\n")

        # Bias / variance
        diffs = [r["predicted_fair_cents"] - r["closing_yes_price"] for r in rows]
        mean_bias = sum(diffs) / len(diffs)
        var = (sum((d - mean_bias) ** 2 for d in diffs) / len(diffs)
               if len(diffs) > 1 else 0.0)
        stdev = var ** 0.5

        # Brier score
        preds = [(r["predicted_fair_cents"],
                  1 if r["closing_yes_price"] >= 50 else 0)
                 for r in rows]
        brier = brier_score(preds)

        out.append(f"- Mean bias (predicted − actual): **{mean_bias:+.2f}¢**")
        out.append(f"- Stdev: {stdev:.2f}¢ (variance {var:.2f})")
        out.append(f"- Brier score: **{brier:.4f}** (lower = better; 0.0 = perfect, 0.25 = random)")
        out.append("")

        # Per-bucket calibration
        buckets: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
        for r in rows:
            b = bucket_of(r["predicted_fair_cents"])
            buckets[b][0] += 1
            if r["closing_yes_price"] >= 50:
                buckets[b][1] += 1

        out.append("| Predicted bucket | n | actual YES rate |")
        out.append("|---|---|---|")
        for (lo, hi), (n, yes) in sorted(buckets.items()):
            rate = yes / n if n else 0
            out.append(f"| [{lo},{hi}) | {n} | {rate:.1%} |")

        # Flag rules
        flags: list[str] = []
        high = buckets.get((80, 90), [0, 0])
        if high[0] >= 5 and (high[1] / high[0]) < 0.70:
            flags.append(
                f"⚠️ bucket [80,90) resolves YES {high[1]}/{high[0]} "
                f"({high[1] / high[0]:.1%}) — under-confident or sign error"
            )
        for lo in (0, 10):
            b = buckets.get((lo, lo + 10), [0, 0])
            if b[0] >= 5 and (b[1] / b[0]) > 0.30:
                flags.append(
                    f"⚠️ bucket [{lo},{lo + 10}) resolves YES {b[1]}/{b[0]} "
                    f"({b[1] / b[0]:.1%}) — over-confident NO predictions"
                )
        if flags:
            out.append("")
            out.append("**Flags:**")
            for f in flags:
                out.append(f"- {f}")

    return "\n".join(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--days", type=int, default=7,
                        help="Number of daily archives to include (default: 7)")
    parser.add_argument(
        "--regime-by",
        choices=list(REGIME_AXES),
        default=None,
        help="Sub-group each strategy section by a regime axis (Session 14). "
             "Records lacking the regime field bucket as 'unknown_regime'.",
    )
    args = parser.parse_args()
    print(report(days=args.days, regime_by=args.regime_by))
