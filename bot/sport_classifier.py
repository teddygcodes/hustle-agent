"""
Fine-grained sport classifier for ticker prefixes (Session 141).

Mirrors tools/discovery_agent/_sport_classifier.sport_from_ticker_distinguished()
intentionally rather than importing across the bot/tools boundary (per CLAUDE.md
final note 11 — patterns get copied, not extracted).

Used at the bot's MOMENTUM_DISABLED_SPORTS check site to distinguish per-game
from futures markets, which the coarser bot.regime.SPORT_PREFIXES does not.
S97 added 'nba_game' to MOMENTUM_DISABLED_SPORTS but the coarse classifier
returned 'nba' for KXNBAGAME-* tickers, causing the disable check to silently
no-op. This module fixes that.
"""
from __future__ import annotations

_FINE_GRAINED_PREFIXES: dict[str, str] = {
    "KXMLBGAME": "mlb_game",
    "KXNBAGAME": "nba_game",
    "KXNHLGAME": "nhl_game",
    "KXMLB": "mlb_futures",
    "KXNBA": "nba_futures",
    "KXNHL": "nhl_futures",
}
_SORTED_PREFIXES = sorted(_FINE_GRAINED_PREFIXES.keys(), key=len, reverse=True)


def sport_from_ticker_fine(ticker: str | None) -> str | None:
    """Return the per-game / futures fine-grained sport for a ticker, or None.

    Longer prefixes (KXNBAGAME) win over shorter (KXNBA) via length-sorted scan.
    Returns None for tickers outside this prefix table — callers should fall
    through to the coarse classification (e.g. self.sport from ACTIVE_SPORTS).
    """
    if not ticker:
        return None
    for prefix in _SORTED_PREFIXES:
        if ticker.startswith(prefix):
            return _FINE_GRAINED_PREFIXES[prefix]
    return None
