"""cohort_emergence: flag (opp_type, sport, source) cohorts new in last 7d, absent in prior 30d.

Vocabulary policy: decisions.opp_type and paper_trades.type are KEPT SEPARATE.
Each cohort key includes the source name so the same string in different vocabularies
(e.g. 'vig_stack' from paper_trades vs 'vig_stack_futures' from decisions) never
collapses. The vocabulary mismatch IS the bug-pair signal we want to surface.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .outlier_pnl import _parse_ts, _sport_from_ticker
from ..findings import Finding

EMERGENCE_WINDOW_DAYS = 7
PRIOR_WINDOW_DAYS = 30
MIN_NEW_OPP_COUNT = 3


def _gather(records, source_name: str, type_field: str, ts_field: str, recent_cutoff, prior_cutoff):
    """Return (recent, prior) dicts: (opp_type, sport, source) -> list[record]."""
    recent: dict[tuple, list[dict]] = defaultdict(list)
    prior: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        opp = r.get(type_field)
        sport = _sport_from_ticker(r.get("ticker", ""))
        ts = _parse_ts(r.get(ts_field))
        if ts is None or opp is None:
            continue
        key = (opp, sport, source_name)
        if ts >= recent_cutoff:
            recent[key].append(r)
        elif ts >= prior_cutoff:
            prior[key].append(r)
    return recent, prior


class CohortEmergence:
    name = "cohort_emergence"
    data_sources = ("decisions", "paper_trades")

    def run(self, ctx) -> list[Finding]:
        now = ctx.loaded_at
        recent_cutoff = now - dt.timedelta(days=EMERGENCE_WINDOW_DAYS)
        prior_cutoff = now - dt.timedelta(days=EMERGENCE_WINDOW_DAYS + PRIOR_WINDOW_DAYS)

        d_recent, d_prior = _gather(
            ctx.decisions, "decisions", "opp_type", "ts", recent_cutoff, prior_cutoff,
        )
        p_recent, p_prior = _gather(
            ctx.paper_trades, "paper_trades", "type", "timestamp", recent_cutoff, prior_cutoff,
        )

        findings: list[Finding] = []
        for recent_map, prior_map in ((d_recent, d_prior), (p_recent, p_prior)):
            for key, recent_records in recent_map.items():
                if key in prior_map:
                    continue
                if len(recent_records) < MIN_NEW_OPP_COUNT:
                    continue
                opp_type, sport, source = key
                positive_pnl = sum(
                    float(r.get("pnl") or 0) for r in recent_records if (r.get("pnl") or 0) > 0
                )
                severity = "high" if positive_pnl > 0 else "notable"
                evidence = {
                    "opp_type": opp_type,
                    "sport": sport,
                    "source": source,
                    "recent_count": len(recent_records),
                    "recent_window_days": EMERGENCE_WINDOW_DAYS,
                    "prior_window_days": PRIOR_WINDOW_DAYS,
                    "positive_pnl_in_window": round(positive_pnl, 2),
                    "sample_tickers": sorted(
                        {r.get("ticker") for r in recent_records if r.get("ticker")}
                    )[:5],
                    "_fingerprint_keys": ["opp_type", "sport", "source"],
                }
                findings.append(Finding(
                    heuristic=self.name,
                    severity=severity,
                    title=(
                        f"NEW cohort: {opp_type}/{sport} in {source} "
                        f"({len(recent_records)} records, prior 30d=0)"
                    ),
                    summary=(
                        f"'{opp_type}' (sport={sport}) appeared {len(recent_records)} times in "
                        f"the last {EMERGENCE_WINDOW_DAYS}d in {source}, but ZERO times in the "
                        f"prior {PRIOR_WINDOW_DAYS}d. Either a real strategy emergence or a "
                        f"classifier change worth investigating."
                    ),
                    evidence=evidence,
                    suggested_action=(
                        f"Grep recent code/config changes for '{opp_type}'. Cross-check whether "
                        f"paper_trades.type matches: vocabulary mismatch is meaningful."
                    ),
                ))
        return findings
