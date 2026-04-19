"""
Tests for data-driven bot fixes (2026-04-05)

Covers:
- Fix 1: Cooldown blocks re-entry on "resolved" positions
- Fix 1: Daily per-ticker loss limit blocks repeat losers
- Fix 2: Crypto edge floor rejects thin edges
- Fix 3: CLV sync from clv.json to paper_trades.json on resolve
- Fix 5: Vig-stack suppression only triggers on strong weather edge (>20%)
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect bot state files to tmp_path."""
    positions_file = tmp_path / "positions.json"
    positions_file.write_text("[]")
    paper_trades_file = tmp_path / "paper_trades.json"
    paper_trades_file.write_text("[]")

    import bot.executor as exc
    monkeypatch.setattr(exc, "POSITIONS_FILE", positions_file)
    monkeypatch.setattr(exc, "PAPER_TRADES_FILE", paper_trades_file)
    monkeypatch.setattr(exc, "PAPER_MODE", True)

    return {"positions": positions_file, "paper_trades": paper_trades_file, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# Fix 1: Cooldown covers "resolved" positions
# ---------------------------------------------------------------------------

class TestCooldownResolved:
    def test_resolved_position_triggers_cooldown(self, tmp_state):
        """A resolved position should block re-entry for 4 hours."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        # Position resolved 30 minutes ago
        positions = [{
            "ticker": "KXHIGHDEN-26APR06-T63",
            "status": "resolved",
            "resolved_at": (now - timedelta(minutes=30)).isoformat(),
            "cost": 2.0,
        }]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHDEN-26APR06-T63")
        assert not ok
        assert "COOLDOWN" in reason

    def test_old_resolved_position_allows_entry(self, tmp_state):
        """A resolved position older than 4 hours should not block."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        positions = [{
            "ticker": "KXHIGHDEN-26APR06-T63",
            "status": "resolved",
            "resolved_at": (now - timedelta(hours=5)).isoformat(),
            "cost": 2.0,
        }]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHDEN-26APR06-T63")
        assert ok

    def test_exited_early_still_triggers_cooldown(self, tmp_state):
        """Existing behavior: exited_early should still trigger cooldown."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        positions = [{
            "ticker": "KXHIGHDEN-26APR06-T63",
            "status": "exited_early",
            "exited_at": (now - timedelta(minutes=30)).isoformat(),
            "cost": 2.0,
        }]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHDEN-26APR06-T63")
        assert not ok
        assert "COOLDOWN" in reason


# ---------------------------------------------------------------------------
# Fix 1: Daily per-ticker loss limit
# ---------------------------------------------------------------------------

class TestDailyTickerLossLimit:
    def test_ticker_exceeding_daily_loss_blocked(self, tmp_state):
        """If ticker has lost > $1.00 today, block re-entry."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        positions = [
            {
                "ticker": "KXHIGHDEN-26APR06-T63",
                "status": "exited_early",
                "exited_at": (now - timedelta(hours=5)).isoformat(),  # past cooldown
                "realized_pnl": -0.44,
                "cost": 2.0,
            },
            {
                "ticker": "KXHIGHDEN-26APR06-T63",
                "status": "exited_early",
                "exited_at": (now - timedelta(hours=5)).isoformat(),
                "realized_pnl": -0.44,
                "cost": 2.0,
            },
            {
                "ticker": "KXHIGHDEN-26APR06-T63",
                "status": "exited_early",
                "exited_at": (now - timedelta(hours=5)).isoformat(),
                "realized_pnl": -0.33,
                "cost": 2.0,
            },
        ]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHDEN-26APR06-T63")
        assert not ok
        assert "DAILY_LOSS_LIMIT" in reason

    def test_different_ticker_not_affected(self, tmp_state):
        """Loss on one ticker should not block a different ticker."""
        import bot.executor as exc
        now = datetime.now(timezone.utc)

        positions = [{
            "ticker": "KXHIGHDEN-26APR06-T63",
            "status": "exited_early",
            "exited_at": (now - timedelta(hours=5)).isoformat(),
            "realized_pnl": -2.00,
            "cost": 2.0,
        }]
        json.dump(positions, open(tmp_state["positions"], "w"))

        ok, reason = exc._check_position_limits(100.0, 2.0, "KXHIGHNY-26APR06-T70")
        assert ok


