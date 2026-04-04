"""
Simple Elo rating model for NBA and MLB.

Seeded from current ESPN standings on first run (win% → Elo).
Updated after each resolved game via update_elo().
Query via get_elo_prob(home_team, away_team, sport) → float (home win prob).

Adjustments applied at query time (not stored in ratings):
  - Home court: +3.5% NBA, +4% MLB (expressed as Elo point bonus)
  - Back-to-back: -4% if team played yesterday (~40 Elo pts)
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
_ELO_FILE  = os.path.join(_STATE_DIR, "elo_ratings.json")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

ESPN_STANDINGS_PATHS: dict[str, str] = {
    "nba": "basketball/nba/standings",
    "mlb": "baseball/mlb/standings",
}
# ESPN v2 standings API returns different stat keys than the site API
ESPN_STANDINGS_V2_BASE = "https://site.api.espn.com/apis/v2/sports"
_WIN_PCT_STAT_NAMES = frozenset({
    "winPercent", "winPct", "pct",
    "leagueWinPercent", "divisionWinPercent",  # v2 API
})

# Home-field advantage expressed as Elo bonus added to home team
_HOME_BONUS: dict[str, float] = {
    "nba": 87.0,   # ≈ +3.5% win probability
    "mlb": 100.0,  # ≈ +4.0% win probability
}

_B2B_PENALTY = 40.0   # Elo pts subtracted from team on back-to-back
_DEFAULT_ELO = 1500.0
_SEED_SCALE  = 600.0  # elo = 1500 + (win_pct - 0.5) * 600

# 6-hour cache for seeded data (standings don't change mid-game)
_SEED_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_SEED_CACHE_TTL = 21600


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: int = 8) -> dict | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [Elo] HTTP error ({url[:80]}): {e}")
        return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_ELO_MAX_AGE_DAYS = 7   # Re-seed from ESPN if file is older than this

def _load_ratings(sport: str) -> dict[str, float]:
    """
    Load Elo ratings from disk for a sport.

    Returns {} if file is missing, corrupt, or older than _ELO_MAX_AGE_DAYS
    (forcing a fresh ESPN seed so stale win% data is never used silently).
    """
    try:
        mtime = os.path.getmtime(_ELO_FILE)
        age_days = (time.time() - mtime) / 86400
        if age_days > _ELO_MAX_AGE_DAYS:
            print(f"  [Elo] Ratings file is {age_days:.0f} days old — forcing re-seed from ESPN")
            return {}
        with open(_ELO_FILE) as f:
            all_ratings: dict = json.load(f)
        return {k: float(v) for k, v in all_ratings.get(sport, {}).items()}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_ratings(sport: str, ratings: dict[str, float]) -> None:
    """Atomically persist updated ratings for one sport."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    try:
        with open(_ELO_FILE) as f:
            all_ratings: dict = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        all_ratings = {}

    all_ratings[sport] = {k: round(v, 2) for k, v in ratings.items()}

    tmp = _ELO_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(all_ratings, f, indent=2)
    os.replace(tmp, _ELO_FILE)


# ---------------------------------------------------------------------------
# Seeding from ESPN standings
# ---------------------------------------------------------------------------

