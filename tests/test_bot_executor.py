"""
Tests for bot/executor.py

Covers:
- execute_trade() — all 5 check failure paths + paper happy path
- execute_double() — series_game_edge uses correct edge calc (Fix 1 regression)
- _verify_edge_still_exists() — 3¢ kill switch and unchanged price paths
- check_fills() — paper resting order fills when ask crosses limit (Fix 5 regression)
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect bot state files to tmp_path so tests never touch real state."""
    positions_file = tmp_path / "positions.json"
    history_file = tmp_path / "trade_history.json"
    paper_trades_file = tmp_path / "paper_trades.json"
    positions_file.write_text("[]")
    history_file.write_text("[]")
    paper_trades_file.write_text("[]")

    # Import after setting up paths so module-level constants are patchable
    import bot.executor as exc

    # Patch the names as they exist in the executor module's namespace
    monkeypatch.setattr(exc, "POSITIONS_FILE", positions_file)
    monkeypatch.setattr(exc, "TRADE_HISTORY_FILE", history_file)
    monkeypatch.setattr(exc, "PAPER_TRADES_FILE", paper_trades_file)
    monkeypatch.setattr(exc, "PAPER_MODE", True)

    return {"positions": positions_file, "history": history_file, "paper_trades": paper_trades_file, "tmp": tmp_path}


def _make_opp(opp_type="weather", side="yes", price_cents=45, fair_value=0.55,
              edge=0.10, relative_edge=0.22, self_check=True):
    """Build a minimal opportunity dict for testing."""
    return {
        "ticker": "KXHIGHNY-26APR-70",
        "title": "NYC High Temp ≥70°F",
        "type": opp_type,
        "opp_type": opp_type,
        "recommended_side": side,
        "edge": edge,
        "relative_edge": relative_edge,
        "city": "NYC",
        "forecast_temp": 72,
        "threshold": 70,
        "direction": "above",
        "canonical_team": "",
        "opponent_team": "",
        "sport": "",
        "market": {},
        "edge_result": {
            "fair_value": fair_value,
            "kalshi_price": price_cents / 100.0,
            "edge": edge,
            "relative_edge": relative_edge,
            "self_check_passed": self_check,
            "warnings": [],
        },
    }


def _make_sizing(contracts=10, price_cents=45):
    total = contracts * price_cents / 100.0
    return {"contracts": contracts, "price_cents": price_cents, "total_cost": total}


# ---------------------------------------------------------------------------
# execute_trade — check failure paths
# ---------------------------------------------------------------------------

class TestExecuteTradeChecks:

    def test_direction_check_fail_aborts(self, tmp_state):
        """If verify_contract_direction returns direction_correct=False, trade is rejected."""
        opp = _make_opp()
        sizing = _make_sizing()

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal:
            mock_dir.return_value = {
                "direction_correct": False,
                "confidence": "low",
                "explanation": "Ambiguous contract",
                "warnings": ["title unclear"],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert result["success"] is False
        assert "Direction check failed" in result["reason"]
        assert result["checks"][0]["name"] == "contract_direction"
        assert result["checks"][0]["passed"] is False

    def test_balance_check_fail_aborts(self, tmp_state, monkeypatch):
        """If balance is insufficient, trade is rejected after direction check."""
        import bot.executor as exc
        # Cost = 10 contracts × $0.45 = $4.50; set paper balance below that
        monkeypatch.setattr(exc, "PAPER_STARTING_BALANCE", 0.10)

        opp = _make_opp()
        sizing = _make_sizing(contracts=10, price_cents=45)

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 0.10}

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert result["success"] is False
        assert "balance" in result["reason"].lower() or "Insufficient" in result["reason"]

    def test_position_limit_fail_aborts(self, tmp_state, monkeypatch):
        """If proposed trade exceeds 20% position limit, trade is rejected."""
        import bot.executor as exc
        # $10 cost on $10 paper balance = 100% of balance — exceeds 20% limit
        monkeypatch.setattr(exc, "PAPER_STARTING_BALANCE", 10.0)

        opp = _make_opp()
        sizing = _make_sizing(contracts=100, price_cents=10)

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 10.0}

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert result["success"] is False
        assert "position" in result["reason"].lower() or "exposure" in result["reason"].lower()

    def test_edge_evaporated_aborts(self, tmp_state):
        """If re-fetched price moved >3¢ from scan price, trade is rejected."""
        opp = _make_opp(price_cents=45)
        sizing = _make_sizing(price_cents=45)

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor.get_market") as mock_market:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            # Price moved 5¢ — exceeds 3¢ kill switch
            mock_market.return_value = {"yes_ask": 50, "yes_bid": 48}

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert result["success"] is False
        assert "moved" in result["reason"] or "evaporate" in result["reason"].lower() or "motion" in result["reason"].lower()

    def test_math_self_check_fail_aborts(self, tmp_state):
        """If edge_result.self_check_passed is False, trade is rejected."""
        opp = _make_opp(self_check=False)
        sizing = _make_sizing()

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert result["success"] is False
        assert "self-check" in result["reason"].lower() or "math" in result["reason"].lower()


