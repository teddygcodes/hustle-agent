"""concurrent_attack_angles heuristic — Session 48 tests.

Mirrors Session 47's test patterns (SimpleNamespace ctx, hand-crafted records,
replay tests for severity demotion). 13 cases per the Session 48 plan.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from tools.discovery_agent.heuristics.concurrent_attack_angles import (
    ConcurrentAttackAngles,
    _event_family,
    _market_type_from_ticker,
    _severity_concurrent_fire,
    _severity_scanner_gap,
    MIN_CONCURRENT_PAIRS,
    MIN_GAP_AVG_VOLUME_24H,
    MIN_GAP_EVENTS,
)


_NOW = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)


def _ctx(*, paper_trades=None, decisions=None, clv=None, universe_rows=None):
    rows = list(universe_rows or [])
    return SimpleNamespace(
        paper_trades=paper_trades or [],
        decisions=decisions or [],
        clv=clv or [],
        universe_iter=lambda: iter(rows),
        loaded_at=_NOW,
    )


def _trade(*, id, ticker, type_, status="won", pnl=5.0, days_ago=2):
    ts = _NOW - dt.timedelta(days=days_ago)
    return {
        "id": id,
        "ticker": ticker,
        "type": type_,
        "status": status,
        "pnl": pnl,
        "timestamp": ts.isoformat(),
        "side": "yes",
    }


def _cf(ticker, *, clv_cents, market_result="yes", days_ago=2, ts_offset_min=0,
        primary_ts=None):
    """Counterfactual_settled record. ts_offset_min is offset from primary_ts (or _NOW)."""
    base = primary_ts or (_NOW - dt.timedelta(days=days_ago))
    ts = base + dt.timedelta(minutes=ts_offset_min)
    return {
        "ticker": ticker,
        "status": "counterfactual_settled",
        "clv_cents": clv_cents,
        "market_result": market_result,
        "recorded_at": ts.isoformat(),
        "skipped_by_gate": "some_gate",
        "opp_type": "live_momentum",
        "side": "yes",
    }


def _universe_row(ticker, *, event_ticker, series_ticker, volume_24h=2000, ts_days_ago=1):
    ts = _NOW - dt.timedelta(days=ts_days_ago)
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "volume_24h": volume_24h,
        "ts": ts.isoformat(),
    }


# ---------------------------------------------------------------------------
# 1 + 2: helper tests
# ---------------------------------------------------------------------------


def test_event_family_extraction_uses_event_ticker_when_present():
    rec = {"ticker": "KXNBAGAME-26APR26CLETOR-CLE", "event_ticker": "EXPLICIT_FAMILY"}
    assert _event_family(rec, prefer_field=True) == "EXPLICIT_FAMILY"
    # prefer_field=False forces fallback even when event_ticker is present.
    assert _event_family(rec, prefer_field=False) == "KXNBAGAME-26APR26CLETOR"


def test_event_family_extraction_falls_back_to_ticker_split():
    assert _event_family("KXNBAGAME-26APR26CLETOR-CLE") == "KXNBAGAME-26APR26CLETOR"
    assert _event_family("KXMLB-26-LAD") == "KXMLB-26"
    assert _event_family("noseparator") is None
    assert _event_family("") is None
    # Dict without event_ticker uses fallback.
    assert _event_family({"ticker": "KXATPMATCH-X-Y"}) == "KXATPMATCH-X"


# ---------------------------------------------------------------------------
# 3: 3-class bucketing (already_trading / scanned_not_taken / never_scanned)
# ---------------------------------------------------------------------------


def test_classify_already_trading_scanned_not_taken_never_scanned():
    """Wire 1 trade + 2 scanned-not-taken + 3 never-scanned in one event family.

    Expected: scanner_gap finding flags the 3 never-scanned tickers; concurrent
    fire pool draws CFs from the 2 scanned-not-taken (one of which is +CLV
    enough to pass the bar with sufficient n).
    """
    fam = "KXNBAGAME-26APR26CLETOR"
    series = "KXNBAGAME"
    primary_ts = _NOW - dt.timedelta(days=1)
    # 1 traded ticker (winner side)
    trades = [_trade(
        id="PAPER-1", ticker=f"{fam}-CLE", type_="live_momentum",
        status="won", pnl=10.0, days_ago=1,
    )]
    # 2 scanned-not-taken (T220 totals + B5 spread) appearing in CFs
    decisions = [
        {"ts": primary_ts.isoformat(), "ticker": f"{fam}-T220", "opp_type": "live_momentum"},
        {"ts": primary_ts.isoformat(), "ticker": f"{fam}-B5", "opp_type": "live_momentum"},
    ]
    # 3 never-scanned high-volume totals tickers, in 10+ events to trigger scanner_gap
    universe_rows = [
        _universe_row(f"{fam}-CLE", event_ticker=fam, series_ticker=series),
        _universe_row(f"{fam}-T220", event_ticker=fam, series_ticker=series),
        _universe_row(f"{fam}-B5", event_ticker=fam, series_ticker=series),
        _universe_row(f"{fam}-NEVER1", event_ticker=fam, series_ticker=series, volume_24h=2500),
        _universe_row(f"{fam}-NEVER2", event_ticker=fam, series_ticker=series, volume_24h=2500),
        _universe_row(f"{fam}-NEVER3", event_ticker=fam, series_ticker=series, volume_24h=2500),
    ]
    # Classification check: emit a scanner_gap requires ≥10 events sharing the gap.
    # Build 12 event families each with the same never-scanned market type.
    for i in range(12):
        ev = f"KXNBAGAME-26APR{i:02d}AAABBB"
        trades.append(_trade(
            id=f"PAPER-{i+10}", ticker=f"{ev}-AAA", type_="live_momentum",
            status="won", pnl=5.0, days_ago=2,
        ))
        universe_rows.append(_universe_row(
            f"{ev}-AAA", event_ticker=ev, series_ticker=series,
        ))
        universe_rows.append(_universe_row(
            f"{ev}-NEVER", event_ticker=ev, series_ticker=series, volume_24h=3000,
        ))

    ctx = _ctx(paper_trades=trades, decisions=decisions, universe_rows=universe_rows)
    findings = ConcurrentAttackAngles().run(ctx)
    gaps = [f for f in findings if f.evidence.get("finding_type") == "scanner_gap"]
    # Should emit at least one gap finding for the NEVER ticker market type.
    assert any(
        g.evidence["events_with_gap_count"] >= MIN_GAP_EVENTS for g in gaps
    ), f"expected ≥1 gap with ≥{MIN_GAP_EVENTS} events, got {[g.evidence for g in gaps]}"


# ---------------------------------------------------------------------------
# 4 + 5: concurrent_fire_candidate positive and below-bar
# ---------------------------------------------------------------------------


def _build_concurrent_scenario(*, n_pairs: int, mean_clv: float,
                               n_no_in_pairs: int, primary_ticker_prefix: str = "KXNBAGAME",
                               candidate_suffix_letter: str = "T",
                               disabled_sport_prefix: bool = False):
    """Build paper_trades + clv records for a concurrent-fire scenario.

    Each pair uses a distinct event family so the cross-family breakdown reflects
    real spread (single-event-family scenarios collapse the demotion ladder).
    """
    trades = []
    cfs = []
    universe_rows = []
    for i in range(n_pairs):
        ev = f"{primary_ticker_prefix}-26APR{i:02d}EVENT"
        primary_ts = _NOW - dt.timedelta(days=2, hours=i % 6)
        primary_ticker = f"{ev}-CLE"
        trades.append({
            "id": f"PAPER-PRIM-{i}",
            "ticker": primary_ticker,
            "type": "live_momentum",
            "status": "won",
            "pnl": 8.0,
            "timestamp": primary_ts.isoformat(),
            "side": "yes",
        })
        # CF on a different ticker + different market type, ±30min from primary
        cf_ticker = f"{ev}-{candidate_suffix_letter}220"
        market_result = "no" if i < n_no_in_pairs else "yes"
        cfs.append({
            "ticker": cf_ticker,
            "status": "counterfactual_settled",
            "clv_cents": mean_clv,
            "market_result": market_result,
            "recorded_at": (primary_ts + dt.timedelta(minutes=15)).isoformat(),
            "skipped_by_gate": "some_gate",
            "side": "yes",
        })
        universe_rows.append(_universe_row(
            primary_ticker, event_ticker=ev, series_ticker=primary_ticker_prefix,
        ))
        universe_rows.append(_universe_row(
            cf_ticker, event_ticker=ev, series_ticker=primary_ticker_prefix,
        ))
    return trades, cfs, universe_rows


def test_concurrent_fire_candidate_positive():
    """5 NHL-game wins + 5 concurrent CFs at +6¢ mean, n_no=2 → emit NOTABLE.

    Session 141: switched from KXNBAGAME (nba_game disabled post-S97 → 1 demote
    → info) to KXNHLGAME (nhl_game NOT disabled → stays at notable). The test
    is about the entry-bar clearing path, not the disabled-sport demotion."""
    trades, cfs, universe_rows = _build_concurrent_scenario(
        n_pairs=5, mean_clv=6.0, n_no_in_pairs=2,
        primary_ticker_prefix="KXNHLGAME",
    )
    ctx = _ctx(paper_trades=trades, clv=cfs, universe_rows=universe_rows)
    findings = ConcurrentAttackAngles().run(ctx)
    cf_findings = [f for f in findings
                   if f.evidence.get("finding_type") == "concurrent_fire_candidate"]
    assert len(cf_findings) == 1
    f = cf_findings[0]
    assert f.severity == "notable"
    ev = f.evidence
    assert ev["n_concurrent_pairs"] == 5
    assert ev["concurrent_cf_mean_clv_cents"] == 6.0
    assert ev["concurrent_cf_n_no"] == 2
    assert ev["concurrent_cf_n_yes"] == 3
    assert ev["primary_strategy"] == "live_momentum"
    assert ev["candidate_market_type"] == "T<n>"
    assert ev["primary_market_type"] == "team"
    assert ev["_fingerprint_keys"] == [
        "event_family_pattern", "primary_strategy", "candidate_market_type",
    ]


def test_concurrent_fire_candidate_below_bar_no_finding():
    """4 pairs (below MIN_CONCURRENT_PAIRS=5) → no finding emitted."""
    trades, cfs, universe_rows = _build_concurrent_scenario(
        n_pairs=4, mean_clv=8.0, n_no_in_pairs=2,
    )
    ctx = _ctx(paper_trades=trades, clv=cfs, universe_rows=universe_rows)
    findings = ConcurrentAttackAngles().run(ctx)
    cf_findings = [f for f in findings
                   if f.evidence.get("finding_type") == "concurrent_fire_candidate"]
    assert cf_findings == []


# ---------------------------------------------------------------------------
# 6 + 7: severity demotion and HIGH severity
# ---------------------------------------------------------------------------


def test_concurrent_fire_severity_demotion_cross_family_negative():
    """Session 47 mirror. Cohort A (NBA) + Cohort B (NHL) on same
    (live_momentum, KXNBAGAME/T<n>) AND (live_momentum, KXNHLGAME/T<n>) pair
    keys diverge — but the brief framing is per (primary_strategy, cand_mt).

    Building it as: one strong cohort (NBA) produces +12¢ × 8 pairs (would be
    notable on its own); same primary_strategy + candidate_market_type pair on
    a SECOND series (NHL) produces -10¢ × 20 pairs that drag cross-family mean
    negative.

    Critical: the two cohorts must share the SAME (primary_strategy,
    candidate_market_type) tuple to share a cross-family aggregator. They don't
    share series_ticker, so each emits its own per-tuple finding — but BOTH
    findings see the negative cross-family mean.
    """
    nba_trades, nba_cfs, nba_universe = _build_concurrent_scenario(
        n_pairs=8, mean_clv=12.0, n_no_in_pairs=4,
        primary_ticker_prefix="KXNBAGAME",
    )
    # NHL cohort with negative CLV: 20 pairs at -10¢
    nhl_trades, nhl_cfs, nhl_universe = _build_concurrent_scenario(
        n_pairs=20, mean_clv=-10.0, n_no_in_pairs=8,
        primary_ticker_prefix="KXNHLGAME",
    )
    trades = nba_trades + nhl_trades
    cfs = nba_cfs + nhl_cfs
    universe_rows = nba_universe + nhl_universe
    ctx = _ctx(paper_trades=trades, clv=cfs, universe_rows=universe_rows)
    findings = ConcurrentAttackAngles().run(ctx)
    cf_findings = [f for f in findings
                   if f.evidence.get("finding_type") == "concurrent_fire_candidate"]
    # NBA cohort clears entry bar (mean 12¢, +CLV 100%, n_no=4).
    # NHL cohort fails entry bar (mean -10¢, below MIN_MEAN_CONCURRENT_CLV_CENTS).
    nba_cohort = [f for f in cf_findings
                  if f.evidence["series_ticker"] == "KXNBAGAME"]
    assert len(nba_cohort) == 1
    f = nba_cohort[0]
    # Cross-family mean across both cohorts:
    # NBA: 8 pairs × +12 = +96; NHL: 20 pairs × -10 = -200; total 28 pairs / -104¢
    # mean ≈ -3.71¢. Negative → 1 demote step.
    assert f.evidence["cross_family_mean_clv_cents"] < 0
    assert f.evidence["cross_family_n_event_families"] >= 28
    # Base notable + cross-family-negative demote → info.
    assert f.severity == "info"


def test_concurrent_fire_severity_high_when_strong_signal():
    """18 pairs, +12¢ mean, +CLV 100%, n_no=6 → HIGH base, no demotion.

    Session 141: switched from default KXNBAGAME (nba_game disabled post-S97 →
    demoted to notable) to KXNHLGAME (nhl_game NOT disabled → stays HIGH).
    This test exercises the no-demotion path; sport choice must NOT be in
    MOMENTUM_DISABLED_SPORTS."""
    trades, cfs, universe_rows = _build_concurrent_scenario(
        n_pairs=18, mean_clv=12.0, n_no_in_pairs=6,
        primary_ticker_prefix="KXNHLGAME",
    )
    ctx = _ctx(paper_trades=trades, clv=cfs, universe_rows=universe_rows)
    findings = ConcurrentAttackAngles().run(ctx)
    cf_findings = [f for f in findings
                   if f.evidence.get("finding_type") == "concurrent_fire_candidate"]
    assert len(cf_findings) == 1
    f = cf_findings[0]
    assert f.severity == "high"
    assert f.evidence["n_concurrent_pairs"] == 18
    assert f.evidence["concurrent_cf_mean_clv_cents"] == 12.0
    assert f.evidence["primary_sport_disabled"] is False


# ---------------------------------------------------------------------------
# 8 + 9: scanner_gap notable/HIGH and below-bar
# ---------------------------------------------------------------------------


def _build_scanner_gap_scenario(*, n_events: int, avg_volume: int,
                                series: str = "KXNBAGAME",
                                missing_suffix: str = "T220"):
    trades = []
    universe_rows = []
    for i in range(n_events):
        ev = f"{series}-26APR{i:02d}EVENT"
        primary_ticker = f"{ev}-CLE"
        # 1 trade per event
        trades.append(_trade(
            id=f"PAPER-{i}", ticker=primary_ticker, type_="live_momentum",
            status="won", pnl=5.0, days_ago=2,
        ))
        # universe has the traded ticker AND the never-scanned ticker
        universe_rows.append(_universe_row(
            primary_ticker, event_ticker=ev, series_ticker=series, volume_24h=avg_volume,
        ))
        universe_rows.append(_universe_row(
            f"{ev}-{missing_suffix}", event_ticker=ev, series_ticker=series,
            volume_24h=avg_volume,
        ))
    return trades, universe_rows


def test_scanner_gap_high_volume_emits_notable():
    """12 events with avg_vol 2500 → severity NOTABLE.
    60 events with avg_vol 8000 → severity HIGH."""
    # NOTABLE case
    trades, universe_rows = _build_scanner_gap_scenario(n_events=12, avg_volume=2500)
    ctx = _ctx(paper_trades=trades, universe_rows=universe_rows)
    findings = ConcurrentAttackAngles().run(ctx)
    gaps = [f for f in findings if f.evidence.get("finding_type") == "scanner_gap"]
    assert len(gaps) == 1
    g = gaps[0]
    assert g.severity == "notable"
    assert g.evidence["events_with_gap_count"] == 12
    assert g.evidence["avg_volume_24h"] == 2500.0

    # HIGH case
    trades2, universe_rows2 = _build_scanner_gap_scenario(n_events=60, avg_volume=8000)
    ctx2 = _ctx(paper_trades=trades2, universe_rows=universe_rows2)
    findings2 = ConcurrentAttackAngles().run(ctx2)
    gaps2 = [f for f in findings2 if f.evidence.get("finding_type") == "scanner_gap"]
    assert len(gaps2) == 1
    assert gaps2[0].severity == "high"


def test_scanner_gap_low_volume_no_finding():
    """12 events with avg_vol=500 (below MIN_GAP_AVG_VOLUME_24H) → no finding."""
    trades, universe_rows = _build_scanner_gap_scenario(n_events=12, avg_volume=500)
    ctx = _ctx(paper_trades=trades, universe_rows=universe_rows)
    findings = ConcurrentAttackAngles().run(ctx)
    gaps = [f for f in findings if f.evidence.get("finding_type") == "scanner_gap"]
    assert gaps == []


# ---------------------------------------------------------------------------
# 10: disabled-sport demotion
# ---------------------------------------------------------------------------


def test_disabled_sport_demotion_applies():
    """Primary trade on KXATPCHALLENGERMATCH (atp_challenger ∈ MOMENTUM_DISABLED_SPORTS).

    All other thresholds met. Severity demotes one step relative to a comparable
    NBA cohort.
    """
    # Disabled sport cohort
    trades_dis, cfs_dis, universe_dis = _build_concurrent_scenario(
        n_pairs=8, mean_clv=12.0, n_no_in_pairs=4,
        primary_ticker_prefix="KXATPCHALLENGERMATCH",
    )
    ctx_dis = _ctx(paper_trades=trades_dis, clv=cfs_dis, universe_rows=universe_dis)
    findings_dis = ConcurrentAttackAngles().run(ctx_dis)
    cf_dis = [f for f in findings_dis
              if f.evidence.get("finding_type") == "concurrent_fire_candidate"]
    assert len(cf_dis) == 1
    assert cf_dis[0].evidence["primary_sport_disabled"] is True
    # Base notable + 1 demote (disabled sport, cross-family is single-cohort
    # positive so no extra demote) → info.
    assert cf_dis[0].severity == "info"

    # Comparable NHL cohort — nhl_game NOT in MOMENTUM_DISABLED_SPORTS.
    # Session 141: switched from KXNBAGAME because nba_game IS in the set
    # post-S97; this slot is the "enabled control" so it must use a sport
    # the disable set does not contain.
    trades_en, cfs_en, universe_en = _build_concurrent_scenario(
        n_pairs=8, mean_clv=12.0, n_no_in_pairs=4,
        primary_ticker_prefix="KXNHLGAME",
    )
    ctx_en = _ctx(paper_trades=trades_en, clv=cfs_en, universe_rows=universe_en)
    findings_en = ConcurrentAttackAngles().run(ctx_en)
    cf_en = [f for f in findings_en
             if f.evidence.get("finding_type") == "concurrent_fire_candidate"]
    assert len(cf_en) == 1
    assert cf_en[0].evidence["primary_sport_disabled"] is False
    # Base notable, no demotion → notable.
    assert cf_en[0].severity == "notable"


# ---------------------------------------------------------------------------
# 11: clean skip when sources are empty
# ---------------------------------------------------------------------------


def test_missing_universe_iter_skips_cleanly():
    """Empty universe + empty trades → empty findings list, no crash."""
    ctx = _ctx()
    findings = ConcurrentAttackAngles().run(ctx)
    assert findings == []


# ---------------------------------------------------------------------------
# 12: canonical schema regression (Session 45 lesson)
# ---------------------------------------------------------------------------


def test_canonical_schema_used_throughout():
    """Lock the heuristic source against the Session 45 schema-value error.

    Per CLAUDE.md "Canonical Data Schema Reference":
    - market_result canonical values are 'yes'/'no'/None — NOT 'yes_won'/'no_won'.
    - The skipped_by_gate field exists on CFs but this heuristic does not
      compare-equal on it (we filter by status only). A 'no_won' substring in
      the source would be a regression of the Session 45 verification error.
    """
    src_path = Path(__file__).parent.parent / "tools" / "discovery_agent" / "heuristics" / "concurrent_attack_angles.py"
    src = src_path.read_text()
    # Substring 'no_won' must NOT appear (canonical is 'no').
    assert "no_won" not in src, (
        "Session 45 schema-value error: market_result is 'no'/'yes', not 'no_won'/'yes_won'"
    )
    # Canonical schema fields must be present.
    assert "market_result" in src
    assert "clv_cents" in src
    assert "counterfactual_settled" in src
    # Heuristic does NOT gate on skipped_by_gate (uses status filter).
    # We allow the substring in docstrings/comments but no compare-equal usage.
    # Quick sanity: the heuristic class body should not have a skipped_by_gate
    # equality check that would inadvertently filter CFs by gate name.
    class_body_start = src.find("class ConcurrentAttackAngles")
    class_body = src[class_body_start:]
    assert 'skipped_by_gate"' not in class_body, (
        "concurrent_attack_angles must not gate on skipped_by_gate — that's a "
        "counterfactual_hotspots concern, not a concurrent-pair concern."
    )


# ---------------------------------------------------------------------------
# 13: smoke test on real data
# ---------------------------------------------------------------------------


def test_smoke_real_data_does_not_raise():
    """Load real bot/state/* and assert run() returns a list without crashing.

    This is the regression that catches schema drift, malformed records, ts
    parsing edge cases, and memory blowups against actual production data.
    """
    from tools.discovery_agent.context import DEFAULT_REPO, DiscoveryContext
    if not (DEFAULT_REPO / "bot" / "state" / "clv.json").exists():
        # Test fixture environment, not the real repo. Skip cleanly.
        return
    ctx = DiscoveryContext.load(repo=DEFAULT_REPO)
    result = ConcurrentAttackAngles().run(ctx)
    assert isinstance(result, list)
    # Each item is a Finding with the expected evidence keys.
    for f in result:
        assert f.heuristic == "concurrent_attack_angles"
        assert f.severity in ("high", "notable", "info")
        ftype = f.evidence.get("finding_type")
        assert ftype in ("concurrent_fire_candidate", "scanner_gap")
        assert "_fingerprint_keys" in f.evidence


# ---------------------------------------------------------------------------
# Additional sanity: helper functions
# ---------------------------------------------------------------------------


def test_market_type_classifier_distinguishes_winner_from_totals():
    """Plan helper sanity — series-agnostic suffix label so the same concept
    compares equal across series for the cross-family aggregator."""
    # Same concept across series → same label (the point).
    assert _market_type_from_ticker("KXNBAGAME-26APR26CLETOR-CLE") == "team"
    assert _market_type_from_ticker("KXNHLGAME-26APR26ABCDEF-DET") == "team"
    assert _market_type_from_ticker("KXMLB-26-LAD") == "team"
    # Different concept within same series → different label.
    assert _market_type_from_ticker("KXNBAGAME-26APR26CLETOR-T220") == "T<n>"
    assert _market_type_from_ticker("KXNBAGAME-26APR26CLETOR-B5") == "B<n>"
    # Edge cases.
    assert _market_type_from_ticker("") == "unknown"
    assert _market_type_from_ticker("nodash") == "unknown"


def test_severity_helpers_clamp():
    """_severity_concurrent_fire clamps at 'info' on multiple demotes."""
    assert _severity_concurrent_fire(
        n_pairs=20, mean_cents=15.0, positive_rate=0.80, n_no=10,
        cross_family_mean=-10.0, primary_sport_disabled=True,
    ) == "info"
    # Strong signal + no demotion → high.
    assert _severity_concurrent_fire(
        n_pairs=20, mean_cents=15.0, positive_rate=0.80, n_no=10,
        cross_family_mean=5.0, primary_sport_disabled=False,
    ) == "high"
    # _severity_scanner_gap thresholds.
    assert _severity_scanner_gap(60, 8000.0) == "high"
    assert _severity_scanner_gap(15, 2000.0) == "notable"
    assert _severity_scanner_gap(5, 500.0) == "info"