def _seed_from_espn(sport: str) -> dict[str, float]:
    """
    Seed Elo ratings from ESPN current standings win percentages.
    Formula: elo = 1500 + (win_pct - 0.5) * 600
    Returns {team_display_name: elo}.
    """
    cached = _SEED_CACHE.get(sport)
    if cached and (time.monotonic() - cached[0]) < _SEED_CACHE_TTL:
        return cached[1]

    path = ESPN_STANDINGS_PATHS.get(sport)
    if not path:
        return {}

    # Try the v2 API first (more reliably returns standings data)
    url = f"{ESPN_STANDINGS_V2_BASE}/{path}"
    data = _get_json(url)
    if not data:
        return {}

    ratings: dict[str, float] = {}

    def _extract_entries(node: dict) -> list:
        """Recursively collect all standings entries from nested children."""
        results = []
        standings = node.get("standings", {})
        if standings:
            results.extend(standings.get("entries", []))
        for child in node.get("children", []):
            results.extend(_extract_entries(child))
        return results

    entries = _extract_entries(data)

    for entry in entries:
        team = entry.get("team", {})
        name = team.get("displayName", "")
        if not name:
            continue
        win_pct = None
        wins = losses = None
        for stat in entry.get("stats", []):
            stat_name = stat.get("name", "")
            if stat_name in _WIN_PCT_STAT_NAMES and win_pct is None:
                try:
                    win_pct = float(stat["value"])
                except (KeyError, TypeError, ValueError):
                    pass
            elif stat_name == "wins":
                try:
                    wins = float(stat["value"])
                except (TypeError, ValueError):
                    pass
            elif stat_name == "losses":
                try:
                    losses = float(stat["value"])
                except (TypeError, ValueError):
                    pass
        if win_pct is None and wins is not None and losses is not None and (wins + losses) > 0:
            win_pct = wins / (wins + losses)

        if win_pct is not None:
            ratings[name] = round(_DEFAULT_ELO + (win_pct - 0.5) * _SEED_SCALE, 2)

    if ratings:
        print(f"  [Elo] Seeded {len(ratings)} {sport.upper()} teams from ESPN standings")
        _SEED_CACHE[sport] = (time.monotonic(), ratings)
    else:
        print(f"  [Elo] WARNING: could not seed {sport.upper()} Elo from ESPN ({url})")

    return ratings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_elo_prob(
    home: str,
    away: str,
    sport: str,
    home_b2b: bool = False,
    away_b2b: bool = False,
) -> float | None:
    """
    Return P(home wins) using Elo ratings.

    Loads from disk; seeds from ESPN if ratings are missing for either team.
    Returns None if either team is not found after seeding.
    """
    ratings = _load_ratings(sport)

    # If either team is missing, try to seed from ESPN
    if home not in ratings or away not in ratings:
        seeded = _seed_from_espn(sport)
        if seeded:
            # Merge — disk ratings take priority over freshly seeded ones
            merged = {**seeded, **ratings}
            _save_ratings(sport, merged)
            ratings = merged

    if home not in ratings or away not in ratings:
        return None

    home_elo = ratings[home]
    away_elo = ratings[away]

    # Apply home-field bonus
    home_bonus = _HOME_BONUS.get(sport, 75.0)
    effective_home_elo = home_elo + home_bonus

    # Apply back-to-back penalties
    if home_b2b:
        effective_home_elo -= _B2B_PENALTY
    if away_b2b:
        away_elo -= _B2B_PENALTY

    prob = 1.0 / (1.0 + 10.0 ** ((away_elo - effective_home_elo) / 400.0))
    return round(prob, 4)


def update_elo(winner: str, loser: str, sport: str, k: int = 20) -> None:
    """
    Update Elo ratings after a resolved game.

    Args:
        winner: Display name of the winning team
        loser:  Display name of the losing team
        sport:  "nba" or "mlb"
        k:      K-factor (default 20 — moderate update rate)
    """
    if sport not in ESPN_STANDINGS_PATHS:
        return

    ratings = _load_ratings(sport)

    # Seed if we don't have either team yet
    if winner not in ratings or loser not in ratings:
        seeded = _seed_from_espn(sport)
        if seeded:
            ratings = {**seeded, **ratings}

    w_elo = ratings.get(winner, _DEFAULT_ELO)
    l_elo = ratings.get(loser,  _DEFAULT_ELO)

    expected_w = 1.0 / (1.0 + 10.0 ** ((l_elo - w_elo) / 400.0))

    ratings[winner] = round(w_elo + k * (1.0 - expected_w), 2)
    ratings[loser]  = round(l_elo + k * (0.0 - (1.0 - expected_w)), 2)

    _save_ratings(sport, ratings)
    print(f"  [Elo] Updated: {winner} {ratings[winner]:.0f} / {loser} {ratings[loser]:.0f}")


def get_all_ratings(sport: str) -> dict[str, float]:
    """
    Return all stored Elo ratings for a sport.
    Seeds from ESPN if the file is empty or missing.
    """
    ratings = _load_ratings(sport)
    if not ratings:
        seeded = _seed_from_espn(sport)
        if seeded:
            _save_ratings(sport, seeded)
            ratings = seeded
    return dict(sorted(ratings.items(), key=lambda x: x[1], reverse=True))
