# Cross-Platform Settlement Matcher Design - Session 105 Outcome B

Date: 2026-05-11

## Decision

Do not ship `bot/cross_platform_matcher.py` in Session 105.

The gating premise did not reach the implementation bar: public market metadata
exists on both sides, but there is no public, deterministic, labeled
Kalshi-to-Polymarket equivalence corpus suitable for validating zero false
positives.

This is not a data-access kill. It is a validation kill-for-now:

- Polymarket historical metadata is publicly reachable through Gamma.
- Kalshi settled-market metadata is publicly reachable through Kalshi's REST
  API and can enrich our paper-ledger tickers.
- Public cross-platform match products exist, but the available ones are
  vendor-controlled, paid, AI/LLM-based, active-market-only, or unlabeled.
- A deterministic matcher without a trusted validation corpus would create the
  exact illusion of safety this gate is meant to prevent.

Outcome A is rejected. Outcome C is too strong because the venue data exists and
a future corpus could unblock this path.

## Data Availability Check

### Polymarket

Primary source: Polymarket Gamma API documentation.

Relevant public endpoints:

- `GET https://gamma-api.polymarket.com/markets`
- `GET https://gamma-api.polymarket.com/events`

The docs state that Gamma has public, unauthenticated market/event endpoints and
that `closed=true` is the way to include historical markets. A direct probe with
a browser user-agent confirmed closed market/event payloads include:

- `question`
- `description`
- `resolutionSource`
- `endDate`
- `closedTime`
- `category`
- `outcomes`
- `outcomePrices`
- `closed`

Caveat: Gamma does not consistently expose a clean `resolved_outcome` enum in
the sampled payloads. For many binary markets the winner can be inferred from
final `outcomePrices` near `1/0`, but older rows can be messy. Any future
Polymarket client must normalize this conservatively and treat ambiguous final
prices as `INSUFFICIENT_DATA`.

### Kalshi

Sources:

- `bot/state/paper_trades.json`
- `GET https://api.elections.kalshi.com/trade-api/v2/markets?status=settled`
- `GET https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}`

Direct enrichment of our last-90-day terminal paper-ledger universe produced:

- Terminal paper rows inspected: `327`
- Unique terminal tickers: `320`
- Kalshi API enriched resolved tickers: `320`
- Fetch errors: `0`

The enriched Kalshi market payloads include:

- `ticker`
- `title`
- `yes_sub_title`
- `no_sub_title`
- `rules_primary`
- `rules_secondary`
- `close_time`
- `settlement_ts`
- `result` (`yes` / `no`)
- prices and settlement value fields

This is enough to build Kalshi-side market objects with question text, close
date, resolution text, and outcome.

### Public Crosswalk / Ground Truth

No acceptable public validation corpus was found.

Rejected sources:

- Predexon exposes `matching-markets` and `matching-markets/pairs`, but the docs
  say matching is LLM-based, may hallucinate, requires Dev/Pro access for
  matching, and returns active exact pairs rather than a public historical
  labeled corpus.
- Matchr documents AI/embedding-based matched markets and claims high-confidence
  accuracy statistics, but does not expose a downloadable labeled ground-truth
  dataset.
- PMXT / 0xInsider / Skreenr / Conduit / MarketPinger advertise matched-market
  products or dashboards, but no open, auditable labeled corpus was found.
- SimpleFunctions' Hugging Face `settled-markets` dataset exposes settled
  Kalshi and Polymarket rows with `venue`, `ticker`, `title`,
  `resolved_outcome`, and `resolved_at`, but it is not a crosswalk. It provides
  venue-level settled-market data, not pair labels.
- Kingsets exposes public Kalshi/Polymarket market and trade tables, but no
  labeled equivalence pairs were found.

The 2024 shutdown mismatch remains a canonical negative example, but one known
negative example is not a validation corpus.

## Why This Blocks Outcome A

False positives are catastrophic. A pair classified as equivalent when the
settlement criteria differ can lose the full dollar on the supposed arbitrage.
False negatives only miss an opportunity.

Because of that asymmetry, a matcher must prove:

- zero false positives on labeled divergent pairs;
- meaningful `MATCH_HIGH_CONFIDENCE` recovery on labeled equivalent pairs;
- sustainable `MATCH_NEEDS_REVIEW` volume.

Without labeled equivalent and divergent pairs, none of those claims can be
measured. A unit-tested matcher would only prove the code follows its own rules,
not that the rules identify identical settlement.

## V1 Matcher Spec If Unblocked

Future module path:

- `bot/cross_platform_matcher.py`

No Polymarket client dependency. The matcher should accept already-normalized
market objects from future data loaders.

