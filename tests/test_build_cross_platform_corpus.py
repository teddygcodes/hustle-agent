"""Tests for tools/build_cross_platform_corpus.py — Session 116 cross-platform
validation corpus builder.

Pure-function helpers (normalize / jaccard / date-window / suggest_label /
generate_pairs / writers) are tested directly. HTTP scrapers are tested with
unittest.mock.patch on requests.get — no network calls.

Per Session 105 design doc, this corpus is what the eventual cross-platform
matcher validates against. The labels here are operator-set; the agent only
generates candidates and suggested labels.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.build_cross_platform_corpus import (
    build_corpus,
    generate_candidate_pairs,
    jaccard,
    normalize_tokens,
    parse_iso_date,
    scrape_kalshi,
    scrape_polymarket,
    suggest_label,
    within_days,
    write_json_cache,
    write_jsonl,
)


# ---------------------------------------------------------------------------
# Task 1 — normalize_tokens
# ---------------------------------------------------------------------------


class TestNormalizeTokens:
    def test_lowercases(self):
        assert normalize_tokens("Trump Wins") == {"trump", "wins"}

    def test_strips_punctuation(self):
        assert normalize_tokens("Will Trump win?") == {"trump", "win"}

    def test_drops_stopwords(self):
        assert normalize_tokens("The Fed rate decision in March") == {
            "fed",
            "rate",
            "decision",
            "march",
        }

    def test_keeps_year_numbers(self):
        assert "2028" in normalize_tokens("Will Trump win in 2028?")

    def test_ascii_folds_unicode(self):
        assert normalize_tokens("Bitcoin price café 🚀") == {"bitcoin", "price", "cafe"}

    def test_empty_string(self):
        assert normalize_tokens("") == set()

    def test_only_stopwords(self):
        assert normalize_tokens("the a of") == set()


# ---------------------------------------------------------------------------
# Task 2 — jaccard
# ---------------------------------------------------------------------------


class TestJaccard:
    def test_identical_sets(self):
        assert jaccard({"a", "b", "c"}, {"a", "b", "c"}) == 1.0

    def test_disjoint_sets(self):
        assert jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial_overlap(self):
        # {a,b} ∩ {b,c} = {b}; union = {a,b,c}; J = 1/3
        result = jaccard({"a", "b"}, {"b", "c"})
        assert abs(result - 1 / 3) < 1e-9

    def test_both_empty_returns_zero(self):
        # Degenerate case — convention is 0.0 so it never trips the threshold
        assert jaccard(set(), set()) == 0.0

    def test_one_empty_returns_zero(self):
        assert jaccard({"a"}, set()) == 0.0


# ---------------------------------------------------------------------------
# Task 3 — date helpers
# ---------------------------------------------------------------------------


class TestParseIsoDate:
    def test_z_suffix(self):
        d = parse_iso_date("2026-05-12T00:00:00Z")
        assert d is not None
        assert d.year == 2026 and d.month == 5 and d.day == 12

    def test_offset_suffix(self):
        d = parse_iso_date("2026-05-12T00:00:00+00:00")
        assert d is not None and d.year == 2026

    def test_date_only(self):
        d = parse_iso_date("2026-05-12")
        assert d is not None and d.year == 2026 and d.month == 5 and d.day == 12

    def test_returns_none_for_garbage(self):
        assert parse_iso_date("") is None
        assert parse_iso_date(None) is None
        assert parse_iso_date("not a date") is None


class TestWithinDays:
    def test_same_day(self):
        a = parse_iso_date("2026-05-12T00:00:00Z")
        b = parse_iso_date("2026-05-12T18:00:00Z")
        assert within_days(a, b, 3)

    def test_within_3_days(self):
        a = parse_iso_date("2026-05-10")
        b = parse_iso_date("2026-05-12")
        assert within_days(a, b, 3)

    def test_outside_window(self):
        a = parse_iso_date("2026-05-01")
        b = parse_iso_date("2026-05-12")
        assert not within_days(a, b, 3)

    def test_none_inputs_return_false(self):
        a = parse_iso_date("2026-05-12")
        assert not within_days(a, None, 3)
        assert not within_days(None, a, 3)
        assert not within_days(None, None, 3)


# ---------------------------------------------------------------------------
# Task 4 — Polymarket scraper (mocked HTTP)
# ---------------------------------------------------------------------------


def _poly_resp(markets, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = markets
    resp.raise_for_status = MagicMock()
    return resp


class TestScrapePolymarket:
    def test_normalizes_yes_outcome(self):
        sample = [{
            "id": "12",
            "question": "Will Trump win 2028?",
            "endDate": "2028-11-05T00:00:00Z",
            "closedTime": "2028-11-06T12:00:00Z",
            "category": "Politics",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]',
            "slug": "trump-2028",
            "closed": True,
        }]
        responses = [_poly_resp(sample), _poly_resp([])]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_polymarket(page_size=500, sleep_seconds=0)
        assert len(result) == 1
        m = result[0]
        assert m["venue"] == "polymarket"
        assert m["ticker"] == "12"
        assert m["question"] == "Will Trump win 2028?"
        assert m["result"] == "yes"
        assert m["category"] == "Politics"
        # closedTime preferred over endDate
        assert m["close_date"] == "2028-11-06T12:00:00Z"

    def test_keeps_market_with_null_category(self):
        # Polymarket no longer tags recent markets with a top-level category —
        # the scraper must NOT filter on category (rely on Jaccard at pair time).
        sample = [{
            "id": "99",
            "question": "Will the match end in a draw?",
            "endDate": "2026-05-15T04:00:00Z",
            "closedTime": "2026-05-12T13:45:47Z",
            "category": None,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0", "1"]',
            "closed": True,
        }]
        responses = [_poly_resp(sample), _poly_resp([])]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_polymarket(page_size=500, sleep_seconds=0)
        assert len(result) == 1
        assert result[0]["result"] == "no"
        assert result[0]["category"] == ""

    def test_skips_ambiguous_outcome(self):
        sample = [{
            "id": "13",
            "question": "Will X happen?",
            "endDate": "2026-01-01T00:00:00Z",
            "category": "Politics",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
            "closed": True,
        }]
        responses = [_poly_resp(sample), _poly_resp([])]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_polymarket(page_size=500, sleep_seconds=0)
        assert result == []

    def test_paginates_until_empty(self):
        page1 = [{
            "id": "1", "question": "Q1", "endDate": "2026-01-01T00:00:00Z",
            "category": "Politics", "outcomes": '["Yes","No"]',
            "outcomePrices": '["1","0"]', "closed": True,
        }]
        page2 = [{
            "id": "2", "question": "Q2", "endDate": "2026-01-02T00:00:00Z",
            "category": "Crypto", "outcomes": '["Yes","No"]',
            "outcomePrices": '["0","1"]', "closed": True,
        }]
        responses = [_poly_resp(page1), _poly_resp(page2), _poly_resp([])]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses) as mock_get:
            result = scrape_polymarket(page_size=1, sleep_seconds=0)
        assert len(result) == 2
        assert mock_get.call_count == 3

    def test_skips_market_without_question(self):
        sample = [{
            "id": "99", "question": "", "endDate": "2026-01-01T00:00:00Z",
            "category": "Politics", "outcomes": '["Yes","No"]',
            "outcomePrices": '["1","0"]', "closed": True,
        }]
        responses = [_poly_resp(sample), _poly_resp([])]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_polymarket(page_size=500, sleep_seconds=0)
        assert result == []


# ---------------------------------------------------------------------------
# Task 5 — Kalshi scraper (mocked HTTP)
# ---------------------------------------------------------------------------


def _kalshi_events_resp(events, cursor=""):
    """Mock a /events response with nested markets."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"events": events, "cursor": cursor}
    resp.raise_for_status = MagicMock()
    return resp


