"""Per-scan snapshot of every active Kalshi market. Append-only JSONL with
daily rotation via scheduler.

Session 12 (Apr 25 pivot-enabling instrumentation arc): existing collection
points (decisions.jsonl, CFs in clv.json, predictions.jsonl) only fire on
opportunities a strategy scanner already considered. Kalshi has thousands of
active markets at any time; we scan a curated handful. Without a record of
the full universe, we can't ask "what alpha is hiding in markets we don't
even look at?" Session 13's hypothetical-strategy back-tester needs a
captured market universe to evaluate against — this is that capture.

Design: buffer-and-flush.
  1. snapshot_universe(scan_id) cursor-loops every active market into an
     in-memory buffer keyed by (scan_id, ticker). scanned_by starts empty.
  2. Scanners call on_market_seen(scan_id, ticker, scanner_name) at their
     per-ticker iteration seam. Mutates the buffered row's scanned_by list.
  3. flush_universe(scan_id) atomically appends every buffered row to
     universe.jsonl with populated scanned_by, then pops the buffer.

Caller (_main_loop) wraps scan_cycle in try/finally so flush always runs
even if a scanner raises. No reader API — analysis tools (tools/universe_
report.py) read the file directly.

Schema:
  {ts, scan_id, ticker, series_ticker, event_ticker, status, close_ts,
   yes_ask, yes_bid, no_ask, no_bid, volume_24h, open_interest, scanned_by}
On partial-cursor failure, every row in that scan also carries `partial: true`.
"""
from __future__ import annotations

import json
import logging
import threading
import time as _time
from collections import deque
from datetime import datetime, timezone

from bot.config import BOT_STATE_DIR
from bot.regime import tag as regime_tag
from bot.state_io import load_json, save_json

UNIVERSE_FILE = BOT_STATE_DIR / "universe.jsonl"
BOT_STATE_FILE = BOT_STATE_DIR / "bot_state.json"

# Session 15.5: trailing-window partial-rate metering. Persistent counters
# live in bot_state.json (total_snapshots_today, partial_snapshots_today,
# last_universe_metering_reset); the window below is in-memory for the
# WARN trigger only.
_PARTIAL_WINDOW: deque[bool] = deque(maxlen=10)
_PARTIAL_WARN_THRESHOLD = 0.10

# Page size for the cursor enumeration. Kalshi caps at 200.
_PAGE_LIMIT = 200

# Wall-clock deadline for snapshot_universe. Empirically Kalshi has 50K+
# active markets at any moment, dominated by Multi-Variate Event (MVE) parlay
# expansions (KXMVE* tickers, ~95%) we filter out below. Even with the filter
# we still have to PAGINATE through MVE pages to reach non-MVE markets —
# under load (live_watcher polling) each page can spend 30-40s on 429 retries.
# 90s gives us ~300 pages = 60K market peek; we capture what we can, mark
# rows `partial: true`, let the next scan try again. Best-effort: this never
# gates trading.
_SNAPSHOT_DEADLINE_SEC = 90

# Skip Multi-Variate Event collections — these are combinatorial parlay
# products Kalshi creates in bulk (60K+ at any time). Counting them here
# overwhelms the universe log without informing strategy gaps: an "ignored
# parlay" is an ignored parlay times 13 legs, not a missed opportunity. The
# tickers always start with "KXMVE". If Session 13's hypothetical-strategy
# framework ever wants to back-test parlay products, lift this filter.
_MVE_PREFIX = "KXMVE"

_LOCK = threading.Lock()
_BUFFER: dict[str, dict[str, dict]] = {}  # scan_id -> {ticker -> row}
_logger = logging.getLogger("glint.universe")


