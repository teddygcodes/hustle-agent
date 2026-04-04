"""Tests for bot scanner configuration."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.config import ACTIVE_SPORTS
from bot.kalshi_series import SPORTS_SERIES


def test_nhl_in_active_sports():
    assert "nhl" in ACTIVE_SPORTS


def test_nhl_has_series_ticker():
    assert SPORTS_SERIES["nhl"] == "KXNHLGAME"


def test_ipl_in_sport_map():
    from agent.sports_data import SPORT_MAP
    assert "ipl" in SPORT_MAP
    assert SPORT_MAP["ipl"] == "cricket_ipl"


def test_ipl_team_aliases_complete():
    from agent.parlay import IPL_TEAM_ALIASES
    required = ["csk", "mi", "rcb", "kkr", "dc", "pbks", "rr", "srh", "gt", "lsg"]
    for abbrev in required:
        assert abbrev in IPL_TEAM_ALIASES, f"Missing IPL alias: {abbrev}"
    for k, v in IPL_TEAM_ALIASES.items():
        assert isinstance(v, str) and len(v) > 0


def test_scan_ipl_series_returns_list():
    from unittest.mock import patch
    from bot.kalshi_series import scan_ipl_series

    fake_markets = [
        {
            "ticker": "KXIPLGAME-26APR11DCCSK-DC",
            "title": "Chennai Super Kings vs Delhi Capitals Winner?",
            "yes_ask": 43,
            "yes_bid": 40,
            "no_ask": 58,
            "no_bid": 55,
            "volume": 500,
            "open_interest": 50,
            "close_time": "2026-04-11T14:00:00Z",
        },
        {
            "ticker": "KXIPLGAME-26APR11DCCSK-CSK",
            "title": "Chennai Super Kings vs Delhi Capitals Winner?",
            "yes_ask": 58,
            "yes_bid": 55,
            "no_ask": 43,
            "no_bid": 40,
            "volume": 500,
            "open_interest": 50,
            "close_time": "2026-04-11T14:00:00Z",
        },
    ]
    fake_lookup = {
        "delhi capitals": 0.60, "capitals": 0.60,
        "chennai super kings": 0.40, "kings": 0.40,
    }

    with patch("bot.kalshi_series._fetch_series_markets", return_value=fake_markets), \
         patch("bot.kalshi_series._build_odds_api_lookup", return_value=fake_lookup):
        result = scan_ipl_series()

    assert isinstance(result, list)
    dc_opps = [o for o in result if o.get("team_abbrev", "").upper() == "DC"]
    assert len(dc_opps) > 0, f"Expected DC opportunity, got: {result}"
    assert dc_opps[0]["type"] == "ipl_game_edge"
    assert dc_opps[0]["edge"] > 0


def test_scan_series_markets_calls_ipl():
    from unittest.mock import patch
    import bot.kalshi_series as ks

    with patch.object(ks, "scan_sports_series", return_value=[]), \
         patch.object(ks, "scan_bitcoin_series", return_value=[]), \
         patch.object(ks, "scan_ethereum_series", return_value=[]), \
         patch.object(ks, "scan_ipl_series", return_value=[{"type": "ipl_game_edge"}]) as mock_ipl:
        result = ks.scan_series_markets()

    mock_ipl.assert_called_once()
    assert any(o["type"] == "ipl_game_edge" for o in result)


def test_ipl_in_active_strategies():
    from bot.config import ACTIVE_STRATEGIES
    assert "ipl_game_edge" in ACTIVE_STRATEGIES


def test_scan_ethereum_series_no_markets_returns_empty():
    from unittest.mock import patch
    from bot.kalshi_series import scan_ethereum_series

    with patch("bot.kalshi_series._fetch_series_markets", return_value=[]):
        result = scan_ethereum_series()

    assert result == []


def test_eth_in_active_strategies():
    from bot.config import ACTIVE_STRATEGIES
    assert "eth_price_edge" in ACTIVE_STRATEGIES
