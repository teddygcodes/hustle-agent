#!/usr/bin/env python3
"""Journal analysis — live_watcher behavior surfaces from live_journal.json.

Reads `bot/state/live_journal.json` (no rotation — single growing JSON list)
and computes:
  a. Time-to-exit distribution per (sport, mode), bucketed.
  b. Exit-reason classification (take_profit / stop_loss / underwater_exit /
     near_settle / settled_win / settled_loss / score_flip / opp_run_exit /
     other) per (sport, mode).
  c. Watch-but-no-enter funnel per sport (scan_found tickers without a bet).
  d. Per-sport aggregates (a/b/c segmented).
  e. Per-game session_end summary (P&L distribution, top-5 best/worst).

Convention notes
- "mode" in this report is the live_watcher's `mode` field on bet/exit/
  session_end events (`momentum` | `conviction`). The spec calls this
  "strategy" in places; the journal only carries `mode`, so this is what
  we report.
- Sport derivation: prefer the record's `sport` field (post-Apr-16 events
  carry it). Fall back to ticker prefix lookup via
  `bot.regime._ticker_to_sport` for pre-Apr-16 records.
- Exit `reason` is freeform descriptive text. We classify by prefix into a
  normalized enum; spec mentions `trailing_stop` and `hard_cap` but those
  prefixes never appear — flagged in Limitations.
- `scan_found` records: post-Session-21 (Apr 27+) carry `skip_reason`
  (None on spawn, named gate on filter). Pre-Session-21 records lack the
  field and bucket as `unknown_skip` in the per-(sport, skip_reason)
  breakdown; the watch funnel restricts to spawned records (skip_reason
  None or absent) for cross-era comparability.
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Reuses bot.regime's prefix table — mirror updates flow through.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.regime import _ticker_to_sport  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_FILE = ROOT / "bot" / "state" / "live_journal.json"
STATE_DIR = ROOT / "bot" / "state"
ARCHIVE_DIR = STATE_DIR / "archive"
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"

# Findings recorded after running the tool against production data.
# Each string is one bullet rendered in the Findings section. Empty list
# renders an explicit "no findings yet" placeholder so the absence is visible.
# Format: "<observation> (n=N, scope). Confidence: <low|medium|high>. Candidate
# config change: <bot/config.py setting / live_watcher.py path>."
FINDINGS: list[str] = [
    # Session 18 (Apr 26)
    "TRAILING STOP and DOLLAR STOP exits are 0% across all sports/modes "
    "(n=95 paired bet→exit lifecycles, Apr 9–Apr 26). Both code paths "
    "exist (bot/live_watcher.py:2276 trailing stop, bot/live_watcher.py:2342 "
    "dollar stop) but never fire — TAKE PROFIT/STOP-LOSS/UNDERWATER EXIT "
    "always fire first. Trailing stop requires a +50% gain trigger "
    "(LIVE_PROFIT_TARGET=0.50) before activating; LIVE_TAKE_PROFIT_CENTS "
    "fires earlier on the +12-15¢ moves we actually see. Dollar stop's "
    "$5 hard cap (MOMENTUM_MAX_LOSS_DOLLARS=5.00) is wider than STOP-LOSS's "
    "10-12¢ trigger × typical 1-6 contract sizing, so STOP-LOSS always "
    "wins. Confidence: high. Candidate config change: either lower "
    "LIVE_PROFIT_TARGET to 0.20 (so winners get to ride into trailing-stop "
    "territory before TAKE_PROFIT exits flat) OR remove these two paths "
    "from live_watcher entirely as dead code. Worth A/B-testing in the "
    "Session 19 tick-replay back-tester rather than retuning live.",
    "UFC live_momentum is mechanically a different strategy from "
    "court-sports live_momentum: median hold = 123s (p25 = 47s) vs 642–1791s "
    "for atp_challenger / nba / nhl / wta. UFC also has the best "
    "scan→bet conversion (44% vs 9–25% elsewhere) and the only positive "
    "session win/loss ratio (5W/2L of 17 games we bet on; all other sports "
    "with n>10 are roughly 1:1 or worse). 0% UNDERWATER EXIT in UFC vs "
    "21–25% in slow sports — UFC fights end before that path's "
    "5-tick threshold can fire. Confidence: medium (n=9 paired UFC holds "
    "is small). Candidate config change: do NOT retune UFC down to slow-"
    "sport thresholds; consider raising UFC sizing or pulling UFC into a "
    "dedicated TickStrategy when Session 19 ships.",
    "Watch-but-no-enter rate is 56–91% across all sports (494 unique scan "
    "tickers, 391 = 79% had no bet). UFC lowest at 56%, wta_challenger "
    "highest at 91%. We have NO visibility into why — `scan_found` "
    "events do not record `skip_reason`. Confidence: high (volume), low "
    "(causes). Candidate config change: instrument live_watcher's "
    "scan_live_matches to write `skip_reason` on scan_found events "
    "(forward-only — won't recover historical reasons). Until then we "
    "cannot tell if leader_min, dip_max, or sport-disable lists are "
    "over-tight. Tracked separately as a small live_watcher follow-up.",
]


def load_journal() -> list[dict]:
    """Read live_journal.json. Returns [] on any I/O or parse failure.

    Live_journal has NO rotation (verified Phase 1 investigation: archive/
    contains decisions and live_ticks but no live_journal). Single file only.
    """
    if not JOURNAL_FILE.exists():
        return []
    try:
        records = json.loads(JOURNAL_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict) and r.get("event")]


def _iter_jsonl_lines(path: Path, opener):
    with opener(path, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def load_decisions() -> list[dict]:
    """Read current + archived decisions for the no-entry context slice."""
    records: list[dict] = []
    sources: list[tuple[Path, callable]] = []
    if DECISIONS_FILE.exists():
        sources.append((DECISIONS_FILE, open))
    if ARCHIVE_DIR.exists():
        for path in sorted(ARCHIVE_DIR.glob("decisions-*.jsonl.gz")):
            sources.append((path, gzip.open))
    for path, opener in sources:
        try:
            records.extend(_iter_jsonl_lines(path, opener))
        except (OSError, EOFError):
            continue
    return records


def _parse_ts(s):
    """Parse ISO 8601 timestamps. Returns None on failure.

    Handles both 'Z' and '+00:00' suffixes (mirror cohort_report._parse_ts).
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


