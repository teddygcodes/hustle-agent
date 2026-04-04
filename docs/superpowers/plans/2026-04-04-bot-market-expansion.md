# Bot Market Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the Glint trading bot to cover NHL (re-enable), IPL cricket, ETH daily markets, and fix weather NWS next-day targeting — improving scan coverage from 3 sports + BTC to 4 sports + cricket + ETH + better weather.

**Architecture:** All changes extend `bot/kalshi_series.py` (new scanner functions) and `bot/config.py`/`agent/sports_data.py` (new sport keys and series tickers). No new files needed except tests. Each scanner follows the same pattern: fetch Kalshi markets for a series ticker, look up true probability from an external odds source, compute relative edge, self-check math, return opportunity dicts.

**Tech Stack:** Python 3.9+, existing Kalshi SDK client, CoinGecko free API, The Odds API (existing key), NWS API, pytest

---

## File Map

| File | Change |
|------|--------|
| `bot/config.py` | Add `"nhl"` to `ACTIVE_SPORTS`; add `ETH_SERIES`; add new strategy keys |
| `agent/sports_data.py` | Add `"ipl"` key to `SPORT_MAP` |
| `agent/parlay.py` | Add `IPL_TEAM_ALIASES` dict |
| `bot/kalshi_series.py` | Add `IPL_SERIES`, `scan_ipl_series()`, `scan_ethereum_series()`, wire both into `scan_series_markets()` |
| `bot/scanner.py` | Fix `scan_weather_markets()` to match NWS period to market target date |
| `tests/test_bot_scanners.py` | New test file covering all four tasks |

---

## Task 1: Re-enable NHL

**Files:**
- Modify: `bot/config.py:167`
- Test: `tests/test_bot_scanners.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bot_scanners.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def test_nhl_in_active_sports():
    from bot.config import ACTIVE_SPORTS
    assert "nhl" in ACTIVE_SPORTS, f"NHL missing from ACTIVE_SPORTS: {ACTIVE_SPORTS}"

def test_nhl_has_series_ticker():
    from bot.kalshi_series import SPORTS_SERIES
    assert "nhl" in SPORTS_SERIES
    assert SPORTS_SERIES["nhl"] == "KXNHLGAME"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/tylergilstrap/Desktop/hustle-agent/hustle-agent
python3 -m pytest tests/test_bot_scanners.py::test_nhl_in_active_sports -v
```

Expected: `FAILED — AssertionError: NHL missing from ACTIVE_SPORTS`

- [ ] **Step 3: Edit `bot/config.py` line 167**

Change:
```python
ACTIVE_SPORTS = ["nba", "mlb", "ncaab"]
```
To:
```python
ACTIVE_SPORTS = ["nba", "mlb", "ncaab", "nhl"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_nhl_in_active_sports tests/test_bot_scanners.py::test_nhl_has_series_ticker -v
```

Expected: both PASS

- [ ] **Step 5: Commit**

```bash
git add bot/config.py tests/test_bot_scanners.py
git commit -m "feat: re-enable NHL in active sports scanner"
```

---

## Task 2: IPL Cricket Scanner

IPL uses The Odds API (`cricket_ipl` sport key) as the primary odds source — Bovada doesn't carry cricket. The scanner follows the same pattern as `scan_sports_series()` but calls `_build_odds_api_lookup("ipl")` directly (skipping Bovada). Kalshi ticker format: `KXIPLGAME-26APR11DCCSK-DC` → abbrev `dc` → "Delhi Capitals".

**Files:**
- Modify: `agent/sports_data.py` (add `"ipl"` to SPORT_MAP)
- Modify: `agent/parlay.py` (add `IPL_TEAM_ALIASES`)
- Modify: `bot/kalshi_series.py` (add series constant + scanner function + wire into `scan_series_markets`)
- Modify: `bot/config.py` (add `"ipl_game_edge"` to `ACTIVE_STRATEGIES`)
- Test: `tests/test_bot_scanners.py`

