# Kalshi Series Ticker Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace keyword-based single-game search with direct series-ticker fetching to find real single-game edges on KXNBAGAME, KXMLBGAME, KXNHLGAME, KXNCAAMBGAME, KXATPMATCH, KXEUROLEAGUEGAME, and add Bitcoin price edge scanning via KXBTCD vs CoinGecko live price.

**Architecture:** New `bot/kalshi_series.py` owns all series-ticker definitions, market fetching (with pagination), and edge calculation per series type. `bot/scanner.py` calls `scan_series_markets()` from the new module and adds results to `scan_cycle()`. Sports series are priced against Bovada consensus using the existing team alias dicts from `agent/parlay.py`. Bitcoin series are priced using a normal-distribution model anchored on CoinGecko live price + historical daily volatility.

**Tech Stack:** Python, existing `agent.kalshi_client.get_markets`, existing `agent.parlay.NBA_TEAM_ALIASES` / `MLB_TEAM_ALIASES` / `NHL_TEAM_ALIASES`, existing `bot.crypto.fetch_crypto_prices`, `math.erf` for normal CDF (stdlib)

---

## Key facts from API exploration

**Sports game markets** (2 markets per game, one per team):
- Ticker format: `KXNBAGAME-{date}{team1}{team2}-{abbrev}` — last segment is team abbreviation
- YES = that team wins; both markets for same game share same title
- Abbreviations in `NBA_TEAM_ALIASES` as lowercase (`hou` → `Houston Rockets`)
- KXNHLGAME and KXNCAAMBGAME work identically
- ATP, Euroleague: same 2-market-per-game pattern, no Bovada reference → log only

**Bitcoin markets** (price ladder, resolves daily at 5PM EDT):
- Ticker: `KXBTCD-{date}17-T{threshold}` where `17` = 17:00 EDT, threshold is price - 0.01
- Subtitle: `"$X or above"` (always "above" direction)
- 318 total markets across multiple dates; only today's 17:00 ones are actionable
- Filter by close_time == today's 5PM EDT
- CoinGecko currently at $66,781; markets price probability ladder correctly — only near-threshold markets have non-trivial prices

**Series tickers with 0 markets**: KXCOPPAITALIAGAME, KXCOPADELREYGAME, KXCOUPEDEFRANCEGAME, KXTACAPORTGAME — skip silently.

**KXPGATOUR**: Thin markets (1¢ = 1%), not comparable to a consensus price. **Excluded.**
**KXVALORANTGAME, KXSB**: No reliable reference price. **Excluded.**

---

## File Map

| File | Change |
|------|--------|
| `bot/kalshi_series.py` | **Create** — all series-ticker constants, fetching, and edge calculation |
| `bot/scanner.py` | **Modify** — remove old `scan_single_game_markets()`, add call to `scan_series_markets()` |

---

## Task 1: Create `bot/kalshi_series.py` — Series Definitions and Market Fetching

**Files:**
- Create: `bot/kalshi_series.py`

- [ ] **Step 1: Write the file with series definitions and paginated fetching**

