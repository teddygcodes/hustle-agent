"""
Glint Trading Bot — Telegram Notifications & Command Handler

Sends edge alerts, trade confirmations, and daily summaries.
15 v1 commands with quiet mode and priority-based filtering.
Uses python-telegram-bot v21+ (async native).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from bot.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PAPER_MODE

logger = logging.getLogger("glint.notifier")


# ---------------------------------------------------------------------------
# Opportunity formatting
# ---------------------------------------------------------------------------

def format_opportunity(opp: dict) -> str:
    """Format an opportunity into a Telegram-ready message."""
    opp_type = opp.get("type", "unknown")
    ticker = opp.get("ticker", "???")
    title = opp.get("title", "Unknown market")
    edge = opp.get("edge", 0)
    relative_edge = opp.get("relative_edge", 0)
    confidence = opp.get("confidence", 0)
    side = opp.get("recommended_side", "???")
    edge_result = opp.get("edge_result", {})

    fair_value = edge_result.get("fair_value", 0)
    kalshi_price = edge_result.get("kalshi_price", 0)
    self_check = edge_result.get("self_check_passed", False)

    # Header
    type_labels = {
        "vig_stack_no":      "Sports Parlay (vig stack)",
        "vig_stack_series":  "Series Vig Stack NO",
        "series_game_edge":  "Series Game Edge",
        "btc_price_edge":    "BTC Price Edge",
        "weather":           "Weather Market",
        "live_latency_arb":  "Live Latency Arb",
    }
    type_label = type_labels.get(opp_type, opp_type)

    queue_num = opp.get("_queue_num")
    header = f"🎯 EDGE FOUND #{queue_num}" if queue_num else "🎯 EDGE FOUND"

    lines = [
        header,
        "",
        f"Market: {ticker}",
        f"Type: {type_label}",
    ]

    # Legs for parlays
    legs = opp.get("legs", [])
    if legs:
        lines.append("")
        lines.append("LEGS:")
        for leg in legs:
            prob = leg.get("probability")
            source = leg.get("source", "")
            raw = leg.get("raw", "unknown")
            if prob is not None:
                lines.append(f"  ✅ {raw}: {prob:.0%} ({source})")
            else:
                lines.append(f"  ⚠️ {raw}: unpriced")

    # Weather details
    if opp_type == "weather":
        city = opp.get("city", "?")
        forecast = opp.get("forecast_temp", "?")
        threshold = opp.get("threshold", "?")
        direction = opp.get("direction", "?")
        lines.append("")
        lines.append(f"City: {city}")
        lines.append(f"Forecast: {forecast}°F (bias-corrected)")
        lines.append(f"Threshold: {threshold}°F ({direction})")

    # Vig stack series details
    if opp_type == "vig_stack_series":
        yes_sum = opp.get("yes_sum_cents", 0)
        vig_factor = opp.get("vig_factor", 1.0)
        no_fair = opp.get("no_fair_cents", 0)
        no_ask = opp.get("no_ask_cents", 0)
        lines.append("")
        lines.append(f"Series: {opp.get('series_ticker', '?')}")
        lines.append(f"YES sum: {yes_sum}¢ ({vig_factor:.0%} of par — {yes_sum - 100}¢ vig excess)")
        lines.append(f"NO fair: {no_fair:.1f}¢ | NO ask: {no_ask}¢")
        lines.append(f"No prediction needed — pure structural edge")

    # Series game edge sports context
    if opp_type == "series_game_edge":
        canonical     = opp.get("canonical_team", "?")
        opponent      = opp.get("opponent_team", "")
        sport         = opp.get("sport", "?").upper()
        h2g           = opp.get("hours_to_game", 0)
        odds_prob     = opp.get("odds_prob", 0)
        odds_src      = opp.get("odds_source", "books")
        game_date_str = opp.get("game_date_str", "")
        l10           = opp.get("l10")
        opp_l10       = opp.get("opp_l10")
        b2b           = opp.get("b2b", False)
        opp_b2b       = opp.get("opp_b2b", False)

        matchup = f"{canonical} vs {opponent}" if opponent else canonical
        lines.append("")
        lines.append("GAME:")
        lines.append(f"  {matchup}")

        # Date + sport + hours away
        date_part = f"{game_date_str}  ({h2g:.0f}h)" if game_date_str else f"In {h2g:.0f}h"
        lines.append(f"  {sport}  |  {date_part}")

        # Sportsbook odds
        lines.append(f"  Books ({odds_src}): {odds_prob:.0%}")

        # Last 10 for each team
        l10_parts = []
        if l10:
            l10_parts.append(f"{canonical.split()[-1]} L10: {l10}")
        if opp_l10 and opponent:
            l10_parts.append(f"{opponent.split()[-1]} L10: {opp_l10}")
        if l10_parts:
            lines.append(f"  {' | '.join(l10_parts)}")

        # Back-to-back warnings
        b2b_flags = []
        if b2b:
            b2b_flags.append(f"{canonical.split()[-1]} ⚠️B2B")
        if opp_b2b and opponent:
            b2b_flags.append(f"{opponent.split()[-1]} ⚠️B2B")
        if b2b_flags:
            lines.append(f"  B2B: {', '.join(b2b_flags)}")

    # Math
    lines.append("")
    lines.append("MATH:")
    lines.append(f"  Fair value: {fair_value:.0%} | Kalshi: {kalshi_price:.0%}")
    lines.append(f"  Edge: {relative_edge:.0%} relative {'✅' if relative_edge >= 0.15 else '⚠️'}")
    lines.append(f"  Side: BUY {side.upper()}")
    lines.append(f"  Self-check: {'✅ PASSED' if self_check else '❌ FAILED'}")

    # Sizing (will be filled by main loop)
    sizing = opp.get("sizing")
    if sizing:
        lines.append("")
        lines.append("SIZING:")
        lines.append(f"  Recommended: {sizing['contracts']} contracts × {sizing['price_cents']}¢ = ${sizing['total_cost']:.2f}")
        lines.append(f"  Max payout: ${sizing['max_payout']:.2f} | Risk: ${sizing['total_cost']:.2f}")
        lines.append(f"  Kelly fraction: {sizing['kelly_fraction']:.2f}")

    # Confidence
    conf_label = "HIGH" if confidence >= 0.7 else "MEDIUM" if confidence >= 0.4 else "LOW"
    lines.append("")
    lines.append(f"📊 Confidence: {conf_label} ({confidence:.0%})")

    # Steam confirmation signal
    if opp.get("steam_confirms"):
        lines.append(f"⚡ STEAM CONFIRMS: {opp.get('steam_detail', 'sharp money agrees')}")

    # Elo signal (series game edges only)
    elo_prob    = opp.get("elo_prob")
    elo_agrees  = opp.get("elo_agrees")
    if elo_prob is not None:
        elo_icon = "✅" if elo_agrees else ("❌" if elo_agrees is False else "—")
        lines.append(f"   Elo model: {elo_prob:.0%}  {elo_icon} {'agrees' if elo_agrees else 'disagrees' if elo_agrees is False else 'n/a'}")

    # Time display — prefer game start time for game markets (Kalshi's close_time
    # on KXNBAGAME/KXMLBGAME/KXNHLGAME markets is set to end-of-season, not game-end)
    from datetime import datetime, timezone as _tz
    hours_to_game = opp.get("hours_to_game")
    if hours_to_game is not None:
        mins_left = int(hours_to_game * 60)
        if mins_left > 0:
            h, m = divmod(mins_left, 60)
            time_str = f"{h}h {m}m" if h else f"{m}m"
            lines.append(f"   Game starts in: {time_str}")
        elif mins_left > -180:
            lines.append(f"   Game in progress")
    else:
        market = opp.get("market", {})
        close_str = market.get("close_time") or market.get("expiration_time")
        if close_str:
            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                mins_left = int((close_dt - datetime.now(_tz.utc)).total_seconds() / 60)
                if mins_left > 0:
                    h, m = divmod(mins_left, 60)
                    time_str = f"{h}h {m}m" if h else f"{m}m"
                    lines.append(f"   Closes in: {time_str}")
            except Exception:
                pass

    # Injury / steam-disagrees warnings
    for w in opp.get("warnings", []):
        lines.append(f"   {w}")

    return "\n".join(lines)


def format_detail(opp: dict) -> str:
    """
    Full leg-by-leg breakdown for the DETAIL command.

    Shows every leg's team, probability, data source, and game reference
    so the user can make an informed GO/SKIP decision.
    """
    opp_type = opp.get("type", "unknown")
    ticker = opp.get("ticker", "???")
    title = opp.get("title", "Unknown market")
    edge_result = opp.get("edge_result", {})
    side = opp.get("recommended_side", "???")

    lines = [
        "📋 FULL BREAKDOWN",
        "",
        f"Market: {ticker}",
        f"Title: {title}",
    ]

    # ------------------------------------------------------------------ #
    # Parlay types: show each leg in full
    # ------------------------------------------------------------------ #
    if opp_type == "vig_stack_no":
        legs = opp.get("legs") or edge_result.get("legs", [])

        real_count = sum(
            1 for leg in legs
            if leg.get("source") and "fallback" not in (leg.get("source") or "")
        )
        fallback_count = len(legs) - real_count

        quality_icon = "✅" if fallback_count == 0 else ("⚠️" if fallback_count == 1 else "❌")
        lines.append("")
        lines.append(
            f"CONTRACT: Buy {side.upper()} — parlay pays if ALL legs win"
        )
        lines.append("")
        lines.append(
            f"LEGS ({len(legs)} total — {quality_icon} {real_count} real data, "
            f"{fallback_count} fallback):"
        )

        for i, leg in enumerate(legs, 1):
            raw = leg.get("raw", "unknown")
            leg_type = leg.get("type", "unknown")
            leg_side = leg.get("side", "yes")
            prob = leg.get("probability")
            source = leg.get("source") or "unknown"
            warnings = leg.get("warnings", [])
            team = leg.get("team") or leg.get("player") or ""
            threshold = leg.get("threshold")

            is_fallback = "fallback" in source
            source_icon = "⚠️ FALLBACK" if is_fallback else "✅"
            source_label = f"{source_icon} — {source}"

            lines.append("")
            lines.append(f"  [{i}] {raw}")
            lines.append(f"      Type: {leg_type} | Side: {leg_side.upper()}")
            if prob is not None:
                lines.append(f"      Probability: {prob:.0%}")
            else:
                lines.append("      Probability: unpriced")
            lines.append(f"      Source: {source_label}")
            if team:
                label = "Team" if leg_type in ("team_win", "spread") else "Player"
                lines.append(f"      {label}: {team}")
            if threshold is not None:
                lines.append(f"      Threshold: {threshold}")
            for w in warnings:
                lines.append(f"      ⚠️ {w}")

        # Same-game groups
        same_game = edge_result.get("same_game_groups", {}) or opp.get("edge_result", {}).get("same_game_groups", {})
        if same_game:
            lines.append("")
            lines.append("GAME REFERENCES:")
            for game_key, leg_raws in same_game.items():
                lines.append(f"  {game_key}: {', '.join(str(r) for r in leg_raws)}")

        # Data quality summary
        lines.append("")
        lines.append("DATA QUALITY:")
        pct = real_count / len(legs) if legs else 0
        lines.append(f"  Real pricing: {real_count}/{len(legs)} legs ({pct:.0%})")
        if fallback_count:
            lines.append(
                f"  ⚠️ {fallback_count} leg(s) used 50% fallback — edge may be overstated"
            )

    # ------------------------------------------------------------------ #
    # Series game edge: team, sport, book vs Elo vs Kalshi breakdown
    # ------------------------------------------------------------------ #
    elif opp_type == "series_game_edge":
        canonical   = opp.get("canonical_team", "?")
        sport       = opp.get("sport", "?").upper()
        abbrev      = opp.get("team_abbrev", "?")
        odds_prob   = opp.get("odds_prob", 0)
        odds_source = opp.get("odds_source", "?")
        elo_prob    = opp.get("elo_prob")
        elo_agrees  = opp.get("elo_agrees")
        h2g         = opp.get("hours_to_game", 0)
        kp          = edge_result.get("kalshi_price", 0)

        lines.append("")
        lines.append(f"CONTRACT: Buy {side.upper()} — {canonical} wins")
        lines.append(f"Sport: {sport}  |  Ticker abbrev: {abbrev}")
        lines.append(f"Hours to game: {h2g:.1f}h")
        lines.append("")
        lines.append("PRICING COMPARISON:")
        lines.append(f"  Books ({odds_source}):  {odds_prob:.1%}")
        if elo_prob is not None:
            elo_icon = "✅ agrees" if elo_agrees else ("❌ disagrees" if elo_agrees is False else "")
            lines.append(f"  Elo model:            {elo_prob:.1%}  {elo_icon}")
        lines.append(f"  Kalshi ask (YES):     {kp:.1%}")
        lines.append(f"  Edge:                 {edge_result.get('edge', 0):.1%} ({edge_result.get('relative_edge', 0):.0%} relative)")

        # Injury warnings
        for w in opp.get("warnings", []):
            lines.append("")
            lines.append(f"  {w}")

    # ------------------------------------------------------------------ #
    # Live latency arb: show game context and pricing comparison
    # ------------------------------------------------------------------ #
    elif opp_type == "live_latency_arb":
        game = opp.get("game", {})
        home = game.get("home_team", "?")
        away = game.get("away_team", "?")
        status = game.get("status", "?")
        espn_prob = opp.get("espn_prob", 0)
        kalshi_price = opp.get("kalshi_price", 0)
        matched_team = opp.get("matched_team", "?")

        lines.append("")
        lines.append("CONTRACT:")
        lines.append(f"  Buy {side.upper()} — {matched_team} outcome")
        lines.append("")
        lines.append("LIVE GAME:")
        lines.append(f"  {away} @ {home}")
        lines.append(f"  Status: {status.replace('STATUS_', '').replace('_', ' ')}")
        lines.append("")
        lines.append("PRICING (latency arb):")
        lines.append(f"  ESPN live consensus: {espn_prob:.0%} ({matched_team})")
        lines.append(f"  Kalshi current ask:  {kalshi_price:.0%} ({int(kalshi_price * 100)}¢)")
        lines.append(f"  Gap: {abs(espn_prob - kalshi_price):.0%} — Kalshi lags ESPN")

    # ------------------------------------------------------------------ #
    # Weather: show forecast data and distribution math
    # ------------------------------------------------------------------ #
    elif opp_type == "weather":
        city = opp.get("city", "?")
        forecast_temp = opp.get("forecast_temp", "?")
        threshold = opp.get("threshold", "?")
        direction = opp.get("direction", "?")
        er = opp.get("edge_result", {})
        corrected = er.get("corrected_temp", forecast_temp)
        p_above = er.get("p_above")
        p_below = er.get("p_below")

        lines.append("")
        lines.append("WEATHER DATA:")
        lines.append(f"  City: {city}")
        lines.append(f"  NWS forecast: {forecast_temp}°F (daytime high)")
        if corrected != forecast_temp:
            lines.append(f"  Bias-corrected: {corrected}°F (NWS warm bias removed)")
        lines.append(f"  Threshold: {threshold}°F ({direction})")
        lines.append("")
        lines.append("PROBABILITY:")
        if p_above is not None:
            lines.append(f"  P(above {threshold}°F): {p_above:.1%}")
        if p_below is not None:
            lines.append(f"  P(below {threshold}°F): {p_below:.1%}")
        kalshi_price = edge_result.get("kalshi_price", 0)
        lines.append(f"  Kalshi YES price: {int(kalshi_price * 100)}¢ ({kalshi_price:.0%})")

    # ------------------------------------------------------------------ #
    # BTC price edge: spot, threshold, vol, and probability math
    # ------------------------------------------------------------------ #
    elif opp_type == "btc_price_edge":
        spot      = opp.get("btc_spot", "?")
        threshold = opp.get("threshold", "?")
        h2e       = opp.get("hours_remaining", "?")
        fv        = edge_result.get("fair_value", 0)
        kp        = edge_result.get("kalshi_price", 0)
        rvol      = opp.get("realized_vol")

        lines.append("")
        lines.append(f"CONTRACT: Buy {side.upper()} — BTC closes {'above' if side == 'yes' else 'below'} ${threshold:,.0f}" if isinstance(threshold, (int, float)) else f"CONTRACT: Buy {side.upper()} — BTC threshold {threshold}")
        lines.append("")
        lines.append("BTC DATA:")
        lines.append(f"  Spot price:       ${spot:,.0f}" if isinstance(spot, (int, float)) else f"  Spot price:       {spot}")
        lines.append(f"  Threshold:        ${threshold:,.0f}" if isinstance(threshold, (int, float)) else f"  Threshold:        {threshold}")
        lines.append(f"  Hours remaining:  {h2e}")
        if rvol is not None:
            lines.append(f"  Realized 24h vol: {rvol:.2%} (from CoinGecko hourly)")
        lines.append("")
        lines.append("PROBABILITY:")
        lines.append(f"  Log-normal model: {fv:.1%}")
        lines.append(f"  Kalshi YES ask:   {kp:.1%}  ({int(kp * 100)}¢)")
        lines.append(f"  Edge:             {edge_result.get('edge', 0):.1%} ({edge_result.get('relative_edge', 0):.0%} relative)")

    # ------------------------------------------------------------------ #
    # Math chain (all types)
    # ------------------------------------------------------------------ #
    math_chain = edge_result.get("math_chain", [])
    if math_chain:
        lines.append("")
        lines.append("MATH CHAIN:")
        for step in math_chain:
            lines.append(f"  {step}")

    # Self-check status
    self_check = edge_result.get("self_check_passed", False)
    lines.append("")
    lines.append(f"Self-check: {'✅ PASSED' if self_check else '❌ FAILED — do not trade'}")

    return "\n".join(lines)


def format_trade_confirmation(result: dict) -> str:
    """Format a trade execution result for Telegram."""
    status = result.get("status", "unknown")
    ticker = result.get("ticker", "???")
    side = result.get("side", "???")
    count = result.get("count", 0)
    filled = result.get("filled_count", 0)
    price = result.get("price_cents", 0)
    cost = result.get("cost_dollars", 0)

    if filled == count:
        return f"✅ FILLED: {filled} contracts @ {price}¢ on {ticker} ({side.upper()}) — ${cost:.2f}"
    elif filled > 0:
        return f"⚠️ PARTIAL: {filled}/{count} filled @ {price}¢ on {ticker} — {count - filled} resting"
    else:
        return f"❌ NOT FILLED: Order on {ticker} did not fill. Status: {status}"


def format_resolution(trade: dict) -> str:
    """Format a trade resolution for Telegram."""
    ticker = trade.get("ticker", "???")
    result = trade.get("result", "???")
    pnl = trade.get("pnl", 0)
    cost = trade.get("cost", 0)
    payout = trade.get("payout", 0)

    if result == "won":
        return f"💰 WON: {ticker} — ${payout:.2f} payout on ${cost:.2f} cost (+${pnl:.2f})"
    else:
        return f"❌ LOST: {ticker} — -${cost:.2f}"


def format_daily_summary(stats: dict) -> str:
    """Format daily P&L summary for Telegram."""
    mode_tag = " [PAPER MODE]" if PAPER_MODE else ""
    lines = [
        f"📊 DAILY SUMMARY{mode_tag}",
        "",
        f"Balance: ${stats.get('balance', 0):.2f}",
        f"Today P&L: ${stats.get('today_pnl', 0):.2f}",
        f"Total P&L: ${stats.get('total_pnl', 0):.2f}",
        "",
        f"Trades today: {stats.get('trades_today', 0)}",
        f"Win rate: {stats.get('win_rate', 0):.0%}",
        f"Open positions: {stats.get('open_positions', 0)}",
        "",
    ]

    best = stats.get("best_trade")
    worst = stats.get("worst_trade")
    if best:
        lines.append(f"Best: {best.get('ticker', '?')} +${best.get('pnl', 0):.2f}")
    if worst:
        lines.append(f"Worst: {worst.get('ticker', '?')} ${worst.get('pnl', 0):.2f}")

    lines.append("")
    lines.append(f"Scans today: {stats.get('scans_today', 0)}")
    lines.append(f"Odds API: {stats.get('odds_api_used', 0)}/{stats.get('odds_api_limit', 450)} this month")

    # Append CLV report
    try:
        from bot.clv import format_clv_report
        clv_text = format_clv_report()
        lines.append("")
        lines.append(clv_text)
    except Exception:
        pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram Bot — command handler callbacks
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """
    Manages Telegram bot for sending alerts and receiving commands.

    Commands are received via text messages and dispatched to registered
    callbacks. The bot runs async alongside the main scan loop.

    Supports quiet mode: when quiet, only "critical" priority messages
    get through (take profit, cut loss, trade resolutions).
    """

    def __init__(self):
        self.app: Optional[Application] = None
        self._command_callbacks: dict[str, Callable] = {}
        self._button_callback: Optional[Callable] = None  # (action, opp_id) -> str
        self._message_ids: dict[str, int] = {}  # opp_id -> telegram message_id
        self._paused = False
        self._quiet_until: Optional[datetime] = None

    async def initialize(self):
        """Build and initialize the Telegram application."""
        if not TELEGRAM_BOT_TOKEN:
            logger.warning("No Telegram bot token configured — notifications disabled")
            return

        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Register handlers
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CallbackQueryHandler(self._handle_button))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))

        await self.app.initialize()
        logger.info("Telegram bot initialized")

    async def start_polling(self):
        """Start polling for Telegram updates."""
        if not self.app:
            return
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram polling started")

    async def stop(self):
        """Stop the Telegram bot."""
        if not self.app:
            return
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        logger.info("Telegram bot stopped")

    def register_callback(self, command: str, callback: Callable):
        """Register a callback for a text command (STATUS, LIST, etc.)."""
        self._command_callbacks[command.upper()] = callback

    def register_button_callback(self, callback: Callable):
        """Register handler for GO/SKIP button presses. callback(action, opp_id) -> str."""
        self._button_callback = callback

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def pending_opportunity(self) -> Optional[dict]:
        return self._pending_opportunity

    @pending_opportunity.setter
    def pending_opportunity(self, value: Optional[dict]):
        self._pending_opportunity = value

    # -- Quiet mode --

    def set_quiet(self, hours: float):
        """Suppress non-critical alerts for N hours."""
        self._quiet_until = datetime.now(timezone.utc) + timedelta(hours=hours)

    def set_loud(self):
        """Cancel quiet mode."""
        self._quiet_until = None

    @property
    def is_quiet(self) -> bool:
        if self._quiet_until is None:
            return False
        if datetime.now(timezone.utc) >= self._quiet_until:
            self._quiet_until = None
            return False
        return True

    # -- Sending messages --

    async def send_message(self, text: str, priority: str = "normal"):
        """
        Send a message to the configured chat.

        Args:
            text: Message text
            priority: "normal" (suppressed in quiet mode) or "critical" (always sent)
        """
        # Quiet mode: suppress normal-priority messages
        if priority == "normal" and self.is_quiet:
            logger.debug(f"[QUIET] Suppressed: {text[:60]}...")
            return

        if not self.app or not TELEGRAM_CHAT_ID:
            logger.info(f"[DRY] Would send: {text[:100]}...")
            return
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=None,  # Plain text for reliability
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    async def send_photo(self, photo_path):
        """Send a photo (e.g. equity chart) to the configured chat."""
        if not self.app or not TELEGRAM_CHAT_ID:
            logger.info(f"[DRY] Would send photo: {photo_path}")
            return
        try:
            with open(photo_path, "rb") as f:
                await self.app.bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=f)
        except Exception as e:
            logger.error(f"Failed to send photo: {e}")

    async def send_alert(self, opportunity: dict):
        """Send an edge alert with inline GO/SKIP buttons."""
        text = format_opportunity(opportunity)
        if PAPER_MODE:
            text = "[PAPER] " + text

        opp_id = opportunity.get("_opp_id", "")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ GO", callback_data=f"go:{opp_id}"),
            InlineKeyboardButton("❌ SKIP", callback_data=f"skip:{opp_id}"),
        ]])

        if not self.app or not TELEGRAM_CHAT_ID:
            logger.info(f"[DRY] Would send alert: {text[:80]}...")
            return

        try:
            msg = await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                reply_markup=keyboard,
            )
            if opp_id:
                self._message_ids[opp_id] = msg.message_id
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    async def send_confirmation(self, trade_result: dict):
        """Send a trade execution confirmation."""
        text = format_trade_confirmation(trade_result)
        await self.send_message(text)

    async def send_resolution(self, trade: dict):
        """Send a trade resolution notification (always critical)."""
        text = format_resolution(trade)
        await self.send_message(text, priority="critical")

    async def send_daily_summary(self, stats: dict):
        """Send the daily P&L summary."""
        text = format_daily_summary(stats)
        await self.send_message(text)

    # -- Command handlers --

    async def _handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses (GO / SKIP)."""
        query = update.callback_query
        await query.answer()  # dismiss the loading spinner immediately

        data = query.data or ""
        if ":" not in data:
            await query.edit_message_text("Unknown button action.")
            return

        action, opp_id = data.split(":", 1)

        if self._button_callback:
            try:
                result_text = self._button_callback(action, opp_id)
            except Exception as e:
                result_text = f"Error: {e}"
        else:
            result_text = "No handler registered."

        # Edit the original alert message in-place — clean, no clutter
        try:
            await query.edit_message_text(result_text)
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            await self.send_message(result_text)

        # Clean up stored message_id
        self._message_ids.pop(opp_id, None)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "✨ Glint Trading Bot active.\n\n"
            "Alerts arrive with ✅ GO and ❌ SKIP buttons — tap to act.\n\n"
            "Queue: LIST, PENDING\n"
            "Info: STATUS, LIVE, EDGES, BALANCE\n"
            "Manage: SELL [ticker], EXITALL, TRAIL [ticker] [%]\n"
            "Stats: HISTORY, WINRATE, ROI, CLV\n"
            "Config: MODE, SCAN\n"
            "Control: PAUSE, RESUME, QUIET [hrs], LOUD"
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cb = self._command_callbacks.get("STATUS")
        if cb:
            result = await cb() if asyncio.iscoroutinefunction(cb) else cb()
            await update.message.reply_text(str(result))
        else:
            await update.message.reply_text("Status callback not registered")

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._paused = True
        await update.message.reply_text("⏸️ Scanning paused. Send /resume to continue.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._paused = False
        await update.message.reply_text("▶️ Scanning resumed.")

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages as commands with optional arguments."""
        raw = update.message.text.strip()
        parts = raw.split(maxsplit=1)
        command = parts[0].upper()
        args = parts[1] if len(parts) > 1 else ""

        # Typo / shorthand aliases
        _ALIASES = {
            "DETAILS": "DETAIL",
            "STAT": "STATUS",
            "STATS": "STATUS",
            "HIST": "HISTORY",
            "POS": "POSITIONS",
            "BAL": "BALANCE",
        }
        command = _ALIASES.get(command, command)

        # Built-in commands
        if command == "PAUSE":
            self._paused = True
            await update.message.reply_text("⏸️ Scanning paused.")
            return

        if command == "RESUME":
            self._paused = False
            await update.message.reply_text("▶️ Scanning resumed.")
            return

        if command == "QUIET":
            hours = 2.0  # default
            if args:
                try:
                    hours = float(args)
                except ValueError:
                    pass
            self.set_quiet(hours)
            await update.message.reply_text(f"🔇 Quiet for {hours:.0f} hours. Critical alerts still get through.")
            return

        if command == "LOUD":
            self.set_loud()
            await update.message.reply_text("🔊 All alerts enabled.")
            return

        # Registered callbacks (GO, SKIP, DETAIL, STATUS, LIVE, etc.)
        cb = self._command_callbacks.get(command)
        if cb:
            try:
                # Pass args to callback if it accepts them
                import inspect
                sig = inspect.signature(cb)
                if len(sig.parameters) > 0:
                    result = await cb(args) if asyncio.iscoroutinefunction(cb) else cb(args)
                else:
                    result = await cb() if asyncio.iscoroutinefunction(cb) else cb()
                if result:
                    # Telegram has a 4096 char limit per message
                    text = str(result)
                    if len(text) > 4000:
                        text = text[:4000] + "\n... (truncated)"
                    await update.message.reply_text(text)
            except Exception as e:
                await update.message.reply_text(f"Error: {e}")
        else:
            available = sorted(self._command_callbacks.keys())
            cmd_list = ", ".join(available)
            await update.message.reply_text(
                f"Unknown: {command}\n\n"
                f"Commands: {cmd_list}\n"
                f"Also: PAUSE, RESUME, QUIET [hours], LOUD"
            )
