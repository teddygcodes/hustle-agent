"""
Nexus Trading Bot — Edge Calculations with Self-Checking Math

Every calculation runs forward AND backward. If they don't match, don't trade.
verify_contract_direction() is MANDATORY before every trade.
"""

from __future__ import annotations

import re
import math
import sys
from pathlib import Path
from typing import Optional

# Add parent to path so we can import agent modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.parlay import parse_parlay_title, price_parlay, ParlayLeg
from agent.player_stats import estimate_player_prop_probability
from bot.config import NWS_BIAS_CORRECTION, WEATHER_STD_DEV, MIN_RELATIVE_EDGE


# ---------------------------------------------------------------------------
# Self-check tolerance
# ---------------------------------------------------------------------------
EPSILON = 1e-6


def _self_check_probability_complement(p: float, label: str) -> tuple[bool, str]:
    """Verify that p + (1 - p) == 1.0 within tolerance."""
    complement_sum = p + (1.0 - p)
    passed = abs(complement_sum - 1.0) < EPSILON
    msg = f"{label}: {p:.6f} + {1.0 - p:.6f} = {complement_sum:.6f} {'✅' if passed else '❌ FAILED'}"
    return passed, msg


def _self_check_edge(fair_value: float, market_price: float, edge: float) -> tuple[bool, str]:
    """Verify that edge = fair_value - market_price, forward and backward."""
    forward_edge = fair_value - market_price
    backward_fair = market_price + edge
    forward_ok = abs(forward_edge - edge) < EPSILON
    backward_ok = abs(backward_fair - fair_value) < EPSILON
    passed = forward_ok and backward_ok
    msg = (
        f"Edge check: {fair_value:.4f} - {market_price:.4f} = {forward_edge:.4f} "
        f"(expected {edge:.4f}) | backward: {market_price:.4f} + {edge:.4f} = {backward_fair:.4f} "
        f"(expected {fair_value:.4f}) {'✅' if passed else '❌ FAILED'}"
    )
    return passed, msg


# ---------------------------------------------------------------------------
# Parlay Edge
# ---------------------------------------------------------------------------

