"""Tests for dip classifier (Session 137)."""
from bot.dip_classifier import classify_dip, DIP_CLASSIFIER_VERSION


def _ctx(**overrides):
    base = {
        "dqs": 0.7,
        "momentum": 0.5,
        "wp_edge": 0.1,
        "spread_cents": 2,
        "volume_24h": 100,
    }
    base.update(overrides)
    return base


def test_class_a_state_confirmed():
    cls, diag = classify_dip(_ctx(), wide_spread_cents=5, thin_volume=50, dqs_min=0.40)
    assert cls == "A"
    assert diag["axis_fired"] == "all_positive"
    assert diag["version"] == DIP_CLASSIFIER_VERSION


def test_class_b_state_deterioration_negative_momentum():
    cls, diag = classify_dip(_ctx(momentum=-0.5), wide_spread_cents=5, thin_volume=50, dqs_min=0.40)
    assert cls == "B"
    assert diag["axis_fired"] == "state_deterioration"


def test_class_c_missing_dqs():
    cls, diag = classify_dip(_ctx(dqs=None), wide_spread_cents=5, thin_volume=50, dqs_min=0.40)
    assert cls == "C"
    assert diag["axis_fired"] == "missing_context"


def test_class_d_wide_spread_at_boundary():
    cls, diag = classify_dip(_ctx(spread_cents=5), wide_spread_cents=5, thin_volume=50, dqs_min=0.40)
    assert cls == "D"
    assert diag["axis_fired"] == "spread_wide"


def test_class_d_thin_volume_below_boundary():
    cls, diag = classify_dip(_ctx(volume_24h=49), wide_spread_cents=5, thin_volume=50, dqs_min=0.40)
    assert cls == "D"
    assert diag["axis_fired"] == "volume_thin"


def test_class_a_at_dqs_boundary():
    cls, _ = classify_dip(_ctx(dqs=0.40), wide_spread_cents=5, thin_volume=50, dqs_min=0.40)
    assert cls == "A"


def test_precedence_spread_before_volume_before_missing():
    cls, diag = classify_dip(
        _ctx(dqs=None, spread_cents=5, volume_24h=49),
        wide_spread_cents=5, thin_volume=50, dqs_min=0.40,
    )
    assert cls == "D"
    assert diag["axis_fired"] == "spread_wide"


def test_diagnostic_dict_shape():
    _, diag = classify_dip(_ctx(), wide_spread_cents=5, thin_volume=50, dqs_min=0.40)
    assert set(diag.keys()) == {"version", "axis_fired"}
    assert diag["version"] == "dip_classifier_v1"
