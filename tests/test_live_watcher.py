"""Tests for Live Game Watcher (WATCH command)."""

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Task 1: Config constants
# ---------------------------------------------------------------------------

def test_live_watch_config():
    from bot.config import LIVE_POLL_INTERVAL, LIVE_WATCH_EDGE_THRESHOLD
    assert LIVE_POLL_INTERVAL == 10
    assert 0.05 <= LIVE_WATCH_EDGE_THRESHOLD <= 0.20


# ---------------------------------------------------------------------------
# Session 96: live_momentum decision context helper
# ---------------------------------------------------------------------------

def test_live_momentum_context_helper_complete_context():
    from bot.game_context import GameContext
    from bot.live_watcher import _build_live_momentum_decision_context

    gc = GameContext(sport="nba")
    gc.update(
        espn_data={"clock": "04:12", "home_score": 95, "away_score": 101},
        our_score=101,
        their_score=95,
        period=4,
        clock_str="04:12",
    )
    ctx = _build_live_momentum_decision_context(
        sport="nba",
        ticker="KXNBAGAME-26MAY10TEST-LAL",
        leader_market={
            "ticker": "KXNBAGAME-26MAY10TEST-LAL",
            "yes_ask": 68,
            "yes_bid": 66,
            "volume_24h": 1200,
        },
        opponent_market={
            "ticker": "KXNBAGAME-26MAY10TEST-BOS",
            "yes_ask": 34,
            "volume_24h": 800,
        },
        leader_side="primary",
        recent_high=73,
        dip_cents=5,
        dqs=0.44,
        game_ctx=gc,
        espn_data={"clock": "04:12", "period": 4, "home_score": 95, "away_score": 101},
        skip_reason="dqs_fail",
        entry_gate="dqs",
        source="watcher",
        elapsed_seconds=1500,
    )

    assert ctx["sport"] == "nba"
    assert ctx["leader_ticker"] == "KXNBAGAME-26MAY10TEST-LAL"
    assert ctx["leader_side"] == "primary"
    assert ctx["leader_price"] == 68
    assert ctx["opponent_price"] == 34
    assert ctx["spread_cents"] == 2
    assert ctx["volume_24h"] == 2000
    assert ctx["recent_high"] == 73
    assert ctx["dip_cents"] == 5
    assert ctx["dqs"] == 0.44
    assert ctx["score_state"] == "101-95"
    assert ctx["game_clock"] == "04:12"
    assert ctx["period"] == 4
    assert ctx["skip_reason"] == "dqs_fail"
    assert ctx["entry_gate"] == "dqs"
    assert ctx["context_available"] is True
    assert "leader_price" not in ctx["missing_context_fields"]


def test_live_momentum_context_helper_missing_fields_listed():
    from bot.live_watcher import _build_live_momentum_decision_context

    ctx = _build_live_momentum_decision_context(
        sport="atp",
        ticker="KXATPMATCH-26MAY10TEST-AAA",
        source="watcher",
    )

    assert ctx["sport"] == "atp"
    assert ctx["ticker"] == "KXATPMATCH-26MAY10TEST-AAA"
    assert ctx["context_available"] is False
    assert "leader_price" in ctx["missing_context_fields"]
    assert "score_state" in ctx["missing_context_fields"]


def test_live_momentum_context_helper_partial_alias_inputs():
    from bot.live_watcher import _build_live_momentum_decision_context

    ctx = _build_live_momentum_decision_context(
        sport="ufc",
        ticker="KXUFCFIGHT-26MAY10TEST-AAA",
        kalshi_market={
            "ticker": "KXUFCFIGHT-26MAY10TEST-AAA",
            "yes_bid": 58,
        },
        price_cents=60,
        volume_24h=700,
        source="scan",
        skip_reason="not_today",
    )

    assert ctx["context_available"] is True
    assert ctx["leader_price"] == 60
    assert ctx["yes_ask"] == 60
    assert ctx["yes_bid"] == 58
    assert ctx["spread_cents"] == 2
    assert ctx["volume_24h"] == 700
    assert ctx["source"] == "scan"
    assert "leader_price" not in ctx["missing_context_fields"]
    assert "momentum" in ctx["missing_context_fields"]


def test_live_momentum_context_helper_does_not_leak_blobs():
    from bot.game_context import GameContext
    from bot.live_watcher import _build_live_momentum_decision_context

    gc = GameContext(sport="nba")
    leader = {"ticker": "T1", "yes_ask": 70, "yes_bid": 69, "raw_blob": {"huge": True}}
    espn = {"clock": "01:00", "competitions": [{"large": "blob"}]}
    ctx = _build_live_momentum_decision_context(
        sport="nba",
        ticker="T1",
        leader_market=leader,
        game_ctx=gc,
        espn_data=espn,
        source="watcher",
    )

    dumped = repr(ctx)
    assert "raw_blob" not in dumped
    assert "competitions" not in dumped
    assert "GameContext" not in dumped


# ---------------------------------------------------------------------------
# Task 2: Notifier methods
# ---------------------------------------------------------------------------

def test_notifier_has_edit_message_method():
    from bot.notifier import TelegramNotifier
    import inspect
    assert hasattr(TelegramNotifier, "edit_message_by_id")
    assert inspect.iscoroutinefunction(TelegramNotifier.edit_message_by_id)


def test_notifier_has_send_message_get_id():
    from bot.notifier import TelegramNotifier
    import inspect
    assert hasattr(TelegramNotifier, "send_message_get_id")
    assert inspect.iscoroutinefunction(TelegramNotifier.send_message_get_id)


# ---------------------------------------------------------------------------
# Task 3: LiveGameWatcher class
# ---------------------------------------------------------------------------

MOCK_ESPN_ODDS = {
    "games": [{
        "home_team": "Los Angeles Lakers",
        "away_team": "Denver Nuggets",
        "status": "STATUS_IN_PROGRESS",
        "consensus": {"Los Angeles Lakers": 0.72, "Denver Nuggets": 0.28},
    }]
}


def test_live_watcher_class_exists():
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    assert hasattr(watcher, "start")
    assert hasattr(watcher, "stop")


def test_find_game_returns_live_game():
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    watcher.query = "lakers"
    watcher.sport = "nba"
    with patch("bot.odds_scraper.fetch_consensus_odds", return_value=MOCK_ESPN_ODDS):
        result = watcher._find_game()
    assert result is not None
    assert "Los Angeles Lakers" in (result.get("home_team") or result.get("away_team"))


def test_find_game_returns_none_when_no_match():
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    watcher.query = "patriots"
    watcher.sport = "nba"
    with patch("bot.odds_scraper.fetch_consensus_odds", return_value=MOCK_ESPN_ODDS):
        result = watcher._find_game()
    assert result is None


def test_compute_edge_positive():
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    # espn says 72% win, kalshi asks 55c
    edge, rel = watcher._compute_edge(espn_prob=0.72, kalshi_ask_cents=55, side="yes")
    assert abs(edge - 0.17) < 0.01
    assert abs(rel - (0.17 / 0.55)) < 0.01


def test_compute_edge_no_side():
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    # espn says 72% for YES, so NO team = 28%
    edge, rel = watcher._compute_edge(espn_prob=0.72, kalshi_ask_cents=30, side="no")
    # NO fair value = 1 - 0.72 = 0.28, kalshi = 0.30
    assert edge < 0  # kalshi overpriced for NO


def test_format_status_card_with_score():
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    watcher.query = "lakers"
    watcher.sport = "nba"
    watcher.bets_placed = []
    watcher.exits = []
    watcher.ticker = "KXNBAGAME-26APR05LALDEN-LAL"

    card = watcher._format_status_card(
        home_team="Los Angeles Lakers",
        away_team="Denver Nuggets",
        home_score=87, away_score=79,
        period_label="Q3", clock="5:32",
        espn_prob=0.72, kalshi_ask_cents=55,
        edge=0.17, relative_edge=0.309,
        last_update_secs=3,
    )
    assert "87" in card
    assert "79" in card
    assert "Q3" in card
    assert "5:32" in card
    assert "72%" in card
    assert "55c" in card
    assert "31%" in card   # relative edge display


def test_status_card_shows_bet_placed():
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    watcher.query = "lakers"
    watcher.sport = "nba"
    watcher.bets_placed = [{"side": "yes", "contracts": 15, "price_cents": 55, "ticker": "X", "order_id": "P"}]
    watcher.exits = []
    watcher.ticker = "KXNBAGAME-26APR05LALDEN-LAL"
    # Set by __init__; __new__ skips it. _format_status_card's bets_placed branch reads both.
    watcher._trailing_active = {}
    watcher._peak_values = {}

    card = watcher._format_status_card(
        home_team="Los Angeles Lakers", away_team="Denver Nuggets",
        home_score=87, away_score=79, period_label="Q3", clock="5:32",
        espn_prob=0.72, kalshi_ask_cents=55,
        edge=0.17, relative_edge=0.309, last_update_secs=3,
    )
    assert "15" in card
    assert "YES" in card.upper()


# ---------------------------------------------------------------------------
# Exit logic
# ---------------------------------------------------------------------------

def test_status_card_shows_exited_position():
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    watcher.query = "lakers"
    watcher.sport = "nba"
    watcher.bets_placed = []
    watcher.exits = [{"side": "yes", "contracts": 10, "price_cents": 55,
                       "ticker": "X", "order_id": "P", "reason": "edge reversed to -3.0%", "pnl": -1.50}]
    watcher.ticker = "KXNBAGAME-26APR05LALDEN-LAL"

    card = watcher._format_status_card(
        home_team="Los Angeles Lakers", away_team="Denver Nuggets",
        home_score=87, away_score=79, period_label="Q3", clock="5:32",
        espn_prob=0.48, kalshi_ask_cents=55,
        edge=-0.07, relative_edge=-0.127, last_update_secs=120,
    )
    assert "EXITED" in card
    assert "reversed" in card


def test_session_summary_includes_exits():
    import time
    from collections import Counter, deque
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    watcher.query = "lakers"
    watcher.bets_placed = []
    watcher.exits = [{"side": "yes", "contracts": 10, "price_cents": 55,
                       "ticker": "X", "order_id": "P", "reason": "edge faded", "pnl": 2.30}]
    # Set by __init__; __new__ skips them. _format_session_summary reads all of these.
    watcher._started_at = time.time()
    watcher.mode = "momentum"
    watcher._match_title = "Los Angeles Lakers @ Denver Nuggets"
    watcher.ticker = "KXNBAGAME-26APR05LALDEN-LAL"
    watcher._price_history = deque()
    watcher._tick_telem = Counter()
    with patch("bot.live_watcher._journal_append"):
        summary = watcher._format_session_summary("Los Angeles Lakers", "Denver Nuggets")
    assert "EXITED" in summary
    assert "+$2.30" in summary
    assert "Session P&L" in summary


