"""Shared helpers + parameterized section renderers for daily/weekly reports.

Used by tools/daily_report.py (window=1) and tools/weekly_report.py (window=7).
Read-only: no state mutation, no schema changes.

The 10 "shared" sections defined here are window-parameterized. Daily and
weekly orchestrators wrap each via `_safe_section` so a single source crash
renders `_[section unavailable: REASON]_` and the report continues.

Migrated from tools/weekly_digest.py (Session 24): _safe_section, _parse_iso,
_demote_h1, _windows, plus the trade-activity (was section_pnl) and CF coverage
section bodies.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Iterator

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9
    ZoneInfo = None  # type: ignore

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Imported lazily inside renderers so a malformed module file in tools/ does not
# break the helpers module's import for unrelated callers (e.g. tests). The
# orchestrators wrap each section in _safe_section, so an ImportError surfaces
# as `[section unavailable: ...]` rather than a hard crash.

ET = ZoneInfo("America/New_York") if ZoneInfo else timezone.utc

STATE_DIR = _REPO_ROOT / "bot" / "state"
ARCHIVE_DIR = STATE_DIR / "archive"
REPORTS_DIR = STATE_DIR / "reports"
LOG_FILE = _REPO_ROOT / "bot" / "logs" / "bot.log"

PAPER_TRADES_FILE = STATE_DIR / "paper_trades.json"
CLV_FILE = STATE_DIR / "clv.json"
BOT_STATE_FILE = STATE_DIR / "bot_state.json"
LOCK_FILE = STATE_DIR / "bot.lock"
CADENCE_FILE = STATE_DIR / "tracker_cadence.jsonl"
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"
LIVE_TICKS_FILE = STATE_DIR / "live_ticks.jsonl"
UNIVERSE_FILE = STATE_DIR / "universe.jsonl"
LIVE_JOURNAL_FILE = STATE_DIR / "live_journal.json"

# All eight state streams the spec wants tracked for §10 file growth.
TRACKED_STATE_FILES: tuple[tuple[str, Path, str | None], ...] = (
    # (display_name, path, archive_prefix-or-None)
    ("decisions.jsonl", DECISIONS_FILE, "decisions"),
    ("clv.json", CLV_FILE, None),
    ("live_ticks.jsonl", LIVE_TICKS_FILE, "live_ticks"),
    ("universe.jsonl", UNIVERSE_FILE, "universe"),
    ("tracker_cadence.jsonl", CADENCE_FILE, "tracker_cadence"),
    ("live_journal.json", LIVE_JOURNAL_FILE, None),
    ("paper_trades.json", PAPER_TRADES_FILE, None),
    ("bot/logs/bot.log", LOG_FILE, None),
)

REGIME_AXES = ("time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase")

# Health-pulse thresholds (Session 17 set position-check loop ≤32s).
CADENCE_FLAG_MS = 32_000
CURSOR_ROWS_WARN = 500
CURSOR_ROWS_CRIT = 200
LOCK_STALE_SECONDS = 60
HEARTBEAT_STALE_SECONDS = 120
STATE_FILE_GROWTH_FLAG_MB = 100.0

LOG_TS_RE = re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


# ───────────────────────────────────────────────────────────────── time helpers

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _windows(now_utc: datetime) -> tuple[datetime, datetime, datetime]:
    """Return (this_week_start, last_week_start, last_week_end) for week-over-week."""
    this_start = now_utc - timedelta(days=7)
    last_start = now_utc - timedelta(days=14)
    last_end = this_start
    return this_start, last_start, last_end


def _delta_str(this_v: float, last_v: float) -> str:
    d = this_v - last_v
    if d > 0:
        return f"+{d:,.0f}"
    return f"{d:,.0f}"


def parse_window(report_date: datetime, days: int) -> tuple[datetime, datetime]:
    """Convert a report-FOR date (ET-aware) into a UTC [start, end) range.

    The report-FOR date covers ``[ET midnight, ET midnight + days)``. Output
    is UTC-aware so JSONL timestamp filters compare cleanly.
    """
    if report_date.tzinfo is None:
        report_date = report_date.replace(tzinfo=ET)
    start_local = report_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=days)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def yesterday_in_et(now_utc: datetime | None = None) -> datetime:
    """Return ``yesterday`` as an ET-midnight datetime."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(ET).date()
    yesterday = today_et - timedelta(days=1)
    return datetime.combine(yesterday, datetime.min.time(), tzinfo=ET)


