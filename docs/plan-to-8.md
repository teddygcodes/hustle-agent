# Bot Quality Plan: 5 → 8

**Goal:** Add calibration, price movement detection, home/away context, dynamic weather uncertainty, and correlated position caps — turning the bot from a gap-detector into a self-improving prediction system.

**Working directory:** `hustle-agent/hustle-agent/`
**Python:** 3.9 | stdlib only (no new deps) | SQLite for persistence

---

## File Map

| File | Change |
|------|--------|
| `bot/outcome_tracker.py` | **Create** — SQLite logger + resolution checker + calibration reporter |
| `bot/price_monitor.py` | **Create** — Kalshi price cache + movement detection |
| `bot/main.py` | Modify — wire tracker + price monitor into alert/scan flow |
| `bot/math_engine.py` | Modify — dynamic σ for weather based on days_ahead |
| `bot/scanner.py` | Modify — correlated position cap for vig stack; call price monitor |
| `bot/kalshi_series.py` | Modify — home/away flag + confidence modifier |
| `tests/test_to_8.py` | **Create** — all new tests |

---

## Task 1: Outcome Tracking + Calibration

**Files:**
- Create: `bot/outcome_tracker.py`
- Modify: `bot/main.py` (~line where paper alerts fire)
- Test: `tests/test_to_8.py`

**What it does:**
Every paper alert gets persisted to SQLite. Each scan cycle, any alert whose `close_time` has passed gets checked against Kalshi for resolution. Win/loss is recorded. After 50+ samples per strategy, calibration stats print automatically.

### Step 1: Write failing tests

```python
# tests/test_to_8.py
"""Tests for bot quality improvements."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Task 1: Outcome Tracker ──────────────────────────────────────────────────

def test_alert_stored_returns_id():
    """store_alert() must return a positive integer ID."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXNBAGAME-26APR05LACSAC-SAC",
            "type": "series_game_edge",
            "edge": 0.23,
            "relative_edge": 0.23,
            "confidence": 0.70,
            "recommended_side": "yes",
            "kalshi_price": 0.14,
            "market": {"close_time": "2026-04-13T00:00:00Z"},
        })
        assert isinstance(alert_id, int) and alert_id > 0
    finally:
        os.unlink(db_path)


def test_resolution_records_win():
    """record_resolution() with YES result on BUY YES alert marks won=True."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXTEST-YES",
            "type": "weather",
            "edge": 0.10,
            "relative_edge": 0.30,
            "confidence": 0.75,
            "recommended_side": "yes",
            "kalshi_price": 0.10,
            "market": {"close_time": "2026-04-05T05:00:00Z"},
        })
        tracker.record_resolution(alert_id, result="yes")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 1
        assert stats["total"] == 1
        assert stats["win_rate"] == 1.0
    finally:
        os.unlink(db_path)


def test_resolution_records_loss():
    """record_resolution() with NO result on BUY YES alert marks won=False."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXTEST-LOSS",
            "type": "weather",
            "edge": 0.10,
            "relative_edge": 0.30,
            "confidence": 0.75,
            "recommended_side": "yes",
            "kalshi_price": 0.10,
            "market": {"close_time": "2026-04-05T05:00:00Z"},
        })
        tracker.record_resolution(alert_id, result="no")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 0
        assert stats["win_rate"] == 0.0
    finally:
        os.unlink(db_path)


def test_calibration_flags_underperforming_strategy():
    """get_calibration_report() flags strategies with win_rate < expected after 50+ samples."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        # 50 alerts at 25% edge (implies ~62% expected win rate for YES) but only 30% wins
        for i in range(50):
            aid = tracker.store_alert({
                "ticker": f"KXTEST-{i}",
                "type": "series_game_edge",
                "edge": 0.25,
                "relative_edge": 0.25,
                "confidence": 0.70,
                "recommended_side": "yes",
                "kalshi_price": 0.40,
                "market": {"close_time": "2026-04-05T00:00:00Z"},
            })
            tracker.record_resolution(aid, result="yes" if i < 15 else "no")
        report = tracker.get_calibration_report()
        assert "series_game_edge" in report
        assert report["series_game_edge"]["flagged"] is True
    finally:
        os.unlink(db_path)
```

