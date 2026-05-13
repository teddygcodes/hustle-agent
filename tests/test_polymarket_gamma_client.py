from __future__ import annotations

import requests
import pytest

from bot import polymarket_gamma_client as gamma


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _gamma_market(**overrides):
    market = {
        "id": "540817",
        "slug": "new-rhianna-album-before-gta-vi-926",
        "question": "New Rihanna Album before GTA VI?",
        "description": "This market will resolve to Yes if Rihanna releases a new album first.",
        "resolutionSource": "Official streaming or download site.",
        "endDate": "2026-07-31T12:00:00Z",
        "active": True,
        "closed": False,
        "bestBid": 0.51,
        "bestAsk": 0.52,
        "lastTradePrice": 0.51,
        "volume24hr": 2218.886524,
        "events": [{"id": "23784", "slug": "what-will-happen-before-gta-vi"}],
    }
    market.update(overrides)
    return market


def test_get_active_markets_paginates(monkeypatch):
    calls = []
    responses = [
        _Resp([{"id": "1"}]),
        _Resp([{"id": "2"}]),
        _Resp([{"id": "3"}]),
        _Resp([]),
    ]

    def fake_get(url, params, timeout):
        calls.append(params.copy())
        return responses.pop(0)

    monkeypatch.setattr(gamma.requests, "get", fake_get)

    markets = gamma.get_active_markets(limit_per_page=2)

    assert [m["id"] for m in markets] == ["1", "2", "3"]
    assert [c["offset"] for c in calls] == [0, 2, 4, 6]
    assert all(c["active"] == "true" and c["closed"] == "false" for c in calls)


def test_get_active_markets_stops_on_empty_page(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params.copy())
        return _Resp([])

    monkeypatch.setattr(gamma.requests, "get", fake_get)

    assert gamma.get_active_markets(limit_per_page=100) == []
    assert len(calls) == 1


def test_get_active_markets_raises_after_first_page_retries(monkeypatch):
    monkeypatch.setattr(gamma.time, "sleep", lambda _: None)
    attempts = {"count": 0}

    def fake_get(url, params, timeout):
        attempts["count"] += 1
        raise requests.ConnectionError("dns down")

    monkeypatch.setattr(gamma.requests, "get", fake_get)

    with pytest.raises(gamma.PolymarketGammaError):
        gamma.get_active_markets(limit_per_page=100)

    assert attempts["count"] == 3


def test_get_active_markets_returns_partial_after_mid_page_failure(monkeypatch, caplog):
    monkeypatch.setattr(gamma.time, "sleep", lambda _: None)
    calls = {"count": 0}

    def fake_get(url, params, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            return _Resp([{"id": "1"}])
        if calls["count"] == 2:
            return _Resp([{"id": "2"}])
        raise requests.Timeout("slow page")

    monkeypatch.setattr(gamma.requests, "get", fake_get)

    with caplog.at_level("WARNING"):
        markets = gamma.get_active_markets(limit_per_page=1, max_pages=3)

    assert [m["id"] for m in markets] == ["1", "2"]
    assert "returning 2 fetched markets" in caplog.text
    assert calls["count"] == 5


def test_normalize_for_matcher_realistic_gamma_shape():
    normalized = gamma.normalize_for_matcher(_gamma_market())

    assert normalized["venue"] == "polymarket"
    assert normalized["ticker"] == "540817"
    assert normalized["id"] == "540817"
    assert normalized["slug"] == "new-rhianna-album-before-gta-vi-926"
    assert normalized["url"] == "https://polymarket.com/market/new-rhianna-album-before-gta-vi-926"
    assert normalized["question"] == "New Rihanna Album before GTA VI?"
    assert normalized["question_text"] == "New Rihanna Album before GTA VI?"
    assert normalized["title"] == "New Rihanna Album before GTA VI?"
    assert normalized["rules_primary"].startswith("This market will resolve")
    assert normalized["resolution_text"] == "Official streaming or download site."
    assert normalized["result"] == ""
    assert normalized["close_date"].tzinfo is not None
    assert normalized["close_date"].isoformat() == "2026-07-31T12:00:00+00:00"
    assert normalized["best_bid"] == 0.51
    assert normalized["best_ask"] == 0.52
    assert normalized["volume_24h"] == 2218.886524
