# Print → Logger Migration (Session 9) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate all remaining `print()` calls in the bot's production modules to named loggers, completing the logging hygiene work started in Session 8.

**Architecture:** Six files still use raw `print()` — scanner.py, kalshi_series.py, odds_scraper.py, elo.py, injuries.py, econ_scanner.py. odds_scraper.py already has a logger configured; the rest need `import logging` + `logger = logging.getLogger(...)` added. Each task follows the same TDD pattern: write a capsys test that asserts no stdout, confirm it fails, migrate the prints, confirm it passes. Also rename the misleadingly-named `print_calibration_summary` in outcome_tracker.py.

**Logger naming convention:**
- `glint.*` — user-facing/scanner modules (scanner.py → `glint.scanner`, econ_scanner.py → `glint.econ_scanner`, odds_scraper already `glint.odds_scraper`)
- `nexus.*` — internal bot modules (kalshi_series.py → `nexus.kalshi_series`, elo.py → `nexus.elo`, injuries.py → `nexus.injuries`)

**Tech Stack:** Python, logging (stdlib), unittest.mock, pytest capsys

---

## Files Modified

| File | Logger name | print() count | Has logging? |
|------|-------------|---------------|--------------|
| `bot/scanner.py` | `glint.scanner` | 31 | No |
| `bot/kalshi_series.py` | `nexus.kalshi_series` | 38 | No |
| `bot/odds_scraper.py` | `glint.odds_scraper` | 22 | **Yes** (already) |
| `bot/elo.py` | `nexus.elo` | 5 | No |
| `bot/injuries.py` | `nexus.injuries` | 5 | No |
| `bot/econ_scanner.py` | `glint.econ_scanner` | 10 | No |
| `bot/outcome_tracker.py` | (existing) | rename only | Yes |
| `bot/main.py` | (existing) | 1-line rename | Yes |
| `tests/test_bot_scanners.py` | — | add 3 tests | — |
| `tests/test_bot_improvements.py` | — | add 3 tests | — |

---

## Task 1: scanner.py — migrate to glint.scanner

**Files:**
- Modify: `hustle-agent/bot/scanner.py`
- Test: `hustle-agent/tests/test_bot_scanners.py`

- [ ] **Step 1: Write the failing test**

Add to `hustle-agent/tests/test_bot_scanners.py`:

```python
def test_scanner_morning_scan_uses_logger_not_print(capsys):
    """scanner.morning_weather_scan must not emit any print() output."""
    from unittest.mock import patch
    import bot.scanner as sc

    with patch.object(sc, "scan_weather_markets", return_value=[]):
        sc.morning_weather_scan()

    captured = capsys.readouterr()
    assert captured.out == "", f"scanner printed to stdout: {captured.out!r}"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_scanners.py::test_scanner_morning_scan_uses_logger_not_print -v
```

Expected: FAIL — `morning_weather_scan` prints `\n  [MORNING] Weather-only scan...` and `  [MORNING] Found 0 weather opportunities`

- [ ] **Step 3: Add logger to scanner.py**

At the top of `hustle-agent/bot/scanner.py`, after the existing imports, add:

```python
import logging
logger = logging.getLogger("glint.scanner")
```

- [ ] **Step 4: Replace all print() calls**

Apply these replacements throughout `hustle-agent/bot/scanner.py`:

**Scan cycle banner (lines 497-499) — three separate prints → one logger.info:**
```python
# OLD:
print(f"\n{'='*60}")
print(f"SCAN CYCLE — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
print(f"{'='*60}")

# NEW:
logger.info("SCAN CYCLE — %s", datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
```

**Scan cycle summary (lines 698-699) — two prints → one logger.info:**
```python
# OLD:
print(f"\n  SUMMARY: {len(all_opportunities)} opportunities | {len(line_movements)} line moves | Next scan: {interval_label}")
print(f"{'='*60}\n")

# NEW:
logger.info("SUMMARY: %d opportunities | %d line moves | next scan: %s",
            len(all_opportunities), len(line_movements), interval_label)
```

**VigStack error:**
```python
# OLD:
print(f"  [VigStack/{series_ticker}] API error: {e}")
# NEW:
logger.warning("VigStack/%s API error: %s", series_ticker, e)
```

**VigStack contract summary:**
```python
# OLD:
print(
    f"  [VigStack/{series_ticker}] {len(valid_markets)} contracts | YES sum={yes_sum}¢ ({yes_sum_prob:.1%}) | vig_excess={vig_excess_cents}¢"
)
# NEW:
logger.info("VigStack/%s: %d contracts | YES sum=%s¢ (%.1f%%) | vig_excess=%s¢",
            series_ticker, len(valid_markets), yes_sum, yes_sum_prob * 100, vig_excess_cents)
```

