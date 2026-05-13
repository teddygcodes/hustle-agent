from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.cross_platform_matcher import MatchDecision, MatchResult
from tools import cross_platform_pair_discovery as discovery


NOW = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)


def _kalshi_raw(ticker="KXNBAGAME-26MAY131200BOSNYK-BOS", title="Boston Celtics vs New York Knicks winner"):
    return {
        "ticker": ticker,
        "title": title,
        "subtitle": "",
        "event_ticker": "KXNBAGAME-26MAY131200BOSNYK",
        "series_ticker": ticker.split("-", 1)[0],
        "status": "open",
        "close_time": "2026-05-13T20:00:00Z",
        "expiration_time": "2026-05-13T20:00:00Z",
        "result": "",
        "rules_primary": "Market resolves to the winner of the game.",
        "yes_bid": 51,
        "yes_ask": 53,
        "volume_24h": 1000,
    }


def _poly_raw(market_id="100", slug="nba-boston-new-york-2026-05-13", question="Boston Celtics vs New York Knicks winner"):
    return {
        "id": market_id,
        "slug": slug,
        "question": question,
        "description": "Market resolves to the winner of the game.",
        "resolutionSource": "",
        "endDate": "2026-05-13T20:00:00Z",
        "active": True,
        "closed": False,
    }


def _decision(result: MatchResult, reason="test_reason"):
    return MatchDecision(result, 0.9, 1.0, False, reason)