### Step 2: Run tests — verify FAIL

```bash
python3 -m pytest tests/test_to_8.py::test_alert_stored_returns_id \
    tests/test_to_8.py::test_resolution_records_win \
    tests/test_to_8.py::test_resolution_records_loss \
    tests/test_to_8.py::test_calibration_flags_underperforming_strategy -v
```

Expected: ImportError (OutcomeTracker doesn't exist yet).

### Step 3: Create `bot/outcome_tracker.py`

```python
"""
Outcome Tracker — paper alert persistence and calibration.

Stores every paper alert to SQLite. Each scan cycle, checks resolved markets
and records wins/losses. After 50+ samples per strategy, flags underperformers.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "state", "outcomes.db")

# Expected win rate given edge: kalshi_price + edge = fair_value
# For BUY YES: expected win rate ≈ fair_value = kalshi_price + edge
# Flag if actual win rate < expected * 0.75 (25% below expected)
_CALIBRATION_SHORTFALL_THRESHOLD = 0.75
_MIN_SAMPLES_FOR_CALIBRATION = 50


class OutcomeTracker:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_alerts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker      TEXT NOT NULL,
                    strategy    TEXT NOT NULL,
                    edge        REAL,
                    relative_edge REAL,
                    confidence  REAL,
                    side        TEXT,
                    kalshi_price REAL,
                    close_time  TEXT,
                    stored_at   TEXT NOT NULL,
                    resolved    INTEGER DEFAULT 0,
                    result      TEXT,
                    won         INTEGER
                )
            """)
            conn.commit()

    def store_alert(self, opp: dict) -> int:
        """Persist a paper alert. Returns the row ID."""
        market = opp.get("market", {})
        close_time = market.get("close_time") or market.get("expiration_time", "")
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO paper_alerts
                   (ticker, strategy, edge, relative_edge, confidence, side,
                    kalshi_price, close_time, stored_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    opp.get("ticker", ""),
                    opp.get("type", "unknown"),
                    opp.get("edge", 0),
                    opp.get("relative_edge", 0),
                    opp.get("confidence", 0),
                    opp.get("recommended_side", "yes"),
                    opp.get("kalshi_price", 0),
                    close_time,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def record_resolution(self, alert_id: int, result: str):
        """Record market resolution. result='yes'|'no'."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT side FROM paper_alerts WHERE id=?", (alert_id,)
            ).fetchone()
            if not row:
                return
            side = row[0]
            won = 1 if (side == "yes" and result == "yes") or (side == "no" and result == "no") else 0
            conn.execute(
                """UPDATE paper_alerts
                   SET resolved=1, result=?, won=?, close_time=close_time
                   WHERE id=?""",
                (result, won, alert_id),
            )
            conn.commit()

    def get_stats(self, strategy: str) -> dict:
        """Return win/loss stats for a strategy."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*), SUM(won) FROM paper_alerts
                   WHERE strategy=? AND resolved=1""",
                (strategy,),
            ).fetchone()
        total = row[0] or 0
        wins = int(row[1] or 0)
        return {
            "strategy": strategy,
            "total": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": wins / total if total > 0 else None,
        }

    def get_pending_resolution(self) -> list[dict]:
        """Return alerts whose close_time has passed but aren't resolved yet."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, ticker, close_time FROM paper_alerts
                   WHERE resolved=0 AND close_time != '' AND close_time < ?""",
                (now,),
            ).fetchall()
        return [{"id": r[0], "ticker": r[1], "close_time": r[2]} for r in rows]

    def get_calibration_report(self) -> dict[str, dict]:
        """
        Return calibration stats per strategy.
        Flags strategies where actual win rate < 75% of expected win rate
        after 50+ resolved samples.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT strategy, COUNT(*), SUM(won), AVG(kalshi_price), AVG(edge)
                   FROM paper_alerts WHERE resolved=1
                   GROUP BY strategy"""
            ).fetchall()

        report = {}
        for strategy, total, wins, avg_price, avg_edge in rows:
            wins = int(wins or 0)
            win_rate = wins / total if total > 0 else None
            expected_win_rate = (avg_price or 0) + (avg_edge or 0)  # fair_value ≈ price + edge
            flagged = (
                total >= _MIN_SAMPLES_FOR_CALIBRATION
                and win_rate is not None
                and win_rate < expected_win_rate * _CALIBRATION_SHORTFALL_THRESHOLD
            )
            report[strategy] = {
                "total": total,
                "wins": wins,
                "win_rate": round(win_rate, 3) if win_rate is not None else None,
                "expected_win_rate": round(expected_win_rate, 3),
                "flagged": flagged,
            }
        return report

    def check_and_resolve(self) -> int:
        """
        Fetch resolution for all pending alerts from Kalshi. Returns count resolved.
        Fails open — if Kalshi is down, alerts stay unresolved.
        """
        from agent.kalshi_client import _kalshi_get
        pending = self.get_pending_resolution()
        resolved_count = 0
        for alert in pending:
            try:
                data = _kalshi_get(f"/markets/{alert['ticker']}")
                market = data.get("market", data)
                result = market.get("result")
                if result in ("yes", "no"):
                    self.record_resolution(alert["id"], result)
                    resolved_count += 1
            except Exception:
                pass
        return resolved_count

    def print_calibration_summary(self):
        """Print calibration report to stdout. Called each scan cycle."""
        report = self.get_calibration_report()
        if not report:
            return
        print("\n  [CALIBRATION] Strategy performance:")
        for strategy, stats in sorted(report.items()):
            flag = " ⚠️ UNDERPERFORMING" if stats["flagged"] else ""
            if stats["total"] < 10:
                continue  # not enough data to print
            wr = f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "n/a"
            exp = f"{stats['expected_win_rate']:.0%}"
            print(
                f"    {strategy}: {stats['wins']}/{stats['total']} "
                f"({wr} actual vs {exp} expected){flag}"
            )
```

### Step 4: Wire into `bot/main.py`

Find where paper alerts fire (look for `[PAPER]` or `send_alert`). Add tracking around it:

```python
# Near top of main.py, after imports:
from bot.outcome_tracker import OutcomeTracker
_outcome_tracker = OutcomeTracker()

# In the alert-sending section, after each paper alert fires:
_outcome_tracker.store_alert(opp)

# In scan_cycle() or main loop, once per cycle:
resolved = _outcome_tracker.check_and_resolve()
if resolved:
    print(f"  [TRACKER] Resolved {resolved} market(s)")
_outcome_tracker.print_calibration_summary()
```

### Step 5: Run tests — verify PASS

```bash
python3 -m pytest tests/test_to_8.py -k "test_alert_stored or test_resolution or test_calibration" -v
```

### Step 6: Commit

```bash
git add bot/outcome_tracker.py bot/main.py tests/test_to_8.py
git commit -m "feat: outcome tracking and calibration system"
```

---

## Task 2: Kalshi Price Movement Detection

**Files:**
- Create: `bot/price_monitor.py`
- Modify: `bot/scanner.py` — call monitor after building opportunities
- Test: `tests/test_to_8.py`

**What it does:**
Caches Kalshi prices between scans (`bot/state/price_cache.json`). If a price
moves >3¢ toward our position since the last scan, adds a warning. If >5¢
against us, reduces confidence by 0.10.

**Direction logic:**
- BUY YES position: price rising = market correcting toward us (bad sign)
- BUY YES position: price falling = market moving away (good — mispricing deepening)
- BUY NO position: price falling = bad (market correcting YES upward = NO gets cheaper = gap closing)

### Step 1: Write failing tests

```python
def test_price_cache_stores_and_retrieves():
    """PriceMonitor stores a price and retrieves it on next call."""
    import tempfile, json
    from bot.price_monitor import PriceMonitor
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({}, f)
        cache_path = f.name
    try:
        monitor = PriceMonitor(cache_path=cache_path)
        monitor.update("KXTEST-T67", yes_ask=27)
        cached = monitor.get_cached("KXTEST-T67")
        assert cached is not None
        assert cached["yes_ask"] == 27
    finally:
        os.unlink(cache_path)


def test_price_moving_against_yes_adds_warning():
    """BUY YES: price rising >5¢ since last scan adds warning and reduces confidence."""
    import tempfile, json
    from bot.price_monitor import PriceMonitor
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({}, f)
        cache_path = f.name
    try:
        monitor = PriceMonitor(cache_path=cache_path)
        # Previously priced at 14¢, now at 20¢ — market correcting against BUY YES
        monitor.update("KXTEST-SAC", yes_ask=14)
        opp = {
            "ticker": "KXTEST-SAC",
            "recommended_side": "yes",
            "confidence": 0.70,
            "warnings": [],
        }
        result = monitor.annotate(opp, current_yes_ask=20)
        assert any("moving against" in w.lower() for w in result["warnings"])
        assert result["confidence"] < 0.70
    finally:
        os.unlink(cache_path)


def test_price_stable_no_annotation():
    """Price stable within 2¢ — no warning added."""
    import tempfile, json
    from bot.price_monitor import PriceMonitor
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({}, f)
        cache_path = f.name
    try:
        monitor = PriceMonitor(cache_path=cache_path)
        monitor.update("KXTEST-STABLE", yes_ask=27)
        opp = {
            "ticker": "KXTEST-STABLE",
            "recommended_side": "yes",
            "confidence": 0.70,
            "warnings": [],
        }
        result = monitor.annotate(opp, current_yes_ask=28)
        assert result["warnings"] == []
        assert result["confidence"] == 0.70
    finally:
        os.unlink(cache_path)
```

### Step 2: Run tests — verify FAIL

```bash
python3 -m pytest tests/test_to_8.py -k "price" -v
```

### Step 3: Create `bot/price_monitor.py`

```python
"""
Price Monitor — detects Kalshi price movement between scans.

Caches YES ask prices per ticker in bot/state/price_cache.json.
On each scan, annotates opportunities with movement direction and
adjusts confidence when the market is correcting against our position.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

DEFAULT_CACHE_PATH = os.path.join(os.path.dirname(__file__), "state", "price_cache.json")

_WARN_THRESHOLD_CENTS = 3    # Add warning if price moved this much against us
_PENALIZE_THRESHOLD_CENTS = 5  # Reduce confidence if price moved this much against us
_CONFIDENCE_PENALTY = 0.10


class PriceMonitor:
    def __init__(self, cache_path: str = DEFAULT_CACHE_PATH):
        self.cache_path = cache_path
        self._cache: dict = self._load()

    def _load(self) -> dict:
        try:
            with open(self.cache_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w") as f:
            json.dump(self._cache, f)

    def update(self, ticker: str, yes_ask: int):
        """Store current YES ask price for ticker."""
        self._cache[ticker] = {
            "yes_ask": yes_ask,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_cached(self, ticker: str) -> Optional[dict]:
        return self._cache.get(ticker)

    def annotate(self, opp: dict, current_yes_ask: int) -> dict:
        """
        Compare current price to cached price. If moving against our position,
        add warning and optionally reduce confidence. Returns modified opp copy.
        """
        ticker = opp.get("ticker", "")
        cached = self.get_cached(ticker)
        if not cached:
            return opp

        prev_ask = cached["yes_ask"]
        side = opp.get("recommended_side", "yes")

        # For BUY YES: rising price is bad (market correcting upward)
        # For BUY NO: falling price is bad (YES getting cheaper = NO gets pricier)
        if side == "yes":
            delta = current_yes_ask - prev_ask  # positive = price rising = bad
        else:
            delta = prev_ask - current_yes_ask  # positive = price falling = bad for NO

        if delta <= 0:
            return opp  # price moving in our favor or stable — no annotation

        opp = opp.copy()
        opp["warnings"] = list(opp.get("warnings", []))
        opp["price_delta_cents"] = delta

        if delta >= _WARN_THRESHOLD_CENTS:
            direction = "rising" if side == "yes" else "falling"
            opp["warnings"].append(
                f"price moving against: YES ask {direction} {delta:+d}¢ since last scan"
            )

        if delta >= _PENALIZE_THRESHOLD_CENTS:
            opp["confidence"] = max(0.0, opp.get("confidence", 0.5) - _CONFIDENCE_PENALTY)
            opp["warnings"][-1] += f" (confidence -{_CONFIDENCE_PENALTY:.0%})"

        return opp

    def annotate_all(self, opportunities: list[dict]) -> list[dict]:
        """Annotate a list of opportunities with price movement data."""
        result = []
        for opp in opportunities:
            market = opp.get("market", {})
            yes_ask = market.get("yes_ask")
            ticker = opp.get("ticker", "")
            if yes_ask and ticker:
                opp = self.annotate(opp, current_yes_ask=yes_ask)
                self.update(ticker, yes_ask=yes_ask)
            result.append(opp)
        return result
```

### Step 4: Wire into `bot/scanner.py` — call at end of `scan_cycle()`

```python
# Near top of scanner.py, after imports:
from bot.price_monitor import PriceMonitor
_price_monitor = PriceMonitor()

# At end of scan_cycle(), before returning, after EV sort:
all_opportunities = _price_monitor.annotate_all(all_opportunities)
```

### Step 5: Run tests — verify PASS

```bash
python3 -m pytest tests/test_to_8.py -k "price" -v
```

### Step 6: Full suite

```bash
python3 -m pytest --tb=no -q
```

### Step 7: Commit

```bash
git add bot/price_monitor.py bot/scanner.py tests/test_to_8.py
git commit -m "feat: Kalshi price movement detection and confidence penalty"
```

---

## Task 3: Home/Away + Rest Differential in Sports Confidence

**Files:**
- Modify: `bot/kalshi_series.py` — read home_away from game map, apply modifier
- Test: `tests/test_to_8.py`

**What it does:**
The Odds API game map already stores `home_away` per team. Home teams win ~55–57%
of NBA games, ~54% of MLB. Being away on B2B compounds the disadvantage. Apply:
- BUY YES on home team: confidence +0.03
- BUY YES on away team: confidence -0.03
- BUY YES on away team + B2B: additional -0.05 (stacks with existing B2B penalty)

### Step 1: Write failing tests

```python
def test_home_team_gets_confidence_boost():
    """Home team flag adds +0.03 to base confidence."""
    from bot.scanner import _apply_home_away_modifier
    opp = {
        "type": "series_game_edge",
        "confidence": 0.70,
        "home_away": "home",
        "b2b": False,
    }
    result = _apply_home_away_modifier(opp)
    assert result["confidence"] == 0.73


def test_away_b2b_stacks_penalties():
    """Away + B2B: -0.03 (away) - 0.05 (away B2B stack) applied."""
    from bot.scanner import _apply_home_away_modifier
    opp = {
        "type": "series_game_edge",
        "confidence": 0.70,
        "home_away": "away",
        "b2b": True,
    }
    result = _apply_home_away_modifier(opp)
    assert result["confidence"] == pytest.approx(0.62, abs=0.001)


def test_no_home_away_flag_noop():
    """If home_away not present, confidence unchanged."""
    from bot.scanner import _apply_home_away_modifier
    opp = {"type": "series_game_edge", "confidence": 0.70}
    result = _apply_home_away_modifier(opp)
    assert result["confidence"] == 0.70
```

Add `import pytest` at top of test file.

### Step 2: Run tests — verify FAIL

```bash
python3 -m pytest tests/test_to_8.py -k "home_away" -v
```

### Step 3: Add `_apply_home_away_modifier()` to `bot/scanner.py`

Add near `_sort_by_ev` and `_apply_b2b_penalty`:

```python
def _apply_home_away_modifier(opp: dict) -> dict:
    """Adjust confidence based on home/away status.

    Home team: +0.03 (home-court/field advantage is real and consistent)
    Away team: -0.03
    Away + B2B: additional -0.05 (away B2B is significantly harder)
    """
    home_away = opp.get("home_away")
    if not home_away or opp.get("type") not in ("series_game_edge", "ipl_game_edge"):
        return opp
    opp = opp.copy()
    if home_away == "home":
        opp["confidence"] = min(0.95, opp.get("confidence", 0.5) + 0.03)
    elif home_away == "away":
        opp["confidence"] = max(0.0, opp.get("confidence", 0.5) - 0.03)
        if opp.get("b2b"):
            opp["confidence"] = max(0.0, opp["confidence"] - 0.05)
    return opp
```

### Step 4: Populate `home_away` field in `bot/kalshi_series.py`

In `scan_sports_series()`, after resolving `canonical` and finding `prob`, look up home_away from the game map. The `_ODDS_API_GAME_MAP` already stores this.

Find the section where the opportunity dict is built (around the `opportunities.append({` block). Before it:

```python
# Look up home/away from game map
home_away = None
try:
    from bot.kalshi_series import _ODDS_API_GAME_MAP
    game_map = _ODDS_API_GAME_MAP.get(sport, {})
    entry = game_map.get(canonical.lower())
    if entry:
        home_away = entry.get("home_away")
except Exception:
    pass
```

Add `"home_away": home_away` to the opportunity dict.

### Step 5: Wire `_apply_home_away_modifier()` into `scan_cycle()` in `bot/scanner.py`

In the `scan_cycle()` function, in the block where series opps are assembled:

```python
all_opportunities = [_apply_home_away_modifier(o) for o in all_opportunities]
```

Apply after `_apply_b2b_penalty`.

### Step 6: Run tests — verify PASS

```bash
python3 -m pytest tests/test_to_8.py -k "home_away" -v
```

### Step 7: Full suite

```bash
python3 -m pytest --tb=no -q
```

### Step 8: Commit

```bash
git add bot/scanner.py bot/kalshi_series.py tests/test_to_8.py
git commit -m "feat: home/away and rest differential confidence modifier"
```

---

## Task 4: Dynamic Weather σ (Time-Scaled Forecast Uncertainty)

**Files:**
- Modify: `bot/math_engine.py` — `calculate_weather_edge()` signature + σ scaling
- Modify: `bot/scanner.py` — pass `days_ahead` to weather edge calculator
- Test: `tests/test_to_8.py`

**What it does:**
NWS 1-day forecasts have ~2°F RMSE. 2-day forecasts have ~3.5°F RMSE. Using a
fixed σ overstates confidence on far-out forecasts. Scale: `σ = 2.0 + 0.75 * (days_ahead - 1)`.

### Step 1: Write failing tests

```python
def test_weather_sigma_1_day_is_tighter_than_2_day():
    """1-day σ must be smaller than 2-day σ for same forecast."""
    from bot.math_engine import calculate_weather_edge
    base_args = dict(
        city="NYC",
        forecast_temp=65.0,
        threshold=67.0,
        direction="above",
        kalshi_price_cents=27,
    )
    edge_1day = calculate_weather_edge(**base_args, days_ahead=1)
    edge_2day = calculate_weather_edge(**base_args, days_ahead=2)
    # 2-day has wider σ → fair value closer to 0.5 → smaller absolute edge
    assert abs(edge_1day["edge"]) >= abs(edge_2day["edge"]), (
        f"1-day edge {edge_1day['edge']:.4f} should be >= 2-day edge {edge_2day['edge']:.4f}"
    )


def test_weather_sigma_default_is_1_day():
    """Default days_ahead=1 produces same result as explicit days_ahead=1."""
    from bot.math_engine import calculate_weather_edge
    args = dict(city="NYC", forecast_temp=65.0, threshold=67.0,
                direction="above", kalshi_price_cents=27)
    default = calculate_weather_edge(**args)
    explicit = calculate_weather_edge(**args, days_ahead=1)
    assert default["edge"] == explicit["edge"]
```

### Step 2: Run tests — verify FAIL

```bash
python3 -m pytest tests/test_to_8.py -k "weather_sigma" -v
```

### Step 3: Modify `bot/math_engine.py`

Find `calculate_weather_edge()`. It currently uses a fixed σ constant (look for `sigma` or `std` or the normal distribution calls). Add `days_ahead: int = 1` parameter and scale σ:

```python
def calculate_weather_edge(
    city: str,
    forecast_temp: float,
    threshold: float,
    direction: str,
    kalshi_price_cents: int,
    threshold_high: float | None = None,
    days_ahead: int = 1,          # <-- add this parameter
) -> dict:
    # Replace the fixed sigma constant with:
    sigma = 2.0 + 0.75 * (days_ahead - 1)  # 2.0 at 1-day, 2.75 at 2-day, 3.5 at 3-day
    # rest of function unchanged
```

### Step 4: Pass `days_ahead` from `bot/scanner.py`

In `scan_weather_markets()`, when calling `calculate_weather_edge()`, the scanner already parses `target_date` from the ticker. Add:

```python
days_ahead = max(1, (target_date - datetime.now(timezone.utc).date()).days)
```

Pass it to `calculate_weather_edge(..., days_ahead=days_ahead)`.

### Step 5: Run tests — verify PASS

```bash
python3 -m pytest tests/test_to_8.py -k "weather_sigma" -v
```

### Step 6: Full suite

```bash
python3 -m pytest --tb=no -q
```

### Step 7: Commit

```bash
git add bot/math_engine.py bot/scanner.py tests/test_to_8.py
git commit -m "feat: time-scaled weather forecast uncertainty (dynamic sigma)"
```

---

## Task 5: Correlated Position Cap for Vig Stack

**Files:**
- Modify: `bot/scanner.py` — group vig stack opps by series, cap combined sizing
- Test: `tests/test_to_8.py`

**What it does:**
Multiple vig stack signals from the same weather series (e.g., 4 KXHIGHNY contracts)
are correlated — if the temperature misses one bucket, it likely misses them all.
Cap: if >1 signal fires in the same series, total recommended contracts across that
series equals what a single signal would get. Divide evenly.

### Step 1: Write failing tests

```python
def test_correlated_vig_signals_capped():
    """3 vig stack signals in same series: total contracts = single-signal max."""
    from bot.scanner import _cap_correlated_vig_stack
    # Each recommends 10 contracts on its own
    opps = [
        {"type": "vig_stack_series", "series_ticker": "KXHIGHNY",
         "ticker": f"KXHIGHNY-T{i}", "recommended_contracts": 10, "edge": 0.10,
         "confidence": 0.90}
        for i in range(3)
    ]
    result = _cap_correlated_vig_stack(opps)
    total_contracts = sum(o["recommended_contracts"] for o in result
                          if o["series_ticker"] == "KXHIGHNY")
    # Should equal ~10 total (single-signal equivalent), not 30
    assert total_contracts <= 12  # allow small rounding


def test_uncorrelated_vig_signals_untouched():
    """Signals from different series are independent — no cap applied."""
    from bot.scanner import _cap_correlated_vig_stack
    opps = [
        {"type": "vig_stack_series", "series_ticker": "KXHIGHNY",
         "ticker": "KXHIGHNY-T67", "recommended_contracts": 10, "edge": 0.10,
         "confidence": 0.90},
        {"type": "vig_stack_series", "series_ticker": "KXHIGHMIA",
         "ticker": "KXHIGHMIA-T80", "recommended_contracts": 10, "edge": 0.10,
         "confidence": 0.90},
    ]
    result = _cap_correlated_vig_stack(opps)
    total = sum(o["recommended_contracts"] for o in result)
    assert total == 20  # both untouched


def test_non_vig_opps_pass_through():
    """Non-vig_stack_series opps are returned unchanged."""
    from bot.scanner import _cap_correlated_vig_stack
    opps = [
        {"type": "weather", "ticker": "KXHIGHNY-T67", "recommended_contracts": 5},
        {"type": "series_game_edge", "ticker": "KXNBAGAME-SAC", "recommended_contracts": 7},
    ]
    result = _cap_correlated_vig_stack(opps)
    assert result == opps
```

### Step 2: Run tests — verify FAIL

```bash
python3 -m pytest tests/test_to_8.py -k "correlated_vig" -v
```

### Step 3: Add `_cap_correlated_vig_stack()` to `bot/scanner.py`

```python
def _cap_correlated_vig_stack(opportunities: list[dict]) -> list[dict]:
    """
    Cap total contract sizing across correlated vig stack signals.

    Multiple signals from the same weather series are correlated — temperature
    either hits or misses the entire ladder. Cap total contracts in a series
    to the equivalent of a single signal (the highest-edge one's sizing).
    """
    from collections import defaultdict

    # Separate vig stack from everything else
    vig = [o for o in opportunities if o.get("type") == "vig_stack_series"]
    other = [o for o in opportunities if o.get("type") != "vig_stack_series"]

    if not vig:
        return opportunities

    # Group by series_ticker
    by_series: dict[str, list[dict]] = defaultdict(list)
    for o in vig:
        by_series[o.get("series_ticker", o["ticker"])].append(o)

    result_vig = []
    for series, signals in by_series.items():
        if len(signals) == 1:
            result_vig.extend(signals)
            continue

        # Cap: find max single-signal contracts (the highest-edge signal sets the limit)
        signals_sorted = sorted(signals, key=lambda x: abs(x.get("edge", 0)), reverse=True)
        single_max = signals_sorted[0].get("recommended_contracts", 10)
        per_signal = max(1, single_max // len(signals))

        for sig in signals:
            sig = sig.copy()
            original = sig.get("recommended_contracts", 10)
            sig["recommended_contracts"] = per_signal
            sig.setdefault("warnings", []).append(
                f"correlated: capped from {original} to {per_signal} contracts "
                f"({len(signals)} signals in {series})"
            )
            result_vig.append(sig)

    return other + result_vig
```

### Step 4: Wire into `scan_cycle()` in `bot/scanner.py`

After vig stack opps are assembled and before `all_opportunities.extend(vig_stack_opps)`:

```python
vig_stack_opps = _cap_correlated_vig_stack(vig_stack_opps)
```

### Step 5: Check where `recommended_contracts` is set

The sizing logic lives in `bot/main.py` (look for Kelly calculation or `recommended_contracts`). The cap function modifies this field. If it's not named `recommended_contracts`, find the actual field name and update the function accordingly.

### Step 6: Run tests — verify PASS

```bash
python3 -m pytest tests/test_to_8.py -k "correlated_vig or uncorrelated_vig or non_vig" -v
```

### Step 7: Full suite

```bash
python3 -m pytest --tb=no -q
```

### Step 8: Commit

```bash
git add bot/scanner.py tests/test_to_8.py
git commit -m "feat: correlated position cap for vig stack series signals"
```

---

## Final Verification

After all 5 tasks:

```bash
# All tests pass
python3 -m pytest --tb=short -q

# Run a live scan and verify output includes:
# - [TRACKER] lines on resolution checks
# - [CALIBRATION] table (after enough resolved samples)
# - "price moving against" warnings (if prices shifted)
# - "Game starts in: Xh Ym" instead of "Closes in: 364h"  (already done)
# - Weather σ scales (check math in EDGE lines for 1-day vs 2-day)
# - Correlated vig stack cap messages
python3 bot/main.py
```

Expected: clean scan with all 5 systems active, no regressions.
