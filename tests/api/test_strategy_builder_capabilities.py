"""Drift-protection tests for WO90 capability registry."""

from __future__ import annotations

import json

from q_backend.api.routers.strategy_builder import get_strategy_builder_capabilities
from q_backend.backtesting.exit_rules.registry import EXIT_RULES
from q_backend.backtesting.genome.node_specs import NODE_SPECS
from q_backend.market_data.clients.metatrader import TIMEFRAME_NAMES
from q_backend.strategy_builder.capability_models import CapabilityRegistry, SCHEMA_VERSION
from q_backend.strategy_builder.registry import (
    UNSUPPORTED_CAPABILITIES,
    build_capability_registry,
    registered_exit_rule_ids,
    registered_genome_node_kinds,
)


def test_capability_registry_schema_version():
    registry = build_capability_registry()
    assert registry.schema_version == SCHEMA_VERSION


def test_all_genome_node_kinds_in_capabilities():
    registry = build_capability_registry()
    capability_kinds = {node.kind for node in registry.genome_nodes}
    assert capability_kinds == registered_genome_node_kinds() == set(NODE_SPECS)


def test_all_exit_rules_in_capabilities():
    registry = build_capability_registry()
    capability_ids = {rule.id for rule in registry.exit_rules}
    assert capability_ids == registered_exit_rule_ids() == {rule.id for rule in EXIT_RULES}


def test_known_unsupported_features_listed():
    registry = build_capability_registry()
    assert set(UNSUPPORTED_CAPABILITIES).issubset(set(registry.unsupported))
    assert "live_order_execution" in registry.unsupported
    assert "broker_routing" in registry.unsupported
    assert "order_book_depth" in registry.unsupported
    assert "options_greeks" in registry.unsupported
    assert "fundamental_data" in registry.unsupported


def test_response_validates_against_capability_registry_model():
    payload = get_strategy_builder_capabilities()
    assert isinstance(payload, CapabilityRegistry)
    roundtrip = CapabilityRegistry.model_validate(payload.model_dump(mode="json"))
    assert roundtrip == payload


def test_output_ordering_is_deterministic():
    first = build_capability_registry().model_dump(mode="json")
    second = build_capability_registry().model_dump(mode="json")
    assert first == second

    assert first["strategies"] == sorted(first["strategies"], key=lambda item: item["name"])
    assert first["genome_nodes"] == sorted(first["genome_nodes"], key=lambda item: item["kind"])
    assert first["operators"] == sorted(first["operators"])
    assert first["unsupported"] == sorted(first["unsupported"])
    assert first["data"]["timeframes"] == list(TIMEFRAME_NAMES)


def test_endpoint_json_is_stable():
    registry = get_strategy_builder_capabilities()
    encoded_once = json.dumps(registry.model_dump(mode="json"), sort_keys=True)
    encoded_twice = json.dumps(
        build_capability_registry().model_dump(mode="json"), sort_keys=True
    )
    assert encoded_once == encoded_twice
