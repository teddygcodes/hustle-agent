"""Augments bot.regime.SPORT_PREFIXES with per-game vs futures granularity.

Discovery-agent only; do NOT modify bot/regime.py — it's the canonical map for
five regime-axis writers and any divergence creates silent attribution bugs.

The bot's map collapses KXMLB-* (futures) and KXMLBGAME-* (per-game) into the
same value 'mlb'. The Session 43-investigate finding (May 1 2026) showed this
hides materially different cohorts — futures decisions vastly outnumber per-game
ones, but trades only fire on per-game. The discovery agent needs to distinguish
them to avoid over-stating cohort emergence findings.
"""

from __future__ import annotations

from bot.regime import _ticker_to_sport

_VARIANT_OVERRIDES = {
    "KXMLBGAME": "mlb_game",
    "KXNBAGAME": "nba_game",
    "KXNHLGAME": "nhl_game",
    "KXMLB": "mlb_futures",
    "KXNBA": "nba_futures",
    "KXNHL": "nhl_futures",
}
_SORTED_OVERRIDE_KEYS = sorted(_VARIANT_OVERRIDES.keys(), key=len, reverse=True)


def sport_from_ticker_distinguished(ticker: str | None) -> str | None:
    """Like bot.regime._ticker_to_sport but distinguishes per-game vs futures
    for MLB / NBA / NHL. Other sports fall through to the bot's map unchanged."""
    if not ticker:
        return None
    for prefix in _SORTED_OVERRIDE_KEYS:
        if ticker.startswith(prefix):
            return _VARIANT_OVERRIDES[prefix]
    return _ticker_to_sport(ticker)