# ---------------------------------------------------------------------------
# Task 4: Cache bypass
# ---------------------------------------------------------------------------

def test_fetch_consensus_odds_has_bypass_param():
    """fetch_consensus_odds must accept bypass_cache keyword."""
    import inspect
    from bot.odds_scraper import fetch_consensus_odds
    sig = inspect.signature(fetch_consensus_odds)
    assert "bypass_cache" in sig.parameters


# ---------------------------------------------------------------------------
# Task 5: WATCH/UNWATCH integration
# ---------------------------------------------------------------------------

def test_glintbot_has_active_watchers():
    from bot.main import GlintBot
    import inspect
    src = inspect.getsource(GlintBot.__init__)
    assert "_active_watchers" in src


def test_watch_command_registered():
    """WATCH and UNWATCH must be registered as command callbacks."""
    from bot.main import GlintBot
    import inspect
    src = inspect.getsource(GlintBot._register_commands)
    assert "WATCH" in src
    assert "UNWATCH" in src


# ---------------------------------------------------------------------------
# Session 7: edge proxy + mom_ctx in _log_decision_dampened
# ---------------------------------------------------------------------------

def test_log_decision_dampened_writes_wp_edge_and_mom_ctx(tmp_path, monkeypatch):
    """live_momentum rejects/accepts carry wp_edge as edge and extra includes
    wp, kalshi_price, dip_cents, dqs."""
    import json
    from bot import decisions
    import bot.shadow_trades as shadow
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    shadow_f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", shadow_f)

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = "KXTEST-T1"
    w._last_decision = (None, None)

    w._log_decision_dampened(
        decision="reject", reason="dqs_fail",
        gates={"can_enter": True, "dqs": False},
        edge=0.07,
        extra={"wp": 0.62, "kalshi_price": 55, "dip_cents": 6,
               "dqs": 0.31, "threshold": 0.4},
    )

    rec = json.loads(f.read_text().splitlines()[0])
    assert rec["opp_type"] == "live_momentum"
    assert rec["edge"] == 0.07
    assert rec["extra"]["wp"] == 0.62
    assert rec["extra"]["kalshi_price"] == 55
    assert rec["extra"]["dip_cents"] == 6
    assert rec["extra"]["dqs"] == 0.31


def test_log_decision_dampened_handles_missing_wp(tmp_path, monkeypatch):
    """Pre-GameContext ticks log wp=None / edge=None — no crash, valid JSON."""
    import json
    from bot import decisions
    import bot.shadow_trades as shadow
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    shadow_f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", shadow_f)

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = "KXTEST-T2"
    w._last_decision = (None, None)

    w._log_decision_dampened(
        decision="reject", reason="sport_disabled",
        gates={"can_enter": False, "sport_enabled": False},
        edge=None,
        extra={"wp": None, "kalshi_price": 50, "dip_cents": 0, "dqs": None,
               "sport": "atp_challenger"},
    )

    rec = json.loads(f.read_text().splitlines()[0])
    assert rec["edge"] is None
    assert rec["extra"]["wp"] is None
    assert rec["extra"]["sport"] == "atp_challenger"


def test_log_decision_dampened_sport_disabled_writes_shadow(tmp_path, monkeypatch):
    import json
    from bot import decisions
    import bot.shadow_trades as shadow
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    shadow_f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", shadow_f)

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = "KXWTAMATCH-26MAY10SIEPLI-PLI"
    w._last_decision = (None, None)

    w._log_decision_dampened(
        decision="reject",
        reason="sport_disabled",
        gates={"can_enter": False, "sport_enabled": False},
        edge=None,
        extra={"sport": "wta", "kalshi_price": 92, "losses_seen": 0},
        close_ts="2026-05-24T08:00:00Z",
    )

    rec = json.loads(shadow_f.read_text().splitlines()[0])
    assert rec["blocked_reason"] == "sport_disabled"
    assert rec["source"] == "live_watcher"
    assert rec["would_side"] == "yes"
    assert rec["would_entry_price"] == 0.92
    assert rec["would_contracts"] is None
    assert rec["sizing_status"] == "unavailable"
    assert rec["sport"] == "wta"
    assert rec["close_ts"] == "2026-05-24T08:00:00Z"


def test_log_decision_dampened_reentry_blocked_shadow_and_dedupe(tmp_path, monkeypatch):
    import json
    from bot import decisions
    import bot.shadow_trades as shadow
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    shadow_f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", shadow_f)

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = "KXATPMATCH-26MAY10ARNJOD-JOD"
    w._last_decision = (None, None)
    kwargs = dict(
        decision="reject",
        reason="reentry_blocked",
        gates={"can_enter": False, "sport_enabled": True, "reentry_blocked": False},
        edge=-0.36,
        extra={"sport": "atp", "kalshi_price": 86, "losses_seen": 1},
        close_ts="2026-05-24T09:00:00Z",
    )

    w._log_decision_dampened(**kwargs)
    w._log_decision_dampened(**kwargs)

    rows = shadow_f.read_text().splitlines()
    assert len(rows) == 1
    rec = json.loads(rows[0])
    assert rec["blocked_reason"] == "reentry_blocked"
    assert rec["extra"]["losses_seen"] == 1
    assert rec["would_contracts"] is None
    assert rec["sizing_status"] == "unavailable"
    assert rec["extra"]["missing_sizing_fields"] == ["would_contracts"]
    assert rec["extra"]["sizing_unavailable_reason"] == "sizing_not_provided"
    assert rec["sport"] == "atp"


def test_log_decision_dampened_reentry_blocked_shadow_available_sizing(tmp_path, monkeypatch):
    import json
    from bot import decisions
    import bot.shadow_trades as shadow
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    shadow_f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", shadow_f)

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = "KXIPLGAME-26MAY11DCPBKS-PBKS"
    w._last_decision = (None, None)

    w._log_decision_dampened(
        decision="reject",
        reason="reentry_blocked",
        gates={"can_enter": False, "sport_enabled": True, "reentry_blocked": False},
        edge=-0.18,
        extra={"sport": "ipl", "kalshi_price": 68, "losses_seen": 1},
        close_ts="2026-05-14T14:00:00Z",
        shadow_would_contracts=20,
        shadow_extra={
            "would_sizing_source": "live_momentum_sizing_v1",
            "would_sizing_price_cents": 68,
            "would_sizing_dip_cents": 5,
            "would_sizing_balance": 10500.0,
        },
    )

    rec = json.loads(shadow_f.read_text().splitlines()[0])
    assert rec["blocked_reason"] == "reentry_blocked"
    assert rec["would_entry_price"] == 0.68
    assert rec["would_contracts"] == 20
    assert rec["would_notional"] == 13.6
    assert rec["sizing_status"] == "available"
    assert rec["extra"]["would_sizing_source"] == "live_momentum_sizing_v1"
    assert "missing_sizing_fields" not in rec["extra"]


# ---------------------------------------------------------------------------
# Session 97: per-sport disable set membership + shadow-write coverage
# ---------------------------------------------------------------------------

def test_session_97_disable_set_membership():
    """Session 97 added atp + nba_game to MOMENTUM_DISABLED_SPORTS based on
    breakeven-WR analysis (atp -27.2pp / nba_game -16.9pp gaps post-Session-54).
    Locks both the additions and the sports deliberately left enabled (ufc
    at n=11 borderline → watch-list; nhl_game / ipl profitable) so a future
    edit cannot silently drift the set in either direction."""
    from bot.config import MOMENTUM_DISABLED_SPORTS
    # Session 97 additions
    assert "atp" in MOMENTUM_DISABLED_SPORTS
    assert "nba_game" in MOMENTUM_DISABLED_SPORTS
    # Carried from Sessions 2 / 38a-2
    assert "atp_challenger" in MOMENTUM_DISABLED_SPORTS
    assert "wta" in MOMENTUM_DISABLED_SPORTS
    assert "wta_challenger" in MOMENTUM_DISABLED_SPORTS
    # Deliberately enabled — bundling these would reverse Session 97's discipline
    for enabled in ("ufc", "nhl_game", "ipl"):
        assert enabled not in MOMENTUM_DISABLED_SPORTS, (
            f"{enabled!r} should not be in MOMENTUM_DISABLED_SPORTS — Session 97 "
            f"deliberately left it enabled (ufc=watch-list, nhl_game/ipl=profitable)"
        )


@pytest.mark.parametrize("sport,ticker", [
    ("atp", "KXATPMATCH-26MAY11ALCJOK-ALC"),
    ("atp_challenger", "KXATPCHALLENGERMATCH-26MAY11SMIWAS-SMI"),
    ("nba_game", "KXNBAGAME-26MAY11LALBOS-BOS"),
    ("wta", "KXWTAMATCH-26MAY11SWIGAU-SWI"),
    ("wta_challenger", "KXWTACHALLENGERMATCH-26MAY11ZANYAS-ZAN"),
])
def test_each_disabled_sport_writes_shadow_row(sport, ticker, tmp_path, monkeypatch):
    """Session 97 regression: every sport in MOMENTUM_DISABLED_SPORTS must
    emit a shadow_trades row when the live_watcher entry path rejects with
    reason=sport_disabled. Without this, the 14d validation window for
    Session 97's atp + nba_game disables collects no counterfactual evidence.
    Mirrors test_log_decision_dampened_sport_disabled_writes_shadow but
    parametrized across all disabled sports."""
    import json
    from bot import decisions
    import bot.shadow_trades as shadow
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    shadow_f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", shadow_f)

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = ticker
    w._last_decision = (None, None)

    w._log_decision_dampened(
        decision="reject",
        reason="sport_disabled",
        gates={"can_enter": False, "sport_enabled": False},
        edge=None,
        extra={"sport": sport, "kalshi_price": 75, "losses_seen": 0},
        close_ts="2026-05-25T03:00:00Z",
    )

    rec = json.loads(shadow_f.read_text().splitlines()[0])
    assert rec["blocked_reason"] == "sport_disabled"
    assert rec["source"] == "live_watcher"
    assert rec["sport"] == sport
    assert rec["ticker"] == ticker
    assert rec["would_entry_price"] == 0.75
    assert rec["sizing_status"] == "unavailable"


# ---------------------------------------------------------------------------
# Session 15.5: close_ts threaded through dampener for regime tagging
# ---------------------------------------------------------------------------

def test_log_decision_dampened_threads_close_ts_into_extra(tmp_path, monkeypatch):
    """The dampener accepts a close_ts kwarg and merges it into extra so
    bot.regime.tag can populate event_horizon_hr."""
    import json
    from bot import decisions
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = "KXNBAGAME-T1"
    w._last_decision = (None, None)

    w._log_decision_dampened(
        decision="reject", reason="dip_too_big",
        gates={"can_enter": True, "dip_max": False},
        edge=0.05,
        extra={"wp": 0.6, "kalshi_price": 55, "dip_cents": 9, "dqs": None,
               "max_dip": 8},
        close_ts="2026-04-26T00:00:00Z",
    )

    rec = json.loads(f.read_text().splitlines()[0])
    assert rec["extra"]["close_ts"] == "2026-04-26T00:00:00Z"


