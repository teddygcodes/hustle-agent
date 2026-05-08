"""Tests for tools/strategy_lab — Session 51.

10 cases per the brief. Reuses tests/conftest.py autouse fixtures
(_isolate_decisions_log, _isolate_predictions_log, _isolate_glint_loggers).
"""
from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.strategy_lab import data_loader, driver, evaluator, reports
from tools.strategy_lab.candidate import CandidateOpportunity, CandidateStrategy
from tools.strategy_lab.candidates.example_total_points_under import (
    STRATEGY as EXAMPLE_STRATEGY,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _ts(days_ago: int = 0, hour: int = 12) -> str:
    """ISO 8601 timestamp N days ago at the given hour (UTC)."""
    today = datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=days_ago)).isoformat()


def _make_universe_row(ticker: str, days_ago: int = 0, **overrides) -> dict:
    """Build a synthetic universe row matching the on-disk schema."""
    row = {
        "ts": _ts(days_ago),
        "scan_id": "TEST-SCAN",
        "ticker": ticker,
        "series_ticker": ticker.split("-")[0],
        "event_ticker": "-".join(ticker.split("-")[:2]),
        "status": "active",
        "close_ts": _ts(-1),  # tomorrow
        "yes_ask": 50,
        "yes_bid": 48,
        "no_ask": 52,
        "no_bid": 48,
        "volume": 0,
        "volume_24h": 5000,
        "open_interest": 0,
        "title": f"Test market {ticker}",
        "scanned_by": [],
    }
    row.update(overrides)
    return row


def _make_clv_record(
    ticker: str,
    *,
    side: str = "yes",
    status: str = "settled",
    entry_price_cents: int = 50,
    market_result: str | None = "yes",
    closing_yes_price: float | None = 100.0,
    days_ago: int = 0,
    hour: int = 12,
    skipped_by_gate: str | None = None,
) -> dict:
    """Build a synthetic clv record. Canonical schema."""
    rec = {
        "trade_id": f"PAPER-{ticker[:8]}",
        "ticker": ticker,
        "opp_type": "test_strategy",
        "side": side,
        "entry_price_cents": entry_price_cents,
        "fair_value_cents": 60.0,
        "edge_at_trade": 0.1,
        "contracts": 1,
        "paper": True,
        "recorded_at": _ts(days_ago, hour=hour),
        "status": status,
        "closing_yes_price": closing_yes_price,
        "clv_cents": (closing_yes_price - entry_price_cents) if (closing_yes_price is not None and side == "yes") else 0,
        "market_result": market_result,
    }
    if skipped_by_gate is not None:
        rec["skipped_by_gate"] = skipped_by_gate
    return rec


@pytest.fixture
def lab_state(tmp_path, monkeypatch):
    """Redirect data_loader paths into tmp_path with seeded files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    archive_dir = state_dir / "archive"
    archive_dir.mkdir()
    universe_file = state_dir / "universe.jsonl"
    clv_file = state_dir / "clv.json"
    decisions_file = state_dir / "decisions.jsonl"

    monkeypatch.setattr(data_loader, "UNIVERSE_FILE", universe_file)
    monkeypatch.setattr(data_loader, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(data_loader, "CLV_FILE", clv_file)
    monkeypatch.setattr(data_loader, "DECISIONS_FILE", decisions_file)

    return {
        "tmp_path": tmp_path,
        "state_dir": state_dir,
        "archive_dir": archive_dir,
        "universe_file": universe_file,
        "clv_file": clv_file,
        "decisions_file": decisions_file,
    }


@pytest.fixture(autouse=True)
def _isolate_lab_reports(tmp_path_factory, monkeypatch):
    """Don't pollute tools/strategy_lab/reports_out/ during tests."""
    sandbox = tmp_path_factory.mktemp("strategy_lab_reports")
    monkeypatch.setattr(reports, "REPORTS_DIR", sandbox)


# ---------------------------------------------------------------------------
# 1. Protocol compliance
# ---------------------------------------------------------------------------

def test_candidate_protocol_compliance():
    """ExampleTotalPointsUnder satisfies CandidateStrategy."""
    assert isinstance(EXAMPLE_STRATEGY, CandidateStrategy)
    assert EXAMPLE_STRATEGY.name == "example_total_points_under"
    # And evaluate() is callable with a market dict
    result = EXAMPLE_STRATEGY.evaluate({"ticker": "FOO"}, {})
    assert result is None or isinstance(result, CandidateOpportunity)


