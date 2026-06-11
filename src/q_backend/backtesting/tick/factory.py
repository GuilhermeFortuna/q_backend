from typing import Any

import q_backend.backtesting.tick.strategies  # noqa: F401 — register tick strategies

from q_backend.backtesting.strategy_registry import (
    get_registered_strategy,
    merge_strategy_params,
)
from q_backend.backtesting.tick.strategy import TickStrategy


def build_tick_strategy(name: str, params: dict[str, Any], symbol: str) -> TickStrategy:
    entry = get_registered_strategy(name)
    if entry.info.engine != "tick":
        raise ValueError(f"Strategy '{name}' is not a tick engine strategy.")
    merged = merge_strategy_params(name, params)
    strategy = entry.build(merged, symbol)
    if not isinstance(strategy, TickStrategy):
        raise TypeError(f"Strategy '{name}' did not build a TickStrategy instance.")
    return strategy