def test_log_decision_dampened_close_ts_does_not_overwrite_explicit(tmp_path, monkeypatch):
    """If extra already contains close_ts, the kwarg fallback does not overwrite."""
    import json
    from bot import decisions
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = "KXNBAGAME-T2"
    w._last_decision = (None, None)

    w._log_decision_dampened(
        decision="accept", reason="dip_buy",
        gates={"can_enter": True, "dqs": True},
        edge=0.08,
        extra={"close_ts": "EXPLICIT-IN-EXTRA",
               "wp": 0.7, "kalshi_price": 50},
        close_ts="FALLBACK-IGNORED",
    )

    rec = json.loads(f.read_text().splitlines()[0])
    assert rec["extra"]["close_ts"] == "EXPLICIT-IN-EXTRA"


# ---------------------------------------------------------------------------
# Session 19a-peakfix: TRAILING_STOP fires after the peak-tracking fix
# ---------------------------------------------------------------------------

def test_check_exit_trailing_stop_fires_after_peak_fix(monkeypatch):
    """Lock for the Session 19a-peakfix one-line fix at bot/live_watcher.py:2225.

    Pre-fix, _peak_values.get(ticker, current_value) defaulted to current_value,
    so the strict `if current_value > prev_peak` was always False on the first
    observation and _peak_values[ticker] was never written. The TRAILING_STOP
    read at line 2258 then ALSO defaulted to current_value, so drop_from_peak
    was always 0 and TRAILING_STOP could not fire.

    The fix uses setdefault(ticker, entry_price), which both reads AND writes
    on the first observation. This test feeds two ticks: a peak, then a drop
    large enough to trigger TRAILING_STOP, and asserts the exit fires.

    On pre-fix code, this test FAILS (no exit). On post-fix code it PASSES.
    """
    import asyncio
    import json
    from collections import deque
    from bot.live_watcher import LiveGameWatcher

    ticker = "KXNBAGAME-26APR26TEST-LAL"
    entry_price = 50  # cents per contract

    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    watcher.mode = "momentum"
    watcher.sport = "nba"  # NBA profile: take_profit=12, trail_stop=4
    watcher.ticker = ticker
    watcher._opponent_ticker = None
    watcher._game_ctx = None  # avoids exit_record snapshot lookup
    watcher._last_espn_data = None
    watcher._price_history = deque([])
    watcher._trailing_active = {}
    watcher._peak_values = {}
    watcher._match_title = "test match"
    watcher.query = "test"
    watcher.bets_placed = [{
        "ticker": ticker,
        "side": "yes",
        "price_cents": entry_price,
        "contracts": 5,
        "order_id": "test-order-1",
        "entered_at": 0,
    }]
    watcher.exits = []

    # Tick 1: yes_bid=58 → current_value=58, gain=8 (under NBA TP=12, no exit).
    # Establishes the peak.
    market_t1 = {"yes_bid": 58, "yes_ask": 59}

    # Tick 2: yes_bid=53 → drop_from_peak = 58 - 53 = 5 ≥ trail_stop=4
    # AND gain_cents = 53 - 50 = 3 > 0 → TRAILING_STOP fires.
    market_t2 = {"yes_bid": 53, "yes_ask": 54}

    # Patch the paper-exit side effects (writes to paper_trades.json + positions.json
    # via state_io). _check_exit appends to self.exits regardless, so the assertion
    # works either way — but mocking keeps the test hermetic.
    monkeypatch.setattr("bot.config.PAPER_MODE", True)
    monkeypatch.setattr("bot.executor._paper_record_exit", lambda *a, **kw: None)
    monkeypatch.setattr("bot.state_io.load_json", lambda *a, **kw: [])
    monkeypatch.setattr("bot.state_io.save_json", lambda *a, **kw: None)

    # Tick 1: peak should be set to current_value=58 (setdefault writes 50 first,
    # then the if-branch updates to 58 because 58 > 50).
    asyncio.run(watcher._check_exit(market_t1, edge=0.0, relative_edge=0.0))
    assert watcher._peak_values.get(ticker) == 58, (
        f"peak_values[ticker] should be 58 after first tick "
        f"(setdefault wrote {entry_price}, if-branch updated to 58); "
        f"got {watcher._peak_values}"
    )
    assert len(watcher.exits) == 0, (
        f"no exit should fire on tick 1 (gain=8 < TP=12, no drop yet); "
        f"got {watcher.exits}"
    )

    # Tick 2: drop=5 from peak=58 to current=53, gain=3, NBA trail_stop=4 → fires.
    asyncio.run(watcher._check_exit(market_t2, edge=0.0, relative_edge=0.0))
    assert len(watcher.exits) == 1, (
        f"TRAILING_STOP should fire on tick 2 (drop=5 ≥ trail_stop=4, gain=3 > 0); "
        f"got {watcher.exits}"
    )
    assert "TRAILING STOP" in watcher.exits[0]["reason"], (
        f"exit reason should be TRAILING STOP; got {watcher.exits[0]['reason']!r}"
    )


# ---------------------------------------------------------------------------
# Session 54: live_watcher correctness pass
# ---------------------------------------------------------------------------

def test_auto_bet_momentum_uses_paper_starting_balance_not_hardcoded_500(tmp_path, monkeypatch):
    """Momentum sizing must use the configured paper balance, not the old $500.

    Pre-fix, _auto_bet_momentum reconstructed paper balance as 500 + realized
    P&L, so post-Apr-29 live_momentum entries were sized at ~9% of intended
    scale. Empty paper_trades should size off PAPER_STARTING_BALANCE.
    """
    import asyncio
    from collections import Counter
    from bot import live_watcher
    from bot.config import PAPER_STARTING_BALANCE

    state_dir = tmp_path / "bot" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "paper_trades.json").write_text("[]")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(live_watcher, "PAPER_MODE", True)
    monkeypatch.setattr(live_watcher, "LIVE_JOURNAL_FILE", tmp_path / "live_journal.json")

    captured = {}

    def fake_kelly_size(**kwargs):
        captured["balance"] = kwargs["balance"]
        return {"contracts": 2, "total_cost": 1.0, "max_payout": 2.0}

    def fake_execute_trade(*args, **kwargs):
        return {
            "success": True,
            "order_result": {"order_id": "paper-order-balance", "filled_count": 2},
        }

    monkeypatch.setattr("bot.sizing.kelly_size", fake_kelly_size)
    monkeypatch.setattr("bot.executor.execute_trade", fake_execute_trade)

    watcher = live_watcher.LiveGameWatcher.__new__(live_watcher.LiveGameWatcher)
    watcher.ticker = "KXNBAGAME-26MAY05TEST-LAL"
    watcher.query = "test"
    watcher.sport = "nba"
    watcher.balance = 0.0
    watcher._game_ctx = None
    watcher._last_espn_data = None
    watcher._match_title = "Test Game"
    watcher.bets_placed = []
    watcher._entry_count = 0
    watcher._tick_telem = Counter()

    asyncio.run(watcher._auto_bet_momentum(
        {"ticker": watcher.ticker},
        yes_ask=50,
        reason="test dip",
        dip_cents=4,
        dqs_score=0.8,
    ))

    assert captured["balance"] >= PAPER_STARTING_BALANCE - 1
    assert captured["balance"] > 5000


