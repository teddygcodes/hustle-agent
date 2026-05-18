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
    CRYPTO_SCAN_INTERVAL, CRYPTO_ENABLED,
    LIVE_POLL_INTERVAL,
)
from bot.state_io import load_json as _load_json_state
from bot.clv import check_clv_settlements, format_clv_report
from bot.scanner import scan_cycle
from bot import universe as _universe
from bot.daily_log import update_daily_log
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
from bot.position_monitor import recheck_open_edges, scan_related_markets
from bot import odds_scraper

_outcome_tracker = OutcomeTracker()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("glint.main")

# Vig-stack opp_types are exempt from auto-exit paths (edge_flipped, take_profit,
# cut_loss). Edge is structural (YES sum > 100¢ on the ladder), so individual
# contract price moves don't invalidate it — only a full ladder vig collapse
# would. See Battle Scar #9 in CLAUDE.md and Session 36.
_VIG_STACK_OPP_TYPES = ("vig_stack_no", "vig_stack_series")

# Session 62: sizing/family-cap handling must include vig_stack_futures even
# though exit exemptions intentionally remain limited to _VIG_STACK_OPP_TYPES.
_VIG_STACK_SIZING_TYPES = ("vig_stack_no", "vig_stack_series", "vig_stack_futures")

# Session 98 (2026-05-11): outer wall-clock cap on snapshot_universe.
# bot/universe.py:_SNAPSHOT_DEADLINE_SEC=300 is checked per-page, so layered
# Kalshi-client + urllib3 retries can blow past it by hours (Phase 0.1
# evidence: May 10 03:08-11:07 UTC = 479-min gap where the internal deadline
# fired only after 666 pages × ~38s/page = 7+ hours). 600s is 1.5x the
# worst-case non-pathological runtime (300s + ~100s overshoot) and well above
# normal-conditions max (~30s) and moderate-flake max (~120s). The Session 39
# run_in_executor wrap keeps OTHER coroutines responsive; this outer wait_for
# bounds _main_loop's own commitment so cadence recovers from Kalshi flakes.
# On timeout: log, increment bot_state.snapshot_outer_timeout_count_24h,
# sleep 60s, continue. The leaked executor thread runs to completion in the
# background — acceptable for rare 1-2x/day timeouts.
_SNAPSHOT_OUTER_TIMEOUT_SEC = 600

# Session 150 (2026-05-17): outer wedge-detection guard for the 4 S148-wrapped
# functions in _main_loop (scan_cycle, check_clv_settlements,
# recheck_open_edges, scan_related_markets). S148's run_in_executor wrap
# moved sync I/O off the event loop but left each await unbounded — a wedged
# executor thread silently halted loop progress (2026-05-17: scan_cycle hung
# 5h+ in the WEATHER branch). 900s = one IDLE scan cycle (per SCAN_INTERVAL
# IDLE post-S109); tight enough to abort within one cycle of the next
# expected scan, loose enough to clear high-Kalshi-load scans. On timeout:
# log, increment per-function *_outer_timeout_count_24h counter, fall through
# to the existing recovery / continue path. Executor threads leak on timeout
# (Python concurrent.futures can't cancel running threads); acceptable for
# transient wedges, watch-list auto-fires if a single counter hits >= 3/day
# sustained.
_EXECUTOR_OUTER_TIMEOUT_SEC = 900


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
    """Only unlink the lockfile if it still contains OUR PID.

    Race condition this guards against (Battle Scar #3 follow-up, May 3 2026):
    if an old orphan process receives SIGTERM AFTER a new process has already
    acquired the lock with its own PID, the old process's SIGTERM handler must
    NOT unconditionally unlink — that wipes the new process's lock content.
    The new process's periodic `LOCK_FILE.touch()` would then recreate the
    file as empty, leaving an empty lockfile despite a healthy bot.
    """
    if not LOCK_FILE.exists():
        return
    try:
        held_by = int(LOCK_FILE.read_text().strip())
    except (ValueError, OSError):
        # Corrupt/empty/unreadable — leave alone; let _acquire_lock handle on next start.
        return
    if held_by == os.getpid():
        LOCK_FILE.unlink(missing_ok=True)


