"""Tests for bot.sport_classifier (Session 141).

The fine-grained classifier mirrors tools/discovery_agent/_sport_classifier.
These tests pin behavior so the two stay in sync — if a new prefix lands in
the discovery side without showing up here, the future-Claude regression-test
canary fires.
"""
from __future__ import annotations

from bot.sport_classifier import sport_from_ticker_fine


class TestSportFromTickerFine:
    # Per-game prefixes (the S97-relevant set).
    def test_kxnbagame_is_nba_game(self):
        assert sport_from_ticker_fine("KXNBAGAME-26MAY13CLEDET-DET") == "nba_game"

    def test_kxmlbgame_is_mlb_game(self):
        assert sport_from_ticker_fine("KXMLBGAME-26MAY13SFPHI-SF") == "mlb_game"

    def test_kxnhlgame_is_nhl_game(self):
        assert sport_from_ticker_fine("KXNHLGAME-26MAY13TORBOS-TOR") == "nhl_game"

    # Futures prefixes — must NOT collide with per-game (longer prefix wins).
    def test_kxnba_is_nba_futures(self):
        assert sport_from_ticker_fine("KXNBA-26FINALS-LAL") == "nba_futures"

    def test_kxmlb_is_mlb_futures(self):
        assert sport_from_ticker_fine("KXMLB-26WS-NYY") == "mlb_futures"

    def test_kxnhl_is_nhl_futures(self):
        assert sport_from_ticker_fine("KXNHL-26CUP-BOS") == "nhl_futures"

    # Length-priority: KXNBAGAME must beat KXNBA. Regression-lock the sort.
    def test_pergame_prefix_beats_futures_prefix(self):
        # Both prefixes share KXNBA; the longer (KXNBAGAME) must match first.
        assert sport_from_ticker_fine("KXNBAGAME-X") == "nba_game"
        assert sport_from_ticker_fine("KXMLBGAME-X") == "mlb_game"
        assert sport_from_ticker_fine("KXNHLGAME-X") == "nhl_game"

    # Out-of-table prefixes return None (caller falls through to coarse classifier).
    def test_unknown_prefix_returns_none(self):
        assert sport_from_ticker_fine("KXATPMATCH-26MAY13") is None
        assert sport_from_ticker_fine("KXUFCFIGHT-26MAY13") is None
        assert sport_from_ticker_fine("KXIPLGAME-26MAY13") is None
        assert sport_from_ticker_fine("KXHIGHAUS-26MAY13") is None

    # Empty / None inputs.
    def test_none_input_returns_none(self):
        assert sport_from_ticker_fine(None) is None

    def test_empty_string_returns_none(self):
        assert sport_from_ticker_fine("") is None

    # Non-prefix strings.
    def test_non_kalshi_string_returns_none(self):
        assert sport_from_ticker_fine("garbage") is None
        assert sport_from_ticker_fine("nba_game") is None
