# Report Calendar

Index of all scheduled routines for the hustle-agent bot. The **Last Run** column is updated automatically by each routine when it completes successfully — if a stamp is more than one cadence-window old, the routine missed a fire and is worth investigating.

All times are in Eastern Time. The bot wall clock and ET match.

## Recurring routines

| Routine | Cadence | Next Run | Last Run | Output | Why | Notes |
|---|---|---|---|---|---|---|
| Daily report | Daily 3:00 AM ET | (after first fire) | — | `bot/state/reports/daily/daily_report_YYYY-MM-DD.md` | Verify bot health + capture every metric we collect | Health pulse on top (5 rows) + 9 data sections below |
| Weekly report | Sundays 6:00 AM ET | (after first fire) | — | `bot/state/reports/weekly/weekly_report_YYYY-WNN.md` | Synthesis layer: calibration findings + retuning candidates | Daily content + 6 weekly-only sections (week-over-week deltas, bucket analysis, dataset rebuild, excursion + exit-replay, calibration findings, retuning candidates) |

## One-off routines

| Routine | Fires | Status | Output | Why |
|---|---|---|---|---|
| Cursor stability re-check | 2026-04-30 5:00 PM ET | scheduled | inline + `bot/state/reports/cursor_stability_2026-04-30.md` | Apr 29 9:07 AM routine died at n=4; tomorrow's larger sample confirms STABLE branch |
| Session 36 day-7 hold-to-settlement check | 2026-05-06 9:00 AM ET | scheduled | `bot/state/reports/vig_stack_holds_2026-05-06.md` | Did vig_stack early-exit % drop from 32% baseline after Session 36 TP/SL exemption shipped? |
| Weekly digest spot-check | 2026-05-12 9:00 AM ET | scheduled | inline | 2-week vs Apr 28 baseline; pre-readiness for May 18 Session 22 auto-fire |
| Session 36 day-14 floor signal recheck | 2026-05-13 9:00 AM ET | scheduled | `bot/state/reports/session_36_day_14_weekly_2026-05-13.md` | Did `non_stable_below_weather_floor` mean CLV diminish from +0.2438? If yes, Session 37 floor-tune becomes a candidate. |
| MOMENTUM_LEADER_MIN re-validation | 2026-05-18 9:00 AM ET | scheduled | inline | 3-week re-validation of Session 19c shipment (0.70 → 0.65); CONFIRM / REVERT / INCONCLUSIVE |

## Completed

| Routine | Fired | Outcome |
|---|---|---|
| Session 17 cadence verification 72h | 2026-04-29 12:21 AM ET | lastRunAt only — no detailed outcome captured by dispatched chat |
| Session 28 cursor stability check | 2026-04-29 9:07 AM ET | Dispatched chat died at 11s with 3 in-flight Bash calls; no commit. Manual re-run same morning showed cursor_rows median 1850 over n=4 (3× the 632 baseline) — sample too thin to commit, rescheduled for Apr 30 |

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
