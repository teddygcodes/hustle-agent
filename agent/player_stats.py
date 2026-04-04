"""
Hustle Agent — Player Stats Client

Fetches player season/recent stats from ESPN's free API and estimates
player prop probabilities using a normal distribution model.

Cache: state/player_cache.json (24h TTL, max 200 entries, LRU eviction).
"""
from __future__ import annotations

import json
import math
import ssl
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

import certifi

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
CACHE_FILE = STATE_DIR / "player_cache.json"

CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
CACHE_MAX_ENTRIES = 200

ESPN_BASE = "https://site.api.espn.com"

SPORT_ESPN_MAP = {
    "nba": ("basketball", "nba"),
    "mlb": ("baseball", "mlb"),
    "nfl": ("football", "nfl"),
    "nhl": ("hockey", "nhl"),
}


# ---------------------------------------------------------------------------
# HTTP helper (mirrors agent/sports_data.py pattern)
# ---------------------------------------------------------------------------

def _espn_get(url: str, timeout: int = 10) -> dict:
    """GET request to ESPN API."""
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(url, headers={"User-Agent": "HustleAgent/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # LRU eviction: drop oldest entries if over max
    if len(cache) > CACHE_MAX_ENTRIES:
        items = sorted(cache.items(), key=lambda kv: kv[1].get("cached_at", 0))
        cache = dict(items[-CACHE_MAX_ENTRIES:])
    tmp = CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    tmp.rename(CACHE_FILE)


def _cache_get(key: str) -> dict | None:
    cache = _load_cache()
    entry = cache.get(key)
    if entry is None:
        return None
    if time.time() - entry.get("cached_at", 0) > CACHE_TTL_SECONDS:
        return None  # expired
    return entry.get("data")


def _cache_set(key: str, data: dict) -> None:
    cache = _load_cache()
    cache[key] = {"data": data, "cached_at": time.time()}
    _save_cache(cache)


def _normalize_name(name: str) -> str:
    """Normalize a player name for cache keys and matching."""
    return name.strip().lower().replace(".", "").replace("'", "").replace("-", " ")


# ---------------------------------------------------------------------------
# Normal distribution helpers (no scipy needed)
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    """Standard normal CDF using math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _prob_over_threshold(mean: float, std: float, threshold: float) -> float:
    """P(X > threshold) assuming normal distribution."""
    if std <= 0:
        return 1.0 if mean >= threshold else 0.0
    z = (threshold - mean) / std
    return 1.0 - _normal_cdf(z)


# ---------------------------------------------------------------------------
# ESPN API functions
# ---------------------------------------------------------------------------

def find_player_id(player_name: str, sport: str = "nba") -> dict | None:
    """
    Search ESPN for a player and return their ID, full name, and team.
    Returns None if not found.
    """
    cache_key = f"player_id_{_normalize_name(player_name)}_{sport}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    query = urllib.parse.quote(player_name)
    sport_cat, league = SPORT_ESPN_MAP.get(sport, ("basketball", "nba"))

    # Try the search endpoint
    url = f"{ESPN_BASE}/apis/common/v3/search?query={query}&type=player&sport={sport_cat}&limit=5"
    data = _espn_get(url)
    if "error" in data:
        return None

    # Parse search results
    items = data.get("items", [])
    if not items:
        # Fallback: try the athletes endpoint
        url2 = f"{ESPN_BASE}/apis/site/v2/sports/{sport_cat}/{league}/athletes?search={query}"
        data2 = _espn_get(url2)
        if "error" not in data2:
            athletes = data2.get("items", data2.get("athletes", []))
            if athletes:
                athlete = athletes[0]
                result = {
                    "id": str(athlete.get("id", "")),
                    "name": athlete.get("displayName", athlete.get("fullName", player_name)),
                    "team": athlete.get("team", {}).get("displayName", ""),
                }
                _cache_set(cache_key, result)
                return result
        return None

    # Use first search result
    item = items[0]
    result = {
        "id": str(item.get("id", item.get("$ref", "").split("/")[-1])),
        "name": item.get("displayName", item.get("name", player_name)),
        "team": "",
    }

    # Try to extract team from the result
    if "team" in item:
        result["team"] = item["team"].get("displayName", "")

    _cache_set(cache_key, result)
    return result


def get_player_season_stats(player_name: str, sport: str = "nba") -> dict | None:
    """
    Fetch season averages for a player.
    Returns dict with per-game averages or None on failure.
    """
    cache_key = f"season_{_normalize_name(player_name)}_{sport}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    player = find_player_id(player_name, sport)
    if not player or not player.get("id"):
        return None

    sport_cat, league = SPORT_ESPN_MAP.get(sport, ("basketball", "nba"))
    pid = player["id"]

    # Try the statistics endpoint
    url = f"{ESPN_BASE}/apis/common/v3/sports/{sport_cat}/{league}/athletes/{pid}/statistics"
    data = _espn_get(url)

    result = {"player": player["name"], "team": player.get("team", ""), "stats": {}}

    if "error" not in data:
        # Parse statistics response
        splits = data.get("splitCategories", data.get("splits", []))
        if isinstance(splits, list):
            for split in splits:
                categories = split.get("splits", split.get("categories", []))
                if isinstance(categories, list):
                    for cat in categories:
                        stats = cat.get("stats", cat.get("averages", []))
                        labels = cat.get("labels", cat.get("names", []))
                        if labels and stats:
                            for label, value in zip(labels, stats):
                                try:
                                    result["stats"][label.lower()] = float(value)
                                except (ValueError, TypeError):
                                    pass

    # Fallback: try the summary endpoint for basic stats
    if not result["stats"]:
        url2 = f"{ESPN_BASE}/apis/site/v2/sports/{sport_cat}/{league}/athletes/{pid}"
        data2 = _espn_get(url2)
        if "error" not in data2:
            athlete = data2.get("athlete", data2)
            stats_list = athlete.get("statistics", [])
            if stats_list:
                for stat_group in stats_list:
                    labels = stat_group.get("labels", [])
                    stat_values = stat_group.get("splits", [{}])
                    if stat_values:
                        values = stat_values[0].get("stats", [])
                        for label, val in zip(labels, values):
                            try:
                                result["stats"][label.lower()] = float(val)
                            except (ValueError, TypeError):
                                pass

    if result["stats"]:
        _cache_set(cache_key, result)
        return result
    return None


def get_player_recent_stats(
    player_name: str, sport: str = "nba", n_games: int = 10
) -> dict | None:
    """
    Fetch last N game logs for a player.
    Returns dict with game-by-game stats or None on failure.
    """
    cache_key = f"recent_{_normalize_name(player_name)}_{sport}_{n_games}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    player = find_player_id(player_name, sport)
    if not player or not player.get("id"):
        return None

    sport_cat, league = SPORT_ESPN_MAP.get(sport, ("basketball", "nba"))
    pid = player["id"]

    url = f"{ESPN_BASE}/apis/common/v3/sports/{sport_cat}/{league}/athletes/{pid}/gamelog"
    data = _espn_get(url)

    if "error" in data:
        return None

    games = []
    # Parse gamelog response
    categories = data.get("categories", data.get("seasonTypes", []))
    for cat in categories if isinstance(categories, list) else []:
        events = cat.get("events", cat.get("games", []))
        labels = cat.get("labels", cat.get("names", []))

        for event in events if isinstance(events, list) else []:
            stats = event.get("stats", [])
            if labels and stats:
                game_stats = {}
                for label, val in zip(labels, stats):
                    try:
                        game_stats[label.lower()] = float(val)
                    except (ValueError, TypeError):
                        game_stats[label.lower()] = val
                if game_stats:
                    games.append(game_stats)

    # Take only last N games
    games = games[:n_games]

    if not games:
        return None

    result = {
        "player": player["name"],
        "team": player.get("team", ""),
        "games": games,
        "game_count": len(games),
    }
    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Stat name mapping
# ---------------------------------------------------------------------------

STAT_ALIASES = {
    "points": ["pts", "points", "ppg"],
    "rebounds": ["reb", "rebounds", "rpg", "trb"],
    "assists": ["ast", "assists", "apg"],
    "steals": ["stl", "steals", "spg"],
    "blocks": ["blk", "blocks", "bpg"],
    "threes": ["3pm", "3pt", "threes", "fg3"],
}


def _find_stat_value(stats: dict, stat_name: str) -> float | None:
    """Find a stat value by checking aliases."""
    stat_lower = stat_name.lower()
    # Direct match
    if stat_lower in stats:
        return stats[stat_lower]
    # Check aliases
    for canonical, aliases in STAT_ALIASES.items():
        if stat_lower in aliases or stat_lower == canonical:
            for alias in aliases + [canonical]:
                if alias in stats:
                    return stats[alias]
    return None


# ---------------------------------------------------------------------------
# Main probability estimation
# ---------------------------------------------------------------------------

def estimate_player_prop_probability(
    player_name: str,
    stat: str = "points",
    threshold: float = 20.0,
    sport: str = "nba",
) -> dict:
    """
    Estimate the probability a player exceeds a threshold for a given stat.

    Uses season average + recent form with a normal distribution model.
    Returns: {probability, mean, std, sample_size, confidence, source, warnings}
    """
    warnings = []

    # Get season stats
    season = get_player_season_stats(player_name, sport)
    season_avg = None
    if season and season.get("stats"):
        season_avg = _find_stat_value(season["stats"], stat)

    # Get recent games
    recent = get_player_recent_stats(player_name, sport, n_games=10)
    recent_values = []
    if recent and recent.get("games"):
        for game in recent["games"]:
            val = _find_stat_value(game, stat)
            if val is not None:
                recent_values.append(val)

    # Compute mean and std
    if recent_values and season_avg is not None:
        # Weighted: 60% recent, 40% season
        recent_mean = sum(recent_values) / len(recent_values)
        mean = 0.6 * recent_mean + 0.4 * season_avg
        source = f"ESPN season avg ({season_avg:.1f}) + recent {len(recent_values)} games ({recent_mean:.1f})"
    elif recent_values:
        mean = sum(recent_values) / len(recent_values)
        source = f"ESPN recent {len(recent_values)} games (no season avg)"
        warnings.append("Season average unavailable, using recent games only")
    elif season_avg is not None:
        mean = season_avg
        source = "ESPN season average only (no recent game data)"
        warnings.append("Recent game data unavailable, using season average only")
    else:
        # No data at all
        return {
            "probability": 0.50,
            "mean": None,
            "std": None,
            "sample_size": 0,
            "confidence": 0.3,
            "source": "No ESPN data available",
            "warnings": [f"No stats found for {player_name} ({stat})"],
        }

    # Compute std from recent values if available, else estimate
    if len(recent_values) >= 3:
        variance = sum((v - mean) ** 2 for v in recent_values) / len(recent_values)
        std = math.sqrt(variance)
        # Floor std at 15% of mean to avoid unrealistically tight distributions
        std = max(std, mean * 0.15) if mean > 0 else max(std, 1.0)
    else:
        # Estimate std as 25% of mean (typical for NBA player stats)
        std = max(mean * 0.25, 1.0)
        warnings.append(f"Std estimated (insufficient game data), using {std:.1f}")

    probability = _prob_over_threshold(mean, std, threshold)
    sample_size = len(recent_values) if recent_values else 0

    # Games over threshold (for reference)
    games_over = sum(1 for v in recent_values if v >= threshold) if recent_values else 0

    # Confidence based on sample size
    if sample_size >= 10:
        confidence = 0.85
    elif sample_size >= 5:
        confidence = 0.7
    else:
        confidence = 0.5

    return {
        "probability": round(probability, 4),
        "mean": round(mean, 1),
        "std": round(std, 1),
        "sample_size": sample_size,
        "games_over": games_over,
        "games_total": sample_size,
        "confidence": confidence,
        "source": source,
        "warnings": warnings,
    }