def _load_bot_state() -> dict:
    if BOT_STATE_FILE.exists():
        try:
            return json.loads(BOT_STATE_FILE.read_text())
        except json.JSONDecodeError:
            logger.warning("bot_state.json corrupt — resetting to defaults")
            return {}
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
        self._active_watchers: dict[str, tuple] = {}  # query -> (LiveGameWatcher, asyncio.Task)

    async def start(self):
        """Initialize and start the bot."""
        _acquire_lock()
        logger.info("✨ Glint Trading Bot — Starting...")

        # Initialize Telegram — non-fatal if SSL/network fails
        try:
            await self.notifier.initialize()
            self._register_commands()
            await self.notifier.start_polling()
        except Exception as e:
            logger.warning("Telegram init failed (%s) — running without notifications", e)

        # Update bot state — also performs watchdog staleness check
        state = _load_bot_state()

        # Restore DK/FD disabled flags from previous session (12h TTL)
        odds_scraper.load_source_flags(state)

        # Watchdog: if the bot was marked running but heartbeat is stale, it likely crashed.
        # The Telegram alert path was silenced in Session 4 (noisy during normal operation);
        # the logger.warning is the surviving signal — surfaces in bot.log for postmortem.
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
            except Exception:
                pass

        # Scheduler drift check: surface silent briefing failures so an 11-day
        # stale morning briefing doesn't go unnoticed again (Session 4).
        for _field, _label in (
            ("last_morning_briefing", "morning briefing"),
            ("last_nightly_summary", "nightly summary"),
        ):
            _last = state.get(_field)
            if not _last:
                continue
            try:
                _stale = (date.today() - date.fromisoformat(_last)).days
                if _stale > 2:
                    logger.warning(
                        "Scheduler drift: %s %d days stale (last=%s) — check bot.log for exceptions",
                        _label, _stale, _last,
                    )
            except Exception:
                pass

        state["running"] = True
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        _save_bot_state(state)

        self._running = True

        # Load and re-alert any surviving pending opportunities from last run
        pending = _prune_pending(_load_pending())
        _save_pending(pending)
        if pending:
            survivors = len(pending)
            tickers = ", ".join(p.get("ticker", "?") for p in pending[:5])
            # Pending opportunities notification silenced
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
            # Startup notification silenced — bot runs quietly
            logger.info("Telegram connected — bot is live")
        else:
            logger.warning("No Telegram token — running in console-only mode")

        # Run main loop + crypto loop + live match scanner + heartbeat + position check concurrently
        tasks = [
            self._main_loop(),
            self._live_scan_loop(),
            self._heartbeat_loop(),
            self._position_check_loop(),
        ]
        if CRYPTO_ENABLED:
            tasks.append(self._crypto_scan_loop())
        else:
            logger.info("CRYPTO LOOP: disabled (CRYPTO_ENABLED=False)")
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot cancelled")
        finally:
            await self.stop()

    async def stop(self):
        """Gracefully shut down."""
        self._running = False

        # Session 58: cancel active watchers BEFORE tearing down the notifier.
        # Watchers spawned via _live_scan_loop / handle_watch are standalone
        # asyncio tasks (not part of the gather'd task list), so a notifier
        # shutdown alone leaves them ticking on a dead HTTPXRequest until
        # asyncio.run() finally GCs them — observed at 16+ minutes on May 6,
        # generating 234 'HTTPXRequest is not initialized' errors during the
        # zombie window. Session 52's retry-with-backoff masks the failures
        # as warnings, so the underlying bug stays invisible during normal
        # operation. Mirror handle_unwatch's pattern: stop()→cancel()→clear.
        if self._active_watchers:
            logger.info("Stopping %d active watcher(s)", len(self._active_watchers))
            watchers_to_stop = list(self._active_watchers.values())
            for watcher, task in watchers_to_stop:
                try:
                    watcher.stop()
                except Exception:
                    pass
                task.cancel()
            self._active_watchers.clear()
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *[t for _, t in watchers_to_stop],
                        return_exceptions=True,
                    ),
                    timeout=5.0,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        state = _load_bot_state()
        state["running"] = False
        _save_bot_state(state)
        _release_lock()

        # Shutdown notification silenced
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
            exited = [t for t in paper_trades if t.get("status") == "exited_early"]
            open_count = sum(1 for t in paper_trades if t.get("status") == "open")
            total_settled = len(resolved)
            wins = sum(1 for t in resolved if t.get("status") == "won")
            settled_pnl = sum(t.get("pnl") or 0 for t in resolved)
            exited_pnl = sum(t.get("pnl") or 0 for t in exited)
            total_pnl = settled_pnl + exited_pnl
            wr_str = f"{wins/total_settled:.0%} ({wins}/{total_settled})" if total_settled > 0 else "n/a"
            lines = [
                f"PAPER STATS [PAPER MODE]" if PAPER_MODE else "PAPER STATS",
                "",
                f"Total trades: {len(paper_trades)} ({open_count} open)",
                f"Settled: {total_settled}  |  Win rate: {wr_str}",
                f"Exited early: {len(exited)}  |  Exit P&L: ${exited_pnl:+.2f}",
                f"Settled P&L: ${settled_pnl:+.2f}",
                f"Total P&L: ${total_pnl:+.2f}",
                "",
                "By strategy:",
            ]
            # Include both resolved and exited in strategy breakdown
            by_type: dict = {}
            for t in resolved + exited:
                s = t.get("type", "unknown")
                d = by_type.setdefault(s, {"wins": 0, "settled": 0, "exited": 0, "pnl": 0.0})
                if t.get("status") in ("won", "lost"):
                    d["settled"] += 1
                    if t.get("status") == "won":
                        d["wins"] += 1
                else:
                    d["exited"] += 1
                d["pnl"] += t.get("pnl") or 0
            for stype, d in sorted(by_type.items()):
                total_s = d["settled"] + d["exited"]
                wr = d["wins"] / d["settled"] if d["settled"] > 0 else 0
                wr_part = f"{wr:.0%} win" if d["settled"] > 0 else "no settlements"
                lines.append(f"  {stype}: {total_s} trades ({wr_part})  ${d['pnl']:+.2f}")
            if not by_type:
                lines.append("  (no closed trades yet)")
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

        def handle_restart(args=""):
            """Restart the bot process (launchd/watchdog will auto-restart)."""
            import os as _os, subprocess
            pid = _os.getpid()
            logger.info("RESTART command received — scheduling kill -9 %d", pid)
            subprocess.Popen(
                ["bash", "-c", f"sleep 2 && kill -9 {pid}"],
                start_new_session=True,
            )
            return "♻️ Restarting bot... back in ~10 seconds."

        def handle_stop(args=""):
            """Stop the bot and unload launchd so it stays down."""
            import os as _os, subprocess
            pid = _os.getpid()
            logger.info("STOP command received — unloading launchd and killing %d", pid)
            subprocess.Popen(
                ["bash", "-c",
                 "sleep 2 "
                 "&& launchctl unload ~/Library/LaunchAgents/com.hustle-agent.bot.plist 2>/dev/null; "
                 f"kill -9 {pid}"],
                start_new_session=True,
            )
            return "🛑 Stopping bot. Send START to bring it back (from Claude Code)."

        def handle_logs(args=""):
            """Tail the last 20 lines of bot.log."""
            try:
                from bot.config import BASE_DIR
                log_file = BASE_DIR / "bot" / "logs" / "bot.log"
                if not log_file.exists():
                    return "No log file found."
                lines = log_file.read_text().strip().split("\n")
                tail = lines[-20:]
                return "\n".join(tail)
            except Exception as e:
                return f"Error reading logs: {e}"

        self.notifier.register_callback("RESTART", handle_restart)
        self.notifier.register_callback("STOP", handle_stop)
        self.notifier.register_callback("LOGS", handle_logs)

        # ------------------------------------------------------------------ #
        # WATCH / UNWATCH — Live game targeting
        # ------------------------------------------------------------------ #

        async def handle_watch(args=""):
            """WATCH <game_query> — start a live game watcher."""
            query = args.strip()
            if not query:
                return "Usage: WATCH <team or game>  (e.g. WATCH lakers  or  WATCH cubs)"

            if query.lower() in self._active_watchers:
                return f"Already watching '{query}'. Send UNWATCH first to restart."

            from bot.live_watcher import LiveGameWatcher
            from agent.kalshi_client import get_balance as kb

            balance_result = kb()
            balance = (
                balance_result.get("balance_dollars", 0)
                if "error" not in balance_result else 0
            )
            watcher = LiveGameWatcher(query, self.notifier, balance=balance)
            task = asyncio.create_task(self._run_watcher(query, watcher))
            self._active_watchers[query.lower()] = (watcher, task)
            return f"Watching: {query}\nPolling every {LIVE_POLL_INTERVAL}s. Send UNWATCH to stop."

        async def handle_unwatch(args=""):
            """UNWATCH — stop all active game watchers."""
            if not self._active_watchers:
                return "No active watchers."
            stopped = []
            for key, (watcher, task) in list(self._active_watchers.items()):
                watcher.stop()
                task.cancel()
                stopped.append(key)
            self._active_watchers.clear()
            return f"Stopped: {', '.join(stopped)}"

        async def handle_recap(args=""):
            from bot.live_watcher import get_daily_recap
            date_str = args.strip() if args.strip() else None
            return get_daily_recap(date_str)

        async def handle_analyze(args=""):
            from bot.live_watcher import analyze_ticks
            date_str = args.strip() if args.strip() else None
            return analyze_ticks(date_str)

        self.notifier.register_callback("WATCH", handle_watch)
        self.notifier.register_callback("UNWATCH", handle_unwatch)
        self.notifier.register_callback("RECAP", handle_recap)
        self.notifier.register_callback("ANALYZE", handle_analyze)

    async def _handle_opportunity(self, opp: dict, balance: float) -> None:
        """Process a single opportunity: size → execute (paper) or queue (live)."""
        _outcome_tracker.store_alert(opp)

        side        = opp.get("recommended_side", "yes")
        fair_value  = opp.get("edge_result", {}).get("fair_value", 0.5)
        opp_type    = opp.get("type", "")
        # For vig_stack trades, fair_value is already from the NO perspective
        # (probability that NO wins). Don't flip it.
        if opp_type in _VIG_STACK_SIZING_TYPES:
            win_prob = fair_value
        else:
            win_prob = fair_value if side == "yes" else (1.0 - fair_value)
        price_cents = (
            opp.get("market", {}).get("yes_ask", 50)
            if side == "yes"
            else opp.get("market", {}).get("no_ask", 50)
        )
        opp_type = opp.get("type", "")
        uncertainty_discount = 0.85 if opp_type in ("weather", "series_game_edge") else 1.0
        # Session 53: vig_stack opp_types size against per-family caps. Family
        # extracted from ticker prefix (e.g. KXINX-25APR30NETLAL → "KXINX").
        # Non-vig_stack opp_types pass family=None → legacy $200 cap (no-op).
        ticker = opp.get("ticker", "")
        family = (
            ticker.split("-", 1)[0]
            if opp_type in _VIG_STACK_SIZING_TYPES and ticker
            else None
        )
        sizing = kelly_size(
            edge=opp.get("edge", 0),
            probability=win_prob,
            balance=balance,
            price_cents=price_cents,
            uncertainty_discount=uncertainty_discount,
            confidence=opp.get("confidence", 0.75),
            family=family,
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
            # Trade notifications silenced — use /status or STATUS in Telegram
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
            # GO/SKIP alerts silenced — use STATUS in Telegram

        # Edge accuracy notifications silenced

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
                            # Track crypto trade count in bot state
                            try:
                                state = _load_bot_state()
                                state.setdefault("crypto_trades_today", 0)
                                state.setdefault("crypto_pnl_session", 0.0)
                                state["crypto_trades_today"] += 1
                                _save_bot_state(state)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error("CRYPTO LOOP: error handling opp %s: %s",
                                         opp.get("ticker", "?"), e)
            except Exception as e:
                logger.error("CRYPTO LOOP: scan error: %s", e)
            await asyncio.sleep(CRYPTO_SCAN_INTERVAL)

    async def _run_watcher(self, query: str, watcher) -> None:
        """
        Run a LiveGameWatcher to completion and clean up.
        Called as an asyncio task from handle_watch().
        """
        try:
            summary = await watcher.start()
            await self.notifier.send_message(summary)
        except asyncio.CancelledError:
            watcher.stop()
        except Exception as e:
            # Don't silently swallow watcher crashes — log and alert
            logger.error(
                "LiveGameWatcher CRASHED for '%s': %s", query, e, exc_info=True,
            )
            try:
                await self.notifier.send_message(
                    f"⚠️ Watcher crashed: {query}\n{type(e).__name__}: {e}"
                )
            except Exception:
                pass
            # Allow scanner to restart this match by removing from _recently_watched
            from bot.live_watcher import _recently_watched
            event_ticker = getattr(watcher, "_match_event_ticker", None)
            if event_ticker:
                _recently_watched.discard(event_ticker)
        finally:
            self._active_watchers.pop(query, None)

    async def _dispatch_position_alerts(self, alerts):
        """Act on take_profit / cut_loss / position_move / resting_expiry alerts
        from update_positions. Vig stack positions are exempt from auto-exit
        (TP and SL paths) — see Battle Scar #9 + Session 36. Extracted from
        _main_loop so the dispatch logic is testable in isolation."""
        for a in alerts:
            alert_type = a.get("type", "position_move")
            opp_type = a.get("opp_type", "")
            if alert_type == "take_profit":
                # Vig stack: structural edge, hold to settlement.
                if opp_type in _VIG_STACK_OPP_TYPES:
                    logger.info(
                        "Take-profit SKIPPED for %s (vig_stack — structural, hold to settlement)",
                        a["ticker"],
                    )
                    continue
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
                # Vig stack: structural edge, hold to settlement.
                if opp_type in _VIG_STACK_OPP_TYPES:
                    logger.info(
                        "Cut-loss SKIPPED for %s (vig_stack — structural, hold to settlement)",
                        a["ticker"],
                    )
                    continue
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
                pass  # silenced
            else:
                pass  # position move notifications silenced

    async def _heartbeat_loop(self):
        """Touch bot.lock every 30s so wedged-loop detection (Gotcha #6) isn't
        masked by long scan_interval. Also refreshes bot_state.last_heartbeat
        so liveness checks (Telegram /STATUS, monitoring scripts) see fresh
        timestamps between scans. Session 15.5."""
        while self._running:
            try:
                LOCK_FILE.touch()
            except Exception:
                logger.debug("heartbeat touch failed", exc_info=True)
            try:
                state = _load_bot_state()
                state["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
                # Session 77: re-affirm running=True on every heartbeat. The
                # signal-handler pattern at async_main:1510-1513 spawns
                # `bot.stop()` as a parallel task on SIGTERM/SIGINT without
                # cancelling start(). When stop() runs but start()'s gather()
                # keeps going, running=False persists on disk while the bot
                # is alive. Heartbeat-driven re-affirmation makes
                # running=True the invariant "heartbeat loop is alive",
                # which IS the right semantic — when the process truly
                # terminates, the heartbeat loop exits and the last-written
                # state from stop() sticks. Self-corrects within 30s.
                state["running"] = True
                _save_bot_state(state)
            except Exception:
                logger.warning("heartbeat bot_state update failed", exc_info=True)
            await asyncio.sleep(30)

    async def _position_check_loop(self):
        """Ratchet MFE/MAE/ticks_observed on open positions every 30s,
        independent of scan_interval. Session 17 (Apr 26).

        scan_interval can sit at IDLE (1800s) for hours when scanner.py's
        odds-API games list doesn't include the Kalshi-native sports
        live_watcher actually bets on (UFC, IPL, individual matches). That
        meant 90% of live_momentum positions exited (status="exited") before
        update_positions ever fired once on them — leaving ticks_observed=None
        and Session 9's MFE/MAE instrumentation useless for the only
        profitable strategy.

        This loop fires every 30s on open positions only (cheap; iterates
        positions.json once per call). Alerts are intentionally discarded —
        take_profit / cut_loss flow stays driven by the existing _main_loop
        call site (line ~1175) so we don't double-fire exits. update_positions
        is idempotent (mfe/mae use `>` comparisons; ticks++ is per-call), so
        running it twice is just two ratchets.
        """
        loop = asyncio.get_event_loop()
        await asyncio.sleep(20)  # let the bot fully initialize first
        while self._running:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: update_positions(called_from="_position_check_loop"),
                )
            except Exception:
                logger.warning("position check loop error", exc_info=True)
            await asyncio.sleep(30)

    async def _live_scan_loop(self):
        """
        Periodically scan Kalshi for live 1v1 matches (tennis, UFC, etc.)
        and auto-start momentum watchers for matches with clear leaders.
        """
        from bot.live_watcher import scan_live_matches, LIVE_SCAN_INTERVAL

        await asyncio.sleep(15)  # let the bot fully initialize first

        while self._running:
            try:
                balance = self._get_paper_balance() if PAPER_MODE else 0.0
                new_watchers = await scan_live_matches(
                    self.notifier, self._active_watchers, balance=balance,
                )
                for query_key, watcher in new_watchers:
                    task = asyncio.create_task(self._run_watcher(query_key, watcher))
                    self._active_watchers[query_key] = (watcher, task)
                    logger.info("Auto-started momentum watcher: %s", query_key)

                if new_watchers:
                    await self.notifier.send_message(
                        f"Auto-scan: started {len(new_watchers)} new live watcher(s)\n"
                        + "\n".join(f"  • {q}" for q, _ in new_watchers)
                    )
            except Exception as e:
                logger.error("LIVE SCAN LOOP error: %s", e)

            await asyncio.sleep(LIVE_SCAN_INTERVAL)

    def _get_paper_balance(self) -> float:
        """Compute current paper balance from paper_trades.json."""
        import json, pathlib
        from bot.config import PAPER_STARTING_BALANCE, PAPER_TRADES_FILE
        pt_file = pathlib.Path(PAPER_TRADES_FILE)
        if not pt_file.exists():
            return PAPER_STARTING_BALANCE
        try:
            trades = json.loads(pt_file.read_text())
            balance = PAPER_STARTING_BALANCE
            for t in trades:
                if not isinstance(t, dict):
                    continue
                entry_cost = t.get("contracts", 0) * t.get("entry_price", 0.0)
                status = t.get("status", "open")
                if status in ("open", "won", "lost", "exited_early"):
                    balance -= entry_cost
                if status == "won":
                    balance += t.get("contracts", 0) * 1.0
                elif status == "exited_early":
                    balance += t.get("contracts", 0) * t.get("exit_price", 0.0)
            return round(balance, 2)
        except Exception:
            return PAPER_STARTING_BALANCE

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
                state["crypto_trades_today"] = 0
                state["telegram_throttled_count_24h"] = 0
                state["snapshot_outer_timeout_count_24h"] = 0  # Session 98
                state["current_date"] = today
            state["scans_today"] = state.get("scans_today", 0) + 1
            _save_bot_state(state)
            LOCK_FILE.touch()  # Session 5: lock mtime is now a liveness signal

            # ----------------------------------------------------------
            # Step 3: Scan for opportunities (ESPN = FREE)
            # ----------------------------------------------------------
            # Session 12: snapshot the full active Kalshi universe before
            # the scan so scanners can attribute which markets they touched
            # via on_market_seen. flush_universe runs in `finally` so a
            # scanner exception doesn't strand the buffered rows.
            scan_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            # Session 39: snapshot_universe does synchronous requests calls +
            # retry sleeps in the cursor walk. On Apr 30, flaky Kalshi caused
            # a single snapshot to block the event loop for 3+ hours, starving
            # _live_scan_loop / _heartbeat_loop / _position_check_loop /
            # Telegram polling. Run it on the default executor so the loop
            # stays responsive. Mirror the pattern at lines 923, 1082, 1408.
            #
            # Session 98 (2026-05-11): the executor wrap kept OTHER coroutines
            # responsive but did not bound _main_loop's OWN await. Phase 0.1
            # observed an 8h gap on May 10 03:08-11:07 UTC where
            # snapshot_universe vastly overshot its internal 300s deadline.
            # Wrap the await in asyncio.wait_for with a hard outer cap.
            # On timeout: log, increment counter, sleep 60s, continue.
            loop = asyncio.get_event_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda: _universe.snapshot_universe(scan_id)
                    ),
                    timeout=_SNAPSHOT_OUTER_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "snapshot_universe outer-deadline %ds exceeded (scan_id=%s) — "
                    "abandoning scan, retrying next cycle. Likely flaky Kalshi; "
                    "see Battle Scar #13 / Session 98.",
                    _SNAPSHOT_OUTER_TIMEOUT_SEC, scan_id,
                )
                try:
                    state = _load_bot_state()
                    state["snapshot_outer_timeout_count_24h"] = (
                        state.get("snapshot_outer_timeout_count_24h", 0) + 1
                    )
                    _save_bot_state(state)
                except Exception:
                    logger.exception("snapshot_outer_timeout counter update failed")
                await asyncio.sleep(60)
                continue
            # Session 13a: pull the snapshotted universe out as a list of
            # Market dataclasses so scan_cycle can pass it to the
            # Strategy contract without strategies re-fetching from
            # Kalshi.
            buffered_universe = _universe.get_buffered_markets(scan_id)
            scan_failed = False
            try:
                # Session 148: wrap scan_cycle in run_in_executor (Battle
                # Scar #13). scan_cycle is synchronous and invokes
                # scanner_weather + kalshi_series + scanner_sports_arb, each
                # of which makes sync Kalshi HTTP calls. Without this wrap,
                # those calls block the event loop and starve
                # _heartbeat_loop / _live_scan_loop / _position_check_loop
                # — the May 2026 wedge symptom. vig_stack reads
                # `buffered_universe` (snapshotted above) and makes no new
                # HTTP, so the wrap moves only weather/series/sports-arb
                # HTTP off the event loop without perturbing the +$1064
                # vig_stack hot path. Mirrors the S39 + S98 pattern.
                #
                # Session 150: add outer asyncio.wait_for. S148's wrap left
                # the await unbounded; the 2026-05-17 wedge ran 5h+ in the
                # WEATHER branch. 900s outer guard aborts within one IDLE
                # scan cycle. On TimeoutError: increment counter, set
                # scan_failed=True, fall through to the existing finally +
                # 60s recovery path.
                loop = asyncio.get_event_loop()
                scan_result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: scan_cycle(
                            scan_id=scan_id,
                            on_market_seen=_universe.on_market_seen,
                            universe=buffered_universe,
                        ),
                    ),
                    timeout=_EXECUTOR_OUTER_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "scan_cycle wedged > %ds (scan_id=%s) — aborting; will "
                    "retry next loop iteration. Executor thread leaks (no "
                    "cancellation); acceptable for transient wedges. If "
                    "scan_cycle_outer_timeout_count_24h >= 3/day, "
                    "investigate scan_cycle internals (WEATHER branch most "
                    "likely per S148 forensics).",
                    _EXECUTOR_OUTER_TIMEOUT_SEC, scan_id,
                )
                try:
                    state = _load_bot_state()
                    state["scan_cycle_outer_timeout_count_24h"] = (
                        state.get("scan_cycle_outer_timeout_count_24h", 0) + 1
                    )
                    _save_bot_state(state)
                except Exception:
                    logger.exception(
                        "scan_cycle_outer_timeout counter update failed"
                    )
                scan_failed = True
            except Exception as e:
                logger.error(f"Scan cycle error: {e}", exc_info=True)
                scan_failed = True
            finally:
                _universe.flush_universe(scan_id)
            if scan_failed:
                await asyncio.sleep(60)
                continue

            scan_interval = scan_result.get("scan_interval", 1800)
            opportunities = scan_result.get("opportunities", [])

            # ----------------------------------------------------------
            # Step 3b: Update daily performance log
            # ----------------------------------------------------------
            try:
                update_daily_log()
            except Exception as e:
                logger.debug("Daily log update error: %s", e)

            # ----------------------------------------------------------
            # Step 4: Check for resolved positions + update patterns
            # ----------------------------------------------------------
            try:
                resolved = resolve_trades()
                # Resolution notifications silenced — check via STATUS
                pass
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
                # Session 148: wrap the sync get_market loop inside
                # check_clv_settlements (clv.py:504) in run_in_executor
                # (Battle Scar #13). Mirrors the S39 pattern.
                #
                # Session 150: add outer asyncio.wait_for. On TimeoutError,
                # increment counter and default newly_settled to [] so the
                # for-loop below iterates safely; the outer try/except still
                # catches other failures.
                loop = asyncio.get_event_loop()
                try:
                    newly_settled = await asyncio.wait_for(
                        loop.run_in_executor(None, check_clv_settlements),
                        timeout=_EXECUTOR_OUTER_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "check_clv_settlements wedged > %ds — aborting; "
                        "will retry next loop iteration. If "
                        "check_clv_settlements_outer_timeout_count_24h >= "
                        "3/day, investigate CLV settlement query path "
                        "(likely Kalshi resolution endpoint).",
                        _EXECUTOR_OUTER_TIMEOUT_SEC,
                    )
                    try:
                        state = _load_bot_state()
                        state["check_clv_settlements_outer_timeout_count_24h"] = (
                            state.get(
                                "check_clv_settlements_outer_timeout_count_24h",
                                0,
                            ) + 1
                        )
                        _save_bot_state(state)
                    except Exception:
                        logger.exception(
                            "check_clv_settlements_outer_timeout counter "
                            "update failed"
                        )
                    newly_settled = []
                for clv_entry in newly_settled:
                    clv_cents = clv_entry.get("clv_cents", 0)
                    ticker = clv_entry["ticker"]
                    side = clv_entry["side"].upper()
                    result = clv_entry.get("market_result", "?")
                    paper_tag = " [PAPER]" if clv_entry.get("paper") else ""
                    emoji = "✅" if clv_cents > 0 else "❌"
                    # CLV settlement notifications silenced
                    pass
            except Exception as e:
                logger.error(f"CLV settlement check error: {e}")

            # ----------------------------------------------------------
            # Step 5: Check fills on resting orders
            # ----------------------------------------------------------
            try:
                fills = check_fills()
                # Fill notifications silenced
                pass
            except Exception as e:
                logger.error(f"Fill check error: {e}")

            # ----------------------------------------------------------
            # Step 6: Update positions — smart alerts
            # ----------------------------------------------------------
            try:
                alerts = update_positions(called_from="_main_loop")
                await self._dispatch_position_alerts(alerts)
            except Exception as e:
                logger.error(f"Position update error: {e}")

            # ----------------------------------------------------------
            # Step 6b: Position monitor — edge recheck + related markets
            # ----------------------------------------------------------
            try:
                # Session 148: wrap the sync get_market loop inside
                # recheck_open_edges (position_monitor.py:41,158,233) in
                # run_in_executor (Battle Scar #13). Mirrors the S39 pattern.
                #
                # Session 150: add outer asyncio.wait_for. On TimeoutError,
                # increment counter and default edge_alerts to [] so the
                # downstream loop iterates safely; scan_related_markets
                # below still runs in the same try block.
                loop = asyncio.get_event_loop()
                try:
                    edge_alerts = await asyncio.wait_for(
                        loop.run_in_executor(None, recheck_open_edges),
                        timeout=_EXECUTOR_OUTER_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "recheck_open_edges wedged > %ds — aborting; will "
                        "retry next loop iteration. If "
                        "recheck_open_edges_outer_timeout_count_24h >= "
                        "3/day, investigate open-edge refetch path "
                        "(likely Kalshi market endpoint or open-positions "
                        "DB query).",
                        _EXECUTOR_OUTER_TIMEOUT_SEC,
                    )
                    try:
                        state = _load_bot_state()
                        state["recheck_open_edges_outer_timeout_count_24h"] = (
                            state.get(
                                "recheck_open_edges_outer_timeout_count_24h",
                                0,
                            ) + 1
                        )
                        _save_bot_state(state)
                    except Exception:
                        logger.exception(
                            "recheck_open_edges_outer_timeout counter "
                            "update failed"
                        )
                    edge_alerts = []
                for ea in edge_alerts:
                    alert_type = ea.get("type", "")
                    ticker = ea.get("ticker", "?")
                    if alert_type == "edge_flipped":
                        entry_e = ea.get("entry_edge", 0)
                        curr_e = ea.get("current_edge", 0)
                        opp_type = ea.get("opp_type", "")
                        # Vig stack trades have structural edge (YES sum > 100¢).
                        # Individual contract price moves don't invalidate the edge —
                        # it only goes away if the entire ladder's vig collapses.
                        # Do NOT auto-exit on recalculated edge flips.
                        if opp_type in _VIG_STACK_OPP_TYPES:
                            logger.info("Edge-flip SKIPPED for %s (vig_stack — structural edge, hold to settlement)", ticker)
                            continue
                        if PAPER_MODE:
                            exit_result = exit_position(ticker, reason="edge_flipped")
                            if exit_result.get("success"):
                                pnl = exit_result.get("realized_pnl", 0)
                                await self.notifier.send_message(
                                    f"🔄 EDGE FLIPPED — auto-exited {ticker}\n"
                                    f"Entry edge: {entry_e:+.1%} → Now: {curr_e:+.1%}\n"
                                    f"P&L: ${pnl:+.2f}",
                                    priority="critical",
                                )
                            else:
                                logger.warning("Edge-flip exit failed for %s: %s",
                                               ticker, exit_result.get("reason"))
                        else:
                            await self.notifier.send_message(
                                f"⚠️ EDGE FLIPPED: {ticker}\n"
                                f"Entry edge: {entry_e:+.1%} → Now: {curr_e:+.1%}\n"
                                f"Reply SELL {ticker} to exit",
                                priority="critical",
                            )
                    elif alert_type == "edge_degraded":
                        logger.info("Edge degraded: %s (entry=%.1f%% now=%.1f%%)",
                                    ticker,
                                    ea.get("entry_edge", 0) * 100,
                                    ea.get("current_edge", ea.get("price_move", 0)) * 100)

                # Related market scan — find sibling props on same events.
                # Session 148: wrap the sync get_markets-per-event call in
                # run_in_executor (Battle Scar #13). Mirrors the S39 pattern.
                #
                # Session 150: add outer asyncio.wait_for. On TimeoutError,
                # increment counter and default related to [] so the
                # downstream loop iterates safely.
                loop = asyncio.get_event_loop()
                try:
                    related = await asyncio.wait_for(
                        loop.run_in_executor(None, scan_related_markets),
                        timeout=_EXECUTOR_OUTER_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "scan_related_markets wedged > %ds — aborting; "
                        "will retry next loop iteration. If "
                        "scan_related_markets_outer_timeout_count_24h >= "
                        "3/day, investigate related-market search path "
                        "(likely Kalshi search endpoint).",
                        _EXECUTOR_OUTER_TIMEOUT_SEC,
                    )
                    try:
                        state = _load_bot_state()
                        state["scan_related_markets_outer_timeout_count_24h"] = (
                            state.get(
                                "scan_related_markets_outer_timeout_count_24h",
                                0,
                            ) + 1
                        )
                        _save_bot_state(state)
                    except Exception:
                        logger.exception(
                            "scan_related_markets_outer_timeout counter "
                            "update failed"
                        )
                    related = []
                for rel in related:
                    _add_to_pending(rel)
                if related:
                    logger.info("RELATED: added %d sibling markets to pending", len(related))
            except Exception as e:
                logger.error(f"Position monitor error: {e}")

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
                        pass  # MM partial notifications silenced
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
                    # MM opportunity notifications silenced
                    # No blocking — scanner keeps running immediately
            except Exception as e:
                logger.error(f"MM scan error: {e}")

            # ----------------------------------------------------------
            # Step 8: Alert on opportunities — fire and forget, no blocking
            # ----------------------------------------------------------
            if PAPER_MODE:
                balance = self._get_paper_balance()
            else:
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
            # Wall-clock sleep — asyncio.sleep uses time.monotonic() which
            # does not advance during macOS system sleep. On a battery laptop
            # in DarkWake (2-180s wake windows), a single 30-min sleep never
            # accumulates a long enough contiguous awake period to fire.
            # Polling every 30s lets each DarkWake window check the wall
            # clock and resume the loop when due.
            target = datetime.now(timezone.utc) + timedelta(seconds=scan_interval)
            while True:
                remaining = (target - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(30.0, remaining))


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
