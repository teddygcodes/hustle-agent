"""Live-momentum bucket-analysis report (Session 30, Stage 2).

Reads the dataset CSV produced by ``tools/live_momentum_dataset.py`` and
emits markdown to stdout with:
  * 7 single-dimension bucket tables (sport, leader_price, dip, wp_edge,
    dqs, game_phase, spread).
  * 4 interaction tables (sport x leader_price, sport x dip,
    leader_price x wp_edge, dip x dqs).
  * A Findings section authored by the user after running on real data.

Per-bucket metrics: n, avg fwd_return_30s/60s/120s, avg MFE/MAE in window,
avg outcome_clv_cents, positive_clv_rate, win_rate (fwd_return_120s > 0).
Buckets with n < 5 are flagged "(low-confidence)".
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_DATASET = "bot/state/research/live_momentum_dataset.csv"
LOW_CONFIDENCE_N = 5

NUMERIC_HINTS = {
    "leader_price",
    "spread_cents",
    "wp",
    "wp_edge",
    "momentum",
    "lead_trend",
    "dip",
    "dqs",
    "period",
    "score_diff",
    "completion",
    "elapsed",
    "recent_high",
    "opp_recent_high",
    "fwd_return_30s_cents",
    "fwd_return_60s_cents",
    "fwd_return_120s_cents",
    "outcome_clv_cents",
    "outcome_clv_relative",
    "outcome_realized_pnl",
    "outcome_target_yes_price_cents",
}

BOOL_FIELDS = {"accept", "leader", "opp_leader"}


def _coerce(key: str, val: str) -> Any:
    if val == "" or val is None:
        return None
    if key in BOOL_FIELDS:
        if val in ("True", "true", "1"):
            return True
        if val in ("False", "false", "0"):
            return False
        return None
    if key in NUMERIC_HINTS or "fwd_return_" in key or "_in_" in key and "_window_" in key:
        try:
            f = float(val)
            return int(f) if f.is_integer() else f
        except ValueError:
            return val
    if "mfe_in_" in key or "mae_in_" in key:
        try:
            return float(val)
        except ValueError:
            return val
    return val


def load_dataset(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = {k: _coerce(k, v) for k, v in raw.items()}
            out.append(row)
    return out


def _band_predicate(label: str, lo: Optional[float], hi: Optional[float]):
    """Return a predicate matching values where lo <= v < hi (None means open)."""
    def pred(v):
        if v is None:
            return False
        try:
            x = float(v)
        except (TypeError, ValueError):
            return False
        if math.isnan(x):
            return False
        if lo is not None and x < lo:
            return False
        if hi is not None and x >= hi:
            return False
        return True
    pred.label = label  # type: ignore[attr-defined]
    return pred


LEADER_PRICE_BANDS = [
    _band_predicate("<60", None, 60),
    _band_predicate("60-70", 60, 70),
    _band_predicate("70-80", 70, 80),
    _band_predicate("80-90", 80, 90),
    _band_predicate(">=90", 90, None),
]

DIP_BANDS = [
    _band_predicate("0-2", 0, 2),
    _band_predicate("2-4", 2, 4),
    _band_predicate("4-6", 4, 6),
    _band_predicate("6-8", 6, 8),
    _band_predicate("8-10", 8, 10),
    _band_predicate(">10", 10, None),
]

WP_EDGE_BANDS = [
    _band_predicate("<-0.05", None, -0.05),
    _band_predicate("-0.05-0", -0.05, 0),
    _band_predicate("0-0.05", 0, 0.05),
    _band_predicate("0.05-0.10", 0.05, 0.10),
    _band_predicate(">0.10", 0.10, None),
]

DQS_BANDS = [
    _band_predicate("<0.4", None, 0.4),
    _band_predicate("0.4-0.5", 0.4, 0.5),
    _band_predicate("0.5-0.6", 0.5, 0.6),
    _band_predicate("0.6-0.7", 0.6, 0.7),
    _band_predicate(">=0.7", 0.7, None),
]

SPREAD_BANDS = [
    _band_predicate("0-2", 0, 2),
    _band_predicate("2-4", 2, 4),
    _band_predicate("4-6", 4, 6),
    _band_predicate(">=6", 6, None),
]


def derive_game_phase(row: dict) -> str:
    """Sport-aware canonical game-phase label.

    NBA: Q1/Q2/Q3/Q4/OT (period 1-4 + 5+).
    NHL: P1/P2/P3/OT.
    MLB: best-effort -- ``period`` is treated as inning if numeric.
    Other sports / missing data: "Unknown".
    """
    sport = (row.get("sport") or "").lower()
    period = row.get("period")
    if not isinstance(period, (int, float)):
        return "Unknown"
    p = int(period)
    if sport == "nba" or sport == "ncaab":
        if 1 <= p <= 4:
            return f"Q{p}"
        if p >= 5:
            return "OT"
        return "Unknown"
    if sport == "nhl":
        if 1 <= p <= 3:
            return f"P{p}"
        if p >= 4:
            return "OT"
        return "Unknown"
    if sport == "mlb":
        return f"Inn{p}"
    if p >= 1:
        return f"R{p}"
    return "Unknown"


def bucket_by(rows: list[dict], dimension, bands_or_categories) -> dict[str, list[dict]]:
    """Bucket rows.

    If bands_or_categories is None: treat dimension as a key OR callable, group
    by distinct value (categorical). If a list of band predicates: assign each
    row to the FIRST predicate that matches; rows matching none go to "Other".
    """
    if callable(dimension):
        get = dimension
    else:
        get = lambda r: r.get(dimension)  # noqa: E731
    out: dict[str, list[dict]] = defaultdict(list)
    if bands_or_categories is None:
        for r in rows:
            v = get(r)
            label = "Unknown" if v is None or v == "" else str(v)
            out[label].append(r)
        return dict(out)
    for r in rows:
        v = get(r)
        placed = False
        for pred in bands_or_categories:
            if pred(v):
                out[pred.label].append(r)  # type: ignore[attr-defined]
                placed = True
                break
        if not placed:
            out["Other"].append(r)
    return dict(out)


def _avg(rows: list[dict], key: str) -> Optional[float]:
    vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _rate(rows: list[dict], pred: Callable[[dict], bool],
          eligible: Callable[[dict], bool] = lambda r: True) -> Optional[float]:
    eligible_rows = [r for r in rows if eligible(r)]
    if not eligible_rows:
        return None
    hits = sum(1 for r in eligible_rows if pred(r))
    return hits / len(eligible_rows)


def _fmt(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def render_bucket_table(buckets: dict[str, list[dict]], label: str,
                        horizon_secs: int = 120) -> str:
    """Markdown table for a single dimension."""
    mfe_col = f"mfe_in_{horizon_secs}s_window_cents"
    mae_col = f"mae_in_{horizon_secs}s_window_cents"
    lines = [
        f"### Bucket: {label}",
        "",
        "| bucket | n | avg fwd_30s | avg fwd_60s | avg fwd_120s | avg MFE | avg MAE | avg CLV | +CLV rate | win rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    # Sort buckets: try natural order if labels are bands (preserve insertion)
    keys = list(buckets.keys())
    # bring "Other"/"Unknown" to the end if present
    sentinel = [k for k in keys if k in ("Other", "Unknown")]
    keys = [k for k in keys if k not in ("Other", "Unknown")] + sentinel
    for k in keys:
        items = buckets[k]
        n = len(items)
        marker = " (low-confidence)" if n < LOW_CONFIDENCE_N else ""
        avg30 = _avg(items, "fwd_return_30s_cents")
        avg60 = _avg(items, "fwd_return_60s_cents")
        avg120 = _avg(items, "fwd_return_120s_cents")
        avg_mfe = _avg(items, mfe_col)
        avg_mae = _avg(items, mae_col)
        avg_clv = _avg(items, "outcome_clv_cents")
        pos_clv = _rate(
            items,
            pred=lambda r: isinstance(r.get("outcome_clv_cents"), (int, float)) and r["outcome_clv_cents"] > 0,
            eligible=lambda r: isinstance(r.get("outcome_clv_cents"), (int, float)),
        )
        win = _rate(
            items,
            pred=lambda r: isinstance(r.get("fwd_return_120s_cents"), (int, float)) and r["fwd_return_120s_cents"] > 0,
            eligible=lambda r: isinstance(r.get("fwd_return_120s_cents"), (int, float)),
        )
        lines.append(
            f"| {k}{marker} | {n} | {_fmt(avg30)} | {_fmt(avg60)} | {_fmt(avg120)} | "
            f"{_fmt(avg_mfe)} | {_fmt(avg_mae)} | {_fmt(avg_clv)} | "
            f"{_fmt_pct(pos_clv)} | {_fmt_pct(win)} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_interaction_table(rows: list[dict], dim_a, bands_a,
                             dim_b, bands_b, label_a: str, label_b: str) -> str:
    """2D markdown grid: cells show 'avg fwd_return_120s (n)'."""
    grid: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if callable(dim_a):
        get_a = dim_a
    else:
        get_a = lambda r: r.get(dim_a)  # noqa: E731
    if callable(dim_b):
        get_b = dim_b
    else:
        get_b = lambda r: r.get(dim_b)  # noqa: E731

    def _label(v, bands):
        if bands is None:
            return "Unknown" if v is None or v == "" else str(v)
        for p in bands:
            if p(v):
                return p.label  # type: ignore[attr-defined]
        return "Other"

    keys_a: list[str] = []
    keys_b: list[str] = []
    for r in rows:
        la = _label(get_a(r), bands_a)
        lb = _label(get_b(r), bands_b)
        if la not in keys_a:
            keys_a.append(la)
        if lb not in keys_b:
            keys_b.append(lb)
        grid[(la, lb)].append(r)

    keys_a.sort()
    keys_b.sort()

    header = "| " + label_a + " \\ " + label_b + " | " + " | ".join(keys_b) + " |"
    sep = "|---|" + "|".join(["---:" for _ in keys_b]) + "|"
    out = [f"### Interaction: {label_a} x {label_b}", "", header, sep]
    for ka in keys_a:
        cells = []
        for kb in keys_b:
            items = grid.get((ka, kb), [])
            n = len(items)
            if n == 0:
                cells.append("—")
            else:
                avg = _avg(items, "fwd_return_120s_cents")
                marker = "*" if n < LOW_CONFIDENCE_N else ""
                cells.append(f"{_fmt(avg)} (n={n}){marker}")
        out.append(f"| {ka} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("_*marks cells with n<5 (low-confidence)._")
    out.append("")
    return "\n".join(out)


def author_findings(rows: list[dict], single_buckets: dict[str, dict[str, list[dict]]]) -> str:
    """Findings section. Surfaces strongest signal vs baseline + thin patterns.

    Renders even on thin samples — emits a "no clear signal" notice instead of
    fabricating one. Mirror Session 18.5 Outcome-B discipline.
    """
    if not rows:
        return "### Findings\n\nNo rows in dataset — nothing to surface.\n"

    baseline_120 = _avg(rows, "fwd_return_120s_cents")
    out = ["### Findings", ""]
    if baseline_120 is None:
        out.append("- Baseline avg fwd_return_120s = — (no settled forward windows).")
    else:
        out.append(f"- Baseline avg fwd_return_120s = {_fmt(baseline_120)}c across n={len(rows)} rows.")

    interesting: list[tuple[str, str, int, float]] = []  # (dim, bucket, n, delta)
    for dim, buckets in single_buckets.items():
        for label, items in buckets.items():
            if len(items) < LOW_CONFIDENCE_N:
                continue
            avg = _avg(items, "fwd_return_120s_cents")
            if avg is None or baseline_120 is None:
                continue
            delta = avg - baseline_120
            if abs(delta) >= 1.0:
                interesting.append((dim, label, len(items), delta))

    interesting.sort(key=lambda t: abs(t[3]), reverse=True)

    if not interesting:
        out.append(
            "- No clear signal in current sample: no bucket with n>=5 deviates "
            "from baseline by >=1.0c on fwd_return_120s. (Same discipline as "
            "Session 18.5 Outcome-B — thin sample, no false positives.)"
        )
    else:
        out.append("")
        out.append("**Strongest dimensional deviations (n>=5, |delta vs baseline| >= 1.0c):**")
        out.append("")
        for dim, label, n, delta in interesting[:8]:
            sign = "+" if delta >= 0 else ""
            out.append(f"- {dim} = `{label}` (n={n}): {sign}{delta:.2f}c vs baseline")

    # Surface suspicious-but-thin patterns separately
    thin: list[tuple[str, str, int, float]] = []
    for dim, buckets in single_buckets.items():
        for label, items in buckets.items():
            if not (1 <= len(items) < LOW_CONFIDENCE_N):
                continue
            avg = _avg(items, "fwd_return_120s_cents")
            if avg is None or baseline_120 is None:
                continue
            delta = avg - baseline_120
            if abs(delta) >= 3.0:
                thin.append((dim, label, len(items), delta))
    thin.sort(key=lambda t: abs(t[3]), reverse=True)
    if thin:
        out.append("")
        out.append("**Suspicious-but-thin (n<5, |delta| >= 3.0c — needs more samples):**")
        out.append("")
        for dim, label, n, delta in thin[:5]:
            sign = "+" if delta >= 0 else ""
            out.append(f"- {dim} = `{label}` (n={n}): {sign}{delta:.2f}c vs baseline")

    out.append("")
    return "\n".join(out)


def _detect_horizon_secs(rows: list[dict]) -> int:
    if not rows:
        return 120
    for k in rows[0].keys():
        if k.startswith("mfe_in_") and k.endswith("s_window_cents"):
            mid = k[len("mfe_in_"): -len("s_window_cents")]
            try:
                return int(mid)
            except ValueError:
                continue
    return 120


def render_report(rows: list[dict]) -> str:
    horizon = _detect_horizon_secs(rows)
    out = ["# Live-momentum bucket report", "",
           f"Source rows: {len(rows)}; horizon: {horizon}s.", ""]

    if not rows:
        out.append("_No data in dataset._\n")
        out.append(author_findings(rows, {}))
        return "\n".join(out)

    accepts = sum(1 for r in rows if r.get("accept") is True)
    rejects = sum(1 for r in rows if r.get("accept") is False)
    out.append(f"Accepts: {accepts}; tunable rejects: {rejects}.")
    out.append("")

    # Single-dimension buckets
    single = {
        "sport": bucket_by(rows, "sport", None),
        "leader_price": bucket_by(rows, "leader_price", LEADER_PRICE_BANDS),
        "dip": bucket_by(rows, "dip", DIP_BANDS),
        "wp_edge": bucket_by(rows, "wp_edge", WP_EDGE_BANDS),
        "dqs": bucket_by(rows, "dqs", DQS_BANDS),
        "game_phase": bucket_by(rows, derive_game_phase, None),
        "spread_cents": bucket_by(rows, "spread_cents", SPREAD_BANDS),
    }

    out.append("## Single-dimension buckets")
    out.append("")
    for label, buckets in single.items():
        out.append(render_bucket_table(buckets, label, horizon_secs=horizon))

    # Interaction tables
    out.append("## Interaction tables")
    out.append("")
    out.append(render_interaction_table(
        rows, "sport", None, "leader_price", LEADER_PRICE_BANDS,
        "sport", "leader_price",
    ))
    out.append(render_interaction_table(
        rows, "sport", None, "dip", DIP_BANDS,
        "sport", "dip",
    ))
    out.append(render_interaction_table(
        rows, "leader_price", LEADER_PRICE_BANDS, "wp_edge", WP_EDGE_BANDS,
        "leader_price", "wp_edge",
    ))
    out.append(render_interaction_table(
        rows, "dip", DIP_BANDS, "dqs", DQS_BANDS,
        "dip", "dqs",
    ))

    out.append(author_findings(rows, single))
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    args = parser.parse_args(argv)

    rows = load_dataset(args.dataset)
    print(render_report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
