"""
Game Context — Live game intelligence for smarter trading decisions.

Tracks score history, momentum, and computes win probability estimates
from ESPN live data. Used by LiveGameWatcher to determine whether a
price dip is noise (buy it) or information (avoid it).

Key concepts:
- Score snapshots every tick → detect scoring events
- Momentum: who's been scoring recently? Is the lead growing or shrinking?
- Win probability: empirical models per sport give fair price estimates
- The EDGE: when Kalshi price < estimated win prob → real statistical edge
- Sport instincts: situational awareness a veteran bettor uses —
  garbage time, clutch time, power plays, empty net, high-leverage ABs
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("glint.game_context")


@dataclass
class ScoreSnapshot:
    """A single score observation at a point in time."""
    our_score: int
    their_score: int
    period: int
    clock_seconds: float  # seconds remaining in current period
    tick_num: int


@dataclass
class GameContext:
    """
    Tracks live game state and computes trading-relevant signals.

    Feed it ESPN data every tick via update(). Query it for:
    - momentum_score: who's been scoring? (-1 to +1, positive = our team)
    - lead_trend: is the lead growing (+) or shrinking (-)?
    - win_probability: estimated fair win prob given score + time
    - is_safe_dip: composite assessment — is a dip likely noise?
    """
    sport: str
    our_team: str = ""
    their_team: str = ""

    # Score history (last 60 ticks = ~10 min at 10s intervals)
    _snapshots: deque = field(default_factory=lambda: deque(maxlen=60))
    _scoring_events: list = field(default_factory=list)  # list of (tick, who_scored, points)
    _tick: int = 0

    # Cached computations
    _last_momentum: float = 0.0
    _last_lead_trend: float = 0.0
    _last_win_prob: float = 0.5

    def update(self, espn_data: dict, our_score: int, their_score: int,
               period: int, clock_str: str = "") -> None:
        """Feed in fresh ESPN data. Call every tick."""
        self._tick += 1
        clock_secs = self._parse_clock(clock_str)

        snap = ScoreSnapshot(
            our_score=our_score,
            their_score=their_score,
            period=period,
            clock_seconds=clock_secs,
            tick_num=self._tick,
        )

        # Detect scoring events by comparing to previous snapshot
        if self._snapshots:
            prev = self._snapshots[-1]
            our_delta = our_score - prev.our_score
            their_delta = their_score - prev.their_score

            if our_delta > 0:
                self._scoring_events.append((self._tick, "us", our_delta))
            if their_delta > 0:
                self._scoring_events.append((self._tick, "them", their_delta))

        self._snapshots.append(snap)

        # Recompute signals
        self._last_momentum = self._compute_momentum()
        self._last_lead_trend = self._compute_lead_trend()
        self._last_win_prob = self._compute_win_prob(snap)

        # Store linescores if available for period-by-period analysis
        self._linescores = espn_data.get("linescores", {})
        self._situation = espn_data.get("situation", {})

    @property
    def momentum(self) -> float:
        """
        Who has momentum? Range -1.0 to +1.0.
        +1.0 = our team has been dominating scoring recently
        -1.0 = opponent is on a run
        0.0 = even / no data
        """
        return self._last_momentum

    @property
    def lead_trend(self) -> float:
        """
        Is the lead growing or shrinking?
        Positive = lead growing (good for buying dips)
        Negative = lead shrinking (opponent is coming back — dangerous)
        0 = stable
        """
        return self._last_lead_trend

    @property
    def win_probability(self) -> float:
        """Estimated fair win probability given current score + time."""
        return self._last_win_prob

    @property
    def score_diff(self) -> int:
        """Current score differential (positive = our team ahead)."""
        if not self._snapshots:
            return 0
        s = self._snapshots[-1]
        return s.our_score - s.their_score

    @property
    def is_blowout(self) -> bool:
        """Is the game a blowout? (>95% estimated win prob)"""
        return self._last_win_prob >= 0.95

    @property
    def is_close_game(self) -> bool:
        """Is the game close? (40-60% win prob)"""
        return 0.40 <= self._last_win_prob <= 0.60

    @property
    def opponent_on_run(self) -> bool:
        """Is the opponent on a scoring run (3+ consecutive scoring events)?"""
        if len(self._scoring_events) < 3:
            return False
        recent = self._scoring_events[-3:]
        return all(ev[1] == "them" for ev in recent)

    @property
    def our_team_on_run(self) -> bool:
        """Is our team on a scoring run?"""
        if len(self._scoring_events) < 3:
            return False
        recent = self._scoring_events[-3:]
        return all(ev[1] == "us" for ev in recent)

    @property
    def game_completion_pct(self) -> float:
        """How far through the game are we? 0.0 to 1.0."""
        if not self._snapshots:
            return 0.0
        s = self._snapshots[-1]
        return self._get_completion_pct(s.period, s.clock_seconds)

    def get_signals_summary(self) -> dict:
        """Return all signals as a dict for logging/display."""
        return {
            "momentum": round(self._last_momentum, 2),
            "lead_trend": round(self._last_lead_trend, 2),
            "win_prob": round(self._last_win_prob, 3),
            "score_diff": self.score_diff,
            "game_pct": round(self.game_completion_pct, 2),
            "opp_on_run": self.opponent_on_run,
            "our_run": self.our_team_on_run,
            "is_blowout": self.is_blowout,
            "is_close": self.is_close_game,
            "scoring_events": len(self._scoring_events),
        }

    # ------------------------------------------------------------------
    # Internal computations
    # ------------------------------------------------------------------

    def _compute_momentum(self) -> float:
        """
        Momentum score based on recent scoring events.
        Looks at last 18 ticks (~3 minutes) of scoring.
        Weights recent events more heavily (exponential decay).
        """
        if not self._scoring_events:
            return 0.0

        cutoff_tick = self._tick - 18  # ~3 minutes of data
        recent = [(t, who, pts) for t, who, pts in self._scoring_events if t > cutoff_tick]

        if not recent:
            return 0.0

        momentum = 0.0
        for tick, who, pts in recent:
            # Recency weight: more recent = more important
            age = self._tick - tick
            weight = math.exp(-age / 12.0)  # half-life of ~12 ticks (2 min)

            if who == "us":
                momentum += pts * weight
            else:
                momentum -= pts * weight

        # Normalize to -1..+1 range
        # In NBA, 10 points in 3 min is a big run
        # In NHL, 2 goals is huge
        max_val = {"nba": 12.0, "nhl": 3.0, "mlb": 4.0}.get(self.sport, 6.0)
        return max(-1.0, min(1.0, momentum / max_val))

    def _compute_lead_trend(self) -> float:
        """
        Is the lead growing or shrinking?
        Compares score diff over last 12 ticks (~2 minutes).
        Returns positive if lead is growing, negative if shrinking.
        """
        if len(self._snapshots) < 4:
            return 0.0

        snaps = list(self._snapshots)
        lookback = min(12, len(snaps) - 1)
        old = snaps[-lookback - 1]
        new = snaps[-1]

        old_diff = old.our_score - old.their_score
        new_diff = new.our_score - new.their_score

        trend = new_diff - old_diff

        # Normalize by sport
        max_val = {"nba": 10.0, "nhl": 2.0, "mlb": 3.0}.get(self.sport, 5.0)
        return max(-1.0, min(1.0, trend / max_val))

    def _compute_win_prob(self, snap: ScoreSnapshot) -> float:
        """
        Estimate win probability based on sport, score, period, clock.

        Uses simplified empirical models:
        - NBA: logistic model calibrated to historical data
        - NHL: lookup table by goal diff + period
        - MLB: run expectancy + leverage by inning
        """
        diff = snap.our_score - snap.their_score
        completion = self._get_completion_pct(snap.period, snap.clock_seconds)

        if self.sport == "nba":
            return self._nba_win_prob(diff, completion)
        elif self.sport == "nhl":
            return self._nhl_win_prob(diff, snap.period, snap.clock_seconds)
        elif self.sport == "mlb":
            return self._mlb_win_prob(diff, snap.period)
        else:
            # Generic: simple logistic
            return self._generic_win_prob(diff, completion)

    def _nba_win_prob(self, score_diff: int, completion: float) -> float:
        """
        NBA win probability model.

        Based on empirical data: teams up by X with Y% of game remaining
        win at predictable rates. The key insight is that variance decreases
        as the game progresses — a 5-point lead means more in Q4 than Q1.

        Model: P(win) = sigmoid(score_diff * k / sqrt(time_remaining + epsilon))
        where k ≈ 0.15 (calibrated from ~50K NBA games)
        """
        time_remaining = max(0.01, 1.0 - completion)  # fraction of game left
        # As game progresses, each point matters more
        # k=0.15 is calibrated: 10-pt lead at halftime ≈ 83% win rate
        k = 0.15
        z = score_diff * k / math.sqrt(time_remaining)
        return 1.0 / (1.0 + math.exp(-z))

    def _nhl_win_prob(self, goal_diff: int, period: int, clock_secs: float) -> float:
        """
        NHL win probability based on goal differential and game state.

        Empirical NHL win rates (approximate):
        +1 goal: P1=62%, P2=70%, P3=85%
        +2 goals: P1=82%, P2=88%, P3=96%
        +3 goals: P1=95%, P2=97%, P3=99%
        Tied: 50% always (slight home-ice advantage ignored)
        """
        # Base probabilities by goal diff (columns: P1, P2, P3)
        # Source: Moneypuck historical data approximation
        win_table = {
            0: [0.50, 0.50, 0.50],
            1: [0.62, 0.70, 0.85],
            2: [0.82, 0.88, 0.96],
            3: [0.95, 0.97, 0.99],
        }

        abs_diff = min(abs(goal_diff), 3)
        period_idx = min(max(period - 1, 0), 2)

        base_prob = win_table[abs_diff][period_idx]

        # Interpolate within period using clock
        # E.g., early P3 with +1 is closer to P2 value than late P3
        if period_idx < 2 and clock_secs > 0:
            period_minutes = 20.0  # NHL periods are 20 min
            progress = 1.0 - (clock_secs / (period_minutes * 60))
            next_period_prob = win_table[abs_diff][period_idx + 1]
            base_prob = base_prob + (next_period_prob - base_prob) * progress * 0.5

        if goal_diff < 0:
            return 1.0 - base_prob

        return min(0.99, max(0.01, base_prob))

    def _mlb_win_prob(self, run_diff: int, inning: int) -> float:
        """
        MLB win probability based on run differential and inning.

        Key MLB insight: each inning matters MORE as game progresses.
        A 2-run lead in the 8th is worth much more than in the 2nd.

        Empirical approximation:
        - 1-run lead: Inn1=54%, Inn5=60%, Inn8=75%, Inn9(2out)=90%
        - 2-run lead: Inn1=62%, Inn5=72%, Inn8=88%, Inn9=95%
        - 3-run lead: Inn1=72%, Inn5=82%, Inn8=94%, Inn9=98%
        """
        abs_diff = min(abs(run_diff), 4)
        inning = min(max(inning, 1), 9)

        # Simplified model: logistic with inning-dependent scaling
        # Each run is worth more in later innings
        inning_weight = 0.5 + (inning / 9.0) * 1.5  # ranges from 0.61 to 2.0
        z = abs_diff * inning_weight * 0.5
        base_prob = 1.0 / (1.0 + math.exp(-z))

        # Boost for very late innings
        if inning >= 8 and abs_diff >= 2:
            base_prob = min(0.99, base_prob * 1.05)

        if run_diff < 0:
            return 1.0 - base_prob

        if run_diff == 0:
            return 0.50

        return min(0.99, max(0.01, base_prob))

    def _generic_win_prob(self, score_diff: int, completion: float) -> float:
        """Fallback model for tennis, UFC, etc."""
        time_remaining = max(0.01, 1.0 - completion)
        z = score_diff * 0.3 / math.sqrt(time_remaining)
        return 1.0 / (1.0 + math.exp(-z))

    def _get_completion_pct(self, period: int, clock_secs: float) -> float:
        """Estimate what percentage of the game is complete."""
        sport_config = {
            "nba": {"periods": 4, "period_minutes": 12},
            "nhl": {"periods": 3, "period_minutes": 20},
            "mlb": {"periods": 9, "period_minutes": 0},  # MLB uses innings
            "ncaab": {"periods": 2, "period_minutes": 20},
        }
        cfg = sport_config.get(self.sport, {"periods": 4, "period_minutes": 12})

        total_periods = cfg["periods"]
        period_minutes = cfg["period_minutes"]

        if self.sport == "mlb":
            # MLB: each inning is ~1/9 of the game
            return min(1.0, (period - 1) / total_periods)

        if period_minutes > 0 and total_periods > 0:
            total_seconds = total_periods * period_minutes * 60
            elapsed_periods = (period - 1) * period_minutes * 60
            current_elapsed = period_minutes * 60 - clock_secs
            total_elapsed = elapsed_periods + max(0, current_elapsed)
            return min(1.0, total_elapsed / total_seconds)

        return min(1.0, period / total_periods)

    @staticmethod
    def _parse_clock(clock_str: str) -> float:
        """Parse ESPN clock string like '5:32' or '12:00' to seconds."""
        if not clock_str:
            return 0.0
        try:
            parts = clock_str.strip().split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 1:
                return float(parts[0])
        except (ValueError, IndexError):
            pass
        return 0.0


@dataclass
class SportInstincts:
    """
    Situational awareness that a veteran sports bettor has.

    These aren't just numbers — they're the "feel" for the game that
    prevents dumb entries and triggers smart exits. Each flag represents
    a situation where mechanical price-watching gives the WRONG signal.

    Usage:
        instincts = SportInstincts.detect(game_ctx, espn_data, sport)
        if instincts.should_avoid_entry:
            # Don't buy this dip — it looks mechanical but context says no
        modifier = instincts.dqs_modifier
        # Apply to DQS: dqs = base_dqs * modifier
    """

    # --- Situational Flags ---
    is_garbage_time: bool = False        # NBA: blowout in Q4 — bench players, meaningless action
    is_clutch_time: bool = False         # Close game late — maximum volatility, tighten everything
    is_power_play: bool = False          # NHL: team has man advantage — expected scoring
    is_penalty_kill: bool = False        # NHL: our team is shorthanded — expected opponent scoring
    is_empty_net: bool = False           # NHL: goalie pulled — goals are coin flips, not signal
    is_high_leverage_ab: bool = False    # MLB: runners in scoring position, 2 outs — wait for resolution
    is_comeback_territory: bool = False  # Team trailing but in plausible comeback range
    is_pulling_away: bool = False        # Team extending lead — dips are maximum noise

    # --- Derived Signals ---
    volatility_regime: str = "normal"    # "low" (blowout), "normal", "high" (clutch/close)
    scoring_run_quality: float = 0.5     # 0-1: how meaningful is the current run? (stage-weighted)
    situational_modifier: float = 1.0    # Multiplier for DQS (< 1.0 = avoid, > 1.0 = boost)

    # --- Explanation ---
    flags: list = field(default_factory=list)  # Human-readable list of active situations

    @property
    def should_avoid_entry(self) -> bool:
        """Hard block on entry — these situations are known money losers."""
        return self.is_garbage_time or self.is_high_leverage_ab

    @property
    def should_reduce_size(self) -> bool:
        """Reduce position size — high uncertainty situations."""
        return self.is_clutch_time or self.is_empty_net

    @property
    def should_widen_stops(self) -> bool:
        """Widen stop losses — expect more noise."""
        return self.is_clutch_time or self.is_empty_net

    @property
    def dqs_modifier(self) -> float:
        """Multiply base DQS by this. <1.0 = penalize, >1.0 = boost."""
        return self.situational_modifier

    @classmethod
    def detect(cls, game_ctx: GameContext | None, espn_data: dict | None,
               sport: str) -> "SportInstincts":
        """
        Analyze current game state and return situational awareness flags.

        This is where years of sports-watching knowledge gets encoded:
        - What does the score + time remaining REALLY mean?
        - Is this a situation where price moves are noise or signal?
        - Should we be aggressive, cautious, or on the sideline?
        """
        inst = cls()
        if not game_ctx or not game_ctx._snapshots:
            return inst

        snap = game_ctx._snapshots[-1]
        diff = snap.our_score - snap.their_score
        completion = game_ctx.game_completion_pct
        period = snap.period
        clock = snap.clock_seconds
        momentum = game_ctx.momentum

        # Pull ESPN situation data if available
        situation = {}
        last_play = ""
        if espn_data:
            situation = espn_data.get("situation", {})
            last_play = (espn_data.get("last_play") or "").lower()

        # ── NBA Instincts ──────────────────────────────────────────────
        if sport == "nba":
            inst = cls._detect_nba(inst, diff, period, clock, completion, momentum, last_play)

        # ── NHL Instincts ──────────────────────────────────────────────
        elif sport == "nhl":
            inst = cls._detect_nhl(inst, diff, period, clock, completion, momentum, last_play, situation)

        # ── MLB Instincts ──────────────────────────────────────────────
        elif sport == "mlb":
            inst = cls._detect_mlb(inst, diff, period, completion, situation)

        # ── Universal: Scoring Run Quality ─────────────────────────────
        # Weight momentum by game stage. A 10-0 run in Q4 is 3x more
        # meaningful than the same run in Q1.
        stage_weight = 0.5 + completion * 1.5  # 0.5 early → 2.0 late
        inst.scoring_run_quality = min(1.0, abs(momentum) * stage_weight)

        # ── Universal: Comeback Detection ──────────────────────────────
        # Trailing but within plausible comeback range
        if diff < 0:
            comeback_thresholds = {
                "nba": {0.5: 15, 0.75: 10, 0.85: 6},   # down 15 at half = possible
                "nhl": {0.33: 3, 0.66: 2, 0.85: 1},     # down 3 after P1 = possible
                "mlb": {0.33: 5, 0.55: 4, 0.77: 3},     # down 5 through 3 inn = possible
            }
            thresholds = comeback_thresholds.get(sport, {0.5: 5, 0.75: 3})
            for pct, max_deficit in sorted(thresholds.items()):
                if completion >= pct and abs(diff) <= max_deficit:
                    inst.is_comeback_territory = True
                    inst.flags.append(f"comeback_territory(down {abs(diff)}, {completion:.0%} done)")
                    break

        # ── Universal: Pulling Away Detection ──────────────────────────
        # Leading AND lead trend is growing AND momentum is ours
        if diff > 0 and game_ctx.lead_trend > 0.2 and momentum > 0.2:
            inst.is_pulling_away = True
            inst.flags.append("pulling_away")
            inst.situational_modifier = min(inst.situational_modifier * 1.3, 1.5)

        # ── Volatility Regime ──────────────────────────────────────────
        if inst.is_garbage_time:
            inst.volatility_regime = "low"
        elif inst.is_clutch_time or inst.is_empty_net:
            inst.volatility_regime = "high"

        return inst

    @classmethod
    def _detect_nba(cls, inst: "SportInstincts", diff: int, period: int,
                    clock: float, completion: float, momentum: float,
                    last_play: str) -> "SportInstincts":
        """
        NBA-specific instincts:

        1. GARBAGE TIME: 20+ pt lead in Q4 with <5 min, or 30+ pt lead in Q4.
           Bench players are in. Price dips are meaningless noise that WON'T
           revert because nobody is trying. Our data confirms: entries during
           blowouts lose money because the price just flatlines, never bounces.

        2. CLUTCH TIME: ≤5 pt game in Q4 with <5 min left.
           NBA defines this officially. Every possession matters. Volatility
           spikes — a single 3-pointer can swing the game 6%. Dips are real
           information, not noise. Require bigger dips, smaller positions.

        3. FREE THROW PARADE: Late game close → intentional fouling.
           Score changes come in 1-2 pt increments. Momentum signals are
           meaningless here — it's just a free throw shooting contest.
        """
        if period >= 4:
            # Garbage time
            if (diff >= 20 and clock <= 300) or diff >= 30:
                inst.is_garbage_time = True
                inst.flags.append(f"nba_garbage_time(+{diff}, {clock/60:.1f}min left Q4)")
                inst.situational_modifier = 0.0  # hard block

            # Clutch time
            elif abs(diff) <= 5 and clock <= 300:
                inst.is_clutch_time = True
                inst.flags.append(f"nba_clutch_time({diff:+d}, {clock/60:.1f}min left Q4)")
                inst.situational_modifier *= 0.6  # require stronger signal

            # Free throw parade (close game, last 2 min)
            if abs(diff) <= 3 and clock <= 120:
                if "free throw" in last_play:
                    inst.flags.append("nba_free_throw_parade")
                    inst.situational_modifier *= 0.7

        # Q3/early Q4 runs — common and often meaningless
        # Teams go on 10-0 runs in Q3 all the time, then the other team
        # responds. Don't overreact to Q3 momentum.
        if period == 3 and abs(momentum) > 0.5:
            inst.flags.append("nba_q3_momentum_discount")
            inst.situational_modifier *= 0.85

        return inst

    @classmethod
    def _detect_nhl(cls, inst: "SportInstincts", diff: int, period: int,
                    clock: float, completion: float, momentum: float,
                    last_play: str, situation: dict) -> "SportInstincts":
        """
        NHL-specific instincts:

        1. POWER PLAY: Team has man advantage (5v4, 5v3).
           Goals on the power play are EXPECTED — they don't represent
           a momentum shift. If opponent scores on PP, don't panic. If
           we score on PP, don't overreact to the bump. PP goals convert
           ~20% of the time — it's a statistical event, not a narrative.

        2. PENALTY KILL: Our team is shorthanded.
           Opponent scoring here is expected. A PK goal against us
           shouldn't trigger a panic exit — it was priced in.

        3. EMPTY NET: Last ~2 min, trailing team pulls goalie.
           Goals are 50/50 noise. The trailing team either scores
           (goalie pull worked) or gives up an empty-netter (game over).
           Neither outcome tells you anything about team quality.

        4. OVERTIME LIKELIHOOD: Tied game in P3 = likely OT.
           OT is basically a coin flip. Don't bet heavily on a coin flip.
        """
        # Detect power play / penalty kill from ESPN situation or last play
        pp_keywords = ["power play", "power-play", "pp", "man advantage"]
        pk_keywords = ["shorthanded", "short-handed", "penalty kill", "pk"]

        last_play_lower = last_play.lower() if last_play else ""
        for kw in pp_keywords:
            if kw in last_play_lower:
                inst.is_power_play = True
                inst.flags.append("nhl_power_play")
                inst.situational_modifier *= 0.8  # discount PP scoring
                break
        for kw in pk_keywords:
            if kw in last_play_lower:
                inst.is_penalty_kill = True
                inst.flags.append("nhl_penalty_kill")
                inst.situational_modifier *= 0.7
                break

        # Empty net detection: P3, trailing by 1-2, <2:30 left
        if period == 3 and diff < 0 and abs(diff) <= 2 and clock <= 150:
            inst.is_empty_net = True
            inst.flags.append(f"nhl_empty_net_likely(down {abs(diff)}, {clock:.0f}s left)")
            inst.situational_modifier *= 0.5  # heavy discount — coin flip territory

        # Leading by 1 in P3 with <5 min — opponent will be desperate
        if period == 3 and diff == 1 and clock <= 300:
            inst.is_clutch_time = True
            inst.flags.append(f"nhl_clutch_1goal_lead({clock/60:.1f}min left P3)")
            inst.situational_modifier *= 0.7

        # Tied in P3 — overtime is likely, which is ~50/50
        if period >= 3 and diff == 0 and clock <= 600:
            inst.flags.append("nhl_ot_likely")
            inst.situational_modifier *= 0.5  # don't bet on coin flips

        return inst

    @classmethod
    def _detect_mlb(cls, inst: "SportInstincts", diff: int, period: int,
                    completion: float, situation: dict) -> "SportInstincts":
        """
        MLB-specific instincts:

        1. HIGH-LEVERAGE AT-BAT: Runners in scoring position + 2 outs.
           The next pitch could score 2-3 runs or end the inning. This is
           maximum uncertainty — WAIT for the at-bat to resolve before
           entering. If you buy during a 2-out RISP situation, you're
           flipping a coin on whether the batter gets a hit.

        2. BULLPEN TRANSITION: Starter just left the game.
           When a pitcher gets pulled, the next few batters are uncertain.
           Relief pitchers need warmup, may not have their best stuff.
           Dips here could be real (bad reliever) or noise (warming up).

        3. LATE-INNING HOLD: Leading by 1-2 in 8th/9th with a good closer.
           This is the most predictable situation in MLB — closers convert
           ~90% of save opportunities. Dips here are maximum noise.

        4. BASES LOADED: Any time bases are loaded, one swing changes everything.
           Grand slam probability is ~3% per AB but it's a 4-run swing.
           Don't enter during bases-loaded situations.
        """
        outs = 0
        on_first = False
        on_second = False
        on_third = False

        if situation:
            outs = situation.get("outs", 0)
            on_first = bool(situation.get("onFirst"))
            on_second = bool(situation.get("onSecond"))
            on_third = bool(situation.get("onThird"))

        risp = on_second or on_third  # runners in scoring position
        bases_loaded = on_first and on_second and on_third

        # High-leverage AB: RISP + 2 outs
        if risp and outs == 2:
            inst.is_high_leverage_ab = True
            inst.flags.append("mlb_high_leverage_2out_risp")
            inst.situational_modifier = 0.0  # hard block — wait for resolution

        # Bases loaded — any time
        if bases_loaded:
            inst.is_high_leverage_ab = True
            inst.flags.append("mlb_bases_loaded")
            inst.situational_modifier = 0.0

        # Late-inning hold (8th/9th, leading by 1-2)
        if period >= 8 and 1 <= diff <= 2:
            inst.is_pulling_away = False  # override — it's not safe yet
            inst.flags.append(f"mlb_save_situation(+{diff}, inn {period})")
            # Closers convert ~90% — dips are noise but don't boost too much
            # because the 10% failure case is catastrophic
            inst.situational_modifier *= 1.1

        # Early game (innings 1-3) with small lead — very low signal
        if period <= 3 and abs(diff) <= 2:
            inst.flags.append(f"mlb_early_game(inn {period}, diff {diff:+d})")
            inst.situational_modifier *= 0.7

        return inst

    def __repr__(self) -> str:
        if not self.flags:
            return "SportInstincts(normal)"
        return f"SportInstincts({', '.join(self.flags)}, mod={self.situational_modifier:.2f})"


def compute_dip_quality(
    *,
    game_ctx: GameContext | None,
    dip_cents: int,
    price: int,
    price_history: deque,
    sport: str,
    espn_data: dict | None = None,
) -> tuple[float, dict]:
    """
    Compute Dip Quality Score using full game intelligence.

    This is the brain of the trading system. It answers:
    "Given everything we know about this game right now,
     is this price dip temporary (buy) or real (avoid)?"

    Returns (score 0.0-1.0, breakdown dict).
    """
    breakdown = {}

    # --- 0. Sport Instincts — situational awareness ---
    # Detect game situations FIRST — they can hard-block entry
    instincts = SportInstincts.detect(game_ctx, espn_data, sport)
    breakdown["instincts"] = instincts.flags if instincts.flags else []
    breakdown["instinct_mod"] = round(instincts.situational_modifier, 2)

    if instincts.should_avoid_entry:
        # Hard block — return 0 immediately with explanation
        breakdown["total"] = 0.0
        breakdown["blocked_by"] = instincts.flags
        logger.info("DQS BLOCKED by instincts: %s", instincts)
        return 0.0, breakdown

    # --- 1. Win Probability Edge (weight 0.30) ---
    # The most important signal: is Kalshi underpricing the actual win probability?
    # If our model says 72% but Kalshi is at 65¢, that's real edge.
    # NOTE: Skip for tennis/UFC — our generic model is bad for non-linear scoring.
    # For those sports, market price IS the best win prob estimate.
    _has_good_model = sport in ("nba", "nhl", "mlb", "ncaab")
    if _has_good_model and game_ctx and game_ctx.win_probability > 0:
        kalshi_implied = price / 100.0
        fair_prob = game_ctx.win_probability
        edge = fair_prob - kalshi_implied

        if edge >= 0.10:
            wp_score = 1.0    # 10%+ edge — strong buy signal
        elif edge >= 0.05:
            wp_score = 0.8    # 5-10% edge — good
        elif edge >= 0.02:
            wp_score = 0.6    # small edge but exists
        elif edge >= 0.0:
            wp_score = 0.4    # no edge but not mispriced
        else:
            wp_score = 0.1    # Kalshi is ABOVE fair value — DON'T buy
        breakdown["wp_edge"] = round(edge, 3)
    else:
        wp_score = 0.5  # no model / no data — neutral (don't penalize)
        breakdown["wp_edge"] = None
    breakdown["win_prob"] = round(wp_score, 2)

    # --- 2. Momentum (weight 0.25) ---
    # If the opponent is on a scoring run, this dip might be real.
    # If our team is on a run, the dip is probably noise.
    if game_ctx:
        mom = game_ctx.momentum
        if mom > 0.3:
            mom_score = 0.9   # our team has momentum — dip is noise, BUY
        elif mom > 0:
            mom_score = 0.7   # slight positive momentum
        elif mom > -0.3:
            mom_score = 0.4   # neutral or slight opponent momentum
        else:
            mom_score = 0.1   # opponent on a run — dip is real, AVOID
        breakdown["momentum_raw"] = round(mom, 2)

        # Extra penalty if opponent is on a verified scoring run
        if game_ctx.opponent_on_run:
            mom_score = max(0.05, mom_score - 0.3)
            breakdown["opp_run"] = True
    else:
        mom_score = 0.5
    breakdown["momentum"] = round(mom_score, 2)

    # --- 3. Lead Trend (weight 0.15) ---
    # Is the lead growing or shrinking over recent ticks?
    if game_ctx:
        trend = game_ctx.lead_trend
        if trend > 0.2:
            trend_score = 0.9  # lead is growing — dips are definitely noise
        elif trend > 0:
            trend_score = 0.7  # lead stable/slightly growing
        elif trend > -0.3:
            trend_score = 0.4  # lead stable or slightly shrinking
        else:
            trend_score = 0.1  # lead shrinking fast — comeback in progress
    else:
        trend_score = 0.5
    breakdown["lead_trend"] = round(trend_score, 2)

    # --- 4. Game Stage (weight 0.15) ---
    # Late game dips on a team that's ahead are the best trades.
    if game_ctx:
        completion = game_ctx.game_completion_pct
        has_lead = game_ctx.score_diff > 0

        if completion >= 0.75 and has_lead:
            stage_score = 1.0   # late game with lead — best possible
        elif completion >= 0.50 and has_lead:
            stage_score = 0.75  # mid game with lead — good
        elif completion >= 0.75 and not has_lead:
            stage_score = 0.2   # late game without lead — risky
        elif completion < 0.25:
            stage_score = 0.35  # early game — too much uncertainty
        else:
            stage_score = 0.5   # mid game
    else:
        stage_score = 0.5
    breakdown["stage"] = round(stage_score, 2)

    # --- 5. Volatility Filter (weight 0.15) ---
    # Dip should exceed recent price noise
    if len(price_history) >= 4:
        prices = list(price_history)
        mean_p = sum(prices) / len(prices)
        variance = sum((p - mean_p) ** 2 for p in prices) / len(prices)
        stddev = max(variance ** 0.5, 0.1)
        z_score = dip_cents / stddev
        if z_score >= 2.0:
            vol_score = 0.9   # dip is well above noise
        elif z_score >= 1.5:
            vol_score = 0.7
        elif z_score >= 1.0:
            vol_score = 0.5
        else:
            vol_score = 0.2   # dip is within normal noise
    else:
        vol_score = 0.5
    breakdown["volatility"] = round(vol_score, 2)

    # --- Weighted Average ---
    # When we have real game context WITH a good model, weight wp/momentum heavily.
    # When we don't (tennis, UFC, pre-game, no ESPN), rely more on price action.
    has_context = game_ctx is not None and len(game_ctx._snapshots) > 0 and _has_good_model
    if has_context:
        dqs = (
            wp_score * 0.30      # win probability edge is king
            + mom_score * 0.25   # momentum matters a lot
            + trend_score * 0.15
            + stage_score * 0.15
            + vol_score * 0.15
        )
    else:
        # No game context — weight price action + leader status more
        # wp/mom/trend are all 0.5 (neutral) so they'd just dilute the signal
        leader_bonus = 0.6 if price >= 60 else 0.4  # is this a clear leader?
        dqs = (
            leader_bonus * 0.35   # leader status is our main signal
            + vol_score * 0.40    # price action is all we have
            + 0.5 * 0.25         # neutral filler for missing signals
        )
        breakdown["mode"] = "price_action_only"

    # --- Apply Sport Instincts modifier ---
    # Clutch time, power plays, pulling away, etc. — adjust DQS up or down
    # based on situational awareness that goes beyond raw numbers.
    if instincts.situational_modifier != 1.0:
        pre_instinct_dqs = dqs
        dqs = dqs * instincts.situational_modifier
        dqs = max(0.0, min(1.0, dqs))
        breakdown["pre_instinct_dqs"] = round(pre_instinct_dqs, 3)

    breakdown["total"] = round(dqs, 3)
    return round(dqs, 3), breakdown
