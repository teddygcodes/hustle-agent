"""Tests for bot/scheduler.py — hour-gate logic for morning / nightly / reconcile.

Session 4 (Apr 20 redemption plan): gate changes from `==` to `>=` with
same-day fire-once guarantee + catch-up semantics. These tests pin both the
happy path (exact hour) and the catch-up path (bot restarted mid-day).
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import scheduler  # noqa: E402

ET = ZoneInfo("America/New_York")


def _freeze_datetime(monkeypatch, fake_now):
    """Patch scheduler.datetime so datetime.now(ET) returns fake_now."""
    from datetime import datetime as real_dt

    class _FrozenDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    monkeypatch.setattr(scheduler, "datetime", _FrozenDT)


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    state_file = tmp_path / "bot_state.json"
    monkeypatch.setattr(scheduler, "BOT_STATE_FILE", state_file)
    return state_file


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.notifier.send_message = AsyncMock()
    bot.notifier.send_photo = AsyncMock()
    return bot


def _set_state(state_file, **fields):
    state_file.write_text(json.dumps(fields))


def _read_state(state_file):
    return json.loads(state_file.read_text())


def _install_body_mocks(monkeypatch, *, morning=True, nightly=True, reconcile=True):
    """Stub the briefing bodies so tests don't import scanner/patterns/kalshi."""
    calls = {"morning": 0, "nightly": 0, "reconcile": 0}

    if morning:
        async def fake_morning(bot):
            calls["morning"] += 1
        monkeypatch.setattr(scheduler, "_send_morning_briefing", fake_morning)
    if nightly:
        async def fake_nightly(bot):
            calls["nightly"] += 1
        monkeypatch.setattr(scheduler, "_send_nightly_summary", fake_nightly)
    if reconcile:
        async def fake_reconcile(bot):
            calls["reconcile"] += 1
        monkeypatch.setattr(scheduler, "_reconcile_daily_balance", fake_reconcile)

    return calls


class TestMorningBriefing:

    def test_fires_at_8am_if_not_yet_today(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 8, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["morning"] == 1
        assert _read_state(tmp_state)["last_morning_briefing"] == "2026-04-24"

    def test_fires_at_9am_catchup_if_not_yet_today(self, tmp_state, mock_bot, monkeypatch):
        """The `>=` gate catches cases where bot missed the 8am minute."""
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 9, 30, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["morning"] == 1
        assert _read_state(tmp_state)["last_morning_briefing"] == "2026-04-24"

    def test_does_not_fire_if_already_sent_today(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 10, 0, tzinfo=ET))
        _set_state(tmp_state, last_morning_briefing="2026-04-24")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["morning"] == 0

    def test_does_not_fire_before_morning_hour(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 5, 0, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["morning"] == 0

    def test_does_not_fire_after_cutoff_hour(self, tmp_state, mock_bot, monkeypatch):
        """Late-night restart must not fire the 'morning' briefing at 11pm."""
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 23, 0, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["morning"] == 0

    def test_fires_next_day_after_yesterday_entry(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 8, 5, tzinfo=ET))
        _set_state(tmp_state, last_morning_briefing="2026-04-23")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["morning"] == 1
        assert _read_state(tmp_state)["last_morning_briefing"] == "2026-04-24"


class TestNightlySummary:

    def test_fires_at_midnight_et(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        # Morning gate is hour>=8, so morning must NOT fire at 0:05
        assert calls["morning"] == 0
        assert calls["nightly"] == 1
        assert _read_state(tmp_state)["last_nightly_summary"] == "2026-04-24"

    def test_catch_up_if_missed_a_day(self, tmp_state, mock_bot, monkeypatch):
        """If last_nightly_summary is older than yesterday, fire at any hour."""
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 15, 0, tzinfo=ET))
        _set_state(tmp_state, last_nightly_summary="2026-04-19")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["nightly"] == 1
        assert _read_state(tmp_state)["last_nightly_summary"] == "2026-04-24"

    def test_no_catch_up_if_fired_yesterday(self, tmp_state, mock_bot, monkeypatch):
        """last_nightly_summary == yesterday is still current; mid-afternoon must not re-fire."""
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 15, 0, tzinfo=ET))
        _set_state(tmp_state, last_nightly_summary="2026-04-23")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["nightly"] == 0

    def test_no_fire_if_already_today(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state, last_nightly_summary="2026-04-24")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["nightly"] == 0


