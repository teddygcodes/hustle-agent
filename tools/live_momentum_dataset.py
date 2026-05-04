"""Live-momentum research dataset builder (Session 30, Stage 1).

Joins ``bot/state/live_ticks.jsonl`` (+ gzipped archives) with
``bot/state/live_journal.json`` and ``bot/state/clv.json`` into one CSV row
per candidate decision (accept OR tunable reject) for the live_momentum
strategy.

Decision source: ``scan_found`` events from the live journal. Accepts are
identified by a paired ``bet`` event with ``mode='momentum'`` within +-10s of
the scan_found. Tunable rejects are identified by ``skip_reason`` membership
in :data:`bot.clv.LIVE_MOMENTUM_TUNABLE_SKIP_REASONS`. Structural rejects
(``capacity_capped``, ``already_watching``, etc.) are excluded -- same scope
convention as Session 23 counterfactual emission.

Decision-time fields are read as-of the latest live_ticks row with
``ts <= decision_ts``. Outcome columns (forward returns, MFE/MAE in window,
realized PnL, CLV) are computed by walking subsequent ticks / journal / clv.

MFE/MAE NAMING: This module's ``mfe_in_<horizon>s_window_cents`` is
DECISION-anchored over a fixed forward window. This is NOT the same metric
as Session 9's ``bot/tracker.py:mfe_cents`` which is ENTRY-to-SETTLEMENT
ratchet. Different anchoring, different math. The window helper is defined
inline rather than extracted into ``bot/clv.py`` because the entry/settlement
metric in tracker.py would not be a clean refactor target.

CLV math: Reuses ``bot.clv.compute_clv_cents`` -- single source of truth per
Session 13b discipline.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import os
import sys
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.clv import LIVE_MOMENTUM_TUNABLE_SKIP_REASONS  # noqa: E402

logger = logging.getLogger("live_momentum_dataset")

DEFAULT_LIVE_TICKS = "bot/state/live_ticks.jsonl"
DEFAULT_LIVE_TICKS_ARCHIVE = "bot/state/archive"
DEFAULT_LIVE_JOURNAL = "bot/state/live_journal.json"
DEFAULT_CLV = "bot/state/clv.json"
DEFAULT_OUT = "bot/state/research/live_momentum_dataset.csv"

DECISION_TICK_LOOKBACK_SECS = 60
CLV_JOIN_WINDOW_SECS = 60


def required_columns(horizon_secs: int) -> list[str]:
    """Stable column order for the dataset CSV."""
    mfe = f"mfe_in_{horizon_secs}s_window_cents"
    mae = f"mae_in_{horizon_secs}s_window_cents"
    return [
        "decision_id",
        "decision_ts",
        "ticker",
        "match",
        "sport",
        "accept",
        "skip_reason",
        "leader_side",
        "leader_price",
        "spread_cents",
        "wp",
        "wp_edge",
        "momentum",
        "lead_trend",
        "dip",
        "dqs",
        "period",
        "score_diff",
        "completion",
        "elapsed",
        "volatility",
        "leader",
        "opp_leader",
        "recent_high",
        "opp_recent_high",
        "regime_time_of_day",
        "regime_day_of_week",
        "regime_sport_phase",
        "regime_event_horizon_hr",
        "fwd_return_30s_cents",
        "fwd_return_60s_cents",
        "fwd_return_120s_cents",
        mfe,
        mae,
        "outcome_clv_cents",
        "outcome_clv_relative",
        "outcome_realized_pnl",
        "outcome_target_yes_price_cents",
        "outcome_settlement",
    ]


def parse_ts(s: object) -> Optional[datetime]:
    """Parse ISO8601 timestamp; return None on failure."""
    if not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _open_maybe_gz(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def load_ticks(
    days: int,
    now: Optional[datetime] = None,
    live_path: str = DEFAULT_LIVE_TICKS,
    archive_dir: str = DEFAULT_LIVE_TICKS_ARCHIVE,
) -> Iterator[dict]:
    """Yield raw tick dicts from the current jsonl + gzipped archive within window.

    Schema-tolerant: missing fields surface as None in downstream rows. Pre-
    Session-23 archives may not carry ``wp_edge``/``momentum``/``lead_trend``.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    paths: list[Path] = []
    live = Path(live_path)
    if live.exists():
        paths.append(live)
    arc = Path(archive_dir)
    if arc.exists():
        for p in sorted(arc.glob("live_ticks-*.jsonl.gz")):
            paths.append(p)
        for p in sorted(arc.glob("live_ticks-*.jsonl")):
            paths.append(p)

    for path in paths:
        try:
            with _open_maybe_gz(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_ts(row.get("ts"))
                    if ts is None or ts < cutoff:
                        continue
                    row["_ts"] = ts
                    yield row
        except OSError as exc:
            logger.warning("Failed reading %s: %s", path, exc)


def load_journal_events(
    days: int,
    now: Optional[datetime] = None,
    journal_path: str = DEFAULT_LIVE_JOURNAL,
) -> list[dict]:
    """Read live_journal.json as a JSON array; filter to relevant events in window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    p = Path(journal_path)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed reading %s: %s", p, exc)
        return []
    if not isinstance(data, list):
        return []
    out = []
    for ev in data:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") not in {"scan_found", "bet", "exit", "session_end"}:
            continue
        ts = parse_ts(ev.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        ev["_ts"] = ts
        out.append(ev)
    return out


def load_clv_records(
    days: int,
    now: Optional[datetime] = None,
    clv_path: str = DEFAULT_CLV,
) -> list[dict]:
    """Read clv.json filtered to opp_type == 'live_momentum' within window."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    p = Path(clv_path)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed reading %s: %s", p, exc)
        return []
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        if r.get("opp_type") != "live_momentum":
            continue
        anchor_ts = parse_ts(r.get("scan_event_ts") or r.get("recorded_at"))
        if anchor_ts is None or anchor_ts < cutoff:
            continue
        r["_anchor_ts"] = anchor_ts
        out.append(r)
    return out