def _active_series_tickers() -> list[str]:
    """Series prefixes the active scanners care about. Used as a "shadow fetch"
    pass so the universe buffer always covers tickers that scanners will
    attribute against — under empirical Kalshi cursor order, the first 300
    pages are dominated by MVE parlay expansions (filtered out) and the
    cursor rarely reaches active-strategy series within the snapshot
    deadline. Per-series fetches are 1 API call each and resolve in <1s.

    Sources (kept in sync by hand):
      - bot.config: WEATHER_SERIES_TICKERS, INDEX_RANGE_SERIES_TICKERS,
        SPORTS_FUTURES_TICKERS
      - bot.scanner_sports_arb: SPREAD_SERIES, TOTAL_SERIES, GAME_SERIES,
        CHAMPIONSHIP_SERIES, PLAYOFF_SERIES_SERIES values
    Returns deduped list. Never raises (best-effort import).
    """
    out: set[str] = set()
    try:
        from bot.config import (
            WEATHER_SERIES_TICKERS,
            INDEX_RANGE_SERIES_TICKERS,
            SPORTS_FUTURES_TICKERS,
        )
        out.update(WEATHER_SERIES_TICKERS or [])
        out.update(INDEX_RANGE_SERIES_TICKERS or [])
        out.update(SPORTS_FUTURES_TICKERS or [])
    except Exception:
        _logger.exception("universe._active_series_tickers: config import failed")
    try:
        from bot.scanner_sports_arb import (
            SPREAD_SERIES, TOTAL_SERIES, GAME_SERIES,
            CHAMPIONSHIP_SERIES, PLAYOFF_SERIES_SERIES,
        )
        for d in (SPREAD_SERIES, TOTAL_SERIES, GAME_SERIES,
                  CHAMPIONSHIP_SERIES, PLAYOFF_SERIES_SERIES):
            out.update(d.values())
    except Exception:
        _logger.exception("universe._active_series_tickers: sports_arb import failed")
    return sorted(out)


