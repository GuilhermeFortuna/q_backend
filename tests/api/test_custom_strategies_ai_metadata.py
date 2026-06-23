"""Tests for AI metadata on custom strategy persistence (WO95)."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from q_backend.api.routers.strategies import (
    CustomStrategySaveRequest,
    delete_custom_strategy,
    get_custom_strategies,
    save_custom_strategy,
)
from q_backend.backtesting.ai_strategy_metadata import AiStrategyMetadata
from q_backend.backtesting.custom_strategy_store import save_custom_strategies
from q_backend.strategy_builder.compiler import compile_strategy_spec_payload

EMA_CROSS_SPEC = {
    "schema_version": "strategy_spec.v1",
    "name": "EMA Trend Cross",
    "universe": ["PETR4"],
    "market": "B3",
    "timeframe": "D1",
    "indicators": [
        {"id": "ema_fast", "type": "ema", "source": "close", "period": 20},
        {"id": "ema_slow", "type": "ema", "source": "close", "period": 50},
    ],
    "entry": {
        "all": [
            {
                "left": "ema_fast",
                "op": "crosses_above",
                "right": "ema_slow",
            }
        ]
    },
    "exit": {
        "any": [
            {
                "left": "ema_fast",
                "op": "crosses_below",
                "right": "ema_slow",
            },
            {"type": "stop_loss", "mode": "percent", "value": 0.03},
        ]
    },
    "risk": {"position_sizing": "fixed_quantity", "quantity": 1.0},
    "execution_assumptions": {
        "signal_timing": "closed_bar",
        "entry_timing": "next_bar_open",
        "allow_short": False,
    },
}


@pytest.fixture(autouse=True)
def clean_custom_strategies(tmp_path, monkeypatch):
    mock_file = tmp_path / "custom_strategies.json"
    monkeypatch.setattr(
        "q_backend.backtesting.custom_strategy_store.custom_strategies_path",
        lambda: mock_file,
    )
    save_custom_strategies([])


def _ai_metadata(**overrides) -> AiStrategyMetadata:
    compiled = compile_strategy_spec_payload(EMA_CROSS_SPEC)
    payload = {
        "strategy_spec": EMA_CROSS_SPEC,
        "strategy_spec_version": "strategy_spec.v1",
        "capabilities_version": "q_capabilities.v1",
        "original_prompt": "Create an EMA crossover with a stop.",
        "assumptions": ["Uses closed-bar signals."],
        "unsupported_requests_acknowledged": ["Live trading"],
        "compiled_strategy_id": compiled.compiled_id,
        "compiled_strategy": compiled.model_dump(mode="json"),
    }
    payload.update(overrides)
    return AiStrategyMetadata.model_validate(payload)


def test_old_custom_strategies_without_ai_metadata_still_load():
    save_custom_strategies(
        [
            {
                "name": "LegacyCustom",
                "base_strategy": "RSIMeanReversion",
                "description": "Legacy only",
                "parameters": {"period": 14, "oversold": 30.0, "overbought": 70.0},
            }
        ]
    )

    response = get_custom_strategies()
    assert len(response) == 1
    assert response[0]["name"] == "LegacyCustom"
    assert "ai_metadata" not in response[0]


def test_ai_metadata_is_optional_and_additive_on_save():
    legacy = CustomStrategySaveRequest(
        name="LegacyCustom",
        base_strategy="RSIMeanReversion",
        description="Legacy only",
        parameters={"period": 14, "oversold": 30.0, "overbought": 70.0},
    )
    save_custom_strategy(legacy)

    ai = CustomStrategySaveRequest(
        name="AiCustom",
        base_strategy="CompositeStrategy",
        description="AI authored",
        parameters={"exit_stop_loss_pct": 0.03},
        ai_metadata=_ai_metadata(),
    )
    save_custom_strategy(ai)

    customs = get_custom_strategies()
    assert len(customs) == 2
    legacy_row = next(item for item in customs if item["name"] == "LegacyCustom")
    ai_row = next(item for item in customs if item["name"] == "AiCustom")
    assert "ai_metadata" not in legacy_row
    assert ai_row["ai_metadata"]["strategy_spec_version"] == "strategy_spec.v1"
    assert ai_row["ai_metadata"]["compiled_strategy_id"]


def test_saved_ai_spec_round_trips():
    metadata = _ai_metadata()
    req = CustomStrategySaveRequest(
        name="AiRoundTrip",
        base_strategy="CompositeStrategy",
        description="Round trip",
        parameters={"exit_stop_loss_pct": 0.03},
        ai_metadata=metadata,
    )
    save_custom_strategy(req)

    loaded = get_custom_strategies()[0]
    restored = AiStrategyMetadata.model_validate(loaded["ai_metadata"])
    assert restored.strategy_spec == metadata.strategy_spec
    assert restored.original_prompt == metadata.original_prompt
    assert restored.compiled_strategy_id == metadata.compiled_strategy_id


def test_non_ai_update_preserves_existing_ai_metadata():
    metadata = _ai_metadata()
    save_custom_strategy(
        CustomStrategySaveRequest(
            name="AiPersist",
            base_strategy="CompositeStrategy",
            description="v1",
            parameters={"exit_stop_loss_pct": 0.03},
            ai_metadata=metadata,
        )
    )

    save_custom_strategy(
        CustomStrategySaveRequest(
            name="AiPersist",
            base_strategy="CompositeStrategy",
            description="v2",
            parameters={"exit_stop_loss_pct": 0.04},
        )
    )

    loaded = get_custom_strategies()[0]
    assert loaded["description"] == "v2"
    assert loaded["parameters"]["exit_stop_loss_pct"] == 0.04
    assert loaded["ai_metadata"]["original_prompt"] == metadata.original_prompt


def test_compiled_payload_hash_is_deterministic_for_saved_metadata():
    first = compile_strategy_spec_payload(EMA_CROSS_SPEC)
    second = compile_strategy_spec_payload(copy.deepcopy(EMA_CROSS_SPEC))
    assert first.compiled_id == second.compiled_id

    metadata = _ai_metadata(compiled_strategy_id=first.compiled_id)
    assert metadata.compiled_strategy_id == first.compiled_id


def test_invalid_ai_metadata_rejected():
    with pytest.raises(ValidationError):
        AiStrategyMetadata.model_validate(
            {
                "strategy_spec": EMA_CROSS_SPEC,
                "strategy_spec_version": "strategy_spec.v1",
                "capabilities_version": "q_capabilities.v1",
                "original_prompt": "",
                "assumptions": [],
                "unsupported_requests_acknowledged": [],
                "compiled_strategy_id": None,
                "compiled_strategy": None,
            }
        )
