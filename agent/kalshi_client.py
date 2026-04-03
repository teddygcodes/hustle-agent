"""
Hustle Agent — Kalshi Client

Wrapper around the kalshi-python SDK for the agent's prediction market tools.
Public endpoints work without auth. Trading endpoints require API key + private key.
"""

import json
import uuid
from pathlib import Path

try:
    import kalshi_python
    from kalshi_python import (
        Configuration, KalshiClient, MarketsApi, EventsApi, PortfolioApi,
        CreateOrderRequest,
    )
    KALSHI_SDK_AVAILABLE = True
except ImportError:
    KALSHI_SDK_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "kalshi.json"

ENVIRONMENTS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "production": "https://api.elections.kalshi.com/trade-api/v2",
}

# Cached clients (lazy init)
_public_client = None
_auth_client = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def _get_base_url(config: dict) -> str:
    env = config.get("environment", "demo")
    return ENVIRONMENTS.get(env, ENVIRONMENTS["demo"])


def _is_configured(config: dict) -> bool:
    return (
        config.get("status") != "not_configured"
        and bool(config.get("api_key_id"))
        and bool(config.get("private_key_path"))
    )


# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------

def _get_public_client(config: dict = None) -> KalshiClient:
    global _public_client
    if _public_client is not None:
        return _public_client
    if not KALSHI_SDK_AVAILABLE:
        return None
    if config is None:
        config = _load_config()
    cfg = Configuration()
    cfg.host = _get_base_url(config)
    _public_client = KalshiClient(cfg)
    return _public_client


def _get_auth_client(config: dict = None) -> KalshiClient:
    global _auth_client
    if _auth_client is not None:
        return _auth_client
    if not KALSHI_SDK_AVAILABLE:
        return None
    if config is None:
        config = _load_config()
    if not _is_configured(config):
        return None
    cfg = Configuration()
    cfg.host = _get_base_url(config)
    client = KalshiClient(cfg)
    key_path = config["private_key_path"]
    if not Path(key_path).is_absolute():
        key_path = str(BASE_DIR / key_path)
    client.set_kalshi_auth(config["api_key_id"], key_path)
    _auth_client = client
    return _auth_client


def reset_clients():
    """Reset cached clients (useful for tests or config changes)."""
    global _public_client, _auth_client
    _public_client = None
    _auth_client = None


# ---------------------------------------------------------------------------
# Public endpoints (no auth required)
# ---------------------------------------------------------------------------

def get_markets(query: str = "", status: str = "open", limit: int = 20,
                cursor: str = None, event_ticker: str = None) -> dict:
    """Browse available markets. No auth needed."""
    if not KALSHI_SDK_AVAILABLE:
        return {"error": "kalshi-python SDK not installed. Run: pip install kalshi-python"}
    config = _load_config()
    client = _get_public_client(config)
    try:
        api = MarketsApi(client)
        kwargs = {"limit": min(limit, 200), "status": status}
        if cursor:
            kwargs["cursor"] = cursor
        if event_ticker:
            kwargs["event_ticker"] = event_ticker
        response = api.get_markets(**kwargs)
        markets = []
        for m in (response.markets or []):
            market_data = {
                "ticker": m.ticker,
                "title": m.title,
                "subtitle": m.subtitle,
                "status": m.status,
                "yes_bid": m.yes_bid,
                "yes_ask": m.yes_ask,
                "no_bid": m.no_bid,
                "no_ask": m.no_ask,
                "last_price": m.last_price,
                "volume": m.volume,
                "volume_24h": m.volume_24h,
                "close_time": m.close_time,
                "event_ticker": m.event_ticker,
            }
            if query:
                text = f"{m.title or ''} {m.subtitle or ''}".lower()
                if query.lower() not in text:
                    continue
            markets.append(market_data)
        return {
            "markets": markets[:limit],
            "cursor": response.cursor if hasattr(response, "cursor") else None,
            "environment": config.get("environment", "demo"),
        }
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


