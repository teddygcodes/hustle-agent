"""
Hustle Agent — Kalshi Integration Tests

Tests for the Kalshi client wrapper, engine tool executors,
risk integration, and tool registration.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

from agent import engine, risk, instincts, kalshi_client, projections
from tests.conftest import make_txn, DEFAULT_STATE


def _seed_projection(projection_id="proj-test"):
    """Create a valid pending projection with data_backing for order tests."""
    proj = {
        "id": projection_id,
        "status": "pending",
        "action": "Kalshi trade",
        "cost": 2.50,
        "strategy_type": "kalshi",
        "expected_return": 5.00,
        "data_backing": {
            "source": "test-data-source",
            "source_probability": 0.70,
            "market_price": 0.50,
            "edge": 0.20,
        },
    }
    proj_list = projections._load()
    proj_list.append(proj)
    projections._save(proj_list)
    return projection_id


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestKalshiConfig:

    def test_load_config_missing_file(self, isolated_fs):
        config = kalshi_client._load_config()
        assert config == {}

    def test_load_config_valid(self, isolated_fs):
        config_dir = isolated_fs / "config"
        config_file = config_dir / "kalshi.json"
        config_file.write_text(json.dumps({
            "provider": "kalshi",
            "api_key_id": "test-key",
            "private_key_path": "config/test.pem",
            "environment": "demo",
            "status": "configured",
        }))
        config = kalshi_client._load_config()
        assert config["api_key_id"] == "test-key"
        assert config["environment"] == "demo"

    def test_is_configured_false_when_not_configured(self, isolated_fs):
        config = {
            "api_key_id": "",
            "private_key_path": "",
            "status": "not_configured",
        }
        assert kalshi_client._is_configured(config) is False

    def test_is_configured_true_when_set(self):
        config = {
            "api_key_id": "key123",
            "private_key_path": "config/key.pem",
            "status": "configured",
        }
        assert kalshi_client._is_configured(config) is True

    def test_get_base_url_demo(self):
        assert "demo-api" in kalshi_client._get_base_url({"environment": "demo"})

    def test_get_base_url_production(self):
        assert "elections" in kalshi_client._get_base_url({"environment": "production"})

    def test_get_base_url_default(self):
        url = kalshi_client._get_base_url({})
        assert "demo-api" in url


# ---------------------------------------------------------------------------
# Public endpoints (mocked)
# ---------------------------------------------------------------------------

class TestPublicEndpoints:

    def _make_raw_market(self, ticker="KXTEST", title="Test Market",
                         yes_bid_dollars="0.55", volume_fp="1000"):
        """Return a dict shaped like the Kalshi REST API response."""
        return {
            "ticker": ticker, "title": title, "subtitle": "Subtitle",
            "status": "open",
            "yes_bid_dollars": yes_bid_dollars, "yes_ask_dollars": "0.57",
            "no_bid_dollars": "0.43", "no_ask_dollars": "0.45",
            "last_price_dollars": "0.56",
            "volume_fp": volume_fp, "volume_24h_fp": "500",
            "open_interest_fp": "200",
            "close_time": "2026-04-10T00:00:00Z",
            "event_ticker": "EVT-TEST",
            "series_ticker": "SER-TEST",
            "open_time": "2026-04-01T00:00:00Z",
            "expiration_time": "2026-04-11T00:00:00Z",
            "result": None, "can_close_early": True,
        }

    @patch.object(kalshi_client, "_kalshi_get")
    def test_get_markets_returns_list(self, mock_get, isolated_fs):
        mock_get.return_value = {
            "markets": [self._make_raw_market(), self._make_raw_market("KXOTHER", "Other Market")],
            "cursor": "next123",
        }
        result = kalshi_client.get_markets(limit=10)

        assert "error" not in result
        assert len(result["markets"]) == 2
        assert result["markets"][0]["ticker"] == "KXTEST"

    @patch.object(kalshi_client, "_kalshi_get")
    def test_get_markets_with_query_filter(self, mock_get, isolated_fs):
        mock_get.return_value = {
            "markets": [
                self._make_raw_market("KXBTC", "Bitcoin price above 100k"),
                self._make_raw_market("KXELEC", "Election result"),
            ],
            "cursor": None,
        }
        result = kalshi_client.get_markets(query="bitcoin", limit=10)

        assert len(result["markets"]) == 1
        assert "BTC" in result["markets"][0]["ticker"]

    @patch.object(kalshi_client, "_kalshi_get")
    def test_get_market_detail(self, mock_get, isolated_fs):
        mock_get.return_value = {"market": self._make_raw_market()}
        result = kalshi_client.get_market("KXTEST")

        assert "error" not in result
        assert result["ticker"] == "KXTEST"
        assert result["yes_bid"] == 55


# ---------------------------------------------------------------------------
# Auth checks
# ---------------------------------------------------------------------------

class TestAuthChecks:

    def test_get_balance_no_config(self, isolated_fs):
        result = kalshi_client.get_balance()
        assert "error" in result
        assert "not configured" in result["error"].lower() or "not installed" in result["error"].lower()

    def test_place_order_no_config(self, isolated_fs):
        result = kalshi_client.place_order("KXTEST", "yes", 5, 50)
        assert "error" in result

    def test_get_positions_no_config(self, isolated_fs):
        result = kalshi_client.get_positions()
        assert "error" in result

    def test_cancel_order_no_config(self, isolated_fs):
        result = kalshi_client.cancel_order("order123")
        assert "error" in result


# ---------------------------------------------------------------------------
# Engine tool executors
# ---------------------------------------------------------------------------

class TestKalshiToolExecutors:

    def test_browse_markets_formats_output(self, isolated_fs):
        with patch.object(kalshi_client, "get_markets", return_value={
            "markets": [
                {"ticker": "KXTEST", "title": "Test Market",
                 "yes_bid": 55, "yes_ask": 57, "volume": 1000,
                 "close_time": "2026-04-10T00:00:00Z"},
            ],
            "environment": "demo",
        }):
            result = engine.exec_browse_kalshi_markets("test")
        assert "KXTEST" in result
        assert "Test Market" in result
        assert "demo" in result.lower()

    def test_browse_markets_no_results(self, isolated_fs):
        with patch.object(kalshi_client, "get_markets", return_value={
            "markets": [], "environment": "demo",
        }):
            result = engine.exec_browse_kalshi_markets("nonexistent")
        assert "No markets found" in result

    def test_browse_markets_error(self, isolated_fs):
        with patch.object(kalshi_client, "get_markets", return_value={
            "error": "Connection failed"
        }):
            result = engine.exec_browse_kalshi_markets()
        assert "ERROR" in result

    def test_get_market_detail_formats(self, isolated_fs):
        with patch.object(kalshi_client, "get_market", return_value={
            "ticker": "KXTEST", "title": "Test Market",
            "event_ticker": "EVT", "status": "open",
            "yes_bid": 55, "yes_ask": 57, "no_bid": 43, "no_ask": 45,
            "last_price": 56, "volume": 1000, "volume_24h": 500,
            "open_time": "2026-04-01", "close_time": "2026-04-10",
            "expiration_time": "2026-04-11", "result": None,
            "can_close_early": True, "environment": "demo",
        }), patch.object(kalshi_client, "get_market_orderbook", return_value={
            "ticker": "KXTEST", "yes": [[55, 100]], "no": [[45, 80]],
        }), patch.object(kalshi_client, "get_trades", return_value={
            "trades": [{"count": 10, "yes_price": 55, "taker_side": "yes", "created_time": "2026-04-01"}],
        }):
            result = engine.exec_get_kalshi_market_detail("KXTEST")
        assert "Market Detail" in result
        assert "KXTEST" in result
        assert "Orderbook" in result

    def test_check_portfolio_not_configured(self, isolated_fs):
        result = engine.exec_check_kalshi_portfolio()
        assert "ERROR" in result

    def test_check_portfolio_shows_resting_orders(self, isolated_fs):
        with patch.object(kalshi_client, "get_balance", return_value={
            "balance_dollars": 50.0, "balance_cents": 5000,
        }), patch.object(kalshi_client, "get_positions", return_value={
            "positions": [],
        }), patch.object(kalshi_client, "get_orders", return_value={
            "orders": [{
                "order_id": "ord999", "ticker": "KXTEST", "side": "yes",
                "count": 10, "filled_count": 0, "remaining_count": 10,
                "status": "resting", "yes_price": 45, "no_price": None,
                "created_time": "2026-04-03T12:00:00Z",
            }],
        }):
            result = engine.exec_check_kalshi_portfolio()
        assert "No filled positions" in result
        assert "Resting Orders (1)" in result
        assert "KXTEST" in result
        assert "ord999" in result

    def test_cancel_order_not_configured(self, isolated_fs):
        result = engine.exec_cancel_kalshi_order("order123")
        assert "FAILED" in result


# ---------------------------------------------------------------------------
# Place order — risk integration
# ---------------------------------------------------------------------------

class TestPlaceOrderRisk:

    def test_planning_mode_blocks(self, isolated_fs):
        # Default state is planning mode
        result = engine.exec_place_kalshi_order("KXTEST", "yes", 5, 50, "test")
        assert "PLANNING MODE" in result

    def test_no_projection_blocks(self, active_state):
        result = engine.exec_place_kalshi_order("KXTEST", "yes", 5, 50, "test")
        assert "BLOCKED" in result
        assert "projection_id" in result

    def test_25_cap_blocks(self, active_state):
        # 30 contracts at 90 cents = $27
        pid = _seed_projection("proj-25cap")
        result = engine.exec_place_kalshi_order("KXTEST", "yes", 30, 90, "test", projection_id=pid)
        assert "BLOCKED" in result
        assert "$25" in result

    def test_exploration_mode_5_cap(self, active_state):
        # In explore mode, 10 contracts at 60 cents = $6 should be blocked
        pid = _seed_projection("proj-explore")
        with patch.object(instincts, "load_actions", return_value=[{"status": "won"}]):
            with patch.object(instincts, "get_exploration_mode", return_value="explore"):
                result = engine.exec_place_kalshi_order("KXTEST", "yes", 10, 60, "test", projection_id=pid)
        assert "BLOCKED" in result
        assert "EXPLORATION MODE" in result

    def test_successful_order_full_fill(self, active_state):
        pid = _seed_projection("proj-success")
        with patch.object(kalshi_client, "place_order", return_value={
            "order_id": "ord123", "ticker": "KXTEST", "side": "yes",
            "count": 5, "filled_count": 5, "remaining_count": 0,
            "price_cents": 50, "cost_dollars": 2.50,
            "status": "executed", "client_order_id": "abc",
        }):
            result = engine.exec_place_kalshi_order("KXTEST", "yes", 5, 50, "good odds", projection_id=pid)
        assert "ORDER PLACED" in result
        assert "KXTEST" in result
        assert "ord123" in result
        assert "5 / 5" in result

        # Check ledger was updated with filled amount
        ledger = engine.load_ledger()
        kalshi_txns = [t for t in ledger if t["strategy"] == "kalshi"]
        assert len(kalshi_txns) == 1
        assert kalshi_txns[0]["amount"] == 2.50

    def test_partial_fill_records_filled_only(self, active_state):
        pid = _seed_projection("proj-partial")
        with patch.object(kalshi_client, "place_order", return_value={
            "order_id": "ord456", "ticker": "KXTEST", "side": "yes",
            "count": 10, "filled_count": 3, "remaining_count": 7,
            "price_cents": 50, "cost_dollars": 1.50,
            "status": "resting", "client_order_id": "abc",
        }):
            result = engine.exec_place_kalshi_order("KXTEST", "yes", 10, 50, "test", projection_id=pid)
        assert "3 / 10" in result
        assert "PARTIAL FILL" in result

        # Ledger should only record the filled amount ($1.50), not requested ($5.00)
        ledger = engine.load_ledger()
        kalshi_txns = [t for t in ledger if t["strategy"] == "kalshi"]
        assert len(kalshi_txns) == 1
        assert kalshi_txns[0]["amount"] == 1.50

    def test_no_fill_no_ledger_entry(self, active_state):
        pid = _seed_projection("proj-nofill")
        with patch.object(kalshi_client, "place_order", return_value={
            "order_id": "ord789", "ticker": "KXTEST", "side": "yes",
            "count": 5, "filled_count": 0, "remaining_count": 5,
            "price_cents": 50, "cost_dollars": 0,
            "status": "resting", "client_order_id": "abc",
        }):
            result = engine.exec_place_kalshi_order("KXTEST", "yes", 5, 50, "test", projection_id=pid)
        assert "0 / 5" in result
        assert "NO CONTRACTS FILLED" in result

        # No ledger entry when nothing filled
        ledger = engine.load_ledger()
        kalshi_txns = [t for t in ledger if t["strategy"] == "kalshi"]
        assert len(kalshi_txns) == 0

    def test_order_api_failure(self, active_state):
        pid = _seed_projection("proj-apifail")
        with patch.object(kalshi_client, "place_order", return_value={
            "error": "Insufficient funds on Kalshi"
        }):
            result = engine.exec_place_kalshi_order("KXTEST", "yes", 5, 50, "test", projection_id=pid)
        assert "ORDER FAILED" in result

    def test_insufficient_balance(self, active_state):
        state = engine.load_state()
        state["balance"] = 2.00
        engine.save_state(state)
        result = engine.exec_place_kalshi_order("KXTEST", "yes", 5, 50, "test")
        assert "BLOCKED" in result


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

class TestToolRegistration:

    def test_all_kalshi_tools_in_schemas(self):
        schema_names = {s["name"] for s in engine.TOOL_SCHEMAS}
        assert "browse_kalshi_markets" in schema_names
        assert "get_kalshi_market_detail" in schema_names
        assert "place_kalshi_order" in schema_names
        assert "check_kalshi_portfolio" in schema_names
        assert "cancel_kalshi_order" in schema_names

    def test_all_kalshi_tools_in_executors(self):
        assert "browse_kalshi_markets" in engine.TOOL_EXECUTORS
        assert "get_kalshi_market_detail" in engine.TOOL_EXECUTORS
        assert "place_kalshi_order" in engine.TOOL_EXECUTORS
        assert "check_kalshi_portfolio" in engine.TOOL_EXECUTORS
        assert "cancel_kalshi_order" in engine.TOOL_EXECUTORS

    def test_schema_executor_parity(self):
        schema_names = {s["name"] for s in engine.TOOL_SCHEMAS}
        executor_names = set(engine.TOOL_EXECUTORS.keys())
        assert schema_names == executor_names, f"Mismatch: schemas={schema_names - executor_names}, executors={executor_names - schema_names}"
