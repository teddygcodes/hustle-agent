"""
ESPN Injury Feed — pre-trade injury check.

Fetches injury reports from ESPN's free public API before alerting on
any series_game_edge opportunity. If a player on the opportunity's team
is listed as OUT or on IR, the opportunity is flagged STALE and dropped
before it reaches Telegram.

Flow:
  1. _get_team_map(sport)         → {canonical_name_lower: espn_team_id}  (24h cache)
  2. _get_team_injuries(sport, id) → [{name, position, status, description}]  (1h cache)
  3. check_game_injuries(canonical_team, sport) → {stale, warnings, checked}

Fail-open design: if ESPN is unreachable, checked=False and stale=False,
so the opportunity is NOT suppressed. Better to show a possibly-stale edge
than to silently block trades when ESPN is down.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.request
from datetime import datetime, date, timedelta

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

ESPN_SPORT_PATHS: dict[str, str] = {
    "nba":   "basketball/nba",
    "mlb":   "baseball/mlb",
    "nhl":   "hockey/nhl",
    "ncaab": "basketball/mens-college-basketball",
}

# module-level caches: sport → (monotonic_timestamp, data)
_TEAM_MAP_CACHE:    dict[str, tuple[float, dict[str, str]]]         = {}
_INJURY_CACHE:      dict[str, tuple[float, list[dict]]]             = {}
_ROSTER_MPG_CACHE:  dict[str, tuple[float, dict[str, float | None]]] = {}
_SCHEDULE_CACHE:    dict[str, tuple[float, list[dict]]]             = {}

_TEAM_MAP_TTL = 86400   # 24 h — ESPN team IDs never change
_INJURY_TTL   = 3600    # 1 h  — injury reports update slowly
_ROSTER_TTL   = 86400   # 24 h — roster/minute data changes rarely mid-season
_SCHEDULE_TTL = 3600    # 1 h  — schedule results update after each game

# MPG threshold: players averaging at least this many minutes are considered significant.
# MLB uses 0.0 so all non-bench players are checked via position slot instead.
STARTER_MPG_THRESHOLDS: dict[str, float] = {
    "nba":   20.0,
    "mlb":    0.0,   # position-based: starting pitchers and everyday players
    "nhl":   14.0,
    "ncaab": 20.0,
}

# Statuses that mean the player is not playing → STALE flag
_STALE_STATUSES = frozenset({
    "out", "ir", "il", "injured reserve",
    "day-to-day", "dtd",
    "il-10", "il-15", "il-60",         # MLB injured lists
    "o - season",                       # NHL season-ending
    "game time decision",               # treated as out-equivalent for safety
})

# Statuses worth showing as a warning but NOT suppressing the trade
_WARN_STATUSES = frozenset({"doubtful", "questionable", "probable"})


def _get_json(url: str, timeout: int = 8) -> dict | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [Injuries] HTTP error ({url[:80]}): {e}")
        return None


def _get_team_map(sport: str) -> dict[str, str]:
    """
    Return {lowercase_team_name: espn_team_id} for all teams in a sport.

    Fetches ESPN's /teams endpoint once and caches for 24 hours.
    Also indexes the last word (nickname) so "Warriors" matches "Golden State Warriors".
    """
    cached = _TEAM_MAP_CACHE.get(sport)
    if cached and (time.monotonic() - cached[0]) < _TEAM_MAP_TTL:
        return cached[1]

    sport_path = ESPN_SPORT_PATHS.get(sport)
    if not sport_path:
        return {}

    url = f"{ESPN_BASE}/{sport_path}/teams?limit=200"
    data = _get_json(url)
    team_map: dict[str, str] = {}

    if data:
        # ESPN response: {"sports": [{"leagues": [{"teams": [{"team": {...}}]}]}]}
        for sport_obj in data.get("sports", []):
            for league in sport_obj.get("leagues", []):
                for entry in league.get("teams", []):
                    team = entry.get("team", {})
                    name    = team.get("displayName", "")
                    team_id = team.get("id", "")
                    abbr    = team.get("abbreviation", "")
                    if name and team_id:
                        team_map[name.lower()] = team_id
                        last_word = name.split()[-1].lower()
                        if last_word not in team_map:   # don't clobber full-name keys
                            team_map[last_word] = team_id
                    if abbr and team_id:
                        team_map[abbr.lower()] = team_id

    count = sum(1 for k, v in team_map.items() if " " in k)  # only full-name keys
    if team_map:
        print(f"  [Injuries] Loaded {count} {sport.upper()} teams from ESPN")
    else:
        print(f"  [Injuries] WARNING: ESPN team map empty for {sport.upper()} ({url})")

    _TEAM_MAP_CACHE[sport] = (time.monotonic(), team_map)
    return team_map


def _get_team_injuries(sport: str, team_id: str) -> list[dict]:
    """
    Fetch current injury report for one team. Cached 1 hour.

    Returns list of {name, position, status, description}.
    """
    cache_key = f"{sport}:{team_id}"
    cached = _INJURY_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _INJURY_TTL:
        return cached[1]

    sport_path = ESPN_SPORT_PATHS.get(sport, "")
    url = f"{ESPN_BASE}/{sport_path}/teams/{team_id}/injuries"
    data = _get_json(url)

    injuries: list[dict] = []
    if data:
        for item in data.get("injuries", []):
            athlete = item.get("athlete", {})
            injuries.append({
                "name":        athlete.get("displayName", "Unknown"),
                "position":    athlete.get("position", {}).get("abbreviation", "?"),
                "status":      item.get("status", "").lower().strip(),
                "description": item.get("shortComment", ""),
            })

    _INJURY_CACHE[cache_key] = (time.monotonic(), injuries)
    return injuries


def _get_minutes_map(sport: str, team_id: str) -> dict[str, float | None]:
    """
    Fetch ESPN roster and return {player_name_lower: minutes_per_game}.

    For NBA/NHL: uses the player's season average MPG from the roster stats.
    For MLB: uses None for position players (SP pitchers and everyday players)
             and marks bench players via their roster slot.
    Falls back to {} on any error (treated as unknown → conservative).
    Cached 24 hours.
    """
    cache_key = f"{sport}:{team_id}:roster"
    cached = _ROSTER_MPG_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _ROSTER_TTL:
        return cached[1]

    sport_path = ESPN_SPORT_PATHS.get(sport, "")
    url = f"{ESPN_BASE}/{sport_path}/teams/{team_id}/roster"
    data = _get_json(url)

    mpg_map: dict[str, float | None] = {}

    if data:
        for athlete in data.get("athletes", []):
            # ESPN roster may nest players inside position groups
            if isinstance(athlete, dict) and "items" in athlete:
                items = athlete["items"]
            else:
                items = [athlete]

            for player in items:
                name = player.get("displayName") or player.get("fullName", "")
                if not name:
                    continue
                name_lower = name.lower()

                if sport in ("nba", "ncaab", "nhl"):
                    # Look for MPG in stats array
                    mpg = None
                    for stat in player.get("stats", []):
                        if stat.get("name") in ("avgMinutes", "minutesPerGame", "minutes"):
                            try:
                                mpg = float(stat["value"])
                            except (KeyError, TypeError, ValueError):
                                pass
                            break
                    mpg_map[name_lower] = mpg

                elif sport == "mlb":
                    # For MLB treat SP and RF/CF/LF/C/1B/2B/3B/SS as significant
                    # Bench (PH, PR, bench) are not significant
                    pos = (
                        player.get("position", {}).get("abbreviation", "")
                        if isinstance(player.get("position"), dict)
                        else str(player.get("position", ""))
                    ).upper()
                    bench_slots = {"BN", "PH", "PR", "RP"}  # relief and bench slots
                    mpg_map[name_lower] = None if pos not in bench_slots else 0.0

    _ROSTER_MPG_CACHE[cache_key] = (time.monotonic(), mpg_map)
    return mpg_map


def check_game_injuries(canonical_team: str, sport: str) -> dict:
    """
    Check injury status for a team before sending an alert.

    Args:
        canonical_team: Full canonical team name, e.g. "Houston Rockets"
        sport:          "nba", "mlb", "nhl", or "ncaab"

    Returns:
        {
            "stale":    bool,        # True → drop this opportunity
            "warnings": list[str],   # Human-readable injury notes
            "checked":  bool,        # False if ESPN unavailable (fail open)
        }
    """
    result: dict = {"stale": False, "warnings": [], "checked": False}

    if not canonical_team or sport not in ESPN_SPORT_PATHS:
        return result

    try:
        team_map = _get_team_map(sport)
        if not team_map:
            return result  # ESPN unavailable — fail open

        # Try full name, then nickname (last word)
        team_id = (
            team_map.get(canonical_team.lower())
            or team_map.get(canonical_team.split()[-1].lower())
        )
        if not team_id:
            print(f"  [Injuries] No ESPN ID for {canonical_team!r} in {sport.upper()} team map")
            return result  # unknown team — fail open

        result["checked"] = True
        injuries = _get_team_injuries(sport, team_id)

        # Load roster MPG map — used to filter bench players (fail-open: {} if unavailable)
        mpg_map = _get_minutes_map(sport, team_id)
        mpg_threshold = STARTER_MPG_THRESHOLDS.get(sport, 20.0)

        for inj in injuries:
            status = inj["status"]
            name   = inj["name"]
            pos    = inj["position"]
            desc   = inj.get("description", "")

            tag    = f"{name} ({pos})"
            detail = f": {desc}" if desc else ""

            # Determine if this player is significant (starter/rotation player)
            mpg = mpg_map.get(name.lower())  # None = unknown, float = minutes per game
            # MLB: mpg==0.0 means bench slot → not significant; None means starter slot
            if mpg is None:
                is_significant = True   # unknown → conservative, treat as significant
            elif sport == "mlb":
                is_significant = mpg != 0.0  # bench slots stored as 0.0
            else:
                is_significant = mpg >= mpg_threshold

            if any(s in status for s in _STALE_STATUSES):
                if is_significant:
                    mpg_note = f" ({mpg:.0f} mpg)" if mpg is not None and sport != "mlb" else ""
                    result["warnings"].append(
                        f"⚠️ OUT  {canonical_team} — {tag}{mpg_note} [{status}{detail}]"
                    )
                    result["stale"] = True
                else:
                    mpg_note = f" ({mpg:.0f} mpg)" if mpg is not None else ""
                    result["warnings"].append(
                        f"ℹ️ OUT (bench) {canonical_team} — {tag}{mpg_note} [{status}]"
                    )
                    # Bench player out — note it but do NOT suppress the trade

            elif any(s in status for s in _WARN_STATUSES) and is_significant:
                mpg_note = f" ({mpg:.0f} mpg)" if mpg is not None and sport != "mlb" else ""
                result["warnings"].append(
                    f"🟡 {status.upper()}  {canonical_team} — {tag}{mpg_note}{detail}"
                )
                # Doubtful/questionable: warn but do NOT suppress

    except Exception as e:
        print(f"  [Injuries] Error checking {canonical_team!r}: {e}")
        # fail open — better to surface a possibly-stale edge than block all trades

    return result


# ---------------------------------------------------------------------------
# Schedule helpers — back-to-back detection and last-10 record
# ---------------------------------------------------------------------------

def _get_team_schedule_events(sport: str, team_id: str) -> list[dict]:
    """Fetch ESPN schedule events for a team (1h cache). Fail-open: returns [] on error."""
    cache_key = f"{sport}:{team_id}:schedule"
    now = time.monotonic()
    if cache_key in _SCHEDULE_CACHE:
        ts, data = _SCHEDULE_CACHE[cache_key]
        if now - ts < _SCHEDULE_TTL:
            return data
    sport_path = ESPN_SPORT_PATHS.get(sport, "")
    if not sport_path:
        return []
    url = f"{ESPN_BASE}/{sport_path}/teams/{team_id}/schedule"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=4) as resp:
            data = json.loads(resp.read().decode())
        events = data.get("events", [])
        _SCHEDULE_CACHE[cache_key] = (now, events)
        return events
    except Exception:
        _SCHEDULE_CACHE[cache_key] = (now, [])  # cache empty to avoid hammering ESPN
        return []


def check_back_to_back(canonical_team: str, sport: str, game_date) -> bool:
    """
    Return True if canonical_team played a completed game the day before game_date.

    Args:
        canonical_team: e.g. "Sacramento Kings"
        sport:          "nba", "mlb", etc.
        game_date:      datetime or date object for the game being evaluated
    """
    team_map = _get_team_map(sport)
    team_id = (
        team_map.get(canonical_team.lower())
        or team_map.get(canonical_team.split()[-1].lower())
    )
    if not team_id:
        return False
    events = _get_team_schedule_events(sport, team_id)
    if hasattr(game_date, "date"):
        game_date = game_date.date()
    yesterday = game_date - timedelta(days=1)
    for event in events:
        comps = event.get("competitions", [])
        if not comps:
            continue
        if comps[0].get("status", {}).get("type", {}).get("name") != "STATUS_FINAL":
            continue
        date_str = event.get("date", "")
        try:
            event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
            if event_date == yesterday:
                return True
        except Exception:
            continue
    return False


def get_last_10(canonical_team: str, sport: str) -> str | None:
    """
    Return last-10-games record as 'W-L' string (e.g. '7-3'), or None if unavailable.

    Args:
        canonical_team: e.g. "Sacramento Kings"
        sport:          "nba", "mlb", etc.
    """
    team_map = _get_team_map(sport)
    team_id = (
        team_map.get(canonical_team.lower())
        or team_map.get(canonical_team.split()[-1].lower())
    )
    if not team_id:
        return None
    events = _get_team_schedule_events(sport, team_id)
    results: list[bool] = []
    for event in events:
        comps = event.get("competitions", [])
        if not comps:
            continue
        comp = comps[0]
        if comp.get("status", {}).get("type", {}).get("name") != "STATUS_FINAL":
            continue
        for c in comp.get("competitors", []):
            if str(c.get("team", {}).get("id", "")) == str(team_id):
                results.append(bool(c.get("winner", False)))
                break
    last10 = results[-10:]
    if not last10:
        return None
    wins = sum(last10)
    return f"{wins}-{len(last10) - wins}"
