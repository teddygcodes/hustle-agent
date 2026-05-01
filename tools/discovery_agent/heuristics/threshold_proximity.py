"""threshold_proximity: flag rejection-gates where many rejects fell within X% of threshold.

If 80% of `non_stable_below_weather_floor` rejects landed at no_ask_prob 0.91-0.93
(floor=0.93, band=5%), the floor may be slightly miscalibrated — those near-misses
are alpha we surrendered. Cross-references near-miss tickers against clv settled
records to estimate how much +CLV we'd have captured.

Reads from decisions.jsonl (last 14d, decision='reject') and clv.json.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .. import _sport_classifier
from ..findings import Finding
from .outlier_pnl import _parse_ts

# observed_field, threshold_field — both extracted from decision.extra
THRESHOLD_REASONS = {
    "edge_below_threshold": ("edge", "min_edge"),
    "non_stable_below_weather_floor": ("no_ask_prob", "floor"),
    "no_price_below_floor": ("no_ask_prob", "floor"),
}

PROXIMITY_BAND_PCT = 0.05
MIN_REJECTS_PER_BUCKET = 5
LOOKBACK_DAYS = 14


def _within_band(observed: float, threshold: float) -> bool:
    """Near-miss = the observed value fell within PROXIMITY_BAND_PCT of threshold.

    Direction-agnostic: applies to both edge_below (observed < threshold) and
    floor gates (observed < threshold). The band is computed as a fraction of
    threshold magnitude with a 0.01 floor to avoid div-zero on tiny thresholds.
    """
    if threshold is None:
        return False
    gap = abs(float(observed) - float(threshold))
    band = PROXIMITY_BAND_PCT * max(abs(float(threshold)), 0.01)
    # 1e-9 epsilon absorbs FP fuzz on the exact boundary — the heuristic is a
    # fuzzy near-miss concept, not a numerical equality check
    return gap <= band + 1e-9


class ThresholdProximity:
    name = "threshold_proximity"
    data_sources = ("decisions", "clv")

    def run(self, ctx) -> list[Finding]:
        cutoff = ctx.loaded_at - dt.timedelta(days=LOOKBACK_DAYS)
        today_iso = ctx.loaded_at.date().isoformat()

        # bucket near-misses by (reason, sport) — also stash all rejects for context
        near_miss: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
        all_rejects: dict[tuple[str, str | None], int] = defaultdict(int)

        for d in ctx.decisions:
            if d.get("decision") != "reject":
                continue
            reason = d.get("reason")
            if reason not in THRESHOLD_REASONS:
                continue
            ts = _parse_ts(d.get("ts"))
            if ts is None or ts < cutoff:
                continue
            extra = d.get("extra") or {}
            obs_field, thr_field = THRESHOLD_REASONS[reason]
            observed = extra.get(obs_field)
            threshold = extra.get(thr_field)
            if observed is None or threshold is None:
                continue
            sport = _sport_classifier.sport_from_ticker_distinguished(d.get("ticker", ""))
            key = (reason, sport)
            all_rejects[key] += 1
            if _within_band(observed, threshold):
                near_miss[key].append({
                    "ticker": d.get("ticker"),
                    "observed": observed,
                    "threshold": threshold,
                })

        # build a ticker -> +CLV count map for cross-reference
        clv_positive_by_ticker: dict[str, int] = defaultdict(int)
        for r in ctx.clv:
            if r.get("status") not in ("settled", "counterfactual_settled"):
                continue
            cents = r.get("clv_cents")
            if cents is None or cents <= 0:
                continue
            tk = r.get("ticker")
            if tk:
                clv_positive_by_ticker[tk] += 1

        findings: list[Finding] = []
        for (reason, sport), nm in near_miss.items():
            if len(nm) < MIN_REJECTS_PER_BUCKET:
                continue
            clv_positive_near_misses = sum(
                1 for r in nm if clv_positive_by_ticker.get(r["ticker"], 0) > 0
            )
            unique_tickers = sorted({r["ticker"] for r in nm if r["ticker"]})
            sample_tickers = unique_tickers[:5]
            obs_field, thr_field = THRESHOLD_REASONS[reason]
            evidence = {
                "reason": reason,
                "sport": sport,
                "date_bucket": today_iso,
                "near_miss_count": len(nm),
                "total_rejects_for_reason_sport": all_rejects[(reason, sport)],
                "unique_near_miss_tickers": len(unique_tickers),
                "clv_positive_near_miss_tickers": clv_positive_near_misses,
                "proximity_band_pct": PROXIMITY_BAND_PCT,
                "lookback_days": LOOKBACK_DAYS,
                "observed_field": obs_field,
                "threshold_field": thr_field,
                "sample_tickers": sample_tickers,
                "_fingerprint_keys": ["reason", "sport", "date_bucket"],
            }
            severity = "notable" if clv_positive_near_misses >= 3 else "info"
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=(
                    f"{reason}/{sport}: {len(nm)} near-miss rejects within "
                    f"{int(PROXIMITY_BAND_PCT * 100)}% of {thr_field} "
                    f"({clv_positive_near_misses} of those tickers later showed +CLV)"
                ),
                summary=(
                    f"In the last {LOOKBACK_DAYS}d, {len(nm)} {reason} rejects on {sport} "
                    f"fell within {int(PROXIMITY_BAND_PCT * 100)}% of {thr_field}. "
                    f"Of those near-miss tickers, {clv_positive_near_misses} subsequently "
                    f"appeared in clv with positive CLV — alpha we may be surrendering. "
                    f"Tunable: loosen the gate's threshold OR investigate per-sport variants."
                ),
                evidence=evidence,
                suggested_action=(
                    f"Inspect the {reason} threshold ({thr_field}). If the bot is rejecting "
                    f"{len(nm)} '{reason}' opportunities/{LOOKBACK_DAYS}d clustered against "
                    f"the boundary AND those tickers later printed +CLV, the gate may be too "
                    f"tight for {sport}. Cross-check decisions.jsonl extra.{obs_field} "
                    f"distribution before retuning."
                ),
            ))
        return findings
