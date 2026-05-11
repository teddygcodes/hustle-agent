"""
Hustle Agent — Kalshi Client

Wrapper around the kalshi-python SDK for the agent's prediction market tools.
Public endpoints work without auth. Trading endpoints require API key + private key.
"""

import json
import threading
import urllib.error
import urllib.request
import urllib.parse
import ssl
import uuid
from pathlib import Path

import certifi

# Session 101: per-request total wall-clock cap for _kalshi_get. urlopen's
# socket-level timeout=10 catches connect failures and full stalls, but is a
# per-recv timeout: a server that drips response bytes (1 byte per few seconds)
# keeps each recv() within budget so the timeout never fires, and a single call
# can run for hours. The daemon-thread wrapper below enforces a total
# wall-clock cap. See bot.log 2026-05-11 14:17:51-15:21:42 EDT, scan_id
# 20260511T181751: 3831s drip case that bypassed urlopen(timeout=10).
_KALSHI_TOTAL_TIMEOUT_SEC = 30

try:
    import kalshi_python
    from kalshi_python import (
        Configuration, KalshiClient, MarketsApi, EventsApi, PortfolioApi,
    )
    KALSHI_SDK_AVAILABLE = True
except ImportError:
    KALSHI_SDK_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "kalshi.json"

PRODUCTION_URL = "https://api.elections.kalshi.com/trade-api/v2"
ENVIRONMENTS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "production": PRODUCTION_URL,
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
    """Public client always uses production URL (demo API is unreliable)."""
    global _public_client
    if _public_client is not None:
        return _public_client
    if not KALSHI_SDK_AVAILABLE:
        return None
    cfg = Configuration()
    cfg.host = PRODUCTION_URL
    cfg.ssl_ca_cert = certifi.where()
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
    cfg.ssl_ca_cert = certifi.where()
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
# Raw HTTP helper (used for market data — SDK doesn't map new _dollars fields)
# ---------------------------------------------------------------------------

def _kalshi_get(path: str, params: dict = None) -> dict:
    """GET from Kalshi production REST API, return parsed JSON.

    Retries up to 3 times with exponential backoff on 429 (rate limit) responses.

    Session 101: bounded by _KALSHI_TOTAL_TIMEOUT_SEC total wall-clock per call
    via a daemon worker thread. urlopen's socket-level timeout=10 stays in
    place as the primary defense (catches connect failures and full stalls);
    the daemon-thread guard is the safety net for slow-drip responses
    (server trickles bytes within the per-recv budget, bypassing the socket
    timeout). On timeout the worker is abandoned (daemon=True ensures it does
    not block process exit); the leaked thread holds one TCP connection until
    the OS closes it. Acceptable cost for cadence safety. The error string
    contains "timeout", matched by bot.universe._is_transient_kalshi_error so
    snapshot_universe's existing retry loop handles it identically to a
    connection-reset.
    """
    import time as _time
    url = PRODUCTION_URL + path
    if params:
        query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
        if query:
            url = f"{url}?{query}"
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    state: dict = {"result": None, "exception": None, "done": False}

    def _worker():
        try:
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                        state["result"] = json.loads(r.read())
                        return
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        wait = 2 ** attempt + 1  # 2s, 3s, 5s
                        print(f"  [Kalshi] 429 rate limit on {path} — retrying in {wait}s (attempt {attempt+1}/3)")
                        _time.sleep(wait)
                        continue
                    state["exception"] = e
                    return
            state["exception"] = Exception(
                f"Kalshi 429 rate limit exceeded after 3 retries on {path}"
            )
        except Exception as e:
            state["exception"] = e
        finally:
            state["done"] = True

    t = threading.Thread(
        target=_worker,
        daemon=True,
        name=f"kalshi-get-{path.replace('/', '-')[:40]}",
    )
    t.start()
    t.join(timeout=_KALSHI_TOTAL_TIMEOUT_SEC)
    if not state["done"]:
        raise TimeoutError(
            f"_kalshi_get total wall-clock timeout {_KALSHI_TOTAL_TIMEOUT_SEC}s on {path}"
        )
    if state["exception"] is not None:
        raise state["exception"]
    return state["result"]


def _dollars_to_cents(val):
    """Convert Kalshi's dollar string ('0.0500') to cents (5)."""
    if val is None:
        return None
    try:
        return round(float(val) * 100)
    except (TypeError, ValueError):
        return None