**VigStack per-market edge:**
```python
# OLD:
print(
    f"  [VigStack/{ticker}] YES_ask={yes_ask}¢ YES_fair={yes_fair_cents:.1f}¢ NO_fair={no_fair_cents:.1f}¢ NO_ask={no_ask}¢ edge={relative_no_edge:+.1%}"
)
# NEW:
logger.info("VigStack/%s: YES_ask=%s¢ YES_fair=%.1f¢ NO_fair=%.1f¢ NO_ask=%s¢ edge=%+.1f%%",
            ticker, yes_ask, yes_fair_cents, no_fair_cents, no_ask, relative_no_edge * 100)
```

**All remaining print() calls — bulk replacement table:**

| Old prefix | New call | Level |
|-----------|----------|-------|
| `[PARLAYS] Pre-fetching` | `logger.info("Pre-fetching parlay markets from Kalshi...")` | info |
| `[PARLAYS] Found` | `logger.info("Found %d total parlay markets", len(...))` | info |
| `[{sport}] Fetching odds` | `logger.info("[%s] Fetching odds...", sport.upper())` | info |
| `[{sport}] Error fetching odds` | `logger.warning("[%s] Error fetching odds: %s", sport.upper(), e)` | warning |
| `[{sport}] {odds_data['error']}` | `logger.warning("[%s] %s", sport.upper(), odds_data['error'])` | warning |
| `[{sport}] N games` | `logger.info("[%s] %d games (%d with odds) | source: %s", ...)` | info |
| `[{sport}] N significant line movements` | `logger.info("[%s] %d significant line movements detected", ...)` | info |
| `[{sport}] Scanning parlays` | `logger.info("[%s] Scanning parlays...", ...)` | info |
| `[{sport}] Found N parlay opps` | `logger.info("[%s] Found %d parlay opportunities", ...)` | info |
| `[{sport}] Scanning live game markets` | `logger.info("[%s] Scanning live game markets...", ...)` | info |
| `[{sport}] Found N live opps` | `logger.info("[%s] Found %d live latency arb opportunities", ...)` | info |
| `[{sport}] Scanning single-game` | `logger.info("[%s] Scanning single-game / prop markets...", ...)` | info |
| `[{sport}] Found N single opps` | `logger.info("[%s] Found %d single-game/prop opportunities", ...)` | info |
| `[SPORTS] Disabled` | `logger.info("SPORTS disabled — not in ACTIVE_STRATEGIES")` | info |
| `[WEATHER] Scanning` | `logger.info("Scanning weather markets...")` | info |
| `[WEATHER] Found` | `logger.info("Found %d weather opportunities", ...)` | info |
| `[VIG_STACK] Suppressed N signals` | `logger.info("VigStack suppressed %d signal(s): weather model conflicts", ...)` | info |
| `[VIG_STACK] Found N opps` | `logger.info("VigStack found %d structural vig stack opportunities", ...)` | info |
| `[SERIES] Scanning` | `logger.info("Scanning Kalshi series markets...")` | info |
| `[SERIES] Found N opps` | `logger.info("Found %d total series opportunities", ...)` | info |
| `[SERIES] Disabled` | `logger.info("SERIES disabled — not in ACTIVE_STRATEGIES")` | info |
| `[ECON] Scanning` | `logger.info("Scanning economic markets...")` | info |
| `[ECON] Found N opps` | `logger.info("Found %d econ opportunities", ...)` | info |
| `[INJURIES] Checking` | `logger.info("Checking injury reports for series game edges...")` | info |
| `[INJURIES] STALE {ticker}` | `logger.warning("STALE %s — %s", ticker, warning_msg)` | warning |
| `[INJURIES] Dropped N STALE` | `logger.info("Dropped %d STALE opportunities (injury-flagged)", ...)` | info |
| `[INJURIES] No injury-stale` | `logger.info("No injury-stale opportunities found")` | info |
| `[INJURIES] Injury check error` | `logger.warning("Injury check error (fail-open): %s", e)` | warning |
| `[GATE] Dropped N opps` | `logger.info("Gate dropped %d opportunities from inactive strategies", ...)` | info |
| `[SANITY] Dropped N opps` | `logger.info("Sanity dropped %d opportunities with failed self-checks", ...)` | info |
| `[MORNING] Weather-only scan` | `logger.info("Morning weather-only scan...")` | info |
| `[MORNING] Found N opps` | `logger.info("Morning scan found %d weather opportunities", ...)` | info |

- [ ] **Step 5: Verify no print() remain**

```bash
grep -n "^\s*print(" hustle-agent/bot/scanner.py
```

Expected: no output