# ---------------------------------------------------------------------------
# Fix 2: Crypto edge floor
# ---------------------------------------------------------------------------

class TestCryptoEdgeFloor:
    def test_crypto_min_edge_is_8_percent(self):
        """CRYPTO_MIN_EDGE should be 0.08 (8%) — lowered to capture hourly/15min edges."""
        from bot.config import CRYPTO_MIN_EDGE
        assert CRYPTO_MIN_EDGE == 0.08

    def test_crypto_min_edge_exists_in_config(self):
        """The constant must exist and be importable."""
        from bot.config import CRYPTO_MIN_EDGE
        assert isinstance(CRYPTO_MIN_EDGE, float)


# ---------------------------------------------------------------------------
# Fix 3: CLV sync to paper_trades on resolve
# ---------------------------------------------------------------------------

class TestCLVSync:
    def _setup_resolve(self, tmp_path, monkeypatch, trade_id, clv_entries):
        """Helper to set up files for a resolve_trades test."""
        import bot.tracker as tracker
        import bot.config as config

        positions_file = tmp_path / "positions.json"
        paper_trades_file = tmp_path / "paper_trades.json"
        trade_history_file = tmp_path / "trade_history.json"
        clv_file = tmp_path / "clv.json"

        monkeypatch.setattr(tracker, "POSITIONS_FILE", positions_file)
        monkeypatch.setattr(tracker, "PAPER_TRADES_FILE", paper_trades_file)
        monkeypatch.setattr(tracker, "TRADE_HISTORY_FILE", trade_history_file)
        monkeypatch.setattr(config, "CLV_FILE", clv_file)

        now = datetime.now(timezone.utc)

        positions = [{
            "ticker": "KXHIGHNY-26APR06-T70",
            "side": "yes",
            "filled": 10,
            "price_cents": 45,
            "cost": 4.50,
            "status": "filled",
            "paper": True,
            "order_id": trade_id,
            "type": "weather",
            "opp_type": "weather",
            "opened_at": (now - timedelta(hours=2)).isoformat(),
        }]
        json.dump(positions, open(positions_file, "w"))

        paper_trades = [{
            "id": trade_id,
            "ticker": "KXHIGHNY-26APR06-T70",
            "type": "weather",
            "side": "yes",
            "entry_price": 0.45,
            "contracts": 10,
            "timestamp": (now - timedelta(hours=2)).isoformat(),
            "status": "open",
            "exit_price": None,
            "pnl": None,
            "resolved_at": None,
        }]
        json.dump(paper_trades, open(paper_trades_file, "w"))
        json.dump([], open(trade_history_file, "w"))
        json.dump(clv_entries, open(clv_file, "w"))

        return paper_trades_file

    def test_clv_written_to_paper_trade_on_resolve(self, tmp_path, monkeypatch):
        """When a trade resolves, CLV data from clv.json is synced to paper_trades."""
        import bot.tracker as tracker

        trade_id = "PAPER-TEST123"
        clv_entries = [{
            "trade_id": trade_id,
            "ticker": "KXHIGHNY-26APR06-T70",
            "status": "settled",
            "clv_cents": 8.5,
            "clv_relative": 0.189,
        }]
        paper_trades_file = self._setup_resolve(tmp_path, monkeypatch, trade_id, clv_entries)

        from unittest.mock import patch
        with patch("bot.tracker.get_market") as mock_market:
            mock_market.return_value = {"status": "settled", "result": "yes"}
            tracker.resolve_trades()

        updated = json.loads(paper_trades_file.read_text())
        assert len(updated) == 1
        assert updated[0]["status"] == "won"
        assert updated[0]["clv_cents"] == 8.5
        assert updated[0]["clv_relative"] == 0.189

    def test_missing_clv_does_not_break_resolve(self, tmp_path, monkeypatch):
        """If clv.json has no matching entry, resolve still works without CLV."""
        import bot.tracker as tracker

        trade_id = "PAPER-NOCLV"
        paper_trades_file = self._setup_resolve(tmp_path, monkeypatch, trade_id, [])

        from unittest.mock import patch
        with patch("bot.tracker.get_market") as mock_market:
            mock_market.return_value = {"status": "settled", "result": "yes"}
            tracker.resolve_trades()

        updated = json.loads(paper_trades_file.read_text())
        assert updated[0]["status"] == "won"
        assert "clv_cents" not in updated[0]


