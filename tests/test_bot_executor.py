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
    shadow_trades_file = tmp_path / "shadow_trades.jsonl"
    positions_file.write_text("[]")
    history_file.write_text("[]")
    paper_trades_file.write_text("[]")

    # Import after setting up paths so module-level constants are patchable
    import bot.executor as exc
    import bot.shadow_trades as shadow

    # Patch the names as they exist in the executor module's namespace
    monkeypatch.setattr(exc, "POSITIONS_FILE", positions_file)
    monkeypatch.setattr(exc, "TRADE_HISTORY_FILE", history_file)
    monkeypatch.setattr(exc, "PAPER_TRADES_FILE", paper_trades_file)
    monkeypatch.setattr(exc, "PAPER_MODE", True)
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", shadow_trades_file)

    return {
        "positions": positions_file,
        "history": history_file,
        "paper_trades": paper_trades_file,
        "shadow_trades": shadow_trades_file,
        "tmp": tmp_path,
    }


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
        # Apr-16 reserve guard fires earlier in the chain when cost == balance
        # (test fixture is balance=$10, cost=$10 → trips reserve before position cap).
        # Either rejection satisfies the test's intent: oversized trades get blocked.
        reason = result["reason"].lower()
        assert any(k in reason for k in ("position", "exposure", "reserve", "balance")), \
            f"expected money-shape rejection, got: {result['reason']}"

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
             patch("bot.executor._verify_edge_still_exists") as mock_edge, \
             patch("bot.executor.get_market") as mock_market:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")
            mock_market.return_value = {"yes_ask": 99, "yes_bid": 43}

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
             patch("bot.executor._verify_edge_still_exists") as mock_edge, \
             patch("bot.executor.get_market") as mock_market:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")
            mock_market.return_value = {"yes_ask": 99, "yes_bid": 43}

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

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        assert trades[0]["id"] == order_id
        assert trades[0]["status"] == "resting"

    def test_paper_trade_immediate_fill_creates_open_paper_trade(self, tmp_state):
        """Marketable paper order writes an active position and open paper trade."""
        opp = _make_opp(price_cents=45)
        sizing = _make_sizing(contracts=5, price_cents=45)

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge, \
             patch("bot.executor.get_market") as mock_market:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")
            mock_market.return_value = {"yes_ask": 45, "yes_bid": 43}

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert result["success"] is True
        assert result["order_result"]["status"] == "paper_filled"
        positions = json.loads(tmp_state["positions"].read_text())
        assert positions[0]["status"] == "filled"
        assert positions[0]["filled"] == 5
        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert trades[0]["status"] == "open"

    def test_paper_order_id_format(self, tmp_state):
        """Paper order ID is PAPER- followed by 8 uppercase hex chars."""
        import re
        opp = _make_opp()
        sizing = _make_sizing()

        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge, \
             patch("bot.executor.get_market") as mock_market:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")
            mock_market.return_value = {"yes_ask": 99, "yes_bid": 43}

            from bot.executor import execute_trade
            result = execute_trade(opp, sizing)

        assert re.match(r"^PAPER-[0-9A-F]{8}$", result["order_result"]["order_id"])


