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


def test_position_check_loop_calls_update_positions_each_cycle(monkeypatch):
    """Session 17: _position_check_loop calls update_positions(called_from=
    '_position_check_loop') once per 30s cycle, independent of scan_interval.
    Patch sleep → instant to exercise N cycles in <100ms."""
    from bot import main as botmain

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot._running = True

    calls: list[str] = []

    def fake_update_positions(called_from: str = "unspecified"):
        calls.append(called_from)
        return []

    monkeypatch.setattr(botmain, "update_positions", fake_update_positions)

    sleeps: list[float] = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        # First call (line `await asyncio.sleep(20)` startup gate) doesn't
        # increment work counter. Stop after the loop has done 3 work cycles.
        if len([s for s in sleeps if s == 30]) >= 3:
            bot._running = False

    monkeypatch.setattr(botmain.asyncio, "sleep", fake_sleep)
    asyncio.run(bot._position_check_loop())

    # 3 work cycles → 3 update_positions calls, each with called_from set.
    assert len(calls) == 3, f"expected 3 update_positions calls, got {len(calls)}"
    assert all(c == "_position_check_loop" for c in calls), \
        f"all calls must thread called_from; got {calls}"

    # Cadence: 1 startup sleep at 20s, then 30s between cycles.
    assert sleeps[0] == 20, "first sleep must be the 20s init delay"
    work_sleeps = [s for s in sleeps[1:] if s == 30]
    assert len(work_sleeps) == 3, f"expected 3 cycle sleeps at 30s, got {sleeps}"


def test_position_check_loop_swallows_update_errors(monkeypatch):
    """A failing update_positions must not crash the position-check task —
    the loop ratchets MFE/MAE on a best-effort basis. One transient market-
    fetch hiccup should not kill the observation loop for the rest of the
    session."""
    from bot import main as botmain

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot._running = True

    def boom(called_from: str = "unspecified"):
        raise RuntimeError("simulated tracker error")

    monkeypatch.setattr(botmain, "update_positions", boom)

    cycles = {"n": 0}

    async def fake_sleep(_secs):
        cycles["n"] += 1
        if cycles["n"] >= 3:
            bot._running = False

    monkeypatch.setattr(botmain.asyncio, "sleep", fake_sleep)
    asyncio.run(bot._position_check_loop())  # must not raise
    assert cycles["n"] == 3


# ---------------------------------------------------------------------------
# Session 36 — vig_stack auto-exit exemption
# ---------------------------------------------------------------------------


class _StubNotifier:
    """Captures send_message calls so tests can assert on Telegram side effects."""

    def __init__(self):
        self.messages = []

    async def send_message(self, text, priority="normal"):
        self.messages.append({"text": text, "priority": priority})


def _make_bot(monkeypatch, paper_mode=True):
    """Build a minimal GlintBot for _dispatch_position_alerts tests."""
    from bot import main as botmain

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot.notifier = _StubNotifier()
    monkeypatch.setattr(botmain, "PAPER_MODE", paper_mode)
    return bot, botmain


def test_dispatch_take_profit_skips_vig_stack_series(monkeypatch):
    """Session 36: vig_stack_series take_profit alert must NOT call exit_position."""
    bot, botmain = _make_bot(monkeypatch)
    calls = []

    def fake_exit(ticker, reason="manual"):
        calls.append((ticker, reason))
        return {"success": True, "realized_pnl": 1.23}

    monkeypatch.setattr(botmain, "exit_position", fake_exit)

    alerts = [{
        "type": "take_profit",
        "ticker": "KXHIGHDEN-26APR29-T95",
        "opp_type": "vig_stack_series",
        "pnl_percent": 0.55,
        "unrealized_pnl": 5.0,
    }]
    asyncio.run(bot._dispatch_position_alerts(alerts))

    assert calls == [], "vig_stack_series TP must not trigger exit_position"
    assert bot.notifier.messages == [], "no Telegram message on skipped TP"


def test_dispatch_take_profit_skips_vig_stack_no(monkeypatch):
    """Session 36: vig_stack_no take_profit alert must also be exempt."""
    bot, botmain = _make_bot(monkeypatch)
    calls = []
    monkeypatch.setattr(botmain, "exit_position", lambda *a, **k: calls.append(a) or {"success": True})

    alerts = [{
        "type": "take_profit",
        "ticker": "KXSOMETHING-NO",
        "opp_type": "vig_stack_no",
        "pnl_percent": 0.60,
        "unrealized_pnl": 7.0,
    }]
    asyncio.run(bot._dispatch_position_alerts(alerts))

    assert calls == []


