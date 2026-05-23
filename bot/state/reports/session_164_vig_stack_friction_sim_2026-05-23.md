# S164 — vig_stack execution-friction simulation (2026-05-23)

**SIMULATION ONLY.** Read-only on `bot/state/` (`paper_trades.json` + `archive/universe-*.jsonl[.gz]` + `universe.jsonl`); writes only this report. No scanner/config/`ACTIVE_STRATEGIES` change, no `PAPER_MODE` flip, no bot restart, no trades, no live Kalshi API. Imports no bot modules.

**Question:** PAPER mode instant-fills marketable limits at the limit price and charges no fees. Does the **+$762.20** vig_stack paper edge (267 settled) survive paying Kalshi fees, the ask, slippage, and the thin two-sided books S163 found — or is it a fill-quality mirage? This re-prices the SAME settled trades (won/lost/exited_early outcomes preserved) under progressively realistic execution.

## 0. Cohort & reconciliation

- Full cohort: **N=267**, recorded ΣP&L (L0, gross) = **$762.20** (reconciles to the +$762.20 headline). Σcontracts = 30,189.
- Ladder-core (KXHIGH*/KXINX, excl. 12 KXMLBGAME per-game): N=255, ΣP&L = **$514.33**, Σcontracts = 27,168.
- L0 is re-derived as `Σ(exit − entry)·contracts` with `exit = entry + pnl/c`, identical to the recorded `pnl` field by construction (penny-exact).

## 1. Robust friction trajectory (no join needed — all trades)

Entry price + contracts are on every trade, so fees and entry slippage are computed on the **whole cohort** with no snapshot dependency and no selection bias. This is the most defensible part of the analysis.

| scope | N | L0 paper | L1 +fees | L2 +fees+1c | L2 +fees+2c |
|---|---|---|---|---|---|
| FULL 267 | 267 | $762.20 | $502.17 | $213.46 | $-74.60 |
| ladder-core | 255 | $514.33 | $316.97 | $58.86 | $-198.66 |

- **Fees** (Kalshi `ceil(rate·c·p(1−p))`, KXINX 0.035 / else 0.07): FULL entry fees $242.20 + early-exit fees $17.83 = $260.03 eaten ($762.20 → $502.17). Won/lost settlement legs cost $0 (`p(1−p)=0` at p∈{0,1}).
- **Entry slippage** is −Δ·Σcontracts: FULL +1c → $213.46, +2c → $-74.60.
- **Exited-early exit-side risk** (entry+exit both slipped, exited_early subset only, N=52): no-slip L1 = $88.01; +1c both legs = $26.23; +2c both legs = $-35.30. (The dominant winners are large exited_early sells — offloading hundreds of contracts in a thin book is the real exit-fill risk.)

## 2. Concentration (the S134/S159 lesson — full cohort)

- Σ winners = $3,643.95 | Σ losers = $-2,881.75 | net = $762.20.
- **Top-3 winners = $673.74** of the net; **ex-top-3-winners = $88.46** (pre-fee). The top winners are all large KXMLBGAME per-game exited-early trades — the per-game 'vig_stack' S159 ruled out as a spread artifact (Outcome C).

| family | N | ΣP&L |
|---|---|---|
| KXHIGHAUS | 62 | $429.62 |
| KXMLBGAME *(per-game, S159-ruled-out)* | 12 | $247.87 |
| KXHIGHDEN | 48 | $175.78 |
| KXHIGHNY | 40 | $141.58 |
| KXHIGHCHI | 41 | $-69.24 |
| KXINX | 28 | $-72.92 |
| KXHIGHMIA | 36 | $-90.49 |

Top-5 winners / losers (ticker · P&L · entry · contracts · status):

