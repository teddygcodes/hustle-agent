"""DiscoveryContext: pre-load all bot state once, expose streaming iterators for big files.

Heuristics consume `ctx.X` attributes — they never touch the filesystem directly. Big
files (universe.jsonl ~39MB, live_ticks.jsonl ~54MB, bot.log + rotated .1..5) are
exposed as factory callables that return a fresh iterator each call, so memory stays
bounded regardless of file size.

Files declared but not present on disk today (universe_archive/, order_microstructure.jsonl)
get empty defaults plus a load_warnings entry — heuristics that depend on them will skip
cleanly via main.py's _source_present check.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

# Repo root, derived portably: context.py lives at tools/discovery_agent/, so
# parents[2] is the repository root regardless of where it's checked out.
DEFAULT_REPO = Path(__file__).resolve().parents[2]


def _read_json(path: Path, default, warnings: list[str]):
    if not path.exists():
        warnings.append(f"missing: {path.name}")
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        warnings.append(f"malformed: {path.name} ({e})")
        return default


def _read_jsonl(path: Path, warnings: list[str]) -> list[dict]:
    if not path.exists():
        warnings.append(f"missing: {path.name}")
        return []
    out: list[dict] = []
    bad = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    if bad:
        warnings.append(f"malformed: {path.name} skipped {bad} line(s)")
    return out


def _stream_jsonl(path: Path) -> Callable[[], Iterator[dict]]:
    def factory() -> Iterator[dict]:
        if not path.exists():
            return
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    return factory


def _stream_log(logs_dir: Path) -> Callable[[], Iterator[str]]:
    """Walk bot.log + bot.log.1..bot.log.5 (whichever exist), uncompressed."""
    def factory() -> Iterator[str]:
        candidates = [logs_dir / "bot.log"] + [logs_dir / f"bot.log.{i}" for i in range(1, 6)]
        for p in candidates:
            if not p.exists():
                continue
            with p.open() as fh:
                for line in fh:
                    yield line.rstrip("\n")
    return factory


@dataclass
class DiscoveryContext:
    """All bot data loaded once. Heuristics read ctx.X — never touch the filesystem."""

    paper_trades: list[dict]
    trade_history: list[dict]
    live_journal: list[dict]
    positions: list[dict]
    bot_state: dict
    clv: list[dict]
    decisions: list[dict]
    predictions: list[dict]
    tracker_cadence: list[dict]
    strategy_audit: dict

    universe_archives: list[Path]
    order_microstructure: list[dict]

    universe_iter: Callable[[], Iterator[dict]]
    live_ticks_iter: Callable[[], Iterator[dict]]
    bot_log_iter: Callable[[], Iterator[str]]

    outcomes_db: sqlite3.Connection | None

    loaded_at: dt.datetime
    cutoff_days: int = 30
    load_warnings: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, repo: Path = DEFAULT_REPO, cutoff_days: int = 30) -> "DiscoveryContext":
        state = repo / "bot" / "state"
        logs = repo / "bot" / "logs"
        warnings: list[str] = []

        outcomes_db: sqlite3.Connection | None = None
        db_path = state / "outcomes.db"
        if db_path.exists():
            try:
                outcomes_db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            except sqlite3.OperationalError as e:
                warnings.append(f"outcomes.db: {e}")
        else:
            warnings.append("missing: outcomes.db")

        archive_dir = state / "universe_archive"
        if archive_dir.exists():
            archives = sorted(archive_dir.glob("*.json"))
        else:
            archives = []
            warnings.append("missing: universe_archive/ (directory does not exist)")

        om_path = state / "order_microstructure.jsonl"
        if om_path.exists():
            order_microstructure = _read_jsonl(om_path, warnings)
        else:
            order_microstructure = []
            warnings.append("missing: order_microstructure.jsonl")

        return cls(
            paper_trades=_read_json(state / "paper_trades.json", [], warnings),
            trade_history=_read_json(state / "trade_history.json", [], warnings),
            live_journal=_read_json(state / "live_journal.json", [], warnings),
            positions=_read_json(state / "positions.json", [], warnings),
            bot_state=_read_json(state / "bot_state.json", {}, warnings),
            clv=_read_json(state / "clv.json", [], warnings),
            decisions=_read_jsonl(state / "decisions.jsonl", warnings),
            predictions=_read_jsonl(state / "predictions.jsonl", warnings),
            tracker_cadence=_read_jsonl(state / "tracker_cadence.jsonl", warnings),
            strategy_audit=_read_json(state / "strategy_audit.json", {}, warnings),
            universe_archives=archives,
            order_microstructure=order_microstructure,
            universe_iter=_stream_jsonl(state / "universe.jsonl"),
            live_ticks_iter=_stream_jsonl(state / "live_ticks.jsonl"),
            bot_log_iter=_stream_log(logs),
            outcomes_db=outcomes_db,
            loaded_at=dt.datetime.now(dt.timezone.utc),
            cutoff_days=cutoff_days,
            load_warnings=warnings,
        )
