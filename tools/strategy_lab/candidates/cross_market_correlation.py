"""Cross-market correlation candidate — Session 72 Phase 1 prototype.

Mathematical premise: two markets describing related outcomes for the same
matchup (series-winner vs game-winner during NBA/NHL playoffs) sometimes
diverge in price beyond what is mathematically explainable by current series
state. When they do, an arbitrage opportunity exists.

LIMITATION (loud): naive divergence detection. For non-decisive games (Game 7
is the only state where P(series) must equal P(next game)), prices CAN
legitimately differ — series 35¢ + game 56¢ when the team is down 1-2 in the
series is mathematically self-consistent (P(series) = 0.5 × 0.56 + 0.10 × 0.44
≈ 32%). The lab's hypothetical P&L will tell us whether naive detection
captures real edge or just structural noise. State-aware math (P(series win |
series_state, P(game win))) is a future v2 if v1 doesn't show signal.

Matchup key (internal state, used for partner lookup):
(sport, frozenset({TEAMA, TEAMB}), winner)
- Series ticker: KXNBASERIES-26CLEDETR2-CLE → ("series", "KXNBA", frozenset({"CLE","DET"}), "CLE")
- Game ticker:   KXNBAGAME-26MAY11DETCLE-CLE → ("game",   "KXNBA", frozenset({"DET","CLE"}), "CLE")

Both map to the same matchup-key ("KXNBA", frozenset({"CLE","DET"}), "CLE")
when betting on the same team's win — used to look up the series partner
for a game-side emission.

Pair_key for dedup (Session 73):  ``f"{ticker}|{side}"``  — game-grain.

Per-emit aggregation in Session 72's prototype run inflated +$2,567 across
5,143 emits (median 35x amplification per unique outcome). Session 72's
manual re-aggregation at game-grain ("140 unique (ticker, side) keys")
flipped the sign to -$4.16. Session 73 codifies this via the
``CandidateOpportunity.pair_key`` field; the lab's headline metric is now
per-unique-pair-key. Game-grain (vs matchup-grain) is the right dedup key
because each game-day produces its own settled outcome — multi-game
playoff series have multiple distinct hypothetical opportunities even
within a single matchup.

Emission discipline: emit on the GAME side only — clv.json has 0 records on
KX*SERIES tickers as of 2026-05-07 (series tickers have never been scanned
by an active strategy), so series-side emissions cannot be scored. Game-side
clv coverage is 248 settled / 373 total. Series-side rows update state only.

Schema discipline (Session 51): canonical market_result enum is "yes" / "no"
(NOT the suffix-_won anti-pattern). test_strategy_lab.py:
test_canonical_schema_used_throughout enforces this at commit time.
"""
from __future__ import annotations

from typing import Optional

from tools.strategy_lab.candidate import CandidateOpportunity

# ---------------------------------------------------------------------------
# Tunable parameters (Session 51 discipline — declared at top of file).
# ---------------------------------------------------------------------------
MIN_DIVERGENCE_CENTS = 5.0       # min |series_yes - game_yes| to consider
MIN_PERSISTENCE_SCANS = 1        # 1 = same scan_id; 2 = persisted across scans
MIN_LIQUIDITY_VOLUME_24H = 100   # both markets need this floor

# Sports we evaluate (other prefixes ignored — keeps emit count bounded).
# NBA + NHL playoffs are the only currently-viable correlation surface
# (KXNBASERIES + KXNBAGAME confirmed concurrent; KXNHLSERIES + KXNHLGAME same).
_SPORTS = ("KXNBA", "KXNHL")


def _parse_pair_key(
    ticker: str,
) -> Optional[tuple[str, str, frozenset[str], str]]:
    """Extract (kind, sport, teams, winner) from a series or game ticker.

    Returns None if ticker isn't a recognized series/game pair candidate
    or isn't from a sport in _SPORTS.

    kind: "series" or "game"
    sport: e.g. "KXNBA" or "KXNHL"
    teams: frozenset of 3-4 char team codes
    winner: 3-4 char team code

    Examples:
        KXNBASERIES-26CLEDETR2-DET -> ("series", "KXNBA", frozenset({"CLE","DET"}), "DET")
        KXNBAGAME-26MAY11DETCLE-CLE -> ("game",   "KXNBA", frozenset({"DET","CLE"}), "CLE")
    """
    parts = ticker.split("-")
    if len(parts) != 3:
        return None
    prefix, middle, winner = parts
    if not winner.isalpha() or not 2 <= len(winner) <= 4:
        return None

    for sport in _SPORTS:
        if prefix == f"{sport}SERIES":
            # middle = 26{TEAMA3}{TEAMB3}R{N} — e.g. 26CLEDETR2
            if not middle.startswith("26") or "R" not in middle:
                return None
            try:
                r_idx = middle.index("R")
            except ValueError:
                return None
            teams_part = middle[2:r_idx]
            if len(teams_part) != 6:
                return None
            return (
                "series",
                sport,
                frozenset({teams_part[:3], teams_part[3:]}),
                winner,
            )
        if prefix == f"{sport}GAME":
            # middle = 26{MMM}{DD}{TEAMA3}{TEAMB3} — e.g. 26MAY11DETCLE
            # Last 6 chars are the two teams (visitor + home, in some order).
            if len(middle) < 6:
                return None
            teams_part = middle[-6:]
            return (
                "game",
                sport,
                frozenset({teams_part[:3], teams_part[3:]}),
                winner,
            )
    return None


