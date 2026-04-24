"""
Glint Trading Bot — Free Odds Scraper

Priority chain (all free, no auth required):
  1. DraftKings JSON API  — real lines, no key (may need server IP to bypass WAF)
  2. Bovada JSON API      — real lines, always accessible, no key
  3. ESPN scoreboard      — scores + game status; odds often missing
  4. ESPN pickcenter      — live/final games only
  5. Odds snapshot        — last known prices for games ESPN no longer serves
  6. The Odds API         — last resort (500 req/month free tier)

Returns data in the same format as agent/sports_data.get_odds() so the
scanner needs zero restructuring.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
import sys
from datetime import datetime, timezone
from pathlib import Path

import certifi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import (
    ESPN_BASE, ESPN_SPORT_PATHS,
    BOT_STATE_FILE, ODDS_SNAPSHOTS_FILE,
    DRAFTKINGS_BASE, DRAFTKINGS_EVENT_GROUPS,
    BOVADA_BASE, BOVADA_SPORT_PATHS,
    FANDUEL_BASE, FANDUEL_AK, FANDUEL_SPORT_IDS,
    THERUNDOWN_API_KEY, THERUNDOWN_BASE, THERUNDOWN_SPORT_IDS,
    ODDS_API_MONTHLY_LIMIT,
)

logger = logging.getLogger("glint.odds_scraper")


# ---------------------------------------------------------------------------
# In-memory cache — 2 min live games, 15 min otherwise
# ---------------------------------------------------------------------------

_cache: dict[str, dict] = {}  # sport -> {"data": ..., "fetched_at": float}
CACHE_TTL_LIVE = 120      # 2 minutes during live games
CACHE_TTL_IDLE = 900      # 15 minutes otherwise


def _cache_get(sport: str) -> dict | None:
    """Return cached data if still fresh, else None."""
    entry = _cache.get(sport)
    if not entry:
        return None
    age = time.time() - entry["fetched_at"]
    ttl = CACHE_TTL_LIVE if entry.get("has_live") else CACHE_TTL_IDLE
    if age < ttl:
        return entry["data"]
    return None


def _cache_set(sport: str, data: dict, has_live: bool = False):
    _cache[sport] = {
        "data": data,
        "fetched_at": time.time(),
        "has_live": has_live,
    }


# ---------------------------------------------------------------------------
# Circuit breaker — transient failure tracking per data source
# ---------------------------------------------------------------------------

_SOURCE_FAILURES: dict[str, int] = {}    # source → consecutive failure count
_SOURCE_COOLDOWN: dict[str, float] = {}  # source → monotonic timestamp of cooldown start
_CB_THRESHOLD = 3        # consecutive failures before cooldown
_CB_COOLDOWN_SECS = 300  # 5 minutes


def _cb_record_failure(source: str) -> None:
    _SOURCE_FAILURES[source] = _SOURCE_FAILURES.get(source, 0) + 1
    if _SOURCE_FAILURES[source] >= _CB_THRESHOLD:
        _SOURCE_COOLDOWN[source] = time.monotonic()
        logger.warning("Circuit breaker OPEN: %s — pausing %ds after %d failures",
                       source, _CB_COOLDOWN_SECS, _CB_THRESHOLD)


def _cb_record_success(source: str) -> None:
    _SOURCE_FAILURES.pop(source, None)
    _SOURCE_COOLDOWN.pop(source, None)


def _cb_is_open(source: str) -> bool:
    ts = _SOURCE_COOLDOWN.get(source)
    if ts is None:
        return False
    if time.monotonic() - ts >= _CB_COOLDOWN_SECS:
        _SOURCE_COOLDOWN.pop(source, None)
        _SOURCE_FAILURES.pop(source, None)
        logger.info("Circuit breaker CLOSED: %s — resuming", source)
        return False
    return True


# ---------------------------------------------------------------------------
# Odds math
# ---------------------------------------------------------------------------

def american_to_implied(odds_str: str | int | float) -> float:
    """Convert American odds string (e.g. '+200', '-150') to implied probability."""
    try:
        cleaned = str(odds_str).replace("+", "").strip()
        # Handle "EVEN" / "PK"
        if cleaned.upper() in ("EVEN", "PK", "E"):
            return 100.0 / 200.0
        odds = int(cleaned)
    except (ValueError, TypeError):
        return 0.0
    if odds > 0:
        return 100.0 / (odds + 100.0)
    elif odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 0.0


def remove_vig(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Remove bookmaker vig to get true probabilities. Returns normalized pair."""
    total = prob_a + prob_b
    if total <= 0:
        return (0.5, 0.5)
    return (prob_a / total, prob_b / total)


# ---------------------------------------------------------------------------
# Snapshot fallback — use last known odds for games ESPN no longer serves
# ---------------------------------------------------------------------------

def _load_snapshot_odds(sport: str) -> dict[str, dict]:
    """Load saved odds from odds_snapshots.json for a sport."""
    try:
        if not ODDS_SNAPSHOTS_FILE.exists():
            return {}
        snapshot = json.loads(ODDS_SNAPSHOTS_FILE.read_text())
        for key in ("current", "previous"):
            sport_data = snapshot.get(key, {}).get(sport, {})
            games = sport_data.get("games", [])
            if games:
                return {
                    g["id"]: {"consensus": g["consensus"], "bookmakers": g["bookmakers"]}
                    for g in games
                    if g.get("id") and g.get("consensus")
                }
        return {}
    except Exception as e:
        logger.warning(f"Could not load odds snapshot: {e}")
        return {}


# ---------------------------------------------------------------------------
# DraftKings Sportsbook API (PRIMARY)
# Blocked by Akamai WAF on some residential IPs; works from cloud/VPS.
# ---------------------------------------------------------------------------

