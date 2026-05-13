"""Dip-class P&L report — per-class breakdown of live_momentum decisions.

Session 137 telemetry consumer. Reads bot/state/decisions.jsonl + the last
N daily archives, filters to live_momentum decisions with the dip_class
label (forward-only since S137 ship), joins to paper_trades.json by
(ticker, ts) within ±60s, and reports for each class A/B/C/D:

  - N decisions (post-cohort-filter, pre-join)
  - N settled (post-join)
  - mean P&L per settled trade
  - WR (win rate)
  - EE% (exited-early rate)
  - Counter of axis_fired reasons

Writes the report to bot/state/reports/dip_class_report_<today>.md and
also echoes a compact table to stdout. Mirrors the shape of
tools/calibration_report.py (argparse with --days / --cohort, gzip-safe
archive read).
"""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DECISIONS_FILE = REPO_ROOT / "bot/state/decisions.jsonl"
ARCHIVE_DIR = REPO_ROOT / "bot/state/archive"
PAPER_TRADES_FILE = REPO_ROOT / "bot/state/paper_trades.json"
REPORTS_DIR = REPO_ROOT / "bot/state/reports"

JOIN_WINDOW_SEC = 60.0

COHORTS = {
    "broad": datetime(2026, 5, 5, tzinfo=timezone.utc),
    "strict": datetime(2026, 5, 11, tzinfo=timezone.utc),
}


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load_decisions(days: int) -> list[dict]:
    recs: list[dict] = []
    if DECISIONS_FILE.exists():
        with DECISIONS_FILE.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if ARCHIVE_DIR.exists():
        for gz in sorted(ARCHIVE_DIR.glob("decisions-*.jsonl.gz"))[-days:]:
            try:
                with gzip.open(gz, "rt") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            recs.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except (OSError, gzip.BadGzipFile):
                continue
    return recs


def load_paper_trades() -> list[dict]:
    if not PAPER_TRADES_FILE.exists():
        return []
    with PAPER_TRADES_FILE.open() as f:
        return json.load(f)


def filter_decision_cohort(
    decisions: list[dict],
    cohort_start: datetime,
) -> list[dict]:
    out: list[dict] = []
    for d in decisions:
        if d.get("opp_type") != "live_momentum":
            continue
        ts = _parse_ts(d.get("ts"))
        if ts is None or ts < cohort_start:
            continue
        extra = d.get("extra")
        if not isinstance(extra, dict):
            continue
        if extra.get("dip_class") is None:
            continue
        out.append({**d, "_ts": ts, "_extra": extra})
    return out


def filter_trade_cohort(
    trades: list[dict],
    cohort_start: datetime,
) -> list[dict]:
    out: list[dict] = []
    for t in trades:
        if t.get("type") != "live_momentum":
            continue
        if t.get("status") not in {"won", "lost", "exited_early"}:
            continue
        ts = _parse_ts(t.get("timestamp"))
        if ts is None or ts < cohort_start:
            continue
        out.append({**t, "_ts": ts})
    return out


def index_trades_by_ticker(trades: list[dict]) -> dict[str, list[dict]]:
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_ticker[t["ticker"]].append(t)
    for v in by_ticker.values():
        v.sort(key=lambda t: t["_ts"])
    return by_ticker


def join_decision_to_trade(
    decision: dict,
    by_ticker: dict[str, list[dict]],
) -> dict | None:
    candidates = by_ticker.get(decision.get("ticker"), [])
    dts = decision["_ts"]
    best, best_delta = None, None
    for t in candidates:
        delta = abs((t["_ts"] - dts).total_seconds())
        if delta > JOIN_WINDOW_SEC:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = t, delta
    return best