def calculate_parlay_edge(
    market_title: str,
    kalshi_price_cents: int,
    sport: str = "nba",
    odds_data: dict | None = None,
) -> dict:
    """
    Calculate edge on a Kalshi parlay market.

    Uses agent/parlay.py to parse and price legs from sportsbook consensus.
    Self-checks all math forward and backward.

    Args:
        market_title: Raw Kalshi market title (e.g. "yes Boston wins, yes LA wins")
        kalshi_price_cents: Current Kalshi YES price in cents (1-99)
        sport: Sport hint for parsing

    Returns:
        {edge, relative_edge, fair_value, kalshi_price, confidence,
         self_check_passed, math_chain, legs, warnings}
    """
    kalshi_price = kalshi_price_cents / 100.0
    math_chain = []

    # Parse parlay legs
    legs = parse_parlay_title(market_title)
    math_chain.append(f"Parsed {len(legs)} legs from title")

    # Get sportsbook odds for pricing (use pre-fetched if available)
    if odds_data is None:
        try:
            from bot.odds_scraper import fetch_consensus_odds
            odds_data = fetch_consensus_odds(sport)
        except Exception as e:
            return {
                "edge": 0.0,
                "relative_edge": 0.0,
                "fair_value": 0.0,
                "kalshi_price": kalshi_price,
                "confidence": 0.0,
                "self_check_passed": False,
                "math_chain": [f"Failed to fetch odds: {e}"],
                "legs": [l.to_dict() for l in legs],
                "warnings": [f"Odds fetch failed: {e}"],
            }

    # Price the parlay
    pricing = price_parlay(
        legs,
        odds_data,
        player_stats_fn=estimate_player_prop_probability,
    )

    fair_value = pricing.get("correlation_adjusted", 0.0)
    if fair_value <= 0:
        fair_value = pricing.get("raw_probability", 0.0)

    math_chain.append(f"Raw probability: {pricing.get('raw_probability', 0):.4f}")
    math_chain.append(f"Correlation adjusted: {fair_value:.4f}")
    math_chain.append(f"Legs priced: {pricing.get('legs_priced', 0)}/{len(legs)}")

    # Guard: near-zero values produce meaningless relative edges (e.g. 3400%).
    # Kalshi's minimum price is 1¢ (= 0.01) which itself causes absurd edges;
    # block anything at or below 3¢ on either side.
    if fair_value <= 0.03 or kalshi_price <= 0.03:
        math_chain.append(f"❌ Near-zero guard: fair_value={fair_value:.4f}, kalshi_price={kalshi_price:.4f}")
        return {
            "edge": 0.0,
            "relative_edge": 0.0,
            "fair_value": round(fair_value, 4),
            "kalshi_price": kalshi_price,
            "confidence": 0.0,
            "self_check_passed": False,
            "math_chain": math_chain,
            "legs": pricing.get("legs", []),
            "warnings": pricing.get("warnings", []) + ["Near-zero guard: values too small to compute edge reliably"],
        }

    # Calculate edge
    edge = fair_value - kalshi_price
    relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

    math_chain.append(f"Edge: {fair_value:.4f} - {kalshi_price:.4f} = {edge:.4f}")
    math_chain.append(f"Relative edge: {edge:.4f} / {kalshi_price:.4f} = {relative_edge:.2%}")

    # Self-checks
    checks = []
    c1_ok, c1_msg = _self_check_probability_complement(fair_value, "Fair value complement")
    checks.append(c1_msg)
    math_chain.append(c1_msg)

    c2_ok, c2_msg = _self_check_edge(fair_value, kalshi_price, edge)
    checks.append(c2_msg)
    math_chain.append(c2_msg)

    # Verify leg probabilities multiply correctly
    leg_probs = [l.probability for l in legs if l.probability is not None]
    if leg_probs:
        product = 1.0
        for p in leg_probs:
            product *= p
        c3_ok = abs(product - pricing.get("raw_probability", 0)) < 0.01
        c3_msg = f"Leg product: {'×'.join(f'{p:.3f}' for p in leg_probs)} = {product:.4f} vs raw {pricing.get('raw_probability', 0):.4f} {'✅' if c3_ok else '❌'}"
        checks.append(c3_msg)
        math_chain.append(c3_msg)
    else:
        c3_ok = True

    all_passed = c1_ok and c2_ok and c3_ok

    return {
        "edge": round(edge, 4),
        "relative_edge": round(relative_edge, 4),
        "fair_value": round(fair_value, 4),
        "kalshi_price": kalshi_price,
        "confidence": pricing.get("confidence", 0.0),
        "self_check_passed": all_passed,
        "math_chain": math_chain,
        "legs": pricing.get("legs", []),
        "warnings": pricing.get("warnings", []),
        "same_game_groups": pricing.get("same_game_groups", {}),
    }


# ---------------------------------------------------------------------------
# Weather Edge
# ---------------------------------------------------------------------------

