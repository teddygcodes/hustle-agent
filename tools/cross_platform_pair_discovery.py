#!/usr/bin/env python3
"""Discover live Kalshi <-> Polymarket pairs for observation-only scanning.

The candidate-generation prefilter is imported from
``tools.build_cross_platform_corpus`` rather than reimplemented. That keeps the
live discovery path aligned with the S116 settled-corpus path: date bucketing
within +/-3 days and token-set Jaccard >= 0.40.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.kalshi_client import get_markets as kalshi_get_markets
from bot import polymarket_gamma_client
from bot.cross_platform_matcher import MatchResult, extract_bet_type, match_markets
from tools.build_cross_platform_corpus import generate_candidate_pairs

try:
    from bot.cross_platform_matcher import _market_sport
except ImportError:  # pragma: no cover - defensive only
    _market_sport = None


logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_PATH = Path("bot/state/cross_platform_pair_registry.jsonl")
KALSHI_PAGE_LIMIT = 200


class CrossPlatformDiscoveryError(RuntimeError):
    """Raised when discovery cannot complete safely."""


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_close_date(market: dict) -> str:
    dt = _parse_datetime(market.get("close_date"))
    return dt.isoformat() if dt else ""


def _compose_kalshi_question(market: dict) -> str:
    title = str(market.get("title") or "").strip()
    subtitle = str(market.get("subtitle") or "").strip()
    if title and subtitle and subtitle.lower() not in title.lower():
        return f"{title} - {subtitle}"
    return title or subtitle


def normalize_kalshi_for_matcher(market: dict) -> dict:
    ticker = str(market.get("ticker") or "")
    question = _compose_kalshi_question(market)
    close_date = _parse_datetime(market.get("close_time") or market.get("expiration_time"))
    source_url = f"https://kalshi.com/markets/{ticker}" if ticker else ""
    rules_primary = str(market.get("rules_primary") or "").strip()
    return {
        "venue": "kalshi",
        "ticker": ticker,
        "id": ticker,
        "slug": ticker,
        "url": source_url,
        "question": question,
        "question_text": question,
        "title": question,
        "close_date": close_date,
        "resolution_text": rules_primary,
        "rules_primary": rules_primary,
        "description": rules_primary,
        "result": str(market.get("result") or "").strip().lower(),
        "category": str(market.get("category") or market.get("series_ticker") or "").strip(),
        "source_url": source_url,
        "event_ticker": market.get("event_ticker"),
        "series_ticker": market.get("series_ticker"),
        "yes_bid": market.get("yes_bid"),
        "yes_ask": market.get("yes_ask"),
        "volume_24h": market.get("volume_24h"),
    }


def _candidate_shape(market: dict) -> dict:
    return {
        "venue": market.get("venue"),
        "ticker": str(market.get("ticker") or market.get("id") or market.get("slug") or ""),
        "question": str(market.get("question") or market.get("question_text") or market.get("title") or ""),
        "close_date": _iso_close_date(market),
        "result": str(market.get("result") or ""),
        "category": str(market.get("category") or ""),
        "source_url": str(market.get("source_url") or market.get("url") or ""),
    }


def fetch_open_kalshi_markets(max_markets: int = 5000) -> list[dict]:
    if max_markets <= 0:
        return []
    out: list[dict] = []
    cursor: str | None = None
    while len(out) < max_markets:
        limit = min(KALSHI_PAGE_LIMIT, max_markets - len(out))
        kwargs: dict[str, Any] = {"status": "open", "limit": limit}
        if cursor:
            kwargs["cursor"] = cursor
        result = kalshi_get_markets(**kwargs)
        if not isinstance(result, dict) or "error" in result:
            raise CrossPlatformDiscoveryError(f"Kalshi open-market fetch failed: {result!r}")
        batch = result.get("markets") or []
        out.extend(batch[: max_markets - len(out)])
        cursor = result.get("cursor")
        if not cursor or not batch:
            break
    return out


def fetch_active_polymarket_markets(max_markets: int = 5000) -> list[dict]:
    if max_markets <= 0:
        return []
    pages = (max_markets + 99) // 100
    markets = polymarket_gamma_client.get_active_markets(limit_per_page=100, max_pages=pages)
    return markets[:max_markets]


def _pair_id(kalshi_id: str, polymarket_id: str) -> str:
    return hashlib.sha256(f"{kalshi_id}|{polymarket_id}".encode("utf-8")).hexdigest()[:16]


def _sport_tag(kalshi_market: dict, polymarket_market: dict) -> str:
    for market in (kalshi_market, polymarket_market):
        if _market_sport:
            try:
                sport = _market_sport(market)
            except Exception:
                sport = None
            if sport:
                if sport == "baseball":
                    return "MLB"
                return sport.upper()
    return "unknown"


def _market_type_tag(kalshi_market: dict, polymarket_market: dict) -> str:
    for market in (kalshi_market, polymarket_market):
        try:
            bet_type = extract_bet_type(market)
        except Exception:
            continue
        if bet_type.kind and bet_type.kind != "other":
            return bet_type.kind
    return "unknown"


def _time_to_resolution_band(close_date: Any, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    dt = _parse_datetime(close_date)
    if dt is None:
        return "unknown"
    delta = dt - now
    if delta.total_seconds() < 0:
        logger.warning("active discovery saw past close_date=%s", dt.isoformat())
        return "live"
    if delta.total_seconds() <= 86400:
        return "today"
    if delta.total_seconds() <= 7 * 86400:
        return "1-7d"
    return "7d+"


def build_registry_rows(
    kalshi_markets: list[dict],
    polymarket_markets: list[dict],
    *,
    discovered_at: datetime | None = None,
    verbose: bool = False,
) -> tuple[list[dict], dict]:
    discovered_at = discovered_at or datetime.now(timezone.utc)
    kalshi_full = [normalize_kalshi_for_matcher(m) for m in kalshi_markets]
    poly_full = [polymarket_gamma_client.normalize_for_matcher(m) for m in polymarket_markets]

    kalshi_full = [m for m in kalshi_full if m.get("ticker") and m.get("question") and m.get("close_date")]
    poly_full = [m for m in poly_full if m.get("ticker") and m.get("question") and m.get("close_date")]

    kalshi_candidates = [_candidate_shape(m) for m in kalshi_full]
    poly_candidates = [_candidate_shape(m) for m in poly_full]
    candidates = generate_candidate_pairs(kalshi_candidates, poly_candidates)
    kalshi_by_id = {m["ticker"]: m for m in kalshi_full}
    poly_by_id = {m["ticker"]: m for m in poly_full}

    rows: list[dict] = []
    classification_counts: Counter[str] = Counter()
    for candidate in candidates:
        kalshi_id = candidate["kalshi_ticker"]
        polymarket_id = candidate["polymarket_ticker"]
        kalshi_market = kalshi_by_id.get(kalshi_id)
        polymarket_market = poly_by_id.get(polymarket_id)
        if not kalshi_market or not polymarket_market:
            continue
        decision = match_markets(kalshi_market, polymarket_market, manual_overrides=None)
        classification_counts[decision.result.name] += 1
        if verbose:
            logger.info("%s <> %s => %s (%s)", kalshi_id, polymarket_id, decision.result.name, decision.reason)
        if decision.result != MatchResult.MATCH_HIGH_CONFIDENCE:
            continue
        rows.append({
            "pair_id": _pair_id(kalshi_id, polymarket_id),
            "kalshi_market_id": kalshi_id,
            "polymarket_market_id": polymarket_id,
            "kalshi_url": kalshi_market.get("source_url") or kalshi_market.get("url") or "",
            "polymarket_url": polymarket_market.get("source_url") or polymarket_market.get("url") or "",
            "matcher_classification": decision.result.name,
            "matcher_reason": decision.reason,
            "discovered_at": discovered_at.isoformat(),
            "slice_tags": {
                "sport": _sport_tag(kalshi_market, polymarket_market),
                "market_type": _market_type_tag(kalshi_market, polymarket_market),
                "time_to_resolution_band": _time_to_resolution_band(kalshi_market.get("close_date"), discovered_at),
            },
        })

    summary = {
        "kalshi_count": len(kalshi_full),
        "polymarket_count": len(poly_full),
        "candidate_count": len(candidates),
        "classification_counts": dict(classification_counts),
        "high_confidence_count": len(rows),
        "sport_breakdown": dict(Counter(row["slice_tags"]["sport"] for row in rows)),
    }
    return rows, summary


def write_registry_atomic(records: list[dict], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    tmp.write_text(body)
    os.replace(tmp, path)


def _print_summary(summary: dict, output_path: str | Path, *, dry_run: bool) -> None:
    sports = summary.get("sport_breakdown") or {}
    sport_text = ", ".join(f"{sport}={count}" for sport, count in sorted(sports.items())) or "none"
    destination = "stdout" if dry_run else str(output_path)
    print(
        f"Fetched {summary['kalshi_count']} kalshi + {summary['polymarket_count']} polymarket markets. "
        f"Generated {summary['candidate_count']} candidate pairs (Jaccard >= 0.40, +/-3 day window). "
        f"Matcher classified {summary['high_confidence_count']} as MATCH_HIGH_CONFIDENCE. "
        f"Wrote registry to {destination}. Sport breakdown: {sport_text}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-path", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--max-kalshi", type=int, default=5000)
    parser.add_argument("--max-polymarket", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        kalshi_markets = fetch_open_kalshi_markets(max_markets=args.max_kalshi)
        polymarket_markets = fetch_active_polymarket_markets(max_markets=args.max_polymarket)
        rows, summary = build_registry_rows(
            kalshi_markets,
            polymarket_markets,
            verbose=args.verbose,
        )
        if args.dry_run:
            for row in rows:
                print(json.dumps(row, sort_keys=True))
        else:
            write_registry_atomic(rows, args.output_path)
        _print_summary(summary, args.output_path, dry_run=args.dry_run)
        return 0
    except Exception as exc:
        logger.error("cross-platform pair discovery failed: %s", exc, exc_info=args.verbose)
        return 1


if __name__ == "__main__":
    sys.exit(main())
