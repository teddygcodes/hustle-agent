"""counterfactual_hotspots: flag (skipped_by_gate, sport) buckets with high mean +CLV.

Direct automation of the manual ATP query that drove Session 38a (Apr 29) — when
a rejection gate has settled CFs averaging >5¢ CLV at >60% +CLV rate, the gate
is surrendering alpha and is a candidate for retuning. Survivorship guard
(n_no_won >= 3) ensures we're not just measuring leader-side wins.

Reads from clv.json, last 30d settled records (real OR counterfactual_settled).
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from .. import _sport_classifier
from ..findings import Finding
from .outlier_pnl import _parse_ts

MIN_CF_COUNT = 10
MIN_MEAN_CLV_CENTS = 5.0
MIN_POSITIVE_CLV_RATE = 0.60
MIN_NO_WON_COUNT = 3
LOOKBACK_DAYS = 30
HIGH_SEVERITY_MEAN_CENTS = 15.0


class CounterfactualHotspots:
    name = "counterfactual_hotspots"
    data_sources = ("clv",)

    def run(self, ctx) -> list[Finding]:
        cutoff = ctx.loaded_at - dt.timedelta(days=LOOKBACK_DAYS)
        buckets: dict[tuple[str, str | None], list[dict]] = defaultdict(list)

        for r in ctx.clv:
            if r.get("status") not in ("settled", "counterfactual_settled"):
                continue
            gate = r.get("skipped_by_gate")
            if gate is None:
                continue  # only counterfactuals carry skipped_by_gate; real trades skipped
            cents = r.get("clv_cents")
            if cents is None:
                continue
            ts = _parse_ts(r.get("settled_at") or r.get("recorded_at"))
            if ts is None or ts < cutoff:
                continue
            sport = _sport_classifier.sport_from_ticker_distinguished(r.get("ticker", ""))
            buckets[(gate, sport)].append(r)

        findings: list[Finding] = []
        for (gate, sport), rows in buckets.items():
            if len(rows) < MIN_CF_COUNT:
                continue
            cents_list = [float(r["clv_cents"]) for r in rows]
            mean_cents = statistics.mean(cents_list)
            if mean_cents < MIN_MEAN_CLV_CENTS:
                continue
            positive_count = sum(1 for c in cents_list if c > 0)
            positive_rate = positive_count / len(cents_list)
            if positive_rate < MIN_POSITIVE_CLV_RATE:
                continue
            n_no_won = sum(1 for r in rows if r.get("market_result") == "no")
            if n_no_won < MIN_NO_WON_COUNT:
                continue

            severity = "high" if mean_cents >= HIGH_SEVERITY_MEAN_CENTS else "notable"
            evidence = {
                "skip_reason": gate,
                "sport": sport,
                "count": len(rows),
                "mean_clv_cents": round(mean_cents, 2),
                "positive_clv_rate": round(positive_rate, 3),
                "n_no_won": n_no_won,
                "n_yes_won": sum(1 for r in rows if r.get("market_result") == "yes"),
                "lookback_days": LOOKBACK_DAYS,
                "_fingerprint_keys": ["skip_reason", "sport"],
            }
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=(
                    f"{gate}/{sport}: {len(rows)} settled CFs, "
                    f"mean CLV {mean_cents:+.1f}¢ at {int(positive_rate * 100)}% +CLV"
                ),
                summary=(
                    f"Rejection gate '{gate}' on {sport} settled {len(rows)} counterfactuals "
                    f"with mean CLV {mean_cents:+.1f}¢ ({int(positive_rate * 100)}% +CLV, "
                    f"n_no_won={n_no_won}). Gate may be surrendering alpha — candidate for "
                    f"retuning if the asymmetric-evidence pattern holds at higher n."
                ),
                evidence=evidence,
                suggested_action=(
                    f"Inspect '{gate}' threshold for {sport}. Pull all {len(rows)} settled "
                    f"CFs and verify n_no_won >= 5 floor before any retuning move "
                    f"(Session 30-followup discipline). Mirror Session 38a's hygiene checks."
                ),
            ))
        return findings