def summarize_class(
    cls_decisions: list[dict],
    joined: list[tuple[dict, dict]],
) -> dict:
    n_dec = len(cls_decisions)
    n_settled = len(joined)
    pnls = [float(t.get("pnl") or 0.0) for _, t in joined]
    mean_pnl = statistics.fmean(pnls) if pnls else None
    wins = sum(1 for _, t in joined if t.get("status") == "won")
    ee = sum(1 for _, t in joined if t.get("status") == "exited_early")
    wr = wins / n_settled if n_settled else None
    ee_rate = ee / n_settled if n_settled else None
    axis_counter = Counter(
        (d["_extra"].get("dip_classifier_diagnostics") or {}).get("axis_fired")
        for d in cls_decisions
    )
    return {
        "n_decisions": n_dec,
        "n_settled": n_settled,
        "mean_pnl": mean_pnl,
        "wr": wr,
        "ee_rate": ee_rate,
        "axes": axis_counter,
    }


def render(report: dict, cohort: str, days: int) -> str:
    lines: list[str] = []
    lines.append(f"# Dip Classifier Report — cohort `{cohort}` — generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}")
    lines.append("")
    lines.append(f"- Lookback: last {days} archive shards + live decisions.jsonl")
    lines.append(f"- Cohort start: {COHORTS[cohort].isoformat()}")
    lines.append(f"- Decisions matched (live_momentum + dip_class present): {report['n_total_decisions']}")
    lines.append(f"- Paper-trade cohort (settled, post-{cohort}-start): {report['n_total_trades']}")
    lines.append("")
    lines.append("| Class | N decisions | N settled | Mean P&L | WR | EE% | Top axis |")
    lines.append("|-------|------------:|----------:|---------:|---:|----:|----------|")
    for cls in ("A", "B", "C", "D"):
        s = report["per_class"][cls]
        mean_pnl = f"{s['mean_pnl']:+.2f}" if s["mean_pnl"] is not None else "—"
        wr = f"{s['wr']:.0%}" if s["wr"] is not None else "—"
        ee = f"{s['ee_rate']:.0%}" if s["ee_rate"] is not None else "—"
        top_axis = s["axes"].most_common(1)
        top = f"{top_axis[0][0]} ({top_axis[0][1]})" if top_axis else "—"
        lines.append(f"| {cls} | {s['n_decisions']} | {s['n_settled']} | {mean_pnl} | {wr} | {ee} | {top} |")
    lines.append("")
    lines.append("## Axis breakdown per class")
    for cls in ("A", "B", "C", "D"):
        s = report["per_class"][cls]
        if not s["axes"]:
            lines.append(f"- **Class {cls}**: no decisions")
            continue
        lines.append(f"- **Class {cls}**:")
        for axis, count in s["axes"].most_common():
            lines.append(f"  - `{axis}`: {count}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14,
                        help="Number of daily archive shards to load (default 14)")
    parser.add_argument("--cohort", choices=("broad", "strict"), default="broad",
                        help="Cohort filter: broad (>=2026-05-05) or strict (>=2026-05-11)")
    args = parser.parse_args()

    cohort_start = COHORTS[args.cohort]
    decisions = load_decisions(args.days)
    trades = load_paper_trades()

    dec_cohort = filter_decision_cohort(decisions, cohort_start)
    trade_cohort = filter_trade_cohort(trades, cohort_start)
    by_ticker = index_trades_by_ticker(trade_cohort)

    per_class: dict[str, dict] = {}
    for cls in ("A", "B", "C", "D"):
        cls_decisions = [d for d in dec_cohort if d["_extra"].get("dip_class") == cls]
        joined: list[tuple[dict, dict]] = []
        for d in cls_decisions:
            t = join_decision_to_trade(d, by_ticker)
            if t is not None:
                joined.append((d, t))
        per_class[cls] = summarize_class(cls_decisions, joined)

    report = {
        "n_total_decisions": len(dec_cohort),
        "n_total_trades": len(trade_cohort),
        "per_class": per_class,
    }
    md = render(report, args.cohort, args.days)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"dip_class_report_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    out.write_text(md)
    print(md)
    print(f"\n→ wrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
