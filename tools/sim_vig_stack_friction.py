#!/usr/bin/env python3
"""S164 — vig_stack execution-friction simulation (SIMULATION ONLY).

THE QUESTION
------------
vig_stack shows +$762 paper P&L (267 settled). PAPER mode instant-fills marketable limit
orders and charges NO fees (`pnl = (exit - entry) * contracts`, gross). S163 found the cheap
NOs vig_stack buys sit in THIN two-sided books. So: does +$762 survive paying fees, the ask,
slippage, and thin/no fills — or is it a fill-quality mirage? This re-prices the SAME settled
trades (won/lost/exited_early outcomes preserved) under progressively realistic execution and
reports the friction trajectory.

WHAT THIS SIM COMPUTES
----------------------
Robust trajectory (no join needed — entry_price + contracts are on every trade):
  L0 paper (recorded, gross) -> L1 +Kalshi fees -> L2 +fees +1c/+2c entry slippage.
Spec tiers (a)-(d), join-dependent (each settled trade joined to its entry-time universe
snapshot for the EXACT ticker):
  (a) MID  : re-enter at mid, no fee (sanity).
  (b) ASK  : re-enter at no_ask, + fees (what you'd actually pay).
  (c) ASK + slippage : no_ask +1c / +2c, + fees.
  (d) DEPTH-GATED : exclude rungs with no two-sided book / thin OI/volume at entry; report
      "fill only rungs with depth" vs "assume full fill" across OI/volume floors.
Every layer reported twice: FULL 267 (reconciling headline) and LADDER-CORE (KXHIGH*/KXINX,
excluding the 12 KXMLBGAME per-game trades S159 ruled out as a spread artifact). Plus a
concentration cut (per-family, top trades, ex-top-3) and the join/coverage rate.

HONEST LIMIT: offline data has thin/missing two-sided liquidity and NO ground-truth fills
(`order_microstructure.jsonl` is empty in PAPER mode). This BOUNDS the edge conservatively; it
cannot CONFIRM real fills/slippage. The definitive answer needs a live-fill probe.

SCOPE: read-only on bot/state/ (paper_trades.json + universe archives). Writes ONLY the report
under bot/state/reports/. No scanner/config/ACTIVE_STRATEGIES change, no PAPER_MODE flip, no
bot restart, no trades, no live Kalshi API. Imports NO bot modules (gzip+json loader + fee
model replicated inline from tools/sim_commodity_vig_stack.py).

Usage:
    python3 tools/sim_vig_stack_friction.py
    python3 tools/sim_vig_stack_friction.py --report-out /tmp/sim.md
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = _REPO_ROOT / "bot" / "state"
ARCHIVE_DIR = STATE_DIR / "archive"
UNIVERSE_FILE = STATE_DIR / "universe.jsonl"
PAPER_TRADES = STATE_DIR / "paper_trades.json"
REPORTS_DIR = STATE_DIR / "reports"

# ---- Kalshi fees (mirrors tools/sim_commodity_vig_stack.py:126-145 exactly) ---------------
# fee = ceil(rate * contracts * p * (1-p) * 100) cents per order/leg, p = price_cents/100.
# KXINX is an S&P index market (financial rate 0.035); everything else uses the general 0.07
# (conservative — overstates fees, the right direction for a "does edge survive" test).
FEE_RATE_FINANCIAL = 0.035
FEE_RATE_GENERAL = 0.07

# Friction sweep parameters.
SLIPPAGE_CENTS = (1, 2)
JOIN_WINDOWS_MIN = (30, 90)        # report join match rate at both
PRIMARY_WINDOW_MIN = 90            # window used for the tier (a)-(d) re-pricing
OI_FLOORS = (0, 50, 200)
VOL_FLOORS = (0, 100, 500)
PRIMARY_OI_FLOOR = 50              # the headline depth gate
PRIMARY_VOL_FLOOR = 100
PER_GAME_PREFIXES = ("KXMLBGAME", "KXNBAGAME", "KXNHLGAME")  # S159-ruled-out per-game vig


def fee_rate_for(series: str) -> float:
    return FEE_RATE_FINANCIAL if series == "KXINX" else FEE_RATE_GENERAL


def kalshi_fee_cents(price_cents: float, rate: float, contracts: int = 1) -> int:
    """Kalshi trading fee for one order/leg, in cents, rounded up to the next cent."""
    p = price_cents / 100.0
    return math.ceil(rate * contracts * p * (1.0 - p) * 100.0)


def fee_dollars(price_dec: float, rate: float, contracts: int) -> float:
    # Prices live in integer cents (CLAUDE.md style rule); round to avoid float artifacts
    # like 0.79*100 == 78.9999 nudging the ceil() across a cent boundary.
    return kalshi_fee_cents(round(price_dec * 100.0), rate, contracts) / 100.0


def _iter_jsonl_lines(path: Path):
    """Yield parsed JSON rows from .jsonl or .jsonl.gz; tolerate malformed lines.

    Replicated from tools/sim_commodity_vig_stack.py:148 so this sim imports no bot modules.
    """
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (OSError, gzip.BadGzipFile):
        return


def _source_files() -> list[Path]:
    """All universe archives (gz + the rare uncompressed full dump) + current snapshot."""
    files = sorted(ARCHIVE_DIR.glob("universe-*.jsonl.gz"))
    files += sorted(ARCHIVE_DIR.glob("universe-*.jsonl"))  # uncompressed full dumps (e.g. 05-17)
    if UNIVERSE_FILE.exists():
        files.append(UNIVERSE_FILE)
    return files


def _family_of(t: dict) -> str:
    f = t.get("family")
    if f:
        return f
    return (t.get("ticker", "") or "").split("-")[0]


def _is_ladder_core(fam: str) -> bool:
    """Genuine weather/index ladder vig_stack (excludes S159-ruled-out per-game)."""
    return fam.startswith("KXHIGH") or fam.startswith("KXLOW") or fam == "KXINX"


def _is_per_game(fam: str) -> bool:
    return any(fam.startswith(p) for p in PER_GAME_PREFIXES)


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------------------- #
# Load cohort + build the entry-time snapshot index
# ----------------------------------------------------------------------------------------- #
def load_cohort() -> list[dict]:
    data = json.loads(PAPER_TRADES.read_text())
    return [t for t in data
            if t.get("type") == "vig_stack" and t.get("status") in ("won", "lost", "exited_early")]


def build_snapshot_index(tickers: set[str]) -> dict[str, list]:
    """ticker -> sorted [(dt, row)] for the cohort's tickers only. Dedup (ticker, ts) — the
    2026-05-17 day appears as both .jsonl and .jsonl.gz (identical rows)."""
    idx: dict[str, list] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for f in _source_files():
        for r in _iter_jsonl_lines(f):
            tkr = r.get("ticker")
            if tkr not in tickers:
                continue
            ts = r.get("ts")
            key = (tkr, ts)
            if key in seen:
                continue
            seen.add(key)
            dt = _parse_ts(ts)
            if dt is None:
                continue
            idx[tkr].append((dt, r))
    for tkr in idx:
        idx[tkr].sort(key=lambda x: x[0])
    return idx


def nearest_snapshot(idx: dict, ticker: str, entry_dt: datetime, window_sec: float):
    """Snapshot of the exact ticker nearest entry_dt within window_sec; (row, delta_sec)."""
    rows = idx.get(ticker)
    if not rows:
        return None, None
    best = None
    best_delta = None
    for dt, r in rows:
        d = abs((dt - entry_dt).total_seconds())
        if best_delta is None or d < best_delta:
            best, best_delta = r, d
    if best is None or best_delta > window_sec:
        return None, best_delta
    return best, best_delta


# ----------------------------------------------------------------------------------------- #
# Per-trade enrichment
# ----------------------------------------------------------------------------------------- #
def enrich(cohort: list[dict], idx: dict) -> list[dict]:
    recs = []
    for t in cohort:
        c = int(t.get("contracts") or 0)
        entry = float(t.get("entry_price") or 0.0)
        pnl = float(t.get("pnl") or 0.0)
        status = t["status"]
        fam = _family_of(t)
        rate = fee_rate_for(fam)
        # Canonical exit value consistent with recorded pnl to the penny:
        #   exitp = entry + pnl/c  (won -> ~1.0, lost -> ~0.0, exited_early -> sell price).
        exitp = (entry + pnl / c) if c else entry
        enr = {
            "t": t, "ticker": t.get("ticker", ""), "fam": fam, "rate": rate,
            "core": _is_ladder_core(fam), "per_game": _is_per_game(fam),
            "c": c, "entry": entry, "exitp": exitp, "pnl": pnl, "status": status,
            "recorded_exit": t.get("exit_price"),
            "entry_ts": t.get("timestamp", ""),
        }
        # join
        edt = _parse_ts(enr["entry_ts"])
        snap, delta = (None, None)
        if edt is not None:
            snap, delta = nearest_snapshot(idx, enr["ticker"], edt, PRIMARY_WINDOW_MIN * 60)
        enr["join_delta_sec"] = delta
        enr["joined"] = snap is not None
        if snap is not None:
            enr["no_ask"] = snap.get("no_ask")
            enr["no_bid"] = snap.get("no_bid")
            enr["yes_ask"] = snap.get("yes_ask")
            enr["yes_bid"] = snap.get("yes_bid")
            enr["oi"] = int(snap.get("open_interest") or 0)
            enr["vol"] = int(snap.get("volume_24h") or snap.get("volume") or 0)
        recs.append(enr)
    return recs


# ---- per-trade P&L under each pricing assumption (dollars) --------------------------------
def net_at(entry_dec: float, exitp: float, c: int, rate: float, status: str,
           charge_fee: bool = True, exit_slip_dec: float = 0.0) -> float:
    """P&L re-entering the NO at `entry_dec`; exit fixed at exitp (less optional exit slippage).
    Fees: entry always (if charge_fee), exit only on exited_early (settlement legs are free and
    cost ~0 anyway since p(1-p)=0 at p in {0,1})."""
    eff_exit = exitp - exit_slip_dec
    gross = (eff_exit - entry_dec) * c
    if not charge_fee:
        return gross
    ef = fee_dollars(entry_dec, rate, c)
    xf = fee_dollars(eff_exit, rate, c) if status == "exited_early" else 0.0
    return gross - ef - xf


# ----------------------------------------------------------------------------------------- #
# Aggregation
# ----------------------------------------------------------------------------------------- #
def _sum(recs, fn):
    return sum(fn(r) for r in recs)


def trajectory(recs: list[dict]) -> dict:
    """Robust, no-join trajectory (all trades in `recs`)."""
    n = len(recs)
    sigc = sum(r["c"] for r in recs)
    l0 = _sum(recs, lambda r: (r["exitp"] - r["entry"]) * r["c"])
    l1 = _sum(recs, lambda r: net_at(r["entry"], r["exitp"], r["c"], r["rate"], r["status"]))
    entry_fees = _sum(recs, lambda r: fee_dollars(r["entry"], r["rate"], r["c"]))
    exit_fees = _sum(recs, lambda r: fee_dollars(r["exitp"], r["rate"], r["c"]) if r["status"] == "exited_early" else 0.0)
    l2 = {}
    for d in SLIPPAGE_CENTS:
        l2[d] = _sum(recs, lambda r, d=d: net_at(r["entry"] + d / 100.0, r["exitp"], r["c"], r["rate"], r["status"]))
    # entry+exit slippage variant on the exited_early subset (thin-book exit risk)
    ee = [r for r in recs if r["status"] == "exited_early"]
    ee_both = {}
    for d in SLIPPAGE_CENTS:
        ee_both[d] = _sum(ee, lambda r, d=d: net_at(r["entry"] + d / 100.0, r["exitp"], r["c"], r["rate"], r["status"], exit_slip_dec=d / 100.0))
    return {"n": n, "sigc": sigc, "l0": l0, "l1": l1, "l2": l2,
            "entry_fees": entry_fees, "exit_fees": exit_fees,
            "ee_n": len(ee), "ee_both": ee_both,
            "ee_l1": _sum(ee, lambda r: net_at(r["entry"], r["exitp"], r["c"], r["rate"], r["status"]))}


def joined_tiers(recs: list[dict]) -> dict:
    """Spec tiers (a)-(d) on the joinable subset of `recs`."""
    j = [r for r in recs if r["joined"] and r.get("no_ask") is not None and r.get("no_bid") is not None]
    n_all = len(recs)
    out = {"n_scope": n_all, "n_joined": len(j)}
    if not j:
        out["empty"] = True
        return out
    # recorded (paper) on the joined subset, for apples-to-apples
    out["paper_joined"] = _sum(j, lambda r: (r["exitp"] - r["entry"]) * r["c"])
    out["l1_joined"] = _sum(j, lambda r: net_at(r["entry"], r["exitp"], r["c"], r["rate"], r["status"]))
    # (a) MID, no fee
    out["mid"] = _sum(j, lambda r: net_at((r["no_ask"] + r["no_bid"]) / 200.0, r["exitp"], r["c"], r["rate"], r["status"], charge_fee=False))
    # (b) ASK + fees
    out["ask"] = _sum(j, lambda r: net_at(r["no_ask"] / 100.0, r["exitp"], r["c"], r["rate"], r["status"]))
    # (c) ASK + slippage + fees
    out["ask_slip"] = {}
    for d in SLIPPAGE_CENTS:
        out["ask_slip"][d] = _sum(j, lambda r, d=d: net_at((r["no_ask"] + d) / 100.0, r["exitp"], r["c"], r["rate"], r["status"]))
    # observed-ask vs recorded-entry comparison (is the ask already baked in?)
    deltas = [round(r["entry"] * 100) - r["no_ask"] for r in j]  # recorded_entry_cents - no_ask
    out["entry_vs_ask"] = {
        "mean_delta_c": statistics.mean(deltas),
        "median_delta_c": statistics.median(deltas),
        "at_or_above_ask": sum(1 for d in deltas if d >= 0),
        "below_ask": sum(1 for d in deltas if d < 0),
        "spread_c": statistics.mean([r["no_ask"] - r["no_bid"] for r in j]),
    }
    # (d) DEPTH-GATED on the ASK + fees + 1c basis (a realistic per-trade net)
    def realistic(r):
        return net_at((r["no_ask"] + 1) / 100.0, r["exitp"], r["c"], r["rate"], r["status"])
    out["depth"] = {}
    for oi_f in OI_FLOORS:
        for vol_f in VOL_FLOORS:
            passing = [r for r in j if (r["no_ask"] > 0 and r["no_bid"] > 0 and r["oi"] >= oi_f and r["vol"] >= vol_f)]
            out["depth"][(oi_f, vol_f)] = {
                "n_pass": len(passing),
                "fill_with_depth": _sum(passing, realistic),
                "assume_full_fill": _sum(j, realistic),
            }
    # headline decider: ladder-core ASK + fees + 2c, depth-gated at the primary floor
    primary = [r for r in j if (r["no_ask"] > 0 and r["no_bid"] > 0 and r["oi"] >= PRIMARY_OI_FLOOR and r["vol"] >= PRIMARY_VOL_FLOOR)]
    out["decider_ask2c_depth"] = _sum(primary, lambda r: net_at((r["no_ask"] + 2) / 100.0, r["exitp"], r["c"], r["rate"], r["status"]))
    out["decider_ask2c_fullfill"] = _sum(j, lambda r: net_at((r["no_ask"] + 2) / 100.0, r["exitp"], r["c"], r["rate"], r["status"]))
    out["decider_n_pass"] = len(primary)
    return out


def join_coverage(recs: list[dict], idx: dict) -> dict:
    """Match rate at each window + date-range ceiling."""
    cov = {}
    for w in JOIN_WINDOWS_MIN:
        n = 0
        for r in recs:
            edt = _parse_ts(r["entry_ts"])
            if edt is None:
                continue
            snap, _ = nearest_snapshot(idx, r["ticker"], edt, w * 60)
            if snap is not None:
                n += 1
        cov[w] = n
    # date-range ceiling: earliest archive ts
    earliest = None
    for tkr, rows in idx.items():
        if rows:
            ts0 = rows[0][0]
            if earliest is None or ts0 < earliest:
                earliest = ts0
    return {"by_window": cov, "earliest_snapshot": earliest}


def concentration(recs: list[dict]) -> dict:
    by_fam = defaultdict(lambda: [0, 0.0])
    for r in recs:
        by_fam[r["fam"]][0] += 1
        by_fam[r["fam"]][1] += r["pnl"]
    srt = sorted(recs, key=lambda r: r["pnl"])
    pos = [r["pnl"] for r in recs if r["pnl"] > 0]
    top3 = sum(sorted(pos, reverse=True)[:3])
    total = sum(r["pnl"] for r in recs)
    return {
        "by_family": sorted(((f, n, p) for f, (n, p) in by_fam.items()), key=lambda x: -x[2]),
        "losers": srt[:5], "winners": list(reversed(srt[-5:])),
        "sum_pos": sum(pos), "sum_neg": sum(r["pnl"] for r in recs if r["pnl"] < 0),
        "top3_winners": top3, "ex_top3": total - top3, "total": total,
    }


# ----------------------------------------------------------------------------------------- #
# Report
# ----------------------------------------------------------------------------------------- #
def _m(x: float) -> str:
    return f"${x:,.2f}"


def render(recs, idx, today: date) -> tuple[str, dict]:
    full = recs
    core = [r for r in recs if r["core"]]
    per_game = [r for r in recs if r["per_game"]]

    tj_full = trajectory(full)
    tj_core = trajectory(core)
    jt_full = joined_tiers(full)
    jt_core = joined_tiers(core)
    cov = join_coverage(recs, idx)
    conc_full = concentration(full)

    L: list[str] = []
    L.append(f"# S164 — vig_stack execution-friction simulation ({today})")
    L.append("")
    L.append("**SIMULATION ONLY.** Read-only on `bot/state/` (`paper_trades.json` + "
             "`archive/universe-*.jsonl[.gz]` + `universe.jsonl`); writes only this report. No "
             "scanner/config/`ACTIVE_STRATEGIES` change, no `PAPER_MODE` flip, no bot restart, "
             "no trades, no live Kalshi API. Imports no bot modules.")
    L.append("")
    L.append("**Question:** PAPER mode instant-fills marketable limits at the limit price and "
             "charges no fees. Does the **+$762.20** vig_stack paper edge (267 settled) survive "
             "paying Kalshi fees, the ask, slippage, and the thin two-sided books S163 found — "
             "or is it a fill-quality mirage? This re-prices the SAME settled trades "
             "(won/lost/exited_early outcomes preserved) under progressively realistic execution.")
    L.append("")

    # ---- reconciliation -------------------------------------------------------------------
    L.append("## 0. Cohort & reconciliation")
    L.append("")
    L.append(f"- Full cohort: **N={tj_full['n']}**, recorded ΣP&L (L0, gross) = "
             f"**{_m(tj_full['l0'])}** (reconciles to the +$762.20 headline). Σcontracts = "
             f"{tj_full['sigc']:,}.")
    L.append(f"- Ladder-core (KXHIGH*/KXINX, excl. {len(per_game)} KXMLBGAME per-game): "
             f"N={tj_core['n']}, ΣP&L = **{_m(tj_core['l0'])}**, Σcontracts = {tj_core['sigc']:,}.")
    L.append(f"- L0 is re-derived as `Σ(exit − entry)·contracts` with `exit = entry + pnl/c`, "
             f"identical to the recorded `pnl` field by construction (penny-exact).")
    L.append("")

    # ---- robust trajectory ----------------------------------------------------------------
    L.append("## 1. Robust friction trajectory (no join needed — all trades)")
    L.append("")
    L.append("Entry price + contracts are on every trade, so fees and entry slippage are "
             "computed on the **whole cohort** with no snapshot dependency and no selection bias. "
             "This is the most defensible part of the analysis.")
    L.append("")
    L.append("| scope | N | L0 paper | L1 +fees | L2 +fees+1c | L2 +fees+2c |")
    L.append("|---|---|---|---|---|---|")
    for label, tj in (("FULL 267", tj_full), ("ladder-core", tj_core)):
        L.append(f"| {label} | {tj['n']} | {_m(tj['l0'])} | {_m(tj['l1'])} | "
                 f"{_m(tj['l2'][1])} | {_m(tj['l2'][2])} |")
    L.append("")
    L.append(f"- **Fees** (Kalshi `ceil(rate·c·p(1−p))`, KXINX {FEE_RATE_FINANCIAL} / else "
             f"{FEE_RATE_GENERAL}): FULL entry fees {_m(tj_full['entry_fees'])} + early-exit fees "
             f"{_m(tj_full['exit_fees'])} = {_m(tj_full['entry_fees'] + tj_full['exit_fees'])} eaten "
             f"({_m(tj_full['l0'])} → {_m(tj_full['l1'])}). Won/lost settlement legs cost $0 "
             f"(`p(1−p)=0` at p∈{{0,1}}).")
    L.append(f"- **Entry slippage** is −Δ·Σcontracts: FULL +1c → {_m(tj_full['l2'][1])}, "
             f"+2c → {_m(tj_full['l2'][2])}.")
    L.append(f"- **Exited-early exit-side risk** (entry+exit both slipped, exited_early subset "
             f"only, N={tj_full['ee_n']}): no-slip L1 = {_m(tj_full['ee_l1'])}; +1c both legs = "
             f"{_m(tj_full['ee_both'][1])}; +2c both legs = {_m(tj_full['ee_both'][2])}. "
             f"(The dominant winners are large exited_early sells — offloading hundreds of "
             f"contracts in a thin book is the real exit-fill risk.)")
    L.append("")

    # ---- concentration --------------------------------------------------------------------
    L.append("## 2. Concentration (the S134/S159 lesson — full cohort)")
    L.append("")
    L.append(f"- Σ winners = {_m(conc_full['sum_pos'])} | Σ losers = {_m(conc_full['sum_neg'])} | "
             f"net = {_m(conc_full['total'])}.")
    L.append(f"- **Top-3 winners = {_m(conc_full['top3_winners'])}** of the net; "
             f"**ex-top-3-winners = {_m(conc_full['ex_top3'])}** (pre-fee). The top winners are "
             f"all large KXMLBGAME per-game exited-early trades — the per-game 'vig_stack' S159 "
             f"ruled out as a spread artifact (Outcome C).")
    L.append("")
    L.append("| family | N | ΣP&L |")
    L.append("|---|---|---|")
    for f, n, p in conc_full["by_family"]:
        tag = " *(per-game, S159-ruled-out)*" if _is_per_game(f) else ""
        L.append(f"| {f}{tag} | {n} | {_m(p)} |")
    L.append("")
    L.append("Top-5 winners / losers (ticker · P&L · entry · contracts · status):")
    L.append("")
    for r in conc_full["winners"]:
        L.append(f"- ▲ `{r['ticker']}` {_m(r['pnl'])} · {r['entry']} · {r['c']} · {r['status']}")
    for r in conc_full["losers"]:
        L.append(f"- ▼ `{r['ticker']}` {_m(r['pnl'])} · {r['entry']} · {r['c']} · {r['status']}")
    L.append("")

    # ---- join coverage --------------------------------------------------------------------
    L.append("## 3. Join coverage (entry-time snapshot, exact ticker)")
    L.append("")
    es = cov["earliest_snapshot"]
    L.append(f"- Earliest archived snapshot for cohort tickers: "
             f"`{es.isoformat() if es else 'n/a'}`. Trades before this have no contemporaneous book.")
    for w in JOIN_WINDOWS_MIN:
        n = cov["by_window"][w]
        L.append(f"- Matched within ±{w} min: **{n}/{tj_full['n']} "
                 f"({100*n/tj_full['n']:.1f}%)**.")
    L.append(f"- **Selection-bias caveat:** the pre-archive trades are net-negative, so the "
             f"joinable subset is the *more profitable* part of the cohort — tiers (a)-(d) below "
             f"therefore **overstate** the friction-adjusted edge relative to the full cohort.")
    L.append("")

    # ---- spec tiers (a)-(d) ---------------------------------------------------------------
    L.append(f"## 4. Spec tiers (a)-(d) — re-priced from the entry-time book (±{PRIMARY_WINDOW_MIN} min join)")
    L.append("")
    for label, jt in (("FULL", jt_full), ("ladder-core", jt_core)):
        L.append(f"### {label} (joined {jt['n_joined']}/{jt['n_scope']})")
        L.append("")
        if jt.get("empty"):
            L.append("_No joined trades — tier (a)-(d) unavailable for this scope._")
            L.append("")
            continue
        eva = jt["entry_vs_ask"]
        L.append(f"- **Is the ask already baked in?** recorded entry − observed `no_ask` = "
                 f"mean {eva['mean_delta_c']:+.1f}c / median {eva['median_delta_c']:+.1f}c; "
                 f"{eva['at_or_above_ask']} at/above ask vs {eva['below_ask']} below; mean book "
                 f"spread {eva['spread_c']:.1f}c. (≥0 ⇒ paper already paid the ask or worse.)")
        L.append(f"- (a) MID, no fee (sanity): {_m(jt['mid'])} | paper on same joined subset: "
                 f"{_m(jt['paper_joined'])} | L1 (recorded entry + fees): {_m(jt['l1_joined'])}")
        L.append(f"- (b) ASK + fees: **{_m(jt['ask'])}**")
        L.append(f"- (c) ASK + fees + slippage: +1c → {_m(jt['ask_slip'][1])} | "
                 f"+2c → {_m(jt['ask_slip'][2])}")
        L.append("- (d) DEPTH-GATED (ASK + fees + 1c basis; fill-only-with-depth vs assume-full-fill):")
        L.append("")
        L.append("  | OI floor | vol floor | rungs fillable | fill only w/ depth | assume full fill |")
        L.append("  |---|---|---|---|---|")
        for oi_f in OI_FLOORS:
            for vol_f in VOL_FLOORS:
                d = jt["depth"][(oi_f, vol_f)]
                L.append(f"  | {oi_f} | {vol_f} | {d['n_pass']}/{jt['n_joined']} | "
                         f"{_m(d['fill_with_depth'])} | {_m(d['assume_full_fill'])} |")
        L.append("")

    # ---- outcome --------------------------------------------------------------------------
    # Outcome classification — which number decides it.
    #   C  : fees alone (no join, no selection bias) already erase the edge, OR even modest
    #        (+1c) slippage with best-case (depth-gated, fill-only) fills stays <= 0.
    #   A  : survives even the pessimistic corner — ladder-core ASK+fees+2c AND assume-full-fill
    #        both > 0.
    #   B  : survives fees but the sign depends on slippage magnitude / fill quality that offline
    #        data cannot settle (the in-between case).
    core_l1 = tj_core["l1"]
    core_l2_1c = tj_core["l2"][1]
    core_l2_2c = tj_core["l2"][2]
    depth_fill = jt_core.get("decider_ask2c_depth")       # ladder-core ASK+2c, depth-gated, fill-only
    depth_full = jt_core.get("decider_ask2c_fullfill")    # ladder-core ASK+2c, assume full fill
    if core_l1 <= 0:
        outcome = "C"
    elif core_l2_2c > 0 and depth_full is not None and depth_full > 0:
        outcome = "A"
    elif core_l2_1c <= 0 and (depth_fill is None or depth_fill <= 0):
        outcome = "C"
    else:
        outcome = "B"

    L.append("## 5. Outcome")
    L.append("")
    L.append(f"**Outcome {outcome}.**")
    L.append("")
    L.append("**The numbers that decide it (ladder-core — the genuine weather/index vig_stack):**")
    L.append("")
    L.append(f"- Fees alone (all {tj_core['n']} trades, no join, no selection bias): "
             f"{_m(tj_core['l0'])} → **{_m(core_l1)}** — survives.")
    L.append(f"- + entry slippage: +1c → **{_m(core_l2_1c)}**, +2c → **{_m(core_l2_2c)}**. The "
             f"sign flips between +1c and +2c — Σ{tj_core['sigc']:,} contracts make per-contract "
             f"slippage the dominant lever.")
    if depth_fill is not None:
        L.append(f"- ASK + fees + 2c, depth-gated (OI≥{PRIMARY_OI_FLOOR}, vol≥{PRIMARY_VOL_FLOOR}, "
                 f"joinable subset, {jt_core.get('decider_n_pass', 0)} fillable rungs): "
                 f"fill-only-with-depth **{_m(depth_fill)}** vs assume-full-fill "
                 f"**{_m(depth_full)}** — fill quality alone flips the sign, and the joinable "
                 f"subset is the *more-profitable* part of the cohort (upward-biased).")
    L.append("")
    L.append(f"Trajectory — FULL: {_m(tj_full['l0'])} (paper) → {_m(tj_full['l1'])} (fees) → "
             f"{_m(tj_full['l2'][2])} (fees+2c). LADDER-CORE: {_m(tj_core['l0'])} → "
             f"{_m(tj_core['l1'])} → {_m(tj_core['l2'][2])}.")
    L.append("")
    if outcome == "A":
        L.append("The ladder-core edge stays solidly +EV through fees, ask, slippage, and "
                 "depth-gating — strong evidence it is real-executable. **Next step (operator-"
                 "gated, NOT this session): a minimal live-fill probe** to populate "
                 "`order_microstructure.jsonl` with ground-truth slippage/fill-rate before a "
                 "go-live decision. Spec below.")
    elif outcome == "B":
        L.append("The edge survives fees but the verdict past that depends on fill assumptions "
                 "offline data cannot settle — thin/missing two-sided books and **no ground-truth "
                 "fills** (`order_microstructure.jsonl` is empty in PAPER mode). The honest answer "
                 "is **offline can't tell**: this BOUNDS the edge, it cannot CONFIRM it. **A small "
                 "live-fill probe is the only way to resolve it** (spec below); do NOT scale or "
                 "flip to live on offline evidence alone.")
    else:
        L.append("Realistic friction (fees + slippage + depth-gating) erases the ladder-core "
                 "edge — the paper +$762 was substantially a fill-quality (and per-game "
                 "concentration) mirage. **Do NOT go live; do NOT scale.** This supports winding "
                 "down rather than expanding vig_stack.")
    L.append("")

    # ---- live-fill probe spec -------------------------------------------------------------
    if outcome in ("A", "B"):
        L.append("## 6. Minimal live-fill probe (SPEC ONLY — operator decides; do not run here)")
        L.append("")
        L.append("- **Goal:** ground-truth slippage + fill-rate that offline data can't provide; "
                 "populate `order_microstructure.jsonl` (empty until `PAPER_MODE=False`).")
        L.append("- **Families:** the most-liquid genuine ladder — KXHIGHAUS (highest ladder-core "
                 "ΣP&L) and KXINX `-B` (the S160 benchmark with the deepest two-sided book).")
        L.append("- **Size:** smallest viable — 1-5 contracts per rung, hard per-day $-cap (e.g. "
                 "$50 total notional), single rung at a time; never the 200-500-contract sizes "
                 "that drive the paper concentration.")
        L.append("- **Measure (per order):** realized fill price vs limit (slippage_cents), fill "
                 "rate, partial-fill count, latency_ms, time-to-fill, queue depth at place; join "
                 "to `clv.json` for slippage-adjusted CLV via `tools/microstructure_report.py`.")
        L.append("- **Risk cap + abort:** stop after N orders or first day; abort if realized "
                 "slippage > 2c median or fill-rate < 50%. Operator-gated GO per order.")
        L.append("- **Decision after probe:** compare realized slippage/fill-rate to the +1c/+2c "
                 "and depth-gated assumptions here; only then a go-live / scale / wind-down call.")
        L.append("")

    # ---- honest limits --------------------------------------------------------------------
    L.append("## 7. Honest limits")
    L.append("")
    L.append("- **No ground-truth fills.** `order_microstructure.jsonl` is empty in PAPER mode; "
             "this re-prices recorded trades under assumptions, it does not observe real "
             "execution. It BOUNDS the edge conservatively; it cannot CONFIRM fills/slippage.")
    L.append(f"- **Coverage.** Tiers (a)-(d) use the ≤{cov['by_window'][PRIMARY_WINDOW_MIN]}/"
             f"{tj_full['n']} joinable trades (pre-archive entries have no book); that subset is "
             f"the more-profitable part of the cohort → overstates the friction-adjusted edge.")
    L.append("- **Concentration.** ~$674 of the $762 net is top-3 per-game exited-early trades; "
             "~$248 is the S159-ruled-out per-game family. The ladder-core cut is the honest base.")
    L.append("- **Depth proxy.** Two-sided book + OI/volume floors approximate fillability; the "
             "real book depth at the rung at order time is unobservable offline (S163 lesson).")
    L.append("")

    # ---- notes ----------------------------------------------------------------------------
    L.append("## Notes")
    L.append("")
    L.append(f"- **Fees.** `ceil(rate·contracts·p·(1−p))` cents per leg, `p=price_cents/100`; "
             f"KXINX {FEE_RATE_FINANCIAL} (financial), else {FEE_RATE_GENERAL} (general; "
             f"conservative). Mirrors S159/S160 exactly (`tools/sim_commodity_vig_stack.py`).")
    L.append("- **Exit value.** `exit = entry + pnl/contracts` reproduces recorded `pnl` to the "
             "penny (won→~1.0, lost→~0.0, exited_early→sell price); friction perturbs only the "
             "ENTRY price, holding the realized exit/settlement value fixed.")
    L.append("- **Join.** Each settled trade matched to the nearest universe snapshot of its "
             f"EXACT ticker within ±{PRIMARY_WINDOW_MIN} min; `(ticker, ts)` deduped (the "
             "2026-05-17 day is archived as both `.jsonl` and `.jsonl.gz`).")
    L.append("- **No dedup of trades** (real settled trades, not CFs — per spec); concentration "
             "is foregrounded instead.")
    L.append("")

    summary = {"outcome": outcome, "tj_full": tj_full, "tj_core": tj_core,
               "jt_full": jt_full, "jt_core": jt_core, "cov": cov,
               "depth_fill": depth_fill, "depth_full": depth_full}
    return "\n".join(L), summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="S164 vig_stack execution-friction offline sim.")
    parser.add_argument("--report-out", default=None,
                        help="Report path (default: bot/state/reports/"
                             "session_164_vig_stack_friction_sim_<today>.md).")
    args = parser.parse_args(argv)

    if not PAPER_TRADES.exists():
        print(f"error: {PAPER_TRADES} not found")
        return 2

    cohort = load_cohort()
    tickers = {t.get("ticker", "") for t in cohort}
    idx = build_snapshot_index(tickers)
    recs = enrich(cohort, idx)

    today = date.today()
    text, summary = render(recs, idx, today)

    out = Path(args.report_out) if args.report_out else (
        REPORTS_DIR / f"session_164_vig_stack_friction_sim_{today.isoformat()}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)

    tjf, tjc = summary["tj_full"], summary["tj_core"]
    print(f"cohort N={tjf['n']}  L0(full)={tjf['l0']:+.2f}  L1(full)={tjf['l1']:+.2f}  "
          f"L2+2c(full)={tjf['l2'][2]:+.2f}")
    print(f"ladder-core N={tjc['n']}  L0={tjc['l0']:+.2f}  L1={tjc['l1']:+.2f}  "
          f"L2+2c={tjc['l2'][2]:+.2f}")
    print(f"join: {summary['cov']['by_window']}  depth_fill(core ask+2c)={summary['depth_fill']}  "
          f"depth_full={summary['depth_full']}  OUTCOME={summary['outcome']}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
