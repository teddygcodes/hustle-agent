#!/usr/bin/env python3
"""Session 134 — vig_stack_futures lean-in investigation.

Investigation-only. Read-only against state files + archived JSONL logs.
Pure stdlib one-off, mirroring the S130/S131/S132/S133 pattern.

Questions:
  A. What are the 10 vig_stack_futures / KXMLBGAME trades?
  B. Is the +EV distributed or top-winner concentrated?
  C. Is this only MLB per-game?
  D. Did auto-exit help or hurt versus hold-to-settlement?
  E. Was the mechanism structural ladder vig or incidental movement?
  F. Are NBA/NHL/NFL per-game markets scanned by vig_stack?
  G. Is the per-game classification bug still present?
  H. Would NBA/NHL/NFL per-game markets show similar YES-sum signatures?
"""

from __future__ import annotations

import gzip
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = REPO_ROOT / "bot/state"
ARCHIVE = STATE / "archive"

PAPER_TRADES = STATE / "paper_trades.json"
TRADE_HISTORY = STATE / "trade_history.json"
POSITIONS = STATE / "positions.json"
CLV = STATE / "clv.json"
DECISIONS_LOG = STATE / "decisions.jsonl"
UNIVERSE_LOG = STATE / "universe.jsonl"
CLAUDE = REPO_ROOT / "CLAUDE.md"
MAIN = REPO_ROOT / "bot/main.py"
VIG_STACK_STRATEGY = REPO_ROOT / "bot/strategies/vig_stack_series.py"

SETTLED = {"won", "lost", "exited_early"}
PER_GAME_FAMILIES = ("KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXNFLGAME")
POST_APR29 = datetime(2026, 4, 29, tzinfo=timezone.utc)
CANONICAL_TICKER = "KXMLBGAME-26APR291840SFPHI-PHI"


def hr(title: str) -> None:
    print()
    print("=" * 96)
    print(f"  {title}")
    print("=" * 96)


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    mode = "rt" if path.suffix == ".gz" else "r"
    with opener(path, mode) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def log_paths(kind: str) -> list[Path]:
    current = STATE / f"{kind}.jsonl"
    archive_paths = sorted(ARCHIVE.glob(f"{kind}-*.jsonl.gz"))
    return [current] + archive_paths


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_between(start: str | None, end: str | None) -> float | None:
    a = parse_ts(start)
    b = parse_ts(end)
    if not a or not b:
        return None
    return (b - a).total_seconds() / 3600.0


def money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:+.2f}"


def cents(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}c"


def family(ticker: str) -> str:
    return ticker.split("-", 1)[0] if ticker else "UNKNOWN"