# ---------------------------------------------------------------------------
# 2. data_loader streams universe + loads clv + decisions
# ---------------------------------------------------------------------------

def test_data_loader_streams_universe(lab_state):
    """Fixture universe.jsonl with 100 records → all yielded; clv & decisions buckets populated."""
    rows = [_make_universe_row(f"KXTEST-{i:03d}", days_ago=1) for i in range(100)]
    lab_state["universe_file"].write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    clv = [
        _make_clv_record("KXTEST-001", days_ago=1),
        _make_clv_record("KXTEST-002", days_ago=1, status="counterfactual_settled", market_result="no"),
    ]
    lab_state["clv_file"].write_text(json.dumps(clv))

    decisions = [
        {"ts": _ts(1), "ticker": "KXTEST-001", "opp_type": "test", "decision": "reject", "reason": "low_volume"},
    ]
    lab_state["decisions_file"].write_text("\n".join(json.dumps(d) for d in decisions))

    universe_iter, clv_lookup, decisions_by_ticker, start, end = data_loader.load_window(days=14)
    yielded = list(universe_iter)

    assert len(yielded) == 100
    assert "KXTEST-001" in clv_lookup
    assert len(clv_lookup["KXTEST-001"]) == 1
    assert "KXTEST-002" in clv_lookup
    assert "KXTEST-001" in decisions_by_ticker
    assert end >= start


# ---------------------------------------------------------------------------
# 3. data_loader filters by date window
# ---------------------------------------------------------------------------

def test_data_loader_window_filters_by_date(lab_state):
    """30-day fixture: --days 14 returns only the last 14d of rows."""
    rows = [_make_universe_row(f"KXTEST-{i:03d}", days_ago=i) for i in range(30)]
    lab_state["universe_file"].write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    lab_state["clv_file"].write_text("[]")

    universe_iter, _, _, start, end = data_loader.load_window(days=14)
    yielded = list(universe_iter)

    # 14-day window inclusive: today + 13 prior days = 14 calendar dates.
    assert len(yielded) == 14
    days_seen = {r["ticker"] for r in yielded}
    # We expect KXTEST-000 (today) through KXTEST-013 (13 days ago).
    expected = {f"KXTEST-{i:03d}" for i in range(14)}
    assert days_seen == expected


# ---------------------------------------------------------------------------
# 4. evaluator scores settled winner
# ---------------------------------------------------------------------------

def test_evaluator_scores_settled_winner():
    """Candidate "no" bet on ticker T with market_result='no' → positive pnl."""
    opp = CandidateOpportunity(
        ticker="KXTEST-A",
        side="no",
        target_price_cents=70.0,  # paid 70c on NO
        fair_value_cents=80.0,
        edge_cents=10.0,
        confidence=0.6,
        reason="test",
    )
    universe_ts = _ts(1, hour=12)
    clv_lookup = {
        "KXTEST-A": [
            _make_clv_record(
                "KXTEST-A",
                side="no",
                status="settled",
                entry_price_cents=70,
                market_result="no",
                closing_yes_price=0.0,  # YES went to 0; NO won — favorable for us
                days_ago=1,
                hour=12,
            )
        ]
    }
    scored = evaluator.score([(opp, universe_ts)], clv_lookup, match_window_hours=2.0)

    assert len(scored) == 1
    s = scored[0]
    assert s.status == "settled"
    assert s.market_result == "no"
    # NO @ 70c, YES closes at 0 → implied YES = 30, clv = 30 - 0 = +30
    assert s.clv_cents == pytest.approx(30.0)
    assert s.pnl_cents == pytest.approx(30.0 * evaluator.DEFAULT_CONTRACTS)
    assert s.pnl_cents > 0


# ---------------------------------------------------------------------------
# 5. evaluator scores settled loser
# ---------------------------------------------------------------------------

