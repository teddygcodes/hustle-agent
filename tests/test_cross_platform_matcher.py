from __future__ import annotations

from bot.cross_platform_matcher import (
    MatchResult,
    dates_aligned,
    jaccard,
    match_markets,
    normalize_tokens,
)


def _market(
    ticker: str,
    question: str = "Will Bitcoin be above 100000 on May 12?",
    close_date: str | None = "2026-05-12T12:00:00Z",
    result: str | None = "yes",
    source: str | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "question_text": question,
        "close_date": close_date,
        "resolved_outcome": result,
        "resolution_source": source,
    }


class TestDateAlignment:
    def test_within_24_hours(self):
        assert dates_aligned("2026-05-12T00:00:00Z", "2026-05-12T23:59:59Z")

    def test_exactly_24_hours_counts_as_aligned(self):
        assert dates_aligned("2026-05-12T00:00:00Z", "2026-05-13T00:00:00Z")

    def test_more_than_24_hours_is_not_aligned(self):
        assert not dates_aligned("2026-05-12T00:00:00Z", "2026-05-13T00:00:01Z")


class TestJaccard:
    def test_full_overlap(self):
        assert jaccard({"bitcoin", "may"}, {"bitcoin", "may"}) == 1.0

    def test_partial_overlap(self):
        assert jaccard({"bitcoin", "may"}, {"bitcoin", "june"}) == 1 / 3

    def test_zero_overlap(self):
        assert jaccard({"bitcoin"}, {"trump"}) == 0.0

    def test_normalization_matches_corpus_builder_shape(self):
        assert normalize_tokens("Will the Fed cut rates?") == {"fed", "cut", "rates"}


class TestMatchResultSemantics:
    def test_high_confidence_when_date_and_keywords_align(self):
        decision = match_markets(
            _market("KXBTC"),
            _market("216", "Bitcoin above 100000 May 12?"),
        )
        assert decision.result == MatchResult.MATCH_HIGH_CONFIDENCE
        assert decision.reason == "date_aligned_and_keyword_high"

    def test_review_band_when_partial_keyword_overlap(self):
        decision = match_markets(
            _market("KXFED", "Will the Fed cut rates in June?"),
            _market("123", "Will the Fed pause policy in June?"),
        )
        assert decision.result == MatchResult.MATCH_NEEDS_REVIEW

    def test_no_match_when_overlap_is_low(self):
        decision = match_markets(
            _market("KXBTC", "Will Bitcoin be above 100000?"),
            _market("123", "Will Trump win the election?"),
        )
        assert decision.result == MatchResult.NO_MATCH

    def test_outcome_conflict_forces_no_match(self):
        decision = match_markets(
            _market("KXBTC", result="yes"),
            _market("123", "Bitcoin above 100000 May 12?", result="no"),
        )
        assert decision.result == MatchResult.NO_MATCH
        assert decision.reason == "resolved_outcome_conflict"

    def test_source_match_can_upgrade_review_to_high_confidence(self):
        decision = match_markets(
            _market("KXFED", "Fed rate June", source="https://federalreserve.gov"),
            _market("123", "Fed outcome June", source="federalreserve.gov"),
        )
        assert decision.result == MatchResult.MATCH_HIGH_CONFIDENCE
        assert decision.reason == "resolution_source_upgrade"

    def test_date_misaligned_high_overlap_needs_review_not_high_confidence(self):
        decision = match_markets(
            _market("KXBTC", close_date="2026-05-12T00:00:00Z"),
            _market("123", "Bitcoin above 100000 May 12?", close_date="2026-05-14T00:00:01Z"),
        )
        assert decision.result == MatchResult.MATCH_NEEDS_REVIEW
        assert decision.reason == "date_misaligned_needs_review"

    def test_insufficient_data_when_missing_question(self):
        decision = match_markets(
            _market("KXEMPTY", question=""),
            _market("123", "Bitcoin above 100000 May 12?"),
        )
        assert decision.result == MatchResult.INSUFFICIENT_DATA

    def test_insufficient_data_when_missing_date(self):
        decision = match_markets(
            _market("KXBTC", close_date=None),
            _market("123", "Bitcoin above 100000 May 12?"),
        )
        assert decision.result == MatchResult.INSUFFICIENT_DATA


class TestManualOverrides:
    def test_block_override_beats_algorithmic_high_confidence(self):
        decision = match_markets(
            _market("KXBTC"),
            _market("123", "Bitcoin above 100000 May 12?"),
            {("KXBTC", "123"): {"decision": "block", "reason": "wording mismatch"}},
        )
        assert decision.result == MatchResult.NO_MATCH
        assert decision.reason.startswith("manual_block")

    def test_allow_override_beats_missing_data(self):
        decision = match_markets(
            _market("KXBTC", question="", close_date=None),
            _market("123", "", close_date=None),
            {("KXBTC", "123"): {"decision": "allow", "reason": "operator verified"}},
        )
        assert decision.result == MatchResult.MATCH_HIGH_CONFIDENCE
        assert decision.reason.startswith("manual_allow")
