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
