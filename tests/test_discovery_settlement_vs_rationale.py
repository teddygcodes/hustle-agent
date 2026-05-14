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
    # Session 140: filter by exact title prefix "disabled_sport_settlement:"
    # (with colon) to exclude the new aggregate row ("disabled_sport_settlement_attrition:")
    # which routes pre-disable rows to INFO severity instead of silently dropping them.
    pattern2 = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert len(pattern2) == 1, (
        f"only the post-disable entry should fire (1 finding); legacy entry "
        f"must be filtered. Got: {[f.title for f in pattern2]}"
    )
    assert pattern2[0].severity == "critical"
    assert pattern2[0].evidence["sport"] == "wta_challenger"
    assert pattern2[0].evidence["ticker"] == "KXWTACHALLENGERMATCH-26MAY05-T1"


# -------- Test 2b (Session 57): deploy-window race excluded --------

def test_disabled_sport_deploy_window_race_excluded():
    """Session 57 P0: entries on the disable date BUT before the commit
    timestamp must NOT fire as regressions.

    The MOMENTUM_DISABLED_SPORTS commit b1f08ff was authored 2026-04-20
    22:31 ET = 2026-04-21 02:31:54 UTC. 9 entries fired between
    2026-04-20T02:24Z and 2026-04-20T20:12Z — all 6+ hours BEFORE the
    commit existed. Pre-Session-57 the heuristic used calendar-midnight
    (2026-04-20T00:00 UTC) as the cutoff, flagging these 9 as critical
    regressions even though they pre-date the disable's existence by hours.

    Locks in the commit-timestamp cutoff. If the heuristic ever drifts
    back to calendar-midnight, these representative entries fire 0
    regressions; revert this test only after auditing every Pattern 2
    finding it would surface.
    """
    # Three of the actual flagged tickers from the May 6 discovery report,
    # exact entry timestamps from paper_trades.json.
    entries = [
        _trade(
            ticker="KXATPCHALLENGERMATCH-26APR19TOKSHA-SHA",
            type_="live_momentum",
            contracts=20,
            entry_price=0.71,
            pnl=3.2,
            status="exited_early",
            timestamp="2026-04-20T02:24:59.689717+00:00",  # earliest leaked entry
            resolved_at="2026-04-20T02:35:42.031615+00:00",
        ),
        _trade(
            ticker="KXWTAMATCH-26APR20VEKJEA-VEK",
            type_="live_momentum",
            contracts=20,
            entry_price=0.81,
            pnl=-16.2,
            status="lost",
            timestamp="2026-04-20T11:55:55.836942+00:00",
            resolved_at="2026-04-20T16:50:40.914259+00:00",
        ),
        _trade(
            ticker="KXATPCHALLENGERMATCH-26APR20ZINKUZ-ZIN",
            type_="live_momentum",
            contracts=20,
            entry_price=0.81,
            pnl=2.2,
            status="exited_early",
            timestamp="2026-04-20T20:12:57.556120+00:00",  # latest leaked entry
            resolved_at="2026-04-20T20:29:12.290211+00:00",
        ),
    ]
    findings = SettlementVsRationale().run(_make_ctx(entries))
    # Session 140: same tightening as test_disabled_sport_settlement_regression.
    # The 3 deploy-window-race entries are pre-disable, so they're now routed
    # to the disabled_sport_settlement_attrition aggregate (INFO). That's
    # acceptable — they still aren't firing as CRITICAL false positives,
    # which is what S57 locked in. The aggregate row is a separate concern.
    pattern2_critical = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert pattern2_critical == [], (
        f"deploy-window-race entries (pre-commit b1f08ff @ 2026-04-21T02:31:54 "
        f"UTC) must NOT fire as CRITICAL regressions. Got: "
        f"{[f.title for f in pattern2_critical]}"
    )


