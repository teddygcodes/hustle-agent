#!/usr/bin/env python3
"""Planner-tuned status consolidator for Glint.

Read-only over the bot's existing reports and state files. The only writes are
the consolidator-owned snapshot artifacts:

* bot/state/glint_status_last.json
* bot/state/glint_status_YYYY-MM-DD.md
"""
from __future__ import annotations

import ast
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot import state_io  # noqa: E402
from tools import _report_helpers as helpers  # noqa: E402


LOG_TS_RE = re.compile(r"^\[?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
DAILY_RE = re.compile(r"daily_report_(\d{4}-\d{2}-\d{2})\.md$")
DISCOVERY_RE = re.compile(r"discovery_report_(\d{4}-\d{2}-\d{2})\.md$")
DISCOVERY_FINDINGS_RE = re.compile(r"discovery_findings_(\d{4}-\d{2}-\d{2})\.jsonl$")
WATCH_KEYWORDS_RE = re.compile(
    r"Watch-list trigger|Re-investigate when|re-evaluate when|REVERT if",
    re.IGNORECASE,
)
SESSION_HEADING_RE = re.compile(r"^#{2,4}\s+[☑☐]?\s*(Session [^—\n]+)")


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    state_dir: Path
    reports_dir: Path
    daily_dir: Path
    discovery_dir: Path
    positions_file: Path
    paper_trades_file: Path
    bot_state_file: Path
    clv_file: Path
    decisions_file: Path
    log_file: Path
    claude_md: Path
    report_calendar: Path
    config_py: Path
    research_dataset: Path
    last_status: Path


@dataclass
class Flag:
    id: str
    severity: str
    message: str
    ref: str | None = None


@dataclass
class DailyReport:
    path: Path | None
    text: str
    generated_at: datetime | None
    report_date: str | None
    stale_note: str


def paths_for(repo_root: Path) -> Paths:
    state = repo_root / "bot" / "state"
    return Paths(
        repo_root=repo_root,
        state_dir=state,
        reports_dir=state / "reports",
        daily_dir=state / "reports" / "daily",
        discovery_dir=state / "discovery",
        positions_file=state / "positions.json",
        paper_trades_file=state / "paper_trades.json",
        bot_state_file=state / "bot_state.json",
        clv_file=state / "clv.json",
        decisions_file=state / "decisions.jsonl",
        log_file=repo_root / "bot" / "logs" / "bot.log",
        claude_md=repo_root / "CLAUDE.md",
        report_calendar=repo_root / "REPORT_CALENDAR.md",
        config_py=repo_root / "bot" / "config.py",
        research_dataset=state / "research" / "live_momentum_dataset.csv",
        last_status=state / "glint_status_last.json",
    )


def _parse_iso(ts: object) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    return helpers._parse_iso(ts)


def _load_json(path: Path, default):
    try:
        if not path.exists():
            return default
        data = json.loads(path.read_text())
        return data
    except (OSError, json.JSONDecodeError):
        return default


def _load_jsonl(path: Path) -> list[dict]:
    return list(helpers.iter_jsonl_tolerant(path))


def _fmt_money(value: float, *, signed: bool = True) -> str:
    if not signed:
        return f"${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"+${value:,.2f}"


def _fmt_int_delta(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def _fmt_money_delta(value: float) -> str:
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"+${value:,.2f}"


def _format_et(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    return dt.astimezone(helpers.ET).strftime("%Y-%m-%d %H:%M ET")


def _age_text(now_utc: datetime, then: datetime | None) -> str:
    if then is None:
        return "unknown age"
    seconds = max(0.0, (now_utc - then).total_seconds())
    if seconds < 120:
        return f"{seconds:.0f}s ago"
    hours = seconds / 3600.0
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24.0:.1f}d ago"


def _trade_key(trade: dict) -> str:
    return str(
        trade.get("id")
        or trade.get("trade_id")
        or f"{trade.get('ticker', '')}:{trade.get('timestamp', '')}"
    )


def _family_from_ticker(ticker: str | None) -> str:
    if not ticker:
        return "unknown"
    return ticker.split("-", 1)[0]


def load_config_surface(paths: Paths) -> dict:
    surface = {
        "paper_starting_balance": 10500.0,
        "vig_stack_default_cap": 200.0,
        "vig_stack_family_caps": {},
    }
    try:
        text = paths.config_py.read_text()
    except OSError:
        return surface

    m = re.search(r"^PAPER_STARTING_BALANCE\s*=\s*([0-9.]+)", text, re.MULTILINE)
    if m:
        surface["paper_starting_balance"] = float(m.group(1))
    m = re.search(r"^VIG_STACK_DEFAULT_MAX_POSITION_DOLLARS\s*=\s*([0-9.]+)", text, re.MULTILINE)
    if m:
        surface["vig_stack_default_cap"] = float(m.group(1))
    m = re.search(
        r"^VIG_STACK_FAMILY_MAX_POSITION_DOLLARS\s*=\s*(\{.*?\n\})",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if m:
        try:
            caps = ast.literal_eval(m.group(1))
            if isinstance(caps, dict):
                surface["vig_stack_family_caps"] = {
                    str(k): float(v) for k, v in caps.items()
                }
        except (ValueError, SyntaxError):
            pass
    return surface


def latest_daily_report(paths: Paths, now_utc: datetime) -> DailyReport:
    candidates: list[tuple[str, Path]] = []
    if paths.daily_dir.exists():
        for path in paths.daily_dir.glob("daily_report_*.md"):
            m = DAILY_RE.search(path.name)
            if m:
                candidates.append((m.group(1), path))
    if not candidates:
        return DailyReport(None, "", None, None, "[missing]")

    report_date, path = sorted(candidates)[-1]
    try:
        text = path.read_text()
    except OSError:
        return DailyReport(path, "", None, report_date, "[unreadable]")

    generated_at = None
    for line in text.splitlines()[:12]:
        if line.startswith("Generated:"):
            generated_at = _parse_iso(line.split(":", 1)[1].strip())
            break
    stale_note = ""
    if generated_at is None:
        stale_note = "[generated timestamp missing]"
    else:
        age_h = max(0.0, (now_utc - generated_at).total_seconds() / 3600.0)
        if age_h > 12:
            stale_note = f"[stale: {age_h:.1f}h ago]"
    return DailyReport(path, text, generated_at, report_date, stale_note)


def extract_markdown_section(markdown: str, heading_prefix: str) -> str:
    """Extract a top-level markdown section by numbered H1/H2 prefix."""
    if not markdown:
        return ""
    lines = markdown.splitlines()
    start = None
    heading_rx = re.compile(rf"^#{{1,3}}\s+{re.escape(heading_prefix)}(?:\b|$)", re.IGNORECASE)
    for idx, line in enumerate(lines):
        if heading_rx.match(line.strip()):
            start = idx
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if re.match(r"^#{1,3}\s+\d+\.\s+", line):
            end = idx
            break
    return "\n".join(lines[start:end]).strip()


def parse_health_statuses(section: str) -> list[str]:
    statuses: list[str] = []
    for line in section.splitlines():
        if not line.startswith("|") or "---" in line or "Status" in line:
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) >= 3:
            statuses.append(parts[2])
    return statuses


def collect_metrics(paths: Paths, now_utc: datetime) -> dict:
    config = load_config_surface(paths)
    paper_trades = _load_json(paths.paper_trades_file, [])
    if not isinstance(paper_trades, list):
        paper_trades = []
    positions = _load_json(paths.positions_file, [])
    if not isinstance(positions, list):
        positions = []
    bot_state = _load_json(paths.bot_state_file, {})
    if not isinstance(bot_state, dict):
        bot_state = {}

    settled = [
        t for t in paper_trades
        if isinstance(t, dict) and t.get("pnl") is not None
    ]
    active_positions = [
        p for p in positions
        if isinstance(p, dict) and p.get("status") in {"filled", "open"}
    ]
    paper_open = [
        t for t in paper_trades
        if isinstance(t, dict) and t.get("status") == "open"
    ]

    pnl_by_type: Counter[str] = Counter()
    settled_by_type: Counter[str] = Counter()
    wins_by_type: Counter[str] = Counter()
    for t in settled:
        typ = str(t.get("type") or "unknown")
        pnl = float(t.get("pnl") or 0.0)
        pnl_by_type[typ] += pnl
        settled_by_type[typ] += 1
        if pnl > 0:
            wins_by_type[typ] += 1

    total_pnl = float(sum(float(t.get("pnl") or 0.0) for t in settled))
    paper_start = float(config["paper_starting_balance"])
    bankroll = paper_start + total_pnl
    position_exposure = float(sum(float(p.get("cost") or 0.0) for p in active_positions))
    paper_exposure = float(
        sum(float(t.get("contracts") or 0.0) * float(t.get("entry_price") or 0.0) for t in paper_open)
    )

    today_start_et = datetime.combine(
        now_utc.astimezone(helpers.ET).date(), time.min, tzinfo=helpers.ET
    ).astimezone(timezone.utc)
    resolved_today = [
        t for t in settled
        if (dt := _parse_iso(t.get("resolved_at"))) is not None and today_start_et <= dt < now_utc
    ]

    return {
        "paper_trades": paper_trades,
        "positions": positions,
        "bot_state": bot_state,
        "settled": settled,
        "active_positions": active_positions,
        "paper_open": paper_open,
        "total_pnl": round(total_pnl, 2),
        "settled_count": len(settled),
        "pnl_by_type": dict(pnl_by_type),
        "settled_by_type": dict(settled_by_type),
        "wins_by_type": dict(wins_by_type),
        "open_positions_count": len(active_positions),
        "paper_open_count": len(paper_open),
        "exposure": round(position_exposure, 2),
        "paper_exposure": round(paper_exposure, 2),
        "bankroll": bankroll,
        "today_start_utc": today_start_et,
        "resolved_today": resolved_today,
        "open_tickers": sorted(str(p.get("ticker") or "") for p in active_positions if p.get("ticker")),
        "paper_open_tickers": sorted(str(t.get("ticker") or "") for t in paper_open if t.get("ticker")),
        "settled_trade_ids": sorted(_trade_key(t) for t in settled),
        "config": config,
    }


def count_discovery_findings(paths: Paths, now_utc: datetime) -> dict:
    today = now_utc.astimezone(helpers.ET).date().isoformat()
    report_path = paths.discovery_dir / f"discovery_report_{today}.md"
    finding_path = paths.discovery_dir / f"discovery_findings_{today}.jsonl"
    source_note = f"today ({today})"
    if not report_path.exists() or not finding_path.exists():
        candidates: list[tuple[str, Path]] = []
        if paths.discovery_dir.exists():
            for p in paths.discovery_dir.glob("discovery_report_*.md"):
                m = DISCOVERY_RE.search(p.name)
                if m:
                    candidates.append((m.group(1), p))
        if candidates:
            date_s, report_path = sorted(candidates)[-1]
            finding_path = paths.discovery_dir / f"discovery_findings_{date_s}.jsonl"
            source_note = f"latest available ({date_s})"
        else:
            return {
                "source_note": "missing",
                "report_path": None,
                "finding_path": None,
                "new": 0,
                "stable": 0,
                "resolved": 0,
                "findings": [],
                "new_fingerprints": set(),
            }

    report_text = ""
    try:
        report_text = report_path.read_text()
    except OSError:
        pass
    counts = {"new": 0, "stable": 0, "resolved": 0}
    m = re.search(r"Findings:\s+(\d+)\s+NEW,\s+(\d+)\s+STABLE,\s+(\d+)\s+RESOLVED", report_text)
    if m:
        counts = {"new": int(m.group(1)), "stable": int(m.group(2)), "resolved": int(m.group(3))}

    new_fingerprints: set[str] = set()
    new_section = extract_freeform_section(report_text, "## NEW findings")
    for fp in re.findall(r"fingerprint `([^`]+)`", new_section):
        new_fingerprints.add(fp)

    # Session 65: derive verdict NEW count from new_fingerprints (same source as
    # §6 body) rather than the report summary regex. When the report's summary
    # line and NEW-findings list disagree internally, this keeps verdict and
    # body counts consistent downstream. The summary regex above remains as the
    # initial seed for stable/resolved counts (which the body doesn't recount).
    counts["new"] = len(new_fingerprints)

    findings = _load_jsonl(finding_path)
    return {
        "source_note": source_note,
        "report_path": report_path,
        "finding_path": finding_path if finding_path.exists() else None,
        **counts,
        "findings": findings,
        "new_fingerprints": new_fingerprints,
    }


def extract_freeform_section(markdown: str, heading_prefix: str) -> str:
    if not markdown:
        return ""
    lines = markdown.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip().startswith(heading_prefix):
            start = idx
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("## ") and not lines[idx].strip().startswith(heading_prefix):
            end = idx
            break
    return "\n".join(lines[start:end]).strip()


def _log_ts(line: str) -> datetime | None:
    m = LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        naive = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=helpers.ET).astimezone(timezone.utc)


def log_lines_since(paths: Paths, since_utc: datetime | None) -> list[str]:
    if not paths.log_file.exists():
        return []
    try:
        lines = paths.log_file.read_text(errors="replace").splitlines()
    except OSError:
        return []
    if since_utc is None:
        return lines
    out: list[str] = []
    include_continuation = False
    for line in lines:
        ts = _log_ts(line)
        if ts is not None:
            include_continuation = ts >= since_utc
        if include_continuation:
            out.append(line)
    return out


def top_live_reject_reasons(paths: Paths, now_utc: datetime, hours: int = 48) -> Counter[str]:
    since = now_utc - timedelta(hours=hours)
    reasons: Counter[str] = Counter()
    for rec in helpers.iter_jsonl_tolerant(paths.decisions_file, since_utc=since, until_utc=now_utc):
        if rec.get("opp_type") != "live_momentum":
            continue
        if rec.get("decision") == "accept":
            continue
        reason = rec.get("reason") or rec.get("skip_reason") or "unknown"
        reasons[str(reason)] += 1
    return reasons


def detect_anomalies(paths: Paths, metrics: dict, now_utc: datetime) -> list[Flag]:
    flags: list[Flag] = []
    paper_trades = metrics["paper_trades"]
    bot_state = metrics["bot_state"]

    live_entries = [
        t for t in paper_trades
        if isinstance(t, dict)
        and t.get("type") == "live_momentum"
        and (dt := _parse_iso(t.get("timestamp"))) is not None
        and dt >= now_utc - timedelta(hours=48)
        and dt < now_utc
    ]
    reasons = top_live_reject_reasons(paths, now_utc, 48)
    if not live_entries and sum(reasons.values()) > 0:
        top = ", ".join(f"{k}={v}" for k, v in reasons.most_common(3)) or "none"
        flags.append(Flag(
            "live_momentum_zero_entries_48h",
            "WARN",
            f"live_momentum: 0 entries in 48h. Top reject reasons: {top}.",
            "Session 17 cadence-limited",
        ))

    resolved_today = metrics["resolved_today"]
    daily_total = sum(float(t.get("pnl") or 0.0) for t in resolved_today)
    if abs(daily_total) >= 50 and resolved_today:
        biggest = max(resolved_today, key=lambda t: abs(float(t.get("pnl") or 0.0)))
        biggest_pnl = float(biggest.get("pnl") or 0.0)
        pct = abs(biggest_pnl) / max(abs(daily_total), 0.01)
        if pct >= 0.30 and abs(biggest_pnl) >= 25:
            flags.append(Flag(
                "single_trade_daily_pnl_dominance",
                "INFO",
                f"Single trade {biggest.get('ticker')} = {pct:.0%} of today's P&L ({_fmt_money(biggest_pnl)}).",
                None,
            ))

    family_caps = metrics["config"]["vig_stack_family_caps"]
    default_cap = float(metrics["config"]["vig_stack_default_cap"])
    at_cap: Counter[str] = Counter()
    over_cap: list[str] = []
    for pos in metrics["active_positions"]:
        family = _family_from_ticker(pos.get("ticker"))
        cap = float(family_caps.get(family, default_cap))
        cost = float(pos.get("cost") or 0.0)
        if cost > cap + 0.01:
            over_cap.append(f"{pos.get('ticker')} ${cost:.2f} > cap ${cap:.0f}")
        if cost >= cap * 0.95:
            at_cap[family] += 1
    for family, n in at_cap.items():
        if n >= 2:
            flags.append(Flag(
                f"family_cap_concentration_{family}",
                "INFO",
                f"{n} positions near family cap in {family}.",
                "Session 53",
            ))
    for item in over_cap:
        flags.append(Flag("position_over_family_cap", "WARN", item, "Session 53"))

    started_at = _parse_iso(bot_state.get("started_at"))
    since_restart = log_lines_since(paths, started_at)
    error_lines = [
        line for line in since_restart
        if "ERROR" in line or "CRITICAL" in line or "Traceback" in line
    ]
    if len(error_lines) > 50:
        flags.append(Flag(
            "log_errors_since_restart",
            "WARN",
            f"{len(error_lines)} error/traceback lines in bot.log since last restart.",
            None,
        ))

    last_heartbeat = _parse_iso(bot_state.get("last_heartbeat"))
    if last_heartbeat is None:
        flags.append(Flag("heartbeat_missing", "CRITICAL", "Heartbeat missing from bot_state.json.", "Battle Scar #6"))
    else:
        age_s = (now_utc - last_heartbeat).total_seconds()
        if age_s > 90:
            flags.append(Flag(
                "heartbeat_stale",
                "CRITICAL",
                f"Heartbeat stale: {age_s:.0f}s (> 90s threshold).",
                "Battle Scar #6",
            ))

    throttled_until = _parse_iso(bot_state.get("telegram_throttled_until"))
    if throttled_until is not None and throttled_until > now_utc:
        flags.append(Flag(
            "telegram_throttled",
            "WARN",
            f"Telegram cooling down until {_format_et(throttled_until)}.",
            "Battle Scar #15",
        ))

    httpx_errors = sum(1 for line in since_restart if "HTTPXRequest is not initialized" in line)
    if httpx_errors > 5:
        flags.append(Flag(
            "httpxrequest_init_race",
            "WARN",
            f"Notifier shutdown/startup race: {httpx_errors} HTTPXRequest errors since restart.",
            "Session 58.5 watch-list",
        ))

    if metrics["open_positions_count"] != metrics["paper_open_count"]:
        flags.append(Flag(
            "positions_paper_open_mismatch",
            "WARN",
            f"positions.json active count ({metrics['open_positions_count']}) != paper_trades open count ({metrics['paper_open_count']}).",
            None,
        ))

    return flags


def extract_watchlist_triggers(claude_text: str) -> list[dict]:
    triggers: list[dict] = []
    lines = claude_text.splitlines()
    current_session = "Session: unknown"
    for idx, line in enumerate(lines):
        m = SESSION_HEADING_RE.match(line)
        if m:
            current_session = m.group(1).strip()
        if not WATCH_KEYWORDS_RE.search(line):
            continue
        block = [line.strip()]
        for nxt in lines[idx + 1: idx + 9]:
            stripped = nxt.strip()
            if not stripped:
                break
            if stripped.startswith("---") or stripped.startswith("### "):
                break
            if stripped.startswith("-") or stripped.startswith("AND ") or stripped.startswith("OR "):
                block.append(stripped)
            elif block and block[-1].endswith((":", "of:")):
                block.append(stripped)
            else:
                break
        triggers.append({
            "line": idx + 1,
            "session": current_session,
            "text": " ".join(block),
        })
    return triggers


def _dataset_rows(paths: Paths) -> list[dict]:
    if not paths.research_dataset.exists():
        return []
    try:
        with paths.research_dataset.open(newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _dataset_float(row: dict, key: str) -> float | None:
    val = row.get(key)
    if val in (None, ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def watchlist_metrics(paths: Paths, metrics: dict, now_utc: datetime) -> dict:
    rows = _dataset_rows(paths)
    challenger = [
        r for r in rows
        if r.get("sport") in {"atp_challenger", "wta_challenger"}
        and r.get("outcome_clv_cents") not in (None, "")
    ]
    challenger_no = [r for r in challenger if r.get("outcome_settlement") == "no_won"]
    wta = [
        r for r in rows
        if r.get("sport") == "wta" and r.get("outcome_clv_cents") not in (None, "")
    ]
    wta_clv = [_dataset_float(r, "outcome_clv_cents") for r in wta]
    wta_clv = [v for v in wta_clv if v is not None]
    no_leader_wta = [
        r for r in wta
        if r.get("skip_reason") == "no_leader"
    ]
    no_leader_wta_clv = [_dataset_float(r, "outcome_clv_cents") for r in no_leader_wta]
    no_leader_wta_clv = [v for v in no_leader_wta_clv if v is not None]

    lm_settled = [t for t in metrics["settled"] if t.get("type") == "live_momentum"]
    lm_pnl = sum(float(t.get("pnl") or 0.0) for t in lm_settled)
    ee_count = sum(1 for t in lm_settled if t.get("status") == "exited_early")
    post_apr23 = datetime(2026, 4, 23, tzinfo=timezone.utc)
    post_apr23_lm = [
        t for t in lm_settled
        if (_parse_iso(t.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= post_apr23
    ]

    by_sport = Counter()
    try:
        from bot.regime import _ticker_to_sport  # noqa: PLC0415
    except Exception:  # pragma: no cover - defensive only
        _ticker_to_sport = lambda _ticker: None  # type: ignore
    for t in lm_settled:
        by_sport[_ticker_to_sport(t.get("ticker") or "") or "unknown"] += 1

    bot_state = metrics["bot_state"]
    restart = _parse_iso(bot_state.get("started_at"))
    since_restart = log_lines_since(paths, restart)
    return {
        "challenger_cf_n": len(challenger),
        "challenger_leader_loss_n": len(challenger_no),
        "wta_cf_n": len(wta),
        "wta_mean_clv": sum(wta_clv) / len(wta_clv) if wta_clv else None,
        "no_leader_wta_n": len(no_leader_wta_clv),
        "no_leader_wta_mean_clv": (
            sum(no_leader_wta_clv) / len(no_leader_wta_clv) if no_leader_wta_clv else None
        ),
        "lm_ee_count": ee_count,
        "lm_per_trade_pnl": lm_pnl / len(lm_settled) if lm_settled else None,
        "post_apr23_lm_settled": len(post_apr23_lm),
        "lm_sport_counts": dict(by_sport),
        "httpx_errors_since_restart": sum(1 for line in since_restart if "HTTPXRequest is not initialized" in line),
        "shutdown_skip_info_since_restart": sum(1 for line in since_restart if "skipped during shutdown" in line),
    }


def evaluate_watchlist_triggers(triggers: list[dict], data: dict) -> list[dict]:
    evaluated: list[dict] = []
    for trig in triggers:
        text = trig["text"]
        low = text.lower()
        status = "MANUAL_CHECK_REQUIRED"
        detail = f"see CLAUDE.md L{trig['line']}"

        if "challenger cfs" in low and "600" in low and "100" in low:
            # Session 65: bar raised after Session 61 Outcome B.
            # Original n>=30 + n_no_won>=5 was met at n=398/leader-loss=122
            # and produced Outcome B (both challengers disabled, per-circuit
            # EVs negative). Re-fire only when materially new data is in:
            # n>=600 combined AND n_no_won>=100. Per-circuit divergence is a
            # separate manual cross-check (out of evaluator scope).
            n = data["challenger_cf_n"]
            losses = data["challenger_leader_loss_n"]
            status = "TRIGGERED" if n >= 600 and losses >= 100 else "NOT_YET_TRIGGERED"
            detail = f"current n={n} / leader-loss={losses}"
        elif "ee cohort" in low and "80" in low:
            ee = data["lm_ee_count"]
            ppt = data["lm_per_trade_pnl"]
            triggered = ee >= 80 or (ppt is not None and ppt <= -1.0)
            status = "TRIGGERED" if triggered else "NOT_YET_TRIGGERED"
            detail = f"EE={ee}; live_momentum per-trade P&L={ppt:+.2f}" if ppt is not None else f"EE={ee}; P&L unavailable"
        elif "settled wta cfs reach" in low or "wta-main" in low:
            n = data["wta_cf_n"]
            mean = data["wta_mean_clv"]
            triggered = n >= 80 or (mean is not None and mean > 0)
            status = "TRIGGERED" if triggered else "NOT_YET_TRIGGERED"
            mean_s = f"{mean:+.2f}c" if mean is not None else "unknown"
            detail = f"wta CF n={n}; mean CLV={mean_s}"
        elif "no_leader/wta" in low or "per-sport `momentum_leader_min` for wta" in low:
            # Session 64 (2026-05-07): bar raised after Pattern B ship.
            # Original n>=30 & mean>0 fired at n=35/+9.34c but threshold
            # sensitivity was non-monotonic (peak +12.05c at 0.60, dip
            # -0.80c at 0.58, n_no_won=5 at peak). Re-fire only when the
            # sub-cohort has grown ~2.3x AND the mean is meaningfully
            # above the noise floor.
            n = data["no_leader_wta_n"]
            mean = data["no_leader_wta_mean_clv"]
            triggered = n >= 80 and mean is not None and mean >= 5.0
            status = "TRIGGERED" if triggered else "NOT_YET_TRIGGERED"
            mean_s = f"{mean:+.2f}c" if mean is not None else "unknown"
            detail = f"no_leader/wta n={n}; mean CLV={mean_s}"
        elif "post-apr-23" in low and "60" in low:
            n = data["post_apr23_lm_settled"]
            status = "TRIGGERED" if n >= 60 else "NOT_YET_TRIGGERED"
            detail = f"post-Apr-23 settled live_momentum n={n}"
        elif "httpxrequest" in low:
            errors = data["httpx_errors_since_restart"]
            skips = data["shutdown_skip_info_since_restart"]
            status = "TRIGGERED" if errors > 0 else "NOT_YET_TRIGGERED"
            detail = f"timestamped HTTPX errors={errors}; shutdown-skip INFO={skips}"
        elif "nba" in low and "30 total settled" in low:
            n = data["lm_sport_counts"].get("nba", 0)
            status = "TRIGGERED" if n >= 30 else "NOT_YET_TRIGGERED"
            detail = f"NBA settled n={n}"
        elif "nhl" in low and "25 total settled" in low:
            n = data["lm_sport_counts"].get("nhl", 0)
            status = "TRIGGERED" if n >= 25 else "NOT_YET_TRIGGERED"
            detail = f"NHL settled n={n}"
        elif "ufc" in low and "25 total settled" in low:
            n = data["lm_sport_counts"].get("ufc", 0)
            status = "TRIGGERED" if n >= 25 else "NOT_YET_TRIGGERED"
            detail = f"UFC settled n={n}"

        evaluated.append({**trig, "status": status, "detail": detail})
    return evaluated


def next_calendar_entries(paths: Paths, now_utc: datetime, limit: int = 5) -> list[dict]:
    if not paths.report_calendar.exists():
        return []
    try:
        text = paths.report_calendar.read_text()
    except OSError:
        return []
    entries: list[dict] = []
    now_et = now_utc.astimezone(helpers.ET)
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|---") or "Routine" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        routine = cells[0]
        fires = cells[1] if re.match(r"\d{4}-\d{2}-\d{2}", cells[1]) else cells[2]
        try:
            dt = datetime.strptime(fires, "%Y-%m-%d %I:%M %p ET").replace(tzinfo=helpers.ET)
        except ValueError:
            continue
        if dt >= now_et:
            entries.append({"routine": routine, "fires": dt, "output": cells[3] if len(cells) > 3 else ""})
    entries.sort(key=lambda e: e["fires"])
    return entries[:limit]


def build_baseline(now_utc: datetime, metrics: dict, discovery: dict, flags: list[Flag]) -> dict:
    return {
        "ts": now_utc.isoformat(),
        "total_pnl": metrics["total_pnl"],
        "settled_count": metrics["settled_count"],
        "vig_stack_pnl": round(float(metrics["pnl_by_type"].get("vig_stack", 0.0)), 2),
        "live_momentum_pnl": round(float(metrics["pnl_by_type"].get("live_momentum", 0.0)), 2),
        "open_positions_count": metrics["open_positions_count"],
        "exposure": metrics["exposure"],
        "discovery_findings_new": discovery["new"],
        "errors_in_log": sum(1 for f in flags if f.id == "log_errors_since_restart"),
        "telegram_last_success": metrics["bot_state"].get("telegram_last_send_success_at"),
        "flags": sorted(f.id for f in flags if f.severity in {"WARN", "CRITICAL"}),
        "open_tickers": metrics["open_tickers"],
        "settled_trade_ids": metrics["settled_trade_ids"],
        "flag_ids": sorted(f.id for f in flags),
    }


def load_last_status(path: Path) -> dict | None:
    data = _load_json(path, None)
    return data if isinstance(data, dict) else None


def render_diff(last: dict | None, current: dict, now_utc: datetime) -> str:
    out = ["## 2. Diff Since Last Check", ""]
    if not last:
        out.append("_No baseline yet - diff available on next run._")
        return "\n".join(out)

    last_ts = _parse_iso(last.get("ts"))
    age = _age_text(now_utc, last_ts)
    out.append(f"Baseline: {_format_et(last_ts)} -> {_format_et(now_utc)} ({age})")
    out.append("")
    pnl_delta = float(current.get("total_pnl", 0.0)) - float(last.get("total_pnl", 0.0))
    settled_delta = int(current.get("settled_count", 0)) - int(last.get("settled_count", 0))
    vig_delta = float(current.get("vig_stack_pnl", 0.0)) - float(last.get("vig_stack_pnl", 0.0))
    lm_delta = float(current.get("live_momentum_pnl", 0.0)) - float(last.get("live_momentum_pnl", 0.0))
    pos_delta = int(current.get("open_positions_count", 0)) - int(last.get("open_positions_count", 0))
    exposure_delta = float(current.get("exposure", 0.0)) - float(last.get("exposure", 0.0))
    findings_delta = int(current.get("discovery_findings_new", 0)) - int(last.get("discovery_findings_new", 0))
    open_now = set(current.get("open_tickers") or [])
    open_last = set(last.get("open_tickers") or [])
    settled_now = set(current.get("settled_trade_ids") or [])
    settled_last = set(last.get("settled_trade_ids") or [])
    flags_now = set(current.get("flag_ids") or current.get("flags") or [])
    flags_last = set(last.get("flag_ids") or last.get("flags") or [])

    out.extend([
        f"- P&L: { _fmt_money(float(last.get('total_pnl', 0.0))) } -> { _fmt_money(float(current.get('total_pnl', 0.0))) } ({_fmt_money_delta(pnl_delta)} / {_fmt_int_delta(settled_delta)} settled)",
        f"- vig_stack: { _fmt_money(float(last.get('vig_stack_pnl', 0.0))) } -> { _fmt_money(float(current.get('vig_stack_pnl', 0.0))) } ({_fmt_money_delta(vig_delta)})",
        f"- live_momentum: { _fmt_money(float(last.get('live_momentum_pnl', 0.0))) } -> { _fmt_money(float(current.get('live_momentum_pnl', 0.0))) } ({_fmt_money_delta(lm_delta)})",
        f"- Positions: {last.get('open_positions_count', 0)} -> {current.get('open_positions_count', 0)} ({_fmt_int_delta(pos_delta)} net; {len(open_last - open_now)} closed, {len(open_now - open_last)} new)",
        f"- Exposure: { _fmt_money(float(last.get('exposure', 0.0)), signed=False) } -> { _fmt_money(float(current.get('exposure', 0.0)), signed=False) } ({_fmt_money_delta(exposure_delta)})",
        f"- Findings NEW: {last.get('discovery_findings_new', 0)} -> {current.get('discovery_findings_new', 0)} ({_fmt_int_delta(findings_delta)})",
        f"- Newly settled trade records: {len(settled_now - settled_last)}",
    ])
    new_flags = sorted(flags_now - flags_last)
    if new_flags:
        out.append("")
        out.append("New flags this period:")
        for flag_id in new_flags:
            out.append(f"- {flag_id}")
    else:
        out.append("")
        out.append("New flags this period: none")
    return "\n".join(out)


def safe_persist_status(path: Path, data: dict) -> bool:
    try:
        state_io.save_json(path, data)
        return True
    except Exception:
        return False


def write_snapshot_md(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.rename(path)
        return True
    except OSError:
        return False


def render_verdict(metrics: dict, flags: list[Flag], discovery: dict, watch: list[dict], now_utc: datetime) -> str:
    critical = [f for f in flags if f.severity == "CRITICAL"]
    warn = [f for f in flags if f.severity == "WARN"]
    triggered = [w for w in watch if w["status"] == "TRIGGERED"]
    manual = [w for w in watch if w["status"] == "MANUAL_CHECK_REQUIRED"]
    if critical:
        label = "CRITICAL"
    elif warn:
        label = "Degraded"
    else:
        label = "Healthy"
    return "\n".join([
        "# Glint Status",
        "",
        f"Generated: {_format_et(now_utc)}",
        "",
        "## 1. Verdict",
        "",
        (
            f"Verdict: **{label}.** {metrics['open_positions_count']} positions / "
            f"{_fmt_money(metrics['exposure'], signed=False)} exposure / "
            f"{_fmt_money(metrics['total_pnl'])} net. "
            f"{len(warn)} WARN, {len(critical)} CRITICAL, "
            f"{discovery['new']} NEW discovery findings, "
            f"{len(triggered)} triggered watch-list checks, {len(manual)} manual checks."
        ),
    ])


def render_health_section(daily: DailyReport) -> str:
    out = ["## 3. Health Pulse", ""]
    if daily.path is None:
        out.append("_No daily report found._")
        return "\n".join(out)
    out.append(f"Source: `{daily.path.relative_to(daily.path.parents[3]) if len(daily.path.parents) > 3 else daily.path}` {daily.stale_note}".rstrip())
    out.append("")
    section = extract_markdown_section(daily.text, "1. Health pulse")
    if not section:
        out.append("_Health pulse section not found in latest daily report._")
    else:
        out.append(section)
    return "\n".join(out)


def render_pnl_section(metrics: dict, daily: DailyReport, now_utc: datetime) -> str:
    out = ["## 4. P&L", ""]
    out.append(f"Total realized: **{_fmt_money(metrics['total_pnl'])}** across {metrics['settled_count']} settled trades.")
    out.append("")
    for strat in sorted(metrics["pnl_by_type"]):
        pnl = float(metrics["pnl_by_type"][strat])
        n = int(metrics["settled_by_type"].get(strat, 0))
        wins = int(metrics["wins_by_type"].get(strat, 0))
        wr = (100.0 * wins / n) if n else 0.0
        out.append(f"- `{strat}`: {_fmt_money(pnl)} / {n} settled / {wr:.0f}% WR")
    if daily.generated_at:
        since = [
            t for t in metrics["settled"]
            if (dt := _parse_iso(t.get("resolved_at"))) is not None and dt > daily.generated_at and dt <= now_utc
        ]
        since_pnl = sum(float(t.get("pnl") or 0.0) for t in since)
        out.append("")
        out.append(f"Delta since latest daily report generation ({_format_et(daily.generated_at)}): {_fmt_money(since_pnl)} / +{len(since)} settled.")
    daily_section = extract_markdown_section(daily.text, "4. Trade activity")
    if daily_section:
        compact = "\n".join(daily_section.splitlines()[:14]).strip()
        out.append("")
        out.append("Daily report excerpt:")
        out.append("")
        out.append(compact)
    return "\n".join(out)


def render_positions_section(metrics: dict) -> str:
    out = ["## 5. Open Positions", ""]
    active = metrics["active_positions"]
    exposure = metrics["exposure"]
    bankroll = metrics["bankroll"]
    pct = (100.0 * exposure / bankroll) if bankroll else 0.0
    out.append(f"Active positions: **{len(active)}**. Exposure: **{_fmt_money(exposure, signed=False)}** ({pct:.1f}% of paper bankroll).")
    if metrics["paper_open_count"] != metrics["open_positions_count"]:
        out.append(f"Cross-check: paper_trades has {metrics['paper_open_count']} open rows ({_fmt_money(metrics['paper_exposure'], signed=False)} exposure).")
    if not active:
        out.append("")
        out.append("_No active positions in positions.json._")
        return "\n".join(out)

    caps = metrics["config"]["vig_stack_family_caps"]
    default_cap = float(metrics["config"]["vig_stack_default_cap"])
    by_family: dict[str, list[dict]] = defaultdict(list)
    for pos in active:
        by_family[_family_from_ticker(pos.get("ticker"))].append(pos)
    out.append("")
    out.append("| Family | N | Exposure | Cap | Status |")
    out.append("|---|---:|---:|---:|---|")
    for family in sorted(by_family):
        rows = by_family[family]
        fam_exp = sum(float(p.get("cost") or 0.0) for p in rows)
        cap = float(caps.get(family, default_cap))
        over = sum(1 for p in rows if float(p.get("cost") or 0.0) > cap + 0.01)
        near = sum(1 for p in rows if float(p.get("cost") or 0.0) >= cap * 0.95)
        if over:
            status = f"WARN: {over} over cap"
        elif near:
            status = f"{near} near cap"
        else:
            status = "OK"
        out.append(f"| `{family}` | {len(rows)} | {_fmt_money(fam_exp, signed=False)} | ${cap:.0f} | {status} |")
    return "\n".join(out)


def render_discovery_section(discovery: dict) -> str:
    out = ["## 6. Discovery Findings", ""]
    out.append(f"Source: {discovery['source_note']}")
    out.append(f"Counts: **{discovery['new']} NEW**, **{discovery['stable']} STABLE**, **{discovery['resolved']} RESOLVED**.")
    findings = discovery["findings"]
    by_fp = {str(f.get("fingerprint")): f for f in findings if f.get("fingerprint")}
    new_fps = discovery.get("new_fingerprints") or set()
    new_rows = [by_fp[fp] for fp in new_fps if fp in by_fp]
    if new_rows:
        out.append("")
        out.append("NEW findings:")
        for f in new_rows:
            out.append(f"- [{f.get('severity')}] {f.get('title')} -- {f.get('summary')}")
            ev = f.get("evidence") if isinstance(f.get("evidence"), dict) else {}
            ctx_keys = [
                "cross_cohort_total_n",
                "cross_cohort_mean_clv_cents",
                "n_disabled_sport_cohorts_in_top3",
            ]
            ctx = ", ".join(f"{k}={ev[k]}" for k in ctx_keys if k in ev)
            if str(f.get("severity")).lower() == "high" and ctx:
                out.append(f"  Cross-cohort context: {ctx}")
    else:
        out.append("")
        out.append("NEW findings: none this run.")
    if discovery["stable"]:
        out.append(f"STABLE findings collapsed: {discovery['stable']}.")
    if discovery["resolved"]:
        out.append(f"RESOLVED findings collapsed: {discovery['resolved']}.")
    return "\n".join(out)


def render_anomalies_watchlist(anomalies: list[Flag], watch: list[dict]) -> str:
    out = ["## 7. Anomalies + Watch-List Status", ""]
    out.append("Anomalies:")
    if not anomalies:
        out.append("- OK: no anomaly flags.")
    else:
        for f in anomalies:
            ref = f" ({f.ref})" if f.ref else ""
            out.append(f"- {f.severity}: {f.message}{ref}")
    out.append("")
    out.append("Watch-list checks:")
    if not watch:
        out.append("- No watch-list triggers extracted.")
    else:
        for item in watch:
            out.append(f"- {item['status']}: {item['session']} L{item['line']} - {item['detail']}")
    return "\n".join(out)


def render_calendar_section(entries: list[dict]) -> str:
    out = ["## 8. Calendar", ""]
    if not entries:
        out.append("_No future dated routines parsed from REPORT_CALENDAR.md._")
        return "\n".join(out)
    for e in entries:
        out.append(f"- {_format_et(e['fires'])}: {e['routine']}")
    return "\n".join(out)


def render_flags_section(flags: list[Flag], daily: DailyReport, watch: list[dict]) -> str:
    all_flags = list(flags)
    if daily.stale_note:
        all_flags.append(Flag("daily_report_stale", "WARN", f"Latest daily report is {daily.stale_note}.", "Session 35"))
    manual = [w for w in watch if w["status"] == "MANUAL_CHECK_REQUIRED"]
    triggered = [w for w in watch if w["status"] == "TRIGGERED"]
    if manual:
        all_flags.append(Flag("watchlist_manual_checks", "WARN", f"{len(manual)} watch-list triggers require manual evaluation.", None))
    if triggered:
        all_flags.append(Flag("watchlist_triggered", "WARN", f"{len(triggered)} watch-list triggers are currently triggered.", None))

    out = ["## 9. Flags", ""]
    if not all_flags:
        out.append("OK: no flags.")
        return "\n".join(out)
    for sev in ("CRITICAL", "WARN", "INFO"):
        rows = [f for f in all_flags if f.severity == sev]
        if not rows:
            continue
        out.append(f"{sev}:")
        for f in rows:
            ref = f" ({f.ref})" if f.ref else ""
            out.append(f"- `{f.id}`: {f.message}{ref}")
        out.append("")
    return "\n".join(out).rstrip()


def build_snapshot(repo_root: Path = _REPO_ROOT, now_utc: datetime | None = None, persist: bool = True) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    paths = paths_for(repo_root)
    metrics = collect_metrics(paths, now_utc)
    daily = latest_daily_report(paths, now_utc)
    discovery = count_discovery_findings(paths, now_utc)
    anomalies = detect_anomalies(paths, metrics, now_utc)

    claude_text = ""
    try:
        claude_text = paths.claude_md.read_text()
    except OSError:
        pass
    triggers = extract_watchlist_triggers(claude_text)
    watch_data = watchlist_metrics(paths, metrics, now_utc)
    watch = evaluate_watchlist_triggers(triggers, watch_data)

    flags_for_baseline = list(anomalies)
    if daily.stale_note:
        flags_for_baseline.append(Flag("daily_report_stale", "WARN", "latest daily report is stale", "Session 35"))
    if any(w["status"] == "MANUAL_CHECK_REQUIRED" for w in watch):
        flags_for_baseline.append(Flag("watchlist_manual_checks", "WARN", "manual watch-list checks", None))
    if any(w["status"] == "TRIGGERED" for w in watch):
        flags_for_baseline.append(Flag("watchlist_triggered", "WARN", "triggered watch-list checks", None))

    current_baseline = build_baseline(now_utc, metrics, discovery, flags_for_baseline)
    last = load_last_status(paths.last_status)
    calendar_entries = next_calendar_entries(paths, now_utc)

    sections = [
        render_verdict(metrics, flags_for_baseline, discovery, watch, now_utc),
        render_diff(last, current_baseline, now_utc),
        render_health_section(daily),
        render_pnl_section(metrics, daily, now_utc),
        render_positions_section(metrics),
        render_discovery_section(discovery),
        render_anomalies_watchlist(anomalies, watch),
        render_calendar_section(calendar_entries),
        render_flags_section(anomalies, daily, watch),
    ]
    output = "\n\n---\n\n".join(s.rstrip() for s in sections).rstrip() + "\n"

    if persist:
        safe_persist_status(paths.last_status, current_baseline)
        today = now_utc.astimezone(helpers.ET).date().isoformat()
        write_snapshot_md(paths.state_dir / f"glint_status_{today}.md", output)
    return output


def main(argv: list[str] | None = None) -> int:
    _ = argv  # reserved for future flags; keep CLI intentionally simple in v1.
    sys.stdout.write(build_snapshot(_REPO_ROOT, persist=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