def test_paper_exit_persists_exit_reason_not_unknown(tmp_path, monkeypatch):
    """Live watcher paper exits must forward the concrete exit reason.

    Pre-fix, _check_exit called _paper_record_exit without reason=..., so every
    forward paper exit written from this path persisted exit_reason="unknown".
    """
    import asyncio
    import json
    from collections import deque
    from bot import live_watcher
    from bot import executor

    ticker = "KXNBAGAME-26MAY05TEST-BOS"
    order_id = "paper-order-exit-reason"
    paper_file = tmp_path / "paper_trades.json"
    paper_file.write_text(json.dumps([{
        "id": order_id,
        "ticker": ticker,
        "type": "live_momentum",
        "side": "yes",
        "entry_price": 0.50,
        "contracts": 5,
        "timestamp": "2026-05-05T00:00:00+00:00",
        "status": "open",
        "exit_price": None,
        "pnl": None,
        "resolved_at": None,
    }]))

    monkeypatch.setattr(live_watcher, "PAPER_MODE", True)
    monkeypatch.setattr(live_watcher, "LIVE_JOURNAL_FILE", tmp_path / "live_journal.json")
    monkeypatch.setattr(executor, "PAPER_TRADES_FILE", paper_file)
    monkeypatch.setattr("bot.config.POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr("bot.tracker.log_settlement", lambda *a, **kw: None)
    monkeypatch.setattr("bot.tracker.check_settlement_invariant", lambda *a, **kw: None)
    monkeypatch.setattr("bot.patterns.record_resolution", lambda *a, **kw: None)

    watcher = live_watcher.LiveGameWatcher.__new__(live_watcher.LiveGameWatcher)
    watcher.mode = "momentum"
    watcher.sport = "nba"
    watcher.ticker = ticker
    watcher._opponent_ticker = None
    watcher._game_ctx = None
    watcher._last_espn_data = None
    watcher._price_history = deque([50, 65])
    watcher._trailing_active = {}
    watcher._peak_values = {}
    watcher._match_title = "Test Game"
    watcher.query = "test"
    watcher.bets_placed = [{
        "ticker": ticker,
        "side": "yes",
        "price_cents": 50,
        "contracts": 5,
        "order_id": order_id,
        "entered_at": 0,
    }]
    watcher.exits = []

    asyncio.run(watcher._check_exit({"yes_bid": 65, "yes_ask": 66}, edge=0.0, relative_edge=0.0))

    record = json.loads(paper_file.read_text())[0]
    expected_reason = "TAKE PROFIT: +15¢ (50¢ → 65¢)"
    assert record["exit_reason"] == expected_reason
    assert record["exit_reason"] != "unknown"


def test_period_extraction_from_score_snapshot_does_not_raise(tmp_path, monkeypatch):
    """ScoreSnapshot is a dataclass; period must be read as an attribute."""
    import asyncio
    from collections import Counter, deque
    from bot import live_watcher
    from bot.game_context import GameContext

    gc = GameContext(sport="nba")
    gc.update({}, our_score=87, their_score=79, period=3, clock_str="5:00")

    monkeypatch.setattr(live_watcher, "PAPER_MODE", False)
    monkeypatch.setattr(live_watcher, "LIVE_JOURNAL_FILE", tmp_path / "live_journal.json")
    monkeypatch.setattr(
        "bot.sizing.kelly_size",
        lambda **kwargs: {"contracts": 2, "total_cost": 1.0, "max_payout": 2.0},
    )
    monkeypatch.setattr(
        "bot.executor.execute_trade",
        lambda *a, **kw: {
            "success": True,
            "order_result": {"order_id": "paper-order-period", "filled_count": 2},
        },
    )

    watcher = live_watcher.LiveGameWatcher.__new__(live_watcher.LiveGameWatcher)
    watcher.mode = "momentum"
    watcher.ticker = "KXNBAGAME-26MAY05TEST-NYK"
    watcher.query = "test"
    watcher.sport = "nba"
    watcher.balance = 1000.0
    watcher._game_ctx = gc
    watcher._last_espn_data = None
    watcher._match_title = "Test Game"
    watcher.bets_placed = []
    watcher.exits = []
    watcher._entry_count = 0
    watcher._tick_telem = Counter()
    watcher._opponent_ticker = None
    watcher._price_history = deque([55, 70])
    watcher._trailing_active = {}
    watcher._peak_values = {}

    asyncio.run(watcher._auto_bet_momentum(
        {"ticker": watcher.ticker},
        yes_ask=55,
        reason="period test",
        dip_cents=4,
        dqs_score=0.8,
    ))

    assert watcher.bets_placed[0]["game_state"]["period"] == 3

    monkeypatch.setattr(live_watcher, "PAPER_MODE", True)
    monkeypatch.setattr("bot.executor._paper_record_exit", lambda *a, **kw: None)
    monkeypatch.setattr("bot.config.POSITIONS_FILE", tmp_path / "positions.json")

    asyncio.run(watcher._check_exit({"yes_bid": 70, "yes_ask": 71}, edge=0.0, relative_edge=0.0))

    assert watcher.exits[0]["exit_game_state"]["period"] == 3


# ---------------------------------------------------------------------------
# Session 29: _journal_append recovers from trailing-bracket corruption
# ---------------------------------------------------------------------------

def test_journal_append_recovers_from_trailing_bracket_corruption(tmp_path, monkeypatch):
    """Session 29 regression: live_journal.json silently stopped being written
    for ~28 hours when its file ended with `]\\n]`. The strict json.loads()
    in _journal_append raised JSONDecodeError; the broad `except Exception`
    silently absorbed it; every subsequent write was a no-op forever.

    Verify recovery: a corrupted file (valid JSON array + trailing `]`) must
    be re-parsed via raw_decode, the new entry appended, and the file rewritten
    cleanly so subsequent writes succeed normally.

    On pre-fix code this test FAILS (file stays at 1 event). On post-fix code
    it PASSES (file ends with both events as a clean array)."""
    import json
    from bot import live_watcher

    journal = tmp_path / "live_journal.json"
    # Same shape observed in production at 2026-04-27T20:14:01 UTC: a valid
    # JSON array followed by an extra `]`.
    journal.write_text('[\n  {"event": "old", "ticker": "T1"}\n]\n]')
    monkeypatch.setattr(live_watcher, "LIVE_JOURNAL_FILE", journal)

    live_watcher._journal_append({"event": "scan_found", "ticker": "T2"})

    # File is valid JSON now and contains BOTH the historical and the new event.
    data = json.loads(journal.read_text())
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["event"] == "old"
    assert data[0]["ticker"] == "T1"
    assert data[1]["event"] == "scan_found"
    assert data[1]["ticker"] == "T2"
    assert "timestamp" in data[1]

    # And a follow-up write on the now-clean file appends without recovery.
    live_watcher._journal_append({"event": "exit", "ticker": "T2"})
    data2 = json.loads(journal.read_text())
    assert len(data2) == 3
    assert data2[2]["event"] == "exit"


# ---------------------------------------------------------------------------
# Session 29-followup: per-event-type _journal_append regression tests.
#
# Verification gap closed: Session 29's only regression test exercised
# scan_found and exit through _journal_append. The investigation that lived
# in CLAUDE.md's Session 29-followup block assumed (incorrectly) that
# bet/exit/session_end had a separate broken writer; the real story was that
# all four event types share _journal_append and the path was healthy after
# Session 29. These four tests pin that contract: every event type the live
# watcher emits goes through _journal_append with the production payload
# shape, so any future write-path regression (or accidental introduction of
# a sibling writer) is caught here, not after a 28-hour observability gap.
# ---------------------------------------------------------------------------


def test_journal_append_writes_scan_found_event(tmp_path, monkeypatch):
    """scan_found goes through _journal_record_scan (live_watcher.py:97), which
    builds the entry and calls _journal_append (live_watcher.py:137)."""
    import json
    from bot import live_watcher

    journal = tmp_path / "live_journal.json"
    monkeypatch.setattr(live_watcher, "LIVE_JOURNAL_FILE", journal)

    live_watcher._journal_record_scan(
        ticker="KXNBAGAME-26APR28PHIBOS-BOS",
        sport="nba",
        skip_reason=None,
        match="Philadelphia at Boston",
        price=87,
        volume=12345,
        event_ticker="KXNBAGAME-26APR28PHIBOS",
    )

    data = json.loads(journal.read_text())
    assert isinstance(data, list)
    assert len(data) == 1
    e = data[0]
    assert e["event"] == "scan_found"
    assert e["ticker"] == "KXNBAGAME-26APR28PHIBOS-BOS"
    assert e["sport"] == "nba"
    assert e["skip_reason"] is None
    assert e["price"] == 87
    assert e["volume"] == 12345
    assert "timestamp" in e


def test_journal_append_writes_bet_event(tmp_path, monkeypatch):
    """bet shape from live_watcher.py:1804-1819 — emitted by _auto_bet_momentum
    after a successful execute_trade."""
    import json
    from bot import live_watcher

    journal = tmp_path / "live_journal.json"
    monkeypatch.setattr(live_watcher, "LIVE_JOURNAL_FILE", journal)

    live_watcher._journal_append({
        "event": "bet",
        "match": "Philadelphia at Boston",
        "ticker": "KXNBAGAME-26APR28PHIBOS-BOS",
        "side": "yes",
        "contracts": 16,
        "price_cents": 87,
        "reason": "leader at 87c, dipped 4c from recent 91c",
        "mode": "dip",
        "entry_number": 1,
        "size_multiplier": 1.0,
        "sport": "nba",
        "filled": 16,
        "game_state": {"score_diff": 11, "period": "Q3"},
        "instincts": [],
    })

    data = json.loads(journal.read_text())
    assert len(data) == 1
    e = data[0]
    assert e["event"] == "bet"
    assert e["ticker"] == "KXNBAGAME-26APR28PHIBOS-BOS"
    assert e["side"] == "yes"
    assert e["contracts"] == 16
    assert e["price_cents"] == 87
    assert e["mode"] == "dip"
    assert "timestamp" in e

    # Subsequent write of a different event type must append cleanly to the
    # same file — defends against a future regression where _journal_append
    # only works on first call.
    live_watcher._journal_append({
        "event": "exit",
        "ticker": "KXNBAGAME-26APR28PHIBOS-BOS",
        "side": "yes",
        "reason": "TAKE PROFIT",
        "pnl": 1.92,
    })
    data2 = json.loads(journal.read_text())
    assert len(data2) == 2
    assert data2[1]["event"] == "exit"


def test_journal_append_writes_exit_event(tmp_path, monkeypatch):
    """exit shape from live_watcher.py:2545-2560 — emitted by _check_exit's
    smart-exit logic. Also covers the live_watcher.py:1010 settlement-detection
    exit, which uses the same _journal_append call shape."""
    import json
    from bot import live_watcher

    journal = tmp_path / "live_journal.json"
    monkeypatch.setattr(live_watcher, "LIVE_JOURNAL_FILE", journal)

    live_watcher._journal_append({
        "event": "exit",
        "match": "Philadelphia at Boston",
        "ticker": "KXNBAGAME-26APR28PHIBOS-BOS",
        "side": "yes",
        "sport": "nba",
        "reason": "TRAILING STOP: peaked at 92c, dropped 6c",
        "pnl": 0.80,
        "entry_price": 87,
        "exit_value": 86,
        "peak_value": 92,
        "hold_seconds": 480,
        "mode": "dip",
        "game_state_at_entry": {"score_diff": 11, "period": "Q3"},
        "game_state_at_exit": {"score_diff": 5, "period": "Q4"},
    })

    data = json.loads(journal.read_text())
    assert len(data) == 1
    e = data[0]
    assert e["event"] == "exit"
    assert e["ticker"] == "KXNBAGAME-26APR28PHIBOS-BOS"
    assert e["reason"].startswith("TRAILING STOP")
    assert e["pnl"] == 0.80
    assert e["entry_price"] == 87
    assert e["peak_value"] == 92
    assert e["hold_seconds"] == 480
    assert "timestamp" in e


def test_journal_append_writes_session_end_event(tmp_path, monkeypatch):
    """session_end shape from live_watcher.py:2748-2760 — emitted by
    _format_session_summary when the watcher's start() loop exits.

    This is the event type whose firing in production at
    2026-04-29T01:45:42 UTC (PHIBOS settlement) provided the direct evidence
    that Session 29's _journal_append fix was healthy for all event types."""
    import json
    from bot import live_watcher

    journal = tmp_path / "live_journal.json"
    monkeypatch.setattr(live_watcher, "LIVE_JOURNAL_FILE", journal)

    live_watcher._journal_append({
        "event": "session_end",
        "match": "Philadelphia at Boston",
        "ticker": "KXNBAGAME-26APR28PHIBOS-BOS",
        "mode": "momentum",
        "duration_min": 73.0,
        "ticks": 438,
        "bets_placed": 1,
        "exits": 1,
        "total_pnl": -13.92,
        "price_history": [91, 90, 87, 85, 72, 24, 1],
        "tick_telem": {
            "ticks": 438,
            "no_leader": 50,
            "dqs_fail": 12,
            "execute_failed": {"ALREADY_HOLD_OPEN_POSITION_IN_KXNBAGAME-26APR28P": 14},
        },
    })

    data = json.loads(journal.read_text())
    assert len(data) == 1
    e = data[0]
    assert e["event"] == "session_end"
    assert e["ticker"] == "KXNBAGAME-26APR28PHIBOS-BOS"
    assert e["mode"] == "momentum"
    assert e["duration_min"] == 73.0
    assert e["bets_placed"] == 1
    assert e["exits"] == 1
    assert e["total_pnl"] == -13.92
    assert e["tick_telem"]["ticks"] == 438
    assert "timestamp" in e


# ---------------------------------------------------------------------------
# Session 33: persist DQS to live_ticks.jsonl rows
# ---------------------------------------------------------------------------
# These tests drive _tick_momentum end-to-end with all I/O mocked. The harness
# uses LiveGameWatcher.__new__() (bypass init) + monkeypatch on the module-level
# _log_tick to capture the tick payload dict.
#
# Goal: verify that the new "dqs" field appears in the captured tick row with
# the correct value across four scenarios:
#   1. Computed real DQS  → captured tick row has dqs=<float in [0,1]>
#   2. No entry evaluation → captured tick row has dqs=None
#   3. Variance-quality scalp path (tennis/UFC) → captured tick row has dqs=None
#   4. Behavior preservation: _auto_bet_momentum receives the unmodified buy_dqs


def _build_momentum_watcher(*, sport, ticker, price_history, yes_ask,
                             entry_count=0, cooldown=0, bets_placed=None,
                             entry_losses=None,
                             status_msg_id=None,
                             opp_yes_ask=30, opp_history=None):
    """Bypass-init a LiveGameWatcher with the minimum state for _tick_momentum.

    All instance methods that perform I/O are stubbed; the test wires its own
    mocks via the returned watcher reference.

    Note on opponent: _tick_momentum's "match settled" check treats
    opp_yes_ask <= 3 as settled. Tests need a non-settled opponent
    (default 30¢) to reach the entry-evaluation block.
    """
    from collections import Counter, deque
    import time
    from unittest.mock import AsyncMock, MagicMock
    from bot.live_watcher import LiveGameWatcher

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = ticker
    w._opponent_ticker = ticker.replace("LAL", "OPP") + "-OPP"
    w.sport = sport
    w._match_title = "test match"
    w.query = "test"
    w._price_history = deque(price_history)
    w._opp_price_history = deque(opp_history or [opp_yes_ask, opp_yes_ask, opp_yes_ask])
    w._game_ctx = None
    w._last_espn_data = None
    w._espn_tick_counter = 0
    w._gone_ticks = 0
    w._cooldown_remaining = cooldown
    w._entry_count = entry_count
    w._entry_losses = list(entry_losses) if entry_losses else []
    w.bets_placed = bets_placed if bets_placed is not None else []
    w.exits = []
    w._tick_telem = Counter()
    w._started_at = time.time() - 60
    w._last_decision = (None, None)
    w._peak_values = {}
    w._trailing_active = {}
    w.status_msg_id = status_msg_id
    w.notifier = MagicMock()
    w.notifier.edit_message_by_id = AsyncMock()
    w.balance = 500.0
    w.active = True
    w.mode = "momentum"

    # I/O methods — stubs so we don't hit Kalshi/ESPN
    market = {
        "ticker": ticker,
        "yes_ask": yes_ask,
        "yes_bid": yes_ask - 1,
        "close_ts": "2026-04-30T23:00:00Z",
    }
    opp_market = {
        "ticker": w._opponent_ticker,
        "yes_ask": opp_yes_ask,
        "yes_bid": max(0, opp_yes_ask - 1),
        "close_ts": "2026-04-30T23:00:00Z",
    }
    w._fetch_kalshi_market = MagicMock(return_value=market)
    w._fetch_opponent_market = MagicMock(return_value=opp_market)
    w._fetch_espn_score = MagicMock(return_value=None)
    w._check_exit = AsyncMock()  # bets_placed empty → never invoked anyway
    w._auto_bet_momentum = AsyncMock()
    w._log_decision_dampened = MagicMock()  # decoupled — Session-7 dampener tests cover its own behavior

    return w


def test_tick_record_includes_dqs_field_when_computed(monkeypatch):
    """Session 33: when compute_dip_quality runs, its score lands on the tick row.

    Drive a primary-side dip-eligible tick with mocked compute_dip_quality
    returning 0.42. Assert _log_tick captured a payload with dqs=0.42, even
    though 0.42 is below MOMENTUM_DQS_THRESHOLD (so no entry fires).
    """
    import asyncio
    from bot import live_watcher
    from bot import game_context

    # Build watcher: NBA profile (no skip_dqs), price history shows a 5¢ dip
    # (recent_high=70, current=65 → dip=5, in [4,8] window).
    w = _build_momentum_watcher(
        sport="nba",
        ticker="KXNBAGAME-26APR30TEST-LAL",
        price_history=[70, 70, 69, 70, 70],
        yes_ask=65,
    )

    captured: list[dict] = []
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: captured.append(dict(d)))

    # DQS = 0.42 → BELOW MOMENTUM_DQS_THRESHOLD (0.40) PASSES; bump to 0.32 to
    # ensure NO entry fires while DQS still gets logged.
    monkeypatch.setattr(
        game_context, "compute_dip_quality",
        lambda **kwargs: (0.32, {"score": 0.32, "stage": 0.5, "total": 0.32}),
    )

    asyncio.run(w._tick_momentum())

    assert len(captured) == 1, f"expected exactly one tick row, got {len(captured)}"
    rec = captured[0]
    assert "dqs" in rec, "tick row missing dqs key"
    assert rec["dqs"] == 0.32, f"expected dqs=0.32, got {rec['dqs']!r}"
    assert isinstance(rec["dqs"], float)
    assert 0.0 <= rec["dqs"] <= 1.0
    # Confirm no entry fired (DQS below threshold)
    w._auto_bet_momentum.assert_not_called()