def last_sunday_in_et(now_utc: datetime | None = None) -> datetime:
    """Return the most recent Sunday (today if Sunday) as an ET-midnight datetime."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(ET).date()
    # Python: Monday=0..Sunday=6. We want the most recent Sunday including today.
    days_since_sunday = (today_et.weekday() + 1) % 7
    sunday = today_et - timedelta(days=days_since_sunday)
    return datetime.combine(sunday, datetime.min.time(), tzinfo=ET)


# ───────────────────────────────────────────────────────────── error wrappers

def _safe_section(fn, *args, **kwargs) -> tuple[str, str | None]:
    """Run fn; return (markdown, None) on success or (fallback, reason) on failure."""
    try:
        body = fn(*args, **kwargs)
        if not isinstance(body, str):
            return (
                f"_[section unavailable: non-string return ({type(body).__name__})]_",
                "non-string return",
            )
        return body, None
    except Exception as exc:
        msg = str(exc).splitlines()[0] if str(exc) else ""
        reason = f"{type(exc).__name__}: {msg}".strip(": ")[:200]
        return f"_[section unavailable: {reason}]_", reason


def _demote_h1(body: str, h1_replacement: str, header_note: str | None = None) -> str:
    """Replace the first '# ' line of a sub-tool report with our H1 + optional note."""
    lines = body.splitlines()
    new_lines = [f"# {h1_replacement}"]
    if header_note:
        new_lines.append("")
        new_lines.append(header_note)
    found_h1 = False
    for line in lines:
        if not found_h1 and line.startswith("# ") and not line.startswith("## "):
            found_h1 = True
            continue
        new_lines.append(line)
    return "\n".join(new_lines).rstrip()


# ───────────────────────────────────────────────── JSONL streaming utilities

def iter_jsonl_tolerant(
    path: Path,
    since_utc: datetime | None = None,
    until_utc: datetime | None = None,
    ts_keys: tuple[str, ...] = ("ts", "timestamp", "recorded_at"),
) -> Iterator[dict]:
    """Stream a JSONL file, skipping malformed lines with a single stderr warning.

    Optional UTC ts filter: yields only records whose first-found ts_key
    parses to a datetime in [since_utc, until_utc).
    """
    if not path.exists():
        return
    skipped = 0
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
            if not isinstance(rec, dict):
                continue
            if since_utc is not None or until_utc is not None:
                ts_val = None
                for k in ts_keys:
                    if k in rec:
                        ts_val = _parse_iso(rec.get(k))
                        if ts_val is not None:
                            break
                if ts_val is None:
                    continue
                if since_utc is not None and ts_val < since_utc:
                    continue
                if until_utc is not None and ts_val >= until_utc:
                    continue
            yield rec
    if skipped:
        print(
            f"_report_helpers: skipped {skipped} malformed lines in {path.name}",
            file=sys.stderr,
        )


# ─────────────────────────────────────────────────────────── log traceback tail

def _rotated_log_paths(log_path: Path) -> list[Path]:
    """Return [log, log.1, log.2, ...] — the active log plus each backup."""
    paths: list[Path] = []
    if log_path.exists():
        paths.append(log_path)
    for n in range(1, 11):
        p = log_path.parent / f"{log_path.name}.{n}"
        if p.exists():
            paths.append(p)
    return paths


def tail_tracebacks(
    log_path: Path, since_utc: datetime, until_utc: datetime, *, max_signatures: int = 50
) -> list[tuple[str, int, str]]:
    """Scan log + rotated backups for Traceback blocks within [since, until).

    bot.log timestamps are ET (per scheduler convention); we convert the UTC
    window to ET for comparison. Traceback signature = the last non-empty line
    of the block (typically the exception class + message). Returns deduped
    [(signature, count, first_seen_iso), ...] sorted by count desc.
    """
    threshold_local = since_utc.astimezone(ET).replace(tzinfo=None) if ZoneInfo else since_utc.replace(tzinfo=None)
    until_local = until_utc.astimezone(ET).replace(tzinfo=None) if ZoneInfo else until_utc.replace(tzinfo=None)

    counter: Counter[str] = Counter()
    first_seen: dict[str, str] = {}

    for path in _rotated_log_paths(log_path):
        try:
            with path.open(errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue

        i = 0
        while i < len(lines):
            line = lines[i]
            if "Traceback (most recent call last)" not in line:
                i += 1
                continue
            ts_local = _extract_log_ts(line)
            block_lines: list[str] = []
            j = i + 1
            while j < len(lines) and j - i < 200:
                nxt = lines[j]
                # Heuristic: stop when we see another timestamped log line that
                # isn't an indented traceback continuation.
                if _extract_log_ts(nxt) is not None and not nxt.startswith((" ", "\t")):
                    break
                block_lines.append(nxt.rstrip())
                j += 1
            i = j

            if ts_local is None:
                continue
            if ts_local < threshold_local or ts_local >= until_local:
                continue

            sig = ""
            for bl in reversed(block_lines):
                if bl.strip():
                    sig = bl.strip()
                    break
            if not sig:
                sig = line.strip()
            sig = sig[:160]
            counter[sig] += 1
            first_seen.setdefault(sig, ts_local.isoformat())

    items = [(sig, counter[sig], first_seen[sig]) for sig in counter]
    items.sort(key=lambda x: -x[1])
    return items[:max_signatures]


def _extract_log_ts(line: str) -> datetime | None:
    m = LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ──────────────────────────────────────────────────────── process / heartbeat

def process_alive(now_utc: datetime | None = None) -> tuple[bool, str]:
    """Return (alive, reason) using the lockfile + PID + heartbeat."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if not LOCK_FILE.exists():
        return False, "bot.lock missing"
    try:
        pid = int(LOCK_FILE.read_text().strip())
    except (ValueError, OSError) as exc:
        return False, f"unreadable lock ({type(exc).__name__})"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, f"PID {pid} not running"
    except PermissionError:
        # Process exists but we don't own it — still counts as alive.
        pass
    except OSError as exc:
        return False, f"kill(0) errored ({type(exc).__name__})"

    try:
        lock_mtime = datetime.fromtimestamp(LOCK_FILE.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False, "lock stat failed"
    if (now_utc - lock_mtime).total_seconds() > LOCK_STALE_SECONDS:
        return False, f"lock mtime {(now_utc - lock_mtime).total_seconds():.0f}s stale"

    if BOT_STATE_FILE.exists():
        try:
            st = json.loads(BOT_STATE_FILE.read_text())
            hb = _parse_iso(st.get("last_heartbeat"))
            if hb is not None and (now_utc - hb).total_seconds() > HEARTBEAT_STALE_SECONDS:
                return False, f"heartbeat {(now_utc - hb).total_seconds():.0f}s stale"
        except (OSError, json.JSONDecodeError):
            pass
    return True, f"PID {pid} alive"


# ───────────────────────────────────────────────────── health-pulse computation

def compute_health_pulse(now_utc: datetime) -> list[dict]:
    """Return 6 rows for the daily/weekly health-pulse table.

    Always 6 rows. ``status`` is one of '✅' / '⚠️' / '🚨'. Designed so the eye
    learns the shape — same row order, same width, every report.
    """
    rows: list[dict] = []

    alive, alive_reason = process_alive(now_utc)
    rows.append({
        "axis": "Bot alive",
        "value": alive_reason,
        "status": "✅" if alive else "🚨",
    })

    cursor_med, scans, partial_pct = _scanner_health_24h(now_utc)
    if scans == 0:
        rows.append({
            "axis": "Scanner health",
            "value": "no universe rows in window",
            "status": "🚨",
        })
    else:
        if cursor_med < CURSOR_ROWS_CRIT:
            status = "🚨"
        elif cursor_med < CURSOR_ROWS_WARN:
            status = "⚠️"
        else:
            status = "✅"
        rows.append({
            "axis": "Scanner health",
            "value": f"cursor_rows median={cursor_med:.0f} over {scans} scans, partial={partial_pct:.0f}%",
            "status": status,
        })

    n_dec, accept_rate = _decisions_volume_24h(now_utc)
    if n_dec == 0:
        status = "🚨"
    else:
        status = "✅"
    rows.append({
        "axis": "Decisions volume",
        "value": f"{n_dec:,} decisions, {accept_rate:.1f}% accept",
        "status": status,
    })

    trades_by_strat = _trades_fired_24h(now_utc)
    total = sum(trades_by_strat.values())
    breakdown = ", ".join(f"{k}={v}" for k, v in trades_by_strat.most_common()) or "—"
    rows.append({
        "axis": "Trades fired",
        "value": f"{total} ({breakdown})",
        "status": "✅" if total > 0 else "⚠️",
    })

    rows.append(_telegram_delivery_health(now_utc))

    err_count = _error_count_24h(now_utc)
    rows.append({
        "axis": "Errors",
        "value": f"{err_count} traceback signatures",
        "status": "✅" if err_count == 0 else "⚠️",
    })

    return rows


def format_health_pulse(rows: list[dict]) -> str:
    """Render the health pulse as a markdown table."""
    out = ["| Axis | Value | Status |", "|---|---|:---:|"]
    for r in rows:
        out.append(f"| {r['axis']} | {r['value']} | {r['status']} |")
    return "\n".join(out)


def _format_age_hours(now_utc: datetime, ts: datetime | None) -> str:
    if ts is None:
        return "never"
    hours = max(0.0, (now_utc - ts).total_seconds() / 3600.0)
    return f"{hours:.1f}h ago"


def _telegram_delivery_health(now_utc: datetime) -> dict:
    state = {}
    if BOT_STATE_FILE.exists():
        try:
            loaded = json.loads(BOT_STATE_FILE.read_text())
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, json.JSONDecodeError):
            state = {}

    last_success = _parse_iso(state.get("telegram_last_send_success_at"))
    throttled_until = _parse_iso(state.get("telegram_throttled_until"))
    try:
        throttle_count = int(state.get("telegram_throttled_count_24h") or 0)
    except (TypeError, ValueError):
        throttle_count = 0

    if throttled_until is not None and throttled_until > now_utc:
        throttle_text = f"throttled until {throttled_until.isoformat()}"
        status = "🚨"
    else:
        throttle_text = "not throttled"
        if last_success is None:
            status = "⚠️"
        else:
            hours_since_success = (now_utc - last_success).total_seconds() / 3600.0
            status = "✅" if hours_since_success <= 24 else "⚠️"

    return {
        "axis": "Telegram delivery",
        "value": (
            f"last success {_format_age_hours(now_utc, last_success)}; "
            f"{throttle_text}; {throttle_count} throttles/24h"
        ),
        "status": status,
    }


