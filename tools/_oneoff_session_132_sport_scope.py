"""Session 132 one-off: live_momentum sport-scope profitability deep-dive.

Maps per-sport profitability on the post-Apr-23 live_momentum cohort across
sub-periods (Apr-29 bankroll bump, May-5 S54 sizer fix, May-11 S97 ATP/NBA
disable, May-12 S112 IPL enable). Investigates whether the S131 IPL+ / ATP-
contrast on the post-S50 sub-cohort holds on the full cohort and, if so,
whether a scope-restriction follow-up is warranted.

Analyses:
  A: Full-cohort sport breakdown (informational, all sports)
  B: Currently-enabled-only sub-cohort
  C: Sub-period split per enabled sport
  D: Time-in-trade and exit-reason by sport
  E: Per-sport entry price distribution (S38c foreshadow)
  F: Counterfactual scope sweep (disable_X / only_X)
  G: Sport-stratified bootstrap CIs
  + DECISION FRAMING with classification (a)-(f)

NO production code or state is mutated; this script only reads
paper_trades.json and prints tables.

Reproducible: rerun any time the cohort has grown. Safe to delete after
S132 ships if no follow-up sport-scope session is queued.

Run:
    python3 tools/_oneoff_session_132_sport_scope.py
"""
from __future__ import annotations

import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

random.seed(132)

# --- Constants ---------------------------------------------------------------
COHORT_START = "2026-04-23"
SUB_PERIODS = [
    ("pre-Apr-29",       "2026-04-23", "2026-04-29"),
    ("Apr-29 to May-5",  "2026-04-29", "2026-05-05"),
    ("May-5 to May-11",  "2026-05-05", "2026-05-11"),
    ("post-May-11",      "2026-05-11", "9999"),
]
MOMENTUM_DISABLED_SPORTS = {"atp", "atp_challenger", "nba_game", "wta", "wta_challenger"}
SPORT_PROFILE_DISABLED   = {"mlb_game", "mlb_futures"}  # mirrors SPORT_PROFILES disabled flag

# Canonical map ordered longest-prefix-first so longer prefixes win.
TICKER_PREFIX_MAP: list[tuple[str, str]] = [
    ("KXATPCHALLENGERMATCH-", "atp_challenger"),
    ("KXWTACHALLENGERMATCH-", "wta_challenger"),
    ("KXATPMATCH-",           "atp"),
    ("KXWTAMATCH-",           "wta"),
    ("KXMLBGAME-",            "mlb_game"),
    ("KXNBAGAME-",            "nba_game"),
    ("KXNHLGAME-",            "nhl_game"),
    ("KXMLB-",                "mlb_futures"),
    ("KXNBA-",                "nba_futures"),
    ("KXNHL-",                "nhl_futures"),
    ("KXIPL",                 "ipl"),
    ("KXUFC",                 "ufc"),
    ("KXHIGH",                "weather_high"),
    ("KXLOW",                 "weather_low"),
    ("KXINX",                 "index"),
]
ACTIONABILITY_GATE_DOLLARS = 1.00  # per-trade Δ threshold to call a scope intervention "actionable"
BOOTSTRAP_ITERS = 10_000

PAPER_TRADES_PATH = Path(__file__).resolve().parent.parent / "bot" / "state" / "paper_trades.json"


# --- Helpers -----------------------------------------------------------------

def canonical_sport(ticker: str) -> str | None:
    if not ticker:
        return None
    for prefix, sport in TICKER_PREFIX_MAP:
        if ticker.startswith(prefix):
            return sport
    return None  # informational gap; vanishingly rare


def load_cohort() -> list[dict]:
    raw = json.loads(PAPER_TRADES_PATH.read_text())
    cohort = [
        t for t in raw
        if t.get("type") == "live_momentum"
        and t.get("status") in ("won", "lost", "exited_early")
        and t.get("timestamp", "") >= COHORT_START
    ]
    for t in cohort:
        t["_sport_canon"] = canonical_sport(t.get("ticker", ""))
        t["_sport_raw"]   = t.get("sport") or "?"
    return cohort


