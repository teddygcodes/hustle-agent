"""counterfactual_hotspots heuristic — gate-level CF +CLV hotspot detector."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tools.discovery_agent.heuristics.counterfactual_hotspots import (
    CounterfactualHotspots,
    MIN_CF_COUNT,
    MOMENTUM_DISABLED_SPORTS,
    _compute_cross_cohort,
    _normalize_sport_for_disabled_check,
    _severity,
    _trimmed_mean,
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
    """10 CFs, mean=10¢, 80% +CLV, n_no_won=4 → fires notable.

    Fixture uses KXNHLGAME (nhl_game — non-disabled per Session 97) so the
    disabled-sport demotion rule does not fire. Contract under test is the
    severity threshold for the CF distribution, not the disabled-set logic."""
    rows = []
    rows += [_cf(f"KXNHLGAME-T{i}", "no_leader", 15, market_result="yes") for i in range(6)]
    # 4 leader-loss settlements (n_no_won=4 — passes the >=3 floor)
    rows += [_cf(f"KXNHLGAME-N{i}", "no_leader", 5, market_result="no") for i in range(4)]
    # mean = (6*15 + 4*5)/10 = 110/10 = 11.0¢; +CLV rate = 10/10 = 100%
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "notable"
    assert f.evidence["skip_reason"] == "no_leader"
    assert f.evidence["sport"] == "nhl_game"
    assert f.evidence["count"] == 10
    assert f.evidence["n_no_won"] == 4


def test_counterfactual_hotspots_high_severity_at_15c_mean():
    """Mean CLV >= 15¢ → severity 'high'. Uses non-disabled sport to isolate
    the severity threshold contract from disabled-sport demotion."""
    rows = [_cf(f"KXNHLGAME-T{i}", "no_leader", 20, market_result="yes") for i in range(7)]
    rows += [_cf(f"KXNHLGAME-N{i}", "no_leader", 15, market_result="no") for i in range(3)]
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


# ---------------------------------------------------------------------------
# Session 47: cross-cohort context + severity demotion
# ---------------------------------------------------------------------------


def _build_cohort(prefix: str, gate: str, mean_clv: float, n_no: int, n_yes: int):
    """Helper: emit n_no + n_yes uniform-clv records for a single cohort."""
    rows = []
    for i in range(n_no):
        rows.append(_cf(f"{prefix}-N{i}", gate, mean_clv, market_result="no"))
    for i in range(n_yes):
        rows.append(_cf(f"{prefix}-Y{i}", gate, mean_clv, market_result="yes"))
    return rows


def test_cross_cohort_context_present():
    """Multi-sport gate fires; new evidence keys populated with correct values.

    Fixture uses nhl_game + ipl — both non-disabled per Session 97 — so this
    test continues to assert the cross-cohort context contract on enabled
    sports (n_disabled_sport_cohorts_in_top3 stays 0)."""
    rows = []
    # nhl_game cohort firing (NOT in MOMENTUM_DISABLED_SPORTS): mean +10¢, +CLV=100% uniform.
    rows += _build_cohort("KXNHLGAME", "no_leader", 10.0, n_no=3, n_yes=7)
    # ipl cohort: mean +5¢ (won't fire — n=8 < MIN_CF_COUNT, but contributes to context).
    rows += _build_cohort("KXIPLGAME", "no_leader", 5.0, n_no=2, n_yes=6)
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert len(findings) == 1  # only nhl_game cohort clears entry bar
    ev = findings[0].evidence
    # cross-cohort total = nhl_game(10) + ipl(8) = 18
    assert ev["cross_cohort_total_n"] == 18
    assert ev["cross_cohort_n_sports"] == 2
    # raw cross-cohort mean = (10*10 + 8*5)/18 = 140/18 ≈ 7.78
    assert ev["cross_cohort_mean_clv_cents"] == 7.78
    assert ev["cross_cohort_n_positive_sports"] == 2
    assert ev["cross_cohort_n_negative_sports"] == 0
    breakdown = ev["cross_cohort_breakdown"]
    assert ("nhl_game", 10, 10.0) in breakdown
    assert ("ipl", 8, 5.0) in breakdown
    assert ev["n_disabled_sport_cohorts_in_top3"] == 0
    assert ev["this_cohort_is_disabled_sport"] is False


def test_severity_demotion_cross_cohort_negative():
    """Per-cohort positive but cross-cohort raw < 0 → severity demoted to info."""
    rows = []
    # atp cohort firing at +10¢ (notable base): n=10, mean=+10, +CLV=100%, n_no=3.
    rows += _build_cohort("KXATPMATCH", "no_leader", 10.0, n_no=3, n_yes=7)
    # nba_game cohort dragging cross-cohort mean negative: 12 records at -20¢.
    rows += _build_cohort("KXNBAGAME", "no_leader", -20.0, n_no=4, n_yes=8)
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    # atp cohort fires (entry bar); nba_game cohort does NOT fire (mean < 5¢ floor).
    atp_findings = [f for f in findings if f.evidence["sport"] == "atp"]
    assert len(atp_findings) == 1
    f = atp_findings[0]
    # Cross-cohort raw mean = (10*10 + 12*-20)/22 ≈ -6.36 → < 0 → demote 1.
    # Trimmed mean drops one +10 and one -20 → (9*10 + 11*-20)/20 = -130/20 = -6.5
    # → < 3 AND raw <= 0 → demote 1 more. atp not disabled → no third demote.
    # base notable + 2 demotes → info.
    assert f.severity == "info"
    assert f.evidence["cross_cohort_mean_clv_cents"] < 0


def test_severity_demotion_disabled_sport():
    """Single-sport positive on wta (disabled): demote ONLY via disabled-sport rule."""
    # 7 yes at +15, 3 no at +5. mean=+12¢, +CLV=100%, n_no=3.
    rows = []
    for i in range(7):
        rows.append(_cf(f"KXWTAMATCH-Y{i}", "no_leader", 15, market_result="yes"))
    for i in range(3):
        rows.append(_cf(f"KXWTAMATCH-N{i}", "no_leader", 5, market_result="no"))
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert len(findings) == 1
    f = findings[0]
    assert f.evidence["sport"] == "wta"
    assert f.evidence["this_cohort_is_disabled_sport"] is True
    # Cross-cohort raw mean = 12 (all positive) — no raw demote.
    # Trimmed mean (drop +15 and +5) = (6*15 + 2*5)/8 = 100/8 = 12.5 — no trimmed demote.
    # wta IN MOMENTUM_DISABLED_SPORTS → 1 demote. notable + 1 → info.
    assert f.severity == "info"


def test_severity_NOT_demoted_when_cross_cohort_aligned():
    """Per-cohort AND cross-cohort positive AND sport NOT disabled → stays at notable."""
    # nhl_game cohort, mean +10¢ uniform: notable base, no demotion conditions fire.
    # nhl_game is non-disabled per Session 97 (was previously: atp, now disabled).
    rows = _build_cohort("KXNHLGAME", "no_leader", 10.0, n_no=3, n_yes=7)
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "notable"
    assert f.evidence["this_cohort_is_disabled_sport"] is False


def test_session_45_replay():
    """Session 45 no_vol_growth_first_seen / atp_challenger demoted to info.

    Reproduces the n=130 cross-cohort distribution from CLAUDE.md Session 45 entry.
    Per-cohort atp_challenger (+12.88¢ at n=24) clears entry bar; cross-cohort
    raw mean ≈ -0.05¢ + atp_challenger ∈ disabled set → notable + 3 demotes → info.
    """
    rows = []
    # 7 cohorts as documented in CLAUDE.md Session 45 corrected table:
    rows += _build_cohort("KXATPCHALLENGERMATCH", "no_vol_growth_first_seen", 12.88, n_no=3, n_yes=21)
    rows += _build_cohort("KXWTACHALLENGERMATCH", "no_vol_growth_first_seen", 0.17, n_no=4, n_yes=20)
    rows += _build_cohort("KXNBAGAME", "no_vol_growth_first_seen", -13.10, n_no=7, n_yes=13)
    rows += _build_cohort("KXATPMATCH", "no_vol_growth_first_seen", 9.55, n_no=3, n_yes=17)
    rows += _build_cohort("KXWTAMATCH", "no_vol_growth_first_seen", 0.60, n_no=5, n_yes=15)
    rows += _build_cohort("KXNHLGAME", "no_vol_growth_first_seen", 5.86, n_no=3, n_yes=11)
    rows += _build_cohort("KXIPL", "no_vol_growth_first_seen", -42.88, n_no=5, n_yes=3)
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    by_sport = {f.evidence["sport"]: f for f in findings}
    assert "atp_challenger" in by_sport
    f = by_sport["atp_challenger"]
    assert f.severity == "info"
    # Cross-cohort raw mean is approximately -0.05 (allowing for fixture rounding).
    assert f.evidence["cross_cohort_mean_clv_cents"] < 0
    # The cohort itself is in disabled set.
    assert f.evidence["this_cohort_is_disabled_sport"] is True
    # All 5 cohorts that clear entry bar should also be demoted to info or lower.
    # atp main is NOT disabled but cross-cohort still drags it down.
    if "atp" in by_sport:
        assert by_sport["atp"].severity == "info"


def test_session_46_replay():
    """Session 46 no_vol_growth_idle / wta demoted to info.

    Reproduces the n=98 cross-cohort distribution from CLAUDE.md Session 46 entry.
    Per-cohort wta (+5.26¢ at n=19) just clears entry bar; cross-cohort raw mean
    ≈ -1.34¢ + wta ∈ disabled set + all-3-of-top-3 sports disabled → info.

    Note: Session 97 added atp to MOMENTUM_DISABLED_SPORTS, so this replay now
    reads 3/3 disabled in top-3 (vs Session 46's 2/3). The demotion path is the
    same (notable → info); only the disabled-count evidence field changed.
    """
    rows = []
    rows += _build_cohort("KXATPCHALLENGERMATCH", "no_vol_growth_idle", 9.17, n_no=4, n_yes=20)
    rows += _build_cohort("KXWTAMATCH", "no_vol_growth_idle", 5.26, n_no=4, n_yes=15)
    rows += _build_cohort("KXATPMATCH", "no_vol_growth_idle", 4.05, n_no=4, n_yes=15)
    rows += _build_cohort("KXWTACHALLENGERMATCH", "no_vol_growth_idle", -12.80, n_no=8, n_yes=17)
    rows += _build_cohort("KXNBAGAME", "no_vol_growth_idle", -18.91, n_no=5, n_yes=6)
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    by_sport = {f.evidence["sport"]: f for f in findings}
    assert "wta" in by_sport
    f = by_sport["wta"]
    assert f.severity == "info"
    assert f.evidence["cross_cohort_mean_clv_cents"] < 0
    assert f.evidence["this_cohort_is_disabled_sport"] is True
    # Top-3 by per-cohort mean: atp_challenger (+9.17), wta (+5.26), atp (+4.05).
    # All 3 are in MOMENTUM_DISABLED_SPORTS post-Session-97 (atp added).
    assert f.evidence["n_disabled_sport_cohorts_in_top3"] == 3


def test_single_sport_degenerate():
    """Gate fires on only one sport (non-disabled) → cross-cohort = per-cohort, no demotion."""
    # nhl_game cohort only, mean +10¢: clears entry bar, no contradiction, nhl_game not disabled.
    rows = _build_cohort("KXNHLGAME", "no_leader", 10.0, n_no=3, n_yes=7)
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "notable"  # no demotion
    assert f.evidence["cross_cohort_n_sports"] == 1
    assert f.evidence["cross_cohort_total_n"] == 10
    assert f.evidence["cross_cohort_mean_clv_cents"] == 10.0
    # No NaN / divide-by-zero — value is finite.
    assert f.evidence["cross_cohort_trimmed_mean_clv_cents"] == 10.0


def test_disabled_sport_set_imported_from_bot_config():
    """Single source of truth: heuristic's MOMENTUM_DISABLED_SPORTS IS bot.config's."""
    from bot.config import MOMENTUM_DISABLED_SPORTS as bot_set
    assert MOMENTUM_DISABLED_SPORTS is bot_set


def test_normalize_sport_for_disabled_check():
    """Session 141: checks the raw sport against MOMENTUM_DISABLED_SPORTS FIRST,
    falls back to suffix-strip only when raw misses but the stripped form lands
    in the set. Pre-S141 the function unconditionally stripped _game/_futures
    before the membership check, silently mis-classifying nba_game cohorts
    (added to the set in S97) as not-disabled."""
    # Raw forms that ARE in the disabled set today — return the raw value.
    # nba_game is the S141-relevant case: pre-fix this returned "nba" (not in set).
    assert _normalize_sport_for_disabled_check("nba_game") == "nba_game"
    assert _normalize_sport_for_disabled_check("atp") == "atp"
    assert _normalize_sport_for_disabled_check("atp_challenger") == "atp_challenger"
    assert _normalize_sport_for_disabled_check("wta") == "wta"
    assert _normalize_sport_for_disabled_check("wta_challenger") == "wta_challenger"

    # Raw forms NOT in the set, where stripping also doesn't land in the set —
    # return the original raw value (no silent normalization for display).
    # Pre-S141 these stripped to "nba" / "mlb" / "nhl" (all not in set) but the
    # display value got rewritten anyway. Post-fix the display tag stays honest.
    assert _normalize_sport_for_disabled_check("nba_futures") == "nba_futures"
    assert _normalize_sport_for_disabled_check("mlb_game") == "mlb_game"
    assert _normalize_sport_for_disabled_check("mlb_futures") == "mlb_futures"
    assert _normalize_sport_for_disabled_check("nhl_game") == "nhl_game"
    assert _normalize_sport_for_disabled_check("nhl_futures") == "nhl_futures"

    # Sports with no per-game/futures suffix — return raw regardless of set membership.
    assert _normalize_sport_for_disabled_check("ufc") == "ufc"
    assert _normalize_sport_for_disabled_check("ipl") == "ipl"
    assert _normalize_sport_for_disabled_check("weather_high") == "weather_high"
    assert _normalize_sport_for_disabled_check(None) is None


def test_normalize_sport_strip_fallback_for_legacy_data():
    """Session 141: the strip-fallback path only fires when raw misses but
    the stripped form lands in the set. This dead-codes most of today's cases
    (the set already carries 'nba_game' directly), but keeps a safety net for
    legacy data or future set shapes where a flat form like 'atp' is in the
    set and a hypothetical 'atp_game' could arrive. Pin the behavior so a
    future refactor doesn't accidentally drop the fallback."""
    # MOMENTUM_DISABLED_SPORTS today has 'atp' (flat). If the classifier ever
    # emitted 'atp_game' or 'atp_futures', the fallback should strip to 'atp'.
    # We can't construct that today (atp tickers don't trigger _game suffix),
    # but the function's fallback contract is independent of current data shape.
    assert _normalize_sport_for_disabled_check("atp_game") == "atp"
    assert _normalize_sport_for_disabled_check("atp_futures") == "atp"
    assert _normalize_sport_for_disabled_check("wta_game") == "wta"
    assert _normalize_sport_for_disabled_check("wta_futures") == "wta"


def test_session_141_demotion_fires_for_nba_game_cohort():
    """Session 141 integration test: a synthetic CF row with sport='nba_game'
    triggers the disabled-sport demotion path. Pre-S141 this test would have
    failed — _normalize_sport_for_disabled_check stripped 'nba_game' → 'nba'
    before the membership check, so this_cohort_is_disabled_sport stayed False
    and the cohort's severity didn't demote.

    Closes the test gap S140's coder flagged: every disabled-sport demotion
    test pre-S141 used WTA (no _game suffix), which doesn't exercise the
    stripping bug surface."""
    # 7 yes at +15, 3 no at +5. mean=+12¢, +CLV=100%, n_no=3 — clears entry bar.
    rows = []
    for i in range(7):
        rows.append(_cf(f"KXNBAGAME-Y{i}", "no_leader", 15, market_result="yes"))
    for i in range(3):
        rows.append(_cf(f"KXNBAGAME-N{i}", "no_leader", 5, market_result="no"))
    findings = CounterfactualHotspots().run(_ctx(clv=rows))
    assert len(findings) == 1
    f = findings[0]
    assert f.evidence["sport"] == "nba_game"
    # The demotion path's key flag: post-S141 this is True (was False pre-fix).
    assert f.evidence["this_cohort_is_disabled_sport"] is True, (
        "nba_game is in MOMENTUM_DISABLED_SPORTS post-S97 — disabled-sport "
        "demotion must fire. Pre-S141 the suffix-strip turned 'nba_game' → "
        "'nba' before the set check, silently mis-classifying it."
    )
    # Single-sport positive: notable base + 1 disabled-sport demote → info.
    assert f.severity == "info"


def test_trimmed_mean_with_n_lt_3():
    """Trimmed mean falls back to raw mean when n < 3 (no slice crash)."""
    assert _trimmed_mean([5.0, 10.0]) == 7.5  # n=2, raw mean
    assert _trimmed_mean([8.0]) == 8.0        # n=1, raw mean
    assert _trimmed_mean([]) == 0.0           # empty, no crash
    # n=3 trims to single middle value.
    assert _trimmed_mean([1.0, 5.0, 9.0]) == 5.0


def test_compute_cross_cohort_helper_directly():
    """Direct unit test on _compute_cross_cohort for shape correctness."""
    rows = []
    rows += _build_cohort("KXATPMATCH", "g", 10.0, n_no=2, n_yes=8)
    rows += _build_cohort("KXNBAGAME", "g", -5.0, n_no=2, n_yes=8)
    ctx_data = _compute_cross_cohort("g", rows)
    assert ctx_data["cross_cohort_total_n"] == 20
    assert ctx_data["cross_cohort_n_sports"] == 2
    # mean = (10*10 + 10*-5)/20 = 50/20 = 2.5
    assert ctx_data["cross_cohort_mean_clv_cents"] == 2.5
    assert ctx_data["cross_cohort_n_positive_sports"] == 1
    assert ctx_data["cross_cohort_n_negative_sports"] == 1


def test_severity_helper_clamps_at_info():
    """Severity helper clamps to 'info' even with more demotions than ladder steps."""
    ctx_data = {
        "cross_cohort_mean_clv_cents": -10.0,
        "cross_cohort_trimmed_mean_clv_cents": -5.0,
    }
    # Notable base + 3 demote conditions all fire → clamps at info.
    assert _severity(per_cohort_mean=10.0, ctx_data=ctx_data, this_cohort_disabled=True) == "info"
    # High base + 3 demote conditions → high → notable → info → clamps at info.
    assert _severity(per_cohort_mean=20.0, ctx_data=ctx_data, this_cohort_disabled=True) == "info"
