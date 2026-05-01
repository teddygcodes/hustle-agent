"""cadence_outcome: bucket settled paper trades by tracker-cadence-before-exit, flag P&L outliers.

Quantifies "does the bot exit late when the cadence loop is slow?" — the Session 39
12-hour-wedge incident territory, but measured in P&L terms, not heartbeat seconds.

For each settled paper_trade: find tracker_cadence records (called_from='_position_check_loop')
in the (resolved_at - 1h, resolved_at] window. Median ms_since_last_call → bucket.
Per bucket with >=10 trades: compare bucket mean P&L to global mean; flag if it lands
>=1 std dev below.

Reads tracker_cadence.jsonl + paper_trades.json (settled subset).
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from ..findings import Finding
from .outlier_pnl import _parse_ts

# Boundaries in MILLISECONDS (tracker_cadence.ms_since_last_call); spec
# CADENCE_BUCKETS_SECONDS = [10, 20, 35, 60, 120] converted to ms here.
CADENCE_BUCKETS_MS = [10000, 20000, 35000, 60000, 120000]
MIN_TRADES_PER_BUCKET = 10
WINDOW_BEFORE_EXIT_HOURS = 1
ALERT_STD_DEVS_BELOW_MEAN = 1.0
SETTLED_STATUSES = {"won", "lost", "exited_early"}


def _bucket_for(median_ms: float) -> str:
    for boundary in CADENCE_BUCKETS_MS:
        if median_ms <= boundary:
            return f"<={boundary // 1000}s"
    return f">{CADENCE_BUCKETS_MS[-1] // 1000}s"


class CadenceOutcome:
    name = "cadence_outcome"
    data_sources = ("tracker_cadence", "paper_trades")

    def run(self, ctx) -> list[Finding]:
        # Pre-parse tracker_cadence rows into (ts, ms) tuples for the position-check loop only
        cadence_rows: list[tuple[dt.datetime, float]] = []
        for r in ctx.tracker_cadence:
            if r.get("called_from") != "_position_check_loop":
                continue
            ts = _parse_ts(r.get("ts"))
            ms = r.get("ms_since_last_call")
            if ts is None or ms is None:
                continue
            cadence_rows.append((ts, float(ms)))
        cadence_rows.sort(key=lambda x: x[0])

        # Bucket each settled trade by its pre-exit median cadence
        per_bucket_pnls: dict[str, list[float]] = defaultdict(list)
        all_pnls: list[float] = []

        for t in ctx.paper_trades:
            if t.get("status") not in SETTLED_STATUSES:
                continue
            resolved = _parse_ts(t.get("resolved_at"))
            if resolved is None:
                continue
            window_start = resolved - dt.timedelta(hours=WINDOW_BEFORE_EXIT_HOURS)
            in_window = [ms for (ts, ms) in cadence_rows if window_start < ts <= resolved]
            if not in_window:
                continue
            median_ms = statistics.median(in_window)
            bucket = _bucket_for(median_ms)
            pnl = float(t.get("pnl") or 0)
            per_bucket_pnls[bucket].append(pnl)
            all_pnls.append(pnl)

        if len(all_pnls) < MIN_TRADES_PER_BUCKET:
            return []
        global_mean = statistics.mean(all_pnls)
        global_std = statistics.pstdev(all_pnls) if len(all_pnls) > 1 else 0.0
        if global_std == 0.0:
            return []  # cannot detect outlier buckets without variance

        threshold = global_mean - ALERT_STD_DEVS_BELOW_MEAN * global_std

        findings: list[Finding] = []
        for bucket, pnls in per_bucket_pnls.items():
            if len(pnls) < MIN_TRADES_PER_BUCKET:
                continue
            bucket_mean = statistics.mean(pnls)
            if bucket_mean >= threshold:
                continue
            evidence = {
                "cadence_bucket": bucket,
                "trade_count": len(pnls),
                "bucket_mean_pnl": round(bucket_mean, 2),
                "global_mean_pnl": round(global_mean, 2),
                "global_std_pnl": round(global_std, 2),
                "std_devs_below_mean": round((global_mean - bucket_mean) / global_std, 2),
                "alert_threshold_std_devs": ALERT_STD_DEVS_BELOW_MEAN,
                "_fingerprint_keys": ["cadence_bucket"],
            }
            severity = "notable"
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=(
                    f"cadence bucket {bucket}: mean P&L ${bucket_mean:+.2f} on {len(pnls)} "
                    f"trades — {evidence['std_devs_below_mean']} std devs below global"
                ),
                summary=(
                    f"Trades whose pre-exit (1h window) tracker-cadence median fell into "
                    f"the {bucket} bucket settled with mean P&L ${bucket_mean:+.2f} across "
                    f"{len(pnls)} trades, vs. global mean ${global_mean:+.2f} (std "
                    f"${global_std:.2f}). May indicate exits firing too late when the "
                    f"position-check loop was running slow — Session 39 territory."
                ),
                evidence=evidence,
                suggested_action=(
                    "Audit bot/main.py:_position_check_loop and bot/tracker.py for "
                    "I/O blocking the 30s heartbeat. Cross-reference recent bot.log "
                    "for snapshot_universe wedges (Session 39) or live_watcher contention."
                ),
            ))
        return findings