- [ ] **Step 6: Run the test**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_scanners.py::test_scanner_morning_scan_uses_logger_not_print -v
```

Expected: PASS

- [ ] **Step 7: Run full suite**

```bash
cd hustle-agent && python3 -m pytest tests/ --tb=short 2>&1 | tail -8
```

Expected: same count as before (401), 0 failures

- [ ] **Step 8: Commit**

```bash
cd hustle-agent && git add bot/scanner.py tests/test_bot_scanners.py
git commit -m "feat: migrate scanner.py print() to glint.scanner logger"
```

---

## Task 2: kalshi_series.py — migrate to nexus.kalshi_series

**Files:**
- Modify: `hustle-agent/bot/kalshi_series.py`
- Test: `hustle-agent/tests/test_bot_scanners.py`

- [ ] **Step 1: Write the failing test**

Add to `hustle-agent/tests/test_bot_scanners.py`:

```python
def test_kalshi_series_uses_logger_not_print(capsys):
    """scan_series_markets with all sub-scanners mocked must not print to stdout."""
    from unittest.mock import patch
    import bot.kalshi_series as ks

    with patch.object(ks, "scan_sports_series", return_value=[]), \
         patch.object(ks, "scan_bitcoin_series", return_value=[]), \
         patch.object(ks, "scan_ethereum_series", return_value=[]), \
         patch.object(ks, "scan_ipl_series", return_value=[]):
        ks.scan_series_markets()

    captured = capsys.readouterr()
    assert captured.out == "", f"kalshi_series printed to stdout: {captured.out!r}"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_scanners.py::test_kalshi_series_uses_logger_not_print -v
```

Expected: FAIL — `scan_series_markets` prints summary lines for BTC, IPL, ETH, and sport series

- [ ] **Step 3: Add logger to kalshi_series.py**

After the existing imports in `hustle-agent/bot/kalshi_series.py`, add:

```python
import logging
logger = logging.getLogger("nexus.kalshi_series")
```

- [ ] **Step 4: Replace all print() calls**

Apply these replacements throughout `hustle-agent/bot/kalshi_series.py`:

**HTTP/fetch errors → warning:**
```python
# OLD: print(f"  [SeriesHTTP] Error fetching {url[:80]}: {e}")
logger.warning("HTTP error fetching %s: %s", url[:80], e)

# OLD: print(f"  [Series] Error fetching {series_ticker}: {result['error']}")
logger.warning("Error fetching %s: %s", series_ticker, result['error'])
```

**ActionNetwork / OddsAPI build lookup:**
```python
# OLD: print(f"  [ActionNetwork/{sport.upper()}] Fetch failed: {e}")
logger.warning("ActionNetwork/%s fetch failed: %s", sport.upper(), e)

