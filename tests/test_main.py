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
# Session 39 — snapshot_universe must run via run_in_executor
# ---------------------------------------------------------------------------


def test_main_loop_runs_snapshot_universe_via_executor(tmp_path, monkeypatch):
    """Session 39: _main_loop must dispatch _universe.snapshot_universe through
    loop.run_in_executor so the synchronous Kalshi cursor walk doesn't block
    the asyncio event loop.

    Verified by recording the thread on which snapshot_universe runs. If the
    fix is in place, snapshot_universe runs on a ThreadPoolExecutor worker
    thread (name != 'MainThread'). If the fix regresses (direct sync call),
    it runs on the main thread and starves _heartbeat_loop / _live_scan_loop /
    _position_check_loop / Telegram polling — the Apr 30 wedge symptom.
    """
    import threading
    from bot import main as botmain

    # Build a minimal bot. Use __new__ to skip __init__ since we're not
    # exercising real Telegram / Kalshi paths.
    class _PausableNotifier:
        paused = False

        async def send_message(self, *_, **__):
            pass

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot._running = True
    bot.notifier = _PausableNotifier()

    # Capture the thread snapshot_universe runs on, plus mock out side effects.
    snapshot_threads: list[str] = []

    def fake_snapshot(scan_id):
        snapshot_threads.append(threading.current_thread().name)
        return 0

    # Stub the universe module's snapshot/buffer/flush surface.
    monkeypatch.setattr(botmain._universe, "snapshot_universe", fake_snapshot)
    monkeypatch.setattr(botmain._universe, "get_buffered_markets", lambda _scan_id: [])
    monkeypatch.setattr(botmain._universe, "flush_universe", lambda _scan_id: 0)
    monkeypatch.setattr(botmain._universe, "on_market_seen", lambda *a, **kw: None)

    # Force scan_cycle to raise so we hit the scan_failed → 60s sleep path,
    # giving us a clean exit point that doesn't drag in fills / opportunities /
    # outcome tracker etc.
    def boom_scan(*_a, **_kw):
        raise RuntimeError("test stub: short-circuit to scan_failed path")

    monkeypatch.setattr(botmain, "scan_cycle", boom_scan)

    # Stub bot-state I/O + lock touch.
    monkeypatch.setattr(botmain, "_load_bot_state", lambda: {})
    monkeypatch.setattr(botmain, "_save_bot_state", lambda _state: None)

    class _NoopLock:
        def touch(self):
            pass

    monkeypatch.setattr(botmain, "LOCK_FILE", _NoopLock())

    # Async no-op for scheduled events.
    async def fake_scheduled(_self):
        return None

    monkeypatch.setattr(botmain, "check_scheduled_events", fake_scheduled)

    # Patch sleep: the 60s scan_failed sleep is our exit hook. Flip _running
    # so the next loop iteration bails out at the `while self._running` gate.
    sleeps: list[float] = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        bot._running = False

    monkeypatch.setattr(botmain.asyncio, "sleep", fake_sleep)

    asyncio.run(bot._main_loop())

    assert len(snapshot_threads) == 1, (
        f"snapshot_universe should be invoked exactly once per loop iteration; "
        f"got {len(snapshot_threads)} calls"
    )
    assert snapshot_threads[0] != "MainThread", (
        f"snapshot_universe ran on {snapshot_threads[0]!r} — expected an executor "
        f"worker thread. The Session 39 run_in_executor wrap may have regressed; "
        f"this would re-introduce the Apr 30 event-loop wedge."
    )


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


def test_vig_stack_sizing_types_include_futures_without_exit_exemption():
    """Session 62: futures need vig_stack sizing/caps, but not TP/SL exemption."""
    from bot.main import _VIG_STACK_OPP_TYPES, _VIG_STACK_SIZING_TYPES

    assert "vig_stack_series" in _VIG_STACK_SIZING_TYPES
    assert "vig_stack_no" in _VIG_STACK_SIZING_TYPES
    assert "vig_stack_futures" in _VIG_STACK_SIZING_TYPES
    assert "vig_stack_futures" not in _VIG_STACK_OPP_TYPES
    assert "live_momentum" not in _VIG_STACK_SIZING_TYPES


