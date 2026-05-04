#!/usr/bin/env python3
"""Excursion report — per-strategy median(MFE − exit_favorable) gap from clv.json.

Convention. MFE and exit_favorable are both in YES-cents-favorable space (the
side-aware "favorable-magnitude from entry"):
  - YES side: exit_favorable = closing_yes - entry_yes
  - NO  side: exit_favorable = (100 - entry_no) - closing_yes
              [= entry_yes_implied - closing_yes]
Both reduce to the same per-contract profit-in-cents semantics.

Mathematically equivalent to bot.clv.compute_clv_cents (Session 13b discipline:
same formula in both places). Computed locally so the report is self-contained
and the gate-units convention is legible right where the gap is calculated.

By definition gap = MFE - exit_favorable ≥ 0, because Session 16 extends
mfe_cents at settlement-time propagation in bot.clv.check_clv_settlements to
include the settlement event itself. A gap > 5¢ flags strategies where MFE
during open life materially exceeded the eventual exit-favorable — i.e., the
bot got into favorable territory and gave it back at exit.

Skips records missing mfe_cents (pre-Session-9 history).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLV_FILE = ROOT / "bot" / "state" / "clv.json"


def exit_favorable_magnitude(side: str, entry_price_cents: int, closing_yes_price: float) -> float:
    """Favorable-magnitude at exit, in YES-cents-favorable space.

    For YES: exit_yes - entry_yes. Positive when YES won (closing_yes=100, entry < 100).
    For NO:  entry_yes_implied - exit_yes = (100 - entry_no_price) - closing_yes.
             Positive when NO won (closing_yes=0, entry < 100).

    Mathematically equivalent to bot.clv.compute_clv_cents (Session 13b).
    """
    if side == "yes":
        return float(closing_yes_price) - float(entry_price_cents)
    return (100.0 - float(entry_price_cents)) - float(closing_yes_price)


def load_settled_with_excursion() -> list[dict]:
    if not CLV_FILE.exists():
        return []
    try:
        records = json.loads(CLV_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(records, list):
        return []
    return [
        r for r in records
        if isinstance(r, dict)
        and r.get("status") == "settled"
        and r.get("mfe_cents") is not None
        and r.get("clv_cents") is not None
        and r.get("closing_yes_price") is not None
        and r.get("side") in ("yes", "no")
        and r.get("entry_price_cents") is not None
    ]


REGIME_AXES = ("time_of_day", "day_of_week", "sport_phase", "event_horizon_hr")


def _regime_value(rec: dict, axis: str | None) -> str:
    if not axis:
        return "_all_"
    val = (rec.get("regime") or {}).get(axis)
    return str(val) if val is not None else "unknown_regime"


def generate_report(flag_threshold: int = 5, regime_by: str | None = None) -> str:
    records = load_settled_with_excursion()
    if not records:
        return "# Excursion report\n\n_No settled records with mfe_cents yet._"

    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        opp = r.get("opp_type", "unknown")
        regime = _regime_value(r, regime_by)
        by_key[(opp, regime)].append(r)

    out: list[str] = []
    suffix = f" (by {regime_by})" if regime_by else ""
    out.append(f"# Excursion report — MFE vs exit_favorable{suffix}")
    out.append("")
    out.append(f"Source: `bot/state/clv.json` ({len(records)} settled records with MFE data)")
    out.append("")
    if regime_by:
        out.append(f"| Strategy | {regime_by} | N | Median MFE¢ | Median exit¢ | Median gap¢ | Median ticks | Flag |")
        out.append("|---|---|---:|---:|---:|---:|---:|---|")
    else:
        out.append("| Strategy | N | Median MFE¢ | Median exit¢ | Median gap¢ | Median ticks | Flag |")
        out.append("|---|---:|---:|---:|---:|---:|---|")

    for (opp, regime) in sorted(by_key):
        recs = by_key[(opp, regime)]
        mfes = [int(r["mfe_cents"]) for r in recs]
        exits = [
            exit_favorable_magnitude(
                r["side"], int(r["entry_price_cents"]), float(r["closing_yes_price"]),
            )
            for r in recs
        ]
        gaps = [m - e for m, e in zip(mfes, exits)]
        ticks = [int(r.get("ticks_observed") or 0) for r in recs]
        median_gap = statistics.median(gaps)
        flag = "⚠️ exit-logic candidate" if median_gap > flag_threshold else ""
        if regime_by:
            out.append(
                f"| {opp} | {regime} | {len(recs)} | {statistics.median(mfes):.0f} | "
                f"{statistics.median(exits):.0f} | {median_gap:.0f} | "
                f"{statistics.median(ticks):.0f} | {flag} |"
            )
        else:
            out.append(
                f"| {opp} | {len(recs)} | {statistics.median(mfes):.0f} | "
                f"{statistics.median(exits):.0f} | {median_gap:.0f} | "
                f"{statistics.median(ticks):.0f} | {flag} |"
            )

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--flag-threshold",
        type=int,
        default=5,
        help="Flag strategies with median gap > N cents (default 5)",
    )
    ap.add_argument(
        "--regime-by",
        choices=list(REGIME_AXES),
        default=None,
        help="Sub-group rows by a regime axis (Session 14). Records lacking the "
             "regime field bucket as 'unknown_regime'.",
    )
    args = ap.parse_args()
    print(generate_report(flag_threshold=args.flag_threshold, regime_by=args.regime_by))
    return 0


if __name__ == "__main__":
    sys.exit(main())
