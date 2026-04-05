"""
Tests for the parlay decomposition engine — parser and pricer.
All tests are pure unit tests with no API calls.
"""
import pytest

from agent.parlay import (
    LegType,
    ParlayLeg,
    parse_parlay_title,
    price_parlay,
    _resolve_team,
    _parse_segment,
)


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestParseSegment:
    def test_team_win_simple(self):
        leg = _parse_segment("yes Boston")
        assert leg.leg_type == LegType.TEAM_WIN
        assert leg.side == "yes"
        assert leg.team == "Boston Celtics"
        assert leg.confidence == 1.0

    def test_team_win_no_side(self):
        """'no' side should still parse as team win."""
        leg = _parse_segment("no Charlotte")
        assert leg.leg_type == LegType.TEAM_WIN
        assert leg.side == "no"
        assert leg.team == "Charlotte Hornets"

    def test_player_prop(self):
        leg = _parse_segment("yes Jalen Brunson: 20+")
        assert leg.leg_type == LegType.PLAYER_PROP
        assert leg.side == "yes"
        assert leg.player == "Jalen Brunson"
        assert leg.threshold == 20.0
        assert leg.stat == "points"

    def test_player_prop_decimal(self):
        leg = _parse_segment("yes Karl-Anthony Towns: 14+")
        assert leg.leg_type == LegType.PLAYER_PROP
        assert leg.player == "Karl-Anthony Towns"
        assert leg.threshold == 14.0

    def test_total_points_over(self):
        leg = _parse_segment("yes Over 235.5 points scored")
        assert leg.leg_type == LegType.TOTAL_POINTS
        assert leg.side == "yes"
        assert leg.threshold == 235.5
        assert leg.direction == "over"

    def test_total_points_no_side(self):
        leg = _parse_segment("no Over 210 points")
        assert leg.leg_type == LegType.TOTAL_POINTS
        assert leg.side == "no"
        assert leg.threshold == 210.0

    def test_total_points_under(self):
        leg = _parse_segment("yes Under 220.5 points scored")
        assert leg.leg_type == LegType.TOTAL_POINTS
        assert leg.direction == "under"
        assert leg.threshold == 220.5

    def test_spread(self):
        leg = _parse_segment("yes Atlanta wins by over 8.5 Points")
        assert leg.leg_type == LegType.SPREAD
        assert leg.side == "yes"
        assert leg.team == "Atlanta Hawks"
        assert leg.threshold == 8.5

    def test_spread_no_explicit_side(self):
        leg = _parse_segment("Atlanta wins by over 4.5 Points")
        assert leg.leg_type == LegType.SPREAD
        assert leg.side == "yes"  # defaults to yes
        assert leg.team == "Atlanta Hawks"
        assert leg.threshold == 4.5

    def test_unknown_leg(self):
        leg = _parse_segment("something completely random here")
        assert leg.leg_type == LegType.UNKNOWN
        assert leg.confidence == 0.3
        assert len(leg.warnings) > 0

    def test_confidence_known_team(self):
        leg = _parse_segment("yes Houston")
        assert leg.confidence == 1.0
        assert leg.team == "Houston Rockets"

    def test_confidence_unknown_team(self):
        leg = _parse_segment("yes Bostton")  # typo
        assert leg.leg_type == LegType.TEAM_WIN
        # Should fuzzy match with lower confidence
        assert leg.confidence <= 1.0


class TestParseTitle:
    def test_full_parlay_title(self):
        title = "yes Boston, yes Charlotte, yes Jalen Brunson: 20+, yes Over 235.5 points scored"
        legs = parse_parlay_title(title)
        assert len(legs) == 4
        assert legs[0].leg_type == LegType.TEAM_WIN
        assert legs[0].team == "Boston Celtics"
        assert legs[1].leg_type == LegType.TEAM_WIN
        assert legs[1].team == "Charlotte Hornets"
        assert legs[2].leg_type == LegType.PLAYER_PROP
        assert legs[2].player == "Jalen Brunson"
        assert legs[3].leg_type == LegType.TOTAL_POINTS
        assert legs[3].threshold == 235.5

    def test_mixed_types(self):
        title = "yes Atlanta wins by over 4.5 Points, no Over 210 points, yes Miami"
        legs = parse_parlay_title(title)
        assert len(legs) == 3
        assert legs[0].leg_type == LegType.SPREAD
        assert legs[1].leg_type == LegType.TOTAL_POINTS
        assert legs[1].side == "no"
        assert legs[2].leg_type == LegType.TEAM_WIN

    def test_empty_title(self):
        assert parse_parlay_title("") == []

    def test_single_leg(self):
        legs = parse_parlay_title("yes Denver")
        assert len(legs) == 1
        assert legs[0].team == "Denver Nuggets"


