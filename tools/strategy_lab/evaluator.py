"""Hypothetical-P&L evaluator for Strategy Lab.

For each ``CandidateOpportunity`` the candidate emits, find the matching
clv record (settled or counterfactual_settled, ticker + ±N hour ts join)
and compute settlement-anchored hypothetical P&L. Reuses
``bot.clv.compute_clv_cents`` for the CLV math when ``closing_yes_price``
is available — single source of truth, no parallel codepath (Session 13b
discipline).

LIMITATIONS (loud-document everywhere): no slippage, no exit-side logic,
no partial fills. Settlement-anchored only. Lab P&L is upper-bound — DO
NOT treat as forecast of production P&L.

Canonical schema: ``market_result`` ∈ {"yes", "no", null}. The lab's
tests forbid the suffix-_won anti-pattern in source files; see README
canonical-schema reminder.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.calibration import _parse_iso  # noqa: E402
from bot.clv import compute_clv_cents  # noqa: E402

from .candidate import CandidateOpportunity  # noqa: E402

DEFAULT_CONTRACTS = 100
UNRESOLVED = "UNRESOLVED"


@dataclass
class ScoredOpportunity:
    """One candidate opp joined to its (possibly missing) clv record."""

    opp: CandidateOpportunity
    universe_ts: str  # ISO 8601 — the row that produced the opp
    sport: Optional[str] = None
    matched_clv: Optional[dict] = None
    status: str = UNRESOLVED  # "settled", "counterfactual_settled", or UNRESOLVED
    market_result: Optional[str] = None  # canonical "yes" | "no" | None
    clv_cents: Optional[float] = None  # from settled record OR computed via compute_clv_cents
    pnl_cents: Optional[float] = None  # hypothetical P&L
    contracts: int = DEFAULT_CONTRACTS
    extra: dict = field(default_factory=dict)


def _ticker_to_sport(ticker: str) -> Optional[str]:
    """Coarse sport classifier — uses ``bot.regime._ticker_to_sport``.

    Matches the bot's vocabulary (mlb / nba / nhl / atp / wta / ufc / ipl
    / weather_high / index, etc.). NOT the discovery agent's per-game-vs-
    futures distinguished form (that's a different layer).
    """
    try:
        from bot.regime import _ticker_to_sport as _bot_classifier
    except Exception:
        return None
    return _bot_classifier(ticker)


def _match_clv_record(
    ticker: str,
    universe_ts: str,
    clv_lookup: dict[str, list[dict]],
    *,
    match_window_hours: float,
) -> Optional[dict]:
    """Find a settled clv record for ``ticker`` within ±N hours of ``universe_ts``.

    Eligible statuses: ``settled`` (real trade) and ``counterfactual_settled``
    (Session 6 CF emissions). Open / counterfactual_open records are skipped
    (no settlement yet). Returns the first match (records are typically in
    insertion order).
    """
    anchor = _parse_iso(universe_ts)
    if anchor is None:
        return None
    candidates = clv_lookup.get(ticker, [])
    if not candidates:
        return None
    window = timedelta(hours=match_window_hours)
    for rec in candidates:
        if rec.get("status") not in ("settled", "counterfactual_settled"):
            continue
        if rec.get("market_result") not in ("yes", "no"):
            continue
        rec_ts = _parse_iso(rec.get("recorded_at"))
        if rec_ts is None:
            continue
        if abs(rec_ts - anchor) > window:
            continue
        return rec
    return None


def _compute_pnl_cents(
    side: str,
    entry_cents: float,
    market_result: str,
    closing_yes_price: Optional[float],
    contracts: int,
) -> tuple[float, float]:
    """Return (clv_cents, pnl_cents) using compute_clv_cents when possible.

    When the matched clv record carries ``closing_yes_price``, defer to
    ``bot.clv.compute_clv_cents`` (single source of truth). Otherwise fall
    back to the explicit settlement formula derived from ``market_result``.
    """
    if closing_yes_price is not None:
        clv_cents, _ = compute_clv_cents(side, int(round(entry_cents)), float(closing_yes_price))
    else:
        # Fall back to discrete settlement math: implicitly closing_yes_price
        # is 100 (yes won) or 0 (no won).
        implied_close = 100.0 if market_result == "yes" else 0.0
        clv_cents, _ = compute_clv_cents(side, int(round(entry_cents)), implied_close)
    pnl_cents = clv_cents * contracts
    return float(clv_cents), float(pnl_cents)


def score(
    opps_with_ts: list[tuple[CandidateOpportunity, str]],
    clv_lookup: dict[str, list[dict]],
    *,
    match_window_hours: float = 2.0,
) -> list[ScoredOpportunity]:
    """Score every candidate opp against ``clv_lookup``.

    ``opps_with_ts`` is a list of ``(opp, universe_row_ts)`` pairs — the
    driver builds it as it iterates. ``match_window_hours`` is the ±N
    join window (candidate may override via ``clv_match_window_hours``).

    Each opp produces exactly one ``ScoredOpportunity``; UNRESOLVED status
    means "no settled clv record matched, so we cannot judge this trade."
    """
    scored: list[ScoredOpportunity] = []
    for opp, universe_ts in opps_with_ts:
        contracts = DEFAULT_CONTRACTS
        if opp.extra and isinstance(opp.extra.get("contracts"), int):
            contracts = max(1, opp.extra["contracts"])

        result = ScoredOpportunity(
            opp=opp,
            universe_ts=universe_ts,
            sport=_ticker_to_sport(opp.ticker),
            contracts=contracts,
        )

        match = _match_clv_record(
            opp.ticker, universe_ts, clv_lookup, match_window_hours=match_window_hours
        )
        if match is None:
            scored.append(result)
            continue

        result.matched_clv = match
        result.status = match.get("status") or UNRESOLVED
        result.market_result = match.get("market_result")

        closing_yes = match.get("closing_yes_price")
        clv_cents, pnl_cents = _compute_pnl_cents(
            side=opp.side,
            entry_cents=opp.target_price_cents,
            market_result=result.market_result or "yes",
            closing_yes_price=closing_yes,
            contracts=contracts,
        )
        result.clv_cents = clv_cents
        result.pnl_cents = pnl_cents
        scored.append(result)

    return scored


def aggregate(scored: list[ScoredOpportunity]) -> dict:
    """Compute summary stats for the report.

    Returns a dict with: ``n_total``, ``n_resolved``, ``n_unresolved``,
    ``settle_rate``, ``mean_clv_cents``, ``win_rate_pct`` (over resolved),
    ``total_pnl_cents``, ``per_sport`` (per-sport breakdown), and
    ``per_confidence_decile`` (when candidates emit confidence variation).
    """
    n_total = len(scored)
    resolved = [s for s in scored if s.status != UNRESOLVED and s.pnl_cents is not None]
    n_resolved = len(resolved)
    n_unresolved = n_total - n_resolved

    if n_resolved:
        clvs = [s.clv_cents for s in resolved if s.clv_cents is not None]
        mean_clv = sum(clvs) / len(clvs) if clvs else None
        wins = sum(1 for s in resolved if (s.clv_cents or 0) > 0)
        win_rate = 100.0 * wins / n_resolved
        total_pnl = sum(s.pnl_cents for s in resolved)
    else:
        mean_clv = None
        win_rate = None
        total_pnl = 0.0

    # Per-sport breakdown
    per_sport: dict[str, dict] = {}
    by_sport: dict[str, list[ScoredOpportunity]] = {}
    for s in scored:
        key = s.sport or "unknown"
        by_sport.setdefault(key, []).append(s)
    for sport, items in by_sport.items():
        rs = [x for x in items if x.status != UNRESOLVED and x.clv_cents is not None]
        per_sport[sport] = {
            "n": len(items),
            "n_resolved": len(rs),
            "mean_clv_cents": (sum(x.clv_cents for x in rs) / len(rs)) if rs else None,
            "total_pnl_cents": sum((x.pnl_cents or 0) for x in rs),
        }

    # Per-confidence-decile (only meaningful if candidates emit varied confidence)
    confidences = [s.opp.confidence for s in scored]
    has_variation = len(set(round(c, 2) for c in confidences)) > 1
    per_decile: dict[str, dict] = {}
    if has_variation:
        for s in scored:
            decile = int(min(9, max(0, s.opp.confidence * 10)))
            bucket = f"[{decile/10:.1f}, {(decile+1)/10:.1f})"
            per_decile.setdefault(bucket, []).append(s)
        per_decile = {
            b: {
                "n": len(items),
                "n_resolved": sum(
                    1 for x in items if x.status != UNRESOLVED and x.clv_cents is not None
                ),
                "mean_clv_cents": _safe_mean(
                    [x.clv_cents for x in items if x.clv_cents is not None]
                ),
                "total_pnl_cents": sum((x.pnl_cents or 0) for x in items),
            }
            for b, items in per_decile.items()
        }

    return {
        "n_total": n_total,
        "n_resolved": n_resolved,
        "n_unresolved": n_unresolved,
        "settle_rate_pct": (100.0 * n_resolved / n_total) if n_total else 0.0,
        "mean_clv_cents": mean_clv,
        "win_rate_pct": win_rate,
        "total_pnl_cents": total_pnl,
        "total_pnl_dollars": total_pnl / 100.0,
        "per_sport": per_sport,
        "per_confidence_decile": per_decile,
    }


def _safe_mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)
