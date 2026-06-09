from typing import Any

import q_backend.backtesting.strategies  # noqa: F401 — register built-in strategies

from q_backend.backtesting.strategy import TradingStrategy
from q_backend.backtesting.strategy_registry import (
    get_registered_strategy,
    merge_strategy_params,
)


def build_strategy(name: str, params: dict[str, Any], symbol: str) -> TradingStrategy:
    entry = get_registered_strategy(name)
    merged = merge_strategy_params(name, params)
    return entry.build(merged, symbol)
