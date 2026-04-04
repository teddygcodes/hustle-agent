"""
Outcome Tracker — paper alert persistence and calibration.

Stores every paper alert to SQLite. Each scan cycle, checks resolved markets
and records wins/losses. After 50+ samples per strategy, flags underperformers.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("nexus.outcome_tracker")

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "state", "outcomes.db")

_CALIBRATION_SHORTFALL_THRESHOLD = 0.75
_MIN_SAMPLES_FOR_CALIBRATION = 50


class OutcomeTracker:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_alerts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker      TEXT NOT NULL,
                    strategy    TEXT NOT NULL,
                    edge        REAL,
                    relative_edge REAL,
                    confidence  REAL,
                    side        TEXT,
                    kalshi_price REAL,
                    close_time  TEXT,
                    stored_at   TEXT NOT NULL,
                    resolved    INTEGER DEFAULT 0,
                    result      TEXT,
                    won         INTEGER
                )
            """)
            conn.commit()

    def store_alert(self, opp: dict) -> int:
        market = opp.get("market", {})
        close_time = market.get("close_time") or market.get("expiration_time", "")
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO paper_alerts
                   (ticker, strategy, edge, relative_edge, confidence, side,
                    kalshi_price, close_time, stored_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    opp.get("ticker", ""),
                    opp.get("type", "unknown"),
                    opp.get("edge", 0),
                    opp.get("relative_edge", 0),
                    opp.get("confidence", 0),
                    opp.get("recommended_side", "yes"),
                    opp.get("kalshi_price", 0),
                    close_time,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def record_resolution(self, alert_id: int, result: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT side FROM paper_alerts WHERE id=?", (alert_id,)
            ).fetchone()
            if not row:
                return
            side = row[0]
            won = 1 if (side == "yes" and result == "yes") or (side == "no" and result == "no") else 0
            conn.execute(
                """UPDATE paper_alerts
                   SET resolved=1, result=?, won=?
                   WHERE id=?""",
                (result, won, alert_id),
            )
            conn.commit()

    def get_stats(self, strategy: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*), SUM(won) FROM paper_alerts
                   WHERE strategy=? AND resolved=1""",
                (strategy,),
            ).fetchone()
        total = row[0] or 0
        wins = int(row[1] or 0)
        return {
            "strategy": strategy,
            "total": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": wins / total if total > 0 else None,
        }

    def get_pending_resolution(self) -> list:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, ticker, close_time FROM paper_alerts
                   WHERE resolved=0 AND close_time != '' AND close_time < ?""",
                (now,),
            ).fetchall()
        return [{"id": r[0], "ticker": r[1], "close_time": r[2]} for r in rows]

    def get_calibration_report(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT strategy, COUNT(*), SUM(won), AVG(kalshi_price), AVG(edge)
                   FROM paper_alerts WHERE resolved=1
                   GROUP BY strategy"""
            ).fetchall()
        report = {}
        for strategy, total, wins, avg_price, avg_edge in rows:
            wins = int(wins or 0)
            win_rate = wins / total if total > 0 else None
            expected_win_rate = (avg_price or 0) + (avg_edge or 0)
            flagged = (
                total >= _MIN_SAMPLES_FOR_CALIBRATION
                and win_rate is not None
                and win_rate < expected_win_rate * _CALIBRATION_SHORTFALL_THRESHOLD
            )
            report[strategy] = {
                "total": total,
                "wins": wins,
                "win_rate": round(win_rate, 3) if win_rate is not None else None,
                "expected_win_rate": round(expected_win_rate, 3),
                "flagged": flagged,
            }
        return report

    def check_and_resolve(self) -> int:
        try:
            from agent.kalshi_client import _kalshi_get
        except ImportError:
            return 0
        pending = self.get_pending_resolution()
        resolved_count = 0
        for alert in pending:
            try:
                data = _kalshi_get(f"/markets/{alert['ticker']}")
                market = data.get("market", data)
                result = market.get("result")
                if result in ("yes", "no"):
                    self.record_resolution(alert["id"], result)
                    resolved_count += 1
            except Exception as e:
                logger.warning("Resolution failed for %s: %s", alert['ticker'], e)
        return resolved_count

    def print_calibration_summary(self):
        report = self.get_calibration_report()
        if not report:
            return
        print("\n  [CALIBRATION] Strategy performance:")
        for strategy, stats in sorted(report.items()):
            flag = " ⚠️ UNDERPERFORMING" if stats["flagged"] else ""
            if stats["total"] < 10:
                continue
            wr = f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "n/a"
            exp = f"{stats['expected_win_rate']:.0%}"
            print(
                f"    {strategy}: {stats['wins']}/{stats['total']} "
                f"({wr} actual vs {exp} expected){flag}"
            )
