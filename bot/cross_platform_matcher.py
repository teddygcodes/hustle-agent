"""Deterministic Kalshi <-> Polymarket settlement matcher.

Session 117 implementation of the Session 105 design doc. This module is pure:
it accepts already-normalized market dictionaries and performs no network or
filesystem I/O.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, NamedTuple


class MatchResult(Enum):
    MATCH_HIGH_CONFIDENCE = "match_high_confidence"
    MATCH_NEEDS_REVIEW = "match_needs_review"
    NO_MATCH = "no_match"
    INSUFFICIENT_DATA = "insufficient_data"


class MatchDecision(NamedTuple):
    result: MatchResult
    jaccard: float
    date_delta_hours: float | None
    source_match: bool
    reason: str


class BetTypeSignature(NamedTuple):
    kind: str
    unit: str | None = None
    number: int | None = None
    threshold: float | None = None
    score: str | None = None


class TimeGranularity(Enum):
    HOUR_SPECIFIC = "hour_specific"
    DAY_WIDE = "day_wide"
    DATE_RANGE = "date_range"
    INDEFINITE = "indefinite"


DATE_ALIGNMENT_HOURS = 24
HIGH_CONFIDENCE_JACCARD = 0.60
SOURCE_UPGRADE_JACCARD = 0.50
REVIEW_JACCARD = 0.30

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "to", "for", "on", "at", "is", "be",
    "will", "by", "with", "and", "or", "as",
})
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)
_NUMBER_RE = re.compile(r"(?<![a-z0-9])([+-]?\$?\d[\d,]*(?:\.\d+)?)(?![a-z0-9])")
_EXACT_SCORE_RE = re.compile(r"\bexact\s+score\b.*?\b(\d{1,2})\s*[-:]\s*(\d{1,2})\b")
_UNIT_WINNER_RE = re.compile(r"\b(set|map|game)\s*(\d{1,2})\b(?:\s+\w+){0,3}\s*\bwinner\b|\b(set|map|game)\s+(\d{1,2})\s*$")
_PERIOD_RE = re.compile(r"\b(1st|2nd|3rd|4th|first|second|third|fourth)\s+(half|quarter|inning|period)\b")
_HOUR_TEXT_RE = re.compile(
    r"\b(?:at\s*)?(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)\s*(?:e[ds]?t|utc|gmt|pt|ct|mt)?\b",
    flags=re.IGNORECASE,
)
_DATE_RANGE_TEXT_RE = re.compile(
    r"\b(?:between|from)\b.+\b(?:and|to|through|thru)\b|\b(?:through|until)\s+(?:may|june|july|august|september|october|november|december|january|february|march|april)\b",
    flags=re.IGNORECASE,
)
_DATE_ONLY_TEXT_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?\b",
    flags=re.IGNORECASE,
)

# Start intentionally empty per S105. Populate only from validated findings.
TICKER_FAMILY_RULES: dict[tuple[str, str], str] = {}


def normalize_tokens(text: str | None) -> set[str]:
    """Lowercase, ASCII-fold, split on word boundaries, drop stopwords."""
    if not text:
        return set()
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return {
        token
        for token in (m.group().lower() for m in _TOKEN_RE.finditer(folded))
        if token and token not in _STOPWORDS
    }


def jaccard(a: set[str], b: set[str]) -> float:
    """Token-set Jaccard similarity. Empty input returns 0.0."""
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def parse_datetime(value) -> datetime | None:
    """Parse datetime/date strings or return aware datetimes unchanged."""
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
    return dt


def date_delta_hours(a, b) -> float | None:
    """Absolute date delta in hours, or None when either date is missing."""
    left = parse_datetime(a)
    right = parse_datetime(b)
    if left is None or right is None:
        return None
    return abs((left - right).total_seconds()) / 3600.0


def dates_aligned(a, b, max_hours: int = DATE_ALIGNMENT_HOURS) -> bool:
    """True when the absolute close-date delta is <= max_hours."""
    delta = date_delta_hours(a, b)
    return delta is not None and delta <= max_hours


def normalize_source(source: str | None) -> str:
    """Normalize resolution-source strings for exact deterministic matching."""
    if not source:
        return ""
    folded = unicodedata.normalize("NFKD", source).encode("ascii", "ignore").decode("ascii")
    lowered = folded.lower()
    lowered = re.sub(r"https?://", "", lowered)
    lowered = re.sub(r"^www\.", "", lowered)
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(lowered.split())


def _fold_text(text: str | None) -> str:
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return folded.lower()


def _parse_number(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _first_number(text: str) -> float | None:
    match = _NUMBER_RE.search(text)
    return _parse_number(match.group(1)) if match else None


def _number_after_price_direction(text: str) -> float | None:
    money_match = re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)", text)
    if money_match:
        return _parse_number(money_match.group(1))
    direction_match = re.search(r"\b(?:above|below|over|under)\s+\$?\s*(\d[\d,]*(?:\.\d+)?)", text)
    if direction_match:
        return _parse_number(direction_match.group(1))
    return None


def _canonical_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 4)


def extract_bet_type(market: Mapping | str | None) -> BetTypeSignature:
    """Infer the market proposition type from reusable text/ticker markers."""
    if isinstance(market, Mapping):
        text = _combined_text(market)
        ticker = _market_id(market)
    else:
        text = str(market or "")
        ticker = ""
    folded = _fold_text(text)
    folded_ticker = _fold_text(ticker)

    exact = _EXACT_SCORE_RE.search(folded)
    if exact:
        return BetTypeSignature("exact_score", score=f"{int(exact.group(1))}-{int(exact.group(2))}")

    if "both teams to score" in folded or re.search(r"\bbtts\b", folded):
        return BetTypeSignature("both_teams_to_score")

    if "completed match" in folded or re.search(r"\bmatch\s+completed\b", folded):
        return BetTypeSignature("completed_match")

    top_n = re.search(r"\btop\s*(\d{1,3})\b", folded)
    if top_n and ("finish" in folded or "finishers" in folded):
        return BetTypeSignature("top_n_finish", threshold=float(top_n.group(1)))

    if re.search(r"\bdraw\b", folded):
        if re.search(r"\bhalf\s*time\b|\bat\s+halftime\b", folded):
            return BetTypeSignature("draw", unit="half")
        return BetTypeSignature("draw")

    handicap_threshold = None
    handicap_match = re.search(r"\(([+-]\d+(?:\.\d+)?)\)", folded)
    if handicap_match:
        handicap_threshold = _parse_number(handicap_match.group(1))
    if (
        "handicap" in folded
        or "cover the spread" in folded
        or re.search(r"\bspread\b", folded)
        or re.search(r"\bby\s+(?:more\s+than|at\s+least)\s+\d", folded)
        or handicap_threshold is not None
    ):
        unit = "map" if "map handicap" in folded else "game" if "game handicap" in folded else None
        return BetTypeSignature("handicap", unit=unit, threshold=_canonical_float(handicap_threshold))

    unit_match = _UNIT_WINNER_RE.search(folded)
    if unit_match:
        unit = unit_match.group(1) or unit_match.group(3)
        number = int(unit_match.group(2) or unit_match.group(4))
        kind = f"{unit}_winner"
        return BetTypeSignature(kind, unit=unit, number=number)

    period_match = _PERIOD_RE.search(folded)
    if period_match:
        period = period_match.group(1)
        period_number = {
            "1st": 1,
            "first": 1,
            "2nd": 2,
            "second": 2,
            "3rd": 3,
            "third": 3,
            "4th": 4,
            "fourth": 4,
        }.get(period)
        return BetTypeSignature("period_winner", unit=period_match.group(2), number=period_number)

    total_match = re.search(r"\bo\s*/\s*u\b|\bover\s*/\s*under\b|\btotal\b", folded)
    if total_match:
        return BetTypeSignature("total", threshold=_canonical_float(_first_number(folded[total_match.end():]) or _first_number(folded)))

    if re.search(r"\b(?:over|under)\s+\d+(?:\.\d+)?\b", folded) and not re.search(r"\bprice\b|\bbitcoin\b|\bethereum\b|\bbtc\b|\beth\b", folded):
        threshold_match = re.search(r"\b(?:over|under)\s+(\d+(?:\.\d+)?)\b", folded)
        return BetTypeSignature("total", threshold=_canonical_float(_parse_number(threshold_match.group(1)) if threshold_match else None))

    if re.search(r"\b(?:bitcoin|btc|ethereum|eth|price)\b", folded) and re.search(r"\b(?:above|below|over|under|or above|or below)\b", folded):
        threshold = _number_after_price_direction(folded)
        return BetTypeSignature("price_threshold", threshold=_canonical_float(threshold))

    if re.search(r"\bwin(?:s|ner)?\b|\bbeats?\b", folded):
        return BetTypeSignature("winner")

    if " vs " in folded or " vs. " in folded:
        if "game" in folded_ticker or "match" in folded_ticker:
            return BetTypeSignature("winner")
        if re.search(r"\bkx\w*map\b", folded_ticker):
            map_match = re.search(r"\bmap\s*(\d{1,2})\b", folded)
            return BetTypeSignature("map_winner", unit="map", number=int(map_match.group(1)) if map_match else None)
        return BetTypeSignature("winner")

    return BetTypeSignature("other")


def extract_time_granularity(market: Mapping | str | None) -> TimeGranularity:
    """Infer settlement-window granularity from explicit text first."""
    if isinstance(market, Mapping):
        text = _combined_text(market)
    else:
        text = str(market or "")
    folded = _fold_text(text)

    if _HOUR_TEXT_RE.search(folded):
        return TimeGranularity.HOUR_SPECIFIC
    if _DATE_RANGE_TEXT_RE.search(folded):
        return TimeGranularity.DATE_RANGE
    if re.search(r"\b(?:bitcoin|btc|ethereum|eth|price)\b", folded) and _DATE_ONLY_TEXT_RE.search(folded):
        return TimeGranularity.DAY_WIDE
    return TimeGranularity.INDEFINITE


def _bet_type_mismatch_reason(left: BetTypeSignature, right: BetTypeSignature) -> str | None:
    if left.kind == "other" or right.kind == "other":
        return "bet_type_ambiguous"
    if left != right:
        return f"bet_type_mismatch: {left} != {right}"
    return None


def _time_mismatch_reason(left: TimeGranularity, right: TimeGranularity) -> str | None:
    if left == right:
        return None
    if TimeGranularity.INDEFINITE in (left, right):
        return None
    return f"time_granularity_mismatch: {left.value} != {right.value}"


def _market_id(market: Mapping) -> str:
    return str(
        market.get("ticker")
        or market.get("id")
        or market.get("slug")
        or ""
    )


def _override_key(kalshi_market: Mapping, polymarket_market: Mapping) -> tuple[str, str]:
    return (_market_id(kalshi_market), _market_id(polymarket_market))


def _outcome(market: Mapping) -> str:
    return str(market.get("resolved_outcome") or market.get("result") or "").strip().lower()


def _question_text(market: Mapping) -> str:
    return str(
        market.get("question_text")
        or market.get("question")
        or market.get("title")
        or ""
    ).strip()


def _resolution_text(market: Mapping) -> str:
    return str(
        market.get("resolution_text")
        or market.get("rules_primary")
        or market.get("description")
        or ""
    ).strip()


def _combined_text(market: Mapping) -> str:
    question = _question_text(market)
    resolution = _resolution_text(market)
    return f"{question} {resolution}".strip()


def _same_family_allowed(kalshi_market: Mapping, polymarket_market: Mapping) -> bool:
    kalshi_family = str(kalshi_market.get("family") or "").strip().lower()
    poly_family = str(polymarket_market.get("family") or "").strip().lower()
    if not kalshi_family or not poly_family:
        return False
    return (kalshi_family, poly_family) in TICKER_FAMILY_RULES


def match_markets(
    kalshi_market: Mapping,
    polymarket_market: Mapping,
    manual_overrides: Mapping[tuple[str, str], Mapping] | None = None,
    *,
    date_window_hours: int = DATE_ALIGNMENT_HOURS,
) -> MatchDecision:
    """Classify a normalized Kalshi/Polymarket pair.

    Manual overrides take precedence over all algorithmic checks. Override shape:
    {("kalshi_ticker", "polymarket_slug_or_id"): {"decision": "allow"|"block"}}.
    """
    overrides = manual_overrides or {}
    override = overrides.get(_override_key(kalshi_market, polymarket_market))
    if override:
        decision = str(override.get("decision") or "").strip().lower()
        reason = str(override.get("reason") or "manual override").strip()
        if decision == "block":
            return MatchDecision(MatchResult.NO_MATCH, 0.0, None, False, f"manual_block: {reason}")
        if decision == "allow":
            return MatchDecision(
                MatchResult.MATCH_HIGH_CONFIDENCE,
                1.0,
                date_delta_hours(kalshi_market.get("close_date"), polymarket_market.get("close_date")),
                True,
                f"manual_allow: {reason}",
            )

    k_text = _combined_text(kalshi_market)
    p_text = _combined_text(polymarket_market)
    k_tokens = normalize_tokens(k_text)
    p_tokens = normalize_tokens(p_text)
    if not k_tokens or not p_tokens:
        return MatchDecision(MatchResult.INSUFFICIENT_DATA, 0.0, None, False, "missing_question_text")

    delta_hours = date_delta_hours(kalshi_market.get("close_date"), polymarket_market.get("close_date"))
    if delta_hours is None:
        return MatchDecision(MatchResult.INSUFFICIENT_DATA, 0.0, None, False, "missing_close_date")

    score = jaccard(k_tokens, p_tokens)
    k_outcome = _outcome(kalshi_market)
    p_outcome = _outcome(polymarket_market)
    if k_outcome and p_outcome and k_outcome != p_outcome:
        return MatchDecision(
            MatchResult.NO_MATCH,
            score,
            delta_hours,
            False,
            "resolved_outcome_conflict",
        )

    k_bet_type = extract_bet_type(kalshi_market)
    p_bet_type = extract_bet_type(polymarket_market)
    bet_type_reason = _bet_type_mismatch_reason(k_bet_type, p_bet_type)
    if bet_type_reason == "bet_type_ambiguous":
        return MatchDecision(
            MatchResult.MATCH_NEEDS_REVIEW,
            score,
            delta_hours,
            False,
            bet_type_reason,
        )
    if bet_type_reason:
        return MatchDecision(
            MatchResult.NO_MATCH,
            score,
            delta_hours,
            False,
            bet_type_reason,
        )

    k_time_granularity = extract_time_granularity(kalshi_market)
    p_time_granularity = extract_time_granularity(polymarket_market)
    time_reason = _time_mismatch_reason(k_time_granularity, p_time_granularity)
    if time_reason:
        return MatchDecision(
            MatchResult.MATCH_NEEDS_REVIEW,
            score,
            delta_hours,
            False,
            time_reason,
        )

    k_source = normalize_source(kalshi_market.get("resolution_source"))
    p_source = normalize_source(polymarket_market.get("resolution_source"))
    source_match = bool(k_source and p_source and k_source == p_source)
    aligned = delta_hours <= date_window_hours
    family_allowed = _same_family_allowed(kalshi_market, polymarket_market)

    if aligned and score >= HIGH_CONFIDENCE_JACCARD:
        return MatchDecision(
            MatchResult.MATCH_HIGH_CONFIDENCE,
            score,
            delta_hours,
            source_match,
            "date_aligned_and_keyword_high",
        )

    if aligned and source_match and score >= SOURCE_UPGRADE_JACCARD:
        return MatchDecision(
            MatchResult.MATCH_HIGH_CONFIDENCE,
            score,
            delta_hours,
            source_match,
            "resolution_source_upgrade",
        )

    if aligned and family_allowed and score >= SOURCE_UPGRADE_JACCARD:
        return MatchDecision(
            MatchResult.MATCH_HIGH_CONFIDENCE,
            score,
            delta_hours,
            source_match,
            "ticker_family_rule",
        )

    if score >= REVIEW_JACCARD:
        reason = "keyword_review_band"
        if not aligned:
            reason = "date_misaligned_needs_review"
        return MatchDecision(MatchResult.MATCH_NEEDS_REVIEW, score, delta_hours, source_match, reason)

    return MatchDecision(MatchResult.NO_MATCH, score, delta_hours, source_match, "keyword_overlap_low")