def test_handle_opportunity_vig_stack_futures_uses_family_and_no_probability(monkeypatch):
    """D6 regression: misclassified KXMLBGAME futures still pass family cap data."""
    from bot import main as botmain

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    seen = {}

    def fake_kelly_size(**kwargs):
        seen.update(kwargs)
        return {
            "contracts": 10,
            "price_cents": kwargs["price_cents"],
            "total_cost": 3.90,
            "reason": "sized",
        }

    def fake_execute_trade(_opp, sizing):
        return {"success": True, "order_result": {"count": sizing["contracts"]}}

    monkeypatch.setattr(botmain._outcome_tracker, "store_alert", lambda _opp: None)
    monkeypatch.setattr(botmain, "PAPER_MODE", True)
    monkeypatch.setattr(botmain, "kelly_size", fake_kelly_size)
    monkeypatch.setattr(botmain, "execute_trade", fake_execute_trade)
    monkeypatch.setattr(botmain, "format_opportunity", lambda _opp: "formatted")

    opp = {
        "ticker": "KXMLBGAME-26MAY082210ATLLAD-LAD",
        "type": "vig_stack_futures",
        "recommended_side": "no",
        "edge": 0.0412,
        "relative_edge": 0.1056,
        "confidence": 0.80,
        "market": {"yes_ask": 61, "no_ask": 39},
        "edge_result": {"fair_value": 0.4312},
    }

    asyncio.run(bot._handle_opportunity(opp, balance=10_500.0))

    assert seen["family"] == "KXMLBGAME"
    assert seen["probability"] == 0.4312
    assert seen["price_cents"] == 39


def test_vig_stack_futures_per_game_mlb_respects_family_cap(monkeypatch):
    """Session 62: KXMLBGAME per-game MLB tagged as futures still sizes <= $50."""
    from bot import main as botmain

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    executed = {}

    def fake_execute_trade(_opp, sizing):
        executed.update(sizing)
        return {"success": True, "order_result": {"count": sizing["contracts"]}}

    monkeypatch.setattr(botmain._outcome_tracker, "store_alert", lambda _opp: None)
    monkeypatch.setattr(botmain, "PAPER_MODE", True)
    monkeypatch.setattr(botmain, "execute_trade", fake_execute_trade)
    monkeypatch.setattr(botmain, "format_opportunity", lambda _opp: "formatted")

    opp = {
        "ticker": "KXMLBGAME-26MAY082210ATLLAD-LAD",
        "type": "vig_stack_futures",
        "recommended_side": "no",
        "edge": 0.10,
        "relative_edge": 0.20,
        "confidence": 0.80,
        "market": {"yes_ask": 50, "no_ask": 50},
        "edge_result": {
            "fair_value": 0.78,
            "kalshi_price": 0.50,
            "self_check_passed": True,
        },
    }

    asyncio.run(bot._handle_opportunity(opp, balance=10_000.0))

    assert executed["reason"] == "sized"
    assert executed["total_cost"] <= 50.0


# ---------------------------------------------------------------------------
# Battle Scar #3 follow-up — _release_lock PID-aware (May 3, 2026 incident)
# ---------------------------------------------------------------------------

def test_release_lock_does_not_unlink_when_owned_by_other_pid(tmp_path, monkeypatch):
    """If the lockfile contains a different PID (i.e., another process has
    acquired the lock since we wrote ours), _release_lock must NOT unlink.

    Without this guard, the May 3 race produced an empty lockfile: an old
    orphan process received SIGTERM, its handler called _release_lock which
    unconditionally unlinked the file, and the new process's periodic
    LOCK_FILE.touch() recreated it as empty.
    """
    from bot import main as botmain
    import os

    lock = tmp_path / "bot.lock"
    monkeypatch.setattr(botmain, "LOCK_FILE", lock)

    other_pid = os.getpid() + 99999  # almost certainly not us
    lock.write_text(str(other_pid))

    botmain._release_lock()

    # File must still exist and still contain the other PID
    assert lock.exists(), "lock was unlinked despite being owned by another PID"
    assert lock.read_text().strip() == str(other_pid)


