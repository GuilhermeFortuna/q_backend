from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import pandas as pd

from q_backend.backtesting.engine import BacktestEngine, ParallelMode
from q_backend.backtesting.factory import build_strategy
from q_backend.backtesting.position_sizing import (
    PositionSizingConfig,
    build_position_sizer,
)
from q_backend.optimization.metrics import build_equity_curve, compute_extended_metrics


@dataclass
class BacktestRunConfig:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    initial_capital: float
    point_value: float
    strategy: str
    strategy_params: dict[str, Any]
    position_sizing: PositionSizingConfig | None
    parallel_mode: ParallelMode = ParallelMode.SEQUENTIAL


@dataclass
class BacktestRunResult:
    metrics: dict[str, Any]
    trial_user_attrs: dict[str, Any] = field(default_factory=dict)


class BacktestRunner(Protocol):
    def run(self, config: BacktestRunConfig) -> BacktestRunResult: ...


class DefaultBacktestRunner:
    def __init__(
        self,
        data_provider: Callable[[BacktestRunConfig], pd.DataFrame] | None = None,
        market_data_service: Any | None = None,
    ):
        self._data_provider = data_provider
        self._market_data_service = market_data_service

    def _fetch_data(self, config: BacktestRunConfig) -> pd.DataFrame:
        if self._data_provider is not None:
            return self._data_provider(config)

        if self._market_data_service is None:
            from q_backend.market_data.service import MarketDataService

            self._market_data_service = MarketDataService()

        ohlcv_data = self._market_data_service.get_ohlcv(
            config.symbol,
            config.timeframe,
            config.start,
            config.end,
        )
        if not ohlcv_data:
            raise ValueError("No market data found for the given parameters.")

        df = pd.DataFrame([bar.model_dump() for bar in ohlcv_data])
        df.set_index("time", inplace=True)
        df.index = pd.to_datetime(df.index)
        return df

    def run(self, config: BacktestRunConfig) -> BacktestRunResult:
        df = self._fetch_data(config)
        strategy = build_strategy(
            config.strategy, config.strategy_params, config.symbol
        )
        sizer = build_position_sizer(config.position_sizing)
        engine = BacktestEngine(
            strategy,
            sizer,
            initial_capital=config.initial_capital,
            point_values={config.symbol: config.point_value},
        )
        registry = engine.run(df, parallel_mode=config.parallel_mode)

        closed_trades = registry.get_closed_trades()
        base_metrics = registry.get_performance_metrics(config.initial_capital)
        equity_curve = build_equity_curve(
            closed_trades,
            config.initial_capital,
            config.start,
            config.end,
        )
        backtest_days = max((config.end - config.start).total_seconds() / 86400, 1.0)
        metrics = compute_extended_metrics(
            base_metrics,
            closed_trades,
            config.initial_capital,
            equity_curve,
            backtest_days,
        )

        return BacktestRunResult(
            metrics=metrics,
            trial_user_attrs={
                "total_trades": metrics.get("total_trades", 0),
            },
        )