```python
"""
Kalshi Series Scanner — Direct Series-Ticker Market Fetching

Replaces keyword-based Kalshi search with targeted series-ticker browsing.
Each series type has its own edge calculation strategy.

Sports series: priced against Bovada consensus via existing team alias dicts.
Bitcoin series: priced against CoinGecko live price using normal distribution.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from agent.kalshi_client import get_markets
from agent.parlay import NBA_TEAM_ALIASES, MLB_TEAM_ALIASES, NHL_TEAM_ALIASES, NCAAB_TEAM_ALIASES
from bot.math_engine import _self_check_edge


# ---------------------------------------------------------------------------
# Series ticker registry
# ---------------------------------------------------------------------------

# Sports game series: each game has exactly 2 markets, one per team.
# YES = that team wins.
SPORTS_GAME_SERIES: dict[str, dict] = {
    "KXNBAGAME":       {"sport": "nba",   "alias_dict": NBA_TEAM_ALIASES},
    "KXMLBGAME":       {"sport": "mlb",   "alias_dict": MLB_TEAM_ALIASES},
    "KXNHLGAME":       {"sport": "nhl",   "alias_dict": NHL_TEAM_ALIASES},
    "KXNCAAMBGAME":    {"sport": "ncaab", "alias_dict": NCAAB_TEAM_ALIASES},
}

# Non-sports game series with 2-market-per-match structure but no Bovada ref.
# These are fetched and logged but not edge-priced.
UNPRICED_GAME_SERIES: list[str] = [
    "KXATPMATCH",
    "KXEUROLEAGUEGAME",
]

# Bitcoin price ladder series
BITCOIN_SERIES = "KXBTCD"

# BTC daily volatility (used for time-scaling uncertainty)
# Approximate 1-day σ for BTC price in USD (historical ~3-4% of price)
BTC_DAILY_VOL_PCT = 0.035   # 3.5% of spot price ≈ 1 std dev per day


def _fetch_series_markets(series_ticker: str, limit: int = 200) -> list[dict]:
    """Fetch all open markets for a series ticker, paginating until exhausted."""
    all_markets = []
    cursor: Optional[str] = None
    for _ in range(10):  # safety cap: 10 pages × 200 = 2000 max
        r = get_markets(status="open", limit=limit, series_ticker=series_ticker, cursor=cursor)
        batch = r.get("markets", [])
        all_markets.extend(batch)
        cursor = r.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(0.3)  # be kind to the API
    return all_markets
```

- [ ] **Step 2: Verify import and series constants work**

```bash
cd ~/Desktop/hustle-agent/hustle-agent && python3 -c "
from bot.kalshi_series import SPORTS_GAME_SERIES, BITCOIN_SERIES, _fetch_series_markets
print('SPORTS_GAME_SERIES:', list(SPORTS_GAME_SERIES.keys()))
print('BITCOIN_SERIES:', BITCOIN_SERIES)
# Fetch a small batch
markets = _fetch_series_markets('KXNBAGAME', limit=5)
print(f'KXNBAGAME sample: {len(markets)} markets')
print(' ', markets[0].get('ticker') if markets else 'none')
"
```

Expected: prints series keys and a real NBA market ticker.

---

## Task 2: Sports Game Series Edge Calculation in `bot/kalshi_series.py`

**Files:**
- Modify: `bot/kalshi_series.py`

- [ ] **Step 1: Add `_extract_team_abbrev()` and `_group_game_markets()` helpers**

Append to `bot/kalshi_series.py`:

```python
def _extract_team_abbrev(ticker: str) -> Optional[str]:
    """
    Extract team abbreviation from Kalshi game market ticker.
    Format: KXNBAGAME-26APR05HOUGSW-HOU → 'HOU'
    """
    parts = ticker.rsplit("-", 1)
    return parts[1] if len(parts) == 2 else None


def _group_game_markets(markets: list[dict]) -> dict[str, list[dict]]:
    """
    Group markets into game buckets.
    KXNBAGAME-26APR05HOUGSW-HOU and KXNBAGAME-26APR05HOUGSW-GSW
    both belong to game 'KXNBAGAME-26APR05HOUGSW'.

    Returns {game_prefix: [market, market]} — always 2 markets per game.
    """
    buckets: dict[str, list[dict]] = {}
    for m in markets:
        ticker = m.get("ticker", "")
        parts = ticker.rsplit("-", 1)
        if len(parts) == 2:
            game_key = parts[0]
            buckets.setdefault(game_key, []).append(m)
    return {k: v for k, v in buckets.items() if len(v) == 2}
```

- [ ] **Step 2: Add `scan_sports_series()` function**

Append to `bot/kalshi_series.py`:

