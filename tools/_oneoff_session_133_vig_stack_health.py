#!/usr/bin/env python3
"""Session 133 — vig_stack health re-check (post-Filter-F + post-bankroll-bump).

Investigation-only. Read-only against paper_trades.json + trade_history.json.
Mirrors S130/S131/S132 one-off pattern: pure stdlib, console output, no files emitted.

Question set:
  1. Is the post-Filter-F cohort net positive or still leaking?
  2. Did the 0.93 volatile floor work?
  3. Are S93's family disables (KXHIGHCHI, KXINX) actually preventing entries?
  4. Is the Apr-30 vig_stack_futures profitable-edge pattern recurring?

Outputs analyses A through K + decision-framing block (outcomes a-i).
"""
import json
import math
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAPER_TRADES = REPO_ROOT / "bot/state/paper_trades.json"
TRADE_HISTORY = REPO_ROOT / "bot/state/trade_history.json"
DECISIONS_LOG = REPO_ROOT / "bot/state/decisions.jsonl"

# Authoritative ladder-context key list (S100, forward-only)
try:
    sys.path.insert(0, str(REPO_ROOT))
    from bot.vig_stack_ladder_context import LADDER_CONTEXT_KEYS  # type: ignore
except Exception:
    LADDER_CONTEXT_KEYS = (
        "family", "ladder_total_yes_sum_cents", "rung_count",
        "selected_rung_rank_asc", "selected_rung_rank_desc",
        "rung_strike", "rung_kind", "no_price_cents",
        "forecast_bucket_distance", "source_forecast_temp",
        "source_city", "time_to_close_hr", "ladder_context_source",
    )

# Phase 0 premises (re-quoted at runtime for verification)
STABLE_FAMILIES = {"KXHIGHMIA", "KXHIGHAUS", "KXINX"}
DISABLED_FAMILIES = {"KXHIGHCHI", "KXINX"}
WEATHER_MIN_PRICE = 0.93

# Sub-period boundaries (UTC ISO 8601 — sortable lexicographically)
APR20 = "2026-04-20T00:00:00+00:00"  # Filter F ship
APR29 = "2026-04-29T00:00:00+00:00"  # bankroll bump + Session 36 TP/SL exemption ship
MAY10 = "2026-05-10T00:00:00+00:00"  # S93 disable
MAY11 = "2026-05-11T00:00:00+00:00"  # S100 ladder-context ship

# Apr-20 audit ground truth (CLAUDE.md "Money (The Honest Numbers)")
APR20_AUDIT = {"N": 54, "total_pnl": -110.62, "wr": 0.54}

# Actionability gates
DELTA_GATE_DOLLARS = 1.00


# ---------------------- helpers ----------------------

def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def fam_from_ticker(ticker: str) -> str:
    return ticker.split("-", 1)[0] if ticker else "UNKNOWN"


FUTURES_PREFIXES = ("KXMLBGAME", "KXNBA", "KXNHL", "KXNFL", "KXMLS", "KXATP", "KXUFC", "KXCFB", "KXSOC")


def classify_family(family: str) -> str:
    if family in STABLE_FAMILIES:
        return "stable"
    if family.startswith(FUTURES_PREFIXES):
        return "futures"
    if family.startswith("KXHIGH") or family.startswith("KXLOW"):
        return "volatile"
    return "other"


def sub_period(ts: str) -> str:
    if not ts:
        return "unknown"
    if ts < APR20:
        return "pre_apr20"
    if ts < APR29:
        return "apr20_apr29"
    if ts < MAY10:
        return "apr29_may10"
    if ts < MAY11:
        return "may10_may11"
    return "post_may11"


def pnl(t) -> float:
    p = t.get("pnl")
    return float(p) if p is not None else 0.0


def entry_cents(t) -> float:
    p = t.get("entry_price")
    return float(p) * 100 if p is not None else 0.0


def settled_n(trades) -> int:
    return sum(1 for t in trades if t["status"] in ("won", "lost", "exited_early"))


def win_rate(trades) -> float:
    """Positive-PnL resolution rate (treats EE with pnl>0 as a win).

    The Apr-20 audit used (won/(won+lost)) but vig_stack carries a large EE
    population — for monetary outcome WR the PnL-sign convention is the
    informative one. Documented in S132.
    """
    n = len(trades)
    if not n:
        return 0.0
    return sum(1 for t in trades if pnl(t) > 0) / n


