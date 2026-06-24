from typing import Any

import q_backend.backtesting.strategies  # noqa: F401 — register built-in strategies

from q_backend.backtesting.composite_entry import CompositeEntryStrategy
from q_backend.backtesting.signal_managers.registry import get_manager
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.strategy_registry import (
    get_registered_strategy,
    merge_strategy_params,
)


def build_composite_entry(
    entries: list[dict[str, Any]],
    manager_kind: str,
    manager_params: dict[str, Any],
    exit_params: dict[str, Any],
    symbol: str,
) -> CompositeEntryStrategy:
    instances = [(entry["strategy"], entry.get("params", {})) for entry in entries]
    manager = get_manager(manager_kind, manager_params)
    return CompositeEntryStrategy(
        instances=instances,
        manager=manager,
        exit_params=exit_params,
        symbol=symbol,
    )


def build_strategy(name: str, params: dict[str, Any], symbol: str) -> TradingStrategy:
    entry = get_registered_strategy(name)
    merged = merge_strategy_params(name, params)
    strategy = entry.build(merged, symbol)
    
    # Centralized hydration of full parameters and exit strategy
    strategy.parameters.update(merged)
    
    from q_backend.backtesting.exit_strategy import ExitStrategy

    strategy.exit_strategy = ExitStrategy(merged)
    
    return strategy