def test_dqs_fail_decision_extra_includes_session96_context(monkeypatch):
    import asyncio
    from bot import game_context, live_watcher

    w = _build_momentum_watcher(
        sport="nba",
        ticker="KXNBAGAME-26MAY10TEST-LAL",
        price_history=[70, 70, 69, 70, 70],
        yes_ask=65,
        opp_yes_ask=34,
    )
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: None)
    monkeypatch.setattr(
        game_context, "compute_dip_quality",
        lambda **kwargs: (0.31, {"score": 0.31, "stage": 0.5, "total": 0.31}),
    )

    asyncio.run(w._tick_momentum())

    reject = next(
        c for c in w._log_decision_dampened.call_args_list
        if c.kwargs.get("reason") == "dqs_fail"
    )
    extra = reject.kwargs["extra"]
    assert extra["context_available"] is True
    assert extra["leader_ticker"] == "KXNBAGAME-26MAY10TEST-LAL"
    assert extra["leader_price"] == 65
    assert extra["opponent_price"] == 34
    assert extra["spread_cents"] == 1
    assert extra["recent_high"] == 70
    assert extra["dip_cents"] == 5
    assert extra["dqs"] == 0.31
    assert extra["skip_reason"] == "dqs_fail"
    assert "market" not in extra


def test_opponent_side_dip_too_big_logs_context_without_entry(monkeypatch):
    import asyncio
    from bot import live_watcher

    w = _build_momentum_watcher(
        sport="nba",
        ticker="KXNBAGAME-26MAY10TEST-LAL",
        price_history=[50, 50, 50, 50, 50],
        yes_ask=50,
        opp_yes_ask=65,
        opp_history=[90, 90, 90],
    )
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: None)

    asyncio.run(w._tick_momentum())

    w._auto_bet_momentum.assert_not_called()
    reject = next(
        c for c in w._log_decision_dampened.call_args_list
        if c.kwargs.get("reason") == "dip_too_big"
    )
    extra = reject.kwargs["extra"]
    assert extra["leader_side"] == "opponent"
    assert extra["leader_price"] == 65
    assert extra["opponent_price"] == 50
    assert extra["dip_cents"] == 25
    assert extra["skip_reason"] == "dip_too_big"


def test_dqs_null_when_not_computed(monkeypatch):
    """Session 33: when compute_dip_quality is not called, tick row has dqs=None.

    Drive a tick where yes_ask is at the recent_high (no dip), so the
    dip-eligibility check fails and compute_dip_quality is never invoked.
    Assert dqs is explicitly None — NOT 0, NOT missing key.
    """
    import asyncio
    from bot import live_watcher
    from bot import game_context

    # No dip: yes_ask=70 == recent_high=70 → dip_cents=0 < min_dip
    w = _build_momentum_watcher(
        sport="nba",
        ticker="KXNBAGAME-26APR30TEST-NO-DIP",
        price_history=[70, 70, 70, 70, 70],
        yes_ask=70,
    )

    captured: list[dict] = []
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: captured.append(dict(d)))

    # Sentinel: if compute_dip_quality is called, the test should fail loudly
    # rather than accidentally setting dqs_for_log.
    def _should_not_be_called(**kwargs):
        raise AssertionError("compute_dip_quality should not be called when no dip eligible")
    monkeypatch.setattr(game_context, "compute_dip_quality", _should_not_be_called)

    asyncio.run(w._tick_momentum())

    assert len(captured) == 1
    rec = captured[0]
    assert "dqs" in rec, "tick row missing dqs key — must be present (with null value), not absent"
    assert rec["dqs"] is None, f"expected dqs=None, got {rec['dqs']!r}"


def test_dqs_null_in_variance_quality_scalp_path(monkeypatch):
    """Session 33: tennis/UFC scalp path sets buy_dqs=1.0 (N/A sentinel) but
    dqs_for_log stays None — the tick row reflects 'DQS was not measured'.

    Drive an atp tick with skip_dqs sport profile, dip eligible, and the
    variance_quality_gate passing. Assert tick row dqs is None.
    """
    import asyncio
    from bot import live_watcher
    from bot import game_context
    from bot.config import SPORT_PROFILES

    # Tennis profile: skip_dqs=True, variance_quality_gate=True (per config.py)
    # Confirm the profile we're using: pick one with skip_dqs=True.
    sport_with_skip = None
    for sport, profile in SPORT_PROFILES.items():
        if profile.get("skip_dqs"):
            sport_with_skip = sport
            break
    assert sport_with_skip, "no skip_dqs sport profile found in SPORT_PROFILES"

    profile = SPORT_PROFILES[sport_with_skip]
    min_dip_for_sport = profile.get("min_dip", 4)

    # Build price history with a dip large enough to be eligible. atp profile
    # has min_dip=5, so use a 6c dip (recent_high=70, current=64).
    dip = max(min_dip_for_sport + 1, 6)
    history = [70] * 12  # plenty of ticks for variance gate
    w = _build_momentum_watcher(
        sport=sport_with_skip,
        ticker=f"KX{sport_with_skip.upper()}-26APR30TEST",
        price_history=history,
        yes_ask=70 - dip,
    )

    captured: list[dict] = []
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: captured.append(dict(d)))

    # Sentinel: compute_dip_quality must NOT be called in scalp path
    def _should_not_be_called(**kwargs):
        raise AssertionError("compute_dip_quality should not be called in skip_dqs scalp path")
    monkeypatch.setattr(game_context, "compute_dip_quality", _should_not_be_called)

    # MOMENTUM_DISABLED_SPORTS may include the chosen sport (atp/atp_challenger
    # /wta/wta_challenger were disabled Apr 20). Force can_enter through by
    # patching the disabled-sports set to empty for this test.
    monkeypatch.setattr(live_watcher, "MOMENTUM_DISABLED_SPORTS", set())

    asyncio.run(w._tick_momentum())

    assert len(captured) == 1
    rec = captured[0]
    assert "dqs" in rec
    assert rec["dqs"] is None, (
        f"expected dqs=None in scalp path (buy_dqs=1.0 is internal sentinel, "
        f"NOT the logged value), got {rec['dqs']!r}"
    )