@dataclass
class CandidateDecision:
    ticker: str
    decision_ts: datetime
    accept: bool
    skip_reason: Optional[str]
    bet_event: Optional[dict] = None
    sport: Optional[str] = None
    match: Optional[str] = None


def candidate_decisions(events: list[dict]) -> Iterator[CandidateDecision]:
    """Yield CandidateDecision per accept (bet) or tunable reject (scan_found).

    Accept = ``bet`` event with mode='momentum'. decision_ts = bet timestamp.
    These come from live_watcher's tick-eval entry path; they are NOT paired
    with scanner-emitted scan_found events (those are upstream first-find
    signals; the live_watcher tick eval is the actual decision moment).

    Tunable reject = scan_found event with skip_reason in
    LIVE_MOMENTUM_TUNABLE_SKIP_REASONS. These come from live_watcher's
    tick-eval rejection path. Structural rejects (capacity_capped,
    already_watching, settled, not_today, bad_event_shape, recently_watched)
    are excluded -- same scope convention as Session 23 CFs.
    """
    for ev in events:
        et = ev.get("event")
        ticker = ev.get("ticker")
        ts = ev.get("_ts")
        if not ticker or ts is None:
            continue
        if et == "bet" and ev.get("mode") == "momentum":
            yield CandidateDecision(
                ticker=ticker,
                decision_ts=ts,
                accept=True,
                skip_reason=None,
                bet_event=ev,
                sport=ev.get("sport"),
                match=ev.get("match"),
            )
        elif et == "scan_found":
            skip = ev.get("skip_reason")
            if skip in LIVE_MOMENTUM_TUNABLE_SKIP_REASONS:
                yield CandidateDecision(
                    ticker=ticker,
                    decision_ts=ts,
                    accept=False,
                    skip_reason=skip,
                    sport=ev.get("sport"),
                    match=ev.get("match"),
                )


def index_ticks_by_ticker(ticks: Iterable[dict]) -> dict[str, list[dict]]:
    """Group ticks by ticker, sorted ascending by ts. ``_ts`` must be present."""
    out: dict[str, list[dict]] = {}
    for t in ticks:
        if "_ts" not in t or not t.get("ticker"):
            continue
        out.setdefault(t["ticker"], []).append(t)
    for lst in out.values():
        lst.sort(key=lambda r: r["_ts"])
    return out