def test_evaluator_scores_settled_loser():
    """Same candidate as #4, market_result='yes' → negative pnl."""
    opp = CandidateOpportunity(
        ticker="KXTEST-B",
        side="no",
        target_price_cents=70.0,
        fair_value_cents=80.0,
        edge_cents=10.0,
        confidence=0.6,
        reason="test",
    )
    universe_ts = _ts(1, hour=12)
    clv_lookup = {
        "KXTEST-B": [
            _make_clv_record(
                "KXTEST-B",
                side="no",
                status="settled",
                entry_price_cents=70,
                market_result="yes",
                closing_yes_price=100.0,  # YES won — bad for NO holder
                days_ago=1,
                hour=12,
            )
        ]
    }
    scored = evaluator.score([(opp, universe_ts)], clv_lookup, match_window_hours=2.0)

    assert len(scored) == 1
    s = scored[0]
    assert s.status == "settled"
    assert s.market_result == "yes"
    # NO @ 70c, YES closes at 100 → implied YES = 30, clv = 30 - 100 = -70
    assert s.clv_cents == pytest.approx(-70.0)
    assert s.pnl_cents == pytest.approx(-70.0 * evaluator.DEFAULT_CONTRACTS)
    assert s.pnl_cents < 0


# ---------------------------------------------------------------------------
# 6. evaluator UNRESOLVED when no clv match
# ---------------------------------------------------------------------------

def test_evaluator_unresolved_when_no_clv_match():
    """No matching clv record → UNRESOLVED tag, no crash."""
    opp = CandidateOpportunity(
        ticker="KXTEST-NOPE",
        side="yes",
        target_price_cents=50.0,
        fair_value_cents=60.0,
        edge_cents=10.0,
        confidence=0.5,
        reason="test",
    )
    scored = evaluator.score([(opp, _ts(1))], clv_lookup={}, match_window_hours=2.0)

    assert len(scored) == 1
    assert scored[0].status == evaluator.UNRESOLVED
    assert scored[0].pnl_cents is None
    assert scored[0].clv_cents is None


# ---------------------------------------------------------------------------
# 7. reports renders zero-candidates gracefully
# ---------------------------------------------------------------------------

def test_reports_renders_zero_candidates_gracefully(lab_state):
    """Candidate that returns None for every market → report file written, summary 'n=0'."""
    # Seed a small universe but use a candidate that emits nothing.
    rows = [_make_universe_row(f"KXTEST-{i:03d}", days_ago=1) for i in range(10)]
    lab_state["universe_file"].write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    lab_state["clv_file"].write_text("[]")

    class NeverEmit:
        name = "never_emit"

        def evaluate(self, market, context=None):
            return None

    report_path, summary = driver.run(NeverEmit(), days=14)

    assert summary["n_total"] == 0
    assert summary["n_resolved"] == 0
    assert report_path.exists()
    body = report_path.read_text()
    assert "0 would-have-bets" in body
    assert "Nothing to score" in body


# ---------------------------------------------------------------------------
# 8. reports renders with candidates
# ---------------------------------------------------------------------------

def test_reports_renders_with_candidates(lab_state):
    """3 opps, 2 settled → markdown contains candidate name, sample tickers, mean CLV."""
    rows = [
        _make_universe_row("KXTEST-A", days_ago=1, yes_ask=50, volume_24h=5000),
        _make_universe_row("KXTEST-B", days_ago=1, yes_ask=50, volume_24h=5000),
        _make_universe_row("KXTEST-C", days_ago=1, yes_ask=50, volume_24h=5000),
    ]
    lab_state["universe_file"].write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    clv = [
        _make_clv_record("KXTEST-A", side="yes", status="settled",
                         entry_price_cents=50, market_result="yes",
                         closing_yes_price=100.0, days_ago=1, hour=12),
        _make_clv_record("KXTEST-B", side="yes", status="counterfactual_settled",
                         entry_price_cents=50, market_result="no",
                         closing_yes_price=0.0, days_ago=1, hour=12),
        # KXTEST-C has no clv record → UNRESOLVED
    ]
    lab_state["clv_file"].write_text(json.dumps(clv))

    class FlatYesAt50:
        name = "flat_yes_at_50"

        def evaluate(self, market, context=None):
            return CandidateOpportunity(
                ticker=market["ticker"],
                side="yes",
                target_price_cents=50.0,
                fair_value_cents=60.0,
                edge_cents=10.0,
                confidence=0.5,
                reason="flat-50 stub",
            )

    report_path, summary = driver.run(FlatYesAt50(), days=14)
    body = report_path.read_text()

    assert summary["n_total"] == 3
    assert summary["n_resolved"] == 2
    assert "flat_yes_at_50" in body
    assert "KXTEST-A" in body  # winner shows up in top-5
    assert "KXTEST-B" in body  # loser shows up in top-5
    # mean CLV header line — two settled trades, +50 and -50 → mean 0
    assert summary["mean_clv_cents"] == pytest.approx(0.0)
    # Per-sport row should appear; KXTEST-* maps to None → unknown bucket
    assert "Per-sport" in body