def _kalshi_event(title, markets, event_ticker="EV-1", category="Politics"):
    return {"event_ticker": event_ticker, "title": title, "category": category, "markets": markets}


def _kalshi_raw_market(ticker, result="no", close_time="2026-05-12T20:00:00Z", **extra):
    m = {
        "ticker": ticker,
        "result": result,
        "close_time": close_time,
        "expiration_time": close_time,
    }
    m.update(extra)
    return m


class TestScrapeKalshi:
    def test_normalizes_event_with_nested_market(self):
        events = [_kalshi_event(
            "Trump approval above 50% on May 12 2026?",
            [_kalshi_raw_market("KXTRUMPAPPROVE-26MAY-T50", result="no")],
            event_ticker="KXTRUMPAPPROVE-26MAY",
            category="Politics",
        )]
        responses = [_kalshi_events_resp(events, cursor="")]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_kalshi(sleep_seconds=0)
        assert len(result) == 1
        m = result[0]
        assert m["venue"] == "kalshi"
        assert m["ticker"] == "KXTRUMPAPPROVE-26MAY-T50"
        assert m["question"] == "Trump approval above 50% on May 12 2026?"
        assert m["result"] == "no"
        assert m["category"] == "Politics"
        assert m["close_date"] == "2026-05-12T20:00:00Z"

    def test_composes_question_with_custom_strike(self):
        events = [_kalshi_event(
            "Who will be the next Supreme Leader of Iran?",
            [_kalshi_raw_market(
                "KXNEXTIRANLEADER-MOJTABA",
                result="yes",
                custom_strike={"Individual": "Mojtaba Khamenei"},
            )],
            category="Elections",
        )]
        responses = [_kalshi_events_resp(events, cursor="")]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_kalshi(sleep_seconds=0)
        assert len(result) == 1
        assert result[0]["question"] == (
            "Who will be the next Supreme Leader of Iran? — Mojtaba Khamenei"
        )

    def test_filters_out_mve_tickers(self):
        events = [_kalshi_event(
            "Parlay event",
            [_kalshi_raw_market("KXMVECROSSCATEGORY-1", result="yes")],
            category="Politics",
        )]
        responses = [_kalshi_events_resp(events, cursor="")]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_kalshi(sleep_seconds=0)
        assert result == []

    def test_filters_out_event_not_in_allowed_category(self):
        # Events whose category isn't on the allowlist must be dropped client-side.
        events = [_kalshi_event(
            "Will Lakers win NBA championship?",
            [_kalshi_raw_market("KXNBAFUT-LAL", result="no", close_time="2026-06-01T00:00:00Z")],
            category="Sports",
        )]
        responses = [_kalshi_events_resp(events, cursor="")]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_kalshi(sleep_seconds=0)
        assert result == []

    def test_skips_markets_without_explicit_result(self):
        events = [_kalshi_event(
            "Fed cut in May?",
            [_kalshi_raw_market("KXFEDDECISION-26MAY-RATE", result="")],
            category="Economics",
        )]
        responses = [_kalshi_events_resp(events, cursor="")]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_kalshi(sleep_seconds=0)
        assert result == []

    def test_paginates_via_cursor(self):
        page1 = _kalshi_events_resp(
            [_kalshi_event(
                "Q1",
                [_kalshi_raw_market("KXPRES2028-WINNER", result="yes", close_time="2028-11-05T00:00:00Z")],
                category="Elections",
            )],
            cursor="abc123",
        )
        page2 = _kalshi_events_resp(
            [_kalshi_event(
                "Q2",
                [_kalshi_raw_market("KXBTC-100K", result="no", close_time="2026-12-31T00:00:00Z")],
                category="Crypto",
            )],
            cursor="",
        )
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=[page1, page2]) as mock_get:
            result = scrape_kalshi(sleep_seconds=0)
        assert len(result) == 2
        assert mock_get.call_count == 2
        second_call_params = mock_get.call_args_list[1].kwargs["params"]
        assert second_call_params["cursor"] == "abc123"

    def test_dedupes_repeated_tickers(self):
        # If the same event appears across multiple pages (API quirk),
        # the ticker dedupe must keep only one copy.
        events_p1 = [_kalshi_event(
            "Q1",
            [_kalshi_raw_market("KXSAME-TICKER", result="yes")],
            category="Politics",
        )]
        events_p2 = [_kalshi_event(
            "Q1 duplicate",
            [_kalshi_raw_market("KXSAME-TICKER", result="yes")],
            category="Politics",
        )]
        responses = [_kalshi_events_resp(events_p1, cursor="abc"), _kalshi_events_resp(events_p2, cursor="")]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_kalshi(sleep_seconds=0)
        assert len(result) == 1

    def test_custom_categories(self):
        events = [_kalshi_event(
            "Q",
            [_kalshi_raw_market("KXFOO-BAR", result="yes", close_time="2026-01-01T00:00:00Z")],
            category="Companies",
        )]
        responses = [_kalshi_events_resp(events, cursor="")]
        with patch("tools.build_cross_platform_corpus.requests.get", side_effect=responses):
            result = scrape_kalshi(sleep_seconds=0, categories=("Companies",))
        assert len(result) == 1
        assert result[0]["category"] == "Companies"


