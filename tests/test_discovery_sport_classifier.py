"""sport_from_ticker_distinguished — produces per-game vs futures sport names."""

from __future__ import annotations

from tools.discovery_agent._sport_classifier import sport_from_ticker_distinguished


def test_kxmlbgame_classifies_as_mlb_game():
    assert sport_from_ticker_distinguished("KXMLBGAME-26APR291840SFPHI-PHI") == "mlb_game"


def test_kxmlb_futures_classifies_as_mlb_futures():
    assert sport_from_ticker_distinguished("KXMLB-26-LAD") == "mlb_futures"


def test_kxnbagame_classifies_as_nba_game():
    assert sport_from_ticker_distinguished("KXNBAGAME-26APR29CLETOR-CLE") == "nba_game"


def test_kxnba_futures_classifies_as_nba_futures():
    assert sport_from_ticker_distinguished("KXNBA-26-OKC") == "nba_futures"


def test_kxnhlgame_classifies_as_nhl_game():
    assert sport_from_ticker_distinguished("KXNHLGAME-26APR29PITSTL-PIT") == "nhl_game"


def test_kxnhl_futures_classifies_as_nhl_futures():
    assert sport_from_ticker_distinguished("KXNHL-26-FLA") == "nhl_futures"


def test_longest_prefix_match_disambiguates_kxmlb_from_kxmlbgame():
    """KXMLBGAME starts with KXMLB — without longest-prefix match, both keys
    would compete and the wrong one might win. Verify the per-game prefix
    is matched first because it's longer."""
    assert sport_from_ticker_distinguished("KXMLBGAME-X") == "mlb_game"
    assert sport_from_ticker_distinguished("KXMLB-X") == "mlb_futures"


def test_non_overridden_sport_falls_through_to_bot_map():
    """KXATPMATCH / KXUFCFIGHT have no per-game/futures distinction —
    fall through to bot.regime._ticker_to_sport unchanged."""
    assert sport_from_ticker_distinguished("KXATPMATCH-26APR15RUB-RUB") == "atp"
    assert sport_from_ticker_distinguished("KXUFCFIGHT-26APR25VIEMCC-VIE") == "ufc"
    assert sport_from_ticker_distinguished("KXATPCHALLENGERMATCH-26APR15-X") == "atp_challenger"


def test_unknown_ticker_returns_none():
    assert sport_from_ticker_distinguished("KXSOMETHINGNEW-X") is None


def test_empty_string_returns_none():
    assert sport_from_ticker_distinguished("") is None


def test_none_returns_none():
    assert sport_from_ticker_distinguished(None) is None