EXIT_REASON_KEYS: tuple[str, ...] = (
    "take_profit", "trailing_stop", "stop_loss", "dollar_stop",
    "underwater_exit", "near_settle", "settled_win", "settled_loss",
    "score_flip", "opp_run_exit", "other",
)

# Order matters: longer-prefix entries must come BEFORE shorter ones that
# would also match (e.g., "TRAILING STOP" before any "STOP*" would be a
# problem if those existed; in practice STOP-LOSS uses a hyphen so there's
# no ambiguity, but we document the discipline).
_EXIT_REASON_PREFIXES: tuple[tuple[str, str], ...] = (
    ("TAKE PROFIT", "take_profit"),
    ("TRAILING STOP", "trailing_stop"),
    ("STOP-LOSS", "stop_loss"),
    ("DOLLAR STOP", "dollar_stop"),
    ("UNDERWATER EXIT", "underwater_exit"),
    ("NEAR-SETTLE", "near_settle"),
    ("SCORE FLIP", "score_flip"),
    ("OPP RUN EXIT", "opp_run_exit"),
)


def _classify_exit_reason(reason) -> str:
    """Classify a freeform exit reason string into a normalized enum.

    Pattern-match by leading prefix. SETTLED is split into win/loss by the
    trailing parenthetical. The spec calls the dollar-cap exit `hard_cap`;
    `bot/live_watcher.py:2342` writes it as `DOLLAR STOP: $X.XX loss exceeds
    $Y.YY cap` so we use `dollar_stop` as the enum key. Both `trailing_stop`
    and `dollar_stop` paths exist in code but have never fired in the
    journal data window (Apr 9 – Apr 26, n=95 paired exits) — see
    Limitations + Findings sections for detail.
    """
    if not reason:
        return "other"
    r = str(reason).strip()
    for prefix, key in _EXIT_REASON_PREFIXES:
        if r.startswith(prefix):
            return key
    if r.startswith("SETTLED"):
        if "WIN" in r:
            return "settled_win"
        if "LOSS" in r:
            return "settled_loss"
    return "other"


