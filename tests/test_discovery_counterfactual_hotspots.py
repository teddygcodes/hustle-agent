"""counterfactual_hotspots heuristic — gate-level CF +CLV hotspot detector."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tools.discovery_agent.heuristics.counterfactual_hotspots import (
    CounterfactualHotspots,
    MIN_CF_COUNT,
)


_NOW = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)


def _ctx(clv=None):
    return SimpleNamespace(clv=clv or [], loaded_at=_NOW)


def _cf(ticker, gate, clv_cents, market_result="yes", days_ago=1):
    ts = _NOW - dt.timedelta(days=days_ago)
    return {
        "ticker": ticker, "status": "counterfactual_settled",
        "skipped_by_gate": gate, "clv_cents": clv_cents,
        "market_result": market_result, "settled_at": ts.isoformat(),
        "recorded_at": ts.isoformat(),
    }


def test_counterfactual_hotspots_positive():
    """10 CFs, mean=10¢, 80% +CLV, n_no_won=4 → fires notable."""
    rows = []
    # 8 wins (+15¢ each, market_result=yes)
    rows += [_cf(f"KXATPMATCH-T{i}", "no_leader", 15, market_result="yes")
             for i in range(8)]
    # 2 wins (+5¢ each) — wait we need more variety. Let me restructure
    rows = []
    rows += [_cf(f"KXATPMATCH-T{i}", "no_leader", 15, market_result="yes") for i in range(6)]
    # 4 leader-loss settlements (n_no_won=4 — passes the >=3 floor)
    rows += [_cf(f"KXATPMATCH-N{i}", "no_leader", 5, market_result="no") for i in range(4)]
    # mean = (6*15 + 4*5)/10 = 110/10 = 11.0¢; +CLV rate = 10/10 = 100%
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "notable"
    assert f.evidence["skip_reason"] == "no_leader"
    assert f.evidence["sport"] == "atp"
    assert f.evidence["count"] == 10
    assert f.evidence["n_no_won"] == 4


def test_counterfactual_hotspots_high_severity_at_15c_mean():
    """Mean CLV >= 15¢ → severity 'high'."""
    rows = [_cf(f"KXATPMATCH-T{i}", "no_leader", 20, market_result="yes") for i in range(7)]
    rows += [_cf(f"KXATPMATCH-N{i}", "no_leader", 15, market_result="no") for i in range(3)]
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert findings[0].severity == "high"


def test_counterfactual_hotspots_negative_below_min_count():
    """Fewer than 10 CFs → no finding."""
    rows = [_cf(f"KXATPMATCH-T{i}", "no_leader", 15, market_result="yes")
            for i in range(MIN_CF_COUNT - 1)]
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert findings == []


def test_counterfactual_hotspots_negative_below_mean_threshold():
    """Mean CLV < 5¢ → no finding."""
    rows = [_cf(f"KXATPMATCH-T{i}", "no_leader", 2, market_result="yes") for i in range(7)]
    rows += [_cf(f"KXATPMATCH-N{i}", "no_leader", 1, market_result="no") for i in range(4)]
    # mean ~1.6¢, way below 5¢
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert findings == []


def test_counterfactual_hotspots_negative_below_positive_rate():
    """+CLV rate < 60% → no finding."""
    rows = [_cf(f"KXATPMATCH-T{i}", "no_leader", 30, market_result="yes") for i in range(5)]
    rows += [_cf(f"KXATPMATCH-T{i}", "no_leader", -2, market_result="yes") for i in range(6)]
    # +CLV rate = 5/11 = 45%
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert findings == []


def test_counterfactual_hotspots_negative_survivorship():
    """n_no_won < 3 → no finding (sample biased toward leader-side wins)."""
    rows = [_cf(f"KXATPMATCH-T{i}", "no_leader", 10, market_result="yes") for i in range(11)]
    # 0 no-side wins; survivorship guard fires
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert findings == []


def test_counterfactual_hotspots_boundary_at_min_count():
    """Exactly MIN_CF_COUNT (10) CFs at all other thresholds met → fires."""
    rows = [_cf(f"KXATPMATCH-T{i}", "no_leader", 10, market_result="yes") for i in range(7)]
    rows += [_cf(f"KXATPMATCH-N{i}", "no_leader", 5, market_result="no") for i in range(3)]
    # n=10, mean=8.5¢, +CLV=100%, n_no_won=3 — all at floors
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert len(findings) == 1


def test_counterfactual_hotspots_skips_real_settled_records():
    """Real-trade settled records have skipped_by_gate=None → ignored."""
    rows = [
        {"ticker": f"KXATPMATCH-T{i}", "status": "settled", "skipped_by_gate": None,
         "clv_cents": 20, "market_result": "yes",
         "settled_at": (_NOW - dt.timedelta(days=1)).isoformat()}
        for i in range(MIN_CF_COUNT + 5)
    ]
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert findings == []


def test_counterfactual_hotspots_skips_records_outside_lookback():
    """Settled records older than 30d → ignored."""
    rows = [_cf(f"KXATPMATCH-T{i}", "no_leader", 15, market_result="yes", days_ago=60)
            for i in range(MIN_CF_COUNT + 5)]
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert findings == []


def test_counterfactual_hotspots_groups_by_gate_and_sport():
    """Same gate, two sports → two findings."""
    rows = []
    rows += [_cf(f"KXATPMATCH-T{i}", "no_leader", 15, market_result="yes") for i in range(7)]
    rows += [_cf(f"KXATPMATCH-N{i}", "no_leader", 5, market_result="no") for i in range(3)]
    rows += [_cf(f"KXNHLGAME-T{i}", "no_leader", 15, market_result="yes") for i in range(7)]
    rows += [_cf(f"KXNHLGAME-N{i}", "no_leader", 5, market_result="no") for i in range(3)]
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    sports = sorted(f.evidence["sport"] for f in findings)
    assert sports == ["atp", "nhl_game"]


def test_counterfactual_hotspots_missing_source_returns_empty():
    findings = CounterfactualHotspots().run(_ctx())
    assert findings == []