_DK_DISABLED = False  # Set True after first 403 — skip all DK calls this session


def _dk_get(url: str, timeout: int = 5) -> dict | None:
    """Fetch JSON from DraftKings public sportsbook API."""
    global _DK_DISABLED
    if _DK_DISABLED or _cb_is_open("draftkings"):
        return None
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://sportsbook.draftkings.com/",
                "Origin": "https://sportsbook.draftkings.com",
            },
        )
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            _cb_record_success("draftkings")
            return data
    except urllib.error.HTTPError as e:
        if e.code == 403:
            _DK_DISABLED = True
            logger.warning("DK 403 Forbidden — Akamai WAF blocking this IP. DraftKings disabled for this session.")
        else:
            _cb_record_failure("draftkings")
            logger.warning("DraftKings API error (%s): %s", url[:80], e)
        return None
    except Exception as e:
        _cb_record_failure("draftkings")
        logger.warning(f"DraftKings API error ({url[:80]}): {e}")
        return None


def _parse_dk_offers(event_group: dict) -> dict[int, dict]:
    """
    Walk DraftKings offerCategories and extract moneyline / spread / total
    for each eventId.

    Returns:
        {eventId: {"moneyline": [(label, oddsAmerican), ...],
                   "spread":    [(label, line_float, oddsAmerican), ...],
                   "total":     [(label, line_float, oddsAmerican), ...]}}
    """
    offers_by_event: dict[int, dict] = {}

    def _ensure(eid: int):
        if eid not in offers_by_event:
            offers_by_event[eid] = {"moneyline": [], "spread": [], "total": []}

    for cat in event_group.get("offerCategories", []):
        for sub in cat.get("offerSubcategoryDescriptors", []):
            sub_name = sub.get("name", "").lower()
            sub_cat = sub.get("offerSubcategory", {})
            # DK wraps offers in a list of lists sometimes
            raw_offers = sub_cat.get("offers", [])
            flat: list[dict] = []
            for item in raw_offers:
                if isinstance(item, list):
                    flat.extend(item)
                elif isinstance(item, dict):
                    flat.append(item)

            for offer in flat:
                eid = offer.get("eventId")
                if not eid:
                    continue
                _ensure(int(eid))
                outcomes = offer.get("outcomes", [])
                is_ml = "moneyline" in sub_name or "money line" in sub_name
                is_spread = (
                    "spread" in sub_name
                    or "point spread" in sub_name
                    or "run line" in sub_name
                    or "puck line" in sub_name
                )
                is_total = "total" in sub_name or "over/under" in sub_name

                for o in outcomes:
                    odds_str = (
                        o.get("oddsAmerican")
                        or o.get("americanOdds")
                        or ""
                    )
                    label = o.get("label", "")
                    line = float(o.get("line") or o.get("handicap") or 0)
                    if not odds_str:
                        continue
                    if is_ml:
                        offers_by_event[int(eid)]["moneyline"].append((label, str(odds_str)))
                    elif is_spread:
                        offers_by_event[int(eid)]["spread"].append((label, line, str(odds_str)))
                    elif is_total:
                        offers_by_event[int(eid)]["total"].append((label, line, str(odds_str)))

    return offers_by_event