```python
def scan_sports_series(
    series_ticker: str,
    odds_data: dict,
    min_relative_edge: float = 0.15,
) -> list[dict]:
    """
    Scan a sports game series ticker (e.g. KXNBAGAME) and find edges.

    Algorithm:
    1. Fetch all open markets for the series (paginated).
    2. Group into game buckets (2 markets per game).
    3. For each game: look up team abbrev → canonical name → Bovada consensus prob.
    4. Compare Kalshi yes_ask to consensus prob, calculate edge.
    5. Return opportunities meeting min_relative_edge threshold.

    Args:
        series_ticker: e.g. "KXNBAGAME"
        odds_data: from fetch_consensus_odds() — contains games with consensus probs
        min_relative_edge: minimum |relative_edge| to report (default 15%)

    Returns:
        list of opportunity dicts compatible with scanner.py sanity filter
    """
    series_info = SPORTS_GAME_SERIES.get(series_ticker, {})
    alias_dict = series_info.get("alias_dict", {})

    # Build lookup: canonical_name.lower() → (game, probability)
    team_to_prob: dict[str, tuple[dict, float]] = {}
    for game in odds_data.get("games", []):
        for team_name, prob in game.get("consensus", {}).items():
            team_to_prob[team_name.lower()] = (game, prob)
            # Also index by last word (e.g. "Warriors" for "Golden State Warriors")
            last_word = team_name.split()[-1].lower()
            if len(last_word) >= 4:
                team_to_prob.setdefault(last_word, (game, prob))

    print(f"  [Series/{series_ticker}] Fetching markets...")
    all_markets = _fetch_series_markets(series_ticker)
    game_buckets = _group_game_markets(all_markets)
    print(
        f"  [Series/{series_ticker}] {len(all_markets)} markets → "
        f"{len(game_buckets)} game buckets"
    )

    opportunities = []

    for game_key, pair in game_buckets.items():
        for market in pair:
            ticker = market.get("ticker", "")
            yes_ask = market.get("yes_ask")
            if not yes_ask or yes_ask <= 0:
                continue

            abbrev = _extract_team_abbrev(ticker)
            if not abbrev:
                continue

            # Look up canonical team name via alias dict
            canonical = alias_dict.get(abbrev.lower())
            if not canonical:
                # Try common alternate forms
                canonical = alias_dict.get(abbrev[:3].lower())
            if not canonical:
                print(
                    f"  [Series/{series_ticker}] SKIP {ticker}: "
                    f"no alias for abbrev '{abbrev}'"
                )
                continue

            # Look up sportsbook consensus probability
            match = team_to_prob.get(canonical.lower())
            if not match:
                # Try matching by last word of canonical name
                last = canonical.split()[-1].lower()
                match = team_to_prob.get(last)
            if not match:
                print(
                    f"  [Series/{series_ticker}] SKIP {ticker}: "
                    f"'{canonical}' not in odds data"
                )
                continue

            game_data, sportsbook_prob = match

            kalshi_price = yes_ask / 100.0
            edge = sportsbook_prob - kalshi_price
            relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

            # Self-check
            check_ok, check_msg = _self_check_edge(sportsbook_prob, kalshi_price, edge)
            if not check_ok:
                continue

            # Near-zero guard (min 3¢)
            if kalshi_price <= 0.03 or sportsbook_prob <= 0.03:
                continue

            title = market.get("title", "")
            print(
                f"  [Series/{series_ticker}] {ticker} | {canonical} | "
                f"sportsbook={sportsbook_prob:.3f} kalshi={kalshi_price:.3f} "
                f"edge={edge:+.3f} rel={relative_edge:+.1%}"
            )

            if abs(relative_edge) < min_relative_edge:
                continue

            game_status = game_data.get("status", "STATUS_SCHEDULED")
            is_live = game_status in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")

            opportunities.append({
                "type": "series_game_edge",
                "ticker": ticker,
                "title": title,
                "market": market,
                "series_ticker": series_ticker,
                "team": canonical,
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": 0.80,
                "recommended_side": "yes" if edge > 0 else "no",
                "sportsbook_prob": round(sportsbook_prob, 4),
                "kalshi_price": round(kalshi_price, 4),
                "game": {
                    "home_team": game_data.get("home_team"),
                    "away_team": game_data.get("away_team"),
                    "status": game_status,
                    "commence_time": game_data.get("commence_time"),
                },
                "edge_result": {
                    "fair_value": round(sportsbook_prob, 4),
                    "kalshi_price": round(kalshi_price, 4),
                    "edge": round(edge, 4),
                    "relative_edge": round(relative_edge, 4),
                    "confidence": 0.80,
                    "self_check_passed": True,
                    "math_chain": [check_msg],
                    "warnings": [],
                },
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            })

    return opportunities
```