def test_dqs_does_not_change_entry_decision(monkeypatch):
    """Session 33 behavior preservation: the new dqs_for_log capture does NOT
    alter buy_dqs / dqs_breakdown / the value passed to _auto_bet_momentum.

    Drive a tick where compute_dip_quality returns 0.85 (well above
    MOMENTUM_DQS_THRESHOLD=0.40). Assert _auto_bet_momentum is called with
    dqs_score=0.85 — i.e., the production-path buy_dqs is set to the real
    DQS, byte-identical to pre-Session-33 behavior. The dqs_for_log capture
    is a side-channel that does NOT bleed into entry-decision state.
    """
    import asyncio
    from bot import live_watcher
    from bot import game_context

    # Build NBA tick with eligible 5¢ dip
    w = _build_momentum_watcher(
        sport="nba",
        ticker="KXNBAGAME-26APR30TEST-CONVICTION",
        price_history=[70, 70, 69, 70, 70, 70],
        yes_ask=65,
    )

    captured: list[dict] = []
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: captured.append(dict(d)))

    # DQS WELL above threshold (0.40) → entry should fire
    monkeypatch.setattr(
        game_context, "compute_dip_quality",
        lambda **kwargs: (0.85, {"score": 1.0, "total": 0.85, "marker": "real-breakdown"}),
    )

    asyncio.run(w._tick_momentum())

    # The non-negotiable assertion: _auto_bet_momentum got the REAL DQS as
    # dqs_score, not 0.0 (default), not 1.0 (scalp sentinel), not None.
    w._auto_bet_momentum.assert_called_once()
    call_kwargs = w._auto_bet_momentum.call_args.kwargs
    assert call_kwargs.get("dqs_score") == 0.85, (
        f"dqs_score passed to _auto_bet_momentum should be the real DQS (0.85); "
        f"got {call_kwargs.get('dqs_score')!r}. This means the Session 33 change "
        f"contaminated buy_dqs — production-behavior preservation broken."
    )

    # And the tick row carries the same value
    assert len(captured) == 1
    assert captured[0]["dqs"] == 0.85


# ---------------------------------------------------------------------------
# Session 34: elapsed_seconds threaded through to _log_decision_dampened.extra
# so bot.regime.tag can populate match_phase on tennis/UFC/IPL live_momentum
# decisions.
# ---------------------------------------------------------------------------


def test_log_decision_extras_carry_elapsed_seconds(monkeypatch):
    """Drive a tick down the not-can-enter (position_open) reject path and
    verify the captured _log_decision_dampened extra dict carries
    elapsed_seconds. mom_ctx is the single feeder; if it's there for one
    site, it's there for all 5.

    Why position_open: it's the cheapest reject path. _check_exit is mocked
    so the held position doesn't trigger exit logic; we just want the
    not-can-enter dampener call to fire."""
    import asyncio
    from bot import live_watcher

    # Pre-existing held position blocks can_enter via the bets_placed branch.
    held = [{
        "ticker": "KXNBAGAME-26APR30TEST-LAL", "side": "yes", "price_cents": 70,
        "contracts": 1,
    }]
    w = _build_momentum_watcher(
        sport="nba",
        ticker="KXNBAGAME-26APR30TEST-LAL",
        price_history=[70, 70, 70, 70, 70],
        yes_ask=70,
        bets_placed=held,
    )

    # Pin elapsed: _started_at set 60s ago in _build_momentum_watcher; force
    # a known value so the assertion is stable across machine speeds.
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: w._started_at + 1200)  # 20 min in

    captured: list[dict] = []
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: captured.append(dict(d)))

    asyncio.run(w._tick_momentum())

    # Assert _log_decision_dampened was called at least once and the extra
    # carries elapsed_seconds matching our pinned value.
    assert w._log_decision_dampened.called, (
        "expected _log_decision_dampened to fire on the not-can-enter path"
    )
    found_with_elapsed = False
    for call in w._log_decision_dampened.call_args_list:
        extra = call.kwargs.get("extra") or {}
        if "elapsed_seconds" in extra:
            assert extra["elapsed_seconds"] == 1200, (
                f"expected elapsed_seconds=1200, got {extra['elapsed_seconds']!r}"
            )
            found_with_elapsed = True
    assert found_with_elapsed, (
        "no _log_decision_dampened call carried elapsed_seconds in extra — "
        "Session 34 plumbing regressed."
    )


# ---------------------------------------------------------------------------
# Session 90: re-entry-after-loss circuit breaker
# Mining of 6 multi-entry trades on 5 ticker-days through 2026-05-08 showed
# 0/4 WR after a loss-exit (PRIDJO 3xSL chain) vs 1/2 after a winning exit.
# Breaker blocks re-entry when any prior exit on this match was a loss; winning
# exits do not poison the loss memory. State lives in self._entry_losses.
# ---------------------------------------------------------------------------

def test_reentry_blocked_after_loss_recorded_session90(monkeypatch):
    """Pre-load _entry_losses with one losing exit and drive a dip-eligible
    tick. Entry must NOT fire; the dampener must log a reject with
    reason='reentry_blocked'."""
    import asyncio
    from bot import live_watcher
    from bot import game_context

    w = _build_momentum_watcher(
        sport="nba",
        ticker="KXNBAGAME-26MAY09TEST-LAL",
        price_history=[70, 70, 70, 70, 70],
        yes_ask=65,
        entry_count=1,  # one prior entry already happened
        cooldown=0,
        entry_losses=[{"reason": "STOP-LOSS: dropped 10c", "pnl": -2.0, "ts": 0.0}],
    )

    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: None)
    # DQS would pass if reached — confirms the breaker fires BEFORE DQS gate.
    monkeypatch.setattr(
        game_context, "compute_dip_quality",
        lambda **kwargs: (0.55, {"score": 0.55}),
    )

    asyncio.run(w._tick_momentum())

    # Entry must not have fired
    w._auto_bet_momentum.assert_not_called()

    # Reject decision with reason='reentry_blocked' must have been logged
    found_reentry = False
    for call in w._log_decision_dampened.call_args_list:
        if call.kwargs.get("reason") == "reentry_blocked":
            found_reentry = True
            extra = call.kwargs.get("extra") or {}
            assert extra.get("losses_seen") == 1, (
                f"expected losses_seen=1 in extra, got {extra.get('losses_seen')!r}"
            )
            assert extra.get("context_available") is True
            assert extra.get("leader_price") == 65
            assert extra.get("skip_reason") == "reentry_blocked"
            break
    assert found_reentry, (
        "no _log_decision_dampened call carried reason='reentry_blocked' — "
        f"got reasons={[c.kwargs.get('reason') for c in w._log_decision_dampened.call_args_list]}"
    )


def test_reentry_allowed_when_no_losses_recorded_session90(monkeypatch):
    """With _entry_losses empty (no prior loss-exits), a dip-eligible tick
    after a prior winning entry must still admit entry. Breaker must not
    over-block when the only prior exits were wins."""
    import asyncio
    from bot import live_watcher
    from bot import game_context

    w = _build_momentum_watcher(
        sport="nba",
        ticker="KXNBAGAME-26MAY09TEST-LAL",
        price_history=[70, 70, 70, 70, 70],
        yes_ask=65,
        entry_count=1,  # one prior winning entry
        cooldown=0,
        entry_losses=[],  # explicit: no losses recorded
    )

    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: None)
    monkeypatch.setattr(
        game_context, "compute_dip_quality",
        lambda **kwargs: (0.55, {"score": 0.55}),
    )

    asyncio.run(w._tick_momentum())

    # Entry must fire
    w._auto_bet_momentum.assert_called_once()


def test_loss_exit_appends_to_entry_losses_session90(monkeypatch):
    """When _check_exit removes a position with negative pnl, the just-appended
    exit_record must be observed and logged into self._entry_losses."""
    import asyncio
    from unittest.mock import AsyncMock
    from bot import live_watcher

    bet = {
        "ticker": "KXNBAGAME-26MAY09TEST-LAL", "side": "yes",
        "price_cents": 75, "contracts": 10,
    }
    w = _build_momentum_watcher(
        sport="nba",
        ticker=bet["ticker"],
        price_history=[75, 73, 70, 68, 65],
        yes_ask=65,
        bets_placed=[bet],
    )

    async def _mock_check_exit(*args, **kwargs):
        # Simulate a stop-loss exit: pop bet, append loss exit_record.
        w.bets_placed.remove(bet)
        w.exits.append({**bet, "reason": "STOP-LOSS: dropped 10c (75c -> 65c)", "pnl": -2.0})

    w._check_exit = AsyncMock(side_effect=_mock_check_exit)
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: None)

    asyncio.run(w._tick_momentum())

    assert len(w._entry_losses) == 1, (
        f"expected 1 loss recorded, got {len(w._entry_losses)}: {w._entry_losses}"
    )
    rec = w._entry_losses[0]
    assert rec["pnl"] == -2.0
    assert "STOP-LOSS" in rec["reason"]
    # Cooldown must also be set on exit (preserve existing behavior)
    assert w._cooldown_remaining == 5  # MOMENTUM_REENTRY_COOLDOWN


def test_winning_exit_does_not_pollute_entry_losses_session90(monkeypatch):
    """A winning exit (positive pnl) must NOT append to _entry_losses.
    Cooldown still gets set (preserving existing post-exit behavior)."""
    import asyncio
    from unittest.mock import AsyncMock
    from bot import live_watcher

    bet = {
        "ticker": "KXNBAGAME-26MAY09TEST-LAL", "side": "yes",
        "price_cents": 70, "contracts": 10,
    }
    w = _build_momentum_watcher(
        sport="nba",
        ticker=bet["ticker"],
        price_history=[70, 75, 80, 85, 90],
        yes_ask=90,
        bets_placed=[bet],
    )

    async def _mock_check_exit(*args, **kwargs):
        w.bets_placed.remove(bet)
        w.exits.append({**bet, "reason": "TAKE-PROFIT", "pnl": 2.0})

    w._check_exit = AsyncMock(side_effect=_mock_check_exit)
    monkeypatch.setattr(live_watcher, "_log_tick", lambda d: None)

    asyncio.run(w._tick_momentum())

    assert len(w._entry_losses) == 0, (
        f"winning exit must not pollute loss memory; got {w._entry_losses}"
    )
    # Cooldown still set
    assert w._cooldown_remaining == 5


# ---------------------------------------------------------------------------
# Session 21: scan_live_matches skip_reason instrumentation
# ---------------------------------------------------------------------------