def fetch_draftkings_odds(sport: str) -> dict:
    """
    Fetch odds from DraftKings public JSON API.

    NOTE: DraftKings uses Akamai Bot Manager. From residential IPs this
    often returns 403; from cloud/VPS IPs it typically works fine.
    The function falls back cleanly so the priority chain handles it.

    Returns data in the standard scanner format.
    """
    if _DK_DISABLED:
        return {"error": "DraftKings disabled this session (403)", "games": []}

    event_group_id = DRAFTKINGS_EVENT_GROUPS.get(sport.lower())
    if not event_group_id:
        return {"error": f"No DraftKings event group for sport: {sport}", "games": []}

    url = f"{DRAFTKINGS_BASE}/eventgroups/{event_group_id}?format=json"
    data = _dk_get(url)
    if not data:
        return {"error": "DraftKings API request failed (403/timeout)", "games": []}

    event_group = data.get("eventGroup", data)
    events_raw = event_group.get("events", [])
    logger.debug("DK %s: %d raw events", sport.upper(), len(events_raw))

    offers_by_event = _parse_dk_offers(event_group)
    logger.debug("DK %s: offer data for %d event IDs", sport.upper(), len(offers_by_event))

    games = []
    has_live = False

    for event in events_raw:
        event_id = event.get("eventId") or event.get("id")
        if not event_id:
            continue

        # Team names — DK has multiple layouts depending on sport/version
        home_team = ""
        away_team = ""
        home_obj = event.get("homeTeam") or event.get("home") or {}
        away_obj = event.get("awayTeam") or event.get("away") or {}
        if isinstance(home_obj, dict) and isinstance(away_obj, dict):
            home_team = (
                home_obj.get("name")
                or home_obj.get("teamName")
                or home_obj.get("shortName", "")
            )
            away_team = (
                away_obj.get("name")
                or away_obj.get("teamName")
                or away_obj.get("shortName", "")
            )

        # Fallback: parse "Away at Home" / "Away vs Home" from event name
        if not home_team or not away_team:
            name = event.get("name", "")
            for sep in (" at ", " vs ", " @ "):
                if sep in name:
                    parts = name.split(sep, 1)
                    away_team = parts[0].strip()
                    home_team = parts[1].strip()
                    break

        # DK v4-style flat fields
        if not home_team:
            home_team = event.get("teamName2") or event.get("teamName1", "")
        if not away_team:
            away_team = event.get("teamName1", "")

        # Status
        status_obj = event.get("eventStatus") or event.get("status") or {}
        status_desc = (status_obj.get("description") or "").lower()
        if "in progress" in status_desc or "live" in status_desc:
            status = "STATUS_IN_PROGRESS"
            has_live = True
        elif "final" in status_desc or "completed" in status_desc:
            status = "STATUS_FINAL"
        else:
            status = "STATUS_SCHEDULED"

        commence_time = event.get("startDate") or event.get("startEventDate") or ""

        # Odds
        event_offers = offers_by_event.get(int(event_id), {})
        moneyline = event_offers.get("moneyline", [])
        spreads = event_offers.get("spread", [])
        totals = event_offers.get("total", [])

        bookmakers = []
        consensus = {}

        if len(moneyline) >= 2:
            home_ml_str = None
            away_ml_str = None

            # Match by last word of team name (most distinctive part)
            home_token = home_team.split()[-1].lower() if home_team else ""
            away_token = away_team.split()[-1].lower() if away_team else ""

            for label, odds_str in moneyline:
                label_lower = label.lower()
                if home_token and home_token in label_lower:
                    home_ml_str = odds_str
                elif away_token and away_token in label_lower:
                    away_ml_str = odds_str

            # DK convention: outcomes[0] = away, outcomes[1] = home
            if not home_ml_str and len(moneyline) >= 2:
                away_ml_str = away_ml_str or moneyline[0][1]
                home_ml_str = home_ml_str or moneyline[1][1]

            if home_ml_str and away_ml_str:
                home_implied = american_to_implied(home_ml_str)
                away_implied = american_to_implied(away_ml_str)
                if home_implied > 0 and away_implied > 0:
                    home_true, away_true = remove_vig(home_implied, away_implied)
                    bookmakers.append({
                        "name": "DraftKings",
                        "h2h": [
                            {"name": home_team, "price": int(str(home_ml_str).replace("+", "")), "implied_prob": round(home_implied, 4)},
                            {"name": away_team, "price": int(str(away_ml_str).replace("+", "")), "implied_prob": round(away_implied, 4)},
                        ],
                    })
                    consensus = {
                        home_team: round(home_true, 4),
                        away_team: round(away_true, 4),
                    }

        if spreads and bookmakers:
            home_token = home_team.split()[-1].lower() if home_team else ""
            for label, line, _ in spreads:
                if home_token and home_token in label.lower():
                    bookmakers[0]["spreads_info"] = {
                        "home_line": line,
                        "away_line": -line,
                    }
                    break

        if totals and bookmakers:
            for label, line, _ in totals:
                if "over" in label.lower() and line:
                    bookmakers[0]["totals_info"] = {"over_under": line}
                    break

        games.append({
            "id": str(event_id),
            "home_team": home_team,
            "away_team": away_team,
            "commence_time": commence_time,
            "bookmakers": bookmakers,
            "consensus": consensus,
            "status": status,
        })

    games_with_odds = [g for g in games if g.get("consensus")]
    logger.info("DraftKings %s: %d games, %d with moneylines", sport.upper(), len(games), len(games_with_odds))

    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "draftkings",
        "has_live": has_live,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Bovada public JSON API (free fallback — no auth, no quota, no WAF)
# ---------------------------------------------------------------------------

def _bovada_get(url: str, timeout: int = 12) -> list | None:
    """Fetch JSON from Bovada public events API. Returns list or None."""
    if _cb_is_open("bovada"):
        return None
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            _cb_record_success("bovada")
            return data
    except Exception as e:
        _cb_record_failure("bovada")
        logger.warning(f"Bovada API error ({url[:80]}): {e}")
        return None


