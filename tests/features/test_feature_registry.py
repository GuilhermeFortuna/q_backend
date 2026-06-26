"""Tests for the FeatureSpec registry (WO127)."""

from __future__ import annotations

import pytest

from q_backend.backtesting.genome.node_specs import NODE_SPECS
from q_backend.features.registry import (
    assert_catalog_consistent,
    feature_id,
    get_feature_spec,
    list_feature_specs,
    resolve_params,
)


def test_assert_catalog_consistent_passes() -> None:
    assert_catalog_consistent()
    for spec in list_feature_specs():
        # Neural specs have node_kind=None (their consistency is checked separately
        # in assert_catalog_consistent); the NODE_SPECS mapping is classical-only.
        if spec.source == "neural":
            continue
        assert spec.param_keys == NODE_SPECS[spec.node_kind].allowed_param_keys
        assert spec.forward_window == 0


def test_get_feature_spec_returns_latest_rsi() -> None:
    spec = get_feature_spec("rsi")
    assert spec.name == "rsi"
    assert spec.version == 1
    assert spec.node_kind == "ind.rsi"
    assert spec.lookback_param == "period"


def test_get_feature_spec_rejects_unknown_version() -> None:
    with pytest.raises(KeyError, match="no version 2"):
        get_feature_spec("rsi", version=2)


def test_resolve_params_merges_valid_override() -> None:
    spec = get_feature_spec("rsi")
    resolved = resolve_params(spec, {"period": 21})
    assert resolved["period"] == 21


def test_resolve_params_rejects_unknown_key() -> None:
    spec = get_feature_spec("rsi")
    with pytest.raises(ValueError, match="Unknown params"):
        resolve_params(spec, {"bogus": 1})


def test_feature_id_is_stable_and_param_sensitive() -> None:
    spec = get_feature_spec("rsi")
    base = resolve_params(spec, {})
    alt = resolve_params(spec, {"period": 21})
    first = feature_id(spec, base)
    second = feature_id(spec, base)
    third = feature_id(spec, alt)
    assert first == second
    assert third != first
    assert first.startswith("rsi.v1.")
