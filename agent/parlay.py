"""
Hustle Agent — Parlay Decomposition Engine

Parses Kalshi multi-leg sports parlay titles into typed legs,
prices each leg from sportsbook consensus and ESPN player stats,
and identifies mispriced markets.
"""
from __future__ import annotations

import re
import difflib
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable


class LegType(Enum):
    TEAM_WIN = "team_win"
    PLAYER_PROP = "player_prop"
    TOTAL_POINTS = "total_points"
    SPREAD = "spread"
    UNKNOWN = "unknown"


@dataclass
class ParlayLeg:
    raw: str
    leg_type: LegType
    side: str = "yes"
    team: Optional[str] = None
    player: Optional[str] = None
    stat: str = "points"
    threshold: Optional[float] = None
    direction: str = "over"
    sport: str = "nba"
    confidence: float = 1.0
    probability: Optional[float] = None
    source: Optional[str] = None
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "type": self.leg_type.value,
            "side": self.side,
            "team": self.team,
            "player": self.player,
            "stat": self.stat,
            "threshold": self.threshold,
            "direction": self.direction,
            "sport": self.sport,
            "confidence": self.confidence,
            "probability": self.probability,
            "source": self.source,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# NBA team aliases: Kalshi short name -> canonical full name (The Odds API)
# ---------------------------------------------------------------------------

NBA_TEAM_ALIASES: dict[str, str] = {
    "atlanta": "Atlanta Hawks",
    "hawks": "Atlanta Hawks",
    "atl": "Atlanta Hawks",
    "boston": "Boston Celtics",
    "celtics": "Boston Celtics",
    "bos": "Boston Celtics",
    "brooklyn": "Brooklyn Nets",
    "nets": "Brooklyn Nets",
    "bkn": "Brooklyn Nets",
    "charlotte": "Charlotte Hornets",
    "hornets": "Charlotte Hornets",
    "cha": "Charlotte Hornets",
    "chicago": "Chicago Bulls",
    "bulls": "Chicago Bulls",
    "chi": "Chicago Bulls",
    "cleveland": "Cleveland Cavaliers",
    "cavaliers": "Cleveland Cavaliers",
    "cavs": "Cleveland Cavaliers",
    "cle": "Cleveland Cavaliers",
    "dallas": "Dallas Mavericks",
    "mavericks": "Dallas Mavericks",
    "mavs": "Dallas Mavericks",
    "dal": "Dallas Mavericks",
    "denver": "Denver Nuggets",
    "nuggets": "Denver Nuggets",
    "den": "Denver Nuggets",
    "detroit": "Detroit Pistons",
    "pistons": "Detroit Pistons",
    "det": "Detroit Pistons",
    "golden state": "Golden State Warriors",
    "warriors": "Golden State Warriors",
    "gsw": "Golden State Warriors",
    "gs": "Golden State Warriors",
    "houston": "Houston Rockets",
    "rockets": "Houston Rockets",
    "hou": "Houston Rockets",
    "indiana": "Indiana Pacers",
    "pacers": "Indiana Pacers",
    "ind": "Indiana Pacers",
    "la clippers": "Los Angeles Clippers",
    "clippers": "Los Angeles Clippers",
    "lac": "Los Angeles Clippers",
    "la lakers": "Los Angeles Lakers",
    "lakers": "Los Angeles Lakers",
    "lal": "Los Angeles Lakers",
    "los angeles lakers": "Los Angeles Lakers",
    "los angeles clippers": "Los Angeles Clippers",
    "memphis": "Memphis Grizzlies",
    "grizzlies": "Memphis Grizzlies",
    "grizz": "Memphis Grizzlies",
    "mem": "Memphis Grizzlies",
    "miami": "Miami Heat",
    "heat": "Miami Heat",
    "mia": "Miami Heat",
    "milwaukee": "Milwaukee Bucks",
    "bucks": "Milwaukee Bucks",
    "mil": "Milwaukee Bucks",
    "minnesota": "Minnesota Timberwolves",
    "timberwolves": "Minnesota Timberwolves",
    "wolves": "Minnesota Timberwolves",
    "min": "Minnesota Timberwolves",
    "new orleans": "New Orleans Pelicans",
    "pelicans": "New Orleans Pelicans",
    "nop": "New Orleans Pelicans",
    "new york": "New York Knicks",
    "knicks": "New York Knicks",
    "nyk": "New York Knicks",
    "oklahoma city": "Oklahoma City Thunder",
    "thunder": "Oklahoma City Thunder",
    "okc": "Oklahoma City Thunder",
    "orlando": "Orlando Magic",
    "magic": "Orlando Magic",
    "orl": "Orlando Magic",
    "philadelphia": "Philadelphia 76ers",
    "76ers": "Philadelphia 76ers",
    "sixers": "Philadelphia 76ers",
    "phi": "Philadelphia 76ers",
    "phoenix": "Phoenix Suns",
    "suns": "Phoenix Suns",
    "phx": "Phoenix Suns",
    "portland": "Portland Trail Blazers",
    "trail blazers": "Portland Trail Blazers",
    "blazers": "Portland Trail Blazers",
    "por": "Portland Trail Blazers",
    "sacramento": "Sacramento Kings",
    "kings": "Sacramento Kings",
    "sac": "Sacramento Kings",
    "san antonio": "San Antonio Spurs",
    "spurs": "San Antonio Spurs",
    "sas": "San Antonio Spurs",
    "toronto": "Toronto Raptors",
    "raptors": "Toronto Raptors",
    "tor": "Toronto Raptors",
    "utah": "Utah Jazz",
    "jazz": "Utah Jazz",
    "uta": "Utah Jazz",
    "washington": "Washington Wizards",
    "wizards": "Washington Wizards",
    "was": "Washington Wizards",
    "wsh": "Washington Wizards",
    "wiz": "Washington Wizards",
}