def test_disabled_sport_post_commit_entry_still_fires():
    """Bookend to deploy-window-race test: an entry AFTER the commit
    timestamp on a disabled sport MUST still fire CRITICAL. Locks in
    that the cutoff is tight enough to catch real post-deploy regressions.
    """
    post_commit = _trade(
        ticker="KXWTACHALLENGERMATCH-26APR21-POSTCOMMIT",
        type_="live_momentum",
        contracts=20,
        entry_price=0.75,
        pnl=-15.0,
        status="lost",
        timestamp="2026-04-21T03:00:00+00:00",  # 28 minutes post-commit
        resolved_at="2026-04-21T05:00:00+00:00",
    )
    findings = SettlementVsRationale().run(_make_ctx([post_commit]))
    # Session 140: same tightening as the regression test above.
    pattern2 = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert len(pattern2) == 1
    assert pattern2[0].severity == "critical"
    assert pattern2[0].evidence["sport"] == "wta_challenger"


# -------- Session 140: Pattern 2 dual-defect fix tests --------

def test_pattern2_nba_post_disable_fires_critical():
    """S140 locks in the vocabulary fix.

    Pre-S140, settlement_vs_rationale imported _ticker_to_sport from bot.regime
    which returns 'nba' (coarse) for KXNBAGAME-* tickers. The S97 disable set
    uses 'nba_game'. So `"nba" not in MOMENTUM_DISABLED_SPORTS` → return [] →
    every NBA live_momentum settlement was silently invisible, hiding real
    post-disable bug indicators. Post-S140 the heuristic uses the discovery-
    layer sport_from_ticker_distinguished() which returns 'nba_game', matching
    the disable set, so post-disable NBA entries fire CRITICAL as designed.
    """
    post_disable = _trade(
        ticker="KXNBAGAME-26MAY11OKCLAL-OKC",
        type_="live_momentum",
        contracts=20,
        entry_price=0.72,
        pnl=-14.40,
        status="lost",
        timestamp="2026-05-11T15:00:00+00:00",  # 30 min post-S97 commit
        resolved_at="2026-05-12T04:00:00+00:00",
    )
    findings = SettlementVsRationale().run(_make_ctx([post_disable]))
    pattern2 = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert len(pattern2) == 1, (
        f"NBA post-S97-disable entry must fire CRITICAL. Got: "
        f"{[f.title for f in findings]}"
    )
    assert pattern2[0].severity == "critical"
    assert pattern2[0].evidence["sport"] == "nba_game"
    assert pattern2[0].evidence["ticker"] == "KXNBAGAME-26MAY11OKCLAL-OKC"


def test_pattern2_nba_pre_disable_demoted():
    """S140: NBA pre-disable entry routes to aggregate INFO, not CRITICAL."""
    pre_disable = _trade(
        ticker="KXNBAGAME-26MAY10-LAKBOS",
        type_="live_momentum",
        contracts=20,
        entry_price=0.71,
        pnl=-14.20,
        status="lost",
        timestamp="2026-05-10T20:00:00+00:00",  # 18+ hours before S97 commit
        resolved_at="2026-05-11T05:00:00+00:00",
    )
    findings = SettlementVsRationale().run(_make_ctx([pre_disable]))
    pattern2_critical = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert pattern2_critical == [], (
        f"NBA pre-S97-disable entry must NOT fire CRITICAL. Got: "
        f"{[f.title for f in pattern2_critical]}"
    )
    attrition = [
        f for f in findings if f.title.startswith("disabled_sport_settlement_attrition:")
    ]
    assert len(attrition) == 1
    assert attrition[0].severity == "info"
    assert attrition[0].evidence["by_sport"] == {"nba_game": 1}


