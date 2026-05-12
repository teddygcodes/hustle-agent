from __future__ import annotations

from bot.cross_platform_matcher import (
    BetTypeSignature,
    GameInstanceSignature,
    MatchResult,
    TimeGranularity,
    _extract_game_instance,
    dates_aligned,
    extract_bet_type,
    extract_time_granularity,
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
    url: str | None = None,
    venue: str | None = None,
) -> dict:
    return {
        "venue": venue,
        "ticker": ticker,
        "question_text": question,
        "close_date": close_date,
        "resolved_outcome": result,
        "resolution_source": source,
        "url": url,
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


class TestBetTypeExtraction:
    def test_extracts_winner(self):
        assert extract_bet_type("Will Atlanta Dream win?") == BetTypeSignature("winner")

    def test_extracts_total(self):
        assert extract_bet_type("Qingdao Hainiu FC vs. Dalian Yingbo FC: O/U 2.5") == BetTypeSignature(
            "total",
            threshold=2.5,
        )

    def test_extracts_handicap_before_map_winner(self):
        assert extract_bet_type("Map Handicap: ISG (-1.5) vs Turma do Pagode (+1.5)") == BetTypeSignature(
            "handicap",
            unit="map",
            threshold=-1.5,
        )

    def test_extracts_exact_score(self):
        assert extract_bet_type("Exact Score: CD Real Tomayapo 2 - 2 CD San Antonio Bulo Bulo?") == BetTypeSignature(
            "exact_score",
            score="2-2",
        )

    def test_extracts_set_map_and_game_winners(self):
        assert extract_bet_type("Set 1 Winner: Khachanov vs Zandschulp") == BetTypeSignature("set_winner", "set", 1)
        assert extract_bet_type("Valorant: ZETA DIVISION vs Gen.G Esports - Map 2 Winner") == BetTypeSignature(
            "map_winner",
            "map",
            2,
        )
        assert extract_bet_type("LoL: G2 Esports vs GIANTX - Game 2 Winner") == BetTypeSignature(
            "game_winner",
            "game",
            2,
        )

    def test_extracts_completed_match_draw_btts_top_n_and_price(self):
        assert extract_bet_type("Cordoba: Completed Match: Juan vs Maximo") == BetTypeSignature("completed_match")
        assert extract_bet_type("Will Team A vs Team B end in a draw?") == BetTypeSignature("draw")
        assert extract_bet_type("RB Leipzig vs. St. Pauli: Both Teams to Score") == BetTypeSignature("both_teams_to_score")
        assert extract_bet_type("Will Beau Hossler finish in the Top 10?") == BetTypeSignature("top_n_finish", threshold=10.0)
        assert extract_bet_type("Bitcoin price on May 12, 2026 at 4am EDT? - $82,400 or above") == BetTypeSignature(
            "price_threshold",
            threshold=82400.0,
        )

    def test_extracts_other_when_no_market_proposition(self):
        assert extract_bet_type("Federal Reserve policy announcement") == BetTypeSignature("other")


class TestTimeGranularityExtraction:
    def test_extracts_hour_specific(self):
        assert extract_time_granularity("Bitcoin above 82,400 on May 12, 4AM ET?") == TimeGranularity.HOUR_SPECIFIC

    def test_extracts_day_wide_for_date_only_price_market(self):
        assert extract_time_granularity("Will the price of Ethereum be above $2,500 on May 11?") == TimeGranularity.DAY_WIDE

    def test_extracts_date_range(self):
        assert extract_time_granularity("Will Bitcoin trade above $100,000 between May 10 and May 12?") == TimeGranularity.DATE_RANGE

    def test_extracts_indefinite(self):
        assert extract_time_granularity("Atlanta Dream vs. Minnesota Lynx") == TimeGranularity.INDEFINITE


class TestGameInstanceExtraction:
    def test_extracts_mlb_game_instance_from_ticker_and_question(self):
        market = _market(
            "KXMLBGAME-26MAY091805COLPHI-COL",
            "Colorado vs Philadelphia",
            venue="kalshi",
        )

        assert _extract_game_instance(market) == GameInstanceSignature(
            "mlb",
            "2026-05-09",
            ("color", "phila"),
        )

    def test_extracts_polymarket_mlb_game_instance_from_url_date(self):
        market = _market(
            "2154818",
            "Colorado Rockies vs. Philadelphia Phillies",
            close_date="2026-05-11 00:37:02+00",
            url="https://polymarket.com/market/mlb-col-phi-2026-05-10",
            venue="polymarket",
        )

        assert _extract_game_instance(market) == GameInstanceSignature(
            "mlb",
            "2026-05-10",
            ("color", "phila"),
        )

    def test_extracts_wnba_and_nhl_style_instances(self):
        assert _extract_game_instance(
            _market("KXWNBAGAME-26MAY09ATLMIN-ATL", "Atlanta vs Minnesota", venue="kalshi")
        ) == GameInstanceSignature("wnba", "2026-05-09", ("atlan", "minne"))
        assert _extract_game_instance(
            _market("KXAHLGAME-26MAY092100COAONT-COA", "Coachella Valley Firebirds vs Ontario Reign", venue="kalshi")
        ) == GameInstanceSignature("ahl", "2026-05-09", ("coach", "ontar"))

    def test_extracts_tennis_instances_from_player_names(self):
        kalshi = _market(
            "KXATPSETWINNER-26MAY12KHAPRI-1-KHA",
            "Karen Khachanov vs Dino Prizmic: Set 1 Winner",
            venue="kalshi",
        )
        polymarket = _market(
            "2208147",
            "Set 1 Winner: Khachanov vs Prizmic",
            close_date="2026-05-12T12:00:00Z",
            url="https://polymarket.com/market/atp-khachan-prizmic-2026-05-12-first-set-winner-Khachanov-vs-Prizmic",
            venue="polymarket",
        )

        assert _extract_game_instance(kalshi) == GameInstanceSignature("atp", "2026-05-12", ("khach", "prizm"))
        assert _extract_game_instance(polymarket) == GameInstanceSignature("atp", "2026-05-12", ("khach", "prizm"))

    def test_extracts_esports_map_instances(self):
        market = _market(
            "KXVALORANTMAP-26MAY120400VARTS-2-VAR",
            "VARREL vs. Team Secret: Map 2",
            venue="kalshi",
        )

        assert _extract_game_instance(market) == GameInstanceSignature("valorant", "2026-05-12", ("secre", "varre"))

    def test_non_sports_markets_return_none(self):
        assert _extract_game_instance(_market("KXHIGHMIA-26MAY10-B88.5", "Highest temperature in Miami on May 10?")) is None

    def test_missing_polymarket_sport_date_returns_none(self):
        market = _market(
            "2154818",
            "Colorado Rockies vs. Philadelphia Phillies",
            close_date="2026-05-11 00:37:02+00",
            venue="polymarket",
        )

        assert _extract_game_instance(market) is None


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
            _market("KXFED", "Will Fed rate be over 4.5 in June?", source="https://federalreserve.gov"),
            _market("123", "Will Fed outcome policy decision be over 4.5 in June?", source="federalreserve.gov"),
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

    def test_bet_type_mismatch_forces_no_match_before_jaccard(self):
        decision = match_markets(
            _market("KXGAME", "Qingdao Hainiu vs Dalian Yingbo FC"),
            _market("1970416", "Qingdao Hainiu FC vs. Dalian Yingbo FC: O/U 2.5"),
        )
        assert decision.result == MatchResult.NO_MATCH
        assert decision.reason.startswith("bet_type_mismatch")

    def test_ambiguous_bet_type_needs_review(self):
        decision = match_markets(
            _market("KXEVENT", "Federal Reserve policy announcement"),
            _market("123", "Federal Reserve policy announcement"),
        )
        assert decision.result == MatchResult.MATCH_NEEDS_REVIEW
        assert decision.reason == "bet_type_ambiguous"

    def test_time_granularity_mismatch_needs_review(self):
        decision = match_markets(
            _market("KXBTC", "Bitcoin price on May 12, 2026 at 4am EDT? - $82,400 or above"),
            _market("123", "Will the price of Bitcoin be above $82,400 on May 12?"),
        )
        assert decision.result == MatchResult.MATCH_NEEDS_REVIEW
        assert decision.reason == "time_granularity_mismatch: hour_specific != day_wide"

    def test_matching_new_gates_preserve_existing_keyword_high_path(self):
        decision = match_markets(
            _market("KXBTC", "Bitcoin price on May 12, 2026 at 4am EDT? - $82,400 or above"),
            _market("123", "Bitcoin above 82,400 on May 12, 4AM ET?"),
        )
        assert decision.result == MatchResult.MATCH_HIGH_CONFIDENCE
        assert decision.reason == "date_aligned_and_keyword_high"

    def test_sports_game_instance_mismatch_forces_no_match(self):
        decision = match_markets(
            _market(
                "KXMLBGAME-26MAY091805COLPHI-COL",
                "Colorado vs Philadelphia",
                close_date="2026-05-10T01:10:14Z",
                result="no",
                venue="kalshi",
            ),
            _market(
                "2154818",
                "Colorado Rockies vs. Philadelphia Phillies",
                close_date="2026-05-11 00:37:02+00",
                result="no",
                url="https://polymarket.com/market/mlb-col-phi-2026-05-10",
                venue="polymarket",
            ),
        )

        assert decision.result == MatchResult.NO_MATCH
        assert decision.reason == "game_instance_mismatch"


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