# ---------------------------------------------------------------------------
# MLB team aliases: Kalshi short name -> canonical full name (The Odds API)
# ---------------------------------------------------------------------------

MLB_TEAM_ALIASES: dict[str, str] = {
    "arizona": "Arizona Diamondbacks",
    "diamondbacks": "Arizona Diamondbacks",
    "d-backs": "Arizona Diamondbacks",
    "dbacks": "Arizona Diamondbacks",
    "ari": "Arizona Diamondbacks",
    "az": "Arizona Diamondbacks",
    "atlanta": "Atlanta Braves",
    "braves": "Atlanta Braves",
    "atl": "Atlanta Braves",
    "baltimore": "Baltimore Orioles",
    "orioles": "Baltimore Orioles",
    "bal": "Baltimore Orioles",
    "boston": "Boston Red Sox",
    "red sox": "Boston Red Sox",
    "bos": "Boston Red Sox",
    "chicago cubs": "Chicago Cubs",
    "cubs": "Chicago Cubs",
    "chc": "Chicago Cubs",
    "chicago white sox": "Chicago White Sox",
    "white sox": "Chicago White Sox",
    "chw": "Chicago White Sox",
    "cws": "Chicago White Sox",
    "cincinnati": "Cincinnati Reds",
    "reds": "Cincinnati Reds",
    "cin": "Cincinnati Reds",
    "cleveland": "Cleveland Guardians",
    "guardians": "Cleveland Guardians",
    "cle": "Cleveland Guardians",
    "colorado": "Colorado Rockies",
    "rockies": "Colorado Rockies",
    "col": "Colorado Rockies",
    "detroit": "Detroit Tigers",
    "tigers": "Detroit Tigers",
    "det": "Detroit Tigers",
    "houston": "Houston Astros",
    "astros": "Houston Astros",
    "hou": "Houston Astros",
    "kansas city": "Kansas City Royals",
    "royals": "Kansas City Royals",
    "kc": "Kansas City Royals",
    "kcr": "Kansas City Royals",
    "los angeles angels": "Los Angeles Angels",
    "los angeles a": "Los Angeles Angels",
    "angels": "Los Angeles Angels",
    "laa": "Los Angeles Angels",
    "ana": "Los Angeles Angels",   # Anaheim Angels legacy code still used by some sources
    "los angeles dodgers": "Los Angeles Dodgers",
    "los angeles d": "Los Angeles Dodgers",
    "dodgers": "Los Angeles Dodgers",
    "lad": "Los Angeles Dodgers",
    "miami": "Miami Marlins",
    "marlins": "Miami Marlins",
    "mia": "Miami Marlins",
    "milwaukee": "Milwaukee Brewers",
    "brewers": "Milwaukee Brewers",
    "mil": "Milwaukee Brewers",
    "minnesota": "Minnesota Twins",
    "twins": "Minnesota Twins",
    "min": "Minnesota Twins",
    "new york mets": "New York Mets",
    "new york m": "New York Mets",
    "mets": "New York Mets",
    "nym": "New York Mets",
    "new york yankees": "New York Yankees",
    "new york y": "New York Yankees",
    "yankees": "New York Yankees",
    "nyy": "New York Yankees",
    "oakland": "Oakland Athletics",
    "athletics": "Oakland Athletics",
    "a's": "Oakland Athletics",
    "oak": "Oakland Athletics",
    "philadelphia": "Philadelphia Phillies",
    "phillies": "Philadelphia Phillies",
    "phi": "Philadelphia Phillies",
    "pittsburgh": "Pittsburgh Pirates",
    "pirates": "Pittsburgh Pirates",
    "pit": "Pittsburgh Pirates",
    "san diego": "San Diego Padres",
    "padres": "San Diego Padres",
    "sd": "San Diego Padres",
    "sdp": "San Diego Padres",
    "san francisco": "San Francisco Giants",
    "giants": "San Francisco Giants",
    "sf": "San Francisco Giants",
    "sfg": "San Francisco Giants",
    "seattle": "Seattle Mariners",
    "mariners": "Seattle Mariners",
    "sea": "Seattle Mariners",
    "st. louis": "St. Louis Cardinals",
    "st louis": "St. Louis Cardinals",
    "cardinals": "St. Louis Cardinals",
    "stl": "St. Louis Cardinals",
    "tampa bay": "Tampa Bay Rays",
    "rays": "Tampa Bay Rays",
    "tb": "Tampa Bay Rays",
    "tbr": "Tampa Bay Rays",
    "texas": "Texas Rangers",
    "rangers": "Texas Rangers",
    "tex": "Texas Rangers",
    "toronto": "Toronto Blue Jays",
    "blue jays": "Toronto Blue Jays",
    "tor": "Toronto Blue Jays",
    "washington": "Washington Nationals",
    "nationals": "Washington Nationals",
    "nats": "Washington Nationals",
    "wsh": "Washington Nationals",
    "wsn": "Washington Nationals",
    # Oakland/Sacramento/Las Vegas Athletics (franchise in transition)
    "las vegas athletics": "Oakland Athletics",
    "sacramento athletics": "Oakland Athletics",
    "lva": "Oakland Athletics",
    "ath": "Oakland Athletics",
}

