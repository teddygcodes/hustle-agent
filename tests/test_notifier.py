"""Session 52 Telegram notifier hardening tests."""
from __future__ import annotations

import asyncio
import json
import warnings
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from telegram.error import NetworkError, RetryAfter, TelegramError


def _notifier_with_fake_app(monkeypatch, tmp_path, bot):
    from bot import notifier

    state_file = tmp_path / "bot_state.json"
    state_file.write_text(json.dumps({"running": True}))

    monkeypatch.setattr(notifier, "BOT_STATE_FILE", state_file)
    monkeypatch.setattr(notifier, "TELEGRAM_CHAT_ID", "12345")

    n = notifier.TelegramNotifier()
    n.app = SimpleNamespace(bot=bot)
    return n, state_file


def _read_state(path: Path) -> dict:
    return json.loads(path.read_text())


def _retry_after(seconds: int) -> RetryAfter:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return RetryAfter(seconds)


def test_notifier_429_backoff(monkeypatch, tmp_path):
    """429-shaped PTB error waits, retries with a fresh coroutine, and surfaces state."""
    from bot import notifier

    calls: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise _retry_after(2)
            return SimpleNamespace(message_id=42)

    n, state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(notifier.asyncio, "sleep", fake_sleep)

    asyncio.run(n.send_message("hello"))

    state = _read_state(state_file)
    assert len(calls) == 2
    assert sleeps == [2]
    assert state["telegram_throttled_until"] is not None
    assert state["telegram_throttled_count_24h"] == 1
    assert state["telegram_last_send_attempt_at"] is not None
    assert state["telegram_last_send_success_at"] is not None


def test_notifier_429_giving_up_after_max_retries(monkeypatch, tmp_path, caplog):
    """Repeated 429s stop after max retries, log ERROR, and still persist throttle state."""
    from bot import notifier

    calls = {"n": 0}

    class FakeBot:
        async def send_message(self, **kwargs):
            calls["n"] += 1
            raise _retry_after(3)

    n, state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(notifier.asyncio, "sleep", fake_sleep)

    with caplog.at_level("ERROR", logger="glint.notifier"):
        asyncio.run(n.send_message("still blocked"))

    state = _read_state(state_file)
    assert calls["n"] == n._TELEGRAM_MAX_RETRIES + 1
    assert sleeps == [3, 3]
    assert state["telegram_throttled_until"] is not None
    assert state["telegram_throttled_count_24h"] == n._TELEGRAM_MAX_RETRIES + 1
    assert "giving up" in caplog.text


def test_notifier_500_retries_then_succeeds(monkeypatch, tmp_path):
    """Transient PTB network/server errors retry and can still succeed."""
    from bot import notifier

    calls = {"n": 0}

    class FakeBot:
        async def send_message(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise NetworkError("server hiccup")
            return SimpleNamespace(message_id=7)

    n, state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(notifier.asyncio, "sleep", fake_sleep)

    asyncio.run(n.send_message("after retry"))

    state = _read_state(state_file)
    assert calls["n"] == 2
    assert sleeps == [1.0]
    assert state["telegram_last_send_success_at"] is not None


def test_notifier_200_no_retry(monkeypatch, tmp_path):
    """A clean Telegram call succeeds once and does not sleep/retry."""
    from bot import notifier

    calls = {"n": 0}

    class FakeBot:
        async def send_message(self, **kwargs):
            calls["n"] += 1
            return SimpleNamespace(message_id=9)

    n, state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(notifier.asyncio, "sleep", fake_sleep)

    asyncio.run(n.send_message("one shot"))

    state = _read_state(state_file)
    assert calls["n"] == 1
    assert sleeps == []
    assert state["telegram_last_send_attempt_at"] is not None
    assert state["telegram_last_send_success_at"] is not None


def test_edit_throttle_token_bucket_math():
    """EditThrottle permits burst=5, then reserves one edit per second."""
    from bot.notifier import EditThrottle

    now = {"t": 0.0}
    throttle = EditThrottle(rate_per_second=1.0, burst=5, clock=lambda: now["t"])

    assert [throttle.reserve_delay() for _ in range(5)] == [0.0] * 5
    assert throttle.reserve_delay() == 1.0

    now["t"] += 1.0
    assert throttle.reserve_delay() == 1.0

    now["t"] += 5.0
    assert throttle.reserve_delay() == 0.0


def test_edit_dedup_skip_identical_content(monkeypatch, tmp_path):
    """Same message_id + same rendered text is skipped after one successful edit."""
    from bot import notifier

    calls: list[dict] = []

    class FakeBot:
        async def edit_message_text(self, **kwargs):
            calls.append(kwargs)
            return True

    n, _state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())
    n._edit_throttle = notifier.EditThrottle(clock=lambda: 0.0)

    assert asyncio.run(n.edit_message_by_id(100, "same text")) is True
    assert asyncio.run(n.edit_message_by_id(100, "same text")) is True
    assert len(calls) == 1