class TestSession62FamilyCapExecutor:
    # Session 93 (2026-05-10): KXINX and KXHIGHCHI removed from this parametrize.
    # Both are now in VIG_STACK_DISABLED_FAMILIES and reject at the executor's
    # family_disabled gate BEFORE the family_cap gate is reached. The cap-
    # enforcement contract no longer applies to disabled families. Disabled-
    # family rejection is exercised in TestSession93FamilyDisableExecutor below.
    @pytest.mark.parametrize(
        "family,expected_cap",
        [
            ("KXMLBGAME", 50),
            ("KXHIGHDEN", 150),
            ("KXHIGHNY",  150),
            ("KXHIGHAUS", 200),
            ("KXHIGHMIA", 200),
        ],
    )
    def test_per_family_cap_enforced_at_executor(self, tmp_state, family, expected_cap):
        """Session 62: cap-clipped vig_stack sizing persists <= family cap."""
        from bot.executor import execute_trade
        from bot.sizing import kelly_size

        ticker = f"{family}-26MAY082210TEST-YES"
        opp = _make_opp(
            opp_type="vig_stack_futures",
            side="no",
            price_cents=50,
            fair_value=0.78,
            edge=0.10,
            relative_edge=0.20,
        )
        opp["ticker"] = ticker
        opp["title"] = "Session 62 family cap regression"
        opp["market"] = {"yes_ask": 50, "no_ask": 50}
        opp["edge_result"]["self_check_passed"] = True

        sizing = kelly_size(
            edge=0.10,
            probability=0.78,
            balance=10_000.0,
            price_cents=50,
            confidence=0.80,
            family=family,
        )
        assert sizing["total_cost"] <= float(expected_cap)

        with patch("bot.executor.get_market") as mock_market, \
             patch("bot.decisions.log_decision"):
            mock_market.return_value = {"yes_ask": 50, "no_ask": 50}
            result = execute_trade(opp, sizing)

        assert result["success"] is True
        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        written_cost = trades[0]["contracts"] * trades[0]["entry_price"]
        assert written_cost <= float(expected_cap)

    def test_oversized_vig_stack_futures_rejected_at_entry(self, tmp_state):
        """Session 92: stale/manual oversized vig_stack sizing cannot place."""
        from bot.executor import execute_trade

        opp = _make_opp(
            opp_type="vig_stack_futures",
            side="no",
            price_cents=44,
            fair_value=0.78,
            edge=0.10,
            relative_edge=0.20,
        )
        opp["ticker"] = "KXMLBGAME-26MAY092110ATLLAD-LAD"
        opp["title"] = "Atlanta vs Los Angeles D Winner?"
        opp["market"] = {"yes_ask": 56, "no_ask": 44, "close_ts": "2026-05-13T01:10:00Z"}
        opp["edge_result"]["self_check_passed"] = True
        sizing = _make_sizing(contracts=454, price_cents=44)

        with patch("bot.decisions.log_decision") as mock_log:
            result = execute_trade(opp, sizing)

        assert result["success"] is False
        assert "cap_exceeded_reject" in result["reason"]
        assert json.loads(tmp_state["paper_trades"].read_text()) == []

        mock_log.assert_called_once()
        _, kwargs = mock_log.call_args
        assert kwargs["decision"] == "reject"
        assert kwargs["reason"] == "cap_exceeded_reject"
        assert kwargs["gates"]["position_cap"] is True
        assert kwargs["gates"]["family_cap"] is False
        assert kwargs["extra"]["family"] == "KXMLBGAME"
        assert kwargs["extra"]["family_cap"] == 50.0
        assert kwargs["extra"]["incoming_cost"] == 199.76
        assert kwargs["extra"]["contracts"] == 454
        assert kwargs["extra"]["price_cents"] == 44
        assert kwargs["extra"]["cap_action"] == "cap_exceeded_reject"
        assert kwargs["extra"]["close_ts"] == "2026-05-13T01:10:00Z"

    def test_under_cap_vig_stack_futures_still_executes(self, tmp_state):
        """Session 92: KXMLBGAME vig_stack sizing at <= $50 still works."""
        from bot.executor import execute_trade

        opp = _make_opp(
            opp_type="vig_stack_futures",
            side="no",
            price_cents=44,
            fair_value=0.78,
            edge=0.10,
            relative_edge=0.20,
        )
        opp["ticker"] = "KXMLBGAME-26MAY092110ATLLAD-LAD"
        opp["title"] = "Atlanta vs Los Angeles D Winner?"
        opp["market"] = {"yes_ask": 56, "no_ask": 44, "close_ts": "2026-05-13T01:10:00Z"}
        opp["edge_result"]["self_check_passed"] = True
        sizing = _make_sizing(contracts=113, price_cents=44)

        with patch("bot.executor.get_market") as mock_market, \
             patch("bot.decisions.log_decision") as mock_log:
            mock_market.return_value = {"yes_ask": 56, "no_ask": 44}
            result = execute_trade(opp, sizing)

        assert result["success"] is True
        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        assert trades[0]["contracts"] * trades[0]["entry_price"] <= 50.0
        reject_reasons = [
            call.kwargs["reason"]
            for call in mock_log.call_args_list
            if call.kwargs.get("decision") == "reject"
        ]
        assert "cap_exceeded_reject" not in reject_reasons