def test_release_lock_unlinks_when_owned_by_us(tmp_path, monkeypatch):
    """When the lockfile contains OUR PID, _release_lock unlinks it."""
    from bot import main as botmain
    import os

    lock = tmp_path / "bot.lock"
    monkeypatch.setattr(botmain, "LOCK_FILE", lock)

    lock.write_text(str(os.getpid()))

    botmain._release_lock()

    assert not lock.exists(), "lock was not unlinked despite containing our PID"


def test_release_lock_handles_missing_lockfile(tmp_path, monkeypatch):
    """_release_lock is a no-op when the lockfile doesn't exist."""
    from bot import main as botmain

    lock = tmp_path / "bot.lock"
    monkeypatch.setattr(botmain, "LOCK_FILE", lock)

    # No file written. Should not raise.
    botmain._release_lock()
    assert not lock.exists()


def test_release_lock_handles_corrupt_lockfile(tmp_path, monkeypatch):
    """_release_lock leaves a corrupt/empty lockfile alone (never unlinks
    something we can't verify ownership of). The acquire path on next start
    will overwrite it.
    """
    from bot import main as botmain

    lock = tmp_path / "bot.lock"
    monkeypatch.setattr(botmain, "LOCK_FILE", lock)

    lock.write_text("not-a-pid\n")

    botmain._release_lock()

    # File preserved (acquire path will handle on next bot start)
    assert lock.exists()
    assert lock.read_text() == "not-a-pid\n"


def test_release_lock_handles_empty_lockfile(tmp_path, monkeypatch):
    """An empty lockfile (the exact symptom of the May 3 race) is left alone."""
    from bot import main as botmain

    lock = tmp_path / "bot.lock"
    monkeypatch.setattr(botmain, "LOCK_FILE", lock)

    lock.write_text("")

    botmain._release_lock()

    assert lock.exists()
    assert lock.read_text() == ""


# ---------------------------------------------------------------------------
# Session 58 — GlintBot.stop() must cancel _active_watchers before notifier
# teardown. Without this, watchers tick on a dead HTTPXRequest for 16+ min
# until process GC. See CLAUDE.md Session 58 for diagnosis.
# ---------------------------------------------------------------------------


def _build_stoppable_bot(monkeypatch, tmp_path, watchers):
    """Construct a partially-initialized GlintBot with a notifier and a
    populated _active_watchers dict for stop()-path testing."""
    from bot import main as botmain

    monkeypatch.setattr(botmain, "LOCK_FILE", tmp_path / "bot.lock")
    monkeypatch.setattr(botmain, "_load_bot_state", lambda: {})
    monkeypatch.setattr(botmain, "_save_bot_state", lambda _state: None)

    notifier_calls: list[str] = []

    class _StubNotifier:
        async def stop(self):
            notifier_calls.append("notifier.stop")

    bot = botmain.GlintBot.__new__(botmain.GlintBot)
    bot._running = True
    bot.notifier = _StubNotifier()
    bot._active_watchers = dict(watchers)
    return bot, notifier_calls


class _StubWatcher:
    def __init__(self, raise_on_stop: bool = False):
        self.stop_called = False
        self.raise_on_stop = raise_on_stop

    def stop(self):
        self.stop_called = True
        if self.raise_on_stop:
            raise RuntimeError("simulated watcher stop failure")


def _make_done_task() -> asyncio.Task:
    """Create a coroutine task that completes immediately on cancel."""
    async def _coro():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return
    loop = asyncio.get_event_loop()
    return loop.create_task(_coro())


def test_stop_cancels_active_watchers_and_clears_dict(tmp_path, monkeypatch):
    """Session 58: GlintBot.stop() must call watcher.stop() AND task.cancel()
    on every entry in _active_watchers, then clear the dict."""

    async def _drive():
        watcher_a = _StubWatcher()
        watcher_b = _StubWatcher()
        task_a = _make_done_task()
        task_b = _make_done_task()
        watchers = {"a": (watcher_a, task_a), "b": (watcher_b, task_b)}
        bot, notifier_calls = _build_stoppable_bot(monkeypatch, tmp_path, watchers)

        await bot.stop()

        assert watcher_a.stop_called, "watcher_a.stop() not invoked"
        assert watcher_b.stop_called, "watcher_b.stop() not invoked"
        assert task_a.cancelled() or task_a.done()
        assert task_b.cancelled() or task_b.done()
        assert bot._active_watchers == {}, "_active_watchers must be cleared"
        assert notifier_calls == ["notifier.stop"], "notifier.stop must run after watcher cancellation"

    asyncio.run(_drive())