def test_edit_dedup_records_only_on_success(monkeypatch, tmp_path):
    """A failed edit must not poison the dedup cache for the next identical text."""
    from bot import notifier

    calls = {"n": 0}

    class FakeBot:
        async def edit_message_text(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TelegramError("bad request")
            return True

    n, _state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())
    n._edit_throttle = notifier.EditThrottle(clock=lambda: 0.0)

    assert asyncio.run(n.edit_message_by_id(200, "retry me")) is False
    assert asyncio.run(n.edit_message_by_id(200, "retry me")) is True
    assert calls["n"] == 2


def test_edit_message_by_id_noop_when_live_cards_disabled(monkeypatch, tmp_path):
    """Session 113: LIVE_GAME_CARDS_ENABLED=False makes edit_message_by_id a no-op.

    Operator doesn't read Telegram, so per-tick editMessageText calls are
    suppressed. Returns False without invoking the bot's edit_message_text.
    """
    from bot import notifier

    calls: list[dict] = []

    class FakeBot:
        async def edit_message_text(self, **kwargs):
            calls.append(kwargs)
            return True

    n, _state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())
    n._edit_throttle = notifier.EditThrottle(clock=lambda: 0.0)
    monkeypatch.setattr(notifier, "LIVE_GAME_CARDS_ENABLED", False)

    result = asyncio.run(n.edit_message_by_id(42, "anything"))

    assert result is False
    assert calls == []


def test_bot_state_telegram_fields_forward_only(monkeypatch, tmp_path):
    """Existing bot_state.json files without telegram_* keys still load cleanly."""
    from bot import notifier

    state_file = tmp_path / "bot_state.json"
    state_file.write_text(json.dumps({"running": True, "scan_count": 12}))
    monkeypatch.setattr(notifier, "BOT_STATE_FILE", state_file)

    state = notifier._load_bot_state()

    assert state["running"] is True
    assert state["scan_count"] == 12
    assert state["telegram_throttled_until"] is None
    assert state["telegram_throttled_count_24h"] == 0
    assert state["telegram_last_send_attempt_at"] is None
    assert state["telegram_last_send_success_at"] is None


def test_daily_report_telegram_health_row(monkeypatch, tmp_path):
    """Health pulse section includes a Telegram delivery row."""
    from datetime import datetime, timedelta, timezone
    from tools import _report_helpers as helpers

    now = datetime(2026, 5, 3, 18, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "bot_state.json"
    state_file.write_text(json.dumps({
        "telegram_throttled_until": None,
        "telegram_throttled_count_24h": 0,
        "telegram_last_send_attempt_at": (now - timedelta(minutes=10)).isoformat(),
        "telegram_last_send_success_at": (now - timedelta(hours=2)).isoformat(),
    }))

    monkeypatch.setattr(helpers, "BOT_STATE_FILE", state_file)
    monkeypatch.setattr(helpers, "process_alive", lambda _now: (True, "PID 1 alive"))
    monkeypatch.setattr(helpers, "_scanner_health_24h", lambda _now: (600.0, 4, 0.0))
    monkeypatch.setattr(helpers, "_decisions_volume_24h", lambda _now: (20, 10.0))
    monkeypatch.setattr(helpers, "_trades_fired_24h", lambda _now: Counter())
    monkeypatch.setattr(helpers, "_error_count_24h", lambda _now: 0)

    rows = helpers.compute_health_pulse(now)
    rendered = helpers.format_health_pulse(rows)

    telegram_row = next(r for r in rows if r["axis"] == "Telegram delivery")
    assert telegram_row["status"] == "✅"
    assert "last success 2.0h ago" in telegram_row["value"]
    assert "not throttled" in rendered


def test_canonical_schema_used_throughout():
    """Notifier source must use the canonical Session 52 telegram_* state keys."""
    source = Path(__file__).resolve().parent.parent / "bot" / "notifier.py"
    text = source.read_text()

    for field in (
        "telegram_throttled_until",
        "telegram_throttled_count_24h",
        "telegram_last_send_attempt_at",
        "telegram_last_send_success_at",
    ):
        assert field in text


def test_high_edit_volume_does_not_exceed_throttle(monkeypatch, tmp_path):
    """Regression: sustained rapid edits are paced at one per second after burst."""
    from bot import notifier

    clock = {"t": 0.0}
    sent_at: list[float] = []

    class FakeBot:
        async def edit_message_text(self, **kwargs):
            sent_at.append(clock["t"])
            return True

    n, _state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())
    n._edit_throttle = notifier.EditThrottle(
        rate_per_second=1.0,
        burst=5,
        clock=lambda: clock["t"],
    )

    async def fake_sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(notifier.asyncio, "sleep", fake_sleep)

    for i in range(100):
        assert asyncio.run(n.edit_message_by_id(300, f"text {i}")) is True

    assert len(sent_at) == 100
    assert sent_at[:5] == [0.0] * 5
    assert all((b - a) >= 1.0 for a, b in zip(sent_at[5:], sent_at[6:]))
    assert sent_at[-1] >= 95.0


# --- Session 58.5: shutdown short-circuit ---


