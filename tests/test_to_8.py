"""Tests for bot quality improvements (5 → 8)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Task 1: Outcome Tracker ──────────────────────────────────────────────────

def test_alert_stored_returns_id():
    """store_alert() must return a positive integer ID."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXNBAGAME-26APR05LACSAC-SAC",
            "type": "series_game_edge",
            "edge": 0.23,
            "relative_edge": 0.23,
            "confidence": 0.70,
            "recommended_side": "yes",
            "kalshi_price": 0.14,
            "market": {"close_time": "2026-04-13T00:00:00Z"},
        })
        assert isinstance(alert_id, int) and alert_id > 0
    finally:
        os.unlink(db_path)


def test_resolution_records_win():
    """record_resolution() with YES result on BUY YES alert marks won=True."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXTEST-YES",
            "type": "weather",
            "edge": 0.10,
            "relative_edge": 0.30,
            "confidence": 0.75,
            "recommended_side": "yes",
            "kalshi_price": 0.10,
            "market": {"close_time": "2026-04-05T05:00:00Z"},
        })
        tracker.record_resolution(alert_id, result="yes")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 1
        assert stats["total"] == 1
        assert stats["win_rate"] == 1.0
    finally:
        os.unlink(db_path)


def test_resolution_records_loss():
    """record_resolution() with NO result on BUY YES alert marks won=False."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXTEST-LOSS",
            "type": "weather",
            "edge": 0.10,
            "relative_edge": 0.30,
            "confidence": 0.75,
            "recommended_side": "yes",
            "kalshi_price": 0.10,
            "market": {"close_time": "2026-04-05T05:00:00Z"},
        })
        tracker.record_resolution(alert_id, result="no")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 0
        assert stats["win_rate"] == 0.0
    finally:
        os.unlink(db_path)


def test_calibration_flags_underperforming_strategy():
    """get_calibration_report() flags strategies with win_rate < expected after 50+ samples."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        # 50 alerts at 25% edge but only 30% wins (expected ~65%, actual 30%)
        for i in range(50):
            aid = tracker.store_alert({
                "ticker": f"KXTEST-{i}",
                "type": "series_game_edge",
                "edge": 0.25,
                "relative_edge": 0.25,
                "confidence": 0.70,
                "recommended_side": "yes",
                "kalshi_price": 0.40,
                "market": {"close_time": "2026-04-05T00:00:00Z"},
            })
            tracker.record_resolution(aid, result="yes" if i < 15 else "no")
        report = tracker.get_calibration_report()
        assert "series_game_edge" in report
        assert report["series_game_edge"]["flagged"] is True
    finally:
        os.unlink(db_path)