# ---------------------------------------------------------------------------
# Task 6 — suggest_label
# ---------------------------------------------------------------------------


class TestSuggestLabel:
    def test_high_jaccard_tight_date_same_result(self):
        assert suggest_label(jaccard_score=0.75, days_apart=0, kalshi_result="yes", polymarket_result="yes") == "MATCH"

    def test_high_jaccard_tight_date_different_result(self):
        # Strong text + date alignment but resolutions disagree: operator eyes
        assert suggest_label(jaccard_score=0.75, days_apart=0, kalshi_result="yes", polymarket_result="no") == "NEEDS_REVIEW"

    def test_medium_jaccard(self):
        assert suggest_label(jaccard_score=0.50, days_apart=1, kalshi_result="yes", polymarket_result="yes") == "NEEDS_REVIEW"

    def test_high_jaccard_loose_date(self):
        assert suggest_label(jaccard_score=0.80, days_apart=2, kalshi_result="yes", polymarket_result="yes") == "NEEDS_REVIEW"

    def test_low_jaccard(self):
        assert suggest_label(jaccard_score=0.30, days_apart=0, kalshi_result="yes", polymarket_result="yes") == "NO_MATCH"


# ---------------------------------------------------------------------------
# Task 7 — generate_candidate_pairs
# ---------------------------------------------------------------------------


