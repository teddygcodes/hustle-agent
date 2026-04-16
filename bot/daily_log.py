"""
Daily Performance Log — tracks P&L, win rate, and trade stats per day.

Creates a new day entry at midnight EST. Reads from paper_trades.json
and writes to state/daily_log.json.

Called by the bot's main loop once per scan cycle.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("glint.daily_log")

BOT_DIR = Path(__file__).resolve().parent
DAILY_LOG_FILE = BOT_DIR / "state" / "daily_log.json"
PAPER_TRADES_FILE = BOT_DIR / "state" / "paper_trades.json"

# Eastern Time offset (UTC-4 during EDT, UTC-5 during EST)
# For simplicity, use UTC-4 (EDT) during baseball season (Apr-Oct)
EST_OFFSET = timedelta(hours=-4)


def _load_json(path: Path) -> list | dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return []


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _get_est_date_str(utc_dt: datetime = None) -> str:
    """Get current date string in EST/EDT (YYYY-MM-DD)."""
    if utc_dt is None:
        utc_dt = datetime.now(timezone.utc)
    est_dt = utc_dt + EST_OFFSET
    return est_dt.strftime("%Y-%m-%d")


def update_daily_log():
    """
    Rebuild today's daily log entry from paper_trades.json.
    Called every scan cycle. Idempotent — recalculates from source data.
    """
    try:
        trades = _load_json(PAPER_TRADES_FILE)
        if not isinstance(trades, list):
            return

        daily_log = _load_json(DAILY_LOG_FILE)
        if not isinstance(daily_log, list):
            daily_log = []

        today_str = _get_est_date_str()

        # Group all closed trades by their EST date
        date_buckets = {}
        for t in trades:
            if not isinstance(t, dict):
                continue
            pnl = t.get("pnl")
            if pnl is None:
                continue  # still open

            # Skip crypto trades
            ticker = t.get("ticker", "")
            if any(x in ticker for x in ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]):
                continue

            # Get the resolved timestamp
            resolved = t.get("resolved_at")
            if resolved:
                try:
                    from dateutil.parser import parse
                    dt = parse(resolved)
                except Exception:
                    dt = datetime.now(timezone.utc)
            else:
                dt = datetime.now(timezone.utc)

            date_str = _get_est_date_str(dt)
            if date_str not in date_buckets:
                date_buckets[date_str] = []
            date_buckets[date_str].append(t)

        # Rebuild daily log entries for any dates that have trades
        existing_dates = {d.get("date"): i for i, d in enumerate(daily_log) if isinstance(d, dict)}

        for date_str, day_trades in sorted(date_buckets.items()):
            wins = [t for t in day_trades if t.get("pnl", 0) > 0]
            losses = [t for t in day_trades if t.get("pnl", 0) < 0]
            breakeven = [t for t in day_trades if t.get("pnl", 0) == 0]

            total_pnl = sum(t.get("pnl", 0) for t in day_trades)
            total_trades = len(day_trades)
            win_count = len(wins)
            loss_count = len(losses)
            win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0

            avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
            avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
            biggest_win = max((t["pnl"] for t in wins), default=0)
            biggest_loss = min((t["pnl"] for t in losses), default=0)

            # Sport breakdown
            sport_stats = {}
            for t in day_trades:
                ticker = t.get("ticker", "")
                if "NBA" in ticker.upper():
                    sport = "nba"
                elif "NHL" in ticker.upper():
                    sport = "nhl"
                elif "MLB" in ticker.upper():
                    sport = "mlb"
                elif "ATP" in ticker.upper() or "WTA" in ticker.upper():
                    sport = "tennis"
                elif "UFC" in ticker.upper():
                    sport = "ufc"
                else:
                    sport = "other"

                if sport not in sport_stats:
                    sport_stats[sport] = {"trades": 0, "wins": 0, "pnl": 0}
                sport_stats[sport]["trades"] += 1
                if t.get("pnl", 0) > 0:
                    sport_stats[sport]["wins"] += 1
                sport_stats[sport]["pnl"] = round(sport_stats[sport]["pnl"] + t.get("pnl", 0), 2)

            entry = {
                "date": date_str,
                "trades": total_trades,
                "wins": win_count,
                "losses": loss_count,
                "win_rate": round(win_rate, 1),
                "pnl": round(total_pnl, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "biggest_win": round(biggest_win, 2),
                "biggest_loss": round(biggest_loss, 2),
                "sports": sport_stats,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            if date_str in existing_dates:
                daily_log[existing_dates[date_str]] = entry
            else:
                daily_log.append(entry)
                existing_dates[date_str] = len(daily_log) - 1

        # Sort by date
        daily_log.sort(key=lambda d: d.get("date", "") if isinstance(d, dict) else "")

        _save_json(DAILY_LOG_FILE, daily_log)

        # Log today's stats
        today_entry = next((d for d in daily_log if isinstance(d, dict) and d.get("date") == today_str), None)
        if today_entry and today_entry.get("trades", 0) > 0:
            logger.info(
                "DAILY LOG [%s]: %d trades, %dW/%dL (%.0f%%), P&L=$%.2f",
                today_str,
                today_entry["trades"],
                today_entry["wins"],
                today_entry["losses"],
                today_entry["win_rate"],
                today_entry["pnl"],
            )

    except Exception as e:
        logger.warning("Daily log update failed: %s", e)


def get_daily_summary() -> str:
    """Return a formatted summary of recent daily performance."""
    daily_log = _load_json(DAILY_LOG_FILE)
    if not isinstance(daily_log, list) or not daily_log:
        return "No daily log data yet."

    lines = ["📊 Daily Performance Log", "=" * 45]

    cumulative_pnl = 0
    for day in daily_log[-7:]:  # Last 7 days
        if not isinstance(day, dict):
            continue
        date = day.get("date", "?")
        trades = day.get("trades", 0)
        wins = day.get("wins", 0)
        losses = day.get("losses", 0)
        wr = day.get("win_rate", 0)
        pnl = day.get("pnl", 0)
        cumulative_pnl += pnl

        icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        lines.append(
            f"{icon} {date}: {trades}t {wins}W/{losses}L ({wr:.0f}%) "
            f"P&L=${pnl:+.2f}"
        )

        # Sport breakdown
        sports = day.get("sports", {})
        for sport, stats in sorted(sports.items()):
            st = stats.get("trades", 0)
            sw = stats.get("wins", 0)
            sp = stats.get("pnl", 0)
            lines.append(f"    {sport}: {st}t {sw}W ${sp:+.2f}")

    lines.append("-" * 45)
    lines.append(f"Cumulative: ${cumulative_pnl:+.2f}")

    return "\n".join(lines)
