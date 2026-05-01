"""outlier_pnl: flag trades dominating their (type, sport) cohort by abs $ AND % of cohort."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from bot.regime import SPORT_PREFIXES

from ..findings import Finding

OUTLIER_DOLLAR_THRESHOLD = 75.0
OUTLIER_PCT_OF_COHORT = 0.30
HIGH_SEVERITY_PCT = 0.50
LOOKBACK_DAYS = 30
SETTLED_STATUSES = {"won", "lost", "exited_early"}

_SORTED_PREFIXES = sorted(SPORT_PREFIXES.keys(), key=len, reverse=True)


def _sport_from_ticker(ticker: str) -> str | None:
    if not ticker:
        return None
    for prefix in _SORTED_PREFIXES:
        if ticker.startswith(prefix):
            return SPORT_PREFIXES[prefix]
    return None


def _parse_ts(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class OutlierPnl:
    name = "outlier_pnl"
    data_sources = ("paper_trades",)

    def run(self, ctx) -> list[Finding]:
        cutoff = ctx.loaded_at - dt.timedelta(days=LOOKBACK_DAYS)

        eligible: list[tuple[dict, str | None]] = []
        for t in ctx.paper_trades:
            if t.get("status") not in SETTLED_STATUSES:
                continue
            ts = _parse_ts(t.get("resolved_at") or t.get("timestamp"))
            if ts is None or ts < cutoff:
                continue
            eligible.append((t, _sport_from_ticker(t.get("ticker", ""))))

        cohort_totals: dict[tuple[str | None, str | None], float] = defaultdict(float)
        for t, sport in eligible:
            key = (t.get("type"), sport)
            cohort_totals[key] += abs(float(t.get("pnl") or 0))

        findings: list[Finding] = []
        for t, sport in eligible:
            pnl = float(t.get("pnl") or 0)
            abs_pnl = abs(pnl)
            if abs_pnl < OUTLIER_DOLLAR_THRESHOLD:
                continue
            key = (t.get("type"), sport)
            cohort_total = cohort_totals[key]
            if cohort_total <= 0:
                continue
            share = abs_pnl / cohort_total
            if share < OUTLIER_PCT_OF_COHORT:
                continue
            severity = "high" if share >= HIGH_SEVERITY_PCT else "notable"
            evidence = {
                "trade_id": t.get("id"),
                "ticker": t.get("ticker"),
                "opp_type": t.get("type"),
                "sport": sport,
                "pnl": pnl,
                "cohort_total_abs_pnl": round(cohort_total, 2),
                "share_of_cohort": round(share, 4),
                "status": t.get("status"),
                "exit_reason": t.get("exit_reason"),
                "_fingerprint_keys": ["opp_type", "sport", "trade_id"],
            }
            direction = "win" if pnl > 0 else "loss"
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=f"{t.get('id')} dominates {t.get('type')}/{sport} cohort ({share:.0%} of ${cohort_total:.0f})",
                summary=(
                    f"{direction} of ${pnl:+.2f} on {t.get('ticker')} represents "
                    f"{share:.0%} of the {LOOKBACK_DAYS}-day {t.get('type')}/{sport} cohort total. "
                    f"Investigate whether the strategy genuinely earned this edge or got lucky."
                ),
                evidence=evidence,
                suggested_action=(
                    "Read the decision log + entry/exit ticks. Was edge real, "
                    "or did model parameters happen to align?"
                ),
            ))
        return findings
