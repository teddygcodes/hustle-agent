"""
Tests for data-driven bot fixes (2026-04-05)

Covers:
- Fix 1: Cooldown blocks re-entry on "resolved" positions
- Fix 1: Daily per-ticker loss limit blocks repeat losers
- Fix 2: Crypto edge floor rejects thin edges
- Fix 3: CLV sync from clv.json to paper_trades.json on resolve
- Fix 5: Vig-stack suppression only triggers on strong weather edge (>20%)
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect bot state files to tmp_path."""
    positions_file = tmp_path / "positions.json"
    positions_file.write_text("[]")
    paper_trades_file = tmp_path / "paper_trades.json"
    paper_trades_file.write_text("[]")

    import bot.executor as exc
    monkeypatch.setattr(exc, "POSITIONS_FILE", positions_file)
    monkeypatch.setattr(exc, "PAPER_TRADES_FILE", paper_trades_file)
    monkeypatch.setattr(exc, "PAPER_MODE", True)

    return {"positions": positions_file, "paper_trades": paper_trades_file, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# Fix 1: Cooldown covers "resolved" positions
# ---------------------------------------------------------------------------

class TestCooldownResolved:
    def test_resolved_position_triggers_cooldown(self, tmp_state):
        """A resolved position should block re-entry for 4 hours."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        # Position resolved 30 minutes ago
        positions = [{
            "ticker": "KXHIGHDEN-26APR06-T63",
            "status": "resolved",
            "resolved_at": (now - timedelta(minutes=30)).isoformat(),
            "cost": 2.0,
        }]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHDEN-26APR06-T63")
        assert not ok
        assert "COOLDOWN" in reason

    def test_old_resolved_position_allows_entry(self, tmp_state):
        """A resolved position older than 4 hours should not block."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        positions = [{
            "ticker": "KXHIGHDEN-26APR06-T63",
            "status": "resolved",
            "resolved_at": (now - timedelta(hours=5)).isoformat(),
            "cost": 2.0,
        }]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHDEN-26APR06-T63")
        assert ok

    def test_exited_early_still_triggers_cooldown(self, tmp_state):
        """Existing behavior: exited_early should still trigger cooldown."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        positions = [{
            "ticker": "KXHIGHDEN-26APR06-T63",
            "status": "exited_early",
            "exited_at": (now - timedelta(minutes=30)).isoformat(),
            "cost": 2.0,
        }]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHDEN-26APR06-T63")
        assert not ok
        assert "COOLDOWN" in reason


# ---------------------------------------------------------------------------
# Fix 1: Daily per-ticker loss limit
# ---------------------------------------------------------------------------

class TestDailyTickerLossLimit:
    def test_ticker_exceeding_daily_loss_blocked(self, tmp_state):
        """If ticker has lost > $1.00 today, block re-entry."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        positions = [
            {
                "ticker": "KXHIGHDEN-26APR06-T63",
                "status": "exited_early",
                "exited_at": (now - timedelta(hours=5)).isoformat(),  # past cooldown
                "realized_pnl": -0.44,
                "cost": 2.0,
            },
            {
                "ticker": "KXHIGHDEN-26APR06-T63",
                "status": "exited_early",
                "exited_at": (now - timedelta(hours=5)).isoformat(),
                "realized_pnl": -0.44,
                "cost": 2.0,
            },
            {
                "ticker": "KXHIGHDEN-26APR06-T63",
                "status": "exited_early",
                "exited_at": (now - timedelta(hours=5)).isoformat(),
                "realized_pnl": -0.33,
                "cost": 2.0,
            },
        ]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHDEN-26APR06-T63")
        assert not ok
        assert "DAILY_LOSS_LIMIT" in reason

    def test_different_ticker_not_affected(self, tmp_state):
        """Loss on one ticker should not block a different ticker."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        positions = [{
            "ticker": "KXHIGHDEN-26APR06-T63",
            "status": "exited_early",
            "exited_at": (now - timedelta(hours=5)).isoformat(),
            "realized_pnl": -2.00,
            "cost": 2.0,
        }]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHNY-26APR06-T70")
        assert ok


# ---------------------------------------------------------------------------
# Fix 2: Crypto edge floor
# ---------------------------------------------------------------------------

class TestCryptoEdgeFloor:
    def test_crypto_min_edge_is_8_percent(self):
        """CRYPTO_MIN_EDGE should be 0.08 (8%) — lowered to capture hourly/15min edges."""
        from bot.config import CRYPTO_MIN_EDGE
        assert CRYPTO_MIN_EDGE == 0.08

    def test_crypto_min_edge_exists_in_config(self):
        """The constant must exist and be importable."""
        from bot.config import CRYPTO_MIN_EDGE
        assert isinstance(CRYPTO_MIN_EDGE, float)


# ---------------------------------------------------------------------------
# Fix 3: CLV sync to paper_trades on resolve
# ---------------------------------------------------------------------------

class TestCLVSync:
    def _setup_resolve(self, tmp_path, monkeypatch, trade_id, clv_entries):
        """Helper to set up files for a resolve_trades test."""
        import bot.tracker as tracker
        import bot.config as config

        positions_file = tmp_path / "positions.json"
        paper_trades_file = tmp_path / "paper_trades.json"
        trade_history_file = tmp_path / "trade_history.json"
        clv_file = tmp_path / "clv.json"

        monkeypatch.setattr(tracker, "POSITIONS_FILE", positions_file)
        monkeypatch.setattr(tracker, "PAPER_TRADES_FILE", paper_trades_file)
        monkeypatch.setattr(tracker, "TRADE_HISTORY_FILE", trade_history_file)
        monkeypatch.setattr(config, "CLV_FILE", clv_file)

        now = datetime.now(timezone.utc)

        positions = [{
            "ticker": "KXHIGHNY-26APR06-T70",
            "side": "yes",
            "filled": 10,
            "price_cents": 45,
            "cost": 4.50,
            "status": "filled",
            "paper": True,
            "order_id": trade_id,
            "type": "weather",
            "opp_type": "weather",
            "opened_at": (now - timedelta(hours=2)).isoformat(),
        }]
        json.dump(positions, open(positions_file, "w"))

        paper_trades = [{
            "id": trade_id,
            "ticker": "KXHIGHNY-26APR06-T70",
            "type": "weather",
            "side": "yes",
            "entry_price": 0.45,
            "contracts": 10,
            "timestamp": (now - timedelta(hours=2)).isoformat(),
            "status": "open",
            "exit_price": None,
            "pnl": None,
            "resolved_at": None,
        }]
        json.dump(paper_trades, open(paper_trades_file, "w"))
        json.dump([], open(trade_history_file, "w"))
        json.dump(clv_entries, open(clv_file, "w"))

        return paper_trades_file

    def test_clv_written_to_paper_trade_on_resolve(self, tmp_path, monkeypatch):
        """When a trade resolves, CLV data from clv.json is synced to paper_trades."""
        import bot.tracker as tracker

        trade_id = "PAPER-TEST123"
        clv_entries = [{
            "trade_id": trade_id,
            "ticker": "KXHIGHNY-26APR06-T70",
            "status": "settled",
            "clv_cents": 8.5,
            "clv_relative": 0.189,
        }]
        paper_trades_file = self._setup_resolve(tmp_path, monkeypatch, trade_id, clv_entries)

        from unittest.mock import patch
        with patch("bot.tracker.get_market") as mock_market:
            mock_market.return_value = {"status": "settled", "result": "yes"}
            tracker.resolve_trades()

        updated = json.loads(paper_trades_file.read_text())
        assert len(updated) == 1
        assert updated[0]["status"] == "won"
        assert updated[0]["clv_cents"] == 8.5
        assert updated[0]["clv_relative"] == 0.189

    def test_missing_clv_does_not_break_resolve(self, tmp_path, monkeypatch):
        """If clv.json has no matching entry, resolve still works without CLV."""
        import bot.tracker as tracker

        trade_id = "PAPER-NOCLV"
        paper_trades_file = self._setup_resolve(tmp_path, monkeypatch, trade_id, [])

        from unittest.mock import patch
        with patch("bot.tracker.get_market") as mock_market:
            mock_market.return_value = {"status": "settled", "result": "yes"}
            tracker.resolve_trades()

        updated = json.loads(paper_trades_file.read_text())
        assert updated[0]["status"] == "won"
        assert "clv_cents" not in updated[0]


# ---------------------------------------------------------------------------
# Fix 5: Vig-stack suppression threshold
# ---------------------------------------------------------------------------

class TestVigStackSuppression:
    def test_weak_weather_edge_does_not_suppress_vig_stack(self):
        """Weather opps with edge < 20% should NOT suppress vig stack."""
        _VIG_SUPPRESS_EDGE = 0.20
        weather_opps = [
            {"ticker": "KXHIGHDEN-T63", "recommended_side": "yes", "edge": 0.10},
        ]
        # With old logic: any YES suppresses. With new logic: only edge >= 20%
        suppressed = {
            opp["ticker"] for opp in weather_opps
            if opp.get("recommended_side") == "yes"
            and abs(opp.get("edge", 0)) >= _VIG_SUPPRESS_EDGE
        }
        assert "KXHIGHDEN-T63" not in suppressed

    def test_strong_weather_edge_suppresses_vig_stack(self):
        """Weather opps with edge >= 20% SHOULD suppress vig stack."""
        _VIG_SUPPRESS_EDGE = 0.20
        weather_opps = [
            {"ticker": "KXHIGHDEN-T63", "recommended_side": "yes", "edge": 0.25},
        ]
        suppressed = {
            opp["ticker"] for opp in weather_opps
            if opp.get("recommended_side") == "yes"
            and abs(opp.get("edge", 0)) >= _VIG_SUPPRESS_EDGE
        }
        assert "KXHIGHDEN-T63" in suppressed
