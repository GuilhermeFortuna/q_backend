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
        # Neural specs have node_kind=None; quote specs are outside NODE_SPECS.
        if spec.source in ("neural", "quote"):
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


def test_quote_features_source_and_pinned_identifiers() -> None:
    specs = {s.name: s for s in list_feature_specs()}
    assert "quote_spread" in specs
    assert "spread_vs_volatility" in specs

    qs = specs["quote_spread"]
    sv = specs["spread_vs_volatility"]

    assert qs.source == "quote"
    assert sv.source == "quote"

    assert qs.node_kind == "feature.quote_spread"
    assert sv.node_kind == "feature.spread_vs_volatility"

    # Pinned feature identifiers from 5a71e95
    assert feature_id(qs, qs.default_params) == "quote_spread.v1.dc8f040d"
    assert feature_id(sv, sv.default_params) == "spread_vs_volatility.v1.dd58137f"


def test_quote_kinds_absent_from_genome_and_capabilities() -> None:
    from q_backend.backtesting.genome.node_specs import add_node_kinds
    from q_backend.backtesting.genome.schema import Genome
    from q_backend.backtesting.genome.score_bias import FEATURE_NAME_TO_KIND
    from q_backend.backtesting.genome.validate import GenomeValidationError, validate_genome
    from q_backend.strategy_builder.interpret_prompt import build_system_prompt
    from q_backend.strategy_builder.registry import build_capability_registry

    quote_kinds = ("feature.quote_spread", "feature.spread_vs_volatility")
    for kind in quote_kinds:
        assert kind not in NODE_SPECS
        assert kind not in add_node_kinds()

    # Score-bias mapping must not include quote kinds
    assert not any(kind in quote_kinds for kind in FEATURE_NAME_TO_KIND.values())

    # Capability registry & system prompt must not include quote kinds
    caps = build_capability_registry()
    genome_node_kinds = {n.kind for n in caps.genome_nodes}
    prompt = build_system_prompt(caps)
    for kind in quote_kinds:
        assert kind not in genome_node_kinds
        assert kind not in prompt

    # Genome naming a quote kind must fail validation
    for kind in quote_kinds:
        genome = Genome.model_validate(
            {
                "version": 1,
                "genome_id": f"test-invalid-{kind}",
                "nodes": [
                    {"id": "src", "kind": "source.close", "params": {}, "inputs": []},
                    {"id": "q", "kind": kind, "params": {}, "inputs": []},
                    {"id": "sig", "kind": "cmp.cross_above", "params": {"threshold": 0.0}, "inputs": ["src"]},
                ],
                "entry_long": {"ref": "sig"},
                "entry_short": {"ref": "sig"},
                "exit_long": {"ref": "sig"},
                "exit_short": {"ref": "sig"},
            }
        )
        with pytest.raises(GenomeValidationError, match="Unknown node kind"):
            validate_genome(genome)