### Step 2a — Add IPL to Odds API sport map

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_scanners.py`:
```python
def test_ipl_in_sport_map():
    from agent.sports_data import SPORT_MAP
    assert "ipl" in SPORT_MAP
    assert SPORT_MAP["ipl"] == "cricket_ipl"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_ipl_in_sport_map -v
```

Expected: `FAILED — AssertionError`

- [ ] **Step 3: Edit `agent/sports_data.py`**

In the `SPORT_MAP` dict (around line 27), add after the `"ufc"` line:
```python
    "ipl":  "cricket_ipl",
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_ipl_in_sport_map -v
```

Expected: PASS

### Step 2b — Add IPL team aliases

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_scanners.py`:
```python
def test_ipl_team_aliases_complete():
    from agent.parlay import IPL_TEAM_ALIASES
    required = ["csk", "mi", "rcb", "kkr", "dc", "pbks", "rr", "srh", "gt", "lsg"]
    for abbrev in required:
        assert abbrev in IPL_TEAM_ALIASES, f"Missing IPL alias: {abbrev}"
    # Verify values are non-empty strings
    for k, v in IPL_TEAM_ALIASES.items():
        assert isinstance(v, str) and len(v) > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_ipl_team_aliases_complete -v
```

Expected: `FAILED — ImportError: cannot import name 'IPL_TEAM_ALIASES'`

- [ ] **Step 3: Edit `agent/parlay.py` — add IPL aliases after the existing alias dicts**