# ---------------------------------------------------------------------------
# NHL team aliases (common Kalshi abbreviations seen in cross-sport parlays)
# ---------------------------------------------------------------------------

NHL_TEAM_ALIASES: dict[str, str] = {
    "anaheim": "Anaheim Ducks",
    "ana ducks": "Anaheim Ducks",
    "ducks": "Anaheim Ducks",
    "ana": "Anaheim Ducks",
    "arizona coyotes": "Arizona Coyotes",
    "coyotes": "Arizona Coyotes",
    "boston bruins": "Boston Bruins",
    "bruins": "Boston Bruins",
    "buffalo": "Buffalo Sabres",
    "sabres": "Buffalo Sabres",
    "buf": "Buffalo Sabres",
    "calgary": "Calgary Flames",
    "flames": "Calgary Flames",
    "cgy": "Calgary Flames",
    "carolina": "Carolina Hurricanes",
    "hurricanes": "Carolina Hurricanes",
    "car": "Carolina Hurricanes",
    "chicago blackhawks": "Chicago Blackhawks",
    "blackhawks": "Chicago Blackhawks",
    "colorado avalanche": "Colorado Avalanche",
    "avalanche": "Colorado Avalanche",
    "avs": "Colorado Avalanche",
    "columbus": "Columbus Blue Jackets",
    "blue jackets": "Columbus Blue Jackets",
    "cbj": "Columbus Blue Jackets",
    "dallas stars": "Dallas Stars",
    "stars": "Dallas Stars",
    "detroit red wings": "Detroit Red Wings",
    "red wings": "Detroit Red Wings",
    "edmonton": "Edmonton Oilers",
    "oilers": "Edmonton Oilers",
    "edm": "Edmonton Oilers",
    "florida panthers": "Florida Panthers",
    "panthers": "Florida Panthers",
    "fla": "Florida Panthers",
    "los angeles kings": "Los Angeles Kings",
    "la kings": "Los Angeles Kings",
    "la": "Los Angeles Kings",
    "minnesota wild": "Minnesota Wild",
    "wild": "Minnesota Wild",
    "montreal": "Montreal Canadiens",
    "canadiens": "Montreal Canadiens",
    "habs": "Montreal Canadiens",
    "mtl": "Montreal Canadiens",
    "nashville": "Nashville Predators",
    "predators": "Nashville Predators",
    "preds": "Nashville Predators",
    "nsh": "Nashville Predators",
    "new jersey": "New Jersey Devils",
    "devils": "New Jersey Devils",
    "njd": "New Jersey Devils",
    "nj": "New Jersey Devils",
    "new york islanders": "New York Islanders",
    "nyi islanders": "New York Islanders",
    "islanders": "New York Islanders",
    "nyi": "New York Islanders",
    "new york rangers": "New York Rangers",
    "new york r": "New York Rangers",
    "nyr": "New York Rangers",
    "ottawa": "Ottawa Senators",
    "senators": "Ottawa Senators",
    "ott": "Ottawa Senators",
    "phi flyers": "Philadelphia Flyers",
    "philadelphia flyers": "Philadelphia Flyers",
    "flyers": "Philadelphia Flyers",
    "pittsburgh penguins": "Pittsburgh Penguins",
    "penguins": "Pittsburgh Penguins",
    "pens": "Pittsburgh Penguins",
    "san jose": "San Jose Sharks",
    "sharks": "San Jose Sharks",
    "sjs": "San Jose Sharks",
    "sj": "San Jose Sharks",
    "seattle kraken": "Seattle Kraken",
    "kraken": "Seattle Kraken",
    "st. louis blues": "St. Louis Blues",
    "st louis blues": "St. Louis Blues",
    "blues": "St. Louis Blues",
    "tampa bay lightning": "Tampa Bay Lightning",
    "lightning": "Tampa Bay Lightning",
    "tbl": "Tampa Bay Lightning",
    "tb": "Tampa Bay Lightning",
    "toronto maple leafs": "Toronto Maple Leafs",
    "maple leafs": "Toronto Maple Leafs",
    "leafs": "Toronto Maple Leafs",
    "vancouver": "Vancouver Canucks",
    "canucks": "Vancouver Canucks",
    "van": "Vancouver Canucks",
    "vegas": "Vegas Golden Knights",
    "golden knights": "Vegas Golden Knights",
    "vgk": "Vegas Golden Knights",
    "washington capitals": "Washington Capitals",
    "capitals": "Washington Capitals",
    "caps": "Washington Capitals",
    "winnipeg": "Winnipeg Jets",
    "jets": "Winnipeg Jets",
    "wpg": "Winnipeg Jets",
    "utah hockey club": "Utah Hockey Club",
    # 3-letter codes commonly used in Kalshi series tickers
    "bos": "Boston Bruins",
    "chi": "Chicago Blackhawks",
    "col": "Colorado Avalanche",
    "dal": "Dallas Stars",
    "det": "Detroit Red Wings",
    "lak": "Los Angeles Kings",
    "min": "Minnesota Wild",
    "phi": "Philadelphia Flyers",
    "pit": "Pittsburgh Penguins",
    "sea": "Seattle Kraken",
    "stl": "St. Louis Blues",
    "tor": "Toronto Maple Leafs",
    "uta": "Utah Hockey Club",
    "wsh": "Washington Capitals",
}

