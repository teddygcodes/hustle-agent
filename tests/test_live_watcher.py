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

    def test_no_leader_records_skip_reason(self, skip_reason_env):
        # Both sides below MOMENTUM_LEADER_MIN * 100 (post-Session-19c: 0.65)
        markets = _make_event_pair("KXNBAGAME-EV3",
                                   yes_ask_a=55, yes_ask_b=45,
                                   volume_a=5000, volume_b=5000)
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "no_leader" for s in scans), scans

    def test_settled_records_skip_reason(self, skip_reason_env):
        # Leader >= 95
        markets = _make_event_pair("KXNBAGAME-EV4",
                                   yes_ask_a=97, yes_ask_b=3,
                                   volume_a=5000, volume_b=5000)
        _run_scan_with(markets)
        scans = _scan_records(skip_reason_env["captured"])
        assert any(s["skip_reason"] == "settled" for s in scans), scans

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