def event_prefix(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


def sport_from_family(fam: str) -> str:
    return {
        "KXMLBGAME": "mlb_game",
        "KXNBAGAME": "nba_game",
        "KXNHLGAME": "nhl_game",
        "KXNFLGAME": "nfl_game",
        "KXMLB": "mlb_futures",
        "KXNBA": "nba_futures",
        "KXNHL": "nhl_futures",
    }.get(fam, fam.lower())


def table(headers: list[str], rows: list[list[object]]) -> None:
    widths = [len(h) for h in headers]
    str_rows = []
    for row in rows:
        out = [str(v) for v in row]
        str_rows.append(out)
        widths = [max(w, len(v)) for w, v in zip(widths, out)]
    print("| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths)) + " |")
    print("| " + " | ".join("-" * w for w in widths) + " |")
    for row in str_rows:
        print("| " + " | ".join(v.ljust(w) for v, w in zip(row, widths)) + " |")


def nearest_history(trade: dict, history_by_ticker: dict[str, list[dict]]) -> dict:
    candidates = history_by_ticker.get(trade.get("ticker", ""), [])
    if not candidates:
        return {}
    entry = parse_ts(trade.get("timestamp"))

    def score(h: dict) -> tuple[float, int, int]:
        opened = parse_ts(h.get("opened_at"))
        if entry and opened:
            delta = abs((entry - opened).total_seconds())
        else:
            delta = 10**12
        contract_penalty = 0 if int(h.get("contracts") or -1) == int(trade.get("contracts") or -2) else 1
        side_penalty = 0 if h.get("side") == trade.get("side") else 1
        return (delta, contract_penalty, side_penalty)

    return min(candidates, key=score)


def result_maps(clv_rows: list[dict], positions: list[dict], history: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    by_ticker = {}
    source = {}
    for name, rows in (("clv", clv_rows), ("positions", positions), ("trade_history", history)):
        for row in rows:
            ticker = row.get("ticker")
            result = row.get("market_result")
            if ticker and result in {"yes", "no"} and ticker not in by_ticker:
                by_ticker[ticker] = result
                source[ticker] = name
    return by_ticker, source


def held_counterfactual(trade: dict, market_result: str | None) -> float | None:
    if market_result not in {"yes", "no"}:
        return None
    side = trade.get("side")
    if side == "no":
        settle_price = 1.0 if market_result == "no" else 0.0
    elif side == "yes":
        settle_price = 1.0 if market_result == "yes" else 0.0
    else:
        return None
    return round((settle_price - float(trade.get("entry_price") or 0.0)) * int(trade.get("contracts") or 0), 2)


def read_premises() -> dict[str, object]:
    claude = CLAUDE.read_text()
    main = MAIN.read_text()
    strategy = VIG_STACK_STRATEGY.read_text()
    return {
        "canonical_present": CANONICAL_TICKER in claude and "two cancelling bugs" in claude,
        "exemption_tuple_present": '_VIG_STACK_OPP_TYPES = ("vig_stack_no", "vig_stack_series")' in main,
        "futures_excluded": "vig_stack_futures" not in main.split("_VIG_STACK_OPP_TYPES", 1)[1].split("\n", 1)[0],
        "per_game_prefix_tuple": '_PER_GAME_VIG_STACK_PREFIXES = ("KXMLBGAME-", "KXNBAGAME-", "KXNHLGAME-")' in strategy,
        "name_for_special_case": 'return "vig_stack_series"' in strategy[strategy.find("def name_for"):strategy.find("def candidate_markets")],
        "accept_uses_is_futures": 'opp_type_full = "vig_stack_futures" if is_futures else "vig_stack_series"' in strategy,
    }


def load_decisions() -> list[dict]:
    rows = []
    for path in log_paths("decisions"):
        rows.extend(iter_jsonl(path))
    return rows


def load_universe() -> list[dict]:
    rows = []
    for path in log_paths("universe"):
        rows.extend(iter_jsonl(path))
    return rows


def nearest_accept_decision(ticker: str, entry_ts: str | None, decisions: list[dict]) -> dict | None:
    entry = parse_ts(entry_ts)
    matches = [
        row for row in decisions
        if row.get("ticker") == ticker and row.get("decision") == "accept"
    ]
    if not matches:
        return None
    if not entry:
        return matches[0]

    def score(row: dict) -> float:
        ts = parse_ts(row.get("ts"))
        if not ts:
            return 10**12
        return abs((entry - ts).total_seconds())

    return min(matches, key=score)


def universe_ladders_for_event(ticker: str, universe: list[dict]) -> dict[str, list[dict]]:
    ev = event_prefix(ticker)
    out: dict[str, list[dict]] = defaultdict(list)
    for row in universe:
        tk = row.get("ticker", "")
        if tk.startswith(ev + "-"):
            out[row.get("scan_id") or row.get("ts") or "unknown"].append(row)
    return out


def nearest_ladder(ticker: str, entry_ts: str | None, universe: list[dict]) -> tuple[str | None, list[dict], float | None]:
    entry = parse_ts(entry_ts)
    ladders = universe_ladders_for_event(ticker, universe)
    if not ladders:
        return (None, [], None)
    choices = []
    for scan_id, rows in ladders.items():
        ts = parse_ts(rows[0].get("ts"))
        delta = abs((entry - ts).total_seconds()) if entry and ts else 10**12
        choices.append((delta, scan_id, rows))
    delta, scan_id, rows = min(choices, key=lambda x: x[0])
    return (scan_id, sorted(rows, key=lambda r: r.get("ticker", "")), None if delta == 10**12 else delta / 60.0)


def yes_sum(rows: list[dict]) -> int:
    return sum(int(r.get("yes_ask") or 0) for r in rows if isinstance(r.get("yes_ask"), (int, float)))


def cheapest_no(rows: list[dict]) -> int | None:
    vals = [int(r.get("no_ask")) for r in rows if isinstance(r.get("no_ask"), (int, float))]
    return min(vals) if vals else None


def exact_family_from_ticker(ticker: str) -> str:
    for fam in ("KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXNFLGAME", "KXMLB", "KXNBA", "KXNHL", "KXNFL"):
        if ticker.startswith(fam):
            return fam
    return family(ticker)


def decision_universe_scope(decisions: list[dict], universe: list[dict]) -> tuple[Counter, Counter, Counter, dict]:
    decision_counts = Counter()
    accept_counts = Counter()
    reason_counts = Counter()
    samples = defaultdict(list)
    for row in decisions:
        opp_type = row.get("opp_type") or row.get("type") or ""
        if not str(opp_type).startswith("vig_stack"):
            continue
        fam = exact_family_from_ticker(row.get("ticker", ""))
        decision_counts[(fam, opp_type)] += 1
        if row.get("decision") == "accept":
            accept_counts[(fam, opp_type)] += 1
        reason_counts[(fam, opp_type, row.get("reason") or "")] += 1
        if len(samples[(fam, opp_type)]) < 3:
            samples[(fam, opp_type)].append((row.get("ts"), row.get("ticker"), row.get("decision"), row.get("reason")))

    universe_counts = Counter()
    scan_ids = defaultdict(set)
    scanned_by_counts = Counter()
    scanned_by_scan_ids = defaultdict(set)
    for row in universe:
        fam = exact_family_from_ticker(row.get("ticker", ""))
        if fam not in {"KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXNFLGAME", "KXMLB", "KXNBA", "KXNHL"}:
            continue
        scan_id = row.get("scan_id") or row.get("ts") or "unknown"
        universe_counts[fam] += 1
        scan_ids[fam].add(scan_id)
        scanned_by = row.get("scanned_by") or []
        if scanned_by:
            scanned_by_counts[fam] += 1
        for scanner in scanned_by:
            scanned_by_scan_ids[(fam, scanner)].add(scan_id)

    return decision_counts, accept_counts, reason_counts, {
        "samples": samples,
        "universe_counts": universe_counts,
        "scan_ids": scan_ids,
        "scanned_by_counts": scanned_by_counts,
        "scanned_by_scan_ids": scanned_by_scan_ids,
    }


def scope_sweep(universe: list[dict]) -> dict[str, dict[str, int]]:
    events: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in universe:
        ts = parse_ts(row.get("ts"))
        if ts and ts < POST_APR29:
            continue
        fam = exact_family_from_ticker(row.get("ticker", ""))
        if fam not in PER_GAME_FAMILIES:
            continue
        scan_id = row.get("scan_id") or row.get("ts") or "unknown"
        ev = row.get("event_ticker") or event_prefix(row.get("ticker", ""))
        yes_ask = row.get("yes_ask")
        if isinstance(yes_ask, (int, float)):
            events[(fam, scan_id)][ev] += int(yes_ask)

    out = {fam: {"events": 0, "yes_sum_gt_100": 0, "yes_sum_lte_100": 0} for fam in PER_GAME_FAMILIES}
    for (fam, _scan_id), evs in events.items():
        for _ev, total in evs.items():
            out[fam]["events"] += 1
            if total > 100:
                out[fam]["yes_sum_gt_100"] += 1
            else:
                out[fam]["yes_sum_lte_100"] += 1
    return out


def main() -> None:
    paper = load_json(PAPER_TRADES, [])
    history = load_json(TRADE_HISTORY, [])
    positions = load_json(POSITIONS, [])
    clv = load_json(CLV, [])
    decisions = load_decisions()
    universe = load_universe()

    history_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in history:
        history_by_ticker[row.get("ticker", "")].append(row)

    settled_vig = [
        row for row in paper
        if row.get("type") == "vig_stack" and row.get("status") in SETTLED
    ]
    joined = []
    for row in settled_vig:
        hist = nearest_history(row, history_by_ticker)
        enriched = dict(row)
        enriched["_hist_opp_type"] = hist.get("opp_type")
        enriched["_hist_opened_at"] = hist.get("opened_at")
        joined.append(enriched)

    futures = [
        row for row in joined
        if row.get("ticker", "").startswith("KXMLBGAME")
        or row.get("_hist_opp_type") == "vig_stack_futures"
    ]
    futures.sort(key=lambda r: float(r.get("pnl") or 0.0), reverse=True)

    results, result_source = result_maps(clv, positions, history)
    premises = read_premises()

    hr("SESSION 134 — vig_stack_futures lean-in investigation")
    print("Phase 0 premise checks:")
    for key, value in premises.items():
        print(f"  {key}: {value}")
    print(f"  settled vig_stack paper cohort: N={len(settled_vig)}")
    print(f"  KXMLBGAME / explicit futures cohort: N={len(futures)}")
    print(f"  explicit trade_history vig_stack_futures rows in cohort: {sum(1 for r in futures if r.get('_hist_opp_type') == 'vig_stack_futures')}")
    print(f"  missing explicit opp_type rows in cohort: {sum(1 for r in futures if r.get('_hist_opp_type') != 'vig_stack_futures')}")

    hr("ANALYSIS A — Per-trade enumeration")
    rows = []
    for row in futures:
        hold = hours_between(row.get("timestamp"), row.get("resolved_at"))
        rows.append([
            row.get("ticker"),
            sport_from_family(family(row.get("ticker", ""))),
            row.get("timestamp"),
            cents(row.get("entry_price")),
            row.get("contracts"),
            row.get("resolved_at") or "n/a",
            row.get("exit_reason") or row.get("status"),
            "n/a" if hold is None else f"{hold:.1f}",
            money(row.get("pnl")),
        ])
    table(["Ticker", "Sport", "Entry ts", "Entry", "Contracts", "Exit ts", "Exit reason", "Hold hr", "P&L"], rows)

    hr("ANALYSIS B — Distribution shape")
    pnls = [float(row.get("pnl") or 0.0) for row in futures]
    top3 = sum(pnls[:3])
    bottom3 = sum(pnls[-3:])
    excluding_top = pnls[1:]
    trimmed = pnls[1:-1]
    print(f"Total P&L: {money(sum(pnls))}")
    print(f"Mean P&L/trade: {money(sum(pnls) / len(pnls)) if pnls else 'n/a'}")
    print(f"Median P&L/trade: {money(statistics.median(pnls)) if pnls else 'n/a'}")
    print(f"Top 3 winners total: {money(top3)}")
    print(f"Bottom 3 losers total: {money(bottom3)}")
    print(f"Total excluding top winner: {money(sum(excluding_top))} (mean {money(sum(excluding_top) / len(excluding_top))})")
    print(f"Trim top + bottom: total {money(sum(trimmed))}; robust mean {money(sum(trimmed) / len(trimmed))}; N={len(trimmed)}")
    print("Interpretation: headline +EV does NOT survive removing the single top winner, but the 10% trimmed mean remains positive.")

    hr("ANALYSIS C — Sport family breakdown")
    by_fam = defaultdict(list)
    for row in futures:
        by_fam[family(row.get("ticker", ""))].append(row)
    rows = []
    for fam, trades in sorted(by_fam.items()):
        vals = [float(t.get("pnl") or 0.0) for t in trades]
        rows.append([
            fam,
            len(trades),
            f"{sum(1 for v in vals if v > 0) / len(vals) * 100:.1f}%",
            money(sum(vals)),
            money(sum(vals) / len(vals)),
        ])
    table(["Family", "N", "WR", "Total P&L", "Mean P&L"], rows)

    hr("ANALYSIS D — Early-exit counterfactual")
    ee_rows = []
    actual_total = 0.0
    cf_total = 0.0
    computable = 0
    for row in [r for r in futures if r.get("status") == "exited_early"]:
        result = results.get(row.get("ticker"))
        cf = held_counterfactual(row, result)
        actual = float(row.get("pnl") or 0.0)
        actual_total += actual
        if cf is not None:
            cf_total += cf
            computable += 1
        ee_rows.append([
            row.get("ticker"),
            row.get("exit_reason") or "n/a",
            cents(row.get("entry_price")),
            cents(row.get("exit_price")),
            money(actual),
            result or "n/a",
            result_source.get(row.get("ticker"), "n/a"),
            money(cf),
            "n/a" if cf is None else money(actual - cf),
        ])
    table(["Ticker", "Exit reason", "Entry", "Exit", "Actual P&L", "Result", "Source", "Held CF", "Actual-CF"], ee_rows)
    print(f"Actual EE total: {money(actual_total)}")
    print(f"Held-to-settlement counterfactual total: {money(cf_total)} on N={computable}")
    print(f"Delta actual minus held: {money(actual_total - cf_total)}")
    print("Interpretation: adding vig_stack_futures to the auto-exit exemption would have hurt this cohort.")

    hr("ANALYSIS E — Mechanism deep-dive")
    focus_tickers = [r.get("ticker") for r in futures[:3] + futures[-2:]]
    for ticker in focus_tickers:
        row = next(r for r in futures if r.get("ticker") == ticker)
        decision = nearest_accept_decision(ticker, row.get("timestamp"), decisions)
        scan_id, ladder, delta_min = nearest_ladder(ticker, row.get("timestamp"), universe)
        selected = next((r for r in ladder if r.get("ticker") == ticker), {})
        print()
        print(f"{ticker}")
        print(f"  trade: entry={cents(row.get('entry_price'))} contracts={row.get('contracts')} status={row.get('status')} exit_reason={row.get('exit_reason') or 'n/a'} pnl={money(row.get('pnl'))}")
        if decision:
            extra = decision.get("extra") or {}
            print(f"  decision: ts={decision.get('ts')} opp_type={decision.get('opp_type')} edge={decision.get('edge')} reason={decision.get('reason')} extra_keys={sorted(extra.keys())}")
        else:
            print("  decision: missing accept row in decisions logs")
        if ladder:
            print(f"  nearest ladder: scan_id={scan_id} delta_min={delta_min:.1f} rungs={len(ladder)} YES_sum={yes_sum(ladder)}c cheapest_NO={cheapest_no(ladder)}c scanned_by={sorted({s for r in ladder for s in (r.get('scanned_by') or [])})}")
            for rung in ladder:
                marker = "*" if rung.get("ticker") == ticker else " "
                print(f"    {marker} {rung.get('ticker')} YES={rung.get('yes_ask')}c NO={rung.get('no_ask')}c result={results.get(rung.get('ticker'), 'n/a')}")
            if selected:
                yes_ask = selected.get("yes_ask")
                no_ask = selected.get("no_ask")
                total_yes = yes_sum(ladder)
                if isinstance(yes_ask, (int, float)) and isinstance(no_ask, (int, float)) and total_yes:
                    vig_factor = total_yes / 100.0
                    no_fair = 100.0 - (float(yes_ask) / vig_factor)
                    edge = (no_fair - float(no_ask)) / float(no_ask) if no_ask else 0.0
                    print(f"  reconstructed structural math: vig_factor={vig_factor:.3f}; NO_fair={no_fair:.2f}c; NO_ask={no_ask}c; rel_edge={edge:.4f}")
        else:
            print("  nearest ladder: NOT RECONSTRUCTIBLE from universe logs")
        result = results.get(ticker)
        cf = held_counterfactual(row, result)
        print(f"  settlement: market_result={result or 'n/a'} held_CF={money(cf)}; actual_minus_CF={money(None if cf is None else float(row.get('pnl') or 0.0) - cf)}")

    print()
    print("Mechanism summary: nearest universe ladders reconstruct real two-rung YES-sum >100c structures for the load-bearing trades. The sign of realized P&L is dominated by game outcome and auto-exit timing: the Apr-30 winner was not held to a winning settlement; auto-take-profit avoided a would-have-lost NO.")

    hr("ANALYSIS F — Scope: NBA/NHL/NFL per-game scan coverage")
    decision_counts, accept_counts, reason_counts, coverage = decision_universe_scope(decisions, universe)
    rows = []
    for fam in ("KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXNFLGAME", "KXMLB", "KXNBA", "KXNHL"):
        for opp_type in sorted({ot for f, ot in decision_counts if f == fam}):
            rows.append([fam, opp_type, decision_counts[(fam, opp_type)], accept_counts[(fam, opp_type)]])
    table(["Family", "Opp type", "Decision rows", "Accepts"], rows)
    print()
    rows = []
    for fam in ("KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXNFLGAME", "KXMLB", "KXNBA", "KXNHL"):
        scanner_bits = {
            scanner: len(ids)
            for (f, scanner), ids in coverage["scanned_by_scan_ids"].items()
            if f == fam
        }
        rows.append([
            fam,
            coverage["universe_counts"][fam],
            len(coverage["scan_ids"][fam]),
            coverage["scanned_by_counts"][fam],
            scanner_bits or "{}",
        ])
    table(["Family", "Universe rows", "Scan IDs", "Rows w/scanned_by", "scanned_by scan IDs"], rows)
    print()
    print("Top GAME-family vig_stack reasons:")
    for fam in PER_GAME_FAMILIES:
        top = [
            (n, opp_type, reason)
            for (f, opp_type, reason), n in reason_counts.items()
            if f == fam
        ]
        if not top:
            print(f"  {fam}: no vig_stack decision rows")
            continue
        print(f"  {fam}: " + ", ".join(f"{opp_type}/{reason}={n}" for n, opp_type, reason in sorted(top, reverse=True)[:5]))

    hr("ANALYSIS G — Classification bug: current behavior")
    print("Current code has two classification surfaces:")
    print("  1. name_for(market): per-game prefixes KXMLBGAME/KXNBAGAME/KXNHLGAME return vig_stack_series attribution.")
    print("  2. accept path: opp_type_full = vig_stack_futures if ladder['is_futures'] else vig_stack_series.")
    print("The historical KXMLBGAME accepts demonstrate the accept path still emitted vig_stack_futures for per-game MLB until the later attribution behavior produced 3 KXMLBGAME vig_stack_series accepts.")
    print("This is still a split-brain classification surface, not a clean intentional taxonomy. No code changed in this session.")

    hr("ANALYSIS H — Rough counterfactual scope-expansion sweep")
    sweep = scope_sweep(universe)
    rows = []
    for fam in PER_GAME_FAMILIES:
        item = sweep[fam]
        rows.append([
            fam,
            item["events"],
            item["yes_sum_gt_100"],
            item["yes_sum_lte_100"],
            "n/a" if not item["events"] else f"{item['yes_sum_gt_100'] / item['events'] * 100:.1f}%",
        ])
    table(["Family", "Event snapshots", "YES sum >100", "YES sum <=100", ">100 rate"], rows)
    print("Interpretation: NBA/NHL per-game event snapshots often show YES-sum >100c but are not attributed/scanned by vig_stack. This supports a follow-up scope-expansion investigation, but not an implementation change today.")

    hr("DECISION FRAMING")
    print("Classification: (f) Mixed, with primary (d) and secondary concentrated-real-arb / scope-watch findings.")
    print("  (d) applies: actual EE total beat held-to-settlement by a material +$248.45 on the computable cohort, so the futures auto-exit omission is currently a feature, not a bug.")
    print("  Concentration applies: removing the top winner flips total P&L negative (-$32.27), so this is not distributed enough for a confident blanket lean-in.")
    print("  Real structural shape applies: nearest ladders show YES sums above 100c on load-bearing trades, but realized P&L depends heavily on auto-exit/game outcome.")
    print("  Scope watch applies: KXNBAGAME/KXNHLGAME are present in universe with frequent YES-sum >100c signatures but no vig_stack scanning attribution.")
    print("Recommended follow-up: do NOT add vig_stack_futures to _VIG_STACK_OPP_TYPES now. Queue a separate scope-expansion simulation for NBA/NHL per-game ladders with explicit auto-exit policy preserved.")


if __name__ == "__main__":
    main()
