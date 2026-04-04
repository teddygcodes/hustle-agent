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