def _scanner_health_24h(now_utc: datetime) -> tuple[float, int, float]:
    since = now_utc - timedelta(hours=24)
    by_scan: dict[str, dict] = {}
    for rec in iter_jsonl_tolerant(UNIVERSE_FILE, since_utc=since, until_utc=now_utc):
        scan_id = rec.get("scan_id")
        if not scan_id:
            continue
        slot = by_scan.setdefault(scan_id, {"rows": 0, "partial": False})
        slot["rows"] += 1
        if rec.get("partial"):
            slot["partial"] = True
    if not by_scan:
        return 0.0, 0, 0.0
    rows_per_scan = sorted(s["rows"] for s in by_scan.values())
    median = statistics.median(rows_per_scan)
    partial_pct = 100.0 * sum(1 for s in by_scan.values() if s["partial"]) / len(by_scan)
    return median, len(by_scan), partial_pct


def _decisions_volume_24h(now_utc: datetime) -> tuple[int, float]:
    since = now_utc - timedelta(hours=24)
    n = 0
    n_accept = 0
    for rec in iter_jsonl_tolerant(DECISIONS_FILE, since_utc=since, until_utc=now_utc):
        n += 1
        if rec.get("decision") == "accept":
            n_accept += 1
    rate = (100.0 * n_accept / n) if n else 0.0
    return n, rate


