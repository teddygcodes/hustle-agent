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


DATE_ALIGNMENT_HOURS = 24
HIGH_CONFIDENCE_JACCARD = 0.60
SOURCE_UPGRADE_JACCARD = 0.50
REVIEW_JACCARD = 0.30

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "to", "for", "on", "at", "is", "be",
    "will", "by", "with", "and", "or", "as",
})
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)

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