# ---------------------------------------------------------------------------
# 9. canonical schema discipline (forbid 'no_won' substring in sources)
# ---------------------------------------------------------------------------

def test_canonical_schema_used_throughout():
    """Parse all tools/strategy_lab/*.py — assert 'no_won' substring is absent.

    Mirrors Session 45's corrected-schema discovery: the canonical
    market_result enum is 'no' / 'yes', NOT 'no_won' / 'yes_won'. Two real
    verification errors traced to schema-value typos in May 1, 2026; this
    test prevents a third in the lab itself.
    """
    lab_dir = Path(__file__).resolve().parent.parent / "tools" / "strategy_lab"
    py_files = sorted(lab_dir.rglob("*.py"))
    assert py_files, "expected .py files under tools/strategy_lab/"
    for f in py_files:
        text = f.read_text()
        # Strip strings that are *describing* the anti-pattern (this test
        # file plus README), but the actual lab .py files must NEVER
        # carry the literal substring.
        assert "no_won" not in text, (
            f"{f} contains anti-pattern 'no_won' — canonical schema is "
            f"market_result == 'no' / 'yes' (NEVER 'no_won' / 'yes_won')."
        )
        assert "yes_won" not in text, (
            f"{f} contains anti-pattern 'yes_won' — canonical schema is "
            f"market_result == 'no' / 'yes'."
        )


# ---------------------------------------------------------------------------
# 10. driver smoke run on real data does not raise
# ---------------------------------------------------------------------------

def test_driver_smoke_real_data_does_not_raise(monkeypatch, tmp_path):
    """Run the example candidate against actual bot/state/ data.

    Output may be empty (universe may not include matching KXNBAGAME-TOTAL
    markets in the captured window — that's fine; the lab handles 0
    matches gracefully). Asserts no exception.
    """
    # Use real bot/state paths — the lab is read-only, so this is safe.
    # Override report output to tmp_path so we don't pollute reports_out/.
    out_dir = tmp_path / "smoke_reports"
    out_dir.mkdir()

    report_path, summary = driver.run(EXAMPLE_STRATEGY, days=14, out_dir=out_dir)
    assert report_path.exists()
    # Either zero candidates or some — both are acceptable.
    assert summary["n_total"] >= 0
    assert summary["n_unresolved"] >= 0


# ---------------------------------------------------------------------------
# Bonus: gzipped archive coverage (data_loader) — guards against archive-only
# windows where universe.jsonl doesn't exist (current-day rotation just fired).
# Not in the brief's 10 cases but cheap insurance.
# ---------------------------------------------------------------------------

def test_data_loader_reads_gzipped_archives(lab_state):
    """Archive-only window (no current universe.jsonl) should still yield rows."""
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    archive_path = lab_state["archive_dir"] / f"universe-{yesterday.isoformat()}.jsonl.gz"
    rows = [_make_universe_row(f"KXARCH-{i:03d}", days_ago=1) for i in range(5)]
    with gzip.open(archive_path, "wt") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")
    lab_state["clv_file"].write_text("[]")
    # Note: lab_state["universe_file"] does not exist (no current jsonl).

    universe_iter, _, _, _, _ = data_loader.load_window(days=14)
    yielded = list(universe_iter)
    assert len(yielded) == 5
    assert {r["ticker"] for r in yielded} == {f"KXARCH-{i:03d}" for i in range(5)}


# ---------------------------------------------------------------------------
# Session 73 — per-pair-key dedup in evaluator.aggregate()
# ---------------------------------------------------------------------------
#
# Founding example: Session 72's cross_market_correlation prototype showed
# +$2,567 per-emit Σ flipping to -$4.16 per-unique-pair-key Σ on dedup.
# Stateful candidates re-emit on every scan while a divergence persists;
# the lab now counts them once per unique outcome to match the real
# "you'd enter the trade once" semantic.
#
# Tests 1-4 exercise the aggregator math directly (constructed
# ScoredOpportunity instances). Test 5 wires the real cross_market_correlation
# candidate end-to-end through driver.run() with a synthetic universe +
# clv fixture mirroring Session 72's amplification shape.
# ---------------------------------------------------------------------------