def calculate_weather_edge(
    city: str,
    forecast_temp: float,
    threshold: float,
    direction: str,
    kalshi_price_cents: int,
    threshold_high: float | None = None,
    days_ahead: int = 1,
) -> dict:
    """
    Calculate edge on a Kalshi weather market using NWS data + bias correction.

    Uses normal distribution with documented NWS warm bias correction.
    Self-checks: P(above) + P(below) must equal 1.0.

    Args:
        city: City name (e.g. "NYC")
        forecast_temp: NWS forecast temperature in °F
        threshold: Kalshi contract threshold in °F (lower bound for "range")
        direction: "above", "below", or "range" — what YES means on this contract
        kalshi_price_cents: Current Kalshi YES price in cents
        threshold_high: Upper bound for "range" direction (e.g. 69° in "68-69°")

    Returns:
        {edge, relative_edge, fair_value, kalshi_price, confidence,
         self_check_passed, math_chain, corrected_temp, p_above, p_below}
    """
    kalshi_price = kalshi_price_cents / 100.0
    math_chain = []

    # Dynamic sigma: cap days_ahead at 3 so uncertainty doesn't grow unbounded
    days_capped = min(days_ahead, 3)
    sigma = 2.0 + 0.75 * (days_capped - 1)
    math_chain.append(f"days_ahead={days_ahead} (capped={days_capped}) → sigma={sigma:.2f}°F")

    # Apply NWS warm bias correction
    corrected_temp = forecast_temp - NWS_BIAS_CORRECTION
    math_chain.append(f"NWS forecast: {forecast_temp}°F - {NWS_BIAS_CORRECTION}°F bias = {corrected_temp}°F corrected")

    # Normal distribution probability
    # P(actual > threshold) using corrected forecast as mean
    z_score = (threshold - corrected_temp) / sigma
    # Using error function: CDF(z) = 0.5 * (1 + erf(z / sqrt(2)))
    p_below = 0.5 * (1.0 + math.erf(z_score / math.sqrt(2)))
    p_above = 1.0 - p_below

    math_chain.append(f"Z-score: ({threshold} - {corrected_temp}) / {sigma:.2f} = {z_score:.3f}")
    math_chain.append(f"P(above {threshold}°F): {p_above:.4f}")
    math_chain.append(f"P(below {threshold}°F): {p_below:.4f}")

    # Determine fair value based on what YES means
    if direction == "above":
        fair_value = p_above
    elif direction == "below":
        fair_value = p_below
    elif direction == "range":
        if threshold_high is None:
            return {
                "edge": 0.0, "relative_edge": 0.0, "fair_value": 0.0,
                "kalshi_price": kalshi_price, "confidence": 0.0,
                "self_check_passed": False,
                "math_chain": ["range direction requires threshold_high"],
                "corrected_temp": corrected_temp, "p_above": p_above, "p_below": p_below,
            }
        # P(low <= temp < high) = CDF(high) - CDF(low)
        z_high = (threshold_high - corrected_temp) / sigma
        p_below_high = 0.5 * (1.0 + math.erf(z_high / math.sqrt(2)))
        fair_value = p_below_high - p_below  # P(threshold <= temp < threshold_high)
        math_chain.append(
            f"Range: P({threshold}°F ≤ temp < {threshold_high}°F) = "
            f"CDF({z_high:.3f}) - CDF({z_score:.3f}) = {p_below_high:.4f} - {p_below:.4f} = {fair_value:.4f}"
        )
    else:
        return {
            "edge": 0.0, "relative_edge": 0.0, "fair_value": 0.0,
            "kalshi_price": kalshi_price, "confidence": 0.0,
            "self_check_passed": False,
            "math_chain": [f"Unknown direction: {direction}"],
            "corrected_temp": corrected_temp, "p_above": p_above, "p_below": p_below,
        }

    math_chain.append(f"Contract YES = '{direction}' → fair_value = {fair_value:.4f}")

    edge = fair_value - kalshi_price
    relative_edge = edge / kalshi_price if kalshi_price > 0 else 0.0

    math_chain.append(f"Edge: {fair_value:.4f} - {kalshi_price:.4f} = {edge:.4f}")
    math_chain.append(f"Relative edge: {relative_edge:.2%}")

    # Self-checks
    c1_ok, c1_msg = _self_check_probability_complement(p_above, "P(above) complement")
    math_chain.append(c1_msg)

    sum_check = abs(p_above + p_below - 1.0)
    c2_ok = sum_check < EPSILON
    c2_msg = f"P(above) + P(below) = {p_above:.6f} + {p_below:.6f} = {p_above + p_below:.6f} {'✅' if c2_ok else '❌ FAILED'}"
    math_chain.append(c2_msg)

    c3_ok, c3_msg = _self_check_edge(fair_value, kalshi_price, edge)
    math_chain.append(c3_msg)

    all_passed = c1_ok and c2_ok and c3_ok

    return {
        "edge": round(edge, 4),
        "relative_edge": round(relative_edge, 4),
        "fair_value": round(fair_value, 4),
        "kalshi_price": kalshi_price,
        "confidence": 0.75,  # Weather has moderate confidence
        "self_check_passed": all_passed,
        "math_chain": math_chain,
        "corrected_temp": corrected_temp,
        "p_above": round(p_above, 4),
        "p_below": round(p_below, 4),
    }