- [ ] **Step 3: Test sports series scanner against live odds**

```bash
cd ~/Desktop/hustle-agent/hustle-agent && python3 -c "
from bot.odds_scraper import fetch_consensus_odds
from bot.kalshi_series import scan_sports_series

odds = fetch_consensus_odds('nba')
wo = [g for g in odds.get('games',[]) if g.get('consensus')]
print(f'NBA odds: {len(wo)} games, source={odds.get(\"source\")}')
opps = scan_sports_series('KXNBAGAME', odds)
print(f'NBA series opportunities: {len(opps)}')
for o in opps[:5]:
    print(f'  {o[\"ticker\"]} | {o[\"team\"]} | sports={o[\"sportsbook_prob\"]:.3f} kalshi={o[\"kalshi_price\"]:.3f} edge={o[\"relative_edge\"]:+.1%}')
" 2>&1 | grep -v NotOpenSSLWarning | grep -v warnings.warn
```

Expected: prints market-by-market comparison with sportsbook vs Kalshi probabilities. Any opportunities where `|rel_edge| >= 15%` appear in the list.

---

## Task 3: Bitcoin Series Edge Calculation in `bot/kalshi_series.py`

**Files:**
- Modify: `bot/kalshi_series.py`

The KXBTCD markets resolve at 5PM EDT based on a 60-second BRTI average. We model the final price as normally distributed around the current spot price, with volatility scaled to remaining hours.

- [ ] **Step 1: Add `_btc_normal_prob()` helper**

Append to `bot/kalshi_series.py`:

```python
def _btc_normal_prob(spot: float, threshold: float, hours_remaining: float) -> float:
    """
    P(BTC price at resolution > threshold) using normal distribution.

    Models BTC as a geometric Brownian motion with daily vol = BTC_DAILY_VOL_PCT.
    Scales volatility to remaining time: σ = spot × daily_vol × sqrt(hours / 24).

    Args:
        spot: Current BTC spot price (USD)
        threshold: Market threshold price (USD)
        hours_remaining: Hours until 5PM EDT resolution

    Returns:
        Probability in [0, 1] that BTC closes above threshold
    """
    # Clip remaining time: minimum 5 minutes to avoid division by near-zero
    hours_remaining = max(hours_remaining, 5 / 60)
    sigma = spot * BTC_DAILY_VOL_PCT * math.sqrt(hours_remaining / 24.0)
    if sigma <= 0:
        return 1.0 if spot > threshold else 0.0
    z = (threshold - spot) / sigma
    # P(above) = 1 - CDF(z) = 0.5 * erfc(z / sqrt(2))
    p_above = 0.5 * math.erfc(z / math.sqrt(2))
    return p_above
```

- [ ] **Step 2: Add `scan_bitcoin_series()` function**

Append to `bot/kalshi_series.py`:

```python
def scan_bitcoin_series(min_relative_edge: float = 0.15) -> list[dict]:
    """
    Scan KXBTCD Bitcoin price markets and compare to CoinGecko live price.

    Only scans today's 17:00 EDT markets (close_time = today ~21:00 UTC).
    Computes P(BTC > threshold) via normal distribution, compares to Kalshi yes_ask.

    Returns:
        list of opportunity dicts compatible with scanner.py sanity filter
    """
    from bot.crypto import fetch_crypto_prices

    # Get current BTC price
    prices = fetch_crypto_prices(["bitcoin"])
    if "error" in prices or "bitcoin" not in prices:
        print(f"  [BTC] CoinGecko fetch failed: {prices}")
        return []
    spot = prices["bitcoin"]["usd"]
    print(f"  [BTC] CoinGecko BTC spot: ${spot:,.0f}")

    # Compute resolution time: today at 21:00 UTC = 5PM EDT
    now_utc = datetime.now(timezone.utc)
    resolution_today = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
    if now_utc > resolution_today:
        print("  [BTC] Today's 5PM EDT resolution already passed — no markets to scan")
        return []
    hours_remaining = (resolution_today - now_utc).total_seconds() / 3600
    print(f"  [BTC] Hours until 5PM EDT resolution: {hours_remaining:.2f}h")

    # Fetch only today's 17:00 markets
    all_markets = _fetch_series_markets(BITCOIN_SERIES, limit=200)
    # Filter: close_time == today 21:00 UTC (5PM EDT)
    today_str = now_utc.strftime("%Y-%m-%d")
    target_close = f"{today_str}T21:00:00Z"
    today_markets = [
        m for m in all_markets
        if m.get("close_time", "") == target_close
    ]
    print(
        f"  [BTC] {len(all_markets)} total KXBTCD markets → "
        f"{len(today_markets)} closing today at 5PM EDT"
    )

    opportunities = []

    for market in today_markets:
        ticker = market.get("ticker", "")
        yes_ask = market.get("yes_ask")
        if not yes_ask or yes_ask <= 0:
            continue

        # Parse threshold from ticker: KXBTCD-26APR0417-T66249.99
        # Threshold = float of part after 'T', which is (price - 0.01)
        # Subtitle: "$66,250 or above" → actual resolution threshold is 66249.99
        m_thresh = re.search(r"-T([\d.]+)$", ticker)
        if not m_thresh:
            continue
        threshold = float(m_thresh.group(1))

        kalshi_price = yes_ask / 100.0
        fair_value = _btc_normal_prob(spot, threshold, hours_remaining)

        edge = fair_value - kalshi_price
        relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

        # Self-checks
        check_ok, check_msg = _self_check_edge(fair_value, kalshi_price, edge)
        if not check_ok:
            continue

        # Near-zero guard (min 3¢ on either side)
        if kalshi_price <= 0.03 or fair_value <= 0.03:
            continue
        # Also skip near-certain markets (>97¢) — liquidity is thin, hard to trade
        if kalshi_price >= 0.97 or fair_value >= 0.97:
            continue

        print(
            f"  [BTC] {ticker} | threshold=${threshold:,.2f} | "
            f"spot=${spot:,.0f} | fair={fair_value:.3f} kalshi={kalshi_price:.3f} "
            f"edge={edge:+.3f} rel={relative_edge:+.1%}"
        )

        if abs(relative_edge) < min_relative_edge:
            continue

        subtitle = market.get("subtitle") or f"${threshold + 0.01:,.2f} or above"
        opportunities.append({
            "type": "btc_price_edge",
            "ticker": ticker,
            "title": market.get("title", ""),
            "subtitle": subtitle,
            "market": market,
            "series_ticker": BITCOIN_SERIES,
            "threshold": threshold,
            "spot_price": spot,
            "hours_remaining": round(hours_remaining, 2),
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "confidence": 0.65,  # BTC is volatile — moderate confidence
            "recommended_side": "yes" if edge > 0 else "no",
            "fair_value": round(fair_value, 4),
            "kalshi_price": round(kalshi_price, 4),
            "edge_result": {
                "fair_value": round(fair_value, 4),
                "kalshi_price": round(kalshi_price, 4),
                "edge": round(edge, 4),
                "relative_edge": round(relative_edge, 4),
                "confidence": 0.65,
                "self_check_passed": True,
                "math_chain": [
                    f"BTC spot=${spot:,.0f} threshold=${threshold:,.2f} "
                    f"hours_remaining={hours_remaining:.2f}",
                    check_msg,
                ],
                "warnings": [
                    "BTC is highly volatile — confidence capped at 0.65",
                    f"σ ≈ ${spot * BTC_DAILY_VOL_PCT * math.sqrt(hours_remaining/24):,.0f} "
                    f"for {hours_remaining:.1f}h window",
                ],
            },
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    return opportunities
```

- [ ] **Step 3: Test Bitcoin scanner**

```bash
cd ~/Desktop/hustle-agent/hustle-agent && python3 -c "
from bot.kalshi_series import scan_bitcoin_series
opps = scan_bitcoin_series()
print(f'BTC opportunities: {len(opps)}')
for o in opps[:5]:
    print(f'  {o[\"ticker\"]} | thresh=\${o[\"threshold\"]:,.0f} spot=\${o[\"spot_price\"]:,.0f} fair={o[\"fair_value\"]:.3f} kalshi={o[\"kalshi_price\"]:.3f} edge={o[\"relative_edge\"]:+.1%}')
" 2>&1 | grep -v NotOpenSSLWarning | grep -v warnings.warn
```

