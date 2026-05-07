# Report Calendar

Index of all scheduled routines for the hustle-agent bot. The **Last Run** column is updated automatically by each routine when it completes successfully — if a stamp is more than one cadence-window old, the routine missed a fire and is worth investigating.

All times are in Eastern Time. The bot wall clock and ET match.

## Recurring routines

| Routine | Cadence | Next Run | Last Run | Output | Why | Notes |
|---|---|---|---|---|---|---|
| Daily report | Daily 3:00 AM ET | (after first fire) | 2026-05-07T05:42Z | `bot/state/reports/daily/daily_report_YYYY-MM-DD.md` | Verify bot health + capture every metric we collect | Health pulse on top (5 rows) + 9 data sections below |
| Weekly report | Sundays 6:00 AM ET | (after first fire) | 2026-05-03T17:39Z | `bot/state/reports/weekly/weekly_report_YYYY-WNN.md` | Synthesis layer: calibration findings + retuning candidates | Daily content + 6 weekly-only sections (week-over-week deltas, bucket analysis, dataset rebuild, excursion + exit-replay, calibration findings, retuning candidates) |
| Discovery agent | Daily 6:00 AM ET | (after first fire) | — | `bot/state/discovery/discovery_report_YYYY-MM-DD.md` | Heuristic scan for new patterns, outliers, and emergent cohorts across all bot data | Session 43a framework + 8 heuristics: outlier_pnl, cohort_emergence (Session 43b refined: ticker-count + accept-rate + futures/per-game distinction), threshold_proximity, counterfactual_hotspots, universe_gap, live_tick_anomalies (streaming), cadence_outcome, log_error_spike (streaming). NEW/STABLE/RESOLVED dedup across runs. Pure stdlib, no LLM, no API. |

## One-off routines

| Routine | Fires | Status | Output | Why |
|---|---|---|---|---|
| Session 39 day-1 spot check | 2026-05-01 9:00 AM ET | scheduled | `bot/state/reports/session_39_day_1_2026-05-01.md` | Verify asyncio executor wrapping holds across one full normal-day. Heartbeat lag, _position_check_loop cadence, partial rate. HEALTHY / DEGRADED / WEDGED-AGAIN. |
| Session 36 day-7 hold-to-settlement check | 2026-05-06 9:00 AM ET | scheduled | `bot/state/reports/vig_stack_holds_2026-05-06.md` | Did vig_stack early-exit % drop from 32% baseline after Session 36 TP/SL exemption shipped? |
| Session 39 day-7 flaky-Kalshi stress check | 2026-05-07 9:00 AM ET | scheduled | `bot/state/reports/session_39_day_7_2026-05-07.md` | Did Session 39 fix hold through any flaky-Kalshi windows during the week? Per-day partial rate, wedge events (heartbeat gaps > 5min), cadence p95. HOLD / REGRESSED / NO-STRESS-OCCURRED. |
| Weekly digest spot-check | 2026-05-12 9:00 AM ET | scheduled | inline | 2-week vs Apr 28 baseline; pre-readiness for May 18 Session 22 auto-fire |
| Session 36 day-14 floor signal recheck | 2026-05-13 9:00 AM ET | scheduled | `bot/state/reports/session_36_day_14_weekly_2026-05-13.md` | Did `non_stable_below_weather_floor` mean CLV diminish from +0.2438? If yes, Session 37 floor-tune becomes a candidate. |
| Session 38a ATP re-validation | 2026-05-13 9:00 AM ET | scheduled | inline | +14d post-deploy: re-runs bucket report on post-Session-38a cohort. CONFIRM/REVERT main-tour ATP re-enable per Session 38a evidence rule. Mirrors Session 22 pattern. |
| Session 49 per-sport sizing re-validation | 2026-05-15 9:00 AM ET | scheduled | inline | +14d post-deploy: per-sport contract count + P&L by sport on post-2026-05-01 cohort. CONFIRM / EXPAND / REVERT NBA & UFC size_multiplier=0.5x cuts. Mirrors Session 22 / 38a pattern. |
| MOMENTUM_LEADER_MIN re-validation | 2026-05-18 9:00 AM ET | scheduled | inline | 3-week re-validation of Session 19c shipment (0.70 → 0.65); CONFIRM / REVERT / INCONCLUSIVE |

## Completed

| Routine | Fired | Outcome |
|---|---|---|
| Session 17 cadence verification 72h | 2026-04-29 12:21 AM ET | lastRunAt only — no detailed outcome captured by dispatched chat |
| Session 28 cursor stability check | 2026-04-29 9:07 AM ET | Dispatched chat died at 11s with 3 in-flight Bash calls; no commit. Manual re-run same morning showed cursor_rows median 1850 over n=4 (3× the 632 baseline) — sample too thin to commit, rescheduled for Apr 30 |
| Session 36 smoke test (1h post-restart) | 2026-04-29 9:33 PM ET | Fired ~8 min late vs scheduled 9:25 PM. lastRunAt confirmed; outcome captured in chat. (Bot has been on Session 36 + 37 + 38a code since; the SKIPPED smoke signal will appear naturally as vig_stack positions drift into TP/SL territory.) |
| Cursor stability re-check | 2026-04-30 5:00 PM ET | **STABLE.** Cursor_rows median 1949 over n=32 unique scans (3× the 632 baseline). Distribution tight: min=810, p25=1612, p75=2206, max=2782. Partial flag 100% (expected — Kalshi API ceiling, Session 28-2 deferred). Cadence median 32.0s over n=99 samples (under Session 29 trigger of 35s — Apr 28 watch-list drift entry resolved-as-stable). No regression from Sessions 38a/38a-followup/39. Committed as a5be853. Report: `bot/state/reports/cursor_stability_2026-04-30.md`. |

## How this file works

- Each routine writes its report file to disk **before** doing any interpretation or commit work — partial chat death preserves the data
- The Last Run stamp is the source of truth: if you `cat` this file and a stamp is stale, that routine missed a fire
- All reports under `bot/state/reports/` are read-only outputs — safe to grep, cat, share
- Updates to this file are local-only by default; Tyler commits the calendar history when he wants a snapshot

## Adding a new routine

To add a routine to this calendar:
1. Schedule it via `mcp__scheduled-tasks__create_scheduled_task` (recurring: cron expression in local time; one-off: ISO 8601 timestamp with offset)
2. Add a row above (recurring or one-off table) with the routine's cadence, output path, and purpose
3. The routine's prompt should include a step to update its own Last Run stamp here
