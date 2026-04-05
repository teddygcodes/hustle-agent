"""
Glint Trading Bot — Entry Point

One terminal. Pure Python. $0.00 per scan. Telegram on your phone.
Tap GO when you see an edge. Money.

Usage:
    python3 bot/main.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

# Ensure parent is on path for agent imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# File logging must be set up before other bot imports
from bot.logger import setup_file_logging
setup_file_logging()

from bot.config import (
    BOT_STATE_FILE, TELEGRAM_BOT_TOKEN,
    PENDING_FILE, PENDING_MAX, PENDING_GO_WINDOW_HOURS,
    BOT_STATE_DIR, PAPER_MODE, ACTIVE_STRATEGIES, PAPER_TRADES_FILE,
    CRYPTO_SCAN_INTERVAL,
)
from bot.state_io import load_json as _load_json_state
from bot.clv import check_clv_settlements, format_clv_report
from bot.scanner import scan_cycle
from bot.market_maker import (
    scan_market_making_opportunities, format_mm_opportunity,
    execute_mm_pair, check_mm_fills,
)
from bot.math_engine import verify_contract_direction
from bot.sizing import kelly_size
from bot.executor import execute_trade, check_fills, exit_position, exit_all_positions, check_trailing_stops
from bot.tracker import (
    update_positions, resolve_trades, compute_daily_summary,
    get_winrate, get_streak, get_roi_by_strategy,
    get_open_positions_detail, get_trade_history,
)
from bot.notifier import TelegramNotifier, format_opportunity, format_detail
from bot.scheduler import check_scheduled_events
from bot.outcome_tracker import OutcomeTracker
from bot import odds_scraper

_outcome_tracker = OutcomeTracker()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("glint.main")


# ---------------------------------------------------------------------------
# Persistent pending queue helpers
# ---------------------------------------------------------------------------

def _load_pending() -> list[dict]:
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            return []
    return []


def _save_pending(pending: list[dict]):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(pending, indent=2, default=str))
    tmp.rename(PENDING_FILE)


def _prune_pending(pending: list[dict]) -> list[dict]:
    """Remove expired entries (market closed or window passed)."""
    now = datetime.now(timezone.utc)
    live = []
    for entry in pending:
        expiry_str = entry.get("expires_at")
        if expiry_str:
            try:
                exp = datetime.fromisoformat(expiry_str)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if now < exp:
                    live.append(entry)
            except Exception:
                live.append(entry)
        else:
            live.append(entry)
    return live


def _add_to_pending(opp: dict) -> str:
    """Add an opportunity to pending.json. Returns its opp_id."""
    pending = _prune_pending(_load_pending())

    opp_id = str(uuid.uuid4())[:8]

    # Expiry = market close time, or PENDING_GO_WINDOW_HOURS from now
    market = opp.get("market", {})
    close_str = market.get("close_time") or market.get("expiration_time")
    if close_str:
        try:
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            expires_at = close_dt + timedelta(hours=PENDING_GO_WINDOW_HOURS)
        except Exception:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=PENDING_GO_WINDOW_HOURS)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=PENDING_GO_WINDOW_HOURS)

    entry = {
        "opp_id": opp_id,
        "ticker": opp.get("ticker"),
        "type": opp.get("type"),
        "edge": opp.get("edge"),
        "relative_edge": opp.get("relative_edge"),
        "recommended_side": opp.get("recommended_side"),
        "added_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at.isoformat(),
        "opp": opp,
    }
    pending.append(entry)
    # Trim to max
    if len(pending) > PENDING_MAX:
        pending = pending[-PENDING_MAX:]

    _save_pending(pending)
    return opp_id


def _remove_from_pending(opp_id: str):
    pending = _load_pending()
    pending = [p for p in pending if p.get("opp_id") != opp_id]
    _save_pending(pending)


def _market_close_timeout(opp: dict) -> float:
    """Return seconds until market close (or 24h if unknown)."""
    market = opp.get("market", {})
    close_str = market.get("close_time") or market.get("expiration_time")
    if close_str:
        try:
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            secs = (close_dt - datetime.now(timezone.utc)).total_seconds()
            return max(60.0, secs)
        except Exception:
            pass
    return 86400.0  # 24h fallback


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

LOCK_FILE = BOT_STATE_DIR / "bot.lock"


def _pid_is_running(pid: int) -> bool:
    """Return True if a process with this PID is currently alive."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _acquire_lock() -> None:
    """Write PID lockfile, aborting if another instance is running."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
        except ValueError:
            pid = 0
        if pid and _pid_is_running(pid):
            logger.error("Bot already running (PID %d). Exiting.", pid)
            sys.exit(1)
        logger.warning("Stale lock file (PID %d not running). Overwriting.", pid)
    LOCK_FILE.write_text(str(os.getpid()))


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _load_bot_state() -> dict:
    if BOT_STATE_FILE.exists():
        return json.loads(BOT_STATE_FILE.read_text())
    return {}


def _save_bot_state(state: dict):
    BOT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Merge current DK/FD disabled flags before persisting
    state.update(odds_scraper.get_source_flags())
    tmp = BOT_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.rename(BOT_STATE_FILE)


# ---------------------------------------------------------------------------
# Main bot class
# ---------------------------------------------------------------------------

class GlintBot:
    """
    Orchestrates scanning, alerting, execution, and tracking.

    Main loop:
    1. Check scheduled events (morning briefing, nightly summary)
    2. Check if paused/quiet
    3. Run scan_cycle() — now uses ESPN (free)
    4. Resolve trades + update patterns
    5. Check fills on resting orders
    6. Update positions — smart alerts (take profit, cut loss)
    7. Check trailing stops
    8. Alert on opportunities → GO/SKIP flow
    9. Sleep for scan interval
    """

    def __init__(self):
        # Ensure state directory exists before anything tries to write to it
        BOT_STATE_DIR.mkdir(parents=True, exist_ok=True)

        self.notifier = TelegramNotifier()
        self._running = False
        self._last_summary_date: str | None = None
        self._watchdog_alert: str | None = None

    async def start(self):
        """Initialize and start the bot."""
        _acquire_lock()
        logger.info("✨ Glint Trading Bot — Starting...")

        # Initialize Telegram
        await self.notifier.initialize()
        self._register_commands()

        # Start Telegram polling
        await self.notifier.start_polling()

        # Update bot state — also performs watchdog staleness check
        state = _load_bot_state()

        # Restore DK/FD disabled flags from previous session (12h TTL)
        odds_scraper.load_source_flags(state)

        # Watchdog: if the bot was marked running but heartbeat is stale, it likely crashed
        _HEARTBEAT_STALE_MINUTES = 15
        last_hb = state.get("last_heartbeat")
        if state.get("running") and last_hb:
            try:
                hb_time = datetime.fromisoformat(last_hb)
                age_minutes = (datetime.now(timezone.utc) - hb_time).total_seconds() / 60
                if age_minutes > _HEARTBEAT_STALE_MINUTES:
                    logger.warning(
                        "Watchdog: last heartbeat was %.0f min ago — bot likely crashed silently",
                        age_minutes,
                    )
                    self._watchdog_alert = (
                        f"⚠️ Watchdog: bot was running but last heartbeat was "
                        f"{age_minutes:.0f} min ago. Likely crashed silently. Restarting now."
                    )
            except Exception:
                pass

        state["running"] = True
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        _save_bot_state(state)

        # Send watchdog alert now that state is saved and Telegram is ready
        if self._watchdog_alert:
            await self.notifier.send_message(self._watchdog_alert)
            self._watchdog_alert = None

        self._running = True

        # Load and re-alert any surviving pending opportunities from last run
        pending = _prune_pending(_load_pending())
        _save_pending(pending)
        if pending:
            survivors = len(pending)
            tickers = ", ".join(p.get("ticker", "?") for p in pending[:5])
            await self.notifier.send_message(
                f"📋 {survivors} pending opportunit{'y' if survivors == 1 else 'ies'} survived restart: {tickers}\n"
                f"Reply LIST to see them."
            )
            logger.info(f"Loaded {survivors} pending opportunities from disk")

        # Reconcile local positions.json against Kalshi live positions.
        # If bot crashed between placing an order and writing positions.json,
        # the position exists on Kalshi but not locally — recover it here.
        try:
            from bot.config import POSITIONS_FILE
            from agent.kalshi_client import get_positions as _kalshi_get_positions
            _live = _kalshi_get_positions()
            if "error" not in _live:
                _local = json.loads(POSITIONS_FILE.read_text()) if POSITIONS_FILE.exists() else []
                _local_tickers = {p.get("ticker") for p in _local if isinstance(p, dict)}
                _recovered = []
                for _pos in _live.get("positions", []):
                    _ticker = _pos.get("ticker")
                    if _ticker and _ticker not in _local_tickers and (_pos.get("position") or 0) != 0:
                        logger.warning("Reconcile: Kalshi position not in local state — %s", _ticker)
                        _recovered.append({"ticker": _ticker, "source": "kalshi_reconcile", **_pos})
                if _recovered:
                    _local.extend(_recovered)
                    _tmp = POSITIONS_FILE.with_suffix(".tmp")
                    _tmp.write_text(json.dumps(_local, indent=2, default=str))
                    _tmp.rename(POSITIONS_FILE)
                    await self.notifier.send_message(
                        f"⚠️ Reconciled {len(_recovered)} position(s) missing from local state: "
                        + ", ".join(p.get("ticker", "?") for p in _recovered)
                    )
                else:
                    logger.info("Reconcile: local positions.json matches Kalshi (%d positions)", len(_local))
        except Exception as _e:
            logger.warning("Position reconciliation failed: %s", _e)

        if TELEGRAM_BOT_TOKEN:
            await self.notifier.send_message("✨ Glint is online. Scanning for edges...")
            logger.info("Telegram connected — bot is live")
        else:
            logger.warning("No Telegram token — running in console-only mode")

        # Run main loop + independent crypto loop concurrently
        try:
            await asyncio.gather(
                self._main_loop(),
                self._crypto_scan_loop(),
            )
        except asyncio.CancelledError:
            logger.info("Bot cancelled")
        finally:
            await self.stop()

    async def stop(self):
        """Gracefully shut down."""
        self._running = False
        state = _load_bot_state()
        state["running"] = False
        _save_bot_state(state)
        _release_lock()

        await self.notifier.send_message("🛑 Glint stopped.")
        await self.notifier.stop()
        logger.info("Bot stopped")

    def _register_commands(self):
        """Register all Telegram command callbacks."""

        # --- Trading commands (existing) ---

        def handle_go(args: str = ""):
            idx = 1
            if args.strip().isdigit():
                idx = int(args.strip())
            pending = _prune_pending(_load_pending())
            if not pending:
                return "No pending opportunities — try SCAN to find edges"
            i = idx - 1
            if i < 0 or i >= len(pending):
                return f"No opportunity #{idx} — {len(pending)} in queue. Try LIST."
            entry = pending[i]
            opp = entry["opp"]
            opp_id = entry["opp_id"]
            sizing = opp.get("sizing", {})
            if not sizing or sizing.get("contracts", 0) <= 0:
                return f"No valid sizing for #{idx} — try SKIP {idx}"
            # Market maker opportunity
            if opp.get("type") == "market_maker":
                result = execute_mm_pair(opp)
                if result["success"]:
                    _remove_from_pending(opp_id)
                    return (
                        f"✅ MM ORDERS PLACED: {opp['ticker']}\n"
                        f"  BUY: {result.get('buy_order_id', '?')}\n"
                        f"  SELL: {result.get('sell_order_id', '?')}"
                    )
                return f"❌ MM failed: {result['reason']}"
            # Regular edge trade
            result = execute_trade(opp, sizing)
            if result["success"]:
                _remove_from_pending(opp_id)
                from bot.notifier import format_trade_confirmation
                return format_trade_confirmation(result["order_result"])
            return f"❌ Trade failed: {result['reason']}\nOpportunity still in queue. SKIP {idx} to remove."

        def handle_skip(args: str = ""):
            idx = 1
            if args.strip().isdigit():
                idx = int(args.strip())
            pending = _prune_pending(_load_pending())
            if not pending:
                return "No pending opportunities"
            i = idx - 1
            if i < 0 or i >= len(pending):
                return f"No opportunity #{idx} — try LIST"
            entry = pending.pop(i)
            _save_pending(pending)
            return f"⏭️ Skipped #{idx}: {entry.get('ticker', '?')}"

        def handle_list():
            pending = _prune_pending(_load_pending())
            _save_pending(pending)
            if not pending:
                return "No pending opportunities in queue"
            now = datetime.now(timezone.utc)
            lines = [f"PENDING QUEUE ({len(pending)} opportunities):"]
            for i, entry in enumerate(pending, 1):
                ticker = entry.get("ticker", "?")
                rel_edge = entry.get("relative_edge", 0)
                side = entry.get("recommended_side", "?")
                exp_str = entry.get("expires_at", "")
                try:
                    exp_dt = datetime.fromisoformat(exp_str)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    mins_left = int((exp_dt - now).total_seconds() / 60)
                    time_left = f"{mins_left}m left"
                except Exception:
                    time_left = "?"
                lines.append(
                    f"\n[{i}] {ticker[:28]}"
                    f"\n    Edge: {rel_edge:.0%} | Side: {side.upper()} | Expires: {time_left}"
                    f"\n    Reply GO to execute (loads #{i}), SKIP {i} to remove"
                )
            return "\n".join(lines)

        def handle_detail(args: str = ""):
            idx = 1
            if args.strip().isdigit():
                idx = int(args.strip())
            pending = _prune_pending(_load_pending())
            if not pending:
                return "No pending opportunity"
            i = idx - 1
            if i < 0 or i >= len(pending):
                return f"No opportunity #{idx} — try LIST"
            return format_detail(pending[i]["opp"])

        # --- Info commands ---

        def handle_status():
            summary = compute_daily_summary()
            streak = get_streak()
            lines = [
                f"Balance: ${summary['balance']:.2f}",
                f"Today P&L: ${summary['today_pnl']:.2f}",
                f"Total P&L: ${summary['total_pnl']:.2f}",
                f"Win rate: {summary['win_rate']:.0%} ({summary['total_wins']}/{summary['total_trades']})",
                f"Open positions: {summary['open_positions']}",
            ]
            if streak["type"] != "none":
                emoji = "🔥" if streak["type"] == "win" else "❄️"
                lines.append(f"Streak: {emoji} {streak['count']} {streak['type']}s")
            return "\n".join(lines)

        def handle_live():
            positions = get_open_positions_detail()
            if not positions:
                return "No open positions"
            lines = ["OPEN POSITIONS:"]
            for p in positions:
                pnl_str = f"+${p['unrealized_pnl']:.2f}" if p['unrealized_pnl'] >= 0 else f"${p['unrealized_pnl']:.2f}"
                lines.append(
                    f"\n{p['ticker'][:25]}"
                    f"\n  {p['side'].upper()} {p['contracts']}x @ {p['entry_price']}¢"
                    f"\n  P&L: {pnl_str} ({p['pnl_percent']:.0%})"
                )
            return "\n".join(lines)

        def handle_balance():
            from agent.kalshi_client import get_balance
            result = get_balance()
            if "error" in result:
                return f"Balance check failed: {result['error']}"
            return f"Balance: ${result.get('balance_dollars', 0):.2f}"

        def handle_edges():
            try:
                result = scan_cycle()
                opps = result.get("opportunities", [])
                if not opps:
                    return "No edges found right now"
                lines = [f"TOP EDGES ({len(opps)} total):"]
                for o in opps[:3]:
                    lines.append(
                        f"\n{o.get('ticker', '?')[:25]}"
                        f"\n  Type: {o.get('type', '?')} | Edge: {o.get('relative_edge', 0):.0%}"
                        f"\n  Side: {o.get('recommended_side', '?').upper()}"
                    )
                return "\n".join(lines)
            except Exception as e:
                return f"Scan error: {e}"

        # --- Trade management commands ---

        def handle_sell(args: str = ""):
            ticker = args.strip()
            if not ticker:
                return "Usage: SELL <ticker>"
            result = exit_position(ticker, reason="manual_sell")
            if result["success"]:
                return f"✅ Sold {ticker}: {result.get('reason', 'ok')}"
            return f"❌ Sell failed: {result['reason']}"

        def handle_exitall():
            results = exit_all_positions(reason="manual_exit_all")
            if not results:
                return "No open positions to exit"
            sold = sum(1 for r in results if r.get("success"))
            return f"Exited {sold}/{len(results)} positions"

        def handle_trail(args: str = ""):
            parts = args.strip().split()
            if len(parts) < 2:
                return "Usage: TRAIL <ticker> <percent>\nExample: TRAIL KXMVE 20"
            ticker = parts[0]
            try:
                pct = float(parts[1].replace("%", "")) / 100.0
            except ValueError:
                return "Invalid percentage"

            # Set trailing stop on the position
            from bot.config import POSITIONS_FILE
            positions = json.loads(POSITIONS_FILE.read_text()) if POSITIONS_FILE.exists() else []
            found = False
            for p in positions:
                if p.get("ticker", "").startswith(ticker) and p.get("status") in ("filled", "partial"):
                    p["trailing_stop_pct"] = pct
                    found = True
                    break
            if not found:
                return f"No open position matching {ticker}"
            tmp = POSITIONS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(positions, indent=2, default=str))
            tmp.rename(POSITIONS_FILE)
            return f"✅ Trailing stop set: {pct:.0%} on {ticker}"

        # --- Stats commands ---

        def handle_history(args: str = ""):
            n = 5
            if args.strip().isdigit():
                n = int(args.strip())
            trades = get_trade_history(n)
            if not trades:
                return "No trade history yet"
            lines = [f"LAST {len(trades)} TRADES:"]
            for t in trades:
                emoji = "💰" if t["result"] == "won" else "❌"
                lines.append(f"  {emoji} {t['type']} | ${t['pnl']:+.2f} | {t['resolved_at']}")
            return "\n".join(lines)

        def handle_winrate():
            overall = get_winrate()
            by_type = get_roi_by_strategy()
            lines = [
                f"OVERALL: {overall['winrate']:.0%} ({overall['wins']}/{overall['total']}) ROI: {overall['roi']:.0%}"
            ]
            if by_type:
                lines.append("\nBy strategy:")
                for stype, data in by_type.items():
                    lines.append(f"  {stype}: {data['winrate']:.0%} ({data['total']} trades)")
            return "\n".join(lines)

        def handle_roi():
            by_type = get_roi_by_strategy()
            if not by_type:
                return "No resolved trades yet"
            lines = ["ROI BY STRATEGY:"]
            for stype, data in by_type.items():
                lines.append(
                    f"\n{stype}:"
                    f"\n  Trades: {data['total']} | Wins: {data['wins']}"
                    f"\n  Win rate: {data['winrate']:.0%} | ROI: {data['roi']:.0%}"
                    f"\n  Total P&L: ${data['total_pnl']:.2f}"
                )
            return "\n".join(lines)

        def handle_scan():
            try:
                result = scan_cycle()
                opps = result.get("opportunities", [])
                moves = result.get("line_movements", [])
                return (
                    f"Scan complete: {result['games_scanned']} games, "
                    f"{len(opps)} edges, {len(moves)} line moves"
                )
            except Exception as e:
                return f"Scan error: {e}"

        def handle_clv():
            return format_clv_report()

        def handle_mode():
            mode = "PAPER" if PAPER_MODE else "LIVE"
            strats = ", ".join(ACTIVE_STRATEGIES) if ACTIVE_STRATEGIES else "none"
            return f"Mode: {mode}\nActive strategies: {strats}"

        def handle_stats():
            paper_trades = _load_json_state(PAPER_TRADES_FILE)
            if not isinstance(paper_trades, list) or not paper_trades:
                return "No paper trades yet."
            resolved = [t for t in paper_trades if t.get("status") in ("won", "lost")]
            open_count = sum(1 for t in paper_trades if t.get("status") == "open")
            total = len(resolved)
            wins = sum(1 for t in resolved if t.get("status") == "won")
            total_pnl = sum(t.get("pnl") or 0 for t in resolved)
            wr_str = f"{wins/total:.0%} ({wins}/{total})" if total > 0 else "n/a"
            lines = [
                f"PAPER STATS [PAPER MODE]" if PAPER_MODE else "PAPER STATS",
                "",
                f"Total trades: {len(paper_trades)} ({open_count} open)",
                f"Resolved: {total}  |  Win rate: {wr_str}",
                f"Total P&L: ${total_pnl:+.2f}",
                "",
                "By strategy:",
            ]
            by_type: dict = {}
            for t in resolved:
                s = t.get("type", "unknown")
                d = by_type.setdefault(s, {"wins": 0, "total": 0, "pnl": 0.0})
                d["total"] += 1
                if t.get("status") == "won":
                    d["wins"] += 1
                d["pnl"] += t.get("pnl") or 0
            for stype, d in sorted(by_type.items()):
                wr = d["wins"] / d["total"] if d["total"] > 0 else 0
                lines.append(f"  {stype}: {wr:.0%} ({d['total']} trades)  ${d['pnl']:+.2f}")
            if not by_type:
                lines.append("  (no resolved trades yet)")
            return "\n".join(lines)

        # Button callback — handles inline GO/SKIP button presses
        def handle_button(action: str, opp_id: str) -> str:
            pending = _prune_pending(_load_pending())
            entry = next((e for e in pending if e.get("opp_id") == opp_id), None)
            if not entry:
                return "Opportunity expired or already acted on."
            opp = entry["opp"]
            if action == "skip":
                _remove_from_pending(opp_id)
                return f"⏭️ Skipped: {opp.get('ticker', '?')}"
            if action == "go":
                sizing = opp.get("sizing", {})
                if not sizing or sizing.get("contracts", 0) <= 0:
                    return f"❌ No valid sizing — try LIST to see queue"
                if opp.get("type") == "market_maker":
                    result = execute_mm_pair(opp)
                    if result["success"]:
                        _remove_from_pending(opp_id)
                        return (
                            f"✅ MM ORDERS PLACED: {opp['ticker']}\n"
                            f"  BUY: {result.get('buy_order_id', '?')}\n"
                            f"  SELL: {result.get('sell_order_id', '?')}"
                        )
                    return f"❌ MM failed: {result['reason']}"
                result = execute_trade(opp, sizing)
                if result["success"]:
                    _remove_from_pending(opp_id)
                    from bot.notifier import format_trade_confirmation
                    return format_trade_confirmation(result["order_result"])
                return f"❌ Trade failed: {result['reason']}\nStill in queue — tap SKIP to remove."
            return "Unknown action."

        self.notifier.register_button_callback(handle_button)

        # Text commands (queue management + info — GO/SKIP/DETAIL replaced by buttons)
        self.notifier.register_callback("LIST", handle_list)
        self.notifier.register_callback("PENDING", handle_list)  # alias
        self.notifier.register_callback("STATUS", handle_status)
        self.notifier.register_callback("LIVE", handle_live)
        self.notifier.register_callback("POSITIONS", handle_live)  # alias
        self.notifier.register_callback("BALANCE", handle_balance)
        self.notifier.register_callback("EDGES", handle_edges)
        self.notifier.register_callback("SELL", handle_sell)
        self.notifier.register_callback("EXITALL", handle_exitall)
        self.notifier.register_callback("TRAIL", handle_trail)
        self.notifier.register_callback("HISTORY", handle_history)
        self.notifier.register_callback("WINRATE", handle_winrate)
        self.notifier.register_callback("ROI", handle_roi)
        self.notifier.register_callback("SCAN", handle_scan)
        self.notifier.register_callback("CLV", handle_clv)
        self.notifier.register_callback("MODE", handle_mode)
        self.notifier.register_callback("STATS", handle_stats)

    async def _handle_opportunity(self, opp: dict, balance: float) -> None:
        """Process a single opportunity: size → execute (paper) or queue (live)."""
        _outcome_tracker.store_alert(opp)

        side        = opp.get("recommended_side", "yes")
        fair_value  = opp.get("edge_result", {}).get("fair_value", 0.5)
        win_prob    = fair_value if side == "yes" else (1.0 - fair_value)
        price_cents = (
            opp.get("market", {}).get("yes_ask", 50)
            if side == "yes"
            else opp.get("market", {}).get("no_ask", 50)
        )
        opp_type = opp.get("type", "")
        uncertainty_discount = 0.85 if opp_type in ("weather", "series_game_edge") else 1.0
        sizing = kelly_size(
            edge=opp.get("edge", 0),
            probability=win_prob,
            balance=balance,
            price_cents=price_cents,
            uncertainty_discount=uncertainty_discount,
            confidence=opp.get("confidence", 0.75),
        )

        if sizing["contracts"] <= 0:
            logger.info("Skipping %s: sizing says no trade (%s)", opp.get("ticker", "?"), sizing["reason"])
            return

        opp["sizing"] = sizing

        if PAPER_MODE:
            result = execute_trade(opp, sizing)
            alert_lines = [format_opportunity(opp)]
            if result["success"]:
                order = result["order_result"]
                contracts = order.get("count", sizing["contracts"])
                alert_lines.append(
                    f"\n📝 PAPER AUTO-EXECUTED — "
                    f"{contracts}x {side.upper()} @ {price_cents}¢"
                )
                logger.info(
                    "Paper auto-executed: %s | %s @ %s¢ | %s contracts | edge=%.1f%%",
                    opp.get("ticker"), side.upper(), price_cents, contracts,
                    opp.get("relative_edge", 0) * 100,
                )
            else:
                alert_lines.append(f"\n⚠️ PAPER EXECUTION FAILED — {result['reason']}")
                logger.warning("Paper auto-execution failed: %s — %s", opp.get("ticker"), result["reason"])
            await self.notifier.send_message("\n".join(alert_lines))
        else:
            opp_id = _add_to_pending(opp)
            opp["_opp_id"] = opp_id
            pending_now = _load_pending()
            queue_num = next(
                (i + 1 for i, e in enumerate(pending_now) if e.get("opp_id") == opp_id), 1
            )
            opp["_queue_num"] = queue_num
            logger.info(
                "Edge queued #%d (%s): %s | edge=%.1f%% | side=%s",
                queue_num, opp_id, opp.get("ticker"), opp.get("relative_edge", 0) * 100,
                opp.get("recommended_side"),
            )
            await self.notifier.send_alert(opp)

        try:
            from bot.patterns import get_edge_accuracy
            accuracy_note = get_edge_accuracy(opp)
            if accuracy_note:
                await self.notifier.send_message(f"📊 {accuracy_note}")
        except Exception:
            pass

    async def _crypto_scan_loop(self):
        """
        Independent crypto scanner — runs every CRYPTO_SCAN_INTERVAL seconds
        (default 5 min) decoupled from the sports scan interval.

        Scans all assets (BTC, ETH, SOL, XRP, DOGE) across all timeframes
        (15-min, hourly, daily) in parallel. In paper mode, edges auto-execute.
        """
        from bot.kalshi_series import scan_all_crypto_markets
        from agent.kalshi_client import get_balance as kb
        loop = asyncio.get_event_loop()
        logger.info("CRYPTO LOOP: starting independent scan task (interval=%ds)", CRYPTO_SCAN_INTERVAL)
        while self._running:
            try:
                opps = await loop.run_in_executor(None, scan_all_crypto_markets)
                if opps:
                    logger.info("CRYPTO LOOP: %d opportunities found", len(opps))
                    balance_result = kb()
                    balance = balance_result.get("balance_dollars", 0) if "error" not in balance_result else 0
                    for opp in opps:
                        try:
                            await self._handle_opportunity(opp, balance)
                        except Exception as e:
                            logger.error("CRYPTO LOOP: error handling opp %s: %s",
                                         opp.get("ticker", "?"), e)
            except Exception as e:
                logger.error("CRYPTO LOOP: scan error: %s", e)
            await asyncio.sleep(CRYPTO_SCAN_INTERVAL)

    async def _main_loop(self):
        """Core scanning and trading loop."""
        while self._running:
            # ----------------------------------------------------------
            # Step 1: Check scheduled events (morning briefing, nightly)
            # ----------------------------------------------------------
            try:
                await check_scheduled_events(self)
            except Exception as e:
                logger.error(f"Scheduled event error: {e}")

            # ----------------------------------------------------------
            # Step 2: Check if paused
            # ----------------------------------------------------------
            if self.notifier.paused:
                logger.info("Paused — waiting 30s before checking again")
                await asyncio.sleep(30)
                continue

            # Update bot state (heartbeat + scan counters)
            state = _load_bot_state()
            now_ts = datetime.now(timezone.utc).isoformat()
            state["last_heartbeat"] = now_ts
            state["last_scan"] = now_ts
            state["scan_count"] = state.get("scan_count", 0) + 1

            # Reset daily counter if new day
            today = date.today().isoformat()
            if state.get("current_date") != today:
                state["scans_today"] = 0
                state["current_date"] = today
            state["scans_today"] = state.get("scans_today", 0) + 1
            _save_bot_state(state)

            # ----------------------------------------------------------
            # Step 3: Scan for opportunities (ESPN = FREE)
            # ----------------------------------------------------------
            try:
                scan_result = scan_cycle()
            except Exception as e:
                logger.error(f"Scan cycle error: {e}", exc_info=True)
                await asyncio.sleep(60)
                continue

            scan_interval = scan_result.get("scan_interval", 1800)
            opportunities = scan_result.get("opportunities", [])

            # ----------------------------------------------------------
            # Step 4: Check for resolved positions + update patterns
            # ----------------------------------------------------------
            try:
                resolved = resolve_trades()
                for r in resolved:
                    await self.notifier.send_resolution(r)
                # Update pattern analysis after resolutions
                if resolved:
                    try:
                        from bot.patterns import analyze_patterns, save_patterns
                        from bot.config import TRADE_HISTORY_FILE
                        history = json.loads(TRADE_HISTORY_FILE.read_text()) if TRADE_HISTORY_FILE.exists() else []
                        if isinstance(history, list):
                            patterns = analyze_patterns(history)
                            save_patterns(patterns)
                    except Exception as e:
                        logger.debug(f"Pattern update skipped: {e}")
            except Exception as e:
                logger.error(f"Resolution check error: {e}")

            # ----------------------------------------------------------
            # Step 4b: Check CLV settlements
            # ----------------------------------------------------------
            try:
                newly_settled = check_clv_settlements()
                for clv_entry in newly_settled:
                    clv_cents = clv_entry.get("clv_cents", 0)
                    ticker = clv_entry["ticker"]
                    side = clv_entry["side"].upper()
                    result = clv_entry.get("market_result", "?")
                    paper_tag = " [PAPER]" if clv_entry.get("paper") else ""
                    emoji = "✅" if clv_cents > 0 else "❌"
                    await self.notifier.send_message(
                        f"{emoji} CLV SETTLED{paper_tag}: {ticker} {side} | "
                        f"CLV={clv_cents:+.1f}¢ | Result={result}",
                        priority="critical",
                    )
            except Exception as e:
                logger.error(f"CLV settlement check error: {e}")

            # ----------------------------------------------------------
            # Step 5: Check fills on resting orders
            # ----------------------------------------------------------
            try:
                fills = check_fills()
                for f in fills:
                    await self.notifier.send_message(
                        f"📋 Fill update: {f.get('ticker')} — {f.get('filled')}/{f.get('contracts')} filled"
                    )
            except Exception as e:
                logger.error(f"Fill check error: {e}")

            # ----------------------------------------------------------
            # Step 6: Update positions — smart alerts
            # ----------------------------------------------------------
            try:
                alerts = update_positions()
                for a in alerts:
                    alert_type = a.get("type", "position_move")
                    if alert_type == "take_profit":
                        if PAPER_MODE:
                            exit_result = exit_position(a["ticker"], reason="auto_take_profit")
                            if exit_result.get("success"):
                                pnl = exit_result.get("realized_pnl", a["unrealized_pnl"])
                                await self.notifier.send_message(
                                    f"💰 AUTO TAKE PROFIT: {a['ticker']} +{a['pnl_percent']:.0%} "
                                    f"(${pnl:.2f}) — locked in",
                                    priority="critical",
                                )
                            else:
                                await self.notifier.send_message(
                                    f"📈 TAKE PROFIT FAILED: {a['ticker']} +{a['pnl_percent']:.0%} "
                                    f"— {exit_result.get('reason')}\nReply SELL {a['ticker']}",
                                    priority="critical",
                                )
                        else:
                            await self.notifier.send_message(
                                f"📈 TAKE PROFIT? {a['ticker']} +{a['pnl_percent']:.0%} "
                                f"(${a['unrealized_pnl']:.2f})\nReply SELL {a['ticker']}",
                                priority="critical",
                            )
                    elif alert_type == "cut_loss":
                        exit_result = exit_position(a["ticker"], reason="auto_cut_loss")
                        if exit_result.get("success"):
                            pnl = exit_result.get("realized_pnl", a["unrealized_pnl"])
                            await self.notifier.send_message(
                                f"✂️ AUTO CUT: {a['ticker']} {a['pnl_percent']:.0%} "
                                f"(${pnl:.2f}) — exited to stop bleeding",
                                priority="critical",
                            )
                        else:
                            await self.notifier.send_message(
                                f"📉 CUT LOSS FAILED: {a['ticker']} {a['pnl_percent']:.0%} "
                                f"(${a['unrealized_pnl']:.2f}) — {exit_result.get('reason')}\n"
                                f"Reply SELL {a['ticker']}",
                                priority="critical",
                            )
                    elif alert_type == "resting_expiry":
                        await self.notifier.send_message(
                            f"⏰ Resting order {a['ticker']} unfilled for {a['age_minutes']}min",
                            priority="normal",
                        )
                    else:
                        await self.notifier.send_message(
                            f"⚠️ {a['ticker']} — {a['pnl_percent']:.0%} "
                            f"(${a['unrealized_pnl']:.2f})"
                        )
            except Exception as e:
                logger.error(f"Position update error: {e}")

            # ----------------------------------------------------------
            # Step 7: Check trailing stops
            # ----------------------------------------------------------
            try:
                triggered = check_trailing_stops()
                for t in triggered:
                    await self.notifier.send_message(
                        f"🛑 TRAILING STOP: {t.get('ticker')} exited — "
                        f"price dropped below stop level",
                        priority="critical",
                    )
            except Exception as e:
                logger.error(f"Trailing stop check error: {e}")

            # ----------------------------------------------------------
            # Step 7b: Check market maker fills + scan for new MM opps
            # ----------------------------------------------------------
            try:
                mm_events = check_mm_fills()
                for evt in mm_events:
                    event = evt.get("event")
                    ticker = evt.get("ticker", "?")
                    if event == "mm_completed":
                        await self.notifier.send_message(
                            f"💹 MM FILLED: {ticker} — profit ${evt['profit']:.2f} "
                            f"({evt['contracts']} contracts both sides filled)",
                            priority="critical",
                        )
                    elif event == "mm_partial":
                        await self.notifier.send_message(
                            f"📋 MM PARTIAL: {ticker} — {evt['message']}"
                        )
                    elif event == "mm_cancelled":
                        logger.info(f"MM pair timed out and cancelled: {ticker}")
            except Exception as e:
                logger.error(f"MM fill check error: {e}")

            try:
                # Exclude tickers where we detected a large directional weather edge —
                # market-making on mispriced markets invites adverse selection
                weather_edge_tickers = {
                    opp["ticker"] for opp in opportunities
                    if opp.get("type") == "weather" and abs(opp.get("relative_edge", 0)) > 0.25
                }
                mm_opps = scan_market_making_opportunities(exclude_tickers=weather_edge_tickers)
                for mm_opp in mm_opps[:3]:  # Max 3 MM alerts per cycle
                    ticker = mm_opp["ticker"]
                    logger.info(
                        f"MM opportunity: {ticker} spread={mm_opp['spread_cents']}¢ "
                        f"capture={mm_opp['target_capture_cents']}¢"
                    )
                    # Add sizing placeholder so handle_go can validate
                    mm_opp.setdefault("sizing", {"contracts": mm_opp.get("contracts_per_side", 1)})
                    _add_to_pending(mm_opp)
                    await self.notifier.send_message(format_mm_opportunity(mm_opp))
                    # No blocking — scanner keeps running immediately
            except Exception as e:
                logger.error(f"MM scan error: {e}")

            # ----------------------------------------------------------
            # Step 8: Alert on opportunities — fire and forget, no blocking
            # ----------------------------------------------------------
            from agent.kalshi_client import get_balance as kb
            balance_result = kb()
            balance = balance_result.get("balance_dollars", 0) if "error" not in balance_result else 0

            for opp in opportunities:
                await self._handle_opportunity(opp, balance)

            # ----------------------------------------------------------
            # Step 8b: OutcomeTracker — resolve settled alerts & calibration
            # ----------------------------------------------------------
            try:
                loop = asyncio.get_event_loop()
                resolved = await loop.run_in_executor(None, _outcome_tracker.check_and_resolve)
                if resolved:
                    logger.info("OutcomeTracker resolved %d market(s)", resolved)
                await loop.run_in_executor(None, _outcome_tracker.log_calibration_summary)
            except Exception as e:
                logger.debug(f"OutcomeTracker step skipped: {e}")

            # ----------------------------------------------------------
            # Step 9: Sleep until next scan
            # ----------------------------------------------------------
            logger.info(f"Next scan in {scan_interval}s ({scan_interval // 60}m)")
            await asyncio.sleep(scan_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def async_main():
    bot = GlintBot()

    # Handle SIGINT/SIGTERM for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))

    await bot.start()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