def test_stop_cancels_watchers_BEFORE_notifier_shutdown(tmp_path, monkeypatch):
    """Session 58: ordering invariant — watchers must be cancelled before
    notifier.stop() runs. If notifier shuts down first, an in-flight watcher
    tick fires the same HTTPXRequest error we're trying to eliminate."""

    async def _drive():
        order: list[str] = []

        class _OrderedWatcher:
            def stop(self):
                order.append("watcher.stop")

        async def _ordered_task():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                order.append("task.cancelled")
                raise

        loop = asyncio.get_event_loop()
        watcher = _OrderedWatcher()
        task = loop.create_task(_ordered_task())
        bot, _ = _build_stoppable_bot(monkeypatch, tmp_path, {"q": (watcher, task)})

        # Replace notifier with one that records its own ordering position.
        class _OrderedNotifier:
            async def stop(self):
                order.append("notifier.stop")
        bot.notifier = _OrderedNotifier()

        await bot.stop()

        assert "watcher.stop" in order
        assert "notifier.stop" in order
        assert order.index("watcher.stop") < order.index("notifier.stop"), (
            f"watcher.stop must come BEFORE notifier.stop; got order={order}"
        )

    asyncio.run(_drive())


def test_stop_with_no_active_watchers_skips_cancellation(tmp_path, monkeypatch):
    """Session 58: empty _active_watchers should not log spam or break the
    stop() flow. Backwards compatibility with paths that never spawned a
    watcher (e.g. early-startup crash before _live_scan_loop fires)."""

    async def _drive():
        bot, notifier_calls = _build_stoppable_bot(monkeypatch, tmp_path, {})

        await bot.stop()

        assert notifier_calls == ["notifier.stop"], "notifier still must run"
        assert bot._active_watchers == {}

    asyncio.run(_drive())


def test_stop_handles_watcher_stop_exception(tmp_path, monkeypatch):
    """Session 58: if one watcher's .stop() raises, others still get
    cancelled and notifier.stop is still called. Mirrors handle_unwatch's
    best-effort discipline."""

    async def _drive():
        bad_watcher = _StubWatcher(raise_on_stop=True)
        good_watcher = _StubWatcher()
        bad_task = _make_done_task()
        good_task = _make_done_task()
        bot, notifier_calls = _build_stoppable_bot(
            monkeypatch, tmp_path,
            {"bad": (bad_watcher, bad_task), "good": (good_watcher, good_task)},
        )

        await bot.stop()  # must not raise

        assert bad_watcher.stop_called
        assert good_watcher.stop_called
        assert bad_task.cancelled() or bad_task.done()
        assert good_task.cancelled() or good_task.done()
        assert notifier_calls == ["notifier.stop"]
        assert bot._active_watchers == {}

    asyncio.run(_drive())


def test_stop_bounded_by_5s_timeout_when_task_doesnt_unwind(tmp_path, monkeypatch):
    """Session 58: a stuck watcher task must not block process shutdown.
    The asyncio.wait_for timeout forces forward progress to notifier.stop()."""

    async def _drive():
        watcher = _StubWatcher()

        async def _stuck_coro():
            # Simulate a task that swallows CancelledError indefinitely.
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass  # malicious — refuse to unwind

        loop = asyncio.get_event_loop()
        stuck_task = loop.create_task(_stuck_coro())
        bot, notifier_calls = _build_stoppable_bot(
            monkeypatch, tmp_path, {"q": (watcher, stuck_task)}
        )

        # Patch wait_for timeout to 0.05s so the test runs fast.
        original_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, timeout):
            return await original_wait_for(coro, timeout=0.05)

        monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

        await bot.stop()  # must complete despite the stuck task

        assert notifier_calls == ["notifier.stop"], (
            "notifier.stop must run even when watcher cancellation times out"
        )
        # Cancel the stuck task at the end so the test doesn't leak.
        stuck_task.cancel()
        try:
            await stuck_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_drive())
