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
    strategy = entry.build(merged, symbol)
    
    # Centralized hydration of full parameters and exit strategy
    strategy.parameters.update(merged)
    
    from q_backend.backtesting.exit_strategy import ExitStrategy
    strategy.exit_strategy = ExitStrategy(
        stop_loss_pct=float(merged.get("stop_loss_pct", 0.0)),
        take_profit_pct=float(merged.get("take_profit_pct", 0.0)),
        trailing_stop_pct=float(merged.get("trailing_stop_pct", 0.0)),
        stop_loss_atr=float(merged.get("stop_loss_atr", 0.0)),
        take_profit_atr=float(merged.get("take_profit_atr", 0.0)),
        atr_period=int(merged.get("atr_period", 14)),
    )
    
    return strategy