def _trades_fired_24h(now_utc: datetime) -> Counter[str]:
    since = now_utc - timedelta(hours=24)
    counts: Counter[str] = Counter()
    if not PAPER_TRADES_FILE.exists():
        return counts
    try:
        trades = json.loads(PAPER_TRADES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return counts
    if not isinstance(trades, list):
        return counts
    for t in trades:
        if not isinstance(t, dict):
            continue
        ts = _parse_iso(t.get("timestamp"))
        if ts is None or ts < since or ts >= now_utc:
            continue
        counts[t.get("type") or "unknown"] += 1
    return counts


def _error_count_24h(now_utc: datetime) -> int:
    since = now_utc - timedelta(hours=24)
    return len(tail_tracebacks(LOG_FILE, since, now_utc))


# ───────────────────────────────────────────────────────── state file growth

def _gzip_uncompressed_size(path: Path) -> int | None:
    """Read the gzip ISIZE footer (last 4 bytes) for a fair size comparison.

    Returns the uncompressed byte count for the LAST gzip member in the file
    (modulo 2^32 — accurate for files <4GB, which all our archives are).
    Returns None on any read error.
    """
    try:
        with path.open("rb") as f:
            f.seek(-4, 2)
            footer = f.read(4)
    except OSError:
        return None
    if len(footer) != 4:
        return None
    return int.from_bytes(footer, "little")


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n = n / 1024.0
    return f"{n:.1f}TB"


def state_file_growth(window_end_utc: datetime, *, baseline_days: int = 1) -> list[dict]:
    """Compute current size vs N days ago for each tracked state file.

    For files with a daily archive (decisions, universe, live_ticks,
    tracker_cadence), baseline = the gzipped archive from N days back. For
    others (clv.json, paper_trades.json, live_journal.json, bot.log), no
    daily archive exists → baseline rendered as ``(no baseline)``.
    """
    rows: list[dict] = []
    baseline_date = (window_end_utc - timedelta(days=baseline_days)).astimezone(ET).date()
    for name, path, archive_prefix in TRACKED_STATE_FILES:
        try:
            current = path.stat().st_size if path.exists() else 0
        except OSError:
            current = 0

        baseline = None
        if archive_prefix is not None and ARCHIVE_DIR.exists():
            archive_path = ARCHIVE_DIR / f"{archive_prefix}-{baseline_date.isoformat()}.jsonl.gz"
            if archive_path.exists():
                baseline = _gzip_uncompressed_size(archive_path)

        if baseline is None:
            delta_str = "(no baseline)"
            flag = ""
        else:
            delta = current - baseline
            pct = (100.0 * delta / baseline) if baseline else 0.0
            sign = "+" if delta >= 0 else "-"
            delta_str = f"{sign}{_human_size(abs(delta))} ({sign}{abs(pct):.0f}%)"
            flag = " ⚠️" if delta > STATE_FILE_GROWTH_FLAG_MB * 1024 * 1024 else ""

        rows.append({
            "name": name,
            "current": current,
            "current_human": _human_size(current),
            "baseline": baseline,
            "delta_str": delta_str,
            "flag": flag,
        })
    return rows


# ─────────────────────────────────────────────────────── prior weekly report

def read_prior_weekly_report(week_end_dt: datetime) -> str | None:
    """Return the markdown of the weekly report for the week BEFORE ``week_end_dt``.

    Looks for ``weekly_report_YYYY-WNN.md`` in REPORTS_DIR/weekly. If none
    exists, return None — caller renders ``_No baseline yet._``.
    """
    prior = week_end_dt - timedelta(days=7)
    iso_year, iso_week, _ = prior.isocalendar()
    candidate = REPORTS_DIR / "weekly" / f"weekly_report_{iso_year}-W{iso_week:02d}.md"
    if candidate.exists():
        try:
            return candidate.read_text()
        except OSError:
            return None
    return None


def extract_headline_metric(text: str, label_pattern: str) -> str | None:
    """Grep ``text`` for the first line matching ``label_pattern``, return the line."""
    rx = re.compile(label_pattern)
    for line in text.splitlines():
        if rx.search(line):
            return line.strip()
    return None


# ────────────────────────────────────────────────────────────── shared sections
# Each renderer takes a window range plus optional regime_by, returns markdown
# starting with a # section header. Orchestrators wrap in _safe_section.

def render_health_pulse(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§1. Always 6 rows, fixed shape."""
    rows = compute_health_pulse(now_utc)
    out = ["# 1. Health pulse", "",
           "Six always-on indicators. Same shape every report — train your eye to scan it.",
           "", format_health_pulse(rows)]
    return "\n".join(out)


def render_scanner_activity(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§2. universe.jsonl distribution over the window."""
    by_scan: dict[str, dict] = {}
    for rec in iter_jsonl_tolerant(UNIVERSE_FILE, since_utc=window_start, until_utc=window_end):
        sid = rec.get("scan_id")
        if not sid:
            continue
        slot = by_scan.setdefault(sid, {"rows": 0, "partial": False, "ts": rec.get("ts")})
        slot["rows"] += 1
        if rec.get("partial"):
            slot["partial"] = True

    out = ["# 2. Scanner activity", ""]
    if not by_scan:
        out.append("_No universe.jsonl rows in window._")
        return "\n".join(out)

    rows_per_scan = sorted(s["rows"] for s in by_scan.values())
    n = len(rows_per_scan)
    p25 = rows_per_scan[max(0, n // 4)]
    p75 = rows_per_scan[min(n - 1, (3 * n) // 4)]
    median = statistics.median(rows_per_scan)
    partial_pct = 100.0 * sum(1 for s in by_scan.values() if s["partial"]) / n
    duration_h = max((window_end - window_start).total_seconds() / 3600.0, 0.001)
    rate = n / duration_h

    out.append(
        f"Scans observed: **{n:,}** ({rate:.2f}/hr over {duration_h:.1f}h). "
        f"`cursor_rows` distribution: min={rows_per_scan[0]}, p25={p25}, median={median:.0f}, "
        f"p75={p75}, max={rows_per_scan[-1]}. Partial rate: **{partial_pct:.1f}%**."
    )
    return "\n".join(out)


def render_decision_audit(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§3. Cohort report scoped to window via the existing render function."""
    from tools import cohort_report  # noqa: PLC0415
    decisions = cohort_report.load_decisions(window_start)
    cfs = cohort_report.load_cf_records(window_start)
    # Filter to the window's upper bound too (load_decisions reads everything since window_start).
    decisions = [d for d in decisions if (_parse_iso(d.get("ts")) or window_start) < window_end]
    cfs = [c for c in cfs if (_parse_iso(c.get("recorded_at")) or window_start) < window_end]
    days = max(1, int((window_end - window_start).total_seconds() / 86400 + 0.5))
    dec_bins = cohort_report.aggregate_decisions(decisions, regime_by=regime_by)
    cf_bins = cohort_report.aggregate_cf(cfs, regime_by=regime_by)
    body = cohort_report.render_markdown(
        decisions, cfs, days, dec_bins, cf_bins, window_start, window_end, regime_by,
    )
    return _demote_h1(
        body,
        "3. Decision audit (cohort findings)",
        f"Window: {window_start.date().isoformat()} → {window_end.date().isoformat()} "
        f"({days}d). Mis-tuning candidates flagged at ≥50% reject rate AND positive mean CLV on rejects.",
    )


def render_trade_activity(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§4. Per-strategy P&L from paper_trades.json (resolved_at within window)."""
    from bot.regime import _ticker_to_sport  # noqa: PLC0415
    if not PAPER_TRADES_FILE.exists():
        return "# 4. Trade activity\n\n_No paper_trades.json found._"
    trades = json.loads(PAPER_TRADES_FILE.read_text())

    by_strat: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0, "size": 0.0, "open": 0})
    by_sport_lm: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    n = 0
    n_open = 0

    for t in trades:
        if not isinstance(t, dict):
            continue
        ts_open = _parse_iso(t.get("timestamp"))
        ts_resolved = _parse_iso(t.get("resolved_at"))
        if ts_open and window_start <= ts_open < window_end:
            n_open += 1
        if ts_resolved is None or ts_resolved < window_start or ts_resolved >= window_end:
            continue
        pnl_dollars = t.get("pnl")
        if pnl_dollars is None:
            continue
        cents = float(pnl_dollars) * 100.0
        strat = t.get("type") or "unknown"
        ticker = t.get("ticker") or ""
        contracts = t.get("contracts") or 0
        entry_cents = (t.get("entry_price") or 0) * 100.0
        slot = by_strat[strat]
        slot["n"] += 1
        slot["pnl"] += cents
        if cents > 0:
            slot["wins"] += 1
        slot["size"] += contracts * entry_cents
        n += 1
        if strat == "live_momentum":
            sport = _ticker_to_sport(ticker) or "unknown"
            by_sport_lm[sport]["n"] += 1
            by_sport_lm[sport]["pnl"] += cents

    out = ["# 4. Trade activity", "",
           f"Resolved trades: **{n}** (closed) · **{n_open}** opened in window. "
           f"P&L from `paper_trades.json` filtered by `resolved_at`."]
    if regime_by:
        out.append(f"_Note: regime-by axis `{regime_by}` not applicable to this section._")
    out.append("")
    if not by_strat:
        out.append("_No trades resolved in window._")
        return "\n".join(out)

    out.append("| Strategy | N | Σ P&L (¢) | Win rate | Avg trade size (¢) |")
    out.append("|---|---:|---:|---:|---:|")
    for strat in sorted(by_strat):
        s = by_strat[strat]
        wr = (100.0 * s["wins"] / s["n"]) if s["n"] else 0.0
        avg_size = (s["size"] / s["n"]) if s["n"] else 0.0
        out.append(f"| `{strat}` | {s['n']} | {s['pnl']:,.0f} | {wr:.0f}% | {avg_size:,.0f} |")
    out.append("")

    if by_sport_lm:
        out.append("**live_momentum per-sport:**")
        out.append("")
        out.append("| Sport | N | Σ P&L (¢) |")
        out.append("|---|---:|---:|")
        for sport in sorted(by_sport_lm):
            s = by_sport_lm[sport]
            out.append(f"| {sport} | {s['n']} | {s['pnl']:,.0f} |")
    return "\n".join(out).rstrip()


def render_cf_coverage(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§5. CF emission + settlement rate over the window."""
    from bot.regime import _ticker_to_sport  # noqa: PLC0415
    if not CLV_FILE.exists():
        return "# 5. CF coverage\n\n_No clv.json found._"
    recs = json.loads(CLV_FILE.read_text())

    emitted: Counter[str] = Counter()
    settled_in_window: Counter[str] = Counter()
    by_strat_sport: Counter[tuple[str, str]] = Counter()
    prior_emitted_settled = 0
    prior_emitted_open = 0

    for r in recs:
        if not isinstance(r, dict):
            continue
        status = r.get("status") or ""
        if not status.startswith("counterfactual"):
            continue
        rec_at = _parse_iso(r.get("recorded_at"))
        opp = r.get("opp_type") or "unknown"
        sport = _ticker_to_sport(r.get("ticker") or "") or "n/a"

        if rec_at is not None and window_start <= rec_at < window_end:
            emitted[opp] += 1
            by_strat_sport[(opp, sport)] += 1
            if status == "counterfactual_settled":
                settled_in_window[opp] += 1
        elif rec_at is not None and rec_at < window_start:
            if status == "counterfactual_settled":
                prior_emitted_settled += 1
            elif status == "counterfactual_open":
                prior_emitted_open += 1

    out = ["# 5. CF coverage", ""]
    if not emitted:
        out.append("_No counterfactuals emitted in window._")
        return "\n".join(out)
    total = sum(emitted.values())
    out.append(f"CFs emitted in window: **{total:,}** "
               f"({', '.join(f'{k}={v}' for k, v in emitted.most_common())}).")
    if prior_emitted_settled or prior_emitted_open:
        prior_total = prior_emitted_settled + prior_emitted_open
        rate = (100.0 * prior_emitted_settled / prior_total) if prior_total else 0.0
        out.append(f"Settlement rate of CFs from prior windows: "
                   f"{prior_emitted_settled:,}/{prior_total:,} = **{rate:.1f}%**.")
    out.append("")
    out.append("| opp_type | sport | CFs emitted |")
    out.append("|---|---|---:|")
    for (opp, sport), n in sorted(by_strat_sport.items(), key=lambda x: -x[1]):
        out.append(f"| `{opp}` | {sport} | {n:,} |")
    return "\n".join(out).rstrip()


def render_live_momentum_events(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§6. journal_analysis pre-filtered to window where the field exists."""
    from tools import journal_analysis  # noqa: PLC0415
    records = journal_analysis.load_journal()
    filtered: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        ts = _parse_iso(r.get("timestamp"))
        if ts is None or ts < window_start or ts >= window_end:
            continue
        filtered.append(r)
    if not filtered:
        return ("# 6. Live momentum events\n\n"
                "_No live_journal events in window._")
    body = journal_analysis.render_markdown(filtered)
    return _demote_h1(
        body,
        "6. Live momentum events",
        f"Window: {window_start.date().isoformat()} → {window_end.date().isoformat()}. "
        f"{len(filtered):,} journal events in scope.",
    )


def render_dqs_regime_distribution(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§7. live_ticks.jsonl grouped by DQS bucket × sport × regime field(s)."""
    by_dqs: Counter[str] = Counter()
    by_sport: Counter[str] = Counter()
    by_dqs_sport: Counter[tuple[str, str]] = Counter()
    by_phase: Counter[str] = Counter()
    n = 0

    for rec in iter_jsonl_tolerant(LIVE_TICKS_FILE, since_utc=window_start, until_utc=window_end):
        n += 1
        dqs = rec.get("dqs")
        if dqs is None:
            dqs_bucket = "no_dqs"
        else:
            try:
                d = float(dqs)
            except (TypeError, ValueError):
                dqs_bucket = "no_dqs"
            else:
                if d < 0.3:
                    dqs_bucket = "[0.0,0.3)"
                elif d < 0.5:
                    dqs_bucket = "[0.3,0.5)"
                elif d < 0.7:
                    dqs_bucket = "[0.5,0.7)"
                else:
                    dqs_bucket = "[0.7,1.0]"
        sport = rec.get("sport") or "unknown"
        by_dqs[dqs_bucket] += 1
        by_sport[sport] += 1
        by_dqs_sport[(dqs_bucket, sport)] += 1
        regime = rec.get("regime") if isinstance(rec.get("regime"), dict) else {}
        phase = (regime.get("sport_phase") if regime else None) or rec.get("period") or "_none"
        by_phase[str(phase)] += 1

    out = ["# 7. DQS + regime distribution", ""]
    if n == 0:
        out.append("_No live_ticks rows in window._")
        return "\n".join(out)
    out.append(f"Total ticks in window: **{n:,}**. Distribution counts only — designed to scan, not regress on.")
    out.append("")
    out.append("**By DQS bucket:**")
    out.append("")
    out.append("| DQS | N | % |")
    out.append("|---|---:|---:|")
    for k in ("[0.0,0.3)", "[0.3,0.5)", "[0.5,0.7)", "[0.7,1.0]", "no_dqs"):
        v = by_dqs.get(k, 0)
        pct = 100.0 * v / n if n else 0.0
        out.append(f"| {k} | {v:,} | {pct:.1f}% |")
    out.append("")
    out.append("**By sport:**")
    out.append("")
    out.append("| Sport | N |")
    out.append("|---|---:|")
    for sport, v in by_sport.most_common():
        out.append(f"| {sport} | {v:,} |")
    out.append("")
    out.append("**By sport_phase / period:**")
    out.append("")
    out.append("| Phase | N |")
    out.append("|---|---:|")
    for phase, v in by_phase.most_common():
        out.append(f"| {phase} | {v:,} |")
    return "\n".join(out)


def render_cadence_health(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§8. tracker_cadence.jsonl ms_since_last_call distribution per called_from."""
    by_caller: dict[str, list[int]] = defaultdict(list)
    for rec in iter_jsonl_tolerant(CADENCE_FILE, since_utc=window_start, until_utc=window_end):
        caller = rec.get("called_from") or "unspecified"
        ms = rec.get("ms_since_last_call")
        if isinstance(ms, (int, float)) and ms >= 0:
            by_caller[caller].append(int(ms))

    out = ["# 8. Cadence health", ""]
    if not by_caller:
        out.append("_No tracker_cadence rows in window._")
        return "\n".join(out)
    out.append(f"Note: actual `called_from` values are `_main_loop` and `_position_check_loop` "
               f"(spec referenced `_scan_loop`; renaming would be a separate scope).")
    out.append("")
    out.append("| called_from | N calls | median (ms) | p95 (ms) | max (ms) | flag |")
    out.append("|---|---:|---:|---:|---:|:---|")
    for caller in sorted(by_caller):
        vals = sorted(by_caller[caller])
        n = len(vals)
        median = statistics.median(vals)
        p95 = vals[min(n - 1, int(0.95 * n))]
        mx = vals[-1]
        flag = "⚠️ p95 over 32s" if p95 > CADENCE_FLAG_MS else ""
        out.append(f"| `{caller}` | {n:,} | {median:,.0f} | {p95:,} | {mx:,} | {flag} |")
    return "\n".join(out)


def render_errors(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§9. Tracebacks in bot.log within window."""
    sigs = tail_tracebacks(LOG_FILE, window_start, window_end)
    out = ["# 9. Errors", ""]
    if not sigs:
        out.append("_No errors logged._")
        return "\n".join(out)
    out.append(f"Distinct traceback signatures: **{len(sigs)}**.")
    out.append("")
    out.append("| Count | First seen (ET) | Signature |")
    out.append("|---:|---|---|")
    for sig, count, first_seen in sigs:
        # Truncate signature to keep table readable.
        s = sig[:100].replace("|", "\\|")
        out.append(f"| {count} | {first_seen} | `{s}` |")
    return "\n".join(out)


def render_state_file_growth(now_utc: datetime, window_start: datetime, window_end: datetime, regime_by: str | None) -> str:
    """§10. Sizes of the 8 tracked state files vs N days ago."""
    rows = state_file_growth(window_end, baseline_days=1)
    out = ["# 10. State file growth", "",
           "Current size vs ~1 day ago (via the daily archive when available)."]
    out.append("")
    out.append("| File | Current | Δ vs 24h ago |")
    out.append("|---|---:|---|")
    for r in rows:
        out.append(f"| `{r['name']}` | {r['current_human']} | {r['delta_str']}{r['flag']} |")
    return "\n".join(out)


# Ordered list used by the orchestrators. Names — not function references — so
# monkeypatching helpers.<renderer_name> at test time takes effect.
SHARED_SECTIONS: tuple[tuple[str, str], ...] = (
    ("1. Health pulse", "render_health_pulse"),
    ("2. Scanner activity", "render_scanner_activity"),
    ("3. Decision audit", "render_decision_audit"),
    ("4. Trade activity", "render_trade_activity"),
    ("5. CF coverage", "render_cf_coverage"),
    ("6. Live momentum events", "render_live_momentum_events"),
    ("7. DQS + regime distribution", "render_dqs_regime_distribution"),
    ("8. Cadence health", "render_cadence_health"),
    ("9. Errors", "render_errors"),
    ("10. State file growth", "render_state_file_growth"),
)


# ────────────────────────────────────────────────────── orchestrator helpers

def write_header(out_path: Path, title: str, generated_at: datetime, *, extra_lines: tuple[str, ...] = ()) -> None:
    """Open the report file (truncate) and write the header. First I/O of every run."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        f.write(f"# {title}\n\n")
        f.write(f"Generated: {generated_at.isoformat()}\n\n")
        for line in extra_lines:
            f.write(line + "\n")
        if extra_lines:
            f.write("\n")
        f.flush()


def append_section(out_path: Path, body: str) -> None:
    with out_path.open("a") as f:
        f.write(body.rstrip() + "\n\n---\n\n")
        f.flush()


def append_footer(out_path: Path, generated_at: datetime, *, skipped: list[str] | None = None,
                  total_sections: int | None = None) -> None:
    with out_path.open("a") as f:
        if total_sections is not None and skipped is not None:
            rendered = total_sections - len(skipped)
            line = f"_Sections rendered: {rendered}/{total_sections}_"
            if skipped:
                line += f" — skipped: {'; '.join(skipped)}"
            f.write(line + "\n\n")
        f.write(f"Last Run Stamp: {generated_at.isoformat()}\n")
        f.flush()


def render_shared_sections(
    out_path: Path,
    now_utc: datetime,
    window_start: datetime,
    window_end: datetime,
    regime_by: str | None,
) -> list[str]:
    """Render the 10 shared sections to ``out_path``. Returns list of skipped section titles."""
    skipped: list[str] = []
    module = sys.modules[__name__]
    for title, fn_name in SHARED_SECTIONS:
        fn = getattr(module, fn_name)
        body, reason = _safe_section(fn, now_utc, window_start, window_end, regime_by)
        if reason is None:
            append_section(out_path, body)
        else:
            append_section(out_path, f"# {title}\n\n{body}")
            skipped.append(f"{title} ({reason})")
    return skipped


def capture_main_stdout(main_fn, argv: list[str] | None = None) -> str:
    """Call a tool's ``main(argv)`` while redirecting sys.stdout, return captured text.

    Used for tools like exit_replay whose render functions need a multi-step
    pipeline to reach. Wrapped in try/except by callers via _safe_section.
    """
    saved = sys.stdout
    buf = StringIO()
    sys.stdout = buf
    try:
        main_fn(argv or [])
    finally:
        sys.stdout = saved
    return buf.getvalue()
