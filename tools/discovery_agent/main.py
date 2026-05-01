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


def _render_cross_cohort_context(f: Finding) -> str | None:
    """Session 47: render the 'Cross-cohort context' sub-block for counterfactual_hotspots
    findings. Returns None for any other heuristic OR when the cross-cohort keys are
    not present (defensive — older fingerprints from a different agent version)."""
    if f.heuristic != "counterfactual_hotspots":
        return None
    ev = f.evidence
    required = (
        "cross_cohort_total_n",
        "cross_cohort_n_sports",
        "cross_cohort_mean_clv_cents",
        "cross_cohort_trimmed_mean_clv_cents",
        "cross_cohort_n_positive_sports",
        "cross_cohort_n_negative_sports",
        "cross_cohort_breakdown",
        "n_disabled_sport_cohorts_in_top3",
    )
    if any(k not in ev for k in required):
        return None

    breakdown = ev["cross_cohort_breakdown"] or []
    top3_positive = sorted(
        [(sport, n, mean) for sport, n, mean in breakdown if mean > 0],
        key=lambda t: -t[2],
    )[:3]
    top3_str = (
        ", ".join(f"{sport} {mean:+.1f}¢" for sport, _n, mean in top3_positive)
        if top3_positive
        else "none"
    )

    raw = ev["cross_cohort_mean_clv_cents"]
    trimmed = ev["cross_cohort_trimmed_mean_clv_cents"]
    n_disabled = ev["n_disabled_sport_cohorts_in_top3"]

    raw_aligns = raw >= 0 and trimmed >= 0
    if raw_aligns and n_disabled == 0:
        verdict = "treat as actionable"
        align_phrase = "aligns with"
    else:
        verdict = "investigate gate-flow caveats before treating as actionable"
        align_phrase = "does NOT clear"

    disabled_clause = (
        f" **{n_disabled} of top-3 positive cohorts are in MOMENTUM_DISABLED_SPORTS** "
        f"(relaxing the gate produces zero new actual trades for them)."
        if n_disabled > 0
        else ""
    )

    return (
        f"**Cross-cohort context:** Gate fires across {ev['cross_cohort_n_sports']} sports "
        f"(n={ev['cross_cohort_total_n']} combined). "
        f"{ev['cross_cohort_n_positive_sports']} cohorts positive ({top3_str}), "
        f"{ev['cross_cohort_n_negative_sports']} negative. "
        f"Cross-cohort mean **{raw:+.2f}¢**, outlier-trimmed {trimmed:+.2f}¢."
        f"{disabled_clause} Per-cohort signal {align_phrase} cross-cohort hygiene — {verdict}."
    )


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
        ]
        cross_cohort = _render_cross_cohort_context(f)
        if cross_cohort:
            lines += [cross_cohort, ""]
        lines += [
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
