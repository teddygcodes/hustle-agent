"""Tests for bot quality improvements (5 → 8)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Task 1: Outcome Tracker ──────────────────────────────────────────────────

def test_alert_stored_returns_id():
    """store_alert() must return a positive integer ID."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXNBAGAME-26APR05LACSAC-SAC",
            "type": "series_game_edge",
            "edge": 0.23,
            "relative_edge": 0.23,
            "confidence": 0.70,
            "recommended_side": "yes",
            "kalshi_price": 0.14,
            "market": {"close_time": "2026-04-13T00:00:00Z"},
        })
        assert isinstance(alert_id, int) and alert_id > 0
    finally:
        os.unlink(db_path)


def test_resolution_records_win():
    """record_resolution() with YES result on BUY YES alert marks won=True."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXTEST-YES",
            "type": "weather",
            "edge": 0.10,
            "relative_edge": 0.30,
            "confidence": 0.75,
            "recommended_side": "yes",
            "kalshi_price": 0.10,
            "market": {"close_time": "2026-04-05T05:00:00Z"},
        })
        tracker.record_resolution(alert_id, result="yes")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 1
        assert stats["total"] == 1
        assert stats["win_rate"] == 1.0
    finally:
        os.unlink(db_path)


def test_resolution_records_loss():
    """record_resolution() with NO result on BUY YES alert marks won=False."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        alert_id = tracker.store_alert({
            "ticker": "KXTEST-LOSS",
            "type": "weather",
            "edge": 0.10,
            "relative_edge": 0.30,
            "confidence": 0.75,
            "recommended_side": "yes",
            "kalshi_price": 0.10,
            "market": {"close_time": "2026-04-05T05:00:00Z"},
        })
        tracker.record_resolution(alert_id, result="no")
        stats = tracker.get_stats("weather")
        assert stats["wins"] == 0
        assert stats["win_rate"] == 0.0
    finally:
        os.unlink(db_path)


def test_calibration_flags_underperforming_strategy():
    """get_calibration_report() flags strategies with win_rate < expected after 50+ samples."""
    from bot.outcome_tracker import OutcomeTracker
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        tracker = OutcomeTracker(db_path=db_path)
        # 50 alerts at 25% edge but only 30% wins (expected ~65%, actual 30%)
        for i in range(50):
            aid = tracker.store_alert({
                "ticker": f"KXTEST-{i}",
                "type": "series_game_edge",
                "edge": 0.25,
                "relative_edge": 0.25,
                "confidence": 0.70,
                "recommended_side": "yes",
                "kalshi_price": 0.40,
                "market": {"close_time": "2026-04-05T00:00:00Z"},
            })
            tracker.record_resolution(aid, result="yes" if i < 15 else "no")
        report = tracker.get_calibration_report()
        assert "series_game_edge" in report
        assert report["series_game_edge"]["flagged"] is True
    finally:
        os.unlink(db_path)


# ── Task 2: Price Monitor ─────────────────────────────────────────────────────

def test_price_cache_stores_and_retrieves():
    """PriceMonitor stores a price and retrieves it on next call."""
    import tempfile, json
    from bot.price_monitor import PriceMonitor
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({}, f)
        cache_path = f.name
    try:
        monitor = PriceMonitor(cache_path=cache_path)
        monitor.update("KXTEST-T67", yes_ask=27)
        cached = monitor.get_cached("KXTEST-T67")
        assert cached is not None
        assert cached["yes_ask"] == 27
    finally:
        os.unlink(cache_path)


def test_price_moving_against_yes_adds_warning():
    """BUY YES: price rising >5¢ since last scan adds warning and reduces confidence."""
    import tempfile, json
    from bot.price_monitor import PriceMonitor
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({}, f)
        cache_path = f.name
    try:
        monitor = PriceMonitor(cache_path=cache_path)
        # Previously priced at 14¢, now at 20¢ — market correcting against BUY YES
        monitor.update("KXTEST-SAC", yes_ask=14)
        opp = {
            "ticker": "KXTEST-SAC",
            "recommended_side": "yes",
            "confidence": 0.70,
            "warnings": [],
        }
        result = monitor.annotate(opp, current_yes_ask=20)
        assert any("moving against" in w.lower() for w in result["warnings"])
        assert result["confidence"] < 0.70
    finally:
        os.unlink(cache_path)