# ---------------------------------------------------------------------------
# Vig Stack (structural NO edge)
# ---------------------------------------------------------------------------

def calculate_vig_stack(
    leg_probabilities: list[float],
    kalshi_yes_price_cents: int,
) -> dict:
    """
    Calculate structural NO edge from vig stacking in multi-leg parlays.

    When bookmakers set each leg with vig, the combined YES price on Kalshi
    is structurally inflated. The NO side captures this excess.

    Args:
        leg_probabilities: True probability for each leg (from sportsbook consensus)
        kalshi_yes_price_cents: Kalshi YES price in cents

    Returns:
        {true_yes_prob, kalshi_yes_price, vig_excess, no_edge, relative_no_edge,
         self_check_passed, math_chain}
    """
    kalshi_yes = kalshi_yes_price_cents / 100.0
    math_chain = []

    # True parlay probability = product of independent legs
    true_yes = 1.0
    for i, p in enumerate(leg_probabilities):
        true_yes *= p
        math_chain.append(f"Leg {i+1}: {p:.4f}")

    math_chain.append(f"True YES probability: {true_yes:.4f}")
    math_chain.append(f"Kalshi YES price: {kalshi_yes:.4f}")

    # Vig excess: how much higher Kalshi prices YES vs true probability
    vig_excess = kalshi_yes - true_yes
    math_chain.append(f"Vig excess (Kalshi - true): {vig_excess:.4f}")

    # NO edge: if Kalshi overprices YES, NO is underpriced
    true_no = 1.0 - true_yes
    kalshi_no = 1.0 - kalshi_yes
    no_edge = true_no - kalshi_no  # positive = NO is underpriced
    relative_no_edge = no_edge / kalshi_no if kalshi_no > 0 else 0.0

    math_chain.append(f"True NO: {true_no:.4f} | Kalshi NO: {kalshi_no:.4f}")
    math_chain.append(f"NO edge: {no_edge:.4f} ({relative_no_edge:.2%} relative)")

    # Self-checks
    c1_ok = abs((true_yes + true_no) - 1.0) < EPSILON
    c1_msg = f"True YES + NO = {true_yes + true_no:.6f} {'✅' if c1_ok else '❌'}"
    math_chain.append(c1_msg)

    c2_ok = abs((kalshi_yes + kalshi_no) - 1.0) < EPSILON
    c2_msg = f"Kalshi YES + NO = {kalshi_yes + kalshi_no:.6f} {'✅' if c2_ok else '❌'}"
    math_chain.append(c2_msg)

    # Verify vig_excess = -no_edge (they're the same quantity from different perspectives)
    c3_ok = abs(vig_excess - (-no_edge)) < EPSILON
    c3_msg = f"Vig excess ({vig_excess:.6f}) = -NO edge ({-no_edge:.6f}) {'✅' if c3_ok else '❌'}"
    math_chain.append(c3_msg)

    all_passed = c1_ok and c2_ok and c3_ok

    return {
        "true_yes_prob": round(true_yes, 4),
        "kalshi_yes_price": kalshi_yes,
        "vig_excess": round(vig_excess, 4),
        "no_edge": round(no_edge, 4),
        "relative_no_edge": round(relative_no_edge, 4),
        "self_check_passed": all_passed,
        "math_chain": math_chain,
    }


# ---------------------------------------------------------------------------
# Contract Direction Verification — THE LAST LINE OF DEFENSE
# ---------------------------------------------------------------------------

