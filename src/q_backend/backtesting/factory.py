from typing import Any

from q_backend.backtesting.moving_averages import normalize_ma_type
from q_backend.backtesting.strategy import MACrossoverStrategy, TradingStrategy


def build_strategy(name: str, params: dict[str, Any], symbol: str) -> TradingStrategy:
    if name == "MACrossover":
        short_period = int(params.get("short_period", 50))
        long_period = int(params.get("long_period", 200))
        threshold = float(params.get("threshold", 0.0))
        short_ma_type = normalize_ma_type(params.get("short_ma_type", "sma"))
        long_ma_type = normalize_ma_type(params.get("long_ma_type", "sma"))
        return MACrossoverStrategy(
            short_period=short_period,
            long_period=long_period,
            threshold=threshold,
            short_ma_type=short_ma_type,
            long_ma_type=long_ma_type,
            symbol=symbol,
        )

    raise ValueError(f"Unknown strategy: {name}")