def test_dispatch_cut_loss_skips_vig_stack(monkeypatch):
    """Session 36: vig_stack cut_loss alert must NOT call exit_position."""
    bot, botmain = _make_bot(monkeypatch)
    calls = []

    def fake_exit(ticker, reason="manual"):
        calls.append((ticker, reason))
        return {"success": True, "realized_pnl": -3.0}

    monkeypatch.setattr(botmain, "exit_position", fake_exit)

    alerts = [{
        "type": "cut_loss",
        "ticker": "KXHIGHCHI-26APR29-T75",
        "opp_type": "vig_stack_series",
        "pnl_percent": -0.32,
        "unrealized_pnl": -3.0,
    }]
    asyncio.run(bot._dispatch_position_alerts(alerts))

    assert calls == [], "vig_stack SL must not trigger exit_position"
    assert bot.notifier.messages == [], "no Telegram message on skipped SL"


def test_dispatch_take_profit_fires_for_non_vig_stack(monkeypatch):
    """Regression: live_momentum (and any non-vig_stack) TP still triggers exit."""
    bot, botmain = _make_bot(monkeypatch, paper_mode=True)
    calls = []

    def fake_exit(ticker, reason="manual"):
        calls.append((ticker, reason))
        return {"success": True, "realized_pnl": 12.50}

    monkeypatch.setattr(botmain, "exit_position", fake_exit)

    alerts = [{
        "type": "take_profit",
        "ticker": "KXNBAGAME-26APR29-LAL",
        "opp_type": "live_momentum",
        "pnl_percent": 0.55,
        "unrealized_pnl": 12.50,
    }]
    asyncio.run(bot._dispatch_position_alerts(alerts))

    assert calls == [("KXNBAGAME-26APR29-LAL", "auto_take_profit")]
    assert len(bot.notifier.messages) == 1
    assert "AUTO TAKE PROFIT" in bot.notifier.messages[0]["text"]


def test_dispatch_cut_loss_fires_for_non_vig_stack(monkeypatch):
    """Regression: non-vig_stack cut_loss still triggers exit_position."""
    bot, botmain = _make_bot(monkeypatch)
    calls = []

    def fake_exit(ticker, reason="manual"):
        calls.append((ticker, reason))
        return {"success": True, "realized_pnl": -4.10}

    monkeypatch.setattr(botmain, "exit_position", fake_exit)

    alerts = [{
        "type": "cut_loss",
        "ticker": "KXNBAGAME-26APR29-BOS",
        "opp_type": "live_momentum",
        "pnl_percent": -0.35,
        "unrealized_pnl": -4.10,
    }]
    asyncio.run(bot._dispatch_position_alerts(alerts))

    assert calls == [("KXNBAGAME-26APR29-BOS", "auto_cut_loss")]
    assert len(bot.notifier.messages) == 1
    assert "AUTO CUT" in bot.notifier.messages[0]["text"]


def test_dispatch_missing_opp_type_falls_through_to_exit(monkeypatch):
    """Defensive: alert lacking opp_type (legacy or external) is treated as
    non-vig_stack and triggers exit. Empty string is NOT in _VIG_STACK_OPP_TYPES."""
    bot, botmain = _make_bot(monkeypatch)
    calls = []
    monkeypatch.setattr(
        botmain, "exit_position",
        lambda ticker, reason="manual": (calls.append((ticker, reason)) or {"success": True, "realized_pnl": 1.0}),
    )

    alerts = [{
        "type": "take_profit",
        "ticker": "KXSOMETHING",
        # no opp_type key
        "pnl_percent": 0.50,
        "unrealized_pnl": 1.0,
    }]
    asyncio.run(bot._dispatch_position_alerts(alerts))

    assert calls == [("KXSOMETHING", "auto_take_profit")]


def test_vig_stack_opp_types_constant():
    """Lock the membership list — these are the strings the alert dict carries
    for the two vig_stack scanners, copied from bot/scanner_*."""
    from bot.main import _VIG_STACK_OPP_TYPES

    assert "vig_stack_series" in _VIG_STACK_OPP_TYPES
    assert "vig_stack_no" in _VIG_STACK_OPP_TYPES
    # Things that are NOT exempt:
    assert "live_momentum" not in _VIG_STACK_OPP_TYPES
    assert "vig_stack_futures" not in _VIG_STACK_OPP_TYPES  # not currently exempt; intentional
    assert "" not in _VIG_STACK_OPP_TYPES