def fetch_bovada_odds(sport: str) -> dict:
    """
    Fetch odds from Bovada's public JSON events API.

    Bovada structure:
      [{"events": [{"competitors": [{"name": ..., "home": bool}],
                    "displayGroups": [{"description": "Game Lines",
                                       "markets": [{"description": "Moneyline",
                                                    "outcomes": [{"description": ...,
                                                                  "price": {"american": ..., "handicap": ...}}]}]}],
                    "startTime": epoch_ms, "live": bool, "status": ...}]}]
    """
    sport_path = BOVADA_SPORT_PATHS.get(sport.lower())
    if not sport_path:
        return {"error": f"No Bovada path for sport: {sport}", "games": []}

    url = f"{BOVADA_BASE}/{sport_path}"
    data = _bovada_get(url)
    if not data or not isinstance(data, list):
        return {"error": "Bovada API request failed", "games": []}

    raw_events = data[0].get("events", []) if data else []
    logger.debug("Bovada %s: %d events", sport.upper(), len(raw_events))

    games = []
    has_live = False

    for event in raw_events:
        # Skip live games — live odds on Bovada aren't as reliable
        # (they update slowly; pregame odds are accurate)
        is_live = event.get("live", False)
        if is_live:
            has_live = True

        competitors = event.get("competitors", [])
        home_team = ""
        away_team = ""
        for c in competitors:
            name = c.get("name", "")
            if c.get("home"):
                home_team = name
            else:
                away_team = name

        # Fallback: parse from description "Away @ Home"
        if not home_team or not away_team:
            desc = event.get("description", "")
            for sep in (" @ ", " vs ", " at "):
                if sep in desc:
                    parts = desc.split(sep, 1)
                    away_team = parts[0].strip()
                    home_team = parts[1].strip()
                    break

        # Start time (epoch ms → ISO)
        start_ms = event.get("startTime", 0)
        try:
            commence_time = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
        except Exception:
            commence_time = ""

        # Status
        if is_live:
            status = "STATUS_IN_PROGRESS"
        else:
            ev_status = (event.get("status") or "").upper()
            if "COMPLETE" in ev_status or "FINAL" in ev_status:
                status = "STATUS_FINAL"
            else:
                status = "STATUS_SCHEDULED"

        # Find "Game Lines" display group → markets
        game_lines_markets: list[dict] = []
        for group in event.get("displayGroups", []):
            if group.get("description", "").lower() == "game lines":
                game_lines_markets = group.get("markets", [])
                break

        bookmakers = []
        consensus = {}

        home_ml_str = None
        away_ml_str = None
        home_spread_line = None
        total_line = None

        for market in game_lines_markets:
            desc = market.get("description", "").lower()
            outcomes = market.get("outcomes", [])

            if desc == "moneyline":
                for o in outcomes:
                    o_name = o.get("description", "")
                    price = o.get("price", {})
                    american = price.get("american", "")
                    if not american:
                        continue
                    # Match by competitor name
                    if home_team and home_team.split()[-1].lower() in o_name.lower():
                        home_ml_str = str(american)
                    elif away_team and away_team.split()[-1].lower() in o_name.lower():
                        away_ml_str = str(american)
                    elif not home_ml_str and o.get("type") == "H":
                        home_ml_str = str(american)
                    elif not away_ml_str and o.get("type") == "A":
                        away_ml_str = str(american)

                # Fallback positional: odds[0]=away, odds[1]=home (typical Bovada)
                if not home_ml_str and len(outcomes) >= 2:
                    away_ml_str = away_ml_str or str(outcomes[0].get("price", {}).get("american", ""))
                    home_ml_str = home_ml_str or str(outcomes[1].get("price", {}).get("american", ""))

            elif desc == "point spread" or desc == "run line" or desc == "puck line":
                for o in outcomes:
                    price = o.get("price", {})
                    handicap = price.get("handicap")
                    o_name = o.get("description", "")
                    if handicap and home_team and home_team.split()[-1].lower() in o_name.lower():
                        home_spread_line = float(handicap)

            elif "total" in desc and "game" not in desc:
                for o in outcomes:
                    price = o.get("price", {})
                    handicap = price.get("handicap")
                    if handicap and "over" in o.get("description", "").lower():
                        total_line = float(handicap)

        if home_ml_str and away_ml_str:
            home_implied = american_to_implied(home_ml_str)
            away_implied = american_to_implied(away_ml_str)
            if home_implied > 0 and away_implied > 0:
                home_true, away_true = remove_vig(home_implied, away_implied)
                bookmaker = {
                    "name": "Bovada",
                    "h2h": [
                        {"name": home_team, "price": home_ml_str, "implied_prob": round(home_implied, 4)},
                        {"name": away_team, "price": away_ml_str, "implied_prob": round(away_implied, 4)},
                    ],
                }
                if home_spread_line is not None:
                    bookmaker["spreads_info"] = {"home_line": home_spread_line, "away_line": -home_spread_line}
                if total_line is not None:
                    bookmaker["totals_info"] = {"over_under": total_line}
                bookmakers.append(bookmaker)
                consensus = {
                    home_team: round(home_true, 4),
                    away_team: round(away_true, 4),
                }

        event_id = str(event.get("id", ""))
        games.append({
            "id": event_id,
            "home_team": home_team,
            "away_team": away_team,
            "commence_time": commence_time,
            "bookmakers": bookmakers,
            "consensus": consensus,
            "status": status,
        })

    games_with_odds = [g for g in games if g.get("consensus")]
    logger.info("Bovada %s: %d games, %d with moneylines", sport.upper(), len(games), len(games_with_odds))

    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "bovada",
        "has_live": has_live,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# FanDuel Sportsbook API (free, no auth — unofficial frontend API)
# Uses FanDuel's public frontend key. Self-disables on 403 like DraftKings.
# ---------------------------------------------------------------------------

_FD_DISABLED = False


def _fd_get(url: str, timeout: int = 8) -> dict | None:
    """Fetch JSON from FanDuel's public sportsbook frontend API."""
    global _FD_DISABLED
    if _FD_DISABLED or _cb_is_open("fanduel"):
        return None
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Referer": "https://www.fanduel.com/",
            },
        )
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            _cb_record_success("fanduel")
            return data
    except urllib.error.HTTPError as e:
        if e.code in (403, 401):
            _FD_DISABLED = True
            logger.warning("FD %d — FanDuel API blocked. Disabled for this session.", e.code)
        else:
            _cb_record_failure("fanduel")
            logger.warning("FanDuel API error (%s): %s", url[:80], e)
        return None
    except Exception as e:
        _cb_record_failure("fanduel")
        logger.warning(f"FanDuel API error ({url[:80]}): {e}")
        return None


