# Hustle Agent — Glint Trading Bot

An autonomous prediction-market trading system built on top of Kalshi's REST API. It runs a continuous scan→rank→size→execute loop across a curated set of active strategies, enforces a multi-layer position safety chain, and manages a full paper-trading sandbox before any real money changes hands.

The original agentic reasoning layer (`agent/`) still exists and owns the Kalshi API client, but **Glint** (`bot/`) is the production system. This document covers the bot in depth.

---

## Current Status (May 8, 2026)

- **Mode:** `PAPER_MODE = True` — **$10,500 simulated balance** (bumped 500 → 10,500 on Apr 29, 2026 with a +$10,000 deposit to scale position sizes for faster signal accumulation; reconstructed balance post-restart = $10,402 with historical −$98 P&L), full pipeline, zero live orders.
- **Active strategies (`ACTIVE_STRATEGIES`):** `vig_stack_series`, `vig_stack_futures`. Plus `live_momentum` via the live-game watcher subsystem. **Sports arb strategies (`sports_monotonicity_arb`, `sports_consistency_arb`) DISABLED Session 56** after Codex review surfaced opportunity-vs-execution shape mismatch (would have fired one-sided directional bets labeled "riskless"; 0 historical fills).
- **Paper performance** (current — from `bot/state/paper_trades.json`, the ground-truth ledger, May 7, 2026):
  - `vig_stack`: 177 settled, **+$614.33**, 120W/6L/51EE (68% WR, +$3.47/trade) — **+$297 over the last 2 days on 8 settled trades**, post-Session-53 family caps appear to be working as intended. The "outlier-dependent" framing from Session 43-investigate has weakened materially; the ledger is now self-supporting.
  - `live_momentum`: 80 settled, **−$34.60**, 24W/8L/48EE (30% WR, −$0.43/trade) — Win:Loss=0.261 structural finding from Session 40; **sizing was at 9% of intended Kelly until Session 54 fixed the phantom-balance bug on May 5**. No new live_momentum trades have settled since May 6, so post-Session-54 corrected-sizing data hasn't accumulated yet. May 15 / May 18 routine cluster is the first true test.
  - **Net: +$579.73 across 257 settled trades** on the $10,500 paper base (~5.5% return). Bot is genuinely profitable from vig_stack as a structural arb — not just outlier carry.
