"""
Glint Trading Bot — Live Game Watcher

Two activation modes:
1. Manual: Telegram WATCH <name> — locks onto a specific game/match
2. Auto-scan: scan_live_matches() — discovers live 1v1 matches on Kalshi
   with clear leaders, auto-starts momentum watchers for each.

Two trading strategies:
- Arb mode: ESPN consensus vs Kalshi latency arbitrage (team sports)
- Momentum mode: Buy the clear leader on dips (1v1 matches — tennis, UFC)

Sends one Telegram message per match, edits in-place every tick — no spam.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import certifi

from bot.config import (
    ESPN_BASE, ESPN_SPORT_PATHS, ACTIVE_SPORTS,
    LIVE_POLL_INTERVAL, LIVE_WATCH_EDGE_THRESHOLD,
    LIVE_TAKE_PROFIT_CENTS, LIVE_NEAR_SETTLE_CENTS,
    LIVE_STOP_LOSS_CENTS,
    MOMENTUM_LEADER_MIN, MOMENTUM_DIP_BUY, MOMENTUM_DIP_MAX,
    MOMENTUM_PRICE_WINDOW,
    MOMENTUM_MAX_ENTRIES, MOMENTUM_REENTRY_COOLDOWN,
    MOMENTUM_SCALE_SMALL_DIP, MOMENTUM_SCALE_MED_DIP, MOMENTUM_SCALE_LARGE_DIP,
    MOMENTUM_DQS_THRESHOLD, MOMENTUM_DQS_TRAIL_STOP,
    MOMENTUM_DISABLED_SPORTS,
    TENNIS_QUALITY_MIN_TICKS, TENNIS_QUALITY_MIN_RANGE,
    SPORT_PROFILES,
    PAPER_MODE,
)
import bot.odds_scraper as _odds
from collections import deque

logger = logging.getLogger("glint.live_watcher")

_ESPN_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# Strips "Game 3:", "Round 2:" and similar playoff-series prefixes from market
# titles so team-name extraction still works during playoffs.
_PLAYOFF_PREFIX_RE = re.compile(r"^(game|round)\s+\d+\s*:\s*", re.IGNORECASE)

_LIVE_STATUSES = {"STATUS_IN_PROGRESS", "STATUS_HALFTIME", "STATUS_END_PERIOD"}
_FINAL_STATUSES = {"STATUS_FINAL", "STATUS_POSTPONED", "STATUS_CANCELED"}

_STATE_DIR = pathlib.Path(__file__).resolve().parent / "state"
LIVE_JOURNAL_FILE = _STATE_DIR / "live_journal.json"


def _journal_append(entry: dict):
    """Append an entry to the live watcher journal (thread-safe-ish)."""
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        data = json.loads(LIVE_JOURNAL_FILE.read_text()) if LIVE_JOURNAL_FILE.exists() else []
        data.append(entry)
        LIVE_JOURNAL_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning("Journal write error: %s", e)


def get_daily_recap(date_str: str | None = None) -> str:
    """
    Build a human-readable recap of all live watcher activity for a given date.
    date_str: "2026-04-09" or None for today.
    """
    from datetime import date
    target = date_str or date.today().isoformat()

    try:
        data = json.loads(LIVE_JOURNAL_FILE.read_text()) if LIVE_JOURNAL_FILE.exists() else []
    except Exception:
        return "No journal data found."

    day_entries = [e for e in data if e.get("timestamp", "").startswith(target)]
    if not day_entries:
        return f"No live watcher activity on {target}."

    # Group by match
    matches: dict[str, list[dict]] = {}
    for e in day_entries:
        key = e.get("ticker") or e.get("match", "unknown")
        matches.setdefault(key, []).append(e)

    lines = [f"LIVE WATCHER RECAP — {target}", ""]

    total_bets = 0
    total_exits = 0
    total_pnl = 0.0
    matches_watched = set()

    for ticker, entries in matches.items():
        match_name = ""
        for e in entries:
            if e.get("match"):
                match_name = e["match"]
                break

        scans = [e for e in entries if e.get("event") == "scan_found"]
        bets = [e for e in entries if e.get("event") == "bet"]
        exits = [e for e in entries if e.get("event") == "exit"]
        sessions = [e for e in entries if e.get("event") == "session_end"]

        matches_watched.add(ticker)
        header = match_name or ticker
        lines.append(f"{'─' * 40}")
        lines.append(f"{header}")

        if scans:
            s = scans[0]
            lines.append(f"  Found: {s.get('sport','?').upper()} | leader at {s.get('price',0)}¢ | vol {s.get('volume',0):,}")

        for b in bets:
            total_bets += 1
            lines.append(f"  BUY: {b.get('side','?').upper()} {b.get('contracts',0)}x @ {b.get('price_cents',0)}¢ — {b.get('reason','')}")

        for ex in exits:
            total_exits += 1
            pnl = (ex.get("pnl") or 0)
            total_pnl += pnl
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            lines.append(f"  EXIT: {ex.get('reason','')} ({pnl_str})")

        for s in sessions:
            dur = s.get("duration_min", 0)
            lines.append(f"  Session: {dur:.0f} min")

        lines.append("")

    # Summary
    lines.append("═" * 40)
    lines.append("SUMMARY")
    lines.append(f"  Matches watched: {len(matches_watched)}")
    lines.append(f"  Bets placed: {total_bets}")
    lines.append(f"  Exits: {total_exits}")
    pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    lines.append(f"  Total P&L: {pnl_str}")

    # Tick stats
    tick_entries = [e for e in day_entries if e.get("event") == "tick_summary"]
    if tick_entries:
        total_ticks = sum(e.get("ticks", 0) for e in tick_entries)
        lines.append(f"  Total ticks: {total_ticks:,}")

    return "\n".join(lines)


TICK_LOG_FILE = _STATE_DIR / "live_ticks.jsonl"


def _log_tick(data: dict):
    """Append a tick record to the JSONL tick log for post-analysis."""
    data["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(TICK_LOG_FILE, "a") as f:
            f.write(json.dumps(data) + "\n")
    except Exception as e:
        logger.warning("_log_tick write failed: %s", e)


def analyze_ticks(date_str: str | None = None) -> str:
    """
    Analyze tick-level data to find the real edge.
    Returns a tuning report showing:
    - Best entries (what dip size led to profitable exits)
    - Optimal leader % range
    - Hold time analysis
    - Win rate by sport, by leader %, by dip size
    - Missed opportunities (dips we didn't buy that would have been profitable)
    """
    from datetime import date
    target = date_str or date.today().isoformat()

    if not TICK_LOG_FILE.exists():
        return "No tick data yet. Run the bot for a day first."

    ticks = []
    with open(TICK_LOG_FILE) as f:
        for line in f:
            try:
                t = json.loads(line.strip())
                if t.get("ts", "").startswith(target):
                    ticks.append(t)
            except Exception:
                continue

    if not ticks:
        return f"No tick data for {target}."

    # Group ticks by ticker
    by_ticker: dict[str, list[dict]] = {}
    for t in ticks:
        by_ticker.setdefault(t.get("ticker", "?"), []).append(t)

    lines = [f"TUNING ANALYSIS — {target}", ""]

    # Per-match analysis
    all_dip_profits = []  # (dip_size, max_gain_after, ticker)
    all_entries = []  # (leader_pct, dip_size, held, pnl_pct)

    for ticker, match_ticks in by_ticker.items():
        prices = [t["price"] for t in match_ticks if "price" in t]
        if len(prices) < 5:
            continue

        match_name = match_ticks[0].get("match", ticker)
        high = max(prices)
        low = min(prices)
        start_price = prices[0]
        end_price = prices[-1]
        volatility = high - low

        lines.append(f"{'─' * 40}")
        lines.append(f"{match_name}")
        lines.append(f"  Range: {low}c → {high}c (vol={volatility}c)")
        lines.append(f"  Start: {start_price}c | End: {end_price}c")
        lines.append(f"  Ticks: {len(prices)}")

        # Find all dips and what happened after each dip
        for i in range(2, len(prices)):
            window_high = max(prices[max(0, i - MOMENTUM_PRICE_WINDOW):i])
            dip = window_high - prices[i]
            if dip >= 2:  # any dip of 2+ cents
                # What was the max price after this dip?
                future_prices = prices[i:]
                max_after = max(future_prices) if future_prices else prices[i]
                gain_after = max_after - prices[i]
                all_dip_profits.append((dip, gain_after, ticker))

        # Entry analysis (from journal bets for this ticker)
        bets = [t for t in match_ticks if t.get("event") == "entry"]
        for b in bets:
            entry_price = b.get("entry_price", 0)
            leader_pct = b.get("leader_pct", 0)
            # Find what happened after entry
            entry_idx = None
            for idx, p in enumerate(prices):
                if abs(p - entry_price) <= 1:
                    entry_idx = idx
                    break
            if entry_idx is not None:
                future = prices[entry_idx:]
                max_future = max(future) if future else entry_price
                all_entries.append((leader_pct, 0, len(future), (max_future - entry_price) / entry_price * 100))

        lines.append("")

    # Dip analysis
    if all_dip_profits:
        lines.append("═" * 40)
        lines.append("DIP ANALYSIS (would this dip be profitable?)")
        lines.append("")

        # Bucket by dip size
        buckets = {}
        for dip, gain, _ in all_dip_profits:
            bucket = int(dip)  # 2c, 3c, 4c, etc.
            buckets.setdefault(bucket, []).append(gain)

        lines.append(f"{'Dip':>5} | {'Count':>5} | {'Avg Gain':>8} | {'Win%':>5} | {'Verdict'}")
        lines.append(f"{'─' * 50}")
        for dip_size in sorted(buckets.keys()):
            gains = buckets[dip_size]
            avg_gain = sum(gains) / len(gains)
            win_pct = len([g for g in gains if g > 1]) / len(gains) * 100
            verdict = "BUY" if win_pct > 60 and avg_gain > 2 else "SKIP" if win_pct < 40 else "MAYBE"
            lines.append(f"{dip_size:>4}c | {len(gains):>5} | {avg_gain:>7.1f}c | {win_pct:>4.0f}% | {verdict}")

        lines.append("")
        lines.append("RECOMMENDATIONS:")
        best_bucket = max(buckets.items(), key=lambda x: sum(x[1]) / len(x[1]) if x[1] else 0)
        lines.append(f"  Best dip size to buy: {best_bucket[0]}c (avg gain {sum(best_bucket[1]) / len(best_bucket[1]):.1f}c)")

        profitable_dips = [(d, g) for d, g, _ in all_dip_profits if g > 2]
        if profitable_dips:
            avg_profitable_dip = sum(d for d, _ in profitable_dips) / len(profitable_dips)
            lines.append(f"  Avg profitable dip: {avg_profitable_dip:.1f}c")

    # Overall stats
    lines.append("")
    lines.append("═" * 40)
    lines.append(f"TOTAL: {len(by_ticker)} matches tracked, {len(ticks)} ticks logged")
    lines.append(f"DIP_BUY threshold: {MOMENTUM_DIP_BUY * 100:.0f}c (adjust if analysis suggests different)")

    return "\n".join(lines)


def _normalize(s: str) -> str:
    return s.lower().strip()


class LiveGameWatcher:
    """
    Watches a single live game and auto-bets Kalshi latency arb edges.

    Usage:
        watcher = LiveGameWatcher("lakers", notifier, sport="nba", balance=500.0)
        task = asyncio.create_task(watcher.start())
        # Later:
        watcher.stop()
    """

    def __init__(
        self,
        query: str,
        notifier,
        sport: str | None = None,
        balance: float = 0.0,
        mode: str = "auto",        # "auto", "arb", or "momentum"
        ticker: str | None = None,          # pre-resolved ticker (skip re-search)
        opponent_ticker: str | None = None, # pre-resolved opponent ticker
    ):
        self.query = _normalize(query)
        self.notifier = notifier
        self.sport = _normalize(sport) if sport else None
        self.balance = balance
        self.mode = mode            # resolved in start()

        self.active = True
        self.ticker: str | None = ticker        # matched Kalshi ticker (may be pre-set by scanner)
        self.status_msg_id: int | None = None   # Telegram message to edit
        self.bets_placed: list[dict] = []       # bets placed this session
        self.exits: list[dict] = []             # exited positions this session
        self._started_at = time.time()
        self._peak_values: dict[str, float] = {}   # ticker → highest mark-to-market value
        self._trailing_active: dict[str, bool] = {}  # ticker → trailing stop engaged
        # NOTE: _underwater_ticks removed Apr 16 with UNDERWATER EXIT branch —
        # data-killed in Apr 14 audit. HARD STOP-LOSS + DOLLAR STOP cover the risk.

        # Momentum mode state
        self._price_history: deque = deque(maxlen=MOMENTUM_PRICE_WINDOW)
        self._opp_price_history: deque = deque(maxlen=MOMENTUM_PRICE_WINDOW)  # opponent prices
        self._opponent_ticker: str | None = opponent_ticker  # the other side of a 1v1 match
        self._match_title: str = ""
        self._entry_count: int = 0            # total entries this match (caps at MAX_ENTRIES)
        self._cooldown_remaining: int = 0     # ticks until re-entry allowed after exit
        self._last_exit_side: str | None = None  # which side we last exited (avoid same-side chasing)

        # Game intelligence
        from bot.game_context import GameContext
        self._game_ctx: GameContext | None = GameContext(sport=sport or "")
        self._espn_tick_counter: int = 0
        self._last_espn_data: dict = {}

        # Session 6 — dampener for the per-decision audit log. Without this,
        # a flat-market live watcher would emit ~6 reject records per second
        # per ticker (50k records/day per match). We log only on state
        # transitions (decision, reason) — first new transition flushes.
        self._last_decision: tuple[str, str] | None = None

        # Tick-level telemetry — counts each decision point in _tick_momentum.
        # Written to the session_end journal entry so we can answer "why did
        # this watcher never bet?" from post-hoc replay. Mirrors the pattern
        # used in scanner.py (_telem dict + end-of-function report).
        self._tick_telem: dict = {
            "ticks": 0,              # total tick executions
            "no_leader": 0,          # neither side at/above MOMENTUM_LEADER_MIN
            "dip_too_big": 0,        # dip outside (min, max) window
            "dqs_fail": 0,           # DQS < MOMENTUM_DQS_THRESHOLD
            "conviction_checked": 0, # conviction gate evaluated
            "conviction_eligible": 0,# conviction_ok == True
            "conviction_near_miss": 0,# wp_edge >=5% but gate failed
            "instinct_avoid": 0,     # SportInstincts.should_avoid_entry tick
            "execute_attempt": 0,    # called _auto_bet_momentum
            "execute_success": 0,    # execute_trade returned success
            "execute_failed": {},    # keyed by reason string from executor
        }

    # ------------------------------------------------------------------
    # Session 6 — dampened decision log
    # ------------------------------------------------------------------
    def _log_decision_dampened(
        self,
        decision: str,
        reason: str,
        gates: dict[str, bool],
        edge: float | None = None,
        extra: dict | None = None,
        close_ts: str | None = None,
    ) -> None:
        """Emit a decisions.log_decision row only on (decision, reason) state
        change. A flat-market live watcher would otherwise emit ~6 records/sec
        per ticker — the dampener compresses to one record per transition.

        Session 15.5: optional close_ts kwarg threads market close into extra
        so bot.regime.tag can populate event_horizon_hr. An explicit close_ts
        in `extra` always wins over the kwarg fallback.
        """
        key = (decision, reason)
        if key == self._last_decision:
            return
        self._last_decision = key
        merged = dict(extra) if extra else None
        if close_ts and (merged is None or "close_ts" not in merged):
            merged = merged or {}
            merged["close_ts"] = close_ts
        try:
            from bot import decisions
            decisions.log_decision(
                ticker=self.ticker or "",
                opp_type="live_momentum",
                edge=edge,
                gates=gates,
                decision=decision,
                reason=reason,
                extra=merged,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> str:
        """
        Main entry point. Returns a summary string when the game/match ends
        or the watcher is stopped.

        Two modes:
        - "arb": ESPN consensus vs Kalshi latency arbitrage
        - "momentum": Buy the clear leader on dips (tennis, UFC, NBA, MLB, NHL)
        - "auto": Try momentum first (search all series), fall back to arb
        """
        from bot.kalshi_series import MATCH_SERIES, SPORTS_SERIES

        # --- Auto-detect mode ---
        if self.mode == "auto":
            # Try momentum first (search 1v1 match series)
            found_match = await self._find_match_market()
            if found_match:
                self.mode = "momentum"
            else:
                self.mode = "arb"

        if self.mode == "momentum":
            return await self._start_momentum()
        else:
            return await self._start_arb()

    async def _start_momentum(self) -> str:
        """Momentum mode: find 1v1 match on Kalshi, buy the leader on dips."""
        # If scanner pre-resolved the ticker, skip the search — just fetch the title
        if self.ticker and self._opponent_ticker:
            player_name = self.query
            opponent_name = "?"
            # Fetch real market title for the direction checker
            try:
                from agent.kalshi_client import get_market
                loop = asyncio.get_event_loop()
                mkt = await loop.run_in_executor(None, lambda: get_market(self.ticker))
                m = mkt.get("market", mkt)
                self._match_title = m.get("title", self.query)
            except Exception:
                self._match_title = self.query
        else:
            match = await self._find_match_market()
            if not match:
                return f"No live match found for '{self.query}'. Check the name and try again."

            self.ticker = match["ticker"]
            self._opponent_ticker = match.get("opponent_ticker")
            self._match_title = match.get("title", "")
            player_name = match.get("player_name", self.query)
            opponent_name = match.get("opponent_name", "?")

        initial_text = (
            f"LIVE WATCH (MOMENTUM): {player_name.upper()}\n"
            f"Match: {self._match_title}\n"
            f"Market: {self.ticker}\n"
            f"Strategy: Buy dips on leader | Trailing stop\n"
            f"Polling every {LIVE_POLL_INTERVAL}s..."
        )
        self.status_msg_id = await self.notifier.send_message_get_id(initial_text)

        logger.info(
            "LiveGameWatcher MOMENTUM started: query=%s ticker=%s opponent=%s",
            self.query, self.ticker, self._opponent_ticker,
        )

        while self.active:
            try:
                await self._tick_momentum()
            except Exception as e:
                logger.warning("LiveGameWatcher momentum tick error: %s", e, exc_info=True)
            await asyncio.sleep(LIVE_POLL_INTERVAL)

        return self._format_session_summary(player_name, opponent_name)

    async def _start_arb(self) -> str:
        """Arb mode: ESPN consensus vs Kalshi latency arbitrage."""
        if not self.sport:
            self.sport = self._detect_sport()

        game = self._find_game()
        if not game:
            return f"No live game found matching '{self.query}'. Check the team name and try again."

        home = game.get("home_team", "?")
        away = game.get("away_team", "?")

        self.ticker = await self._find_kalshi_ticker(home, away)
        if not self.ticker:
            return (
                f"Found live game: {away} @ {home}\n"
                f"But no open Kalshi market found. May not be listed yet."
            )

        initial_text = (
            f"LIVE WATCH (ARB): {self.query.upper()}\n"
            f"Game: {away} @ {home}\n"
            f"Market: {self.ticker}\n"
            f"Polling every {LIVE_POLL_INTERVAL}s..."
        )
        self.status_msg_id = await self.notifier.send_message_get_id(initial_text)

        logger.info(
            "LiveGameWatcher ARB started: query=%s sport=%s ticker=%s",
            self.query, self.sport, self.ticker,
        )

        while self.active:
            try:
                await self._tick(game)
            except Exception as e:
                logger.warning("LiveGameWatcher tick error: %s", e)
            await asyncio.sleep(LIVE_POLL_INTERVAL)

        return self._format_session_summary(home, away)

    def stop(self):
        """Stop the watcher loop gracefully."""
        self.active = False

    # ------------------------------------------------------------------
    # Momentum: match discovery
    # ------------------------------------------------------------------

    async def _find_match_market(self) -> dict | None:
        """
        Search all match/game series on Kalshi for a market matching self.query.
        Covers 1v1 (tennis, UFC) AND team sports (NBA, MLB, NHL).
        Returns dict with ticker, opponent_ticker, title, etc.
        """
        from agent.kalshi_client import get_markets
        from bot.kalshi_series import MATCH_SERIES, SPORTS_SERIES

        loop = asyncio.get_event_loop()
        q = self.query

        all_series = {**MATCH_SERIES, **SPORTS_SERIES}
        for sport, series in all_series.items():
            try:
                result = await loop.run_in_executor(
                    None, lambda s=series: get_markets(series_ticker=s, status="open", limit=200)
                )
            except Exception as e:
                logger.debug("Match search error for %s: %s", series, e)
                continue

            markets = result.get("markets", [])

            # Group markets by event (same match has 2 sides — player A and player B)
            # Match titles look like: "Will X win the X vs Y : Round match?"
            for m in markets:
                title = (m.get("title") or "").lower()
                ticker = m.get("ticker", "")
                if len(q) >= 3 and q in title:
                    # Found our player — now find the opponent side
                    # Event ticker groups both sides of the same match
                    event_tk = m.get("event_ticker", "")
                    opponent = None
                    player_name = self._extract_player_name(title, q)

                    if event_tk:
                        for other in markets:
                            if (other.get("event_ticker") == event_tk
                                    and other.get("ticker") != ticker):
                                opponent = other
                                break

                    self.sport = sport
                    return {
                        "ticker": ticker,
                        "opponent_ticker": opponent.get("ticker") if opponent else None,
                        "title": m.get("title", ""),
                        "player_name": player_name,
                        "opponent_name": self._extract_player_name(
                            (opponent.get("title") or "").lower(),
                            ""
                        ) if opponent else "?",
                        "yes_ask": m.get("yes_ask", 50),
                        "event_ticker": event_tk,
                    }

        return None

    @staticmethod
    def _extract_player_name(title: str, query: str) -> str:
        """Extract player/team name from market title.

        Handles:
        - Tennis: "Will Harry Wendelken win the Fomin vs Wendelken..."
        - NBA:    "Phoenix at Los Angeles L Winner?" → extracts team matching query
        - Generic: "X at Y Winner?" or "X vs Y Winner?"
        - Playoff series prefix: "Game 3: New York at Atlanta Winner?" → strips "game 3:"
        """
        title = title.lower()
        # Strip playoff-series prefix ("game N:", "round N:") so downstream
        # substring matching against ESPN's displayName works. NBA/NHL/MLB
        # playoffs introduced these prefixes around Apr 19 2026 and broke
        # `_fetch_espn_score()`'s `q in home_name or q in away_name` check.
        title = _PLAYOFF_PREFIX_RE.sub("", title, count=1).strip()
        # Tennis / UFC style: "Will X win..."
        if "will " in title and " win " in title:
            name = title.split("will ", 1)[1].split(" win ", 1)[0].strip()
            return name.title()
        # Team sport style: "X at Y Winner?" or "X vs Y Winner?"
        for sep in (" at ", " vs ", " vs. "):
            if sep in title:
                parts = title.split(sep, 1)
                away = parts[0].strip().rstrip("?").strip()
                home = parts[1].replace("winner?", "").replace("winner", "").strip().rstrip("?").strip()
                # If we have a query, return the matching team
                if query:
                    if query in away:
                        return away.title()
                    if query in home:
                        return home.title()
                # No query — return the first team (away)
                return away.title()
        if query:
            return query.title()
        return "?"

    # ------------------------------------------------------------------
    # Momentum tick
    # ------------------------------------------------------------------

    def _fetch_opponent_market(self) -> dict | None:
        """Fetch current price for the opponent side of this match."""
        if not self._opponent_ticker:
            return None
        from agent.kalshi_client import get_market
        result = get_market(self._opponent_ticker)
        if "error" in result:
            err = str(result.get("error", ""))
            if "429" in err or "rate" in err.lower():
                return {"_rate_limited": True}
            return None
        return result.get("market", result)

    def _dip_size_multiplier(self, dip_cents: float) -> float:
        """Scale bet size by dip size — DATA: bigger dips = better bounce.

        59k ticks analysis:
          dip 4-5c: 52.9% win, +0.43c avg (noise)
          dip 6-8c: 57.7% win, +1.64c avg (signal)
          dip 9-12c: 65.2% win, +3.67c avg (strong signal)

        Bet MORE on bigger dips, not less.
        """
        profile = self._get_sport_profile()
        min_dip = profile.get("min_dip", 4)

        if dip_cents >= min_dip + 6:    # e.g., 12c+ for NBA (min=6)
            return MOMENTUM_SCALE_LARGE_DIP   # 1.5x — biggest dips = best signal
        elif dip_cents >= min_dip + 3:  # e.g., 9c+ for NBA
            return MOMENTUM_SCALE_MED_DIP     # 1.2x
        else:
            return MOMENTUM_SCALE_SMALL_DIP   # 1.0x — just met threshold

    def _get_sport_profile(self) -> dict:
        """Get sport-specific trading parameters. Falls back to defaults."""
        default = {
            "min_dip": int(MOMENTUM_DIP_BUY * 100),
            "max_dip": int(MOMENTUM_DIP_MAX * 100),
            "max_entry": 75,
            "min_score_diff": 0,
            "periods": 4,
            "late_game_period": 3,
        }
        if self.sport and self.sport in SPORT_PROFILES:
            return SPORT_PROFILES[self.sport]
        return default

    def _variance_quality_ok(self, price_history: deque) -> tuple[bool, str]:
        """Tier 2.4: lightweight dip-quality gate for sports that skip full DQS.

        Reject "flat" dips where the last N ticks have almost no price movement
        — that's a set break / changeover / timeout with no information, not
        a real dip worth buying. Returns (ok, reason).
        """
        if len(price_history) < TENNIS_QUALITY_MIN_TICKS:
            # Benefit of the doubt early in the watch — don't block on thin history
            return True, "not_enough_history"
        last_n = list(price_history)[-TENNIS_QUALITY_MIN_TICKS:]
        price_range = max(last_n) - min(last_n)
        if price_range < TENNIS_QUALITY_MIN_RANGE:
            return False, f"flat_{price_range}c_range_over_{TENNIS_QUALITY_MIN_TICKS}ticks"
        return True, "ok"

    def _compute_dqs(
        self, *,
        dip_cents: int,
        price: int,
        score_diff: int | None,
        period: int | None,
        price_history: deque,
        sport_profile: dict,
    ) -> tuple[float, dict]:
        """
        Compute Dip Quality Score (0.0 - 1.0).

        Factors:
        1. Score context (0.35 weight) — is the team actually ahead by enough?
        2. Game stage (0.25 weight) — late game dips on leaders are highest quality
        3. Price level (0.15 weight) — stronger leaders (70¢) revert better than weak (61¢)
        4. Volatility filter (0.15 weight) — dip must exceed recent noise level
        5. Dip size sweet spot (0.10 weight) — 4-6¢ dips revert best, too big = real shift

        Returns (dqs_score, breakdown_dict) for logging.
        """
        breakdown = {}

        # --- 1. Score Context (weight 0.35) ---
        # If we have live score data, use it. No score = neutral 0.5.
        if score_diff is not None:
            min_diff = sport_profile.get("min_score_diff", 0)
            if score_diff <= 0:
                # Team we're buying is LOSING — very bad signal
                score_score = 0.1
            elif score_diff < min_diff:
                # Ahead but not by enough — weak
                score_score = 0.3
            elif score_diff >= min_diff * 3:
                # Blowout — dip is almost certainly noise
                score_score = 1.0
            elif score_diff >= min_diff * 2:
                # Comfortable lead
                score_score = 0.85
            else:
                # Meets minimum — decent
                score_score = 0.65
        else:
            # No score data — neutral, rely on other signals
            score_score = 0.5
        breakdown["score"] = round(score_score, 2)

        # --- 2. Game Stage (weight 0.25) ---
        # Late game + leading = dips are noise. Early game = too uncertain.
        if period is not None and period > 0:
            total_periods = sport_profile.get("periods", 4)
            late_period = sport_profile.get("late_game_period", 3)
            if period >= late_period:
                # Late game — if leading, dips are high quality
                if score_diff is not None and score_diff > 0:
                    stage_score = 0.95
                else:
                    stage_score = 0.4  # late game but not leading = risky
            elif period >= total_periods // 2:
                # Mid game
                stage_score = 0.6
            else:
                # Early game — lots of time for things to change
                stage_score = 0.35
        else:
            stage_score = 0.5
        breakdown["stage"] = round(stage_score, 2)

        # --- 3. Price Level (weight 0.15) ---
        # Higher prices = stronger leader = more likely to revert
        if price >= 72:
            price_score = 0.9
        elif price >= 68:
            price_score = 0.75
        elif price >= 65:
            price_score = 0.6
        elif price >= 62:
            price_score = 0.45
        else:
            price_score = 0.3  # barely a leader at 60-61¢
        breakdown["price_level"] = round(price_score, 2)

        # --- 4. Volatility Filter (weight 0.15) ---
        # Dip should exceed recent price noise (standard deviation)
        if len(price_history) >= 4:
            prices = list(price_history)
            mean_p = sum(prices) / len(prices)
            variance = sum((p - mean_p) ** 2 for p in prices) / len(prices)
            stddev = variance ** 0.5
            if stddev > 0:
                # How many stddevs is this dip?
                z_score = dip_cents / stddev
                if z_score >= 2.5:
                    vol_score = 1.0   # dip is way outside normal noise
                elif z_score >= 1.5:
                    vol_score = 0.75
                elif z_score >= 1.0:
                    vol_score = 0.5
                else:
                    vol_score = 0.2   # dip is within normal noise — bad signal
            else:
                vol_score = 0.8  # no variance = stable price, any dip is notable
        else:
            vol_score = 0.5  # not enough data yet
        breakdown["volatility"] = round(vol_score, 2)

        # --- 5. Dip Size Sweet Spot (weight 0.10) ---
        # 4-6¢ = optimal recovery zone. Above 6¢ = diminishing returns.
        if 4 <= dip_cents <= 6:
            dip_score = 0.9
        elif dip_cents == 7:
            dip_score = 0.6
        elif dip_cents == 8:
            dip_score = 0.4
        else:
            dip_score = 0.3
        breakdown["dip_sweet"] = round(dip_score, 2)

        # --- Weighted Average ---
        dqs = (
            score_score * 0.35
            + stage_score * 0.25
            + price_score * 0.15
            + vol_score * 0.15
            + dip_score * 0.10
        )
        breakdown["total"] = round(dqs, 3)
        return round(dqs, 3), breakdown

    async def _tick_momentum(self):
        """
        Momentum tick: fetch Kalshi price → track history → buy dips on leader → exit.
        Now trades BOTH sides of the match and supports re-entry after exit.
        """
        self._tick_telem["ticks"] += 1
        loop = asyncio.get_event_loop()

        market = await loop.run_in_executor(None, self._fetch_kalshi_market)
        if not market:
            self._gone_ticks = getattr(self, "_gone_ticks", 0) + 1
            if self._gone_ticks >= 3:
                logger.info("LiveGameWatcher momentum: market gone for %d ticks — stopping", self._gone_ticks)
                self.active = False
            return
        if market.get("_rate_limited"):
            # Don't increment _gone_ticks — market exists, we're just throttled
            return
        self._gone_ticks = 0

        # Session 15.5: thread market close into every dampener call so
        # bot.regime.tag can populate event_horizon_hr on live_momentum decisions.
        _close_ts = market.get("close_ts") or market.get("close_time")

        yes_ask = market.get("yes_ask", 0)
        if not yes_ask:
            return

        # Also fetch opponent side (for both-sides trading)
        opp_market = None
        opp_yes_ask = 0
        if self._opponent_ticker:
            opp_market = await loop.run_in_executor(None, self._fetch_opponent_market)
            if opp_market and not opp_market.get("_rate_limited"):
                opp_yes_ask = opp_market.get("yes_ask", 0)

        # Settled? Check BOTH sides — either market hitting 97+/3- means match over.
        # Using 97 instead of 99 to catch markets stuck at 98¢ that never tick to 99.
        primary_settled = yes_ask >= 97 or yes_ask <= 3
        opp_settled = opp_yes_ask >= 97 or opp_yes_ask <= 3
        if primary_settled or opp_settled:
            logger.info(
                "LiveGameWatcher momentum: match settled (primary=%dc opp=%dc) — stopping",
                yes_ask, opp_yes_ask,
            )
            for bet in list(self.bets_placed):
                bet_ticker = bet.get("ticker")
                # Determine settlement value for THIS bet's ticker
                if bet_ticker == self.ticker:
                    settle_val = yes_ask if primary_settled else (1 if opp_yes_ask >= 97 else 99)
                elif bet_ticker == self._opponent_ticker:
                    settle_val = opp_yes_ask if opp_settled else (1 if yes_ask >= 97 else 99)
                else:
                    settle_val = 99 if (yes_ask >= 97 or opp_yes_ask <= 3) else 1
                entry_p = bet.get("price_cents", 0)
                contracts = bet.get("contracts", 1)
                pnl = (settle_val - entry_p) / 100.0 * contracts
                reason = f"SETTLED at {settle_val}¢ ({'WIN' if pnl > 0 else 'LOSS'})"
                self.exits.append({**bet, "reason": reason, "pnl": pnl})
                self.bets_placed.remove(bet)
                logger.info("LiveGameWatcher SETTLE EXIT: %s pnl=$%.2f", bet_ticker, pnl)
                _journal_append({
                    "event": "exit",
                    "match": self._match_title or self.query,
                    "ticker": bet_ticker,
                    "side": bet.get("side"),
                    "reason": reason,
                    "pnl": pnl,
                    "entry_price": entry_p,
                    "exit_value": settle_val,
                    "peak_value": self._peak_values.get(bet_ticker, settle_val),
                    "mode": self.mode,
                })
            self.active = False
            return

        # Track price history for both sides
        self._price_history.append(yes_ask)
        if opp_yes_ask > 0:
            self._opp_price_history.append(opp_yes_ask)

        # Tick cooldown timer
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1

        # Current state — primary side
        player_prob = yes_ask / 100.0
        is_leader = player_prob >= MOMENTUM_LEADER_MIN
        recent_high = max(self._price_history) if self._price_history else yes_ask
        dip_cents = recent_high - yes_ask

        # Current state — opponent side
        opp_is_leader = False
        opp_recent_high = 0
        opp_dip_cents = 0
        if opp_yes_ask > 0:
            opp_prob = opp_yes_ask / 100.0
            opp_is_leader = opp_prob >= MOMENTUM_LEADER_MIN
            opp_recent_high = max(self._opp_price_history) if self._opp_price_history else opp_yes_ask
            opp_dip_cents = opp_recent_high - opp_yes_ask

        # Telemetry: neither side is a leader on this tick (can't enter)
        if not is_leader and not opp_is_leader:
            self._tick_telem["no_leader"] += 1

        elapsed = int(time.time() - self._started_at)

        # --- Fetch live game context (ESPN score) ---
        # Throttle ESPN calls to every 3rd tick (~30s) to avoid hammering
        espn_data = self._last_espn_data
        if self._espn_tick_counter % 3 == 0:
            try:
                espn_data = await loop.run_in_executor(None, self._fetch_espn_score)
                self._last_espn_data = espn_data
            except Exception:
                # _fetch_espn_score handles its own errors internally; this only
                # catches executor-level failures (cancellation, loop teardown).
                # Keep stale _last_espn_data rather than resetting to empty.
                logger.exception("ESPN executor error (ticker=%s)", self.ticker)
        self._espn_tick_counter += 1

        # Derive score differential and update GameContext
        score_diff = None
        period = None
        our_score = 0
        their_score = 0
        if espn_data:
            period = espn_data.get("period", 0)
            if not period:
                import re
                period_match = re.search(r"(\d+)", espn_data.get("period_label", ""))
                period = int(period_match.group(1)) if period_match else None

            h_score = espn_data.get("home_score", 0)
            a_score = espn_data.get("away_score", 0)
            clock_str = espn_data.get("clock", "")

            # Figure out which team is "ours" from the ticker
            our_is_home = False
            if self.ticker:
                suffix = self.ticker.rsplit("-", 1)[-1].lower()
                home_name = _normalize(str(espn_data.get("home_name", "")))
                away_name = _normalize(str(espn_data.get("away_name", "")))
                if suffix in home_name:
                    our_is_home = True
                    score_diff = h_score - a_score
                elif suffix in away_name:
                    our_is_home = False
                    score_diff = a_score - h_score
                else:
                    our_is_home = is_leader
                    score_diff = abs(h_score - a_score) if is_leader else -abs(h_score - a_score)

            our_score = h_score if our_is_home else a_score
            their_score = a_score if our_is_home else h_score

            # Update GameContext with fresh data
            if self._game_ctx and period:
                self._game_ctx.update(
                    espn_data=espn_data,
                    our_score=our_score,
                    their_score=their_score,
                    period=period,
                    clock_str=clock_str,
                )

        # Get sport-specific profile
        sport_profile = self._get_sport_profile()

        # --- Exit logic (runs first) ---
        # SAFETY: if we hold an opponent position but couldn't fetch opponent
        # market, skip exit check this tick — don't evaluate against wrong price
        if self.bets_placed:
            holding_opp = any(b.get("ticker") == self._opponent_ticker for b in self.bets_placed)
            if holding_opp and not opp_market:
                logger.debug("Skipping exit check — holding opponent but opp_market unavailable")
            else:
                prev_bet_count = len(self.bets_placed)
                edge = 0.0
                relative_edge = 0.0
                await self._check_exit(
                    market, edge, relative_edge,
                    opp_market=opp_market,
                    score_diff=score_diff,
                    period=period,
                )
                # If we just exited, start cooldown before re-entry
                if len(self.bets_placed) < prev_bet_count:
                    self._cooldown_remaining = MOMENTUM_REENTRY_COOLDOWN

        # --- Entry logic: buy dips on EITHER leader (now with DQS) ---
        max_entry = sport_profile.get("max_entry", 75)
        max_dip = sport_profile.get("max_dip", int(MOMENTUM_DIP_MAX * 100))
        min_dip = sport_profile.get("min_dip", int(MOMENTUM_DIP_BUY * 100))

        # Can we enter? Check caps and cooldown.
        # Sport-disable gate (Apr 20 Session 2): block new entries in sports
        # listed in MOMENTUM_DISABLED_SPORTS. Exits are unaffected — _check_exit
        # does not consult this flag, so already-open positions still close on
        # TP / SL / trailing normally.
        can_enter = (
            self._entry_count < MOMENTUM_MAX_ENTRIES
            and self._cooldown_remaining <= 0
            and not self.bets_placed  # one position at a time
            and (self.sport or "").lower() not in MOMENTUM_DISABLED_SPORTS
        )

        # Session 7 — edge proxy + context for decision log.
        # wp_edge mirrors live_ticks.jsonl enrichment (same formula as the
        # tick logger and the conviction block below).
        gc_wp = self._game_ctx.win_probability if self._game_ctx else None
        wp_edge = round(gc_wp - yes_ask / 100.0, 3) if gc_wp is not None else None
        mom_ctx = {
            "wp": round(gc_wp, 3) if gc_wp is not None else None,
            "kalshi_price": yes_ask,
            "dip_cents": dip_cents,
            "dqs": None,  # filled at sites where DQS was actually computed
        }

        if not can_enter:
            sport_lc = (self.sport or "").lower()
            if sport_lc in MOMENTUM_DISABLED_SPORTS:
                _reason = "sport_disabled"
                _gates = {"can_enter": False, "sport_enabled": False}
            elif self._entry_count >= MOMENTUM_MAX_ENTRIES:
                _reason = "max_entries"
                _gates = {"can_enter": False, "sport_enabled": True, "max_entries": False}
            elif self._cooldown_remaining > 0:
                _reason = "cooldown"
                _gates = {"can_enter": False, "sport_enabled": True, "cooldown": False}
            elif self.bets_placed:
                _reason = "position_open"
                _gates = {"can_enter": False, "sport_enabled": True, "position_open": False}
            else:
                _reason = "cannot_enter"
                _gates = {"can_enter": False}
            self._log_decision_dampened(
                decision="reject", reason=_reason, gates=_gates,
                edge=wp_edge,
                extra={**mom_ctx, "sport": sport_lc,
                       "entry_count": self._entry_count,
                       "cooldown_remaining": self._cooldown_remaining,
                       "open_bets": len(self.bets_placed)},
                close_ts=_close_ts,
            )

        buy_ticker = None
        buy_market = None
        buy_price = 0
        buy_reason = ""
        buy_dip = 0
        buy_dqs = 0.0
        dqs_breakdown = {}

        skip_dqs = sport_profile.get("skip_dqs", False)

        if can_enter:
            from bot.game_context import compute_dip_quality, SportInstincts

            # Check primary side
            if (is_leader and yes_ask <= max_entry
                    and len(self._price_history) >= 3
                    and dip_cents >= min_dip):
                if dip_cents <= max_dip:
                    if skip_dqs:
                        # Tier 2.4: variance quality gate (tennis) — reject flat
                        # dips that happen during set breaks/changeovers.
                        q_ok, q_reason = True, "ok"
                        if sport_profile.get("variance_quality_gate"):
                            q_ok, q_reason = self._variance_quality_ok(self._price_history)
                        if not q_ok:
                            self._tick_telem["dqs_fail"] += 1
                            logger.info(
                                "LiveGameWatcher VARIANCE REJECT: %s dip=%dc price=%dc (%s)",
                                self.ticker, dip_cents, yes_ask, q_reason,
                            )
                            self._log_decision_dampened(
                                decision="reject", reason="variance_quality",
                                gates={"can_enter": True, "is_leader": True,
                                       "dip_window": True, "variance_quality": False},
                                edge=wp_edge,
                                extra={**mom_ctx, "q_reason": q_reason},
                                close_ts=_close_ts,
                            )
                        else:
                            # DATA-DRIVEN: Tennis/UFC — skip DQS, pure price action
                            # Backtested: dip≥8c SL=8c TP=15c → 46% win, +2.39c/trade
                            buy_ticker = self.ticker
                            buy_market = market
                            buy_price = yes_ask
                            buy_dip = dip_cents
                            buy_dqs = 1.0  # N/A
                            dqs_breakdown = {"mode": "price_action", "skip_dqs": True, "q_reason": q_reason}
                            buy_reason = (
                                f"leader at {yes_ask}c, dipped {dip_cents}c from recent {recent_high}c "
                                f"(SCALP MODE — pure price action)"
                            )
                    else:
                        # Compute Dip Quality Score with full game intelligence + instincts
                        dqs, dqs_bd = compute_dip_quality(
                            game_ctx=self._game_ctx,
                            dip_cents=dip_cents,
                            price=yes_ask,
                            price_history=self._price_history,
                            sport=self.sport or "",
                            espn_data=espn_data or None,
                        )
                        if dqs >= MOMENTUM_DQS_THRESHOLD:
                            buy_ticker = self.ticker
                            buy_market = market
                            buy_price = yes_ask
                            buy_dip = dip_cents
                            buy_dqs = dqs
                            dqs_breakdown = dqs_bd
                            buy_reason = (
                                f"leader at {yes_ask}c, dipped {dip_cents}c from recent {recent_high}c "
                                f"(DQS={dqs:.2f} wp={dqs_bd.get('wp_edge','?')} mom={dqs_bd.get('momentum_raw','?')})"
                            )
                        else:
                            logger.info(
                                "LiveGameWatcher DQS REJECT: %s dip=%dc DQS=%.2f < %.2f "
                                "(wp=%s mom=%s trend=%s stage=%s vol=%s)",
                                self.ticker, dip_cents, dqs, MOMENTUM_DQS_THRESHOLD,
                                dqs_bd.get("win_prob"), dqs_bd.get("momentum"),
                                dqs_bd.get("lead_trend"), dqs_bd.get("stage"),
                                dqs_bd.get("volatility"),
                            )
                            self._tick_telem["dqs_fail"] += 1
                            self._log_decision_dampened(
                                decision="reject", reason="dqs_fail",
                                gates={"can_enter": True, "is_leader": True,
                                       "dip_window": True, "dqs": False},
                                edge=wp_edge,
                                extra={**mom_ctx, "dqs": round(dqs, 3),
                                       "threshold": MOMENTUM_DQS_THRESHOLD},
                                close_ts=_close_ts,
                            )
                else:
                    logger.info(
                        "LiveGameWatcher SKIP: %s dip too large (%dc > %dc max)",
                        self.ticker, dip_cents, max_dip,
                    )
                    self._tick_telem["dip_too_big"] += 1
                    self._log_decision_dampened(
                        decision="reject", reason="dip_too_big",
                        gates={"can_enter": True, "is_leader": True,
                               "dip_min": True, "dip_max": False},
                        edge=wp_edge,
                        extra={**mom_ctx, "max_dip": max_dip},
                        close_ts=_close_ts,
                    )

            # Check opponent side (if primary didn't qualify)
            if (not buy_ticker and opp_market and opp_is_leader
                    and opp_yes_ask <= max_entry
                    and len(self._opp_price_history) >= 3
                    and opp_dip_cents >= min_dip):
                if opp_dip_cents <= max_dip:
                    if skip_dqs:
                        # Tier 2.4: variance quality gate (opponent side).
                        q_ok, q_reason = True, "ok"
                        if sport_profile.get("variance_quality_gate"):
                            q_ok, q_reason = self._variance_quality_ok(self._opp_price_history)
                        if not q_ok:
                            self._tick_telem["dqs_fail"] += 1
                            logger.info(
                                "LiveGameWatcher VARIANCE REJECT (opp): %s dip=%dc price=%dc (%s)",
                                self._opponent_ticker, opp_dip_cents, opp_yes_ask, q_reason,
                            )
                        else:
                            # Tennis/UFC scalp mode — skip DQS
                            buy_ticker = self._opponent_ticker
                            buy_market = opp_market
                            buy_price = opp_yes_ask
                            buy_dip = opp_dip_cents
                            buy_dqs = 1.0
                            dqs_breakdown = {"mode": "price_action", "skip_dqs": True, "q_reason": q_reason}
                            buy_reason = (
                                f"OPP leader at {opp_yes_ask}c, dipped {opp_dip_cents}c "
                                f"from recent {opp_recent_high}c "
                                f"(SCALP MODE — pure price action)"
                            )
                    else:
                        # Build a temporary opponent GameContext (inverted scores)
                        opp_game_ctx = None
                        if self._game_ctx and self._game_ctx._snapshots:
                            from bot.game_context import GameContext as _GC
                            opp_game_ctx = _GC(sport=self.sport or "")
                            # Feed it inverted score data
                            if espn_data and period:
                                opp_game_ctx.update(
                                    espn_data=espn_data,
                                    our_score=their_score,
                                    their_score=our_score,
                                    period=period,
                                    clock_str=espn_data.get("clock", ""),
                                )

                        dqs, dqs_bd = compute_dip_quality(
                            game_ctx=opp_game_ctx,
                            dip_cents=opp_dip_cents,
                            price=opp_yes_ask,
                            price_history=self._opp_price_history,
                            sport=self.sport or "",
                            espn_data=espn_data or None,
                        )
                        if dqs >= MOMENTUM_DQS_THRESHOLD:
                            buy_ticker = self._opponent_ticker
                            buy_market = opp_market
                            buy_price = opp_yes_ask
                            buy_dip = opp_dip_cents
                            buy_dqs = dqs
                            dqs_breakdown = dqs_bd
                            buy_reason = (
                                f"OPP leader at {opp_yes_ask}c, dipped {opp_dip_cents}c "
                                f"from recent {opp_recent_high}c "
                                f"(DQS={dqs:.2f} wp={dqs_bd.get('wp_edge','?')} mom={dqs_bd.get('momentum_raw','?')})"
                            )
                        else:
                            logger.info(
                                "LiveGameWatcher DQS REJECT OPP: %s dip=%dc DQS=%.2f < %.2f",
                                self._opponent_ticker, opp_dip_cents, dqs, MOMENTUM_DQS_THRESHOLD,
                            )
                            self._tick_telem["dqs_fail"] += 1
                else:
                    logger.info(
                        "LiveGameWatcher SKIP: %s opp dip too large (%dc > %dc max)",
                        self._opponent_ticker, opp_dip_cents, max_dip,
                    )
                    self._tick_telem["dip_too_big"] += 1

        # --- Conviction Entry: "read the game, buy without a dip" ---
        # Sometimes there IS no dip. The team is dominating, the price just
        # keeps climbing. A real bettor watching would say "just buy it."
        # This triggers when:
        #   - Win probability is significantly above Kalshi price (the game
        #     says this team should be priced higher)
        #   - Momentum is positive (our team is scoring)
        #   - Lead trend is stable/growing (not a comeback)
        #   - We've watched enough ticks to actually read the game
        #   - Price is in the value zone (not too low = uncertain, not too
        #     high = no upside)
        # Only works for sports with reliable win prob models.
        if can_enter and not buy_ticker and not skip_dqs:
            from bot.config import (
                CONVICTION_ENABLED, CONVICTION_MIN_WP_EDGE,
                CONVICTION_MIN_MOMENTUM, CONVICTION_MIN_LEAD_TREND,
                CONVICTION_MIN_PRICE, CONVICTION_MAX_PRICE,
                CONVICTION_MIN_TICKS, CONVICTION_MIN_COMPLETION,
                CONVICTION_EXCLUDED_SPORTS,
            )
            from bot.game_context import SportInstincts
            sport_key = (self.sport or "").lower().split("_")[0]  # "nba", "nhl", etc.
            if (CONVICTION_ENABLED
                    and sport_key not in CONVICTION_EXCLUDED_SPORTS
                    and self._game_ctx
                    and len(self._game_ctx._snapshots) >= CONVICTION_MIN_TICKS):
                self._tick_telem["conviction_checked"] += 1
                gc = self._game_ctx
                wp = gc.win_probability
                kalshi_implied = yes_ask / 100.0
                wp_edge = wp - kalshi_implied
                completion = gc.game_completion_pct

                # Check instincts — don't conviction-buy in garbage/clutch/etc.
                conv_instincts = SportInstincts.detect(gc, espn_data, self.sport or "")
                if conv_instincts.should_avoid_entry:
                    self._tick_telem["instinct_avoid"] += 1

                conviction_ok = (
                    wp_edge >= CONVICTION_MIN_WP_EDGE
                    and gc.momentum >= CONVICTION_MIN_MOMENTUM
                    and gc.lead_trend >= CONVICTION_MIN_LEAD_TREND
                    and CONVICTION_MIN_PRICE <= yes_ask <= CONVICTION_MAX_PRICE
                    and completion >= CONVICTION_MIN_COMPLETION
                    and gc.score_diff > 0  # must actually be winning
                    and not conv_instincts.should_avoid_entry
                    and is_leader
                )

                if conviction_ok:
                    self._tick_telem["conviction_eligible"] += 1
                    buy_ticker = self.ticker
                    buy_market = market
                    buy_price = yes_ask
                    buy_dip = 0  # no dip — conviction entry
                    buy_dqs = 0.9  # high confidence from game reading
                    dqs_breakdown = {
                        "mode": "conviction",
                        "wp": round(wp, 3),
                        "wp_edge": round(wp_edge, 3),
                        "momentum": round(gc.momentum, 2),
                        "lead_trend": round(gc.lead_trend, 2),
                        "completion": round(completion, 2),
                        "score_diff": gc.score_diff,
                        "instincts": conv_instincts.flags,
                    }
                    buy_reason = (
                        f"CONVICTION: wp={wp:.0%} vs price={yes_ask}c "
                        f"(+{wp_edge:.0%} edge) mom={gc.momentum:+.1f} "
                        f"trend={gc.lead_trend:+.1f} {completion:.0%} done"
                    )
                    logger.info(
                        "LiveGameWatcher CONVICTION ENTRY: %s wp=%.1f%% price=%dc "
                        "edge=%.1f%% mom=%.2f trend=%.2f completion=%.0f%% diff=%d",
                        self.ticker, wp * 100, yes_ask, wp_edge * 100,
                        gc.momentum, gc.lead_trend, completion * 100, gc.score_diff,
                    )
                elif wp_edge >= 0.05 and gc.momentum > 0:
                    # Close to conviction but not quite — log for tuning
                    self._tick_telem["conviction_near_miss"] += 1
                    logger.debug(
                        "LiveGameWatcher CONVICTION NEAR-MISS: %s wp=%.0f%% price=%dc "
                        "edge=%.1f%% mom=%.2f trend=%.2f comp=%.0f%%",
                        self.ticker, wp * 100, yes_ask, wp_edge * 100,
                        gc.momentum, gc.lead_trend, completion * 100,
                    )

                # Also check opponent side for conviction
                if not buy_ticker and opp_yes_ask > 0 and opp_is_leader:
                    # Build opponent GameContext (inverted)
                    opp_wp = 1.0 - wp if wp > 0 else 0.5
                    opp_kalshi = opp_yes_ask / 100.0
                    opp_wp_edge = opp_wp - opp_kalshi
                    opp_momentum = -gc.momentum  # inverted
                    opp_lead_trend = -gc.lead_trend
                    opp_score_diff = -gc.score_diff

                    opp_conviction_ok = (
                        opp_wp_edge >= CONVICTION_MIN_WP_EDGE
                        and opp_momentum >= CONVICTION_MIN_MOMENTUM
                        and opp_lead_trend >= CONVICTION_MIN_LEAD_TREND
                        and CONVICTION_MIN_PRICE <= opp_yes_ask <= CONVICTION_MAX_PRICE
                        and completion >= CONVICTION_MIN_COMPLETION
                        and opp_score_diff > 0
                        and not conv_instincts.should_avoid_entry
                        and opp_is_leader
                    )

                    if opp_conviction_ok:
                        buy_ticker = self._opponent_ticker
                        buy_market = opp_market
                        buy_price = opp_yes_ask
                        buy_dip = 0
                        buy_dqs = 0.9
                        dqs_breakdown = {
                            "mode": "conviction",
                            "wp": round(opp_wp, 3),
                            "wp_edge": round(opp_wp_edge, 3),
                            "momentum": round(opp_momentum, 2),
                            "side": "opponent",
                        }
                        buy_reason = (
                            f"CONVICTION OPP: wp={opp_wp:.0%} vs price={opp_yes_ask}c "
                            f"(+{opp_wp_edge:.0%} edge) mom={opp_momentum:+.1f}"
                        )

        if buy_ticker and buy_market:
            self._tick_telem["execute_attempt"] += 1  # Tier 1.3
            reentry_tag = f" [RE-ENTRY #{self._entry_count + 1}]" if self._entry_count > 0 else ""
            # Conviction entries use reduced sizing
            conviction_mode = buy_dip == 0 and "conviction" in buy_reason.lower()
            # Session 6 — accept-path log (dampened). Note: executor will also
            # log its own accept after position-limit + edge-recheck gates.
            self._log_decision_dampened(
                decision="accept",
                reason="conviction" if conviction_mode else "dip_buy",
                gates={"can_enter": True, "is_leader": True,
                       "dip_window": True, "dqs": True},
                edge=wp_edge,
                extra={**mom_ctx,
                       "dqs": round(buy_dqs, 3) if buy_dqs else None,
                       "buy_price": buy_price, "buy_dip": buy_dip,
                       "ticker": buy_ticker},
                close_ts=_close_ts,
            )
            await self._auto_bet_momentum(
                buy_market, buy_price,
                buy_reason + reentry_tag,
                ticker_override=buy_ticker,
                dip_cents=buy_dip if not conviction_mode else 4,  # use min dip for sizing
                dqs_score=buy_dqs,
                conviction=conviction_mode,
            )

        # Build score string for logging
        score_str = ""
        ctx_str = ""
        if espn_data:
            h = espn_data.get("home_score", "?")
            a = espn_data.get("away_score", "?")
            pl = espn_data.get("period_label", "?")
            clk = espn_data.get("clock", "")
            score_str = f" [{a}-{h} {pl} {clk}]"
        if self._game_ctx and self._game_ctx._snapshots:
            g = self._game_ctx
            wp = g.win_probability
            mom = g.momentum
            ctx_str = f" wp={wp:.0%} mom={mom:+.1f}"
            if g.opponent_on_run:
                ctx_str += " OPP_RUN!"
            elif g.our_team_on_run:
                ctx_str += " OUR_RUN!"

        logger.info(
            "LiveGameWatcher momentum tick: %s price=%dc leader=%s dip=%dc "
            "opp=%dc opp_leader=%s opp_dip=%dc entries=%d/%d cooldown=%d holding=%d%s%s",
            self.ticker, yes_ask, is_leader, dip_cents,
            opp_yes_ask, opp_is_leader, opp_dip_cents,
            self._entry_count, MOMENTUM_MAX_ENTRIES,
            self._cooldown_remaining, len(self.bets_placed),
            score_str, ctx_str,
        )

        # Compute instincts for logging (reuse if already computed this tick)
        from bot.game_context import SportInstincts
        tick_instincts = SportInstincts.detect(self._game_ctx, espn_data, self.sport or "")

        # Log tick data for post-analysis — capture EVERYTHING needed to evaluate strategies
        gc = self._game_ctx
        _log_tick({
            # Identity
            "ticker": self.ticker,
            "match": self._match_title or self.query,
            "sport": self.sport,
            # Prices (both sides)
            "price": yes_ask,
            "bid": market.get("yes_bid", 0) if market else 0,
            "opp_price": opp_yes_ask,
            "opp_bid": opp_market.get("yes_bid", 0) if opp_market else 0,
            # Dip detection
            "leader": is_leader,
            "opp_leader": opp_is_leader,
            "dip": dip_cents,
            "opp_dip": opp_dip_cents,
            "recent_high": recent_high,
            "opp_recent_high": opp_recent_high,
            # Position state
            "holding": len(self.bets_placed),
            "entry_count": self._entry_count,
            "elapsed": elapsed,
            # Game state (from ESPN)
            "score_diff": score_diff,
            "period": period,
            "completion": round(gc.game_completion_pct, 3) if gc else None,
            # Model outputs — the "brain" readings
            "wp": round(gc.win_probability, 3) if gc else None,
            "momentum": round(gc.momentum, 3) if gc else None,
            "lead_trend": round(gc.lead_trend, 3) if gc else None,
            "wp_edge": round(gc.win_probability - yes_ask / 100.0, 3) if gc else None,
            # Instincts
            "instincts": tick_instincts.flags if tick_instincts.flags else None,
            "instinct_mod": tick_instincts.situational_modifier if tick_instincts.flags else None,
            "avoid_entry": tick_instincts.should_avoid_entry,
            "volatility": tick_instincts.volatility_regime,
            # Conviction check
            "conviction_eligible": bool(
                gc and len(gc._snapshots) >= 12
                and gc.win_probability > (yes_ask / 100.0 + 0.05)
                and gc.momentum > 0
            ) if gc else None,
            # ESPN raw (compact — just scores and situation, not full blob)
            "espn_scores": {
                "home": espn_data.get("home_score"),
                "away": espn_data.get("away_score"),
                "detail": espn_data.get("detail", ""),
            } if espn_data else None,
        })

        # --- Update Telegram card ---
        card = self._format_momentum_card(
            yes_ask=yes_ask,
            recent_high=recent_high,
            dip_cents=dip_cents,
            is_leader=is_leader,
            elapsed=elapsed,
            opp_price=opp_yes_ask,
            opp_leader=opp_is_leader,
            opp_dip=opp_dip_cents,
            espn_data=espn_data,
            score_diff=score_diff,
        )
        if self.status_msg_id:
            await self.notifier.edit_message_by_id(self.status_msg_id, card)

    async def _auto_bet_momentum(
        self, market: dict, yes_ask: int, reason: str,
        ticker_override: str | None = None, dip_cents: float = 4,
        dqs_score: float = 0.0, conviction: bool = False,
    ):
        """Place a momentum buy — YES on the leader. Supports both sides via ticker_override."""
        from bot.executor import execute_trade
        from bot.sizing import kelly_size

        side = "yes"
        price_cents = yes_ask
        # Fair probability: use GameContext win prob if available (much smarter),
        # fall back to price + dip assumption
        if self._game_ctx and self._game_ctx._snapshots and self._game_ctx.win_probability > 0:
            # Use empirical win probability as fair value
            fair_prob = min(0.95, self._game_ctx.win_probability)
            logger.info(
                "Momentum sizing: using win_prob=%.3f as fair_prob (kalshi=%dc, dip=%dc)",
                fair_prob, yes_ask, dip_cents,
            )
        else:
            # Fallback: fair prob = market price + dip (assume dip is temporary)
            fair_prob = min(0.95, (yes_ask + dip_cents) / 100.0)
        use_ticker = ticker_override or self.ticker

        # Compute paper balance if needed
        balance = self.balance
        if PAPER_MODE:
            import json, pathlib
            pt_file = pathlib.Path("bot/state/paper_trades.json")
            if pt_file.exists():
                trades = json.loads(pt_file.read_text())
                paper_pnl = sum(t.get("pnl") or 0 for t in trades)
                balance = 500.0 + paper_pnl

        # Dip-scaled sizing: small dips = high confidence = bigger bet
        size_mult = self._dip_size_multiplier(dip_cents)

        # Edge = dip in dollar terms. Fair prob = price + dip.
        # e.g. buying at 60c with 4c dip → fair = 64%, edge = 0.04
        assumed_edge = dip_cents / 100.0
        confidence = 0.80
        sizing = kelly_size(
            edge=assumed_edge,
            probability=fair_prob,
            balance=balance,
            price_cents=price_cents,
            confidence=confidence,
        )
        if sizing["contracts"] <= 0:
            logger.debug("Momentum sizing returned 0 contracts")
            return

        # Apply dip multiplier to contract count
        import math
        scaled_contracts = max(1, math.floor(sizing["contracts"] * size_mult))

        # CONVICTION: Reduce size — we're less confident without a dip signal
        if conviction:
            from bot.config import CONVICTION_SIZE_FACTOR
            scaled_contracts = max(1, int(scaled_contracts * CONVICTION_SIZE_FACTOR))
            logger.info("Conviction sizing: %dx (%.0f%% of normal)", scaled_contracts, CONVICTION_SIZE_FACTOR * 100)

        # INSTINCT: Reduce size in high-volatility situations (clutch, empty net)
        from bot.game_context import SportInstincts
        bet_instincts = SportInstincts.detect(self._game_ctx, self._last_espn_data, self.sport or "")
        if bet_instincts.should_reduce_size:
            scaled_contracts = max(1, scaled_contracts // 2)
            logger.info("Instincts halved position: %s (%s)", use_ticker, bet_instincts.flags)

        # Cap contracts per sport profile (UFC/tennis = smaller positions)
        sport_profile = self._get_sport_profile()
        max_contracts = sport_profile.get("max_contracts")
        if max_contracts and scaled_contracts > max_contracts:
            logger.info("Momentum: capping contracts %d → %d (sport max)", scaled_contracts, max_contracts)
            scaled_contracts = max_contracts
        sizing["contracts"] = scaled_contracts
        sizing["total_cost"] = round(scaled_contracts * price_cents / 100.0, 2)
        sizing["max_payout"] = round(scaled_contracts * 1.0, 2)

        gc = self._game_ctx
        opp = {
            "type": "live_momentum",
            "ticker": use_ticker,
            "title": self._match_title or self.query,
            "action": "buy",
            "side": side,
            "recommended_side": side,
            "price_cents": price_cents,
            "strategy": "live_momentum",
            "sport": self.sport or "",
            "reason": f"MOMENTUM: {reason}",
            "entry_mode": "conviction" if conviction else "dip",
            "market": market,
            "edge_result": {
                "kalshi_price": yes_ask / 100.0,
                "fair_value": fair_prob,
                "edge": assumed_edge,
                "wp_edge": round(gc.win_probability - yes_ask / 100.0, 3) if gc else 0,
                "self_check_passed": True,
                "math_chain": [f"Momentum: leader at {yes_ask}c, dip buy, scale={size_mult:.1f}x"],
                "warnings": [],
            },
        }

        try:
            result = execute_trade(opp, sizing)
        except Exception as e:
            # Tier 1.4: surface as a failure reason for tuning diagnostics.
            self._tick_telem["execute_failed"]["EXCEPTION"] = (
                self._tick_telem["execute_failed"].get("EXCEPTION", 0) + 1
            )
            logger.error(
                "LiveGameWatcher execute_trade EXCEPTION for %s: %s",
                use_ticker, e, exc_info=True,
            )
            return
        if not result["success"]:
            # Tier 1.4: count and surface the exact rejection reason so we can tell
            # whether conviction is dying on EXPOSURE_CAP, POSITION_LIMIT, cooldown,
            # duplicate-entry guard, or something new.
            reason_key = str(result.get("reason") or "unknown").upper().replace(" ", "_")[:48]
            self._tick_telem["execute_failed"][reason_key] = (
                self._tick_telem["execute_failed"].get(reason_key, 0) + 1
            )
            logger.warning(
                "LiveGameWatcher MOMENTUM BET BLOCKED: %s — reason=%s mode=%s price=%dc",
                use_ticker, reason_key,
                "conviction" if conviction else "dip",
                price_cents,
            )
            return

        # Tier 1.3: trade executed (paper or live) — count success.
        self._tick_telem["execute_success"] += 1
        order = result["order_result"]
        gc = self._game_ctx
        entry_record = {
            "ticker": use_ticker,
            "side": side,
            "entered_at": time.time(),
            "contracts": scaled_contracts,
            "price_cents": price_cents,
            "order_id": order.get("order_id", "PAPER"),
            "filled": order.get("filled_count", 0),
            # Full entry context — everything needed to evaluate WHY we entered
            "entry_reason": reason,
            "entry_mode": "conviction" if conviction else "dip",
            "sport": self.sport or "",
            "dip_cents": dip_cents,
            "dqs_score": dqs_score,
            "game_state": {
                "score_diff": gc.score_diff if gc else None,
                "period": gc._snapshots[-1].get("period") if gc and gc._snapshots else None,
                "completion": round(gc.game_completion_pct, 3) if gc else None,
                "wp": round(gc.win_probability, 3) if gc else None,
                "momentum": round(gc.momentum, 3) if gc else None,
                "lead_trend": round(gc.lead_trend, 3) if gc else None,
                "wp_edge": round(gc.win_probability - price_cents / 100.0, 3) if gc else None,
            } if gc else None,
            "instincts": bet_instincts.flags if bet_instincts.flags else [],
            "size_multiplier": size_mult,
        }
        self.bets_placed.append(entry_record)
        self._entry_count += 1
        logger.info(
            "LiveGameWatcher MOMENTUM BET: %s %s %dx @ %sc (%.1fx scale) (%s) [entry #%d] filled=%d",
            use_ticker, side.upper(), scaled_contracts, price_cents, size_mult, reason,
            self._entry_count, order.get("filled_count", 0),
        )
        _journal_append({
            "event": "bet",
            "match": self._match_title or self.query,
            "ticker": use_ticker,
            "side": side,
            "contracts": scaled_contracts,
            "price_cents": price_cents,
            "reason": reason,
            "mode": "conviction" if conviction else "momentum",
            "entry_number": self._entry_count,
            "size_multiplier": size_mult,
            "sport": self.sport or "",
            "filled": order.get("filled_count", 0),
            "game_state": entry_record.get("game_state"),
            "instincts": entry_record.get("instincts"),
        })

    def _format_momentum_card(
        self, *, yes_ask: int, recent_high: int, dip_cents: int,
        is_leader: bool, elapsed: int,
        opp_price: int = 0, opp_leader: bool = False, opp_dip: int = 0,
        espn_data: dict | None = None, score_diff: int | None = None,
    ) -> str:
        lines = ["LIVE WATCH (MOMENTUM)"]
        lines.append("")
        lines.append(self._match_title[:60] if self._match_title else self.query.upper())

        # Live score from ESPN
        if espn_data:
            h = espn_data.get("home_score", "?")
            a = espn_data.get("away_score", "?")
            pl = espn_data.get("period_label", "?")
            clk = espn_data.get("clock", "")
            detail = espn_data.get("detail", "")
            lines.append(f"Score: {a}-{h} | {detail or f'{pl} {clk}'}")

            # Game intelligence summary
            if self._game_ctx and self._game_ctx._snapshots:
                g = self._game_ctx
                wp = g.win_probability
                mom = g.momentum
                mom_arrow = ">>>" if mom > 0.3 else ">>" if mom > 0 else "=" if mom > -0.3 else "<<"
                wp_str = f"WinProb: {wp:.0%}"
                mom_str = f"Momentum: {mom_arrow}"
                if g.opponent_on_run:
                    mom_str += " OPP RUN"
                elif g.our_team_on_run:
                    mom_str += " OUR RUN"
                lines.append(f"{wp_str} | {mom_str}")

                # Sport instincts — show active situational flags
                from bot.game_context import SportInstincts
                instincts = SportInstincts.detect(g, espn_data, self.sport or "")
                if instincts.flags:
                    flag_str = " | ".join(f.split("(")[0].strip() for f in instincts.flags[:3])
                    lines.append(f"INSTINCT: {flag_str}")

                # Conviction readout — show when game reading is active
                kalshi_implied = yes_ask / 100.0
                wp_edge_pct = wp - kalshi_implied
                if wp_edge_pct >= 0.05 and not instincts.should_avoid_entry:
                    edge_label = f"+{wp_edge_pct:.0%} edge"
                    if wp_edge_pct >= 0.08 and mom > 0.15:
                        lines.append(f"READING: {edge_label} | DOMINATING")
                    elif wp_edge_pct >= 0.08:
                        lines.append(f"READING: {edge_label} | STRONG")
                    else:
                        lines.append(f"READING: {edge_label}")

            # MLB situation
            sit = espn_data.get("situation", {})
            if sit and self.sport == "mlb":
                outs = sit.get("outs", "?")
                bases = []
                if sit.get("onFirst"): bases.append("1B")
                if sit.get("onSecond"): bases.append("2B")
                if sit.get("onThird"): bases.append("3B")
                base_str = ",".join(bases) if bases else "empty"
                lines.append(f"  {outs} out | Bases: {base_str}")
        lines.append("")

        # Both sides display
        leader_tag = " LEADER" if is_leader else ""
        lines.append(f"Player A: {yes_ask}c{leader_tag} | high {recent_high}c | dip {dip_cents}c")
        if opp_price:
            opp_tag = " LEADER" if opp_leader else ""
            lines.append(f"Player B: {opp_price}c{opp_tag} | dip {opp_dip}c")
        lines.append("")

        # Position info
        if self.bets_placed:
            for b in self.bets_placed:
                entry_c = b.get("price_cents", 0)
                bet_ticker = b.get("ticker", "")
                # Use correct current price depending on which side we hold
                if bet_ticker == self._opponent_ticker and opp_price:
                    cur_val = opp_price
                else:
                    cur_val = yes_ask
                gain_cents = cur_val - entry_c
                gain_str = f"+{gain_cents}c" if gain_cents >= 0 else f"{gain_cents}c"
                side_label = "A" if bet_ticker == self.ticker else "B"
                peak = self._peak_values.get(bet_ticker, cur_val)
                lines.append(
                    f"HOLDING [{side_label}]: YES {b['contracts']}x @ {entry_c}c "
                    f"-> {cur_val}c ({gain_str}) peak={peak}c"
                )

        # Session P&L from exits
        if self.exits:
            session_pnl = sum(ex.get("pnl", 0) for ex in self.exits)
            wins = sum(1 for ex in self.exits if (ex.get("pnl") or 0) > 0)
            losses = len(self.exits) - wins
            pnl_str = f"+${session_pnl:.2f}" if session_pnl >= 0 else f"-${abs(session_pnl):.2f}"
            lines.append(f"Session: {pnl_str} ({wins}W/{losses}L)")
            # Show last exit
            last = self.exits[-1]
            last_pnl = last.get("pnl", 0)
            last_str = f"+${last_pnl:.2f}" if last_pnl >= 0 else f"-${abs(last_pnl):.2f}"
            lines.append(f"  Last: {last['reason'][:40]} ({last_str})")

        if not self.bets_placed and not self.exits:
            lines.append("Watching for entry...")
        elif not self.bets_placed and self._cooldown_remaining > 0:
            lines.append(f"Cooldown: {self._cooldown_remaining} ticks until re-entry")
        elif not self.bets_placed and self._entry_count < MOMENTUM_MAX_ENTRIES:
            lines.append("Scanning for re-entry...")

        lines.append("")
        sport_tag = f" [{self.sport.upper()}]" if self.sport else ""
        lines.append(f"Entries: {self._entry_count}/{MOMENTUM_MAX_ENTRIES} | {elapsed}s{sport_tag} | UNWATCH to stop")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Arb tick (original)
    # ------------------------------------------------------------------

    async def _tick(self, game: dict):
        """One poll cycle: fetch odds -> fetch Kalshi price -> edge -> update Telegram."""
        loop = asyncio.get_event_loop()

        # Refresh odds — bypass cache for 10s polling
        odds = await loop.run_in_executor(
            None, lambda: _odds.fetch_consensus_odds(self.sport, bypass_cache=True)
        )

        # Find this specific game in the refreshed data
        current_game = self._find_game_in_data(odds)
        if not current_game:
            # Game dropped from odds feed — likely finished
            self._gone_ticks = getattr(self, "_gone_ticks", 0) + 1
            if self._gone_ticks >= 3:
                logger.info("LiveGameWatcher: game gone from feed for %d ticks — stopping", self._gone_ticks)
                self.active = False
            else:
                logger.debug("LiveGameWatcher: game not found in odds data this tick (%d)", self._gone_ticks)
            return
        self._gone_ticks = 0

        # Check if game ended
        status = current_game.get("status", "")
        if status in _FINAL_STATUSES:
            logger.info("LiveGameWatcher: game status=%s — stopping", status)
            self.active = False
            return

        # Get ESPN live score/clock
        score_data = await loop.run_in_executor(
            None, self._fetch_espn_score
        )

        # Get Kalshi market price
        market = await loop.run_in_executor(None, self._fetch_kalshi_market)
        if not market:
            return

        yes_ask = market.get("yes_ask", 0)
        if not yes_ask:
            return

        # Kalshi settled (97+¢ or ≤3¢) — market is effectively over
        if yes_ask >= 97 or yes_ask <= 3:
            logger.info("LiveGameWatcher: Kalshi settled at %dc — stopping", yes_ask)
            self.active = False
            return

        # Compute edge
        consensus = current_game.get("consensus", {})
        home = current_game.get("home_team", "")
        away = current_game.get("away_team", "")

        # Determine which side the ticker represents (YES = ticker team wins)
        ticker_team = self.ticker.split("-")[-1] if self.ticker else ""
        espn_prob = 0.0
        for team_name, prob in consensus.items():
            if ticker_team.lower() in team_name.lower() or team_name.lower().startswith(ticker_team.lower()):
                espn_prob = prob
                break
        if not espn_prob:
            # Fallback: use home team prob
            espn_prob = consensus.get(home, 0.5)

        edge, relative_edge = self._compute_edge(espn_prob, yes_ask, "yes")

        elapsed = int(time.time() - self._started_at)

        # --- Position management: exit if edge reverses against us ---
        if self.bets_placed:
            await self._check_exit(market, edge, relative_edge)

        # Auto-bet if edge exceeds threshold and we haven't bet this direction yet
        bet_side = "yes" if edge > 0 else "no"
        already = self._already_bet(self.ticker, bet_side)
        logger.info(
            "LiveGameWatcher tick: %s espn=%.0f%% kalshi=%dc edge=%.1f%% rel=%.1f%% side=%s already=%s",
            self.ticker, espn_prob * 100, yes_ask, edge * 100, relative_edge * 100, bet_side, already,
        )
        if abs(relative_edge) >= LIVE_WATCH_EDGE_THRESHOLD and not already:
            await self._auto_bet(market, espn_prob, edge, relative_edge, current_game)

        # Tier 2.2: arb wp_delta diagnostic.
        # Arb mode has 0 resolved trades — we don't know if 10% LIVE_WATCH_EDGE_THRESHOLD
        # is ever actually hit. Log every tick's wp_delta so we can compute the
        # distribution from live_ticks.jsonl after 48h and decide to keep/tune/retire.
        _log_tick({
            "mode": "arb",
            "ticker": self.ticker,
            "match": f"{away} @ {home}",
            "sport": self.sport,
            "price": yes_ask,
            "bid": market.get("yes_bid", 0) if market else 0,
            "espn_prob": round(espn_prob, 4),
            "wp_delta": round(espn_prob - yes_ask / 100.0, 4),  # raw: espn - kalshi
            "edge": round(edge, 4),
            "relative_edge": round(relative_edge, 4),
            "edge_threshold": LIVE_WATCH_EDGE_THRESHOLD,
            "gate_hit": abs(relative_edge) >= LIVE_WATCH_EDGE_THRESHOLD,
            "already_bet": already,
            "bet_side": bet_side,
            "holding": len(self.bets_placed),
            "elapsed": elapsed,
        })

        # Build score display from ESPN data
        home_score = score_data.get("home_score", "?")
        away_score = score_data.get("away_score", "?")
        period_label = score_data.get("period_label", "")
        clock = score_data.get("clock", "")

        # Update Telegram card
        card = self._format_status_card(
            home_team=home, away_team=away,
            home_score=home_score, away_score=away_score,
            period_label=period_label, clock=clock,
            espn_prob=espn_prob, kalshi_ask_cents=yes_ask,
            edge=edge, relative_edge=relative_edge,
            last_update_secs=elapsed,
        )
        if self.status_msg_id:
            await self.notifier.edit_message_by_id(self.status_msg_id, card)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_game(self) -> dict | None:
        """Search all active sports for a live game matching self.query."""
        sports = [self.sport] if self.sport else ACTIVE_SPORTS
        for sport in sports:
            odds = _odds.fetch_consensus_odds(sport)
            game = self._find_game_in_data(odds)
            if game:
                if not self.sport:
                    self.sport = sport
                return game
        return None

    def _find_game_in_data(self, odds: dict) -> dict | None:
        """Find a game in an odds data dict that matches self.query."""
        q = self.query
        for game in odds.get("games", []):
            home = _normalize(game.get("home_team", ""))
            away = _normalize(game.get("away_team", ""))
            if len(q) >= 3 and (q in home or q in away):
                return game
        return None

    def _detect_sport(self) -> str:
        """Try each active sport and return the first one with a matching live game."""
        for sport in ACTIVE_SPORTS:
            odds = _odds.fetch_consensus_odds(sport)
            if self._find_game_in_data(odds):
                return sport
        return "nba"  # default fallback

    async def _find_kalshi_ticker(self, home: str, away: str) -> str | None:
        """Search Kalshi for an open moneyline market matching this game."""
        from agent.kalshi_client import get_markets
        from bot.kalshi_series import SPORTS_SERIES

        loop = asyncio.get_event_loop()
        home_low = _normalize(home)
        away_low = _normalize(away)

        # Strategy 1: Search by series ticker (KXNBAGAME, KXMLBGAME, etc.)
        # This is the most reliable — returns single-game moneyline markets directly.
        series_ticker = SPORTS_SERIES.get(self.sport, "")
        if series_ticker:
            result = await loop.run_in_executor(
                None, lambda: get_markets(series_ticker=series_ticker, status="open", limit=200)
            )
            for m in result.get("markets", []):
                title_low = _normalize(m.get("title", ""))
                # Match if title contains both team city names
                if home_low.split()[0] in title_low and away_low.split()[0] in title_low:
                    return m["ticker"]
                # Also match on team abbreviation in ticker (e.g. KXNBAGAME-26APR08PORSAS-POR)
                ticker = m.get("ticker", "")
                for team in (home_low, away_low):
                    # Check if any word from the team name appears in the title
                    for word in team.split():
                        if len(word) >= 4 and word in title_low:
                            return ticker

        # Strategy 2: Keyword search fallback
        queries = [home.split()[0], away.split()[0]]
        seen: dict[str, dict] = {}
        for q in queries:
            result = await loop.run_in_executor(
                None, lambda q=q: get_markets(query=q, status="open", limit=100)
            )
            for m in result.get("markets", []):
                seen.setdefault(m["ticker"], m)

        for ticker, market in seen.items():
            title_low = _normalize(market.get("title", ""))
            if (home_low.split()[0] in title_low or away_low.split()[0] in title_low):
                if "," not in title_low:  # skip parlays
                    return ticker
        return None

    def _fetch_kalshi_market(self) -> dict | None:
        """Fetch current price for self.ticker from Kalshi.
        Returns None only if the market truly doesn't exist.
        Returns {"_rate_limited": True} on 429 so callers can distinguish."""
        if not self.ticker:
            return None
        from agent.kalshi_client import get_market
        result = get_market(self.ticker)
        if "error" in result:
            err = str(result.get("error", ""))
            if "429" in err or "rate" in err.lower():
                logger.debug("Rate limited fetching %s — will retry next tick", self.ticker)
                return {"_rate_limited": True}
            return None
        return result.get("market", result)

    def _fetch_espn_score(self) -> dict:
        """
        Fetch current score/clock/period for the watched game from ESPN.
        Returns rich game context including linescores, situation, and last play.
        """
        if not self.sport:
            return {}
        sport_path = ESPN_SPORT_PATHS.get(self.sport, "")
        if not sport_path:
            if not getattr(self, "_espn_unsupported_logged", False):
                logger.warning(
                    "ESPN not configured for sport=%r (ticker=%s); wp will stay at default 0.5",
                    self.sport, self.ticker,
                )
                self._espn_unsupported_logged = True
            return {}
        url = f"{ESPN_BASE}/{sport_path}/scoreboard"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GlintBot/1.0"})
            with urllib.request.urlopen(req, context=_ESPN_SSL_CTX, timeout=8) as resp:
                data = json.loads(resp.read().decode())

            _PERIOD_LABELS = {
                "nba": {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"},
                "nhl": {1: "P1", 2: "P2", 3: "P3"},
                "ncaab": {1: "H1", 2: "H2"},
            }
            q = self.query
            for event in data.get("events", []):
                comps = event.get("competitions", [{}])
                comp = comps[0]
                home_score = away_score = 0
                home_name = away_name = ""
                home_linescores = []
                away_linescores = []
                for c in comp.get("competitors", []):
                    name = _normalize(c.get("team", {}).get("displayName", ""))
                    try:
                        sc = int(c.get("score", 0))
                    except (ValueError, TypeError):
                        sc = 0
                    ls = c.get("linescores", [])
                    if c.get("homeAway") == "home":
                        home_name, home_score = name, sc
                        home_linescores = ls
                    else:
                        away_name, away_score = name, sc
                        away_linescores = ls
                if q and (q in home_name or q in away_name):
                    if not getattr(self, "_espn_success_logged", False):
                        logger.info(
                            "ESPN fetch OK (ticker=%s sport=%s): %s @ %s",
                            self.ticker, self.sport, away_name, home_name,
                        )
                        self._espn_success_logged = True
                    sb = comp.get("status", {})
                    period = sb.get("period", 0)
                    labels = _PERIOD_LABELS.get(self.sport, {})
                    if labels:
                        pl = labels.get(period, "OT" if period > len(labels) else f"P{period}")
                    else:
                        pl = f"Inn {period}"

                    # Rich situation data (MLB: outs/runners, NBA: possession)
                    situation = comp.get("situation", {})
                    last_play = ""
                    if situation:
                        lp = situation.get("lastPlay", {})
                        last_play = lp.get("text", "") if lp else ""

                    clock_str = sb.get("displayClock", "")
                    detail = sb.get("type", {}).get("detail", "")

                    return {
                        "home_score": home_score,
                        "away_score": away_score,
                        "home_name": home_name,
                        "away_name": away_name,
                        "period_label": pl,
                        "period": period,
                        "clock": clock_str,
                        "detail": detail,
                        "home_linescores": home_linescores,
                        "away_linescores": away_linescores,
                        "situation": situation,
                        "last_play": last_play,
                    }
            # Loop finished with no match — the query doesn't hit any competitor.
            # Log once per watcher so we can see when team-name/query drift breaks matching.
            if not getattr(self, "_espn_nomatch_logged", False):
                sample = []
                for ev in data.get("events", [])[:5]:
                    comp0 = (ev.get("competitions", [{}]) or [{}])[0]
                    for c in comp0.get("competitors", []):
                        sample.append(_normalize(c.get("team", {}).get("displayName", "")))
                logger.warning(
                    "ESPN scoreboard had %d events, none matched query=%r (ticker=%s); sample names=%s",
                    len(data.get("events", [])), q, self.ticker, sample[:8],
                )
                self._espn_nomatch_logged = True
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            logger.warning("ESPN fetch network error (ticker=%s): %s", self.ticker, e)
        except json.JSONDecodeError as e:
            logger.warning("ESPN fetch JSON parse error (ticker=%s): %s", self.ticker, e)
        except Exception:
            logger.exception("Unexpected ESPN fetch error (ticker=%s)", self.ticker)
        return {}

    def _compute_edge(
        self, espn_prob: float, kalshi_ask_cents: int, side: str
    ) -> tuple[float, float]:
        """Return (edge, relative_edge) for the given side."""
        kalshi_price = kalshi_ask_cents / 100.0
        if side == "no":
            espn_prob = 1.0 - espn_prob
        edge = round(espn_prob - kalshi_price, 4)
        relative_edge = round(edge / kalshi_price, 4) if kalshi_price > 0 else 0.0
        return edge, relative_edge

    async def _check_exit(self, market: dict, edge: float, relative_edge: float,
                          opp_market: dict | None = None,
                          score_diff: int | None = None, period: int | None = None):
        """
        Smart exit logic — take profits fast, cut losses faster.

        Priority order (momentum mode):
        1. TAKE PROFIT — gain ≥ sport_tp (default 12¢) → sell immediately
        2. NEAR-SETTLEMENT — yes-side AND price ≥ 93¢ → match nearly won, lock in
        2b. TRAILING STOP — drop_from_peak ≥ sport_trail (default 6¢) AND gain > 0
        2c. SCORE FLIP — momentum + lead_trend negative AND we're trailing
        3. UNDERWATER EXIT — REMOVED Apr 16 (data-killed, see config note)
        4. HARD STOP-LOSS — drop ≥ sport_sl (default 30¢, tennis 8¢) safety net
        4b. DOLLAR STOP — unrealized loss exceeds MOMENTUM_MAX_LOSS_DOLLARS

        Priority order (arb mode additions):
        5. EDGE REVERSAL — edge flipped against us
        6. EDGE FADING — edge below 30% of threshold
        """
        from bot.executor import exit_position

        for bet in list(self.bets_placed):
            held_side = bet.get("side")
            ticker = bet.get("ticker")
            entry_price = bet.get("price_cents", 0)  # price per contract in cents
            if not ticker or not entry_price:
                continue

            should_exit = False
            reason = ""

            # --- Current value of the position ---
            # Use the correct market for this bet (opponent ticker needs opponent market)
            bet_market = market
            if ticker == self._opponent_ticker and opp_market and not opp_market.get("_rate_limited"):
                bet_market = opp_market

            if held_side == "yes":
                # Use yes_bid (what we'd get selling). Don't use `or` — yes_bid=0 is valid.
                yes_bid = bet_market.get("yes_bid")
                current_value = yes_bid if yes_bid is not None else (bet_market.get("yes_ask") or 0)
            else:
                current_value = 100 - (bet_market.get("yes_ask") or 100)

            gain_cents = current_value - entry_price
            gain_pct = gain_cents / entry_price if entry_price > 0 else 0

            # Track high-water mark.
            # Fix Apr 26 (Session 19a-peakfix): default was current_value, which made
            # `current_value > prev_peak` always False on first observation; peak_values
            # was never written, and the TRAILING_STOP read at line 2258 also defaulted
            # to current_value, so drop_from_peak was always 0. setdefault both reads
            # AND writes on first observation; the line 2258 .get() default becomes a
            # no-op once the key exists. See commit 1e5daec for back-tester
            # quantification (+558¢ over 20 trades on the wide-window sweep).
            prev_peak = self._peak_values.setdefault(ticker, entry_price)
            if current_value > prev_peak:
                self._peak_values[ticker] = current_value
                prev_peak = current_value

            # --- 1. TAKE PROFIT ---
            # Sport-specific: tennis uses 15c (data: 46% win, +2.39c/trade)
            # Others use 12c (backtested default)
            sport_tp = self._get_sport_profile().get("take_profit", LIVE_TAKE_PROFIT_CENTS)
            if gain_cents >= sport_tp:
                should_exit = True
                reason = (
                    f"TAKE PROFIT: +{gain_cents}¢ "
                    f"({entry_price}¢ → {current_value}¢)"
                )

            # --- 2. NEAR-SETTLEMENT: match is essentially over → lock in ---
            # If YES price hits 93¢+, the player is almost certainly winning.
            # Don't risk a freak comeback — take the profit.
            if not should_exit and held_side == "yes" and current_value >= LIVE_NEAR_SETTLE_CENTS:
                should_exit = True
                reason = (
                    f"NEAR-SETTLE: {current_value}¢ (match nearly won, "
                    f"entry {entry_price}¢, gain +{gain_cents}¢)"
                )

            # --- 2b. TRAILING STOP: price dropped from peak ---
            # Once we're in a position, track the peak price. If it drops N¢
            # from the peak and we're above entry, lock in partial gains.
            # Sport-specific trail_stop if configured, otherwise global default.
            # INSTINCT: In clutch/empty-net situations, widen the trail to avoid
            # getting shaken out by high-volatility noise.
            if not should_exit and self.mode == "momentum":
                peak = self._peak_values.get(ticker, current_value)
                drop_from_peak = peak - current_value
                sport_trail = self._get_sport_profile().get("trail_stop", MOMENTUM_DQS_TRAIL_STOP)

                # Detect current situation for dynamic stop adjustment
                from bot.game_context import SportInstincts
                exit_instincts = SportInstincts.detect(
                    self._game_ctx, self._last_espn_data, self.sport or ""
                )
                if exit_instincts.should_widen_stops:
                    # Clutch/empty-net: widen trail by 50% to ride through noise
                    sport_trail = int(sport_trail * 1.5)
                    logger.debug("Instincts widened trail_stop to %dc (%s)", sport_trail, exit_instincts.flags)

                if drop_from_peak >= sport_trail and gain_cents > 0:
                    should_exit = True
                    reason = (
                        f"TRAILING STOP: peaked at {peak}¢, dropped {drop_from_peak}¢ "
                        f"(entry {entry_price}¢ → {current_value}¢, locking +{gain_cents}¢)"
                    )

            # --- 2c. SCORE FLIP + OPPONENT RUN: team lost lead or opponent surging ---
            # Tier 2.3 (Apr 16): tightened — previously fired on raw score_diff
            # flipping sign, which kills winners when the opponent scores once
            # in a game we're still momentum-favored to win. Now requires BOTH
            #   (a) raw score diff flipped (effective_score_diff < 0), AND
            #   (b) GameContext confirms it's a real flip (momentum < 0 AND
            #       lead_trend < 0 — i.e. lead is shrinking, not just noisy).
            # Conservative: strictly exits less often than before, so we only
            # lose additional upside on false-positive flips (no new downside).
            # Replay validation skipped: only 2 historical SCORE FLIP exits in
            # live_journal.json (both MLB, now disabled) — n too small for a
            # replay, so we ship the tightening and re-evaluate after Tier 1
            # telemetry produces more data.
            if not should_exit and self.mode == "momentum" and self._game_ctx:
                gc = self._game_ctx
                effective_score_diff = score_diff
                if ticker == self._opponent_ticker:
                    effective_score_diff = -score_diff if score_diff is not None else None

                # Exit if team is now losing AND GameContext confirms real flip
                flip_confirmed = gc.momentum < 0 and gc.lead_trend < 0
                if (effective_score_diff is not None
                        and effective_score_diff < 0
                        and flip_confirmed):
                    should_exit = True
                    reason = (
                        f"SCORE FLIP: trailing by {abs(effective_score_diff)}, "
                        f"mom={gc.momentum:+.2f} lead_trend={gc.lead_trend:+.2f} "
                        f"({entry_price}¢ → {current_value}¢, {gain_cents:+d}¢)"
                    )
                # Exit if opponent is on a scoring run and we're underwater
                elif gc.opponent_on_run and gain_cents < 0:
                    should_exit = True
                    reason = (
                        f"OPP RUN EXIT: opponent on scoring run, position underwater "
                        f"({entry_price}¢ → {current_value}¢, {gain_cents:+d}¢)"
                    )

            # --- 3. UNDERWATER EXIT (REMOVED Apr 16) ---
            # Data-killed in Apr 14 audit: every UW exit recovered to TP if held.
            # HARD STOP-LOSS + DOLLAR STOP below handle runaway losses; no
            # tick-count "momentum lost" bail. See config.py note.

            # --- 4. HARD STOP-LOSS ---
            # Sport-specific: tennis uses 8c (data: caps losses, +3.6% ROI)
            # Others use 30c global safety net
            sport_sl = self._get_sport_profile().get("stop_loss", LIVE_STOP_LOSS_CENTS)
            if not should_exit:
                drop_cents = entry_price - current_value
                if drop_cents >= sport_sl:
                    should_exit = True
                    reason = f"STOP-LOSS: dropped {drop_cents}¢ from entry ({entry_price}¢ → {current_value}¢)"

            # --- 4b. HARD DOLLAR STOP-LOSS ---
            # Cents-based stop can still allow huge $ losses on big positions.
            # Data: 7 trades lost >$10, totaling -$127. $5 cap turns -$104 → +$2.84.
            if not should_exit:
                from bot.config import MOMENTUM_MAX_LOSS_DOLLARS
                contracts = bet.get("contracts", 1)
                unrealized_loss = (entry_price - current_value) / 100.0 * contracts
                if unrealized_loss >= MOMENTUM_MAX_LOSS_DOLLARS:
                    should_exit = True
                    reason = (
                        f"DOLLAR STOP: ${unrealized_loss:.2f} loss exceeds "
                        f"${MOMENTUM_MAX_LOSS_DOLLARS:.2f} cap "
                        f"({entry_price}¢ → {current_value}¢ x{contracts})"
                    )

            # --- 5. EDGE REVERSAL: wrong side of the market now (arb mode) ---
            if not should_exit and edge != 0:
                if held_side == "yes" and edge < -0.02:
                    should_exit = True
                    reason = f"edge reversed to {edge:+.1%}"
                elif held_side == "no" and edge > 0.02:
                    should_exit = True
                    reason = f"edge reversed to {edge:+.1%}"

            # --- 6. EDGE FADING (arb mode) ---
            if not should_exit and edge != 0:
                if abs(relative_edge) < LIVE_WATCH_EDGE_THRESHOLD * 0.3:
                    should_exit = True
                    reason = f"edge faded to {relative_edge:.1%}"

            if should_exit:
                # Calculate P&L in cents → dollars
                contracts = bet.get("contracts", 1)
                pnl = (current_value - entry_price) / 100.0 * contracts

                if PAPER_MODE:
                    # Paper mode: record exit to paper_trades.json so balance
                    # stays accurate for subsequent re-entries.
                    logger.info(
                        "LiveGameWatcher PAPER EXIT: %s %s — %s (pnl=$%.2f)",
                        ticker, held_side.upper(), reason, pnl,
                    )
                    try:
                        from bot.executor import _paper_record_exit
                        exit_price = current_value / 100.0
                        order_id = bet.get("order_id", "")
                        _paper_record_exit(order_id, exit_price, round(pnl, 4))
                        # Also mark position as exited in positions.json so the
                        # orphan auto-close in _check_position_limits() doesn't
                        # later re-settle this trade against market outcome.
                        from bot.state_io import load_json as _lj, save_json as _sj
                        from bot.config import POSITIONS_FILE
                        _positions = _lj(POSITIONS_FILE)
                        if isinstance(_positions, list):
                            for _p in _positions:
                                if (isinstance(_p, dict)
                                        and _p.get("ticker") == ticker
                                        and _p.get("status") in ("resting", "filled", "partial")):
                                    _p["status"] = "exited"
                                    _p["exit_price"] = exit_price
                                    _p["unrealized_pnl"] = round(pnl, 4)
                                    _p["resolved_at"] = datetime.now(timezone.utc).isoformat()
                            _sj(POSITIONS_FILE, _positions)
                    except Exception as e:
                        logger.warning("Paper exit record failed: %s", e)
                else:
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None, lambda t=ticker, r=reason: exit_position(t, reason=f"live_watcher: {r}")
                    )
                    if not result.get("success"):
                        logger.warning(
                            "LiveGameWatcher exit FAILED for %s: %s",
                            ticker, result.get("reason", "unknown"),
                        )
                        continue
                    pnl = result.get("realized_pnl", pnl)

                hold_seconds = int(time.time() - bet.get("entered_at", time.time()))
                exit_gc = self._game_ctx
                exit_record = {
                    **bet,
                    "reason": reason,
                    "pnl": pnl,
                    "exit_price": current_value,
                    "peak_price": prev_peak,
                    "trough_price": min(self._price_history) if self._price_history else current_value,
                    "hold_seconds": hold_seconds,
                    "exit_game_state": {
                        "score_diff": exit_gc.score_diff if exit_gc else None,
                        "period": exit_gc._snapshots[-1].get("period") if exit_gc and exit_gc._snapshots else None,
                        "completion": round(exit_gc.game_completion_pct, 3) if exit_gc else None,
                        "wp": round(exit_gc.win_probability, 3) if exit_gc else None,
                    } if exit_gc else None,
                }
                self.exits.append(exit_record)
                self.bets_placed.remove(bet)
                self._peak_values.pop(ticker, None)
                self._trailing_active.pop(ticker, None)
                logger.info(
                    "LiveGameWatcher EXIT: %s %s — %s (pnl=$%.2f, held %ds, peak %dc)",
                    ticker, held_side.upper(), reason, pnl, hold_seconds, prev_peak,
                )
                _journal_append({
                    "event": "exit",
                    "match": self._match_title or self.query,
                    "ticker": ticker,
                    "side": held_side,
                    "sport": self.sport or "",
                    "reason": reason,
                    "pnl": pnl,
                    "entry_price": entry_price,
                    "exit_value": current_value,
                    "peak_value": prev_peak,
                    "hold_seconds": hold_seconds,
                    "mode": bet.get("entry_mode", self.mode),
                    "game_state_at_entry": bet.get("game_state"),
                    "game_state_at_exit": exit_record.get("exit_game_state"),
                })

    def _already_bet(self, ticker: str, side: str) -> bool:
        return any(
            b.get("ticker") == ticker and b.get("side") == side
            for b in self.bets_placed
        )

    async def _auto_bet(
        self, market: dict, espn_prob: float,
        edge: float, relative_edge: float, game: dict,
    ):
        """Place a trade using the existing execute_trade() + kelly_size() pipeline."""
        from bot.executor import execute_trade
        from bot.sizing import kelly_size

        side = "yes" if edge > 0 else "no"
        price_cents = market.get("yes_ask" if side == "yes" else "no_ask", 50)
        win_prob = espn_prob if side == "yes" else (1.0 - espn_prob)

        # Use paper balance when in paper mode — self.balance may be real Kalshi balance
        bet_balance = self.balance
        if PAPER_MODE:
            from bot.config import PAPER_STARTING_BALANCE
            from bot.executor import _load_json
            from bot.config import PAPER_TRADES_FILE
            paper_trades = _load_json(PAPER_TRADES_FILE)
            if isinstance(paper_trades, list):
                bet_balance = PAPER_STARTING_BALANCE
                for t in paper_trades:
                    if not isinstance(t, dict):
                        continue
                    entry_cost = t.get("contracts", 0) * t.get("entry_price", 0.0)
                    status = t.get("status", "open")
                    if status in ("open", "won", "lost", "exited_early"):
                        bet_balance -= entry_cost
                    if status == "won":
                        bet_balance += t.get("contracts", 0) * 1.0
                    elif status == "exited_early":
                        bet_balance += t.get("contracts", 0) * t.get("exit_price", 0.0)
                bet_balance = round(bet_balance, 2)

        sizing = kelly_size(
            edge=abs(edge),
            probability=win_prob,
            balance=bet_balance,
            price_cents=price_cents,
            confidence=0.75,
        )
        if sizing["contracts"] <= 0:
            logger.info("LiveGameWatcher: sizing returned 0 contracts — %s", sizing.get("reason", ""))
            return

        # Build opportunity dict compatible with execute_trade()
        # Note: this bypasses ACTIVE_STRATEGIES intentionally —
        # the user explicitly requested this trade via WATCH command.
        opp = {
            "type": "live_latency_arb",
            "ticker": market.get("ticker", self.ticker),
            "title": market.get("title", ""),
            "market": market,
            "edge": edge,
            "relative_edge": relative_edge,
            "recommended_side": side,
            "espn_prob": espn_prob,
            "kalshi_price": price_cents / 100.0,
            "matched_team": self.query,
            "game": {
                "home_team": game.get("home_team"),
                "away_team": game.get("away_team"),
                "status": game.get("status"),
            },
            "edge_result": {
                "fair_value": round(espn_prob, 4),
                "kalshi_price": market.get("yes_ask", 50) / 100.0,  # always YES price for direction check
                "edge": edge,
                "relative_edge": relative_edge,
                "confidence": 0.75,
                "self_check_passed": True,
                "math_chain": [f"ESPN: {espn_prob:.1%} | Kalshi: {price_cents}c"],
                "warnings": [],
            },
            "sizing": sizing,
        }

        result = execute_trade(opp, sizing)
        if not result["success"]:
            logger.warning(
                "LiveGameWatcher BET BLOCKED: %s %s — %s",
                opp["ticker"], side.upper(), result.get("reason", "unknown"),
            )
            return
        if result["success"]:
            order = result["order_result"]
            self.bets_placed.append({
                "ticker": opp["ticker"],
                "side": side,
                "contracts": sizing["contracts"],
                "price_cents": price_cents,
                "order_id": order.get("order_id", "PAPER"),
            })
            logger.info(
                "LiveGameWatcher BET: %s %s %dx @ %sc (edge=%.1f%%)",
                opp["ticker"], side.upper(), sizing["contracts"],
                price_cents, relative_edge * 100,
            )

    def _format_status_card(
        self, *, home_team: str, away_team: str,
        home_score, away_score, period_label: str, clock: str,
        espn_prob: float, kalshi_ask_cents: int,
        edge: float, relative_edge: float,
        last_update_secs: int,
    ) -> str:
        lines = ["LIVE WATCH"]
        lines.append("")
        score_line = f"{away_team} @ {home_team}"
        lines.append(score_line)
        if home_score != "?" and away_score != "?":
            period_str = f"({period_label} {clock})".strip("() ").strip()
            lines.append(f"Score: {home_score}-{away_score}  {period_str}".strip())
        lines.append("")
        lines.append("EDGE:")
        lines.append(f"  ESPN:   {espn_prob:.0%}")
        lines.append(f"  Kalshi: {kalshi_ask_cents}c  ({kalshi_ask_cents}%)")
        rel_pct = f"{abs(relative_edge):.0%}"
        direction = "BUY YES" if edge > 0 else "BUY NO" if edge < 0 else "-"
        if abs(relative_edge) >= LIVE_WATCH_EDGE_THRESHOLD:
            lines.append(f"  Gap: {rel_pct} <- {direction}")
        else:
            lines.append(f"  Gap: {rel_pct} (below threshold)")
        lines.append("")
        if self.bets_placed:
            for b in self.bets_placed:
                entry_c = b.get("price_cents", 0)
                side = b["side"]
                # compute current value
                if side == "yes":
                    cur_val = kalshi_ask_cents  # approximate
                else:
                    cur_val = 100 - kalshi_ask_cents
                gain_pct = (cur_val - entry_c) / entry_c * 100 if entry_c else 0
                gain_str = f"+{gain_pct:.0f}%" if gain_pct >= 0 else f"{gain_pct:.0f}%"
                trailing = " [TRAILING]" if self._trailing_active.get(b.get("ticker")) else ""
                peak = self._peak_values.get(b.get("ticker"), cur_val)
                lines.append(
                    f"HOLDING: {side.upper()} {b['contracts']}x @ {entry_c}c "
                    f"→ {cur_val}c ({gain_str}){trailing}"
                )
                if self._trailing_active.get(b.get("ticker")):
                    lines.append(f"  Peak: {peak:.0f}c | Stop if drops to {peak * (1 - LIVE_TRAILING_STOP):.0f}c")
        if self.exits:
            for ex in self.exits:
                pnl = (ex.get("pnl") or 0)
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                lines.append(f"EXITED: {ex['side'].upper()} — {ex['reason']} ({pnl_str})")
        if not self.bets_placed and not self.exits:
            lines.append("No bets placed yet")
        lines.append("")
        lines.append(f"  {last_update_secs}s elapsed | UNWATCH to stop")
        return "\n".join(lines)

    def _format_session_summary(self, home: str, away: str) -> str:
        duration_min = (time.time() - self._started_at) / 60
        total_ticks = int(duration_min * 60 / LIVE_POLL_INTERVAL)

        lines = [f"Watch ended: {self.query.upper()}"]
        lines.append(f"Game: {away} @ {home}")
        lines.append(f"Duration: {duration_min:.0f} min | Mode: {self.mode}")
        total_bets = len(self.bets_placed) + len(self.exits)
        if total_bets:
            lines.append(f"Bets: {total_bets} placed, {len(self.exits)} exited")
            for b in self.bets_placed:
                lines.append(f"  HOLDING: {b['side'].upper()} {b['contracts']}x @ {b['price_cents']}c")
            for ex in self.exits:
                pnl = (ex.get("pnl") or 0)
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                lines.append(f"  EXITED: {ex['side'].upper()} — {ex['reason']} ({pnl_str})")
            total_pnl = sum((ex.get("pnl") or 0) for ex in self.exits)
            if self.exits:
                lines.append(f"Session P&L: ${total_pnl:+.2f}")
        else:
            lines.append("No bets placed.")

        # Journal the session end
        # Tier 1.3: surface tick_telem so RECAP / post-session analysis can answer
        # "where did the ticks go?" (no_leader / dqs_fail / dip_too_big /
        # conviction_checked / instinct_avoid / execute_failed breakdown).
        _journal_append({
            "event": "session_end",
            "match": self._match_title or f"{away} @ {home}",
            "ticker": self.ticker,
            "mode": self.mode,
            "duration_min": round(duration_min, 1),
            "ticks": total_ticks,
            "bets_placed": total_bets,
            "exits": len(self.exits),
            "total_pnl": sum((ex.get("pnl") or 0) for ex in self.exits),
            "price_history": list(self._price_history) if self._price_history else [],
            "tick_telem": dict(self._tick_telem),
        })

        return "\n".join(lines)


# ======================================================================
# Auto-scanner: discovers live matches and starts watchers automatically
# ======================================================================

LIVE_SCAN_INTERVAL = 120   # scan for new live matches every 2 minutes
MAX_AUTO_WATCHERS  = 5     # max concurrent — each watcher fetches 2 tickers/tick (both sides)
MIN_VOLUME_LIVE    = 1000  # minimum volume to consider a match "live" (not upcoming)

# Track volume between scans to detect active trading
_prev_scan_volumes: dict[str, int] = {}
_prev_scan_date: str = ""  # date when volumes were last recorded
# Event tickers already watched this session — prevents restart loops
_recently_watched: set[str] = set()


def _is_today_market(ticker: str) -> bool:
    """Check if a market's ticker is for today (local or UTC).

    Kalshi uses UTC dates in tickers, so an evening NBA game in the US
    shows as tomorrow's UTC date. We accept both today-local and today-UTC.
    """
    import re
    from datetime import date, datetime, timezone, timedelta
    today_local = date.today()
    today_utc = datetime.now(timezone.utc).date()
    valid_dates = {today_local, today_utc}
    # Also accept yesterday-UTC for early-morning edge case
    valid_dates.add(today_utc - timedelta(days=1))
    # Ticker format: KXATPMATCH-26APR09VACHUR-VAC → extract 26APR09
    m = re.search(r'(\d{2})([A-Z]{3})(\d{2})', ticker)
    if not m:
        return False
    year_short, month_str, day_str = m.group(1), m.group(2), m.group(3)
    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    try:
        month = months.get(month_str, 0)
        day = int(day_str)
        year = 2000 + int(year_short)
        ticker_date = date(year, month, day)
        return ticker_date in valid_dates
    except (ValueError, KeyError):
        return False


async def scan_live_matches(notifier, active_watchers: dict, balance: float = 0.0):
    """
    Scan all 1v1 match series on Kalshi for live markets with clear leaders.
    Auto-starts momentum watchers for matches not already being watched.

    Filters:
    - Must have a clear leader (60%+)
    - Must have real volume (5k+) indicating the match is actually live
    - Caps at MAX_AUTO_WATCHERS concurrent to avoid overload

    Called periodically from the main bot loop.
    Returns list of newly started (query, watcher) tuples.
    """
    from agent.kalshi_client import get_markets
    from bot.kalshi_series import MATCH_SERIES, SPORTS_SERIES
    from datetime import date

    # Daily cleanup: reset stale volumes and recently-watched from yesterday
    global _prev_scan_date, _prev_scan_volumes, _recently_watched
    today = date.today().isoformat()
    if _prev_scan_date != today:
        _prev_scan_volumes.clear()
        _recently_watched.clear()
        _prev_scan_date = today
        logger.info("Live scan: new day %s — cleared volume cache and recently_watched", today)

    # Don't start more if already at capacity
    current_auto = len(active_watchers)
    slots = MAX_AUTO_WATCHERS - current_auto
    if slots <= 0:
        logger.debug("Live scan: at capacity (%d watchers), skipping", current_auto)
        return []

    started = []
    candidates = []

    # Telemetry: count drops by reason so we can diagnose why watchers
    # don't spawn. Mirrors the `_telem` pattern in scanner.py:427.
    _telem: dict[str, int] = {
        "seen": 0, "disabled_sport": 0, "api_error": 0, "no_markets": 0,
        "bad_event_shape": 0, "low_volume": 0, "not_today": 0,
        "no_leader": 0, "settled": 0, "unknown_name": 0,
        "already_watching": 0, "recently_watched": 0,
        "no_vol_growth_first_seen": 0, "no_vol_growth_idle": 0,
        "capacity_capped": 0, "spawned": 0,
    }

    # Scan both 1v1 match series (tennis/UFC) AND team sport series (NBA/MLB/NHL)
    all_series = {**MATCH_SERIES, **SPORTS_SERIES}
    for sport, series in all_series.items():
        # Tier 1.1: skip sports marked disabled in SPORT_PROFILES.
        # MLB is disabled per Apr 14 audit (13 trades, -$11.85). Spawning
        # watchers for it wastes ~47% of tick volume on a sport we can't trade.
        if SPORT_PROFILES.get(sport, {}).get("disabled"):
            _telem["disabled_sport"] += 1
            continue
        try:
            result = get_markets(series_ticker=series, status="open", limit=200)
        except Exception as e:
            logger.debug("Live scan error for %s: %s", series, e)
            _telem["api_error"] += 1
            continue

        markets = result.get("markets", [])
        if not markets:
            _telem["no_markets"] += 1
            continue

        # Group markets by event_ticker (each match has 2 sides)
        events: dict[str, list[dict]] = {}
        for m in markets:
            ev = m.get("event_ticker", "")
            if ev:
                events.setdefault(ev, []).append(m)

        for event_tk, sides in events.items():
            _telem["seen"] += 1
            if len(sides) != 2:
                _telem["bad_event_shape"] += 1
                continue

            side_a, side_b = sides
            price_a = side_a.get("yes_ask", 50)
            price_b = side_b.get("yes_ask", 50)
            vol_a = side_a.get("volume", 0)
            vol_b = side_b.get("volume", 0)
            total_vol = vol_a + vol_b

            # Must have real trading activity (live match, not upcoming)
            if total_vol < MIN_VOLUME_LIVE:
                _telem["low_volume"] += 1
                continue

            # Must be today's match (ticker date = today) — skip pre-fight/upcoming
            leader_ticker_raw = side_a.get("ticker", "")
            if not _is_today_market(leader_ticker_raw):
                _telem["not_today"] += 1
                continue

            # Determine the leader
            if price_a >= MOMENTUM_LEADER_MIN * 100:
                leader, leader_price = side_a, price_a
            elif price_b >= MOMENTUM_LEADER_MIN * 100:
                leader, leader_price = side_b, price_b
            else:
                _telem["no_leader"] += 1
                continue

            # Skip settled markets
            if leader_price >= 95:
                _telem["settled"] += 1
                continue

            title = (leader.get("title") or "").lower()
            leader_ticker = leader.get("ticker", "")
            player_name = LiveGameWatcher._extract_player_name(title, "")
            # For team sports (NBA/MLB/NHL), extract team abbrev from ticker
            # e.g. KXNBAGAME-26APR10PHXLAL-PHX → "PHX"
            if (not player_name or player_name == "?") and leader_ticker:
                team_abbrev = leader_ticker.rsplit("-", 1)[-1] if "-" in leader_ticker else ""
                if team_abbrev:
                    player_name = team_abbrev.upper()
            if not player_name or player_name == "?":
                _telem["unknown_name"] += 1
                continue

            query_key = _normalize(player_name)

            # Skip if already watching OR recently watched (avoid restart loops)
            already_watching = False
            for existing_q, (existing_w, _) in active_watchers.items():
                if (existing_w.ticker == leader_ticker
                        or query_key in existing_q
                        or existing_q in query_key):
                    already_watching = True
                    break
            if already_watching:
                _telem["already_watching"] += 1
                continue
            # Also skip if this event_ticker was already watched (prevents
            # scanner from restarting a watcher that just finished)
            ev_ticker = leader.get("event_ticker", "")
            if ev_ticker and ev_ticker in _recently_watched:
                _telem["recently_watched"] += 1
                continue


            vol24_a = side_a.get("volume_24h") or 0
            vol24_b = side_b.get("volume_24h") or 0
            total_vol_24h = vol24_a + vol24_b

            # Find opponent ticker (the other side of this event)
            opponent_ticker = None
            for s in sides:
                if s.get("ticker") != leader_ticker:
                    opponent_ticker = s.get("ticker")
                    break

            candidates.append({
                "query": query_key,
                "player_name": player_name,
                "sport": sport,
                "ticker": leader_ticker,
                "opponent_ticker": opponent_ticker,
                "event_ticker": event_tk,
                "price": leader_price,
                "volume": total_vol,
                "volume_24h": total_vol_24h,
            })

    # Sort by 24h volume (most actively traded RIGHT NOW first)
    candidates.sort(key=lambda c: c["volume_24h"], reverse=True)

    # Filter: only start watchers for matches with GROWING volume
    # (= actively being traded right now, not sitting idle)
    active_candidates = []
    for c in candidates:
        ticker = c["ticker"]
        current_vol = c["volume"]
        prev_vol = _prev_scan_volumes.get(ticker, 0)
        _prev_scan_volumes[ticker] = current_vol

        if prev_vol == 0:
            # First time seeing this market — record volume, skip this cycle
            # Next scan (2 min later) we'll check if volume grew
            logger.debug(
                "Live scan: %s first seen (vol=%d) — will check growth next cycle",
                c["player_name"], current_vol,
            )
            _telem["no_vol_growth_first_seen"] += 1
            continue

        vol_growth = current_vol - prev_vol
        if vol_growth < 500:
            # Volume hasn't grown meaningfully — match not actively trading
            logger.debug(
                "Live scan: %s idle (vol growth=%d in 2min) — skipping",
                c["player_name"], vol_growth,
            )
            _telem["no_vol_growth_idle"] += 1
            continue

        logger.info(
            "Live scan: %s ACTIVE (vol +%d in 2min) — eligible",
            c["player_name"], vol_growth,
        )
        active_candidates.append(c)

    # Capacity is capped at `slots`; the remainder are eligible but dropped this tick
    if len(active_candidates) > slots:
        _telem["capacity_capped"] += len(active_candidates) - slots
    for c in active_candidates[:slots]:
        logger.info(
            "Live scan FOUND: %s at %d%% in %s (%s) vol=%d — starting momentum watcher",
            c["player_name"], c["price"], c["sport"].upper(), c["ticker"], c["volume"],
        )
        _journal_append({
            "event": "scan_found",
            "match": c["player_name"],
            "ticker": c["ticker"],
            "sport": c["sport"],
            "price": c["price"],
            "volume": c["volume"],
        })
        # Mark this event as watched so scanner doesn't restart it
        if c.get("event_ticker"):
            _recently_watched.add(c["event_ticker"])
        watcher = LiveGameWatcher(
            query=c["query"],
            notifier=notifier,
            sport=c["sport"],
            balance=balance,
            mode="momentum",
            ticker=c["ticker"],
            opponent_ticker=c.get("opponent_ticker"),
        )
        # Store event_ticker so crash handler can remove from _recently_watched
        watcher._match_event_ticker = c.get("event_ticker", "")
        started.append((c["query"], watcher))
        _telem["spawned"] += 1

    if not candidates:
        logger.debug("Live scan: no live matches with clear leaders found")

    # Emit telemetry line — one per scan — so we can see in logs WHY
    # watchers don't spawn when they should. Suppress zeros to keep the
    # log line compact.
    _drops = {k: v for k, v in _telem.items() if k not in ("seen", "spawned") and v > 0}
    logger.info(
        "LIVE_SCAN_TELEMETRY: seen=%d spawned=%d capacity=%d drops=%s",
        _telem["seen"], _telem["spawned"], slots, _drops or "none",
    )

    return started
