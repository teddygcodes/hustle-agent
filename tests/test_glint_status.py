from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import glint_status as glint  # noqa: E402


def _minimal_metrics(now: datetime) -> dict:
    return {
        "paper_trades": [],
        "settled": [],
        "active_positions": [],
        "paper_open": [],
        "bot_state": {
            "last_heartbeat": (now - timedelta(seconds=10)).isoformat(),
            "started_at": (now - timedelta(minutes=5)).isoformat(),
        },
        "resolved_today": [],
        "config": {
            "vig_stack_family_caps": {},
            "vig_stack_default_cap": 200.0,
            "paper_starting_balance": 10500.0,
        },
        "open_positions_count": 0,
        "paper_open_count": 0,
        "pnl_by_type": {},
        "settled_by_type": {},
        "wins_by_type": {},
        "total_pnl": 0.0,
        "settled_count": 0,
        "exposure": 0.0,
        "paper_exposure": 0.0,
        "bankroll": 10500.0,
        "open_tickers": [],
        "paper_open_tickers": [],
        "settled_trade_ids": [],
    }


def test_diff_when_no_baseline_emits_no_baseline_marker():
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    current = {"ts": now.isoformat(), "total_pnl": 1.0}
    md = glint.render_diff(None, current, now)
    assert "_No baseline yet" in md


def test_diff_computes_correct_deltas():
    last = {
        "ts": "2026-05-06T12:00:00+00:00",
        "total_pnl": 282.81,
        "settled_count": 249,
        "vig_stack_pnl": 317.41,
        "live_momentum_pnl": -34.60,
        "open_positions_count": 13,
        "exposure": 2096.0,
        "discovery_findings_new": 3,
        "open_tickers": ["A", "B"],
        "settled_trade_ids": ["T1"],
        "flag_ids": ["old_flag"],
    }
    current = {
        "ts": "2026-05-07T12:00:00+00:00",
        "total_pnl": 579.73,
        "settled_count": 257,
        "vig_stack_pnl": 614.33,
        "live_momentum_pnl": -34.60,
        "open_positions_count": 13,
        "exposure": 1894.0,
        "discovery_findings_new": 5,
        "open_tickers": ["B", "C"],
        "settled_trade_ids": ["T1", "T2", "T3"],
        "flag_ids": ["old_flag", "new_flag"],
    }
    md = glint.render_diff(last, current, datetime(2026, 5, 7, 12, tzinfo=timezone.utc))
    assert "+$296.92 / +8 settled" in md
    assert "Positions: 13 -> 13" in md
    assert "$2,096.00 -> $1,894.00 (-$202.00)" in md
    assert "Newly settled trade records: 2" in md
    assert "new_flag" in md


def test_anomaly_detector_live_momentum_zero_entries(tmp_path: Path):
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    paths = glint.paths_for(tmp_path)
    paths.decisions_file.parent.mkdir(parents=True, exist_ok=True)
    paths.decisions_file.write_text(json.dumps({
        "ts": (now - timedelta(hours=1)).isoformat(),
        "opp_type": "live_momentum",
        "decision": "reject",
        "reason": "sport_disabled",
    }) + "\n")
    paths.log_file.parent.mkdir(parents=True, exist_ok=True)
    paths.log_file.write_text("")
    metrics = _minimal_metrics(now)
    flags = glint.detect_anomalies(paths, metrics, now)
    assert any(f.id == "live_momentum_zero_entries_48h" and f.severity == "WARN" for f in flags)


def test_anomaly_detector_no_false_positive_on_normal_state(tmp_path: Path):
    now = datetime(2026, 5, 7, 12, tzinfo=timezone.utc)
    paths = glint.paths_for(tmp_path)
    paths.decisions_file.parent.mkdir(parents=True, exist_ok=True)
    paths.decisions_file.write_text("")
    paths.log_file.parent.mkdir(parents=True, exist_ok=True)
    paths.log_file.write_text("[2026-05-07 08:00:00] INFO clean\n")
    metrics = _minimal_metrics(now)
    metrics["paper_trades"] = [{
        "type": "live_momentum",
        "timestamp": (now - timedelta(hours=1)).isoformat(),
        "status": "open",
    }]
    flags = glint.detect_anomalies(paths, metrics, now)
    assert [f for f in flags if f.severity in {"WARN", "CRITICAL"}] == []


def test_watchlist_parser_extracts_triggers_from_claude_md():
    triggers = glint.extract_watchlist_triggers((REPO_ROOT / "CLAUDE.md").read_text())
    assert len(triggers) >= 10
    assert all("line" in t and "session" in t and "text" in t for t in triggers)


def test_watchlist_evaluator_threshold_check():
    triggers = [{
        "line": 12,
        "session": "Session synthetic",
        "text": "Watch-list trigger: when challenger CFs accumulate >=30 rows and >=5 leader-loss rows",
    }]
    data = {
        "challenger_cf_n": 30,
        "challenger_leader_loss_n": 5,
        "lm_ee_count": 0,
        "lm_per_trade_pnl": 0.0,
        "wta_cf_n": 0,
        "wta_mean_clv": None,
        "no_leader_wta_n": 0,
        "no_leader_wta_mean_clv": None,
        "post_apr23_lm_settled": 0,
        "lm_sport_counts": {},
        "httpx_errors_since_restart": 0,
        "shutdown_skip_info_since_restart": 0,
    }
    out = glint.evaluate_watchlist_triggers(triggers, data)
    assert out[0]["status"] == "TRIGGERED"


def test_watchlist_evaluator_unparseable_trigger_emits_manual_check():
    out = glint.evaluate_watchlist_triggers(
        [{"line": 7, "session": "Session X", "text": "Watch-list trigger: re-open only if vibes and prose line up"}],
        {},
    )
    assert out[0]["status"] == "MANUAL_CHECK_REQUIRED"
    assert "CLAUDE.md L7" in out[0]["detail"]


def test_daily_report_section_extraction():
    md = """# Daily Report

# 1. Health pulse

| Axis | Value | Status |
|---|---|:---:|
| Bot alive | PID 1 alive | ✅ |

---

# 2. Scanner activity

body

# 4. Trade activity

Resolved trades: **2**
"""
    health = glint.extract_markdown_section(md, "1. Health pulse")
    pnl = glint.extract_markdown_section(md, "4. Trade activity")
    assert "Bot alive" in health
    assert "Scanner activity" not in health
    assert "Resolved trades" in pnl


def test_state_persistence_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "glint_status_last.json"
    target.write_text(json.dumps({"ts": "old"}))
    original_save = glint.state_io.save_json

    def fail_save(_path, _data):
        raise OSError("disk full")

    monkeypatch.setattr(glint.state_io, "save_json", fail_save)
    assert glint.safe_persist_status(target, {"ts": "new"}) is False
    assert json.loads(target.read_text())["ts"] == "old"

    monkeypatch.setattr(glint.state_io, "save_json", original_save)
    assert glint.safe_persist_status(target, {"ts": "new"}) is True
    assert json.loads(target.read_text())["ts"] == "new"


def test_consolidator_completes_in_under_2s():
    start = time.perf_counter()
    md = glint.build_snapshot(REPO_ROOT, now_utc=datetime.now(timezone.utc), persist=False)
    elapsed = time.perf_counter() - start
    assert "# Glint Status" in md
    assert "## 9. Flags" in md
    assert elapsed < 2.0