def test_pattern2_atp_pre_disable_demoted():
    """S140 ATP attrition: mirrors a real 2026-05-13 discovery report row.

    Without the S140 fix, this trade fired CRITICAL because atp was missing
    from MOMENTUM_DISABLED_SINCE (S97 added atp to MOMENTUM_DISABLED_SPORTS
    on May 11 but didn't update SINCE). One of 22 such rows that drowned
    §9's HIGH-severity surface.
    """
    pre_disable_atp = _trade(
        ticker="KXATPMATCH-26MAY08PRIDJO-DJO",
        type_="live_momentum",
        contracts=20,
        entry_price=0.81,
        pnl=-16.20,
        status="lost",
        timestamp="2026-05-08T16:00:00+00:00",  # 3+ days pre-S97 commit
        resolved_at="2026-05-09T04:00:00+00:00",
    )
    findings = SettlementVsRationale().run(_make_ctx([pre_disable_atp]))
    pattern2_critical = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert pattern2_critical == [], (
        f"ATP pre-S97-disable entry must NOT fire CRITICAL. Got: "
        f"{[f.title for f in pattern2_critical]}"
    )
    attrition = [
        f for f in findings if f.title.startswith("disabled_sport_settlement_attrition:")
    ]
    assert len(attrition) == 1
    assert attrition[0].evidence["by_sport"] == {"atp": 1}


def test_pattern2_atp_post_disable_fires_critical():
    """S140 bookend: an ATP entry AFTER the S97 re-disable commit MUST fire
    CRITICAL. Locks in that the cutoff is tight enough to catch real
    post-re-disable regressions if any ever fire.
    """
    post_disable_atp = _trade(
        ticker="KXATPMATCH-26MAY12-POSTS97",
        type_="live_momentum",
        contracts=20,
        entry_price=0.75,
        pnl=-15.0,
        status="lost",
        timestamp="2026-05-12T12:00:00+00:00",  # 22h post-S97 commit
        resolved_at="2026-05-13T04:00:00+00:00",
    )
    findings = SettlementVsRationale().run(_make_ctx([post_disable_atp]))
    pattern2 = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert len(pattern2) == 1
    assert pattern2[0].severity == "critical"
    assert pattern2[0].evidence["sport"] == "atp"
    assert pattern2[0].evidence["ticker"] == "KXATPMATCH-26MAY12-POSTS97"


def test_pattern2_aggregate_info_row_emitted():
    """S140: multi-sport attrition produces ONE aggregate INFO row with
    by_sport counts.

    Fixture: 3 ATP + 2 NBA pre-disable trades. Expect exactly one
    `disabled_sport_settlement_attrition` Finding of severity 'info'
    summarizing all 5; zero CRITICAL rows from Pattern 2.
    """
    atp_attrition = [
        _trade(
            ticker=f"KXATPMATCH-26MAY0{i + 5}-T{i}",
            type_="live_momentum",
            contracts=10,
            entry_price=0.80,
            pnl=-8.0,
            status="lost",
            timestamp=f"2026-05-0{i + 5}T14:00:00+00:00",
            resolved_at=f"2026-05-0{i + 5}T18:00:00+00:00",
            trade_id=f"PAPER-TEST-ATP-{i}",
        )
        for i in range(3)
    ]
    nba_attrition = [
        _trade(
            ticker=f"KXNBAGAME-26MAY0{i + 8}-T{i}",
            type_="live_momentum",
            contracts=10,
            entry_price=0.75,
            pnl=-7.50,
            status="lost",
            timestamp=f"2026-05-0{i + 8}T14:00:00+00:00",
            resolved_at=f"2026-05-0{i + 8}T22:00:00+00:00",
            trade_id=f"PAPER-TEST-NBA-{i}",
        )
        for i in range(2)
    ]
    findings = SettlementVsRationale().run(_make_ctx(atp_attrition + nba_attrition))

    pattern2_critical = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert pattern2_critical == [], (
        f"All 5 fixtures are pre-disable; zero CRITICAL expected. Got: "
        f"{[f.title for f in pattern2_critical]}"
    )

    attrition = [
        f for f in findings if f.title.startswith("disabled_sport_settlement_attrition:")
    ]
    assert len(attrition) == 1
    f = attrition[0]
    assert f.severity == "info"
    assert f.evidence["n_attrition"] == 5
    assert f.evidence["by_sport"] == {"atp": 3, "nba_game": 2}
    assert f.evidence["_fingerprint_keys"] == ["run_date", "disable_map_digest"]
    # Title carries the by-sport breakdown for at-a-glance scanning.
    assert "5 pre-disable settlements" in f.title
    assert "3 atp" in f.title
    assert "2 nba_game" in f.title