class CrossMarketCorrelation:
    """Series-vs-game divergence detector. Emits on the game side."""

    name = "cross_market_correlation"
    # Wide clv-match window because a ticker's settlement is independent of
    # the original trade time — we want every scan's emit on the same ticker
    # to match the same settled record. 168h = 7 days covers the playoff
    # week without introducing cross-event collisions (tickers are unique
    # per game per matchup).
    clv_match_window_hours: float = 168.0

    def __init__(self):
        # ticker -> latest universe row dict
        self._latest_by_ticker: dict[str, dict] = {}
        # (sport, teams, winner) -> series ticker — O(1) partner lookup
        self._series_ticker_by_pair: dict[tuple, str] = {}
        # (sport, teams, winner) -> set of scan_ids where divergence was seen
        self._scan_pair_seen: dict[tuple, set[str]] = {}

    def evaluate(
        self, market: dict, context: Optional[dict] = None
    ) -> Optional[CandidateOpportunity]:
        ticker = market.get("ticker") or ""
        scan_id = market.get("scan_id") or ""
        if not ticker or not scan_id:
            return None

        # State update: every recognized row updates the latest-by-ticker map.
        self._latest_by_ticker[ticker] = market

        parsed = _parse_pair_key(ticker)
        if parsed is None:
            return None
        kind, sport, teams, winner = parsed
        match_key = (sport, teams, winner)

        if kind == "series":
            # Series-side rows ONLY update the partner index. They do not
            # emit (no clv coverage on KX*SERIES tickers as of 2026-05-07).
            self._series_ticker_by_pair[match_key] = ticker
            return None

        # kind == "game" — try to find a series partner for this matchup.
        partner_ticker = self._series_ticker_by_pair.get(match_key)
        if partner_ticker is None:
            return None
        partner = self._latest_by_ticker.get(partner_ticker)
        if partner is None:
            return None

        # Liquidity floor on both legs.
        if (market.get("volume_24h") or 0) < MIN_LIQUIDITY_VOLUME_24H:
            return None
        if (partner.get("volume_24h") or 0) < MIN_LIQUIDITY_VOLUME_24H:
            return None

        # Compute divergence on yes_ask side.
        m_yes_ask = market.get("yes_ask")
        p_yes_ask = partner.get("yes_ask")
        if m_yes_ask is None or p_yes_ask is None:
            return None

        game_yes = float(m_yes_ask)
        series_yes = float(p_yes_ask)
        divergence = abs(game_yes - series_yes)
        if divergence < MIN_DIVERGENCE_CENTS:
            return None

        # Persistence: track scan_ids per matchup.
        scan_ids = self._scan_pair_seen.setdefault(match_key, set())
        scan_ids.add(scan_id)
        if len(scan_ids) < MIN_PERSISTENCE_SCANS:
            return None

        # Emit on the game side. Direction depends on which leg is "expensive."
        # Theory: the more-expensive leg has premium that should decay toward
        # the cheaper leg's implied probability.
        if game_yes > series_yes:
            # Game-yes is HIGHER than series-yes. Buy NO on the game ticker.
            target_price = 100.0 - game_yes
            fair_value = 100.0 - series_yes
            side = "no"
        else:
            # Game-yes is LOWER than series-yes. Buy YES on the game ticker.
            target_price = game_yes
            fair_value = series_yes
            side = "yes"
        edge_cents = fair_value - target_price

        return CandidateOpportunity(
            ticker=ticker,
            side=side,
            target_price_cents=target_price,
            fair_value_cents=fair_value,
            edge_cents=edge_cents,
            confidence=0.5,
            reason=(
                f"cross-market divergence game-side emit "
                f"(game_yes={game_yes:.0f}c vs series_yes={series_yes:.0f}c, "
                f"Δ={divergence:.1f}c)"
            ),
            extra={
                "matchup_key": f"{sport}/{sorted(teams)}/{winner}",
                "partner_ticker": partner_ticker,
                "divergence_cents": divergence,
                "series_yes_ask": series_yes,
                "game_yes_ask": game_yes,
                "scan_id": scan_id,
                "n_scans_persisted": len(scan_ids),
            },
            # Session 73 — game-grain dedup key. Each KX*GAME-* ticker is
            # unique per game-day per matchup-side; this collapses the 35x
            # median amplification documented in Session 72.
            pair_key=f"{ticker}|{side}",
        )


STRATEGY = CrossMarketCorrelation()
