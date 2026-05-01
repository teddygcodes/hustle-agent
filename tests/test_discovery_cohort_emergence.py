"""cohort_emergence — flag (opp_type, sport, source) cohorts new in last 7d, absent in prior 30d.

Session 43b refinement tests live alongside the original Session 43a tests:
- sport classification distinguishes futures from per-game (mlb_game vs mlb_futures)
- severity demotes to 'info' when accepts_recent == 0 AND paper_trades_recent == 0
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tools.discovery_agent.heuristics.cohort_emergence import CohortEmergence


def _ctx(decisions=None, paper_trades=None):
    return SimpleNamespace(
        decisions=decisions or [],
        paper_trades=paper_trades or [],
        cutoff_days=30,
        loaded_at=dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc),
    )


def _decision(ts_days_ago, opp_type, ticker, decision="reject", reason="x"):
    ts = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=ts_days_ago)
    return {
        "ts": ts.isoformat(), "opp_type": opp_type, "ticker": ticker,
        "decision": decision, "reason": reason,
    }


def test_vig_stack_futures_emergence_in_decisions():
    recent = [_decision(1, "vig_stack_futures", "KXMLBGAME-26APR29-X") for _ in range(3)]
    prior = [_decision(15, "vig_stack_series", "KXMLBGAME-26APR15-Y") for _ in range(5)]
    findings = CohortEmergence().run(_ctx(decisions=recent + prior))
    vsf = [f for f in findings if f.evidence["opp_type"] == "vig_stack_futures"]
    assert len(vsf) == 1
    assert vsf[0].evidence["sport"] == "mlb_game"  # KXMLBGAME → mlb_game (Session 43b)
    assert vsf[0].evidence["source"] == "decisions"
    assert vsf[0].evidence["recent_count"] == 3
    # All 3 are decision='reject' so accepts_recent==0 and no paper_trades
    # → severity demotes to 'info' per Session 43b refinement
    assert vsf[0].severity == "info"


def test_no_finding_below_min_count():
    recent = [_decision(1, "vig_stack_futures", "KXMLBGAME-26APR29-X") for _ in range(2)]  # < 3
    prior = [_decision(15, "vig_stack_series", "KXMLBGAME-26APR15-Y")]
    findings = CohortEmergence().run(_ctx(decisions=recent + prior))
    assert [f for f in findings if f.evidence["opp_type"] == "vig_stack_futures"] == []


def test_no_finding_when_present_in_prior_window():
    """If cohort existed before, it's not emergent — even if 3+ recent."""
    recent = [_decision(1, "vig_stack_futures", "KXMLBGAME-26APR29-X") for _ in range(5)]
    prior = [_decision(15, "vig_stack_futures", "KXMLBGAME-26APR15-Y")]
    findings = CohortEmergence().run(_ctx(decisions=recent + prior))
    assert [f for f in findings if f.evidence["opp_type"] == "vig_stack_futures"] == []


def test_paper_trades_use_type_field_not_opp_type():
    """paper_trades.type vocabulary is separate from decisions.opp_type."""
    pt = [
        {"id": f"P{i}", "ticker": "KXMLBGAME-26APR29-X", "type": "live_momentum",
         "timestamp": (dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=1)).isoformat()}
        for i in range(3)
    ]
    pt += [
        {"id": "OLD", "ticker": "KXMLBGAME-26APR15-Y", "type": "vig_stack",
         "timestamp": (dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=15)).isoformat()}
    ]
    findings = CohortEmergence().run(_ctx(paper_trades=pt))
    lm = [f for f in findings
          if f.evidence["opp_type"] == "live_momentum" and f.evidence["source"] == "paper_trades"]
    assert len(lm) == 1


def test_empty_sources_returns_empty():
    assert CohortEmergence().run(_ctx()) == []


# ----- Session 43b refinement tests -----


