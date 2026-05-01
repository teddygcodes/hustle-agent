"""threshold_proximity heuristic — flags reject-gates where many rejects sit near threshold."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tools.discovery_agent.heuristics.threshold_proximity import (
    MIN_REJECTS_PER_BUCKET,
    ThresholdProximity,
)


_NOW = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)


def _ctx(decisions=None, clv=None):
    return SimpleNamespace(
        decisions=decisions or [],
        clv=clv or [],
        loaded_at=_NOW,
    )


def _reject(reason, ticker, extra, days_ago=1):
    ts = _NOW - dt.timedelta(days=days_ago)
    return {
        "ts": ts.isoformat(), "ticker": ticker, "decision": "reject",
        "reason": reason, "extra": extra,
    }


def test_threshold_proximity_positive_non_stable_floor():
    """5+ rejects within 5% of floor=0.93 → emits a finding."""
    decisions = [
        _reject("non_stable_below_weather_floor",
                f"KXNBA-26-T{i}",
                {"no_ask_prob": 0.91, "floor": 0.93})  # gap=0.02, band=0.0465 → near-miss
        for i in range(MIN_REJECTS_PER_BUCKET)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions))
    assert len(findings) == 1
    f = findings[0]
    assert f.evidence["reason"] == "non_stable_below_weather_floor"
    assert f.evidence["sport"] == "nba_futures"
    assert f.evidence["near_miss_count"] == MIN_REJECTS_PER_BUCKET


def test_threshold_proximity_negative_below_min_count():
    """Fewer than MIN_REJECTS_PER_BUCKET near-misses → no finding."""
    decisions = [
        _reject("non_stable_below_weather_floor",
                f"KXNBA-26-T{i}",
                {"no_ask_prob": 0.91, "floor": 0.93})
        for i in range(MIN_REJECTS_PER_BUCKET - 1)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions))
    assert findings == []


def test_threshold_proximity_negative_far_from_threshold():
    """Rejects beyond the proximity band → not flagged as near-miss."""
    decisions = [
        _reject("non_stable_below_weather_floor",
                f"KXNBA-26-T{i}",
                {"no_ask_prob": 0.50, "floor": 0.93})  # gap=0.43, way past band
        for i in range(MIN_REJECTS_PER_BUCKET + 5)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions))
    assert findings == []


def test_threshold_proximity_boundary_exactly_at_band_edge():
    """observed sits at gap == band → counts as near-miss (<= band)."""
    # threshold=0.93, band=0.05*0.93=0.0465. observed=0.8835 → gap=0.0465 (exactly at edge)
    decisions = [
        _reject("non_stable_below_weather_floor",
                f"KXNBA-26-T{i}",
                {"no_ask_prob": 0.8835, "floor": 0.93})
        for i in range(MIN_REJECTS_PER_BUCKET)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions))
    assert len(findings) == 1


def test_threshold_proximity_skips_unknown_reasons():
    """Reasons outside THRESHOLD_REASONS are ignored."""
    decisions = [
        _reject("unknown_reason", f"KXNBA-26-T{i}", {"some_field": 0.5})
        for i in range(MIN_REJECTS_PER_BUCKET + 5)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions))
    assert findings == []


def test_threshold_proximity_skips_records_outside_lookback():
    """Decisions older than LOOKBACK_DAYS → ignored."""
    decisions = [
        _reject("non_stable_below_weather_floor",
                f"KXNBA-26-T{i}",
                {"no_ask_prob": 0.91, "floor": 0.93}, days_ago=30)
        for i in range(MIN_REJECTS_PER_BUCKET + 5)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions))
    assert findings == []


def test_threshold_proximity_clv_cross_reference_promotes_severity():
    """Near-miss tickers that later +CLV → severity 'notable' (else 'info')."""
    decisions = [
        _reject("non_stable_below_weather_floor",
                f"KXNBA-26-T{i}",
                {"no_ask_prob": 0.91, "floor": 0.93})
        for i in range(MIN_REJECTS_PER_BUCKET)
    ]
    clv = [
        {"ticker": f"KXNBA-26-T{i}", "status": "counterfactual_settled", "clv_cents": 10}
        for i in range(3)  # 3 of 5 near-miss tickers later +CLV
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions, clv=clv))
    assert findings[0].severity == "notable"
    assert findings[0].evidence["clv_positive_near_miss_tickers"] == 3


def test_threshold_proximity_edge_below_threshold_uses_min_edge_field():
    """edge_below_threshold reads (edge, min_edge) from extra — not (no_ask_prob, floor)."""
    decisions = [
        _reject("edge_below_threshold",
                f"KXNBA-26-T{i}",
                {"edge": 0.019, "min_edge": 0.02})
        for i in range(MIN_REJECTS_PER_BUCKET)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions))
    assert len(findings) == 1
    assert findings[0].evidence["observed_field"] == "edge"
    assert findings[0].evidence["threshold_field"] == "min_edge"


def test_threshold_proximity_skips_when_extra_missing_fields():
    """Records with reason=non_stable_below_weather_floor but no no_ask_prob/floor → skipped."""
    decisions = [
        _reject("non_stable_below_weather_floor", f"KXNBA-26-T{i}", {})
        for i in range(MIN_REJECTS_PER_BUCKET + 5)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=decisions))
    assert findings == []


def test_threshold_proximity_groups_by_sport_and_reason():
    """Same reason, two different sports → two separate findings."""
    nba_rejects = [
        _reject("non_stable_below_weather_floor",
                f"KXNBA-26-T{i}",
                {"no_ask_prob": 0.91, "floor": 0.93})
        for i in range(MIN_REJECTS_PER_BUCKET)
    ]
    nhl_rejects = [
        _reject("non_stable_below_weather_floor",
                f"KXNHL-26-T{i}",
                {"no_ask_prob": 0.91, "floor": 0.93})
        for i in range(MIN_REJECTS_PER_BUCKET)
    ]
    findings = ThresholdProximity().run(_ctx(decisions=nba_rejects + nhl_rejects))
    sports = sorted(f.evidence["sport"] for f in findings)
    assert sports == ["nba_futures", "nhl_futures"]


def test_threshold_proximity_missing_source_returns_empty():
    findings = ThresholdProximity().run(_ctx())
    assert findings == []
