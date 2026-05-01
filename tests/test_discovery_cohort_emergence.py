"""cohort_emergence — flag (opp_type, sport, source) cohorts new in last 7d, absent in prior 30d."""

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


def _decision(ts_days_ago, opp_type, ticker):
    ts = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=ts_days_ago)
    return {"ts": ts.isoformat(), "opp_type": opp_type, "ticker": ticker}


def test_vig_stack_futures_emergence_in_decisions():
    recent = [_decision(1, "vig_stack_futures", "KXMLBGAME-26APR29-X") for _ in range(3)]
    prior = [_decision(15, "vig_stack_series", "KXMLBGAME-26APR15-Y") for _ in range(5)]
    findings = CohortEmergence().run(_ctx(decisions=recent + prior))
    vsf = [f for f in findings if f.evidence["opp_type"] == "vig_stack_futures"]
    assert len(vsf) == 1
    assert vsf[0].evidence["sport"] == "mlb"
    assert vsf[0].evidence["source"] == "decisions"
    assert vsf[0].evidence["recent_count"] == 3
    assert vsf[0].severity in ("notable", "high")


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