def test_cohort_emergence_distinguishes_futures_from_per_game():
    """KXMLB-* (futures) and KXMLBGAME-* (per-game) emit as separate cohorts."""
    futures = [_decision(1, "vig_stack_futures", f"KXMLB-26-D{i}") for i in range(4)]
    per_game = [_decision(1, "vig_stack_futures", f"KXMLBGAME-26APR29-G{i}") for i in range(4)]
    # No prior records for either cohort
    findings = CohortEmergence().run(_ctx(decisions=futures + per_game))
    sports = sorted({
        f.evidence["sport"] for f in findings
        if f.evidence["opp_type"] == "vig_stack_futures"
    })
    assert sports == ["mlb_futures", "mlb_game"]


def test_cohort_emergence_high_decisions_zero_accepts_is_info():
    """1000 decisions, 0 accepts, 0 paper_trades → severity demotes to 'info'."""
    recent = [_decision(1, "vig_stack_futures", f"KXMLB-26-T{i % 10}", decision="reject")
              for i in range(1000)]
    findings = CohortEmergence().run(_ctx(decisions=recent))
    vsf = [f for f in findings if f.evidence["opp_type"] == "vig_stack_futures"]
    assert len(vsf) == 1
    assert vsf[0].severity == "info"
    assert vsf[0].evidence["accepts_recent"] == 0
    assert vsf[0].evidence["paper_trades_recent"] == 0


def test_cohort_emergence_some_accepts_is_notable_or_higher():
    """5 decisions / 1 accept → severity is 'notable' or 'high', not 'info'."""
    recent = [_decision(1, "vig_stack_futures", "KXMLBGAME-26APR29-X", decision="reject")
              for _ in range(4)]
    recent.append(_decision(1, "vig_stack_futures", "KXMLBGAME-26APR29-X",
                            decision="accept", reason="all_gates_passed"))
    findings = CohortEmergence().run(_ctx(decisions=recent))
    vsf = [f for f in findings if f.evidence["opp_type"] == "vig_stack_futures"]
    assert len(vsf) == 1
    assert vsf[0].severity in ("notable", "high")
    assert vsf[0].evidence["accepts_recent"] == 1


def test_unique_tickers_recent_counts_distinct_only():
    """5 records on 2 unique tickers → unique_tickers_recent == 2."""
    recent = [_decision(1, "vig_stack_futures", "KXMLB-26-A") for _ in range(3)]
    recent += [_decision(1, "vig_stack_futures", "KXMLB-26-B") for _ in range(2)]
    findings = CohortEmergence().run(_ctx(decisions=recent))
    vsf = [f for f in findings if f.evidence["opp_type"] == "vig_stack_futures"]
    assert len(vsf) == 1
    assert vsf[0].evidence["unique_tickers_recent"] == 2
    assert vsf[0].evidence["recent_count"] == 5


def test_paper_trades_recent_count_joins_via_type_and_sport():
    """paper_trades_recent matches paper_trades.type + same sport in window."""
    decisions = [_decision(1, "vig_stack", "KXMLBGAME-26APR29-X") for _ in range(3)]
    paper_trades = [
        {"id": "P1", "ticker": "KXMLBGAME-26APR29-X", "type": "vig_stack",
         "timestamp": (dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=1)).isoformat()},
        {"id": "P2", "ticker": "KXMLBGAME-26APR29-Y", "type": "vig_stack",
         "timestamp": (dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=2)).isoformat()},
        # different sport (futures) — shouldn't match the per-game decisions cohort
        {"id": "P3", "ticker": "KXMLB-26-A", "type": "vig_stack",
         "timestamp": (dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=1)).isoformat()},
    ]
    findings = CohortEmergence().run(_ctx(decisions=decisions, paper_trades=paper_trades))
    decisions_finding = [
        f for f in findings
        if f.evidence["opp_type"] == "vig_stack" and f.evidence["source"] == "decisions"
    ]
    assert len(decisions_finding) == 1
    # 2 paper trades on KXMLBGAME-* (mlb_game), 1 on KXMLB-* (mlb_futures, different cohort)
    assert decisions_finding[0].evidence["paper_trades_recent"] == 2