- **Vig_stack auto-exit exemption (Apr 29 — Session 36):** Battle Scar #9 expanded. Vig_stack positions are exempt from ALL three auto-exit paths in `bot/main.py`'s position-loop: `edge_flipped` (already exempt pre-Session-36), `take_profit` (+50% pnl), and `cut_loss` (-30% pnl). Reason: 32% of vig_stack paper trades were exiting early at mean −$5 to −$10 across ALL families (median hold 23h — slow drift exits, not snap kills), driven by the cut_loss path firing on ~2.85¢ adverse moves at 95¢ NO-side entries. Floor gate is structural defense; the actual fix is exempting vig_stack from TP/SL since the edge is structural (YES sum > 100¢ on the ladder; only ladder-vig collapse should exit). Same session shipped `exit_reason` persistence on `paper_trades.json` so future audits distinguish auto_take_profit / auto_cut_loss / edge_flipped / manual paths. May 13 day-14 verification routine queued.
- **Filter F (Apr 18 → Apr 20):** `vig_stack` entries are whitelisted to stable families (`KXHIGHMIA`, `KXHIGHAUS`, `KXINX`) at 0.70+; volatile families require **NO ≥ 0.93** (raised from 0.90 on Apr 20 after bucket analysis showed only [92-96¢) is breakeven on volatile ladders).
- **Live-momentum disable list (Apr 20 → May 7):** `MOMENTUM_DISABLED_SPORTS = {atp_challenger, wta, wta_challenger}` (down from 4 sports — **ATP main tour re-enabled Apr 29 (Session 38a)** after Investigation #1's bucket re-run showed atp main-tour CFs at +11.32¢ avg CLV across n=56 settled with 82% +CLV (46/10) and 10 leader-loss settlements clearing the survivorship bar; the Apr 20 disable was based on challenger evidence, not main-tour evidence — same asymmetric-evidence pattern Session 30-followup flagged for wta_challenger). **Session 61 held both challenger circuits disabled** at the mature re-check: `atp_challenger` n=200 / avg CLV -0.62¢, `wta_challenger` n=198 / avg CLV -4.07¢. Held tennis-challenger positions still exit normally; new entries blocked. May 13 ATP main-tour day-14 re-validation routine queued.
- **STRATEGY_BUDGETS (Apr 16):** `vig_stack` 60%, `live_momentum` 20%, `arbs` 20% (fractions of equity). Prevents any one strategy from starving the others.
- **Disabled (data-driven kills):** weather single-market (17% WR), series_game_edge (26% WR), all crypto (`CRYPTO_ENABLED=False`), economic indicators, parlay edge. See `config.py:ACTIVE_STRATEGIES` for the current truth.
- **Apr 20 redemption plan: complete (Sessions 1–5).** Settlement-pipeline rebuild, active-strategy retuning, ESPN fetch restoration, scheduler hardening + drift warnings, state hygiene (live_ticks rotation + clv filter + lock heartbeat).
- **Apr 24–25 closed-loop data collection: complete (Sessions 6–11).** Per-decision audit log, counterfactual CLV records, stratified sampling across (gate, opp_type), live-momentum edge proxy + 30s heartbeat lock-touch, per-position MFE/MAE, gate-context enrichment, fair-value calibration loop. The bot now self-instruments — every accept and every reject carries a gate fingerprint and downstream outcome attribution.
- **Apr 25 pivot-enabling instrumentation arc: complete (Sessions 12–15.5).** Universe log (every active Kalshi market each scan, with `scanned_by` attribution — first snapshot found 53% of markets ignored by every active strategy), Strategy Protocol contract (pure-function strategies that take Market data in, return Opportunity dicts out), offline back-tester sharing the same `compute_clv_cents` function as live trading (no parallel codepath), hypothetical-variant report (parameter sweeps without going live), Kalshi history fallback for back-testing tickers we never traded, regime tagger (time_of_day / day_of_week / sport_phase / event_horizon_hr on every record), live-order microstructure capture (plumbing-only — verification deferred until `PAPER_MODE=False`), and a final hardening pass (Session 15.5) that closed silent-corruption gaps before the week-long unattended run.
- **Apr 26–27 strategy iteration arc: complete (Sessions 16–19).** Excursion gap-math fix surfacing the actual MFE-vs-exit signal (Session 16), tracker cadence audit + `_position_check_loop` (Session 17 — 90% of live_momentum positions had `ticks_observed=None` because main_loop's scan_interval was IDLE most of the time), `live_journal.json` analysis tool surfacing 3 actionable findings (Session 18), exit-logic replay tool that ruled out cheap trail-stop tuning AND removed 3 dead config constants (Session 18.5), and the **full Session 19 arc** — TickStrategy Protocol + behavior-preserving `live_momentum` port (19a, vindicated 8/8 paper-trade parity by 19a-followup), production peak-tracking bug fix (19a-peakfix — `bot/live_watcher.py:2225` was chicken-and-egg, peak_values was NEVER written, TRAILING_STOP physically couldn't fire; +14¢/trade conservative impact), tick-replay back-tester (19b), and a 15-variant entry × exit parameter sweep with 70/30 train/test discipline (19c) shipping `MOMENTUM_LEADER_MIN: 0.70 → 0.65` with an honest single-trade-dominance caveat. **Scheduled May 18 routine** auto-fires to re-validate on the larger sample (CONFIRM/REVERT/INCONCLUSIVE).
- **Apr 27–29 pre-checkpoint coverage arc (Sessions 21, 23, 24, 28, 29, 30, 33, 34, 35).** Continuation of strategy iteration with focus on data completeness for the May 2 retuning checkpoint. Session 21 instrumented `live_watcher` with `skip_reason` capture across 11 gates (closes Session 18 Finding #3's "watch-but-no-enter" black hole); Session 23 added `live_momentum` counterfactuals with stratified-by-(sport,skip_reason) sampling (~10×/day rate); Session 24 shipped weekly aggregator (later superseded by Session 35); Sessions 28 + 28-followup pushed `_SNAPSHOT_DEADLINE_SEC` 90→300 (Outcome D — at Kalshi API ceiling); Sessions 29 + 29-followup fixed JSONDecodeError-forever silent corruption in `_journal_append`; Session 30 shipped the `live_momentum` research dataset + bucket analysis (Session 30-followup-2 fixed an 84%-missing-CFs bug; final dataset 413→714 rows after regenerations); **Session 33** persisted DQS to live_ticks.jsonl rows so the bucket dimension is no longer empty; **Session 34** added `match_phase` as the 5th regime axis (set/round/over for tennis/UFC/IPL); **Session 35** shipped `tools/daily_report.py` + `tools/weekly_report.py` + `tools/_report_helpers.py` — automated daily 3 AM ET + weekly Sunday 6 AM ET reports under `bot/state/reports/{daily,weekly}/`, retired the original `weekly_digest.py`.
- **Apr 29 hygiene + ATP re-enable (Sessions 36, 36-followup, 37, 38a).** Session 36 vig_stack TP/SL exemption (above) + exit_reason persistence; Session 36-followup fixed two Session-34 backfill_regime test oversights; **Session 37** cleaned up 10 documented pre-existing test failures via group-paid-down approach (Group A `__new__()` bypass, Group B stale config pins, Group C silenced watchdog dead-code removal, Group D per-day-cap monkeypatch); Session 38a re-enabled ATP main tour.
- **Apr 30 stability + investigation arc (Sessions 38a-2, 39, 40, 41, 42).** **Session 38a-2** WTA main-tour disable re-evaluation — Outcome B doc-only (insufficient evidence to revert; held). **Session 39 (CRITICAL)** fixed asyncio event-loop blocking on `snapshot_universe` via `loop.run_in_executor` wrapping after a 12-hour bot wedge; defense-in-depth deadline check inside the retry loop. **Sessions 40 + 41 + 42** investigated live_momentum's W:L=0.261 EE rate across three exit-side framings (global TP/SL ratio, per-sport TP/SL variants, exit-balanced mix) — all Pattern C (statistically inconclusive); collectively rule out exit-side parameter tuning as the fix and surface sizing (Kelly cap on high-confidence-but-losing trades) as the strongest queued direction.
- **May 1 morning — discovery agent + first leads (Sessions 43a, 43-investigate, 43b).** Built an autonomous heuristic discovery agent (`tools/discovery_agent/`) — pure-Python, no API calls, daily 6 AM ET via launchd. **Session 43a** shipped the framework + 2 SFPHI-catching heuristics (outlier_pnl, cohort_emergence) with SFPHI regression test as P0 value-prop lock-in. First run produced 13 NEW findings including the SFPHI signal AND a vig_stack_futures cross-sport cohort lead. **Session 43-investigate** decomposed the cohort lead and confirmed SFPHI is a singleton (don't lean in), AND surfaced a separate real lead (`non_stable_below_weather_floor` rejecting +EV on sports futures — KXNBA-26-OKC at edge=+11.48¢ rejected on no_ask_prob=0.48 vs floor=0.93). **Session 43b** added 6 more heuristics (threshold_proximity, counterfactual_hotspots, universe_gap, live_tick_anomalies, cadence_outcome, log_error_spike) + cohort_emergence refinement. Day-2 run produced 3 HIGH counterfactual_hotspots findings: `no_leader/wta` +20¢ CLV (n=15) directly contradicting the Session 38a-2 WTA disable; `no_leader/nhl_game` +22¢ (n=19); `no_leader/atp` +9¢ corroborating Session 38a re-enable.
- **May 1 afternoon — discipline cycle + agent self-improvement + first P&L intervention (Sessions 44, 45, 45-correction, 46, 47, 48, 49).** Six straight sessions reinforcing the discipline + expanding the search frontier + finally acting on the leak: **Session 44** gate-flow walk disambiguated the WTA `no_leader` +20¢ finding (it's leader-detection signal, not sport-disable signal — re-enabling WTA wouldn't change those CFs). **Session 45 + 46** both shipped Outcome C HOLD on counterfactual_hotspots-surfaced retuning candidates that turned out to be cross-cohort cherry-picks. **Session 45 post-session correction** caught a verification-query schema bug (`market_result == 'no_won'` should have been `== 'no'`) and added a "**Canonical Data Schema Reference**" section pinned at the top of CLAUDE.md (mirrored to `tools/discovery_agent/README.md`) — single source of truth for `clv.json` / `decisions.jsonl` / `paper_trades.json` field names + value enums. **Session 47** refined `counterfactual_hotspots` with cross-cohort context + severity demotion ladder so future Sessions 45/46-shape cherry-picks auto-demote to INFO. **Session 48** added `concurrent_attack_angles` as the 9th discovery heuristic — surfaces NEW attack angles by analyzing the same events the bot already trades (multi-strategy-per-event candidates derived from data we already collect). **Session 49** shipped the first production-code intervention of the day: per-sport `size_multiplier` on live_momentum, sizing **NBA + UFC down to 0.5×** based on measured per-sport bleed (NBA −$26.57 / n=21, UFC −$8.30 / n=8). NHL + MLB explicitly held at 1.0× (n too thin to size up); IPL deferred (Session 38b queued). May 15 +14d re-validation routine scheduled.
- **May 1 → May 2 transition — pre-let-it-run observability + lab arc (Sessions 50, 51).** Two sessions closing the "let the bot run for 14 days and learn" loop. **Session 50** shipped forward-only `confidence` + `dqs` + `sport` persistence on live_momentum `paper_trades.json` records (the 3 dimensions were missing — we couldn't bucket entries by signal-strength to validate Session 40's "high-confidence-but-losing" hypothesis OR confirm Session 49 sized down the right cohort). Composite confidence formula `min(1.0, dqs * (1 + max(0, wp_edge)))`. Vig_stack path byte-identical post-restart (verified). Bot restarted via launchd; Battle Scar #3 caught its 3rd orphan today; PID 82747 fresh. **Session 51** shipped `tools/strategy_lab/` v1 — rapid hypothesis prototyping for new strategies. Drop a 20-line candidate file in `tools/strategy_lab/candidates/`, run `python3 -m tools.strategy_lab.driver --candidate <name> --days 14`, get a markdown report with hypothetical P&L + per-sport breakdown + reason histogram + top winners/losers in seconds. Days/weeks → seconds for the "is this idea worth pursuing?" decision. Schema discipline now self-enforcing in TWO places (CLAUDE.md doc + commit-time `test_canonical_schema_used_throughout` guard). **End-to-end "find new attack angles" loop closed:** discovery agent surfaces candidates → strategy lab validates them in seconds → real production scanner ships if validated → that scanner becomes another input to the next day's agent run. Compounding strategy expansion.
- **May 3 — Telegram rate-limit incident + hardening (Session 52).** Apr 28→May 3 the bot's `editMessageText` volume (9,357/24h) tripped Telegram's anti-abuse system; the old notifier silently dropped messages on 429s, leaving Tyler with zero visibility for 16+ hours while trading continued. Session 52 shipped a structural fix: shared retry/backoff wrapper, per-chat edit throttle (1/sec sustained, burst 5), message-id-keyed SHA1 dedup, durable `bot_state.json:telegram_*` state surfacing. **Battle Scar #15** codified: "Telegram 429s are state, not noise — do NOT restart Glint while cooling down" (each restart re-hits Telegram and extends the outage). **Session 71 retired the operational rule** — the notifier now reads `telegram_throttled_until` on init and restores `_flood_until`, so restarts during cooldown are safe. Plus two May 3 follow-ups: lock-empty race fix in `_release_lock` (PID-guard before unlinking) + 21 explicit `!tools/<file>.py` exceptions in `.gitignore` for load-bearing tools.
- **May 4 — Per-family vig_stack max position cap (Session 53).** Session 52 audit revealed KXINX vig_stack flipped EV-negative at the post-Apr-29 balance bump — same 78% WR, but position size jumped 6.7× (qty 14 → qty 235) and one ladder collapse = full $200 loss. Math: `0.78 × $25 win − 0.22 × $200 loss = −$24.50/trade` at max-cap sizing. Shipped `VIG_STACK_FAMILY_MAX_POSITION_DOLLARS` dict in `bot/config.py`: KXINX/KXMLBGAME $50, KXHIGHCHI/DEN/NY $150, KXHIGHAUS/MIA $200. Architectural mirror of Session 49's per-sport `size_multiplier`. Verification gate (May 14 day-14): KXINX P&L slope should trend toward break-even from current −$22.94/trade slope. **Open caveat:** pre-Session-53 oversize legacy positions ride to settlement at original size — May 6 saw the first KXMLBGAME legacy collapse (−$199.88) and surfaced an unexpected KXHIGHMIA −$199.95 loss in a family Session 53 categorized as "healthy" at $200; worth investigating before day-14.
- **May 5 — live_watcher correctness pass (Session 54).** Codex review surfaced three production `bot/live_watcher.py` bugs, all live_momentum-side: (1) paper sizing reconstructed bankroll as `$500 + paper_pnl` instead of `PAPER_STARTING_BALANCE + paper_pnl`, leaving live_momentum sizing at ~9% of intended Kelly since the Apr 29 balance bump; (2) `_paper_record_exit` on PAPER EXIT path dropped the `reason` kwarg, breaking Session 36's forward-only `exit_reason` contract; (3) two telemetry sites (`live_watcher.py:1819, 2564`) treated `ScoreSnapshot` dataclasses like dicts via `.get("period")` — silently swallowed by an outer try/except. All three fixes shipped with regression tests; bot restarted clean on PID 93948→93967. **Battle Scar #16** codified: "live_momentum sizing must use the configured paper bankroll, not historical constants." **Important consequence:** Session 19c (`MOMENTUM_LEADER_MIN: 0.65`) and Session 49 per-sport sizing evidence is now provisional pending 14d of corrected post-Session-54 data — the May 15 / May 18 routines are the first true test.
- **May 6 — Glint Analyst proposal → 4-AI panel review → ship 10th discovery heuristic instead (Session 55).** Drafted "Glint Analyst" (autonomous LLM analyst running daily, reading state + curated CLAUDE.md, writing draft session prompts). 4-AI panel review (ChatGPT, Grok, Claude.ai) surfaced four concerns: (1) **founding example was config-comparison-shaped not prose-shaped** — the KXHIGHMIA finding I anchored on lives in `bot/config.py` as a dict, no LLM needed; (2) timing wrong (5 days into the 14-day let-it-run window from Session 51); (3) cost estimate wrong; (4) anchoring failure mode (LLM reading recent CLAUDE.md prose every morning subtly biases which sessions get surfaced). Updated my position based on the critique. Shipped `tools/discovery_agent/heuristics/settlement_vs_rationale.py` instead — 10th deterministic heuristic with 3 patterns (`tail_loss_in_high_cap_family`, `disabled_sport_settlement`, `outsized_notional_post_size_multiplier`) + Session 47-style cross-cohort demotion ladder. KXHIGHMIA founding regression locked as P0. First real-data run surfaced 3 actionable categories: KXHIGHMIA at n=4 tail losses (not n=1, "pattern not outlier"), bonus KXHIGHAUS −$200 same shape, and **9 post-disable entries on `atp_challenger`/`wta`/`wta_challenger`** — disable-list regression candidate (queued as Session 57). Glint Analyst v0 brief stays deferred with explicit re-open trigger documented.
- **May 6 — Disable sports_arb + clean LIVE_TRAILING_STOP (Session 56).** Codex review surfaced two correctness issues: (1) `sports_monotonicity_arb` and `sports_consistency_arb` opportunity dicts carry `arb_pair` metadata describing two legs, but `bot/main.py:900` → `executor.execute_trade()` reads only the single-ticker `recommended_side` field. Verified by code walk: `grep arb_pair bot/main.py bot/executor.py` returns zero hits. Every "MONOTONICITY ARB: guaranteed profit" would have fired as a $200-Kelly one-sided directional bet labeled `confidence=0.95`. Mitigating fact: 0 historical fills (we got lucky); non-mitigating: live in `ACTIVE_STRATEGIES` and would fire on first real Kalshi violation. (2) Stale `LIVE_TRAILING_STOP` reference in `live_watcher.py` from a constant deleted in Session 18.5 — flagged in Session 19a's pre-flight grep but never cleaned up. **Shipped:** Both arb strategies disabled via 4-layer defense (removed from `ACTIVE_STRATEGIES` + scanner early-returns + CLAUDE.md table update + property test asserting nothing reaches executor). Dead `_trailing_active` infrastructure deleted entirely (~6 LOC). Bot restart deferred per Battle Scar #15 — Telegram throttle was active until 03:12 UTC May 7. Watch-list: rebuild paired execution (Option A) only when arb opportunity volume justifies the new code surface.
- **May 6/7 — SFPHI regression test date-drift fix (Session 56.5).** P0 baseline restoration. Session 43a's SFPHI regression test (the founding-example lock-in for the entire discovery_agent framework) had failed for 3 consecutive sessions (54→55→56) as "pre-existing date-drift, not caused by this session." Per Session 37 precedent, that's exactly how documented-baseline-failures grow from 2→10 over months. Stopped the drift while the fix was small. Diagnosis confirmed: `_seed_sfphi_fixture` had hardcoded ISO timestamps that fell outside `cohort_emergence`'s lookback window as wall-clock advanced. Fix: anchored fixture timestamps to `dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=N)`, mirroring the fixture's existing baseline-trade discipline. Added safeguard test (`test_sfphi_fixture_dates_are_relative`) locking commit-time discipline against future re-drift. **Test baseline restored to 1,410 / 0 failed.** P0 SFPHI contract back in force.
- **May 7 — Disabled-sport "leak" was heuristic precision drift, not a bot bug (Session 57).** First coder session triggered by a Session 55 heuristic finding (six days post-deploy — validates the cross-AI panel decision to ship deterministic heuristic over LLM analyst). Session 55's `settlement_vs_rationale` Pattern 2 flagged 9 post-Apr-20 `live_momentum` entries on `atp_challenger`/`wta`/`wta_challenger` as critical regressions. Phase 0 forensic investigation flipped the diagnosis from "bot's gate is leaking" to "heuristic's date filter is too loose": all 9 entries fired between 2026-04-20T02:24Z and 2026-04-20T20:12Z, but `git blame bot/config.py:172` shows the disable commit `b1f08ff` was authored at 2026-04-20 22:31:54 -0400 = **2026-04-21 02:31:54 UTC** — every leaked entry pre-dates the commit by 6+ hours. They're pre-deploy artifacts; the bot's gate at `bot/live_watcher.py:1158` has worked correctly since the deploy (zero post-Apr-21 entries on disabled tennis sports across 16 days; today's `cohort_emergence` finding shows 9 attempts / **0 accepts / 0 trades**). Same shape as Session 56.5: heuristic's date math drifted from production's actual deploy timeline. Fix: tightened `MOMENTUM_DISABLED_SINCE` cutoffs in `tools/discovery_agent/heuristics/settlement_vs_rationale.py` from calendar-midnight (`2026-04-20T00:00:00Z`) to commit timestamp (`2026-04-21T02:31:54Z`); 2 regression tests pin the deploy-window-race exclusion + post-commit-still-fires bookend. Real-data re-run: Pattern 2 went **9 → 0 findings**; Pattern 1 unchanged (KXHIGHMIA + KXHIGHAUS still firing as real findings). 9 historical entries (net P&L −$12.80) documented as pre-fix; not backfilled per forward-only precedent. No bot code change. No restart. Battle Scar candidate (heuristic calendar-vs-commit-timestamp drift) considered but declined — needs a third instance to promote past discipline-note status.
- **May 7 — Notifier `HTTPXRequest is not initialized` failures: NOT a startup race; watcher asyncio tasks tick on dead notifier post-stop (Session 58).** Brief framed the recurring `RuntimeError('HTTPXRequest is not initialized!')` errors as a startup race. Phase-0 diagnostics flipped the diagnosis: 234 errors AND 105 successful edits in the same 5-min buckets post-restart, two specific ATP watchers fail every tick while 4-5 others succeed concurrently through the same notifier — bug is structural, not a startup window. Investigation locked **Diagnosis D5**: `GlintBot.stop()` doesn't cancel `self._active_watchers`, so watcher tasks (spawned via `_live_scan_loop`/`handle_watch` as standalone `asyncio.create_task`, NOT in the `gather()`'d task list) keep running their `while self.active: await self._tick_momentum(); await asyncio.sleep(LIVE_POLL_INTERVAL)` loops on a notifier whose `HTTPXRequest` was just shut down by `await self.app.shutdown()`. Each tick fails → Session 52 retry-with-backoff retries 3× and gives up → next tick same cycle. The OLD process's `asyncio.run()` finally GCs the orphaned tasks ~16 minutes later. Session 52's hardening masked the lifetime bug as warning spam instead of a crash that would have surfaced via `_run_watcher`'s exception handler. **Fix:** `GlintBot.stop()` extended at [bot/main.py:397-435](hustle-agent/bot/main.py:397) — iterate `_active_watchers`, call `watcher.stop()` + `task.cancel()` on every entry, await a 5s `asyncio.wait_for` grace period, **then** call `notifier.stop()`. Cancel-then-shutdown order is load-bearing; reversed, in-flight ticks fire HTTPXRequest errors during the unwind. Mirrors `handle_unwatch`'s pattern. **Acceptance gate verified live (two restart cycles):** pre-fix 121 errors in 2-min post-restart window → with-fix **2 errors** (98.3% reduction); zero errors after 10s past stop boundary. The 2 residual errors are from `send_message` (not `edit_message_text`) — bounded smaller race for in-flight `_live_scan_loop` announce messages mid-await; out of scope. **Battle Scar #17** codified: "Watcher asyncio tasks must be cancelled before notifier teardown." 5 regression tests pin the cancel-order-empty-exception-timeout discipline. Operating Posture observation: self-healing infrastructure (Session 52 retry hardening) can mask underlying lifetime bugs that would otherwise surface as crashes — when adding hardening, also add a regression test for the underlying root cause, not just the symptom.
- **May 7 — `_stopping` flag short-circuits notifier-shutdown retry log spam (Session 58.5, ~45min, defense-in-depth).** Closes the 2 residual `HTTPXRequest is not initialized` errors per restart that Session 58 left out of scope. The residuals are a different race: `_live_scan_loop`'s `"Auto-scan: started N new watcher(s)"` `send_message` is mid-await when SIGTERM lands, the notifier teardown shuts down the HTTPXRequest while the in-flight POST is still awaiting, Session 52's retry-with-backoff retries 3× on the dead HTTPXRequest, generating warning+error chain. **Fix:** 3-step `_stopping` flag on `TelegramNotifier`. (1) `__init__` declares `self._stopping = False`. (2) `stop()` sets `self._stopping = True` BEFORE `app.updater.stop()` / `app.stop()` / `app.shutdown()` — order load-bearing, flag must land before HTTPXRequest teardown. (3) `_telegram_call`'s `except Exception` block, BEFORE branching on `_is_flood_error`/`isinstance(e, (NetworkError, TimedOut))`: `if self._stopping: log INFO + return None`. Catches whatever exception flavor surfaces (raw RuntimeError, PTB-wrapped NetworkError, httpx.ReadError) without gating on a specific class. ~15 LOC across 3 sites. **Acceptance gate live:** post-restart `POST_RESTART_HTTPX_ERRORS = 0` (was 2 pre-fix), `POST_RESTART_SHUTDOWN_INFO = 0` — the in-flight race didn't fire this restart cycle (timing-sensitive), but unit tests prove the flag mechanism mechanically. **Defense-in-depth value (the structural win):** if Battle Scar #17 ever regresses (Session 58's `GlintBot.stop()` watcher-cancel ordering reverted), the `_stopping` flag still catches the symptom and surfaces ONE INFO line per restart instead of 121 warnings — making future regressions MORE visible (single focused signal), not less. **No new Battle Scar; refines #17.** Methodological note: first acceptance-gate run reported 6 errors via lexicographic-comparison artifact (untimestamped traceback fragments lex-greater than `[`-prefixed marker); fixed by anchoring awk regex to `^\[YYYY-MM-DD HH:MM`. 2 regression tests pin the short-circuit + Session-52-retry-preservation contract.
- **May 7 — Planner-tuned status consolidator (Session 59).** Ships `tools/glint_status.py`, a read-only on-demand CLI for the planner workflow: one command reads existing daily/weekly/discovery reports, state files, `CLAUDE.md`, `REPORT_CALENDAR.md`, and `bot/config.py`, then emits a 9-section snapshot with verdict, diff since last check, stale-aware health pulse, current P&L, open-position family/cap table, discovery summary, anomalies, watch-list trigger status, calendar, and flags. It writes `bot/state/glint_status_last.json` for next-run diff plus `bot/state/glint_status_YYYY-MM-DD.md` as the new canonical "what's happening right now" desktop/planner artifact, replacing the previous 3-6 ad-hoc Python invocations for every status check. Daily/weekly/discovery reports stay unchanged; this is an additive consolidation layer. First real run parsed 27 CLAUDE watch-list trigger blocks, auto-surfaced the Session 30-followup challenger-CF trigger as TRIGGERED, and marked the latest daily report stale. No P&L change, no bot restart, no Telegram push, no scheduled run. 10 regression tests pin diff math, anomaly conservatism, watch-list parser/evaluator fallback, section extraction, atomic persistence, and <2s real-state runtime.
- **May 7 — Daily-report cron + paper-ledger consistency repair (Session 60).** Second coder session triggered by a `glint_status.py` finding. Phase A diagnosed the stale daily-report source as D4: `tools/daily_report.py` still rendered current production state cleanly, but there was no daily-report launchd plist, loaded service, disabled-service entry, or crontab. Installed and enabled `com.hustle-agent.daily-report` as a user LaunchAgent at 03:00 ET and generated today's forward-only `daily_report_2026-05-07.md` (May 4-6 intentionally not backfilled). Phase B diagnosed the 14-vs-15 `positions.json` / `paper_trades.json` mismatch as a stale unfilled paper order (`PAPER-BAEB1FC9`) that was `trade_history.status=resting` / `filled=0` but `paper_trades.status=open`. Paper ledger lifecycle now mirrors position lifecycle: unfilled paper orders are `resting`, fill promotion makes them `open`, and stale settled-market cancellation closes both `open` and `resting` rows. One-shot ignored-state cleanup moved only the orphan to `cancelled_stale`. `glint_status.py` now has no `daily_report_stale` or `positions_paper_open_mismatch` flags. No P&L change.
- **May 7 — Challenger CF re-evaluation + live_momentum zero-entry diagnosis (Session 61).** Third coder session triggered by `glint_status.py` findings. Phase A consumed the Session 30-followup challenger watch-list trigger on a freshly regenerated 14d dataset (1,675 rows; combined challenger settled CFs n=398 / leader-loss=122) and held both challenger circuits disabled: `atp_challenger` n=200 / avg CLV -0.62¢ despite 70% +CLV, `wta_challenger` n=198 / avg CLV -4.07¢. Phase B diagnosed `live_momentum_zero_entries_48h` as passive/no-entry-signal rather than a runtime bug: trailing 48h had 55 watcher spawns, but enabled NBA/NHL/ATP/IPL watchers never reached `execute_attempt`, while the logged inner-loop rejects were disabled tennis (`sport_disabled`). Sizing is far above the $1 floor at the current $11,079.73 reconstructed balance. No code/config change, no restart, no P&L change; `glint_status.py` still surfaces the WARN until a forward live_momentum entry lands.
- **May 7 — KXMLBGAME family-cap breach fixed at the futures sizing gate (Session 62).** Fourth coder session triggered by `glint_status.py` findings in 24h: it surfaced an OPEN KXMLBGAME position at **$199.68 > $50 cap** before May 8 settlement, plus a second resting over-cap KXMLBGAME order at **$199.76**. Phase 0 diagnosed D6: per-game MLB KXMLBGAME opportunities were tagged `vig_stack_futures`, but Session 53's `_handle_opportunity()` family extraction covered only `vig_stack_no`/`vig_stack_series`, so `kelly_size()` received `family=None` and fell back to the legacy $200 cap. Fix: added a sizing-only vig_stack tuple including `vig_stack_futures` for NO-perspective probability + family-cap lookup, while leaving TP/SL/edge-flip exit exemptions unchanged. Tyler chose to let the already-open position ride; the resting over-cap order is operator-owned for manual cancellation, not code-mutated. P&L unchanged by the forward fix; restart deferred until Telegram cooldown clears at 2026-05-08T03:12:48Z. Follow-up: the Session 43-investigate SFPHI singleton framing may be stale because this is now a recurring KXMLBGAME-as-futures mechanism; scanner classification remains out of scope here.
- **May 7 — KXMLBGAME scanner classification fixed at the source (Session 63).** Structural half of the Session 62/63 paired arc. Phase 0 found the bad label was not current `VigStackSeries.name_for()` behavior; it was `bot/scanner_sports_arb.py:scan_game_vig()` hard-coding per-game `KXMLBGAME-*` attribution and opportunities as `vig_stack_futures`. Fix: explicit per-game prefixes (`KXMLBGAME-`, `KXNBAGAME-`, `KXNHLGAME-`) now emit `vig_stack_series`, while true championship futures keep `vig_stack_futures`. Session 62's sizing-only tuple extension remains as defense-in-depth. Downstream analytical layers (`decisions.jsonl`, CLV, discovery cohorts, strategy lab, reports) get clean post-fix labels; historical rows are not backfilled.
- **May 7 — WTA per-sport `MOMENTUM_LEADER_MIN` re-evaluation (Session 64).** SIXTH coder session triggered by `glint_status.py` findings; FIRST that surfaced a positive-CLV signal as a candidate for production change rather than a regression-fix. Phase 0 evidence: no_leader/wta sub-cohort PASSES survivorship + EV (n=35, mean +9.34¢, 11 leader-loss settlements). Cross-cohort context (all-sport no_leader aggregate -6.15¢) confirms WTA edge is NOT cherry-pick. But threshold sensitivity is non-monotonic (peak +12.05¢ at 0.60, dip -0.80¢ at 0.58, n_no_won=5 at peak), preventing clean Pattern A value choice. **Pattern B chosen:** ship per-sport override infrastructure (`MOMENTUM_LEADER_MIN_PER_SPORT` dict + `get_leader_min_for_sport` helper in `bot/config.py`, used at `bot/live_watcher.py:scan_live_matches` outer gate AND `bot/live_watcher.py:_tick_momentum` inner gate), document the WTA candidate value (0.55), but keep WTA in `MOMENTUM_DISABLED_SPORTS`. Net behavioral change zero. Watch-list bar raised to n>=80 / mean>=+5¢. Future activation cost: ~5 lines. Architectural mirror of Session 49's `kelly_size(sport=...)` pattern — top-level dict, NOT inside `SPORT_PROFILES["tennis"]` (which aliases to atp/atp_challenger/wta_challenger).
- **May 7 — Housekeeping bundle: 3 small fixes, no production code (Session 65).** Mirror of Session 56.5's housekeeping shape — small, focused, low-risk, restores baseline cleanliness. **Phase A:** raised `tools/glint_status.py` Session 30-followup challenger-CF threshold from `n>=30 AND losses>=5` (met long ago at Session 61's n=398 / leader-loss=122 baseline, evaluated Outcome B) to `n>=600 AND losses>=100`. Boy-who-cried-wolf shape: an already-evaluated TRIGGERED line every glint run risked obscuring genuinely-new triggers. CLAUDE.md L2153 prose updated to lead with the new bar + document Session 61 as historical context. Same architectural pattern as Session 64's no_leader/wta bar raise. **Phase B:** glint_status.py verdict NEW count now derived from `new_fingerprints` (same source as §6 body) rather than the `Findings: N NEW` summary regex. Prevents future verdict-vs-body divergence when the discovery report's summary line and NEW-list section disagree internally (the failure mode that motivated this fix was a "Verdict: 2 NEW" line while the body said "none this run"). **Phase C:** removed untracked `bot/tools/` legacy directory (empty `__init__.py` + standalone `clv_by_strategy.py` from Apr 16, stdlib-only imports, zero callers anywhere across `bot/`, `tools/`, `tests/`). Untracked since before this session series; every recent ship report explicitly noted "pre-existing untracked `bot/tools/` remains untouched." Long-pending cleanup finally closed per Session 52's gitignore-audit discipline. No bot restart, no `bot/` runtime code touched, no P&L change.
- **May 7 — Strategy Candidates safety net in reports (Session 66).** Discovery-agent strategy candidates no longer live only in buried `bot/state/discovery/discovery_findings_*.jsonl`. `tools/_report_helpers.py` now renders a shared 14d Strategy Candidates body from the seven strategy-relevant heuristics (concurrent attack angles, cohort emergence, counterfactual hotspots, threshold proximity, outlier P&L, settlement-vs-rationale, universe gap), excluding operational-only heuristics. It tracks first/last/latest severity per fingerprint, inclusive days stable, resolved-when-absent-from-latest-run, no truncation, severity sort, and CLAUDE watch-list annotations. `tools/glint_status.py` adds §10 plus verdict/diff candidate counts; `tools/daily_report.py` adds a `_safe_section`-wrapped Strategy Candidates section with byte-identical body for the same window. Today's live output: 21 active (H 3 / N 5 / I 13), 29 resolved 14d.
- **May 7 — `edge_below_threshold/nhl_futures` Phase-1 → Outcome C HOLD doc-only (Session 68).** THIRD Session 66 §10-driven Phase-1. The §10 finding read striking on its face (n=14 mean CLV +6.4¢ at 100% +CLV, 4d STABLE). **Phase-0 survivorship gate PASSED decisively** (n_no=14 leader-side losses, n_yes=0 — opposite of survivorship, genuine leader-side validation). Phase-1 disqualified the lever on three independent grounds: (a) cohort collapses to 3 unique tickers (TB×8, EDM×4, DAL×2), n_effective ~3 vs Session 38a's n=56 bar; (b) **threshold sweep is structurally FLAT across all positive thresholds** — every CF has NEGATIVE computed `edge_at_trade` in [-0.0051, -0.0022], so sweeping `min_relative_edge` across {0.005, 0.010, 0.015, 0.020 (current), 0.025, 0.030} admits 0/14 in every case; only NEGATIVE thresholds admit, which violates vig_stack's structural-edge invariant; (c) historical realized vacuous (0 KXNHL-* trades in `paper_trades.json`). **Mechanism is fair-value-model error, not gate-threshold tightness:** all 14 CFs had `fair_value_cents` in [92.52, 94.69] vs `closing_yes_price=0` → actual fair was effectively 100¢ NO; model under-priced reality by 5-7¢. The lever to capture this signal is the fair-value calculation in `bot/math_engine.py` (Session 69+ candidate), NOT `VIG_STACK_MIN_EDGE`. Outcomes A and B both disqualified because the threshold lever cannot capture the signal at any structurally-valid setting; Outcome D N/A (data shape is the blocker, not tooling). Pattern C ship preserves the search frontier (signal IS real, just not on this lever) without false-positive activation. Watch-list trigger raised: re-investigate when n>=30 across >=10 distinct KXNHL-* tickers AND fair-value model investigated AND sweep produces monotonic positive-side admission curve. Mirror of Sessions 18.5/19a/41/56.5/58/67 "verify before locking scope" discipline. Doc-only: no production code change, no tests added, no bot restart.
- **May 7 — settlement_vs_rationale family-tail counter refinement + no_price_below_floor Phase-1 → Outcome B per-family floor override (Session 67).** Two deliverables bundled because they share infrastructure and the heuristic refinement informs how the agent surfaces vig_stack-side findings going forward. **Deliverable 1:** Pattern 1's family-aggregate counter (`family_recent_tail_losses`) was inflating with EE-noise — small positions that lost 100% of tiny notional but were never near the family cap. KXHIGHMIA Phase 0a forensic: 4 tail losses by pure -95%-of-notional filter, but only **1 TRUE at-cap tail loss**; the other 3 were $14.11 / $18.40 / $10.08 notional EE-noise. Per-finding trigger at `_check_pattern1` was already at-cap-correct; the family-aggregate counter at line 195 used the at-cap-blind `_is_pattern1_tail` helper. Fix: introduced `_is_pattern1_tail_at_cap(trade, family_cap)` and swapped at the leak site. Real-data verification: KXHIGHMIA family_recent_tail_losses **4→1**, KXHIGHAUS **3→1**, both findings now correctly demote HIGH→NOTABLE per Session 47 ladder (cascading consequence: pre-fix demotion clause `n_tail==1` failed, severity stayed HIGH; post-fix it fires, severity is NOTABLE). `glint_status.py §10` confirms post-fix: HIGH 3→1, NOTABLE 5→10. **Deliverable 2:** Session 66 §10 surfaced `no_price_below_floor/None: 65 settled CFs, mean +12.5¢ at 69% +CLV` as a 7d-stable NOTABLE — Phase 0b discovery: the 65 CFs are **100% stable-family** (KXHIGHMIA + KXHIGHAUS + KXINX), measuring the **stable-family 0.70 floor**, NOT the volatile 0.93 floor the brief framed (gate-name `no_price_below_floor` is the stable branch at `bot/strategies/vig_stack_series.py:603`; volatile is `non_stable_below_weather_floor` at line 620). Brief out-of-scope excluded stable-family floor; user expanded scope via AskUserQuestion. **Phase 1 evidence on stable cohort:** survivorship n_no=45 PASS; per-cohort EV KXHIGHMIA +18.56¢ / KXHIGHAUS +14.69¢ / KXINX -0.67¢ FAIL; cross-family raw +12.46¢ / trimmed +14.44¢; threshold sweep monotonic-decreasing on combined; historical realized May 1+ KXHIGHMIA +$11.75/trade, KXHIGHAUS +$24.12/trade, KXINX -$48.51/trade. Signal real but ISOLATED to KXHIGHMIA + KXHIGHAUS. **Outcome B (mirror Session 64 Pattern B):** shipped `VIG_STACK_FAMILY_FLOOR_OVERRIDES: dict[str, float] = {}` + `get_vig_stack_floor_for_family(family, default)` helper in `bot/config.py`; wired into both gate branches at `bot/strategies/vig_stack_series.py`. Production dict empty → net behavioral change zero. Future activation cost: ~5 lines (uncomment + schedule +14d re-validation). 23 existing strategy golden-file tests still pass — byte-identical behavior locked. Bot restart deferred per Battle Scar #15 (Telegram cool-down active until 2026-05-08T03:12:48Z); zero behavioral impact regardless. Operating Posture: FIRST session triggered by §10 Strategy Candidates → Phase-0 scope-mismatch discovery (gate-name vs floor-value) → Phase-1 against re-scoped cohort → Pattern B ship — exemplifies "verify before locking scope" mirroring Sessions 18.5/19a/41/56.5/58.
- **May 7 — `vig_stack_series.evaluate` fair-value formula calibration → Outcome C HOLD doc-only (Session 69).** FOURTH consolidator/§10-driven Phase-1; direct deferral target from Session 68's "fair-value model is the actual lever, not the threshold." Phase 0a caught a **file misidentification**: the prompt named `bot/math_engine.calculate_vig_stack` but that's parlay math (used by `scanner_sports.py:265` only — `true_yes = product(leg_probabilities)` for combining independent legs into one Kalshi parlay market). The actual fair-value formula for both `vig_stack_series` + `vig_stack_futures` lives at `bot/strategies/vig_stack_series.py:691-697` (math_chain) backed by computation at lines 549-550 — a SINGLE UNIFIED formula across all three families (weather B-buckets, index ranges, sports futures); branch at line 373-374 only switches the `opp_type` label. **Implication:** any change to this formula applies UNIFORMLY across weather + index + futures by construction. Phase 0c confirmed Hypothesis H1 — proportional vig redistribution (`yes_fair = yes_ask / vig_factor; no_fair = 100 - yes_fair`) systematically under-states fair_no for mid-band teams (`yes_ask` ≈ 5-15¢ range) when bookmakers concentrate vig on favorites. Phase 1 cross-cohort + weather: NHL n=14 / 3 unique tickers (TB×8, EDM×4, DAL×2) / +6.36¢, NBA n=0, MLB n=0; weather 3 of 6 families (KXHIGHMIA +5.11¢/n=38, KXHIGHCHI +3.78¢/n=54, KXHIGHNY +4.04¢/n=27) show the SAME bias pattern. **Three Outcome-C triggers fire simultaneously:** (a) Phase-1 single-sport cherry-pick (NHL only; NBA + MLB EMPTY); (b) weather regression FALSIFIED (3 of 6 families show same bias → formula change has 6× blast radius across n=249 weather CFs); (c) n_effective < 30 on cross-cohort (NHL n_effective ≈ 3 unique market events). Mechanism IS cleanly identifiable — the "Phase-0 mechanism unclear" trigger does NOT fire — but the other three fire decisively. Outcome A (universal recalibration) rejected: cannot validate net impact across unified weather + futures cohort without back-tester infrastructure (Session 13b's `tools/backtest.py` doesn't currently support vig_stack_futures or fair-value sweeps). Outcome B (per-market-type override) rejected: bias is NOT isolated to one type — weather + futures both show it, so picking per-family bias values with only 1 sport's meaningful data is over-fitting. Outcome D (defer for infrastructure) ranks below C because the underlying signal IS real; C documents the finding and sets a watch-list, D would silently lose the work. Watch-list trigger: re-evaluate when ALL of (i) NBA OR MLB futures n>=20 settled CFs at this gate, (ii) NHL n>=30 across n_unique>=10 (current 14/3), (iii) `tools/backtest.py` extends to support vig_stack_futures fair-value sweeps OR `VIG_STACK_FAIR_VALUE_BIAS_OVERRIDES: dict[str, float]` per-family override surface ships first as Pattern B null-op (Session 67 architectural precedent — `VIG_STACK_FAMILY_FLOOR_OVERRIDES` shipped that exact shape). Realistic earliest re-evaluation Q3 2026 (calendar-driven on (i) + (ii); infra-driven on (iii) — back-tester extension or override architecture each ~3-5h coder session). Mirror of Session 67's "gate-name vs floor-value scope mismatch" + Pattern C discipline of Sessions 18.5/40/41/45/46/68 — verify before locking scope. Doc-only: no production code change, no test change, no bot restart. CLAUDE.md is not loaded by `bot/main.py`; Telegram throttle (`telegram_throttled_until = 2026-05-08T03:12:48Z`) irrelevant since no restart needed. Note for future Session 69-followup: if Outcome A or B ever ships, that session must add `KXNHL-`/`KXNBA-`/`KXMLB-` entries to `VIG_STACK_FAMILY_MAX_POSITION_DOLLARS` at the Session 53 untested-family $50 tier per the prompt's Family-cap regression rail (those families currently produce 0 trades, so no caps are needed yet).
- **May 7 — `test_positions_paper_trades_consistency.py` race fix + "all tests always pass" discipline rule (Session 70).** Closes the Session 37 anti-pattern (10 baseline failures cleaned up at once because each session waved off "pre-existing"). Sessions 68 + 69 each documented "1 pre-existing live-state race" without naming the test or diagnosing the mechanism. Phase 0: race surface is exactly **one file** (`grep -rln "REPO_ROOT.*bot/state\|read_text.*bot/state\|open.*bot/state\|json.load.*bot/state" tests/` returns only `tests/test_positions_paper_trades_consistency.py`). Mechanism: each individual file is read-atomic (bot writes via `bot/state_io.py` tmpfile + rename), but the cross-file write sequence (positions.json → paper_trades.json) is NOT atomic — two reads microseconds apart land on either side of mid-write boundaries and see inconsistent snapshots. **Deliverable 1:** `_read_stable_snapshot(positions_path, trades_path, max_attempts=20, settle_delay_secs=0.05)` mtime-fence helper — pre-stat both files, read both, post-stat both, retry if either mtime changed; raises `RuntimeError` with diagnostic after max_attempts. Bounded 1s total in default config; with 30s scan cadence finding a stable window in 1s is virtually guaranteed. Single existing test `test_paper_trades_open_count_matches_positions_active` keeps its assertion logic byte-identical — only the file-read mechanism changes. Mtime-fence chosen over skip-when-bot-alive because Tyler's discipline is "tests must run AND pass" — skip semantics surrender to env dependency. Architectural multi-file atomicity in `bot/state_io.py` is the source-fix but out of scope (much bigger touch surface; mtime-fence achieves test correctness without production code change). **Deliverable 1.5:** 3 regression tests using `tmp_path` + deterministic `Path.stat` mocking (initial threading + `write_text` churners surfaced their own bugs — `write_text` is truncate-then-write so reads see empty files / `JSONDecodeError`, and timing-based churners can luck into stable windows; redesigned with `os.utime` semantics and `Path.stat` mock-injection so behavior is bounded by call counts, not wall-clock). Tests: succeeds on quiet files (first-attempt return), retries when mtime mismatches in early attempts then succeeds, raises cleanly with "stable snapshot" + attempt count after max_attempts on always-advancing mtime. **Deliverable 2:** "All tests must always pass" discipline rule added to CLAUDE.md "Style Rules for This Codebase" between "Logs are the audit trail" and "Every session ends with `git push origin main`". Three branches: (a) failure in scope → fix before ship; (b) flake (race / timing / external dep) → fix the flake structurally, no skip / no `xfail` / no "pre-existing" doc; (c) failure unrelated AND real bug → fix as session's first deliverable OR open immediate follow-up before main work. Future sessions: test name MUST appear in ship report if any test fails, AND failure MUST be addressed in same or next session. Verification: 10× consecutive `pytest tests/test_positions_paper_trades_consistency.py` runs with bot alive at PID 43478 → **4 passed every time, no flakes, no skips**. Full suite: **1,482 passed / 0 failed in 31.31s** (1,479 baseline + 3 new helper tests). No `bot/` runtime change, no bot restart, no P&L impact.
- **May 7 — TelegramNotifier reads persisted cooldown on init (Session 71).** Closes the half-open Telegram persistence loop: notifier WROTE `bot_state.json:telegram_throttled_until` on every 429 (Session 52) but never READ it; `__init__` always set `self._flood_until = 0.0`, so a fresh process had zero memory of an active cooldown and would attempt the startup announcement → re-hit 429 → extend the outage. Battle Scar #15 was load-bearing because of this gap. **Fix:** ~22 LOC added immediately after `self._flood_until: float = 0.0` in `TelegramNotifier.__init__` ([bot/notifier.py:660-685](hustle-agent/bot/notifier.py:660)) — calls existing `_load_bot_state()`, parses `telegram_throttled_until` ISO timestamp, sets `_flood_until` to its unix value if still in the future, logs INFO with remaining seconds. Reuses `_load_bot_state()`, `TELEGRAM_THROTTLED_UNTIL` constant, and already-imported `time`/`datetime` — no new helpers, no new imports. The existing `_check_flood()` pre-send check at [bot/notifier.py:764](hustle-agent/bot/notifier.py:764) consumes the restored value with zero changes. 4 regression tests in `tests/test_notifier.py` (cooldown-in-future restores, cooldown-in-past stays-zero, missing-field clean, malformed-timestamp WARN) following Session 52/58.5 `monkeypatch.setattr(notifier, "BOT_STATE_FILE", state_file)` convention. CLAUDE.md Battle Scar #15 operational note replaced with the post-Session-71 rule. **Restart anytime, cooldown or not.**
- **May 8 — Strategy lab per-pair-key dedup fix (Session 73).** Session 72's prototype run surfaced a per-emit-vs-per-unique-pair-key sign flip (+$2,567 → -$4.16 manual re-aggregation); Session 73 codifies the dedup methodology as durable lab infrastructure so future stateful candidates ship correct out of the box. `CandidateOpportunity` gained optional `pair_key: str | None = None`; `evaluator.aggregate()` computes both per-emit (preserved verbatim) AND per-unique-pair-key metrics in one pass; `reports.render_markdown()` headline section now leads with **per-unique-pair-key Σ P&L** + per-emit as DIAGNOSTIC + median emits-per-key amplification ratio. Per-sport / top-5 winners-losers / reason histogram tables stay per-emit so the visible amplification signal (e.g., Session 72's 5× duplicate winner ticker) remains a "spot the problem" cue. Cross_market_correlation retrofit: `pair_key=f"{ticker}|{side}"` (game-grain, matches Session 72's "140 unique (ticker, side) keys"). Backward-compat preserved for one-shot candidates (`pair_key=None` → per-pair-key Σ EXACTLY equals per-emit Σ; locked by Test 3 regression). README "When to set pair_key" subsection added per Session 73 brief. **Test baseline:** 1,506 (Session 72) + 5 new (stateful single key, stateful distinct keys, one-shot None backward-compat, mixed, end-to-end synthetic amplification regression) = **1,511 passed, 0 failed**. Manual end-to-end on real bot/state/ reproduces Session 72 numbers EXACTLY: 140 unique pair-keys (51 resolved), median 35.0 emits per unique key, per-emit Σ +$2,567, per-pair-key Σ -$416 (= -$4.16 per-share × 100 contracts default — same direction, units convention). No `bot/` touch, no bot restart, no P&L change. Lab-only ship; pattern transfer means any future stateful candidate that re-emits on persistent divergence/condition automatically gets correct dedup just by setting `pair_key`.
- **May 8 — `no_vol_growth_first_seen/nhl_game` §10 HIGH cleaned via Outcome C HOLD (Session 74).** Session 66 §10 had one remaining HIGH: `counterfactual_hotspots: no_vol_growth_first_seen/nhl_game`, now formally evaluated at the sub-cohort level. Phase 0 confirmed both sides of the premise: gate still fires on `_prev_scan_volumes.get(ticker, 0) == 0` in `bot/live_watcher.py` (binary first-seen cycle-delay, no `bot/config.py` threshold), and the NHL-game signal is real (`n=33`, `n_no=3`, `n_yes=30`, mean **+20.91¢**, **90.9% +CLV**). Same gate as Session 45, same non-existent lever, same Outcome C: real but unactionable until an architectural change ships (persist `_prev_scan_volumes`, first-sight entry path, or materially lower `LIVE_SCAN_INTERVAL`). Watch-list trigger now canonical for all `no_vol_growth_first_seen/*`: re-open only on architecture change, cross-sport convergence (`nhl_game` + `atp_challenger` + one more sport all `n>=30` with combined mean `>= +5¢`), or post-architecture realized evidence (`n>=20` positive EV). Doc-only: no `bot/`, `tests/`, or `tools/` changes; no bot restart; baseline remains **1,511 passed / 0 failed / 0 skipped**.
- **May 7-8 — Cross-market correlation Phase 0 + strategy_lab prototype: Outcome C (Session 72).** First "search the frontier for new strategy categories" arc since Session 51 (which built the lab). Phase 0 forensic mapping confirmed NBA + NHL playoff series and game tickers concurrent in `universe.jsonl` (Round 2 active); Phase 0a re-confirmed Type 3 (bracket-vs-threshold within weather event) is **already covered by vig_stack** ([bot/strategies/vig_stack_series.py:339-354](hustle-agent/bot/strategies/vig_stack_series.py:339) puts both B-bracket AND T-threshold markets in the same ladder; yes_sum vig math sums across both — drop type #3 per spec early-out); Phase 0c manual sanity check on KXNBASERIES-26CLEDETR2-CLE (35¢) vs KXNBAGAME-26MAY*-CLE (41-58¢) showed median divergence **21¢** but mathematically self-consistent for "CLE down 1-2 in series" (P(series) = 0.5 × 0.56 + 0.10 × 0.44 ≈ 32%); Phase 0d found **0 KX*SERIES records in clv.json** (series tickers have NEVER been scanned by an active strategy → no settlement data → prototype must emit on game side only). **Phase 1 prototype:** [tools/strategy_lab/candidates/cross_market_correlation.py](hustle-agent/tools/strategy_lab/candidates/cross_market_correlation.py) (~190 LOC) — stateful candidate with O(1) partner-index lookup, `pair_key = (sport, frozenset({TEAMA, TEAMB}), winner)`, emits on game side (NO if game_yes > series_yes; YES otherwise). Tunables: `MIN_DIVERGENCE_CENTS=5.0`, `MIN_PERSISTENCE_SCANS=1`, `MIN_LIQUIDITY_VOLUME_24H=100`. **Raw lab:** 5,143 emits / 1,331 settled / +$2,567 hypothetical P&L (NHL +$5,118 / NBA −$2,551). **Per-unique-ticker re-aggregation (Session 47-style cross-cohort discipline):** 140 unique (ticker, side) keys collapse to 51 resolved at NBA −$2.87 (n=21, mean −13.67¢) + NHL −$1.29 (n=30, mean −4.30¢) = combined **−$4.16, 37.3% WR**. Both sports negative on the per-unique-ticker basis. The +$2,567 raw was scan-cadence amplification: median 35 emits per unique key (max 122) inflated a small sample of unique outcomes. **Decision: Outcome C (no real edge)** — naive divergence detection captures legitimate series-state asymmetry, not mispricing (exactly Phase 0c hypothesis). **Methodology lesson:** strategy_lab evaluator counts per-emit, not per-unique-outcome — for stateful candidates that re-emit per scan, this introduces amplification. Future stateful candidates need (a) per-pair-key emission dedup, (b) report-side aggregator dedup by (ticker, side), or (c) loud emit documentation for manual decision-time dedup. Session 72 is the founding example of this lesson. **Watch-list trigger:** re-evaluate when ALL of (i) state-aware math implemented (per-game series state from ESPN/title parsing OR Game-7-only filter), (ii) lab cross-cohort dedup infrastructure shipped, (iii) series-side clv coverage populated. Until #1 fires, naive cross-market correlation is unactionable. 20 new tests + Session 51 `test_canonical_schema_used_throughout` recursively covers new file (caught my own initial docstring violation). No bot code touched. No restart. Lab read-only on `bot/state/`. Mirror Sessions 18.5/40/41/42/45/46/67-investigate/68/69 Pattern C discipline — preserve search frontier, kill the hypothesis cleanly.
- **Honest P&L position (May 7, post-Session-71):** Realized P&L sits at **+$579.73 across 257 settled trades** on the $10,500 paper base (~5.5% return). Per-strategy: vig_stack **+$614.33** (68% WR, +$3.47/trade — gained +$297 over the May 6→7 window on 8 settled trades, outlier-independence materially stronger than at end-of-Session-54 framing); live_momentum **−$34.60** (30% WR, −$0.43/trade) — unchanged since May 6 (no new live_momentum trades have settled in the post-Session-54 corrected-sizing window yet). **Bot is now genuinely profitable from vig_stack as a structural arb, not just outlier carry.** Sessions 58 + 58.5 + 59 + 60 + 65 + 66 are operational/observability (notifier lifetime + log cleanup + planner consolidator + daily-report cron + housekeeping + §10 reports surface), and Sessions 61 + 67 + 68 + 69 + 70 are doc/test-only Pattern B/C ships — no impact on returns. The **May 14-18 routine cluster** (Session 53 KXINX day-14, Session 38a ATP re-validation, Session 49 NBA/UFC sizing, Session 22 MOMENTUM_LEADER_MIN) remains the next sharp decision point — and Session 55's `settlement_vs_rationale` heuristic is now actively surfacing actionable findings between routine fires.
- **Test baseline: 1,511 passed / 0 failed** (Session 73 added 5 regression tests for the per-pair-key dedup methodology — stateful single key, stateful distinct keys, one-shot None backward-compat, mixed stateful + one-shot, end-to-end synthetic amplification regression with sign flip; plus Session 51's `test_canonical_schema_used_throughout` recursively covers the new file edits). Session 72 added 20 regression tests for `cross_market_correlation` — pair-key parametrized + parametrized negatives, state-only-on-series-side, no-partner, below-divergence, liquidity floors, NO/YES emit branches with correct math, persistence threshold via monkeypatch, wrong-winner non-match, cross-matchup state isolation, defensive None on missing identifiers. Session 71 added 4 regression tests for the persisted-cooldown init-read path; Session 70 closed the documented `test_paper_trades_open_count_matches_positions_active` live-state race via mtime-fence helper + 3 regression tests; Sessions 68 + 69 + 72 + 73 were doc-only / lab-only Pattern C ships with no production behavior change. **Zero baseline failures, zero skips, zero flakes** — Session 70's discipline rule in CLAUDE.md "Style Rules" section codifies this as the standing bar going forward. Battle Scars: **17 entries**, with **#15's operational rule retired post-Session-71** (the structural failure mode is still documented for posterity; the operational note now points forward — restart anytime). Session 57's calendar-vs-commit-timestamp pattern remains a discipline note pending a third recurrence to promote to Battle Scar #18. Discovery agent: **10 heuristics** unchanged post-Session-71. Strategy lab candidates: 2 (`example_total_points_under` + `cross_market_correlation`).
- **Calendar of scheduled routines:** see `REPORT_CALENDAR.md` at the repo root. Daily 3 AM ET (daily report), daily 6 AM ET (discovery agent — **10 heuristics post-Session-55**), weekly Sundays 6 AM ET (weekly report). One-offs still upcoming: **May 12** weekly digest spot-check; **May 13** dual routine (Session 36 day-14 floor signal recheck + Session 38a ATP re-validation); **May 14** Session 53 KXINX day-14 P&L slope check (and informally — KXHIGHMIA/KXHIGHAUS family-cap revisit candidate from Session 55 findings); **May 15** Session 49 per-sport sizing re-validation (now on corrected-balance data per Session 54); **May 18** Session 22 MOMENTUM_LEADER_MIN re-validation (also corrected-balance). Each routine writes its report file to disk before any interpretation work — partial chat death preserves data.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [How It Works End-to-End](#how-it-works-end-to-end)
3. [Strategy Types](#strategy-types)
4. [Edge Quality & Safety Controls](#edge-quality--safety-controls)
5. [Position Sizing](#position-sizing)
6. [Execution Pipeline](#execution-pipeline)
7. [Paper Trading Sandbox](#paper-trading-sandbox)
8. [Closing-Line Value Tracking](#closing-line-value-tracking)
9. [Telegram Interface](#telegram-interface)
10. [Configuration Reference](#configuration-reference)
11. [Architecture & File Map](#architecture--file-map)
12. [Running the Bot](#running-the-bot)
13. [Testing](#testing)
14. [Dashboard UI](#dashboard-ui)
15. [Discovery Agent](#discovery-agent)
16. [Strategy Lab](#strategy-lab)
17. [Recent Improvements (Apr 20–May 7)](#recent-improvements-apr-20may-7)

---

## What It Does

Glint watches Kalshi prediction markets 24/7 and looks for contracts whose market-implied probability is meaningfully wrong relative to an independent model. When it finds one — with edge above a minimum threshold, sizing above a minimum dollar amount, and no position conflicts — it alerts via Telegram and either executes automatically (paper mode) or sends a GO prompt for manual approval.

It covers (see [Strategy Types](#strategy-types) for per-strategy active/disabled status):

- **Vig stacking** (`vig_stack_series`, `vig_stack_futures`) — **ACTIVE.** Structural overprice in Kalshi contract ladders. No external data needed. Net negative on paper before Filter F; the whitelist + NO ≥ 0.93 gate (Apr 20) is the repair.
- **Live game momentum** (`live_momentum`) — **ACTIVE via the live-watcher subsystem.** Dip-buy the leader on 1v1 live matches; take-profit + trailing stop exits. Currently **−$34.60 on 80 settled (30% WR, 60% EE rate)** — but pre-Session-54 sizing was at 9% of intended Kelly per Battle Scar #16; the current loss number is at distorted scale. ATP main-tour re-enabled Apr 29 (Session 38a); challengers + WTA stay disabled.
- **Sports arbs** (`sports_monotonicity_arb`, `sports_consistency_arb`) — **DISABLED Session 56 (May 6, 2026).** Codex review surfaced an opportunity-vs-execution shape mismatch: scanner emits two-leg `arb_pair` metadata but executor reads only the single-ticker `recommended_side` field, so every "riskless arb" would have fired as a one-sided directional bet at $200-Kelly sizing labeled `confidence=0.95`. 0 historical fills (lucky catch before any real execution). Re-enable only via paired-leg execution (Option A) when arb opportunity volume justifies the new code surface.
- **Weather single-market**, **sports series edge**, **parlay edge**, **crypto price edge**, **economic indicators** — **DISABLED** (data-driven kills from the Apr 14 audit; see the table in Strategy Types).

Everything runs in **paper mode by default** — full edge detection, sizing, and trade lifecycle management with zero real capital at risk until you flip one line in `config.py`.

---

## How It Works End-to-End

### The Main Loop

`bot/main.py` runs three concurrent `asyncio` tasks (one disabled):

1. **`_main_loop()`** — the primary scan cycle. Runs immediately on startup, then on an adaptive schedule:
   - 2-minute cadence when games are live
   - 10-minute cadence in pregame windows
   - 30-minute cadence when nothing is live

   Each cycle calls `scan_cycle()` in `bot/scanner.py`, which fans out to all strategy scanners in parallel (ThreadPoolExecutor), deduplicates results across strategies, ranks by edge, and returns a prioritized opportunity list. The main loop then:
   - Filters to `ACTIVE_STRATEGIES` only
   - Re-runs Kelly sizing with current live balance
   - Runs `verify_contract_direction()` to confirm the title semantics match the intended trade side
   - Checks position limits and cooldowns
   - Executes automatically in paper mode, or sends a GO prompt in live mode
   - Handles Telegram callbacks (GO / SELL / status commands)
   - Runs fill reconciliation, trailing stop checks, and CLV settlement scans as background steps

2. **`_live_scan_loop()`** — independent loop running every 60s. Discovers live 1v1 matches on Kalshi and auto-spawns per-match `LiveGameWatcher` tasks that poll every 10s. This is where `live_momentum` runs.

3. **`_crypto_scan_loop()`** — disabled. Kept wired up behind `CRYPTO_ENABLED = False`; the crypto log-normal model was killed in the Apr 14 audit.

### Single Scan Cycle

```
scan_cycle()
  ├── prefetch: Kalshi parlay markets (400+ contracts, cached)
  ├── scanner_weather.py   → [DISABLED] still imported but returns [] for single-market edge
  ├── scanner_sports.py    → [DISABLED] series_game_edge killed in Apr 14 audit (26% WR)
  ├── kalshi_series.py     → [ACTIVE] vig_stack_series + vig_stack_futures (crypto ladder also killed)
  ├── scanner_sports_arb.py → [ACTIVE] monotonicity + consistency riskless arbs
  ├── econ_scanner.py      → [DISABLED] CPI nowcast retired
  └── filter to ACTIVE_STRATEGIES + deduplicate + rank by edge → return top N
```
Results are filtered to `ACTIVE_STRATEGIES` in `main.py` before execution. Disabled scanners are kept plumbed so re-enabling is a one-line config change, not a code change.

Each scanner returns a list of opportunity dicts with a common schema:
```
{
  ticker, title, side, edge, relative_edge, fair_value,
  kalshi_price, confidence, opp_type, math_chain, ...
}
```

---

## Strategy Types

> **Active vs disabled:** Only the strategies in `ACTIVE_STRATEGIES` actually place trades. As of May 7 (post-Session-56) that's `vig_stack_series` and `vig_stack_futures`, plus `live_momentum` via the live-watcher subsystem. **Sports arbs were disabled Session 56** after Codex flagged the opportunity-vs-execution shape mismatch (would have fired one-sided directional bets labeled "riskless"). Weather single-market, series_game_edge, parlay edge, crypto, and econ are all **disabled** — kept for reference but not called from the main scan cycle. Each section below flags its status. The honest current money-maker is **`vig_stack`** (177 settled, +$614.33, 68% WR, +$3.47/trade as of May 7) — outlier-independence is materially stronger than the end-of-Session-43-investigate framing; Filter F + Session 36 TP/SL exemption + Session 53 family caps are rehabilitating the strategy effectively. **`live_momentum` is ambiguous-EV** pending 14d of corrected post-Session-54 sizing data.

### 1. Weather (`weather`) — DISABLED

**Signal:** NWS grid-point forecast minus a documented 1.5°F warm bias, compared against Kalshi's implied temperature distribution.

**Mechanics:**
- Fetches hourly/daily forecast from `api.weather.gov` for 18 US cities
- Maps city names to Kalshi series tickers (`KXHIGHNY`, `KXHIGHMIA`, etc.)
- Applies `NWS_BIAS_CORRECTION = 1.5°F` — NWS historically runs warm
- Computes probability using a normal distribution with dynamically estimated σ (derived from contract spacing in the Kalshi ladder)
- Converts that probability to a fair YES price; compares to Kalshi ask

**Filters:**
- Skips markets closing within 8 hours (`WEATHER_MIN_HOURS_TO_CLOSE = 8`) — same-day markets price off real-time temperature observations, not forecasts
- Applies the 25% absolute edge cap shared across all strategies
- Only scans next-day contracts where NWS forecast data is most reliable

**Coverage:** NYC (Central Park), Miami (MIA airport), Chicago (Midway), Denver, Austin, Boston, DC, SF, LA, Seattle, Phoenix, Dallas, Atlanta, Philadelphia, Las Vegas, Portland, Minneapolis, Nashville

---

### 2. Series Game Edge (`series_game_edge`, `ipl_game_edge`) — DISABLED (26% WR)

**Signal:** Sportsbook consensus moneyline vs Kalshi win/loss market for the same game.

**Mechanics:**
- Fetches moneylines from a priority cascade of four sources: DraftKings → Bovada → FanDuel → TheRundown (Odds API backup)
- Normalizes each team name to a canonical form and fuzzy-matches to Kalshi market titles
- Converts American odds to implied probability, removes the vig to get fair probabilities
- Compares against Kalshi bid/ask mid price
- Runs forward/backward self-check on every probability calculation

**Sports:** NBA, MLB, NHL, NCAAB, IPL

**Odds source cascade:** DraftKings is preferred but blocked on some residential IPs (Akamai WAF). The bot auto-disables DK for the session on a 403 and promotes Bovada. If Bovada fails, FanDuel, then ESPN, then TheRundown.

---

### 3. Parlay Edge (`parlay_yes`, `parlay_no`) — DISABLED

**Signal:** Independent-leg probability product vs Kalshi parlay contract price.

**Mechanics:**
- Parses Kalshi parlay market titles ("yes Boston wins, yes LA wins") into individual legs using `agent/parlay.py`
- Prices each leg from sportsbook moneylines, then multiplies assuming independence
- Applies a correlation discount (parlays are not perfectly independent — favorites tend to correlate)
- Compares the resulting fair value to the Kalshi YES ask or NO bid

---

### 4. Vig Stack (`vig_stack_series`, `vig_stack_futures`) — ACTIVE

**Signal:** Pure structural arbitrage — Kalshi contract ladders routinely over-price the NO side across adjacent strikes.

**Mechanics:**
- For any series with multiple mutually-exclusive thresholds (e.g., "NYC high < 60°F", "NYC high 60-65°F", "NYC high > 65°F"), the sum of YES probabilities across bins must equal 1.0
- When the ladder mis-prices and the implied sum exceeds 1.0, the cheapest NO contracts carry positive expected value with no external model needed
- No weather data, no sportsbook odds, no external API required — this edge is purely computational

**Filter F (Apr 18 → Apr 20):** The structural math is right but the *ladders* differ in quality. Stable ladders (Miami highs, Austin highs, S&P INX) sit on tight distributions where the NO edge converts to wins. Volatile ladders (high-variance weather cities, fast-moving indices) blow out in the tails and turn +EV math into −$100 of realized losses on paper. Filter F whitelists stable families via `VIG_STACK_STABLE_FAMILIES` and requires NO ≥ **0.93** (`VIG_STACK_WEATHER_MIN_PRICE`) on everything else. Apr 20 raised this from 0.90 to 0.93 after bucket analysis showed only [92-96¢) is breakeven on volatile families (`<92¢` was −$110.79 / 42 trades; the new 0.93 floor sits 1¢ above the bottom of the breakeven band).

**Auto-exit exemption (Apr 29 — Session 36):** Vig_stack positions (`opp_type in ("vig_stack_no", "vig_stack_series")`) are exempt from ALL three auto-exit paths in `bot/main.py`'s position-loop: `edge_flipped` (already exempt pre-Session-36), `take_profit` (+50% pnl_percent), and `cut_loss` (-30% pnl_percent). Reason: pre-Session-36, the cut_loss gate (-30% pnl) translated to a ~2.85¢ adverse yes_ask move on a 95¢ NO entry — trivially common in weather markets. 32% of vig_stack paper trades were exiting early at mean −$5 to −$10 across ALL families (median hold 23h — slow drift exits, not snap kills). The floor gate was a conservative downstream defense against a broken cut path; the actual fix is exempting vig_stack from TP/SL since the edge is structural (only ladder-vig collapse should exit). Same session shipped `exit_reason` persistence on `paper_trades.json` so future audits distinguish auto_take_profit / auto_cut_loss / edge_flipped / manual paths. May 13 day-14 verification routine queued — if the `non_stable_below_weather_floor` mean CLV signal diminishes meaningfully, the diagnosis is confirmed and `VIG_STACK_WEATHER_MIN_PRICE` becomes a Session 39+ retuning candidate.

**Paper performance (Apr 20 ground truth):** 54 settled, **−$110.62**, 29W/25L (54% WR). Filter F is expected to drift this positive on new entries; the historical loss pool doesn't retroactively fix. Session 36's TP/SL exemption is expected to drive new vig_stack trades toward hold-to-settlement (current 68% → ~100%); May 6 day-7 routine measures this. By family: volatile (`KXHIGHDEN/NY/CHI`) = 36 trades / −$126.88 / 69% early-cut; whitelist (`KXHIGHMIA/AUS/INX`) = 18 trades / +$16.26.

**This is the most mechanical strategy in the bot** — it doesn't predict outcomes, it exploits pricing inconsistencies in the market structure itself.

---

### 5. Crypto Price Edge (`btc_price_edge`, `eth_price_edge`, `sol_price_edge`, `xrp_price_edge`, `doge_price_edge`) — DISABLED (`CRYPTO_ENABLED=False`)

**Signal:** Log-normal price model vs Kalshi intraday/daily price threshold contracts.

**Mechanics:**
- Fetches current spot price and 30-day realized volatility from CoinGecko (30-minute cache TTL)
- Models next price as log-normal: `ln(S_T/S_0) ~ N(0, σ√T)`
- Computes `P(price > threshold)` using the normal CDF
- Compares to Kalshi YES ask for above-threshold contracts

**Independent loop:** Runs on its own 5-minute cadence (`_crypto_scan_loop`) to avoid interfering with sports/weather timing and to prevent doubled CoinGecko requests.

**Timeframes:** 5-hour and 10-hour intraday contracts, plus daily close contracts.

---

### 6. Economic Edge (`econ_cpi_edge`) — DISABLED

**Signal:** CPI nowcast model vs Kalshi inflation/economic indicator markets.

**Mechanics:**
- Pulls 100 active Kalshi economic markets
- Applies an econometric nowcast model for CPI (currently seeded at 2.43% for the current cycle)
- Computes probability that the realized number lands above/below each Kalshi threshold
- Surface to Telegram as opportunities with the same edge/sizing pipeline as other strategies

---

### 7. Live Game Momentum (`live_momentum`) — ACTIVE (via watcher subsystem)

**Signal:** Buy dips on the clear leader of a live 1v1 or head-to-head match (UFC, NBA, NHL, ATP main tour as of Apr 29) and ride the trailing stop / take-profit. Challenger circuits + WTA disabled Apr 20.

**Mechanics:**
- `_live_scan_loop()` in `bot/main.py` polls every 60s, discovers live matches on Kalshi, auto-spawns a `LiveGameWatcher` task for each match with a clear leader
- The watcher polls Kalshi every 10s (`LIVE_POLL_INTERVAL`), tracks price history in a deque, recomputes a `GameContext` (momentum, win probability, lead trend, dip quality score) every tick. ESPN scoreboard fetch supplies `wp` / `wp_edge` / score / period — restored Apr 23 (Session 3) after silently failing for ~10 days on a missing UA header + cert validation
- Buys when the leader dips 4–8¢ from its recent high AND the dip quality score passes sport-specific thresholds. **Leader floor `MOMENTUM_LEADER_MIN = 0.65`** (lowered 0.70 → 0.65 on Apr 27 Session 19c after 15-variant tick-replay sweep showed +488¢ test delta over 6 test trades — fragile signal documented; auto-revalidating May 18)
- **Conviction entry:** if there's no dip but game state screams value (wp_edge > 8%, positive momentum, 68–82¢ entry, ≥ Q3 completion), buys a 70%-sized position. NBA/NHL only; MLB and tennis excluded from conviction
- Exits: take-profit (12¢), trailing stop (6¢ from peak — Session 19a-peakfix made this physically able to fire for the first time), stop-loss (10¢), near-settle lock (≥93¢), hard-cap ($5 max loss)
- Per-sport tuning in `SPORT_PROFILES` (`config.py:150-369`). Investigation #1 (Apr 29 night) flagged UFC as mechanically distinct (median hold 123s vs 642–1791s for court sports) — per-sport TickStrategy variants filed as Session 39 candidate.
- **Per-sport `size_multiplier` (Session 49 — May 1):** NBA and UFC entries are sized at **0.5×** of standard Kelly fraction (NHL + MLB explicit at 1.0×; all others default 1.0×). Direct response to measured per-sport bleed (NBA n=21 −$26.57 48% WR; UFC n=8 −$8.30 12% WR). See [Position Sizing](#position-sizing) for full table. May 15 +14d re-validation routine queued.

**Disable list evolution (`MOMENTUM_DISABLED_SPORTS`):**
- **Apr 20 (Session 2):** initial set `{atp, atp_challenger, wta, wta_challenger}` — based on n=17 atp_challenger trades at -$7.80 and a precautionary bundle of the other 3 tennis variants.
- **Apr 29 (Session 38a):** ATP main tour removed from the set after Investigation #1 surfaced n=56 settled CFs at +11.32¢ avg CLV / 82% +CLV (46/10) — passed the n_no_won >= 10 survivorship bar from Session 30-followup. Same asymmetric-evidence pattern Session 30-followup flagged for wta_challenger (disable was based on challenger evidence, not main-tour evidence). May 13 day-14 re-validation routine queued.
- **Apr 30 (Session 38a-2):** WTA main-tour disable re-evaluated — Outcome B (doc-only, held disabled) on insufficient evidence at the time.
- **Current set (May 1):** `{atp_challenger, wta, wta_challenger}`. **Discovery agent (Session 43b) found `no_leader/wta` +20¢ mean CLV (n=15)** on day-2 run — directly contradicts the Session 38a-2 hold decision. Candidate Session 44 will mirror the Session 38a methodology to revisit the WTA disable with this fresh evidence. Held tennis positions still exit normally — the `can_enter` gate only blocks entries.

**Paper performance (May 1 — refreshed):** 74 settled, **−$36.39**, 23W/7L/44EE (31% WR; 59% positive when including EE_positive). Win:Loss magnitude ratio 0.54 (losses 1.85× wins) — asymmetric-loss multiplier confirmed at trade level. Per-sport: **NHL +$7.80 / 70% WR**, **ATP main +$8.60 / 25% WR (n=4)**, NBA −$26.57 / 48% WR (deepest hole), UFC −$8.30 / 12% WR, WTA −$10.20 / 17% WR (currently disabled). Session 49 sizing intervention now in effect on NBA + UFC. The 7 hard-lost trades all clustered at high entry prices (0.65–0.87) — clear-leader bets that got upset. Apr 16 `STRATEGY_BUDGETS` (20% equity allocation) stopped vig_stack from starving live_momentum's pool.

---

## Edge Quality & Safety Controls

Every opportunity passes through a gauntlet before a trade is placed.

### 1. Minimum Relative Edge
```python
MIN_RELATIVE_EDGE = 0.15   # 15% — skip anything weaker
```
Relative edge = `(fair_value - market_price) / market_price`. This filters out small mispricings that don't justify execution cost and slippage.

### 2. Maximum Absolute Edge Cap (25%)
```python
MAX_CRYPTO_EDGE = 0.25
```
Applied in `_scan_crypto_series()` and `scanner_weather.py`. Any edge above 25¢ absolute is treated as stale pricing, near-expiry noise, or broken liquidity — not a real opportunity. The math is correct; the market is broken. Implemented after the bot was entering XRP contracts at 5¢ when XRP spot was 41% above the threshold and fair value was ~99.8%.

### 3. Direction Verification
```python
verify_contract_direction(ticker, title, side, fair_value)
```
Run as a mandatory pre-trade check in the executor. Parses the contract title to confirm the semantic direction matches the intended trade side. Uses a bank of regex patterns for above/below/between temperature syntax, and separate parsers for sports and crypto contracts. If the direction check fails, the trade is hard-blocked with an error log.

### 4. Price Movement Kill Switch
```python
MAX_PRICE_MOVE_CENTS = 3
```
Before executing a GO, the executor re-fetches the current Kalshi price. If the market has moved more than 3 cents since the opportunity was identified, the trade is aborted. This prevents chasing a price that was already consumed.

### 5. Position Deduplication
Re-entry on a currently-held ticker is blocked regardless of strategy type. If `positions.json` already has an open position on `KXHIGHDEN-26APR06-T63`, that ticker is skipped on all future scans until the position closes.

### 6. 4-Hour Cooldown After Exit
After any position exits (win, loss, or early cut), the same ticker is blocked for 4 hours. Implemented after observing `KXHIGHDEN-26APR06-T63` being entered 5 times in a single day — the position kept hitting the cut-loss threshold, getting exited, then being re-detected as an edge on the next scan.

```python
_COOLDOWN = timedelta(hours=4)
```

### 7. Exposure Limits

```python
MAX_POSITION_PERCENT = 0.20   # No single trade > 20% of balance
MAX_TOTAL_EXPOSURE   = 1.00   # Global cap — 100% of equity (balance + open exposure, Apr 16)
STRATEGY_BUDGETS = {
    "vig_stack":     0.60,    # 60% of equity
    "live_momentum": 0.20,    # 20% of equity
    "arbs":          0.20,    # 20% of equity
}
```

- `MAX_TOTAL_EXPOSURE` is enforced against `balance + total_exposure` (equity), not just cash.
- `STRATEGY_BUDGETS` caps each strategy's open exposure independently, so a heavy vig_stack position can't starve live_momentum. Rejections surface as `STRATEGY_BUDGET: vig_stack has $X of $Y budget` in the logs.
- Both live together: a trade must pass the global cap **and** the per-strategy budget.

### 8. Math Self-Check
`math_engine.py` runs every probability and edge calculation forward and backward:
- Probability complement check: `p + (1-p) == 1.0` within 1e-6
- Edge check: `fair_value - market_price == edge` and `market_price + edge == fair_value`

If any self-check fails, the trade is blocked and the failure logged with the full math chain for debugging.

---

## Position Sizing

Sizing lives in `bot/sizing.py` and uses fractional Kelly criterion.

### The Formula

```
full_kelly  = (b × p - q) / b
            where b = (1 / price_dollars) - 1   # net odds on a $1 payout contract
                  p = fair probability
                  q = 1 - p

sport_mult  = SPORT_PROFILES.get(sport, {}).get('size_multiplier', 1.0)  # Session 49
fractional  = full_kelly × KELLY_FRACTION × sport_mult                   # 25% × per-sport
capped      = min(fractional, MAX_BET_FRACTION)  # hard cap at 5% of balance
risk_dollars = balance × capped
risk_dollars = max(risk_dollars, $1.00)          # floor
risk_dollars = min(risk_dollars, min(balance × 5%, $200))  # dynamic ceiling
```

### Why 25% Fractional Kelly
Full Kelly maximizes long-run log wealth but has brutal drawdowns when probability estimates are wrong. At 25% fractional Kelly, you sacrifice roughly half the growth rate of full Kelly in exchange for drawdowns that are ~6× smaller. For a bot operating on model-derived edge estimates, this is appropriate.

### Uncertainty Discount
Kelly assumes you know the true probability. You don't. The `uncertainty_discount` parameter (default 0.85 for model-derived edges) scales the input probability down before computing Kelly fraction, giving the model credit for being wrong ~15% of the time. High-confidence scans (confidence ≥ 0.9) can use a smaller discount.

### Per-Sport Size Multiplier (Session 49 — May 1, 2026)

`SPORT_PROFILES` (the same per-sport surface Sessions 41 + 42 use for TP/SL overrides) carries an optional `size_multiplier` field. Default 1.0 = no change. Currently set:

| Sport | size_multiplier | Why |
|---|---|---|
| `nba` | **0.5** | n=21 settled live_momentum, −$26.57, 48% WR — biggest measured bleed; halve exposure |
| `ufc` | **0.5** | n=8, −$8.30, 12% WR — small sample but consistent loss pattern |
| `nhl` | 1.0 (explicit) | n=10 70% WR — too thin to size up (Session 38a bar n=56); explicit-1.0 documents the no-change decision |
| `mlb` | 1.0 (explicit) | hold pending more data |
| (all others) | 1.0 (default) | unchanged |

The multiplier applies inside `kelly_size()` after full-Kelly but before fractional Kelly. Threaded via `sport=self.sport` from `bot/live_watcher.py:1695-1702` (`_auto_bet_momentum`) and verified at line 2621 (`_auto_bet` WATCH path). vig_stack and any sport-less call site pass `sport=None` → multiplier defaults to 1.0 → behavior unchanged.

**May 15 +14d re-validation routine queued** (mirrors Session 38a methodology): pulls post-deploy live_momentum trades grouped by sport, computes per-sport mean P&L + win rate post-2026-05-01, decides CONFIRM / EXPAND (size down further OR add NHL/ATP size-up if data clears n=56 bar) / REVERT.

### Resulting Behavior
A 15% relative edge at a 20¢ contract on a $500 paper balance produces roughly 14 contracts (~$2.80 total cost) on a 1.0× sport (or sport=None). On NBA or UFC (0.5×) the same scenario produces roughly 7 contracts (~$1.40), still respecting the $1.00 floor.

---

## Execution Pipeline

The execution path for a single trade (in `bot/executor.py`):

```
execute_trade(opportunity)
  1. verify_contract_direction()     ← hard-block if title semantics don't match
  2. kelly_size()                    ← compute contracts and total cost
  3. _check_balance()                ← paper: derived from paper_trades.json; live: API
  4. _check_position_limits()        ← dedup + 4h cooldown + MAX_POSITION_PERCENT 20%
                                       + MAX_TOTAL_EXPOSURE 100% (vs equity, Apr 16)
                                       + STRATEGY_BUDGETS (vig_stack 60% / live_momentum 20% / arbs 20%)
  5. re-fetch Kalshi price           ← abort if moved > 3 cents
  6. place_order() or paper record   ← live: Kalshi API; paper: write to paper_trades.json
  7. record_clv_entry()              ← log entry price and fair value for CLV tracking
  8. append to positions.json        ← position tracker picks this up for P&L monitoring
```

**Live exit path:**
```
exit_position(ticker, reason)
  1. Look up position in positions.json
  2. In PAPER_MODE: simulate exit at current yes_bid; update paper_trades.json
  3. In live mode: place market sell order via Kalshi API; cancel any resting orders
  4. Mark position exited_at timestamp (starts cooldown clock)
  5. Send Telegram notification with realized P&L
```

---

## Paper Trading Sandbox

`PAPER_MODE = True` by default. The paper sandbox is a full-fidelity simulation:

- **Entry** — Written to `bot/state/paper_trades.json` with entry price, contracts, cost, fair value, and edge at trade time
- **Balance** — Computed from scratch on every balance check by walking `paper_trades.json`. Starting balance is **$10,500** (bumped 500 → 10,500 on Apr 29 with a +$10,000 deposit; see Session 38a-followup). Open positions subtract cost. Won positions credit $1/contract. Early exits credit the simulated exit price. This makes the balance tracking self-contained and independent of the live Kalshi balance.
- **Exit** — Simulated at current `yes_bid` (what you'd actually get if you sold into the market). Realistic — not mid or ask.
- **Resolution** — When `resolve_trades()` detects a market has settled, it updates both `positions.json` and `paper_trades.json` with the actual result, payout, and realized P&L.
- **CLV** — Recorded at entry, updated at settlement. This is the primary signal for whether strategies are worth going live with.

**When to go live:** Only after ≥50 settled paper trades per strategy with average CLV > 0 and a positive-CLV rate above 55%. Flip `PAPER_MODE = False` in `config.py`.

---

## Closing-Line Value Tracking

CLV is the most important metric in this system. It measures whether the market moved in your favor after you entered — independent of whether you actually won. A strategy with positive average CLV has real edge. A strategy with negative CLV is getting lucky on wins or just picking off illiquid contracts.

**Calculation (in `bot/clv.py`):**

```
YES trade:  CLV = closing_yes_price - entry_yes_price
NO trade:   CLV = (100 - entry_yes_price) - closing_yes_price
            (positive CLV = market moved toward NO = good for our position)
```

Closing price is the settlement value (100 for YES result, 0 for NO result), or the mid price if the market is still open (for interim tracking).

**Report format (via `/clv` in Telegram):**
```
📈 CLV REPORT (Closing-Line Value)

Overall (38 trades): ✅ avg CLV = +4.2¢ (+18.3%) | beat line: 63%

By strategy:
  ✅ weather (12): +3.1¢ avg | 67% beat rate
  ✅ vig_stack_series (18): +6.8¢ avg | 72% beat rate
  ❌ series_game_edge (8): -1.2¢ avg | 38% beat rate
```

A strategy with a beat rate below 50% is a red flag regardless of win/loss record.

---

## Discovery Agent

Sessions 43a + 43b + 47 + 48 (May 1, 2026) shipped a daily-running heuristic discovery agent under `tools/discovery_agent/`. It scans every meaningful bot data file, surfaces patterns via **9 pluggable heuristics**, and emits a markdown report + JSONL findings file. **Pure-Python — no LLM API calls, no plugin/Claude-Agent-SDK integration.** Daily 6:00 AM ET via launchd.

The trigger that built it: on Apr 30 the bot made +$172 on a vig_stack_futures trade (KXMLBGAME-26APR291840SFPHI-PHI). Investigation revealed two cancelling bugs (scanner mis-classifying per-game MLB winners as `vig_stack_futures` + Session 36's exemption-set missing `vig_stack_futures`) that together produce profitable behavior on a misunderstood market type. We only found it because Tyler pinged about a Telegram notification. **A daily heuristic scanner over all bot data would have surfaced that class of pattern within a day instead of by coincidence.** Operating Posture in CLAUDE.md codifies the prime directive ("always search for new possibilities"); this agent operationalizes it.

**The 9 heuristics** (each with tunable thresholds at the top of its file):

| # | Heuristic | What it catches |
|---|---|---|
| 1 | `outlier_pnl` | Single trades dominating their (opp_type, sport) cohort by ≥$75 AND ≥30% of cohort total |
| 2 | `cohort_emergence` | New (opp_type, sport) cohorts emerging in last 7d vs prior 30d. Refined in 43b: distinguishes futures (`KXMLB-*`) from per-game (`KXMLBGAME-*`); demotes severity to `info` when cohort has 0 accepts AND 0 trades |
| 3 | `threshold_proximity` | Reject-gates with ≥5 rejects clustered within 5% of their threshold; cross-references to find +CLV near-misses |
| 4 | `counterfactual_hotspots` | (skip_reason, sport) buckets with ≥10 settled CFs AND mean +CLV ≥5¢ AND +CLV rate ≥60% AND survivorship sanity (n_no_won ≥3). **Refined in Session 47 — surfaces cross-cohort context inline + auto-demotes severity when per-cohort flag is positive but cross-cohort distribution is flat-or-negative OR cohort sport is in MOMENTUM_DISABLED_SPORTS** (3-trigger demotion ladder, prevents Sessions 45/46-shape cherry-pick failures from burning future sessions) |
| 5 | `universe_gap` | Sport+market_type pairs in recent decisions but absent from current universe.jsonl — catches scanner-side regressions |
| 6 | `live_tick_anomalies` | STREAMING — tickers with ≥3 jumps ≥15¢ in the lookback window; cross-checks against open positions at the time |
| 7 | `cadence_outcome` | Per-cadence-bucket P&L outliers — measures Session 39 territory (asyncio loop cadence) in P&L terms |
| 8 | `log_error_spike` | STREAMING — error fingerprints with ≥3× recent (24h) rate vs baseline (168h); high severity at ≥10× and ≥20 recent occurrences |
| 9 | `concurrent_attack_angles` | **Session 48 — search-frontier expander.** For each event family with ALREADY_TRADING tickers, surfaces (a) `concurrent_fire_candidate` findings where a SCANNED_NOT_TAKEN sibling market shows positive concurrent CLV when the primary strategy wins, and (b) `scanner_gap` findings where event families have NEVER_SCANNED market types worth building a scanner for. Same Session 47 cross-family demotion ladder applied at the strategy-pair × event-family level. **The bot's own search engine for new attack angles derived from data we already collect** |

**Architecture invariants (locked by tests):**
- `DiscoveryContext` pre-loads all 14 bot data sources once per run; heuristics never read files directly
- Streaming iterators for `live_ticks.jsonl`, `bot.log`, and the 39MB `universe.jsonl` — memory-safety regression tests cap peak RSS <50MB on 100k-line fixtures
- Each Finding has a stable fingerprint hash → cross-run dedup classifies findings as NEW / STABLE / RESOLVED so the daily report leads with what's actually new
- One broken heuristic does NOT abort the run (per-heuristic try/except in `main.py` + report's "Heuristic errors" section)
- Schema-aware skip — heuristics with missing data sources log + exit cleanly, no traceback noise
- **Canonical schema reference is mirrored in `tools/discovery_agent/README.md`** — single source of truth for `clv.json` / `decisions.jsonl` / `paper_trades.json` field names + value enums (added in Session 45 post-session correction after a `market_result == 'no_won'` vs `'no'` schema-value bug nearly produced a falsified disqualification)
- `test_sfphi_regression.py` is a **P0 value-prop lock-in**: if SFPHI ever stops surfacing in the agent's output, the agent has lost its founding example

**Self-improving loop (May 1 in 48 hours).** The agent has improved itself THREE times since shipping:
- **Session 43-investigate → Session 43b refinement.** First-run cohort_emergence over-stated the SFPHI lead because it counted decisions, not unique tickers / accepts / trades. Refinement added those three evidence keys + futures-vs-per-game sport distinction.
- **Sessions 45 + 46 → Session 47 refinement.** Two consecutive HOLD outcomes on counterfactual_hotspots-surfaced retuning candidates that turned out to be cross-cohort cherry-picks. Refinement added cross-cohort context to every Finding's evidence + 3-trigger severity demotion ladder so the failure mode auto-handles.
- **Tyler's directive → Session 48 expansion.** New 9th heuristic concurrent_attack_angles makes the agent surface NEW strategy candidates instead of only refining existing ones. Search frontier expander.

**Real-world validation (first 2 days of operation):**
- **Day 1 (May 1 manual run):** 13 NEW findings. SFPHI surfaced as `outlier_pnl` HIGH severity. `cohort_emergence` flagged `vig_stack_futures` across mlb/nba/nhl as a NEW cohort. Investigation (Session 43-investigate) decomposed the cohort lead → confirmed SFPHI is a singleton AND surfaced a separate real lead (`non_stable_below_weather_floor` rejecting +EV opportunities on sports futures, e.g. KXNBA-26-OKC at edge=+11.48¢ rejected on no_ask_prob=0.48 vs floor=0.93).
- **Day 2 (May 1 e2e after 43b):** 3 HIGH `counterfactual_hotspots` findings: `no_leader/wta` +20¢ CLV (n=15), `no_leader/nhl_game` +22¢ CLV (n=19), `no_leader/atp` +9¢. Sessions 44/45/46 each followed up — gate-flow walks + 2 HOLD outcomes confirming the cherry-pick pattern.
- **Day 2.5 (May 1 evening, after Session 47 refinement):** Sessions 45/46 cohorts re-classified to INFO with cross-cohort context inline. Bonus catch: Session 44 trigger (`no_leader/wta`) auto-demoted HIGH → INFO. Refinement covers the broader cross-cohort cherry-pick + disabled-sport pattern across the board, not just the immediate triggers.
- **Day 3+ (May 2 6 AM ET onward — autonomous):** First runs with Session 48's `concurrent_attack_angles` will start surfacing new attack angle candidates as universe + traded-event-family overlap accumulates over the next ~7 days.

The agent runs read-only on bot data and writes only to `bot/state/discovery/`. It does NOT modify bot state, config, or any production file. Findings drive next-session candidates; auto-fix actions are explicitly out of scope.

**The Discovery Agent is paired with the Strategy Lab (Session 51) for the full "find new attack angles" loop** — agent surfaces candidates daily, lab validates them in seconds, real production scanner ships if validated, that scanner becomes another input to the next day's agent run. See next section.

---

## Strategy Lab

Session 51 (May 2 early hours, no production code) shipped `tools/strategy_lab/` — a rapid-prototyping surface for testing new strategy hypotheses against historical data without writing production code. **Pure-Python — no LLM, no API calls.** On-demand CLI (not scheduled).

The trigger: Session 48's `concurrent_attack_angles` heuristic surfaces candidate new strategies daily. Without a fast prototyping path, acting on a candidate meant building a production scanner + wiring into ACTIVE_STRATEGIES + restarting bot + waiting weeks for trades + settlements before knowing if the idea even works. **Lab cuts that cycle from days/weeks to seconds.**

### How to use

1. Drop a 20-line candidate file in `tools/strategy_lab/candidates/your_idea.py` implementing the `CandidateStrategy` Protocol — a single `evaluate(market, context) → CandidateOpportunity | None` method
2. Run `python3 -m tools.strategy_lab.driver --candidate your_idea --days 14`
3. Read the markdown report at `tools/strategy_lab/reports_out/strategy_lab_your_idea_<date>.md` — hypothetical P&L, per-sport breakdown, reason histogram, top winners/losers, sample tickers
4. If the hypothesis holds: open a follow-up coder session to write a real production scanner. If not: discard the candidate, move on.

### What the lab provides

- **Streaming data loader** — universe.jsonl over a configurable date range, builds `clv_lookup` keyed by ticker + `existing_decisions_by_ticker` for context
- **Evaluator** — for each would-have-bet, finds matching clv records (±N hour window, configurable) and computes hypothetical outcome via settlement-anchored formula
- **Markdown reports** — loud lab-limitations header on every report (settlement-anchored, no slippage, no exit-side, ±N hour clv match window) so reports are never confused for production forecasts
- **Schema discipline** — `test_canonical_schema_used_throughout` is a commit-time guard against the `'no_won'` vs `'no'` anti-pattern that almost falsified Session 45. Future commits introducing the suffix-`_won` shape in lab source files will fail the test
- **Per-pair-key dedup (Session 73)** — `CandidateOpportunity.pair_key: str | None = None`. Stateful candidates that re-emit on persistent divergence MUST set `pair_key` per emit; the lab dedupes by first-emit-wins so per-unique-outcome metrics are the headline. Per-emit metrics stay as a diagnostic line + median emits-per-key amplification ratio (large gap = stateful candidate amplification). One-shot candidates leave `pair_key=None`; per-pair-key Σ exactly equals per-emit Σ — backward-compat locked by Test 3 regression

### What the lab is NOT

- **Not a live-trading harness.** Purely backtest. Outputs are HYPOTHETICAL.
- **Not a parameter optimizer.** Single-candidate evaluation only for v1; no genetic/grid sweeps yet.
- **Not auto-promoted to production.** The Lab is read-only on bot state and only outputs reports. Acting on a Lab finding requires a separate coder session that writes a real scanner.
- **Not scheduled.** Runs on-demand via CLI; not part of the daily 6 AM agent loop.

### Limitations to keep in mind

Hypothetical P&L is back-of-envelope: settlement-anchored, ignores slippage, exit-side logic, partial fills. The ±N hour clv match window is a heuristic; may miss valid matches on long-dated futures. The lab matches against EXISTING clv records — markets the bot never evaluated produce UNRESOLVED tags. **Use the lab to FILTER ideas (any candidate net-negative here is dead). Do NOT use lab P&L as a forecast of production P&L.**

---

## Telegram Interface

The bot communicates exclusively via Telegram. No web UI required for operation.

### Automatic Alerts

| Event | Message |
|-------|---------|
| New opportunity found | Formatted opportunity card with edge %, price, fair value, strategy type |
| Trade executed (paper) | Confirmation with contracts, cost, entry price |
| Take profit trigger | Position up ≥50% — prompt to exit |
| Cut loss trigger | Position down ≥30% — auto-exits in paper mode |
| Market resolved | Result + realized P&L + CLV |
| Morning briefing (8am ET) | Weather scan results + open positions. Catches up if missed (Apr 23 Session 4) |
| Nightly summary (midnight ET) | Daily P&L, trade count, win rate. Persists `total_pnl` to `bot_state.json` |
| Startup drift warning | Logged if `last_morning_briefing` or `last_nightly_summary` is >2 days stale (Apr 23 Session 4) |

### Commands

Commands are case-insensitive and not slash-prefixed. Sent as plain Telegram messages.

**Trading**
| Command | Response |
|---------|----------|
| `GO [n]` | Execute pending opportunity #n (default 1). Also fired by inline GO button |
| `SKIP [n]` | Remove opportunity #n from queue |
| `LIST` / `PENDING` | Show the pending queue |
| `DETAIL [n]` | Full breakdown of pending opportunity #n |
| `SCAN` | Force a scan cycle now |
| `EDGES` | Show current top 3 edges found |

**Position management**
| Command | Response |
|---------|----------|
| `LIVE` / `POSITIONS` | Open positions with unrealized P&L |
| `SELL <ticker>` | Immediate exit |
| `EXITALL` | Exit all open positions |
| `TRAIL <ticker> <pct>` | Set a trailing stop |

**Live game watching**
| Command | Response |
|---------|----------|
| `WATCH <team>` | Start a `LiveGameWatcher` for that query (10s polling) |
| `UNWATCH` | Stop all active watchers |
| `RECAP [date]` | Human-readable journal recap from `live_journal.json` |
| `ANALYZE [date]` | Tick-level dip analysis: what dip size led to profitable exits |

**Status & stats**
| Command | Response |
|---------|----------|
| `STATUS` | Balance, today P&L, total P&L, win rate, open positions, streak |
| `BALANCE` | Raw Kalshi balance check |
| `STATS` | Paper stats with strategy breakdown |
| `HISTORY [n]` | Last n resolved trades (reads `trade_history.json`) |
| `WINRATE` | Overall + per-strategy win rate + ROI |
| `ROI` | Per-strategy ROI table |
| `CLV` | Closing-line value report (active strategies only — Apr 23 Session 5 filter) |
| `MODE` | Current PAPER/LIVE mode + active strategies |

**System**
| Command | Response |
|---------|----------|
| `LOGS` | Tail last 20 lines of `bot/logs/bot.log` |
| `RESTART` | `kill -9 $pid` — watchdog (`run_bot.sh` or launchd) brings it back |
| `STOP` | Unload launchd + kill |

### Pending Queue

Opportunities are written to `bot/state/pending.json` (max 20 entries) with an expiry timestamp set to 2 hours after the market closes. Stale entries are pruned on every scan. This allows GO commands to reference opportunities that were identified several minutes ago without re-scanning.

---

## Configuration Reference

All tunables live in `bot/config.py`. No scattered constants anywhere else.

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `PAPER_MODE` | `True` | Simulate all trades; flip to `False` for live |
| `PAPER_STARTING_BALANCE` | `$10,500` | Simulated starting balance. Bumped 500 → 10,500 on Apr 29, 2026 (+$10,000 deposit). All edge math is balance-invariant; only Kelly sizing + dollar caps scale 21× |
| `MIN_RELATIVE_EDGE` | `0.15` | Skip opportunities below 15% relative edge |
| `KELLY_FRACTION` | `0.25` | Use 25% of full Kelly for sizing |
| `MAX_BET_FRACTION` | `0.05` | Hard cap at 5% of balance per trade |
| `MIN_BET_DOLLARS` | `$1.00` | Don't execute below this cost |
| `MAX_POSITION_PERCENT` | `0.20` | No single position > 20% of balance |
| `MAX_TOTAL_EXPOSURE` | `1.00` | Global cap — up to 100% of equity deployed (Apr 16) |
| `STRATEGY_BUDGETS` | `{vig_stack: 0.60, live_momentum: 0.20, arbs: 0.20}` | Per-strategy exposure caps vs equity (Apr 16) |
| `VIG_STACK_STABLE_FAMILIES` | `{KXHIGHMIA, KXHIGHAUS, KXINX}` | Filter F whitelist — only these vig_stack families trade freely (Apr 18) |
| `VIG_STACK_WEATHER_MIN_PRICE` | `0.93` | Filter F — volatile vig_stack families require NO ≥ 0.93 (raised from 0.90 Apr 20) |
| `MOMENTUM_LEADER_MIN` | `0.65` | Live-momentum entry floor; below this, leader probability isn't strong enough. **Lowered 0.70 → 0.65 on Apr 27 (Session 19c)** based on a tick-replay sweep showing +488¢ test delta over 6 test trades — fragile signal (dominated by 1 trade); auto-revalidating May 18 |
| `MOMENTUM_DISABLED_SPORTS` | `{atp_challenger, wta, wta_challenger}` | Tennis variants blocked from new live-momentum entries. **ATP main tour re-enabled Apr 29 (Session 38a)** after n=56 settled CFs showed +11.32¢ avg CLV / 82% +CLV; May 13 day-14 re-validation queued |
| `CUT_LOSS_THRESHOLD` | `-0.30` | Auto-cut at -30% unrealized P&L. **Vig_stack opp_types exempt (Apr 29 Session 36)** — see Battle Scar #9 in CLAUDE.md |
| `TAKE_PROFIT_THRESHOLD` | `0.50` | Alert at +50% unrealized P&L. **Vig_stack opp_types exempt (Apr 29 Session 36)** — same exemption pattern |
| `MAX_PRICE_MOVE_CENTS` | `3` | Abort GO if price moved >3¢ since alert |
| `NWS_BIAS_CORRECTION` | `1.5°F` | NWS documented warm bias correction |
| `WEATHER_MIN_HOURS_TO_CLOSE` | `8` | Skip same-day weather markets |
| `SCAN_INTERVAL_LIVE` | `120s` | Scan every 2 min when games are live |
| `SCAN_INTERVAL_PREGAME` | `600s` | Scan every 10 min in pregame window |
| `SCAN_INTERVAL_IDLE` | `1800s` | Scan every 30 min when nothing is live |
| `CRYPTO_SCAN_INTERVAL` | `300s` | Crypto-only scan every 5 min |
| `CRYPTO_CACHE_TTL` | `60s` | CoinGecko price cache TTL |
| `PENDING_MAX` | `20` | Max queued opportunities |
| `PENDING_GO_WINDOW_HOURS` | `2` | Opportunity expires 2h after market close |
| `ACTIVE_STRATEGIES` | (list) | Strategy types that trigger trades |

**Credentials** are loaded from `config/` JSON files at startup — never hardcoded:
- `config/kalshi.json` — `api_key_id`, `private_key_path`, `environment`
- `config/telegram.json` — `bot_token`, `chat_id`
- `config/sports_data.json` — Odds API key
- `config/therundown.json` — TheRundown API key (free tier, 20K/day)

---

## Architecture & File Map

```
hustle-agent/
├── bot/
│   ├── main.py              # Entrypoint — asyncio event loop, scan cycle, Telegram. _VIG_STACK_OPP_TYPES constant + extracted _dispatch_position_alerts() with vig_stack TP/SL exemption (Apr 29 Session 36); dead _watchdog_alert machinery removed (Apr 29 Session 37)
│   ├── config.py            # All constants, thresholds, credentials loader. SPORT_PROFILES extended Session 41/42 (TP/SL overrides) + Session 49 (size_multiplier — nba/ufc 0.5x; nhl/mlb 1.0x explicit)
│   ├── scanner.py           # Orchestrator — fans out to all scanners, deduplicates
│   │
│   ├── scanner_weather.py   # NWS forecast → normal distribution → Kalshi edge
│   ├── scanner_sports.py    # Parlay/moneyline edge (NBA, MLB, NHL, NCAAB)
│   ├── kalshi_series.py     # Series game edges + vig stack + crypto ladders
│   ├── econ_scanner.py      # CPI nowcast vs Kalshi economic indicator markets
│   │
│   ├── scanner_sports_arb.py # Monotonicity + consistency riskless arb scanners (ACTIVE)
│   ├── math_engine.py       # All edge math with forward/backward self-checks
│   ├── sizing.py            # Fractional Kelly criterion with uncertainty discount. Session 49 added optional sport kwarg → SPORT_PROFILES.size_multiplier lookup (default 1.0; vig_stack and sport-less paths preserved)
│   ├── executor.py          # Trade execution — safety pipeline, paper + live, STRATEGY_BUDGETS, gate-context-rich reject logging (Apr 24 Session 10), exit_reason persistence on _paper_record_exit (Apr 29 Session 36), Session 50 forward-only confidence/dqs/sport persistence on live_momentum paper_trades (composite confidence formula at executor.py:1050-1086 conditional emits)
│   ├── tracker.py           # P&L tracking, market resolution, CLV settlement (idempotent), per-position MFE/MAE ratchet (Apr 24 Session 9). called_from kwarg for cadence telemetry (Apr 26 Session 17). opp_type threaded onto alert dicts so main.py can filter vig_stack from auto-exits (Apr 29 Session 36)
│   ├── tracker_cadence.py   # Append-only JSONL log of every update_positions invocation — site, ms_since_last_call, num_open_positions (Apr 26 Session 17)
│   ├── clv.py               # Closing-Line Value per strategy + counterfactual records for stratified rejected opportunities (Apr 24 Sessions 6/8); MFE/MAE propagation at settlement extends mfe_cents to clv_cents at settle-time so MFE-vs-exit gap is well-defined (Apr 24 Session 9 + Apr 26 Session 16); paired prediction emission (Apr 25 Session 11)
│   ├── decisions.py         # Per-decision audit log — atomic JSONL append (Apr 24 Session 6)
│   ├── calibration.py       # Per-prediction fair-value log + ±60s settlement matching (Apr 25 Session 11)
│   ├── universe.py          # Per-scan snapshot of every active Kalshi market with scanned_by attribution; two-pass cursor + per-active-series shadow fetch (Apr 25 Session 12)
│   ├── regime.py            # Pure-function regime tagger — time_of_day / day_of_week / sport_phase / event_horizon_hr / match_phase (Apr 25 Session 14, Apr 29 Session 34 added the 5th match_phase axis for tennis/UFC/IPL with state-aware path + elapsed-time fallback)
│   ├── kalshi_history.py    # Settled-market close fetch + permanent cache for back-testing tickers we never traded (Apr 25 Session 13c)
│   ├── order_microstructure.py # Per-live-order lifecycle capture (place / partial fills / terminal). Plumbing-only — empty until PAPER_MODE=False (Apr 25 Session 15)
│   ├── strategies/          # Strategy contract + concrete implementations (Apr 25 Session 13)
│   │   ├── __init__.py      # Two Protocols: Strategy (snapshot-based, vig_stack-style) + TickStrategy (stateful per-tick, live_momentum-style — Apr 26 Session 19a). Market / Tick / State / Buy / Sell / Hold dataclasses
│   │   ├── vig_stack_series.py  # Refactored from scan_vig_stack_series; supports parameter overrides via __init__ kwargs (13c)
│   │   ├── live_momentum.py     # Behavior-preserving port of live_watcher's _tick_momentum into TickStrategy contract; 19a-followup verified 8/8 paper-trade parity within 1¢ tolerance (Apr 26 Session 19a)
│   │   └── nba_game_momentum_strawman.py  # 60-line strawman targeting KXNBAGAME — verifies the contract is general (13c PART 4)
│   │
│   ├── live_watcher.py      # Per-game 10s-tick watcher — live_momentum + live arb; wp_edge proxy + dampened decision logging (Apr 24 Session 7); peak-tracking bug fix at line 2225 (Apr 26 Session 19a-peakfix — TRAILING_STOP can now physically fire); DQS persistence to live_ticks rows (Apr 29 Session 33); elapsed_seconds threaded into mom_ctx for Session 34 match_phase regime axis
│   ├── game_context.py      # Live game intelligence: momentum, win_prob, DQS, instincts
│   ├── position_monitor.py  # Edge-recheck loop for open positions
│   │
│   ├── odds_scraper.py      # DraftKings / Bovada / FanDuel / ESPN / TheRundown cascade
│   ├── crypto.py            # CoinGecko spot price + 30d volatility cache (currently disabled)
│   ├── elo.py               # Team Elo ratings for sports edge adjustment
│   ├── injuries.py          # Injury report integration
│   ├── market_maker.py      # Passive limit-order market-making
│   ├── price_monitor.py     # Line movement detection (5pp threshold)
│   ├── outcome_tracker.py   # Trade outcome logging for calibration
│   ├── notifier.py          # Telegram formatting and HTTP sender
│   ├── patterns.py          # Historical win rate per strategy type (dynamic confidence)
│   ├── scheduler.py         # Cron events (morning briefing, nightly summary, balance reconcile, live_ticks/decisions/predictions/universe/order_microstructure/tracker_cadence rotation)
│   ├── daily_log.py         # Rolling daily performance log
│   ├── state_io.py          # Atomic JSON read/write (write-to-tmp-then-rename)
│   ├── logger.py            # RotatingFileHandler — bot/logs/bot.log, 10 MB × 5
│   └── tools/               # In-package diagnostic scripts (e.g. clv_by_strategy.py)
│
├── tools/                   # Top-level analysis tools (gitignored — local-only by convention)
│   ├── cohort_report.py     # Per-(opp_type, gate) reject-rate + distance-from-threshold histograms; --regime-by axis flag (Apr 24 Sessions 6/10, Apr 25 Session 14)
│   ├── excursion_report.py  # Per-strategy median(MFE − exit) — flags exit-logic candidates; --regime-by axis flag (Apr 24 Session 9, Apr 25 Session 14, Apr 26 Session 16 sign-convention fix)
│   ├── calibration_report.py # Per-strategy mean-bias / Brier score / per-bucket hit-rate; --regime-by axis flag (Apr 25 Sessions 11/14)
│   ├── universe_report.py   # Per-(series prefix, event_type) ignored-vs-scanned breakdown — surfaces market families we don't touch; --regime-by axis flag (Apr 25 Sessions 12/14)
│   ├── backtest.py          # Offline back-tester for snapshot-based Strategy classes; reuses bot.clv.compute_clv_cents (single source of CLV math); --include-history pulls closes for never-traded tickers via bot.kalshi_history (Apr 25 Sessions 13b/13c)
│   ├── hypothetical_report.py # Parameter sweep across N variants of a snapshot Strategy; markdown comparison table sorted by sum_clv_cents (Apr 25 Session 13c)
│   ├── backfill_regime.py   # One-shot script that retroactively tags every existing record with regime via the same pure tagger; idempotent (Apr 25 Session 14)
│   ├── microstructure_report.py # Per-strategy slippage / fill-latency distributions + slippage-adjusted CLV vs paper CLV; flags execution-quality issues. Empty output until PAPER_MODE=False (Apr 25 Session 15)
│   ├── journal_analysis.py  # Reads bot/state/live_journal.json — time-to-exit distribution, exit-reason breakdown, watch-but-no-enter funnel, per-sport split. Surfaced 3 actionable findings about live_momentum (Apr 26 Session 18)
│   ├── exit_replay.py       # Mirrors live_watcher's exit-decision logic; sweeps a single exit-knob across paired bet→exit history. Exit-only — does NOT replay entries (Apr 26 Session 18.5)
│   ├── tick_backtest.py     # Full tick-replay back-tester for TickStrategy classes — replays per-game tick streams, joins to settled clv records, supports --leader-min/--trail-stop sweep grid + 70/30 train/test split + 2¢ slippage pessimism. Reality-checked 8/8 against paper trades within 1¢ (Apr 26 Session 19b/19a-followup)
│   ├── _report_helpers.py   # Shared utilities + 10 parameterized "shared section" renderers used by daily_report.py and weekly_report.py, plus Strategy Candidates renderer for daily_report.py + glint_status.py. _safe_section() failure-tolerance, JSONL streaming with malformed-line tolerance, traceback dedupe, gzip-uncompressed-size for archive comparisons. ~1.1k LOC (Apr 29 Session 35; Strategy Candidates added Session 66)
│   ├── daily_report.py      # Comprehensive daily markdown report — 11 sections, 24h ET window. Health pulse first (6 rows: alive / scanner / decisions / trades / Telegram / errors with ✅/⚠️/🚨 status), then scanner activity, decision audit (cohort), trade activity, CF coverage, live momentum events, DQS+regime distribution, cadence health, errors, state file growth, Strategy Candidates. First I/O writes header so partial reports survive crashes. Output: bot/state/reports/daily/daily_report_YYYY-MM-DD.md. Auto-fires daily 3 AM ET via scheduled-task (Apr 29 Session 35)
│   ├── weekly_report.py     # 16-section weekly synthesis — the 10 daily-shape sections at 7d + 6 weekly-only (week-over-week deltas, buckets, dataset rebuild summary, excursion + exit-replay, calibration findings, retuning candidates). Replaces weekly_digest.py from Session 24 (deleted; Session 24's bot/state/weekly_digest_*.md outputs preserved as historical artifacts). Output: bot/state/reports/weekly/weekly_report_YYYY-WNN.md. Auto-fires Sundays 6 AM ET via scheduled-task (Apr 29 Session 35)
│   ├── live_momentum_dataset.py # Per-tick decision dataset joining live_ticks + live_journal + clv into one tabular surface. One row per candidate decision (accept AND tunable-reject from Session 21 + 23). Forward returns at 30/60/120s + MFE/MAE in fixed window. Leakage property test enforced. CF-only fallback added Session 30-followup-2 (Apr 29) — recovered 84% of CFs that the original join-by-tick logic was dropping. Output: bot/state/research/live_momentum_dataset.csv (Apr 28 Session 30)
│   ├── live_momentum_buckets.py # Markdown bucket reports across 7 single dimensions (sport, leader_price, dip, wp_edge, dqs, game_phase, spread) + 4 interaction tables. Marks n<5 as low-confidence. Authored Findings section (Apr 28 Session 30). Investigation #1 (Apr 29 night) re-ran on regenerated 714-row dataset and surfaced the trigger findings for Sessions 38a-e
│   ├── tick_backtest.py            # (already documented above) — extended in Session 42 with SWEEP_GRID_TP_SL_PER_SPORT for per-sport TP/SL sweeps
│   └── discovery_agent/            # Autonomous heuristic discovery agent — daily 6 AM ET via launchd; pure Python, NO API calls (May 1 Sessions 43a + 43b). Tracked via .gitignore exception (tools/ generally gitignored, discovery_agent/ specifically un-ignored)
│       ├── main.py                 # Entry point — loads context, runs heuristics, writes outputs (markdown report + JSONL findings)
│       ├── context.py              # DiscoveryContext dataclass — pre-loads ALL bot data sources once (eager-loaded for small/medium files; streaming iterator factories for live_ticks.jsonl + bot.log + 39MB universe.jsonl)
│       ├── findings.py             # Finding dataclass + fingerprint hash + cross-run NEW/STABLE/RESOLVED dedup
│       ├── _sport_classifier.py    # Sport-from-ticker wrapper extending bot's _TICKER_PREFIX_TO_SPORT to distinguish futures (KXMLB-*) from per-game (KXMLBGAME-*) — Session 43b refinement
│       ├── heuristics/             # 10 pluggable heuristics, each with declared data_sources + isolated try/except in main.py
│       │   ├── base.py             # Heuristic Protocol
│       │   ├── outlier_pnl.py      # Single trades dominating cohort P&L (catches SFPHI; Session 43a)
│       │   ├── cohort_emergence.py # New (opp_type, sport) cohorts emerging in last 7d vs prior 30d. Refined in 43b: emits unique_tickers_recent + accepts_recent + paper_trades_recent evidence; severity demotes to info when accepts==0 AND trades==0
│       │   ├── threshold_proximity.py    # Reject-gates clustered <5% from threshold (catches the non_stable_below_weather_floor lead pattern; Session 43b)
│       │   ├── counterfactual_hotspots.py # (skip_reason, sport) buckets with high settled-CF +CLV (automates Session 38a manual ATP query; Session 43b — produced 3 HIGH findings on day-2 run including the WTA lead). REFINED Session 47: cross-cohort context evidence + 3-trigger severity demotion ladder (cross-cohort mean<0 / trimmed mean<3¢ AND raw≤0 / disabled-sport) — auto-demotes Sessions 45/46-shape cherry-picks
│       │   ├── universe_gap.py     # Sport+market_type pairs in recent decisions but absent from current universe.jsonl (Session 43b — reframed without archive dir)
│       │   ├── live_tick_anomalies.py    # STREAMING: tickers with ≥3 jumps ≥15¢ (Session 43b)
│       │   ├── cadence_outcome.py  # Per-cadence-bucket P&L outliers — measures Session 39 territory in P&L terms (Session 43b)
│       │   ├── log_error_spike.py  # STREAMING: 3×+ recent error-rate ratio over 7d baseline (Session 43b)
│       │   └── concurrent_attack_angles.py # Session 48 — search-frontier expander. Two finding types: concurrent_fire_candidate (ALREADY_TRADING + SCANNED_NOT_TAKEN sibling markets where not-taken side shows positive concurrent CLV when primary wins) + scanner_gap (event families where we trade some markets but never scan others). Same Session 47 cross-family demotion ladder applied at strategy-pair × event-family level. The bot's own engine for new attack angles
│       └── README.md               # Tunables table per heuristic + "how to add a heuristic" + report dedup explanation + Canonical Data Schema mirror (Session 45 post-correction)
│
├── tools/strategy_lab/          # Session 51 — rapid hypothesis prototyping for new strategies. Tracked via .gitignore exception (tools/ generally gitignored). On-demand CLI, no schedule.
│   ├── __init__.py
│   ├── README.md                # how to write a candidate, how to read a report, lab limitations, canonical schema reference
│   ├── candidate.py             # CandidateStrategy Protocol + CandidateOpportunity dataclass — the contract for user-written candidates
│   ├── data_loader.py           # streams universe.jsonl over date range; loads clv_lookup keyed by ticker; existing_decisions_by_ticker for context
│   ├── evaluator.py             # for each would-have-bet, finds matching clv records (±N hour window), computes hypothetical P&L via settlement-anchored formula, aggregates per-sport + reason histogram + top winners/losers
│   ├── driver.py                # CLI entry: python3 -m tools.strategy_lab.driver --candidate <name> --days <N> --report-out <dir>
│   ├── reports.py               # markdown rendering with loud lab-limitations header (settlement-anchored, no slippage, no exit-side, ±N hour clv match window)
│   ├── candidates/              # gitignored — user-written candidates (except example + __init__.py + .gitkeep)
│   │   ├── __init__.py
│   │   ├── example_total_points_under.py  # reference implementation showing the contract
│   │   └── .gitkeep
│   └── reports_out/             # gitignored — markdown reports go here
│       └── .gitkeep
│
├── agent/
│   ├── kalshi_client.py     # Kalshi REST API (used by bot for all API calls)
│   ├── parlay.py            # Parlay title parser + multi-leg pricer
│   ├── player_stats.py      # Player prop probability estimator
│   ├── engine.py            # Original reasoning loop (Claude API)
│   └── ...
│
├── bot/state/               # Runtime state (gitignored)
│   ├── bot.lock             # PID lockfile; touched every 30s by dedicated _heartbeat_loop task (Apr 24 Session 7); per-scan touch retained as belt-and-suspenders
│   ├── bot_state.json       # Scan count, session stats, heartbeat, last_*_rotation flags, total_pnl
│   ├── positions.json       # All open + resolved positions; carries mfe_cents/mae_cents/mfe_at/mae_at/ticks_observed (Apr 24 Session 9); regime tagged (Apr 25 Session 14)
│   ├── paper_trades.json    # Paper RESOLUTION log — balance reconstructed from this. Ground truth
│   ├── trade_history.json   # ORDER log — every execute_trade/execute_hedge appends here. Distinct from paper_trades
│   ├── pending.json         # Queued opportunities with expiry
│   ├── clv.json             # CLV records per trade. _load() filters to active strategies (Apr 23 Session 5). Also stores counterfactual records (status=counterfactual_open|counterfactual_settled, trade_id=CF-{scan_id}-{ticker}) for stratified rejected opportunities (Apr 24 Sessions 6/8). Settled records carry MFE/MAE (Apr 24 Session 9). Every record regime-tagged (Apr 25 Session 14)
│   ├── decisions.jsonl      # Per-decision audit log (Apr 24 Session 6). Every scan-time accept and reject with {ts, ticker, opp_type, edge, gates, decision, reason, extra, regime}. extra carries gate-specific distance-from-threshold context (Apr 24 Session 10); regime tags time_of_day/day_of_week/sport_phase/event_horizon_hr (Apr 25 Session 14). Daily rotation to archive/
│   ├── predictions.jsonl    # Per-prediction fair-value vs. actual log (Apr 25 Session 11). One row per opp evaluated (real trade or CF). Brier-scored by tools/calibration_report.py. Regime tagged (Apr 25 Session 14)
│   ├── universe.jsonl       # Per-scan snapshot of every active Kalshi market (Apr 25 Session 12). Schema {ts, scan_id, ticker, series_ticker, event_ticker, status, close_ts, yes_ask, yes_bid, no_ask, no_bid, volume_24h, open_interest, scanned_by[], regime}. Empty scanned_by = no active strategy looked at this market. Read by tools/universe_report.py and tools/backtest.py (Session 13). Daily rotation
│   ├── order_microstructure.jsonl  # Per-live-order lifecycle (Apr 25 Session 15). EMPTY until PAPER_MODE=False — paper trades intentionally produce zero rows. Schema includes ts_placed/ts_filled/ts_canceled, requested vs filled price+qty, signed slippage_cents (positive = adverse), latency_ms, partial_fill_count, terminal_status, slippage_source enum. Read by tools/microstructure_report.py
│   ├── tracker_cadence.jsonl  # Per-call observation log of tracker.update_positions (Apr 26 Session 17). Schema {ts, called_from, num_open_positions, ms_since_last_call}. Confirms _position_check_loop fires every ~30s vs main_loop's 30-min idle scan_interval — the diagnostic that surfaced the cadence-limited bug
│   ├── research/              # Local-only research artifacts (gitignored)
│   │   └── live_momentum_dataset.csv  # Per-tick decision dataset (Apr 28 Session 30). Regenerable via tools/live_momentum_dataset.py
│   ├── reports/               # Daily + weekly automated report outputs (Apr 29 Session 35)
│   │   ├── daily/daily_report_YYYY-MM-DD.md     # Daily 3 AM ET report — health pulse + 10 data sections including Strategy Candidates
│   │   └── weekly/weekly_report_YYYY-WNN.md     # Weekly Sunday 6 AM ET synthesis — 10 daily-shape sections + 6 weekly-only
│   ├── discovery/             # Discovery agent outputs (May 1 Session 43a + 43b)
│   │   ├── discovery_report_YYYY-MM-DD.md       # Daily 6 AM ET report — NEW / STABLE / RESOLVED findings from 8 heuristics
│   │   └── discovery_findings_YYYY-MM-DD.jsonl  # Machine-readable findings (one Finding per line) — drives next-run dedup
│   ├── cache/                # Permanent caches for settled-market data (Apr 25 Session 13c)
│   │   └── kalshi_settled_closes.json  # Ticker → closing_yes_price for back-testing tickers we never traded. Settled markets never change so cache is permanent
│   ├── strategy_audit.json  # Per-strategy status + settlement_log (idempotent, Apr 18; rebuilt Apr 20 Session 1)
│   ├── live_journal.json    # Live-watcher events: scan_found, bet, exit, session_end
│   ├── live_ticks.jsonl     # Enriched per-tick log: price, wp, momentum, DQS, game_state, espn_scores
│   ├── archive/             # Daily gzipped JSONL archives — created by scheduler at midnight ET
│   │   ├── live_ticks-YYYY-MM-DD.jsonl.gz           # (Apr 23 Session 5)
│   │   ├── decisions-YYYY-MM-DD.jsonl.gz            # (Apr 24 Session 6)
│   │   ├── predictions-YYYY-MM-DD.jsonl.gz          # (Apr 25 Session 11)
│   │   ├── universe-YYYY-MM-DD.jsonl.gz             # (Apr 25 Session 12)
│   │   ├── order_microstructure-YYYY-MM-DD.jsonl.gz # (Apr 25 Session 15)
│   │   └── tracker_cadence-YYYY-MM-DD.jsonl.gz      # (Apr 26 Session 17)
│   ├── patterns.json        # Historical win rate per strategy type (dynamic confidence)
│   ├── outcomes.db          # SQLite: alert → outcome log for calibration
│   ├── elo_ratings.json     # Sport ELO ratings
│   ├── daily_log.json       # Rolling daily performance snapshot
│   └── mm_positions.json    # Market-making pair tracker
│
├── bot/logs/
│   └── bot.log              # Rotating log — tail this for real-time status
│
├── tests/                   # pytest suite (see Testing section for current count)
│   ├── test_bot_executor.py    # Execution pipeline, position limits, cooldowns, STRATEGY_BUDGETS
│   ├── test_bot_scanners.py    # Scanner math, weather parsing, series matching
│   ├── test_bot_tracker.py     # P&L resolution, paper trade updates, CLV, settlement idempotency
│   ├── test_bot_improvements.py # Edge cap, cooldown, logging fixes
│   ├── test_live_watcher.py    # Watcher start/stop, tick processing, exit paths
│   ├── test_sport_instincts.py # Per-sport instinct filters (avoid_entry etc.)
│   ├── test_instincts.py       # Heuristic edge detection
│   ├── test_data_driven_fixes.py # Regression guard for Apr 14/16/18 tuning decisions
│   ├── test_kalshi.py          # Kalshi client mocking and response parsing
│   ├── test_parlay.py          # Parlay leg parsing and pricing math
│   ├── test_player_stats.py    # Player prop estimator
│   ├── test_to_8.py            # Batch improvements and regressions
│   └── test_agent.py           # Original agent subsystems (legacy, kept for kalshi_client coverage)
│
├── ui/                      # React dashboard (Vite + React + TypeScript + Tailwind)
│   ├── server/index.ts      # Express API — reads state files, serves JSON
│   └── src/                 # 10 pages covering positions, P&L, CLV, strategies
│
├── REPORT_CALENDAR.md       # Index of all scheduled routines (recurring + one-offs) with Last Run stamps. Created Apr 29 to surface stale routines visually — if a stamp is more than one cadence-window old, that routine missed a fire (Apr 29 Session 35 supporting artifact)
├── CLAUDE.md                # Project guide for Claude Code (sessions narrative, gotchas, runbooks)
└── README.md                # This document — deep prose overview, kept in sync per CLAUDE.md style rules
```

---

## Running the Bot

### Prerequisites

```bash
pip install -r requirements.txt
```

Populate `config/`:
```bash
config/
├── kalshi.json          # {"api_key_id": "...", "private_key_path": "kalshi-private-key.pem"}
├── kalshi-private-key.pem
├── telegram.json        # {"bot_token": "...", "chat_id": "..."}
├── sports_data.json     # {"api_key": "..."}    ← Odds API key (optional)
└── therundown.json      # {"api_key": "..."}    ← TheRundown (optional, free tier)
```

### Start

```bash
# Foreground (recommended for first run — watch the logs directly)
cd hustle-agent
python3 -m bot.main

# Background
nohup python3 -m bot.main > /tmp/glint.log 2>&1 &

# Follow the log
tail -f bot/logs/bot.log
```

The bot will:
1. Load any pending opportunities from disk
2. Reconcile `positions.json` against the live Kalshi API
3. Send a Telegram startup message
4. Begin the first scan cycle immediately
5. Start `_live_scan_loop()` on a 60s cadence (spawns `LiveGameWatcher` tasks for live matches)
6. (`_crypto_scan_loop` is wired up but gated off by `CRYPTO_ENABLED = False` — no work happens there until re-enabled)

### Stopping

Send `SIGTERM` or `SIGINT` (Ctrl-C). The bot catches both signals, releases `bot/state/bot.lock` via the shutdown handler, sends a Telegram shutdown message, and exits cleanly. If the process is mid-scan or wedged on I/O it can take a while to actually exit (the lock file is released early in the handler, so a missing lock + a still-alive PID is a known intermediate state) — escalate with `kill -9 <pid>` if it doesn't exit within ~30s.

The Telegram `STOP` command is the same path plus an `unload` against launchd; `RESTART` is `kill -9` and relies on `run_bot.sh` (or launchd) bringing the bot back.

---

## Testing

```bash
python3 -m pytest tests/ -q
# 1,309 tests passed, 0 failed (May 1 baseline post-Session 43b)
# Was 1,165 baseline post-Session-37 hygiene cleanup; +74 from Session 43a/43b discovery agent tests + ~70 from Sessions 38a/39/40/41/42 net additions.
# 108 of those tests live under tests/test_discovery_*.py (Session 43a + 43b — covers context/findings/isolation/SFPHI-regression and per-heuristic positive/negative/boundary/missing-source + 2 streaming memory-safety tests).
# All tests mock external APIs — no real Kalshi calls, no CoinGecko, no sportsbook requests, no Telegram messages.
```

> **Test baseline went CLEAN Apr 29 evening (Session 37 + Session 36-followup) and has stayed clean since.** The 12 documented pre-existing failures that had been accumulating since Apr 14 — Group A `__new__()` bypass-init AttributeError pattern (`test_status_card_shows_bet_placed`, `test_session_summary_includes_exits`), Group B stale config pins (`test_position_limit_fail_aborts`, `test_eth_in_active_strategies`, `test_weather_min_price_constant`, `test_volatile_family_at_weather_floor_survives`, `test_ticker_exceeding_daily_loss_blocked`), Group C silenced watchdog (`test_watchdog_alerts_on_stale_heartbeat`, `test_watchdog_no_alert_when_fresh`), Group D per-day cap (`test_per_day_cap_resets_at_utc_midnight`), and the 2 Session-34 backfill_regime test oversights (Group E) — were paid down across two surgical sessions. Group C also removed the dead `_watchdog_alert` machinery from `bot/main.py` (logger.warning preserved as the sole watchdog signal). Every session since (38a, 38a-2, 39, 40, 41, 42, 43a, 43b) shipped with "0 failures unchanged" — any new failure is a real regression worth investigating.

All tests mock external APIs — no real Kalshi calls, no CoinGecko, no sportsbook requests, no Telegram messages. The test suite covers:

- **Executor:** balance check (paper and live), position limit enforcement, 4-hour cooldown, price movement kill switch, direction verification, paper trade lifecycle, `STRATEGY_BUDGETS` (Apr 16), Filter F gate (Apr 18 → 0.93 Apr 20)
- **Scanners:** weather normal distribution math, NWS response parsing, city alias mapping, series game edge calculation, vig stack detection, crypto log-normal model
- **Tracker:** market resolution logic, P&L computation, paper_trades.json update on settlement, settlement-log idempotency (Apr 18), `exited_early` settlement pipeline + `record_resolution` (Apr 20 Session 1)
- **Scheduler (Apr 23 — `test_scheduler.py`, 19 tests):** morning briefing fire-at-8am-or-catch-up, nightly summary midnight + missed-day catch-up, balance reconcile at 21:00, `total_pnl` persistence, `live_ticks.jsonl` midnight rotation + collision-suffix + skip-if-too-small
- **Live watcher:** watcher start/stop, tick-level dip + DQS + variance_quality_gate (Tier 2.4), conviction-entry gating, exit paths (take-profit, trail, stop, near-settle), sport-instinct avoid_entry guards, `MOMENTUM_DISABLED_SPORTS` `can_enter` gate (Apr 20)
- **Sizing:** Kelly formula correctness, fractional cap, uncertainty discount, dollar floor/ceiling
- **CLV:** entry recording, settlement computation (YES and NO sides), report generation, active-strategy filter at `_load` (Apr 23), counterfactual schema + idempotency + stratified selection (Apr 24 Sessions 6/8), MFE/MAE propagation at settlement (Apr 24 Session 9)
- **Parlay:** title parsing for multi-leg contracts, edge calculation with correlation discount
- **Closed-loop instrumentation (Apr 24–25 — Sessions 6–11):** `test_decisions.py` (atomic JSONL append under contention, schema integrity, dampener), `test_tracker.py` (10 MFE/MAE cases — side-aware ratchet, lazy-init, monotonic, settlement propagation), `test_cohort_report.py` (distance-from-threshold histogram math), `test_calibration.py` (31 cases including Brier handcraft, ±60s settlement matching window, idempotency), `test_main.py` (heartbeat lock-touch task), `test_scheduler.py` extensions (decisions + predictions rotation)
- **Pivot-enabling instrumentation (Apr 25 — Sessions 12–15):** `test_universe.py` (snapshot schema, MVE filter, scanned_by attribution, partial-cursor tolerance), `test_vig_stack_series_strategy.py` (12-case golden-file regression locking VigStackSeries == legacy scan_vig_stack_series byte-identical), `test_strategies.py` (parameter override flow into evaluate gate decisions), `test_backtest.py` (replay loop, ±60s clv join, --include-history fallback, --verify-against-clv-report mode, no-parallel-codepath assertion), `test_kalshi_history.py` (12 cases — settle-result branches, finalized-status regression, cache behavior), `test_hypothetical_report.py` (parameter sweep markdown rendering), `test_nba_game_momentum_strawman.py` (9-case strawman strategy contract verification), `test_regime.py` (42 cases — DST boundaries, all 7 days, sport phase transitions, event_horizon buckets, 100x determinism property), `test_backfill_regime.py` (idempotency, dry-run, gzipped archive handling), `test_order_microstructure.py` (10+ cases — placement / partial / terminal / synchronous-rejection paths, paper-mode untouched, daily rotation)
- **Strategy iteration arc (Apr 26-27 — Sessions 16–19):** `test_excursion_report.py` (29 cases — 100-tuple property test for gap math, sign-convention regression after Session 16's MFE-extension-at-settlement fix), `test_journal_analysis.py` (38 cases covering schema-tolerance + per-sport aggregation), `test_exit_replay.py` (24 cases including the smoking-gun trail=3 vs trail=8 differential test), `test_live_momentum_strategy.py` (20 fixture-based golden-file tests — 5 hand-crafted scenarios × actions/log_calls/state assertions; faithfully preserves production peak-tracking bug then verifies trail-stop fires post-fix), `test_live_momentum_strategy_helpers.py` (5 helper unit tests for non-obvious return shapes), `test_tick_backtest.py` (28 cases including parity classification with COVERAGE_GAP semantics, qty_override flag for parity, 7-case sweep grid + train/test split coverage), `tests/test_live_watcher.py` regression for peak-tracking fix at line 2225
- **Regression guards:** `test_bot_improvements.py` + `test_data_driven_fixes.py` lock in the specific fixes from the Apr 14/16/18/20 audits (edge cap, cooldown, UW-exit removal, SCORE-FLIP momentum gate, Filter F, tennis disable, etc.) so they cannot silently regress

---

## Dashboard UI

> **Status:** The React dashboard under `ui/` is **legacy and not actively maintained.** The bot is operated entirely via Telegram. The static `bot/dashboard.html` file is a lighter read-only view that works without a Node toolchain.

A React dashboard reads all state files and shows the bot's activity in real-time.

```bash
cd ui && npm install && npm run dev
```

Starts:
- Express API server on port `3001` (reads `../bot/state/` files)
- Vite dev server on port `5173`
- Polls every 5 seconds

| Page | What it shows |
|------|--------------|
| Command Center | Paper balance, open position count, scan stats, recent activity feed |
| Finances | Full paper trade ledger, balance over time, realized P&L |
| Strategies | Per-strategy win rate, ROI, and CLV stats |
| Positions | Open positions with current bid, unrealized P&L, entry detail |
| CLV | Closing-Line Value breakdown by strategy and timeframe |
| The Dream | GPU fund progress (original agent concept) |
| Journal | Agent diary entries |
| Chat | Message history |

---

## Design Principles

A few decisions that shaped the system:

**No prediction is better than a wrong prediction.** Every strategy either has an external reference model (NWS, sportsbook moneyline, CoinGecko + log-normal) or exploits a structural pricing property (vig stack). There's no pure sentiment or momentum trading.

**Paper mode is a first-class mode, not a toggle.** Balance accounting, exit simulation, and CLV tracking all work identically in paper mode. Going live is a one-line config change, not a system change.

**CLV gates live trading, not win rate.** Win rate is a lagging indicator and is heavily influenced by contract resolution luck. CLV tells you whether the market confirmed your edge in real-time. A strategy with 60% win rate and negative CLV is getting lucky. A strategy with 45% win rate and positive CLV is finding real edge.

**Math self-checks are not optional.** The bot runs forward/backward verification on every edge calculation. The cost is microseconds per trade. The benefit is catching sign errors, unit errors, and inversion bugs before they execute real orders.

---

## Recent Improvements (Apr 20–May 7)

Eleven arcs.

**Apr 20–23 — Redemption (Sessions 1–5).** The Apr 20 state audit surfaced 12 issues across real bugs, tuning opportunities, and dead weight. Bundled into 5 focused sessions, all shipped.

**Apr 24–25 — Closed-loop data collection (Sessions 6–11).** With the bot stable, the missing piece for retuning was outcome attribution: the trade log told us *what fired* but not *what almost fired and was killed by which gate, and what would have happened if we'd taken it anyway*. Sessions 6–11 instrument the bot end-to-end so gate calibration becomes a regression problem instead of folklore. All shipped.

**Apr 25 — Pivot-enabling instrumentation (Sessions 12–15.5).** Sessions 6–11 made the bot able to *tune itself* inside its existing strategy frame. The remaining question — "are the strategies we're running the right ones?" — required different instrumentation: capture the universe of markets we ignore, build a strategy contract + back-tester so alternatives can be evaluated without going live, tag every record with regime context, capture live-order microstructure (deferred verification), and a final hardening pass (15.5) closing silent-corruption gaps before the week-long unattended run. All shipped; the bot now genuinely supports evaluating alternatives, with airtight observability.

**Apr 26–27 — Strategy iteration arc (Sessions 16–19).** The first run of `excursion_report.py` on Apr 25 surfaced two real findings (a math bug in the gap computation; a structural cadence gap for live_momentum positions where 90% had `ticks_observed=None` because `update_positions` was paced by IDLE scan_interval) and a third gap by absence (`live_journal.json` sitting unread). This arc closed those, then attempted the live_momentum tick-replay back-tester. Highlights: Session 18 surfaced 3 actionable findings from the journal, **Session 18.5 ruled out a cheap exit-tuning shortcut AND removed 3 dead config constants** in the same session, **Session 19a discovered a chicken-and-egg peak-tracking bug at `bot/live_watcher.py:2225` that meant TRAILING_STOP physically couldn't fire** (peak_values was never written), and **Session 19c shipped `MOMENTUM_LEADER_MIN: 0.70 → 0.65` with an honest single-trade-dominance caveat + auto-scheduled May 18 re-validation routine**. The discipline that made this arc successful: when Session 19b's parity check came back 0/9, we audited the back-tester instead of papering over the failure — and discovered the back-tester (not the port) had bugs that produced false negatives, including an artifact that had created a "load-bearing bug" hypothesis we almost shipped a code change against. Real reversals, real intellectual honesty.

**Apr 27–29 — Pre-checkpoint coverage arc (Sessions 21, 23, 24, 28, 29, 30, 33, 34, 35).** Continuation of strategy iteration explicitly framed around making the May 2 retuning checkpoint analytically rich. Driving principle (per user): "in paper mode, failure is the price of the data — collect everything we can, find an edge faster, make money sooner." Highlights: Session 21 closed Session 18 Finding #3's watch-but-no-enter black hole with 11-gate `skip_reason` instrumentation; Session 23 added live_momentum counterfactuals (mirrors Session 8's vig_stack pattern, ~10×/day rate, per-(sport,skip_reason)-per-day cap honored); Session 24 shipped `weekly_digest.py` (later superseded by Session 35); Sessions 28 + 28-followup hit the Kalshi API ceiling on universe enumeration (Outcome D — pushed deadline 90→300; Session 28-2 architecture rewrite filed on watch list); Sessions 29 + 29-followup fixed a JSONDecodeError-forever silent-corruption bug in `_journal_append` that had stopped journal writes for 27.5 hours; Session 30 shipped the `live_momentum` research dataset + bucket reports (followup-2 same day fixed an 84%-missing-CFs bug that was hiding most of the actual signal); **Session 33** persisted DQS to live_ticks rows so the bucket dimension is no longer empty; **Session 34** added `match_phase` as the 5th regime axis (set/round/over for tennis/UFC/IPL) — closes the structural Unknown-game-phase gap for sports without ESPN scoreboard support; **Session 35** shipped `tools/daily_report.py` + `tools/weekly_report.py` + `tools/_report_helpers.py` — the comprehensive automated reporting surface that runs daily 3 AM ET and weekly Sundays 6 AM ET under `bot/state/reports/{daily,weekly}/`, retiring the original `weekly_digest.py`.

**Apr 29 — Hygiene + ATP re-enable (Sessions 36, 36-followup, 37, 38a).** Long single evening covering the discipline cycle from "the weekly report flagged X" through "ship a fix" through "verify the fix didn't break anything" through "investigate the next weekly-report-flagged thing." **Session 36** extended Battle Scar #9's vig_stack auto-exit exemption to cover `take_profit` and `cut_loss` (only `edge_flipped` was exempt pre-36) — surfaced when Session 35's first weekly report flagged `non_stable_below_weather_floor` as mis-tuned (+24.38¢ mean rel-CLV on rejects) and investigation revealed the floor was actually doing its job by blocking entries that would get killed by an inappropriate cut_loss path before settlement (32% of vig_stack trades were exiting early at mean −$5 to −$10 across ALL families). Same session shipped `exit_reason` persistence on `paper_trades.json`. **Session 36-followup** fixed two Session-34 backfill_regime test oversights (those were genuinely-new failures hidden in the "documented pre-existing baseline" noise). **Session 37** cleaned up the remaining 10 documented pre-existing test failures via group-paid-down approach (Group A `__new__()` bypass, Group B stale config pins, Group C silenced watchdog dead-code removal, Group D per-day-cap monkeypatch). **Session 38a** re-enabled ATP main tour after Investigation #1 surfaced n=56 settled CFs at +11.32¢ avg CLV — same asymmetric-evidence pattern Session 30-followup flagged for wta_challenger.

**Apr 29+ — Evidence-Driven Retuning Arc (Sessions 38a–e — 38a + 38a-2 shipped, 38b/c/d/e queued).** Sessions 12–37 built the eyes (instrumentation) and the reactive loop (find what's broken → fix it). Session 38+ opens the *active interrogation* loop. **Investigation #1 (Apr 29 night)** re-ran `tools/live_momentum_buckets.py` against the regenerated dataset (714 rows, 321 with settled CLV) and surfaced 3 real signals + 2 hidden infrastructure gaps. ATP main-tour re-enable shipped Apr 29; **WTA main-tour re-evaluation (Apr 30 — Session 38a-2)** held disabled as Outcome B on insufficient evidence (now contradicted by Session 43b discovery agent finding — candidate Session 44); IPL sport-disable (Session 38b), `MOMENTUM_LEADER_MAX` ceiling for >=90¢ leaders (Session 38c), match_phase axis wiring (Session 38d), bucket report n-column split (Session 38e) all queued. Discipline: each session ships independently to preserve attribution per-change.

**Apr 30 — Stability + Investigation Arc (Sessions 39, 40, 41, 42).** A 12-hour bot wedge on Apr 30 (asyncio event loop blocked by sync `snapshot_universe`) drove the most critical fix in the project's history. **Session 39 (CRITICAL)** wrapped snapshot_universe in `loop.run_in_executor(None, fn)` at `bot/main.py:1188-1196` plus a defense-in-depth deadline check inside the universe.py retry loop. Bot resumed full operation including live_watcher notifications. **Sessions 40 + 41 + 42** systematically investigated live_momentum's W:L=0.261 EE rate across three exit-side framings: Session 40 (exits-balanced — exits aren't the leak), Session 41 (global TP/SL ratio sweep — flat across SL axis, structural), Session 42 (per-sport TP/SL variants — same flat shape per sport, plus architectural audit revealed `bot/live_watcher.py:2362,2454` resolves TP/SL via `sport_profile.get(...)` first which had been shadowing global sweeps). All three produced Pattern C (statistically inconclusive, doc-only). The collective finding: W:L=0.261 is structural and not addressable by exit-side parameter tuning at any granularity tested. **Sizing (Kelly cap on high-confidence-but-losing trades)** is now the strongest queued candidate that hasn't been opened — Session 40 surfaced "7 lost-class trades net −$91.68 with avg −$13.10 vs avg win +$3.41" — the asymmetric-loss multiplier.

**May 1 morning — Discovery Agent Arc (Sessions 43a, 43-investigate, 43b).** The SFPHI investigation crystallized a longstanding gap: real leads were being surfaced by Tyler-pings-about-Telegram-notifications, not by systematic scanning. **Session 43a** built the framework (DiscoveryContext + Findings + Heuristic Protocol + main orchestrator with isolation) on a `tools/discovery_agent/` chassis, plus 2 SFPHI-catching heuristics and a P0 regression test that locks the founding example. Pure Python, no API calls (Tyler veto). Daily 6 AM ET via launchd. **Session 43-investigate** decomposed the cohort_emergence finding from 43a's first run: confirmed SFPHI is a singleton (5 decisions, 1 trade — don't lean in), AND surfaced a separate real lead (`non_stable_below_weather_floor` rejecting +EV opportunities on sports futures — 417 such rejections in 2 days, e.g. KXNBA-26-OKC at edge=+11.48¢ rejected on no_ask_prob=0.48 vs floor=0.93). **Session 43b** added the remaining 6 heuristics (threshold_proximity, counterfactual_hotspots, universe_gap, live_tick_anomalies, cadence_outcome, log_error_spike) on the proven chassis + folded in the cohort_emergence refinement. Day-2 e2e produced 3 HIGH `counterfactual_hotspots` findings — most actionable: `no_leader/wta` +20¢ CLV on n=15, directly contradicting the Session 38a-2 WTA disable hold decision.

**May 1 afternoon — Discipline Cycle + Agent Self-Improvement + First P&L Intervention Arc (Sessions 44, 45, 45-correction, 46, 47, 48, 49).** The discipline of HOLD outcomes + the discovery agent's self-improvement loop + the first production code change today aimed at the measured P&L leak. **Session 44** gate-flow walked the WTA `no_leader` finding from the morning's run — confirmed the agent's signal was real but the lever was leader-detection tuning (not sport disable), so Session 38a-2's HOLD on WTA stands. **Session 45** investigated `no_vol_growth_first_seen` as a retuning candidate (HIGH counterfactual_hotspots from 43b morning run); shipped Outcome C HOLD because the gate is a binary cycle-delay (no tunable threshold) AND cross-cohort distribution flat. **Session 45 post-session correction** caught a verification-query schema bug (`market_result == 'no_won'` vs canonical `'no'`) and added the **Canonical Data Schema Reference** section pinned at the top of CLAUDE.md (mirrored to `tools/discovery_agent/README.md`). Single source of truth for `clv.json` / `decisions.jsonl` / `paper_trades.json` field names + value enums — every future session prompt that touches state files reads this as Step 0. **Session 46** investigated `no_vol_growth_idle` (the companion gate, this one IS tunable per `bot/live_watcher.py:3140` magic-number `< 500`) — shipped Outcome C HOLD because cross-cohort mean is flat AND top positive sports are in MOMENTUM_DISABLED_SPORTS (gate-tuning structurally neutralized). **Session 47** refined `counterfactual_hotspots` with cross-cohort context evidence + 3-trigger severity demotion ladder (cross-cohort mean<0 / trimmed mean<3¢ AND raw≤0 / disabled-sport) — auto-handles the Sessions 45/46 cherry-pick failure mode going forward + as bonus catch demoted Session 44's no_leader/wta finding HIGH→INFO. **Session 48** added the 9th discovery heuristic `concurrent_attack_angles` per Tyler's directive ("the bot should find new attack angles itself") — surfaces (a) `concurrent_fire_candidate` findings where the same event has multiple market types our existing strategies could attack concurrently, and (b) `scanner_gap` findings where event families have NEVER_SCANNED market types worth building a scanner for. Search-frontier expander. **Session 49** shipped the day's first production-code intervention: per-sport `size_multiplier` on live_momentum, sizing **NBA + UFC down to 0.5×** based on measured per-sport bleed (NBA n=21 −$26.57 48% WR; UFC n=8 −$8.30 12% WR). NHL + MLB explicitly held at 1.0× (n too thin to size up); IPL deferred (Session 38b queued to disable). Bot restarted; Battle Scar #3 protocol caught + killed an orphan PID from the prior day. May 15 +14d re-validation routine scheduled. **Net of the arc:** the agent is now smarter (auto-handles cherry-picks), wider (9 heuristics including new-strategy surfacer), and the bot's measured-bleeding cohorts are actively being de-risked. Honest P&L position remains: bot at +$18.78 with SFPHI carry; strip the singleton and we're at −$153.74. Bot isn't profitable yet — we're collecting information to get there.

**May 1 → May 2 transition — Pre-Let-It-Run Observability + Lab Arc (Sessions 50, 51).** Two final sessions before letting the bot run autonomously for 14 days, closing two specific gaps: **Session 50** shipped forward-only `confidence` + `dqs` + `sport` persistence on live_momentum `paper_trades.json` records. The 3 dimensions had been missing — all 74 settled live_momentum trades had `confidence=0` and no DQS or sport fields. Without these, the May 15 Session 49 re-validation could only know "NBA was bad" but not "NBA was bad ONLY at confidence > 0.85" — a structurally different decision tree. Composite confidence formula `min(1.0, dqs * (1 + max(0, wp_edge)))`. Vig_stack path preserved byte-identical (verified post-restart on 3 vig_stack records). Bot restarted; **Battle Scar #3 caught its 3rd orphan today** (morning false-alarm, Session 49 restart kill, Session 50 restart kill). PID 82747 fresh. **Session 51** shipped `tools/strategy_lab/` v1 — rapid hypothesis prototyping for new strategies. Drop a 20-line candidate file in `tools/strategy_lab/candidates/`, run `python3 -m tools.strategy_lab.driver --candidate <name> --days 14`, get a markdown report with hypothetical P&L + per-sport breakdown + reason histogram + top winners/losers in seconds. Days/weeks → seconds for the "is this idea worth pursuing?" decision. **Schema discipline now self-enforcing in TWO places** — CLAUDE.md "Canonical Data Schema Reference" section as documentation + `test_canonical_schema_used_throughout` as commit-time guard against the `'no_won'` vs `'no'` anti-pattern that almost falsified Session 45. **End-to-end "find new attack angles" loop closed:** discovery agent surfaces candidates → strategy lab validates them in seconds → real production scanner ships if validated → that scanner becomes another input to the next day's agent run. Compounding strategy expansion. **The 14-day clock starts now (hour 0).** Next decision points: May 13 (Session 36 day-14 + Session 38a ATP re-validation), May 15 (Session 49 NBA/UFC sizing re-validation, now with confidence/dqs/sport buckets), May 18 (Session 22 MOMENTUM_LEADER_MIN re-validation).

### ☑ Session 1 — Settlement + pattern pipeline (Apr 20)
58 of 93 resolved paper trades (`exited_early`) were silently missing from `strategy_audit.settlement_log` because `executor._paper_record_exit` never called `_log_settlements_to_audit` or `patterns.record_resolution`. Fixed by extracting `tracker.log_settlement(trade)` per-trade helper, adding `patterns.record_resolution`, wiring both into `_paper_record_exit`, and rebuilding the audit via `tools/rebuild_strategy_audit.py`. Post-rebuild: paper / settlement_log / rollup all reconcile to 93 trades. Backup at `bot/state/strategy_audit.json.bak-20260421`.

### ☑ Session 2 — Active strategy retuning (Apr 20)
Two dollar leaks:
- **Vig_stack volatile branch**: KXHIGHDEN/NY/CHI = −$126.88 on 36 trades. Bucket analysis showed only [92-96¢) was breakeven on volatile families. → `VIG_STACK_WEATHER_MIN_PRICE` raised 0.90 → **0.93** (1¢ safety margin above the breakeven floor).
- **Live_momentum tennis**: 72% of momentum volume for −$6.20 net. → New `MOMENTUM_DISABLED_SPORTS = {atp, atp_challenger, wta, wta_challenger}`; `can_enter` gate in `live_watcher._tick_momentum` blocks new entries while preserving normal exits on held positions.
- Briefly raised `MOMENTUM_LEADER_MIN` 0.70 → 0.75 to skip the [75-80¢) dead zone, then reverted same day — MIN is a floor so 0.75 *admitted* the dead zone while surrendering the positive [70-75¢) bucket. Proper dead-zone exclusion in `is_leader` is TODO.

### ☑ Session 3 — Live-watcher ESPN restoration (Apr 23)
3000/3000 recent live ticks had `espn_scores: None`; `wp` defaulted to 0.5. Three silent failures stacked: missing `User-Agent` header (ESPN started 403'ing), default SSL context (intermittent cert validation), and a bare `except:` swallowing the exception. Fixed in `bot/live_watcher.py:_fetch_espn_score`: `User-Agent: GlintBot/1.0`, `_ESPN_SSL_CTX = ssl.create_default_context(cafile=certifi.where())`, structured error logging, one-shot success log per (ticker, sport). `ESPN_BASE` + `ESPN_SPORT_PATHS` hoisted into `bot/config.py`. Verification (last 500 ticks Apr 23): NHL 68/68 ✓, NBA live games OK, all sports `wp` 100% populated.

### ☑ Session 4 — Scheduler + bot_state revival (Apr 23)
`last_morning_briefing` was 11 days stale, `last_nightly_summary` 4 days stale, `total_pnl` was always 0. Root causes: scheduler hour-gate was `current_hour == HOUR` (narrow window the polling loop kept skipping); `_send_nightly_summary` computed `total_pnl` but never persisted it; latent write-ordering bug clobbered concurrent state writes; `crypto_trades_today` was a stale counter not zeroed on date rollover. Fixed in `bot/scheduler.py` (hour gate `>=` + same-day flag + missed-day catch-up clause; persist `total_pnl` to `bot_state.json`; reload state before stamping to fix write ordering) and `bot/main.py` (zero `crypto_trades_today` on rollover; new startup drift warning if scheduler timestamps are >2 days stale). 14 new tests in `test_scheduler.py`.

### ☑ Session 5 — State hygiene (Apr 23)
117MB of `bot/state/`, 108MB of which was `live_ticks.jsonl` growing unbounded; 53% of `clv.json` records were for disabled strategies; six confirmed-stale files; `bot.lock` mtime frozen since startup. Fixed:
- `bot/clv.py:_load()` now filters records to active strategies (`ACTIVE_STRATEGIES + live_momentum`). Single read site, so disabled-strategy noise gets dropped on the next save.
- `bot/scheduler.py` — new `_rotate_live_ticks(today_str)` + midnight-ET gate. Renames `live_ticks.jsonl` → `state/archive/live_ticks-YYYY-MM-DD.jsonl`, gzips, unlinks. Race-safe because `_log_tick` reopens the file every write. 5 new rotation tests.
- `bot/main.py` — one-line `LOCK_FILE.touch()` in the heartbeat block. `bot.lock` mtime is now a liveness signal (purely additive — no reader consumed it before).
- One-shot `tools/purge_clv_disabled.py` + `tools/clean_stale_state.py` to drain on-disk noise. Deleted: `odds_snapshots.json`, `price_cache.json`, `watchlist.json`, `paper_trades_archive.json`, two Apr-18 `.bak` leftovers. Kept: `strategy_audit.json.bak-20260421` (Session 1 backup).
- CLAUDE.md state-files table now distinguishes `trade_history.json` (order log) from `paper_trades.json` (paper resolution log).

**Result**: `bot/state/` from 117MB → 5.4MB after one-shot + rotation. Zero trading-logic changes; all five sessions are safety/observability/cleanup.

### ☑ Session 6 — Closed-loop data collection foundation (Apr 24)
The trade log answered "what fired"; nothing answered "what almost fired and what would have happened." Three new pieces:
- `bot/decisions.py` (new, ~120 lines) — `log_decision(ticker, opp_type, edge, gates, decision, reason, extra)` atomically appends to `bot/state/decisions.jsonl`. Single write site, threading lock, never raises.
- `bot/clv.py:record_counterfactual_skip` — top rejected opportunities per scan get a CLV record (`status=counterfactual_open`, `trade_id=CF-{scan_id}-{ticker}`). The existing settlement poller fills `closing_yes_price` on them naturally.
- Instrumented every scanner reject (`scanner.py` vig_stack gates) and the executor's 7 position-limit + 3 verify-edge gates with `log_decision` calls. Live-momentum gates use a dampener (only emit on `(decision, reason)` change) so a flat-market ticker doesn't spam 50k records/day.
- Daily rotation of `decisions.jsonl` to `archive/decisions-YYYY-MM-DD.jsonl.gz` mirrors the Session-5 live_ticks pattern.
- New `tools/cohort_report.py` (local-only) joins decisions to CLV CF records to compute "edge left on table" per gate.
- Follow-up the same day: filter CF entry-price < 3¢ (relative-edge math `(fair-price)/price` blows up at 1-2¢ entries, crowding out legitimate higher-quality rejects in top-K selection).

### ☑ Session 7 — Decision-log observability gaps (Apr 24)
First 24h of `decisions.jsonl` surfaced two gaps:
- **Live-momentum decisions logged `edge=null`** because `_tick_momentum` has no scalar edge concept. Wired `wp_edge` (already computed each tick for `live_ticks.jsonl`) into `_log_decision_dampened` at all 5 reject sites. Added `mom_ctx={wp, kalshi_price, dip_cents, dqs}` to `extra` so the cohort report can join on something useful.
- **`bot.lock` mtime advanced only at scan boundaries** (2-30 min), making healthy idle bots look wedged per Gotcha #6's 15-min stale-mtime rule. Added a dedicated `_heartbeat_loop` task on `GlintBot` that touches `LOCK_FILE` every 30s. Per-scan touch in `_main_loop` retained as belt-and-suspenders. Worst-case stale gap drops 30 min → ≤60s.

### ☑ Session 8 — Stratified CF sampling (Apr 24)
First 24h of CF data showed 29/29 records attributed to `non_stable_below_weather_floor` (real 4-20¢ edges) while the gates we most need to retune had **zero** outcome attribution: `vig_stack_series forecast_in_bucket` (143 rejects, 0 CFs), `vig_stack_futures edge_below_threshold` (130 rejects, 0 CFs), `vig_stack_series edge_below_threshold` (114 rejects, 0 CFs). The Session-6 "top-5 by global edge" rule starved low-edge-by-design gates. Replaced with two-stage stratified sampling in `bot/scanner.py:_stratified_cf_rejects`:
1. **Stratified core** — 1 highest-edge reject per `(opp_type, skip_reason)` group
2. **Budget fill** — highest-edge leftovers up to total_budget=10
3. **Dedup by ticker** (higher edge wins); hard cap 15/scan

Bonus fix: `forecast_in_bucket` rejects were logged to `decisions.jsonl` but never appended to `rejected_opps` because fair-value computation ran *after* the short-circuit. Hoisted the fair-value block above the forecast check so forecast-rejected contracts now enter the CF sample with a real edge.

Budget math: ≤480/day idle, ≤7200/day active. Well under the ≤900/day idle, ≤13k/day active envelope. **Part 2:** `run_bot.sh` hardcoded a Python 3.9 binary path but `bot/daily_log.py` uses PEP 604 union syntax requiring Python 3.10+; bumped to Python 3.14 framework path and re-enabled the user-domain launchd service.

### ☑ Session 9 — Per-position MFE/MAE tracking (Apr 24)
`clv.json` recorded entry, settlement, and final CLV but nothing about what the price did *between* entry and settlement. Two trades can have identical CLV but very different lived experiences — one drifted straight to close, the other spiked +30¢ then unwound. The first vindicates conviction sizing; the second is a missed-exit signal.
- `bot/tracker.py:update_positions` ratchets `mfe_cents`/`mae_cents`/`mfe_at`/`mae_at`/`ticks_observed` on every price observation. Side-aware via `current_bid` (yes_bid for YES, no_bid for NO). Lazy-init on first observation so pre-Session-9 open positions upgrade cleanly.
- `bot/clv.py:check_clv_settlements` builds `order_id → position` lookup and copies the five excursion fields into real-trade settlement records. Counterfactuals untouched.
- New `tools/excursion_report.py` (local) groups settled CLV by `opp_type` and flags `median(MFE − exit) > 5¢` as exit-logic candidates.
- 10 new tests covering init, side-aware ratchet, monotonicity, timestamp semantics, settlement propagation.

### ☑ Session 10 — Gate-context enrichment in `extra` (Apr 24)
`decisions.jsonl` recorded *which* gate fired but not *by how much*. A gate that rejects "just barely" 80% of the time is a tuning candidate; a gate that rejects "by a mile" 80% of the time is doing its job. Backfilled `extra` across every reject site with gate-relevant diagnostics:
- `forecast_in_bucket` → `forecast_temp`, `bucket_lo`, `bucket_hi`, `distance` (rounded cents)
- `edge_below_threshold` → `edge` (actual), `vig`, `time_to_settle_hr`, `min_edge` threshold
- `low_liquidity` → `volume`, `open_interest`, `min_volume`, `min_open_interest`
- Executor: refactored `_log_position_reject`/`_log_edge_reject` helpers to accept an `extra` kwarg, then enriched all 7 position-limit gates (position_cap, duplicate, same_game, cooldown, daily_loss, strategy_budget, total_exposure) plus edge-verify and self-check gates with their respective context.
- Updated `tools/cohort_report.py` to render distance-from-threshold histograms (replacing the binary reject-rate).
- Proper TDD: 8 commits, failing tests first, then implementation.

### ☑ Session 11 — Fair-value calibration loop (Apr 25)
Every edge calc is `(fair_value - market_price) / market_price` — the whole bot is one big bet on `fair_value` being right, and CLV alone can't catch a scanner that consistently overestimates fair value (CLV measures execution, not prediction).
- New `bot/calibration.py` (171 lines) mirrors the `decisions.py` pattern: `record_prediction()` appends to `bot/state/predictions.jsonl` on every CLV entry (real trade or CF). Atomic JSONL append, threading lock, never raises. Idempotent on `(scan_id, ticker)`. Skips rows where `predicted_fair_cents` is None/0 (live_momentum has no usable fair value — Session 7 cross-reference).
- `bot/clv.py:check_clv_settlements` calls `update_prediction_close()` to fill `closing_yes_price` on matching prediction rows via ticker + recorded_at ±60s window (handles the small lag between `record_clv_entry` and `record_prediction`).
- Daily rotation of `predictions.jsonl` to `archive/predictions-YYYY-MM-DD.jsonl.gz` mirrors the existing patterns.
- New `tools/calibration_report.py` (local) emits per-strategy mean-bias / Brier score / per-bucket hit-rate. After 7 days of settlements, `vig_stack_series` predicted bucket [80,90¢) resolving <70% YES is a flag for fair-value retuning.
- 31 new tests including atomic append under contention, idempotency, settlement matching window, missing-archive rotation, and Brier handcraft (5 records → 0.082 by hand).

**Result**: the bot is now self-instrumented end-to-end. Every accept and every reject carries a gate fingerprint with distance-from-threshold context. Every prediction (acted-on or counterfactual) is paired with its eventual closing price. Every position carries excursion data. Three local-only analysis tools (`cohort_report`, `excursion_report`, `calibration_report`) join the streams. Once 7 days of data accumulate, gate retuning becomes a regression problem instead of folklore.

### ☑ Session 12 — Universe log (Apr 25)
Existing collection points (`decisions.jsonl`, CFs, `predictions.jsonl`) only fired on opportunities a strategy scanner already considered. Kalshi has 50K+ active markets at any time (95% MVE parlay expansions); we scanned a curated handful. Without a record of the full universe, we couldn't ask "what alpha is hiding in markets we don't even look at?"
- `bot/universe.py` — buffer-and-flush snapshot writer that captures the active Kalshi universe alongside each `scan_cycle`. `scanned_by` attribution on every row links each market to whichever active scanner(s) evaluated it. Empty `scanned_by` = no active strategy looked at this market — that's the join key Session 13's back-tester needs.
- **Two-pass design** discovered empirically: cursor pagination captures the long-tail (KXMVE* parlay expansions filtered out — 95% of raw response volume), then a per-active-series shadow fetch guarantees buffer coverage of every ticker active scanners will attribute against. Without the shadow pass, cursor order made attribution silently fail under the 90s deadline.
- `bot/main.py:_main_loop` hoists `scan_id` once per loop iteration and wraps `scan_cycle` in `try/finally` so flush runs even on scanner exception.
- Daily rotation in `bot/scheduler.py` mirrors predictions: archive to `state/archive/universe-YYYY-MM-DD.jsonl.gz`. Universe is the largest of any log.
- `tools/universe_report.py` (gitignored) reads current + 7-day archives, surfaces ignored families with high volume + spread as Session-13 candidate territory.
- **Verified live:** first post-deploy snapshot captured 976 markets (47% scanned, 53% ignored) — immediately surfaced `KXNBAGAME` with $262K avg volume completely ignored by every active scanner. Concrete actionable signal before Session 13 even shipped.
- 8 tests in `test_universe.py` + 5 rotation tests. Bonus fix: discovered + fixed a pre-existing test isolation bug where every rotation test class shared a midnight-boundary fixture, causing cross-test pollution on real state files; fixed with autouse fixture.

### ☑ Session 13 — Hypothetical strategy framework (Apr 25, 3 sub-sessions)

The biggest session of the arc. The session where frame-escape actually became possible.

**13a — Strategy contract.** New `bot/strategies/__init__.py` defines a `Strategy` Protocol with `candidate_markets()` and `evaluate()` methods, plus a frozen `Market` dataclass. Strategies take Market data in (no Kalshi API calls), return Opportunity dicts out — back-testable trivially. Refactored `vig_stack_series` (the smallest, most mechanical scanner) into `bot/strategies/vig_stack_series.py`. **Behavior preservation enforced via 12-case golden-file test** (`test_vig_stack_series_strategy.py`): same accepted ticker set, 1e-6 epsilon on every float field, identical decision-log call set across 5 hand-crafted scenarios. Lock the regression THEN delete the legacy. Also added: `name_for(market)` for one-class-spans-multiple-opp_types attribution, `finalize(scan_id)` for end-of-loop side effects.

**13b — Offline back-tester.** New `tools/backtest.py` (gitignored) replays refactored Strategy classes against `universe.jsonl` archives (current + gzipped 7-day window), joins emitted opportunities to settled CLV records via `(ticker, recorded_at±60s)`, reports per-day P&L / win rate / mean edge / mean CLV. **Critical discipline:** REUSES `bot.clv.compute_clv_cents` extracted from inline math at `bot/clv.py:284-299` as the prereq commit — single source of truth, no parallel codepath. The `--verify-against-clv-report` flag prints back-test mean alongside live `clv_report` mean and asserts `|diff| < 1e-6` on the actually-taken subset. Asymmetric vacuous handling: bt-empty is OK (universe coverage gap), bt-non-empty + live-empty IS FAIL.

**13c — Hypothetical strategy report + Kalshi history fallback + strawman.** Four parts:
1. Threaded tunable parameters through `VigStackSeries.__init__` (defaults to existing constants) so back-tester can sweep variants without touching live code. Re-ran 13a golden test to verify behavior preservation.
2. New `bot/kalshi_history.py` — `fetch_settled_close(ticker)` with permanent caching to `state/cache/kalshi_settled_closes.json` for back-testing tickers we never traded. **Caught a real production bug in flight:** Kalshi reports resolved markets with `status="finalized"`, NOT `"settled"` — the authoritative settle signal is the `result` field. Added regression test `test_finalized_status_resolves_via_result`.
3. Refactored `tools/backtest.py` to expose a programmatic `run_backtest()` entry point. New `tools/hypothetical_report.py` runs N variants of a strategy against captured universe and prints markdown comparison sorted by sum_clv. **Sweep verification (vig_stack_series, min_relative_edge across [0.05–0.25], 7 days):** opps strictly monotonic descending (47 → 28 → 18 → 3 → 0), mean_edge strictly increasing — proves the param refactor wires through correctly.
4. New `bot/strategies/nba_game_momentum_strawman.py` — 60-line strawman targeting the KXNBAGAME family Session 12 surfaced. **The contract held cleanly.** Zero changes to `bot/strategies/__init__.py` were needed for the strawman — proves the Protocol is general, not secretly molded around vig_stack's specifics.

56 tests across 6 files. The bot can now genuinely evaluate alternatives without going live.

### ☑ Session 14 — Regime tags (Apr 25)
A strategy net-negative on average might be +EV in a specific regime — NBA playoffs, weekday mornings, close-to-settlement markets. Without regime context on records, that signal is invisible.
- `bot/regime.py` — pure function `tag(ts, ticker, market_state) -> dict` returns a fixed-key dict with 4 axes: `time_of_day` (morning/afternoon/evening/overnight in ET), `day_of_week`, `sport_phase` (preseason/regular/playoffs/off — NBA/NHL/MLB/NCAAB only in v1), `event_horizon_hr` (hours-to-settle bucket).
- `sport_phase` from a hardcoded date table — ESPN cache reuse not viable (live_watcher caches per-game live state, not season schedule). Yearly bump documented in module docstring.
- 5 writers tag at write time: `decisions.py`, `calibration.py`, `clv.py` (real + CF), `tracker.py` (positions), `universe.py`.
- `tools/backfill_regime.py` (gitignored) ran clean on production state: **18,515 records → 100% coverage on every state file**.
- 4 reports gain `--regime-by` axis flag: cohort, excursion, calibration, universe.
- `market_vol_tier` deferred — needs per-ticker price history infra; worth its own session.
- 165 Session-14 tests including 42 in `test_regime.py` covering DST boundaries, all 7 days, sport phase transitions, and a 100x-iteration determinism property.

### ☑ Session 15 — Live order microstructure (Apr 25, plumbing-only)
YAGNI per spec: only matters when `PAPER_MODE=False`. The plumbing ships now; first real verification waits for live trading.
- `bot/order_microstructure.py` — atomic JSONL append mirroring `decisions.py`, with 4 lifecycle functions: `record_placement`, `observe_fill_progress`, `record_terminal`, `record_synchronous_rejection`. In-memory `_PENDING` dict bridges place→terminal across the place_order call and the check_fills polling loop.
- Sign convention documented: `slippage_cents = filled - requested`, positive = adverse for both YES and NO buys. `slippage_source` enum (`limit_price_echo` / `fills_endpoint` / `none`) preserves audit trail for v2's fills-endpoint upgrade.
- Hooked in `bot/executor.py` LIVE branch only at lines 909-917 (place) and `check_fills` (terminal). PAPER_MODE branch byte-identical and explicitly tested to produce zero microstructure rows.
- `queue_depth_at_place` pulled from existing `opportunity['market'][f'{side}_ask']` (top-of-book) — no extra API call.
- Daily rotation via the same `_rotate_jsonl` helper that emerged across earlier sessions.
- `tools/microstructure_report.py` (gitignored) — per-strategy slippage / latency distributions + slippage-adjusted CLV vs paper CLV. Flags strategies where divergence > 2¢ as "paper-mode over-optimistic" candidates. **REUSES** `bot.clv.compute_clv_cents` (single source of truth from 13b) and `bot.calibration._within_window` (±60s join from 13b).
- Known v1 gaps documented in module docstring: `_PENDING` lost on restart (process-local), Kalshi cancellation pruning, `limit_price_echo` slippage approximation pending v2 fills-endpoint integration.
- Verification deferred: first live order populates a row; after 50 live orders run `microstructure_report` and check median slippage > 2¢ or fill latency > 5s p95 (Session 16+ execution-tuning candidates).

### ☑ Session 15.5 — Data integrity hardening (Apr 25)
Pre-week-long-run polish. Sessions 1–15 shipped comprehensive instrumentation; Session 15.5 closed the silent-corruption and silent-feature-loss gaps before the bot runs unattended for 7+ days.
- **Heartbeat dual-update.** Session 7's `_heartbeat_loop` touched `bot/state/bot.lock` every 30s but did NOT update `bot_state.json:last_heartbeat` — that field was only refreshed per-scan, so anything reading it for liveness (Telegram `/STATUS`, future watchdogs) saw a "wedged" bot that wasn't. `_heartbeat_loop` now writes both. Verified live: `last_heartbeat` age dropped from 18+ min to **<60s** in production.
- **Test logger isolation.** Running pytest was writing test scenarios (mocked `OSError("disk full")`, simulated `ImportError`) into `bot/logs/bot.log` via the shared `glint.*` logger config — making `grep ERROR bot.log` an unreliable health check (190 fake errors in 24h). New `tests/conftest.py` autouse fixture swaps every `glint.*` handler to `NullHandler` for the test session, restored on teardown. Tests that explicitly want log assertions use pytest's `caplog`.
- **Universe partial-rate metering.** `bot/universe.py:snapshot_universe` was setting `partial: true` on rows when cursor pagination hit the 90s deadline or fetch errors, but tracked nothing globally. Could have been silently working with partial data 30%+ of the time and never known. Now `bot_state.json` carries `total_snapshots_today` + `partial_snapshots_today` counters (reset at midnight ET), with a WARN log at 10% partial rate. **Live result:** 18% partial rate in production — Kalshi rate-limiting under load is real, now visible.
- **`event_horizon_hr` regime coverage.** Session 14's regime tagger had this axis at 0% coverage in production because no log_decision call site was passing `close_ts` through the `extra` dict. Threaded `close_ts` through 4 writers (`bot/executor.py`'s 7 position-cap + 4 verify-edge sites, `bot/strategies/vig_stack_series.py`, `bot/live_watcher.py:_log_decision_dampened`). **Live coverage on post-fix records: 83.5%** — beats the original 60% target. Remaining ~17% nulls are structural (gates that fire before market data unpacks).
- **`positions.json` regime backfill gap.** 152/154 positions had regime, 2 didn't (today's UFC fights opened after first backfill ran). Re-ran `tools/backfill_regime.py` → 154/154.
- **"7-Day Retuning Report" runbook in CLAUDE.md.** New section walks Claude Code through the full report-running playbook: which 4 reports to run, which `--regime-by` slices to add, what numbers actually mean, cross-report intersection logic, and known caveats. Per-report confidence is honestly differentiated (cohort/calibration/universe = high confidence on Day 7; excursion = low until Session 17 ships; live_momentum coverage is structurally absent from calibration regardless of when you run it).
- 6 commits, ~24 new tests, ~250 LOC. The fixes are small individually; collectively they make every health check reliable for the upcoming week.

**Arc result:** Sessions 12–15.5 transformed the bot from "well-instrumented inside its own frame" into "able to evaluate alternatives outside it, with airtight observability." The Strategy contract + back-tester + Kalshi history fallback let any new strategy idea be tested against historical data in 50 lines without a live deploy. Universe log surfaces what we're not looking at. Regime tags slice everything by time/sport/horizon. Microstructure plumbing is ready for live. Session 15.5 made every data check reliable.

### ☑ Session 16 — Excursion gap-math fix (Apr 26)
First production run of `tools/excursion_report.py` on Apr 25 showed median gap = -1¢ for both strategies. Mathematically impossible if MFE is "max favorable excursion from entry." Investigation found it wasn't a units/sign bug — it was a *semantic MFE-completeness bug*: production `tracker.update_positions` capped MFE at observed bids (≤99¢ for YES winners due to bid spread), but settlement uses the payout (100¢). Fixed in `bot/clv.py:check_clv_settlements` with one line: `rec["mfe_cents"] = max(observed_mfe, max(0, clv_cents))` at settle-time propagation. Settlement IS part of the position's lifetime; MFE should include it. Tracker stays pure tick-observation; settlement layer reconciles to final outcome. Backfilled 6 winning records via `tools/backfill_extended_mfe.py`. Post-fix: median gap = 0¢ for both strategies, every per-record gap ≥ 0. 29 new tests in `test_excursion_report.py` including 100-tuple property test.

### ☑ Session 17 — Tracker cadence audit (Apr 26)
Apr 25 excursion data showed median ticks = 1 for live_momentum positions — half the settled positions had ONE tracker observation between entry and settlement. **Diagnosis (Outcome B — cadence-limited):** `tracker.update_positions` was called only from `_main_loop` Step 6, paced by `scan_interval` (1800s IDLE / 600s pregame / 120s live). But `scan_interval` comes from the *odds-API games list*, which doesn't include Kalshi-native sports (UFC, IPL, individual matches) that live_watcher actually bets on. Result: idle most of the time live_momentum was open. Pre-fix: 54/60 (90%) live_momentum positions had `ticks_observed = None`. Fix: added `_position_check_loop` as third concurrent loop alongside `_main_loop` + `_heartbeat_loop`, fires every 30s independent of scan_interval. New `bot/tracker_cadence.py` logs every `update_positions` call (called_from, ms_since_last_call) for ongoing visibility. Verified post-deploy: median `ms_since_last_call` for `_position_check_loop` = 31.3s (target 30s).

### ☑ Session 18 — `live_journal.json` analysis tool (Apr 26)
`bot/state/live_journal.json` (~600 KB, 1,710 records of `scan_found`/`bet`/`exit`/`session_end` events) was sitting unread by any tool. New `tools/journal_analysis.py` (gitignored) with 5 aggregations: time-to-exit distribution, exit-reason classifier (11-key enum), watch-but-no-enter funnel, per-sport split, session_end P&L distribution. Surfaced **3 actionable findings** baked into the commit message:
1. **TRAILING STOP and DOLLAR STOP exits fire 0% across all sports** (n=95 paired bet→exit lifecycles) — one of these would later turn out to be a real production bug found in Session 19a.
2. **UFC live_momentum is mechanically a different strategy** — median hold 123s (p25 = 47s) vs 642–1791s elsewhere; only positive session win/loss ratio (5W/2L of 17). Don't flatten UFC into global config.
3. **Watch-but-no-enter rate is 56–91% across all sports** but `scan_found` events don't record `skip_reason` — instrument live_watcher to capture WHY in a future small follow-up.
38 tests in `test_journal_analysis.py`.

### ☑ Session 18.5 — Exit-logic replay tool + dead-config removal (Apr 26)
Cheap follow-up to Session 18 Finding #1 — sweep `MOMENTUM_DQS_TRAIL_STOP` across [3,4,5,6,7,8]¢ on historical bet→exit pairs. **Phase-1 dead-config discovery flipped the premise:** the param I named in Session 18's commit (`LIVE_PROFIT_TARGET=0.50`) was actually dead config — defined in `bot/config.py:61`, imported in `bot/live_watcher.py:36`, **never read in any logic anywhere**. Same for `LIVE_TRAILING_STOP` and `LIVE_HARD_PROFIT_TARGET`. Removed all 3, verified 0 readers via grep. Sweep axis pivoted to `MOMENTUM_DQS_TRAIL_STOP` (the parameter that actually gates trailing). **Decision: Outcome B (no config change shipped).** Strict-cohort sweep was non-monotonic (Σ¢: -11/-3/-11/-3/-14/+13), best variant delta well below the +50¢ "clear winner" threshold, widening looked better but was methodologically biased (the [bet.ts, exit.ts] tick window can't honestly evaluate strategies that DELAY exit). Two real takeaways for Session 19: (a) tick window must extend beyond production exit_ts; (b) train/test split is empirically required. New `tools/exit_replay.py` (gitignored, ~440 LOC). 24 tests.

### ☑ Session 19 — Tick-replay back-tester for live_momentum (Apr 26-27, 5 sub-sessions)

The biggest, most consequential session of the entire arc. Five sub-sessions following the 13a/b/c pattern + two unscheduled discoveries.

**19a — TickStrategy Protocol + behavior-preserving `live_momentum` port.** New `bot/strategies/__init__.py:TickStrategy` Protocol (distinct from snapshot-based `Strategy` from 13a) with `init_state(market)` + `process_tick(state, tick) → (new_state, action)`. New `bot/strategies/live_momentum.py` byte-identical port of `_tick_momentum`. **Pragmatic Task 3 chosen over full async I/O mocking harness:** golden-file fixtures captured from new code (5 hand-crafted scenarios), with explicit "differential proof deferred to 19b's reality check" caveat documented in commit. **Pre-flight grep (Prereq B from 18.5) caught a residual dead reference** to `LIVE_TRAILING_STOP` at `bot/live_watcher.py:2600` inside an unreachable branch — flagged out of scope, not removed. 47 tests pass.

**19a-followup — Parity restored, port vindicated.** Session 19b's parity check came back **0/9 within 1¢ tolerance** — alarming. Two paths: (a) audit the port (manual review missed something), (b) audit the back-tester. We chose (b). **Every divergence pattern was a back-tester / sample / comparator issue, not a port bug.** `bot/strategies/live_momentum.py` was byte-identical to its 19a state and never modified. Five back-tester fixes shipped: `--min-entry-date` filter, `--debug-ticker` diagnostic, parity-window cap, `qty_override` for parity (sizing depends on real-time balance not in archives), `COVERAGE_GAP` classification, slippage=0 default for parity. **Result: 8/8 PASS at sample 20.** The "load-bearing peak-tracking bug" claim from 19b was also refuted: peak-fix delta sign-flipped from -240¢ (artifact of the broken comparator) to +558¢ on n=20 (real signal). 18 new tests.

**19a-peakfix — Production peak-tracking bug fix.** One-line bug fix at `bot/live_watcher.py:2225`: `setdefault(ticker, entry_price)` replaces `get(ticker, current_value)`. The original code had a chicken-and-egg bug — `prev_peak = self._peak_values.get(ticker, current_value)` defaulted to `current_value`, then strict `if current_value > prev_peak` was always False, so `peak_values[ticker]` was NEVER written on first observation. **Result: TRAILING_STOP physically couldn't fire in production for the entire history of the bot.** This is the actual root cause of Session 18 Finding #1 — not LIVE_PROFIT_TARGET (dead config), not threshold tuning. Conservative impact: ~+14¢/trade. Port also updated at `bot/strategies/live_momentum.py:267` to keep parity at 8/8. 1 regression test added; the trailing_stop fixture regenerated and now contains a real Sell action.

**19b — Tick-replay back-tester.** New `tools/tick_backtest.py` (gitignored). Replays per-game tick streams through TickStrategy, joins to settled clv records, supports parameter sweep + train/test split + slippage pessimism. Reuses `bot.clv.compute_clv_cents` (single source of CLV math from 13b). After 19a-followup repaired the 5 back-tester bugs, parity check passes 8/8 within 1¢ tolerance — the differential validation layer Option 1 promised.

**19c — Parameter sweep with train/test discipline (Outcome A — config shipped).** 15-variant sweep (LM ∈ [0.65, 0.70, 0.75] × TRAIL_STOP ∈ [4, 5, 6, 7, 8]) on n=22 post-Apr-23 paper trades, 70/30 train/test split, 2¢ slippage. Result: LM=0.65 test Σ P&L delta = **+488¢** vs baseline (LM=0.70). Sign agreement train/test held. **Honest CAVEAT documented in `bot/config.py:67-90` AND CLAUDE.md AND the commit message:** the +488¢ is dominated by ONE trade (KXNBAGAME-26APR26-CLE: −424¢ → +94¢, +518¢ swing). Without CLE, the remaining 5 test trades net −30¢. The +50¢ Outcome A threshold is technically met. Mitigations: (1) consistent with prior Apr 20 evidence that lower LM admits +EV [70-75¢) bucket, (2) honest docs throughout, (3) **scheduled May 18 routine auto-fires to re-validate on the larger sample** — CONFIRM/REVERT/INCONCLUSIVE per `~/.claude/scheduled-tasks/session-22-momentum-leader-min-revalidation/SKILL.md`. 7 new sweep tests.

**Arc result:** the bot now has a working tick-replay back-tester with reality-validated parity (8/8 within 1¢), proven discipline for handling failed parity (audit the comparator, not the code), an actual production bug fix that took five sessions of analysis to converge on (Sessions 18 → 18.5 → 19a all hypothesized different causes; 19a found the real one), and a config change that's a fragile-but-honestly-shipped bet with an automated re-validation already on the calendar. The pivot-enabling instrumentation arc and the strategy iteration arc together represent the bot's transition from "we're guessing about parameters" to "we have the eyes and the discipline to evidence-test changes before shipping."

### ☑ Session 21 — live_watcher skip_reason instrumentation (Apr 27)
Closed Session 18 Finding #3: the 56-91% watch-but-no-enter rate across all sports was a black hole because `scan_found` events didn't record WHY each match was skipped. New `_journal_record_scan` helper wired at all 11 match-level gate sites (`bad_event_shape`, `low_volume`, `not_today`, `no_leader`, `settled`, `unknown_name`, `already_watching`, `recently_watched`, `no_vol_growth_first_seen`, `no_vol_growth_idle`, `capacity_capped`) plus the spawn-accept site with `skip_reason=None`. `LIVE_SCAN_TELEMETRY` log line preserved byte-identical. Forward-only (historical events can't be backfilled). 13 new tests asserting each gate's `skip_reason` matches expected. **Bonus discovery (Session 21-followup):** investigating IPL 33% / UFC 37% `not_today` rate found these are STRUCTURAL — IPL games run daily (~75% of markets are forward-dated), UFC fights only weekends (~100% of markets are next Saturday between cards). Outcome B (no code change), documented as "structural, not a bug."

### ☑ Session 23 — live_momentum counterfactuals (Apr 27)
Mirrors Session 8's stratified-sampling pattern, adapted for tick-replay strategies. `LIVE_MOMENTUM_TUNABLE_SKIP_REASONS = {no_leader, low_volume, no_vol_growth_idle, no_vol_growth_first_seen}` — `disabled_sport` correctly excluded during pre-flight (gate fires series-level before market fetch; per-match data unavailable by design). `_should_emit_live_momentum_cf()` enforces bucket-fill cap of 5 per `(sport, skip_reason)` per UTC-midnight day; `record_live_momentum_counterfactual_skip()` mirrors the vig_stack pattern with `opp_type="live_momentum"`, `paper=True`, `trade_id=f"CF-LM-{ts}-{ticker}"`. Settlement is opp_type-agnostic (verified by reading clv.py:269). 8 new tests including cross-sport + cross-skip_reason isolation, UTC-midnight cap reset, settlement flow. **Live result: ~10×/day rate** (319 CFs in ~48h), distribution healthy across all 4 tunable skip_reasons.

### ☑ Session 24 — Weekly digest tool (Apr 28)
Single aggregator over 8 sections (P&L, cohort, calibration, excursion, journal, universe, CF coverage, bot health) — pure aggregator, no new analysis logic. Refactored two category-(c) tools (`excursion_report.py`, `universe_report.py`) to expose `generate_report()` for programmatic use. **`_safe_section()` failure-tolerance is the centerpiece** — wraps every section so a single thin-data crash renders `[section unavailable: <reason>]` and continues. Footer reports `Sections rendered: N/8` with skipped list. Runs in ~0.5s (well under 60s budget). Output: markdown to stdout + archives to `bot/state/weekly_digest_<date>.md`. 10 new tests including smoke (8/8 against real state), single + multi-section crash recovery, --regime-by passthrough, file-vs-stdout parity, idempotency. **Real-state digest already surfaced two findings** worth tracking: live_momentum CFs accumulating (Session 23 working), partial_snapshots_today 13/13 (100%) flagged (became Session 28).

### ☑ Session 28 — partial_snapshots tuning (Apr 28, Outcome C → D)
Apr 25 baseline was 18% partial; Apr 28 hit 100%. Investigation phase 1 confirmed both candidate causes contributing: 38 deadline hits + 22 connection resets + 5 read timeouts; live dry-run measured 106.5s with 1076 rows still flagged partial under the 90s deadline. **Outcome C (mixed):** shipped both `_SNAPSHOT_DEADLINE_SEC: 90→180` AND a bounded transient-retry loop on the error-dict path. **Session 28-followup verified the deadline bump worked but didn't restore the partial rate** — cursor reach exactly doubled with the deadline (2.0× pages for 2.0× time, linear scaling), then the followup pushed `180→300`. **Final result: Outcome D — at Kalshi API ceiling.** Cursor reach grew 4× total (87→632 cursor_rows, 221→802 pages) but partial flag stayed `True` because no reasonable deadline can enumerate Kalshi's open-markets cursor under current load. Honored own guardrail: do not bump again. **Session 28-2 (per-series-paginated universe rewrite) filed on watch list with explicit triggers — NOT shipped reactively.** The bias on `scanned_by` attribution is materially reduced even though the binary partial flag is still True; cohort_report findings on May 2 will be far less biased than pre-fix.

### ☑ Session 29 — Live journal write regression (Apr 28)
`bot/state/live_journal.json` mtime was 27.5+ hours stale despite the bot actively scanning. **Root cause: `bot/live_watcher.py:69-77` `_journal_append`'s strict `json.loads()` raised `JSONDecodeError` on a trailing-corrupted file (`}\n]\n]`) and a broad `except Exception` silently swallowed it, poisoning all future writes for 27.5 hours.** Session 21 (commit d5297e1) was the rate amplifier (~100/day → ~70k+/day on an unlocked RMW path); Session 23 was NOT the cause (my hypothesis was wrong, chat caught it). 6-line fix: `JSONDecodeError → raw_decode` recovery branch. Same forgiving parser the diagnostic script proved decodes the 6,581 leading events. Regression test plants the production `[...]\n]` shape; verified FAIL pre-fix → PASS post-fix. Live verification: mtime advanced from `Apr 28 20:36:34` to `Apr 28 20:44:42`; 88 fresh `scan_found` events with skip_reason distribution matching `LIVE_SCAN_TELEMETRY` drops; backup at `bot/state/live_journal.json.bak-session29`. **Session 29-followup investigation closed a verification gap** — Session 29 had assumed all 4 event types resumed because they share `_journal_append`, but didn't actually verify bet/exit/session_end. Followup confirmed all 4 ARE writing (production proof: PHIBOS `session_end` fired at `2026-04-29T01:45:42 UTC`, plus 2 exit events). Added 4 explicit per-event-type regression tests to close the discipline gap. NO code change needed in followup — investigation only.

### ☑ Session 30 — live_momentum research dataset + bucket analysis (Apr 28)
Stages 1+2 of the 3-stage research-layer proposal. Stage 3 (model.py) explicitly deferred — sample-size physics make ML on ~hundreds of decision rows produce 0.5-0.6 AUC (random); will revisit when sample > 1000 settled live_momentum rows. **Stage 1 (`tools/live_momentum_dataset.py`):** unified per-tick decision dataset joining `live_ticks.jsonl` + `live_journal.json` + `clv.json`. One row per candidate decision (accept AND tunable-reject from Session 21 + 23 — leverages both prior arcs). Forward returns at 30/60/120s + MFE/MAE in fixed window. **Decision-time vs outcome-time leakage prevention enforced via property test** — the #1 ML trap, ruled out at the dataset layer. MFE/MAE named `mfe_in_120s_window_cents` to avoid collision with Session 9's settlement-anchored `mfe_cents`. Output: `bot/state/research/live_momentum_dataset.csv` (gitignored). **Stage 2 (`tools/live_momentum_buckets.py`):** markdown bucket reports across 7 single dimensions + 4 interaction tables. Marks n<5 low-confidence. 27 tests across both files; leakage property test green; 89 rows in 7-day run. **Authored Findings (baseline fwd_return_120s = +0.13¢):** UFC −4.33¢ at n=5 (contradicts Session 18; needs reconciliation), challenger circuits over-perform (atp_challenger +1.53¢ n=6, wta_challenger +1.10¢ n=13 — DESPITE being in MOMENTUM_DISABLED_SPORTS, worth investigating), dip 6-8 beats tight dips, **leader_price 60-70 wins at 100% +CLV at n=12** (supports Session 19c's LM=0.65 change from a totally different angle). Adaptations from spec documented: actual tick fields are `price/bid/opp_*` (not `yes_*/no_*`); `compute_mfe_in_window` kept inline (not extracted into bot/clv.py) because tracker.py's MFE has different anchoring.

### ☑ Session 30-followup-2 — Dataset 84%-missing-CFs fix (Apr 29)
Session 30-followup investigation surfaced that the dataset was joining clv records to live_ticks by ticker+ts proximity, dropping any CF that didn't have a matching tick row. **All 340 LM CFs in `clv.json` are emitted from match-level pre-watcher gates** (`no_leader`, `low_volume`, `no_vol_growth_*`) which fire BEFORE a watcher spawns for that ticker — so `live_ticks.jsonl` has nothing for them, the join fails, the row gets dropped. Fix: post-loop sweep over unclaimed CFs with a `_cf_only_row` helper that constructs the null-feature row from CF identity + regime + outcome columns. Dataset rows: **89 → 413** (4.6×); challenger rows: 19 → 134 (7.0×); reject/CF ratio: 23.5% → 118.8%. Honest limitation surfaced: most new rows are CF-only with null decision-time features, so feature-bucket metrics (`dip`, `wp_edge`, `dqs`) didn't gain n on `fwd_return_120s` averages, but identity-bucket metrics (`sport`, `leader_price`) DID gain real n on the CLV outcome columns. Re-authored every Session 30 finding against the corrected dataset; **wta_challenger DIRECTION FLIPPED** on the CLV-outcome lens (+1.10¢ fwd → −5.54¢ settled CLV at the larger n). 5 new tests in TestCfWithoutTicks. NO bot changes (read-only tool fix).

### ☑ Session 33 — DQS persistence to live_ticks rows (Apr 29)
Session 30 bucket report's DQS dimension was empty because DQS wasn't stored on `live_ticks.jsonl` rows. DQS is computed in `bot/game_context.py` per-tick and used in-flight by live_watcher's entry decision but discarded after. **5 net code lines** in `bot/live_watcher.py:_tick_momentum`: `dqs_for_log` declaration + capture-after-`compute_dip_quality()` at primary + opponent sites + write to tick row payload. Behavior preservation: `buy_dqs`, `dqs_breakdown`, `_auto_bet_momentum(dqs_score=...)` and the entry/skip outcome are byte-identical to pre-Session-33 — `dqs_for_log` is a side-channel local that ONLY feeds `_log_tick`. Locked by the `test_dqs_does_not_change_entry_decision` regression test. 4 new tests; forward-only fix (pre-Session-33 ticks get `dqs=null`, gracefully handled by the dataset extractor). What this unlocks: future bucket reports (Investigation #1+) can answer "is DQS a useful feature for predicting forward returns / CLV?"

### ☑ Session 34 — match_phase regime axis for tennis / UFC / IPL (Apr 29)
Session 30 bucket report's `game_phase` dimension was mostly Unknown for tennis, UFC, and IPL because `period=None` on those sports' tick rows (no ESPN scoreboard integration). Session 30-followup-2 surfaced IPL CLV at **−23.13¢** on settled CFs — needed to know if IPL is uniformly bad or bad in some in-match phases and okay in others. **v1 taxonomy** in `bot/regime.py:tag()`: per-sport, state-aware path preferred when present, time-fallback otherwise. Tennis: `set_1`/`set_2`/`set_3+` from `set_number` OR `early`/`mid`/`late` from elapsed (<30m / 30-90m / >90m). UFC: `round_1`/`round_2`/`round_3+` from `round_num` OR by elapsed (<5m / 5-10m / >10m). IPL: `powerplay`/`middle`/`death` from `over_count` (overs 1-6 / 7-15 / 16-20). Investigation summary (CASE B confirmed): only `elapsed_seconds` is practically threadable in v1 since Kalshi market dict doesn't expose set/round/over for these sports today. State path is forward-compatible. **6 writers** call `regime.tag()`; only `live_watcher` has match-state in scope. **One-line elapsed_seconds add** to `_tick_momentum`'s `mom_ctx` dict covers all 5 dampener call sites. `REGIME_KEYS` tuple grew 4 → 5. `tools/backfill_regime.py` reworked for in-place extension of partial-regime records; backfilled 114,772 historical records. +75 regime tests including the regression guard. **Hidden gap:** `tools/live_momentum_dataset.py` doesn't extract `regime_match_phase` to the CSV (key-agnostic claim was false). Filed as Session 38d.

### ☑ Session 35 — Daily + weekly report generators (Apr 29)
The bot collects 8 streams of state and has 9 analysis tools, but seeing "is everything healthy and what did the bot learn" required ad-hoc invocation of each tool. Session 24's `weekly_digest.py` covered 8 sections + weekly cadence but had no daily counterpart and didn't lead with a health-pulse. Without automation, the analysis ritual would happen at May 2 / May 18 and then drift. **Three new tools (gitignored — local-only per project convention):** `tools/_report_helpers.py` (~750 LOC: shared utilities + 10 parameterized "shared section" renderers); `tools/daily_report.py` (CLI with `--date YYYY-MM-DD --regime-by AXIS`, 10 sections at 24h ET window, output to `bot/state/reports/daily/`); `tools/weekly_report.py` (replaces weekly_digest.py — same 10 shared sections at 7d + 6 weekly-only: §11 week-over-week deltas, §12 buckets, §13 dataset rebuild, §14 excursion + exit-replay, §15 calibration findings, §16 retuning candidates; output to `bot/state/reports/weekly/`). **Discipline:** first I/O writes the header so partial reports survive mid-run crashes; each section wrapped in `_safe_section` (single source's failure renders `[section unavailable: REASON]` and the script continues); imports throughout (no subprocess) — every called tool exposes a clean render function. **63 new tests across 3 files.** Live verification: `daily_report.py --date 2026-04-29` rendered all 10 sections green; `weekly_report.py --week-end 2026-04-26` rendered 16/16 sections and **§15 surfaced 2 real Session 36+ retuning candidates automatically** (`vig_stack_series.non_stable_below_weather_floor` +0.2438 n=95, `vig_stack_series.edge_below_threshold` +0.0350 n=68). Crash test passed (artificial cohort_report break → 9/10 sections rendered + the broken one marked unavailable). Recurring scheduled-task chats wired separately: daily 3 AM ET, weekly Sundays 6 AM ET. Both auto-update `REPORT_CALENDAR.md` Last Run stamps.

### ☑ Session 36 — Vig_stack TP/SL exemption + exit_reason persistence (Apr 29)
Session 35's first weekly report flagged `vig_stack_series.non_stable_below_weather_floor` (+0.2438 mean rel-CLV at n=95) as a mis-tuned gate. Investigation revealed the CF signal was real but the wrong fix was implied. **Diagnosis:** Battle Scar #9 in CLAUDE.md claims vig_stack is exempt from auto-exits ("structural edge — only ladder vig collapse should exit") but code reality only had `edge_flipped` exempt; `auto_take_profit` and `auto_cut_loss` had NO opp_type filter. For vig_stack-NO at 95¢ entry, `cut_loss` triggers at -30% pnl_percent which is a ~2.85¢ adverse yes_ask move — trivially common. 32% of vig_stack paper trades (46/144) were `exited_early` with mean −$5 to −$10 across ALL families. The floor gate was the conservative downstream defense against a broken cut path. **What shipped:** `bot/main.py` _VIG_STACK_OPP_TYPES constant + extracted `_dispatch_position_alerts()` from `_main_loop`; vig_stack TP and SL paths now log `"<path> SKIPPED for <ticker> (vig_stack — structural, hold to settlement)"` and continue. `bot/tracker.py` threads `opp_type` onto the 3 alert dicts. `bot/executor.py` `_paper_record_exit(reason="unknown")` persists `exit_reason` field; both call sites in `exit_position` thread the caller's reason. **11 new tests pass; identical 12 pre-existing failures vs. baseline; zero new regressions.** May 6 day-7 hold-to-settlement check + May 13 day-14 floor signal recheck routines queued — if floor signal diminishes meaningfully on May 13, diagnosis is confirmed and `VIG_STACK_WEATHER_MIN_PRICE` becomes a Session 39+ retuning candidate.

### ☑ Session 36-followup — Backfill_regime test fixes (Apr 29)
Session 36 verification ran the full pytest suite and surfaced 12 pre-existing failures. 10 were documented historical tech debt; 2 were NOT pre-existing — Session 34 (commit `46ebaa8`, ~16h old) leftovers. Session 34 changed `tools/backfill_regime.py` to extend partial-regime records in place instead of skipping; production code change is correct and is locked by `test_existing_regime_fields_unchanged` in `test_regime.py`, but two tests in `test_backfill_regime.py` were left asserting the old skip-when-present semantics. Renamed + flipped: `test_backfill_skips_records_with_existing_regime` → `test_backfill_extends_records_with_partial_regime` (n==0 → n==1); `test_backfill_json_array_real_records_and_skips_disabled` → `test_backfill_json_array_extends_partial_regime` (n==1 → n==2). Added `test_backfill_skips_records_with_complete_regime` for the actual NEW skip case. 8/8 backfill_regime tests pass post-fix. Net effect: 12 → 10 failures.

### ☑ Session 37 — Test hygiene cleanup — suite baseline 12 → 0 failures (Apr 29)
Tier-paid all 10 documented pre-existing test failures from the Apr 29 audit. Each fix matched test to shipped reality (production wins). Net result: clean suite baseline. Future sessions report "0 failures unchanged" instead of "10 documented failures unchanged" — any new failure is a real regression. **Group A (`__new__()` bypass-init AttributeError, 2 tests):** `tests/test_live_watcher.py:125, :168` — added `_trailing_active`, `_peak_values`, `_started_at`, `mode`, `_match_title`, `ticker`, `_price_history`, `_tick_telem` inline. **Group B (stale config/strategy pins, 5 tests):** broadened money-shape rejection assertion (Apr-16 reserve guard fires earlier than position cap); inverted `test_eth_in_active_strategies` → `test_eth_not_in_active_strategies`; weather floor 0.90 → 0.93 + KXHIGHDEN-A fixture rewrite + ladder math recompute; pinned `datetime.datetime` to mid-day UTC via monkeypatch (executor uses `from datetime import datetime` so module-level patch on `bot.executor.datetime` doesn't reach it). **Group C (silenced watchdog, 2 tests + dead production code):** deleted dead `_watchdog_alert` machinery (init, set, silenced-send block) at `bot/main.py:254/288/318` and both timing-out tests; preserved the `logger.warning` diagnostic. **Group D (per-day cap, 1 test):** monkeypatched `bot.clv.datetime` so day-1 records get `recorded_at` stamps on day 1. **Verification:** `pytest tests/ --timeout=15 --tb=no -q` → 1165 passed, 0 failed (was 12 failed, 1154 passed). Bot restarted (Group C touched main.py); orphan PID killed per Battle Scar #3.

### ☑ Session 38a — ATP main-tour disable re-evaluation (Apr 29)
Apr 29 night Investigation #1 re-ran `tools/live_momentum_buckets.py` against the regenerated dataset (714 rows, 321 with settled CLV) and surfaced a strong asymmetric-evidence pattern. The Apr 20 disable evidence had been: `atp_challenger` n=17 trades −$7.80 (solid), `atp` (main tour) bundled in "precautionarily" (no direct main-tour evidence cited), `wta` n=1, `wta_challenger` n=1 — all 4 disabled. Tonight's settled-CF evidence: **main-tour ATP shows +$11.32¢ avg CLV across n=56 settled rejected opportunities, with 10 leader-loss settlements (n_neg=10) — passes the survivorship-bias bar Session 30-followup defined**. Same asymmetric-evidence pattern Session 30-followup flagged for wta_challenger (disable was based on challenger evidence, not main-tour evidence). Hygiene checks (per Session 38a brief): (1) Survivorship — n_no_won=10 ≥ 10 threshold ✓; (2) Skip_reason distribution dominated by sport_disabled ✓; (3) Historical pre-Apr-20 main-tour ATP trades zero/near-zero — decision is purely-counterfactual ✓. **Outcome A shipped:** `bot/config.py:128` — `MOMENTUM_DISABLED_SPORTS = {"atp_challenger", "wta", "wta_challenger"}` (removed `"atp"`). 30-line evidence comment block now carries Apr 20 + Session 38a evidence. No test changes (verified `MOMENTUM_DISABLED_SPORTS` has no contents-asserting tests). 1165/0 baseline held. Bot restarted; post-restart `bot.log` shows ATP main-tour watchers spawning + ticking (KXATPMATCH-26APR30RUUBLO-RUU at 73¢ leader, KXATPMATCH-26APR30COBZVE-ZVE at 72¢ leader); zero `sport_disabled` rejects in post-restart log window (pre-deploy behavior would have emitted one immediately). **May 13 day-14 re-validation routine queued** at `~/.claude/scheduled-tasks/session-38a-atp-revalidation/` — pulls post-deploy cohort settled CFs + realized P&L from `paper_trades.json` and decides CONFIRM or REVERT per the rule documented in CLAUDE.md Session 38a block. Mirrors Session 22 pattern.

### ☑ Session 38a-2 — WTA main-tour disable re-evaluation (Apr 30 — Outcome B, doc-only)
Mirror of the Session 38a methodology but applied to WTA. Pulled post-deploy cohort + survivorship checks. **Outcome B (held disabled):** insufficient evidence at the time to revert. Documented rationale rather than shipping a config change. **Now contradicted by Session 43b's discovery agent finding** (`no_leader/wta` +20¢ mean CLV on n=15) — candidate Session 44 will revisit with the fresh evidence using the same methodology.

### ☑ Session 39 — Asyncio event loop blocking on snapshot_universe (Apr 30, CRITICAL)
A 12-hour bot wedge on Apr 30 traced to a synchronous `snapshot_universe` call inside the asyncio event loop blocking everything downstream including live_watcher notifications. Fix wrapped the call in `loop.run_in_executor(None, snapshot_universe)` at `bot/main.py:1188-1196`. Defense-in-depth: added a deadline check inside `bot/universe.py:277-307`'s retry loop so a partial-snapshot path can no longer hold the loop indefinitely. Bot resumed full operation post-restart. May 1 day-1 spot check + May 7 day-7 flaky-Kalshi stress check routines queued. Highest-impact fix in the project's history — without it, every other Apr 30 + May 1 finding would have been moot.

### ☑ Session 40 — Live_momentum EE-rate investigation (Apr 30 — Outcome C, doc-only)
Investigated whether the 32% early-exit rate on live_momentum is the leak. Per-exit-reason P&L breakdown showed exits are roughly balanced — TP wins, SL losses, trail-stops in between. The asymmetry IS real but it's at the entry/sizing layer, not the exit layer. Surfaced "7 lost-class trades net −$91.68 with avg −$13.10 vs avg win +$3.41" — the asymmetric-loss multiplier. Ruled out exit-side as the leak. No code change.

### ☑ Session 41 — TP/SL ratio sweep for live_momentum (Apr 30 — Outcome C + Phase 0 plumbing)
Swept GLOBAL `LIVE_TAKE_PROFIT_CENTS × LIVE_STOP_LOSS_CENTS` across a TP×SL grid. Loud finding: **SL axis is structurally flat** within every TP row across all 4 SL values. Phase-1 architecture audit revealed why — at runtime, `bot/live_watcher.py:2362,2454` (production gate) and `bot/strategies/live_momentum.py:277-278` (back-tester strategy port) resolve TP/SL as `sport_profile.get(...)` first with the global as fallback. Every enabled sport (NBA, NHL, UFC, tennis) has both keys set in SPORT_PROFILES — so the global SL sweep was structurally shadowed for 24 of 31 cohort trades. Phase 0 plumbing extended `tools/tick_backtest.py` with `SWEEP_GRID_PRIMARY` + `SWEEP_BASELINE_TP_SL`. No bot config change.

### ☑ Session 42 — Per-sport TP/SL variants for live_momentum (Apr 30 — Pattern C all sports + Phase 2 plumbing)
Per-sport sweep on the architecturally-correct axis after Session 41. Three sweep reports (`bot/state/reports/session_42_{nba,nhl,ufc}_tp_sl_sweep_2026-04-30.md`). All three sports produced Pattern C: NBA (sign disagreement train vs test, n_test=4 < 5), NHL (top-3 ties, n_test=2 < 5), UFC (sign disagreement, n_test=3 < 5, all 12 variants loss-making in training). **SL-axis-flat repeats per-sport** — production exit-priority (`bot/live_watcher.py:2306-2316`) fires TAKE_PROFIT / NEAR-SETTLE / TRAILING / SCORE_FLIP / OPP_RUN before STOP_LOSS, so making SL value sport-specific can't help when SL doesn't fire on winners regardless. **Direction-setting conclusion across Sessions 40 + 41 + 42:** Win:Loss=0.261 from Session 40 is structural and not addressable by exit-side parameter tuning at any granularity tested. Sizing (Kelly cap on high-confidence-but-losing trades) is the strongest queued candidate. Phase 2 plumbing extended `bot/strategies/live_momentum.py` with `sport_overrides` constructor arg + the per-sport sweep grid in `tick_backtest.py` + 21 new tests including the tennis-aliasing regression and the default-path byte-identical regression. No bot config change.

### ☑ Session 43a — Discovery Agent framework + 2 SFPHI-catching heuristics (May 1)
Built the chassis: `DiscoveryContext` (loads ALL 14 bot data sources once, streaming iterators for the multi-GB ones), `Finding` dataclass with stable fingerprint hash for cross-run dedup, `Heuristic` Protocol with declared `data_sources` + isolation in `main.py`. Two heuristics shipped: `outlier_pnl` (catches PAPER-4A16F5D2 = SFPHI as HIGH severity) + `cohort_emergence` (catches new (opp_type, sport) cohorts). `test_sfphi_regression.py` is a P0 value-prop lock-in. launchd plist `com.hustle-agent.discovery` registered, daily 6:00 AM ET. 7 commits, 34/34 discovery tests + 1235/1235 full repo (no regressions). First real-data run produced 13 NEW findings including SFPHI surfacing as designed AND a vig_stack_futures cross-sport cohort lead.

### ☑ Session 43-investigate — vig_stack_futures cross-sport lead (May 1, doc-only follow-up)
Followed up on 43a's first-run cohort_emergence finding. Decomposed the 867 mlb / 599 nba / 583 nhl raw decision counts: 2044 of those are correctly-classified long-dated futures (KX{MLB,NBA,NHL}-*, settle 2028+) at 0% accept rate — not a bug. The actual SFPHI bug-pair surface (KXMLBGAME-* misclassified as vig_stack_futures) is a SINGLETON: 5 decisions, 1 accept = SFPHI itself. The mechanism does NOT generalize. **Real new lead surfaced**: `non_stable_below_weather_floor` is rejecting +EV opportunities on sports futures (KXNBA-26-OKC at edge=+11.48¢ rejected on no_ask_prob=0.48 vs floor=0.93; 417 such rejections across mlb/nba/nhl futures in 2 days). vig_stack as a strategy survives on outliers — 125 settled trades net +$67; without 2 outliers (SFPHI +$172 + KXINX +$81) would be −$187 net. Candidate Session 44 (`non_stable_below_weather_floor` floor-keying) and Session 45 (vig_stack outlier dependence). Refinement folded into 43b: `cohort_emergence` should distinguish futures-vs-per-game and demote severity when 0 accepts/trades.

### ☑ Session 43b — Discovery Agent: 6 additional heuristics + cohort_emergence refinement (May 1)
Added `threshold_proximity`, `counterfactual_hotspots`, `universe_gap` (reframed: decisions vs current universe, no archive dir needed), `live_tick_anomalies` (streaming + memory-safety), `cadence_outcome`, `log_error_spike` (streaming + memory-safety) on the 43a chassis. Plus the cohort_emergence refinement folded in from 43-investigate (`unique_tickers_recent` + `accepts_recent` + `paper_trades_recent` evidence; severity demotes to `info` when accepts==0 AND trades==0; futures-vs-per-game sport classification via new `_sport_classifier.py` wrapper). 8 commits to origin/main. Tests: 1309 full repo (was 1235 baseline + 74 new), 108 discovery tests, 0 regressions, SFPHI regression still green. **Day-2 e2e produced 3 HIGH `counterfactual_hotspots` findings:** `no_leader/wta` +20¢ CLV n=15 (directly contradicts Session 38a-2 WTA hold — candidate Session 44); `no_leader/nhl_game` +22¢ CLV n=19 (production code touch, candidate Session 45); `no_leader/atp` +9¢ (corroborates Session 38a re-enable, folded into May 13 re-validation). Schema corrections caught at plan-time (saved a debug cycle): `clv_cents` not `outcome_clv_cents`, `market_result` not `outcome_settlement`, `skipped_by_gate` canonical, `no_price_below_floor` not `price_floor`, `bot.log` `[YYYY-MM-DD HH:MM:SS]` not ISO-T, `live_ticks` has no volume field. The discovery agent is now consistently surfacing first-class production decisions on its daily run.

### ☑ Session 44 — Discovery agent housekeeping: gate-flow analysis + IPL spot-check + lead reprioritization (May 1, doc-only ~30min)
Pre-check on the discovery agent's day-2 HIGH `no_leader/wta` finding revealed gate-flow caveat: `no_leader` fires in OUTER `scan_live_matches` loop before `sport_disabled` is checked in inner `_tick_momentum`. Per `bot/clv.py:213`'s `LIVE_MOMENTUM_TUNABLE_SKIP_REASONS`, sport_disabled is NOT in the CF emission allowlist — so the entire Session 38a-2 WTA evidence set (n=48 mean −1.23¢) is from outer-loop tunable rejections, none from sport_disabled. Decomposing: 15 no_leader CFs at +20¢ (sum +300¢) vs 33 other CFs (low_volume + no_vol_growth_*) at sum −359¢ (mean −10.88¢). **Re-enabling WTA wouldn't change the no_leader CFs at all** (they'd still hit no_leader regardless of disable status). The actual lever the agent points at is leader-detection tuning — separate from the disable decision. Outcome B (held disabled) per Session 38a-2 stands. Updated Session 38a-2 watch-list trigger to include "re-evaluate per-sport `MOMENTUM_LEADER_MIN` for WTA if no_leader/wta sub-cohort reaches n=30 with sustained +CLV." Also: IPL spot-check on the 7-trade cohort the agent flagged via cohort_emergence — n=7 settled, total −$3.12, mean −$0.45, 57% WR, 6 of 7 EE'd. Sample too thin (Session 38a bar was n=56) to act either way — Session 38b stays queued.

### ☑ Session 45 — `no_vol_growth_first_seen` retuning HELD (May 1, doc-only, Outcome C)
Discovery agent surfaced cross-cohort `counterfactual_hotspots` findings on `no_vol_growth_first_seen` across atp / atp_challenger / nhl_game; brief proposed a 10–20% global threshold relaxation. Disqualified at the gate-mechanism layer: `no_vol_growth_first_seen` fires on `_prev_scan_volumes.get(ticker, 0) == 0` — i.e., the first time the bot ever sees a ticker. **Binary cycle-delay, NOT a tunable threshold.** No constant in `bot/config.py`. Instrumentation ≠ knob. Outcome C HOLD on Layer 1 (no knob) alone. Defense-in-depth observation: even if a future structural change makes the cycle-delay bypassable, cross-cohort distribution shows the per-cohort signal does NOT clear cross-cohort hygiene (n=130 across 7 sports, mean −0.05¢ raw / +0.40¢ outlier-trimmed once nba/ipl/wta included). Watch-list trigger for ALL of: structural change shipped (e.g., `_prev_scan_volumes` persistence across restarts), cross-cohort settled-CF count grows past n=300 with `n_no_won >= 30`, AND cross-cohort mean CLV >= +5¢ with outlier-trimmed >= +3¢.

### ☑ Session 45 post-session correction — Canonical Data Schema Reference added (May 1, doc-only)
Layer-3 disqualification in Session 45 ("survivorship n_no_won=0 across every cohort") was based on a verification-query schema bug: brief's Step-3 script checked `r.get('market_result') == 'no_won'` but actual canonical value is `'no'` (verified directly: 1005 'no' + 865 'yes' records in `clv.json`). Discovery agent's heuristic at `counterfactual_hotspots.py:64` checks `== 'no'` — was correct all along. Survivorship actually PASSES every cohort (n_no=30/130). **Outcome C HOLD remains correct on Layer 1 alone.** Defense-in-depth observation: Layer 1 saved the right outcome from a wrong-rationale path. Added new "**Canonical Data Schema Reference**" section pinned at the top of CLAUDE.md (mirrored to `tools/discovery_agent/README.md`) — single source of truth for `clv.json` / `decisions.jsonl` / `paper_trades.json` field names + value enums. Every session prompt that touches state files MUST cross-reference as Step 0. Two schema mistakes in 24 hours (Session 43b plan-time fix + Session 45 verification error) made this necessary; if a third instance happens, the next decision could ride on falsified evidence.

### ☑ Session 46 — `no_vol_growth_idle` retuning HELD (May 1, doc-only, Outcome C)
Companion gate to Session 45's no_vol_growth_first_seen. Critical structural difference: `no_vol_growth_idle` IS a real threshold (`if vol_growth < 500` — hardcoded magic number at `bot/live_watcher.py:3140`, not in `bot/config.py`). Outcome A (relax) was structurally on the table this time. But evidence killed it on three independent grounds: cross-cohort cherry-pick (combined mean −1.34¢, outlier-trimmed −0.76¢), gate-flow neutralization (2 of top-3 positive cohorts are atp_challenger and wta — currently DISABLED, so relaxing 500→400 produces zero new actual trades), bimodal distribution (25 records in [−100, −50], 73 records in [+0, +50], nothing between −50 and 0 — CLV is structurally a settlement-success signal, not a near-the-line gate-tunability signal). Side correction: Session 45's strikethrough'd lookahead claim ("no_vol_growth_idle n=98 has n_no_won=0 everywhere") falsified on canonical schema — actual combined n_no=25, survivorship PASSES.

### ☑ Session 47 — `counterfactual_hotspots` cross-cohort context refinement (May 1, ~1h coder, discovery agent only)
Sessions 45 + 46 both shipped Outcome C HOLD on the same failure mode: per-cohort flag positive, cross-cohort distribution flat-or-negative, top positive sports often in MOMENTUM_DISABLED_SPORTS (gate-tuning structurally neutralized). Two consecutive sessions burned ~3h coder time re-deriving the same cross-cohort math. Refinement moves the math INTO the heuristic. Added 8 new evidence keys (cross_cohort_total_n, cross_cohort_n_sports, cross_cohort_mean_clv_cents, cross_cohort_trimmed_mean_clv_cents, cross_cohort_n_positive_sports, cross_cohort_n_negative_sports, cross_cohort_breakdown, n_disabled_sport_cohorts_in_top3, this_cohort_is_disabled_sport). 3-trigger severity demotion ladder (cross-cohort mean<0 / trimmed mean<3¢ AND raw≤0 / disabled-sport — each demotes one level on high→notable→info ladder). MOMENTUM_DISABLED_SPORTS imported from `bot.config` (single source of truth). 12 new tests (23 total). Real-data run after deploy: Sessions 45/46 cohorts demoted to INFO with cross-cohort context inline. **Bonus catch:** Session 44's `no_leader/wta` finding also auto-demoted HIGH → INFO. Refinement covers the broader cherry-pick + disabled-sport pattern, not just the immediate triggers.

### ☑ Session 48 — `concurrent_attack_angles` heuristic — search-frontier expander (May 1, ~2.5h coder, discovery agent only)
Per Tyler's directive ("the bot should find new attack angles itself, not me enumerating them") — added the 9th discovery heuristic. For each event family with at least one ALREADY_TRADING ticker, classifies sibling markets in 3 buckets (ALREADY_TRADING / SCANNED_NOT_TAKEN / NEVER_SCANNED) and emits two finding types: `concurrent_fire_candidate` (where SCANNED_NOT_TAKEN sibling shows positive concurrent CLV when primary strategy wins — surfaces multi-strategy-per-event opportunities like "live_momentum LEADER + total-points UNDER on same NBA game") and `scanner_gap` (where NEVER_SCANNED market types worth building a scanner for). Same Session 47 cross-family demotion ladder applied at the strategy-pair × event-family level. 15 new tests (plan asked for 13; coder added 2 helper sanity tests). Smart coder deviation: `_market_type_from_ticker` originally specified as series-prefixed form (`KXNBAGAME/team`) but coder caught on first cross-family-negative test failure that the prefix form would defeat the demotion ladder by treating NBA-totals and NHL-totals as different keys. Switched to suffix-only (`team`); series identity preserved via `series_ticker` + `event_family_pattern` evidence keys. Test-driven save. Real-data day-1 emitted 0 findings (acceptable — universe × traded-event-family overlap minimal today; tomorrow's 6 AM scheduled run is the natural validator). Each shipped concurrent strategy from a Session 48 finding becomes another input to the next day's 48 run, surfacing further angles. Compounding strategy expansion.

### ☑ Session 49 — Per-sport `size_multiplier` for live_momentum (May 1, ~1.5h coder, FIRST production-code change today)
Live_momentum loss-class breakdown (measured May 1 evening) showed asymmetric-loss multiplier 0.54 ratio (losses 1.85× wins) concentrated in NBA (n=21, −$26.57, 48% WR), UFC (n=8, −$8.30, 12% WR), IPL (n=7, −$3.12, 14% WR). NHL (+$7.80, 70% WR, n=10) and ATP (+$8.60, 25% WR, n=4) were the only positive cohorts but n was below the Session 38a n=56 bar to size up. Added `size_multiplier` field to `SPORT_PROFILES` (same architectural surface Sessions 41/42 use for TP/SL overrides) — **NBA: 0.5x**, **UFC: 0.5x**, **NHL: 1.0x explicit**, **MLB: 1.0x explicit** (smart coder deviation: making the no-change decision EXPLICIT documents that we know about these sports and chose 1.0; provides future touchpoint for sizing-up sessions). IPL deferred per coder Phase-1 verification (no existing SPORT_PROFILES entry; Session 38b queued to disable; adding fresh entry would require calibrated defaults for non-sizing keys). Tennis-alias hazard called out (atp/atp_chal/wta/wta_chal alias same `tennis` dict by reference at config.py:341-342) — all tennis sizing changes deferred to a future session that explicitly addresses the alias structure. 8 new tests in `tests/test_sizing.py` (created from scratch). Bot restarted via `launchctl kickstart`; Battle Scar #3 protocol caught + killed orphan PID from prior day. Single PID 19189 post-restart. May 15 +14d re-validation routine scheduled mirroring Session 38a methodology — pulls post-deploy live_momentum trades grouped by sport, decides CONFIRM / EXPAND / REVERT.

### ☑ Session 50 — Trade-record observability: confidence + dqs + sport on live_momentum (May 1 → May 2 transition, ~1h coder)
Pre-let-it-run-for-14-days observability audit surfaced 3 missing dimensions on live_momentum `paper_trades.json` records: `confidence` (vig_stack records it; live_momentum was writing 0 for all 74 settled trades — verified directly), `dqs` (Session 33 added it to `live_ticks.jsonl` rows but never threaded to paper_trades), `sport` (derived from ticker prefix every analysis). Without these, the May 15 Session 49 re-validation could only know "NBA was bad" — not "NBA was bad ONLY at confidence > 0.85, suggesting confidence-ceiling at entry is the right lever instead of sizing-down." Forward-only persistence shipped: `bot/executor.py:1050-1086` inline paper_trades.append extended with 3 conditional emits (paper_confidence/paper_dqs/paper_sport); `bot/live_watcher.py:1735-1773` `_auto_bet_momentum` threads all 3 with **composite confidence formula `min(1.0, dqs * (1 + max(0, wp_edge)))`** (DQS dominant signal, wp_edge boost clamped non-negative + total clamped 1.0); `bot/live_watcher.py:2675-2680` `_auto_bet` (WATCH path) sets `paper_sport` only (WATCH path doesn't compute DQS). 4 new tests in `TestSession50PaperTradeFields`. Schema reference (CLAUDE.md "Canonical Data Schema Reference") updated with new field semantics + Session 50 forward-only notes. Bot restarted; **Battle Scar #3 caught its 3rd orphan today** (morning false-alarm, Session 49 restart, Session 50 restart). PID 82747 fresh. **Vig_stack regression PASS:** 3 vig_stack records written post-restart carry the original 13-key shape — no `dqs`/`sport` leak. Byte-equality preserved exactly. The 14-day clock for the May 15 Session 49 re-validation starts now with full bucketable data.

### ☑ Session 51 — Strategy Lab v1: rapid hypothesis prototyping for new strategies (May 2 early hours, ~2.5h coder, no production code)
Per Tyler's directive ("i want strategy prototyping too") — paired with Session 48's `concurrent_attack_angles` heuristic. Without a fast prototyping path, acting on a Session 48 candidate meant building a production scanner + wiring into ACTIVE_STRATEGIES + restarting bot + waiting weeks for trades + settlements before knowing if the idea worked. **Lab cuts that to seconds.** New `tools/strategy_lab/` directory (gitignored generally; re-included via `.gitignore` exception). 7 tracked files: `__init__.py`, `README.md` (8KB — how to write a candidate, how to read a report, lab limitations, canonical schema reference), `candidate.py` (CandidateStrategy Protocol + CandidateOpportunity dataclass), `data_loader.py` (streams universe.jsonl over date range, builds clv_lookup keyed by ticker, builds existing_decisions_by_ticker for context), `evaluator.py` (for each would-have-bet, finds matching clv records ±N hour window, computes hypothetical P&L via settlement-anchored formula, aggregates per-sport + reason histogram + top winners/losers), `driver.py` (CLI: `python3 -m tools.strategy_lab.driver --candidate <name> --days <N>`), `reports.py` (markdown rendering with **loud lab-limitations header** on every report). Plus `candidates/example_total_points_under.py` reference implementation (stub showing the contract — produces 0 would-have-bets on current data because KXNBAGAME-*-TOTAL markets aren't in current universe, lab handles 0-candidate runs gracefully) and `.gitkeep` placeholders. **11 tests** (10 brief-required + 1 bonus archive-coverage): protocol compliance, data_loader streams + window-filters, evaluator scores winner/loser/unresolved, reports renders zero AND with-candidates gracefully, driver smoke on real data, AND **`test_canonical_schema_used_throughout`** — assertion-style commit-time guard that lab source files don't introduce the `'no_won'` substring (the anti-pattern that almost falsified Session 45). Schema discipline now self-enforcing in TWO places: CLAUDE.md doc + commit-time test. **End-to-end "find new attack angles" loop closed:** discovery agent surfaces candidates → strategy lab validates them in seconds → real production scanner ships if validated → that scanner becomes another input to the next day's agent run. Compounding strategy expansion. The post-Session-48 → Session-51 workflow now exists end-to-end. **The 14-day clock for the May 13-18 re-validation cluster is now at hour 0.**

### ☑ Session 73 — Strategy lab per-pair-key dedup fix (May 8, ~1.5h, lab-only, methodology codified)
Session 72's first run of `cross_market_correlation` produced **+$2,567 per-emit Σ P&L** that flipped to **-$4.16 per-unique-pair-key Σ** when manually re-aggregated by (ticker, side). Stateful candidates re-emit on every scan while a divergence persists; the same hypothetical opportunity got counted 35-122 times (median 35x) inflating per-emit numbers. Same shape will hit any future stateful candidate. Session 73 bakes dedup into the lab so future Sessions don't have to re-find this lesson manually. **6 files modified, 1 commit + README sync, lab-only ship:** (1) `tools/strategy_lab/candidate.py` — `CandidateOpportunity` dataclass gained optional `pair_key: Optional[str] = None` field after `extra`; docstring distinguishes stateful candidates (MUST set per emit) from one-shot candidates (leave None); backward-compat preserved by `None` default. (2) `tools/strategy_lab/evaluator.py` — `aggregate()` extended to compute both per-emit (preserved verbatim) AND per-unique-pair-key metrics in one pass; new helpers `_compute_basic_metrics()` (factored from existing math), `_dedup_by_pair_key()` (first-emit-wins per unique key — matches "you'd enter the trade once" real semantic), `_median_emits_per_pair_key()` (amplification ratio diagnostic); `None` pair_keys count as own bucket — backward-compat for one-shot candidates is byte-identical. (3) `tools/strategy_lab/reports.py` — `render_markdown()` "Aggregate (resolved subset)" section now leads with **per-unique-pair-key (HEADLINE)**, then includes per-emit as **DIAGNOSTIC** with the median-emits-per-key amplification ratio; per-sport/top-5/reason-histogram tables stay per-emit so visible amplification (e.g., Session 72's 5× duplicate winner ticker) remains a "spot the problem" cue. (4) `tools/strategy_lab/candidates/cross_market_correlation.py` — added `pair_key=f"{ticker}|{side}"` to constructor at game-grain (matches Session 72's manual "140 unique (ticker, side) keys" re-aggregation); renamed pre-existing `extra["pair_key"]` (matchup-grain, diagnostic only) to `extra["matchup_key"]` so the new dataclass field has the canonical role. (5) `tools/strategy_lab/README.md` — new "When to set pair_key" subsection in the "How to write a candidate" walkthrough plus extension of the `CandidateOpportunity` fields code block. (6) `tests/test_strategy_lab.py` — 5 new cases (stateful single key, stateful distinct keys, one-shot None backward-compat regression — `pair_key=None` makes per-pair-key Σ EXACTLY equal per-emit Σ — non-negotiable, mixed stateful + one-shot, end-to-end synthetic amplification regression with sign flip per-emit +$2,720 vs per-pair-key -$80 mirroring Session 72's shape). Plus `_make_scored()` test helper. **Verification:** targeted 16/16 pass in 1.46s; Session 72 companion 20/20 pass (no regressions); full repo **1,511 passed / 0 failed in 32.26s** (Session 72 baseline 1,506 + 5 new); manual end-to-end on real bot/state/ reproduces Session 72 numbers EXACTLY — 140 unique pair-keys (51 resolved), median 35.0 emits per unique key, per-emit Σ +$2,567, per-pair-key Σ -$416 (= -$4.16 per-share × 100 contracts default). No `bot/` touch, no bot restart needed, no P&L change. **Pattern transfer:** any future stateful candidate that re-emits on persistent divergence/condition automatically gets correct dedup just by setting `pair_key`. Framework prevents the same false-signal trap. Operating Posture observation: Session 72 was a Pattern C lab evaluation (no production change) — investigation produced ONE durable methodological improvement, codified here as durable infrastructure rather than a one-time prose lesson. Mirror of Sessions 43b / 47 / 56.5 / 67 — the discovery agent / lab / fixtures keep teaching us their own boundaries; we make those boundaries first-class.

### ☑ Session 74 — `no_vol_growth_first_seen/nhl_game` Outcome C HOLD (May 8, doc-only, clean §10 HIGH)
Session 66 §10's lone remaining HIGH was `counterfactual_hotspots: no_vol_growth_first_seen/nhl_game` at 7d stable. Formal sub-cohort verification confirms the signal is real: `n=33`, `n_no=3`, `n_yes=30`, mean **+20.91¢**, **90.9% +CLV**. Gate-flow verification also confirms Session 45's structural disqualification still applies: `bot/live_watcher.py` records `prev_vol = _prev_scan_volumes.get(ticker, 0)` and skips when `prev_vol == 0` — a binary first-sight cycle-delay in the running process, with no `bot/config.py` threshold to relax. Same gate, same lever, same answer as Session 45's atp_challenger ruling: **Outcome C HOLD**. The per-cohort NHL-game signal is real but unactionable until architecture changes. New canonical watch-list trigger for all `no_vol_growth_first_seen/*`: re-open on architectural change (persist `_prev_scan_volumes`, first-sight entry path, or materially lower `LIVE_SCAN_INTERVAL`), OR cross-sport convergence (`nhl_game` + `atp_challenger` + one additional sport all `n>=30` with combined mean `>= +5¢`), OR post-architecture realized trades (`n>=20` positive EV). Until one fires, §10 findings on this gate are cycle-delay-disqualified and should carry Session 45 + Session 74 as "already evaluated" cross-references. Doc-only: no `bot/`, `tests/`, or `tools/` touch; no restart; full-suite baseline remains **1,511 passed, 0 failed, 0 skipped**.