Find the end of the existing alias dicts (e.g., after `NCAAB_TEAM_ALIASES`) and add:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_ipl_team_aliases_complete -v
```

Expected: PASS

### Step 2c — Add IPL scanner function to `bot/kalshi_series.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_scanners.py`:
```python
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
    # Odds API returns DC at 60% true prob
    fake_lookup = {"delhi capitals": 0.60, "capitals": 0.60,
                   "chennai super kings": 0.40, "kings": 0.40}

    with patch("bot.kalshi_series._fetch_series_markets", return_value=fake_markets), \
         patch("bot.kalshi_series._build_odds_api_lookup", return_value=fake_lookup):
        result = scan_ipl_series()

    assert isinstance(result, list)
    # DC at 43¢ ask with 60% true prob → 39.5% relative edge → should appear
    dc_opps = [o for o in result if "DC" in o.get("ticker", "")]
    assert len(dc_opps) > 0, f"Expected DC opportunity, got: {result}"
    assert dc_opps[0]["type"] == "ipl_game_edge"
    assert dc_opps[0]["edge"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_scan_ipl_series_returns_list -v
```

Expected: `FAILED — ImportError: cannot import name 'scan_ipl_series'`

- [ ] **Step 3: Add IPL series constant and scanner to `bot/kalshi_series.py`**

At the top of `kalshi_series.py`, after the `BTC_SERIES` constant (around line 58), add:

```python
IPL_SERIES = "KXIPLGAME"
```

At the top of `kalshi_series.py`, add `IPL_TEAM_ALIASES` to the import from `agent.parlay`:

```python
from agent.parlay import (
    NBA_TEAM_ALIASES, MLB_TEAM_ALIASES,
    NHL_TEAM_ALIASES, NCAAB_TEAM_ALIASES,
    IPL_TEAM_ALIASES,
)
```

Then add the `scan_ipl_series()` function after `scan_bitcoin_series()` (near the end of `kalshi_series.py`, before `scan_series_markets`):

```python
def scan_ipl_series() -> list[dict]:
    """
    Scan KXIPLGAME series for edges vs The Odds API consensus.

    IPL cricket markets — Bovada doesn't carry cricket, so we use
    The Odds API directly (cricket_ipl sport key).
    Same edge/filter logic as scan_sports_series().
    Returns list of opportunity dicts with type='ipl_game_edge'.
    """
    markets = _fetch_series_markets(IPL_SERIES)
    if not markets:
        print(f"  [Series/IPL] No open markets for {IPL_SERIES}")
        return []

    # IPL odds come from The Odds API only (Bovada doesn't carry cricket)
    odds_lookup = _build_odds_api_lookup("ipl")
    print(
        f"  [Series/IPL] {len(markets)} open markets | "
        f"{len(odds_lookup)//2} Odds API teams"
    )

    now_utc = datetime.now(timezone.utc)
    opportunities = []
    for market in markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        # Skip illiquid markets (same threshold as sports series)
        volume = market.get("volume") or 0
        open_interest = market.get("open_interest") or 0
        if volume < 10 and open_interest < 5:
            continue

        abbrev = _extract_team_abbrev(ticker)
        if not abbrev:
            continue

        canonical = IPL_TEAM_ALIASES.get(abbrev.lower())
        if not canonical:
            print(f"    [Series/IPL] SKIP {ticker}: unknown abbrev={abbrev!r}")
            continue

        prob = (
            odds_lookup.get(canonical.lower())
            or odds_lookup.get(canonical.split()[-1].lower())
        )
        if prob is None or prob <= 0:
            print(
                f"    [Series/IPL] SKIP {ticker}: "
                f"no odds match for canonical={canonical!r}"
            )
            continue

        # Parse hours-to-game from ticker date segment
        hours_to_game = 0.0
        game_dt = None
        try:
            parts = ticker.split("-")
            if len(parts) >= 3:
                date_seg = parts[1]
                import re as _re
                m = _re.match(r"(\d{2})([A-Z]{3})(\d{2})", date_seg)
                if m:
                    yy, mon, dd = m.group(1), m.group(2), m.group(3)
                    month_map = {
                        "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                        "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12
                    }
                    game_dt = datetime(
                        2000 + int(yy), month_map.get(mon, 1), int(dd),
                        tzinfo=timezone.utc
                    )
                    hours_to_game = (game_dt - now_utc).total_seconds() / 3600
        except Exception:
            pass

        # Same distant-game edge penalty as sports series
        min_edge_required = MIN_RELATIVE_EDGE
        if hours_to_game > 48:
            min_edge_required = MIN_RELATIVE_EDGE + (hours_to_game - 48) * 0.002

        kalshi_price = yes_ask / 100.0
        edge = prob - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        check_ok, check_msg = _self_check_edge(prob, kalshi_price, edge)
        if not check_ok:
            continue

        if kalshi_price <= 0.03 or prob <= 0.03:
            continue

        if abs(relative_edge) < min_edge_required:
            continue

        # Cap absurd edges (stale/live-game prices)
        if abs(relative_edge) > 1.5:
            print(
                f"    [Series/IPL] SKIP {ticker}: "
                f"relative_edge={relative_edge:.1%} exceeds sanity cap"
            )
            continue

        confidence = 0.75  # Cricket odds markets are thinner than major US sports
        if hours_to_game > 72:
            confidence = 0.60
        elif hours_to_game > 48:
            confidence = 0.68

        game_date_str = game_dt.strftime("%a %b") + f" {game_dt.day}" if game_dt else None

        opportunities.append({
            "type": "ipl_game_edge",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": confidence,
            "recommended_side": "yes" if edge > 0 else "no",
            "odds_prob": round(prob, 4),
            "odds_source": "odds_api",
            "kalshi_price": round(kalshi_price, 4),
            "team_abbrev": abbrev,
            "canonical_team": canonical,
            "sport": "ipl",
            "hours_to_game": round(hours_to_game, 1),
            "game_date_str": game_date_str,
            "edge_result": {
                "fair_value": round(prob, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": confidence,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_scan_ipl_series_returns_list -v
```

Expected: PASS

### Step 2d — Wire IPL into `scan_series_markets()` and `ACTIVE_STRATEGIES`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_scanners.py`:
```python
def test_scan_series_markets_calls_ipl():
    from unittest.mock import patch, MagicMock
    import bot.kalshi_series as ks

    with patch.object(ks, "scan_sports_series", return_value=[]) as mock_sports, \
         patch.object(ks, "scan_bitcoin_series", return_value=[]) as mock_btc, \
         patch.object(ks, "scan_ipl_series", return_value=[{"type": "ipl_game_edge"}]) as mock_ipl:
        result = ks.scan_series_markets()

    mock_ipl.assert_called_once()
    assert any(o["type"] == "ipl_game_edge" for o in result)

def test_ipl_in_active_strategies():
    from bot.config import ACTIVE_STRATEGIES
    assert "ipl_game_edge" in ACTIVE_STRATEGIES
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_scan_series_markets_calls_ipl tests/test_bot_scanners.py::test_ipl_in_active_strategies -v
```

Expected: both FAIL

- [ ] **Step 3: Edit `scan_series_markets()` in `bot/kalshi_series.py`**

Find the `scan_series_markets()` function (line 766). After the `btc_opps` block, add:

```python
    ipl_opps = scan_ipl_series()
    print(f"  [Series/IPL] Found {len(ipl_opps)} opportunities")
    all_opps.extend(ipl_opps)
```

The full end of `scan_series_markets()` should look like:
```python
    btc_opps = scan_bitcoin_series()
    print(f"  [Series/BTC] Found {len(btc_opps)} opportunities")
    all_opps.extend(btc_opps)

    ipl_opps = scan_ipl_series()
    print(f"  [Series/IPL] Found {len(ipl_opps)} opportunities")
    all_opps.extend(ipl_opps)

    return all_opps
```

- [ ] **Step 4: Edit `bot/config.py` — add `ipl_game_edge` to ACTIVE_STRATEGIES**

Change:
```python
ACTIVE_STRATEGIES = ["weather", "vig_stack_series", "series_game_edge"]
```
To:
```python
ACTIVE_STRATEGIES = ["weather", "vig_stack_series", "series_game_edge", "ipl_game_edge"]
```

Also add IPL to the `scan_cycle` strategy check. In `bot/scanner.py` line 1220, the condition is:
```python
if any(s in ACTIVE_STRATEGIES for s in ("series_game_edge", "btc_price_edge")):
```
Change to:
```python
if any(s in ACTIVE_STRATEGIES for s in ("series_game_edge", "btc_price_edge", "ipl_game_edge")):
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_scan_series_markets_calls_ipl tests/test_bot_scanners.py::test_ipl_in_active_strategies -v
```

Expected: both PASS

- [ ] **Step 6: Commit**

```bash
git add agent/sports_data.py agent/parlay.py bot/kalshi_series.py bot/config.py bot/scanner.py tests/test_bot_scanners.py
git commit -m "feat: add IPL cricket scanner (KXIPLGAME via Odds API)"
```

---

## Task 3: ETH Daily Scanner

Ethereum daily close markets (series `ETH` on Kalshi) use the same lognormal model as BTC. CoinGecko already returns ETH price in `crypto.py` — we call it directly here to avoid importing the module.

**Files:**
- Modify: `bot/kalshi_series.py` (add `ETH_SERIES`, `_get_eth_spot()`, `scan_ethereum_series()`, wire into `scan_series_markets()`)
- Modify: `bot/config.py` (add `"eth_price_edge"` to `ACTIVE_STRATEGIES`)
- Test: `tests/test_bot_scanners.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_scanners.py`:
```python
def test_scan_ethereum_series_returns_list():
    from unittest.mock import patch
    from bot.kalshi_series import scan_ethereum_series

    fake_markets = [
        {
            "ticker": "ETH-26APR05-T1800",
            "title": "Will Ethereum close above $1800 on Apr 5?",
            "yes_ask": 35,
            "yes_bid": 32,
            "no_ask": 66,
            "no_bid": 63,
            "volume": 200,
            "open_interest": 25,
            "close_time": "2026-04-05T21:00:00Z",
        },
    ]

    with patch("bot.kalshi_series._fetch_series_markets", return_value=fake_markets), \
         patch("bot.kalshi_series._get_eth_spot", return_value=1950.0), \
         patch("bot.kalshi_series._get_eth_realized_vol", return_value=0.04):
        result = scan_ethereum_series()

    assert isinstance(result, list)
    # ETH at $1950, threshold $1800 → strong YES — should show opportunity
    assert len(result) > 0, f"Expected ETH opportunity at 1950 vs 1800, got empty"
    assert result[0]["type"] == "eth_price_edge"
    assert result[0]["edge"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_scan_ethereum_series_returns_list -v
```

Expected: `FAILED — ImportError: cannot import name 'scan_ethereum_series'`

- [ ] **Step 3: Add ETH scanner to `bot/kalshi_series.py`**

After the `BTC_DAILY_VOL` and `BTC_SERIES` constants block, add:
```python
ETH_SERIES = "ETH"
ETH_DAILY_VOL = 0.045   # fallback if CoinGecko unavailable (ETH slightly more volatile than BTC)
COINGECKO_ETH_URL = f"{COINGECKO_BASE}/simple/price?ids=ethereum&vs_currencies=usd"
COINGECKO_ETH_HISTORY_URL = (
    f"{COINGECKO_BASE}/coins/ethereum/market_chart"
    "?vs_currency=usd&days=1&interval=hourly"
)

_ETH_VOL_CACHE: tuple[float, float] | None = None
_ETH_SPOT_CACHE: tuple[float, float] | None = None
_ETH_CACHE_TTL = 1800  # 30 min
```

Then add the ETH helper functions after `_get_btc_spot()`:
```python
def _get_eth_realized_vol() -> float:
    """Compute 24h realized daily volatility for ETH from CoinGecko hourly prices."""
    global _ETH_VOL_CACHE
    if _ETH_VOL_CACHE and (_time.monotonic() - _ETH_VOL_CACHE[0]) < _ETH_CACHE_TTL:
        return _ETH_VOL_CACHE[1]

    data = _get_json(COINGECKO_ETH_HISTORY_URL)
    if not data:
        return ETH_DAILY_VOL

    prices = [p[1] for p in data.get("prices", []) if len(p) == 2]
    if len(prices) < 4:
        return ETH_DAILY_VOL

    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    hourly_variance = sum(r ** 2 for r in log_returns) / len(log_returns)
    daily_vol = math.sqrt(hourly_variance * 24)
    daily_vol = max(0.01, min(0.15, daily_vol))  # Floor 1%, ceiling 15%

    print(f"  [Series/ETH] Realized 24h vol: {daily_vol:.2%}")
    _ETH_VOL_CACHE = (_time.monotonic(), daily_vol)
    return daily_vol


def _get_eth_spot() -> float | None:
    """Fetch ETH/USD spot price from CoinGecko."""
    global _ETH_SPOT_CACHE
    if _ETH_SPOT_CACHE and (_time.monotonic() - _ETH_SPOT_CACHE[0]) < _ETH_CACHE_TTL:
        return _ETH_SPOT_CACHE[1]

    data = _get_json(COINGECKO_ETH_URL)
    if not data:
        return None
    try:
        price = float(data["ethereum"]["usd"])
        _ETH_SPOT_CACHE = (_time.monotonic(), price)
        return price
    except (KeyError, TypeError, ValueError):
        return None
```

Then add `scan_ethereum_series()` after `scan_bitcoin_series()`:
```python
def scan_ethereum_series() -> list[dict]:
    """
    Scan ETH series for edges vs CoinGecko spot price.

    Uses same lognormal model as BTC scanner.
    Skips silently if no open markets exist (ETH markets are intermittent).
    Returns list of opportunity dicts with type='eth_price_edge'.
    """
    markets = _fetch_series_markets(ETH_SERIES)
    if not markets:
        print(f"  [Series/ETH] No open markets for {ETH_SERIES}")
        return []

    spot = _get_eth_spot()
    if not spot:
        print(f"  [Series/ETH] Could not fetch ETH spot price")
        return []

    now_utc = datetime.now(timezone.utc)
    # ETH markets resolve at 21:00 UTC (same schedule as BTC on Kalshi)
    resolve_today = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
    hours_remaining = (resolve_today - now_utc).total_seconds() / 3600.0

    if hours_remaining < 0:
        print(f"  [Series/ETH] All today's markets already resolved")
        return []

    today_str = now_utc.strftime("%y%b%d").upper()
    print(
        f"  [Series/ETH] {len(markets)} open markets | "
        f"spot=${spot:,.0f} | hours_remaining={hours_remaining:.1f}h"
    )

    opportunities = []
    for market in markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        # Only trade today's markets — future markets have negligible spot edge
        if today_str not in ticker:
            continue

        # Parse threshold and direction from ticker suffix
        # ETH tickers: ETH-26APR05-T1800 (above $1800), ETH-26APR05-B1750 (below $1750)
        parts = ticker.split("-")
        if len(parts) < 3:
            continue
        suffix = parts[-1]
        if suffix.startswith("T"):
            try:
                threshold = float(suffix[1:])
                direction = "above"
            except ValueError:
                continue
        elif suffix.startswith("B"):
            try:
                threshold = float(suffix[1:])
                direction = "below"
            except ValueError:
                continue
        else:
            continue

        # Lognormal probability (same as BTC)
        vol = _get_eth_realized_vol() * math.sqrt(hours_remaining / 24.0)
        if vol <= 0:
            continue
        log_ratio = math.log(spot / threshold)
        z = log_ratio / vol
        p_above = 0.5 * math.erfc(-z / math.sqrt(2))
        p_below = 1.0 - p_above

        fair_value = p_above if direction == "above" else p_below
        kalshi_price = yes_ask / 100.0
        edge = fair_value - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        check_ok, check_msg = _self_check_edge(fair_value, kalshi_price, edge)
        if not check_ok:
            continue

        if kalshi_price <= 0.03 or fair_value <= 0.03:
            continue

        if abs(relative_edge) < MIN_RELATIVE_EDGE:
            continue

        opportunities.append({
            "type": "eth_price_edge",
            "ticker": ticker,
            "title": title,
            "market": market,
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": 0.70,  # ETH vol model is approximate
            "recommended_side": "yes" if edge > 0 else "no",
            "spot_price": spot,
            "threshold": threshold,
            "direction": direction,
            "fair_value": round(fair_value, 4),
            "kalshi_price": round(kalshi_price, 4),
            "hours_remaining": round(hours_remaining, 1),
            "edge_result": {
                "fair_value": round(fair_value, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": 0.70,
                "self_check_passed": True,
                "math_chain": [check_msg],
                "warnings": [],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_scan_ethereum_series_returns_list -v
```

Expected: PASS

- [ ] **Step 5: Wire ETH into `scan_series_markets()` and add to ACTIVE_STRATEGIES**

In `scan_series_markets()` in `bot/kalshi_series.py`, after the IPL block:
```python
    eth_opps = scan_ethereum_series()
    print(f"  [Series/ETH] Found {len(eth_opps)} opportunities")
    all_opps.extend(eth_opps)
```

In `bot/config.py`, update `ACTIVE_STRATEGIES`:
```python
ACTIVE_STRATEGIES = ["weather", "vig_stack_series", "series_game_edge", "ipl_game_edge", "eth_price_edge"]
```

In `bot/scanner.py` line 1220, update the strategy check:
```python
if any(s in ACTIVE_STRATEGIES for s in ("series_game_edge", "btc_price_edge", "ipl_game_edge", "eth_price_edge")):
```

- [ ] **Step 6: Run all tests**

```bash
python3 -m pytest tests/test_bot_scanners.py -v
```

Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add bot/kalshi_series.py bot/config.py bot/scanner.py tests/test_bot_scanners.py
git commit -m "feat: add ETH daily price scanner (lognormal model, series ETH)"
```

---

## Task 4: Fix Weather NWS Next-Day Targeting

**The bug:** `scan_weather_markets()` picks the first non-"night" NWS period (typically "Today"), but when the Kalshi market is for *tomorrow's* date, the relevant period is tomorrow's daytime forecast. Using today's temperature for a tomorrow contract gives wrong edge calculations.

**The fix:** Extract the target date from the Kalshi ticker (`KXHIGHNY-26APR06-T67` → Apr 6), then find the NWS period whose `startTime` matches that date.

**Files:**
- Modify: `bot/scanner.py` (fix forecast period selection in `scan_weather_markets()`)
- Test: `tests/test_bot_scanners.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_scanners.py`:
```python
def test_weather_forecast_period_matches_market_date():
    """Verify that the period selector picks the period matching the market date, not always 'Today'."""
    from bot.scanner import _get_forecast_temp_for_date
    from datetime import date

    # Simulated NWS periods — "Today" is Apr 5, next daytime is "Sunday" = Apr 6
    fake_periods = [
        {"name": "Today",         "temperature": 67, "temperatureUnit": "F", "start": "2026-04-05T06:00:00-05:00"},
        {"name": "Tonight",       "temperature": 52, "temperatureUnit": "F", "start": "2026-04-05T18:00:00-05:00"},
        {"name": "Sunday",        "temperature": 71, "temperatureUnit": "F", "start": "2026-04-06T06:00:00-05:00"},
        {"name": "Sunday Night",  "temperature": 55, "temperatureUnit": "F", "start": "2026-04-06T18:00:00-05:00"},
    ]

    # For a market closing on Apr 5, should return Today's temp (67)
    temp_apr5 = _get_forecast_temp_for_date(fake_periods, date(2026, 4, 5))
    assert temp_apr5 == 67, f"Expected 67 for Apr 5, got {temp_apr5}"

    # For a market closing on Apr 6, should return Sunday's temp (71), not Today's (67)
    temp_apr6 = _get_forecast_temp_for_date(fake_periods, date(2026, 4, 6))
    assert temp_apr6 == 71, f"Expected 71 for Apr 6, got {temp_apr6}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_weather_forecast_period_matches_market_date -v
```

Expected: `FAILED — ImportError: cannot import name '_get_forecast_temp_for_date'`

- [ ] **Step 3: Add `_get_forecast_temp_for_date()` to `bot/scanner.py`**

Add this function before `scan_weather_markets()` (around line 228):

```python
def _get_forecast_temp_for_date(periods: list[dict], target_date) -> float | None:
    """
    Find the daytime high forecast temperature for a specific calendar date.

    NWS period startTimes are ISO timestamps. We match the target_date
    against the date portion and pick the first daytime (non-night) period
    on that day.

    Args:
        periods: List of NWS forecast period dicts with 'name', 'temperature', 'start'.
        target_date: datetime.date object for the desired forecast date.

    Returns:
        Temperature in °F for the matching daytime period, or None if not found.
    """
    from datetime import date as _date
    for period in periods:
        # Skip night periods
        if "night" in period.get("name", "").lower():
            continue
        start_str = period.get("start", "") or period.get("startTime", "")
        if not start_str:
            continue
        try:
            # Parse ISO timestamp — handle both offset (+/-HH:MM) and Z formats
            from datetime import datetime as _dt
            start_dt = _dt.fromisoformat(start_str.replace("Z", "+00:00"))
            period_date = start_dt.date()
        except (ValueError, TypeError):
            continue
        if period_date == target_date:
            return period.get("temperature")
    return None
```

- [ ] **Step 4: Run test to verify `_get_forecast_temp_for_date` passes**

```bash
python3 -m pytest tests/test_bot_scanners.py::test_weather_forecast_period_matches_market_date -v
```

Expected: PASS

- [ ] **Step 5: Write integration test for the full weather scanner using correct date targeting**

Add to `tests/test_bot_scanners.py`:
```python
def test_scan_weather_uses_correct_forecast_date():
    """End-to-end: market closing Apr 6 should use Apr 6 forecast (71°F), not Apr 5 (67°F)."""
    from unittest.mock import patch
    from datetime import datetime, timezone
    from bot.scanner import scan_weather_markets

    # Market for Apr 6 — closes in 30 hours from a fake "now" of Apr 5 noon UTC
    fake_market = {
        "ticker": "KXHIGHNY-26APR06-T70",
        "title": "Will the **high temp in NYC** be >70° on Apr 6, 2026?",
        "yes_ask": 25,
        "yes_bid": 22,
        "no_ask": 76,
        "no_bid": 73,
        "volume": 500,
        "open_interest": 40,
        "close_time": "2026-04-06T23:59:00Z",
    }
    fake_forecast = {
        "city": "NYC",
        "periods": [
            {"name": "Today",        "temperature": 67, "temperatureUnit": "F", "start": "2026-04-05T06:00:00-05:00"},
            {"name": "Tonight",      "temperature": 52, "temperatureUnit": "F", "start": "2026-04-05T18:00:00-05:00"},
            {"name": "Sunday",       "temperature": 71, "temperatureUnit": "F", "start": "2026-04-06T06:00:00-05:00"},
            {"name": "Sunday Night", "temperature": 55, "temperatureUnit": "F", "start": "2026-04-06T18:00:00-05:00"},
        ],
    }

    fake_now = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)

    with patch("bot.scanner.get_markets", return_value={"markets": [fake_market]}), \
         patch("bot.scanner._fetch_nws_forecast", return_value=fake_forecast), \
         patch("bot.scanner.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        result = scan_weather_markets()

    # At 71°F forecast (corrected: 69.5°F) vs >70° threshold → P(above 70) ≈ 32%
    # Kalshi ask 25¢ → edge ≈ +28% relative → should appear
    assert len(result) > 0, f"Expected weather opportunity for Apr 6 market, got none"
    assert result[0]["city"] == "NYC"
```

- [ ] **Step 6: Run test to verify it fails** (scanner still uses old period logic)

```bash
python3 -m pytest tests/test_bot_scanners.py::test_scan_weather_uses_correct_forecast_date -v
```

Expected: FAIL (wrong temp used or no result)

- [ ] **Step 7: Update `scan_weather_markets()` in `bot/scanner.py` to use `_get_forecast_temp_for_date`**

Find the forecast temperature extraction block inside `scan_weather_markets()` (around line 356):

Replace:
```python
        # Get forecast temp (use daytime high)
        forecast_temp = None
        for period in forecasts[matched_city]["periods"]:
            if "night" not in period["name"].lower():
                forecast_temp = period["temperature"]
                break
```

With:
```python
        # Get forecast temp for the specific market date (not just "today")
        forecast_temp = None
        market_date = None
        close_str_for_date = market.get("close_time") or market.get("expiration_time", "")
        if close_str_for_date:
            try:
                close_dt_for_date = datetime.fromisoformat(
                    close_str_for_date.replace("Z", "+00:00")
                )
                market_date = close_dt_for_date.date()
            except Exception:
                pass

        if market_date:
            forecast_temp = _get_forecast_temp_for_date(
                forecasts[matched_city]["periods"], market_date
            )

        if forecast_temp is None:
            # Fallback: first daytime period (old behavior)
            for period in forecasts[matched_city]["periods"]:
                if "night" not in period.get("name", "").lower():
                    forecast_temp = period.get("temperature")
                    break
```

- [ ] **Step 8: Run all tests**

```bash
python3 -m pytest tests/test_bot_scanners.py -v
```

Expected: all PASS

- [ ] **Step 9: Commit**

```bash
git add bot/scanner.py tests/test_bot_scanners.py
git commit -m "fix: weather scanner uses NWS forecast period matching market target date"
```

---

## Final Verification

- [ ] **Run full existing test suite to confirm no regressions**

```bash
python3 -m pytest tests/ -v --tb=short
```

Expected: all pre-existing tests still pass

- [ ] **Live smoke test — run one scan cycle and confirm all four scanners appear in output**

```bash
cd /Users/tylergilstrap/Desktop/hustle-agent/hustle-agent
python3 -c "
from bot.scanner import scan_cycle
result = scan_cycle()
print(f'Total opportunities: {len(result[\"opportunities\"])}')
for opp in result['opportunities']:
    print(f'  {opp[\"type\"]}: {opp[\"ticker\"]} rel_edge={opp[\"relative_edge\"]:.1%}')
"
```

Expected output includes lines with `series_game_edge` (NHL), `ipl_game_edge`, `eth_price_edge` (if ETH markets open), and `weather` opportunities.

- [ ] **Final commit**

```bash
git add -A
git commit -m "chore: bot market expansion — NHL, IPL, ETH, weather date fix"
```
