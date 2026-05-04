"""Universe report — what active Kalshi markets we're scanning vs. ignoring.

Session 12 (Apr 25 pivot-enabling instrumentation arc): every existing
collection point only fires on opportunities a strategy already considered.
universe.jsonl captures every active Kalshi market per scan with a scanned_by
join key — empty list = ignored by every active strategy. This report
surfaces what we're missing.

Reads bot/state/universe.jsonl + last 7 daily archives. Per series_ticker
prefix (KXTEMP, KXNBA, ...) and per scanner attribution:
  - Total markets (unique tickers across the window)
  - % scanned vs % ignored
  - Volume distribution of ignored markets (mean / median / p90)
  - Spread distribution (yes_ask - yes_bid) of ignored markets

Sanity flag: any prefix where ignored markets average >$100/day volume AND
>5¢ mean spread is called out as Session-13 candidate territory.

Skips rows with `partial: true` from absolute counts (cursor pagination
failed mid-snapshot) but includes them in scanned/ignored ratios since
those are still meaningful for the captured slice.
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_FILE = REPO_ROOT / "bot/state/universe.jsonl"
ARCHIVE_DIR = REPO_ROOT / "bot/state/archive"

# Sanity-flag thresholds — markets we ignore that exceed BOTH count as
# Session-13 candidate territory.
SANITY_VOLUME_THRESHOLD = 100   # $/day equivalent (volume_24h is integer count;
                                # treat as $ proxy when contract price ~$0.50)
SANITY_SPREAD_THRESHOLD_CENTS = 5

REGIME_AXES = ("time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase")


def _regime_value(rec: dict, axis: str | None) -> str:
    if not axis:
        return "_all_"
    val = (rec.get("regime") or {}).get(axis)
    return str(val) if val is not None else "unknown_regime"


def load_records(days: int = 7) -> list[dict]:
    """Load universe records from live file + last `days` daily archives."""
    recs: list[dict] = []
    if UNIVERSE_FILE.exists():
        with open(UNIVERSE_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    if ARCHIVE_DIR.exists():
        for gz in sorted(ARCHIVE_DIR.glob("universe-*.jsonl.gz"))[-days:]:
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
    return recs


def _series_prefix(series_ticker: str | None, ticker: str | None) -> str:
    """Group key: prefer series_ticker; fall back to first chunk of ticker.

    Kalshi tickers look like KXTEMP-26APR25-T70.5. series_ticker is usually
    KXTEMP. When the series field is missing (older or odd records), use
    the first hyphen-delimited segment of ticker.
    """
    if series_ticker:
        return series_ticker
    if ticker:
        return ticker.split("-")[0] or "UNKNOWN"
    return "UNKNOWN"


def _spread_cents(rec: dict) -> int | None:
    ya = rec.get("yes_ask")
    yb = rec.get("yes_bid")
    if not isinstance(ya, (int, float)) or not isinstance(yb, (int, float)):
        return None
    return int(ya) - int(yb)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct * (len(s) - 1)))))
    return s[k]


def generate_report(days: int = 7, by_scanner: bool = False, regime_by: str | None = None) -> str:
    recs = load_records(days)
    out: list[str] = []
    if not recs:
        return "# Universe report\n\n_No universe.jsonl records found._"

    by_ticker: dict[str, dict] = {}
    scanned_acc: dict[str, set] = defaultdict(set)
    partial_count = 0

    for r in recs:
        ticker = r.get("ticker")
        if not ticker:
            continue
        if r.get("partial"):
            partial_count += 1
        prev = by_ticker.get(ticker)
        if prev is None or (r.get("ts", "") > prev.get("ts", "")):
            by_ticker[ticker] = r
        for s in (r.get("scanned_by") or []):
            scanned_acc[ticker].add(s)

    for t, r in by_ticker.items():
        r["scanned_by"] = sorted(scanned_acc.get(t, set()))

    by_prefix: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in by_ticker.values():
        prefix = _series_prefix(r.get("series_ticker"), r.get("ticker"))
        regime = _regime_value(r, regime_by)
        by_prefix[(prefix, regime)].append(r)

    total = len(by_ticker)
    scanned = sum(1 for r in by_ticker.values() if r.get("scanned_by"))
    ignored = total - scanned

    out.append(f"# Universe report — last {days} day(s)")
    out.append("")
    out.append(f"- Unique tickers observed: **{total:,}**")
    out.append(f"- Scanned by ≥1 active strategy: **{scanned:,}** "
               f"({100 * scanned / total if total else 0:.1f}%)")
    out.append(f"- Ignored: **{ignored:,}** "
               f"({100 * ignored / total if total else 0:.1f}%)")
    if partial_count:
        pct = 100 * partial_count / max(1, total)
        out.append(f"- _{partial_count:,} ({pct:.0f}%) rows from partial scans (cursor pagination didn't complete)._")
    out.append("")

    sanity_flags: list[tuple[str, str, int, float, float]] = []
    for (prefix, regime), prefix_recs in by_prefix.items():
        ignored_recs = [r for r in prefix_recs if not r.get("scanned_by")]
        if not ignored_recs:
            continue
        vols = [r.get("volume_24h") or 0 for r in ignored_recs]
        spreads = [s for s in (_spread_cents(r) for r in ignored_recs)
                   if s is not None and s >= 0]
        if not vols or not spreads:
            continue
        mean_vol = statistics.mean(vols)
        mean_spread = statistics.mean(spreads)
        if mean_vol > SANITY_VOLUME_THRESHOLD and mean_spread > SANITY_SPREAD_THRESHOLD_CENTS:
            sanity_flags.append((prefix, regime, len(ignored_recs), mean_vol, mean_spread))

    if sanity_flags:
        out.append("## ⚠️  Session-13 candidate territory")
        out.append("")
        out.append(f"_Ignored prefixes with >${SANITY_VOLUME_THRESHOLD} mean volume_24h "
                   f"AND >{SANITY_SPREAD_THRESHOLD_CENTS}¢ mean spread:_")
        out.append("")
        if regime_by:
            out.append(f"| Prefix | {regime_by} | Ignored markets | Mean volume_24h | Mean spread |")
            out.append("|---|---|---:|---:|---:|")
            for prefix, regime, n, vol, spread in sorted(sanity_flags, key=lambda x: -x[3]):
                out.append(f"| `{prefix}` | {regime} | {n:,} | {vol:,.0f} | {spread:.1f}¢ |")
        else:
            out.append("| Prefix | Ignored markets | Mean volume_24h | Mean spread |")
            out.append("|---|---:|---:|---:|")
            for prefix, _regime, n, vol, spread in sorted(sanity_flags, key=lambda x: -x[3]):
                out.append(f"| `{prefix}` | {n:,} | {vol:,.0f} | {spread:.1f}¢ |")
        out.append("")

    out.append("## Per-series-prefix breakdown")
    out.append("")
    if regime_by:
        out.append(f"| Prefix | {regime_by} | Total | Scanned | Ignored | "
                   "Ignored vol mean | Ignored vol p90 | Ignored spread mean |")
        out.append("|---|---|---:|---:|---:|---:|---:|---:|")
    else:
        out.append("| Prefix | Total | Scanned | Ignored | "
                   "Ignored vol mean | Ignored vol p90 | Ignored spread mean |")
        out.append("|---|---:|---:|---:|---:|---:|---:|")
    for (prefix, regime) in sorted(by_prefix.keys()):
        prefix_recs = by_prefix[(prefix, regime)]
        n_total = len(prefix_recs)
        n_scanned = sum(1 for r in prefix_recs if r.get("scanned_by"))
        n_ignored = n_total - n_scanned
        ignored_recs = [r for r in prefix_recs if not r.get("scanned_by")]
        vols = [r.get("volume_24h") or 0 for r in ignored_recs]
        spreads = [s for s in (_spread_cents(r) for r in ignored_recs)
                   if s is not None and s >= 0]
        vol_mean = statistics.mean(vols) if vols else 0.0
        vol_p90 = _percentile([float(v) for v in vols], 0.9) if vols else 0.0
        spread_mean = statistics.mean(spreads) if spreads else 0.0
        flag = " ⚠️" if (vol_mean > SANITY_VOLUME_THRESHOLD
                          and spread_mean > SANITY_SPREAD_THRESHOLD_CENTS) else ""
        if regime_by:
            out.append(f"| `{prefix}`{flag} | {regime} | {n_total:,} | {n_scanned:,} | {n_ignored:,} | "
                       f"{vol_mean:,.0f} | {vol_p90:,.0f} | {spread_mean:.1f}¢ |")
        else:
            out.append(f"| `{prefix}`{flag} | {n_total:,} | {n_scanned:,} | {n_ignored:,} | "
                       f"{vol_mean:,.0f} | {vol_p90:,.0f} | {spread_mean:.1f}¢ |")
    out.append("")

    if by_scanner:
        out.append("## Per-scanner attribution")
        out.append("")
        scanner_counts: dict[str, int] = defaultdict(int)
        for r in by_ticker.values():
            for s in (r.get("scanned_by") or []):
                scanner_counts[s] += 1
        if not scanner_counts:
            out.append("_No scanner attributions recorded._")
        else:
            out.append("| Scanner | Tickers seen |")
            out.append("|---|---:|")
            for s, n in sorted(scanner_counts.items(), key=lambda x: -x[1]):
                out.append(f"| `{s}` | {n:,} |")
        out.append("")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7,
                    help="archive lookback in days (default 7)")
    ap.add_argument("--by-scanner", action="store_true",
                    help="add per-scanner breakdown table")
    ap.add_argument(
        "--regime-by",
        choices=list(REGIME_AXES),
        default=None,
        help="Sub-group prefix breakdown by a regime axis (Session 14). "
             "Records lacking the regime field bucket as 'unknown_regime'.",
    )
    args = ap.parse_args()
    print(generate_report(days=args.days, by_scanner=args.by_scanner, regime_by=args.regime_by))


if __name__ == "__main__":
    main()