def snapshot_universe(scan_id: str) -> int:
    """Snapshot every active Kalshi market into an in-memory buffer keyed
    by `scan_id`. Two-pass:

      1. Cursor pagination (status=open) until exhausted or deadline —
         captures the long-tail of markets we don't actively scan. MVE
         parlay expansions are filtered out (95% of raw response volume).
      2. Per-active-series fetches — guarantees buffer covers tickers
         active scanners will attribute against (cursor order rarely
         reaches them within the deadline).

    Returns count buffered. Never raises.

    The buffer is held in module state until flush_universe(scan_id) writes
    it to disk. Callers MUST call flush_universe(scan_id) to avoid leaking
    memory across scans (try/finally at the call site).

    Partial-cursor tolerance: if pagination fails mid-loop or hits the
    deadline, every buffered row gets `partial: true` and we return the
    partial count. Per-series pass runs regardless so attribution still
    works.
    """
    try:
        from agent.kalshi_client import get_markets
    except Exception:
        _logger.exception("universe.snapshot_universe: kalshi_client import failed")
        return 0

    rows: dict[str, dict] = {}
    partial = False
    cursor: str | None = None
    pages = 0
    started = _time.monotonic()

    def _add_row(m: dict, ts: str) -> None:
        ticker = m.get("ticker")
        if not ticker:
            return
        if ticker.startswith(_MVE_PREFIX):
            return
        if ticker in rows:
            return  # first observation wins; per-series shadow won't overwrite
        # Kalshi's /markets response doesn't echo series_ticker on each
        # row (only accepts it as a request filter), so derive from the
        # ticker prefix: "KXTEMP-26APR25-T70.5" -> "KXTEMP".
        series = m.get("series_ticker") or ticker.split("-", 1)[0]
        row = {
            "ts": ts,
            "scan_id": scan_id,
            "ticker": ticker,
            "series_ticker": series,
            "event_ticker": m.get("event_ticker"),
            "status": m.get("status"),
            "close_ts": m.get("close_time"),
            "yes_ask": m.get("yes_ask"),
            "yes_bid": m.get("yes_bid"),
            "no_ask": m.get("no_ask"),
            "no_bid": m.get("no_bid"),
            # Capture both volume series. Lifetime `volume` is what the
            # vig_stack low_liquidity gate reads; volume_24h is the
            # rolling-window field reported in dashboards. Session 13a
            # added volume to support strategy back-testing.
            "volume": m.get("volume"),
            "volume_24h": m.get("volume_24h"),
            "open_interest": m.get("open_interest"),
            "title": m.get("title"),
            "scanned_by": [],
        }
        try:
            row["regime"] = regime_tag(
                ts=datetime.fromisoformat(ts),
                ticker=ticker,
                market_state=row,
            )
        except Exception:
            _logger.exception("universe._add_row: regime_tag failed for %s", ticker)
        rows[ticker] = row

    try:
        while True:
            if _time.monotonic() - started > _SNAPSHOT_DEADLINE_SEC:
                _logger.warning(
                    "universe.snapshot_universe: deadline %ds exceeded after %d pages (scan_id=%s) — flushing partial",
                    _SNAPSHOT_DEADLINE_SEC, pages, scan_id,
                )
                partial = True
                break

            try:
                kwargs = {"status": "open", "limit": _PAGE_LIMIT}
                if cursor:
                    kwargs["cursor"] = cursor
                result = get_markets(**kwargs)
            except Exception:
                _logger.warning(
                    "universe.snapshot_universe: cursor pagination failed at page %d (scan_id=%s) — flushing partial",
                    pages, scan_id, exc_info=True,
                )
                partial = True
                break

            if not isinstance(result, dict) or "error" in result:
                _logger.warning(
                    "universe.snapshot_universe: kalshi error at page %d (scan_id=%s): %r — flushing partial",
                    pages, scan_id, result,
                )
                partial = True
                break

            markets = result.get("markets") or []
            ts = datetime.now(timezone.utc).isoformat()
            for m in markets:
                _add_row(m, ts)

            pages += 1
            cursor = result.get("cursor")
            if not cursor:
                break

        cursor_count = len(rows)

        # Pass 2: per-active-series shadow fetch. Each call is fast
        # (single page, ~200ms) and guarantees attribution coverage for
        # active scanners.
        active_series = _active_series_tickers()
        active_added = 0
        for series in active_series:
            if _time.monotonic() - started > _SNAPSHOT_DEADLINE_SEC + 30:
                # Even the secondary pass has a budget — don't block forever
                # if Kalshi is hammered.
                _logger.warning(
                    "universe.snapshot_universe: shadow-fetch deadline at series=%s",
                    series,
                )
                partial = True
                break
            try:
                result = get_markets(series_ticker=series, status="open", limit=200)
            except Exception:
                _logger.warning(
                    "universe.snapshot_universe: shadow fetch failed for %s",
                    series, exc_info=True,
                )
                continue
            if not isinstance(result, dict) or "error" in result:
                continue
            ts = datetime.now(timezone.utc).isoformat()
            before = len(rows)
            for m in (result.get("markets") or []):
                _add_row(m, ts)
            active_added += len(rows) - before

        if partial:
            for r in rows.values():
                r["partial"] = True

        with _LOCK:
            _BUFFER[scan_id] = rows
        _logger.info(
            "universe snapshot: scan_id=%s pages=%d cursor_rows=%d active_added=%d total=%d%s",
            scan_id, pages, cursor_count, active_added, len(rows),
            " (PARTIAL)" if partial else "",
        )
        # Session 15.5: per-day counters + sliding-window WARN.
        try:
            _record_partial_meter(partial=partial)
        except Exception:
            _logger.debug("partial meter update failed", exc_info=True)
        return len(rows)
    except Exception:
        _logger.exception("universe.snapshot_universe failed for scan_id=%s", scan_id)
        return 0