def _record_sport(rec: dict) -> str:
    """Resolve sport from a journal record.

    Post-Apr-16 records carry `sport` directly on bet/exit. Pre-Apr-16 fall
    back to ticker prefix via bot.regime._ticker_to_sport. Returns
    'unknown_sport' if neither path yields a value.
    """
    sport = rec.get("sport")
    if sport:
        return str(sport)
    inferred = _ticker_to_sport(rec.get("ticker") or "")
    return inferred or "unknown_sport"


TIME_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<60s",     0.0,    60.0),
    ("60s-5min", 60.0,   300.0),
    ("5-15min",  300.0,  900.0),
    ("15-60min", 900.0,  3600.0),
    (">60min",   3600.0, float("inf")),
)


def _bucket_hold(seconds: float) -> str:
    for label, lo, hi in TIME_BUCKETS:
        if lo <= seconds < hi:
            return label
    return TIME_BUCKETS[-1][0]


def _pair_bets_to_exits(records: list[dict]) -> tuple[list[tuple[dict, dict, float]], list[dict]]:
    """Pair each bet with the first eligible exit on the same ticker.

    Algorithm:
      1. Sort all events by timestamp; records with unparseable timestamps
         are excluded entirely.
      2. For each bet (chronological), find the first exit on the same
         ticker with ts >= bet.ts not already claimed by an earlier bet.
      3. Bets without a matching exit are 'open' (in-flight).

    Returns:
      paired:    list of (bet, exit, hold_seconds)
      open_bets: list of bet records with no matching exit

    Bets with unparseable timestamps are silently dropped (not counted
    as open) — this is the same defensive policy excursion_report uses
    for malformed records.
    """
    indexed: list[tuple[datetime, int, dict]] = []
    for i, r in enumerate(records):
        ts = _parse_ts(r.get("timestamp"))
        if ts is None:
            continue
        indexed.append((ts, i, r))
    indexed.sort(key=lambda x: (x[0], x[1]))

    bets = [(ts, i, r) for ts, i, r in indexed if r.get("event") == "bet"]
    exits = [(ts, i, r) for ts, i, r in indexed if r.get("event") == "exit"]

    claimed: set[int] = set()
    paired: list[tuple[dict, dict, float]] = []
    open_bets: list[dict] = []

    for bet_ts, _bi, bet in bets:
        ticker = bet.get("ticker")
        match = None
        for ex_ts, ex_i, ex in exits:
            if ex_i in claimed:
                continue
            if ex.get("ticker") != ticker:
                continue
            if ex_ts < bet_ts:
                continue
            match = (ex_ts, ex_i, ex)
            break
        if match is None:
            open_bets.append(bet)
            continue
        ex_ts, ex_i, ex = match
        claimed.add(ex_i)
        paired.append((bet, ex, (ex_ts - bet_ts).total_seconds()))

    return paired, open_bets


def compute_time_to_exit(records: list[dict]) -> dict[tuple[str, str], dict]:
    """Bucket paired bet→exit holds by (sport, mode).

    Returns dict[(sport, mode), dict] with keys:
      - buckets:        dict[bucket_label, count]
      - n:              total paired holds
      - open:           count of unpaired bets in this (sport, mode)
      - median_seconds: median hold across paired holds (None if n=0)
      - p25_seconds:    25th-percentile hold (None if n<2)
    """
    paired, open_bets = _pair_bets_to_exits(records)

    by_key: dict[tuple[str, str], list[float]] = defaultdict(list)
    open_counts: dict[tuple[str, str], int] = defaultdict(int)
    for bet, ex, hold in paired:
        sport = _record_sport(bet) if bet.get("sport") else _record_sport(ex)
        mode = bet.get("mode") or ex.get("mode") or "unknown_mode"
        by_key[(sport, mode)].append(hold)
    for bet in open_bets:
        sport = _record_sport(bet)
        mode = bet.get("mode") or "unknown_mode"
        open_counts[(sport, mode)] += 1

    out: dict[tuple[str, str], dict] = {}
    all_keys = set(by_key) | set(open_counts)
    for key in all_keys:
        holds = by_key.get(key, [])
        buckets = {label: 0 for label, _, _ in TIME_BUCKETS}
        for h in holds:
            buckets[_bucket_hold(h)] += 1
        out[key] = {
            "buckets": buckets,
            "n": len(holds),
            "open": open_counts.get(key, 0),
            "median_seconds": statistics.median(holds) if holds else None,
            "p25_seconds": statistics.quantiles(holds, n=4)[0] if len(holds) >= 2 else None,
        }
    return out