def _parse_market(m: dict) -> dict:
    """Normalize a raw Kalshi market dict into consistent internal format."""
    return {
        "ticker": m.get("ticker"),
        "title": m.get("title"),
        "subtitle": m.get("subtitle"),
        "event_ticker": m.get("event_ticker"),
        "series_ticker": m.get("series_ticker"),
        "status": m.get("status"),
        "yes_bid": _dollars_to_cents(m.get("yes_bid_dollars")),
        "yes_ask": _dollars_to_cents(m.get("yes_ask_dollars")),
        "no_bid": _dollars_to_cents(m.get("no_bid_dollars")),
        "no_ask": _dollars_to_cents(m.get("no_ask_dollars")),
        "last_price": _dollars_to_cents(m.get("last_price_dollars")),
        "volume": int(float(m["volume_fp"])) if m.get("volume_fp") else None,
        "volume_24h": int(float(m["volume_24h_fp"])) if m.get("volume_24h_fp") else None,
        "open_time": m.get("open_time"),
        "close_time": m.get("close_time"),
        "expiration_time": m.get("expiration_time"),
        "result": m.get("result") or "",
        "can_close_early": m.get("can_close_early"),
        "floor_strike": m.get("floor_strike"),
        "rules_primary": m.get("rules_primary"),
        "open_interest": int(float(m["open_interest_fp"])) if m.get("open_interest_fp") else None,
    }


# ---------------------------------------------------------------------------
# Public endpoints (no auth required)
# ---------------------------------------------------------------------------

def get_markets(query: str = "", status: str = "open", limit: int = 20,
                cursor: str = None, event_ticker: str = None,
                series_ticker: str = None) -> dict:
    """Browse available markets. No auth needed."""
    config = _load_config()
    try:
        fetch_limit = 200 if query else min(limit, 200)
        params = {"limit": fetch_limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = _kalshi_get("/markets", params)
        markets = []
        for m in (data.get("markets") or []):
            if query:
                text = f"{m.get('title') or ''} {m.get('subtitle') or ''}".lower()
                if query.lower() not in text:
                    continue
            markets.append(_parse_market(m))
        return {
            "markets": markets[:limit],
            "cursor": data.get("cursor"),
            "environment": config.get("environment", "production"),
        }
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


def get_market(ticker: str) -> dict:
    """Get detailed info on a specific market. No auth needed."""
    config = _load_config()
    try:
        data = _kalshi_get(f"/markets/{urllib.parse.quote(ticker)}")
        m = data.get("market") or data
        result = _parse_market(m)
        result["environment"] = config.get("environment", "production")
        return result
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
    # Ensure int types for pydantic strict validation in kalshi-python SDK
    count = int(count)
    price_cents = int(price_cents)
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
        response = api.create_order(**order_params)
        order = response.order
        remaining = getattr(order, "remaining_count", count)
        filled = count - (remaining if remaining is not None else count)
        return {
            "order_id": order.order_id if hasattr(order, "order_id") else str(order),
            "ticker": ticker,
            "side": side,
            "count": count,
            "filled_count": filled,
            "remaining_count": remaining if remaining is not None else count,
            "price_cents": price_cents,
            "cost_dollars": round(filled * price_cents / 100.0, 2),
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


def get_order(order_id: str) -> dict:
    """Get status of a specific order including fill info. Auth required."""
    client, err = _require_auth()
    if err:
        return {"error": err}
    try:
        api = PortfolioApi(client)
        response = api.get_order(order_id)
        order = response.order
        remaining = getattr(order, "remaining_count", None)
        count = getattr(order, "count", 0)
        filled = count - remaining if remaining is not None else 0
        return {
            "order_id": order.order_id,
            "ticker": order.ticker,
            "side": order.side,
            "count": count,
            "filled_count": filled,
            "remaining_count": remaining,
            "status": order.status,
            "yes_price": order.yes_price,
            "no_price": order.no_price,
            "created_time": getattr(order, "created_time", None),
        }
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


def get_orders(ticker: str = None, status: str = None) -> dict:
    """Get orders, optionally filtered by ticker and/or status. Auth required."""
    client, err = _require_auth()
    if err:
        return {"error": err}
    try:
        api = PortfolioApi(client)
        kwargs = {}
        if ticker:
            kwargs["ticker"] = ticker
        if status:
            kwargs["status"] = status
        response = api.get_orders(**kwargs)
        orders = []
        for o in (response.orders or []):
            remaining = getattr(o, "remaining_count", None)
            count = getattr(o, "count", 0)
            filled = count - remaining if remaining is not None else 0
            orders.append({
                "order_id": o.order_id,
                "ticker": o.ticker,
                "side": o.side,
                "count": count,
                "filled_count": filled,
                "remaining_count": remaining,
                "status": o.status,
                "yes_price": o.yes_price,
                "no_price": o.no_price,
                "created_time": getattr(o, "created_time", None),
            })
        return {"orders": orders}
    except Exception as e:
        return {"error": f"Kalshi API error: {str(e)}"}


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