# ---------------------------------------------------------------------------
# Session 93 (2026-05-10) — per-family vig_stack disable at executor entry.
# KXHIGHCHI + KXINX added to VIG_STACK_DISABLED_FAMILIES based on lifetime
# breakeven-WR analysis (loss/win ratio 5x+ requires WR ≥ 84%; actual 77-79%).
# The family_disabled gate fires before family_cap; reject reason
# "family_disabled_reject" emits a clean signal in decisions.jsonl rather than
# the misleading "cap_exceeded_reject" that a cap=$0 proxy would produce.
# ---------------------------------------------------------------------------
class TestSession93FamilyDisableExecutor:
    @pytest.mark.parametrize(
        "family,ticker",
        [
            ("KXHIGHCHI", "KXHIGHCHI-26MAY101200-T70"),
            ("KXINX",     "KXINX-26MAY10H1600-B5400.5"),
        ],
    )
    def test_disabled_family_vig_stack_rejected_with_family_disabled_reject(
        self, tmp_state, family, ticker,
    ):
        """Session 93: vig_stack on disabled family rejects before cap math."""
        from bot.executor import execute_trade

        opp = _make_opp(
            opp_type="vig_stack_series",
            side="no",
            price_cents=44,
            fair_value=0.78,
            edge=0.10,
            relative_edge=0.20,
        )
        opp["ticker"] = ticker
        opp["title"] = f"{family} disabled-family regression"
        opp["market"] = {"yes_ask": 56, "no_ask": 44, "close_ts": "2026-05-13T01:10:00Z"}
        opp["edge_result"]["self_check_passed"] = True
        # Sized far below any cap — proves the disable gate fires regardless of size.
        sizing = _make_sizing(contracts=10, price_cents=44)

        with patch("bot.decisions.log_decision") as mock_log:
            result = execute_trade(opp, sizing)

        assert result["success"] is False
        assert "family_disabled_reject" in result["reason"]
        assert family in result["reason"]
        assert json.loads(tmp_state["paper_trades"].read_text()) == []

        mock_log.assert_called_once()
        _, kwargs = mock_log.call_args
        assert kwargs["decision"] == "reject"
        assert kwargs["reason"] == "family_disabled_reject"
        # family_disabled gate fires AFTER position_cap and BEFORE family_cap.
        assert kwargs["gates"]["position_cap"] is True
        assert kwargs["gates"]["family_disabled"] is False
        # Fingerprint stops at the rejecting gate; family_cap not reached.
        assert "family_cap" not in kwargs["gates"]
        assert kwargs["extra"]["family"] == family
        # disabled_set persisted in extra for forensic context — must include both.
        assert "KXHIGHCHI" in kwargs["extra"]["disabled_set"]
        assert "KXINX" in kwargs["extra"]["disabled_set"]
        assert kwargs["extra"]["close_ts"] == "2026-05-13T01:10:00Z"

        shadow_rows = tmp_state["shadow_trades"].read_text().splitlines()
        assert len(shadow_rows) == 1
        shadow = json.loads(shadow_rows[0])
        assert shadow["blocked_reason"] == "family_disabled_reject"
        assert shadow["source"] == "executor"
        assert shadow["would_side"] == "no"
        assert shadow["would_entry_price"] == 0.44
        assert shadow["would_contracts"] == 10
        assert shadow["would_notional"] == 4.4
        assert shadow["sizing_status"] == "available"
        assert shadow["family"] == family

    def test_kxhighaus_vig_stack_passes_family_disabled_gate(self, tmp_state):
        """Session 93 control: KXHIGHAUS NOT disabled — disable gate must pass."""
        from bot.executor import execute_trade

        opp = _make_opp(
            opp_type="vig_stack_series",
            side="no",
            price_cents=44,
            fair_value=0.78,
            edge=0.10,
            relative_edge=0.20,
        )
        opp["ticker"] = "KXHIGHAUS-26MAY101200-T80"
        opp["title"] = "KXHIGHAUS allow-through control"
        opp["market"] = {"yes_ask": 56, "no_ask": 44, "close_ts": "2026-05-13T01:10:00Z"}
        opp["edge_result"]["self_check_passed"] = True
        sizing = _make_sizing(contracts=10, price_cents=44)  # $4.40, well under $200 cap

        with patch("bot.executor.get_market") as mock_market, \
             patch("bot.decisions.log_decision") as mock_log:
            mock_market.return_value = {"yes_ask": 56, "no_ask": 44}
            result = execute_trade(opp, sizing)

        # Trade succeeds — disable gate passed, cap gate passed, all downstream ok.
        assert result["success"] is True
        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        # Defense in depth: even if other gates rejected for some reason,
        # family_disabled_reject MUST NOT appear among any reject reasons.
        reject_reasons = [
            call.kwargs["reason"]
            for call in mock_log.call_args_list
            if call.kwargs.get("decision") == "reject"
        ]
        assert "family_disabled_reject" not in reject_reasons


