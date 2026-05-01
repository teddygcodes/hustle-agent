"""Finding dataclass + fingerprint + cross-run NEW/STABLE/RESOLVED dedup."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class Finding:
    heuristic: str
    severity: str  # 'info' | 'notable' | 'high'
    title: str
    summary: str
    evidence: dict
    suggested_action: str | None = None

    def fingerprint(self) -> str:
        keys = self.evidence.get("_fingerprint_keys")
        if not keys:
            raise ValueError(
                f"{self.heuristic}: Finding.evidence missing '_fingerprint_keys' — "
                f"every Finding must declare which evidence fields define identity"
            )
        material = json.dumps(
            [self.heuristic] + [self.evidence.get(k) for k in keys],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint()
        return d


def classify_findings(
    new_findings: list[Finding],
    prior_findings: list[Finding],
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """Return (new, stable, resolved) tuple based on fingerprint matching."""
    new_fps = {f.fingerprint(): f for f in new_findings}
    prior_fps = {f.fingerprint(): f for f in prior_findings}
    is_new = [f for fp, f in new_fps.items() if fp not in prior_fps]
    stable = [f for fp, f in new_fps.items() if fp in prior_fps]
    resolved = [f for fp, f in prior_fps.items() if fp not in new_fps]
    return is_new, stable, resolved


def write_findings_jsonl(path: Path, findings: Iterable[Finding]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for f in findings:
            fh.write(json.dumps(f.to_dict(), default=str) + "\n")


_DATE_RE = re.compile(r"discovery_findings_(\d{4}-\d{2}-\d{2})\.jsonl$")


def load_prior_findings(discovery_dir: Path) -> list[Finding]:
    """Load the most recent discovery_findings_YYYY-MM-DD.jsonl file in dir."""
    if not discovery_dir.exists():
        return []
    candidates = sorted(
        (p for p in discovery_dir.iterdir() if _DATE_RE.search(p.name)),
        key=lambda p: _DATE_RE.search(p.name).group(1),
        reverse=True,
    )
    if not candidates:
        return []
    out: list[Finding] = []
    for line in candidates[0].read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        d.pop("fingerprint", None)
        out.append(Finding(**d))
    return out