def _record_partial_meter(*, partial: bool) -> None:
    """Increment per-day snapshot counters in bot_state.json and log a WARN
    when the trailing-window partial rate exceeds the threshold.

    Counters reset at midnight ET via the last_universe_metering_reset
    marker (mirrors the Session 5+6 rotation pattern). Atomic write via
    bot.state_io.save_json. Session 15.5.
    """
    try:
        from zoneinfo import ZoneInfo
        et_tz = ZoneInfo("America/New_York")
    except Exception:
        et_tz = timezone.utc  # fallback when tzdata is missing

    _PARTIAL_WINDOW.append(bool(partial))

    state = load_json(BOT_STATE_FILE)
    if not isinstance(state, dict):
        state = {}
    today_et = datetime.now(et_tz).date().isoformat()
    if state.get("last_universe_metering_reset") != today_et:
        state["total_snapshots_today"] = 0
        state["partial_snapshots_today"] = 0
        state["last_universe_metering_reset"] = today_et

    state["total_snapshots_today"] = int(state.get("total_snapshots_today", 0)) + 1
    if partial:
        state["partial_snapshots_today"] = int(state.get("partial_snapshots_today", 0)) + 1
    save_json(BOT_STATE_FILE, state)

    if len(_PARTIAL_WINDOW) >= 10:
        rate = sum(_PARTIAL_WINDOW) / len(_PARTIAL_WINDOW)
        if rate >= _PARTIAL_WARN_THRESHOLD:
            _logger.warning(
                "universe partial rate elevated: %.0f%% over last %d snapshots",
                rate * 100, len(_PARTIAL_WINDOW),
            )


def on_market_seen(scan_id: str, ticker: str, scanner_name: str) -> None:
    """Callback for scanners. Mutates buffered row's scanned_by list.

    No-op if scan_id or ticker is unknown to the buffer (defensive — handles
    scanners running before snapshot or markets the snapshot missed). Never
    raises.
    """
    try:
        with _LOCK:
            scan = _BUFFER.get(scan_id)
            if scan is None:
                return
            row = scan.get(ticker)
            if row is None:
                return
            seen = row.get("scanned_by")
            if seen is None:
                row["scanned_by"] = [scanner_name]
            elif scanner_name not in seen:
                seen.append(scanner_name)
    except Exception:
        _logger.exception(
            "universe.on_market_seen failed: scan_id=%s ticker=%s scanner=%s",
            scan_id, ticker, scanner_name,
        )


def get_buffered_markets(scan_id: str) -> list:
    """Return buffered Market rows for a scan_id as a list of
    bot.strategies.Market. Read-only — does not mutate the buffer.
    Returns [] if scan_id unknown.

    Added in Session 13a so scan_cycle can pass the universe to
    Strategy.candidate_markets without strategies calling Kalshi
    directly. The list is a snapshot — strategies that mutate the
    rows (e.g., on_market_seen) keep going through the existing
    callback path.
    """
    from bot.strategies import Market
    with _LOCK:
        scan = _BUFFER.get(scan_id)
        if scan is None:
            return []
        return [
            Market(
                ticker=r["ticker"],
                series_ticker=r["series_ticker"],
                event_ticker=r.get("event_ticker"),
                status=r.get("status") or "",
                close_ts=r.get("close_ts"),
                yes_ask=r.get("yes_ask"),
                yes_bid=r.get("yes_bid"),
                no_ask=r.get("no_ask"),
                no_bid=r.get("no_bid"),
                volume_24h=r.get("volume_24h"),
                open_interest=r.get("open_interest"),
                ts=r.get("ts"),
                scan_id=r.get("scan_id"),
                raw=r,
            )
            for r in scan.values()
        ]


def flush_universe(scan_id: str) -> int:
    """Atomically append every buffered row for `scan_id` to universe.jsonl.
    Returns count flushed. Pops the buffer entry whether or not the write
    succeeded (avoids unbounded buffer growth on persistent disk failure).
    Never raises.

    Idempotent: a second call with the same scan_id finds an empty buffer
    and is a no-op.
    """
    try:
        with _LOCK:
            rows = _BUFFER.pop(scan_id, None)
        if not rows:
            return 0
        lines = "".join(
            json.dumps(r, separators=(",", ":")) + "\n" for r in rows.values()
        )
        try:
            with _LOCK:
                BOT_STATE_DIR.mkdir(parents=True, exist_ok=True)
                with open(UNIVERSE_FILE, "a") as f:
                    f.write(lines)
            return len(rows)
        except Exception:
            _logger.exception("universe.flush_universe write failed for scan_id=%s", scan_id)
            return 0
    except Exception:
        _logger.exception("universe.flush_universe failed for scan_id=%s", scan_id)
        return 0