# ---------------------------------------------------------------------------
# NCAAB team aliases (commonly seen in Kalshi cross-sport parlays)
# ---------------------------------------------------------------------------

NCAAB_TEAM_ALIASES: dict[str, str] = {
    "uconn": "UConn Huskies",
    "connecticut": "UConn Huskies",
    "ucla": "UCLA Bruins",
    "duke": "Duke Blue Devils",
    "blue devils": "Duke Blue Devils",
    "north carolina": "North Carolina Tar Heels",
    "unc": "North Carolina Tar Heels",
    "tar heels": "North Carolina Tar Heels",
    "kentucky": "Kentucky Wildcats",
    "wildcats": "Kentucky Wildcats",
    "kansas": "Kansas Jayhawks",
    "jayhawks": "Kansas Jayhawks",
    "gonzaga": "Gonzaga Bulldogs",
    "auburn": "Auburn Tigers",
    "purdue": "Purdue Boilermakers",
    "boilermakers": "Purdue Boilermakers",
    "tennessee": "Tennessee Volunteers",
    "vols": "Tennessee Volunteers",
    "alabama": "Alabama Crimson Tide",
    "houston cougars": "Houston Cougars",
    "cougars": "Houston Cougars",
    "creighton": "Creighton Bluejays",
    "bluejays": "Creighton Bluejays",
    "south carolina": "South Carolina Gamecocks",
    "gamecocks": "South Carolina Gamecocks",
    "arizona wildcats": "Arizona Wildcats",
    "oklahoma": "Oklahoma Sooners",
    "sooners": "Oklahoma Sooners",
    "baylor": "Baylor Bears",
    "iowa state": "Iowa State Cyclones",
    "michigan state": "Michigan State Spartans",
    "spartans": "Michigan State Spartans",
    "marquette": "Marquette Golden Eagles",
    "villanova": "Villanova Wildcats",
    "florida gators": "Florida Gators",
    "gators": "Florida Gators",
    "michigan": "Michigan Wolverines",
    "wolverines": "Michigan Wolverines",
    "st. john's": "St. John's Red Storm",
    "st john's": "St. John's Red Storm",
    # Additional short codes seen in Kalshi NCAAB tickers
    "conn": "UConn Huskies",
    "ariz": "Arizona Wildcats",
    "mich": "Michigan Wolverines",
    "okla": "Oklahoma Sooners",
    "bay": "Baylor Bears",
    "ill": "Illinois Fighting Illini",
    "illinois": "Illinois Fighting Illini",
    "crei": "Creighton Bluejays",
    "wvu": "West Virginia Mountaineers",
    "west virginia": "West Virginia Mountaineers",
    "mountaineers": "West Virginia Mountaineers",
    "indiana": "Indiana Hoosiers",
    "hoosiers": "Indiana Hoosiers",
    "iu": "Indiana Hoosiers",
    "ohio state": "Ohio State Buckeyes",
    "buckeyes": "Ohio State Buckeyes",
    "osu": "Ohio State Buckeyes",
    "wisconsin": "Wisconsin Badgers",
    "badgers": "Wisconsin Badgers",
    "wisc": "Wisconsin Badgers",
    "florida": "Florida Gators",
    "memphis": "Memphis Tigers",
    "texas": "Texas Longhorns",
    "longhorns": "Texas Longhorns",
    "tex": "Texas Longhorns",
    "arkansas": "Arkansas Razorbacks",
    "razorbacks": "Arkansas Razorbacks",
    "ark": "Arkansas Razorbacks",
    "san diego state": "San Diego State Aztecs",
    "aztecs": "San Diego State Aztecs",
    "sdsu": "San Diego State Aztecs",
    "miami fl": "Miami Hurricanes",
    "hurricanes": "Miami Hurricanes",
    "lsu": "LSU Tigers",
    "rutgers": "Rutgers Scarlet Knights",
    "rutg": "Rutgers Scarlet Knights",
    "iowa": "Iowa Hawkeyes",
    "hawkeyes": "Iowa Hawkeyes",
    "iowa hawkeyes": "Iowa Hawkeyes",
    "northwestern": "Northwestern Wildcats",
    "penn state": "Penn State Nittany Lions",
    "nittany lions": "Penn State Nittany Lions",
    "psu": "Penn State Nittany Lions",
    "minnesota": "Minnesota Golden Gophers",
    "golden gophers": "Minnesota Golden Gophers",
    "nebr": "Nebraska Cornhuskers",
    "nebraska": "Nebraska Cornhuskers",
    "xavier": "Xavier Musketeers",
    "seton hall": "Seton Hall Pirates",
    "butler": "Butler Bulldogs",
    "providence": "Providence Friars",
    "georgetown": "Georgetown Hoyas",
    "hoyas": "Georgetown Hoyas",
    "cincinnati": "Cincinnati Bearcats",
    "bearcats": "Cincinnati Bearcats",
    "wake forest": "Wake Forest Demon Deacons",
    "virginia": "Virginia Cavaliers",
    "uva": "Virginia Cavaliers",
    "virginia tech": "Virginia Tech Hokies",
    "hokies": "Virginia Tech Hokies",
    "vanderbilt": "Vanderbilt Commodores",
    "nc state": "NC State Wolfpack",
    "wolfpack": "NC State Wolfpack",
    "ncst": "NC State Wolfpack",
    "pittsburgh": "Pittsburgh Panthers",
    "pitt": "Pittsburgh Panthers",
    "louisville": "Louisville Cardinals",
    "card": "Louisville Cardinals",
    "lou": "Louisville Cardinals",
    "colorado": "Colorado Buffaloes",
    "buffaloes": "Colorado Buffaloes",
    "col": "Colorado Buffaloes",
    "oregon": "Oregon Ducks",
    "ducks": "Oregon Ducks",
    "ore": "Oregon Ducks",
    "washington": "Washington Huskies",
    "huskies": "Washington Huskies",
    "uw": "Washington Huskies",
    "utah": "Utah Utes",
    "utes": "Utah Utes",
}