def _make_scored(
    *,
    pair_key: str | None,
    pnl_cents: float,
    sport: str | None = "test_sport",
    confidence: float = 0.5,
    ticker: str = "KXTEST",
) -> "evaluator.ScoredOpportunity":
    """Build a settled ScoredOpportunity with the given pair_key and pnl."""
    opp = CandidateOpportunity(
        ticker=ticker,
        side="yes",
        target_price_cents=50.0,
        fair_value_cents=60.0,
        edge_cents=10.0,
        confidence=confidence,
        reason="test",
        pair_key=pair_key,
    )
    return evaluator.ScoredOpportunity(
        opp=opp,
        universe_ts=_ts(1),
        sport=sport,
        status="settled",
        market_result="yes",
        clv_cents=pnl_cents / 100.0,  # pnl_cents = clv_cents × 100 contracts
        pnl_cents=pnl_cents,
        contracts=evaluator.DEFAULT_CONTRACTS,
    )


def test_aggregate_dedups_by_pair_key_stateful_single_key():
    """Stateful candidate emitting 100 times with same pair_key.

    Per-emit Σ counts all 100; per-pair-key Σ counts only the first emit.
    Verifies first-emit-wins dedup math AND backward-compat preservation
    of per-emit numbers (still available for diagnostic).
    """
    # All 100 share pair_key="A|yes" with pnl_cents=+50 each.
    # Per-emit Σ = 100 × 50 = 5,000 ; Per-pair-key Σ = 50 (first emit only).
    scored = [
        _make_scored(pair_key="A|yes", pnl_cents=50.0)
        for _ in range(100)
    ]
    summary = evaluator.aggregate(scored)

    # Per-emit (preserved verbatim from pre-Session-73)
    assert summary["n_total"] == 100
    assert summary["n_resolved"] == 100
    assert summary["total_pnl_cents"] == pytest.approx(5_000.0)

    # Per-unique-pair-key (Session 73 headline)
    assert summary["n_unique_pair_keys"] == 1
    assert summary["n_resolved_pair_keys"] == 1
    assert summary["total_pnl_cents_per_pair_key"] == pytest.approx(50.0)

    # Median amplification ratio
    assert summary["median_emits_per_pair_key"] == pytest.approx(100.0)


def test_aggregate_dedups_by_pair_key_stateful_distinct_keys():
    """Stateful candidate emitting 5 times with 5 distinct pair_keys.

    No dedup happens (all keys unique); per-pair-key Σ equals per-emit Σ.
    """
    scored = [
        _make_scored(pair_key=f"K{i}|yes", pnl_cents=10.0 + i)
        for i in range(5)
    ]
    summary = evaluator.aggregate(scored)

    assert summary["n_total"] == 5
    assert summary["n_unique_pair_keys"] == 5
    # Sum 10 + 11 + 12 + 13 + 14 = 60.
    assert summary["total_pnl_cents"] == pytest.approx(60.0)
    assert summary["total_pnl_cents_per_pair_key"] == pytest.approx(60.0)
    # Each key has 1 emit → median = 1.
    assert summary["median_emits_per_pair_key"] == pytest.approx(1.0)


def test_aggregate_one_shot_pair_key_none_backward_compat():
    """One-shot candidate (pair_key=None) — per-pair-key Σ exactly equals per-emit Σ.

    Backward-compat regression guard for ``example_total_points_under``
    and any future one-shot candidate. Each None pair_key counts as its
    own unique bucket so Σ is identical both ways.
    """
    scored = [
        _make_scored(pair_key=None, pnl_cents=20.0),
        _make_scored(pair_key=None, pnl_cents=30.0),
        _make_scored(pair_key=None, pnl_cents=-15.0),
    ]
    summary = evaluator.aggregate(scored)

    assert summary["n_total"] == 3
    # Each None pair_key is treated as its own bucket — no collapse.
    assert summary["n_unique_pair_keys"] == 3
    # Sum 20 + 30 - 15 = 35.
    assert summary["total_pnl_cents"] == pytest.approx(35.0)
    # CRITICAL: per-pair-key Σ EXACTLY equals per-emit Σ on one-shot.
    assert summary["total_pnl_cents_per_pair_key"] == pytest.approx(
        summary["total_pnl_cents"]
    )
    assert summary["mean_clv_cents_per_pair_key"] == pytest.approx(
        summary["mean_clv_cents"]
    )
    assert summary["win_rate_pct_per_pair_key"] == pytest.approx(
        summary["win_rate_pct"]
    )


