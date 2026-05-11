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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREDICTIONS_FILE = REPO_ROOT / "bot/state/predictions.jsonl"
ARCHIVE_DIR = REPO_ROOT / "bot/state/archive"
PAPER_TRADES_FILE = REPO_ROOT / "bot/state/paper_trades.json"

BUCKETS = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
           (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]

# Session 99 — buckets for live_momentum estimated_win_prob (0.0–1.0 probability).
PROB_BUCKETS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
                (0.80, 0.90), (0.90, 1.01)]

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


def report_live_momentum_calibration(days: int = 7) -> str:
    """Session 99: Brier calibration for live_momentum estimated_win_prob.

    Reads paper_trades.json (forward-only since Session 99 — pre-ship trades
    won't carry the field). Renders as a separate section so bad proxy
    performance cannot be mistaken for a vig_stack model failure (Data
    Collection Backlog Priority 3 requirement).

    The `days` arg is accepted for parity with `report()` but is currently
    unused because paper_trades.json is the live (un-rotated) state file.
    """
    out = ["", "## live_momentum proxy calibration"]
    if not PAPER_TRADES_FILE.exists():
        out.append("\n_paper_trades.json missing._")
        return "\n".join(out)
    try:
        trades = json.loads(PAPER_TRADES_FILE.read_text())
    except Exception:
        out.append("\n_paper_trades.json unreadable._")
        return "\n".join(out)
    if not isinstance(trades, list):
        out.append("\n_paper_trades.json schema unexpected._")
        return "\n".join(out)
    rows = [
        t for t in trades
        if isinstance(t, dict)
        and t.get("type") == "live_momentum"
        and t.get("status") in ("won", "lost")
        and t.get("estimated_win_prob") is not None
    ]
    out[-1] = f"## live_momentum proxy calibration (n={len(rows)})"
    if not rows:
        out.append(
            "\n_No resolved live_momentum trades with estimated_win_prob yet — "
            "ships forward-only; expect first calibration after ~14d._"
        )
        return "\n".join(out)

    preds: list[tuple[float, int]] = []
    for t in rows:
        try:
            prob = float(t["estimated_win_prob"])
        except (TypeError, ValueError):
            continue
        preds.append((prob, 1 if t["status"] == "won" else 0))
    if not preds:
        out.append("\n_All rows had unreadable estimated_win_prob; nothing to calibrate._")
        return "\n".join(out)

    brier = sum((p - a) ** 2 for p, a in preds) / len(preds)
    mean_pred = sum(p for p, _ in preds) / len(preds)
    mean_actual = sum(a for _, a in preds) / len(preds)

    out.append(f"\n- Mean predicted: **{mean_pred:.3f}**")
    out.append(f"- Mean actual win rate: **{mean_actual:.3f}**")
    out.append(f"- Mean bias (predicted − actual): **{mean_pred - mean_actual:+.3f}**")
    out.append(
        f"- Brier score: **{brier:.4f}** (lower = better; 0.0 = perfect, 0.25 = random)\n"
    )

    buckets: dict[tuple[float, float], list[int]] = defaultdict(lambda: [0, 0])
    for prob, won in preds:
        for lo, hi in PROB_BUCKETS:
            if lo <= prob < hi:
                buckets[(lo, hi)][0] += 1
                buckets[(lo, hi)][1] += won
                break

    out.append("| Predicted prob bucket | n | actual win rate |")
    out.append("|---|---|---|")
    for (lo, hi), (n, wins) in sorted(buckets.items()):
        rate = wins / n if n else 0
        out.append(f"| [{lo:.2f},{hi:.2f}) | {n} | {rate:.1%} |")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Session 100 — vig_stack ladder shapes report (Data Collection Backlog
# Priority 5). Two sections: per-family × rung_rank (all families) and
# per-weather-family × forecast_bucket_distance bucket (KXHIGHAUS B-bucket
# hypothesis from the Session 100 brief). Reads paper_trades.json directly
# (forward-only since Session 100 ship).
# ---------------------------------------------------------------------------

_FORECAST_DISTANCE_BUCKETS = (
    ("<-1.5 (deep in)",   None,  -1.5),
    ("[-1.5,-0.5) (in)",  -1.5,  -0.5),
    ("[-0.5,0.5) (edge)", -0.5,   0.5),
    ("[0.5,1.5) (out)",    0.5,   1.5),
    (">=1.5 (far out)",    1.5,   None),
)


def _forecast_distance_bucket_label(d: float) -> str:
    for label, lo, hi in _FORECAST_DISTANCE_BUCKETS:
        if lo is None and d < hi:
            return label
        if hi is None and d >= lo:
            return label
        if lo is not None and hi is not None and lo <= d < hi:
            return label
    return _FORECAST_DISTANCE_BUCKETS[-1][0]


def report_vig_stack_ladder_shapes(days: int = 7) -> str:
    """Session 100: per-family ladder-shape outcome tables for vig_stack.

    Reads paper_trades.json (forward-only since Session 100 — pre-ship
    trades won't carry the family/rank/distance fields). Two sections:

      Section A — per-family × selected_rung_rank_asc (all families;
                  identifies which rungs within a ladder lose)
      Section B — per-weather-family × forecast_bucket_distance bucket
                  (KXHIGH*/KXLOW* only — KXHIGHAUS B-bucket hypothesis)

    The `days` arg is accepted for parity with `report()` but currently
    unused because paper_trades.json is the live (un-rotated) state file.
    """
    out = ["", "## vig_stack ladder shapes"]
    if not PAPER_TRADES_FILE.exists():
        out.append("\n_paper_trades.json missing._")
        return "\n".join(out)
    try:
        trades = json.loads(PAPER_TRADES_FILE.read_text())
    except Exception:
        out.append("\n_paper_trades.json unreadable._")
        return "\n".join(out)
    if not isinstance(trades, list):
        out.append("\n_paper_trades.json schema unexpected._")
        return "\n".join(out)

    rows = [
        t for t in trades
        if isinstance(t, dict)
        and t.get("type") == "vig_stack"
        and t.get("family") is not None
        and t.get("status") in ("won", "lost")
    ]
    out[-1] = f"## vig_stack ladder shapes (n={len(rows)} resolved)"
    if not rows:
        out.append(
            "\n_No resolved vig_stack trades carrying ladder context yet — "
            "ships forward-only; expect first meaningful report after ~14d._"
        )
        return "\n".join(out)

    # === Section A — per-family × selected_rung_rank_asc ===
    out.append("")
    out.append("### A: per-family × rung_rank (ascending; rank 1 = lowest strike)")
    out.append("")
    rank_rows = [r for r in rows if r.get("selected_rung_rank_asc") is not None]
    if not rank_rows:
        out.append("_No resolved rows with selected_rung_rank_asc yet._")
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        agg: dict[tuple, dict] = defaultdict(
            lambda: {"n": 0, "won": 0, "lost": 0, "pnl": 0.0,
                     "n_lost_7d": 0, "rank_count": 0}
        )
        for r in rank_rows:
            key = (r["family"], int(r["selected_rung_rank_asc"]))
            a = agg[key]
            a["n"] += 1
            try:
                a["rank_count"] = max(a["rank_count"], int(r.get("rung_count") or 0))
            except (TypeError, ValueError):
                pass
            if r["status"] == "won":
                a["won"] += 1
            else:
                a["lost"] += 1
                if (r.get("resolved_at") or "") >= cutoff:
                    a["n_lost_7d"] += 1
            try:
                a["pnl"] += float(r.get("pnl") or 0)
            except (TypeError, ValueError):
                pass
        out.append("| family | rank | of | n | W | L | WR | total_pnl | n_lost_7d |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for (fam, rank), a in sorted(agg.items()):
            wr = (a["won"] / a["n"]) if a["n"] else 0
            out.append(
                f"| {fam} | {rank} | {a['rank_count']} | {a['n']} | "
                f"{a['won']} | {a['lost']} | {wr:.1%} | "
                f"${a['pnl']:+.2f} | {a['n_lost_7d']} |"
            )

    # === Section B — per-weather-family × forecast_bucket_distance bucket ===
    out.append("")
    out.append("### B: per-weather-family × forecast_bucket_distance bucket")
    out.append("(negative = forecast inside bucket; positive = outside)")
    out.append("")
    weather_rows = [
        r for r in rows
        if r.get("forecast_bucket_distance") is not None
        and isinstance(r.get("family"), str)
        and (r["family"].startswith("KXHIGH") or r["family"].startswith("KXLOW"))
    ]
    if not weather_rows:
        out.append("_No resolved weather rows with forecast_bucket_distance yet._")
        return "\n".join(out)

    bagg: dict[tuple, dict] = defaultdict(
        lambda: {"n": 0, "won": 0, "lost": 0, "pnl": 0.0}
    )
    for r in weather_rows:
        try:
            d = float(r["forecast_bucket_distance"])
        except (TypeError, ValueError):
            continue
        key = (r["family"], _forecast_distance_bucket_label(d))
        b = bagg[key]
        b["n"] += 1
        if r["status"] == "won":
            b["won"] += 1
        else:
            b["lost"] += 1
        try:
            b["pnl"] += float(r.get("pnl") or 0)
        except (TypeError, ValueError):
            pass

    out.append("| family | distance_bucket | n | W | L | WR | total_pnl |")
    out.append("|---|---|---|---|---|---|---|")
    _bucket_order = [label for label, _, _ in _FORECAST_DISTANCE_BUCKETS]
    for fam in sorted({k[0] for k in bagg}):
        for bucket in _bucket_order:
            key = (fam, bucket)
            if key not in bagg:
                continue
            b = bagg[key]
            wr = (b["won"] / b["n"]) if b["n"] else 0
            out.append(
                f"| {fam} | {bucket} | {b['n']} | {b['won']} | {b['lost']} | "
                f"{wr:.1%} | ${b['pnl']:+.2f} |"
            )

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
    # Session 99: live_momentum proxy calibration as a separate section so it
    # cannot be conflated with vig_stack fair-value calibration.
    print(report_live_momentum_calibration(days=args.days))
    # Session 100: vig_stack ladder shapes (per-family × rank, per-weather-family
    # × forecast_bucket_distance) — Data Collection Backlog Priority 5.
    print(report_vig_stack_ladder_shapes(days=args.days))