class TestBalanceReconcile:

    def test_fires_at_21_et(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 21, 30, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["reconcile"] == 1
        assert _read_state(tmp_state)["last_balance_reconcile_date"] == "2026-04-24"

    def test_does_not_fire_outside_hour_21(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 22, 0, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["reconcile"] == 0

    def test_does_not_refire_same_day(self, tmp_state, mock_bot, monkeypatch):
        calls = _install_body_mocks(monkeypatch)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 21, 30, tzinfo=ET))
        _set_state(tmp_state, last_balance_reconcile_date="2026-04-24")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert calls["reconcile"] == 0


class TestLiveTicksRotation:
    """Session 5: nightly live_ticks.jsonl → state/archive/*.jsonl.gz rotation."""

    @pytest.fixture
    def tmp_ticks(self, tmp_path, monkeypatch):
        """Repoint TICK_LOG_FILE to a tmp directory."""
        ticks = tmp_path / "live_ticks.jsonl"
        from bot import live_watcher
        monkeypatch.setattr(live_watcher, "TICK_LOG_FILE", ticks)
        return ticks

    def _seed_ticks(self, path, count=200):
        # Each tick ≥ 50 bytes → comfortably above the 1KB rotation floor.
        path.write_text("\n".join(
            json.dumps({"ticker": f"T{i}", "price": 50 + i, "ts": "x"})
            for i in range(count)
        ) + "\n")

    def test_fires_at_midnight_and_archives_file(self, tmp_state, tmp_ticks, mock_bot, monkeypatch):
        _install_body_mocks(monkeypatch)
        self._seed_ticks(tmp_ticks)
        original_size = tmp_ticks.stat().st_size

        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        # Original file is gone; archive contains the gzip.
        assert not tmp_ticks.exists()
        archive_dir = tmp_ticks.parent / "archive"
        gz = archive_dir / "live_ticks-2026-04-24.jsonl.gz"
        assert gz.exists()
        assert gz.stat().st_size < original_size  # gzip actually shrunk it

        state = _read_state(tmp_state)
        assert state["last_ticks_rotation"] == "2026-04-24"

    def test_skips_if_file_too_small(self, tmp_state, tmp_ticks, mock_bot, monkeypatch):
        _install_body_mocks(monkeypatch)
        tmp_ticks.write_text('{"x":1}\n')  # 8 bytes, below the 1KB floor
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        # File untouched, but flag still advances (we did "consider" rotation)
        assert tmp_ticks.exists()
        assert (tmp_ticks.parent / "archive").exists() is False
        assert _read_state(tmp_state)["last_ticks_rotation"] == "2026-04-24"

    def test_no_refire_same_day(self, tmp_state, tmp_ticks, mock_bot, monkeypatch):
        _install_body_mocks(monkeypatch)
        self._seed_ticks(tmp_ticks)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 30, tzinfo=ET))
        _set_state(tmp_state, last_ticks_rotation="2026-04-24")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        # File untouched because gate already fired today.
        assert tmp_ticks.exists()
        assert not (tmp_ticks.parent / "archive").exists()

    def test_catch_up_if_missed_a_day(self, tmp_state, tmp_ticks, mock_bot, monkeypatch):
        """If last_ticks_rotation is older than yesterday, rotate at any hour."""
        _install_body_mocks(monkeypatch)
        self._seed_ticks(tmp_ticks)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 14, 0, tzinfo=ET))
        _set_state(tmp_state, last_ticks_rotation="2026-04-19")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        gz = tmp_ticks.parent / "archive" / "live_ticks-2026-04-24.jsonl.gz"
        assert gz.exists()

    def test_collision_appends_suffix(self, tmp_state, tmp_ticks, mock_bot, monkeypatch):
        """If today's archive already exists, dest gets a -2 suffix."""
        _install_body_mocks(monkeypatch)
        self._seed_ticks(tmp_ticks)
        archive_dir = tmp_ticks.parent / "archive"
        archive_dir.mkdir()
        # Pre-existing gzip from an earlier rotation today
        (archive_dir / "live_ticks-2026-04-24.jsonl.gz").write_bytes(b"prior")

        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert (archive_dir / "live_ticks-2026-04-24.jsonl.gz").read_bytes() == b"prior"
        assert (archive_dir / "live_ticks-2026-04-24-2.jsonl.gz").exists()


