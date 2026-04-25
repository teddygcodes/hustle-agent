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