# OLD: print(f"  [OddsAPI/{sport.upper()}] ActionNetwork: {len(an_games)} games | {len(lookup)//2} teams")
logger.info("OddsAPI/%s ActionNetwork: %d games | %d teams", sport.upper(), len(an_games), len(lookup)//2)

# OLD: print(f"  [OddsAPI/{sport.upper()}] ActionNetwork: no data")
logger.info("OddsAPI/%s ActionNetwork: no data", sport.upper())

# OLD: print(f"  [OddsAPI/{sport.upper()}] Fetching from The Odds API...")
logger.info("OddsAPI/%s fetching from The Odds API...", sport.upper())

# OLD: print(f"  [OddsAPI/{sport.upper()}] Got {len(raw_games)} games | ...")
logger.info("OddsAPI/%s got %d games | %d teams | %d no-consensus",
            sport.upper(), len(raw_games), len(lookup)//2, skipped_no_consensus)

# OLD: print(f"  [OddsAPI/{sport.upper()}] ERROR: {result['error']}")
logger.warning("OddsAPI/%s ERROR: %s", sport.upper(), result['error'])

# OLD: print(f"  [OddsAPI/{sport.upper()}] Exception: {e}")
logger.warning("OddsAPI/%s exception: %s", sport.upper(), e)

# OLD: print(f"  [OddsAPI/{sport.upper()}] +{source} overlay: {len(lookup)//2} total teams")
logger.info("OddsAPI/%s +%s overlay: %d total teams", sport.upper(), source, len(lookup)//2)
```

**scan_sports_series internals:**
```python
# OLD: print(f"  [Series/{sport.upper()}] No open markets for {series_ticker}")
logger.info("Series/%s no open markets for %s", sport.upper(), series_ticker)

# OLD: print(f"  [Series/{sport.upper()}] {len(markets)} open markets | ...")
logger.info("Series/%s: %d open markets | %d Bovada games | %d Odds API teams",
            sport.upper(), len(markets), len(game_lines), len(odds_api_lookup)//2)

# OLD: print(f"    [Series/{sport.upper()}] SKIP {ticker}: no match ...")  (line 619)
logger.debug("Series/%s SKIP %s: no match abbrev=%r canonical=%r",
             sport.upper(), ticker, abbrev, canonical)

# OLD: print(f"    [Series/{sport.upper()}] SKIP {ticker}: game started ... ago")  (line 661)
logger.debug("Series/%s SKIP %s: game started %.1fh ago (stale odds)",
             sport.upper(), ticker, -hours_to_game)

# OLD: print(f"    [Series/{sport.upper()}] B2B: {canonical}")  (line 673)
logger.debug("Series/%s B2B: %s", sport.upper(), canonical)

# OLD: print(f"    [Series/{sport.upper()}] SKIP {ticker}: relative_edge ... sanity cap")  (line 698)
logger.debug("Series/%s SKIP %s: relative_edge=%.1f%% exceeds sanity cap",
             sport.upper(), ticker, relative_edge * 100)

# OLD: print(f"    [Series/{sport.upper()}] B2B (opp): {opp_name}")  (line 733)
logger.debug("Series/%s B2B (opp): %s", sport.upper(), opp_name)
```

**BTC series:**
```python
# OLD: print(f"  [Series/BTC] Realized 10d vol: {daily_vol:.2%} ...")
logger.info("Series/BTC realized 10d vol: %.2f%% (from %d closes)", daily_vol * 100, len(prices))

# OLD: print(f"  [Series/BTC] No open markets for {BTC_SERIES}")
logger.info("Series/BTC no open markets for %s", BTC_SERIES)

# OLD: print(f"  [Series/BTC] Could not fetch BTC spot price")
logger.warning("Series/BTC could not fetch BTC spot price")

# OLD: print(f"  [Series/BTC] All today's markets already resolved (past 21:00 UTC)")
logger.info("Series/BTC all today's markets already resolved (past 21:00 UTC)")

# OLD: print(f"  [Series/BTC] {len(markets)} open markets | spot=${spot:,.0f} | ...")
logger.info("Series/BTC: %d open markets | spot=$%s | hours_remaining=%.1fh",
            len(markets), f"{spot:,.0f}", hours_remaining)
```

**IPL series:**
```python
# OLD: print(f"  [Series/IPL] No open markets for {IPL_SERIES}")
logger.info("Series/IPL no open markets for %s", IPL_SERIES)

# OLD: print(f"  [Series/IPL] +bovada overlay: ...")
logger.info("Series/IPL +bovada overlay: %d total teams (%d games)",
            len(odds_lookup)//2, len(bovada_games))

# OLD: print(f"  [Series/IPL] {len(markets)} open markets | ...")
logger.info("Series/IPL: %d open markets | %d Odds API teams",
            len(markets), len(odds_lookup)//2)

# OLD: print(f"    [Series/IPL] SKIP {ticker}: unknown abbrev={abbrev!r}")
logger.debug("Series/IPL SKIP %s: unknown abbrev=%r", ticker, abbrev)

# OLD: print(f"    [Series/IPL] SKIP {ticker}: no odds match ...")
logger.debug("Series/IPL SKIP %s: no odds match for canonical=%r", ticker, canonical)

# OLD: print(f"    [Series/IPL] SKIP {ticker}: relative_edge ... exceeds sanity cap")
logger.debug("Series/IPL SKIP %s: relative_edge=%.1f%% exceeds sanity cap",
             ticker, relative_edge * 100)
```

**ETH series:**
```python
# OLD: print(f"  [Series/ETH] Realized 10d vol: {daily_vol:.2%}")
logger.info("Series/ETH realized 10d vol: %.2f%%", daily_vol * 100)

# OLD: print(f"  [Series/ETH] No open markets for {ETH_SERIES}")
logger.info("Series/ETH no open markets for %s", ETH_SERIES)

# OLD: print(f"  [Series/ETH] Could not fetch ETH spot price")
logger.warning("Series/ETH could not fetch ETH spot price")

# OLD: print(f"  [Series/ETH] All today's markets already resolved (past 21:00 UTC)")
logger.info("Series/ETH all today's markets already resolved (past 21:00 UTC)")

# OLD: print(f"  [Series/ETH] {len(markets)} open markets | spot=${spot:,.0f} | ...")
logger.info("Series/ETH: %d open markets | spot=$%s | vol=%.2f%% | hours_remaining=%.1fh",
            len(markets), f"{spot:,.0f}", vol * 100, hours_remaining)
```

**scan_series_markets summary:**
```python
# OLD: print(f"  [Series/{sport.upper()}] Found {len(opps)} opportunities")
logger.info("Series/%s found %d opportunities", sport.upper(), len(opps))

# OLD: print(f"  [Series/{sport.upper()}] Error: {e}")
logger.warning("Series/%s error: %s", sport.upper(), e)

# OLD: print(f"  [Series/BTC] Found {len(btc_opps)} opportunities")
logger.info("Series/BTC found %d opportunities", len(btc_opps))

# OLD: print(f"  [Series/IPL] Found {len(ipl_opps)} opportunities")
logger.info("Series/IPL found %d opportunities", len(ipl_opps))

# OLD: print(f"  [Series/ETH] Found {len(eth_opps)} opportunities")
logger.info("Series/ETH found %d opportunities", len(eth_opps))
```

- [ ] **Step 5: Verify no print() remain**

```bash
grep -n "^\s*print(" hustle-agent/bot/kalshi_series.py
```

Expected: no output

- [ ] **Step 6: Run the test**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_scanners.py::test_kalshi_series_uses_logger_not_print -v
```

Expected: PASS

- [ ] **Step 7: Run full suite**

```bash
cd hustle-agent && python3 -m pytest tests/ --tb=short 2>&1 | tail -8
```

Expected: 0 failures

- [ ] **Step 8: Commit**

```bash
cd hustle-agent && git add bot/kalshi_series.py tests/test_bot_scanners.py
git commit -m "feat: migrate kalshi_series.py print() to nexus.kalshi_series logger"
```

---

## Task 3: odds_scraper.py — logger already exists, just migrate prints

**Files:**
- Modify: `hustle-agent/bot/odds_scraper.py`
- Test: `hustle-agent/tests/test_bot_scanners.py`

**Note:** `odds_scraper.py` already has `logger = logging.getLogger("glint.odds_scraper")` — just replace print() calls, no logger setup needed.

- [ ] **Step 1: Write the failing test**

Add to `hustle-agent/tests/test_bot_scanners.py`:

```python
def test_odds_scraper_uses_logger_not_print(capsys):
    """fetch_consensus_odds with mocked network must not print to stdout."""
    from unittest.mock import patch
    import bot.odds_scraper as os_mod

    # fetch_consensus_odds tries multiple sources; mock the top-level fetchers
    with patch.object(os_mod, "fetch_draftkings_odds", return_value={"games": [], "error": "mocked"}), \
         patch.object(os_mod, "fetch_bovada_odds", return_value={"games": [], "error": "mocked"}), \
         patch.object(os_mod, "fetch_fanduel_odds", return_value={"games": [], "error": "mocked"}), \
         patch.object(os_mod, "fetch_espn_odds", return_value={"games": []}):
        os_mod.fetch_consensus_odds("nba")

    captured = capsys.readouterr()
    assert captured.out == "", f"odds_scraper printed to stdout: {captured.out!r}"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_scanners.py::test_odds_scraper_uses_logger_not_print -v
```

Expected: FAIL — `fetch_consensus_odds` prints fallback chain messages

- [ ] **Step 3: Replace all print() calls in odds_scraper.py**

Conversion table:

| Old | New | Level |
|-----|-----|-------|
| `print("  [DK] 403 Forbidden — Akamai WAF blocking...")` | `logger.warning("DK 403 Forbidden — Akamai WAF blocking this IP")` | warning |
| `print(f"  [DK-DEBUG] {sport.upper()}: {len(events_raw)} raw events")` | `logger.debug("DK %s: %d raw events", sport.upper(), len(events_raw))` | debug |
| `print(f"  [DK-DEBUG] {sport.upper()}: offer data for {len(offers_by_event)} event IDs")` | `logger.debug("DK %s: offer data for %d event IDs", sport.upper(), len(offers_by_event))` | debug |
| `print(f"  [DraftKings] {N} games, {M} with consensus odds")` | `logger.info("DraftKings %s: %d games, %d with consensus odds", ...)` | info |
| `print(f"  [Bovada-DEBUG] {sport.upper()}: {len(raw_events)} events")` | `logger.debug("Bovada %s: %d events", sport.upper(), len(raw_events))` | debug |
| `print(f"  [Bovada] {N} games, {M} with consensus odds")` | `logger.info("Bovada %s: %d games, %d with consensus odds", ...)` | info |
| `print(f"  [FD] {e.code} — FanDuel API blocked...")` | `logger.warning("FD %d — FanDuel API blocked", e.code)` | warning |
| `print(f"  [FD] {N} games, {M} with consensus odds")` | `logger.info("FanDuel %s: %d games, %d with consensus odds", ...)` | info |
| `print(f"  [ESPN-DEBUG] {sport.upper()} scoreboard: {N} events")` | `logger.debug("ESPN %s scoreboard: %d events", sport.upper(), N)` | debug |
| `print(f"  [ESPN-DEBUG] odds[0] keys: ...")` | `logger.debug("ESPN odds[0] keys: %s", sorted(...))` | debug |
| `print(f"  [ESPN-DEBUG] No odds in first game competition")` | `logger.debug("ESPN: no odds in first game competition")` | debug |
| `print(f"  [ESPN-DEBUG] No events in ESPN ... response")` | `logger.debug("ESPN %s: no events in response", sport.upper())` | debug |
| `print(f"  [Rundown] {N} games, {M} with consensus odds")` | `logger.info("Rundown %s: %d games, %d with consensus odds", ...)` | info |
| `print(f"  [DK] DraftKings returned 0 odds ... trying Bovada")` | `logger.info("DK %s returned 0 odds — trying Bovada", sport.upper())` | info |
| `print(f"  [DK] DraftKings failed ... trying Bovada")` | `logger.warning("DK %s failed: %s — trying Bovada", sport.upper(), result.get('error'))` | warning |
| `print(f"  [Bovada] Bovada returned 0 odds ... trying FanDuel")` | `logger.info("Bovada %s returned 0 odds — trying FanDuel", sport.upper())` | info |
| `print(f"  [Bovada] Bovada failed ... trying FanDuel")` | `logger.warning("Bovada %s failed: %s — trying FanDuel", sport.upper(), result.get('error'))` | warning |
| `print(f"  [FD] FanDuel returned 0 odds ... trying ESPN")` | `logger.info("FD %s returned 0 odds — trying ESPN", sport.upper())` | info |
| `print(f"  [FD] FanDuel failed ... trying ESPN")` | `logger.warning("FD %s failed: %s — trying ESPN", sport.upper(), result.get('error'))` | warning |
| `print(f"  [ESPN] {N} games ...")` | `logger.info("ESPN %s: %d games, %d with consensus odds", ...)` | info |
| `print(f"  [ESPN] After pickcenter: {N} games with odds")` | `logger.info("ESPN %s after pickcenter: %d games with odds", sport.upper(), N)` | info |
| `print(f"  [ESPN] Recovered {N} games from snapshot")` | `logger.info("ESPN %s recovered %d games from snapshot", sport.upper(), N)` | info |
| `print(f"  [Rundown] TheRundown returned 0 ... trying Odds API")` | `logger.info("Rundown %s returned 0 odds — trying Odds API", sport.upper())` | info |
| `print(f"  [Rundown] TheRundown failed ... trying Odds API")` | `logger.warning("Rundown %s failed: %s — trying Odds API", sport.upper(), result.get('error'))` | warning |
| `print(f"  [ODDS] All free sources exhausted ... falling back to Odds API")` | `logger.info("%s all free sources exhausted — falling back to Odds API", sport.upper())` | info |
| `print(f"  [ODDS] Odds API also failed ...")` | `logger.warning("%s Odds API also failed: %s", sport.upper(), result.get('error'))` | warning |
| `print(f"  [ODDS] Odds API returned {N} games with odds")` | `logger.info("%s Odds API returned %d games with odds", sport.upper(), N)` | info |

- [ ] **Step 4: Verify no print() remain**

```bash
grep -n "^\s*print(" hustle-agent/bot/odds_scraper.py
```

Expected: no output

- [ ] **Step 5: Run the test**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_scanners.py::test_odds_scraper_uses_logger_not_print -v
```

Expected: PASS

- [ ] **Step 6: Run full suite**

```bash
cd hustle-agent && python3 -m pytest tests/ --tb=short 2>&1 | tail -8
```

Expected: 0 failures

- [ ] **Step 7: Commit**

```bash
cd hustle-agent && git add bot/odds_scraper.py tests/test_bot_scanners.py
git commit -m "feat: migrate odds_scraper.py print() to glint.odds_scraper logger"
```

---

## Task 4: elo.py + injuries.py — add nexus.elo and nexus.injuries loggers

**Files:**
- Modify: `hustle-agent/bot/elo.py`
- Modify: `hustle-agent/bot/injuries.py`
- Test: `hustle-agent/tests/test_bot_improvements.py`

Both files are small (5 print() calls each) and are internal support modules, so they share a task.

- [ ] **Step 1: Write the failing tests**

Add to `hustle-agent/tests/test_bot_improvements.py`:

```python
def test_elo_uses_logger_not_print(capsys):
    """EloRatings._seed_from_espn with mocked network must not print to stdout."""
    from unittest.mock import patch
    import bot.elo as elo_mod

    with patch("bot.elo.requests.get", side_effect=Exception("mocked")):
        tracker = elo_mod.EloRatings()
        tracker._seed_from_espn("nba")

    captured = capsys.readouterr()
    assert captured.out == "", f"elo printed to stdout: {captured.out!r}"


def test_injuries_uses_logger_not_print(capsys):
    """InjuryTracker with mocked network must not print to stdout."""
    from unittest.mock import patch
    import bot.injuries as inj_mod

    with patch("bot.injuries.requests.get", side_effect=Exception("mocked")):
        tracker = inj_mod.InjuryTracker()
        tracker._build_espn_team_map("nba")

    captured = capsys.readouterr()
    assert captured.out == "", f"injuries printed to stdout: {captured.out!r}"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_improvements.py::test_elo_uses_logger_not_print tests/test_bot_improvements.py::test_injuries_uses_logger_not_print -v
```

Expected: both FAIL

- [ ] **Step 3: Add logger to elo.py**

After the existing imports in `hustle-agent/bot/elo.py`, add:

```python
import logging
logger = logging.getLogger("nexus.elo")
```

Replace the 5 print() calls:

```python
# Line 76 — HTTP error
# OLD: print(f"  [Elo] HTTP error ({url[:80]}): {e}")
logger.warning("HTTP error (%s): %s", url[:80], e)

# Line 97 — stale ratings file
# OLD: print(f"  [Elo] Ratings file is {age_days:.0f} days old — forcing re-seed from ESPN")
logger.info("Ratings file is %.0f days old — forcing re-seed from ESPN", age_days)

# Line 192 — successful seed
# OLD: print(f"  [Elo] Seeded {len(ratings)} {sport.upper()} teams from ESPN standings")
logger.info("Seeded %d %s teams from ESPN standings", len(ratings), sport.upper())

# Line 195 — failed seed
# OLD: print(f"  [Elo] WARNING: could not seed {sport.upper()} Elo from ESPN ({url})")
logger.warning("Could not seed %s Elo from ESPN (%s)", sport.upper(), url)

# Line 278 — rating update
# OLD: print(f"  [Elo] Updated: {winner} {ratings[winner]:.0f} / {loser} {ratings[loser]:.0f}")
logger.info("Updated: %s %.0f / %s %.0f", winner, ratings[winner], loser, ratings[loser])
```

- [ ] **Step 4: Add logger to injuries.py**

After the existing imports in `hustle-agent/bot/injuries.py`, add:

```python
import logging
logger = logging.getLogger("nexus.injuries")
```

Replace the 5 print() calls:

```python
# Line 84 — HTTP error
# OLD: print(f"  [Injuries] HTTP error ({url[:80]}): {e}")
logger.warning("HTTP error (%s): %s", url[:80], e)

# Line 126 — successful team map load
# OLD: print(f"  [Injuries] Loaded {count} {sport.upper()} teams from ESPN")
logger.info("Loaded %d %s teams from ESPN", count, sport.upper())

# Line 128 — empty team map
# OLD: print(f"  [Injuries] WARNING: ESPN team map empty for {sport.upper()} ({url})")
logger.warning("ESPN team map empty for %s (%s)", sport.upper(), url)

# Line 257 — missing ESPN ID
# OLD: print(f"  [Injuries] No ESPN ID for {canonical_team!r} in {sport.upper()} team map")
logger.warning("No ESPN ID for %r in %s team map", canonical_team, sport.upper())

# Line 308 — check error
# OLD: print(f"  [Injuries] Error checking {canonical_team!r}: {e}")
logger.warning("Error checking %r: %s", canonical_team, e)
```

- [ ] **Step 5: Verify no print() remain**

```bash
grep -n "^\s*print(" hustle-agent/bot/elo.py hustle-agent/bot/injuries.py
```

Expected: no output

- [ ] **Step 6: Run the tests**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_improvements.py::test_elo_uses_logger_not_print tests/test_bot_improvements.py::test_injuries_uses_logger_not_print -v
```

Expected: both PASS

- [ ] **Step 7: Run full suite**

```bash
cd hustle-agent && python3 -m pytest tests/ --tb=short 2>&1 | tail -8
```

Expected: 0 failures

- [ ] **Step 8: Commit**

```bash
cd hustle-agent && git add bot/elo.py bot/injuries.py tests/test_bot_improvements.py
git commit -m "feat: migrate elo.py and injuries.py print() to named loggers"
```

---

## Task 5: econ_scanner.py — migrate to glint.econ_scanner

**Files:**
- Modify: `hustle-agent/bot/econ_scanner.py`
- Test: `hustle-agent/tests/test_bot_improvements.py`

- [ ] **Step 1: Write the failing test**

Add to `hustle-agent/tests/test_bot_improvements.py`:

```python
def test_econ_scanner_uses_logger_not_print(capsys):
    """scan_econ_markets with mocked network must not print to stdout."""
    from unittest.mock import patch
    import bot.econ_scanner as econ_mod

    with patch.object(econ_mod, "get_markets", return_value={"markets": []}), \
         patch("bot.econ_scanner.requests.get", side_effect=Exception("mocked")):
        econ_mod.scan_econ_markets()

    captured = capsys.readouterr()
    assert captured.out == "", f"econ_scanner printed to stdout: {captured.out!r}"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_improvements.py::test_econ_scanner_uses_logger_not_print -v
```

Expected: FAIL

- [ ] **Step 3: Add logger to econ_scanner.py**

After the existing imports in `hustle-agent/bot/econ_scanner.py`, add:

```python
import logging
logger = logging.getLogger("glint.econ_scanner")
```

- [ ] **Step 4: Replace all print() calls**

```python
# Line 76 — HTTP error
# OLD: print(f"  [EconHTTP] Error fetching {url[:80]}: {e}")
logger.warning("HTTP error fetching %s: %s", url[:80], e)

# Line 95 — no FRED API key (these two lines together are a single user message)
# OLD: print("  [Econ] No FRED API key — register free at fred.stlouisfed.org/docs/api/api_key.html")
# OLD: print("  [Econ] Save key to config/fred.json: {\"api_key\": \"YOUR_KEY\"}")
logger.warning(
    "No FRED API key — register free at fred.stlouisfed.org/docs/api/api_key.html\n"
    "  Save key to config/fred.json: {\"api_key\": \"YOUR_KEY\"}"
)

# Line 108 — unexpected FRED response
# OLD: print(f"  [Econ] FRED API returned unexpected response — check api_key in config/fred.json")
logger.warning("FRED API returned unexpected response — check api_key in config/fred.json")

# Line 122 — no valid observations
# OLD: print(f"  [Econ] FRED returned no valid observations")
logger.warning("FRED returned no valid observations")

# Line 131 — missing year-ago data
# OLD: print(f"  [Econ] FRED missing year-ago data ({year_ago_month}) — data not ready")
logger.info("FRED missing year-ago data (%s) — data not ready", year_ago_month)

# Line 138 — CPI data summary (check exact variable names in file before applying)
# OLD: print(f"  [Econ] FRED CPI: ...")
logger.info("FRED CPI data loaded")

# Line 175 — no open markets
# OLD: print("  [Econ] No open economic markets found")
logger.info("No open economic markets found")

# Line 180 — FRED unavailable
# OLD: print("  [Econ] FRED CPI data unavailable — skipping")
logger.warning("FRED CPI data unavailable — skipping")

# Line 183 — scan summary
# OLD: print(f"  [Econ] {len(markets)} markets | Cleveland Fed CPI nowcast={nowcast:.2f}%")
logger.info("%d markets | Cleveland Fed CPI nowcast=%.2f%%", len(markets), nowcast)
```

**Note for line 138:** Before applying the replacement, read the actual code at that line — the exact variable names in the f-string need to match. The replacement above is a safe stub; adjust the format string to match the actual variables used.

- [ ] **Step 5: Verify no print() remain**

```bash
grep -n "^\s*print(" hustle-agent/bot/econ_scanner.py
```

Expected: no output

- [ ] **Step 6: Run the test**

```bash
cd hustle-agent && python3 -m pytest tests/test_bot_improvements.py::test_econ_scanner_uses_logger_not_print -v
```

Expected: PASS

- [ ] **Step 7: Run full suite**

```bash
cd hustle-agent && python3 -m pytest tests/ --tb=short 2>&1 | tail -8
```

Expected: 0 failures

- [ ] **Step 8: Commit**

```bash
cd hustle-agent && git add bot/econ_scanner.py tests/test_bot_improvements.py
git commit -m "feat: migrate econ_scanner.py print() to glint.econ_scanner logger"
```

---

## Task 6: Rename print_calibration_summary → log_calibration_summary

**Files:**
- Modify: `hustle-agent/bot/outcome_tracker.py` (rename method)
- Modify: `hustle-agent/bot/main.py` (update the call site)
- Test: `hustle-agent/tests/test_bot_tracker.py` (update any reference)

**Why:** The method no longer prints — it was migrated to `logger.info()` in Session 8. Keeping the old name is misleading and will confuse future readers.

- [ ] **Step 1: Check for all call sites**

```bash
grep -rn "print_calibration_summary" hustle-agent/
```

Expected output: two lines — one in outcome_tracker.py (definition) and one in main.py (call site). Note the exact line numbers.

- [ ] **Step 2: Rename in outcome_tracker.py**

In `hustle-agent/bot/outcome_tracker.py`, change the method definition:

```python
# OLD:
def print_calibration_summary(self):

# NEW:
def log_calibration_summary(self):
```

- [ ] **Step 3: Update call site in main.py**

In `hustle-agent/bot/main.py`, find the call (currently around line 820):

```python
# OLD:
_outcome_tracker.print_calibration_summary()

# NEW:
_outcome_tracker.log_calibration_summary()
```

- [ ] **Step 4: Update any test references**

```bash
grep -rn "print_calibration_summary" hustle-agent/tests/
```

If any test files reference the old name, rename them to `log_calibration_summary` in the same edit.

- [ ] **Step 5: Run full suite**

```bash
cd hustle-agent && python3 -m pytest tests/ --tb=short 2>&1 | tail -8
```

Expected: 0 failures — the rename should be transparent since no test was calling the old name directly (it was only called from main.py)

- [ ] **Step 6: Commit**

```bash
cd hustle-agent && git add bot/outcome_tracker.py bot/main.py
git commit -m "refactor: rename print_calibration_summary → log_calibration_summary"
```

---

## Verification

After all tasks complete:

```bash
cd hustle-agent

# 1. No bare print() in any bot production module
grep -rn "^\s*print(" bot/ --include="*.py" | grep -v "__pycache__"

# 2. All tests pass
python3 -m pytest tests/ -v 2>&1 | tail -15
```

Expected:
- `grep` returns nothing
- All tests pass, 0 failures
