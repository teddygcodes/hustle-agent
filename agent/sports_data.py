"""
Hustle Agent — Sports Data Client

Fetches real-time odds, scores, and game data from The Odds API.
Used by the agent to source quantitative probabilities for Kalshi sports markets.

Requires a free API key from https://the-odds-api.com (500 requests/month free).
Store the key in config/sports_data.json as {"api_key": "..."}.
"""

import json
import ssl
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

import certifi

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "sports_data.json"

THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Sport keys The Odds API uses
SPORT_MAP = {
    "nba": "basketball_nba",
    "mlb": "baseball_mlb",
    "nfl": "americanfootball_nfl",
    "nhl": "icehockey_nhl",
    "ncaab": "basketball_ncaab",
    "ncaaf": "americanfootball_ncaaf",
    "mls": "soccer_usa_mls",
    "epl": "soccer_epl",
    "soccer": "soccer_epl",
    "tennis": "tennis_atp_french_open",
    "ufc": "mma_mixed_martial_arts",
    "ipl":  "cricket_ipl",
}

# Bookmakers to include — Pinnacle leads (sharpest lines, ~2% vig vs ~5-8% for US books)
# followed by major US books for broader coverage and consensus width calculation
DEFAULT_BOOKMAKERS = "pinnacle,fanduel,draftkings,betmgm,caesars,bovada"


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def _api_get(endpoint: str, params: dict) -> dict:
    """Make a GET request to The Odds API."""
    config = _load_config()
    api_key = config.get("api_key", "")
    if not api_key:
        return {"error": "Sports data API key not configured. Ask Tyler to add a free key from the-odds-api.com to config/sports_data.json"}

    params["apiKey"] = api_key
    query = urllib.parse.urlencode(params)
    url = f"{THE_ODDS_API_BASE}/{endpoint}?{query}"

    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(url, headers={"User-Agent": "HustleAgent/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return {"data": data, "remaining_requests": resp.headers.get("x-requests-remaining", "?")}
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"error": str(e)}


def _american_to_implied_prob(odds: int) -> float:
    """Convert American odds to implied probability."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def _decimal_to_implied_prob(odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if odds <= 0:
        return 0.0
    return 1.0 / odds


def get_available_sports() -> dict:
    """List all sports currently available on The Odds API."""
    result = _api_get("sports", {})
    if "error" in result:
        return result

    sports = []
    for s in result["data"]:
        if s.get("active"):
            sports.append({
                "key": s["key"],
                "title": s["title"],
                "description": s.get("description", ""),
                "has_outrights": s.get("has_outrights", False),
            })
    return {"sports": sports, "remaining_requests": result.get("remaining_requests")}


def get_odds(sport: str, markets: str = "h2h,spreads,totals", regions: str = "us") -> dict:
    """
    Fetch current odds for a sport.

    Args:
        sport: Sport key (nba, mlb, nfl, nhl, ncaab, soccer, etc.)
        markets: Comma-separated market types (h2h, spreads, totals)
        regions: Region for bookmaker selection (us, eu, uk, au)
    """
    sport_key = SPORT_MAP.get(sport.lower(), sport.lower())

    result = _api_get(f"sports/{sport_key}/odds", {
        "regions": regions,
        "markets": markets,
        "bookmakers": DEFAULT_BOOKMAKERS,
        "oddsFormat": "american",
    })
    if "error" in result:
        return result

    games = []
    for game in result["data"]:
        parsed = {
            "id": game.get("id", ""),
            "home_team": game.get("home_team", ""),
            "away_team": game.get("away_team", ""),
            "commence_time": game.get("commence_time", ""),
            "bookmakers": [],
            "consensus": {},
        }

        all_h2h_probs = {}  # team -> list of implied probs

        for bm in game.get("bookmakers", []):
            bm_data = {"name": bm.get("title", bm.get("key", ""))}
            for market in bm.get("markets", []):
                mkey = market.get("key", "")
                outcomes = []
                for o in market.get("outcomes", []):
                    price = o.get("price", 0)
                    implied = _american_to_implied_prob(price) if isinstance(price, int) else 0
                    entry = {
                        "name": o.get("name", ""),
                        "price": price,
                        "implied_prob": round(implied, 4),
                    }
                    if o.get("point") is not None:
                        entry["point"] = o["point"]
                    outcomes.append(entry)

                    # Accumulate h2h probs for consensus
                    if mkey == "h2h":
                        name = o.get("name", "")
                        all_h2h_probs.setdefault(name, []).append(implied)

                bm_data[mkey] = outcomes
            parsed["bookmakers"].append(bm_data)

        # Compute consensus (average implied probability across books)
        if all_h2h_probs:
            consensus = {}
            consensus_std = {}
            for team, probs in all_h2h_probs.items():
                avg = sum(probs) / len(probs)
                consensus[team] = round(avg, 4)
                # Std dev across books — high value means books disagree (uncertain/stale)
                if len(probs) >= 2:
                    variance = sum((p - avg) ** 2 for p in probs) / len(probs)
                    consensus_std[team] = round(variance ** 0.5, 4)
                else:
                    consensus_std[team] = 0.0
            # Normalize to remove vig
            total = sum(consensus.values())
            if total > 0:
                consensus = {t: round(p / total, 4) for t, p in consensus.items()}
            parsed["consensus"] = consensus
            parsed["consensus_std"] = consensus_std  # Fix #4: book disagreement signal

        games.append(parsed)

    return {
        "sport": sport_key,
        "game_count": len(games),
        "games": games,
        "remaining_requests": result.get("remaining_requests"),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def get_scores(sport: str, days_from: int = 1) -> dict:
    """
    Fetch recent scores/results for a sport.

    Args:
        sport: Sport key
        days_from: How many days back to fetch (1-3)
    """
    sport_key = SPORT_MAP.get(sport.lower(), sport.lower())

    result = _api_get(f"sports/{sport_key}/scores", {
        "daysFrom": min(days_from, 3),
    })
    if "error" in result:
        return result

    games = []
    for game in result["data"]:
        parsed = {
            "id": game.get("id", ""),
            "home_team": game.get("home_team", ""),
            "away_team": game.get("away_team", ""),
            "commence_time": game.get("commence_time", ""),
            "completed": game.get("completed", False),
            "scores": game.get("scores"),
            "last_update": game.get("last_update"),
        }
        games.append(parsed)

    return {
        "sport": sport_key,
        "game_count": len(games),
        "games": games,
        "remaining_requests": result.get("remaining_requests"),
    }


def get_event_odds(sport: str, event_id: str, markets: str = "h2h,spreads,totals") -> dict:
    """
    Fetch odds for a specific game/event by event ID.

    Args:
        sport: Sport key
        event_id: The Odds API event ID (from get_odds results)
        markets: Market types to fetch
    """
    sport_key = SPORT_MAP.get(sport.lower(), sport.lower())

    result = _api_get(f"sports/{sport_key}/events/{event_id}/odds", {
        "regions": "us",
        "markets": markets,
        "bookmakers": DEFAULT_BOOKMAKERS,
        "oddsFormat": "american",
    })
    if "error" in result:
        return result

    game = result["data"]
    all_h2h_probs = {}

    bookmakers = []
    for bm in game.get("bookmakers", []):
        bm_data = {"name": bm.get("title", bm.get("key", ""))}
        for market in bm.get("markets", []):
            mkey = market.get("key", "")
            outcomes = []
            for o in market.get("outcomes", []):
                price = o.get("price", 0)
                implied = _american_to_implied_prob(price) if isinstance(price, int) else 0
                entry = {
                    "name": o.get("name", ""),
                    "price": price,
                    "implied_prob": round(implied, 4),
                }
                if o.get("point") is not None:
                    entry["point"] = o["point"]
                outcomes.append(entry)

                if mkey == "h2h":
                    name = o.get("name", "")
                    all_h2h_probs.setdefault(name, []).append(implied)

            bm_data[mkey] = outcomes
        bookmakers.append(bm_data)

    consensus = {}
    if all_h2h_probs:
        for team, probs in all_h2h_probs.items():
            avg = sum(probs) / len(probs)
            consensus[team] = round(avg, 4)
        total = sum(consensus.values())
        if total > 0:
            consensus = {t: round(p / total, 4) for t, p in consensus.items()}

    return {
        "home_team": game.get("home_team", ""),
        "away_team": game.get("away_team", ""),
        "commence_time": game.get("commence_time", ""),
        "bookmakers": bookmakers,
        "consensus": consensus,
        "remaining_requests": result.get("remaining_requests"),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
