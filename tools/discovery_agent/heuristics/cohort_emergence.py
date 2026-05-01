"""cohort_emergence: flag (opp_type, sport, source) cohorts new in last 7d, absent in prior 30d.

Vocabulary policy: decisions.opp_type and paper_trades.type are KEPT SEPARATE.
Each cohort key includes the source name so the same string in different vocabularies
(e.g. 'vig_stack' from paper_trades vs 'vig_stack_futures' from decisions) never
collapses. The vocabulary mismatch IS the bug-pair signal we want to surface.

Session 43b refinement (May 1 2026, post-Session-43-investigate finding):
- sport now uses _sport_classifier.sport_from_ticker_distinguished so KXMLB-* (futures)
  and KXMLBGAME-* (per-game) emit as separate cohorts (mlb_futures vs mlb_game).
- evidence carries unique_tickers_recent + accepts_recent + paper_trades_recent so a
  cohort with 1000 decisions / 0 accepts / 0 trades is distinguishable from a cohort
  with 5 decisions / 1 accept / 1 trade. Severity demotes to 'info' when accepts and
  paper_trades are both 0 — the cohort is interesting context, not actionable.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .. import _sport_classifier
from ..findings import Finding
from .outlier_pnl import _parse_ts

EMERGENCE_WINDOW_DAYS = 7
PRIOR_WINDOW_DAYS = 30
MIN_NEW_OPP_COUNT = 3


def _gather(records, source_name: str, type_field: str, ts_field: str, recent_cutoff, prior_cutoff):
    """Return (recent, prior) dicts: (opp_type, sport, source) -> list[record]."""
    recent: dict[tuple, list[dict]] = defaultdict(list)
    prior: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        opp = r.get(type_field)
        sport = _sport_classifier.sport_from_ticker_distinguished(r.get("ticker", ""))
        ts = _parse_ts(r.get(ts_field))
        if ts is None or opp is None:
            continue
        key = (opp, sport, source_name)
        if ts >= recent_cutoff:
            recent[key].append(r)
        elif ts >= prior_cutoff:
            prior[key].append(r)
    return recent, prior


def _count_paper_trades(paper_trades: list[dict], opp_type: str, sport: str | None,
                        recent_cutoff: dt.datetime) -> int:
    """Count paper_trades records matching this cohort by (type, sport) in recent window.

    Joins via paper_trades.type since paper_trades does not carry opp_type.
    Returns 0 when source is decisions (decisions.opp_type and paper_trades.type
    have intentionally distinct vocabularies — see vocabulary-policy note above).
    """
    n = 0
    for t in paper_trades:
        if t.get("type") != opp_type:
            continue
        if _sport_classifier.sport_from_ticker_distinguished(t.get("ticker", "")) != sport:
            continue
        ts = _parse_ts(t.get("timestamp"))
        if ts is None or ts < recent_cutoff:
            continue
        n += 1
    return n


def _count_accepts_in_decisions(decisions: list[dict], opp_type: str, sport: str | None,
                                recent_cutoff: dt.datetime) -> int:
    """Count decision='accept' records in recent window for this (opp_type, sport)."""
    n = 0
    for r in decisions:
        if r.get("decision") != "accept":
            continue
        if r.get("opp_type") != opp_type:
            continue
        if _sport_classifier.sport_from_ticker_distinguished(r.get("ticker", "")) != sport:
            continue
        ts = _parse_ts(r.get("ts"))
        if ts is None or ts < recent_cutoff:
            continue
        n += 1
    return n


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

                unique_tickers_recent = len(
                    {r.get("ticker") for r in recent_records if r.get("ticker")}
                )
                if source == "decisions":
                    accepts_recent = _count_accepts_in_decisions(
                        ctx.decisions, opp_type, sport, recent_cutoff,
                    )
                else:
                    # paper_trades records ARE accepts by construction
                    accepts_recent = len(recent_records)
                paper_trades_recent = _count_paper_trades(
                    ctx.paper_trades, opp_type, sport, recent_cutoff,
                )

                positive_pnl = sum(
                    float(r.get("pnl") or 0) for r in recent_records if (r.get("pnl") or 0) > 0
                )

                if accepts_recent == 0 and paper_trades_recent == 0:
                    severity = "info"
                else:
                    severity = "high" if positive_pnl > 0 else "notable"

                evidence = {
                    "opp_type": opp_type,
                    "sport": sport,
                    "source": source,
                    "recent_count": len(recent_records),
                    "unique_tickers_recent": unique_tickers_recent,
                    "accepts_recent": accepts_recent,
                    "paper_trades_recent": paper_trades_recent,
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
                        f"({len(recent_records)} records, {unique_tickers_recent} tickers, "
                        f"{accepts_recent} accepts, {paper_trades_recent} trades; prior 30d=0)"
                    ),
                    summary=(
                        f"'{opp_type}' (sport={sport}) appeared {len(recent_records)} times in "
                        f"the last {EMERGENCE_WINDOW_DAYS}d in {source} across "
                        f"{unique_tickers_recent} unique tickers ({accepts_recent} accepts, "
                        f"{paper_trades_recent} settled paper trades), but ZERO times in the "
                        f"prior {PRIOR_WINDOW_DAYS}d. Either a real strategy emergence or a "
                        f"classifier change worth investigating."
                    ),
                    evidence=evidence,
                    suggested_action=(
                        f"{opp_type} emerged across {sport}: {len(recent_records)} decisions, "
                        f"{unique_tickers_recent} unique tickers, {accepts_recent} accepts, "
                        f"{paper_trades_recent} trades. Investigate whether broader bug-pair "
                        f"surface or singleton (cf SFPHI investigation, May 1 2026)."
                    ),
                ))
        return findings