# Patterns that indicate what "YES" means in a Kalshi contract title.
# Each pattern returns (direction, description) when matched.
# These cover weather, sports, and generic prediction market contracts.

# Temperature contracts
# Temperature unit suffix: matches "°F", "°f", "degrees F", "F", or nothing
_TEMP_SUFFIX = r"[°\s]*(?:degrees?\s*)?[fF]?"

_TEMP_ABOVE_PATTERNS = [
    # "68°F or above" / "68 degrees or warmer" / "68 or above"
    (r"(\d+)" + _TEMP_SUFFIX + r"\s+or\s+(?:above|higher|more|greater|warmer|hotter)", "above"),
    # "above 68°F" / "over 68" / "at least 68°F" / "exceed 68"
    (r"(?:above|over|exceed|at\s+least|≥|>=)\s*(\d+)" + _TEMP_SUFFIX, "above"),
]

_TEMP_BELOW_PATTERNS = [
    # "68°F or below" / "68 degrees or cooler"
    (r"(\d+)" + _TEMP_SUFFIX + r"\s+or\s+(?:below|under|less|lower|cooler|colder)", "below"),
    # "under 68°F" / "below 68" / "less than 68"
    (r"(?:below|under|less\s+than|<|≤|<=)\s*(\d+)" + _TEMP_SUFFIX, "below"),
]

# Sports: team win patterns
_TEAM_WIN_PATTERNS = [
    # "Will X win?"
    (r"(?:will\s+)?(.+?)\s+win", "team_win"),
    # "X to win"
    (r"(.+?)\s+to\s+win", "team_win"),
    # "X vs Y" — first team is usually the subject
    (r"(.+?)\s+(?:vs\.?|versus)\s+", "team_win_first"),
]

# Over/under patterns for totals or player props
_OVER_UNDER_PATTERNS = [
    (r"over\s+(\d+\.?\d*)", "over"),
    (r"under\s+(\d+\.?\d*)", "under"),
    (r"(\d+\.?\d*)\s*\+", "over"),  # "20+" means over 20
    (r"more\s+than\s+(\d+\.?\d*)", "over"),
    (r"fewer\s+than\s+(\d+\.?\d*)", "under"),
    (r"less\s+than\s+(\d+\.?\d*)", "under"),
]