# ---------------------------------------------------------------------------
# Session 50 — paper_trades.json observability fields (confidence/dqs/sport)
# ---------------------------------------------------------------------------

class TestSession50PaperTradeFields:
    """Forward-only persistence of confidence/dqs/sport on live_momentum
    paper_trades records. When the opp dict carries paper_* keys, the inline
    write site at bot/executor.py:1050 emits them on the record. When absent
    (mimicking vig_stack), the record stays byte-identical to pre-Session-50.
    """

    def _run_paper_trade(self, opp, sizing):
        """Helper: mock the 4 internal checks and call execute_trade."""
        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge, \
             patch("bot.executor.get_market") as mock_market:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")
            mock_market.return_value = {"yes_ask": 99, "yes_bid": 43}

            from bot.executor import execute_trade
            return execute_trade(opp, sizing)

    def test_paper_trade_persists_session_50_fields_when_set(self, tmp_state):
        """When opp carries paper_confidence/paper_dqs/paper_sport, all 3 land
        on the paper_trades record. Sport is lowercased."""
        opp = _make_opp(opp_type="live_momentum")
        opp["paper_confidence"] = 0.85
        opp["paper_dqs"] = 0.42
        opp["paper_sport"] = "NBA"
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert rec["confidence"] == 0.85
        assert rec["dqs"] == 0.42
        assert rec["sport"] == "nba"

    def test_paper_trade_omits_session_50_fields_when_unset(self, tmp_state):
        """When opp has no paper_* keys (mimics vig_stack), record has NO dqs
        and NO sport keys, and confidence falls back to the line-1064 default
        (relative_edge fallback). This is the byte-equality regression-lock."""
        opp = _make_opp(opp_type="weather", relative_edge=0.22)
        # explicitly NO paper_* keys
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert "dqs" not in rec
        assert "sport" not in rec
        # Existing default: confidence comes from opp["confidence"] OR opp["relative_edge"]
        assert rec["confidence"] == 0.22

    def test_paper_trade_partial_session_50_fields(self, tmp_state):
        """When only paper_confidence is set, only confidence overrides; dqs
        and sport remain absent from the record."""
        opp = _make_opp(opp_type="live_momentum", relative_edge=0.0)
        opp["paper_confidence"] = 0.7
        # explicitly NOT setting paper_dqs or paper_sport
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert rec["confidence"] == 0.7
        assert "dqs" not in rec
        assert "sport" not in rec

    def test_paper_trade_sport_is_lowercased(self, tmp_state):
        """Mixed-case sport string is lowercased before persistence."""
        opp = _make_opp(opp_type="live_momentum")
        opp["paper_sport"] = "UFC"
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert trades[0]["sport"] == "ufc"


# ---------------------------------------------------------------------------
# Session 99 — paper_trades.json fair-value proxy fields
# ---------------------------------------------------------------------------