# ---------------------------------------------------------------------------
# execute_trade — paper happy path
# ---------------------------------------------------------------------------

class TestExecuteTradePaperHappyPath:

    def test_paper_trade_creates_resting_position(self, tmp_state):
        """Paper trade writes resting position to positions.json with filled=0."""
        opp = _make_opp()
        sizing = _make_sizing(contracts=5, price_cents=45)

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert result["success"] is True
        order_id = result["order_result"]["order_id"]
        assert order_id.startswith("PAPER-")
        assert result["order_result"]["status"] == "paper_resting"
        assert result["order_result"]["filled_count"] == 0

        # Position written as resting with filled=0
        positions = json.loads(tmp_state["positions"].read_text())
        assert len(positions) == 1
        pos = positions[0]
        assert pos["status"] == "resting"
        assert pos["filled"] == 0
        assert pos["paper"] is True

    def test_paper_order_id_format(self, tmp_state):
        """Paper order ID is PAPER- followed by 8 uppercase hex chars."""
        import re
        opp = _make_opp()
        sizing = _make_sizing()

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert re.match(r"^PAPER-[0-9A-F]{8}$", result["order_result"]["order_id"])


# ---------------------------------------------------------------------------
# execute_double — Fix 1 regression
# ---------------------------------------------------------------------------

class TestExecuteDoubleSeriesGameEdge:

    def test_series_game_edge_uses_stored_fair_value(self, tmp_state):
        """
        execute_double() for series_game_edge must derive fair value from
        stored edge+price, NOT call calculate_parlay_edge (Fix 1 regression).
        """
        # Seed a filled series_game_edge position
        position = {
            "ticker": "KXNBAGAME-26APR05BOSLAC-BOS",
            "title": "Boston Celtics at LA Clippers Winner?",
            "side": "yes",
            "contracts": 5,
            "filled": 5,
            "price_cents": 40,
            "cost": 2.0,
            "order_id": "LIVE-12345",
            "type": "series_game_edge",
            "opp_type": "series_game_edge",
            "edge": 0.10,
            "relative_edge": 0.25,
            "canonical_team": "Boston Celtics",
            "opponent_team": "LA Clippers",
            "sport": "nba",
            "status": "filled",
            "paper": True,
        }
        tmp_state["positions"].write_text(json.dumps([position]))

        with patch("bot.executor.get_market") as mock_market, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge, \
             patch("bot.executor.calculate_parlay_edge") as mock_parlay:

            # Market price hasn't moved — still 40¢
            mock_market.return_value = {"yes_ask": 40, "yes_bid": 38, "no_ask": 60}
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_edge.return_value = (True, "ok")

            from bot.executor import execute_double
            execute_double("KXNBAGAME-26APR05BOSLAC-BOS")

        # calculate_parlay_edge must NOT have been called for series_game_edge
        mock_parlay.assert_not_called()

    def test_series_game_edge_fair_value_math(self, tmp_state):
        """
        Fair value for double-down = stored edge + original price.
        Stored edge=0.10, original price=0.40 → fair_value=0.50.
        """
        position = {
            "ticker": "KXNBAGAME-26APR05BOSLAC-BOS",
            "title": "Boston at LA Winner?",
            "side": "yes",
            "contracts": 5,
            "filled": 5,
            "price_cents": 40,
            "cost": 2.0,
            "order_id": "LIVE-99",
            "type": "series_game_edge",
            "opp_type": "series_game_edge",
            "edge": 0.10,       # stored edge at time of original trade
            "relative_edge": 0.25,
            "canonical_team": "Boston Celtics",
            "sport": "nba",
            "status": "filled",
            "paper": True,
        }
        tmp_state["positions"].write_text(json.dumps([position]))

        captured_opp = {}

        def capture_execute(opp, sizing):
            captured_opp.update(opp)
            return {"success": False, "checks": [], "reason": "captured"}

        with patch("bot.executor.get_market") as mock_market, \
             patch("bot.executor.execute_trade", side_effect=capture_execute), \
             patch("bot.executor.get_balance"):
            mock_market.return_value = {"yes_ask": 38, "yes_bid": 36}  # slight move

            from bot.executor import execute_double
            execute_double("KXNBAGAME-26APR05BOSLAC-BOS")

        er = captured_opp.get("edge_result", {})
        assert abs(er.get("fair_value", 0) - 0.50) < 0.001   # 0.10 + 0.40
        assert er.get("self_check_passed") is True