def find_decision_tick(
    ticks_for_ticker: list[dict], decision_ts: datetime
) -> Optional[dict]:
    """Return the latest tick with ts <= decision_ts within DECISION_TICK_LOOKBACK_SECS.

    Returns None if no tick qualifies. ``ticks_for_ticker`` MUST be sorted by ts.
    """
    if not ticks_for_ticker:
        return None
    keys = [t["_ts"] for t in ticks_for_ticker]
    idx = bisect_right(keys, decision_ts) - 1
    if idx < 0:
        return None
    cand = ticks_for_ticker[idx]
    if (decision_ts - cand["_ts"]).total_seconds() > DECISION_TICK_LOOKBACK_SECS:
        return None
    return cand


def _price_for_side(tick: dict, side: str) -> Optional[int]:
    """Return the side-relevant entry-equivalent price (yes_ask analog) on the tick."""
    if side == "yes":
        v = tick.get("price")
    else:
        v = tick.get("opp_price")
    return int(v) if isinstance(v, (int, float)) else None


def _spread_for_side(tick: dict, side: str) -> Optional[int]:
    if side == "yes":
        a, b = tick.get("price"), tick.get("bid")
    else:
        a, b = tick.get("opp_price"), tick.get("opp_bid")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return int(a) - int(b)
    return None


def compute_forward_returns(
    ticks_for_ticker: list[dict],
    decision_ts: datetime,
    decision_price: int,
    side: str,
) -> dict[str, Optional[float]]:
    """Forward return at +30s, +60s, +120s. None if no tick reached that horizon.

    Return = (price_at_horizon - decision_price), in cents. Side-aware: yes
    side uses ``price``; no side uses ``opp_price``. Sign convention: positive
    when the price moved UP for the yes side (or down for no, since ``opp_price``
    is the no leg's own ask).
    """
    horizons = (30, 60, 120)
    keys = [t["_ts"] for t in ticks_for_ticker]
    out: dict[str, Optional[float]] = {}
    for h in horizons:
        target = decision_ts + timedelta(seconds=h)
        idx = bisect_right(keys, target) - 1
        if idx < 0 or ticks_for_ticker[idx]["_ts"] < decision_ts:
            out[f"fwd_return_{h}s_cents"] = None
            continue
        last = ticks_for_ticker[idx]
        p = _price_for_side(last, side)
        if p is None:
            out[f"fwd_return_{h}s_cents"] = None
            continue
        out[f"fwd_return_{h}s_cents"] = float(p - decision_price)
    return out


def compute_mfe_in_window(
    ticks_for_ticker: list[dict],
    decision_ts: datetime,
    decision_price: int,
    side: str,
    horizon_secs: int,
) -> tuple[Optional[float], Optional[float]]:
    """Decision-anchored MFE/MAE over (decision_ts, decision_ts + horizon].

    Different from Session 9's ``bot/tracker.py:mfe_cents`` which is anchored
    entry-to-settlement. Names use ``..._in_<horizon>s_window_cents`` to keep
    the two namespaces from colliding in downstream analysis.
    """
    end = decision_ts + timedelta(seconds=horizon_secs)
    favorable = None
    adverse = None
    saw_any = False
    for t in ticks_for_ticker:
        if t["_ts"] <= decision_ts:
            continue
        if t["_ts"] > end:
            break
        p = _price_for_side(t, side)
        if p is None:
            continue
        saw_any = True
        diff = float(p - decision_price)
        if favorable is None or diff > favorable:
            favorable = diff
        if adverse is None or diff < adverse:
            adverse = diff
    if not saw_any:
        return None, None
    mae_magnitude = max(0.0, -adverse) if adverse is not None else 0.0
    mfe_magnitude = max(0.0, favorable) if favorable is not None else 0.0
    return mfe_magnitude, mae_magnitude


def join_clv(
    clv_records: list[dict],
    ticker: str,
    decision_ts: datetime,
    accept: bool,
    bet_event: Optional[dict],
) -> Optional[dict]:
    """Find the matching clv record within CLV_JOIN_WINDOW_SECS of the decision.

    Accepts: match by ticker + recorded_at within window of bet timestamp.
    Rejects: match by ticker + scan_event_ts within window of decision_ts.
    """
    anchor = bet_event["_ts"] if (accept and bet_event) else decision_ts
    best = None
    best_delta = None
    for r in clv_records:
        if r.get("ticker") != ticker:
            continue
        if accept:
            if r.get("status") not in {"open", "settled", "paper"}:
                continue
        else:
            if r.get("status") not in {"counterfactual_open", "counterfactual_settled"}:
                continue
        rts = r.get("_anchor_ts")
        if rts is None:
            continue
        delta = abs((rts - anchor).total_seconds())
        if delta > CLV_JOIN_WINDOW_SECS:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = r
    return best