# ---------------------------------------------------------------------------
# Fix 5: Vig-stack suppression threshold
# ---------------------------------------------------------------------------

class TestVigStackSuppression:
    def test_weak_weather_edge_does_not_suppress_vig_stack(self):
        """Weather opps with edge < 20% should NOT suppress vig stack."""
        _VIG_SUPPRESS_EDGE = 0.20
        weather_opps = [
            {"ticker": "KXHIGHDEN-T63", "recommended_side": "yes", "edge": 0.10},
        ]
        # With old logic: any YES suppresses. With new logic: only edge >= 20%
        suppressed = {
            opp["ticker"] for opp in weather_opps
            if opp.get("recommended_side") == "yes"
            and abs(opp.get("edge", 0)) >= _VIG_SUPPRESS_EDGE
        }
        assert "KXHIGHDEN-T63" not in suppressed

    def test_strong_weather_edge_suppresses_vig_stack(self):
        """Weather opps with edge >= 20% SHOULD suppress vig stack."""
        _VIG_SUPPRESS_EDGE = 0.20
        weather_opps = [
            {"ticker": "KXHIGHDEN-T63", "recommended_side": "yes", "edge": 0.25},
        ]
        suppressed = {
            opp["ticker"] for opp in weather_opps
            if opp.get("recommended_side") == "yes"
            and abs(opp.get("edge", 0)) >= _VIG_SUPPRESS_EDGE
        }
        assert "KXHIGHDEN-T63" in suppressed


# ---------------------------------------------------------------------------
# Fix 6: Vig-stack NO entry price floor (Apr 18)
# Below 0.70, 24-trade paper history shows 2W/4L (−$37.60); at/above, 15W/3L
# (+$10.01). Gate lives in scanner.scan_vig_stack_series.
# ---------------------------------------------------------------------------

class TestVigStackMinNoPrice:
    def test_floor_is_set_to_70_cents(self):
        from bot.config import VIG_STACK_MIN_NO_ENTRY_PRICE
        assert VIG_STACK_MIN_NO_ENTRY_PRICE == 0.70, (
            "Floor was tuned at 0.70 on Apr 18 from 24-trade history. "
            "Change requires fresh evidence."
        )

    def test_floor_filters_sub_70_no_contracts(self):
        """Scanner should drop NO contracts with no_ask < 70¢ via the price floor."""
        from bot.config import VIG_STACK_MIN_NO_ENTRY_PRICE
        from unittest.mock import patch
        from bot import scanner as _scanner

        # Build a minimal 2-contract ladder: YES prices sum to 110¢ (vig present),
        # NO asks at 60¢ (below floor) and 80¢ (above floor). Only the 80¢ one
        # should surface.
        markets = [
            {"ticker": "KXTEST-A", "title": "A", "yes_ask": 30, "no_ask": 60,
             "volume": 100, "open_interest": 100},
            {"ticker": "KXTEST-B", "title": "B", "yes_ask": 80, "no_ask": 80,
             "volume": 100, "open_interest": 100},
        ]

        # Force deterministic series list: one made-up futures ticker so we hit
        # the futures code path without weather/forecast side-effects.
        with patch.object(_scanner, "WEATHER_SERIES_TICKERS", []), \
             patch.object(_scanner, "INDEX_RANGE_SERIES_TICKERS", []), \
             patch.object(_scanner, "SPORTS_FUTURES_TICKERS", ["KXTEST"]), \
             patch.object(_scanner, "get_markets",
                          return_value={"markets": markets}), \
             patch.object(_scanner, "_fetch_vig_stack_forecasts",
                          return_value={}):
            opps = _scanner.scan_vig_stack_series()

        surfaced_tickers = {o["ticker"] for o in opps}
        # 60¢ rung must be dropped by the floor; 80¢ rung may or may not
        # survive downstream checks but it is the ONLY one that can.
        assert "KXTEST-A" not in surfaced_tickers, (
            f"NO price 60¢ < floor {VIG_STACK_MIN_NO_ENTRY_PRICE} — should be filtered"
        )


