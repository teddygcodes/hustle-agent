"""counterfactual_hotspots: flag (skipped_by_gate, sport) buckets with high mean +CLV.

Direct automation of the manual ATP query that drove Session 38a (Apr 29) — when
a rejection gate has settled CFs averaging >5¢ CLV at >60% +CLV rate, the gate
is surrendering alpha and is a candidate for retuning. Survivorship guard
(n_no_won >= 3) ensures we're not just measuring leader-side wins.

Reads from clv.json, last 30d settled records (real OR counterfactual_settled).

Session 47 refinement (May 1 2026, post-Sessions 45 + 46): per-cohort signal is
preserved (data + Finding) but each Finding now carries cross-cohort context
on its gate alongside the per-cohort flag. Severity demotes when cross-cohort
contradicts (raw mean < 0¢ AND/OR trimmed mean < 3¢ on negative raw) OR the
cohort's sport is in MOMENTUM_DISABLED_SPORTS (gate-tuning structurally
neutralized — relaxing the gate produces zero new actual trades for that
sport). Sessions 45 + 46 both shipped Outcome C HOLD on cherry-picked +CLV
clusters that the cross-cohort lens contradicted; this refinement automates
that lens at agent-time so future findings carry the context inline.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from bot.config import MOMENTUM_DISABLED_SPORTS

from .. import _sport_classifier
from ..findings import Finding
from .outlier_pnl import _parse_ts

MIN_CF_COUNT = 10
MIN_MEAN_CLV_CENTS = 5.0
MIN_POSITIVE_CLV_RATE = 0.60
MIN_NO_WON_COUNT = 3
LOOKBACK_DAYS = 30
HIGH_SEVERITY_MEAN_CENTS = 15.0

# Session 47 cross-cohort tunables.
CROSS_COHORT_MEAN_DEMOTION_FLOOR = 0.0   # cross-cohort mean < this → demote 1
CROSS_COHORT_TRIMMED_MEAN_FLOOR = 3.0    # trimmed mean < this AND raw <= 0 → demote 1 more
DISABLED_SPORT_DEMOTION = True           # this cohort's sport in MOMENTUM_DISABLED_SPORTS → demote 1

_SEVERITY_LADDER = ("high", "notable", "info")


def _normalize_sport_for_disabled_check(sport: str | None) -> str | None:
    """Map _sport_classifier output to bot.config.MOMENTUM_DISABLED_SPORTS vocab.

    The bot's disabled set has flat names ('atp_challenger', 'wta', 'nba'); the
    discovery agent's classifier emits per-game/futures variants ('nba_game',
    'nba_futures', 'mlb_game', etc.). Strip those suffixes so the comparison
    works. Other sports (atp_challenger, wta, ufc, ipl, weather_high, index,
    None) pass through unchanged — pass-through rather than crash means an
    unrecognized future sport family flows to `in MOMENTUM_DISABLED_SPORTS`
    returning False, which is the safe default.
    """
    if sport is None:
        return None
    for suffix in ("_game", "_futures"):
        if sport.endswith(suffix):
            return sport[: -len(suffix)]
    return sport


def _trimmed_mean(values: list[float]) -> float:
    """Drop top-1 + bottom-1 then mean. If n < 3, return raw mean (don't crash on slice)."""
    if not values:
        return 0.0
    if len(values) < 3:
        return statistics.mean(values)
    sorted_vals = sorted(values)
    return statistics.mean(sorted_vals[1:-1])


def _compute_cross_cohort(gate: str, all_rows: list[dict]) -> dict:
    """Compute cross-cohort context for one gate across all sports it touched.

    Returns dict with the canonical Session 47 cross-cohort evidence keys:
      - cross_cohort_total_n / n_sports
      - cross_cohort_mean_clv_cents / trimmed_mean_clv_cents
      - cross_cohort_n_positive_sports / n_negative_sports
      - cross_cohort_breakdown (top-10 by n desc, [(sport, n, mean), ...])
      - n_disabled_sport_cohorts_in_top3 (top-3 by per-cohort mean)
    """
    by_sport: dict[str | None, list[float]] = defaultdict(list)
    for r in all_rows:
        cents = float(r["clv_cents"])
        sport = _sport_classifier.sport_from_ticker_distinguished(r.get("ticker", ""))
        by_sport[sport].append(cents)

    per_sport_stats: list[tuple[str | None, int, float]] = [
        (sport, len(vals), statistics.mean(vals)) for sport, vals in by_sport.items()
    ]

    all_cents = [c for vals in by_sport.values() for c in vals]
    raw_mean = statistics.mean(all_cents) if all_cents else 0.0
    trimmed = _trimmed_mean(all_cents)

    n_pos = sum(1 for _, _, m in per_sport_stats if m > 0)
    n_neg = sum(1 for _, _, m in per_sport_stats if m <= 0)

    breakdown_sorted_by_n = sorted(per_sport_stats, key=lambda t: -t[1])[:10]
    breakdown = [
        (sport, n, round(mean, 2)) for sport, n, mean in breakdown_sorted_by_n
    ]

    # Top-3 sport cohorts by per-cohort mean (highest first); count those whose
    # normalized sport is in MOMENTUM_DISABLED_SPORTS.
    top3_by_mean = sorted(per_sport_stats, key=lambda t: -t[2])[:3]
    n_disabled_in_top3 = sum(
        1 for sport, _, _ in top3_by_mean
        if _normalize_sport_for_disabled_check(sport) in MOMENTUM_DISABLED_SPORTS
    )

    return {
        "cross_cohort_total_n": len(all_cents),
        "cross_cohort_n_sports": len(by_sport),
        "cross_cohort_mean_clv_cents": round(raw_mean, 2),
        "cross_cohort_trimmed_mean_clv_cents": round(trimmed, 2),
        "cross_cohort_n_positive_sports": n_pos,
        "cross_cohort_n_negative_sports": n_neg,
        "cross_cohort_breakdown": breakdown,
        "n_disabled_sport_cohorts_in_top3": n_disabled_in_top3,
    }


def _severity(per_cohort_mean: float, ctx_data: dict, this_cohort_disabled: bool) -> str:
    """Compute severity with Session 47 demotion ladder.

    Base = high if per-cohort mean >= HIGH_SEVERITY_MEAN_CENTS else notable.
    Demote 1 step per condition that fires:
      - cross-cohort raw mean < CROSS_COHORT_MEAN_DEMOTION_FLOOR
      - cross-cohort trimmed mean < CROSS_COHORT_TRIMMED_MEAN_FLOOR AND raw <= 0
      - this cohort's sport is in MOMENTUM_DISABLED_SPORTS (DISABLED_SPORT_DEMOTION)

    Final clamps at 'info'.
    """
    base = "high" if per_cohort_mean >= HIGH_SEVERITY_MEAN_CENTS else "notable"
    demote = 0
    raw = ctx_data["cross_cohort_mean_clv_cents"]
    trimmed = ctx_data["cross_cohort_trimmed_mean_clv_cents"]
    if raw < CROSS_COHORT_MEAN_DEMOTION_FLOOR:
        demote += 1
    if trimmed < CROSS_COHORT_TRIMMED_MEAN_FLOOR and raw <= 0:
        demote += 1
    if DISABLED_SPORT_DEMOTION and this_cohort_disabled:
        demote += 1
    base_idx = _SEVERITY_LADDER.index(base)
    final_idx = min(base_idx + demote, len(_SEVERITY_LADDER) - 1)
    return _SEVERITY_LADDER[final_idx]


class CounterfactualHotspots:
    name = "counterfactual_hotspots"
    data_sources = ("clv",)

    def run(self, ctx) -> list[Finding]:
        cutoff = ctx.loaded_at - dt.timedelta(days=LOOKBACK_DAYS)
        buckets: dict[tuple[str, str | None], list[dict]] = defaultdict(list)

        for r in ctx.clv:
            if r.get("status") not in ("settled", "counterfactual_settled"):
                continue
            gate = r.get("skipped_by_gate")
            if gate is None:
                continue  # only counterfactuals carry skipped_by_gate; real trades skipped
            cents = r.get("clv_cents")
            if cents is None:
                continue
            ts = _parse_ts(r.get("settled_at") or r.get("recorded_at"))
            if ts is None or ts < cutoff:
                continue
            sport = _sport_classifier.sport_from_ticker_distinguished(r.get("ticker", ""))
            buckets[(gate, sport)].append(r)

        # Session 47: pre-compute cross-cohort context per gate (one pass).
        per_gate: dict[str, list[dict]] = defaultdict(list)
        for (gate, _sport), rows in buckets.items():
            per_gate[gate].extend(rows)
        cross_cohort_context: dict[str, dict] = {
            gate: _compute_cross_cohort(gate, all_rows)
            for gate, all_rows in per_gate.items()
        }

        findings: list[Finding] = []
        for (gate, sport), rows in buckets.items():
            if len(rows) < MIN_CF_COUNT:
                continue
            cents_list = [float(r["clv_cents"]) for r in rows]
            mean_cents = statistics.mean(cents_list)
            if mean_cents < MIN_MEAN_CLV_CENTS:
                continue
            positive_count = sum(1 for c in cents_list if c > 0)
            positive_rate = positive_count / len(cents_list)
            if positive_rate < MIN_POSITIVE_CLV_RATE:
                continue
            n_no_won = sum(1 for r in rows if r.get("market_result") == "no")
            if n_no_won < MIN_NO_WON_COUNT:
                continue

            ctx_data = cross_cohort_context[gate]
            this_cohort_disabled = (
                _normalize_sport_for_disabled_check(sport) in MOMENTUM_DISABLED_SPORTS
            )
            severity = _severity(mean_cents, ctx_data, this_cohort_disabled)

            evidence = {
                "skip_reason": gate,
                "sport": sport,
                "count": len(rows),
                "mean_clv_cents": round(mean_cents, 2),
                "positive_clv_rate": round(positive_rate, 3),
                "n_no_won": n_no_won,
                "n_yes_won": sum(1 for r in rows if r.get("market_result") == "yes"),
                "lookback_days": LOOKBACK_DAYS,
                # Session 47: cross-cohort context.
                **ctx_data,
                "this_cohort_is_disabled_sport": this_cohort_disabled,
                "_fingerprint_keys": ["skip_reason", "sport"],
            }
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=(
                    f"{gate}/{sport}: {len(rows)} settled CFs, "
                    f"mean CLV {mean_cents:+.1f}¢ at {int(positive_rate * 100)}% +CLV"
                ),
                summary=(
                    f"Rejection gate '{gate}' on {sport} settled {len(rows)} counterfactuals "
                    f"with mean CLV {mean_cents:+.1f}¢ ({int(positive_rate * 100)}% +CLV, "
                    f"n_no_won={n_no_won}). Gate may be surrendering alpha — candidate for "
                    f"retuning if the asymmetric-evidence pattern holds at higher n."
                ),
                evidence=evidence,
                suggested_action=(
                    f"Inspect '{gate}' threshold for {sport}. Pull all {len(rows)} settled "
                    f"CFs and verify n_no_won >= 5 floor before any retuning move "
                    f"(Session 30-followup discipline). Mirror Session 38a's hygiene checks."
                ),
            ))
        return findings
