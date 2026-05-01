"""live_tick_anomalies: STREAM live_ticks.jsonl, flag tickers with repeated >=15¢ price jumps.

Catches order book disruptions, news flashes, or potential thin-market manipulation
on currently-tradeable markets. Cross-references against open positions and recent
paper trades to surface "did we get caught in this?"

Streaming-only — never loads live_ticks.jsonl (~54MB, multi-GB pre-rotation) into
memory. Uses per-ticker collections.deque(maxlen=5) rolling window. Memory bounded
by O(unique_tickers * WINDOW_TICKS) ~= a few hundred KB even on a full archive.

Note: spec called for a MIN_VOLUME filter, but live_ticks.jsonl rows don't carry
a volume field — tick log records price/bid/ask only, not order-book depth.
The MIN_JUMPS_PER_TICKER threshold below is the noise filter instead.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict, deque

from ..findings import Finding
from .outlier_pnl import _parse_ts

JUMP_THRESHOLD_CENTS = 15
WINDOW_TICKS = 5
MIN_JUMPS_PER_TICKER = 3


class LiveTickAnomalies:
    name = "live_tick_anomalies"
    data_sources = ("live_ticks_iter",)

    def run(self, ctx) -> list[Finding]:
        rolling: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=WINDOW_TICKS))
        jumps: dict[str, list[dict]] = defaultdict(list)

        for tick in ctx.live_ticks_iter():
            ticker = tick.get("ticker")
            price = tick.get("price")
            ts_str = tick.get("ts")
            if ticker is None or price is None or ts_str is None:
                continue
            window = rolling[ticker]
            if len(window) >= 2:  # need at least 2 prior ticks to compute median
                median = statistics.median(window)
                if abs(float(price) - median) >= JUMP_THRESHOLD_CENTS:
                    jumps[ticker].append({
                        "ts": ts_str,
                        "price": float(price),
                        "median": float(median),
                    })
            window.append(float(price))

        # Build position + paper-trade indices once for cross-reference
        open_position_tickers = {
            p.get("ticker") for p in (ctx.positions or [])
            if isinstance(p, dict)
            and p.get("filled", 0) > 0
            and p.get("status") in ("filled", "partial")
        }
        paper_ticker_intervals: dict[str, list[tuple[dt.datetime | None, dt.datetime | None]]] = (
            defaultdict(list)
        )
        for t in (ctx.paper_trades or []):
            tk = t.get("ticker")
            if not tk:
                continue
            entry = _parse_ts(t.get("timestamp"))
            exit_ = _parse_ts(t.get("resolved_at"))
            paper_ticker_intervals[tk].append((entry, exit_))

        findings: list[Finding] = []
        for ticker, events in jumps.items():
            if len(events) < MIN_JUMPS_PER_TICKER:
                continue
            first_ts = _parse_ts(events[0]["ts"])
            jump_date = first_ts.date().isoformat() if first_ts else "unknown"

            held_during_jump = ticker in open_position_tickers
            paper_overlap = False
            for entry, exit_ in paper_ticker_intervals.get(ticker, []):
                for ev in events:
                    ev_ts = _parse_ts(ev["ts"])
                    if ev_ts is None:
                        continue
                    if entry and entry <= ev_ts and (exit_ is None or ev_ts <= exit_):
                        paper_overlap = True
                        break
                if paper_overlap:
                    break

            evidence = {
                "ticker": ticker,
                "jump_date": jump_date,
                "jump_count": len(events),
                "max_jump_cents": round(
                    max(abs(e["price"] - e["median"]) for e in events), 2
                ),
                "held_during_jump": held_during_jump,
                "paper_trade_overlap": paper_overlap,
                "first_jump_ts": events[0]["ts"],
                "last_jump_ts": events[-1]["ts"],
                "_fingerprint_keys": ["ticker", "jump_date"],
            }
            severity = "notable" if (held_during_jump or paper_overlap) else "info"
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=(
                    f"{ticker}: {len(events)} price jumps >={JUMP_THRESHOLD_CENTS}¢ "
                    f"on {jump_date}"
                    + (" (held position)" if held_during_jump else "")
                    + (" (paper trade overlap)" if paper_overlap else "")
                ),
                summary=(
                    f"{ticker} showed {len(events)} price jumps of >="
                    f"{JUMP_THRESHOLD_CENTS}¢ vs the rolling median "
                    f"(window={WINDOW_TICKS} ticks) starting {jump_date}. "
                    f"Held during jump: {held_during_jump}. Paper trade overlap: {paper_overlap}."
                ),
                evidence=evidence,
                suggested_action=(
                    "Inspect bot/state/live_ticks.jsonl for this ticker around the jump "
                    "timestamps to determine cause (news, thin order book, Kalshi feed "
                    "glitch). If we held a position, verify exit path fired correctly."
                ),
            ))
        return findings
