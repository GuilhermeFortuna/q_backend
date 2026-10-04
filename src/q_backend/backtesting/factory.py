from datetime import datetime
from typing import Any

import q_backend.backtesting.strategies  # noqa: F401 — register built-in strategies

from q_backend.backtesting.composite_entry import CompositeEntryStrategy
from q_backend.backtesting.signal_managers.registry import get_manager
from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.strategy_registry import (
    get_registered_strategy,
    merge_strategy_params,
    reject_research_only_strategy,
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
    # Research-only variants need their post-combination hydration; building them bare
    # would silently drop the gate.
    reject_research_only_strategy(name, "this workflow")
    entry = get_registered_strategy(name)
    merged = merge_strategy_params(name, params)
    strategy = entry.build(merged, symbol)

    # Centralized hydration of full parameters and exit strategy
    strategy.parameters.update(merged)

    from q_backend.backtesting.exit_strategy import ExitStrategy

    strategy.exit_strategy = ExitStrategy(merged)

    return strategy


def wrap_with_ml_filter(
    strategy: TradingStrategy,
    fitted_model: Any,
    threshold: float,
    *,
    entry_start: datetime | None = None,
    entry_end: datetime | None = None,
) -> TradingStrategy:
    """Gate the finished, combined entry strategy with a frozen Q-086 classifier."""
    from q_backend.ml_filters.filter import EntryFilteredStrategy

    return EntryFilteredStrategy(
        strategy,
        fitted_model,
        threshold,
        entry_start=entry_start,
        entry_end=entry_end,
    )
