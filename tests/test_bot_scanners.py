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


def test_ipl_not_in_active_strategies():
    """ipl_game_edge disabled — no resolved data, no proven edge."""
    from bot.config import ACTIVE_STRATEGIES
    assert "ipl_game_edge" not in ACTIVE_STRATEGIES


def test_scan_ethereum_series_no_markets_returns_empty():
    from unittest.mock import patch
    from bot.kalshi_series import scan_ethereum_series

    with patch("bot.kalshi_series._fetch_series_markets", return_value=[]):
        result = scan_ethereum_series()

    assert result == []


def test_eth_not_in_active_strategies():
    """eth_price_edge disabled — vol model overestimates intraday movement (CRYPTO_ENABLED=False)."""
    from bot.config import ACTIVE_STRATEGIES
    assert "eth_price_edge" not in ACTIVE_STRATEGIES


def test_scan_game_vig_kxmlbgame_emits_series_and_attributes():
    """Session 63: per-game MLB structural vig is series-shaped, not futures."""
    from unittest.mock import patch
    from bot.scanner_sports_arb import scan_game_vig

    event = "KXMLBGAME-26MAY082210ATLLAD"
    markets = [
        {
            "ticker": f"{event}-LAD",
            "event_ticker": event,
            "yes_ask": 61,
            "no_ask": 39,
            "volume": 200,
            "title": "Atlanta at Los Angeles Winner?",
        },
        {
            "ticker": f"{event}-ATL",
            "event_ticker": event,
            "yes_ask": 63,
            "no_ask": 64,
            "volume": 200,
            "title": "Atlanta at Los Angeles Winner?",
        },
    ]
    seen = []

    with patch("bot.scanner_sports_arb.get_markets", return_value={"markets": markets}):
        opps = scan_game_vig(
            scan_id="scan-session-63",
            on_market_seen=lambda *args: seen.append(args),
        )

    assert seen == [
        ("scan-session-63", f"{event}-LAD", "vig_stack_series"),
        ("scan-session-63", f"{event}-ATL", "vig_stack_series"),
    ]
    assert len(opps) == 1
    assert opps[0]["ticker"] == f"{event}-LAD"
    assert opps[0]["type"] == "vig_stack_series"
    assert opps[0]["series_ticker"] == "KXMLBGAME"


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


def test_scanner_morning_scan_uses_logger_not_print(capsys):
    """morning_weather_scan must not emit any print() output."""
    from unittest.mock import patch
    import bot.scanner as sc

    with patch.object(sc, "scan_weather_markets", return_value=[]):
        sc.morning_weather_scan()

    captured = capsys.readouterr()
    assert captured.out == "", f"scanner morning_weather_scan printed to stdout: {captured.out!r}"


def test_kalshi_series_uses_logger_not_print(capsys):
    """scan_series_markets must not emit any print() output."""
    from unittest.mock import patch
    import bot.kalshi_series as ks

    with patch.object(ks, "scan_sports_series", return_value=[]), \
         patch.object(ks, "scan_all_crypto_markets", return_value=[]), \
         patch.object(ks, "scan_ipl_series", return_value=[]):
        ks.scan_series_markets()

    captured = capsys.readouterr()
    assert captured.out == "", f"kalshi_series printed to stdout: {captured.out!r}"


def test_odds_scraper_uses_logger_not_print(capsys):
    """fetch_consensus_odds must not emit any print() output."""
    from unittest.mock import patch
    import bot.odds_scraper as os_mod

    with patch.object(os_mod, "fetch_draftkings_odds", return_value={"games": [], "error": "mocked"}), \
         patch.object(os_mod, "fetch_bovada_odds", return_value={"games": [], "error": "mocked"}), \
         patch.object(os_mod, "fetch_fanduel_odds", return_value={"games": [], "error": "mocked"}), \
         patch.object(os_mod, "fetch_espn_odds", return_value={"games": [], "error": "mocked"}), \
         patch.object(os_mod, "_odds_api_fallback", return_value={"games": [], "error": "mocked"}):
        os_mod.fetch_consensus_odds("nba")

    captured = capsys.readouterr()
    assert captured.out == "", f"odds_scraper printed to stdout: {captured.out!r}"


# ---------------------------------------------------------------------------
# Session 109: SCAN_INTERVAL_IDLE 1800→900 cadence increase (Outcome A-defensive)
# Opens a 30-day post_event_reversion re-measurement window. Live and pregame
# cadence are explicitly regression-locked here so any future tuning must touch
# them deliberately.
# ---------------------------------------------------------------------------

def test_scan_interval_idle_is_15_min_post_s109():
    """S109 pins SCAN_INTERVAL_IDLE to 900s (15 min) for denser non-live coverage."""
    from bot.config import SCAN_INTERVAL_IDLE
    assert SCAN_INTERVAL_IDLE == 900


def test_scan_interval_live_unchanged_post_s109():
    """Regression-lock: S109 must not change SCAN_INTERVAL_LIVE."""
    from bot.config import SCAN_INTERVAL_LIVE
    assert SCAN_INTERVAL_LIVE == 120


def test_scan_interval_pregame_unchanged_post_s109():
    """Regression-lock: S109 must not change SCAN_INTERVAL_PREGAME."""
    from bot.config import SCAN_INTERVAL_PREGAME
    assert SCAN_INTERVAL_PREGAME == 600


def test_get_scan_interval_idle_returns_new_value():
    """get_scan_interval with no live or imminent games returns the post-S109 IDLE value."""
    from bot.scanner import get_scan_interval
    assert get_scan_interval([]) == 900


def test_get_scan_interval_live_path_unchanged():
    """get_scan_interval with at least one live game still returns SCAN_INTERVAL_LIVE.

    S109 is non-live-only — the live tick path must be untouched.
    """
    from datetime import datetime, timedelta, timezone
    from bot.scanner import get_scan_interval

    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    live_game = {"commence_time": one_hour_ago}
    assert get_scan_interval([live_game]) == 120