# ---------------------------------------------------------------------------
# _verify_edge_still_exists
# ---------------------------------------------------------------------------

class TestVerifyEdgeStillExists:

    def test_price_moved_more_than_3_cents_kills_trade(self):
        opp = _make_opp(price_cents=45)
        opp["edge_result"]["kalshi_price"] = 0.45

        with patch("bot.executor.get_market") as mock_market:
            mock_market.return_value = {"yes_ask": 50, "yes_bid": 48}  # moved 5¢

            from bot.executor import _verify_edge_still_exists
            ok, msg = _verify_edge_still_exists(opp)

        assert ok is False
        assert "5.0" in msg or "moved" in msg.lower()

    def test_price_unchanged_passes(self):
        opp = _make_opp(price_cents=45)
        opp["edge_result"]["kalshi_price"] = 0.45

        with patch("bot.executor.get_market") as mock_market:
            mock_market.return_value = {"yes_ask": 45, "yes_bid": 43}  # unchanged

            from bot.executor import _verify_edge_still_exists
            ok, msg = _verify_edge_still_exists(opp)

        assert ok is True

    def test_edge_evaporated_below_threshold_fails(self):
        """Price moved 2¢ (within kill switch) but edge drops below 15% threshold."""
        opp = _make_opp(price_cents=45, fair_value=0.50, relative_edge=0.11)
        opp["edge_result"]["kalshi_price"] = 0.45
        opp["edge_result"]["fair_value"] = 0.50

        with patch("bot.executor.get_market") as mock_market:
            # Moved 2¢ within kill switch, but new relative edge = (0.50 - 0.47) / 0.47 ≈ 6.4% < 15%
            mock_market.return_value = {"yes_ask": 47, "yes_bid": 45}

            from bot.executor import _verify_edge_still_exists
            ok, msg = _verify_edge_still_exists(opp)

        assert ok is False
        assert "evaporate" in msg.lower() or "threshold" in msg.lower()


# ---------------------------------------------------------------------------
# check_fills — Fix 5 regression
# ---------------------------------------------------------------------------

