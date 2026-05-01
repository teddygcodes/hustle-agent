"""DiscoveryContext loader tests — schema-aware, fault-tolerant, streaming."""

from __future__ import annotations

import json
import sqlite3
import tracemalloc
from pathlib import Path

from tools.discovery_agent.context import DiscoveryContext


def _bot_state_dir(root: Path) -> Path:
    d = root / "bot" / "state"
    d.mkdir(parents=True, exist_ok=True)
    (root / "bot" / "logs").mkdir(parents=True, exist_ok=True)
    return d


def _populate_full(root: Path) -> Path:
    state = _bot_state_dir(root)
    (state / "paper_trades.json").write_text(json.dumps([{"id": "P1", "type": "vig_stack"}]))
    (state / "trade_history.json").write_text(json.dumps([{"id": "T1", "opp_type": "vig_stack"}]))
    (state / "live_journal.json").write_text(json.dumps([{"event": "tick", "sport": "mlb"}]))
    (state / "positions.json").write_text(json.dumps([{"ticker": "X", "opp_type": "y"}]))
    (state / "bot_state.json").write_text(json.dumps({"running": True}))
    (state / "clv.json").write_text(json.dumps([{"trade_id": "T1"}]))
    (state / "decisions.jsonl").write_text(json.dumps({"ts": "2026-04-30T00:00:00+00:00", "opp_type": "vig_stack_futures"}) + "\n")
    (state / "predictions.jsonl").write_text(json.dumps({"ts": "2026-04-30T00:00:00+00:00", "opp_type": "vig_stack"}) + "\n")
    (state / "tracker_cadence.jsonl").write_text(json.dumps({"ts": "2026-04-30T00:00:00+00:00", "called_from": "main"}) + "\n")
    (state / "strategy_audit.json").write_text(json.dumps({"_meta": {}, "strategies": {}}))
    (state / "universe.jsonl").write_text(json.dumps({"ticker": "U1"}) + "\n")
    (state / "live_ticks.jsonl").write_text(json.dumps({"ts": "2026-04-30T00:00:00+00:00", "ticker": "T1"}) + "\n")
    conn = sqlite3.connect(state / "outcomes.db")
    conn.execute("CREATE TABLE IF NOT EXISTS outcomes (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    (root / "bot" / "logs" / "bot.log").write_text("INFO line 1\n")
    return root


def test_load_all_sources_present(tmp_path: Path):
    _populate_full(tmp_path)
    ctx = DiscoveryContext.load(repo=tmp_path)
    assert len(ctx.paper_trades) == 1
    assert len(ctx.trade_history) == 1
    assert len(ctx.live_journal) == 1
    assert len(ctx.positions) == 1
    assert ctx.bot_state["running"] is True
    assert len(ctx.clv) == 1
    assert len(ctx.decisions) == 1
    assert len(ctx.predictions) == 1
    assert len(ctx.tracker_cadence) == 1
    assert ctx.strategy_audit["_meta"] == {}
    assert callable(ctx.universe_iter)
    assert callable(ctx.live_ticks_iter)
    assert callable(ctx.bot_log_iter)
    assert ctx.outcomes_db is not None


def test_load_missing_files_empty_containers(tmp_path: Path):
    _bot_state_dir(tmp_path)
    ctx = DiscoveryContext.load(repo=tmp_path)
    assert ctx.paper_trades == []
    assert ctx.bot_state == {}
    assert ctx.strategy_audit == {}
    assert ctx.outcomes_db is None
    assert ctx.universe_archives == []
    assert ctx.order_microstructure == []
    warning_text = "\n".join(ctx.load_warnings)
    assert "paper_trades.json" in warning_text
    assert "universe_archive" in warning_text
    assert "order_microstructure.jsonl" in warning_text


def test_load_malformed_jsonl_skips_lines(tmp_path: Path):
    state = _bot_state_dir(tmp_path)
    (state / "decisions.jsonl").write_text(
        json.dumps({"ts": "2026-04-30T00:00:00+00:00", "opp_type": "x"}) + "\n"
        + "not-json-at-all\n"
        + json.dumps({"ts": "2026-04-30T01:00:00+00:00", "opp_type": "y"}) + "\n"
    )
    ctx = DiscoveryContext.load(repo=tmp_path)
    assert len(ctx.decisions) == 2
    assert any("decisions.jsonl" in w and "malformed" in w for w in ctx.load_warnings)


def test_streaming_iterator_factory_returns_fresh_each_call(tmp_path: Path):
    state = _bot_state_dir(tmp_path)
    (state / "live_ticks.jsonl").write_text(
        "\n".join(json.dumps({"i": i}) for i in range(5)) + "\n"
    )
    ctx = DiscoveryContext.load(repo=tmp_path)
    first = list(ctx.live_ticks_iter())
    second = list(ctx.live_ticks_iter())
    assert [r["i"] for r in first] == [0, 1, 2, 3, 4]
    assert [r["i"] for r in second] == [0, 1, 2, 3, 4]


def test_streaming_iterator_memory_safe(tmp_path: Path):
    """Write a fat live_ticks.jsonl, iterate one record at a time, peak memory must stay small."""
    state = _bot_state_dir(tmp_path)
    target = state / "live_ticks.jsonl"
    with target.open("w") as fh:
        for i in range(100_000):
            fh.write(json.dumps({"i": i, "pad": "x" * 150}) + "\n")
    ctx = DiscoveryContext.load(repo=tmp_path)

    tracemalloc.start()
    count = 0
    for _ in ctx.live_ticks_iter():
        count += 1
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 100_000
    assert peak < 50 * 1024 * 1024, f"streaming peak too high: {peak/1024/1024:.1f}MB"


def test_bot_log_iter_walks_rotated_files(tmp_path: Path):
    _bot_state_dir(tmp_path)
    logs = tmp_path / "bot" / "logs"
    (logs / "bot.log").write_text("current-1\ncurrent-2\n")
    (logs / "bot.log.1").write_text("rotated-1\n")
    (logs / "bot.log.2").write_text("rotated-2\n")
    ctx = DiscoveryContext.load(repo=tmp_path)
    lines = list(ctx.bot_log_iter())
    assert lines[:2] == ["current-1", "current-2"]
    assert "rotated-1" in lines and "rotated-2" in lines
