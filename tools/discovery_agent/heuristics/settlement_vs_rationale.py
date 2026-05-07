"""settlement_vs_rationale: flag settlements that contradict production-config rationale.

Direct automation of the manual KXHIGHMIA-26MAY05-T87 investigation (May 6 2026):
a $199.95 loss in a family Session 53 explicitly categorized as 'healthy at $200
cap' sat unflagged for ~6 hours. Cross-AI panel review concluded the founding
example is config-comparison-shaped, not prose-synthesis-shaped — the rationale
lives in bot/config.py dicts, not just CLAUDE.md prose.

Reads paper_trades.json settlements vs three config constants:
  - VIG_STACK_FAMILY_MAX_POSITION_DOLLARS (Session 53)
  - MOMENTUM_DISABLED_SPORTS (Session 38a/38a-2)
  - SPORT_PROFILES.size_multiplier (Session 49)

Three patterns:
  1. tail_loss_in_high_cap_family — vig_stack near-cap position with -95%+ loss
     in a $150+ cap family. Cross-cohort demote per Session 47 ladder pattern
     (single tail in positive family → demote; pre-Session-53 legacy → demote).
  2. disabled_sport_settlement — live_momentum settlement on a sport currently
     in MOMENTUM_DISABLED_SPORTS. CRITICAL severity, no demotion (correctness
     regression — disable list is not taking effect on entries).
  3. outsized_notional_post_size_multiplier — live_momentum notional exceeds
     expected post-multiplier ceiling. HIGH severity, no demotion (correctness
     gate — Session 49 sizing intervention may not be applying at kelly_size).

Out of scope: prose parsing of CLAUDE.md, new patterns beyond the 3 above,
modifying config dicts (read-only), Glint Analyst LLM (deferred).
"""

from __future__ import annotations

import datetime as dt

try:
    from bot.config import (
        MAX_BET_FRACTION,
        MOMENTUM_DISABLED_SPORTS,
        PAPER_STARTING_BALANCE,
        SPORT_PROFILES,
        VIG_STACK_FAMILY_MAX_POSITION_DOLLARS,
    )
    from bot.regime import _ticker_to_sport
    _CONFIG_AVAILABLE = True
except Exception:
    # Graceful degradation: if bot.config can't be imported (e.g., during a
    # schema migration or syntax error), heuristic returns empty findings
    # rather than crashing the whole agent run. main.py already wraps each
    # heuristic in try/except, but defending here keeps the failure mode
    # explicit and unit-testable.
    _CONFIG_AVAILABLE = False
    MOMENTUM_DISABLED_SPORTS = set()
    SPORT_PROFILES = {}
    VIG_STACK_FAMILY_MAX_POSITION_DOLLARS = {}
    MAX_BET_FRACTION = 0.0
    PAPER_STARTING_BALANCE = 0.0

    def _ticker_to_sport(ticker):  # type: ignore[no-redef]
        return None

from ..findings import Finding
from .outlier_pnl import _parse_ts

# ----- Pattern 1 thresholds -----
PATTERN1_LOSS_PCT_OF_NOTIONAL = 0.95   # -95% loss = catastrophic tail
PATTERN1_NOTIONAL_PCT_OF_CAP = 0.95    # at-or-near-cap
PATTERN1_HIGH_CAP_TIER_DOLLARS = 150   # only flag mid/healthy tiers; skip $50 already-aggressive

# ----- Pattern 1 demotion -----
LOOKBACK_DAYS = 14
SESSION_53_DEPLOY_TS = dt.datetime(
    2026, 5, 4, 23, 43, 0, tzinfo=dt.timezone.utc
)  # bot-restart timestamp, not commit-time