Expected: BTC spot logged, list of near-threshold markets with edge calculations printed. Opportunities are any where `|rel_edge| >= 15%`.

---

## Task 4: Top-Level `scan_series_markets()` Orchestrator in `bot/kalshi_series.py`

**Files:**
- Modify: `bot/kalshi_series.py`

- [ ] **Step 1: Add `scan_series_markets()` orchestrator**

Append to `bot/kalshi_series.py`:

```python
def scan_series_markets(odds_by_sport: dict[str, dict], min_relative_edge: float = 0.15) -> list[dict]:
    """
    Scan all configured series tickers and return a flat list of opportunities.

    Args:
        odds_by_sport: {"nba": odds_data, "mlb": odds_data, ...}
                       as returned by fetch_consensus_odds() per sport.
        min_relative_edge: Minimum |relative_edge| to include.

    Returns:
        Flat list of opportunity dicts, sorted by |relative_edge| descending.
    """
    all_opportunities = []

    # Sports game series
    for series_ticker, info in SPORTS_GAME_SERIES.items():
        sport = info["sport"]
        odds_data = odds_by_sport.get(sport, {})
        if not odds_data.get("games"):
            print(
                f"  [Series/{series_ticker}] No odds data for {sport.upper()} — skipping"
            )
            continue
        try:
            opps = scan_sports_series(series_ticker, odds_data, min_relative_edge)
            all_opportunities.extend(opps)
            print(
                f"  [Series/{series_ticker}] → {len(opps)} opportunities"
            )
        except Exception as e:
            print(f"  [Series/{series_ticker}] ERROR: {e}")

    # Unpriced game series — fetch and log but don't edge-price
    for series_ticker in UNPRICED_GAME_SERIES:
        try:
            markets = _fetch_series_markets(series_ticker, limit=50)
            buckets = _group_game_markets(markets)
            priced = [
                m for bucket in buckets.values()
                for m in bucket
                if (m.get("yes_ask") or 0) > 0
            ]
            print(
                f"  [Series/{series_ticker}] {len(markets)} markets, "
                f"{len(buckets)} games, {len(priced)} with prices — no reference odds, logged only"
            )
            if priced:
                for m in priced[:3]:
                    print(
                        f"    {m.get('ticker')} | yes_ask={m.get('yes_ask')}¢ | "
                        f"{m.get('title','')[:60]}"
                    )
        except Exception as e:
            print(f"  [Series/{series_ticker}] ERROR: {e}")

    # Bitcoin price series
    try:
        btc_opps = scan_bitcoin_series(min_relative_edge)
        all_opportunities.extend(btc_opps)
        print(f"  [Series/KXBTCD] → {len(btc_opps)} opportunities")
    except Exception as e:
        print(f"  [Series/KXBTCD] ERROR: {e}")

    all_opportunities.sort(key=lambda x: abs(x.get("relative_edge", 0)), reverse=True)
    return all_opportunities
```

- [ ] **Step 2: Test full orchestrator**

```bash
cd ~/Desktop/hustle-agent/hustle-agent && python3 -c "
from bot.odds_scraper import fetch_consensus_odds
from bot.kalshi_series import scan_series_markets

odds_by_sport = {}
for sport in ['nba', 'mlb', 'nhl', 'ncaab']:
    odds_by_sport[sport] = fetch_consensus_odds(sport)

opps = scan_series_markets(odds_by_sport)
print(f'Total series opportunities: {len(opps)}')
for o in opps[:10]:
    print(f'  [{o[\"type\"]}] {o[\"ticker\"]} edge={o[\"relative_edge\"]:+.1%} side={o[\"recommended_side\"]}')
" 2>&1 | grep -v NotOpenSSLWarning | grep -v warnings.warn
```

Expected: no crashes, real market output with edge comparisons, opportunities list.

---