def _today_ticker_date() -> str:
    """Build the YYJANDD-style date prefix _is_today_market accepts."""
    months = {1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
              7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"}
    today = datetime.now(timezone.utc).date()
    return f"{today.year % 100:02d}{months[today.month]}{today.day:02d}"


def _make_market(*, ticker, event_ticker, yes_ask=70, volume=10000, title="",
                 volume_24h=10000):
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "yes_ask": yes_ask,
        "volume": volume,
        "volume_24h": volume_24h,
        "title": title,
    }


def _make_event_pair(event_ticker, *, ticker_a=None, ticker_b=None,
                     yes_ask_a=70, yes_ask_b=30, volume_a=5000, volume_b=5000,
                     title_a="Player Alpha vs Player Beta",
                     title_b="Player Beta vs Player Alpha"):
    """Build a 2-sided event (A and B markets) for one event_ticker."""
    today = _today_ticker_date()
    ta = ticker_a or f"KXNBAGAME-{today}ALPHA-A"
    tb = ticker_b or f"KXNBAGAME-{today}ALPHA-B"
    return [
        _make_market(ticker=ta, event_ticker=event_ticker,
                     yes_ask=yes_ask_a, volume=volume_a, title=title_a),
        _make_market(ticker=tb, event_ticker=event_ticker,
                     yes_ask=yes_ask_b, volume=volume_b, title=title_b),
    ]


@pytest.fixture
def skip_reason_env(monkeypatch):
    """Reset live_watcher module state and patch external deps for scan tests."""
    from bot import live_watcher
    # Reset module-level scan caches so each test starts clean. Set
    # _prev_scan_date to TODAY so scan_live_matches' "new day" branch (which
    # clears _prev_scan_volumes + _recently_watched) does NOT fire — that
    # branch would wipe per-test pre-seeds.
    live_watcher._prev_scan_volumes.clear()
    live_watcher._recently_watched.clear()
    live_watcher._prev_scan_date = date.today().isoformat()

    # Patch series dicts so we only iterate ONE sport per test
    monkeypatch.setattr("bot.kalshi_series.MATCH_SERIES", {"nba": "KXNBAGAME"})
    monkeypatch.setattr("bot.kalshi_series.SPORTS_SERIES", {})

    # Capture journal writes
    captured: list[dict] = []

    def _capture(entry):
        captured.append(dict(entry))

    monkeypatch.setattr(live_watcher, "_journal_append", _capture)

    # Prevent real watcher startup (accept-path test) but preserve the real
    # static method `_extract_player_name` — scan_live_matches calls it BEFORE
    # constructing a watcher, so a bare MagicMock breaks player-name parsing.
    real_extract = live_watcher.LiveGameWatcher._extract_player_name
    fake_watcher_class = MagicMock()
    fake_watcher_class._extract_player_name = real_extract
    monkeypatch.setattr(live_watcher, "LiveGameWatcher", fake_watcher_class)

    yield {"captured": captured, "live_watcher": live_watcher}

    live_watcher._prev_scan_volumes.clear()
    live_watcher._recently_watched.clear()
    live_watcher._prev_scan_date = None


def _scan_records(captured: list[dict]) -> list[dict]:
    return [r for r in captured if r.get("event") == "scan_found"]


def _assert_scan_context(row: dict, *, reason: str, leader_price: int):
    extra = row.get("extra")
    assert isinstance(extra, dict), row
    assert extra["context_available"] is True
    assert extra["leader_price"] == leader_price
    assert extra["source"] == "scan"
    assert extra["entry_gate"] == reason
    assert extra["skip_reason"] == reason
    assert "missing_context_fields" in extra
    assert "market" not in extra


def _run_scan(markets):
    """Run scan_live_matches once with get_markets returning the given markets."""
    with patch("agent.kalshi_client.get_markets",
               return_value={"markets": markets}):
        asyncio.run(_dummy_scan())


async def _dummy_scan(active_watchers=None):
    from bot.live_watcher import scan_live_matches
    return await scan_live_matches(
        notifier=MagicMock(),
        active_watchers=active_watchers or {},
        balance=500.0,
    )


def _run_scan_with(markets, *, active_watchers=None):
    with patch("agent.kalshi_client.get_markets",
               return_value={"markets": markets}):
        asyncio.run(_dummy_scan(active_watchers=active_watchers))


class TestScanLiveMatchesSkipReason:
    """Session 21: every match-level gate writes the right skip_reason."""

    def test_bad_event_shape_records_skip_reason(self, skip_reason_env):
        # Single-sided event → bad_event_shape
        today = _today_ticker_date()
        m = _make_market(ticker=f"KXNBAGAME-{today}A-A",
                         event_ticker="KXNBAGAME-EV1")
        _run_scan_with([m])
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "bad_event_shape" for s in scans), scans
        row = next(s for s in scans if s["skip_reason"] == "bad_event_shape")
        _assert_scan_context(row, reason="bad_event_shape", leader_price=70)

    def test_low_volume_records_skip_reason(self, skip_reason_env):
        # Both sides volume below MIN_VOLUME_LIVE (1000)
        markets = _make_event_pair("KXNBAGAME-EV2",
                                   yes_ask_a=70, yes_ask_b=30,
                                   volume_a=100, volume_b=100)
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "low_volume" for s in scans), scans

    def test_not_today_records_skip_reason(self, skip_reason_env):
        # Ticker date in January (won't match today/yesterday-UTC)
        markets = [
            _make_market(ticker="KXNBAGAME-26JAN01ALPHA-A",
                         event_ticker="KXNBAGAME-EVOLD",
                         yes_ask=70, volume=5000),
            _make_market(ticker="KXNBAGAME-26JAN01ALPHA-B",
                         event_ticker="KXNBAGAME-EVOLD",
                         yes_ask=30, volume=5000),
        ]
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "not_today" for s in scans), scans
        row = next(s for s in scans if s["skip_reason"] == "not_today")
        _assert_scan_context(row, reason="not_today", leader_price=70)

    def test_no_leader_records_skip_reason(self, skip_reason_env):
        # Both sides below MOMENTUM_LEADER_MIN * 100 (post-Session-19c: 0.65)
        markets = _make_event_pair("KXNBAGAME-EV3",
                                   yes_ask_a=55, yes_ask_b=45,
                                   volume_a=5000, volume_b=5000)
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "no_leader" for s in scans), scans
        row = next(s for s in scans if s["skip_reason"] == "no_leader")
        assert row["extra"]["context_available"] is True
        assert row["extra"]["leader_price"] == 55
        assert row["extra"]["opponent_price"] == 45
        assert row["extra"]["volume_24h"] == 20000
        assert row["extra"]["entry_gate"] == "no_leader"
        assert "market" not in row["extra"]

    def test_settled_records_skip_reason(self, skip_reason_env):
        # Leader >= 95
        markets = _make_event_pair("KXNBAGAME-EV4",
                                   yes_ask_a=97, yes_ask_b=3,
                                   volume_a=5000, volume_b=5000)
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "settled" for s in scans), scans
        row = next(s for s in scans if s["skip_reason"] == "settled")
        _assert_scan_context(row, reason="settled", leader_price=97)

    def test_unknown_name_records_skip_reason(self, skip_reason_env):
        # Empty title AND ticker without "-" → no team abbrev → unknown_name
        today = _today_ticker_date()
        markets = [
            _make_market(ticker=f"KXNBAGAME{today}NONAMEA",
                         event_ticker="KXNBAGAME-EV5",
                         yes_ask=70, volume=5000, title=""),
            _make_market(ticker=f"KXNBAGAME{today}NONAMEB",
                         event_ticker="KXNBAGAME-EV5",
                         yes_ask=30, volume=5000, title=""),
        ]
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "unknown_name" for s in scans), scans
        row = next(s for s in scans if s["skip_reason"] == "unknown_name")
        _assert_scan_context(row, reason="unknown_name", leader_price=70)

    def test_already_watching_records_skip_reason(self, skip_reason_env):
        # active_watchers contains a watcher whose ticker matches the leader
        today = _today_ticker_date()
        leader_ticker = f"KXNBAGAME-{today}DUP-DUP"
        markets = _make_event_pair(
            "KXNBAGAME-EV6",
            ticker_a=leader_ticker,
            ticker_b=f"KXNBAGAME-{today}DUP-OTH",
            yes_ask_a=72, yes_ask_b=28,
            volume_a=5000, volume_b=5000,
        )
        existing = MagicMock()
        existing.ticker = leader_ticker
        active_watchers = {"alpha vs beta": (existing, None)}
        _run_scan_with(markets, active_watchers=active_watchers)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "already_watching" for s in scans), scans
        row = next(s for s in scans if s["skip_reason"] == "already_watching")
        _assert_scan_context(row, reason="already_watching", leader_price=72)

    def test_recently_watched_records_skip_reason(self, skip_reason_env):
        from bot import live_watcher
        live_watcher._recently_watched.add("KXNBAGAME-EV7")
        markets = _make_event_pair(
            "KXNBAGAME-EV7",
            yes_ask_a=72, yes_ask_b=28,
            volume_a=5000, volume_b=5000,
        )
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "recently_watched" for s in scans), scans
        row = next(s for s in scans if s["skip_reason"] == "recently_watched")
        _assert_scan_context(row, reason="recently_watched", leader_price=72)

    def test_no_vol_growth_first_seen_records_skip_reason(self, skip_reason_env):
        # _prev_scan_volumes empty → first sighting → no_vol_growth_first_seen
        markets = _make_event_pair(
            "KXNBAGAME-EV8",
            yes_ask_a=72, yes_ask_b=28,
            volume_a=5000, volume_b=5000,
        )
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "no_vol_growth_first_seen" for s in scans), scans

    def test_no_vol_growth_idle_records_skip_reason(self, skip_reason_env):
        from bot import live_watcher
        today = _today_ticker_date()
        leader_ticker = f"KXNBAGAME-{today}IDL-IDL"
        # Pre-seed with current_vol - 100 → growth = 100 < 500 → idle
        live_watcher._prev_scan_volumes[leader_ticker] = 9900
        markets = _make_event_pair(
            "KXNBAGAME-EV9",
            ticker_a=leader_ticker,
            ticker_b=f"KXNBAGAME-{today}IDL-OTH",
            yes_ask_a=72, yes_ask_b=28,
            volume_a=10000, volume_b=0,
        )
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "no_vol_growth_idle" for s in scans), scans

    def test_capacity_capped_records_skip_reason(self, skip_reason_env):
        from bot import live_watcher
        # Fill active_watchers to MAX_AUTO_WATCHERS so slots = 0
        active_watchers = {
            f"q{i}": (MagicMock(ticker=f"OTHER-{i}"), None)
            for i in range(live_watcher.MAX_AUTO_WATCHERS)
        }
        # No markets needed — early return at slots <= 0 means we never reach
        # any gate. Confirm this branch doesn't silently break (no records,
        # not an exception).
        _run_scan_with([], active_watchers=active_watchers)
        # Now exercise the post-eligibility cap with slots > 0 but more
        # candidates than slots. Pre-seed enough tickers in
        # _prev_scan_volumes so the volume-growth filter passes. Keep
        # _prev_scan_date pinned to today so scan_live_matches doesn't
        # wipe the pre-seeds via its "new day" branch.
        live_watcher._prev_scan_volumes.clear()
        live_watcher._recently_watched.clear()
        skip_reason_env["captured"].clear()

        today = _today_ticker_date()
        markets: list[dict] = []
        for i in range(live_watcher.MAX_AUTO_WATCHERS + 2):
            ev = f"KXNBAGAME-CAP{i}"
            la = f"KXNBAGAME-{today}CAP{i}-A"
            lb = f"KXNBAGAME-{today}CAP{i}-B"
            markets.extend([
                _make_market(ticker=la, event_ticker=ev, yes_ask=72,
                             volume=10000, title=f"Cap{i} Alpha vs Beta"),
                _make_market(ticker=lb, event_ticker=ev, yes_ask=28,
                             volume=5000, title=f"Cap{i} Beta vs Alpha"),
            ])
            # Pre-seed prev_vol below current so growth check passes
            live_watcher._prev_scan_volumes[la] = 1000
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "capacity_capped" for s in scans), scans

    def test_accept_records_skip_reason_none(self, skip_reason_env):
        """Match passes every gate → scan_found has skip_reason=None."""
        from bot import live_watcher
        today = _today_ticker_date()
        leader_ticker = f"KXNBAGAME-{today}OK-OK"
        # Pre-seed prev_vol so the growth check passes (current=10000 > prev+500)
        live_watcher._prev_scan_volumes[leader_ticker] = 1000
        markets = _make_event_pair(
            "KXNBAGAME-EVOK",
            ticker_a=leader_ticker,
            ticker_b=f"KXNBAGAME-{today}OK-OTH",
            yes_ask_a=72, yes_ask_b=28,
            volume_a=10000, volume_b=5000,
        )
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        # Among the scan_found records, exactly one for the accept path with
        # skip_reason=None for our leader_ticker
        accepted = [s for s in scans
                    if s.get("skip_reason") is None
                    and s.get("ticker") == leader_ticker]
        assert len(accepted) == 1, (
            f"expected one scan_found(skip_reason=None) for {leader_ticker}, "
            f"got {scans}"
        )

    def test_journal_record_has_required_fields(self, skip_reason_env):
        """Every scan_found record carries event/ticker/sport/skip_reason."""
        markets = _make_event_pair(
            "KXNBAGAME-EVF",
            yes_ask_a=55, yes_ask_b=45,  # triggers no_leader
            volume_a=5000, volume_b=5000,
        )
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert scans, "expected at least one scan_found record"
        for s in scans:
            assert s["event"] == "scan_found"
            assert "ticker" in s
            assert "sport" in s
            assert "skip_reason" in s  # explicitly present, may be None