# ----- Pattern 2 entry-timestamp filter -----
# When a sport was added to MOMENTUM_DISABLED_SPORTS. Only entries AFTER this
# timestamp count as regressions; legacy pre-disable entries that happen to
# settle later are not regressions (the disable list correctly didn't apply
# at entry-time). Without this filter, every pre-disable settlement on a
# now-disabled sport would surface as a CRITICAL false positive (~24 noise
# findings on real data per Session 55 verification).
#
# Session 57 (May 6 2026): tightened from calendar-midnight to actual COMMIT
# timestamp. The Apr 20 disable was committed at 22:31:54 ET (b1f08ff) =
# 2026-04-21 02:31:54 UTC. Pre-Session-57 the cutoff was 2026-04-20 00:00:00
# UTC — the *start* of Apr 20 UTC, which is 26 hours before the disable
# actually existed. 9 entries fired between 02:24 UTC and 20:12 UTC on Apr 20
# (all 6+ hours BEFORE the commit existed) and got flagged as critical
# regressions on the May 6 first-real-deploy run. Calendar-precision was
# wrong; commit-timestamp is the right cutoff. Mirror Session 56.5
# discipline: when the heuristic's date math drifts from production's
# actual timeline, fix the heuristic, not the data.
#
# Sources:
#   atp_challenger, wta, wta_challenger — git blame bot/config.py:172
#     b1f08ff (Tyler Gilstrap 2026-04-20 22:31:54 -0400) blanket tennis kill
MOMENTUM_DISABLED_SINCE = {
    "atp_challenger": dt.datetime(2026, 4, 21, 2, 31, 54, tzinfo=dt.timezone.utc),
    "wta": dt.datetime(2026, 4, 21, 2, 31, 54, tzinfo=dt.timezone.utc),
    "wta_challenger": dt.datetime(2026, 4, 21, 2, 31, 54, tzinfo=dt.timezone.utc),
}

# ----- Pattern 3 sizing tolerance -----
PATTERN3_KELLY_VARIANCE_TOLERANCE = 1.2

# Severity ladder local to this heuristic. "critical" is added for Pattern 2
# (disabled-sport regression). main.py:SEVERITY_RANK gains a "critical": -1
# entry so the renderer sorts it above "high".
_SEVERITY_LADDER = ("critical", "high", "notable", "info")

SETTLED_STATUSES = frozenset({"won", "lost", "exited_early"})


def _family_from_ticker(ticker: str) -> str:
    """Mirror bot/main.py:_handle_opportunity() Session 53 pattern."""
    return ticker.split("-", 1)[0] if ticker else ""


def _notional(trade: dict) -> float:
    """notional = contracts * entry_price.

    entry_price is 0.0-1.0 dollars per canonical paper_trades.json schema
    (CLAUDE.md "Canonical Data Schema Reference"). NOT cents. Don't divide by 100.
    """
    contracts = trade.get("contracts") or 0
    entry_price = trade.get("entry_price") or 0.0
    return float(contracts) * float(entry_price)


def _is_settled(trade: dict) -> bool:
    return trade.get("status") in SETTLED_STATUSES


def _is_pattern1_tail(trade: dict) -> bool:
    """Pattern 1 raw match (before family-cap check)."""
    if trade.get("type") != "vig_stack":
        return False
    pnl = trade.get("pnl")
    if pnl is None:
        return False
    notional = _notional(trade)
    if notional <= 0:
        return False
    return float(pnl) <= -PATTERN1_LOSS_PCT_OF_NOTIONAL * notional


