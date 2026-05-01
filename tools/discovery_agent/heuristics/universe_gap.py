"""universe_gap: flag (sport, market_type) pairs decisions touched but today's universe lacks.

If decisions.jsonl shows the bot acted on KXMLBGAME-* markets ≥5 times in the last
7d but today's universe.jsonl snapshot has no rows with that prefix, the scanner
or universe writer regressed (or Kalshi delisted the product). Decision-side is
the historical record; universe-side is current-day reality. Mismatch = signal.

Reframed from spec's universe_archive/ approach because that directory does not
exist on disk. Decisions-as-history gives a richer signal anyway: proves we
acted on the market, not just saw it.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from bot.regime import SPORT_PREFIXES

from .. import _sport_classifier
from ..findings import Finding
from .outlier_pnl import _parse_ts

LOOKBACK_DAYS = 7
MIN_DECISION_COUNT = 5

_SORTED_SPORT_PREFIX_KEYS = sorted(SPORT_PREFIXES.keys(), key=len, reverse=True)


def _market_type_from_ticker(ticker: str) -> str | None:
    """Return the longest SPORT_PREFIXES key that matches the ticker prefix.
    e.g. KXMLBGAME-26APR29-X → 'KXMLBGAME'; KXATPMATCH-X → 'KXATPMATCH'."""
    if not ticker:
        return None
    for prefix in _SORTED_SPORT_PREFIX_KEYS:
        if ticker.startswith(prefix):
            return prefix
    return None


class UniverseGap:
    name = "universe_gap"
    data_sources = ("decisions", "universe_iter")

    def run(self, ctx) -> list[Finding]:
        cutoff = ctx.loaded_at - dt.timedelta(days=LOOKBACK_DAYS)

        # decisions side: count (sport, market_type) pairs we acted on
        decisions_pairs: dict[tuple[str | None, str | None], int] = defaultdict(int)
        for d in ctx.decisions:
            ts = _parse_ts(d.get("ts"))
            if ts is None or ts < cutoff:
                continue
            ticker = d.get("ticker", "")
            sport = _sport_classifier.sport_from_ticker_distinguished(ticker)
            mt = _market_type_from_ticker(ticker)
            if sport is None or mt is None:
                continue
            decisions_pairs[(sport, mt)] += 1

        if not decisions_pairs:
            return []

        # find the most recent scan_id in universe.jsonl (append-only — last seen wins)
        latest_scan_id: str | None = None
        latest_ts: dt.datetime | None = None
        for row in ctx.universe_iter():
            sid = row.get("scan_id")
            ts = _parse_ts(row.get("ts"))
            if sid is None or ts is None:
                continue
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest_scan_id = sid

        if latest_scan_id is None:
            return []

        # universe side: collect (sport, market_type) pairs in the latest snapshot
        universe_pairs: set[tuple[str | None, str | None]] = set()
        for row in ctx.universe_iter():
            if row.get("scan_id") != latest_scan_id:
                continue
            ticker = row.get("ticker", "")
            sport = _sport_classifier.sport_from_ticker_distinguished(ticker)
            mt = _market_type_from_ticker(ticker)
            if sport is None or mt is None:
                continue
            universe_pairs.add((sport, mt))

        findings: list[Finding] = []
        for (sport, mt), count in decisions_pairs.items():
            if count < MIN_DECISION_COUNT:
                continue
            if (sport, mt) in universe_pairs:
                continue
            evidence = {
                "sport": sport,
                "market_type": mt,
                "recent_decision_count": count,
                "latest_universe_scan_id": latest_scan_id,
                "lookback_days": LOOKBACK_DAYS,
                "_fingerprint_keys": ["sport", "market_type"],
            }
            findings.append(Finding(
                heuristic=self.name,
                severity="notable",
                title=(
                    f"{mt}/{sport}: scanner-decided {count} times in last "
                    f"{LOOKBACK_DAYS}d but absent from latest universe snapshot"
                ),
                summary=(
                    f"Decisions log shows {count} records on '{mt}' markets ({sport}) "
                    f"in the last {LOOKBACK_DAYS}d, but today's universe snapshot "
                    f"({latest_scan_id}) contains zero rows for that prefix. Either "
                    f"Kalshi delisted the product or the universe writer / scanner "
                    f"regressed for this ticker family."
                ),
                evidence=evidence,
                suggested_action=(
                    f"Manually fetch '{mt}' on Kalshi to confirm whether markets exist. "
                    f"If yes, audit bot/universe.py active-series list and the relevant "
                    f"scanner module (bot/scanner.py / bot/scanner_sports.py / etc.) "
                    f"for the missing prefix attribution."
                ),
            ))
        return findings
