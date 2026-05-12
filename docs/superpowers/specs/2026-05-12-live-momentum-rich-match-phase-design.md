# Live-Momentum Rich Match-Phase State Design — Session 110 Outcome B

Date: 2026-05-12

## Decision

Do not ship the live_momentum rich match-phase field extension in Session 110.

Phase 0 confirmed that ESPN exposes rich cricket state via the `cricket/8048/scoreboard` endpoint (full innings + over_count + wickets schema) for IPL. However, the bot does NOT currently call ESPN for IPL — `ESPN_SPORT_PATHS` at [bot/config.py:765-771](hustle-agent/bot/config.py:765) contains only `{nba, mlb, nhl, nfl, ncaab}`. Enabling IPL would require adding a new sport mapping that activates a per-tick HTTP fetch the bot has never made before, which violates two of this session's discipline constraints:

1. **No new network calls** (S96/S107 lineage). Adding `"ipl": "cricket/8048"` to `ESPN_SPORT_PATHS` makes `_fetch_espn_score` start hitting ESPN every 10s tick during IPL games. That's exactly the "new network call in the decision path" pattern these constraints are designed to prevent without a deliberate, scoped session.
2. **No ESPN integration, no cricket API** (S110 brief out-of-scope). Even though ESPN is generally a known data source for team sports, adding a new sport mapping that wires up cricket is exactly the "new integration" pattern the brief flags.

Outcome A is therefore rejected. Outcome C is too strong because the data exists and the integration path is concrete. This is a deferred-A, not a kill-forever: a future session can add the integration with the full discipline (per-fetch latency budget, error handling, observability, watch-list trigger for cohort N growth).

Tennis (ATP/WTA, currently disabled per Session 97) and UFC are also out of source scope — see "Sport-by-Sport Reachability" below.

This decision unblocks the S107 follow-on entry in CLAUDE.md "Open Loops" by replacing the deferred reference with this design doc.

## Phase 0 Empirical Findings

### Cricket (IPL)

**Reachable schema at `https://site.api.espn.com/apis/site/v2/sports/cricket/8048/scoreboard`** (HTTP 200, ~12-21 KB response). Numeric league ID `8048` is required — the slug `cricket/indian-premier-league` returns HTTP 404.

Per-event structure (sampled from the 2026-05-10 Chennai Super Kings vs Lucknow Super Giants final, second-innings chase):

```json
{
  "events": [{
    "name": "Chennai Super Kings v Lucknow Super Giants",
    "status": { "type": { "state": "post", "detail": "Final" } },
    "competitions": [{
      "status": { "period": 2, "displayClock": "0'" },
      "competitors": [{
        "team": { "displayName": "Chennai Super Kings" },
        "score": "208/5 (19.2/20 ov, target 204)",
        "winner": true,
        "linescores": [
          {"period":1,"runs":0,"wickets":0,"overs":20.0,"isBatting":false,"isCurrent":0,"description":"complete"},
          {"period":2,"runs":208,"wickets":5,"overs":19.2,"isBatting":true,"isCurrent":1,"description":"target reached"}
        ]
      }, {
        "team": { "displayName": "Lucknow Super Giants" },
        "score": "203/8",
        "winner": false,
        "linescores": [
          {"period":1,"runs":203,"wickets":8,"overs":20.0,"isBatting":true,"isCurrent":0,"description":"complete"},
          {"period":2,"runs":0,"wickets":0,"overs":19.2,"isBatting":false,"isCurrent":1,"description":"target reached"}
        ]
      }]
    }]
  }]
}
```

The schema provides every field the regime tagger needs:

| Regime field | ESPN source | Extraction rule |
|---|---|---|
| `over_count` (int 1..20) | `linescores[].overs` (float) | `max(1, min(20, int(overs) + 1))` for the linescore where `isBatting==True` AND `isCurrent==1` |
| `innings` (1 or 2) | `competitions[0].status.period` OR `linescores[].period` | Direct int read |
| `wickets_lost` | `linescores[].wickets` | Direct int read on the current batting linescore |
| `runs_scored` | `linescores[].runs` | Direct int read on the current batting linescore |
| `score_state` | `competitors[].score` (formatted string) | Either parse the string or compose from runs/wickets |