def get_market(ticker: str) -> dict:
    """Get detailed info on a specific market. No auth needed."""
    if not KALSHI_SDK_AVAILABLE:
        return {"error": "kalshi-python SDK not installed. Run: pip install kalshi-python"}
    config = _load_config()
    client = _get_public_client(config)
    try:
        api = MarketsApi(client)
        response = api.get_market(ticker)
        m = response.market
        return {
            "ticker": m.ticker,
            "title": m.title,
            "subtitle": m.subtitle,
            "event_ticker": m.event_ticker,
            "series_ticker": m.series_ticker,
            "status": m.status,
            "yes_bid": m.yes_bid,
            "yes_ask": m.yes_ask,
            "no_bid": m.no_bid,
            "no_ask": m.no_ask,
            "last_price": m.last_price,
            "volume": m.volume,
            "volume_24h": m.volume_24h,
            "open_time": m.open_time,
            "close_time": m.close_time,
            "expiration_time": m.expiration_time,
            "result": m.result,
            "can_close_early": m.can_close_early,
            "environment": config.get("environment", "demo"),
        }
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


def get_market_orderbook(ticker: str, depth: int = 10) -> dict:
    """Get current orderbook for a market. No auth needed."""
    if not KALSHI_SDK_AVAILABLE:
        return {"error": "kalshi-python SDK not installed. Run: pip install kalshi-python"}
    config = _load_config()
    client = _get_public_client(config)
    try:
        api = MarketsApi(client)
        response = api.get_market_orderbook(ticker, depth=depth)
        ob = response.orderbook
        return {
            "ticker": ticker,
            "yes": [[level.price, level.quantity] for level in (ob.yes or [])] if ob else [],
            "no": [[level.price, level.quantity] for level in (ob.no or [])] if ob else [],
        }
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


def get_events(status: str = None, limit: int = 20, cursor: str = None,
               series_ticker: str = None) -> dict:
    """Browse event categories. No auth needed."""
    if not KALSHI_SDK_AVAILABLE:
        return {"error": "kalshi-python SDK not installed. Run: pip install kalshi-python"}
    config = _load_config()
    client = _get_public_client(config)
    try:
        api = EventsApi(client)
        kwargs = {"limit": min(limit, 200)}
        if status:
            kwargs["status"] = status
        if cursor:
            kwargs["cursor"] = cursor
        if series_ticker:
            kwargs["series_ticker"] = series_ticker
        response = api.get_events(**kwargs)
        events = []
        for e in (response.events or []):
            events.append({
                "event_ticker": e.event_ticker,
                "title": e.title,
                "series_ticker": e.series_ticker,
                "status": getattr(e, "status", None),
            })
        return {
            "events": events,
            "cursor": response.cursor if hasattr(response, "cursor") else None,
        }
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


def get_trades(ticker: str, limit: int = 20) -> dict:
    """Get recent trades for a market. No auth needed."""
    if not KALSHI_SDK_AVAILABLE:
        return {"error": "kalshi-python SDK not installed. Run: pip install kalshi-python"}
    config = _load_config()
    client = _get_public_client(config)
    try:
        api = MarketsApi(client)
        response = api.get_trades(ticker=ticker, limit=min(limit, 100))
        trades = []
        for t in (response.trades or []):
            trades.append({
                "ticker": getattr(t, "ticker", ticker),
                "count": getattr(t, "count", None),
                "yes_price": getattr(t, "yes_price", None),
                "no_price": getattr(t, "no_price", None),
                "created_time": getattr(t, "created_time", None),
                "taker_side": getattr(t, "taker_side", None),
            })
        return {"trades": trades}
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


# ---------------------------------------------------------------------------
# Authenticated endpoints
# ---------------------------------------------------------------------------