def is_enabled(sport: str | None) -> bool:
    if sport is None:
        return False
    return sport not in MOMENTUM_DISABLED_SPORTS and sport not in SPORT_PROFILE_DISABLED


def pnl(t: dict) -> float:
    return float(t.get("pnl") or 0.0)


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = math.floor(k); c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def cohort_stats(trades: list[dict]) -> tuple[int, float, float]:
    n = len(trades)
    total = sum(pnl(t) for t in trades)
    mean = total / n if n else 0.0
    return n, total, mean


def bootstrap_mean_ci(values: list[float], iters: int = BOOTSTRAP_ITERS, alpha: float = 0.05) -> tuple[float, float, float]:
    if not values:
        return float("nan"), float("nan"), float("nan")
    n = len(values)
    means = []
    for _ in range(iters):
        sample = [values[random.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[int((1 - alpha / 2) * iters)]
    return statistics.mean(values), lo, hi


def categorize_exit(t: dict) -> str:
    reason = (t.get("exit_reason") or "").upper()
    if not reason or reason in ("MISSING", "UNKNOWN"):
        return "Other"
    if "TAKE PROFIT" in reason or "TRAIL" in reason or "NEAR-SETTLE" in reason or "NEAR SETTLE" in reason:
        return "TP"
    if "STOP LOSS" in reason or "DOLLAR STOP" in reason or "AUTO_CUT_LOSS" in reason:
        return "SL"
    if "OPP RUN" in reason:
        return "EE"
    return "Other"


def trade_hold_minutes(t: dict) -> float | None:
    ts_in  = t.get("timestamp")
    ts_out = t.get("resolved_at")
    if not ts_in or not ts_out:
        return None
    try:
        dt_in  = datetime.fromisoformat(ts_in.replace("Z", "+00:00"))
        dt_out = datetime.fromisoformat(ts_out.replace("Z", "+00:00"))
        return (dt_out - dt_in).total_seconds() / 60.0
    except ValueError:
        return None


# --- Audit -------------------------------------------------------------------

def print_audit(cohort: list[dict]) -> None:
    print("=" * 78)
    print("S132 — live_momentum sport-scope profitability deep-dive")
    print("=" * 78)
    print(f"Cohort: live_momentum, status in {{won, lost, exited_early}}, ts >= {COHORT_START}")
    print(f"N = {len(cohort)}")
    print()
    print("Sport reconstruction audit:")
    raw_counter   = Counter(t["_sport_raw"]   for t in cohort)
    canon_counter = Counter(t["_sport_canon"] or "UNMAPPED" for t in cohort)
    granularity_drift = Counter()   # raw 'nba' → canon 'nba_game' (expected normalization)
    category_drift    = Counter()   # raw 'atp' → canon 'nba_game' (red flag)
    unmapped: list[str] = []
    for t in cohort:
        raw, canon = t["_sport_raw"], t["_sport_canon"]
        if canon is None:
            unmapped.append(t.get("ticker", "?"))
            continue
        if raw == "?":
            continue
        if raw == canon:
            continue
        # Granularity: canon is `<raw>_game` / `<raw>_futures` / `<raw>_challenger`
        if canon.startswith(raw + "_"):
            granularity_drift[(raw, canon)] += 1
        else:
            category_drift[(raw, canon)] += 1
    print(f"  raw distribution:   {dict(raw_counter.most_common())}")
    print(f"  canon distribution: {dict(canon_counter.most_common())}")
    print(f"  granularity drift (raw → canon, expected normalization):")
    if granularity_drift:
        for (raw, canon), n in granularity_drift.most_common():
            print(f"    {raw!r} → {canon!r}: {n}")
    else:
        print(f"    (none)")
    print(f"  category drift (raw → canon, RED FLAG):")
    if category_drift:
        for (raw, canon), n in category_drift.most_common():
            print(f"    {raw!r} → {canon!r}: {n}")
    else:
        print(f"    (none)")
    print(f"  unmapped tickers:   {unmapped or '(none)'}")
    print()


# --- Shared table renderer ---------------------------------------------------

def _print_sport_breakdown(trades_by_sport: dict[str, list[dict]]) -> None:
    rows = []
    for sport, trades in trades_by_sport.items():
        n      = len(trades)
        wins   = sum(1 for t in trades if t["status"] == "won")
        losses = sum(1 for t in trades if t["status"] == "lost")
        ees    = sum(1 for t in trades if t["status"] == "exited_early")
        pnls   = [pnl(t) for t in trades]
        total  = sum(pnls)
        mean   = total / n if n else 0.0
        wr     = wins / n if n else 0.0
        mean_entry = statistics.mean(float(t.get("entry_price") or 0.0) for t in trades) if trades else 0.0
        confs = [float(t["confidence"]) for t in trades if t.get("confidence")]
        mean_conf = statistics.mean(confs) if confs else float("nan")
        rows.append((sport, n, f"{wins}/{losses}/{ees}", wr, mean, total, mean_entry, mean_conf))
    rows.sort(key=lambda r: r[5], reverse=True)
    print(f"  {'Sport':<16} {'N':>4} {'W/L/EE':>10} {'WR':>6} {'Mean$':>8} {'Total$':>9} {'Entry¢':>7} {'Conf':>6}")
    for sport, n, wle, wr, mean, total, entry, conf in rows:
        conf_str = f"{conf:.3f}" if not math.isnan(conf) else "  —  "
        print(f"  {sport:<16} {n:>4} {wle:>10} {wr:>6.1%} {mean:>+8.2f} {total:>+9.2f} {entry*100:>7.1f} {conf_str:>6}")


# --- Analysis A --------------------------------------------------------------

def analysis_a(cohort: list[dict]) -> None:
    print("─" * 78)
    print("Analysis A — Full-cohort sport breakdown (all sports, informational)")
    print("─" * 78)
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for t in cohort:
        by_sport[t["_sport_canon"] or "UNMAPPED"].append(t)
    _print_sport_breakdown(by_sport)
    print("  (Confidence column is mean over trades with confidence > 0; pre-S50 trades = 0 are excluded.)")
    print()


# --- Analysis B --------------------------------------------------------------

def analysis_b(cohort: list[dict]) -> list[dict]:
    print("─" * 78)
    print("Analysis B — Currently-enabled sports only (forward-looking)")
    print("─" * 78)
    enabled = [t for t in cohort if is_enabled(t["_sport_canon"])]
    print(f"  Enabled-cohort N = {len(enabled)} (of {len(cohort)} full cohort)")
    print(f"  Excluded sports: MOMENTUM_DISABLED = {sorted(MOMENTUM_DISABLED_SPORTS)}")
    print(f"  Excluded sports: SPORT_PROFILE_DISABLED = {sorted(SPORT_PROFILE_DISABLED)}")
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for t in enabled:
        by_sport[t["_sport_canon"]].append(t)
    _print_sport_breakdown(by_sport)
    print()
    return enabled


# --- Analysis C --------------------------------------------------------------

def analysis_c(cohort: list[dict]) -> None:
    print("─" * 78)
    print("Analysis C — Sub-period split per enabled sport (gate: N≥5 in post-S54 sub-cohort)")
    print("─" * 78)
    enabled = [t for t in cohort if is_enabled(t["_sport_canon"])]
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for t in enabled:
        by_sport[t["_sport_canon"]].append(t)

    # Always-shown diagnostic: per-sport sub-period N distribution (no gate).
    # Outcome (e) sub-period instability requires this to be visible even when
    # the gated detail tables don't print.
    print(f"  Sub-period N per enabled sport (all enabled sports, ungated):")
    period_labels = [name for name, _, _ in SUB_PERIODS]
    header = "    " + f"{'Sport':<14}" + "".join(f"{lbl:>20}" for lbl in period_labels) + f"{'Total':>7}"
    print(header)
    for sport in sorted(by_sport.keys()):
        trades = by_sport[sport]
        cells = []
        total_pnl_period = []
        for name, lo, hi in SUB_PERIODS:
            sub = [t for t in trades if lo <= t.get("timestamp", "") < hi]
            n = len(sub)
            if n == 0:
                cells.append(f"{'0':>20}")
            else:
                total = sum(pnl(t) for t in sub)
                cells.append(f"{n:>3} ({total:+.1f})".rjust(20))
            total_pnl_period.append(sum(pnl(t) for t in sub))
        print("    " + f"{sport:<14}" + "".join(cells) + f"{len(trades):>7}")
    print()

    post_s54_lo = "2026-05-05"
    any_printed = False
    for sport in sorted(by_sport.keys()):
        trades = by_sport[sport]
        post_s54 = [t for t in trades if t.get("timestamp", "") >= post_s54_lo]
        if len(post_s54) < 5:
            continue
        any_printed = True
        print(f"  Sport: {sport} (total N={len(trades)}, post-S54 N={len(post_s54)})")
        print(f"    {'Sub-period':<20} {'N':>4} {'WR':>6} {'Mean$':>8} {'Total$':>9}")
        for name, lo, hi in SUB_PERIODS:
            sub = [t for t in trades if lo <= t.get("timestamp", "") < hi]
            if not sub:
                print(f"    {name:<20} {0:>4}      —        —         —")
                continue
            n = len(sub)
            wins = sum(1 for t in sub if t["status"] == "won")
            pnls = [pnl(t) for t in sub]
            wr = wins / n
            mean = sum(pnls) / n
            total = sum(pnls)
            print(f"    {name:<20} {n:>4} {wr:>6.1%} {mean:>+8.2f} {total:>+9.2f}")
        print()
    if not any_printed:
        print("  (no enabled sport has N≥5 post-S54 — see diagnostic table above)")
    print()


# --- Analysis D --------------------------------------------------------------

def analysis_d(cohort: list[dict]) -> None:
    print("─" * 78)
    print("Analysis D — Time-in-trade and exit-reason by sport (gate: enabled, N≥10)")
    print("─" * 78)
    enabled = [t for t in cohort if is_enabled(t["_sport_canon"])]
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for t in enabled:
        by_sport[t["_sport_canon"]].append(t)
    print(f"  {'Sport':<16} {'N':>4} {'MeanHold':>9} {'MedHold':>8} {'TP%':>6} {'SL%':>6} {'EE%':>6} {'Other%':>7}")
    any_printed = False
    overall_other_count = 0
    overall_total = 0
    for sport in sorted(by_sport.keys()):
        trades = by_sport[sport]
        if len(trades) < 10:
            continue
        any_printed = True
        holds = [m for t in trades for m in [trade_hold_minutes(t)] if m is not None]
        mean_hold = statistics.mean(holds) if holds else float("nan")
        med_hold  = statistics.median(holds) if holds else float("nan")
        cats = Counter(categorize_exit(t) for t in trades)
        n = len(trades)
        tp = cats.get("TP", 0) / n
        sl = cats.get("SL", 0) / n
        ee = cats.get("EE", 0) / n
        other = cats.get("Other", 0) / n
        overall_other_count += cats.get("Other", 0)
        overall_total += n
        print(f"  {sport:<16} {n:>4} {mean_hold:>9.1f} {med_hold:>8.1f} {tp:>6.1%} {sl:>6.1%} {ee:>6.1%} {other:>7.1%}")
    if not any_printed:
        print("  (no enabled sport with N≥10)")
    if overall_total:
        print(f"  Note: {overall_other_count}/{overall_total} ({overall_other_count*100/overall_total:.0f}%) trades fell in 'Other' bucket")
        print(f"        — mostly pre-S36 records missing exit_reason field.")
    print()


# --- Analysis E --------------------------------------------------------------

def analysis_e(cohort: list[dict]) -> None:
    print("─" * 78)
    print("Analysis E — Per-sport entry price distribution (gate: enabled, N≥10)")
    print("─" * 78)
    enabled = [t for t in cohort if is_enabled(t["_sport_canon"])]
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for t in enabled:
        by_sport[t["_sport_canon"]].append(t)
    print(f"  {'Sport':<16} {'N':>4} {'Mean¢':>7} {'Med¢':>7} {'p25¢':>7} {'p75¢':>7}")
    any_printed = False
    high_median_sports: list[str] = []
    for sport in sorted(by_sport.keys()):
        trades = by_sport[sport]
        if len(trades) < 10:
            continue
        any_printed = True
        entries = [float(t.get("entry_price") or 0.0) for t in trades]
        m   = statistics.mean(entries) * 100
        md  = statistics.median(entries) * 100
        p25 = percentile(entries, 0.25) * 100
        p75 = percentile(entries, 0.75) * 100
        print(f"  {sport:<16} {len(trades):>4} {m:>7.1f} {md:>7.1f} {p25:>7.1f} {p75:>7.1f}")
        if md >= 85.0:
            high_median_sports.append(sport)
    if not any_printed:
        print("  (no enabled sport with N≥10)")
    if high_median_sports:
        print(f"  S38c FLAG: median entry ≥85¢ for {high_median_sports} — entry-price-by-sport overlap, not re-scoped here.")
    print()


# --- Analysis F --------------------------------------------------------------

def analysis_f(cohort: list[dict]) -> dict:
    print("─" * 78)
    print("Analysis F — Counterfactual scope sweep (baseline = currently-enabled)")
    print("─" * 78)
    enabled = [t for t in cohort if is_enabled(t["_sport_canon"])]
    base_n, base_total, base_mean = cohort_stats(enabled)
    enabled_sports = sorted({t["_sport_canon"] for t in enabled})
    print(f"  {'Action':<32} {'N':>4} {'Total$':>10} {'Mean$':>8} {'Δ vs base':>11}")
    print(f"  {'baseline (enabled)':<32} {base_n:>4} {base_total:>+10.2f} {base_mean:>+8.2f} {'—':>11}")
    result = {"baseline_mean": base_mean, "baseline_n": base_n, "disable": {}, "only": {}}
    for s in enabled_sports:
        kept = [t for t in enabled if t["_sport_canon"] != s]
        n, total, mean = cohort_stats(kept)
        delta = mean - base_mean
        flag = " *" if delta >= ACTIONABILITY_GATE_DOLLARS else ""
        print(f"  {'disable_' + s:<32} {n:>4} {total:>+10.2f} {mean:>+8.2f} {delta:>+11.2f}{flag}")
        result["disable"][s] = {"n": n, "total": total, "mean": mean, "delta": delta}
    for s in enabled_sports:
        only = [t for t in enabled if t["_sport_canon"] == s]
        n, total, mean = cohort_stats(only)
        delta = mean - base_mean
        flag = " *" if delta >= ACTIONABILITY_GATE_DOLLARS else ""
        print(f"  {'only_' + s:<32} {n:>4} {total:>+10.2f} {mean:>+8.2f} {delta:>+11.2f}{flag}")
        result["only"][s] = {"n": n, "total": total, "mean": mean, "delta": delta}
    print(f"  ('*' marks Δ ≥ +${ACTIONABILITY_GATE_DOLLARS:.2f} per-trade actionability gate)")
    print()
    return result


# --- Analysis G --------------------------------------------------------------

def analysis_g(cohort: list[dict]) -> list[tuple[str, int, float, float, float]]:
    print("─" * 78)
    print("Analysis G — Sport-stratified bootstrap 95% CI on per-sport mean P&L")
    print("─" * 78)
    enabled = [t for t in cohort if is_enabled(t["_sport_canon"])]
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for t in enabled:
        by_sport[t["_sport_canon"]].append(t)
    top = sorted(by_sport.items(), key=lambda kv: len(kv[1]), reverse=True)[:3]
    print(f"  {'Sport':<16} {'N':>4} {'Mean$':>8} {'95% CI lo':>11} {'95% CI hi':>11} {'CI width':>9} {'Verdict':<18}")
    rows: list[tuple[str, int, float, float, float]] = []
    for sport, trades in top:
        pnls = [pnl(t) for t in trades]
        mean, lo, hi = bootstrap_mean_ci(pnls)
        width = hi - lo
        if hi < 0:
            verdict = "excludes 0 (neg)"
        elif lo > 0:
            verdict = "excludes 0 (pos)"
        else:
            verdict = "spans 0"
        print(f"  {sport:<16} {len(trades):>4} {mean:>+8.2f} {lo:>+11.2f} {hi:>+11.2f} {width:>9.2f} {verdict:<18}")
        rows.append((sport, len(trades), mean, lo, hi))
    print(f"  ({BOOTSTRAP_ITERS:,} iterations, seed=132. Wide CI (>$10 width) → small N caveat.)")
    print()
    return rows


# --- Decision framing --------------------------------------------------------

def decision_frame(cohort: list[dict], f_result: dict, g_result: list) -> None:
    print("─" * 78)
    print("DECISION FRAMING")
    print("─" * 78)
    enabled = [t for t in cohort if is_enabled(t["_sport_canon"])]
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for t in enabled:
        by_sport[t["_sport_canon"]].append(t)

    classifiers: list[str] = []
    notes: list[str] = []

    actionable_disable = [(s, v["delta"]) for s, v in f_result["disable"].items()
                          if v["delta"] >= ACTIONABILITY_GATE_DOLLARS]
    actionable_only    = [(s, v["delta"], v["n"]) for s, v in f_result["only"].items()
                          if v["delta"] >= ACTIONABILITY_GATE_DOLLARS and v["n"] >= 5]

    confident_disable = [(s, n, m, lo, hi) for (s, n, m, lo, hi) in g_result if hi < 0]
    confident_keep    = [(s, n, m, lo, hi) for (s, n, m, lo, hi) in g_result if lo > 0]

    if actionable_disable and confident_disable:
        classifiers.append("(a) clear concentration — disable a specific sport")
        for s, d in actionable_disable:
            notes.append(f"  disable_{s} → Δ +${d:.2f}/trade vs baseline")
        for s, n, m, lo, hi in confident_disable:
            notes.append(f"  {s} CI = [${lo:+.2f}, ${hi:+.2f}], N={n} (excludes 0 on downside)")
    if actionable_only and confident_keep:
        classifiers.append("(b) clear concentration — restrict to a single sport")
        for s, d, n in actionable_only:
            notes.append(f"  only_{s} (N={n}) → Δ +${d:.2f}/trade vs baseline")
        for s, n, m, lo, hi in confident_keep:
            notes.append(f"  {s} CI = [${lo:+.2f}, ${hi:+.2f}], N={n} (excludes 0 on upside)")

    if not classifiers:
        max_n = max((len(v) for v in by_sport.values()), default=0)
        if max_n < 15:
            classifiers.append(
                f"(d) N too thin per-sport for confident calls (max per-sport N = {max_n}; "
                f"watch-list trigger: re-investigate at per-sport N≥15)"
            )
        else:
            classifiers.append("(c) profitability roughly uniform across enabled sports (or no actionable concentration)")

    print(f"  Classification: {'; '.join(classifiers)}")
    for note in notes:
        print(note)
    if not notes:
        print("  (no per-sport contrast cleared both the +$1.00 gate AND the bootstrap CI)")
    print()
    print(f"  Per-sport N (enabled cohort): "
          f"{ {s: len(v) for s, v in sorted(by_sport.items(), key=lambda kv: len(kv[1]), reverse=True)} }")
    print(f"  Operator note: Outcome (e) sub-period instability is not auto-detected here —")
    print(f"  inspect Analysis C output above for sign-flips across May-5 / May-11 boundaries.")
    print()


# --- main --------------------------------------------------------------------

def main() -> None:
    cohort = load_cohort()
    print_audit(cohort)
    analysis_a(cohort)
    analysis_b(cohort)
    analysis_c(cohort)
    analysis_d(cohort)
    analysis_e(cohort)
    f_result = analysis_f(cohort)
    g_result = analysis_g(cohort)
    decision_frame(cohort, f_result, g_result)


if __name__ == "__main__":
    main()
