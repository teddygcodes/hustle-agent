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
         patch("bot.kalshi_series._build_odds_api_lookup", return_value=fake_lookup), \
         patch("bot.odds_scraper.fetch_bovada_odds", return_value={"games": []}):
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


# ---------------------------------------------------------------------------
# Weather NWS date targeting
# ---------------------------------------------------------------------------

def test_get_forecast_temp_for_date_matches_correct_period():
    """Returns the temperature for the daytime period on the target date."""
    from datetime import date
    from bot.scanner_weather import _get_forecast_temp_for_date

    forecast = {
        "periods": [
            {"name": "This Afternoon", "temperature": 55, "start": "2026-04-04T13:00:00-04:00"},
            {"name": "Tonight", "temperature": 42, "start": "2026-04-04T19:00:00-04:00"},
            {"name": "Sunday", "temperature": 68, "start": "2026-04-05T06:00:00-04:00"},
            {"name": "Sunday Night", "temperature": 50, "start": "2026-04-05T19:00:00-04:00"},
        ]
    }

    # Should return tomorrow's high, not today's
    result = _get_forecast_temp_for_date(forecast, date(2026, 4, 5))
    assert result == 68.0


def test_get_forecast_temp_for_date_skips_night_periods():
    """Night periods are skipped even if their date matches."""
    from datetime import date
    from bot.scanner_weather import _get_forecast_temp_for_date

    forecast = {
        "periods": [
            {"name": "Tonight", "temperature": 42, "start": "2026-04-05T19:00:00-04:00"},
        ]
    }

    result = _get_forecast_temp_for_date(forecast, date(2026, 4, 5))
    assert result is None


def test_get_forecast_temp_for_date_returns_none_when_no_match():
    """Returns None when no period exists for the target date."""
    from datetime import date
    from bot.scanner_weather import _get_forecast_temp_for_date

    forecast = {
        "periods": [
            {"name": "This Afternoon", "temperature": 55, "start": "2026-04-04T13:00:00-04:00"},
        ]
    }

    result = _get_forecast_temp_for_date(forecast, date(2026, 4, 7))
    assert result is None


# ---------------------------------------------------------------------------
# Logger hygiene — scanner files must not write to stdout
# ---------------------------------------------------------------------------

def test_scanner_weather_uses_logger_not_print(capsys):
    """scanner_weather must not emit any print() output."""
    from unittest.mock import patch
    import bot.scanner_weather as sw

    with patch.object(sw, "get_markets", return_value={"markets": []}), \
         patch("bot.scanner_weather.requests.get", side_effect=Exception("no network")):
        sw.scan_weather_markets()

    captured = capsys.readouterr()
    assert captured.out == "", f"scanner_weather printed to stdout: {captured.out!r}"


def test_scanner_sports_uses_logger_not_print(capsys):
    """scanner_sports must not emit any print() output."""
    from unittest.mock import patch
    import bot.scanner_sports as ss

    with patch.object(ss, "get_markets", return_value={"markets": []}):
        ss.scan_parlays("nba", odds_data={}, parlay_markets=[])

    captured = capsys.readouterr()
    assert captured.out == "", f"scanner_sports printed to stdout: {captured.out!r}"