def test_price_small_movement_no_penalty():
    """Price movement <=3¢ against position produces no warning."""
    import tempfile, json
    from bot.price_monitor import PriceMonitor
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({}, f)
        cache_path = f.name
    try:
        monitor = PriceMonitor(cache_path=cache_path)
        monitor.update("KXTEST-SMALL", yes_ask=14)
        opp = {
            "ticker": "KXTEST-SMALL",
            "recommended_side": "yes",
            "confidence": 0.70,
            "warnings": [],
        }
        result = monitor.annotate(opp, current_yes_ask=16)  # only 2¢ rise
        assert result["warnings"] == []
        assert result["confidence"] == 0.70
    finally:
        os.unlink(cache_path)


def test_price_warn_only_band_no_confidence_penalty():
    """4¢ movement against YES: warning added but confidence unchanged."""
    import tempfile, json
    from bot.price_monitor import PriceMonitor
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({}, f)
        cache_path = f.name
    try:
        monitor = PriceMonitor(cache_path=cache_path)
        monitor.update("KXTEST-WARN", yes_ask=14)
        opp = {
            "ticker": "KXTEST-WARN",
            "recommended_side": "yes",
            "confidence": 0.70,
            "warnings": [],
        }
        result = monitor.annotate(opp, current_yes_ask=18)  # 4¢ rise — warn only
        assert any("moving against" in w.lower() for w in result["warnings"])
        assert result["confidence"] == 0.70  # no penalty at this band
    finally:
        os.unlink(cache_path)


def test_price_moving_against_no_position():
    """BUY NO: YES price falling >5¢ = warning and reduced confidence."""
    import tempfile, json
    from bot.price_monitor import PriceMonitor
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({}, f)
        cache_path = f.name
    try:
        monitor = PriceMonitor(cache_path=cache_path)
        monitor.update("KXTEST-NO", yes_ask=70)  # prior YES price
        opp = {
            "ticker": "KXTEST-NO",
            "recommended_side": "no",
            "confidence": 0.70,
            "warnings": [],
        }
        # YES price fell to 60¢ (10¢ drop) — bad for BUY NO holder
        result = monitor.annotate(opp, current_yes_ask=60)
        assert any("moving against" in w.lower() for w in result["warnings"])
        assert result["confidence"] < 0.70
    finally:
        os.unlink(cache_path)


# ── Task 4: Dynamic Weather Sigma ────────────────────────────────────────────

def test_weather_sigma_scales_with_days_ahead():
    """calculate_weather_edge() uses larger sigma for farther-out forecasts."""
    from bot.math_engine import calculate_weather_edge
    # Same setup: NYC, forecast 72°F, threshold 75°F, direction "above", price 20¢
    edge_1day = calculate_weather_edge("New York", 72.0, 75.0, "above", 20, days_ahead=1)
    edge_3day = calculate_weather_edge("New York", 72.0, 75.0, "above", 20, days_ahead=3)
    # With larger sigma on 3-day, P(above) is higher → but so is uncertainty
    # The key: fair_value should differ between 1-day and 3-day
    assert edge_1day["fair_value"] != edge_3day["fair_value"]


def test_weather_1day_sigma_unchanged():
    """days_ahead=1 (default) produces same result as before (sigma=2.0)."""
    from bot.math_engine import calculate_weather_edge
    edge_default = calculate_weather_edge("New York", 72.0, 75.0, "above", 20)
    edge_explicit = calculate_weather_edge("New York", 72.0, 75.0, "above", 20, days_ahead=1)
    assert edge_default["fair_value"] == edge_explicit["fair_value"]


def test_weather_3day_lower_edge_than_1day():
    """3-day forecast produces lower edge than 1-day for the same gap (more uncertainty)."""
    from bot.math_engine import calculate_weather_edge
    # Forecast 72°F, threshold 75°F (3°F gap), BUY YES at 20¢
    # With more uncertainty (3-day), fair_value approaches 50% → less edge
    edge_1day = calculate_weather_edge("New York", 72.0, 75.0, "above", 20, days_ahead=1)
    edge_3day = calculate_weather_edge("New York", 72.0, 75.0, "above", 20, days_ahead=3)
    # When temp is below threshold, more uncertainty means higher P(above) — closer to 50%
    # So for a BUY YES case where forecast < threshold, 3-day means P(above) is HIGHER
    # BUT: for a case where the forecast IS above threshold (clear edge), 3-day should reduce edge
    edge_1day_clear = calculate_weather_edge("New York", 80.0, 75.0, "above", 85, days_ahead=1)
    edge_3day_clear = calculate_weather_edge("New York", 80.0, 75.0, "above", 85, days_ahead=3)
    # When forecast (80) >> threshold (75), 1-day has tighter distribution → higher confidence
    # 3-day has wider sigma → P(above) is still high but closer to 50% → lower edge at same price
    assert abs(edge_1day_clear.get("edge", 0)) >= abs(edge_3day_clear.get("edge", 0))