class TestSession99PaperTradeProxyFields:
    """Forward-only persistence of estimated_win_prob / model_source /
    confidence_components on live_momentum paper_trades records. Mirrors the
    Session 50 dqs/sport/confidence pattern: when the opp dict carries
    paper_estimated_win_prob etc., the inline write site at executor.py
    PAPER_MODE branch emits them on the record. When absent (mimicking
    vig_stack), no spillover."""

    def _run_paper_trade(self, opp, sizing):
        """Helper: mock the 4 internal checks and call execute_trade."""
        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge, \
             patch("bot.executor.get_market") as mock_market:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")
            mock_market.return_value = {"yes_ask": 99, "yes_bid": 43}

            from bot.executor import execute_trade
            return execute_trade(opp, sizing)

    def test_paper_trade_persists_estimated_win_prob_for_live_momentum(self, tmp_state):
        """When opp carries paper_estimated_win_prob / paper_model_source /
        paper_confidence_components, all 3 land on the paper_trades record.
        estimated_win_prob is rounded to 4 decimals."""
        opp = _make_opp(opp_type="live_momentum")
        opp["paper_estimated_win_prob"] = 0.6512345
        opp["paper_model_source"] = "game_context_win_probability_v1"
        opp["paper_confidence_components"] = {
            "wp_edge": 0.05,
            "market_implied": 0.60,
            "pre_clamp_raw": 0.65,
            "clamped": False,
        }
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert rec["estimated_win_prob"] == 0.6512  # rounded to 4 decimals
        assert rec["model_source"] == "game_context_win_probability_v1"
        assert rec["confidence_components"]["wp_edge"] == 0.05
        assert rec["confidence_components"]["clamped"] is False

    def test_paper_trade_omits_proxy_fields_for_vig_stack(self, tmp_state):
        """Spillover prevention — a vig_stack opp without the paper_* keys
        produces a record without estimated_win_prob / model_source /
        confidence_components. Byte-equality regression-lock for vig_stack."""
        opp = _make_opp(opp_type="weather", relative_edge=0.22)
        # explicitly NO paper_estimated_win_prob etc.
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert "estimated_win_prob" not in rec
        assert "model_source" not in rec
        assert "confidence_components" not in rec

    def test_paper_trade_handles_partial_proxy_fields(self, tmp_state):
        """When only paper_estimated_win_prob is set (no model_source / no
        confidence_components), only estimated_win_prob lands on the record.
        Mirrors Session 50's partial-fields handling."""
        opp = _make_opp(opp_type="live_momentum")
        opp["paper_estimated_win_prob"] = 0.72
        # explicitly NOT setting paper_model_source / paper_confidence_components
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert rec["estimated_win_prob"] == 0.72
        assert "model_source" not in rec
        assert "confidence_components" not in rec

    def test_paper_trade_skips_non_numeric_estimated_win_prob(self, tmp_state):
        """Defensive: if paper_estimated_win_prob is non-numeric somehow, the
        record stays clean — no estimated_win_prob field, no crash."""
        opp = _make_opp(opp_type="live_momentum")
        opp["paper_estimated_win_prob"] = "not-a-number"
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert "estimated_win_prob" not in rec


# ---------------------------------------------------------------------------
# Session 100 — paper_trades.json ladder context fields for vig_stack
# (family / rung_count / selected_rung_rank_asc / forecast_bucket_distance /
# time_to_close_hr / source_city / etc.)
# ---------------------------------------------------------------------------