IPL_TEAM_ALIASES: dict[str, str] = {
    "csk":  "Chennai Super Kings",
    "mi":   "Mumbai Indians",
    "rcb":  "Royal Challengers Bengaluru",
    "kkr":  "Kolkata Knight Riders",
    "dc":   "Delhi Capitals",
    "pbks": "Punjab Kings",
    "rr":   "Rajasthan Royals",
    "srh":  "Sunrisers Hyderabad",
    "gt":   "Gujarat Titans",
    "lsg":  "Lucknow Super Giants",
}

# Combined alias lookup: try NBA first, then MLB, NHL, NCAAB
_ALL_TEAM_ALIASES: dict[str, str] = {}
_ALL_TEAM_ALIASES.update(NCAAB_TEAM_ALIASES)
_ALL_TEAM_ALIASES.update(NHL_TEAM_ALIASES)
_ALL_TEAM_ALIASES.update(MLB_TEAM_ALIASES)
_ALL_TEAM_ALIASES.update(NBA_TEAM_ALIASES)  # NBA wins conflicts (e.g. "boston")

# Reverse lookup: full name -> short city name (for matching odds data back)
_FULL_TO_CITY: dict[str, str] = {}
for _alias, _full in NBA_TEAM_ALIASES.items():
    if _full not in _FULL_TO_CITY:
        _FULL_TO_CITY[_full] = _alias


_RE_SPORT_MLB = re.compile(r"\bruns?\b|\binnings?\b|\bstrikeouts?\b", re.IGNORECASE)
_RE_SPORT_NHL = re.compile(r"\bgoals?\b|\bsaves?\b", re.IGNORECASE)
_RE_SPORT_NBA = re.compile(r"\bpoints?\b|\brebounds?\b|\bthree\b", re.IGNORECASE)


def _detect_sport_from_context(raw_title: str) -> Optional[str]:
    """Guess the sport from keywords in the full market title."""
    if _RE_SPORT_MLB.search(raw_title):
        return "mlb"
    if _RE_SPORT_NHL.search(raw_title):
        return "nhl"
    if _RE_SPORT_NBA.search(raw_title):
        return "nba"
    return None