def _regime_tags(decision_ts: datetime, ticker: str) -> dict[str, Optional[str]]:
    try:
        from bot.regime import tag as regime_tag
    except ImportError:
        return {
            "regime_time_of_day": None,
            "regime_day_of_week": None,
            "regime_sport_phase": None,
            "regime_event_horizon_hr": None,
        }
    try:
        r = regime_tag(decision_ts, ticker, None)
    except Exception:  # noqa: BLE001 - regime tagger should never crash the dataset
        return {
            "regime_time_of_day": None,
            "regime_day_of_week": None,
            "regime_sport_phase": None,
            "regime_event_horizon_hr": None,
        }
    return {
        "regime_time_of_day": r.get("time_of_day"),
        "regime_day_of_week": r.get("day_of_week"),
        "regime_sport_phase": r.get("sport_phase"),
        "regime_event_horizon_hr": r.get("event_horizon_hr"),
    }


def _exit_pnl_for_bet(events: list[dict], ticker: str, bet_ts: datetime) -> Optional[float]:
    """Find the matching exit event for a bet (same ticker, after bet_ts)."""
    best_exit = None
    for ev in events:
        if ev.get("event") != "exit" or ev.get("ticker") != ticker:
            continue
        if ev["_ts"] < bet_ts:
            continue
        if best_exit is None or ev["_ts"] < best_exit["_ts"]:
            best_exit = ev
    if best_exit is None:
        return None
    pnl = best_exit.get("pnl")
    if isinstance(pnl, (int, float)):
        return float(pnl)
    return None


def _settlement_label(clv: Optional[dict]) -> Optional[str]:
    if clv is None:
        return None
    mr = clv.get("market_result")
    if mr == "yes":
        return "yes_won"
    if mr == "no":
        return "no_won"
    return None


def _cf_only_row(cf: dict, horizon_secs: int) -> dict:
    """Build a dataset row for a counterfactual that lacks tick context.

    Session 30-followup-2: live_momentum CFs come from match-level pre-watcher
    gates (no_leader, low_volume, no_vol_growth_*) so ``live_ticks.jsonl`` has
    nothing for the ticker. We emit the row with populated identity / regime /
    outcome columns sourced directly from the CF, and ``None`` for every
    decision-time feature that would have come from a tick. Forward returns
    and MFE/MAE-in-window are also ``None`` because they require subsequent
    ticks for the ticker (which by definition do not exist).

    Downstream bucket analysis must treat ``leader_price``-bucketed slices as
    populated (CF carries entry_price_cents) but tick-feature slices (wp,
    momentum, dip, dqs, etc.) as a separate "no_tick_context" cohort.
    """
    mfe_col = f"mfe_in_{horizon_secs}s_window_cents"
    mae_col = f"mae_in_{horizon_secs}s_window_cents"

    scan_ts = parse_ts(cf.get("scan_event_ts") or cf.get("recorded_at"))
    ticker = cf.get("ticker") or ""
    decision_iso = scan_ts.isoformat() if scan_ts else (cf.get("scan_event_ts") or "")

    leader_price = cf.get("entry_price_cents")
    if isinstance(leader_price, (int, float)):
        leader_price = int(leader_price)
    else:
        leader_price = None

    side = cf.get("side") or None
    regime = _regime_tags(scan_ts, ticker) if scan_ts else {
        "regime_time_of_day": None,
        "regime_day_of_week": None,
        "regime_sport_phase": None,
        "regime_event_horizon_hr": None,
    }

    target_yes_price: Optional[float] = None
    if cf.get("status") in {"counterfactual_settled", "settled"}:
        tv = cf.get("closing_yes_price")
        if isinstance(tv, (int, float)):
            target_yes_price = float(tv)

    return {
        "decision_id": f"{ticker}-{decision_iso}",
        "decision_ts": decision_iso,
        "ticker": ticker,
        "match": None,
        "sport": cf.get("sport"),
        "accept": False,
        "skip_reason": cf.get("skipped_by_gate"),
        "leader_side": side,
        "leader_price": leader_price,
        "spread_cents": None,
        "wp": None,
        "wp_edge": None,
        "momentum": None,
        "lead_trend": None,
        "dip": None,
        "dqs": None,
        "period": None,
        "score_diff": None,
        "completion": None,
        "elapsed": None,
        "volatility": None,
        "leader": None,
        "opp_leader": None,
        "recent_high": None,
        "opp_recent_high": None,
        **regime,
        "fwd_return_30s_cents": None,
        "fwd_return_60s_cents": None,
        "fwd_return_120s_cents": None,
        mfe_col: None,
        mae_col: None,
        "outcome_clv_cents": cf.get("clv_cents"),
        "outcome_clv_relative": cf.get("clv_relative"),
        "outcome_realized_pnl": None,
        "outcome_target_yes_price_cents": target_yes_price,
        "outcome_settlement": _settlement_label(cf),
    }


