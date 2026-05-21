"""
Closing-Line Value (CLV) Tracker

Records the Kalshi price at trade entry, then compares to the closing price
when the market settles. Positive CLV = you beat the closing line = your
edge detection is working. Negative CLV = the market disagreed with you =
stop trading that strategy until you understand why.

All values in YES-price space (cents) for consistency:
  - YES trades:  CLV = closing_yes_price - entry_yes_price
  - NO trades:   CLV = entry_yes_price - closing_yes_price
  (positive CLV always means "market moved in my favor")

CLV is tracked per strategy type so you can see which edges are real.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bot.regime import tag as regime_tag

logger = logging.getLogger("glint.clv")

# Imported at call-site to avoid circular imports
_CLV_FILE: Optional[Path] = None

# Session 151 (2026-05-18): cap per-call work in check_clv_settlements to
# bound runtime inside the 900s S150 outer guard. The unbounded sequential
# iteration over every open CLV record, gated by the S146 Kalshi rate
# limiter (1 call per ~3s sustained: 20 tokens / 60s, 2 concurrent), takes
# ~3 * N seconds. With N > 300 the guard fires every iteration and leaks
# an executor thread (Python concurrent.futures can't cancel running
# threads); leaked threads continue consuming rate-limit slots and slow
# every subsequent _main_loop call. 200 keeps the worst-case wall-clock at
# ~600s, inside the 900s budget with margin. Oldest-recorded-first FIFO
# drains the backlog naturally — older records are most likely already
# settled in Kalshi's books and just need a get_market round-trip to
# confirm. Records past the cap roll over to the next call.
_CHECK_CLV_BATCH_SIZE = 200

# Session 152 (2026-05-21): batch settlement groups open records by event_ticker
# and fetches each event's settled markets in one get_markets call (vs one
# get_market per record). Phase 0.4 chose "event": a full drain is 274 event
# calls (~1 page each) vs ~4,000 per-record calls, and event grouping bounds
# pagination — series grouping risked deep historical settled sets on sports
# series (KXATPMATCH spans 74 events). Also breaks the S151 oldest-first FIFO
# starvation where ~1,200 long-dated season futures clogged the queue head.
_CLV_BATCH_GROUP_BY = "event"


def _event_ticker(ticker: str) -> Optional[str]:
    """Derive the Kalshi event_ticker from a market ticker
    (KXHIGHMIA-26APR24-T80 -> KXHIGHMIA-26APR24). Returns None for tickers with
    fewer than 3 segments (one-offs / malformed like KXHIGHDEN-A) so the caller
    routes them to the per-record fallback."""
    if not ticker or ticker.count("-") < 2:
        return None
    return ticker.rsplit("-", 1)[0]


def _fetch_settled_markets(group_value: str) -> dict:
    """Return {ticker: market} for every settled market in the group, paginated
    defensively (events are ~1 page). Mirrors the get_markets cursor walk in
    bot/kalshi_series._fetch_series_markets and bot/position_monitor.py:415."""
    from agent.kalshi_client import get_markets
    out: dict = {}
    cursor = None
    param = "event_ticker" if _CLV_BATCH_GROUP_BY == "event" else "series_ticker"
    while True:
        kwargs = {param: group_value, "status": "settled", "limit": 100}
        if cursor:
            kwargs["cursor"] = cursor
        resp = get_markets(**kwargs)
        if not isinstance(resp, dict) or "error" in resp:
            err = resp.get("error") if isinstance(resp, dict) else resp
            logger.warning("CLV batch fetch failed for %s: %s", group_value, err)
            break
        batch = resp.get("markets", [])
        for m in batch:
            if m.get("ticker"):
                out[m["ticker"]] = m
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
    return out


# Session 152: prune records whose market will never produce a binary CLV so the
# settler stops re-polling them every cycle. Two routes — provable-by-shape (no
# API call) and confirmed-404 (per-record fallback). Phase 0 (2026-05-21) found
# ~1,070 such records inflating the open set: KXHIGHNY-26APR* (995, partial date,
# no day) + KXTEST* (69) + a handful of 2-segment one-offs (KXHIGHDEN-A).
_DEAD_EVENT_DATE = re.compile(r"^\d{2}[A-Z]{3}$")   # YYMON with no day -> not a real event


def _is_dead_ticker(ticker: str) -> bool:
    """True for tickers that provably can't be a real Kalshi market: a 3+-segment
    ticker whose event date segment lacks a day (KXHIGHNY-26APR-70 -> event
    KXHIGHNY-26APR; Phase 0 confirmed 0 settled + 404). This also catches the
    KXTEST-26APR* pollution. Excludes season futures (SERIES-YY) and dated markets
    (SERIES-YYMONDD-...); 2-segment one-offs (KXHIGHDEN-A, KXTEST-A) fall through
    to the per-record fallback where a confirmed 404 terminal-marks them. We match
    on shape, not a "KXTEST" prefix, so benign KXTEST-* fixtures still settle."""
    if not ticker:
        return False
    parts = ticker.split("-")
    return len(parts) >= 3 and bool(_DEAD_EVENT_DATE.match(parts[1]))


def _mark_unsettleable(rec: dict, note: str) -> None:
    """Terminal status for a record that will never produce a binary CLV (confirmed
    404 / malformed ticker). Forward-only status, excluded from the open-candidate
    filter and from get_clv_report (which filters settled / counterfactual_settled),
    so the record stops being polled every cycle."""
    rec["status"] = "settlement_failed"
    rec["settlement_note"] = note
    rec["settled_at"] = datetime.now(timezone.utc).isoformat()


def _get_file() -> Path:
    global _CLV_FILE
    if _CLV_FILE is None:
        from bot.config import CLV_FILE
        _CLV_FILE = CLV_FILE
    return _CLV_FILE


def _active_strategies() -> set[str]:
    # Scanner-driven strategies + live_momentum (runs via live_watcher,
    # not in ACTIVE_STRATEGIES). Drop any record whose opp_type isn't here —
    # disabled-strategy CLV records are noise (Apr 23 Session 5).
    from bot.config import ACTIVE_STRATEGIES
    return set(ACTIVE_STRATEGIES) | {"live_momentum"}


def compute_clv_cents(
    side: str,
    entry_price_cents: int,
    closing_yes_price: float,
) -> tuple[float, float]:
    """Compute closing-line value in cents + relative for one trade.

    Pure function, no I/O. Single source of truth for CLV math — used by
    bot.clv.check_clv_settlements (live settler) and tools/backtest.py
    (Session 13b offline back-tester). Back-tester divergence here means
    the back-tester is wrong, not live.

    Args:
        side: "yes" or "no" — direction of the trade.
        entry_price_cents: Integer cents (1-99) paid at entry.
        closing_yes_price: Final YES price in cents (0.0-100.0). 100 if YES
            won, 0 if NO won at settlement; mid-price for partial updates.

    Returns:
        (clv_cents, clv_relative): rounded to 2dp and 4dp respectively to
        match legacy storage format. clv_relative is 0.0 when entry <= 0
        (defensive — pre-Session-6 records may have slipped through).
    """
    if side == "yes":
        clv_cents = closing_yes_price - entry_price_cents
    else:
        # NO trade: implicit YES price = 100 - entry. Positive CLV when YES
        # moved AWAY from us (down) — good for NO holder.
        our_implied_yes = 100 - entry_price_cents
        clv_cents = our_implied_yes - closing_yes_price

    clv_relative = clv_cents / entry_price_cents if entry_price_cents > 0 else 0.0
    return round(clv_cents, 2), round(clv_relative, 4)


def _apply_clv_settlement(rec, result, closing_yes, is_cf, positions_by_order, settled_now):
    """Write all settlement-time fields onto a settled record.

    Session 152: single source of truth for the settlement write so the batch
    path and the per-record fallback in check_clv_settlements stay byte-identical
    (same discipline as compute_clv_cents). Preserves S13b CLV math, S11
    prediction-close propagation, S9 mfe/mae/ticks, and the S16 mfe extension.
    Caller adds rec's trade_id to `mutated_ids` after this returns (S155).
    """
    # Session 13b: single source of truth — back-tester calls the same fn.
    clv_cents_rounded, clv_relative_rounded = compute_clv_cents(
        rec["side"], rec["entry_price_cents"], closing_yes,
    )

    rec["status"] = "counterfactual_settled" if is_cf else "settled"
    rec["closing_yes_price"] = closing_yes
    rec["market_result"] = result
    rec["clv_cents"] = clv_cents_rounded
    rec["clv_relative"] = clv_relative_rounded
    rec["settled_at"] = datetime.now(timezone.utc).isoformat()

    # Session 11: propagate closing price to predictions.jsonl rows that
    # match this clv record by ticker + recorded_at ±60s. Fires for both
    # real trades and counterfactuals (both sides emit predictions).
    try:
        from bot.calibration import update_prediction_close
        update_prediction_close(
            ticker=rec["ticker"],
            recorded_at=rec.get("recorded_at", ""),
            closing_yes_price=closing_yes,
        )
    except Exception:
        logger.exception("calibration update_prediction_close failed (non-fatal)")

    if not is_cf:
        pos = positions_by_order.get(rec.get("trade_id"))
        if pos is not None:
            for key in ("mfe_cents", "mae_cents", "mfe_at", "mae_at", "ticks_observed"):
                if pos.get(key) is not None:
                    rec[key] = pos[key]
        # Session 16: extend mfe_cents to include the settlement event so
        # gap = mfe_cents - clv_cents is ≥ 0 in the excursion report.
        # mfe_cents from positions caps at the highest *observed bid*
        # during open life (yes_bid ≤ 99 for winning YES, no_bid ≤ 99 for
        # winning NO). clv_cents at settlement uses the payout value
        # (100/0). Without this max, every winning held-to-settlement
        # position has a structural -1¢ gap that obscures the "exit
        # logic" signal. After this max, gap > 0 truly means "MFE during
        # open life exceeded the eventual exit-favorable" — i.e. the bot
        # peaked and gave it back. The clamp to ≥ 0 preserves the MFE
        # convention (non-negative magnitude); for losers clv_cents is
        # negative and the ratchet is a no-op.
        mfe_with_settlement = max(0, int(round(clv_cents_rounded)))
        existing_mfe = rec.get("mfe_cents")
        if existing_mfe is None or mfe_with_settlement > existing_mfe:
            rec["mfe_cents"] = mfe_with_settlement
            rec["mfe_at"] = rec.get("settled_at") or datetime.now(timezone.utc).isoformat()
        settled_now.append(rec.copy())
        logger.info(
            f"CLV settled: {rec['ticker']} | {rec['side'].upper()} entry={rec['entry_price_cents']}¢ "
            f"close={closing_yes}¢ | CLV={clv_cents_rounded:+.1f}¢ ({clv_relative_rounded:+.1%}) | "
            f"result={result}"
        )


def _load() -> list[dict]:
    f = _get_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    active = _active_strategies()
    return [r for r in data if r.get("opp_type") in active]


def _save(records: list[dict]):
    f = _get_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2, default=str))
    tmp.rename(f)


def _persist_progress(records: list[dict], mutated_ids: set) -> None:
    """Session 155: re-load clv.json fresh and re-apply our mutated records
    (matched by trade_id), then atomic-save. Used for the dead-mark-first
    persist, incremental persist, and final persist in check_clv_settlements
    so a mid-cycle abort by the S150 900s outer guard never discards drained
    progress (the S155 drain-failure root cause: the old single end-of-cycle
    _save was thrown away on every 900s abort).

    Re-load-merge — NOT a plain _save(records) — because clv.json is written
    WITHOUT state_io's shared lock (clv._save is a bespoke tmp+rename) and
    live_watcher.record_live_momentum_counterfactual_skip appends concurrently.
    Every clv.json writer is append-only or mutates distinct records, so
    replacing only our mutated trade_ids onto a fresh read preserves those
    concurrent appends. The residual sub-second load→save race is pre-existing
    (the old end-of-cycle _save had the same window) and low-severity (the only
    concurrent writer is a rare, daily-capped CF append; tmp+rename rules out
    torn writes)."""
    if not mutated_ids:
        return
    mine = {
        r.get("trade_id"): r
        for r in records
        if r.get("trade_id") in mutated_ids
    }
    fresh = _load()
    _save([mine.get(r.get("trade_id"), r) for r in fresh])


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record_counterfactual_skip(opp: dict, gate: str, scan_id: str) -> None:
    """Record a 'what would the closing price have been if we'd taken this' entry.

    Session 6 (Apr 24): top-N rejected opportunities per scan get a CF record
    so we can later answer "did this gate cost us money?" — the same settlement
    poller fills closing_yes_price for these and `tools/cohort_report.py`
    joins gate ↔ CLV to surface mis-tuned thresholds.

    Idempotent on (scan_id, ticker). Records carry status="counterfactual_open"
    until settled; `get_clv_report()` filters on status=="settled" so CF rows
    do not pollute paper-trade CLV reports.
    """
    side = opp.get("side") or opp.get("recommended_side") or "yes"
    side = str(side).lower()

    entry_price = opp.get("price_cents")
    if entry_price is None:
        entry_price = opp.get("yes_ask")
    try:
        entry_price = int(entry_price) if entry_price is not None else None
    except (TypeError, ValueError):
        entry_price = None
    if entry_price is None or entry_price <= 0:
        logger.debug("CF skip: %s missing usable entry price", opp.get("ticker"))
        return

    fair = opp.get("fair_value_cents") or opp.get("fair_cents") or 0
    edge = opp.get("edge")

    cf_id = f"CF-{scan_id}-{opp.get('ticker','UNK')}"

    records = _load()
    if any(r.get("trade_id") == cf_id for r in records):
        return

    records.append({
        "ticker": opp.get("ticker", ""),
        "opp_type": opp.get("opp_type") or opp.get("type") or "vig_stack_series",
        "side": side,
        "entry_price_cents": entry_price,
        "fair_value_cents": round(float(fair), 2) if fair else 0.0,
        "edge_at_trade": round(float(edge), 4) if edge is not None else None,
        "contracts": 0,
        "trade_id": cf_id,
        "paper": False,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "counterfactual_open",
        "skipped_by_gate": gate,
        "closing_yes_price": None,
        "clv_cents": None,
        "clv_relative": None,
        "settled_at": None,
    })
    try:
        records[-1]["regime"] = regime_tag(
            ts=datetime.fromisoformat(records[-1]["recorded_at"]),
            ticker=opp.get("ticker", ""),
            market_state=opp,
        )
    except Exception:
        logger.exception("clv.record_counterfactual_skip: regime_tag failed for %s", opp.get("ticker"))
    _save(records)

    # Session 11: pair every CF with a prediction record so calibration_report
    # can compute Brier + per-bucket hit-rate over the rejected distribution.
    try:
        from bot.calibration import record_prediction
        record_prediction(
            ticker=opp.get("ticker", ""),
            opp_type=opp.get("opp_type") or opp.get("type") or "vig_stack_series",
            predicted_fair_cents=float(fair) if fair else None,
            market_price_cents=entry_price,
            scan_id=scan_id,
            recorded_at=records[-1]["recorded_at"],
        )
    except Exception:
        logger.exception("calibration record_prediction failed (non-fatal CF)")


# ---------------------------------------------------------------------------
# Session 23 (Apr 27+): live_momentum counterfactuals
# ---------------------------------------------------------------------------
# Mirrors record_counterfactual_skip's vig_stack pattern, adapted for the
# tick-replay live_watcher path. Stratification key is (sport, skip_reason)
# rather than (opp_type, skip_reason) because opp_type is always
# "live_momentum" here.
#
# TUNABLE allowlist below excludes skip_reasons that aren't actionable for
# threshold tuning:
#   - not_today: structural — Kalshi lists future-dated markets ahead of
#     events (Session 21-followup confirmed Apr 27, IPL/UFC dominate).
#     Filtering them is correct; CFs would flood clv.json with
#     unactionable rows.
#   - bad_event_shape, unknown_name: data integrity, not strategy.
#   - settled, already_watching, recently_watched: concurrency / lifecycle,
#     not strategy.
#   - capacity_capped: rare; defer.
#   - disabled_sport: fires series-level BEFORE markets are fetched, so no
#     ticker / side / entry_price exists at the gate site. A CF here would
#     not flow through check_clv_settlements (no market to poll). Revisiting
#     MOMENTUM_DISABLED_SPORTS is best done via an offline universe-table
#     walk in a future session, NOT a per-scan CF.

LIVE_MOMENTUM_TUNABLE_SKIP_REASONS = frozenset({
    "no_leader",
    "low_volume",
    "no_vol_growth_idle",
    "no_vol_growth_first_seen",
})

# Per (sport, skip_reason) per UTC day. Bucket-fill: emit while under cap,
# drop once full. Replacement-on-better-proximity is a v2 follow-up if the
# 24h post-deploy distribution skews far from threshold.
LIVE_MOMENTUM_CF_DAILY_CAP = 5


def _today_start_utc(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def _should_emit_live_momentum_cf(
    *,
    sport: str | None,
    skip_reason: str,
    now: datetime,
    per_day_cap: int = LIVE_MOMENTUM_CF_DAILY_CAP,
) -> bool:
    """Return True if a new live_momentum CF for (sport, skip_reason) is under
    today's per-day cap. Reads clv.json once per call; counts CFs whose
    recorded_at falls in [today_start_utc, now). Never raises."""
    if skip_reason not in LIVE_MOMENTUM_TUNABLE_SKIP_REASONS:
        return False
    try:
        records = _load()
    except Exception:
        logger.exception("clv._should_emit_live_momentum_cf: _load failed")
        return False
    today_start = _today_start_utc(now)
    count = 0
    for r in records:
        if r.get("opp_type") != "live_momentum":
            continue
        status = r.get("status", "")
        if not status.startswith("counterfactual"):
            continue
        if r.get("skipped_by_gate") != skip_reason:
            continue
        if r.get("sport") != sport:
            continue
        recorded_at = r.get("recorded_at")
        if not recorded_at:
            continue
        try:
            ts = datetime.fromisoformat(recorded_at)
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= today_start:
            count += 1
            if count >= per_day_cap:
                return False
    return count < per_day_cap


def record_live_momentum_counterfactual_skip(
    *,
    ticker: str,
    sport: str | None,
    skip_reason: str,
    side: str,
    entry_price_cents: int,
    opponent_ticker: str | None = None,
    measured_value: float | None = None,
    threshold_value: float | None = None,
    market_state: dict | None = None,
    scan_event_ts: datetime | None = None,
) -> None:
    """Record a live_momentum CF row for a watch-but-no-enter scan event.

    Mirrors record_counterfactual_skip:
    - Idempotent on trade_id = f"CF-LM-{compact_ts}-{ticker}"
    - Atomic-write via _save (tmp + rename)
    - Regime-tagged at write time
    - Never raises; caller wraps loosely too

    Caller is expected to pre-filter on LIVE_MOMENTUM_TUNABLE_SKIP_REASONS via
    _should_emit_live_momentum_cf. We re-check defensively so direct callers
    (tests, future callers) can't bypass the allowlist.
    """
    try:
        if skip_reason not in LIVE_MOMENTUM_TUNABLE_SKIP_REASONS:
            logger.debug(
                "live_momentum CF skipped: skip_reason=%s not tunable", skip_reason,
            )
            return
        try:
            entry_int = int(entry_price_cents) if entry_price_cents is not None else None
        except (TypeError, ValueError):
            entry_int = None
        if entry_int is None or entry_int <= 0:
            logger.debug("live_momentum CF skipped: %s missing usable entry price", ticker)
            return
        if not ticker:
            return

        ts = scan_event_ts or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        day_str = ts.astimezone(timezone.utc).strftime("%Y%m%d")
        # Session 86: per-(ticker, sport, skip_reason) per UTC day. Pre-Session-86 the
        # trade_id used second precision; two emits within a few seconds produced
        # different trade_ids and both rows landed (33 raw → 17 unique on the
        # no_vol_growth_first_seen/nhl_game cohort per Session 85). The semantic
        # check below also catches legacy second-precision rows from the same UTC day
        # during the 30-day historical-inflation transition window.
        trade_id = f"CF-LM-{day_str}-{ticker}"

        records = _load()
        for r in records:
            if r.get("opp_type") != "live_momentum":
                continue
            if r.get("ticker") != ticker:
                continue
            if r.get("sport") != sport:
                continue
            if r.get("skipped_by_gate") != skip_reason:
                continue
            # Compare day-of-scan, not day-of-write: scan_event_ts mirrors the original
            # trade_id semantics. Fall back to recorded_at if a row predates scan_event_ts.
            rec_ts = r.get("scan_event_ts") or r.get("recorded_at") or ""
            # ISO 8601 format e.g. "2026-05-08T14:30:22.123+00:00";
            # compare YYYY-MM-DD prefix → day_str's YYYYMMDD form.
            if rec_ts[:10].replace("-", "") == day_str:
                return

        side_norm = str(side or "yes").lower()
        recorded_at = datetime.now(timezone.utc).isoformat()
        record = {
            "ticker": ticker,
            "opp_type": "live_momentum",
            "sport": sport,
            "side": side_norm,
            "entry_price_cents": entry_int,
            "opponent_ticker": opponent_ticker,
            "scan_event_ts": ts.isoformat(),
            "measured_value": measured_value,
            "threshold_value": threshold_value,
            "contracts": 0,
            "trade_id": trade_id,
            "paper": True,
            "recorded_at": recorded_at,
            "status": "counterfactual_open",
            "skipped_by_gate": skip_reason,
            "closing_yes_price": None,
            "clv_cents": None,
            "clv_relative": None,
            "settled_at": None,
        }
        try:
            record["regime"] = regime_tag(
                ts=ts,
                ticker=ticker,
                market_state=market_state,
            )
        except Exception:
            logger.exception(
                "clv.record_live_momentum_counterfactual_skip: regime_tag failed for %s",
                ticker,
            )
        records.append(record)
        _save(records)
    except Exception:
        logger.exception(
            "clv.record_live_momentum_counterfactual_skip failed for ticker=%s",
            ticker,
        )


def record_clv_entry(
    ticker: str,
    opp_type: str,
    side: str,
    entry_price_cents: int,
    fair_value_cents: float,
    edge_at_trade: float,
    contracts: int,
    trade_id: str,
    paper: bool = False,
    scan_id: str | None = None,
):
    """
    Record a new CLV entry at trade time.

    Args:
        ticker: Kalshi market ticker
        opp_type: Strategy type (weather, vig_stack_series, etc.)
        side: "yes" or "no"
        entry_price_cents: What we paid (yes_ask for YES, no_ask for NO)
        fair_value_cents: Our model's fair value in cents
        edge_at_trade: Relative edge at the time of the trade
        contracts: Number of contracts
        trade_id: Order ID or paper trade ID for cross-reference
        paper: True if this was a paper trade
        scan_id: Session 11 — scanner-emitted scan ID for prediction
            idempotency. Falls back to f"trade-{trade_id}" when missing.
    """
    records = _load()
    records.append({
        "ticker": ticker,
        "opp_type": opp_type,
        "side": side,
        "entry_price_cents": entry_price_cents,
        "fair_value_cents": round(fair_value_cents, 2),
        "edge_at_trade": round(edge_at_trade, 4),
        "contracts": contracts,
        "trade_id": trade_id,
        "paper": paper,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "closing_yes_price": None,
        "clv_cents": None,
        "clv_relative": None,
        "settled_at": None,
    })
    try:
        records[-1]["regime"] = regime_tag(
            ts=datetime.fromisoformat(records[-1]["recorded_at"]),
            ticker=ticker,
            market_state=None,
        )
    except Exception:
        logger.exception("clv.record_clv_entry: regime_tag failed for %s", ticker)
    _save(records)
    logger.info(
        f"CLV recorded: {ticker} | {side.upper()} @ {entry_price_cents}¢ | "
        f"fair={fair_value_cents:.1f}¢ | edge={edge_at_trade:.1%} | {'PAPER' if paper else 'REAL'}"
    )

    # Session 11: pair every real trade with a prediction record so
    # calibration_report can join predicted fair vs. closing yes-price.
    try:
        from bot.calibration import record_prediction
        record_prediction(
            ticker=ticker,
            opp_type=opp_type,
            predicted_fair_cents=fair_value_cents,
            market_price_cents=entry_price_cents,
            scan_id=scan_id or f"trade-{trade_id}",
            recorded_at=records[-1]["recorded_at"],
        )
    except Exception:
        logger.exception("calibration record_prediction failed (non-fatal)")


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def check_clv_settlements() -> list[dict]:
    """
    For every open CLV entry, fetch the current Kalshi market.
    If it's settled, record the closing price and compute CLV.

    Returns list of newly settled CLV entries (for Telegram notification).
    """
    from agent.kalshi_client import get_market
    from bot.config import POSITIONS_FILE
    from bot.state_io import load_json as _load_positions

    records = _load()
    settled_now = []
    # Session 155: trade_ids of every record we mutate this cycle (dead-marks +
    # settlements + current_yes_mid updates). Drives _persist_progress instead of
    # a single end-of-cycle _save, so progress survives a 900s abort. Replaces
    # the old `changed` flag.
    mutated_ids: set = set()

    # Session 9: build order_id -> position lookup so we can propagate
    # mfe/mae/ticks onto each clv record at settlement time. Counterfactuals
    # have no position record and are left untouched.
    _positions_raw = _load_positions(POSITIONS_FILE)
    _positions_by_order = {
        p.get("order_id"): p
        for p in (_positions_raw or [])
        if isinstance(p, dict) and p.get("order_id")
    }

    # Session 152 (2026-05-21): batch settlement. Group open candidates by
    # event_ticker and fetch each event's settled markets in ONE get_markets
    # call (vs one get_market per record); records whose ticker can't be grouped
    # (<3 segments) use the per-record fallback. A shared per-cycle call budget
    # (= _CHECK_CLV_BATCH_SIZE) keeps wall-clock inside the 900s S150 guard — the
    # same envelope as the S151 cap — while settling far more records per call,
    # and event grouping breaks the S151 oldest-first FIFO starvation where
    # long-dated season futures clogged the queue head. Mutations on `rec`
    # propagate through `records` (list elements by reference), so _save(records)
    # persists in-place updates.
    from collections import defaultdict

    candidates = [
        rec for rec in records
        if rec.get("status") in ("open", "counterfactual_open")
    ]
    groups: dict = defaultdict(list)
    fallback: list = []
    for rec in candidates:
        tk = rec["ticker"]
        if _is_dead_ticker(tk):
            # Provably-nonexistent shape (malformed/test): terminal-mark with no
            # API call so it stops clogging the open set every cycle.
            _mark_unsettleable(rec, "malformed_ticker")
            mutated_ids.add(rec.get("trade_id"))
            continue
        key = (_event_ticker(tk) if _CLV_BATCH_GROUP_BY == "event"
               else (tk.split("-")[0] or None))
        (groups[key] if key else fallback).append(rec)

    # Session 155: persist the zero-API dead-marks NOW, before the slow
    # rate-limited fetch loop. The S150 900s guard can abort the fetch
    # mid-cycle; persisting first guarantees the ~1,097 malformed records
    # (~27% of OPEN, Phase 0 2026-05-21) clear every cycle regardless of how
    # far the fetch gets. This is the cheap win the old persist-at-end threw
    # away on every abort.
    if mutated_ids:
        _persist_progress(records, mutated_ids)

    # Oldest-first by each group's oldest record (preserves the S151 FIFO intent).
    ordered = sorted(
        groups.items(),
        key=lambda kv: min((r.get("recorded_at") or "") for r in kv[1]),
    )
    budget = _CHECK_CLV_BATCH_SIZE
    if len(ordered) > budget:
        logger.info(
            "check_clv_settlements: %d event-groups (of %d open records); "
            "draining oldest %d this cycle (S152 batch).",
            len(ordered), len(candidates), budget,
        )

    for key, recs in ordered:
        if budget <= 0:
            break
        settled_map = _fetch_settled_markets(key)
        budget -= 1
        for rec in recs:
            m = settled_map.get(rec["ticker"])
            if not m:
                continue
            mstatus = m.get("status", "")
            result = m.get("result", "")
            if mstatus not in ("settled", "finalized", "closed") or not result:
                continue
            if result.upper() == "YES":
                closing_yes = 100
            elif result.upper() == "NO":
                closing_yes = 0
            else:
                continue  # non-binary result (e.g. scalar) — can't compute CLV
            _apply_clv_settlement(
                rec, result, closing_yes,
                rec.get("status") == "counterfactual_open",
                _positions_by_order, settled_now,
            )
            mutated_ids.add(rec.get("trade_id"))

    # Per-record fallback for un-groupable tickers (Outcome B hybrid), bounded by
    # the remaining shared budget. Byte-identical to the legacy detection,
    # including current_yes_mid for still-open markets. get_market is imported at
    # the top of this function.
    fallback.sort(key=lambda r: r.get("recorded_at") or "")
    for rec in fallback[:max(0, budget)]:
        is_cf = rec.get("status") == "counterfactual_open"
        ticker = rec["ticker"]
        try:
            market_resp = get_market(ticker)
        except Exception as e:
            logger.warning(f"CLV settlement check failed for {ticker}: {e}")
            continue
        market = market_resp if isinstance(market_resp, dict) else {}
        err = market.get("error")
        if err and ("404" in err or "not found" in err.lower()):
            # Confirmed-nonexistent market — terminal-mark, stop re-polling.
            _mark_unsettleable(rec, "market_not_found")
            mutated_ids.add(rec.get("trade_id"))
            continue
        if "market" in market:
            market = market["market"]
        mstatus = market.get("status", "")
        result = market.get("result", "")
        yes_bid = market.get("yes_bid") or 0
        yes_ask = market.get("yes_ask") or 0
        if mstatus in ("settled", "finalized", "closed") and result:
            if result.upper() == "YES":
                closing_yes = 100
            elif result.upper() == "NO":
                closing_yes = 0
            else:
                continue
        elif yes_bid > 0 and yes_ask > 0:
            rec["current_yes_mid"] = round((yes_bid + yes_ask) / 2.0, 1)
            mutated_ids.add(rec.get("trade_id"))
            continue
        else:
            continue
        _apply_clv_settlement(
            rec, result, closing_yes, is_cf, _positions_by_order, settled_now,
        )
        mutated_ids.add(rec.get("trade_id"))

    if mutated_ids:
        _persist_progress(records, mutated_ids)

    return settled_now


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def get_clv_report() -> dict:
    """
    Compute CLV statistics per strategy type.

    Returns:
        {
          overall: {avg_clv_cents, avg_clv_relative, positive_rate, count},
          by_strategy: {
            "weather": {avg_clv_cents, avg_clv_relative, positive_rate, count, paper_count},
            ...
          },
          recent: [last 5 settled entries],
        }
    """
    records = _load()
    settled = [r for r in records if r.get("status") == "settled" and r.get("clv_cents") is not None]

    def _stats(rows: list[dict]) -> dict:
        if not rows:
            return {"avg_clv_cents": 0.0, "avg_clv_relative": 0.0, "positive_rate": 0.0, "count": 0}
        clvs = [r["clv_cents"] for r in rows]
        rels = [r["clv_relative"] for r in rows]
        n = len(rows)
        pos = sum(1 for c in clvs if c > 0)
        return {
            "avg_clv_cents": round(sum(clvs) / n, 2),
            "avg_clv_relative": round(sum(rels) / n, 4),
            "positive_rate": round(pos / n, 4),
            "count": n,
        }

    by_strategy: dict[str, dict] = {}
    for rec in settled:
        stype = rec.get("opp_type", "unknown")
        by_strategy.setdefault(stype, []).append(rec)

    by_strategy_stats = {}
    for stype, rows in by_strategy.items():
        stats = _stats(rows)
        stats["paper_count"] = sum(1 for r in rows if r.get("paper"))
        by_strategy_stats[stype] = stats

    recent = sorted(settled, key=lambda r: r.get("settled_at", ""), reverse=True)[:5]

    return {
        "overall": _stats(settled),
        "by_strategy": by_strategy_stats,
        "recent": recent,
        "open_count": sum(1 for r in records if r.get("status") == "open"),
    }


def format_clv_report() -> str:
    """Format CLV report as a Telegram message."""
    report = get_clv_report()
    overall = report["overall"]
    by_strat = report["by_strategy"]
    recent = report["recent"]
    open_count = report["open_count"]

    lines = ["📈 CLV REPORT (Closing-Line Value)", ""]

    if overall["count"] == 0:
        lines.append("No settled trades yet — run paper mode for a few cycles first.")
    else:
        clv_emoji = "✅" if overall["avg_clv_cents"] > 0 else "❌"
        lines.append(
            f"Overall ({overall['count']} trades): "
            f"{clv_emoji} avg CLV = {overall['avg_clv_cents']:+.1f}¢ "
            f"({overall['avg_clv_relative']:+.1%}) | "
            f"beat line: {overall['positive_rate']:.0%}"
        )

        if by_strat:
            lines.append("")
            lines.append("By strategy:")
            for stype, s in by_strat.items():
                label = "PAPER" if s.get("paper_count", 0) == s["count"] else ""
                strat_emoji = "✅" if s["avg_clv_cents"] > 0 else "❌"
                lines.append(
                    f"  {strat_emoji} {stype} ({s['count']}{'p' if label else ''}): "
                    f"{s['avg_clv_cents']:+.1f}¢ avg | {s['positive_rate']:.0%} beat rate"
                )

    if recent:
        lines.append("")
        lines.append("Recent:")
        for r in recent:
            ticker = r["ticker"][-22:]
            clv = r["clv_cents"]
            side = r["side"].upper()
            result = r.get("market_result", "?")
            emoji = "✅" if clv > 0 else "❌"
            lines.append(f"  {emoji} {ticker} {side} | CLV={clv:+.1f}¢ | {result}")

    if open_count:
        lines.append(f"\n{open_count} open (awaiting settlement)")

    return "\n".join(lines)
