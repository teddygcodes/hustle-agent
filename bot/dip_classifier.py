"""Dip classifier (Session 137).

Classifies live_momentum candidate dips into:
  A — state-confirmed (positive game-state signals; market dip looks real)
  B — state-deterioration (game state weakening; falling-knife pattern)
  C — unknown-context (required fields missing; classifier abstains)
  D — spread-liquidity (microstructure dominates the dip)

Ships as forward-only telemetry. Does NOT change entry/exit behavior.
Used to label decisions for downstream analysis; the Class-A entry filter
is a separate session.
"""

DIP_CLASSIFIER_VERSION = "dip_classifier_v1"


def classify_dip(
    context: dict,
    *,
    wide_spread_cents: int,
    thin_volume: float,
    dqs_min: float,
) -> tuple[str, dict]:
    spread = context.get("spread_cents")
    volume = context.get("volume_24h")
    dqs = context.get("dqs")
    momentum = context.get("momentum")
    wp_edge = context.get("wp_edge")

    diag = {"version": DIP_CLASSIFIER_VERSION, "axis_fired": None}

    if spread is not None and spread >= wide_spread_cents:
        diag["axis_fired"] = "spread_wide"
        return "D", diag

    if volume is not None and volume < thin_volume:
        diag["axis_fired"] = "volume_thin"
        return "D", diag

    if dqs is None or momentum is None or wp_edge is None:
        diag["axis_fired"] = "missing_context"
        return "C", diag

    if dqs >= dqs_min and momentum > 0 and wp_edge > 0:
        diag["axis_fired"] = "all_positive"
        return "A", diag

    diag["axis_fired"] = "state_deterioration"
    return "B", diag