def test_fetch_open_kalshi_markets_paginates(monkeypatch):
    calls = []
    responses = [
        {"markets": [{"ticker": "K1"}], "cursor": "abc"},
        {"markets": [{"ticker": "K2"}], "cursor": None},
    ]

    def fake_get_markets(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(discovery, "kalshi_get_markets", fake_get_markets)

    markets = discovery.fetch_open_kalshi_markets(max_markets=500)

    assert markets == [{"ticker": "K1"}, {"ticker": "K2"}]
    assert calls[0] == {"status": "open", "limit": 200}
    assert calls[1] == {"status": "open", "limit": 200, "cursor": "abc"}


def test_fetch_active_polymarket_markets_caps(monkeypatch):
    def fake_get_active_markets(limit_per_page, max_pages):
        assert limit_per_page == 100
        assert max_pages == 2
        return [{"id": str(i)} for i in range(150)]

    monkeypatch.setattr(discovery.polymarket_gamma_client, "get_active_markets", fake_get_active_markets)

    markets = discovery.fetch_active_polymarket_markets(max_markets=125)

    assert len(markets) == 125
    assert markets[-1]["id"] == "124"


def test_only_high_confidence_pairs_reach_registry(monkeypatch):
    candidates = [
        {"kalshi_ticker": "K1", "polymarket_ticker": "P1"},
        {"kalshi_ticker": "K2", "polymarket_ticker": "P2"},
        {"kalshi_ticker": "K3", "polymarket_ticker": "P3"},
    ]
    monkeypatch.setattr(discovery, "generate_candidate_pairs", lambda k, p: candidates)

    def fake_normalize_poly(raw):
        return {
            "venue": "polymarket",
            "ticker": raw["id"],
            "id": raw["id"],
            "slug": raw["slug"],
            "url": f"https://polymarket.com/market/{raw['slug']}",
            "source_url": f"https://polymarket.com/market/{raw['slug']}",
            "question": raw["question"],
            "question_text": raw["question"],
            "title": raw["question"],
            "close_date": NOW + timedelta(hours=4),
            "result": "",
            "category": "",
            "rules_primary": raw["description"],
            "description": raw["description"],
            "resolution_text": "",
        }

    monkeypatch.setattr(discovery.polymarket_gamma_client, "normalize_for_matcher", fake_normalize_poly)
    decisions = {
        "P1": _decision(MatchResult.MATCH_HIGH_CONFIDENCE, "date_aligned_and_keyword_high"),
        "P2": _decision(MatchResult.MATCH_NEEDS_REVIEW, "keyword_review_band"),
        "P3": _decision(MatchResult.NO_MATCH, "keyword_overlap_low"),
    }
    monkeypatch.setattr(discovery, "match_markets", lambda k, p, manual_overrides=None: decisions[p["ticker"]])

    kalshi = [
        _kalshi_raw("K1"),
        _kalshi_raw("K2"),
        _kalshi_raw("K3"),
    ]
    poly = [
        _poly_raw("P1", "p1"),
        _poly_raw("P2", "p2"),
        _poly_raw("P3", "p3"),
    ]

    rows, summary = discovery.build_registry_rows(kalshi, poly, discovered_at=NOW)

    assert len(rows) == 1
    row = rows[0]
    assert row["kalshi_market_id"] == "K1"
    assert row["polymarket_market_id"] == "P1"
    assert row["matcher_classification"] == "MATCH_HIGH_CONFIDENCE"
    assert row["matcher_reason"] == "date_aligned_and_keyword_high"
    assert row["pair_id"] == discovery._pair_id("K1", "P1")
    assert row["kalshi_url"] == "https://kalshi.com/markets/K1"
    assert row["polymarket_url"] == "https://polymarket.com/market/p1"
    assert row["discovered_at"] == NOW.isoformat()
    assert set(row["slice_tags"]) == {"sport", "market_type", "time_to_resolution_band"}
    assert summary["candidate_count"] == 3
    assert summary["high_confidence_count"] == 1


def test_slice_tag_derivation_sport_market_type_and_time_bands(caplog):
    nba = discovery.normalize_kalshi_for_matcher(_kalshi_raw())
    mlb = discovery.normalize_kalshi_for_matcher(_kalshi_raw("KXMLBGAME-26MAY131200BOSNYY-BOS", "Boston Red Sox vs New York Yankees winner"))
    poly = discovery.polymarket_gamma_client.normalize_for_matcher(_poly_raw())
    total = discovery.normalize_kalshi_for_matcher(_kalshi_raw("KXNBATOTAL-26MAY13-BOSNYK-T210", "Boston Celtics vs New York Knicks total over 210.5"))

    assert discovery._sport_tag(nba, poly) == "NBA"
    assert discovery._sport_tag(mlb, poly) == "MLB"
    unknown_poly = discovery.polymarket_gamma_client.normalize_for_matcher(
        _poly_raw("101", "will-x-happen", "Will X happen?")
    )
    assert discovery._sport_tag({"venue": "kalshi", "ticker": "KXUNKNOWN", "question": "Will X happen?"}, unknown_poly) == "unknown"
    assert discovery._market_type_tag(nba, poly) == "winner"
    assert discovery._market_type_tag(total, poly) == "total"
    assert discovery._market_type_tag({"question": "Federal Reserve policy announcement"}, {"question": "Federal Reserve policy announcement"}) == "unknown"

    assert discovery._time_to_resolution_band(NOW - timedelta(minutes=1), NOW) == "live"
    assert discovery._time_to_resolution_band(NOW + timedelta(hours=23), NOW) == "today"
    assert discovery._time_to_resolution_band(NOW + timedelta(days=3), NOW) == "1-7d"
    assert discovery._time_to_resolution_band(NOW + timedelta(days=8), NOW) == "7d+"
    assert "past close_date" in caplog.text


def test_write_registry_atomic_preserves_old_file_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "registry.jsonl"
    path.write_text(json.dumps({"old": True}) + "\n")

    def fail_replace(src, dst):
        raise RuntimeError("crash during rename")

    monkeypatch.setattr(discovery.os, "replace", fail_replace)

    with pytest.raises(RuntimeError):
        discovery.write_registry_atomic([{"new": True}], path)

    assert path.read_text() == json.dumps({"old": True}) + "\n"


def test_print_summary_line(capsys):
    summary = {
        "kalshi_count": 2,
        "polymarket_count": 3,
        "candidate_count": 4,
        "high_confidence_count": 1,
        "sport_breakdown": {"NBA": 1},
    }

    discovery._print_summary(summary, "bot/state/cross_platform_pair_registry.jsonl", dry_run=False)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "Fetched 2 kalshi + 3 polymarket markets. Generated 4 candidate pairs "
        "(Jaccard >= 0.40, +/-3 day window). Matcher classified 1 as "
        "MATCH_HIGH_CONFIDENCE. Wrote registry to bot/state/cross_platform_pair_registry.jsonl. "
        "Sport breakdown: NBA=1\n"
    ) == captured.err