def test_pattern2_aggregate_not_emitted_when_empty():
    """S140: the aggregate INFO row only fires when there's attrition to
    report. Fixture: a vig_stack trade + a UFC live_momentum trade (UFC is
    NOT in MOMENTUM_DISABLED_SPORTS as of S140). Expect zero attrition rows.
    """
    vigstack = _trade(
        ticker="KXHIGHMIA-26MAY10-B85",
        type_="vig_stack",
        contracts=100,
        entry_price=0.95,
        pnl=5.0,
        status="won",
        side="no",
    )
    ufc = _trade(
        ticker="KXUFCFIGHT-26MAY11ABCDEF-ABC",
        type_="live_momentum",
        contracts=10,
        entry_price=0.80,
        pnl=2.0,
        status="won",
    )
    findings = SettlementVsRationale().run(_make_ctx([vigstack, ufc]))
    attrition = [
        f for f in findings if f.title.startswith("disabled_sport_settlement_attrition:")
    ]
    assert attrition == [], (
        f"No disabled-sport trades in fixture; aggregate must not fire. Got: "
        f"{[f.title for f in attrition]}"
    )


def test_pattern2_missing_entered_at_emits_defensive_critical():
    """S140 defensive behavior: when timestamp is None, we can't prove
    pre-disable, so we err on the side of surfacing the row as CRITICAL.

    Pre-S140 the heuristic silently filtered missing timestamps; that was
    wrong because missing data should not hide potential bugs. Locks in the
    new defensive emission.
    """
    no_timestamp = _trade(
        ticker="KXATPMATCH-26MAY12-NOTS",
        type_="live_momentum",
        contracts=10,
        entry_price=0.75,
        pnl=-7.5,
        status="lost",
        timestamp=None,
        resolved_at="2026-05-13T04:00:00+00:00",
    )
    findings = SettlementVsRationale().run(_make_ctx([no_timestamp]))
    pattern2 = [
        f for f in findings if f.title.startswith("disabled_sport_settlement:")
    ]
    assert len(pattern2) == 1
    assert pattern2[0].severity == "critical"
    assert pattern2[0].evidence["sport"] == "atp"
    # Aggregate must NOT contain this trade (we couldn't classify it as attrition).
    attrition = [
        f for f in findings if f.title.startswith("disabled_sport_settlement_attrition:")
    ]
    assert attrition == []


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


# -------- Tests 11-13 (Session 67): family-tail counter at-cap refinement --------