Input market object:

```python
{
    "venue": "kalshi" | "polymarket",
    "ticker": str,
    "question_text": str,
    "close_date": datetime | None,
    "resolution_source": str | None,
    "family": str | None,
    "category": str | None,
}
```

Output enum:

```python
class MatchResult(Enum):
    MATCH_HIGH_CONFIDENCE = "match_high_confidence"
    MATCH_NEEDS_REVIEW = "match_needs_review"
    NO_MATCH = "no_match"
    INSUFFICIENT_DATA = "insufficient_data"
```

Deterministic components:

- Date alignment: close dates must be within `24` hours for any automatic high
  confidence result. Missing dates return `INSUFFICIENT_DATA` unless a manual
  override applies.
- Keyword overlap: lowercase, ASCII-normalized, stopword-stripped Jaccard over
  question + core resolution text.
- High-confidence threshold: Jaccard `>= 0.60`, date-aligned, no conflict
  tokens, no manual block.
- Review threshold: Jaccard `>= 0.30` and `< 0.60`, or source/date evidence is
  incomplete but not contradictory.
- Resolution-source upgrade: exact or normalized-source match can upgrade
  review to high confidence only when date and keyword gates already pass.
- Ticker-family rules: explicit deterministic mappings, starting empty until
  populated from validated pairs.
- Manual overrides: explicit allow/block list wins before all algorithmic rules.

Manual override shape:

```python
{
    ("kalshi_ticker", "polymarket_slug_or_id"): {
        "decision": "allow" | "block",
        "reason": str,
        "reviewed_by": str,
        "reviewed_at": str,
    }
}
```

Block overrides must be easier to add than allow overrides. Allow overrides must
require settlement-source notes.

## Validation Methodology

Minimum validation corpus before Outcome A:

- At least `20` labeled equivalent pairs.
- At least `20` labeled divergent/near-miss pairs.
- Must include at least one pair from each intended launch category, or the
  matcher must be category-gated to only the covered categories.
- Must include known subtle wording mismatches such as shutdown-duration vs
  shutdown-occurrence style contracts.

Acceptance bar:

- False positives on labeled divergent pairs: `0`.
- High-confidence rate on labeled equivalent pairs: `>= 30%`.
- Any high-confidence false positive kills the deterministic v1 matcher.
- If the corpus has fewer than `20` labeled pairs total, ship only a design doc
  and keep the client blocked.

Validation outputs to record:

- Confusion matrix by result enum.
- False-positive table with pair text and rule that caused the miss.
- High-confidence recovery rate by category.
- Needs-review rate by category.
- Corpus provenance and whether labels are human-reviewed.

## Operator Workload Gate

The workload estimate is not defensible yet because it depends on a future
candidate-pair generator and a Polymarket market sample. Vendor claims range
from hundreds to 1,000+ active matched pairs, but those claims are AI-generated
product outputs, not our deterministic candidate workload.

Future workload gate:

- Run the deterministic matcher on a one-week active-market sample.
- Count `MATCH_NEEDS_REVIEW` candidate pairs after date/category prefilters.
- Sustainable range: `5` to `50` review items per week.
- `<5/week` likely means candidate generation is too narrow.
- `>50/week` makes manual review operationally expensive unless category scope
  is narrowed.

No auto-trading may depend on a review item until it is converted into a manual
allow override with settlement notes.

## V2 Inputs That Would Unblock This

Any one of these would justify reopening the implementation gate:

- A public, auditable historical Kalshi-Polymarket crosswalk with both exact
  matches and near-miss negatives.
- A human-labeled internal corpus built from sampled active and historical
  markets, with explicit settlement-rule notes.
- A vendor export of historical matched pairs plus enough raw text and
  methodology detail to audit false positives manually.
- A peer-reviewed deterministic matching approach with a published validation
  set.

Paid or LLM-based vendor matches can be used as candidate suggestions for human
labeling, but not as ground truth for auto-match validation.

## Out Of Scope Preserved

- No Polymarket client.
- No scanner/executor/strategy changes.
- No live or paper cross-platform trades.
- No LLM matching.
- No bot restart.

## Watch List

Re-evaluate cross-platform settlement matching only if one of these triggers
fires:

- Polymarket or another public source exposes historical settlement outcomes in
  a cleaner machine-readable form plus market pairing metadata.
- A public labeled Kalshi-Polymarket crosswalk appears.
- We intentionally create a human-reviewed internal corpus with at least the
  minimum validation size above.
- A peer-reviewed deterministic settlement-matching method with corpus appears.

Until then, the cross-platform arb path remains blocked before client
integration.
