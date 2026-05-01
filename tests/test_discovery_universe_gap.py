"""universe_gap heuristic — flag (sport, market_type) decision-touched but absent from today's universe."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tools.discovery_agent.heuristics.universe_gap import (
    MIN_DECISION_COUNT,
    UniverseGap,
)


_NOW = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)


def _ctx(decisions=None, universe_rows=None):
    rows = list(universe_rows or [])

    def universe_iter():
        return iter(rows)

    return SimpleNamespace(
        decisions=decisions or [],
        universe_iter=universe_iter,
        loaded_at=_NOW,
    )


def _decision(ticker, days_ago=1):
    ts = _NOW - dt.timedelta(days=days_ago)
    return {"ts": ts.isoformat(), "ticker": ticker, "decision": "reject", "reason": "x"}


def _universe_row(ticker, scan_id, days_ago=0):
    ts = _NOW - dt.timedelta(days=days_ago)
    return {"ts": ts.isoformat(), "scan_id": scan_id, "ticker": ticker}


def test_universe_gap_positive_decision_pair_absent_from_today_universe():
    """5 decisions on KXMLBGAME-* in last 7d; today's universe has no KXMLBGAME-* → fire."""
    decisions = [_decision(f"KXMLBGAME-26APR29-T{i}") for i in range(MIN_DECISION_COUNT)]
    universe = [
        _universe_row("KXNHLGAME-26MAY01-X", scan_id="scan-2"),
        _universe_row("KXATPMATCH-26MAY01-Y", scan_id="scan-2"),
    ]
    findings = UniverseGap().run(_ctx(decisions=decisions, universe_rows=universe))
    assert len(findings) == 1
    assert findings[0].evidence["sport"] == "mlb_game"
    assert findings[0].evidence["market_type"] == "KXMLBGAME"
    assert findings[0].evidence["recent_decision_count"] == MIN_DECISION_COUNT


def test_universe_gap_negative_pair_present_in_today_universe():
    """5 decisions on KXMLBGAME, today's universe has KXMLBGAME → no finding."""
    decisions = [_decision(f"KXMLBGAME-26APR29-T{i}") for i in range(MIN_DECISION_COUNT + 5)]
    universe = [_universe_row(f"KXMLBGAME-26MAY01-X{i}", scan_id="s") for i in range(3)]
    findings = UniverseGap().run(_ctx(decisions=decisions, universe_rows=universe))
    assert findings == []


def test_universe_gap_negative_below_min_decision_count():
    """4 decisions on KXMLBGAME → no finding even if absent from universe."""
    decisions = [_decision(f"KXMLBGAME-26APR29-T{i}") for i in range(MIN_DECISION_COUNT - 1)]
    universe = [_universe_row("KXNHLGAME-26MAY01-X", scan_id="s")]
    findings = UniverseGap().run(_ctx(decisions=decisions, universe_rows=universe))
    assert findings == []


def test_universe_gap_boundary_at_min_decision_count():
    """Exactly MIN_DECISION_COUNT decisions, pair absent → fires."""
    decisions = [_decision(f"KXMLBGAME-26APR29-T{i}") for i in range(MIN_DECISION_COUNT)]
    universe = [_universe_row("KXNHLGAME-26MAY01-X", scan_id="s")]
    findings = UniverseGap().run(_ctx(decisions=decisions, universe_rows=universe))
    assert len(findings) == 1


def test_universe_gap_uses_only_latest_scan_id():
    """Older scan_ids ignored; only the most recent ts's scan_id defines 'today'."""
    decisions = [_decision(f"KXMLBGAME-26APR29-T{i}") for i in range(MIN_DECISION_COUNT)]
    # Older scan HAD KXMLBGAME (would suppress), but a newer scan doesn't
    universe = [
        _universe_row("KXMLBGAME-26APR28-X", scan_id="old", days_ago=2),
        _universe_row("KXNHLGAME-26MAY01-X", scan_id="latest", days_ago=0),
    ]
    findings = UniverseGap().run(_ctx(decisions=decisions, universe_rows=universe))
    assert len(findings) == 1
    assert findings[0].evidence["latest_universe_scan_id"] == "latest"


def test_universe_gap_skips_decisions_outside_lookback():
    """Decisions older than 7d → ignored."""
    decisions = [_decision(f"KXMLBGAME-26APR29-T{i}", days_ago=14)
                 for i in range(MIN_DECISION_COUNT + 5)]
    universe = [_universe_row("KXNHLGAME-26MAY01-X", scan_id="s")]
    findings = UniverseGap().run(_ctx(decisions=decisions, universe_rows=universe))
    assert findings == []


def test_universe_gap_empty_universe_returns_empty():
    """If universe.jsonl is empty, can't determine 'today's' set → bail."""
    decisions = [_decision(f"KXMLBGAME-26APR29-T{i}") for i in range(MIN_DECISION_COUNT)]
    findings = UniverseGap().run(_ctx(decisions=decisions, universe_rows=[]))
    assert findings == []


def test_universe_gap_empty_decisions_returns_empty():
    universe = [_universe_row("KXNHLGAME-X", scan_id="s")]
    findings = UniverseGap().run(_ctx(decisions=[], universe_rows=universe))
    assert findings == []


def test_universe_gap_distinguishes_futures_from_per_game():
    """KXMLB-* (futures) and KXMLBGAME-* (per-game) are different pairs."""
    decisions = []
    decisions += [_decision(f"KXMLBGAME-26APR29-T{i}") for i in range(MIN_DECISION_COUNT)]
    decisions += [_decision(f"KXMLB-26-T{i}") for i in range(MIN_DECISION_COUNT)]
    # Universe has only KXMLB futures (per-game absent)
    universe = [_universe_row(f"KXMLB-26-X{i}", scan_id="s") for i in range(2)]
    findings = UniverseGap().run(_ctx(decisions=decisions, universe_rows=universe))
    # KXMLBGAME absent → fires; KXMLB present → not flagged
    assert len(findings) == 1
    assert findings[0].evidence["sport"] == "mlb_game"
    assert findings[0].evidence["market_type"] == "KXMLBGAME"


def test_universe_gap_missing_universe_iter_returns_empty():
    """universe_iter is a callable; missing/empty rows → empty."""
    decisions = [_decision(f"KXMLBGAME-26APR29-T{i}") for i in range(MIN_DECISION_COUNT)]
    findings = UniverseGap().run(_ctx(decisions=decisions))  # universe_rows defaults to []
    assert findings == []