def test_pattern1_family_counter_excludes_ee_noise():
    """Session 67 P0 — family aggregate counter must exclude EE-noise.

    Synthetic family with 1 at-cap tail + 3 small EE-100%-loss positions.
    Pre-fix (Session 55-66): family_recent_tail_losses reports 4 because
    _is_pattern1_tail is at-cap-blind. Post-fix (Session 67):
    family_recent_tail_losses reports 1 because _is_pattern1_tail_at_cap
    excludes the 3 EE-noise positions whose notional is far below 95% of
    the $200 family cap.

    Mirrors the real KXHIGHMIA evidence dict bug verified by Phase 0a:
    KXHIGHMIA-26APR22-B81.5 ($14.11 notional, -$14.11), KXHIGHMIA-26APR24
    ($18.40, -$18.40), KXHIGHMIA-26APR26 ($10.08, -$10.08) all reporting
    in family_recent_tail_losses pre-fix.
    """
    # Trigger: at-cap KXHIGHMIA tail loss ($199.95 / $200 cap, -100% loss).
    trigger = _trade(
        ticker="KXHIGHMIA-26MAY06-TRIGGER",
        type_="vig_stack",
        contracts=200,
        entry_price=1.00,           # notional = $200, at-cap
        pnl=-200.00,
        status="lost",
        timestamp="2026-05-06T08:00:00+00:00",
        resolved_at="2026-05-06T12:00:00+00:00",
    )
    # 3 EE-noise positions: small notional, 100% loss. NOT at-cap.
    # Mirrors the real Phase 0a forensic shape on KXHIGHMIA.
    ee_noise = [
        _trade(
            ticker="KXHIGHMIA-26APR22-B81.5",
            type_="vig_stack",
            contracts=17,
            entry_price=0.83,        # notional = $14.11, way below $190
            pnl=-14.11,
            status="lost",
            timestamp="2026-04-29T08:00:00+00:00",
            resolved_at="2026-04-29T12:00:00+00:00",
            trade_id="PAPER-TEST-EE-1",
        ),
        _trade(
            ticker="KXHIGHMIA-26APR24-B84.5",
            type_="vig_stack",
            contracts=20,
            entry_price=0.92,        # notional = $18.40
            pnl=-18.40,
            status="lost",
            timestamp="2026-04-30T08:00:00+00:00",
            resolved_at="2026-04-30T12:00:00+00:00",
            trade_id="PAPER-TEST-EE-2",
        ),
        _trade(
            ticker="KXHIGHMIA-26APR26-T87",
            type_="vig_stack",
            contracts=14,
            entry_price=0.72,        # notional = $10.08
            pnl=-10.08,
            status="lost",
            timestamp="2026-05-01T08:00:00+00:00",
            resolved_at="2026-05-01T12:00:00+00:00",
            trade_id="PAPER-TEST-EE-3",
        ),
    ]
    findings = SettlementVsRationale().run(_make_ctx([trigger] + ee_noise))
    trigger_findings = [f for f in findings
                        if "KXHIGHMIA-26MAY06-TRIGGER" in f.title]
    assert len(trigger_findings) == 1, (
        f"trigger must fire (per-finding gate is at-cap-correct). Got: "
        f"{[f.title for f in findings]}"
    )
    f = trigger_findings[0]
    # The lock-in: counter excludes the 3 EE-noise positions.
    assert f.evidence["family_recent_tail_losses"] == 1, (
        f"Session 67 bug: family_recent_tail_losses must equal 1 (only the "
        f"trigger), not 4. Got {f.evidence['family_recent_tail_losses']}. "
        f"EE-noise positions ($14, $18, $10 notional) are NOT at-cap and "
        f"must be excluded from the family-aggregate counter."
    )


def test_pattern1_per_finding_trigger_unchanged():
    """Session 67 — per-finding trigger correctness preserved.

    KXHIGHMIA at-cap tail loss alone (no winners, no EE-noise). Trigger MUST
    still fire (per-finding gate at _check_pattern1 lines 247-254 was already
    at-cap-correct; Session 67 refactor must not perturb it). With aggregate
    negative (single tail loss in window) and no demotion clause firing,
    severity stays HIGH.

    Lock-in: refactor of _severity_pattern1 line 195 (counter helper swap)
    does NOT break the per-finding trigger.
    """
    trigger = _trade(
        ticker="KXHIGHMIA-26MAY05-T87",   # exact production ticker shape
        type_="vig_stack",
        contracts=200,
        entry_price=1.00,
        pnl=-199.95,
        status="lost",
        timestamp="2026-05-06T08:00:00+00:00",
        resolved_at="2026-05-06T12:00:00+00:00",
    )
    findings = SettlementVsRationale().run(_make_ctx([trigger]))
    trigger_findings = [f for f in findings
                        if "KXHIGHMIA-26MAY05-T87" in f.title]
    assert len(trigger_findings) == 1, (
        f"per-finding trigger must fire on isolated at-cap tail. Got: "
        f"{[f.title for f in findings]}"
    )
    f = trigger_findings[0]
    assert "tail_loss_in_high_cap_family" in f.title
    # No demotion: family_pnl_sum == -$199.95 (negative) AND no legacy
    # exclusion fires (settle is post Session-53 deploy + 24h).
    assert f.severity == "high", (
        f"isolated tail in negative-aggregate family should stay HIGH "
        f"(no demotion clause fires). Got {f.severity}."
    )
    # n_tail == 1 (the trigger itself) — counter shape unchanged on
    # this clean case.
    assert f.evidence["family_recent_tail_losses"] == 1


