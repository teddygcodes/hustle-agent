"""Tests for bot improvement tasks."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Task 1: EV sort + B2B penalty
# ---------------------------------------------------------------------------

def test_ev_sort_orders_by_edge_times_confidence():
    """Opportunities must be sorted descending by edge * confidence."""
    opps = [
        {"type": "series_game_edge", "edge": 0.20, "confidence": 0.60},  # EV=0.12
        {"type": "weather",          "edge": 0.18, "confidence": 0.85},  # EV=0.153
        {"type": "btc_price_edge",   "edge": 0.30, "confidence": 0.40},  # EV=0.12
    ]
    from bot.scanner import _sort_by_ev
    result = _sort_by_ev(opps)
    evs = [o["edge"] * o["confidence"] for o in result]
    assert evs == sorted(evs, reverse=True), f"Not EV-sorted: {evs}"


def test_b2b_penalty_reduces_confidence():
    """Back-to-back flag must reduce confidence by 0.10."""
    from bot.scanner import _apply_b2b_penalty
    opp = {
        "type": "series_game_edge",
        "confidence": 0.80,
        "b2b": True,
    }
    result = _apply_b2b_penalty(opp)
    assert abs(result["confidence"] - 0.70) < 1e-9
    assert result.get("warnings") and "b2b" in result["warnings"][0].lower()


def test_b2b_penalty_noop_when_no_flag():
    """No-op when b2b is False or absent."""
    from bot.scanner import _apply_b2b_penalty
    opp = {"type": "series_game_edge", "confidence": 0.80}
    result = _apply_b2b_penalty(opp)
    assert result["confidence"] == 0.80


# ---------------------------------------------------------------------------
# Task 2: 10-day rolling vol
# ---------------------------------------------------------------------------

def test_btc_vol_uses_log_returns_from_prices():
    """Vol calculation must use log-returns, clamp 1%-12%, return float."""
    from unittest.mock import patch
    import bot.kalshi_series as ks

    prices = [50000 * (1.02 ** i) for i in range(11)]
    fake_data = {"prices": [[i * 86400000, prices[i]] for i in range(11)]}

    ks._BTC_VOL_CACHE = None
    with patch("bot.kalshi_series._get_json", return_value=fake_data):
        vol = ks._get_btc_realized_vol()

    assert 0.01 <= vol <= 0.12, f"Vol {vol:.4f} out of expected range"
    assert isinstance(vol, float)


def test_eth_vol_clamps_to_15_percent():
    """ETH vol ceiling must be 15% (higher than BTC's 12%)."""
    from unittest.mock import patch
    import bot.kalshi_series as ks

    prices = [1000.0]
    for _ in range(10):
        prices.append(prices[-1] * 1.20)
    fake_data = {"prices": [[i * 86400000, prices[i]] for i in range(11)]}

    ks._ETH_VOL_CACHE = None
    with patch("bot.kalshi_series._get_json", return_value=fake_data):
        vol = ks._get_eth_realized_vol()

    assert vol == 0.15, f"ETH vol should be clamped to 0.15, got {vol}"


# ---------------------------------------------------------------------------
# Task 3: HTTP retry
# ---------------------------------------------------------------------------

def test_get_json_retries_on_failure():
    """_get_json must retry up to 3 times before returning None."""
    from unittest.mock import patch
    import urllib.error
    import bot.kalshi_series as ks

    call_count = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        call_count["n"] += 1
        raise urllib.error.URLError("connection reset")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("bot.kalshi_series._time") as mock_time:
        mock_time.sleep = lambda _: None
        result = ks._get_json("http://example.com/test")

    assert result is None
    assert call_count["n"] == 3, f"Expected 3 attempts, got {call_count['n']}"


# ---------------------------------------------------------------------------
# Task 4: Economic markets scanner
# ---------------------------------------------------------------------------

def test_econ_scanner_returns_empty_when_no_markets():
    """scan_econ_markets must return [] when Kalshi has no econ markets open."""
    from unittest.mock import patch
    from bot.econ_scanner import scan_econ_markets

    with patch("bot.econ_scanner._fetch_kalshi_econ_markets", return_value=[]), \
         patch("bot.econ_scanner._get_cpi_nowcast", return_value=None):
        result = scan_econ_markets()

    assert result == []


def test_econ_scanner_edge_detected_when_nowcast_diverges():
    """Edge detected when Cleveland Fed nowcast diverges >15% from Kalshi price."""
    from unittest.mock import patch
    from bot.econ_scanner import scan_econ_markets

    fake_market = {
        "ticker": "KXCPIYOY-26JUN-T3",
        "title": "Will CPI be above 3% year-over-year in June?",
        "yes_ask": 40,
        "volume": 500,
        "open_interest": 50,
        "close_time": "2026-06-15T20:00:00Z",
    }

    with patch("bot.econ_scanner._fetch_kalshi_econ_markets", return_value=[fake_market]), \
         patch("bot.econ_scanner._get_cpi_nowcast", return_value=3.8):
        result = scan_econ_markets()

    assert len(result) == 1
    assert result[0]["type"] == "econ_cpi_edge"
    assert result[0]["edge"] > 0
    assert result[0]["relative_edge"] >= 0.15


def test_econ_not_in_active_strategies():
    """econ_cpi_edge disabled — no resolved data, no proven edge."""
    from bot.config import ACTIVE_STRATEGIES
    assert "econ_cpi_edge" not in ACTIVE_STRATEGIES


# ---------------------------------------------------------------------------
# Task 5: Bot health watchdog
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone, timedelta


def _make_bot_state(last_heartbeat_offset_minutes: float, running: bool) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=last_heartbeat_offset_minutes)).isoformat()
    return {
        "running": running,
        "last_heartbeat": ts,
        "scan_count": 10,
        "scans_today": 2,
        "odds_api_requests_this_month": 50,
        "started_at": ts,
        "last_scan": ts,
        "current_date": "2026-04-05",
        "last_odds_api_request": ts,
        "last_nightly_summary": "2026-04-05",
    }


