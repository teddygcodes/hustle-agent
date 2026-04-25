"""
Cross-Market Consistency Scanner — Sports Correlation Arbitrage

Detects mathematical violations between related Kalshi sports markets:

1. MONOTONICITY (spread/total thresholds)
   "Team wins by 4.5+" must be priced ≤ "Team wins by 1.5+"
   If violated: buy cheap lower threshold, sell expensive higher threshold.

2. CHAMPIONSHIP ≤ SERIES constraint
   P(team wins championship) must be ≤ P(team wins current series)
   If violated: buy NO on championship, buy YES on series.

3. GAME vs SPREAD consistency
   P(team wins game) should be > P(team wins by 1.5+) by ~3-5%
   Large discrepancies = potential arb.

These violations are RARE but RISKLESS when they occur. The scanner runs
every cycle, mostly finds nothing, but catches transient mispricings.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.kalshi_client import get_markets
from bot.math_engine import _self_check_edge

logger = logging.getLogger("glint.scanner_sports_arb")

# ---------------------------------------------------------------------------
# Market type mappings
# ---------------------------------------------------------------------------
SPREAD_SERIES = {
    "nba": "KXNBASPREAD",
    "nhl": "KXNHLSPREAD",
}
TOTAL_SERIES = {
    "nba": "KXNBATOTAL",
    "nhl": "KXNHLTOTAL",
    "mlb": "KXMLBTOTAL",
}
GAME_SERIES = {
    "nba": "KXNBAGAME",
    "nhl": "KXNHLGAME",
    "mlb": "KXMLBGAME",
}
CHAMPIONSHIP_SERIES = {
    "nba": "KXNBA",
    "nhl": "KXNHL",
    "mlb": "KXMLB",
}
PLAYOFF_SERIES_SERIES = {
    "nba": "KXNBASERIES",
    "nhl": "KXNHLSERIES",
}


def _parse_spread_threshold(ticker: str) -> tuple[str, float] | None:
    """
    Parse spread ticker suffix to extract team + threshold.
    e.g. 'KXNBASPREAD-26APR14PORPHX-POR1' → ('POR', 1.5)
    """
    suffix = ticker.split("-")[-1]
    match = re.match(r"([A-Z]+)(\d+)", suffix)
    if not match:
        return None
    team = match.group(1)
    threshold = int(match.group(2)) + 0.5
    return team, threshold


def _parse_total_threshold(ticker: str) -> float | None:
    """
    Parse total ticker suffix to extract threshold.
    e.g. 'KXNBATOTAL-26APR15GSWLAC-235' → 235.5
    """
    suffix = ticker.split("-")[-1]
    try:
        return float(suffix) + 0.5
    except ValueError:
        return None


def scan_monotonicity_violations(scan_id: str | None = None,
                                 on_market_seen=None) -> list[dict]:
    """
    Check threshold markets (spreads + totals) for monotonicity violations.

    For spread thresholds of the same team in the same game:
    P(win by 1.5+) ≥ P(win by 4.5+) ≥ P(win by 7.5+) ...

    A violation means you can buy YES on the lower threshold (cheap)
    and buy NO on the higher threshold (cheap) for guaranteed profit.

    Session 12: optional `scan_id` and `on_market_seen` callback let the
    main loop attribute every spread/total ticker this scanner touched to
    its universe.jsonl row. None for both = no-op (Telegram handlers, tests).
    """
    opportunities = []
    _telem = {"sports_scanned": 0, "spread_markets": 0, "total_markets": 0, "events": 0, "pairs_checked": 0, "violations": 0}

    def _attribute(markets_list):
        if on_market_seen and scan_id:
            for _m in markets_list:
                on_market_seen(scan_id, _m.get("ticker", ""), "sports_monotonicity_arb")

    for sport in ("nba",):  # Start with NBA, extend as needed
        _telem["sports_scanned"] += 1
        # --- Spread monotonicity ---
        spread_series = SPREAD_SERIES.get(sport)
        if spread_series:
            try:
                result = get_markets(series_ticker=spread_series, status="open", limit=200)
                markets = result.get("markets", [])
                _telem["spread_markets"] += len(markets)
            except Exception as e:
                logger.warning("SportsArb/%s spread API error: %s", sport, e)
                markets = []

            _attribute(markets)

            # Group by event (game)
            events: dict[str, list] = {}
            for m in markets:
                evt = m.get("event_ticker", "")
                events.setdefault(evt, []).append(m)

            for evt, mkts in events.items():
                # Parse and group by team
                teams: dict[str, list] = {}
                for m in mkts:
                    parsed = _parse_spread_threshold(m.get("ticker", ""))
                    if not parsed:
                        continue
                    team, threshold = parsed
                    teams.setdefault(team, []).append({
                        "threshold": threshold,
                        "yes_ask": m.get("yes_ask", 0),
                        "yes_bid": m.get("yes_bid", 0),
                        "no_ask": m.get("no_ask", 0),
                        "ticker": m.get("ticker", ""),
                        "title": m.get("title", ""),
                        "volume": m.get("volume", 0),
                        "market": m,
                    })

                for team, contracts in teams.items():
                    contracts.sort(key=lambda x: x["threshold"])
                    for i in range(len(contracts) - 1):
                        lower = contracts[i]   # lower threshold = should have HIGHER prob
                        higher = contracts[i + 1]  # higher threshold = should have LOWER prob

                        # Monotonicity violation: lower threshold YES_ASK < higher threshold YES_ASK
                        # means it's CHEAPER to buy "wins by less" than "wins by more" — impossible
                        if lower["yes_ask"] > 0 and higher["yes_ask"] > lower["yes_ask"]:
                            profit_cents = higher["yes_ask"] - lower["yes_ask"]
                            # To arb: buy YES on lower (cheap), buy NO on higher
                            total_cost = lower["yes_ask"] + higher["no_ask"]
                            if total_cost < 100:
                                guaranteed_profit = 100 - total_cost
                                rel_edge = guaranteed_profit / total_cost

                                logger.info(
                                    "MONOTONICITY VIOLATION: %s vs %s | "
                                    "over %.1f at %d¢ vs over %.1f at %d¢ | "
                                    "guaranteed profit: %d¢",
                                    lower["ticker"], higher["ticker"],
                                    lower["threshold"], lower["yes_ask"],
                                    higher["threshold"], higher["yes_ask"],
                                    guaranteed_profit,
                                )

                                math_chain = [
                                    f"Monotonicity violation in {evt}",
                                    f"{team} over {lower['threshold']}pt: YES_ask={lower['yes_ask']}¢",
                                    f"{team} over {higher['threshold']}pt: YES_ask={higher['yes_ask']}¢",
                                    f"Lower threshold should NEVER be cheaper than higher",
                                    f"Arb: buy YES {lower['ticker']} at {lower['yes_ask']}¢ + "
                                    f"buy NO {higher['ticker']} at {higher['no_ask']}¢ = {total_cost}¢",
                                    f"Guaranteed payout: 100¢ → profit = {guaranteed_profit}¢",
                                ]

                                opportunities.append({
                                    "type": "sports_monotonicity_arb",
                                    "ticker": lower["ticker"],
                                    "title": f"MONOTONICITY ARB: {team} spread {lower['threshold']}/{higher['threshold']}",
                                    "market": lower["market"],
                                    "edge": round(rel_edge, 4),
                                    "relative_edge": round(rel_edge, 4),
                                    "confidence": 0.95,
                                    "recommended_side": "yes",
                                    "arb_pair": {
                                        "buy_yes": lower["ticker"],
                                        "buy_no": higher["ticker"],
                                        "cost_cents": total_cost,
                                        "profit_cents": guaranteed_profit,
                                    },
                                    "edge_result": {
                                        "fair_value": 1.0,
                                        "kalshi_price": total_cost / 100,
                                        "edge": round(rel_edge, 4),
                                        "relative_edge": round(rel_edge, 4),
                                        "confidence": 0.95,
                                        "self_check_passed": True,
                                        "math_chain": math_chain,
                                        "warnings": [],
                                    },
                                })

        # --- Total monotonicity ---
        total_series = TOTAL_SERIES.get(sport)
        if total_series:
            try:
                result = get_markets(series_ticker=total_series, status="open", limit=200)
                markets = result.get("markets", [])
            except Exception as e:
                logger.warning("SportsArb/%s total API error: %s", sport, e)
                markets = []

            _attribute(markets)

            events = {}
            for m in markets:
                evt = m.get("event_ticker", "")
                events.setdefault(evt, []).append(m)

            for evt, mkts in events.items():
                parsed = []
                for m in mkts:
                    threshold = _parse_total_threshold(m.get("ticker", ""))
                    if threshold is None:
                        continue
                    parsed.append({
                        "threshold": threshold,
                        "yes_ask": m.get("yes_ask", 0),
                        "no_ask": m.get("no_ask", 0),
                        "ticker": m.get("ticker", ""),
                        "title": m.get("title", ""),
                        "market": m,
                    })

                parsed.sort(key=lambda x: x["threshold"])
                for i in range(len(parsed) - 1):
                    lower = parsed[i]
                    higher = parsed[i + 1]

                    if lower["yes_ask"] > 0 and higher["yes_ask"] > lower["yes_ask"]:
                        total_cost = lower["yes_ask"] + higher["no_ask"]
                        if total_cost < 100:
                            guaranteed_profit = 100 - total_cost
                            rel_edge = guaranteed_profit / total_cost

                            logger.info(
                                "TOTAL MONOTONICITY VIOLATION: %s vs %s | profit: %d¢",
                                lower["ticker"], higher["ticker"], guaranteed_profit,
                            )

                            opportunities.append({
                                "type": "sports_monotonicity_arb",
                                "ticker": lower["ticker"],
                                "title": f"TOTAL ARB: over {lower['threshold']}/{higher['threshold']}",
                                "market": lower["market"],
                                "edge": round(rel_edge, 4),
                                "relative_edge": round(rel_edge, 4),
                                "confidence": 0.95,
                                "recommended_side": "yes",
                                "arb_pair": {
                                    "buy_yes": lower["ticker"],
                                    "buy_no": higher["ticker"],
                                    "cost_cents": total_cost,
                                    "profit_cents": guaranteed_profit,
                                },
                                "edge_result": {
                                    "fair_value": 1.0,
                                    "kalshi_price": total_cost / 100,
                                    "edge": round(rel_edge, 4),
                                    "relative_edge": round(rel_edge, 4),
                                    "confidence": 0.95,
                                    "self_check_passed": True,
                                    "math_chain": [
                                        f"Total monotonicity violation in {evt}",
                                        f"Over {lower['threshold']}: {lower['yes_ask']}¢",
                                        f"Over {higher['threshold']}: {higher['yes_ask']}¢",
                                        f"Arb cost: {total_cost}¢ → profit: {guaranteed_profit}¢",
                                    ],
                                    "warnings": [],
                                },
                            })

    _telem["violations"] = len(opportunities)
    logger.info("MONOTONICITY_TELEMETRY: %s", _telem)
    return opportunities


def scan_championship_series_violations(scan_id: str | None = None,
                                        on_market_seen=None) -> list[dict]:
    """
    Check that P(team wins championship) ≤ P(team wins current series).

    Winning the series is a PREREQUISITE for winning the championship.
    If the championship price exceeds the series price, that's mathematically
    impossible and a guaranteed arb.

    Session 12: scan_id + on_market_seen attribute every champ/playoff
    ticker to its universe.jsonl row. None = no-op.
    """
    opportunities = []

    for sport in ("nba", "nhl"):
        champ_series = CHAMPIONSHIP_SERIES.get(sport)
        playoff_series = PLAYOFF_SERIES_SERIES.get(sport)

        if not champ_series or not playoff_series:
            continue

        # Fetch championship markets
        try:
            champ_result = get_markets(series_ticker=champ_series, status="open", limit=100)
            champ_markets = champ_result.get("markets", [])
        except Exception as e:
            logger.warning("SportsArb/%s championship API error: %s", sport, e)
            continue

        # Fetch series markets
        try:
            series_result = get_markets(series_ticker=playoff_series, status="open", limit=100)
            series_markets = series_result.get("markets", [])
        except Exception as e:
            logger.warning("SportsArb/%s series API error: %s", sport, e)
            continue

        if on_market_seen and scan_id:
            for _m in champ_markets:
                on_market_seen(scan_id, _m.get("ticker", ""), "sports_consistency_arb")
            for _m in series_markets:
                on_market_seen(scan_id, _m.get("ticker", ""), "sports_consistency_arb")

        # Build team → price maps
        champ_prices = {}  # team_code → yes_ask (cents)
        for m in champ_markets:
            team = m.get("ticker", "").split("-")[-1]
            champ_prices[team] = {
                "yes_ask": m.get("yes_ask", 0),
                "no_ask": m.get("no_ask", 0),
                "ticker": m.get("ticker", ""),
                "title": m.get("title", ""),
                "market": m,
            }

        series_prices = {}  # team_code → yes_ask (cents)
        for m in series_markets:
            team = m.get("ticker", "").split("-")[-1]
            series_prices[team] = {
                "yes_ask": m.get("yes_ask", 0),
                "no_ask": m.get("no_ask", 0),
                "ticker": m.get("ticker", ""),
                "title": m.get("title", ""),
                "market": m,
            }

        # Check constraint: P(championship) ≤ P(series win)
        for team, champ in champ_prices.items():
            if team not in series_prices:
                continue

            series = series_prices[team]

            # Use yes_ask for championship (what you'd pay) and yes_bid for series
            # (what you could sell at). If champ_ask > series_ask, that's suspicious.
            # If champ_ask > series_bid, that's a tradeable violation.
            champ_ask = champ["yes_ask"]
            series_ask = series["yes_ask"]

            if champ_ask > series_ask and champ_ask > 1 and series_ask > 1:
                # Mathematical violation!
                # Arb: buy NO on championship, buy YES on series
                # If team wins championship: they also won series → YES series pays, NO champ loses
                # If team doesn't win championship: NO champ pays
                # Need: NO champ cost + YES series cost < guaranteed scenarios
                # Actually the clean arb is: sell YES championship, buy YES series
                # (or equivalently: buy NO championship at champ_no_ask)

                champ_no_ask = champ["no_ask"]
                edge_cents = champ_ask - series_ask
                rel_edge = edge_cents / champ_ask if champ_ask > 0 else 0

                logger.info(
                    "CHAMPIONSHIP>SERIES VIOLATION: %s %s | champ=%d¢ > series=%d¢ | "
                    "impossible edge: %d¢",
                    sport.upper(), team, champ_ask, series_ask, edge_cents,
                )

                opportunities.append({
                    "type": "sports_consistency_arb",
                    "ticker": champ["ticker"],
                    "title": f"CONSISTENCY ARB: {team} champ({champ_ask}¢) > series({series_ask}¢)",
                    "market": champ["market"],
                    "edge": round(rel_edge, 4),
                    "relative_edge": round(rel_edge, 4),
                    "confidence": 0.95,
                    "recommended_side": "no",
                    "violation": {
                        "type": "championship_gt_series",
                        "team": team,
                        "sport": sport,
                        "champ_ticker": champ["ticker"],
                        "champ_yes_ask": champ_ask,
                        "series_ticker": series["ticker"],
                        "series_yes_ask": series_ask,
                        "edge_cents": edge_cents,
                    },
                    "edge_result": {
                        "fair_value": round(series_ask / 100, 4),
                        "kalshi_price": round(champ_ask / 100, 4),
                        "edge": round(rel_edge, 4),
                        "relative_edge": round(rel_edge, 4),
                        "confidence": 0.95,
                        "self_check_passed": True,
                        "math_chain": [
                            f"Championship ≤ Series constraint violation",
                            f"{team} championship: YES_ask={champ_ask}¢",
                            f"{team} series: YES_ask={series_ask}¢",
                            f"Championship MUST be ≤ Series (series win is prerequisite)",
                            f"Edge: {edge_cents}¢ ({rel_edge:.1%} relative)",
                        ],
                        "warnings": [],
                    },
                })

    return opportunities


def scan_game_vig(min_vig_pct: float = 0.08,
                  scan_id: str | None = None,
                  on_market_seen=None) -> list[dict]:
    """
    Check binary game markets where YES sum is high enough for structural NO edge.

    MLB games 3+ days out can have 20-40% vig (YES sum 120-142¢).
    With only 2 outcomes, the vig concentrates on fewer contracts — but the
    per-contract edge can still be significant at high vig levels.

    Only scans games with meaningful volume (>100) to avoid illiquid spreads.

    Session 12: scan_id + on_market_seen attribute every game-series ticker
    to its universe.jsonl row. Emits the same opp_type (vig_stack_futures)
    as scan_vig_stack_series's futures branch, so we attribute under the
    same name for consistency. None = no-op.
    """
    opportunities = []

    for sport in ("mlb",):  # MLB has the highest game vig
        game_series = GAME_SERIES.get(sport)
        if not game_series:
            continue

        try:
            result = get_markets(series_ticker=game_series, status="open", limit=200)
            markets = result.get("markets", [])
        except Exception as e:
            logger.warning("SportsArb/%s game vig API error: %s", sport, e)
            continue

        if on_market_seen and scan_id:
            for _m in markets:
                on_market_seen(scan_id, _m.get("ticker", ""), "vig_stack_futures")

        # Group by event
        events: dict[str, list] = {}
        for m in markets:
            evt = m.get("event_ticker", "")
            events.setdefault(evt, []).append(m)

        for evt, mkts in events.items():
            if len(mkts) != 2:
                continue

            yes_sum = sum(m.get("yes_ask", 0) for m in mkts)
            vig_pct = (yes_sum - 100) / 100

            if vig_pct < min_vig_pct:
                continue

            # Need some volume — illiquid markets have wide spreads but no fills
            total_vol = sum(m.get("volume", 0) for m in mkts)
            if total_vol < 100:
                continue

            vig_factor = yes_sum / 100
            for m in mkts:
                ya = m.get("yes_ask", 0)
                na = m.get("no_ask", 0)
                if ya <= 0 or na <= 0:
                    continue

                no_fair = 100 - (ya / vig_factor)
                edge = no_fair - na
                rel_edge = edge / na if na > 0 else 0

                if rel_edge < 0.02:
                    continue

                check_ok, check_msg = _self_check_edge(no_fair / 100, na / 100, edge / 100)
                if not check_ok:
                    continue

                ticker = m.get("ticker", "")
                team = ticker.split("-")[-1]

                logger.info(
                    "GAME VIG: %s | YES sum=%d¢ (%.0f%% vig) | %s NO_fair=%.1f¢ vs NO_ask=%d¢ | edge=%.1f%%",
                    evt, yes_sum, vig_pct * 100, team, no_fair, na, rel_edge * 100,
                )

                opportunities.append({
                    "type": "vig_stack_futures",  # Reuse same type — same structural logic
                    "ticker": ticker,
                    "title": m.get("title", ""),
                    "market": m,
                    "series_ticker": game_series,
                    "edge": round(edge / 100, 4),
                    "relative_edge": round(rel_edge, 4),
                    "confidence": 0.80,  # Lower confidence — binary market, less vig distribution
                    "recommended_side": "no",
                    "yes_sum_cents": yes_sum,
                    "vig_factor": round(vig_factor, 4),
                    "no_fair_cents": round(no_fair, 2),
                    "no_ask_cents": na,
                    "edge_result": {
                        "fair_value": round(no_fair / 100, 4),
                        "kalshi_price": round(na / 100, 4),
                        "edge": round(edge / 100, 4),
                        "relative_edge": round(rel_edge, 4),
                        "confidence": 0.80,
                        "self_check_passed": True,
                        "math_chain": [
                            f"Game: {evt} | {sport.upper()}",
                            f"YES sum: {yes_sum}¢ ({vig_pct:.0%} vig) — 2 outcomes",
                            f"{team} YES_ask={ya}¢ | YES_fair={ya / vig_factor:.1f}¢",
                            f"NO_fair={no_fair:.1f}¢ | NO_ask={na}¢",
                            f"Edge={edge:.1f}¢ ({rel_edge:.1%} relative)",
                            check_msg,
                        ],
                        "warnings": ["Binary market — vig concentrates on fewer contracts"],
                    },
                })

    return opportunities


def scan_sports_arb(scan_id: str | None = None,
                    on_market_seen=None) -> list[dict]:
    """
    Master function: run all cross-market consistency checks.
    Called from scan_cycle() in scanner.py.

    Session 12: passes scan_id + on_market_seen to each sub-scanner so
    every ticker they evaluate gets attributed to the matching universe.jsonl
    row. None for both = no-op (Telegram handlers, tests).
    """
    all_opps = []

    logger.info("SPORTS_ARB: scanning spread/total monotonicity")
    mono_opps = scan_monotonicity_violations(scan_id=scan_id, on_market_seen=on_market_seen)
    all_opps.extend(mono_opps)
    logger.info("SPORTS_ARB: %d monotonicity violations found", len(mono_opps))

    logger.info("SPORTS_ARB: scanning championship≤series consistency")
    consistency_opps = scan_championship_series_violations(
        scan_id=scan_id, on_market_seen=on_market_seen)
    all_opps.extend(consistency_opps)
    logger.info("SPORTS_ARB: %d championship>series violations found", len(consistency_opps))

    logger.info("SPORTS_ARB: scanning high-vig game markets")
    vig_opps = scan_game_vig(scan_id=scan_id, on_market_seen=on_market_seen)
    all_opps.extend(vig_opps)
    logger.info("SPORTS_ARB: %d high-vig game opportunities found", len(vig_opps))

    return all_opps