class TestCheckFillsPaperSim:

    def test_paper_resting_fills_when_ask_at_limit(self, tmp_state):
        """Paper resting order fills when current ask ≤ limit price."""
        position = {
            "ticker": "KXHIGHNY-26APR-70",
            "side": "yes",
            "contracts": 8,
            "filled": 0,
            "price_cents": 45,
            "cost": 3.60,
            "order_id": "PAPER-AABBCCDD",
            "type": "weather",
            "status": "resting",
            "paper": True,
        }
        tmp_state["positions"].write_text(json.dumps([position]))

        with patch("bot.executor.get_market") as mock_market:
            # Market ask dropped to our limit price — should fill
            mock_market.return_value = {"yes_ask": 45, "yes_bid": 43}

            from bot.executor import check_fills
            updates = check_fills()

        assert len(updates) == 1
        assert updates[0]["filled"] == 8
        assert updates[0]["status"] == "filled"

        saved = json.loads(tmp_state["positions"].read_text())
        assert saved[0]["filled"] == 8
        assert saved[0]["status"] == "filled"

    def test_paper_resting_does_not_fill_when_ask_above_limit(self, tmp_state):
        """Paper resting order stays resting when ask > limit price."""
        position = {
            "ticker": "KXHIGHNY-26APR-70",
            "side": "yes",
            "contracts": 8,
            "filled": 0,
            "price_cents": 45,
            "cost": 3.60,
            "order_id": "PAPER-AABBCCDD",
            "type": "weather",
            "status": "resting",
            "paper": True,
        }
        tmp_state["positions"].write_text(json.dumps([position]))

        with patch("bot.executor.get_market") as mock_market:
            # Market ask is above our limit — should NOT fill
            mock_market.return_value = {"yes_ask": 50, "yes_bid": 48}

            from bot.executor import check_fills
            updates = check_fills()

        assert len(updates) == 0
        saved = json.loads(tmp_state["positions"].read_text())
        assert saved[0]["status"] == "resting"

    def test_live_resting_order_uses_api_not_paper_sim(self, tmp_state):
        """Live orders (no paper flag) still go through get_order() API call."""
        position = {
            "ticker": "KXHIGHNY-26APR-70",
            "side": "yes",
            "contracts": 5,
            "filled": 0,
            "price_cents": 45,
            "cost": 2.25,
            "order_id": "live-order-id-123",   # no PAPER- prefix
            "type": "weather",
            "status": "resting",
            "paper": False,
        }
        tmp_state["positions"].write_text(json.dumps([position]))

        with patch("bot.executor.get_order") as mock_order, \
             patch("bot.executor.get_market") as mock_market:
            mock_order.return_value = {"filled_count": 5, "status": "filled"}
            mock_market.return_value = {}  # should not be called for live order

            from bot.executor import check_fills
            updates = check_fills()

        mock_order.assert_called_once_with("live-order-id-123")
        mock_market.assert_not_called()
        assert len(updates) == 1
        assert updates[0]["filled"] == 5


