"""
Tests for bot/outcome_tracker.py and bot/tracker.py

Covers:
- OutcomeTracker.get_pending_resolution() — 3-hour cutoff (Fix 2 regression)
- OutcomeTracker.record_resolution() — YES/NO win/loss assignment
- OutcomeTracker.store_alert() — idempotency
- OutcomeTracker.get_calibration_report() — flagging logic
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tracker(tmp_path):
    """Fresh OutcomeTracker backed by a temp SQLite DB."""
    from bot.outcome_tracker import OutcomeTracker
    db = tmp_path / "test_outcomes.db"
    return OutcomeTracker(db_path=str(db))


def _make_alert(ticker="KXTEST-01", strategy="weather", side="yes",
                kalshi_price=0.45, edge=0.10, relative_edge=0.22,
                close_time=None):
    return {
        "ticker": ticker,
        "type": strategy,
        "recommended_side": side,
        "kalshi_price": kalshi_price,
        "edge": edge,
        "relative_edge": relative_edge,
        "market": {"close_time": close_time or ""},
    }


# ---------------------------------------------------------------------------
# OutcomeTracker.store_alert — idempotency
# ---------------------------------------------------------------------------

class TestStoreAlert:

    def test_stores_alert_and_returns_id(self, tracker):
        opp = _make_alert()
        alert_id = tracker.store_alert(opp)
        assert isinstance(alert_id, int)
        assert alert_id > 0

    def test_idempotent_same_ticker_strategy(self, tracker):
        """Calling store_alert twice for same ticker+strategy returns the same ID."""
        opp = _make_alert()
        id1 = tracker.store_alert(opp)
        id2 = tracker.store_alert(opp)
        assert id1 == id2

    def test_different_strategy_gets_new_row(self, tracker):
        """Different strategy for same ticker gets its own row."""
        opp1 = _make_alert(strategy="weather")
        opp2 = _make_alert(strategy="series_game_edge")
        id1 = tracker.store_alert(opp1)
        id2 = tracker.store_alert(opp2)
        assert id1 != id2


# ---------------------------------------------------------------------------
# OutcomeTracker.record_resolution — win/loss assignment
# ---------------------------------------------------------------------------

class TestRecordResolution:

    def test_yes_side_yes_result_is_win(self, tracker):
        alert_id = tracker.store_alert(_make_alert(side="yes"))
        tracker.record_resolution(alert_id, "yes")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 1
        assert stats["losses"] == 0

    def test_yes_side_no_result_is_loss(self, tracker):
        alert_id = tracker.store_alert(_make_alert(side="yes"))
        tracker.record_resolution(alert_id, "no")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 0
        assert stats["losses"] == 1

    def test_no_side_no_result_is_win(self, tracker):
        alert_id = tracker.store_alert(_make_alert(side="no"))
        tracker.record_resolution(alert_id, "no")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 1
        assert stats["losses"] == 0

    def test_no_side_yes_result_is_loss(self, tracker):
        alert_id = tracker.store_alert(_make_alert(side="no"))
        tracker.record_resolution(alert_id, "yes")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 0
        assert stats["losses"] == 1

    def test_win_rate_calculation(self, tracker):
        """3 wins out of 4 = 0.75 win rate."""
        for _ in range(3):
            alert_id = tracker.store_alert(_make_alert(ticker=f"KXTEST-{_}"))
            tracker.record_resolution(alert_id, "yes")
        alert_id = tracker.store_alert(_make_alert(ticker="KXTEST-lose"))
        tracker.record_resolution(alert_id, "no")

        stats = tracker.get_stats("weather")
        assert stats["total"] == 4
        assert stats["wins"] == 3
        assert abs(stats["win_rate"] - 0.75) < 0.001


# ---------------------------------------------------------------------------
# OutcomeTracker.get_pending_resolution — 3-hour cutoff (Fix 2 regression)
# ---------------------------------------------------------------------------

class TestGetPendingResolution:

    def test_alert_older_than_3h_is_eligible(self, tracker):
        """An alert stored 4 hours ago should appear in pending resolution."""
        import sqlite3
        four_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        with sqlite3.connect(tracker.db_path) as conn:
            conn.execute(
                """INSERT INTO paper_alerts
                   (ticker, strategy, edge, relative_edge, confidence, side,
                    kalshi_price, close_time, stored_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("KXOLD-01", "weather", 0.1, 0.2, 0.8, "yes", 0.45, "", four_hours_ago),
            )
            conn.commit()

        pending = tracker.get_pending_resolution()
        tickers = [p["ticker"] for p in pending]
        assert "KXOLD-01" in tickers

    def test_alert_stored_2h_ago_is_not_eligible(self, tracker):
        """An alert stored 2 hours ago (< 3h cutoff) should NOT appear."""
        import sqlite3
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        future_close = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        with sqlite3.connect(tracker.db_path) as conn:
            conn.execute(
                """INSERT INTO paper_alerts
                   (ticker, strategy, edge, relative_edge, confidence, side,
                    kalshi_price, close_time, stored_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("KXNEW-01", "weather", 0.1, 0.2, 0.8, "yes", 0.45, future_close, two_hours_ago),
            )
            conn.commit()

        pending = tracker.get_pending_resolution()
        tickers = [p["ticker"] for p in pending]
        assert "KXNEW-01" not in tickers

    def test_expired_close_time_triggers_resolution(self, tracker):
        """An alert whose close_time is in the past should appear in pending, even if < 3h old."""
        import sqlite3
        just_now = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        past_close = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with sqlite3.connect(tracker.db_path) as conn:
            conn.execute(
                """INSERT INTO paper_alerts
                   (ticker, strategy, edge, relative_edge, confidence, side,
                    kalshi_price, close_time, stored_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("KXEXP-01", "weather", 0.1, 0.2, 0.8, "yes", 0.45, past_close, just_now),
            )
            conn.commit()

        pending = tracker.get_pending_resolution()
        tickers = [p["ticker"] for p in pending]
        assert "KXEXP-01" in tickers

    def test_already_resolved_not_returned(self, tracker):
        """Resolved alerts should not appear in pending list."""
        alert_id = tracker.store_alert(_make_alert(ticker="KXDONE-01"))
        tracker.record_resolution(alert_id, "yes")

        pending = tracker.get_pending_resolution()
        tickers = [p["ticker"] for p in pending]
        assert "KXDONE-01" not in tickers


# ---------------------------------------------------------------------------
# OutcomeTracker.get_calibration_report
# ---------------------------------------------------------------------------

class TestCalibrationReport:

    def test_no_data_returns_empty(self, tracker):
        report = tracker.get_calibration_report()
        assert report == {}

    def test_under_50_samples_not_flagged(self, tracker):
        """Strategies with < 50 resolved samples are never flagged."""
        import sqlite3
        # Insert 10 resolved losses (0% win rate — would be flagged if enough samples)
        with sqlite3.connect(tracker.db_path) as conn:
            for i in range(10):
                conn.execute(
                    """INSERT INTO paper_alerts
                       (ticker, strategy, edge, relative_edge, confidence, side,
                        kalshi_price, close_time, stored_at, resolved, result, won)
                       VALUES (?,?,?,?,?,?,?,?,?,1,?,0)""",
                    (f"KXTEST-{i}", "weather", 0.1, 0.2, 0.8, "yes", 0.45, "", "2026-01-01", "no"),
                )
            conn.commit()

        report = tracker.get_calibration_report()
        assert report.get("weather", {}).get("flagged") is False

    def test_strategy_flagged_when_underperforming_with_50_samples(self, tracker):
        """Strategy is flagged if actual win_rate < 75% of expected with 50+ samples."""
        import sqlite3
        # Insert 50 resolved losses (0% win rate, expected ~55%)
        with sqlite3.connect(tracker.db_path) as conn:
            for i in range(50):
                conn.execute(
                    """INSERT INTO paper_alerts
                       (ticker, strategy, edge, relative_edge, confidence, side,
                        kalshi_price, close_time, stored_at, resolved, result, won)
                       VALUES (?,?,?,?,?,?,?,?,?,1,?,0)""",
                    (f"KXBAD-{i}", "bad_strategy", 0.1, 0.2, 0.8, "yes", 0.45, "", "2026-01-01", "no"),
                )
            conn.commit()

        report = tracker.get_calibration_report()
        assert report.get("bad_strategy", {}).get("flagged") is True


# ---------------------------------------------------------------------------
# bot.tracker.log_settlement — Apr 20 settlement-pipeline fix
# ---------------------------------------------------------------------------

@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    """Isolate strategy_audit.json + paper_trades.json in tmp_path for tracker tests."""
    import json as _json
    from bot import tracker as _tracker, config as _config

    audit_file = tmp_path / "strategy_audit.json"
    paper_file = tmp_path / "paper_trades.json"

    audit_file.write_text(_json.dumps({
        "_meta": {},
        "strategies": {
            "vig_stack_series": {"real_trades": 0, "real_pnl": 0, "real_wins": 0, "real_losses": 0, "real_wr": "0%"},
            "live_momentum":    {"real_trades": 0, "real_pnl": 0, "real_wins": 0, "real_losses": 0, "real_wr": "0%"},
        },
        "settlement_log": [],
    }))
    paper_file.write_text("[]")

    monkeypatch.setattr(_tracker, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(_tracker, "PAPER_TRADES_FILE", paper_file)
    monkeypatch.setattr(_config, "PAPER_TRADES_FILE", paper_file)
    return tmp_path, audit_file, paper_file


class TestLogSettlement:

    def test_exited_early_positive_pnl_counts_as_win(self, audit_env):
        import json as _json
        from bot.tracker import log_settlement

        _, audit_file, _ = audit_env
        trade = {"ticker": "T1", "type": "live_momentum", "status": "exited_early",
                 "pnl": 2.50, "contracts": 5}
        assert log_settlement(trade) is True

        audit = _json.loads(audit_file.read_text())
        assert len(audit["settlement_log"]) == 1
        entry = audit["settlement_log"][0]
        assert entry["result"] == "won"
        assert entry["strategy"] == "live_momentum"
        assert audit["strategies"]["live_momentum"]["real_trades"] == 1
        assert audit["strategies"]["live_momentum"]["real_wins"] == 1

    def test_exited_early_negative_pnl_counts_as_loss(self, audit_env):
        import json as _json
        from bot.tracker import log_settlement

        _, audit_file, _ = audit_env
        trade = {"ticker": "T2", "type": "vig_stack", "status": "exited_early",
                 "pnl": -4.10, "contracts": 10}
        assert log_settlement(trade) is True

        audit = _json.loads(audit_file.read_text())
        entry = audit["settlement_log"][0]
        assert entry["result"] == "lost"
        assert entry["strategy"] == "vig_stack_series"
        assert audit["strategies"]["vig_stack_series"]["real_losses"] == 1

    def test_dedup_on_fingerprint(self, audit_env):
        import json as _json
        from bot.tracker import log_settlement

        _, audit_file, _ = audit_env
        trade = {"ticker": "T3", "type": "live_momentum", "status": "exited_early",
                 "pnl": 1.00, "contracts": 3}
        assert log_settlement(trade) is True
        assert log_settlement(trade) is False

        audit = _json.loads(audit_file.read_text())
        assert len(audit["settlement_log"]) == 1
        assert audit["strategies"]["live_momentum"]["real_trades"] == 1

    def test_ghost_trade_rejected(self, audit_env):
        from bot.tracker import log_settlement
        trade = {"ticker": "T4", "type": "live_momentum", "status": "exited_early",
                 "pnl": 1.00, "contracts": 0}
        assert log_settlement(trade) is False

    def test_paper_type_maps_to_strategy_key(self, audit_env):
        import json as _json
        from bot.tracker import log_settlement

        _, audit_file, _ = audit_env
        trade = {"ticker": "T5", "type": "vig_stack", "status": "won",
                 "pnl": 0.15, "contracts": 20}
        assert log_settlement(trade) is True

        audit = _json.loads(audit_file.read_text())
        assert audit["settlement_log"][0]["strategy"] == "vig_stack_series"
        assert audit["strategies"]["vig_stack_series"]["real_trades"] == 1

    def test_invariant_holds_after_batch(self, audit_env):
        from bot.tracker import log_settlement, check_settlement_invariant
        import json as _json

        _, _, paper_file = audit_env
        trades = [
            {"id": "a", "ticker": "A", "type": "live_momentum", "status": "exited_early", "pnl": 1.0, "contracts": 2},
            {"id": "b", "ticker": "B", "type": "live_momentum", "status": "exited_early", "pnl": -0.5, "contracts": 3},
            {"id": "c", "ticker": "C", "type": "vig_stack", "status": "won", "pnl": 0.1, "contracts": 10},
        ]
        paper_file.write_text(_json.dumps(trades))
        for t in trades:
            log_settlement(t)
        assert check_settlement_invariant() is True


# ---------------------------------------------------------------------------
# Session 153: graceful-degrade — NullOutcomeTracker + degraded attribute
# ---------------------------------------------------------------------------
def test_outcome_tracker_degraded_false(tmp_path):
    from bot.outcome_tracker import OutcomeTracker
    t = OutcomeTracker(db_path=str(tmp_path / "ok.db"))
    assert t.degraded is False


def test_null_outcome_tracker_degraded_true():
    from bot.outcome_tracker import NullOutcomeTracker
    stub = NullOutcomeTracker(reason="OperationalError: disk I/O error")
    assert stub.degraded is True
    assert "disk I/O error" in stub.degraded_reason


def test_null_outcome_tracker_noops_full_interface():
    from bot.outcome_tracker import NullOutcomeTracker
    stub = NullOutcomeTracker()
    assert stub.store_alert({"ticker": "X", "type": "vig_stack_series"}) == 0
    assert stub.check_and_resolve() == 0
    assert stub.get_pending_resolution() == []
    assert stub.get_stats("vig_stack") == {}
    assert stub.get_calibration_report() == {}
    assert stub.record_resolution(1, "yes") is None
    assert stub.log_calibration_summary() is None
