"""Tests for bot/kalshi_history.py — settled-close fetch + cache."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import bot.kalshi_history as kh


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch):
    """Redirect the cache file to a tmp path so tests don't pollute
    bot/state/cache/."""
    cache_file = tmp_path / "kalshi_settled_closes.json"
    monkeypatch.setattr(kh, "_CACHE_FILE", cache_file)
    # Reset the in-memory cache between tests
    monkeypatch.setattr(kh, "_cache", None)
    yield


class TestFetchSettledClose:
    def test_yes_wins_returns_100(self, monkeypatch):
        def fake_get_market(ticker):
            return {"ticker": ticker, "status": "settled", "result": "yes"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("KXNBAGAME-26APR25PHXLAL-PHX") == 100.0

    def test_no_wins_returns_0(self, monkeypatch):
        def fake_get_market(ticker):
            return {"ticker": ticker, "status": "settled", "result": "no"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("KXNBAGAME-26APR25PHXLAL-LAL") == 0.0

    def test_not_yet_settled_returns_none(self, monkeypatch):
        def fake_get_market(ticker):
            return {"ticker": ticker, "status": "open", "result": ""}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("KXNBAGAME-OPEN") is None

    def test_fetch_error_returns_none(self, monkeypatch):
        def fake_get_market(ticker):
            return {"error": "Kalshi API error: timeout"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("KXFAIL") is None

    def test_unknown_result_value_returns_none(self, monkeypatch):
        """Defensive: a 'result' field we don't recognize ('void',
        'cancelled', etc.) shouldn't crash — just decline to settle it."""
        def fake_get_market(ticker):
            return {"ticker": ticker, "status": "settled", "result": "void"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("KXVOID") is None

    def test_empty_ticker_returns_none(self, monkeypatch):
        # Defensive: avoid making a Kalshi call for empty input.
        called = {"n": 0}
        def fake_get_market(ticker):
            called["n"] += 1
            return {}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("") is None
        assert called["n"] == 0

    def test_finalized_status_resolves_via_result(self, monkeypatch):
        """Regression: Kalshi sends status='finalized' (NOT 'settled') for
        resolved markets. The `result` field is the authoritative signal —
        'yes' / 'no' means the market resolved regardless of status."""
        def fake_get_market(ticker):
            return {"ticker": ticker, "status": "finalized", "result": "no"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("KXNBAGAME-FINAL") == 0.0


class TestCache:
    def test_second_call_does_not_invoke_client(self, monkeypatch):
        """Settled markets never change — once cached, no re-fetch."""
        call_count = {"n": 0}
        def fake_get_market(ticker):
            call_count["n"] += 1
            return {"ticker": ticker, "status": "settled", "result": "yes"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)

        kh.fetch_settled_close("KXNBA-CACHE-TEST")
        kh.fetch_settled_close("KXNBA-CACHE-TEST")
        kh.fetch_settled_close("KXNBA-CACHE-TEST")
        assert call_count["n"] == 1

    def test_unsettled_results_are_not_cached(self, monkeypatch):
        """A None response (market still open) should NOT be persisted —
        next call must re-fetch in case the market has since settled."""
        responses = [
            {"ticker": "KXOPEN", "status": "open", "result": ""},
            {"ticker": "KXOPEN", "status": "settled", "result": "yes"},
        ]
        def fake_get_market(ticker):
            return responses.pop(0)
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)

        assert kh.fetch_settled_close("KXOPEN") is None
        assert kh.fetch_settled_close("KXOPEN") == 100.0

    def test_cache_persists_to_disk(self, monkeypatch):
        def fake_get_market(ticker):
            return {"ticker": ticker, "status": "settled", "result": "no"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)

        kh.fetch_settled_close("KXPERSIST")
        # Force re-read from disk by clearing the in-memory cache
        monkeypatch.setattr(kh, "_cache", None)

        with open(kh._CACHE_FILE) as f:
            on_disk = json.load(f)
        assert on_disk["KXPERSIST"] == 0.0

    def test_cache_file_missing_is_handled(self, monkeypatch):
        """First-ever call: cache file does not exist, no crash."""
        assert not kh._CACHE_FILE.exists()
        def fake_get_market(ticker):
            return {"ticker": ticker, "status": "settled", "result": "yes"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("KXFIRST") == 100.0
        assert kh._CACHE_FILE.exists()

    def test_corrupted_cache_file_is_recovered(self, monkeypatch):
        """Bad JSON on disk shouldn't crash — just treat as empty cache."""
        kh._CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(kh._CACHE_FILE, "w") as f:
            f.write("{not valid json")
        def fake_get_market(ticker):
            return {"ticker": ticker, "status": "settled", "result": "yes"}
        monkeypatch.setattr(kh, "_kalshi_get_market", fake_get_market)
        assert kh.fetch_settled_close("KXRECOVER") == 100.0