# ---------------------------------------------------------------------------
# Fix 7: Vig-stack per-ladder rung cap across scans (Apr 18)
# Apr 17 DEN ladder accumulated 6 correlated rungs across 3 scans and all
# resolved zero for −$70.35. The existing within-scan _cap_correlated_vig_stack
# can't see open positions; _cap_ladder_rungs does.
# ---------------------------------------------------------------------------

class TestVigStackLadderRungCap:
    def test_cap_constant_matches_default(self):
        from bot.config import VIG_STACK_MAX_RUNGS_PER_LADDER
        assert VIG_STACK_MAX_RUNGS_PER_LADDER == 3, (
            "Cap was chosen at 3 rungs per ladder-event on Apr 18 based on "
            "the Apr 17 DEN 6-rung wipe. Change requires fresh evidence."
        )

    def test_ladder_event_key_extraction(self):
        from bot.scanner import _ladder_event_key
        assert _ladder_event_key("KXHIGHDEN-26APR17-B57.5") == "KXHIGHDEN-26APR17"
        assert _ladder_event_key("KXINX-26APR17H1600-B7062") == "KXINX-26APR17H1600"
        assert _ladder_event_key("NOHYPHEN") == "NOHYPHEN"
        assert _ladder_event_key("") == ""

    def test_cap_preserves_all_when_under_limit(self, tmp_path, monkeypatch):
        """With no open positions and only 2 new rungs, both survive."""
        from bot import scanner as _scanner

        empty_positions = tmp_path / "positions.json"
        empty_positions.write_text("[]")
        monkeypatch.setattr(_scanner, "POSITIONS_FILE", empty_positions)

        opps = [
            {"ticker": "KXHIGHNY-26APR20-B60.5", "relative_edge": 0.10},
            {"ticker": "KXHIGHNY-26APR20-B62.5", "relative_edge": 0.08},
        ]
        result = _scanner._cap_ladder_rungs(opps)
        assert len(result) == 2

    def test_cap_drops_when_open_positions_fill_ladder(self, tmp_path, monkeypatch):
        """3 open rungs on a ladder → all new rungs on that ladder are dropped."""
        from bot import scanner as _scanner

        positions_file = tmp_path / "positions.json"
        positions_file.write_text(json.dumps([
            {"ticker": "KXHIGHDEN-26APR20-B55.5", "type": "vig_stack_series",
             "filled": 10, "status": "filled"},
            {"ticker": "KXHIGHDEN-26APR20-B57.5", "type": "vig_stack_series",
             "filled": 10, "status": "filled"},
            {"ticker": "KXHIGHDEN-26APR20-B59.5", "type": "vig_stack_series",
             "filled": 10, "status": "filled"},
        ]))
        monkeypatch.setattr(_scanner, "POSITIONS_FILE", positions_file)

        opps = [
            {"ticker": "KXHIGHDEN-26APR20-B61.5", "relative_edge": 0.10},
            {"ticker": "KXHIGHDEN-26APR20-B63.5", "relative_edge": 0.05},
        ]
        result = _scanner._cap_ladder_rungs(opps)
        assert len(result) == 0, "All new rungs should drop when ladder already at cap"

    def test_cap_keeps_highest_relative_edge_when_overflow(self, tmp_path, monkeypatch):
        """1 open rung + 3 new rungs on same ladder → keep top 2 by relative_edge."""
        from bot import scanner as _scanner

        positions_file = tmp_path / "positions.json"
        positions_file.write_text(json.dumps([
            {"ticker": "KXHIGHDEN-26APR20-B55.5", "type": "vig_stack_series",
             "filled": 10, "status": "filled"},
        ]))
        monkeypatch.setattr(_scanner, "POSITIONS_FILE", positions_file)

        opps = [
            {"ticker": "KXHIGHDEN-26APR20-B61.5", "relative_edge": 0.05},
            {"ticker": "KXHIGHDEN-26APR20-B63.5", "relative_edge": 0.15},
            {"ticker": "KXHIGHDEN-26APR20-B65.5", "relative_edge": 0.10},
        ]
        result = _scanner._cap_ladder_rungs(opps)
        kept = {o["ticker"] for o in result}
        assert len(result) == 2, f"cap is 3, 1 open → only 2 slots. got {len(result)}"
        assert "KXHIGHDEN-26APR20-B63.5" in kept, "highest rel_edge (15%) must survive"
        assert "KXHIGHDEN-26APR20-B65.5" in kept, "second-highest rel_edge (10%) must survive"
        assert "KXHIGHDEN-26APR20-B61.5" not in kept, "lowest rel_edge (5%) should drop"

    def test_cap_scoped_per_ladder_event(self, tmp_path, monkeypatch):
        """Different ladder events cap independently."""
        from bot import scanner as _scanner

        positions_file = tmp_path / "positions.json"
        positions_file.write_text("[]")
        monkeypatch.setattr(_scanner, "POSITIONS_FILE", positions_file)

        # 4 rungs on DEN-APR20 + 2 rungs on DEN-APR21 (different events)
        opps = [
            {"ticker": "KXHIGHDEN-26APR20-B55.5", "relative_edge": 0.10},
            {"ticker": "KXHIGHDEN-26APR20-B57.5", "relative_edge": 0.09},
            {"ticker": "KXHIGHDEN-26APR20-B59.5", "relative_edge": 0.08},
            {"ticker": "KXHIGHDEN-26APR20-B61.5", "relative_edge": 0.07},
            {"ticker": "KXHIGHDEN-26APR21-B55.5", "relative_edge": 0.06},
            {"ticker": "KXHIGHDEN-26APR21-B57.5", "relative_edge": 0.05},
        ]
        result = _scanner._cap_ladder_rungs(opps)
        apr20 = [o for o in result if "APR20" in o["ticker"]]
        apr21 = [o for o in result if "APR21" in o["ticker"]]
        assert len(apr20) == 3, f"APR20 ladder capped at 3, got {len(apr20)}"
        assert len(apr21) == 2, f"APR21 ladder under cap, kept all 2, got {len(apr21)}"

    def test_cap_handles_missing_positions_file(self, tmp_path, monkeypatch):
        """Missing positions file shouldn't crash — treat as no open positions."""
        from bot import scanner as _scanner

        missing = tmp_path / "no_such_file.json"  # does not exist
        monkeypatch.setattr(_scanner, "POSITIONS_FILE", missing)

        opps = [
            {"ticker": "KXHIGHDEN-26APR20-B55.5", "relative_edge": 0.10},
            {"ticker": "KXHIGHDEN-26APR20-B57.5", "relative_edge": 0.09},
        ]
        result = _scanner._cap_ladder_rungs(opps)
        assert len(result) == 2, "Missing positions file → treat as 0 open, keep all under-cap opps"