def test_notifier_skips_retries_when_stopping(monkeypatch, tmp_path, caplog):
    """Session 58.5: when _stopping=True, exceptions short-circuit immediately,
    logging ONCE at INFO and skipping the 3-retry loop. Catches the residual
    HTTPXRequest race that Session 58's GlintBot.stop() reorder doesn't.
    """
    from bot import notifier

    calls = {"n": 0}

    class FakeBot:
        async def send_message(self, **kwargs):
            calls["n"] += 1
            raise RuntimeError("This HTTPXRequest is not initialized!")

    n, _state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(notifier.asyncio, "sleep", fake_sleep)

    # Simulate: stop() has been called and set the flag, but in-flight
    # send_message is still awaiting when the HTTPXRequest tears down.
    n._stopping = True

    with caplog.at_level("INFO", logger="glint.notifier"):
        asyncio.run(n.send_message("auto-scan announce"))

    # Single attempt, no retries, no sleep
    assert calls["n"] == 1
    assert sleeps == []
    # INFO log fired once
    assert "skipped during shutdown" in caplog.text
    assert "RuntimeError" in caplog.text


def test_notifier_normal_retry_when_not_stopping(monkeypatch, tmp_path):
    """Session 58.5: when _stopping=False (default), preserve Session 52's
    retry-on-NetworkError contract. Regression guard against the flag
    accidentally short-circuiting normal-operation transient failures.
    """
    from bot import notifier

    calls = {"n": 0}

    class FakeBot:
        async def send_message(self, **kwargs):
            calls["n"] += 1
            raise NetworkError("transient network blip")

    n, _state_file = _notifier_with_fake_app(monkeypatch, tmp_path, FakeBot())

    # Default state: _stopping is False. Verify it explicitly.
    assert n._stopping is False

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(notifier.asyncio, "sleep", fake_sleep)

    asyncio.run(n.send_message("normal blip"))

    # Full retry contract preserved: 3 attempts (max_retries + 1)
    assert calls["n"] == n._TELEGRAM_MAX_RETRIES + 1
    # Backoff sleeps fired (1.0 then 2.0 from 1.0 * 2^attempt_idx)
    assert len(sleeps) == n._TELEGRAM_MAX_RETRIES


# ---------------------------------------------------------------------------
# Session 71: restore _flood_until from bot_state.json on init
# Closes Battle Scar #15 gap — notifier now READS the throttle field it has
# always WRITTEN, so a restart during cooldown is safe (startup sends respect
# the restored cooldown via the existing _check_flood() pre-send check).
# ---------------------------------------------------------------------------


def _notifier_with_persisted_state(monkeypatch, tmp_path, state_dict):
    """Session 71: write fixture state, redirect BOT_STATE_FILE, construct notifier."""
    from bot import notifier

    state_file = tmp_path / "bot_state.json"
    state_file.write_text(json.dumps(state_dict))

    monkeypatch.setattr(notifier, "BOT_STATE_FILE", state_file)
    monkeypatch.setattr(notifier, "TELEGRAM_CHAT_ID", "12345")

    return notifier.TelegramNotifier()


def test_init_restores_flood_until_when_cooldown_in_future(monkeypatch, tmp_path):
    """Persisted cooldown in the future restores _flood_until on init."""
    import time as time_mod
    from datetime import datetime, timezone

    expected_until = time_mod.time() + 3600  # 1 hour from now
    until_iso = datetime.fromtimestamp(expected_until, timezone.utc).isoformat()

    n = _notifier_with_persisted_state(
        monkeypatch, tmp_path, {"telegram_throttled_until": until_iso}
    )

    # Within ±2s of expected (allows for execution time)
    assert abs(n._flood_until - expected_until) < 2.0
    assert n._flood_until > time_mod.time()


def test_init_does_not_restore_when_cooldown_in_past(monkeypatch, tmp_path):
    """Expired persisted cooldown leaves _flood_until at 0.0."""
    import time as time_mod
    from datetime import datetime, timezone

    past_until = time_mod.time() - 3600  # 1 hour ago
    until_iso = datetime.fromtimestamp(past_until, timezone.utc).isoformat()

    n = _notifier_with_persisted_state(
        monkeypatch, tmp_path, {"telegram_throttled_until": until_iso}
    )

    assert n._flood_until == 0.0


def test_init_handles_missing_field_cleanly(monkeypatch, tmp_path, caplog):
    """No telegram_throttled_until field -> _flood_until stays 0.0, no warning."""
    with caplog.at_level("WARNING", logger="glint.notifier"):
        n = _notifier_with_persisted_state(monkeypatch, tmp_path, {"running": True})

    assert n._flood_until == 0.0
    # No "Failed to restore Telegram cooldown" warning expected
    assert "Failed to restore Telegram cooldown" not in caplog.text


def test_init_handles_malformed_timestamp_gracefully(monkeypatch, tmp_path, caplog):
    """Malformed timestamp string -> _flood_until stays 0.0, WARNING logged."""
    with caplog.at_level("WARNING", logger="glint.notifier"):
        n = _notifier_with_persisted_state(
            monkeypatch, tmp_path, {"telegram_throttled_until": "not-a-date"}
        )

    assert n._flood_until == 0.0
    assert "Failed to restore Telegram cooldown" in caplog.text