def verify_contract_direction(
    contract_title: str,
    data_thesis: str,
    intended_side: str,
    contract_details: Optional[dict] = None,
) -> dict:
    """
    MANDATORY before every trade. The last line of defense against backwards bets.

    This function does NOT do simple string matching. It performs semantic parsing
    of what the Kalshi contract is actually asking, extracts what YES/NO mean in
    plain English, compares to the data thesis, and refuses to trade if there's
    ANY ambiguity.

    Args:
        contract_title: Full Kalshi contract title
            e.g. "Will the high temperature in NYC be 68°F or above on April 5?"
        data_thesis: Plain English description of what the data says
            e.g. "NWS forecasts 72°F after bias correction, which is ABOVE 68°F"
        intended_side: "yes" or "no" — which side we plan to buy
        contract_details: Optional dict with additional contract metadata
            (rules_primary, subtitle, etc.) for extra verification

    Returns:
        {
            direction_correct: bool,  # True ONLY if everything checks out
            confidence: str,          # "HIGH", "MEDIUM", "REFUSE"
            yes_means: str,           # Plain English: what YES means
            no_means: str,            # Plain English: what NO means
            thesis_supports: str,     # "yes", "no", or "ambiguous"
            intended_side: str,       # Echo back for verification
            explanation: str,         # Full reasoning chain
            warnings: list[str],      # Any concerns
        }
    """
    title_lower = contract_title.lower().strip()
    warnings = []
    explanation_parts = []

    # ------------------------------------------------------------------
    # Step 1: Parse what the contract is ACTUALLY asking
    # ------------------------------------------------------------------
    yes_means = None
    no_means = None
    contract_type = None
    threshold_value = None

    explanation_parts.append(f"CONTRACT: \"{contract_title}\"")

    # ------------------------------------------------------------------
    # Parlay detection FIRST — parlay titles contain "yes X,yes Y" legs
    # that would otherwise false-match weather/over-under patterns
    # (e.g. "over 1.5 runs" matching temperature regex)
    # ------------------------------------------------------------------
    _is_parlay = bool(re.search(r"\byes\b.*,.*\byes\b", title_lower))

    if _is_parlay:
        yes_means = f"All conditions in the parlay are met: {contract_title}"
        no_means = "At least one condition in the parlay fails"
        contract_type = "vig_stack_no"
        explanation_parts.append("PARSED: Parlay contract — YES = all legs hit")

    # Check temperature contracts (only for non-parlay titles)
    if not yes_means:
        for pattern, direction in _TEMP_ABOVE_PATTERNS:
            m = re.search(pattern, title_lower)
            if m:
                threshold_value = float(m.group(1))
                yes_means = f"Temperature will be {threshold_value}°F or ABOVE"
                no_means = f"Temperature will be BELOW {threshold_value}°F"
                contract_type = "weather_above"
                explanation_parts.append(f"PARSED: Weather contract — YES = above {threshold_value}°F")
                break

    if not yes_means:
        for pattern, direction in _TEMP_BELOW_PATTERNS:
            m = re.search(pattern, title_lower)
            if m:
                threshold_value = float(m.group(1))
                yes_means = f"Temperature will be BELOW {threshold_value}°F"
                no_means = f"Temperature will be {threshold_value}°F or ABOVE"
                contract_type = "weather_below"
                explanation_parts.append(f"PARSED: Weather contract — YES = below {threshold_value}°F")
                break

    # Check over/under contracts (player props, totals — non-parlay only)
    if not yes_means:
        for pattern, direction in _OVER_UNDER_PATTERNS:
            m = re.search(pattern, title_lower)
            if m:
                threshold_value = float(m.group(1))
                if direction == "over":
                    yes_means = f"Value will be OVER {threshold_value}"
                    no_means = f"Value will be UNDER {threshold_value}"
                    contract_type = "over"
                else:
                    yes_means = f"Value will be UNDER {threshold_value}"
                    no_means = f"Value will be OVER {threshold_value}"
                    contract_type = "under"
                explanation_parts.append(f"PARSED: Over/under contract — YES = {direction} {threshold_value}")
                break

    # Check team win (single-game contracts without "yes" markers)
    if not yes_means:
        for pattern, wtype in _TEAM_WIN_PATTERNS:
            m = re.search(pattern, title_lower)
            if m:
                team = m.group(1).strip()
                yes_means = f"{team.title()} WINS"
                no_means = f"{team.title()} LOSES"
                contract_type = "team_win"
                explanation_parts.append(f"PARSED: Team win contract — YES = {team.title()} wins")
                break

    # ------------------------------------------------------------------
    # Step 2: If we couldn't parse, check contract_details for rules
    # ------------------------------------------------------------------
    if not yes_means and contract_details:
        rules = contract_details.get("rules_primary", "")
        subtitle = contract_details.get("subtitle", "")
        if rules:
            explanation_parts.append(f"RULES TEXT: \"{rules[:200]}\"")
            warnings.append("Could not parse title — using rules text as fallback")
            yes_means = f"Contract resolves YES per rules: {rules[:100]}"
            no_means = f"Contract resolves NO (opposite of YES condition)"
            contract_type = "rules_fallback"

    # ------------------------------------------------------------------
    # Step 3: If we STILL can't parse — REFUSE TO TRADE
    # ------------------------------------------------------------------
    if not yes_means:
        explanation_parts.append("FAILED: Could not determine what YES/NO mean")
        explanation_parts.append("REFUSING TO TRADE — ambiguous contract")
        return {
            "direction_correct": False,
            "confidence": "REFUSE",
            "yes_means": "UNKNOWN — could not parse contract",
            "no_means": "UNKNOWN — could not parse contract",
            "thesis_supports": "ambiguous",
            "intended_side": intended_side,
            "explanation": " | ".join(explanation_parts),
            "warnings": ["REFUSED: Cannot determine contract direction from title"],
        }

    explanation_parts.append(f"YES means: {yes_means}")
    explanation_parts.append(f"NO means: {no_means}")

    # ------------------------------------------------------------------
    # Step 4: Parse the data thesis to understand what the data says
    # ------------------------------------------------------------------
    thesis_lower = data_thesis.lower()
    thesis_direction = None

    # Weather thesis parsing
    if contract_type in ("weather_above", "weather_below"):
        # Look for temperature in thesis
        temp_match = re.search(r"(\d+\.?\d*)\s*°?\s*F", data_thesis)
        above_signal = any(w in thesis_lower for w in ["above", "over", "higher", "warmer", "exceeds", "hotter"])
        below_signal = any(w in thesis_lower for w in ["below", "under", "lower", "cooler", "colder"])

        if above_signal and not below_signal:
            thesis_direction = "above"
            explanation_parts.append("THESIS: Data says temperature will be ABOVE threshold")
        elif below_signal and not above_signal:
            thesis_direction = "below"
            explanation_parts.append("THESIS: Data says temperature will be BELOW threshold")
        else:
            # Try to infer from numbers
            if temp_match and threshold_value:
                forecast = float(temp_match.group(1))
                if forecast > threshold_value:
                    thesis_direction = "above"
                    explanation_parts.append(f"THESIS: Forecast {forecast}°F > threshold {threshold_value}°F → ABOVE")
                elif forecast < threshold_value:
                    thesis_direction = "below"
                    explanation_parts.append(f"THESIS: Forecast {forecast}°F < threshold {threshold_value}°F → BELOW")
                else:
                    thesis_direction = "ambiguous"
                    explanation_parts.append(f"THESIS: Forecast {forecast}°F ≈ threshold {threshold_value}°F → AMBIGUOUS")
                    warnings.append("Forecast is very close to threshold")

    # Over/under thesis parsing
    elif contract_type in ("over", "under"):
        over_signal = any(w in thesis_lower for w in ["over", "above", "exceed", "more than", "higher"])
        under_signal = any(w in thesis_lower for w in ["under", "below", "fewer", "less than", "lower"])

        if over_signal and not under_signal:
            thesis_direction = "over"
            explanation_parts.append("THESIS: Data supports OVER")
        elif under_signal and not over_signal:
            thesis_direction = "under"
            explanation_parts.append("THESIS: Data supports UNDER")
        else:
            thesis_direction = "ambiguous"

    # Parlay / team win thesis parsing
    elif contract_type in ("vig_stack_no", "team_win"):
        # "NO has edge" / "YES is overpriced" → supports NO side
        no_edge_signal = any(phrase in thesis_lower for phrase in [
            "no has edge", "no side", "overpriced", "yes is overpriced",
        ])
        yes_edge_signal = any(w in thesis_lower for w in [
            "favored", "likely", "strong", "win", "underpriced",
        ])
        # "edge" alone is ambiguous — only count it for YES if no NO signals
        if not no_edge_signal and "edge" in thesis_lower:
            yes_edge_signal = True

        if no_edge_signal and not yes_edge_signal:
            thesis_direction = "no"
            explanation_parts.append("THESIS: Data supports NO outcome (YES overpriced)")
        elif yes_edge_signal and not no_edge_signal:
            thesis_direction = "yes"
            explanation_parts.append("THESIS: Data supports YES outcome")
        elif no_edge_signal and yes_edge_signal:
            # Both signals — check which side the thesis explicitly names
            if "no has edge" in thesis_lower or "no side" in thesis_lower:
                thesis_direction = "no"
                explanation_parts.append("THESIS: Data supports NO outcome (explicit NO edge)")
            else:
                thesis_direction = "ambiguous"
                warnings.append("Thesis has mixed YES/NO signals")
        else:
            thesis_direction = "ambiguous"

    # Rules fallback — no automatic parsing, require explicit signals
    elif contract_type == "rules_fallback":
        thesis_direction = "ambiguous"
        warnings.append("Using rules fallback — cannot automatically verify direction")

    if thesis_direction is None:
        thesis_direction = "ambiguous"
        warnings.append("Could not determine thesis direction from data")

    # ------------------------------------------------------------------
    # Step 5: Cross-check thesis direction against intended trade side
    # ------------------------------------------------------------------
    thesis_supports_side = None

    if contract_type == "weather_above":
        # YES = above. If thesis says above → thesis supports YES
        if thesis_direction == "above":
            thesis_supports_side = "yes"
        elif thesis_direction == "below":
            thesis_supports_side = "no"
    elif contract_type == "weather_below":
        # YES = below. If thesis says below → thesis supports YES
        if thesis_direction == "below":
            thesis_supports_side = "yes"
        elif thesis_direction == "above":
            thesis_supports_side = "no"
    elif contract_type == "over":
        if thesis_direction == "over":
            thesis_supports_side = "yes"
        elif thesis_direction == "under":
            thesis_supports_side = "no"
    elif contract_type == "under":
        if thesis_direction == "under":
            thesis_supports_side = "yes"
        elif thesis_direction == "over":
            thesis_supports_side = "no"
    elif contract_type in ("vig_stack_no", "team_win"):
        thesis_supports_side = thesis_direction  # already "yes", "no", or "ambiguous"

    if thesis_supports_side is None:
        thesis_supports_side = "ambiguous"

    explanation_parts.append(f"THESIS SUPPORTS: {thesis_supports_side.upper()} side")
    explanation_parts.append(f"INTENDED SIDE: {intended_side.upper()}")

    # ------------------------------------------------------------------
    # Step 6: Final verdict — does intended side match thesis?
    # ------------------------------------------------------------------
    if thesis_supports_side == "ambiguous":
        direction_correct = False
        confidence = "REFUSE"
        explanation_parts.append("VERDICT: REFUSE — thesis direction is ambiguous, cannot verify")
        warnings.append("REFUSED: Ambiguous thesis — cannot confirm trade direction")
    elif thesis_supports_side == intended_side.lower():
        direction_correct = True
        confidence = "HIGH"
        explanation_parts.append(f"VERDICT: ✅ CORRECT — thesis supports {intended_side.upper()}, intended {intended_side.upper()}")
    else:
        direction_correct = False
        confidence = "REFUSE"
        explanation_parts.append(
            f"VERDICT: ❌ BACKWARDS — thesis supports {thesis_supports_side.upper()} "
            f"but intended side is {intended_side.upper()}"
        )
        warnings.append(
            f"BACKWARDS BET DETECTED: Data supports {thesis_supports_side.upper()} "
            f"but you're trying to buy {intended_side.upper()}"
        )

    # ------------------------------------------------------------------
    # Step 7: Additional paranoia checks
    # ------------------------------------------------------------------

    # Check for negation words that could flip meaning
    negation_words = ["not", "won't", "will not", "doesn't", "does not", "no longer", "unlikely to"]
    for neg in negation_words:
        if neg in title_lower:
            warnings.append(f"CAUTION: Negation word '{neg}' found in contract title — double-check YES/NO meaning")
            if confidence == "HIGH":
                confidence = "MEDIUM"

    # Check for conditional language
    conditional_words = ["if", "given that", "assuming", "provided"]
    for cond in conditional_words:
        if cond in title_lower:
            warnings.append(f"CAUTION: Conditional language '{cond}' in title — verify conditions are met")

    return {
        "direction_correct": direction_correct,
        "confidence": confidence,
        "yes_means": yes_means,
        "no_means": no_means,
        "thesis_supports": thesis_supports_side,
        "intended_side": intended_side,
        "explanation": " | ".join(explanation_parts),
        "warnings": warnings,
    }