def _family_recent_window(
    paper_trades: list[dict],
    family: str,
    ref_ts: dt.datetime,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[dict]:
    """All settled trades in `family` within `lookback_days` ending at `ref_ts`."""
    cutoff = ref_ts - dt.timedelta(days=lookback_days)
    out = []
    for t in paper_trades:
        if not _is_settled(t):
            continue
        if _family_from_ticker(t.get("ticker", "")) != family:
            continue
        ts = _parse_ts(t.get("resolved_at") or t.get("timestamp"))
        if ts is None or ts < cutoff or ts > ref_ts:
            continue
        out.append(t)
    return out


def _severity_pattern1(
    trade: dict,
    family: str,
    notional: float,
    family_cap: int,
    paper_trades: list[dict],
) -> tuple[str, dict]:
    """Compute Pattern 1 severity + cross-cohort context dict.

    Default base = high. Demote +1 per:
      - Family aggregate (last 14d) > 0 AND tail-loss count in window == 1
        (single tail event in otherwise-profitable family).
      - Settled within 24h after Session 53 deploy AND notional > current cap
        (legacy pre-cap position — Session 53 explicitly excluded these from
        the intervention's scope; not a config-rationale contradiction).

    Clamp via min(base_idx + demote, len(_SEVERITY_LADDER) - 1) per Session 47
    counterfactual_hotspots._severity() pattern.
    """
    settled_at = _parse_ts(trade.get("resolved_at") or trade.get("timestamp"))
    if settled_at is None:
        return "high", {
            "family_recent_pnl_sum": 0.0,
            "family_recent_n": 0,
            "family_recent_tail_losses": 0,
        }

    window = _family_recent_window(paper_trades, family, settled_at)
    family_pnl_sum = sum(float(t.get("pnl") or 0) for t in window)
    tail_losses = [t for t in window if _is_pattern1_tail(t)]
    n_tail = len(tail_losses)

    base_idx = _SEVERITY_LADDER.index("high")
    demote = 0

    if family_pnl_sum > 0 and n_tail == 1:
        demote += 1

    if (
        SESSION_53_DEPLOY_TS <= settled_at < SESSION_53_DEPLOY_TS + dt.timedelta(hours=24)
        and notional > family_cap
    ):
        demote += 1

    final_idx = min(base_idx + demote, len(_SEVERITY_LADDER) - 1)
    severity = _SEVERITY_LADDER[final_idx]

    cross_cohort = {
        "family_recent_pnl_sum": round(family_pnl_sum, 2),
        "family_recent_n": len(window),
        "family_recent_tail_losses": n_tail,
    }
    return severity, cross_cohort


class SettlementVsRationale:
    name = "settlement_vs_rationale"
    data_sources = ("paper_trades",)

    def run(self, ctx) -> list[Finding]:
        if not _CONFIG_AVAILABLE:
            return []
        paper_trades = ctx.paper_trades or []
        findings: list[Finding] = []

        for trade in paper_trades:
            if not _is_settled(trade):
                continue
            findings.extend(self._check_pattern1(trade, paper_trades))
            findings.extend(self._check_pattern2(trade))
            findings.extend(self._check_pattern3(trade))

        return findings

    # -------- Pattern 1: tail_loss_in_high_cap_family --------

    def _check_pattern1(self, trade: dict, paper_trades: list[dict]) -> list[Finding]:
        if not _is_pattern1_tail(trade):
            return []
        ticker = trade.get("ticker", "")
        family = _family_from_ticker(ticker)
        if family not in VIG_STACK_FAMILY_MAX_POSITION_DOLLARS:
            return []
        family_cap = VIG_STACK_FAMILY_MAX_POSITION_DOLLARS[family]
        if family_cap < PATTERN1_HIGH_CAP_TIER_DOLLARS:
            return []  # already-aggressive $50 tier; skip
        notional = _notional(trade)
        if notional < PATTERN1_NOTIONAL_PCT_OF_CAP * family_cap:
            return []  # not at-or-near-cap

        pnl = float(trade.get("pnl") or 0)
        loss_pct = abs(pnl) / notional if notional > 0 else 0.0
        severity, cross_cohort = _severity_pattern1(
            trade, family, notional, family_cap, paper_trades
        )

        evidence = {
            "ticker": ticker,
            "family": family,
            "settled_at": trade.get("resolved_at") or trade.get("timestamp"),
            "notional": round(notional, 2),
            "loss_pct_of_notional": round(loss_pct, 3),
            "family_cap": family_cap,
            **cross_cohort,
            "config_rationale": (
                f"Session 53 categorized {family} cap at ${family_cap}; this "
                f"settlement was at-or-near-cap and produced a -{int(loss_pct * 100)}% "
                f"loss. Either the cap is too permissive or the entry quality on "
                f"{family} is structurally lower than Session 53 measured."
            ),
            "_fingerprint_keys": ["ticker", "family", "settled_at"],
        }

        return [Finding(
            heuristic=self.name,
            severity=severity,
            title=(
                f"tail_loss_in_high_cap_family: {ticker} lost ${abs(pnl):.2f} "
                f"on ${notional:.0f} notional (cap ${family_cap})"
            ),
            summary=(
                f"vig_stack settlement {ticker} produced -{int(loss_pct * 100)}% loss "
                f"at ${notional:.0f} notional, near {family} family cap of "
                f"${family_cap}. Family 14d aggregate P&L: "
                f"${cross_cohort.get('family_recent_pnl_sum', 0):.2f} across "
                f"n={cross_cohort.get('family_recent_n', 0)} settled, "
                f"n={cross_cohort.get('family_recent_tail_losses', 0)} tail losses."
            ),
            evidence=evidence,
            suggested_action=(
                f"Investigate whether {family} cap of ${family_cap} should be "
                f"reduced. If family aggregate is positive and this is a single "
                f"tail event, may be tolerable at current cap. If 2+ tail losses "
                f"in 14d, consider tightening cap to next tier."
            ),
        )]

    # -------- Pattern 2: disabled_sport_settlement --------

    def _check_pattern2(self, trade: dict) -> list[Finding]:
        if trade.get("type") != "live_momentum":
            return []
        ticker = trade.get("ticker", "")
        sport = _ticker_to_sport(ticker)
        if sport is None or sport not in MOMENTUM_DISABLED_SPORTS:
            return []

        # Filter out legacy pre-disable entries. A trade entered BEFORE the
        # sport was disabled is not a regression — the disable list correctly
        # didn't exist at entry-time. Use trade.timestamp (entry time, per
        # canonical paper_trades.json schema) vs MOMENTUM_DISABLED_SINCE.
        # If the sport isn't in the SINCE map (unknown disable date), default
        # to flagging to be safe — better a noisy critical than a missed regression.
        disabled_since = MOMENTUM_DISABLED_SINCE.get(sport)
        if disabled_since is not None:
            entry_ts = _parse_ts(trade.get("timestamp"))
            if entry_ts is None or entry_ts < disabled_since:
                return []  # legacy pre-disable entry, not a regression

        evidence = {
            "ticker": ticker,
            "sport": sport,
            "entered_at": trade.get("timestamp"),
            "settled_at": trade.get("resolved_at") or trade.get("timestamp"),
            "disable_list_current": sorted(MOMENTUM_DISABLED_SPORTS),
            "disable_session_ref": "Session 38a/38a-2",
            "_fingerprint_keys": ["ticker", "sport", "settled_at"],
        }

        return [Finding(
            heuristic=self.name,
            severity="critical",
            title=f"disabled_sport_settlement: live_momentum trade on {sport} ({ticker})",
            summary=(
                f"live_momentum settled on {sport}, which is in MOMENTUM_DISABLED_SPORTS "
                f"({sorted(MOMENTUM_DISABLED_SPORTS)}). Disable list is not taking "
                f"effect on entries — correctness regression."
            ),
            evidence=evidence,
            suggested_action=(
                f"Verify MOMENTUM_DISABLED_SPORTS gate is firing in "
                f"bot/live_watcher.py:_tick_momentum. Check ticker prefix-to-sport "
                f"map didn't drift. Pull decisions.jsonl for this ticker to confirm "
                f"the gate fired or was bypassed."
            ),
        )]

    # -------- Pattern 3: outsized_notional_post_size_multiplier --------

    def _check_pattern3(self, trade: dict) -> list[Finding]:
        if trade.get("type") != "live_momentum":
            return []
        ticker = trade.get("ticker", "")
        sport = _ticker_to_sport(ticker)
        if sport is None:
            return []
        profile = SPORT_PROFILES.get(sport, {})
        size_multiplier = profile.get("size_multiplier")
        if size_multiplier is None or size_multiplier >= 1.0:
            return []

        notional = _notional(trade)
        expected_max = (
            MAX_BET_FRACTION * PAPER_STARTING_BALANCE * size_multiplier
            * PATTERN3_KELLY_VARIANCE_TOLERANCE
        )
        if notional <= expected_max:
            return []

        evidence = {
            "ticker": ticker,
            "sport": sport,
            "size_multiplier_configured": size_multiplier,
            "expected_max_notional": round(expected_max, 2),
            "actual_notional": round(notional, 2),
            "session_ref": "Session 49",
            "_fingerprint_keys": ["ticker", "sport", "actual_notional"],
        }

        return [Finding(
            heuristic=self.name,
            severity="high",
            title=(
                f"outsized_notional_post_size_multiplier: {ticker} ({sport}) "
                f"sized ${notional:.2f} > ceiling ${expected_max:.2f}"
            ),
            summary=(
                f"live_momentum settlement on {sport} (size_multiplier="
                f"{size_multiplier}) had notional ${notional:.2f}, exceeding "
                f"post-multiplier ceiling ${expected_max:.2f} (= MAX_BET_FRACTION × "
                f"balance × multiplier × {PATTERN3_KELLY_VARIANCE_TOLERANCE} "
                f"tolerance). Session 49 sizing intervention may not be applying "
                f"at the kelly_size call site."
            ),
            evidence=evidence,
            suggested_action=(
                f"Verify Session 49 sport= kwarg is threaded through to "
                f"bot/sizing.py:kelly_size for {sport}. Check "
                f"bot/live_watcher.py:_auto_bet_momentum sizing path."
            ),
        )]