def _resolve_team(raw_team: str, sport_hint: Optional[str] = None) -> tuple[Optional[str], float]:
    """Resolve a raw team name to canonical full name. Returns (full_name, confidence).

    Args:
        raw_team: Raw team string from Kalshi title
        sport_hint: Optional sport hint ("nba", "mlb") to prefer the right alias dict
    """
    key = raw_team.strip().lower()

    # Pick the preferred lookup order based on sport hint
    _SPORT_DICTS = {
        "nba": NBA_TEAM_ALIASES,
        "mlb": MLB_TEAM_ALIASES,
        "nhl": NHL_TEAM_ALIASES,
        "ncaab": NCAAB_TEAM_ALIASES,
    }
    order = [_SPORT_DICTS.get(sport_hint, NBA_TEAM_ALIASES)]
    for d in [NBA_TEAM_ALIASES, MLB_TEAM_ALIASES, NHL_TEAM_ALIASES, NCAAB_TEAM_ALIASES]:
        if d not in order:
            order.append(d)

    # Exact match in ordered dicts
    for d in order:
        if key in d:
            return d[key], 1.0

    # Fuzzy match against all aliases
    all_keys = list(_ALL_TEAM_ALIASES.keys())
    matches = difflib.get_close_matches(key, all_keys, n=1, cutoff=0.6)
    if matches:
        return _ALL_TEAM_ALIASES[matches[0]], 0.8

    # Try matching against full names
    full_names = list(set(_ALL_TEAM_ALIASES.values()))
    matches = difflib.get_close_matches(raw_team.strip(), full_names, n=1, cutoff=0.6)
    if matches:
        return matches[0], 0.8

    return None, 0.5


# ---------------------------------------------------------------------------
# Regex patterns (ordered most specific → broadest)
# ---------------------------------------------------------------------------

# "yes Jalen Brunson: 20+" or "yes Karl-Anthony Towns: 14+"
_RE_PLAYER_PROP = re.compile(
    r"^(yes|no)\s+(.+?):\s*(\d+(?:\.\d+)?)\+?\s*$", re.IGNORECASE
)

# "yes Over 235.5 points scored" or "no Over 210 points" or "yes Over 5.5 runs scored"
_RE_TOTAL_POINTS = re.compile(
    r"^(yes|no)\s+(Over|Under)\s+(\d+(?:\.\d+)?)\s+(points?|runs?|goals?)\s*(?:scored)?\s*$",
    re.IGNORECASE,
)

# "yes Atlanta wins by over 8.5 Points" or "Atlanta wins by over 4.5 Points"
# Also handles runs (MLB) and goals (NHL/soccer)
_RE_SPREAD = re.compile(
    r"^(?:(yes|no)\s+)?(.+?)\s+wins?\s+by\s+over\s+(\d+(?:\.\d+)?)\s+(?:Points?|runs?|goals?)\s*$",
    re.IGNORECASE,
)

# "yes Boston" — broadest, tried last
_RE_TEAM_WIN = re.compile(r"^(yes|no)\s+(.+)$", re.IGNORECASE)


def _unit_to_sport(unit: str) -> str:
    """Map a scoring unit to a sport key."""
    u = unit.lower().rstrip("s")
    if u == "run":
        return "mlb"
    if u == "goal":
        return "nhl"
    return "nba"


def _parse_segment(segment: str, sport_hint: Optional[str] = None) -> ParlayLeg:
    """Parse a single comma-separated segment into a ParlayLeg."""
    segment = segment.strip()

    # 1. Player prop
    m = _RE_PLAYER_PROP.match(segment)
    if m:
        side = m.group(1).lower()
        player = m.group(2).strip()
        threshold = float(m.group(3))
        return ParlayLeg(
            raw=segment,
            leg_type=LegType.PLAYER_PROP,
            side=side,
            player=player,
            stat="points",
            threshold=threshold,
            direction="over",
            sport=sport_hint or "nba",
            confidence=0.9,  # player name matching is fuzzy
        )

    # 2. Total points / runs / goals
    m = _RE_TOTAL_POINTS.match(segment)
    if m:
        side = m.group(1).lower()
        direction = m.group(2).lower()
        threshold = float(m.group(3))
        unit = m.group(4)
        sport = _unit_to_sport(unit)
        return ParlayLeg(
            raw=segment,
            leg_type=LegType.TOTAL_POINTS,
            side=side,
            threshold=threshold,
            direction=direction,
            sport=sport,
            confidence=1.0,
        )

    # 3. Spread / margin
    m = _RE_SPREAD.match(segment)
    if m:
        side = (m.group(1) or "yes").lower()
        raw_team = m.group(2).strip()
        threshold = float(m.group(3))
        full_name, conf = _resolve_team(raw_team, sport_hint)
        return ParlayLeg(
            raw=segment,
            leg_type=LegType.SPREAD,
            side=side,
            team=full_name or raw_team,
            threshold=threshold,
            direction="over",
            sport=sport_hint or "nba",
            confidence=conf,
            warnings=[] if full_name else [f"Unknown team: '{raw_team}'"],
        )

    # 4. Team win (broadest)
    m = _RE_TEAM_WIN.match(segment)
    if m:
        side = m.group(1).lower()
        raw_team = m.group(2).strip()
        full_name, conf = _resolve_team(raw_team, sport_hint)
        return ParlayLeg(
            raw=segment,
            leg_type=LegType.TEAM_WIN,
            side=side,
            team=full_name or raw_team,
            sport=sport_hint or "nba",
            confidence=conf,
            warnings=[] if full_name else [f"Unknown team: '{raw_team}'"],
        )

    # 5. Unknown — don't crash
    return ParlayLeg(
        raw=segment,
        leg_type=LegType.UNKNOWN,
        confidence=0.3,
        warnings=[f"Could not parse leg: '{segment}'"],
    )