class TestDecisionsRotation:
    """Session 6: nightly decisions.jsonl → state/archive/*.jsonl.gz rotation."""

    @pytest.fixture
    def tmp_decisions(self, tmp_path, monkeypatch):
        """Repoint DECISIONS_FILE to a tmp directory."""
        dec = tmp_path / "decisions.jsonl"
        from bot import decisions
        monkeypatch.setattr(decisions, "DECISIONS_FILE", dec)
        return dec

    def _seed(self, path, count=200):
        # Each line ≥ 50 bytes → comfortably above the 1KB rotation floor.
        path.write_text("\n".join(
            json.dumps({"ts": "x", "ticker": f"T{i}", "decision": "reject", "reason": "r"})
            for i in range(count)
        ) + "\n")

    def test_fires_at_midnight_and_archives_file(self, tmp_state, tmp_decisions, mock_bot, monkeypatch):
        _install_body_mocks(monkeypatch)
        self._seed(tmp_decisions)
        original_size = tmp_decisions.stat().st_size

        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert not tmp_decisions.exists()
        archive_dir = tmp_decisions.parent / "archive"
        gz = archive_dir / "decisions-2026-04-24.jsonl.gz"
        assert gz.exists()
        assert gz.stat().st_size < original_size

        state = _read_state(tmp_state)
        assert state["last_decisions_rotation"] == "2026-04-24"

    def test_skips_if_file_too_small(self, tmp_state, tmp_decisions, mock_bot, monkeypatch):
        _install_body_mocks(monkeypatch)
        tmp_decisions.write_text('{"x":1}\n')
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert tmp_decisions.exists()
        assert (tmp_decisions.parent / "archive").exists() is False
        assert _read_state(tmp_state)["last_decisions_rotation"] == "2026-04-24"

    def test_no_refire_same_day(self, tmp_state, tmp_decisions, mock_bot, monkeypatch):
        _install_body_mocks(monkeypatch)
        self._seed(tmp_decisions)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 30, tzinfo=ET))
        _set_state(tmp_state, last_decisions_rotation="2026-04-24")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert tmp_decisions.exists()
        assert not (tmp_decisions.parent / "archive").exists()

    def test_catch_up_if_missed_a_day(self, tmp_state, tmp_decisions, mock_bot, monkeypatch):
        """If last_decisions_rotation is older than yesterday, rotate at any hour."""
        _install_body_mocks(monkeypatch)
        self._seed(tmp_decisions)
        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 14, 0, tzinfo=ET))
        _set_state(tmp_state, last_decisions_rotation="2026-04-19")

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        gz = tmp_decisions.parent / "archive" / "decisions-2026-04-24.jsonl.gz"
        assert gz.exists()

    def test_collision_appends_suffix(self, tmp_state, tmp_decisions, mock_bot, monkeypatch):
        """If today's archive already exists, dest gets a -2 suffix."""
        _install_body_mocks(monkeypatch)
        self._seed(tmp_decisions)
        archive_dir = tmp_decisions.parent / "archive"
        archive_dir.mkdir()
        (archive_dir / "decisions-2026-04-24.jsonl.gz").write_bytes(b"prior")

        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        assert (archive_dir / "decisions-2026-04-24.jsonl.gz").read_bytes() == b"prior"
        assert (archive_dir / "decisions-2026-04-24-2.jsonl.gz").exists()


class TestTotalPnlPersist:
    """After nightly fires, bot_state['total_pnl'] reflects compute_daily_summary."""

    def test_total_pnl_written_to_state(self, tmp_state, mock_bot, monkeypatch):
        # Stub morning + reconcile bodies
        _install_body_mocks(monkeypatch, nightly=False)

        # Stub compute_daily_summary used inside _send_nightly_summary
        from bot import tracker
        monkeypatch.setattr(
            tracker, "compute_daily_summary",
            lambda: {
                "balance": 500.0, "today_pnl": -1.25, "total_pnl": -98.32,
                "trades_today": 0, "resolved_today": 0, "win_rate": 0.5,
                "open_positions": 0, "total_trades": 93, "total_wins": 53,
                "best_trade": None, "worst_trade": None,
                "scans_today": 0, "odds_api_used": 152, "odds_api_limit": 450,
                "date": "2026-04-24",
            }
        )
        monkeypatch.setattr(tracker, "get_streak", lambda: {"type": "none", "count": 0})
        monkeypatch.setattr(tracker, "get_roi_by_strategy", lambda: {})

        _freeze_datetime(monkeypatch, datetime(2026, 4, 24, 0, 5, tzinfo=ET))
        _set_state(tmp_state)

        asyncio.run(scheduler.check_scheduled_events(mock_bot))

        state = _read_state(tmp_state)
        assert state["total_pnl"] == -98.32
        assert state["today_pnl"] == -1.25
        assert state["last_nightly_summary"] == "2026-04-24"
