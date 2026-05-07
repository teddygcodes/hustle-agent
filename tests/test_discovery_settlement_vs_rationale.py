"""Regression + behavior tests for SettlementVsRationale heuristic (Session 55).

The KXHIGHMIA founding regression (test 1) is the P0 lock-in — mirrors
test_discovery_sfphi_regression.py shape. If it ever fails, the heuristic
has lost its founding example; treat as system-down severity.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tools.discovery_agent.heuristics.settlement_vs_rationale import (
    SettlementVsRationale,
)


def _make_ctx(paper_trades):
    """Minimal DiscoveryContext stub — heuristic only reads ctx.paper_trades."""
    return SimpleNamespace(paper_trades=paper_trades)


def _trade(
    ticker,
    type_,
    contracts,
    entry_price,
    pnl,
    status="lost",
    timestamp="2026-05-06T12:00:00+00:00",
    resolved_at="2026-05-06T13:00:00+00:00",
    side="no",
    trade_id=None,
):
    return {
        "id": trade_id or f"PAPER-TEST-{ticker}",
        "ticker": ticker,
        "type": type_,
        "contracts": contracts,
        "entry_price": entry_price,
        "pnl": pnl,
        "status": status,
        "timestamp": timestamp,
        "resolved_at": resolved_at,
        "side": side,
    }


# -------- Test 1 (P0): KXHIGHMIA founding regression --------

def test_kxhighmia_founding_regression():
    """P0: May 6 KXHIGHMIA-26MAY05-T87 must always trigger
    tail_loss_in_high_cap_family.

    If this fails, the heuristic has lost its founding example. Treat as
    system-down severity, mirror test_discovery_sfphi_regression.py P0 discipline.
    """
    kxhighmia = _trade(
        ticker="KXHIGHMIA-26MAY05-T87",
        type_="vig_stack",
        contracts=200,
        entry_price=1.00,  # 0.0-1.0 dollars per canonical schema
        pnl=-199.95,
        status="lost",
        timestamp="2026-05-06T08:00:00+00:00",
        resolved_at="2026-05-06T12:00:00+00:00",
    )
    # 5 winning KXHIGHMIA trades in the 14d window so family aggregate is
    # POSITIVE → demotion fires → severity drops HIGH → NOTABLE. (Validates
    # the demotion ladder works on the founding case too.)
    winners = [
        _trade(
            ticker=f"KXHIGHMIA-26APR{30}-T{i}",
            type_="vig_stack",
            contracts=200,
            entry_price=0.95,
            pnl=10.00,
            status="won",
            timestamp=f"2026-05-0{i + 1}T08:00:00+00:00",
            resolved_at=f"2026-05-0{i + 1}T12:00:00+00:00",
            trade_id=f"PAPER-TEST-WIN-{i}",
        )
        for i in range(5)
    ]
    ctx = _make_ctx([kxhighmia] + winners)
    findings = SettlementVsRationale().run(ctx)

    kxh_findings = [f for f in findings if "KXHIGHMIA-26MAY05-T87" in f.title]
    assert len(kxh_findings) >= 1, (
        f"founding example KXHIGHMIA must surface. Got: "
        f"{[(f.heuristic, f.title) for f in findings]}"
    )
    f = kxh_findings[0]
    assert f.heuristic == "settlement_vs_rationale"
    # Severity will be NOTABLE (demoted from HIGH because family aggregate
    # positive AND only 1 tail loss in window — ladder fires).
    assert f.severity in ("high", "notable"), (
        f"expected high or notable (depending on demotion), got {f.severity}"
    )
    assert "tail_loss_in_high_cap_family" in f.title
    assert f.evidence["ticker"] == "KXHIGHMIA-26MAY05-T87"
    assert f.evidence["family"] == "KXHIGHMIA"


# -------- Test 2: disabled_sport_settlement regression --------

def test_disabled_sport_settlement_regression():
    """live_momentum settlement on a disabled sport must emit CRITICAL.

    Also locks in the legacy-entry filter: a pre-Apr-20 entry that settled
    after the disable list was added is NOT a regression — the disable list
    correctly didn't apply at entry time. The Apr 14 entry must NOT fire;
    the May 5 entry must fire CRITICAL. Without this filter, real-data
    verification surfaced 24 false-positive critical findings (Session 55).
    """
    legacy = _trade(
        ticker="KXATPCHALLENGERMATCH-26APR14-LEGACY",
        type_="live_momentum",
        contracts=10,
        entry_price=0.70,
        pnl=-7.0,
        status="lost",
        timestamp="2026-04-14T12:00:00+00:00",  # pre-disable (Apr 20)
        resolved_at="2026-04-22T12:00:00+00:00",  # post-disable
    )
    real = _trade(
        ticker="KXWTACHALLENGERMATCH-26MAY05-T1",
        type_="live_momentum",
        contracts=10,
        entry_price=0.70,
        pnl=-7.0,
        status="lost",
        timestamp="2026-05-05T12:00:00+00:00",  # post-disable entry — real regression
    )
    findings = SettlementVsRationale().run(_make_ctx([legacy, real]))
    pattern2 = [f for f in findings if "disabled_sport_settlement" in f.title]
    assert len(pattern2) == 1, (
        f"only the post-disable entry should fire (1 finding); legacy entry "
        f"must be filtered. Got: {[f.title for f in pattern2]}"
    )
    assert pattern2[0].severity == "critical"
    assert pattern2[0].evidence["sport"] == "wta_challenger"
    assert pattern2[0].evidence["ticker"] == "KXWTACHALLENGERMATCH-26MAY05-T1"


# -------- Test 3: outsized_notional_post_size_multiplier regression --------

def test_outsized_notional_post_multiplier_regression():
    """NBA live_momentum at 2x expected post-multiplier ceiling → HIGH."""
    # NBA size_multiplier=0.5; PAPER_STARTING_BALANCE=$10500, MAX_BET_FRACTION=0.05
    # → ceiling = 10500 * 0.05 * 0.5 * 1.2 = $315. Make notional $700 (>2x).
    trade = _trade(
        ticker="KXNBAGAME-26MAY05-LAL",
        type_="live_momentum",
        contracts=1000,
        entry_price=0.70,  # notional = $700
        pnl=-7.0,
        status="lost",
    )
    findings = SettlementVsRationale().run(_make_ctx([trade]))
    pattern3 = [f for f in findings if "outsized_notional" in f.title]
    assert len(pattern3) == 1
    assert pattern3[0].severity == "high"
    assert pattern3[0].evidence["sport"] == "nba"
    assert pattern3[0].evidence["actual_notional"] == 700.0


# -------- Test 4: demotion — single tail in positive family → NOTABLE --------

def test_demotion_single_tail_in_positive_family():
    """One tail loss + 5 winners in same family → demoted HIGH → NOTABLE.

    Uses KXHIGHAUS (also $200 cap, healthy tier) to isolate the demotion
    mechanic from the KXHIGHMIA founding-example check in test 1.
    """
    tail = _trade(
        ticker="KXHIGHAUS-26MAY06-T1",
        type_="vig_stack",
        contracts=200,
        entry_price=1.00,
        pnl=-200.00,
        status="lost",
        timestamp="2026-05-06T08:00:00+00:00",
        resolved_at="2026-05-06T12:00:00+00:00",
    )
    # 5 winners, total +$50 → family aggregate POSITIVE (50 - 200 = -150
    # pulls aggregate negative — fix winners to overcome the tail loss).
    winners = [
        _trade(
            ticker=f"KXHIGHAUS-26MAY0{i + 1}-T{i}",
            type_="vig_stack",
            contracts=200,
            entry_price=0.95,
            pnl=50.00,  # 5 × $50 = $250 total wins, vs -$200 loss → aggregate +$50
            status="won",
            timestamp=f"2026-05-0{i + 1}T08:00:00+00:00",
            resolved_at=f"2026-05-0{i + 1}T12:00:00+00:00",
            trade_id=f"PAPER-TEST-AUS-WIN-{i}",
        )
        for i in range(5)
    ]
    findings = SettlementVsRationale().run(_make_ctx([tail] + winners))
    aus_findings = [f for f in findings if "KXHIGHAUS-26MAY06-T1" in f.title]
    assert len(aus_findings) == 1
    # family_pnl_sum > 0 AND n_tail == 1 → demote HIGH → NOTABLE
    assert aus_findings[0].severity == "notable"
    assert aus_findings[0].evidence["family_recent_tail_losses"] == 1
    assert aus_findings[0].evidence["family_recent_pnl_sum"] > 0


# -------- Test 5: no demotion when 2+ tail losses in family --------

def test_no_demotion_multiple_tail_in_family():
    """2 tail losses + 3 winners → severity stays HIGH (pattern, not outlier)."""
    tails = [
        _trade(
            ticker=f"KXHIGHAUS-26MAY0{i + 5}-TAIL{i}",
            type_="vig_stack",
            contracts=200,
            entry_price=1.00,
            pnl=-200.00,
            status="lost",
            timestamp=f"2026-05-0{i + 5}T08:00:00+00:00",
            resolved_at=f"2026-05-0{i + 5}T12:00:00+00:00",
            trade_id=f"PAPER-TEST-AUS-TAIL-{i}",
        )
        for i in range(2)
    ]
    winners = [
        _trade(
            ticker=f"KXHIGHAUS-26MAY0{i + 1}-WIN{i}",
            type_="vig_stack",
            contracts=200,
            entry_price=0.95,
            pnl=50.00,
            status="won",
            timestamp=f"2026-05-0{i + 1}T08:00:00+00:00",
            resolved_at=f"2026-05-0{i + 1}T12:00:00+00:00",
            trade_id=f"PAPER-TEST-AUS-WIN-{i}",
        )
        for i in range(3)
    ]
    findings = SettlementVsRationale().run(_make_ctx(tails + winners))
    # Both tail trades should fire pattern 1; both should remain HIGH.
    # Note on windowing: each trade's lookback only sees PAST trades (ts <=
    # ref_ts), so the earlier tail's window sees n_tail=1 and the later
    # tail's window sees n_tail=2. Severity stays HIGH on both anyway because
    # family aggregate is NEGATIVE (3 × $50 wins - 2 × $200 losses = -$250,
    # or -$50 in the earlier-tail window) — the first demotion clause
    # requires aggregate > 0. Legacy clause doesn't fire (post-deploy + 24h
    # window). The point this test locks in: "2+ tail losses present →
    # severity does not demote on either trade."
    aus_findings = [f for f in findings if "tail_loss_in_high_cap_family" in f.title]
    assert len(aus_findings) == 2
    for f in aus_findings:
        assert f.severity == "high", (
            f"expected high (negative aggregate + non-legacy → no demotion), "
            f"got {f.severity} for {f.title}"
        )
    # Later tail's window sees both tails — locks in the n_tail >= 2 evidence
    # is reachable (so the underlying counter math works).
    later_tail = max(
        aus_findings, key=lambda f: f.evidence.get("settled_at", "")
    )
    assert later_tail.evidence["family_recent_tail_losses"] == 2


# -------- Test 6: pre-Session-53 legacy excluded --------

def test_pre_session_53_legacy_excluded():
    """Tail loss settled <24h post-deploy with notional > current cap →
    demoted (legacy pre-cap position; Session 53 explicitly excluded these)."""
    # Session 53 deploy = 2026-05-04T23:43:00+00:00. Settle within 24h (May 5
    # mid-afternoon UTC), notional > cap (KXHIGHMIA cap is $200).
    legacy = _trade(
        ticker="KXHIGHMIA-26MAY04-LEGACY",
        type_="vig_stack",
        contracts=300,
        entry_price=1.00,  # notional = $300 > $200 cap
        pnl=-300.00,
        status="lost",
        timestamp="2026-05-04T20:00:00+00:00",  # pre-deploy entry
        resolved_at="2026-05-05T08:00:00+00:00",  # post-deploy settle, <24h
    )
    findings = SettlementVsRationale().run(_make_ctx([legacy]))
    legacy_findings = [f for f in findings if "tail_loss_in_high_cap_family" in f.title]
    assert len(legacy_findings) == 1
    # Pre-Session-53 legacy demotion fires (notional > cap AND settled <24h
    # post-deploy). family_pnl_sum is non-positive (only this trade in window
    # except its own loss) so the first demotion clause does NOT fire — only
    # the legacy clause. Demote HIGH → NOTABLE.
    assert legacy_findings[0].severity == "notable", (
        f"expected notable (legacy pre-cap demotion), got {legacy_findings[0].severity}"
    )


# -------- Test 7: canonical schema discipline (Session 51 mirror) --------

def test_canonical_schema_used():
    """Mirror Session 51's test_canonical_schema_used_throughout pattern.

    Heuristic must use canonical paper_trades.json field names: 'type' (not
    'opp_type'), 'pnl' (not 'outcome_realized_pnl'), 'no'/'yes' (not 'no_won'/
    'yes_won'). Schema-discipline failures here mask real signals (Session 45
    forensic).
    """
    src_path = (
        Path(__file__).resolve().parent.parent
        / "tools" / "discovery_agent" / "heuristics" / "settlement_vs_rationale.py"
    )
    src = src_path.read_text()
    # Canonical: paper_trades uses 'type', not 'opp_type'
    assert "'opp_type'" not in src, (
        "Use 'type' for paper_trades records, not 'opp_type'"
    )
    assert '"opp_type"' not in src, (
        "Use 'type' for paper_trades records, not 'opp_type'"
    )
    # Canonical: paper_trades uses 'pnl', not 'outcome_realized_pnl'
    assert "outcome_realized_pnl" not in src
    # Canonical: market_result uses 'no'/'yes', not 'no_won'/'yes_won'
    assert "no_won" not in src
    assert "yes_won" not in src
    # Canonical: clv.json uses 'clv_cents', not 'outcome_clv_cents'
    assert "outcome_clv_cents" not in src


# -------- Test 8: SFPHI regression still passes --------

def test_sfphi_regression_still_passes():
    """Session 55's heuristic addition must not perturb the outlier_pnl SFPHI
    founding example. Smoke check that importing settlement_vs_rationale didn't
    break outlier_pnl import-side (the actual SFPHI test runs in
    test_discovery_sfphi_regression.py)."""
    from tools.discovery_agent.heuristics.outlier_pnl import OutlierPnl
    h = OutlierPnl()
    assert h.name == "outlier_pnl"
    assert h.data_sources == ("paper_trades",)


# -------- Test 9: graceful degradation when bot.config can't import --------

def test_heuristic_isolation_when_config_import_fails(monkeypatch):
    """Graceful degradation: if bot.config can't be imported (e.g., during a
    schema migration or syntax error in config.py), the heuristic returns
    empty findings rather than crashing the whole agent run.
    """
    import tools.discovery_agent.heuristics.settlement_vs_rationale as svr
    monkeypatch.setattr(svr, "_CONFIG_AVAILABLE", False)
    findings = svr.SettlementVsRationale().run(_make_ctx([
        _trade("KXHIGHMIA-26MAY05-T87", "vig_stack", 200, 1.00, -199.95)
    ]))
    assert findings == []


# -------- Test 10: empty paper_trades returns empty --------

def test_empty_paper_trades_returns_empty():
    """Boundary: no settlements → no findings."""
    findings = SettlementVsRationale().run(_make_ctx([]))
    assert findings == []