- ▲ `KXMLBGAME-26MAY061905TEXNYY-NYY` $296.92 · 0.35 · 571 · exited_early
- ▲ `KXMLBGAME-26MAY092110ATLLAD-LAD` $204.30 · 0.44 · 454 · exited_early
- ▲ `KXMLBGAME-26APR291840SFPHI-PHI` $172.52 · 0.44 · 454 · exited_early
- ▲ `KXHIGHAUS-26MAY03-B78.5` $85.50 · 0.7 · 285 · won
- ▲ `KXHIGHMIA-26MAY03-B83.5` $85.50 · 0.7 · 285 · won
- ▼ `KXHIGHAUS-26MAY04-B82.5` $-200.00 · 0.8 · 250 · lost
- ▼ `KXINX-26MAY04H1600-B7212` $-200.00 · 0.8 · 250 · lost
- ▼ `KXHIGHMIA-26MAY05-T87` $-199.95 · 0.93 · 215 · lost
- ▼ `KXHIGHMIA-26MAY14-T93` $-199.95 · 0.93 · 215 · lost
- ▼ `KXMLBGAME-26MAY051905TEXNYY-NYY` $-199.88 · 0.38 · 526 · exited_early

## 3. Join coverage (entry-time snapshot, exact ticker)

- Earliest archived snapshot for cohort tickers: `2026-04-25T16:44:59.771877+00:00`. Trades before this have no contemporaneous book.
- Matched within ±30 min: **155/267 (58.1%)**.
- Matched within ±90 min: **180/267 (67.4%)**.
- **Selection-bias caveat:** the pre-archive trades are net-negative, so the joinable subset is the *more profitable* part of the cohort — tiers (a)-(d) below therefore **overstate** the friction-adjusted edge relative to the full cohort.

## 4. Spec tiers (a)-(d) — re-priced from the entry-time book (±90 min join)

### FULL (joined 180/267)

- **Is the ask already baked in?** recorded entry − observed `no_ask` = mean +0.1c / median +0.0c; 175 at/above ask vs 5 below; mean book spread 1.8c. (≥0 ⇒ paper already paid the ask or worse.)
- (a) MID, no fee (sanity): $956.18 | paper on same joined subset: $669.12 | L1 (recorded entry + fees): $445.16
- (b) ASK + fees: **$466.24**
- (c) ASK + fees + slippage: +1c → $201.09 | +2c → $-63.42
- (d) DEPTH-GATED (ASK + fees + 1c basis; fill-only-with-depth vs assume-full-fill):

  | OI floor | vol floor | rungs fillable | fill only w/ depth | assume full fill |
  |---|---|---|---|---|
  | 0 | 0 | 180/180 | $201.09 | $201.09 |
  | 0 | 100 | 130/180 | $-35.18 | $201.09 |
  | 0 | 500 | 79/180 | $-316.91 | $201.09 |
  | 50 | 0 | 148/180 | $65.37 | $201.09 |
  | 50 | 100 | 130/180 | $-35.18 | $201.09 |
  | 50 | 500 | 79/180 | $-316.91 | $201.09 |
  | 200 | 0 | 112/180 | $-0.95 | $201.09 |
  | 200 | 100 | 110/180 | $-39.35 | $201.09 |
  | 200 | 500 | 79/180 | $-316.91 | $201.09 |

### ladder-core (joined 170/255)

- **Is the ask already baked in?** recorded entry − observed `no_ask` = mean +0.1c / median +0.0c; 165 at/above ask vs 5 below; mean book spread 1.8c. (≥0 ⇒ paper already paid the ask or worse.)
- (a) MID, no fee (sanity): $863.06 | paper on same joined subset: $598.54 | L1 (recorded entry + fees): $424.55
- (b) ASK + fees: **$444.43**
- (c) ASK + fees + slippage: +1c → $205.21 | +2c → $-33.41
- (d) DEPTH-GATED (ASK + fees + 1c basis; fill-only-with-depth vs assume-full-fill):

  | OI floor | vol floor | rungs fillable | fill only w/ depth | assume full fill |
  |---|---|---|---|---|
  | 0 | 0 | 170/170 | $205.21 | $205.21 |
  | 0 | 100 | 123/170 | $270.47 | $205.21 |
  | 0 | 500 | 77/170 | $-51.49 | $205.21 |
  | 50 | 0 | 138/170 | $69.49 | $205.21 |
  | 50 | 100 | 123/170 | $270.47 | $205.21 |
  | 50 | 500 | 77/170 | $-51.49 | $205.21 |
  | 200 | 0 | 107/170 | $258.62 | $205.21 |
  | 200 | 100 | 106/170 | $251.20 | $205.21 |
  | 200 | 500 | 77/170 | $-51.49 | $205.21 |

