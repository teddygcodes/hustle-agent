"""Tests for Live Game Watcher (WATCH command)."""

from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Task 1: Config constants
# ---------------------------------------------------------------------------

def test_live_watch_config():
    from bot.config import LIVE_POLL_INTERVAL, LIVE_WATCH_EDGE_THRESHOLD
    assert LIVE_POLL_INTERVAL == 10
    assert 0.05 <= LIVE_WATCH_EDGE_THRESHOLD <= 0.20


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
    from bot.live_watcher import LiveGameWatcher
    watcher = LiveGameWatcher.__new__(LiveGameWatcher)
    watcher.query = "lakers"
    watcher.bets_placed = []
    watcher.exits = [{"side": "yes", "contracts": 10, "price_cents": 55,
                       "ticker": "X", "order_id": "P", "reason": "edge faded", "pnl": 2.30}]
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
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)

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
    from bot.live_watcher import LiveGameWatcher

    f = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(decisions, "DECISIONS_FILE", f)
    monkeypatch.setattr("bot.decisions.BOT_STATE_DIR", tmp_path)

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