def _kalshi_market(**overrides):
    m = {
        "venue": "kalshi",
        "ticker": "KXTRUMPAPPROVE-26MAY-T50",
        "question": "Trump approval above 50% in May 2026?",
        "close_date": "2026-05-31T00:00:00Z",
        "result": "no",
        "category": "Politics",
        "source_url": "https://kalshi.com/markets/KXTRUMPAPPROVE-26MAY-T50",
    }
    m.update(overrides)
    return m


def _poly_market(**overrides):
    m = {
        "venue": "polymarket",
        "ticker": "9001",
        "question": "Will Trump approval be above 50% on May 31 2026?",
        "close_date": "2026-05-31T20:00:00Z",
        "result": "no",
        "category": "Politics",
        "source_url": "https://polymarket.com/market/trump-approval-may-2026",
    }
    m.update(overrides)
    return m


class TestGenerateCandidatePairs:
    def test_emits_high_overlap_pair(self):
        pairs = generate_candidate_pairs([_kalshi_market()], [_poly_market()])
        assert len(pairs) == 1
        p = pairs[0]
        assert p["kalshi_ticker"] == "KXTRUMPAPPROVE-26MAY-T50"
        assert p["polymarket_ticker"] == "9001"
        assert p["jaccard"] >= 0.40
        assert p["days_apart"] <= 3
        assert p["suggested_label"] in {"MATCH", "NEEDS_REVIEW"}
        assert p["operator_label"] is None

    def test_drops_low_jaccard(self):
        k = _kalshi_market(question="Will the Fed cut rates in May?")
        p = _poly_market(question="Will Bitcoin reach 100k in May?")
        pairs = generate_candidate_pairs([k], [p])
        assert pairs == []

    def test_drops_far_dates(self):
        k = _kalshi_market()
        p = _poly_market(close_date="2026-01-01T00:00:00Z")
        pairs = generate_candidate_pairs([k], [p])
        assert pairs == []

    def test_serializable_to_json(self):
        pairs = generate_candidate_pairs([_kalshi_market()], [_poly_market()])
        for pair in pairs:
            assert json.loads(json.dumps(pair)) == pair