class TestSession100LadderContextFields:
    """Forward-only persistence of the ladder-context paper_* keys on
    vig_stack paper_trades records. When the opp dict carries paper_family
    + the rest of the LADDER_CONTEXT_KEYS, the inline loop at executor.py
    after the Session 99 block renames each `paper_X` to `X` on the record.
    When absent (mimicking live_momentum), the record stays free of ladder
    keys — spillover regression-lock."""

    def _run_paper_trade(self, opp, sizing):
        with patch("bot.executor.verify_contract_direction") as mock_dir, \
             patch("bot.executor.get_balance") as mock_bal, \
             patch("bot.executor._check_position_limits") as mock_pos, \
             patch("bot.executor._verify_edge_still_exists") as mock_edge, \
             patch("bot.executor.get_market") as mock_market:
            mock_dir.return_value = {
                "direction_correct": True, "confidence": "high",
                "explanation": "ok", "warnings": [],
            }
            mock_bal.return_value = {"balance_dollars": 100.0}
            mock_pos.return_value = (True, "ok")
            mock_edge.return_value = (True, "ok")
            mock_market.return_value = {"yes_ask": 99, "yes_bid": 43}

            from bot.executor import execute_trade
            return execute_trade(opp, sizing)

    def test_paper_trade_persists_ladder_context_for_vig_stack(self, tmp_state):
        """When opp carries the full Session 100 paper_* ladder-context set,
        each renames to its on-disk field on the paper_trades record."""
        opp = _make_opp(opp_type="vig_stack_series")
        opp["paper_family"] = "KXHIGHAUS"
        opp["paper_ladder_total_yes_sum_cents"] = 135
        opp["paper_rung_count"] = 3
        opp["paper_selected_rung_rank_asc"] = 3
        opp["paper_selected_rung_rank_desc"] = 1
        opp["paper_rung_strike"] = 89.0
        opp["paper_rung_kind"] = "B"
        opp["paper_no_price_cents"] = 80
        opp["paper_forecast_bucket_distance"] = -0.3
        opp["paper_source_forecast_temp"] = 89.7
        opp["paper_source_city"] = "Austin"
        opp["paper_time_to_close_hr"] = 4.0
        opp["paper_ladder_context_source"] = "vig_stack_ladder_context_v1"
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert rec["family"] == "KXHIGHAUS"
        assert rec["ladder_total_yes_sum_cents"] == 135
        assert rec["rung_count"] == 3
        assert rec["selected_rung_rank_asc"] == 3
        assert rec["selected_rung_rank_desc"] == 1
        assert rec["rung_strike"] == 89.0
        assert rec["rung_kind"] == "B"
        assert rec["no_price_cents"] == 80
        assert rec["forecast_bucket_distance"] == -0.3
        assert rec["source_forecast_temp"] == 89.7
        assert rec["source_city"] == "Austin"
        assert rec["time_to_close_hr"] == 4.0
        assert rec["ladder_context_source"] == "vig_stack_ladder_context_v1"

    def test_paper_trade_omits_ladder_context_for_live_momentum(self, tmp_state):
        """Spillover regression-lock: a live_momentum opp without the
        Session 100 paper_* keys produces a record with NONE of the
        ladder-context fields (family/rung_count/rank/etc). Mirrors Session
        99's vig_stack spillover guard."""
        opp = _make_opp(opp_type="live_momentum")
        # Carry some Session 50/99 keys but NO Session 100 keys.
        opp["paper_confidence"] = 0.85
        opp["paper_sport"] = "NBA"
        opp["paper_estimated_win_prob"] = 0.65
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        # No ladder-context fields land on a live_momentum row.
        for k in ("family", "ladder_total_yes_sum_cents", "rung_count",
                  "selected_rung_rank_asc", "selected_rung_rank_desc",
                  "rung_strike", "rung_kind", "no_price_cents",
                  "forecast_bucket_distance", "source_forecast_temp",
                  "source_city", "time_to_close_hr", "ladder_context_source"):
            assert k not in rec, f"unexpected ladder field {k}={rec[k]} on live_momentum row"
        # Session 50 + 99 fields still land.
        assert rec["confidence"] == 0.85
        assert rec["sport"] == "nba"
        assert rec["estimated_win_prob"] == 0.65

    def test_paper_trade_partial_ladder_fields_for_vig_stack(self, tmp_state):
        """Partial ladder context (e.g. futures opp with no rank/forecast)
        lands only the keys that are present. Mirrors the Session 50/99
        partial-fields behavior — `if v is not None` skips absent keys."""
        opp = _make_opp(opp_type="vig_stack_futures")
        opp["paper_family"] = "KXNBA"
        opp["paper_ladder_total_yes_sum_cents"] = 125
        opp["paper_rung_count"] = 3
        opp["paper_no_price_cents"] = 75
        opp["paper_time_to_close_hr"] = 720.0
        opp["paper_ladder_context_source"] = "vig_stack_ladder_context_v1"
        # NOT setting rank/strike/kind/forecast/source_city (futures opp).
        sizing = _make_sizing(contracts=5, price_cents=45)

        result = self._run_paper_trade(opp, sizing)
        assert result["success"] is True

        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert len(trades) == 1
        rec = trades[0]
        assert rec["family"] == "KXNBA"
        assert rec["rung_count"] == 3
        assert rec["time_to_close_hr"] == 720.0
        # Absent fields stay absent.
        for k in ("selected_rung_rank_asc", "selected_rung_rank_desc",
                  "rung_strike", "rung_kind",
                  "forecast_bucket_distance", "source_forecast_temp",
                  "source_city"):
            assert k not in rec


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
        tmp_state["paper_trades"].write_text(json.dumps([{
            "id": "PAPER-AABBCCDD",
            "ticker": "KXHIGHNY-26APR-70",
            "status": "resting",
            "entry_price": 0.45,
            "contracts": 8,
        }]))

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
        trades = json.loads(tmp_state["paper_trades"].read_text())
        assert trades[0]["status"] == "open"
        assert trades[0]["filled_at"]

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
                   return_value=(True, "ok")), \
             patch("bot.executor.get_market",
                   return_value={"yes_ask": 99, "yes_bid": 43}):
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
                   return_value=(True, "ok")), \
             patch("bot.executor.get_market",
                   return_value={"yes_ask": 99, "yes_bid": 43}):
            exc.execute_trade(opp, sizing)
        recs = [json.loads(l) for l in sandbox.read_text().splitlines() if l.strip()]
        accept = next((r for r in recs if r.get("decision") == "accept"), None)
        assert accept is not None, f"expected an accept log, got decisions: {[r['decision'] for r in recs]}"
        assert accept["extra"]["close_ts"] == "2026-04-26T00:00:00Z"


