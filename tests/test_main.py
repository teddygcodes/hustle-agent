"""Tests for bot/main.py — Session 7 heartbeat lock-touch."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_heartbeat_loop_touches_lock_each_cycle(tmp_path, monkeypatch):
    """GlintBot._heartbeat_loop calls LOCK_FILE.touch() once per cycle.
    Patch sleep → instant so we exercise N cycles in <100ms."""
    from bot import main as botmain

    lock = tmp_path / "bot.lock"
    monkeypatch.setattr(botmain, "LOCK_FILE", lock)

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot._running = True

    cycles = {"n": 0}

    async def fake_sleep(_secs):
        cycles["n"] += 1
        if cycles["n"] >= 3:
            bot._running = False  # break the loop after 3 iterations

    monkeypatch.setattr(botmain.asyncio, "sleep", fake_sleep)
    asyncio.run(bot._heartbeat_loop())

    assert lock.exists(), "heartbeat must create/touch the lock file"
    assert cycles["n"] == 3, "loop should iterate 3 times before stopping"


def test_heartbeat_loop_swallows_touch_errors(tmp_path, monkeypatch):
    """A failing LOCK_FILE.touch() must not crash the heartbeat task —
    transient FS issues should not take down the bot's liveness signal."""
    from bot import main as botmain

    class _BadPath:
        def touch(self):
            raise OSError("simulated disk error")

    monkeypatch.setattr(botmain, "LOCK_FILE", _BadPath())

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot._running = True

    cycles = {"n": 0}

    async def fake_sleep(_secs):
        cycles["n"] += 1
        if cycles["n"] >= 2:
            bot._running = False

    monkeypatch.setattr(botmain.asyncio, "sleep", fake_sleep)
    # Must not raise.
    asyncio.run(bot._heartbeat_loop())
    assert cycles["n"] == 2


def test_heartbeat_loop_updates_bot_state_last_heartbeat(tmp_path, monkeypatch):
    """Session 15.5: _heartbeat_loop refreshes bot_state.last_heartbeat each
    iteration so liveness checks (Telegram /STATUS, monitoring scripts) see
    fresh timestamps even between scans."""
    from datetime import datetime, timezone

    from bot import main as botmain

    monkeypatch.setattr(botmain, "LOCK_FILE", tmp_path / "bot.lock")

    writes: list[str] = []

    def fake_load():
        return {"last_heartbeat": "2025-01-01T00:00:00+00:00"}

    def fake_save(state):
        writes.append(state.get("last_heartbeat"))

    monkeypatch.setattr(botmain, "_load_bot_state", fake_load)
    monkeypatch.setattr(botmain, "_save_bot_state", fake_save)

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot._running = True

    cycles = {"n": 0}

    async def fake_sleep(_secs):
        cycles["n"] += 1
        if cycles["n"] >= 3:
            bot._running = False

    monkeypatch.setattr(botmain.asyncio, "sleep", fake_sleep)
    asyncio.run(bot._heartbeat_loop())

    assert len(writes) == 3, f"expected 3 saves, got {len(writes)}"
    now = datetime.now(timezone.utc)
    for ts in writes:
        parsed = datetime.fromisoformat(ts)
        delta = (now - parsed).total_seconds()
        assert 0 <= delta < 5, f"timestamp {ts} not fresh (delta={delta}s)"


def test_heartbeat_loop_swallows_state_io_errors(tmp_path, monkeypatch):
    """Session 15.5: a failing _save_bot_state must not crash the heartbeat
    task. Liveness signal must survive transient state-io failures."""
    from bot import main as botmain

    monkeypatch.setattr(botmain, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(botmain, "_load_bot_state", lambda: {})

    def boom(_state):
        raise OSError("simulated state-io error")

    monkeypatch.setattr(botmain, "_save_bot_state", boom)

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot._running = True

    cycles = {"n": 0}

    async def fake_sleep(_secs):
        cycles["n"] += 1
        if cycles["n"] >= 2:
            bot._running = False

    monkeypatch.setattr(botmain.asyncio, "sleep", fake_sleep)
    asyncio.run(bot._heartbeat_loop())  # must not raise
    assert cycles["n"] == 2