class TestResolveTeam:
    def test_exact_match(self):
        name, conf = _resolve_team("Boston")
        assert name == "Boston Celtics"
        assert conf == 1.0

    def test_case_insensitive(self):
        name, conf = _resolve_team("BOSTON")
        assert name == "Boston Celtics"
        assert conf == 1.0

    def test_alias(self):
        name, conf = _resolve_team("Cavs")
        assert name == "Cleveland Cavaliers"

    def test_nba_abbreviation(self):
        name, conf = _resolve_team("GSW")
        assert name == "Golden State Warriors"
        assert conf == 1.0

    def test_nba_abbreviation_nyk(self):
        name, conf = _resolve_team("NYK")
        assert name == "New York Knicks"
        assert conf == 1.0

    def test_mlb_team(self):
        name, conf = _resolve_team("Seattle", sport_hint="mlb")
        assert name == "Seattle Mariners"
        assert conf == 1.0

    def test_mlb_abbreviation(self):
        name, conf = _resolve_team("SF", sport_hint="mlb")
        assert name == "San Francisco Giants"
        assert conf == 1.0

    def test_mlb_new_york_mets(self):
        name, conf = _resolve_team("New York M", sport_hint="mlb")
        assert name == "New York Mets"
        assert conf == 1.0

    def test_mlb_los_angeles_angels(self):
        name, conf = _resolve_team("Los Angeles A", sport_hint="mlb")
        assert name == "Los Angeles Angels"
        assert conf == 1.0

    def test_mlb_texas(self):
        name, conf = _resolve_team("Texas", sport_hint="mlb")
        assert name == "Texas Rangers"
        assert conf == 1.0

    def test_unknown(self):
        name, conf = _resolve_team("Nonexistent Team XYZ")
        assert conf <= 0.5


# ---------------------------------------------------------------------------
# Pricer tests
# ---------------------------------------------------------------------------

def _make_odds_data():
    """Factory for mock odds data."""
    return {
        "sport": "basketball_nba",
        "game_count": 2,
        "games": [
            {
                "id": "game1",
                "home_team": "Boston Celtics",
                "away_team": "Charlotte Hornets",
                "commence_time": "2026-04-03T23:00:00Z",
                "consensus": {
                    "Boston Celtics": 0.91,
                    "Charlotte Hornets": 0.09,
                },
                "bookmakers": [
                    {
                        "name": "FanDuel",
                        "h2h": [
                            {"name": "Boston Celtics", "price": -1100, "implied_prob": 0.9167},
                            {"name": "Charlotte Hornets", "price": 700, "implied_prob": 0.125},
                        ],
                        "spreads": [
                            {"name": "Boston Celtics", "price": -110, "point": -12.5, "implied_prob": 0.5238},
                            {"name": "Charlotte Hornets", "price": -110, "point": 12.5, "implied_prob": 0.5238},
                        ],
                        "totals": [
                            {"name": "Over", "price": -110, "point": 220.5, "implied_prob": 0.5238},
                            {"name": "Under", "price": -110, "point": 220.5, "implied_prob": 0.5238},
                        ],
                    }
                ],
            },
            {
                "id": "game2",
                "home_team": "Miami Heat",
                "away_team": "Atlanta Hawks",
                "commence_time": "2026-04-03T23:30:00Z",
                "consensus": {
                    "Miami Heat": 0.55,
                    "Atlanta Hawks": 0.45,
                },
                "bookmakers": [
                    {
                        "name": "FanDuel",
                        "h2h": [
                            {"name": "Miami Heat", "price": -130, "implied_prob": 0.5652},
                            {"name": "Atlanta Hawks", "price": 110, "implied_prob": 0.4762},
                        ],
                        "spreads": [],
                        "totals": [],
                    }
                ],
            },
        ],
    }


def _mock_player_stats(player, stat, threshold, sport):
    """Mock player stats function."""
    return {
        "probability": 0.78,
        "mean": 25.3,
        "std": 6.1,
        "sample_size": 10,
        "confidence": 0.85,
        "source": "ESPN player stats (mock)",
        "warnings": [],
    }