def fetch_fanduel_odds(sport: str) -> dict:
    """
    Fetch moneylines from FanDuel's unofficial frontend API.

    Parses the content-managed-page response: events + markets dicts.
    Falls back cleanly so the priority chain moves on if parsing fails.
    """
    if _FD_DISABLED:
        return {"error": "FanDuel disabled this session", "games": []}

    sport_id = FANDUEL_SPORT_IDS.get(sport.lower())
    if not sport_id:
        return {"error": f"No FanDuel sport ID for: {sport}", "games": []}

    url = (
        f"{FANDUEL_BASE}/content-managed-page"
        f"?page=CUSTOM&customPageId={sport_id}&_ak={FANDUEL_AK}"
        f"&timezone=America%2FNew_York"
    )
    data = _fd_get(url)
    if not data:
        return {"error": "FanDuel API request failed", "games": []}

    try:
        attachments = data.get("attachments", {})
        events_raw = attachments.get("events", {})
        markets_raw = attachments.get("markets", {})

        if not events_raw:
            return {"error": "FanDuel: no events in response", "games": []}

        games = []
        has_live = False
        now_utc = datetime.now(timezone.utc)

        for event_id, event in events_raw.items():
            name = event.get("name", "")
            open_date = event.get("openDate", "")
            status_raw = (event.get("eventStatus") or event.get("status") or "").upper()

            # Parse team names from event name: "Away @ Home" or "Away v Home"
            home_team, away_team = "", ""
            for sep in (" @ ", " v ", " vs ", " at "):
                if sep in name:
                    parts = name.split(sep, 1)
                    away_team = parts[0].strip()
                    home_team = parts[1].strip()
                    break
            if not home_team:
                continue

            # Parse game time
            commence_time = ""
            if open_date:
                try:
                    dt = datetime.fromisoformat(open_date.replace("Z", "+00:00"))
                    commence_time = dt.isoformat()
                    if dt <= now_utc:
                        has_live = True
                except (ValueError, TypeError):
                    pass

            # Find MATCH_ODDS (moneyline) market for this event
            linked_ids = event.get("linkedMarketIds", [])
            home_ml_str, away_ml_str = None, None

            for mid in linked_ids:
                market = markets_raw.get(str(mid), {})
                mtype = (market.get("marketType") or "").upper()
                if mtype not in ("MATCH_ODDS", "MONEYLINE", "MATCH_WINNER"):
                    continue
                runners = market.get("runners", [])
                for runner in runners:
                    rname = runner.get("runnerName", "")
                    # Navigate FanDuel's nested odds structure
                    win_odds = runner.get("winRunnerOdds", {})
                    american_odds = (
                        win_odds.get("americanDisplayOdds", {}).get("americanOdds")
                        or win_odds.get("trueOdds", {}).get("decimalOdds", {}).get("decimalOdds")
                    )
                    if not american_odds:
                        continue
                    if home_team.split()[-1].lower() in rname.lower() or rname == home_team:
                        home_ml_str = str(american_odds)
                    elif away_team.split()[-1].lower() in rname.lower() or rname == away_team:
                        away_ml_str = str(american_odds)
                if home_ml_str and away_ml_str:
                    break

            consensus = {}
            bookmakers = []
            if home_ml_str and away_ml_str:
                home_implied = american_to_implied(home_ml_str)
                away_implied = american_to_implied(away_ml_str)
                if home_implied > 0 and away_implied > 0:
                    home_true, away_true = remove_vig(home_implied, away_implied)
                    consensus = {
                        home_team: round(home_true, 4),
                        away_team: round(away_true, 4),
                    }
                    bookmakers = [{"name": "FanDuel", "h2h": [
                        {"name": home_team, "price": home_ml_str, "implied_prob": round(home_implied, 4)},
                        {"name": away_team, "price": away_ml_str, "implied_prob": round(away_implied, 4)},
                    ]}]

            games.append({
                "id": event_id,
                "home_team": home_team,
                "away_team": away_team,
                "commence_time": commence_time,
                "bookmakers": bookmakers,
                "consensus": consensus,
                "status": "STATUS_IN_PROGRESS" if "LIVE" in status_raw else "STATUS_SCHEDULED",
            })

        games_with_odds = [g for g in games if g.get("consensus")]
        logger.info("FD %s: %d games, %d with moneylines", sport.upper(), len(games), len(games_with_odds))

        return {
            "sport": sport,
            "game_count": len(games),
            "games": games,
            "source": "fanduel",
            "has_live": has_live,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.warning(f"FanDuel parse error for {sport}: {e}")
        return {"error": f"FanDuel parse error: {e}", "games": []}


# ---------------------------------------------------------------------------
# ESPN API (scores + game status; odds often missing)
# ---------------------------------------------------------------------------

def _espn_get(url: str, timeout: int = 10) -> dict | None:
    """Fetch JSON from ESPN API."""
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(url, headers={"User-Agent": "GlintBot/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"ESPN API error ({url[:80]}): {e}")
        return None


def fetch_espn_odds(sport: str) -> dict:
    """
    Fetch game list + status from ESPN's free scoreboard API.
    Odds are often absent — use primarily for game status enrichment.
    """
    sport_path = ESPN_SPORT_PATHS.get(sport.lower())
    if not sport_path:
        return {"error": f"Unknown sport: {sport}", "games": []}

    url = f"{ESPN_BASE}/{sport_path}/scoreboard"
    data = _espn_get(url)
    if not data:
        return {"error": "ESPN API request failed", "games": []}

    games = []
    has_live = False

    events = data.get("events", [])
    if events:
        first_comp = events[0].get("competitions", [{}])[0]
        raw_odds = first_comp.get("odds", [])
        logger.debug("ESPN %s scoreboard: %d events", sport.upper(), len(events))
        if raw_odds:
            logger.debug("ESPN odds[0] keys: %s", sorted(raw_odds[0].keys()))
        else:
            logger.debug("ESPN %s no odds in first game competition", sport.upper())
    else:
        logger.debug("ESPN %s no events in scoreboard response", sport.upper())

    for event in events:
        competitions = event.get("competitions", [])
        if not competitions:
            continue
        comp = competitions[0]

        competitors = comp.get("competitors", [])
        home_team = ""
        away_team = ""
        for c in competitors:
            team = c.get("team", {})
            name = team.get("displayName", team.get("name", ""))
            if c.get("homeAway") == "home":
                home_team = name
            else:
                away_team = name

        status = comp.get("status", {}).get("type", {}).get("name", "STATUS_SCHEDULED")
        if status in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME"):
            has_live = True

        odds_list = comp.get("odds", [])
        bookmakers = []
        consensus = {}

        if odds_list:
            odds = odds_list[0]
            provider_name = odds.get("provider", {}).get("name", "DraftKings")
            home_ml = odds.get("homeTeamOdds", {}).get("moneyLine")
            away_ml = odds.get("awayTeamOdds", {}).get("moneyLine")

            if not home_ml:
                home_ml = odds.get("moneyline", {}).get("home", {}).get("close", {}).get("odds")
            if not away_ml:
                away_ml = odds.get("moneyline", {}).get("away", {}).get("close", {}).get("odds")

            if home_ml and away_ml:
                home_implied = american_to_implied(home_ml)
                away_implied = american_to_implied(away_ml)
                h2h = [
                    {"name": home_team, "price": int(str(home_ml).replace("+", "")), "implied_prob": round(home_implied, 4)},
                    {"name": away_team, "price": int(str(away_ml).replace("+", "")), "implied_prob": round(away_implied, 4)},
                ]
                bookmakers.append({"name": provider_name, "h2h": h2h})
                home_true, away_true = remove_vig(home_implied, away_implied)
                consensus = {
                    home_team: round(home_true, 4),
                    away_team: round(away_true, 4),
                }

        games.append({
            "id": event.get("id", ""),
            "home_team": home_team,
            "away_team": away_team,
            "commence_time": comp.get("date", ""),
            "bookmakers": bookmakers,
            "consensus": consensus,
            "status": status,
        })

    return {
        "sport": ESPN_SPORT_PATHS.get(sport.lower(), sport),
        "game_count": len(games),
        "games": games,
        "source": "espn",
        "has_live": has_live,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_espn_pickcenter(sport: str, event_id: str) -> dict | None:
    """Fetch moneyline from ESPN pickcenter for live/completed games."""
    sport_path = ESPN_SPORT_PATHS.get(sport.lower())
    if not sport_path:
        return None

    url = f"{ESPN_BASE}/{sport_path}/summary?event={event_id}"
    data = _espn_get(url)
    if not data:
        return None

    pickcenter = data.get("pickcenter", [])
    if not pickcenter:
        return None

    pc = pickcenter[0]
    home_ml = pc.get("homeTeamOdds", {}).get("moneyLine")
    away_ml = pc.get("awayTeamOdds", {}).get("moneyLine")

    if not home_ml or not away_ml:
        return None

    return {
        "home_moneyline": home_ml,
        "away_moneyline": away_ml,
        "spread": pc.get("spread"),
        "over_under": pc.get("overUnder"),
        "provider": pc.get("provider", {}).get("name", "DraftKings"),
    }


# ---------------------------------------------------------------------------
# TheRundown API (free — 20,000 data points/day; requires free key signup)
# Register at https://therundown.io/api — no credit card. Save key to
# config/therundown.json: {"api_key": "YOUR_KEY"}
# Covers NBA/MLB/NHL/NCAAB with Pinnacle lines (sharpest reference odds available).
# ---------------------------------------------------------------------------

def fetch_therundown_odds(sport: str) -> dict:
    """
    Fetch consensus moneylines from TheRundown API.

    Uses RapidAPI gateway (free tier: 20,000 data points/day).
    Returns empty without error if no API key is configured.
    """
    if not THERUNDOWN_API_KEY:
        return {"error": "TheRundown: no API key (register free at therundown.io/api)", "games": []}

    sport_id = THERUNDOWN_SPORT_IDS.get(sport.lower())
    if not sport_id:
        return {"error": f"TheRundown: no sport ID for {sport}", "games": []}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"{THERUNDOWN_BASE}/sports/{sport_id}/events/{today}?include=scores"

    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(
            url,
            headers={
                "X-TheRundown-Key": THERUNDOWN_API_KEY,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            logger.warning("TheRundown: invalid API key")
        return {"error": f"TheRundown HTTP {e.code}", "games": []}
    except Exception as e:
        return {"error": f"TheRundown request failed: {e}", "games": []}

    events = data.get("events", [])
    if not events:
        return {"error": "TheRundown: no events", "games": []}

    games = []
    has_live = False
    now_utc = datetime.now(timezone.utc)

    for event in events:
        teams = event.get("teams", [])
        if len(teams) < 2:
            continue

        # TheRundown: teams[0] = away, teams[1] = home
        away_team = teams[0].get("name", "")
        home_team = teams[1].get("name", "")
        if not home_team or not away_team:
            continue

        commence_raw = event.get("event_date", "")
        commence_time = ""
        if commence_raw:
            try:
                dt = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
                commence_time = dt.isoformat()
                if dt <= now_utc:
                    has_live = True
            except (ValueError, TypeError):
                pass

        # Extract Pinnacle moneyline from lines_away / lines_home
        home_ml_str, away_ml_str = None, None
        lines = event.get("lines", {})
        for book_name, book_data in lines.items():
            if "pinnacle" not in book_name.lower():
                continue
            ml = book_data.get("moneyline", {})
            home_ml_str = str(ml.get("moneyline_home", "") or "")
            away_ml_str = str(ml.get("moneyline_away", "") or "")
            break

        # Fallback: use any available book if Pinnacle not present
        if not home_ml_str or not away_ml_str:
            for book_name, book_data in lines.items():
                ml = book_data.get("moneyline", {})
                h = str(ml.get("moneyline_home", "") or "")
                a = str(ml.get("moneyline_away", "") or "")
                if h and a and h != "0" and a != "0":
                    home_ml_str, away_ml_str = h, a
                    break

        consensus = {}
        bookmakers = []
        if home_ml_str and away_ml_str and home_ml_str != "0" and away_ml_str != "0":
            home_implied = american_to_implied(home_ml_str)
            away_implied = american_to_implied(away_ml_str)
            if home_implied > 0 and away_implied > 0:
                home_true, away_true = remove_vig(home_implied, away_implied)
                consensus = {
                    home_team: round(home_true, 4),
                    away_team: round(away_true, 4),
                }
                bookmakers = [{"name": "TheRundown/Pinnacle", "h2h": [
                    {"name": home_team, "price": home_ml_str, "implied_prob": round(home_implied, 4)},
                    {"name": away_team, "price": away_ml_str, "implied_prob": round(away_implied, 4)},
                ]}]

        status = "STATUS_SCHEDULED"
        score = event.get("score", {})
        if score and score.get("event_status") == "STATUS_IN_PROGRESS":
            status = "STATUS_IN_PROGRESS"
            has_live = True

        games.append({
            "id": str(event.get("event_id", "")),
            "home_team": home_team,
            "away_team": away_team,
            "commence_time": commence_time,
            "bookmakers": bookmakers,
            "consensus": consensus,
            "status": status,
        })

    games_with_odds = [g for g in games if g.get("consensus")]
    logger.info("Rundown %s: %d games, %d with moneylines", sport.upper(), len(games), len(games_with_odds))

    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "therundown",
        "has_live": has_live,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# The Odds API fallback (last resort — 500/month free tier)
# ---------------------------------------------------------------------------

def _increment_odds_api_count(n: int = 1):
    """Track The Odds API usage in bot state.

    Only called from `_odds_api_fallback` — the paid last-resort source hit
    after DK / FD / ESPN / TheRundown all fail. A stale `last_odds_api_request`
    timestamp therefore means the paid fallback hasn't been hit, not that odds
    data has stopped flowing. If ESPN/live watcher degrades, that's a Session 3
    symptom, not this field.
    """
    try:
        state = {}
        if BOT_STATE_FILE.exists():
            state = json.loads(BOT_STATE_FILE.read_text())
        if not isinstance(state, dict):
            state = {}
        state["odds_api_requests_this_month"] = state.get("odds_api_requests_this_month", 0) + n
        state["last_odds_api_request"] = datetime.now(timezone.utc).isoformat()
        tmp = BOT_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        tmp.rename(BOT_STATE_FILE)
    except Exception as e:
        logger.warning(f"Could not update Odds API count: {e}")


def _odds_api_fallback(sport: str) -> dict:
    """Fall back to The Odds API. Tracks usage."""
    try:
        if BOT_STATE_FILE.exists():
            _s = json.loads(BOT_STATE_FILE.read_text())
            _used = _s.get("odds_api_requests_this_month", 0)
            if _used >= ODDS_API_MONTHLY_LIMIT:
                logger.warning("Odds API quota exhausted (%d/%d) — skipping", _used, ODDS_API_MONTHLY_LIMIT)
                return {"error": "quota_exhausted", "games": []}
            if _used >= int(ODDS_API_MONTHLY_LIMIT * 0.9):
                logger.warning("Odds API at 90%% quota (%d/%d)", _used, ODDS_API_MONTHLY_LIMIT)
    except Exception:
        pass
    try:
        from agent.sports_data import get_odds
        logger.info(f"Falling back to The Odds API for {sport}")
        result = get_odds(sport)
        if "error" not in result:
            _increment_odds_api_count()
            result["source"] = "odds_api"
        return result
    except Exception as e:
        logger.error(f"Odds API fallback failed: {e}")
        return {"error": str(e), "games": []}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_consensus_odds(sport: str, bypass_cache: bool = False) -> dict:
    """
    Fetch consensus odds for a sport.

    Priority chain:
    1. In-memory cache
    2. DraftKings JSON API  (primary — no auth; may 403 on residential IPs)
    3. Bovada JSON API      (reliable free fallback — no WAF)
    4. FanDuel JSON API     (unofficial frontend API — no auth; self-disables on 403)
    5. ESPN scoreboard      (scores/status only — odds often absent)
    6. ESPN pickcenter      (live/final games only)
    7. Odds snapshot        (saved prices when all live sources fail)
    8. TheRundown API       (20k pts/day free; requires free key at therundown.io/api)
    9. The Odds API         (last resort — 500 req/month free tier)

    Returns the same format as agent/sports_data.get_odds().

    Args:
        bypass_cache: If True, skip cache lookup (used by LiveGameWatcher for
                      10-second polling — the default 120s cache would defeat
                      the purpose of fast polling).
    """
    # 1. Cache
    if not bypass_cache:
        cached = _cache_get(sport)
        if cached:
            logger.debug(f"Cache hit for {sport}")
            return cached

    def _has_real_odds(result: dict) -> bool:
        return "error" not in result and bool(
            [g for g in result.get("games", []) if g.get("consensus")]
        )

    def _enrich_status_from_espn(result: dict, sport: str) -> None:
        """Overlay ESPN live-game status onto an already-priced result."""
        espn_result = fetch_espn_odds(sport)
        if "error" in espn_result:
            return
        espn_by_teams: dict[tuple, str] = {}
        for eg in espn_result.get("games", []):
            key = (eg.get("home_team", "").lower(), eg.get("away_team", "").lower())
            espn_by_teams[key] = eg.get("status", "STATUS_SCHEDULED")
        for game in result.get("games", []):
            key = (game.get("home_team", "").lower(), game.get("away_team", "").lower())
            if key in espn_by_teams:
                game["status"] = espn_by_teams[key]

    # 2. DraftKings
    result = fetch_draftkings_odds(sport)
    if _has_real_odds(result):
        _enrich_status_from_espn(result, sport)
        has_live = any(
            g.get("status") in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
            for g in result["games"]
        )
        _cache_set(sport, result, has_live=has_live)
        return result

    if "error" not in result:
        logger.warning("DK %s returned 0 odds — trying Bovada", sport.upper())
    else:
        logger.warning("DK %s failed: %s — trying Bovada", sport.upper(), result.get('error'))

    # 3. Bovada
    result = fetch_bovada_odds(sport)
    if _has_real_odds(result):
        _enrich_status_from_espn(result, sport)
        has_live = any(
            g.get("status") in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
            for g in result["games"]
        )
        _cache_set(sport, result, has_live=has_live)
        return result

    if "error" not in result:
        logger.warning("Bovada %s returned 0 odds — trying FanDuel", sport.upper())
    else:
        logger.warning("Bovada %s failed: %s — trying FanDuel", sport.upper(), result.get('error'))

    # 4. FanDuel
    result = fetch_fanduel_odds(sport)
    if _has_real_odds(result):
        has_live = any(
            g.get("status") in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
            for g in result["games"]
        )
        _cache_set(sport, result, has_live=has_live)
        return result

    if "error" not in result:
        logger.warning("FD %s returned 0 odds — trying ESPN", sport.upper())
    else:
        logger.warning("FD %s failed: %s — trying ESPN", sport.upper(), result.get('error'))

    # 5+6. ESPN scoreboard + pickcenter
    result = fetch_espn_odds(sport)
    if "error" not in result and result.get("games"):
        games_with_odds = [g for g in result["games"] if g.get("consensus")]
        logger.info("ESPN %s: %d games, %d with odds from scoreboard",
                    sport.upper(), len(result['games']), len(games_with_odds))

        if not games_with_odds:
            live_or_final = [
                g for g in result["games"]
                if g.get("status") in (
                    "STATUS_IN_PROGRESS", "STATUS_HALFTIME",
                    "STATUS_FINAL", "STATUS_END_PERIOD",
                )
            ]
            for game in live_or_final:
                pc = _fetch_espn_pickcenter(sport, game["id"])
                if pc:
                    home_implied = american_to_implied(pc["home_moneyline"])
                    away_implied = american_to_implied(pc["away_moneyline"])
                    home_true, away_true = remove_vig(home_implied, away_implied)
                    game["consensus"] = {
                        game["home_team"]: round(home_true, 4),
                        game["away_team"]: round(away_true, 4),
                    }
                    game["bookmakers"] = [{
                        "name": pc["provider"],
                        "h2h": [
                            {"name": game["home_team"], "price": pc["home_moneyline"], "implied_prob": round(home_implied, 4)},
                            {"name": game["away_team"], "price": pc["away_moneyline"], "implied_prob": round(away_implied, 4)},
                        ],
                    }]
            games_with_odds = [g for g in result["games"] if g.get("consensus")]
            logger.info("ESPN after pickcenter: %d games with odds", len(games_with_odds))

        # 6. Odds snapshot
        if not games_with_odds:
            snapshot_odds = _load_snapshot_odds(sport)
            for game in result["games"]:
                if not game.get("consensus"):
                    saved = snapshot_odds.get(game.get("id"))
                    if saved:
                        game["consensus"] = saved["consensus"]
                        game["bookmakers"] = saved["bookmakers"]
            games_with_odds = [g for g in result["games"] if g.get("consensus")]
            if games_with_odds:
                logger.info("ESPN recovered %d games from snapshot", len(games_with_odds))

        if games_with_odds:
            result["source"] = "espn"
            has_live = any(
                g.get("status") in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
                for g in result.get("games", [])
            )
            _cache_set(sport, result, has_live=has_live)
            return result

    # 8. TheRundown (20,000 pts/day free; requires free key at therundown.io/api)
    if THERUNDOWN_API_KEY:
        result = fetch_therundown_odds(sport)
        if _has_real_odds(result):
            has_live = any(
                g.get("status") == "STATUS_IN_PROGRESS"
                for g in result["games"]
            )
            _cache_set(sport, result, has_live=has_live)
            return result
        if "error" not in result:
            logger.warning("Rundown %s returned 0 odds — trying Odds API", sport.upper())
        else:
            logger.warning("Rundown %s failed: %s — trying Odds API", sport.upper(), result.get('error'))

    # 9. The Odds API (last resort — 500/month free tier)
    logger.warning("ODDS all free sources exhausted for %s — falling back to Odds API", sport.upper())
    result = _odds_api_fallback(sport)
    if "error" in result:
        logger.warning("ODDS Odds API also failed for %s: %s", sport.upper(), result.get('error'))
    else:
        games_with_odds = [g for g in result.get("games", []) if g.get("consensus")]
        logger.info("ODDS Odds API returned %d games with odds", len(games_with_odds))
        _cache_set(sport, result)
    return result


# ---------------------------------------------------------------------------
# DK/FD disabled-flag persistence (survive bot restarts)
# ---------------------------------------------------------------------------

def load_source_flags(state: dict) -> None:
    """Restore DK/FD disabled flags from saved bot_state. Call on startup."""
    global _DK_DISABLED, _FD_DISABLED
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).timestamp()

    dk_until = state.get("dk_disabled_until", 0)
    if dk_until and now < dk_until:
        _DK_DISABLED = True
        logger.info("DraftKings still blocked (%.0f min remaining) — skipping DK this session",
                    (dk_until - now) / 60)
    else:
        _DK_DISABLED = False

    fd_until = state.get("fd_disabled_until", 0)
    if fd_until and now < fd_until:
        _FD_DISABLED = True
        logger.info("FanDuel still blocked (%.0f min remaining) — skipping FD this session",
                    (fd_until - now) / 60)
    else:
        _FD_DISABLED = False


def get_source_flags() -> dict:
    """Return current DK/FD disabled state for persistence in bot_state.json."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    ttl = 12 * 3600  # 12-hour WAF rotation window
    return {
        "dk_disabled": _DK_DISABLED,
        "dk_disabled_until": (now + ttl) if _DK_DISABLED else 0,
        "fd_disabled": _FD_DISABLED,
        "fd_disabled_until": (now + ttl) if _FD_DISABLED else 0,
    }
