"""Build live evaluation strategies from immutable compiled deployment config."""

from __future__ import annotations

from typing import Any

from q_backend.api.schemas.backtest import BacktestRequest
from q_backend.backtesting.entry_config import normalize_entries
from q_backend.backtesting.factory import build_composite_entry, build_strategy
from q_backend.backtesting.strategy import TradingStrategy


class UnsupportedForwardStrategyError(ValueError):
    """Raised when a saved configuration cannot run on the forward evaluator."""


def build_strategy_from_compiled(
    compiled_config: dict[str, Any],
    *,
    symbol: str,
) -> TradingStrategy:
    """Hydrate a ``TradingStrategy`` through the existing registry/factory."""
    try:
        request = BacktestRequest.model_validate(compiled_config)
    except Exception as exc:
        raise UnsupportedForwardStrategyError("compiled_config is not a valid candle backtest configuration") from exc

    if request.engine == "tick":
        raise UnsupportedForwardStrategyError("tick/sub-second strategies are outside forward execution scope")

    entries, manager, exit_params = normalize_entries(request)
    if request.entries is not None:
        return build_composite_entry(
            [{"strategy": entry.strategy, "params": entry.params} for entry in entries],
            manager.kind,
            manager.params,
            exit_params,
            symbol,
        )

    entry_params = {key: value for key, value in entries[0].params.items()}
    return build_strategy(request.strategy, entry_params, symbol)