def _run_bot_start(state: dict) -> list[str]:
    """Helper: run GlintBot.start() with fully mocked I/O, return send_message call strings."""
    from bot.main import GlintBot

    mock_notifier = AsyncMock()
    mock_notifier.initialize = AsyncMock()
    mock_notifier.start_polling = AsyncMock()
    mock_notifier.send_message = AsyncMock()
    mock_notifier.paused = False

    async def _run():
        with patch("bot.main._load_bot_state", return_value=state), \
             patch("bot.main._save_bot_state"), \
             patch("bot.main._load_pending", return_value=[]), \
             patch("bot.main._save_pending"), \
             patch("bot.main._acquire_lock"), \
             patch("bot.main._release_lock"), \
             patch("bot.main.TelegramNotifier", return_value=mock_notifier), \
             patch.object(GlintBot, "_register_commands"), \
             patch.object(GlintBot, "_main_loop", new_callable=AsyncMock), \
             patch.object(GlintBot, "_crypto_scan_loop", new_callable=AsyncMock):
            bot = GlintBot()
            await bot.start()
        return [str(call) for call in mock_notifier.send_message.call_args_list]

    return asyncio.run(_run())


def test_watchdog_alerts_on_stale_heartbeat():
    """If last_heartbeat is >15 min old and running=True, send Telegram alert on startup."""
    stale_state = _make_bot_state(last_heartbeat_offset_minutes=20, running=True)
    calls = _run_bot_start(stale_state)
    assert any(
        "heartbeat" in c.lower() or "crash" in c.lower() or "stale" in c.lower()
        for c in calls
    ), f"Expected watchdog alert, got calls: {calls}"


def test_watchdog_no_alert_when_fresh():
    """If last_heartbeat is recent (2 min), no watchdog alert."""
    fresh_state = _make_bot_state(last_heartbeat_offset_minutes=2, running=True)
    calls = _run_bot_start(fresh_state)
    watchdog_calls = [
        c for c in calls
        if "heartbeat" in c.lower() or "crash" in c.lower() or "stale" in c.lower()
    ]
    assert len(watchdog_calls) == 0, f"Unexpected watchdog alert: {watchdog_calls}"
