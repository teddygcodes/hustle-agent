"""log_error_spike: STREAM bot.log + rotated, flag error fingerprints spiking in last 24h.

Catches new failure modes that erupt between sessions — would have flagged the
Apr 30 12-hour wedge within the first run after it began. Fingerprint = first 80
chars of the message after stripping the timestamp/level/logger prefix; recent
24h vs baseline 24h–192h ago.

Streaming-only. Per-fingerprint counters keyed in two dicts; memory bounded by
unique error-message-prefixes (~few KB even on 100k-line fixtures).
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from ..findings import Finding

BASELINE_WINDOW_HOURS = 168
RECENT_WINDOW_HOURS = 24
SPIKE_RATIO = 3.0
MIN_RECENT_COUNT = 5
HIGH_SEVERITY_RATIO = 10.0
HIGH_SEVERITY_MIN_COUNT = 20
ERROR_PATTERNS = ("ERROR", "CRITICAL", "Exception", "Traceback")
FINGERPRINT_PREFIX_LEN = 80

_LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[\w+\] [\w.]+: "
)


def _parse_log_line(line: str, now: dt.datetime) -> tuple[dt.datetime | None, str | None]:
    """Returns (line_ts, fingerprint_text) for ERROR-bearing lines, else (None, None).

    fingerprint_text = first FINGERPRINT_PREFIX_LEN chars after stripping the
    bot's [TS] [LEVEL] logger.name: prefix. Lines that don't match the prefix
    pattern but contain an error pattern (e.g. Traceback continuation lines)
    are NOT counted — we only fingerprint top-level error lines so we don't
    double-count multi-line tracebacks.
    """
    m = _LOG_LINE_RE.match(line)
    if not m:
        return None, None
    if not any(pat in line for pat in ERROR_PATTERNS):
        return None, None
    try:
        line_ts = dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=now.tzinfo
        )
    except ValueError:
        return None, None
    remainder = line[m.end():]
    fingerprint = remainder[:FINGERPRINT_PREFIX_LEN]
    return line_ts, fingerprint


class LogErrorSpike:
    name = "log_error_spike"
    data_sources = ("bot_log_iter",)

    def run(self, ctx) -> list[Finding]:
        now = ctx.loaded_at
        recent_cutoff = now - dt.timedelta(hours=RECENT_WINDOW_HOURS)
        baseline_cutoff = now - dt.timedelta(
            hours=RECENT_WINDOW_HOURS + BASELINE_WINDOW_HOURS
        )

        recent_counts: dict[str, int] = defaultdict(int)
        baseline_counts: dict[str, int] = defaultdict(int)

        for line in ctx.bot_log_iter():
            line_ts, fp = _parse_log_line(line, now)
            if line_ts is None or fp is None:
                continue
            if line_ts >= recent_cutoff:
                recent_counts[fp] += 1
            elif line_ts >= baseline_cutoff:
                baseline_counts[fp] += 1

        # Per-hour rates so the ratio compares like-to-like
        recent_hours = RECENT_WINDOW_HOURS
        baseline_hours = BASELINE_WINDOW_HOURS

        findings: list[Finding] = []
        for fp, recent_count in recent_counts.items():
            if recent_count < MIN_RECENT_COUNT:
                continue
            recent_rate = recent_count / recent_hours
            baseline_count = baseline_counts.get(fp, 0)
            baseline_rate = max(baseline_count / baseline_hours, 1e-9)  # avoid div-zero
            ratio = recent_rate / baseline_rate
            if ratio < SPIKE_RATIO:
                continue
            severity = (
                "high"
                if ratio >= HIGH_SEVERITY_RATIO and recent_count >= HIGH_SEVERITY_MIN_COUNT
                else "notable"
            )
            evidence = {
                "error_msg_prefix": fp,
                "recent_count_24h": recent_count,
                "baseline_count_168h": baseline_count,
                "rate_ratio": round(ratio, 2),
                "_fingerprint_keys": ["error_msg_prefix"],
            }
            findings.append(Finding(
                heuristic=self.name,
                severity=severity,
                title=(
                    f"Error spike ({ratio:.1f}× baseline): "
                    f"'{fp[:60]}{'…' if len(fp) > 60 else ''}'"
                ),
                summary=(
                    f"Error fingerprint '{fp[:80]}…' appeared {recent_count} times in the "
                    f"last 24h vs {baseline_count} in the prior {BASELINE_WINDOW_HOURS}h. "
                    f"Rate ratio {ratio:.1f}× — investigate before it propagates."
                ),
                evidence=evidence,
                suggested_action=(
                    f"grep '{fp[:40]}' bot/logs/bot.log to inspect recent occurrences. "
                    "Identify root cause; if external (Kalshi API, network), defer with a "
                    "watch-list entry. If internal, fix and re-deploy."
                ),
            ))
        return findings