def _require_auth():
    """Check auth and return (client, None) or (None, error_string)."""
    if not KALSHI_SDK_AVAILABLE:
        return None, "kalshi-python SDK not installed. Run: pip install kalshi-python"
    config = _load_config()
    if not _is_configured(config):
        return None, (
            "Kalshi not configured. Ask Tyler for API credentials. "
            "Config file: config/kalshi.json needs api_key_id and private_key_path."
        )
    client = _get_auth_client(config)
    if client is None:
        return None, "Failed to initialize Kalshi client. Check config/kalshi.json."
    return client, None


def get_balance() -> dict:
    """Get Kalshi account balance. Auth required."""
    client, err = _require_auth()
    if err:
        return {"error": err}
    try:
        api = PortfolioApi(client)
        response = api.get_balance()
        # Balance is returned in cents
        balance_cents = response.balance or 0
        return {
            "balance_cents": balance_cents,
            "balance_dollars": round(balance_cents / 100.0, 2),
        }
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


def place_order(ticker: str, side: str, count: int, price_cents: int,
                action: str = "buy") -> dict:
    """Place an order on Kalshi. Auth required.

    Args:
        ticker: Market ticker
        side: "yes" or "no"
        count: Number of contracts
        price_cents: Limit price in cents (1-99)
        action: "buy" or "sell"
    """
    client, err = _require_auth()
    if err:
        return {"error": err}
    if price_cents < 1 or price_cents > 99:
        return {"error": f"Price must be 1-99 cents, got {price_cents}"}
    if count < 1:
        return {"error": f"Count must be at least 1, got {count}"}
    try:
        api = PortfolioApi(client)
        order_params = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
        }
        if side == "yes":
            order_params["yes_price"] = price_cents
        else:
            order_params["no_price"] = price_cents
        request = CreateOrderRequest(**order_params)
        response = api.create_order(request)
        order = response.order
        return {
            "order_id": order.order_id if hasattr(order, "order_id") else str(order),
            "ticker": ticker,
            "side": side,
            "count": count,
            "price_cents": price_cents,
            "cost_dollars": round(count * price_cents / 100.0, 2),
            "status": getattr(order, "status", "submitted"),
            "client_order_id": order_params["client_order_id"],
        }
    except Exception as e:
        return {"error": f"Kalshi order failed: {str(e)}"}


def cancel_order(order_id: str) -> dict:
    """Cancel a resting order. Auth required."""
    client, err = _require_auth()
    if err:
        return {"error": err}
    try:
        api = PortfolioApi(client)
        api.cancel_order(order_id)
        return {"cancelled": True, "order_id": order_id}
    except Exception as e:
        return {"error": f"Kalshi cancel failed: {str(e)}"}


def get_positions(event_ticker: str = None) -> dict:
    """Get current open positions. Auth required."""
    client, err = _require_auth()
    if err:
        return {"error": err}
    try:
        api = PortfolioApi(client)
        kwargs = {}
        if event_ticker:
            kwargs["event_ticker"] = event_ticker
        response = api.get_positions(**kwargs)
        positions = []
        for p in (response.positions or []):
            positions.append({
                "ticker": p.ticker,
                "event_ticker": p.event_ticker,
                "position": p.position,
                "realized_pnl": p.realized_pnl,
                "resting_order_count": p.resting_order_count,
                "fees_paid": p.fees_paid,
                "total_cost": p.total_cost,
                "market_result": p.market_result,
            })
        return {"positions": positions}
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


def get_portfolio_history(limit: int = 50) -> dict:
    """Get trade fill history. Auth required."""
    client, err = _require_auth()
    if err:
        return {"error": err}
    try:
        api = PortfolioApi(client)
        response = api.get_fills(limit=min(limit, 100))
        fills = []
        for f in (response.fills or []):
            fills.append({
                "ticker": getattr(f, "ticker", None),
                "order_id": getattr(f, "order_id", None),
                "side": getattr(f, "side", None),
                "action": getattr(f, "action", None),
                "count": getattr(f, "count", None),
                "yes_price": getattr(f, "yes_price", None),
                "no_price": getattr(f, "no_price", None),
                "created_time": getattr(f, "created_time", None),
            })
        return {"fills": fills}
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}