def compute_exit_reasons(records: list[dict]) -> dict[tuple[str, str], dict]:
    """Classify exit reasons per (sport, mode).

    Reuses _pair_bets_to_exits so we count exits that actually closed a
    bet we placed (excludes orphan exits or settlement events for
    counterfactual records). Counts dict carries every enum key zero-
    initialized so absent buckets render as 0%.
    """
    paired, _open = _pair_bets_to_exits(records)
    by_key: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"counts": {k: 0 for k in EXIT_REASON_KEYS}, "n": 0}
    )
    for bet, ex, _hold in paired:
        sport = _record_sport(bet) if bet.get("sport") else _record_sport(ex)
        mode = bet.get("mode") or ex.get("mode") or "unknown_mode"
        bucket = _classify_exit_reason(ex.get("reason"))
        by_key[(sport, mode)]["counts"][bucket] += 1
        by_key[(sport, mode)]["n"] += 1
    return dict(by_key)


def _scan_record_was_spawned(r: dict) -> bool:
    """True if the scan_found record represents a watcher being spawned.

    Pre-Session-21: scan_found was emitted only at the spawn-watcher branch,
    so the absence of `skip_reason` means "spawned". Post-Session-21: every
    match-level gate's continue site also emits scan_found with a non-None
    `skip_reason`, while the spawn site emits skip_reason=None.

    Treating field-absent and field=None as the same "spawned" bucket keeps
    the watch-funnel comparable across the schema migration.
    """
    if "skip_reason" not in r:
        return True  # pre-Session-21 record
    return r["skip_reason"] is None


def compute_watch_funnel(records: list[dict]) -> dict[str, dict]:
    """Per-sport scan→bet funnel (spawned-watcher tickers only).

    Counts unique scan_found tickers per sport that were SPAWNED into a
    watcher (skip_reason=None or pre-Session-21 records) and what fraction
    were followed by a bet. Repeat scans on the same ticker collapse to a
    single entry.

    Filtered (skip_reason=<gate>) records are excluded — they're surfaced in
    the per-(sport, skip_reason) breakdown instead.
    """
    bet_tickers: set[str] = {
        r["ticker"] for r in records
        if r.get("event") == "bet" and r.get("ticker")
    }
    scan_tickers_by_sport: dict[str, set[str]] = defaultdict(set)
    for r in records:
        if r.get("event") != "scan_found":
            continue
        if not _scan_record_was_spawned(r):
            continue
        ticker = r.get("ticker")
        if not ticker:
            continue
        scan_tickers_by_sport[_record_sport(r)].add(ticker)
    out: dict[str, dict] = {}
    for sport, tickers in scan_tickers_by_sport.items():
        with_bet = len(tickers & bet_tickers)
        no_bet = len(tickers) - with_bet
        out[sport] = {
            "unique_scans": len(tickers),
            "scan_with_bet": with_bet,
            "scan_no_bet": no_bet,
        }
    return out