def test_aggregate_mixed_stateful_and_one_shot():
    """Mixed: 50 emits across 3 stateful pair_keys (20+20+10) + 2 one-shot None.

    Per-pair-key bucket count = 3 stateful + 2 one-shot (each None its own
    bucket) = 5. Per-pair-key Σ uses 1 emit per stateful key + both
    one-shot emits.
    """
    scored: list[evaluator.ScoredOpportunity] = []
    # 20 emits with pair_key="X" at pnl=+5 each → first-wins contributes +5.
    scored.extend(_make_scored(pair_key="X", pnl_cents=5.0) for _ in range(20))
    # 20 emits with pair_key="Y" at pnl=-3 each → first-wins contributes -3.
    scored.extend(_make_scored(pair_key="Y", pnl_cents=-3.0) for _ in range(20))
    # 10 emits with pair_key="Z" at pnl=+2 each → first-wins contributes +2.
    scored.extend(_make_scored(pair_key="Z", pnl_cents=2.0) for _ in range(10))
    # 2 one-shot emits (pair_key=None) at pnl=+10 and -4.
    scored.append(_make_scored(pair_key=None, pnl_cents=10.0))
    scored.append(_make_scored(pair_key=None, pnl_cents=-4.0))

    summary = evaluator.aggregate(scored)

    # Per-emit total: 20×5 + 20×(-3) + 10×2 + 10 + (-4) = 100 - 60 + 20 + 10 - 4 = 66
    assert summary["n_total"] == 52
    assert summary["total_pnl_cents"] == pytest.approx(66.0)

    # Per-pair-key: 3 stateful buckets + 2 one-shot buckets = 5 unique.
    assert summary["n_unique_pair_keys"] == 5
    # Per-pair-key total: +5 + (-3) + +2 + +10 + (-4) = 10
    assert summary["total_pnl_cents_per_pair_key"] == pytest.approx(10.0)

    # Median emits per key. Counts (sorted): [1, 1, 10, 20, 20] → median = 10.
    assert summary["median_emits_per_pair_key"] == pytest.approx(10.0)