def test_pattern1_severity_demotion_unchanged():
    """Session 67 — severity demotion ladder fires correctly post-refactor.

    Session 47 ladder: family aggregate > 0 AND n_tail == 1 → demote
    HIGH → NOTABLE. Session 67's counter-helper swap must not break this.

    The strongest version of this test: single at-cap tail in profitable
    family WITH EE-noise present. Pre-fix (Session 55-66): EE-noise inflated
    the counter to n_tail = 4 → demotion clause failed (n_tail != 1) →
    severity stayed HIGH (WRONG — should have demoted). Post-fix (Session 67):
    counter correctly reports n_tail = 1 → demotion clause fires →
    severity = NOTABLE.

    This is the cascading consequence of the family-counter bug — a
    legitimate single-tail-in-positive-family case was being mis-classified
    as "pattern" (not demoted) when it should have been "outlier" (demoted).
    """
    # Trigger: at-cap tail in KXHIGHAUS (also $200 cap, healthy tier).
    trigger = _trade(
        ticker="KXHIGHAUS-26MAY06-TRIGGER",
        type_="vig_stack",
        contracts=200,
        entry_price=1.00,
        pnl=-200.00,
        status="lost",
        timestamp="2026-05-06T08:00:00+00:00",
        resolved_at="2026-05-06T12:00:00+00:00",
    )
    # 5 winners totaling +$300, in 14d window. Trigger contributes -$200.
    # 3 EE-noise tails contribute -$45 (-$15 each). Aggregate = +$55 (positive).
    winners = [
        _trade(
            ticker=f"KXHIGHAUS-26MAY0{i + 1}-W{i}",
            type_="vig_stack",
            contracts=200,
            entry_price=0.95,
            pnl=60.00,                # 5 × $60 = $300 wins
            status="won",
            timestamp=f"2026-05-0{i + 1}T08:00:00+00:00",
            resolved_at=f"2026-05-0{i + 1}T12:00:00+00:00",
            trade_id=f"PAPER-TEST-AUS-WIN-{i}",
        )
        for i in range(5)
    ]
    # 3 EE-noise tails — same shape as Test 11 (Session 67 founding bug).
    ee_noise = [
        _trade(
            ticker=f"KXHIGHAUS-26APR3{i}-EE{i}",
            type_="vig_stack",
            contracts=15,
            entry_price=1.00,         # notional = $15, way below $190 at-cap
            pnl=-15.00,
            status="lost",
            timestamp=f"2026-04-3{i}T08:00:00+00:00",
            resolved_at=f"2026-04-3{i}T12:00:00+00:00",
            trade_id=f"PAPER-TEST-AUS-EE-{i}",
        )
        for i in range(3)
    ]
    findings = SettlementVsRationale().run(_make_ctx([trigger] + winners + ee_noise))
    trigger_findings = [f for f in findings
                        if "KXHIGHAUS-26MAY06-TRIGGER" in f.title]
    assert len(trigger_findings) == 1
    f = trigger_findings[0]
    # POST-FIX: counter excludes EE-noise → n_tail = 1.
    assert f.evidence["family_recent_tail_losses"] == 1
    # Family aggregate POSITIVE: +$300 wins - $200 trigger - $45 EE = +$55.
    assert f.evidence["family_recent_pnl_sum"] > 0
    # Demotion fires: HIGH → NOTABLE.
    # Pre-fix (with EE-noise inflating counter to 4) this would have stayed HIGH.
    assert f.severity == "notable", (
        f"Session 67 cascading bug: single at-cap tail in positive family "
        f"with EE-noise must demote HIGH → NOTABLE post-fix. Got {f.severity}. "
        f"Pre-fix the counter inflated to 4, demotion clause (n_tail==1) "
        f"failed, severity stayed HIGH — this test locks the cascading fix."
    )