def mean_pnl(trades) -> float:
    if not trades:
        return 0.0
    return sum(pnl(t) for t in trades) / len(trades)


def total_pnl(trades) -> float:
    return sum(pnl(t) for t in trades)


def mean_entry_cents(trades) -> float:
    if not trades:
        return 0.0
    return sum(entry_cents(t) for t in trades) / len(trades)


def bootstrap_ci(values, iterations=10000, seed=133, conf=0.95):
    if not values:
        return (None, None, None)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iterations):
        s = 0.0
        for _ in range(n):
            s += values[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo_idx = int(iterations * (1 - conf) / 2)
    hi_idx = int(iterations * (1 - (1 - conf) / 2)) - 1
    return (sum(values) / n, means[lo_idx], means[hi_idx])


def fmt_pct(x):
    return f"{x*100:5.1f}%" if x is not None else "  n/a"


def fmt_money(x):
    return f"${x:+8.2f}" if x is not None else "    n/a"


# ---------------------- main ----------------------

def main():
    paper = json.loads(PAPER_TRADES.read_text())
    history = json.loads(TRADE_HISTORY.read_text())

    # opp_type lookup by ticker (for Analysis G — finer vig_stack_series vs futures)
    opp_type_by_ticker = {}
    for r in history:
        tk = r.get("ticker")
        ot = r.get("opp_type")
        if tk and ot and tk not in opp_type_by_ticker:
            opp_type_by_ticker[tk] = ot

    # Build cohort (vig_stack-typed, settled = won/lost/EE)
    cohort = []
    for t in paper:
        if t.get("type") != "vig_stack":
            continue
        if t.get("status") not in ("won", "lost", "exited_early"):
            continue
        fam = t.get("family") or fam_from_ticker(t.get("ticker", ""))
        cls = classify_family(fam)
        sp = sub_period(t.get("timestamp", ""))
        opp = opp_type_by_ticker.get(t.get("ticker", ""), "")
        c = dict(t)
        c["_family"] = fam
        c["_class"] = cls
        c["_sub_period"] = sp
        c["_opp_type"] = opp
        cohort.append(c)

    # =============================== PREMISES ===============================
    hr("SESSION 133 — vig_stack health re-check (post-Filter-F + post-bankroll-bump)")
    print()
    print("PHASE 0 — premises re-quoted (verify against bot/config.py):")
    print(f"  STRATEGY_BUDGETS[vig_stack]   = 0.60")
    print(f"  VIG_STACK_STABLE_FAMILIES     = {sorted(STABLE_FAMILIES)}")
    print(f"  VIG_STACK_WEATHER_MIN_PRICE   = {WEATHER_MIN_PRICE}")
    print(f"  VIG_STACK_DISABLED_FAMILIES   = {sorted(DISABLED_FAMILIES)}")
    print(f"  PAPER_STARTING_BALANCE        = 10500.0")
    print(f"  _VIG_STACK_OPP_TYPES          = ('vig_stack_no', 'vig_stack_series')")
    print(f"  LADDER_CONTEXT_KEYS (S100)    = {len(LADDER_CONTEXT_KEYS)} fields")
    print()
    print("KXINX duality: appears in BOTH STABLE and DISABLED. Historical stable (Apr-20")
    print("  era); S93 (2026-05-10) added to disabled set. Treated as stable for Analysis F")
    print("  sub-period split with post-S93 row marked 'disabled'.")
    print()
    print("Sub-period boundaries (UTC):")
    print(f"  pre_apr20:    ts <  2026-04-20  (pre-Filter-F era)")
    print(f"  apr20_apr29:  2026-04-20 ≤ ts < 2026-04-29")
    print(f"  apr29_may10:  2026-04-29 ≤ ts < 2026-05-10  (post-S36 TP/SL exemption + bankroll bump)")
    print(f"  may10_may11:  2026-05-10 ≤ ts < 2026-05-11  (post-S93 KXHIGHCHI/KXINX disable)")
    print(f"  post_may11:   ts >= 2026-05-11  (post-S100 ladder-context ship)")
    print()
    print(f"Apr-20 audit baseline (CLAUDE.md): N={APR20_AUDIT['N']}, "
          f"total ${APR20_AUDIT['total_pnl']:+.2f}, WR {APR20_AUDIT['wr']*100:.0f}%")
    print()
    print(f"Full settled vig_stack cohort: N={len(cohort)}")
    sp_counts = Counter(t["_sub_period"] for t in cohort)
    print("  Per sub-period:")
    for name in ("pre_apr20", "apr20_apr29", "apr29_may10", "may10_may11", "post_may11"):
        print(f"    {name:14s} N={sp_counts.get(name, 0)}")
    print()
    print("Family distribution (full settled cohort):")
    fam_counts = Counter(t["_family"] for t in cohort)
    for fam, n in sorted(fam_counts.items(), key=lambda kv: -kv[1]):
        cls = classify_family(fam)
        disabled = "  [DISABLED 2026-05-10]" if fam in DISABLED_FAMILIES else ""
        print(f"  {fam:14s} class={cls:8s} N={n}{disabled}")

    # =============================== ANALYSIS A ===============================
    hr("ANALYSIS A — Headline P&L re-validation")
    print()
    print(f"{'Cohort window':<28s} {'N':>5s} {'WR':>7s} {'Mean P&L':>10s} {'Total P&L':>12s} {'Entry':>9s}")
    print("-" * 78)
    windows = [
        ("full settled cohort", cohort),
        ("post-Apr-20 (Filter F)", [t for t in cohort if t["_sub_period"] != "pre_apr20"]),
        ("post-Apr-29 (bankroll+S36)", [t for t in cohort if t["_sub_period"] in ("apr29_may10", "may10_may11", "post_may11")]),
        ("post-May-10 (post-S93)", [t for t in cohort if t["_sub_period"] in ("may10_may11", "post_may11")]),
    ]
    for label, sub in windows:
        if not sub:
            print(f"{label:<28s} {0:>5d}  empty")
            continue
        print(f"{label:<28s} {len(sub):>5d} {fmt_pct(win_rate(sub)):>7s} "
              f"{fmt_money(mean_pnl(sub)):>10s} {fmt_money(total_pnl(sub)):>12s} "
              f"{mean_entry_cents(sub):>7.1f}¢")
    print()
    print(f"Apr-20 audit baseline reminder: N=54, total $-110.62, WR 54%")
    print()
    print("Per sub-period breakdown (avoid cross-bankroll $ aggregation):")
    print(f"{'Sub-period':<14s} {'N':>5s} {'WR':>7s} {'Mean P&L':>10s} {'Total P&L':>12s} {'Entry':>9s}")
    print("-" * 70)
    for name in ("pre_apr20", "apr20_apr29", "apr29_may10", "may10_may11", "post_may11"):
        sub = [t for t in cohort if t["_sub_period"] == name]
        if not sub:
            print(f"{name:<14s} {0:>5d}  empty")
            continue
        print(f"{name:<14s} {len(sub):>5d} {fmt_pct(win_rate(sub)):>7s} "
              f"{fmt_money(mean_pnl(sub)):>10s} {fmt_money(total_pnl(sub)):>12s} "
              f"{mean_entry_cents(sub):>7.1f}¢")

    # =============================== ANALYSIS B ===============================
    hr("ANALYSIS B — Per-family breakdown (full settled cohort)")
    print()
    print(f"{'Family':<14s} {'Class':<9s} {'N':>5s} {'WR':>7s} {'Mean P&L':>10s} {'Total P&L':>12s} {'Disabled':>12s}")
    print("-" * 78)
    fam_rows = []
    for fam in fam_counts:
        sub = [t for t in cohort if t["_family"] == fam]
        cls = classify_family(fam)
        dis = "2026-05-10" if fam in DISABLED_FAMILIES else "—"
        fam_rows.append((fam, cls, sub, dis))
    fam_rows.sort(key=lambda r: -total_pnl(r[2]))
    for fam, cls, sub, dis in fam_rows:
        print(f"{fam:<14s} {cls:<9s} {len(sub):>5d} {fmt_pct(win_rate(sub)):>7s} "
              f"{fmt_money(mean_pnl(sub)):>10s} {fmt_money(total_pnl(sub)):>12s} {dis:>12s}")

    # =============================== ANALYSIS C ===============================
    hr("ANALYSIS C — Volatile-floor effectiveness (post-Apr-20, volatile family only)")
    print()
    volatile_post = [t for t in cohort if t["_class"] == "volatile" and t["_sub_period"] != "pre_apr20"]
    print(f"N (post-Apr-20 volatile): {len(volatile_post)}")
    print()
    print(f"{'Entry NO¢':<14s} {'N':>5s} {'WR':>7s} {'Mean P&L':>10s} {'Total P&L':>12s} {'≥0.93 floor?':>14s}")
    print("-" * 78)
    buckets = [(85, 89), (89, 91), (91, 93), (93, 95), (95, 97), (97, 101)]
    admitted = []
    rejected = []
    for lo, hi in buckets:
        sub = [t for t in volatile_post if lo <= entry_cents(t) < hi]
        admits = "Y" if lo >= 93 else "N"
        if not sub:
            print(f"  [{lo:3d}, {hi:3d})    {0:>5d}  empty                                          {admits:>14s}")
            continue
        print(f"  [{lo:3d}, {hi:3d})    {len(sub):>5d} {fmt_pct(win_rate(sub)):>7s} "
              f"{fmt_money(mean_pnl(sub)):>10s} {fmt_money(total_pnl(sub)):>12s} {admits:>14s}")
        if lo >= 93:
            admitted.extend(sub)
        else:
            rejected.extend(sub)
    print("-" * 78)
    print(f"  admitted by 0.93 floor: N={len(admitted)}, total ${total_pnl(admitted):+.2f}, "
          f"mean ${mean_pnl(admitted):+.2f}/trade")
    print(f"  below 0.93 floor (leaked in pre-Filter-F era / edge cases): "
          f"N={len(rejected)}, total ${total_pnl(rejected):+.2f}")

    # =============================== ANALYSIS D ===============================
    hr("ANALYSIS D — Status mix (Battle Scar #9 exemption verification)")
    print()
    print("Battle Scar #9: vig_stack_series + vig_stack_no exempt from edge_flipped/TP/SL.")
    print("Session 36 (2026-04-29) extended exemption to TP/SL.")
    print("vig_stack_futures is NOT in _VIG_STACK_OPP_TYPES — futures DO auto-exit.")
    print()
    print(f"{'Sub-period':<14s} {'N':>5s} {'won %':>7s} {'lost %':>7s} {'EE %':>7s} {'flag':>10s}")
    print("-" * 62)
    for name in ("pre_apr20", "apr20_apr29", "apr29_may10", "may10_may11", "post_may11"):
        sub = [t for t in cohort if t["_sub_period"] == name]
        n = len(sub)
        if n == 0:
            continue
        w = sum(1 for x in sub if x["status"] == "won") / n
        l = sum(1 for x in sub if x["status"] == "lost") / n
        e = sum(1 for x in sub if x["status"] == "exited_early") / n
        flag = " ⚠ >5%" if (name in ("apr29_may10", "may10_may11", "post_may11") and e > 0.05) else ""
        print(f"{name:<14s} {n:>5d} {fmt_pct(w):>7s} {fmt_pct(l):>7s} {fmt_pct(e):>7s} {flag:>10s}")
    print()
    print("Post-Apr-29 (post-S36) class breakdown — exemption only protects weather/series:")
    print(f"{'class':<12s} {'N':>5s} {'won %':>7s} {'lost %':>7s} {'EE %':>7s}")
    print("-" * 52)
    post_apr29_full = [t for t in cohort if t["_sub_period"] in ("apr29_may10", "may10_may11", "post_may11")]
    for cls in ("stable", "volatile", "futures", "other"):
        sub = [t for t in post_apr29_full if t["_class"] == cls]
        n = len(sub)
        if n == 0:
            continue
        w = sum(1 for x in sub if x["status"] == "won") / n
        l = sum(1 for x in sub if x["status"] == "lost") / n
        e = sum(1 for x in sub if x["status"] == "exited_early") / n
        print(f"{cls:<12s} {n:>5d} {fmt_pct(w):>7s} {fmt_pct(l):>7s} {fmt_pct(e):>7s}")
    print()
    # exit_reason audit on all EE trades (post-S36 specifically)
    ee_all = [t for t in cohort if t["status"] == "exited_early"]
    ee_post_s36 = [t for t in ee_all if t["_sub_period"] in ("apr29_may10", "may10_may11", "post_may11")]
    print(f"All-time EE trades: N={len(ee_all)} (with exit_reason populated: "
          f"{sum(1 for t in ee_all if t.get('exit_reason'))})")
    print(f"Post-S36 EE trades: N={len(ee_post_s36)}")
    print()
    if ee_post_s36:
        print(f"{'class':<12s} {'EE_N':>5s} {'has_reason':>11s}  reasons (Counter)")
        print("-" * 78)
        for cls in ("stable", "volatile", "futures", "other"):
            sub = [t for t in ee_post_s36 if t["_class"] == cls]
            if not sub:
                continue
            has = sum(1 for t in sub if t.get("exit_reason"))
            rsn = Counter(t.get("exit_reason", "(unset)") for t in sub)
            print(f"{cls:<12s} {len(sub):>5d} {has:>11d}  {dict(rsn)}")
        print()
        print("Reading: if (stable + volatile) post-S36 EE rate is materially >5% with reasons")
        print("         that are auto_take_profit/auto_cut_loss/edge_flipped, that contradicts")
        print("         Battle Scar #9 → Outcome (e) regression candidate.")

    # =============================== ANALYSIS E ===============================
    hr("ANALYSIS E — S93 disable enforcement (KXHIGHCHI, KXINX)")
    print()
    print(f"S93 ship: 2026-05-10. Expected post-S93 entries on disabled families: 0")
    print()
    s93_breach_count = 0
    for fam in ("KXHIGHCHI", "KXINX"):
        all_fam = [t for t in cohort if t["_family"] == fam]
        pre = [t for t in all_fam if t["_sub_period"] not in ("may10_may11", "post_may11")]
        post = [t for t in all_fam if t["_sub_period"] in ("may10_may11", "post_may11")]
        last_ts = max((t["timestamp"] for t in all_fam), default="—")
        s93_breach_count += len(post)
        flag = "  ⚠ REGRESSION" if post else "  ✓"
        print(f"  {fam}: pre-S93 N={len(pre)}, post-S93 N={len(post)}{flag}, last_entry={last_ts}")
    print()
    if DECISIONS_LOG.exists():
        family_disabled = 0
        total_decisions = 0
        with DECISIONS_LOG.open() as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                total_decisions += 1
                try:
                    d = json.loads(line)
                    reason = d.get("reason", "")
                    if isinstance(reason, str) and "family_disabled" in reason:
                        family_disabled += 1
                except Exception:
                    pass
        print(f"  decisions.jsonl total entries: {total_decisions}")
        print(f"  family_disabled* reject events: {family_disabled}")
    else:
        print(f"  (decisions.jsonl not found at {DECISIONS_LOG})")

    # =============================== ANALYSIS F ===============================
    hr("ANALYSIS F — Sub-period split per stable family (per user direction)")
    print()
    print(f"{'Family':<12s} {'Sub-period':<14s} {'N':>5s} {'WR':>7s} {'Mean P&L':>10s} {'Total P&L':>12s} {'Note':>26s}")
    print("-" * 88)
    for fam in ("KXHIGHMIA", "KXHIGHAUS", "KXINX"):
        for sp_name in ("apr20_apr29", "apr29_may10", "may10_may11", "post_may11"):
            sub = [t for t in cohort if t["_family"] == fam and t["_sub_period"] == sp_name]
            note = ""
            if fam == "KXINX" and sp_name in ("may10_may11", "post_may11"):
                note = "[disabled, N=0 expected]"
            if sub:
                print(f"{fam:<12s} {sp_name:<14s} {len(sub):>5d} {fmt_pct(win_rate(sub)):>7s} "
                      f"{fmt_money(mean_pnl(sub)):>10s} {fmt_money(total_pnl(sub)):>12s} {note:>26s}")
            else:
                print(f"{fam:<12s} {sp_name:<14s} {0:>5d} {'  n/a':>7s} {'    n/a':>10s} {'      n/a':>12s} {note:>26s}")
        print()
    # KXHIGHAUS B vs T bucket (S100 watch — May-9/10 B-bucket loss concentration)
    print("KXHIGHAUS bucket-level breakdown (S100 watch: May-9/10 B-bucket loss concentration):")
    aus = [t for t in cohort if t["_family"] == "KXHIGHAUS"]
    for bucket in ("B", "T"):
        sub = []
        for t in aus:
            parts = (t.get("ticker") or "").split("-")
            if len(parts) >= 3 and parts[2].startswith(bucket):
                sub.append(t)
        if not sub:
            print(f"  {bucket}-bucket: N=0")
        else:
            print(f"  {bucket}-bucket: N={len(sub)}, WR={fmt_pct(win_rate(sub))}, "
                  f"mean ${mean_pnl(sub):+.2f}, total ${total_pnl(sub):+.2f}")
    print()
    # KXHIGHAUS B-bucket sub-period — the watch metric
    print("KXHIGHAUS B-bucket per sub-period (the S100 watch metric):")
    for sp_name in ("apr20_apr29", "apr29_may10", "may10_may11", "post_may11"):
        sub = []
        for t in aus:
            if t["_sub_period"] != sp_name:
                continue
            parts = (t.get("ticker") or "").split("-")
            if len(parts) >= 3 and parts[2].startswith("B"):
                sub.append(t)
        if sub:
            print(f"  {sp_name:14s} B-bucket N={len(sub)}, WR={fmt_pct(win_rate(sub))}, "
                  f"mean ${mean_pnl(sub):+.2f}, total ${total_pnl(sub):+.2f}")

    # =============================== ANALYSIS G ===============================
    hr("ANALYSIS G — vig_stack_futures lean-in (Operating Posture)")
    print()
    print("Apr-30 +$172 KXMLBGAME finding (CLAUDE.md): unintended-but-profitable path.")
    print("Session 36 exemption tuple does NOT include vig_stack_futures → these auto-exit.")
    print()
    futures = [t for t in cohort if t["_class"] == "futures"]
    print(f"Futures paper rows (settled): N={len(futures)}")
    print()
    print(f"{'Ticker':<46s} {'Side':>4s} {'Entry¢':>7s} {'Status':>14s} {'PnL':>10s}")
    print("-" * 86)
    for t in sorted(futures, key=lambda x: x.get("timestamp", "")):
        tk = (t.get("ticker") or "")[:44]
        print(f"{tk:<46s} {t.get('side', ''):>4s} {entry_cents(t):>6.1f}¢ "
              f"{t.get('status', ''):>14s} {fmt_money(pnl(t)):>10s}")
    print()
    if futures:
        print(f"Futures TOTAL: ${total_pnl(futures):+.2f}, MEAN ${mean_pnl(futures):+.2f}/trade, "
              f"WR (PnL>0) {fmt_pct(win_rate(futures))}")
    print()
    # opp_type cross-reference
    fut_with_opp = [t for t in futures if t["_opp_type"] == "vig_stack_futures"]
    print(f"Of futures rows, {len(fut_with_opp)} have opp_type='vig_stack_futures' (trade_history.json cross-ref).")
    print()
    # Per-prefix breakdown (KXMLBGAME, KXNBA, etc.)
    print("Per-prefix breakdown:")
    pfx_counts = Counter()
    for t in futures:
        tk = t.get("ticker", "")
        pfx = next((p for p in FUTURES_PREFIXES if tk.startswith(p)), tk.split("-", 1)[0])
        pfx_counts[pfx] += 1
    for pfx, n in sorted(pfx_counts.items(), key=lambda kv: -kv[1]):
        sub = [t for t in futures if (t.get("ticker", "").startswith(pfx))]
        print(f"  {pfx:>14s}  N={n}  total {fmt_money(total_pnl(sub))}  mean {fmt_money(mean_pnl(sub))}/trade")

    # =============================== ANALYSIS H ===============================
    hr("ANALYSIS H — S100 forward-only ladder context field analysis")
    print()
    s100 = [t for t in cohort if "selected_rung_rank_asc" in t]
    print(f"Post-S100 trades carrying ladder_context: N={len(s100)}")
    if len(s100) < 5:
        print("Post-S100 N too thin for slicing — defer to S133 follow-up at N≥5 per slice.")
    else:
        for field in ("selected_rung_rank_asc", "selected_rung_rank_desc",
                      "forecast_bucket_distance", "ladder_total_yes_sum_cents",
                      "time_to_close_hr"):
            vals = [t[field] for t in s100 if t.get(field) is not None]
            if len(vals) < 4:
                continue
            med = statistics.median(vals)
            lo_b = [t for t in s100 if t.get(field) is not None and t[field] < med]
            hi_b = [t for t in s100 if t.get(field) is not None and t[field] >= med]
            if len(lo_b) >= 2 and len(hi_b) >= 2:
                d = mean_pnl(hi_b) - mean_pnl(lo_b)
                surface = " ⚠ surface" if abs(d) > 5.0 and min(len(lo_b), len(hi_b)) >= 5 else ""
                print(f"  {field}: lo N={len(lo_b)} mean {fmt_money(mean_pnl(lo_b))} | "
                      f"hi N={len(hi_b)} mean {fmt_money(mean_pnl(hi_b))} | "
                      f"Δ {fmt_money(d)}{surface}")

    # =============================== ANALYSIS I ===============================
    hr("ANALYSIS I — Counterfactual floor sweep (volatile families, post-Apr-20)")
    print()
    base_n = len(volatile_post)
    base_mean = mean_pnl(volatile_post)
    base_total = total_pnl(volatile_post)
    print(f"Baseline (floor 0.93): N={base_n}, total {fmt_money(base_total)}, "
          f"mean {fmt_money(base_mean)}/trade")
    print()
    print(f"{'Floor':>7s} {'N_remain':>10s} {'Total P&L':>14s} {'Mean P&L/trade':>17s} {'Δ vs 0.93':>14s} {'gate':>8s}")
    print("-" * 78)
    best_floor_delta = 0.0
    best_floor = None
    for floor in (0.93, 0.94, 0.95, 0.96, 0.97):
        remain = [t for t in volatile_post if (t.get("entry_price") or 0) >= floor]
        n = len(remain)
        tot = total_pnl(remain)
        mp = (tot / n) if n else 0.0
        delta = mp - base_mean
        gate = " ✓ +$1" if delta >= DELTA_GATE_DOLLARS else ""
        print(f"  {floor:.2f}  {n:>10d} {fmt_money(tot):>14s} {fmt_money(mp):>17s} "
              f"{fmt_money(delta):>14s} {gate:>8s}")
        if delta > best_floor_delta:
            best_floor_delta = delta
            best_floor = floor

    # =============================== ANALYSIS J ===============================
    hr("ANALYSIS J — Counterfactual family disable (currently-enabled volatile families)")
    print()
    enabled_volatile = sorted({
        t["_family"] for t in cohort
        if t["_class"] == "volatile" and t["_family"] not in DISABLED_FAMILIES
    })
    post_apr20 = [t for t in cohort if t["_sub_period"] != "pre_apr20"]
    base_mean_post = mean_pnl(post_apr20)
    print(f"Baseline (post-Apr-20 cohort): N={len(post_apr20)}, mean {fmt_money(base_mean_post)}/trade")
    print()
    print(f"{'Disable family':<14s} {'N_drop':>8s} {'N_remain':>9s} {'Mean P&L/trade':>17s} {'Δ vs base':>12s} {'gate':>8s}")
    print("-" * 78)
    best_fam_delta = 0.0
    best_fam = None
    for fam in enabled_volatile:
        remain = [t for t in post_apr20 if t["_family"] != fam]
        dropped = [t for t in post_apr20 if t["_family"] == fam]
        mp = mean_pnl(remain)
        delta = mp - base_mean_post
        gate = " ✓ +$1" if delta >= DELTA_GATE_DOLLARS else ""
        print(f"  {fam:<12s} {len(dropped):>8d} {len(remain):>9d} {fmt_money(mp):>17s} "
              f"{fmt_money(delta):>12s} {gate:>8s}")
        if delta > best_fam_delta:
            best_fam_delta = delta
            best_fam = fam

    # =============================== ANALYSIS K ===============================
    hr("ANALYSIS K — Bootstrap CIs on the most informative splits (10000 iter, seed=133)")
    print()
    stable_enabled_post = [t for t in post_apr20
                            if t["_class"] == "stable" and t["_family"] not in DISABLED_FAMILIES]
    volatile_enabled_post = [t for t in post_apr20
                              if t["_class"] == "volatile" and t["_family"] not in DISABLED_FAMILIES]
    print("Split 1: Stable-enabled vs Volatile-enabled mean P&L (post-Apr-20)")
    print(f"{'Cohort':<22s} {'N':>5s} {'Mean P&L':>10s} {'95% CI':>26s} {'CI excludes 0?':>16s}")
    print("-" * 86)
    for label, sub in (("Stable (enabled)", stable_enabled_post),
                       ("Volatile (enabled)", volatile_enabled_post)):
        pnls = [pnl(t) for t in sub]
        m, lo, hi = bootstrap_ci(pnls)
        if m is None:
            print(f"{label:<22s} {len(sub):>5d}  empty")
            continue
        excl = "yes ⚠" if (lo > 0 or hi < 0) else "no"
        print(f"{label:<22s} {len(sub):>5d} {fmt_money(m):>10s}   "
              f"[{lo:+.2f}, {hi:+.2f}]   {excl:>16s}")
    print()
    # Split 2: family with largest |Total P&L|
    fam_totals = {fam: total_pnl([t for t in cohort if t["_family"] == fam]) for fam in fam_counts}
    biggest_fam = max(fam_totals, key=lambda f: abs(fam_totals[f]))
    sub = [t for t in cohort if t["_family"] == biggest_fam]
    pnls = [pnl(t) for t in sub]
    m, lo, hi = bootstrap_ci(pnls)
    excl = "yes ⚠" if (m is not None and (lo > 0 or hi < 0)) else "no"
    print(f"Split 2: Largest |Total P&L| family — {biggest_fam} (total ${fam_totals[biggest_fam]:+.2f})")
    print(f"  N={len(sub)}, mean {fmt_money(m)}, 95% CI [{lo:+.2f}, {hi:+.2f}], CI excludes 0? {excl}")
    print()
    # Split 3 (bonus): futures cohort bootstrap (Operating Posture lean-in)
    if futures:
        pnls = [pnl(t) for t in futures]
        m, lo, hi = bootstrap_ci(pnls)
        excl = "yes ⚠" if (m is not None and (lo > 0 or hi < 0)) else "no"
        print(f"Split 3 (lean-in): vig_stack_futures cohort")
        print(f"  N={len(futures)}, mean {fmt_money(m)}, 95% CI [{lo:+.2f}, {hi:+.2f}], "
              f"CI excludes 0? {excl}")

    # =============================== DECISION FRAMING ===============================
    hr("DECISION FRAMING — Session 133 outcome signal summary")
    print()
    # (e) auto-exit regression check
    weather_post_s36 = [t for t in cohort
                        if t["_class"] in ("stable", "volatile")
                        and t["_sub_period"] in ("apr29_may10", "may10_may11", "post_may11")]
    if weather_post_s36:
        ee_w = sum(1 for x in weather_post_s36 if x["status"] == "exited_early") / len(weather_post_s36)
        ee_flag = " ⚠ >5% — investigate" if ee_w > 0.05 else " ✓ within tolerance"
        print(f"  (e) Post-S36 weather EE rate: {fmt_pct(ee_w)} on N={len(weather_post_s36)} "
              f"(stable+volatile only) — gate 5%{ee_flag}")
    # (f) S93 breach
    f_flag = " ⚠ REGRESSION" if s93_breach_count else " ✓ clean"
    print(f"  (f) S93 disable breaches (post-2026-05-10 KXHIGHCHI/KXINX entries): "
          f"{s93_breach_count}{f_flag}")
    # (b) floor adjustment
    if best_floor is not None:
        b_flag = " ✓ +$1 actionable" if best_floor_delta >= DELTA_GATE_DOLLARS else " — below +$1 gate"
        print(f"  (b) Best floor candidate: {best_floor:.2f} with Δ "
              f"{fmt_money(best_floor_delta)}/trade{b_flag}")
    # (c) family disable
    if best_fam is not None:
        c_flag = " ✓ +$1 actionable" if best_fam_delta >= DELTA_GATE_DOLLARS else " — below +$1 gate"
        print(f"  (c) Best volatile-family disable: {best_fam} with Δ "
              f"{fmt_money(best_fam_delta)}/trade{c_flag}")
    # (a) post-Apr-20 healthy?
    post_apr20_total = total_pnl(post_apr20)
    post_apr20_mean = mean_pnl(post_apr20)
    a_flag = " ✓ net positive" if post_apr20_total > 0 else " — still net negative"
    print(f"  (a) Post-Apr-20 cohort: total {fmt_money(post_apr20_total)}, "
          f"mean {fmt_money(post_apr20_mean)}/trade{a_flag}")
    # (d) futures pattern
    if futures:
        fut_total = total_pnl(futures)
        fut_mean = mean_pnl(futures)
        d_flag = " ⚠ lean-in surface" if (fut_total > 50 and len(futures) >= 5) else " — too thin or unprofitable"
        print(f"  (d) vig_stack_futures: N={len(futures)}, total {fmt_money(fut_total)}, "
              f"mean {fmt_money(fut_mean)}/trade{d_flag}")
    print()
    print("Interpretation → see CLAUDE-sessions.md S133 entry for outcome classification (a)–(i).")
    print()


if __name__ == "__main__":
    main()
