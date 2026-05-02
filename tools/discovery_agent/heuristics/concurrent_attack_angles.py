"""concurrent_attack_angles: surface multi-strategy-per-event opportunities + scanner gaps.

Session 48 (May 1 2026). Search-frontier expander, not a refinement.

Tyler's directive: 'discover new ways to bet on the same markets we are already
betting on. multiple strategies tied to the same game that if both proven to
work can fire at the same time.'

The bot bets ONE attack angle per event today (live_momentum on the winner side
OR vig_stack on a futures/weather ladder). Kalshi typically lists MANY market
types per event (winner + total points + spread + period winners + player
props). When a strategy has a winning view on one market, the same view often
supports edge on OTHER markets within the same event — but we don't currently
surface those, leaving alpha on the table.

This heuristic emits two finding types from data we already collect:

1. concurrent_fire_candidate: events we already trade where settled CFs on a
   DIFFERENT market type within the SAME event show +CLV concurrent with our
   primary winning trade's time window. Two strategies that should fire
   concurrently from one underlying view.

2. scanner_gap: events we already trade where high-volume market types within
   the same series exist in universe.jsonl but never appear in
   decisions.jsonl/clv.json/paper_trades.json. We're not even looking at them.

Cross-event-family demotion mirrors Session 47's counterfactual_hotspots
discipline: per-cohort flag is preserved (data + Finding) but severity demotes
when cross-family aggregate contradicts (mean < 0¢) OR primary's sport is in
MOMENTUM_DISABLED_SPORTS (gate-tuning structurally neutralized — relaxing
produces zero new actual primary trades).

Acting on a NOTABLE/HIGH candidate that holds STABLE for ≥3 days is a separate
follow-up session (48b/48c/...), not auto-promotion.

Schema (per CLAUDE.md "Canonical Data Schema Reference"):
- decisions.jsonl: ts, ticker, opp_type. NO event_ticker (rsplit fallback).
- clv.json: ticker, recorded_at, clv_cents, market_result ('yes'/'no'), status,
  side. CFs have status='counterfactual_settled'. NO event_ticker.
- paper_trades.json: ticker, type, status, pnl, timestamp. NO event_ticker.
- universe.jsonl: ticker, event_ticker (PRESENT), series_ticker, volume_24h, ts.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from bot.config import MOMENTUM_DISABLED_SPORTS
from bot.regime import SPORT_PREFIXES

from .. import _sport_classifier
from ..findings import Finding
from .counterfactual_hotspots import _normalize_sport_for_disabled_check
from .outlier_pnl import _parse_ts

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = 30

# TYPE A: concurrent_fire_candidate
MIN_CONCURRENT_PAIRS = 5
MIN_MEAN_CONCURRENT_CLV_CENTS = 5.0
MIN_CONCURRENT_POSITIVE_RATE = 0.65
MIN_CONCURRENT_N_NO = 2          # survivorship guard, mirrors Session 47 MIN_NO_WON_COUNT shape

# TYPE A severity bumps
HIGH_SEVERITY_CONCURRENT_PAIRS = 15
HIGH_SEVERITY_MEAN_CLV_CENTS = 10.0
HIGH_SEVERITY_POSITIVE_RATE = 0.75
HIGH_SEVERITY_N_NO = 5

# TYPE B: scanner_gap
MIN_GAP_EVENTS = 10
MIN_GAP_AVG_VOLUME_24H = 1000
HIGH_SEVERITY_GAP_EVENTS = 50
HIGH_SEVERITY_GAP_VOLUME = 5000

# Session 47 cross-cohort demotion mirror, applied cross-event-family
CROSS_FAMILY_MEAN_DEMOTION_FLOOR = 0.0
DISABLED_SPORT_DEMOTION = True

# Concurrency time window — arbitrary first-pass per Session 48 brief
CONCURRENT_PAIR_WINDOW_HOURS = 2

_SEVERITY_LADDER = ("high", "notable", "info")

_SETTLED_OR_OPEN = {"won", "lost", "exited_early", "open"}

# Sort SPORT_PREFIXES keys longest-first for prefix matching (mirrors
# outlier_pnl.py:18 and universe_gap.py:27).
_SORTED_SPORT_PREFIX_KEYS = sorted(SPORT_PREFIXES.keys(), key=len, reverse=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_family(rec, *, prefer_field: bool = True) -> str | None:
    """Resolve event family.

    For dict inputs (universe rows), prefer the explicit `event_ticker` field
    when prefer_field=True. For string inputs (raw ticker) or dicts where
    prefer_field=False or the field is absent, fall back to
    ticker.rsplit('-', 1)[0]. Returns None for tickers without a hyphen —
    caller skips/logs.
    """
    if isinstance(rec, str):
        ticker = rec
    else:
        if prefer_field:
            et = rec.get("event_ticker")
            if et:
                return et
        ticker = rec.get("ticker", "") or ""
    if not ticker or "-" not in ticker:
        return None
    return ticker.rsplit("-", 1)[0]


def _market_type_from_ticker(ticker: str) -> str:
    """Coarse v1 market-type classifier — series-agnostic suffix label.

    Returns just the tail-pattern portion so the same betting CONCEPT (e.g.
    "totals" — letter+digit tail) compares EQUAL across series. This is what
    the cross-event-family aggregator (Session 47 mirror) keys on: same pair
    `(primary_strategy, candidate_market_type)` across NBA / NHL / MLB.

    Distinguishes winner-side (`team` tail = alpha-only) from totals / spreads /
    period markets (letter+digit tails like T220, B68.5, S8.5) within the same
    event family.

    Examples:
      'KXNBAGAME-26APR26CLETOR-CLE'  → 'team'        (alpha-only — winner side)
      'KXNBAGAME-26APR26CLETOR-T220' → 'T<n>'        (totals)
      'KXNBAGAME-26APR26CLETOR-B5'   → 'B<n>'        (spread)
      'KXMLB-26-LAD'                 → 'team'        (futures winner)
    """
    if not ticker or "-" not in ticker:
        return "unknown"
    _head, _, tail = ticker.rpartition("-")
    if not tail:
        return "empty"
    if tail.isalpha():
        return "team"
    if tail[0].isalpha() and any(c.isdigit() for c in tail):
        return f"{tail[0]}<n>"
    if tail.replace(".", "").isdigit():
        return "numeric"
    return "other"


def _is_winning_trade(t: dict) -> bool:
    """Winners: status='won' OR (status='exited_early' AND pnl > 0)."""
    status = t.get("status")
    if status == "won":
        return True
    if status == "exited_early":
        try:
            return float(t.get("pnl") or 0) > 0
        except (TypeError, ValueError):
            return False
    return False


def _cross_family_stats(pairs: list[tuple[str, float]]) -> dict:
    """Aggregate (family, clv_cents) pairs by family for the cross-family
    breakdown + cross-family raw mean used by the Session 47 demotion ladder.
    """
    by_family: dict[str, list[float]] = defaultdict(list)
    for family, cents in pairs:
        by_family[family].append(cents)
    breakdown = sorted(
        ((family, len(vals), round(statistics.mean(vals), 2))
         for family, vals in by_family.items()),
        key=lambda t: -t[1],
    )[:10]
    all_cents = [c for _f, c in pairs]
    raw_mean = statistics.mean(all_cents) if all_cents else 0.0
    n_pos = sum(1 for _, _, m in breakdown if m > 0)
    return {
        "cross_family_n_event_families": len(by_family),
        "cross_family_mean_clv_cents": round(raw_mean, 2),
        "cross_family_n_positive_event_families": n_pos,
        "cross_family_breakdown": breakdown,
    }


def _severity_concurrent_fire(*, n_pairs: int, mean_cents: float,
                              positive_rate: float, n_no: int,
                              cross_family_mean: float,
                              primary_sport_disabled: bool) -> str:
    """Session 47 mirror — base severity then cross-family + disabled-sport demotion."""
    if (n_pairs >= HIGH_SEVERITY_CONCURRENT_PAIRS
            and mean_cents >= HIGH_SEVERITY_MEAN_CLV_CENTS
            and positive_rate >= HIGH_SEVERITY_POSITIVE_RATE
            and n_no >= HIGH_SEVERITY_N_NO):
        base = "high"
    else:
        base = "notable"
    demote = 0
    if cross_family_mean < CROSS_FAMILY_MEAN_DEMOTION_FLOOR:
        demote += 1
    if DISABLED_SPORT_DEMOTION and primary_sport_disabled:
        demote += 1
    base_idx = _SEVERITY_LADDER.index(base)
    return _SEVERITY_LADDER[min(base_idx + demote, len(_SEVERITY_LADDER) - 1)]


def _severity_scanner_gap(n_events: int, avg_vol: float) -> str:
    if n_events >= HIGH_SEVERITY_GAP_EVENTS and avg_vol >= HIGH_SEVERITY_GAP_VOLUME:
        return "high"
    if n_events >= MIN_GAP_EVENTS and avg_vol >= MIN_GAP_AVG_VOLUME_24H:
        return "notable"
    return "info"


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------


class ConcurrentAttackAngles:
    name = "concurrent_attack_angles"
    data_sources = ("paper_trades", "decisions", "clv", "universe_iter")

    def run(self, ctx) -> list[Finding]:
        cutoff = ctx.loaded_at - dt.timedelta(days=LOOKBACK_DAYS)

        # Phase 1 — universe family map (single pass, latest snapshot per ticker)
        latest_row_by_ticker: dict[str, dict] = {}
        latest_ts_by_ticker: dict[str, dt.datetime] = {}
        for row in ctx.universe_iter():
            ticker = row.get("ticker")
            if not ticker:
                continue
            ts = _parse_ts(row.get("ts"))
            existing_ts = latest_ts_by_ticker.get(ticker)
            if existing_ts is None or (ts is not None and ts > existing_ts):
                latest_row_by_ticker[ticker] = row
                if ts is not None:
                    latest_ts_by_ticker[ticker] = ts

        family_to_universe_markets: dict[str, dict[str, dict]] = defaultdict(dict)
        family_to_series: dict[str, str] = {}
        for ticker, row in latest_row_by_ticker.items():
            family = _event_family(row, prefer_field=True)
            if family is None:
                continue
            family_to_universe_markets[family][ticker] = row
            if family not in family_to_series:
                series = row.get("series_ticker")
                if series:
                    family_to_series[family] = series

        # Phase 2 — trade index by event family
        family_to_trades: dict[str, list[dict]] = defaultdict(list)
        for t in ctx.paper_trades:
            if t.get("status") not in _SETTLED_OR_OPEN:
                continue
            ts = _parse_ts(t.get("timestamp"))
            if ts is None or ts < cutoff:
                continue
            family = _event_family(t, prefer_field=False)
            if family is None:
                continue
            family_to_trades[family].append(t)

        if not family_to_trades:
            return []

        # Phase 3 — CF index by event family (counterfactual_settled only)
        family_to_cfs: dict[str, list[dict]] = defaultdict(list)
        for r in ctx.clv:
            if r.get("status") != "counterfactual_settled":
                continue
            if r.get("clv_cents") is None:
                continue
            ts = _parse_ts(r.get("recorded_at"))
            if ts is None or ts < cutoff:
                continue
            family = _event_family(r, prefer_field=False)
            if family is None:
                continue
            family_to_cfs[family].append(r)

        # Phase 4 — scanned ticker set per family (decisions + CFs)
        family_to_scanned_tickers: dict[str, set[str]] = defaultdict(set)
        for d in ctx.decisions:
            ts = _parse_ts(d.get("ts"))
            if ts is None or ts < cutoff:
                continue
            ticker = d.get("ticker", "")
            family = _event_family(d, prefer_field=False)
            if family is None or not ticker:
                continue
            family_to_scanned_tickers[family].add(ticker)
        for family, rows in family_to_cfs.items():
            for r in rows:
                tk = r.get("ticker")
                if tk:
                    family_to_scanned_tickers[family].add(tk)

        findings: list[Finding] = []
        findings.extend(self._emit_concurrent_fire(
            family_to_trades, family_to_cfs,
        ))
        findings.extend(self._emit_scanner_gap(
            family_to_trades, family_to_scanned_tickers, family_to_universe_markets,
            family_to_series,
        ))
        return findings

    # ------------------------------------------------------------------
    # TYPE A: concurrent_fire_candidate
    # ------------------------------------------------------------------

    def _emit_concurrent_fire(self, family_to_trades, family_to_cfs) -> list[Finding]:
        # Per-tuple accumulators across all families.
        per_tuple_pairs: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        per_tuple_winners: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        per_tuple_primary_sport: dict[tuple[str, str, str], str | None] = {}
        per_tuple_primary_market_type: dict[tuple[str, str, str], str] = {}
        # Cross-family aggregator, keyed by (primary_strategy, candidate_market_type).
        per_cross_family: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)

        window = dt.timedelta(hours=CONCURRENT_PAIR_WINDOW_HOURS)

        for family, trades in family_to_trades.items():
            winners = [t for t in trades if _is_winning_trade(t)]
            if not winners:
                continue
            cfs = family_to_cfs.get(family, [])
            if not cfs:
                continue
            traded_tickers = {t["ticker"] for t in trades if t.get("ticker")}

            # series_ticker derived from the family. Universe-side has it
            # canonically, but Phase 6 also operates on families that may not
            # be in universe (delisted), so fall back to family head segment.
            family_head = family.split("-", 1)[0]

            for primary in winners:
                primary_ts = _parse_ts(primary.get("timestamp"))
                if primary_ts is None:
                    continue
                primary_ticker = primary.get("ticker") or ""
                primary_strategy = primary.get("type") or ""
                if not primary_strategy or not primary_ticker:
                    continue
                primary_market_type = _market_type_from_ticker(primary_ticker)
                primary_sport = _sport_classifier.sport_from_ticker_distinguished(primary_ticker)
                window_start = primary_ts - window
                window_end = primary_ts + window

                for cf in cfs:
                    cf_ticker = cf.get("ticker")
                    if not cf_ticker or cf_ticker in traded_tickers:
                        continue
                    cf_ts = _parse_ts(cf.get("recorded_at"))
                    if cf_ts is None or not (window_start <= cf_ts <= window_end):
                        continue
                    cand_market_type = _market_type_from_ticker(cf_ticker)
                    if cand_market_type == primary_market_type:
                        continue  # different market type only

                    # series_ticker for the event_family_pattern: prefer the
                    # primary's classifier; falls back to family head segment.
                    series_ticker = family_head
                    tuple_key = (series_ticker, primary_strategy, cand_market_type)
                    cents = float(cf["clv_cents"])
                    per_tuple_pairs[tuple_key].append({
                        "primary_trade_id": primary.get("id"),
                        "primary_pnl": float(primary.get("pnl") or 0),
                        "cf_clv_cents": cents,
                        "cf_market_result": cf.get("market_result"),
                        "family": family,
                    })
                    per_tuple_winners[tuple_key].add(primary.get("id") or primary_ticker)
                    if tuple_key not in per_tuple_primary_sport:
                        per_tuple_primary_sport[tuple_key] = primary_sport
                    if tuple_key not in per_tuple_primary_market_type:
                        per_tuple_primary_market_type[tuple_key] = primary_market_type
                    per_cross_family[(primary_strategy, cand_market_type)].append(
                        (family, cents)
                    )

        findings: list[Finding] = []
        for tuple_key, pairs in per_tuple_pairs.items():
            if len(pairs) < MIN_CONCURRENT_PAIRS:
                continue
            cents_list = [p["cf_clv_cents"] for p in pairs]
            mean_cents = statistics.mean(cents_list)
            if mean_cents < MIN_MEAN_CONCURRENT_CLV_CENTS:
                continue
            positive_count = sum(1 for c in cents_list if c > 0)
            positive_rate = positive_count / len(cents_list)
            if positive_rate < MIN_CONCURRENT_POSITIVE_RATE:
                continue
            n_no = sum(1 for p in pairs if p["cf_market_result"] == "no")
            if n_no < MIN_CONCURRENT_N_NO:
                continue
            n_yes = sum(1 for p in pairs if p["cf_market_result"] == "yes")

            series_ticker, primary_strategy, cand_market_type = tuple_key
            cross_pairs = per_cross_family[(primary_strategy, cand_market_type)]
            cross_stats = _cross_family_stats(cross_pairs)

            primary_sport = per_tuple_primary_sport.get(tuple_key)
            normalized_sport = _normalize_sport_for_disabled_check(primary_sport)
            sport_disabled = normalized_sport in MOMENTUM_DISABLED_SPORTS

            severity = _severity_concurrent_fire(
                n_pairs=len(pairs),
                mean_cents=mean_cents,
                positive_rate=positive_rate,
                n_no=n_no,
                cross_family_mean=cross_stats["cross_family_mean_clv_cents"],
                primary_sport_disabled=sport_disabled,
            )

            estimated_pnl = round(sum(cents_list) / 100.0, 2)

            event_family_pattern = f"{series_ticker}-*"
            evidence = {
                "finding_type": "concurrent_fire_candidate",
                "event_family_pattern": event_family_pattern,
                "series_ticker": series_ticker,
                "primary_strategy": primary_strategy,
                "primary_market_type": per_tuple_primary_market_type.get(
                    tuple_key, "unknown",
                ),
                "candidate_market_type": cand_market_type,
                "n_concurrent_pairs": len(pairs),
                "primary_won_count": len(per_tuple_winners[tuple_key]),
                "concurrent_cf_mean_clv_cents": round(mean_cents, 2),
                "concurrent_cf_positive_rate": round(positive_rate, 3),
                "concurrent_cf_n_no": n_no,
                "concurrent_cf_n_yes": n_yes,
                "estimated_realized_pnl_if_concurrent": estimated_pnl,
                **cross_stats,
                "primary_sport_disabled": sport_disabled,
                "_fingerprint_keys": [
                    "event_family_pattern", "primary_strategy", "candidate_market_type",
                ],
            }
            title = (
                f"{event_family_pattern}: {primary_strategy} winners + "
                f"{cand_market_type} concurrent CFs — n={len(pairs)}, "
                f"mean {mean_cents:+.1f}¢ at {int(positive_rate * 100)}% +CLV"
            )
            summary = (
                f"Across {cross_stats['cross_family_n_event_families']} event families "
                f"in series {event_family_pattern}, {primary_strategy} winning trades "
                f"co-occurred with {cand_market_type} CFs that closed at mean "
                f"{mean_cents:+.1f}¢ ({int(positive_rate * 100)}% +CLV, n_no={n_no}). "
                f"Concurrent strategy candidate — both views could fire on the same event."
            )
            suggested_action = (
                f"Open a Session 48-followup to prototype a {cand_market_type} scanner "
                f"that fires concurrently with {primary_strategy} on {event_family_pattern} "
                f"events. Wait for STABLE classification on ≥3 daily runs before building."
            )
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=title,
                summary=summary,
                evidence=evidence,
                suggested_action=suggested_action,
            ))
        return findings

    # ------------------------------------------------------------------
    # TYPE B: scanner_gap
    # ------------------------------------------------------------------

    def _emit_scanner_gap(self, family_to_trades, family_to_scanned_tickers,
                          family_to_universe_markets, family_to_series) -> list[Finding]:
        # Aggregate by (series_ticker, missing_market_type) across all event
        # families that we have ≥1 trade on.
        per_series_gap: dict[tuple[str, str], list[tuple[str, list[dict]]]] = defaultdict(list)

        for family, trades in family_to_trades.items():
            universe_markets = family_to_universe_markets.get(family, {})
            if not universe_markets:
                continue
            series_ticker = family_to_series.get(family)
            if not series_ticker:
                # fall back to longest SPORT_PREFIX match on family head
                series_ticker = next(
                    (p for p in _SORTED_SPORT_PREFIX_KEYS if family.startswith(p)),
                    family.split("-", 1)[0],
                )
            scanned_tickers = family_to_scanned_tickers.get(family, set())
            traded_tickers = {t["ticker"] for t in trades if t.get("ticker")}
            never_scanned = set(universe_markets.keys()) - scanned_tickers - traded_tickers
            if not never_scanned:
                continue
            by_market_type: dict[str, list[dict]] = defaultdict(list)
            for tk in never_scanned:
                mt = _market_type_from_ticker(tk)
                by_market_type[mt].append(universe_markets[tk])
            for mt, rows in by_market_type.items():
                per_series_gap[(series_ticker, mt)].append((family, rows))

        findings: list[Finding] = []
        for (series_ticker, missing_mt), entries in per_series_gap.items():
            n_events_with_gap = len(entries)
            if n_events_with_gap < MIN_GAP_EVENTS:
                continue
            all_volumes = [
                float(row.get("volume_24h") or 0)
                for _family, rows in entries for row in rows
            ]
            avg_vol = statistics.mean(all_volumes) if all_volumes else 0.0
            if avg_vol < MIN_GAP_AVG_VOLUME_24H:
                continue
            total_ticker_count = sum(len(rows) for _f, rows in entries)
            sample_tickers: list[str] = []
            for _f, rows in entries:
                for r in rows:
                    tk = r.get("ticker")
                    if tk:
                        sample_tickers.append(tk)
                    if len(sample_tickers) >= 5:
                        break
                if len(sample_tickers) >= 5:
                    break

            severity = _severity_scanner_gap(n_events_with_gap, avg_vol)

            evidence = {
                "finding_type": "scanner_gap",
                "series_ticker": series_ticker,
                "missing_market_type": missing_mt,
                "events_with_gap_count": n_events_with_gap,
                "avg_volume_24h": round(avg_vol, 1),
                "total_ticker_count": total_ticker_count,
                "sample_tickers": sample_tickers,
                "_fingerprint_keys": ["series_ticker", "missing_market_type"],
            }
            title = (
                f"{series_ticker}: {missing_mt} unscanned across {n_events_with_gap} "
                f"events (avg 24h vol {avg_vol:,.0f})"
            )
            summary = (
                f"Series {series_ticker} has {missing_mt}-type markets across "
                f"{n_events_with_gap} traded events that the scanner never touched, "
                f"despite avg 24h volume of {avg_vol:,.0f}. "
                f"{total_ticker_count} unscanned tickers total. We trade other angles "
                f"on these events but ignore this market type entirely."
            )
            suggested_action = (
                f"Open a Session 48-followup to prototype a scanner for {missing_mt} "
                f"markets within series {series_ticker}. Sample tickers: "
                f"{', '.join(sample_tickers[:3])}. Wait for STABLE classification on "
                f"≥3 daily runs before building."
            )
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=title,
                summary=summary,
                evidence=evidence,
                suggested_action=suggested_action,
            ))
        return findings


