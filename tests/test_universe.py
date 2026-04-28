"""Tests for bot/universe.py — buffered universe-snapshot writer.

Session 12 (Apr 25 pivot-enabling instrumentation arc): every scan snapshots
the full active Kalshi universe before scanners run, scanners attribute
the tickers they touch via on_market_seen, then flush_universe writes the
populated rows to universe.jsonl. These tests pin schema integrity, atomic
append under contention, the never-raise contract, idempotency, and
partial-cursor tolerance.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import universe  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_universe_file(tmp_path, monkeypatch):
    f = tmp_path / "universe.jsonl"
    monkeypatch.setattr(universe, "UNIVERSE_FILE", f)
    monkeypatch.setattr("bot.universe.BOT_STATE_DIR", tmp_path)
    # Ensure a clean buffer between tests.
    universe._BUFFER.clear()
    return f


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _fake_kalshi_pages(pages):
    """Build a get_markets stub that yields the given pages in order.

    Each page is a list of partial market dicts. The stub returns
    {"markets": page, "cursor": <next or None>}.
    """
    state = {"i": 0}

    def _stub(**kwargs):
        i = state["i"]
        if i >= len(pages):
            return {"markets": [], "cursor": None}
        markets = pages[i]
        state["i"] += 1
        cursor = "next" if state["i"] < len(pages) else None
        return {"markets": markets, "cursor": cursor}

    return _stub


def _market(ticker, **overrides):
    base = {
        "ticker": ticker,
        "series_ticker": ticker.split("-")[0],
        "event_ticker": "-".join(ticker.split("-")[:2]),
        "status": "open",
        "close_time": "2026-04-25T20:00:00Z",
        "yes_ask": 53,
        "yes_bid": 51,
        "no_ask": 49,
        "no_bid": 47,
        "volume_24h": 1234,
        "open_interest": 5678,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSnapshotAndFlush:

    def test_snapshot_writes_buffered_rows_on_flush(self, tmp_universe_file, monkeypatch):
        """Schema integrity: every required field present, scanned_by=[]
        initially, ts is ISO UTC, close_time is renamed to close_ts,
        series_ticker is derived from ticker prefix when API returns null."""
        stub = _fake_kalshi_pages([
            [_market("KXTEMP-26APR25-T70.5", series_ticker=None),
             _market("KXTEMP-26APR25-T80.5", series_ticker=None)],
        ])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)

        n = universe.snapshot_universe("S1")
        assert n == 2

        # Before flush, file is empty — buffer holds the rows.
        assert _read_records(tmp_universe_file) == []
        flushed = universe.flush_universe("S1")
        assert flushed == 2

        recs = _read_records(tmp_universe_file)
        assert len(recs) == 2
        for r in recs:
            assert set(r.keys()) >= {
                "ts", "scan_id", "ticker", "series_ticker", "event_ticker",
                "status", "close_ts", "yes_ask", "yes_bid", "no_ask", "no_bid",
                "volume_24h", "open_interest", "scanned_by",
            }
            assert r["scan_id"] == "S1"
            assert r["scanned_by"] == []
            assert r["ts"].endswith("+00:00")
            # close_time → close_ts rename happened
            assert r["close_ts"] == "2026-04-25T20:00:00Z"
            assert "close_time" not in r
            # series_ticker derived from ticker prefix when API returns null
            assert r["series_ticker"] == "KXTEMP"

    def test_snapshot_populates_regime_per_row(self, tmp_universe_file, monkeypatch):
        """Session 14: every flushed row carries `regime` with all 4 axes.
        event_horizon_hr should populate from close_ts on each row."""
        stub = _fake_kalshi_pages([
            [_market("KXNBAGAME-26APR25-LAL"),
             _market("KXTEMP-26APR25-T70.5")],
        ])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)

        universe.snapshot_universe("S1")
        universe.flush_universe("S1")
        recs = _read_records(tmp_universe_file)
        assert len(recs) == 2
        for r in recs:
            assert "regime" in r
            regime = r["regime"]
            assert set(regime.keys()) == {
                "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr",
            }
            assert regime["time_of_day"] in {"morning", "afternoon", "evening", "overnight"}
            assert regime["day_of_week"] in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
            # close_time on the fake market (2026-04-25T20:00:00Z) is in the future
            # relative to record creation, so event_horizon_hr should populate.
            assert regime["event_horizon_hr"] is not None
        # Sport ticker → playoffs (Apr 25 falls in NBA playoffs window)
        nba_row = next(r for r in recs if r["ticker"].startswith("KXNBAGAME"))
        assert nba_row["regime"]["sport_phase"] == "playoffs"
        # Non-sport ticker → null
        temp_row = next(r for r in recs if r["ticker"].startswith("KXTEMP"))
        assert temp_row["regime"]["sport_phase"] is None

    def test_skips_mve_parlay_markets(self, tmp_universe_file, monkeypatch):
        """KXMVE* tickers are parlay-expansion products Kalshi creates in
        bulk; they overwhelm the log without informing strategy gaps."""
        stub = _fake_kalshi_pages([
            [_market("KXTEMP-26APR25-T70.5"),
             _market("KXMVESPORTSMULTIGAMEEXTENDED-S2026D002-99B"),
             _market("KXMVECROSSCATEGORY-S2026A001-AABBCC"),
             _market("KXNBA-26-LAL")],
        ])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)

        n = universe.snapshot_universe("S1")
        assert n == 2  # only KXTEMP + KXNBA make it; both KXMVE* skipped
        universe.flush_universe("S1")

        recs = _read_records(tmp_universe_file)
        tickers = {r["ticker"] for r in recs}
        assert tickers == {"KXTEMP-26APR25-T70.5", "KXNBA-26-LAL"}
        for r in recs:
            assert not r["ticker"].startswith("KXMVE")


class TestAtomicAppend:

    def test_concurrent_flushes_all_land(self, tmp_universe_file):
        """20 threads × 10 scan_ids each = 200 flushed rows under contention."""
        N_THREADS = 20
        N_PER_THREAD = 10

        def writer(thread_id):
            for i in range(N_PER_THREAD):
                scan_id = f"S{thread_id}-{i}"
                # Seed the buffer directly (skipping the API call).
                with universe._LOCK:
                    universe._BUFFER[scan_id] = {
                        f"T{thread_id}-{i}": {
                            "ts": "x",
                            "scan_id": scan_id,
                            "ticker": f"T{thread_id}-{i}",
                            "scanned_by": [],
                        },
                    }
                universe.flush_universe(scan_id)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        recs = _read_records(tmp_universe_file)
        assert len(recs) == N_THREADS * N_PER_THREAD
        assert {r["ticker"] for r in recs} == {
            f"T{t}-{i}" for t in range(N_THREADS) for i in range(N_PER_THREAD)
        }


class TestAttribution:

    def test_on_market_seen_populates_scanned_by(self, tmp_universe_file, monkeypatch):
        """Buffer mutation correctness: multiple scanners on same ticker dedupe;
        unknown ticker is no-op; unknown scan_id is no-op."""
        stub = _fake_kalshi_pages([[_market("KXTEMP-T1"), _market("KXTEMP-T2")]])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)
        universe.snapshot_universe("S1")

        universe.on_market_seen("S1", "KXTEMP-T1", "vig_stack_series")
        universe.on_market_seen("S1", "KXTEMP-T1", "vig_stack_series")  # dedupe
        universe.on_market_seen("S1", "KXTEMP-T1", "sports_monotonicity_arb")
        universe.on_market_seen("S1", "KXTEMP-T2", "vig_stack_series")
        universe.on_market_seen("S1", "UNKNOWN-TICKER", "x")  # no-op
        universe.on_market_seen("UNKNOWN-SCAN", "KXTEMP-T1", "x")  # no-op

        universe.flush_universe("S1")
        recs = {r["ticker"]: r for r in _read_records(tmp_universe_file)}
        assert recs["KXTEMP-T1"]["scanned_by"] == ["vig_stack_series", "sports_monotonicity_arb"]
        assert recs["KXTEMP-T2"]["scanned_by"] == ["vig_stack_series"]


class TestIdempotency:

    def test_double_flush_writes_once(self, tmp_universe_file, monkeypatch):
        """First flush writes + pops buffer; second flush is a no-op (buffer empty)."""
        stub = _fake_kalshi_pages([[_market("KXTEMP-T1")]])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)
        universe.snapshot_universe("S1")

        first = universe.flush_universe("S1")
        second = universe.flush_universe("S1")
        assert first == 1
        assert second == 0
        assert len(_read_records(tmp_universe_file)) == 1


class TestPartialCursor:

    def test_partial_pagination_marks_partial_and_flushes(self, tmp_universe_file, monkeypatch):
        """When mid-cursor pagination raises, captured rows still flush with
        partial: true on every row. snapshot_universe returns the partial count."""
        call_state = {"i": 0}

        def flaky_stub(**kwargs):
            i = call_state["i"]
            call_state["i"] += 1
            if i == 0:
                return {
                    "markets": [_market("KXTEMP-T1"), _market("KXTEMP-T2")],
                    "cursor": "next",
                }
            raise RuntimeError("rate-limit retries exhausted")

        monkeypatch.setattr("agent.kalshi_client.get_markets", flaky_stub)
        n = universe.snapshot_universe("S1")
        assert n == 2  # partial — captured page 1 only

        universe.flush_universe("S1")
        recs = _read_records(tmp_universe_file)
        assert len(recs) == 2
        assert all(r.get("partial") is True for r in recs)

    # Session 28: transient-error retry on the error-dict path.

    def test_transient_error_dict_retries_then_succeeds(self, tmp_universe_file, monkeypatch):
        """A transient kalshi error dict (Connection reset / timed out) on the
        FIRST call followed by a healthy second call should produce a complete
        (non-partial) snapshot. The retry loop swallows the recoverable blip."""
        # Neutralize sleep so the test stays fast.
        monkeypatch.setattr(universe._time, "sleep", lambda *a, **kw: None)
        call_state = {"i": 0}

        def stub(**kwargs):
            i = call_state["i"]
            call_state["i"] += 1
            if i == 0:
                return {"error": "Kalshi API error: [Errno 54] Connection reset by peer"}
            return {
                "markets": [_market("KXTEMP-T1"), _market("KXTEMP-T2")],
                "cursor": None,
            }

        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)
        n = universe.snapshot_universe("S1")
        assert n == 2
        # Retried at least once.
        assert call_state["i"] >= 2

        universe.flush_universe("S1")
        recs = _read_records(tmp_universe_file)
        assert len(recs) == 2
        assert not any(r.get("partial") for r in recs)

    def test_transient_error_dict_retries_exhausted_marks_partial(self, tmp_universe_file, monkeypatch):
        """Persistent transient errors burn through retries, then the cursor
        walk bails partial. Cursor-walk calls = 1 + _CURSOR_RETRY_MAX. (Pass 2
        shadow fetches still run but they're scoped per series_ticker — we
        only count cursor-walk calls here.)"""
        monkeypatch.setattr(universe._time, "sleep", lambda *a, **kw: None)
        cursor_calls = {"n": 0}

        def stub(**kwargs):
            if "series_ticker" not in kwargs:
                cursor_calls["n"] += 1
            return {"error": "Kalshi API error: read operation timed out"}

        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)
        n = universe.snapshot_universe("S1")
        # Cursor walk captured nothing; Pass 2 shadow-fetches also fail with
        # the transient error (no retry there), so the buffer stays empty.
        assert n == 0
        assert cursor_calls["n"] == 1 + universe._CURSOR_RETRY_MAX

        # Empty buffer flushes 0 rows.
        universe.flush_universe("S1")
        recs = _read_records(tmp_universe_file)
        assert len(recs) == 0

    def test_non_transient_error_dict_bails_without_retry(self, tmp_universe_file, monkeypatch):
        """Non-transient errors (e.g. auth failure) should bail on first
        observation of the cursor walk — no retries, no extra latency."""
        monkeypatch.setattr(universe._time, "sleep", lambda *a, **kw: None)
        cursor_calls = {"n": 0}

        def stub(**kwargs):
            if "series_ticker" not in kwargs:
                cursor_calls["n"] += 1
            return {"error": "Kalshi API error: invalid api key"}

        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)
        universe.snapshot_universe("S1")
        assert cursor_calls["n"] == 1  # no retry — bailed immediately


class TestNeverRaises:
    """The trade path must never blow up because of universe-log failure."""

    def test_disk_failure_is_swallowed(self, tmp_universe_file, monkeypatch):
        """Patch open() in flush_universe to raise — flush returns 0, buffer
        is popped (no unbounded growth), no exception escapes."""
        stub = _fake_kalshi_pages([[_market("KXTEMP-T1")]])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)
        universe.snapshot_universe("S1")
        assert "S1" in universe._BUFFER  # buffer present pre-flush

        with patch("bot.universe.open", side_effect=OSError("disk full")):
            n = universe.flush_universe("S1")  # must not raise
        assert n == 0
        assert _read_records(tmp_universe_file) == []
        # Buffer was popped despite the failure — won't leak across scans.
        assert "S1" not in universe._BUFFER

    def test_kalshi_import_failure_returns_zero(self, monkeypatch, tmp_universe_file):
        """If agent.kalshi_client import raises, snapshot returns 0 silently."""
        # Force the import inside snapshot_universe to fail.
        import builtins
        real_import = builtins.__import__

        def boom(name, *args, **kwargs):
            if name.startswith("agent.kalshi_client"):
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", boom)
        n = universe.snapshot_universe("S1")
        assert n == 0


# ---------------------------------------------------------------------------
# Session 15.5 — partial-rate metering
# ---------------------------------------------------------------------------

class TestPartialRateMetering:
    """snapshot_universe persists daily counters total_snapshots_today and
    partial_snapshots_today to bot_state.json (atomic), resets at midnight ET,
    and logs a WARN if the trailing-window partial rate exceeds the threshold."""

    @pytest.fixture
    def metering_state_file(self, tmp_path, monkeypatch):
        """Redirect bot_state.json to tmp_path and clear the sliding window
        between tests to avoid cross-contamination."""
        state_file = tmp_path / "bot_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(universe, "BOT_STATE_FILE", state_file)
        universe._PARTIAL_WINDOW.clear()
        return state_file

    def test_counter_increments_on_full_snapshot(self, monkeypatch, tmp_universe_file, metering_state_file):
        """A non-partial snapshot increments total but not partial."""
        stub = _fake_kalshi_pages([[_market("KXTEMP-26APR25-T70.5")]])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)

        universe.snapshot_universe("S1")

        state = json.loads(metering_state_file.read_text())
        assert state["total_snapshots_today"] == 1
        assert state.get("partial_snapshots_today", 0) == 0

    def test_counter_increments_partial_on_deadline_hit(self, monkeypatch, tmp_universe_file, metering_state_file):
        """When the deadline triggers partial=True, both counters increment."""
        # Force the deadline to immediately trigger by setting it to 0s.
        monkeypatch.setattr(universe, "_SNAPSHOT_DEADLINE_SEC", 0)
        stub = _fake_kalshi_pages([[_market("KXTEMP-26APR25-T70.5")]])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)

        universe.snapshot_universe("S2")

        state = json.loads(metering_state_file.read_text())
        assert state["total_snapshots_today"] == 1
        assert state["partial_snapshots_today"] == 1

    def test_partial_warn_fires_when_window_full_at_threshold(self, monkeypatch, tmp_universe_file,
                                                              metering_state_file, caplog):
        """When sliding window of 10 has >=10% partial, WARN is logged."""
        import logging as _logging
        # Force partial on every snapshot.
        monkeypatch.setattr(universe, "_SNAPSHOT_DEADLINE_SEC", 0)
        stub = _fake_kalshi_pages([[_market("KXTEMP-26APR25-T70.5")]])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)

        with caplog.at_level(_logging.WARNING, logger="glint.universe"):
            for i in range(10):
                universe.snapshot_universe(f"warn-{i}")

        warn_msgs = [r.message for r in caplog.records
                     if r.levelno >= _logging.WARNING and "partial rate" in r.message.lower()]
        assert warn_msgs, f"expected partial-rate WARN, got records: {[r.message for r in caplog.records]}"

    def test_no_warn_below_threshold(self, monkeypatch, tmp_universe_file, metering_state_file, caplog):
        """If window is shorter than 10, WARN does NOT fire even at 100% partial rate."""
        import logging as _logging
        monkeypatch.setattr(universe, "_SNAPSHOT_DEADLINE_SEC", 0)
        stub = _fake_kalshi_pages([[_market("KXTEMP-26APR25-T70.5")]])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)

        with caplog.at_level(_logging.WARNING, logger="glint.universe"):
            # Only 5 partial snapshots — under-filled window.
            for i in range(5):
                universe.snapshot_universe(f"under-{i}")

        warn_msgs = [r.message for r in caplog.records
                     if "partial rate" in r.message.lower()]
        assert not warn_msgs, f"unexpected WARN with under-filled window: {warn_msgs}"

    def test_daily_reset_zeroes_counters(self, monkeypatch, tmp_universe_file, metering_state_file):
        """When ET date rolls over, both counters reset to 0 and last_universe_metering_reset advances."""
        # Seed yesterday's counters.
        state_file = metering_state_file
        state_file.write_text(json.dumps({
            "total_snapshots_today": 50,
            "partial_snapshots_today": 5,
            "last_universe_metering_reset": "2020-01-01",  # ancient stamp
        }))

        stub = _fake_kalshi_pages([[_market("KXTEMP-26APR25-T70.5")]])
        monkeypatch.setattr("agent.kalshi_client.get_markets", stub)

        universe.snapshot_universe("RESET-1")

        state = json.loads(state_file.read_text())
        # Should reset to 0 then increment to 1 — NOT 51.
        assert state["total_snapshots_today"] == 1
        assert state["partial_snapshots_today"] == 0
        assert state["last_universe_metering_reset"] != "2020-01-01"
