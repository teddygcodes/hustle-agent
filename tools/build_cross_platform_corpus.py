"""Cross-platform Kalshi <-> Polymarket validation corpus builder (Session 116).

Scrapes settled politics/econ/crypto/geo markets from both venues, generates
candidate pairs via deterministic similarity (token-set Jaccard + date alignment),
writes a JSONL queue for operator hand-labeling.

The operator labels each pair as MATCH / NEEDS_REVIEW / NO_MATCH. The labeled
corpus is the validation gate for the S105 cross-platform settlement matcher.

S105 design doc: docs/superpowers/specs/2026-05-11-cross-platform-matcher-design.md

Out of scope:
- The matcher implementation itself (separate post-labeling session).
- Any agent/ touches (project_scope: bot/ only).
- LLM-based matching (anti-pattern per Predexon precedent).

Usage:
    python tools/build_cross_platform_corpus.py                  # full run
    python tools/build_cross_platform_corpus.py --skip-scrape    # reuse caches
    python tools/build_cross_platform_corpus.py --dry-run        # report counts only
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure helpers — normalize / jaccard / date-window
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "to", "for", "on", "at", "is", "be",
    "will", "by", "with", "and", "or", "as",
})

_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)


def normalize_tokens(text: str) -> set[str]:
    """Lowercase, ASCII-fold, split on word boundaries, drop stopwords."""
    if not text:
        return set()
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return {
        tok
        for tok in (m.group().lower() for m in _TOKEN_RE.finditer(folded))
        if tok and tok not in _STOPWORDS
    }


def jaccard(a: set[str], b: set[str]) -> float:
    """Token-set Jaccard similarity. Returns 0.0 if either set is empty."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def parse_iso_date(value):
    """Parse an ISO 8601 timestamp or date. Returns None on any failure."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def within_days(a, b, days: int) -> bool:
    """True if |a - b| <= days. Returns False for any None input."""
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) <= days * 86400


# ---------------------------------------------------------------------------
# Scrapers — Polymarket Gamma API + Kalshi public REST
# ---------------------------------------------------------------------------

POLYMARKET_BASE = "https://gamma-api.polymarket.com/markets"
POLYMARKET_CATEGORIES = frozenset({
    "politics", "us-current-affairs", "world", "geopolitics",
    "economy", "economics", "business", "finance",
    "crypto", "cryptocurrency",
})

KALSHI_EVENTS_BASE = "https://api.elections.kalshi.com/trade-api/v2/events"
# Kalshi categories that plausibly overlap with Polymarket coverage. Sports,
# Entertainment, Climate and Weather, etc. are excluded — no Polymarket
# overlap. The category enum is discovered via the /series endpoint.
KALSHI_DEFAULT_CATEGORIES: tuple[str, ...] = (
    "Politics", "Elections", "Economics", "Crypto", "World",
)
# MVE = Multi-Variate Event parlay expansions. Same filter the bot's
# universe.py applies — these are parlay variants, not real markets.
_KALSHI_MVE_PREFIX = "KXMVE"


def _polymarket_infer_result(outcome_prices_raw):
    """Polymarket has no explicit result field; infer from final outcomePrices.
    Returns 'yes', 'no', or None if ambiguous.
    """
    if isinstance(outcome_prices_raw, str):
        try:
            prices = json.loads(outcome_prices_raw)
        except (ValueError, TypeError):
            return None
    else:
        prices = outcome_prices_raw
    if not isinstance(prices, list) or len(prices) != 2:
        return None
    try:
        p_yes = float(prices[0])
        p_no = float(prices[1])
    except (ValueError, TypeError):
        return None
    if p_yes >= 0.99 and p_no <= 0.01:
        return "yes"
    if p_no >= 0.99 and p_yes <= 0.01:
        return "no"
    return None


def _http_get_with_retries(url: str, params: dict, attempts: int = 5) -> requests.Response:
    """GET with exponential backoff on connection errors and 429s. Raises on final failure."""
    last_exc = None
    for i in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                # Honor Retry-After header if present; otherwise exponential backoff
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else (2 ** (i + 1))
                logger.warning("429 from %s; sleeping %.1fs", url, wait)
                time.sleep(wait)
                last_exc = requests.HTTPError(f"429 from {url}", response=resp)
                continue
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise last_exc


def scrape_polymarket(
    page_size: int = 500,
    sleep_seconds: float = 0.5,
    max_pages: int = 200,
    min_close_date=None,
) -> list[dict]:
    """Fetch settled Polymarket markets via Gamma API.

    Sorts by closedTime descending (recent first), stops early when older than
    min_close_date. Note: Polymarket no longer tags recent markets with a top-
    level `category` field, so we skip category filtering and rely on token-set
    Jaccard similarity at pair-generation time to find topical overlap with
    Kalshi politics/econ/crypto markets.

    Returns list of normalized dicts: {venue, ticker, question, close_date, result, category, source_url}.
    """
    out: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        resp = _http_get_with_retries(
            POLYMARKET_BASE,
            {
                "closed": "true",
                "limit": page_size,
                "offset": offset,
                "order": "closedTime",
                "ascending": "false",
            },
        )
        page = resp.json() or []
        if not page:
            break
        if min_close_date is not None:
            newest_raw = page[0].get("closedTime") or page[0].get("endDate")
            newest = parse_iso_date(newest_raw) if newest_raw else None
            if newest is not None and newest < min_close_date:
                break
        for m in page:
            question = (m.get("question") or "").strip()
            if not question:
                continue
            close_date = m.get("closedTime") or m.get("endDate")
            if not close_date:
                continue
            if min_close_date is not None:
                parsed = parse_iso_date(close_date)
                if parsed is not None and parsed < min_close_date:
                    continue
            result = _polymarket_infer_result(m.get("outcomePrices"))
            if result is None:
                continue
            # Category may be present on older markets, null on newer ones; keep
            # whatever the API surfaces but never gate on it.
            category = (m.get("category") or "").strip()
            out.append({
                "venue": "polymarket",
                "ticker": str(m.get("id", "")),
                "question": question,
                "close_date": close_date,
                "result": result,
                "category": category,
                "source_url": f"https://polymarket.com/market/{m.get('slug', '')}",
            })
        offset += page_size
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return out


def _compose_kalshi_question(event_title: str, market: dict) -> str:
    """Combine the event-level question with the market's outcome specifier.

    Kalshi events have a parent question ("Who will be the next Supreme Leader of Iran?")
    and individual markets carry the outcome specifier (custom_strike Individual="Mojtaba
    Khamenei", or yes_sub_title="above 50%"). For corpus matching we want both.
    """
    event_title = (event_title or "").strip()
    subtitle = (
        market.get("yes_sub_title")
        or market.get("no_sub_title")
        or ""
    ).strip()
    custom = market.get("custom_strike") or {}
    if isinstance(custom, dict):
        # Take the first non-empty value (typically a candidate name or threshold)
        for v in custom.values():
            if v:
                subtitle = subtitle or str(v).strip()
                break
    if event_title and subtitle and subtitle.lower() not in event_title.lower():
        return f"{event_title} — {subtitle}"
    return event_title or subtitle


def scrape_kalshi(
    sleep_seconds: float = 1.5,
    max_pages: int = 200,
    categories: tuple[str, ...] = KALSHI_DEFAULT_CATEGORIES,
) -> list[dict]:
    """Fetch settled Kalshi markets via /events with nested markets.

    The Kalshi /events API IGNORES the `category` query parameter — same set
    of events comes back for every value. So we do a single pass and filter
    by the per-event `category` field client-side. The MVE parlay flood is
    filtered out by ticker prefix (same rule as bot/universe.py).

    Bypasses agent/kalshi_client.py (project_scope: bot/ only) — direct REST.
    Returns list of normalized dicts: {venue, ticker, question, close_date, result, category, source_url}.
    """
    out: list[dict] = []
    allowed_categories = {c.lower() for c in categories}
    seen_tickers: set[str] = set()
    cursor = ""
    for _ in range(max_pages):
        params: dict = {
            "status": "settled",
            "with_nested_markets": "true",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        resp = _http_get_with_retries(KALSHI_EVENTS_BASE, params)
        payload = resp.json() or {}
        events = payload.get("events") or []
        for ev in events:
            event_category = (ev.get("category") or "").strip()
            if event_category.lower() not in allowed_categories:
                continue
            event_title = ev.get("title") or ev.get("sub_title") or ""
            markets = ev.get("markets") or []
            for m in markets:
                ticker = m.get("ticker") or ""
                if not ticker or ticker.startswith(_KALSHI_MVE_PREFIX):
                    continue
                if ticker in seen_tickers:
                    continue
                result_raw = (m.get("result") or "").strip().lower()
                if result_raw not in ("yes", "no"):
                    continue
                close_date = m.get("close_time") or m.get("expiration_time")
                if not close_date:
                    continue
                question = _compose_kalshi_question(event_title, m)
                if not question:
                    continue
                seen_tickers.add(ticker)
                out.append({
                    "venue": "kalshi",
                    "ticker": ticker,
                    "question": question,
                    "close_date": close_date,
                    "result": result_raw,
                    "category": event_category,
                    "source_url": f"https://kalshi.com/markets/{ticker}",
                })
        cursor = payload.get("cursor") or ""
        if not cursor:
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return out


# ---------------------------------------------------------------------------
# Candidate-pair generation — suggested label rules + cross-join
# ---------------------------------------------------------------------------

# Looser than the eventual S105 matcher's thresholds: operator handles ambiguity.
CORPUS_JACCARD_FLOOR = 0.40   # below: NO_MATCH (and won't be emitted)
CORPUS_JACCARD_HIGH = 0.60    # matcher's eventual high-confidence threshold
CORPUS_DATE_FLOOR_DAYS = 3    # outside this: NO_MATCH (won't be emitted)
CORPUS_DATE_TIGHT_DAYS = 1    # matcher's eventual tight window


def suggest_label(
    jaccard_score: float,
    days_apart: float,
    kalshi_result: str,
    polymarket_result: str,
) -> str:
    """Suggest MATCH / NEEDS_REVIEW / NO_MATCH for a candidate pair.

    Conservative bias toward NEEDS_REVIEW when ambiguous — false MATCH
    suggestions train the operator to disagree, which slows labeling.
    """
    if jaccard_score < CORPUS_JACCARD_FLOOR or days_apart > CORPUS_DATE_FLOOR_DAYS:
        return "NO_MATCH"
    if (
        jaccard_score >= CORPUS_JACCARD_HIGH
        and days_apart <= CORPUS_DATE_TIGHT_DAYS
        and kalshi_result == polymarket_result
    ):
        return "MATCH"
    return "NEEDS_REVIEW"


def generate_candidate_pairs(
    kalshi_markets: list[dict],
    polymarket_markets: list[dict],
) -> list[dict]:
    """Cross-join Kalshi×Polymarket; emit pairs above corpus thresholds.

    Uses day-precision date bucketing on the Kalshi side so each Polymarket
    market only compares against Kalshi markets within ±CORPUS_DATE_FLOOR_DAYS
    of its close date — O(P × small bucket) instead of O(P × all-Kalshi).
    """
    from collections import defaultdict
    from datetime import timedelta

    kalshi_by_day: dict = defaultdict(list)
    for k in kalshi_markets:
        d = parse_iso_date(k["close_date"])
        if d is None:
            continue
        kalshi_by_day[d.date()].append({
            "market": k,
            "tokens": normalize_tokens(k["question"]),
            "date": d,
        })

    out: list[dict] = []
    for p in polymarket_markets:
        p_tokens = normalize_tokens(p["question"])
        p_date = parse_iso_date(p["close_date"])
        if p_date is None or not p_tokens:
            continue
        p_day = p_date.date()
        for offset in range(-CORPUS_DATE_FLOOR_DAYS, CORPUS_DATE_FLOOR_DAYS + 1):
            bucket = kalshi_by_day.get(p_day + timedelta(days=offset))
            if not bucket:
                continue
            for kp in bucket:
                score = jaccard(kp["tokens"], p_tokens)
                if score < CORPUS_JACCARD_FLOOR:
                    continue
                days_apart = abs((kp["date"] - p_date).total_seconds()) / 86400.0
                k = kp["market"]
                out.append({
                    "kalshi_ticker": k["ticker"],
                    "kalshi_question": k["question"],
                    "kalshi_close_date": k["close_date"],
                    "kalshi_result": k["result"],
                    "kalshi_category": k["category"],
                    "kalshi_url": k["source_url"],
                    "polymarket_ticker": p["ticker"],
                    "polymarket_question": p["question"],
                    "polymarket_close_date": p["close_date"],
                    "polymarket_result": p["result"],
                    "polymarket_category": p["category"],
                    "polymarket_url": p["source_url"],
                    "jaccard": round(score, 4),
                    "days_apart": round(days_apart, 3),
                    "suggested_label": suggest_label(score, days_apart, k["result"], p["result"]),
                    "operator_label": None,
                })
    return out


# ---------------------------------------------------------------------------
# Atomic writers — JSONL queue + JSON cache
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def write_jsonl(records: list[dict], path) -> None:
    """Write records as one-JSON-per-line. Atomic via os.replace."""
    if not records:
        _atomic_write(Path(path), "")
        return
    body = "\n".join(json.dumps(r) for r in records) + "\n"
    _atomic_write(Path(path), body)


def write_json_cache(payload: dict, path) -> None:
    """Write a JSON cache file. Atomic via os.replace."""
    _atomic_write(Path(path), json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------

DEFAULT_QUEUE_PATH = "bot/state/cross_platform_labeling_queue.jsonl"
DEFAULT_CACHE_DIR = "bot/state/cache"
_KALSHI_CACHE_NAME = "kalshi_settled_markets.json"
_POLY_CACHE_NAME = "polymarket_settled_markets.json"


def _load_cache(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    return payload.get("markets") or []


def build_corpus(
    queue_path,
    cache_dir,
    skip_scrape: bool,
    kalshi_only: bool,
    polymarket_only: bool,
    dry_run: bool,
    lookback_days: int = 180,
    queue_top_n: int = 5000,
) -> dict:
    queue_path = Path(queue_path)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    kalshi_cache = cache_dir / _KALSHI_CACHE_NAME
    poly_cache = cache_dir / _POLY_CACHE_NAME

    from datetime import timedelta
    min_close_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    if skip_scrape:
        kalshi_markets = _load_cache(kalshi_cache)
        poly_markets = _load_cache(poly_cache)
    else:
        if polymarket_only:
            kalshi_markets = _load_cache(kalshi_cache)
        else:
            logger.info("Scraping Kalshi settled markets...")
            kalshi_markets = scrape_kalshi()
            write_json_cache({"markets": kalshi_markets}, kalshi_cache)
        if kalshi_only:
            poly_markets = _load_cache(poly_cache)
        else:
            logger.info("Scraping Polymarket settled markets (sorted recent-first, lookback %dd)...", lookback_days)
            poly_markets = scrape_polymarket(min_close_date=min_close_date)
            write_json_cache({"markets": poly_markets}, poly_cache)

    # Apply lookback filter to both sides (esp. important when reusing a
    # pre-existing Kalshi cache that may contain markets from older eras).
    def _within_lookback(m):
        d = parse_iso_date(m.get("close_date"))
        return d is not None and d >= min_close_date

    kalshi_markets = [m for m in kalshi_markets if _within_lookback(m)]
    poly_markets = [m for m in poly_markets if _within_lookback(m)]

    all_pairs = generate_candidate_pairs(kalshi_markets, poly_markets)
    # Sort by Jaccard descending so the highest-quality candidates are at the top
    all_pairs.sort(key=lambda p: (p["jaccard"], -p["days_apart"]), reverse=True)

    full_breakdown = Counter(p["suggested_label"] for p in all_pairs)

    # Cap the labeling queue to a tractable, git-committable size. The full
    # uncapped set is written alongside the queue for downstream use.
    queue_pairs = all_pairs[:queue_top_n] if queue_top_n > 0 else all_pairs
    queue_breakdown = Counter(p["suggested_label"] for p in queue_pairs)

    summary = {
        "kalshi_count": len(kalshi_markets),
        "polymarket_count": len(poly_markets),
        "candidate_count_total": len(all_pairs),
        "candidate_count_queued": len(queue_pairs),
        "label_breakdown_total": dict(full_breakdown),
        "label_breakdown_queued": dict(queue_breakdown),
        "queue_top_n": queue_top_n,
    }

    if not dry_run:
        write_jsonl(queue_pairs, queue_path)
        summary["queue_path"] = str(queue_path)
        # Write full uncapped corpus to cache dir for advanced use
        if len(all_pairs) > len(queue_pairs):
            full_path = cache_dir / "cross_platform_candidate_pairs_full.jsonl"
            write_jsonl(all_pairs, full_path)
            summary["full_corpus_path"] = str(full_path)

    return summary


def _print_summary(summary: dict) -> None:
    print("=" * 60)
    print("Cross-platform validation corpus — build summary")
    print("=" * 60)
    print(f"  Kalshi settled markets:      {summary['kalshi_count']}")
    print(f"  Polymarket settled markets:  {summary['polymarket_count']}")
    print(f"  Total candidate pairs:       {summary['candidate_count_total']}")
    print(f"    breakdown:                 {summary['label_breakdown_total']}")
    print(f"  Queued for labeling (top {summary['queue_top_n']}):  {summary['candidate_count_queued']}")
    print(f"    breakdown:                 {summary['label_breakdown_queued']}")
    if "queue_path" in summary:
        print(f"  Queue written to:            {summary['queue_path']}")
    if "full_corpus_path" in summary:
        print(f"  Full corpus written to:      {summary['full_corpus_path']}")
    print("=" * 60)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--queue-path", default=DEFAULT_QUEUE_PATH, help="Output JSONL labeling queue path")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="Cache dir for scraped market JSON")
    parser.add_argument("--skip-scrape", action="store_true", help="Reuse cached scrapes; do not hit APIs")
    parser.add_argument("--kalshi-only", action="store_true", help="Only scrape Kalshi; reuse Polymarket cache")
    parser.add_argument("--polymarket-only", action="store_true", help="Only scrape Polymarket; reuse Kalshi cache")
    parser.add_argument("--dry-run", action="store_true", help="Print summary but do not write queue")
    parser.add_argument("--lookback-days", type=int, default=180, help="Filter markets to last N days (both sides)")
    parser.add_argument("--queue-top-n", type=int, default=5000, help="Cap queue file at top N by Jaccard descending (0 = no cap)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    summary = build_corpus(
        queue_path=Path(args.queue_path),
        cache_dir=Path(args.cache_dir),
        skip_scrape=args.skip_scrape,
        kalshi_only=args.kalshi_only,
        polymarket_only=args.polymarket_only,
        dry_run=args.dry_run,
        lookback_days=args.lookback_days,
        queue_top_n=args.queue_top_n,
    )
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