def build_decision_rows(
    ticks: Iterable[dict],
    journal: list[dict],
    clv: list[dict],
    horizon_secs: int = 120,
) -> Iterator[dict]:
    """Top-level join. One row per accepted-or-tunable-rejected scan_found.

    Session 30-followup-2: ALSO emits one row per live_momentum counterfactual
    in ``clv`` that wasn't already covered by a journal-driven candidate. CFs
    come from pre-watcher match-level gates, so the ticker often has no ticks
    and the journal-driven path drops it. Without this fallback the dataset
    misses ~74% of LM CFs (340 in clv.json -> 80 reject rows pre-fix).
    """
    by_ticker = index_ticks_by_ticker(ticks)
    mfe_col = f"mfe_in_{horizon_secs}s_window_cents"
    mae_col = f"mae_in_{horizon_secs}s_window_cents"

    claimed_cf_ids: set[str] = set()

    for cand in candidate_decisions(journal):
        ticks_for = by_ticker.get(cand.ticker, [])
        decision_tick = find_decision_tick(ticks_for, cand.decision_ts)
        if decision_tick is None:
            # Journal-driven path can't build a tick-rich row. For rejects, try
            # to recover via a directly-matched CF so we still emit one row with
            # null decision-time features.
            if not cand.accept:
                cf_match = join_clv(clv, cand.ticker, cand.decision_ts, False, None)
                if cf_match and (cf_match.get("status") or "").startswith("counterfactual"):
                    tid = cf_match.get("trade_id")
                    if tid and tid not in claimed_cf_ids:
                        claimed_cf_ids.add(tid)
                        yield _cf_only_row(cf_match, horizon_secs)
            continue

        # Determine leader side: prefer bet event side for accepts; else infer
        # from decision tick's ``leader`` flag (True=yes leg leading).
        if cand.accept and cand.bet_event:
            side = cand.bet_event.get("side") or "yes"
        else:
            side = "yes" if decision_tick.get("leader") else "no"

        decision_price = _price_for_side(decision_tick, side)
        if decision_price is None:
            continue

        spread = _spread_for_side(decision_tick, side)
        fwd = compute_forward_returns(ticks_for, cand.decision_ts, decision_price, side)
        mfe, mae = compute_mfe_in_window(
            ticks_for, cand.decision_ts, decision_price, side, horizon_secs
        )
        clv_match = join_clv(clv, cand.ticker, cand.decision_ts, cand.accept, cand.bet_event)
        if (
            clv_match
            and not cand.accept
            and (clv_match.get("status") or "").startswith("counterfactual")
        ):
            tid = clv_match.get("trade_id")
            if tid:
                claimed_cf_ids.add(tid)
        regime = _regime_tags(cand.decision_ts, cand.ticker)

        realized_pnl: Optional[float] = None
        if cand.accept and cand.bet_event:
            realized_pnl = _exit_pnl_for_bet(journal, cand.ticker, cand.bet_event["_ts"])

        target_yes_price = None
        if clv_match and clv_match.get("status") in {
            "counterfactual_settled",
            "settled",
        }:
            tv = clv_match.get("closing_yes_price")
            if isinstance(tv, (int, float)):
                target_yes_price = float(tv)

        row = {
            "decision_id": f"{cand.ticker}-{cand.decision_ts.isoformat()}",
            "decision_ts": cand.decision_ts.isoformat(),
            "ticker": cand.ticker,
            "match": cand.match or decision_tick.get("match"),
            "sport": cand.sport or decision_tick.get("sport"),
            "accept": cand.accept,
            "skip_reason": cand.skip_reason,
            "leader_side": side,
            "leader_price": decision_price,
            "spread_cents": spread,
            "wp": decision_tick.get("wp"),
            "wp_edge": decision_tick.get("wp_edge"),
            "momentum": decision_tick.get("momentum"),
            "lead_trend": decision_tick.get("lead_trend"),
            "dip": decision_tick.get("dip"),
            # DQS not stored on tick rows in current schema. Leave None until
            # a future bot change emits it on each tick or a back-derivation
            # path is wired in. Documented in module docstring above.
            "dqs": None,
            "period": decision_tick.get("period"),
            "score_diff": decision_tick.get("score_diff"),
            "completion": decision_tick.get("completion"),
            "elapsed": decision_tick.get("elapsed"),
            "volatility": decision_tick.get("volatility"),
            "leader": decision_tick.get("leader"),
            "opp_leader": decision_tick.get("opp_leader"),
            "recent_high": decision_tick.get("recent_high"),
            "opp_recent_high": decision_tick.get("opp_recent_high"),
            **regime,
            "fwd_return_30s_cents": fwd["fwd_return_30s_cents"],
            "fwd_return_60s_cents": fwd["fwd_return_60s_cents"],
            "fwd_return_120s_cents": fwd["fwd_return_120s_cents"],
            mfe_col: mfe,
            mae_col: mae,
            "outcome_clv_cents": (clv_match or {}).get("clv_cents"),
            "outcome_clv_relative": (clv_match or {}).get("clv_relative"),
            "outcome_realized_pnl": realized_pnl,
            "outcome_target_yes_price_cents": target_yes_price,
            "outcome_settlement": _settlement_label(clv_match),
        }
        yield row

    # Session 30-followup-2: emit CF-only rows for any live_momentum CF not
    # already represented by a journal-driven candidate. trade_id is the
    # idempotency key (Session 8 contract). Each yielded row carries null
    # decision-time features and (where settled) populated outcome columns.
    for cf in clv:
        if cf.get("opp_type") != "live_momentum":
            continue
        if not (cf.get("status") or "").startswith("counterfactual"):
            continue
        tid = cf.get("trade_id")
        if not tid or tid in claimed_cf_ids:
            continue
        claimed_cf_ids.add(tid)
        yield _cf_only_row(cf, horizon_secs)