## Task 5: Wire `scan_series_markets()` into `bot/scanner.py`

**Files:**
- Modify: `bot/scanner.py`

- [ ] **Step 1: Import `scan_series_markets` at top of scanner.py**

In `bot/scanner.py`, add to the import block (after the `from bot.math_engine import ...` line):

```python
from bot.kalshi_series import scan_series_markets
```

- [ ] **Step 2: Remove old `scan_single_game_markets()` function and its scan_cycle() call**

In `bot/scanner.py`:
- Delete the `_SINGLE_GAME_QUERIES` dict
- Delete the `_PROP_QUERIES` list
- Delete the entire `scan_single_game_markets()` function
- In `scan_cycle()`, remove these two lines:
  ```python
  print(f"  [{sport.upper()}] Scanning single-game / prop markets...")
  single_opps = scan_single_game_markets(sport, odds_data)
  all_opportunities.extend(single_opps)
  print(f"  [{sport.upper()}] Found {len(single_opps)} single-game/prop opportunities")
  ```

- [ ] **Step 3: Add series scan call and odds accumulation in `scan_cycle()`**

In `scan_cycle()`, the function currently iterates sports one at a time. We need to:
1. Collect each sport's `odds_data` into a dict as we go
2. After the per-sport loop, call `scan_series_markets()`

**Change 1:** Add `odds_by_sport: dict[str, dict] = {}` at the top of `scan_cycle()`, after `all_games = []`.

**Change 2:** Inside the per-sport loop, after `all_games.extend(games)`, add:
```python
        odds_by_sport[sport] = odds_data
```

**Change 3:** After the per-sport loop and before the weather scan section, add:
```python
    # Scan individual game series tickers (NBA/MLB/NHL/NCAAB/BTC)
    print(f"\n  [SERIES] Scanning series-ticker markets...")
    series_opps = scan_series_markets(odds_by_sport, min_relative_edge=MIN_RELATIVE_EDGE)
    all_opportunities.extend(series_opps)
    print(f"  [SERIES] Found {len(series_opps)} series opportunities")
```

- [ ] **Step 4: Verify scan_cycle imports and runs**

```bash
cd ~/Desktop/hustle-agent/hustle-agent && python3 -c "
from bot.scanner import scan_cycle
" 2>&1 | grep -v NotOpenSSLWarning | grep -v warnings.warn
```

Expected: no import errors.

---

## Task 6: Full Scan Cycle Smoke Test

- [ ] **Step 1: Run full scan cycle and verify series markets appear**

```bash
cd ~/Desktop/hustle-agent/hustle-agent && python3 -c "
from bot.scanner import scan_cycle
result = scan_cycle(['nba', 'mlb'])
print(f'Opportunities: {len(result[\"opportunities\"])}')
print(f'Games scanned: {result[\"games_scanned\"]}')
types = {}
for o in result['opportunities']:
    t = o.get('type','?')
    types[t] = types.get(t,0) + 1
print('By type:', types)
for o in result['opportunities'][:8]:
    print(f'  [{o[\"type\"]}] {o[\"ticker\"]} | edge={o.get(\"relative_edge\",0):+.1%} | {o.get(\"title\",\"\")[:50]}')
" 2>&1 | grep -v NotOpenSSLWarning | grep -v warnings.warn
```

Expected:
- `types` dict includes `series_game_edge` and/or `btc_price_edge` alongside other types
- No crashes
- If 0 opportunities: edge logs still show sportsbook vs Kalshi comparison values for each market

---

## Verification Checklist

- [ ] `from bot.kalshi_series import scan_series_markets` imports cleanly
- [ ] `scan_sports_series('KXNBAGAME', odds_data)` logs every game market with sportsbook vs Kalshi comparison
- [ ] `scan_bitcoin_series()` logs BTC spot price and all near-threshold markets with edge calculations
- [ ] `scan_series_markets(odds_by_sport)` returns list without exceptions even when all sports have 0 odds
- [ ] `scan_cycle(['nba', 'mlb'])` completes without exceptions and types dict is populated
- [ ] Sanity filter in `scan_cycle()` properly evaluates `edge_result.self_check_passed = True` for series opportunities