def compute_skip_reason_breakdown(records: list[dict]) -> dict[str, dict[str, int]]:
    """Per-(sport, skip_reason) breakdown of unique scan_found tickers (Session 21).

    Returns: {sport: {skip_reason: count_of_unique_tickers}}. Within a (sport,
    skip_reason) bucket, repeat scans on the same ticker collapse — we count
    the ticker once. Spawned matches (skip_reason=None) bucket as "_spawned".
    Pre-Session-21 records (no skip_reason field) bucket as "unknown_skip"
    so the schema migration is visible in the report.
    """
    by_sport_reason: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for r in records:
        if r.get("event") != "scan_found":
            continue
        ticker = r.get("ticker")
        if not ticker:
            continue
        if "skip_reason" not in r:
            reason = "unknown_skip"
        elif r["skip_reason"] is None:
            reason = "_spawned"
        else:
            reason = str(r["skip_reason"])
        by_sport_reason[_record_sport(r)][reason].add(ticker)
    return {
        sport: {reason: len(tickers) for reason, tickers in reasons.items()}
        for sport, reasons in by_sport_reason.items()
    }


_NO_ENTRY_NUMERIC_FIELDS = (
    "leader_price", "dip_cents", "dqs", "spread_cents", "volume_24h",
)


def _numeric_value(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num:
        return None
    return num


def _context_from_record(rec: dict) -> dict:
    extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
    return extra or {}


def _context_sport(rec: dict, context: dict) -> str:
    return (
        context.get("sport")
        or rec.get("sport")
        or _record_sport(rec)
        or "unknown_sport"
    )


def compute_live_momentum_no_entry_context(
    journal_records: list[dict],
    decision_records: list[dict],
) -> dict:
    """Aggregate enriched live_momentum reject/no-entry context."""
    rows: list[dict] = []
    for rec in decision_records:
        if rec.get("opp_type") != "live_momentum" or rec.get("decision") != "reject":
            continue
        context = _context_from_record(rec)
        rows.append({
            "sport": _context_sport(rec, context),
            "reason": rec.get("reason") or "unknown",
            "context": context,
        })
    for rec in journal_records:
        if rec.get("event") != "scan_found":
            continue
        reason = rec.get("skip_reason")
        if reason is None:
            continue
        context = _context_from_record(rec)
        rows.append({
            "sport": _context_sport(rec, context),
            "reason": str(reason),
            "context": context,
        })

    by_key: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "count": 0,
        "enriched": 0,
        "values": {field: [] for field in _NO_ENTRY_NUMERIC_FIELDS},
        "missing": Counter(),
    })
    total = 0
    enriched_total = 0
    missing_total: Counter = Counter()
    for row in rows:
        total += 1
        context = row["context"]
        enriched = bool(context.get("context_available"))
        if enriched:
            enriched_total += 1
        bucket = by_key[(str(row["sport"]), str(row["reason"]))]
        bucket["count"] += 1
        if enriched:
            bucket["enriched"] += 1
        for field in _NO_ENTRY_NUMERIC_FIELDS:
            num = _numeric_value(context.get(field))
            if num is not None:
                bucket["values"][field].append(num)
        missing = context.get("missing_context_fields")
        if isinstance(missing, list):
            for field in missing:
                if isinstance(field, str):
                    bucket["missing"][field] += 1
                    missing_total[field] += 1

    return {
        "total": total,
        "enriched": enriched_total,
        "by_key": dict(by_key),
        "missing": missing_total,
    }


def _fmt_avg_median(values: list[float]) -> str:
    if not values:
        return "—"
    avg = sum(values) / len(values)
    med = statistics.median(values)
    return f"{avg:.1f}/{med:.1f}"