class TestPriceParlay:
    def test_team_win_uses_consensus(self):
        legs = parse_parlay_title("yes Boston")
        result = price_parlay(legs, _make_odds_data())
        assert result["legs_priced"] == 1
        assert result["legs_unpriced"] == 0
        assert abs(result["legs"][0]["probability"] - 0.91) < 0.01

    def test_no_side_inverts(self):
        legs = parse_parlay_title("no Boston")
        result = price_parlay(legs, _make_odds_data())
        # no Boston = 1 - 0.91 = 0.09
        assert abs(result["legs"][0]["probability"] - 0.09) < 0.01

    def test_player_prop_calls_fn(self):
        legs = parse_parlay_title("yes Jalen Brunson: 20+")
        result = price_parlay(legs, _make_odds_data(), _mock_player_stats)
        assert result["legs_priced"] == 1
        assert abs(result["legs"][0]["probability"] - 0.78) < 0.01

    def test_unknown_leg_fifty_percent(self):
        legs = parse_parlay_title("something random")
        result = price_parlay(legs, _make_odds_data())
        assert result["legs_unpriced"] == 1
        assert abs(result["legs"][0]["probability"] - 0.50) < 0.01

    def test_multi_leg_probability(self):
        legs = parse_parlay_title("yes Boston, yes Miami")
        result = price_parlay(legs, _make_odds_data())
        # 0.91 * 0.55 = 0.5005
        assert abs(result["raw_probability"] - 0.5005) < 0.01

    def test_correlation_same_game(self):
        """Two legs from the same game should get a correlation discount."""
        legs = parse_parlay_title("yes Boston, yes Over 220.5 points scored")
        odds = _make_odds_data()
        result = price_parlay(legs, odds)
        # Both are in game1 (Boston vs Charlotte), should apply 0.95 discount
        assert result["correlation_adjusted"] < result["raw_probability"]

    def test_no_correlation_cross_game(self):
        """Legs from different games should not get a correlation discount."""
        legs = parse_parlay_title("yes Boston, yes Miami")
        result = price_parlay(legs, _make_odds_data())
        # Different games — no correlation discount
        assert result["correlation_adjusted"] == result["raw_probability"]

    def test_confidence_degrades_with_unpriced(self):
        legs = parse_parlay_title("yes Boston, something weird")
        result = price_parlay(legs, _make_odds_data())
        assert result["confidence"] < 1.0

    def test_no_odds_data(self):
        legs = parse_parlay_title("yes Boston")
        result = price_parlay(legs, {})
        assert result["legs_unpriced"] == 1
        assert result["legs"][0]["probability"] == 0.50

    def test_return_structure(self):
        legs = parse_parlay_title("yes Boston, yes Charlotte")
        result = price_parlay(legs, _make_odds_data())
        assert "legs" in result
        assert "legs_priced" in result
        assert "legs_unpriced" in result
        assert "raw_probability" in result
        assert "correlation_adjusted" in result
        assert "confidence" in result
        assert "warnings" in result
        assert "same_game_groups" in result


class TestMLBParsing:
    def test_total_runs(self):
        leg = _parse_segment("yes Over 5.5 runs scored")
        assert leg.leg_type == LegType.TOTAL_POINTS
        assert leg.threshold == 5.5
        assert leg.direction == "over"
        assert leg.sport == "mlb"

    def test_total_runs_no_scored(self):
        leg = _parse_segment("no Over 8.5 runs")
        assert leg.leg_type == LegType.TOTAL_POINTS
        assert leg.side == "no"
        assert leg.threshold == 8.5
        assert leg.sport == "mlb"

    def test_mlb_spread(self):
        leg = _parse_segment("yes Kansas City wins by over 1.5 runs")
        assert leg.leg_type == LegType.SPREAD
        assert leg.team == "Kansas City Royals"
        assert leg.threshold == 1.5

    def test_mlb_team_win_with_hint(self):
        # When "runs" appears in the full title, sport_hint = "mlb"
        legs = parse_parlay_title("yes Seattle,yes Over 5.5 runs scored")
        assert legs[0].team == "Seattle Mariners"
        assert legs[0].sport == "mlb"

    def test_total_goals(self):
        leg = _parse_segment("yes Over 3.5 goals scored")
        assert leg.leg_type == LegType.TOTAL_POINTS
        assert leg.threshold == 3.5
        assert leg.sport == "nhl"

    def test_goals_spread(self):
        leg = _parse_segment("no Boston wins by over 2.5 goals")
        assert leg.leg_type == LegType.SPREAD
        assert leg.side == "no"
        assert leg.threshold == 2.5


class TestToDict:
    def test_leg_to_dict(self):
        leg = ParlayLeg(
            raw="yes Boston",
            leg_type=LegType.TEAM_WIN,
            side="yes",
            team="Boston Celtics",
        )
        d = leg.to_dict()
        assert d["type"] == "team_win"
        assert d["team"] == "Boston Celtics"
        assert d["raw"] == "yes Boston"
