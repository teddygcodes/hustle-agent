#!/usr/bin/env python3
"""Session 161 — "Were the blocks right?" report.

Reads the now-settled bot/state/shadow_trades.jsonl and aggregates settled rows
by blocked_reason × family/sport, reporting raw AND deduped N (S86 key collapses
the re-emission inflation), win-rate, and would_pnl where computable. Emits a
per-cohort decision label per the S95/S97 rule — EVIDENCE ONLY; re-enable /
keep-disabled is a separate per-cohort follow-up.

Run:  python3 tools/shadow_settlement_report.py
Writes bot/state/reports/shadow_settlement_YYYY-MM-DD.md and prints the path.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.config import BOT_STATE_DIR  # noqa: E402
from bot.shadow_settlement import _load_shadow_rows  # noqa: E402

# S95/S97 decision rule thresholds.
_WR_WRONG = 0.55      # would-have WR above this (with +pnl where available) => block likely wrong
_WR_CONFIRMED = 0.40  # WR below this (or negative pnl) => block data-confirmed
_N_THIN = 10          # deduped N below this => evidence-only flag

_FAMILY_REASON = "family_disabled_reject"


def _day(row: dict) -> str:
    return (row.get("ts") or "")[:10]


def _cohort_key(row: dict) -> str:
    if row.get("blocked_reason") == _FAMILY_REASON:
        return row.get("family") or "?"
    return row.get("sport") or "?"


def _outcome(row: dict) -> str | None:
    return (row.get("extra") or {}).get("would_outcome")


def _wl(rows: list[dict]) -> tuple[int, int]:
    won = sum(1 for r in rows if _outcome(r) == "won")
    lost = sum(1 for r in rows if _outcome(r) == "lost")
    return won, lost


def _pnl_sum(rows: list[dict]) -> float | None:
    vals = [r["would_pnl"] for r in rows if r.get("would_pnl") is not None]
    return round(sum(vals), 2) if vals else None


def _dedup(rows: list[dict]) -> list[dict]:
    """One representative row per S86 (ticker, day) key — collapses re-emission."""
    seen: dict[tuple, dict] = {}
    for r in rows:
        seen.setdefault((r.get("ticker"), _day(r)), r)
    return list(seen.values())


def _cohort_stats(rows: list[dict]) -> dict:
    reps = _dedup(rows)
    won, lost = _wl(rows)
    dwon, dlost = _wl(reps)
    return {
        "raw_n": len(rows),
        "dedup_n": len(reps),
        "won": won, "lost": lost,
        "dwon": dwon, "dlost": dlost,
        "wr_raw": won / (won + lost) if (won + lost) else None,
        "wr_dedup": dwon / (dwon + dlost) if (dwon + dlost) else None,
        "pnl_raw": _pnl_sum(rows),
        "pnl_dedup": _pnl_sum(reps),
        "has_pnl": any(r.get("would_pnl") is not None for r in rows),
    }


def _decision_label(s: dict) -> str:
    wr = s["wr_dedup"]
    thin = " ⚠️ N-thin (evidence only)" if s["dedup_n"] < _N_THIN else ""
    if wr is None:
        return "no settled directional outcomes" + thin
    pnl = s["pnl_dedup"]
    if s["has_pnl"] and pnl is not None:
        if wr > _WR_WRONG and pnl > 0:
            verdict = "block likely WRONG (re-enable candidate)"
        elif wr < _WR_CONFIRMED or pnl < 0:
            verdict = "block data-CONFIRMED"
        else:
            verdict = "inconclusive"
    else:
        if wr > _WR_WRONG:
            verdict = "block likely WRONG (re-enable candidate)"
        elif wr < _WR_CONFIRMED:
            verdict = "block data-CONFIRMED"
        else:
            verdict = "inconclusive"
    return verdict + thin


def _pct(x: float | None) -> str:
    return f"{x:.0%}" if x is not None else "—"


def _money(x: float | None) -> str:
    return f"${x:+,.2f}" if x is not None else "—"


def build_report(rows: list[dict], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    settled = [r for r in rows if r.get("status") == "settled"]
    failed = [r for r in rows if r.get("status") == "settlement_failed"]
    still_open = [r for r in rows if r.get("status") == "open"]

    src = defaultdict(int)
    for r in settled:
        src[(r.get("extra") or {}).get("settlement_source", "?")] += 1
    undetermined = sum(1 for r in settled if _outcome(r) is None)

    cohorts: dict[tuple, list[dict]] = defaultdict(list)
    for r in settled:
        cohorts[(r.get("blocked_reason"), _cohort_key(r))].append(r)

    L: list[str] = []
    L.append(f"# S161 — Were the Blocks Right? Shadow-Settlement Report ({now:%Y-%m-%d})")
    L.append("")
    L.append("**EVIDENCE ONLY.** Settles `bot/state/shadow_trades.jsonl` (S95 blocked-opportunity "
             "ledger) and aggregates would-have outcomes per blocked cohort. Re-enable / "
             "keep-disabled is a per-cohort follow-up — this report produces the evidence, not the "
             "decision. Dedup uses the S86 `(ticker, day)` key; small cohorts that look big raw "
             "collapse hard.")
    L.append("")
    L.append("## 1. Coverage")
    L.append(f"- total shadow rows: **{len(rows)}** | settled: **{len(settled)}** | "
             f"settlement_failed: {len(failed)} | still open: {len(still_open)}")
    L.append(f"- settlement source: " + ", ".join(f"{k}={v}" for k, v in sorted(src.items())))
    L.append(f"- settled rows with no directional outcome (would_side null): {undetermined}")
    L.append("- Phase 0 gate: **PASS** — primary path is a zero-API local join against clv.json's "
             "`counterfactual_settled` records; Tier-2 event fetch + Tier-3 bounded probe cover the rest.")
    L.append("")

    fam = sorted((k for k in cohorts if k[0] == _FAMILY_REASON), key=lambda k: k[1])
    sport = sorted((k for k in cohorts if k[0] == "sport_disabled"), key=lambda k: k[1])
    reentry = sorted((k for k in cohorts if k[0] == "reentry_blocked"), key=lambda k: k[1])

    L.append("## 2. `family_disabled_reject` — would_pnl + WR (S93: KXINX, KXHIGHCHI)")
    L.append("")
    L.append("| family | raw N | dedup N | WR raw | WR dedup | would_pnl raw | would_pnl dedup | decision |")
    L.append("|---|---|---|---|---|---|---|---|")
    for key in fam:
        s = _cohort_stats(cohorts[key])
        L.append(f"| {key[1]} | {s['raw_n']} | {s['dedup_n']} | {_pct(s['wr_raw'])} | "
                 f"{_pct(s['wr_dedup'])} | {_money(s['pnl_raw'])} | {_money(s['pnl_dedup'])} | "
                 f"{_decision_label(s)} |")
    if not fam:
        L.append("| _(none settled)_ | | | | | | | |")
    L.append("")

    L.append("## 3. `sport_disabled` — direction WR (S97: atp/atp_challenger/wta/wta_challenger/nba)")
    L.append("")
    L.append("| sport | raw N | dedup N | won/lost | WR raw | WR dedup | decision |")
    L.append("|---|---|---|---|---|---|---|")
    for key in sport:
        s = _cohort_stats(cohorts[key])
        L.append(f"| {key[1]} | {s['raw_n']} | {s['dedup_n']} | {s['won']}/{s['lost']} | "
                 f"{_pct(s['wr_raw'])} | {_pct(s['wr_dedup'])} | {_decision_label(s)} |")
    if not sport:
        L.append("| _(none settled)_ | | | | | | |")
    L.append("")

    L.append("## 4. `reentry_blocked` (S90)")
    L.append("")
    L.append("| sport | raw N | dedup N | won/lost | WR dedup | would_pnl dedup | decision |")
    L.append("|---|---|---|---|---|---|---|")
    for key in reentry:
        s = _cohort_stats(cohorts[key])
        L.append(f"| {key[1]} | {s['raw_n']} | {s['dedup_n']} | {s['won']}/{s['lost']} | "
                 f"{_pct(s['wr_dedup'])} | {_money(s['pnl_dedup'])} | {_decision_label(s)} |")
    if not reentry:
        L.append("| _(none settled)_ | | | | | | |")
    L.append("")

    L.append("## 5. Reading")
    L.append("- **Decision rule (S95/S97):** deduped WR > 55% **and** positive would_pnl ⇒ block "
             "likely WRONG (re-enable candidate); deduped WR < 40% **or** negative would_pnl ⇒ block "
             "data-CONFIRMED; otherwise inconclusive.")
    L.append("- **N-thin flag** fires when deduped N < {0} — the family cohorts collapse hard on "
             "dedup (KXINX/KXHIGHCHI re-emit the same hourly/daily tickers many times), so their "
             "would_pnl signal is evidence, not a verdict.".format(_N_THIN))
    L.append("- `sport_disabled` rows are direction-only (no `would_contracts` at block time) → WR "
             "only, no would_pnl.")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    rows = _load_shadow_rows()
    now = datetime.now(timezone.utc)
    report = build_report(rows, now)
    out_dir = BOT_STATE_DIR / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"shadow_settlement_{now:%Y-%m-%d}.md"
    out_path.write_text(report)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