# ---------------------------------------------------------------------------
# Session 56 (2026-05-06) — LIVE_TRAILING_STOP / _trailing_active dead-code
# regression. CLAUDE.md Session 19a flagged the unfixed reference; Session 56
# cleaned it up. These tests pin the cleanup so a future session can't
# resurrect the dead infrastructure without explicit attention.
# ---------------------------------------------------------------------------

class TestSession56DeadInfraRemoval:
    """Pins removal of LIVE_TRAILING_STOP + _trailing_active dead-code in
    bot/live_watcher.py. Pattern matches Session 51's
    test_canonical_schema_used_throughout discipline — source-level grep
    asserts the literal string is absent."""

    def _live_watcher_source(self) -> str:
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "bot" / "live_watcher.py"
        return src.read_text()

    def test_live_trailing_stop_constant_not_referenced(self):
        """
        LIVE_TRAILING_STOP was removed from bot/config.py in Session 18.5.
        Session 56 deleted the dangling reference inside an unreachable
        status-card branch in bot/live_watcher.py (line 2746 pre-fix). If
        this test fails, someone reintroduced a reference to a config
        constant that no longer exists — NameError waiting to fire.
        """
        text = self._live_watcher_source()
        assert "LIVE_TRAILING_STOP" not in text, (
            "LIVE_TRAILING_STOP literal found in bot/live_watcher.py. "
            "The constant was deleted from bot/config.py in Session 18.5; "
            "any reference is a NameError. See CLAUDE.md Session 56."
        )

    def test_trailing_active_attribute_removed(self):
        """
        _trailing_active was an instance dict initialized to {} but never
        written to (no `_trailing_active[ticker] = True` anywhere in the
        codebase). All reads returned falsy; both consuming branches were
        dead. Session 56 deleted the init, the .pop(), and both reads. If
        this test fails, someone resurrected dead infrastructure — find out
        why and consider whether they actually need a working trailing-stop
        flag instead.
        """
        text = self._live_watcher_source()
        assert "_trailing_active" not in text, (
            "_trailing_active reference found in bot/live_watcher.py. "
            "Session 56 deleted this dead infrastructure. The live "
            "trailing-stop logic at _check_exit uses MOMENTUM_DQS_TRAIL_STOP "
            "and is the actual production trailing-stop path."
        )


# ---------------------------------------------------------------------------
# Session 99 — fair-value proxy fields land on live_momentum decisions
# ---------------------------------------------------------------------------

def test_session_99_proxy_fields_ride_through_annotate_helper():
    """The merge at live_watcher.py:1422-1453 attaches estimated_win_prob,
    model_source, confidence_components to primary_context/opponent_context.
    `_annotate_live_momentum_context` must preserve those keys (it starts
    with `out = dict(context or {})` per the Session 96 contract — this
    test locks that pass-through so a future refactor can't accidentally
    drop the proxy fields)."""
    from bot.live_watcher import _annotate_live_momentum_context
    from bot.live_momentum_proxy import estimate_live_momentum_win_prob, MODEL_SOURCE

    # Build a minimal pre-proxy context with the two required inputs.
    pre_proxy = {
        "wp_edge": 0.05,
        "leader_price": 60,
        "sport": "nba",
        "ticker": "KXNBAGAME-26MAY10TEST-LAL",
        "context_available": True,
    }
    proxy = estimate_live_momentum_win_prob(pre_proxy)
    pre_proxy["estimated_win_prob"] = proxy["estimated_win_prob"]
    pre_proxy["model_source"] = proxy["model_source"]
    pre_proxy["confidence_components"] = proxy["confidence_components"]

    out = _annotate_live_momentum_context(
        pre_proxy, skip_reason="position_open", entry_gate="position_open",
    )

    assert out["estimated_win_prob"] == 0.65
    assert out["model_source"] == MODEL_SOURCE
    assert out["confidence_components"]["wp_edge"] == 0.05
    assert out["confidence_components"]["market_implied"] == 0.6
    assert out["confidence_components"]["clamped"] is False
    # Late-bound labels still apply correctly on top of the proxy keys.
    assert out["skip_reason"] == "position_open"
    assert out["entry_gate"] == "position_open"


def test_session_99_proxy_fields_appear_in_decisions_jsonl_extras(tmp_path, monkeypatch):
    """End-to-end: a `_log_decision_dampened` call with a proxy-enriched
    extra dict writes estimated_win_prob / model_source / confidence_components
    into the decisions.jsonl record's `extra`. Mirrors the
    test_log_decision_dampened_writes_wp_edge_and_mom_ctx precedent."""
    import json
    from bot import decisions
    import bot.shadow_trades as shadow
    from bot.live_watcher import LiveGameWatcher, _annotate_live_momentum_context
    from bot.live_momentum_proxy import estimate_live_momentum_win_prob, MODEL_SOURCE

    f = tmp_path / "decisions.jsonl"
    shadow_f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", shadow_f)

    # Build the proxy-enriched primary_context the way live_watcher does
    # at lines 1422-1453.
    primary_context = {
        "wp_edge": 0.08,
        "leader_price": 72,
        "sport": "nba",
        "ticker": "KXNBAGAME-26MAY10TEST-LAL",
        "leader_side": "primary",
        "context_available": True,
    }
    proxy = estimate_live_momentum_win_prob(primary_context)
    primary_context["estimated_win_prob"] = proxy["estimated_win_prob"]
    primary_context["model_source"] = proxy["model_source"]
    primary_context["confidence_components"] = proxy["confidence_components"]

    w = LiveGameWatcher.__new__(LiveGameWatcher)
    w.ticker = "KXNBAGAME-26MAY10TEST-LAL"
    w._last_decision = (None, None)

    # Simulate a reject path identical to live_watcher.py:1476-1489
    # ("cannot_enter" → no can_enter branch).
    w._log_decision_dampened(
        decision="reject", reason="position_open",
        gates={"can_enter": False, "sport_enabled": True, "position_open": False},
        edge=0.08,
        extra={
            **_annotate_live_momentum_context(
                primary_context, skip_reason="position_open",
                entry_gate="position_open",
            ),
            "sport": "nba",
        },
    )

    rec = json.loads(f.read_text().splitlines()[0])
    assert rec["opp_type"] == "live_momentum"
    assert rec["decision"] == "reject"
    extra = rec["extra"]
    # Proxy fields land in the persisted record.
    assert extra["estimated_win_prob"] == 0.80
    assert extra["model_source"] == MODEL_SOURCE
    assert extra["confidence_components"]["wp_edge"] == 0.08
    assert extra["confidence_components"]["market_implied"] == 0.72


def test_session_99_proxy_returns_none_when_game_ctx_absent():
    """At scan-time call sites (3320/3382/3544/3587/3633/3659), game_ctx is
    None so wp_edge is also None. The proxy must correctly return
    estimated_win_prob=None for those records — never a phantom scalar."""
    from bot.live_watcher import _build_live_momentum_decision_context
    from bot.live_momentum_proxy import estimate_live_momentum_win_prob

    # Mimic a scan-time call (no game_ctx, only market + ticker).
    ctx = _build_live_momentum_decision_context(
        sport="ufc",
        ticker="KXUFCFIGHT-26MAY10TEST-AAA",
        leader_market={"ticker": "KXUFCFIGHT-26MAY10TEST-AAA", "yes_ask": 60,
                       "yes_bid": 58, "volume_24h": 500},
        leader_price=60,
        source="scan",
    )
    # leader_price present but wp_edge absent (no game_ctx → no win_probability).
    proxy = estimate_live_momentum_win_prob(ctx)
    assert proxy["estimated_win_prob"] is None
    assert proxy["confidence_components"]["reason"] == "missing_required_input"
    assert proxy["confidence_components"]["wp_edge_present"] is False
    assert proxy["confidence_components"]["leader_price_present"] is True
