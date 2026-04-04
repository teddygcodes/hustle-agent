"""
Tests for the player stats module — normal distribution model, caching, and estimation.
All tests mock ESPN HTTP calls (no real API requests).
"""
import json
import math
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.player_stats import (
    _normal_cdf,
    _prob_over_threshold,
    _normalize_name,
    _load_cache,
    _save_cache,
    _cache_get,
    _cache_set,
    estimate_player_prop_probability,
    CACHE_TTL_SECONDS,
    CACHE_MAX_ENTRIES,
)


# ---------------------------------------------------------------------------
# Normal distribution tests
# ---------------------------------------------------------------------------

class TestNormalCDF:
    def test_cdf_at_zero(self):
        assert abs(_normal_cdf(0) - 0.5) < 1e-10

    def test_cdf_large_positive(self):
        assert _normal_cdf(5.0) > 0.999

    def test_cdf_large_negative(self):
        assert _normal_cdf(-5.0) < 0.001

    def test_cdf_at_one(self):
        # P(Z <= 1) ≈ 0.8413
        assert abs(_normal_cdf(1.0) - 0.8413) < 0.001

    def test_cdf_at_negative_one(self):
        # P(Z <= -1) ≈ 0.1587
        assert abs(_normal_cdf(-1.0) - 0.1587) < 0.001


class TestProbOverThreshold:
    def test_mean_well_above_threshold(self):
        # mean=30, std=5, threshold=20 → P(X>20) should be high
        p = _prob_over_threshold(30, 5, 20)
        assert p > 0.95

    def test_mean_well_below_threshold(self):
        # mean=10, std=3, threshold=25 → P(X>25) should be very low
        p = _prob_over_threshold(10, 3, 25)
        assert p < 0.01

    def test_mean_at_threshold(self):
        # mean=20, std=5, threshold=20 → P(X>20) = 0.5
        p = _prob_over_threshold(20, 5, 20)
        assert abs(p - 0.5) < 0.01

    def test_zero_std_above(self):
        assert _prob_over_threshold(25, 0, 20) == 1.0

    def test_zero_std_below(self):
        assert _prob_over_threshold(15, 0, 20) == 0.0

    def test_realistic_nba_scenario(self):
        # Jalen Brunson: avg 25 pts, std 6, threshold 20
        p = _prob_over_threshold(25, 6, 20)
        assert 0.75 < p < 0.85  # ~79.8%


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------

class TestCache:
    @pytest.fixture(autouse=True)
    def use_tmp_cache(self, tmp_path, monkeypatch):
        """Redirect cache to temp directory."""
        cache_file = tmp_path / "player_cache.json"
        monkeypatch.setattr("agent.player_stats.CACHE_FILE", cache_file)
        monkeypatch.setattr("agent.player_stats.STATE_DIR", tmp_path)

    def test_cache_miss_returns_none(self):
        assert _cache_get("nonexistent_key") is None

    def test_cache_roundtrip(self):
        _cache_set("test_key", {"foo": "bar"})
        result = _cache_get("test_key")
        assert result == {"foo": "bar"}

    def test_cache_ttl_expired(self):
        _cache_set("old_key", {"stale": True})
        # Manually expire the entry
        from agent.player_stats import CACHE_FILE
        cache = json.loads(CACHE_FILE.read_text())
        cache["old_key"]["cached_at"] = time.time() - CACHE_TTL_SECONDS - 1
        CACHE_FILE.write_text(json.dumps(cache))
        assert _cache_get("old_key") is None

    def test_cache_max_size_eviction(self, monkeypatch):
        monkeypatch.setattr("agent.player_stats.CACHE_MAX_ENTRIES", 5)
        for i in range(10):
            _cache_set(f"key_{i}", {"index": i})
        from agent.player_stats import CACHE_FILE
        cache = json.loads(CACHE_FILE.read_text())
        assert len(cache) <= 5


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_basic(self):
        assert _normalize_name("Jalen Brunson") == "jalen brunson"

    def test_special_chars(self):
        assert _normalize_name("Karl-Anthony Towns Jr.") == "karl anthony towns jr"

    def test_whitespace(self):
        assert _normalize_name("  LeBron James  ") == "lebron james"


# ---------------------------------------------------------------------------
# Estimation integration (mocked ESPN)
# ---------------------------------------------------------------------------

class TestEstimatePlayerProp:
    @patch("agent.player_stats.get_player_season_stats")
    @patch("agent.player_stats.get_player_recent_stats")
    def test_with_both_sources(self, mock_recent, mock_season):
        mock_season.return_value = {
            "player": "Jalen Brunson",
            "team": "New York Knicks",
            "stats": {"pts": 25.0, "reb": 3.5, "ast": 7.2},
        }
        mock_recent.return_value = {
            "player": "Jalen Brunson",
            "team": "New York Knicks",
            "games": [
                {"pts": 28}, {"pts": 22}, {"pts": 30}, {"pts": 18},
                {"pts": 25}, {"pts": 32}, {"pts": 20}, {"pts": 27},
                {"pts": 24}, {"pts": 26},
            ],
            "game_count": 10,
        }
        result = estimate_player_prop_probability("Jalen Brunson", "points", 20, "nba")
        assert 0.0 < result["probability"] < 1.0
        assert result["sample_size"] == 10
        assert result["confidence"] == 0.85
        assert result["mean"] is not None
        assert result["std"] is not None

    @patch("agent.player_stats.get_player_season_stats")
    @patch("agent.player_stats.get_player_recent_stats")
    def test_no_data_returns_fifty(self, mock_recent, mock_season):
        mock_season.return_value = None
        mock_recent.return_value = None
        result = estimate_player_prop_probability("Unknown Player", "points", 20, "nba")
        assert result["probability"] == 0.50
        assert result["confidence"] == 0.3

    @patch("agent.player_stats.get_player_season_stats")
    @patch("agent.player_stats.get_player_recent_stats")
    def test_season_only(self, mock_recent, mock_season):
        mock_season.return_value = {
            "player": "Test Player",
            "stats": {"pts": 22.0},
        }
        mock_recent.return_value = None
        result = estimate_player_prop_probability("Test Player", "points", 20, "nba")
        assert result["probability"] > 0.5  # avg 22 > threshold 20
        assert "season average only" in result["source"]

    @patch("agent.player_stats.get_player_season_stats")
    @patch("agent.player_stats.get_player_recent_stats")
    def test_small_sample_low_confidence(self, mock_recent, mock_season):
        mock_season.return_value = None
        mock_recent.return_value = {
            "player": "Test",
            "games": [{"pts": 25}, {"pts": 20}],
            "game_count": 2,
        }
        result = estimate_player_prop_probability("Test", "points", 20, "nba")
        assert result["confidence"] == 0.5  # <5 games