class TestSession107LiveMomentumDecisionContext:
    """Executor-side live_momentum logs merge watcher-built decision_context."""

    def _decision_context(self):
        return {
            "context_available": True,
            "leader_price": 69,
            "dip_cents": 4,
            "source": "watcher",
            "entry_gate": "dip_buy",
            "missing_context_fields": ["match_phase"],
        }

    def test_live_momentum_position_reject_merges_decision_context(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "s107_pos_limit.jsonl"
        with patch.object(decisions, "DECISIONS_FILE", sandbox):
            exc._check_position_limits(
                balance=100.0,
                cost_dollars=50.0,
                ticker="KXIPLGAME-26MAY11DCPBKS-PBKS",
                opp_type="live_momentum",
                close_ts="2026-05-14T14:00:00Z",
                decision_context=self._decision_context(),
            )
        rec = json.loads(sandbox.read_text().splitlines()[0])
        assert rec["reason"] == "position_cap"
        assert rec["extra"]["leader_price"] == 69
        assert rec["extra"]["dip_cents"] == 4
        assert rec["extra"]["source"] == "watcher"
        assert rec["extra"]["cost_dollars"] == 50.0

    def test_live_momentum_self_check_failed_merges_decision_context(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "s107_self_check.jsonl"
        opp = _make_opp(opp_type="live_momentum", self_check=False)
        opp["decision_context"] = self._decision_context()
        opp["close_ts"] = "2026-05-14T14:00:00Z"
        sizing = _make_sizing(contracts=2, price_cents=45)
        with patch.object(decisions, "DECISIONS_FILE", sandbox), \
             patch("bot.executor.verify_contract_direction",
                   return_value={
                       "direction_correct": True, "confidence": "HIGH",
                       "explanation": "test", "warnings": [],
                       "yes_means": "yes", "no_means": "no",
                       "thesis_supports": "yes", "intended_side": "yes",
                   }), \
             patch("bot.executor.get_market",
                   return_value={"yes_ask": 45, "yes_bid": 44}):
            exc.execute_trade(opp, sizing)
        reject = next(
            json.loads(line) for line in sandbox.read_text().splitlines()
            if json.loads(line).get("reason") == "self_check_failed"
        )
        assert reject["extra"]["leader_price"] == 69
        assert reject["extra"]["dip_cents"] == 4
        assert reject["extra"]["close_ts"] == "2026-05-14T14:00:00Z"

    def test_live_momentum_all_gates_passed_merges_decision_context(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "s107_accept.jsonl"
        opp = _make_opp(opp_type="live_momentum", self_check=True)
        opp["decision_context"] = self._decision_context()
        opp["close_ts"] = "2026-05-14T14:00:00Z"
        sizing = _make_sizing(contracts=2, price_cents=45)
        with patch.object(decisions, "DECISIONS_FILE", sandbox), \
             patch("bot.executor.verify_contract_direction",
                   return_value={
                       "direction_correct": True, "confidence": "HIGH",
                       "explanation": "test", "warnings": [],
                       "yes_means": "yes", "no_means": "no",
                       "thesis_supports": "yes", "intended_side": "yes",
                   }), \
             patch("bot.executor.get_market",
                   return_value={"yes_ask": 45, "yes_bid": 44}):
            exc.execute_trade(opp, sizing)
        accept = next(
            json.loads(line) for line in sandbox.read_text().splitlines()
            if json.loads(line).get("reason") == "all_gates_passed"
        )
        assert accept["extra"]["leader_price"] == 69
        assert accept["extra"]["dip_cents"] == 4
        assert accept["extra"]["source"] == "watcher"
        assert accept["extra"]["contracts"] == 2
        assert accept["extra"]["price_cents"] == 45

    def test_non_live_momentum_accept_does_not_merge_decision_context(self, tmp_state):
        from bot import decisions, executor as exc
        sandbox = tmp_state["tmp"] / "s107_spillover.jsonl"
        opp = _make_opp(opp_type="vig_stack_series", self_check=True)
        opp["ticker"] = "KXTEST-26APR-70"
        opp["decision_context"] = self._decision_context()
        opp["market"] = {"yes_ask": 45}
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
                   return_value=(True, "ok")), \
             patch("bot.executor.get_market",
                   return_value={"yes_ask": 45, "yes_bid": 44}):
            exc.execute_trade(opp, sizing)
        accept = next(
            json.loads(line) for line in sandbox.read_text().splitlines()
            if json.loads(line).get("reason") == "all_gates_passed"
        )
        assert "leader_price" not in accept["extra"]
        assert "dip_cents" not in accept["extra"]
        assert accept["extra"]["contracts"] == 2


# ---------------------------------------------------------------------------
# Session 36 — _paper_record_exit reason persistence
# ---------------------------------------------------------------------------


class TestPaperRecordExitReason:
    """Session 36: _paper_record_exit gained a `reason` parameter that persists
    as `exit_reason` on the paper_trades.json record. exit_position threads
    its caller-supplied reason through both the no-bid path and the normal
    market-exit path. Forward-only: pre-Session-36 records have no exit_reason."""

    def test_paper_record_exit_persists_reason(self, tmp_state):
        """_paper_record_exit(reason='X') writes exit_reason='X' on the record."""
        from bot import executor as exc
        from bot.state_io import save_json as _save_json

        _save_json(tmp_state["paper_trades"], [{
            "id": "ord-123",
            "ticker": "KXFOO",
            "status": "filled",
            "price": 0.50,
        }])

        exc._paper_record_exit("ord-123", 0.65, 1.50, reason="auto_take_profit")

        records = json.loads(tmp_state["paper_trades"].read_text())
        assert records[0]["status"] == "exited_early"
        assert records[0]["exit_price"] == 0.65
        assert records[0]["pnl"] == 1.50
        assert records[0]["exit_reason"] == "auto_take_profit"
        assert "resolved_at" in records[0]

    def test_paper_record_exit_default_reason(self, tmp_state):
        """When called without reason kwarg, default 'unknown' is persisted —
        guards against accidental loss of attribution."""
        from bot import executor as exc
        from bot.state_io import save_json as _save_json

        _save_json(tmp_state["paper_trades"], [{"id": "ord-456", "status": "filled"}])

        exc._paper_record_exit("ord-456", 0.20, -0.75)  # no reason kwarg

        records = json.loads(tmp_state["paper_trades"].read_text())
        assert records[0]["exit_reason"] == "unknown"

    def test_exit_position_threads_reason_through_market_exit(self, tmp_state, monkeypatch):
        """exit_position(ticker, reason='auto_cut_loss') → record has exit_reason='auto_cut_loss'."""
        from bot import executor as exc
        from bot.state_io import save_json as _save_json

        order_id = "ord-789"
        _save_json(tmp_state["positions"], [{
            "ticker": "KXBAR",
            "order_id": order_id,
            "status": "filled",
            "side": "yes",
            "filled": 5,
            "price_cents": 60,
            "paper": True,
        }])
        _save_json(tmp_state["paper_trades"], [{
            "id": order_id,
            "ticker": "KXBAR",
            "status": "filled",
        }])

        # yes_bid = 50¢ → 10¢ loss × 5 contracts = -$0.50
        monkeypatch.setattr(exc, "get_market", lambda t: {"yes_bid": 50, "no_bid": 50})

        result = exc.exit_position("KXBAR", reason="auto_cut_loss")

        assert result["success"] is True
        records = json.loads(tmp_state["paper_trades"].read_text())
        assert records[0]["exit_reason"] == "auto_cut_loss"

    def test_exit_position_no_bid_path_threads_reason_with_suffix(self, tmp_state, monkeypatch):
        """No-bid path persists '<reason>_no_bid' — matches the existing
        pos['exit_reason'] suffix written to positions.json."""
        from bot import executor as exc
        from bot.state_io import save_json as _save_json

        order_id = "ord-nobid"
        _save_json(tmp_state["positions"], [{
            "ticker": "KXNOBID",
            "order_id": order_id,
            "status": "filled",
            "side": "yes",
            "filled": 3,
            "price_cents": 80,
            "paper": True,
        }])
        _save_json(tmp_state["paper_trades"], [{
            "id": order_id,
            "ticker": "KXNOBID",
            "status": "filled",
        }])

        monkeypatch.setattr(exc, "get_market", lambda t: {"yes_bid": 0, "no_bid": 0})

        result = exc.exit_position("KXNOBID", reason="auto_take_profit")

        assert result["success"] is True
        records = json.loads(tmp_state["paper_trades"].read_text())
        assert records[0]["exit_reason"] == "auto_take_profit_no_bid"
