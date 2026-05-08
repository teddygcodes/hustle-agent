"""Session 75 Branch-B prototype: live_momentum with a scan-volume overlay.

Phase 0 found no per-tick volume in ``live_ticks.jsonl``. This candidate is
therefore intentionally coarse: it runs against ``universe.jsonl`` scan rows
and treats ``volume_24h`` history as a proxy for volume momentum. That proxy
is not a production signal; it is a quick lab test to decide whether the idea
is worth real per-tick instrumentation in a later session.

Entry logic mirrors the live_momentum price-action gates from
``bot/strategies/live_momentum.py`` without importing that strategy class:
leader floor, sport profile min/max dip, max entry, disabled-sport gate, and
the DQS threshold when a row supplies ``dqs``. Universe rows do not carry game
context, so missing DQS is recorded as a Branch-B limitation rather than
inventing a parallel game-state model inside the lab.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

from bot.config import (
    MOMENTUM_DIP_BUY,
    MOMENTUM_DIP_MAX,
    MOMENTUM_DISABLED_SPORTS,
    MOMENTUM_DQS_THRESHOLD,
    MOMENTUM_PRICE_WINDOW,
    SPORT_PROFILES,
    get_leader_min_for_sport,
)
from bot.regime import _ticker_to_sport
from tools.strategy_lab.candidate import CandidateOpportunity

# ---------------------------------------------------------------------------
# Tunable parameters (Session 51 discipline - declared at top of file).
# ---------------------------------------------------------------------------
VOLUME_LOOKBACK_TICKS = 12
VOLUME_BASELINE_TICKS = 60
MIN_VOL_RATIO = 1.5

# Branch-B scan-level proxy. Real tick volume instrumentation would replace
# this with per-tick traded volume.
VOLUME_FIELD = "volume_24h"

_LIVE_MOMENTUM_PREFIXES = (
    "KXNBAGAME",
    "KXNHLGAME",
    "KXMLBGAME",
    "KXATPMATCH",
    "KXATPCHALLENGERMATCH",
    "KXWTAMATCH",
    "KXWTACHALLENGERMATCH",
    "KXUFCFIGHT",
    "KXIPLGAME",
)

_DEFAULT_SPORT_PROFILE = {
    "min_dip": int(MOMENTUM_DIP_BUY * 100),
    "max_dip": int(MOMENTUM_DIP_MAX * 100),
    "max_entry": 75,
    "min_score_diff": 0,
    "periods": 4,
    "late_game_period": 3,
}


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_live_momentum_market(ticker: str) -> bool:
    return ticker.startswith(_LIVE_MOMENTUM_PREFIXES)


def _sport_profile(sport: str | None) -> dict:
    if sport and sport in SPORT_PROFILES:
        return SPORT_PROFILES[sport]
    return _DEFAULT_SPORT_PROFILE


class VolumeSpikeLiveMomentum:
    name = "volume_spike_live_momentum"
    clv_match_window_hours: float = 2.0

    def __init__(
        self,
        *,
        volume_lookback_ticks: int = VOLUME_LOOKBACK_TICKS,
        volume_baseline_ticks: int = VOLUME_BASELINE_TICKS,
        min_vol_ratio: float = MIN_VOL_RATIO,
        volume_field: str = VOLUME_FIELD,
        require_volume_spike: bool = True,
        name: str | None = None,
    ) -> None:
        if volume_lookback_ticks <= 0:
            raise ValueError("volume_lookback_ticks must be positive")
        if volume_baseline_ticks <= 0:
            raise ValueError("volume_baseline_ticks must be positive")
        self.volume_lookback_ticks = volume_lookback_ticks
        self.volume_baseline_ticks = volume_baseline_ticks
        self.min_vol_ratio = min_vol_ratio
        self.volume_field = volume_field
        self.require_volume_spike = require_volume_spike
        if name is not None:
            self.name = name

        price_window = max(3, MOMENTUM_PRICE_WINDOW)
        volume_window = volume_lookback_ticks + volume_baseline_ticks
        self._prices: dict[str, deque[float]] = {}
        self._volumes: dict[str, deque[float]] = {}
        self._price_window = price_window
        self._volume_window = volume_window

    def _price_stats(self, ticker: str, price_cents: float) -> tuple[int, float]:
        history = self._prices.setdefault(ticker, deque(maxlen=self._price_window))
        history.append(price_cents)
        recent_high = max(history) if history else price_cents
        return len(history), recent_high

    def _volume_stats(
        self,
        ticker: str,
        volume_value: float,
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        history = self._volumes.setdefault(ticker, deque(maxlen=self._volume_window))
        history.append(volume_value)
        if len(history) < self._volume_window:
            return None, None, None

        values = list(history)
        recent = values[-self.volume_lookback_ticks:]
        baseline = values[: self.volume_baseline_ticks]
        recent_avg = sum(recent) / len(recent)
        baseline_avg = sum(baseline) / len(baseline)
        if baseline_avg <= 0:
            return None, recent_avg, baseline_avg
        return recent_avg / baseline_avg, recent_avg, baseline_avg

    def _dqs_passes(self, market: dict, profile: dict) -> tuple[bool, Optional[float], str]:
        dqs = _as_float(market.get("dqs"))
        if dqs is not None:
            return dqs >= MOMENTUM_DQS_THRESHOLD, dqs, "row_dqs"
        if profile.get("skip_dqs", False):
            return True, None, "skip_dqs_sport"
        # Branch B limitation: universe rows do not include GameContext or the
        # computed DQS. Let the coarse prototype keep testing price+volume, but
        # make the missing gate visible in every emit.
        return True, None, "unavailable_scan_proxy"

    def evaluate(
        self,
        market: dict,
        context: Optional[dict] = None,
    ) -> Optional[CandidateOpportunity]:
        ticker = market.get("ticker") or ""
        if not ticker or not _is_live_momentum_market(ticker):
            return None
        if market.get("status") not in (None, "active", "open"):
            return None

        sport = _ticker_to_sport(ticker)
        sport_lc = (sport or "").lower()
        if sport_lc in MOMENTUM_DISABLED_SPORTS:
            return None

        price = _as_float(market.get("yes_ask"))
        if price is None or price <= 0:
            return None

        profile = _sport_profile(sport)
        max_entry = float(profile.get("max_entry", 75))
        min_dip = float(profile.get("min_dip", int(MOMENTUM_DIP_BUY * 100)))
        max_dip = float(profile.get("max_dip", int(MOMENTUM_DIP_MAX * 100)))
        leader_min_cents = get_leader_min_for_sport(sport) * 100.0

        price_history_len, recent_high = self._price_stats(ticker, price)
        dip_cents = recent_high - price

        volume_ratio = None
        recent_volume_avg = None
        baseline_volume_avg = None
        if self.require_volume_spike:
            volume_value = _as_float(market.get(self.volume_field))
            if volume_value is None:
                return None
            volume_ratio, recent_volume_avg, baseline_volume_avg = self._volume_stats(
                ticker, volume_value
            )

        if price < leader_min_cents:
            return None
        if price > max_entry:
            return None
        if price_history_len < 3:
            return None
        if dip_cents < min_dip or dip_cents > max_dip:
            return None

        dqs_ok, dqs, dqs_mode = self._dqs_passes(market, profile)
        if not dqs_ok:
            return None

        if self.require_volume_spike:
            if volume_ratio is None or volume_ratio < self.min_vol_ratio:
                return None

        fair_value = min(95.0, price + dip_cents)
        edge_cents = fair_value - price
        confidence = 0.5
        if volume_ratio is not None:
            confidence = min(1.0, 0.5 + max(0.0, volume_ratio - self.min_vol_ratio) * 0.1)

        return CandidateOpportunity(
            ticker=ticker,
            side="yes",
            target_price_cents=price,
            fair_value_cents=fair_value,
            edge_cents=edge_cents,
            confidence=confidence,
            reason=(
                f"live_momentum dip + {self.volume_field} ratio "
                f">= {self.min_vol_ratio:.2f}"
            ),
            extra={
                "branch": "B_scan_volume_proxy",
                "sport": sport,
                "volume_field": self.volume_field,
                "volume_ratio": volume_ratio,
                "recent_volume_avg": recent_volume_avg,
                "baseline_volume_avg": baseline_volume_avg,
                "dip_cents": dip_cents,
                "recent_high": recent_high,
                "leader_min_cents": leader_min_cents,
                "min_dip": min_dip,
                "max_dip": max_dip,
                "dqs": dqs,
                "dqs_mode": dqs_mode,
            },
            pair_key=f"{ticker}|long",
        )


class DipOnlyLiveMomentumBaseline(VolumeSpikeLiveMomentum):
    """Same scan-level price gates, with the volume overlay disabled."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("require_volume_spike", False)
        kwargs.setdefault("name", "dip_only_live_momentum_baseline")
        super().__init__(**kwargs)


STRATEGY = VolumeSpikeLiveMomentum()
