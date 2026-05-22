"""Shadow-trade settlement resolver (Session 161).

`bot/shadow_trades.py` (Session 95) appends blocked-opportunity rows
(`family_disabled_reject` / `sport_disabled` / `reentry_blocked`) recording what
we WOULD have traded. Nothing ever settled them — every row sat `status="open"`,
`market_result=null`, `would_pnl=null`. This module settles them so we can answer
"were the blocks right?" (the Session 93 / 97 disable decisions).

Phase 0 (2026-05-21) finding: the bot ALSO records a CLV counterfactual for each
blocked opportunity, and `check_clv_settlements` already settles those into
`clv.json` (7,094 `counterfactual_settled` rows). So the primary path is a
**zero-API local join** against clv.json — the result is already on disk; the
shadow ledger just never read it back. Three bounded tiers:

  Tier 1  local join (zero API)  — exact ticker present in clv.json settled set;
          also shape-based dead-mark of provably-malformed tickers (clv._is_dead_ticker).
  Tier 2  event fetch (piggyback) — reuse clv._fetch_settled_markets for events
          whose exact ticker isn't in clv but a sibling is settled.
  Tier 3  per-ticker probe (bounded) — get_market for the residual sport rows;
          dead-mark ONLY on a confirmed terminal-but-non-binary result (void),
          never on a transient fetch error (forward-only rows must not be
          false-marked on a network blip).

Settlement math mirrors bot/tracker.py:374 resolve_trades (binary contracts,
dollars). Persistence mirrors clv._persist_progress: re-load-merge by `id`, atomic
tmp+rename, forward-only (never revert a terminal status, never clobber concurrent
shadow_trades appends). Reuses existing endpoints only (no new network calls).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from agent.kalshi_client import get_market
from bot import clv as _clv
from bot import shadow_trades as _shadow
from bot.clv import _event_ticker, _fetch_settled_markets, _is_dead_ticker

logger = logging.getLogger("glint.shadow_settlement")

# Runtime (per _main_loop cycle) caps — keep steady-state near-zero. Terminal
# rows are forward-only, so after the one-time backfill each cycle resolves only
# the handful of rows appended since the previous cycle.
_SHADOW_SETTLE_TIME_BUDGET_SEC = 120
_SHADOW_SETTLE_MAX_EVENT_FETCHES = 25
_SHADOW_SETTLE_MAX_TICKER_PROBES = 25

# One-time backfill caps (511 existing rows → ~11 family events + ≤82 sport probes).
_BACKFILL_TIME_BUDGET_SEC = 600
_BACKFILL_MAX_EVENT_FETCHES = 400
_BACKFILL_MAX_TICKER_PROBES = 600

_SETTLED_CLV_STATUSES = ("settled", "counterfactual_settled", "finalized", "closed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_shadow_rows() -> list[dict]:
    """Read shadow_trades.jsonl raw (one JSON object per line). Malformed lines
    are skipped, never fatal."""
    path = _shadow.SHADOW_TRADES_FILE
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return rows


def _clv_index() -> tuple[dict[str, str], set[str], set[str]]:
    """Build the settlement index from clv.json read RAW.

    Critical: read via clv._get_file() (respects the S158 test sandbox) but parse
    the file directly — NOT clv._load(), which filters to active strategies and
    would silently drop the disabled-sport CF records we need (Phase 0: the 186
    sport exact-matches are only visible on a raw read).

    Returns (settled_by_ticker {ticker -> "yes"|"no"}, settlement_failed_tickers,
    events_with_a_settled_market).
    """
    path = _clv._get_file()
    if not path.exists():
        return {}, set(), set()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return {}, set(), set()
    if not isinstance(data, list):
        return {}, set(), set()
    settled_by_ticker: dict[str, str] = {}
    failed: set[str] = set()
    events_settled: set[str] = set()
    for rec in data:
        if not isinstance(rec, dict):
            continue
        tk = rec.get("ticker")
        if not tk:
            continue
        status = rec.get("status")
        result = rec.get("market_result")
        if status in _SETTLED_CLV_STATUSES and result in ("yes", "no"):
            settled_by_ticker[tk] = result
            ev = _event_ticker(tk)
            if ev:
                events_settled.add(ev)
        elif status == "settlement_failed":
            failed.add(tk)
    return settled_by_ticker, failed, events_settled


def _market_result(market: object) -> Optional[str]:
    """Extract "yes"/"no" from a Kalshi market dict, or None if not settled."""
    if not isinstance(market, dict):
        return None
    status = str(market.get("status", "")).lower()
    result = str(market.get("result", "")).lower()
    if status in ("settled", "finalized", "closed") and result in ("yes", "no"):
        return result
    return None


def _probe_market(ticker: str) -> tuple[Optional[str], str]:
    """Single-ticker Kalshi probe (Tier 3). Returns (result, kind):
      ("yes"|"no", "settled") — resolved binary market
      (None, "void")          — terminal status but non-binary result (cancelled/void)
      (None, "open")          — market not yet settled
      (None, "error")         — fetch failed / 404 (transient — NEVER dead-mark on this)
    Dead-marking only on "void" (a confirmed terminal signal) avoids false terminal
    marks on a network blip; shadow rows are forward-only."""
    if not ticker:
        return None, "error"
    try:
        market = get_market(ticker)
    except Exception:
        return None, "error"
    if not isinstance(market, dict) or market.get("error"):
        return None, "error"
    status = str(market.get("status", "")).lower()
    result = str(market.get("result", "")).lower()
    if status in ("settled", "finalized", "closed"):
        return (result, "settled") if result in ("yes", "no") else (None, "void")
    return None, "open"


def _settle_row(row: dict, result: str, source: str, now: datetime) -> None:
    """Mark a shadow row settled (forward-only). would_pnl mirrors
    bot/tracker.py:374: won -> contracts*(1 - entry); lost -> -contracts*entry,
    in dollars (would_entry_price is decimal dollars). Unavailable-sizing rows
    (would_contracts null) get market_result + a won/lost flag, would_pnl stays
    null — never invent a contract count."""
    row["status"] = "settled"
    row["settled_at"] = now.isoformat()
    row["market_result"] = result

    extra = dict(row.get("extra") or {})
    extra["settlement_source"] = source

    side = row.get("would_side")
    if side in ("yes", "no"):
        won = result == side
        extra["would_outcome"] = "won" if won else "lost"
        contracts = row.get("would_contracts")
        entry = row.get("would_entry_price")
        if contracts is not None and entry is not None:
            pnl = contracts * (1.0 - entry) if won else -contracts * entry
            row["would_pnl"] = round(pnl, 4)
    row["extra"] = extra


def _dead_mark_row(row: dict, note: str, now: datetime) -> None:
    """Terminal status for a shadow row whose market will never produce a binary
    result (void / 404 / malformed). Forward-only; stops it re-polling (S157)."""
    row["status"] = "settlement_failed"
    row["settled_at"] = now.isoformat()
    extra = dict(row.get("extra") or {})
    extra["settlement_note"] = note
    row["extra"] = extra


def _persist(mutated: dict[str, dict]) -> int:
    """Re-load-merge by `id` + atomic tmp+rename (mirror clv._persist_progress).

    shadow_trades.py appends under an in-process threading.Lock, but this resolver
    runs in a different thread/process, so re-read the file fresh and replace only
    our mutated `id`s — preserving any rows appended during the run. Forward-only:
    only replace a row that is still `open` on the fresh read (never revert a
    terminal status). Lines match shadow_trades._append's compact format so
    untouched rows re-serialize byte-identically."""
    if not mutated:
        return 0
    path = _shadow.SHADOW_TRADES_FILE
    fresh = _load_shadow_rows()
    applied = 0
    out_lines: list[str] = []
    for row in fresh:
        rid = row.get("id")
        replacement = mutated.get(rid)
        if replacement is not None and row.get("status") == "open":
            row = replacement
            applied += 1
        out_lines.append(json.dumps(row, separators=(",", ":"), default=str))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text("\n".join(out_lines) + "\n" if out_lines else "")
    os.replace(tmp, path)
    return applied


def resolve_shadow_trades(
    *,
    max_event_fetches: int,
    max_ticker_probes: int,
    time_budget_sec: float,
    now: Optional[datetime] = None,
) -> dict:
    """Resolve open shadow rows via the three bounded tiers. Returns a summary
    dict (counts by source). Idempotent: terminal rows are skipped, so re-running
    only touches rows still open."""
    now = now or _now()
    deadline = time.monotonic() + time_budget_sec
    summary = {
        "open_before": 0,
        "settled_local": 0,
        "settled_event": 0,
        "settled_probe": 0,
        "dead_marked": 0,
        "event_fetches": 0,
        "ticker_probes": 0,
        "still_open": 0,
        "persisted": 0,
    }

    rows = _load_shadow_rows()
    open_rows = [r for r in rows if r.get("status") == "open"]
    summary["open_before"] = len(open_rows)
    if not open_rows:
        return summary

    settled_by_ticker, failed_tickers, events_settled = _clv_index()
    mutated: dict[str, dict] = {}

    # Tier 1 — local join (zero API) + shape-based dead-mark.
    for row in open_rows:
        ticker = row.get("ticker") or ""
        if ticker in settled_by_ticker:
            _settle_row(row, settled_by_ticker[ticker], "clv_local", now)
            mutated[row["id"]] = row
            summary["settled_local"] += 1
        elif ticker in failed_tickers or _is_dead_ticker(ticker):
            note = "clv_settlement_failed" if ticker in failed_tickers else "malformed_ticker"
            _dead_mark_row(row, note, now)
            mutated[row["id"]] = row
            summary["dead_marked"] += 1

    # Tier 2 — event fetch (piggyback on clv's proven event walk), dedup by event.
    by_event: dict[str, list[dict]] = {}
    for row in open_rows:
        if row["id"] in mutated:
            continue
        ev = _event_ticker(row.get("ticker") or "")
        if ev and ev in events_settled:
            by_event.setdefault(ev, []).append(row)
    for event, ev_rows in by_event.items():
        if summary["event_fetches"] >= max_event_fetches or time.monotonic() > deadline:
            break
        markets = _fetch_settled_markets(event)
        summary["event_fetches"] += 1
        for row in ev_rows:
            result = _market_result(markets.get(row.get("ticker")))
            if result:
                _settle_row(row, result, "event_fetch", now)
                mutated[row["id"]] = row
                summary["settled_event"] += 1

    # Tier 3 — bounded per-ticker probe (operator-approved fallback). Dead-mark
    # ONLY on a confirmed terminal-but-non-binary market; transient errors and
    # still-open markets are left open to re-probe next cycle (never false-mark).
    for row in open_rows:
        if row["id"] in mutated:
            continue
        if summary["ticker_probes"] >= max_ticker_probes or time.monotonic() > deadline:
            break
        result, kind = _probe_market(row.get("ticker") or "")
        summary["ticker_probes"] += 1
        if kind == "settled" and result:
            _settle_row(row, result, "ticker_probe", now)
            mutated[row["id"]] = row
            summary["settled_probe"] += 1
        elif kind == "void":
            _dead_mark_row(row, "settled_non_binary", now)
            mutated[row["id"]] = row
            summary["dead_marked"] += 1
        # kind in ("open", "error"): leave open.

    summary["persisted"] = _persist(mutated)
    summary["still_open"] = summary["open_before"] - len(mutated)
    logger.info(
        "shadow settlement: open=%d local=%d event=%d probe=%d dead=%d "
        "(fetches=%d probes=%d) still_open=%d",
        summary["open_before"], summary["settled_local"], summary["settled_event"],
        summary["settled_probe"], summary["dead_marked"], summary["event_fetches"],
        summary["ticker_probes"], summary["still_open"],
    )
    return summary


def resolve_shadow_trades_runtime() -> dict:
    """Zero-arg entry point for the _main_loop hook (bounded per-cycle caps).
    Called via run_in_executor + asyncio.wait_for in bot/main.py (Battle Scar #13)."""
    return resolve_shadow_trades(
        max_event_fetches=_SHADOW_SETTLE_MAX_EVENT_FETCHES,
        max_ticker_probes=_SHADOW_SETTLE_MAX_TICKER_PROBES,
        time_budget_sec=_SHADOW_SETTLE_TIME_BUDGET_SEC,
    )


def backfill() -> dict:
    """One-time bounded backfill of the existing open rows (larger caps)."""
    return resolve_shadow_trades(
        max_event_fetches=_BACKFILL_MAX_EVENT_FETCHES,
        max_ticker_probes=_BACKFILL_MAX_TICKER_PROBES,
        time_budget_sec=_BACKFILL_TIME_BUDGET_SEC,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = backfill()
    print(json.dumps(result, indent=2))