Cricket overs notation: `overs=19.2` means "19 complete overs + 2 balls bowled in over 20." Standard cricket parsing — `int(overs) + 1` clamped to `[1, 20]` produces the over_count regime spec expects.

### Tennis (ATP / WTA)

ESPN endpoints respond at `tennis/atp/scoreboard` and `tennis/wta/scoreboard` (both HTTP 200), but the events returned are **tournament-level**, not match-level:

```json
{
  "events": [{
    "name": "Internazionali BNL d'Italia",
    "status": { "type": { "state": "post" } },
    "competitions": [{
      "status": { "period": null },
      "competitors": [{ "score": "" }]
    }]
  }]
}
```

`status.period` is null. There are no per-match competitors with set scores at this level. Match-level state would require a drill-down via per-tournament event detail endpoints (the same `$ref` pattern visible in the cricket `leaders` blob), which is materially more complex than cricket: per-tournament path discovery + per-match fetch + state-machine reconciliation for live matches.

Even if tennis match-level state were sourced, ATP/ATP_Challenger/WTA/WTA_Challenger are currently disabled per Session 97 (`MOMENTUM_DISABLED_SPORTS` at [bot/config.py:239](hustle-agent/bot/config.py:239)). Shadow-ledger instrumentation does still run but its cohort observability is bounded by entry volume, which is zero post-S97 re-disable.

Tennis is therefore deferred to a separate session AFTER the IPL ESPN integration ships and AFTER tennis is re-enabled (or shadow-ledger volume justifies match-level fetches).

### UFC

UFC is not in `ESPN_SPORT_PATHS` and ESPN does not expose live UFC fight state via the `site.api` family. Confirmed via probe (no `combat-sports/ufc/scoreboard` family). Round number is unreachable from any free source we currently use.

UFC stays elapsed-only via the existing `_context_match_phase` and `regime._match_phase` time-path branches. Cohort N is held at n=11 per Session 97 watch-list trigger (re-investigate at N≥15).

### NHL

Already reachable via the existing ESPN team-sport parse (`status.period`, `displayClock`, competitor scores). However, `regime._MATCH_PHASE_SPORTS` at [bot/regime.py:106-108](hustle-agent/bot/regime.py:106) only includes `{atp, atp_challenger, wta, wta_challenger, ufc, ipl}`. Adding NHL would require both (a) a regime taxonomy decision (period_1 / period_2 / period_3 / overtime?) and (b) bucket-validation work that's orthogonal to this session's IPL focus.

Out of scope for the follow-on session as well, unless a separate session establishes that NHL `match_phase` slicing yields net cohort signal beyond what `sport_phase` (regular/playoffs) already provides.

## What a Future Integration Session Looks Like

This is the implementation blueprint for the deferred Outcome A. Use this as the brief shape when scheduling the follow-on.

### Scope

- Add `"ipl": "cricket/8048"` to `ESPN_SPORT_PATHS` at [bot/config.py:765-771](hustle-agent/bot/config.py:765).
- Extend `_fetch_espn_score` at [bot/live_watcher.py:2699-2805](hustle-agent/bot/live_watcher.py:2699) with a sport-discriminated branch for `sport=="ipl"` that extracts `over_count`, `innings`, `wickets_lost`, `runs_scored` from `linescores[]` where `isBatting==True` AND `isCurrent==1`. Keep the generic team-sport return shape unchanged.
- Extend `_LIVE_MOMENTUM_CONTEXT_FIELDS` allowlist at [bot/live_watcher.py:69-75](hustle-agent/bot/live_watcher.py:69) with the new fields.
- Extend `_build_live_momentum_decision_context` at [bot/live_watcher.py:128-258](hustle-agent/bot/live_watcher.py:128) — accept new kwargs, populate from `espn_data` if not passed explicitly, include in returned context dict, update `missing_context_fields`.
- Extend `_context_match_phase` at [bot/live_watcher.py:108-125](hustle-agent/bot/live_watcher.py:108) — use the new state-path keys when present, falling back to elapsed-time (which today returns `None` for IPL).
- Thread `over_count` into `mom_ctx` at [bot/live_watcher.py:1497-1510](hustle-agent/bot/live_watcher.py:1497) (the existing forward-compat TODO marker site). Removes the TODO comment block.
- TDD-style tests in `tests/test_live_watcher.py`: `_with_over_count_populates_match_phase`, `_without_over_count_annotates_missing`, `_non_ipl_sport_unchanged_shape` (spillover regression-lock).
- Update CLAUDE.md "Canonical Data Schema Reference" Session 96 subsection.
- Schedule a 14d watch-list trigger to re-measure IPL `match_phase × leader_strength` cohort N.

