"""Research-only capability gating of the ML-filter variant (Q-087)."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from q_backend.api.routers.strategies import list_strategies
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.strategy_registry import StrategiesResponse, UnsupportedStrategyWorkflowError
from q_backend.execution.catalog import build_catalog
from q_backend.execution.strategy_build import UnsupportedForwardStrategyError, build_strategy_from_compiled
from q_backend.execution.validation import (
    ExecutionValidationError,
    compute_config_hash,
    validate_strategy_identity,
)
from q_backend.optimization.models import (
    BacktestConfig,
    ObjectiveConfig,
    ObjectiveMode,
    StudyConfig,
)
from q_backend.optimization.strategy_search import StrategySearchConfig
from q_backend.optimization.walkforward import WalkForwardConfig
from q_backend.strategy_builder.registry import _builtin_strategies

VARIANT = "MACrossoverMLFilter"
BACKTEST = {"symbol": "WIN$", "timeframe": "D1", "start": datetime(2024, 1, 1), "end": datetime(2024, 4, 30)}


def test_strategies_endpoint_advertises_capabilities_only_for_the_variant():
    payload = StrategiesResponse(**list_strategies()).model_dump(mode="json")
    by_name = {item["name"]: item for item in payload["strategies"]}

    assert set(by_name[VARIANT]["capabilities"]) == {"ml_entry_filter", "research_only"}
    assert all("capabilities" not in item for name, item in by_name.items() if name != VARIANT)


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy": VARIANT},
        {"entries": [{"strategy": VARIANT, "params": {}}]},
        {"ml_filter": {"model_version_id": "a" * 64, "threshold": 0.5}},
    ],
)
def test_optimization_and_walkforward_configs_reject_the_variant(overrides):
    with pytest.raises(ValidationError, match="research-only|single candle backtests"):
        BacktestConfig(**BACKTEST, **overrides)
    BacktestConfig(**BACKTEST, strategy="MACrossover")


def _search(strategies: list[str] | None) -> StrategySearchConfig:
    return StrategySearchConfig(
        backtest={**BACKTEST, "strategy": "MACrossover"},
        objective=ObjectiveConfig(mode=ObjectiveMode.MAXIMIZE_NET_PROFIT),
        walkforward=WalkForwardConfig(train_days=30, test_days=15, mode="rolling", min_windows=2),
        study=StudyConfig(name="sweep", n_trials=2, seed=1, storage={"type": "memory"}),
        strategies=strategies,
        include_risk_search=False,
    )


def test_discovery_rejects_explicit_use_and_default_sweeps_skip_the_variant():
    with pytest.raises(ValidationError, match="research-only"):
        _search(["MACrossover", VARIANT])

    from q_backend.optimization.strategy_search import list_registered_strategies

    default_names = [
        info.name
        for info in list_registered_strategies()
        if "research_only" not in info.capabilities and info.engine == "candle"
    ]
    assert "MACrossover" in default_names and VARIANT not in default_names


def test_live_catalog_validation_and_builder_exclude_the_variant():
    assert VARIANT not in {entry.name for entry in build_catalog()}
    assert "MACrossover" in {entry.name for entry in build_catalog()}
    assert VARIANT not in {info.name for info in _builtin_strategies()}


def test_forward_deployment_validation_rejects_the_variant():
    config = {"symbol": "WIN$", "timeframe": "M15", "strategy": VARIANT}
    with pytest.raises(ExecutionValidationError, match="research-only"):
        validate_strategy_identity(
            strategy_name=VARIANT,
            strategy_version=1,
            compiled_config=config,
            config_hash=compute_config_hash(config),
            symbol="WIN$",
            timeframe="M15",
            sizing_config={},
        )


@pytest.mark.parametrize(
    "config",
    [
        {"symbol": "WIN$", "timeframe": "M15", "strategy": VARIANT},
        {"symbol": "WIN$", "timeframe": "M15", "entries": [{"strategy": VARIANT, "params": {}}]},
        {
            "symbol": "WIN$",
            "timeframe": "M15",
            "strategy": "MACrossover",
            "ml_filter": {"model_version_id": "a" * 64, "threshold": 0.5},
        },
    ],
)
def test_forward_execution_cannot_build_ml_filter_configurations(config):
    with pytest.raises(UnsupportedForwardStrategyError, match="research-only"):
        build_strategy_from_compiled(config, symbol="WIN$")


def test_building_the_variant_bare_is_refused_so_the_gate_cannot_be_dropped():
    with pytest.raises(UnsupportedStrategyWorkflowError, match="research-only"):
        build_strategy(VARIANT, {}, "WIN$")
    assert build_strategy("MACrossover", {}, "WIN$") is not None