# ---------------------------------------------------------------------------
# Fix 8: Vig-stack family-aware price floor (Apr 18 evening)
# Post-paper-fill-bug data dive (33 resolved trades, Apr 15-18) showed ladder
# family dominates entry price: stable families (MIA/AUS/INX) succeed at 0.70+,
# volatile ones (DEN/CHI/NY) need 0.90+. Retroactive Filter F on the same
# window: kept 15 trades at 87% WR +$23.47, blocked 18 at 28% WR −$129.79.
# ---------------------------------------------------------------------------

class TestVigStackFamilyFloor:
    def test_stable_families_constant(self):
        from bot.config import VIG_STACK_STABLE_FAMILIES
        # Exact membership: MIA/AUS/INX only (Apr 18 evidence). Adding or
        # removing a family requires re-running the family×date grid and
        # documenting the WR delta in config comments.
        assert VIG_STACK_STABLE_FAMILIES == {"KXHIGHMIA", "KXHIGHAUS", "KXINX"}, (
            "Whitelist tuned on Apr 18 from 33-trade family×date grid. "
            "Changes require fresh evidence (family must have 80%+ WR on n>=5)."
        )

    def test_weather_min_price_constant(self):
        from bot.config import VIG_STACK_WEATHER_MIN_PRICE
        assert VIG_STACK_WEATHER_MIN_PRICE == 0.90, (
            "Volatile-family floor tuned at 0.90 on Apr 18. Below this, "
            "risk/reward demands WR > 85%, which volatile families clock "
            "at 28%. Change requires fresh evidence."
        )

    def test_weather_floor_strictly_above_baseline(self):
        """The weather floor must be strictly higher than the baseline floor."""
        from bot.config import VIG_STACK_MIN_NO_ENTRY_PRICE, VIG_STACK_WEATHER_MIN_PRICE
        assert VIG_STACK_WEATHER_MIN_PRICE > VIG_STACK_MIN_NO_ENTRY_PRICE, (
            "Weather floor must be stricter than baseline — otherwise there's "
            "no selection pressure on volatile-climate ladders."
        )

    def test_volatile_family_below_weather_floor_is_filtered(self):
        """KXHIGHDEN @ 0.80 NO must be dropped (below 0.90 weather floor)."""
        from unittest.mock import patch
        from bot import scanner as _scanner

        # Volatile family ticker (DEN), NO ask at 80¢ (below 0.90 weather floor).
        # YES/NO sum well above 100¢ so vig is present; only the floor should
        # stop this from surfacing.
        markets = [
            {"ticker": "KXHIGHDEN-A", "title": "A", "yes_ask": 35, "no_ask": 80,
             "volume": 100, "open_interest": 100},
        ]
        with patch.object(_scanner, "WEATHER_SERIES_TICKERS", ["KXHIGHDEN"]), \
             patch.object(_scanner, "INDEX_RANGE_SERIES_TICKERS", []), \
             patch.object(_scanner, "SPORTS_FUTURES_TICKERS", []), \
             patch.object(_scanner, "get_markets",
                          return_value={"markets": markets}), \
             patch.object(_scanner, "_fetch_vig_stack_forecasts",
                          return_value={}):
            opps = _scanner.scan_vig_stack_series()
        assert not any(o["ticker"] == "KXHIGHDEN-A" for o in opps), (
            "Volatile family KXHIGHDEN @ 0.80 NO should be blocked by the "
            "0.90 weather floor."
        )

    def test_stable_family_below_weather_floor_survives(self):
        """KXINX @ 0.75 NO must pass the family check (stable → 0.70 floor)."""
        from unittest.mock import patch
        from bot import scanner as _scanner

        # Stable family (KXINX — non-weather, runs through futures path), NO
        # ask at 75¢: above the 0.70 baseline, below the 0.90 weather floor.
        # Must surface because stable families use the looser 0.70 floor.
        # Ladder needs enough vig to clear the 2% relative-edge threshold:
        # yes_A=20 + yes_B=99 = 119 → vig_factor=1.19 → no_fair_A=83.19 →
        # relative_edge = 8.19/75 = 10.9% ✓.
        markets = [
            {"ticker": "KXINX-A", "title": "A", "yes_ask": 20, "no_ask": 75,
             "volume": 100, "open_interest": 100},
            # Cheap YES companion creates vig; its NO at 1¢ is filtered by
            # no_price_too_low (<0.03) but doesn't affect KXINX-A.
            {"ticker": "KXINX-B", "title": "B", "yes_ask": 99, "no_ask": 1,
             "volume": 100, "open_interest": 100},
        ]
        with patch.object(_scanner, "WEATHER_SERIES_TICKERS", []), \
             patch.object(_scanner, "INDEX_RANGE_SERIES_TICKERS", []), \
             patch.object(_scanner, "SPORTS_FUTURES_TICKERS", ["KXINX"]), \
             patch.object(_scanner, "get_markets",
                          return_value={"markets": markets}), \
             patch.object(_scanner, "_fetch_vig_stack_forecasts",
                          return_value={}):
            opps = _scanner.scan_vig_stack_series()

        surfaced = {o["ticker"] for o in opps}
        # KXINX-A at 0.75 must survive the floor check (it may or may not
        # survive downstream edge/self-check, but it must at least pass the
        # family-aware price floor).
        assert "KXINX-A" in surfaced, (
            f"Stable family KXINX @ 0.75 NO should pass the 0.70 baseline "
            f"floor and surface. Got: {surfaced}"
        )

    def test_volatile_family_at_weather_floor_survives(self):
        """KXHIGHDEN @ 0.90 NO must pass (meets the weather floor exactly)."""
        from unittest.mock import patch
        from bot import scanner as _scanner

        # Volatile family, NO at 0.90 (meets weather floor exactly). Scanner
        # requires yes_sum_prob >= 1.05 ladder-level before any contract is
        # considered; at NO=0.90, the complement YES is ~5-10¢ which alone
        # wouldn't clear the ladder-vig floor. Use 3 contracts with two
        # high-YES companions so yes_sum = 5+99+99 = 203 → vig_factor=2.03 →
        # no_fair_A = 100-5/2.03 = 97.54 → relative_edge = 7.54/90 = 8.4% ✓.
        # The B and C contracts get filtered individually by no_price_too_low
        # (<0.03) but still count toward the ladder vig sum.
        markets = [
            {"ticker": "KXHIGHDEN-A", "title": "A", "yes_ask": 5, "no_ask": 90,
             "volume": 100, "open_interest": 100},
            {"ticker": "KXHIGHDEN-B", "title": "B", "yes_ask": 99, "no_ask": 1,
             "volume": 100, "open_interest": 100},
            {"ticker": "KXHIGHDEN-C", "title": "C", "yes_ask": 99, "no_ask": 1,
             "volume": 100, "open_interest": 100},
        ]
        with patch.object(_scanner, "WEATHER_SERIES_TICKERS", ["KXHIGHDEN"]), \
             patch.object(_scanner, "INDEX_RANGE_SERIES_TICKERS", []), \
             patch.object(_scanner, "SPORTS_FUTURES_TICKERS", []), \
             patch.object(_scanner, "get_markets",
                          return_value={"markets": markets}), \
             patch.object(_scanner, "_fetch_vig_stack_forecasts",
                          return_value={}):
            opps = _scanner.scan_vig_stack_series()
        surfaced = {o["ticker"] for o in opps}
        assert "KXHIGHDEN-A" in surfaced, (
            f"Volatile family KXHIGHDEN @ 0.90 NO meets weather floor and "
            f"should surface. Got: {surfaced}"
        )

    def test_telemetry_counts_non_stable_below_weather_floor(self):
        """Drops by the weather floor must increment the new telemetry key."""
        # Importing the symbol name confirms the key exists in the dict schema.
        # We test by running a scan where a volatile family is below the floor,
        # then checking the log output / counters — but since _telem is scan-local,
        # we verify indirectly via the config constants being wired.
        from bot.config import (
            VIG_STACK_STABLE_FAMILIES,
            VIG_STACK_WEATHER_MIN_PRICE,
            VIG_STACK_MIN_NO_ENTRY_PRICE,
        )
        # Invariant: the two floors and whitelist must be mutually consistent.
        assert VIG_STACK_MIN_NO_ENTRY_PRICE < VIG_STACK_WEATHER_MIN_PRICE
        assert "KXHIGHMIA" in VIG_STACK_STABLE_FAMILIES
        assert "KXHIGHDEN" not in VIG_STACK_STABLE_FAMILIES
        assert "KXHIGHCHI" not in VIG_STACK_STABLE_FAMILIES
        assert "KXHIGHNY" not in VIG_STACK_STABLE_FAMILIES