def test_cross_market_correlation_synthetic_amplification_regression(lab_state):
    """End-to-end synthetic regression mirroring Session 72's sign-flip pattern.

    Three matchups with deliberately uneven emit counts (50/10/1) and
    settled outcomes designed so that:
      - Per-emit Σ comes out POSITIVE (winner's amplification dominates)
      - Per-pair-key Σ comes out NEGATIVE (single emit per unique
        outcome, both losers drag the net down)
    Locks the dedup methodology in isolation. The actual Session 72
    -$4.16 / +$2,567 reproduction lives in the manual verification gate
    (``python3 -m tools.strategy_lab.driver --candidate
    cross_market_correlation --days 14``), not here — that depends on
    real bot/state/ data drift since 2026-05-08.
    """
    from tools.strategy_lab.candidates.cross_market_correlation import (
        CrossMarketCorrelation,
    )

    rows: list[dict] = []
    # 50 scans of matchup A (winner). Series row first, then game row.
    for scan_idx in range(50):
        scan_id = f"SCAN-{scan_idx:04d}"
        rows.append(_make_universe_row(
            "KXNBASERIES-26AAABBBR1-AAA",
            days_ago=1,
            scan_id=scan_id,
            yes_ask=40,         # series_yes
            volume_24h=5000,
        ))
        rows.append(_make_universe_row(
            "KXNBAGAME-26MAY01AAABBB-AAA",
            days_ago=1,
            scan_id=scan_id,
            yes_ask=70,         # game_yes (Δ=30; side=no, target=30)
            volume_24h=5000,
        ))
        if scan_idx < 10:
            # Matchup B (loser) on the first 10 scans.
            rows.append(_make_universe_row(
                "KXNBASERIES-26CCCDDDR1-CCC",
                days_ago=1,
                scan_id=scan_id,
                yes_ask=20,
                volume_24h=5000,
            ))
            rows.append(_make_universe_row(
                "KXNBAGAME-26MAY02CCCDDD-CCC",
                days_ago=1,
                scan_id=scan_id,
                yes_ask=30,     # Δ=10; side=no, target=70
                volume_24h=5000,
            ))
        if scan_idx < 1:
            # Matchup C (loser) on the first scan only.
            rows.append(_make_universe_row(
                "KXNBASERIES-26EEEFFFR1-EEE",
                days_ago=1,
                scan_id=scan_id,
                yes_ask=10,
                volume_24h=5000,
            ))
            rows.append(_make_universe_row(
                "KXNBAGAME-26MAY03EEEFFF-EEE",
                days_ago=1,
                scan_id=scan_id,
                yes_ask=20,     # Δ=10; side=no, target=80
                volume_24h=5000,
            ))
    lab_state["universe_file"].write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )

    # Three settled clv records, one per game ticker.
    clv = [
        # Matchup A: NO @ 30c, NO_won, closing_yes=0 → clv = +70
        _make_clv_record(
            "KXNBAGAME-26MAY01AAABBB-AAA",
            side="no",
            status="settled",
            entry_price_cents=30,
            market_result="no",
            closing_yes_price=0.0,
            days_ago=1,
            hour=12,
        ),
        # Matchup B: NO @ 70c, YES_won, closing_yes=100 → clv = -70
        _make_clv_record(
            "KXNBAGAME-26MAY02CCCDDD-CCC",
            side="no",
            status="settled",
            entry_price_cents=70,
            market_result="yes",
            closing_yes_price=100.0,
            days_ago=1,
            hour=12,
        ),
        # Matchup C: NO @ 80c, YES_won, closing_yes=100 → clv = -80
        _make_clv_record(
            "KXNBAGAME-26MAY03EEEFFF-EEE",
            side="no",
            status="settled",
            entry_price_cents=80,
            market_result="yes",
            closing_yes_price=100.0,
            days_ago=1,
            hour=12,
        ),
    ]
    lab_state["clv_file"].write_text(json.dumps(clv))

    # Fresh strategy instance — module-level STRATEGY accumulates state.
    fresh = CrossMarketCorrelation()
    report_path, summary = driver.run(fresh, days=14)

    # Per-emit shape.
    # Total emits: 50 (A) + 10 (B) + 1 (C) = 61.
    assert summary["n_total"] == 61
    assert summary["n_resolved"] == 61
    # Per-emit Σ:
    #   A: 50 × (+70 × 100 contracts) = +350,000 cents
    #   B: 10 × (-70 × 100 contracts) =  -70,000 cents
    #   C:  1 × (-80 × 100 contracts) =   -8,000 cents
    #   total = +272,000 cents = $+2,720
    assert summary["total_pnl_cents"] == pytest.approx(272_000.0)
    assert summary["total_pnl_dollars"] == pytest.approx(2_720.0)

    # Per-pair-key shape.
    assert summary["n_unique_pair_keys"] == 3
    assert summary["n_resolved_pair_keys"] == 3
    # Per-pair-key Σ:
    #   A first emit: +70 × 100 = +7,000
    #   B first emit: -70 × 100 = -7,000
    #   C first emit: -80 × 100 = -8,000
    #   total = -8,000 cents = $-80
    assert summary["total_pnl_cents_per_pair_key"] == pytest.approx(-8_000.0)
    assert summary["total_pnl_dollars_per_pair_key"] == pytest.approx(-80.0)

    # SIGN FLIP — the founding-example methodology check.
    assert summary["total_pnl_cents"] > 0, "per-emit should be positive"
    assert summary["total_pnl_cents_per_pair_key"] < 0, (
        "per-pair-key should flip negative — same shape as Session 72"
    )

    # Median amplification ratio. Counts: A=50, B=10, C=1 → median = 10.
    assert summary["median_emits_per_pair_key"] == pytest.approx(10.0)

    # Report headline section renders the per-pair-key story.
    body = report_path.read_text()
    assert "Per unique pair-key (HEADLINE)" in body
    assert "Per emit (DIAGNOSTIC" in body
