"""Discovery agent entry point. Run via `python3 -m tools.discovery_agent.main`."""

from __future__ import annotations

import datetime as dt
import traceback
from pathlib import Path

from .context import DEFAULT_REPO, DiscoveryContext
from .findings import Finding, classify_findings, load_prior_findings, write_findings_jsonl
from .heuristics.cadence_outcome import CadenceOutcome
from .heuristics.cohort_emergence import CohortEmergence
from .heuristics.counterfactual_hotspots import CounterfactualHotspots
from .heuristics.live_tick_anomalies import LiveTickAnomalies
from .heuristics.log_error_spike import LogErrorSpike
from .heuristics.outlier_pnl import OutlierPnl
from .heuristics.threshold_proximity import ThresholdProximity
from .heuristics.universe_gap import UniverseGap

DEFAULT_HEURISTICS = [
    OutlierPnl(),
    CohortEmergence(),
    ThresholdProximity(),
    CounterfactualHotspots(),
    UniverseGap(),
    LiveTickAnomalies(),
    CadenceOutcome(),
    LogErrorSpike(),
]
SEVERITY_RANK = {"high": 0, "notable": 1, "info": 2}


def _source_present(ctx: DiscoveryContext, attr: str) -> bool:
    val = getattr(ctx, attr, None)
    if callable(val):
        return True  # streamers — emptiness is determined on consumption
    if val is None:
        return False
    if hasattr(val, "__len__"):
        return len(val) > 0
    return True


def _render_evidence(ev: dict) -> str:
    lines = []
    for k, v in ev.items():
        if k.startswith("_"):
            continue
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines)


def _sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_RANK.get(f.severity, 99), f.heuristic, f.title))


def _write_markdown(
    path: Path,
    ctx: DiscoveryContext,
    new: list[Finding],
    stable: list[Finding],
    resolved: list[Finding],
    errors: list[tuple[str, str]],
) -> None:
    today = dt.date.today().isoformat()
    heuristics = list(DEFAULT_HEURISTICS)
    lines = [
        f"# Discovery Report — {today}",
        "",
        f"Run started: {ctx.loaded_at.isoformat()} (cutoff_days={ctx.cutoff_days})",
        f"Heuristics: {len(heuristics) - len(errors)}/{len(heuristics)} ran "
        f"({len(errors)} errors/skips)",
        f"Findings: {len(new)} NEW, {len(stable)} STABLE, {len(resolved)} RESOLVED",
        "",
        f"## NEW findings ({len(new)})",
        "",
    ]
    if not new:
        lines += ["(none this run)", ""]
    for f in _sort_findings(new):
        lines += [
            f"### [{f.severity.upper()}] {f.heuristic}: {f.title}",
            "",
            f.summary,
            "",
            f"**Suggested action:** {f.suggested_action or '—'}",
            "",
            "**Evidence:**",
            _render_evidence(f.evidence),
            "",
        ]
    lines += [f"## STABLE findings ({len(stable)})", ""]
    if not stable:
        lines.append("(none)")
    for f in _sort_findings(stable):
        lines.append(f"- [{f.heuristic}] {f.title} — fingerprint `{f.fingerprint()}`")
    lines += ["", f"## RESOLVED findings ({len(resolved)})", ""]
    if not resolved:
        lines.append("(none)")
    for f in _sort_findings(resolved):
        lines.append(f"- [{f.heuristic}] {f.title} — fingerprint `{f.fingerprint()}`")
    lines += ["", f"## Heuristic errors / skips ({len(errors)})", ""]
    if not errors:
        lines.append("(none)")
    for name, tb in errors:
        lines += [f"### {name}", "", "```", tb.strip(), "```", ""]
    lines += ["", f"## Load warnings ({len(ctx.load_warnings)})", ""]
    if not ctx.load_warnings:
        lines.append("(none)")
    for w in ctx.load_warnings:
        lines.append(f"- {w}")
    path.write_text("\n".join(lines) + "\n")


def run(repo: Path = DEFAULT_REPO) -> dict:
    ctx = DiscoveryContext.load(repo=repo)
    all_findings: list[Finding] = []
    errors: list[tuple[str, str]] = []

    for h in DEFAULT_HEURISTICS:
        missing = [src for src in h.data_sources if not _source_present(ctx, src)]
        if missing:
            errors.append((h.name, f"skipped: missing sources {missing}"))
            continue
        try:
            all_findings.extend(h.run(ctx))
        except Exception:  # heuristic isolation
            errors.append((h.name, traceback.format_exc()))

    discovery_dir = repo / "bot" / "state" / "discovery"
    prior = load_prior_findings(discovery_dir)
    new, stable, resolved = classify_findings(all_findings, prior)

    today = dt.date.today().isoformat()
    discovery_dir.mkdir(parents=True, exist_ok=True)
    write_findings_jsonl(discovery_dir / f"discovery_findings_{today}.jsonl", all_findings)
    _write_markdown(
        discovery_dir / f"discovery_report_{today}.md",
        ctx, new, stable, resolved, errors,
    )

    return {
        "all_findings": all_findings,
        "new": new,
        "stable": stable,
        "resolved": resolved,
        "errors": errors,
    }


def main() -> None:
    out = run()
    print(
        f"Discovery agent: {len(out['new'])} NEW, {len(out['stable'])} STABLE, "
        f"{len(out['resolved'])} RESOLVED, {len(out['errors'])} errors/skips"
    )


if __name__ == "__main__":
    main()