def _render_live_momentum_no_entry_context_section(agg: dict) -> list[str]:
    out = [
        "## Live Momentum No-Entry Context",
        "",
        "_Counts existing live_momentum rejects plus scan_found no-entry records. "
        "Numeric cells are avg/median over enriched rows with that field._",
        "",
    ]
    total = agg.get("total", 0)
    if not total:
        out.append("_No live_momentum no-entry/reject records in this dataset._")
        out.append("")
        return out

    enriched = agg.get("enriched", 0)
    coverage = enriched / total * 100 if total else 0.0
    missing = agg.get("missing") or Counter()
    top_missing = ", ".join(
        f"{field} ({count})" for field, count in missing.most_common(5)
    ) or "none"
    out.append(
        f"Context coverage: **{coverage:.1f}%** ({enriched}/{total}); "
        f"top missing fields: {top_missing}"
    )
    out.append("")
    rows = sorted(
        (agg.get("by_key") or {}).items(),
        key=lambda item: (-item[1]["count"], item[0][0], item[0][1]),
    )
    if len(rows) > 30:
        out.append(f"_Showing top 30 of {len(rows)} reason buckets._")
        out.append("")
    out.append(
        "| Sport | Reason | Count | Context % | Leader price | Dip cents | DQS | Spread | Volume 24h |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for (sport, reason), bucket in rows[:30]:
        count = bucket["count"]
        context_pct = bucket["enriched"] / count * 100 if count else 0.0
        values = bucket["values"]
        out.append(
            f"| {sport} | {reason} | {count} | {context_pct:.0f}% | "
            f"{_fmt_avg_median(values['leader_price'])} | "
            f"{_fmt_avg_median(values['dip_cents'])} | "
            f"{_fmt_avg_median(values['dqs'])} | "
            f"{_fmt_avg_median(values['spread_cents'])} | "
            f"{_fmt_avg_median(values['volume_24h'])} |"
        )
    out.append("")
    return out


def compute_session_ends(records: list[dict]) -> dict[tuple[str, str], dict]:
    """Per (sport, mode) session_end aggregation: P&L bucket counts,
    median, top-5 best/worst by total_pnl.
    """
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        if r.get("event") != "session_end":
            continue
        pnl = r.get("total_pnl")
        if pnl is None:
            continue
        sport = _record_sport(r)
        mode = r.get("mode") or "unknown_mode"
        by_key[(sport, mode)].append({
            "ticker": r.get("ticker", ""),
            "match": r.get("match", ""),
            "pnl": float(pnl),
            "duration_min": r.get("duration_min"),
        })
    out: dict[tuple[str, str], dict] = {}
    for key, sessions in by_key.items():
        pnls = [s["pnl"] for s in sessions]
        sorted_by_pnl = sorted(sessions, key=lambda s: s["pnl"])
        out[key] = {
            "n": len(sessions),
            "profit": sum(1 for p in pnls if p > 0),
            "loss": sum(1 for p in pnls if p < 0),
            "break_even": sum(1 for p in pnls if p == 0),
            "median_pnl": statistics.median(pnls) if pnls else 0.0,
            "best_5": list(reversed(sorted_by_pnl[-5:])),
            "worst_5": sorted_by_pnl[:5],
        }
    return out


def _findings_section() -> list[str]:
    out = ["## Findings", ""]
    if not FINDINGS:
        out.append(
            "_No findings recorded yet — run the tool against production data, "
            "then add the insight string to FINDINGS at the top of "
            "`tools/journal_analysis.py`._"
        )
    else:
        for f in FINDINGS:
            out.append(f"- {f}")
    out.append("")
    return out


def _limitations_section() -> list[str]:
    return [
        "## Limitations",
        "",
        "- `scan_found.skip_reason` instrumented Apr 27+ (Session 21 — forward-only). "
        "Pre-Session-21 records (Apr 9–Apr 26) lack the field and bucket as "
        "`unknown_skip` in the per-(sport, skip_reason) breakdown. The watch "
        "funnel restricts to spawned records for cross-era comparability.",
        "- Both `trailing_stop` (bot/live_watcher.py:2276) and `dollar_stop` "
        "(bot/live_watcher.py:2342, the spec's `hard_cap`) code paths exist "
        "but have NEVER fired in the n=95 paired exits to date — TAKE PROFIT "
        "/ STOP-LOSS / UNDERWATER EXIT always trigger first. Classifier "
        "recognizes the prefixes so future fires are bucketed correctly; "
        "see Findings for the actionable interpretation.",
        "- Pre-Apr-16 bet/exit records lack `sport`; we infer from ticker prefix via "
        "`bot.regime._ticker_to_sport`. Sport unrecognized by that table → "
        "`unknown_sport`.",
        "- Bet→exit pairing is greedy first-eligible. A ticker with N bets and M<N "
        "exits pairs the first M bets in chronological order; the remaining N−M "
        "are `open`.",
        "- `mode` is the live_watcher field (`momentum` / `conviction`), not the "
        "scanner's `ACTIVE_STRATEGIES` axis. Vig-stack and other strategy variants "
        "do not write to live_journal.",
        "- `live_journal.json` has no rotation. Current size grows ~36 KB/day; flag "
        "for a future small rotation task before it exceeds ~10 MB.",
        "",
    ]


def _render_time_to_exit_section(by_key: dict[tuple[str, str], dict]) -> list[str]:
    out = ["## Time-to-Exit Distribution (paired bet→exit holds)", ""]
    if not by_key:
        out.append("_No paired holds in this dataset._")
        out.append("")
        return out
    out.append(
        "| Sport | Mode | N | Open | Median (s) | p25 (s) "
        "| <60s | 60s-5min | 5-15min | 15-60min | >60min |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for key in sorted(by_key):
        v = by_key[key]
        b = v["buckets"]
        med = "—" if v["median_seconds"] is None else f"{v['median_seconds']:.0f}"
        p25 = "—" if v["p25_seconds"] is None else f"{v['p25_seconds']:.0f}"
        sport, mode = key
        out.append(
            f"| {sport} | {mode} | {v['n']} | {v['open']} | {med} | {p25} | "
            f"{b['<60s']} | {b['60s-5min']} | {b['5-15min']} | "
            f"{b['15-60min']} | {b['>60min']} |"
        )
    out.append("")
    return out


def _render_exit_reasons_section(by_key: dict[tuple[str, str], dict]) -> list[str]:
    out = ["## Exit Reason Breakdown", ""]
    if not by_key:
        out.append("_No paired exits in this dataset._")
        out.append("")
        return out
    headers = ["Sport", "Mode", "N"] + list(EXIT_REASON_KEYS)
    out.append("| " + " | ".join(headers) + " |")
    out.append("|---|---|---:|" + "|".join(["---:"] * len(EXIT_REASON_KEYS)) + "|")
    for key in sorted(by_key):
        v = by_key[key]
        n = v["n"]
        cells = [key[0], key[1], str(n)]
        for k in EXIT_REASON_KEYS:
            c = v["counts"][k]
            cells.append(f"{c} ({c / n * 100:.0f}%)" if n else "0")
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    return out


def _render_watch_funnel_section(by_sport: dict[str, dict]) -> list[str]:
    out = [
        "## Watch-but-No-Enter Funnel (spawned tickers only)",
        "",
        "_Counts only scan_found records that resulted in a watcher being "
        "spawned (skip_reason=None or pre-Session-21 records). Filtered "
        "records are surfaced in the per-(sport, skip_reason) breakdown below._",
        "",
    ]
    if not by_sport:
        out.append("_No spawned scan_found records in this dataset._")
        out.append("")
        return out
    out.append("| Sport | Unique scans | Scan→bet | Scan→no_bet | % no_bet |")
    out.append("|---|---:|---:|---:|---:|")
    for sport in sorted(by_sport):
        v = by_sport[sport]
        u = v["unique_scans"]
        no_bet_pct = v["scan_no_bet"] / u * 100 if u else 0.0
        out.append(
            f"| {sport} | {u} | {v['scan_with_bet']} | "
            f"{v['scan_no_bet']} | {no_bet_pct:.0f}% |"
        )
    out.append("")
    return out


def _render_skip_reason_section(by_sport_reason: dict[str, dict[str, int]]) -> list[str]:
    """Per-(sport, skip_reason) breakdown table (Session 21).

    Columns: every distinct skip_reason that appears in the dataset, plus
    "_spawned" for skip_reason=None and "unknown_skip" for pre-Session-21
    records. Counts are unique tickers per (sport, skip_reason) bucket.
    """
    out = [
        "## Per-(Sport, Skip Reason) Breakdown (Session 21)",
        "",
        "_Unique tickers per sport per skip_reason. `_spawned` = passed all "
        "gates and a watcher was created. `unknown_skip` = pre-Session-21 "
        "record (the field doesn't exist on those — they were all spawns "
        "under the old single-emission semantic)._",
        "",
    ]
    if not by_sport_reason:
        out.append("_No scan_found records in this dataset._")
        out.append("")
        return out
    # Stable column order: _spawned first, unknown_skip last, everything else
    # alphabetised in between (matches the LIVE_SCAN_TELEMETRY drop dict's
    # natural sort).
    all_reasons: set[str] = set()
    for d in by_sport_reason.values():
        all_reasons.update(d.keys())
    middle = sorted(r for r in all_reasons if r not in ("_spawned", "unknown_skip"))
    columns: list[str] = []
    if "_spawned" in all_reasons:
        columns.append("_spawned")
    columns.extend(middle)
    if "unknown_skip" in all_reasons:
        columns.append("unknown_skip")

    headers = ["Sport", "Total"] + columns
    out.append("| " + " | ".join(headers) + " |")
    out.append("|---|---:|" + "|".join(["---:"] * len(columns)) + "|")
    for sport in sorted(by_sport_reason):
        row_counts = by_sport_reason[sport]
        total = sum(row_counts.values())
        cells = [sport, str(total)]
        for reason in columns:
            n = row_counts.get(reason, 0)
            if n == 0:
                cells.append("0")
            elif total:
                cells.append(f"{n} ({n / total * 100:.0f}%)")
            else:
                cells.append(str(n))
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    return out


def _render_session_ends_section(by_key: dict[tuple[str, str], dict]) -> list[str]:
    out = ["## Per-Game Session End Summary", ""]
    if not by_key:
        out.append("_No session_end records in this dataset._")
        out.append("")
        return out
    out.append("| Sport | Mode | N | Profit | Break-even | Loss | Median P&L |")
    out.append("|---|---|---:|---:|---:|---:|---:|")
    for key in sorted(by_key):
        v = by_key[key]
        out.append(
            f"| {key[0]} | {key[1]} | {v['n']} | {v['profit']} | "
            f"{v['break_even']} | {v['loss']} | {v['median_pnl']:+.2f} |"
        )
    out.append("")
    out.append("### Top-5 Best/Worst Sessions (by total_pnl)")
    out.append("")
    for key in sorted(by_key):
        v = by_key[key]
        if not (v["best_5"] or v["worst_5"]):
            continue
        out.append(f"**{' / '.join(key)}** (n={v['n']})")
        out.append("")
        out.append("| Rank | ticker | match | total_pnl |")
        out.append("|---|---|---|---:|")
        for i, s in enumerate(v["best_5"], 1):
            match = (s["match"] or "")[:60]
            out.append(f"| best #{i} | {s['ticker']} | {match} | {s['pnl']:+.2f} |")
        for i, s in enumerate(v["worst_5"], 1):
            match = (s["match"] or "")[:60]
            out.append(f"| worst #{i} | {s['ticker']} | {match} | {s['pnl']:+.2f} |")
        out.append("")
    return out


def render_markdown(records: list[dict], decisions: list[dict] | None = None) -> str:
    """Build the Markdown report.

    Section order: header → Findings → Time-to-Exit → Exit Reasons →
    Watch Funnel → Session Ends → Limitations.
    """
    out: list[str] = ["# Journal Analysis — Live Watcher Behavior", ""]
    out.append(f"Source: `bot/state/live_journal.json` ({len(records)} records)")

    timestamps = [t for t in (_parse_ts(r.get("timestamp")) for r in records) if t]
    if timestamps:
        first = min(timestamps).date().isoformat()
        last = max(timestamps).date().isoformat()
        out.append(f"Time window: {first} → {last}")
    out.append("")

    if not records:
        out.append("_No journal records yet._")
        return "\n".join(out)

    out.extend(_findings_section())
    out.extend(_render_time_to_exit_section(compute_time_to_exit(records)))
    out.extend(_render_exit_reasons_section(compute_exit_reasons(records)))
    out.extend(_render_watch_funnel_section(compute_watch_funnel(records)))
    out.extend(_render_skip_reason_section(compute_skip_reason_breakdown(records)))
    out.extend(_render_live_momentum_no_entry_context_section(
        compute_live_momentum_no_entry_context(records, decisions or [])
    ))
    out.extend(_render_session_ends_section(compute_session_ends(records)))
    out.extend(_limitations_section())
    return "\n".join(out)


def main() -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args()
    records = load_journal()
    decisions = load_decisions()
    print(render_markdown(records, decisions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
