"""Data loader for Strategy Lab.

Streams ``universe.jsonl`` (current + gzipped daily archives) over a date
window, plus loads ``clv.json`` keyed by ticker and ``decisions.jsonl``
keyed by ticker. Reuses helpers from ``tools.backtest`` and
``bot.calibration`` — single source of truth for jsonl-iteration and
ISO-timestamp parsing.

Canonical schema: see ``CLAUDE.md`` "Canonical Data Schema Reference".
``clv.json`` records use ``status`` ∈ {"open", "settled",
"counterfactual_open", "counterfactual_settled"}, ``market_result`` ∈
{"yes", "no", null}, ``clv_cents`` (signed), ``recorded_at`` (ISO 8601),
``skipped_by_gate``. The lab's tests forbid the suffix-_won anti-pattern
in any tools/strategy_lab/ source file.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.calibration import _parse_iso  # noqa: E402
from bot.config import BOT_STATE_DIR  # noqa: E402
from tools.backtest import _iter_jsonl_lines  # noqa: E402

UNIVERSE_FILE = BOT_STATE_DIR / "universe.jsonl"
ARCHIVE_DIR = BOT_STATE_DIR / "archive"
CLV_FILE = BOT_STATE_DIR / "clv.json"
DECISIONS_FILE = BOT_STATE_DIR / "decisions.jsonl"


def _resolve_window(days: int, today: date | None = None) -> tuple[date, date]:
    """Return (start, end) inclusive UTC dates for an N-day lookback."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    return start, today


def _universe_sources(start: date, end: date) -> list[Path]:
    """Daily archive files in window plus the current universe.jsonl.

    Mirrors ``tools/backtest.py:load_universe_snapshots`` source-collection.
    """
    sources: list[Path] = []
    cur = start
    while cur <= end:
        gz = ARCHIVE_DIR / f"universe-{cur.isoformat()}.jsonl.gz"
        if gz.exists():
            sources.append(gz)
        cur += timedelta(days=1)
    if UNIVERSE_FILE.exists():
        sources.append(UNIVERSE_FILE)
    return sources


def iter_universe(start: date, end: date) -> Iterator[dict]:
    """Yield raw universe rows whose ``ts`` date is in [start, end].

    Streams lazily from gzipped archives + current ``universe.jsonl``;
    tolerates malformed lines. Rows are returned as raw dicts (not
    ``Market``-converted) so candidates can write ``market.get(...)``.
    """
    for src in _universe_sources(start, end):
        for row in _iter_jsonl_lines(src):
            ts = _parse_iso(row.get("ts"))
            if ts is None or not (start <= ts.date() <= end):
                continue
            yield row


def load_clv_lookup(
    start: date,
    end: date,
    *,
    match_window_hours: float = 2.0,
) -> dict[str, list[dict]]:
    """Load ``clv.json`` and bucket records by ticker.

    Filtered to ``recorded_at`` within [start - match_window_hours,
    end + match_window_hours] so candidates near window edges can still
    find their matching settled CFs. Returns ``{ticker: [records]}``.
    """
    if not CLV_FILE.exists():
        return {}
    try:
        with open(CLV_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, list):
        return {}

    window = timedelta(hours=match_window_hours)
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) - window
    end_dt = datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc) + window

    lookup: dict[str, list[dict]] = defaultdict(list)
    for rec in data:
        if not isinstance(rec, dict):
            continue
        ticker = rec.get("ticker")
        if not ticker:
            continue
        ts = _parse_iso(rec.get("recorded_at"))
        if ts is None or not (start_dt <= ts <= end_dt):
            continue
        lookup[ticker].append(rec)
    return dict(lookup)


def _decisions_sources(start: date, end: date) -> list[Path]:
    """decisions.jsonl daily archives in window plus current."""
    sources: list[Path] = []
    cur = start
    while cur <= end:
        gz = ARCHIVE_DIR / f"decisions-{cur.isoformat()}.jsonl.gz"
        if gz.exists():
            sources.append(gz)
        cur += timedelta(days=1)
    if DECISIONS_FILE.exists():
        sources.append(DECISIONS_FILE)
    return sources


def load_decisions_by_ticker(
    start: date,
    end: date,
) -> dict[str, list[dict]]:
    """Return ``{ticker: [decisions]}`` for the window.

    Scope is the same N-day window as the universe stream — keeps memory
    bounded. If a future candidate needs longer history, switch to a
    streaming lookup.
    """
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for src in _decisions_sources(start, end):
        for row in _iter_jsonl_lines(src):
            ts = _parse_iso(row.get("ts"))
            if ts is None or not (start <= ts.date() <= end):
                continue
            ticker = row.get("ticker")
            if not ticker:
                continue
            by_ticker[ticker].append(row)
    return dict(by_ticker)


def load_window(
    days: int = 14,
    *,
    today: date | None = None,
    clv_match_window_hours: float = 2.0,
) -> tuple[Iterator[dict], dict[str, list[dict]], dict[str, list[dict]], date, date]:
    """One-shot loader for the driver.

    Returns ``(universe_iter, clv_lookup, decisions_by_ticker, start, end)``.
    The universe iterator is lazy — exhausted as the driver scans.
    """
    start, end = _resolve_window(days, today=today)
    clv_lookup = load_clv_lookup(start, end, match_window_hours=clv_match_window_hours)
    decisions_by_ticker = load_decisions_by_ticker(start, end)
    universe_iter = iter_universe(start, end)
    return universe_iter, clv_lookup, decisions_by_ticker, start, end
