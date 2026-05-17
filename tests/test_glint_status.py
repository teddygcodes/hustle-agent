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
    triggers = glint.extract_watchlist_triggers((REPO_ROOT / "CLAUDE-sessions.md").read_text())
    assert len(triggers) >= 10
    assert all("line" in t and "session" in t and "text" in t for t in triggers)


def test_watchlist_evaluator_threshold_check():
    triggers = [{
        "line": 12,
        "session": "Session synthetic",
        "text": "Watch-list trigger: when challenger CFs accumulate n>=600 combined and n_no_won>=100",
    }]
    data = {
        "challenger_cf_n": 600,
        "challenger_leader_loss_n": 100,
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
    assert "CLAUDE-sessions.md L7" in out[0]["detail"]


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
    # Session 91: §9 Flags deleted; §10 Strategy Candidates renumbered to §9.
    assert "## 9. Strategy Candidates" in md
    assert "## 9. Flags" not in md
    assert elapsed < 2.0


def test_session30_followup_post_session65_threshold():
    # Session 65: original n>=30 + n_no_won>=5 trigger was evaluated by Session 61
    # (Outcome B). New bar is n>=600 combined AND n_no_won>=100. At the Session 61
    # baseline (n=398/leader-loss=122), the trigger now shows NOT_YET_TRIGGERED.
    triggers = [{
        "line": 2153,
        "session": "Session 30-followup",
        "text": (
            "Watch-list trigger (Session 65 update): re-evaluate per-circuit when "
            "challenger CFs accumulate n>=600 combined AND n_no_won>=100"
        ),
    }]
    base = {
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
    cases = [
        # (n, losses, expected_status)
        (398, 122, "NOT_YET_TRIGGERED"),  # Session 61 baseline; below new bar
        (30, 5, "NOT_YET_TRIGGERED"),     # Old bar exactly; locks against re-regress
        (599, 100, "NOT_YET_TRIGGERED"),  # Just below new n bar
        (600, 99, "NOT_YET_TRIGGERED"),   # n meets, losses below
        (600, 100, "TRIGGERED"),          # New bar exactly
        (1000, 250, "TRIGGERED"),         # Well above
    ]
    for n, losses, expected in cases:
        out = glint.evaluate_watchlist_triggers(
            triggers,
            {**base, "challenger_cf_n": n, "challenger_leader_loss_n": losses},
        )
        assert out[0]["status"] == expected, (
            f"n={n}/losses={losses}: expected {expected}, got {out[0]['status']}"
        )


def test_count_discovery_findings_new_count_matches_fingerprints(tmp_path: Path):
    # Session 65: when discovery_report's summary line and NEW findings list
    # disagree internally, count_discovery_findings should report the count
    # derived from fingerprints (same source as §6 body) so the verdict line
    # and §6 body show the same NEW count downstream.
    paths = glint.paths_for(tmp_path)
    paths.discovery_dir.mkdir(parents=True, exist_ok=True)
    report = (
        "# Discovery Report 2026-05-07\n\n"
        "Findings: 5 NEW, 1 STABLE, 0 RESOLVED.\n\n"
        "## NEW findings\n\n"
        "- fingerprint `fp_synthetic_a` -- something\n"
        "- fingerprint `fp_synthetic_b` -- something else\n\n"
        "## STABLE findings\n\n"
        "(stable section)\n"
    )
    findings_jsonl = (
        json.dumps({"fingerprint": "fp_synthetic_a", "severity": "low", "title": "A", "summary": "first"}) + "\n"
        + json.dumps({"fingerprint": "fp_synthetic_b", "severity": "low", "title": "B", "summary": "second"}) + "\n"
    )
    today = "2026-05-07"
    (paths.discovery_dir / f"discovery_report_{today}.md").write_text(report)
    (paths.discovery_dir / f"discovery_findings_{today}.jsonl").write_text(findings_jsonl)
    now = datetime(2026, 5, 7, 16, tzinfo=timezone.utc)
    discovery = glint.count_discovery_findings(paths, now)
    # 'new' count derives from fingerprints (2), not the summary regex (5)
    assert discovery["new"] == 2
    assert len(discovery["new_fingerprints"]) == 2
    # Verdict line and §6 body now agree on the NEW count
    metrics = _minimal_metrics(now)
    verdict = glint.render_verdict(metrics, [], discovery, [], now)
    body = glint.render_discovery_section(discovery)
    assert "2 NEW discovery findings" in verdict
    assert "**2 NEW**" in body


def test_verdict_includes_strategy_candidate_counts():
    now = datetime(2026, 5, 7, 16, tzinfo=timezone.utc)
    metrics = _minimal_metrics(now)
    discovery = {"new": 0}
    strategy_candidates = {"active": 4, "high": 1, "notable": 2, "info": 1, "resolved": 3}

    verdict = glint.render_verdict(metrics, [], discovery, [], now, strategy_candidates)

    assert "4 strategy candidates active (H 1 / N 2 / I 1), 3 resolved 14d" in verdict


def test_baseline_and_diff_track_strategy_candidate_counts():
    now = datetime(2026, 5, 7, 16, tzinfo=timezone.utc)
    metrics = _minimal_metrics(now)
    discovery = {"new": 0}
    current = glint.build_baseline(
        now,
        metrics,
        discovery,
        [],
        {"active": 6, "high": 2, "notable": 3, "info": 1, "resolved": 4},
    )
    last = {
        "ts": "2026-05-07T15:00:00+00:00",
        "total_pnl": 0,
        "settled_count": 0,
        "vig_stack_pnl": 0,
        "live_momentum_pnl": 0,
        "open_positions_count": 0,
        "exposure": 0,
        "discovery_findings_new": 0,
        "strategy_candidates_active": 5,
        "flag_ids": [],
        "open_tickers": [],
        "settled_trade_ids": [],
    }

    md = glint.render_diff(last, current, now)

    assert current["strategy_candidates_active"] == 6
    assert "Strategy candidates active: 5 -> 6 (+1; H 2 / N 3 / I 1, 4 resolved 14d)" in md


def test_glint_strategy_candidates_section_uses_shared_renderer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(glint.helpers, "render_strategy_candidates", lambda **_kwargs: "SHARED BODY")

    md = glint.render_strategy_candidates_section(datetime(2026, 5, 7, 16, tzinfo=timezone.utc))

    # Session 91 sub-feature 6: §10 → §9 after Flags deletion.
    assert md == "## 9. Strategy Candidates\n\nSHARED BODY"


def test_render_health_section_always_shows_generated_timestamp_and_age(tmp_path: Path):
    """Session 83: §3 must surface generated_at + age-in-hours in its header
    regardless of the 12h stale_note threshold. The data inside §3 is a snapshot
    of the bot's state at generated_at, NOT current — operators must see the
    age unambiguously even when the report is fresh-ish (e.g. 5.6h old at 09:00
    ET reading the 03:15 ET nightly)."""
    report_path = tmp_path / "daily_report_2026-05-08.md"
    report_path.write_text(
        "Generated: 2026-05-08T07:15:00+00:00\n"
        "\n"
        "# 1. Health pulse\n"
        "\n"
        "| Axis | Value | Status |\n"
        "|---|---|:---:|\n"
        "| Bot alive | lock mtime fresh | ✅ |\n"
    )
    daily = glint.DailyReport(
        path=report_path,
        text=report_path.read_text(),
        generated_at=datetime(2026, 5, 8, 7, 15, tzinfo=timezone.utc),
        report_date="2026-05-08",
        stale_note="",  # under 12h threshold — would have been silent before fix
    )
    now_utc = datetime(2026, 5, 8, 12, 51, tzinfo=timezone.utc)  # ~5.6h after generation

    md = glint.render_health_section(daily, now_utc)

    # Header must show generated_at in ET + age in hours
    assert "Generated:" in md
    assert "5.6h ago" in md
    # And the explicit warning that values are point-in-time, not now
    assert "reflect bot state at generation time, not now" in md
    # Existing health-pulse section must still render
    assert "Bot alive" in md


def test_render_health_section_handles_no_generated_at_gracefully(tmp_path: Path):
    """Session 83: when generated_at is missing (e.g. malformed daily report),
    the section must still render without raising."""
    report_path = tmp_path / "daily_report_2026-05-08.md"
    report_path.write_text("# 1. Health pulse\n\n_no axes_\n")
    daily = glint.DailyReport(
        path=report_path,
        text=report_path.read_text(),
        generated_at=None,
        report_date="2026-05-08",
        stale_note="[generated timestamp missing]",
    )
    now_utc = datetime(2026, 5, 8, 12, 51, tzinfo=timezone.utc)

    md = glint.render_health_section(daily, now_utc)

    # Stale_note still surfaces; new Generated: line is suppressed when unknown
    assert "[generated timestamp missing]" in md
    assert "Generated:" not in md


# ---------------------------------------------------------------------------
# Session 91 sub-feature 2: Live bot vitals header (§1)
# ---------------------------------------------------------------------------

def test_render_bot_vitals_alive():
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    state = {
        "running": True,
        "started_at": (now - timedelta(hours=2, minutes=14)).isoformat(),
        "last_heartbeat": (now - timedelta(seconds=12)).isoformat(),
        "last_scan": (now - timedelta(minutes=4)).isoformat(),
        "scans_today": 17,
    }
    # Use our own PID — it's guaranteed to be alive when the test runs.
    import os as _os
    out = glint.render_bot_vitals(state, lock_pid=_os.getpid(), now=now)
    assert "PID" in out and str(_os.getpid()) in out
    assert "uptime 2h 14m" in out
    assert "heartbeat 12s" in out
    assert "scans_today 17" in out
    assert "last scan 4m" in out
    assert "DEAD" not in out


def test_render_bot_vitals_dead_stale_heartbeat():
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    state = {
        "running": True,
        "last_heartbeat": (now - timedelta(minutes=12)).isoformat(),
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "last_scan": (now - timedelta(minutes=12)).isoformat(),
        "scans_today": 5,
    }
    import os as _os
    out = glint.render_bot_vitals(state, lock_pid=_os.getpid(), now=now)
    assert "DEAD" in out
    assert "12m" in out


def test_render_bot_vitals_dead_no_pid():
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    state = {"running": False, "last_heartbeat": (now - timedelta(seconds=20)).isoformat()}
    out = glint.render_bot_vitals(state, lock_pid=None, now=now)
    assert "DEAD" in out and "lock missing" in out


def test_render_bot_vitals_dead_pid_not_running():
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    state = {
        "running": True,
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "last_heartbeat": (now - timedelta(seconds=10)).isoformat(),
        "last_scan": (now - timedelta(seconds=30)).isoformat(),
        "scans_today": 12,
    }
    # PID 1 is init/launchd on macOS — exists but not ours. We treat
    # PermissionError as "alive" so this would render alive. Use a sentinel
    # PID guaranteed not to exist (max PID + 1 territory).
    out = glint.render_bot_vitals(state, lock_pid=2_147_483_646, now=now)
    assert "DEAD" in out
    assert "not running" in out


# ---------------------------------------------------------------------------
# Session 91 sub-feature 5: §3 staleness collapse (12h threshold)
# ---------------------------------------------------------------------------

def test_render_health_section_fresh_renders_full(tmp_path: Path):
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    report_path = tmp_path / "daily_report_2026-05-09.md"
    report_path.write_text(
        "Generated: 2026-05-09T11:00:00+00:00\n"
        "\n"
        "# 1. Health pulse\n"
        "\n"
        "All healthy.\n"
    )
    daily = glint.DailyReport(
        path=report_path,
        text=report_path.read_text(),
        generated_at=datetime(2026, 5, 9, 11, tzinfo=timezone.utc),
        report_date="2026-05-09",
        stale_note="",
    )
    md = glint.render_health_section(daily, now)
    assert "All healthy." in md
    assert "too stale to show as live" not in md


def test_render_health_section_stale_collapses(tmp_path: Path):
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    generated = now - timedelta(hours=21, minutes=24)
    report_path = tmp_path / "daily_report_2026-05-08.md"
    report_path.write_text(
        f"Generated: {generated.isoformat()}\n"
        "\n"
        "# 1. Health pulse\n"
        "\n"
        "DETAIL_BODY_SHOULD_NOT_RENDER\n"
    )
    daily = glint.DailyReport(
        path=report_path,
        text=report_path.read_text(),
        generated_at=generated,
        report_date="2026-05-08",
        stale_note="[stale: 21.4h ago]",
    )
    md = glint.render_health_section(daily, now)
    assert "## 3. Health Pulse" in md
    assert "too stale to show as live" in md
    assert "21." in md  # age shown
    assert "DETAIL_BODY_SHOULD_NOT_RENDER" not in md  # excerpt body suppressed


# ---------------------------------------------------------------------------
# Session 91 sub-feature 6: §9 merge into §7 + delete §9
# ---------------------------------------------------------------------------

def test_section_7_includes_daily_report_stale_after_merge(tmp_path: Path):
    daily = glint.DailyReport(
        path=tmp_path / "x.md",
        text="",
        generated_at=None,
        report_date="2026-05-08",
        stale_note="[stale: 21.4h ago]",
    )
    md = glint.render_anomalies_watchlist([], [], daily)
    # §7 uses the existing severity/message format; the daily_report_stale flag
    # is conveyed by message content + Session 35 ref, not the flag-id token.
    assert "Latest daily report is" in md
    assert "21.4h" in md
    assert "Session 35" in md


def test_section_7_includes_watchlist_summary_flags(tmp_path: Path):
    watch = [
        {"status": "MANUAL_CHECK_REQUIRED", "session": "S19", "line": 909, "detail": "x"},
        {"status": "TRIGGERED", "session": "S40", "line": 100, "detail": "y"},
    ]
    daily = glint.DailyReport(path=None, text="", generated_at=None, report_date="", stale_note="")
    md = glint.render_anomalies_watchlist([], watch, daily)
    assert "watch-list triggers require manual evaluation" in md
    assert "watch-list triggers are currently triggered" in md
    # Each underlying entry still surfaces in the per-entry list below.
    assert "MANUAL_CHECK_REQUIRED: S19 L909" in md
    assert "TRIGGERED: S40 L100" in md


def test_render_flags_section_no_longer_exists():
    # Session 91 sub-feature 6: render_flags_section was deleted; confirm the
    # symbol is gone so future regressions can't accidentally reintroduce it.
    assert not hasattr(glint, "render_flags_section")


# ---------------------------------------------------------------------------
# Session 91 sub-feature 4: Settlements next 24h (§5)
# ---------------------------------------------------------------------------

def test_parse_kalshi_settlement_weather():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    result = glint._parse_kalshi_settlement("KXHIGHCHI-26MAY09-T63", now=now)
    assert result is not None
    # End-of-day ET on 2026-05-09 = 03:59:59 UTC on 2026-05-10 (EDT, UTC-4).
    assert result.year == 2026 and result.month == 5
    assert result.day in (9, 10)


def test_parse_kalshi_settlement_index():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    result = glint._parse_kalshi_settlement("KXINXU-26MAY09-T5000", now=now)
    assert result is not None


def test_parse_kalshi_settlement_unknown_returns_none():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    # Live game ticker — settlement-time heuristic too noisy for game tickers.
    assert glint._parse_kalshi_settlement("KXMLBGAME-26MAY082210ATLLAD-LAD", now=now) is None
    # Truly unknown format.
    assert glint._parse_kalshi_settlement("UNKNOWN-FORMAT", now=now) is None
    # Empty string.
    assert glint._parse_kalshi_settlement("", now=now) is None


def test_parse_kalshi_settlement_invalid_month():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    assert glint._parse_kalshi_settlement("KXHIGH-26ZZZ09-T1", now=now) is None


def test_render_settlements_24h_summary():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    positions = [
        {"ticker": "KXHIGHCHI-26MAY09-T63", "filled": 47, "status": "filled", "cost": 199.79},
        {"ticker": "KXHIGHAUS-26MAY09-T80", "filled": 100, "status": "filled", "cost": 90.00},
        {"ticker": "KXHIGHMIA-26MAY15-T85", "filled": 50, "status": "filled", "cost": 45.00},  # outside 24h
        {"ticker": "KXHIGHCHI-26MAY09-T63", "filled": 0, "status": "exited", "cost": 0},  # filtered
        {"ticker": "KXMLBGAME-26MAY092210ATLLAD-LAD", "filled": 5, "status": "filled", "cost": 25.00},  # game ticker → ignored
    ]
    out = glint.render_settlements_24h(positions, now)
    assert "2 positions" in out
    assert "$289.79" in out
    assert "Next:" in out
    # Both KXHIGHCHI and KXHIGHAUS settle at 23:59:59 ET on the same day; sort
    # is stable so the first one appended wins as "Next".
    assert "KXHIGHCHI-26MAY09-T63" in out


def test_render_settlements_24h_empty_returns_empty_string():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    assert glint.render_settlements_24h([], now) == ""
    # Only out-of-window positions:
    assert glint.render_settlements_24h(
        [{"ticker": "KXHIGHMIA-26MAY30-T85", "filled": 1, "status": "filled", "cost": 10.0}],
        now,
    ) == ""


# ---------------------------------------------------------------------------
# Session 91 sub-feature 1: Watch-list auto-resolver
# ---------------------------------------------------------------------------

def test_extract_session_dates_parses_header_format():
    text = (
        "### ☑ Session 1 — Settlement + pattern pipeline (Apr 20)\n"
        "body...\n"
        "### ☑ Session 38a — re-enable atp main tour (Apr 29, shipped)\n"
        "more body...\n"
    )
    out = glint._extract_session_dates(text, current_year=2026)
    assert "Session 1" in out
    assert out["Session 1"].month == 4 and out["Session 1"].day == 20
    assert "Session 38a" in out
    assert out["Session 38a"].month == 4 and out["Session 38a"].day == 29


def test_resolved_key_stable_under_text_changes():
    a = {"session": "Session 19", "line": 909, "text": "trigger A"}
    b = {"session": "Session 19", "line": 909, "text": "trigger A (whitespace edit)"}
    assert glint._resolved_key(a) == glint._resolved_key(b)
    # Different line -> different key.
    c = {"session": "Session 19", "line": 910, "text": "trigger A"}
    assert glint._resolved_key(a) != glint._resolved_key(c)


def test_apply_watchlist_resolution_filters_resolved_entries():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    triggers = [
        {"session": "Session 19", "line": 909, "text": "x", "status": "MANUAL_CHECK_REQUIRED"},
        {"session": "Session 90", "line": 100, "text": "y", "status": "MANUAL_CHECK_REQUIRED"},
    ]
    resolved = {
        "Session_19_L909": {
            "resolved_at": (now - timedelta(days=1)).isoformat(),
            "reason": "auto_time_based_30d",
            "trigger_text_snippet": "x",
            "unresolved_count_24h": 0,
        }
    }
    filtered, updated = glint._apply_watchlist_resolution(triggers, resolved, now)
    assert len(filtered) == 1
    assert filtered[0]["session"] == "Session 90"
    assert "Session_19_L909" in updated  # still resolved


def test_apply_watchlist_resolution_unresolves_on_fresh_trigger():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    triggers = [
        {"session": "Session 19", "line": 909, "text": "x", "status": "TRIGGERED"},
    ]
    resolved = {
        "Session_19_L909": {
            "resolved_at": (now - timedelta(days=1)).isoformat(),
            "reason": "auto_time_based_30d",
            "trigger_text_snippet": "x",
            "unresolved_count_24h": 0,
        }
    }
    filtered, updated = glint._apply_watchlist_resolution(triggers, resolved, now)
    assert len(filtered) == 1  # un-resolved entry surfaces this scan
    assert "Session_19_L909" not in updated  # removed from resolved set
    # Thrash counter is recorded under the _RECENT suffix.
    recent = updated.get("Session_19_L909_RECENT")
    assert recent is not None
    assert recent["unresolved_count_24h"] == 1


def test_apply_watchlist_resolution_thrash_protection():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    triggers = [{"session": "Session 19", "line": 909, "text": "x", "status": "MANUAL_CHECK_REQUIRED"}]
    resolved = {
        "Session_19_L909": {
            "resolved_at": (now - timedelta(hours=1)).isoformat(),
            "reason": "auto_time_based_30d",
            "trigger_text_snippet": "x",
            "unresolved_count_24h": 3,  # over threshold (>2)
            "last_unresolved_at": (now - timedelta(hours=2)).isoformat(),
        }
    }
    filtered, _updated = glint._apply_watchlist_resolution(triggers, resolved, now)
    assert len(filtered) == 1  # keep visible despite resolved entry — thrash protection


def test_apply_watchlist_resolution_manual_axis_ruled_out_bypasses_reversibility():
    """S147: ``manual_axis_ruled_out: True`` keeps the entry resolved even when
    the trigger is currently firing. Cohort growth past threshold does not
    re-open a semantic question the operator has already closed."""
    now = datetime(2026, 5, 17, 12, tzinfo=timezone.utc)
    triggers = [
        {"session": "Session 41", "line": 2381, "text": "x", "status": "TRIGGERED"},
    ]
    resolved = {
        "Session_41_L2381": {
            "resolved_at": (now - timedelta(hours=1)).isoformat(),
            "reason": "manual_axis_ruled_out",
            "resolved_by": "S147",
            "rationale": "TP/SL Pattern C in S41+S129; substrate gap per S134",
            "trigger_text_snippet": "post-Apr-23 settled live_momentum n>=60",
            "unresolved_count_24h": 0,
            "manual_axis_ruled_out": True,
        }
    }
    filtered, updated = glint._apply_watchlist_resolution(triggers, resolved, now)
    assert filtered == []  # bypassed — trigger does NOT surface
    assert "Session_41_L2381" in updated  # entry preserved (not popped)
    assert "Session_41_L2381_RECENT" not in updated  # no thrash marker created


def test_maybe_auto_resolve_resolves_old_entries(tmp_path: Path):
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    claude_text = (
        "### ☑ Session 1 — Settlement + pattern pipeline (Apr 1)\n"  # 38d old
        "Watch-list trigger: when X happens.\n"
        "### ☑ Session 90 — re-entry breaker (May 9)\n"  # today
        "Watch-list trigger: when Y happens.\n"
    )
    triggers = [
        {"session": "Session 1", "line": 2, "text": "Watch-list trigger: when X", "status": "MANUAL_CHECK_REQUIRED"},
        {"session": "Session 90", "line": 4, "text": "Watch-list trigger: when Y", "status": "MANUAL_CHECK_REQUIRED"},
    ]
    updated = glint._maybe_auto_resolve(triggers, {}, claude_text, now)
    assert "Session_1_L2" in updated  # 38d old → resolved
    assert "Session_90_L4" not in updated  # today → still visible
    assert updated["Session_1_L2"]["reason"] == "auto_time_based_30d"


def test_maybe_auto_resolve_skips_thrashed_keys(tmp_path: Path):
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    claude_text = "### ☑ Session 1 — old (Apr 1)\nWatch-list trigger: x.\n"
    triggers = [{"session": "Session 1", "line": 2, "text": "Watch-list trigger: x", "status": "MANUAL_CHECK_REQUIRED"}]
    resolved = {
        "Session_1_L2_RECENT": {"unresolved_count_24h": 5, "reason": "fresh_trigger_fired"},
    }
    updated = glint._maybe_auto_resolve(triggers, resolved, claude_text, now)
    assert "Session_1_L2" not in updated  # thrash → don't auto-resolve
    assert "Session_1_L2_RECENT" in updated  # thrash record preserved


def test_load_save_watchlist_resolved_round_trip(tmp_path: Path):
    paths = glint.paths_for(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"Session_19_L909": {"resolved_at": "2026-05-09T00:00:00+00:00", "reason": "auto_time_based_30d"}}
    glint._save_watchlist_resolved(paths, payload)
    out = glint._load_watchlist_resolved(paths)
    assert out == payload


def test_load_watchlist_resolved_returns_empty_when_missing(tmp_path: Path):
    paths = glint.paths_for(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    assert glint._load_watchlist_resolved(paths) == {}


# ---------------------------------------------------------------------------
# Session 91 sub-feature 3: Active observations registry (§10)
# ---------------------------------------------------------------------------

def test_render_active_observations_empty():
    md = glint.render_active_observations([], datetime.now(timezone.utc))
    assert "## 10. Active Observations" in md
    assert "No active observations" in md


def test_render_active_observations_with_entries():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    obs = [{
        "session": 90,
        "shipped_at": (now - timedelta(days=1)).isoformat(),
        "description": "circuit breaker test",
        "observation_window_days": 14,
        "metrics": [{"name": "reentry_blocked", "current_value": 0, "expectation": ">=1 per attempt"}],
    }]
    md = glint.render_active_observations(obs, now)
    assert "Session 90" in md
    assert "circuit breaker test" in md
    assert "reentry_blocked" in md
    assert "13d remaining" in md


def test_render_active_observations_expired_window_excluded():
    now = datetime(2026, 5, 9, 12, tzinfo=timezone.utc)
    obs = [{
        "session": 50,
        "shipped_at": (now - timedelta(days=30)).isoformat(),
        "description": "old ship",
        "observation_window_days": 14,
        "metrics": [],
    }]
    md = glint.render_active_observations(obs, now)
    assert "old ship" not in md
    assert "No active observations within their watch window" in md


def test_load_active_observations_seed_present():
    # The seeded bot/state/active_observations.json should load with at least
    # the Session 90 circuit breaker entry.
    paths = glint.paths_for(REPO_ROOT)
    obs = glint._load_active_observations(paths)
    assert isinstance(obs, list)
    sessions = {o.get("session") for o in obs if isinstance(o, dict)}
    assert 90 in sessions, f"expected Session 90 entry in active_observations.json; got sessions={sessions}"


def test_load_active_observations_returns_empty_when_missing(tmp_path: Path):
    paths = glint.paths_for(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    assert glint._load_active_observations(paths) == []


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_compute_active_observations_overrides_stale_manual_values(tmp_path: Path):
    import gzip

    paths = glint.paths_for(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    _write_json(paths.paper_trades_file, [])
    _write_json(paths.positions_file, [])
    _write_json(paths.bot_state_file, {})
    paths.config_py.parent.mkdir(parents=True, exist_ok=True)
    paths.config_py.write_text(
        "PAPER_STARTING_BALANCE = 10500\n"
        "VIG_STACK_DEFAULT_MAX_POSITION_DOLLARS = 200\n"
        "VIG_STACK_FAMILY_MAX_POSITION_DOLLARS = {'KXMLBGAME': 50}\n"
        "VIG_STACK_DISABLED_FAMILIES: set[str] = {'KXHIGHCHI', 'KXINX'}\n"
    )
    current_rows = [
        {
            "ts": "2026-05-10T16:20:45+00:00",
            "ticker": "KXIPLGAME-26MAY10MIRCB-RCB",
            "opp_type": "live_momentum",
            "decision": "reject",
            "reason": "reentry_blocked",
            "extra": {"sport": "ipl"},
        },
        {
            "ts": "2026-05-10T19:01:10+00:00",
            "ticker": "KXHIGHCHI-26MAY10-B65.5",
            "opp_type": "vig_stack_series",
            "decision": "reject",
            "reason": "family_disabled_reject",
            "extra": {"family": "KXHIGHCHI"},
        },
        {
            "ts": "2026-05-10T20:01:10+00:00",
            "ticker": "KXMLBGAME-26MAY10A-B",
            "opp_type": "vig_stack_futures",
            "decision": "reject",
            "reason": "cap_exceeded_reject",
            "extra": {"family": "KXMLBGAME"},
        },
    ]
    paths.decisions_file.write_text("\n".join(json.dumps(r) for r in current_rows) + "\n")
    archive = paths.state_dir / "archive" / "decisions-2026-05-10.jsonl.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive, "wt") as f:
        f.write(json.dumps({
            "ts": "2026-05-10T19:57:06+00:00",
            "ticker": "KXATPMATCH-26MAY10ARNJOD-JOD",
            "opp_type": "live_momentum",
            "decision": "reject",
            "reason": "reentry_blocked",
            "extra": {"sport": "atp"},
        }) + "\n")

    metrics = glint.collect_metrics(paths, datetime(2026, 5, 11, tzinfo=timezone.utc))
    obs = [{
        "session": 90,
        "shipped_at": "2026-05-09T00:00:00+00:00",
        "metrics": [
            {"name": "reentry_blocked rejects in decisions.jsonl", "current_value": 0},
            {"name": "per-sport distribution of reentry_blocked rejects", "current_value": "stale"},
            {"name": "manual metric stays manual", "current_value": "manual"},
        ],
    }, {
        "session": 92,
        "shipped_at": "2026-05-10T03:00:00+00:00",
        "metrics": [{"name": "cap_exceeded_reject rejects in decisions.jsonl", "current_value": 0}],
    }, {
        "session": 93,
        "shipped_at": "2026-05-10T12:00:00+00:00",
        "metrics": [{"name": "family_disabled_reject events in decisions.jsonl", "current_value": 0}],
    }]

    computed = glint.compute_active_observations(
        obs,
        paths,
        metrics,
        datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    by_name = {
        m["name"]: m["current_value"]
        for o in computed
        for m in o["metrics"]
    }

    assert by_name["reentry_blocked rejects in decisions.jsonl"] == 2
    assert by_name["per-sport distribution of reentry_blocked rejects"] == "n=2: atp=1, ipl=1"
    assert by_name["cap_exceeded_reject rejects in decisions.jsonl"] == 1
    assert by_name["family_disabled_reject events in decisions.jsonl"] == 1
    assert by_name["manual metric stays manual"] == "manual"


def test_compute_active_observations_counts_disabled_family_entries(tmp_path: Path):
    paths = glint.paths_for(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    _write_json(paths.bot_state_file, {})
    paths.decisions_file.write_text("")
    paths.config_py.parent.mkdir(parents=True, exist_ok=True)
    paths.config_py.write_text(
        "PAPER_STARTING_BALANCE = 10500\n"
        "VIG_STACK_DEFAULT_MAX_POSITION_DOLLARS = 200\n"
        "VIG_STACK_FAMILY_MAX_POSITION_DOLLARS = {}\n"
        "VIG_STACK_DISABLED_FAMILIES: set[str] = {'KXHIGHCHI', 'KXINX'}\n"
    )
    _write_json(paths.paper_trades_file, [
        {
            "id": "PAPER-BAD",
            "ticker": "KXHIGHCHI-26MAY10-B65.5",
            "type": "vig_stack",
            "timestamp": "2026-05-10T13:00:00+00:00",
            "status": "open",
        },
        {
            "id": "PAPER-OLD",
            "ticker": "KXINX-26MAY10H1600-B5400.5",
            "type": "vig_stack",
            "timestamp": "2026-05-10T11:00:00+00:00",
            "status": "open",
        },
    ])
    _write_json(paths.positions_file, [])
    metrics = glint.collect_metrics(paths, datetime(2026, 5, 11, tzinfo=timezone.utc))
    obs = [{
        "session": 93,
        "shipped_at": "2026-05-10T12:00:00+00:00",
        "metrics": [{
            "name": "new vig_stack positions opened on KXHIGHCHI or KXINX after ship",
            "current_value": 0,
        }],
    }]

    computed = glint.compute_active_observations(
        obs,
        paths,
        metrics,
        datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert computed[0]["metrics"][0]["current_value"] == 1