class TestLogRejectExtra:
    """Session 10 — _log_position_reject and _log_edge_reject must propagate
    extra dict so cohort_report can compute distance-from-threshold."""

    def test_log_position_reject_propagates_extra(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "decisions_extra_test.jsonl"
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._log_position_reject(
                "KX-X", "vig_stack_series", "cooldown",
                extra={"last_trade_age_min": 145, "cooldown_min": 240},
            )
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        assert len(recs) == 1
        assert recs[0]["reason"] == "cooldown"
        assert recs[0]["extra"] == {"last_trade_age_min": 145, "cooldown_min": 240}

    def test_log_position_reject_extra_none_omits_key(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "decisions_no_extra.jsonl"
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._log_position_reject("KX-X", "vig_stack_series", "duplicate")
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        assert "extra" not in recs[0]

    def test_log_edge_reject_propagates_extra(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "decisions_edge_extra.jsonl"
        opp = {"ticker": "KX-Y", "type": "vig_stack_series", "edge": 0.15}
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._log_edge_reject(
                opp, "price_moved",
                extra={"move_cents": 7.5, "kill_cents": 5},
            )
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        assert recs[0]["extra"] == {"move_cents": 7.5, "kill_cents": 5}


class TestCloseTsThreading:
    """Session 15.5 — every reject extras dict must carry close_ts so
    bot.regime.tag can populate event_horizon_hr. Closes the 0%-coverage gap
    on the event_horizon_hr regime axis."""

    def test_log_edge_reject_threads_close_ts_from_opportunity(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "ct_edge.jsonl"
        opp = {
            "ticker": "KX-Y", "type": "vig_stack_series", "edge": 0.15,
            "close_ts": "2026-04-26T00:00:00Z",
        }
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._log_edge_reject(opp, "edge_evaporated",
                                 extra={"new_relative": 0.01})
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        assert recs[0]["extra"]["close_ts"] == "2026-04-26T00:00:00Z"

    def test_log_edge_reject_falls_back_to_market_close_ts(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "ct_edge_fallback.jsonl"
        # No top-level close_ts; only nested under .market.
        opp = {
            "ticker": "KX-Y", "type": "vig_stack_series", "edge": 0.15,
            "market": {"close_ts": "2026-04-26T12:00:00Z"},
        }
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._log_edge_reject(opp, "price_moved", extra={"move_cents": 7})
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        assert recs[0]["extra"]["close_ts"] == "2026-04-26T12:00:00Z"

    def test_log_position_reject_accepts_close_ts_param(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "ct_pos.jsonl"
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._log_position_reject(
                "KX-Y", "vig_stack_series", "position_cap",
                extra={"cost_dollars": 100},
                close_ts="2026-04-26T00:00:00Z",
            )
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        assert recs[0]["extra"]["close_ts"] == "2026-04-26T00:00:00Z"

    def test_log_position_reject_explicit_extra_close_ts_wins(self, tmp_state):
        """If the caller already put close_ts in extra, helper does not overwrite."""
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "ct_pos_explicit.jsonl"
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._log_position_reject(
                "KX-Y", "vig_stack_series", "duplicate",
                extra={"close_ts": "EXPLICIT", "existing_count": 1},
                close_ts="FALLBACK",
            )
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        assert recs[0]["extra"]["close_ts"] == "EXPLICIT"

    def test_check_position_limits_threads_close_ts_to_reject(self, tmp_state):
        """A real _check_position_limits failure path includes close_ts in extra."""
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "ct_pos_limit.jsonl"
        # Force the position_cap path: cost_dollars > balance * MAX_POSITION_PERCENT.
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._check_position_limits(
                balance=100.0,
                cost_dollars=50.0,  # 50% of balance, certainly over 20%.
                ticker="KX-Y",
                opp_type="vig_stack_series",
                close_ts="2026-04-26T00:00:00Z",
            )
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        assert recs, "expected a position_cap reject log"
        assert recs[0]["reason"] == "position_cap"
        assert recs[0]["extra"]["close_ts"] == "2026-04-26T00:00:00Z"

    def test_self_check_failed_extra_includes_close_ts(self, tmp_state):
        """The self_check_failed direct log_decision call (executor.py:844)
        must include opportunity.close_ts in extra."""
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "ct_self_check.jsonl"
        opp = _make_opp(opp_type="weather", self_check=False)
        opp["close_ts"] = "2026-04-26T00:00:00Z"
        sizing = _make_sizing(contracts=2, price_cents=45)
        with patch.object(decisions, "DECISIONS_FILE", sandbox), \
             patch("bot.executor.verify_contract_direction",
                   return_value={
                       "direction_correct": True, "confidence": "HIGH",
                       "explanation": "test", "warnings": [],
                       "yes_means": "yes", "no_means": "no",
                       "thesis_supports": "yes", "intended_side": "yes",
                   }), \
             patch("bot.executor._verify_edge_still_exists",
                   return_value=(True, "ok")):
            exc.execute_trade(opp, sizing)
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        # Find the self_check_failed reject in the log stream.
        reject = next((r for r in recs if r.get("reason") == "self_check_failed"), None)
        assert reject is not None, f"expected self_check_failed log, got: {[r['reason'] for r in recs]}"
        assert reject["extra"]["close_ts"] == "2026-04-26T00:00:00Z"

    def test_all_gates_passed_extra_includes_close_ts(self, tmp_state):
        """The accept-path direct log_decision call (executor.py:865) must
        include opportunity.close_ts in extra."""
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "ct_accept.jsonl"
        opp = _make_opp(opp_type="weather", self_check=True)
        opp["close_ts"] = "2026-04-26T00:00:00Z"
        sizing = _make_sizing(contracts=2, price_cents=45)
        with patch.object(decisions, "DECISIONS_FILE", sandbox), \
             patch("bot.executor.verify_contract_direction",
                   return_value={
                       "direction_correct": True, "confidence": "HIGH",
                       "explanation": "test", "warnings": [],
                       "yes_means": "yes", "no_means": "no",
                       "thesis_supports": "yes", "intended_side": "yes",
                   }), \
             patch("bot.executor._verify_edge_still_exists",
                   return_value=(True, "ok")):
            exc.execute_trade(opp, sizing)
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        accept = next((r for r in recs if r.get("decision") == "accept"), None)
        assert accept is not None, f"expected an accept log, got decisions: {[r['decision'] for r in recs]}"
        assert accept["extra"]["close_ts"] == "2026-04-26T00:00:00Z"