def parse_parlay_title(title: str) -> list[ParlayLeg]:
    """Parse a Kalshi parlay market title into a list of typed legs."""
    sport_hint = _detect_sport_from_context(title)
    segments = [s.strip() for s in title.split(",") if s.strip()]
    return [_parse_segment(seg, sport_hint) for seg in segments]


# ---------------------------------------------------------------------------
# Parlay Pricer
# ---------------------------------------------------------------------------


def _match_team_to_game(team_name: str, odds_data: dict) -> Optional[dict]:
    """Find the game in odds_data that includes this team."""
    if not odds_data or "games" not in odds_data:
        return None
    for game in odds_data["games"]:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        if team_name in (home, away):
            return game
        # Fuzzy: check if team_name is a substring
        if team_name.lower() in home.lower() or team_name.lower() in away.lower():
            return game
    return None


def _get_game_key(game: dict) -> str:
    """Unique key for a game."""
    return f"{game.get('away_team', '')}@{game.get('home_team', '')}"


def _extract_spread_prob(game: dict, team_name: str, threshold: float) -> Optional[float]:
    """Extract spread implied probability from bookmaker data."""
    for bm in game.get("bookmakers", []):
        spreads = bm.get("spreads", [])
        for outcome in spreads:
            name = outcome.get("name", "")
            point = outcome.get("point")
            if point is not None and name.lower() in team_name.lower() or team_name.lower() in name.lower():
                # If the sportsbook spread matches close to our threshold, use its implied prob
                if abs(float(point) - (-threshold)) < 2.0:
                    return outcome.get("implied_prob")
    return None


def _extract_total_prob(game: dict, threshold: float, direction: str) -> Optional[float]:
    """Extract totals implied probability from bookmaker data."""
    for bm in game.get("bookmakers", []):
        totals = bm.get("totals", [])
        for outcome in totals:
            name = outcome.get("name", "").lower()
            point = outcome.get("point")
            if point is not None and name == direction:
                # Use this bookmaker's line if it's close to our threshold
                if abs(float(point) - threshold) < 3.0:
                    return outcome.get("implied_prob")
    return None


def _find_game_for_total(leg: ParlayLeg, odds_data: dict, team_game_map: dict[str, str]) -> Optional[str]:
    """Try to associate a total_points leg with a game.

    Strategy: if other legs in the parlay are already mapped to a single game,
    and this total matches that game's bookmaker totals line, associate it.
    Also try matching by totals line proximity.
    """
    if not odds_data or "games" not in odds_data:
        return None
    # If there's only one game in the parlay so far, associate with it
    unique_games = set(team_game_map.values())
    if len(unique_games) == 1:
        return next(iter(unique_games))
    # Try matching by totals line
    for game in odds_data["games"]:
        for bm in game.get("bookmakers", []):
            for outcome in bm.get("totals", []):
                point = outcome.get("point")
                if point is not None and leg.threshold is not None:
                    if abs(float(point) - leg.threshold) < 5.0:
                        return _get_game_key(game)
    return None


def _group_legs_by_game(
    legs: list[ParlayLeg], odds_data: dict
) -> dict[str, list[int]]:
    """Group leg indices by game key for correlation adjustment."""
    groups: dict[str, list[int]] = {}
    ungrouped_key = "__cross_game__"

    # First pass: map legs with teams to games
    team_game_map: dict[str, str] = {}  # leg index -> game key
    for i, leg in enumerate(legs):
        if leg.team:
            game = _match_team_to_game(leg.team, odds_data)
            if game:
                key = _get_game_key(game)
                team_game_map[str(i)] = key
                groups.setdefault(key, []).append(i)
                continue
        if leg.leg_type not in (LegType.TOTAL_POINTS,):
            groups.setdefault(ungrouped_key, []).append(i)

    # Second pass: try to associate total_points legs with games
    for i, leg in enumerate(legs):
        if leg.leg_type == LegType.TOTAL_POINTS and str(i) not in team_game_map:
            game_key = _find_game_for_total(leg, odds_data, team_game_map)
            if game_key:
                groups.setdefault(game_key, []).append(i)
            else:
                groups.setdefault(ungrouped_key, []).append(i)

    return groups