def write_csv(rows: Iterable[dict], out_path: str, horizon_secs: int) -> int:
    """Write rows to CSV. Returns number of rows written."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = required_columns(horizon_secs)
    n = 0
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            normalized = {
                k: ("" if v is None else v)
                for k, v in row.items()
            }
            w.writerow(normalized)
            n += 1
    return n


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--horizon-secs", type=int, default=120)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--ticks", default=DEFAULT_LIVE_TICKS)
    parser.add_argument("--archive-dir", default=DEFAULT_LIVE_TICKS_ARCHIVE)
    parser.add_argument("--journal", default=DEFAULT_LIVE_JOURNAL)
    parser.add_argument("--clv", default=DEFAULT_CLV)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.quiet:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    now = datetime.now(timezone.utc)
    ticks = list(load_ticks(args.days, now, args.ticks, args.archive_dir))
    journal = load_journal_events(args.days, now, args.journal)
    clv = load_clv_records(args.days, now, args.clv)

    rows = list(build_decision_rows(ticks, journal, clv, args.horizon_secs))
    n = write_csv(rows, args.out, args.horizon_secs)
    if not args.quiet:
        accepts = sum(1 for r in rows if r["accept"])
        rejects = n - accepts
        print(
            f"wrote {n} rows to {args.out} "
            f"({accepts} accept, {rejects} tunable-reject) "
            f"from {len(ticks)} ticks / {len(journal)} journal events / {len(clv)} clv"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
