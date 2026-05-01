"""outlier_pnl heuristic — flag trades dominating their (type, sport) cohort."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tools.discovery_agent.heuristics.outlier_pnl import OutlierPnl


def _ctx(paper_trades):
    return SimpleNamespace(
        paper_trades=paper_trades,
        cutoff_days=30,
        loaded_at=dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc),
    )


def _trade(tid, ticker, ttype, pnl, status="exited_early", days_ago=1):
    ts = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=days_ago)
    return {
        "id": tid, "ticker": ticker, "type": ttype, "status": status,
        "pnl": pnl, "timestamp": ts.isoformat(),
        "resolved_at": ts.isoformat(),
    }


def test_sfphi_record_is_high_severity_outlier():
    sfphi = _trade("PAPER-4A16F5D2", "KXMLBGAME-26APR291840SFPHI-PHI", "vig_stack", 172.52)
    other = _trade("X1", "KXMLBGAME-26APR15-X", "live_momentum", 10.0)
    findings = OutlierPnl().run(_ctx([sfphi, other]))
    sfphi_finding = [f for f in findings if f.evidence.get("trade_id") == "PAPER-4A16F5D2"]
    assert len(sfphi_finding) == 1
    assert sfphi_finding[0].severity == "high"
    assert sfphi_finding[0].evidence["sport"] == "mlb"
    assert sfphi_finding[0].evidence["opp_type"] == "vig_stack"


def test_no_finding_below_dollar_threshold():
    small = _trade("S1", "KXMLBGAME-26APR15-X", "vig_stack", 50.0)
    findings = OutlierPnl().run(_ctx([small]))
    assert findings == []


def test_no_finding_when_diluted_in_cohort():
    """If trade is $100 but cohort total is $1000, the trade is only 10% — below 30% threshold."""
    big = _trade("B1", "KXMLBGAME-26APR15-X", "vig_stack", 100.0)
    fillers = [_trade(f"F{i}", "KXMLBGAME-26APR15-X", "vig_stack", 50.0, days_ago=i+2) for i in range(20)]
    findings = OutlierPnl().run(_ctx([big] + fillers))
    assert [f for f in findings if f.evidence["trade_id"] == "B1"] == []


def test_skips_unsettled_trades():
    open_trade = _trade("O1", "KXMLBGAME-26APR15-X", "vig_stack", 200.0, status="open")
    findings = OutlierPnl().run(_ctx([open_trade]))
    assert findings == []


def test_skips_trades_outside_lookback():
    old = _trade("OLD", "KXMLBGAME-26APR15-X", "vig_stack", 200.0, days_ago=60)
    findings = OutlierPnl().run(_ctx([old]))
    assert findings == []


def test_severity_notable_at_30_to_50_pct():
    a = _trade("A", "KXMLBGAME-26APR15-X", "vig_stack", 100.0)
    b = _trade("B", "KXMLBGAME-26APR15-X", "vig_stack", -100.0, days_ago=2)
    c = _trade("C", "KXMLBGAME-26APR15-X", "vig_stack", -100.0, days_ago=3)
    findings = OutlierPnl().run(_ctx([a, b, c]))
    a_finding = [f for f in findings if f.evidence["trade_id"] == "A"]
    assert a_finding and a_finding[0].severity == "notable"


def test_missing_paper_trades_returns_empty():
    findings = OutlierPnl().run(_ctx([]))
    assert findings == []