### Cost estimate

- **HTTP overhead**: 1 fetch per 10s tick per active IPL watcher. Response size ~12-21 KB. Latency observed in this Phase 0: ~200-500 ms. Identical profile to existing NHL ESPN fetch.
- **Concurrent watchers**: at peak IPL season, typically 1-2 simultaneous matches. Daily total fetches: ~36/min × 60 min × 4 hours × 2 matches = ~17,280 fetches/day per match-day. Well under ESPN free-tier rate limits (empirically: NHL ESPN runs with no rate limiting issues at higher concurrency).
- **Failure modes**: ESPN cricket endpoint can return empty payloads outside IPL season (mid-Oct → mid-Mar). Bot logs warning once per watcher via existing `_espn_unsupported_logged` flag at line 2708 — no new failure-handling needed.
- **PII / data hygiene**: ESPN responses contain public player names + match metadata only. No PII concerns; same posture as NHL fetch.

### Discipline constraints for the follow-on session

- **Re-validate the endpoint before shipping**. ESPN can rotate league IDs or paths (the slug `cricket/indian-premier-league` worked at some prior point — see `_PERIOD_LABELS["ipl"]` history, if applicable). Phase 0 of the follow-on session should re-probe the URL and capture a fresh response sample for the design-doc lineage.
- **Verify off-season behavior** before deploying. Probe the endpoint during IPL off-season; if the response is well-formed empty, no special handling needed. If it errors, add explicit off-season skip logic.
- **Spillover regression-lock**. Test that vig_stack rows do not acquire the new fields, mirroring S107's `TestSession107ContextSpillover` at `tests/test_bot_executor.py`.
- **Watch-list trigger**. After 14 days post-restart, re-measure IPL cohort observability per the active_observations.json Session 110 follow-on entry.

## Out of Scope (this Session 110)

- Bot code changes. This session is documentation-only.
- ESPN_SPORT_PATHS additions. The whole point of this design doc is to defer that change to a scoped session.
- Tennis match-level integration. Separate deferred work; bundle with shadow-ledger volume justification.
- NHL `regime._MATCH_PHASE_SPORTS` expansion. Separate session entirely.
- UFC. No reachable source.
- live_momentum behavior changes (entries, exits, sizing, disable changes). Out of all S110 paths.

## Open Loops Update

Replace the existing S107 follow-on entry under CLAUDE.md "Open Loops" → "Blocked: needs dedicated session to unblock data collection":

- **OLD**: Unblocked by "source compact sport-state fields ... without full ESPN blobs."
- **NEW**: Source: [docs/superpowers/specs/2026-05-12-live-momentum-rich-match-phase-design.md]. Unblocks: ship the IPL ESPN integration described in the design doc, gated on a scoped follow-on session with the discipline constraints listed above. Tennis remains deferred (requires per-match drill-down + ATP/WTA re-enable).

## Decision Lineage

- Brief: Session 110 prompt (rich match-phase context for IPL + tennis, S107 follow-on)
- Phase 1 exploration: parallel Explore agents identified the forward-compat hook in `bot/regime.py:_match_phase` and the TODO marker at `bot/live_watcher.py:1502-1510`.
- Phase 0 empirical: ESPN cricket scoreboard probed at three candidate paths; `cricket/8048/scoreboard` returns rich linescores; configured slug `cricket/indian-premier-league` returns HTTP 404. ESPN tennis returns tournament-level events without match state. ESPN UFC has no scoreboard family.
- Outcome decision: Outcome B, this design doc.
- Operator review: approved (plan ExitPlanMode).