# ---------------------------------------------------------------------------
# Task 8 — writers
# ---------------------------------------------------------------------------


class TestWriteJsonl:
    def test_writes_one_line_per_record(self, tmp_path):
        path = tmp_path / "queue.jsonl"
        pairs = [
            {"kalshi_ticker": "K1", "polymarket_ticker": "P1", "jaccard": 0.8},
            {"kalshi_ticker": "K2", "polymarket_ticker": "P2", "jaccard": 0.5},
        ]
        write_jsonl(pairs, path)
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["kalshi_ticker"] == "K1"
        assert json.loads(lines[1])["kalshi_ticker"] == "K2"

    def test_empty_writes_empty_file(self, tmp_path):
        path = tmp_path / "queue.jsonl"
        write_jsonl([], path)
        assert path.exists()
        assert path.read_text() == ""

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "queue.jsonl"
        path.write_text("old content\n")
        write_jsonl([{"a": 1}], path)
        body = path.read_text()
        # JSON content present, old content gone
        assert json.loads(body.strip()) == {"a": 1}


class TestWriteJsonCache:
    def test_atomic_replace(self, tmp_path):
        path = tmp_path / "cache.json"
        write_json_cache({"markets": [{"ticker": "K1"}]}, path)
        assert json.loads(path.read_text()) == {"markets": [{"ticker": "K1"}]}


# ---------------------------------------------------------------------------
# Task 9 — build_corpus CLI orchestration
# ---------------------------------------------------------------------------


class TestBuildCorpus:
    def test_writes_queue_and_caches(self, tmp_path):
        kalshi_sample = [_kalshi_market()]
        poly_sample = [_poly_market()]
        with patch("tools.build_cross_platform_corpus.scrape_kalshi", return_value=kalshi_sample), \
             patch("tools.build_cross_platform_corpus.scrape_polymarket", return_value=poly_sample):
            summary = build_corpus(
                queue_path=tmp_path / "queue.jsonl",
                cache_dir=tmp_path / "cache",
                skip_scrape=False,
                kalshi_only=False,
                polymarket_only=False,
                dry_run=False,
            )
        assert summary["kalshi_count"] == 1
        assert summary["polymarket_count"] == 1
        assert summary["candidate_count_total"] == 1
        assert summary["candidate_count_queued"] == 1
        queue_path = tmp_path / "queue.jsonl"
        assert queue_path.exists()
        lines = queue_path.read_text().splitlines()
        assert len(lines) == 1
        assert (tmp_path / "cache" / "kalshi_settled_markets.json").exists()
        assert (tmp_path / "cache" / "polymarket_settled_markets.json").exists()

    def test_dry_run_does_not_write_queue(self, tmp_path):
        with patch("tools.build_cross_platform_corpus.scrape_kalshi", return_value=[]), \
             patch("tools.build_cross_platform_corpus.scrape_polymarket", return_value=[]):
            summary = build_corpus(
                queue_path=tmp_path / "queue.jsonl",
                cache_dir=tmp_path / "cache",
                skip_scrape=False,
                kalshi_only=False,
                polymarket_only=False,
                dry_run=True,
            )
        assert summary["candidate_count_total"] == 0
        assert summary["candidate_count_queued"] == 0
        assert not (tmp_path / "queue.jsonl").exists()

    def test_skip_scrape_reuses_cache(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "kalshi_settled_markets.json").write_text(json.dumps({"markets": []}))
        (cache_dir / "polymarket_settled_markets.json").write_text(json.dumps({"markets": []}))
        with patch("tools.build_cross_platform_corpus.scrape_kalshi") as mk, \
             patch("tools.build_cross_platform_corpus.scrape_polymarket") as mp:
            build_corpus(
                queue_path=tmp_path / "queue.jsonl",
                cache_dir=cache_dir,
                skip_scrape=True,
                kalshi_only=False,
                polymarket_only=False,
                dry_run=False,
            )
        mk.assert_not_called()
        mp.assert_not_called()