# ── Task 3: Home/Away Modifier ───────────────────────────────────────────────

def test_home_team_confidence_boost():
    """Home team opportunity gets +0.03 confidence boost."""
    from bot.scanner import _apply_home_away_modifier
    from bot import kalshi_series
    # Inject a fake game map entry
    kalshi_series._ODDS_API_GAME_MAP["nba"] = {
        "lakers": {"home_team": "lakers", "away_team": "celtics", "is_b2b": False}
    }
    opp = {
        "ticker": "KXNBAGAME-26APR05LAKCEL-LAK",
        "type": "series_game_edge",
        "confidence": 0.70,
        "team": "lakers",
        "sport": "nba",
    }
    result = _apply_home_away_modifier(opp)
    assert abs(result["confidence"] - 0.73) < 0.001


def test_away_team_confidence_penalty():
    """Away team opportunity gets -0.03 confidence penalty."""
    from bot.scanner import _apply_home_away_modifier
    from bot import kalshi_series
    kalshi_series._ODDS_API_GAME_MAP["nba"] = {
        "celtics": {"home_team": "lakers", "away_team": "celtics", "is_b2b": False}
    }
    opp = {
        "ticker": "KXNBAGAME-26APR05LAKCEL-CEL",
        "type": "series_game_edge",
        "confidence": 0.70,
        "team": "celtics",
        "sport": "nba",
    }
    result = _apply_home_away_modifier(opp)
    assert abs(result["confidence"] - 0.67) < 0.001


def test_away_b2b_stacks_penalty():
    """Away team on B2B gets -0.03 - 0.05 = -0.08 total penalty."""
    from bot.scanner import _apply_home_away_modifier
    from bot import kalshi_series
    kalshi_series._ODDS_API_GAME_MAP["nba"] = {
        "celtics": {"home_team": "lakers", "away_team": "celtics", "is_b2b": True}
    }
    opp = {
        "ticker": "KXNBAGAME-26APR05LAKCEL-CEL",
        "type": "series_game_edge",
        "confidence": 0.70,
        "team": "celtics",
        "sport": "nba",
    }
    result = _apply_home_away_modifier(opp)
    assert abs(result["confidence"] - 0.62) < 0.001


# ── Task 5: Correlated Vig Stack Cap ─────────────────────────────────────────

def test_single_vig_signal_uncapped():
    """Single vig stack signal for a series remains uncapped."""
    from bot.scanner import _cap_correlated_vig_stack
    opps = [{"ticker": "KXHIGHNY-26APR06", "type": "vig_stack", "recommended_contracts": 10}]
    result = _cap_correlated_vig_stack(opps)
    assert result[0]["recommended_contracts"] == 10


def test_multiple_correlated_vig_signals_capped():
    """3 correlated signals for same series each get capped to ~3 contracts."""
    from bot.scanner import _cap_correlated_vig_stack
    opps = [
        {"ticker": "KXHIGHNY-26APR06T67", "type": "vig_stack", "recommended_contracts": 10},
        {"ticker": "KXHIGHNY-26APR07T69", "type": "vig_stack", "recommended_contracts": 10},
        {"ticker": "KXHIGHNY-26APR08T71", "type": "vig_stack", "recommended_contracts": 10},
    ]
    result = _cap_correlated_vig_stack(opps)
    # Each should be capped: 10 // 3 = 3
    for opp in result:
        assert opp["recommended_contracts"] == 3


def test_cap_distributes_evenly():
    """2 correlated signals for same series each get half the contracts."""
    from bot.scanner import _cap_correlated_vig_stack
    opps = [
        {"ticker": "KXHIGHCHI-26APR06T50", "type": "vig_stack", "recommended_contracts": 8},
        {"ticker": "KXHIGHCHI-26APR07T52", "type": "vig_stack", "recommended_contracts": 8},
    ]
    result = _cap_correlated_vig_stack(opps)
    assert result[0]["recommended_contracts"] == 4
    assert result[1]["recommended_contracts"] == 4