def price_parlay(
    legs: list[ParlayLeg],
    odds_data: dict,
    player_stats_fn: Optional[Callable] = None,
) -> dict:
    """
    Price a parsed parlay by assigning probabilities to each leg.

    Args:
        legs: Parsed ParlayLeg objects from parse_parlay_title
        odds_data: Result from sports_data.get_odds() (fetched once per scan)
        player_stats_fn: Callable(player, stat, threshold, sport) -> dict with 'probability'
    """
    all_warnings: list[str] = []
    legs_priced = 0
    legs_unpriced = 0

    for leg in legs:
        prob = None
        source = None

        if leg.leg_type == LegType.TEAM_WIN:
            game = _match_team_to_game(leg.team, odds_data) if leg.team else None
            if game and game.get("consensus"):
                # Look up consensus probability
                consensus = game["consensus"]
                # Try exact match first, then substring
                for team_name, p in consensus.items():
                    if leg.team and (
                        leg.team.lower() in team_name.lower()
                        or team_name.lower() in leg.team.lower()
                    ):
                        prob = p
                        source = "Sportsbook consensus"
                        break

        elif leg.leg_type == LegType.SPREAD:
            game = _match_team_to_game(leg.team, odds_data) if leg.team else None
            if game:
                prob = _extract_spread_prob(game, leg.team or "", leg.threshold or 0)
                if prob:
                    source = "Sportsbook spread lines"

        elif leg.leg_type == LegType.TOTAL_POINTS:
            # Try to find any game's totals — for cross-game totals, use first available
            if odds_data and "games" in odds_data:
                for game in odds_data["games"]:
                    prob = _extract_total_prob(
                        game, leg.threshold or 0, leg.direction
                    )
                    if prob:
                        source = "Sportsbook totals lines"
                        break

        elif leg.leg_type == LegType.PLAYER_PROP:
            if player_stats_fn and leg.player and leg.threshold is not None:
                try:
                    result = player_stats_fn(
                        leg.player, leg.stat, leg.threshold, leg.sport
                    )
                    if isinstance(result, dict) and "probability" in result:
                        prob = result["probability"]
                        source = result.get("source", "ESPN player stats")
                        leg.confidence = min(
                            leg.confidence, result.get("confidence", 0.7)
                        )
                        if result.get("warnings"):
                            leg.warnings.extend(result["warnings"])
                except Exception as e:
                    leg.warnings.append(f"Player stats error: {e}")

        # Fallback for anything unpriced
        if prob is None:
            prob = 0.50
            source = "unpriced (fallback 50%)"
            legs_unpriced += 1
            if leg.leg_type == LegType.UNKNOWN:
                leg.warnings.append("Unknown leg type — assigned 50% probability")
            else:
                leg.warnings.append(
                    f"Could not find pricing data for {leg.leg_type.value} leg"
                )
                all_warnings.append(
                    f"{leg.leg_type.value} '{leg.raw}' fell back to 50%"
                )
        else:
            legs_priced += 1

        # Handle "no" side — invert probability
        if leg.side == "no":
            prob = 1.0 - prob

        leg.probability = round(prob, 4)
        leg.source = source

    # Raw combined probability (assuming independence)
    raw_probability = 1.0
    for leg in legs:
        raw_probability *= leg.probability or 0.5

    # Correlation adjustment for same-game legs
    game_groups = _group_legs_by_game(legs, odds_data)
    correlation_factor = 1.0
    same_game_info = {}
    for key, indices in game_groups.items():
        if key == "__cross_game__":
            continue
        if len(indices) > 1:
            # Apply 0.82 discount per extra leg in the same game.
            # Same-game legs are correlated — a team playing well affects all
            # same-game outcomes. The old 0.95 (5%) was too small to matter;
            # 0.82 (18%) per extra leg is more conservative and defensible.
            discount = 0.82 ** (len(indices) - 1)
            correlation_factor *= discount
            same_game_info[key] = [legs[i].raw for i in indices]

    correlation_adjusted = raw_probability * correlation_factor

    # Overall confidence: weighted average of leg confidences
    if legs:
        total_conf = sum(leg.confidence for leg in legs)
        avg_confidence = total_conf / len(legs)
        # Penalize for unpriced legs
        if legs_unpriced > 0:
            penalty = 0.15 * legs_unpriced
            avg_confidence = max(0.1, avg_confidence - penalty)
    else:
        avg_confidence = 0.0

    all_warnings.extend(w for leg in legs for w in leg.warnings)

    return {
        "legs": [leg.to_dict() for leg in legs],
        "legs_priced": legs_priced,
        "legs_unpriced": legs_unpriced,
        "raw_probability": round(raw_probability, 6),
        "correlation_adjusted": round(correlation_adjusted, 6),
        "confidence": round(avg_confidence, 2),
        "warnings": all_warnings,
        "same_game_groups": same_game_info,
    }
