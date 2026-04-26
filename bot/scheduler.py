"""
Glint Trading Bot — Scheduled Events

Morning briefing (8am ET) and nightly summary (midnight ET).
Uses zoneinfo for timezone handling. Called each main loop iteration.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.config import (
    BOT_STATE_FILE, MORNING_BRIEFING_HOUR, NIGHTLY_SUMMARY_HOUR,
)

RECONCILE_BALANCE_HOUR = 21  # 9pm ET — after US markets close
MORNING_BRIEFING_CUTOFF_HOUR = 20  # Don't fire "morning" briefing after 8pm ET

logger = logging.getLogger("glint.scheduler")

ET = ZoneInfo("America/New_York")


def _load_bot_state() -> dict:
    if BOT_STATE_FILE.exists():
        try:
            return json.loads(BOT_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_bot_state(state: dict):
    BOT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOT_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.rename(BOT_STATE_FILE)


async def check_scheduled_events(bot) -> None:
    """
    Check if any scheduled events should fire. Called every loop iteration.

    Args:
        bot: The GlintBot instance (has .notifier, .tracker references)
    """
    now_et = datetime.now(ET)
    today_str = now_et.strftime("%Y-%m-%d")
    yesterday_str = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
    current_hour = now_et.hour

    state = _load_bot_state()

    # --- Morning briefing (>= 8am ET, same-day fire-once, cutoff 8pm) ---
    # Gate was `==` before Session 4 — missed any hour the bot wasn't polling
    # at that minute. `>=` with a cutoff lets a restarted bot still send a
    # useful briefing, without firing "morning" at 11pm.
    if (
        MORNING_BRIEFING_HOUR <= current_hour < MORNING_BRIEFING_CUTOFF_HOUR
        and state.get("last_morning_briefing") != today_str
    ):
        logger.info("Sending morning briefing...")
        try:
            await _send_morning_briefing(bot)
            state["last_morning_briefing"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Morning briefing failed")

    # --- Nightly summary (midnight ET, with catch-up) ---
    # Normal fire: hour 0 and not yet fired today.
    # Catch-up: any hour if last fire is older than yesterday (missed a day).
    last_nightly = state.get("last_nightly_summary", "")
    should_fire_nightly = (
        (current_hour == NIGHTLY_SUMMARY_HOUR and last_nightly != today_str)
        or (last_nightly and last_nightly < yesterday_str)
    )
    if should_fire_nightly:
        logger.info("Sending nightly summary...")
        try:
            await _send_nightly_summary(bot)
            # Re-read: _send_nightly_summary persists total_pnl/today_pnl.
            # Reload so we don't clobber it with the pre-briefing `state` copy.
            state = _load_bot_state()
            state["last_nightly_summary"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Nightly summary failed")

    # --- Daily balance reconciliation (9pm ET) ---
    if (
        current_hour == RECONCILE_BALANCE_HOUR
        and state.get("last_balance_reconcile_date") != today_str
    ):
        logger.info("Running daily balance reconciliation...")
        try:
            await _reconcile_daily_balance(bot)
            # Reload: _reconcile_daily_balance writes last_known_kalshi_balance
            # and last_balance_reconcile internally.
            state = _load_bot_state()
            state["last_balance_reconcile_date"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Balance reconciliation failed")

    # --- Nightly live_ticks rotation (midnight ET, with catch-up) ---
    # Session 5: file grew to 108MB unbounded. Rotate to gzipped daily archive.
    last_rotation = state.get("last_ticks_rotation", "")
    should_rotate = (
        (current_hour == 0 and last_rotation != today_str)
        or (last_rotation and last_rotation < yesterday_str)
    )
    if should_rotate:
        logger.info("Rotating live_ticks.jsonl...")
        try:
            _rotate_live_ticks(today_str)
            state = _load_bot_state()
            state["last_ticks_rotation"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Live ticks rotation failed")

    # --- Nightly decisions.jsonl rotation (Session 6, midnight ET, catch-up) ---
    last_dec_rotation = state.get("last_decisions_rotation", "")
    should_rotate_dec = (
        (current_hour == 0 and last_dec_rotation != today_str)
        or (last_dec_rotation and last_dec_rotation < yesterday_str)
    )
    if should_rotate_dec:
        logger.info("Rotating decisions.jsonl...")
        try:
            _rotate_decisions_log(today_str)
            state = _load_bot_state()
            state["last_decisions_rotation"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Decisions rotation failed")

    # --- Nightly predictions.jsonl rotation (Session 11, midnight ET, catch-up) ---
    last_pred_rotation = state.get("last_predictions_rotation", "")
    should_rotate_pred = (
        (current_hour == 0 and last_pred_rotation != today_str)
        or (last_pred_rotation and last_pred_rotation < yesterday_str)
    )
    if should_rotate_pred:
        logger.info("Rotating predictions.jsonl...")
        try:
            _rotate_predictions_log(today_str)
            state = _load_bot_state()
            state["last_predictions_rotation"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Predictions rotation failed")

    # --- Nightly universe.jsonl rotation (Session 12, midnight ET, catch-up) ---
    last_uni_rotation = state.get("last_universe_rotation", "")
    should_rotate_uni = (
        (current_hour == 0 and last_uni_rotation != today_str)
        or (last_uni_rotation and last_uni_rotation < yesterday_str)
    )
    if should_rotate_uni:
        logger.info("Rotating universe.jsonl...")
        try:
            _rotate_universe_log(today_str)
            state = _load_bot_state()
            state["last_universe_rotation"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Universe rotation failed")

    # --- Nightly order_microstructure.jsonl rotation (Session 15, midnight ET, catch-up) ---
    last_om_rotation = state.get("last_order_microstructure_rotation", "")
    should_rotate_om = (
        (current_hour == 0 and last_om_rotation != today_str)
        or (last_om_rotation and last_om_rotation < yesterday_str)
    )
    if should_rotate_om:
        logger.info("Rotating order_microstructure.jsonl...")
        try:
            _rotate_order_microstructure_log(today_str)
            state = _load_bot_state()
            state["last_order_microstructure_rotation"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Order microstructure rotation failed")

    # --- Nightly tracker_cadence.jsonl rotation (Session 17, midnight ET, catch-up) ---
    last_tc_rotation = state.get("last_tracker_cadence_rotation", "")
    should_rotate_tc = (
        (current_hour == 0 and last_tc_rotation != today_str)
        or (last_tc_rotation and last_tc_rotation < yesterday_str)
    )
    if should_rotate_tc:
        logger.info("Rotating tracker_cadence.jsonl...")
        try:
            _rotate_tracker_cadence_log(today_str)
            state = _load_bot_state()
            state["last_tracker_cadence_rotation"] = today_str
            _save_bot_state(state)
        except Exception:
            logger.exception("Tracker cadence rotation failed")


def _rotate_jsonl(source: Path, prefix: str, today_str: str) -> None:
    """Move source.jsonl → source.parent/archive/<prefix>-YYYY-MM-DD.jsonl.gz.

    Race-safe: writers reopen the file each append, so renaming out from
    under them is fine — the next write creates a fresh file at the
    original path. Skip if file < 1KB.
    """
    if not source.exists() or source.stat().st_size < 1024:
        logger.info("%s: nothing to rotate (missing or <1KB)", prefix)
        return

    archive_dir = source.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    dest = archive_dir / f"{prefix}-{today_str}.jsonl"
    suffix = 2
    while dest.exists() or dest.with_suffix(".jsonl.gz").exists():
        dest = archive_dir / f"{prefix}-{today_str}-{suffix}.jsonl"
        suffix += 1

    original_size = source.stat().st_size
    source.rename(dest)

    gz_dest = dest.with_suffix(".jsonl.gz")
    with open(dest, "rb") as src, gzip.open(gz_dest, "wb") as gz:
        shutil.copyfileobj(src, gz)
    dest.unlink()

    gz_size = gz_dest.stat().st_size
    logger.info(
        "%s rotated: %s (%.1fMB → %.1fMB gzipped, %.0f%% saved)",
        prefix,
        gz_dest.name,
        original_size / 1_048_576,
        gz_size / 1_048_576,
        100 * (1 - gz_size / original_size) if original_size else 0,
    )


def _rotate_live_ticks(today_str: str) -> None:
    from bot.live_watcher import TICK_LOG_FILE
    _rotate_jsonl(TICK_LOG_FILE, "live_ticks", today_str)


def _rotate_decisions_log(today_str: str) -> None:
    from bot.decisions import DECISIONS_FILE
    _rotate_jsonl(DECISIONS_FILE, "decisions", today_str)


def _rotate_predictions_log(today_str: str) -> None:
    from bot.calibration import PREDICTIONS_FILE
    _rotate_jsonl(PREDICTIONS_FILE, "predictions", today_str)


def _rotate_universe_log(today_str: str) -> None:
    from bot.universe import UNIVERSE_FILE
    _rotate_jsonl(UNIVERSE_FILE, "universe", today_str)


def _rotate_order_microstructure_log(today_str: str) -> None:
    from bot.order_microstructure import MICROSTRUCTURE_FILE
    _rotate_jsonl(MICROSTRUCTURE_FILE, "order_microstructure", today_str)


def _rotate_tracker_cadence_log(today_str: str) -> None:
    from bot.tracker_cadence import CADENCE_FILE
    _rotate_jsonl(CADENCE_FILE, "tracker_cadence", today_str)


async def _send_morning_briefing(bot):
    """Compose and send the morning briefing."""
    from bot.tracker import compute_daily_summary, get_open_positions_detail, get_clv_stats
    from bot.scanner import morning_weather_scan

    summary = compute_daily_summary()
    positions = get_open_positions_detail()
    weather_opps = morning_weather_scan()

    lines = [
        "☀️ MORNING BRIEFING",
        "",
        f"Balance: ${summary['balance']:.2f}",
        f"Total P&L: ${summary['total_pnl']:.2f}",
    ]

    # Open positions
    if positions:
        lines.append(f"\nOpen positions ({len(positions)}):")
        for p in positions[:5]:
            pnl_str = f"+${p['unrealized_pnl']:.2f}" if p['unrealized_pnl'] >= 0 else f"-${abs(p['unrealized_pnl']):.2f}"
            lines.append(f"  {p['ticker'][:20]} {p['side'].upper()} — {pnl_str} ({p['pnl_percent']:.0%})")
    else:
        lines.append("\nNo open positions")

    # Weather opportunities
    if weather_opps:
        lines.append(f"\nWeather edges ({len(weather_opps)}):")
        for w in weather_opps[:3]:
            lines.append(
                f"  {w.get('city', '?')}: {w.get('relative_edge', 0):.0%} edge "
                f"({w.get('direction', '?')} {w.get('threshold', '?')}°F)"
            )

    # Crypto prices (if available)
    try:
        from bot.crypto import fetch_crypto_prices, format_crypto_summary
        prices = fetch_crypto_prices()
        if "error" not in prices:
            lines.append(f"\n{format_crypto_summary(prices)}")
    except ImportError:
        pass

    # Elo standings snapshot (top 5 each sport)
    try:
        from bot.elo import get_all_ratings
        for sport, label in (("nba", "NBA"), ("mlb", "MLB")):
            ratings = get_all_ratings(sport)
            if ratings:
                top5 = list(ratings.items())[:5]
                lines.append(f"\n{label} Elo top-5:")
                for name, elo in top5:
                    lines.append(f"  {name}: {elo:.0f}")
    except Exception:
        pass

    # CLV model health check
    clv = get_clv_stats()
    if not clv.get("no_data"):
        rate = clv["positive_rate"]
        n = clv["count"]
        health = "healthy" if rate >= 0.5 else "WEAK — fading sharp money"
        lines.append(f"\nModel CLV ({n} trades): {rate:.0%} positive — {health}")

    lines.append("\nReply SCAN to check sports edges now")

    await bot.notifier.send_message("\n".join(lines), priority="normal")


async def _send_nightly_summary(bot):
    """Compose and send the nightly summary with optional equity chart."""
    from bot.tracker import compute_daily_summary, get_streak, get_roi_by_strategy

    summary = compute_daily_summary()
    streak = get_streak()
    roi_by_type = get_roi_by_strategy()

    # Persist headline P&L numbers to bot_state so STATUS/briefings and the
    # dashboard read a live value instead of the default 0. (Session 4)
    try:
        persisted = _load_bot_state()
        persisted["total_pnl"] = summary.get("total_pnl", 0)
        persisted["today_pnl"] = summary.get("today_pnl", 0)
        _save_bot_state(persisted)
    except Exception as e:
        logger.warning(f"Nightly summary: failed to persist P&L to bot_state: {e}")

    lines = [
        "🌙 NIGHTLY SUMMARY",
        "",
        f"Balance: ${summary['balance']:.2f}",
        f"Today P&L: ${summary['today_pnl']:.2f}",
        f"Total P&L: ${summary['total_pnl']:.2f}",
        "",
        f"Trades today: {summary['trades_today']}",
        f"Resolved today: {summary['resolved_today']}",
        f"Win rate: {summary['win_rate']:.0%} ({summary['total_wins']}/{summary['total_trades']})",
    ]

    # Streak
    if streak["type"] != "none":
        emoji = "🔥" if streak["type"] == "win" else "❄️"
        lines.append(f"Streak: {emoji} {streak['count']} {streak['type']}s")

    # Best/worst
    if summary.get("best_trade"):
        lines.append(f"Best: {summary['best_trade']['ticker']} +${summary['best_trade']['pnl']:.2f}")
    if summary.get("worst_trade"):
        lines.append(f"Worst: {summary['worst_trade']['ticker']} ${summary['worst_trade']['pnl']:.2f}")

    # ROI by strategy
    if roi_by_type:
        lines.append("\nROI by strategy:")
        for stype, data in roi_by_type.items():
            lines.append(
                f"  {stype}: {data['winrate']:.0%} win ({data['total']} trades) "
                f"ROI: {data['roi']:.0%}"
            )

    lines.append(f"\nOdds API: {summary['odds_api_used']}/{summary['odds_api_limit']} this month")

    await bot.notifier.send_message("\n".join(lines), priority="normal")

    # Try to send equity chart as image
    try:
        from bot.patterns import generate_equity_curve
        from bot.tracker import _load_json
        from bot.config import TRADE_HISTORY_FILE
        history = _load_json(TRADE_HISTORY_FILE)
        if isinstance(history, list):
            chart_path = generate_equity_curve(history)
            if chart_path and chart_path.exists():
                await bot.notifier.send_photo(chart_path)
    except Exception as e:
        logger.debug(f"Equity chart not available: {e}")


async def _reconcile_daily_balance(bot) -> None:
    """
    Compare Kalshi live balance to last known local balance.
    Sends a Telegram alert if drift exceeds $0.50.
    Runs once daily at RECONCILE_BALANCE_HOUR.
    """
    from agent.kalshi_client import get_balance as _kalshi_get_balance
    result = _kalshi_get_balance()
    if "error" in result:
        logger.warning("Balance reconcile: Kalshi fetch failed — %s", result["error"])
        return

    live_balance = result.get("balance_dollars", 0.0)
    state = _load_bot_state()
    last_known = state.get("last_known_kalshi_balance")
    state["last_known_kalshi_balance"] = live_balance
    state["last_balance_reconcile"] = datetime.now(timezone.utc).isoformat()
    _save_bot_state(state)

    if last_known is not None:
        delta = abs(live_balance - last_known)
        if delta > 0.50:
            msg = (
                f"⚠️ Balance drift: Kalshi=${live_balance:.2f}, "
                f"last known=${last_known:.2f}, delta=${delta:.2f}"
            )
            logger.warning(msg)
            await bot.notifier.send_message(msg)
        else:
            logger.info("Balance reconcile: OK — Kalshi=$%.2f (delta=$%.2f)", live_balance, delta)
    else:
        logger.info("Balance reconcile: baseline set — Kalshi=$%.2f", live_balance)