## 5. Outcome

**Outcome B.**

**The numbers that decide it (ladder-core — the genuine weather/index vig_stack):**

- Fees alone (all 255 trades, no join, no selection bias): $514.33 → **$316.97** — survives.
- + entry slippage: +1c → **$58.86**, +2c → **$-198.66**. The sign flips between +1c and +2c — Σ27,168 contracts make per-contract slippage the dominant lever.
- ASK + fees + 2c, depth-gated (OI≥50, vol≥100, joinable subset, 123 fillable rungs): fill-only-with-depth **$103.29** vs assume-full-fill **$-33.41** — fill quality alone flips the sign, and the joinable subset is the *more-profitable* part of the cohort (upward-biased).

Trajectory — FULL: $762.20 (paper) → $502.17 (fees) → $-74.60 (fees+2c). LADDER-CORE: $514.33 → $316.97 → $-198.66.

The edge survives fees but the verdict past that depends on fill assumptions offline data cannot settle — thin/missing two-sided books and **no ground-truth fills** (`order_microstructure.jsonl` is empty in PAPER mode). The honest answer is **offline can't tell**: this BOUNDS the edge, it cannot CONFIRM it. **A small live-fill probe is the only way to resolve it** (spec below); do NOT scale or flip to live on offline evidence alone.

## 6. Minimal live-fill probe (SPEC ONLY — operator decides; do not run here)

- **Goal:** ground-truth slippage + fill-rate that offline data can't provide; populate `order_microstructure.jsonl` (empty until `PAPER_MODE=False`).
- **Families:** the most-liquid genuine ladder — KXHIGHAUS (highest ladder-core ΣP&L) and KXINX `-B` (the S160 benchmark with the deepest two-sided book).
- **Size:** smallest viable — 1-5 contracts per rung, hard per-day $-cap (e.g. $50 total notional), single rung at a time; never the 200-500-contract sizes that drive the paper concentration.
- **Measure (per order):** realized fill price vs limit (slippage_cents), fill rate, partial-fill count, latency_ms, time-to-fill, queue depth at place; join to `clv.json` for slippage-adjusted CLV via `tools/microstructure_report.py`.
- **Risk cap + abort:** stop after N orders or first day; abort if realized slippage > 2c median or fill-rate < 50%. Operator-gated GO per order.
- **Decision after probe:** compare realized slippage/fill-rate to the +1c/+2c and depth-gated assumptions here; only then a go-live / scale / wind-down call.

## 7. Honest limits

- **No ground-truth fills.** `order_microstructure.jsonl` is empty in PAPER mode; this re-prices recorded trades under assumptions, it does not observe real execution. It BOUNDS the edge conservatively; it cannot CONFIRM fills/slippage.
- **Coverage.** Tiers (a)-(d) use the ≤180/267 joinable trades (pre-archive entries have no book); that subset is the more-profitable part of the cohort → overstates the friction-adjusted edge.
- **Concentration.** ~$674 of the $762 net is top-3 per-game exited-early trades; ~$248 is the S159-ruled-out per-game family. The ladder-core cut is the honest base.
- **Depth proxy.** Two-sided book + OI/volume floors approximate fillability; the real book depth at the rung at order time is unobservable offline (S163 lesson).

## Notes

- **Fees.** `ceil(rate·contracts·p·(1−p))` cents per leg, `p=price_cents/100`; KXINX 0.035 (financial), else 0.07 (general; conservative). Mirrors S159/S160 exactly (`tools/sim_commodity_vig_stack.py`).
- **Exit value.** `exit = entry + pnl/contracts` reproduces recorded `pnl` to the penny (won→~1.0, lost→~0.0, exited_early→sell price); friction perturbs only the ENTRY price, holding the realized exit/settlement value fixed.
- **Join.** Each settled trade matched to the nearest universe snapshot of its EXACT ticker within ±90 min; `(ticker, ts)` deduped (the 2026-05-17 day is archived as both `.jsonl` and `.jsonl.gz`).
- **No dedup of trades** (real settled trades, not CFs — per spec); concentration is foregrounded instead.
